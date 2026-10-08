# Issues Log — SF_to_SF / Flat File workstream

Living log of issues fixed during Source Org + Flat File pipeline work.  
Each entry uses:

1. **Issue** — what the user saw  
2. **Root Cause** — why it happened  
3. **Fix Applied** — what changed  
4. **Type** — Bug or Enhancement  

Related deep-dives (kept as-is):

- [`docs/flat_file_save_draft_500_rca.md`](flat_file_save_draft_500_rca.md)
- [`SF_TO_SF_FILE_TO_SF_IDENTITY_FIX.md`](../SF_TO_SF_FILE_TO_SF_IDENTITY_FIX.md)
- [`docs/sf_to_sf_architecture_confirmation.md`](sf_to_sf_architecture_confirmation.md)
- [`docs/PARKED_DESIGNS.md`](PARKED_DESIGNS.md) — agreed designs intentionally deferred from implementation
- [`docs/SF_TO_SF_READINESS_REPORT.md`](SF_TO_SF_READINESS_REPORT.md) — UAT → Prod readiness assessment (2026-08-25)
- [`docs/STORAGE_HEALTH_READINESS_REPORT.md`](STORAGE_HEALTH_READINESS_REPORT.md) — disk cleanup, safe harbour, retention (2026-08-25)

---

## WRK-001 — SF-to-SF UAT → Production readiness assessment

**Recorded:** 2026-08-25  
**Type:** Workstream assessment (not a bug/fix)

### Summary

After ~15 days of CSV-to-SF testing and fixes, assessed remaining work to enable UAT → Production SF-to-SF migration. Shared backend after raw staging is largely ready (~85–90%). Recent transformer, loader, identity, and lookup fixes apply to both modes.

### Confirmed remaining work

1. Build UAT source templates in Source Org Builder (`/source-org`)
2. Wire UAT + Prod credentials (`SOURCE_SF_*`, `TARGET_SF_*`)
3. Run pilots (main unknown)
4. Optionally enforce source prevalidation gate and post-extraction validation if pilots require it

### Not required

Backend rewrite, Flat File Designer for UAT templates, or mixed-mode pipelines.

### Effort estimate (SF-to-SF only)

| Track | Who | With AI | Without AI | Team (no build) |
|-------|-----|---------|------------|-----------------|
| A — Operational (required) | Team | — | — | 11–25 person-days / 2–4 weeks |
| B — Engineering minimum | You + AI / dev | ~1–2 days | ~2–5 days | — |
| C — Optional hardening | You + AI / dev | ~3–5 days | ~6–11 days | — |

**Main effort is team templates + pilots (no AI build).** Engineering with AI: ~1–2 days if pilot finds gaps.

**Full report:** [`docs/SF_TO_SF_READINESS_REPORT.md`](SF_TO_SF_READINESS_REPORT.md)

---

## WRK-002 — Storage health readiness for SF-to-SF migration wave

**Recorded:** 2026-08-25  
**Type:** Workstream assessment (not a bug/fix)

### Summary

Team will repeat the same migration volume for UAT → Prod SF-to-SF. Staging remains **local** (per-run DuckDB + `exports/` CSVs). Storage Health (`/storage`) is **template-centric** so the team only selects templates they built — no backend knowledge required.

### Prepared today

- Clean by template (flat file + source org templates)
- Review impact → Archive → Clean → Restore workflow
- Active-run protection; templates and pipeline designs kept by default
- Disk health warnings (&lt; 5 GB critical)

### Team actions before scale-up

1. Set **backup folder outside the project** on Storage Health (default `runtime/archive` is inside workspace)
2. After signed-off runs: select template → Archive → Clean
3. Do not clean staging/ID-map for runs still resumable or referenced by other objects

### Gaps (non-blocking)

- No automatic retention schedule
- Source template rows may not list object names as clearly as flat-file templates
- `run_audit.duckdb` always retained and grows

### Effort estimate (Storage Health only)

