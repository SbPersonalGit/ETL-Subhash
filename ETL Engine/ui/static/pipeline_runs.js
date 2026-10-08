const $ = id => document.getElementById(id);
const PAGE_SIZE = 20;
let pageRuns = [];
let pageOffset = 0;
let totalRuns = 0;
let searchTimer = null;
let loadSeq = 0;

document.addEventListener('DOMContentLoaded', async () => {
  const preOption = document.createElement('option');
  preOption.value = 'PRE_VALIDATION';
  preOption.textContent = 'Pre-validations';
  $('runs-activity-filter').appendChild(preOption);
  const storageOption = document.createElement('option');
  storageOption.value = 'STORAGE';
  storageOption.textContent = 'Storage operations';
  $('runs-activity-filter').appendChild(storageOption);
  const utilityOption = document.createElement('option');
  utilityOption.value = 'GENERAL_UTILITY';
  utilityOption.textContent = 'General Utility metadata';
  $('runs-activity-filter').appendChild(utilityOption);
  $('runs-refresh').addEventListener('click', () => loadRuns());
  $('runs-prev').addEventListener('click', () => {
    pageOffset = Math.max(0, pageOffset - PAGE_SIZE);
    loadRuns();
  });
  $('runs-next').addEventListener('click', () => {
    if (pageOffset + PAGE_SIZE >= totalRuns) return;
    pageOffset += PAGE_SIZE;
    loadRuns();
  });
  $('runs-search').addEventListener('input', () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => {
      pageOffset = 0;
      loadRuns();
    }, 300);
  });
  $('runs-activity-filter').addEventListener('change', () => {
    pageOffset = 0;
    loadRuns();
  });
  $('runs-operation-filter').addEventListener('change', () => {
    pageOffset = 0;
    loadRuns();
  });
  await loadRuns();
  window.setInterval(() => loadRuns({ quiet: true }), 15000);
});

async function loadRuns({ quiet = false } = {}) {
  const seq = ++loadSeq;
  try {
    const params = new URLSearchParams({
      limit: String(PAGE_SIZE),
      offset: String(pageOffset),
    });
    const activity = $('runs-activity-filter').value;
    const operation = $('runs-operation-filter').value;
    const query = $('runs-search').value.trim();
    if (activity) params.set('activity_type', activity);
    if (operation) params.set('data_operation', operation);
    if (query) params.set('q', query);

    const response = await fetch(`/api/pipeline/runs?${params}`, { cache: 'no-store' });
    const data = await response.json();
    if (seq !== loadSeq) return;
    if (!response.ok || data.error) throw new Error(data.error || 'Unable to load runs');

    pageRuns = data.runs || [];
    totalRuns = Number(data.total || 0);
    if (pageOffset > 0 && !pageRuns.length && totalRuns > 0) {
      pageOffset = Math.max(0, Math.floor((totalRuns - 1) / PAGE_SIZE) * PAGE_SIZE);
      return loadRuns({ quiet });
    }
    renderRuns();
    updatePager();
    if (!quiet) {
      const start = totalRuns ? pageOffset + 1 : 0;
      const end = Math.min(pageOffset + pageRuns.length, totalRuns);
      setBanner('ok', `${start}-${end} of ${totalRuns} run(s) · refreshed ${new Date().toLocaleTimeString()}`);
    }
  } catch (error) {
    if (seq !== loadSeq) return;
    setBanner('error', error.message);
  }
}

function updatePager() {
  const start = totalRuns ? pageOffset + 1 : 0;
  const end = Math.min(pageOffset + pageRuns.length, totalRuns);
  const page = totalRuns ? Math.floor(pageOffset / PAGE_SIZE) + 1 : 0;
  const pages = Math.max(1, Math.ceil(totalRuns / PAGE_SIZE));
  $('runs-page-label').textContent = totalRuns
    ? `Showing ${start}-${end} of ${totalRuns} · Page ${page} of ${pages}`
    : 'No runs';
  $('runs-prev').disabled = pageOffset <= 0;
  $('runs-next').disabled = pageOffset + PAGE_SIZE >= totalRuns;
}

