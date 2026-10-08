const state = {
  org: 'source',
  orgProfile: 'default',
  orgProfiles: { source: [], target: [] },
  allObjects: [],
  savedTemplates: [],
  nodes: {},
  rootId: null,
  selectedNodeId: null,
  nodeDirection: {},
  objectCache: {},
  nodeSelections: {},
  maxDepth: Number.POSITIVE_INFINITY,
  currentTemplateId: null,
  currentTemplateName: '',
  topologyView: {
    mode: 'minimal',
    radialScope: 'children',
    fontScale: 1,
    nodeFillFilter: 'all',
    groupDuplicates: false,
    showParents: true,
    showChildren: true,
    minLevel: 0,
    maxLevel: 6,
    focusNodeIds: [],
  },
  treeView: {
    showParents: true,
    showChildren: true,
  },
  objectExternalIds: {},
  externalIdDraft: {},
  pipelineStages: [],
  runStatus: {
    migration: null,
    started_at: null,
    last_updated: null,
    pipelines: {},
  },
};

let addModalContext = null;
let pipelineStatusRefreshHandle = null;

const $ = id => document.getElementById(id);

function bindIfPresent(id, eventName, handler) {
  const el = $(id);
  if (el) {
    el.addEventListener(eventName, handler);
  }
}

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

function compactInstanceLabel(instanceUrl) {
  const host = String(instanceUrl || '').replace(/^https?:\/\//, '').trim();
  if (!host) {
    return 'Unknown org';
  }
  const parts = host.split('.');
  return parts.slice(0, 2).join('.');
}

function authModeLabel(value) {
  const normalized = String(value || '').trim().toLowerCase();
  if (normalized === 'session_id') return 'session';
  if (normalized === 'jwt') return 'jwt';
  if (normalized === 'client_credentials') return 'client_credentials';
  return 'unknown';
}

document.addEventListener('DOMContentLoaded', () => {
  initTopbarMenus();
  loadOrgProfiles().then(async () => {
    await checkAuth();
    await loadObjects();
  });
  loadTemplates();
  initPanelResizer();

  bindIfPresent('org-select', 'change', () => {
    state.org = $('org-select').value;
    renderOrgProfileOptions();
    resetWorkspace();
    checkAuth();
    loadObjects();
    loadTemplates();
  });
  bindIfPresent('org-profile-select', 'change', () => {
    state.orgProfile = $('org-profile-select').value || 'default';
    resetWorkspace();
    checkAuth();
    loadObjects();
    loadTemplates();
  });
  bindIfPresent('template-select', 'change', e => {
    if (e.target.value) {
      loadTemplate(e.target.value);
    }
  });
  bindIfPresent('btn-manage-templates', 'click', openTemplateManager);
  bindIfPresent('btn-refresh-metadata', 'click', refreshMetadataCache);
  bindIfPresent('btn-add-current-pipeline', 'click', addCurrentTemplateToPipeline);
  bindIfPresent('btn-refresh-pipeline-status', 'click', () => loadRunStatus(true));
  bindIfPresent('btn-clear-pipeline', 'click', clearPipelineStages);
  bindIfPresent('btn-external-ids', 'click', openExternalIdModal);
  bindIfPresent('btn-save-template', 'click', saveTemplate);
  bindIfPresent('btn-save-template-as', 'click', saveTemplateAs);
  bindIfPresent('tree-show-parents', 'change', e => {
    state.treeView.showParents = e.target.checked;
    renderTree();
  });
  bindIfPresent('tree-show-children', 'change', e => {
    state.treeView.showChildren = e.target.checked;
    renderTree();
  });
  bindIfPresent('tree-add-parent-top', 'click', () => openSelectedNodeAddModal('parents'));
  bindIfPresent('tree-add-child-top', 'click', () => openSelectedNodeAddModal('children'));
  bindIfPresent('search-objects', 'input', e => filterObjects(e.target.value));
  bindIfPresent('btn-generate', 'click', generateConfig);
  bindIfPresent('btn-generate-bottom', 'click', generateConfig);
  bindIfPresent('btn-topology', 'click', openTopologyModal);
  bindIfPresent('modal-close', 'click', () => $('modal-overlay').style.display = 'none');
  bindIfPresent('topology-close', 'click', closeTopologyModal);
  bindIfPresent('topology-show-children', 'change', e => {
    state.topologyView.showChildren = e.target.checked;
    if ($('topology-modal').style.display === 'flex') {
      renderTopologyGraph();
    }
  });
  bindIfPresent('topology-show-parents', 'change', e => {
    state.topologyView.showParents = e.target.checked;
    if ($('topology-modal').style.display === 'flex') {
      renderTopologyGraph();
    }
  });
  bindIfPresent('topology-min-level', 'input', e => {
    state.topologyView.minLevel = normalizeTopologyLevel(e.target.value, 0);
    e.target.value = state.topologyView.minLevel;
    if (state.topologyView.maxLevel < state.topologyView.minLevel) {
      state.topologyView.maxLevel = state.topologyView.minLevel;
      $('topology-max-level').value = state.topologyView.maxLevel;
    }
    if ($('topology-modal').style.display === 'flex') {
      renderTopologyGraph();
    }
  });
  bindIfPresent('topology-max-level', 'input', e => {
    state.topologyView.maxLevel = normalizeTopologyLevel(e.target.value, state.topologyView.minLevel);
    e.target.value = state.topologyView.maxLevel;
    if ($('topology-modal').style.display === 'flex') {
      renderTopologyGraph();
    }
  });
  bindIfPresent('topology-clear-focus', 'click', () => {
    state.topologyView.focusNodeIds = [];
    if ($('topology-modal').style.display === 'flex') {
      renderTopologyGraph();
    }
  });
  bindIfPresent('topology-layout-mode', 'change', e => {
    state.topologyView.mode = e.target.value || 'minimal';
    if ($('topology-modal').style.display === 'flex') {
      renderTopologyGraph();
    }
  });
  bindIfPresent('topology-radial-scope', 'change', e => {
    state.topologyView.radialScope = e.target.value || 'children';
    if ($('topology-modal').style.display === 'flex') {
      renderTopologyGraph();
    }
  });
  bindIfPresent('topology-font-scale', 'input', e => {
    state.topologyView.fontScale = Math.max(0.5, Math.min(1, (Number.parseInt(e.target.value, 10) || 100) / 100));
    if ($('topology-modal').style.display === 'flex') {
      renderTopologyGraph();
    }
  });
  bindIfPresent('topology-node-fill-filter', 'change', e => {
    state.topologyView.nodeFillFilter = e.target.value || 'all';
    if ($('topology-modal').style.display === 'flex') {
      renderTopologyGraph();
    }
  });
  bindIfPresent('topology-group-duplicates', 'change', e => {
    state.topologyView.groupDuplicates = !!e.target.checked;
    if ($('topology-modal').style.display === 'flex') {
      renderTopologyGraph();
    }
  });
  bindIfPresent('topology-expand', 'click', toggleTopologyWidth);
  bindIfPresent('external-id-close', 'click', closeExternalIdModal);
  bindIfPresent('external-id-apply-defaults', 'click', applySuggestedExternalIds);
  bindIfPresent('external-id-save', 'click', saveExternalIdMappings);
  bindIfPresent('external-id-unmapped-only', 'change', renderExternalIdTable);
  bindIfPresent('template-manager-close', 'click', closeTemplateManager);
  bindIfPresent('btn-copy', 'click', () => navigator.clipboard.writeText($('yaml-output').textContent));
  bindIfPresent('btn-save', 'click', saveConfig);
  bindIfPresent('fh-filter', 'input', e => updateNodeFilter(e.target.value));
  bindIfPresent('fh-order', 'input', e => updateNodeQueryPart('orderBy', e.target.value));
  bindIfPresent('fh-limit', 'input', e => updateNodeQueryPart('limit', e.target.value));
  bindIfPresent('fh-offset', 'input', e => updateNodeQueryPart('offset', e.target.value));
  bindIfPresent('fh-extid', 'input', e => updateNodeExtId(e.target.value));
  bindIfPresent('seed-filter', 'input', e => updateSeedFilter(e.target.value));
  bindIfPresent('seed-order', 'input', e => updateSeedQueryPart('orderBy', e.target.value));
  bindIfPresent('seed-limit', 'input', e => updateSeedQueryPart('limit', e.target.value));
  bindIfPresent('seed-offset', 'input', e => updateSeedQueryPart('offset', e.target.value));
  renderPipelineBoard();
  loadRunStatus();
  pipelineStatusRefreshHandle = window.setInterval(() => loadRunStatus(), 15000);
  loadTemplateFromUrl();
});

async function checkAuth() {
  const el = $('auth-status');
  try {
    const data = await fetch(`/api/auth/check?org=${state.org}&profile=${encodeURIComponent(state.orgProfile)}`).then(r => r.json());
    if (data.status === 'ok') {
      el.className = 'auth-status ok';
      const profileLabel = state.orgProfile === 'default' ? 'Default' : state.orgProfile;
      el.innerHTML = `<span class="dot"></span> Connected - ${escHtml(compactInstanceLabel(data.instance_url))} | ${escHtml(state.org)}:${escHtml(profileLabel)} | auth:${escHtml(authModeLabel(data.auth_mode))}`;
    } else {
      throw new Error(data.message || 'Auth failed');
    }
  } catch (e) {
    el.className = 'auth-status error';
    el.innerHTML = '<span class="dot"></span> Auth failed';
  }
}

async function loadOrgProfiles() {
  try {
    const data = await fetch('/api/org-profiles').then(r => r.json());
    state.orgProfiles = {
      source: Array.isArray(data.source) ? data.source : [],
      target: Array.isArray(data.target) ? data.target : [],
    };
  } catch (_e) {
    state.orgProfiles = { source: [{ id: 'default', label: 'Default' }], target: [{ id: 'default', label: 'Default' }] };
  }
  renderOrgProfileOptions();
}

function renderOrgProfileOptions() {
  const select = $('org-profile-select');
  if (!select) {
    return;
  }
  const profiles = state.orgProfiles[state.org] || [];
  const available = profiles.length ? profiles : [{ id: 'default', label: 'Default' }];
  const selected = available.some(profile => profile.id === state.orgProfile) ? state.orgProfile : available[0].id;
  state.orgProfile = selected;
  select.innerHTML = available
    .map(profile => `<option value="${escHtml(profile.id)}" ${profile.id === selected ? 'selected' : ''}>${escHtml(profile.label)}</option>`)
    .join('');
}

async function loadObjects() {
  $('object-list').innerHTML = '<div class="loading"><span class="spinner"></span> Loading...</div>';
  try {
    const data = await fetch(`/api/objects?org=${state.org}&profile=${encodeURIComponent(state.orgProfile)}`).then(r => r.json());
    if (data.error) {
      throw new Error(data.error);
    }
    state.allObjects = data.objects;
    $('object-count').textContent = data.objects.length;
    renderObjectList(data.objects);
  } catch (e) {
    $('object-list').innerHTML = `<div class="loading" style="color:var(--red)">Error: ${escHtml(e.message)}</div>`;
  }
}

function renderObjectList(objects) {
  $('object-list').innerHTML = objects.map(o => `
    <div class="object-item" onclick="selectSeed('${escJs(o.name)}','${escJs(o.label)}')">
      <span class="obj-name">${escHtml(o.name)}</span>
      <span class="obj-label">${escHtml(o.label)}</span>
    </div>`).join('');
}

function filterObjects(q) {
  const lq = q.toLowerCase();
  renderObjectList(state.allObjects.filter(o =>
    o.name.toLowerCase().includes(lq) || o.label.toLowerCase().includes(lq)));
}

async function selectSeed(objectName, label) {
  state.nodes = {};
  state.rootId = null;
  state.selectedNodeId = null;
  state.nodeDirection = {};
  state.nodeSelections = {};
  state.objectExternalIds = {};
  state.externalIdDraft = {};
  state.treeView.showParents = true;
  state.treeView.showChildren = true;

  const data = await fetchObjectData(objectName);
  if (!data) {
    return;
  }

  const nodeId = 'n0';
  state.nodes[nodeId] = {
    id: nodeId,
    object: objectName,
    label,
    parentId: null,
    direction: 'seed',
    via_field: null,
    depth: 0,
    childIds: [],
  };
  state.rootId = nodeId;
  state.nodeSelections[nodeId] = buildDefaultSelection(data);
  state.nodeSelections[nodeId].externalId = getObjectExternalId(objectName);

  $('tree-title').textContent = objectName;
  $('object-browser').style.display = 'none';
  $('tree-panel').style.display = '';
  $('bottombar').style.display = 'block';
  $('btn-generate').disabled = false;
  $('btn-topology').disabled = false;
  $('btn-external-ids').disabled = false;
  syncTreeToolbar();

  renderTree();
  selectNode(nodeId);
}

function changeSeed() {
  clearCurrentTemplate();
  $('object-browser').style.display = '';
  $('tree-panel').style.display = 'none';
  $('fields-detail').style.display = 'none';
  $('fields-empty').style.display = '';
  $('bottombar').style.display = 'none';
  $('btn-generate').disabled = true;
  $('btn-topology').disabled = true;
  $('btn-external-ids').disabled = true;
  closeTopologyModal();
  closeExternalIdModal();
  renderPipelineBoard();
}

function resetWorkspace() {
  clearCurrentTemplate();
  state.nodes = {};
  state.rootId = null;
  state.selectedNodeId = null;
  state.nodeDirection = {};
  state.nodeSelections = {};
  state.objectExternalIds = {};
  state.externalIdDraft = {};
  state.treeView.showParents = true;
  state.treeView.showChildren = true;
  $('tree-title').textContent = 'Migration Tree';
  $('object-browser').style.display = '';
  $('tree-panel').style.display = 'none';
  $('fields-detail').style.display = 'none';
  $('fields-empty').style.display = '';
  $('bottombar').style.display = 'none';
  $('btn-generate').disabled = true;
  $('btn-topology').disabled = true;
  $('btn-external-ids').disabled = true;
  syncTreeToolbar();
  $('sel-all-fields').checked = false;
  $('sel-all-lookups').checked = false;
  $('sel-all-others').checked = false;
  closeTopologyModal();
  closeExternalIdModal();
  renderPipelineBoard();
}

function clearCurrentTemplate() {
  state.currentTemplateId = null;
  state.currentTemplateName = '';
  const select = $('template-select');
  if (select) {
    select.value = '';
  }
  const btn = $('btn-save-template');
  if (btn) {
    btn.textContent = 'Save Template';
  }
  renderPipelineBoard();
}

function setCurrentTemplate(templateId, templateName) {
  state.currentTemplateId = templateId || null;
  state.currentTemplateName = templateName || '';
  const select = $('template-select');
  if (select && templateId) {
    select.value = templateId;
  }
  const btn = $('btn-save-template');
  if (btn) {
    btn.textContent = templateId ? 'Update Template' : 'Save Template';
  }
  renderPipelineBoard();
}

async function fetchObjectData(objectName, forceRefresh = false) {
  const key = objectCacheKey(objectName);
  if (!forceRefresh && state.objectCache[key]) {
    return state.objectCache[key];
  }
  try {
    const refreshFlag = forceRefresh ? '&refresh=1' : '';
    const data = await fetch(`/api/object/${objectName}?org=${state.org}&profile=${encodeURIComponent(state.orgProfile)}${refreshFlag}`).then(r => r.json());
    if (data.error) {
      throw new Error(data.error);
    }
    state.objectCache[key] = data;
    return data;
  } catch (e) {
    alert(`Failed to load ${objectName}: ${e.message}`);
    return null;
  }
}

async function refreshMetadataCache() {
  const org = state.org || 'source';
  try {
    const response = await fetch('/api/metadata/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ org }),
    });
    const data = await response.json();
    if (!response.ok || data.error) {
      throw new Error(data.error || 'Failed to refresh metadata');
    }

    Object.keys(state.objectCache).forEach(key => {
      if (key.startsWith(`${org}:`)) {
        delete state.objectCache[key];
      }
    });

    await loadObjects();

    const activeObjects = [...new Set(Object.values(state.nodes).map(node => node?.object).filter(Boolean))];
    for (const objectName of activeObjects) {
      await fetchObjectData(objectName, true);
    }

    renderTree();

    if (state.selectedNodeId && state.nodes[state.selectedNodeId]) {
      const currentNode = state.nodes[state.selectedNodeId];
      const objectData = getCachedObjectData(currentNode.object);
      const selection = getNodeSelection(state.selectedNodeId);
      if (objectData && selection) {
        renderFieldsPanel(objectData, selection);
      } else {
        selectNode(state.selectedNodeId);
      }
    }

    alert(`Metadata refreshed for ${org}.`);
  } catch (e) {
    alert(`Failed to refresh metadata: ${e.message}`);
  }
}

