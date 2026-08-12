/* Per-target page: tabs, persona editing, schedules, compose, test send. */

/* ── tabs ───────────────────────────────────────────────────────────────── */

document.querySelectorAll('.pane-btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    const name = btn.dataset.pane;
    document.querySelectorAll('.pane-btn').forEach((b) => {
      const on = b === btn;
      b.className = `pane-btn px-4 py-2.5 text-sm font-medium border-b-2 -mb-px whitespace-nowrap ${
        on ? 'border-accent text-accent' : 'border-transparent text-muted hover:text-ink'}`;
    });
    document.querySelectorAll('[data-pane-body]').forEach((pane) => {
      pane.classList.toggle('hidden', pane.dataset.paneBody !== name);
    });
    if (name === 'history') loadLogs();
    if (name === 'schedule') loadSchedules();
    location.hash = name;
  });
});

if (location.hash) {
  const btn = document.querySelector(`.pane-btn[data-pane="${location.hash.slice(1)}"]`);
  if (btn) btn.click();
}

/* ── overview ───────────────────────────────────────────────────────────── */

async function loadOverview() {
  try {
    const rows = await api.get('/api/targets/summary');
    const mine = rows.find((r) => r.id === TARGET_ID);
    if (mine) {
      document.getElementById('ov-next').textContent = mine.next_run_label || 'Not scheduled';
      document.getElementById('ov-last').textContent = mine.last_sent_label || 'Never';
    }
    const logs = await api.get(`/api/logs?target_id=${TARGET_ID}&status=sent&limit=500`);
    document.getElementById('ov-count').textContent = logs.length;
  } catch (err) { /* non-fatal */ }
}

document.getElementById('t-enabled').addEventListener('change', async (e) => {
  try {
    await api.patch(`/api/targets/${TARGET_ID}`, { enabled: e.target.checked });
    showBanner(e.target.checked ? 'Target enabled.' : 'Target paused.', 'success');
  } catch (err) { showBanner(err.message); e.target.checked = !e.target.checked; }
});

/* ── compose now ────────────────────────────────────────────────────────── */

document.getElementById('btn-compose').addEventListener('click', async (e) => {
  const out = document.getElementById('compose-result');
  e.target.disabled = true;
  e.target.textContent = 'Researching…';
  out.innerHTML = '<div class="text-sm text-muted">Searching the web, then drafting. This takes 10–40 seconds.</div>';
  try {
    const res = await api.post(`/api/targets/${TARGET_ID}/compose-now`, {
      content_type: document.getElementById('compose-type').value,
    });
    if (res.status === 'sent') {
      out.innerHTML = `<div class="rounded-xl border border-ok/20 bg-ok/5 px-4 py-3 text-sm text-ok">
        Sent straight to WhatsApp (approval is off for this target).</div>`;
    } else {
      out.innerHTML = `<div class="rounded-xl border border-accent/20 bg-accent/5 px-4 py-3 text-sm">
        <div class="font-medium text-accent">Draft ready for approval</div>
        <div class="text-muted mt-1">Used ${res.sources} source${res.sources === 1 ? '' : 's'}.</div>
        <a href="#pending" class="text-accent hover:underline mt-1 inline-block"
           onclick="document.querySelector('.pane-btn[data-pane=pending]').click(); setTimeout(()=>location.reload(),50)">
          Review it →</a></div>`;
    }
  } catch (err) {
    out.innerHTML = `<div class="rounded-xl border border-danger/20 bg-danger/5 px-4 py-3 text-sm text-danger">
      ${escapeHtml(err.message)}</div>`;
  } finally {
    e.target.disabled = false;
    e.target.textContent = 'Compose now';
  }
});

/* ── test send ──────────────────────────────────────────────────────────── */

