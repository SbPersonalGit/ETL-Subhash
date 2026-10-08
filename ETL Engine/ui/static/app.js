/* ── State ──────────────────────────────────────────────────────── */
const state = {
  org: 'source',
  allObjects: [],
  // Tree nodes: nodeId -> { id, object, label, parentId, direction, via_field, depth, childIds }
  nodes: {},
  rootId: null,
  selectedNodeId: null,
  // Per-node direction toggle: nodeId -> null | 'parents' | 'children'
  nodeDirection: {},
  // Per-object field data cache
  objectCache: {},
  // Per-object selections: objectName -> { selectedFields: Set, queryFilter, externalId }
  selections: {},
  maxDepth: 2,
};

let addModalContext = null; // { nodeId, mode: 'parents'|'children' }

/* ── DOM refs ───────────────────────────────────────────────────── */
const $ = id => document.getElementById(id);

/* ── Init ───────────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  checkAuth();
  loadObjects();
  $('org-select').addEventListener('change', () => { state.org = $('org-select').value; loadObjects(); });
  $('search-objects').addEventListener('input', e => filterObjects(e.target.value));
  $('max-depth').addEventListener('change', e => { state.maxDepth = +e.target.value; });
  $('btn-generate').addEventListener('click', generateConfig);
  $('btn-generate-bottom').addEventListener('click', generateConfig);
  $('modal-close').addEventListener('click', () => $('modal-overlay').style.display = 'none');
  $('btn-copy').addEventListener('click', () => navigator.clipboard.writeText($('yaml-output').textContent));
  $('btn-save').addEventListener('click', saveConfig);
  $('fh-filter').addEventListener('change', e => updateNodeFilter(e.target.value));
  $('fh-extid').addEventListener('change',  e => updateNodeExtId(e.target.value));
});

/* ── Auth ───────────────────────────────────────────────────────── */
async function checkAuth() {
  const el = $('auth-status');
  try {
    const data = await fetch('/api/auth/check').then(r => r.json());
    if (data.source?.status === 'ok') {
      el.className = 'auth-status ok';
      el.innerHTML = `<span class="dot"></span> Connected — ${data.source.instance_url.replace('https://','')}`;
    } else throw new Error(data.source?.message);
  } catch (e) {
    el.className = 'auth-status error';
    el.innerHTML = `<span class="dot"></span> Auth failed`;
  }
}

/* ── Object List ────────────────────────────────────────────────── */
async function loadObjects() {
  $('object-list').innerHTML = '<div class="loading"><span class="spinner"></span> Loading...</div>';
  try {
    const data = await fetch(`/api/objects?org=${state.org}`).then(r => r.json());
    if (data.error) throw new Error(data.error);
    state.allObjects = data.objects;
    $('object-count').textContent = data.objects.length;
    renderObjectList(data.objects);
  } catch (e) {
    $('object-list').innerHTML = `<div class="loading" style="color:var(--red)">Error: ${e.message}</div>`;
  }
}

function renderObjectList(objects) {
  $('object-list').innerHTML = objects.map(o => `
    <div class="object-item" onclick="selectSeed('${o.name}','${esc(o.label)}')">
      <span class="obj-name">${o.name}</span>
      <span class="obj-label">${o.label}</span>
    </div>`).join('');
}

function filterObjects(q) {
  const lq = q.toLowerCase();
  renderObjectList(state.allObjects.filter(o =>
    o.name.toLowerCase().includes(lq) || o.label.toLowerCase().includes(lq)));
}

/* ── Select Seed ─────────────────────────────────────────────────── */
async function selectSeed(objectName, label) {
  state.nodes = {};
  state.rootId = null;
  state.selectedNodeId = null;
  state.nodeDirection = {};

  const data = await fetchObjectData(objectName);
  if (!data) return;

  const nodeId = 'n0';
  state.nodes[nodeId] = {
    id: nodeId, object: objectName, label,
    parentId: null, direction: 'seed', via_field: null, depth: 0, childIds: [],
  };
  state.rootId = nodeId;

  $('tree-title').textContent = objectName;
  $('object-browser').style.display = 'none';
  $('tree-panel').style.display = '';
  $('bottombar').style.display = 'block';
  $('btn-generate').disabled = false;

  renderTree();
  selectNode(nodeId);
}

