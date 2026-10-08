# Validation Reference

This document explains the validation stages used by OrgShift before and after a Salesforce data migration. It is intended for migration users, reviewers, and project leads.

## Validation flow

| Stage | Purpose | Main output |
|---|---|---|
| 1. Template Builder validation | Confirm that the object, fields, mappings, operation, Record Types, and target permissions are configured correctly. | Saved template with validation status |
| 2. File and metadata pre-validation | Confirm that the source files are unchanged/readable and that mappings are compatible with current target Salesforce metadata. | Pre-validation report |
| 3. Row/schema validation | Validate every selected row and separate accepted and rejected records. | Clean and rejected CSV files |
| 4. Target-reference validation | Confirm that lookup and external-ID references exist in the target org or can be resolved by the migration. | Missing-reference results |
| 5. Migration gate | Allow migration only after full validation passes or validation is explicitly bypassed. | `PASSED` or `BYPASSED` status |
| 6. Runtime load validation | Validate batches during transformation/loading and capture Salesforce failures. | Run logs and failed-row files |
| 7. Post-load validation | Compare expected source/staged records with target Salesforce results. | Summary, missing, and mismatch reports |

## 1. Template Builder checks

The Template Builder performs configuration checks before a template is used in a pipeline:

- Source object and target object selection.
- Source CSV headers and field mappings.
- Target field API-name existence.
- External ID and match-key configuration.
- Insert, update, or upsert operation configuration.
- Record Type source column and Record Type mappings.
- Lookup target object and lookup external-ID configuration.
- Required target fields are mapped.
- Read-only, calculated, auto-number, compound, and system-managed fields.
- Target profile and target-org compatibility.

Saving a template does not mean that its data has passed full validation.

## 2. File integrity and metadata checks

Pre-validation checks the following before processing rows:

- Source file exists and is readable.
- A drop-folder file has not changed size since it was registered.
- The file path or parent folder identifies the configured Salesforce object.
- Mapped target fields exist in Salesforce Describe metadata.
- The migration user can create or update the object for the configured operation.
- Mapped fields are visible and writable to the migration user.
- Required dependent-picklist controllers are visible and createable.
- Configured Record Types are available to the migration user.
- Source and target API names are unambiguous.

Salesforce Describe metadata is cached and reused when several split files belong to the same object.

## 3. Row and schema checks

Each selected row is checked for:

### Required values

- Required target field is mapped.
- Required mapped value is not blank.
- Salesforce-generated/defaulted/formula/auto-number fields are excluded from normal required-input checks.

### Data types and formats

- Whole-number format and 32-bit integer range.
- Number, currency, and percent format.
- Numeric precision and scale.
- Boolean values.
- Date, time, and ISO-8601 datetime formats.
- Salesforce 15- or 18-character ID format.
- Email address format.
- Phone number format.
- Absolute HTTP or HTTPS URL format.
- Maximum character length.
- Maximum UTF-8 byte length.
- Latitude range of -90 through 90.
- Longitude range of -180 through 180.
- Opportunity Probability range of 0 through 100.
- Lead conversion state restrictions.

### Picklists

- Restricted picklist value is active in the target org.
- Every multi-select picklist value is valid.
- Value is available for the selected target Record Type.
- A dependent picklist has a mapped and populated controlling field.
- Dependent value is valid for the controller value and Record Type.

### Record Types

- Source Record Type ID or DeveloperName resolves to a target Record Type.
- Resolved Record Type is available to the migration user.
- Record-Type-specific picklist rules are applied.

Repeated Record Type and picklist combinations may be cached/set-validated for performance, but every affected row still receives the resulting pass/failure outcome.

### Duplicate migration keys

- Duplicate `Id` values within the selected input scope.
- Duplicate configured external-ID values, such as `Legacy_ID__c`.

Only actual key columns are checked for duplicates. Duplicate values are collected once and then checked through an in-memory set; unrelated mapped fields do not cause database duplicate queries.

### Safe normalization

The validator may apply deterministic, non-lossy normalization:

- Trim surrounding whitespace.
- Convert `yes`, `y`, or `1` to `true`.
- Convert `no`, `n`, or `0` to `false`.
- Remove formatting commas from numeric values.

## 4. Target-reference checks

Target-aware validation checks whether lookup values exist in the target Salesforce org.

Examples:

- `ParentId` to `Account.Id`.
- `OwnerId` to `User.Legacy_ID__c`.
- Region source value to the region object's `Legacy_ID__c`.

Each result reports:

- `checked`: number of distinct nonblank values checked.
- `found`: values found in the target.
- `missing`: values not found in the target.
- Examples of missing values.

A correctly formatted Salesforce ID can pass schema validation but fail target-reference validation because that exact record does not exist in the target.

## 5. Quick sample versus full validation

### Quick sample

- Validates up to the configured number of rows per object.
- The budget is shared across split files for the same object.
- Sample clean/rejected files are report-only.
- Sample files never replace migration-ready cleaned files.
- Produces `SAMPLE_PASSED` or `SAMPLE_ISSUES_FOUND`.
- Does not satisfy the migration gate.

Use Quick sample to identify configuration-wide problems quickly.

### Full validation

- Validates every row in every selected file.
- Produces complete clean and rejected files.
- With target-aware full mode, checks every distinct lookup/reference value.
- Required before normal migration unless validation is explicitly bypassed.

Pipeline pre-validation always invokes flat-file validation with full row scope.

## 6. Knowledge Article checks

For `Knowledge__kav` files, validation also checks:

- Salesforce-hosted images requiring manual rehosting.
- Embedded Salesforce record/article links.
- Availability of source-to-target ID mappings for embedded links.
- Configured rich-text columns.

## 7. Outputs and reports

Pre-validation can generate:

- `prevalidation_report.json`: machine-readable overall report.
- `prevalidation_progress.json`: live progress and current phase.
- `*_clean.csv`: accepted rows for a full validation.
- `*_rejected.csv`: rejected rows with source row number and reasons.
- `*_target_reference_failures.csv`: missing lookup/reference values.
- Run log: metadata counts, target queries, result counts, and elapsed time.

Each completed file report records `elapsed_seconds`. The progress page displays the live duration for the current file and the final duration for completed files. Pipeline-stage reports also record stage duration.

## 8. Migration gate statuses

| Status | Migration allowed? | Meaning |
|---|---:|---|
| `DRAFT` | No | Template exists but full validation has not passed. |
| `PREPARED` | No | Configuration/staging preparation is not a full validation pass. |
| `SAMPLE_PASSED` | No | Sample passed; full data was not validated. |
| `SAMPLE_ISSUES_FOUND` | No | Sample found issues. |
| `ISSUES_FOUND` | No | Full validation found blocking issues. |
| `PASSED` | Yes | Full validation passed. |
| `BYPASSED` | Yes | User explicitly accepted the risk and skipped validation. |

The gate is enforced in both the Pipeline Designer and the server migration API.

## 9. Post-load checks

After migration, post-load validation may check:

- Expected record count versus loaded record count.
- Records missing from the target.
- Field-value mismatches.
- Source-to-stage and stage-to-target consistency.
- Partial-load outcomes.
- Object-specific failed-row downloads.

## Recommended user workflow

1. Create or update the template in Template Builder.
2. Run Quick sample when working with a new mapping or unfamiliar data.
3. Resolve Record Type, field, picklist, lookup, and format issues.
4. Run full pre-validation.
5. Review clean/rejected and target-reference reports.
6. Start migration only after status is `PASSED`, or explicitly use bypass with documented approval.
7. Monitor file/stage timings and runtime failures.
8. Run post-load validation and retain the reports as migration evidence.
