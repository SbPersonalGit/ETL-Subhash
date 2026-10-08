const $ = id => document.getElementById(id);

let healthPayload = null;
let selectedTemplateIds = new Set();
let expandedTemplateIds = new Set();
let filtersBound = false;
let lastArchivePackageId = window.sessionStorage.getItem('storageCleanupPackageId') || null;
let lastImpact = null;
let showFiles = false;
let templateSort = { key: 'created', direction: 'asc' };
let storageLoaderTimer = null;
let reviewBypassed = false;
let archiveBypassed = false;

function linkCleanupPackage(packageId) {
  lastArchivePackageId = String(packageId || '').trim() || null;
  if (lastArchivePackageId) window.sessionStorage.setItem('storageCleanupPackageId', lastArchivePackageId);
  else window.sessionStorage.removeItem('storageCleanupPackageId');
}

function initStorageHealthPage() {
  bindFilters();
  loadHealth();
  loadArchives();
}

function showStorageLoader(title, detail) {
  const loader = $('storage-loader');
  if (!loader) return;
  const titleEl = $('storage-loader-title');
  const detailEl = $('storage-loader-detail');
  if (titleEl) titleEl.textContent = title || 'Scanning storage...';
  if (detailEl) detailEl.textContent = detail || 'Calculating local disk usage.';
  const elapsedEl = $('storage-loader-elapsed');
  const startedAt = Date.now();
  if (storageLoaderTimer) window.clearInterval(storageLoaderTimer);
  if (elapsedEl) elapsedEl.textContent = 'Elapsed: 0s';
  storageLoaderTimer = window.setInterval(() => {
    if (elapsedEl) elapsedEl.textContent = `Elapsed: ${Math.floor((Date.now() - startedAt) / 1000)}s`;
  }, 1000);
  loader.hidden = false;
  const main = document.querySelector('main');
  if (main) main.setAttribute('aria-busy', 'true');
}

function hideStorageLoader() {
  if (storageLoaderTimer) window.clearInterval(storageLoaderTimer);
  storageLoaderTimer = null;
  const loader = $('storage-loader');
  if (loader) loader.hidden = true;
  const main = document.querySelector('main');
  if (main) main.removeAttribute('aria-busy');
}

function bindFilters() {
  if (filtersBound) return;
  const search = $('storage-search');
  const eligibility = $('storage-eligibility-filter');
  const selectAll = $('storage-select-all');
  const refresh = $('storage-refresh');
  const cleanup = $('storage-cleanup');
  const archive = $('storage-archive');
  const review = $('storage-review');
  const bypassReview = $('storage-bypass-review');
  const bypassArchive = $('storage-bypass-archive');
  const toggleFiles = $('storage-toggle-files');
  if (!search || !eligibility || !selectAll || !refresh || !cleanup || !archive || !review || !bypassReview || !bypassArchive) return;

  search.addEventListener('input', applyFilters);
  search.addEventListener('keyup', applyFilters);
  eligibility.addEventListener('change', applyFilters);
  document.querySelectorAll('.storage-column-filters input').forEach(input => {
    input.addEventListener('input', applyFilters);
    input.addEventListener('change', applyFilters);
  });
  document.querySelectorAll('button[data-sort-key]').forEach(button => {
    button.addEventListener('click', () => {
      const key = button.getAttribute('data-sort-key');
      templateSort = templateSort.key === key
        ? { key, direction: templateSort.direction === 'asc' ? 'desc' : 'asc' }
        : { key, direction: ['disk_used', 'can_clean'].includes(key) ? 'desc' : 'asc' };
      renderTemplates();
      syncSortIndicators();
    });
  });
  const clearColumnFilters = $('storage-clear-column-filters');
  if (clearColumnFilters) clearColumnFilters.addEventListener('click', () => {
    document.querySelectorAll('.storage-column-filters input').forEach(input => { input.value = ''; });
    applyFilters();
  });
  selectAll.addEventListener('change', event => {
    const allowed = visibleTemplates().filter(row => row.cleanup_allowed);
    if (event.target.checked) allowed.forEach(row => selectedTemplateIds.add(row.template_id));
    else allowed.forEach(row => selectedTemplateIds.delete(row.template_id));
    lastImpact = null;
    reviewBypassed = false;
    archiveBypassed = false;
    renderImpact(null);
    renderTemplates();
    syncActionButtons();
  });
  refresh.addEventListener('click', async () => {
    await loadHealth();
    await loadArchives();
  });
  review.addEventListener('click', reviewImpact);
  bypassReview.addEventListener('click', () => {
    if (!window.confirm('Bypass dependency review for this selection? Protected/active items will still be rejected by the server.')) return;
    reviewBypassed = true;
    lastImpact = null;
    renderImpact({can_cleanup: true, blockers: [], warnings: [], suggested_related_items: [], review_bypassed: true});
    setBanner('checking', 'Dependency review bypassed by user. Archive is still recommended before cleanup.');
    syncActionButtons();
  });
  bypassArchive.addEventListener('click', () => {
    if (!window.confirm('Bypass archive for this selection? Cleanup will permanently delete local files with no OrgShift restore package.')) return;
    archiveBypassed = true;
    linkCleanupPackage(null);
    setBanner('error', 'Archive bypassed by user. Cleanup will be irreversible.');
    syncActionButtons();
    loadArchives();
  });
  archive.addEventListener('click', archiveSelected);
  cleanup.addEventListener('click', cleanupSelected);
  if (toggleFiles) {
    toggleFiles.addEventListener('click', () => {
      showFiles = !showFiles;
      toggleFiles.textContent = showFiles ? 'Hide individual files' : 'Show individual files';
      const wrap = $('storage-files-wrap');
      if (wrap) wrap.hidden = !showFiles;
      if (showFiles) renderFiles();
    });
  }
  filtersBound = true;
}

