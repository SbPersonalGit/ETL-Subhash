# Flat File Save Draft Template — 500 Error

## Issue

Clicking **Save Draft Template** on Flat File Migration returned:

```text
Request failed (500)
```

Reproduced while creating a template from:

```text
state/flat_file_drop/01 Platform Base Data (Tiers, Users, Accounts)/Users/User.csv
```

The failure appeared as a generic HTTP 500 rather than a normal validation message. The CSV itself was not the primary cause.

## RCA (Root Cause Analysis)

In `ui/app.py` → `save_flat_file_template()` (`POST /api/flat-files/templates`), a recent change added a drop-folder integrity check:

```python
validate_flat_file_reference(source_path, file_config)
```

Two defects made this crash:

1. **`source_path` was never defined** in that code path.  
   Earlier in the same function, a local `path` variable was resolved for each file, but the new call used `source_path`, which did not exist in scope.

2. **The call was nested inside the per-mapping loop.**  
   It ran once per field mapping instead of once per file, and still depended on the undefined variable.

Resulting exception:

```text
NameError: name 'source_path' is not defined
```

Because the route had no handler for that unexpected exception, Flask returned **HTTP 500**.

This affected any Save Draft / Save Template request that reached the identity/lookup post-check block — including drop-folder packages such as `User.csv`.

## Solution

Updated `save_flat_file_template()` to:

1. Resolve the source file path explicitly per file:

   ```python
   source_path = resolve_flat_file_path(
       file_config, batch_dir, FLAT_FILE_DROP_DIR, prefer_cleaned=False,
   )
   ```

2. Call `validate_flat_file_reference` **once per file**, outside the mapping loop.

3. Catch `FlatFileValidationError` and append it to the normal validation `errors` list, so a changed drop-folder file returns **HTTP 400** with a clear message instead of crashing.

### Expected behavior after fix

| Case | Result |
|---|---|
| Valid draft save | Template saved successfully |
| Drop-folder file size changed after import | HTTP 400 with “changed size… re-import” message |
| Drop-folder mtime-only drift (OneDrive/sync), same size | Allowed; stored mtime is refreshed |
| Undefined path / unexpected crash | No longer occurs for this check |

### Follow-up (2026-08-05)

Users on OneDrive saw:

```text
Drop-folder file changed after template registration: User.csv. Re-import it.
```

even when `User.csv` content/size was unchanged. Root cause: `source_mtime_ns` equality is unstable under OneDrive/antivirus retouches. Solution: fail only on **size** change; refresh mtime when size matches.
