# Data Retention and Safe Cleanup

Use this checklist only after migration and post-migration validation are complete. Never clean files belonging to a running validation or migration.

## Preconditions

Confirm all of the following before moving or deleting processed data:

- Migration has reached a terminal status.
- Post-migration record counts and failure reports have been reviewed.
- No child-object migration still requires the run's ID mappings.
- No retry or rejected-row correction is pending.
- The required reports have been copied to the approved OneDrive project location.
- The copied archive opens successfully and its file sizes match the local files.
- The run key, template name, migration date and target org are recorded with the archive.

## Retention table

| Data | Local location | Recommended action | When safe |
|---|---|---|---|
| Original source CSVs | `state/flat_file_drop/<Object>/` | Move to OneDrive | After migration and post-validation approval |
| Template batch files | `state/flat_file_uploads/<batch_id>/` | Move to OneDrive or delete | After all retries are complete and the template no longer needs its clean files |
| Validation clean/rejected CSVs | `logs/verify/<validation_run>/` | Move required evidence to OneDrive; delete local copies | After report review and approval |
| Validation JSON reports | `logs/verify/<validation_run>/prevalidation_report.json` | Retain in OneDrive | Keep with migration evidence |
| Migration exports/results | `exports/<migration_run>/` | Move to OneDrive | After post-validation and retry completion |
| Run staging database | `state/run_sf_<run_key>_staging.duckdb` | Delete | After migration, retries and post-validation |
| ID-mapping database | `state/run_sf_<run_key>_id_map.duckdb` | Archive or delete | Only after all dependent/child objects and relationship passes finish |
| Validation request | `state/validation_requests/<validation_key>.json` | Delete | After the run is terminal and its report is archived |
| Run configuration | `state/run_configs/<run_key>.yaml` | Archive or delete | After the run is terminal and reproducibility requirements are satisfied |
| Run state file | `state/run_states/<run_key>.json` | Archive or delete | After migration and post-validation approval |
| Old run log | `logs/run_sf_<run_key>.log` | Move to OneDrive or delete according to retention policy | After reports are archived |
| Temporary folders | `temp/`, root `tmp*`, and `state/tmp*` | Delete | Only when no application process is using them |

## Always retain locally

Do not delete these application files:

- `config/`
- `state/run_audit.duckdb`
- `state/flat_file_drop/.gitkeep`
- `state/flat_file_drop/README.md`
- Application source code and dependency files
- Files for any active, queued or retryable run

## Recommended OneDrive archive layout

```text
Migration_Archive/
  <Project_or_Org>/
    <YYYY-MM-DD>_<Pipeline_Name>/
      source/
      validated_clean/
      rejected/
      migration_results/
      validation_reports/
      logs/
      id_mapping/
      README.md
```

The archive `README.md` should record:

- Source and target org names
- Pipeline and template names
- Migration and validation run keys
- Migration date
- Record counts and final status
- Known rejected or unresolved records
- Approval/reference ticket

## Safe cleanup order

1. Stop or finish every related application process.
2. Copy required source, result, rejected and report files to OneDrive.
3. Verify the OneDrive copy.
4. Delete old failed/stopped validation folders.
5. Delete completed run staging databases.
6. Delete template-batch clean/rejected files only after retries are closed.
7. Move original source CSVs last.
8. Start the application and confirm templates, Runs and reports behave as expected.

## Current Account template example

Original source files:

```text
state/flat_file_drop/Account/
```

Template batch working files:

```text
state/flat_file_uploads/6ff8aa33ee504079be7fad9c92724aa2/
```

Current validation artifacts:

```text
logs/verify/pipeline_prevalidation_20260806_142041_e6d0fa80/
```

Do not move or delete the current validation folder or its template batch while that validation is running.

## Recovery warning

Deleting local files is not reversible through OrgShift. Confirm the OneDrive archive before deletion. If there is any uncertainty, move the folder to the archive first and defer permanent deletion.