function changeSeed() {
  $('object-browser').style.display = '';
  $('tree-panel').style.display = 'none';
  $('fields-detail').style.display = 'none';
  $('fields-empty').style.display = '';
  $('bottombar').style.display = 'none';
  $('btn-generate').disabled = true;
}

/* ── Fetch Object Data (cached) ──────────────────────────────────── */
async function fetchObjectData(objectName) {
  if (state.objectCache[objectName]) return state.objectCache[objectName];
  try {
    const data = await fetch(`/api/object/${objectName}?org=${state.org}`).then(r => r.json());
    if (data.error) throw new Error(data.error);
    state.objectCache[objectName] = data;
    if (!state.selections[objectName]) {
      state.selections[objectName] = {
        selectedFields: new Set(data.required_fields),
        queryFilter: '',
        externalId: data.external_id_fields[0] || 'External_Id__c',
      };
    }
    return data;
  } catch (e) { alert(`Failed to load ${objectName}: ${e.message}`); return null; }
}

/* ── Tree Rendering ──────────────────────────────────────────────── */
function renderTree() {
  const container = $('tree-container');
  container.innerHTML = '';
  if (state.rootId) renderTreeNode(state.nodes[state.rootId], container, 0);
}

function renderTreeNode(node, container, indent) {
  const data    = state.objectCache[node.object];
  const dir     = state.nodeDirection[node.id] || null;
  const isRoot  = node.parentId === null;
  const selected = state.selectedNodeId === node.id;

  // ── Node row ──
  const row = document.createElement('div');
  row.className = `tree-node-row ${selected ? 'selected' : ''}`;
  row.onclick = () => selectNode(node.id);

  const indentHTML = Array.from({length: indent}, () =>
    `<div class="tree-indent-line"></div>`).join('');

  const icon = isRoot ? '🌱' : node.direction === 'parent' ? '↑' : '↓';
  const meta = node.via_field ? `via ${node.via_field}` : node.label;

  row.innerHTML = `
    <div class="tree-indent">${indentHTML}</div>
    <span class="tree-node-icon">${icon}</span>
    <div class="tree-node-info">
      <div class="tree-node-name">${node.object}</div>
      <div class="tree-node-meta">${meta}</div>
    </div>
    ${!isRoot ? `<button class="btn-icon tree-node-close" title="Remove"
      onclick="event.stopPropagation(); removeNode('${node.id}')">✕</button>` : ''}
  `;
  container.appendChild(row);

  // ── Direction toggle (only show if object data loaded) ──
  if (data) {
    const hasLookups  = data.fields.some(f => f.is_lookup);
    const hasChildren = data.children.length > 0;
    const atLimit     = node.depth >= state.maxDepth;

    // ── Expanded child nodes (always render, regardless of direction) ──
    if (node.childIds.length > 0) {
      node.childIds.forEach(cid => {
        if (state.nodes[cid]) renderTreeNode(state.nodes[cid], container, indent + 1);
      });
    }

    // ── Direction toggle + Add button (hidden at depth limit) ──
    if (!atLimit) {
      const toggle = document.createElement('div');
      toggle.className = 'tree-dir-toggle';
      toggle.innerHTML = `
        <div class="tree-indent">${Array.from({length: indent + 1}, () =>
          `<div class="tree-indent-line"></div>`).join('')}</div>
        <button class="tree-dir-btn ${dir === 'parents' ? 'active-parent' : ''}"
                ${!hasLookups ? 'disabled' : ''}
                onclick="event.stopPropagation(); toggleDirection('${node.id}', 'parents')">
          ↑ Parents
        </button>
        <button class="tree-dir-btn ${dir === 'children' ? 'active-children' : ''}"
                ${!hasChildren ? 'disabled' : ''}
                onclick="event.stopPropagation(); toggleDirection('${node.id}', 'children')">
          ↓ Children
        </button>
      `;
      container.appendChild(toggle);

      if (dir) {
        const addBtn = document.createElement('button');
        addBtn.className = 'tree-add-btn';
        const indentSpan = Array.from({length: indent + 1}, () =>
          `<div class="tree-indent-line"></div>`).join('');
        addBtn.innerHTML = `<div class="tree-indent">${indentSpan}</div>
          <span>+ Add ${dir === 'parents' ? 'Parent' : 'Child'}</span>`;
        addBtn.onclick = (e) => { e.stopPropagation(); openAddModal(node.id, dir); };
        container.appendChild(addBtn);
      }
    } else if (node.childIds.length === 0) {
      // Show depth limit label only when no children already added
      const limitEl = document.createElement('div');
      limitEl.className = 'tree-depth-limit-label';
      limitEl.innerHTML = `<div class="tree-indent">${Array.from({length: indent + 1}, () =>
        `<div class="tree-indent-line"></div>`).join('')}</div>
        <span>Max depth reached</span>`;
      container.appendChild(limitEl);
    }
  }
}