function applyFilters() {
  try {
    renderTemplates();
    syncActionButtons();
    if (showFiles) renderFiles();
  } catch (error) {
    setBanner('error', error.message || String(error));
  }
}

function selectedItemIds() {
  const selected = selectedTemplateIds;
  const ids = new Set();
  ((healthPayload && healthPayload.templates) || []).forEach(group => {
    if (!selected.has(group.template_id)) return;
    (group.item_ids || []).forEach(id => ids.add(id));
  });
  ((healthPayload && healthPayload.items) || []).forEach(item => {
    if (!item.cleanup_allowed) return;
    if (item.category === 'templates' || item.category === 'flat_file_drop') return;
    const owners = item.template_ids || [];
    if (owners.length <= 1) return;
    if (owners.every(id => selected.has(id))) ids.add(item.id);
  });
  return [...ids];
}

async function loadHealth() {
  showStorageLoader(
    'Scanning storage...',
    'Calculating template, run, validation, and archive sizes. Large workspaces may take a moment.'
  );
  setBanner('checking', 'Scanning local disk usage...');
  try {
    const response = await fetch('/api/storage/health', { cache: 'no-store' });
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || 'Unable to load storage health');
    healthPayload = data;
    const known = new Set((data.templates || []).map(row => row.template_id));
    selectedTemplateIds = new Set([...selectedTemplateIds].filter(id => known.has(id)));
    renderSummary();
    renderBackup();
    renderCategories();
    renderTemplates();
    syncActionButtons();
    if (showFiles) renderFiles();
    const totals = data.totals || {};
    setBanner(
      data.health === 'critical' ? 'error' : data.health === 'warning' ? 'checking' : 'ok',
      `${data.health_message || 'Storage scanned'} · tracked ${totals.tracked_human || '0 B'} · reclaimable ${totals.template_reclaimable_human || totals.reclaimable_human || '0 B'}`
    );
  } catch (error) {
    setBanner('error', error.message || String(error));
  } finally {
    hideStorageLoader();
  }
}

