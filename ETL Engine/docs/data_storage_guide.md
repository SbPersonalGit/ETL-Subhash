# Data Storage Guide

This document explains what data this ETL project stores locally, where it is stored, and how to inspect it.

For **team cleanup, safe harbour (archive), retention, and effort estimates**, see [`STORAGE_HEALTH_READINESS_REPORT.md`](STORAGE_HEALTH_READINESS_REPORT.md). Use **Storage Health** in the app at `/storage`.

## Overview

The project stores data in multiple local layers during a run:

- DuckDB staging databases for extracted and transformed object data
- DuckDB ID-map databases for source-to-target record mappings
- JSON run-state files for pipeline/object execution status
- YAML run-config files for the migration config used in a run
- CSV artifact folders for exported `raw` and `xfm` data
- Text log files for each run

Think of the flow like this:

```text
Salesforce Source
  ->
DuckDB raw_* tables
  ->
DuckDB xfm_* tables
  ->
Salesforce Target
  ->
DuckDB idmap_* tables

In parallel:
- CSV artifacts are written under exports/
- execution status is written under state/run_states/
- run logs are written under logs/
```

## Storage Locations

All local files live under the project root:

`C:\Users\puneetmalhotra\OneDrive - Nagarro\Desktop\ETL App\Python_backend - Local Only`

### Main folders