/* ── Direction Toggle ────────────────────────────────────────────── */
function toggleDirection(nodeId, dir) {
  const current = state.nodeDirection[nodeId];
  // Toggle off if same button clicked again, otherwise switch direction
  // Never remove already-added nodes — user removes them manually with ✕
  state.nodeDirection[nodeId] = (current === dir) ? null : dir;
  renderTree();
}

function removeChildNodes(nodeId) {
  const node = state.nodes[nodeId];
  if (!node) return;
  node.childIds.forEach(cid => removeChildNodes(cid)); // recurse
  node.childIds.forEach(cid => delete state.nodes[cid]);
  node.childIds = [];
}

function removeNode(nodeId) {
  const node = state.nodes[nodeId];
  if (!node || !node.parentId) return;
  // Remove from parent's childIds
  const parent = state.nodes[node.parentId];
  if (parent) parent.childIds = parent.childIds.filter(id => id !== nodeId);
  // Remove recursively
  removeChildNodes(nodeId);
  delete state.nodes[nodeId];
  if (state.selectedNodeId === nodeId) {
    state.selectedNodeId = null;
    $('fields-detail').style.display = 'none';
    $('fields-empty').style.display = '';
  }
  renderTree();
}

/* ── Add Node Modal ──────────────────────────────────────────────── */
function openAddModal(parentNodeId, mode) {
  addModalContext = { parentNodeId, mode };
  const parentNode = state.nodes[parentNodeId];
  const data = state.objectCache[parentNode.object];
  if (!data) return;

  $('add-modal-title').textContent = mode === 'parents'
    ? `Add Parent of ${parentNode.object}`
    : `Add Child of ${parentNode.object}`;

  // Build items list
  let items = [];
  if (mode === 'parents') {
    items = data.fields
      .filter(f => f.is_lookup && (f.references_to || []).length >= 1)
      .map(f => ({
        name: f.references_to[0],
        label: `${f.label} (${f.name})`,
        via: f.name,
        refs: f.references_to,
      }));
  } else {
    items = data.children.map(c => ({
      name: c.object,
      label: `via ${c.field}`,
      via: c.field,
      refs: [],
    }));
  }

  renderAddModalList(items);
  $('add-modal-search').value = '';
  $('add-modal').style.display = 'flex';
}

function renderAddModalList(items) {
  $('add-modal-list').innerHTML = items.length
    ? items.map(item => `
        <div class="add-modal-item" onclick="addNode('${esc(item.name)}','${esc(item.label)}','${esc(item.via)}')">
          <span class="item-name">${item.name}</span>
          <span class="item-label">${item.label}</span>
        </div>`).join('')
    : '<div class="loading">No items available</div>';
}

function filterAddModal(q) {
  if (!addModalContext) return;
  const parentNode = state.nodes[addModalContext.parentNodeId];
  const data = state.objectCache[parentNode.object];
  const lq = q.toLowerCase();
  let items = [];
  if (addModalContext.mode === 'parents') {
    items = data.fields
      .filter(f => f.is_lookup && (f.references_to || []).length >= 1)
      .filter(f => f.name.toLowerCase().includes(lq) || f.label.toLowerCase().includes(lq) || (f.references_to[0]||'').toLowerCase().includes(lq))
      .map(f => ({ name: f.references_to[0], label: `${f.label} (${f.name})`, via: f.name }));
  } else {
    items = data.children
      .filter(c => c.object.toLowerCase().includes(lq) || c.field.toLowerCase().includes(lq))
      .map(c => ({ name: c.object, label: `via ${c.field}`, via: c.field }));
  }
  renderAddModalList(items);
}

