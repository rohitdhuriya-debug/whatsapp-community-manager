/* Shared front-end helpers. No framework, no build step. */

const api = {
  async request(method, url, body) {
    const opts = { method, headers: { 'Content-Type': 'application/json' } };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const res = await fetch(url, opts);
    let data = null;
    try { data = await res.json(); } catch (_) { /* empty body is fine */ }
    if (!res.ok) {
      const msg = (data && (data.detail || data.error || data.message)) || `HTTP ${res.status}`;
      throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    }
    return data;
  },
  get(url) { return this.request('GET', url); },
  post(url, body) { return this.request('POST', url, body); },
  patch(url, body) { return this.request('PATCH', url, body); },
  del(url) { return this.request('DELETE', url); },
};

/* ── Banner ─────────────────────────────────────────────────────────────── */

function showBanner(message, kind = 'error') {
  const host = document.getElementById('global-banner');
  if (!host) return;
  const palette = {
    error:   { bg: '#FEF2F2', border: '#FECACA', text: '#DC2626' },
    success: { bg: '#F0FDF4', border: '#BBF7D0', text: '#16A34A' },
    info:    { bg: '#EEF2FF', border: '#C7D2FE', text: '#4F46E5' },
  }[kind] || {};
  host.className = 'px-8 pt-6';
  host.innerHTML = `
    <div class="rounded-xl border px-4 py-3 text-sm flex items-start justify-between gap-4"
         style="background:${palette.bg};border-color:${palette.border};color:${palette.text}">
      <span>${escapeHtml(message)}</span>
      <button class="opacity-60 hover:opacity-100 shrink-0"
              onclick="document.getElementById('global-banner').className='hidden'">✕</button>
    </div>`;
  if (kind === 'success') {
    setTimeout(() => { host.className = 'hidden'; }, 4000);
  }
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/* ── WAHA status dot in the sidebar ─────────────────────────────────────── */

async function refreshWahaDot() {
  const dot = document.getElementById('waha-dot');
  const label = document.getElementById('waha-label');
  if (!dot || !label) return;
  try {
    const s = await api.get('/api/waha/status');
    const colour = s.ok ? '#16A34A' : (s.reachable ? '#D97706' : '#DC2626');
    dot.style.backgroundColor = colour;
    label.textContent = `WAHA: ${s.status}`;
    label.title = s.error || 'Session is connected and ready.';
  } catch (err) {
    dot.style.backgroundColor = '#DC2626';
    label.textContent = 'WAHA: unreachable';
    label.title = err.message;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  refreshWahaDot();
  setInterval(refreshWahaDot, 20000);
});
