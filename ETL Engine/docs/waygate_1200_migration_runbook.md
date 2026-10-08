# Waygate 1200 Migration Runbook and Lessons Learned

This document records the issues found during run
`run_sf_sf_run_20260731_140638_568407 / 1200_TestData_v6_attempt_1`, the
corrections applied, and the controls required before future Waygate runs.

## Final Result

The corrected retry completed with 150 records each for Account, Contact,
Contract, Product2, Lead, Opportunity, and Case. Retries were made idempotent
by excluding target records whose `Legacy_ID__c` already existed.

## Mandatory Preflight Checklist

1. Run **Validate and Clean Files** against the selected live target org.
2. Upload only object CSVs. Do not upload `coverage.csv` or
   `lookup_integrity.csv`.
3. After changing a source CSV, create a new upload batch. Existing batches
   under `state/flat_file_uploads` are immutable snapshots.
4. Confirm every upsert object has a writable, unique target External ID.
5. Normalize every Salesforce Date value to `YYYY-MM-DD`.
6. Verify both `load_failed` and `load_blocked`; blocked children must be
   retried after their parents.
7. Confirm target-owned setup records, especially Business Hours, are active
   and mapped to IDs from the current target org.
8. Deactivate or formally bypass the VisualCompliance automation listed below
   for the Case load, then reactivate it after reconciliation.
9. Confirm generated Accounts meet the target Opportunity commercial-account
   lookup-filter criteria before including `Opportunity.AccountId`.
10. Capture source count, clean count, rejected count, inserted count, failed
    count, blocked count, and final target count for every object.

## Issues and Preventive Controls

| Area | Observed issue | Preventive control |
|---|---|---|
| External IDs | `Lead.Legacy_ID__c` did not initially exist in target metadata. | Live External ID validation now runs before cleaning and template save. |
| Metadata cache | A newly deployed field was hidden by an in-process Describe cache. | Auto-map, cleaning, and template save refresh target Describe metadata. |
| Business Hours | Case referenced an inactive Business Hours record. | Map `BusinessHoursId` to an active record from the current target org. |
| CSV dates | Dates were rewritten from `2026-08-01` to `8/1/26`. | Run `scripts/normalize_flat_file_dates.py`; do not resave migration CSVs with localized date formatting. |
| Upload snapshots | Corrected local files did not update an existing UI batch. | Re-upload corrected files and use the new batch ID. |
| Audit CSVs | `coverage.csv` was treated as object data. | Upload only files from `upload_files/`. |
| Dependencies | 45 Opportunities were blocked while Contracts were missing; Cases then lacked Opportunities. | Use dependency order and retry both failed and blocked files. |
| Contract lifecycle | Activated Contract statuses caused `FAILED_ACTIVATION`. | Insert historical Contracts with a draft status such as `Pending`; activate only through an approved later step. |
| Lead coordinates | Latitude values exceeded 90. | Validate Latitude as -90..90 and Longitude as -180..180. |
| Lead conversion | `IsConverted=true` required converted Account/Contact IDs. | Do not migrate `IsConverted`; use Salesforce Lead conversion after insertion if required. |
| Lead duplicates | Target duplicate rules rejected synthetic Leads. | Decide whether duplicate rules are enforced, disabled, or bypassed with an approved REST header. |
| Opportunity Probability | Values exceeded 100. | Validate `Probability` as 0..100. |
| Opportunity Contact | `ContactId` invoked Opportunity Contact Role automation and failed its Account update. | Omit this optional field for synthetic data or satisfy the target automation prerequisites. |
| Opportunity Account | Commercial-account lookup filter rejected generated Accounts. | Validate the business filter criteria or omit the optional lookup. Describe metadata alone cannot evaluate record-level lookup filters. |
| External-ID casing | Case relationship lookup normalized `aFF...` to `aff...` and missed case-sensitive values. | Preserve casing or resolve lookup keys to target Salesforce IDs before load. |
| Case automation | VisualCompliance automation exceeded `Too many SOQL queries: 201`. | Deactivate/bypass automation and use isolated transactions when required. |
| Bulk API | Opportunity Bulk job returned an internal Salesforce Gack ID. | Use an approved REST fallback for a small failed subset and retain the Gack ID in run evidence. |
| Proxy handling | Bulk job creation bypassed the connector and used a dead local proxy. | All Bulk lifecycle calls now use the shared authenticated connector session. |
| Retry safety | Partial successes made repeated inserts risky. | Query `Legacy_ID__c` before retry and insert only missing records. |

## Target Automation Required Inactive for Case

- `VisualCompliance Resubmit Screening - Case`
- `VC Account Create Audit`

Record who disabled the automation, when it was disabled, the migration window,
who reactivated it, and the post-reactivation verification result.

## Required Load Order

1. Account
2. Campaign
3. Product2
4. Contact
5. Contract
6. Opportunity
7. Lead
8. Case

## Useful Commands

Normalize dates before upload:

```powershell
py -m scripts.normalize_flat_file_dates sample_data/waygate_standard_1200/upload_files
```

The target-specific, idempotent recovery implementation is retained for audit
and diagnostic reference:

```text
scripts/retry_waygate_failed_direct.py
```

Do not use the direct-retry script as the normal migration path. The standard
ETL pipeline remains the required path for planned production migrations.