async function loadArchives() {
  try {
    const response = await fetch('/api/storage/archives', { cache: 'no-store' });
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || 'Unable to load archives');
    const body = $('storage-archives-body');
    const packages = data.packages || [];
    body.innerHTML = packages.length ? packages.map(pkg => `<tr>
      <td><strong>${esc(pkg.label || pkg.package_id)}</strong><small>${esc(pkg.package_id)}</small><small>${esc(pkg.path || pkg.archive_root || '')}</small></td>
      <td>${esc(formatDate(pkg.created_at))}</td>
      <td>${esc(pkg.bytes_human || '0 B')}</td>
      <td>${esc(pkg.item_count || 0)}</td>
      <td><button class="btn btn-secondary btn-sm" type="button" data-restore-id="${escAttr(pkg.package_id)}">Restore</button> <button class="btn btn-secondary btn-sm" type="button" data-use-id="${escAttr(pkg.package_id)}" ${lastArchivePackageId === pkg.package_id ? 'disabled' : ''}>${lastArchivePackageId === pkg.package_id ? 'Linked for cleanup' : 'Use for cleanup'}</button></td>
    </tr>`).join('') : '<tr><td colspan="5">No archive packages yet.</td></tr>';
    body.querySelectorAll('button[data-restore-id]').forEach(button => {
      button.addEventListener('click', () => restorePackage(button.getAttribute('data-restore-id')));
    });
    body.querySelectorAll('button[data-use-id]').forEach(button => {
      button.addEventListener('click', () => {
        const packageId = button.getAttribute('data-use-id');
        linkCleanupPackage(packageId);
        archiveBypassed = false;
        loadArchives();
        syncActionButtons();
        setBanner('ok', `Backup ${packageId} linked. Select its templates, review impact, then click Clean selected.`);
      });
    });
  } catch (error) {
    $('storage-archives-body').innerHTML = `<tr><td colspan="5">${esc(error.message || String(error))}</td></tr>`;
  }
}

function renderSummary() {
  const disk = (healthPayload && healthPayload.disk) || {};
  const totals = (healthPayload && healthPayload.totals) || {};
  $('storage-summary').innerHTML = [
    card('Disk free', disk.free_human || 'n/a', `${disk.used_percent || 0}% used`, 'disk'),
    card('Templates', String(totals.template_count || 0), `${((healthPayload && healthPayload.templates) || []).length} group(s)`, 'templates'),
    card('Can clean now', totals.template_reclaimable_human || totals.reclaimable_human || '0 B', 'Old run/validation files for selected templates', 'reclaimable'),
    card('Health', String((healthPayload && healthPayload.health) || 'unknown').toUpperCase(), (healthPayload && healthPayload.health_message) || '', `health-${String((healthPayload && healthPayload.health) || 'unknown').toLowerCase()}`),
  ].join('');
}

function card(title, value, detail, tone) {
  return `<article class="storage-summary-card ${escAttr(tone || '')}"><h3>${esc(title)}</h3><div class="storage-summary-value">${esc(value)}</div><p>${esc(detail)}</p></article>`;
}

function renderBackup() {
  const panel = $('storage-backup');
  if (!panel) return;
  const strategy = (healthPayload && healthPayload.backup_strategy) || {};
  const archive = (healthPayload && healthPayload.archive) || {};
  const steps = strategy.steps || [];
  const current = archive.archive_root || archive.default_workspace_root || '';
  const warning = archive.inside_workspace
    ? 'This folder is inside the workspace. If this project folder is deleted, these backups go with it. Prefer a path such as D:\\ETL_Backups.'
    : 'Backups stay in this folder so Restore/rollback can put files back later.';
  panel.innerHTML = `<details class="storage-backup-details">
    <summary><span><strong>Backup strategy</strong><small>${esc(current || 'No backup folder configured')}</small></span><span class="storage-disclosure-hint">Show details</span></summary>
    <div class="storage-backup-content">
      <p>${esc(strategy.summary || 'Cleanup archives selected files first, then deletes the working copies.')}</p>
      <label for="storage-archive-path">Backup folder</label>
      <div class="storage-backup-path-row">
        <input id="storage-archive-path" class="fh-input" type="text" spellcheck="false" value="${escAttr(current)}" placeholder="D:\\ETL_Backups\\OrgShift" />
        <button id="storage-save-location" class="btn btn-secondary btn-sm" type="button">Save location</button>
        <button id="storage-use-workspace" class="btn btn-secondary btn-sm" type="button">Use this workspace</button>
      </div>
      <p class="storage-backup-note ${archive.inside_workspace ? 'warn' : ''}">${esc(warning)}</p>
      ${steps.length ? `<ol>${steps.map(step => `<li>${esc(step)}</li>`).join('')}</ol>` : ''}
    </div>
  </details>`;
  const save = $('storage-save-location');
  const useWorkspace = $('storage-use-workspace');
  if (save) save.addEventListener('click', () => saveArchiveLocation());
  if (useWorkspace) {
    useWorkspace.addEventListener('click', () => {
      const input = $('storage-archive-path');
      if (input) input.value = archive.default_workspace_root || '';
      saveArchiveLocation(archive.default_workspace_root || 'runtime/archive');
    });
  }
}

