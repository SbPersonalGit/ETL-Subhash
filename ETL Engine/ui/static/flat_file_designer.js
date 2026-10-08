const state = { batchId: null, files: [], objects: [], savedId: null, referenceTemplates: [], referenceRows: [], uploadedMappings: [], polymorphicContext: null };
const dropFileSelection = new Set();
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(
  /[&<>"']/g,
  char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[char]),
);

const PREVALIDATION_TEMPLATE_KEY = 'sf_etl_prevalidation_template_id';
function syncPreValidationTemplate(templateId = '') {
  const selected = templateId || $('ff-template-select').value || state.savedId || '';
  $('ff-pre-validation').href = selected ? `/validation?phase=pre&template_id=${encodeURIComponent(selected)}` : '/validation?phase=pre';
}

function rememberTemplate(templateId = '') {
  if (templateId) sessionStorage.setItem(PREVALIDATION_TEMPLATE_KEY, templateId);
  else sessionStorage.removeItem(PREVALIDATION_TEMPLATE_KEY);
  syncPreValidationTemplate(templateId);
}

function loadedTemplateLabel() {
  return ($('ff-template-name').value || '').trim() || state.savedId || '';
}

function clearWorkingPackage({ clearFiles = false } = {}) {
  state.savedId = null;
  $('ff-template-name').value = '';
  $('ff-template-select').value = '';
  $('ff-delete-template').disabled = true;
  $('ff-ingest').disabled = true;
  $('ff-add-pipeline').disabled = true;
  $('ff-allow-unmapped').checked = false;
  rememberTemplate('');
  if (clearFiles) {
    state.batchId = null;
    state.files = [];
    render();
  }
}

$('ff-template-select').onchange = () => {
  syncPreValidationTemplate($('ff-template-select').value);
  $('ff-delete-template').disabled = !$('ff-template-select').value;
};
$('ff-pre-validation').onclick = event => {
  const templateId = $('ff-template-select').value || state.savedId || '';
  if (!templateId) return;
  event.preventDefault();
  syncPreValidationTemplate(templateId);
  window.location.href = `/validation?phase=pre&template_id=${encodeURIComponent(templateId)}`;
};

function msg(text, kind = '') {
  const element = $('ff-message');
  element.textContent = text || '';
  element.className = `ff-message ${kind}`;
}

function validationErrorMsg(error) {
  const details = error.details || [];
  const visible = details.slice(0, 8);
  const remaining = details.slice(8);
  msg(`${error.message}${visible.length ? `\n${visible.map(detail => `• ${detail}`).join('\n')}` : ''}`, 'error');
  if (!remaining.length) return;

  const disclosure = document.createElement('details');
  disclosure.className = 'ff-validation-details';
  const summary = document.createElement('summary');
  summary.textContent = `Show ${remaining.length} more issue(s)`;
  const issueList = document.createElement('div');
  issueList.className = 'ff-validation-issue-list';
  issueList.textContent = remaining.map(detail => `• ${detail}`).join('\n');
  disclosure.append(summary, issueList);
  $('ff-message').appendChild(disclosure);
}

function showLoader(title, detail = 'Please wait until this operation is complete.') {
  $('ff-loader-title').textContent = title;
  $('ff-loader-detail').textContent = detail;
  $('ff-loader').classList.add('visible');
}

function hideLoader() {
  $('ff-loader').classList.remove('visible');
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const raw = await response.text();
  let data = {};
  try {
    data = raw ? JSON.parse(raw) : {};
  } catch (_error) {
    data = { error: raw && raw.length < 500 ? raw : '' };
  }
  if (!response.ok) {
    const error = new Error(data.error || `Request failed (${response.status}). Check logs/sf_etl.log for the server traceback.`);
    error.details = data.errors || [];
    error.errorCount = data.error_count || error.details.length;
    error.affectedFiles = data.affected_files || [];
    throw error;
  }
  return data;
}

