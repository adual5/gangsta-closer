# Gangsta Closer

A Chrome extension that plays a sound clip of your choice every time you close an issue or
pull request in **GitHub**, **Linear**, or **Jira Cloud**, or mark a task complete in **Asana**.
A [Claude Code plugin](#claude-code-plugin) in the same repo does the same when Claude Code makes the close.

We recommend *"Damn It Feels Good to Be a Gangsta"*. The extension ships no music: you bring
your own clip, and **whatever you choose, make sure you have the right to use it.** Until you
pick one, a short built-in chime plays.

## Install

1. Clone this repo.
2. Open `chrome://extensions`, turn on **Developer mode**.
3. **Load unpacked** → pick the repo folder.
4. The **Choose your clip** page opens. Pick an audio file (MP3, WAV, OGG or M4A, up to 5 MB;
   a few seconds works best). You can reopen it any time from the popup.
5. Click the extension icon → **▶ Test sound**.

Your clip is stored only in your browser (`chrome.storage.local`). Nothing is uploaded, and the
extension collects no data.

After pulling updates, hit the reload ↻ on the extension card. Open GitHub/Linear/Jira/Asana tabs get the new
version without a refresh; if one stops playing, its console says to refresh it. Needs Chrome 116 or later.

## What triggers it

| | Issue / PR page | Board drag-and-drop |
|---|---|---|
| **GitHub** | Close issue (as completed), Close PR, Merge PR — including the issue side panel | Projects board: card dropped in a done column |
| **Linear** | Status set to a done status (menu, keyboard shortcut, detail view) | Board view: card dropped in a done column |
| **Jira Cloud** (`*.atlassian.net`) | Status transitioned to a done status | Board: card dropped in a done column |
| **Asana** (`app.asana.com`) | Task marked complete (checkmark in a list row or the task pane) | Board: card marked complete, or dropped in a done column |

**Done** means a state or column name that starts with *Done, Closed, Complete(d), Resolved,*
or *Merged*, plus anything you add under **Extra “done” names** in the popup (e.g. `Shipped`).
*Closed as not planned, Won't Do, Canceled, Duplicate* are deliberately silent — those are closes, not wins.
In Asana, unticking a completed task is silent too.

## How it detects a close

Every site goes through one engine ([`src/content/engine.js`](src/content/engine.js)):

1. A small per-site adapter reports what's on screen: the viewed item's state and each board card's column.
2. Right **before** each click/keypress/drag, the engine snapshots that as a baseline.
3. For ~8 s **after** the input, it re-checks. Any item or card that went from not-done → done plays the clip.

So it only fires when the close actually lands (not when a click fails), and not when you drag a card
across Done and drop it elsewhere. A board card only counts when your click, drag or key press was on it,
so a teammate's card moving while you work elsewhere on the board stays silent, and an item or card that
was off screen for more than a few seconds starts fresh when it comes back. A page load caused by the close
itself (classic GitHub PR close) is covered by carrying the baseline across it in `sessionStorage`; a manual
reload or Back isn't, so reloading a tab after someone else's close stays silent.
Audio plays from an offscreen extension document, so site CSP/autoplay rules can't block it.

Selectors live in one small file per site ([`github.js`](src/content/github.js), [`linear.js`](src/content/linear.js),
[`jira.js`](src/content/jira.js), [`asana.js`](src/content/asana.js)); when a site redesign breaks detection, that's the only file to touch.
Turn on **Debug logging** in the popup to see `[gangsta-closer]` logs in the page console.

## Tests

```bash
python3 -m venv .venv && .venv/bin/pip install playwright && .venv/bin/playwright install chromium
.venv/bin/python test/run_tests.py            # fixtures + live public GitHub pages
.venv/bin/python test/run_tests.py --offline  # fixtures only
```

Loads the unpacked extension into Chromium and drives real mouse/keyboard/drag input.
Jira has no public instance, so `*.atlassian.net` is served from mock pages that use Jira's
real `data-testid`s. Linear boards and Asana are covered by mocks copied from the live apps' DOM.
See [`test/fixtures`](test/fixtures).

| | Verified how |
|---|---|
| GitHub issue close | Live, real "Close issue" click; plus a live not-planned check and a mock PR close with full reload |
| GitHub Projects board | Live selector check on a real board; drag behaviour on a mock |
| Linear | Live: board drag to Done and issue-page status change to Done both played (v1.0.2); drag behaviour also on a mock |
| Jira | Mock pages only; no Jira instance was available |
| Install and update | Tabs opened before install, and before an update, play without a refresh: the extension is installed and updated into the running browser (CDP `Extensions.loadUnpacked`) so Chrome fires the real `onInstalled` |
| Popup | Extra done names typed and then closed with Esc are saved |
| Asana | Live: ticking a task complete in a list row, on a board card and in the task pane (wide and narrow) each played, and unticking was silent. The engine and Asana adapter were run in the page with real clicks, not as the installed extension. A drag into a Done column is checked on a mock only |

## Claude Code plugin

The same idea for closes made through [Claude Code](https://claude.com/claude-code): the plugin plays your
clip when a tool call closes an issue, merges a PR, or completes a task in GitHub, Linear, Jira Cloud or Asana.
It doesn't need the extension; the two never play for the same close.

**Install** (needs `python3`; GitHub also needs [`gh`](https://cli.github.com) signed in):

```
/plugin marketplace add adual5/gangsta-closer
/plugin install gangsta-closer@gangsta-closer
```

**Your clip:** `/gangsta-closer:clip ~/Music/clip.mp3` copies it to `~/.gangsta-closer/` and plays it once.
MP3, WAV or M4A, up to 5 MB (OGG works on Linux, not macOS). On Linux, M4A needs `ffplay` (ffmpeg) and MP3
needs `ffplay` or `mpg123`; the command says so if nothing can play your file. `/gangsta-closer:clip` alone plays the current
clip; `/gangsta-closer:clip reset` goes back to the system chime. The extension keeps its clip inside Chrome,
so pick the same file in both if you want them to match.

**What plays:**

| | Plays | Silent |
|---|---|---|
| **GitHub** issue: `gh issue close`, `gh api … issues/N`, GitHub MCP `issue_write` / `update_issue_state` / `update_issue` | Closed as completed | Not planned, duplicate, already closed, a close that didn't land |
| **GitHub** PR: `gh pr merge`, `gh api … pulls/N/merge`, GitHub MCP `merge_pull_request` | Merged | Closed without merging; `--auto` or a merge queue (still open when the call returns) |
| **Linear** MCP `save_issue` on an existing issue (state by ID, name or type) | New state's type is *completed* (so a renamed "Shipped" counts) | *canceled*, *duplicate*, any other type, creating an issue |
| **Jira Cloud** Atlassian MCP `transitionJiraIssue` (by transition ID or name) | Lands in a status in the Done category | Other categories; Done-category statuses named Won't Do, Canceled, Duplicate… |
| **Asana** MCP `update_tasks` | `completed: true` on a task the result lists as updated | Unticking, other edits, an update that failed |

**How it decides.** For GitHub, a hook reads the item with `gh` right before the call and again after it;
only a not-done → done change plays, so a failed or repeated close stays quiet, and a close inside a command
that fails afterwards (`gh issue close 12 && npm test`) still plays. The hook only ever asks github.com, about a
plain `owner/repo`; it never contacts another host. The hook has no Linear, Jira
or Asana token, so it reads the MCP call itself: its input and result, plus the state listings Claude
fetched earlier in the session (Linear's statuses, Jira's transitions), from the session transcript.
Linear's result carries the new state's type. Jira's carries only the transition ID and the status name it
landed on, so the category comes from the transition list; without one, the status name decides by the
extension's done-name rule. Plays at most once per
3 seconds, in the background, and never blocks or fails a tool call. Code:
[`claude-code/hooks/closer.py`](claude-code/hooks/closer.py). Set `GANGSTA_CLOSER_DEBUG=1` to log decisions to
`~/.gangsta-closer/debug.log`.

**Known gaps.** Asana and Jira give no before-state, so completing an already-complete Asana task, or
transitioning a Jira issue that's already Done to Done, plays again. GraphQL closes (`gh api graphql`) and
GitHub Enterprise aren't watched. The clip command passes your path to the shell in double quotes, so a
file name containing `$`, a backtick or `"` needs renaming first.

**Tests:** `python3 claude-code/tests/test_closer.py` runs the real hook against a fake `gh` and a fake player
(103 cases).

| | Verified how |
|---|---|
| GitHub | Live, through Claude Code with the plugin installed, on a private throwaway repo: close as completed played; closing it again, not planned, and closing a PR without merging were silent; `gh pr merge 4` and `cd repo && gh pr merge` (PR from the current branch) played |
| Asana | Live, through the Asana connector: completing a task played; unticking, renaming, and a completion that failed (unknown task) were silent |

The test cases for Linear, Jira and Asana use the result formats captured from those live calls.
| Linear | Live, through the Linear connector on a throwaway issue: moving to Done by state ID and by state type (`completed`) played; Canceled and In Progress were silent |
| Jira | Live, through the Atlassian connector on a throwaway issue: transitioning to Done by ID and by transition name played; In Progress and To Do (by ID and by name) were silent |

## License

Code: [MIT](LICENSE). The license covers the code only, not any audio you load into it.
