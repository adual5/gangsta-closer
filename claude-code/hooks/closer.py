#!/usr/bin/env python3
"""Gangsta Closer for Claude Code: play your clip when a tool call closes something.

  closer.py pre     PreToolUse hook: record the state of anything the call is about to close
  closer.py post    PostToolUse / PostToolUseFailure hook: re-read it; play only on a not-done -> done change
  closer.py clip    /gangsta-closer:clip: set, show, test or reset your clip

What counts as done (same idea as the extension; closes that aren't wins stay silent):
  GitHub issue   state CLOSED with stateReason COMPLETED (not "not planned" or duplicate)
  GitHub PR      state MERGED (a PR closed without merging, or queued with --auto, is silent)
  Linear         workflow state type "completed" ("canceled" covers Won't Do and Duplicate)
  Jira Cloud     status category Done
  Asana          task completed

GitHub state is read with `gh` before and after the call. The hook has no token for
Linear, Jira or Asana, so those come from the MCP call itself: its input, its result, and
the state listings Claude fetched earlier in the session (from the transcript).

Never fails a tool call: any error means silence. GANGSTA_CLOSER_DEBUG=1 logs to
~/.gangsta-closer/debug.log.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # not POSIX
    fcntl = None

HOME = Path(os.environ.get("GANGSTA_CLOSER_HOME") or Path.home() / ".gangsta-closer")
PENDING = HOME / "pending"
CLIP_FORMATS = (".mp3", ".wav", ".m4a") if sys.platform == "darwin" else (".mp3", ".wav", ".m4a", ".ogg")
MAX_CLIP_BYTES = 5 * 1024 * 1024
COOLDOWN_S = 3
PENDING_TTL_S = 6 * 3600  # a call can wait on a permission prompt or run for a long time
GH_TIMEOUT_S = 6
PRE_DEADLINE_S = 11  # stay well under the 15 s PreToolUse timeout in hooks.json
MAX_TARGETS = 8
WATCH_S = 30 * 60  # how long a background Bash call's items are watched for the close to land
WATCH_EVERY_S = 10
UNCHANGED_S = 30  # a Linear updatedAt older than this means the save changed nothing
ALREADY_DONE_S = 120  # without updatedAt, a completedAt older than this means the issue was already done


def private_dir(path):
    """mkdir -p with owner-only permissions: pending files and the log name private repos."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def write_private(path, text, append=False):
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    with os.fdopen(os.open(path, flags, 0o600), "a" if append else "w") as f:
        f.write(text)


def log(*parts):
    if os.environ.get("GANGSTA_CLOSER_DEBUG") != "1":
        return
    try:
        private_dir(HOME)
        write_private(HOME / "debug.log", time.strftime("%H:%M:%S ") + " ".join(str(p) for p in parts) + "\n",
                      append=True)
    except OSError:
        pass


# ---------------------------------------------------------------- GitHub via gh

