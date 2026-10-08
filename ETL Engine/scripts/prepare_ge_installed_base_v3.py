from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


USER_FALLBACK_ID = "005S900000dr9BaIAI"
SOURCE_RECORD_TYPE_ID = "012120000019jwqAAA"
RECORD_TYPE_DEVELOPER_NAME = "DS_IT"
TARGET_RECORD_TYPE_ID = "012S9000005vykZIAQ"
USER_ERROR = re.compile(r"^([^:]+): reference '([^']+)' not found in User\.Legacy_ID__c$")


def main(source: Path, rejected: Path, output: Path) -> None:
    missing_users: dict[str, set[str]] = defaultdict(set)
    with rejected.open("r", encoding="utf-8-sig", newline="") as handle:
        rejected_rows = list(csv.DictReader(handle))
    for row in rejected_rows:
        for issue in str(row.get("__validation_errors") or "").split(" | "):
            match = USER_ERROR.fullmatch(issue.strip())
            if match:
                missing_users[match.group(1)].add(match.group(2))

    changes: Counter[str] = Counter()
    audit: list[dict] = []
    output.parent.mkdir(parents=True, exist_ok=True)
    with source.open("r", encoding="utf-8-sig", newline="") as source_handle, output.open(
        "w", encoding="utf-8-sig", newline=""
    ) as output_handle:
        reader = csv.DictReader(source_handle)
        headers = list(reader.fieldnames or [])
        required = {"Id", "RecordTypeId", *missing_users}
        absent = sorted(required - set(headers))
        if absent:
            raise ValueError(f"Source CSV is missing required columns: {', '.join(absent)}")
        writer = csv.DictWriter(output_handle, fieldnames=headers)
        writer.writeheader()
        row_count = 0
        for source_row, row in enumerate(reader, start=2):
            row_count += 1
            record_key = row.get("Id", "")
            for field, missing_values in missing_users.items():
                original = str(row.get(field) or "").strip()
                if original and original in missing_values:
                    row[field] = USER_FALLBACK_ID
                    changes[field] += 1
                    audit.append({
                        "source_row": source_row,
                        "record_id": record_key,
                        "field": field,
                        "original": original,
                        "replacement": USER_FALLBACK_ID,
                        "reason": "missing User.Legacy_ID__c reference",
                    })
            if str(row.get("RecordTypeId") or "").strip() == SOURCE_RECORD_TYPE_ID:
                row["RecordTypeId"] = RECORD_TYPE_DEVELOPER_NAME
                changes["RecordTypeId"] += 1
                audit.append({
                    "source_row": source_row,
                    "record_id": record_key,
                    "field": "RecordTypeId",
                    "original": SOURCE_RECORD_TYPE_ID,
                    "replacement": RECORD_TYPE_DEVELOPER_NAME,
                    "resolved_target_id": TARGET_RECORD_TYPE_ID,
                    "reason": "cross-org RecordType transformation by DeveloperName",
                })
            writer.writerow(row)

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": str(source.resolve()),
        "rejected_evidence": str(rejected.resolve()),
        "output": str(output.resolve()),
        "input_rows": row_count,
        "output_rows": row_count,
        "input_columns": len(headers),
        "output_columns": len(headers),
        "user_fallback_id": USER_FALLBACK_ID,
        "missing_user_values_by_field": {field: sorted(values) for field, values in missing_users.items()},
        "record_type_mapping": {
            "source_id": SOURCE_RECORD_TYPE_ID,
            "developer_name": RECORD_TYPE_DEVELOPER_NAME,
            "target_id": TARGET_RECORD_TYPE_ID,
        },
        "changes_by_field": dict(changes),
        "total_cell_changes": sum(changes.values()),
        "audit_file": str(output.with_suffix(".fallback_audit.json").resolve()),
    }
    output.with_suffix(".fallback_audit.json").write_text(json.dumps(audit, indent=2), encoding="utf-8")
    output.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("Usage: prepare_ge_installed_base_v3.py SOURCE.csv REJECTED.csv OUTPUT.csv")
    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
