export function init() {
  const btn = document.getElementById('btn-test-telegram');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    btn.textContent = 'Testing…';
    try {
      await apiFetch('/api/settings/test-telegram', { method: 'POST' });
      toast('Telegram connected! Check your chat for the test message.', 'success');
    } catch (e) {
      toast('Test failed: ' + e.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Test Connection';
    }
  });
}
