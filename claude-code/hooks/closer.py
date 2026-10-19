#!/usr/bin/env python3
"""Gangsta Closer for Claude Code: play your clip when a tool call closes something.

  closer.py pre     PreToolUse hook: record the state of anything the call is about to close
  closer.py post    PostToolUse hook: re-read it; play only on a not-done -> done change
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
from pathlib import Path

HOME = Path(os.environ.get("GANGSTA_CLOSER_HOME") or Path.home() / ".gangsta-closer")
PENDING = HOME / "pending"
CLIP_FORMATS = (".mp3", ".wav", ".m4a") if sys.platform == "darwin" else (".mp3", ".wav", ".m4a", ".ogg")
MAX_CLIP_BYTES = 5 * 1024 * 1024
COOLDOWN_S = 3
PENDING_TTL_S = 600
GH_TIMEOUT_S = 6


def log(*parts):
    if os.environ.get("GANGSTA_CLOSER_DEBUG") != "1":
        return
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        with open(HOME / "debug.log", "a") as f:
            f.write(time.strftime("%H:%M:%S ") + " ".join(str(p) for p in parts) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- GitHub via gh

GH_API_PATH = re.compile(r"^/?repos/([^/]+)/([^/]+)/(issues|pulls)/(\d+)(/merge)?/?$")


def gh_view(target, cwd):
    """target is {"kind": "issue"|"pr", "ref": number-or-url, "repo": "o/r" or None}.
    Returns {"url", "done"} or None when gh can't read it."""
    fields = "url,state,stateReason" if target["kind"] == "issue" else "url,state"
    cmd = ["gh", target["kind"], "view"] + ([target["ref"]] if target["ref"] else [])
    if target.get("repo"):
        cmd += ["-R", target["repo"]]
    cmd += ["--json", fields]
    try:
        out = subprocess.run(cmd, cwd=cwd or None, capture_output=True, text=True, timeout=GH_TIMEOUT_S)
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


def split_commands(command):
    """Split a shell command into simple-command token lists (on && || ; | and newlines)."""
    try:
        lexer = shlex.shlex(command.replace("\\\n", " "), posix=True, punctuation_chars=";&|\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return []
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
                   "-t", "--subject", "--author-email", "--match-head-commit", "--duplicate-of"}
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
    for words in split_commands(command):
        if words[0] == "cd" and len(words) == 2:
            cwd = os.path.join(cwd or os.getcwd(), os.path.expanduser(words[1]))
            continue
        # Skip env assignments and wrappers: `GH_REPO=x gh ...`, `env gh ...`, `/opt/homebrew/bin/gh ...`
        i = 0
        while i < len(words) and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[i]) or words[i] in ("env", "command", "sudo")):
            i += 1
        if i >= len(words) or os.path.basename(words[i]) != "gh":
            continue
        args = words[i + 1:]
        repo = flag_value(args, "-R", "--repo")
        if args[:2] == ["issue", "close"]:
            for ref in positionals(args[2:])[:1]:
                targets.append({"kind": "issue", "ref": ref, "repo": repo, "cwd": cwd})
        elif args[:2] == ["pr", "merge"]:
            refs = positionals(args[2:])[:1]
            targets.append({"kind": "pr", "ref": refs[0] if refs else None, "repo": repo, "cwd": cwd})
        elif args[:1] == ["api"]:
            for p in positionals(args[1:]):
                m = GH_API_PATH.match(p)
                if not m:
                    continue
                owner, name, kind, num, merge = m.groups()
                if kind == "pulls" and merge:
                    targets.append({"kind": "pr", "ref": num, "repo": f"{owner}/{name}"})
                elif kind == "issues":
                    targets.append({"kind": "issue", "ref": num, "repo": f"{owner}/{name}"})
    return targets


def gh_targets_from_mcp(tool, args):
    """GitHub MCP server (github/github-mcp-server): issue updates and PR merges."""
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


def linear_done(args, response, transcript):
    """save_issue on an existing issue (`id`) that moves it to a state of type "completed"."""
    if not args.get("id"):
        return False  # creating an issue isn't closing one
    want = next((args[k] for k in ("state", "stateId", "status", "statusId") if args.get(k)), None)
    if not want:
        return False
    want = str(want)
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
        for d in transcript:
            to = d.get("to")
            if str(d.get("id")) == str(tid) and isinstance(to, dict) and isinstance(to.get("statusCategory"), dict):
                # Teams file Won't Do and Duplicate under Done too; like the extension, those stay silent.
                return to["statusCategory"].get("key") == "done" and \
                    not NOT_A_WIN.search(str(to.get("name") or d.get("name") or ""))
    if landed:
        return bool(DONE_NAME.search(landed)) and not NOT_A_WIN.search(landed)
    return False


