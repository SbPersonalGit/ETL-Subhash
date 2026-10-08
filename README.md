# ETL Updated

This workspace keeps migration files in object folders and hides the Python engine.

## Folders

1. **Input Files** — put source CSVs here as `Input Files / ObjectAPIName / file.csv`
2. **Pre Validate Result** — pre-validation rejected, clean, and report files by object
3. **Migration Result** — load success and failed files by object
4. **Post Validate Result** — post-validation / verify files by object
5. **ETL Engine** — application code, virtual environment, and config (you do not need to use this folder day to day)
6. **Temp Files** — extra files the engine creates during a run, grouped by object

## Start

Double-click `Open Setup.bat`.

The Setup screen asks for:

- `TARGET_SF_LOGIN_URL`
- `TARGET_SF_SESSION_ID`

Then use **Start Python Server** or **Restart Python Server**.


**Fallback Script**
.\.venv\Scripts\python.exe scripts\apply_rejected_lookup_fallbacks.py `
  --rejected "..\Pre Validate Result\Student_School__c\prevalidation_20261007_230003_bd2c57da\Student_School__c.v1_rejected.csv" `
  --fallbacks "C:\Users\subhashchander\OneDrive - Nagarro\KLG\Full Sandbox Migration\KLG Fallback Ids.xlsx"
