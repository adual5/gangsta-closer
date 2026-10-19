"""End-to-end tests: loads the unpacked extension into Chromium and drives real input.

  python3 -m venv .venv && .venv/bin/pip install playwright && .venv/bin/playwright install chromium
  .venv/bin/python test/run_tests.py            # fixtures + live public GitHub pages
  .venv/bin/python test/run_tests.py --offline  # fixtures only

Jira has no public instance to test against, so *.atlassian.net is served from
test/fixtures (mock pages that use Jira's real data-testids). GitHub tests run
against real public pages; the state change itself is simulated in the DOM since
closing needs write access.
"""
import shutil
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "test" / "fixtures"
OFFLINE = "--offline" in sys.argv
SETTLE = 2.5  # seconds to let the engine re-check after an action
COOLDOWN = 3.2

results = []


def check(name, got, want):
    ok = got == want
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  (plays: got {got}, want {want})"))


def check_ok(name, res, chime):
    audio = (res or {}).get("audio") or {}
    ok = audio.get("ok") is True and audio.get("chime") == chime
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  (got {res})"))


def beep_wav():
    """A synthetic 0.2 s 440 Hz WAV as a data URL, so the clip path is tested without real music."""
    import base64, io, math, struct, wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / 8000))) for i in range(1600)))
    return "data:audio/wav;base64," + base64.b64encode(buf.getvalue()).decode()