async function addNode(objectName, label, viaField) {
  closeAddModal();
  const { parentNodeId, mode } = addModalContext || {};
  if (!parentNodeId) return;

  const parentNode = state.nodes[parentNodeId];
  const direction  = mode === 'parents' ? 'parent' : 'child';
  const newDepth   = parentNode.depth + 1;

  // Prevent duplicate: same object + same via_field under same parent
  const alreadyExists = parentNode.childIds.some(cid => {
    const n = state.nodes[cid];
    return n && n.object === objectName && n.via_field === viaField;
  });
  if (alreadyExists) {
    alert(`${objectName} via ${viaField} is already added.`);
    return;
  }

  const newId = `n${Date.now()}`;

  const data = await fetchObjectData(objectName);
  if (!data) return;

  state.nodes[newId] = {
    id: newId, object: objectName, label: objectName,
    parentId: parentNodeId, direction, via_field: viaField,
    depth: newDepth, childIds: [],
  };
  parentNode.childIds.push(newId);

  renderTree();
  selectNode(newId);
}

function closeAddModal() {
  $('add-modal').style.display = 'none';
}

/* ── Select Node → Show Fields ───────────────────────────────────── */
function selectNode(nodeId) {
  state.selectedNodeId = nodeId;
  const node = state.nodes[nodeId];
  const data = state.objectCache[node.object];
  const sel  = state.selections[node.object];

  $('fields-empty').style.display  = 'none';
  $('fields-detail').style.display = '';

  // Header
  $('fh-object-name').textContent = node.object;
  const pathParts = [];
  let cur = node;
  while (cur) { pathParts.unshift(cur.object); cur = state.nodes[cur.parentId]; }
  $('fh-breadcrumb').textContent = pathParts.join(' → ');

  $('fh-filter').value = sel?.queryFilter || '';
  $('fh-extid').value  = sel?.externalId  || '';
  $('field-search').value = '';

  renderFieldsPanel(data, sel);
  renderTree(); // refresh selected highlight
}

function renderFieldsPanel(data, sel) {
  const body = $('fields-body');
  body.innerHTML = '';

  const addLabel = (text) => {
    const d = document.createElement('div');
    d.className = 'section-label';
    d.textContent = text;
    body.appendChild(d);
  };

  const addField = (f, force) => {
    const checked = force || sel.selectedFields.has(f.name);
    const row = document.createElement('div');
    row.className = 'field-row';
    row.dataset.field = f.name;
    let tags = '';
    if (f.required)        tags += `<span class="field-tag tag-required">required</span>`;
    if (f.is_external_id)  tags += `<span class="field-tag tag-extid">ext-id</span>`;
    if (f.is_lookup && !f.required) tags += `<span class="field-tag tag-lookup">lookup</span>`;
    row.innerHTML = `
      <input type="checkbox" class="field-check"
             ${checked ? 'checked' : ''} ${force ? 'disabled' : ''}
             onchange="toggleField('${f.name}', this.checked)" />
      <div class="field-info">
        <div class="field-name">${f.name}</div>
        <div class="field-label">${f.label} · ${f.type}</div>
      </div>
      ${tags}`;
    body.appendChild(row);
  };

  // Required
  const req = data.fields.filter(f => f.required);
  if (req.length) { addLabel('Required'); req.forEach(f => addField(f, true)); }

  // Lookups
  const lookups = data.fields.filter(f => f.is_lookup && !f.required);
  if (lookups.length) { addLabel('Lookups'); lookups.forEach(f => addField(f, false)); }

  // Other fields
  const others = data.fields.filter(f => !f.required && !f.is_lookup);
  if (others.length) { addLabel('Fields'); others.forEach(f => addField(f, false)); }
}

/* ── Field Panel Actions ──────────────────────────────────────────── */
function toggleField(fieldName, checked) {
  const node = state.nodes[state.selectedNodeId];
  if (!node) return;
  const sel = state.selections[node.object];
  if (!sel) return;
  if (checked) sel.selectedFields.add(fieldName);
  else         sel.selectedFields.delete(fieldName);
}