function renderRuns() {
  const runs = pageRuns;
  $('runs-body').innerHTML = runs.length ? runs.map(run => {
    const pipelines = (run.pipelines || []).map(pipe => `${pipe.name || pipe.id} (${pipe.status})`).join(', ') || 'No pipeline detail';
    const firstPipeline = run.pipelines?.[0]?.id || '';
    const isValidation = run.activity_type === 'VALIDATION';
    const isPreValidation = run.activity_type === 'PRE_VALIDATION';
    const isStorage = run.activity_type === 'STORAGE';
    const isGeneralUtility = run.activity_type === 'GENERAL_UTILITY';
    const context = isGeneralUtility
      ? `<strong>${esc(run.migration_name || 'General Utility metadata')}</strong><small>Persistent Salesforce Tooling API field/access operation</small>`
      : isStorage
      ? `<strong>${esc(run.migration_name || 'Storage Health')}</strong><small>Archive, cleanup, or restore operation</small>`
      : isPreValidation
      ? `<strong>Template: ${esc(run.template_name || run.template_id || 'Unknown')}</strong><small>Migration/activity: ${esc(run.migration_name || '')}</small><small>Pipeline: ${esc(run.migration_pipeline_name || 'Not assigned yet')}</small>`
      : `${esc(run.migration_name || '')}<small>${esc(pipelines)}</small>${isValidation && run.source_pipeline_status ? `<small class="source-outcome ${statusClass(run.source_pipeline_status)}">Source migration: ${esc(run.source_pipeline_status)}</small>` : ''}`;
    const sourceRun = run.source_run_key || run.run_key;
    const percent = runPercent(run);
    const actions = isGeneralUtility
      ? `<a class="btn btn-secondary btn-sm" href="/general-utility/runs/${encodeURIComponent(run.run_key)}">Progress</a><a class="btn btn-primary btn-sm" href="/general-utility">General Utility</a>`
      : isStorage
      ? `<a class="btn btn-secondary btn-sm" href="/storage">Storage Health</a>`
      : isPreValidation
      ? `<a class="btn btn-secondary btn-sm" href="/validation/runs/${encodeURIComponent(run.run_key)}">Progress</a><a class="btn btn-secondary btn-sm" href="/pipeline/logs?run_key=${encodeURIComponent(run.run_key)}">Logs</a><a class="btn btn-primary btn-sm" href="/validation?phase=pre&template_id=${encodeURIComponent(firstPipeline)}">Run Again</a>`
      : isValidation
      ? `<a class="btn btn-secondary btn-sm" href="/validation/runs/${encodeURIComponent(run.run_key)}">Progress</a><a class="btn btn-secondary btn-sm" href="/pipeline/logs?run_key=${encodeURIComponent(run.run_key)}">Logs</a><a class="btn btn-primary btn-sm" href="/validation?phase=post&run_key=${encodeURIComponent(sourceRun)}&pipeline_id=${encodeURIComponent(firstPipeline)}">Run Again</a>`
      : `<a class="btn btn-secondary btn-sm" href="/pipeline/runs/${encodeURIComponent(run.run_key)}">Progress</a><a class="btn btn-secondary btn-sm" href="/pipeline/logs?run_key=${encodeURIComponent(run.run_key)}">Logs</a><a class="btn btn-primary btn-sm" href="/validation?run_key=${encodeURIComponent(run.run_key)}&pipeline_id=${encodeURIComponent(firstPipeline)}">Validate</a>`;
    const statusLabel = run.status;
    return `<tr><td><span class="activity-type-badge ${isGeneralUtility ? 'utility' : isStorage ? 'storage' : (isValidation || isPreValidation ? 'validation' : 'migration')}">${esc(run.activity_type)}</span><strong>${esc(run.run_key)}</strong><small>${esc(run.submitted_by_name || '')}</small></td><td>${context}</td><td>${esc(run.data_operation || '')}</td><td><span class="pipeline-stage-status ${statusClass(run.status)}">${esc(statusLabel)}</span>${isValidation && run.source_pipeline_status ? `<small>Source: ${esc(run.source_pipeline_status)}</small>` : ''}<div class="runs-percent"><span style="width:${percent}%"></span></div><small>${percent}%</small></td><td>${esc(formatDate(run.created_at))}</td><td>${esc(run.target_profile || 'default')}</td><td class="pipeline-runs-actions">${actions}</td></tr>`;
  }).join('') : '<tr><td colspan="7">No matching activities.</td></tr>';
}

function runPercent(run) {
  if (run.progress_percent !== null && run.progress_percent !== undefined) return Math.max(0, Math.min(100, Number(run.progress_percent) || 0));
  const status = String(run.status || '').toUpperCase();
  if (['COMPLETED', 'FAILED', 'STOPPED', 'DATA_LOAD_FAILED', 'PARTIAL_DATA_LOAD_FAILED', 'VALIDATION_COMPLETED', 'VALIDATION_FAILED', 'PARTIALLY_VALIDATED'].includes(status)) return 100;
  const objects = (run.pipelines || []).flatMap(p => Object.values(p.objects || {}));
  if (!objects.length) return status === 'RUNNING' ? 1 : 0;
  const terminal = new Set(['COMPLETED', 'FAILED', 'STOPPED', 'DATA_LOAD_FAILED', 'PARTIAL_DATA_LOAD_FAILED']);
  const done = objects.filter(x => terminal.has(String(x.status || '').toUpperCase())).length;
  return Math.round(done * 100 / objects.length);
}

function statusClass(status) {
  const v = String(status || '').toUpperCase();
  if (v === 'COMPLETED' || v === 'VALIDATION_COMPLETED') return 'completed';
  if (v.includes('FAILED') || v === 'STOPPED' || v === 'PARTIALLY_VALIDATED') return 'failed';
  if (v === 'RUNNING' || v === 'QUEUED') return 'running';
  return 'pending';
}
function setBanner(kind, text) {
  const el = $('runs-banner');
  el.className = `auth-status ${kind}`;
  el.innerHTML = `<span class="dot"></span> ${esc(text)}`;
}
function formatDate(value) { return value ? new Date(value).toLocaleString() : ''; }
function esc(value) {
  return String(value ?? '').replace(/[&<>'"]/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
}
