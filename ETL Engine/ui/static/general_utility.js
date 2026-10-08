const gu = { files: [], objects: [], analysis: null };
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));

async function api(url, options = {}) {
  const response = await fetch(url, {headers: {'Content-Type': 'application/json'}, ...options});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}
function message(text, kind = '') { $('gu-message').textContent = text; $('gu-message').className = `gu-message ${kind}`; }
function list(value) { return String(value || '').split(',').map(x => x.trim()).filter(Boolean); }
function selectedValues(select) { return [...select.selectedOptions].map(option => option.value); }
function selectedConcatColumns() { return [...document.querySelectorAll('.gu-concat-column:checked')].map(input => input.value); }
function updateConcatCount() {
  const all = document.querySelectorAll('.gu-concat-column').length;
  const selected = document.querySelectorAll('.gu-concat-column:checked').length;
  $('gu-concat-count').textContent = `(${selected} selected of ${all})`;
}
function filterConcatColumns() {
  const search = $('gu-concat-filter').value.trim().toLocaleLowerCase();
  document.querySelectorAll('#gu-concat-columns label').forEach(label => { label.hidden = Boolean(search && !label.dataset.column.includes(search)); });
}
function resultBox(id, data, links = []) {
  const node = $(id); node.hidden = false; node.className = 'gu-result ok';
  node.innerHTML = `<strong>Completed.</strong> ${esc(data.source_rows ?? '')} rows; ${esc(data.replacements ?? data.columns_added?.length ?? 0)} change(s).<div>${links.map(([label, url]) => `<a href="${esc(url)}">${esc(label)}</a>`).join('')}</div>`;
}
const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
function renderFieldProgress(job) {
  const panel = $('gu-field-progress');
  const percent = Math.max(0, Math.min(100, Number(job.percent) || 0));
  panel.hidden = false;
  panel.classList.toggle('complete', job.status === 'completed');
  panel.classList.toggle('error', job.status === 'error');
  $('gu-progress-title').textContent = job.message || 'Metadata operation is running...';
  $('gu-progress-percent').textContent = `${percent}%`;
  $('gu-progress-bar').style.width = `${percent}%`;
  const track = panel.querySelector('[role="progressbar"]');
  track.setAttribute('aria-valuenow', String(percent));
  $('gu-progress-events').innerHTML = (job.events || []).map(event => `<li>${esc(event.message)}</li>`).join('');
  $('gu-progress-events').scrollTop = $('gu-progress-events').scrollHeight;
}
async function monitorFieldJob(statusUrl, startedAt) {
  const timer = setInterval(() => {
    $('gu-progress-elapsed').textContent = `Elapsed: ${Math.max(0, Math.floor((Date.now() - startedAt) / 1000))}s`;
  }, 1000);
  try {
    while (true) {
      const job = await api(statusUrl);
      renderFieldProgress(job);
      if (job.status === 'completed') return job;
      if (job.status === 'error') throw new Error(job.error || job.message || 'Metadata operation failed.');
      await wait(750);
    }
  } finally {
    clearInterval(timer);
    $('gu-progress-elapsed').textContent = `Elapsed: ${Math.max(0, Math.floor((Date.now() - startedAt) / 1000))}s`;
  }
}

