# Storage Health Readiness Report

**Date:** 2026-08-25  
**Branch:** `SF_to_SF`  
**Context:** The team will repeat the same migration volume for UAT → Production (SF-to-SF). Staging remains local. This report assesses disk cleanup, safe harbour (archive), retention, and team-facing readiness — without requiring backend knowledge.

**Related documents:**

- [`data_storage_guide.md`](data_storage_guide.md) — what is stored where (DuckDB, exports, logs)
- [`SF_TO_SF_READINESS_REPORT.md`](SF_TO_SF_READINESS_REPORT.md) — SF-to-SF migration readiness (separate estimate)
- [`issues_log.md`](issues_log.md) — WRK-002 workstream log entry

---

## Executive summary

**Storage Health is largely built and ready for team use (~85–90%).** The team cleans disk **by template** (the object/file packages they created) via `/storage`. Archive-before-delete and restore are implemented.

**Remaining work is mostly operational** (configure external backup folder, train team on archive → clean flow). **Optional engineering** (auto-retention, source-template UX polish) is not required to start the SF-to-SF migration wave.

| Track | Estimate |
|-------|----------|
| **Use Storage Health as-is (team setup)** | **0.5 person-day** |
| **Recommended polish (engineering)** | **1–2 dev-days** |
| **Optional automation (engineering)** | **5–8 dev-days** |

---

## Analysis: local staging and disk growth

### Staging is always local

All migration data is written to the machine running OrgShift:

| Layer | Path pattern | CSV-to-SF | SF-to-SF |
|-------|--------------|-----------|----------|
| Staging DB | `state/run_sf_*_staging.duckdb` | After CSV ingest | After UAT extract |
| ID map DB | `state/run_sf_*_id_map.duckdb` | After load | After load |
| Export CSVs | `exports/run_sf_*/…/raw/`, `…/xfm/` | Yes | Yes (written at extract/transform) |
| Run logs | `logs/run_sf_*.log` | Yes | Yes |
| Source inputs | `state/flat_file_drop/`, `flat_file_uploads/` | Yes | **No** (UAT is queried at runtime) |
| Saved templates | `config/flat_file_templates/`, `config/templates/` | Yes | Source templates only |
| Audit history | `state/run_audit.duckdb` | Always kept | Always kept |

**Disk impact for SF-to-SF:** Similar total volume to CSV for the same record counts. SF-to-SF may use **slightly more** disk because extracted rows exist in both DuckDB and export CSVs. There is no upfront drop-folder copy, but runtime extract fills the same logical space.

---

## What is already built

| Capability | Location | Readiness |
|------------|----------|-----------|
| Disk scan by category | `core/storage_health.py` | Ready |
| Template-centric grouping | `core/storage_archive.py` | Ready |
| Archive package + checksum manifest | `core/storage_archive.py` | Ready |
| Restore from backup | `/api/storage/restore` | Ready |
| Review impact / dependency analysis | `/api/storage/dependencies` | Ready |
| Active-run protection | `storage_health.py` | Ready |
| Template + pipeline design protection | `storage_archive.py` | Ready |
| Storage Health UI | `/storage` | Ready |
| Configurable archive root | `config/archive.json` | Ready (default inside workspace) |
| Source org template indexing | `config/templates/` | Ready |
| Flat file template indexing | `config/flat_file_templates/` | Ready |
| Tests | `tests/test_storage_health.py`, `test_storage_archive.py` | Present |

### Team workflow (no backend required)

```text
/storage
  → Set backup folder OUTSIDE project (one-time)
  → Select template(s) you created
  → Review impact
  → Archive selected   (safe harbour copy)
  → Clean selected     (free local disk)
  → Restore from Backup packages if rollback needed
```

Templates and pipeline designs are **not** removed by “Clean selected” unless the template itself is explicitly deleted and not referenced.

---

## Safe harbour and retention

| Need | Supported today | Gap |
|------|-----------------|-----|
| Copy before delete | Yes — archive packages with manifest | Default folder is inside project |
| External safe harbour | Yes — configure on Storage Health page | Team must set manually |
| Restore / rollback | Yes — Backup packages table | — |
| Retain templates indefinitely | Yes — templates kept by default | — |
| Retain audit history | Yes — `run_audit.duckdb` never auto-deleted | Grows over time; no purge UI |
| Scheduled auto-archive / auto-clean | **No** | Manual only |
| Block migration start when disk low | **No** | Warning on Storage Health only |

**Critical operational rule:** Set archive root to a folder **outside** the project (e.g. `D:\OrgShiftArchive` or team network share) before large-scale cleanup. Default `runtime/archive` lives inside the workspace and can be removed if the whole project tree is cleaned naively.

---

## Readiness by area

