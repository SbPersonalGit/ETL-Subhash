# Waygate Account cycle-group sample package

Objects: 13. Insert records: 1300.

Run `01_insert_files` first. Map each source `Id` to target `Legacy_ID__c` and use upsert.
Run `02_deferred_lookup_updates` only after every insert object succeeds.
For every lookup, follow `lookup_strategy.csv`: `target_id` means preserve the supplied Salesforce ID; `external_id` means resolve using the listed external ID.
The `target_mapping` column records the relationship mapping expected for external-ID resolution.
Do not combine both directories into one upload batch.

`validationFailed` contains rejected output from the earlier generator version and is retained as historical evidence. The regenerated source files are under `01_insert_files`; see `validation_audit.json` for their current full validation result.