async function checkTargetConnection(profile = 'default') {
  const element = $('ff-auth-status');
  element.className = 'auth-status checking';
  element.innerHTML = '<span class="dot"></span> Checking target org...';
  try {
    const data = await api(`/api/auth/check?mode=flat_file&org=target&profile=${encodeURIComponent(profile)}`);
    const url = String(data.instance_url || '').replace(/^https?:\/\//, '');
    element.className = 'auth-status ok';
    element.innerHTML = `<span class="dot"></span> Target: ${esc(url)}`;
  } catch (error) {
    element.className = 'auth-status error';
    element.innerHTML = `<span class="dot"></span> Target connection failed: ${esc(error.message)}`;
  }
}

function objectOptions(selected = '') {
  return '<option value="">Select target object</option>' + state.objects.map(object =>
    `<option value="${esc(object.name)}" ${object.name === selected ? 'selected' : ''}>${esc(object.label)} — ${esc(object.name)}</option>`,
  ).join('');
}

function externalIdOptions(file) {
  const values = file.externalIdFields || [];
  return '<option value="">Select external ID</option>' + values.map(value =>
    `<option value="${esc(value)}" ${value === file.external_id ? 'selected' : ''}>${esc(value)}</option>`,
  ).join('');
}

function lookupExternalIdControl(file, mapping, fileIndex, mappingIndex) {
  const targetMeta = lookupFieldMeta(file, mapping.target);
  const polymorphic = Boolean(
    mapping.is_polymorphic
    || (targetMeta?.type === 'reference' && (targetMeta.references_to || []).length > 1)
  );
  if (polymorphic) {
    // The current flat-file polymorphic contract accepts already-resolved
    // destination IDs. Persist the policy even when the single-option select
    // never fires a change event (for example when editing an older template).
    mapping.is_polymorphic = true;
    mapping.referenced_objects = mapping.referenced_objects || targetMeta?.references_to || [];
    mapping.lookup_resolution = mapping.lookup_resolution || 'target_id';
    if (mapping.lookup_resolution === 'target_id') mapping.lookup_external_id = '';
    const policy = mapping.lookup_resolution || 'target_id';
    const labels = { target_id: 'Target Salesforce IDs only', polymorphic_external_id: 'External IDs by object', polymorphic_target_first: 'Mixed: target ID first' };
    return `<button class="btn btn-secondary ff-poly-config" type="button" data-file="${fileIndex}" data-map="${mappingIndex}">${esc(labels[policy] || policy)}</button><div class="ff-file-meta">Polymorphic: ${mapping.polymorphic_mappings?.length || 0} object rule(s).</div>`;
  }
  if (!mapping.referenced_object) return '<span class="ff-file-meta">Not a lookup</span>';
  const parentIncluded = state.files.some(file =>
    file.object === mapping.referenced_object && !file.skip_staging
  );
  if (parentIncluded) {
    return `<span class="ff-file-meta">Auto from ${esc(mapping.referenced_object)} object</span>`;
  }
  const values = mapping.lookup_external_id_fields || [];
  if (!values.length) return '<span class="ff-file-meta">Preserve target Salesforce ID</span>';
  return `<select class="fh-input ff-lookup-external-id" data-file="${fileIndex}" data-map="${mappingIndex}">
    <option value="">Select lookup resolution</option>
    <option value="__target_id__" ${mapping.lookup_resolution === 'target_id' ? 'selected' : ''}>Preserve target Salesforce ID</option>
    ${values.map(value => `<option value="${esc(value)}" ${value === mapping.lookup_external_id ? 'selected' : ''}>${esc(mapping.referenced_object)}.${esc(value)}</option>`).join('')}
  </select>`;
}

function polyObjectOptions(selected = '') {
  return '<option value="">Select object</option>' + state.objects.map(item => {
    const name = item.name || '';
    return `<option value="${esc(name)}" ${name === selected ? 'selected' : ''}>${esc(item.label || name)} — ${esc(name)}</option>`;
  }).join('');
}

function renderPolymorphicRows() {
  const context = state.polymorphicContext;
  if (!context) return;
  const mapping = state.files[context.fileIndex].mappings[context.mappingIndex];
  const rules = mapping.polymorphic_mappings || [];
  $('ff-poly-body').innerHTML = rules.map((rule, index) => `<tr data-index="${index}">
    <td><select class="fh-input ff-poly-method"><option value="prefix" ${rule.identify_by === 'prefix' ? 'selected' : ''}>Salesforce ID prefix</option><option value="companion" ${rule.identify_by === 'companion' ? 'selected' : ''}>Companion column</option><option value="fixed" ${rule.identify_by === 'fixed' ? 'selected' : ''}>Fixed object</option></select></td>
    <td><input class="fh-input ff-poly-value" value="${esc(rule.match_value || '')}" placeholder="001 or Account"></td>
    <td><input class="fh-input ff-poly-column" value="${esc(rule.companion_column || '')}" placeholder="ObjectType column"></td>
    <td><select class="fh-input ff-poly-object">${polyObjectOptions(rule.referenced_object)}</select></td>
    <td><input class="fh-input ff-poly-external" list="ff-poly-ext-${index}" value="${esc(rule.external_id || '')}" placeholder="Select target External ID"><datalist id="ff-poly-ext-${index}">${(rule.external_id_fields || []).map(value => `<option value="${esc(value)}"></option>`).join('')}</datalist></td>
    <td><button class="btn btn-danger ff-poly-remove" type="button">Remove</button></td>
  </tr>`).join('') || '<tr><td colspan="6" class="ff-file-meta">Add at least one object mapping for external-ID or mixed resolution.</td></tr>';
  document.querySelectorAll('.ff-poly-remove').forEach(button => button.onclick = () => {
    mapping.polymorphic_mappings = collectPolymorphicRows();
    mapping.polymorphic_mappings.splice(+button.closest('tr').dataset.index, 1);
    renderPolymorphicRows();
  });
  document.querySelectorAll('.ff-poly-object').forEach(select => select.onchange = async () => {
    const row = select.closest('tr');
    const index = +row.dataset.index;
    if (!select.value) return;
    try {
      const profile = $('ff-target-profile').value || 'default';
      const data = await api(`/api/flat-files/reference-metadata?profile=${encodeURIComponent(profile)}&object=${encodeURIComponent(select.value)}`);
      rules[index].referenced_object = select.value;
      rules[index].external_id_fields = data.external_id_fields || [];
      if (row.querySelector('.ff-poly-method').value === 'prefix' && data.key_prefix) row.querySelector('.ff-poly-value').value = data.key_prefix;
      const ext = row.querySelector('.ff-poly-external');
      if (!ext.value && data.external_id_fields?.length) ext.value = data.external_id_fields.includes('External_Id__c') ? 'External_Id__c' : data.external_id_fields[0];
      const list = row.querySelector('datalist');
      list.innerHTML = (data.external_id_fields || []).map(value => `<option value="${esc(value)}"></option>`).join('');
    } catch (error) { msg(error.message, 'error'); }
  });
}

function openPolymorphicDialog(fileIndex, mappingIndex) {
  state.polymorphicContext = { fileIndex, mappingIndex };
  const mapping = state.files[fileIndex].mappings[mappingIndex];
  mapping.polymorphic_mappings ||= [];
  $('ff-poly-title').textContent = `Configure ${mapping.target || mapping.source}`;
  $('ff-poly-policy').value = mapping.lookup_resolution || 'target_id';
  togglePolymorphicRuleEditor();
  renderPolymorphicRows();
  $('ff-polymorphic-dialog').showModal();
}

function togglePolymorphicRuleEditor() {
  const targetOnly = $('ff-poly-policy').value === 'target_id';
  $('ff-poly-rules').hidden = targetOnly;
}

function collectPolymorphicRows() {
  return [...document.querySelectorAll('#ff-poly-body tr[data-index]')].map(row => ({
    identify_by: row.querySelector('.ff-poly-method').value,
    match_value: row.querySelector('.ff-poly-value').value.trim(),
    companion_column: row.querySelector('.ff-poly-column').value.trim(),
    referenced_object: row.querySelector('.ff-poly-object').value.trim(),
    external_id: row.querySelector('.ff-poly-external').value.trim(),
  }));
}

function scanFlatReferenceRows() {
  const existing = new Map(state.referenceRows.map(row => [row.object, row]));
  const byObject = new Map([...existing.entries()].map(([object, row]) => [object, {
    ...row,
    columns: new Set(row.columns || []),
    fields: [...(row.fields || [])],
  }]));
  for (const file of state.files) for (const mapping of (file.mappings || [])) {
    if (mapping.ignored || !mapping.referenced_object) continue;
    const previous = existing.get(mapping.referenced_object) || {};
    const row = byObject.get(mapping.referenced_object) || {
      object: mapping.referenced_object, columns: new Set(previous.columns || []), fields: previous.fields || mapping.lookup_external_id_fields || [], target_external_id: previous.target_external_id || mapping.lookup_external_id || '',
    };
    row.columns.add(mapping.source);
    row.fields = [...new Set([...row.fields, ...(mapping.lookup_external_id_fields || [])])];
    byObject.set(row.object, row);
  }
  state.referenceRows = [...byObject.values()].map(row => ({ ...row, columns: [...row.columns] }));
}

function renderFlatReferenceDialog() {
  $('ff-reference-body').innerHTML = state.referenceRows.map((row, index) => {
    const columns = row.columns || [];
    const preview = columns.slice(0, 6).join(', ') + (columns.length > 6 ? `, +${columns.length - 6} more` : '');
    return `<tr><td>${esc(row.object)}</td><td><span class="ff-reference-columns" title="${esc(columns.join(', '))}">${esc(preview || 'Loaded from template')}</span></td><td><select class="fh-input ff-reference-key" data-index="${index}"><option value="">Select target key</option>${row.fields.map(field => `<option value="${esc(field)}" ${field === row.target_external_id ? 'selected' : ''}>${esc(field)}</option>`).join('')}</select></td></tr>`;
  }).join('') || '<tr><td colspan="3">No referenced lookup objects loaded. Map lookup columns and click Load all referenced objects, or load a saved template.</td></tr>';
  document.querySelectorAll('.ff-reference-key').forEach(select => select.onchange = event => {
    state.referenceRows[+event.target.dataset.index].target_external_id = event.target.value;
  });
  $('ff-reference-load').innerHTML = (state.referenceTemplates.length
    ? '<option value="">Load Reference Identity template</option>'
    : '<option value="">No saved Reference Identity templates</option>')
    + state.referenceTemplates.map(template => `<option value="${esc(template.id)}">${esc(template.name)}</option>`).join('');
}

function applyFlatReferenceRows() {
  const keys = new Map(state.referenceRows.map(row => [row.object, row.target_external_id]));
  for (const file of state.files) for (const mapping of (file.mappings || [])) {
    const key = keys.get(mapping.referenced_object);
    if (!key) continue;
    mapping.lookup_resolution = 'object_template';
    mapping.lookup_external_id = key;
  }
  render();
}

function fieldSuggestions(fields) {
  return fields.map(field =>
    `<option value="${esc(field.name)}">${esc(field.label)} — ${esc(field.type)}</option>`,
  ).join('');
}

function fieldIndexByName(file) {
  const index = new Map();
  for (const field of file.all_fields || []) {
    if (field?.name) index.set(String(field.name).toLowerCase(), field);
  }
  for (const field of file.fields || []) {
    if (field?.name) index.set(String(field.name).toLowerCase(), field);
  }
  return index;
}

function lookupFieldMeta(file, apiName) {
  const name = String(apiName || '').trim();
  if (!name) return null;
  return fieldIndexByName(file).get(name.toLowerCase()) || null;
}

function mappingFieldType(file, mapping) {
  if (mapping.target) {
    const targetMeta = lookupFieldMeta(file, mapping.target);
    if (targetMeta?.type) return targetMeta.type;
  }
  if (mapping.corresponding_type) return mapping.corresponding_type;
  const corresponding = lookupFieldMeta(file, mapping.source);
  return corresponding?.type || '';
}

function correspondingFieldPresence(file, mapping) {
  if (typeof mapping.corresponding_exists === 'boolean') {
    return {
      exists: mapping.corresponding_exists,
      writable: Boolean(mapping.corresponding_writable),
      type: mapping.corresponding_type || '',
    };
  }
  const corresponding = lookupFieldMeta(file, mapping.source);
  if (!corresponding) return { exists: false, writable: false, type: '' };
  const writable = typeof corresponding.writable === 'boolean'
    ? corresponding.writable
    : (file.fields || []).some(field =>
      String(field.name).toLowerCase() === String(corresponding.name).toLowerCase()
    );
  return {
    exists: true,
    writable,
    type: corresponding.type || '',
  };
}

function targetPresenceBadge(file, mapping) {
  const presence = correspondingFieldPresence(file, mapping);
  if (presence.exists && presence.writable) {
    return '<span class="ff-target-presence ff-target-present" title="A field with this API name exists and is writable on the target object.">✓ In target</span>';
  }
  if (presence.exists) {
    return '<span class="ff-target-presence ff-target-readonly" title="A field with this API name exists on the target object, but it is not writable.">✓ Exists (read-only)</span>';
  }
  return '<span class="ff-target-presence ff-target-missing" title="No field with this API name exists on the target object. That is why it was not auto-mapped.">✗ Not in target</span>';
}

function mappingSummary(file) {
  const mappings = file.mappings || [];
  const mapped = mappings.filter(mapping => !mapping.ignored && mapping.target).length;
  const unmapped = mappings.filter(mapping => !mapping.ignored && !mapping.target).length;
  return mappings.length
    ? `${mapped} mapped${unmapped ? ` · ${unmapped} unmapped` : ''}`
    : 'Mapping not started';
}

function siblingFileIndexes(index) {
  const objectName = String(state.files[index]?.object || '').trim();
  if (!objectName) return [];
  return state.files
    .map((file, otherIndex) => ({ file, otherIndex }))
    .filter(({ file, otherIndex }) => otherIndex !== index && String(file.object || '').trim() === objectName)
    .map(({ otherIndex }) => otherIndex);
}

function detectionBadge(file) {
  if (!file.object_auto_detected) return '';
  const source = String(file.object_detection_source || '').trim();
  if (source === 'folder') return '<span class="ff-detected">object from folder</span>';
  if (source === 'filename') return '<span class="ff-detected">object from filename</span>';
  return '<span class="ff-detected">object detected</span>';
}

function dropFolderOf(file) {
  const name = String(file?.source_path || file?.file || '').replace(/\\/g, '/');
  const parts = name.split('/').filter(Boolean);
  if (parts.length < 2) return '';
  return parts.slice(0, -1).join('/');
}

function folderGroupKey(file, index) {
  return dropFolderOf(file) || `__object__:${file.object || index}`;
}

function filePathOf(file) {
  return String(file?.source_path || file?.file || '').replace(/\\/g, '/');
}

function prepareFolderSamples(files) {
  const groups = new Map();
  files.forEach((file, index) => {
    const key = folderGroupKey(file, index);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(index);
  });
  for (const indexes of groups.values()) {
    if (indexes.length === 1) {
      files[indexes[0]].mapping_ui_hidden = false;
      continue;
    }
    const pointedTo = indexes.find(index => {
      const samplePath = filePathOf(files[index]);
      const sampleName = String(files[index].file || '');
      return indexes.some(other => {
        const template = String(files[other].mapping_template_file || '');
        return template && (template === samplePath || template === sampleName);
      });
    });
    const sampleIndex = pointedTo
      ?? indexes.find(index => !files[index].mapping_template_file)
      ?? indexes[0];
    const samplePath = filePathOf(files[sampleIndex]);
    indexes.forEach(index => {
      const isSample = index === sampleIndex;
      files[index].mapping_ui_hidden = !isSample;
      files[index].collapsed = !isSample;
      if (!isSample) files[index].mapping_template_file = files[index].mapping_template_file || samplePath;
    });
  }
}

function sampleFileIndexes() {
  return state.files
    .map((file, index) => (file.mapping_ui_hidden ? -1 : index))
    .filter(index => index >= 0);
}

function hiddenFolderFiles(index) {
  const file = state.files[index];
  const folder = dropFolderOf(file);
  const objectName = String(file.object || '').trim();
  return state.files.filter((other, otherIndex) => {
    if (otherIndex === index || !other.mapping_ui_hidden) return false;
    if (folder) return dropFolderOf(other) === folder;
    return objectName && String(other.object || '').trim() === objectName;
  });
}

function folderFileList(index) {
  const hidden = hiddenFolderFiles(index);
  if (!hidden.length) return '';
  const names = hidden.map(file => esc(file.file || file.source_path)).join(', ');
  return `<div class="ff-folder-file-list"><strong>${hidden.length} other file(s) in this template use this mapping:</strong> ${names}</div>`;
}

function folderMappingNote(index) {
  const file = state.files[index];
  const folder = dropFolderOf(file);
  const hidden = hiddenFolderFiles(index);
  if (file.mapping_template_file && file.mapping_ui_hidden) {
    return `<div class="ff-sibling-note">Using mapping from <strong>${esc(file.mapping_template_file)}</strong>.</div>`;
  }
  if (hidden.length) {
    return `<div class="ff-sibling-note">Showing one sample file. <strong>${hidden.length + 1}</strong> files in <strong>${esc(folder || file.object || 'this package')}</strong> stay in the template and use this mapping. Save to keep changes.</div>`;
  }
  if (folder) {
    return `<div class="ff-sibling-note">Map this one sample file, then apply it to the other CSVs in <strong>${esc(folder)}</strong>. You do not need to load every file into this page.</div>`;
  }
  return siblingNote(index);
}

function siblingNote(index) {
  const siblings = siblingFileIndexes(index);
  if (!siblings.length) return '';
  const objectName = state.files[index].object;
  return `<div class="ff-sibling-note">${siblings.length + 1} files share object <strong>${esc(objectName)}</strong> — they use one mapping template. Unmapping, ignoring, or changing a field on this file updates the same column on every sibling. Save the template to keep it.</div>`;
}

function cloneMappingPolicy(shared, mapping) {
  return {
    ...mapping,
    target: shared.target,
    ignored: shared.ignored,
    status: shared.status,
    unmapped_reason: shared.unmapped_reason,
    corresponding_exists: shared.corresponding_exists,
    corresponding_type: shared.corresponding_type,
    corresponding_writable: shared.corresponding_writable,
    referenced_object: shared.referenced_object,
    lookup_external_id: shared.lookup_external_id,
    lookup_external_id_fields: shared.lookup_external_id_fields,
    lookup_resolution: shared.lookup_resolution,
    is_polymorphic: shared.is_polymorphic,
    referenced_objects: [...(shared.referenced_objects || [])],
    polymorphic_mappings: (shared.polymorphic_mappings || []).map(rule => ({
      ...rule,
      external_id_fields: [...(rule.external_id_fields || [])],
    })),
    value_policy: shared.value_policy,
  };
}

function cloneMappingsForColumns(source, columns) {
  const bySource = new Map();
  for (const mapping of source.mappings || []) {
    const key = String(mapping.source || '').trim().toLocaleLowerCase();
    if (key) bySource.set(key, mapping);
  }
  const headers = (columns || [])
    .map(column => String(column || '').trim())
    .filter(Boolean);
  if (!headers.length) {
    return (source.mappings || []).map(mapping => cloneMappingPolicy(mapping, { ...mapping }));
  }
  return headers.map(header => {
    const shared = bySource.get(header.toLocaleLowerCase());
    if (shared) return cloneMappingPolicy(shared, { source: header });
    return {
      source: header,
      target: null,
      ignored: true,
      status: 'ignored',
      unmapped_reason: 'Column is not in the shared folder mapping template.',
    };
  });
}

function propagateObjectTemplate(sourceIndex) {
  const source = state.files[sourceIndex];
  if (!source?.object || !Array.isArray(source.mappings) || !source.mappings.length) return 0;
  const bySource = new Map();
  for (const mapping of source.mappings) {
    const key = String(mapping.source || '').trim();
    if (key) bySource.set(key, mapping);
    const folded = key.toLocaleLowerCase();
    if (folded && !bySource.has(folded)) bySource.set(folded, mapping);
  }
  let updated = 0;
  for (const otherIndex of siblingFileIndexes(sourceIndex)) {
    const target = state.files[otherIndex];
    target.object = target.object || source.object;
    target.external_id = source.external_id;
    target.externalIdFields = source.externalIdFields;
    target.fields = source.fields;
    target.all_fields = source.all_fields;
    target.recordTypes = source.recordTypes;
    target.access_issues = source.access_issues;
    target.record_type_mappings = { ...(source.record_type_mappings || {}) };
    const columns = target.columns
      || (Array.isArray(target.mappings) ? target.mappings.map(item => item.source) : []);
    if (!Array.isArray(target.mappings) || target.mappings.length === 0) {
      target.mappings = cloneMappingsForColumns(source, columns);
    } else {
      target.mappings = target.mappings.map(mapping => {
        const sourceName = String(mapping.source || '').trim();
        const shared = bySource.get(sourceName) || bySource.get(sourceName.toLocaleLowerCase());
        if (shared) return cloneMappingPolicy(shared, mapping);
        return mapping;
      });
    }
    target.cleaned_name = null;
    target.cleaning_result = null;
    updated += 1;
  }
  return updated;
}

function propagateSharedColumn(sourceIndex, sourceColumn) {
  const source = state.files[sourceIndex];
  const shared = (source.mappings || []).find(mapping => mapping.source === sourceColumn);
  if (!shared) return 0;
  let updated = 0;
  for (const otherIndex of siblingFileIndexes(sourceIndex)) {
    const target = state.files[otherIndex];
    const mapping = (target.mappings || []).find(item => item.source === sourceColumn);
    if (!mapping) continue;
    Object.assign(mapping, cloneMappingPolicy(shared, mapping));
    target.cleaned_name = null;
    target.cleaning_result = null;
    updated += 1;
  }
  return updated;
}

function syncSharedObjectTemplates() {
  const seen = new Set();
  for (let index = 0; index < state.files.length; index += 1) {
    const objectName = String(state.files[index].object || '').trim();
    if (!objectName || seen.has(objectName) || !state.files[index].mappings) continue;
    seen.add(objectName);
    propagateObjectTemplate(index);
  }
}

function csvEscape(value) {
  const text = String(value ?? '');
  if (/[",\n\r]/.test(text)) return `"${text.replace(/"/g, '""')}"`;
  return text;
}

function safeDownloadStem(value, fallback = 'mapping') {
  const cleaned = String(value || '')
    .replace(/[\\/:*?"<>|]+/g, '_')
    .replace(/\s+/g, '_')
    .replace(/_+/g, '_')
    .replace(/^\.+|\.+$/g, '')
    .slice(0, 80);
  return cleaned || fallback;
}

function presenceExportLabel(file, mapping) {
  const presence = correspondingFieldPresence(file, mapping);
  if (presence.exists && presence.writable) return 'in target';
  if (presence.exists) return 'exists (read-only)';
  return 'not in target';
}

function mappingExportRows(files) {
  const rows = [];
  for (const file of files) {
    for (const mapping of file.mappings || []) {
      rows.push({
        object: file.object || '',
        file: file.file || '',
        source_column: mapping.source || '',
        field_type: mappingFieldType(file, mapping) || '',
        exists_in_target: presenceExportLabel(file, mapping),
        target_field: mapping.target || '',
        included: mapping.ignored ? 'no' : 'yes',
        status: mapping.status || '',
        unmapped_reason: mapping.unmapped_reason || '',
        referenced_object: mapping.referenced_object || '',
        lookup_resolution: mapping.lookup_resolution || '',
        lookup_external_id: mapping.lookup_external_id || '',
        object_external_id: file.external_id || '',
        detection_source: file.object_detection_source || '',
      });
    }
  }
  return rows;
}

function parseCsvText(text) {
  const rows = [];
  let row = [];
  let cell = '';
  let inQuotes = false;
  const src = String(text || '').replace(/^\ufeff/, '');
  for (let i = 0; i < src.length; i += 1) {
    const ch = src[i];
    if (inQuotes) {
      if (ch === '"') {
        if (src[i + 1] === '"') {
          cell += '"';
          i += 1;
        } else {
          inQuotes = false;
        }
      } else {
        cell += ch;
      }
    } else if (ch === '"') {
      inQuotes = true;
    } else if (ch === ',') {
      row.push(cell);
      cell = '';
    } else if (ch === '\n') {
      row.push(cell);
      rows.push(row);
      row = [];
      cell = '';
    } else if (ch !== '\r') {
      cell += ch;
    }
  }
  if (cell.length || row.length) {
    row.push(cell);
    rows.push(row);
  }
  return rows.filter(item => item.some(value => String(value).trim()));
}

function normalizeMappingKey(value) {
  return String(value || '').replace(/[\t\r\n]+/g, '').trim().toLowerCase();
}

function parseMappingCsv(text) {
  const table = parseCsvText(text);
  if (!table.length) throw new Error('The mapping CSV is empty.');
  const headers = table[0].map(header => normalizeMappingKey(header).replace(/ /g, '_'));
  const sourceIndex = headers.indexOf('source_column');
  const targetIndex = headers.indexOf('target_field');
  if (sourceIndex < 0 || targetIndex < 0) {
    throw new Error('Mapping CSV must include source_column and target_field columns.');
  }
  const indexOf = name => headers.indexOf(name);
  return table.slice(1).map(cells => ({
    object: String(cells[indexOf('object')] || '').trim(),
    file: String(cells[indexOf('file')] || '').trim(),
    source_column: String(cells[sourceIndex] || '').replace(/[\t]+/g, '').trim(),
    target_field: String(cells[targetIndex] || '').replace(/[\t]+/g, '').trim(),
    included: String(cells[indexOf('included')] || 'yes').trim().toLowerCase(),
    status: String(cells[indexOf('status')] || '').trim(),
    unmapped_reason: String(cells[indexOf('unmapped_reason')] || '').trim(),
    referenced_object: String(cells[indexOf('referenced_object')] || '').trim(),
    lookup_resolution: String(cells[indexOf('lookup_resolution')] || '').trim(),
    lookup_external_id: String(cells[indexOf('lookup_external_id')] || '').trim(),
    object_external_id: String(cells[indexOf('object_external_id')] || '').trim(),
  })).filter(row => row.source_column);
}

function applyUploadedMappingsToFiles() {
  const rows = state.uploadedMappings || [];
  if (!rows.length || !state.files.length) return { files: 0, columns: 0 };
  const byObjectAndSource = new Map();
  const bySource = new Map();
  for (const row of rows) {
    const source = normalizeMappingKey(row.source_column);
    if (!source) continue;
    const objectName = normalizeMappingKey(row.object);
    if (objectName) byObjectAndSource.set(`${objectName}::${source}`, row);
    bySource.set(source, row);
  }
  let filesTouched = 0;
  let columns = 0;
  for (const file of state.files) {
    let touched = false;
    const objectName = normalizeMappingKey(file.object);
    const objectExtIds = new Set();
    for (const mapping of file.mappings || []) {
      const source = normalizeMappingKey(mapping.source);
      const row = (objectName && byObjectAndSource.get(`${objectName}::${source}`)) || bySource.get(source);
      if (!row) continue;
      const target = String(row.target_field || '').trim();
      const included = String(row.included || 'yes').trim().toLowerCase();
      mapping.target = target || null;
      mapping.ignored = included === 'no' || included === 'false' || included === 'n';
      mapping.status = mapping.ignored
        ? 'ignored'
        : (row.status || (mapping.target ? 'user_mapped' : 'unmapped'));
      if (row.referenced_object) mapping.referenced_object = row.referenced_object;
      if (row.lookup_resolution) mapping.lookup_resolution = row.lookup_resolution;
      if (row.lookup_external_id) mapping.lookup_external_id = row.lookup_external_id;
      if (row.unmapped_reason) mapping.unmapped_reason = row.unmapped_reason;
      if (row.object_external_id) objectExtIds.add(row.object_external_id);
      file.cleaned_name = null;
      touched = true;
      columns += 1;
    }
    if (objectExtIds.size === 1) {
      applyExternalIdToFile(file, [...objectExtIds][0]);
    }
    if (touched) filesTouched += 1;
  }
  return { files: filesTouched, columns };
}

function downloadMappingCsv(filename, files) {
  const rows = mappingExportRows(files);
  if (!rows.length) {
    msg('No field mappings to download yet. Map at least one file first.', 'error');
    return false;
  }
  const headers = [
    'object',
    'file',
    'source_column',
    'field_type',
    'exists_in_target',
    'target_field',
    'included',
    'status',
    'unmapped_reason',
    'referenced_object',
    'lookup_resolution',
    'lookup_external_id',
    'object_external_id',
    'detection_source',
  ];
  const lines = [headers.join(',')];
  for (const row of rows) {
    lines.push(headers.map(header => csvEscape(row[header])).join(','));
  }
  const blob = new Blob([`\ufeff${lines.join('\r\n')}\r\n`], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
  return true;
}

function downloadAllMappingsCsv() {
  const name = safeDownloadStem($('ff-template-name').value || state.savedId || 'flat_file_package');
  if (downloadMappingCsv(`${name}_field_mappings.csv`, state.files)) {
    msg(`Downloaded field mappings for ${state.files.length} file(s).`, 'ok');
  }
}

function downloadMappingsByObject() {
  const byObject = new Map();
  for (const file of state.files) {
    const objectName = String(file.object || '').trim() || '_unassigned';
    if (!byObject.has(objectName)) byObject.set(objectName, []);
    byObject.get(objectName).push(file);
  }
  let downloaded = 0;
  const packageName = safeDownloadStem($('ff-template-name').value || state.savedId || 'flat_file_package');
  for (const [objectName, files] of byObject) {
    if (downloadMappingCsv(`${packageName}_${safeDownloadStem(objectName, 'object')}_field_mappings.csv`, files)) {
      downloaded += 1;
    }
  }
  if (downloaded) {
    msg(`Downloaded ${downloaded} object-wise mapping CSV file(s).`, 'ok');
  }
}

function filteredMappings(file) {
  const filter = file.mapping_filter || 'all';
  const search = String(file.mapping_search || '').trim().toLocaleLowerCase();
  return (file.mappings || [])
    .map((mapping, index) => ({ mapping, index }))
    .filter(({ mapping }) => {
      if (filter === 'unmapped') return !mapping.ignored && !mapping.target;
      if (filter === 'mapped') return !mapping.ignored && Boolean(mapping.target);
      if (filter === 'ignored') return Boolean(mapping.ignored);
      return true;
    })
    .filter(({ mapping }) => {
      if (!search) return true;
      const field = lookupFieldMeta(file, mapping.target) || {};
      const presence = correspondingFieldPresence(file, mapping);
      const presenceLabel = presence.exists
        ? (presence.writable ? 'in target exists writable' : 'exists read-only')
        : 'not in target missing';
      const searchable = [
        mapping.source,
        mapping.target,
        field.label,
        field.type,
        mappingFieldType(file, mapping),
        presence.type,
        presenceLabel,
        field.required ? 'required' : '',
        mapping.referenced_object,
        mapping.lookup_external_id,
        mapping.lookup_resolution,
        mapping.status,
        mapping.unmapped_reason,
        mapping.ignored ? 'ignored excluded unchecked' : 'included checked',
      ].filter(Boolean).join(' ').toLocaleLowerCase();
      return searchable.includes(search);
    });
}

function mappingFilterControl(file, fileIndex) {
  const filter = file.mapping_filter || 'all';
  const visible = filteredMappings(file).length;
  const total = (file.mappings || []).length;
  return `<div class="ff-mapping-tools">
    <label class="ff-text-filter-label">Search all mapping columns
      <input type="search" class="fh-input ff-text-filter" data-index="${fileIndex}"
             value="${esc(file.mapping_search || '')}"
             placeholder="API name, label, type, in target, status..." />
    </label>
    <label class="ff-mapping-filter">Mapping view
    <select class="fh-input ff-filter" data-index="${fileIndex}">
      <option value="all" ${filter === 'all' ? 'selected' : ''}>All fields (${total})</option>
      <option value="unmapped" ${filter === 'unmapped' ? 'selected' : ''}>Unmapped only</option>
      <option value="mapped" ${filter === 'mapped' ? 'selected' : ''}>Mapped only</option>
      <option value="ignored" ${filter === 'ignored' ? 'selected' : ''}>Ignored only</option>
    </select>
    <span class="ff-file-meta">${visible} visible</span>
    </label>
  </div>`;
}

function applyExternalIdToFile(file, externalId) {
  file.external_id = externalId;
  const mappings = file.mappings || [];
  const idMapping = mappings.find(mapping =>
    String(mapping.source || '').trim().toLowerCase() === 'id'
  );
  if (!idMapping) return { applied: true, visible: false };
  if (!externalId) {
    idMapping.target = 'Id';
    idMapping.status = 'auto_mapped';
    idMapping.ignored = false;
    return { applied: true, visible: true };
  }

  const conflict = mappings.find(mapping =>
    mapping !== idMapping
    && !mapping.ignored
    && String(mapping.target || '').trim() === externalId
  );
  if (conflict) {
    return { applied: false, conflict: conflict.source };
  }
  idMapping.target = externalId;
  idMapping.status = 'external_id_mapped';
  idMapping.ignored = false;
  return { applied: true, visible: true };
}

function recordTypeControls(file, fileIndex) {
  const sourceValues = file.record_type_values || [];
  if (!sourceValues.length) return '';
  const targetTypes = file.recordTypes || [];
  return `<div class="ff-record-types">
    <h3>Record Type mapping</h3>
    <p>Source RecordTypeIds are org-specific. Map each distinct source value to an active target Record Type.</p>
    ${sourceValues.map(sourceValue => `<label>
      <code>${esc(sourceValue)}</code><span>→</span>
      <select class="fh-input ff-record-type" data-file="${fileIndex}" data-value="${esc(sourceValue)}">
        <option value="">Select target Record Type</option>
        ${targetTypes.map(recordType => `<option value="${esc(recordType.id)}" ${file.record_type_mappings?.[sourceValue] === recordType.id ? 'selected' : ''}>${esc(recordType.name)} — ${esc(recordType.developer_name)}</option>`).join('')}
      </select>
    </label>`).join('')}
  </div>`;
}

function mappingTable(file, fileIndex) {
  const listId = `ff-fields-${fileIndex}`;
  const visibleMappings = filteredMappings(file);
  return `<div class="ff-table-wrap">
    <datalist id="${listId}">${fieldSuggestions(file.fields || [])}</datalist>
    <table class="ff-table">
      <thead><tr><th>Include</th><th>File column</th><th>Type</th><th>In target</th><th>Target Salesforce field</th><th>Lookup reference external ID</th><th>Value policy</th><th>Status</th></tr></thead>
      <tbody>${visibleMappings.length ? visibleMappings.map(({ mapping, index: mappingIndex }) => {
        const fieldType = mappingFieldType(file, mapping);
        return `<tr>
        <td><input type="checkbox" class="ff-include" data-file="${fileIndex}" data-map="${mappingIndex}" ${mapping.ignored ? '' : 'checked'} title="Include or exclude this field from the migration template"></td>
        <td><code>${esc(mapping.source)}</code></td>
        <td class="ff-field-type">${fieldType ? `<code>${esc(fieldType)}</code>` : '<span class="ff-file-meta">—</span>'}</td>
        <td>${targetPresenceBadge(file, mapping)}</td>
        <td><input class="fh-input ff-target-field" list="${listId}" data-file="${fileIndex}" data-map="${mappingIndex}" value="${esc(mapping.target || '')}" placeholder="Field API name" ${mapping.ignored ? 'disabled' : ''}>${isRequiredTarget(file, mapping.target) ? '<span class="ff-required-badge">Required</span>' : ''}</td>
        <td>${lookupExternalIdControl(file, mapping, fileIndex, mappingIndex)}</td>
        <td>${valuePolicyControl(file, mapping, fileIndex, mappingIndex)}</td>
        <td class="ff-status-${esc(mapping.status)}">${esc(String(mapping.status || '').replaceAll('_', ' '))}${mapping.unmapped_reason ? `<div class="ff-file-meta">${esc(mapping.unmapped_reason)}</div>` : ''}</td>
      </tr>`;
      }).join('') : '<tr><td colspan="8" class="ff-empty-filter">No fields match this view.</td></tr>'}</tbody>
    </table>
  </div>`;
}

function isRequiredTarget(file, targetName) {
  return Boolean((file.fields || []).find(field =>
    field.name === targetName && field.required
  ));
}

function requiredFieldsPanel(file) {
  if (file.skip_staging) {
    return '<div class="ff-required-panel"><strong>Staging:</strong> This file is excluded. Mapping and validation are bypassed.</div>';
  }
  const required = (file.fields || []).filter(field => field.required && field.name !== 'Id');
  if (!required.length) {
    return '<details class="ff-required-panel"><summary><strong>Required target fields:</strong> None without target defaults.</summary></details>';
  }
  const mappedTargets = new Set(
    (file.mappings || [])
      .filter(mapping => !mapping.ignored && mapping.target)
      .map(mapping => mapping.target)
  );
  const missing = required.filter(field => !mappedTargets.has(field.name));
  const accessIssues = file.access_issues || [];
  const accessIssueFields = new Set(accessIssues.map(issue => issue.field));
  const accessPanel = accessIssues.length ? `<div class="ff-required-warning">
    <strong>Migration-user access problem</strong>
    ${accessIssues.map(issue => `<div>${esc(issue.message)}</div>`).join('')}
  </div>` : '';
  const missingPanel = missing.length
    ? accessIssues.length
      ? `<div class="ff-required-warning">${missing.length} required field(s) cannot currently be mapped because target field access or dependency validation failed. Resolve the migration-user access problem shown above, then refresh metadata and auto-map again.</div>`
      : `<div class="ff-required-warning">${missing.length} required field(s) are not mapped. Add mappings before validation; otherwise rows will be rejected.</div>`
    : '';
  return `<details class="ff-required-panel">
    <summary><strong>Required target fields (${required.length})</strong><span class="ff-required-summary">${missing.length ? `${missing.length} not mapped` : 'all mapped'}</span></summary>
    <div class="ff-required-content"><div class="ff-required-list">${required.map(field =>
      `<span class="${mappedTargets.has(field.name) ? 'mapped' : 'missing'}">${esc(field.name)} — ${mappedTargets.has(field.name) ? 'mapped' : accessIssueFields.has(field.name) ? 'blocked by migration-user access' : 'not mapped'}</span>`
    ).join('')}</div>
    ${accessPanel}
    ${missingPanel}
    </div>
  </details>`;
}

function render() {
  $('ff-files-panel').innerHTML = state.files.map((file, index) => {
    if (file.mapping_ui_hidden) return '';
    return `<section class="ff-card ${file.collapsed ? 'ff-collapsed' : ''}">
    <div class="ff-file-header">
      <div>
        <div class="ff-file-name">${esc(file.file)} ${detectionBadge(file)}</div>
        <div class="ff-file-meta">${(file.columns || file.mappings || []).length} columns · ${Number(file.size || 0).toLocaleString()} bytes · ${esc(mappingSummary(file))}</div>
        ${folderMappingNote(index)}
        ${folderFileList(index)}
      </div>
      <label>Target Salesforce object<select class="fh-input ff-object" data-index="${index}">${objectOptions(file.object)}</select></label>
      <label>Object external ID<select class="fh-input ff-external-id" data-index="${index}" ${file.externalIdFields ? '' : 'disabled'}>${externalIdOptions(file)}</select></label>
      <div class="ff-file-actions">
        <label><input type="checkbox" class="ff-skip-staging" data-index="${index}" ${file.skip_staging ? 'checked' : ''}> Skip file from staging</label>
        ${file.mappings ? mappingFilterControl(file, index) : ''}
        <button class="btn btn-secondary ff-collapse" data-index="${index}" aria-expanded="${file.collapsed ? 'false' : 'true'}">${file.collapsed ? 'Expand' : 'Collapse'}</button>
        <button class="btn btn-secondary ff-auto" data-index="${index}" ${file.object ? '' : 'disabled'}>Auto-map API names</button>
        ${file.mappings ? `<button class="btn btn-secondary ff-download-file-mapping" data-index="${index}" type="button">Download file mapping</button>` : ''}
        ${file.mappings && file.object ? `<button class="btn btn-secondary ff-download-object-mapping" data-index="${index}" type="button">Download object mapping</button>` : ''}
        ${siblingFileIndexes(index).length ? `<button class="btn btn-secondary ff-apply-siblings" data-index="${index}">Use this mapping for all ${siblingFileIndexes(index).length + 1} ${esc(state.files[index].object || 'object')} files</button>` : ''}
        ${dropFolderOf(file) && file.mappings && !file.mapping_template_file ? `<button class="btn btn-primary ff-apply-folder" data-index="${index}">Apply this mapping to all CSVs in ${esc(dropFolderOf(file))}</button>` : ''}
      </div>
    </div>
    <div class="ff-file-body" ${file.collapsed ? 'hidden' : ''}>${file.mappings ? requiredFieldsPanel(file) + recordTypeControls(file, index) + mappingTable(file, index) : '<p>Select an object to create the initial mapping.</p>'}</div>
  </section>`;
  }).join('');

  $('ff-actions').hidden = !state.files.length;
  $('ff-package-policy').hidden = !state.files.length;
  const externalIds = [...new Set(state.files.flatMap(file => file.externalIdFields || []))].sort();
  $('ff-external-id-suggestions').innerHTML = externalIds.map(value => `<option value="${esc(value)}"></option>`).join('');
  bind();
}

function bind() {
  document.querySelectorAll('.ff-skip-staging').forEach(element => {
    element.onchange = event => {
      const file = state.files[+event.target.dataset.index];
      file.skip_staging = event.target.checked;
      file.cleaned_name = null;
      file.rejected_name = null;
      file.cleaning_result = null;
      state.savedId = null;
      $('ff-ingest').disabled = true;
      $('ff-add-pipeline').disabled = true;
      render();
    };
  });
  document.querySelectorAll('.ff-collapse').forEach(element => {
    element.onclick = () => {
      const index = +element.dataset.index;
      state.files[index].collapsed = !state.files[index].collapsed;
      render();
    };
  });
  document.querySelectorAll('.ff-filter').forEach(element => {
    element.onchange = event => {
      state.files[+event.target.dataset.index].mapping_filter = event.target.value;
      render();
    };
  });
  document.querySelectorAll('.ff-text-filter').forEach(element => {
    element.oninput = event => {
      const index = +event.target.dataset.index;
      const cursor = event.target.selectionStart ?? event.target.value.length;
      state.files[index].mapping_search = event.target.value;
      render();
      const replacement = document.querySelector(`.ff-text-filter[data-index="${index}"]`);
      if (replacement) {
        replacement.focus();
        replacement.setSelectionRange(cursor, cursor);
      }
    };
  });
  document.querySelectorAll('.ff-object').forEach(element => {
    element.onchange = async event => {
      const index = +event.target.dataset.index;
      Object.assign(state.files[index], {
        object: event.target.value,
        object_auto_detected: false,
        object_detection_source: '',
        mappings: null,
        external_id: '',
        externalIdFields: null,
        recordTypes: [],
        record_type_mappings: {},
        cleaned_name: null,
        cleaning_result: null,
      });
      render();
      if (event.target.value) await autoMap(index);
    };
  });
  document.querySelectorAll('.ff-external-id').forEach(element => {
    element.onchange = event => {
      const file = state.files[+event.target.dataset.index];
      state.files.forEach(other => {
        if (other.object === file.object) {
          applyExternalIdToFile(other, event.target.value);
          other.cleaned_name = null;
          other.cleaning_result = null;
        }
      });
      render();
    };
  });
  document.querySelectorAll('.ff-record-type').forEach(element => {
    element.onchange = event => {
      const index = +event.target.dataset.file;
      const file = state.files[index];
      file.record_type_mappings = file.record_type_mappings || {};
      if (event.target.value) file.record_type_mappings[event.target.dataset.value] = event.target.value;
      else delete file.record_type_mappings[event.target.dataset.value];
      file.cleaned_name = null;
      for (const otherIndex of siblingFileIndexes(index)) {
        const other = state.files[otherIndex];
        other.record_type_mappings = { ...(file.record_type_mappings || {}) };
        other.cleaned_name = null;
      }
    };
  });
  document.querySelectorAll('.ff-lookup-external-id').forEach(element => {
    element.onchange = event => {
      const fileIndex = +event.target.dataset.file;
      const file = state.files[fileIndex];
      const mapping = file.mappings[+event.target.dataset.map];
      if (event.target.value === '__target_id__') {
        mapping.lookup_resolution = 'target_id';
        mapping.lookup_external_id = '';
      } else {
        mapping.lookup_resolution = 'external_id';
        mapping.lookup_external_id = event.target.value;
      }
      file.cleaned_name = null;
      const updated = propagateObjectTemplate(fileIndex);
      render();
      if (updated) {
        msg(`${mapping.source} lookup change applied to ${updated} other ${file.object} file(s).`, 'ok');
      }
    };
  });
  document.querySelectorAll('.ff-poly-config').forEach(element => {
    element.onclick = () => openPolymorphicDialog(+element.dataset.file, +element.dataset.map);
  });
  document.querySelectorAll('.ff-value-policy').forEach(element => {
    element.onchange = event => {
      const fileIndex = +event.target.dataset.file;
      const file = state.files[fileIndex];
      const mapping = file.mappings[+event.target.dataset.map];
      mapping.value_policy = event.target.value;
      file.cleaned_name = null;
      file.cleaning_result = null;
      const updated = propagateObjectTemplate(fileIndex);
      render();
      if (updated) {
        msg(`${mapping.source} value policy applied to ${updated} other ${file.object} file(s).`, 'ok');
      }
    };
  });
  document.querySelectorAll('.ff-auto').forEach(element => {
    element.onclick = () => autoMap(+element.dataset.index);
  });
  document.querySelectorAll('.ff-download-file-mapping').forEach(element => {
    element.onclick = () => {
      const index = +element.dataset.index;
      const file = state.files[index];
      const fileStem = safeDownloadStem(String(file.file || `file_${index + 1}`).split(/[\\/]/).pop());
      const name = safeDownloadStem(`${file.object || 'object'}_${fileStem}`);
      if (downloadMappingCsv(`${name}_field_mappings.csv`, [file])) {
        msg(`Downloaded mapping for ${file.file}.`, 'ok');
      }
    };
  });
  document.querySelectorAll('.ff-download-object-mapping').forEach(element => {
    element.onclick = () => {
      const index = +element.dataset.index;
      const file = state.files[index];
      const objectName = String(file.object || '').trim();
      const files = state.files.filter(item => String(item.object || '').trim() === objectName);
      const name = safeDownloadStem(`${objectName || 'object'}_all_files`);
      if (downloadMappingCsv(`${name}_field_mappings.csv`, files)) {
        msg(`Downloaded ${objectName} mapping for ${files.length} file(s).`, 'ok');
      }
    };
  });
  document.querySelectorAll('.ff-apply-siblings').forEach(element => {
    element.onclick = async () => {
      const index = +element.dataset.index;
      const file = state.files[index];
      const siblingCount = siblingFileIndexes(index).length;
      if (!file?.mappings?.length) return msg('Map this sample file first.', 'error');
      element.disabled = true;
      showLoader(
        `Applying ${file.object || 'object'} mapping...`,
        siblingCount
          ? `Copying this mapping to ${siblingCount} other ${file.object} file(s). This can take a moment for large files.`
          : 'Looking for other files that share this object.',
      );
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      try {
        const updated = propagateObjectTemplate(index);
        render();
        msg(
          updated
            ? `Applied this ${file.object} mapping, including removed/unmapped fields, to ${updated} other file(s). Save the template to keep it.`
            : 'No sibling files share this object.',
          updated ? 'ok' : '',
        );
      } catch (error) {
        msg(error.message || 'Failed to apply mapping to the other files.', 'error');
      } finally {
        hideLoader();
      }
    };
  });
  document.querySelectorAll('.ff-apply-folder').forEach(element => {
    element.onclick = async () => {
      const index = +element.dataset.index;
      const source = state.files[index];
      if (!source?.mappings?.length) return msg('Map this sample file first.', 'error');
      try {
        showLoader(
          'Applying folder mapping...',
          'Reading CSV headers in the same folder. Other files are not auto-mapped against Salesforce.',
        );
        const slim = { ...source };
        delete slim.fields;
        delete slim.all_fields;
        delete slim.recordTypes;
        const data = await api('/api/flat-files/apply-folder-mapping', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            file: slim,
            existing_files: state.files.map(item => item.file),
            profile: $('ff-target-profile').value || 'default',
          }),
        });
        for (const added of data.added || []) {
          added.fields = source.fields;
          added.all_fields = source.all_fields;
          added.recordTypes = source.recordTypes;
          added.externalIdFields = source.externalIdFields;
          added.access_issues = source.access_issues;
          added.collapsed = true;
          added.mapping_ui_hidden = true;
          state.files.push(added);
        }
        const updated = propagateObjectTemplate(index);
        render();
        const addedCount = (data.added || []).length;
        if (addedCount) {
          msg(
            `Added ${addedCount} file(s) from ${data.folder} using this mapping.${updated ? ` Updated ${updated} already-loaded file(s).` : ''} Save the draft template.`,
            'ok',
          );
        } else if (updated) {
          msg(`No new files to add. Updated mapping on ${updated} already-loaded file(s) in ${data.folder}.`, 'ok');
        } else {
          msg(`No other CSV files found in ${data.folder}.`);
        }
      } catch (error) {
        msg(error.message, 'error');
      } finally {
        hideLoader();
      }
    };
  });
  document.querySelectorAll('.ff-include').forEach(element => {
    element.onchange = event => {
      const fileIndex = +event.target.dataset.file;
      const file = state.files[fileIndex];
      const mapping = file.mappings[+event.target.dataset.map];
      mapping.ignored = !event.target.checked;
      mapping.status = mapping.ignored
        ? 'ignored'
        : mapping.target
          ? 'user_mapped'
          : String(mapping.unmapped_reason || '').includes('read-only')
            ? 'read_only'
            : 'unmapped';
      if (mapping.ignored) {
        file.access_issues = (file.access_issues || []).filter(issue =>
          issue.code !== 'target_field_read_only' || issue.field !== mapping.source
        );
      } else if (mapping.status === 'read_only') {
        const message = `Source column ${mapping.source} matches ${file.object}.${mapping.source}, but the target field is read-only for the migration user. Remove the column or mark it ignored; it cannot be loaded by insert or upsert.`;
        file.access_issues = [
          ...(file.access_issues || []).filter(issue =>
            issue.code !== 'target_field_read_only' || issue.field !== mapping.source
          ),
          { code: 'target_field_read_only', field: mapping.source, message },
        ];
      }
      file.cleaned_name = null;
      const updated = propagateObjectTemplate(fileIndex);
      render();
      if (updated) {
        const action = mapping.ignored ? 'removed/ignored' : 'include setting';
        msg(`${mapping.source} ${action} applied to ${updated} other ${file.object} file(s).`, 'ok');
      }
    };
  });
  document.querySelectorAll('.ff-target-field').forEach(element => {
    element.onchange = event => {
      const fileIndex = +event.target.dataset.file;
      const file = state.files[fileIndex];
      const mapping = file.mappings[+event.target.dataset.map];
      mapping.target = event.target.value || null;
      mapping.status = mapping.target ? 'user_mapped' : 'unmapped';
      if (!mapping.target) mapping.ignored = false;
      file.cleaned_name = null;
      const updated = propagateObjectTemplate(fileIndex);
      render();
      if (updated) {
        const action = mapping.target ? `mapped to ${mapping.target}` : 'unmapped/removed';
        msg(`${mapping.source} ${action} on ${updated} other ${file.object} file(s).`, 'ok');
      }
    };
  });
}

$('ff-clean').onclick = async () => {
  if (!state.files.length) return msg('Upload files first.', 'error');
  const button = $('ff-clean');
  const result = $('ff-clean-result');
  button.disabled = true;
  result.className = 'ff-stage-result visible running';
  result.textContent = 'Validating every mapped value against the target schema...';
  try {
    showLoader(
      'Validating and cleaning files...',
      'Preserving originals and separating schema-valid rows from rejected rows.',
    );
    const files = state.files.map(({ fields, all_fields, columns, size, recordTypes, ...file }) => file);
    const data = await api('/api/flat-files/clean', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        batch_id: state.batchId,
        target_profile: $('ff-target-profile').value,
        files,
      }),
    });
    for (const fileResult of data.files || []) {
      const file = state.files.find(item => item.file === fileResult.file && item.object === fileResult.object);
      if (file) {
        file.cleaned_name = fileResult.cleaned_name;
        file.rejected_name = fileResult.rejected_name;
        file.cleaning_result = fileResult;
      }
    }
    state.savedId = null;
    $('ff-ingest').disabled = true;
    $('ff-add-pipeline').disabled = true;
    const links = (data.files || []).map(file => {
      if (file.skipped) {
        return `<div class="ff-clean-file ff-clean-file-skipped"><strong>${esc(file.file)}</strong>: skipped — not validated and will not be staged</div>`;
      }
      const fieldErrors = Object.entries(file.field_counts || {})
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([field, count]) => `${field}: ${Number(count).toLocaleString()} invalid row(s)`);
      const errorSummary = fieldErrors.length
        ? `<div class="ff-validation-detail"><strong>All fields with detected errors:</strong> ${fieldErrors.map(item => esc(item)).join(' | ')}</div>`
        : '';
      const statusClass = Number(file.rejected_count) > 0 ? 'ff-clean-file-error' : 'ff-clean-file-ok';
      return `<div class="ff-clean-file ${statusClass}"><strong>${esc(file.file)}</strong>: ${file.clean_count} clean, ${file.rejected_count} rejected`
        + ` · <a href="${esc(file.clean_download)}">Download clean</a>`
        + (file.rejected_count ? ` · <a href="${esc(file.rejected_download)}">Download rejected</a>` : '')
        + errorSummary
        + `</div>`;
    }).join('');
    result.className = `ff-stage-result visible ${data.rejected_count ? 'error' : 'ok'}`;
    result.innerHTML = `<strong>Cleaning complete.</strong> ${data.clean_count} clean row(s), `
      + `${data.rejected_count} rejected row(s), ${data.normalized_count} safely normalized row(s).${links}`;
    msg(
      data.rejected_count
        ? `Cleaning completed with ${data.rejected_count} rejected row(s). Review the rejected downloads before saving.`
        : 'All rows match the target schema. Save the template to continue.',
      data.rejected_count ? 'error' : 'ok',
    );
  } catch (error) {
    result.className = 'ff-stage-result visible error';
    result.textContent = `Cleaning failed: ${error.message}`;
    msg(error.message, 'error');
  } finally {
    hideLoader();
    button.disabled = false;
  }
};

async function autoMap(index, preserveExisting = false, options = {}) {
  const propagate = options.propagate !== false;
  const file = state.files[index];
  const previousMappings = new Map(
    (file.mappings || []).map(mapping => [mapping.source, mapping]),
  );
  msg(`Mapping ${file.file}...`);
  try {
    const data = await api('/api/flat-files/auto-map', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        object: file.object,
        columns: file.columns || file.mappings.map(mapping => mapping.source),
        profile: $('ff-target-profile').value,
      }),
    });
    file.fields = data.fields;
    file.all_fields = data.all_fields || [];
    file.mappings = data.mappings.map(autoMapping => {
      const previous = preserveExisting ? previousMappings.get(autoMapping.source) : null;
      const mapping = previous
        ? {
            ...autoMapping,
            ...previous,
            corresponding_exists: autoMapping.corresponding_exists,
            corresponding_type: autoMapping.corresponding_type,
            corresponding_writable: autoMapping.corresponding_writable,
            unmapped_reason: previous.unmapped_reason || autoMapping.unmapped_reason,
          }
        : autoMapping;
      const lookup = data.lookup_fields?.[mapping.target] || null;
      const targetMeta = (data.fields || []).find(field => field.name === mapping.target);
      const polymorphic = Boolean(
        mapping.is_polymorphic
        || (targetMeta?.type === 'reference' && (targetMeta.references_to || []).length > 1)
        || (mapping.target === 'OwnerId' && (targetMeta?.references_to || []).includes('User'))
      );
      const defaultOwnerRules = mapping.target === 'OwnerId'
        && (targetMeta?.references_to || []).includes('User')
        && (lookup?.external_id_fields || []).includes('External_Id__c')
        ? [{identify_by: 'prefix', match_value: '005', companion_column: '', referenced_object: 'User', external_id: 'External_Id__c', external_id_fields: lookup.external_id_fields || []}]
        : [];
      return polymorphic ? {
        ...mapping,
        referenced_object: mapping.referenced_object || lookup?.referenced_object || '',
        lookup_external_id_fields: lookup?.external_id_fields || mapping.lookup_external_id_fields || [],
        is_polymorphic: true,
        referenced_objects: targetMeta.references_to || [],
        lookup_external_id: '',
        lookup_resolution: mapping.lookup_resolution || (defaultOwnerRules.length ? 'polymorphic_external_id' : 'target_id'),
        polymorphic_mappings: mapping.polymorphic_mappings?.length ? mapping.polymorphic_mappings : defaultOwnerRules,
      } : lookup ? {
        ...mapping,
        referenced_object: lookup.referenced_object,
        lookup_external_id_fields: lookup.external_id_fields || [],
        lookup_external_id: mapping.lookup_resolution === 'target_id'
          ? ''
          : (mapping.lookup_external_id || lookup.recommended_external_id || ''),
        lookup_resolution: mapping.lookup_resolution || ((lookup.external_id_fields || []).length ? 'external_id' : 'target_id'),
      } : mapping;
    });
    for (const mapping of file.mappings) {
      if (!mapping.value_policy && String(mappingFieldType(file, mapping)).toLowerCase() === 'url') {
        mapping.value_policy = 'url_salesforce';
      }
    }
    file.externalIdFields = data.external_id_fields || [];
    file.external_id = file.external_id || data.recommended_external_id || '';
    if (file.external_id) applyExternalIdToFile(file, file.external_id);
    file.recordTypes = data.record_types || [];
    file.access_issues = data.access_issues || [];
    file.record_type_mappings = file.record_type_mappings || {};
    if (!preserveExisting) {
      file.cleaned_name = null;
      file.cleaning_result = null;
    }
    for (const sourceValue of file.record_type_values || []) {
      const match = file.recordTypes.find(recordType =>
        recordType.id === sourceValue
        || recordType.developer_name.toLowerCase() === String(sourceValue).toLowerCase()
      );
      if (match && !file.record_type_mappings[sourceValue]) {
        file.record_type_mappings[sourceValue] = match.id;
      }
    }
    if (propagate) {
      const synced = propagateObjectTemplate(index);
      const overlay = applyUploadedMappingsToFiles();
      if (overlay.columns) propagateObjectTemplate(index);
      render();
      msg(
        overlay.columns
          ? `${file.file}: mapped, then applied uploaded mapping sheet to ${overlay.columns} column(s).`
          : (synced
            ? `${file.file}: mapped and synced to ${synced} sibling file(s) for ${file.object}.`
            : `${file.file}: object and API-name mapping completed.`),
        'ok',
      );
    }
  } catch (error) {
    msg(error.message, 'error');
  }
}

async function templates() {
  const selectedBeforeRefresh = $('ff-template-select').value || '';
  const data = await api('/api/flat-files/templates');
  $('ff-template-select').innerHTML = '<option value="">Saved templates</option>' + data.templates.map(template =>
    `<option value="${esc(template.id)}">${esc(template.name)} (${template.file_count} files)</option>`,
  ).join('');
  if (selectedBeforeRefresh && [...$('ff-template-select').options].some(option => option.value === selectedBeforeRefresh)) {
    $('ff-template-select').value = selectedBeforeRefresh;
  } else {
    $('ff-template-select').value = '';
  }
  $('ff-delete-template').disabled = !$('ff-template-select').value;
  syncPreValidationTemplate($('ff-template-select').value);
}

function valuePolicyControl(file, mapping, fileIndex, mappingIndex) {
  const fieldType = String(mappingFieldType(file, mapping) || '').toLowerCase();
  const policy = mapping.value_policy || '';
  if (fieldType === 'date') {
    return `<select class="fh-input ff-value-policy" data-file="${fileIndex}" data-map="${mappingIndex}">
      <option value="" ${!policy ? 'selected' : ''}>Strict YYYY-MM-DD</option>
      <option value="date_mdy4" ${policy === 'date_mdy4' ? 'selected' : ''}>Convert M/D/YYYY</option>
      <option value="date_mdy2" ${policy === 'date_mdy2' ? 'selected' : ''}>Convert M/D/YY</option>
      <option value="date_bypass" ${policy === 'date_bypass' ? 'selected' : ''}>Bypass date format</option>
    </select>`;
  }
  if (fieldType === 'url') {
    return `<select class="fh-input ff-value-policy" data-file="${fileIndex}" data-map="${mappingIndex}">
      <option value="url_salesforce" ${policy === 'url_salesforce' ? 'selected' : ''}>Salesforce-compatible</option>
      <option value="" ${!policy ? 'selected' : ''}>Require HTTP/HTTPS</option>
      <option value="url_prepend_https" ${policy === 'url_prepend_https' ? 'selected' : ''}>Add https://</option>
      <option value="url_bypass" ${policy === 'url_bypass' ? 'selected' : ''}>Bypass URL format</option>
    </select>`;
  }
  return '<span class="ff-file-meta">Default</span>';
}

function buildDropTree(files) {
  const root = { directories: new Map(), files: [] };
  for (const file of files) {
    const parts = file.name.split('/').filter(Boolean);
    const filename = parts.pop();
    let node = root;
    for (const part of parts) {
      if (!node.directories.has(part)) node.directories.set(part, { directories: new Map(), files: [] });
      node = node.directories.get(part);
    }
    node.files.push({ ...file, filename });
  }
  return root;
}

function dropTreeFileCount(node) {
  return node.files.length + [...node.directories.values()].reduce((total, child) => total + dropTreeFileCount(child), 0);
}

function renderDropTreeNode(node, path = '') {
  const directories = [...node.directories.entries()].sort(([left], [right]) => left.localeCompare(right));
  const files = [...node.files].sort((left, right) => left.filename.localeCompare(right.filename));
  return directories.map(([name, child]) => {
    const childPath = path ? `${path}/${name}` : name;
    return `<details class="ff-tree-folder" role="treeitem">
      <summary><input class="ff-folder-check" type="checkbox" aria-label="Select folder ${esc(childPath)}" /> &#128193; ${esc(name)} <span class="ff-tree-count">(${dropTreeFileCount(child)})</span>
        <span class="ff-folder-actions"><button class="ff-folder-action" data-action="select" type="button">Select all</button><button class="ff-folder-action" data-action="clear" type="button">Clear</button></span>
      </summary>
      <div class="ff-tree-children" role="group">${renderDropTreeNode(child, childPath)}</div>
    </details>`;
  }).join('') + files.map(file =>
    `<label class="ff-tree-file" role="treeitem"><input class="ff-drop-file" type="checkbox" value="${esc(file.name)}" ${dropFileSelection.has(file.name) ? 'checked' : ''} /> <span>&#128196; ${esc(file.filename)}</span> <span class="ff-tree-count">${Number(file.size).toLocaleString()} bytes</span></label>`,
  ).join('');
}

function syncDropTreeSelection() {
  $('ff-files').querySelectorAll('.ff-tree-folder').forEach(folder => {
    const folderCheck = folder.querySelector(':scope > summary .ff-folder-check');
    const fileChecks = [...folder.querySelectorAll('.ff-drop-file')];
    const checkedCount = fileChecks.filter(input => input.checked).length;
    folderCheck.checked = Boolean(fileChecks.length) && checkedCount === fileChecks.length;
    folderCheck.indeterminate = checkedCount > 0 && checkedCount < fileChecks.length;
  });
  $('ff-selection-count').textContent = `${dropFileSelection.size.toLocaleString()} selected`;
}

function setAllDropFiles(checked) {
  $('ff-files').querySelectorAll('.ff-drop-file').forEach(input => {
    input.checked = checked;
    if (checked) dropFileSelection.add(input.value);
    else dropFileSelection.delete(input.value);
  });
  syncDropTreeSelection();
}

async function refreshDropFiles() {
  try {
    const data = await api('/api/flat-files/drop-files');
    const available = new Set(data.files.map(file => file.name));
    [...dropFileSelection].forEach(name => { if (!available.has(name)) dropFileSelection.delete(name); });
    $('ff-files').innerHTML = data.files.length
      ? renderDropTreeNode(buildDropTree(data.files))
      : '<div class="ff-tree-empty">No CSV files found.</div>';
    $('ff-drop-folder').textContent = `Drop folder: ${data.folder}`;
    syncDropTreeSelection();
    if (!data.files.length) msg('No CSV files found. Copy files into the drop folder, then refresh.');
  } catch (error) {
    msg(error.message, 'error');
  }
}

$('ff-refresh-files').onclick = refreshDropFiles;
$('ff-select-all').onclick = () => setAllDropFiles(true);
$('ff-clear-files').onclick = () => setAllDropFiles(false);
$('ff-expand-tree').onclick = () => {
  const expanded = $('ff-drop-panel').classList.toggle('expanded');
  $('ff-expand-tree').textContent = expanded ? 'Compact view' : 'Expand view';
};
$('ff-files').onchange = event => {
  if (event.target.classList.contains('ff-drop-file')) {
    if (event.target.checked) dropFileSelection.add(event.target.value);
    else dropFileSelection.delete(event.target.value);
  } else if (event.target.classList.contains('ff-folder-check')) {
    const folder = event.target.closest('.ff-tree-folder');
    folder.querySelectorAll('.ff-drop-file').forEach(input => {
      input.checked = event.target.checked;
      if (input.checked) dropFileSelection.add(input.value);
      else dropFileSelection.delete(input.value);
    });
  }
  syncDropTreeSelection();
};
$('ff-files').onclick = event => {
  const folderAction = event.target.closest('.ff-folder-action');
  if (folderAction) {
    event.preventDefault();
    event.stopPropagation();
    const checked = folderAction.dataset.action === 'select';
    const folder = folderAction.closest('.ff-tree-folder');
    folder.querySelectorAll('.ff-drop-file').forEach(input => {
      input.checked = checked;
      if (checked) dropFileSelection.add(input.value);
      else dropFileSelection.delete(input.value);
    });
    syncDropTreeSelection();
  } else if (event.target.matches('summary input')) {
    event.stopPropagation();
  }
};

$('ff-import').onclick = async () => {
  const selectedFiles = [...dropFileSelection];
  if (!selectedFiles.length) return msg('Select one or more CSV files from the drop folder.', 'error');
  const loadedName = loadedTemplateLabel();
  if (state.savedId || state.files.length) {
    const proceed = window.confirm(
      loadedName
        ? `"${loadedName}" is currently open. Using drop-folder files starts a new package and does not add files to that saved template.\n\nTo add files to an existing template, Load it and use "Apply this mapping to all CSVs in this folder".\n\nStart a new package?`
        : 'Files are already open in this page. Using drop-folder files replaces them and starts a new package.\n\nContinue?',
    );
    if (!proceed) return;
  }
  if (selectedFiles.length > 3) {
    const proceed = window.confirm(
      `You selected ${selectedFiles.length} files.\n\nFor split files in one folder (for example Case/*.csv), import ONE sample file, map it, then click "Apply this mapping to all CSVs in this folder".\n\nLoad all ${selectedFiles.length} files into this page anyway? That can be slow.`,
    );
    if (!proceed) return;
  }
  try {
    showLoader(
      `Importing ${selectedFiles.length} file(s)...`,
      'Detecting objects from folder or filename, then mapping fields by API name.',
    );
    msg(`Importing and detecting ${selectedFiles.length} file(s)...`);
    const data = await api('/api/flat-files/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ files: selectedFiles, profile: $('ff-target-profile').value || 'default' }),
    });
    state.batchId = data.batch_id;
    state.files = data.files;
    clearWorkingPackage();
    prepareFolderSamples(state.files);
    render();
    const detectedIndexes = sampleFileIndexes().filter(index => state.files[index].object);
    await Promise.all(detectedIndexes.map(index => autoMap(index, false, { propagate: false })));
    const overlay = applyUploadedMappingsToFiles();
    detectedIndexes.forEach(index => propagateObjectTemplate(index));
    render();
    const unresolved = state.files.filter(file => !file.object).length;
    const folderDetected = state.files.filter(file => file.object_detection_source === 'folder').length;
    const filenameDetected = state.files.filter(file => file.object_detection_source === 'filename').length;
    msg(
      unresolved
        ? `${detectedIndexes.length} object(s) detected (${folderDetected} folder / ${filenameDetected} filename); assign ${unresolved} unmatched file(s) manually.`
        : overlay.columns
          ? `All ${detectedIndexes.length} file(s) imported and mapped from the uploaded sheet (${overlay.columns} column(s) applied).`
          : `All ${detectedIndexes.length} file(s) detected (${folderDetected} folder / ${filenameDetected} filename) and API fields mapped. Same-object siblings share one mapping template.`,
      unresolved ? '' : 'ok',
    );
  } catch (error) {
    msg(error.message, 'error');
  } finally {
    hideLoader();
  }
};

$('ff-apply-external-id').onclick = () => {
  const desired = $('ff-package-external-id').value.trim();
  if (!desired) return msg('Enter or select an external-ID API name.', 'error');
  const incompatible = [];
  const conflicts = [];
  for (const file of state.files) {
    if (!(file.externalIdFields || []).includes(desired)) {
      incompatible.push(file.object || file.file);
      continue;
    }
    const result = applyExternalIdToFile(file, desired);
    file.cleaned_name = null;
    file.cleaning_result = null;
    if (!result.applied) {
      conflicts.push(`${file.object || file.file} (${result.conflict} already maps to ${desired})`);
    }
  }
  render();
  if (incompatible.length || conflicts.length) {
    const details = [];
    if (incompatible.length) details.push(`not available on: ${incompatible.join(', ')}`);
    if (conflicts.length) details.push(`mapping conflict on: ${conflicts.join(', ')}`);
    msg(`Applied ${desired} where safe; ${details.join('; ')}.`, 'error');
  } else {
    msg(`Applied source Id → ${desired} to all ${state.files.length} object mappings.`, 'ok');
  }
};

$('ff-download-mappings').onclick = () => downloadAllMappingsCsv();
$('ff-download-mappings-by-object').onclick = () => downloadMappingsByObject();
$('ff-upload-mappings').onclick = () => $('ff-upload-mappings-file').click();
$('ff-upload-mappings-early')?.addEventListener('click', () => $('ff-upload-mappings-file').click());
$('ff-upload-mappings-file').onchange = async event => {
  const file = event.target.files?.[0];
  event.target.value = '';
  if (!file) return;
  try {
    const text = await file.text();
    state.uploadedMappings = parseMappingCsv(text);
    if (!state.uploadedMappings.length) {
      return msg('No mapping rows found in the uploaded CSV.', 'error');
    }
    if (!state.files.length) {
      return msg(
        `Loaded ${state.uploadedMappings.length} mapping row(s) from ${file.name}. Import files next and the sheet will be applied automatically.`,
        'ok',
      );
    }
    const overlay = applyUploadedMappingsToFiles();
    sampleFileIndexes().forEach(index => propagateObjectTemplate(index));
    render();
    msg(
      overlay.columns
        ? `Applied ${overlay.columns} mapping(s) from ${file.name} to ${overlay.files} file(s). Review, then save the template.`
        : `Loaded ${state.uploadedMappings.length} mapping row(s), but none matched current source columns. Check object names and CSV headers.`,
      overlay.columns ? 'ok' : 'error',
    );
  } catch (error) {
    msg(error.message || 'Failed to read mapping CSV.', 'error');
  }
};

$('ff-save').onclick = async () => {
  const name = $('ff-template-name').value.trim();
  if (!name) return msg('Enter a template name.', 'error');
  let policyIgnoredCount = 0;
  try {
    showLoader(
      'Validating mapping package...',
      'Checking fields, external IDs, lookups, and Record Types against the target org.',
    );
    const allowUnmapped = $('ff-allow-unmapped').checked;
    if (allowUnmapped) {
      sampleFileIndexes().forEach(index => {
        (state.files[index].mappings || []).forEach(mapping => {
          if (!mapping.ignored && !mapping.target) {
            mapping.ignored = true;
            mapping.status = 'ignored_by_package_policy';
            policyIgnoredCount += 1;
          }
        });
        propagateObjectTemplate(index);
      });
      render();
    } else {
      sampleFileIndexes().forEach(index => propagateObjectTemplate(index));
    }
    const files = state.files.map(({ fields, all_fields, columns, size, recordTypes, mapping_ui_hidden, collapsed, mapping_filter, mapping_search, ...file }) => file);
    const data = await api('/api/flat-files/templates', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name,
        batch_id: state.batchId,
        target_profile: $('ff-target-profile').value,
        allow_unmapped: allowUnmapped,
        draft: true,
        files,
      }),
    });
    state.savedId = data.template.id;
    rememberTemplate(state.savedId);
    const prepared = state.files.filter(file => !file.skip_staging).every(file => file.cleaned_name);
    $('ff-ingest').disabled = !prepared;
    $('ff-add-pipeline').disabled = !prepared;
    await templates();
    $('ff-template-select').value = state.savedId;
    $('ff-delete-template').disabled = false;
    syncPreValidationTemplate(state.savedId);
    const ignoredCount = data.ignored_unmapped_count || policyIgnoredCount;
    const ignoredNote = ignoredCount
      ? ` ${ignoredCount} unmapped column(s) were excluded.`
      : '';
    const valueValidation = data.data_validation || {};
    if (valueValidation.issue_count) {
      const summaries = (valueValidation.files || []).slice(0, 5).map(file => {
        const fields = Object.entries(file.field_counts || {})
          .slice(0, 5)
          .map(([field, count]) => `${field} (${count})`)
          .join(', ');
        return `${file.file}: ${file.issue_count} issue(s)${fields ? ` — ${fields}` : ''}`;
      });
      msg(
        `Template "${data.template.name}" was saved with ${valueValidation.issue_count} data value issue(s). `
        + `Add transformations before running.\n${summaries.map(item => `• ${item}`).join('\n')}`,
        'error',
      );
    } else {
      msg(`Draft template "${data.template.name}" was saved.${ignoredNote} Open Pre-validation to create clean and rejected files before staging.`, 'ok');
    }
  } catch (error) {
    if (error.errorCount) {
      validationErrorMsg(error);
    } else {
      msg(error.message, 'error');
    }
  } finally {
    hideLoader();
  }
};

