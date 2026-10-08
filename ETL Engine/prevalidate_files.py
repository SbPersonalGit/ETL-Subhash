"""Background, scalable pre-migration validation for saved flat-file templates."""

from __future__ import annotations

import argparse
import copy
import csv
import json
import random
import re
import sqlite3
import shutil
import sys
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv

from core.connector import connector_from_env
from core.flat_file import open_csv, resolve_flat_file_path, safe_name, validate_flat_file_reference
from core.workspace_layout import INPUT_FILES_DIR
from core.id_mapper import IDMapper
from core.flat_file_schema_validator import (
    _record_type_source_column,
    clean_csv_against_schema,
    validate_migration_field_access,
    validate_object_and_record_type_access,
    validate_strict_api_names,
)
from verify_records import bulk_query_records
from knowledge_article.dependency_validator import validate_flat_knowledge_dependencies


ROOT = Path(__file__).resolve().parent

MANDATORY_CHECKS = {
    "csv_structure", "scientific_notation", "migration_key_present",
    "record_type_mapping", "record_type_picklists",
}
OPTIONAL_CHECKS = {
    "duplicate_keys", "required_fields", "restricted_picklists",
    "dependent_picklists", "master_detail_references", "lookup_references",
    "text_limits", "numeric_precision", "salesforce_id_format", "contact_formats",
    "date_formats",
}


def _log(message: str) -> None:
    print(message, flush=True)


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write_progress(out_dir: Path, payload: dict) -> None:
    path = out_dir / "prevalidation_progress.json"
    body = {
        **payload,
        "updated_at": _utc_now(),
    }
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")


def _copy_with_retry(source: Path, destination: Path, attempts: int = 5) -> None:
    """Copy artifacts with retries for transient Windows/OneDrive locks."""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            shutil.copy2(source, destination)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.35 * (attempt + 1))
        except OSError as exc:
            last_error = exc
            # OneDrive sync can raise sharing violations as WinError 32/33.
            if getattr(exc, "winerror", None) not in {32, 33} and attempt == attempts - 1:
                raise
            time.sleep(0.35 * (attempt + 1))
    if last_error:
        raise last_error


def _progress_message(payload: dict) -> str:
    total = int(payload.get("total_files") or 0)
    index = int(payload.get("current_index") or 0)
    filename = str(payload.get("current_file") or "").strip() or "file"
    phase = str(payload.get("phase") or "").strip()
    rows = payload.get("rows_processed")
    total_rows = payload.get("total_rows")
    clean = payload.get("clean_count")
    rejected = payload.get("rejected_count")
    prefix = f"Validating file {index} of {total}: {filename}" if total else f"Validating {filename}"
    if phase == "duplicate_scan":
        detail = f"scanning keys ({int(rows or 0):,} rows)" if rows else "scanning keys"
        return f"{prefix} — {detail}"
    if phase == "schema_clean":
        if total_rows:
            detail = (
                f"schema cleaning {int(rows or 0):,}/{int(total_rows):,} rows"
                f" (clean={int(clean or 0):,}, rejected={int(rejected or 0):,})"
            )
        else:
            detail = f"schema cleaning ({int(rows or 0):,} rows)"
        return f"{prefix} — {detail}"
    if phase == "target_checks":
        return f"{prefix} — checking target references"
    if phase == "starting":
        return f"{prefix} — starting"
    if phase == "completed":
        completed = int(payload.get("completed_files") or 0)
        return f"Completed {completed} of {total} file(s)" if total else "Pre-validation completed"
    return prefix