| Track | Who | With AI | Without AI | Team (no build) |
|-------|-----|---------|------------|-----------------|
| A — Use as-is (required) | Team | — | — | 0.5 day |
| B — Polish (optional) | You + AI / dev | ~0.5–1 day | ~1–2 days | — |
| C — Automation (defer) | You + AI / dev | ~3–5 days | ~5–8 days | — |

**Feature already built.** Team setup only for go-live; no AI build required unless optional polish/automation requested.

**Full report:** [`docs/STORAGE_HEALTH_READINESS_REPORT.md`](STORAGE_HEALTH_READINESS_REPORT.md)

---

## ISS-001 — Flat File Save Draft returns HTTP 500

### 1. Issue

Clicking **Save Draft Template** on Flat File Migration failed with:

```text
Request failed (500)
```

Reproduced with drop-folder package including:

```text
state/flat_file_drop/.../Users/User.csv
```

### 2. Root Cause

In `ui/app.py` → `save_flat_file_template()`, a drop-folder integrity check called:

```python
validate_flat_file_reference(source_path, file_config)
```

- `source_path` was **never defined** in that scope → `NameError`
- The call ran inside the per-mapping loop instead of once per file
- Unhandled exception bubbled to Flask as HTTP 500

### 3. Fix Applied

- Resolve path with `resolve_flat_file_path(..., prefer_cleaned=False)` once per file
- Call `validate_flat_file_reference` once per file (outside mapping loop)
- Catch `FlatFileValidationError` and return **HTTP 400** with a clear message

**Files:** `ui/app.py`  
**Detail:** [`docs/flat_file_save_draft_500_rca.md`](flat_file_save_draft_500_rca.md)

### 4. Type

**4.1 Bug**

---

## ISS-002 — False “drop-folder file changed” on Save Draft (OneDrive)

### 1. Issue

After ISS-001, Save Draft sometimes failed with:

```text
Drop-folder file changed after template registration: User.csv. Re-import it.
```

even when CSV content/size had not changed.

### 2. Root Cause

`validate_flat_file_reference` treated **mtime drift** as a content change.  
OneDrive / antivirus sync retouches `source_mtime_ns` without changing file bytes, so same-size files were rejected.

### 3. Fix Applied

- Fail only when **file size** differs from the registered size
- When size is unchanged but mtime differs, **refresh** stored `source_mtime_ns` (and size fields) in place
- Added regression tests for mtime-only drift vs real size change

**Files:** `core/flat_file.py`, `tests/test_flat_file.py`  
**Detail:** Follow-up in [`docs/flat_file_save_draft_500_rca.md`](flat_file_save_draft_500_rca.md)

### 4. Type

**4.1 Bug**

---

## ISS-003 — Pipeline prevalidation fails on non-UTF-8 CSV (`0xe9`)

### 1. Issue

Pipeline prevalidation failed, e.g.:

```text
http://localhost:5000/validation/runs/pipeline_prevalidation_20260805_170403_f165a6a2
```

Error:

```text
'utf-8' codec can't decode byte 0xe9 in position 5840: invalid continuation byte
```

Affected stage/file: `User_First_v1` / `User.csv` (German signatures / accented names such as `René`).

### 2. Root Cause

Drop-folder `User.csv` is **Windows CP1252** (byte `0xe9` = `é`).  
Prevalidation, schema cleaning, and related readers opened CSVs as `utf-8-sig` only.  
Polars ingest similarly defaulted to UTF-8.

### 3. Fix Applied

Shared helpers in `core/flat_file.py`:

- `detect_csv_encoding()` — try `utf-8-sig` → `cp1252` → `latin-1` (cached by path/size/mtime)
- `open_csv()` — text open with detected encoding
- `polars_csv_encoding()` — map to Polars (`utf8` / `cp1252` / `latin1`)

Wired into:

- `prevalidate_files.py`
- `core/flat_file_schema_validator.py`
- flat-file ingest + `core/stager.py` CSV load
- Knowledge dependency CSV scan

Cleaned output remains UTF-8. Tests cover CP1252 accented names and UTF-8 preference when valid.