# gh fills in {owner}/{repo} (or :owner/:repo) from the working copy.
GH_API_PATH = re.compile(r"^(?:https://api\.github\.com)?/?repos/([^/]+)/([^/]+)/(issues|pulls)/(\d+)(/merge)?/?$")
GH_PLACEHOLDERS = {("{owner}", "{repo}"), (":owner", ":repo")}
# The hook runs before the user approves the call, so it only ever talks to github.com, about
# a plain owner/name repo and a number, a github.com URL, or a branch name.
SAFE_REPO = re.compile(r"^(?:https://)?(?:github\.com/)?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$")
GITHUB_ITEM_URL = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(issues|pull)/(\d+)(?=$|[/#?])")
BRANCH = re.compile(r"^[A-Za-z0-9_][\w./+@-]*$")


def safe_ref(ref):
    """A number, a github.com issue/PR URL (as its plain form, so /files or #comment-1 still work), or a
    branch name. None for anything else, such as a URL on another host."""
    if ref.isdigit():
        return ref
    m = GITHUB_ITEM_URL.match(ref)
    if m:
        return "https://github.com/{}/{}/{}/{}".format(*m.groups())
    return ref if BRANCH.match(ref) and "://" not in ref else None


def gh_env():
    env = {k: v for k, v in os.environ.items() if k not in ("GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")}
    env["GH_HOST"] = "github.com"
    return env


def gh_view(target, cwd):
    """target is {"kind": "issue"|"pr", "ref": number, URL or branch, or None, "repo": "o/r" or None}.
    Returns {"url", "done"} or None when gh can't read it."""
    ref, repo = target.get("ref"), target.get("repo")
    m = SAFE_REPO.match(repo) if repo else None
    if (ref and not safe_ref(ref)) or (repo and not m):
        log("skipped, not a plain github.com target:", target)
        return None
    ref, repo = safe_ref(ref) if ref else None, m.group(1) if m else None
    fields = "url,state,stateReason" if target["kind"] == "issue" else "url,state"
    cmd = ["gh", target["kind"], "view"] + ([ref] if ref else [])
    if repo:
        cmd += ["-R", repo]
    cmd += ["--json", fields]
    try:
        out = subprocess.run(cmd, cwd=cwd or None, capture_output=True, text=True, timeout=GH_TIMEOUT_S,
                             env=gh_env())
    except (OSError, subprocess.TimeoutExpired) as e:
        log("gh failed:", cmd, e)
        return None
    if out.returncode != 0:
        log("gh error:", cmd, out.stderr.strip())
        return None
    try:
        data = json.loads(out.stdout)
    except ValueError:
        return None
    if target["kind"] == "issue":
        done = data.get("state") == "CLOSED" and data.get("stateReason") == "COMPLETED"
    else:
        done = data.get("state") == "MERGED"
    return {"url": data.get("url"), "done": done}


HEREDOC = re.compile(r"(?<!<)<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")


def strip_heredocs(command):
    """Drop heredoc bodies: they're text, not commands, and an apostrophe in one breaks the lexer.
    A `<<` with no terminator line further down (`print(1<<n)`) isn't a heredoc, so nothing is dropped."""
    lines, out, i = command.split("\n"), [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for m in HEREDOC.finditer(line):
            dash, word = m.group(1) == "-", m.group(3)
            j = i
            while j < len(lines) and (lines[j].lstrip("\t") if dash else lines[j]) != word:
                j += 1
            if j < len(lines):
                i = j + 1
    return "\n".join(out)


def strip_comments(command):
    """Drop `#` comments the way the shell does: only a `#` that starts a word outside quotes. (shlex's own
    comment handling eats the newline after one, gluing the next line's command onto it; and once shlex has
    removed the quotes, "#12 fix" looks like a comment.)"""
    out, quote, i, n = [], None, 0, len(command)
    while i < n:
        c = command[i]
        if quote:
            out.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                out.append(command[i + 1])
                i += 1
            elif c == quote:
                quote = None
        elif c == "\\" and i + 1 < n:
            out.append(c + command[i + 1])
            i += 1
        elif c in "'\"":
            quote = c
            out.append(c)
        elif c == "#" and (i == 0 or command[i - 1] in " \t\n;&|("):
            while i < n and command[i] != "\n":
                i += 1
            continue
        else:
            out.append(c)
        i += 1
    return "".join(out)


def lex(command):
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""  # strip_comments already removed them
    return list(lexer)


def split_commands(command):
    """Split a shell command into simple-command token lists (on && || ; | and newlines)."""
    command = strip_comments(strip_heredocs(command.replace("\\\n", " ")))
    try:
        tokens = lex(command)
    except ValueError:
        # One unbalanced quote shouldn't hide every other line: lex the lines that parse.
        tokens = []
        for line in command.split("\n"):
            try:
                tokens += lex(line) + ["\n"]
            except ValueError:
                pass
    out, cur = [], []
    for t in tokens:
        if t and set(t) <= set(";&|\n"):
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        out.append(cur)
    return out


def flag_value(args, *names):
    for i, a in enumerate(args):
        for n in names:
            if a == n and i + 1 < len(args):
                return args[i + 1]
            if n.startswith("--") and a.startswith(n + "="):
                return a[len(n) + 1:]
            if not n.startswith("--") and a.startswith(n) and len(a) > len(n):
                return a[len(n):]
    return None


def positionals(args):
    """Positional args, skipping flags and the values of flags that take one."""
    takes_value = {"-R", "--repo", "-r", "--reason", "-c", "--comment", "-b", "--body", "-F", "--body-file",
                   "-t", "--subject", "-A", "--author-email", "--match-head-commit", "--duplicate-of"}
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a in takes_value:
            skip = True
        elif not a.startswith("-"):
            out.append(a)
    return out


def gh_targets_from_bash(command, cwd=None):
    """GitHub items a Bash command may close or merge: gh issue close, gh pr merge, gh api.
    Follows `cd` earlier in the command, so `cd repo && gh pr merge` reads that repo's PR."""
    targets = []
    branch = None  # set by an earlier `git checkout` / `git switch` in the same command
    created = False  # an earlier `gh pr create`: a ref-less merge is for a PR that doesn't exist yet
    for words in split_commands(command):
        if words[0] == "cd" and len(words) == 2:
            cwd = os.path.join(cwd or os.getcwd(), os.path.expanduser(words[1]))
            continue
        # Skip env assignments and wrappers: `GH_REPO=x gh ...`, `env gh ...`, `/opt/homebrew/bin/gh ...`
        i, assigned = 0, {}
        while i < len(words) and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[i]) or words[i] in ("env", "command", "sudo")):
            if "=" in words[i]:
                k, v = words[i].split("=", 1)
                assigned[k] = v
            i += 1
        if i >= len(words):
            continue
        if os.path.basename(words[i]) == "git" and words[i + 1:i + 2] in (["checkout"], ["switch"]):
            names = [w for w in words[i + 2:] if not w.startswith("-")]
            if len(names) == 1 and names[0] not in (".", ".."):  # `git checkout .` restores files
                branch = names[0]
            continue
        if os.path.basename(words[i]) != "gh":
            continue
        if assigned.get("GH_HOST", "github.com") != "github.com":
            continue  # GitHub Enterprise isn't supported
        args = words[i + 1:]
        repo = flag_value(args, "-R", "--repo") or assigned.get("GH_REPO")
        if args[:2] == ["pr", "create"]:
            created = True
        elif args[:2] == ["issue", "close"]:
            for ref in positionals(args[2:])[:1]:
                targets.append({"kind": "issue", "ref": ref, "repo": repo, "cwd": cwd})
        elif args[:2] == ["pr", "merge"]:
            refs = positionals(args[2:])[:1]
            t = {"kind": "pr", "ref": refs[0] if refs else branch, "repo": repo, "cwd": cwd}
            if not refs and created:
                t["new"] = True
            targets.append(t)
        elif args[:1] == ["api"]:
            for p in positionals(args[1:]):
                m = GH_API_PATH.match(p)
                if not m:
                    continue
                owner, name, kind, num, merge = m.groups()
                full = None if (owner, name) in GH_PLACEHOLDERS else f"{owner}/{name}"
                if kind == "pulls" and merge:
                    targets.append({"kind": "pr", "ref": num, "repo": full, "cwd": cwd})
                elif kind == "issues":
                    targets.append({"kind": "issue", "ref": num, "repo": full, "cwd": cwd})
    return targets[:MAX_TARGETS]


def gh_targets_from_mcp(tool, args):
    """GitHub MCP servers (github/github-mcp-server, and the older reference server's update_issue):
    issue updates and PR merges."""
    owner, repo = args.get("owner"), args.get("repo")
    if not owner or not repo:
        return []
    full = f"{owner}/{repo}"
    issue = args.get("issue_number") or args.get("issueNumber")
    pr = args.get("pullNumber") or args.get("pull_number")
    if "merge" in tool and pr:
        return [{"kind": "pr", "ref": str(pr), "repo": full}]
    if issue and args.get("state") == "closed":
        return [{"kind": "issue", "ref": str(issue), "repo": full}]
    return []


# ---------------------------------------------------------------- Linear, Jira, Asana via the MCP result

def result_text(response):
    """Flatten an MCP tool_response (string, content blocks, or JSON) to one string."""
    if response is None:
        return ""
    if isinstance(response, str):
        return response
    if isinstance(response, list):
        return "\n".join(result_text(r) for r in response)
    if isinstance(response, dict):
        if "text" in response and isinstance(response["text"], str):
            return response["text"]
        if "content" in response:
            return result_text(response["content"])
    return json.dumps(response)


def walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def json_objects(text):
    """Parse text that is JSON, or holds JSON objects after a line of prose."""
    text = text.strip()
    if not text:
        return []
    try:
        return [json.loads(text)]
    except ValueError:
        pass
    found = []
    for m in re.finditer(r"(?m)^[\[{]", text):
        try:
            found.append(json.JSONDecoder().raw_decode(text[m.start():])[0])
        except ValueError:
            pass
    return found


def deep_dicts(obj):
    """Every dict in obj, including ones inside JSON-encoded strings (tool results in the transcript)."""
    for d in walk(obj):
        yield d
        for v in d.values():
            if isinstance(v, str) and v[:1] in "[{" and len(v) > 2:
                for inner in json_objects(v):
                    yield from deep_dicts(inner)


def transcript_dicts(path, max_bytes=4 * 1024 * 1024):
    """Dicts from the end of the session transcript, newest first."""
    out = []
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - max_bytes))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except (OSError, TypeError):
        return out
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        out.extend(reversed(list(deep_dicts(entry))))
    return out