$('ff-ingest').onclick = async () => {
  if (!state.savedId) return;
  const button = $('ff-ingest');
  const result = $('ff-stage-result');
  button.disabled = true;
  button.textContent = 'Staging...';
  result.className = 'ff-stage-result visible running';
  result.textContent = 'Normalizing files and writing raw pipeline staging tables...';
  try {
    msg('Normalizing and staging all files...');
    const data = await api(`/api/flat-files/templates/${encodeURIComponent(state.savedId)}/ingest`, { method: 'POST' });
    const summary = Object.entries(data.object_counts).map(([name, count]) => `${name}: ${Number(count).toLocaleString()} rows`).join(' · ');
    result.className = 'ff-stage-result visible ok';
    result.innerHTML = `<strong>Staging complete.</strong> ${esc(summary)}<br>Next stage: Transform`;
    msg('');
  } catch (error) {
    result.className = 'ff-stage-result visible error';
    result.textContent = `Staging failed: ${error.message}`;
    msg(error.message, 'error');
  } finally {
    button.disabled = false;
    button.textContent = 'Stage files for pipeline';
  }
};

$('ff-new-template').onclick = () => {
  if (state.files.length && !window.confirm('Clear the open files and start a new template? Saved templates are not deleted.')) return;
  clearWorkingPackage({ clearFiles: true });
  msg('No template is loaded. Select drop-folder files to start a new package, or choose a saved template and click Load.', 'ok');
};