**Files:** `core/flat_file.py`, `core/flat_file_schema_validator.py`, `prevalidate_files.py`, `core/stager.py`, `knowledge_article/dependency_validator.py`, `tests/test_flat_file.py`

### 4. Type

**4.1 Bug**

---

## ISS-004 — Corporate DLP blocks Flat File browser upload (local POC)

### 1. Issue

Selecting CSVs on `/flat-files` triggered corporate DLP as an “external file upload”, even though the destination was only `http://127.0.0.1:5000` / `localhost:5000`.

### 2. Root Cause

Browser multipart `FormData` POST to `/api/flat-files/upload` matches DLP upload signatures.  
Controls often do **not** treat loopback as safe; they key off file-picker + multipart POST, not whether bytes leave the workstation.

### 3. Fix Applied

- Added **local / drop-folder import** so the server copies CSVs from a configured local path
- Avoids browser multipart upload for the POC path
- Documented that a DLP allowlist remains the proper long-term control for true browser upload

**Files:** `ui/app.py`, Flat File designer UI/JS, `tests/test_flat_file_drop_folder.py` (and related import tests)

### 4. Type

**4.2 Enhancement** (workaround + product capability; root cause is corporate DLP policy)

---

## ISS-005 — Target / source profile not preserved through Pipeline Designer

### 1. Issue

Pipeline Designer runs could lose or mix Salesforce connection profiles:

- Source Org stages need both `source_profile` and `target_profile`
- File→SF stages need target profile only
- Mixed profiles across stages in one run were unsafe

### 2. Root Cause

Pipeline compile / serialize paths did not consistently propagate profile metadata from templates into pipeline entries, so runtime connectors could fall back to defaults or inconsistent profiles.

### 3. Fix Applied

- Preserve and send `source_profile` + `target_profile` from templates through Pipeline Designer
- Block mixed-profile pipelines
- File→SF keeps target profile only

**Files:** `ui/static/pipeline_designer.js`, `ui/app.py` (pipeline entry build), related template summary APIs

### 4. Type

**4.1 Bug**

---

## ISS-006 — Source Org prevalidation was incomplete vs describe metadata

### 1. Issue

Source Org mapping templates could pass weak checks and still fail later at extract/load because field existence, types, and operation rules were not validated against live describe metadata.

### 2. Root Cause

Early Source mode reuse of seed-oriented validation did not fully cover:

- Source fields vs source describe
- Target fields vs target describe
- Field types
- Operation (insert / update / upsert)
- Match / external-ID fields only when the selected operation requires them

### 3. Fix Applied

- Strengthened `prevalidate_source.py` metadata checks against Source + Target connectors
- Operation-aware match-field rules
- Shared Validation Center entry for Source templates

**Files:** `prevalidate_source.py`, `ui/app.py`, `tests/test_source_template_mode.py`

### 4. Type

**4.2 Enhancement** (correctness gap closed for Source mode)

---

## ISS-007 — Lookup remap used wrong relationship after field rename

### 1. Issue

When a source lookup column was renamed to a different target relationship field, lookup resolution could use the wrong target relationship metadata / external ID path.

### 2. Root Cause

Transform applied `field_rename` before lookup remapping (or otherwise resolved lookups using pre-rename names), so relationship metadata no longer matched the field being remapped.

### 3. Fix Applied

- Remap lookups **before** `field_rename` in `core/transformer.py`
- Use target-org relationship metadata for renamed lookup fields

**Files:** `core/transformer.py` (+ related identity/compile tests)

### 4. Type

**4.1 Bug**

---

## ISS-008 — Duplicate identity on lookups + cyclic reference keys

### 1. Issue

Templates stored the parent external ID on every child lookup (second source of truth), which could drift from the parent object identity. Cyclic SF→SF / File→SF lookups could also defer the wrong key type.

### 2. Root Cause

Lookup mappings independently owned `ReferencedObject.ExternalId` even when the parent already defined object-level identity (`source key → target match field`). Cyclic pass-2 resolution needed the same identity key the loader used for ID mapping.