def response_dicts(response):
    return [d for obj in json_objects(result_text(response)) for d in deep_dicts(obj)] + \
        ([d for d in deep_dicts(response)] if isinstance(response, (dict, list)) else [])


# Same done words as the extension (src/content/engine.js), used only when a state's type is unknown.
DONE_NAME = re.compile(r"^[^\w]*(done|closed|complete|completed|resolved|merged)(?![^\W\d_])", re.I)
NOT_A_WIN = re.compile(r"not planned|won['’]?t|cancel|duplicate|rejected|declined|obsolete", re.I)


LINEAR_STATE_TYPES = {"triage", "backlog", "unstarted", "started", "completed", "canceled", "duplicate"}


def age_s(stamp):
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds()


def already_completed(response):
    """True when this call didn't change an issue that was already done. Saving Done on a done issue
    leaves updatedAt alone, while a reopened issue closed again gets its old completedAt back (both seen
    live), so updatedAt decides; completedAt is only the fallback for a result without it."""
    for d in response_dicts(response):
        if not d.get("id"):
            continue
        updated = age_s(d.get("updatedAt"))
        if updated is not None:
            return updated > UNCHANGED_S
        completed = age_s(d.get("completedAt"))
        if completed is not None:
            return completed > ALREADY_DONE_S
    return False


