"""Investigate why Deal_Machine__kav was missing from Legacy_ID scan."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env", override=True)

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


def main() -> None:
    connector = connector_from_env("TARGET")
    connector.connect()

    matches = [o for o in connector.describe_global() if "Deal_Machine" in (o.get("name") or "")]
    print("=== GLOBAL DESCRIBE MATCHES ===")
    for obj in matches:
        print(
            json.dumps(
                {
                    "name": obj.get("name"),
                    "label": obj.get("label"),
                    "queryable": obj.get("queryable"),
                    "keyPrefix": obj.get("keyPrefix"),
                    "custom": obj.get("custom"),
                },
                indent=2,
            )
        )

    print("\n=== DESCRIBE CANDIDATES ===")
    candidates = [
        "Deal_Machine__kav",
        "Deal_Machine__ka",
        "Deal_Machine__c",
        "Deal_Machine__DataCategorySelection",
        "Deal_Machine__ViewStat",
        "Deal_Machine__VoteStat",
    ]
    for name in candidates:
        try:
            describe = connector.describe_object(name)
            legacy_fields = [
                field["name"]
                for field in describe.get("fields") or []
                if "Legacy" in (field.get("name") or "")
            ]
            print(
                f"{name}: OK queryable={describe.get('queryable')} "
                f"legacy_fields={legacy_fields}"
            )
        except Exception as exc:
            print(f"{name}: FAIL {exc}")

    print("\n=== TOOLING CustomField Legacy_ID rows related to Deal_Machine ===")
    rows = tooling_query_all(
        connector,
        (
            "SELECT Id, DeveloperName, NamespacePrefix, TableEnumOrId, ManageableState "
            "FROM CustomField WHERE DeveloperName = 'Legacy_ID' AND NamespacePrefix = null"
        ),
    )
    custom_ids = sorted(
        {
            row["TableEnumOrId"]
            for row in rows
            if str(row.get("TableEnumOrId") or "").startswith("01I")
        }
    )
    id_map: dict[str, dict] = {}
    for i in range(0, len(custom_ids), 100):
        chunk = custom_ids[i : i + 100]
        id_list = ",".join(f"'{oid}'" for oid in chunk)
        for row in tooling_query_all(
            connector,
            f"SELECT Id, DeveloperName, NamespacePrefix FROM CustomObject WHERE Id IN ({id_list})",
        ):
            developer = row.get("DeveloperName") or ""
            ns = row.get("NamespacePrefix") or ""
            # Our scan assumed __c. Knowledge articles are __kav.
            id_map[row["Id"]] = {
                "DeveloperName": developer,
                "NamespacePrefix": ns,
                "assumed_api": f"{ns}__{developer}__c" if ns else f"{developer}__c",
            }

    related = []
    for row in rows:
        table = row.get("TableEnumOrId") or ""
        meta = id_map.get(table)
        resolved = meta["assumed_api"] if meta else table
        if (
            "Deal_Machine" in table
            or "Deal_Machine" in resolved
            or (meta and meta.get("DeveloperName") == "Deal_Machine")
        ):
            related.append(
                {
                    "fieldId": row.get("Id"),
                    "TableEnumOrId": table,
                    "assumed_api_by_scan": resolved,
                    "customObject": meta,
                    "ManageableState": row.get("ManageableState"),
                }
            )
    print(json.dumps(related, indent=2))

    print("\n=== CustomObject metadata for Deal_Machine ===")
    custom_objects = tooling_query_all(
        connector,
        (
            "SELECT Id, DeveloperName, NamespacePrefix, ManageableState "
            "FROM CustomObject WHERE DeveloperName = 'Deal_Machine'"
        ),
    )
    print(json.dumps(custom_objects, indent=2))

    print("\n=== COUNT ON Deal_Machine__kav ===")
    try:
        count_rows = connector.query(
            "SELECT COUNT(Id) cnt FROM Deal_Machine__kav WHERE Legacy_ID__c != null"
        )
        print(count_rows)
    except Exception as exc:
        print(f"COUNT FAIL: {exc}")


if __name__ == "__main__":
    main()
