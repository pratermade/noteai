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
        if (data.type === 'image_pending') {
          showImagePicker(token, data);
        } else {
          location.replace(`/?note=${encodeURIComponent(data.note_id)}`);
        }
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

  function showImagePicker(tok, data) {
    document.getElementById('loading').style.display = 'none';
    const section = document.getElementById('image-pending');
    section.style.display = 'block';

    const preview = document.getElementById('image-preview');
    preview.src = `/api/share/preview?token=${encodeURIComponent(tok)}`;

    document.getElementById('btn-new-note').addEventListener('click', () => finalize(tok, 'new'));

    document.getElementById('btn-attach').addEventListener('click', () => {
      const ns = document.getElementById('note-search');
      ns.style.display = 'block';
      document.getElementById('note-search-input').focus();
      loadNotes();
    });

    let allNotes = [];
    let notesLoaded = false;

    async function loadNotes() {
      if (notesLoaded) return;
      const jwt = localStorage.getItem('auth_token');
      if (!jwt) {
        document.getElementById('attach-status').textContent = 'Not logged in — open the app first.';
        return;
      }
      try {
        const res = await fetch('/api/notes', {
          headers: { 'Authorization': 'Bearer ' + jwt }
        });
        if (!res.ok) throw new Error('fetch failed');
        allNotes = await res.json();
        notesLoaded = true;
        renderResults('');
      } catch {
        document.getElementById('attach-status').textContent = 'Could not load notes.';
      }
    }

    function renderResults(query) {
      const container = document.getElementById('note-search-results');
      const q = query.trim().toLowerCase();
      const filtered = q
        ? allNotes.filter(n => n.title && n.title.toLowerCase().includes(q))
        : allNotes.slice(0, 20);
      if (!filtered.length) {
        container.innerHTML = '<div class="note-result-empty">No notes found</div>';
        return;
      }
      container.innerHTML = filtered.slice(0, 20).map(n =>
        `<div class="note-result" data-id="${esc(n.id)}">${esc(n.title || 'Untitled')}</div>`
      ).join('');
      container.querySelectorAll('.note-result').forEach(el => {
        el.addEventListener('click', () => finalize(tok, 'attach', el.dataset.id));
      });
    }

    let debounceTimer;
    document.getElementById('note-search-input').addEventListener('input', e => {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => renderResults(e.target.value), 200);
    });
  }

  async function finalize(tok, action, noteId) {
    const jwt = localStorage.getItem('auth_token');
    setButtonsDisabled(true);
    document.getElementById('attach-status').textContent = 'Saving…';
    try {
      const body = { token: tok, action };
      if (noteId) body.note_id = noteId;
      const res = await fetch('/api/share/finalize', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': 'Bearer ' + jwt,
        },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error('finalize failed');
      const data = await res.json();
      location.replace(`/?note=${encodeURIComponent(data.note_id)}`);
    } catch {
      document.getElementById('attach-status').textContent = 'Save failed. Try again.';
      setButtonsDisabled(false);
    }
  }

  function setButtonsDisabled(disabled) {
    document.getElementById('btn-new-note').disabled = disabled;
    document.getElementById('btn-attach').disabled   = disabled;
  }

  function esc(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
})();