def _soql_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _record_type_validation_context(
    connector, object_name: str, describe: dict, file_config: dict,
    source_path: Path, row_limit: int | None = None,
):
    """Resolve incoming DeveloperNames and load target Record-Type picklist metadata."""
    effective_config = copy.deepcopy(file_config)
    available_ids = {
        str(info.get("recordTypeId") or "").strip()
        for info in describe.get("recordTypeInfos") or []
        if info.get("available", False) and str(info.get("recordTypeId") or "").strip()
    }
    default_record_type_id = next(
        (
            str(info.get("recordTypeId") or "").strip()
            for info in describe.get("recordTypeInfos") or []
            if info.get("defaultRecordTypeMapping")
            and info.get("available", False)
            and str(info.get("recordTypeId") or "").strip()
        ),
        "",
    )
    record_type_source = _record_type_source_column(effective_config)
    if not record_type_source:
        picklists = {
            default_record_type_id: connector.describe_record_type_picklists(
                object_name, default_record_type_id
            )
        } if default_record_type_id else {}
        return effective_config, picklists, default_record_type_id, available_ids

    source_values = set()
    with open_csv(source_path) as handle:
        for row_index, row in enumerate(csv.DictReader(handle), start=1):
            if row_limit is not None and row_index > row_limit:
                break
            value = str(row.get(record_type_source) or "").strip()
            if value:
                source_values.add(value)

    escaped_object = object_name.replace("\\", "\\\\").replace("'", "\\'")
    target_record_types = connector.query(
        "SELECT Id, DeveloperName FROM RecordType "
        f"WHERE SobjectType = '{escaped_object}' AND IsActive = true"
    )
    by_identifier = {}
    for record_type in target_record_types:
        record_type_id = str(record_type.get("Id") or "").strip()
        developer_name = str(record_type.get("DeveloperName") or "").strip()
        if record_type_id not in available_ids:
            continue
        by_identifier[record_type_id.casefold()] = record_type_id
        if developer_name:
            by_identifier[developer_name.casefold()] = record_type_id

    mappings = {
        str(source).strip(): str(target).strip()
        for source, target in (effective_config.get("record_type_mappings") or {}).items()
        if str(source).strip() and str(target).strip()
    }
    for source_value in source_values:
        if source_value not in mappings:
            matched_id = by_identifier.get(source_value.casefold())
            if matched_id:
                mappings[source_value] = matched_id
    effective_config["record_type_mappings"] = mappings

    used_ids = {
        target_id for target_id in mappings.values() if target_id in available_ids
    }
    if default_record_type_id:
        used_ids.add(default_record_type_id)
    picklists = {
        record_type_id: connector.describe_record_type_picklists(object_name, record_type_id)
        for record_type_id in sorted(used_ids)
    }
    return effective_config, picklists, default_record_type_id, available_ids


def _sample_values(
    path: Path, columns: list[str], size: int | None, seed: int,
    row_limit: int | None = None,
) -> dict[str, list[str]]:
    rng = random.Random(seed)
    seen = {column: 0 for column in columns}
    values = {column: [] for column in columns}
    with open_csv(path) as handle:
        for row_index, row in enumerate(csv.DictReader(handle), start=1):
            if row_limit is not None and row_index > row_limit:
                break
            for column in columns:
                value = str(row.get(column) or "").strip()
                if not value:
                    continue
                seen[column] += 1
                bucket = values[column]
                if size is None:
                    bucket.append(value)
                elif len(bucket) < size:
                    bucket.append(value)
                else:
                    position = rng.randrange(seen[column])
                    if position < size:
                        bucket[position] = value
    return {column: list(dict.fromkeys(bucket)) for column, bucket in values.items()}


def _lookup_field_for_mapping(config: dict, mapping: dict) -> str:
    """Resolve the target key used by a lookup prevalidation check."""
    referenced = str(mapping.get("referenced_object") or "").strip()
    lookup_resolution = str(mapping.get("lookup_resolution") or "").strip()
    if lookup_resolution == "pipeline_object" and referenced == str(config.get("object") or "").strip():
        return str(config.get("external_id") or "Id").strip() or "Id"
    if lookup_resolution in {"polymorphic_external_id", "polymorphic_target_first"}:
        matching_rules = [
            rule for rule in mapping.get("polymorphic_mappings") or []
            if str(rule.get("referenced_object") or "").strip() == referenced
        ]
        if len(matching_rules) == 1:
            return str(matching_rules[0].get("external_id") or "Id").strip() or "Id"
    return str(mapping.get("lookup_external_id") or "Id").strip() or "Id"


def _in_pipeline_key_source(config: dict, mapping: dict) -> str:
    """Return the source column that supplies an in-file self-lookup identity."""
    referenced = str(mapping.get("referenced_object") or "").strip()
    if (
        str(mapping.get("lookup_resolution") or "").strip() != "pipeline_object"
        or referenced != str(config.get("object") or "").strip()
    ):
        return ""
    external_id = str(config.get("external_id") or "").strip()
    if not external_id:
        return ""
    for candidate in config.get("mappings") or []:
        if candidate.get("ignored"):
            continue
        if str(candidate.get("target") or "").strip() == external_id:
            return str(candidate.get("source") or "").strip()
    return ""


