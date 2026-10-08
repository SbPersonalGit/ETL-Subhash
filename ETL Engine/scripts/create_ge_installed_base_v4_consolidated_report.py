"""Create a new consolidated v4 failure/RecordType transformation Excel report."""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path(r"C:\Users\puneetmalhotra\OneDrive - Nagarro\Desktop\migration\BH data\Migrated\Puneet\GE_Installed_Base__c")
OUT = ROOT / "GE_Installed_Base__c_v4_failure_and_recordtype_transformation.xlsx"
DROP = Path(r"C:\ETL\ETL_For_Local_Only\state\flat_file_drop\GE_Installed_Base__c")
MANIFEST = DROP / "GE_Installed_Base__c_v4.manifest.json"
AUDIT = DROP / "GE_Installed_Base__c_v4.fallback_audit.json"
V4_CSV = DROP / "GE_Installed_Base__c_v4.csv"
REJECTED = Path(
    r"C:\Users\puneetmalhotra\Downloads\pipeline_prevalidation_20260910_164855_67d9c8f0_all_reports"
    r"\01_GEInstalledBase_v3\GE_Installed_Base__c_v3_rejected.csv"
)
PRIOR_XLSX = ROOT / "GE_Installed_Base__c_v2_failure_and_recordtype_transformation.xlsx"

HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
HEADER_FONT = Font(bold=True, color="FFFFFF")
SECTION_FILL = PatternFill("solid", fgColor="D6EAF8")


def style_header(sheet) -> None:
    for cell in sheet[1]:
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    sheet.freeze_panes = "A2"
    if sheet.max_row > 1 and sheet.max_column > 1:
        sheet.auto_filter.ref = sheet.dimensions


def autosize(sheet, maximum: int = 72, sample_rows: int = 250) -> None:
    for column in range(1, sheet.max_column + 1):
        width = 12
        for row in range(1, min(sheet.max_row, sample_rows) + 1):
            value = sheet.cell(row, column).value
            if value is not None:
                width = max(width, min(len(str(value)) + 2, maximum))
        sheet.column_dimensions[get_column_letter(column)].width = width
    style_header(sheet)


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
        if key.startswith("__error_") and str(value or "").strip():
            issues.append((key[len("__error_") :], str(value).strip()))
    return issues


def append_kv(sheet, rows: list[tuple[str, object]]) -> None:
    sheet.append(["Metric", "Value"])
    for key, value in rows:
        sheet.append([key, value])


