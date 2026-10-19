const DEFAULTS = { enabled: true, volume: 0.8, sites: { github: true, linear: true, jira: true }, extraDone: '', debug: false };
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
  $('#volume').oninput = (e) => chrome.storage.sync.set({ volume: Number(e.target.value) });
  $('#extraDone').onchange = (e) => chrome.storage.sync.set({ extraDone: e.target.value });
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
