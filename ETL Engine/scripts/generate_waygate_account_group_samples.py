"""Generate a two-pass, lookup-rich sample package for the Account cycle group."""

from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from core.connector import connector_from_env
from scripts.generate_waygate_standard_samples import (
    is_writable,
    scalar_value,
    stable_source_id,
    write_csv,
)
from scripts.generate_waygate_wave_samples import (
    dependency_order,
    dependent_picklist_value,
)


OBJECTS = (
    "Account",
    "BH_MCS_MCIB__c",
    "BH_MCS_MCServiceAgreement__c",
    "BH_MCS_WOIBLink__c",
    "Case",
    "Contact",
    "Contract",
    "Opportunity",
    "Project_ge_og__c",
    "SVMXC__Installed_Product__c",
    "SVMXC__Service_Group_Members__c",
    "SVMXC__Service_Order__c",
    "SVMXC__Site__c",
)

TECHNICAL_REFERENCES = {"RecordType", "Group"}
SKIP_LOOKUP_FIELDS = {"OwnerId", "RecordTypeId"}


def required(field: dict) -> bool:
    return bool(
        field.get("createable")
        and not field.get("nillable", True)
        and not field.get("defaultedOnCreate", False)
        and not field.get("calculated", False)
        and not field.get("autoNumber", False)
    )


def usable_reference(field: dict) -> str | None:
    targets = [str(value) for value in field.get("referenceTo") or [] if value]
    if (
        field.get("type") != "reference"
        or not field.get("createable")
        or field.get("name") in SKIP_LOOKUP_FIELDS
        or len(targets) != 1
        or targets[0] in TECHNICAL_REFERENCES
    ):
        return None
    return targets[0]


def select_scalar_fields(fields: list[dict], coverage: float, minimum: int) -> list[dict]:
    writable = [field for field in fields if is_writable(field)]
    scalar = [
        field for field in writable
        if field.get("type") != "reference" and field.get("name") != "Legacy_ID__c"
    ]
    mandatory = [field for field in scalar if required(field)]
    mandatory_names = {field["name"] for field in mandatory}
    candidates = [field for field in scalar if field["name"] not in mandatory_names]
    target = max(minimum, math.ceil(len(writable) * coverage))
    selected = [*mandatory, *candidates[:max(0, target - 1 - len(mandatory))]]
    # Remove inaccessible dependent chains; required chains are rejected.
    while True:
        names = {str(field["name"]) for field in selected}
        unsafe = [
            field for field in selected
            if field.get("dependentPicklist")
            and str(field.get("controllerName") or "") not in names
        ]
        if not unsafe:
            break
        required_unsafe = [field["name"] for field in unsafe if required(field)]
        if required_unsafe:
            raise ValueError(
                "required dependent picklist controller is unavailable: "
                + ", ".join(required_unsafe)
            )
        unsafe_names = {field["name"] for field in unsafe}
        selected = [field for field in selected if field["name"] not in unsafe_names]
    return selected


def record_type_scalar_value(
    object_name: str,
    field: dict,
    row_index: int,
    picklist_metadata: dict[str, dict],
) -> str:
    if object_name == "Contract" and str(field.get("name") or "") == "Status":
        return "Draft"
    field_type = str(field.get("type") or "").casefold()
    if field_type in {"picklist", "multipicklist"}:
        config = picklist_metadata.get(str(field["name"]))
        if isinstance(config, dict):
            values = sorted(str(value) for value in (config.get("values") or []))
            if values:
                return values[row_index % len(values)]
    return scalar_value(object_name, field, row_index)