def _target_checks(
    connector, source_path: Path, config: dict, sample_size: int | None,
    seed: int, query_mode: str, row_limit: int | None = None,
    described_fields: list[dict] | None = None,
    enabled_checks: set[str] | None = None,
) -> list[dict]:
    checks = []
    mappings = []
    field_meta = {str(field.get("name") or "").casefold(): field for field in (described_fields or [])}
    for mapping in config.get("mappings") or []:
        if mapping.get("ignored"):
            continue
        source = str(mapping.get("source") or "").strip()
        target = str(mapping.get("target") or "").strip()
        referenced = str(mapping.get("referenced_object") or "").strip()
        if not source or not referenced:
            continue
        meta = field_meta.get(target.casefold(), {})
        is_master_detail = meta.get("relationshipOrder") is not None
        check_id = "master_detail_references" if is_master_detail else "lookup_references"
        if enabled_checks is not None and check_id not in enabled_checks:
            continue
        lookup_field = _lookup_field_for_mapping(config, mapping)
        local_key_source = _in_pipeline_key_source(config, mapping)
        mappings.append((source, referenced, lookup_field, target, local_key_source))
    sampled = _sample_values(
        source_path, [item[0] for item in mappings], sample_size, seed,
        row_limit=row_limit,
    ) if sample_size is not None else {}
    scope_label = "full scan" if sample_size is None else f"sample up to {sample_size:,} value(s) per field"
    _log(f"  [target] Preparing {len(mappings)} lookup check(s) | scope={scope_label} | retrieval={query_mode}")
    for source, referenced, lookup_field, target, local_key_source in mappings:
        check_started = time.perf_counter()
        _log(f"  [target] {source} -> {referenced}.{lookup_field} | collecting distinct source values")
        if sample_size is None:
            temp = tempfile.NamedTemporaryFile(prefix="sf_etl_refs_", suffix=".sqlite3", delete=False)
            temp.close()
            db = sqlite3.connect(temp.name)
            db.execute("CREATE TABLE missing(value TEXT PRIMARY KEY)")
            with open_csv(source_path) as handle:
                pending = []
                for row in csv.DictReader(handle):
                    value = str(row.get(source) or "").strip()
                    if value:
                        pending.append((value,))
                    if len(pending) >= 10_000:
                        db.executemany("INSERT OR IGNORE INTO missing(value) VALUES (?)", pending); db.commit(); pending.clear()
                if pending:
                    db.executemany("INSERT OR IGNORE INTO missing(value) VALUES (?)", pending); db.commit()
            checked = db.execute("SELECT COUNT(*) FROM missing").fetchone()[0]
            if local_key_source:
                with open_csv(source_path) as handle:
                    for row_index, row in enumerate(csv.DictReader(handle), start=1):
                        if row_limit is not None and row_index > row_limit:
                            break
                        local_value = str(row.get(local_key_source) or "").strip()
                        if local_value:
                            db.execute("DELETE FROM missing WHERE value = ?", (local_value,))
                db.commit()
            soql = f"SELECT {lookup_field} FROM {referenced} WHERE {lookup_field} != null"
            for row in bulk_query_records(connector, soql, referenced):
                value = row.get(lookup_field)
                if value is not None:
                    db.execute("DELETE FROM missing WHERE value = ?", (str(value),))
            db.commit()
            missing_count = db.execute("SELECT COUNT(*) FROM missing").fetchone()[0]
            examples = [row[0] for row in db.execute("SELECT value FROM missing LIMIT 100").fetchall()]
            missing_values = [row[0] for row in db.execute("SELECT value FROM missing").fetchall()]
            db.close(); Path(temp.name).unlink(missing_ok=True)
            checks.append({"source": source, "target": target, "referenced_object": referenced, "lookup_field": lookup_field, "checked": checked, "found": checked - missing_count, "missing_count": missing_count, "missing_examples": examples, "_missing_values": missing_values})
            _log(f"  [target] {source}: checked={checked:,} found={checked - missing_count:,} missing={missing_count:,} | API=Bulk 2.0 | {time.perf_counter() - check_started:.2f}s")
            continue
        requested = sampled.get(source, [])
        requested_set = set(requested)
        found = set()
        if local_key_source:
            local_values = _sample_values(
                source_path, [local_key_source], None, seed, row_limit=row_limit,
            ).get(local_key_source, [])
            found.update(requested_set & set(local_values))
        use_bulk = query_mode == "bulk" or (query_mode == "auto" and sample_size is None)
        query_groups = [requested[offset:offset + 200] for offset in range(0, len(requested), 200)]
        if use_bulk:
            query_groups = [None]
        for chunk in query_groups:
            soql = f"SELECT {lookup_field} FROM {referenced} WHERE {lookup_field} != null" if chunk is None else f"SELECT {lookup_field} FROM {referenced} WHERE {lookup_field} IN ({','.join(_soql_literal(value) for value in chunk)})"
            rows = bulk_query_records(connector, soql, referenced) if use_bulk else connector.query(soql)
            for row in rows:
                value = row.get(lookup_field)
                if value is not None and str(value) in requested_set:
                    found.add(str(value))
        missing = [value for value in requested if value not in found]
        checks.append({
            "source": source, "target": target, "referenced_object": referenced,
            "lookup_field": lookup_field, "checked": len(requested),
            "found": len(found), "missing_count": len(missing), "missing_examples": missing[:100],
            "_missing_values": missing,
        })
        api_label = "Bulk 2.0" if use_bulk else "REST/SOQL chunks"
        _log(f"  [target] {source}: checked={len(requested):,} found={len(found):,} missing={len(missing):,} | API={api_label} | {time.perf_counter() - check_started:.2f}s")
    return checks


