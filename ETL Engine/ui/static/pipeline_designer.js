const DESIGNER_STORAGE_KEY = 'sfetl_pipeline_designer_v1';
const DESIGNER_TAB_ID_KEY = 'sfetl_pipeline_designer_tab_id';
const DESIGNER_WORKSPACE_QUERY_PARAM = 'workspace';

function slugifyWorkspaceId(value) {
  return String(value || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 80);
}

function getDesignerWorkspaceId() {
  const params = new URLSearchParams(window.location.search || '');
  return slugifyWorkspaceId(params.get(DESIGNER_WORKSPACE_QUERY_PARAM) || '');
}

function buildPipelineWorkspaceId() {
  const migrationName = $('designer-migration-name')?.value || designerState.runStatus?.migration || 'pipeline';
  return slugifyWorkspaceId(migrationName) || `pipeline_${Date.now()}`;
}

function buildPipelineWorkspaceUrl(workspaceId) {
  const url = new URL(window.location.href);
  if (workspaceId) {
    url.searchParams.set(DESIGNER_WORKSPACE_QUERY_PARAM, workspaceId);
  } else {
    url.searchParams.delete(DESIGNER_WORKSPACE_QUERY_PARAM);
  }
  return url.toString();
}

function getDesignerTabId() {
  let tabId = window.sessionStorage.getItem(DESIGNER_TAB_ID_KEY);
  if (!tabId) {
    tabId = `tab_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    window.sessionStorage.setItem(DESIGNER_TAB_ID_KEY, tabId);
  }
  return tabId;
}

function getDesignerStorageKey() {
  const workspaceId = getDesignerWorkspaceId();
  if (workspaceId) {
    return `${DESIGNER_STORAGE_KEY}:workspace:${workspaceId}`;
  }
  return `${DESIGNER_STORAGE_KEY}:${getDesignerTabId()}`;
}

function getDesignerStorageArea() {
  return getDesignerWorkspaceId() ? window.localStorage : window.sessionStorage;
}

const designerState = {
  savedTemplates: [],
  filteredTemplates: [],
  pipelineStages: [],
  pipelineType: '',
  runStatus: {
    migration: null,
    started_at: null,
    last_updated: null,
    pipelines: {},
    runner: {
      running: false,
      launching: false,
      pid: null,
      started_at: null,
      log_path: '',
      config_path: '',
      returncode: null,
    },
  },
  failedFiles: {
    pipelineId: '',
    runName: '',
    directories: [],
    files: [],
    selectedPaths: [],
  },
  sourceProfiles: [],
  targetProfiles: [],
  savedSnapshots: [],
  transformationProfiles: [],
  usedPipelineNames: [],
  claimedPipelineName: '',
  savedSnapshotId: '',
  savedSnapshotFingerprint: '',
  sourceProfile: 'default',
  targetProfile: 'default',
  domainFilters: {
    sourceDomain: '',
    targetDomain: '',
  },
  panels: {
    failedCollapsed: false,
    logsCollapsed: false,
  },
  activeTransformStageIndex: null,
  suppressStoredRunStatus: false,
};

function normalizeInputMode(value) {
  const normalized = String(value || '').trim().toLowerCase();
  if (normalized === 'flat_file') return 'flat_file';
  if (normalized === 'salesforce_seed' || normalized === 'salesforce_source') return 'salesforce_source';
  return 'salesforce_source';
}

function normalizePipelineType(value) {
  const normalized = String(value || '').trim().toLowerCase();
  if (!normalized) return '';
  if (normalized === 'flat_file') return 'flat_file';
  if (normalized === 'salesforce_seed' || normalized === 'salesforce_source') return 'salesforce_source';
  return '';
}

function pipelineTypeLabel(value) {
  const type = normalizePipelineType(value);
  if (type === 'flat_file') return 'Files → SF';
  if (type === 'salesforce_source') return 'SF → SF';
  return 'Select Pipeline Type';
}

function modeBadgeLabel(value) {
  return normalizeInputMode(value) === 'flat_file' ? 'FILES → SF' : 'SF → SF';
}

function orgDomainLabel(value) {
  const host = String(value || '').replace(/^https?:\/\//i, '').split('/')[0].trim();
  return host || '';
}

function templateSourceDomain(template) {
  return orgDomainLabel(template?.source_domain || template?.sourceDomain);
}

function templateTargetDomain(template) {
  return orgDomainLabel(template?.target_domain || template?.targetDomain);
}

function _currentTargetDomain() {
  const stage = designerState.pipelineStages?.[0] || {};
  const template = designerState.savedTemplates.find(item => item.id === stage.templateId) || {};
  const profileId = getSelectedTargetProfile() || designerState.targetProfile || '';
  const profile = (designerState.targetProfiles || []).find(item => item.id === profileId) || {};
  return [
    stage.targetDomain,
    stage.target_domain,
    template.target_domain,
    template.targetDomain,
    profile.login_domain,
    profile.login_url,
    profile.instance_url,
    profile.label,
    profileId,
  ].filter(Boolean).join(' ').toLowerCase();
}

function _isSandboxTarget() {
  const domain = _currentTargetDomain();
  return domain.includes('.sandbox.') || domain.includes('--') || domain.includes('test.salesforce.com') || /\bsandbox\b/.test(domain);
}

function _isProductionTarget() {
  const domain = _currentTargetDomain().trim();
  return Boolean(domain) && !_isSandboxTarget();
}

function _syncEmailMaskDefaults() {
  const maskCb = document.getElementById('mask-emails-checkbox');
  const removeCb = document.getElementById('remove-email-mask-checkbox');
  if (!maskCb || !removeCb) return;
  if (_isSandboxTarget()) {
    maskCb.checked = true;
    removeCb.checked = false;
    return;
  }
  if (_isProductionTarget()) {
    maskCb.checked = false;
    removeCb.checked = true;
  }
}

function _getMaskEmailsValue() {
  const cb = document.getElementById('mask-emails-checkbox');
  return cb ? cb.checked : _isSandboxTarget();
}

function _getRemoveEmailMaskValue() {
  const cb = document.getElementById('remove-email-mask-checkbox');
  return cb ? cb.checked : _isProductionTarget();
}

function _getStopAfterTransformValue() {
  const cb = document.getElementById('stop-after-transform-checkbox');
  return cb ? cb.checked : false;
}

const DEFAULT_LOAD_BATCH_SIZE = 50000;
const MIN_LOAD_BATCH_SIZE = 100;
const MAX_LOAD_BATCH_SIZE = 100000;

function _parseLoadBatchSize(raw) {
  const n = Number(String(raw ?? '').trim().replace(/,/g, ''));
  if (!Number.isInteger(n) || n < MIN_LOAD_BATCH_SIZE || n > MAX_LOAD_BATCH_SIZE) {
    return null;
  }
  return n;
}

function _getLoadBatchSize() {
  return _parseLoadBatchSize($('designer-load-batch-size')?.value) || DEFAULT_LOAD_BATCH_SIZE;
}

function _askLoadBatchSize() {
  const current = _getLoadBatchSize();
  const entered = window.prompt(
    `Load batch size (records per Bulk API job, ${MIN_LOAD_BATCH_SIZE}–${MAX_LOAD_BATCH_SIZE}):`,
    String(current)
  );
  if (entered === null) {
    return null;
  }
  const parsed = _parseLoadBatchSize(entered);
  if (parsed == null) {
    alert(`Batch size must be a whole number between ${MIN_LOAD_BATCH_SIZE} and ${MAX_LOAD_BATCH_SIZE}.`);
    return null;
  }
  const input = $('designer-load-batch-size');
  if (input) {
    input.value = String(parsed);
  }
  return parsed;
}

function getSelectedPipelineType() {
  const select = $('designer-pipeline-type');
  if (select) {
    // Prefer the select value even when empty so clearing type is intentional.
    return normalizePipelineType(select.value);
  }
  return normalizePipelineType(designerState.pipelineType || '');
}

function inferPipelineTypeFromStages(stages = designerState.pipelineStages) {
  const modes = [...new Set(
    (stages || [])
      .map(stage => normalizeInputMode(stage?.inputMode))
      .filter(Boolean),
  )];
  if (modes.length === 1) return modes[0];
  return '';
}

function applyPipelineTypeUi() {
  const type = normalizePipelineType(
    $('designer-pipeline-type')?.value ?? designerState.pipelineType ?? '',
  );
  designerState.pipelineType = type;
  if ($('designer-pipeline-type') && $('designer-pipeline-type').value !== type) {
    $('designer-pipeline-type').value = type;
  }
  const sourceProfile = $('designer-source-profile');
  if (sourceProfile) {
    sourceProfile.hidden = type === 'flat_file';
    sourceProfile.disabled = type === 'flat_file';
  }
  const targetProfile = $('designer-target-profile');
  if (targetProfile) {
    targetProfile.hidden = false;
    targetProfile.disabled = false;
  }
  const subtitle = $('designer-library-subtitle');
  if (subtitle) {
    if (!type) {
      subtitle.textContent = 'Select a pipeline type to see matching templates.';
    } else if (type === 'flat_file') {
      subtitle.textContent = 'Files → SF templates only. Put cyclic objects in one multi-file template (one stage).';
    } else {
      subtitle.textContent = 'SF → SF templates only. Multi-object packages from Source Org stay as one stage for two-pass cycles.';
    }
  }
}

function normalizePrevalidationStatus(value) {
  return String(value || 'DRAFT').trim().toUpperCase() || 'DRAFT';
}

function isStageValidationReady(stage) {
  return ['PASSED', 'BYPASSED'].includes(normalizePrevalidationStatus(stage?.prevalidationStatus));
}

function normalizeProfileId(value) {
  const normalized = String(value || '').trim().toLowerCase();
  return normalized || 'default';
}

function stageSourceProfile(stage) {
  if (normalizeInputMode(stage?.inputMode) === 'flat_file') return '';
  return normalizeProfileId(stage?.sourceProfile);
}

function stageTargetProfile(stage) {
  return normalizeProfileId(stage?.targetProfile);
}

function collectPipelineTypeIntegrity() {
  const selectedType = getSelectedPipelineType();
  const stageModes = [...new Set(
    (designerState.pipelineStages || [])
      .map(stage => normalizeInputMode(stage?.inputMode))
      .filter(Boolean),
  )];
  const mismatchedStages = (designerState.pipelineStages || []).filter(stage => {
    const mode = normalizeInputMode(stage?.inputMode);
    if (!selectedType) return Boolean(mode);
    return mode !== selectedType;
  });
  const mixedTypes = stageModes.length > 1;
  const hasMismatch = mismatchedStages.length > 0;
  let message = '';
  if (!selectedType && designerState.pipelineStages.length) {
    message = 'Select a Pipeline Type, then keep only matching templates in the sequence.';
  } else if (mixedTypes) {
    message = 'Mixed template types detected. A pipeline cannot contain both SF → SF and Files → SF stages. Remove the wrong stage(s) to continue.';
  } else if (hasMismatch) {
    message = `This is a ${pipelineTypeLabel(selectedType)} pipeline. Remove non-${pipelineTypeLabel(selectedType)} stage(s): ${mismatchedStages.map(stage => stage.pipelineName).join(', ')}.`;
  }
  return {
    selectedType,
    stageModes,
    mismatchedStages,
    mixedTypes,
    hasMismatch,
    blocked: Boolean(designerState.pipelineStages.length) && (!selectedType || mixedTypes || hasMismatch),
    message,
  };
}

function collectPipelineProfileIntegrity() {
  const stages = designerState.pipelineStages || [];
  if (!stages.length) {
    return { blocked: false, message: '', sourceProfiles: [], targetProfiles: [] };
  }
  const isFlat = getSelectedPipelineType() === 'flat_file'
    || stages.every(stage => normalizeInputMode(stage.inputMode) === 'flat_file');
  const sourceProfiles = [...new Set(stages.map(stageSourceProfile).filter(Boolean))];
  const targetProfiles = [...new Set(stages.map(stageTargetProfile).filter(Boolean))];
  const selectedSource = normalizeProfileId(getSelectedSourceProfile());
  const selectedTarget = normalizeProfileId(getSelectedTargetProfile());
  let message = '';
  if (!isFlat && sourceProfiles.length > 1) {
    message = `Mixed source profiles in this pipeline: ${sourceProfiles.join(', ')}. Use templates from the same source org profile.`;
  } else if (targetProfiles.length > 1) {
    message = `Mixed target profiles in this pipeline: ${targetProfiles.join(', ')}. Use templates from the same target org profile.`;
  } else if (!isFlat && sourceProfiles.length === 1 && sourceProfiles[0] !== selectedSource) {
    message = `Selected source profile "${selectedSource}" does not match stage profile "${sourceProfiles[0]}".`;
  } else if (targetProfiles.length === 1 && targetProfiles[0] !== selectedTarget) {
    message = `Selected target profile "${selectedTarget}" does not match stage profile "${targetProfiles[0]}".`;
  }
  return {
    blocked: Boolean(message),
    message,
    sourceProfiles,
    targetProfiles,
    isFlat,
  };
}

function assertHomogeneousPipelineProfiles() {
  const integrity = collectPipelineProfileIntegrity();
  if (integrity.blocked) {
    alert(integrity.message || 'Correct the org profiles before continuing.');
    return false;
  }
  return true;
}

function profilesCompatibleWithPipeline(candidate = {}) {
  const stages = designerState.pipelineStages || [];
  if (!stages.length) return { ok: true, message: '' };
  const isFlat = normalizeInputMode(candidate.inputMode) === 'flat_file';
  const candidateSource = isFlat ? '' : normalizeProfileId(candidate.sourceProfile);
  const candidateTarget = normalizeProfileId(candidate.targetProfile);
  for (const stage of stages) {
    const stageFlat = normalizeInputMode(stage.inputMode) === 'flat_file';
    if (!isFlat && !stageFlat && stageSourceProfile(stage) !== candidateSource) {
      return {
        ok: false,
        message: `Template source profile "${candidateSource}" does not match existing stage "${stage.pipelineName}" (${stageSourceProfile(stage)}).`,
      };
    }
    if (stageTargetProfile(stage) !== candidateTarget) {
      return {
        ok: false,
        message: `Template target profile "${candidateTarget}" does not match existing stage "${stage.pipelineName}" (${stageTargetProfile(stage)}).`,
      };
    }
  }
  return { ok: true, message: '' };
}

function syncDesignerProfilesFromStages() {
  const stages = designerState.pipelineStages || [];
  if (!stages.length) return;
  const first = stages[0];
  if (normalizeInputMode(first.inputMode) !== 'flat_file') {
    designerState.sourceProfile = stageSourceProfile(first) || designerState.sourceProfile || 'default';
  }
  designerState.targetProfile = stageTargetProfile(first) || designerState.targetProfile || 'default';
  renderOrgProfileOptions();
  _syncEmailMaskDefaults();
}

function collectPipelineValidationReadiness() {
  const blockedStages = designerState.pipelineStages.filter(stage => !isStageValidationReady(stage));
  return {
    ready: blockedStages.length === 0,
    blockedStages,
  };
}

function getPipelineBaseName() {
  return ($('designer-migration-name')?.value || '').trim();
}

function normalizePipelineNameKey(value) {
  return String(value || '').trim().toLowerCase();
}

function isPipelineNameUnique(value = getPipelineBaseName()) {
  const name = String(value || '').trim();
  if (!name) return false;
  const key = normalizePipelineNameKey(name);
  if (normalizePipelineNameKey(designerState.claimedPipelineName) === key) {
    return true;
  }
  return !(designerState.usedPipelineNames || []).some(
    used => normalizePipelineNameKey(used) === key,
  );
}

function collectPipelineNameReadiness(value = getPipelineBaseName()) {
  const name = String(value || '').trim();
  if (!name) {
    return {
      ready: false,
      message: 'Pipeline Name is required.',
    };
  }
  return { ready: true, message: '' };
}

function collectPipelineNameReuseWarning(value = getPipelineBaseName()) {
  const name = String(value || '').trim();
  if (!name || isPipelineNameUnique(name)) {
    return '';
  }
  return `Pipeline Name "${name}" was used before. Starting migration will create a new run.`;
}

function requirePipelineBaseName() {
  const readiness = collectPipelineNameReadiness();
  if (!readiness.ready) {
    alert(readiness.message || 'Pipeline Name is required.');
    $('designer-migration-name')?.focus();
    return '';
  }
  return getPipelineBaseName();
}

function claimPipelineName(value = getPipelineBaseName()) {
  const name = String(value || '').trim();
  if (!name) return;
  designerState.claimedPipelineName = name;
}

async function loadUsedPipelineNames() {
  try {
    const response = await fetch('/api/pipeline/used-names');
    const data = await response.json();
    designerState.usedPipelineNames = Array.isArray(data.names) ? data.names : [];
  } catch (_error) {
    designerState.usedPipelineNames = [];
  }
}

function requirePipelineType() {
  const type = getSelectedPipelineType();
  if (!type) {
    alert('Select Pipeline Type first: SF → SF or Files → SF.');
    $('designer-pipeline-type')?.focus();
    return '';
  }
  return type;
}

function assertHomogeneousPipelineStages() {
  const integrity = collectPipelineTypeIntegrity();
  if (integrity.blocked) {
    alert(integrity.message || 'Correct the pipeline template types before continuing.');
    return false;
  }
  if (!assertHomogeneousPipelineProfiles()) {
    return false;
  }
  return true;
}

const TRANSFORM_RULE_DEFS = [
  { type: 'trim', label: 'Trim' },
  { type: 'lowercase', label: 'Lowercase' },
  { type: 'uppercase', label: 'Uppercase' },
  { type: 'static_value', label: 'Static Value' },
  { type: 'null_default', label: 'Null Default' },
  { type: 'text_to_decimal', label: 'Text → Decimal' },
  { type: 'boolean_convert', label: 'Boolean 1/0 ↔ True/False' },
  { type: 'date_convert', label: 'Date → Salesforce Date' },
  { type: 'anonymize_email', label: 'Anonymize Email' },
  { type: 'anonymize_phone', label: 'Anonymize Phone' },
];

const EMAIL_MASK_MODE_DEFS = [
  { value: 'hash', label: 'Hash Mask' },
  { value: 'append_suffix', label: 'Append Suffix' },
  { value: 'replace_domain', label: 'Replace Domain' },
  { value: 'fixed_local', label: 'Fixed Local Part' },
  { value: 'fixed_full', label: 'Fixed Full Email' },
];

const $ = id => document.getElementById(id);

function initTopbarMenus() {
  const menus = Array.from(document.querySelectorAll('.topbar-menu'));
  if (!menus.length) {
    return;
  }

  menus.forEach(menu => {
    menu.addEventListener('toggle', () => {
      if (!menu.open) {
        return;
      }
      menus.forEach(otherMenu => {
        if (otherMenu !== menu) {
          otherMenu.open = false;
        }
      });
    });
  });

  document.addEventListener('click', event => {
    if (event.target.closest('.topbar-menu')) {
      return;
    }
    menus.forEach(menu => {
      menu.open = false;
    });
  });

  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') {
      return;
    }
    menus.forEach(menu => {
      menu.open = false;
    });
  });
}

document.addEventListener('DOMContentLoaded', async () => {
  initTopbarMenus();
  restoreDesignerState();
  bindDesignerEvents();
  renderPipelineFlow();
  await loadSourceProfiles();
  await loadUsedPipelineNames();
  await loadTemplateLibrary();
  await loadPipelineSnapshots();
  adoptSavedSnapshotForCurrentPipeline();
  persistDesignerState();
  await loadTransformationProfiles();
  renderPipelineFlow();
  _syncEmailMaskDefaults();
  await loadScheduledJobs();
  window.setInterval(() => {
    loadScheduledJobs();
  }, 15000);
});

function bindDesignerEvents() {
  $('designer-template-search').addEventListener('input', e => filterTemplateLibrary(e.target.value));
  $('designer-pipeline-type').addEventListener('change', () => setPipelineType($('designer-pipeline-type').value));
  $('designer-filter-source-domain')?.addEventListener('change', e => {
    designerState.domainFilters.sourceDomain = String(e.target.value || '');
    filterTemplateLibrary($('designer-template-search')?.value || '');
  });
  $('designer-filter-target-domain')?.addEventListener('change', e => {
    designerState.domainFilters.targetDomain = String(e.target.value || '');
    filterTemplateLibrary($('designer-template-search')?.value || '');
  });
  $('designer-target-profile')?.addEventListener('change', () => _syncEmailMaskDefaults());
  $('mask-emails-checkbox')?.addEventListener('change', event => {
    if (event.target.checked) {
      const removeCb = $('remove-email-mask-checkbox');
      if (removeCb) removeCb.checked = false;
    }
  });
  $('remove-email-mask-checkbox')?.addEventListener('change', event => {
    if (event.target.checked) {
      const maskCb = $('mask-emails-checkbox');
      if (maskCb) maskCb.checked = false;
    }
  });
  $('designer-run-validation-pipeline').addEventListener('click', () => startValidationPipelineRun());
  $('designer-skip-validation-pipeline').addEventListener('click', () => skipValidationPipeline());
  $('designer-start-pipeline').addEventListener('click', () => startPipelineRun());
  $('designer-schedule-pipeline').addEventListener('click', () => openScheduleModal());
  $('designer-schedule-close').addEventListener('click', closeScheduleModal);
  $('designer-schedule-submit').addEventListener('click', () => submitScheduledPipeline());
  $('designer-schedule-panel')?.addEventListener('click', event => {
    const button = event.target.closest('[data-cancel-schedule]');
    if (!button) {
      return;
    }
    cancelScheduledJob(button.getAttribute('data-cancel-schedule'));
  });
  $('designer-run-post-validation').addEventListener('click', () => openPostValidation());
  $('designer-open-verify').addEventListener('click', openVerifyModal);
  $('designer-open-verify-records').addEventListener('click', openVerifyRecordsModal);
  $('designer-open-report-refs').addEventListener('click', openReportRefsModal);
  $('designer-clear-pipeline').addEventListener('click', clearPipelineStages);
  $('designer-save-snapshot').addEventListener('click', savePipelineSnapshot);
  $('designer-load-snapshot').addEventListener('click', loadSelectedPipelineSnapshot);
  $('designer-delete-snapshot').addEventListener('click', deleteSelectedPipelineSnapshot);
  $('designer-preview-config').addEventListener('click', () => previewPipelineConfig());
  $('designer-save-config').addEventListener('click', () => savePipelineConfig());
  $('designer-open-current-tab').addEventListener('click', openCurrentPipelineInNewTab);
  $('designer-open-empty-tab').addEventListener('click', openNewPipelineWorkspace);
  $('designer-yaml-close').addEventListener('click', () => $('designer-yaml-modal').style.display = 'none');
  $('designer-transform-close').addEventListener('click', closeTransformManager);
  $('designer-verify-close').addEventListener('click', closeVerifyModal);
  $('designer-run-verify').addEventListener('click', runVerifySelfLookup);
  $('designer-verify-pipeline').addEventListener('change', renderVerifyObjectOptions);
  $('designer-verify-records-close').addEventListener('click', closeVerifyRecordsModal);
  $('designer-run-verify-records').addEventListener('click', runVerifyRecords);
  $('designer-verify-records-pipeline').addEventListener('change', renderVerifyRecordsObjectOptions);
  $('designer-report-refs-close').addEventListener('click', closeReportRefsModal);
  $('designer-run-report-refs').addEventListener('click', runReportRefs);
  $('designer-yaml-copy').addEventListener('click', () => navigator.clipboard.writeText($('designer-yaml-output').textContent));
  $('designer-migration-name').addEventListener('input', () => {
    const current = getPipelineBaseName();
    if (
      designerState.claimedPipelineName
      && normalizePipelineNameKey(designerState.claimedPipelineName) !== normalizePipelineNameKey(current)
    ) {
      designerState.claimedPipelineName = '';
    }
    persistDesignerState();
    renderPipelineFlow();
  });
  $('designer-source-profile').addEventListener('change', () => {
    designerState.sourceProfile = $('designer-source-profile').value || 'default';
    persistDesignerState();
    renderPipelineFlow();
  });
  $('designer-target-profile')?.addEventListener('change', () => {
    designerState.targetProfile = $('designer-target-profile').value || 'default';
    persistDesignerState();
    renderPipelineFlow();
  });
}

function getSelectedStartStage() {
  return $('designer-start-stage')?.value || 'extract';
}

function getSelectedSourceProfile() {
  return normalizeProfileId($('designer-source-profile')?.value || designerState.sourceProfile || 'default');
}

function getSelectedTargetProfile() {
  return normalizeProfileId($('designer-target-profile')?.value || designerState.targetProfile || 'default');
}

function setSelectedStartStage(value) {
  const normalized = ['extract', 'transform', 'load'].includes(value) ? value : 'extract';
  if ($('designer-start-stage')) {
    $('designer-start-stage').value = normalized;
  }
  persistDesignerState();
  renderPipelineFlow();
}

async function loadSourceProfiles() {
  try {
    const response = await fetch('/api/org-profiles');
    const data = await response.json();
    designerState.sourceProfiles = Array.isArray(data.source) ? data.source : [];
    designerState.targetProfiles = Array.isArray(data.target) ? data.target : [];
  } catch (_e) {
    designerState.sourceProfiles = [];
    designerState.targetProfiles = [];
  }
  renderOrgProfileOptions();
  _syncEmailMaskDefaults();
}

function renderOrgProfileOptions() {
  const sourceSelect = $('designer-source-profile');
  const targetSelect = $('designer-target-profile');
  const sourceProfiles = designerState.sourceProfiles.length
    ? designerState.sourceProfiles
    : [{ id: 'default', label: 'Default' }];
  const targetProfiles = designerState.targetProfiles.length
    ? designerState.targetProfiles
    : [{ id: 'default', label: 'Default' }];

  if (sourceSelect) {
    const selectedSource = sourceProfiles.some(profile => profile.id === designerState.sourceProfile)
      ? designerState.sourceProfile
      : sourceProfiles[0].id;
    designerState.sourceProfile = selectedSource;
    sourceSelect.innerHTML = sourceProfiles
      .map(profile => `<option value="${escHtml(profile.id)}" ${profile.id === selectedSource ? 'selected' : ''}>Source: ${escHtml(profile.label)}</option>`)
      .join('');
  }

  if (targetSelect) {
    const selectedTarget = targetProfiles.some(profile => profile.id === designerState.targetProfile)
      ? designerState.targetProfile
      : targetProfiles[0].id;
    designerState.targetProfile = selectedTarget;
    targetSelect.innerHTML = targetProfiles
      .map(profile => `<option value="${escHtml(profile.id)}" ${profile.id === selectedTarget ? 'selected' : ''}>Target: ${escHtml(profile.label)}</option>`)
      .join('');
  }
}

function renderSourceProfileOptions() {
  renderOrgProfileOptions();
}

function getDesignerSnapshotPayload() {
  return {
    migrationName: $('designer-migration-name')?.value || '',
    pipelineType: getSelectedPipelineType(),
    startStage: getSelectedStartStage(),
    sourceProfile: getSelectedSourceProfile(),
    targetProfile: getSelectedTargetProfile(),
    pipelineStages: designerState.pipelineStages,
    runStatus: {
      runner: {
        run_key: designerState.runStatus?.runner?.run_key || '',
        log_path: designerState.runStatus?.runner?.log_path || '',
        config_path: designerState.runStatus?.runner?.config_path || '',
        state_path: designerState.runStatus?.runner?.state_path || '',
        started_at: designerState.runStatus?.runner?.started_at || null,
        returncode: designerState.runStatus?.runner?.returncode ?? null,
      },
    },
    panels: designerState.panels,
    failedFiles: {
      pipelineId: designerState.failedFiles.pipelineId || '',
      selectedPaths: designerState.failedFiles.selectedPaths || [],
    },
    savedSnapshotId: designerState.savedSnapshotId || '',
    savedSnapshotFingerprint: designerState.savedSnapshotFingerprint || '',
  };
}

function stablePipelineSnapshotValue(value) {
  if (Array.isArray(value)) return value.map(stablePipelineSnapshotValue);
  if (!value || typeof value !== 'object') return value;
  const volatileKeys = new Set(['prevalidationStatus', 'collapsed']);
  return Object.keys(value)
    .filter(key => !volatileKeys.has(key))
    .sort()
    .reduce((result, key) => {
      result[key] = stablePipelineSnapshotValue(value[key]);
      return result;
    }, {});
}

function currentPipelineSnapshotFingerprint() {
  return JSON.stringify(stablePipelineSnapshotValue({
    migrationName: getPipelineBaseName(),
    pipelineType: getSelectedPipelineType(),
    sourceProfile: getSelectedSourceProfile(),
    targetProfile: getSelectedTargetProfile(),
    pipelineStages: (designerState.pipelineStages || []).map(stage => ({
      templateId: stage?.templateId || '',
      flatFileTemplateId: stage?.flatFileTemplateId || '',
      pipelineId: stage?.pipelineId || '',
      pipelineName: stage?.pipelineName || '',
      inputMode: stage?.inputMode || '',
      sourceProfile: stage?.sourceProfile || '',
      targetProfile: stage?.targetProfile || '',
      workspace: {
        nodeSelections: stage?.workspace?.nodeSelections || {},
        objectExternalIds: stage?.workspace?.objectExternalIds || {},
        lookupMappings: stage?.workspace?.lookupMappings || {},
        objectTransforms: stage?.workspace?.objectTransforms || {},
      },
    })),
  }));
}

function markCurrentPipelineSnapshotSaved(snapshotId = designerState.savedSnapshotId) {
  designerState.savedSnapshotId = snapshotId || '';
  designerState.savedSnapshotFingerprint = currentPipelineSnapshotFingerprint();
}

function isCurrentPipelineSnapshotSaved() {
  return !!designerState.savedSnapshotId
    && designerState.savedSnapshotFingerprint === currentPipelineSnapshotFingerprint();
}

function slugifyPipelineSnapshotName(name) {
  const normalized = String(name || '').trim().replace(/[^A-Za-z0-9_-]+/g, '_').replace(/_+/g, '_').replace(/^_|_$/g, '');
  return (normalized.slice(0, 80) || 'pipeline_snapshot');
}

function adoptSavedSnapshotForCurrentPipeline() {
  const name = getPipelineBaseName();
  if (!name) {
    return false;
  }
  const snapshotId = slugifyPipelineSnapshotName(name);
  const match = (designerState.savedSnapshots || []).find(entry => (
    entry?.id === snapshotId
    || normalizePipelineNameKey(entry?.name) === normalizePipelineNameKey(name)
    || normalizePipelineNameKey(entry?.migration_name) === normalizePipelineNameKey(name)
  ));
  if (!match) {
    return false;
  }
  markCurrentPipelineSnapshotSaved(match.id);
  claimPipelineName(name);
  if ($('designer-snapshot-select') && !$('designer-snapshot-select').value) {
    $('designer-snapshot-select').value = match.id;
  }
  return true;
}

function requireSavedPipelineSnapshot() {
  if (isCurrentPipelineSnapshotSaved()) return true;
  alert('Save the current pipeline snapshot before running pre-validation or migration.');
  return false;
}

async function loadPipelineSnapshots() {
  try {
    const response = await fetch('/api/pipeline-designer/snapshots');
    const data = await response.json();
    if (!response.ok || data.error) {
      throw new Error(data.error || 'Failed to load saved pipelines');
    }
    designerState.savedSnapshots = Array.isArray(data.snapshots) ? data.snapshots : [];
  } catch (_error) {
    designerState.savedSnapshots = [];
  }
  renderPipelineSnapshotOptions();
}

function renderPipelineSnapshotOptions() {
  const select = $('designer-snapshot-select');
  if (!select) {
    return;
  }
  const currentValue = select.value || '';
  const snapshots = designerState.savedSnapshots || [];
  select.innerHTML = [
    '<option value="">Saved Pipelines</option>',
    ...snapshots.map(snapshot => (
      `<option value="${escHtml(snapshot.id)}">${escHtml(snapshot.name)}${snapshot.stage_count != null ? ` (${snapshot.stage_count})` : ''}</option>`
    )),
  ].join('');
  if (snapshots.some(snapshot => snapshot.id === currentValue)) {
    select.value = currentValue;
  }
}

async function loadTemplateLibrary() {
  const wrap = $('designer-template-list');
  wrap.innerHTML = '<div class="pipeline-library-empty">Loading templates...</div>';
  try {
    const [seedResponse, flatResponse] = await Promise.all([
      fetch('/api/templates'),
      fetch('/api/flat-files/templates'),
    ]);
    const seedData = await seedResponse.json();
    const flatData = await flatResponse.json();
    if (!seedResponse.ok || seedData.error) {
      throw new Error(seedData.error || 'Failed to load Data Seed templates');
    }
    if (!flatResponse.ok || flatData.error) {
      throw new Error(flatData.error || 'Failed to load Flat File templates');
    }
    const seedTemplates = (seedData.templates || []).map(template => ({
      ...template,
      input_mode: normalizeInputMode(template.input_mode),
      source_domain: orgDomainLabel(template.source_domain),
      target_domain: orgDomainLabel(template.target_domain),
    }));
    const flatTemplates = (flatData.templates || []).map(template => ({
      ...template,
      template_id: template.id,
      id: `flat:${template.id}`,
      pipeline_id: template.id,
      pipeline_name: template.name,
      seed_object: (template.objects || [])[0] || '',
      input_mode: 'flat_file',
      source_domain: '',
      target_domain: orgDomainLabel(template.target_domain),
      target_profile: template.target_profile || 'default',
    }));
    designerState.savedTemplates = [...seedTemplates, ...flatTemplates];
    syncDomainFilterOptions();
    const params = new URLSearchParams(window.location.search);
    const addFlatId = params.get('add_flat');
    const addSeedId = params.get('add_seed');
    if (!getSelectedPipelineType() && designerState.pipelineStages.length) {
      const inferred = inferPipelineTypeFromStages();
      if (inferred) setPipelineType(inferred, { force: true, silent: true });
    } else if (addFlatId && !getSelectedPipelineType()) {
      setPipelineType('flat_file', { force: true, silent: true });
    } else if (addSeedId && !getSelectedPipelineType()) {
      setPipelineType('salesforce_source', { force: true, silent: true });
    } else {
      applyPipelineTypeUi();
    }
    filterTemplateLibrary($('designer-template-search').value || '');
    if (designerState.pipelineStages.length) {
      await syncPipelineStagesFromTemplates();
      renderDesignerPanels();
    }
    if (addFlatId) {
      if (!getSelectedPipelineType()) {
        setPipelineType('flat_file', { force: true, silent: true });
      }
      if (getSelectedPipelineType() === 'flat_file') {
        const libraryId = `flat:${addFlatId}`;
        if (!designerState.pipelineStages.some(stage => stage.templateId === libraryId)) {
          await addTemplateToPipeline(libraryId);
        } else {
          await syncPipelineStagesFromTemplates();
        }
      }
      _syncEmailMaskDefaults();
      const cleanUrl = new URL(window.location.href);
      cleanUrl.searchParams.delete('add_flat');
      window.history.replaceState({}, '', cleanUrl);
    }
    if (addSeedId) {
      if (!getSelectedPipelineType()) {
        setPipelineType('salesforce_source', { force: true, silent: true });
      }
      if (getSelectedPipelineType() === 'salesforce_source') {
        if (!designerState.pipelineStages.some(stage => stage.templateId === addSeedId)) {
          await addTemplateToPipeline(addSeedId);
        }
      }
      const cleanUrl = new URL(window.location.href);
      cleanUrl.searchParams.delete('add_seed');
      window.history.replaceState({}, '', cleanUrl);
    }
  } catch (e) {
    wrap.innerHTML = `<div class="pipeline-library-empty">${escHtml(e.message)}</div>`;
  }
}

function syncDomainFilterOptions() {
  const wrap = $('designer-domain-filters');
  const sourceSelect = $('designer-filter-source-domain');
  const targetSelect = $('designer-filter-target-domain');
  if (!wrap || !sourceSelect || !targetSelect) {
    return;
  }
  const pipelineType = getSelectedPipelineType();
  if (!pipelineType) {
    wrap.hidden = true;
    return;
  }
  wrap.hidden = false;
  sourceSelect.hidden = pipelineType === 'flat_file';
  sourceSelect.disabled = pipelineType === 'flat_file';

  const typeTemplates = designerState.savedTemplates.filter(
    template => normalizeInputMode(template.input_mode) === pipelineType,
  );
  const sourceDomains = [...new Set(typeTemplates.map(templateSourceDomain).filter(Boolean))].sort((a, b) => a.localeCompare(b));
  const targetDomains = [...new Set(typeTemplates.map(templateTargetDomain).filter(Boolean))].sort((a, b) => a.localeCompare(b));

  const previousSource = designerState.domainFilters.sourceDomain;
  const previousTarget = designerState.domainFilters.targetDomain;
  sourceSelect.innerHTML = '<option value="">All source orgs</option>'
    + sourceDomains.map(domain => `<option value="${escHtml(domain)}">${escHtml(domain)}</option>`).join('');
  targetSelect.innerHTML = '<option value="">All target orgs</option>'
    + targetDomains.map(domain => `<option value="${escHtml(domain)}">${escHtml(domain)}</option>`).join('');

  designerState.domainFilters.sourceDomain = pipelineType === 'flat_file'
    ? ''
    : (sourceDomains.includes(previousSource) ? previousSource : '');
  designerState.domainFilters.targetDomain = targetDomains.includes(previousTarget) ? previousTarget : '';
  sourceSelect.value = designerState.domainFilters.sourceDomain;
  targetSelect.value = designerState.domainFilters.targetDomain;
}

function setPipelineType(nextType, options = {}) {
  const type = normalizePipelineType(nextType);
  // Compare against stored state — the <select> may already hold the new value on change.
  const current = normalizePipelineType(designerState.pipelineType);
  if (type === current && !options.force) {
    applyPipelineTypeUi();
    syncDomainFilterOptions();
    filterTemplateLibrary($('designer-template-search')?.value || '');
    renderPipelineFlow();
    return true;
  }
  const conflictingStages = (designerState.pipelineStages || []).filter(
    stage => normalizeInputMode(stage.inputMode) && normalizeInputMode(stage.inputMode) !== type,
  );
  if (type && conflictingStages.length) {
    if (!options.force) {
      const confirmed = window.confirm(
        `This pipeline currently has ${conflictingStages.length} non-${pipelineTypeLabel(type)} stage(s). `
        + `Switching to ${pipelineTypeLabel(type)} will remove those stage(s). Continue?`,
      );
      if (!confirmed) {
        if ($('designer-pipeline-type')) $('designer-pipeline-type').value = current;
        return false;
      }
    }
    designerState.pipelineStages = designerState.pipelineStages.filter(
      stage => normalizeInputMode(stage.inputMode) === type,
    );
    designerState.suppressStoredRunStatus = true;
  }
  designerState.pipelineType = type;
  if ($('designer-pipeline-type')) {
    $('designer-pipeline-type').value = type;
  }
  applyPipelineTypeUi();
  syncDomainFilterOptions();
  filterTemplateLibrary($('designer-template-search')?.value || '');
  renderPipelineFlow();
  persistDesignerState();
  return true;
}

function filterTemplateLibrary(query) {
  const normalized = String(query || '').trim().toLowerCase();
  const pipelineType = getSelectedPipelineType();
  const sourceDomainFilter = designerState.domainFilters.sourceDomain;
  const targetDomainFilter = designerState.domainFilters.targetDomain;
  designerState.filteredTemplates = designerState.savedTemplates.filter(template => {
    const templateMode = normalizeInputMode(template.input_mode);
    if (!pipelineType) return false;
    if (templateMode !== pipelineType) return false;
    if (sourceDomainFilter && templateSourceDomain(template) !== sourceDomainFilter) return false;
    if (targetDomainFilter && templateTargetDomain(template) !== targetDomainFilter) return false;
    if (!normalized) return true;
    return [
      template.name,
      template.id,
      template.pipeline_id,
      template.seed_object,
      templateSourceDomain(template),
      templateTargetDomain(template),
    ]
      .filter(Boolean)
      .some(value => String(value).toLowerCase().includes(normalized));
  });
  renderTemplateLibrary();
}

function renderTemplateOrgTags(template) {
  const tags = [];
  const sourceDomain = templateSourceDomain(template);
  const targetDomain = templateTargetDomain(template);
  if (normalizeInputMode(template.input_mode) !== 'flat_file' && sourceDomain) {
    tags.push(`<span class="pipeline-library-org-tag" title="Source org">${escHtml(sourceDomain)}</span>`);
  }
  if (targetDomain) {
    tags.push(`<span class="pipeline-library-org-tag" title="Target org">${escHtml(targetDomain)}</span>`);
  }
  if (!tags.length) {
    tags.push('<span class="pipeline-library-org-tag" title="Org domain not stored on this template">Org unknown</span>');
  }
  return `<div class="pipeline-library-org-tags">${tags.join('')}</div>`;
}

function renderTemplateLibrary() {
  const wrap = $('designer-template-list');
  if (!getSelectedPipelineType()) {
    wrap.innerHTML = '<div class="pipeline-library-empty">Select SF → SF or Files → SF first to load matching templates.</div>';
    return;
  }
  if (!designerState.filteredTemplates.length) {
    wrap.innerHTML = '<div class="pipeline-library-empty">No templates match this pipeline type, org filter, or search.</div>';
    return;
  }
  const integrity = collectPipelineTypeIntegrity();
  wrap.innerHTML = designerState.filteredTemplates.map(template => {
    const added = designerState.pipelineStages.some(stage => stage.templateId === template.id);
    const addDisabled = added || integrity.blocked;
    return `
      <div class="pipeline-library-item">
        <div class="pipeline-library-item-name">${escHtml(template.name || template.id)} <span class="pipeline-stage-status ${template.input_mode === 'flat_file' ? 'status-running' : 'status-pending'}">${modeBadgeLabel(template.input_mode)}</span></div>
        <div class="pipeline-library-item-meta">Pipeline: ${escHtml(template.pipeline_id || template.id)}</div>
        <div class="pipeline-library-item-meta">${template.input_mode === 'flat_file' ? 'First object' : 'Seed'}: ${escHtml(template.seed_object || 'Unknown')} | Objects: ${escHtml(template.object_count || 0)}</div>
        ${renderTemplateOrgTags(template)}
        <div class="pipeline-library-item-actions">
          <button class="btn btn-secondary btn-sm" onclick="${template.input_mode === 'flat_file' ? `window.location.href='/flat-files'` : `openTemplateInBuilder('${escJs(template.id)}')`}">${template.input_mode === 'flat_file' ? 'Open Mapping' : 'Open Builder'}</button>
          <button class="btn btn-primary btn-sm" onclick="addTemplateToPipeline('${escJs(template.id)}')" ${addDisabled ? 'disabled' : ''} title="${integrity.blocked ? escHtml(integrity.message) : ''}">Add Stage</button>
        </div>
      </div>
    `;
  }).join('');
}

async function addTemplateToPipeline(templateId) {
  if (!templateId) {
    return;
  }
  const pipelineType = requirePipelineType();
  if (!pipelineType) {
    return;
  }
  const integrity = collectPipelineTypeIntegrity();
  if (integrity.blocked) {
    alert(integrity.message || 'Remove mismatched stages before adding another template.');
    return;
  }
  const meta = designerState.savedTemplates.find(template => template.id === templateId) || {};
  const templateMode = normalizeInputMode(meta.input_mode || (String(templateId).startsWith('flat:') ? 'flat_file' : 'salesforce_source'));
  if (templateMode !== pipelineType) {
    alert(
      `This pipeline is ${pipelineTypeLabel(pipelineType)}. `
      + `Only ${pipelineTypeLabel(pipelineType)} templates can be added to this sequence.`,
    );
    return;
  }
  if (designerState.pipelineStages.some(stage => stage.templateId === templateId)) {
    return;
  }
  const isFlatFile = templateMode === 'flat_file';
  const actualTemplateId = isFlatFile ? (meta.template_id || templateId.replace(/^flat:/, '')) : templateId;
  const response = await fetch(isFlatFile
    ? `/api/flat-files/templates/${encodeURIComponent(actualTemplateId)}`
    : `/api/templates/${encodeURIComponent(templateId)}`);
  const payload = await response.json();
  if (!response.ok || payload.error) {
    alert(payload.error || 'Failed to load template');
    return;
  }
  const stage = ensureUniquePipelineId(
    isFlatFile ? buildFlatFilePipelineStage(payload, meta) : buildPipelineStage(payload, meta),
    designerState.pipelineStages,
  );
  const profileCheck = profilesCompatibleWithPipeline(stage);
  if (!profileCheck.ok) {
    alert(profileCheck.message || 'Template org profiles are incompatible with this pipeline.');
    return;
  }
  designerState.pipelineStages.push(stage);
  syncDesignerProfilesFromStages();
  designerState.suppressStoredRunStatus = true;
  persistDesignerState();
  renderTemplateLibrary();
  renderPipelineFlow();
  renderDesignerPanels();
  _syncEmailMaskDefaults();
}

async function savePipelineSnapshot() {
  if (!designerState.pipelineStages.length) {
    alert('Add at least one stage before saving a pipeline snapshot.');
    return;
  }
  const suggestedName = $('designer-migration-name')?.value || 'Pipeline Snapshot';
  const name = window.prompt('Save this pipeline as:', suggestedName);
  if (!name || !name.trim()) {
    return;
  }
  if ($('designer-migration-name')) {
    $('designer-migration-name').value = name.trim();
  }
  const snapshotPayload = getDesignerSnapshotPayload();
  const response = await fetch('/api/pipeline-designer/snapshots', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      name: name.trim(),
      snapshot: snapshotPayload,
    }),
  });
  const data = await response.json();
  if (!response.ok || data.error) {
    alert(data.error || 'Failed to save pipeline snapshot.');
    return;
  }
  await loadPipelineSnapshots();
  if (data.snapshot?.id && $('designer-snapshot-select')) {
    $('designer-snapshot-select').value = data.snapshot.id;
  }
  designerState.savedSnapshotId = data.snapshot?.id || '';
  markCurrentPipelineSnapshotSaved(designerState.savedSnapshotId);
  persistDesignerState();
  renderPipelineFlow();
  alert(`Saved pipeline snapshot "${data.snapshot?.name || name.trim()}".`);
}

async function loadSelectedPipelineSnapshot() {
  const snapshotId = $('designer-snapshot-select')?.value || '';
  if (!snapshotId) {
    alert('Choose a saved pipeline first.');
    return;
  }
  const response = await fetch(`/api/pipeline-designer/snapshots/${encodeURIComponent(snapshotId)}`);
  const data = await response.json();
  if (!response.ok || data.error) {
    alert(data.error || 'Failed to load pipeline snapshot.');
    return;
  }
  designerState.pipelineStages = Array.isArray(data.pipelineStages) ? data.pipelineStages : [];
  designerState.sourceProfile = data.sourceProfile || 'default';
  designerState.targetProfile = data.targetProfile || 'default';
  designerState.panels = {
    failedCollapsed: !!data.panels?.failedCollapsed,
    logsCollapsed: !!data.panels?.logsCollapsed,
  };
  designerState.failedFiles.pipelineId = data.failedFiles?.pipelineId || '';
  designerState.failedFiles.selectedPaths = Array.isArray(data.failedFiles?.selectedPaths)
    ? data.failedFiles.selectedPaths
    : [];
  designerState.suppressStoredRunStatus = true;
  if ($('designer-migration-name')) {
    $('designer-migration-name').value = data.migrationName || data.name || snapshotId;
  }
  designerState.claimedPipelineName = '';
  const snapshotType = normalizePipelineType(data.pipelineType) || inferPipelineTypeFromStages(designerState.pipelineStages);
  setPipelineType(snapshotType, { force: true, silent: true });
  if ($('designer-start-stage')) {
    $('designer-start-stage').value = data.startStage || 'extract';
  }
  renderOrgProfileOptions();
  persistDesignerState();
  renderTemplateLibrary();
  renderPipelineFlow();
  renderDesignerPanels();
  await syncPipelineStagesFromTemplates();
  syncDesignerProfilesFromStages();
  designerState.savedSnapshotId = snapshotId;
  markCurrentPipelineSnapshotSaved(snapshotId);
  persistDesignerState();
  renderPipelineFlow();
  alert(`Loaded pipeline snapshot "${data.name || snapshotId}".`);
}

function openCurrentPipelineInNewTab() {
  const workspaceId = getDesignerWorkspaceId() || buildPipelineWorkspaceId();
  persistDesignerState();
  window.open(buildPipelineWorkspaceUrl(workspaceId), '_blank', 'noopener');
}

function openNewPipelineWorkspace() {
  const workspaceId = `pipeline_${Date.now()}`;
  window.open(buildPipelineWorkspaceUrl(workspaceId), '_blank', 'noopener');
}

async function deleteSelectedPipelineSnapshot() {
  const snapshotId = $('designer-snapshot-select')?.value || '';
  if (!snapshotId) {
    alert('Choose a saved pipeline first.');
    return;
  }
  const snapshot = (designerState.savedSnapshots || []).find(entry => entry.id === snapshotId);
  if (!window.confirm(`Delete saved pipeline "${snapshot?.name || snapshotId}"?`)) {
    return;
  }
  const response = await fetch(`/api/pipeline-designer/snapshots/${encodeURIComponent(snapshotId)}`, {
    method: 'DELETE',
  });
  const data = await response.json();
  if (!response.ok || data.error) {
    alert(data.error || 'Failed to delete pipeline snapshot.');
    return;
  }
  await loadPipelineSnapshots();
}

async function syncPipelineStagesFromTemplates() {
  if (!designerState.pipelineStages.length) {
    return;
  }
  const refreshedStages = await Promise.all(designerState.pipelineStages.map(async stage => {
    if (!stage?.templateId) {
      return stage;
    }
    try {
      const isFlatFile = stage.inputMode === 'flat_file' || String(stage.templateId).startsWith('flat:');
      const actualTemplateId = stage.flatFileTemplateId || String(stage.templateId).replace(/^flat:/, '');
      const response = await fetch(isFlatFile
        ? `/api/flat-files/templates/${encodeURIComponent(actualTemplateId)}`
        : `/api/templates/${encodeURIComponent(stage.templateId)}`);
      const payload = await response.json();
      if (!response.ok || payload.error) {
        return stage;
      }
      const meta = designerState.savedTemplates.find(template => template.id === stage.templateId) || {};
      const rebuilt = isFlatFile
        ? buildFlatFilePipelineStage(payload, meta)
        : buildPipelineStage(payload, meta);
      return {
        ...rebuilt,
        pipelineId: stage.pipelineId || rebuilt.pipelineId,
        pipelineName: stage.pipelineName || rebuilt.pipelineName,
        collapsed: stage.collapsed ?? true,
        workspace: mergeStageWorkspace(rebuilt.workspace, stage.workspace),
      };
    } catch (_error) {
      return stage;
    }
  }));
  const normalizedStages = [];
  refreshedStages.forEach(stage => {
    normalizedStages.push(ensureUniquePipelineId(stage, normalizedStages));
  });
  designerState.pipelineStages = normalizedStages;
  syncDesignerProfilesFromStages();
  _syncEmailMaskDefaults();
  if (designerState.savedSnapshotId) {
    markCurrentPipelineSnapshotSaved(designerState.savedSnapshotId);
  } else {
    adoptSavedSnapshotForCurrentPipeline();
  }
  persistDesignerState();
  renderPipelineFlow();
}

function buildFlatFilePipelineStage(templatePayload, templateMeta = {}) {
  const files = templatePayload.files || [];
  const objectFields = new Map();
  const objectExternalIdConfig = new Map();
  const lookupMappings = {};
  files.forEach(file => {
    if (!file.object) return;
    if (!objectFields.has(file.object)) objectFields.set(file.object, new Set());
    if (file.external_id) objectExternalIdConfig.set(file.object, file.external_id);
    (file.mappings || []).forEach(mapping => {
      if (!mapping.ignored && mapping.target) objectFields.get(file.object).add(mapping.target);
      if (!mapping.ignored && mapping.target && ['target_id', 'polymorphic_external_id', 'polymorphic_target_first'].includes(mapping.lookup_resolution)) {
        lookupMappings[file.object] ||= {};
        lookupMappings[file.object][mapping.target] = {
          lookup_resolution: mapping.lookup_resolution,
          ...(mapping.polymorphic_mappings?.length ? { polymorphic_mappings: mapping.polymorphic_mappings.map(rule => ({ ...rule })) } : {}),
        };
      } else if (!mapping.ignored && mapping.target && mapping.referenced_object) {
        lookupMappings[file.object] ||= {};
        lookupMappings[file.object][mapping.target] = {
          referenced_object: mapping.referenced_object,
          lookup_resolution: mapping.lookup_resolution || 'pipeline_object',
          ...(mapping.lookup_external_id ? { external_id: mapping.lookup_external_id } : {}),
        };
      }
    });
  });
  const objectNames = [...objectFields.keys()];
  const nodes = objectNames.map((objectName, index) => ({
    id: `flat_${index}_${objectName}`,
    object: objectName,
    parentId: null,
    direction: index === 0 ? 'seed' : 'flat_file',
    via_field: '',
    depth: index,
  }));
  const nodeSelections = {};
  const objectExternalIds = {};
  nodes.forEach(node => {
    const fields = [...(objectFields.get(node.object) || [])];
    nodeSelections[node.id] = { selectedFields: fields };
    objectExternalIds[node.object] = objectExternalIdConfig.get(node.object)
      || (fields.includes('Id') ? 'External_Id__c' : '')
      || (fields.includes('External_Id__c') ? 'External_Id__c' : '')
      || fields.find(field => /external|legacy.*id|_id__c$/i.test(field))
      || 'Id';
  });
  const actualId = templatePayload.id || templateMeta.template_id || String(templateMeta.id || '').replace(/^flat:/, '');
  const libraryId = `flat:${actualId}`;
  const profiles = resolveTemplateProfiles(templatePayload, templateMeta, { flatFile: true });
  const workspace = {
    rootId: nodes[0]?.id || '',
    nodes,
    nodeSelections,
    objectExternalIds,
    lookupMappings,
    objectTransforms: {},
    controls: {
      pipelineId: actualId,
      migrationName: templatePayload.name || templateMeta.name || actualId,
    },
  };
  return {
    templateId: libraryId,
    flatFileTemplateId: actualId,
    templateName: templatePayload.name || templateMeta.name || actualId,
    pipelineId: actualId,
    pipelineName: templatePayload.name || templateMeta.name || actualId,
    seedObject: objectNames[0] || '',
    objectCount: objectNames.length,
    nodeCount: nodes.length,
    inputMode: 'flat_file',
    sourceDomain: '',
    targetDomain: orgDomainLabel(templatePayload.target_domain || templateMeta.target_domain),
    sourceProfile: profiles.sourceProfile,
    targetProfile: profiles.targetProfile,
    prevalidationStatus: normalizePrevalidationStatus(templatePayload.prevalidation_status || templateMeta.prevalidation_status),
    collapsed: true,
    workspace,
  };
}

function buildPipelineStage(templatePayload, templateMeta = {}) {
  const templateKind = String(templatePayload?.template_kind || templateMeta.template_kind || '').trim().toLowerCase();
  if (templateKind === 'source_mapping' || templateKind === 'source_cycle_group') {
    return buildSourceMappingPipelineStage(templatePayload, templateMeta);
  }
  const profiles = resolveTemplateProfiles(templatePayload, templateMeta);
  const workspace = templatePayload?.template || {};
  const controls = workspace.controls || {};
  const nodes = workspace.nodes || [];
  const rootNode = nodes.find(node => node.id === workspace.rootId) || nodes.find(node => !node.parentId) || null;
  const uniqueObjects = [...new Set(nodes.map(node => node.object).filter(Boolean))];
  const templateId = templatePayload.template_id || templateMeta.id || '';
  const templateName = templatePayload.template_name || templateMeta.name || templateId;
  const pipelineId = (controls.pipelineId || templateMeta.pipeline_id || templateId).trim() || templateId;
  const pipelineName = (controls.migrationName || templateMeta.pipeline_name || templateName).trim() || templateName;
  return {
    templateId,
    templateName,
    pipelineId,
    pipelineName,
    seedObject: rootNode?.object || '',
    objectCount: uniqueObjects.length,
    nodeCount: nodes.length,
    collapsed: true,
    inputMode: normalizeInputMode(templatePayload?.input_mode || templateMeta.input_mode),
    sourceDomain: orgDomainLabel(templatePayload.source_domain || templateMeta.source_domain),
    targetDomain: orgDomainLabel(templatePayload.target_domain || templateMeta.target_domain),
    sourceProfile: profiles.sourceProfile,
    targetProfile: profiles.targetProfile,
    prevalidationStatus: normalizePrevalidationStatus(templatePayload?.prevalidation_status || templateMeta.prevalidation_status),
    workspace,
  };
}

function getSourceTemplateMappings(templatePayload = {}) {
  const raw = templatePayload?.template || {};
  if (Array.isArray(raw.objects) && raw.objects.length) {
    return raw.objects.filter(item => item && typeof item === 'object');
  }
  if (raw.source_object || raw.target_object) {
    return [raw];
  }
  return [];
}

function buildRenameRulesForSourceMapping(mapping = {}) {
  const renameRules = [];
  const seenRenameRules = new Set();
  for (const fieldMapping of (mapping.field_mappings || [])) {
    const sourceField = String(fieldMapping?.source_field || '').trim();
    const targetField = String(fieldMapping?.target_field || '').trim();
    if (!sourceField || !targetField || sourceField === targetField) continue;
    const ruleKey = `${sourceField}=>${targetField}`;
    if (seenRenameRules.has(ruleKey)) continue;
    seenRenameRules.add(ruleKey);
    renameRules.push({ type: 'field_rename', source: sourceField, target: targetField });
  }
  const matchSource = String(mapping.match_mapping?.source_field || '').trim();
  const matchTarget = String(mapping.match_mapping?.target_field || '').trim();
  if (matchSource && matchTarget && matchSource !== matchTarget) {
    const ruleKey = `${matchSource}=>${matchTarget}`;
    if (!seenRenameRules.has(ruleKey)) {
      renameRules.push({ type: 'field_rename', source: matchSource, target: matchTarget });
    }
  }
  return renameRules;
}

function resolveTemplateProfiles(templatePayload = {}, templateMeta = {}, { flatFile = false } = {}) {
  return {
    sourceProfile: flatFile
      ? ''
      : normalizeProfileId(
        templatePayload.source_profile
        || templateMeta.source_profile
        || templatePayload.org_profile
        || templateMeta.org_profile
        || 'default',
      ),
    targetProfile: normalizeProfileId(
      templatePayload.target_profile
      || templateMeta.target_profile
      || 'default',
    ),
  };
}

function buildSourceMappingPipelineStage(templatePayload, templateMeta = {}) {
  const profiles = resolveTemplateProfiles(templatePayload, templateMeta);
  const mappings = getSourceTemplateMappings(templatePayload);
  if (!mappings.length) {
    return {
      templateId: templatePayload.template_id || templateMeta.id || '',
      templateName: templatePayload.template_name || templateMeta.name || '',
      pipelineId: templateMeta.pipeline_id || templatePayload.template_id || templateMeta.id || '',
      pipelineName: templateMeta.pipeline_name || templatePayload.template_name || templateMeta.name || '',
      seedObject: '',
      objectCount: 0,
      nodeCount: 0,
      collapsed: true,
      inputMode: normalizeInputMode(templatePayload?.input_mode || templateMeta.input_mode),
      sourceDomain: orgDomainLabel(templatePayload.source_domain || templateMeta.source_domain),
      targetDomain: orgDomainLabel(templatePayload.target_domain || templateMeta.target_domain),
      sourceProfile: profiles.sourceProfile,
      targetProfile: profiles.targetProfile,
      prevalidationStatus: normalizePrevalidationStatus(templatePayload.prevalidation_status || templateMeta.prevalidation_status),
      workspace: { rootId: '', nodes: [], nodeSelections: {}, objectExternalIds: {}, lookupMappings: {}, objectTransforms: {}, sourceObjects: [], controls: {} },
    };
  }

  const nodes = [];
  const nodeSelections = {};
  const objectExternalIds = {};
  const objectTransforms = {};
  const sourceObjects = [];
  mappings.forEach((mapping, index) => {
    const sourceObject = String(mapping.source_object || '').trim();
    const targetObject = String(mapping.target_object || '').trim();
    const objectName = sourceObject || targetObject;
    if (!objectName) return;
    const nodeId = `source_mapping_${objectName}_${index}`;
    const selectedFields = [...new Set(
      [
        'Id',
        ...(mapping.field_mappings || []).map(item => item?.source_field).filter(Boolean),
        mapping.match_mapping?.source_field || '',
      ].filter(Boolean),
    )].sort((a, b) => a.localeCompare(b));
    const renameRules = buildRenameRulesForSourceMapping(mapping);
    const matchTarget = String(mapping.match_mapping?.target_field || '').trim();
    nodes.push({
      id: nodeId,
      object: objectName,
      parentId: null,
      direction: index === 0 ? 'seed' : 'child',
      via_field: '',
      depth: index === 0 ? 0 : 1,
    });
    nodeSelections[nodeId] = {
      selectedFields,
      queryFilter: mapping.query_filter || '',
    };
    objectExternalIds[objectName] = matchTarget || mapping.target_external_id || 'Id';
    objectTransforms[objectName] = renameRules;
    sourceObjects.push({
      source_object: sourceObject,
      target_object: targetObject,
      query_filter: String(mapping.query_filter || ''),
      allowed_operations: Array.isArray(mapping.allowed_operations) ? [...mapping.allowed_operations] : [],
      default_operation: String(mapping.default_operation || 'upsert'),
      target_external_id: String(mapping.target_external_id || ''),
      match_mapping: {
        source_field: String(mapping.match_mapping?.source_field || '').trim(),
        target_field: matchTarget,
      },
      field_mappings: Array.isArray(mapping.field_mappings) ? mapping.field_mappings.map(item => ({ ...item })) : [],
      transform_rules: renameRules.map(rule => ({ ...rule })),
      lookup_mappings: mapping.lookup_mappings && typeof mapping.lookup_mappings === 'object'
        ? { ...mapping.lookup_mappings }
        : {},
    });
  });

  // Refresh parent external IDs from package peers (same pattern as Flat File).
  sourceObjects.forEach(item => {
    const nextLookups = window.SourceLookupPolicy.compileExplicitLookups(item, sourceObjects);
    // Infer missing lookups from *Id fields to package peers.
    sourceObjects.forEach(parent => {
      const parentName = String(parent.source_object || parent.target_object || '').trim();
      if (!parentName) return;
      const parentExt = String(parent.match_mapping?.target_field || parent.target_external_id || '').trim();
      if (!parentExt) return;
      (item.field_mappings || []).forEach(fieldMapping => {
        const fieldName = String(fieldMapping?.source_field || '').trim();
        if (!fieldName || nextLookups[fieldName]) return;
        if (fieldName === 'ParentId' && parentName === String(item.source_object || item.target_object || '').trim()) {
          nextLookups[fieldName] = {
            referenced_object: parentName,
            lookup_resolution: 'pipeline_object',
            referenced_source_key: String(parent.match_mapping?.source_field || 'Id').trim() || 'Id',
          };
          return;
        }
        if (fieldName.endsWith('Id') && fieldName !== 'Id' && fieldName.slice(0, -2) === parentName) {
          nextLookups[fieldName] = {
            referenced_object: parentName,
            lookup_resolution: 'pipeline_object',
            referenced_source_key: String(parent.match_mapping?.source_field || 'Id').trim() || 'Id',
          };
        }
      });
    });
    item.lookup_mappings = nextLookups;
  });

  const first = sourceObjects[0] || {};
  const workspace = {
    rootId: nodes[0]?.id || '',
    nodes,
    nodeSelections,
    objectExternalIds,
    lookupMappings: {},
    objectTransforms,
    sourceObjects,
    // Keep legacy single-object shape for one-object packages.
    sourceTemplate: sourceObjects.length === 1 ? first : null,
    controls: {
      pipelineId: (templateMeta.pipeline_id || templatePayload.template_id || templateMeta.id || '').trim(),
      migrationName: (templateMeta.pipeline_name || templatePayload.template_name || templateMeta.name || templateMeta.id || '').trim(),
    },
  };
  const templateId = templatePayload.template_id || templateMeta.id || '';
  const templateName = templatePayload.template_name || templateMeta.name || templateId;
  const pipelineId = (workspace.controls.pipelineId || templateId).trim() || templateId;
  const pipelineName = (workspace.controls.migrationName || templateName).trim() || templateName;
  return {
    templateId,
    templateName,
    pipelineId,
    pipelineName,
    seedObject: first.source_object || first.target_object || '',
    objectCount: sourceObjects.length,
    nodeCount: nodes.length,
    collapsed: true,
    inputMode: normalizeInputMode(templatePayload?.input_mode || templateMeta.input_mode),
    sourceDomain: orgDomainLabel(templatePayload.source_domain || templateMeta.source_domain),
    targetDomain: orgDomainLabel(templatePayload.target_domain || templateMeta.target_domain),
    sourceProfile: profiles.sourceProfile,
    targetProfile: profiles.targetProfile,
    prevalidationStatus: normalizePrevalidationStatus(templatePayload.prevalidation_status || templateMeta.prevalidation_status),
    workspace,
  };
}

function slugifyPipelineId(value) {
  const normalized = String(value || '')
    .trim()
    .replace(/[^A-Za-z0-9_-]+/g, '_')
    .replace(/_+/g, '_')
    .replace(/^_+|_+$/g, '');
  return normalized || 'pipeline';
}

function ensureUniquePipelineId(stage, existingStages = []) {
  const baseId = slugifyPipelineId(stage?.pipelineId || stage?.templateId || stage?.templateName || 'pipeline');
  const existingIds = new Set(
    existingStages
      .map(existing => String(existing?.pipelineId || '').trim())
      .filter(Boolean)
  );
  if (!existingIds.has(baseId)) {
    return { ...stage, pipelineId: baseId };
  }

  const seedSuffix = slugifyPipelineId(stage?.seedObject || '');
  if (seedSuffix) {
    const seededId = `${baseId}_${seedSuffix}`;
    if (!existingIds.has(seededId)) {
      return { ...stage, pipelineId: seededId };
    }
  }

  for (let idx = 2; idx < 1000; idx += 1) {
    const candidate = `${baseId}_${idx}`;
    if (!existingIds.has(candidate)) {
      return { ...stage, pipelineId: candidate };
    }
  }

  return { ...stage, pipelineId: `${baseId}_${Date.now()}` };
}

function mergeStageWorkspace(nextWorkspace = {}, existingWorkspace = {}) {
  const merged = { ...nextWorkspace };
  merged.objectTransforms = { ...(nextWorkspace.objectTransforms || {}) };
  const existingTransforms = existingWorkspace?.objectTransforms || {};
  Object.entries(existingTransforms).forEach(([objectName, rules]) => {
    if (!Array.isArray(rules)) {
      return;
    }
    merged.objectTransforms[objectName] = rules.map(rule => ({ ...rule }));
  });
  return merged;
}

function getStageObjectNames(stage) {
  const nodes = stage?.workspace?.nodes || [];
  return [...new Set(nodes.map(node => node.object).filter(Boolean))].sort((a, b) => a.localeCompare(b));
}

function getObjectFieldsForStage(stage, objectName) {
  const workspace = stage?.workspace || {};
  const nodes = workspace.nodes || [];
  const selectedFields = new Set();
  nodes
    .filter(node => node.object === objectName)
    .forEach(node => {
      const selection = workspace.nodeSelections?.[node.id] || {};
      (selection.selectedFields || []).forEach(field => selectedFields.add(field));
    });
  return [...selectedFields].sort((a, b) => a.localeCompare(b));
}

function getTransformRulesForObject(stage, objectName) {
  const rules = stage?.workspace?.objectTransforms?.[objectName];
  return Array.isArray(rules) ? rules : [];
}

async function loadTransformationProfiles() {
  try {
    const response = await fetch('/api/transform-profiles');
    const data = await response.json();
    if (!response.ok || data.error) {
      throw new Error(data.error || 'Failed to load transformation profiles.');
    }
    designerState.transformationProfiles = Array.isArray(data.profiles) ? data.profiles : [];
    if (designerState.activeTransformStageIndex != null) {
      renderTransformManager();
    }
  } catch (error) {
    console.error(error);
    designerState.transformationProfiles = [];
  }
}

function transformationProfileOptions(objectName) {
  const profiles = designerState.transformationProfiles
    .filter(profile => profile.object === objectName);
  return '<option value="">Reusable profile</option>' + profiles
    .map(profile => `<option value="${escHtml(profile.id)}">${escHtml(profile.name)} (${(profile.rules || []).length})</option>`)
    .join('');
}

function renderTransformRuleFields(rule, objectFields, stageIndex, objectName, ruleIndex) {
  const fieldOptions = objectFields.length
    ? objectFields.map(field => `<option value="${escHtml(field)}" ${field === (rule.field || '') ? 'selected' : ''}>${escHtml(field)}</option>`).join('')
    : '<option value="">No selected fields</option>';
  const typeOptions = TRANSFORM_RULE_DEFS
    .map(def => `<option value="${escHtml(def.type)}" ${def.type === (rule.type || '') ? 'selected' : ''}>${escHtml(def.label)}</option>`)
    .join('');

  let extra = '';
  if (rule.type === 'anonymize_email') {
    const emailModeOptions = EMAIL_MASK_MODE_DEFS
      .map(def => `<option value="${escHtml(def.value)}" ${def.value === (rule.mode || 'hash') ? 'selected' : ''}>${escHtml(def.label)}</option>`)
      .join('');
    const mode = rule.mode || 'hash';
    let modeSpecific = '';
    if (mode === 'hash' || mode === 'replace_domain' || mode === 'fixed_full') {
      modeSpecific += `<input class="fh-input designer-transform-input" type="text" placeholder="Domain" value="${escHtml(rule.domain || (mode === 'hash' ? 'masked.example' : 'mask.com'))}" onchange="updateObjectTransformRule(${stageIndex}, '${escJs(objectName)}', ${ruleIndex}, 'domain', this.value)" />`;
    }
    if (mode === 'append_suffix') {
      modeSpecific += `<input class="fh-input designer-transform-input" type="text" placeholder="Suffix" value="${escHtml(rule.suffix || 'mask')}" onchange="updateObjectTransformRule(${stageIndex}, '${escJs(objectName)}', ${ruleIndex}, 'suffix', this.value)" />`;
    }
    if (mode === 'fixed_local' || mode === 'fixed_full') {
      modeSpecific += `<input class="fh-input designer-transform-input" type="text" placeholder="Local Part" value="${escHtml(rule.local_part || 'maskedemail')}" onchange="updateObjectTransformRule(${stageIndex}, '${escJs(objectName)}', ${ruleIndex}, 'local_part', this.value)" />`;
    }
    extra = `<select class="org-select template-select designer-transform-select" onchange="changeEmailMaskMode(${stageIndex}, '${escJs(objectName)}', ${ruleIndex}, this.value)">${emailModeOptions}</select>${modeSpecific}`;
  } else if (rule.type === 'static_value') {
    extra = `<input class="fh-input designer-transform-input" type="text" placeholder="Value" value="${escHtml(rule.value || '')}" oninput="updateObjectTransformRule(${stageIndex}, '${escJs(objectName)}', ${ruleIndex}, 'value', this.value, false)" />`;
  } else if (rule.type === 'null_default') {
    extra = `<input class="fh-input designer-transform-input" type="text" placeholder="Default" value="${escHtml(rule.default ?? '0')}" oninput="updateObjectTransformRule(${stageIndex}, '${escJs(objectName)}', ${ruleIndex}, 'default', this.value, false)" />`;
  } else if (rule.type === 'text_to_decimal') {
    extra = `
      <select class="org-select template-select designer-transform-select" onchange="updateObjectTransformRule(${stageIndex}, '${escJs(objectName)}', ${ruleIndex}, 'invalid_value', this.value)">
        <option value="null" ${(rule.invalid_value || 'null') === 'null' ? 'selected' : ''}>Invalid/blank → Null</option>
        <option value="zero" ${rule.invalid_value === 'zero' ? 'selected' : ''}>Invalid/blank → 0</option>
      </select>
      <input class="fh-input designer-transform-input" type="number" min="0" max="18" placeholder="Decimal places" value="${escHtml(rule.decimal_places ?? '2')}" oninput="updateObjectTransformRule(${stageIndex}, '${escJs(objectName)}', ${ruleIndex}, 'decimal_places', this.value, false)" />
    `;
  } else if (rule.type === 'boolean_convert') {
    extra = `
      <select class="org-select template-select designer-transform-select" onchange="updateObjectTransformRule(${stageIndex}, '${escJs(objectName)}', ${ruleIndex}, 'direction', this.value)">
        <option value="number_to_boolean" ${(rule.direction || 'number_to_boolean') === 'number_to_boolean' ? 'selected' : ''}>1/0 → True/False</option>
        <option value="boolean_to_number" ${rule.direction === 'boolean_to_number' ? 'selected' : ''}>True/False → 1/0</option>
      </select>
    `;
  } else if (rule.type === 'date_convert') {
    extra = `
      <select class="org-select template-select designer-transform-select" onchange="updateObjectTransformRule(${stageIndex}, '${escJs(objectName)}', ${ruleIndex}, 'source_format', this.value)">
        <option value="dmy2_dash" ${rule.source_format === 'dmy2_dash' ? 'selected' : ''}>DD-MM-YY to YYYY-MM-DD</option>
        <option value="mdy2" ${(rule.source_format || 'mdy2') === 'mdy2' ? 'selected' : ''}>M/D/YY → YYYY-MM-DD</option>
        <option value="mdy4" ${rule.source_format === 'mdy4' ? 'selected' : ''}>M/D/YYYY → YYYY-MM-DD</option>
        <option value="dmy2" ${rule.source_format === 'dmy2' ? 'selected' : ''}>D/M/YY → YYYY-MM-DD</option>
        <option value="dmy4" ${rule.source_format === 'dmy4' ? 'selected' : ''}>D/M/YYYY → YYYY-MM-DD</option>
        <option value="dmy4_dash" ${rule.source_format === 'dmy4_dash' ? 'selected' : ''}>DD-MM-YYYY → YYYY-MM-DD</option>
        <option value="iso" ${rule.source_format === 'iso' ? 'selected' : ''}>YYYY-MM-DD (validate/retain)</option>
      </select>
      <select class="org-select template-select designer-transform-select" onchange="updateObjectTransformRule(${stageIndex}, '${escJs(objectName)}', ${ruleIndex}, 'invalid_value', this.value)">
        <option value="retain" ${(rule.invalid_value || 'retain') === 'retain' ? 'selected' : ''}>Invalid → retain for load error</option>
        <option value="null" ${rule.invalid_value === 'null' ? 'selected' : ''}>Invalid → Null</option>
      </select>
    `;
  }

  return `
    <div class="designer-transform-row">
      <select class="org-select template-select designer-transform-select" onchange="updateObjectTransformRule(${stageIndex}, '${escJs(objectName)}', ${ruleIndex}, 'field', this.value)">${fieldOptions}</select>
      <select class="org-select template-select designer-transform-select" onchange="changeObjectTransformType(${stageIndex}, '${escJs(objectName)}', ${ruleIndex}, this.value)">${typeOptions}</select>
      ${extra}
      <button class="btn btn-danger btn-sm" onclick="removeObjectTransformRule(${stageIndex}, '${escJs(objectName)}', ${ruleIndex})">Remove</button>
    </div>
  `;
}

function renderStageTransforms(stage, stageIndex) {
  const objectNames = getStageObjectNames(stage);
  if (!objectNames.length) {
    return '<div class="designer-transform-empty">No objects available for transform configuration in this stage.</div>';
  }
  return objectNames.map(objectName => {
    const fields = getObjectFieldsForStage(stage, objectName);
    const rules = getTransformRulesForObject(stage, objectName);
    return `
      <div class="designer-transform-object">
        <div class="designer-transform-object-top">
          <div>
            <div class="designer-transform-object-name">${escHtml(objectName)}</div>
            <div class="designer-transform-object-meta">${escHtml(fields.length)} selected field(s)</div>
          </div>
          <div class="designer-transform-profile-actions">
            <select id="transform-profile-${stageIndex}-${escHtml(objectName)}" class="org-select template-select designer-transform-select">
              ${transformationProfileOptions(objectName)}
            </select>
            <button class="btn btn-secondary btn-sm" onclick="applyObjectTransformProfile(${stageIndex}, '${escJs(objectName)}')">Apply Profile</button>
            <button class="btn btn-secondary btn-sm" onclick="saveObjectTransformProfile(${stageIndex}, '${escJs(objectName)}')" ${rules.length ? '' : 'disabled'}>Save as Profile</button>
            <button class="btn btn-secondary btn-sm" onclick="deleteObjectTransformProfile(${stageIndex}, '${escJs(objectName)}')">Delete Profile</button>
            <button class="btn btn-secondary btn-sm" onclick="addObjectTransformRule(${stageIndex}, '${escJs(objectName)}')" ${fields.length ? '' : 'disabled'}>Add Transform</button>
          </div>
        </div>
        ${rules.length
          ? `<div class="designer-transform-list">${rules.map((rule, ruleIndex) => renderTransformRuleFields(rule, fields, stageIndex, objectName, ruleIndex)).join('')}</div>`
          : '<div class="designer-transform-empty">No transforms configured for this object.</div>'}
      </div>
    `;
  }).join('');
}

function selectedTransformProfileId(stageIndex, objectName) {
  return document.getElementById(`transform-profile-${stageIndex}-${objectName}`)?.value || '';
}

async function saveObjectTransformProfile(stageIndex, objectName) {
  const rules = getTransformRulesForObject(designerState.pipelineStages[stageIndex], objectName);
  if (!rules.length) {
    alert('Add at least one transformation rule first.');
    return;
  }
  const name = window.prompt(`Save ${objectName} transformations as profile:`, `${objectName} transforms`);
  if (!name || !name.trim()) {
    return;
  }
  const response = await fetch('/api/transform-profiles', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: name.trim(), object: objectName, rules }),
  });
  const data = await response.json();
  if (!response.ok || data.error) {
    alert(data.error || 'Failed to save transformation profile.');
    return;
  }
  await loadTransformationProfiles();
  alert(`Saved transformation profile "${data.profile.name}" for ${objectName}.`);
}

function applyObjectTransformProfile(stageIndex, objectName) {
  const profileId = selectedTransformProfileId(stageIndex, objectName);
  const profile = designerState.transformationProfiles.find(item =>
    item.id === profileId && item.object === objectName
  );
  if (!profile) {
    alert(`Choose a reusable ${objectName} profile first.`);
    return;
  }
  const current = getTransformRulesForObject(designerState.pipelineStages[stageIndex], objectName);
  if (current.length && !window.confirm(
    `Replace ${current.length} current ${objectName} transformation rule(s) with profile "${profile.name}"?`
  )) {
    return;
  }
  const stage = designerState.pipelineStages[stageIndex];
  stage.workspace = stage.workspace || {};
  stage.workspace.objectTransforms = stage.workspace.objectTransforms || {};
  stage.workspace.objectTransforms[objectName] = (profile.rules || []).map(rule => ({ ...rule }));
  persistDesignerState();
  renderPipelineFlow();
  renderTransformManager();
}

async function deleteObjectTransformProfile(stageIndex, objectName) {
  const profileId = selectedTransformProfileId(stageIndex, objectName);
  const profile = designerState.transformationProfiles.find(item =>
    item.id === profileId && item.object === objectName
  );
  if (!profile) {
    alert(`Choose a reusable ${objectName} profile first.`);
    return;
  }
  if (!window.confirm(`Delete transformation profile "${profile.name}"?`)) {
    return;
  }
  const response = await fetch(`/api/transform-profiles/${encodeURIComponent(profile.id)}`, {
    method: 'DELETE',
  });
  const data = await response.json();
  if (!response.ok || data.error) {
    alert(data.error || 'Failed to delete transformation profile.');
    return;
  }
  await loadTransformationProfiles();
}

function renderPipelineFlow() {
  const wrap = $('designer-pipeline-flow');
  const meta = $('designer-pipeline-meta');
  const startBtn = $('designer-start-pipeline');
  const scheduleBtn = $('designer-schedule-pipeline');
  const validationBtn = $('designer-run-validation-pipeline');
  const skipBtn = $('designer-skip-validation-pipeline');
  const postBtn = $('designer-run-post-validation');
  const readiness = collectPipelineValidationReadiness();
  const integrity = collectPipelineTypeIntegrity();
  const nameReadiness = collectPipelineNameReadiness();
  const nameReuseWarning = collectPipelineNameReuseWarning();
  const typeBlocked = integrity.blocked;
  const nameBlocked = !nameReadiness.ready;
  const snapshotBlocked = !isCurrentPipelineSnapshotSaved();
  const actionsLocked = typeBlocked || nameBlocked || snapshotBlocked || !!designerState.runStatus.runner?.launching;
  const nameTitle = nameBlocked ? (nameReadiness.message || 'Enter a unique Pipeline Name first.') : '';
  const actionTitle = snapshotBlocked ? 'Save the current pipeline snapshot before running it.' : nameTitle;
  if (startBtn) {
    startBtn.disabled = !designerState.pipelineStages.length || actionsLocked || !readiness.ready;
    startBtn.textContent = designerState.runStatus.runner?.launching
        ? 'Starting Migration...'
        : 'Start Migration';
    startBtn.title = typeBlocked
      ? (integrity.message || 'Correct mixed template types first.')
      : (snapshotBlocked || nameBlocked
        ? actionTitle
        : (readiness.ready ? '' : 'Run Pre-validation or Skip Pre-validation first to enable Start Migration.'));
  }
  if (scheduleBtn) {
    scheduleBtn.disabled = !designerState.pipelineStages.length || actionsLocked || !readiness.ready;
    scheduleBtn.title = typeBlocked
      ? (integrity.message || 'Correct mixed template types first.')
      : (snapshotBlocked || nameBlocked
        ? actionTitle
        : (readiness.ready ? 'Queue this pipeline to run after the current migration or at a chosen time.' : 'Run Pre-validation or Skip Pre-validation first to enable Schedule.'));
  }
  if (validationBtn) {
    validationBtn.disabled = !designerState.pipelineStages.length || actionsLocked;
    validationBtn.title = typeBlocked
      ? (integrity.message || 'Correct mixed template types first.')
      : actionTitle;
  }
  if (skipBtn) {
    skipBtn.disabled = !designerState.pipelineStages.length || actionsLocked;
    skipBtn.title = typeBlocked
      ? (integrity.message || 'Correct mixed template types first.')
      : (snapshotBlocked || nameBlocked
        ? actionTitle
        : (readiness.ready
          ? 'All stages are already validation-ready. You can skip again if needed.'
          : 'Mark all stages as bypassed so Start Migration can proceed.'));
  }
  if (postBtn) {
    postBtn.disabled = actionsLocked || typeBlocked;
    postBtn.title = typeBlocked
      ? (integrity.message || 'Correct mixed template types first.')
      : (nameBlocked
        ? nameTitle
        : 'Open Validation Center to run post-migration validation against a completed migration run.');
  }
  if (!designerState.pipelineStages.length) {
    meta.textContent = getSelectedPipelineType()
      ? `${pipelineTypeLabel(getSelectedPipelineType())} pipeline ready. Add matching templates from the library.`
      : 'Select Pipeline Type first (SF → SF or Files → SF), then assemble matching templates into one sequence.';
    wrap.innerHTML = getSelectedPipelineType()
      ? `<div class="designer-flow-empty">No stages added yet. Add ${pipelineTypeLabel(getSelectedPipelineType())} templates from the library.</div>`
      : '<div class="designer-flow-empty">Select SF → SF or Files → SF first. A pipeline cannot mix both operation types.</div>';
    return;
  }

  if (typeBlocked) {
    meta.textContent = integrity.message;
  } else if (nameBlocked) {
    meta.textContent = nameReadiness.message;
  } else if (snapshotBlocked) {
    meta.textContent = 'Save the current pipeline snapshot before Skip Pre-validation or Start Migration.';
  } else {
    const reuseNote = nameReuseWarning ? ` ${nameReuseWarning}` : '';
    meta.textContent = readiness.ready
      ? `${pipelineTypeLabel(getSelectedPipelineType())}: ${designerState.pipelineStages.length} stage(s) in flow. Validation-ready for migration.${reuseNote}`
      : `${pipelineTypeLabel(getSelectedPipelineType())}: ${designerState.pipelineStages.length} stage(s) in flow. ${readiness.blockedStages.length} stage(s) still need Pre-validation or Skip Pre-validation before Start Migration is enabled.${reuseNote}`;
  }

  wrap.innerHTML = designerState.pipelineStages.map((stage, index) => {
    const collapsed = stage.collapsed !== false;
    const validationStatus = normalizePrevalidationStatus(stage.prevalidationStatus);
    const validationReady = isStageValidationReady(stage);
    const stageMode = normalizeInputMode(stage.inputMode);
    const stageMismatch = !integrity.selectedType || stageMode !== integrity.selectedType;
    return `
      <div class="designer-stage-shell">
        <div class="designer-stage-card ${collapsed ? 'is-collapsed' : ''}${stageMismatch ? ' is-invalid' : ''}">
          <div class="designer-stage-top">
            <div>
              <div class="designer-stage-order">Stage ${index + 1}</div>
              <div class="designer-stage-title">${escHtml(stage.pipelineName)}</div>
              <div class="designer-stage-template">${escHtml(stage.templateName)} | ${escHtml(stage.pipelineId)} | ${modeBadgeLabel(stage.inputMode)}</div>
            </div>
            <div class="designer-stage-top-actions">
              <span class="pipeline-stage-status ${stageMismatch ? 'status-pending' : (validationReady ? 'status-running' : 'status-pending')}">${escHtml(stageMismatch ? 'TYPE MISMATCH' : validationStatus)}</span>
              <button class="btn btn-secondary btn-sm designer-stage-toggle" onclick="toggleDesignerStageCollapse(${index})">${collapsed ? 'Expand' : 'Collapse'}</button>
            </div>
          </div>
          <div class="designer-stage-body">
            <div class="designer-stage-line">${stage.inputMode === 'flat_file' ? 'First object' : 'Seed'}: ${escHtml(stage.seedObject || 'Unknown')}</div>
            <div class="designer-stage-line">Objects: ${escHtml(stage.objectCount)} | Nodes: ${escHtml(stage.nodeCount)}</div>
            <div class="designer-stage-line">${stage.inputMode === 'flat_file'
              ? `Target: ${escHtml(orgDomainLabel(stage.targetDomain) || 'Unknown')} (${escHtml(stageTargetProfile(stage))})`
              : `Source: ${escHtml(orgDomainLabel(stage.sourceDomain) || 'Unknown')} (${escHtml(stageSourceProfile(stage))}) | Target: ${escHtml(orgDomainLabel(stage.targetDomain) || 'Unknown')} (${escHtml(stageTargetProfile(stage))})`}</div>
            <div class="designer-stage-line">${stageMismatch
              ? `Wrong type for this pipeline (${modeBadgeLabel(stage.inputMode)}). Remove this stage.`
              : `Validation: ${escHtml(validationStatus)}${validationReady ? ' | ready for migration' : ' | run or skip pre-validation first'}`}</div>
          </div>
          <div class="designer-stage-actions">
            <button class="btn btn-secondary btn-sm" onclick="${stage.inputMode === 'flat_file' ? `window.location.href='/flat-files'` : `openTemplateInBuilder('${escJs(stage.templateId)}')`}">${stage.inputMode === 'flat_file' ? 'Open Mapping' : 'Open Builder'}</button>
            <button class="btn btn-secondary btn-sm" onclick="openTransformManager(${index})" ${stageMismatch ? 'disabled' : ''}>Manage Transforms</button>
            <button class="btn btn-secondary btn-sm" onclick="moveDesignerStage(${index}, -1)" ${index === 0 ? 'disabled' : ''}>Left</button>
            <button class="btn btn-secondary btn-sm" onclick="moveDesignerStage(${index}, 1)" ${index === designerState.pipelineStages.length - 1 ? 'disabled' : ''}>Right</button>
            <button class="btn btn-danger btn-sm" onclick="removeDesignerStage(${index})">Remove</button>
          </div>
        </div>
        ${index < designerState.pipelineStages.length - 1 ? '<div class="designer-flow-arrow">➜</div>' : ''}
      </div>
    `;
  }).join('');
}

function moveDesignerStage(index, direction) {
  const target = index + direction;
  if (target < 0 || target >= designerState.pipelineStages.length) {
    return;
  }
  const [stage] = designerState.pipelineStages.splice(index, 1);
  designerState.pipelineStages.splice(target, 0, stage);
  designerState.suppressStoredRunStatus = true;
  persistDesignerState();
  renderPipelineFlow();
}

function toggleDesignerStageCollapse(index) {
  const stage = designerState.pipelineStages[index];
  if (!stage) {
    return;
  }
  stage.collapsed = stage.collapsed === false;
  persistDesignerState();
  renderPipelineFlow();
}

function removeDesignerStage(index) {
  designerState.pipelineStages.splice(index, 1);
  syncDesignerProfilesFromStages();
  designerState.suppressStoredRunStatus = true;
  persistDesignerState();
  renderTemplateLibrary();
  renderPipelineFlow();
}

function clearPipelineStages() {
  if (!designerState.pipelineStages.length) {
    return;
  }
  if (!window.confirm('Clear the complete pipeline flow?')) {
    return;
  }
  designerState.pipelineStages = [];
  designerState.suppressStoredRunStatus = true;
  persistDesignerState();
  renderTemplateLibrary();
  renderPipelineFlow();
  renderDesignerPanels();
}

function openTemplateInBuilder(templateId) {
  window.location.href = `/?template=${encodeURIComponent(templateId)}`;
}

async function loadRunStatus(showError = false) {
  try {
    const runKey = designerState.runStatus.runner?.run_key || '';
    const query = new URLSearchParams({ _: String(Date.now()) });
    if (runKey) {
      query.set('run_key', runKey);
    }
    const response = await fetch(`/api/run-status?${query.toString()}`, { cache: 'no-store' });
    const data = await response.json();
    if (!response.ok || data.error) {
      throw new Error(data.error || 'Failed to load run status');
    }
    designerState.runStatus = {
      migration: data.migration || null,
      started_at: data.started_at || null,
      last_updated: data.last_updated || null,
      pipelines: data.pipelines || {},
      runner: data.runner || {
        running: false,
        pid: null,
        started_at: null,
        log_path: '',
        config_path: '',
        returncode: null,
      },
    };
    persistDesignerState();
    renderStatusBanner();
    renderPipelineFlow();
    await loadFailedFiles();
  } catch (e) {
    if (showError) {
      alert(e.message);
    }
  }
}

function renderStatusBanner() {
  const banner = $('pipeline-status-banner');
  if (!banner) {
    return;
  }
  const runner = designerState.runStatus.runner || {};
  const visibleRunStatus = getVisibleRunStatus();
  updateLogActions();
  if (runner.launching) {
    banner.className = 'auth-status checking';
    banner.innerHTML = '<span class="dot"></span> Pipeline starting | Preparing run request...';
    return;
  }
  if (runner.running) {
    banner.className = 'auth-status checking';
    const activeCount = Number(runner.active_run_count || 0);
    const countSuffix = activeCount > 1 ? ` | ${activeCount} active runs` : '';
    banner.innerHTML = `<span class="dot"></span> Pipeline running | Started ${escHtml(formatTimestamp(runner.started_at))}${escHtml(countSuffix)}`;
    return;
  }
  if (runner.returncode != null && runner.returncode !== 0) {
    banner.className = 'auth-status error';
    banner.innerHTML = `<span class="dot"></span> Last pipeline run failed | Exit ${escHtml(runner.returncode)}`;
    return;
  }
  const lastUpdated = visibleRunStatus.last_updated ? formatTimestamp(visibleRunStatus.last_updated) : 'No updates yet';
  const runSummary = runner.started_at
    ? `Last launch ${formatTimestamp(runner.started_at)}${runner.returncode === 0 ? ' | completed' : runner.returncode != null ? ` | exit ${runner.returncode}` : ''}`
    : visibleRunStatus.last_updated ? `Latest update ${lastUpdated}` : 'No run data for current flow yet';
  banner.className = 'auth-status ok';
  banner.innerHTML = `<span class="dot"></span> Pipeline status ready | ${escHtml(runSummary)}`;
}

function updateLogActions() {
  const openFullscreenLink = document.querySelector('a[href="/pipeline/logs"], a[data-role="designer-open-fullscreen"]');
  if (!openFullscreenLink) {
    return;
  }
  const runKey = designerState.runStatus?.runner?.run_key || '';
  openFullscreenLink.dataset.role = 'designer-open-fullscreen';
  openFullscreenLink.href = runKey
    ? `/pipeline/logs?run_key=${encodeURIComponent(runKey)}`
    : '/pipeline/logs';
}

function summarizePipelineMetrics(record) {
  if (!record) {
    return 'Waiting for first run';
  }
  const objects = Object.values(record.objects || {});
  const completed = objects.filter(objectState => objectState.status === 'COMPLETED').length;
  const running = objects.filter(objectState => objectState.status === 'RUNNING').length;
  const failed = objects.filter(objectState => objectState.status === 'FAILED').length;
  return `${objects.length} object(s) tracked | Completed ${completed} | Running ${running} | Failed ${failed}`;
}

function isZeroRowCompletedPipeline(record) {
  if (!record || String(record.status || '').toUpperCase() !== 'COMPLETED') {
    return false;
  }
  const objects = Object.values(record.objects || {});
  if (!objects.length) {
    return false;
  }
  return objects.every(objectState =>
    String(objectState.status || '').toUpperCase() === 'COMPLETED'
    && Number(objectState.extracted || 0) === 0
    && Number(objectState.transformed || 0) === 0
    && Number(objectState.loaded_success || 0) === 0
    && Number(objectState.loaded_failed || 0) === 0
  );
}

function getDisplayPipelineStatus(record) {
  if (isZeroRowCompletedPipeline(record)) {
    return 'NO DATA';
  }
  return record?.status || 'PENDING';
}

function summarizePipelineLatestUpdate(record) {
  if (!record) {
    return 'No run data yet';
  }
  const timestamps = [
    record.failed_at,
    record.completed_at,
    record.started_at,
    ...Object.values(record.objects || {}).flatMap(objectState => [
      objectState.failed_at,
      objectState.completed_at,
      objectState.started_at,
    ]),
  ].filter(Boolean);
  if (!timestamps.length) {
    return 'No object updates yet';
  }
  timestamps.sort((a, b) => new Date(b).getTime() - new Date(a).getTime());
  return formatTimestamp(timestamps[0]);
}

function pipelineStatusClass(status) {
  const normalized = String(status || 'PENDING').toLowerCase().replace(/\s+/g, '-');
  if (normalized.includes('failed')) {
    return 'failed';
  }
  return ['pending', 'running', 'completed', 'failed', 'skipped', 'blocked', 'no-data'].includes(normalized)
    ? normalized
    : 'pending';
}

function getAvailableFailedPipelines() {
  const fromStages = designerState.pipelineStages.map(stage => stage.pipelineId).filter(Boolean);
  const fromRunStatus = Object.keys(getVisibleRunStatus().pipelines || {});
  return [...new Set([...fromStages, ...fromRunStatus])].sort((a, b) => a.localeCompare(b));
}

function getVisibleRunStatus() {
  const runner = designerState.runStatus.runner || {};
  if (designerState.suppressStoredRunStatus && !runner.running && !runner.launching) {
    return {
      last_updated: null,
      pipelines: {},
    };
  }
  return {
    last_updated: designerState.runStatus.last_updated || null,
    pipelines: designerState.runStatus.pipelines || {},
  };
}

function renderFailedPipelineOptions() {
  const select = $('designer-failed-pipeline');
  if (!select) {
    return;
  }
  const pipelineIds = getAvailableFailedPipelines();
  const domSelected = select.value;
  if (!pipelineIds.length) {
    select.innerHTML = '<option value="">No pipeline</option>';
    select.disabled = true;
    return;
  }

  const selected = domSelected && pipelineIds.includes(domSelected)
    ? domSelected
    : designerState.failedFiles.pipelineId && pipelineIds.includes(designerState.failedFiles.pipelineId)
      ? designerState.failedFiles.pipelineId
    : pipelineIds[0];
  designerState.failedFiles.pipelineId = selected;
  select.disabled = false;
  select.innerHTML = pipelineIds.map(pipelineId => (
    `<option value="${escHtml(pipelineId)}" ${pipelineId === selected ? 'selected' : ''}>${escHtml(pipelineId)}</option>`
  )).join('');
}

function openVerifyModal() {
  renderVerifyPipelineOptions();
  renderVerifyObjectOptions();
  $('designer-verify-modal').style.display = 'flex';
}

function closeVerifyModal() {
  $('designer-verify-modal').style.display = 'none';
}

function renderVerifyPipelineOptions() {
  const select = $('designer-verify-pipeline');
  if (!select) {
    return;
  }
  const stages = designerState.pipelineStages || [];
  if (!stages.length) {
    select.innerHTML = '<option value="">No stages</option>';
    select.disabled = true;
    return;
  }
  select.disabled = false;
  select.innerHTML = stages.map(stage => (
    `<option value="${escHtml(stage.pipelineId)}">${escHtml(stage.pipelineId)} - ${escHtml(stage.pipelineName)}</option>`
  )).join('');
}

function renderStageSelectOptions(selectId) {
  const select = $(selectId);
  if (!select) {
    return;
  }
  const stages = designerState.pipelineStages || [];
  if (!stages.length) {
    select.innerHTML = '<option value="">No stages</option>';
    select.disabled = true;
    return;
  }
  select.disabled = false;
  select.innerHTML = stages.map(stage => (
    `<option value="${escHtml(stage.pipelineId)}">${escHtml(stage.pipelineId)} - ${escHtml(stage.pipelineName)}</option>`
  )).join('');
}

function renderVerifyObjectOptions() {
  renderStageObjectOptions('designer-verify-pipeline', 'designer-verify-object');
}

function renderStageObjectOptions(pipelineSelectId, objectSelectId) {
  const pipelineId = $(pipelineSelectId)?.value || '';
  const select = $(objectSelectId);
  if (!select) {
    return;
  }
  const stage = (designerState.pipelineStages || []).find(entry => entry.pipelineId === pipelineId);
  const objects = stage ? getStageObjectNames(stage) : [];
  if (!objects.length) {
    select.innerHTML = '<option value="">No objects</option>';
    select.disabled = true;
    return;
  }
  select.disabled = false;
  select.innerHTML = objects.map(objectName => `<option value="${escHtml(objectName)}">${escHtml(objectName)}</option>`).join('');
}

function openVerifyRecordsModal() {
  renderStageSelectOptions('designer-verify-records-pipeline');
  renderVerifyRecordsObjectOptions();
  $('designer-verify-records-modal').style.display = 'flex';
}

function closeVerifyRecordsModal() {
  $('designer-verify-records-modal').style.display = 'none';
}

function renderVerifyRecordsObjectOptions() {
  renderStageObjectOptions('designer-verify-records-pipeline', 'designer-verify-records-object');
}

async function runVerifyRecords() {
  if (designerState.runStatus.runner?.running) {
    alert('A pipeline run is already in progress.');
    return;
  }
  const runDir = ($('designer-verify-records-run-dir')?.value || '').trim();
  const pipelineId = $('designer-verify-records-pipeline')?.value || '';
  const objectName = $('designer-verify-records-object')?.value || '';
  if (!runDir || !pipelineId || !objectName) {
    alert('Run Dir, Stage ID, and Object are required.');
    return;
  }
  const response = await fetch('/api/pipeline/verify-records', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ run_dir: runDir, pipeline_id: pipelineId, object_name: objectName }),
  });
  const data = await response.json();
  if (!response.ok || data.error) {
    alert(data.error || 'Failed to start verify records.');
    return;
  }
  if (data.runner) {
    designerState.runStatus.runner = data.runner;
  }
  closeVerifyRecordsModal();
  await loadPipelineLogs(true);
  await loadRunStatus();
  alert(`Started verify_records for ${objectName}. Check Run Logs for progress.`);
}

function openReportRefsModal() {
  renderStageSelectOptions('designer-report-refs-pipeline');
  $('designer-report-refs-modal').style.display = 'flex';
}

function closeReportRefsModal() {
  $('designer-report-refs-modal').style.display = 'none';
}

async function runReportRefs() {
  if (designerState.runStatus.runner?.running) {
    alert('A pipeline run is already in progress.');
    return;
  }
  const runDir = ($('designer-report-refs-run-dir')?.value || '').trim();
  const pipelineId = $('designer-report-refs-pipeline')?.value || '';
  const fromLastLevel = parseInt($('designer-report-refs-from-last-level')?.value || '0', 10) || 0;
  if (!runDir || !pipelineId) {
    alert('Run Dir and Stage ID are required.');
    return;
  }
  const response = await fetch('/api/pipeline/report-last-level-refs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ run_dir: runDir, pipeline_id: pipelineId, from_last_level: fromLastLevel }),
  });
  const data = await response.json();
  if (!response.ok || data.error) {
    alert(data.error || 'Failed to start reference report.');
    return;
  }
  if (data.runner) {
    designerState.runStatus.runner = data.runner;
  }
  closeReportRefsModal();
  await loadPipelineLogs(true);
  await loadRunStatus();
  alert(`Started last-level source/target ref report for ${pipelineId}. Check Run Logs for progress.`);
}

async function runVerifySelfLookup() {
  if (designerState.runStatus.runner?.running) {
    alert('A pipeline run is already in progress.');
    return;
  }
  const pipelineId = $('designer-verify-pipeline')?.value || '';
  const objectName = $('designer-verify-object')?.value || '';
  const fieldName = $('designer-verify-field')?.value || '';
  const sampleSize = parseInt($('designer-verify-sample-size')?.value || '25', 10) || 25;
  if (!pipelineId || !objectName) {
    alert('Select a stage and object to verify.');
    return;
  }

  const response = await fetch('/api/pipeline/verify-self-lookup', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      pipeline_id: pipelineId,
      object_name: objectName,
      field_name: fieldName,
      sample_size: sampleSize,
    }),
  });
  const data = await response.json();
  if (!response.ok || data.error) {
    alert(data.error || 'Failed to start verify.');
    return;
  }
  if (data.runner) {
    designerState.runStatus.runner = data.runner;
  }
  closeVerifyModal();
  await loadPipelineLogs(true);
  await loadRunStatus();
  alert(`Started verify for ${objectName} in stage ${pipelineId}. Check Run Logs for progress.`);
}

function getSelectedFailedPathSet() {
  return new Set(designerState.failedFiles.selectedPaths || []);
}

function syncSelectedFailedPaths() {
  const availablePaths = new Set((designerState.failedFiles.files || []).map(file => file.path).filter(Boolean));
  designerState.failedFiles.selectedPaths = (designerState.failedFiles.selectedPaths || [])
    .filter(path => availablePaths.has(path));
}

function updateFailedSelectionSummary() {
  const btn = $('designer-select-all-failed');
  if (!btn) {
    return;
  }
  const total = (designerState.failedFiles.files || []).length;
  const selectedCount = (designerState.failedFiles.selectedPaths || []).length;
  if (!total) {
    btn.textContent = 'Select All';
    btn.disabled = true;
    return;
  }
  btn.disabled = false;
  btn.textContent = selectedCount === total ? 'Clear All' : `Select All (${selectedCount}/${total})`;
}

function toggleFailedFileSelection(path, checked) {
  if (!path) {
    return;
  }
  const selected = getSelectedFailedPathSet();
  if (checked) {
    selected.add(path);
  } else {
    selected.delete(path);
  }
  designerState.failedFiles.selectedPaths = [...selected];
  updateFailedSelectionSummary();
}

function toggleSelectAllFailedFiles() {
  const files = designerState.failedFiles.files || [];
  if (!files.length) {
    return;
  }
  const allPaths = files.map(file => file.path).filter(Boolean);
  const selected = getSelectedFailedPathSet();
  const shouldSelectAll = allPaths.some(path => !selected.has(path));
  designerState.failedFiles.selectedPaths = shouldSelectAll ? allPaths : [];
  renderFailedFiles();
}

function toggleDesignerPanel(panelKey) {
  if (!designerState.panels || !(panelKey in designerState.panels)) {
    return;
  }
  designerState.panels[panelKey] = !designerState.panels[panelKey];
  persistDesignerState();
  renderDesignerPanels();
}

function renderDesignerPanels() {
  const failedPanel = $('designer-failed-panel');
  const logsPanel = $('designer-log-panel');
  const failedToggle = $('designer-failed-toggle');
  const logsToggle = $('designer-logs-toggle');

  if (failedPanel) {
    failedPanel.classList.toggle('is-collapsed', !!designerState.panels.failedCollapsed);
  }
  if (logsPanel) {
    logsPanel.classList.toggle('is-collapsed', !!designerState.panels.logsCollapsed);
  }
  if (failedToggle) {
    failedToggle.textContent = designerState.panels.failedCollapsed ? 'Expand' : 'Collapse';
  }
  if (logsToggle) {
    logsToggle.textContent = designerState.panels.logsCollapsed ? 'Expand' : 'Collapse';
  }
}

function buildPipelineRequest(stage) {
  const sourceObjects = Array.isArray(stage?.workspace?.sourceObjects) && stage.workspace.sourceObjects.length
    ? stage.workspace.sourceObjects
    : (stage?.workspace?.sourceTemplate ? [stage.workspace.sourceTemplate] : null);
  if (sourceObjects) {
    const selections = [];
    const nodes = [];
    sourceObjects.forEach((sourceTemplate, index) => {
      const sourceObject = String(sourceTemplate.source_object || '').trim();
      const targetObject = String(sourceTemplate.target_object || '').trim();
      if (sourceObject && targetObject && sourceObject !== targetObject) {
        throw new Error(`Source template "${stage.pipelineName}" uses ${sourceObject} -> ${targetObject}. Runtime currently supports same object API names only.`);
      }
      const objectName = sourceObject || targetObject;
      const sourceMatchField = String(sourceTemplate.match_mapping?.source_field || '').trim();
      const targetMatchField = String(sourceTemplate.match_mapping?.target_field || '').trim();
      const configuredOperation = String(sourceTemplate.default_operation || '').trim().toLowerCase();
      const operation = ['insert', 'update', 'upsert'].includes(configuredOperation) ? configuredOperation : 'upsert';
      const externalId = targetMatchField || String(sourceTemplate.target_external_id || '').trim() || 'Id';
      const selectedFields = [...new Set([
        'Id',
        ...(sourceTemplate.field_mappings || []).map(item => item?.source_field).filter(Boolean),
        sourceMatchField,
      ].filter(Boolean))].sort((a, b) => a.localeCompare(b));
      selections.push({
        object: objectName,
        selected_fields: selectedFields,
        external_id: externalId,
        source_key_field: sourceMatchField || 'Id',
        operation,
        match_by: targetMatchField || '',
        transforms: Array.isArray(sourceTemplate.transform_rules) ? sourceTemplate.transform_rules.map(rule => ({ ...rule })) : [],
        lookup_mappings: sourceTemplate.lookup_mappings && typeof sourceTemplate.lookup_mappings === 'object'
          ? { ...sourceTemplate.lookup_mappings }
          : {},
        record_type_resolution: sourceTemplate.record_type_resolution || 'developer_name',
        record_type_mappings: sourceTemplate.record_type_mappings && typeof sourceTemplate.record_type_mappings === 'object'
          ? { ...sourceTemplate.record_type_mappings }
          : {},
      });
      nodes.push({
        id: `source_mapping_${objectName || 'root'}_${index}`,
        object: objectName,
        parent_id: null,
        direction: index === 0 ? 'seed' : 'child',
        via_field: '',
        depth: index === 0 ? 0 : 1,
        query_filter: String(sourceTemplate.query_filter || ''),
      });
    });
    const seedObject = String(sourceObjects[0]?.source_object || sourceObjects[0]?.target_object || '').trim();
    return {
      input_mode: 'salesforce_source',
      source_object: seedObject,
      target_object: seedObject,
      flat_file_template_id: '',
      seed_object: seedObject,
      seed_filter: String(sourceObjects[0]?.query_filter || ''),
      seed_where: '',
      seed_order_by: '',
      seed_limit: '',
      seed_offset: '',
      max_records: 10000,
      migration_name: stage.pipelineName,
      pipeline_id: stage.pipelineId,
      pipeline_name: stage.pipelineName,
      selections,
      traversal: { parents: [], children: [] },
      nodes,
    };
  }
  const workspace = stage.workspace || {};
  const controls = workspace.controls || {};
  const nodes = workspace.nodes || [];
  const rootNode = nodes.find(node => node.id === workspace.rootId) || nodes.find(node => !node.parentId) || null;
  const selectionsByObject = new Map();

  nodes.forEach(node => {
    if (!node.object) {
      return;
    }
    const selection = workspace.nodeSelections?.[node.id] || {};
    const objectName = node.object;
    const externalId = workspace.objectExternalIds?.[objectName] || selection.externalId || '';
    if (!selectionsByObject.has(objectName)) {
      selectionsByObject.set(objectName, {
        object: objectName,
        selected_fields: new Set(),
        external_id: externalId,
      });
    }
    const entry = selectionsByObject.get(objectName);
    (selection.selectedFields || []).forEach(field => entry.selected_fields.add(field));
    entry.external_id = externalId;
  });

  const objectTransforms = workspace.objectTransforms || {};
  const selections = Array.from(selectionsByObject.values()).map(entry => ({
    object: entry.object,
    selected_fields: [...entry.selected_fields].sort(),
    external_id: entry.external_id || '',
    transforms: Array.isArray(objectTransforms[entry.object])
      ? objectTransforms[entry.object].map(rule => ({ ...rule }))
      : [],
    lookup_mappings: { ...(workspace.lookupMappings?.[entry.object] || {}) },
  }));

  const traversal = {
    parents: nodes.filter(node => node.direction === 'parent').map(node => ({ object: node.object, via_field: node.via_field, depth: node.depth })),
    children: nodes.filter(node => node.direction === 'child').map(node => ({ object: node.object, via_field: node.via_field, depth: node.depth })),
  };

  const requestNodes = nodes.map(node => {
    const selection = workspace.nodeSelections?.[node.id] || {};
    return {
      id: node.id,
      object: node.object,
      parent_id: node.parentId,
      direction: node.direction,
      via_field: node.via_field,
      depth: node.depth,
      query_filter: selection.queryFilter || composeQueryFilter(selection),
      where_clause: selection.whereClause || '',
      order_by: selection.orderBy || '',
      limit: selection.limit || '',
      offset: selection.offset || '',
    };
  });

  return {
    input_mode: normalizeInputMode(stage.inputMode),
    flat_file_template_id: stage.flatFileTemplateId || '',
    seed_object: rootNode?.object || '',
    seed_filter: composeQueryFilter({
      whereClause: controls.seedFilter || '',
      orderBy: controls.seedOrder || '',
      limit: controls.seedLimit || '',
      offset: controls.seedOffset || '',
    }),
    seed_where: controls.seedFilter || '',
    seed_order_by: controls.seedOrder || '',
    seed_limit: controls.seedLimit || '',
    seed_offset: controls.seedOffset || '',
    max_records: parseInt(controls.maxRecords, 10) || 10000,
    migration_name: stage.pipelineName,
    pipeline_id: stage.pipelineId,
    pipeline_name: stage.pipelineName,
    selections,
    traversal,
    nodes: requestNodes,
  };
}

async function requestPipelineConfig(saveToFile = false) {
  if (!designerState.pipelineStages.length) {
    alert('Add at least one stage to the pipeline first.');
    return null;
  }
  if (!requirePipelineType() || !assertHomogeneousPipelineStages()) {
    return null;
  }
  const pipelineBaseName = requirePipelineBaseName();
  if (!pipelineBaseName) {
    return null;
  }
  await syncPipelineStagesFromTemplates();
  const payload = {
    migration_name: `${pipelineBaseName} - Migration`,
    save_to_file: saveToFile,
    pipelines: designerState.pipelineStages.map(stage => buildPipelineRequest(stage)),
  };
  const response = await fetch('/api/config/generate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok || data.error) {
    alert(data.error || 'Failed to generate pipeline config');
    return null;
  }
  return data;
}

async function startPipelineRun() {
  if (!designerState.pipelineStages.length) {
    alert('Add at least one stage to the pipeline first.');
    return;
  }
  if (!requirePipelineType() || !assertHomogeneousPipelineStages()) {
    return;
  }
  if (!requireSavedPipelineSnapshot()) {
    return;
  }
  const pipelineBaseName = requirePipelineBaseName();
  if (!pipelineBaseName) {
    return;
  }
  const readiness = collectPipelineValidationReadiness();
  if (!readiness.ready) {
    const blockedNames = readiness.blockedStages.map(stage => stage.pipelineName).join(', ');
    alert(`Run Pre-validation or Skip Pre-validation before migration. Pending stage(s): ${blockedNames}`);
    return;
  }
  const loadBatchSize = _askLoadBatchSize();
  if (loadBatchSize == null) {
    return;
  }
  showPipelinePreparation('Preparing pipeline...', 'Refreshing stage templates and mappings.');
  try {
    await syncPipelineStagesFromTemplates();
  } catch (error) {
    hidePipelinePreparation();
    alert(error.message || 'Failed to prepare pipeline stages.');
    return;
  }
  updatePipelinePreparation('Preparing run configuration...', 'Validating stage order, profiles, transforms, and flat-file inputs.');
  designerState.suppressStoredRunStatus = false;

  designerState.runStatus.runner = {
    ...designerState.runStatus.runner,
    launching: true,
    running: false,
    started_at: new Date().toISOString(),
    returncode: null,
  };
  renderStatusBanner();
  renderPipelineFlow();

  const isFlatFilePipeline = getSelectedPipelineType() === 'flat_file';
  if (isFlatFilePipeline && getSelectedStartStage() === 'extract') {
    setSelectedStartStage('transform');
  }
  const payload = {
    migration_name: `${pipelineBaseName} - Migration`,
    snapshot_id: designerState.savedSnapshotId || '',
    fresh: true,
    dry_run: false,
    mask_emails: _getMaskEmailsValue(),
    remove_email_mask: _getRemoveEmailMaskValue(),
    stop_after_transform: _getStopAfterTransformValue(),
    load_batch_size: loadBatchSize,
    target_domain: designerState.pipelineStages?.[0]?.target_domain || designerState.pipelineStages?.[0]?.targetDomain || '',
    start_stage: isFlatFilePipeline ? 'transform' : getSelectedStartStage(),
    source_profile: getSelectedSourceProfile(),
    target_profile: getSelectedTargetProfile(),
    pipelines: designerState.pipelineStages.map(stage => buildPipelineRequest(stage)),
  };

  try {
    updatePipelinePreparation('Starting pipeline...', 'Creating the run, log, state, and artifact directories.');
    const response = await fetch('/api/pipeline/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const responseText = await response.text();
    let data;
    try {
      data = responseText ? JSON.parse(responseText) : {};
    } catch (parseError) {
      const contentType = response.headers.get('content-type') || 'unknown content type';
      const preview = responseText.replace(/\s+/g, ' ').trim().slice(0, 240);
      throw new Error(
        `Start Migration returned HTTP ${response.status} as ${contentType}, not JSON.`
        + (preview ? ` Response: ${preview}` : '')
      );
    }
    if (!response.ok || data.error) {
      throw new Error(data.error || 'Failed to start pipeline.');
    }

    if (data.runner) {
      designerState.runStatus.runner = {
        launching: false,
        ...data.runner,
      };
    } else {
      designerState.runStatus.runner = {
        ...designerState.runStatus.runner,
        launching: false,
      };
    }
    const progressUrl = data.progress_url || (data.run_key ? `/pipeline/runs/${encodeURIComponent(data.run_key)}` : '');
    if (progressUrl) {
      updatePipelinePreparation('Pipeline accepted', 'Opening the live progress page...');
      window.location.assign(progressUrl);
      return;
    }
    hidePipelinePreparation();
    alert(`Pipeline started from ${getSelectedStartStage()}. Progress will update on this screen automatically.`);
  } catch (error) {
    designerState.runStatus.runner = {
      ...designerState.runStatus.runner,
      launching: false,
      running: false,
    };
    renderStatusBanner();
    renderPipelineFlow();
    hidePipelinePreparation();
    alert(error.message || 'Failed to start pipeline.');
  }
}

function defaultScheduleDateTimeValue() {
  const value = new Date(Date.now() + 60 * 60 * 1000);
  const pad = number => String(number).padStart(2, '0');
  return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}T${pad(value.getHours())}:${pad(value.getMinutes())}`;
}

function closeScheduleModal() {
  const modal = $('designer-schedule-modal');
  if (modal) {
    modal.style.display = 'none';
  }
}

function openScheduleModal() {
  if (!designerState.pipelineStages.length) {
    alert('Add at least one stage to the pipeline first.');
    return;
  }
  if (!requirePipelineType() || !assertHomogeneousPipelineStages()) {
    return;
  }
  if (!requireSavedPipelineSnapshot()) {
    return;
  }
  if (!requirePipelineBaseName()) {
    return;
  }
  const readiness = collectPipelineValidationReadiness();
  if (!readiness.ready) {
    const blockedNames = readiness.blockedStages.map(stage => stage.pipelineName).join(', ');
    alert(`Run Pre-validation or Skip Pre-validation before scheduling. Pending stage(s): ${blockedNames}`);
    return;
  }
  const startFrom = $('designer-schedule-start-from');
  if (startFrom) {
    startFrom.innerHTML = designerState.pipelineStages.map((stage, index) => (
      `<option value="${index}">${index + 1}. ${escHtml(stage.pipelineName || stage.pipelineId || `Stage ${index + 1}`)}</option>`
    )).join('');
  }
  const afterCurrent = $('designer-schedule-after-current');
  const atTime = $('designer-schedule-at-time');
  const datetime = $('designer-schedule-datetime');
  const stageMode = $('designer-schedule-stage-mode');
  if (afterCurrent) {
    afterCurrent.checked = true;
  }
  if (atTime) {
    atTime.checked = false;
  }
  if (datetime) {
    datetime.value = defaultScheduleDateTimeValue();
  }
  if (stageMode) {
    stageMode.value = getSelectedPipelineType() === 'flat_file' ? 'separate_runs' : 'single_run';
  }
  $('designer-schedule-modal').style.display = 'flex';
}

function formatScheduleWhen(job) {
  const parts = [];
  if (job.after_current) {
    parts.push('after current run');
  }
  if (job.run_at) {
    parts.push(formatTimestamp(job.run_at));
  }
  return parts.join(' and ') || 'when idle';
}

function renderScheduledJobs(jobs = []) {
  const panel = $('designer-schedule-panel');
  if (!panel) {
    return;
  }
  const visible = (jobs || []).filter(job => ['queued', 'running', 'failed'].includes(String(job.status || '')));
  if (!visible.length) {
    panel.hidden = true;
    panel.innerHTML = '';
    return;
  }
  panel.hidden = false;
  panel.innerHTML = `
    <div class="designer-schedule-title">Scheduled migrations</div>
    <div class="designer-schedule-list">
      ${visible.map(job => {
        const names = (job.stage_names || []).join(' → ') || job.label || job.id;
        const runLink = job.active_run_key
          ? ` <a href="/pipeline/runs/${encodeURIComponent(job.active_run_key)}">Open progress</a>`
          : '';
        const cancel = ['queued', 'running'].includes(String(job.status || ''))
          ? `<button class="btn btn-secondary" data-cancel-schedule="${escHtml(job.id)}">Cancel</button>`
          : '';
        const error = job.error ? `<div class="designer-schedule-item-meta">${escHtml(job.error)}</div>` : '';
        return `
          <div class="designer-schedule-item">
            <div>
              <div><span class="designer-schedule-status ${escHtml(job.status)}">${escHtml(job.status)}</span> ${escHtml(names)}</div>
              <div class="designer-schedule-item-meta">${escHtml(job.stage_mode === 'separate_runs' ? 'Each stage as its own run' : 'One combined run')} | ${escHtml(formatScheduleWhen(job))}${runLink}</div>
              ${error}
            </div>
            ${cancel}
          </div>
        `;
      }).join('')}
    </div>
  `;
}

async function loadScheduledJobs() {
  try {
    const response = await fetch('/api/pipeline/schedule', { cache: 'no-store' });
    const data = await response.json();
    if (!response.ok || data.error) {
      return;
    }
    renderScheduledJobs(data.jobs || []);
  } catch (_error) {
    // Keep the designer usable if the scheduler endpoint is temporarily unavailable.
  }
}

async function cancelScheduledJob(jobId) {
  if (!jobId) {
    return;
  }
  const response = await fetch(`/api/pipeline/schedule/${encodeURIComponent(jobId)}/cancel`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
  const data = await response.json();
  if (!response.ok || data.error) {
    alert(data.error || 'Failed to cancel scheduled job.');
    return;
  }
  await loadScheduledJobs();
}

async function submitScheduledPipeline() {
  if (!designerState.pipelineStages.length) {
    alert('Add at least one stage to the pipeline first.');
    return;
  }
  if (!requirePipelineType() || !assertHomogeneousPipelineStages()) {
    return;
  }
  if (!requireSavedPipelineSnapshot()) {
    return;
  }
  const pipelineBaseName = requirePipelineBaseName();
  if (!pipelineBaseName) {
    return;
  }
  const afterCurrent = !!$('designer-schedule-after-current')?.checked;
  const atTime = !!$('designer-schedule-at-time')?.checked;
  const datetimeValue = $('designer-schedule-datetime')?.value || '';
  if (!afterCurrent && !atTime) {
    alert('Choose “after the current migration finishes”, a start time, or both.');
    return;
  }
  let runAt = '';
  if (atTime) {
    if (!datetimeValue) {
      alert('Choose a start date and time.');
      return;
    }
    const parsed = new Date(datetimeValue);
    if (Number.isNaN(parsed.getTime())) {
      alert('Scheduled time is invalid.');
      return;
    }
    runAt = parsed.toISOString();
  }
  const startFrom = Number($('designer-schedule-start-from')?.value || 0);
  const stageMode = $('designer-schedule-stage-mode')?.value || 'single_run';
  const isFlatFilePipeline = getSelectedPipelineType() === 'flat_file';
  if (isFlatFilePipeline && getSelectedStartStage() === 'extract') {
    setSelectedStartStage('transform');
  }
  const loadBatchSize = _askLoadBatchSize();
  if (loadBatchSize == null) {
    return;
  }
  showPipelinePreparation('Queueing schedule...', 'Saving this pipeline to start later.');
  try {
    await syncPipelineStagesFromTemplates();
    const payload = {
      migration_name: `${pipelineBaseName} - Migration`,
      snapshot_id: designerState.savedSnapshotId || '',
      fresh: true,
      dry_run: false,
      mask_emails: _getMaskEmailsValue(),
      remove_email_mask: _getRemoveEmailMaskValue(),
      stop_after_transform: _getStopAfterTransformValue(),
      load_batch_size: loadBatchSize,
      target_domain: designerState.pipelineStages?.[0]?.target_domain || designerState.pipelineStages?.[0]?.targetDomain || '',
      start_stage: isFlatFilePipeline ? 'transform' : getSelectedStartStage(),
      source_profile: getSelectedSourceProfile(),
      target_profile: getSelectedTargetProfile(),
      pipelines: designerState.pipelineStages.map(stage => buildPipelineRequest(stage)),
    };
    const response = await fetch('/api/pipeline/schedule', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        after_current: afterCurrent,
        run_at: runAt,
        stage_mode: stageMode,
        start_from_index: startFrom,
        label: pipelineBaseName,
        snapshot_id: designerState.savedSnapshotId || '',
        payload,
      }),
    });
    const data = await response.json();
    if (!response.ok || data.error) {
      throw new Error(data.error || 'Failed to schedule pipeline.');
    }
    closeScheduleModal();
    hidePipelinePreparation();
    await loadScheduledJobs();
    alert('Scheduled. Keep this UI running; the job waits for the chosen time and for any current migration to finish.');
  } catch (error) {
    hidePipelinePreparation();
    alert(error.message || 'Failed to schedule pipeline.');
  }
}

