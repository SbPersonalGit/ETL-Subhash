# Waygate Wave 1 sample package

Contains 10 objects, 100 records per object, and 1000 total records.
Selection target: at least 20% or 15 fields, whichever is higher.
When an object has fewer eligible loadable fields, every eligible field is populated and the shortfall is recorded in coverage.csv.
OwnerId values reference the active target-org User recorded in manifest.json.
These foundation objects have no record lookup dependencies in the hierarchy workbook.

Load order:

1. `upload_files/Account_Plan_Initiative__c.csv`
2. `upload_files/BA_Condition_Master__c.csv`
3. `upload_files/BA_Inco_Terms__c.csv`
4. `upload_files/BA_LE_Override__c.csv`
5. `upload_files/BA_LE_Preferences__c.csv`
6. `upload_files/BA_Legal_Entity__c.csv`
7. `upload_files/BA_Questionnaire__c.csv`
8. `upload_files/BA_Tier3_Threshold__c.csv`
9. `upload_files/BA_Treaty_Matrix__c.csv`
10. `upload_files/BH_MCS_SMAXDSCUSTOMERASSETS__c.csv`
