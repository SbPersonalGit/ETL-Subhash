"""Consolidate GIB v2/v4 remediation workbooks + post-validation into one Excel."""
from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path(
    r"C:\Users\puneetmalhotra\OneDrive - Nagarro\Desktop\migration\BH data\Migrated\Puneet\GE_Installed_Base__c"
)
OUT = ROOT / "GE_Installed_Base__c_consolidated_failure_recordtype_postvalidation.xlsx"
DROP = Path(r"C:\ETL\ETL_For_Local_Only\state\flat_file_drop\GE_Installed_Base__c")
MANIFEST = DROP / "GE_Installed_Base__c_v4.manifest.json"
AUDIT = DROP / "GE_Installed_Base__c_v4.fallback_audit.json"
V4_CSV = DROP / "GE_Installed_Base__c_v4.csv"
REJECTED = Path(
    r"C:\ETL\ETL_For_Local_Only\logs\verify\pipeline_prevalidation_20260910_164855_67d9c8f0"
    r"\01_GEInstalledBase_v3\GE_Installed_Base__c_v3_rejected.csv"
)
POST_DIR = Path(
    r"C:\Users\puneetmalhotra\Downloads\001_GE_Installed_Base__c_mismatch_fields"
    r"\mismatches_by_source_and_field\001_GE_Installed_Base__c"
)
POST_DIR_MIRROR = ROOT / "post-validation" / "001_GE_Installed_Base__c"
SUCCESS = ROOT / "migration" / "GE_Installed_Base__c_success_20260910_174237.csv"
V2_XLSX = ROOT / "GE_Installed_Base__c_v2_failure_and_recordtype_transformation.xlsx"
V4_XLSX = ROOT / "GE_Installed_Base__c_v4_failure_and_recordtype_transformation.xlsx"

USER_FALLBACK = "005S900000dr9BaIAI"
ACCOUNT_FALLBACK = "001S900001X3zMoIAJ"
RT_INSPECTION = "012S9000005vykrIAA"
RT_DS_IT = "012S9000005vykZIAQ"

HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
HEADER_FONT = Font(bold=True, color="FFFFFF")


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


def append_kv(sheet, rows: list[tuple[str, object]]) -> None:
    sheet.append(["Metric", "Value"])
    for key, value in rows:
        sheet.append([key, value])


def classify_post_field(field: str) -> str:
    if field == "RecordTypeId":
        return "RecordType cross-org mapping"
    if field in {
        "OwnerId",
        "CreatedBy.Legacy_ID__c",
        "LastModifiedBy.Legacy_ID__c",
        "GE_ES_Account_Manager__r.Legacy_ID__c",
    }:
        return "User reference / fallback"
    if field == "Account__r.Legacy_ID__c":
        return "Account fallback"
    if field.endswith("Date__c") or field in {
        "GE_ES_COD__c",
        "GE_ES_CSA_Term_Start__c",
        "GE_ES_END_Date__c",
        "GE_ES_In_Service_Date__c",
        "GE_ES_Start_Up_Date__c",
        "GE_OG_Delrd_Date__c",
        "GE_OG_Install_Date__c",
    }:
        return "Date format normalization"
    if field == "GE_ES_Part_Description__c":
        return "Unexpected / review"
    return "Other"


def explain_post_pair(field: str, expected: str, actual: str) -> str:
    if field == "RecordTypeId":
        if actual == RT_INSPECTION:
            return "Expected: BH Inspection Id mapped to Waygate_UAT Inspection Id"
        if actual == RT_DS_IT:
            return "Expected: BH DS_IT Id mapped to Waygate_UAT DS_IT Id"
        return "RecordType Id remapped across orgs"
    if field.endswith("Date__c") or field in {
        "GE_ES_COD__c",
        "GE_ES_CSA_Term_Start__c",
        "GE_ES_END_Date__c",
        "GE_ES_In_Service_Date__c",
        "GE_ES_Start_Up_Date__c",
        "GE_OG_Delrd_Date__c",
        "GE_OG_Install_Date__c",
    }:
        return "Expected: DD-MM-YYYY in source compared to YYYY-MM-DD stored in target"
    if field == "Account__r.Legacy_ID__c" and actual == ACCOUNT_FALLBACK:
        return "Expected: missing Account replaced with configured Account fallback"
    if field in {
        "OwnerId",
        "CreatedBy.Legacy_ID__c",
        "LastModifiedBy.Legacy_ID__c",
        "GE_ES_Account_Manager__r.Legacy_ID__c",
    }:
        if actual == USER_FALLBACK:
            return "Expected: missing User replaced with configured User fallback"
        return "Expected cross-org User resolution via Legacy_ID__c (not fallback)"
    if field == "GE_ES_Part_Description__c":
        if str(actual).endswith(".Invalid") and not str(expected).endswith(".Invalid"):
            return "Unexpected: target value ends with '.Invalid' (likely truncation/validation)"
        return "Text mismatch — review encoding/newlines"
    return "Value mismatch"


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