function buildPipelineValidationRequest() {
  const pipelineBaseName = getPipelineBaseName();
  return {
    pipeline_name: pipelineBaseName,
    pipeline_id: slugifyPipelineId(pipelineBaseName || 'pipeline_validation'),
    snapshot_id: designerState.savedSnapshotId || '',
    stages: designerState.pipelineStages.map(stage => ({
      pipeline_id: stage.pipelineId,
      pipeline_name: stage.pipelineName,
      template_id: stage.inputMode === 'flat_file'
        ? (stage.flatFileTemplateId || String(stage.templateId).replace(/^flat:/, ''))
        : stage.templateId,
      input_mode: normalizeInputMode(stage.inputMode),
      object_names: getStageObjectNames(stage),
    })),
  };
}

async function startValidationPipelineRun() {
  if (!designerState.pipelineStages.length) {
    alert('Add at least one stage to the pipeline first.');
    return;
  }
  if (!requirePipelineType() || !assertHomogeneousPipelineStages()) {
    return;
  }
  if (!requireSavedPipelineSnapshot()) {
    return;
  }
  const pipelineBaseName = requirePipelineBaseName();
  if (!pipelineBaseName) {
    return;
  }
  showPipelinePreparation('Preparing validation pipeline...', 'Refreshing stage templates and ordered validation sequence.');
  try {
    await syncPipelineStagesFromTemplates();
  } catch (error) {
    hidePipelinePreparation();
    alert(error.message || 'Failed to prepare pipeline stages.');
    return;
  }
  try {
    const validationRequest = buildPipelineValidationRequest();
    sessionStorage.setItem('sf_etl_pipeline_prevalidation_request', JSON.stringify(validationRequest));
    const firstTemplate = validationRequest.stages.find(stage => stage.input_mode === 'flat_file')?.template_id || '';
    const query = new URLSearchParams({ phase: 'pre', pipeline_config: '1' });
    if (firstTemplate) query.set('template_id', firstTemplate);
    updatePipelinePreparation('Opening validation configuration...', 'Choose the checks before starting the pipeline.');
    window.location.assign(`/validation?${query.toString()}`);
  } catch (error) {
    hidePipelinePreparation();
    alert(error.message || 'Failed to start validation pipeline.');
  }
}

