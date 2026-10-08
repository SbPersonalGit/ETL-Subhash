# SF-to-SF (UAT → Production) Readiness Report

**Date:** 2026-08-25  
**Branch:** `SF_to_SF`  
**Context:** CSV-to-SF (Flat File) migration has been heavily tested over the prior ~15 days with many bug fixes and enhancements. This report assesses remaining work to enable UAT → Production Salesforce-to-Salesforce migration using the same shared backend after raw staging.

**Related documents:**

- [`sf_to_sf_architecture_confirmation.md`](sf_to_sf_architecture_confirmation.md) — architecture decisions and delivery phases
- [`SF_TO_SF_FILE_TO_SF_IDENTITY_FIX.md`](../SF_TO_SF_FILE_TO_SF_IDENTITY_FIX.md) — shared identity and cyclic lookup model
- [`PARKED_DESIGNS.md`](PARKED_DESIGNS.md) — intentionally deferred designs (mixed-mode runs, cross-object mapping)
- [`STORAGE_HEALTH_READINESS_REPORT.md`](STORAGE_HEALTH_READINESS_REPORT.md) — disk cleanup, safe harbour, retention (separate estimate)
- [`issues_log.md`](issues_log.md) — issue and enhancement history

---

## Executive summary

**The shared ETL engine after raw staging is largely ready for SF-to-SF.** Recent Flat File work strengthened transform, load, identity compilation, lookup policy, run audit, and pipeline UI — all shared components.

**Remaining work is operational and validation-hardening, not a backend rewrite:**

1. Build UAT source templates in Source Org Builder (`/source-org`)
2. Wire UAT + Production credentials
3. Run pilots on real UAT → Prod data
4. Optionally close validation gaps if pilots expose them
5. **Plan disk and Storage Health** — same migration volume will land locally again; use template-based archive/clean before scaling