$('ff-load-template').onclick = async () => {
  const id = $('ff-template-select').value;
  if (!id) return;
  try {
    showLoader(
      'Loading saved template...',
      'Opening one sample file per folder. Other tagged files stay in the template without being mapped in this page.',
    );
    const template = await api(`/api/flat-files/templates/${encodeURIComponent(id)}`);
    state.batchId = template.batch_id;
    state.files = template.files || [];
    prepareFolderSamples(state.files);
    state.savedId = template.id;
    rememberTemplate(state.savedId);
    $('ff-template-name').value = template.name;
    $('ff-target-profile').value = template.target_profile || 'default';
    $('ff-allow-unmapped').checked = Boolean(template.allow_unmapped);
    const samples = sampleFileIndexes();
    for (const index of samples) {
      await autoMap(index, true, { propagate: false });
      propagateObjectTemplate(index);
    }
    const prepared = template.prevalidation_status === 'PASSED' || template.prevalidation_status === 'ISSUES_FOUND';
    $('ff-ingest').disabled = !prepared;
    $('ff-add-pipeline').disabled = !prepared;
    render();
    const hiddenCount = state.files.filter(file => file.mapping_ui_hidden).length;
    msg(
      hiddenCount
        ? `Loaded template "${template.name}". Showing ${samples.length} sample file(s); ${hiddenCount} other file(s) stay in the template and use the same mapping.`
        : `Loaded template "${template.name}".`,
      'ok',
    );
  } catch (error) {
    msg(error.message, 'error');
  } finally {
    hideLoader();
  }
};