async function loadProfiles() {
  const data = await api('/api/org-profiles');
  const orgs = Array.isArray(data.orgs) && data.orgs.length ? data.orgs : (data.target || []);
  const options = orgs.map(item => `<option value="${esc(item.id)}">${esc(item.label || item.name || item.id)}${item.login_domain ? ` (${esc(item.login_domain)})` : ''}</option>`).join('') || '<option value="default">default</option>';
  $('gu-profile').innerHTML = options;
  $('gu-fls-profile').innerHTML = options;
  const preferred = orgs.find(item => item.legacy_role === 'target')
    || orgs.find(item => item.id === 'target')
    || orgs.find(item => (item.aliases || []).includes('default'))
    || orgs[0];
  if (preferred) {
    $('gu-profile').value = preferred.id;
    $('gu-fls-profile').value = preferred.id;
  }
}
async function loadFiles() {
  const data = await api('/api/flat-files/drop-files'); gu.files = data.files || [];
  $('gu-file').innerHTML = '<option value="">Select a state-folder CSV</option>' + gu.files.map(file => `<option value="${esc(file.name)}">${esc(file.name)} (${Math.ceil(file.size / 1024)} KB)</option>`).join('');
}
async function loadObjects() {
  const profile = $('gu-profile').value || 'default';
  await api(`/api/auth/check?mode=flat_file&org=target&profile=${encodeURIComponent(profile)}`);
  const data = await api(`/api/flat-files/objects?profile=${encodeURIComponent(profile)}`); gu.objects = data.objects || [];
  $('gu-object').innerHTML = '<option value="">Select object</option>' + gu.objects.map(item => `<option value="${esc(item.name)}">${esc(item.label)} — ${esc(item.name)}</option>`).join('');
  $('gu-auth').className = 'auth-status ok'; $('gu-auth').innerHTML = '<span class="dot"></span> Target connected';
  autoSelectObject();
}
function fillAccessSelectors(permissionSelectId, profileSelectId, data, {preferMigration = true} = {}) {
  $(permissionSelectId).innerHTML = (data.permission_sets || []).map(item => `<option value="${esc(item.name)}">${esc(item.label || item.name)} — ${esc(item.name)}</option>`).join('');
  $(profileSelectId).innerHTML = (data.profiles || []).map(item => `<option value="${esc(item.name)}">${esc(item.label || item.name)}</option>`).join('');
  if (!preferMigration) return;
  const prefer = item => /data\s*migration/i.test(`${item.label || ''} ${item.name || ''}`);
  [...$(permissionSelectId).options].forEach(option => {
    const match = (data.permission_sets || []).find(item => item.name === option.value);
    if (match && prefer(match)) option.selected = true;
  });
  [...$(profileSelectId).options].forEach(option => {
    const match = (data.profiles || []).find(item => item.name === option.value);
    if (match && prefer(match)) option.selected = true;
  });
}
async function loadAccessPrincipals() {
  const profile = $('gu-profile').value || 'default';
  const data = await api(`/api/general-utility/access-principals?target_profile=${encodeURIComponent(profile)}`);
  fillAccessSelectors('gu-permission-sets', 'gu-profiles', data);
}
async function loadFlsAccessPrincipals() {
  const profile = $('gu-fls-profile').value || $('gu-profile').value || 'default';
  await api(`/api/auth/check?mode=flat_file&org=target&profile=${encodeURIComponent(profile)}`);
  const data = await api(`/api/general-utility/access-principals?target_profile=${encodeURIComponent(profile)}`);
  fillAccessSelectors('gu-fls-permission-sets', 'gu-fls-profiles', data);
  $('gu-auth').className = 'auth-status ok';
  $('gu-auth').innerHTML = '<span class="dot"></span> Target connected';
}
async function loadTargetContext() {
  await Promise.all([loadObjects(), loadAccessPrincipals(), loadFlsAccessPrincipals()]);
}

function detectObjectFromPath(relativePath) {
  const normalized = String(relativePath || '').replaceAll('\\', '/');
  const parts = normalized.split('/').filter(Boolean);
  if (!parts.length) return null;
  const byName = new Map(gu.objects.map(item => [String(item.name || '').toLocaleLowerCase(), item.name]));
  for (const folder of parts.slice(0, -1).reverse()) {
    const match = byName.get(folder.trim().toLocaleLowerCase());
    if (match) return {name: match, source: 'folder'};
  }
  const filename = parts.at(-1) || '';
  const stem = filename.replace(/\.csv$/i, '').trim().toLocaleLowerCase();
  const match = byName.get(stem);
  return match ? {name: match, source: 'filename'} : null;
}

function autoSelectObject() {
  const detected = detectObjectFromPath($('gu-file').value);
  if (!detected) return;
  $('gu-object').value = detected.name;
  message(`Target object auto-detected from ${detected.source}: ${detected.name}. You can override it before analysis.`, 'ok');
}