def main():
    profile = tempfile.mkdtemp(prefix="gangsta-closer-test-")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            profile,
            channel="chromium",
            headless=True,
            args=[f"--disable-extensions-except={ROOT}", f"--load-extension={ROOT}"],
        )
        sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker")

        def plays():
            return sw.evaluate("chrome.storage.local.get('closes').then(r => r.closes || 0)")

        def last_audio():
            return sw.evaluate("chrome.storage.local.get('lastClose').then(r => r.lastClose && r.lastClose.audio)")

        # --- Mock servers -------------------------------------------------------
        pr_state = {"s": "Open"}

        def serve_jira(route):
            url = route.request.url
            name = "jira-issue.html" if "/browse/" in url else "jira-board.html"
            route.fulfill(path=str(FIXTURES / name))

        def serve_pr(route):
            if route.request.url.endswith("/board"):
                return route.fulfill(path=str(FIXTURES / "github-board.html"))
            if route.request.method == "POST":
                pr_state["s"] = "Closed"
            html = (FIXTURES / "github-pr.html").read_text().replace("{{STATE}}", pr_state["s"]).replace("{{STATE_LC}}", pr_state["s"].lower())
            route.fulfill(body=html, content_type="text/html")

        ctx.route("https://acme.atlassian.net/**", serve_jira)
        ctx.route("https://linear.app/gangsta-fixture/**", lambda route: route.fulfill(path=str(FIXTURES / "linear-board.html")))
        ctx.route("https://github.com/gangsta-fixture/**", serve_pr)
        ctx.route("https://app.asana.com/**", lambda route: route.fulfill(path=str(FIXTURES / "asana.html")))

        # onInstalled opens the options page, and Chrome may put it in whichever tab is blank; wait for it
        # so it can't take over the tab the tests navigate.
        deadline = time.time() + 5
        while time.time() < deadline and not any("/options/options.html" in pg.url for pg in ctx.pages):
            time.sleep(0.1)
        page = ctx.new_page()

        # --- Jira board ---------------------------------------------------------
        print("Jira board (fixture)")
        page.goto("https://acme.atlassian.net/jira/software/projects/GC/boards/1")
        page.wait_for_timeout(800)
        col = lambda n: f'[data-name="{n}"]'

        n = plays()
        page.drag_and_drop("#card-GC-1", col("Done"))
        page.wait_for_timeout(SETTLE * 1000)
        check("drag To Do -> Done plays", plays() - n, 1)
        page.wait_for_timeout(COOLDOWN * 1000)

        n = plays()
        page.drag_and_drop("#card-GC-2", col("In Progress"))
        page.wait_for_timeout(SETTLE * 1000)
        check("drag To Do -> In Progress is silent", plays() - n, 0)

        n = plays()
        box = lambda sel: page.locator(sel).bounding_box()
        c = box("#card-GC-3")
        page.mouse.move(c["x"] + 20, c["y"] + 10)
        page.mouse.down()
        for target in ("Done", "Done", "In Review"):
            b = box(col(target))
            page.mouse.move(b["x"] + 50, b["y"] + 150, steps=8)
            page.wait_for_timeout(300)
        page.mouse.up()
        page.wait_for_timeout(SETTLE * 1000)
        in_review = page.evaluate("document.querySelector('[data-name=\"In Review\"]').contains(document.querySelector('#card-GC-3'))")
        check(f"drag across Done, drop in In Review is silent (card landed in In Review: {in_review})", plays() - n, 0)

        n = plays()
        page.drag_and_drop("#card-GC-4", col("Won't Do"))
        page.wait_for_timeout(SETTLE * 1000)
        check("drag -> Won't Do is silent", plays() - n, 0)

        # --- Jira issue view ----------------------------------------------------
        print("Jira issue view (fixture)")
        page.goto("https://acme.atlassian.net/browse/GC-7")
        page.wait_for_timeout(800)
        page.wait_for_timeout(COOLDOWN * 1000)

        n = plays()
        page.click("#status")
        page.click("text=Won't Do")
        page.wait_for_timeout(SETTLE * 1000)
        check("status -> Won't Do is silent", plays() - n, 0)

        page.reload()
        page.wait_for_timeout(800)
        n = plays()
        page.click("#status")
        page.click("[role=menuitem]:text-is('Done')")
        page.wait_for_timeout(SETTLE * 1000)
        check("status In Progress -> Done plays", plays() - n, 1)

        page.reload()
        page.wait_for_timeout(800)
        page.click("#unrelated")
        page.wait_for_timeout(9000)  # outside the input window
        n = plays()
        page.evaluate("remoteSetStatus('Done')")
        page.wait_for_timeout(SETTLE * 1000)
        check("teammate sets Done (no local input) is silent", plays() - n, 0)

        # --- GitHub classic PR: close via form POST + full reload ---------------
        print("GitHub PR close with full page reload (fixture)")
        page.wait_for_timeout(COOLDOWN * 1000)
        page.goto("https://github.com/gangsta-fixture/repo/pull/1")
        page.wait_for_timeout(800)
        n = plays()
        page.click("#close")
        page.wait_for_load_state()
        page.wait_for_timeout(SETTLE * 1000)
        check("Close pull request (page reloads) plays", plays() - n, 1)

        page.wait_for_timeout(COOLDOWN * 1000)
        pr_state["s"] = "Open"
        page.goto("https://github.com/gangsta-fixture/repo/pull/1")
        page.wait_for_timeout(800)
        page.keyboard.press("Shift")
        pr_state["s"] = "Closed"  # someone else closes it; this tab still shows Open
        n = plays()
        page.reload()
        page.wait_for_timeout(SETTLE * 1000)
        check("reload after someone else's close is silent", plays() - n, 0)

        pr_state["s"] = "Open"
        page.goto("https://github.com/gangsta-fixture/repo/pull/1")
        page.wait_for_timeout(800)
        page.mouse.click(5, 300)
        page.goto("https://github.com/gangsta-fixture/board")
        page.wait_for_timeout(4000)  # the PR is off screen for a while
        pr_state["s"] = "Closed"
        page.mouse.click(5, 300)
        n = plays()
        page.goto("https://github.com/gangsta-fixture/repo/pull/1")
        page.wait_for_timeout(SETTLE * 1000)
        check("coming back to a PR someone else closed is silent", plays() - n, 0)

        # --- GitHub Projects board ----------------------------------------------
        print("GitHub Projects board (fixture)")
        page.wait_for_timeout(COOLDOWN * 1000)
        page.goto("https://github.com/gangsta-fixture/board")
        page.wait_for_timeout(800)
        gcol = lambda n: f'[data-board-column="{n}"]'
        n = plays()
        page.drag_and_drop("#c101", gcol("Done"))
        page.wait_for_timeout(SETTLE * 1000)
        check("drag Todo -> Done plays", plays() - n, 1)
        page.wait_for_timeout(COOLDOWN * 1000)

        n = plays()
        page.drag_and_drop("#c102", gcol("Shipped"))
        page.wait_for_timeout(SETTLE * 1000)
        check("drag Todo -> Shipped is silent by default", plays() - n, 0)

        sw.evaluate("chrome.storage.sync.set({ extraDone: 'Shipped' })")
        page.wait_for_timeout(300)
        n = plays()
        page.drag_and_drop("#c103", gcol("Shipped"))
        page.wait_for_timeout(SETTLE * 1000)
        check("drag In Progress -> Shipped plays with 'Shipped' as extra done name", plays() - n, 1)
        sw.evaluate("chrome.storage.sync.set({ extraDone: '' })")
        page.wait_for_timeout(9000)  # past the input window of the drags above

        # A card's status set from its side panel: the click on the card, then input in the panel.
        move = "(id) => document.querySelector('[data-board-column=\"Done\"]').append(document.getElementById(id))"
        page.click("#c102")
        page.wait_for_timeout(500)
        page.mouse.click(5, 5)  # in the side panel, not on the card
        n = plays()
        page.evaluate(move, "c103")
        page.wait_for_timeout(SETTLE * 1000)
        check("teammate's card moves to Done while you work in another card's panel is silent", plays() - n, 0)
        page.mouse.click(5, 5)
        n = plays()
        page.evaluate(move, "c102")
        page.wait_for_timeout(SETTLE * 1000)
        check("card set to Done from its side panel plays", plays() - n, 1)

        # --- Linear board -------------------------------------------------------
        print("Linear board (fixture of live DOM structure)")
        page.wait_for_timeout(COOLDOWN * 1000)
        page.goto("https://linear.app/gangsta-fixture/team/OPS/all")
        page.wait_for_timeout(800)
        lcol = lambda i: f'[data-board-column="{i}"]'

        def pointer_drag(card, *columns):
            c = box(card)
            page.mouse.move(c["x"] + 20, c["y"] + 10)
            page.mouse.down()
            for i in columns:
                b = box(lcol(i))
                page.mouse.move(b["x"] + 60, b["y"] + 200, steps=6)
                page.wait_for_timeout(200)
            page.mouse.up()

        n = plays()
        pointer_drag("#OPS-15", 1)
        page.wait_for_timeout(SETTLE * 1000)
        check("drag Backlog -> Todo is silent", plays() - n, 0)

        n = plays()
        pointer_drag("#OPS-15", 4)
        page.wait_for_timeout(SETTLE * 1000)
        check("drag Todo -> Done plays", plays() - n, 1)
        page.wait_for_timeout(COOLDOWN * 1000)

        n = plays()
        pointer_drag("#OPS-16", 4, 3)
        page.wait_for_timeout(SETTLE * 1000)
        check("drag across Done, drop in In Review is silent", plays() - n, 0)

        n = plays()
        pointer_drag("#OPS-17", 5)
        page.wait_for_timeout(SETTLE * 1000)
        check("drag -> Canceled is silent", plays() - n, 0)

        # --- Asana (mock) ------------------------------------------------------
        print("Asana list, task pane and board (fixture of live DOM structure)")
        page.wait_for_timeout(COOLDOWN * 1000)
        page.goto("https://app.asana.com/1/1/project/111/task/222")
        page.wait_for_timeout(800)

        n = plays()
        page.click("#row301")
        page.wait_for_timeout(SETTLE * 1000)
        check("list row: tick the checkmark plays", plays() - n, 1)
        page.wait_for_timeout(COOLDOWN * 1000)

        n = plays()
        page.click("#row302")
        page.wait_for_timeout(SETTLE * 1000)
        check("list row: untick a completed task is silent", plays() - n, 0)

        n = plays()
        page.click("#pane-button")
        page.wait_for_timeout(SETTLE * 1000)
        check("task pane (wide): Mark complete plays", plays() - n, 1)
        page.wait_for_timeout(COOLDOWN * 1000)

        n = plays()
        page.click("#pane-button")
        page.wait_for_timeout(SETTLE * 1000)
        check("task pane: mark incomplete is silent", plays() - n, 0)

        n = plays()
        page.evaluate("narrowPane()")
        page.click("#pane-check")
        page.wait_for_timeout(SETTLE * 1000)
        check("task pane (narrow): checkmark plays", plays() - n, 1)
        page.wait_for_timeout(COOLDOWN * 1000)

        n = plays()
        page.drag_and_drop("#c401", '[data-name="Done"]')
        page.wait_for_timeout(SETTLE * 1000)
        check("board: drag Doing -> Done plays", plays() - n, 1)
        page.wait_for_timeout(COOLDOWN * 1000)

        n = plays()
        page.drag_and_drop("#c402", '[data-name="Review"]')
        page.wait_for_timeout(SETTLE * 1000)
        check("board: drag Doing -> Review is silent", plays() - n, 0)

        page.wait_for_timeout(9000)  # outside the input window
        n = plays()
        page.evaluate("remoteComplete('402')")
        page.wait_for_timeout(SETTLE * 1000)
        check("teammate completes a task (no local input) is silent", plays() - n, 0)

        n = plays()
        page.mouse.click(5, 5)
        page.evaluate("""() => document.querySelector('#row302').setAttribute('aria-checked', 'true')""")
        page.wait_for_timeout(SETTLE * 1000)
        check("teammate completes a task while you click elsewhere is silent", plays() - n, 0)

        # --- Injecting into a tab that already runs the engine is safe ------------
        # (install and update themselves are driven in install_and_update(), in a browser of their own)
        print("Injection into an already-open tab (fixture)")
        page.wait_for_timeout(COOLDOWN * 1000)
        page.goto("https://acme.atlassian.net/browse/GC-8")
        page.wait_for_timeout(800)
        sw.evaluate("injectIntoOpenTabs()")
        page.wait_for_timeout(800)
        n = plays()
        page.click("#status")
        page.click("[role=menuitem]:text-is('Done')")
        page.wait_for_timeout(SETTLE * 1000)
        plays_in_page = page.evaluate("Number(document.documentElement.dataset.gangstaCloserPlays || 0)")
        check(f"injected twice, one close plays once (engine plays in page: {plays_in_page})", plays() - n, 1)

        # --- Popup: Extra done names survive the popup closing ------------------
        print("Popup Extra done names (popup closes without a change event)")
        ext = sw.url.split("/")[2]
        for how in ("close right after typing", "Escape, then close"):
            pop = ctx.new_page()
            pop.goto(f"chrome-extension://{ext}/src/popup/popup.html")
            pop.wait_for_timeout(400)
            value = "Shipped " + how.split()[0]
            pop.fill("#extraDone", "")
            pop.type("#extraDone", value)
            if how.startswith("Escape"):
                pop.keyboard.press("Escape")
            pop.close(run_before_unload=True)
            saved = sw.evaluate("chrome.storage.sync.get('extraDone').then(r => r.extraDone)")
            check(f"{how}: typed names are saved", saved == value, True)
        sw.evaluate("chrome.storage.sync.set({ extraDone: '' })")

        if not OFFLINE:
            live_github(page, plays)

        print(f"\nAudio result of last play: {last_audio()}")

        print("Sound source")
        res = sw.evaluate("play({ test: true })")
        check_ok("no clip chosen plays the built-in chime", res, chime=True)
        sw.evaluate(f"chrome.storage.local.set({{ clip: {{ name: 'beep.wav', size: 0, dataUrl: '{beep_wav()}' }} }})")
        res = sw.evaluate("play({ test: true })")
        check_ok("user clip plays", res, chime=None)
        sw.evaluate("chrome.storage.local.remove('clip')")
        ctx.close()

        install_and_update(p, serve_pr, pr_state)

    passed = sum(results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)


