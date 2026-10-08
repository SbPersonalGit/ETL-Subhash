const $ = id => document.getElementById(id);
const RUN_KEY = document.body.dataset.runKey || '';
let currentRunner = {};
let currentPipelines = {};

document.addEventListener('DOMContentLoaded', async () => {
  $('run-progress-refresh').addEventListener('click', () => refreshRun(true));
  $('run-progress-stop').addEventListener('click', stopPipelineRun);
  await refreshRun();
  window.setInterval(() => refreshRun(), 5000);
});

async function refreshRun(showError = false) {
  try {
    await Promise.all([loadStatus(), loadLogs()]);
  } catch (error) {
    setBanner('error', `Unable to load run: ${error.message}`);
    if (showError) alert(error.message);
  }
}

async function readJsonResponse(response) {
  const text = await response.text();
  try {
    return JSON.parse(text);
  } catch (_error) {
    const plain = text.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
    throw new Error(
      plain
        ? `API returned HTML/non-JSON (HTTP ${response.status}): ${plain.slice(0, 240)}`
        : `Server returned HTTP ${response.status} instead of JSON.`
    );
  }
}

async function loadStatus() {
  const query = new URLSearchParams({ run_key: RUN_KEY, _: String(Date.now()) });
  const response = await fetch(`/api/run-status?${query.toString()}`, { cache: 'no-store' });
  const data = await readJsonResponse(response);
  if (!response.ok || data.error) throw new Error(data.error || 'Failed to load run status');

  const runner = data.runner || {};
  currentRunner = runner;
  $('run-progress-stop').disabled = !runner.running;
  const pipelines = data.pipelines || {};
  currentPipelines = pipelines;
  const runStatus = deriveRunStatus(runner, pipelines);
  $('run-progress-title').textContent = data.migration || 'Pipeline run';
  $('run-progress-meta').textContent = [
    `Run key: ${RUN_KEY}`,
    data.started_at ? `Started: ${formatTimestamp(data.started_at)}` : '',
    data.last_updated ? `Updated: ${formatTimestamp(data.last_updated)}` : '',
    data.output_dir ? `Artifacts: ${data.output_dir}` : '',
  ].filter(Boolean).join(' | ');
  setBanner(bannerClass(runStatus), `${runStatus} | ${Object.keys(pipelines).length} pipeline(s)`);
  renderRunPercent(runStatus, pipelines, data.active_pipeline || '');
  renderPipelines(pipelines, runner);
}

function pipelineObjectTotals(record) {
  const objects = Object.values(record.objects || {});
  return objects.reduce((totals, item) => ({
    loaded: totals.loaded + Number(item.loaded_success || 0) + Number(item.loaded_failed || 0),
    total: totals.total + Number(item.transformed || 0),
    inProgress: totals.inProgress || Boolean(item.load_in_progress),
  }), { loaded: 0, total: 0, inProgress: false });
}