### 3. Fix Applied

- Normalize **object-level identity** once; in-pipeline lookups use `pipeline_object` (+ derived parent keys)
- Keep explicit `Parent.ExternalId` only for out-of-pipeline / already-in-target parents
- Align cyclic deferred keys with that identity model for SF→SF and File→SF
- Designer compile policy for out-of-package / preserve / idmapper / object_template (`ui/static/source_lookup_policy.js`)

**Files:** identity compiler / transformer / loader paths, designer compile JS  
**Detail:** [`SF_TO_SF_FILE_TO_SF_IDENTITY_FIX.md`](../SF_TO_SF_FILE_TO_SF_IDENTITY_FIX.md)

### 4. Type

**4.1 Bug** (with supporting design cleanup)

---

## ISS-009 — SF→SF data-level prevalidation (RecordType / picklists)

### 1. Issue

Metadata-only Source prevalidation could miss data problems that only appear in source rows, e.g. inactive / unmapped RecordType DeveloperNames, invalid picklist values, or dependent picklist pairs.

### 2. Root Cause

Prevalidation queried describe/metadata but did not sample source data aggregates for RT / picklist usage.

### 3. Fix Applied

- Added `core/source_data_prevalidate.py` (SOQL `GROUP BY` for RecordType DeveloperNames, picklist values, dependent pairs)
- Wired into `prevalidate_source.py`
- Report flag `data_aware: true`
- Tests in `tests/test_source_data_prevalidate.py`

### 4. Type

**4.2 Enhancement**

---

## ISS-010 — Source Org product surface (mapping templates, not Data Seed)

### 1. Issue

Users needed SF→SF migration with mapping templates (object/field map, SOQL at runtime extract), but `/` still presented Data Seed–style UX. Requirement: dedicated Source Org flow, reusable with existing Pipeline Designer / Validation / Runs, without downloading business data at design time.

### 2. Root Cause

Product lacked a first-class `salesforce_source` mapping UI and package envelope separate from Data Seed, while the shared ETL engine already supported runtime source extract.

### 3. Fix Applied

- Dedicated `/source-org` Source Query Mapping Template UI
- Persist `input_mode`, profiles, field mappings, operations (insert/update/upsert)
- Hook into existing prevalidation + pipeline add flows
- Keep Data Seed unreachable/unchanged per product decision
- Shared Pipeline Designer / Validation / Runs remain mode-aware

**Files:** `ui/templates/source_org_builder.html`, `ui/static/source_org_builder.js`, `ui/app.py`, `prevalidate_source.py`, `prevalidate_pipeline.py`, home/nav templates  
**Detail:** [`docs/sf_to_sf_architecture_confirmation.md`](sf_to_sf_architecture_confirmation.md)

### 4. Type

**4.2 Enhancement**

---

## Quick index

| ID | Short title | Type |
|---|---|---|
| WRK-001 | SF-to-SF UAT → Prod readiness assessment | Assessment |
| WRK-002 | Storage health readiness for SF-to-SF migration wave | Assessment |
| ISS-001 | Save Draft HTTP 500 (`source_path`) | Bug |
| ISS-002 | False drop-folder “file changed” (mtime) | Bug |
| ISS-003 | Prevalidation UTF-8 / CP1252 CSV decode | Bug |
| ISS-004 | DLP-safe local/drop-folder import | Enhancement |
| ISS-005 | Profile propagation in Pipeline Designer | Bug |
| ISS-006 | Source metadata prevalidation completeness | Enhancement |
| ISS-007 | Lookup remap vs field rename order | Bug |
| ISS-008 | Object identity + cyclic lookup keys | Bug |
| ISS-009 | SF→SF data-aware RT/picklist prevalidation | Enhancement |
| ISS-010 | Source Org mapping template product surface | Enhancement |
| ISS-011 | Multi-file same-object via folder + shared mapping | Enhancement |
| ISS-012 | Polymorphic OwnerId reference validation is skipped | Bug (Backlog) |