async function skipValidationPipeline() {
  if (!designerState.pipelineStages.length) {
    alert('Add at least one stage to the pipeline first.');
    return;
  }
  if (!requirePipelineType() || !assertHomogeneousPipelineStages()) {
    return;
  }
  if (!requireSavedPipelineSnapshot()) {
    return;
  }
  const pipelineBaseName = requirePipelineBaseName();
  if (!pipelineBaseName) {
    return;
  }
  if (!window.confirm('Skip pre-validation for all stages in this pipeline and allow Start Migration?')) {
    return;
  }
  showPipelinePreparation('Skipping pre-validation...', 'Marking all stages as bypassed.');
  try {
    await syncPipelineStagesFromTemplates();
    const response = await fetch('/api/pipeline/skip-prevalidation', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildPipelineValidationRequest()),
    });
    const data = await response.json();
    if (!response.ok || data.error) {
      throw new Error(data.error || 'Failed to skip pre-validation.');
    }
    claimPipelineName(pipelineBaseName);
    await syncPipelineStagesFromTemplates();
    hidePipelinePreparation();
    renderPipelineFlow();
    alert('Pre-validation skipped. Start Migration is now enabled.');
  } catch (error) {
    hidePipelinePreparation();
    alert(error.message || 'Failed to skip pre-validation.');
  }
}