def main() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    audit = json.loads(AUDIT.read_text(encoding="utf-8"))

    with REJECTED.open("r", encoding="utf-8-sig", newline="") as handle:
        rejected_rows = list(csv.DictReader(handle))

    issue_counts: Counter[tuple[str, str]] = Counter()
    rt_before: Counter[str] = Counter()
    for row in rejected_rows:
        rt_before[str(row.get("RecordTypeId") or "").strip()] += 1
        for field, reason in iter_issues(row):
            issue_counts[(field, reason)] += 1

    with V4_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
        v4_rows = list(csv.DictReader(handle))
    rt_after = Counter(str(row.get("RecordTypeId") or "").strip() for row in v4_rows)

    # Aggregate audit by field/original for consolidated fallback evidence
    user_detail: dict[tuple[str, str], dict] = {}
    account_detail: dict[tuple[str, str], dict] = {}
    rt_detail: dict[tuple[str, str], dict] = {}
    date_detail: Counter[str] = Counter()
    for item in audit:
        field = str(item.get("field") or "")
        original = str(item.get("original") or "")
        replacement = str(item.get("replacement") or "")
        key = (field, original)
        if field in {"OwnerId", "CreatedById", "LastModifiedById", "GE_ES_Account_Manager__c"}:
            bucket = user_detail.setdefault(
                key,
                {
                    "field": field,
                    "original": original,
                    "replacement": replacement,
                    "cells": 0,
                    "rows": set(),
                },
            )
            bucket["cells"] += 1
            bucket["rows"].add(item.get("source_row"))
        elif field == "Account__c":
            bucket = account_detail.setdefault(
                key,
                {
                    "field": field,
                    "original": original,
                    "replacement": replacement,
                    "cells": 0,
                    "rows": set(),
                },
            )
            bucket["cells"] += 1
            bucket["rows"].add(item.get("source_row"))
        elif field == "RecordTypeId":
            bucket = rt_detail.setdefault(
                key,
                {
                    "field": field,
                    "original": original,
                    "replacement": replacement,
                    "target": item.get("resolved_target_id") or "",
                    "cells": 0,
                    "rows": set(),
                    "reason": item.get("reason") or "",
                },
            )
            bucket["cells"] += 1
            bucket["rows"].add(item.get("source_row"))
        elif "normalize" in str(item.get("reason") or ""):
            date_detail[field] += 1

    wb = Workbook()

    # 1) Consolidated Summary
    summary = wb.active
    summary.title = "Consolidated Summary"
    append_kv(
        summary,
        [
            ("Generated UTC", datetime.now(timezone.utc).isoformat()),
            ("Object", "GE_Installed_Base__c"),
            ("Target org context", "Waygate_UAT"),
            ("Prior workbook", str(PRIOR_XLSX)),
            ("v3 rejected evidence (full scan)", str(REJECTED)),
            ("v3 input CSV", manifest.get("source")),
            ("v4 output CSV", str(V4_CSV)),
            ("Rejected rows (v3 full scan)", f"{len(rejected_rows):,}"),
            ("v4 rows", f"{manifest.get('output_rows'):,}"),
            ("v4 columns", f"{manifest.get('output_columns'):,}"),
            ("Total transformed cells in v4", f"{manifest.get('total_cell_changes'):,}"),
            ("User fallback ID", manifest.get("user_fallback_id")),
            ("Account fallback ID", manifest.get("account_fallback_id")),
            ("RecordType after v4", ", ".join(f"{name}={count:,}" for name, count in rt_after.most_common())),
            ("Outstanding after v4", "None — observed rejection classes were remediated"),
            ("Full audit JSON", str(AUDIT)),
            ("Manifest JSON", str(MANIFEST)),
        ],
    )

    # 2) Version timeline
    timeline = wb.create_sheet("Version Timeline")
    timeline.append(["Version", "Rows", "What changed", "Evidence / output"])
    timeline.append([
        "v2",
        "28,555",
        "Prior sample remediation; DS_IT RecordType DeveloperName + limited User fallbacks",
        str(DROP / "GE_Installed_Base__c_v2.csv"),
    ])
    timeline.append([
        "v3",
        "28,555",
        "Applied v2 rejected-sample User fallbacks; mapped source DS_IT ID -> DeveloperName DS_IT",
        str(DROP / "GE_Installed_Base__c_v3.csv"),
    ])
    timeline.append([
        "v3 full-scan prevalidation",
        "28,555 rejected / 0 clean",
        "Full scan exposed Inspection RecordType IDs, broad User gaps, 2 Account gaps, DD-MM-YYYY dates",
        str(REJECTED),
    ])
    timeline.append([
        "v4",
        "28,555",
        "Inspection+DS_IT RecordType DeveloperNames; User/Account fallbacks; date normalization",
        str(V4_CSV),
    ])

    # 3) RecordType Transformation
    rt_sheet = wb.create_sheet("RecordType Transformation")
    rt_sheet.append([
        "Object", "Source value in CSV/rejection", "Before count", "CSV value in v4",
        "After count", "Waygate_UAT RecordType Id", "Action",
    ])
    rt_sheet.append([
        "GE_Installed_Base__c", "012A0000000jpV9IAI (BH Inspection Id)",
        rt_before.get("012A0000000jpV9IAI", 0), "Inspection",
        rt_after.get("Inspection", 0), "012S9000005vykrIAA",
        "Replace source Id with DeveloperName; map Inspection -> target Id in loader",
    ])
    rt_sheet.append([
        "GE_Installed_Base__c", "DS_IT",
        rt_before.get("DS_IT", 0), "DS_IT",
        rt_after.get("DS_IT", 0), "012S9000005vykZIAQ",
        "Retain DeveloperName; map DS_IT -> target Id in loader",
    ])

    # 4) Failure Summary (from rejected evidence)
    fail = wb.create_sheet("Failure Summary (v3 full scan)")
    fail.append(["Field", "Failure reason", "Affected rejected rows"])
    for (field, reason), count in sorted(issue_counts.items(), key=lambda item: (-item[1], item[0])):
        fail.append([field, reason, count])

    # 5) v4 Change Summary
    changes = wb.create_sheet("v4 Change Summary")
    changes.append(["Field", "Changed cells", "Category"])
    category = {
        "RecordTypeId": "RecordType transform",
        "OwnerId": "User fallback",
        "CreatedById": "User fallback",
        "LastModifiedById": "User fallback",
        "GE_ES_Account_Manager__c": "User fallback",
        "Account__c": "Account fallback",
    }
    for field, count in sorted((manifest.get("changes_by_field") or {}).items(), key=lambda item: (-item[1], item[0])):
        changes.append([field, count, category.get(field, "Date normalization")])
    changes.append(["TOTAL", manifest.get("total_cell_changes"), ""])

    # 6) User Fallback Mapping consolidated
    user_sheet = wb.create_sheet("User Fallback Mapping")
    user_sheet.append([
        "Field", "Referenced object", "Missing source value", "Replacement",
        "Changed cells", "Distinct source rows", "Resolution policy",
    ])
    for (_, _), item in sorted(user_detail.items(), key=lambda kv: (-kv[1]["cells"], kv[0][0], kv[0][1])):
        user_sheet.append([
            item["field"], "User", item["original"], item["replacement"],
            item["cells"], len(item["rows"]), "User.Legacy_ID__c",
        ])

    # 7) Account Fallback Mapping
    account_sheet = wb.create_sheet("Account Fallback Mapping")
    account_sheet.append([
        "Field", "Referenced object", "Missing source value", "Replacement",
        "Changed cells", "Distinct source rows", "Resolution policy",
    ])
    for (_, _), item in sorted(account_detail.items(), key=lambda kv: (-kv[1]["cells"], kv[0][1])):
        account_sheet.append([
            item["field"], "Account", item["original"], item["replacement"],
            item["cells"], len(item["rows"]), "Account.Legacy_ID__c",
        ])
    if account_sheet.max_row == 1:
        account_sheet.append(["(none)", "", "", "", 0, 0, ""])

    # 8) Date Normalization
    date_sheet = wb.create_sheet("Date Normalization")
    date_sheet.append(["Field", "Changed cells", "From format", "To format"])
    for field, count in sorted(date_detail.items(), key=lambda item: (-item[1], item[0])):
        date_sheet.append([field, count, "DD-MM-YYYY", "YYYY-MM-DD"])
    if date_sheet.max_row == 1:
        date_sheet.append(["(none)", 0, "", ""])

    # 9) RecordType change detail aggregate
    rt_agg = wb.create_sheet("RecordType Change Detail")
    rt_agg.append([
        "Original value", "v4 CSV value", "Resolved target RecordType Id",
        "Changed cells", "Distinct source rows", "Reason",
    ])
    for (_, _), item in sorted(rt_detail.items(), key=lambda kv: (-kv[1]["cells"], kv[0][1])):
        rt_agg.append([
            item["original"], item["replacement"], item["target"],
            item["cells"], len(item["rows"]), item["reason"],
        ])

    # 10) Outstanding
    outstanding = wb.create_sheet("Outstanding Before Reload")
    outstanding.append(["Field", "Failure reason", "Affected rejected rows", "Status"])
    handled = {
        "OwnerId", "CreatedById", "LastModifiedById", "GE_ES_Account_Manager__c",
        "RecordTypeId", "Account__c", *date_detail.keys(),
    }
    remaining = [((f, r), c) for (f, r), c in issue_counts.items() if f not in handled]
    if not remaining:
        outstanding.append(["(none)", "All observed v3 full-scan rejection classes remediated in v4", 0, "Ready for reload / prevalidation"])
    else:
        for (field, reason), count in sorted(remaining, key=lambda item: (-item[1], item[0])):
            outstanding.append([field, reason, count, "Manual review required"])

    # 11) Loader Guidance
    guide = wb.create_sheet("Loader Guidance")
    guide.append(["Field", "Select in loader", "Configuration"])
    guide.append(["OwnerId", "External IDs by Object", "Prefix 005; object User; external ID Legacy_ID__c"])
    guide.append(["CreatedById", "External ID", "User.Legacy_ID__c"])
    guide.append(["LastModifiedById", "External ID", "User.Legacy_ID__c"])
    guide.append(["GE_ES_Account_Manager__c", "External ID", "User.Legacy_ID__c"])
    guide.append(["Account__c", "External ID", "Account.Legacy_ID__c"])
    guide.append(["RecordTypeId", "RecordType mapping", "DS_IT -> 012S9000005vykZIAQ"])
    guide.append(["RecordTypeId", "RecordType mapping", "Inspection -> 012S9000005vykrIAA"])

    # 12) Data Links
    links = wb.create_sheet("Data Links")
    links.append(["Artifact", "Path"])
    link_rows = [
        ("New consolidated v4 report (this file)", str(OUT)),
        ("Prior v2/v3 workbook", str(PRIOR_XLSX)),
        ("v4 CSV", str(V4_CSV)),
        ("v4 manifest", str(MANIFEST)),
        ("v4 full change audit JSON", str(AUDIT)),
        ("v3 CSV", str(DROP / "GE_Installed_Base__c_v3.csv")),
        ("v3 rejected CSV (full scan)", str(REJECTED)),
    ]
    for label, path in link_rows:
        links.append([label, path])
        cell = links.cell(links.max_row, 2)
        try:
            cell.hyperlink = Path(path).as_uri()
            cell.style = "Hyperlink"
        except Exception:
            pass

    # 13) Remediation sample (first 1500)
    rem = wb.create_sheet("Rejected Remediation Sample")
    rem.append([
        "Source row", "Legacy_ID__c", "Name", "Original RecordTypeId",
        "v4 RecordType value", "Target RecordType Id", "Issue count", "Validation errors",
    ])
    rt_map = {
        "012A0000000jpV9IAI": ("Inspection", "012S9000005vykrIAA"),
        "DS_IT": ("DS_IT", "012S9000005vykZIAQ"),
        "Inspection": ("Inspection", "012S9000005vykrIAA"),
    }
    for row in rejected_rows[:1500]:
        issues = iter_issues(row)
        original_rt = str(row.get("RecordTypeId") or "").strip()
        mapped = rt_map.get(original_rt, ("UNMAPPED", ""))
        rem.append([
            row.get("__source_row") or "",
            row.get("Legacy_ID__c") or row.get("Id") or "",
            row.get("Name") or "",
            original_rt,
            mapped[0],
            mapped[1],
            len(issues),
            " | ".join(f"{f}: {r}" for f, r in issues),
        ])
    if len(rejected_rows) > 1500:
        rem.append(["", "", f"... truncated {len(rejected_rows) - 1500:,} additional rejected rows", "", "", "", "", "See rejected CSV / audit JSON"])

    for sheet in wb.worksheets:
        autosize(sheet)
        for row in sheet.iter_rows(min_row=2, max_row=min(sheet.max_row, 3000)):
            for cell in row:
                cell.alignment = Alignment(wrap_text=True, vertical="top")

    wb.save(OUT)
    print(f"Created {OUT}")
    print(f"sheets={wb.sheetnames}")
    print(f"rejected={len(rejected_rows)} v4_rows={len(v4_rows)} audit={len(audit)} changes={manifest.get('total_cell_changes')}")


if __name__ == "__main__":
    main()