async function saveArchiveLocation(explicitPath) {
  const input = $('storage-archive-path');
  const archiveRoot = String(explicitPath || (input && input.value) || '').trim();
  if (!archiveRoot) {
    setBanner('error', 'Enter a backup folder path first.');
    return null;
  }
  showStorageLoader('Validating backup location...', `Checking write access to ${archiveRoot}`);
  setBanner('checking', 'Saving backup folder...');
  try {
    const response = await fetch('/api/storage/archive-location', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ archive_root: archiveRoot }),
    });
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || 'Unable to save backup folder');
    if (data.health) healthPayload = data.health;
    renderSummary();
    renderBackup();
    renderCategories();
    await loadArchives();
    setBanner('ok', `Backup folder set to ${((data.archive || {}).archive_root) || archiveRoot}`);
    return (data.archive || {}).archive_root || archiveRoot;
  } catch (error) {
    setBanner('error', error.message || String(error));
    return null;
  } finally {
    hideStorageLoader();
  }
}

async function currentArchiveRoot() {
  const input = $('storage-archive-path');
  const typed = String((input && input.value) || '').trim();
  const saved = ((healthPayload && healthPayload.archive) || {}).archive_root || '';
  if (typed && typed !== saved) {
    return await saveArchiveLocation(typed);
  }
  return typed || saved;
}

function renderCategories() {
  const rows = (healthPayload && healthPayload.categories) || [];
  $('storage-categories').innerHTML = rows.length
    ? rows.map(row => `<article class="storage-category-card"><h3>${esc(row.label)}</h3><div class="storage-summary-value">${esc(row.size_human)}</div><p>${row.item_count} item(s) · reclaimable ${esc(row.cleanup_allowed_human)}</p></article>`).join('')
    : '<p class="storage-empty">No tracked artifact categories found.</p>';
}

function visibleTemplates() {
  const searchEl = $('storage-search');
  const eligibilityEl = $('storage-eligibility-filter');
  const query = ((searchEl && searchEl.value) || '').trim().toLowerCase();
  const eligibility = (eligibilityEl && eligibilityEl.value) || '';
  const templateFilter = String(($('storage-filter-template') && $('storage-filter-template').value) || '').trim().toLowerCase();
  const createdFilter = String(($('storage-filter-created') && $('storage-filter-created').value) || '').trim();
  const usedInFilter = String(($('storage-filter-used-in') && $('storage-filter-used-in').value) || '').trim().toLowerCase();
  const diskUsedFilter = Number(($('storage-filter-disk-used') && $('storage-filter-disk-used').value) || 0) * 1024 * 1024;
  const canCleanFilter = Number(($('storage-filter-can-clean') && $('storage-filter-can-clean').value) || 0) * 1024 * 1024;
  const statusFilter = String(($('storage-filter-status') && $('storage-filter-status').value) || '').trim().toLowerCase();
  const rows = (healthPayload && healthPayload.templates) || [];
  const visible = rows.filter(row => {
    if (eligibility === 'allowed' && !row.cleanup_allowed) return false;
    if (eligibility === 'protected' && row.cleanup_allowed) return false;
    const haystack = [
      row.name,
      row.template_id,
      row.last_status,
      row.recommendation,
      row.created_at,
      ...(row.objects || []),
      ...((row.pipelines || []).map(item => item.pipeline_name)),
    ].join(' ').toLowerCase();
    if (query && !haystack.includes(query)) return false;
    const templateText = [row.name, row.template_id, ...(row.objects || [])].join(' ').toLowerCase();
    const usedInText = (row.pipelines || []).map(item => item.pipeline_name).filter(Boolean).join(' ').toLowerCase();
    const statusText = String(row.last_status || '').toLowerCase();
    if (templateFilter && !templateText.includes(templateFilter)) return false;
    if (usedInFilter && !usedInText.includes(usedInFilter)) return false;
    if (statusFilter && !statusText.includes(statusFilter)) return false;
    if (diskUsedFilter && Number(row.size_bytes || 0) < diskUsedFilter) return false;
    if (canCleanFilter && Number(row.reclaimable_bytes || 0) < canCleanFilter) return false;
    if (createdFilter) {
      const created = Date.parse(row.created_at || '');
      const endOfDay = Date.parse(`${createdFilter}T23:59:59.999`);
      if (!Number.isFinite(created) || created > endOfDay) return false;
    }
    return true;
  });
  const createdValue = row => {
    const value = Date.parse(row.created_at || '');
    return Number.isFinite(value) ? value : Number.POSITIVE_INFINITY;
  };
  const sortValue = row => ({
    template: String(row.name || row.template_id || '').toLowerCase(),
    created: createdValue(row),
    used_in: (row.pipelines || []).map(item => item.pipeline_name).filter(Boolean).join(', ').toLowerCase(),
    disk_used: Number(row.size_bytes || 0),
    can_clean: Number(row.reclaimable_bytes || 0),
    status: String(row.last_status || '').toLowerCase(),
  }[templateSort.key]);
  visible.sort((left, right) => {
    const leftValue = sortValue(left);
    const rightValue = sortValue(right);
    if (leftValue === rightValue) return String(left.name || '').localeCompare(String(right.name || ''));
    if (leftValue === Number.POSITIVE_INFINITY) return 1;
    if (rightValue === Number.POSITIVE_INFINITY) return -1;
    const comparison = typeof leftValue === 'string'
      ? leftValue.localeCompare(rightValue)
      : leftValue - rightValue;
    return templateSort.direction === 'asc' ? comparison : -comparison;
  });
  return visible;
}