def install_and_update(p, serve_pr, pr_state):
    """Tabs open before install, and before an update, play without a refresh. The extension is installed
    after the tab loads (CDP Extensions.loadUnpacked), so Chrome fires the real onInstalled('install');
    loading it again with a higher version fires onInstalled('update')."""
    print("Install and update with tabs already open")
    ext_dir = Path(tempfile.mkdtemp()) / "ext"  # a copy, so the version bump never touches the repo
    shutil.copytree(ROOT, ext_dir, ignore=shutil.ignore_patterns(".git", "test", "claude-code"))
    ctx = p.chromium.launch_persistent_context(tempfile.mkdtemp(prefix="gangsta-closer-install-"), channel="chromium",
                                               headless=True, args=["--enable-unsafe-extension-debugging"],
                                               ignore_default_args=["--disable-extensions"])
    ctx.route("https://github.com/gangsta-fixture/**", serve_pr)
    ctx.route("https://acme.atlassian.net/**", lambda route: route.fulfill(path=str(FIXTURES / "jira-issue.html")))
    page = ctx.new_page()
    pr_state["s"] = "Open"
    page.goto("https://github.com/gangsta-fixture/repo/pull/1")
    page.wait_for_timeout(800)

    cdp = ctx.browser.new_browser_cdp_session()
    ext = cdp.send("Extensions.loadUnpacked", {"path": str(ext_dir)})["id"]
    page.wait_for_timeout(1500)
    # Playwright doesn't pick up a service worker registered this late, so read counts from an extension page.
    probe = ctx.new_page()
    probe.goto(f"chrome-extension://{ext}/src/popup/popup.html")
    plays = lambda: probe.evaluate("chrome.storage.local.get('closes').then(r => r.closes || 0)")

    n = plays()
    page.bring_to_front()
    page.click("#close")
    page.wait_for_load_state()
    page.wait_for_timeout(SETTLE * 1000)
    check("install: tab opened before install, Close pull request plays", plays() - n, 1)

    # The PR close reloads the page, so the update is checked on an in-page Jira status change.
    page.wait_for_timeout(COOLDOWN * 1000)
    page.goto("https://acme.atlassian.net/browse/GC-7")
    page.wait_for_timeout(1000)
    manifest = ext_dir / "manifest.json"
    manifest.write_text(manifest.read_text().replace('"version": "', '"version": "9', 1))
    cdp.send("Extensions.loadUnpacked", {"path": str(ext_dir)})
    page.wait_for_timeout(SETTLE * 1000)
    probe.close()
    probe = ctx.new_page()
    probe.goto(f"chrome-extension://{ext}/src/popup/popup.html")
    n = plays()
    url = page.url
    page.click("#status")
    page.click("[role=menuitem]:text-is('Done')")
    page.wait_for_timeout(SETTLE * 1000)
    check(f"update: same tab, no reload ({page.url == url}), status -> Done plays once", plays() - n, 1)
    ctx.close()


