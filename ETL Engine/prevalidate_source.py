"""Metadata-only pre-validation for Source Org templates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from core.connector import connector_from_env
from core.source_data_prevalidate import validate_incoming_source_data

ROOT = Path(__file__).resolve().parent

# Salesforce types considered compatible for source→target mapping checks.
_TYPE_ALIASES = {
    "string": {"string", "textarea", "url", "phone", "email", "id", "reference", "combobox", "encryptedstring"},
    "textarea": {"string", "textarea", "url", "phone", "email"},
    "picklist": {"picklist", "string", "textarea", "combobox"},
    "multipicklist": {"multipicklist", "string", "textarea"},
    "boolean": {"boolean"},
    "int": {"int", "double", "currency", "percent", "long"},
    "double": {"int", "double", "currency", "percent", "long"},
    "currency": {"int", "double", "currency", "percent"},
    "percent": {"int", "double", "currency", "percent"},
    "date": {"date", "datetime"},
    "datetime": {"date", "datetime"},
    "time": {"time"},
    "reference": {"reference", "id", "string"},
    "id": {"id", "reference", "string"},
}


def _log(message: str) -> None:
    print(message, flush=True)


def _normalize_operation(value: str | None, allowed: list | None = None) -> str:
    operation = str(value or "").strip().lower()
    if operation in {"insert", "update", "upsert"}:
        return operation
    for item in allowed or []:
        candidate = str(item or "").strip().lower()
        if candidate in {"insert", "update", "upsert"}:
            return candidate
    return "upsert"


def _types_compatible(source_type: str, target_type: str) -> bool:
    source = str(source_type or "").strip().lower()
    target = str(target_type or "").strip().lower()
    if not source or not target:
        return True
    if source == target:
        return True
    return target in _TYPE_ALIASES.get(source, {source})


def _selected_objects(template: dict, requested: set[str]) -> list[dict]:
    kind = str(template.get("template_kind") or "").strip().lower()
    if kind in {"source_mapping", "source_cycle_group"}:
        by_object: dict[str, dict] = {}
        raw = template.get("template") if isinstance(template.get("template"), dict) else {}
        mappings = []
        if isinstance(raw.get("objects"), list) and raw.get("objects"):
            mappings = [item for item in raw["objects"] if isinstance(item, dict)]
        elif raw.get("source_object") or raw.get("target_object"):
            mappings = [raw]
        for mapping in mappings:
            source_object = str(mapping.get("source_object") or mapping.get("target_object") or "").strip()
            target_object = str(mapping.get("target_object") or mapping.get("source_object") or "").strip()
            object_name = source_object or target_object
            if not object_name or (requested and object_name not in requested and target_object not in requested):
                continue
            field_pairs: list[tuple[str, str]] = []
            source_fields = {"Id"}
            target_fields: set[str] = set()
            for field_mapping in mapping.get("field_mappings") or []:
                if not isinstance(field_mapping, dict):
                    continue
                if field_mapping.get("include") is False and not field_mapping.get("target_field"):
                    continue
                source_field = str(field_mapping.get("source_field") or "").strip()
                target_field = str(field_mapping.get("target_field") or "").strip()
                if source_field:
                    source_fields.add(source_field)
                if target_field:
                    target_fields.add(target_field)
                if source_field and target_field:
                    field_pairs.append((source_field, target_field))
                elif source_field:
                    # Same-name mapping assumed when target omitted.
                    target_fields.add(source_field)
                    field_pairs.append((source_field, source_field))

            match = mapping.get("match_mapping") if isinstance(mapping.get("match_mapping"), dict) else {}
            match_source = str((match or {}).get("source_field") or "").strip()
            match_target = str(
                (match or {}).get("target_field")
                or mapping.get("target_external_id")
                or ""
            ).strip()
            if match_source:
                source_fields.add(match_source)
            if match_target:
                target_fields.add(match_target)

            operation = _normalize_operation(
                mapping.get("default_operation"),
                mapping.get("allowed_operations") if isinstance(mapping.get("allowed_operations"), list) else None,
            )
            key = f"{source_object}|{target_object}|{operation}"
            entry = by_object.setdefault(key, {
                "name": object_name,
                "source_object": source_object or object_name,
                "target_object": target_object or object_name,
                "source_fields": set(),
                "target_fields": set(),
                "field_pairs": [],
                "operation": operation,
                "external_id": "",
                "match_source": "",
                "match_target": "",
                "query_filter": str(mapping.get("query_filter") or "").strip(),
                "record_type_mappings": {},
            })
            entry["source_fields"].update(source_fields)
            entry["target_fields"].update(target_fields)
            entry["field_pairs"].extend(field_pairs)
            if match_source and not entry["match_source"]:
                entry["match_source"] = match_source
            if match_target and not entry["match_target"]:
                entry["match_target"] = match_target
                entry["external_id"] = match_target
            if not entry.get("query_filter"):
                entry["query_filter"] = str(mapping.get("query_filter") or "").strip()
            rt_maps = mapping.get("record_type_mappings")
            if isinstance(rt_maps, dict):
                entry["record_type_mappings"].update({
                    str(source_id).strip(): str(target_id).strip()
                    for source_id, target_id in rt_maps.items()
                    if str(source_id).strip() and str(target_id).strip()
                })
        return [
            {
                "name": config["name"],
                "source_object": config["source_object"],
                "target_object": config["target_object"],
                "source_fields": sorted(config["source_fields"]),
                "target_fields": sorted(config["target_fields"]),
                # Compatibility alias consumed by the shared target metadata
                # validator and older callers. These are deliberately target
                # fields, never source API names.
                "fields": sorted(config["target_fields"]),
                "field_pairs": sorted(set(config["field_pairs"])),
                "operation": config["operation"],
                "external_id": config["external_id"],
                "match_source": config["match_source"],
                "match_target": config["match_target"],
                "query_filter": config.get("query_filter") or "",
                "record_type_mappings": dict(config.get("record_type_mappings") or {}),
            }
            for _, config in sorted(by_object.items())
        ]

    workspace = template.get("template") or {}
    nodes = workspace.get("nodes") or []
    node_selections = workspace.get("nodeSelections") or {}
    by_object: dict[str, dict] = {}
    for node in nodes:
        object_name = str(node.get("object") or "").strip()
        if not object_name or (requested and object_name not in requested):
            continue
        selection = node_selections.get(str(node.get("id") or "")) or {}
        entry = by_object.setdefault(object_name, {
            "name": object_name,
            "source_object": object_name,
            "target_object": object_name,
            "source_fields": set(),
            "target_fields": set(),
            "field_pairs": [],
            "operation": "upsert",
            "external_id": "",
            "match_source": "",
            "match_target": "",
        })
        for field_name in selection.get("selectedFields") or []:
            name = str(field_name).strip()
            if not name:
                continue
            entry["source_fields"].add(name)
            entry["target_fields"].add(name)
            if name != "Id":
                entry["field_pairs"].append((name, name))
        external_id = str(selection.get("externalId") or "").strip()
        if external_id and not entry["external_id"]:
            entry["external_id"] = external_id
            entry["match_target"] = external_id
            entry["target_fields"].add(external_id)
    return [
        {
            "name": object_name,
            "source_object": config["source_object"],
            "target_object": config["target_object"],
            "source_fields": sorted(config["source_fields"]),
            "target_fields": sorted(config["target_fields"]),
            "field_pairs": sorted(set(config["field_pairs"])),
            "operation": config["operation"],
            "external_id": config["external_id"],
            "match_source": config["match_source"],
            "match_target": config["match_target"],
        }
        for object_name, config in sorted(by_object.items())
    ]


def _object_catalog(connector) -> dict[str, dict]:
    return {
        str(item.get("name") or ""): item
        for item in connector.describe_global()
        if item.get("name")
    }


def _describe_fields(connector, object_name: str) -> dict[str, dict]:
    describe = connector.describe_object(object_name)
    return {
        str(field.get("name") or ""): field
        for field in describe.get("fields", [])
        if field.get("name")
    }


def _validate_object_config(
    config: dict,
    source_catalog: dict[str, dict],
    target_catalog: dict[str, dict],
    source,
    target,
) -> dict:
    source_object = config["source_object"]
    target_object = config["target_object"]
    operation = config["operation"]
    item = {
        "file": source_object,
        "object": source_object,
        "target_object": target_object,
        "operation": operation,
        "errors": [],
        "field_access_issues": [],
        "file_validation": {"clean_count": 0, "rejected_count": 0, "normalized_count": 0},
        "target_checks": [],
    }

    source_meta = source_catalog.get(source_object) or {}
    target_meta = target_catalog.get(target_object) or {}
    if not source_meta:
        item["errors"].append(f"Object '{source_object}' does not exist on source org")
    elif not source_meta.get("queryable", False):
        item["errors"].append(f"Object '{source_object}' is not queryable on source org")
    if not target_meta:
        item["errors"].append(f"Object '{target_object}' does not exist on target org")
    elif not target_meta.get("createable", False) and operation in {"insert", "upsert"}:
        item["errors"].append(f"Object '{target_object}' is not createable on target org")
    elif not target_meta.get("updateable", False) and operation in {"update", "upsert"}:
        item["field_access_issues"].append({
            "field": target_object,
            "severity": "warning",
            "message": f"Object '{target_object}' may not be updateable on target org for operation '{operation}'",
        })

    source_fields: dict[str, dict] = {}
    target_fields: dict[str, dict] = {}
    if source_meta:
        try:
            source_fields = _describe_fields(source, source_object)
        except Exception as exc:
            item["errors"].append(f"Could not describe '{source_object}' on source org: {exc}")
    if target_meta:
        try:
            target_fields = _describe_fields(target, target_object)
        except Exception as exc:
            item["errors"].append(f"Could not describe '{target_object}' on target org: {exc}")

    for field_name in config.get("source_fields") or []:
        if field_name == "Id":
            continue
        if field_name not in source_fields:
            item["errors"].append(f"Source field '{field_name}' does not exist on source '{source_object}'")

    for field_name in config.get("target_fields") or []:
        if field_name == "Id":
            continue
        if field_name not in target_fields:
            item["errors"].append(f"Target field '{field_name}' does not exist on target '{target_object}'")
            continue
        fmeta = target_fields[field_name]
        if not fmeta.get("createable", False) and not fmeta.get("updateable", False):
            item["field_access_issues"].append({
                "field": field_name,
                "severity": "warning",
                "message": (
                    f"Field '{field_name}' is not createable/updateable on target "
                    f"'{target_object}' — will be skipped by Salesforce"
                ),
            })

    for source_field, target_field in config.get("field_pairs") or []:
        if source_field == "Id" or target_field == "Id":
            continue
        source_meta_field = source_fields.get(source_field) or {}
        target_meta_field = target_fields.get(target_field) or {}
        if not source_meta_field or not target_meta_field:
            continue
        if not _types_compatible(source_meta_field.get("type"), target_meta_field.get("type")):
            item["errors"].append(
                f"Incompatible types for {source_object}.{source_field} "
                f"({source_meta_field.get('type')}) → {target_object}.{target_field} "
                f"({target_meta_field.get('type')})"
            )

    # Match / external ID rules depend on operation.
    external_id = str(config.get("external_id") or config.get("match_target") or "").strip()
    match_source = str(config.get("match_source") or "").strip()
    if operation in {"upsert", "update"}:
        if not external_id:
            item["errors"].append(
                f"No target match/external ID field configured for operation '{operation}' "
                f"on '{target_object}'"
            )
        elif external_id not in target_fields:
            item["errors"].append(
                f"Match/external ID field '{external_id}' does not exist on target '{target_object}'"
            )
        else:
            fmeta = target_fields[external_id]
            if operation == "upsert" and not fmeta.get("externalId", False):
                item["field_access_issues"].append({
                    "field": external_id,
                    "severity": "warning",
                    "message": (
                        f"'{external_id}' is not flagged External ID on target '{target_object}' — "
                        "loader will use the match-by load path (insert+update split)."
                    ),
                })
            if not fmeta.get("updateable", False) and not fmeta.get("createable", False):
                item["errors"].append(
                    f"Match/external ID field '{external_id}' is not writeable on target '{target_object}'"
                )
        if match_source and match_source not in source_fields and match_source != "Id":
            item["errors"].append(
                f"Source match field '{match_source}' does not exist on source '{source_object}'"
            )
    elif operation == "insert" and external_id:
        # Optional external id on insert is fine; only warn if missing on target.
        if external_id not in target_fields:
            item["field_access_issues"].append({
                "field": external_id,
                "severity": "warning",
                "message": (
                    f"Configured external ID '{external_id}' does not exist on target "
                    f"'{target_object}' (not required for insert)."
                ),
            })

    # Incoming source-data checks (RecordType / picklist / dependent picklist).
    # Skip when describe failed — metadata issues already block the object.
    if source_fields and target_fields and not any(
        "Could not describe" in error or "does not exist on source org" in error or "does not exist on target org" in error
        for error in item["errors"]
    ):
        data_issues = validate_incoming_source_data(
            config=config,
            source=source,
            target=target,
            source_fields=source_fields,
            target_fields=target_fields,
        )
        item["field_access_issues"].extend(data_issues)

    return item


def run(args) -> int:
    template_path = Path(args.template).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    template = json.loads(template_path.read_text(encoding="utf-8"))
    requested = {str(name).strip() for name in (args.object or []) if str(name).strip()}
    object_configs = _selected_objects(template, requested)

    source_profile = str(template.get("source_profile") or "default").strip() or "default"
    target_profile = str(template.get("target_profile") or "default").strip() or "default"

    source = connector_from_env("SOURCE", profile=source_profile)
    target = connector_from_env("TARGET", profile=target_profile)
    source.connect()
    target.connect()

    _log(
        f"Source pre-validation | template={template.get('template_name') or template.get('template_id') or template_path.stem} "
        f"| source={source_profile} | target={target_profile} | objects={len(object_configs)} | reports={out_dir}"
    )

    source_catalog = _object_catalog(source)
    target_catalog = _object_catalog(target)
    report = {
        "template_id": template.get("template_id") or template_path.stem,
        "template_name": template.get("template_name") or template_path.stem,
        "source_profile": source_profile,
        "target_profile": target_profile,
        "target_aware": True,
        "data_aware": True,
        "files": [],
    }
    failed = False

    for config in object_configs:
        item = _validate_object_config(config, source_catalog, target_catalog, source, target)
        if item["errors"] or any(
            issue.get("severity") == "error" for issue in item["field_access_issues"]
        ):
            failed = True
        report["files"].append(item)
        _log(
            f"  [{config['source_object']}→{config['target_object']}|{config['operation']}] "
            f"errors={len(item['errors'])} field_issues={len(item['field_access_issues'])}"
        )

    if not report["files"]:
        report["files"].append({
            "file": "",
            "object": "",
            "errors": ["No source objects were selected in the template."],
            "field_access_issues": [],
            "file_validation": {"clean_count": 0, "rejected_count": 0, "normalized_count": 0},
            "target_checks": [],
        })
        failed = True

    report["status"] = "ISSUES_FOUND" if failed else "PASSED"
    (out_dir / "prevalidation_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    template["prevalidation_status"] = report["status"]
    template_path.write_text(json.dumps(template, indent=2, ensure_ascii=False), encoding="utf-8")
    _log(f"Source pre-validation complete | status={report['status']}")
    return 1 if failed else 0


def parse_args():
    parser = argparse.ArgumentParser(description="Pre-validate a saved Source Org migration template.")
    parser.add_argument("--template", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--object", action="append", default=[])
    return parser.parse_args()


if __name__ == "__main__":
    load_dotenv(ROOT / ".env", override=True)
    raise SystemExit(run(parse_args()))
