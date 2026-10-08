# How Case Reference Fallbacks Works

## Purpose

Salesforce Case CSVs from source often contain lookup IDs (`ContactId`, `OwnerId`, `AccountId`, …) that do not exist in target UAT. Loading those rows fails foreign-key validation.

This script:

1. Builds a **slim** Case CSV with only configured columns.
2. For each lookup: **keep** if the ID exists in UAT (via Existing Record), else **replace** with a configured fallback ID when allowed.
3. Writes an **audit map** of every replacement.

## Inputs

| Input | Role |
|-------|------|
| `Files/` | Source Case CSVs |
| `Existing Record/` | Object CSVs already in UAT; identity column `External_Id__c` |
| `Referrence Columns.csv` | Columns to output, rename, preserve, and whether fallback applies |
| `Fallback Mapping CSV.csv` | Default UAT Id per referenced object |

## Matching rule (important)

The script does **not** search Case `Id` in Existing Record.

It searches each **lookup field value** from the Case file against `External_Id__c` in the **matching object** file:

- `ContactId` → `Existing Record\Contact.csv` → `External_Id__c`
- `OwnerId` → `Existing Record\User.csv` → `External_Id__c`
- `AccountId` → `Existing Record\Account.csv` → `External_Id__c`

Id matching uses Salesforce variants (exact, 15-char prefix case-sensitive, 18-char casefold).

## Decision flow

```text
lookup value
  ├─ blank?                         → leave blank
  ├─ in Existing[ReferencedObject]? → keep original
  ├─ missing + ApplyFallback = N?   → keep original (e.g. ParentId)
  └─ missing + ApplyFallback = Y?   → write FallbackId + audit row
```

If `PreserveOriginalAs` is set, the original value is always copied to that extra column (even when the working column is replaced).

## Outputs

| Output | Contents |
|--------|----------|
| `Output/<same filename>.csv` | Slim Case file; same row count/order as source |
| `Output/fallback_maps/<file>_fallback_map.csv` | Replacements for that file |
| `Output/all_fallback_map.csv` | All replacements combined |

Audit columns: `SourceFile`, `Id`, `CaseNumber`, `Column`, `ReferencedObject`, `OriginalValue`, `FallbackId`

## Summary counters

| Stat | Meaning |
|------|---------|
| kept present | Lookup matched Existing Record |
| replacements | Missing from Existing; fallback applied |
| left unchanged (missing) | Missing, no fallback configured |
| blank skipped | Empty lookup cells |

## Column map fields

| Field | Meaning |
|-------|---------|
| `Column` | Field name in Case Files |
| `ReferencedObject` | Empty = identity/rename only; else lookup object |
| `ApplyFallback` | `Y` = replace missing; `N` = pass through |
| `OutputColumn` | Name in Output (can rename) |
| `PreserveOriginalAs` | Optional column that always stores pre-fallback value |

Fallback Id resolution: column-specific override (if present) → else object-level FallbackId.