function buildDefaultSelection(data) {
  return {
    selectedFields: new Set(data.required_fields || []),
    queryFilter: '',
    whereClause: '',
    orderBy: '',
    limit: '',
    offset: '',
    externalId: (data.external_id_fields && data.external_id_fields[0]) || '',
  };
}

function normalizeNumericClause(value) {
  const text = `${value ?? ''}`.trim();
  if (!text) {
    return '';
  }
  const num = Number.parseInt(text, 10);
  return Number.isFinite(num) && num >= 0 ? String(num) : '';
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

function findTopLevelKeyword(text, keyword) {
  const source = text || '';
  const upper = source.toUpperCase();
  const target = keyword.toUpperCase();
  let depth = 0;
  let inQuote = false;

  for (let i = 0; i <= source.length - target.length; i += 1) {
    const char = source[i];
    if (char === "'" && (i === 0 || source[i - 1] !== '\\')) {
      inQuote = !inQuote;
      continue;
    }
    if (inQuote) {
      continue;
    }
    if (char === '(') {
      depth += 1;
      continue;
    }
    if (char === ')' && depth > 0) {
      depth -= 1;
      continue;
    }
    if (depth !== 0 || upper.slice(i, i + target.length) !== target) {
      continue;
    }
    const before = i === 0 ? ' ' : upper[i - 1];
    const after = i + target.length >= upper.length ? ' ' : upper[i + target.length];
    if (/\s/.test(before) && /\s/.test(after)) {
      return i;
    }
  }
  return -1;
}

function parseLegacyQueryFilter(value) {
  const text = `${value || ''}`.trim();
  if (!text) {
    return { whereClause: '', orderBy: '', limit: '', offset: '' };
  }

  const whereIdx = findTopLevelKeyword(` ${text} `, ' WHERE ');
  const groupIdx = findTopLevelKeyword(` ${text} `, ' GROUP BY ');
  const havingIdx = findTopLevelKeyword(` ${text} `, ' HAVING ');
  const orderIdx = findTopLevelKeyword(` ${text} `, ' ORDER BY ');
  const limitIdx = findTopLevelKeyword(` ${text} `, ' LIMIT ');
  const offsetIdx = findTopLevelKeyword(` ${text} `, ' OFFSET ');
  const points = [groupIdx, havingIdx, orderIdx, limitIdx, offsetIdx].filter(idx => idx !== -1);
  const firstTailIdx = points.length ? Math.min(...points) : -1;

  let whereClause = '';
  if (whereIdx !== -1) {
    const end = firstTailIdx !== -1 && firstTailIdx > whereIdx ? firstTailIdx : text.length + 1;
    whereClause = text.slice(whereIdx + ' WHERE '.length - 1, end - 1).trim();
  } else if (firstTailIdx > 0) {
    whereClause = text.slice(0, firstTailIdx - 1).trim();
  } else if (groupIdx === -1 && havingIdx === -1) {
    whereClause = text;
  }

  const extractTailValue = (keyword, startIdx, nextIdxs) => {
    if (startIdx === -1) {
      return '';
    }
    const endCandidates = nextIdxs.filter(idx => idx > startIdx);
    const end = endCandidates.length ? Math.min(...endCandidates) : text.length + 1;
    return text.slice(startIdx + keyword.length - 1, end - 1).trim();
  };

  const orderBy = extractTailValue(' ORDER BY ', orderIdx, [limitIdx, offsetIdx]);
  const limit = extractTailValue(' LIMIT ', limitIdx, [offsetIdx]);
  const offset = extractTailValue(' OFFSET ', offsetIdx, []);
  return {
    whereClause,
    orderBy,
    limit: normalizeNumericClause(limit),
    offset: normalizeNumericClause(offset),
  };
}

function getSuggestedExternalIdForObject(objectName) {
  const data = getCachedObjectData(objectName);
  return (data?.external_id_fields && data.external_id_fields[0]) || '';
}

function getExternalIdOptionsForObject(objectName) {
  const data = getCachedObjectData(objectName);
  const options = data?.external_id_fields || [];
  return options;
}

function getObjectExternalId(objectName) {
  return state.objectExternalIds[objectName] || getSuggestedExternalIdForObject(objectName);
}

function hasExplicitExternalId(objectName) {
  return !!(state.objectExternalIds[objectName] && state.objectExternalIds[objectName].trim());
}

function setExternalIdForObject(objectName, value) {
  const normalized = (value || '').trim() || getSuggestedExternalIdForObject(objectName);
  state.objectExternalIds[objectName] = normalized;
  Object.values(state.nodes).forEach(node => {
    if (node.object !== objectName) {
      return;
    }
    const selection = getNodeSelection(node.id);
    if (selection) {
      selection.externalId = normalized;
    }
  });
  const selectedNode = state.nodes[state.selectedNodeId];
  if (selectedNode?.object === objectName) {
    $('fh-extid').value = normalized;
  }
}

function clearExternalIdForObject(objectName) {
  delete state.objectExternalIds[objectName];
  Object.values(state.nodes).forEach(node => {
    if (node.object !== objectName) {
      return;
    }
    const selection = getNodeSelection(node.id);
    if (selection) {
      selection.externalId = getObjectExternalId(objectName);
    }
  });
  const selectedNode = state.nodes[state.selectedNodeId];
  if (selectedNode?.object === objectName) {
    $('fh-extid').value = getObjectExternalId(objectName);
  }
}

function getObjectUsageCount(objectName) {
  return Object.values(state.nodes).filter(node => node.object === objectName).length;
}

function getNodeSelection(nodeId) {
  const node = state.nodes[nodeId];
  if (!node) {
    return null;
  }
  if (!state.nodeSelections[nodeId]) {
    const data = getCachedObjectData(node.object);
    state.nodeSelections[nodeId] = buildDefaultSelection(data || { required_fields: [], external_id_fields: [] });
    state.nodeSelections[nodeId].externalId = getObjectExternalId(node.object);
  }
  return state.nodeSelections[nodeId];
}

function syncSelectionQueryFilter(selection) {
  if (!selection) {
    return;
  }
  selection.queryFilter = composeQueryFilter(selection);
}

function renderTree() {
  const container = $('tree-container');
  container.innerHTML = '';
  syncTreeToolbar();
  if (state.rootId && state.nodes[state.rootId]) {
    renderTreeNode(state.nodes[state.rootId], container, 0);
  }
}

function renderTreeNode(node, container, indent) {
  const data = getCachedObjectData(node.object);
  const dir = state.nodeDirection[node.id] || null;
  const isRoot = node.parentId === null;
  const selected = state.selectedNodeId === node.id;

  const row = document.createElement('div');
  row.className = `tree-node-row ${selected ? 'selected' : ''}`;
  row.onclick = () => selectNode(node.id);

  const indentHTML = buildIndentGuides(indent);

  const icon = isRoot ? '[S]' : node.direction === 'parent' ? '[P]' : '[C]';
  const meta = formatNodeRelation(node);
  const branchColor = getTreeLevelColor(Math.max(indent, 1));
  const wiringStatus = getNodeWiringStatus(node);
  const wiringBadge = wiringStatus
    ? `<span class="tree-node-badge ${wiringStatus.valid ? 'is-valid' : 'is-invalid'}" title="${escHtml(wiringStatus.hint)}">${escHtml(wiringStatus.label)}</span>`
    : '';

  row.innerHTML = `
    <div class="tree-indent">${indentHTML}</div>
    ${!isRoot ? `<span class="tree-branch-connector" style="--branch-color:${branchColor}"></span>` : '<span class="tree-branch-spacer"></span>'}
    <span class="tree-node-icon">${icon}</span>
    <div class="tree-node-info">
      <div class="tree-node-name">${escHtml(node.object)} ${wiringBadge}</div>
      <div class="tree-node-meta">${escHtml(meta)}</div>
    </div>
    ${!isRoot ? `<button class="btn-icon tree-node-close" title="Remove"
      onclick="event.stopPropagation(); removeNode('${escJs(node.id)}')">x</button>` : ''}
  `;
  container.appendChild(row);

  if (node.childIds.length > 0) {
    node.childIds.forEach(cid => {
      if (state.nodes[cid] && isTreeNodeVisible(state.nodes[cid])) {
        renderTreeNode(state.nodes[cid], container, indent + 1);
      }
    });
  }

  if (!data) {
    return;
  }

  const hasLookups = data.fields.some(f => f.is_lookup);
  const hasChildren = data.children.length > 0;
  const toggle = document.createElement('div');
  toggle.className = 'tree-dir-toggle';
  toggle.innerHTML = `
    <div class="tree-indent">${buildIndentGuides(indent + 1)}</div>
    <span class="tree-branch-connector" style="--branch-color:${getTreeLevelColor(indent + 1)}"></span>
    <button class="tree-dir-btn ${dir === 'parents' ? 'active-parent' : ''}"
            ${!hasLookups ? 'disabled' : ''}
            onclick="event.stopPropagation(); toggleDirection('${escJs(node.id)}', 'parents')">
      Parents
    </button>
    <button class="tree-dir-btn ${dir === 'children' ? 'active-children' : ''}"
            ${!hasChildren ? 'disabled' : ''}
            onclick="event.stopPropagation(); toggleDirection('${escJs(node.id)}', 'children')">
      Children
    </button>
  `;
  container.appendChild(toggle);

  if (dir) {
    const addBtn = document.createElement('button');
    addBtn.className = 'tree-add-btn';
    const indentSpan = buildIndentGuides(indent + 1);
    addBtn.innerHTML = `<div class="tree-indent">${indentSpan}</div>
      <span class="tree-branch-connector" style="--branch-color:${getTreeLevelColor(indent + 1)}"></span>
      <span>+ Add ${dir === 'parents' ? 'Parent' : 'Child'}</span>`;
    addBtn.onclick = e => {
      e.stopPropagation();
      openAddModal(node.id, dir);
    };
    container.appendChild(addBtn);
  }
}

function toggleDirection(nodeId, dir) {
  const current = state.nodeDirection[nodeId];
  state.nodeDirection[nodeId] = current === dir ? null : dir;
  renderTree();
}

function formatNodeRelation(node) {
  if (!node) {
    return '';
  }
  if (!node.via_field || node.direction === 'seed' || !node.parentId) {
    return node.label || node.object;
  }

  const parent = state.nodes[node.parentId];
  const parentObject = parent?.object || '?';

  if (node.direction === 'parent') {
    return `${parentObject}.${node.via_field} -> ${node.object}`;
  }
  if (node.direction === 'child') {
    return `${node.object}.${node.via_field} -> ${parentObject}`;
  }
  return `${parentObject}.${node.via_field} -> ${node.object}`;
}

function getNodeWiringStatus(node) {
  if (!node || node.direction !== 'parent' || !node.parentId || !node.via_field) {
    return null;
  }
  const parentNode = state.nodes[node.parentId];
  const fieldMeta = parentNode ? findLookupFieldMeta(parentNode.id, node.via_field) : null;
  if (fieldMeta && fieldMeta.references_to[0] === node.object) {
    return {
      valid: true,
      label: 'lookup-linked',
      hint: `Linked through ${parentNode.object}.${node.via_field}`,
    };
  }
  return {
    valid: false,
    label: 'invalid-link',
    hint: `No valid lookup ${parentNode?.object || '?'}.${node.via_field || '?'} -> ${node.object}`,
  };
}

function getTreeLevelColor(level) {
  const palette = [
    'var(--tree-l1)',
    'var(--tree-l2)',
    'var(--tree-l3)',
    'var(--tree-l4)',
    'var(--tree-l5)',
    'var(--tree-l6)',
  ];
  return palette[Math.max(0, (level - 1) % palette.length)];
}

function buildIndentGuides(depth) {
  return Array.from({ length: depth }, (_, idx) =>
    `<div class="tree-indent-line" style="--level-color:${getTreeLevelColor(idx + 1)}"></div>`).join('');
}

function initPanelResizer() {
  const resizer = $('panel-resizer');
  if (!resizer) {
    return;
  }

  const MIN_WIDTH = 300;
  const maxWidth = () => Math.min(720, Math.floor(window.innerWidth * 0.7));
  const savedWidth = Number(window.localStorage.getItem('sfetl.leftPanelWidth') || 0);
  if (savedWidth) {
    document.documentElement.style.setProperty('--left-w', `${Math.max(MIN_WIDTH, Math.min(savedWidth, maxWidth()))}px`);
  }

  let dragging = false;

  const applyWidth = clientX => {
    const next = Math.max(MIN_WIDTH, Math.min(clientX, maxWidth()));
    document.documentElement.style.setProperty('--left-w', `${next}px`);
    window.localStorage.setItem('sfetl.leftPanelWidth', String(next));
  };

  resizer.addEventListener('mousedown', e => {
    dragging = true;
    document.body.classList.add('is-resizing');
    e.preventDefault();
  });

  window.addEventListener('mousemove', e => {
    if (!dragging) {
      return;
    }
    applyWidth(e.clientX);
  });

  window.addEventListener('mouseup', () => {
    if (!dragging) {
      return;
    }
    dragging = false;
    document.body.classList.remove('is-resizing');
  });
}

function removeChildNodes(nodeId) {
  const node = state.nodes[nodeId];
  if (!node) {
    return;
  }
  node.childIds.forEach(cid => removeChildNodes(cid));
  node.childIds.forEach(cid => {
    delete state.nodeSelections[cid];
    delete state.nodeDirection[cid];
    delete state.nodes[cid];
  });
  node.childIds = [];
}

function removeNode(nodeId) {
  const node = state.nodes[nodeId];
  if (!node || !node.parentId) {
    return;
  }
  const parent = state.nodes[node.parentId];
  const shouldRefreshParentFields = state.selectedNodeId === node.parentId;
  if (parent) {
    parent.childIds = parent.childIds.filter(id => id !== nodeId);
  }
  removeChildNodes(nodeId);
  delete state.nodeSelections[nodeId];
  delete state.nodeDirection[nodeId];
  delete state.nodes[nodeId];

  if (state.selectedNodeId === nodeId) {
    state.selectedNodeId = parent ? parent.id : null;
    if (state.selectedNodeId) {
      selectNode(state.selectedNodeId);
      return;
    }
    $('fields-detail').style.display = 'none';
    $('fields-empty').style.display = '';
  }

  if (shouldRefreshParentFields && parent) {
    const parentData = getCachedObjectData(parent.object);
    const parentSelection = getNodeSelection(parent.id);
    if (parentData && parentSelection) {
      renderFieldsPanel(parentData, parentSelection);
    }
  }

  renderTree();
}

function openAddModal(parentNodeId, mode) {
  const parentNode = state.nodes[parentNodeId];
  if (!parentNode) {
    return;
  }
  addModalContext = { parentNodeId, mode };
  const data = getCachedObjectData(parentNode.object);
  if (!data) {
    return;
  }

  $('add-modal-title').textContent = mode === 'parents'
    ? `Add Parent of ${parentNode.object}`
    : `Add Child of ${parentNode.object}`;
  $('add-modal-options').style.display = mode === 'parents' ? '' : 'none';
  if ($('add-modal-apply-all')) {
    $('add-modal-apply-all').checked = true;
  }

  renderAddModalList(buildAddModalItems(parentNode.object, mode, ''));
  $('add-modal-search').value = '';
  $('add-modal').style.display = 'flex';
}

function openSelectedNodeAddModal(mode) {
  const nodeId = state.selectedNodeId;
  const node = nodeId ? state.nodes[nodeId] : null;
  if (!node) {
    alert('Select a node first.');
    return;
  }
  openAddModal(nodeId, mode);
}

function buildAddModalItems(objectName, mode, query) {
  const data = getCachedObjectData(objectName);
  const lq = query.toLowerCase();
  if (!data) {
    return [];
  }

  if (mode === 'parents') {
    return data.fields
      .filter(f => f.is_lookup && (f.references_to || []).length >= 1)
      .filter(f => {
        if (!lq) {
          return true;
        }
        const refName = f.references_to[0] || '';
        return f.name.toLowerCase().includes(lq)
          || f.label.toLowerCase().includes(lq)
          || refName.toLowerCase().includes(lq);
      })
      .map(f => ({
        name: f.references_to[0],
        label: `${f.label} (${f.name})`,
        via: f.name,
      }));
  }

  return data.children
    .filter(c => !lq || c.object.toLowerCase().includes(lq) || c.field.toLowerCase().includes(lq))
    .map(c => ({
      name: c.object,
      label: `${c.object}.${c.field} -> ${objectName}`,
      via: c.field,
    }));
}

function renderAddModalList(items) {
  $('add-modal-list').innerHTML = items.length
    ? items.map(item => `
      <div class="add-modal-item" onclick="addNode('${escJs(item.name)}','${escJs(item.label)}','${escJs(item.via)}')">
        <span class="item-name">${escHtml(item.name)}</span>
        <span class="item-label">${escHtml(item.label)}</span>
      </div>`).join('')
    : '<div class="loading">No items available</div>';
}

function filterAddModal(q) {
  if (!addModalContext) {
    return;
  }
  const parentNode = state.nodes[addModalContext.parentNodeId];
  if (!parentNode) {
    return;
  }
  renderAddModalList(buildAddModalItems(parentNode.object, addModalContext.mode, q));
}

async function addNode(objectName, label, viaField) {
  const ctx = addModalContext;
  closeAddModal();
  if (!ctx) {
    return;
  }

  const parentNode = state.nodes[ctx.parentNodeId];
  if (!parentNode) {
    return;
  }

  if (ctx.mode === 'parents') {
    const applyAll = !!$('add-modal-apply-all')?.checked;
    if (applyAll) {
      await ensureParentLookupLinkForMatchingNodes(ctx.parentNodeId, objectName, viaField, { selectNewNode: true });
    } else {
      await ensureParentLookupLink(ctx.parentNodeId, objectName, viaField, { selectNewNode: true });
    }
    return;
  }

  await createNode(ctx.parentNodeId, ctx.mode, objectName, viaField, { selectNewNode: true });
}

function closeAddModal() {
  $('add-modal').style.display = 'none';
  addModalContext = null;
}

function selectNode(nodeId) {
  state.selectedNodeId = nodeId;
  const node = state.nodes[nodeId];
  const data = node ? getCachedObjectData(node.object) : null;
  const sel = getNodeSelection(nodeId);
  if (!node || !data || !sel) {
    return;
  }

  $('fields-empty').style.display = 'none';
  $('fields-detail').style.display = '';

  $('fh-object-name').textContent = node.object;
  const pathParts = [];
  let cur = node;
  while (cur) {
    pathParts.unshift(cur.direction === 'seed' ? cur.object : formatNodeRelation(cur));
    cur = state.nodes[cur.parentId];
  }
  $('fh-breadcrumb').textContent = pathParts.join(' | ');

  $('fh-filter').value = sel.whereClause || '';
  $('fh-order').value = sel.orderBy || '';
  $('fh-limit').value = sel.limit || '';
  $('fh-offset').value = sel.offset || '';
  if (node.direction === 'seed') {
    $('seed-filter').value = sel.whereClause || '';
    $('seed-order').value = sel.orderBy || '';
    $('seed-limit').value = sel.limit || '';
    $('seed-offset').value = sel.offset || '';
  }
  $('fh-extid').value = sel.externalId || '';
  $('field-search').value = '';

  renderFieldsPanel(data, sel);
  renderTree();
}

function isTreeNodeVisible(node) {
  if (!node) {
    return false;
  }
  if (node.direction === 'seed') {
    return true;
  }
  if (node.direction === 'parent') {
    return state.treeView.showParents;
  }
  if (node.direction === 'child') {
    return state.treeView.showChildren;
  }
  return true;
}

function syncTreeToolbar() {
  const parentToggle = $('tree-show-parents');
  const childToggle = $('tree-show-children');
  if (parentToggle) {
    parentToggle.checked = state.treeView.showParents;
  }
  if (childToggle) {
    childToggle.checked = state.treeView.showChildren;
  }

  const selectedNode = state.selectedNodeId ? state.nodes[state.selectedNodeId] : null;
  const addParentBtn = $('tree-add-parent-top');
  const addChildBtn = $('tree-add-child-top');

  if (addParentBtn) {
    const selectedData = selectedNode ? getCachedObjectData(selectedNode.object) : null;
    addParentBtn.disabled = !selectedNode || !selectedData?.fields?.some(f => f.is_lookup);
  }
  if (addChildBtn) {
    const selectedData = selectedNode ? getCachedObjectData(selectedNode.object) : null;
    addChildBtn.disabled = !selectedNode || !(selectedData?.children?.length > 0);
  }
}

function renderFieldsPanel(data, sel) {
  const body = $('fields-body');
  body.innerHTML = '';

  const addLabel = text => {
    const d = document.createElement('div');
    d.className = 'section-label';
    d.textContent = text;
    body.appendChild(d);
  };

  const addField = (f) => {
    const checked = sel.selectedFields.has(f.name);
    const row = document.createElement('div');
    row.className = 'field-row';
    row.dataset.field = f.name;
    row.dataset.group = f.required ? 'required' : (f.is_lookup ? 'lookups' : 'fields');
    let tags = '';
    if (f.required) {
      tags += '<span class="field-tag tag-required">required</span>';
    }
    if (f.is_external_id) {
      tags += '<span class="field-tag tag-extid">ext-id</span>';
    }
    if (f.is_lookup && !f.required) {
      tags += '<span class="field-tag tag-lookup">lookup</span>';
    }
    if (f.is_audit_field) {
      tags += '<span class="field-tag tag-audit" title="Selectable for record creation when Salesforce audit-field permissions are enabled">audit-on-create</span>';
    }

    row.innerHTML = `
      <input type="checkbox" class="field-check"
             ${checked ? 'checked' : ''}
             onchange="toggleField('${escJs(f.name)}', this.checked)" />
      <div class="field-info">
        <div class="field-name">${escHtml(f.name)}</div>
        <div class="field-label">${escHtml(f.label)} - ${escHtml(f.type)}</div>
      </div>
      ${tags}`;
    body.appendChild(row);
  };

  const req = data.fields.filter(f => f.required);
  if (req.length) {
    addLabel('Required');
    req.forEach(f => addField(f));
  }

  const lookups = data.fields.filter(f => f.is_lookup && !f.required);
  if (lookups.length) {
    addLabel('Lookups');
    lookups.forEach(f => addField(f));
  }

  const others = data.fields.filter(f => !f.required && !f.is_lookup);
  if (others.length) {
    addLabel('Fields');
    others.forEach(f => addField(f));
  }

  syncSectionCheckboxes();
}

async function toggleField(fieldName, checked) {
  const sel = getNodeSelection(state.selectedNodeId);
  const node = state.nodes[state.selectedNodeId];
  if (!sel || !node) {
    return;
  }
  if (checked) {
    sel.selectedFields.add(fieldName);
  } else {
    sel.selectedFields.delete(fieldName);
  }
  await syncLookupParentNode(node.id, fieldName, checked);
  renderFieldsPanel(getCachedObjectData(node.object), sel);
  renderTree();
}

function updateNodeFilter(value) {
  updateNodeQueryPart('whereClause', value);
}

function updateNodeQueryPart(part, value) {
  const sel = getNodeSelection(state.selectedNodeId);
  if (!sel) {
    return;
  }
  sel[part] = part === 'limit' || part === 'offset' ? normalizeNumericClause(value) : value;
  syncSelectionQueryFilter(sel);
  const node = state.nodes[state.selectedNodeId];
  if (node?.direction === 'seed') {
    syncSeedControls(sel);
  }
}

function syncSeedControls(selection) {
  $('seed-filter').value = selection?.whereClause || '';
  $('seed-order').value = selection?.orderBy || '';
  $('seed-limit').value = selection?.limit || '';
  $('seed-offset').value = selection?.offset || '';
}

function updateSeedQueryPart(part, value) {
  const seedNode = state.nodes[state.rootId];
  if (!seedNode) {
    return;
  }
  const seedSelection = getNodeSelection(seedNode.id);
  if (!seedSelection) {
    return;
  }
  seedSelection[part] = part === 'limit' || part === 'offset' ? normalizeNumericClause(value) : value;
  syncSelectionQueryFilter(seedSelection);
  if (state.selectedNodeId === seedNode.id) {
    $('fh-filter').value = seedSelection.whereClause || '';
    $('fh-order').value = seedSelection.orderBy || '';
    $('fh-limit').value = seedSelection.limit || '';
    $('fh-offset').value = seedSelection.offset || '';
  }
}

function updateNodeExtId(value) {
  const node = state.nodes[state.selectedNodeId];
  if (!node) {
    return;
  }
  setExternalIdForObject(node.object, value);
}

async function selectAllFieldsPanel(checked) {
  const node = state.nodes[state.selectedNodeId];
  const sel = getNodeSelection(state.selectedNodeId);
  if (!sel || !node) {
    return;
  }
  const lookupUpdates = [];
  $('fields-body').querySelectorAll('.field-check:not(:disabled)').forEach(cb => {
    cb.checked = checked;
    const fieldName = cb.closest('.field-row')?.dataset.field;
    if (!fieldName) {
      return;
    }
    if (checked) {
      sel.selectedFields.add(fieldName);
    } else {
      sel.selectedFields.delete(fieldName);
    }
    const fieldMeta = findLookupFieldMeta(node.id, fieldName);
    if (fieldMeta) {
      lookupUpdates.push(syncLookupParentNode(node.id, fieldName, checked));
    }
  });
  await Promise.all(lookupUpdates);
  renderFieldsPanel(getCachedObjectData(node.object), sel);
  renderTree();
}

async function selectSectionFields(section, checked) {
  const node = state.nodes[state.selectedNodeId];
  const sel = getNodeSelection(state.selectedNodeId);
  if (!sel || !node) {
    return;
  }

  const lookupUpdates = [];
  $('fields-body').querySelectorAll(`.field-row[data-group="${section}"] .field-check:not(:disabled)`).forEach(cb => {
    cb.checked = checked;
    const fieldName = cb.closest('.field-row')?.dataset.field;
    if (!fieldName) {
      return;
    }
    if (checked) {
      sel.selectedFields.add(fieldName);
    } else {
      sel.selectedFields.delete(fieldName);
    }
    if (section === 'lookups') {
      lookupUpdates.push(syncLookupParentNode(node.id, fieldName, checked));
    }
  });

  await Promise.all(lookupUpdates);
  renderFieldsPanel(getCachedObjectData(node.object), sel);
  renderTree();
}

function syncSectionCheckboxes() {
  const sync = (checkboxId, selector) => {
    const checkbox = $(checkboxId);
    if (!checkbox) {
      return;
    }
    const rows = [...$('fields-body').querySelectorAll(selector)];
    if (!rows.length) {
      checkbox.checked = false;
      checkbox.indeterminate = false;
      return;
    }
    const total = rows.length;
    const checked = rows.filter(row => row.querySelector('.field-check')?.checked).length;
    checkbox.checked = checked > 0 && checked === total;
    checkbox.indeterminate = checked > 0 && checked < total;
  };

  sync('sel-all-fields', '.field-row[data-group="lookups"], .field-row[data-group="fields"]');
  sync('sel-all-lookups', '.field-row[data-group="lookups"]');
  sync('sel-all-others', '.field-row[data-group="fields"]');
}

async function createNode(parentNodeId, mode, objectName, viaField, options = {}) {
  const parentNode = state.nodes[parentNodeId];
  if (!parentNode) {
    return null;
  }

  const newDepth = parentNode.depth + 1;

  const existingId = parentNode.childIds.find(cid => {
    const existing = state.nodes[cid];
    return existing
      && existing.object === objectName
      && existing.via_field === viaField
      && existing.direction === (mode === 'parents' ? 'parent' : 'child');
  });
  if (existingId) {
    if (options.selectNewNode) {
      selectNode(existingId);
    }
    return existingId;
  }

  const data = await fetchObjectData(objectName);
  if (!data) {
    return null;
  }

  const newId = `n${Date.now()}${Math.floor(Math.random() * 1000)}`;
  state.nodes[newId] = {
    id: newId,
    object: objectName,
    label: objectName,
    parentId: parentNodeId,
    direction: mode === 'parents' ? 'parent' : 'child',
    via_field: viaField,
    depth: newDepth,
    childIds: [],
    autoLinkedField: options.autoLinkedField || null,
  };
  state.nodeSelections[newId] = buildDefaultSelection(data);
  state.nodeSelections[newId].externalId = getObjectExternalId(objectName);
  parentNode.childIds.push(newId);

  if (options.selectNewNode) {
    renderTree();
    selectNode(newId);
  }
  return newId;
}

async function ensureParentLookupLink(parentNodeId, objectName, viaField, options = {}) {
  const fieldMeta = findLookupFieldMeta(parentNodeId, viaField);
  const sel = getNodeSelection(parentNodeId);
  if (!fieldMeta || !sel || fieldMeta.references_to[0] !== objectName) {
    alert(`Cannot add parent '${objectName}' via '${viaField}' because it is not a valid lookup on the source node.`);
    return null;
  }

  sel.selectedFields.add(viaField);
  return createNode(parentNodeId, 'parents', objectName, viaField, {
    ...options,
    autoLinkedField: viaField,
  });
}

async function ensureParentLookupLinkForMatchingNodes(parentNodeId, objectName, viaField, options = {}) {
  const sourceNode = state.nodes[parentNodeId];
  if (!sourceNode) {
    return null;
  }

  const matchingNodeIds = Object.values(state.nodes)
    .filter(node => node && node.object === sourceNode.object)
    .map(node => node.id);

  let primaryId = null;
  for (const nodeId of matchingNodeIds) {
    const createdId = await ensureParentLookupLink(nodeId, objectName, viaField, {
      selectNewNode: nodeId === parentNodeId && !!options.selectNewNode,
    });
    if (nodeId === parentNodeId) {
      primaryId = createdId;
    }
  }
  renderTree();
  return primaryId;
}

function removeParentLookupLinkForMatchingNodes(parentNodeId, objectName, viaField) {
  const sourceNode = state.nodes[parentNodeId];
  if (!sourceNode) {
    return;
  }

  Object.values(state.nodes)
    .filter(node => node && node.object === sourceNode.object)
    .forEach(node => {
      const matchingChildIds = (node.childIds || []).filter(cid => {
        const child = state.nodes[cid];
        return child
          && child.direction === 'parent'
          && child.object === objectName
          && child.via_field === viaField;
      });
      matchingChildIds.forEach(cid => removeNode(cid));
    });

  renderTree();
}

function findLookupFieldMeta(nodeId, fieldName) {
  const node = state.nodes[nodeId];
  const data = node ? getCachedObjectData(node.object) : null;
  if (!data) {
    return null;
  }
  return data.fields.find(f => f.name === fieldName && f.is_lookup && (f.references_to || []).length >= 1) || null;
}

async function syncLookupParentNode(nodeId, fieldName, checked) {
  const fieldMeta = findLookupFieldMeta(nodeId, fieldName);
  if (!fieldMeta) {
    return;
  }

  const parentNode = state.nodes[nodeId];
  if (!parentNode) {
    return;
  }

  const refObject = fieldMeta.references_to[0];
  const matchingChildIds = parentNode.childIds.filter(cid => {
    const child = state.nodes[cid];
    return child
      && child.direction === 'parent'
      && child.object === refObject
      && child.via_field === fieldName;
  });

  if (checked) {
    return ensureParentLookupLinkForMatchingNodes(nodeId, refObject, fieldName, {
      selectNewNode: false,
      autoLinkedField: fieldName,
    });
  }

  if (matchingChildIds.length) {
    removeParentLookupLinkForMatchingNodes(nodeId, refObject, fieldName);
  }
  return null;
}

function syncLookupFieldForParentLink(parentNodeId, objectName, viaField, checked) {
  const fieldMeta = findLookupFieldMeta(parentNodeId, viaField);
  const sel = getNodeSelection(parentNodeId);
  if (!fieldMeta || !sel || fieldMeta.references_to[0] !== objectName) {
    return;
  }
  if (checked) {
    sel.selectedFields.add(viaField);
  } else {
    sel.selectedFields.delete(viaField);
  }
}

function normalizeLookupParentLinks() {
  Object.values(state.nodes).forEach(node => {
    if (!node || node.direction !== 'parent' || !node.parentId || !node.via_field) {
      return;
    }
    const parentNode = state.nodes[node.parentId];
    const fieldMeta = parentNode ? findLookupFieldMeta(parentNode.id, node.via_field) : null;
    const sel = parentNode ? getNodeSelection(parentNode.id) : null;
    if (!fieldMeta || !sel || fieldMeta.references_to[0] !== node.object) {
      return;
    }
    sel.selectedFields.add(node.via_field);
    node.autoLinkedField = node.via_field;
  });
}

function collectInvalidParentLinks() {
  const issues = [];
  Object.values(state.nodes).forEach(node => {
    if (!node || node.direction !== 'parent' || !node.parentId || !node.via_field) {
      return;
    }
    const parentNode = state.nodes[node.parentId];
    const fieldMeta = parentNode ? findLookupFieldMeta(parentNode.id, node.via_field) : null;
    if (!parentNode || !fieldMeta || fieldMeta.references_to[0] !== node.object) {
      issues.push(`${parentNode?.object || '?'}.${node.via_field} -> ${node.object}`);
    }
  });
  return issues;
}

function filterFieldsPanel(q) {
  const lq = q.toLowerCase();
  $('fields-body').querySelectorAll('.field-row').forEach(row => {
    if (!lq) {
      row.style.display = '';
      return;
    }
    const name = (row.dataset.field || '').toLowerCase();
    const label = row.querySelector('.field-label')?.textContent.toLowerCase() || '';
    row.style.display = (name.includes(lq) || label.includes(lq)) ? '' : 'none';
  });

  $('fields-body').querySelectorAll('.section-label').forEach(lbl => {
    let next = lbl.nextElementSibling;
    let visible = false;
    while (next && !next.classList.contains('section-label')) {
      if (next.style.display !== 'none') {
        visible = true;
        break;
      }
      next = next.nextElementSibling;
    }
    lbl.style.display = visible ? '' : 'none';
  });
}

function validateDepthConstraints() {
  return true;
}

function buildSelectionsPayload(allNodes) {
  const byObject = new Map();

  allNodes.forEach(node => {
    const selection = getNodeSelection(node.id);
    const objectName = node.object;
      if (!byObject.has(objectName)) {
        byObject.set(objectName, {
          object: objectName,
          selected_fields: new Set(),
          external_id: getObjectExternalId(objectName),
        });
      }

      const entry = byObject.get(objectName);
      (selection?.selectedFields || []).forEach(field => entry.selected_fields.add(field));

      entry.external_id = getObjectExternalId(objectName);
    });

    return Array.from(byObject.values()).map(entry => ({
      object: entry.object,
      selected_fields: [...entry.selected_fields].sort(),
      external_id: entry.external_id || '',
    }));
  }

function buildTraversalPayload(allNodes) {
  return {
    parents: allNodes
      .filter(n => n.direction === 'parent')
      .map(n => ({ object: n.object, via_field: n.via_field, depth: n.depth })),
    children: allNodes
      .filter(n => n.direction === 'child')
      .map(n => ({ object: n.object, via_field: n.via_field, depth: n.depth })),
  };
}

function getSeedFilterValue() {
  const seedNode = state.nodes[state.rootId];
  const seedSelection = seedNode ? getNodeSelection(seedNode.id) : null;
  if (seedSelection) {
    seedSelection.whereClause = $('seed-filter').value || seedSelection.whereClause || '';
    seedSelection.orderBy = $('seed-order').value || seedSelection.orderBy || '';
    seedSelection.limit = normalizeNumericClause($('seed-limit').value || seedSelection.limit || '');
    seedSelection.offset = normalizeNumericClause($('seed-offset').value || seedSelection.offset || '');
    syncSelectionQueryFilter(seedSelection);
    return seedSelection.queryFilter || '';
  }
  return composeQueryFilter({
    whereClause: $('seed-filter').value || '',
    orderBy: $('seed-order').value || '',
    limit: $('seed-limit').value || '',
    offset: $('seed-offset').value || '',
  });
}

function updateSeedFilter(value) {
  updateSeedQueryPart('whereClause', value);
}

function buildNodesPayload(allNodes) {
  return allNodes.map(node => ({
    id: node.id,
    object: node.object,
    parent_id: node.parentId,
    direction: node.direction,
    via_field: node.via_field,
    depth: node.depth,
    query_filter: getNodeSelection(node.id)?.queryFilter || '',
    where_clause: getNodeSelection(node.id)?.whereClause || '',
    order_by: getNodeSelection(node.id)?.orderBy || '',
    limit: getNodeSelection(node.id)?.limit || '',
    offset: getNodeSelection(node.id)?.offset || '',
  }));
}

function dedupeStateNodes() {
  let changed = true;
  while (changed) {
    changed = false;
    const parents = Object.values(state.nodes);
    for (const parent of parents) {
      if (!parent?.childIds?.length) {
        continue;
      }
      const seen = new Map();
      const nextChildIds = [];

      for (const childId of parent.childIds) {
        const child = state.nodes[childId];
        if (!child) {
          continue;
        }
        const signature = `${child.direction}|${child.object}|${child.via_field || ''}`;
        const existingId = seen.get(signature);
        if (!existingId) {
          seen.set(signature, childId);
          nextChildIds.push(childId);
          continue;
        }

        const existing = state.nodes[existingId];
        if (!existing) {
          continue;
        }

        child.childIds.forEach(grandChildId => {
          const grandChild = state.nodes[grandChildId];
          if (!grandChild) {
            return;
          }
          grandChild.parentId = existingId;
          if (!existing.childIds.includes(grandChildId)) {
            existing.childIds.push(grandChildId);
          }
        });

        if (!state.nodeSelections[existingId] && state.nodeSelections[childId]) {
          state.nodeSelections[existingId] = state.nodeSelections[childId];
        }
        if (!state.nodeDirection[existingId] && state.nodeDirection[childId]) {
          state.nodeDirection[existingId] = state.nodeDirection[childId];
        }
        if (state.selectedNodeId === childId) {
          state.selectedNodeId = existingId;
        }

        delete state.nodeSelections[childId];
        delete state.nodeDirection[childId];
        delete state.nodes[childId];
        changed = true;
      }

      parent.childIds = nextChildIds;
    }
  }
}

function getSerializableNodes() {
  dedupeStateNodes();
  normalizeLookupParentLinks();
  return Object.values(state.nodes);
}

function syncActiveEditorState() {
  const selectedNode = state.nodes[state.selectedNodeId];
  if (selectedNode) {
    updateNodeFilter($('fh-filter').value);
    updateNodeQueryPart('orderBy', $('fh-order').value);
    updateNodeQueryPart('limit', $('fh-limit').value);
    updateNodeQueryPart('offset', $('fh-offset').value);
    updateNodeExtId($('fh-extid').value);
  }
  if (state.rootId && state.nodes[state.rootId]) {
    updateSeedFilter($('seed-filter').value);
    updateSeedQueryPart('orderBy', $('seed-order').value);
    updateSeedQueryPart('limit', $('seed-limit').value);
    updateSeedQueryPart('offset', $('seed-offset').value);
  }
  normalizeLookupParentLinks();
}

function serializeWorkspaceState() {
  const nodes = getSerializableNodes();
  const nodeSelections = {};
  Object.entries(state.nodeSelections).forEach(([nodeId, selection]) => {
    nodeSelections[nodeId] = {
      selectedFields: [...(selection?.selectedFields || [])].sort(),
      queryFilter: selection?.queryFilter || '',
      whereClause: selection?.whereClause || '',
      orderBy: selection?.orderBy || '',
      limit: selection?.limit || '',
      offset: selection?.offset || '',
      externalId: selection?.externalId || getObjectExternalId(state.nodes[nodeId]?.object || ''),
    };
  });

  return {
      org: state.org,
      org_profile: state.orgProfile,
      maxDepth: Number.isFinite(state.maxDepth) ? state.maxDepth : null,
      rootId: state.rootId,
      selectedNodeId: state.selectedNodeId,
      nodeDirection: state.nodeDirection,
      nodes: nodes.map(node => ({
        id: node.id,
        object: node.object,
        label: node.label,
        parentId: node.parentId,
      direction: node.direction,
      via_field: node.via_field,
      depth: node.depth,
      childIds: [...(node.childIds || [])],
      autoLinkedField: node.autoLinkedField || null,
    })),
    nodeSelections,
    objectExternalIds: { ...state.objectExternalIds },
    controls: {
      migrationName: $('migration-name').value || 'Custom Migration',
      pipelineId: $('pipeline-id').value || 'migration',
      seedFilter: $('seed-filter').value || '',
      seedOrder: $('seed-order').value || '',
      seedLimit: $('seed-limit').value || '',
      seedOffset: $('seed-offset').value || '',
      maxRecords: $('max-records').value || '10000',
    },
  };
}

async function restoreWorkspaceState(template) {
  if (!template) {
    return;
  }

  const uniqueObjects = [...new Set((template.nodes || []).map(node => node.object).filter(Boolean))];
  state.org = template.org || 'source';
  state.orgProfile = template.org_profile || 'default';
  $('org-select').value = state.org;
  renderOrgProfileOptions();
  await loadObjects();

  for (const objectName of uniqueObjects) {
    await fetchObjectData(objectName, true);
  }

  state.maxDepth = Number.POSITIVE_INFINITY;

  state.nodes = {};
  (template.nodes || []).forEach(node => {
    state.nodes[node.id] = {
      id: node.id,
      object: node.object,
      label: node.label || node.object,
      parentId: node.parentId ?? null,
      direction: node.direction,
      via_field: node.via_field ?? null,
      depth: Number(node.depth || 0),
      childIds: [...(node.childIds || [])],
      autoLinkedField: node.autoLinkedField || null,
    };
  });

  state.rootId = template.rootId || 'n0';
  state.selectedNodeId = template.selectedNodeId || template.rootId || null;
  state.nodeDirection = { ...(template.nodeDirection || {}) };
  state.nodeSelections = {};
  state.objectExternalIds = { ...(template.objectExternalIds || {}) };
  state.externalIdDraft = {};
  Object.entries(template.nodeSelections || {}).forEach(([nodeId, selection]) => {
    const objectName = state.nodes[nodeId]?.object;
    const extId = selection?.externalId || (objectName ? getObjectExternalId(objectName) : '');
    if (objectName && !state.objectExternalIds[objectName] && extId) {
      state.objectExternalIds[objectName] = extId;
    }
    state.nodeSelections[nodeId] = {
      selectedFields: new Set(selection?.selectedFields || []),
      queryFilter: selection?.queryFilter || '',
      whereClause: selection?.whereClause || '',
      orderBy: selection?.orderBy || '',
      limit: normalizeNumericClause(selection?.limit || ''),
      offset: normalizeNumericClause(selection?.offset || ''),
      externalId: extId,
    };
    if ((!selection?.whereClause && !selection?.orderBy && !selection?.limit && !selection?.offset) && selection?.queryFilter) {
      Object.assign(state.nodeSelections[nodeId], parseLegacyQueryFilter(selection.queryFilter));
    }
    syncSelectionQueryFilter(state.nodeSelections[nodeId]);
  });

  Object.values(state.nodes).forEach(node => {
    const selection = getNodeSelection(node.id);
    if (selection) {
      selection.externalId = getObjectExternalId(node.object);
    }
  });

  $('migration-name').value = template.controls?.migrationName || 'Custom Migration';
  $('pipeline-id').value = template.controls?.pipelineId || 'migration';
  $('seed-filter').value = template.controls?.seedFilter || '';
  $('seed-order').value = template.controls?.seedOrder || '';
  $('seed-limit').value = template.controls?.seedLimit || '';
  $('seed-offset').value = template.controls?.seedOffset || '';
  $('max-records').value = template.controls?.maxRecords || '10000';

  dedupeStateNodes();

  const rootNode = state.nodes[state.rootId];
  $('tree-title').textContent = rootNode?.object || 'Migration Tree';
  $('object-browser').style.display = 'none';
  $('tree-panel').style.display = '';
  $('bottombar').style.display = 'block';
  $('btn-generate').disabled = !rootNode;
  $('btn-topology').disabled = !rootNode;
  $('btn-external-ids').disabled = !rootNode;

  renderTree();
  renderPipelineBoard();
  if (state.selectedNodeId && state.nodes[state.selectedNodeId]) {
    selectNode(state.selectedNodeId);
  } else {
    $('fields-detail').style.display = 'none';
    $('fields-empty').style.display = '';
  }
}

async function loadTemplates() {
  const select = $('template-select');
  if (!select) {
    return;
  }
  try {
    const data = await fetch('/api/templates').then(r => r.json());
    const templates = data.templates || [];
    state.savedTemplates = templates;
    select.innerHTML = '<option value="">Load Template</option>' + templates
      .map(t => `<option value="${escHtml(t.id)}">${escHtml(t.name)}</option>`)
      .join('');
    select.value = state.currentTemplateId || '';
  } catch (e) {
    state.savedTemplates = [];
    select.innerHTML = '<option value="">Load Template</option>';
  }
}

function loadTemplateFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const templateId = (params.get('template') || '').trim();
  if (templateId) {
    loadTemplate(templateId);
  }
}