function renderRunPercent(runStatus, pipelines, activePipelineId = '') {
  const entries = Object.entries(pipelines);
  const pipelineRecords = Object.values(pipelines);
  const terminal = new Set(['COMPLETED', 'FAILED', 'STOPPED', 'DATA_LOAD_FAILED', 'PARTIAL_DATA_LOAD_FAILED']);
  const finished = terminal.has(String(runStatus || '').toUpperCase());
  let queuedIds = entries.filter(([, record]) => ['QUEUED', 'PENDING'].includes(String(record.status || '').toUpperCase()));
  const completedIds = entries.filter(([, record]) => terminal.has(String(record.status || '').toUpperCase()));
  let activeId = activePipelineId;
  if (!activeId) {
    const loading = entries.find(([, record]) => {
      const status = String(record.status || '').toUpperCase();
      if (['QUEUED', 'PENDING'].includes(status)) return false;
      return pipelineObjectTotals(record).inProgress || status === 'RUNNING';
    });
    activeId = loading ? loading[0] : '';
  }
  if (!queuedIds.length) {
    const openStages = entries.filter(([, record]) => !terminal.has(String(record.status || '').toUpperCase()));
    if (openStages.length > 1) {
      if (!activeId) activeId = openStages[0][0];
      queuedIds = openStages.filter(([id]) => id !== activeId);
    }
  }
  const activeRecord = (activeId && pipelines[activeId]) || null;
  const activeTotals = activeRecord ? pipelineObjectTotals(activeRecord) : { loaded: 0, total: 0 };
  if (activeTotals.total > 0 || queuedIds.length || completedIds.length) {
    const stageCount = entries.length || 1;
    const currentFrac = activeTotals.total > 0 ? Math.min(1, activeTotals.loaded / activeTotals.total) : 0;
    const percent = finished ? 100 : Math.min(99, Math.round(((completedIds.length + currentFrac) * 100) / stageCount));
    $('run-progress-fill').style.width = `${percent}%`;
    $('run-progress-percent').textContent = `${percent}%`;
    const parts = [];
    if (activeTotals.total > 0) {
      parts.push(`${activeTotals.loaded.toLocaleString()} of ${activeTotals.total.toLocaleString()} records in ${activeRecord.name || activeId}`);
    }
    if (queuedIds.length) parts.push(`${queuedIds.length} stage(s) in queue`);
    if (completedIds.length && !finished) parts.push(`${completedIds.length} stage(s) done`);
    $('run-progress-count').textContent = parts.join(' · ');
    return;
  }
  const objects = Object.values(pipelines).flatMap(pipeline => Object.values(pipeline.objects || {}));
  const total = objects.length || pipelineRecords.length;
  const done = objects.length
    ? objects.filter(record => terminal.has(objectDisplayStatus(record))).length
    : pipelineRecords.filter(record => terminal.has(String(record.status || '').toUpperCase())).length;
  const percent = finished ? 100 : (total ? Math.round(done * 100 / total) : 0);
  $('run-progress-fill').style.width = `${percent}%`;
  $('run-progress-percent').textContent = `${percent}%`;
  $('run-progress-count').textContent = total ? `${done.toLocaleString()} of ${total.toLocaleString()} work units finished` : '';
}

async function loadLogs() {
  const response = await fetch(`/api/pipeline/logs?run_key=${encodeURIComponent(RUN_KEY)}`, { cache: 'no-store' });
  const data = await readJsonResponse(response);
  if (!response.ok || data.error) throw new Error(data.error || 'Failed to load logs');
  const output = $('run-progress-log-output');
  const nearBottom = output.scrollHeight - output.scrollTop - output.clientHeight < 80;
  output.textContent = data.content || 'No logs yet.';
  $('run-progress-log-meta').textContent = `${data.log_path || ''}${data.size != null ? ` | ${data.size} bytes` : ''}${data.truncated ? ' | latest tail' : ''}`;
  if (nearBottom) output.scrollTop = output.scrollHeight;
}