function openPostValidation() {
  if (!requirePipelineType() || !assertHomogeneousPipelineStages()) {
    return;
  }
  if (!requirePipelineBaseName()) {
    return;
  }
  const runKey = designerState.runStatus?.runner?.run_key || '';
  const params = new URLSearchParams({ phase: 'post' });
  if (runKey) {
    params.set('run_key', runKey);
  }
  window.location.href = `/validation?${params.toString()}`;
}

function showPipelinePreparation(title, detail) {
  updatePipelinePreparation(title, detail);
  $('designer-launch-loader').hidden = false;
}

function updatePipelinePreparation(title, detail) {
  $('designer-launch-loader-title').textContent = title;
  $('designer-launch-loader-detail').textContent = detail;
}

function hidePipelinePreparation() {
  $('designer-launch-loader').hidden = true;
}

async function resumePipelineFromStage(index) {
  await syncPipelineStagesFromTemplates();
  designerState.suppressStoredRunStatus = false;
  const stage = designerState.pipelineStages[index];
  if (!stage) {
    return;
  }
  if (designerState.runStatus.runner?.running) {
    alert('A pipeline run is already in progress.');
    return;
  }
  if (!window.confirm(`Resume the pipeline from "${stage.pipelineName}" and continue downstream stages? Earlier completed stages will be skipped.`)) {
    return;
  }

  const pipelineBaseName = requirePipelineBaseName();
  if (!pipelineBaseName) {
    return;
  }

  const payload = {
    migration_name: `${pipelineBaseName} - Migration`,
    fresh: false,
    dry_run: false,
    from_pipeline: stage.pipelineId,
    start_stage: getSelectedStartStage(),
    source_profile: getSelectedSourceProfile(),
    target_profile: getSelectedTargetProfile(),
    snapshot_id: designerState.savedSnapshotId || '',
    pipelines: designerState.pipelineStages.map(stageEntry => buildPipelineRequest(stageEntry)),
  };

  const response = await fetch('/api/pipeline/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok || data.error) {
    alert(data.error || 'Failed to resume pipeline.');
    return;
  }

  if (data.runner) {
    designerState.runStatus.runner = data.runner;
  }
  await loadRunStatus();
  await loadPipelineLogs();
  renderStatusBanner();
  renderPipelineFlow();
  alert(`Pipeline resumed from "${stage.pipelineName}" starting at ${getSelectedStartStage()}.`);
}