def live_github(page, plays):
    set_state = """([text, icon]) => {
        const el = document.querySelector('[data-testid="header-state"]');
        el.querySelector('svg').setAttribute('class', 'octicon ' + icon);
        [...el.childNodes].filter(n => n.nodeType === 3 || n.tagName === 'SPAN').forEach(n => n.remove());
        el.append(document.createTextNode(text));
    }"""

    print("GitHub issue page (live cli/cli issue, state change simulated)")
    time.sleep(COOLDOWN)
    page.goto("https://github.com/cli/cli/issues/14467")
    page.wait_for_selector('[data-testid="header-state"]')
    page.wait_for_timeout(1500)
    n = plays()
    page.mouse.click(5, 300)
    page.evaluate(set_state, ["Closed", "octicon-issue-closed"])
    page.wait_for_timeout(SETTLE * 1000)
    check("Open -> Closed (completed) plays", plays() - n, 1)

    page.wait_for_timeout(COOLDOWN * 1000)
    page.reload()
    page.wait_for_selector('[data-testid="header-state"]')
    page.wait_for_timeout(1500)
    n = plays()
    page.mouse.click(5, 300)
    page.evaluate(set_state, ["Closed as not planned", "octicon-skip"])
    page.wait_for_timeout(SETTLE * 1000)
    check("Open -> Closed as not planned is silent", plays() - n, 0)

    print("GitHub Projects board (live github/roadmap board, selector check)")
    cdp = page.context.new_cdp_session(page)
    worlds = {}
    cdp.on("Runtime.executionContextCreated", lambda e: worlds.update({e["context"]["name"]: e["context"]["id"]}))
    cdp.send("Runtime.enable")
    page.goto("https://github.com/orgs/github/projects/4247/views/3")
    page.wait_for_selector("[data-board-card-id]")
    page.wait_for_timeout(1000)
    snap = cdp.send("Runtime.evaluate", {"expression": "GangstaCloser.debugSnapshot()", "contextId": worlds["Gangsta Closer"], "returnByValue": True})["result"]["value"]
    cards = {k: v for k, v in snap.items() if k.startswith("card:gh:")}
    real = page.evaluate("[...document.querySelectorAll('[data-board-card-id]')].filter(c => c.closest('[data-board-column]')).length")
    columns = sorted(set(cards.values()))
    ok = len(cards) == real > 0 and all(columns)
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  engine reads {len(cards)}/{real} live cards, columns {columns}")

if __name__ == "__main__":
    main()