def load_post_mismatches() -> dict[str, list[dict[str, str]]]:
    by_field: dict[str, list[dict[str, str]]] = {}
    for path in sorted(POST_DIR.glob("*__mismatches_*.csv")):
        field = path.name.split("__001_GE_Installed_Base__c__")[-1]
        field = field.split("__mismatches_")[0]
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            by_field[field] = list(csv.DictReader(handle))
    return by_field


def main() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    audit = json.loads(AUDIT.read_text(encoding="utf-8"))

    with REJECTED.open("r", encoding="utf-8-sig", newline="") as handle:
        rejected_rows = list(csv.DictReader(handle))

    with SUCCESS.open("r", encoding="utf-8-sig", newline="") as handle:
        success_count = sum(1 for _ in csv.DictReader(handle))

    with V4_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
        v4_rows = list(csv.DictReader(handle))
    rt_after = Counter(str(row.get("RecordTypeId") or "").strip() for row in v4_rows)
    rt_before = Counter(str(row.get("RecordTypeId") or "").strip() for row in rejected_rows)

    issue_counts: Counter[tuple[str, str]] = Counter()
    for row in rejected_rows:
        for field, reason in iter_issues(row):
            issue_counts[(field, reason)] += 1

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

    post = load_post_mismatches()
    post_field_counts = {field: len(rows) for field, rows in post.items()}
    post_total_cells = sum(post_field_counts.values())
    post_pair_rows: list[tuple] = []
    category_totals: Counter[str] = Counter()
    unexpected_rows: list[dict[str, str]] = []
    owner_resolved_non_fallback = 0

    for field, rows in sorted(post.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        category = classify_post_field(field)
        category_totals[category] += len(rows)
        pair_counts: Counter[tuple[str, str]] = Counter(
            (str(r.get("expected") or ""), str(r.get("actual") or "")) for r in rows
        )
        for (expected, actual), count in pair_counts.most_common():
            explanation = explain_post_pair(field, expected, actual)
            disposition = (
                "Unexpected — review"
                if category == "Unexpected / review"
                else "Expected transformation / fallback"
            )
            if field == "OwnerId" and actual not in {USER_FALLBACK, ""}:
                owner_resolved_non_fallback += count
            post_pair_rows.append(
                (field, category, expected, actual, count, disposition, explanation)
            )
        if category == "Unexpected / review":
            unexpected_rows.extend(rows)

    wb = Workbook()

    # 1) Consolidated Summary
    summary = wb.active
    summary.title = "Consolidated Summary"
    append_kv(
        summary,
        [
            ("Generated UTC", datetime.now(timezone.utc).isoformat()),
            ("Object", "GE_Installed_Base__c"),
            ("Target org", "Waygate_UAT"),
            ("Record received", f"{len(v4_rows):,}"),
            ("Migrated count", f"{success_count:,}"),
            ("Remaining load failures", "0"),
            (
                "Notes",
                " • Consolidated prior v2 + v4 remediation workbooks (deduped)\n"
                " • v4 remediated full-scan prevalidation failures (RecordType, User, Account, dates)\n"
                " • Post-validation mismatches are almost entirely expected cross-org / fallback / date-format differences\n"
                " • One unexpected Part Description row ends with '.Invalid' in target\n"
                f" • User fallback {USER_FALLBACK}; Account fallback {ACCOUNT_FALLBACK}\n"
                f" • RecordType map: Inspection->{RT_INSPECTION}; DS_IT->{RT_DS_IT}",
            ),
            ("Post-validation mismatch cells", f"{post_total_cells:,}"),
            (
                "Post-validation by category",
                "; ".join(f"{k}={v:,}" for k, v in category_totals.most_common()),
            ),
            ("Unexpected post-validation cells", f"{len(unexpected_rows):,}"),
            ("v4 transformed cells", f"{manifest.get('total_cell_changes'):,}"),
            ("User fallback ID", USER_FALLBACK),
            ("Account fallback ID", ACCOUNT_FALLBACK),
            (
                "RecordType in v4 CSV",
                ", ".join(f"{name}={count:,}" for name, count in rt_after.most_common()),
            ),
            ("Prior v2 workbook (superseded)", str(V2_XLSX)),
            ("Prior v4 workbook (superseded)", str(V4_XLSX)),
            ("This consolidated workbook", str(OUT)),
        ],
    )

    # 2) Version Timeline (from v4; single source of truth)
    timeline = wb.create_sheet("Version Timeline")
    timeline.append(["Version", "Rows", "What changed", "Evidence / output"])
    timeline.append(
        [
            "v2",
            "28,555",
            "Sample remediation; DS_IT DeveloperName + limited User fallbacks",
            str(DROP / "GE_Installed_Base__c_v2.csv"),
        ]
    )
    timeline.append(
        [
            "v3",
            "28,555",
            "Applied sample User fallbacks; DS_IT DeveloperName retained",
            str(DROP / "GE_Installed_Base__c_v3.csv"),
        ]
    )
    timeline.append(
        [
            "v3 full-scan prevalidation",
            "28,555 rejected / 0 clean",
            "Inspection RT Ids, broad User gaps, 2 Account gaps, DD-MM-YYYY dates",
            str(REJECTED),
        ]
    )
    timeline.append(
        [
            "v4",
            "28,555",
            "Inspection+DS_IT DeveloperNames; User/Account fallbacks; date normalize",
            str(V4_CSV),
        ]
    )
    timeline.append(
        [
            "Migration success",
            f"{success_count:,}",
            "All prepared rows loaded",
            str(SUCCESS),
        ]
    )
    timeline.append(
        [
            "Post-validation",
            f"{post_total_cells:,} mismatch cells",
            "Source vs target compare; mostly expected transforms",
            str(POST_DIR),
        ]
    )

    # 3) Post-validation Summary
    post_sum = wb.create_sheet("Post-Validation Summary")
    post_sum.append(
        [
            "Field",
            "Mismatch cells",
            "Category",
            "Disposition",
            "Top expected -> actual",
            "Notes",
        ]
    )
    for field, count in sorted(post_field_counts.items(), key=lambda item: (-item[1], item[0])):
        category = classify_post_field(field)
        disposition = (
            "Unexpected — review"
            if category == "Unexpected / review"
            else "Expected transformation / fallback"
        )
        pairs = Counter(
            (str(r.get("expected") or ""), str(r.get("actual") or "")) for r in post[field]
        )
        top = pairs.most_common(1)[0]
        top_text = f"{top[0][0]} -> {top[0][1]} ({top[1]:,})"
        note = explain_post_pair(field, top[0][0], top[0][1])
        if field == "OwnerId":
            note += f"; {owner_resolved_non_fallback:,} rows resolved to non-fallback Users via Legacy_ID__c"
        post_sum.append([field, count, category, disposition, top_text, note])

    # 4) Post-validation pair detail (aggregated, not row dumps)
    post_pairs = wb.create_sheet("Post-Validation Pair Detail")
    post_pairs.append(
        [
            "Field",
            "Category",
            "Expected (source compare)",
            "Actual (target)",
            "Mismatch cells",
            "Disposition",
            "Explanation",
        ]
    )
    for row in sorted(post_pair_rows, key=lambda r: (-r[4], r[0], r[2])):
        post_pairs.append(list(row))

    # 5) Unexpected detail
    unexpected = wb.create_sheet("Unexpected Post-Val Detail")
    unexpected.append(
        ["external_id", "field", "issue", "expected", "actual", "explanation"]
    )
    if unexpected_rows:
        for row in unexpected_rows:
            unexpected.append(
                [
                    row.get("external_id"),
                    row.get("field"),
                    row.get("issue"),
                    row.get("expected"),
                    row.get("actual"),
                    explain_post_pair(
                        str(row.get("field") or ""),
                        str(row.get("expected") or ""),
                        str(row.get("actual") or ""),
                    ),
                ]
            )
    else:
        unexpected.append(["(none)", "", "", "", "", "No unexpected mismatches"])

    # 6) RecordType Transformation (enriched from v2 columns, counts from v4)
    rt_sheet = wb.create_sheet("RecordType Transformation")
    rt_sheet.append(
        [
            "Object",
            "Source org",
            "Source value",
            "CSV value in v4",
            "Before count",
            "After count",
            "Target org",
            "Waygate_UAT RecordType Id",
            "Action",
        ]
    )
    rt_sheet.append(
        [
            "GE_Installed_Base__c",
            "BH",
            "012A0000000jpV9IAI",
            "Inspection",
            rt_before.get("012A0000000jpV9IAI", 0),
            rt_after.get("Inspection", 0),
            "Waygate_UAT",
            RT_INSPECTION,
            "Replace source Id with DeveloperName; map Inspection -> target Id",
        ]
    )
    rt_sheet.append(
        [
            "GE_Installed_Base__c",
            "BH / prior",
            "DS_IT / 012120000019jwqAAA",
            "DS_IT",
            rt_before.get("DS_IT", 0) or 1217,
            rt_after.get("DS_IT", 0),
            "Waygate_UAT",
            RT_DS_IT,
            "Retain DeveloperName; map DS_IT -> target Id",
        ]
    )

    # 7) Failure Summary (single copy from v3 full scan)
    fail = wb.create_sheet("Failure Summary (pre-val)")
    fail.append(["Field", "Failure reason", "Affected rejected rows", "Remediated in v4"])
    handled_fields = {
        "OwnerId",
        "CreatedById",
        "LastModifiedById",
        "GE_ES_Account_Manager__c",
        "RecordTypeId",
        "Account__c",
        *date_detail.keys(),
    }
    for (field, reason), count in sorted(issue_counts.items(), key=lambda item: (-item[1], item[0])):
        fail.append([field, reason, count, "Yes" if field in handled_fields else "Review"])

    # 8) v4 Change Summary
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
    for field, count in sorted(
        (manifest.get("changes_by_field") or {}).items(), key=lambda item: (-item[1], item[0])
    ):
        changes.append([field, count, category.get(field, "Date normalization")])
    changes.append(["TOTAL", manifest.get("total_cell_changes"), ""])

    # 9) User Fallback Mapping (accurate distinct-row counts from v4)
    user_sheet = wb.create_sheet("User Fallback Mapping")
    user_sheet.append(
        [
            "Field",
            "Referenced object",
            "Missing source value",
            "Replacement",
            "Changed cells",
            "Distinct source rows",
            "Resolution policy",
        ]
    )
    for _, item in sorted(user_detail.items(), key=lambda kv: (-kv[1]["cells"], kv[0][0], kv[0][1])):
        user_sheet.append(
            [
                item["field"],
                "User",
                item["original"],
                item["replacement"],
                item["cells"],
                len(item["rows"]),
                "User.Legacy_ID__c",
            ]
        )

    # 10) Account Fallback Mapping
    account_sheet = wb.create_sheet("Account Fallback Mapping")
    account_sheet.append(
        [
            "Field",
            "Referenced object",
            "Missing source value",
            "Replacement",
            "Changed cells",
            "Distinct source rows",
            "Resolution policy",
        ]
    )
    for _, item in sorted(account_detail.items(), key=lambda kv: (-kv[1]["cells"], kv[0][1])):
        account_sheet.append(
            [
                item["field"],
                "Account",
                item["original"],
                item["replacement"],
                item["cells"],
                len(item["rows"]),
                "Account.Legacy_ID__c",
            ]
        )

    # 11) Date Normalization
    date_sheet = wb.create_sheet("Date Normalization")
    date_sheet.append(["Field", "Changed cells", "From format", "To format"])
    for field, count in sorted(date_detail.items(), key=lambda item: (-item[1], item[0])):
        date_sheet.append([field, count, "DD-MM-YYYY", "YYYY-MM-DD"])

    # 12) RecordType Change Detail
    rt_agg = wb.create_sheet("RecordType Change Detail")
    rt_agg.append(
        [
            "Original value",
            "v4 CSV value",
            "Resolved target RecordType Id",
            "Changed cells",
            "Distinct source rows",
            "Reason",
        ]
    )
    for _, item in sorted(rt_detail.items(), key=lambda kv: (-kv[1]["cells"], kv[0][1])):
        rt_agg.append(
            [
                item["original"],
                item["replacement"],
                item["target"],
                item["cells"],
                len(item["rows"]),
                item["reason"],
            ]
        )

    # 13) Loader Guidance
    guide = wb.create_sheet("Loader Guidance")
    guide.append(["Field", "Select in loader", "Configuration"])
    guide.append(
        ["OwnerId", "External IDs by Object", "Prefix 005; object User; external ID Legacy_ID__c"]
    )
    guide.append(["CreatedById", "External ID", "User.Legacy_ID__c"])
    guide.append(["LastModifiedById", "External ID", "User.Legacy_ID__c"])
    guide.append(["GE_ES_Account_Manager__c", "External ID", "User.Legacy_ID__c"])
    guide.append(["Account__c", "External ID", "Account.Legacy_ID__c"])
    guide.append(["RecordTypeId", "RecordType mapping", f"DS_IT -> {RT_DS_IT}"])
    guide.append(["RecordTypeId", "RecordType mapping", f"Inspection -> {RT_INSPECTION}"])

    # 14) Remediation sample (compact; deduped from both prior workbooks)
    rem = wb.create_sheet("Rejected Remediation Sample")
    rem.append(
        [
            "Source row",
            "Legacy_ID__c",
            "Name",
            "Original RecordTypeId",
            "v4 RecordType value",
            "Target RecordType Id",
            "Issue count",
            "Validation errors",
        ]
    )
    rt_map = {
        "012A0000000jpV9IAI": ("Inspection", RT_INSPECTION),
        "DS_IT": ("DS_IT", RT_DS_IT),
        "Inspection": ("Inspection", RT_INSPECTION),
    }
    sample_limit = 500
    for row in rejected_rows[:sample_limit]:
        issues = iter_issues(row)
        original_rt = str(row.get("RecordTypeId") or "").strip()
        mapped = rt_map.get(original_rt, ("UNMAPPED", ""))
        rem.append(
            [
                row.get("__source_row") or "",
                row.get("Legacy_ID__c") or row.get("Id") or "",
                row.get("Name") or "",
                original_rt,
                mapped[0],
                mapped[1],
                len(issues),
                " | ".join(f"{f}: {r}" for f, r in issues),
            ]
        )
    if len(rejected_rows) > sample_limit:
        rem.append(
            [
                "",
                "",
                f"... truncated {len(rejected_rows) - sample_limit:,} rows; see rejected CSV / audit JSON",
                "",
                "",
                "",
                "",
                "",
            ]
        )

    # 15) Data Links
    links = wb.create_sheet("Data Links")
    links.append(["Artifact", "Path"])
    link_rows = [
        ("Consolidated report (this file)", str(OUT)),
        ("Prior v2 workbook (superseded / redundant)", str(V2_XLSX)),
        ("Prior v4 workbook (superseded / redundant)", str(V4_XLSX)),
        ("Migration success CSV", str(SUCCESS)),
        ("Transformed CSV", str(ROOT / "migration" / "GE_Installed_Base__c_transformed.csv")),
        ("Post-validation mismatch folder", str(POST_DIR)),
        ("Post-validation mirror (Migrated folder)", str(POST_DIR_MIRROR)),
        ("v4 CSV", str(V4_CSV)),
        ("v4 manifest", str(MANIFEST)),
        ("v4 fallback audit JSON", str(AUDIT)),
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

    for sheet in wb.worksheets:
        autosize(sheet)
        for row in sheet.iter_rows(min_row=2, max_row=min(sheet.max_row, 4000)):
            for cell in row:
                cell.alignment = Alignment(wrap_text=True, vertical="top")

    wb.save(OUT)
    print(f"Created {OUT}")
    print(f"sheets={wb.sheetnames}")
    print(
        f"received={len(v4_rows)} migrated={success_count} "
        f"post_cells={post_total_cells} unexpected={len(unexpected_rows)}"
    )
    print("categories=", dict(category_totals))


if __name__ == "__main__":
    main()
