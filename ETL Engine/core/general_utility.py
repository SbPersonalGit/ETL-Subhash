"""Reusable CSV preparation and Salesforce metadata helpers for General Utility."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_FIELD_DESCRIPTION = (
    "Field created for BH to Waygate Data Migration validation purpose. "
    "Can be deleted after validation done."
)
# OwnerId often targets User+Group; CreatedBy/LastModifiedBy target User only.
USER_LIKE_REFERENCE_TARGETS = frozenset({"User", "Group"})
EXCLUDED_DEFAULT_REFERENCE_TARGETS = frozenset({"User", "Group", "RecordType", "Profile"})
API_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
csv.field_size_limit(2_147_483_647)


def is_user_like_reference(referenced_objects: list[str] | tuple[str, ...] | None) -> bool:
    """True when a reference only points at User (and optionally Group)."""
    refs = {
        str(item or "").strip()
        for item in (referenced_objects or [])
        if str(item or "").strip()
    }
    return bool(refs) and refs <= USER_LIKE_REFERENCE_TARGETS


def is_lookup_or_master_detail_default(field: dict, referenced_objects: list[str] | None = None) -> bool:
    """
    True when a reference field should be default-selected for preservation.

    Only Lookup / Master-Detail business relationships qualify. User, Group,
    RecordType, and Profile targets stay unchecked unless chosen manually.
    """
    if str(field.get("type") or "").casefold() != "reference":
        return False
    refs = [
        str(item or "").strip()
        for item in (
            referenced_objects
            if referenced_objects is not None
            else (field.get("referenceTo") or [])
        )
        if str(item or "").strip()
    ]
    if not refs:
        return False
    name = str(field.get("name") or "").strip().casefold()
    if name in {"recordtypeid", "profileid", "ownerid", "createdbyid", "lastmodifiedbyid"}:
        return False
    if "RecordType" in refs or "Profile" in refs:
        return False
    if is_user_like_reference(refs) or set(refs) <= EXCLUDED_DEFAULT_REFERENCE_TARGETS:
        return False
    return True


def validate_custom_field_api_name(api_name: str) -> None:
    """Reject names Salesforce would interpret as namespace-qualified."""
    if (
        not API_NAME_RE.fullmatch(api_name)
        or not api_name.endswith("__c")
        or "__" in api_name[:-3]
    ):
        raise ValueError(
            f"Invalid custom field API name: {api_name!r}. "
            "Only the final '__c' may contain consecutive underscores."
        )


def identity_variants(value: str) -> set[str]:
    """Match Salesforce IDs safely while leaving arbitrary external IDs exact."""
    cleaned = str(value or "").strip()
    if not cleaned:
        return set()
    variants = {cleaned}
    if re.fullmatch(r"[A-Za-z0-9]{18}", cleaned):
        variants.update({cleaned[:15], cleaned.casefold()})
    return variants


def inventory_contains(inventory: set[str], value: str) -> bool:
    keys = set().union(*(identity_variants(item) for item in inventory))
    return bool(identity_variants(value) & keys)


def populated_counts(path: Path) -> tuple[list[str], Counter[str], int]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = list(reader.fieldnames or [])
        if not headers or any(not str(item or "").strip() for item in headers):
            raise ValueError(f"CSV has missing or blank headers: {path.name}")
        counts: Counter[str] = Counter()
        rows = 0
        for row in reader:
            rows += 1
            for header in headers:
                if str(row.get(header) or "").strip():
                    counts[header] += 1
    return headers, counts, rows


def legacy_field_name(source_column: str, prefix: str = "Legacy_") -> str:
    prefix = str(prefix or "").strip()
    if not prefix or not API_NAME_RE.fullmatch(prefix):
        raise ValueError("Prefix must start with a letter and contain only letters, numbers, or underscores.")
    base = str(source_column or "").strip()
    if base.endswith("__c"):
        base = base[:-3]
    candidate = f"{prefix}{base}__c"
    if len(candidate) > 40:
        available = 40 - len(prefix) - len("__c")
        hash_length = 6
        stem_length = available - hash_length - 1
        if stem_length < 1:
            raise ValueError(
                "Prefix is too long to generate a unique Salesforce field API name. "
                "Shorten the prefix and analyze again."
            )
        digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:hash_length]
        candidate = f"{prefix}{base[:stem_length]}_{digest}__c"
    return candidate


def discover_populated_references(path: Path, describe: dict, prefix: str) -> dict:
    headers, counts, row_count = populated_counts(path)
    by_name = {str(field.get("name") or "").casefold(): field for field in describe.get("fields", [])}
    references = []
    preservation_candidates = []
    for header in headers:
        field = by_name.get(header.casefold())
        if not field:
            continue
        field_type = str(field.get("type") or "").casefold()
        is_reference = field_type == "reference"
        is_auto_number = bool(field.get("autoNumber"))
        is_custom = bool(field.get("custom")) or header.endswith("__c")
        if counts[header] and (is_reference or is_auto_number or is_custom) and not header.casefold().startswith(prefix.casefold()):
            original_base = header[:-3] if header.endswith("__c") else header
            requested_legacy_field = f"{prefix}{original_base}__c"
            legacy_field_error = (
                f"Generated field API name exceeds 40 characters: {requested_legacy_field}"
                if len(requested_legacy_field) > 40 else ""
            )
            referenced_objects = list(field.get("referenceTo") or [])
            relationship_name = str(field.get("relationshipName") or "").strip()
            user_like = is_reference and is_user_like_reference(referenced_objects)
            default_lookup_or_md = is_lookup_or_master_detail_default(field, referenced_objects)
            if relationship_name and len(referenced_objects) == 1:
                match_expression = f"{relationship_name}.External_Id__c"
            elif relationship_name and "User" in referenced_objects:
                match_expression = f"{relationship_name}:User.External_Id__c"
            else:
                match_expression = header
            # Default-check only Lookup / Master-Detail business references.
            # User/Group, RecordType, Profile, and auto-numbers stay off unless chosen.
            default_selected = bool(counts[header] and default_lookup_or_md)
            preservation_candidates.append({
                "column": header,
                "populated_rows": counts[header],
                "source_field_type": field_type or "unknown",
                "kind": "reference" if is_reference else "auto_number" if is_auto_number else "custom",
                "referenced_objects": referenced_objects if is_reference else [],
                "is_user_reference": user_like,
                "default_selected": default_selected,
                "match_supported": not is_auto_number,
                "default_match": bool(counts[header] and default_lookup_or_md),
                "match_expression": match_expression,
                "legacy_field": requested_legacy_field,
                "legacy_field_valid": not legacy_field_error,
                "legacy_field_error": legacy_field_error,
                "legacy_field_shortened": False,
                "requested_legacy_field": requested_legacy_field,
            })
        if not counts[header] or not is_reference:
            continue
        referenced_objects = list(field.get("referenceTo") or [])
        relationship_name = str(field.get("relationshipName") or "").strip()
        if relationship_name and len(referenced_objects) == 1:
            match_expression = f"{relationship_name}.External_Id__c"
        elif relationship_name and "User" in referenced_objects:
            match_expression = f"{relationship_name}:User.External_Id__c"
        else:
            match_expression = header
        original_base = header[:-3] if header.endswith("__c") else header
        generated_legacy_field = f"{prefix}{original_base}__c"
        legacy_field_error = (
            f"Generated field API name exceeds 40 characters: {generated_legacy_field}"
            if len(generated_legacy_field) > 40 else ""
        )
        references.append({
            "column": header,
            "populated_rows": counts[header],
            "referenced_objects": referenced_objects,
            "polymorphic": len(referenced_objects) != 1,
            "relationship_name": relationship_name,
            "match_expression": match_expression,
            "legacy_field": generated_legacy_field,
            "legacy_field_valid": not legacy_field_error,
            "legacy_field_error": legacy_field_error,
        })
    return {
        "headers": headers,
        "row_count": row_count,
        "references": references,
        "preservation_candidates": preservation_candidates,
    }


def write_prepared_csv(
    source: Path,
    output: Path,
    *,
    preservation: list[dict] | None = None,
    concatenations: list[dict] | None = None,
) -> dict:
    preservation = preservation or []
    concatenations = concatenations or []
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = list(reader.fieldnames or [])
        rows = list(reader)
    if not headers:
        raise ValueError("Source CSV has no header.")

    additions: list[str] = []
    for item in [*preservation, *concatenations]:
        output_field = str(item.get("output_field") or "").strip()
        validate_custom_field_api_name(output_field)
        if len(output_field) > 40:
            raise ValueError(f"Output field API name exceeds 40 characters: {output_field}")
        occupied = {name.casefold() for name in [*headers, *additions]}
        if output_field.casefold() in occupied:
            raise ValueError(f"Output column already exists or is duplicated: {output_field}")
        additions.append(output_field)

    for item in preservation:
        source_field = str(item.get("source_field") or "").strip()
        if source_field not in headers:
            raise ValueError(f"Preservation source column not found: {source_field}")
        for row in rows:
            row[item["output_field"]] = row.get(source_field, "") or ""

    for item in concatenations:
        source_fields = [str(value).strip() for value in item.get("source_fields") or []]
        if not source_fields:
            raise ValueError("A concatenation must contain at least one source field.")
        missing = [field for field in source_fields if field not in headers]
        if missing:
            raise ValueError(f"Concatenation columns not found: {', '.join(missing)}")
        for row_number, row in enumerate(rows, start=2):
            value = json.dumps(
                {field: row.get(field, "") or "" for field in source_fields},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if len(value.encode("utf-16-le", errors="surrogatepass")) // 2 > 131_072:
                raise ValueError(
                    f"JSON value for {item['output_field']} exceeds the Salesforce "
                    "Long Text Area limit at source row "
                    f"{row_number}."
                )
            row[item["output_field"]] = value

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*headers, *additions])
        writer.writeheader()
        writer.writerows(rows)
    return {
        "source_rows": len(rows),
        "source_columns": len(headers),
        "columns_added": additions,
        "output_columns": len(headers) + len(additions),
    }


def apply_fallbacks(
    source: Path,
    output: Path,
    rules: list[dict],
    inventories: dict[str, set[str]],
) -> dict:
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = list(reader.fieldnames or [])
        rows = list(reader)
    if not headers:
        raise ValueError("Source CSV has no header.")
    changed = Counter()
    audit = []
    for rule in rules:
        column = str(rule.get("column") or "").strip()
        referenced_object = str(rule.get("referenced_object") or "").strip()
        fallback_id = str(rule.get("fallback_id") or "").strip()
        preserve_as = str(rule.get("preserve_as") or "").strip()
        if column not in headers:
            raise ValueError(f"Fallback column not found: {column}")
        if not referenced_object or referenced_object not in inventories:
            raise ValueError(f"No confirmed inventory was supplied for {referenced_object or column}")
        if not fallback_id:
            raise ValueError(f"Fallback ID is required for {column}")
        existing = inventories[referenced_object]
        existing_keys = set().union(*(identity_variants(item) for item in existing))
        if not (identity_variants(fallback_id) & existing_keys):
            raise ValueError(
                f"Fallback ID {fallback_id!r} was not found in the confirmed "
                f"{referenced_object} inventory."
            )
        if preserve_as and preserve_as not in headers:
            headers.append(preserve_as)
        for row_number, row in enumerate(rows, start=2):
            original = str(row.get(column) or "").strip()
            if preserve_as:
                row[preserve_as] = row.get(column, "") or ""
            if original and not (identity_variants(original) & existing_keys):
                row[column] = fallback_id
                changed[column] += 1
                audit.append({"source_row": row_number, "column": column, "referenced_object": referenced_object, "original_value": original, "fallback_id": fallback_id})
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader(); writer.writerows(rows)
    audit_path = output.with_suffix(".fallback_audit.json")
    audit_path.write_text(json.dumps(audit, indent=2), encoding="utf-8")
    return {"source_rows": len(rows), "output_rows": len(rows), "replacements": sum(changed.values()), "by_column": dict(changed), "audit_file": audit_path.name}


def create_custom_field(connector, object_name: str, spec: dict) -> dict:
    api_name = str(spec.get("api_name") or "").strip()
    field_type = str(spec.get("type") or "Text").strip()
    if not API_NAME_RE.fullmatch(object_name):
        raise ValueError("Invalid object API name.")
    validate_custom_field_api_name(api_name)
    existing = {str(field.get("name") or "").casefold(): field for field in connector.describe_object(object_name).get("fields", [])}
    is_formula_checkbox = field_type.casefold() == "formulacheckbox"
    formula = str(spec.get("formula") or "").strip()
    if is_formula_checkbox and not formula:
        raise ValueError(f"Formula is required for {api_name}.")
    if api_name.casefold() in existing:
        current = existing[api_name.casefold()]
        actual_type = str(current.get("type") or "").casefold()
        expected_types = {"longtextarea": {"textarea"}, "text": {"string"}, "formulacheckbox": {"boolean"}}
        compatible = actual_type in expected_types.get(field_type.casefold(), {field_type.casefold()})
        requested_length = 0 if is_formula_checkbox else int(spec.get("length") or (131072 if field_type == "LongTextArea" else 255))
        actual_length = int(current.get("length") or 0)
        formula_matches = not is_formula_checkbox or (
            bool(current.get("calculated"))
            and re.sub(r"\s+", "", str(current.get("calculatedFormula") or "")).casefold()
            == re.sub(r"\s+", "", formula).casefold()
        )
        if not compatible or actual_length < requested_length or not formula_matches:
            raise ValueError(
                f"Existing field {object_name}.{api_name} is incompatible: "
                f"type={current.get('type')}, length={actual_length}; expected "
                f"{field_type}{f'({requested_length})' if requested_length else ''}."
            )
        return {"field": api_name, "created": False, "reason": "already_exists_compatible"}
    metadata = {"label": str(spec.get("label") or api_name[:-3]).replace("_", " "), "type": "Checkbox" if is_formula_checkbox else field_type, "description": str(spec.get("description") or DEFAULT_FIELD_DESCRIPTION)}
    if is_formula_checkbox:
        metadata["formula"] = formula
    elif field_type == "LongTextArea":
        metadata.update({"length": int(spec.get("length") or 131072), "visibleLines": int(spec.get("visible_lines") or 5)})
    else:
        metadata["length"] = int(spec.get("length") or 255)
    url = f"{connector.instance_url}/services/data/{connector.api_version}/tooling/sobjects/CustomField"
    try:
        response = connector._request("POST", url, json={"FullName": f"{object_name}.{api_name}", "Metadata": metadata}).json()
    except Exception as exc:
        # A newly-created field can take a short time to appear in an object
        # describe.  A retry during that window is still successful/idempotent:
        # Salesforce itself confirms the exact developer name already exists.
        error_text = str(exc)
        if "DUPLICATE_DEVELOPER_NAME" in error_text and api_name.removesuffix("__c") in error_text:
            connector._object_describe_cache.pop(object_name, None)
            return {"field": api_name, "created": False, "reason": "already_exists_salesforce_confirmed"}
        raise
    connector._object_describe_cache.pop(object_name, None)
    return {"field": api_name, "created": True, "id": response.get("id")}


def resolve_permission_parents(
    connector,
    permission_sets: list[str],
    profiles: list[str],
) -> list[dict]:
    """
    Resolve Permission Set / Profile names to FieldPermissions ParentIds.

    Profiles are granted through their profile-owned PermissionSet row.
    """
    parents: list[dict] = []
    for name in permission_sets:
        safe = str(name).replace("'", "\\'")
        rows = connector.query(
            f"SELECT Id, Name FROM PermissionSet WHERE Name = '{safe}' AND IsOwnedByProfile = false"
        )
        if not rows:
            raise ValueError(f"Permission Set not found: {name}")
        parents.append({
            "parent_id": rows[0]["Id"],
            "label": f"PermissionSet:{name}",
            "kind": "permission_set",
            "name": str(name),
            "profile_owned": False,
        })
    for name in profiles:
        safe = str(name).replace("'", "\\'")
        rows = connector.query(f"SELECT Id, Name FROM Profile WHERE Name = '{safe}'")
        if not rows:
            raise ValueError(f"Profile not found: {name}")
        profile_id = rows[0]["Id"]
        owned = connector.query(f"SELECT Id FROM PermissionSet WHERE ProfileId = '{profile_id}'")
        if not owned:
            raise ValueError(f"Profile-owned PermissionSet not found: {name}")
        parents.append({
            "parent_id": owned[0]["Id"],
            "label": f"Profile:{name}",
            "kind": "profile",
            "name": str(name),
            "profile_id": profile_id,
            "profile_owned": True,
        })
    return parents


def grant_field_permissions(
    connector,
    object_name: str,
    api_name: str,
    permission_sets: list[str],
    profiles: list[str],
    *,
    editable: bool = True,
) -> list[dict]:
    parents = resolve_permission_parents(connector, permission_sets, profiles)
    results = []
    url = f"{connector.instance_url}/services/data/{connector.api_version}/sobjects/FieldPermissions"
    field_name = f"{object_name}.{api_name}"
    for parent in parents:
        parent_id = parent["parent_id"]
        label = parent["label"]
        existing = connector.query(
            f"SELECT Id FROM FieldPermissions WHERE ParentId = '{parent_id}' "
            f"AND SobjectType = '{object_name}' AND Field = '{field_name}'"
        )
        if existing:
            results.append({"principal": label, "created": False, "reason": "already_exists"})
            continue
        response = connector._request(
            "POST",
            url,
            json={
                "ParentId": parent_id,
                "SobjectType": object_name,
                "Field": field_name,
                "PermissionsRead": True,
                "PermissionsEdit": bool(editable),
            },
        ).json()
        results.append({
            "principal": label,
            "created": True,
            "id": response.get("id"),
            "readable": True,
            "editable": bool(editable),
        })
    return results


def validate_permission_principals(connector, permission_sets: list[str], profiles: list[str]) -> None:
    """Fail before field creation when a requested access principal is invalid."""
    if not permission_sets and not profiles:
        raise ValueError("Provide at least one Permission Set or Profile for field access.")
    resolve_permission_parents(connector, permission_sets, profiles)


def manifest_payload(operation: str, source: Path, output: Path, details: dict) -> dict:
    return {"created_at": datetime.now(timezone.utc).isoformat(), "status": "COMPLETE", "operation": operation, "source_file": str(source), "output_file": str(output), **details}
