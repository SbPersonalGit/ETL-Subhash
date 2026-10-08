# Automation Script

## Name
Replace Values in Files

## Script location
- Python script: `scripts/replace_values_in_files.py`
- Sample mapping file: `replace_mapping.csv` (project root)
- Combined-run helper: `run_replace_both.bat` (project root)

---

## Overview

This script supports two replacement modes. Both can run against individual files, multiple files, or entire folders (recursively).

| Mode | Purpose | Required inputs |
|------|---------|-----------------|
| **Generic mapping** | Replace exact old values with new values | `--mapping` CSV |
| **User cleanup** | Replace unsupported Salesforce User IDs (`005...`) with a fallback user ID | `--valid-users-file`, `--default-user-id` |

You can run either mode alone, or combine both in a single run.

---

## PowerShell vs Command Prompt

This project is typically run from **PowerShell** on Windows (`PS C:\ETL\ETL_For_Local_Only>`).

| Shell | Line continuation | Notes |
|-------|-------------------|-------|
| **PowerShell** | backtick `` ` `` | The backtick must be the **last character** on the line. No space after it. |
| **cmd.exe** | `^` | Do **not** use `^` in PowerShell. It causes `Missing expression after unary operator '--'`. |

Preferred: put the whole command on **one line**. Do not press Enter in the middle of a command unless the previous line ends with a backtick.

If PowerShell shows `>>`, it is waiting for the next line of a broken command. Press `Ctrl+C` and paste the full one-liner again.

Long commands often wrap in the chat window. If you paste and PowerShell splits them onto a second line (`>>`), use the helper instead:

```powershell
.\run_replace_both.bat
```

That runs both mapping replacement and user cleanup against `state\flat_file_drop\Case`.

**PowerShell one-liner:**
```powershell
python scripts/replace_values_in_files.py --valid-users-file existing_user.csv --default-user-id 005S900000dr9BaIAI --path state\flat_file_drop\Case
```

**PowerShell multi-line:**
```powershell
python scripts/replace_values_in_files.py `
  --valid-users-file existing_user.csv `
  --default-user-id 005S900000dr9BaIAI `
  --path state\flat_file_drop\Case
```

**cmd.exe multi-line (only if you are not in PowerShell):**
```bat
python scripts/replace_values_in_files.py ^
  --valid-users-file existing_user.csv ^
  --default-user-id 005S900000dr9BaIAI ^
  --path state\flat_file_drop\Case
```

---

## Mode 1: Generic Mapping Replacement

Use this when you have a sheet with two columns of values to swap — for example Salesforce IDs, account IDs, or any repeated text.

### Supported mapping CSV headers
The script accepts any of these column name pairs (case-insensitive):

| Old column | New column |
|------------|------------|
| `A` | `B` |
| `old_value` | `new_value` |
| `Old_entries` | `new_entries` |
| `old_entry` | `new_entry` |
| `from` / `source` | `to` / `target` |

### Example mapping CSV

```csv
Old_entries,new_entries
0051H000007TlU5QAK,005A0000002Sg01IAC
005A0000002DBgVIAW,005A0000002Sg01IAC
0051H000009S5L1QAK,005A0000002Sg01IAC
```

The first column is the value to find. The second column is the replacement value.

### Run commands (PowerShell)

**Single folder:**
```powershell
python scripts/replace_values_in_files.py --mapping replace_mapping.csv --path config\flat_file_templates
```

**Multiple folders or files** (repeat `--path`):
```powershell
python scripts/replace_values_in_files.py --mapping replace_mapping.csv --path config\flat_file_templates --path config\pipeline_designer
```

**Single file:**
```powershell
python scripts/replace_values_in_files.py --mapping replace_mapping.csv --path state\flat_file_drop\Case\2021_Case_1.csv
```

**Preview without writing:**
```powershell
python scripts/replace_values_in_files.py --mapping replace_mapping.csv --path config\flat_file_templates --dry-run
```

**Multi-line (PowerShell backtick):**
```powershell
python scripts/replace_values_in_files.py `
  --mapping replace_mapping.csv `
  --path config\flat_file_templates `
  --path config\pipeline_designer `
  --dry-run
```

---

## Mode 2: Salesforce User Cleanup

Use this when migrating data to a target Salesforce org. Provide the list of users that exist in the target org. The script scans all target files for Salesforce User IDs (values starting with `005`) and replaces any ID **not** in the valid-user list with a fallback user ID you specify.