def linear_done(args, response, transcript):
    """save_issue on an existing issue (`id`) that moves it to a state of type "completed"."""
    if not args.get("id"):
        return False  # creating an issue isn't closing one
    want = next((args[k] for k in ("state", "stateId", "status", "statusId") if args.get(k)), None)
    if not want:
        return False
    want = str(want)
    if already_completed(response):
        return False  # re-sending Done to an issue that was already done isn't a close
    if result_text(response).strip() and not json_objects(result_text(response)):
        return False  # an error message, not an issue (Claude Code reports these as failures anyway)
    # 0. save_issue also takes a state type ("completed", "canceled") in place of a name or ID.
    if want.lower() in LINEAR_STATE_TYPES:
        return want.lower() == "completed"
    # 1. The result's own issue state: save_issue returns {"status": "Done", "statusType": "completed"}.
    for d in response_dicts(response):
        if isinstance(d.get("statusType"), str) and d.get("id"):
            return d["statusType"] == "completed"
        for k in ("state", "status"):
            st = d.get(k)
            if isinstance(st, dict) and st.get("type"):
                return st["type"] == "completed"
    # 2. The state's type from an earlier list_issue_statuses (or any listing) in this session.
    for d in transcript:
        if isinstance(d.get("type"), str) and d["type"] in LINEAR_STATE_TYPES and (d.get("id") == want or str(d.get("name", "")).lower() == want.lower()):
            return d["type"] == "completed"
    # 3. Its name, by the extension's rule.
    return bool(DONE_NAME.search(want)) and not NOT_A_WIN.search(want)


