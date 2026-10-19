// Shared close-detection engine. Each site file calls GangstaCloser.start(adapter).
//
// How detection works (same for every site):
//   1. The adapter returns a snapshot of what's on screen: the open issue/PR's
//      state ("item") and every board card's column ("card"), keyed by identity.
//   2. Right before each user input (pointerdown / keydown / dragstart) we take a
//      silent baseline snapshot, so changes made by teammates never count.
//   3. After the input we re-snapshot for a few seconds. If any key went from a
//      not-done state to a done state, the user closed something: play.
//   While a drag is in flight nothing is compared, so hovering a card over the
//   Done column and dropping it elsewhere doesn't count.
(() => {
  if (window.GangstaCloser) return;

  const BUILTIN_DONE = ['done', 'closed', 'complete', 'completed', 'resolved', 'merged'];
  // "Closed as not planned", "Won't do", "Canceled", "Duplicate" are closes, but not wins.
  const EXCLUDED = /not planned|won['’]?t|cancel|duplicate|rejected|declined|obsolete/i;
  const INPUT_WINDOW_MS = 8000;
  const COOLDOWN_MS = 3000;
  const RECHECK_DELAYS = [150, 400, 900, 1600, 2600, 4000, 6000, 8000];
  const PERSIST_KEY = 'gangsta-closer:items';
  const PERSIST_TTL_MS = 15000;
  // No real drag or press lasts this long; if we think one does, we missed its end event.
  const MAX_GESTURE_MS = 15000;

  let doneRe = buildDoneRe([]);
  let debug = false;

  function escapeRe(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

  function buildDoneRe(extra) {
    const words = [...BUILTIN_DONE, ...extra].map((w) => escapeRe(w.trim().toLowerCase())).filter(Boolean);
    // Match the leading word(s) of a state/column name: "Done", "DONE 12", "✅ Done", "Closed".
    return new RegExp(`^[^\\p{L}\\p{N}]*(?:${words.join('|')})(?![\\p{L}])`, 'iu');
  }

  function normalize(s) { return (s || '').replace(/\s+/g, ' ').trim(); }

  function isDone(state) {
    const s = normalize(state);
    return !!s && doneRe.test(s) && !EXCLUDED.test(s);
  }

  function log(...args) {
    if (!debug) return;
    const text = args.map((a) => (typeof a === 'string' ? a : JSON.stringify(a))).join(' ');
    console.log(`%c[gangsta-closer]%c ${text}`, 'color:#d4a017;font-weight:bold', '');
  }

  function loadSettings(onLoaded) {
    try {
      chrome.storage.sync.get({ extraDone: '', debug: false }, (s) => {
        doneRe = buildDoneRe((s.extraDone || '').split(','));
        debug = !!s.debug;
        onLoaded();
      });
      chrome.storage.onChanged.addListener((changes, area) => {
        if (area !== 'sync') return;
        if (changes.extraDone) doneRe = buildDoneRe((changes.extraDone.newValue || '').split(','));
        if (changes.debug) debug = !!changes.debug.newValue;
      });
    } catch (_) { /* extension context gone */ }
  }

  function start(adapter) {
    let version = '?';
    try { version = chrome.runtime.getManifest().version; } catch (_) {}
    loadSettings(() => log(`watching ${adapter.site}, v${version}`, Object.fromEntries(baseline)));

    let baseline = new Map(); // key -> state
    let url = location.href;
    let lastInput = 0;
    let lastPlay = 0;
    let pointerDown = false;
    let dragging = false;
    let gestureStartedAt = 0;
    let timers = [];

    function snapshot() {
      const map = new Map();
      let entries = [];
      try { entries = adapter.snapshot() || []; } catch (e) { log('adapter error', String(e)); }
      for (const { key, state } of entries) {
        const s = normalize(state);
        if (key && s && !map.has(key)) map.set(key, s);
      }
      return map;
    }

    function check(reason) {
      if (pointerDown || dragging) { // compare only once the gesture is finished
        if (Date.now() - gestureStartedAt < MAX_GESTURE_MS) {
          if (reason === 'timer') log('check skipped, gesture in progress', { pointerDown, dragging });
          return;
        }
        log('gesture never ended, resetting', { pointerDown, dragging });
        pointerDown = dragging = false;
      }
      if (location.href !== url) {
        // New page/view: start a fresh baseline. Items carry over (same issue, new URL
        // params), cards don't (a different board has different columns).
        url = location.href;
        baseline = new Map([...baseline].filter(([k]) => k.startsWith('item:')));
        log('new page', location.pathname);
      }
      const now = snapshot();
      let hit = null;
      for (const [key, state] of now) {
        const before = baseline.get(key);
        if (before === undefined) { if (key.startsWith('item:')) log('tracking', key, state); continue; }
        if (before === state) continue;
        log(`change (${reason})`, key, `${before} → ${state}`);
        if (!isDone(before) && isDone(state)) hit = { key, before, state };
      }
      // Keep last-known state for keys that scrolled out of view (virtualized boards).
      for (const [k, v] of baseline) if (!now.has(k)) now.set(k, v);
      baseline = now;
      persist();

      if (!hit) return;
      const sinceInput = Date.now() - lastInput;
      const what = `${hit.key}: ${hit.before} → ${hit.state}`;
      if (sinceInput > INPUT_WINDOW_MS) { log(`ignored, no user input in the last ${INPUT_WINDOW_MS / 1000}s:`, what); return; }
      if (Date.now() - lastPlay < COOLDOWN_MS) { log('ignored, cooldown:', what); return; }
      lastPlay = Date.now();
      log(`CLOSED (${reason}) ${what}`);
      play(hit);
    }

    function play(hit) {
      try {
        chrome.runtime.sendMessage({ type: 'gangsta:play', site: adapter.site, detail: hit }, () => void chrome.runtime.lastError);
      } catch (_) { /* extension was reloaded; this tab needs a refresh */ }
      document.documentElement.dataset.gangstaCloserPlays = String(Number(document.documentElement.dataset.gangstaCloserPlays || 0) + 1);
    }

    function scheduleRechecks() {
      timers.forEach(clearTimeout);
      timers = RECHECK_DELAYS.map((d) => setTimeout(() => check('timer'), d));
    }

    // Survive full page reloads (e.g. a form POST that closes a PR) within one tab.
    function persist() {
      try {
        const items = [...baseline].filter(([k]) => k.startsWith('item:'));
        sessionStorage.setItem(PERSIST_KEY, JSON.stringify({ t: Date.now(), lastInput, items }));
      } catch (_) {}
    }
    function restore() {
      try {
        const saved = JSON.parse(sessionStorage.getItem(PERSIST_KEY) || 'null');
        if (saved && Date.now() - saved.t < PERSIST_TTL_MS) {
          baseline = new Map(saved.items);
          lastInput = saved.lastInput || 0;
        }
      } catch (_) {}
    }

    // Baseline right before the input, then mark the input.
    function beforeInput(e) {
      if (!e.isTrusted) return;
      check('pre-input');
      lastInput = Date.now();
      persist();
    }
    function afterInput(e) {
      if (!e.isTrusted) return;
      lastInput = Date.now();
      scheduleRechecks();
    }

    window.addEventListener('pointerdown', (e) => { dragging = false; beforeInput(e); pointerDown = true; gestureStartedAt = Date.now(); }, true);
    window.addEventListener('pointerup', (e) => { pointerDown = false; afterInput(e); }, true);
    window.addEventListener('pointercancel', () => { pointerDown = false; }, true);
    // Chrome sends no pointer events during a native drag, so any pointer movement
    // means there's no native drag in flight (and whether a button is still held).
    window.addEventListener('pointermove', (e) => { dragging = false; if (!e.buttons) pointerDown = false; }, true);
    window.addEventListener('dragstart', (e) => {
      if (!e.isTrusted) return;
      dragging = true;
      lastInput = gestureStartedAt = Date.now();
      // Apps with their own drag-and-drop (Linear) cancel the native drag, and a
      // cancelled drag never fires dragend. Check once the event has been handled.
      setTimeout(() => {
        log('native dragstart', e.defaultPrevented ? '(cancelled by page)' : '');
        if (e.defaultPrevented) dragging = false;
      }, 0);
    }, true);
    window.addEventListener('drop', (e) => { dragging = false; afterInput(e); }, true);
    window.addEventListener('dragend', (e) => { dragging = false; afterInput(e); }, true);
    window.addEventListener('keydown', (e) => { beforeInput(e); }, true);
    window.addEventListener('keyup', afterInput, true);
    // A drag that ends outside the window never delivers pointerup.
    window.addEventListener('blur', () => { pointerDown = false; dragging = false; });

    // DOM changes shortly after input trigger a (throttled) re-check.
    let pending = false;
    new MutationObserver(() => {
      if (pending || Date.now() - lastInput > INPUT_WINDOW_MS) return;
      pending = true;
      setTimeout(() => { pending = false; check('mutation'); }, 120);
    }).observe(document.documentElement, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: adapter.watchAttributes || ['class', 'title', 'aria-label'] });

    document.documentElement.dataset.gangstaCloser = adapter.site;
    restore();
    check('load');

    window.GangstaCloser.debugSnapshot = () => Object.fromEntries(snapshot());
  }

  window.GangstaCloser = { start, isDone, normalize };
})();
