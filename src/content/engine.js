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
//   Done column and dropping it elsewhere doesn't count. A board card only counts
//   when the input touched it, so a teammate's card moving while you type doesn't.
(() => {
  // A copy left behind by an extension reload or update can't play any more; let the new one run.
  if (window.GangstaCloser?.alive?.()) return;

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
  // How long a key that left the screen keeps its last state. Cards scroll out of view on
  // virtualized boards mid-drag; the viewed item only blinks out while its header re-renders.
  // Any longer and a teammate's change made while it was gone would count as yours.
  const CARD_CARRY_MS = MAX_GESTURE_MS;
  const ITEM_CARRY_MS = 3000;

  function alive() {
    try { return !!chrome.runtime?.id; } catch (_) { return false; }
  }

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

  let started = false;

  function start(adapter) {
    if (started) return; // injected twice (a tab loading while the extension installs)
    started = true;
    let version = '?';
    try { version = chrome.runtime.getManifest().version; } catch (_) {}
    loadSettings(() => log(`watching ${adapter.site}, v${version}`, Object.fromEntries(baseline)));

    let baseline = new Map(); // key -> state
    let missingSince = new Map(); // key -> when it left the screen (kept in baseline meanwhile)
    let elements = new Map(); // card key -> its element, from the latest snapshot
    const touched = new Map(); // card key -> when an input last landed on it
    let lastCard = null; // the card most recently touched
    let url = location.href;
    let lastInput = 0;
    let lastPlay = 0;
    let pointerDown = false;
    let dragging = false;
    let gestureStartedAt = 0;
    let timers = [];
    let observer = null;
    const listeners = [];

    function snapshot() {
      const map = new Map();
      const els = new Map();
      let entries = [];
      try { entries = adapter.snapshot() || []; } catch (e) { log('adapter error', String(e)); }
      for (const { key, state, el } of entries) {
        const s = normalize(state);
        if (key && s && !map.has(key)) {
          map.set(key, s);
          if (el) els.set(key, el);
        }
      }
      elements = els;
      return map;
    }

    // The extension was reloaded or updated: this copy can't reach it any more. Stop, and say so.
    function retire() {
      timers.forEach(clearTimeout);
      observer?.disconnect();
      for (const [type, fn, capture] of listeners) window.removeEventListener(type, fn, capture);
      listeners.length = 0;
      console.warn('[gangsta-closer] The extension was reloaded or updated. Refresh this tab if closes stop playing.');
    }

    function check(reason) {
      if (!alive()) return retire();
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
      const hits = [];
      // A key that left the screen keeps its last state, but not for long: coming back to an item
      // or card a teammate closed meanwhile must not count as your close.
      const t = Date.now();
      const stale = (k) => missingSince.has(k) && t - missingSince.get(k) > (k.startsWith('item:') ? ITEM_CARRY_MS : CARD_CARRY_MS);
      for (const [key, state] of now) {
        const before = stale(key) ? undefined : baseline.get(key);
        if (before === undefined) { if (key.startsWith('item:')) log('tracking', key, state); continue; }
        if (before === state) continue;
        log(`change (${reason})`, key, `${before} → ${state}`);
        if (!isDone(before) && isDone(state)) hits.push({ key, before, state });
      }
      // Carry the rest over while they're briefly off screen (virtualized boards, a re-rendering header).
      for (const k of now.keys()) missingSince.delete(k);
      for (const [k, v] of baseline) {
        if (now.has(k)) continue;
        if (!missingSince.has(k)) missingSince.set(k, t);
        if (stale(k)) missingSince.delete(k);
        else now.set(k, v);
      }
      baseline = now;
      persist();

      if (!hits.length) return;
      const describe = (h) => `${h.key}: ${h.before} → ${h.state}`;
      const sinceInput = Date.now() - lastInput;
      if (sinceInput > INPUT_WINDOW_MS) { log(`ignored, no user input in the last ${INPUT_WINDOW_MS / 1000}s:`, hits.map(describe)); return; }
      // The viewed item is the one you're acting on; a board card has to be the one you touched.
      const hit = hits.find((h) => !h.key.startsWith('card:') || Date.now() - (touched.get(h.key) || 0) <= INPUT_WINDOW_MS);
      if (!hit) { log('ignored, not a card you touched:', hits.map(describe)); return; }
      const what = describe(hit);
      if (Date.now() - lastPlay < COOLDOWN_MS) { log('ignored, cooldown:', what); return; }
      lastPlay = Date.now();
      log(`CLOSED (${reason}) ${what}`);
      play(hit);
    }

    function play(hit) {
      try {
        chrome.runtime.sendMessage({ type: 'gangsta:play', site: adapter.site, detail: hit }, () => void chrome.runtime.lastError);
      } catch (_) { return retire(); }
      document.documentElement.dataset.gangstaCloserPlays = String(Number(document.documentElement.dataset.gangstaCloserPlays || 0) + 1);
    }

    function scheduleRechecks() {
      timers.forEach(clearTimeout);
      timers = RECHECK_DELAYS.map((d) => setTimeout(() => check('timer'), d));
    }

    // Survive full page reloads (e.g. a form POST that closes a PR) within one tab.
    function persist() {
      try {
        const items = [...baseline].filter(([k]) => k.startsWith('item:')).map(([k, v]) => [k, v, missingSince.get(k) || 0]);
        sessionStorage.setItem(PERSIST_KEY, JSON.stringify({ t: Date.now(), lastInput, items }));
      } catch (_) {}
    }
    function restore() {
      try {
        // Only a page loaded by the close itself (a form POST, a link) carries the baseline over.
        // A reload or Back would compare against a state the page showed before a teammate's change.
        const nav = performance.getEntriesByType('navigation')[0];
        if (nav && nav.type !== 'navigate') return;
        const saved = JSON.parse(sessionStorage.getItem(PERSIST_KEY) || 'null');
        if (saved && Date.now() - saved.t < PERSIST_TTL_MS) {
          baseline = new Map(saved.items.map(([k, v]) => [k, v]));
          missingSince = new Map(saved.items.filter(([, , m]) => m).map(([k, , m]) => [k, m]));
          lastInput = saved.lastInput || 0;
        }
      } catch (_) {}
    }

    // Remember which cards the input landed on: the card itself, or for a keyboard shortcut the
    // focused or hovered card. Input that lands on no card (the side panel or menu a card opened)
    // keeps counting for the card touched last.
    function markTouched(e) {
      const targets = [e.target];
      if (e.type === 'keydown') targets.push(document.activeElement, [...document.querySelectorAll(':hover')].pop());
      const nodes = targets.filter((n) => n instanceof Node);
      let hitCard = false;
      for (const [key, el] of elements) {
        if (nodes.some((n) => el.contains(n))) {
          touched.set(key, Date.now());
          lastCard = key;
          hitCard = true;
        }
      }
      if (!hitCard && lastCard) touched.set(lastCard, Date.now());
    }

    // Baseline right before the input, then mark the input.
    function beforeInput(e) {
      if (!e.isTrusted) return;
      check('pre-input');
      markTouched(e);
      lastInput = Date.now();
      persist();
    }
    function afterInput(e) {
      if (!e.isTrusted) return;
      lastInput = Date.now();
      scheduleRechecks();
    }

    function on(type, fn, capture = true) {
      window.addEventListener(type, fn, capture);
      listeners.push([type, fn, capture]);
    }

    on('pointerdown', (e) => { dragging = false; beforeInput(e); pointerDown = true; gestureStartedAt = Date.now(); });
    on('pointerup', (e) => { pointerDown = false; afterInput(e); });
    on('pointercancel', () => { pointerDown = false; });
    // Chrome sends no pointer events during a native drag, so any pointer movement
    // means there's no native drag in flight (and whether a button is still held).
    on('pointermove', (e) => { dragging = false; if (!e.buttons) pointerDown = false; });
    on('dragstart', (e) => {
      if (!e.isTrusted) return;
      dragging = true;
      markTouched(e);
      lastInput = gestureStartedAt = Date.now();
      // Apps with their own drag-and-drop (Linear) cancel the native drag, and a
      // cancelled drag never fires dragend. Check once the event has been handled.
      setTimeout(() => {
        log('native dragstart', e.defaultPrevented ? '(cancelled by page)' : '');
        if (e.defaultPrevented) dragging = false;
      }, 0);
    });
    on('drop', (e) => { dragging = false; afterInput(e); });
    on('dragend', (e) => { dragging = false; afterInput(e); });
    on('keydown', (e) => { beforeInput(e); });
    on('keyup', afterInput);
    // A drag that ends outside the window never delivers pointerup.
    on('blur', () => { pointerDown = false; dragging = false; }, false);

    // DOM changes shortly after input trigger a (throttled) re-check.
    let pending = false;
    observer = new MutationObserver(() => {
      if (pending || Date.now() - lastInput > INPUT_WINDOW_MS) return;
      pending = true;
      setTimeout(() => { pending = false; check('mutation'); }, 120);
    });
    observer.observe(document.documentElement, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: adapter.watchAttributes || ['class', 'title', 'aria-label'] });

    document.documentElement.dataset.gangstaCloser = adapter.site;
    restore();
    check('load');

    window.GangstaCloser.debugSnapshot = () => Object.fromEntries(snapshot());
  }

  window.GangstaCloser = { start, isDone, normalize, alive };
})();
