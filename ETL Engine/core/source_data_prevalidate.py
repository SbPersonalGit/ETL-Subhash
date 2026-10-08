"""Bounded SF→SF incoming-data checks used by source prevalidation.

Uses SOQL aggregates (GROUP BY) so every distinct source value is checked
without extracting full row payloads.
"""

from __future__ import annotations

import logging
from typing import Any

from core.flat_file_schema_validator import _record_type_picklist_error

logger = logging.getLogger(__name__)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _nested_value(row: dict, path: str) -> str:
    current: Any = row
    for part in path.split("."):
        if not isinstance(current, dict):
            return ""
        current = current.get(part)
    if current is None:
        return ""
    return _text(current)


def _escape_soql(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _active_picklist_values(field_meta: dict) -> set[str]:
    return {
        _text(item.get("value"))
        for item in (field_meta.get("picklistValues") or [])
        if item.get("active", True) and _text(item.get("value"))
    }


def _query_groups(connector, soql: str) -> list[dict]:
    try:
        return connector.query(soql)
    except Exception as exc:
        logger.warning("Source data prevalidation query failed: %s | %s", soql, exc)
        raise


def _source_record_type_developer_names(connector, object_name: str, query_filter: str = "") -> set[str]:
    where = ""
    normalized = _text(query_filter)
    if normalized:
        if normalized.upper().startswith("WHERE "):
            where = f" {normalized}"
        else:
            where = f" WHERE {normalized}"
    soql = (
        f"SELECT RecordType.DeveloperName developerName, COUNT(Id) recordCount "
        f"FROM {object_name}{where} "
        f"GROUP BY RecordType.DeveloperName"
    )
    rows = _query_groups(connector, soql)
    names = set()
    for row in rows:
        value = _text(row.get("developerName")) or _nested_value(row, "RecordType.DeveloperName")
        if value:
            names.add(value)
    return names


def _source_distinct_field_values(
    connector,
    object_name: str,
    field_name: str,
    query_filter: str = "",
) -> set[str]:
    where = ""
    normalized = _text(query_filter)
    if normalized:
        if normalized.upper().startswith("WHERE "):
            where = f" {normalized}"
        else:
            where = f" WHERE {normalized}"
    soql = (
        f"SELECT {field_name} fieldValue, COUNT(Id) recordCount "
        f"FROM {object_name}{where} "
        f"GROUP BY {field_name}"
    )
    rows = _query_groups(connector, soql)
    values = set()
    for row in rows:
        raw = row.get("fieldValue")
        if raw is None:
            raw = row.get(field_name)
        text = _text(raw)
        if not text:
            continue
        # multipicklist rows may already be semicolon-joined per record;
        # distinct aggregate returns whole cell values.
        for part in text.split(";"):
            part = part.strip()
            if part:
                values.add(part)
    return values


def _source_distinct_pairs(
    connector,
    object_name: str,
    controller_field: str,
    dependent_field: str,
    query_filter: str = "",
) -> set[tuple[str, str]]:
    where = ""
    normalized = _text(query_filter)
    if normalized:
        if normalized.upper().startswith("WHERE "):
            where = f" {normalized}"
        else:
            where = f" WHERE {normalized}"
    soql = (
        f"SELECT {controller_field} controllerValue, {dependent_field} dependentValue, "
        f"COUNT(Id) recordCount "
        f"FROM {object_name}{where} "
        f"GROUP BY {controller_field}, {dependent_field}"
    )
    rows = _query_groups(connector, soql)
    pairs = set()
    for row in rows:
        controller = _text(row.get("controllerValue") if row.get("controllerValue") is not None else row.get(controller_field))
        dependent = _text(row.get("dependentValue") if row.get("dependentValue") is not None else row.get(dependent_field))
        if dependent:
            pairs.add((controller, dependent))
    return pairs


def _target_record_types_by_developer_name(connector, object_name: str) -> dict[str, dict]:
    soql = (
        "SELECT Id, DeveloperName, Name, IsActive "
        f"FROM RecordType WHERE SobjectType = '{_escape_soql(object_name)}' AND IsActive = true"
    )
    rows = connector.query(soql)
    return {
        _text(row.get("DeveloperName")): row
        for row in rows
        if _text(row.get("DeveloperName"))
    }


def _resolve_target_record_type_id(
    source_developer_name: str,
    *,
    source_types_by_id: dict[str, dict],
    target_types_by_dn: dict[str, dict],
    record_type_mappings: dict[str, str],
) -> str | None:
    # Explicit sourceId → targetId mapping wins.
    for source_id, target_id in (record_type_mappings or {}).items():
        source_rt = source_types_by_id.get(_text(source_id)) or {}
        if _text(source_rt.get("DeveloperName")) == source_developer_name:
            return _text(target_id) or None
    target = target_types_by_dn.get(source_developer_name)
    if target:
        return _text(target.get("Id")) or None
    return None


def validate_incoming_source_data(
    *,
    config: dict,
    source,
    target,
    source_fields: dict[str, dict],
    target_fields: dict[str, dict],
) -> list[dict]:
    """Return field_access_issues-style entries for incoming source values."""
    issues: list[dict] = []
    source_object = _text(config.get("source_object") or config.get("name"))
    target_object = _text(config.get("target_object") or source_object)
    query_filter = _text(config.get("query_filter"))
    field_pairs = list(config.get("field_pairs") or [])
    record_type_mappings = {
        _text(source_id): _text(target_id)
        for source_id, target_id in (config.get("record_type_mappings") or {}).items()
        if _text(source_id) and _text(target_id)
    }
    source_to_target = {
        _text(source_field): _text(target_field)
        for source_field, target_field in field_pairs
        if _text(source_field) and _text(target_field)
    }
    target_to_source = {target: source for source, target in source_to_target.items()}

    # --- RecordType DeveloperName coverage ---
    if "RecordTypeId" in source_to_target or "RecordTypeId" in (config.get("source_fields") or []):
        try:
            used_developer_names = _source_record_type_developer_names(source, source_object, query_filter)
            source_types = source.query(
                "SELECT Id, DeveloperName, Name FROM RecordType "
                f"WHERE SobjectType = '{_escape_soql(source_object)}'"
            )
            source_types_by_id = {
                _text(row.get("Id")): row for row in source_types if _text(row.get("Id"))
            }
            target_types_by_dn = _target_record_types_by_developer_name(target, target_object)
            for developer_name in sorted(used_developer_names):
                target_id = _resolve_target_record_type_id(
                    developer_name,
                    source_types_by_id=source_types_by_id,
                    target_types_by_dn=target_types_by_dn,
                    record_type_mappings=record_type_mappings,
                )
                if not target_id:
                    issues.append({
                        "field": "RecordTypeId",
                        "severity": "error",
                        "message": (
                            f"Source RecordType DeveloperName '{developer_name}' is used in "
                            f"'{source_object}' data but has no matching target Record Type "
                            "and no Record Type mapping."
                        ),
                    })
        except Exception as exc:
            issues.append({
                "field": "RecordTypeId",
                "severity": "warning",
                "message": f"Could not validate source RecordType values: {exc}",
            })

    # Prepare Record-Type picklist metadata for dependent checks when possible.
    record_type_picklists: dict[str, dict] = {}
    default_target_rt_id = ""
    try:
        target_describe = target.describe_object(target_object)
        default_target_rt_id = next(
            (
                _text(info.get("recordTypeId"))
                for info in (target_describe.get("recordTypeInfos") or [])
                if info.get("defaultRecordTypeMapping") and info.get("available", True)
            ),
            "",
        )
        target_rt_ids = set(record_type_mappings.values())
        if default_target_rt_id:
            target_rt_ids.add(default_target_rt_id)
        for record_type_id in sorted(target_rt_ids):
            try:
                record_type_picklists[record_type_id] = target.describe_record_type_picklists(
                    target_object, record_type_id
                )
            except Exception as exc:
                issues.append({
                    "field": "RecordTypeId",
                    "severity": "warning",
                    "message": (
                        f"Could not load target Record Type picklists for "
                        f"{record_type_id}: {exc}"
                    ),
                })
    except Exception as exc:
        issues.append({
            "field": target_object,
            "severity": "warning",
            "message": f"Could not prepare target Record Type picklist metadata: {exc}",
        })

    # --- Picklist / multipicklist value checks ---
    for source_field, target_field in field_pairs:
        source_field = _text(source_field)
        target_field = _text(target_field)
        if not source_field or not target_field or target_field == "RecordTypeId":
            continue
        target_meta = target_fields.get(target_field) or {}
        field_type = _text(target_meta.get("type")).lower()
        if field_type not in {"picklist", "multipicklist"}:
            continue
        used_values: set[str] = set()
        try:
            used_values = _source_distinct_field_values(
                source, source_object, source_field, query_filter
            )
        except Exception as exc:
            issues.append({
                "field": target_field,
                "severity": "warning",
                "message": f"Could not query source values for {source_object}.{source_field}: {exc}",
            })

        allowed = _active_picklist_values(target_meta)
        if used_values and allowed:
            invalid = sorted(value for value in used_values if value not in allowed)
            if invalid:
                preview = ", ".join(invalid[:12])
                more = f" (+{len(invalid) - 12} more)" if len(invalid) > 12 else ""
                issues.append({
                    "field": target_field,
                    "severity": "error",
                    "message": (
                        f"Source values for {source_object}.{source_field} are not active on target "
                        f"{target_object}.{target_field}: {preview}{more}"
                    ),
                })

        # Dependent picklist combinations (controller + dependent).
        if not target_meta.get("dependentPicklist"):
            continue
        controller = _text(target_meta.get("controllerName"))
        if not controller:
            continue
        controller_source = target_to_source.get(controller)
        if not controller_source:
            issues.append({
                "field": target_field,
                "severity": "error",
                "message": (
                    f"Dependent picklist {target_object}.{target_field} requires mapped "
                    f"controlling field {controller}."
                ),
            })
            continue
        try:
            pairs = _source_distinct_pairs(
                source, source_object, controller_source, source_field, query_filter
            )
        except Exception as exc:
            issues.append({
                "field": target_field,
                "severity": "warning",
                "message": (
                    f"Could not query controlling/dependent pairs for "
                    f"{controller_source}/{source_field}: {exc}"
                ),
            })
            continue

        # Prefer Record-Type aware metadata when available; otherwise fall back
        # to global target picklist values already checked above.
        rt_id = default_target_rt_id or next(iter(record_type_picklists), "")
        rt_fields = record_type_picklists.get(rt_id) or {}
        if not rt_fields:
            continue
        bad_pairs = []
        for controller_value, dependent_value in sorted(pairs):
            message = _record_type_picklist_error(
                dependent_value,
                target_field,
                rt_id,
                rt_fields,
                controller_value=controller_value,
                field_type=field_type,
            )
            if message:
                bad_pairs.append((controller_value, dependent_value, message))
        if bad_pairs:
            preview_items = [
                f"{controller}='{controller_value or '(blank)'}' + {target_field}='{dependent_value}'"
                for controller_value, dependent_value, _message in bad_pairs[:8]
            ]
            more = f" (+{len(bad_pairs) - 8} more)" if len(bad_pairs) > 8 else ""
            issues.append({
                "field": target_field,
                "severity": "error",
                "message": (
                    f"Invalid controlling/dependent combinations in source data: "
                    f"{'; '.join(preview_items)}{more}"
                ),
            })

    return issues
