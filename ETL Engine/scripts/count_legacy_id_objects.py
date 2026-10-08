"""List target-org objects that have Legacy_ID__c and count non-null values."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.connector import connector_from_env


def tooling_query_all(connector, soql: str) -> list[dict]:
    url = f"{connector.instance_url}/services/data/{connector.api_version}/tooling/query"
    records: list[dict] = []
    params = {"q": soql}
    while True:
        resp = connector._request("GET", url, params=params)
        resp.raise_for_status()
        data = resp.json()
        records.extend(data.get("records", []))
        if data.get("done", True):
            break
        url = f"{connector.instance_url}{data['nextRecordsUrl']}"
        params = {}
    return records


def resolve_custom_object_api_name(
    developer: str,
    namespace: str,
    global_names: set[str],
) -> str:
    """
    Map CustomObject DeveloperName to the live queryable API name.

    Knowledge article types are stored as CustomObject DeveloperName X, but the
    queryable object that carries custom fields is X__kav (not X__c).
    """
    base = f"{namespace}__{developer}" if namespace else developer
    # Prefer Knowledge article version when present; otherwise normal custom object.
    for suffix in ("__kav", "__c"):
        candidate = f"{base}{suffix}"
        if candidate in global_names:
            return candidate
    # Fall back for clearer OBJECT_MISSING diagnostics.
    return f"{base}__c"


def resolve_custom_object_names(
    connector,
    object_ids: set[str],
    global_names: set[str],
) -> dict[str, str]:
    """Map CustomObject Ids (01I...) to QualifiedApiName."""
    mapping: dict[str, str] = {}
    if not object_ids:
        return mapping
    ids = sorted(object_ids)
    chunk_size = 100
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i : i + chunk_size]
        id_list = ",".join(f"'{oid}'" for oid in chunk)
        soql = f"SELECT Id, DeveloperName, NamespacePrefix FROM CustomObject WHERE Id IN ({id_list})"
        for row in tooling_query_all(connector, soql):
            developer = row.get("DeveloperName") or ""
            ns = row.get("NamespacePrefix") or ""
            if not developer:
                continue
            mapping[row["Id"]] = resolve_custom_object_api_name(developer, ns, global_names)
    return mapping


def objects_with_legacy_id(connector, global_names: set[str]) -> list[str]:
    """
    Find every object that has custom field Legacy_ID__c via Tooling CustomField.
    TableEnumOrId is the object API name for standard objects, or a CustomObject Id
    for custom objects. Exclude namespaced package fields (those are not Legacy_ID__c).
    """
    rows = tooling_query_all(
        connector,
        (
            "SELECT Id, DeveloperName, NamespacePrefix, TableEnumOrId "
            "FROM CustomField WHERE DeveloperName = 'Legacy_ID' AND NamespacePrefix = null"
        ),
    )
    standard_or_named: list[str] = []
    custom_ids: set[str] = set()
    for row in rows:
        table = (row.get("TableEnumOrId") or "").strip()
        if not table:
            continue
        if table.startswith("01I"):
            custom_ids.add(table)
        else:
            standard_or_named.append(table)

    id_to_name = resolve_custom_object_names(connector, custom_ids, global_names)
    unresolved = sorted(custom_ids - set(id_to_name))
    if unresolved:
        print(f"WARNING: could not resolve {len(unresolved)} CustomObject Ids: {unresolved[:10]}")

    names = sorted(set(standard_or_named) | set(id_to_name.values()))
    return names


def object_has_legacy_id_field(connector, object_name: str) -> tuple[bool, str]:
    """Confirm Legacy_ID__c exists on the live describe (filters stale Tooling rows)."""
    try:
        describe = connector.describe_object(object_name)
    except Exception as exc:
        return False, f"describe_failed: {exc}"
    for field in describe.get("fields") or []:
        if (field.get("name") or "") == "Legacy_ID__c":
            return True, "ok"
    return False, "field_missing_on_describe"


def count_legacy_populated(connector, object_name: str) -> tuple[int | None, str]:
    soql = f"SELECT COUNT(Id) cnt FROM {object_name} WHERE Legacy_ID__c != null"
    try:
        records = connector.query(soql)
        if not records:
            return 0, "ok"
        value = records[0].get("cnt", records[0].get("expr0"))
        return int(value or 0), "ok"
    except Exception as exc:
        return None, str(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "output" / "legacy_id_object_counts.csv",
    )
    parser.add_argument("--target-profile", default=None)
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env", override=True)
    connector = connector_from_env("TARGET", profile=args.target_profile)
    connector.connect()
    print(f"Connected: {connector.instance_url}")

    print("Discovering objects with Legacy_ID__c via Tooling CustomField...")
    global_by_name = {entry["name"]: entry for entry in connector.describe_global()}
    objects = objects_with_legacy_id(connector, set(global_by_name))
    print(f"Found {len(objects)} objects with Legacy_ID__c")

    results: list[dict] = []
    started = time.time()
    for idx, object_name in enumerate(objects, 1):
        meta = global_by_name.get(object_name)
        if meta is None:
            results.append(
                {
                    "Object": object_name,
                    "HasLegacyId": "Unknown",
                    "Queryable": "No",
                    "LegacyIdPopulatedCount": "",
                    "Status": "OBJECT_MISSING",
                    "Detail": "Not present in global describe",
                }
            )
            print(f"[{idx}/{len(objects)}] {object_name}: OBJECT_MISSING")
            continue
        if not meta.get("queryable"):
            results.append(
                {
                    "Object": object_name,
                    "HasLegacyId": "Unknown",
                    "Queryable": "No",
                    "LegacyIdPopulatedCount": "",
                    "Status": "NOT_QUERYABLE",
                    "Detail": "Object is not queryable via SOQL",
                }
            )
            print(f"[{idx}/{len(objects)}] {object_name}: NOT_QUERYABLE")
            continue

        has_field, field_status = object_has_legacy_id_field(connector, object_name)
        if not has_field:
            results.append(
                {
                    "Object": object_name,
                    "HasLegacyId": "No",
                    "Queryable": "Yes",
                    "LegacyIdPopulatedCount": "",
                    "Status": "NO_LEGACY_FIELD",
                    "Detail": field_status,
                }
            )
            print(f"[{idx}/{len(objects)}] {object_name}: NO_LEGACY_FIELD ({field_status})")
            continue

        count, status = count_legacy_populated(connector, object_name)
        results.append(
            {
                "Object": object_name,
                "HasLegacyId": "Yes",
                "Queryable": "Yes",
                "LegacyIdPopulatedCount": count if count is not None else "",
                "Status": "OK" if status == "ok" else "ERROR",
                "Detail": "" if status == "ok" else status,
            }
        )
        shown = count if count is not None else status
        print(f"[{idx}/{len(objects)}] {object_name}: {shown}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "Object",
                "HasLegacyId",
                "Queryable",
                "LegacyIdPopulatedCount",
                "Status",
                "Detail",
            ],
        )
        writer.writeheader()
        writer.writerows(results)

    ok_rows = [r for r in results if r["Status"] == "OK"]
    with_data = [r for r in ok_rows if int(r["LegacyIdPopulatedCount"] or 0) > 0]
    zero_rows = [r for r in ok_rows if int(r["LegacyIdPopulatedCount"] or 0) == 0]
    total_populated = sum(int(r["LegacyIdPopulatedCount"] or 0) for r in ok_rows)
    skipped = [r for r in results if r["Status"] != "OK"]

    summary = {
        "instance_url": connector.instance_url,
        "tooling_candidates": len(objects),
        "confirmed_with_legacy_id": len(ok_rows),
        "objects_with_populated_legacy_id": len(with_data),
        "objects_with_zero_legacy_id": len(zero_rows),
        "skipped_or_error": len(skipped),
        "total_populated_rows": total_populated,
        "elapsed_seconds": round(time.time() - started, 1),
        "output": str(args.output),
    }
    summary_path = args.output.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))
    print("\nObjects with populated Legacy_ID__c (count > 0):")
    for row in sorted(with_data, key=lambda r: int(r["LegacyIdPopulatedCount"]), reverse=True):
        print(f"  {row['Object']}: {row['LegacyIdPopulatedCount']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
