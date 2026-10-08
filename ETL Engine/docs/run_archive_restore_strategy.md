# ETL Run Archive and Restore Strategy

## Purpose

This guide defines a recoverable archive strategy for completed ETL migration
runs. It covers:

- uploaded, cleaned, transformed, rejected, and failed CSV files;
- run-specific DuckDB staging and ID-map databases;
- run configuration and state files;
- load and verification reports;
- SharePoint storage;
- checksum verification;
- complete and partial restoration.

The objective is to reduce local storage while preserving everything required
for audit, verification, investigation, retry, or restoration.

## Archive boundary

Treat one migration run as one recoverable unit:

```text
run_sf_sf_run_20260730_150656_646686
```

Do not archive isolated files without retaining their run ID, configuration,
checksums, and original relative paths.

## Recommended archive structure

```text
run_sf_sf_run_20260730_150656_646686/
├── manifest.json
├── checksums.sha256
├── exports/
│   └── WayGateClean_v4_attempt_1/
│       ├── uploaded/
│       ├── cleaned/
│       ├── xfm/
│       ├── rejected/
│       └── failed/
├── state/
│   ├── run_sf_sf_run_20260730_150656_646686_staging.duckdb
│   ├── run_sf_sf_run_20260730_150656_646686_id_map.duckdb
│   ├── run_config.yaml
│   └── run_state.json
├── logs/
│   ├── load/
│   ├── retry/
│   └── verification/
└── config/
    └── data_seeding_template.json
```

The archive must preserve relative paths so the restore process can return files
to their expected workspace locations.

## What the manifest must contain

The archive manifest should record:

- archive schema version;
- run ID;
- migration name;
- Data Seeding or pipeline template name and ID;
- source and target profile identifiers, without credentials;
- start, completion, archive, and verification timestamps;
- pipeline and attempt folder names;
- object names;
- external-ID fields;
- record counts at each stage;
- load attempted, successful, and failed counts;
- verification missing and mismatched counts;
- original workspace-relative paths;
- archive-relative paths;
- file sizes;
- SHA-256 checksums;
- archive status;
- SharePoint destination;
- application version or Git commit, when available;
- operator and business-acceptance references, when required.

Never store passwords, access tokens, JWT private keys, `.env` contents, or
other credentials in the archive.

## Archive lifecycle

Use explicit states:

| State | Meaning |
|---|---|
| `ACTIVE` | Migration is still processing |
| `VERIFYING` | Target verification is in progress |
| `READY_TO_ARCHIVE` | Migration, retries, and verification passed |
| `PACKAGING` | Archive creation is in progress |
| `PACKAGED` | Local archive and manifest were created |
| `UPLOADED` | Archive was copied to SharePoint |
| `VERIFIED_REMOTE` | A remote/downloaded copy passed checksum validation |
| `LOCAL_PURGED` | Approved local artifacts were removed |
| `RESTORING` | Restore is in progress |
| `RESTORED` | Restored files passed validation |
| `ARCHIVE_FAILED` | Packaging, upload, or verification failed |

Local cleanup must only be permitted after `VERIFIED_REMOTE`.

## Required archive sequence

```text
Complete migration
    ↓
Complete all retries
    ↓
Complete target verification
    ↓
Receive required acceptance
    ↓
Stop ETL processes
    ↓
Checkpoint and close DuckDB
    ↓
Build archive package
    ↓
Generate manifest and checksums
    ↓
Test packaged DuckDB files
    ↓
Upload to SharePoint
    ↓
Download or independently verify remote copy
    ↓
Apply approved local retention policy
```

Archive creation and local deletion must be separate operations.

## DuckDB safety

Never copy, compress, upload, or delete a DuckDB database while an ETL process
is writing to it.

Before packaging DuckDB:

1. Stop migration, retry, verification, and UI processes that use the run.
2. Close all DuckDB connections.
3. Ensure pending work is checkpointed.
4. Check for a corresponding `.wal` file.
5. If a WAL exists, do not ignore it. Open and close the database cleanly or
   preserve the database and WAL together.
6. Copy the database to the package staging directory.
7. Open the packaged copy read-only and run basic table/count checks.

DuckDB is already compressed internally. ZIP compression may not reduce its
size substantially.

## Packaging options

### Single package

For smaller runs:

```text
run_<id>.zip
run_<id>.manifest.json
run_<id>.sha256
```

### Component packages

For larger runs:

```text
run_<id>_exports.zip
run_<id>_state.zip
run_<id>_logs.zip
run_<id>_config.zip
run_<id>_archive_manifest.json
run_<id>_checksums.sha256
```

Component packages are preferable for large migrations because:

- individual uploads are smaller;
- failed uploads can be retried independently;
- only the required component needs to be restored;
- reports can be accessed without downloading large databases;
- changing one component does not require rebuilding everything.

For very large objects, consider one export package per object or processing
day.

## Generate a ZIP package

Example:

```powershell
$runId = 'run_sf_sf_run_20260730_150656_646686'
$packageRoot = "C:\ETL_Archive\staging\$runId"
$archivePath = "C:\ETL_Archive\packages\$runId.zip"

Compress-Archive `
    -LiteralPath $packageRoot `
    -DestinationPath $archivePath `
    -CompressionLevel Optimal
```

Do not use the live workspace directory as the package root. Build the package
in a separate staging directory so the archive contains a stable snapshot.

## Generate SHA-256 checksums

For one package:

```powershell
$archivePath = 'C:\ETL_Archive\packages\run_sf_sf_run_20260730_150656_646686.zip'
$checksumPath = "$archivePath.sha256.csv"

Get-FileHash -LiteralPath $archivePath -Algorithm SHA256 |
    Export-Csv -LiteralPath $checksumPath -NoTypeInformation
```

For every package in a run:

```powershell
$packageDirectory = 'C:\ETL_Archive\packages\run_sf_sf_run_20260730_150656_646686'
$checksumPath = Join-Path $packageDirectory 'checksums.sha256.csv'

Get-ChildItem -LiteralPath $packageDirectory -File |
    Where-Object Name -ne 'checksums.sha256.csv' |
    Get-FileHash -Algorithm SHA256 |
    Select-Object Path, Algorithm, Hash |
    Export-Csv -LiteralPath $checksumPath -NoTypeInformation
```

## Verify checksums

```powershell
$checksumPath = 'C:\ETL_Restore\run_sf_sf_run_20260730_150656_646686\checksums.sha256.csv'
$expected = Import-Csv -LiteralPath $checksumPath

$results = foreach ($entry in $expected) {
    $actual = Get-FileHash -LiteralPath $entry.Path -Algorithm $entry.Algorithm

    [pscustomobject]@{
        Path = $entry.Path
        Expected = $entry.Hash
        Actual = $actual.Hash
        Match = $entry.Hash -eq $actual.Hash
    }
}

$results | Format-Table -AutoSize

if ($results.Match -contains $false) {
    throw 'Archive checksum verification failed.'
}
```

The final verification must be performed against a downloaded or independently
read SharePoint copy, not only against the local pre-upload package.

## SharePoint storage

Recommended SharePoint organization:

```text
ETL Migration Archives/
└── 2026/
    └── 07/
        └── run_sf_sf_run_20260730_150656_646686/
            ├── manifest.json
            ├── checksums.sha256.csv
            ├── exports.zip
            ├── state.zip
            ├── logs.zip
            └── config.zip
```

Record the final SharePoint folder or document-library location in the manifest
and local archive registry.

Avoid synchronizing live DuckDB files directly through the OneDrive client.
Only completed, closed, packaged snapshots should be transferred.

## Restore scopes

Support partial restoration:

| Scope | Restores | Typical use |
|---|---|---|
| `All` | Every component | Full investigation or resume |
| `Exports` | Uploaded, cleaned, xfm, rejected, failed | Inspect or rerun file-based validation |
| `DuckDB` | Staging and ID-map databases | Resume or inspect staged state |
| `Logs` | Load, retry, and operational logs | Troubleshooting |
| `Verification` | Summaries and mismatch reports | Audit or business review |
| `Config` | Template, run configuration, state metadata | Reproduce run settings |

Examples:

- Rerun target validation: restore transformed exports, configuration, and
  verification workspace.
- Resume transformation: restore staging DuckDB, ID-map database,
  configuration, and state.
- Review mismatches: restore verification reports only.
- Reproduce the complete run: restore all components.

## Required restore sequence

```text
Select run and restore scope
    ↓
Download required SharePoint packages
    ↓
Verify package checksums
    ↓
Extract into an isolated restore directory
    ↓
Verify every file against the manifest
    ↓
Open DuckDB copies read-only
    ↓
Check destination paths for conflicts
    ↓
