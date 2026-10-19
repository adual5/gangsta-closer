# Gangsta Closer

A Chrome extension that plays a sound clip of your choice every time you close an issue or
pull request in **GitHub**, **Linear**, or **Jira Cloud**, or mark a task complete in **Asana**.

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

After pulling updates, hit the reload ↻ on the extension card and refresh open GitHub/Linear/Jira/Asana tabs.

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

So it only fires when the close actually lands (not when a click fails), never for teammates' changes
you happen to be watching, and not when you drag a card across Done and drop it elsewhere. A full page
reload (classic GitHub PR close) is covered by carrying the baseline across the reload in `sessionStorage`.
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
| Asana | Live: ticking a task complete in a list row, on a board card and in the task pane (wide and narrow) each played, and unticking was silent. The engine and Asana adapter were run in the page with real clicks, not as the installed extension. A drag into a Done column is checked on a mock only |

## License

Code: [MIT](LICENSE). The license covers the code only, not any audio you load into it.