- `state\`
- `exports\`
- `logs\`
- `config\`

## State Folder

The `state\` folder contains DuckDB databases and run-tracking files.

### Legacy shared files

These may still exist from older runs or non-isolated flows:

- `state\staging.duckdb`
- `state\id_map.duckdb`
- `state\run_state.json`

These are older shared files and should not be assumed to represent the latest concurrent runs.

### Current per-run files

Each isolated run now gets its own files.

Examples:

- `state\run_sf_sf_run_20260428_162139_558479_staging.duckdb`
- `state\run_sf_sf_run_20260428_162139_558479_id_map.duckdb`
- `state\run_states\run_sf_sf_run_20260428_162139_558479.json`
- `state\run_configs\run_sf_sf_run_20260428_162139_558479\migration.yaml`

## Run-Key Naming

The project uses a run key to tie all files together.

Example run key:

`sf_run_20260428_162139_558479`

The filesystem-safe folder prefix becomes:

`run_sf_sf_run_20260428_162139_558479`

This prefix is reused across:

- `exports\run_sf_sf_run_20260428_162139_558479\`
- `logs\run_sf_sf_run_20260428_162139_558479.log`
- `state\run_sf_sf_run_20260428_162139_558479_staging.duckdb`
- `state\run_sf_sf_run_20260428_162139_558479_id_map.duckdb`
- `state\run_states\run_sf_sf_run_20260428_162139_558479.json`
- `state\run_configs\run_sf_sf_run_20260428_162139_558479\migration.yaml`

## DuckDB Databases

There are two main database types.

### 1. Staging DB

File pattern:

- `state\run_sf_<run_key>_staging.duckdb`

Purpose:

- stores extracted and transformed object data
- used during ETL processing before/during load

Typical table prefixes:

- `raw_`
- `xfm_`

### 2. ID Map DB

File pattern:

- `state\run_sf_<run_key>_id_map.duckdb`

Purpose:

- stores source-to-target ID mappings after successful load
- used to resolve lookup relationships for downstream objects

Typical table prefixes:

- `idmap_`

## Table Naming Conventions

### `raw_<ObjectName>`

Example:

- `raw_Knowledge__kav`
- `raw_Product2`

Meaning:

- extracted source data
- closest representation of what came from source Salesforce

Typical contents:

- source `Id`
- original source fields
- one row per source record

### `xfm_<ObjectName>`

Example:

- `xfm_Knowledge__kav`
- `xfm_Product2`

Meaning:

- transformed data
- ready for load into target Salesforce

Typical differences from `raw_`:

- some fields removed
- some values normalized
- lookup columns prepared
- fields adjusted for insert/update rules

### `idmap_<ObjectName>`

Example:

- `idmap_Knowledge__kav`
- `idmap_Product2`

Meaning:

- source-to-target record mapping

Typical columns:

- `source_id`
- `target_id`

## Why One Object Can Appear in Multiple Tables

The same object is stored at different ETL stages.

Example for `Knowledge__kav`:

- `raw_Knowledge__kav`: extracted source rows
- `xfm_Knowledge__kav`: transformed rows
- `idmap_Knowledge__kav`: loaded row mappings

This separation helps with:

- debugging
- rerunning load without re-extracting
- comparing source vs transformed data
- resolving downstream lookups

## Why You Might See Only Two Tables

If `SHOW TABLES;` returns only:

- `raw_Knowledge__kav`
- `xfm_Knowledge__kav`

that usually means:

- you opened a run-specific staging DB for a run that processed only one object
- the pipeline depth/config only staged the root object
- ID-map tables are in a different DB file (`*_id_map.duckdb`)

This is normal.

## Exports Folder

The `exports\` folder contains CSV artifacts written per run and per pipeline.

Example:

`exports\run_sf_sf_run_20260428_162139_558479\Product_Migration_attempt_1\xfm\`

Typical subfolders:

- `raw\`
- `xfm\`

Purpose:

- human inspection
- artifact-based resume support
- troubleshooting outside DuckDB

Important:

- data in `exports\` is related to DuckDB, but it is not the same storage layer
- if Product CSVs exist under `exports\...`, you still need to open the matching per-run staging DB to see Product tables in DuckDB

## Logs Folder

The `logs\` folder stores per-run text logs.

Example:

- `logs\run_sf_sf_run_20260428_162139_558479.log`

Purpose:

- CLI output
- execution progress
- errors and stack traces

There may also be stage-wise log folders under run-specific subdirectories created by the pipeline.

## Run-State JSON

Per-run state JSON files live under:

- `state\run_states\`

Example:

- `state\run_states\run_sf_sf_run_20260428_162139_558479.json`

Purpose:

- current pipeline/object status
- run metadata like timestamps
- used by the UI to show progress

Typical contents:

- migration name
- run key
- started/updated timestamps
- pipeline status
- object status
- failure/completion state

## Run Config YAML

Per-run config files live under:

- `state\run_configs\`

Example:

- `state\run_configs\run_sf_sf_run_20260428_162139_558479\migration.yaml`

Purpose:

- exact pipeline configuration used for that run
- useful for debugging what the run actually executed

## How to Match Files for One Run

If you know the export folder:

`exports\run_sf_sf_run_20260428_162139_558479\`

then the matching files are:

- `state\run_sf_sf_run_20260428_162139_558479_staging.duckdb`
- `state\run_sf_sf_run_20260428_162139_558479_id_map.duckdb`
- `state\run_states\run_sf_sf_run_20260428_162139_558479.json`
- `state\run_configs\run_sf_sf_run_20260428_162139_558479\migration.yaml`
- `logs\run_sf_sf_run_20260428_162139_558479.log`

## How To Explore In DBeaver

### Open the right DB

For current concurrent runs, do not start with:

- `state\staging.duckdb`

Instead, open the matching per-run file:

- `state\run_sf_<run_key>_staging.duckdb`

### Useful queries

List tables:

```sql
SHOW TABLES;
```

List metadata:

```sql
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'main'
ORDER BY table_name;
```

Inspect columns:

```sql
DESCRIBE xfm_Knowledge__kav;
```

Preview rows:

```sql
SELECT *
FROM xfm_Knowledge__kav
LIMIT 50;
```

Count rows:

```sql
SELECT COUNT(*)
FROM raw_Knowledge__kav;
```

Compare raw vs transformed:

```sql
SELECT 'raw_Knowledge__kav' AS table_name, COUNT(*) AS row_count FROM raw_Knowledge__kav
UNION ALL
SELECT 'xfm_Knowledge__kav' AS table_name, COUNT(*) AS row_count FROM xfm_Knowledge__kav;
```

In the ID map DB:

```sql
SHOW TABLES;
```

```sql
SELECT *
FROM idmap_Knowledge__kav
LIMIT 50;
```

## Troubleshooting Tips

### I only see `Knowledge__kav` tables, but Product was also run

Likely causes:

- you opened `state\staging.duckdb` instead of the matching per-run staging DB
- Product belongs to a different run key
- Product exists in `exports\...` for another run, not in the DB you opened

### Fullscreen log shows the wrong file

Check that:

- the URL contains `?run_key=...`
- the run key matches the run you want
- the matching log file exists under `logs\`

### I cannot find `idmap_` tables

Check the ID-map DB, not the staging DB:

- `state\run_sf_<run_key>_id_map.duckdb`

## Mental Model

Use this short model when debugging:

- `raw_*`: extracted source data
- `xfm_*`: transformed load-ready data
- `idmap_*`: source-to-target mappings
- `exports\`: CSV artifacts for inspection/resume
- `logs\`: run output
- `run_states\`: status seen by UI
- `run_configs\`: exact config used by run