async function persistTemplate(forceSaveAs = false) {
  const seed = state.nodes[state.rootId];
  if (!seed) {
    alert('Build a migration topology first, then save it as a template.');
    return;
  }

  const defaultName = state.currentTemplateName || $('migration-name').value || seed.object;
  const templateName = window.prompt('Template name', defaultName);
  if (!templateName) {
    return;
  }

  const trimmedName = templateName.trim();
  if (!trimmedName) {
    return;
  }

  let templateId = '';
  if (!forceSaveAs && state.currentTemplateId && trimmedName === (state.currentTemplateName || '').trim()) {
    templateId = state.currentTemplateId;
  }

  const payload = {
    template_id: templateId,
    template_name: trimmedName,
    template: serializeWorkspaceState(),
    input_mode: 'salesforce_source',
    source_profile: state.orgProfile || 'default',
    target_profile: 'default',
    prevalidation_status: 'DRAFT',
  };

  const data = await fetch('/api/templates', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }).then(r => r.json());

  if (data.error) {
    alert(data.error);
    return;
  }

  await loadTemplates();
  setCurrentTemplate(data.template_id, data.template_name);
  alert(`Template saved: ${data.template_name}`);
}

async function saveTemplate() {
  await persistTemplate(false);
}

async function saveTemplateAs() {
  await persistTemplate(true);
}