def asana_done(args, response):
    """update_tasks that sets completed: true on a task the result lists as updated. update_tasks
    reports per task: {"data": [{"gid": ...}], "failed": [...]}, and a failed update is still a
    normal result, so the input alone isn't enough."""
    wanted = {str(d.get("task") or d.get("task_gid") or d.get("gid")) for d in walk(args)
              if d.get("completed") in (True, "true")}
    if not wanted:
        return False
    updated = {str(item.get("gid")) for d in response_dicts(response) if isinstance(d.get("data"), list)
               for item in d["data"] if isinstance(item, dict)}
    return bool(wanted & updated)


def mcp_done(provider, tool, event):
    args = event.get("tool_input") or {}
    response = event.get("tool_response")
    if provider == "linear" and tool in ("save_issue", "update_issue"):
        return linear_done(args, response, transcript_dicts(event.get("transcript_path")))
    if provider == "jira" and tool == "transitionjiraissue":
        return jira_done(args, response, transcript_dicts(event.get("transcript_path")))
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


def hook_pre(event):
    targets = gh_targets(event)
    path = pending_path(event)
    if not targets or not path:
        return
    before = []
    for t in targets:
        state = gh_view(t, t.get("cwd") or event.get("cwd"))
        if state:
            before.append({"target": dict(t, ref=state["url"], repo=None), "done": state["done"]})
    log("pre", event.get("tool_name"), before)
    if before:
        PENDING.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(before))
    cleanup_pending()


def hook_post(event):
    name = event.get("tool_name")
    path = pending_path(event)
    if path and path.exists():
        try:
            before = json.loads(path.read_text())
        finally:
            path.unlink()
        for b in before:
            # The pre hook stored each item's URL, so a merged branch that `gh` no longer
            # resolves from the working copy still reads back the right PR.
            after = gh_view(b["target"], event.get("cwd"))
            log("post", name, b, after)
            if after and after["done"] and not b["done"]:
                return play()
        return
    provider, tool = server_of(event)
    if provider in ("linear", "jira", "asana"):
        done = mcp_done(provider, tool, event)
        log("post", name, "done" if done else "not done")
        if done:
            play()


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
    if shutil.which("paplay"):
        return ["paplay", str(path)]
    if shutil.which("ffplay"):
        return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)]
    if path.suffix == ".wav" and shutil.which("aplay"):
        return ["aplay", "-q", str(path)]
    return None


def play(path=None, cooldown=True):
    """Start playback in the background and return at once. One play per COOLDOWN_S."""
    stamp = HOME / "last-played"
    if cooldown:
        try:
            if time.time() - stamp.stat().st_mtime < COOLDOWN_S:
                log("cooldown, skipped")
                return False
        except OSError:
            pass
    path = path or current_clip() or fallback_sound()
    cmd = player_for(path) if path else None
    if not cmd:
        log("no clip or no player")
        return False
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        HOME.mkdir(parents=True, exist_ok=True)
        stamp.touch()
    except OSError as e:
        log("play failed:", e)
        return False
    log("played", path)
    return True


# ---------------------------------------------------------------- /gangsta-closer:clip

def clean_path_arg(raw):
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1]
    s = re.sub(r"\\(.)", r"\1", s)  # paths dragged into the terminal arrive as My\ Music/x.mp3
    return Path(os.path.expanduser(s))


def cmd_clip(raw):
    raw = (raw or "").strip()
    clip = current_clip()
    if not raw or raw == "test":
        if clip:
            print(f"Your clip: {clip}. Playing it now.")
            play(clip, cooldown=False)
        else:
            print("No clip set; a system chime plays. Set one with /gangsta-closer:clip <path to MP3, WAV or M4A>.")
            play(cooldown=False)
        return 0
    if raw == "reset":
        if clip:
            clip.unlink()
        print("Clip removed; a system chime plays from now on.")
        return 0
    src = clean_path_arg(raw)
    if not src.is_file():
        print(f"Not set: no file at {src}.")
        return 1
    if src.suffix.lower() not in CLIP_FORMATS:
        names = ", ".join(e[1:].upper() for e in CLIP_FORMATS)
        extra = " macOS can't play OGG; convert it to MP3 or M4A." if src.suffix.lower() == ".ogg" else ""
        print(f"Not set: {src.name} isn't {names}.{extra}")
        return 1
    if src.stat().st_size > MAX_CLIP_BYTES:
        print(f"Not set: {src.name} is {src.stat().st_size / 1048576:.1f} MB; the limit is 5 MB. A few seconds works best.")
        return 1
    HOME.mkdir(parents=True, exist_ok=True)
    for ext in CLIP_FORMATS:
        (HOME / f"clip{ext}").unlink(missing_ok=True)
    dest = HOME / f"clip{src.suffix.lower()}"
    shutil.copyfile(src, dest)
    print(f"Clip set: {src.name}, copied to {dest}. Playing it now.")
    play(dest, cooldown=False)
    return 0


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "clip":
        return cmd_clip(" ".join(sys.argv[2:]))
    try:
        event = json.load(sys.stdin)
        if mode == "pre":
            hook_pre(event)
        elif mode == "post":
            hook_post(event)
    except Exception as e:  # a sound effect must never break a tool call
        log("error:", repr(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