| Area | Readiness | Notes |
|------|-----------|-------|
| Template-based clean (flat file) | ~95% | Battle-tested |
| Template-based clean (source org) | ~90% | Same engine; run linking via audit |
| Archive → clean → restore | ~90% | Tested for flat file; applies to SF-to-SF runs |
| Link runs to source templates | ~85% | Via run audit + pipeline refs |
| Object names on source template row | ~60% | Flat file shows `files[]`; source may show empty until enhanced |
| Disk warnings | ~90% | Critical &lt; 5 GB free |
| Team-facing documentation | ~70% | `data_storage_guide.md` exists; playbook in this report |
| Automatic retention policy | 0% | Not built |

---

## Gaps (optional engineering)

| Item | Effort | Priority |
|------|--------|----------|
| Show object names for source templates in Storage Health UI | 0.5–1 dev-day | Low (cosmetic) |
| Fix UI copy (“Flat Files”) for unused source templates | 0.25 dev-day | Low |
| Document external archive setup in SETUP_GUIDE | 0.25 dev-day | Medium |
| Minimum free-disk check before starting a migration run | 1–2 dev-days | Medium |
| Scheduled retention (e.g. archive runs older than N days) | 3–5 dev-days | Low (defer) |
| Run audit DB rotation / purge UI | 2–3 dev-days | Low (defer) |

None of these block using Storage Health for the SF-to-SF wave.

---

## Effort estimate (Storage Health)

> **How to read these estimates**
>
> | Label | Meaning |
> |-------|---------|
> | **Team (no AI build)** | Use `/storage` as shipped — set backup folder, archive/clean by template. |
> | **Engineering with AI** | Cursor / agent implements optional polish or automation. |
> | **Engineering without AI** | Traditional developer — typically **2–3×** the AI-assisted range. |
>
> **Storage Health is already built (~85–90%).** Track A is not “AI building the feature” — it is team setup only.

Estimates assume one migration workstation or shared VM. Separate from SF-to-SF template/migration work.

### A. Team operational — use as-is (required, no AI build)

| Activity | Owner | Estimate |
|----------|-------|----------|
| Set external backup folder on `/storage` | Team / lead | 0.25 day |
| Brief team on archive → clean → restore playbook | Team / lead | 0.25 day |
| **Subtotal** | | **0.5 person-day** |

**Calendar:** same day as SF-to-SF env setup. **No code build.**

### B. Engineering — recommended polish (optional; you ask AI/dev to build)

| Activity | With AI (Cursor) | Without AI |
|----------|------------------|------------|
| Source template object names in Storage Health UI | 2–4 hours | 0.5–1 day |
| UI copy fixes for source templates | 1–2 hours | 0.25 day |
| SETUP_GUIDE note for external archive | 1 hour | 0.25 day |
| **Subtotal** | **~0.5–1 day** | **~1–2 days** |

**Not blocking go-live.**

### C. Engineering — automation (optional, defer)

| Activity | With AI (Cursor) | Without AI |
|----------|------------------|------------|
| Min free-disk gate before migration start | 0.5–1 day | 1–2 days |
| Scheduled archive/clean policy | 1.5–2.5 days | 3–5 days |
| Run audit retention UI | 1–1.5 days | 2–3 days |
| **Subtotal** | **~3–5 days** | **~5–8 days** |

**Defer unless team outgrows manual cleanup.**

### Summary table

| Track | Who | With AI | Without AI | Team (no build) | Blocking SF-to-SF? |
|-------|-----|---------|------------|-----------------|-------------------|
| **A — Use as-is** | Team | — | — | 0.5 day | No — do first |
| **B — Polish** | You + AI / dev | ~0.5–1 day | ~1–2 days | — | No |
| **C — Automation** | You + AI / dev | ~3–5 days | ~5–8 days | — | No |

**Total Storage Health readiness:**

- **Team only:** **0.5 day** — feature already exists; no AI build required
- **Plus polish with AI:** add **~0.5–1 day** only if you want UX tweaks
- **Plus automation with AI:** add **~3–5 days** — defer

**Product build status:** ~85–90% complete today.

---

## Recommended team playbook

1. **Before migration wave:** `/storage` → set backup folder outside project.
2. **During migration:** Do not clean templates tied to running or resumable failed runs.
3. **After sign-off per template:** Review impact → Archive → Clean.
4. **Keep until cutover done:** ID-map DuckDB + exports for objects still referenced downstream.
5. **If disk warning:** Clean oldest **completed** templates first.
6. **Rollback:** Restore from Backup packages on Storage Health.

---

## Risks and mitigations

| Risk | Mitigation |
|------|------------|
| Disk full mid-migration | Monitor `/storage`; clean completed templates; ensure ≥20 GB free before large extracts |
| Backups inside workspace deleted | Move archive root outside project before first clean |
| Accidental clean of resumable run | Active-run protection + team playbook; archive before clean |
| Audit DB grows unbounded | Defer purge UI; manual DBA backup if needed |

---

## Change log

| Date | Author | Change |
|------|--------|--------|
| 2026-08-25 | Assessment (Cursor agent) | Initial storage readiness report and effort estimate |
| 2026-08-25 | Assessment (Cursor agent) | Split estimates: team (no build) vs engineering with/without AI |
