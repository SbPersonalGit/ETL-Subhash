"""Bulk Field-Level Security, object, and record-type access grants."""

from __future__ import annotations

import csv
import io
import json
import time
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from core.general_utility import resolve_permission_parents, validate_permission_principals

OBJECT_HEADER_ALIASES = frozenset({
    "object",
    "objects",
    "objectapi",
    "objectapiname",
    "object_api_name",
    "api_name",
    "apiname",
    "sobject",
    "sobjecttype",
    "name",
})


def read_object_list(path: Path) -> list[str]:
    """
    Read Salesforce object API names from a text or CSV file.

    Supports one name per line, optional # comments, or a CSV with an Object column.
    """
    raw = path.read_text(encoding="utf-8-sig")
    if not raw.strip():
        raise ValueError(f"Object list file is empty: {path}")

    objects: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        name = str(value or "").strip().strip('"').strip("'")
        if not name or name.startswith("#"):
            return
        key = name.casefold()
        if key in seen:
            return
        seen.add(key)
        objects.append(name)

    # Prefer CSV parsing when a delimiter is clearly present.
    delimiter_chars = {",", "\t", ";", "|"}
    uses_delimiter = any(char in raw for char in delimiter_chars)
    if uses_delimiter:
        sample = raw[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
            has_header = csv.Sniffer().has_header(sample)
        except csv.Error:
            dialect = csv.excel
            has_header = False
        reader = csv.reader(io.StringIO(raw), dialect)
        rows = [row for row in reader if any(str(cell or "").strip() for cell in row)]
        if not rows:
            raise ValueError(f"Object list file has no usable rows: {path}")
        header = [str(cell or "").strip() for cell in rows[0]]
        header_keys = [cell.casefold().replace(" ", "").replace("-", "") for cell in header]
        column_index = next(
            (index for index, key in enumerate(header_keys) if key in OBJECT_HEADER_ALIASES),
            0,
        )
        start = 1 if header_keys[column_index] in OBJECT_HEADER_ALIASES or has_header else 0
        for row in rows[start:]:
            if column_index < len(row):
                add(row[column_index])
    else:
        for line in raw.splitlines():
            add(line.split("#", 1)[0])
        # Single-column CSVs have no delimiter characters; drop a header row if present.
        if objects and objects[0].casefold().replace(" ", "").replace("-", "") in OBJECT_HEADER_ALIASES:
            objects = objects[1:]
            seen = {name.casefold() for name in objects}

    if not objects:
        raise ValueError(f"No object API names found in: {path}")
    return objects


def _soql_escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def _permissionable_fields(describe: dict) -> list[dict]:
    fields = []
    for field in describe.get("fields") or []:
        if not field.get("permissionable"):
            continue
        name = str(field.get("name") or "").strip()
        if not name:
            continue
        fields.append({
            "name": name,
            "editable": bool(field.get("updateable")),
            "label": str(field.get("label") or name),
        })
    return fields


def _upsert_object_permissions(connector, parent_id: str, object_name: str, describe: dict) -> dict:
    desired = {
        "PermissionsRead": True,
        "PermissionsCreate": bool(describe.get("createable", True)),
        "PermissionsEdit": bool(describe.get("updateable", True)),
        "PermissionsDelete": bool(describe.get("deletable", True)),
        "PermissionsViewAllRecords": True,
        "PermissionsModifyAllRecords": bool(describe.get("updateable", True)),
    }
    # View All / Modify All require Read / Edit respectively.
    if not desired["PermissionsRead"]:
        desired["PermissionsViewAllRecords"] = False
    if not desired["PermissionsEdit"]:
        desired["PermissionsModifyAllRecords"] = False

    existing = connector.query(
        f"SELECT Id, PermissionsRead, PermissionsCreate, PermissionsEdit, PermissionsDelete, "
        f"PermissionsViewAllRecords, PermissionsModifyAllRecords "
        f"FROM ObjectPermissions WHERE ParentId = '{parent_id}' AND SobjectType = '{_soql_escape(object_name)}'"
    )
    url_base = f"{connector.instance_url}/services/data/{connector.api_version}/sobjects/ObjectPermissions"
    attempts = [
        desired,
        {**desired, "PermissionsViewAllRecords": False, "PermissionsModifyAllRecords": False},
        {
            "PermissionsRead": True,
            "PermissionsCreate": False,
            "PermissionsEdit": False,
            "PermissionsDelete": False,
            "PermissionsViewAllRecords": False,
            "PermissionsModifyAllRecords": False,
        },
    ]
    last_error = None
    for payload in attempts:
        try:
            if existing:
                row = existing[0]
                changes = {
                    key: value
                    for key, value in payload.items()
                    if bool(row.get(key)) != bool(value)
                }
                if not changes:
                    return {"created": False, "updated": False, "reason": "already_exists", "permissions": payload}
                connector._request("PATCH", f"{url_base}/{row['Id']}", json=changes)
                return {"created": False, "updated": True, "id": row["Id"], "permissions": payload}
            response = connector._request(
                "POST",
                url_base,
                json={"ParentId": parent_id, "SobjectType": object_name, **payload},
            ).json()
            return {"created": True, "updated": False, "id": response.get("id"), "permissions": payload}
        except Exception as exc:
            last_error = str(exc)
            continue
    return {"created": False, "updated": False, "error": last_error or "Unable to grant object permissions"}


def _upsert_field_permission(
    connector,
    parent_id: str,
    object_name: str,
    field_name: str,
    *,
    editable: bool,
) -> dict:
    full_field = f"{object_name}.{field_name}"
    existing = connector.query(
        f"SELECT Id, PermissionsRead, PermissionsEdit FROM FieldPermissions "
        f"WHERE ParentId = '{parent_id}' AND SobjectType = '{_soql_escape(object_name)}' "
        f"AND Field = '{_soql_escape(full_field)}'"
    )
    url_base = f"{connector.instance_url}/services/data/{connector.api_version}/sobjects/FieldPermissions"
    wanted_edit = bool(editable)
    attempts = [wanted_edit, False] if wanted_edit else [False]
    last_error = None
    for edit_flag in attempts:
        payload = {"PermissionsRead": True, "PermissionsEdit": edit_flag}
        try:
            if existing:
                row = existing[0]
                if bool(row.get("PermissionsRead")) and bool(row.get("PermissionsEdit")) == edit_flag:
                    return {
                        "field": field_name,
                        "created": False,
                        "updated": False,
                        "reason": "already_exists",
                        "readable": True,
                        "editable": edit_flag,
                    }
                connector._request("PATCH", f"{url_base}/{row['Id']}", json=payload)
                return {
                    "field": field_name,
                    "created": False,
                    "updated": True,
                    "id": row["Id"],
                    "readable": True,
                    "editable": edit_flag,
                }
            response = connector._request(
                "POST",
                url_base,
                json={
                    "ParentId": parent_id,
                    "SobjectType": object_name,
                    "Field": full_field,
                    **payload,
                },
            ).json()
            return {
                "field": field_name,
                "created": True,
                "updated": False,
                "id": response.get("id"),
                "readable": True,
                "editable": edit_flag,
            }
        except Exception as exc:
            last_error = str(exc)
            continue
    return {"field": field_name, "created": False, "updated": False, "error": last_error or "Unable to grant field permission"}


def _grant_record_types_permission_set(connector, parent_id: str, object_name: str) -> list[dict]:
    rows = connector.query(
        f"SELECT Id, DeveloperName, Name, IsActive FROM RecordType "
        f"WHERE SobjectType = '{_soql_escape(object_name)}' AND IsActive = true"
    )
    results = []
    url = f"{connector.instance_url}/services/data/{connector.api_version}/sobjects/SetupEntityAccess"
    for row in rows:
        record_type_id = row["Id"]
        label = str(row.get("DeveloperName") or row.get("Name") or record_type_id)
        existing = connector.query(
            f"SELECT Id FROM SetupEntityAccess WHERE ParentId = '{parent_id}' "
            f"AND SetupEntityId = '{record_type_id}'"
        )
        if existing:
            results.append({
                "record_type": label,
                "id": existing[0]["Id"],
                "created": False,
                "reason": "already_exists",
            })
            continue
        try:
            response = connector._request(
                "POST",
                url,
                json={"ParentId": parent_id, "SetupEntityId": record_type_id},
            ).json()
            results.append({"record_type": label, "id": response.get("id"), "created": True})
        except Exception as exc:
            results.append({"record_type": label, "created": False, "error": str(exc)})
    return results


def _profile_metadata_full_name(connector, profile_name: str) -> str:
    safe = _soql_escape(profile_name)
    tooling_url = (
        f"{connector.instance_url}/services/data/{connector.api_version}/tooling/query/"
        f"?q=SELECT+Id,Name,FullName+FROM+Profile+WHERE+Name+=+'{safe}'"
    )
    try:
        payload = connector._request("GET", tooling_url).json()
        records = payload.get("records") or []
        if records and records[0].get("FullName"):
            return str(records[0]["FullName"])
    except Exception:
        pass
    return profile_name


def _grant_record_types_profile(connector, profile_name: str, object_name: str) -> list[dict]:
    rows = connector.query(
        f"SELECT Id, DeveloperName, Name FROM RecordType "
        f"WHERE SobjectType = '{_soql_escape(object_name)}' AND IsActive = true"
    )
    if not rows:
        return []
    full_name = _profile_metadata_full_name(connector, profile_name)
    visibility_xml = []
    for row in rows:
        developer = str(row.get("DeveloperName") or "").strip()
        if not developer:
            continue
        visibility_xml.append(
            "        <recordTypeVisibilities>\n"
            f"            <recordType>{escape(object_name)}.{escape(developer)}</recordType>\n"
            "            <default>false</default>\n"
            "            <visible>true</visible>\n"
            "        </recordTypeVisibilities>"
        )
    if not visibility_xml:
        return []

    profile_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Profile xmlns="http://soap.sforce.com/2006/04/metadata">\n'
        + "\n".join(visibility_xml)
        + "\n</Profile>\n"
    )
    package_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Package xmlns="http://soap.sforce.com/2006/04/metadata">\n'
        "    <types>\n"
        f"        <members>{escape(full_name)}</members>\n"
        "        <name>Profile</name>\n"
        "    </types>\n"
        "    <version>61.0</version>\n"
        "</Package>\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("package.xml", package_xml)
        archive.writestr(f"profiles/{full_name}.profile", profile_xml)
    zip_bytes = buffer.getvalue()

    deploy_url = f"{connector.instance_url}/services/data/{connector.api_version}/metadata/deployRequest"
    deploy_options = {
        "allowMissingFiles": False,
        "autoUpdatePackage": False,
        "checkOnly": False,
        "ignoreWarnings": True,
        "purgeOnDelete": False,
        "rollbackOnError": True,
        "singlePackage": True,
        "testLevel": "NoTestRun",
    }
    try:
        response = connector._session.post(
            deploy_url,
            headers={"Authorization": f"Bearer {connector.access_token}"},
            files={
                "json": (None, json.dumps({"deployOptions": deploy_options}), "application/json"),
                "file": ("package.zip", zip_bytes, "application/zip"),
            },
            timeout=(15, 180),
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Metadata deploy failed [{response.status_code}]: {response.text[:2000]}")
        deploy_id = (response.json() or {}).get("id")
        if not deploy_id:
            raise RuntimeError("Metadata deploy did not return a deployment id.")
        status_url = f"{deploy_url}/{deploy_id}?includeDetails=true"
        for _ in range(60):
            status = connector._request("GET", status_url).json()
            deploy_result = status.get("deployResult") or status
            state = str(deploy_result.get("status") or status.get("status") or "").upper()
            if state in {"SUCCEEDED", "SUCCEEDED_PARTIAL", "FAILED", "CANCELED", "CANCELLED"}:
                if state.startswith("SUCCEED"):
                    return [
                        {
                            "record_type": str(row.get("DeveloperName") or row.get("Name")),
                            "created": True,
                            "reason": "metadata_deploy",
                            "deployment_id": deploy_id,
                        }
                        for row in rows
                        if row.get("DeveloperName")
                    ]
                details = deploy_result.get("details") or {}
                failures = details.get("componentFailures") or []
                message = failures[0].get("problem") if failures and isinstance(failures[0], dict) else state
                raise RuntimeError(f"Profile record-type deploy {state}: {message}")
            time.sleep(2)
        raise RuntimeError(f"Timed out waiting for profile record-type deploy {deploy_id}")
    except Exception as exc:
        return [{"record_type": "*", "created": False, "error": str(exc)}]


def assign_object_access(
    connector,
    object_name: str,
    parents: list[dict],
) -> dict:
    """Grant object, field, and record-type access for one object to all parents."""
    describe = connector.describe_object(object_name)
    fields = _permissionable_fields(describe)
    principal_results = []
    for parent in parents:
        object_result = _upsert_object_permissions(connector, parent["parent_id"], object_name, describe)
        field_results = [
            _upsert_field_permission(
                connector,
                parent["parent_id"],
                object_name,
                field["name"],
                editable=field["editable"],
            )
            for field in fields
        ]
        if parent.get("profile_owned"):
            record_types = _grant_record_types_profile(connector, parent["name"], object_name)
        else:
            record_types = _grant_record_types_permission_set(connector, parent["parent_id"], object_name)
        principal_results.append({
            "principal": parent["label"],
            "object_permissions": object_result,
            "fields": field_results,
            "record_types": record_types,
            "field_created": sum(1 for item in field_results if item.get("created")),
            "field_updated": sum(1 for item in field_results if item.get("updated")),
            "field_errors": sum(1 for item in field_results if item.get("error")),
            "record_type_granted": sum(1 for item in record_types if item.get("created") or item.get("reason") == "already_exists"),
            "record_type_errors": sum(1 for item in record_types if item.get("error")),
        })
    return {
        "object": object_name,
        "field_count": len(fields),
        "principals": principal_results,
    }


def bulk_assign_fls(
    connector,
    object_names: list[str],
    permission_sets: list[str],
    profiles: list[str],
    *,
    progress_callback=None,
) -> dict:
    """Validate principals and grant full field/object/record-type access for each object."""
    validate_permission_principals(connector, permission_sets, profiles)
    parents = resolve_permission_parents(connector, permission_sets, profiles)
    results = []
    for index, object_name in enumerate(object_names, 1):
        if progress_callback:
            progress_callback(f"Processing object {index}/{len(object_names)}: {object_name}")
        try:
            results.append(assign_object_access(connector, object_name, parents))
        except Exception as exc:
            results.append({"object": object_name, "error": str(exc), "principals": []})
    return {
        "objects": results,
        "permission_sets": permission_sets,
        "profiles": profiles,
        "object_count": len(object_names),
        "error_count": sum(1 for item in results if item.get("error")),
    }
