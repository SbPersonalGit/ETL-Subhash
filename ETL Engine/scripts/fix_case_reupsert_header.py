from pathlib import Path


path = Path("state/flat_file_drop/Case/Case_ReUpsert.csv")
raw = path.read_bytes()
newline_at = raw.find(b"\n")
if newline_at < 0:
    raise ValueError(f"CSV has no header line: {path}")

header = raw[:newline_at]
old = b"Legacy_Case_Number__c "
new = b"Legacy_Case_Number__c"
count = header.count(old)
if count != 1:
    raise ValueError(f"Expected exactly one malformed header, found {count}")

updated_header = header.replace(old, new, 1)
path.write_bytes(updated_header + raw[newline_at:])
print(f"Corrected trailing space in {path}")