function fileOptions(selected = '') {
  return '<option value="">Select inventory</option>' + gu.files.map(file => `<option value="${esc(file.name)}" ${file.name === selected ? 'selected' : ''}>${esc(file.name)}</option>`).join('');
}
function matchFieldName(legacyField, suffix = $('gu-match-suffix').value.trim() || 'Match') {
  const base = String(legacyField || '').replace(/__c$/i, '');
  const cleanSuffix = String(suffix || 'Match').replace(/^_+|_+$/g, '') || 'Match';
  const maxBaseLength = Math.max(1, 40 - cleanSuffix.length - 4);
  // Truncation can land immediately after an underscore. Strip it before
  // appending the separator so Salesforce does not parse "__" as a namespace.
  const truncatedBase = base.slice(0, maxBaseLength).replace(/_+$/g, '');
  return `${truncatedBase}_${cleanSuffix}__c`;
}
function refreshMatchDefaults(force = false) {
  document.querySelectorAll('#gu-preserve-body tr').forEach(row => {
    const legacy = row.querySelector('.gu-preserve-name');
    const matchName = row.querySelector('.gu-match-name');
    const formula = row.querySelector('.gu-match-formula');
    if (!legacy || !matchName || !formula || formula.disabled) return;
    const nextName = matchFieldName(legacy.value);
    const nextFormula = `${legacy.value.trim()} = ${formula.dataset.matchExpression}`;
    if (force || !matchName.value || matchName.value === matchName.dataset.auto) matchName.value = nextName;
    if (force || !formula.value || formula.value === formula.dataset.auto) formula.value = nextFormula;
    matchName.dataset.auto = nextName;
    formula.dataset.auto = nextFormula;
  });
}
function syncPreserveControls(row) {
  const preserve = row.querySelector('.gu-preserve-use');
  const createMatch = row.querySelector('.gu-match-use');
  if (!preserve || !createMatch) return;
  // Create Match only makes sense with Preserve; clear it when Preserve is off.
  if (!preserve.checked && createMatch.checked && !createMatch.disabled) {
    createMatch.checked = false;
  }
  syncMatchControls(row);
}
function syncMatchControls(row) {
  const createMatch = row.querySelector('.gu-match-use');
  const matchName = row.querySelector('.gu-match-name');
  const formula = row.querySelector('.gu-match-formula');
  if (!createMatch || !matchName || !formula) return;
  const enabled = !createMatch.disabled && createMatch.checked;
  matchName.disabled = !enabled;
  formula.disabled = !enabled;
  if (!enabled) {
    matchName.value = '';
    formula.value = '';
    return;
  }
  const legacy = row.querySelector('.gu-preserve-name')?.value.trim() || '';
  matchName.value = matchName.value || matchFieldName(legacy);
  formula.value = formula.value || `${legacy} = ${formula.dataset.matchExpression}`;
  matchName.dataset.auto = matchName.value;
  formula.dataset.auto = formula.value;
}
function filterPreservationRows() {
  const search = $('gu-preserve-filter').value.trim().toLocaleLowerCase();
  const type = $('gu-preserve-type').value;
  const view = $('gu-preserve-view').value;
  let visible = 0;
  const rows = [...document.querySelectorAll('#gu-preserve-body tr[data-index]')];
  rows.forEach(row => {
    const preserve = row.querySelector('.gu-preserve-use')?.checked;
    const match = row.querySelector('.gu-match-use')?.checked;
    const viewMatches = view === 'all'
      || (view === 'populated' && Number(row.dataset.populated) > 0)
      || (view === 'selected' && preserve)
      || (view === 'match' && match);
    row.hidden = Boolean((search && !row.dataset.column.includes(search)) || (type && row.dataset.fieldType !== type) || !viewMatches);
    if (!row.hidden) visible += 1;
  });
  $('gu-preserve-count').textContent = `${visible} of ${rows.length} fields shown`;
}
function renderAnalysis() {
  const data = gu.analysis; const refs = data.references || [];
  const candidates = data.preservation_candidates || refs;
  $('gu-summary').innerHTML = `<div>Source rows<strong>${data.row_count.toLocaleString()}</strong></div><div>Source columns<strong>${data.headers.length.toLocaleString()}</strong></div><div>Populated references<strong>${refs.length.toLocaleString()}</strong></div>`;
  $('gu-fallback-body').innerHTML = refs.map((ref, i) => `<tr data-index="${i}">
    <td><input class="gu-fallback-use" type="checkbox" ${ref.polymorphic ? 'disabled title="Polymorphic fallback requires prefix rules"' : ''}></td><td><code>${esc(ref.column)}</code>${ref.polymorphic ? '<div class="gu-help">Polymorphic: configure prefix rules before fallback</div>' : ''}</td><td><span class="gu-reference-object" data-value="${esc(ref.referenced_objects[0] || '')}">${esc(ref.referenced_objects.join(', '))}</span></td><td>${ref.populated_rows}</td>
    <td><input class="fh-input gu-fallback-preserve" value="${esc(ref.legacy_field)}"></td><td><input class="fh-input gu-fallback-id" placeholder="Fallback Salesforce/source ID"></td><td><select class="fh-input gu-inventory-file">${fileOptions()}</select></td></tr>`).join('') || '<tr><td colspan="7">No populated reference fields were detected.</td></tr>';
  $('gu-preserve-body').innerHTML = candidates.map((item, i) => {
    const autoNumber = item.kind === 'auto_number';
    const matchName = item.default_match ? matchFieldName(item.legacy_field) : '';
    const matchFormula = item.default_match ? `${item.legacy_field} = ${item.match_expression}` : '';
    const kindLabel = autoNumber ? 'Auto number' : item.kind === 'reference' ? 'Reference' : 'Custom field';
    return `<tr data-index="${i}" data-column="${esc(item.column.toLocaleLowerCase())}" data-field-type="${esc(item.source_field_type || 'unknown')}" data-populated="${item.populated_rows}">
      <td><input class="gu-preserve-use" type="checkbox" ${item.default_selected ? 'checked' : ''}></td><td><code class="${item.legacy_field_valid === false ? 'gu-invalid-api-name' : ''}">${esc(item.column)}</code><div class="gu-help">${kindLabel}; ${item.populated_rows} populated</div></td><td>${item.referenced_objects?.length ? item.referenced_objects.map(name => `<code>${esc(name)}</code>`).join(', ') : '<span class="gu-help">—</span>'}</td><td><input class="fh-input gu-preserve-name ${item.legacy_field_valid === false ? 'gu-invalid-api-name' : ''}" value="${esc(item.legacy_field)}" title="${esc(item.legacy_field_error || '')}">${item.legacy_field_error ? `<div class="gu-field-error">${esc(item.legacy_field_error)}</div>` : ''}</td><td>${item.target_field_exists ? `Exists (${esc(item.target_field_type)})` : 'Missing'}</td>
      <td><input class="gu-match-use" type="checkbox" ${item.default_match ? 'checked' : ''} ${item.match_supported === false ? 'disabled title="Match fields are not created for auto-number fields"' : ''}></td><td><input class="fh-input gu-match-name" value="${esc(matchName)}" data-auto="${esc(matchName)}" ${item.default_match ? '' : 'disabled'}></td><td><input class="fh-input gu-match-formula" value="${esc(matchFormula)}" data-auto="${esc(matchFormula)}" data-match-expression="${esc(item.match_expression || item.column)}" ${item.default_match ? '' : 'disabled'}></td><td><textarea class="fh-input gu-field-description" rows="2" placeholder="Uses common description when blank"></textarea></td></tr>`;
  }).join('') || '<tr><td colspan="9">No populated reference, auto-number, or custom fields were detected in the CSV.</td></tr>';
  const types = [...new Set(candidates.map(item => item.source_field_type || 'unknown'))].sort();
  $('gu-preserve-type').innerHTML = '<option value="">All data types</option>' + types.map(type => `<option value="${esc(type)}">${esc(type)}</option>`).join('');
  $('gu-preserve-filter').value = '';
  $('gu-preserve-view').value = 'all';
  filterPreservationRows();
  $('gu-concat-columns').innerHTML = data.headers.map(header => `<label data-column="${esc(header.toLocaleLowerCase())}"><input class="gu-concat-column" type="checkbox" value="${esc(header)}"><code>${esc(header)}</code></label>`).join('');
  $('gu-concat-filter').value = '';
  document.querySelectorAll('.gu-concat-column').forEach(input => input.addEventListener('change', updateConcatCount));
  updateConcatCount();
  $('gu-concat-name').value = `${data.prefix}Concatenated_Values__c`;
  $('gu-results').hidden = false;
  syncInventoryMode();
}
function syncInventoryMode() {
  const local = $('gu-inventory-mode').value === 'local';
  document.querySelectorAll('.gu-inventory-file').forEach(node => { node.disabled = !local; });
}

