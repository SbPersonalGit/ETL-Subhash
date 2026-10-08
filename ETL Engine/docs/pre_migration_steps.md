# Pre-Migration Steps

This document records activities that must be completed before migration data is
moved into staging. It covers:

- **Global steps** that apply to every migration file and object.
- **Object-specific steps** for target-org dependencies that are not inserted by
  the migration and may not appear in the data dictionary.

Complete this checklist for each target org and retain the evidence with the
migration run.

For the concrete issues and controls discovered in the 1,200-record Waygate
test, also review `docs/waygate_1200_migration_runbook.md`.

## Global Pre-Migration Steps

### 1. Confirm the target org and connection profile

- Confirm that the selected target profile points to the intended Salesforce
  org.
- Confirm that the migration user can describe the objects and fields included
  in the migration.
- Record the target Org ID, connection profile, validation date, and validator.

### 2. Run **Validate and Clean Files**

Every uploaded CSV must pass the existing **Validate and Clean Files** process
before it is saved as a flat-file template or moved into staging.

The process:

- confirms that the file is a CSV with a valid, non-empty header;
- rejects duplicate or blank column names;
- confirms the selected target object and field mappings;
- identifies unmapped and read-only target fields;
- validates target external-ID and lookup configuration;
- validates Salesforce IDs and supported data types;
- validates number precision and scale;
- validates dates, datetimes, times, booleans, email addresses, and field
  lengths;
- validates active target picklist values, including applicable record-type
  picklist values;
- applies safe normalizations, such as boolean normalization and removal of
  number-formatting commas;
- produces separate clean and rejected CSV files.

Only the generated clean file is eligible to move into staging. Review and
correct every rejected row, then rerun validation. Do not manually rename an
unvalidated source file as a clean file.

> **Important:** Schema validation does not prove that every referenced
> target-org record is active or operationally valid. Target-owned setup and
> reference records must also be checked using the object-specific steps below.

### 3. Review the validation result

- Confirm that the correct target object is assigned to each file.
- Confirm that every source column is mapped or intentionally ignored.
- Confirm that the external ID and operation (`insert`, `update`, or `upsert`)
  are correct.
- Confirm that lookup mappings use the intended target external ID or an
  explicitly verified target record ID.
- Confirm that the rejected-row count is zero, or that rejected rows are
  formally excluded from the migration scope.
- Save the validation report and clean/rejected file counts as run evidence.

### 4. Approve movement into staging

Move a clean file into staging only after the global checklist and all
applicable object-specific checks have passed.

Record the approval:

| Item | Value |
|---|---|
| Migration/run name | |
| Target Org ID | |
| Validation date | |
| Clean file name(s) | |
| Clean row count | |
| Rejected row count | |
| Validated by | |
| Approved for staging by | |

## Object-Specific Pre-Migration Steps

### Case

#### Business Hours mapping

`BusinessHours` is a target-org setup object. It is **not inserted by this
migration** and is **not expected to be supplied by the migration data
dictionary**.

Before moving the Case file into staging:

1. Identify the active Business Hours record already configured in the target
   org.
2. Confirm with the business or Salesforce administrator whether the target
   org's default Business Hours record or another named active record must be
   used.
3. Map the source `Case.BusinessHoursId` value to the selected target
   `BusinessHours.Id`. Never carry a Business Hours ID from another Salesforce
   org into the target payload.
4. Verify that the selected target record has `IsActive = true`.
5. Replace the source-org value in the clean migration data, or apply an
   approved transformation that sets the verified target ID.
6. Record the mapping and approval in the table below.

Use this target-org query to identify eligible records:

```sql
SELECT Id, Name, IsActive, IsDefault
FROM BusinessHours
WHERE IsActive = true
ORDER BY IsDefault DESC, Name
```

Mapping evidence:

| Source value or rule | Target Business Hours ID | Target name | Active | Default | Approved by |
|---|---|---|---|---|---|
| | | | | | |

Expected control:

- Every non-blank `Case.BusinessHoursId` in the staged file must equal an
  approved, active Business Hours ID from the current target org.
- If the field is intentionally left blank, confirm and document that
  Salesforce should apply the target org's default behavior.
- A Case load must not proceed when the mapped Business Hours record is missing
  or inactive. Otherwise Salesforce rejects it with
  `FIELD_INTEGRITY_EXCEPTION: Business Hours is inactive`.

## Additional Object-Specific Dependencies

Add other target-owned setup or reference dependencies as they are identified.
Use the following template:

### `<Object API Name>`

#### `<Dependency or field>`

- **Not migrated because:**  
- **Target-org prerequisite:**  
- **Mapping/transformation rule:**  
- **Validation query or check:**  
- **Approval/evidence:**  
- **Expected failure if missed:**  