**Estimated effort to first confident UAT → Prod migration:** see **[Effort estimate (SF-to-SF)](#effort-estimate-sf-to-sf)** below. Storage disk management is a **separate** track — see [`STORAGE_HEALTH_READINESS_REPORT.md`](STORAGE_HEALTH_READINESS_REPORT.md).

---

## Architecture confirmation

The product has two insulated entry flows that converge at raw staging:

```text
Flat File Migration (CSV → SF)     Source Org Migration (SF → SF)
─────────────────────────────     ───────────────────────────────
/flat-files                       /source-org
config/flat_file_templates/       config/templates/
CSV validation & ingest           Runtime Bulk API extract
              \                          /
               └──► Raw staging (DuckDB) ──► Transform ──► Load ──► Audit / Post-validation
```

**User intuition confirmed:** After staging, both modes use the same transform → load → audit path. SF-to-SF differs only in how data is acquired (runtime extract vs prepared CSV).

Single-mode runs only — a pipeline cannot mix SF → SF and Files → SF stages (by design).

---

## Readiness by layer

| Layer | Readiness | Notes |
|-------|-----------|-------|
| Transform / Load / ID map / cyclic lookups | ~90% | Heavily exercised via CSV; recent fixes (ISS-007, ISS-008) were shared |
| Runtime extract (UAT pull) | ~85% | `TopologyExtractor`, `Orchestrator`, CLI `run`/`extract` exist; needs UAT connectivity + pilot |
| Source Org template UI | ~75% | `/source-org` built; less field use than Flat File Designer |
| Pre-run validation (metadata + sampled data) | ~80% | `prevalidate_source.py`, `core/source_data_prevalidate.py` |
| Post-extraction validation (every staged row) | ~20% | Designed in architecture doc; not implemented as mandatory gate |
| Saved UAT → Prod templates & run history | ~0% | `config/templates/` empty; all recent templates are flat-file |
| Production pilot runs | Not started | Main unknown |

---

## What is already in place

### Shared backend (benefited from recent CSV work)

| Area | Status |
|------|--------|
| `core/transformer.py` — transform rules, lookup remap order | Shared, recently fixed |
| `core/loader.py`, `core/id_mapper.py` | Shared |
| `core/identity_compiler.py` — object identity, in-pipeline lookups | Shared (ISS-008) |
| Pipeline Designer, Pipeline Runs, Validation Center | Mode-aware (`SF → SF` / `Files → SF`) |
| Run audit, resume at transform/load | Shared |
| Owner ID / case-sensitive ID handling | Shared loader path |

### SF-to-SF–specific components

| Component | Location / route | Purpose |
|-----------|------------------|---------|
| Source Org Builder | `/source-org` | Multi-object packages, field maps, SOQL, RT mapping |
| Source prevalidation | `prevalidate_source.py` | Source + target metadata, operations, match fields |
| Data-aware prevalidation | `core/source_data_prevalidate.py` | SOQL `GROUP BY` for RecordTypes, picklists, dependent pairs |
| Pipeline integration | `ui/static/pipeline_designer.js` | `buildSourceMappingPipelineStage()` |
| CLI execution | `cli.py` | Extract via source connector; topology-driven staging |
| Tests | `tests/test_source_template_mode.py`, `tests/test_source_data_prevalidate.py` | Source mode contracts |

---

## Remaining work (confirmed checklist)

### 1. Build UAT source templates

- Use **Source Org Builder** (`/source-org`), not Flat File Designer
- Persist to `config/templates/`
- Per object: field mappings, object identity (source key → target external ID), lookup resolution, SOQL filter, Record Type mapping where needed
- Multi-object cyclic packages must be one template/stage (for two-pass load)
- Constraint: source and target object API names must match (e.g. `Case → Case`)

### 2. Wire UAT + Production credentials

Configure `.env`:

- `SOURCE_SF_*` → UAT org
- `TARGET_SF_*` → Production org

Verify connection from Source Org Builder auth status and Validation Center before any migration run.

### 3. Run pilots

Recommended sequence per pilot object:

1. Prevalidate template (Validation Center)
2. Add to Pipeline Designer (SF → SF only)
3. Run: Extract → review raw CSVs in `exports/` → Transform → pause → Load
4. Compare with equivalent CSV migration behaviour where available

**This is the main unknown** — object-specific config issues (lookups, RT, picklists) may appear; engine gaps are less likely.

### 4. Optionally close validation gaps

Only if pilots expose confidence gaps:

| Gap | Current state | Optional fix |
|-----|---------------|--------------|
| Prevalidation gate on migration start | Enforced for flat file only (`_pipeline_prevalidation_gate_errors`) | Extend gate to `salesforce_source` templates |
| Post-extraction full-record validation | Not a mandatory gate before transform/load | Implement per architecture doc phase 6 |
| Polymorphic `OwnerId` validation | Backlog (ISS-012) | Fix if OwnerId-heavy objects fail in pilot |

---

## What is NOT required for v1 UAT → Prod

- Rebuilding transform, load, or audit
- Flat File Designer for UAT templates
- Mixed SF + CSV stages in one pipeline (PARK-002)
- Arbitrary cross-object API name mapping, e.g. `Legacy_Customer__c → Account` (PARK-003)
- Data Seed Builder UX (replaced by Source Org Builder per ISS-010)

---

## Recommended delivery phases

| Phase | Activity | Duration (estimate) |
|-------|----------|----------------------|
| 1 | Configure UAT + Prod `.env`; verify both org connections | 0.5–1 day |
| 2 | Build templates for 1–2 pilot objects; run prevalidation | 2–5 days |
| 3 | End-to-end pilot: extract → transform → load → verify | 2–3 days |
| 4 | Scale to full object set; harden validation if needed | 3–7 days |

---

## Effort estimate (SF-to-SF)

> **How to read these estimates**
>
> | Label | Meaning |
> |-------|---------|
> | **Team (no AI build)** | Migration analysts use the app — Source Org Builder, Validation, Pipeline Runs. No code written. |
> | **Engineering with AI** | Cursor / agent-assisted code changes in this repo (you direct; AI implements). |
> | **Engineering without AI** | Traditional developer, no AI pair programming — typically **2–3×** the AI-assisted range. |
>
> Estimates below assume you **do not** need a backend rewrite. Optional hardening is **defer until pilot** unless gaps appear.

Estimates assume same-object API name mapping (`Case → Case`), scope similar to current CSV migrations, and one team operating Source Org Builder + Pipeline Designer. **Storage Health setup is excluded** — see [`STORAGE_HEALTH_READINESS_REPORT.md`](STORAGE_HEALTH_READINESS_REPORT.md).

### Product build status

| Area | Build complete | Remaining build |
|------|----------------|-----------------|
| Shared engine (transform / load / audit) | ~90% | Pilot-driven fixes only |
| Runtime extract + orchestration | ~85% | Connectivity + pilot |
| Source Org Builder UI | ~75% | Pilot-driven UX fixes |
| Source prevalidation | ~80% | Optional gate enforcement |
| Post-extraction validation gate | ~20% | Optional; only if pilot requires |
| Saved UAT templates + prod runs | ~0% | Team operational work |

**Core SF-to-SF product build is largely done.** Remaining effort is **team operational** (templates, pilots, credentials) plus **small engineering** only if pilots expose gaps.

### A. Team operational — required (no AI build)

Work the migration team does in the UI. **You build templates; the app runs migration.**

| Activity | Owner | Estimate |
|----------|-------|----------|
| Configure `SOURCE_SF_*` (UAT) + `TARGET_SF_*` (Prod) in `.env` | Team / lead | 0.5–1 day |
| Build source templates in `/source-org` (pilot: 1–2 objects) | Team | 2–3 days |
| Prevalidate + add to Pipeline Designer | Team | 0.5–1 day |
| End-to-end pilot (extract → transform → load → verify) | Team | 2–3 days |
| Build remaining object templates (full scope) | Team | 3–10 days |
| Scale runs + sign-off per object/stage | Team | 3–7 days |
| **Subtotal** | | **11–25 person-days** |

**Calendar:** **2–4 weeks** (parallel object work can shorten).

### B. Engineering — minimum before prod scale (only if you ask AI/dev to build)

| Activity | With AI (Cursor) | Without AI | Trigger |
|----------|------------------|------------|---------|
| Pilot support + config bug fixes | 0.5–1.5 days | 1.5–4 days | First pilot |
| Enforce prevalidation gate for `salesforce_source` | 2–4 hours | 0.5–1 day | Before unattended prod runs |
| **Subtotal** | **~1–2 days** | **~2–5 days** | |

**Calendar with AI:** **1–2 focused sessions** alongside pilot week 1.

### C. Engineering — optional hardening (defer; build only if pilot proves gap)

| Activity | With AI (Cursor) | Without AI |
|----------|------------------|------------|
| Mandatory post-extraction validation gate | 1–2 days | 3–5 days |
| Polymorphic `OwnerId` validation (ISS-012) | 0.5–1 day | 1–2 days |
| Source Org Builder fixes from pilot feedback | 1–2 days | 2–4 days |
| **Subtotal** | **~3–5 days** | **~6–11 days** |

**Calendar with AI:** **~1 week** if all items needed; otherwise **skip**.

### Summary table

| Track | Who | With AI (engineering) | Without AI (engineering) | Team (no build) | Calendar |
|-------|-----|----------------------|--------------------------|-----------------|----------|
| **A — Operational** | Team | — | — | 11–25 days | 2–4 weeks |
| **B — Engineering minimum** | You + AI / dev | ~1–2 days | ~2–5 days | — | 1–2 sessions |
| **C — Optional hardening** | You + AI / dev | ~3–5 days | ~6–11 days | — | Defer |

**Total SF-to-SF to confident UAT → Prod:**

- **Team only (no code build):** 2–4 weeks, 11–25 person-days — **this is the main effort**
- **Plus engineering with AI:** add **~1–2 days** if pilot finds small gaps; **~3–5 days** more only if optional hardening is required
- **Plus engineering without AI:** add **~2–5 days** minimum; **~6–11 days** if full hardening

---

## Risks and mitigations

| Risk | Mitigation |
|------|------------|
| No SF-to-SF production run history | Start with low-risk pilot objects; use transform pause before load |
| Prevalidation less exhaustive than full CSV scan | Run data-aware prevalidation; review raw extract artifacts |
| Source templates not gated at run start | Run prevalidation manually before migration; optionally enforce gate in code |
| UAT metadata differs from Prod | Prevalidation compares both orgs; fix mapping before extract |
| Cyclic lookups across objects | Use multi-object package template in Source Org Builder |

---

## Storage health and disk performance (SF-to-SF)

> **Full analysis and separate effort estimate:** [`STORAGE_HEALTH_READINESS_REPORT.md`](STORAGE_HEALTH_READINESS_REPORT.md)  
> **Storage estimate summary:** 0.5 person-day team setup to use as-is; optional 1–2 dev-days polish; optional 5–8 dev-days automation (defer).

### Staging is still local

Yes — **all staging remains on the migration machine**, for both CSV and SF-to-SF:

| Layer | Location | SF-to-SF behaviour |
|-------|----------|-------------------|
| Extracted rows | `state/run_sf_<run_key>_staging.duckdb` (`raw_*` tables) | Written at **runtime extract** from UAT |
| Transformed rows | Same staging DB (`xfm_*` tables) | Same shared transform path |
| ID mappings | `state/run_sf_<run_key>_id_map.duckdb` | Same shared load path |
| CSV artifacts | `exports/run_sf_<run_key>/…/raw/` and `…/xfm/` | Written during extract/transform for audit and resume |
| Run logs | `logs/run_sf_<run_key>.log` | Same |
| Run config/state | `state/run_configs/`, `state/run_states/` | Same |
| Audit history | `state/run_audit.duckdb` | Always retained |

SF-to-SF does **not** store source business data in `state/flat_file_drop/` or `state/flat_file_uploads/` (those are CSV-only). Instead, extract pulls from UAT and lands in DuckDB + `exports/` — **similar total data volume** to a CSV migration of the same record counts (often slightly more because raw exists in both DuckDB and export CSVs).

See also: [`data_storage_guide.md`](data_storage_guide.md)

### What the team already has (no backend knowledge required)

**Storage Health** (`/storage`) is built for template owners, not engineers:

- Groups disk usage **by the template you created** (flat file or source org)
- Shows what is safe to clean vs what must stay
- Links run artifacts (exports, DuckDB, validation reports, logs) back to templates via run audit
- **Recommended flow:** select templates → Review impact → **Archive selected** → **Clean selected**
- **Restore** from backup packages if something was removed by mistake

Templates themselves (`config/templates/` for SF-to-SF, `config/flat_file_templates/` for CSV) are **kept** during cleanup unless explicitly deleted and not referenced by a pipeline.

### Safe harbour (archive before delete)

| Feature | Status |
|---------|--------|
| Template-centric cleanup UI | **Ready** — `/storage` |
| Archive package with checksum manifest | **Ready** — `core/storage_archive.py` |
| Restore into working tree | **Ready** — Backup packages section on Storage Health page |
| Configurable backup folder | **Ready** — `config/archive.json` (default: `runtime/archive`) |
| Dependency / impact review before delete | **Ready** — Review impact button |
| Active-run protection | **Ready** — running/queued runs cannot be cleaned |
| Auto-scheduled retention | **Not built** — manual only |

**Important:** Default archive folder is **inside the project** (`runtime/archive`). For production-scale migrations, **set an external backup folder** on the Storage Health page (e.g. a network drive or separate disk) *before* the team starts cleaning. Otherwise a broad cleanup could remove backups that live in the same workspace.

### What stays vs what can be cleaned (team view)

| Category | SF-to-SF | Typical team action |
|----------|----------|---------------------|
| Saved source template (`config/templates/*.json`) | Keep | Your mapping design — not deleted by “Clean selected” |
| Pipeline design (`config/pipeline_designer/*.json`) | Keep | Referenced pipelines block template deletion |
| Per-run staging DuckDB | Cleanable after run finishes | Archive first if you may need **resume at transform/load** |
| Per-run ID-map DuckDB | Cleanable after run finishes | Keep until downstream objects / retries are done |
| Export CSVs (`exports/`) | Cleanable | Archive if you need audit evidence later |
| Validation reports (`logs/verify/`) | Cleanable | Archive if compliance needs reports |
| Run logs | Cleanable | Archive for troubleshooting history |
| `state/run_audit.duckdb` | **Always kept** | Grows over time; not cleaned via Storage Health |
| Drop-folder CSVs | N/A for SF-to-SF | CSV-only; protected from accidental delete |

### Storage readiness for a second full migration wave

| Area | Readiness | Notes |
|------|-----------|-------|
| Template-based clean by object/file template | **~90%** | Works for source templates in `config/templates/` |
| Archive → clean → restore workflow | **~90%** | Tested for flat file; same engine for source runs |
| Linking SF-to-SF runs to source templates | **~85%** | Via run audit + pipeline template refs |
| Object names shown under source template row | **~60%** | Flat file shows objects from `files[]`; source templates may show empty object list until enhanced |
| External safe harbour setup | **Operational** | Team must configure once; not automatic |
| Disk space warnings | **Ready** | Critical &lt; 5 GB free; warning at high usage or large reclaimable artifacts |
| Automatic retention / scheduled cleanup | **0%** | Not implemented |

### Recommended team playbook (before UAT → Prod scale-up)

1. **Before migration wave:** Open `/storage` → set **backup folder outside the project** (e.g. `D:\OrgShiftArchive` or team share).
2. **During migration:** Do not clean templates tied to **running** or **failed-but-resumable** runs.
3. **After a template’s runs are finished and signed off:**
   - Select that template in Storage Health
   - **Review impact** → **Archive selected** → **Clean selected**
4. **Keep until cutover complete:** ID-map DuckDB and exports for objects that other stages still reference.
5. **If disk warning appears:** Clean oldest **completed** templates first; never clean active runs.
6. **Rollback:** Use **Restore** from Backup packages on Storage Health.

### Storage gaps to watch (optional engineering follow-ups)

- Enrich source template groups to show object names from `template.objects[]` (cosmetic; cleanup still works)
- Fix UI copy that still says “Flat Files” for unused source templates
- Optional: retention policy (e.g. auto-archive runs older than N days) — not built today
- Optional: enforce minimum free disk before starting a migration run — not built today

---

## Conclusion

SF-to-SF UAT → Production migration does **not** require re-implementing the ETL engine. The last ~15 days of CSV-focused fixes largely strengthened the shared post-staging path.

**Confirmed remaining work:**

1. Build UAT source templates (Source Org Builder)
2. Wire UAT + Prod credentials
3. Run pilots
4. Optionally close validation gaps
5. Plan Storage Health — configure external archive folder; archive/clean per template after sign-off

**Staging remains local for SF-to-SF.** Storage Health is template-centric and ready for team use (~85–90%); configure safe harbour outside the workspace before repeating full migration volume.

---

## Change log

| Date | Author | Change |
|------|--------|--------|
| 2026-08-25 | Assessment (Cursor agent) | Initial readiness report after CSV workstream review |
| 2026-08-25 | Assessment (Cursor agent) | Added storage health, safe harbour, and team playbook section |
| 2026-08-25 | Assessment (Cursor agent) | Added separate SF-to-SF effort estimate section; linked storage report |
| 2026-08-25 | Assessment (Cursor agent) | Split estimates: team (no build) vs engineering with/without AI |