### How it works
1. Reads the valid user list from a CSV or text file.
2. Scans every target file for Salesforce User IDs matching the `005` prefix pattern.
3. Keeps IDs that are in the valid-user list unchanged.
4. Replaces any unknown user ID with the provided `--default-user-id`.

This applies to user references in any field — `OwnerId`, `CreatedById`, `LastModifiedById`, manifest JSON files, URLs, etc.

### Example valid-users file

A simple text file with one user ID per line:
```txt
005S900000b19USIAY
005S900000blwJzIAI
005S900000aanQxIAI
```

A single-column CSV (header optional) also works. The script extracts every `005...` ID from the file; it does not depend on CSV delimiters:

```csv
id
005S900000b19USIAY
005S900000blwJzIAI
```

Or a CSV export from the target org (the script extracts any `005...` IDs found in any column):
```csv
Id,Username
005S900000b19USIAY,admin@targetorg.com
005S900000blwJzIAI,user1@targetorg.com
```

### Run commands (PowerShell)

**Single folder:**
```powershell
python scripts/replace_values_in_files.py --valid-users-file existing_user.csv --default-user-id 005S900000dr9BaIAI --path state\flat_file_drop\Case
```

**Multiple folders:**
```powershell
python scripts/replace_values_in_files.py --valid-users-file existing_user.csv --default-user-id 005S900000dr9BaIAI --path config\flat_file_templates --path config\pipeline_designer
```

**Preview without writing:**
```powershell
python scripts/replace_values_in_files.py --valid-users-file existing_user.csv --default-user-id 005S900000dr9BaIAI --path state\flat_file_drop\Case --dry-run
```

**Multi-line (PowerShell backtick):**
```powershell
python scripts/replace_values_in_files.py `
  --valid-users-file existing_user.csv `
  --default-user-id 005S900000dr9BaIAI `
  --path state\flat_file_drop\Case
```

---

## Combined Mode

Run both generic mapping and user cleanup in one pass.

**PowerShell one-liner:**
```powershell
python scripts/replace_values_in_files.py --mapping replace_mapping.csv --valid-users-file existing_user.csv --default-user-id 005S900000dr9BaIAI --path config\flat_file_templates --path config\pipeline_designer
```

**PowerShell multi-line:**
```powershell
python scripts/replace_values_in_files.py `
  --mapping replace_mapping.csv `
  --valid-users-file existing_user.csv `
  --default-user-id 005S900000dr9BaIAI `
  --path config\flat_file_templates `
  --path config\pipeline_designer
```

Mapping replacements are applied first, then user cleanup runs on the updated content.

---

## Input Paths

### Individual files
Pass one or more file paths with `--path`:
```powershell
python scripts/replace_values_in_files.py --mapping replace_mapping.csv --path file1.json --path file2.csv
```

### Folders (recursive)
When a folder is passed, the script processes **all files inside that folder and all subfolders**:
```powershell
python scripts/replace_values_in_files.py --mapping replace_mapping.csv --path config\flat_file_templates
```

### Path list file
Create a text file with one path per line, or a CSV with a `Path` column:

```txt
config/flat_file_templates
config/pipeline_designer
state/flat_file_drop/Case
```

Then run:
```powershell
python scripts/replace_values_in_files.py --mapping replace_mapping.csv --paths-file paths.txt
```

---

## All CLI Options

| Option | Description |
|--------|-------------|
| `--mapping` | CSV mapping file with old/new value columns |
| `--valid-users-file` | CSV or text file with target-org user IDs |
| `--default-user-id` | Fallback User ID for unknown users (required with `--valid-users-file`) |
| `--path` | File or directory to process (repeatable) |
| `--paths-file` | Text/CSV file listing paths to process |
| `--dry-run` | Preview changes without writing to disk |

At least one of `--mapping` or `--valid-users-file` must be provided.

---

## Notes
- Generic mode replaces **exact text matches** only. Longer values are replaced before shorter ones to avoid partial overlaps.
- User cleanup mode matches Salesforce User IDs using the `005` prefix pattern (15- or 18-character IDs).
- Both 15- and 18-character user ID forms are handled (matching is done on the 15-char prefix).
- The script processes files recursively when a directory is passed.
- Always test with `--dry-run` before applying changes to large data sets.
- Works best on text-based files: CSV, JSON, TXT, XML, etc.
- In **PowerShell**, do not use `^` to continue a command. Use a one-liner, or a backtick `` ` `` with no trailing space.
