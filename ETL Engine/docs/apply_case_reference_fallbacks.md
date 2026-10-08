# Case Reference Fallbacks

Replace Case lookup IDs that are missing from target UAT with configured fallback IDs, and write an audit map of every change.

## What it does

For each configured lookup column on each Case row:

1. Read the lookup value from the Case file (e.g. `ContactId`, `OwnerId`).
2. Search that value in the matching object file under **Existing Record**, column **`Legacy_ID__c`**.
3. Decide:
   - **Blank** → leave blank (never invent a value).
   - **Found in Existing Record** → keep original ID.
   - **Not found + ApplyFallback = Y** → replace with Fallback ID and log it.
   - **Not found + ApplyFallback = N** → leave original ID unchanged (e.g. `ParentId`).

Also builds a **slim** Case CSV with only the columns listed in the columns map.

## Folder layout

```text
ParentMapping/
├── Existing Record/          # one CSV per object (must have Legacy_ID__c)
│   ├── Contact.csv
│   ├── User.csv
│   ├── Account.csv
│   └── ...
├── Files/                    # source Case CSVs
│   ├── 2025_Case_1.csv
│   └── ...
├── Referrence Columns.csv    # which columns to keep / fallback
├── Fallback Mapping CSV.csv  # object → fallback UAT Id
└── Output/                   # created by the script
    ├── <same Case filenames>.csv
    ├── all_fallback_map.csv
    └── fallback_maps/
        └── <file>_fallback_map.csv
```

## Simple steps to use

### 1. Prerequisites

- Python 3.10+ (stdlib only; no extra pip packages required)
- Existing Record exports with `Legacy_ID__c`
- Case CSV extracts in `Files`
- Fallback IDs that already exist in target UAT

### 2. Prepare config CSVs

**Referrence Columns.csv**

```csv
Column,ReferencedObject,ApplyFallback,OutputColumn,PreserveOriginalAs
Id,,N,Id,
CaseNumber,,N,Legacy_Case_Number__c,
ContactId,Contact,Y,ContactId,Legacy_Contact_Id__c
OwnerId,User,Y,OwnerId,Legacy_users__c
ParentId,Case,N,ParentId,
AccountId,Account,Y,AccountId,Legacy_Account_Id__c
```

| Field | Meaning |
|-------|---------|
| `Column` | Field name in Case Files |
| `ReferencedObject` | Empty for non-lookups; object API name for lookups |
| `ApplyFallback` | `Y` = replace missing; `N` = leave missing unchanged |
| `OutputColumn` | Name written in Output (can rename) |
| `PreserveOriginalAs` | Optional second column that always keeps the pre-fallback value |

**Fallback Mapping CSV.csv**

```csv
ReferencedObject,FallbackId
Contact,003S900001acF7HIAU
User,005S900000dr9BaIAI
Account,001S900001X3zMoIAJ
```

### 3. Run

From the repo root (or from the shared package folder):

```powershell
$base = "C:\path\to\ParentMapping"

python scripts/apply_case_reference_fallbacks.py `
  --existing-dir "$base\Existing Record" `
  --case-dir "$base\Files" `
  --columns-map "$base\Referrence Columns.csv" `
  --fallback-map "$base\Fallback Mapping CSV.csv" `
  --output-dir "$base\Output"
```

Shared-package variant:

```powershell
python apply_case_reference_fallbacks.py `
  --existing-dir "$base\Existing Record" `
  --case-dir "$base\Files" `
  --columns-map "$base\Referrence Columns.csv" `
  --fallback-map "$base\Fallback Mapping CSV.csv" `
  --output-dir "$base\Output"
```

### 4. Check results

| Output | Use |
|--------|-----|
| `Output/*.csv` | Slim Case files ready for load / next remapping step |
| `Output/fallback_maps/*_fallback_map.csv` | Per-file list of replacements |
| `Output/all_fallback_map.csv` | Combined audit of every replacement |
| Console Summary | Counts: kept present, replacements, blanks, left unchanged |

## How matching works (technical)

- Case lookup value (e.g. `ContactId`) is matched against **`Legacy_ID__c`** in `Existing Record\Contact.csv` (not against Case `Id`).
- Matching uses Salesforce Id variants:
  - exact value
  - 15-char prefix (case-sensitive)
  - 18-char casefold (18-char Ids are case-insensitive via checksum)
- Case `Id` / `CaseNumber` are keep/rename only; they are not looked up in Existing Record for fallback.

## Decision flow

```text
lookup value
  ├─ blank?                         → leave blank
  ├─ in Existing[ReferencedObject]? → keep original
  ├─ missing + ApplyFallback = N?   → keep original
  └─ missing + ApplyFallback = Y?   → write FallbackId + audit row
```

## Console summary meaning

| Stat | Meaning |
|------|---------|
| kept present | Value found in Existing Record |
| replacements | Missing from Existing; fallback applied |
| left unchanged (missing) | Missing, but no fallback configured |
| blank skipped | Empty lookup cells |

## CLI arguments

| Argument | Required | Description |
|----------|----------|-------------|
| `--existing-dir` | Yes | Folder of object CSVs with `Legacy_ID__c` |
| `--case-dir` | Yes | Folder of Case CSVs |
| `--columns-map` | Yes | Referrence Columns CSV |
| `--fallback-map` | Yes | Fallback Mapping CSV |
| `--output-dir` | Yes | Output folder |
| `--existing-id-column` | No | Default `Legacy_ID__c` |
| `--no-fallback-columns` | No | Default `ParentId` |
| `--glob` | No | Default `*.csv` |

## Tips

- Every object with `ApplyFallback=Y` must have a matching Existing Record CSV and a FallbackId.
- `PreserveOriginalAs` is optional; without it, originals for replaced cells exist only in the audit maps.
- Re-running into the same `--output-dir` overwrites previous Output files.
- Trailing spaces in Fallback Mapping object names are stripped automatically.