$('ff-delete-template').onclick = async () => {
  const select = $('ff-template-select');
  const id = select.value;
  if (!id) return;
  const name = select.options[select.selectedIndex]?.textContent || id;
  if (!window.confirm(`Delete saved template "${name}"? This does not delete uploaded CSV files or migration history.`)) return;
  const button = $('ff-delete-template');
  button.disabled = true;
  try {
    await api(`/api/flat-files/templates/${encodeURIComponent(id)}`, { method: 'DELETE' });
    if (state.savedId === id) clearWorkingPackage({ clearFiles: true });
    else if (sessionStorage.getItem(PREVALIDATION_TEMPLATE_KEY) === id) sessionStorage.removeItem(PREVALIDATION_TEMPLATE_KEY);
    await templates();
    select.value = '';
    $('ff-delete-template').disabled = true;
    syncPreValidationTemplate('');
    msg(`Deleted template "${name}".`, 'ok');
  } catch (error) {
    msg(error.message, 'error');
  } finally {
    button.disabled = !select.value;
  }
};

$('ff-add-pipeline').onclick = () => {
  if (state.savedId) window.location.href = `/pipeline?add_flat=${encodeURIComponent(state.savedId)}`;
};

$('ff-target-profile').onchange = async event => {
  showLoader('Switching target profile...', 'Refreshing the target-only object catalog.');
  try {
    checkTargetConnection(event.target.value);
    const data = await api(`/api/flat-files/objects?profile=${encodeURIComponent(event.target.value)}`);
    state.objects = data.objects || [];
    render();
  } catch (error) {
    msg(error.message, 'error');
  } finally {
    hideLoader();
  }
};