def jira_done(args, response, transcript):
    """transitionJiraIssue that lands the issue in a status in the Done category. Its result is
    {"data": {"transitionId": 41, "statusName": "Done"}}, with no category, so the category comes from
    the listJiraIssueTransitions result Claude fetched first (each transition's "to" status carries
    one). Without that listing, the landed status name decides, by the extension's done-name rule."""
    results = response_dicts(response)
    for d in results:
        cat = d.get("statusCategory")
        if isinstance(cat, dict) and cat.get("key"):
            return cat["key"] == "done"
    t = args.get("transition")
    tid = t.get("id") if isinstance(t, dict) else None
    tid = tid or args.get("transitionId") or next((d["transitionId"] for d in results if d.get("transitionId")), None)
    landed = next((d["statusName"] for d in results if isinstance(d.get("statusName"), str)), None)
    if tid is not None:
        # Transition IDs are only unique within one workflow, so a listing for another issue can hold
        # the same ID with a different target. When the result names the landed status, only a
        # transition to that status counts.
        for d in transcript:
            to = d.get("to")
            if str(d.get("id")) == str(tid) and isinstance(to, dict) and isinstance(to.get("statusCategory"), dict):
                if landed and str(to.get("name") or "").lower() != landed.lower():
                    continue
                # Teams file Won't Do and Duplicate under Done too; like the extension, those stay silent.
                return to["statusCategory"].get("key") == "done" and \
                    not NOT_A_WIN.search(str(to.get("name") or d.get("name") or ""))
    if landed:
        return bool(DONE_NAME.search(landed)) and not NOT_A_WIN.search(landed)
    return False


def asana_done(args, response):
    """update_tasks that sets completed: true (or approves an approval task, which completes it) on a
    task the result lists as updated. update_tasks reports per task: {"data": [{"gid": ...}],
    "failed": [...]}, and a failed update is still a normal result, so the input alone isn't enough.
    Single-task tools take task_id and return one {"data": {"gid": ...}}."""
    wanted = {str(d.get("task") or d.get("task_gid") or d.get("task_id") or d.get("gid")) for d in walk(args)
              if d.get("completed") in (True, "true") or d.get("approval_status") == "approved"}
    if not wanted:
        return False
    updated = set()
    for d in response_dicts(response):
        data = d.get("data")
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict) and item.get("gid"):
                updated.add(str(item["gid"]))
    return bool(wanted & updated)


def session_dicts(event):
    """Transcript dicts, newest first. In a subagent the hook still gets the parent's transcript_path,
    while the subagent's own calls (the listing it fetched first) are in
    <session>/subagents/agent-<agent_id>.jsonl (seen live), so that one is read first."""
    path = event.get("transcript_path")
    agent = str(event.get("agent_id") or "")
    out = []
    if path and re.fullmatch(r"[A-Za-z0-9_-]+", agent):
        out = transcript_dicts(str(Path(path).with_suffix("") / "subagents" / f"agent-{agent}.jsonl"))
    return out + transcript_dicts(path)


def mcp_done(provider, tool, event):
    args = event.get("tool_input") or {}
    response = event.get("tool_response")
    if provider == "linear" and tool in ("save_issue", "update_issue"):
        return linear_done(args, response, session_dicts(event))
    if provider == "jira" and tool == "transitionjiraissue":
        return jira_done(args, response, session_dicts(event))
    if provider == "asana" and tool in ("update_tasks", "update_task", "asana_update_task"):
        return asana_done(args, response)
    return False


# ---------------------------------------------------------------- server detection

