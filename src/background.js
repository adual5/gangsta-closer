// Service worker: receives "play" requests from content scripts / popup and plays the
// clip in an offscreen document, so page CSP and autoplay rules can't interfere.
const DEFAULTS = {
  enabled: true,
  volume: 0.8,
  sites: { github: true, linear: true, jira: true, asana: true },
  extraDone: '',
  debug: false,
};

let creating = null;

async function ensureOffscreen() {
  const existing = await chrome.runtime.getContexts({ contextTypes: ['OFFSCREEN_DOCUMENT'] });
  if (existing.length) return;
  if (!creating) {
    creating = chrome.offscreen
      .createDocument({ url: 'src/offscreen.html', reasons: ['AUDIO_PLAYBACK'], justification: 'Play the close sound effect' })
      .finally(() => { creating = null; });
  }
  await creating;
}

async function play({ site, test, detail }) {
  const s = await chrome.storage.sync.get(DEFAULTS);
  if (!test && (!s.enabled || !s.sites?.[site])) return { played: false };
  await ensureOffscreen();
  const { clip } = await chrome.storage.local.get('clip');
  const audio = await chrome.runtime.sendMessage({ type: 'gangsta:offscreen-play', volume: s.volume, clip: clip?.dataUrl });
  if (!test) {
    const { closes = 0 } = await chrome.storage.local.get('closes');
    await chrome.storage.local.set({ closes: closes + 1, lastClose: { site, detail, audio, at: Date.now() } });
  }
  return { played: true, audio };
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type !== 'gangsta:play') return;
  play(msg).then(sendResponse, (e) => sendResponse({ played: false, error: String(e) }));
  return true;
});

// Chrome only injects manifest content scripts into pages loaded after install, and an update
// or reload cuts off the copies already running. Inject into the tabs that are already open.
async function injectIntoOpenTabs() {
  for (const { matches, js } of chrome.runtime.getManifest().content_scripts || []) {
    const tabs = await chrome.tabs.query({ url: matches }).catch(() => []);
    for (const tab of tabs) {
      if (tab.discarded) continue;
      chrome.scripting.executeScript({ target: { tabId: tab.id }, files: js }).catch(() => {});
    }
  }
}

chrome.runtime.onInstalled.addListener(async ({ reason }) => {
  if (reason === 'install') chrome.runtime.openOptionsPage();
  if (reason === 'install' || reason === 'update') injectIntoOpenTabs();
  const current = await chrome.storage.sync.get(null);
  await chrome.storage.sync.set({ ...DEFAULTS, ...current, sites: { ...DEFAULTS.sites, ...current.sites } });
});