$('ff-reference-identities').onclick = async () => {
  const all = await api('/api/templates');
  state.referenceTemplates = (all.templates || []).filter(template => ['reference_identity', 'object_external_id'].includes(template.template_kind));
  renderFlatReferenceDialog();
  $('ff-reference-dialog').showModal();
};

$('ff-reference-scan').onclick = () => {
  scanFlatReferenceRows();
  renderFlatReferenceDialog();
  msg(state.referenceRows.length
    ? `Loaded ${state.referenceRows.length} referenced object(s) from mapped file lookups.`
    : 'No mapped lookup objects found. Map file lookup columns first or load a saved Reference Identity template.',
    state.referenceRows.length ? 'ok' : 'error');
};

$('ff-reference-close').onclick = () => $('ff-reference-dialog').close();

$('ff-reference-load').onchange = async event => {
  if (!event.target.value) return;
  const data = await api(`/api/templates/${encodeURIComponent(event.target.value)}`);
  const rows = data.template?.objects || [];
  const scanned = new Map(state.referenceRows.map(row => [row.object, row]));
  state.referenceRows = rows.map(item => {
    const object = item.object || item.target_object || item.source_object || '';
    const previous = scanned.get(object) || {};
    const targetKey = item.match_mapping?.target_field || item.target_external_id || '';
    return {
      object,
      columns: previous.columns || [],
      fields: [...new Set([...(previous.fields || []), ...(targetKey ? [targetKey] : [])])],
      target_external_id: targetKey,
    };
  });
  $('ff-reference-name').value = data.template_name || event.target.value;
  renderFlatReferenceDialog();
  $('ff-reference-load').value = event.target.value;
};