async function loadTemplate(templateId) {
  if (!templateId) {
    return;
  }
  const data = await fetch(`/api/templates/${encodeURIComponent(templateId)}`).then(r => r.json());
  if (data.error) {
    alert(data.error);
    return;
  }
  await restoreWorkspaceState(data.template);
  setCurrentTemplate(data.template_id || templateId, data.template_name || templateId);
}

function closeTemplateManager() {
  $('template-manager-modal').style.display = 'none';
}

async function openTemplateManager() {
  const wrap = $('template-manager-list');
  wrap.innerHTML = '<div class="template-manager-empty">Loading templates...</div>';
  $('template-manager-modal').style.display = 'flex';

  try {
    const data = await fetch('/api/templates').then(r => r.json());
    const templates = data.templates || [];
    renderTemplateManager(templates);
  } catch (e) {
    wrap.innerHTML = `<div class="template-manager-empty">Failed to load templates: ${escHtml(e.message)}</div>`;
  }
}

function renderTemplateManager(templates) {
  const wrap = $('template-manager-list');
  if (!templates.length) {
    wrap.innerHTML = '<div class="template-manager-empty">No saved templates found.</div>';
    return;
  }

  wrap.innerHTML = templates.map(template => {
    const inUse = template.id === state.currentTemplateId;
    return `
      <div class="template-manager-item">
        <div class="template-manager-meta">
          <div class="template-manager-name">${escHtml(template.name || template.id)}</div>
          <div class="template-manager-id">ID: ${escHtml(template.id)} | Pipeline: ${escHtml(template.pipeline_id || template.id)}</div>
          <div class="template-manager-status">${inUse ? 'Currently loaded in workspace' : 'Not in use'}</div>
        </div>
        <div class="template-manager-actions">
          <button
            class="btn btn-secondary btn-sm"
            onclick="loadTemplateFromManager('${escJs(template.id)}')"
          >Load</button>
          <button
            class="btn btn-danger btn-sm"
            onclick="deleteTemplateFromManager('${escJs(template.id)}', '${escJs(template.name || template.id)}')"
            ${inUse ? 'disabled' : ''}
          >Delete</button>
        </div>
      </div>
    `;
  }).join('');
}

