# Waygate flat-file sample package

Generated 2026-07-29T17:18:30.441887+00:00 from local Salesforce metadata.
Each object contains two coordinated source records. `Id` values represent source-org IDs;
the flat-file planner maps them to each object's `Legacy_ID__c` external ID.
Lookup values use matching source IDs from the related CSV so the existing transformer
can emit `ParentObject.Legacy_ID__c` relationship columns.

The requested `BH_MCS_MCSAPServicesTransaction` name was resolved to
`BH_MCS_MCSAPServicesTransactions__c` from the metadata.

See `coverage.csv` and `manifest.json` for per-object coverage and Record Type status.
