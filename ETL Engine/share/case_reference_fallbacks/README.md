# Case Reference Fallbacks — Quick Start

Replace missing Case lookup IDs (not present in target UAT) with fallback IDs, and keep an audit trail.

## Simple steps

### Step 1 — Install / prepare

1. Install **Python 3.10+** (no extra packages needed).
2. Unzip this package to any folder.
3. Prepare your data folders (see layout below).

### Step 2 — Prepare folders

```text
YourWorkFolder/
├── Existing Record/     # Contact.csv, User.csv, Account.csv, ... (must include External_Id__c)
├── Files/               # Case CSV extracts
├── Referrence Columns.csv
├── Fallback Mapping CSV.csv
└── Output/              # script creates this
```

Sample config files are in `samples/`. Copy and edit them.

### Step 3 — Edit config

**Referrence Columns.csv** — which Case columns to keep and whether to apply fallback.

**Fallback Mapping CSV.csv** — one FallbackId per referenced object (must already exist in UAT).

### Step 4 — Run

```powershell
$base = "C:\path\to\YourWorkFolder"

python apply_case_reference_fallbacks.py `
  --existing-dir "$base\Existing Record" `
  --case-dir "$base\Files" `
  --columns-map "$base\Referrence Columns.csv" `
  --fallback-map "$base\Fallback Mapping CSV.csv" `
  --output-dir "$base\Output"
```

### Step 5 — Review output

- `Output\*.csv` — slim Case files (selected columns only)
- `Output\all_fallback_map.csv` — every ID that was replaced
- `Output\fallback_maps\` — same audit, split per source file
- Console **Summary** — kept / replaced / blank counts

## How it decides (short)

For each lookup cell (example: `ContactId`):

1. Look for that ID in `Existing Record\Contact.csv` → `External_Id__c`
2. If found → **keep**
3. If missing and ApplyFallback=Y → **replace with FallbackId** and log it
4. If blank → **leave blank**
5. If ApplyFallback=N (e.g. ParentId) → **leave as-is** even if missing

## Package contents

| File | Purpose |
|------|---------|
| `apply_case_reference_fallbacks.py` | Main script |
| `README.md` | This quick start |
| `HOW_IT_WORKS.md` | Fuller technical explanation |
| `samples/Referrence_Columns.csv` | Example columns map |
| `samples/Fallback_Mapping.csv` | Example fallback map |

## Need more detail?

See **HOW_IT_WORKS.md** in this package.