# Claude.ai connectors in the desktop app get opaque server names (mcp__4ba31cde-...__tool), so the
# tool name alone has to identify the service. These are the official servers' state-changing tools.
TOOL_PROVIDERS = {
    "issue_write": "github", "update_issue_state": "github", "merge_pull_request": "github",
    "save_issue": "linear",
    "transitionjiraissue": "jira",
    "update_tasks": "asana", "asana_update_task": "asana",
}


def server_of(event):
    """-> (provider, tool) for an MCP call; provider is None for anything we don't watch."""
    m = re.match(r"^mcp__(.+?)__(.+)$", event.get("tool_name") or "")
    if not m:
        return None, None
    tool = m.group(2).lower()
    server = (m.group(1) + " " + str((event.get("mcp_server") or {}).get("name") or "")).lower()
    for provider in ("github", "linear", "atlassian", "jira", "asana"):
        if provider in server:
            return ("jira" if provider == "atlassian" else provider), tool
    if tool == "update_issue":  # GitHub's older server and Linear's both have one; only GitHub's takes a repo
        args = event.get("tool_input") or {}
        return ("github" if args.get("owner") and args.get("repo") else "linear"), tool
    return TOOL_PROVIDERS.get(tool), tool


def gh_targets(event):
    args = event.get("tool_input") or {}
    if event.get("tool_name") == "Bash":
        return gh_targets_from_bash(args.get("command") or "", event.get("cwd"))
    provider, tool = server_of(event)
    if provider == "github":
        return gh_targets_from_mcp(tool, args)
    return []


# ---------------------------------------------------------------- hooks

def pending_path(event):
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", event.get("tool_use_id") or "")
    return PENDING / f"{key}.json" if key else None


def cleanup_pending():
    try:
        for p in PENDING.glob("*.json"):
            if time.time() - p.stat().st_mtime > PENDING_TTL_S:
                p.unlink()
    except OSError:
        pass


def current_branch(cwd):
    try:
        out = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd or None, capture_output=True,
                             text=True, timeout=GH_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    name = out.stdout.strip()
    return name if out.returncode == 0 and name and name != "HEAD" else None


def before_state(t, cwd):
    state = gh_view(t, cwd)
    if state:
        return {"target": dict(t, ref=state["url"], repo=None, cwd=None), "done": state["done"]}
    if t.get("new"):
        # `gh pr create ... && gh pr merge`: no PR yet, so it isn't merged. Find it by branch afterwards.
        ref = t.get("ref") or current_branch(cwd)
        if ref:
            return {"target": dict(t, ref=ref), "done": False}
    return None


def hook_pre(event):
    targets = gh_targets(event)
    path = pending_path(event)
    if not targets or not path:
        return
    # gh calls run side by side under one deadline, so a long chain can't hit the hook timeout.
    pool = ThreadPoolExecutor(max_workers=len(targets))
    futures = [pool.submit(before_state, t, t.get("cwd") or event.get("cwd")) for t in targets]
    wait(futures, timeout=PRE_DEADLINE_S)
    pool.shutdown(wait=False, cancel_futures=True)
    before = [f.result() for f in futures if f.done() and not f.cancelled() and f.exception() is None and f.result()]
    log("pre", event.get("tool_name"), before)
    if before:
        private_dir(PENDING)
        write_private(path, json.dumps(before))
    cleanup_pending()


def landed(before, cwd, name):
    """Whether any item that wasn't done before the call is done now."""
    for b in before:
        # The pre hook stored each item's URL, so a merged branch that `gh` no longer
        # resolves from the working copy still reads back the right PR.
        after = gh_view(b["target"], b["target"].get("cwd") or cwd)
        log("post", name, b, after)
        if after and after["done"] and not b["done"]:
            return True
    return False


def in_background(event):
    response = event.get("tool_response")
    return bool((event.get("tool_input") or {}).get("run_in_background") or
                (isinstance(response, dict) and response.get("backgroundTaskId")))


