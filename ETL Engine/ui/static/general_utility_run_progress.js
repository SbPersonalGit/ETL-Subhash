const $ = id => document.getElementById(id);
const JOB_ID = document.body.dataset.jobId;
const JOB_TYPE = document.body.dataset.jobType || 'create_fields';
const esc = value => String(value ?? '').replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
let terminal = false;

function date(value) { return value ? new Date(value).toLocaleString() : '—'; }
function elapsed(job) {
  if (!job.started_at) return '';
  const end = job.finished_at ? new Date(job.finished_at).getTime() : Date.now();
  return `Started ${date(job.started_at)} · elapsed ${Math.max(0, Math.floor((end - new Date(job.started_at).getTime()) / 1000))}s`;
}
function render(job) {
  terminal = ['completed', 'error'].includes(job.status);
  const percent = Math.max(0, Math.min(100, Number(job.percent) || 0));
  const panel = $('gu-field-progress');
  const jobType = job.job_type || JOB_TYPE;
  panel.classList.toggle('complete', job.status === 'completed');
  panel.classList.toggle('error', job.status === 'error');
  $('gup-title').textContent = `General Utility · ${job.object || 'Metadata'}`;
  $('gup-meta').textContent = `Run ${job.id} · Target ${job.target_profile || 'default'}`;
  $('gup-status').textContent = String(job.status || 'unknown').toUpperCase();
  $('gup-api').textContent = job.execution_api || 'Salesforce Tooling API + REST FieldPermissions';
  $('gu-progress-title').textContent = job.message || 'Running...';
  $('gu-progress-elapsed').textContent = elapsed(job);
  $('gu-progress-percent').textContent = `${percent}%`;
  $('gu-progress-bar').style.width = `${percent}%`;
  panel.querySelector('[role="progressbar"]').setAttribute('aria-valuenow', String(percent));
  $('gu-progress-events').innerHTML = (job.events || []).map(event => `<li><small>${esc(date(event.time))}</small> ${esc(event.message)}</li>`).join('');
  if (jobType === 'bulk_fls') {
    $('gup-results-heading').textContent = 'Object access results';
    $('gup-fields-head').innerHTML = '<tr><th>Object</th><th>Object permission</th><th>Permission ID</th><th>Permission/Profile</th><th>Fields / Record Types</th><th>Detail ID</th></tr>';
  } else {
    $('gup-results-heading').textContent = 'Salesforce field and access results';
    $('gup-fields-head').innerHTML = '<tr><th>Field</th><th>Field result</th><th>Salesforce field ID</th><th>Permission/Profile</th><th>Access result</th><th>Salesforce permission ID</th></tr>';
  }
  const rows = [];
  (job.fields || []).forEach(field => {
    const permissions = field.permissions?.length ? field.permissions : [{}];
    permissions.forEach((permission, index) => rows.push(`<tr><td><code>${index ? '' : esc(field.field)}</code></td><td>${index ? '' : esc(field.created ? 'CREATED' : field.reason || 'VERIFIED')}</td><td><code>${index ? '' : esc(field.id || '—')}</code></td><td>${esc(permission.principal || '—')}</td><td>${esc(permission.created ? 'CREATED' : permission.reason || '—')}</td><td><code>${esc(permission.id || '—')}</code></td></tr>`));
  });
  $('gup-fields').innerHTML = rows.join('') || '<tr><td colspan="6">Waiting for the first Salesforce result...</td></tr>';
  $('gup-error-card').hidden = !job.error;
  $('gup-error').textContent = job.error || '';
  $('gup-auth').className = `auth-status ${job.status === 'error' ? 'error' : job.status === 'completed' ? 'ok' : 'checking'}`;
  $('gup-auth').innerHTML = `<span class="dot"></span> ${esc(job.message || job.status)}`;
}
async function load() {
  try {
    const response = await fetch(`/api/general-utility/jobs/${encodeURIComponent(JOB_ID)}`, {cache: 'no-store'});
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || 'Unable to load operation');
    render(data);
  } catch (error) {
    $('gup-auth').className = 'auth-status error';
    $('gup-auth').innerHTML = `<span class="dot"></span> ${esc(error.message)}`;
  }
}
$('gup-refresh').addEventListener('click', load);
load();
window.setInterval(() => { if (!terminal) load(); }, 1000);