async function loadTemplateFromManager(templateId) {
  closeTemplateManager();
  await loadTemplate(templateId);
}

async function deleteTemplateFromManager(templateId, templateName) {
  if (!templateId) {
    return;
  }
  if (templateId === state.currentTemplateId) {
    alert('This template is currently in use. Load a different template or clear the workspace before deleting it.');
    return;
  }
  const confirmed = window.confirm(`Delete template "${templateName}"?`);
  if (!confirmed) {
    return;
  }

  const response = await fetch(`/api/templates/${encodeURIComponent(templateId)}`, {
    method: 'DELETE',
  });
  const data = await response.json();
  if (!response.ok || data.error) {
    alert(data.error || 'Failed to delete template.');
    return;
  }

  await loadTemplates();
  await openTemplateManager();
}

function getTemplateMeta(templateId) {
  return state.savedTemplates.find(template => template.id === templateId) || null;
}

function workspaceRootNode(workspace) {
  const nodes = workspace?.nodes || [];
  const rootId = workspace?.rootId;
  return nodes.find(node => node.id === rootId)
    || nodes.find(node => !node.parentId)
    || null;
}

function buildSelectionsPayloadFromWorkspace(workspace) {
  const byObject = new Map();
  (workspace?.nodes || []).forEach(node => {
    if (!node?.object) {
      return;
    }
    const objectName = node.object;
    const selection = workspace?.nodeSelections?.[node.id] || {};
    const externalId = workspace?.objectExternalIds?.[objectName] || selection.externalId || '';
    if (!byObject.has(objectName)) {
      byObject.set(objectName, {
        object: objectName,
        selected_fields: new Set(),
        external_id: externalId,
      });
    }
    const entry = byObject.get(objectName);
    (selection.selectedFields || []).forEach(field => entry.selected_fields.add(field));
    entry.external_id = externalId;
  });
  return Array.from(byObject.values()).map(entry => ({
    object: entry.object,
    selected_fields: [...entry.selected_fields].sort(),
    external_id: entry.external_id || '',
  }));
}

function buildTraversalPayloadFromWorkspace(workspace) {
  const nodes = workspace?.nodes || [];
  return {
    parents: nodes
      .filter(node => node.direction === 'parent')
      .map(node => ({ object: node.object, via_field: node.via_field, depth: node.depth })),
    children: nodes
      .filter(node => node.direction === 'child')
      .map(node => ({ object: node.object, via_field: node.via_field, depth: node.depth })),
  };
}