function renderPipelines(pipelines, runner) {
  const entries = Object.entries(pipelines);
  const container = $('run-progress-pipelines');
  if (!entries.length) {
    container.innerHTML = '<div class="designer-flow-empty">Run accepted. Waiting for pipeline state...</div>';
    return;
  }
  container.innerHTML = entries.map(([pipelineId, record]) => {
    const objects = Object.entries(record.objects || {});
    const status = String(record.status || 'PENDING').toUpperCase();
    const statusLabel = status === 'QUEUED' ? 'IN QUEUE' : status;
    const completed = objects.filter(([, value]) => objectDisplayStatus(value) === 'COMPLETED').length;
    const failed = objects.filter(([, value]) => ['FAILED', 'DATA_LOAD_FAILED', 'PARTIAL_DATA_LOAD_FAILED'].includes(objectDisplayStatus(value))).length;
    const objectRows = objects.length
      ? objects.map(([name, value]) => {
          const objectStatus = objectDisplayStatus(value);
          const success = Number(value.loaded_success || 0);
          const failedRows = Number(value.loaded_failed || 0);
          const transformed = Number(value.transformed || 0);
          const queued = objectStatus === 'QUEUED' || status === 'QUEUED';
          const inProgress = value.load_in_progress ? ' (in progress)' : '';
          const reviewReady = transformed > 0 && success === 0 && failedRows === 0 && !value.load_in_progress
            && ['COMPLETED', 'PENDING', 'RUNNING', 'STOPPED'].includes(objectStatus);
          const counts = queued && transformed
            ? `${transformed.toLocaleString()} records in queue`
            : reviewReady
              ? `${transformed.toLocaleString()} transformed | ready for review before load`
            : (transformed
              ? `${success.toLocaleString()} loaded | ${failedRows.toLocaleString()} failed | ${transformed.toLocaleString()} to load${inProgress}`
              : (success || failedRows
                ? `${success.toLocaleString()} loaded | ${failedRows.toLocaleString()} failed`
                : 'Waiting for record counts...'));
          const downloadUrl = failedRows > 0 && value.failed_file
            ? `/api/pipeline/runs/${encodeURIComponent(RUN_KEY)}/failed-files/${encodeURIComponent(pipelineId)}/${encodeURIComponent(name)}`
            : '';
          const xfmDownloadName = `${String(name || 'object').replace(/[\\/:*?"<>|]+/g, '_')}_transformed.csv`;
          const xfmDownloadUrl = transformed > 0
            ? `/api/pipeline/runs/${encodeURIComponent(RUN_KEY)}/xfm-files/${encodeURIComponent(pipelineId)}/${encodeURIComponent(name)}`
            : '';
          const failedLabel = value.load_in_progress
            ? 'Download Failed CSV (so far)'
            : 'Download Failed CSV';
          const successDownloadUrl = success > 0 && value.success_file
            ? `/api/pipeline/runs/${encodeURIComponent(RUN_KEY)}/success-files/${encodeURIComponent(pipelineId)}/${encodeURIComponent(name)}`
            : '';
          return `<div class="run-progress-object">
            <span class="run-progress-object-name">${esc(name)}<small>${esc(counts)}</small></span>
            <span class="run-progress-object-actions">
              ${xfmDownloadUrl ? `<a class="btn btn-secondary btn-sm" href="${xfmDownloadUrl}" download="${esc(xfmDownloadName)}">Download Transformed CSV</a>` : ''}
              ${successDownloadUrl ? `<a class="btn btn-secondary btn-sm" href="${successDownloadUrl}">Download Success CSV</a>` : ''}
              ${downloadUrl ? `<a class="btn btn-secondary btn-sm" href="${downloadUrl}">${failedLabel}</a>` : ''}
              ${reviewReady ? '<span class="pipeline-library-item-meta">Review transformed files, then use Resume at Load.</span>' : ''}
              <span class="pipeline-stage-status ${statusClass(objectStatus)}">${esc(objectStatus === 'QUEUED' ? 'IN QUEUE' : objectStatus)}</span>
            </span>
          </div>`;
        }).join('')
      : '<div class="pipeline-library-item-meta">Waiting for object progress...</div>';
    const resumeDisabled = runner.running ? 'disabled' : '';
    return `<article class="designer-stage-card run-progress-card">
      <div class="designer-stage-top">
        <div><div class="designer-stage-order">Pipeline</div><div class="designer-stage-title">${esc(record.name || pipelineId)}</div><div class="designer-stage-template">${esc(pipelineId)}</div></div>
        <span class="pipeline-stage-status ${statusClass(status)}">${esc(statusLabel)}</span>
      </div>
      <div class="designer-stage-body"><div class="designer-stage-line">Objects: ${completed}/${objects.length} completed${failed ? ` | ${failed} failed` : ''}</div></div>
      <div class="run-progress-objects">${objectRows}</div>
      <div class="run-progress-actions">
        <button class="btn btn-primary btn-sm" ${resumeDisabled} onclick="resumeFromPipeline('${escJs(pipelineId)}')">Resume from this pipeline</button>
        ${runner.running ? '<span class="pipeline-library-item-meta">Available after the current run stops.</span>' : ''}
      </div>
    </article>`;
  }).join('');
}

function objectDisplayStatus(record) {
  const liveStatus = String(record?.status || 'PENDING').toUpperCase();
  if (liveStatus === 'QUEUED') return 'QUEUED';
  if (record?.load_in_progress) {
    return liveStatus === 'PENDING' ? 'RUNNING' : liveStatus;
  }
  const success = Number(record?.loaded_success || 0);
  const failed = Number(record?.loaded_failed || 0);
  if (failed > 0 && success === 0) return 'DATA_LOAD_FAILED';
  if (failed > 0 && success > 0) return 'PARTIAL_DATA_LOAD_FAILED';
  return String(record?.status || 'PENDING').toUpperCase();
}

