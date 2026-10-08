const $ = id => document.getElementById(id);
const LOG_VIEWER_RUN_KEY = new URLSearchParams(window.location.search).get('run_key') || '';

document.addEventListener('DOMContentLoaded', async () => {
  $('log-viewer-refresh').addEventListener('click', () => loadLogs(true));
  $('log-viewer-clear').addEventListener('click', clearLogs);
  await loadLogs();
  window.setInterval(() => loadLogs(), 5000);
});

async function loadLogs(showError = false) {
  try {
    const wasNearBottom = window.innerHeight + window.scrollY >= document.body.offsetHeight - 120;
    const params = new URLSearchParams({ _: String(Date.now()) });
    if (LOG_VIEWER_RUN_KEY) params.set('run_key', LOG_VIEWER_RUN_KEY);
    const response = await fetch(`/api/pipeline/logs?${params.toString()}`, { cache: 'no-store' });
    const data = await response.json();
    if (!response.ok || data.error) {
      throw new Error(data.error || 'Failed to load logs');
    }
    $('log-viewer-output').textContent = data.content || 'No logs yet.';
    $('log-viewer-meta').textContent = `${data.log_path || ''}${data.size != null ? ` | ${data.size} bytes` : ''}${data.truncated ? ' | showing latest tail' : ''}`;
    $('log-viewer-banner').className = 'auth-status ok';
    $('log-viewer-banner').innerHTML = '<span class="dot"></span> Log viewer connected';
    if (wasNearBottom) {
      window.scrollTo({ top: document.body.scrollHeight, behavior: 'auto' });
    }
  } catch (e) {
    $('log-viewer-banner').className = 'auth-status error';
    $('log-viewer-banner').innerHTML = '<span class="dot"></span> Log viewer error';
    if (showError) {
      alert(e.message);
    }
  }
}

async function clearLogs() {
  if (!window.confirm('Clear the pipeline log file?')) {
    return;
  }
  const query = LOG_VIEWER_RUN_KEY ? `?run_key=${encodeURIComponent(LOG_VIEWER_RUN_KEY)}` : '';
  const response = await fetch(`/api/pipeline/logs${query}`, {
    method: 'DELETE',
  });
  const data = await response.json();
  if (!response.ok || data.error) {
    alert(data.error || 'Failed to clear logs');
    return;
  }
  await loadLogs(true);
}