async function stopPipelineRun() {
  if (!designerState.runStatus.runner?.running) {
    alert('No pipeline run is active right now.');
    return;
  }
  if (!window.confirm('Stop the running pipeline?')) {
    return;
  }

  const response = await fetch('/api/pipeline/stop', {
    method: 'POST',
  });
  const data = await response.json();
  if (!response.ok || data.error) {
    alert(data.error || 'Failed to stop pipeline.');
    return;
  }

  if (data.runner) {
    designerState.runStatus.runner = data.runner;
  }
  await loadRunStatus();
  await loadPipelineLogs();
  alert('Pipeline stop requested.');
}

async function loadPipelineLogs(showError = false) {
  try {
    const runKey = designerState.runStatus.runner?.run_key || '';
    const query = runKey ? `?run_key=${encodeURIComponent(runKey)}` : '';
    const response = await fetch(`/api/pipeline/logs${query}`);
    const data = await response.json();
    if (!response.ok || data.error) {
      throw new Error(data.error || 'Failed to load pipeline logs');
    }
    const output = $('designer-log-output');
    const meta = $('designer-log-meta');
    output.textContent = data.content || 'No logs yet.';
    meta.textContent = `${data.log_path || ''}${data.size != null ? ` | ${data.size} bytes` : ''}${data.truncated ? ' | showing latest tail' : ''}`;
    output.scrollTop = output.scrollHeight;
  } catch (e) {
    if (showError) {
      alert(e.message);
    }
  }
}