def _merge_reference_failures(
    source_path: Path, clean_path: Path, rejected_path: Path,
    config: dict, checks: list[dict], row_limit: int | None = None,
) -> int:
    """Move rows with unresolved references from clean to rejected output."""
    failures = {
        str(check.get("source") or ""): (
            check,
            set(str(value) for value in check.get("_missing_values") or []),
        )
        for check in checks
        if check.get("source") and check.get("_missing_values")
    }
    if not failures:
        return 0

    existing_errors: dict[int, str] = {}
    if rejected_path.exists():
        with open_csv(rejected_path) as handle:
            for row in csv.DictReader(handle):
                try:
                    existing_errors[int(row.get("__source_row") or 0)] = str(
                        row.get("__validation_errors") or ""
                    )
                except ValueError:
                    continue

    clean_rows = []
    if clean_path.exists():
        with open_csv(clean_path) as handle:
            clean_rows = list(csv.DictReader(handle))
    clean_index = 0

    active = [
        mapping for mapping in config.get("mappings") or []
        if not mapping.get("ignored") and mapping.get("source") and mapping.get("target")
    ]
    with open_csv(source_path) as handle:
        reader = csv.DictReader(handle)
        source_headers = list(reader.fieldnames or [])
        rejected_headers = [str(mapping["target"]) for mapping in active]
        rows = []
        for row_number, row in enumerate(reader, start=2):
            if row_limit is not None and row_number - 1 > row_limit:
                break
            errors = [existing_errors[row_number]] if existing_errors.get(row_number) else []
            output_row = row
            if row_number not in existing_errors and clean_index < len(clean_rows):
                output_row = clean_rows[clean_index]
                clean_index += 1
            for source, (check, missing) in failures.items():
                value = str(row.get(source) or "").strip()
                if value in missing:
                    errors.append(
                        f"{check.get('target') or source}: reference '{value}' not found in "
                        f"{check.get('referenced_object')}.{check.get('lookup_field')}"
                    )
            rows.append((row_number, output_row, errors))

    clean_tmp = clean_path.with_suffix(clean_path.suffix + ".references.tmp")
    rejected_tmp = rejected_path.with_suffix(rejected_path.suffix + ".references.tmp")
    with clean_tmp.open("w", encoding="utf-8-sig", newline="") as clean_handle, \
            rejected_tmp.open("w", encoding="utf-8-sig", newline="") as rejected_handle:
        clean_writer = csv.DictWriter(clean_handle, fieldnames=source_headers)
        rejected_writer = csv.DictWriter(
            rejected_handle,
            fieldnames=[*rejected_headers, "__source_row", "__validation_errors"],
        )
        clean_writer.writeheader()
        rejected_writer.writeheader()
        for row_number, row, errors in rows:
            if errors:
                rejected_writer.writerow({
                    **{
                        str(mapping["target"]): row.get(str(mapping["source"]), "")
                        for mapping in active
                    },
                    "__source_row": row_number,
                    "__validation_errors": " | ".join(error for error in errors if error),
                })
            else:
                clean_writer.writerow(row)
    clean_tmp.replace(clean_path)
    rejected_tmp.replace(rejected_path)
    return sum(1 for _row_number, _row, errors in rows if errors)