function syncSortIndicators() {
  document.querySelectorAll('[data-sort-indicator]').forEach(indicator => {
    indicator.textContent = indicator.getAttribute('data-sort-indicator') === templateSort.key
      ? (templateSort.direction === 'asc' ? '▲' : '▼')
      : '';
  });
}

function artifactList(title, rows) {
  if (!rows || !rows.length) return '';
  return `<div><h4>${esc(title)}</h4><ul>${rows.slice(0, 12).map(row => {
    const extra = row.shared && (row.template_ids || []).length
      ? ` · shared with ${(row.template_ids || []).join(', ')}`
      : '';
    return `<li>${esc(row.kind_label || row.category)} — ${esc(row.size_human)}${esc(extra)}</li>`;
  }).join('')}${rows.length > 12 ? `<li>+${rows.length - 12} more</li>` : ''}</ul></div>`;
}

function workflowStep(label, detail) {
  if (!detail) return `<span class="storage-step pending">${esc(label)} pending</span>`;
  const status = String(detail.status || '').toUpperCase();
  const completed = status === 'COMPLETED';
  const suffix = detail.package_id ? ` · ${detail.package_id}` : (detail.completed_at ? ` · ${formatDate(detail.completed_at)}` : '');
  return `<span class="storage-step ${completed ? 'completed' : 'failed'}" title="${escAttr(detail.error || detail.run_key || '')}">${esc(label)} ${completed ? '✓' : status}${esc(suffix)}</span>`;
}

function workflowBadges(row) {
  const workflow = row.workflow || {};
  return `<div class="storage-workflow" aria-label="Storage workflow history">${workflowStep('Review', workflow.review)}${workflowStep('Archive', workflow.archive)}${workflowStep('Cleanup', workflow.cleanup)}</div>`;
}