def hook_post(event):
    name = event.get("tool_name")
    failed = event.get("hook_event_name") == "PostToolUseFailure"
    path = pending_path(event)
    if path and path.exists():
        try:
            before = json.loads(path.read_text())
        finally:
            path.unlink()
        if in_background(event) and not failed:
            # The post hook fires as the command starts, and nothing fires when it ends, so a
            # detached watcher re-reads the items until one lands or WATCH_S runs out.
            if any(not b["done"] for b in before):
                watcher = subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "watch"],
                                           stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, start_new_session=True, text=True)
                watcher.stdin.write(json.dumps({"before": before, "cwd": event.get("cwd"), "name": name}))
                watcher.stdin.close()
                log("post", name, "background: watching", [b["target"]["ref"] for b in before])
            return
        # A Bash call that failed may still have landed the close (`gh issue close 12 && npm test`),
        # so it's re-read the same way.
        if landed(before, event.get("cwd"), name):
            play()
        return
    if failed:
        return  # Linear, Jira and Asana are judged from the result, and a failed call has none
    provider, tool = server_of(event)
    if provider in ("linear", "jira", "asana"):
        done = mcp_done(provider, tool, event)
        log("post", name, "done" if done else "not done")
        if done:
            play()


def watch(job):
    """Detached from the hook: for a background Bash call, re-read its items until one lands."""
    every = float(os.environ.get("GANGSTA_CLOSER_WATCH_EVERY_S") or WATCH_EVERY_S)
    deadline = time.time() + WATCH_S
    while time.time() < deadline:
        time.sleep(every)
        if landed(job["before"], job.get("cwd"), job.get("name")):
            play()
            return
    log("watch: gave up", [b["target"]["ref"] for b in job["before"]])


# ---------------------------------------------------------------- sound

def current_clip():
    for ext in CLIP_FORMATS:
        p = HOME / f"clip{ext}"
        if p.exists():
            return p
    return None


def fallback_sound():
    for p in ("/System/Library/Sounds/Glass.aiff", "/usr/share/sounds/freedesktop/stereo/complete.oga"):
        if Path(p).exists():
            return Path(p)
    return None


def player_for(path):
    override = os.environ.get("GANGSTA_CLOSER_PLAYER")
    if override:
        return shlex.split(override) + [str(path)]
    if sys.platform == "darwin" and shutil.which("afplay"):
        return ["afplay", str(path)]
    # paplay decodes through libsndfile: no M4A at all, and MP3 only from libsndfile 1.1.0.
    ext = path.suffix.lower()
    ffplay = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)] if shutil.which("ffplay") else None
    if ext in (".wav", ".ogg", ".oga", ".aiff") and shutil.which("paplay"):
        return ["paplay", str(path)]
    if ffplay:
        return ffplay
    if ext == ".mp3" and shutil.which("mpg123"):
        return ["mpg123", "-q", str(path)]
    if ext == ".mp3" and shutil.which("paplay"):
        return ["paplay", str(path)]
    if ext == ".wav" and shutil.which("aplay"):
        return ["aplay", "-q", str(path)]
    return None


def claim_cooldown():
    """Take the one play allowed per COOLDOWN_S. Hooks run in parallel, so check and stamp under a lock."""
    stamp = HOME / "last-played"
    private_dir(HOME)
    with open(HOME / "play.lock", "a") as lock:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if time.time() - stamp.stat().st_mtime < COOLDOWN_S:
                return False
        except OSError:
            pass
        stamp.touch()
        return True


def play(path=None, cooldown=True):
    """Start playback in the background and return at once. One play per COOLDOWN_S.
    Returns the file it played (the system chime when the clip can't be played here), or None."""
    clip = path or current_clip()
    cmd = player_for(clip) if clip else None
    if not cmd:  # no clip, or nothing here can play it: the system chime instead
        clip = fallback_sound()
        cmd = player_for(clip) if clip else None
    if not cmd:
        log("no clip or no player")
        return None
    try:
        if cooldown and not claim_cooldown():
            log("cooldown, skipped")
            return None
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as e:
        log("play failed:", e)
        return None
    log("played", clip)
    return clip