def _split_rejected_error_columns(rejected_path: Path) -> list[str]:
    """Replace the combined error cell with filterable per-field columns."""
    if not rejected_path.exists():
        return []
    with open_csv(rejected_path) as handle:
        reader = csv.DictReader(handle)
        headers = list(reader.fieldnames or [])
        rows = list(reader)
    if "__validation_errors" not in headers:
        return [header for header in headers if header.startswith("__error_")]

    parsed_rows = []
    error_types = []
    seen_types = set()
    for row in rows:
        grouped: dict[str, list[str]] = {}
        for raw_error in str(row.get("__validation_errors") or "").split(" | "):
            error = raw_error.strip()
            if not error:
                continue
            error_type, separator, detail = error.partition(": ")
            if not separator:
                error_type, detail = "General", error
            safe_type = re.sub(r"[^A-Za-z0-9_]+", "_", error_type).strip("_") or "General"
            column = f"__error_{safe_type}"
            grouped.setdefault(column, []).append(detail)
            if column not in seen_types:
                seen_types.add(column)
                error_types.append(column)
        parsed_rows.append((row, grouped))

    output_headers = [header for header in headers if header != "__validation_errors"]
    output_headers.extend(error_types)
    temp_path = rejected_path.with_suffix(rejected_path.suffix + ".errors.tmp")
    with temp_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=output_headers)
        writer.writeheader()
        for row, grouped in parsed_rows:
            output = {header: row.get(header, "") for header in output_headers}
            for column, messages in grouped.items():
                output[column] = " ; ".join(messages)
            writer.writerow(output)
    temp_path.replace(rejected_path)
    return error_types


def _validation_log_result(item: dict) -> str:
    """Return the same combined object outcome represented by the progress UI."""
    errors = len(item.get("errors") or [])
    field_access_issues = len(item.get("field_access_issues") or [])
    rejected = int((item.get("file_validation") or {}).get("rejected_count") or 0)
    missing_references = sum(
        int(check.get("missing_count") or 0)
        for check in item.get("target_checks") or []
    )
    knowledge_issues = len((item.get("knowledge_dependencies") or {}).get("issues") or [])
    details = []
    if errors:
        details.append(f"{errors} configuration/metadata issue(s)")
    if field_access_issues:
        details.append(f"{field_access_issues} field-access issue(s)")
    if rejected:
        details.append(f"{rejected} rejected row(s)")
    if missing_references:
        details.append(f"{missing_references} missing target reference value(s)")
    if knowledge_issues:
        details.append(f"{knowledge_issues} Knowledge dependency issue(s)")
    return f"FAILED - {', '.join(details)}" if details else "CLEAN"


