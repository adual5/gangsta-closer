"""Tests for the Claude Code hook. Runs the real hook script against a fake `gh` and a fake player.

  python3 claude-code/tests/test_closer.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "closer.py"

FAKE_GH = r'''#!/usr/bin/env python3
# Fake gh: `gh issue|pr view [ref] [-R repo] --json f` answers from $FAKE_GH_STATE.
import json, os, re, sys
args = sys.argv[1:]
if os.environ.get("GH_CALLS"):
    with open(os.environ["GH_CALLS"], "a") as f:
        f.write(json.dumps({"args": args, "GH_HOST": os.environ.get("GH_HOST"),
                            "token": bool(os.environ.get("GH_ENTERPRISE_TOKEN"))}) + "\n")
state = json.load(open(os.environ["FAKE_GH_STATE"]))
kind = args[0]
if "-R" in args and not re.match(r"^[\w.-]+/[\w.-]+$", args[args.index("-R") + 1]):
    sys.stderr.write("invalid repo\n"); sys.exit(1)
rest, skip = [], False
for a in args[2:]:
    if skip:
        skip = False
    elif a in ("-R", "--json"):
        skip = True
    else:
        rest.append(a)
ref = rest[0] if rest else ""
if not ref and os.environ.get("FAKE_GH_CWD") and os.path.realpath(os.getcwd()) != os.path.realpath(os.environ["FAKE_GH_CWD"]):
    sys.stderr.write("no pull requests found for branch\n"); sys.exit(1)
items = state.get(kind, {})
item = items.get(ref) or next((v for v in items.values() if v["url"] == ref), None)
if item is None:
    sys.stderr.write("not found\n"); sys.exit(1)
print(json.dumps(item))
'''

FAKE_PLAYER = r'''#!/bin/sh
echo "$1" >> "$PLAYED_LOG"
'''


def issue(n, state="OPEN", reason=""):
    return {"url": f"https://github.com/o/r/issues/{n}", "state": state, "stateReason": reason}


def pr(n, state="OPEN"):
    return {"url": f"https://github.com/o/r/pull/{n}", "state": state}


class Hook(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text(FAKE_GH)
        (bin_dir / "gh").chmod(0o755)
        (self.tmp / "player").write_text(FAKE_PLAYER)
        (self.tmp / "player").chmod(0o755)
        self.state_file = self.tmp / "gh-state.json"
        self.played_log = self.tmp / "played.log"
        self.env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", FAKE_GH_STATE=str(self.state_file),
                        GANGSTA_CLOSER_HOME=str(self.tmp / "home"), GANGSTA_CLOSER_PLAYER=str(self.tmp / "player"),
                        PLAYED_LOG=str(self.played_log), GANGSTA_CLOSER_DEBUG="1",
                        GH_CALLS=str(self.tmp / "gh-calls.jsonl"))
        self.n = 0

    def gh_state(self, issues=(), prs=(), current_pr=None, branches=None):
        state = {"issue": {}, "pr": {}}
        for i in issues:
            state["issue"][i["url"].rsplit("/", 1)[1]] = i
        for p in prs:
            state["pr"][p["url"].rsplit("/", 1)[1]] = p
        if current_pr:
            state["pr"][""] = current_pr
        for name, p in (branches or {}).items():
            state["pr"][name] = p
        self.state_file.write_text(json.dumps(state))

    def gh_calls(self):
        path = self.tmp / "gh-calls.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def run_hook(self, mode, event):
        subprocess.run([sys.executable, str(HOOK), mode], input=json.dumps(event), text=True, env=self.env,
                       check=True, timeout=30)

    def plays(self):
        time.sleep(0.4)
        return self.played_log.read_text().count("\n") if self.played_log.exists() else 0

    def call(self, tool_name, tool_input, before, after, tool_response=None, post_event="PostToolUse"):
        """Simulate one tool call: gh state `before`, pre hook, state becomes `after`, post hook."""
        self.n += 1
        event = {"tool_name": tool_name, "tool_input": tool_input, "tool_use_id": f"toolu_{self.n}",
                 "cwd": str(self.tmp), "hook_event_name": "PreToolUse"}
        self.gh_state(**before)
        self.run_hook("pre", event)
        self.gh_state(**after)
        post = dict(event, hook_event_name=post_event)
        if post_event == "PostToolUse":
            post["tool_response"] = tool_response or {}
        else:
            post["error"] = "Exit code 1"
        self.run_hook("post", post)
        return self.plays()

    def bash(self, command, before, after, **kw):
        return self.call("Bash", {"command": command}, before, after, **kw)

    # ---- GitHub through gh

    def test_issue_close_completed_plays(self):
        self.assertEqual(self.bash("gh issue close 12", {"issues": [issue(12)]},
                                   {"issues": [issue(12, "CLOSED", "COMPLETED")]}), 1)

    def background(self, before, later):
        """run_in_background: the post hook fires as the command starts (seen live), then the close lands."""
        self.env["GANGSTA_CLOSER_WATCH_EVERY_S"] = "0.5"
        self.n += 1
        event = {"tool_name": "Bash", "tool_input": {"command": "sleep 8 && gh issue close 12", "run_in_background": True},
                 "tool_use_id": f"toolu_{self.n}", "cwd": str(self.tmp), "hook_event_name": "PreToolUse"}
        self.gh_state(**before)
        self.run_hook("pre", event)
        self.run_hook("post", dict(event, hook_event_name="PostToolUse",
                                   tool_response={"stdout": "", "backgroundTaskId": "b1"}))
        at_start = self.plays()
        self.gh_state(**later)
        time.sleep(2)
        return at_start, self.plays()

    def test_background_close_plays_when_it_lands(self):
        self.assertEqual(self.background({"issues": [issue(12)]}, {"issues": [issue(12, "CLOSED", "COMPLETED")]}), (0, 1))

    def test_background_close_that_never_lands_silent(self):
        self.assertEqual(self.background({"issues": [issue(12)]}, {"issues": [issue(12)]}), (0, 0))

    def test_background_on_already_closed_issue_silent(self):
        closed = {"issues": [issue(12, "CLOSED", "COMPLETED")]}
        self.assertEqual(self.background(closed, closed), (0, 0))

    def test_issue_close_not_planned_silent(self):
        self.assertEqual(self.bash('gh issue close 12 --reason "not planned"', {"issues": [issue(12)]},
                                   {"issues": [issue(12, "CLOSED", "NOT_PLANNED")]}), 0)

    def test_issue_close_duplicate_silent(self):
        self.assertEqual(self.bash("gh issue close 12 --duplicate-of 3", {"issues": [issue(12)]},
                                   {"issues": [issue(12, "CLOSED", "DUPLICATE")]}), 0)

    def test_issue_already_closed_silent(self):
        closed = {"issues": [issue(12, "CLOSED", "COMPLETED")]}
        self.assertEqual(self.bash("gh issue close 12", closed, closed), 0)

    def test_issue_close_that_did_not_land_silent(self):
        self.assertEqual(self.bash("gh issue close 12", {"issues": [issue(12)]}, {"issues": [issue(12)]}), 0)

    def test_issue_close_by_url_with_repo_and_chain(self):
        cmd = 'cd /tmp && gh issue close https://github.com/o/r/issues/12 -c "fixed in #40" && echo done'
        self.assertEqual(self.bash(cmd, {"issues": [issue(12)]}, {"issues": [issue(12, "CLOSED", "COMPLETED")]}), 1)

    def test_pr_merge_plays(self):
        self.assertEqual(self.bash("gh pr merge 7 --squash -R o/r", {"prs": [pr(7)]}, {"prs": [pr(7, "MERGED")]}), 1)

    def test_pr_merge_current_branch_plays_after_branch_deleted(self):
        # `gh pr merge --delete-branch` with no number: after the merge the branch is gone, so the
        # PR is only findable by the URL the pre hook recorded.
        self.assertEqual(self.bash("gh pr merge --squash --delete-branch", {"current_pr": pr(7)},
                                   {"prs": [pr(7, "MERGED")]}), 1)

    def test_cd_then_merge_current_branch_reads_that_repo(self):
        # The fake gh only knows the current-branch PR when run inside repo/.
        (self.tmp / "repo").mkdir()
        self.env["FAKE_GH_CWD"] = str(self.tmp / "repo")
        self.assertEqual(self.bash("cd repo && gh pr merge --squash --delete-branch", {"current_pr": pr(7)},
                                   {"prs": [pr(7, "MERGED")]}), 1)

    def test_pr_auto_merge_still_open_silent(self):
        self.assertEqual(self.bash("gh pr merge 7 --auto --squash", {"prs": [pr(7)]}, {"prs": [pr(7)]}), 0)

    def test_pr_closed_without_merge_silent(self):
        self.assertEqual(self.bash("gh pr close 7", {"prs": [pr(7)]}, {"prs": [pr(7, "CLOSED")]}), 0)

    def test_gh_api_issue_close_plays(self):
        cmd = "gh api -X PATCH repos/o/r/issues/12 -f state=closed -f state_reason=completed"
        self.assertEqual(self.bash(cmd, {"issues": [issue(12)]}, {"issues": [issue(12, "CLOSED", "COMPLETED")]}), 1)

    def test_gh_api_pr_merge_plays(self):
        self.assertEqual(self.bash("gh api -X PUT repos/o/r/pulls/7/merge", {"prs": [pr(7)]},
                                   {"prs": [pr(7, "MERGED")]}), 1)

    def test_unrelated_bash_silent(self):
        self.assertEqual(self.bash("gh issue list && git status", {"issues": [issue(12)]},
                                   {"issues": [issue(12, "CLOSED", "COMPLETED")]}), 0)

    def test_quoted_gh_text_is_not_a_command(self):
        self.assertEqual(self.bash('echo "gh issue close 12"', {"issues": [issue(12)]},
                                   {"issues": [issue(12, "CLOSED", "COMPLETED")]}), 0)

    def test_failed_tool_call_leaves_no_sound(self):
        # A call's pending file belongs to that call only; the next unrelated call must not play.
        self.gh_state(issues=[issue(12)])
        self.run_hook("pre", {"tool_name": "Bash", "tool_input": {"command": "gh issue close 12"},
                              "tool_use_id": "toolu_failed", "cwd": str(self.tmp)})
        self.gh_state(issues=[issue(12, "CLOSED", "COMPLETED")])
        self.run_hook("post", {"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_use_id": "toolu_next",
                               "cwd": str(self.tmp), "tool_response": {}})
        self.assertEqual(self.plays(), 0)

    def test_cooldown_one_play_for_a_burst(self):
        before, after = {"issues": [issue(1), issue(2)]}, {"issues": [issue(1, "CLOSED", "COMPLETED"),
                                                                      issue(2, "CLOSED", "COMPLETED")]}
        self.bash("gh issue close 1", before, {"issues": [issue(1, "CLOSED", "COMPLETED"), issue(2)]})
        self.assertEqual(self.bash("gh issue close 2", {"issues": [issue(1, "CLOSED", "COMPLETED"), issue(2)]},
                                   after), 1)


    # ---- failed calls, odd command shapes

    def test_close_in_a_bash_call_that_failed_still_plays(self):
        # `gh issue close 12 && npm test` with failing tests: the close landed, the call exits 1.
        self.assertEqual(self.bash("gh issue close 12 && npm test", {"issues": [issue(12)]},
                                   {"issues": [issue(12, "CLOSED", "COMPLETED")]}, post_event="PostToolUseFailure"), 1)

    def test_failed_bash_call_whose_close_did_not_land_silent(self):
        self.assertEqual(self.bash("gh issue close 12", {"issues": [issue(12)]}, {"issues": [issue(12)]},
                                   post_event="PostToolUseFailure"), 0)

    def test_comment_line_does_not_hide_the_next_command(self):
        (self.tmp / "repo").mkdir()
        self.env["FAKE_GH_CWD"] = str(self.tmp / "repo")
        self.assertEqual(self.bash("cd repo  # go there\ngh pr merge --squash", {"current_pr": pr(7)},
                                   {"prs": [pr(7, "MERGED")]}), 1)

    def test_apostrophe_in_a_heredoc_does_not_hide_the_close(self):
        cmd = "cat <<'EOF' > notes.txt\nit's done\nEOF\ngh issue close 12"
        self.assertEqual(self.bash(cmd, {"issues": [issue(12)]}, {"issues": [issue(12, "CLOSED", "COMPLETED")]}), 1)

    def test_unbalanced_quote_on_another_line_does_not_hide_the_close(self):
        cmd = "echo it's\ngh issue close 12"
        self.assertEqual(self.bash(cmd, {"issues": [issue(12)]}, {"issues": [issue(12, "CLOSED", "COMPLETED")]}), 1)

    def test_quoted_hash_is_not_a_comment(self):
        self.assertEqual(self.bash('git commit -m "#12 fix login" && gh issue close 12', {"issues": [issue(12)]},
                                   {"issues": [issue(12, "CLOSED", "COMPLETED")]}), 1)

    def test_markdown_pr_body_then_merge_plays(self):
        self.assertEqual(self.bash('gh pr create --title x --body "## Summary" && gh pr merge 7 --squash',
                                   {"prs": [pr(7)]}, {"prs": [pr(7, "MERGED")]}), 1)

    def test_shift_operator_is_not_a_heredoc(self):
        self.assertEqual(self.bash('python3 -c "print(1<<n)"\ngh issue close 12', {"issues": [issue(12)]},
                                   {"issues": [issue(12, "CLOSED", "COMPLETED")]}), 1)

    def test_pr_url_with_comment_fragment_plays(self):
        self.assertEqual(self.bash("gh pr merge https://github.com/o/r/pull/7#issuecomment-1 --squash",
                                   {"prs": [pr(7)]}, {"prs": [pr(7, "MERGED")]}), 1)

    def test_repo_given_with_host_plays(self):
        self.assertEqual(self.bash("gh pr merge 7 -R https://github.com/o/r", {"prs": [pr(7)]},
                                   {"prs": [pr(7, "MERGED")]}), 1)

    def test_gh_repo_env_prefix_is_used(self):
        self.bash("GH_REPO=o/r gh issue close 12", {"issues": [issue(12)]}, {"issues": [issue(12, "CLOSED", "COMPLETED")]})
        self.assertIn(["issue", "view", "12", "-R", "o/r", "--json", "url,state,stateReason"],
                      [c["args"] for c in self.gh_calls()])

    def test_author_email_short_flag_is_not_the_pr(self):
        self.assertEqual(self.bash("gh pr merge -A me@example.com 7 --squash", {"prs": [pr(7)]},
                                   {"prs": [pr(7, "MERGED")]}), 1)

    def test_checkout_then_merge_reads_that_branch_pr(self):
        # The working copy is still on another branch's PR (9) when the pre hook runs.
        self.assertEqual(self.bash("git checkout feat && gh pr merge --squash",
                                   {"current_pr": pr(9), "branches": {"feat": pr(7)}, "prs": [pr(7)]},
                                   {"current_pr": pr(9), "prs": [pr(7, "MERGED")]}), 1)

    def test_create_then_merge_plays(self):
        self.assertEqual(self.bash("git switch -c feat && gh pr create --fill && gh pr merge --squash", {},
                                   {"branches": {"feat": pr(7, "MERGED")}}), 1)

    def test_gh_api_placeholders_read_the_working_copy(self):
        cmd = "gh api -X PATCH repos/{owner}/{repo}/issues/12 -f state=closed"
        self.assertEqual(self.bash(cmd, {"issues": [issue(12)]}, {"issues": [issue(12, "CLOSED", "COMPLETED")]}), 1)

    def test_other_hosts_are_never_contacted(self):
        self.env["GH_ENTERPRISE_TOKEN"] = "secret"
        for cmd in ("gh issue close 1 -R attacker.example/o/r", "gh issue close https://attacker.example/o/r/issues/1",
                    "GH_HOST=attacker.example gh issue close 1"):
            self.bash(cmd, {"issues": [issue(1)]}, {"issues": [issue(1, "CLOSED", "COMPLETED")]})
        self.bash("gh issue close 12", {"issues": [issue(12)]}, {"issues": [issue(12, "CLOSED", "COMPLETED")]})
        calls = self.gh_calls()
        self.assertTrue(calls)
        self.assertFalse([c for c in calls if "attacker" in " ".join(c["args"])])
        self.assertTrue(all(c["GH_HOST"] == "github.com" and not c["token"] for c in calls))

    def test_pending_file_is_private(self):
        self.gh_state(issues=[issue(12)])
        self.run_hook("pre", {"tool_name": "Bash", "tool_input": {"command": "gh issue close 12"},
                              "tool_use_id": "toolu_private", "cwd": str(self.tmp)})
        pending = self.tmp / "home" / "pending"
        self.assertEqual(pending.stat().st_mode & 0o777, 0o700)
        self.assertEqual((pending / "toolu_private.json").stat().st_mode & 0o777, 0o600)

    def test_parallel_closes_play_once(self):
        self.gh_state(issues=[issue(1), issue(2)])
        events = [{"tool_name": "Bash", "tool_input": {"command": f"gh issue close {n}"}, "tool_use_id": f"toolu_p{n}",
                   "cwd": str(self.tmp)} for n in (1, 2)]
        for e in events:
            self.run_hook("pre", e)
        self.gh_state(issues=[issue(1, "CLOSED", "COMPLETED"), issue(2, "CLOSED", "COMPLETED")])
        procs = [subprocess.Popen([sys.executable, str(HOOK), "post"], stdin=subprocess.PIPE, text=True, env=self.env)
                 for _ in events]
        for proc, e in zip(procs, events):
            proc.communicate(json.dumps(dict(e, hook_event_name="PostToolUse", tool_response={})), timeout=30)
        self.assertEqual(self.plays(), 1)

    # ---- GitHub MCP server

    def test_github_mcp_issue_close_plays(self):
        args = {"method": "update", "owner": "o", "repo": "r", "issue_number": 12, "state": "closed",
                "state_reason": "completed"}
        self.assertEqual(self.call("mcp__github__issue_write", args, {"issues": [issue(12)]},
                                   {"issues": [issue(12, "CLOSED", "COMPLETED")]}), 1)

    def test_github_mcp_not_planned_silent(self):
        args = {"method": "update", "owner": "o", "repo": "r", "issue_number": 12, "state": "closed",
                "state_reason": "not_planned"}
        self.assertEqual(self.call("mcp__plugin_engineering_github__issue_write", args, {"issues": [issue(12)]},
                                   {"issues": [issue(12, "CLOSED", "NOT_PLANNED")]}), 0)

    def test_github_mcp_merge_plays(self):
        args = {"owner": "o", "repo": "r", "pullNumber": 7, "merge_method": "squash"}
        self.assertEqual(self.call("mcp__github__merge_pull_request", args, {"prs": [pr(7)]},
                                   {"prs": [pr(7, "MERGED")]}), 1)


    def test_older_github_mcp_update_issue_close_plays(self):
        args = {"owner": "o", "repo": "r", "issue_number": 12, "state": "closed"}
        self.assertEqual(self.call("mcp__9a1b__update_issue", args, {"issues": [issue(12)]},
                                   {"issues": [issue(12, "CLOSED", "COMPLETED")]}), 1)


class McpServices(unittest.TestCase):
    """Linear, Jira and Asana: decided from the MCP call's input, result and earlier listings."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "player").write_text(FAKE_PLAYER)
        (self.tmp / "player").chmod(0o755)
        self.played_log = self.tmp / "played.log"
        self.transcript = self.tmp / "transcript.jsonl"
        self.transcript.write_text("")
        self.env = dict(os.environ, GANGSTA_CLOSER_HOME=str(self.tmp / "home"),
                        GANGSTA_CLOSER_PLAYER=str(self.tmp / "player"), PLAYED_LOG=str(self.played_log))

    def earlier_result(self, payload):
        """Append a tool result to the transcript the way Claude Code stores one: JSON text in a content block."""
        entry = {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_x", "content": [{"type": "text", "text": json.dumps(payload)}]}]}}
        with open(self.transcript, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def post(self, tool_name, tool_input, tool_response="", mcp_server=None, hook_event_name="PostToolUse", **extra):
        event = {"tool_name": tool_name, "tool_input": tool_input, "tool_response": tool_response,
                 "tool_use_id": "toolu_1", "transcript_path": str(self.transcript), "cwd": str(self.tmp),
                 "hook_event_name": hook_event_name, **extra}
        if mcp_server:
            event["mcp_server"] = mcp_server
        subprocess.run([sys.executable, str(HOOK), "post"], input=json.dumps(event), text=True, env=self.env,
                       check=True, timeout=30)
        time.sleep(0.4)
        return self.played_log.read_text().count("\n") if self.played_log.exists() else 0

    LINEAR_STATES = {"states": [
        {"id": "s-todo", "name": "Todo", "type": "unstarted"},
        {"id": "s-ship", "name": "Shipped", "type": "completed"},
        {"id": "s-wont", "name": "Won't Do", "type": "canceled"},
        {"id": "s-qa", "name": "Done-ish QA", "type": "started"}]}

    # ---- Linear

    @staticmethod
    def linear_result(status, status_type):
        # save_issue's real result shape (trimmed), captured live on 2026-10-19.
        return [{"type": "text", "text": json.dumps({"id": "AMD-5", "uuid": "u", "title": "t", "status": status,
                                                     "statusType": status_type, "completedAt": None})}]

    def test_linear_result_status_type_completed_plays(self):
        self.assertEqual(self.post("mcp__linear__save_issue", {"id": "AMD-5", "state": "a7e4"},
                                   self.linear_result("Done", "completed")), 1)

    def test_linear_result_status_type_canceled_silent(self):
        self.assertEqual(self.post("mcp__linear__save_issue", {"id": "AMD-5", "state": "Canceled"},
                                   self.linear_result("Canceled", "canceled")), 0)

    def test_linear_state_given_as_type_plays(self):
        self.assertEqual(self.post("mcp__linear__save_issue", {"id": "AMD-5", "state": "completed"}), 1)

    def test_linear_state_given_as_duplicate_type_silent(self):
        self.assertEqual(self.post("mcp__linear__save_issue", {"id": "AMD-5", "state": "duplicate"}), 0)

    def test_transcript_entries_with_list_types_dont_break_lookup(self):
        # Real transcripts hold entries whose "type" is a list; the lookup crashed on them and went silent.
        self.earlier_result(self.LINEAR_STATES)
        self.earlier_result({"type": ["a", "b"], "id": "x"})
        self.assertEqual(self.post("mcp__linear__save_issue", {"id": "ENG-1", "state": "Shipped"}), 1)

    def test_linear_state_id_of_completed_type_plays(self):
        self.earlier_result(self.LINEAR_STATES)
        self.assertEqual(self.post("mcp__linear__save_issue", {"id": "ENG-1", "state": "s-ship"}), 1)

    def test_linear_state_name_of_completed_type_plays_even_if_renamed(self):
        self.earlier_result(self.LINEAR_STATES)
        self.assertEqual(self.post("mcp__plugin_engineering_linear__save_issue", {"id": "ENG-1", "state": "Shipped"}), 1)

    def test_linear_canceled_type_silent(self):
        self.earlier_result(self.LINEAR_STATES)
        self.assertEqual(self.post("mcp__linear__save_issue", {"id": "ENG-1", "state": "s-wont"}), 0)

    def test_linear_type_beats_a_done_looking_name(self):
        self.earlier_result(self.LINEAR_STATES)
        self.assertEqual(self.post("mcp__linear__save_issue", {"id": "ENG-1", "state": "Done-ish QA"}), 0)

    def test_linear_result_state_type_wins(self):
        result = [{"type": "text", "text": json.dumps({"id": "ENG-1", "state": {"name": "Done", "type": "canceled"}})}]
        self.assertEqual(self.post("mcp__linear__save_issue", {"id": "ENG-1", "state": "Done"}, result), 0)

    def test_linear_name_fallback_done_plays(self):
        self.assertEqual(self.post("mcp__linear__save_issue", {"id": "ENG-1", "state": "Done"}), 1)

    def test_linear_name_fallback_canceled_silent(self):
        self.assertEqual(self.post("mcp__linear__save_issue", {"id": "ENG-1", "state": "Canceled"}), 0)

    def test_linear_create_in_done_silent(self):
        self.assertEqual(self.post("mcp__linear__save_issue", {"title": "x", "team": "ENG", "state": "Done"}), 0)

    def test_linear_connector_with_opaque_server_name_plays(self):
        self.earlier_result(self.LINEAR_STATES)
        self.assertEqual(self.post("mcp__4ba31cde-b778-4ff1__save_issue", {"id": "ENG-1", "state": "s-ship"}), 1)

    def test_linear_already_done_issue_silent(self):
        old = [{"type": "text", "text": json.dumps({"id": "AMD-5", "status": "Done", "statusType": "completed",
                                                    "completedAt": "2026-01-02T03:04:05.000Z"})}]
        self.assertEqual(self.post("mcp__linear__save_issue", {"id": "AMD-5", "state": "completed"}, old), 0)

    def test_linear_just_completed_plays(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        fresh = [{"type": "text", "text": json.dumps({"id": "AMD-5", "status": "Done", "statusType": "completed",
                                                      "completedAt": now})}]
        self.assertEqual(self.post("mcp__linear__save_issue", {"id": "AMD-5", "state": "completed"}, fresh), 1)

    @staticmethod
    def linear_at(updated_s_ago, completed_s_ago):
        # Live save_issue results (AMD-6, 2026-10-19): a no-op Done keeps the old updatedAt; Done after a
        # reopen sets updatedAt to now but restores the old completedAt.
        from datetime import datetime, timedelta, timezone
        stamp = lambda s: (datetime.now(timezone.utc) - timedelta(seconds=s)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return [{"type": "text", "text": json.dumps({"id": "AMD-6", "status": "Done", "statusType": "completed",
                                                     "updatedAt": stamp(updated_s_ago), "completedAt": stamp(completed_s_ago)})}]

    def test_linear_reclose_after_reopen_plays(self):
        self.assertEqual(self.post("mcp__linear__save_issue", {"id": "AMD-6", "state": "Done"}, self.linear_at(1, 600)), 1)

    def test_linear_done_resent_soon_after_closing_silent(self):
        self.assertEqual(self.post("mcp__linear__save_issue", {"id": "AMD-6", "state": "Done"}, self.linear_at(60, 60)), 0)

    def test_linear_error_text_as_result_silent(self):
        err = [{"type": "text", "text": 'Error: Could not find issue "AMD-99999". To create a new issue, omit the "id" parameter.'}]
        self.assertEqual(self.post("mcp__linear__save_issue", {"id": "AMD-99999", "state": "Done"}, err), 0)

    def test_linear_failed_call_silent(self):
        self.assertEqual(self.post("mcp__linear__save_issue", {"id": "AMD-5", "state": "completed"},
                                   hook_event_name="PostToolUseFailure"), 0)

    def test_linear_update_issue_with_opaque_server_name_plays(self):
        self.assertEqual(self.post("mcp__4ba31cde__update_issue", {"id": "ENG-1", "state": "completed"}), 1)

    def test_linear_non_state_edit_silent(self):
        self.assertEqual(self.post("mcp__linear__save_issue", {"id": "ENG-1", "title": "Renamed"}), 0)

    # ---- Jira

    JIRA_TRANSITIONS = {"transitions": [
        {"id": "21", "name": "In Progress", "to": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}},
        {"id": "31", "name": "Ship it", "to": {"name": "Released", "statusCategory": {"key": "done"}}},
        {"id": "41", "name": "Won't Do", "to": {"name": "Won't Do", "statusCategory": {"key": "done"}}}]}

    @staticmethod
    def jira_result(tid, status):
        # transitionJiraIssue's real result shape, captured live on 2026-10-19.
        return [{"type": "text", "text": json.dumps({"data": {"message": "Issue PROJ-1 transitioned successfully",
                                                              "transitionId": tid, "statusName": status}})}]

    def test_jira_transition_by_name_uses_result_id(self):
        self.earlier_result(self.JIRA_TRANSITIONS)
        self.assertEqual(self.post("mcp__atlassian__transitionJiraIssue",
                                   {"cloudId": "c", "issueIdOrKey": "PROJ-1", "transitionName": "Ship it"},
                                   self.jira_result(31, "Released")), 1)

    def test_jira_transition_by_name_to_in_progress_silent(self):
        self.earlier_result(self.JIRA_TRANSITIONS)
        self.assertEqual(self.post("mcp__atlassian__transitionJiraIssue",
                                   {"cloudId": "c", "issueIdOrKey": "PROJ-1", "transitionName": "In Progress"},
                                   self.jira_result(21, "In Progress")), 0)

    def test_jira_no_listing_falls_back_to_landed_status_name(self):
        self.assertEqual(self.post("mcp__atlassian__transitionJiraIssue",
                                   {"cloudId": "c", "issueIdOrKey": "PROJ-1", "transitionName": "Done"},
                                   self.jira_result(41, "Done")), 1)

    def test_jira_no_listing_wont_do_name_silent(self):
        self.assertEqual(self.post("mcp__atlassian__transitionJiraIssue",
                                   {"cloudId": "c", "issueIdOrKey": "PROJ-1", "transitionName": "Won't Do"},
                                   self.jira_result(51, "Won't Do")), 0)

    def test_jira_transition_id_string_input(self):
        self.earlier_result(self.JIRA_TRANSITIONS)
        self.assertEqual(self.post("mcp__atlassian__transitionJiraIssue",
                                   {"cloudId": "c", "issueIdOrKey": "PROJ-1", "transitionId": "31"},
                                   self.jira_result(31, "Released")), 1)

    def test_jira_transition_to_done_category_plays(self):
        self.earlier_result(self.JIRA_TRANSITIONS)
        self.assertEqual(self.post("mcp__atlassian__transitionJiraIssue",
                                   {"cloudId": "c", "issueIdOrKey": "PROJ-1", "transition": {"id": "31"}}), 1)

    def test_jira_transition_to_in_progress_silent(self):
        self.earlier_result(self.JIRA_TRANSITIONS)
        self.assertEqual(self.post("mcp__atlassian__transitionJiraIssue",
                                   {"cloudId": "c", "issueIdOrKey": "PROJ-1", "transition": {"id": "21"}}), 0)

    def test_jira_wont_do_in_done_category_silent(self):
        self.earlier_result(self.JIRA_TRANSITIONS)
        self.assertEqual(self.post("mcp__atlassian__transitionJiraIssue",
                                   {"cloudId": "c", "issueIdOrKey": "PROJ-1", "transition": {"id": "41"}}), 0)

    def test_jira_connector_with_opaque_server_name_plays(self):
        self.earlier_result(self.JIRA_TRANSITIONS)
        self.assertEqual(self.post("mcp__9f1e__transitionJiraIssue",
                                   {"cloudId": "c", "issueIdOrKey": "PROJ-1", "transition": {"id": "31"}}), 1)

    def test_jira_listing_fetched_by_a_subagent_is_found(self):
        # Live: a subagent's hook event carries agent_id and the parent's transcript_path; its own calls are
        # in <session>/subagents/agent-<id>.jsonl. "Released" isn't a done word, so only the listing decides.
        sub = self.transcript.with_suffix("") / "subagents" / "agent-a1b2c3.jsonl"
        sub.parent.mkdir(parents=True)
        main, self.transcript = self.transcript, sub
        self.earlier_result(self.JIRA_TRANSITIONS)
        self.transcript = main
        args = {"cloudId": "c", "issueIdOrKey": "PROJ-1", "transitionId": "31"}
        self.assertEqual(self.post("mcp__atlassian__transitionJiraIssue", args, self.jira_result(31, "Released")), 0)
        self.assertEqual(self.post("mcp__atlassian__transitionJiraIssue", args, self.jira_result(31, "Released"),
                                   agent_id="a1b2c3", agent_type="general-purpose"), 1)

    def test_jira_unknown_transition_silent(self):
        self.assertEqual(self.post("mcp__atlassian__transitionJiraIssue",
                                   {"cloudId": "c", "issueIdOrKey": "PROJ-1", "transition": {"id": "31"}}), 0)

    def test_jira_same_transition_id_in_another_workflow_is_ignored(self):
        # Default workflows reuse IDs: 21 is In Progress for PROJ, Done for OPS (listed later).
        self.earlier_result(self.JIRA_TRANSITIONS)
        self.earlier_result({"transitions": [{"id": "21", "name": "Finish", "to": {"name": "Done", "statusCategory": {"key": "done"}}}]})
        self.assertEqual(self.post("mcp__atlassian__transitionJiraIssue",
                                   {"cloudId": "c", "issueIdOrKey": "PROJ-1", "transitionId": "21"},
                                   self.jira_result(21, "In Progress")), 0)

    def test_jira_matching_listing_found_behind_another_workflows(self):
        self.earlier_result(self.JIRA_TRANSITIONS)
        self.earlier_result({"transitions": [{"id": "31", "name": "Review", "to": {"name": "In Review", "statusCategory": {"key": "indeterminate"}}}]})
        self.assertEqual(self.post("mcp__atlassian__transitionJiraIssue",
                                   {"cloudId": "c", "issueIdOrKey": "PROJ-1", "transitionId": "31"},
                                   self.jira_result(31, "Released")), 1)

    def test_jira_edit_silent(self):
        self.assertEqual(self.post("mcp__atlassian__editJiraIssue",
                                   {"cloudId": "c", "issueIdOrKey": "PROJ-1", "fields": {"summary": "x"}}), 0)

    # ---- Asana

    @staticmethod
    def asana_result(updated_gids, failed=0):
        # update_tasks' real result shape, captured live on 2026-10-19.
        payload = {"data": [{"gid": g, "name": "t"} for g in updated_gids],
                   "failed": [{"errors": [{"error": "not_found"}]}] * failed,
                   "summary": f"Updated {len(updated_gids)} of {len(updated_gids) + failed} tasks."}
        return [{"type": "text", "text": json.dumps(payload)}]

    def test_asana_complete_plays(self):
        self.assertEqual(self.post("mcp__asana__update_tasks", {"tasks": [{"task": "1", "completed": True}]},
                                   self.asana_result(["1"])), 1)

    def test_asana_connector_with_opaque_server_name_plays(self):
        self.assertEqual(self.post("mcp__367ac3e7-dc83__update_tasks", {"tasks": [{"task": "1", "completed": True}]},
                                   self.asana_result(["1"])), 1)

    def test_asana_result_as_plain_string_plays(self):
        text = self.asana_result(["1"])[0]["text"]
        self.assertEqual(self.post("mcp__asana__update_tasks", {"tasks": [{"task": "1", "completed": True}]}, text), 1)

    def test_asana_failed_completion_silent(self):
        self.assertEqual(self.post("mcp__asana__update_tasks", {"tasks": [{"task": "9", "completed": True}]},
                                   self.asana_result([], failed=1)), 0)

    def test_asana_complete_one_rename_other_only_rename_succeeded_silent(self):
        args = {"tasks": [{"task": "1", "completed": True}, {"task": "2", "name": "x"}]}
        self.assertEqual(self.post("mcp__asana__update_tasks", args, self.asana_result(["2"], failed=1)), 0)

    def test_asana_single_task_tool_plays(self):
        result = [{"type": "text", "text": json.dumps({"data": {"gid": "1", "completed": True}})}]
        self.assertEqual(self.post("mcp__asana__asana_update_task", {"task_id": "1", "completed": True}, result), 1)

    def test_asana_approval_approved_plays(self):
        self.assertEqual(self.post("mcp__asana__update_tasks", {"tasks": [{"task": "1", "approval_status": "approved"}]},
                                   self.asana_result(["1"])), 1)

    def test_asana_uncomplete_silent(self):
        self.assertEqual(self.post("mcp__asana__update_tasks", {"tasks": [{"task": "1", "completed": False}]},
                                   self.asana_result(["1"])), 0)

    def test_asana_rename_silent(self):
        self.assertEqual(self.post("mcp__asana__update_tasks", {"tasks": [{"task": "1", "name": "x"}]},
                                   self.asana_result(["1"])), 0)

    # ---- everything else

    def test_unwatched_mcp_tool_silent(self):
        self.assertEqual(self.post("mcp__notion__update_page", {"completed": True, "state": "Done", "id": "1"}), 0)


class Clip(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.env = dict(os.environ, GANGSTA_CLOSER_HOME=str(self.home), GANGSTA_CLOSER_PLAYER="true")

    def clip(self, arg):
        return subprocess.run([sys.executable, str(HOOK), "clip", arg], text=True, capture_output=True,
                              env=self.env, timeout=30)

    def test_set_clip_with_spaces_and_tilde_style_escapes(self):
        d = self.tmp / "My Music"
        d.mkdir()
        (d / "win.mp3").write_bytes(b"ID3" + b"\0" * 100)
        out = self.clip(str(d).replace(" ", "\\ ") + "/win.mp3")
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertTrue((self.home / "clip.mp3").exists())

    def test_new_clip_replaces_old_format(self):
        for name in ("a.mp3", "b.wav"):
            (self.tmp / name).write_bytes(b"x" * 10)
            self.assertEqual(self.clip(str(self.tmp / name)).returncode, 0)
        self.assertFalse((self.home / "clip.mp3").exists())
        self.assertTrue((self.home / "clip.wav").exists())

    def rejected(self, out, *words):
        # A non-zero exit would abort the slash command and hide the reason, so refusals exit 0.
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn("Not set", out.stdout)
        for w in words:
            self.assertIn(w, out.stdout)

    def test_rejects_too_big(self):
        (self.tmp / "big.mp3").write_bytes(b"x" * (5 * 1024 * 1024 + 1))
        self.rejected(self.clip(str(self.tmp / "big.mp3")), "5 MB")

    def test_rejects_unsupported_format(self):
        (self.tmp / "song.flac").write_bytes(b"x")
        self.rejected(self.clip(str(self.tmp / "song.flac")))

    @unittest.skipUnless(sys.platform == "darwin", "OGG is only refused on macOS")
    def test_rejects_ogg_on_macos(self):
        (self.tmp / "song.ogg").write_bytes(b"x")
        self.rejected(self.clip(str(self.tmp / "song.ogg")), "OGG")

    def test_missing_file(self):
        self.rejected(self.clip(str(self.tmp / "nope.mp3")))

    def test_reset(self):
        (self.tmp / "a.mp3").write_bytes(b"x")
        self.clip(str(self.tmp / "a.mp3"))
        self.assertEqual(self.clip("reset").returncode, 0)
        self.assertFalse((self.home / "clip.mp3").exists())

    def test_setting_the_current_clip_again_keeps_it(self):
        (self.tmp / "a.mp3").write_bytes(b"x" * 10)
        self.clip(str(self.tmp / "a.mp3"))
        out = self.clip(str(self.home / "clip.mp3"))
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertEqual((self.home / "clip.mp3").read_bytes(), b"x" * 10)

    @unittest.skipIf(os.geteuid() == 0, "root can read anything")
    def test_unreadable_new_clip_keeps_the_old_one(self):
        (self.tmp / "a.mp3").write_bytes(b"old")
        self.clip(str(self.tmp / "a.mp3"))
        (self.tmp / "b.wav").write_bytes(b"new")
        (self.tmp / "b.wav").chmod(0)
        self.rejected(self.clip(str(self.tmp / "b.wav")), "unchanged")
        self.assertEqual((self.home / "clip.mp3").read_bytes(), b"old")

    def test_clip_and_folder_are_private(self):
        (self.tmp / "a.mp3").write_bytes(b"x")
        self.clip(str(self.tmp / "a.mp3"))
        self.assertEqual(self.home.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.home / "clip.mp3").stat().st_mode & 0o777, 0o600)

    def test_no_player_and_no_chime_says_so(self):
        env = dict(self.env, PATH=str(self.tmp))  # no players on PATH
        env.pop("GANGSTA_CLOSER_PLAYER")
        out = subprocess.run([sys.executable, str(HOOK), "clip", "test"], text=True, capture_output=True,
                             env=env, timeout=30)
        self.assertEqual(out.returncode, 0)
        if not Path("/System/Library/Sounds/Glass.aiff").exists() and \
                not Path("/usr/share/sounds/freedesktop/stereo/complete.oga").exists():
            self.assertIn("Nothing on this machine can play it", out.stdout)


class ClipFallback(unittest.TestCase):
    def test_test_says_when_the_chime_plays_instead_of_the_clip(self):
        import contextlib, importlib.util, io
        tmp = Path(tempfile.mkdtemp())
        os.environ["GANGSTA_CLOSER_HOME"] = str(tmp)
        os.environ.pop("GANGSTA_CLOSER_PLAYER", None)
        try:
            spec = importlib.util.spec_from_file_location("closer_fb", HOOK)
            c = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(c)
        finally:
            os.environ.pop("GANGSTA_CLOSER_HOME")
        (tmp / "clip.m4a").write_bytes(b"x")
        (tmp / "chime.oga").write_bytes(b"x")
        c.CLIP_FORMATS = (".m4a",)
        c.sys = type("S", (), {"platform": "linux"})()
        c.shutil = type("Sh", (), {"which": staticmethod(lambda cmd: "/usr/bin/paplay" if cmd == "paplay" else None)})()
        c.fallback_sound = lambda: tmp / "chime.oga"
        c.subprocess = type("Sp", (), {"Popen": staticmethod(lambda *a, **k: None), "DEVNULL": None})()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            c.cmd_clip("test")
        self.assertIn("system chime played instead", out.getvalue())


class Players(unittest.TestCase):
    """Which player each clip format gets on Linux (paplay can't decode M4A, or MP3 on older libsndfile)."""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("closer", HOOK)
        self.closer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.closer)
        self.closer.sys = type("S", (), {"platform": "linux"})()
        os.environ.pop("GANGSTA_CLOSER_PLAYER", None)

    def player(self, name, installed):
        self.closer.shutil = type("Sh", (), {"which": staticmethod(lambda c: f"/usr/bin/{c}" if c in installed else None)})()
        cmd = self.closer.player_for(Path(name))
        return cmd[0] if cmd else None

    def test_m4a_skips_paplay(self):
        self.assertEqual(self.player("clip.m4a", {"paplay", "ffplay"}), "ffplay")
        self.assertIsNone(self.player("clip.m4a", {"paplay", "aplay"}))

    def test_mp3_prefers_ffplay_then_mpg123(self):
        self.assertEqual(self.player("clip.mp3", {"paplay", "ffplay"}), "ffplay")
        self.assertEqual(self.player("clip.mp3", {"paplay", "mpg123"}), "mpg123")

    def test_wav_and_ogg_use_paplay(self):
        self.assertEqual(self.player("clip.wav", {"paplay", "ffplay"}), "paplay")
        self.assertEqual(self.player("chime.oga", {"paplay"}), "paplay")


if __name__ == "__main__":
    unittest.main(verbosity=1)
