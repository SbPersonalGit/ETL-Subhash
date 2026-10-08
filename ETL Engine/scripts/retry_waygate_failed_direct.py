"""Correct and directly retry the Waygate 1200 failed payloads.

This follows the transformed-staging convention: source ``Id`` has already
become ``Legacy_ID__c`` and lookup values use relationship External IDs.
Existing target Legacy IDs are excluded so the direct operation remains insert.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import polars as pl

from core.connector import connector_from_env
from core.loader import Loader
from core.stager import Stager


OBJECT_FILES = {
    "Contract": ("failed", "Contract_failed_20260731_193705.csv"),
    "Lead": ("failed", "Lead_failed_20260731_193719.csv"),
    "Opportunity": (
        ("failed", "Opportunity_failed_20260731_193737.csv"),
        ("blocked", "Opportunity_blocked_20260731_193737.csv"),
    ),
    "Case": ("failed", "Case_failed_20260731_193749.csv"),
}


def existing_legacy_ids(connector, object_name: str) -> set[str]:
    rows = connector.query(
        f"SELECT Legacy_ID__c FROM {object_name} WHERE Legacy_ID__c != null LIMIT 50000"
    )
    return {
        str(row.get("Legacy_ID__c") or "").strip()
        for row in rows
        if str(row.get("Legacy_ID__c") or "").strip()
    }


def target_id_map(connector, object_name: str) -> dict[str, str]:
    return {
        str(row.get("Legacy_ID__c") or "").strip(): str(row.get("Id") or "").strip()
        for row in connector.query(
            f"SELECT Id, Legacy_ID__c FROM {object_name} WHERE Legacy_ID__c != null LIMIT 50000"
        )
        if row.get("Id") and row.get("Legacy_ID__c")
    }


def corrected_rows(
    object_name: str,
    path: Path,
    account_target_ids: dict[str, str],
    contact_target_ids: dict[str, str],
    contract_target_ids: dict[str, str],
    opportunity_target_ids: dict[str, str],
    product_target_ids: dict[str, str],
) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for index, row in enumerate(rows):
        row.pop("sf__Id", None)
        row.pop("sf__Error", None)
        if object_name == "Contract":
            # Avoid activation automation during historical-data insertion.
            row["Status"] = "Pending"
        elif object_name == "Lead":
            row.pop("IsConverted", None)
            row["Latitude"] = f"{25.000000 + index * 0.001:.6f}"
            row["Longitude"] = f"{55.000000 + index * 0.001:.6f}"
            if row.get("Status") == "Converted":
                row["Status"] = "Active"
        elif object_name == "Opportunity":
            row["Probability"] = str(index % 101)
            # The target Opportunity.AccountId lookup filter rejects these
            # synthetic Accounts as non-commercial. AccountId is optional.
            row.pop("Account.Legacy_ID__c", None)
            # ContactId invokes target OpportunityContactRole automation whose
            # Account lookup filter rejects these synthetic commercial accounts.
            # It is optional, so omit it for this target-specific retry.
            row.pop("ContactId", None)
            source_contract_id = str(row.pop("Contract.Legacy_ID__c", "") or "").strip()
            target_contract_id = contract_target_ids.get(source_contract_id)
            if source_contract_id and not target_contract_id:
                raise ValueError(
                    f"Opportunity Contract {source_contract_id!r} has no target mapping"
                )
            row["ContractId"] = target_contract_id or ""
        elif object_name == "Case":
            relationship_fields = {
                "Account.Legacy_ID__c": ("AccountId", account_target_ids),
                "Contact.Legacy_ID__c": ("ContactId", contact_target_ids),
                "Opportunity_Name__r.Legacy_ID__c": (
                    "Opportunity_Name__c",
                    opportunity_target_ids,
                ),
                "Product.Legacy_ID__c": ("ProductId", product_target_ids),
            }
            for relationship_column, (target_field, id_map) in relationship_fields.items():
                source_id = str(row.pop(relationship_column, "") or "").strip()
                target_id = id_map.get(source_id)
                if source_id and not target_id:
                    raise ValueError(
                        f"Case {relationship_column} value {source_id!r} has no target mapping"
                    )
                row[target_field] = target_id or ""
    return rows


def write_payload(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    headers = list(rows[0])
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def insert_records_rest(
    connector,
    object_name: str,
    rows: list[dict[str, str]],
    log_path: Path,
    *,
    allow_duplicates: bool = False,
) -> tuple[int, int]:
    describe = connector.describe_object(object_name)
    writable = {
        str(field["name"])
        for field in describe.get("fields", [])
        if field.get("name") and field.get("createable")
    }
    failures = []
    success = 0
    url = connector.bulk_url(f"sobjects/{object_name}")
    for row in rows:
        payload = {
            key: value
            for key, value in row.items()
            if key in writable and value not in (None, "")
        }
        try:
            headers = (
                {"Sforce-Duplicate-Rule-Header": "allowSave=true"}
                if allow_duplicates else {}
            )
            response = connector._request("POST", url, json=payload, headers=headers).json()
            if response.get("success"):
                success += 1
            else:
                failures.append({"legacy_id": row.get("Legacy_ID__c"), "response": response})
        except Exception as exc:
            failures.append({"legacy_id": row.get("Legacy_ID__c"), "error": str(exc)})
    if failures:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps(failures, indent=2), encoding="utf-8")
    return success, len(failures)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("failed_dir", type=Path)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    failed_dir = args.failed_dir.resolve()
    output = failed_dir.parent / "direct_retry"
    payload_dir = output / "payloads"
    payload_dir.mkdir(parents=True, exist_ok=True)

    connector = connector_from_env("TARGET", args.profile).connect()
    account_target_ids = target_id_map(connector, "Account")
    contact_target_ids = target_id_map(connector, "Contact")
    contract_target_ids = target_id_map(connector, "Contract")
    opportunity_target_ids = target_id_map(connector, "Opportunity")
    product_target_ids = target_id_map(connector, "Product2")
    stager = Stager(":memory:")
    loader = Loader(connector, stager, log_root=output / "logs")
    try:
        for object_name, file_specs in OBJECT_FILES.items():
            if object_name == "Case":
                # Opportunity may have been completed earlier in this same run.
                opportunity_target_ids = target_id_map(connector, "Opportunity")
            if file_specs and isinstance(file_specs[0], str):
                file_specs = (file_specs,)
            rows = []
            for location, filename in file_specs:
                source_root = failed_dir if location == "failed" else failed_dir.parent / "load_blocked"
                rows.extend(corrected_rows(
                    object_name,
                    source_root / filename,
                    account_target_ids,
                    contact_target_ids,
                    contract_target_ids,
                    opportunity_target_ids,
                    product_target_ids,
                ))
            existing = existing_legacy_ids(connector, object_name)
            rows = [
                row for row in rows
                if str(row.get("Legacy_ID__c") or "").strip() not in existing
            ]
            if args.limit is not None:
                rows = rows[: max(0, args.limit)]
            payload_path = payload_dir / f"{object_name}_direct_insert.csv"
            write_payload(payload_path, rows)
            if not rows:
                print(f"{object_name}: nothing to insert; all Legacy IDs already exist")
                continue
            frame = pl.read_csv(
                payload_path,
                infer_schema_length=0,
                null_values=[],
                try_parse_dates=False,
            )
            if object_name == "Lead" and not args.dry_run:
                success_count, failed_count = insert_records_rest(
                    connector,
                    "Lead",
                    rows,
                    output / "logs" / "load_failed" / "Lead_rest_failed.json",
                    allow_duplicates=True,
                )
                print(
                    f"Lead: processed={len(rows)} success={success_count} "
                    f"failed={failed_count} blocked=0"
                )
                if failed_count:
                    raise RuntimeError("Lead direct REST retry was not fully successful")
                continue
            if object_name == "Opportunity" and not args.dry_run:
                success_count, failed_count = insert_records_rest(
                    connector,
                    "Opportunity",
                    rows,
                    output / "logs" / "load_failed" / "Opportunity_rest_failed.json",
                )
                print(
                    f"Opportunity: processed={len(rows)} success={success_count} "
                    f"failed={failed_count} blocked=0"
                )
                if failed_count:
                    raise RuntimeError("Opportunity direct REST retry was not fully successful")
                continue
            if object_name == "Case" and not args.dry_run:
                success_count, failed_count = insert_records_rest(
                    connector,
                    "Case",
                    rows,
                    output / "logs" / "load_failed" / "Case_rest_failed.json",
                )
                print(
                    f"Case: processed={len(rows)} success={success_count} "
                    f"failed={failed_count} blocked=0"
                )
                if failed_count:
                    raise RuntimeError("Case direct REST retry was not fully successful")
                continue
            result = loader.load_dataframe(
                object_name,
                frame,
                external_id_field="Legacy_ID__c",
                operation="insert",
                dry_run=args.dry_run,
            )
            print(
                f"{object_name}: processed={result.total_processed} "
                f"success={result.success_count} failed={result.failed_count} "
                f"blocked={result.blocked_count} failed_file={result.failed_file}"
            )
            if result.failed_count or result.blocked_count:
                raise RuntimeError(f"{object_name} direct retry was not fully successful")
    finally:
        stager.close()


if __name__ == "__main__":
    main()
