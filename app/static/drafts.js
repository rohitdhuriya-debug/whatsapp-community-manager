/* Approve / edit / reject behaviour for every [data-draft] card. */

function wireDraftCard(card) {
  const draftId = card.dataset.draft;
  const editor = card.querySelector('[data-editor]');
  const preview = card.querySelector('[data-wa-preview]');

  const on = (action, handler) => {
    const btn = card.querySelector(`[data-action="${action}"]`);
    if (btn) btn.addEventListener('click', handler);
  };

  on('edit', () => editor.classList.toggle('hidden'));
  on('cancel-edit', () => editor.classList.add('hidden'));

  on('save', async (e) => {
    const body = { content: card.querySelector('[data-edit-text]').value };
    const opts = card.querySelector('[data-edit-options]');
    if (opts) body.poll_options = opts.value.split('\n').map((s) => s.trim()).filter(Boolean);

    e.target.disabled = true;
    try {
      const updated = await api.patch(`/api/drafts/${draftId}`, body);
      preview.textContent = updated.content;
      editor.classList.add('hidden');
      showBanner('Draft updated.', 'success');
    } catch (err) {
      showBanner(err.message);
    } finally {
      e.target.disabled = false;
    }
  });

  on('approve', async (e) => {
    e.target.disabled = true;
    e.target.textContent = 'Sending…';
    try {
      await api.post(`/api/drafts/${draftId}/approve`);
      card.style.transition = 'opacity .25s';
      card.style.opacity = '0.35';
      card.querySelector('footer').innerHTML =
        '<span class="text-sm font-medium text-ok">Sent to WhatsApp</span>';
      showBanner('Sent.', 'success');
    } catch (err) {
      showBanner(err.message);
      e.target.disabled = false;
      e.target.textContent = 'Approve & send';
    }
  });

  on('reject', async (e) => {
    e.target.disabled = true;
    try {
      await api.post(`/api/drafts/${draftId}/reject`);
      card.style.transition = 'opacity .25s';
      card.style.opacity = '0.35';
      card.querySelector('footer').innerHTML =
        '<span class="text-sm font-medium text-muted">Rejected</span>';
    } catch (err) {
      showBanner(err.message);
      e.target.disabled = false;
    }
  });
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('[data-draft]').forEach(wireDraftCard);
});