document.getElementById('btn-test').addEventListener('click', async (e) => {
  const text = document.getElementById('test-text').value.trim();
  if (!text) { showBanner('Type something to send.'); return; }
  e.target.disabled = true;
  e.target.textContent = 'Sending…';
  try {
    await api.post(`/api/targets/${TARGET_ID}/send-test`, { text });
    showBanner('Test message sent. Check WhatsApp.', 'success');
    loadOverview();
  } catch (err) {
    showBanner(err.message);
  } finally {
    e.target.disabled = false;
    e.target.textContent = 'Send test message';
  }
});

/* ── persona ────────────────────────────────────────────────────────────── */

(async () => {
  const select = document.getElementById('p-model');
  const target = await api.get(`/api/targets/${TARGET_ID}`);
  try {
    const data = await api.get('/api/settings/models');
    select.innerHTML = '<option value="">Use app default</option>';
    data.models.forEach((m) => {
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = m.id;
      if (m.id === target.model_override) opt.selected = true;
      select.appendChild(opt);
    });
  } catch (err) {
    select.innerHTML = '<option value="">Use app default</option>';
  }
})();

document.getElementById('btn-save-persona').addEventListener('click', async (e) => {
  const examples = document.getElementById('p-examples').value
    .split(/\n\s*\n/).map((s) => s.trim()).filter(Boolean);
  e.target.disabled = true;
  try {
    await api.patch(`/api/targets/${TARGET_ID}`, {
      name: document.getElementById('p-name').value.trim(),
      niche: document.getElementById('p-niche').value.trim(),
      persona_prompt: document.getElementById('p-persona').value,
      research_instructions: document.getElementById('p-research').value,
      tone: document.getElementById('p-tone').value.trim(),
      language: document.getElementById('p-language').value,
      cta_link: document.getElementById('p-cta').value.trim(),
      banned_topics: document.getElementById('p-banned').value.trim(),
      example_messages: examples,
      model_override: document.getElementById('p-model').value || null,
      disclaimer_mode: document.getElementById('p-disclaimer').value,
      approval_required: document.getElementById('p-approval').checked,
    });
    const flag = document.getElementById('persona-saved');
    flag.classList.remove('hidden');
    setTimeout(() => flag.classList.add('hidden'), 2500);
  } catch (err) { showBanner(err.message); }
  finally { e.target.disabled = false; }
});

/* ── schedules ──────────────────────────────────────────────────────────── */

async function loadSchedules() {
  const host = document.getElementById('sched-list');
  try {
    const rows = await api.get(`/api/schedules?target_id=${TARGET_ID}`);
    if (!rows.length) {
      host.innerHTML = '<p class="text-sm text-muted">No schedules yet — this target only runs when you hit Compose now.</p>';
      return;
    }
    host.innerHTML = rows.map((r) => `
      <div class="flex items-center justify-between gap-4 rounded-xl border border-line px-4 py-3">
        <div class="min-w-0">
          <div class="text-sm font-medium">${escapeHtml(r.human)}</div>
          <div class="text-xs text-muted mt-0.5">
            <code>${escapeHtml(r.cron_expr)}</code> · ${escapeHtml(r.content_type)}
            ${r.next_run_label ? ` · next ${escapeHtml(r.next_run_label)}` : ' · inactive'}
          </div>
        </div>
        <div class="flex items-center gap-3 shrink-0">
          <label class="flex items-center gap-1.5 text-xs text-muted">
            <input type="checkbox" data-sched-active="${r.id}" ${r.active ? 'checked' : ''}
                   class="h-4 w-4 rounded border-line text-accent focus:ring-accent/30">active
          </label>
          <button data-sched-del="${r.id}" class="text-xs text-danger hover:underline">Delete</button>
        </div>
      </div>`).join('');

    host.querySelectorAll('[data-sched-active]').forEach((el) => {
      el.addEventListener('change', async () => {
        try { await api.patch(`/api/schedules/${el.dataset.schedActive}`, { active: el.checked }); loadSchedules(); }
        catch (err) { showBanner(err.message); }
      });
    });
    host.querySelectorAll('[data-sched-del]').forEach((el) => {
      el.addEventListener('click', async () => {
        try { await api.del(`/api/schedules/${el.dataset.schedDel}`); loadSchedules(); }
        catch (err) { showBanner(err.message); }
      });
    });
  } catch (err) {
    host.innerHTML = `<p class="text-sm text-danger">${escapeHtml(err.message)}</p>`;
  }
}

