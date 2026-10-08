"""Generate a coherent Waygate sample flat-file package from Salesforce metadata."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path


NS = {"m": "http://soap.sforce.com/2006/04/metadata"}

OBJECTS = [
    "Account",
    "GE_OG_Product_Hierarchy1__c",
    "GE_OG_Defect_Code_Hierarchy__c",
    "SVMXC__Site__c",
    "SVMXC__Skill__c",
    "SVMXC__Service_Group__c",
    "SVMXC__Service_Group_Members__c",
    "SVMXC__Service_Group_Skills__c",
    "SVMXC__Dispatcher_Access__c",
    "SVMXC__Installed_Product__c",
    "BH_MCS_MCIB__c",
    "BH_MCS_MCServiceAgreement__c",
    "BH_MCS_AgreementIBLink__c",
    "BH_MCS_MCSAPServicesTransactions__c",
    "BH_MCS_SMAXDSCUSTOMERASSETS__c",
    "SVMXC__Service_Order__c",
    "SVMXC__Service_Order_Line__c",
    "BH_MCS_WOIBLink__c",
    "BH_MCS_WOAssetLink__c",
    "GE_AL_Service_Checklist__c",
    "GE_OG_GS_Passport__c",
    "GE_OG_GS_Visa__c",
    "GE_OG_DS_Gate_Pass__c",
    "DS_SOAR_Prospects__c",
    "SMax_Campaign__c",
    "SMax_Lead__c",
    "Case",
    "Task",
    "SVMXC__SVMX_Event__c",
]

LABELS = {
    "Account": ["Waygate Technologies Gulf Services", "Waygate Inspection Solutions Europe"],
    "Case": ["UT probe calibration support", "Remote visual inspection image issue"],
    "Task": ["Confirm inspection window", "Review borescope service report"],
    "SVMXC__Installed_Product__c": ["Mentor Visual iQ System", "Krautkrämer USM 100"],
    "SVMXC__Service_Order__c": ["Dubai refinery annual inspection", "Rotterdam turbine outage support"],
    "SVMXC__Service_Order_Line__c": ["Calibrate ultrasonic detector", "Inspect turbine combustion chamber"],
    "SVMXC__Site__c": ["Jebel Ali Inspection Site", "Rotterdam Service Facility"],
    "SVMXC__Skill__c": ["Ultrasonic Testing Level II", "Remote Visual Inspection"],
}

STANDARD_REQUIRED = {
    "Account": ["Name"],
    "Case": ["Subject", "Status", "Origin"],
    "Task": ["Subject", "Status", "Priority"],
}

REFERENCE_PREFERENCES = {
    "AccountId": "Account",
    "ParentId": "Account",
    "WhatId": "Account",
    "SVMXC__Service_Order__c": "SVMXC__Service_Order__c",
    "SVMXC__Installed_Product__c": "SVMXC__Installed_Product__c",
    "SVMXC__Site__c": "SVMXC__Site__c",
    "SVMXC__Service_Group__c": "SVMXC__Service_Group__c",
    "SVMXC__Skill__c": "SVMXC__Skill__c",
}

STANDARD_FIELD_KINDS = {
    "AnnualRevenue": "Currency",
    "AccountId": "Lookup",
    "AssetId": "Lookup",
    "BusinessHoursId": "Lookup",
    "ContactId": "Lookup",
    "EntitlementId": "Lookup",
}


@dataclass
class Field:
    name: str
    kind: str
    formula: bool
    calculated: bool
    auto_number: bool
    references: list[str]
    picklist: list[str]
    length: int
    precision: int
    scale: int
    controller_name: str


def text(node: ET.Element, path: str, default: str = "") -> str:
    child = node.find(path, NS)
    return (child.text or "").strip() if child is not None else default


def truth(node: ET.Element, path: str) -> bool:
    return text(node, path).lower() == "true"


def stable_source_id(object_name: str, row: int) -> str:
    digest = hashlib.sha1(object_name.encode("utf-8")).hexdigest().upper()
    return ("a" + digest[:12] + f"{row:05d}")[:18]


def read_global_values(metadata_root: Path) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    folder = metadata_root.parent / "globalValueSets"
    if not folder.exists():
        return result
    for path in folder.glob("*.globalValueSet-meta.xml"):
        root = ET.parse(path).getroot()
        values = []
        for value in root.findall("m:customValue", NS):
            name = text(value, "m:fullName")
            active_text = text(value, "m:isActive")
            if name and active_text.lower() != "false":
                values.append(name)
        result[path.name.removesuffix(".globalValueSet-meta.xml")] = values
    return result


def read_fields(object_dir: Path, global_values: dict[str, list[str]]) -> list[Field]:
    fields: list[Field] = []
    for path in sorted((object_dir / "fields").glob("*.field-meta.xml")):
        root = ET.parse(path).getroot()
        values = [
            text(v, "m:fullName")
            for v in root.findall("m:valueSet/m:valueSetDefinition/m:value", NS)
            if text(v, "m:fullName")
        ]
        global_name = text(root, "m:valueSet/m:valueSetName")
        if not values and global_name:
            values = global_values.get(global_name, [])
        fields.append(
            Field(
                name=text(root, "m:fullName"),
                kind=text(root, "m:type") or STANDARD_FIELD_KINDS.get(
                    text(root, "m:fullName"), "Text"
                ),
                formula=root.find("m:formula", NS) is not None,
                calculated=truth(root, "m:calculated"),
                auto_number=text(root, "m:type") == "AutoNumber",
                references=[r.text.strip() for r in root.findall("m:referenceTo", NS) if r.text],
                picklist=values,
                length=int(text(root, "m:length", "255") or 255),
                precision=int(text(root, "m:precision", "18") or 18),
                scale=int(text(root, "m:scale", "0") or 0),
                controller_name=text(root, "m:valueSet/m:controllingField"),
            )
        )
    return fields


def name_field(object_dir: Path) -> tuple[str, bool]:
    path = object_dir / f"{object_dir.name}.object-meta.xml"
    if not path.exists():
        return "Name", True
    root = ET.parse(path).getroot()
    node = root.find("m:nameField", NS)
    if node is None:
        return "Name", True
    return text(node, "m:type", "Text"), text(node, "m:type", "Text") != "AutoNumber"


def record_type_names(object_dir: Path) -> list[str]:
    return [
        path.name.removesuffix(".recordType-meta.xml")
        for path in sorted((object_dir / "recordTypes").glob("*.recordType-meta.xml"))
    ]


def choose_reference(field: Field) -> str | None:
    preferred = REFERENCE_PREFERENCES.get(field.name)
    # Standard lookup metadata retrieved from source control may omit
    # referenceTo. Retain explicit, known relationships in that case.
    if preferred in OBJECTS and (not field.references or preferred in field.references):
        return preferred
    return next((name for name in field.references if name in OBJECTS), None)


def business_text(object_name: str, field_name: str, row: int) -> str:
    label = LABELS.get(object_name, [f"Waygate {object_name}", f"Waygate {object_name}"])[row]
    lower = field_name.lower()
    if object_name == "Account" and field_name == "GE_HQ_DUNS_Number__c":
        return f"10000000{row + 1}"
    if object_name == "Account" and field_name == "GE_HQ_SFDC_LEGACY_ID__c":
        return stable_source_id(object_name, row)
    if field_name in {"Name", "Subject"}:
        return label
    if "serial" in lower:
        return f"WG-{2026 + row}-SN-{10001 + row}"
    if "description" in lower or "comment" in lower or "note" in lower:
        return f"{label} - sample Waygate inspection and service migration record"
    if "street" in lower:
        return ["Plot S201, Jebel Ali Free Zone", "Waalhaven 12"][row]
    if "city" in lower:
        return ["Dubai", "Rotterdam"][row]
    if "state" in lower or "province" in lower:
        return ["Dubai", "South Holland"][row]
    if "postal" in lower or "zip" in lower:
        return ["00000", "3088"][row]
    if "country" in lower:
        return ["United Arab Emirates", "Netherlands"][row]
    if "email" in lower:
        return ["service.dubai@waygate.example", "service.rotterdam@waygate.example"][row]
    if "phone" in lower or "mobile" in lower:
        return ["+97145550101", "+31105550102"][row]
    if "url" in lower or "website" in lower:
        return "https://www.waygate-tech.com"
    if "currency" in lower:
        return ["USD", "EUR"][row]
    if "language" in lower:
        return "English"
    return f"WG {re.sub(r'__c$', '', field_name).replace('_', ' ')} {row + 1}"[:120]


def value_for(
    field: Field,
    object_name: str,
    row: int,
    source_ids: dict[str, list[str]],
    record_type_values: dict[str, dict] | None = None,
    current_row: dict[str, str] | None = None,
) -> str:
    reference = choose_reference(field)
    if reference:
        return source_ids[reference][row]
    kind = field.kind.lower()
    if kind in {"lookup", "masterdetail", "reference"}:
        # Never manufacture lookup IDs. Unknown/out-of-package references are
        # intentionally blank so the generated row remains loadable.
        return ""
    if kind in {"picklist", "multiselectpicklist"}:
        config = (record_type_values or {}).get(field.name)
        allowed = config.get("values") if isinstance(config, dict) else config
        candidates = [
            value for value in field.picklist
            if allowed is None or value in allowed
        ]
        if isinstance(config, dict) and config.get("controller_values"):
            controller_value = str((current_row or {}).get(field.controller_name) or "")
            controller_index = config["controller_values"].get(controller_value)
            candidates = [
                value for value in candidates
                if controller_index is not None
                and controller_index in config.get("valid_for", {}).get(value, set())
            ]
        if allowed is not None and not candidates:
            candidates = sorted(allowed)
        return candidates[min(row, len(candidates) - 1)] if candidates else ""
    if kind in {"boolean", "checkbox"}:
        return "true" if row == 0 else "false"
    if kind == "date":
        return ["2026-08-15", "2026-09-20"][row]
    if kind == "datetime":
        return ["2026-08-15T08:30:00.000Z", "2026-09-20T10:00:00.000Z"][row]
    if kind == "time":
        return ["08:30:00.000Z", "10:00:00.000Z"][row]
    if kind in {"currency", "double", "number", "percent"}:
        if field.name == "AnnualRevenue":
            return ["1250000", "2475000"][row]
        integral_digits = max(1, field.precision - field.scale)
        maximum_integral = int("9" * min(integral_digits, 6))
        candidates = [12.5, 24.5] if kind == "percent" else [1250, 2475]
        value = min(candidates[row], maximum_integral)
        return f"{value:.{field.scale}f}" if field.scale else str(int(value))
    if kind in {"int", "integer", "long"}:
        return str(row + 1)
    if kind == "email":
        return ["service.dubai@waygate.example", "service.rotterdam@waygate.example"][row]
    if kind == "phone":
        return ["+97145550101", "+31105550102"][row]
    if kind == "url":
        return "https://www.waygate-tech.com"
    if kind in {"location"}:
        return ""
    return business_text(object_name, field.name, row)[: max(1, field.length)]


def selected_fields(fields: list[Field]) -> tuple[list[Field], int, int, bool]:
    writable = [f for f in fields if not f.formula and not f.calculated and not f.auto_number]
    # Include one field of headroom because a metadata picklist can reference a
    # standard value set whose values are not stored beside the object.
    target = min(len(writable), math.ceil(len(writable) * 0.50) + 1)
    # Source Id is deliberately mapped to Legacy_ID__c by the flat-file planner.
    # Do not also emit Legacy_ID__c, which would create the duplicate-column
    # condition this workflow is designed to prevent.
    candidates = [f for f in writable if f.name != "Legacy_ID__c"]
    legacy_mapped_from_id = any(f.name == "Legacy_ID__c" for f in writable)
    csv_target = max(0, target - int(legacy_mapped_from_id))
    primitives = [f for f in candidates if not f.references]
    internal_refs = [f for f in candidates if f.references and choose_reference(f)]
    other_refs = [f for f in candidates if f.references and not choose_reference(f)]
    selected = (internal_refs + primitives + other_refs)[:csv_target]
    if len(selected) < csv_target:
        raise ValueError(f"Only {len(selected)} writable fields are available for target {csv_target}")
    return selected, target, len(writable), legacy_mapped_from_id


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--record-type-ids", type=Path)
    parser.add_argument("--query-target-record-types", action="store_true")
    args = parser.parse_args()

    metadata_root = args.metadata_root.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    global_values = read_global_values(metadata_root)
    record_type_ids = {}
    target_record_type_picklists: dict[str, dict[str, dict[str, set[str]]]] = {}
    target_default_business_hours_id = ""
    if args.record_type_ids and args.record_type_ids.exists():
        record_type_ids = json.loads(args.record_type_ids.read_text(encoding="utf-8"))
    if args.query_target_record_types:
        from core.connector import connector_from_env

        target = connector_from_env("TARGET").connect()
        rows = target.query(
            "SELECT Id, SObjectType, DeveloperName FROM RecordType WHERE IsActive = true"
        )
        for record in rows:
            object_name = record.get("SobjectType") or record.get("SObjectType")
            if object_name in OBJECTS:
                record_type_ids.setdefault(object_name, {})[
                    record["DeveloperName"]
                ] = record["Id"]
        for object_name, by_developer_name in record_type_ids.items():
            target_record_type_picklists[object_name] = {
                record_type_id: target.describe_record_type_picklists(
                    object_name, record_type_id
                )
                for record_type_id in sorted(set(by_developer_name.values()))
            }
        business_hours = target.query(
            "SELECT Id FROM BusinessHours WHERE IsActive = true "
            "ORDER BY IsDefault DESC, Name LIMIT 1"
        )
        if business_hours:
            target_default_business_hours_id = str(
                business_hours[0].get("Id") or ""
            )

    source_ids = {name: [stable_source_id(name, 0), stable_source_id(name, 1)] for name in OBJECTS}
    report = []
    manifest = {"source": str(metadata_root), "objects": []}

    for object_name in OBJECTS:
        object_dir = metadata_root / object_name
        if not object_dir.exists():
            raise FileNotFoundError(object_dir)
        fields = read_fields(object_dir, global_values)
        selected, target, writable_count, legacy_mapped_from_id = selected_fields(fields)
        headers = ["Id"]
        name_type, has_writable_name = name_field(object_dir)
        if has_writable_name and "Name" not in {f.name for f in selected}:
            headers.append("Name")
        for standard in STANDARD_REQUIRED.get(object_name, []):
            if standard not in headers and standard not in {f.name for f in selected}:
                headers.append(standard)
        rt_names = record_type_names(object_dir)
        rt_ids = record_type_ids.get(object_name, {})
        available_rt_names = [name for name in rt_names if name in rt_ids]
        if available_rt_names:
            headers.append("RecordTypeId")
        headers.extend(f.name for f in selected if f.name not in headers)

        rows = []
        for row_index in range(2):
            row = {"Id": source_ids[object_name][row_index]}
            selected_record_type_id = ""
            if "RecordTypeId" in headers:
                rt_name = available_rt_names[row_index % len(available_rt_names)]
                selected_record_type_id = rt_ids[rt_name]
                row["RecordTypeId"] = selected_record_type_id
            record_type_values = (
                target_record_type_picklists.get(object_name, {}).get(
                    selected_record_type_id, {}
                )
            )
            if "Name" in headers:
                row["Name"] = LABELS.get(
                    object_name,
                    [f"Waygate {object_name} Dubai", f"Waygate {object_name} Rotterdam"],
                )[row_index]
            if "Subject" in headers:
                row["Subject"] = LABELS.get(object_name, ["Waygate service request", "Waygate inspection follow-up"])[row_index]
            if "Status" in headers:
                desired_status = (
                    ["Not Started", "Completed"][row_index]
                    if object_name == "Task"
                    else ["New", "Closed"][row_index]
                )
                allowed_status = record_type_values.get("Status")
                row["Status"] = (
                    desired_status
                    if allowed_status is None or desired_status in allowed_status
                    else sorted(allowed_status)[0] if allowed_status else ""
                )
            if "Priority" in headers:
                row["Priority"] = ["Normal", "High"][row_index]
            if "Origin" in headers:
                row["Origin"] = ["Web", "Phone"][row_index]
            for field in selected:
                row[field.name] = value_for(
                    field,
                    object_name,
                    row_index,
                    source_ids,
                    record_type_values,
                    row,
                )
            if "BusinessHoursId" in headers and target_default_business_hours_id:
                row["BusinessHoursId"] = target_default_business_hours_id
            rows.append(row)

        csv_path = output / f"{object_name}.csv"
        with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

        populated_metadata = sum(
            1 for field in selected if all(row.get(field.name, "") != "" for row in rows)
        ) + int(legacy_mapped_from_id)
        coverage = populated_metadata / writable_count if writable_count else 1
        entry = {
            "object": object_name,
            "csv": csv_path.name,
            "metadata_fields": len(fields),
            "writable_metadata_fields": writable_count,
            "selected_metadata_fields": len(selected) + int(legacy_mapped_from_id),
            "populated_in_both_rows": populated_metadata,
            "coverage_percent": round(coverage * 100, 1),
            "record_types": rt_names,
            "record_type_ids_included": bool(available_rt_names),
            "external_id": "Legacy_ID__c" if any(f.name == "Legacy_ID__c" for f in fields) else None,
        }
        report.append(entry)
        manifest["objects"].append(entry)

    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    with (output / "coverage.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(report[0]))
        writer.writeheader()
        writer.writerows(report)
    readme = [
        "# Waygate flat-file sample package",
        "",
        f"Generated {datetime.now(timezone.utc).isoformat()} from local Salesforce metadata.",
        "Each object contains two coordinated source records. `Id` values represent source-org IDs;",
        "the flat-file planner maps them to each object's `Legacy_ID__c` external ID.",
        "Lookup values use matching source IDs from the related CSV so the existing transformer",
        "can emit `ParentObject.Legacy_ID__c` relationship columns.",
        "",
        "The requested `BH_MCS_MCSAPServicesTransaction` name was resolved to",
        "`BH_MCS_MCSAPServicesTransactions__c` from the metadata.",
        "",
        "See `coverage.csv` and `manifest.json` for per-object coverage and Record Type status.",
    ]
    (output / "README.md").write_text("\n".join(readme) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