async function clearPipelineLogs() {
  if (!window.confirm('Clear the pipeline log file?')) {
    return;
  }
  const runKey = designerState.runStatus.runner?.run_key || '';
  const query = runKey ? `?run_key=${encodeURIComponent(runKey)}` : '';
  const response = await fetch(`/api/pipeline/logs${query}`, {
    method: 'DELETE',
  });
  const data = await response.json();
  if (!response.ok || data.error) {
    alert(data.error || 'Failed to clear pipeline logs.');
    return;
  }
  await loadPipelineLogs(true);
}

async function loadFailedFiles(showError = false) {
  renderFailedPipelineOptions();
  const pipelineId = $('designer-failed-pipeline')?.value || designerState.failedFiles.pipelineId || '';
  const wrap = $('designer-failed-files');
  const meta = $('designer-failed-meta');
  if (!wrap || !meta) {
    return;
  }
  if (!pipelineId) {
    designerState.failedFiles = { pipelineId: '', runName: '', directories: [], files: [], selectedPaths: [] };
    wrap.innerHTML = '<div class="designer-failed-empty">No pipeline available yet.</div>';
    meta.textContent = 'Pick a pipeline to see failed and blocked load CSV files from the latest log run.';
    updateFailedSelectionSummary();
    return;
  }

  try {
    const response = await fetch(`/api/pipeline/failed-files?pipeline_id=${encodeURIComponent(pipelineId)}`);
    const data = await response.json();
    if (!response.ok || data.error) {
      throw new Error(data.error || 'Failed to load failed files');
    }
    designerState.failedFiles = {
      pipelineId,
      runName: data.run_name || '',
      directories: Array.isArray(data.directories) ? data.directories : [],
      files: Array.isArray(data.files) ? data.files : [],
      selectedPaths: pipelineId === designerState.failedFiles.pipelineId
        ? (designerState.failedFiles.selectedPaths || [])
        : [],
    };
    syncSelectedFailedPaths();
    meta.textContent = data.run_name
      ? `${pipelineId} | Latest log run ${data.run_name}${designerState.failedFiles.directories.length ? ` | ${designerState.failedFiles.directories.join(' | ')}` : ''}`
      : `${pipelineId} | No log run found yet`;
    renderFailedFiles();
  } catch (e) {
    if (showError) {
      alert(e.message);
    }
    designerState.failedFiles = { pipelineId, runName: '', directories: [], files: [], selectedPaths: [] };
    wrap.innerHTML = `<div class="designer-failed-empty">${escHtml(e.message)}</div>`;
    updateFailedSelectionSummary();
  }
}

