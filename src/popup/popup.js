const DEFAULTS = { enabled: true, volume: 0.8, sites: { github: true, linear: true, jira: true, asana: true }, extraDone: '', debug: false };
const $ = (sel) => document.querySelector(sel);

(async () => {
  const s = await chrome.storage.sync.get(DEFAULTS);
  const { closes = 0, clip } = await chrome.storage.local.get(['closes', 'clip']);

  $('#closes').textContent = closes;
  if (clip) $('#clip').textContent = clip.name;
  $('#enabled').checked = s.enabled;
  $('#volume').value = s.volume;
  $('#extraDone').value = s.extraDone;
  $('#debug').checked = s.debug;
  for (const box of document.querySelectorAll('[data-site]')) box.checked = s.sites[box.dataset.site] !== false;

  $('#enabled').onchange = (e) => chrome.storage.sync.set({ enabled: e.target.checked });
  // storage.sync allows 120 writes a minute; one per slider step can exceed it and drop the last value.
  $('#volume').onchange = (e) => chrome.storage.sync.set({ volume: Number(e.target.value) }).catch(() => {});
  // The popup closes (Esc, click outside) without a change event, so save as you type. Debounced for
  // the storage.sync write limit, and flushed when the popup goes away.
  let typed = null;
  let timer = 0;
  const saveExtra = () => {
    clearTimeout(timer);
    if (typed === null) return;
    chrome.storage.sync.set({ extraDone: typed }).catch(() => {});
    typed = null;
  };
  $('#extraDone').oninput = (e) => { typed = e.target.value; clearTimeout(timer); timer = setTimeout(saveExtra, 400); };
  $('#extraDone').onchange = saveExtra;
  addEventListener('pagehide', saveExtra);
  document.addEventListener('visibilitychange', () => { if (document.hidden) saveExtra(); });
  $('#debug').onchange = (e) => chrome.storage.sync.set({ debug: e.target.checked });
  for (const box of document.querySelectorAll('[data-site]')) {
    box.onchange = async () => {
      const { sites } = await chrome.storage.sync.get(DEFAULTS);
      chrome.storage.sync.set({ sites: { ...sites, [box.dataset.site]: box.checked } });
    };
  }
  $('#choose').onclick = (e) => { e.preventDefault(); chrome.runtime.openOptionsPage(); };
  $('#test').onclick = () => chrome.runtime.sendMessage({ type: 'gangsta:play', test: true });
})();