async function analyze() {
  if (!$('gu-file').value || !$('gu-object').value) return message('Select a source CSV and target object.', 'error');
  message('Analyzing CSV and Target Org metadata...');
  try {
    gu.analysis = await api('/api/general-utility/analyze', {method: 'POST', body: JSON.stringify({file: $('gu-file').value, object: $('gu-object').value, target_profile: $('gu-profile').value, prefix: $('gu-prefix').value})});
    renderAnalysis(); message('Analysis complete. Review each selected action before running it.', 'ok');
  } catch (error) { message(error.message, 'error'); }
}

function fallbackRules() {
  return [...$('gu-fallback-body').querySelectorAll('tr')].filter(row => row.querySelector('.gu-fallback-use')?.checked).map(row => {
    const ref = gu.analysis.references[Number(row.dataset.index)];
    const objectControl = row.querySelector('.gu-reference-object');
    const referencedObject = objectControl.dataset.value;
    if (!referencedObject) throw new Error(`Select a referenced object for ${ref.column}.`);
    return {column: ref.column, referenced_object: referencedObject, preserve_as: row.querySelector('.gu-fallback-preserve').value.trim(), fallback_id: row.querySelector('.gu-fallback-id').value.trim(), inventory_file: row.querySelector('.gu-inventory-file').value};
  });
}
async function runFallback() {
  try {
    const rules = fallbackRules(); const inventoryFiles = {};
    rules.forEach(rule => {
      if (rule.inventory_file && inventoryFiles[rule.referenced_object] && inventoryFiles[rule.referenced_object] !== rule.inventory_file) throw new Error(`Use one confirmed inventory file for all ${rule.referenced_object} references.`);
      if (rule.inventory_file) inventoryFiles[rule.referenced_object] = rule.inventory_file;
    });
    const data = await api('/api/general-utility/apply-fallbacks', {method: 'POST', body: JSON.stringify({file: $('gu-file').value, target_profile: $('gu-profile').value, inventory_mode: $('gu-inventory-mode').value, external_id_field: $('gu-external-id').value, inventory_files: inventoryFiles, rules})});
    resultBox('gu-fallback-result', data, [['Download prepared CSV', data.download], ['Download audit', data.audit_download]]);
  } catch (error) { message(error.message, 'error'); }
}