function renderFailedFiles() {
  const wrap = $('designer-failed-files');
  if (!wrap) {
    return;
  }
  const files = designerState.failedFiles.files || [];
  if (!files.length) {
    wrap.innerHTML = '<div class="designer-failed-empty">No failed or blocked load files found for the selected pipeline in the latest log run.</div>';
    updateFailedSelectionSummary();
    return;
  }

  const selectedPaths = getSelectedFailedPathSet();
  wrap.innerHTML = files.map((file, index) => `
    <label class="designer-failed-item">
      <div class="designer-failed-item-main">
        <input type="checkbox" class="designer-failed-checkbox" data-index="${index}" data-path="${escHtml(file.path || '')}" ${selectedPaths.has(file.path) ? 'checked' : ''} />
        <div>
          <div class="designer-failed-item-name">${escHtml(file.name)}</div>
          <div class="designer-failed-item-meta">Object: ${escHtml(file.object_name || 'Unknown')} | Type: ${escHtml(file.stage || 'load_failed')}</div>
          <div class="designer-failed-item-meta">Updated: ${escHtml(formatTimestamp(file.modified_at))} | Size: ${escHtml(file.size)} bytes</div>
        </div>
      </div>
      <div class="designer-failed-item-meta">${escHtml(file.path || '')}</div>
    </label>
  `).join('');
  wrap.querySelectorAll('.designer-failed-checkbox').forEach(input => {
    input.addEventListener('change', event => {
      toggleFailedFileSelection(event.target.dataset.path, event.target.checked);
    });
  });
  updateFailedSelectionSummary();
}