function updateNodeFilter(value) {
  const node = state.nodes[state.selectedNodeId];
  if (!node) return;
  state.selections[node.object].queryFilter = value;
}

function updateNodeExtId(value) {
  const node = state.nodes[state.selectedNodeId];
  if (!node) return;
  state.selections[node.object].externalId = value;
}

function selectAllFieldsPanel(checked) {
  const node = state.nodes[state.selectedNodeId];
  if (!node) return;
  const sel = state.selections[node.object];
  $('fields-body').querySelectorAll('.field-check:not(:disabled)').forEach(cb => {
    cb.checked = checked;
    const fieldName = cb.closest('.field-row')?.dataset.field;
    if (fieldName) { if (checked) sel.selectedFields.add(fieldName); else sel.selectedFields.delete(fieldName); }
  });
}

function filterFieldsPanel(q) {
  const lq = q.toLowerCase();
  $('fields-body').querySelectorAll('.field-row').forEach(row => {
    if (!lq) { row.style.display = ''; return; }
    const name  = (row.dataset.field || '').toLowerCase();
    const label = row.querySelector('.field-label')?.textContent.toLowerCase() || '';
    row.style.display = (name.includes(lq) || label.includes(lq)) ? '' : 'none';
  });
  $('fields-body').querySelectorAll('.section-label').forEach(lbl => {
    let next = lbl.nextElementSibling, visible = false;
    while (next && !next.classList.contains('section-label')) {
      if (next.style.display !== 'none') { visible = true; break; }
      next = next.nextElementSibling;
    }
    lbl.style.display = visible ? '' : 'none';
  });
}

/* ── Generate Config ─────────────────────────────────────────────── */
async function generateConfig() {
  const allNodes = Object.values(state.nodes);
  const selections = allNodes.map(node => ({
    object: node.object,
    selected_fields: [...(state.selections[node.object]?.selectedFields || [])],
    query_filter: state.selections[node.object]?.queryFilter || '',
    external_id: state.selections[node.object]?.externalId || 'External_Id__c',
  }));

  const parents  = allNodes.filter(n => n.direction === 'parent').map(n => ({ object: n.object, via_field: n.via_field, depth: n.depth }));
  const children = allNodes.filter(n => n.direction === 'child').map(n => ({ object: n.object, via_field: n.via_field, depth: n.depth }));

  const seed = state.nodes[state.rootId];
  const payload = {
    seed_object: seed.object,
    seed_filter: $('seed-filter').value,
    max_records: parseInt($('max-records').value) || 10000,
    migration_name: $('migration-name').value || 'Custom Migration',
    selections,
    traversal: { parents, children },
  };

  const data = await fetch('/api/config/generate', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  }).then(r => r.json());

  if (data.error) { alert(data.error); return; }
  $('yaml-output').textContent = data.yaml;
  $('modal-overlay').style.display = 'flex';
}

async function saveConfig() {
  const seed = state.nodes[state.rootId];
  const allNodes = Object.values(state.nodes);
  const payload = {
    seed_object: seed.object,
    seed_filter: $('seed-filter').value,
    max_records: parseInt($('max-records').value) || 10000,
    migration_name: $('migration-name').value || 'Custom Migration',
    selections: allNodes.map(n => ({
      object: n.object,
      selected_fields: [...(state.selections[n.object]?.selectedFields || [])],
      query_filter: state.selections[n.object]?.queryFilter || '',
      external_id: state.selections[n.object]?.externalId || 'External_Id__c',
    })),
    traversal: {
      parents:  allNodes.filter(n=>n.direction==='parent').map(n=>({object:n.object,via_field:n.via_field,depth:n.depth})),
      children: allNodes.filter(n=>n.direction==='child').map(n=>({object:n.object,via_field:n.via_field,depth:n.depth})),
    },
    save_to_file: true,
  };
  const data = await fetch('/api/config/generate', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  }).then(r => r.json());
  if (data.error) { alert(data.error); return; }
  alert('Saved to config/migration.yaml');
  $('modal-overlay').style.display = 'none';
}

/* ── Util ────────────────────────────────────────────────────────── */
function esc(s) {
  return String(s||'').replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/"/g,'&quot;');
}