function renderTemplates() {
  const allCount = ((healthPayload && healthPayload.templates) || []).length;
  const rows = visibleTemplates();
  const countEl = $('storage-filter-count');
  if (countEl) countEl.textContent = `Showing ${rows.length} of ${allCount} template(s)`;
  const body = $('storage-body');
  if (!body) return;
  syncSortIndicators();

  body.innerHTML = rows.length ? rows.map(row => {
    const disabled = row.cleanup_allowed ? '' : 'disabled';
    const checked = selectedTemplateIds.has(row.template_id) ? 'checked' : '';
    const expanded = expandedTemplateIds.has(row.template_id);
    const usedIn = (row.pipelines || []).map(item => item.pipeline_name).filter(Boolean);
    const objects = (row.objects || []).join(', ');
    const badge = row.has_active_run
      ? `<span class="storage-badge active">${esc(row.active_status || 'ACTIVE')} · cleanup blocked</span>`
      : row.cleanup_allowed
      ? '<span class="storage-badge allowed">Can clean related files</span>'
      : '<span class="storage-badge protected">Nothing unique to clean</span>';
    const sharedBadge = row.shared_bytes
      ? `<span class="storage-badge shared">Shared ${esc(row.shared_human)}</span>`
      : '';
    const details = expanded ? `<tr class="storage-template-details"><td colspan="7">
      <p>${esc(row.recommendation || '')}</p>
      <div class="storage-artifact-list">
        ${artifactList('Will clean if selected', row.can_clean)}
        ${artifactList('Shared with other templates', row.shared)}
        ${artifactList('Kept', row.keep)}
      </div>
    </td></tr>` : '';
    return `<tr>
      <td><input type="checkbox" data-template-id="${escAttr(row.template_id)}" ${disabled} ${checked} /></td>
      <td>
        <button class="storage-expand-btn" type="button" data-expand-id="${escAttr(row.template_id)}">${expanded ? '▾' : '▸'} ${esc(row.name)}</button>
        <small>${esc(row.kind === 'unassigned' ? 'Not linked to a saved template' : (objects || row.template_id))}</small>
        ${badge}${sharedBadge}${workflowBadges(row)}
      </td>
      <td>${row.created_at ? esc(formatDate(row.created_at)) : '<span class="storage-muted">Unknown</span>'}</td>
      <td>${usedIn.length ? `<strong>${esc(usedIn.join(', '))}</strong>` : '<span class="storage-muted">Not used in a pipeline</span>'}</td>
      <td>${esc(row.size_human)}</td>
      <td>${esc(row.reclaimable_human)}</td>
      <td>${esc(row.last_status || '—')}</td>
    </tr>${details}`;
  }).join('') : '<tr><td colspan="7">No matching templates.</td></tr>';

  body.querySelectorAll('input[type="checkbox"][data-template-id]').forEach(input => {
    input.addEventListener('change', () => {
      const id = input.getAttribute('data-template-id');
      if (input.checked) selectedTemplateIds.add(id);
      else selectedTemplateIds.delete(id);
      lastImpact = null;
      reviewBypassed = false;
      archiveBypassed = false;
      renderImpact(null);
      syncActionButtons();
      const selectAll = $('storage-select-all');
      const allowedVisible = visibleTemplates().filter(row => row.cleanup_allowed);
      if (selectAll) {
        selectAll.checked = allowedVisible.length > 0 && allowedVisible.every(row => selectedTemplateIds.has(row.template_id));
        selectAll.indeterminate = allowedVisible.some(row => selectedTemplateIds.has(row.template_id)) && !selectAll.checked;
      }
    });
  });
  body.querySelectorAll('button[data-expand-id]').forEach(button => {
    button.addEventListener('click', () => {
      const id = button.getAttribute('data-expand-id');
      if (expandedTemplateIds.has(id)) expandedTemplateIds.delete(id);
      else expandedTemplateIds.add(id);
      renderTemplates();
    });
  });

  const selectAll = $('storage-select-all');
  const allowedVisible = rows.filter(row => row.cleanup_allowed);
  if (selectAll) {
    selectAll.checked = allowedVisible.length > 0 && allowedVisible.every(row => selectedTemplateIds.has(row.template_id));
    selectAll.indeterminate = allowedVisible.some(row => selectedTemplateIds.has(row.template_id)) && !selectAll.checked;
  }
}

function renderFiles() {
  const body = $('storage-files-body');
  if (!body) return;
  const items = (healthPayload && healthPayload.items) || [];
  body.innerHTML = items.length ? items.map(item => {
    const owners = (item.template_ids || []).join(', ') || 'Unassigned';
    return `<tr>
      <td>${esc(item.kind_label || item.category_label || item.category)}</td>
      <td><strong>${esc(item.name)}</strong><small>${esc(item.relative_path)}</small></td>
      <td>${esc(item.size_human)}</td>
      <td>${esc(owners)}</td>
      <td>${esc(item.recommendation || item.protection_reason || '')}</td>
    </tr>`;
  }).join('') : '<tr><td colspan="5">No files tracked.</td></tr>';
}

function syncActionButtons() {
  const count = selectedItemIds().length;
  const templates = selectedTemplateIds.size;
  const reviewPassed = Boolean((lastImpact && lastImpact.can_cleanup) || reviewBypassed);
  const backupLinked = Boolean(lastArchivePackageId || archiveBypassed);
  $('storage-review').disabled = count === 0;
  $('storage-bypass-review').disabled = count === 0 || reviewPassed;
  $('storage-archive').disabled = count === 0 || !reviewPassed;
  $('storage-bypass-archive').disabled = count === 0 || !reviewPassed || backupLinked;
  $('storage-cleanup').disabled = count === 0 || !reviewPassed || !backupLinked;
  $('storage-review').textContent = count ? `Review impact (${templates} template${templates === 1 ? '' : 's'})` : 'Review impact';
  $('storage-archive').textContent = count && !reviewPassed ? 'Archive selected (review required)' : (count ? `Archive selected (${templates})` : 'Archive selected');
  $('storage-cleanup').textContent = count && !reviewPassed
    ? 'Clean selected (review required)'
    : (count && !backupLinked ? 'Clean selected (link backup)' : (count ? `Clean selected (${templates})` : 'Clean selected'));
}