async function retrySelectedFailedFiles() {
  if (designerState.runStatus.runner?.running) {
    alert('A pipeline run is already in progress.');
    return;
  }
  const selectedPaths = getSelectedFailedPathSet();
  const selected = (designerState.failedFiles.files || [])
    .filter(file => selectedPaths.has(file.path));
  if (!selected.length) {
    alert('Select at least one failed or blocked file to reprocess.');
    return;
  }
  if (!window.confirm(`Reprocess ${selected.length} failed/blocked file(s) for ${designerState.failedFiles.pipelineId}?`)) {
    return;
  }

  const response = await fetch('/api/pipeline/retry-failed', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      pipeline_id: designerState.failedFiles.pipelineId,
      files: selected.map(file => file.path),
    }),
  });
  const data = await response.json();
  if (!response.ok || data.error) {
    alert(data.error || 'Failed to retry selected files.');
    return;
  }

  if (data.runner) {
    designerState.runStatus.runner = data.runner;
  }
  await loadRunStatus();
  await loadPipelineLogs();
  alert(`Started reprocessing ${selected.length} failed/blocked file(s).`);
}

async function previewPipelineConfig() {
  const data = await requestPipelineConfig(false);
  if (!data) {
    return;
  }
  $('designer-yaml-output').textContent = data.yaml;
  $('designer-yaml-modal').style.display = 'flex';
}

async function savePipelineConfig() {
  const data = await requestPipelineConfig(true);
  if (!data) {
    return;
  }
  alert(`Saved pipeline config with ${designerState.pipelineStages.length} stage(s) to config/migration.yaml`);
}

function persistDesignerState() {
  getDesignerStorageArea().setItem(getDesignerStorageKey(), JSON.stringify(getDesignerSnapshotPayload()));
}

function openTransformManager(stageIndex) {
  const stage = designerState.pipelineStages[stageIndex];
  if (!stage) {
    return;
  }
  designerState.activeTransformStageIndex = stageIndex;
  renderTransformManager();
  $('designer-transform-modal').style.display = 'flex';
}

function closeTransformManager() {
  designerState.activeTransformStageIndex = null;
  $('designer-transform-modal').style.display = 'none';
}

function renderTransformManager() {
  const stageIndex = designerState.activeTransformStageIndex;
  const stage = stageIndex == null ? null : designerState.pipelineStages[stageIndex];
  const title = $('designer-transform-title');
  const subtitle = $('designer-transform-subtitle');
  const content = $('designer-transform-content');
  if (!title || !subtitle || !content) {
    return;
  }
  if (!stage) {
    title.textContent = 'Stage Transforms';
    subtitle.textContent = 'Manage field transformations for the selected stage.';
    content.innerHTML = '<div class="designer-transform-empty">No stage selected.</div>';
    return;
  }
  title.textContent = `Stage Transforms - ${stage.pipelineName}`;
  subtitle.textContent = `Stage ID: ${stage.pipelineId}`;
  content.innerHTML = renderStageTransforms(stage, stageIndex);
}

function addObjectTransformRule(stageIndex, objectName) {
  const stage = designerState.pipelineStages[stageIndex];
  if (!stage || !objectName) {
    return;
  }
  stage.workspace = stage.workspace || {};
  stage.workspace.objectTransforms = stage.workspace.objectTransforms || {};
  const fields = getObjectFieldsForStage(stage, objectName);
  stage.workspace.objectTransforms[objectName] = [
    ...(stage.workspace.objectTransforms[objectName] || []),
    { type: 'trim', field: fields[0] || '' },
  ];
  persistDesignerState();
  renderPipelineFlow();
  renderTransformManager();
}

function updateObjectTransformRule(stageIndex, objectName, ruleIndex, key, value, rerender = true) {
  const rule = designerState.pipelineStages?.[stageIndex]?.workspace?.objectTransforms?.[objectName]?.[ruleIndex];
  if (!rule) {
    return;
  }
  rule[key] = value;
  persistDesignerState();
  if (rerender) {
    renderTransformManager();
  }
}

function changeObjectTransformType(stageIndex, objectName, ruleIndex, nextType) {
  const rule = designerState.pipelineStages?.[stageIndex]?.workspace?.objectTransforms?.[objectName]?.[ruleIndex];
  if (!rule) {
    return;
  }
  const field = rule.field || '';
  const nextRule = { type: nextType, field };
  if (nextType === 'anonymize_email') {
    nextRule.domain = 'masked.example';
    nextRule.mode = 'hash';
  } else if (nextType === 'null_default') {
    nextRule.default = '0';
  } else if (nextType === 'text_to_decimal') {
    nextRule.invalid_value = 'null';
    nextRule.decimal_places = 2;
  } else if (nextType === 'boolean_convert') {
    nextRule.direction = 'number_to_boolean';
  } else if (nextType === 'date_convert') {
    nextRule.source_format = 'mdy2';
    nextRule.invalid_value = 'retain';
  }
  designerState.pipelineStages[stageIndex].workspace.objectTransforms[objectName][ruleIndex] = nextRule;
  persistDesignerState();
  renderPipelineFlow();
  renderTransformManager();
}

function changeEmailMaskMode(stageIndex, objectName, ruleIndex, nextMode) {
  const rule = designerState.pipelineStages?.[stageIndex]?.workspace?.objectTransforms?.[objectName]?.[ruleIndex];
  if (!rule || rule.type !== 'anonymize_email') {
    return;
  }
  const nextRule = { type: 'anonymize_email', field: rule.field || '', mode: nextMode };
  if (nextMode === 'hash') {
    nextRule.domain = rule.domain || 'masked.example';
  } else if (nextMode === 'append_suffix') {
    nextRule.suffix = rule.suffix || 'mask';
  } else if (nextMode === 'replace_domain') {
    nextRule.domain = rule.domain || 'mask.com';
  } else if (nextMode === 'fixed_local') {
    nextRule.local_part = rule.local_part || 'maskedemail';
  } else if (nextMode === 'fixed_full') {
    nextRule.local_part = rule.local_part || 'maskedemail';
    nextRule.domain = rule.domain || 'mask.com';
  }
  designerState.pipelineStages[stageIndex].workspace.objectTransforms[objectName][ruleIndex] = nextRule;
  persistDesignerState();
  renderTransformManager();
}

function removeObjectTransformRule(stageIndex, objectName, ruleIndex) {
  const rules = designerState.pipelineStages?.[stageIndex]?.workspace?.objectTransforms?.[objectName];
  if (!Array.isArray(rules)) {
    return;
  }
  rules.splice(ruleIndex, 1);
  if (!rules.length) {
    delete designerState.pipelineStages[stageIndex].workspace.objectTransforms[objectName];
  }
  persistDesignerState();
  renderPipelineFlow();
  renderTransformManager();
}

function restoreDesignerState() {
  try {
    const raw = getDesignerStorageArea().getItem(getDesignerStorageKey());
    if (!raw) {
      applyPipelineTypeUi();
      return;
    }
    const payload = JSON.parse(raw);
    designerState.pipelineStages = Array.isArray(payload.pipelineStages) ? payload.pipelineStages : [];
    if ($('designer-migration-name')) {
      $('designer-migration-name').value = payload.migrationName || '';
    }
    designerState.savedSnapshotId = payload.savedSnapshotId || '';
    designerState.savedSnapshotFingerprint = payload.savedSnapshotFingerprint || '';
    designerState.claimedPipelineName = '';
    const restoredType = normalizePipelineType(payload.pipelineType) || inferPipelineTypeFromStages(designerState.pipelineStages);
    setPipelineType(restoredType, { force: true, silent: true });
    if ($('designer-start-stage')) {
      $('designer-start-stage').value = payload.startStage || 'extract';
    }
    designerState.sourceProfile = payload.sourceProfile || 'default';
    designerState.targetProfile = payload.targetProfile || 'default';
    if (payload.runStatus?.runner) {
      designerState.runStatus.runner = {
        ...designerState.runStatus.runner,
        ...payload.runStatus.runner,
        launching: false,
      };
    }
    designerState.failedFiles.pipelineId = payload.failedFiles?.pipelineId || '';
    designerState.failedFiles.selectedPaths = Array.isArray(payload.failedFiles?.selectedPaths)
      ? payload.failedFiles.selectedPaths
      : [];
    designerState.panels = {
      failedCollapsed: !!payload.panels?.failedCollapsed,
      logsCollapsed: !!payload.panels?.logsCollapsed,
    };
  } catch (_error) {
    designerState.pipelineStages = [];
    applyPipelineTypeUi();
  }
}

function composeQueryFilter(parts = {}) {
  const clauses = [];
  const whereClause = (parts.whereClause || '').trim();
  const orderBy = (parts.orderBy || '').trim();
  const limit = normalizeNumericClause(parts.limit);
  const offset = normalizeNumericClause(parts.offset);
  if (whereClause) {
    clauses.push(`WHERE ${whereClause}`);
  }
  if (orderBy) {
    clauses.push(`ORDER BY ${orderBy}`);
  }
  if (limit) {
    clauses.push(`LIMIT ${limit}`);
  }
  if (offset) {
    clauses.push(`OFFSET ${offset}`);
  }
  return clauses.join(' ');
}

function normalizeNumericClause(value) {
  const text = `${value ?? ''}`.trim();
  if (!text) {
    return '';
  }
  const num = Number.parseInt(text, 10);
  return Number.isFinite(num) && num >= 0 ? String(num) : '';
}

function formatTimestamp(value) {
  if (!value) {
    return 'No updates yet';
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function escHtml(value) {
  return String(value || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function escJs(value) {
  return String(value || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'");
}