function columnPayload() {
  const description = $('gu-description').value.trim();
  const selectedRows = [...$('gu-preserve-body').querySelectorAll('tr')].filter(row => row.querySelector('.gu-preserve-use')?.checked);
  const candidates = gu.analysis.preservation_candidates || gu.analysis.references;
  const preservation = selectedRows.map(row => ({source_field: candidates[Number(row.dataset.index)].column, output_field: row.querySelector('.gu-preserve-name').value.trim(), description: row.querySelector('.gu-field-description').value.trim() || description}));
  const overlength = preservation.find(item => item.output_field.length > 40);
  if (overlength) throw new Error(`Generated field API name exceeds 40 characters: ${overlength.output_field}`);
  const matchFields = selectedRows.filter(row => row.querySelector('.gu-match-use')?.checked).map(row => ({
    api_name: row.querySelector('.gu-match-name').value.trim(),
    formula: row.querySelector('.gu-match-formula').value.trim(),
    description: row.querySelector('.gu-field-description').value.trim() || description,
  }));
  if (matchFields.some(item => !item.api_name || !item.formula)) throw new Error('Every selected Match field requires an API name and checkbox formula.');
  const concatName = $('gu-concat-name').value.trim(); const concatSources = selectedConcatColumns();
  if (concatSources.length && !concatName) throw new Error('Enter an API name for the JSON concatenation field.');
  const concatenations = concatSources.length ? [{output_field: concatName, source_fields: concatSources, description: $('gu-concat-description').value.trim() || description}] : [];
  if (!preservation.length && !concatenations.length) throw new Error('Select at least one preservation column or configure a JSON concatenation field.');
  return {preservation, concatenations, match_fields: matchFields};
}
async function prepareColumns() {
  try { const payload = columnPayload(); const data = await api('/api/general-utility/prepare-columns', {method: 'POST', body: JSON.stringify({file: $('gu-file').value, ...payload})}); resultBox('gu-column-result', data, [['Download prepared CSV', data.download], ['Download manifest', data.manifest_download]]); }
  catch (error) { message(error.message, 'error'); }
}
async function createFields() {
  const button = $('gu-create-fields');
  try {
    const payload = columnPayload();
    const fields = payload.preservation.map(item => ({api_name: item.output_field, type: 'Text', length: 255, description: item.description}))
      .concat(payload.concatenations.map(item => ({api_name: item.output_field, type: 'LongTextArea', length: 131072, visible_lines: 5, description: item.description})))
      .concat(payload.match_fields.map(item => ({api_name: item.api_name, type: 'FormulaCheckbox', formula: item.formula, description: item.description})));
    if (!confirm(`Create or verify ${fields.length} field(s) on Target ${$('gu-object').value} and grant the listed permissions?`)) return;
    button.disabled = true;
    button.textContent = 'Metadata operation running...';
    $('gu-column-result').hidden = true;
    const startedAt = Date.now();
    renderFieldProgress({status: 'queued', percent: 0, message: 'Submitting metadata operation...', events: []});
    const accepted = await api('/api/general-utility/create-fields', {method: 'POST', body: JSON.stringify({object: $('gu-object').value, target_profile: $('gu-profile').value, fields, permission_sets: selectedValues($('gu-permission-sets')), profiles: selectedValues($('gu-profiles'))})});
    $('gu-column-result').hidden = false;
    $('gu-column-result').className = 'gu-result ok';
    $('gu-column-result').innerHTML = `Metadata operation submitted. <a href="${esc(accepted.progress_url)}">Open persistent progress</a> You may leave this page and follow it from Pipeline Runs.`;
    const data = await monitorFieldJob(accepted.status_url, startedAt);
    const completedFields = Array.isArray(data.fields) ? data.fields : [];
    $('gu-column-result').hidden = false; $('gu-column-result').className = 'gu-result ok'; $('gu-column-result').innerHTML = `Metadata completed for ${completedFields.length} field(s). <a href="${esc(accepted.progress_url)}">Open run details</a> Re-analyze to refresh Target status.`;
    message('Target metadata operation completed.', 'ok');
  } catch (error) {
    $('gu-field-progress').classList.add('error');
    message(error.message, 'error');
  } finally {
    button.disabled = false;
    button.textContent = 'Create missing fields and grant access';
  }
}