function buildNodesPayloadFromWorkspace(workspace) {
  return (workspace?.nodes || []).map(node => {
    const selection = workspace?.nodeSelections?.[node.id] || {};
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
}

function buildPipelineStage(templatePayload, templateMeta = null) {
  const workspace = templatePayload?.template || {};
  const rootNode = workspaceRootNode(workspace);
  const fallbackMeta = templateMeta || {};
  const templateId = templatePayload?.template_id || fallbackMeta.id || '';
  const templateName = templatePayload?.template_name || fallbackMeta.name || templateId || 'Untitled Template';
  const pipelineId = (workspace?.controls?.pipelineId || fallbackMeta.pipeline_id || templateId || templateName).trim();
  const pipelineName = (workspace?.controls?.migrationName || fallbackMeta.pipeline_name || templateName).trim() || templateName;
  const uniqueObjects = [...new Set((workspace?.nodes || []).map(node => node.object).filter(Boolean))];
  return {
    templateId,
    templateName,
    pipelineId,
    pipelineName,
    seedObject: rootNode?.object || '',
    objectCount: uniqueObjects.length,
    nodeCount: (workspace?.nodes || []).length,
    inputMode: templatePayload?.input_mode || fallbackMeta.input_mode || 'salesforce_source',
    workspace,
  };
}

function hasPipelineStageForTemplate(templateId) {
  return state.pipelineStages.some(stage => stage.templateId === templateId);
}

function hasPipelineStageForPipelineId(pipelineId) {
  return state.pipelineStages.some(stage => stage.pipelineId === pipelineId);
}

async function addCurrentTemplateToPipeline() {
  if (!state.currentTemplateId) {
    alert('Load a saved template first, then add it to the orchestrated pipeline.');
    return;
  }
  if (hasPipelineStageForTemplate(state.currentTemplateId)) {
    alert('This template is already in the pipeline sequence.');
    return;
  }
  const stage = buildPipelineStage({
    template_id: state.currentTemplateId,
    template_name: state.currentTemplateName || state.currentTemplateId,
    template: serializeWorkspaceState(),
  }, getTemplateMeta(state.currentTemplateId));
  if (hasPipelineStageForPipelineId(stage.pipelineId)) {
    alert(`Pipeline ID "${stage.pipelineId}" is already in the sequence. Use a different template or change its pipeline ID first.`);
    return;
  }
  state.pipelineStages.push(stage);
  renderPipelineBoard();
  if ($('template-manager-modal')?.style.display === 'flex') {
    renderTemplateManager(state.savedTemplates);
  }
}

async function addTemplateToPipelineFromManager(templateId) {
  if (!templateId) {
    return;
  }
  if (hasPipelineStageForTemplate(templateId)) {
    alert('This template is already in the pipeline sequence.');
    return;
  }
  const response = await fetch(`/api/templates/${encodeURIComponent(templateId)}`);
  const payload = await response.json();
  if (!response.ok || payload.error) {
    alert(payload.error || 'Failed to load template.');
    return;
  }
  const stage = buildPipelineStage(payload, getTemplateMeta(templateId));
  if (hasPipelineStageForPipelineId(stage.pipelineId)) {
    alert(`Pipeline ID "${stage.pipelineId}" is already in the sequence. Update the template pipeline ID before adding it twice.`);
    return;
  }
  state.pipelineStages.push(stage);
  renderPipelineBoard();
  if ($('template-manager-modal')?.style.display === 'flex') {
    renderTemplateManager(state.savedTemplates);
  }
}

function movePipelineStage(index, direction) {
  const targetIndex = index + direction;
  if (index < 0 || index >= state.pipelineStages.length || targetIndex < 0 || targetIndex >= state.pipelineStages.length) {
    return;
  }
  const [stage] = state.pipelineStages.splice(index, 1);
  state.pipelineStages.splice(targetIndex, 0, stage);
  renderPipelineBoard();
}

function removePipelineStage(index) {
  if (index < 0 || index >= state.pipelineStages.length) {
    return;
  }
  state.pipelineStages.splice(index, 1);
  renderPipelineBoard();
  if ($('template-manager-modal')?.style.display === 'flex') {
    renderTemplateManager(state.savedTemplates);
  }
}

function clearPipelineStages() {
  if (!state.pipelineStages.length) {
    return;
  }
  if (!window.confirm('Clear the full pipeline sequence?')) {
    return;
  }
  state.pipelineStages = [];
  renderPipelineBoard();
  if ($('template-manager-modal')?.style.display === 'flex') {
    renderTemplateManager(state.savedTemplates);
  }
}

function getPipelineStatusRecord(pipelineId) {
  return state.runStatus?.pipelines?.[pipelineId] || null;
}

function pipelineStatusClass(status) {
  const normalized = String(status || 'PENDING').toLowerCase();
  if (normalized.includes('failed')) {
    return 'failed';
  }
  return ['pending', 'running', 'completed', 'failed', 'skipped', 'blocked'].includes(normalized)
    ? normalized
    : 'pending';
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

function summarizePipelineMetrics(record) {
  if (!record) {
    return 'Waiting for first run';
  }
  const objects = Object.values(record.objects || {});
  const completed = objects.filter(objectState => objectState.status === 'COMPLETED').length;
  const failed = objects.filter(objectState => objectState.status === 'FAILED').length;
  const running = objects.filter(objectState => objectState.status === 'RUNNING').length;
  return `${objects.length} object(s) tracked | Completed ${completed} | Running ${running} | Failed ${failed}`;
}

function renderPipelineBoard() {
  const board = $('pipeline-board');
  const meta = $('pipeline-strip-meta');
  const generateBtn = $('btn-generate');
  if (!board || !meta) {
    return;
  }
  if (generateBtn) {
    generateBtn.disabled = !state.pipelineStages.length && !state.rootId;
  }
  if (!state.pipelineStages.length) {
    meta.textContent = 'Add saved templates here, arrange them in sequence, and generate one migration pipeline config.';
    board.innerHTML = '<div class="pipeline-empty">No pipeline stages selected yet. Use "Manage Templates" or "Add To Pipeline" after loading a template.</div>';
    return;
  }

  const updatedText = state.runStatus?.last_updated ? `Latest run update: ${formatTimestamp(state.runStatus.last_updated)}` : 'No run-state updates captured yet';
  meta.textContent = `${state.pipelineStages.length} stage(s) in sequence. Generate Config now writes a multi-pipeline migration. ${updatedText}`;

  board.innerHTML = state.pipelineStages.map((stage, index) => {
    const statusRecord = getPipelineStatusRecord(stage.pipelineId);
    const status = statusRecord?.status || 'PENDING';
    const templateLoaded = stage.templateId && stage.templateId === state.currentTemplateId;
    return `
      <div class="pipeline-stage">
        <div class="pipeline-stage-header">
          <div>
            <div class="pipeline-stage-order">Stage ${index + 1}</div>
            <div class="pipeline-stage-name">${escHtml(stage.pipelineName)}</div>
            <div class="pipeline-stage-subtitle">${escHtml(stage.templateName)} | ${escHtml(stage.pipelineId)}</div>
          </div>
          <span class="pipeline-stage-status ${pipelineStatusClass(status)}">${escHtml(status)}</span>
        </div>
        <div class="pipeline-stage-detail">Seed: ${escHtml(stage.seedObject || 'Unknown')} | Objects: ${escHtml(stage.objectCount)} | Nodes: ${escHtml(stage.nodeCount)}</div>
        <div class="pipeline-stage-detail">${escHtml(summarizePipelineMetrics(statusRecord))}</div>
        <div class="pipeline-stage-update">Latest update: ${escHtml(summarizePipelineLatestUpdate(statusRecord))}</div>
        <div class="pipeline-stage-footer">
          <div class="pipeline-stage-subtitle">${templateLoaded ? 'Currently loaded in builder' : 'Saved template stage'}</div>
          <div class="pipeline-stage-actions">
            <button class="btn btn-secondary btn-sm" onclick="loadPipelineStageIntoWorkspace(${index})">Load</button>
            <button class="btn btn-secondary btn-sm" onclick="movePipelineStage(${index}, -1)" ${index === 0 ? 'disabled' : ''}>Up</button>
            <button class="btn btn-secondary btn-sm" onclick="movePipelineStage(${index}, 1)" ${index === state.pipelineStages.length - 1 ? 'disabled' : ''}>Down</button>
            <button class="btn btn-danger btn-sm" onclick="removePipelineStage(${index})">Remove</button>
          </div>
        </div>
      </div>
      ${index < state.pipelineStages.length - 1 ? '<div class="pipeline-arrow">→</div>' : ''}
    `;
  }).join('');
}

async function loadPipelineStageIntoWorkspace(index) {
  const stage = state.pipelineStages[index];
  if (!stage?.workspace) {
    return;
  }
  await restoreWorkspaceState(stage.workspace);
  setCurrentTemplate(stage.templateId || null, stage.templateName || stage.pipelineName);
  renderPipelineBoard();
}

async function loadRunStatus(forceRender = false) {
  try {
    const response = await fetch('/api/run-status');
    const data = await response.json();
    if (!response.ok || data.error) {
      if (forceRender) {
        alert(data.error || 'Failed to load run status.');
      }
      return;
    }
    state.runStatus = {
      migration: data.migration || null,
      started_at: data.started_at || null,
      last_updated: data.last_updated || null,
      pipelines: data.pipelines || {},
    };
    renderPipelineBoard();
  } catch (e) {
    if (forceRender) {
      alert(`Failed to load run status: ${e.message}`);
    }
  }
}

function buildPipelineRequestFromStage(stage) {
  const workspace = stage.workspace || {};
  const controls = workspace.controls || {};
  const rootNode = workspaceRootNode(workspace);
  return {
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
    selections: buildSelectionsPayloadFromWorkspace(workspace),
    traversal: buildTraversalPayloadFromWorkspace(workspace),
    nodes: buildNodesPayloadFromWorkspace(workspace),
  };
}

async function requestConfigGeneration(saveToFile = false) {
  if (state.pipelineStages.length) {
    const payload = {
      migration_name: $('migration-name').value || 'Custom Migration',
      save_to_file: saveToFile,
      pipelines: state.pipelineStages.map(stage => buildPipelineRequestFromStage(stage)),
    };
    const response = await fetch('/api/config/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok || data.error) {
      alert(data.error || 'Failed to generate orchestrated pipeline config.');
      return null;
    }
    return data;
  }
  return null;
}

async function generateConfig() {
  if (state.pipelineStages.length) {
    const data = await requestConfigGeneration(false);
    if (!data) {
      return;
    }
    $('yaml-output').textContent = data.yaml;
    $('modal-overlay').style.display = 'flex';
    return;
  }
  if (!validateDepthConstraints()) {
    return;
  }
  syncActiveEditorState();
  const invalidParentLinks = collectInvalidParentLinks();
  if (invalidParentLinks.length) {
    alert(
      `Some parent links are not wired to valid lookup fields and can break extraction:\n\n${invalidParentLinks.join('\n')}`
    );
    return;
  }
  const allNodes = getSerializableNodes();
  const seed = state.nodes[state.rootId];
  if (!seed) {
    return;
  }

  const payload = {
    seed_object: seed.object,
    seed_filter: getSeedFilterValue(),
    seed_where: $('seed-filter').value || '',
    seed_order_by: $('seed-order').value || '',
    seed_limit: normalizeNumericClause($('seed-limit').value),
    seed_offset: normalizeNumericClause($('seed-offset').value),
    max_records: parseInt($('max-records').value, 10) || 10000,
    migration_name: $('migration-name').value || 'Custom Migration',
    pipeline_id: $('pipeline-id').value || 'migration',
    selections: buildSelectionsPayload(allNodes),
    traversal: buildTraversalPayload(allNodes),
    nodes: buildNodesPayload(allNodes),
  };

  const data = await fetch('/api/config/generate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }).then(r => r.json());

  if (data.error) {
    alert(data.error);
    return;
  }
  $('yaml-output').textContent = data.yaml;
  $('modal-overlay').style.display = 'flex';
}

function getUniqueWorkspaceObjects() {
  const seen = new Set();
  const ordered = [];
  getSerializableNodes().forEach(node => {
    if (!node.object || seen.has(node.object)) {
      return;
    }
    seen.add(node.object);
    ordered.push(node.object);
  });
  return ordered;
}

function getExternalIdDraftValue(objectName) {
  if (Object.prototype.hasOwnProperty.call(state.externalIdDraft, objectName)) {
    return state.externalIdDraft[objectName];
  }
  return hasExplicitExternalId(objectName) ? state.objectExternalIds[objectName] : '';
}

function resetExternalIdDraft() {
  state.externalIdDraft = {};
  getUniqueWorkspaceObjects().forEach(objectName => {
    state.externalIdDraft[objectName] = hasExplicitExternalId(objectName) ? state.objectExternalIds[objectName] : '';
  });
}

function renderExternalIdTable() {
  const wrap = $('external-id-table-wrap');
  if (!wrap) {
    return;
  }
  const objects = getUniqueWorkspaceObjects();
  if (!objects.length) {
    wrap.innerHTML = '<div class="external-id-empty">Build a migration topology first, then manage External IDs here.</div>';
    return;
  }

  const unmappedOnly = $('external-id-unmapped-only')?.checked;
  const visibleObjects = objects.filter(objectName => {
    if (!unmappedOnly) {
      return true;
    }
    return !getExternalIdDraftValue(objectName).trim();
  });

  const mappedCount = objects.filter(objectName => getExternalIdDraftValue(objectName).trim()).length;
  const summary = `
    <div class="external-id-summary">
      Unique objects in topology: ${objects.length} | Mapped: ${mappedCount} | Remaining: ${objects.length - mappedCount}
    </div>
  `;

  if (!visibleObjects.length) {
    wrap.innerHTML = `${summary}<div class="external-id-empty">Nothing left here. All unique objects already have a mapping in this view.</div>`;
    return;
  }

  const rows = visibleObjects.map(objectName => {
    const suggested = getSuggestedExternalIdForObject(objectName);
    const current = getObjectExternalId(objectName);
    const explicit = getExternalIdDraftValue(objectName);
    const usage = getObjectUsageCount(objectName);
    const listId = `extid-options-${objectName.replace(/[^a-zA-Z0-9_-]+/g, '_')}`;
    const options = getExternalIdOptionsForObject(objectName);
    return `
      <tr>
        <td>
          <div class="external-id-object">${escHtml(objectName)}</div>
          <div class="external-id-meta">${usage} node${usage !== 1 ? 's' : ''} in current topology</div>
        </td>
        <td><span class="external-id-pill">${escHtml(suggested)}</span></td>
        <td>
          <div class="external-id-meta">${hasExplicitExternalId(objectName) ? 'Saved mapping' : 'Using suggested fallback'}</div>
        </td>
        <td>
          <input
            class="external-id-input"
            list="${escHtml(listId)}"
            value="${escHtml(explicit)}"
            data-object="${escHtml(objectName)}"
            placeholder="${escHtml(suggested)}"
          />
          <datalist id="${escHtml(listId)}">
            ${options.map(option => `<option value="${escHtml(option)}"></option>`).join('')}
          </datalist>
        </td>
      </tr>
    `;
  }).join('');

  wrap.innerHTML = `
    ${summary}
    <table class="external-id-table">
      <thead>
        <tr>
          <th>Object</th>
          <th>Suggested</th>
          <th>Status</th>
          <th>Map To External ID</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;

  wrap.querySelectorAll('.external-id-input').forEach(input => {
    input.addEventListener('input', e => {
      const objectName = e.target.dataset.object;
      state.externalIdDraft[objectName] = e.target.value;
    });
  });
}

function openExternalIdModal() {
  if (!state.rootId || !state.nodes[state.rootId]) {
    return;
  }
  resetExternalIdDraft();
  renderExternalIdTable();
  $('external-id-modal').style.display = 'flex';
}

function closeExternalIdModal() {
  $('external-id-modal').style.display = 'none';
}

function applySuggestedExternalIds() {
  getUniqueWorkspaceObjects().forEach(objectName => {
    state.externalIdDraft[objectName] = getSuggestedExternalIdForObject(objectName);
  });
  renderExternalIdTable();
}

function saveExternalIdMappings() {
  getUniqueWorkspaceObjects().forEach(objectName => {
    const draftValue = (state.externalIdDraft[objectName] || '').trim();
    if (draftValue) {
      setExternalIdForObject(objectName, draftValue);
    } else {
      clearExternalIdForObject(objectName);
    }
  });
  closeExternalIdModal();
}

async function saveConfig() {
  if (state.pipelineStages.length) {
    const data = await requestConfigGeneration(true);
    if (!data) {
      return;
    }
    alert(`Saved orchestrated pipeline config with ${state.pipelineStages.length} stage(s) to config/migration.yaml`);
    $('modal-overlay').style.display = 'none';
    return;
  }
  if (!validateDepthConstraints()) {
    return;
  }
  syncActiveEditorState();
  const invalidParentLinks = collectInvalidParentLinks();
  if (invalidParentLinks.length) {
    alert(
      `Some parent links are not wired to valid lookup fields and cannot be saved safely:\n\n${invalidParentLinks.join('\n')}`
    );
    return;
  }
  const allNodes = getSerializableNodes();
  const seed = state.nodes[state.rootId];
  if (!seed) {
    return;
  }

  const payload = {
    seed_object: seed.object,
    seed_filter: getSeedFilterValue(),
    seed_where: $('seed-filter').value || '',
    seed_order_by: $('seed-order').value || '',
    seed_limit: normalizeNumericClause($('seed-limit').value),
    seed_offset: normalizeNumericClause($('seed-offset').value),
    max_records: parseInt($('max-records').value, 10) || 10000,
    migration_name: $('migration-name').value || 'Custom Migration',
    pipeline_id: $('pipeline-id').value || 'migration',
    selections: buildSelectionsPayload(allNodes),
    traversal: buildTraversalPayload(allNodes),
    nodes: buildNodesPayload(allNodes),
    save_to_file: true,
  };

  const data = await fetch('/api/config/generate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }).then(r => r.json());

  if (data.error) {
    alert(data.error);
    return;
  }
  alert('Saved to config/migration.yaml');
  $('modal-overlay').style.display = 'none';
}

function openTopologyModal() {
  if (!state.rootId || !state.nodes[state.rootId]) {
    return;
  }
  $('topology-layout-mode').value = state.topologyView.mode || 'minimal';
  $('topology-radial-scope').value = state.topologyView.radialScope || 'children';
  $('topology-font-scale').value = Math.round((state.topologyView.fontScale || 1) * 100);
  $('topology-node-fill-filter').value = state.topologyView.nodeFillFilter || 'all';
  const groupDupEl = $('topology-group-duplicates');
  if (groupDupEl) {
    groupDupEl.checked = !!state.topologyView.groupDuplicates;
  }
  $('topology-show-children').checked = state.topologyView.showChildren;
  $('topology-show-parents').checked = state.topologyView.showParents;
  $('topology-min-level').value = state.topologyView.minLevel;
  $('topology-max-level').value = state.topologyView.maxLevel;
  renderTopologyGraph();
  $('topology-modal').style.display = 'flex';
}

function closeTopologyModal() {
  $('topology-modal').style.display = 'none';
}

function toggleTopologyWidth() {
  const modal = $('topology-modal').querySelector('.modal-topology');
  const btn = $('topology-expand');
  if (!modal || !btn) {
    return;
  }
  const fullscreen = modal.classList.toggle('fullscreen');
  modal.classList.toggle('expanded', fullscreen);
  btn.textContent = fullscreen ? 'Exit Fullscreen' : 'Fullscreen';
  renderTopologyGraph();
}

function renderTopologyGraph() {
  const svg = $('topology-canvas');
  const nodes = getVisibleTopologyNodes();
  const mode = state.topologyView.mode || 'minimal';
  if (mode === 'radial') {
    renderRadialTopologyGraph(svg, nodes);
    return;
  }
  if (mode === 'classic') {
    renderClassicTopologyGraph(svg, nodes);
    return;
  }
  renderMinimalTopologyGraph(svg, nodes);
}

function renderClassicTopologyGraph(svg, nodes) {
  const selectedId = state.selectedNodeId;
  const columns = groupNodesBySignedDepth(nodes);
  const boxWidth = 210;
  const boxHeight = 64;
  const xGap = 120;
  const yGap = 34;
  const padding = 40;
  const laneLabelHeight = 28;

  const columnHeights = columns.map(col => col.length * boxHeight + Math.max(0, col.length - 1) * yGap);
  const graphHeight = Math.max(520, ...columnHeights) + padding * 2 + laneLabelHeight;
  const graphWidth = Math.max(760, columns.length * boxWidth + Math.max(0, columns.length - 1) * xGap + padding * 2);

  svg.setAttribute('viewBox', `0 0 ${graphWidth} ${graphHeight}`);
  svg.innerHTML = '';

  const positions = {};
  columns.forEach((col, depth) => {
    const totalHeight = col.length * boxHeight + Math.max(0, col.length - 1) * yGap;
    const startY = (graphHeight - totalHeight) / 2 + laneLabelHeight / 2;
    col.forEach((node, index) => {
      positions[node.id] = {
        x: padding + depth * (boxWidth + xGap),
        y: startY + index * (boxHeight + yGap),
      };
    });
  });

  columns.forEach((col, depthIndex) => {
    const band = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    band.setAttribute('class', 'topology-lane-band');
    band.setAttribute('x', padding + depthIndex * (boxWidth + xGap) - 20);
    band.setAttribute('y', 0);
    band.setAttribute('width', boxWidth + 40);
    band.setAttribute('height', graphHeight);
    band.setAttribute('fill', getTopologyLevelColor(col.depth));
    svg.appendChild(band);
  });

  const signedDepths = columns.map(col => col.depth);
  if (signedDepths.some(depth => depth < 0)) {
    appendTopologyLaneLabel(svg, padding, 22, 'Children');
  }
  const zeroDepthIndex = signedDepths.indexOf(0);
  if (zeroDepthIndex !== -1) {
    appendTopologyLaneLabel(
      svg,
      padding + zeroDepthIndex * (boxWidth + xGap),
      22,
      'Seed',
    );
  }
  if (signedDepths.some(depth => depth > 0)) {
    const firstParentDepth = signedDepths.find(depth => depth > 0);
    const firstParentIndex = signedDepths.indexOf(firstParentDepth);
    appendTopologyLaneLabel(
      svg,
      padding + firstParentIndex * (boxWidth + xGap),
      22,
      'Parents',
    );
  }

  const focusState = computeTopologyFocusState(nodes);

  nodes.forEach(node => {
    if (!node.parentId || !positions[node.id] || !positions[node.parentId]) {
      return;
    }
    const from = positions[node.parentId];
    const to = positions[node.id];
    const selectedEdge = focusState.edges.has(node.id)
      || selectedId === node.id
      || selectedId === node.parentId;
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('class', `topology-link${selectedEdge ? ' selected' : ''}${focusState.hasFocus && !selectedEdge ? ' dimmed' : ''}`);
    path.setAttribute('d', buildCurvePath(from.x + boxWidth, from.y + boxHeight / 2, to.x, to.y + boxHeight / 2));
    if (selectedEdge) {
      path.setAttribute('stroke', getTopologyLevelColor(node.depth));
    }
    svg.appendChild(path);
  });

  nodes.forEach(node => {
    const pos = positions[node.id];
    if (!pos) {
      return;
    }

    const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    const boxClasses = ['topology-node-box'];
    if (node.direction === 'seed') {
      boxClasses.push('seed');
    }
    if (selectedId === node.id) {
      boxClasses.push('selected');
    }
    if (focusState.nodes.has(node.id)) {
      boxClasses.push('highlight');
    }
    if (focusState.hasFocus && !focusState.nodes.has(node.id)) {
      boxClasses.push('dimmed');
    }
    rect.setAttribute('class', boxClasses.join(' '));
    rect.setAttribute('x', pos.x);
    rect.setAttribute('y', pos.y);
    rect.setAttribute('width', boxWidth);
    rect.setAttribute('height', boxHeight);
    rect.setAttribute('stroke', selectedId === node.id || focusState.nodes.has(node.id)
      ? getTopologyLevelColor(node.depth)
      : '');
    rect.style.cursor = 'pointer';
    rect.addEventListener('click', () => {
      toggleTopologyFocus(node.id);
      renderTopologyGraph();
    });
    svg.appendChild(rect);

    const name = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    name.setAttribute('class', `topology-node-text${focusState.hasFocus && !focusState.nodes.has(node.id) ? ' dimmed' : ''}`);
    name.setAttribute('x', pos.x + 14);
    name.setAttribute('y', pos.y + 24);
    name.textContent = node.object;
    name.style.pointerEvents = 'none';
    svg.appendChild(name);

    const meta = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    meta.setAttribute('class', `topology-node-meta${focusState.hasFocus && !focusState.nodes.has(node.id) ? ' dimmed' : ''}`);
    meta.setAttribute('x', pos.x + 14);
    meta.setAttribute('y', pos.y + 44);
    const relation = describeTopologyRelation(node);
    meta.textContent = `${relation} | depth ${node.depth}`;
    meta.style.pointerEvents = 'none';
    svg.appendChild(meta);
  });

  const parentCount = nodes.filter(n => n.direction === 'parent').length;
  const childCount = nodes.filter(n => n.direction === 'child').length;
  $('topology-summary').textContent =
    `${nodes.length} visible node(s) | ${parentCount} parent relation(s) | ${childCount} child relation(s) | levels ${state.topologyView.minLevel}-${state.topologyView.maxLevel}`;
}

function renderMinimalTopologyGraph(svg, nodes) {
  const selectedId = state.selectedNodeId;
  const columns = groupNodesBySignedDepth(nodes);
  const boxWidth = 170;
  const boxHeight = 28;
  const xGap = 88;
  const yGap = 18;
  const padding = 40;
  const laneLabelHeight = 28;

  const columnHeights = columns.map(col => col.length * boxHeight + Math.max(0, col.length - 1) * yGap);
  const graphHeight = Math.max(520, ...columnHeights, 260) + padding * 2 + laneLabelHeight;
  const graphWidth = Math.max(760, columns.length * boxWidth + Math.max(0, columns.length - 1) * xGap + padding * 2);
  svg.setAttribute('viewBox', `0 0 ${graphWidth} ${graphHeight}`);
  svg.innerHTML = '';

  const positions = {};
  columns.forEach((col, depth) => {
    const totalHeight = col.length * boxHeight + Math.max(0, col.length - 1) * yGap;
    const startY = (graphHeight - totalHeight) / 2 + laneLabelHeight / 2;
    col.forEach((node, index) => {
      positions[node.id] = {
        x: padding + depth * (boxWidth + xGap),
        y: startY + index * (boxHeight + yGap),
      };
    });
  });

  columns.forEach((col, depthIndex) => {
    const band = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    band.setAttribute('class', 'topology-lane-band');
    band.setAttribute('x', padding + depthIndex * (boxWidth + xGap) - 18);
    band.setAttribute('y', 0);
    band.setAttribute('width', boxWidth + 36);
    band.setAttribute('height', graphHeight);
    band.setAttribute('fill', getTopologyLevelColor(col.depth));
    svg.appendChild(band);
  });

  const signedDepths = columns.map(col => col.depth);
  if (signedDepths.some(depth => depth < 0)) {
    appendTopologyLaneLabel(svg, padding, 22, 'Children');
  }
  const zeroDepthIndex = signedDepths.indexOf(0);
  if (zeroDepthIndex !== -1) {
    appendTopologyLaneLabel(svg, padding + zeroDepthIndex * (boxWidth + xGap), 22, 'Seed');
  }
  if (signedDepths.some(depth => depth > 0)) {
    const firstParentDepth = signedDepths.find(depth => depth > 0);
    const firstParentIndex = signedDepths.indexOf(firstParentDepth);
    appendTopologyLaneLabel(svg, padding + firstParentIndex * (boxWidth + xGap), 22, 'Parents');
  }

  const focusState = computeTopologyFocusState(nodes);
  nodes.forEach(node => {
    if (!node.parentId || !positions[node.id] || !positions[node.parentId]) {
      return;
    }
    const from = positions[node.parentId];
    const to = positions[node.id];
    const selectedEdge = focusState.edges.has(node.id) || selectedId === node.id || selectedId === node.parentId;
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('class', `topology-link${selectedEdge ? ' selected' : ''}${focusState.hasFocus && !selectedEdge ? ' dimmed' : ''}`);
    path.setAttribute('d', buildCurvePath(from.x + 12, from.y + boxHeight / 2, to.x + 12, to.y + boxHeight / 2));
    if (selectedEdge) {
      path.setAttribute('stroke', getTopologyLevelColor(node.depth));
    }
    svg.appendChild(path);
  });

  nodes.forEach(node => renderCompactTopologyNode(svg, node, positions[node.id], focusState, selectedId));
  updateTopologySummary(nodes, modeLabelForTopology('minimal'));
}

function renderRadialTopologyGraph(svg, nodes) {
  const radialScope = state.topologyView.radialScope || 'children';
  const radialScaffoldNodes = getRadialTopologyNodes(getVisibleTopologyNodesBase(), radialScope);
  const radialNodes = getRadialTopologyNodes(nodes, radialScope);
  const selectedId = state.selectedNodeId;

  const byDepth = groupNodesBySignedDepth(radialScaffoldNodes);
  const maxAbsDepth = Math.max(0, ...byDepth.map(col => Math.abs(col.depth)));
  const ringGap = 70;
  const outerRadius = Math.max(180, maxAbsDepth * ringGap + 40);
  const width = Math.max(900, outerRadius * 2 + 260);
  const height = Math.max(700, outerRadius * 2 + 160);
  const centerX = width / 2;
  const centerY = height / 2;

  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.classList.remove('has-hover');
  svg.innerHTML = '';

  for (let depth = 1; depth <= maxAbsDepth; depth += 1) {
    const ring = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    ring.setAttribute('class', 'topology-ring');
    ring.setAttribute('cx', centerX);
    ring.setAttribute('cy', centerY);
    ring.setAttribute('r', depth * ringGap);
    svg.appendChild(ring);

    const ringLabel = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    ringLabel.setAttribute('class', 'topology-ring-label');
    ringLabel.setAttribute('x', centerX + 6);
    ringLabel.setAttribute('y', centerY - depth * ringGap - 4);
    ringLabel.textContent = `L${depth}`;
    svg.appendChild(ringLabel);
  }

  appendTopologySideLabel(
    svg,
    centerX - 44,
    centerY - outerRadius - 18,
    state.topologyView.radialScope === 'parents' ? 'Parents' : 'Children',
  );

  // Build the subtree layout against the scaffold so positions stay stable
  // even when fill-filters hide leaves.
  const layout = computeRadialTreeLayout(radialScaffoldNodes, {
    centerX,
    centerY,
    ringGap,
    startAngle: -Math.PI / 2 - Math.PI,
    endAngle: -Math.PI / 2 + Math.PI,
  });
  const { positions, angleByNode, childMap } = layout;

  // Optionally collapse adjacent same-object siblings to cut visual noise on
  // dense rings (common with polymorphic references).
  const groupInfo = state.topologyView.groupDuplicates
    ? computeRadialDuplicateGroups(childMap, state.nodes)
    : { hidden: new Set(), countByRepId: {} };

  const visibleIds = new Set(radialNodes.map(n => n.id));
  const renderNodes = radialNodes.filter(n => !groupInfo.hidden.has(n.id));
  const renderIds = new Set(renderNodes.map(n => n.id));
  const focusState = computeTopologyFocusState(renderNodes);

  renderNodes.forEach(node => {
    if (!node.parentId || !positions[node.id]) {
      return;
    }
    // Walk up any hidden ancestors so the edge connects to the nearest
    // visible representative (keeps the tree connected when grouping).
    let anchorId = node.parentId;
    while (anchorId && (!positions[anchorId] || !renderIds.has(anchorId))) {
      const anchor = state.nodes[anchorId];
      anchorId = anchor ? anchor.parentId : null;
    }
    if (!anchorId || !positions[anchorId]) {
      return;
    }
    const from = positions[anchorId];
    const to = positions[node.id];
    const selectedEdge = focusState.edges.has(node.id)
      || selectedId === node.id
      || selectedId === anchorId;
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('class', `topology-link radial${selectedEdge ? ' selected' : ''}${focusState.hasFocus && !selectedEdge ? ' dimmed' : ''}`);
    path.setAttribute('d', buildRadialCurvePath(centerX, centerY, from.x, from.y, to.x, to.y));
    path.setAttribute('data-target-id', node.id);
    path.setAttribute('data-source-id', anchorId);
    if (selectedEdge) {
      path.setAttribute('stroke', getTopologyLevelColor(node.depth));
    }
    svg.appendChild(path);
  });

  renderNodes.forEach(node => renderCompactTopologyNode(svg, node, positions[node.id], focusState, selectedId, {
    radial: true,
    centerX,
    centerY,
    fontScale: state.topologyView.fontScale || 1,
    angleByNode,
    groupCount: groupInfo.countByRepId[node.id] || 0,
  }));

  const groupedExtras = Object.values(groupInfo.countByRepId).reduce((sum, n) => sum + Math.max(0, n - 1), 0);
  updateTopologySummary(
    renderNodes,
    modeLabelForTopology('radial'),
    groupedExtras ? { extraText: `grouped +${groupedExtras} duplicate(s)` } : null,
  );
  // Keep counts honest if filters hid some nodes vs scaffold
  void visibleIds;
}

function renderCompactTopologyNode(svg, node, pos, focusState, selectedId, layout = {}) {
  if (!pos) {
    return;
  }
  const isSeed = node.direction === 'seed';
  const fillKind = getTopologyNodeFillKind(node);
  const groupCount = layout.groupCount || 0;
  const fontScale = layout.fontScale || 1;
  const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
  const circleClasses = ['topology-node-dot', fillKind];
  if (isSeed) {
    circleClasses.push('seed');
  }
  if (groupCount > 1) {
    circleClasses.push('group');
  }
  if (selectedId === node.id) {
    circleClasses.push('selected');
  }
  if (focusState.nodes.has(node.id)) {
    circleClasses.push('highlight');
  }
  if (focusState.hasFocus && !focusState.nodes.has(node.id)) {
    circleClasses.push('dimmed');
  }
  circle.setAttribute('class', circleClasses.join(' '));
  circle.setAttribute('cx', pos.x);
  circle.setAttribute('cy', pos.y);
  const baseRadius = isSeed ? 7 * fontScale : Math.max(2.4, 4.5 * fontScale);
  const radius = groupCount > 1 ? baseRadius + Math.min(4, Math.log2(groupCount)) : baseRadius;
  circle.setAttribute('r', radius);
  circle.setAttribute('data-node-id', node.id);
  circle.style.cursor = 'pointer';
  circle.addEventListener('click', () => {
    toggleTopologyFocus(node.id);
    renderTopologyGraph();
  });
  if (layout.radial && !isSeed) {
    circle.addEventListener('mouseenter', () => highlightRadialLineage(node.id, true));
    circle.addEventListener('mouseleave', () => highlightRadialLineage(node.id, false));
  }
  const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
  const groupLine = groupCount > 1 ? `\ngrouped x${groupCount} sibling(s)` : '';
  title.textContent = `${node.object}\n${describeTopologyRelation(node)}\nvia ${node.via_field || '-'}\ndepth ${node.depth}\n${fillKind === 'filled' ? 'expanded / record path' : 'reference-only leaf'}${groupLine}`;
  circle.appendChild(title);
  svg.appendChild(circle);

  const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
  const labelClasses = ['topology-node-label'];
  if (isSeed) {
    labelClasses.push('seed');
  }
  if (focusState.hasFocus && !focusState.nodes.has(node.id)) {
    labelClasses.push('dimmed');
  }
  label.setAttribute('class', labelClasses.join(' '));
  label.setAttribute('data-node-id', node.id);
  const labelBase = shortenTopologyLabel(node.object, layout.radial ? 18 : 20);
  const labelText = groupCount > 1 ? `${labelBase} x${groupCount}` : labelBase;
  if (isSeed) {
    label.setAttribute('x', pos.x);
    label.setAttribute('y', pos.y + 28);
    label.setAttribute('text-anchor', 'middle');
    label.style.fontSize = `${14 * fontScale}px`;
  } else if (layout.radial) {
    // Tangential placement: the label runs along the ray through its dot so
    // labels in dense rings never stack horizontally on top of each other.
    const angleRad = (layout.angleByNode && layout.angleByNode[node.id] !== undefined)
      ? layout.angleByNode[node.id]
      : Math.atan2(pos.y - layout.centerY, pos.x - layout.centerX);
    const deg = angleRad * 180 / Math.PI;
    const onLeft = Math.cos(angleRad) < 0;
    const offset = radius + 5;
    const rot = onLeft ? deg + 180 : deg;
    const tx = onLeft ? -offset : offset;
    label.setAttribute('text-anchor', onLeft ? 'end' : 'start');
    label.setAttribute('dy', '0.32em');
    label.setAttribute('transform', `translate(${pos.x},${pos.y}) rotate(${rot.toFixed(2)}) translate(${tx},0)`);
    label.style.fontSize = `${Math.max(7, 10 * fontScale)}px`;
  } else {
    label.setAttribute('x', pos.x + 14);
    label.setAttribute('y', pos.y + 4);
    label.setAttribute('text-anchor', 'start');
  }
  label.textContent = labelText;
  label.style.pointerEvents = 'none';
  svg.appendChild(label);
}

function computeRadialTreeLayout(scaffoldNodes, opts) {
  const { centerX, centerY, ringGap, startAngle, endAngle } = opts;
  const nodeMap = {};
  const childMap = {};
  scaffoldNodes.forEach(n => { nodeMap[n.id] = n; });
  scaffoldNodes.forEach(n => {
    if (n.direction === 'seed' || !n.parentId || !nodeMap[n.parentId]) {
      return;
    }
    if (!childMap[n.parentId]) {
      childMap[n.parentId] = [];
    }
    childMap[n.parentId].push(n.id);
  });
  // Sort siblings: by object name first (keeps duplicates adjacent so the
  // Group Duplicates pass can collapse them), then by id for stability.
  Object.keys(childMap).forEach(pid => {
    childMap[pid].sort((a, b) => {
      const na = nodeMap[a]; const nb = nodeMap[b];
      const byObj = (na?.object || '').localeCompare(nb?.object || '');
      if (byObj !== 0) return byObj;
      return String(a).localeCompare(String(b));
    });
  });

  const leafCounts = {};
  const computeLeaves = (id) => {
    if (leafCounts[id] !== undefined) return leafCounts[id];
    const kids = childMap[id];
    if (!kids || !kids.length) {
      leafCounts[id] = 1;
      return 1;
    }
    let total = 0;
    kids.forEach(cid => { total += computeLeaves(cid); });
    leafCounts[id] = total;
    return total;
  };

  const positions = {};
  const angleByNode = {};
  const seed = scaffoldNodes.find(n => n.direction === 'seed');
  if (!seed) {
    return { positions, angleByNode, childMap };
  }
  computeLeaves(seed.id);

  const walk = (id, depth, aStart, aEnd) => {
    const midAngle = (aStart + aEnd) / 2;
    angleByNode[id] = midAngle;
    if (depth === 0) {
      positions[id] = { x: centerX, y: centerY };
    } else {
      const radius = depth * ringGap;
      positions[id] = {
        x: centerX + Math.cos(midAngle) * radius,
        y: centerY + Math.sin(midAngle) * radius,
      };
    }
    const kids = childMap[id];
    if (!kids || !kids.length) {
      return;
    }
    const totalWeight = kids.reduce((sum, cid) => sum + (leafCounts[cid] || 1), 0) || 1;
    const fullSpan = aEnd - aStart;
    // Leave a tiny gap between subtrees so their wedges read as separate.
    const gapFraction = kids.length > 1 ? Math.min(0.12, 0.015 * kids.length) : 0;
    const gap = fullSpan * gapFraction / Math.max(1, kids.length);
    const usableSpan = fullSpan - gap * Math.max(0, kids.length - 1);
    let cursor = aStart;
    kids.forEach((cid, idx) => {
      const share = usableSpan * ((leafCounts[cid] || 1) / totalWeight);
      const childStart = cursor;
      const childEnd = cursor + share;
      walk(cid, depth + 1, childStart, childEnd);
      cursor = childEnd + gap;
    });
  };

  walk(seed.id, 0, startAngle, endAngle);
  return { positions, angleByNode, childMap };
}

function computeRadialDuplicateGroups(childMap, allNodes) {
  const hidden = new Set();
  const countByRepId = {};
  Object.keys(childMap).forEach(pid => {
    const kids = childMap[pid];
    if (!kids || kids.length < 2) return;
    let i = 0;
    while (i < kids.length) {
      const repId = kids[i];
      const repObj = allNodes[repId]?.object;
      let j = i + 1;
      while (j < kids.length && allNodes[kids[j]]?.object === repObj) {
        j += 1;
      }
      const groupSize = j - i;
      if (groupSize > 1) {
        countByRepId[repId] = groupSize;
        for (let k = i + 1; k < j; k += 1) {
          hidden.add(kids[k]);
        }
      }
      i = j;
    }
  });
  // Cascade: any node whose ancestor is hidden is also hidden.
  let changed = true;
  while (changed) {
    changed = false;
    Object.keys(childMap).forEach(pid => {
      if (!hidden.has(pid)) return;
      (childMap[pid] || []).forEach(cid => {
        if (!hidden.has(cid)) {
          hidden.add(cid);
          changed = true;
        }
      });
    });
  }
  return { hidden, countByRepId };
}

function highlightRadialLineage(nodeId, on) {
  const svg = $('topology-canvas');
  if (!svg) return;
  svg.querySelectorAll('.hover-path').forEach(el => el.classList.remove('hover-path'));
  svg.classList.toggle('has-hover', !!on);
  if (!on) return;
  let current = nodeId;
  const safety = 128;
  let steps = 0;
  while (current && steps < safety) {
    const dot = svg.querySelector(`.topology-node-dot[data-node-id="${cssEscapeId(current)}"]`);
    const lbl = svg.querySelector(`.topology-node-label[data-node-id="${cssEscapeId(current)}"]`);
    if (dot) dot.classList.add('hover-path');
    if (lbl) lbl.classList.add('hover-path');
    const edge = svg.querySelector(`.topology-link.radial[data-target-id="${cssEscapeId(current)}"]`);
    if (edge) edge.classList.add('hover-path');
    const node = state.nodes[current];
    if (!node || node.direction === 'seed') break;
    current = node.parentId;
    steps += 1;
  }
}

function cssEscapeId(id) {
  if (window.CSS && typeof window.CSS.escape === 'function') {
    return window.CSS.escape(id);
  }
  return String(id).replace(/(["\\\]])/g, '\\$1');
}

function updateTopologySummary(nodes, modeLabel, extras) {
  const parentCount = nodes.filter(n => n.direction === 'parent').length;
  const childCount = nodes.filter(n => n.direction === 'child').length;
  const parts = [
    `${modeLabel} view`,
    `${nodes.length} visible node(s)`,
    `${parentCount} parent relation(s)`,
    `${childCount} child relation(s)`,
    `levels ${state.topologyView.minLevel}-${state.topologyView.maxLevel}`,
  ];
  if (extras && extras.extraText) {
    parts.push(extras.extraText);
  }
  $('topology-summary').textContent = parts.join(' | ');
}

function modeLabelForTopology(mode) {
  if (mode === 'radial') {
    return 'Radial';
  }
  if (mode === 'classic') {
    return 'Classic';
  }
  return 'Minimal';
}

function getVisibleTopologyNodesBase() {
  return Object.values(state.nodes).filter(node => {
    if (node.direction === 'seed') {
      return true;
    }
    if (Math.abs(node.depth || 0) < state.topologyView.minLevel) {
      return false;
    }
    if (Math.abs(node.depth || 0) > state.topologyView.maxLevel) {
      return false;
    }
    if (node.direction === 'parent') {
      return state.topologyView.showParents;
    }
    if (node.direction === 'child') {
      return state.topologyView.showChildren;
    }
    return true;
  });
}

function getVisibleTopologyNodes() {
  return getVisibleTopologyNodesBase().filter(node => matchesTopologyNodeFillFilter(node));
}

function getTopologyNodeFillKind(node) {
  const isSeed = node.direction === 'seed';
  const hasDescendants = Array.isArray(node.childIds) && node.childIds.length > 0;
  return isSeed || hasDescendants ? 'filled' : 'hollow';
}

function matchesTopologyNodeFillFilter(node) {
  const filter = state.topologyView.nodeFillFilter || 'all';
  if (filter === 'all' || node.direction === 'seed') {
    return true;
  }
  return getTopologyNodeFillKind(node) === filter;
}

function groupNodesBySignedDepth(nodes) {
  const nodeIds = new Set(nodes.map(node => node.id));
  const signedDepthById = {};

  const assignDepth = (nodeId) => {
    if (signedDepthById[nodeId] !== undefined) {
      return signedDepthById[nodeId];
    }
    const node = state.nodes[nodeId];
    if (!node) {
      return 0;
    }
    if (!node.parentId || node.direction === 'seed') {
      signedDepthById[nodeId] = 0;
      return 0;
    }

    const parentDepth = assignDepth(node.parentId);
    const signedDepth = node.direction === 'child'
      ? parentDepth - 1
      : parentDepth + 1;
    signedDepthById[nodeId] = signedDepth;
    return signedDepth;
  };

  nodes.forEach(node => assignDepth(node.id));

  const byDepth = {};
  nodes.forEach(node => {
    if (node.parentId && !nodeIds.has(node.parentId)) {
      return;
    }
    const depth = signedDepthById[node.id] ?? 0;
    if (!byDepth[depth]) {
      byDepth[depth] = [];
    }
    byDepth[depth].push(node);
  });

  return Object.keys(byDepth)
    .map(depth => Number(depth))
    .sort((a, b) => a - b)
    .map(depth => {
      const col = byDepth[depth].sort((a, b) => a.object.localeCompare(b.object));
      col.depth = depth;
      return col;
    });
}

function getSignedTopologyDepth(nodeId) {
  const node = state.nodes[nodeId];
  if (!node || !node.parentId || node.direction === 'seed') {
    return 0;
  }
  const parentDepth = getSignedTopologyDepth(node.parentId);
  return node.direction === 'child' ? parentDepth - 1 : parentDepth + 1;
}

function getTopologyBranchDirection(nodeId) {
  const node = state.nodes[nodeId];
  if (!node) {
    return null;
  }
  if (!node.parentId || node.direction === 'seed') {
    return 'seed';
  }
  const parentBranch = getTopologyBranchDirection(node.parentId);
  if (parentBranch === 'seed') {
    return node.direction === 'parent' || node.direction === 'child' ? node.direction : null;
  }
  if (parentBranch !== node.direction) {
    return 'mixed';
  }
  return parentBranch;
}

function getRadialTopologyNodes(nodes, scope = 'children') {
  return nodes.filter(node => {
    const branch = getTopologyBranchDirection(node.id);
    if (branch === 'seed') {
      return true;
    }
    return scope === 'parents' ? branch === 'parent' : branch === 'child';
  });
}

function appendTopologyLaneLabel(svg, x, y, text) {
  const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
  label.setAttribute('class', 'topology-lane-label');
  label.setAttribute('x', x + 8);
  label.setAttribute('y', y);
  label.textContent = text;
  svg.appendChild(label);
}

function appendTopologySideLabel(svg, x, y, text) {
  const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
  label.setAttribute('class', 'topology-side-label');
  label.setAttribute('x', x);
  label.setAttribute('y', y);
  label.textContent = text;
  svg.appendChild(label);
}

function buildCurvePath(x1, y1, x2, y2) {
  const delta = Math.max(40, (x2 - x1) / 2);
  return `M ${x1} ${y1} C ${x1 + delta} ${y1}, ${x2 - delta} ${y2}, ${x2} ${y2}`;
}

function buildRadialCurvePath(centerX, centerY, x1, y1, x2, y2) {
  const control1X = x1 + (centerX - x1) * 0.35;
  const control1Y = y1 + (centerY - y1) * 0.35;
  const control2X = x2 + (centerX - x2) * 0.35;
  const control2Y = y2 + (centerY - y2) * 0.35;
  return `M ${x1} ${y1} C ${control1X} ${control1Y}, ${control2X} ${control2Y}, ${x2} ${y2}`;
}

function shortenTopologyLabel(value, maxLength = 18) {
  const text = String(value || '');
  if (text.length <= maxLength) {
    return text;
  }
  return `${text.slice(0, Math.max(0, maxLength - 2))}..`;
}

function normalizeTopologyLevel(value, fallback = 0) {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed) || parsed < 0) {
    return fallback;
  }
  return parsed;
}

function toggleTopologyFocus(nodeId) {
  const current = state.topologyView.focusNodeIds || [];
  if (current.includes(nodeId)) {
    state.topologyView.focusNodeIds = current.filter(id => id !== nodeId);
    return;
  }
  state.topologyView.focusNodeIds = [...current, nodeId].slice(-2);
}

function computeTopologyFocusState(nodes) {
  const visibleIds = new Set(nodes.map(node => node.id));
  const requested = (state.topologyView.focusNodeIds || []).filter(id => visibleIds.has(id));
  const nodeSet = new Set(requested);
  const edgeSet = new Set();
  if (!requested.length) {
    return { hasFocus: false, nodes: nodeSet, edges: edgeSet };
  }

  const visibleNodes = {};
  nodes.forEach(node => {
    visibleNodes[node.id] = node;
  });

  if (requested.length === 1) {
    const focusId = requested[0];
    nodes.forEach(node => {
      if (node.id === focusId || node.parentId === focusId || visibleNodes[focusId]?.parentId === node.id) {
        nodeSet.add(node.id);
        if (node.parentId === focusId || node.id === focusId) {
          edgeSet.add(node.id);
        }
        if (visibleNodes[focusId]?.parentId === node.id) {
          edgeSet.add(focusId);
        }
      }
    });
    return { hasFocus: true, nodes: nodeSet, edges: edgeSet };
  }

  const [startId, endId] = requested;
  const pathIds = findTopologyPath(startId, endId, visibleNodes);
  pathIds.forEach(id => nodeSet.add(id));
  pathIds.forEach(id => {
    if (visibleNodes[id]?.parentId && nodeSet.has(visibleNodes[id].parentId)) {
      edgeSet.add(id);
    }
  });
  return { hasFocus: true, nodes: nodeSet, edges: edgeSet };
}

function findTopologyPath(startId, endId, visibleNodes) {
  if (startId === endId) {
    return [startId];
  }
  const queue = [[startId]];
  const visited = new Set([startId]);
  while (queue.length) {
    const path = queue.shift();
    const current = path[path.length - 1];
    const neighbors = [];
    const currentNode = visibleNodes[current];
    if (currentNode?.parentId && visibleNodes[currentNode.parentId]) {
      neighbors.push(currentNode.parentId);
    }
    Object.values(visibleNodes).forEach(node => {
      if (node.parentId === current) {
        neighbors.push(node.id);
      }
    });
    for (const neighbor of neighbors) {
      if (visited.has(neighbor)) {
        continue;
      }
      const nextPath = [...path, neighbor];
      if (neighbor === endId) {
        return nextPath;
      }
      visited.add(neighbor);
      queue.push(nextPath);
    }
  }
  return [startId, endId];
}

function getTopologyLevelColor(level) {
  const palette = ['#4f8ef7', '#2dd4bf', '#f6c945', '#fb7185', '#a78bfa', '#60a5fa', '#34d399'];
  return palette[Math.abs(level) % palette.length];
}

function describeTopologyRelation(node) {
  if (node.direction === 'seed') {
    return 'Seed object';
  }
  const parent = node.parentId ? state.nodes[node.parentId] : null;
  if (node.direction === 'parent') {
    return `${parent?.object || '?'}.${node.via_field || '-'} -> ${node.object}`;
  }
  if (node.direction === 'child') {
    return `${node.object}.${node.via_field || '-'} -> ${parent?.object || '?'}`;
  }
  return `${node.direction} via ${node.via_field || '-'}`;
}

function objectCacheKey(objectName) {
  return `${state.org}:${objectName}`;
}

function getCachedObjectData(objectName) {
  return state.objectCache[objectCacheKey(objectName)];
}

function escJs(value) {
  return String(value || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'");
}

function escHtml(value) {
  return String(value || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
