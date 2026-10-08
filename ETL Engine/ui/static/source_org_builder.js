function emptyObjectEntry() {
  return {
    source_object: '',
    target_object: '',
    query_filter: '',
    allowed_operations: ['insert', 'upsert'],
    default_operation: 'upsert',
    match_mapping: { source_field: '', target_field: '' },
    target_external_id: '',
    field_mappings: [],
    lookup_mappings: {},
    cleared_lookups: {},
    record_type_resolution: 'developer_name',
    record_type_mappings: {},
  };
}

const state = {
  templates: [],
  extIdTemplates: [],
  extIdDraft: {
    templateId: '',
    rows: [{ object: '', target_external_id: '' }],
  },
  recordTypeTemplates: [],
  sourceProfiles: [],
  targetProfiles: [],
  sourceObjects: [],
  targetObjects: [],
  sourceFields: [],
  targetFields: [],
  fieldMappings: {},
  metadataCache: {},
  refExternalIdCache: {},
  recordTypeCache: {
    objectName: '',
    sourceProfile: '',
    targetProfile: '',
    source: [],
    target: [],
  },
  packageObjects: [emptyObjectEntry()],
  activeObjectIndex: 0,
  currentTemplateId: '',
  currentPrevalidationStatus: 'DRAFT',
  filters: {
    search: '',
    requiredOnly: false,
    booleanOnly: false,
    lookupOnly: false,
    mappedOnly: false,
    unmappedOnly: false,
  },
};

const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>'"]/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));

