/* Share target handler */
(async function () {
  const params = new URLSearchParams(location.search);
  const token  = params.get('token');

  if (!token) {
    showError();
    return;
  }

  let attempts = 0;
  const MAX = 30;

  const timer = setInterval(async () => {
    attempts++;
    try {
      const res = await fetch(`/api/share/pending?token=${encodeURIComponent(token)}`);
      if (res.ok) {
        const data = await res.json();
        clearInterval(timer);
        showChoice(data.note_id);
        return;
      }
    } catch {}
    if (attempts >= MAX) {
      clearInterval(timer);
      showError();
    }
  }, 1000);

  function showError() {
    document.getElementById('loading').style.display = 'none';
    document.getElementById('error').style.display   = 'block';
  }

  function showChoice(noteId) {
    document.getElementById('loading').style.display = 'none';
    document.getElementById('choice').style.display  = 'flex';

    document.getElementById('btn-open').addEventListener('click', () => {
      location.replace(`/?note=${encodeURIComponent(noteId)}`);
    });

    document.getElementById('btn-journal').addEventListener('click', async () => {
      document.getElementById('btn-open').disabled    = true;
      document.getElementById('btn-journal').disabled = true;
      document.getElementById('rewrite-status').textContent = 'Rewriting as journal entry…';
      try {
        const r = await fetch(`/api/notes/${noteId}/rewrite-journal`, { method: 'POST' });
        if (!r.ok) throw new Error('rewrite failed');
        location.replace(`/?note=${encodeURIComponent(noteId)}`);
      } catch {
        document.getElementById('rewrite-status').textContent =
          'Rewrite failed — opening original note.';
        setTimeout(() => location.replace(`/?note=${encodeURIComponent(noteId)}`), 2000);
      }
    });
  }
})();
