# Archive Scripts Quick Start

## Workspace-local layout

The scripts initially use:

```text
runtime/active
runtime/archive
runtime/restore
runtime/package_staging
```

Place or configure active ETL outputs under:

```text
runtime/active/
├── exports/<run-id>/
├── state/
├── logs/<run-id>/
└── verification/<run-id>/
```

## Archive a run

```powershell
py scripts\archive_run.py `
  --run-id "run_sf_example" `
  --config-file "config\flat_file_templates\Example.json"
```

The script:

- discovers run-specific exports, state, logs, and verification artifacts;
- refuses sensitive files and DuckDB databases with a WAL;
- builds a stable snapshot under `runtime/package_staging`;
- validates copied DuckDB databases read-only;
- writes component ZIPs under `runtime/archive/<year>/<month>/<run-id>`;
- writes `manifest.json` and `checksums.sha256.json`;
- removes only its successful duplicate package-staging copy;
- never deletes active ETL artifacts.

## Verify an archive

Package-level verification:

```powershell
py scripts\verify_archive.py `
  --manifest "runtime\archive\<year>\<month>\run_sf_example\manifest.json"
```

Deep member and DuckDB validation:

```powershell
py scripts\verify_archive.py `
  --manifest "runtime\archive\<year>\<month>\run_sf_example\manifest.json" `
  --deep `
  --work-root "runtime\package_staging\verify_work"
```

Run deep verification against a downloaded OneDrive/SharePoint copy before
considering the remote archive verified.

## Prepare an isolated restore

```powershell
py scripts\restore_run.py `
  --manifest "runtime\archive\<year>\<month>\run_sf_example\manifest.json" `
  --scope all
```

This extracts into `runtime/restore/<run-id>` and validates all restored files.
It does not change active data.

## Apply a validated restore

After inspecting the isolated restore:

```powershell
py scripts\restore_run.py `
  --manifest "runtime\archive\<year>\<month>\run_sf_example\manifest.json" `
  --scope all `
  --apply
```

The apply step refuses to overwrite any existing destination file.

## Partial restore scopes

```text
all
exports
state
logs
verification
config
```

Example:

```powershell
py scripts\restore_run.py `
  --manifest "runtime\archive\<year>\<month>\run_sf_example\manifest.json" `
  --scope verification
```

## Future non-OneDrive active root

```powershell
py scripts\archive_run.py `
  --run-id "run_sf_example" `
  --etl-root "C:\ETL\active" `
  --archive-root "C:\Users\puneetmalhotra\OneDrive - Nagarro\ETL Migration Archives" `
  --staging-root "C:\ETL\package_staging"
```

Restore:

```powershell
py scripts\restore_run.py `
  --manifest "<OneDrive archive manifest path>" `
  --etl-root "C:\ETL\active" `
  --restore-root "C:\ETL\restore" `
  --scope all
```