def playable(path):
    """Whether this machine can decode the file: a probe, where one is installed."""
    if os.environ.get("GANGSTA_CLOSER_PLAYER"):
        return True
    if not player_for(path):
        return False
    probe = ["afinfo", str(path)] if sys.platform == "darwin" else \
        ["ffprobe", "-v", "error", str(path)] if shutil.which("ffprobe") else None
    if not probe or not shutil.which(probe[0]):
        return True
    try:
        return subprocess.run(probe, capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return True


# ---------------------------------------------------------------- /gangsta-closer:clip

def clean_path_arg(raw):
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1]
    s = re.sub(r"\\(.)", r"\1", s)  # paths dragged into the terminal arrive as My\ Music/x.mp3
    return Path(os.path.expanduser(s))


NO_SOUND = "Nothing on this machine can play it, and no system chime was found."


def cmd_clip(raw):
    """Always exits 0: a non-zero exit aborts the slash command, and the user never sees why."""
    raw = (raw or "").strip()
    clip = current_clip()
    if not raw or raw == "test":
        if clip:
            played = play(clip, cooldown=False)
            print(f"Your clip: {clip}. Playing it now." if played == clip else
                  f"Your clip: {clip}. Nothing on this machine can play it, so the system chime played instead."
                  if played else f"Your clip: {clip}. {NO_SOUND}")
        elif play(cooldown=False):
            print("No clip set; a system chime plays. Set one with /gangsta-closer:clip <path to MP3, WAV or M4A>.")
        else:
            print(f"No clip set. {NO_SOUND} Set one with /gangsta-closer:clip <path to MP3, WAV or M4A>.")
        return 0
    if raw == "reset":
        if clip:
            clip.unlink()
        print("Clip removed; a system chime plays from now on." if fallback_sound() else
              "Clip removed. No system chime was found, so closes are silent until you set a clip.")
        return 0
    src = clean_path_arg(raw)
    if not src.is_file():
        print(f"Not set: no file at {src}.")
        return 0
    ext = src.suffix.lower()
    if ext not in CLIP_FORMATS:
        names = ", ".join(e[1:].upper() for e in CLIP_FORMATS)
        extra = " macOS can't play OGG; convert it to MP3 or M4A." if ext == ".ogg" else ""
        print(f"Not set: {src.name} isn't {names}.{extra}")
        return 0
    if src.stat().st_size > MAX_CLIP_BYTES:
        print(f"Not set: {src.name} is {src.stat().st_size / 1048576:.1f} MB; the limit is 5 MB. A few seconds works best.")
        return 0
    dest = HOME / f"clip{ext}"
    if clip and clip.exists() and src.resolve() == clip.resolve():
        print(f"That's already your clip ({clip}). Playing it now.")
        play(clip, cooldown=False)
        return 0
    if not player_for(src):
        print(f"Not set: nothing on this machine can play {ext[1:].upper()} files. Install ffmpeg, or use a WAV.")
        return 0
    if not playable(src):
        print(f"Not set: {src.name} couldn't be read as audio. It may be damaged; try another file.")
        return 0
    # Copy beside the old clip, then swap it in, so a failed copy keeps the old one.
    private_dir(HOME)
    tmp = HOME / f".clip-new{ext}"
    try:
        shutil.copyfile(src, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        print(f"Not set: couldn't copy {src.name} ({e.strerror or e}). Your previous clip is unchanged.")
        return 0
    for other in CLIP_FORMATS:
        if other != ext:
            (HOME / f"clip{other}").unlink(missing_ok=True)
    print(f"Clip set: {src.name}, copied to {dest}. Playing it now.")
    play(dest, cooldown=False)
    return 0


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "clip":
        try:
            return cmd_clip(" ".join(sys.argv[2:]))
        except Exception as e:
            print(f"Something went wrong: {e!r}. Your clip is unchanged.")
            return 0
    try:
        event = json.load(sys.stdin)
        if mode == "pre":
            hook_pre(event)
        elif mode == "post":
            hook_post(event)
        elif mode == "watch":
            watch(event)
    except Exception as e:  # a sound effect must never break a tool call
        log("error:", repr(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