function pipelineNeedsLoadResume(pipelineId) {
  const record = currentPipelines?.[pipelineId] || {};
  const objects = Object.values(record.objects || {});
  if (!objects.length) return false;
  return objects.every(value => {
    const status = objectDisplayStatus(value);
    const success = Number(value?.loaded_success || 0);
    const failed = Number(value?.loaded_failed || 0);
    const transformed = Number(value?.transformed || 0);
    return transformed > 0 && success === 0 && failed === 0 && !value?.load_in_progress
      && ['PENDING', 'RUNNING', 'STOPPED', 'COMPLETED'].includes(status);
  });
}

async function resumeFromPipeline(pipelineId) {
  if (currentRunner.running) {
    alert('Stop or wait for the current run to finish before resuming.');
    return;
  }
  const selectedStage = $('run-progress-resume-stage').value || 'transform';
  const startStage = pipelineNeedsLoadResume(pipelineId) ? 'load' : selectedStage;
  const label = startStage === 'load' ? 'Load' : 'Transform';
  const staleWarning = startStage === 'load'
    ? '\n\nLoad reuses the previously transformed data. Changes to the source CSV or transformation rules will NOT be applied. Select Transform instead when either changed.'
    : '';
  if (!window.confirm(`Resume from pipeline "${pipelineId}" at ${label}? This creates a new run and keeps this run unchanged.${staleWarning}`)) {
    return;
  }
  const response = await fetch('/api/pipeline/resume', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      run_key: RUN_KEY,
      pipeline_id: pipelineId,
      start_stage: startStage,
    }),
  });
  const data = await response.json();
  if (!response.ok || data.error) {
    alert(data.error || 'Failed to resume pipeline.');
    return;
  }
  const progressUrl = data.progress_url || (data.run_key ? `/pipeline/runs/${encodeURIComponent(data.run_key)}` : '');
  if (progressUrl) {
    window.location.assign(progressUrl);
  }
}

async function stopPipelineRun() {
  if (!currentRunner.running) return;
  if (!window.confirm(`Stop pipeline run "${RUN_KEY}"? The completed work and logs will be preserved.`)) {
    return;
  }
  const button = $('run-progress-stop');
  button.disabled = true;
  button.textContent = 'Stopping...';
  try {
    const response = await fetch('/api/pipeline/stop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_key: RUN_KEY }),
    });
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || 'Failed to stop pipeline.');
    await refreshRun(true);
  } catch (error) {
    alert(error.message);
  } finally {
    button.textContent = 'Stop Pipeline';
    button.disabled = !currentRunner.running;
  }
}

function deriveRunStatus(runner, pipelines) {
  if (runner.running) return 'RUNNING';
  const statuses = Object.values(pipelines).map(value => String(value.status || '').toUpperCase());
  if (statuses.some(value => value === 'STOPPED')) return 'STOPPED';
  if (statuses.some(value => value === 'PARTIAL_DATA_LOAD_FAILED')) return 'PARTIAL_DATA_LOAD_FAILED';
  if (statuses.some(value => value === 'DATA_LOAD_FAILED')) return 'DATA_LOAD_FAILED';
  if (statuses.some(value => value === 'FAILED')) return 'FAILED';
  if (statuses.length && statuses.every(value => ['COMPLETED', 'SKIPPED'].includes(value))) return 'COMPLETED';
  if (runner.returncode != null) return runner.returncode === 0 ? 'COMPLETED' : 'FAILED';
  return statuses.length ? 'QUEUED' : 'WAITING';
}

function setBanner(kind, text) {
  const banner = $('run-progress-banner');
  banner.className = `auth-status ${kind}`;
  banner.innerHTML = `<span class="dot"></span> ${esc(text)}`;
}

function statusClass(value) {
  const status = String(value || 'PENDING').toUpperCase();
  if (status === 'RUNNING') return 'running';
  if (status === 'COMPLETED') return 'completed';
  if (status === 'SKIPPED') return 'skipped';
  if (status.includes('FAILED') || status === 'STOPPED') return 'failed';
  return 'pending';
}

function bannerClass(value) {
  const status = String(value || '').toUpperCase();
  if (status === 'RUNNING' || status === 'QUEUED') return 'checking';
  if (status === 'FAILED' || status === 'STOPPED') return 'error';
  return 'ok';
}

function formatTimestamp(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value || '') : date.toLocaleString();
}

function esc(value) {
  return String(value ?? '').replace(/[&<>'"]/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[character]);
}

function escJs(value) {
  return String(value ?? '').replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/[\r\n]/g, ' ');
}