def query_reference_pool(conn, object_name: str) -> dict:
    describe = conn.describe_object(object_name)
    fields = {str(field.get("name")): field for field in describe.get("fields") or []}
    has_legacy = bool(
        fields.get("Legacy_ID__c")
        and fields["Legacy_ID__c"].get("externalId")
    )
    active_where = " WHERE IsActive = true" if object_name in {"BusinessHours", "User"} else ""
    try:
        rows = conn.query(f"SELECT Id FROM {object_name}{active_where} LIMIT 100")
        legacy_rows = (
            conn.query(
                f"SELECT Id, Legacy_ID__c FROM {object_name} "
                "WHERE Legacy_ID__c != null LIMIT 100"
            )
            if has_legacy else []
        )
    except Exception:
        return {"object": object_name, "ids": [], "legacy_ids": [], "has_legacy": has_legacy}
    return {
        "object": object_name,
        "ids": [str(row["Id"]) for row in rows if row.get("Id")],
        "legacy_ids": [
            str(row["Legacy_ID__c"])
            for row in legacy_rows
            if row.get("Legacy_ID__c")
        ],
        "has_legacy": has_legacy,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--records-per-object", type=int, default=100)
    parser.add_argument("--coverage", type=float, default=0.20)
    parser.add_argument("--minimum-scalar-fields", type=int, default=15)
    args = parser.parse_args()

    output = args.output.resolve()
    insert_dir = output / "01_insert_files"
    deferred_dir = output / "02_deferred_lookup_updates"
    insert_dir.mkdir(parents=True, exist_ok=True)
    deferred_dir.mkdir(parents=True, exist_ok=True)
    conn = connector_from_env("TARGET", args.profile).connect()
    describes = {name: conn.describe_object(name) for name in OBJECTS}
    source_ids = {
        name: [stable_source_id(name, index) for index in range(args.records_per_object)]
        for name in OBJECTS
    }

    reference_objects = set()
    for describe in describes.values():
        for field in describe.get("fields") or []:
            target = usable_reference(field)
            if target and target not in OBJECTS:
                reference_objects.add(target)
    pools = {name: query_reference_pool(conn, name) for name in sorted(reference_objects)}
    # Accounts with source-system keys are preferred for required Contract/Project references.
    account_pool = query_reference_pool(conn, "Account")
    if not account_pool["legacy_ids"]:
        raise RuntimeError("Existing target Account records have no Legacy_ID__c values")

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_profile": args.profile,
        "records_per_object": args.records_per_object,
        "total_insert_records": len(OBJECTS) * args.records_per_object,
        "objects": list(OBJECTS),
        "passes": [
            {"order": 1, "directory": "01_insert_files", "operation": "upsert"},
            {"order": 2, "directory": "02_deferred_lookup_updates", "operation": "upsert"},
        ],
        "reference_pools": pools,
        "object_details": [],
    }
    strategies: list[dict] = []

    for object_name in OBJECTS:
        describe = describes[object_name]
        fields = describe.get("fields") or []
        scalar_fields = select_scalar_fields(
            fields, args.coverage, args.minimum_scalar_fields
        )
        default_rt = next(
            (
                str(info.get("recordTypeId") or "")
                for info in (describe.get("recordTypeInfos") or [])
                if info.get("defaultRecordTypeMapping") and info.get("available", True)
            ),
            "",
        )
        picklists = (
            conn.describe_record_type_picklists(object_name, default_rt)
            if default_rt else {}
        )
        unavailable_picklists = [
            field
            for field in scalar_fields
            if str(field.get("type") or "").casefold() in {"picklist", "multipicklist"}
            and isinstance(picklists.get(str(field["name"])), dict)
            and not (picklists[str(field["name"])].get("values") or [])
        ]
        unavailable_required = [field["name"] for field in unavailable_picklists if required(field)]
        if unavailable_required:
            raise ValueError(
                f"{object_name}: required picklist has no values for default Record Type: "
                + ", ".join(unavailable_required)
            )
        unavailable_names = {field["name"] for field in unavailable_picklists}
        scalar_fields = [field for field in scalar_fields if field["name"] not in unavailable_names]
        required_refs = []
        external_refs = []
        deferred_refs = []
        skipped_refs = []
        for field in fields:
            target = usable_reference(field)
            if not target:
                continue
            item = (field, target)
            if required(field):
                required_refs.append(item)
            elif target in OBJECTS:
                deferred_refs.append(item)
            elif pools.get(target, {}).get("ids"):
                external_refs.append(item)
            else:
                skipped_refs.append({"field": field["name"], "target": target, "reason": "no reusable target records"})

        insert_fields = [*scalar_fields, *[field for field, _ in required_refs], *[field for field, _ in external_refs]]
        insert_rows = []
        for index in range(args.records_per_object):
            row = {"Id": source_ids[object_name][index]}
            for field in dependency_order(scalar_fields):
                if field.get("dependentPicklist"):
                    row[field["name"]] = dependent_picklist_value(field, row, picklists, index)
                else:
                    row[field["name"]] = record_type_scalar_value(
                        object_name, field, index, picklists
                    )
            for field, target in [*required_refs, *external_refs]:
                if target == "Account" and required(field):
                    values = account_pool["legacy_ids"]
                    strategy = "external_id"
                    external_id = "Legacy_ID__c"
                else:
                    pool = pools[target]
                    if pool["legacy_ids"]:
                        values = pool["legacy_ids"]
                        strategy = "external_id"
                        external_id = "Legacy_ID__c"
                    else:
                        values = pool["ids"]
                        strategy = "target_id"
                        external_id = ""
                row[field["name"]] = values[index % len(values)]
                if index == 0:
                    strategies.append({
                        "pass": "insert",
                        "object": object_name,
                        "field": field["name"],
                        "referenced_object": target,
                        "resolution": strategy,
                        "lookup_external_id": external_id,
                        "relationship_name": str(field.get("relationshipName") or ""),
                        "target_mapping": (
                            f"{field.get('relationshipName')}.Legacy_ID__c"
                            if strategy == "external_id" and field.get("relationshipName")
                            else field["name"]
                        ),
                        "required": required(field),
                    })
            insert_rows.append(row)

        insert_headers = ["Id", *[field["name"] for field in insert_fields]]
        write_csv(insert_dir / f"{object_name}.csv", insert_headers, insert_rows)

        deferred_rows = []
        if deferred_refs:
            for index in range(args.records_per_object):
                row = {"Id": source_ids[object_name][index]}
                for field, target in deferred_refs:
                    row[field["name"]] = source_ids[target][index % args.records_per_object]
                    if index == 0:
                        strategies.append({
                            "pass": "deferred_update",
                            "object": object_name,
                            "field": field["name"],
                            "referenced_object": target,
                            "resolution": "external_id",
                            "lookup_external_id": "Legacy_ID__c",
                            "relationship_name": str(field.get("relationshipName") or ""),
                            "target_mapping": (
                                f"{field.get('relationshipName')}.Legacy_ID__c"
                                if field.get("relationshipName") else field["name"]
                            ),
                            "required": False,
                        })
                deferred_rows.append(row)
            deferred_headers = ["Id", *[field["name"] for field, _ in deferred_refs]]
            write_csv(deferred_dir / f"{object_name}.csv", deferred_headers, deferred_rows)

        manifest["object_details"].append({
            "object": object_name,
            "insert_records": len(insert_rows),
            "insert_columns": len(insert_headers),
            "scalar_fields": len(scalar_fields),
            "required_lookups": len(required_refs),
            "existing_target_lookups": len(external_refs),
            "deferred_group_lookups": len(deferred_refs),
            "skipped_lookup_fields": skipped_refs,
            "deferred_file": bool(deferred_refs),
        })

    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    with (output / "lookup_strategy.csv").open("w", encoding="utf-8", newline="") as handle:
        columns = ["pass", "object", "field", "referenced_object", "resolution", "lookup_external_id", "relationship_name", "target_mapping", "required"]
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(strategies)
    with (output / "object_summary.csv").open("w", encoding="utf-8", newline="") as handle:
        columns = ["object", "insert_records", "insert_columns", "scalar_fields", "required_lookups", "existing_target_lookups", "deferred_group_lookups", "deferred_file"]
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows({key: item[key] for key in columns} for item in manifest["object_details"])
    readme = [
        "# Waygate Account cycle-group sample package",
        "",
        f"Objects: {len(OBJECTS)}. Insert records: {manifest['total_insert_records']}.",
        "",
        "Run `01_insert_files` first. Map each source `Id` to target `Legacy_ID__c` and use upsert.",
        "Run `02_deferred_lookup_updates` only after every insert object succeeds.",
        "For every lookup, follow `lookup_strategy.csv`: `target_id` means preserve the supplied Salesforce ID; `external_id` means resolve using the listed external ID.",
        "The `target_mapping` column records the relationship mapping expected for external-ID resolution.",
        "Do not combine both directories into one upload batch.",
    ]
    (output / "README.md").write_text("\n".join(readme) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