$('ff-reference-save').onclick = async () => {
  const name = $('ff-reference-name').value.trim();
  if (!name) return msg('Enter a Reference Identity template name.', 'error');
  const incomplete = state.referenceRows.filter(row => !row.target_external_id);
  if (incomplete.length) return msg(`Select target match keys for: ${incomplete.map(row => row.object).join(', ')}`, 'error');
  const objects = state.referenceRows.map(row => ({
    object: row.object,
    source_object: row.object,
    target_object: row.object,
    target_external_id: row.target_external_id,
    match_mapping: { source_field: row.target_external_id, target_field: row.target_external_id },
  }));
  await api('/api/templates', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ template_name: name, template_kind: 'reference_identity', input_mode: 'flat_file', target_profile: $('ff-target-profile').value || 'default', template: { objects } }),
  });
  applyFlatReferenceRows();
  $('ff-reference-dialog').close();
  msg(`Reference Identity template "${name}" saved and applied.`, 'ok');
};

$('ff-poly-add').onclick = () => {
  const context = state.polymorphicContext;
  if (!context) return;
  const mapping = state.files[context.fileIndex].mappings[context.mappingIndex];
  mapping.polymorphic_mappings = document.querySelector('#ff-poly-body tr[data-index]')
    ? collectPolymorphicRows()
    : (mapping.polymorphic_mappings || []);
  mapping.polymorphic_mappings.push({ identify_by: 'prefix', match_value: '', companion_column: '', referenced_object: '', external_id: 'External_Id__c' });
  renderPolymorphicRows();
};
$('ff-poly-policy').onchange = togglePolymorphicRuleEditor;
$('ff-poly-close').onclick = () => { $('ff-polymorphic-dialog').close(); state.polymorphicContext = null; };
$('ff-poly-save').onclick = () => {
  const context = state.polymorphicContext;
  if (!context) return;
  const mapping = state.files[context.fileIndex].mappings[context.mappingIndex];
  const policy = $('ff-poly-policy').value;
  const rules = collectPolymorphicRows();
  if (policy !== 'target_id' && !rules.length) return msg('Add at least one polymorphic object mapping.', 'error');
  mapping.lookup_resolution = policy;
  mapping.polymorphic_mappings = rules;
  mapping.lookup_external_id = '';
  state.files[context.fileIndex].cleaned_name = null;
  $('ff-polymorphic-dialog').close();
  state.polymorphicContext = null;
  render();
  msg(`Applied ${rules.length} polymorphic object rule(s). Save the template to persist them.`, 'ok');
};

Promise.all([
  api('/api/org-profiles'),
  api('/api/flat-files/objects?profile=default'),
  templates(),
  checkTargetConnection('default'),
  refreshDropFiles(),
]).then(([profiles, objects]) => {
  $('ff-target-profile').innerHTML = (profiles.target || []).map(profile =>
    `<option value="${esc(profile.id)}">${esc(profile.label)}</option>`,
  ).join('') || '<option value="default">Default</option>';
  state.objects = objects.objects || [];
  $('ff-template-select').value = '';
  $('ff-delete-template').disabled = true;
  syncPreValidationTemplate('');
  if ($('ff-files').querySelector('.ff-drop-file')) msg('Ready. Select files or entire folders from the drop-folder tree. Load a saved template only when you want to edit it.');
}).catch(error => msg(error.message, 'error'))
  .finally(() => hideLoader());
