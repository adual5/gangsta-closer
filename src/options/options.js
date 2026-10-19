// Lets the user pick their own clip. It is stored as a data URL in chrome.storage.local
// (10 MB quota), which the service worker hands to the offscreen player on each close.
const MAX_BYTES = 5 * 1024 * 1024;
const $ = (sel) => document.querySelector(sel);

function kb(bytes) {
  return bytes < 1024 * 1024 ? `${Math.round(bytes / 1024)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

async function render() {
  const { clip } = await chrome.storage.local.get('clip');
  $('#current').textContent = clip ? `${clip.name} (${kb(clip.size)})` : 'Built-in chime';
  $('#remove').hidden = !clip;
}

function showError(text) {
  $('#error').textContent = text;
  $('#error').hidden = !text;
}

$('#file').onchange = async (e) => {
  const file = e.target.files[0];
  e.target.value = '';
  showError('');
  if (!file) return;
  if (!file.type.startsWith('audio/')) return showError('That file is not audio.');
  if (file.size > MAX_BYTES) return showError(`That file is ${kb(file.size)}. The limit is 5 MB.`);
  const dataUrl = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
  const playable = await new Audio(dataUrl).play().then(() => true, () => false);
  if (!playable) return showError('Chrome could not play that file. Try an MP3.');
  await chrome.storage.local.set({ clip: { name: file.name, size: file.size, dataUrl } });
  render();
};

$('#remove').onclick = async () => {
  await chrome.storage.local.remove('clip');
  showError('');
  render();
};

$('#test').onclick = () => chrome.runtime.sendMessage({ type: 'gangsta:play', test: true });

render();