---

## ISS-011 — Multi-file same-object mapping via folder name

### 1. Issue

Large object extracts are often split across multiple CSVs under one folder (e.g. `Account/part1.csv`, `Account/part2.csv`). Auto-detect only used the **filename** stem, so split files like `part1.csv` did not map to `Account`, and each file needed its own manual mapping even when they shared one object template.

### 2. Root Cause

`detect_object_from_filename` ignored parent folders. The designer auto-mapped each file independently without syncing mapping policy across same-object siblings.

### 3. Fix Applied

- Added `detect_object_from_path()`: nearest parent folder matching an object API name, else filename stem
- Import stores `object_detection_source` (`folder` / `filename`)
- Designer syncs mapping / external ID / Record Type / lookup policy across same-object siblings; **Apply mapping to N sibling(s)** button
- Runtime unchanged: one template stage can already ingest many files into one `raw_<Object>`

**Files:** `core/flat_file.py`, `ui/app.py`, `ui/static/flat_file_designer.js`, `ui/static/flat_file_designer.css`, tests

### 4. Type

**4.2 Enhancement**

---

## ISS-012 — Polymorphic `OwnerId` reference validation is skipped

### Status

**Backlog — not fixed**

### 1. Issue

Flat-file prevalidation correctly reports unresolved user references for fields such as `CreatedById`, but does not report the same problem for `OwnerId`.

Observed in validation run `prevalidation_20260812_101509_73da7782`, file `SMAX_DS_Campaigns_SMax_Campaign__c_Exract_rejected.csv`. For example, Salesforce user ID `005A0000005a5f8IAA` is rejected when supplied through `CreatedById` because it is not found in `User.Legacy_ID__c`. An equivalent unresolved value in `OwnerId` is not checked or reported.

This can allow prevalidation to pass an owner reference that later fails during migration or resolves incorrectly.

### 2. Root Cause

Salesforce describes `CreatedById` as a single-object reference to `User`, but describes `OwnerId` as a polymorphic reference, normally to `User` or `Group`.

The flat-file metadata endpoint builds lookup-validation metadata only when the reference has exactly one target:

```python
or len(references) != 1
```

Because `OwnerId` has multiple targets, it receives no `referenced_object`, `lookup_external_id`, or `lookup_resolution` in the saved template. `prevalidate_files.py` skips mappings without `referenced_object`, so no owner existence check or `__error_OwnerId` column is produced.

Relevant files:

- `ui/app.py` — lookup metadata generation near the `len(references) != 1` condition
- `prevalidate_files.py` — target-reference checks skip mappings without `referenced_object`
- `config/flat_file_templates/Smax_Campaign_UAT_v1.json` — `CreatedById` has User lookup metadata while `OwnerId` does not

### 3. Proposed Fix

- Add explicit polymorphic lookup metadata for `OwnerId`.
- Route IDs with prefix `005` to the configured `User` migration key, such as `User.Legacy_ID__c`.
- Route IDs with prefix `00G` to the configured `Group` migration key or supported target-ID policy.
- Reject unsupported owner ID prefixes with a field-level validation error.
- Produce `__error_OwnerId` in the rejected artifact when owner resolution fails.
- Preserve support for valid queue/group ownership; do not assume every owner is a `User`.

### 4. Acceptance Criteria

- An unresolved `005` owner produces an `OwnerId` error against the configured User key.
- An unresolved `00G` owner produces an `OwnerId` error against the configured Group resolution policy.
- Valid User and Group owners pass prevalidation.
- `CreatedById` behavior remains unchanged.
- Automated tests cover User owners, Group/queue owners, missing owners, unsupported prefixes, and blank values.

### 5. Type

**Bug — backlog**

---

## How to add the next issue

Copy this block and increment the ID:

```markdown
## ISS-0XX — Short title

### 1. Issue
...

### 2. Root Cause
...

### 3. Fix Applied
...

### 4. Type
**4.1 Bug**  or  **4.2 Enhancement**
```