function renderImpact(impact) {
  const panel = $('storage-impact');
  if (!impact) {
    panel.hidden = true;
    panel.innerHTML = '';
    return;
  }
  const blockers = impact.blockers || [];
  const warnings = impact.warnings || [];
  const related = impact.suggested_related_items || [];
  panel.hidden = false;
  panel.innerHTML = `
    <div class="storage-impact-title">Dependency impact</div>
    <p>${impact.can_cleanup ? 'No blockers — archive is recommended before cleanup. Saved templates and drop-folder source files stay.' : 'Cleanup is blocked until dependencies are resolved.'}</p>
    ${blockers.length ? `<div class="storage-impact-block"><strong>Blockers</strong><ul>${blockers.map(row => `<li>${esc(row.message)}</li>`).join('')}</ul></div>` : ''}
    ${warnings.length ? `<div class="storage-impact-block"><strong>Warnings</strong><ul>${warnings.slice(0, 12).map(row => `<li>${esc(row.message)}</li>`).join('')}</ul>${warnings.length > 12 ? `<small>${warnings.length - 12} more…</small>` : ''}</div>` : ''}
    ${related.length ? `<div class="storage-impact-block"><strong>Suggested related archive members</strong><ul>${related.map(row => `<li>${esc(row.kind_label || row.relative_path)} (${esc(row.size_human || '')})</li>`).join('')}</ul></div>` : ''}
  `;
}

async function reviewImpact() {
  const ids = selectedItemIds();
  if (!ids.length) return;
  showStorageLoader(
    'Analyzing dependencies...',
    `Checking ${ids.length} selected storage item(s) for active runs, shared templates, mappings, and cleanup blockers.`
  );
  setBanner('checking', 'Analyzing dependencies...');
  try {
    const response = await fetch('/api/storage/dependencies', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ item_ids: ids, template_ids: [...selectedTemplateIds] }),
    });
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || 'Dependency analysis failed');
    lastImpact = data;
    reviewBypassed = false;
    renderImpact(data);
    syncActionButtons();
    await loadHealth();
    setBanner(data.can_cleanup ? 'ok' : 'error', data.can_cleanup
      ? `Impact reviewed · ${(data.warnings || []).length} warning(s)`
      : `Blocked · ${(data.blockers || []).length} blocker(s)`);
  } catch (error) {
    setBanner('error', error.message || String(error));
  } finally {
    hideStorageLoader();
  }
}

async function archiveSelected() {
  const ids = selectedItemIds();
  if (!ids.length) return;
  if ((!lastImpact || !lastImpact.can_cleanup) && !reviewBypassed) {
    window.alert('Review impact first. Archive is enabled only after the selected templates pass dependency review.');
    return;
  }
  const names = [...selectedTemplateIds].join(', ');
  const label = window.prompt('Optional archive label (shown in Backup packages):', names) || '';
  const archiveRoot = await currentArchiveRoot();
  if (!archiveRoot) return;
  showStorageLoader(
    'Creating backup package...',
    `Copying selected files, calculating checksums, and writing the package to ${archiveRoot}`
  );
  setBanner('checking', `Archiving files to ${archiveRoot}...`);
  try {
    const response = await fetch('/api/storage/archive', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ item_ids: ids, template_ids: [...selectedTemplateIds], include_related: true, label, archive_root: archiveRoot }),
    });
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || 'Archive failed');
    linkCleanupPackage(data.package_id);
    archiveBypassed = false;
    await loadArchives();
    await loadHealth();
    syncActionButtons();
    setBanner('ok', `Archive only completed: ${data.package_id} · ${((data.manifest || {}).totals || {}).bytes_human || '0 B'}. Wait for OneDrive to sync, then click Clean selected.`);
  } catch (error) {
    setBanner('error', error.message || String(error));
  } finally {
    hideStorageLoader();
  }
}

