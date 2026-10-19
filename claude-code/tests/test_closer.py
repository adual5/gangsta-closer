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
import json, os, sys
args = sys.argv[1:]
state = json.load(open(os.environ["FAKE_GH_STATE"]))
kind = args[0]
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
                        PLAYED_LOG=str(self.played_log), GANGSTA_CLOSER_DEBUG="1")
        self.n = 0

    def gh_state(self, issues=(), prs=(), current_pr=None):
        state = {"issue": {}, "pr": {}}
        for i in issues:
            state["issue"][i["url"].rsplit("/", 1)[1]] = i
        for p in prs:
            state["pr"][p["url"].rsplit("/", 1)[1]] = p
        if current_pr:
            state["pr"][""] = current_pr
        self.state_file.write_text(json.dumps(state))

    def run_hook(self, mode, event):
        subprocess.run([sys.executable, str(HOOK), mode], input=json.dumps(event), text=True, env=self.env,
                       check=True, timeout=30)

    def plays(self):
        time.sleep(0.4)
        return self.played_log.read_text().count("\n") if self.played_log.exists() else 0

    def call(self, tool_name, tool_input, before, after, tool_response=None):
        """Simulate one tool call: gh state `before`, pre hook, state becomes `after`, post hook."""
        self.n += 1
        event = {"tool_name": tool_name, "tool_input": tool_input, "tool_use_id": f"toolu_{self.n}",
                 "cwd": str(self.tmp), "hook_event_name": "PreToolUse"}
        self.gh_state(**before)
        self.run_hook("pre", event)
        self.gh_state(**after)
        self.run_hook("post", dict(event, hook_event_name="PostToolUse", tool_response=tool_response or {}))
        return self.plays()

    def bash(self, command, before, after):
        return self.call("Bash", {"command": command}, before, after)

    # ---- GitHub through gh

    def test_issue_close_completed_plays(self):
        self.assertEqual(self.bash("gh issue close 12", {"issues": [issue(12)]},
                                   {"issues": [issue(12, "CLOSED", "COMPLETED")]}), 1)

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
        # PostToolUse doesn't fire for a failed call; the next unrelated call must not play.
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

    def post(self, tool_name, tool_input, tool_response="", mcp_server=None):
        event = {"tool_name": tool_name, "tool_input": tool_input, "tool_response": tool_response,
                 "tool_use_id": "toolu_1", "transcript_path": str(self.transcript), "cwd": str(self.tmp)}
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

    def test_jira_unknown_transition_silent(self):
        self.assertEqual(self.post("mcp__atlassian__transitionJiraIssue",
                                   {"cloudId": "c", "issueIdOrKey": "PROJ-1", "transition": {"id": "31"}}), 0)

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

    def test_rejects_too_big(self):
        (self.tmp / "big.mp3").write_bytes(b"x" * (5 * 1024 * 1024 + 1))
        out = self.clip(str(self.tmp / "big.mp3"))
        self.assertEqual(out.returncode, 1)
        self.assertIn("5 MB", out.stdout)

    def test_rejects_unsupported_format(self):
        (self.tmp / "song.flac").write_bytes(b"x")
        self.assertEqual(self.clip(str(self.tmp / "song.flac")).returncode, 1)

    @unittest.skipUnless(sys.platform == "darwin", "OGG is only refused on macOS")
    def test_rejects_ogg_on_macos(self):
        (self.tmp / "song.ogg").write_bytes(b"x")
        out = self.clip(str(self.tmp / "song.ogg"))
        self.assertEqual(out.returncode, 1)
        self.assertIn("OGG", out.stdout)

    def test_missing_file(self):
        self.assertEqual(self.clip(str(self.tmp / "nope.mp3")).returncode, 1)

    def test_reset(self):
        (self.tmp / "a.mp3").write_bytes(b"x")
        self.clip(str(self.tmp / "a.mp3"))
        self.assertEqual(self.clip("reset").returncode, 0)
        self.assertFalse((self.home / "clip.mp3").exists())


if __name__ == "__main__":
    unittest.main(verbosity=1)
