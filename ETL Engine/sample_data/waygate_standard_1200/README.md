# Waygate core-object sample package

This package contains synthetic Waygate inspection and NDT records for
Account, Campaign, Contact, Product2, Lead, Contract, Opportunity, and Case.

Total generated records: **1,200**.

Each CSV contains at least 50% of the object's writable, non-calculated
target fields. Every populated lookup references another generated CSV record,
except `BusinessHoursId`, which references the active target-org Business Hours
record documented in `manifest.json`.

Load order:

1. `upload_files/Account.csv`
2. `upload_files/Campaign.csv`
3. `upload_files/Product2.csv`
4. `upload_files/Contact.csv`
5. `upload_files/Contract.csv`
6. `upload_files/Opportunity.csv`
7. `upload_files/Lead.csv`
8. `upload_files/Case.csv`

Upload only the eight CSV files inside `upload_files/`. Do not upload
`coverage.csv` or `lookup_integrity.csv`; they are audit reports, not
Salesforce object data.

Use `coverage.csv` for field coverage and `lookup_integrity.csv` for the
lookup-to-parent evidence.

## Target permission prerequisites

- The connected target profile cannot create Campaign. Coverage uses schema candidates without profile FLS; grant object and field create access before loading.
