# Migration Change Log

Changes are recorded in this file only when explicitly requested.

## 2026-08-11 — Campaign missing-reference replacements

- Source preserved: `state/flat_file_drop/Campaign/Campaign_R-54716_Campaign_3Aug_v2.csv`
- Corrected copy: `logs/verify/prevalidation_20260811_162828_bf75ba1e/Campaign_R-54716_Campaign_3Aug_v2_references_fixed.csv`
- Rows processed: 838
- Missing Parent Campaign IDs replaced with `701S900001Ws5UgIAJ` in `ParentId`: 254 replacements.
- Missing User legacy IDs replaced with `005A0000002Sg01IAC`:
  - `OwnerId`: 562 replacements.
  - `Campaign_Support_ge_og__c`: 803 replacements.
  - `Commercial_owner_Telesales_script_ge_og__c`: 821 replacements.
  - `Subject_Matter_Expert_ge_og__c`: 832 replacements.
- Total replacements: 3,272.
- Verification: none of the original 35 missing User IDs or 54 missing Parent Campaign IDs remains in the corrected lookup columns.
- Target verification:
  - Parent Campaign `701S900001Ws5UgIAJ` exists; its `Legacy_ID__c` is `701S900001Ws5UgIAJ`.
  - Target User `005S900000dVFe8IAG` has `Legacy_ID__c = 005A0000002Sg01IAC`.
  - The Target User is inactive. The user confirmed that assignment to an inactive owner has already been checked and is acceptable for this migration.

## 2026-08-11 — Campaign transformation, lookup, and final-load audit

### Files and run configuration

- Loaded file: `state/flat_file_drop/Campaign/Campaign_R-54716_Campaign_3Aug_v3.csv`.
- Pipeline/template: `Campaign_UAT_4`.
- Object identity and upsert key: `Campaign.Legacy_ID__c`.
- Final successful run: `sf_run_20260811_142548_494466`.
- Immutable run configuration: `state/run_configs/run_sf_sf_run_20260811_142548_494466/migration.yaml`.

### Data-level corrections

- The 3,272 missing-reference replacements recorded in the preceding entry were carried into the v3 load file.
- Missing `ParentId` values were assigned the fallback Campaign legacy/external ID `701S900001Ws5UgIAJ` (254 rows).
- Missing User references were assigned User legacy/external ID `005A0000002Sg01IAC`, which resolves in Target to User ID `005S900000dVFe8IAG`:
  - `OwnerId`: 562 replacements.
  - `Campaign_Support_ge_og__c`: 803 replacements.
  - `Commercial_owner_Telesales_script_ge_og__c`: 821 replacements.
  - `Subject_Matter_Expert_ge_og__c`: 832 replacements.
- The fallback Campaign `701S900001Ws5UgIAJ` was changed in Target from GBP to USD so it could be assigned as the parent of USD Campaigns. This Target-side change was made by the user.
- `Website_URL_Other__c` was retained as supplied; the final pipeline configuration contains no value transformation for this field.
- `CreatedById` and `LastModifiedById` were retained as migration/audit requirements but were not added to this Campaign DML payload. Audit-field loading remains a separate follow-up item.

### Transformations applied during load

- `StartDate`: converted with the `mdy2` source-date rule to Salesforce date format; invalid values configured as `retain`.
- `EndDate`: converted with the `mdy2` source-date rule to Salesforce date format; invalid values configured as `retain`.
- `RecordTypeId`: resolved using Record Type developer name metadata from the Target org.
- `OwnerId` and the three custom User lookups were resolved against live Target `User.Legacy_ID__c`; local User data was not treated as authoritative.
- `ParentId` was handled as a deferred Campaign self-lookup:
  - current-run parents resolve through the pipeline ID map;
  - parents already present in Target resolve through `Campaign.Legacy_ID__c` during Pass 2.

### Target permissions and restrictions addressed

- `Set Audit Fields upon Record Creation` was enabled at org level and assigned to the migration user through a permission set.
- `Update Records with Inactive Owners` was enabled for the migration user.
- Target validation/lookup restrictions requiring active Campaign owners or active custom User lookup values were identified as independent of the inactive-owner permission and had to permit the final migration.

### Failure history and application correction

- Run `sf_run_20260811_140716_832139` initially completed Pass 1 for 838 rows, but 254 `ParentId` updates failed in Pass 2 because the fallback parent was GBP while the children were USD.
- The saved historical result was corrected to 584 successful and 254 failed records, with the cyclic failure CSV attached.
- Pipeline result accounting was changed so future Pass 2/cyclic-reference failures:
  - reduce the displayed successful-record count;
  - increase the failed-record count;
  - produce a partial-failure status and downloadable failure CSV;
  - appear in the final load summary instead of showing a false full success.

### Final verification

- Final run result: 838 transformed, 838 loaded, 0 failed, 0 blocked.
- Pass 2 result: 838 `ParentId` updates succeeded with no cyclic failure file.
- Direct Target validation used the exact 254 Campaign IDs from the earlier cyclic failure CSV:
  - Target records found: 254.
  - `ParentId = 701S900001Ws5UgIAJ`: 254.
  - Missing `ParentId`: 0.
  - Incorrect `ParentId`: 0.
  - Child currency: USD on all 254 records.