const sPreset = document.getElementById('s-preset');
const sCron = document.getElementById('s-cron');

function sChosen() {
  return sPreset.value === 'custom' ? sCron.value.trim() : sPreset.value;
}

async function sPreview() {
  const out = document.getElementById('s-preview');
  const expr = sChosen();
  if (!expr) { out.textContent = ''; return; }
  try {
    const p = await api.get(`/api/schedules/preview?cron_expr=${encodeURIComponent(expr)}`);
    out.innerHTML = `<span class="text-ink">${escapeHtml(p.human)}</span> · next: ${escapeHtml(p.next_runs[0] || '—')}`;
  } catch (err) {
    out.innerHTML = `<span class="text-danger">${escapeHtml(err.message)}</span>`;
  }
}

sPreset.addEventListener('change', () => {
  sCron.classList.toggle('hidden', sPreset.value !== 'custom');
  sPreview();
});
sCron.addEventListener('input', sPreview);

document.getElementById('btn-add-sched').addEventListener('click', async (e) => {
  const expr = sChosen();
  if (!expr) { showBanner('Pick or type a schedule.'); return; }
  e.target.disabled = true;
  try {
    await api.post('/api/schedules', {
      target_id: TARGET_ID,
      cron_expr: expr,
      content_type: document.getElementById('s-content').value,
      active: true,
    });
    await loadSchedules();
    loadOverview();
    showBanner('Schedule added.', 'success');
  } catch (err) { showBanner(err.message); }
  finally { e.target.disabled = false; }
});

/* ── history ────────────────────────────────────────────────────────────── */

async function loadLogs() {
  const host = document.getElementById('log-list');
  try {
    const rows = await api.get(`/api/logs?target_id=${TARGET_ID}&limit=100`);
    if (!rows.length) {
      host.innerHTML = '<p class="px-6 py-8 text-center text-sm text-muted">No sends yet.</p>';
      return;
    }
    host.innerHTML = `<table class="w-full text-sm">
      <thead class="bg-wash text-xs text-muted"><tr>
        <th class="text-left font-medium px-6 py-2.5">When</th>
        <th class="text-left font-medium px-3 py-2.5">Status</th>
        <th class="text-left font-medium px-3 py-2.5">Detail</th>
      </tr></thead>
      <tbody class="divide-y divide-line">${rows.map((r) => `
        <tr>
          <td class="px-6 py-3 whitespace-nowrap text-muted">${escapeHtml(r.created_label)}</td>
          <td class="px-3 py-3">
            <span class="text-[11px] px-2 py-0.5 rounded-full border ${
              r.status === 'sent' ? 'bg-ok/10 text-ok border-ok/20' : 'bg-danger/10 text-danger border-danger/20'}">
              ${escapeHtml(r.status)}</span>
          </td>
          <td class="px-3 py-3 text-muted">${escapeHtml(r.error || '—')}</td>
        </tr>`).join('')}</tbody></table>`;
  } catch (err) {
    host.innerHTML = `<p class="px-6 py-8 text-sm text-danger">${escapeHtml(err.message)}</p>`;
  }
}

/* ── delete ─────────────────────────────────────────────────────────────── */

document.getElementById('btn-delete').addEventListener('click', async (e) => {
  if (!confirm('Delete this target, its schedules, drafts and logs? Your WhatsApp group is not affected.')) return;
  e.target.disabled = true;
  try {
    await api.del(`/api/targets/${TARGET_ID}`);
    window.location.href = '/';
  } catch (err) { showBanner(err.message); e.target.disabled = false; }
});

loadOverview();
loadSchedules();