function renderFlsProgress(job) {
  const panel = $('gu-fls-progress');
  const percent = Math.max(0, Math.min(100, Number(job.percent) || 0));
  panel.hidden = false;
  panel.classList.toggle('complete', job.status === 'completed');
  panel.classList.toggle('error', job.status === 'error');
  $('gu-fls-progress-title').textContent = job.message || 'Bulk FLS is running...';
  $('gu-fls-progress-percent').textContent = `${percent}%`;
  $('gu-fls-progress-bar').style.width = `${percent}%`;
  panel.querySelector('[role="progressbar"]').setAttribute('aria-valuenow', String(percent));
  $('gu-fls-progress-events').innerHTML = (job.events || []).map(event => `<li>${esc(event.message)}</li>`).join('');
  $('gu-fls-progress-events').scrollTop = $('gu-fls-progress-events').scrollHeight;
}

async function monitorFlsJob(statusUrl, startedAt) {
  const timer = setInterval(() => {
    $('gu-fls-progress-elapsed').textContent = `Elapsed: ${Math.max(0, Math.floor((Date.now() - startedAt) / 1000))}s`;
  }, 1000);
  try {
    while (true) {
      const job = await api(statusUrl);
      renderFlsProgress(job);
      if (job.status === 'completed') return job;
      if (job.status === 'error') throw new Error(job.error || job.message || 'Bulk FLS operation failed.');
      await wait(750);
    }
  } finally {
    clearInterval(timer);
    $('gu-fls-progress-elapsed').textContent = `Elapsed: ${Math.max(0, Math.floor((Date.now() - startedAt) / 1000))}s`;
  }
}

async function runBulkFls() {
  const button = $('gu-fls-run');
  try {
    const objectListPath = $('gu-fls-object-list').value.trim();
    const permissionSets = selectedValues($('gu-fls-permission-sets'));
    const profiles = selectedValues($('gu-fls-profiles'));
    if (!objectListPath) throw new Error('Enter the object list file path.');
    if (!permissionSets.length && !profiles.length) throw new Error('Select at least one Permission Set or Profile.');
    if (!confirm('Assign object, field, and record-type access in the Target Org for every object in this list?')) return;
    button.disabled = true;
    button.textContent = 'Bulk FLS running...';
    $('gu-fls-result').hidden = true;
    const startedAt = Date.now();
    renderFlsProgress({status: 'queued', percent: 0, message: 'Submitting bulk FLS operation...', events: []});
    const accepted = await api('/api/general-utility/bulk-fls', {
      method: 'POST',
      body: JSON.stringify({
        object_list_path: objectListPath,
        target_profile: $('gu-fls-profile').value || 'default',
        permission_sets: permissionSets,
        profiles,
      }),
    });
    $('gu-fls-result').hidden = false;
    $('gu-fls-result').className = 'gu-result ok';
    $('gu-fls-result').innerHTML = `Bulk FLS submitted for ${esc(accepted.object_count)} object(s). <a href="${esc(accepted.progress_url)}">Open persistent progress</a>`;
    const data = await monitorFlsJob(accepted.status_url, startedAt);
    const errorCount = Number(data.summary?.error_count || 0);
    $('gu-fls-result').innerHTML = `Bulk FLS completed for ${esc(accepted.object_count)} object(s)${errorCount ? ` with ${esc(errorCount)} object error(s)` : ''}. <a href="${esc(accepted.progress_url)}">Open run details</a>`;
    message(errorCount ? 'Bulk FLS finished with some object errors.' : 'Bulk FLS completed.', errorCount ? 'error' : 'ok');
  } catch (error) {
    $('gu-fls-progress').classList.add('error');
    message(error.message, 'error');
  } finally {
    button.disabled = false;
    button.textContent = 'Assign FLS and record types';
  }
}