function orgDomainLabel(instanceUrl) {
  const host = String(instanceUrl || '').replace(/^https?:\/\//, '').trim();
  return host || 'Unknown org';
}

function activeObjectEntry() {
  if (!state.packageObjects.length) {
    state.packageObjects = [emptyObjectEntry()];
    state.activeObjectIndex = 0;
  }
  if (state.activeObjectIndex < 0 || state.activeObjectIndex >= state.packageObjects.length) {
    state.activeObjectIndex = 0;
  }
  return state.packageObjects[state.activeObjectIndex];
}

function sourceFieldMeta(name) {
  return state.sourceFields.find(field => field.name === name) || null;
}

function targetFieldMeta(name) {
  return state.targetFields.find(field => field.name === name) || null;
}

function ensureMappingState() {
  const next = {};
  for (const field of state.sourceFields) {
    const previous = state.fieldMappings[field.name] || {};
    next[field.name] = {
      include: Boolean(previous.include),
      targetField: previous.targetField || '',
    };
  }
  state.fieldMappings = next;
}

function packageObjectByName(packageObjects = state.packageObjects) {
  const byName = new Map();
  for (const item of packageObjects) {
    const name = String(item?.source_object || item?.target_object || '').trim();
    if (name) byName.set(name, item);
  }
  return byName;
}

function fieldIsRecordType(field) {
  if (!field) return false;
  if (field.is_record_type) return true;
  if (field.name === 'RecordTypeId') return true;
  const refs = Array.isArray(field.references_to) ? field.references_to : [];
  return refs.length === 1 && refs[0] === 'RecordType';
}

function fieldSupportsLookupResolution(field) {
  if (!field?.is_lookup) return false;
  if (fieldIsRecordType(field)) return false;
  if (field.supports_lookup_resolution === false) return false;
  // Salesforce describe uses type=reference for lookup, master-detail, and User fields.
  return true;
}

function packageExternalId(entry) {
  return String(entry?.match_mapping?.target_field || entry?.target_external_id || '').trim();
}

function suggestLookupForField(field, byName, entry) {
  if (!fieldSupportsLookupResolution(field)) return null;
  const refs = Array.isArray(field?.references_to)
    ? field.references_to.map(value => String(value || '').trim()).filter(Boolean)
    : [];
  if (refs.length === 1) {
    const parent = byName.get(refs[0]);
    const extId = packageExternalId(parent);
    if (parent && extId) {
      return {
        lookup_resolution: 'external_id',
        referenced_object: refs[0],
        external_id: extId,
      };
    }
    const fromTemplate = objectExternalIdFromTemplates(refs[0]);
    if (fromTemplate?.external_id) {
      return {
        lookup_resolution: 'object_template',
        referenced_object: refs[0],
        external_id: fromTemplate.external_id,
        referenced_source_key: fromTemplate.referenced_source_key || 'Id',
        template_id: fromTemplate.template_id || '',
        template_name: fromTemplate.template_name || '',
      };
    }
  }
  const fieldName = String(field?.name || '').trim();
  const selfName = String(entry?.source_object || entry?.target_object || '').trim();
  let referencedObject = '';
  if (fieldName === 'ParentId' && selfName && byName.has(selfName)) {
    referencedObject = selfName;
  } else if (fieldName.endsWith('Id') && fieldName !== 'Id') {
    const candidate = fieldName.slice(0, -2);
    if (byName.has(candidate)) referencedObject = candidate;
  }
  if (!referencedObject) return null;
  const parent = byName.get(referencedObject);
  const extId = packageExternalId(parent);
  if (!extId) return null;
  return {
    lookup_resolution: 'external_id',
    referenced_object: referencedObject,
    external_id: extId,
  };
}

function refreshLookupMappingsForEntry(entry, packageObjects = state.packageObjects, sourceFields = null, { fillMissing = true } = {}) {
  const byName = packageObjectByName(packageObjects);
  const result = {};
  const mappedSourceFields = new Set(
    (entry.field_mappings || []).map(item => item?.source_field).filter(Boolean),
  );

  // Preserve user/auto choices; sync external_id from the parent's match field when possible.
  for (const [fieldName, cfg] of Object.entries(entry.lookup_mappings || {})) {
    if (fieldName === 'RecordTypeId') continue;
    const sourceField = (Array.isArray(sourceFields) ? sourceFields : [])
      .find(item => item.name === fieldName);
    if (sourceField && !fieldSupportsLookupResolution(sourceField)) continue;
    const resolution = String(cfg?.lookup_resolution || '').trim().toLowerCase();
    if (resolution === 'preserve' || resolution === 'target_id') {
      result[fieldName] = { lookup_resolution: 'preserve', referenced_object: '', external_id: '' };
      continue;
    }
    if (resolution === 'idmapper') {
      result[fieldName] = { lookup_resolution: 'idmapper', referenced_object: '', external_id: '' };
      continue;
    }
    if (resolution === 'object_template') {
      const referencedObject = String(cfg?.referenced_object || '').trim();
      const resolved = referencedObject ? objectExternalIdFromTemplates(referencedObject) : null;
      result[fieldName] = {
        lookup_resolution: 'object_template',
        referenced_object: referencedObject,
        external_id: resolved?.external_id || String(cfg?.external_id || '').trim(),
        referenced_source_key: resolved?.referenced_source_key || String(cfg?.referenced_source_key || 'Id').trim() || 'Id',
        template_id: resolved?.template_id || String(cfg?.template_id || '').trim(),
        template_name: resolved?.template_name || String(cfg?.template_name || '').trim(),
      };
      continue;
    }
    const referencedObject = String(cfg?.referenced_object || '').trim();
    if (!referencedObject) continue;
    const parent = byName.get(referencedObject);
    const extId = (parent && packageExternalId(parent)) || String(cfg?.external_id || '').trim();
    if (!extId) continue;
    result[fieldName] = {
      lookup_resolution: 'external_id',
      referenced_object: referencedObject,
      external_id: extId,
    };
  }

  if (!fillMissing) return result;

  const cleared = entry.cleared_lookups || {};
  const fields = Array.isArray(sourceFields) ? sourceFields : null;
  if (fields && fields.length) {
    for (const field of fields) {
      if (!fieldSupportsLookupResolution(field) || !mappedSourceFields.has(field.name) || result[field.name]) continue;
      if (cleared[field.name]) continue;
      const suggested = suggestLookupForField(field, byName, entry);
      if (suggested) result[field.name] = suggested;
    }
    return result;
  }

  const selfName = String(entry.source_object || entry.target_object || '').trim();
  for (const mapping of entry.field_mappings || []) {
    const fieldName = String(mapping?.source_field || '').trim();
    if (!fieldName || result[fieldName] || cleared[fieldName]) continue;
    if (fieldName === 'RecordTypeId') continue;
    let referencedObject = '';
    if (fieldName === 'ParentId' && selfName && byName.has(selfName)) {
      referencedObject = selfName;
    } else if (fieldName.endsWith('Id') && fieldName !== 'Id') {
      const candidate = fieldName.slice(0, -2);
      if (byName.has(candidate)) referencedObject = candidate;
    }
    if (!referencedObject) continue;
    const parent = byName.get(referencedObject);
    const extId = packageExternalId(parent);
    if (!extId) continue;
    result[fieldName] = {
      lookup_resolution: 'external_id',
      referenced_object: referencedObject,
      external_id: extId,
    };
  }
  return result;
}

function formatLookupMappingValue(mapping) {
  if (!mapping || typeof mapping !== 'object') return '';
  const resolution = String(mapping.lookup_resolution || '').trim().toLowerCase();
  if (resolution === 'preserve' || resolution === 'target_id') return '__preserve__';
  if (resolution === 'idmapper') return '__idmapper__';
  if (resolution === 'object_template') {
    const referencedObject = String(mapping.referenced_object || '').trim();
    return referencedObject ? `__object_template__||${referencedObject}` : '__object_template__';
  }
  const referencedObject = String(mapping.referenced_object || '').trim();
  const externalId = String(mapping.external_id || '').trim();
  if (referencedObject && externalId) return `${referencedObject}.${externalId}`;
  return '';
}

function objectExternalIdFromTemplates(objectName) {
  const name = String(objectName || '').trim();
  if (!name) return null;

  const parent = packageObjectByName().get(name);
  const packageExtId = packageExternalId(parent);
  if (packageExtId) {
    return {
      referenced_object: name,
      external_id: packageExtId,
      template_id: state.currentTemplateId || '',
      template_name: 'Current package',
    };
  }

  // Prefer dedicated Object External ID templates, then package/source mapping templates.
  const pools = [
    ...(state.extIdTemplates || []),
    ...(state.templates || []),
  ];
  for (const template of pools) {
    const map = template.object_external_ids || {};
    const externalId = String(map[name] || '').trim();
    if (!externalId) continue;
    return {
      referenced_object: name,
      external_id: externalId,
      referenced_source_key: String((template.object_source_keys || {})[name] || 'Id').trim() || 'Id',
      template_id: template.id || '',
      template_name: template.name || template.id || name,
    };
  }
  return null;
}

function parseLookupMappingValue(raw, field = null) {
  const value = String(raw || '').trim();
  if (!value) return null;
  if (value === '__preserve__' || value.toLowerCase() === 'preserve') {
    return { lookup_resolution: 'preserve', referenced_object: '', external_id: '' };
  }
  if (value === '__idmapper__' || value.toLowerCase() === 'idmapper') {
    return { lookup_resolution: 'idmapper', referenced_object: '', external_id: '' };
  }
  if (value === '__object_template__' || value.startsWith('__object_template__||')) {
    const explicitRef = value.includes('||') ? String(value.split('||')[1] || '').trim() : '';
    const refs = Array.isArray(field?.references_to)
      ? field.references_to.map(item => String(item || '').trim()).filter(Boolean)
      : [];
    const referencedObject = explicitRef || (refs.length === 1 ? refs[0] : '');
    if (!referencedObject) {
      return {
        lookup_resolution: 'object_template',
        referenced_object: '',
        external_id: '',
      };
    }
    const resolved = objectExternalIdFromTemplates(referencedObject);
    return {
      lookup_resolution: 'object_template',
      referenced_object: referencedObject,
      external_id: resolved?.external_id || '',
      referenced_source_key: resolved?.referenced_source_key || 'Id',
      template_id: resolved?.template_id || '',
      template_name: resolved?.template_name || '',
    };
  }
  const sep = value.includes('||') ? '||' : '.';
  const parts = value.split(sep);
  if (parts.length < 2) return null;
  const referencedObject = String(parts[0] || '').trim();
  const externalId = String(parts.slice(1).join(sep === '.' ? '.' : '||') || '').trim();
  if (!referencedObject || !externalId) return null;
  return {
    lookup_resolution: 'external_id',
    referenced_object: referencedObject,
    external_id: externalId,
  };
}

function captureLookupMappingsFromForm(previous = {}, entry = null) {
  const next = { ...(previous || {}) };
  const cleared = { ...(entry?.cleared_lookups || {}) };
  document.querySelectorAll('select[data-lookup-field]').forEach(select => {
    const fieldName = select.getAttribute('data-lookup-field') || '';
    if (!fieldName) return;
    let raw = select.value || '';
    if (raw === '__custom__') {
      const custom = document.querySelector(`[data-lookup-custom="${CSS.escape(fieldName)}"]`);
      raw = custom ? custom.value : '';
    }
    const field = state.sourceFields.find(item => item.name === fieldName) || null;
    const parsed = parseLookupMappingValue(raw, field);
    if (!parsed) {
      delete next[fieldName];
      cleared[fieldName] = true;
      return;
    }
    next[fieldName] = parsed;
    delete cleared[fieldName];
  });
  if (entry) entry.cleared_lookups = cleared;
  return next;
}

function lookupCandidateOptions(field) {
  const byName = packageObjectByName();
  const refs = Array.isArray(field.references_to)
    ? field.references_to.map(value => String(value || '').trim()).filter(Boolean)
    : [];
  const candidates = [];
  const seen = new Set();

  function addCandidate(objectName, externalId, tag) {
    const name = String(objectName || '').trim();
    const extId = String(externalId || '').trim();
    if (!name || !extId) return;
    const key = `${name}.${extId}`;
    if (seen.has(key)) return;
    seen.add(key);
    candidates.push({ objectName: name, externalId: extId, tag, value: `${name}||${extId}` });
  }

  for (const ref of refs) {
    const parent = byName.get(ref);
    const packageExtId = packageExternalId(parent);
    if (packageExtId) addCandidate(ref, packageExtId, 'package');
    const cached = state.refExternalIdCache[ref] || [];
    for (const extId of cached) addCandidate(ref, extId, byName.has(ref) ? 'package' : 'existing');
  }
  for (const [name, parent] of byName.entries()) {
    const extId = packageExternalId(parent);
    if (extId) addCandidate(name, extId, 'package');
  }
  return candidates;
}

function lookupResolutionOptionsHtml(field, fieldMapping) {
  if (fieldIsRecordType(field)) {
    if (!(fieldMapping?.include || fieldMapping?.targetField)) {
      return '<span class="som-help">Map field first</span>';
    }
    const entry = activeObjectEntry();
    const mappingCount = Object.keys(entry.record_type_mappings || {}).length;
    const status = mappingCount
      ? `${mappingCount} Record Type mapping(s) — use “Record Type mapping” button above`
      : 'Use “Record Type mapping” button above to map by DeveloperName or replacement';
    return `
      <div class="som-lookup-cell">
        <div class="som-help">Not a lookup — resolved by Record Type mapping</div>
        <div class="som-help">${esc(status)}</div>
      </div>
    `;
  }
  if (!fieldSupportsLookupResolution(field)) {
    return '<span class="som-help">—</span>';
  }
  if (!(fieldMapping?.include || fieldMapping?.targetField)) {
    return '<span class="som-help">Map field first</span>';
  }
  const entry = activeObjectEntry();
  const current = (entry.lookup_mappings || {})[field.name] || null;
  const currentValue = formatLookupMappingValue(current);
  const candidates = lookupCandidateOptions(field);
  const matchedCandidate = candidates.find(item => (
    current?.lookup_resolution !== 'object_template'
    && current?.referenced_object === item.objectName
    && current?.external_id === item.externalId
  ));
  const refs = Array.isArray(field.references_to)
    ? field.references_to.map(value => String(value || '').trim()).filter(Boolean)
    : [];
  const isPreserve = currentValue === '__preserve__';
  const isIdMapper = currentValue === '__idmapper__';
  const isObjectTemplate = String(current?.lookup_resolution || '').toLowerCase() === 'object_template'
    || currentValue.startsWith('__object_template__');
  const isCustom = Boolean(currentValue)
    && !isPreserve
    && !isIdMapper
    && !isObjectTemplate
    && !matchedCandidate;

  const options = [];
  if (refs.length <= 1) {
    const ref = refs[0] || current?.referenced_object || '';
    const resolved = ref ? objectExternalIdFromTemplates(ref) : null;
    const label = resolved
      ? `Object external ID template → ${resolved.referenced_object}.${resolved.external_id}`
      : 'Object external ID template (from saved template)';
    options.push(`<option value="__object_template__" ${isObjectTemplate ? 'selected' : ''}>${esc(label)}</option>`);
  } else {
    options.push(`<option value="" ${!currentValue ? 'selected' : ''}>Select lookup resolution</option>`);
    for (const ref of refs) {
      const resolved = objectExternalIdFromTemplates(ref);
      const label = resolved
        ? `Object external ID template → ${ref}.${resolved.external_id}`
        : `Object external ID template → ${ref}`;
      const selected = isObjectTemplate && current?.referenced_object === ref ? 'selected' : '';
      options.push(`<option value="__object_template__||${esc(ref)}" ${selected}>${esc(label)}</option>`);
    }
  }
  options.push(`<option value="__preserve__" ${isPreserve ? 'selected' : ''}>Preserve ID as-is</option>`);
  options.push(`<option value="__idmapper__" ${isIdMapper ? 'selected' : ''}>IDMapper (same-run parent only)</option>`);
  for (const item of candidates) {
    const selected = matchedCandidate && matchedCandidate.value === item.value;
    const suffix = item.tag === 'package' ? 'package' : 'existing target';
    options.push(
      `<option value="${esc(item.value)}" ${selected ? 'selected' : ''}>${esc(item.objectName)}.${esc(item.externalId)} (${suffix})</option>`,
    );
  }
  options.push(`<option value="__custom__" ${isCustom ? 'selected' : ''}>Custom Object.ExternalId…</option>`);

  const customDisplay = isCustom ? currentValue : '';
  const customHidden = isCustom ? '' : 'hidden';
  const hint = isObjectTemplate && current?.external_id
    ? `<div class="som-help">Uses ${esc(current.referenced_object)}.${esc(current.external_id)}${current.template_name ? ` from “${esc(current.template_name)}”` : ''}</div>`
    : (isObjectTemplate
      ? '<div class="som-help">No Object External ID template found yet — use the button above to define parent Ext IDs.</div>'
      : '');
  return `
    <div class="som-lookup-cell">
      <select class="fh-input" data-lookup-field="${esc(field.name)}" data-lookup-select="${esc(field.name)}">
        ${options.join('')}
      </select>
      <input
        class="fh-input"
        data-lookup-custom="${esc(field.name)}"
        placeholder="e.g. Pricebook2.External_Id__c"
        value="${esc(customDisplay)}"
        ${customHidden}
      />
      ${hint}
    </div>
  `;
}

function captureActiveObjectFromForm() {
  const entry = activeObjectEntry();
  const operations = selectedOperations();
  const defaultOperation = operations.includes('upsert')
    ? 'upsert'
    : operations.includes('insert')
      ? 'insert'
      : operations[0] || 'upsert';
  entry.source_object = $('som-source-object').value || '';
  entry.target_object = $('som-target-object').value || '';
  entry.query_filter = $('som-query-filter').value || '';
  entry.allowed_operations = operations;
  entry.default_operation = defaultOperation;
  entry.match_mapping = {
    source_field: $('som-source-match-field').value || '',
    target_field: $('som-target-match-field').value || '',
  };
  entry.target_external_id = entry.match_mapping.target_field || '';
  entry.field_mappings = state.sourceFields
    .map(field => ({
      source_field: field.name,
      target_field: (state.fieldMappings[field.name] || {}).targetField || '',
      include: Boolean((state.fieldMappings[field.name] || {}).include),
    }))
    .filter(mapping => mapping.include || mapping.target_field)
    .map(({ source_field, target_field }) => ({ source_field, target_field }));
  entry.lookup_mappings = captureLookupMappingsFromForm(entry.lookup_mappings || {}, entry);
  entry.lookup_mappings = refreshLookupMappingsForEntry(entry, state.packageObjects, state.sourceFields);
  if (entry.lookup_mappings) delete entry.lookup_mappings.RecordTypeId;
  if (!entry.record_type_resolution) entry.record_type_resolution = 'developer_name';
  if (!entry.record_type_mappings || typeof entry.record_type_mappings !== 'object') {
    entry.record_type_mappings = {};
  }
}

async function applyActiveObjectToForm({ loadMetadata = true } = {}) {
  const entry = activeObjectEntry();
  $('som-source-object').value = entry.source_object || '';
  $('som-target-object').value = entry.target_object || '';
  $('som-query-filter').value = entry.query_filter || '';
  const allowedOperations = Array.isArray(entry.allowed_operations) && entry.allowed_operations.length
    ? entry.allowed_operations
    : ['upsert'];
  $('som-op-insert').checked = allowedOperations.includes('insert');
  $('som-op-update').checked = allowedOperations.includes('update');
  $('som-op-upsert').checked = allowedOperations.includes('upsert');
  if (loadMetadata) {
    const tasks = [];
    if (entry.source_object) {
      tasks.push(loadFieldMetadata('source'));
    } else {
      state.sourceFields = [];
      ensureMappingState();
    }
    if (entry.target_object) {
      tasks.push(loadFieldMetadata('target'));
    } else {
      state.targetFields = [];
      $('som-target-match-field').innerHTML = '<option value="">Select target external ID field</option>';
    }
    if (tasks.length) {
      await Promise.all(tasks);
    }
  }
  const matchTarget = entry.match_mapping?.target_field || entry.target_external_id || '';
  $('som-source-match-field').value = entry.match_mapping?.source_field || '';
  $('som-target-match-field').value = matchTarget;
  entry.target_external_id = matchTarget;
  ensureMappingState();
  for (const mapping of entry.field_mappings || []) {
    if (!mapping.source_field || !state.fieldMappings[mapping.source_field]) continue;
    state.fieldMappings[mapping.source_field] = {
      include: true,
      targetField: mapping.target_field || '',
    };
  }
  entry.lookup_mappings = refreshLookupMappingsForEntry(entry, state.packageObjects, state.sourceFields);
  renderObjectTabs();
  renderMappingTable();
  state.recordTypeCache = { objectName: '', sourceProfile: '', targetProfile: '', source: [], target: [] };
  refreshRecordTypeObjectLabel();
  populateRecordTypeTemplateLoadOptions();
  if (isRecordTypeModalOpen()) {
    await refreshRecordTypeModal({ force: true, autoMapEmpty: false });
  }
}

function metadataCacheKey(org, objectName, profile) {
  return `${org}:${profile || 'default'}:${objectName}`;
}

function hasCachedMetadata(org, objectName, profile) {
  if (!objectName) return true;
  return Boolean(state.metadataCache[metadataCacheKey(org, objectName, profile)]);
}

function clearMetadataCache(org = null) {
  if (!org) {
    state.metadataCache = {};
    state.refExternalIdCache = {};
    return;
  }
  const prefix = `${org}:`;
  Object.keys(state.metadataCache).forEach(key => {
    if (key.startsWith(prefix)) delete state.metadataCache[key];
  });
  if (org === 'target') {
    state.refExternalIdCache = {};
  }
}

function objectSwitchNeedsFetch(entry) {
  const sourceProfile = $('som-source-profile')?.value || 'default';
  const targetProfile = $('som-target-profile')?.value || 'default';
  return !hasCachedMetadata('source', entry.source_object, sourceProfile)
    || !hasCachedMetadata('target', entry.target_object, targetProfile);
}

function renderObjectTabs() {
  const wrap = $('som-object-tabs');
  const count = $('som-object-count');
  if (count) count.textContent = String(state.packageObjects.length);
  if (!wrap) return;
  wrap.innerHTML = state.packageObjects.map((entry, index) => {
    const label = entry.source_object || entry.target_object || `Object ${index + 1}`;
    const active = index === state.activeObjectIndex ? ' active' : '';
    return `<button type="button" class="som-object-tab${active}" data-index="${index}">${esc(label)}</button>`;
  }).join('');
  wrap.querySelectorAll('.som-object-tab').forEach(button => {
    button.addEventListener('click', async () => {
      const nextIndex = Number(button.getAttribute('data-index'));
      if (Number.isNaN(nextIndex) || nextIndex === state.activeObjectIndex) return;
      captureActiveObjectFromForm();
      state.activeObjectIndex = nextIndex;
      const entry = activeObjectEntry();
      const needsFetch = objectSwitchNeedsFetch(entry);
      if (needsFetch) {
        showLoader('Switching object...', 'Loading field metadata for the selected package object.');
      }
      try {
        await applyActiveObjectToForm({ loadMetadata: true });
      } finally {
        if (needsFetch) hideLoader();
      }
    });
  });
}

async function addPackageObject() {
  captureActiveObjectFromForm();
  state.packageObjects.push(emptyObjectEntry());
  state.activeObjectIndex = state.packageObjects.length - 1;
  state.sourceFields = [];
  state.targetFields = [];
  state.fieldMappings = {};
  await applyActiveObjectToForm({ loadMetadata: false });
  setMessage('Added a new object slot. Select source and target objects, then map fields.', 'ok');
}

async function removePackageObject() {
  if (state.packageObjects.length <= 1) {
    setMessage('A package must keep at least one object.', 'error');
    return;
  }
  state.packageObjects.splice(state.activeObjectIndex, 1);
  state.activeObjectIndex = Math.min(state.activeObjectIndex, state.packageObjects.length - 1);
  const entry = activeObjectEntry();
  const needsFetch = objectSwitchNeedsFetch(entry);
  if (needsFetch) {
    showLoader('Removing object...', 'Refreshing the active package object.');
  }
  try {
    await applyActiveObjectToForm({ loadMetadata: true });
  } finally {
    if (needsFetch) hideLoader();
  }
  setMessage('Removed object from package.', 'ok');
}

document.addEventListener('DOMContentLoaded', async () => {
  bindEvents();
  renderObjectTabs();
  await loadProfiles();
  await Promise.all([loadObjects('source'), loadObjects('target'), loadTemplates()]);
  await refreshAuthStatus();
});

function showLoader(title, detail = 'Please wait...') {
  $('som-loader-title').textContent = title;
  $('som-loader-detail').textContent = detail;
  $('som-loader').classList.add('visible');
}

function hideLoader() {
  $('som-loader').classList.remove('visible');
}

function bindEvents() {
  $('som-source-profile').addEventListener('change', async () => {
    showLoader('Refreshing source profile...', 'Loading source objects and reconnecting.');
    try {
      clearMetadataCache('source');
      state.recordTypeCache = { objectName: '', sourceProfile: '', targetProfile: '', source: [], target: [] };
      await loadObjects('source');
      state.sourceFields = [];
      ensureMappingState();
      renderMappingTable();
      await refreshAuthStatus();
    } finally {
      hideLoader();
    }
  });
  $('som-target-profile').addEventListener('change', async () => {
    showLoader('Refreshing target profile...', 'Loading target objects and reconnecting.');
    try {
      clearMetadataCache('target');
      state.recordTypeCache = { objectName: '', sourceProfile: '', targetProfile: '', source: [], target: [] };
      await loadObjects('target');
      state.targetFields = [];
      renderTargetMatchFields();
      renderMappingTable();
      await refreshAuthStatus();
    } finally {
      hideLoader();
    }
  });
  $('som-source-object').addEventListener('change', async () => {
    showLoader('Loading source object...', 'Fetching source and target field metadata.');
    try {
      await loadFieldMetadata('source', { force: true });
      state.recordTypeCache = { objectName: '', sourceProfile: '', targetProfile: '', source: [], target: [] };
      maybeAutoSelectTargetObject(true);
      if ($('som-target-object').value) {
        await loadFieldMetadata('target');
      }
      autoMapFields(false);
      refreshRecordTypeObjectLabel();
      populateRecordTypeTemplateLoadOptions();
      if (isRecordTypeModalOpen()) {
        await refreshRecordTypeModal({ force: true, autoMapEmpty: false });
      }
    } finally {
      hideLoader();
    }
  });
  $('som-target-object').addEventListener('change', async () => {
    showLoader('Loading target object...', 'Fetching target field metadata and external IDs.');
    try {
      await loadFieldMetadata('target', { force: true });
      autoMapFields(false);
    } finally {
      hideLoader();
    }
  });
  $('som-target-match-field')?.addEventListener('change', () => {
    const entry = activeObjectEntry();
    entry.match_mapping = entry.match_mapping || {};
    entry.match_mapping.target_field = $('som-target-match-field').value || '';
    entry.target_external_id = entry.match_mapping.target_field;
    entry.lookup_mappings = captureLookupMappingsFromForm(entry.lookup_mappings || {}, entry);
    entry.lookup_mappings = refreshLookupMappingsForEntry(entry, state.packageObjects, state.sourceFields);
    renderMappingTable();
  });
  $('som-auto-map').addEventListener('click', () => autoMapFields(true));
  $('som-open-extid-modal')?.addEventListener('click', () => openExtIdModal());
  $('som-open-rt-modal')?.addEventListener('click', () => openRecordTypeModal({ autoMapEmpty: true }));
  $('som-extid-close')?.addEventListener('click', () => closeExtIdModal());
  $('som-rt-close')?.addEventListener('click', () => closeRecordTypeModal());
  $('som-extid-modal')?.addEventListener('click', event => {
    if (event.target === $('som-extid-modal')) closeExtIdModal();
  });
  $('som-rt-modal')?.addEventListener('click', event => {
    if (event.target === $('som-rt-modal')) closeRecordTypeModal();
  });
  $('som-rt-auto-map')?.addEventListener('click', async () => {
    await loadRecordTypeLists({ force: true });
    autoMapRecordTypesByDeveloperName({ fillOnlyEmpty: false, silent: false });
    renderRecordTypePanel({ forceShow: true });
    renderMappingTable();
  });
  $('som-rt-refresh')?.addEventListener('click', async () => {
    await refreshRecordTypeModal({ force: true, autoMapEmpty: false });
  });
  $('som-extid-add')?.addEventListener('click', () => {
    state.extIdDraft.rows.push({ object: '', target_external_id: '' });
    renderExtIdDraftTable();
  });
  $('som-extid-scan')?.addEventListener('click', () => scanReferencedObjectsIntoDraft());
  $('som-extid-save')?.addEventListener('click', () => saveObjectExternalIdTemplate());
  $('som-extid-use-current')?.addEventListener('click', () => useCurrentObjectForExtIdTemplate());
  $('som-extid-load')?.addEventListener('change', () => loadSelectedExtIdTemplate());
  $('som-rt-save-template')?.addEventListener('click', () => saveRecordTypeTemplate());
  $('som-rt-load')?.addEventListener('change', () => loadSelectedRecordTypeTemplate());
  $('som-add-pipeline').addEventListener('click', addToPipeline);
  $('som-save-template').addEventListener('click', saveTemplate);
  $('som-add-object')?.addEventListener('click', () => addPackageObject());
  $('som-remove-object')?.addEventListener('click', () => removePackageObject());
  $('som-template-select').addEventListener('change', loadSelectedTemplate);
  $('som-field-search').addEventListener('input', event => {
    state.filters.search = event.target.value || '';
    renderMappingTable();
  });
  $('som-filter-required').addEventListener('change', event => {
    state.filters.requiredOnly = !!event.target.checked;
    renderMappingTable();
  });
  $('som-filter-boolean').addEventListener('change', event => {
    state.filters.booleanOnly = !!event.target.checked;
    renderMappingTable();
  });
  $('som-filter-lookup').addEventListener('change', event => {
    state.filters.lookupOnly = !!event.target.checked;
    renderMappingTable();
  });
  $('som-filter-mapped').addEventListener('change', event => {
    state.filters.mappedOnly = !!event.target.checked;
    if (state.filters.mappedOnly) {
      state.filters.unmappedOnly = false;
      $('som-filter-unmapped').checked = false;
    }
    renderMappingTable();
  });
  $('som-filter-unmapped').addEventListener('change', event => {
    state.filters.unmappedOnly = !!event.target.checked;
    if (state.filters.unmappedOnly) {
      state.filters.mappedOnly = false;
      $('som-filter-mapped').checked = false;
    }
    renderMappingTable();
  });
}

function setMessage(text, kind = '') {
  const el = $('som-message');
  el.textContent = text || '';
  el.className = `som-message ${kind}`.trim();
}

function renderTemplateStatus() {
  const status = String(state.currentPrevalidationStatus || 'DRAFT').trim().toUpperCase() || 'DRAFT';
  $('som-status').textContent = `Status: ${status}`;
}

function selectedTemplateSummary() {
  return state.templates.find(template => template.id === state.currentTemplateId) || null;
}

function selectedOperations() {
  return ['insert', 'update', 'upsert'].filter(operation => {
    const checkbox = $(`som-op-${operation}`);
    return checkbox ? checkbox.checked : false;
  });
}

async function loadProfiles() {
  const response = await fetch('/api/org-profiles');
  const data = await response.json();
  state.sourceProfiles = Array.isArray(data.source) ? data.source : [];
  state.targetProfiles = Array.isArray(data.target) ? data.target : [];
  renderProfileOptions('som-source-profile', state.sourceProfiles);
  renderProfileOptions('som-target-profile', state.targetProfiles);
}

function renderProfileOptions(id, profiles) {
  const select = $(id);
  const current = select.value || 'default';
  const options = (profiles.length ? profiles : [{ id: 'default', label: 'Default' }])
    .map(profile => `<option value="${esc(profile.id)}">${esc(profile.label || profile.id)}</option>`)
    .join('');
  select.innerHTML = options;
  if ([...select.options].some(option => option.value === current)) select.value = current;
}

async function refreshAuthStatus() {
  const sourceProfile = $('som-source-profile').value || 'default';
  const targetProfile = $('som-target-profile').value || 'default';
  const auth = $('som-auth-status');
  auth.className = 'auth-status checking';
  auth.innerHTML = '<span class="dot"></span> Checking source and target connections...';
  try {
    const [sourceResp, targetResp] = await Promise.all([
      fetch(`/api/auth/check?org=source&profile=${encodeURIComponent(sourceProfile)}`),
      fetch(`/api/auth/check?org=target&profile=${encodeURIComponent(targetProfile)}`),
    ]);
    const source = await sourceResp.json();
    const target = await targetResp.json();
    if (source.status !== 'ok') throw new Error(source.message || 'Source auth failed');
    if (target.status !== 'ok') throw new Error(target.message || 'Target auth failed');
    auth.className = 'auth-status ok';
    auth.innerHTML = `<span class="dot"></span> Source: ${esc(orgDomainLabel(source.instance_url))} | Target: ${esc(orgDomainLabel(target.instance_url))}`;
    auth.title = `Source auth:${source.auth_mode || 'unknown'} | Target auth:${target.auth_mode || 'unknown'}`;
  } catch (error) {
    auth.className = 'auth-status error';
    auth.innerHTML = `<span class="dot"></span> ${esc(error.message)}`;
    auth.title = '';
  }
}

async function loadObjects(org) {
  const profile = $(org === 'source' ? 'som-source-profile' : 'som-target-profile').value || 'default';
  const response = await fetch(`/api/objects?org=${encodeURIComponent(org)}&profile=${encodeURIComponent(profile)}`);
  const data = await response.json();
  const objects = Array.isArray(data.objects) ? data.objects : [];
  if (org === 'source') state.sourceObjects = objects;
  else state.targetObjects = objects;
  const select = $(org === 'source' ? 'som-source-object' : 'som-target-object');
  const current = select.value;
  select.innerHTML = `<option value="">Select ${org} object</option>` + objects.map(item =>
    `<option value="${esc(item.name)}">${esc(item.label)} - ${esc(item.name)}</option>`
  ).join('');
  if ([...select.options].some(option => option.value === current)) select.value = current;
  if (org === 'target') {
    maybeAutoSelectTargetObject(false);
  }
  void renderExtIdDraftTable();
}

function maybeAutoSelectTargetObject(force = false) {
  const sourceObject = $('som-source-object').value || '';
  const targetSelect = $('som-target-object');
  if (!sourceObject || !targetSelect) return;
  const exact = state.targetObjects.find(item => item.name === sourceObject);
  const caseInsensitive = state.targetObjects.find(
    item => String(item.name || '').toLowerCase() === sourceObject.toLowerCase(),
  );
  const match = exact || caseInsensitive;
  if (match && (force || !targetSelect.value)) {
    targetSelect.value = match.name;
  }
}

async function loadFieldMetadata(org, { force = false } = {}) {
  const objectName = $(org === 'source' ? 'som-source-object' : 'som-target-object').value;
  const profile = $(org === 'source' ? 'som-source-profile' : 'som-target-profile').value || 'default';
  if (!objectName) {
    if (org === 'source') state.sourceFields = [];
    else state.targetFields = [];
    ensureMappingState();
    renderMappingTable();
    return;
  }
  const cacheKey = metadataCacheKey(org, objectName, profile);
  let fields = state.metadataCache[cacheKey];
  if (force || !fields) {
    const response = await fetch(`/api/object/${encodeURIComponent(objectName)}?org=${encodeURIComponent(org)}&profile=${encodeURIComponent(profile)}`);
    const data = await response.json();
    fields = Array.isArray(data.fields) ? data.fields : [];
    state.metadataCache[cacheKey] = fields;
  }
  if (org === 'source') {
    state.sourceFields = fields;
    ensureMappingState();
    maybeAutoSelectTargetObject(false);
    renderSourceMatchFields();
    await prefetchReferencedExternalIds(fields);
  } else {
    state.targetFields = fields;
    renderTargetMatchFields();
  }
  renderMappingTable();
}

async function prefetchReferencedExternalIds(fields) {
  const refs = new Set();
  for (const field of fields || []) {
    if (!field?.is_lookup) continue;
    for (const ref of field.references_to || []) {
      const name = String(ref || '').trim();
      if (name) refs.add(name);
    }
  }
  const profile = $('som-target-profile').value || 'default';
  const missing = [...refs].filter(name => !Array.isArray(state.refExternalIdCache[name]));
  if (!missing.length) return;
  await Promise.all(missing.map(async objectName => {
    try {
      const cacheKey = metadataCacheKey('target', objectName, profile);
      let fieldsForObject = state.metadataCache[cacheKey];
      let externalIds = [];
      if (!fieldsForObject) {
        const response = await fetch(
          `/api/object/${encodeURIComponent(objectName)}?org=target&profile=${encodeURIComponent(profile)}`,
        );
        const data = await response.json();
        fieldsForObject = Array.isArray(data.fields) ? data.fields : [];
        state.metadataCache[cacheKey] = fieldsForObject;
        externalIds = Array.isArray(data.external_id_fields) ? data.external_id_fields : [];
      }
      if (!externalIds.length) {
        externalIds = (fieldsForObject || [])
          .filter(field => field.is_external_id && field.writable)
          .map(field => field.name);
      }
      state.refExternalIdCache[objectName] = [...new Set(externalIds.map(value => String(value || '').trim()).filter(Boolean))];
    } catch (_error) {
      state.refExternalIdCache[objectName] = state.refExternalIdCache[objectName] || [];
    }
  }));
}

function eligibleTargetExternalIdFields() {
  return [...state.targetFields]
    .filter(field => field.is_external_id && field.writable)
    .sort((a, b) => String(a.name || '').localeCompare(String(b.name || '')));
}

function renderSourceMatchFields() {
  const current = $('som-source-match-field').value || '';
  const sourceMatchFields = [{ name: 'Id' }, ...state.sourceFields]
    .filter((field, index, all) => all.findIndex(item => item.name === field.name) === index)
    .sort((a, b) => String(a.name || '').localeCompare(String(b.name || '')));
  const options = sourceMatchFields
    .map(field => `<option value="${esc(field.name)}">${esc(field.name)}</option>`)
    .join('');
  $('som-source-match-field').innerHTML = '<option value="">Select source match field</option>' + options;
  if ([...$('som-source-match-field').options].some(option => option.value === current)) {
    $('som-source-match-field').value = current;
  }
}

function renderTargetMatchFields() {
  const current = $('som-target-match-field').value || '';
  const options = eligibleTargetExternalIdFields()
    .map(field => `<option value="${esc(field.name)}">${esc(field.name)}</option>`)
    .join('');
  $('som-target-match-field').innerHTML = '<option value="">Select target external ID field</option>' + options;
  if ([...$('som-target-match-field').options].some(option => option.value === current)) {
    $('som-target-match-field').value = current;
  }
}

function normalizeLabel(value) {
  return String(value || '')
    .toLowerCase()
    .replace(/__c$/g, '')
    .replace(/[^a-z0-9]+/g, ' ')
    .trim()
    .replace(/\s+/g, ' ');
}

function findAutoMapTarget(sourceField, usedTargets) {
  const sourceName = String(sourceField.name || '');
  const sourceLabel = String(sourceField.label || sourceName);

  // 1) Exact API name
  const exact = state.targetFields.find(field => field.name === sourceName && !usedTargets.has(field.name));
  if (exact) return { name: exact.name, method: 'exact API name' };

  // 2) Case-insensitive API name
  const sourceNameKey = sourceName.toLowerCase();
  const caseInsensitive = state.targetFields.find(
    field => field.name.toLowerCase() === sourceNameKey && !usedTargets.has(field.name),
  );
  if (caseInsensitive) return { name: caseInsensitive.name, method: 'case-insensitive API name' };

  // 3) Label similarity
  const sourceLabelKey = normalizeLabel(sourceLabel);
  const sourceNameAsLabel = normalizeLabel(sourceName);
  if (!sourceLabelKey && !sourceNameAsLabel) return null;

  const labelExact = state.targetFields.find(field => {
    if (usedTargets.has(field.name)) return false;
    const targetLabelKey = normalizeLabel(field.label || field.name);
    const targetNameKey = normalizeLabel(field.name);
    return (
      (sourceLabelKey && (targetLabelKey === sourceLabelKey || targetNameKey === sourceLabelKey))
      || (sourceNameAsLabel && (targetLabelKey === sourceNameAsLabel || targetNameKey === sourceNameAsLabel))
    );
  });
  if (labelExact) return { name: labelExact.name, method: 'label match' };

  const labelLoose = state.targetFields.find(field => {
    if (usedTargets.has(field.name)) return false;
    const targetLabelKey = normalizeLabel(field.label || field.name);
    const targetNameKey = normalizeLabel(field.name);
    if (!targetLabelKey && !targetNameKey) return false;
    return (
      (sourceLabelKey && (targetLabelKey.includes(sourceLabelKey) || sourceLabelKey.includes(targetLabelKey)))
      || (sourceNameAsLabel && (targetNameKey.includes(sourceNameAsLabel) || sourceNameAsLabel.includes(targetNameKey)))
    );
  });
  if (labelLoose) return { name: labelLoose.name, method: 'label similarity' };

  return null;
}

function autoMapFields(showMessage = true) {
  const usedTargets = new Set();
  const methodCounts = {
    'exact API name': 0,
    'case-insensitive API name': 0,
    'label match': 0,
    'label similarity': 0,
  };
  let matched = 0;

  for (const field of state.sourceFields) {
    const result = findAutoMapTarget(field, usedTargets);
    if (!result) continue;
    const current = state.fieldMappings[field.name] || { include: false, targetField: '' };
    state.fieldMappings[field.name] = {
      include: true,
      targetField: result.name,
    };
    usedTargets.add(result.name);
    methodCounts[result.method] += 1;
    if (current.targetField !== result.name) matched += 1;
  }

  const entry = activeObjectEntry();
  entry.field_mappings = state.sourceFields
    .map(field => ({
      source_field: field.name,
      target_field: (state.fieldMappings[field.name] || {}).targetField || '',
      include: Boolean((state.fieldMappings[field.name] || {}).include),
    }))
    .filter(mapping => mapping.include || mapping.target_field)
    .map(({ source_field, target_field }) => ({ source_field, target_field }));
  entry.lookup_mappings = refreshLookupMappingsForEntry(entry, state.packageObjects, state.sourceFields);
  renderMappingTable();
  if (!showMessage) return;

  const total = Object.values(methodCounts).reduce((sum, value) => sum + value, 0);
  if (!total) {
    setMessage('No matching source/target fields were found.', '');
    return;
  }
  const details = Object.entries(methodCounts)
    .filter(([, count]) => count > 0)
    .map(([method, count]) => `${count} by ${method}`)
    .join(', ');
  setMessage(`Auto-mapped ${total} field(s)${matched ? ` (${matched} updated)` : ''}: ${details}.`, 'ok');
}

function filteredSourceFields() {
  const search = String(state.filters.search || '').trim().toLowerCase();
  return state.sourceFields.filter(field => {
    const mapping = state.fieldMappings[field.name] || { include: false, targetField: '' };
    const target = targetFieldMeta(mapping.targetField);
    if (state.filters.requiredOnly && !field.required) return false;
    if (state.filters.booleanOnly && String(field.type || '').toLowerCase() !== 'boolean') return false;
    if (state.filters.lookupOnly && !field.is_lookup) return false;
    if (state.filters.mappedOnly && !mapping.targetField) return false;
    if (state.filters.unmappedOnly && mapping.targetField) return false;
    if (!search) return true;
    const haystack = [
      field.name,
      field.label,
      field.type,
      mapping.targetField,
      target?.label,
      target?.name,
    ]
      .filter(Boolean)
      .join(' ')
      .toLowerCase();
    return haystack.includes(search);
  });
}

function syncActiveLookupMappingsFromGrid() {
  const entry = activeObjectEntry();
  entry.field_mappings = state.sourceFields
    .map(field => ({
      source_field: field.name,
      target_field: (state.fieldMappings[field.name] || {}).targetField || '',
      include: Boolean((state.fieldMappings[field.name] || {}).include),
    }))
    .filter(mapping => mapping.include || mapping.target_field)
    .map(({ source_field, target_field }) => ({ source_field, target_field }));
  entry.lookup_mappings = captureLookupMappingsFromForm(entry.lookup_mappings || {}, entry);
  entry.lookup_mappings = refreshLookupMappingsForEntry(entry, state.packageObjects, state.sourceFields);
}

function renderMappingTable() {
  const body = $('som-mapping-body');
  const visibleFields = filteredSourceFields();
  $('som-mapping-count').textContent = `${visibleFields.length}/${state.sourceFields.length}`;
  if (!state.sourceFields.length) {
    body.innerHTML = '<tr><td colspan="6" class="som-help">Select source and target objects to start mapping.</td></tr>';
    return;
  }
  if (!visibleFields.length) {
    body.innerHTML = '<tr><td colspan="6" class="som-help">No fields match the active filters.</td></tr>';
    return;
  }
  const options = '<option value="">Not mapped</option>' + state.targetFields.map(field =>
    `<option value="${esc(field.name)}">${esc(field.name)}</option>`
  ).join('');
  body.innerHTML = visibleFields.map(field => {
    const mapping = state.fieldMappings[field.name] || { include: false, targetField: '' };
    const target = targetFieldMeta(mapping.targetField);
    const targetMeta = target
      ? `${target.type || '-'} ${target.required ? '| required: *' : ''}`
      : '-';
    return `
      <tr>
        <td><input type="checkbox" data-include-field="${esc(field.name)}" ${mapping.include ? 'checked' : ''}></td>
        <td>${esc(field.name)}${field.required ? ' <span class="som-required" title="Required">*</span>' : ''}</td>
        <td>${esc(field.type || '-')}</td>
        <td>
          <select class="fh-input" data-target-field="${esc(field.name)}">
            ${options}
          </select>
        </td>
        <td>${lookupResolutionOptionsHtml(field, mapping)}</td>
        <td class="som-target-meta" data-target-meta="${esc(field.name)}">${esc(targetMeta)}</td>
      </tr>
    `;
  }).join('');

  body.querySelectorAll('input[data-include-field]').forEach(input => {
    const sourceField = input.getAttribute('data-include-field') || '';
    input.addEventListener('change', event => {
      const current = state.fieldMappings[sourceField] || { include: false, targetField: '' };
      current.include = !!event.target.checked;
      state.fieldMappings[sourceField] = current;
      const field = state.sourceFields.find(item => item.name === sourceField);
      if (fieldSupportsLookupResolution(field) || fieldIsRecordType(field)) {
        syncActiveLookupMappingsFromGrid();
        renderMappingTable();
      }
    });
  });

  body.querySelectorAll('select[data-target-field]').forEach(select => {
    const sourceField = select.getAttribute('data-target-field') || '';
    const current = state.fieldMappings[sourceField] || { include: false, targetField: '' };
    select.value = current.targetField || '';
    select.addEventListener('change', event => {
      const next = state.fieldMappings[sourceField] || { include: false, targetField: '' };
      next.targetField = event.target.value || '';
      if (next.targetField) next.include = true;
      state.fieldMappings[sourceField] = next;
      const metaEl = body.querySelector(`[data-target-meta="${CSS.escape(sourceField)}"]`);
      const target = targetFieldMeta(next.targetField);
      if (metaEl) metaEl.textContent = target ? `${target.type || '-'} ${target.required ? '| required: *' : ''}` : '-';
      const checkbox = body.querySelector(`input[data-include-field="${CSS.escape(sourceField)}"]`);
      if (checkbox) checkbox.checked = next.include;
      const field = state.sourceFields.find(item => item.name === sourceField);
      if (fieldSupportsLookupResolution(field) || fieldIsRecordType(field)) {
        syncActiveLookupMappingsFromGrid();
        renderMappingTable();
      }
    });
  });

  body.querySelectorAll('select[data-lookup-select]').forEach(select => {
    const fieldName = select.getAttribute('data-lookup-select') || '';
    select.addEventListener('change', () => {
      const custom = document.querySelector(`[data-lookup-custom="${CSS.escape(fieldName)}"]`);
      if (custom) {
        if (select.value === '__custom__') {
          custom.hidden = false;
          custom.focus();
        } else {
          custom.hidden = true;
          custom.value = '';
        }
      }
      const entry = activeObjectEntry();
      entry.lookup_mappings = captureLookupMappingsFromForm(entry.lookup_mappings || {}, entry);
    });
  });

  body.querySelectorAll('input[data-lookup-custom]').forEach(input => {
    input.addEventListener('change', () => {
      const entry = activeObjectEntry();
      entry.lookup_mappings = captureLookupMappingsFromForm(entry.lookup_mappings || {}, entry);
    });
    input.addEventListener('blur', () => {
      const entry = activeObjectEntry();
      entry.lookup_mappings = captureLookupMappingsFromForm(entry.lookup_mappings || {}, entry);
    });
  });

  refreshRecordTypeObjectLabel();
}

async function openExtIdModal() {
  captureActiveObjectFromForm();
  $('som-extid-modal')?.classList.add('visible');
  await renderExtIdDraftTable();
}

async function scanReferencedObjectsIntoDraft() {
  captureActiveObjectFromForm();
  showLoader('Scanning lookups...', 'Finding referenced objects across the package.');
  try {
    await seedExtIdDraftFromPackageLookups();
    await renderExtIdDraftTable();
    const count = (state.extIdDraft.rows || []).filter(row => row.object).length;
    setMessage(`Loaded ${count} referenced object(s) from package lookups.`, count ? 'ok' : 'warning');
  } finally {
    hideLoader();
  }
}

function closeExtIdModal() {
  $('som-extid-modal')?.classList.remove('visible');
}

function selectedFieldNamesForEntry(entry) {
  const names = new Set();
  for (const item of entry?.field_mappings || []) {
    if (!item?.source_field) continue;
    if (item.include || item.target_field) names.add(item.source_field);
  }
  if (
    state.activeObjectIndex >= 0
    && state.packageObjects[state.activeObjectIndex] === entry
  ) {
    for (const [fieldName, mapping] of Object.entries(state.fieldMappings || {})) {
      if (mapping?.include || mapping?.targetField) names.add(fieldName);
    }
  }
  return names;
}

async function collectReferencedObjectsFromPackage() {
  const refs = new Set();
  const sourceProfile = $('som-source-profile')?.value || 'default';

  async function fieldsForEntry(entry) {
    const objectName = String(entry?.source_object || '').trim();
    if (!objectName) return [];
    if (
      state.activeObjectIndex >= 0
      && state.packageObjects[state.activeObjectIndex] === entry
      && state.sourceFields.length
      && ($('som-source-object')?.value || '') === objectName
    ) {
      return state.sourceFields;
    }
    const cacheKey = metadataCacheKey('source', objectName, sourceProfile);
    if (state.metadataCache[cacheKey]) return state.metadataCache[cacheKey];
    try {
      const response = await fetch(
        `/api/object/${encodeURIComponent(objectName)}?org=source&profile=${encodeURIComponent(sourceProfile)}`,
      );
      const data = await response.json();
      const fields = Array.isArray(data.fields) ? data.fields : [];
      state.metadataCache[cacheKey] = fields;
      return fields;
    } catch (_error) {
      return [];
    }
  }

  function addReferenced(name) {
    const value = String(name || '').trim();
    if (!value || value === 'RecordType') return;
    refs.add(value);
  }

  for (const entry of state.packageObjects || []) {
    const selectedNames = selectedFieldNamesForEntry(entry);
    for (const [fieldName, cfg] of Object.entries(entry.lookup_mappings || {})) {
      if (fieldName === 'RecordTypeId') continue;
      if (selectedNames.size && !selectedNames.has(fieldName)) continue;
      addReferenced(cfg?.referenced_object);
    }
    const fields = await fieldsForEntry(entry);
    const fieldsByName = new Map(fields.map(field => [field.name, field]));
    const namesToScan = selectedNames.size
      ? [...selectedNames]
      : fields.filter(field => fieldSupportsLookupResolution(field)).map(field => field.name);

    for (const fieldName of namesToScan) {
      if (fieldName === 'RecordTypeId') continue;
      const field = fieldsByName.get(fieldName);
      if (field) {
        if (!fieldSupportsLookupResolution(field)) continue;
        for (const ref of field.references_to || []) addReferenced(ref);
        continue;
      }
      // Metadata unavailable — Id-suffix heuristic for mapped *Id fields.
      if (fieldName !== 'Id' && fieldName.endsWith('Id')) {
        addReferenced(fieldName.slice(0, -2));
      }
    }
  }
  return [...refs].sort((a, b) => a.localeCompare(b));
}

async function seedExtIdDraftFromPackageLookups() {
  const referenced = await collectReferencedObjectsFromPackage();
  if (!Array.isArray(state.extIdDraft.rows)) state.extIdDraft.rows = [];
  const byObject = new Map(
    state.extIdDraft.rows
      .filter(row => row?.object)
      .map(row => [row.object, row]),
  );
  for (const objectName of referenced) {
    if (byObject.has(objectName)) continue;
    const fromTemplate = objectExternalIdFromTemplates(objectName);
    const packageParent = packageObjectByName().get(objectName);
    const packageExtId = packageExternalId(packageParent);
    byObject.set(objectName, {
      object: objectName,
      source_reference_field: fromTemplate?.referenced_source_key || 'Id',
      target_external_id: packageExtId || fromTemplate?.external_id || '',
    });
  }
  const rows = [...byObject.values()];
  // Keep any blank trailing row only if nothing was found.
  state.extIdDraft.rows = rows.length ? rows : [{ object: '', target_external_id: '' }];
  // Prefetch external ID field lists for new objects.
  await Promise.all(
    state.extIdDraft.rows
      .filter(row => row.object)
      .map(row => extIdFieldOptionsHtml(row.object, row.target_external_id || '')),
  );
}

async function openRecordTypeModal({ autoMapEmpty = false } = {}) {
  captureActiveObjectFromForm();
  const objectName = currentRecordTypeObjectName();
  if (!objectName) {
    setMessage('Select a package object before opening Record Type mapping.', 'error');
    return;
  }
  const nameInput = $('som-rt-template-name');
  if (nameInput && !(nameInput.value || '').trim()) {
    nameInput.value = `${objectName}_record_types`;
  }
  $('som-rt-modal')?.classList.add('visible');
  await refreshRecordTypeModal({ force: true, autoMapEmpty });
}

function closeRecordTypeModal() {
  $('som-rt-modal')?.classList.remove('visible');
}

function currentRecordTypeObjectName() {
  const entry = activeObjectEntry();
  return String(
    entry.source_object
    || entry.target_object
    || $('som-source-object')?.value
    || $('som-target-object')?.value
    || ''
  ).trim();
}

function refreshRecordTypeObjectLabel() {
  const label = $('som-rt-object-label');
  if (!label) return;
  const objectName = currentRecordTypeObjectName();
  label.textContent = objectName || 'No object';
}

function isRecordTypeModalOpen() {
  return Boolean($('som-rt-modal')?.classList.contains('visible'));
}

async function refreshRecordTypeModal({ force = false, autoMapEmpty = false } = {}) {
  refreshRecordTypeObjectLabel();
  populateRecordTypeTemplateLoadOptions();
  await loadRecordTypeLists({ force });
  if (autoMapEmpty) {
    autoMapRecordTypesByDeveloperName({ fillOnlyEmpty: true, silent: true });
  }
  renderRecordTypePanel({ forceShow: true });
}

async function ensureRecordTypePanel({ force = false, autoMapEmpty = false, forceShow = false } = {}) {
  if (forceShow || isRecordTypeModalOpen()) {
    if (forceShow) $('som-rt-modal')?.classList.add('visible');
    await refreshRecordTypeModal({ force, autoMapEmpty });
    return;
  }
  refreshRecordTypeObjectLabel();
}

function recordTypeIsMappedInForm() {
  const mapping = state.fieldMappings.RecordTypeId || {};
  if (mapping.include || mapping.targetField) return true;
  const entry = activeObjectEntry();
  return (entry.field_mappings || []).some(item => item.source_field === 'RecordTypeId' && item.target_field);
}

async function loadRecordTypeLists({ force = false } = {}) {
  const objectName = currentRecordTypeObjectName();
  if (!objectName) {
    state.recordTypeCache = { objectName: '', sourceProfile: '', targetProfile: '', source: [], target: [] };
    return;
  }
  const sourceProfile = $('som-source-profile').value || 'default';
  const targetProfile = $('som-target-profile').value || 'default';
  const cache = state.recordTypeCache || {};
  if (
    !force
    && cache.objectName === objectName
    && cache.sourceProfile === sourceProfile
    && cache.targetProfile === targetProfile
    && Array.isArray(cache.source)
    && Array.isArray(cache.target)
  ) {
    return;
  }
  showLoader('Loading Record Types...', `Fetching Record Types for ${objectName}.`);
  try {
    const [sourceResp, targetResp] = await Promise.all([
      fetch(`/api/object/${encodeURIComponent(objectName)}/record-types?org=source&profile=${encodeURIComponent(sourceProfile)}`),
      fetch(`/api/object/${encodeURIComponent(objectName)}/record-types?org=target&profile=${encodeURIComponent(targetProfile)}`),
    ]);
    const sourceData = await sourceResp.json();
    const targetData = await targetResp.json();
    if (sourceData.error) throw new Error(sourceData.error);
    if (targetData.error) throw new Error(targetData.error);
    state.recordTypeCache = {
      objectName,
      sourceProfile,
      targetProfile,
      source: Array.isArray(sourceData.record_types) ? sourceData.record_types : [],
      target: Array.isArray(targetData.record_types) ? targetData.record_types : [],
    };
  } finally {
    hideLoader();
  }
}

function autoMapRecordTypesByDeveloperName({ fillOnlyEmpty = false, silent = false } = {}) {
  const entry = activeObjectEntry();
  const targetByDn = {};
  for (const rt of state.recordTypeCache.target || []) {
    targetByDn[String(rt.developer_name || '').trim()] = rt;
  }
  const next = { ...(entry.record_type_mappings || {}) };
  let matched = 0;
  let missing = 0;
  let skipped = 0;
  for (const rt of state.recordTypeCache.source || []) {
    const sourceId = String(rt.id || '').trim();
    if (!sourceId) continue;
    if (fillOnlyEmpty && next[sourceId]) {
      skipped += 1;
      continue;
    }
    const dn = String(rt.developer_name || '').trim();
    const target = targetByDn[dn];
    if (target?.id) {
      next[sourceId] = target.id;
      matched += 1;
    } else {
      missing += 1;
    }
  }
  entry.record_type_resolution = 'mapped';
  entry.record_type_mappings = next;
  if (entry.lookup_mappings) delete entry.lookup_mappings.RecordTypeId;
  if (!silent) {
    setMessage(
      `RecordType auto-map: ${matched} matched by DeveloperName`
      + (skipped ? `, ${skipped} kept as manually set` : '')
      + (missing ? `, ${missing} need a replacement target Record Type` : '')
      + '.',
      missing ? 'warning' : 'ok',
    );
  }
}

function renderRecordTypePanel({ forceShow = false } = {}) {
  const body = $('som-rt-body');
  const count = $('som-rt-count');
  if (!body) return;
  refreshRecordTypeObjectLabel();
  const entry = activeObjectEntry();
  const mappings = entry.record_type_mappings || {};
  const sourceTypes = state.recordTypeCache.source || [];
  const targetTypes = state.recordTypeCache.target || [];
  const objectName = currentRecordTypeObjectName();
  if (count) count.textContent = String(sourceTypes.length);
  if (!objectName) {
    body.innerHTML = '<p class="som-help">Select a package object to load Record Types.</p>';
    return;
  }
  if (!sourceTypes.length) {
    body.innerHTML = `<p class="som-help">No active source Record Types found for ${esc(objectName)}.</p>`;
    return;
  }
  if (!targetTypes.length) {
    body.innerHTML = `<p class="som-help">No active target Record Types found for ${esc(objectName)}. Create or activate Record Types on target first.</p>`;
    return;
  }

  const targetById = new Map(targetTypes.map(rt => [rt.id, rt]));
  const matched = [];
  const needsWork = [];
  for (const rt of sourceTypes) {
    const sourceId = String(rt.id || '').trim();
    const selected = mappings[sourceId] || '';
    const target = selected ? targetById.get(selected) : null;
    const sameDn = Boolean(
      target && String(target.developer_name || '') === String(rt.developer_name || ''),
    );
    const row = { rt, sourceId, selected, target, sameDn };
    if (sameDn) matched.push(row);
    else needsWork.push(row);
  }
  if (count) count.textContent = `${matched.length}/${sourceTypes.length}`;

  const targetOptions = '<option value="">Select target Record Type</option>' + targetTypes.map(rt =>
    `<option value="${esc(rt.id)}">${esc(rt.name || rt.developer_name)} — ${esc(rt.developer_name)}</option>`
  ).join('');

  function sectionHtml(title, rows, emptyText, titleClass) {
    if (!rows.length) {
      return `
        <div class="som-rt-section">
          <div class="som-rt-section-title ${titleClass || ''}">${esc(title)} (0)</div>
          <p class="som-help">${esc(emptyText)}</p>
        </div>
      `;
    }
    const tableRows = rows.map(({ rt, sourceId, selected, sameDn }) => {
      const note = !selected
        ? '<div class="som-help som-rt-mismatch">Needs a target Record Type</div>'
        : (sameDn
          ? '<div class="som-help som-rt-matched">Matched by DeveloperName</div>'
          : '<div class="som-help som-rt-mismatch">Remapped to a different target Record Type</div>');
      return `
        <tr>
          <td>${esc(rt.name || rt.developer_name)}</td>
          <td>${esc(rt.developer_name || '-')}</td>
          <td>
            <select class="fh-input" data-rt-source="${esc(sourceId)}">${targetOptions}</select>
            ${note}
          </td>
        </tr>
      `;
    }).join('');
    return `
      <div class="som-rt-section">
        <div class="som-rt-section-title ${titleClass || ''}">${esc(title)} (${rows.length})</div>
        <div class="som-rt-table-wrap">
          <table class="som-table">
            <thead>
              <tr>
                <th>Source Record Type</th>
                <th>DeveloperName</th>
                <th>Target Record Type</th>
              </tr>
            </thead>
            <tbody>${tableRows}</tbody>
          </table>
        </div>
      </div>
    `;
  }

  body.innerHTML = [
    sectionHtml(
      'Matched by DeveloperName',
      matched,
      'No DeveloperName matches yet — use Re-run DeveloperName match.',
      'som-rt-matched',
    ),
    sectionHtml(
      'Needs replacement or remap',
      needsWork,
      'All source Record Types matched by DeveloperName.',
      'som-rt-mismatch',
    ),
  ].join('');

  body.querySelectorAll('select[data-rt-source]').forEach(select => {
    const sourceId = select.getAttribute('data-rt-source') || '';
    select.value = mappings[sourceId] || '';
    select.addEventListener('change', event => {
      const active = activeObjectEntry();
      active.record_type_mappings = active.record_type_mappings || {};
      const value = event.target.value || '';
      if (value) active.record_type_mappings[sourceId] = value;
      else delete active.record_type_mappings[sourceId];
      active.record_type_resolution = 'mapped';
      renderRecordTypePanel();
      renderMappingTable();
    });
  });
}

async function autoMapRecordTypes() {
  await refreshRecordTypeModal({ force: true, autoMapEmpty: false });
  autoMapRecordTypesByDeveloperName({ fillOnlyEmpty: false, silent: false });
  renderRecordTypePanel({ forceShow: true });
  renderMappingTable();
}

async function loadTemplates() {
  const currentSelection = $('som-template-select').value || state.currentTemplateId || '';
  const response = await fetch('/api/templates');
  const data = await response.json();
  const all = Array.isArray(data.templates) ? data.templates : [];
  state.templates = all.filter(template =>
    template.template_kind === 'source_mapping' || template.template_kind === 'source_cycle_group'
  );
  state.extIdTemplates = all.filter(template => ['object_external_id', 'reference_identity'].includes(template.template_kind));
  state.recordTypeTemplates = all.filter(template => template.template_kind === 'record_type_mapping');

  $('som-template-select').innerHTML = '<option value="">Load package template</option>' + state.templates.map(template =>
    `<option value="${esc(template.id)}">${esc(template.name || template.id)}</option>`
  ).join('');
  if ([...$('som-template-select').options].some(option => option.value === currentSelection)) {
    $('som-template-select').value = currentSelection;
  }

  const extLoad = $('som-extid-load');
  if (extLoad) {
    const currentExt = extLoad.value || state.extIdDraft.templateId || '';
    extLoad.innerHTML = '<option value="">Load Object External ID template</option>' + state.extIdTemplates.map(template => {
      const names = Array.isArray(template.object_names) && template.object_names.length
        ? template.object_names.join(', ')
        : (template.source_object || template.seed_object || '');
      const count = template.object_count || (template.object_names || []).length || 0;
      return `<option value="${esc(template.id)}">${esc(template.name || template.id)} — ${esc(names)} (${count})</option>`;
    }).join('');
    if ([...extLoad.options].some(option => option.value === currentExt)) extLoad.value = currentExt;
  }

  const rtLoad = $('som-rt-load');
  if (rtLoad) {
    populateRecordTypeTemplateLoadOptions(rtLoad.value || '');
  }

  renderExtIdDraftTable();
  refreshRecordTypeObjectLabel();
  const summary = selectedTemplateSummary();
  if (summary) {
    state.currentPrevalidationStatus = summary.prevalidation_status || 'DRAFT';
  } else if (!state.currentTemplateId) {
    state.currentPrevalidationStatus = 'DRAFT';
  }
  renderTemplateStatus();
}

function populateRecordTypeTemplateLoadOptions(currentValue = '') {
  const rtLoad = $('som-rt-load');
  if (!rtLoad) return;
  const objectName = currentRecordTypeObjectName();
  const filtered = (state.recordTypeTemplates || []).filter(template => {
    if (!objectName) return true;
    const templateObject = String(template.source_object || template.seed_object || '').trim();
    return !templateObject || templateObject === objectName;
  });
  rtLoad.innerHTML = '<option value="">Load Record Type template</option>' + filtered.map(template => {
    const name = template.source_object || template.seed_object || '';
    const count = template.record_type_mapping_count || 0;
    return `<option value="${esc(template.id)}">${esc(template.name || template.id)} — ${esc(name)} (${count})</option>`;
  }).join('');
  if ([...rtLoad.options].some(option => option.value === currentValue)) rtLoad.value = currentValue;
}

function extIdObjectOptionsHtml(selected = '') {
  const objects = state.targetObjects.length ? state.targetObjects : state.sourceObjects;
  return '<option value="">Select object</option>' + objects.map(item =>
    `<option value="${esc(item.name)}" ${item.name === selected ? 'selected' : ''}>${esc(item.label || item.name)} — ${esc(item.name)}</option>`
  ).join('');
}

async function extIdFieldOptionsHtml(objectName, selected = '') {
  if (!objectName) return '<option value="">Select external ID</option>';
  const profile = $('som-target-profile').value || 'default';
  const cacheKey = metadataCacheKey('target', objectName, profile);
  let fields = state.metadataCache[cacheKey];
  let externalIds = state.refExternalIdCache[objectName] || [];
  if (!fields) {
    const response = await fetch(`/api/object/${encodeURIComponent(objectName)}?org=target&profile=${encodeURIComponent(profile)}`);
    const data = await response.json();
    fields = Array.isArray(data.fields) ? data.fields : [];
    state.metadataCache[cacheKey] = fields;
    externalIds = Array.isArray(data.external_id_fields)
      ? data.external_id_fields
      : fields.filter(field => field.is_external_id).map(field => field.name);
    state.refExternalIdCache[objectName] = externalIds;
  }
  if (!externalIds.length) {
    externalIds = (fields || []).filter(field => field.is_external_id && field.writable).map(field => field.name);
  }
  return '<option value="">Select external ID</option>' + externalIds.map(name =>
    `<option value="${esc(name)}" ${name === selected ? 'selected' : ''}>${esc(name)}</option>`
  ).join('');
}

async function sourceReferenceFieldOptionsHtml(objectName, selected = '') {
  if (!objectName) return '<option value="">Select source key</option>';
  const profile = $('som-source-profile').value || 'default';
  const response = await fetch(`/api/object/${encodeURIComponent(objectName)}?org=source&profile=${encodeURIComponent(profile)}`);
  const data = await response.json();
  const fields = [{ name: 'Id' }, ...(Array.isArray(data.fields) ? data.fields : [])]
    .filter((field, index, all) => field.name && all.findIndex(item => item.name === field.name) === index);
  return '<option value="">Select source key</option>' + fields.map(field =>
    `<option value="${esc(field.name)}" ${field.name === selected ? 'selected' : ''}>${esc(field.name)}</option>`
  ).join('');
}

async function renderExtIdDraftTable() {
  const body = $('som-extid-body');
  const count = $('som-extid-count');
  if (!body) return;
  if (!Array.isArray(state.extIdDraft.rows) || !state.extIdDraft.rows.length) {
    state.extIdDraft.rows = [{ object: '', target_external_id: '' }];
  }
  if (count) count.textContent = String(state.extIdDraft.rows.filter(row => row.object).length);
  const rowsHtml = [];
  for (let index = 0; index < state.extIdDraft.rows.length; index += 1) {
    const row = state.extIdDraft.rows[index];
    row.source_reference_field = String(row.source_reference_field || 'Id').trim() || 'Id';
    const sourceOptions = await sourceReferenceFieldOptionsHtml(row.object || '', row.source_reference_field || 'Id');
    const fieldOptions = await extIdFieldOptionsHtml(row.object || '', row.target_external_id || '');
    rowsHtml.push(`
      <tr>
        <td>
          <select class="fh-input" data-extid-object="${index}">
            ${extIdObjectOptionsHtml(row.object || '')}
          </select>
        </td>
        <td><select class="fh-input" data-extid-source-field="${index}">${sourceOptions}</select></td>
        <td>
          <select class="fh-input" data-extid-field="${index}">
            ${fieldOptions}
          </select>
        </td>
        <td>
          <button type="button" class="btn btn-secondary" data-extid-remove="${index}" ${state.extIdDraft.rows.length <= 1 ? 'disabled' : ''}>Remove</button>
        </td>
      </tr>
    `);
  }
  body.innerHTML = rowsHtml.join('');

  body.querySelectorAll('select[data-extid-object]').forEach(select => {
    const index = Number(select.getAttribute('data-extid-object'));
    select.addEventListener('change', async () => {
      state.extIdDraft.rows[index] = state.extIdDraft.rows[index] || { object: '', target_external_id: '' };
      state.extIdDraft.rows[index].object = select.value || '';
      state.extIdDraft.rows[index].source_reference_field = 'Id';
      state.extIdDraft.rows[index].target_external_id = '';
      await renderExtIdDraftTable();
    });
  });
  body.querySelectorAll('select[data-extid-source-field]').forEach(select => {
    const index = Number(select.getAttribute('data-extid-source-field'));
    select.addEventListener('change', () => {
      state.extIdDraft.rows[index].source_reference_field = select.value || '';
    });
  });
  body.querySelectorAll('select[data-extid-field]').forEach(select => {
    const index = Number(select.getAttribute('data-extid-field'));
    select.addEventListener('change', () => {
      state.extIdDraft.rows[index] = state.extIdDraft.rows[index] || { object: '', target_external_id: '' };
      state.extIdDraft.rows[index].target_external_id = select.value || '';
      if ($('som-extid-count')) {
        $('som-extid-count').textContent = String(state.extIdDraft.rows.filter(row => row.object).length);
      }
    });
  });
  body.querySelectorAll('button[data-extid-remove]').forEach(button => {
    button.addEventListener('click', async () => {
      const index = Number(button.getAttribute('data-extid-remove'));
      state.extIdDraft.rows.splice(index, 1);
      if (!state.extIdDraft.rows.length) state.extIdDraft.rows = [{ object: '', target_external_id: '' }];
      await renderExtIdDraftTable();
    });
  });
}

async function saveObjectExternalIdTemplate() {
  const templateName = ($('som-extid-template-name').value || '').trim();
  if (!templateName) {
    setMessage('Object External ID template name is required.', 'error');
    return;
  }
  const objects = [];
  const seen = new Set();
  for (const row of state.extIdDraft.rows || []) {
    const objectName = String(row.object || '').trim();
    const externalId = String(row.target_external_id || '').trim();
    const sourceReferenceField = String(row.source_reference_field || '').trim();
    if (!objectName && !externalId) continue;
    if (!objectName || !sourceReferenceField || !externalId) {
      setMessage('Each Reference Identity row needs an object, source reference key, and target match key.', 'error');
      return;
    }
    if (seen.has(objectName)) {
      setMessage(`Duplicate object '${objectName}' in Object External ID template.`, 'error');
      return;
    }
    seen.add(objectName);
    objects.push({
      object: objectName,
      source_object: objectName,
      target_object: objectName,
      target_external_id: externalId,
      match_mapping: {
        source_field: sourceReferenceField,
        target_field: externalId,
      },
    });
  }
  if (!objects.length) {
    setMessage('Add at least one object + external ID before saving.', 'error');
    return;
  }
  const payload = {
    template_id: state.extIdDraft.templateId || '',
    template_name: templateName,
    input_mode: 'salesforce_source',
    template_kind: 'reference_identity',
    source_profile: $('som-source-profile').value || 'default',
    target_profile: $('som-target-profile').value || 'default',
    prevalidation_status: 'DRAFT',
    template: { objects },
  };
  const response = await fetch('/api/templates', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok || data.error) {
    setMessage(data.error || 'Failed to save Object External ID template.', 'error');
    return;
  }
  state.extIdDraft.templateId = data.template_id || '';
  await loadTemplates();
  if ($('som-extid-load')) $('som-extid-load').value = data.template_id || '';
  const names = objects.map(item => `${item.object}.${item.target_external_id}`).join(', ');
  setMessage(
    `Saved Object External ID template "${data.template_name}" with ${objects.length} object(s): ${names}. Use Lookup Resolution → Object external ID template.`,
    'ok',
  );
}

async function loadSelectedExtIdTemplate() {
  const templateId = $('som-extid-load')?.value || '';
  if (!templateId) return;
  const response = await fetch(`/api/templates/${encodeURIComponent(templateId)}`);
  const data = await response.json();
  if (data.error) {
    setMessage(data.error, 'error');
    return;
  }
  const template = data.template || {};
  let rows = [];
  if (Array.isArray(template.objects) && template.objects.length) {
    rows = template.objects.map(item => ({
      object: String(item.object || item.target_object || item.source_object || '').trim(),
      source_reference_field: String(item.match_mapping?.source_field || 'Id').trim(),
      target_external_id: String(
        item.match_mapping?.target_field
        || item.target_external_id
        || ''
      ).trim(),
    }));
  } else {
    const objectName = String(template.object || template.target_object || template.source_object || '').trim();
    const externalId = String(
      template.match_mapping?.target_field
      || template.target_external_id
      || ''
    ).trim();
    if (objectName || externalId) {
      rows = [{ object: objectName, source_reference_field: String(template.match_mapping?.source_field || 'Id'), target_external_id: externalId }];
    }
  }
  if (!rows.length) rows = [{ object: '', target_external_id: '' }];
  state.extIdDraft = {
    templateId: data.template_id || templateId,
    rows,
  };
  $('som-extid-template-name').value = data.template_name || templateId;
  await renderExtIdDraftTable();
  setMessage(
    `Loaded Object External ID template "${data.template_name || templateId}" with ${rows.filter(row => row.object).length} object(s).`,
    'ok',
  );
}

async function useCurrentObjectForExtIdTemplate() {
  const objectName = $('som-target-object')?.value || $('som-source-object')?.value || '';
  const externalId = $('som-target-match-field')?.value || '';
  if (!objectName) {
    setMessage('Select a package object first.', 'error');
    return;
  }
  const existing = state.extIdDraft.rows.find(row => row.object === objectName);
  if (existing) {
    existing.source_reference_field = $('som-source-match-field')?.value || existing.source_reference_field || 'Id';
    existing.target_external_id = externalId || existing.target_external_id;
  } else {
    const blank = state.extIdDraft.rows.find(row => !row.object && !row.target_external_id);
    if (blank) {
      blank.object = objectName;
      blank.source_reference_field = $('som-source-match-field')?.value || 'Id';
      blank.target_external_id = externalId;
    } else {
      state.extIdDraft.rows.push({ object: objectName, source_reference_field: $('som-source-match-field')?.value || 'Id', target_external_id: externalId });
    }
  }
  if (!$('som-extid-template-name').value) {
    $('som-extid-template-name').value = 'shared_lookup_external_ids';
  }
  await renderExtIdDraftTable();
  setMessage(`Added ${objectName}${externalId ? `.${externalId}` : ''} to the Object External ID draft.`, 'ok');
}

async function saveRecordTypeTemplate() {
  const entry = activeObjectEntry();
  const objectName = currentRecordTypeObjectName();
  const templateName = ($('som-rt-template-name').value || '').trim();
  if (!templateName) {
    setMessage('Record Type template name is required.', 'error');
    return;
  }
  if (!objectName) {
    setMessage('Select a package object before saving a Record Type template.', 'error');
    return;
  }
  if (!isRecordTypeModalOpen()) {
    await openRecordTypeModal({ autoMapEmpty: false });
  } else {
    await refreshRecordTypeModal({ force: false, autoMapEmpty: false });
  }
  const mappings = entry.record_type_mappings || {};
  if (!Object.keys(mappings).length) {
    setMessage('Map at least one Record Type (or Auto-map by DeveloperName) before saving the template.', 'error');
    return;
  }
  const payload = {
    template_id: '',
    template_name: templateName,
    input_mode: 'salesforce_source',
    template_kind: 'record_type_mapping',
    source_profile: $('som-source-profile').value || 'default',
    target_profile: $('som-target-profile').value || 'default',
    prevalidation_status: 'DRAFT',
    template: {
      object: objectName,
      source_object: objectName,
      target_object: objectName,
      record_type_resolution: entry.record_type_resolution || 'mapped',
      record_type_mappings: { ...mappings },
    },
  };
  const response = await fetch('/api/templates', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok || data.error) {
    setMessage(data.error || 'Failed to save Record Type template.', 'error');
    return;
  }
  await loadTemplates();
  if ($('som-rt-load')) $('som-rt-load').value = data.template_id || '';
  setMessage(`Saved Record Type template "${data.template_name}" for ${objectName} (${Object.keys(mappings).length} mapping(s)).`, 'ok');
}

async function loadSelectedRecordTypeTemplate() {
  const templateId = $('som-rt-load')?.value || '';
  if (!templateId) return;
  const response = await fetch(`/api/templates/${encodeURIComponent(templateId)}`);
  const data = await response.json();
  if (data.error) {
    setMessage(data.error, 'error');
    return;
  }
  const template = data.template || {};
  const objectName = String(template.object || template.target_object || template.source_object || '').trim();
  const entry = activeObjectEntry();
  if (objectName && !entry.source_object && !entry.target_object) {
    $('som-source-object').value = objectName;
    $('som-target-object').value = objectName;
    entry.source_object = objectName;
    entry.target_object = objectName;
  }
  if (objectName && entry.source_object && entry.source_object !== objectName) {
    setMessage(
      `Record Type template is for ${objectName}, but the active package object is ${entry.source_object}. Switch object tab/selection first.`,
      'error',
    );
    return;
  }
  entry.record_type_resolution = template.record_type_resolution || 'mapped';
  entry.record_type_mappings = template.record_type_mappings && typeof template.record_type_mappings === 'object'
    ? { ...template.record_type_mappings }
    : {};
  $('som-rt-template-name').value = data.template_name || templateId;
  if (!isRecordTypeModalOpen()) $('som-rt-modal')?.classList.add('visible');
  await refreshRecordTypeModal({ force: true, autoMapEmpty: false });
  renderMappingTable();
  setMessage(
    `Loaded Record Type template "${data.template_name || templateId}" for ${objectName || currentRecordTypeObjectName()} (${Object.keys(entry.record_type_mappings).length} mapping(s)).`,
    'ok',
  );
}

async function loadSelectedTemplate() {
  const templateId = $('som-template-select').value;
  if (!templateId) return;
  const response = await fetch(`/api/templates/${encodeURIComponent(templateId)}`);
  const data = await response.json();
  if (data.error) {
    setMessage(data.error, 'error');
    return;
  }
  const template = data.template || {};
  state.currentTemplateId = data.template_id || templateId;
  state.currentPrevalidationStatus = data.prevalidation_status || 'DRAFT';
  $('som-template-name').value = data.template_name || templateId;
  $('som-source-profile').value = data.source_profile || 'default';
  $('som-target-profile').value = data.target_profile || 'default';
  await Promise.all([loadObjects('source'), loadObjects('target')]);
  const mappings = Array.isArray(template.objects) && template.objects.length
    ? template.objects.map(item => ({ ...emptyObjectEntry(), ...item, match_mapping: { source_field: '', target_field: '', ...(item.match_mapping || {}) } }))
    : [{
      ...emptyObjectEntry(),
      source_object: template.source_object || '',
      target_object: template.target_object || '',
      query_filter: template.query_filter || '',
      allowed_operations: Array.isArray(template.allowed_operations) ? template.allowed_operations : ['upsert'],
      default_operation: template.default_operation || 'upsert',
      match_mapping: {
        source_field: template.match_mapping?.source_field || '',
        target_field: template.match_mapping?.target_field || '',
      },
      target_external_id: template.target_external_id || '',
      field_mappings: Array.isArray(template.field_mappings) ? template.field_mappings : [],
      lookup_mappings: template.lookup_mappings && typeof template.lookup_mappings === 'object'
        ? { ...template.lookup_mappings }
        : {},
      cleared_lookups: template.cleared_lookups && typeof template.cleared_lookups === 'object'
        ? { ...template.cleared_lookups }
        : {},
    }];
  state.packageObjects = mappings.map(item => ({
    ...item,
    lookup_mappings: item.lookup_mappings && typeof item.lookup_mappings === 'object'
      ? { ...item.lookup_mappings }
      : {},
    cleared_lookups: item.cleared_lookups && typeof item.cleared_lookups === 'object'
      ? { ...item.cleared_lookups }
      : {},
    record_type_resolution: item.record_type_resolution || 'developer_name',
    record_type_mappings: item.record_type_mappings && typeof item.record_type_mappings === 'object'
      ? { ...item.record_type_mappings }
      : {},
  }));
  state.activeObjectIndex = 0;
  showLoader('Loading template...', 'Restoring package objects and field mappings.');
  try {
    await applyActiveObjectToForm({ loadMetadata: true });
  } finally {
    hideLoader();
  }
  renderTemplateStatus();
  const objectCount = state.packageObjects.length;
  setMessage(
    `Loaded template "${data.template_name || templateId}" with ${objectCount} object(s).`,
    'ok',
  );
}

function addToPipeline() {
  if (!state.currentTemplateId) {
    setMessage('Save the template before adding it to a pipeline.', 'error');
    return;
  }
  window.location.href = `/pipeline?add_seed=${encodeURIComponent(state.currentTemplateId)}`;
}

async function saveTemplate() {
  await loadTemplates();
  captureActiveObjectFromForm();
  const templateName = ($('som-template-name').value || '').trim();
  if (!templateName) {
    setMessage('Template name is required.', 'error');
    return;
  }
  const objects = [];
  const warnings = [];
  for (let index = 0; index < state.packageObjects.length; index += 1) {
    const entry = state.packageObjects[index];
    const sourceObject = String(entry.source_object || '').trim();
    const targetObject = String(entry.target_object || '').trim();
    const operations = Array.isArray(entry.allowed_operations) ? entry.allowed_operations.filter(Boolean) : [];
    const sourceMatchField = String(entry.match_mapping?.source_field || '').trim();
    const targetMatchField = String(entry.match_mapping?.target_field || '').trim();
    const mappings = Array.isArray(entry.field_mappings) ? entry.field_mappings : [];
    if (!sourceObject || !targetObject) {
      setMessage(`Object ${index + 1}: source object and target object are required.`, 'error');
      return;
    }
    if (sourceObject !== targetObject) {
      setMessage(`Object ${index + 1}: runtime currently supports same object API names only (${sourceObject} → ${targetObject}).`, 'error');
      return;
    }
    if (!operations.length) {
      setMessage(`Object ${index + 1}: select at least one allowed operation.`, 'error');
      return;
    }
    if ((operations.includes('update') || operations.includes('upsert')) && (!sourceMatchField || !targetMatchField)) {
      setMessage(`Object ${index + 1}: update/upsert require source and target match fields.`, 'error');
      return;
    }
    if (!mappings.length) {
      setMessage(`Object ${index + 1}: map at least one field before saving.`, 'error');
      return;
    }
    const defaultOperation = operations.includes('upsert')
      ? 'upsert'
      : operations.includes('insert')
        ? 'insert'
        : operations[0];
    const lookupMappings = refreshLookupMappingsForEntry(
      {
        ...entry,
        source_object: sourceObject,
        target_object: targetObject,
        match_mapping: {
          source_field: sourceMatchField,
          target_field: targetMatchField,
        },
        target_external_id: targetMatchField,
        field_mappings: mappings,
        lookup_mappings: entry.lookup_mappings || {},
        cleared_lookups: entry.cleared_lookups || {},
      },
      state.packageObjects,
      index === state.activeObjectIndex ? state.sourceFields : null,
    );
    for (const [fieldName, cfg] of Object.entries(lookupMappings || {})) {
      if (String(cfg?.lookup_resolution || '').toLowerCase() !== 'object_template') continue;
      if (String(cfg?.external_id || '').trim()) continue;
      const ref = String(cfg?.referenced_object || fieldName).trim() || fieldName;
      setMessage(
        `Object ${index + 1}: lookup '${fieldName}' uses Object external ID template, but no saved Source Org template external ID was found for '${ref}'. Save a template for that object first (or choose Custom Object.ExternalId).`,
        'error',
      );
      return;
    }
    if (lookupMappings.RecordTypeId) delete lookupMappings.RecordTypeId;
    const usesRecordType = mappings.some(item => item.source_field === 'RecordTypeId');
    if (usesRecordType) {
      await loadRecordTypeLists({ force: false });
      const sourceTypes = state.recordTypeCache.source || [];
      const mappedIds = entry.record_type_mappings || {};
      const missing = sourceTypes
        .filter(rt => rt.id && !mappedIds[rt.id])
        .map(rt => rt.developer_name || rt.name || rt.id);
      if (missing.length) {
        setMessage(
          `Object ${index + 1}: RecordTypeId is mapped, but ${missing.length} source Record Type(s) have no target replacement (${missing.slice(0, 5).join(', ')}${missing.length > 5 ? ', …' : ''}). Use Record Type Mapping to assign an existing target Record Type.`,
          'error',
        );
        return;
      }
    }
    objects.push({
      source_object: sourceObject,
      target_object: targetObject,
      query_filter: String(entry.query_filter || ''),
      allowed_operations: operations,
      default_operation: defaultOperation,
      match_mapping: {
        source_field: sourceMatchField,
        target_field: targetMatchField,
      },
      target_external_id: targetMatchField,
      field_mappings: mappings.map(item => ({
        source_field: item.source_field,
        target_field: item.target_field,
      })),
      lookup_mappings: lookupMappings,
      cleared_lookups: entry.cleared_lookups || {},
      record_type_resolution: entry.record_type_resolution || 'developer_name',
      record_type_mappings: entry.record_type_mappings && typeof entry.record_type_mappings === 'object'
        ? { ...entry.record_type_mappings }
        : {},
    });
  }

  // Final pass: sync external IDs from each parent object's match field; fill only gaps (respect cleared).
  for (const item of objects) {
    const cleared = item.cleared_lookups || {};
    item.lookup_mappings = refreshLookupMappingsForEntry(item, objects, null);
    item.cleared_lookups = cleared;
  }

  // Store only relationship intent for package-local parents. Python derives
  // the parent's identity, so Parent.ExternalId is not duplicated per lookup.
  const packageNames = new Set(objects.map(item => String(item.source_object || '').trim()));
  for (const item of objects) {
    for (const cfg of Object.values(item.lookup_mappings || {})) {
      const referencedObject = String(cfg?.referenced_object || '').trim();
      if (!packageNames.has(referencedObject)) continue;
      const resolution = String(cfg?.lookup_resolution || '').trim().toLowerCase();
      if (['preserve', 'target_id', 'idmapper'].includes(resolution)) continue;
      cfg.lookup_resolution = 'pipeline_object';
      delete cfg.external_id;
      delete cfg.template_id;
      delete cfg.template_name;
    }
  }

  const seen = new Set();
  for (const item of objects) {
    const key = item.source_object;
    if (seen.has(key)) {
      setMessage(`Duplicate object '${key}' in package. Each object may appear once.`, 'error');
      return;
    }
    seen.add(key);
  }

  const multiObject = objects.length > 1;
  const payload = {
    template_id: state.currentTemplateId,
    template_name: templateName,
    input_mode: 'salesforce_source',
    template_kind: multiObject ? 'source_cycle_group' : 'source_mapping',
    source_profile: $('som-source-profile').value || 'default',
    target_profile: $('som-target-profile').value || 'default',
    prevalidation_status: 'DRAFT',
    template: multiObject
      ? { objects }
      : {
        source_object: objects[0].source_object,
        target_object: objects[0].target_object,
        query_filter: objects[0].query_filter,
        allowed_operations: objects[0].allowed_operations,
        default_operation: objects[0].default_operation,
        match_mapping: objects[0].match_mapping,
        target_external_id: objects[0].target_external_id,
        field_mappings: objects[0].field_mappings,
        lookup_mappings: objects[0].lookup_mappings || {},
        cleared_lookups: objects[0].cleared_lookups || {},
        record_type_resolution: objects[0].record_type_resolution || 'developer_name',
        record_type_mappings: objects[0].record_type_mappings || {},
      },
  };
  const response = await fetch('/api/templates', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok || data.error) {
    setMessage(data.error || 'Failed to save template.', 'error');
    return;
  }
  state.currentTemplateId = data.template_id || '';
  state.currentPrevalidationStatus = 'DRAFT';
  await loadTemplates();
  $('som-template-select').value = state.currentTemplateId;
  renderObjectTabs();
  renderTemplateStatus();
  const kindLabel = multiObject ? 'multi-object package' : 'template';
  setMessage(`Saved ${kindLabel} "${data.template_name}" with ${objects.length} object(s). Add it once in Pipeline Designer as a single stage.`, 'ok');
  if (warnings.length) {
    setMessage(`${warnings.join(' ')}`, 'warning');
  }
}
