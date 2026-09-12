import RFB from './core/rfb.js';

const screen = document.getElementById('screen');
const status = document.getElementById('status');
let current;
let attempt = 0;
let pending;

// URLs are relative to the authenticated session prefix supplied by the gateway.
// Neither a host/IP nor a credential can be selected through query parameters.
async function connect() {
  const generation = ++attempt;
  pending?.abort();
  current?.disconnect();
  pending = new AbortController();
  const abort = pending;
  status.textContent = 'Connecting…';
  const timeout = setTimeout(() => abort.abort(), 10000);
  try {
    const response = await fetch(new URL('./credentials', window.location.href), {
      credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: abort.signal,
      headers: { Accept: 'application/json' },
    });
    if (generation !== attempt) return;
    if (!response.ok) throw new Error('Desktop access expired or unavailable. Refresh the Lab workspace.');
    const credentials = await response.json();
    if (typeof credentials.password !== 'string' || !/^[\x21-\x7e]{8,128}$/.test(credentials.password)) {
      throw new Error('Desktop credentials are unavailable.');
    }
    if (generation !== attempt) return;
    const socket = new URL('./websockify', window.location.href);
    socket.protocol = socket.protocol === 'https:' ? 'wss:' : 'ws:';
    socket.search = '';
    socket.hash = '';
    const rfb = new RFB(screen, socket.href, { credentials: { password: credentials.password } });
    credentials.password = '';
    current = rfb;
    rfb.scaleViewport = true;
    rfb.resizeSession = true;
    rfb.qualityLevel = 6;
    rfb.compressionLevel = 2;
    rfb.addEventListener('connect', () => {
      if (generation === attempt) status.textContent = 'Connected';
    });
    rfb.addEventListener('disconnect', () => {
      if (generation === attempt) status.textContent = 'Disconnected. Reconnect to continue.';
    });
    rfb.addEventListener('securityfailure', () => {
      if (generation === attempt) status.textContent = 'Desktop authorization failed. Refresh the Lab workspace.';
    });
  } catch (error) {
    if (generation === attempt) status.textContent = error.name === 'AbortError'
      ? 'Connection timed out. Try reconnecting.' : (error.message || 'Desktop connection failed.');
  } finally {
    clearTimeout(timeout);
  }
}

document.getElementById('reconnect').addEventListener('click', connect);
document.getElementById('fullscreen').addEventListener('click', async () => {
  try { await document.documentElement.requestFullscreen(); } catch { /* Browser may deny. */ }
});
window.addEventListener('pagehide', () => { ++attempt; pending?.abort(); current?.disconnect(); });
connect();
