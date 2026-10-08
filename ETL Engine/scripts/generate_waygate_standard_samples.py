"""Generate dependency-complete Waygate samples for core Salesforce objects.

The generator reads writable field metadata from the connected target org,
selects at least 50 percent of writable/non-calculated fields, and only selects
lookup fields whose referenced records are either generated in this package or
are the target org's existing Business Hours records.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from core.connector import connector_from_env


OBJECTS = (
    "Account",
    "Campaign",
    "Contact",
    "Product2",
    "Lead",
    "Contract",
    "Opportunity",
    "Case",
)

LOAD_ORDER = (
    "Account",
    "Campaign",
    "Product2",
    "Contact",
    "Contract",
    "Opportunity",
    "Lead",
    "Case",
)

STANDARD_REQUIRED = {
    "Account": ("Name",),
    "Campaign": ("Name",),
    "Contact": ("LastName", "AccountId"),
    "Product2": ("Name",),
    "Lead": ("LastName", "Company"),
    "Contract": (
        "AccountId",
        "CustomerSignedId",
        "StartDate",
        "ContractTerm",
        "Status",
    ),
    "Opportunity": (
        "Name",
        "StageName",
        "CloseDate",
        "AccountId",
        "CampaignId",
        "ContactId",
        "ContractId",
    ),
    "Case": (
        "Subject",
        "Status",
        "Origin",
        "BusinessHoursId",
        "AccountId",
        "ContactId",
        "ProductId",
        "Opportunity_Name__c",
    ),
}

WAYGATE_NAMES = {
    "Account": "Waygate Inspection Customer",
    "Campaign": "Waygate NDT Solutions Campaign",
    "Contact": "Waygate Service Contact",
    "Product2": "Waygate Inspection Product",
    "Lead": "Waygate NDT Prospect",
    "Contract": "Waygate Service Contract",
    "Opportunity": "Waygate Inspection Opportunity",
    "Case": "Waygate Inspection Support Case",
}

REFERENCE_PRIORITY = {
    "AccountId": "Account",
    "CampaignId": "Campaign",
    "ContactId": "Contact",
    "ContractId": "Contract",
    "ProductId": "Product2",
    "ParentId": None,
    "ReportsToId": "Contact",
    "CustomerSignedId": "Contact",
    "BusinessHoursId": "BusinessHours",
}

SKIP_REFERENCE_OBJECTS = {
    "User",
    "Group",
    "RecordType",
    "Pricebook2",
    "Asset",
    "Entitlement",
    "ServiceContract",
}

EXCLUDED_SAMPLE_FIELDS = {
    "Lead": {
        "IsConverted",
    },
    "Opportunity": {
        "AccountId",
        "ContactId",
        "Date_Completed_ITO_OTR_GE_OG__c",
        "EXPECTED_RFQ_RFP_RECEIVED_DATE__c",
        "ITC_HRCWF_Submission_Date__c",
        "Original_Bid_Due_Date__c",
        "SAP_CPQ_Job_End_Date__c",
    },
}

SYSTEM_MANAGED_FIELDS = {
    "Id",
    "IsDeleted",
    "CreatedDate",
    "CreatedById",
    "LastModifiedDate",
    "LastModifiedById",
    "SystemModstamp",
    "LastActivityDate",
    "LastViewedDate",
    "LastReferencedDate",
    "MasterRecordId",
}


def stable_source_id(object_name: str, row_number: int) -> str:
    """Return a deterministic synthetic 18-character Salesforce-shaped ID."""
    digest = hashlib.sha1(object_name.encode("utf-8")).hexdigest().upper()
    return ("a" + digest[:12] + f"{row_number:05d}")[:18]


def active_picklist_values(field: dict) -> list[str]:
    return [
        str(item["value"])
        for item in field.get("picklistValues") or []
        if item.get("active") and item.get("value") is not None
    ]


def is_writable(field: dict, *, ignore_fls: bool = False) -> bool:
    permission_allows = bool(field.get("createable")) or (
        ignore_fls
        and field.get("name") not in SYSTEM_MANAGED_FIELDS
        and not str(field.get("name") or "").startswith("NumberOf")
    )
    return permission_allows and not (
        field.get("calculated")
        or field.get("autoNumber")
        or field.get("type") in {"address", "location"}
    )


def reference_target(object_name: str, field: dict) -> str | None:
    references = [
        str(item)
        for item in field.get("referenceTo") or []
        if item and item not in SKIP_REFERENCE_OBJECTS
    ]
    preferred = REFERENCE_PRIORITY.get(str(field.get("name") or ""))
    if preferred is None and field.get("name") == "ParentId":
        preferred = object_name
    if preferred and preferred in references:
        return preferred
    allowed = [item for item in references if item in OBJECTS or item == "BusinessHours"]
    return allowed[0] if len(allowed) == 1 else None


def select_fields(
    object_name: str,
    fields: list[dict],
    *,
    ignore_fls: bool = False,
) -> tuple[list[dict], int, bool]:
    excluded = EXCLUDED_SAMPLE_FIELDS.get(object_name, set())
    writable_all = [
        field for field in fields if is_writable(field, ignore_fls=ignore_fls)
    ]
    writable = [field for field in writable_all if field.get("name") not in excluded]
    target = math.ceil(len(writable_all) * 0.50)
    by_name = {field["name"]: field for field in writable}
    explicitly_required = [
        by_name[name]
        for name in STANDARD_REQUIRED.get(object_name, ())
        if name in by_name
    ]
    metadata_required = [
        field
        for field in writable
        if not ignore_fls
        and not field.get("nillable", True)
        and not field.get("defaultedOnCreate", False)
    ]
    required = []
    seen_required = set()
    for field in [*explicitly_required, *metadata_required]:
        if field["name"] not in seen_required:
            required.append(field)
            seen_required.add(field["name"])
    unsupported_required_references = [
        field["name"]
        for field in required
        if field.get("type") == "reference"
        and not reference_target(object_name, field)
    ]
    if unsupported_required_references:
        raise ValueError(
            f"{object_name}: required reference field(s) have no generated parent: "
            + ", ".join(unsupported_required_references)
        )
    required_names = {field["name"] for field in required}
    legacy_mapped_from_id = "Legacy_ID__c" in by_name
    scalar = [
        field
        for field in writable
        if field["name"] not in required_names
        and field["name"] != "Legacy_ID__c"
        and field.get("type") != "reference"
    ]
    safe_references = [
        field
        for field in writable
        if field["name"] not in required_names
        and field["name"] != "Legacy_ID__c"
        and field.get("type") == "reference"
        and reference_target(object_name, field)
    ]
    selected = required + scalar + safe_references
    csv_target = max(0, target - int(legacy_mapped_from_id))
    if len(selected) < csv_target:
        raise ValueError(
            f"{object_name}: only {len(selected)} fields can be safely populated; "
            f"{csv_target} are required for 50% coverage"
        )
    return (
        selected[: max(csv_target, len(required))],
        len(writable_all),
        legacy_mapped_from_id,
    )


def waygate_text(object_name: str, field_name: str, row_index: int) -> str:
    ordinal = row_index + 1
    lower = field_name.casefold()
    base = WAYGATE_NAMES.get(
        object_name,
        f"Waygate {re.sub(r'__c$', '', object_name).replace('_', ' ')}",
    )
    if field_name in {"Name", "Subject"}:
        return f"{base} {ordinal:04d}"
    if field_name == "LastName":
        return f"Inspector {ordinal:04d}"
    if field_name == "FirstName":
        return "Waygate"
    if field_name == "Company":
        return f"Waygate Prospect Company {ordinal:04d}"
    if "serial" in lower:
        return f"WG-NDT-{ordinal:06d}"
    if "description" in lower or "comment" in lower or "note" in lower:
        return (
            f"{base} {ordinal:04d}: ultrasonic, remote visual inspection, "
            "eddy-current, and nondestructive-testing sample data."
        )
    if "street" in lower:
        return f"{100 + ordinal} Inspection Drive"
    if "city" in lower:
        return ("Dubai", "Rotterdam", "Houston")[row_index % 3]
    if "state" in lower or "province" in lower:
        return ("Dubai", "South Holland", "Texas")[row_index % 3]
    if "postal" in lower or "zip" in lower:
        return f"{10000 + ordinal:05d}"
    if "country" in lower:
        return ("United Arab Emirates", "Netherlands", "United States")[row_index % 3]
    if "email" in lower:
        return f"inspection{ordinal:04d}@waygate.example"
    if "phone" in lower or "mobile" in lower or "fax" in lower:
        return f"+1555{ordinal:07d}"
    if "url" in lower or "website" in lower:
        return "https://www.waygate-tech.com"
    if "currency" in lower:
        return "USD"
    if "language" in lower:
        return "English"
    if "external" in lower or "legacy" in lower:
        return f"WG-{object_name.upper()}-{ordinal:06d}"
    clean_name = re.sub(r"__c$", "", field_name).replace("_", " ")
    return f"Waygate {clean_name} {ordinal:04d}"


def scalar_value(object_name: str, field: dict, row_index: int) -> str:
    field_name = str(field["name"])
    field_type = str(field.get("type") or "string").casefold()
    ordinal = row_index + 1
    if object_name == "Contract" and field_name == "Status":
        return "Pending"
    if field_name.casefold() == "latitude":
        return f"{25.000000 + row_index * 0.001:.6f}"
    if field_name.casefold() == "longitude":
        return f"{55.000000 + row_index * 0.001:.6f}"
    if field_type in {"picklist", "multipicklist"}:
        values = active_picklist_values(field)
        if not values:
            return ""
        value = values[row_index % len(values)]
        return value if field_type == "picklist" else value
    if field_type == "boolean":
        return "true" if row_index % 2 == 0 else "false"
    if field_type == "date":
        return (date(2026, 8, 1) + timedelta(days=row_index % 300)).isoformat()
    if field_type == "datetime":
        value = datetime(2026, 8, 1, 8, 30, tzinfo=timezone.utc) + timedelta(days=row_index)
        return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if field_type == "time":
        return "08:30:00.000Z"
    if field_type in {"currency", "double", "percent"}:
        if field_name == "Probability":
            return str(row_index % 101)
        precision = int(field.get("precision") or 18)
        scale = int(field.get("scale") or 0)
        integral_digits = max(1, precision - scale)
        maximum = int("9" * min(integral_digits, 6))
        number = min(100 + ordinal, maximum)
        return f"{number:.{scale}f}" if scale else str(number)
    if field_type in {"int", "integer", "long"}:
        return str(ordinal)
    if field_type == "email":
        return f"inspection{ordinal:04d}@waygate.example"
    if field_type == "phone":
        return f"+1555{ordinal:07d}"
    if field_type == "url":
        return "https://www.waygate-tech.com"
    value = waygate_text(object_name, field_name, row_index)
    length = int(field.get("length") or 255)
    return value[:length]


def reference_value(
    object_name: str,
    field: dict,
    row_index: int,
    source_ids: dict[str, list[str]],
    business_hours_id: str,
) -> str:
    target = reference_target(object_name, field)
    if target == "BusinessHours":
        return business_hours_id
    if not target:
        return ""
    target_ids = source_ids[target]
    if target == object_name:
        # First record anchors the self-reference chain.
        return "" if row_index == 0 else target_ids[row_index - 1]
    return target_ids[row_index % len(target_ids)]


def write_csv(path: Path, headers: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--records-per-object", type=int, default=150)
    parser.add_argument(
        "--hierarchy-workbook",
        type=Path,
        default=Path("temp/hierarchy_edit/waygate_object_hierarchy.xlsx"),
    )
    args = parser.parse_args()
    if args.records_per_object < 2:
        raise ValueError("--records-per-object must be at least 2")

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    upload_dir = output / "upload_files"
    upload_dir.mkdir(parents=True, exist_ok=True)
    target = connector_from_env("TARGET", args.profile).connect()
    business_hours = target.query(
        "SELECT Id, Name, IsActive, IsDefault FROM BusinessHours "
        "WHERE IsActive = true ORDER BY IsDefault DESC, Name LIMIT 1"
    )
    if not business_hours:
        raise RuntimeError("The target org has no active Business Hours record")
    business_hours_id = str(business_hours[0]["Id"])

    record_counts = {
        object_name: args.records_per_object for object_name in OBJECTS
    }
    source_ids = {
        object_name: [
            stable_source_id(object_name, row_index)
            for row_index in range(record_counts[object_name])
        ]
        for object_name in OBJECTS
    }
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "hierarchy_source": str(args.hierarchy_workbook.resolve()),
        "target_org_business_hours": business_hours[0],
        "load_order": list(LOAD_ORDER),
        "total_records": sum(record_counts.values()),
        "objects": [],
    }
    lookup_checks: list[dict] = []

    for object_name in OBJECTS:
        describe = target.describe_object(object_name)
        permission_warning = ""
        ignore_fls = not bool(describe.get("createable"))
        if ignore_fls:
            permission_warning = (
                f"The connected target profile cannot create {object_name}. "
                "Coverage uses schema candidates without profile FLS; grant object "
                "and field create access before loading."
            )
        selected, writable_count, legacy_mapped_from_id = select_fields(
            object_name,
            describe.get("fields") or [],
            ignore_fls=ignore_fls,
        )
        headers = ["Id", *[field["name"] for field in selected]]
        rows: list[dict[str, str]] = []
        for row_index in range(record_counts[object_name]):
            row = {"Id": source_ids[object_name][row_index]}
            for field in selected:
                if field.get("type") == "reference":
                    row[field["name"]] = reference_value(
                        object_name,
                        field,
                        row_index,
                        source_ids,
                        business_hours_id,
                    )
                else:
                    row[field["name"]] = scalar_value(object_name, field, row_index)
            rows.append(row)

        csv_name = f"{object_name}.csv"
        relative_csv = Path("upload_files") / csv_name
        write_csv(output / relative_csv, headers, rows)
        populated_fields = [
            field["name"]
            for field in selected
            if any(str(row.get(field["name"]) or "").strip() for row in rows)
        ]
        lookup_fields = []
        for field in selected:
            if field.get("type") != "reference":
                continue
            target_object = reference_target(object_name, field)
            populated = sum(bool(row.get(field["name"])) for row in rows)
            check = {
                "object": object_name,
                "field": field["name"],
                "referenced_object": target_object,
                "populated_rows": populated,
                "parent_source": (
                    "target BusinessHours"
                    if target_object == "BusinessHours"
                    else f"{target_object}.csv"
                ),
            }
            lookup_checks.append(check)
            lookup_fields.append(check)

        coverage_fields = len(populated_fields) + int(legacy_mapped_from_id)
        coverage = coverage_fields / writable_count if writable_count else 0.0
        if coverage < 0.50:
            raise RuntimeError(
                f"{object_name}: populated coverage {coverage:.1%} is below 50%"
            )
        manifest["objects"].append(
            {
                "object": object_name,
                "csv": relative_csv.as_posix(),
                "records": len(rows),
                "writable_non_calculated_fields": writable_count,
                "selected_fields": len(selected) + int(legacy_mapped_from_id),
                "populated_fields": coverage_fields,
                "coverage_percent": round(coverage * 100, 1),
                "profile_createable": bool(describe.get("createable")),
                "permission_warning": permission_warning,
                "lookup_fields": lookup_fields,
            }
        )

    with (output / "coverage.csv").open("w", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "object",
            "records",
            "writable_non_calculated_fields",
            "selected_fields",
            "populated_fields",
            "coverage_percent",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in manifest["objects"]:
            writer.writerow({name: item[name] for name in fieldnames})

    with (output / "lookup_integrity.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "object",
                "field",
                "referenced_object",
                "populated_rows",
                "parent_source",
            ],
        )
        writer.writeheader()
        writer.writerows(lookup_checks)

    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    readme = [
        "# Waygate core-object sample package",
        "",
        "This package contains synthetic Waygate inspection and NDT records for",
        "Account, Campaign, Contact, Product2, Lead, Contract, Opportunity, and Case.",
        "",
        f"Total generated records: **{manifest['total_records']:,}**.",
        "",
        "Each CSV contains at least 50% of the object's writable, non-calculated",
        "target fields. Every populated lookup references another generated CSV record,",
        "except `BusinessHoursId`, which references the active target-org Business Hours",
        "record documented in `manifest.json`.",
        "",
        "Load order:",
        "",
        *[
            f"{index}. `upload_files/{name}.csv`"
            for index, name in enumerate(LOAD_ORDER, start=1)
        ],
        "",
        "Upload only the eight CSV files inside `upload_files/`. Do not upload",
        "`coverage.csv` or `lookup_integrity.csv`; they are audit reports, not",
        "Salesforce object data.",
        "",
        "Use `coverage.csv` for field coverage and `lookup_integrity.csv` for the",
        "lookup-to-parent evidence.",
    ]
    permission_warnings = [
        item["permission_warning"]
        for item in manifest["objects"]
        if item["permission_warning"]
    ]
    if permission_warnings:
        readme.extend(
            [
                "",
                "## Target permission prerequisites",
                "",
                *[f"- {warning}" for warning in permission_warnings],
            ]
        )
    (output / "README.md").write_text("\n".join(readme) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
