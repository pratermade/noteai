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
        location.replace(`/?note=${encodeURIComponent(data.note_id)}`);
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
})();