Restore to original relative paths
    ↓
Run counts and integrity checks
    ↓
Mark run RESTORED
```

Always restore first into:

```text
C:\ETL_Restore\<run-id>\
```

Do not extract directly over the active workspace.

## Restore destinations

After validation, restore components to:

```text
exports\<run-id>\
state\<run-id>_staging.duckdb
state\<run-id>_id_map.duckdb
state\run_configs\<run-id>.yaml
state\run_states\<run-id>.json
logs\<run-id>\
```

The restore process must refuse to overwrite an existing active run unless an
operator explicitly approves a separate, recoverable overwrite workflow.

## Post-restore validation

After restoration:

- compare all restored file hashes to the manifest;
- confirm file sizes;
- confirm required directories exist;
- open DuckDB read-only;
- list expected `raw_*` and `xfm_*` tables;
- reconcile table counts to manifest counts;
- confirm external-ID mappings exist;
- confirm restored configuration matches the archived template;
- confirm no credential files were introduced;
- run a read-only verification or dry run before resuming processing.

## Local retention policy

A practical policy:

- Active run: retain all files locally.
- Verified run: package and upload to SharePoint.
- Remotely verified run: retain local summaries and failures for an operational
  grace period, such as 2–7 days.
- After the grace period: remove only artifacts approved by the retention
  policy.
- Preserve local archive registry and SharePoint references.

Possible cleanup candidates after remote verification:

- uploaded, cleaned, and transformed CSV copies;
- downloaded target verification snapshots;
- temporary Bulk API downloads;
- reproducible intermediate exports;
- completed retry working files.

Run-specific DuckDB files may also be archived and removed if the business and
operational policy permits it. If DuckDB remains local, monitor its cumulative
storage separately.

## Recovery testing

An archive is not considered reliable until it has been restored successfully.

Perform periodic recovery drills:

1. Select a completed non-production run.
2. Restore it to an isolated directory.
3. Verify checksums.
4. Open DuckDB read-only.
5. Compare table and CSV counts.
6. Run target verification in dry/read-only mode.
7. Record restore duration and findings.

Test at least one archive before the production migration begins, then repeat
at agreed intervals during the migration.

## Recommended automation

The workspace includes these scripts:

```text
scripts/archive_run.ps1
scripts/restore_run.ps1
scripts/test_run_archive.ps1
```

The implemented Python equivalents are:

```text
scripts/archive_run.py
scripts/verify_archive.py
scripts/restore_run.py
```

Their workspace-local default roots are:

```text
runtime/active
runtime/archive
runtime/restore
runtime/package_staging
```

The same scripts remain portable when the active ETL data is moved outside
OneDrive because every root can be supplied explicitly.

Proposed usage:

```powershell
py scripts\archive_run.py `
    --run-id 'run_sf_sf_run_20260730_150656_646686' `
    --etl-root 'C:\ETL\active' `
    --archive-root 'C:\Users\puneetmalhotra\OneDrive - Nagarro\ETL Migration Archives' `
    --staging-root 'C:\ETL\package_staging'
```

```powershell
py scripts\verify_archive.py `
    --manifest 'C:\Users\puneetmalhotra\OneDrive - Nagarro\ETL Migration Archives\2026\07\run_sf_sf_run_20260730_150656_646686\manifest.json' `
    --deep
```

```powershell
py scripts\restore_run.py `
    --manifest 'C:\Users\puneetmalhotra\OneDrive - Nagarro\ETL Migration Archives\2026\07\run_sf_sf_run_20260730_150656_646686\manifest.json' `
    --etl-root 'C:\ETL\active' `
    --restore-root 'C:\ETL\restore' `
    --scope all
```

Without `--apply`, restore only extracts and validates files in the isolated
restore directory. After inspection, use the same command with `--apply`.
Existing destination files are never overwritten.

The archive command should not delete local files. Local cleanup should be a
separate command requiring a `VERIFIED_REMOTE` archive status.

## Safety rules

- Never archive an active DuckDB database.
- Never omit a relevant WAL file without checkpointing and closing the database.
- Never include credentials or private keys.
- Never delete local data before remote checksum verification.
- Never overwrite an active restored destination automatically.
- Never rely only on SharePoint upload success; verify a downloaded copy.
- Never treat an untested archive as a valid recovery point.
- Preserve the manifest and checksums in at least two locations.
- Keep archive creation, upload, verification, and cleanup as separately
  auditable actions.
