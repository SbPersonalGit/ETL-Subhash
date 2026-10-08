"""Create GE_Installed_Base__c_v4.csv from v3 using full-scan rejection evidence."""
from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

USER_FALLBACK_ID = "005S900000dr9BaIAI"
ACCOUNT_FALLBACK_ID = "001S900001X3zMoIAJ"

# CSV keeps DeveloperName; loader maps DeveloperName -> Waygate_UAT RecordType Id
RECORD_TYPE_MAP = {
    "012A0000000jpV9IAI": {
        "developer_name": "Inspection",
        "target_id": "012S9000005vykrIAA",
        "reason": "cross-org RecordType transformation by DeveloperName (Inspection)",
    },
    "DS_IT": {
        "developer_name": "DS_IT",
        "target_id": "012S9000005vykZIAQ",
        "reason": "retain DeveloperName for loader RecordType mapping (DS_IT)",
    },
    "012120000019jwqAAA": {
        "developer_name": "DS_IT",
        "target_id": "012S9000005vykZIAQ",
        "reason": "cross-org RecordType transformation by DeveloperName (DS_IT)",
    },
}

USER_COLS = {
    "OwnerId",
    "CreatedById",
    "LastModifiedById",
    "GE_ES_Account_Manager__c",
}
ACCOUNT_COLS = {"Account__c"}
DMY = re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{4})$")
YMD = re.compile(r"^\d{4}-\d{2}-\d{2}$")
USER_REF = re.compile(r"reference '([^']+)' not found in User\.Legacy_ID__c")
ACCOUNT_REF = re.compile(r"reference '([^']+)' not found in Account\.Legacy_ID__c")


def normalize_date(value: str) -> tuple[str, bool]:
    raw = (value or "").strip()
    if not raw or YMD.match(raw):
        return raw, False
    match = DMY.match(raw)
    if not match:
        return raw, False
    day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d"), True
    except ValueError:
        return raw, False


def iter_issues(row: dict[str, str]) -> list[tuple[str, str]]:
    issues: list[tuple[str, str]] = []
    combined = str(row.get("__validation_errors") or "").strip()
    if combined:
        for part in combined.split(" | "):
            field, sep, reason = part.partition(": ")
            if sep:
                issues.append((field.strip(), reason.strip()))
            elif part.strip():
                issues.append(("General", part.strip()))
        return issues
    for key, value in row.items():
        if not key.startswith("__error_"):
            continue
        reason = str(value or "").strip()
        if reason:
            issues.append((key[len("__error_") :], reason))
    return issues


def main(source: Path, rejected: Path, output: Path) -> None:
    missing_users: dict[str, set[str]] = defaultdict(set)
    missing_accounts: dict[str, set[str]] = defaultdict(set)
    issue_counts: Counter[tuple[str, str]] = Counter()

    with rejected.open("r", encoding="utf-8-sig", newline="") as handle:
        rejected_rows = list(csv.DictReader(handle))
    for row in rejected_rows:
        for field, reason in iter_issues(row):
            issue_counts[(field, reason)] += 1
            if field in USER_COLS:
                match = USER_REF.search(reason)
                if match:
                    missing_users[field].add(match.group(1))
            if field in ACCOUNT_COLS:
                match = ACCOUNT_REF.search(reason)
                if match:
                    missing_accounts[field].add(match.group(1))

    changes: Counter[str] = Counter()
    audit: list[dict] = []
    output.parent.mkdir(parents=True, exist_ok=True)

    with source.open("r", encoding="utf-8-sig", newline="") as source_handle, output.open(
        "w", encoding="utf-8-sig", newline=""
    ) as output_handle:
        reader = csv.DictReader(source_handle)
        headers = list(reader.fieldnames or [])
        required = {"Id", "RecordTypeId", *missing_users, *missing_accounts}
        absent = sorted(required - set(headers))
        if absent:
            raise ValueError(f"Source CSV is missing required columns: {', '.join(absent)}")

        # Date columns that appeared in rejection evidence
        date_fields = sorted({field for field, _ in issue_counts if "expected YYYY-MM-DD" in _})
        writer = csv.DictWriter(output_handle, fieldnames=headers)
        writer.writeheader()
        row_count = 0
        for source_row, row in enumerate(reader, start=2):
            row_count += 1
            record_key = row.get("Id", "") or row.get("Legacy_ID__c", "")

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
                        "resolved_target_id": USER_FALLBACK_ID,
                        "reason": "missing User.Legacy_ID__c reference",
                    })

            for field, missing_values in missing_accounts.items():
                original = str(row.get(field) or "").strip()
                if original and original in missing_values:
                    row[field] = ACCOUNT_FALLBACK_ID
                    changes[field] += 1
                    audit.append({
                        "source_row": source_row,
                        "record_id": record_key,
                        "field": field,
                        "original": original,
                        "replacement": ACCOUNT_FALLBACK_ID,
                        "resolved_target_id": ACCOUNT_FALLBACK_ID,
                        "reason": "missing Account.Legacy_ID__c reference",
                    })

            original_rt = str(row.get("RecordTypeId") or "").strip()
            rt_map = RECORD_TYPE_MAP.get(original_rt)
            if rt_map and original_rt != rt_map["developer_name"]:
                row["RecordTypeId"] = rt_map["developer_name"]
                changes["RecordTypeId"] += 1
                audit.append({
                    "source_row": source_row,
                    "record_id": record_key,
                    "field": "RecordTypeId",
                    "original": original_rt,
                    "replacement": rt_map["developer_name"],
                    "resolved_target_id": rt_map["target_id"],
                    "reason": rt_map["reason"],
                })

            for field in date_fields:
                if field not in row:
                    continue
                new_value, changed = normalize_date(str(row.get(field) or ""))
                if changed:
                    original = str(row.get(field) or "")
                    row[field] = new_value
                    changes[field] += 1
                    audit.append({
                        "source_row": source_row,
                        "record_id": record_key,
                        "field": field,
                        "original": original,
                        "replacement": new_value,
                        "resolved_target_id": "",
                        "reason": "normalize DD-MM-YYYY to YYYY-MM-DD",
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
        "account_fallback_id": ACCOUNT_FALLBACK_ID,
        "missing_user_values_by_field": {
            field: sorted(values) for field, values in missing_users.items()
        },
        "missing_account_values_by_field": {
            field: sorted(values) for field, values in missing_accounts.items()
        },
        "record_type_mappings": [
            {
                "source_value": source_value,
                "developer_name": meta["developer_name"],
                "target_id": meta["target_id"],
            }
            for source_value, meta in RECORD_TYPE_MAP.items()
        ],
        "date_fields_normalized": date_fields,
        "issue_counts": [
            {"field": field, "reason": reason, "count": count}
            for (field, reason), count in sorted(issue_counts.items(), key=lambda item: (-item[1], item[0]))
        ],
        "changes_by_field": dict(changes),
        "total_cell_changes": int(sum(changes.values())),
        "audit_file": str(output.with_suffix(".fallback_audit.json").resolve()),
        "rejected_row_count": len(rejected_rows),
    }
    output.with_suffix(".fallback_audit.json").write_text(
        json.dumps(audit, indent=2), encoding="utf-8"
    )
    output.with_suffix(".manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit(
            "Usage: prepare_ge_installed_base_v4.py SOURCE_v3.csv REJECTED_v3.csv OUTPUT_v4.csv"
        )
    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