$('gu-analyze').addEventListener('click', analyze);
$('gu-file').addEventListener('change', autoSelectObject);
$('gu-match-suffix').addEventListener('input', () => refreshMatchDefaults());
$('gu-preserve-body').addEventListener('input', event => {
  if (event.target.classList.contains('gu-preserve-name')) {
    const invalid = event.target.value.trim().length > 40;
    event.target.classList.toggle('gu-invalid-api-name', invalid);
    const source = event.target.closest('tr')?.querySelector('td:nth-child(2) code');
    if (source) source.classList.toggle('gu-invalid-api-name', invalid);
    refreshMatchDefaults();
  }
  if (event.target.classList.contains('gu-preserve-use')) {
    syncPreserveControls(event.target.closest('tr'));
    filterPreservationRows();
  }
  if (event.target.classList.contains('gu-match-use')) {
    const row = event.target.closest('tr');
    const preserve = row.querySelector('.gu-preserve-use');
    // Checking Create Match without Preserve is invalid; force Preserve on.
    if (event.target.checked && preserve && !preserve.checked) preserve.checked = true;
    syncMatchControls(row);
    filterPreservationRows();
  }
});
$('gu-preserve-filter').addEventListener('input', filterPreservationRows);
$('gu-preserve-type').addEventListener('change', filterPreservationRows);
$('gu-preserve-view').addEventListener('change', filterPreservationRows);
$('gu-refresh').addEventListener('click', async () => { try { await loadFiles(); await loadTargetContext(); message('Files, Target metadata, and access choices refreshed.', 'ok'); } catch (error) { message(error.message, 'error'); } });
$('gu-profile').addEventListener('change', () => {
  $('gu-fls-profile').value = $('gu-profile').value;
  loadTargetContext().catch(error => message(error.message, 'error'));
});
$('gu-fls-profile').addEventListener('change', () => {
  $('gu-profile').value = $('gu-fls-profile').value;
  loadTargetContext().catch(error => message(error.message, 'error'));
});
$('gu-fls-refresh').addEventListener('click', async () => {
  try {
    await loadFlsAccessPrincipals();
    message('Permission Sets and Profiles refreshed from the Target Org.', 'ok');
  } catch (error) {
    message(error.message, 'error');
  }
});
$('gu-fls-run').addEventListener('click', runBulkFls);
$('gu-inventory-mode').addEventListener('change', syncInventoryMode);
$('gu-run-fallback').addEventListener('click', runFallback);
$('gu-prepare-columns').addEventListener('click', prepareColumns);
$('gu-create-fields').addEventListener('click', createFields);
$('gu-concat-filter').addEventListener('input', filterConcatColumns);
$('gu-concat-select-all').addEventListener('click', () => { document.querySelectorAll('#gu-concat-columns label:not([hidden]) .gu-concat-column').forEach(input => { input.checked = true; }); updateConcatCount(); });
$('gu-concat-clear').addEventListener('click', () => { document.querySelectorAll('.gu-concat-column').forEach(input => { input.checked = false; }); updateConcatCount(); });

(async () => { try { await loadProfiles(); await Promise.all([loadFiles(), loadTargetContext()]); } catch (error) { $('gu-auth').className = 'auth-status error'; $('gu-auth').innerHTML = '<span class="dot"></span> Target unavailable'; message(error.message, 'error'); } })();