def run(args) -> int:
    run_started = time.perf_counter()
    template_path = Path(args.template).resolve()
    template = json.loads(template_path.read_text(encoding="utf-8"))
    batch_dir = (ROOT / "state" / "flat_file_uploads" / str(template.get("batch_id") or "")).resolve()
    drop_dir = INPUT_FILES_DIR.resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    connector = connector_from_env("TARGET", profile=str(template.get("target_profile") or "default"))
    connector.connect()
    selected = set(args.object or [])
    selected_files = [
        file_config for file_config in template.get("files") or []
        if not file_config.get("skip_staging")
        and (not selected or str(file_config.get("object") or "").strip() in selected)
    ]
    sample_limit = args.sample_size if args.scan_mode == "sample" else None
    sampled_rows_by_object: dict[str, int] = {}
    optional_checks = set(args.optional_check or []) & OPTIONAL_CHECKS
    enabled_checks = MANDATORY_CHECKS | optional_checks
    skipped_checks = sorted(OPTIONAL_CHECKS - optional_checks)
    _log(
        f"Pre-validation configuration | template={template.get('name') or template.get('id')} "
        f"| profile={template.get('target_profile') or 'default'} | objects={len(selected_files)} "
        f"| target-aware={'yes' if args.target_aware else 'no'} | scan={args.scan_mode} "
        f"| retrieval={args.query_mode} | reports={out_dir}"
    )
    report = {
        "template_id": template.get("id"),
        "target_aware": args.target_aware,
        "validation_scope": "sample" if sample_limit is not None else "full",
        "sample_size": sample_limit,
        "mandatory_checks": sorted(MANDATORY_CHECKS),
        "selected_optional_checks": sorted(optional_checks),
        "executed_checks": sorted(enabled_checks),
        "skipped_checks": [{"id": check_id, "status": "SKIPPED_BY_USER"} for check_id in skipped_checks],
        "files": [],
    }
    report_path = out_dir / "prevalidation_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    progress_state = {
        "status": "RUNNING",
        "phase": "starting",
        "template_id": template.get("id"),
        "total_files": len(selected_files),
        "completed_files": 0,
        "current_index": 0,
        "current_file": "",
        "current_object": "",
        "rows_processed": 0,
        "total_rows": None,
        "clean_count": 0,
        "rejected_count": 0,
        "started_at": _utc_now(),
        "message": (
            f"Starting pre-validation for {len(selected_files)} file(s)"
            if selected_files else "No files selected for pre-validation"
        ),
    }
    progress_state["message"] = _progress_message(progress_state) if selected_files else progress_state["message"]
    _write_progress(out_dir, progress_state)
    failed = False
    last_progress_write = 0.0
    describe_cache: dict[str, dict] = {}

    def persist_progress(extra: dict | None = None) -> None:
        nonlocal last_progress_write
        if extra:
            progress_state.update(extra)
        progress_state["message"] = _progress_message(progress_state)
        now = time.monotonic()
        # Always flush phase/index changes; throttle row-level heartbeats.
        force = not extra or any(
            key in (extra or {})
            for key in ("phase", "current_index", "completed_files", "status")
        )
        if force or (now - last_progress_write) >= 2.0:
            _write_progress(out_dir, progress_state)
            last_progress_write = now

    for object_index, file_config in enumerate(selected_files, start=1):
        object_name = str(file_config.get("object") or "").strip()
        file_row_limit = None
        if sample_limit is not None:
            file_row_limit = max(
                0, sample_limit - sampled_rows_by_object.get(object_name, 0)
            )
        filename = str(file_config.get("stored_name") or file_config.get("file") or "").strip()
        source_path = resolve_flat_file_path(
            file_config, batch_dir, drop_dir, prefer_cleaned=False
        )
        item = {
            "file": filename,
            "object": object_name,
            "status": "RUNNING",
            "errors": [],
            "file_validation": {
                "clean_count": 0,
                "rejected_count": 0,
                "normalized_count": 0,
                "in_progress": True,
            },
        }
        report["files"] = [
            existing for existing in report["files"]
            if str(existing.get("file") or "") != filename
        ]
        report["files"].append(item)
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        persist_progress({
            "status": "RUNNING",
            "phase": "starting",
            "current_index": object_index,
            "current_file": filename,
            "current_object": object_name,
            "current_file_started_at": _utc_now(),
            "rows_processed": 0,
            "total_rows": None,
            "clean_count": 0,
            "rejected_count": 0,
        })
        if file_row_limit == 0:
            item.pop("status", None)
            item["sample_skipped"] = True
            item["elapsed_seconds"] = 0.0
            item["file_validation"] = {
                "clean_count": 0,
                "rejected_count": 0,
                "normalized_count": 0,
                "rows_scanned": 0,
                "sample_only": True,
                "sample_size": sample_limit,
                "in_progress": False,
            }
            report["files"] = [
                existing for existing in report["files"]
                if str(existing.get("file") or "") != filename
            ]
            report["files"].append(item)
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            persist_progress({
                "completed_files": object_index,
                "phase": "starting" if object_index < len(selected_files) else "completed",
                "rows_processed": 0,
                "total_rows": 0,
                "clean_count": 0,
                "rejected_count": 0,
            })
            _log(
                f"[{object_index}/{len(selected_files)}] {object_name} | SAMPLE SKIPPED "
                f"- {sample_limit:,} row object budget already reached"
            )
            continue
        object_started = time.perf_counter()
        _log(f"[{object_index}/{len(selected_files)}] {object_name} | START | file={source_path}")
        try:
            if not source_path.is_file():
                raise FileNotFoundError(f"Flat-file source not found: {source_path}")
            validate_flat_file_reference(source_path, file_config)
            describe = describe_cache.get(object_name)
            if describe is None:
                describe = connector.describe_object(object_name)
                describe_cache[object_name] = describe
                describe_source = "salesforce"
            else:
                describe_source = "cache"
            fields = describe.get("fields") or []
            active_mappings = [mapping for mapping in file_config.get("mappings") or [] if not mapping.get("ignored") and mapping.get("target")]
            _log(f"  [metadata] described_fields={len(fields):,} | source={describe_source} | active_mappings={len(active_mappings):,} | external_id={file_config.get('external_id') or '(none)'}")
            if object_name == "Contract":
                statuses = connector.query("SELECT ApiName, StatusCode FROM ContractStatus ORDER BY SortOrder")
                allowed = sorted(str(row.get("ApiName")) for row in statuses if row.get("ApiName") and row.get("StatusCode") == "Draft")
                for field in fields:
                    if field.get("name") == "Status":
                        field["_migrationAllowedValues"] = allowed; field["_migrationAllowedReason"] = "historical Contract insertion policy (draft statuses only)"
            elif object_name == "Case":
                allowed = sorted(str(row.get("Id")) for row in connector.query("SELECT Id FROM BusinessHours WHERE IsActive = true") if row.get("Id"))
                for field in fields:
                    if field.get("name") == "BusinessHoursId":
                        field["_migrationAllowedValues"] = allowed; field["_migrationAllowedReason"] = "active target Business Hours policy"
            effective_config, record_type_picklists, default_record_type_id, available_record_type_ids = (
                _record_type_validation_context(
                    connector, object_name, describe, file_config, source_path,
                    row_limit=file_row_limit,
                )
            )
            file_config["record_type_mappings"] = effective_config.get("record_type_mappings", {})
            item["errors"].extend(validate_strict_api_names(effective_config))
            item["errors"].extend(validate_object_and_record_type_access(object_name, describe, effective_config))
            item["field_access_issues"] = validate_migration_field_access(object_name, fields, effective_config.get("mappings"))
            _log(f"  [metadata] configuration_issues={len(item['errors'])} | field_access_issues={len(item['field_access_issues'])}")
            clean_name = safe_name(f"{Path(filename).stem}_clean.csv")
            rejected_name = safe_name(f"{Path(filename).stem}_rejected.csv")

            def on_clean_progress(info: dict) -> None:
                persist_progress({
                    "phase": str(info.get("phase") or "schema_clean"),
                    "rows_processed": int(info.get("rows_processed") or 0),
                    "total_rows": info.get("total_rows"),
                    "clean_count": int(info.get("clean_count") or 0),
                    "rejected_count": int(info.get("rejected_count") or 0),
                })

            # Generate large artifacts inside this run's private report folder.
            # Writing directly to the template's shared batch directory lets
            # concurrent/retried runs open the same filenames and produces a
            # late PermissionError on Windows after the expensive source scan.
            clean_work_path = out_dir / clean_name
            rejected_work_path = out_dir / rejected_name
            result = clean_csv_against_schema(
                source_path,
                clean_work_path,
                rejected_work_path,
                effective_config,
                fields,
                record_type_picklists,
                default_record_type_id,
                valid_record_type_ids=available_record_type_ids,
                progress_callback=on_clean_progress,
                row_limit=file_row_limit,
                enabled_checks=enabled_checks,
            )
            effective_config["_salesforce_id_length"] = args.salesforce_id_length
            if sample_limit is not None:
                result["sample_only"] = True
                result["sample_size"] = sample_limit
                result["object_sample_rows"] = int(result.get("rows_scanned") or 0)
                sampled_rows_by_object[object_name] = (
                    sampled_rows_by_object.get(object_name, 0)
                    + int(result.get("rows_scanned") or 0)
                )
            item["file_validation"] = result
            item.pop("status", None)
            _log(
                f"  [file] clean={int(result.get('clean_count') or 0):,} "
                f"| rejected={int(result.get('rejected_count') or 0):,} "
                f"| normalized={int(result.get('normalized_count') or 0):,}"
            )
            _log(f"  [file] clean_csv={out_dir / clean_name} | rejected_csv={out_dir / rejected_name}")
            if optional_checks & {"master_detail_references", "lookup_references"}:
                persist_progress({"phase": "target_checks"})
                item["target_checks"] = _target_checks(
                    connector, source_path, effective_config,
                    args.sample_size if args.scan_mode == "sample" else None,
                    args.sample_seed, args.query_mode, row_limit=file_row_limit,
                    described_fields=fields, enabled_checks=enabled_checks,
                )
                reference_rejected = _merge_reference_failures(
                    source_path, clean_work_path, rejected_work_path,
                    effective_config, item["target_checks"], row_limit=file_row_limit,
                )
                if reference_rejected:
                    result["rejected_count"] = reference_rejected
                    result["clean_count"] = max(
                        0, int(result.get("rows_scanned") or 0) - reference_rejected
                    )
                for check in item["target_checks"]:
                    check.pop("_missing_values", None)
            if sample_limit is None:
                result["error_columns"] = _split_rejected_error_columns(rejected_work_path)
                batch_dir.mkdir(parents=True, exist_ok=True)
                _copy_with_retry(clean_work_path, batch_dir / clean_name)
                _copy_with_retry(rejected_work_path, batch_dir / rejected_name)
                file_config["cleaned_name"] = clean_name
                file_config["rejected_name"] = rejected_name
                file_config["cleaning_result"] = result
            if object_name == "Knowledge__kav":
                mapper = IDMapper()
                try:
                    item["knowledge_dependencies"] = validate_flat_knowledge_dependencies(
                        source_path, effective_config, id_mapper=mapper,
                        row_limit=file_row_limit,
                    )
                finally:
                    mapper.close()
                _log(
                    "  [knowledge] "
                    f"record_links={item['knowledge_dependencies']['record_link_count']:,} "
                    f"| unresolved_links={item['knowledge_dependencies']['unresolved_record_link_count']:,} "
                    f"| salesforce_images={item['knowledge_dependencies']['salesforce_image_count']:,} "
                    f"| issues={len(item['knowledge_dependencies']['issues'])}"
                )
            if item["errors"] or item["field_access_issues"] or result.get("rejected_count") or any(check["missing_count"] for check in item.get("target_checks", [])) or (item.get("knowledge_dependencies") or {}).get("issues"):
                failed = True
        except Exception as exc:
            error_message = str(exc)
            if isinstance(exc, PermissionError):
                blocked_path = getattr(exc, "filename", None) or getattr(exc, "filename2", None)
                if blocked_path:
                    error_message = f"Permission denied while accessing: {blocked_path}"
                else:
                    error_message = (
                        "Permission denied while reading the source or writing validation artifacts "
                        f"for {source_path}"
                    )
            item["errors"].append(error_message)
            item.pop("status", None)
            item["file_validation"] = {
                "clean_count": 0,
                "rejected_count": 0,
                "normalized_count": 0,
                "in_progress": False,
            }
            failed = True
            _log(f"  [error] {type(exc).__name__}: {error_message}")
        report["files"] = [
            existing for existing in report["files"]
            if str(existing.get("file") or "") != filename
        ]
        item["elapsed_seconds"] = round(time.perf_counter() - object_started, 3)
        report["files"].append(item)
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        persist_progress({
            "completed_files": object_index,
            "phase": "starting" if object_index < len(selected_files) else "completed",
            "rows_processed": int((item.get("file_validation") or {}).get("clean_count") or 0)
            + int((item.get("file_validation") or {}).get("rejected_count") or 0),
            "total_rows": int((item.get("file_validation") or {}).get("clean_count") or 0)
            + int((item.get("file_validation") or {}).get("rejected_count") or 0),
            "clean_count": int((item.get("file_validation") or {}).get("clean_count") or 0),
            "rejected_count": int((item.get("file_validation") or {}).get("rejected_count") or 0),
        })
        _log(f"[{object_index}/{len(selected_files)}] {object_name} | {_validation_log_result(item)} | elapsed={item['elapsed_seconds']:.2f}s")
    report["status"] = "ISSUES_FOUND" if failed else "PASSED"
    report["validation_scope"] = "sample" if sample_limit is not None else "full"
    report["sample_size"] = sample_limit
    report["salesforce_id_length"] = args.salesforce_id_length
    report["sampled_rows_by_object"] = sampled_rows_by_object
    template["prevalidation_status"] = (
        f"SAMPLE_{report['status']}" if sample_limit is not None else report["status"]
    )
    template_path.write_text(json.dumps(template, indent=2), encoding="utf-8")
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    persist_progress({
        "status": report["status"],
        "phase": "completed",
        "completed_files": len(selected_files),
        "current_index": len(selected_files),
        "current_file": "",
        "current_object": "",
        "current_file_started_at": None,
    })
    clean_objects = sum(_validation_log_result(item) == "CLEAN" for item in report["files"])
    _log(
        f"Pre-validation {report['status']} | objects={len(report['files'])} "
        f"| clean={clean_objects} | with_issues={len(report['files']) - clean_objects} "
        f"| elapsed={time.perf_counter() - run_started:.2f}s"
    )
    _log(f"Report: {report_path}")
    return 2 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--target-aware", action="store_true")
    parser.add_argument("--scan-mode", choices=("sample", "full"), default="sample")
    parser.add_argument("--sample-size", type=int, default=1000)
    parser.add_argument("--sample-seed", type=int, default=42)
    parser.add_argument("--query-mode", choices=("auto", "rest", "bulk"), default="auto")
    parser.add_argument("--object", action="append")
    parser.add_argument("--optional-check", action="append", choices=sorted(OPTIONAL_CHECKS), default=[])
    parser.add_argument("--salesforce-id-length", choices=("15_or_18", "15", "18"), default="15_or_18")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env", override=True)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
