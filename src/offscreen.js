// Plays the clip. A fresh Audio element per play lets rapid closes overlap cleanly.
// Offscreen documents only get chrome.runtime, so the service worker sends the clip itself.
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type !== 'gangsta:offscreen-play') return;
  const volume = Math.max(0, Math.min(1, Number(msg.volume ?? 0.8)));
  if (!msg.clip) {
    chime(volume).then(() => sendResponse({ ok: true, chime: true }), (e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  }
  const audio = new Audio(msg.clip);
  audio.volume = volume;
  audio.play().then(
    () => sendResponse({ ok: true }),
    (e) => sendResponse({ ok: false, error: String(e) }),
  );
  return true;
});

// Built-in sound for when no clip has been chosen: a short rising arpeggio, synthesized.
async function chime(volume) {
  const ctx = new AudioContext();
  await ctx.resume();
  const t0 = ctx.currentTime;
  [523.25, 659.25, 783.99, 1046.5].forEach((freq, i) => {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    const t = t0 + i * 0.09;
    osc.type = 'triangle';
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0, t);
    gain.gain.linearRampToValueAtTime(0.35 * volume, t + 0.01);
    gain.gain.exponentialRampToValueAtTime(0.001, t + 0.45);
    osc.connect(gain).connect(ctx.destination);
    osc.start(t);
    osc.stop(t + 0.5);
  });
  setTimeout(() => ctx.close(), 1200);
}
