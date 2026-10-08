"""Audit and map Partner Central User dependencies from BH Dev to Waygate UAT."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path


def sf_query(alias: str, soql: str) -> list[dict]:
    executable = shutil.which("sf") or shutil.which("sf.cmd")
    if not executable:
        raise RuntimeError("Salesforce CLI executable was not found on PATH")
    result = subprocess.run(
        [executable, "data", "query", "--query", soql, "--target-org", alias, "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stdout.strip() or result.stderr.strip())
    payload = json.loads(result.stdout[result.stdout.find("{") :])
    return payload.get("result", {}).get("records", [])


def quoted(values: list[str]) -> str:
    return ",".join("'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'" for value in values)


def query_chunks(alias: str, select: str, object_name: str, field: str, values: set[str]) -> list[dict]:
    ordered = sorted(value for value in values if value)
    records: list[dict] = []
    for start in range(0, len(ordered), 100):
        part = ordered[start : start + 100]
        records.extend(sf_query(alias, f"SELECT {select} FROM {object_name} WHERE {field} IN ({quoted(part)})"))
    return records


def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("user_csv", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--source-org", default="BH Dev")
    parser.add_argument("--target-org", default="Waygate_UAT")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    with args.user_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = list(reader.fieldnames or [])
        users = list(reader)

    role_ids = {row.get("UserRoleId", "") for row in users} - {""}
    profile_ids = {row.get("ProfileId", "") for row in users} - {""}
    account_ids = {row.get("AccountId", "") for row in users} - {""}
    contact_ids = {row.get("ContactId", "") for row in users} - {""}
    user_ids = {row.get("Id", "") for row in users} - {""}

    source_roles = query_chunks(args.source_org, "Id,Name,DeveloperName,ParentRoleId", "UserRole", "Id", role_ids)
    role_names = {row["DeveloperName"] for row in source_roles}
    target_roles = query_chunks(args.target_org, "Id,Name,DeveloperName,ParentRoleId", "UserRole", "DeveloperName", role_names)
    target_role_by_name = {row["DeveloperName"]: row for row in target_roles}
    role_map: dict[str, str] = {}
    role_rows = []
    for source in sorted(source_roles, key=lambda row: row["DeveloperName"]):
        target = target_role_by_name.get(source["DeveloperName"])
        if target:
            role_map[source["Id"]] = target["Id"]
        role_rows.append({
            "SourceRoleId": source["Id"],
            "DeveloperName": source["DeveloperName"],
            "SourceName": source["Name"],
            "TargetRoleId": target["Id"] if target else "",
            "TargetName": target["Name"] if target else "",
            "Status": "MATCHED" if target else "MISSING_IN_TARGET",
        })

    source_profiles = query_chunks(args.source_org, "Id,Name,UserLicense.Name", "Profile", "Id", profile_ids)
    profile_names = {row["Name"] for row in source_profiles}
    target_profiles = query_chunks(args.target_org, "Id,Name,UserLicense.Name", "Profile", "Name", profile_names)
    target_profile_by_key = {(row["Name"], (row.get("UserLicense") or {}).get("Name")): row for row in target_profiles}
    profile_map: dict[str, str] = {}
    profile_rows = []
    for source in source_profiles:
        license_name = (source.get("UserLicense") or {}).get("Name", "")
        target = target_profile_by_key.get((source["Name"], license_name))
        if target:
            profile_map[source["Id"]] = target["Id"]
        profile_rows.append({
            "SourceProfileId": source["Id"],
            "ProfileName": source["Name"],
            "UserLicense": license_name,
            "TargetProfileId": target["Id"] if target else "",
            "Status": "MATCHED" if target else "MISSING_IN_TARGET_OR_LICENSE_MISMATCH",
        })

    lookup_results: dict[str, dict[str, str]] = {"AccountId": {}, "ContactId": {}}
    lookup_errors: dict[str, str] = {}
    for column, object_name, values in (
        ("AccountId", "Account", account_ids),
        ("ContactId", "Contact", contact_ids),
    ):
        try:
            records = query_chunks(args.target_org, "Id,Legacy_ID__c", object_name, "Legacy_ID__c", values)
            lookup_results[column] = {str(row.get("Legacy_ID__c")): row["Id"] for row in records if row.get("Legacy_ID__c")}
        except RuntimeError as exc:
            lookup_errors[column] = str(exc)[:500]

    mapped_users = []
    audit_rows = []
    for source in users:
        row = dict(source)
        row["ProfileId"] = profile_map.get(source.get("ProfileId", ""), "")
        row["UserRoleId"] = role_map.get(source.get("UserRoleId", ""), "")
        for column in ("AccountId", "ContactId"):
            legacy = source.get(column, "")
            row[column] = lookup_results[column].get(legacy, "") if legacy else ""
        mapped_users.append(row)
        audit_rows.append({
            "SourceUserId": source.get("Id", ""),
            "Username": source.get("Username", ""),
            "FederationIdentifier": source.get("FederationIdentifier", ""),
            "SourceProfileId": source.get("ProfileId", ""),
            "TargetProfileId": row.get("ProfileId", ""),
            "ProfileStatus": "MATCHED" if row.get("ProfileId") else "UNRESOLVED",
            "SourceUserRoleId": source.get("UserRoleId", ""),
            "TargetUserRoleId": row.get("UserRoleId", ""),
            "RoleStatus": "MATCHED" if row.get("UserRoleId") else "UNRESOLVED",
            "SourceAccountId": source.get("AccountId", ""),
            "TargetAccountId": row.get("AccountId", ""),
            "AccountStatus": "MATCHED" if row.get("AccountId") else "UNRESOLVED",
            "SourceContactId": source.get("ContactId", ""),
            "TargetContactId": row.get("ContactId", ""),
            "ContactStatus": "MATCHED" if row.get("ContactId") else "UNRESOLVED",
        })

    # Audit post-user access dependencies. These are exported as stable names, not source IDs.
    post_rows: list[dict] = []
    post_errors: dict[str, str] = {}
    post_queries = {
        "PermissionSetAssignment": "AssigneeId,PermissionSetId,PermissionSet.Name,PermissionSet.IsOwnedByProfile,PermissionSetGroupId,PermissionSetGroup.DeveloperName",
        "PermissionSetLicenseAssign": "AssigneeId,PermissionSetLicenseId,PermissionSetLicense.DeveloperName,PermissionSetLicense.MasterLabel",
        "UserPackageLicense": "UserId,PackageLicenseId,PackageLicense.NamespacePrefix",
        "GroupMember": "UserOrGroupId,GroupId,Group.Name,Group.DeveloperName,Group.Type",
    }
    for object_name, select in post_queries.items():
        user_field = "UserId" if object_name == "UserPackageLicense" else "UserOrGroupId" if object_name == "GroupMember" else "AssigneeId"
        try:
            records = query_chunks(args.source_org, select, object_name, user_field, user_ids)
            for record in records:
                post_rows.append({
                    "DependencyType": object_name,
                    "SourceUserId": record.get(user_field, ""),
                    "StableName": (
                        (record.get("PermissionSetGroup") or {}).get("DeveloperName")
                        or (record.get("PermissionSet") or {}).get("Name")
                        or (record.get("PermissionSetLicense") or {}).get("DeveloperName")
                        or (record.get("PackageLicense") or {}).get("NamespacePrefix")
                        or (record.get("Group") or {}).get("DeveloperName")
                        or ""
                    ),
                    "Subtype": (record.get("Group") or {}).get("Type", ""),
                    "SourceDependencyId": record.get("PermissionSetGroupId") or record.get("PermissionSetId") or record.get("PermissionSetLicenseId") or record.get("PackageLicenseId") or record.get("GroupId") or "",
                })
        except RuntimeError as exc:
            post_errors[object_name] = str(exc)[:500]

    write_csv(args.output_dir / "role_mapping.csv", ["SourceRoleId", "DeveloperName", "SourceName", "TargetRoleId", "TargetName", "Status"], role_rows)
    write_csv(args.output_dir / "profile_mapping.csv", ["SourceProfileId", "ProfileName", "UserLicense", "TargetProfileId", "Status"], profile_rows)
    write_csv(args.output_dir / "user_dependency_audit.csv", list(audit_rows[0]), audit_rows)
    write_csv(args.output_dir / "User_target_ids.csv", headers, mapped_users)
    write_csv(args.output_dir / "post_user_dependencies.csv", ["DependencyType", "SourceUserId", "StableName", "Subtype", "SourceDependencyId"], post_rows)

    summary = {
        "users": len(users),
        "profiles": {"source": len(profile_ids), "matched": len(profile_map)},
        "roles": {"source": len(role_ids), "matched": len(role_map), "missing": len(role_ids - set(role_map))},
        "accounts": {"source": len(account_ids), "matched": len(lookup_results["AccountId"])},
        "contacts": {"source": len(contact_ids), "matched": len(lookup_results["ContactId"])},
        "post_user_dependencies": dict(defaultdict(int)),
        "lookup_errors": lookup_errors,
        "post_query_errors": post_errors,
        "load_ready": all((len(profile_map) == len(profile_ids), len(role_map) == len(role_ids), len(lookup_results["AccountId"]) == len(account_ids), len(lookup_results["ContactId"]) == len(contact_ids))),
    }
    for row in post_rows:
        summary["post_user_dependencies"][row["DependencyType"]] = summary["post_user_dependencies"].get(row["DependencyType"], 0) + 1
    (args.output_dir / "dependency_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