async function cleanupSelected() {
  const ids = selectedItemIds();
  if (!ids.length) return;

  if (!lastImpact && !reviewBypassed) {
    await reviewImpact();
    if (!lastImpact) return;
  }
  if (lastImpact && !lastImpact.can_cleanup && !reviewBypassed) {
    window.alert('Cleanup is blocked by dependencies. Resolve blockers or change selection.');
    return;
  }

  let archivePackageId = lastArchivePackageId;
  if (!archivePackageId && !archiveBypassed) {
    window.alert('No backup package is linked. Use Archive selected first, wait for OneDrive to sync, or choose Use for cleanup beside an existing backup package.');
    setBanner('error', 'Cleanup stopped: link a synced backup package first.');
    return;
  }

  const backupMessage = archivePackageId
    ? `Backup package: ${archivePackageId}`
    : 'NO BACKUP PACKAGE — deletion cannot be restored through OrgShift.';
  const confirmed = window.confirm(
    `Delete old files for ${selectedTemplateIds.size} template(s)?\n\n${backupMessage}\nSaved templates and drop-folder source files are kept.`
  );
  if (!confirmed) return;

  showStorageLoader(
    'Cleaning selected files...',
    `Removing archived working files for ${selectedTemplateIds.size} template(s) and recalculating storage usage.`
  );
  setBanner('checking', `Cleaning files for ${selectedTemplateIds.size} template(s)...`);
  try {
    const response = await fetch('/api/storage/cleanup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        item_ids: ids,
        template_ids: [...selectedTemplateIds],
        confirm: true,
        archive_package_id: archivePackageId,
        skip_backup: archiveBypassed,
        bypass_confirmed: archiveBypassed,
      }),
    });
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || 'Cleanup failed');
    healthPayload = data.health || healthPayload;
    selectedTemplateIds = new Set();
    lastImpact = null;
    linkCleanupPackage(null);
    reviewBypassed = false;
    archiveBypassed = false;
    renderSummary();
    renderBackup();
    renderCategories();
    renderTemplates();
    renderImpact(null);
    syncActionButtons();
    if (showFiles) renderFiles();
    await loadArchives();
    setBanner(
      (data.errors || []).length ? 'error' : 'ok',
      `Freed ${data.freed_human || '0 B'} · deleted ${(data.deleted || []).length} · skipped ${(data.skipped || []).length} · backup ${archivePackageId}`
    );
  } catch (error) {
    setBanner('error', error.message || String(error));
  } finally {
    hideStorageLoader();
  }
}

async function restorePackage(packageId) {
  if (!packageId) return;
  const proceed = window.confirm(
    `Restore package ${packageId} into the working folders?\n\nOK = continue. You will be asked next whether to overwrite files that already exist.`
  );
  if (!proceed) return;
  const replaceExisting = window.confirm('Overwrite files that already exist locally?');
  showStorageLoader(
    'Restoring backup package...',
    `Reading ${packageId}, verifying its payload, and restoring files into the ETL workspace.`
  );
  setBanner('checking', `Restoring ${packageId}...`);
  try {
    const response = await fetch('/api/storage/restore', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ package_id: packageId, confirm: true, overwrite: replaceExisting }),
    });
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || 'Restore failed');
    if (data.health) {
      healthPayload = data.health;
      renderSummary();
      renderBackup();
      renderCategories();
      renderTemplates();
      syncActionButtons();
      if (showFiles) renderFiles();
    }
    setBanner(
      (data.errors || []).length ? 'error' : 'ok',
      `Restored ${(data.restored || []).length} file(s) · skipped ${(data.skipped || []).length} · errors ${(data.errors || []).length}`
    );
  } catch (error) {
    setBanner('error', error.message || String(error));
  } finally {
    hideStorageLoader();
  }
}

function setBanner(kind, text) {
  const el = $('storage-banner');
  if (!el) return;
  el.className = `auth-status ${kind}`;
  el.innerHTML = `<span class="dot"></span> ${esc(text)}`;
}

function formatDate(value) {
  if (!value) return '';
  try { return new Date(value).toLocaleString(); } catch (_error) { return String(value); }
}

function esc(value) {
  return String(value == null ? '' : value).replace(/[&<>'"]/g, char => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    "'": '&#39;',
    '"': '&quot;',
  }[char]));
}

function escAttr(value) {
  return esc(value).replace(/`/g, '&#96;');
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initStorageHealthPage);
} else {
  initStorageHealthPage();
}
