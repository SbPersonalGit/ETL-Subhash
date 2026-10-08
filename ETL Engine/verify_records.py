"""
Verify migrated records object-wise by comparing transformed CSV rows to target-org data.

Design:
  - Uses the transformed export (`xfm/<Object>.csv` in new runs, `xfm_<Object>.csv` in old runs)
  - Queries the target org using the object's external ID
  - For lookup/reference columns, queries target relationship external IDs directly
  - Produces per-object summary + mismatch CSV files
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable

import yaml
from openpyxl import Workbook
from openpyxl.styles import Font

from cli import build_connectors, get_latest_run_dir
from core.field_rules import build_field_profile
from core.flat_file import open_csv
from core.workspace_layout import INPUT_FILES_DIR


QUERY_ID_CHUNK_SIZE = 200
BULK_QUERY_THRESHOLD = 2000
BULK_POLL_SECONDS = 2
BULK_MAX_POLLS = 900
DEFAULT_EXTERNAL_ID = "Prod_Record_Id__c"
NUMERIC_VALUE_RE = re.compile(
    r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$"
)
MULTISELECT_FIELD_TYPES = {"multipicklist", "multiselectpicklist"}
PROJECT_ROOT = Path(__file__).resolve().parent
FLAT_FILE_DROP_DIR = INPUT_FILES_DIR


@dataclass
class VerifyResult:
    object_name: str
    expected_rows: int
    matched_rows: int
    missing_in_target: int
    mismatched_rows: int
    mismatched_fields: int
    skipped_fields: list[str]
    summary_file: Path
    mismatch_file: Path


def find_data_seeding_file_config(config: dict, object_name: str) -> dict:
    for file_cfg in config.get("files", []) or []:
        if file_cfg.get("object") == object_name:
            return file_cfg
    raise KeyError(f"Data Seeding file configuration not found for {object_name}")


def resolve_data_seeding_source_dir(config: dict, config_path: Path, source_dir: str | None) -> Path:
    if source_dir:
        resolved = Path(source_dir)
    else:
        batch_id = str(config.get("batch_id") or "").strip()
        if not batch_id:
            raise ValueError("Data Seeding config has no batch_id; pass --source-dir.")
        resolved = config_path.resolve().parents[2] / "state" / "flat_file_uploads" / batch_id
    if not resolved.is_dir():
        raise FileNotFoundError(f"Data Seeding source directory not found: {resolved}")
    return resolved


def resolve_archived_source_file(source_dir: Path, source_name: str) -> Path:
    """Resolve a copied upload or a safely archived zero-copy drop-folder reference."""
    source_dir = source_dir.resolve()
    source_path = (source_dir / source_name).resolve()
    if source_path.parent == source_dir and source_path.is_file():
        return source_path

    reference_path = (source_dir / f"{source_name}.reference.json").resolve()
    if reference_path.parent != source_dir or not reference_path.is_file():
        raise FileNotFoundError(f"Source file not found: {source_path}")
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    if str(reference.get("source_type") or "") != "drop_folder":
        raise ValueError(f"Unsupported archived source reference: {reference_path}")
    relative = Path(str(reference.get("source_path") or ""))
    drop_root = FLAT_FILE_DROP_DIR.resolve()
    if not str(relative) or relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Invalid drop-folder source reference: {reference_path}")
    resolved = (drop_root / relative).resolve()
    if not resolved.is_relative_to(drop_root) or not resolved.is_file():
        raise FileNotFoundError(f"Referenced drop-folder source file not found: {resolved}")
    expected_size = reference.get("source_size", reference.get("size"))
    if expected_size is not None and int(expected_size) != resolved.stat().st_size:
        raise ValueError(
            f"Referenced drop-folder source changed size after the migration run: {resolved.name}"
        )
    return resolved


def verify_source_to_stage(
    object_name: str,
    run_dir: Path,
    pipeline_id: str | None,
    config: dict,
    source_dir: Path,
    source_kind: str,
    output_dir: Path,
    sample_size: int | None = None,
    sample_seed: int = 42,
) -> VerifyResult:
    file_cfg = find_data_seeding_file_config(config, object_name)
    external_id_field = file_cfg.get("external_id") or DEFAULT_EXTERNAL_ID
    source_name_key = "cleaned_name" if source_kind == "cleaned" else "stored_name"
    source_name = file_cfg.get(source_name_key) or file_cfg.get("file")
    source_path = resolve_archived_source_file(source_dir, str(source_name))

    xfm_path = find_xfm_csv(run_dir, object_name, pipeline_id=pipeline_id)
    staged_rows, staged_fields = read_expected_rows(
        xfm_path, external_id_field, sample_size=sample_size, sample_seed=sample_seed
    )
    staged_by_id = {row[external_id_field]: row for row in staged_rows}
    staged_field_set = set(staged_fields)

    mappings = [
        mapping for mapping in file_cfg.get("mappings", []) or []
        if not mapping.get("ignored") and mapping.get("source") and mapping.get("target")
    ]
    external_mapping = next(
        (mapping for mapping in mappings if mapping.get("target") == external_id_field),
        None,
    )
    if not external_mapping:
        raise ValueError(
            f"{object_name}: no source mapping targets external ID field {external_id_field}."
        )

    comparable = [
        mapping for mapping in mappings
        if mapping.get("target") in staged_field_set
        and mapping.get("target") != external_id_field
    ]
    source_rows: list[dict[str, str]] = []
    rng = random.Random(sample_seed)
    eligible_count = 0
    with open_csv(source_path) as handle:
        for row in csv.DictReader(handle):
            normalized = {key: normalize_value(value) for key, value in row.items() if key}
            if not normalized.get(external_mapping["source"], ""):
                continue
            eligible_count += 1
            if sample_size is None or len(source_rows) < sample_size:
                source_rows.append(normalized)
                continue
            replacement = rng.randrange(eligible_count)
            if replacement < sample_size:
                source_rows[replacement] = normalized

    mismatch_rows: list[dict[str, str]] = []
    matched_rows = 0
    missing_in_stage = 0
    mismatched_ids: set[str] = set()
    external_source_field = external_mapping["source"]
    record_type_mappings = file_cfg.get("record_type_mappings") or {}

    for source_row in source_rows:
        external_id = source_row.get(external_source_field, "")
        if not external_id:
            continue
        staged_row = staged_by_id.get(external_id)
        if not staged_row:
            missing_in_stage += 1
            mismatch_rows.append({
                "object_name": object_name,
                "external_id": external_id,
                "field": "__row__",
                "issue": "missing_in_stage",
                "expected": "present",
                "actual": "",
            })
            continue

        row_mismatch = False
        for mapping in comparable:
            source_field = mapping["source"]
            target_field = mapping["target"]
            expected_value = source_row.get(source_field, "")
            if target_field == "RecordTypeId":
                expected_value = record_type_mappings.get(expected_value, expected_value)
            actual_value = staged_row.get(target_field, "")
            if values_equal(expected_value, actual_value):
                continue
            row_mismatch = True
            mismatch_rows.append({
                "object_name": object_name,
                "external_id": external_id,
                "field": target_field,
                "issue": "source_stage_mismatch",
                "expected": expected_value,
                "actual": actual_value,
            })
        if row_mismatch:
            mismatched_ids.add(external_id)
        else:
            matched_rows += 1

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    mismatch_file = output_dir / f"{object_name}_source_stage_mismatches_{timestamp}.csv"
    summary_file = output_dir / f"{object_name}_source_stage_summary_{timestamp}.csv"
    with open(mismatch_file, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["object_name", "external_id", "field", "issue", "expected", "actual"],
        )
        writer.writeheader()
        writer.writerows(mismatch_rows)
    with open(summary_file, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "object_name", "source_rows", "matched_rows", "missing_in_stage",
                "mismatched_rows", "mismatched_fields", "comparable_fields",
                "source_file", "xfm_csv", "mismatch_file",
            ],
        )
        writer.writeheader()
        writer.writerow({
            "object_name": object_name,
            "source_rows": len(source_rows),
            "matched_rows": matched_rows,
            "missing_in_stage": missing_in_stage,
            "mismatched_rows": len(mismatched_ids),
            "mismatched_fields": len(mismatch_rows),
            "comparable_fields": len(comparable),
            "source_file": str(source_path),
            "xfm_csv": str(xfm_path),
            "mismatch_file": str(mismatch_file),
        })
    return VerifyResult(
        object_name=object_name,
        expected_rows=len(source_rows),
        matched_rows=matched_rows,
        missing_in_target=missing_in_stage,
        mismatched_rows=len(mismatched_ids),
        mismatched_fields=len(mismatch_rows),
        skipped_fields=[],
        summary_file=summary_file,
        mismatch_file=mismatch_file,
    )


def load_config(config_path: Path) -> dict:
    with open(config_path, encoding="utf-8-sig") as handle:
        if config_path.suffix.lower() == ".json":
            return json.load(handle) or {}
        return yaml.safe_load(handle) or {}


def build_object_config_map(config: dict) -> dict[str, dict]:
    """Normalize migration YAML and Data Seeding JSON into object configs."""
    objects: dict[str, dict] = {}

    # Standard migration YAML:
    # pipelines[].objects[] = {name, external_id, fields, ...}
    for pipeline in config.get("pipelines", []):
        for obj_cfg in pipeline.get("objects", []):
            if obj_cfg.get("name"):
                objects[obj_cfg["name"]] = obj_cfg

    # Data Seeding / flat-file template JSON:
    # files[] = {object, external_id, mappings: [{target, ignored, ...}], ...}
    for file_cfg in config.get("files", []):
        object_name = file_cfg.get("object")
        if not object_name:
            continue

        fields = []
        for mapping in file_cfg.get("mappings", []) or []:
            target_field = mapping.get("target")
            if mapping.get("ignored") or not target_field:
                continue
            fields.append(target_field)

        objects[object_name] = {
            "name": object_name,
            "external_id": file_cfg.get("external_id") or DEFAULT_EXTERNAL_ID,
            "fields": list(dict.fromkeys(fields)),
            "_config_source": "data_seeding_json",
        }
    return objects


def chunked(values: list[str], size: int) -> Iterable[list[str]]:
    for idx in range(0, len(values), size):
        yield values[idx: idx + size]


def escape_soql(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def flatten_record(record: dict, prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, value in record.items():
        if key == "attributes":
            continue
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(flatten_record(value, full_key))
            continue
        flat[full_key] = normalize_value(value)
    return flat


def normalize_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip()
    if text.lower() in {"none", "null", "nan"}:
        return ""
    return text


def values_equal(expected, actual, field_type: str | None = None) -> bool:
    """Compare CSV and Salesforce values while ignoring representation-only differences."""
    expected_text = normalize_value(expected)
    actual_text = normalize_value(actual)
    if expected_text == actual_text:
        return True

    # Salesforce can return long-text values with different line endings than
    # the CSV source even though their displayed content is identical.
    expected_text = expected_text.replace("\r\n", "\n").replace("\r", "\n")
    actual_text = actual_text.replace("\r\n", "\n").replace("\r", "\n")
    if expected_text == actual_text:
        return True

    # Multi-select picklist selections are a set-like value. Salesforce may
    # return the same selections in a different order after migration. Keep
    # duplicates so malformed values are not accidentally treated as equal.
    if str(field_type or "").lower() in MULTISELECT_FIELD_TYPES:
        expected_items = sorted(item.strip() for item in expected_text.split(";"))
        actual_items = sorted(item.strip() for item in actual_text.split(";"))
        if expected_items == actual_items:
            return True

    boolean_values = {"true", "false"}
    if expected_text.lower() in boolean_values and actual_text.lower() in boolean_values:
        return expected_text.lower() == actual_text.lower()

    # Salesforce commonly returns the case-safe 18-character form for a
    # 15-character record ID supplied by a CSV or staging table.
    if {len(expected_text), len(actual_text)} == {15, 18}:
        shorter, longer = sorted((expected_text, actual_text), key=len)
        if shorter.isalnum() and longer.isalnum() and longer.startswith(shorter):
            return True

    if NUMERIC_VALUE_RE.fullmatch(expected_text) and NUMERIC_VALUE_RE.fullmatch(actual_text):
        try:
            return Decimal(expected_text) == Decimal(actual_text)
        except InvalidOperation:
            pass

    try:
        expected_datetime = datetime.fromisoformat(
            expected_text.replace("Z", "+00:00")
        )
        actual_datetime = datetime.fromisoformat(
            re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", actual_text).replace("Z", "+00:00")
        )
        return expected_datetime == actual_datetime
    except ValueError:
        return False


def read_expected_rows(
    csv_path: Path,
    external_id_field: str,
    sample_size: int | None = None,
    sample_seed: int = 42,
) -> tuple[list[dict[str, str]], list[str]]:
    with open_csv(csv_path) as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        rows = []
        rng = random.Random(sample_seed)
        eligible_count = 0
        for row in reader:
            normalized = {key: normalize_value(value) for key, value in row.items() if key}
            ext_id = normalized.get(external_id_field, "")
            if not ext_id:
                continue
            eligible_count += 1
            if sample_size is None or len(rows) < sample_size:
                rows.append(normalized)
                continue
            replacement = rng.randrange(eligible_count)
            if replacement < sample_size:
                rows[replacement] = normalized
    return rows, fieldnames


def bulk_query_records(
    connector,
    soql: str,
    object_name: str,
    progress_callback=None,
) -> Iterable[dict[str, str]]:
    job = connector.bulk_post("jobs/query", {"operation": "query", "query": soql})
    job_id = job["id"]
    print(f"[{object_name}] Bulk query started | job_id={job_id}", flush=True)
    if progress_callback:
        progress_callback({
            "message": f"[{object_name}] Bulk query started | job_id={job_id}",
            "stage_fraction": 0.05,
            "bulk_job_id": job_id,
            "bulk_state": "UploadComplete",
        })
    for poll_idx in range(BULK_MAX_POLLS):
        status = connector.bulk_get(f"jobs/query/{job_id}").json()
        state = status.get("state")
        records_processed = status.get("numberRecordsProcessed")
        # Poll loop fills roughly 5%..70% of the query stage.
        fraction = min(0.7, 0.05 + (poll_idx + 1) / max(BULK_MAX_POLLS, 1) * 0.65)
        if poll_idx == 0 or state == "JobComplete" or poll_idx % 6 == 0:
            message = (
                f"[{object_name}] Bulk query status | job_id={job_id} state={state} "
                f"recordsProcessed={records_processed}"
            )
            if progress_callback:
                progress_callback({
                    "message": message,
                    "stage_fraction": 0.7 if state == "JobComplete" else fraction,
                    "bulk_job_id": job_id,
                    "bulk_state": state,
                    "records_processed": records_processed,
                })
            else:
                print(message, flush=True)
        if state == "JobComplete":
            break
        if state in {"Failed", "Aborted"}:
            raise RuntimeError(f"[{object_name}] Bulk query {job_id} ended in {state}: {status}")
        time.sleep(BULK_POLL_SECONDS)
    else:
        raise TimeoutError(f"[{object_name}] Bulk query {job_id} did not complete in time")

    locator = None
    page = 0
    while True:
        page += 1
        response = connector.bulk_get_csv(f"jobs/query/{job_id}/results", locator=locator)
        text = connector.decode_csv_response(response)
        if text.strip():
            message = f"[{object_name}] Bulk result page {page} downloaded ({len(text):,} chars)"
            if progress_callback:
                progress_callback({
                    "message": message,
                    "stage_fraction": min(0.95, 0.7 + page * 0.05),
                    "bulk_job_id": job_id,
                    "bulk_state": "Downloading",
                    "bulk_page": page,
                })
            else:
                print(message, flush=True)
            for record in csv.DictReader(io.StringIO(text)):
                yield {key: normalize_value(value) for key, value in record.items() if key}
        locator = response.headers.get("Sforce-Locator")
        if not locator or locator == "null":
            break
    done_message = f"[{object_name}] Bulk query download complete | pages={page}"
    if progress_callback:
        progress_callback({
            "message": done_message,
            "stage_fraction": 1.0,
            "bulk_job_id": job_id,
            "bulk_state": "Complete",
            "bulk_page": page,
        })
    else:
        print(done_message, flush=True)


def resolve_reference_compare_field(ref_object: str, connector, object_cfg_map: dict[str, dict], profile_cache: dict[str, object]) -> str | None:
    ref_profile = profile_cache.get(ref_object)
    if ref_profile is None:
        ref_profile = build_field_profile(connector, ref_object)
        profile_cache[ref_object] = ref_profile

    configured_external_id = object_cfg_map.get(ref_object, {}).get("external_id")
    if configured_external_id and configured_external_id in ref_profile.field_meta:
        return configured_external_id

    if DEFAULT_EXTERNAL_ID in ref_profile.field_meta:
        return DEFAULT_EXTERNAL_ID

    if ref_profile.external_id_fields:
        return ref_profile.external_id_fields[0]

    return None


def build_compare_fields(object_cfg: dict, profile, object_cfg_map: dict[str, dict], connector) -> tuple[list[str], list[str]]:
    selected: list[str] = []
    skipped: list[str] = []
    ref_profile_cache: dict[str, object] = {}

    for field_name in object_cfg.get("fields", []) or []:
        meta = profile.field_meta.get(field_name)
        if not meta:
            skipped.append(field_name)
            continue

        if not meta.is_lookup:
            selected.append(field_name)
            continue

        if len(meta.references_to) != 1 or not meta.relationship_name:
            skipped.append(field_name)
            continue

        ref_object = meta.references_to[0]
        ref_external_id = resolve_reference_compare_field(ref_object, connector, object_cfg_map, ref_profile_cache)
        if not ref_external_id:
            skipped.append(field_name)
            continue
        selected.append(f"{meta.relationship_name}.{ref_external_id}")

    deduped = list(dict.fromkeys(selected))
    return deduped, skipped


def find_xfm_csv(run_dir: Path, object_name: str, pipeline_id: str | None = None) -> Path:
    legacy_path = run_dir / f"xfm_{object_name}.csv"
    if legacy_path.exists():
        return legacy_path

    direct_stage_path = run_dir / "xfm" / f"{object_name}.csv"
    if direct_stage_path.exists():
        return direct_stage_path

    if pipeline_id:
        pipeline_path = run_dir / pipeline_id / "xfm" / f"{object_name}.csv"
        if pipeline_path.exists():
            return pipeline_path
        attempt_matches = sorted(run_dir.glob(f"{pipeline_id}_attempt_*/xfm/{object_name}.csv"))
        if len(attempt_matches) == 1:
            return attempt_matches[0]
        if len(attempt_matches) > 1:
            match_list = ", ".join(str(path.parent.parent.name) for path in attempt_matches)
            raise FileNotFoundError(
                f"Multiple transformed CSV files found for {object_name} under pipeline {pipeline_id}: {match_list}. "
                "Use a more specific run folder or clean up older attempt folders."
            )
        raise FileNotFoundError(
            f"Transformed CSV not found: {pipeline_path}. "
            f"Also checked attempt folders matching {pipeline_id}_attempt_*"
        )

    matches = sorted(run_dir.glob(f"*/xfm/{object_name}.csv"))
    if not matches:
        raise FileNotFoundError(
            f"Transformed CSV not found for {object_name}. "
            f"Checked legacy path, direct xfm folder, and pipeline folders under {run_dir}"
        )
    if len(matches) > 1:
        match_list = ", ".join(str(path.parent.parent.name) for path in matches)
        raise FileNotFoundError(
            f"Multiple transformed CSV files found for {object_name}: {match_list}. "
            f"Pass --pipeline <pipeline_id> to select one."
        )
    return matches[0]


def resolve_query_fields(object_name: str, candidate_fields: list[str], target_profile) -> tuple[list[str], list[str]]:
    direct_fields = set(target_profile.field_meta.keys()) | {"Id"}
    skipped: list[str] = []
    selected: list[str] = []

    for field_name in candidate_fields:
        if field_name == "Id":
            continue
        if "." not in field_name:
            if field_name in direct_fields:
                selected.append(field_name)
            else:
                skipped.append(field_name)
            continue

        relationship_name, ref_field = field_name.split(".", 1)
        matched = False
        for meta in target_profile.field_meta.values():
            if meta.relationship_name != relationship_name:
                continue
            if len(meta.references_to) != 1:
                continue
            matched = True
            selected.append(field_name)
            break
        if not matched:
            skipped.append(field_name)

    deduped = list(dict.fromkeys(selected))
    return deduped, skipped


def query_rows_by_field_values(
    connector,
    object_name: str,
    match_field: str,
    match_values: list[str],
    query_fields: list[str],
    print_soql: bool = False,
    progress_label: str = "target",
    query_mode: str = "auto",
    progress_callback=None,
    id_chunk_size: int | None = None,
) -> dict[str, dict[str, str]]:
    if match_field not in query_fields:
        query_fields = [match_field, *query_fields]

    rows_by_key: dict[str, dict[str, str]] = {}
    select_clause = ", ".join(query_fields)
    use_bulk = query_mode == "bulk" or (query_mode == "auto" and len(match_values) >= BULK_QUERY_THRESHOLD)
    if use_bulk:
        expected = set(match_values)
        soql = f"SELECT {select_clause} FROM {object_name} WHERE {match_field} != null"
        print(
            f"[{object_name}] Querying {progress_label} with Bulk API 2.0 | "
            f"expected keys={len(expected):,} (org-wide WHERE {match_field} != null)",
            flush=True,
        )
        if progress_callback:
            progress_callback({
                "message": (
                    f"[{object_name}] Querying {progress_label} with Bulk API 2.0 | "
                    f"expected keys={len(expected):,} (org-wide WHERE {match_field} != null)"
                ),
                "stage_fraction": 0.02,
            })
        if print_soql:
            print(soql, flush=True)
        for record in bulk_query_records(
            connector, soql, object_name, progress_callback=progress_callback
        ):
            match_value = record.get(match_field, "")
            if match_value in expected:
                rows_by_key[match_value] = record
        print(
            f"[{object_name}] Bulk filter complete | matched_keys={len(rows_by_key):,}/"
            f"{len(expected):,}",
            flush=True,
        )
        if progress_callback:
            progress_callback({
                "message": (
                    f"[{object_name}] Bulk filter complete | matched_keys={len(rows_by_key):,}/"
                    f"{len(expected):,}"
                ),
                "stage_fraction": 1.0,
            })
        return rows_by_key

    chunk_size = int(id_chunk_size or QUERY_ID_CHUNK_SIZE)
    chunk_size = max(25, min(500, chunk_size))
    total_chunks = (len(match_values) + chunk_size - 1) // chunk_size

    for chunk_idx, value_chunk in enumerate(chunked(match_values, chunk_size), start=1):
        values_csv = "', '".join(escape_soql(value) for value in value_chunk)
        soql = (
            f"SELECT {select_clause} "
            f"FROM {object_name} "
            f"WHERE {match_field} IN ('{values_csv}')"
        )
        if print_soql:
            print(
                f"[{object_name}] {progress_label.upper()} SOQL chunk {chunk_idx}/{total_chunks} | "
                f"values={len(value_chunk)}"
            )
            print(
                f"SELECT {select_clause} "
                f"FROM {object_name} "
                f"WHERE {match_field} IN (<{len(value_chunk)} values>)"
            )
        elif total_chunks > 1 and (chunk_idx == 1 or chunk_idx == total_chunks or chunk_idx % 10 == 0):
            message = (
                f"[{object_name}] Querying {progress_label} chunk {chunk_idx}/{total_chunks}"
                f" (batch={chunk_size})..."
            )
            print(message, flush=True)
            if progress_callback:
                progress_callback({
                    "message": message,
                    "stage_fraction": chunk_idx / max(total_chunks, 1),
                    "chunk_index": chunk_idx,
                    "total_chunks": total_chunks,
                    "id_chunk_size": chunk_size,
                })

        for record in connector.query(soql):
            flat = flatten_record(record)
            match_value = flat.get(match_field, "")
            if match_value:
                rows_by_key[match_value] = flat

    return rows_by_key


def query_rows_by_filter(
    connector,
    object_name: str,
    query_fields: list[str],
    query_filter: str,
    key_field: str,
    print_soql: bool = False,
    progress_label: str = "target",
    query_mode: str = "auto",
) -> dict[str, dict[str, str]]:
    if key_field not in query_fields:
        query_fields = [key_field, *query_fields]

    select_clause = ", ".join(dict.fromkeys(query_fields))
    normalized_filter = (query_filter or "").strip()
    where_clause = normalized_filter if normalized_filter.upper().startswith("WHERE ") else f"WHERE {normalized_filter}"
    soql = f"SELECT {select_clause} FROM {object_name} {where_clause}"

    if print_soql:
        print(f"[{object_name}] {progress_label.upper()} filter query")
        print(soql)
    else:
        print(f"[{object_name}] Querying {progress_label} with filter: {where_clause}")

    rows_by_key: dict[str, dict[str, str]] = {}
    records = bulk_query_records(connector, soql, object_name) if query_mode == "bulk" else connector.query(soql)
    for record in records:
        flat = record if query_mode == "bulk" else flatten_record(record)
        key_value = flat.get(key_field, "")
        if key_value:
            rows_by_key[key_value] = flat
    return rows_by_key


def query_target_rows(
    target,
    object_name: str,
    external_id_field: str,
    query_fields: list[str],
    expected_ids: list[str],
    print_soql: bool = False,
    query_mode: str = "auto",
    progress_callback=None,
    id_chunk_size: int | None = None,
) -> dict[str, dict[str, str]]:
    return query_rows_by_field_values(
        connector=target,
        object_name=object_name,
        match_field=external_id_field,
        match_values=expected_ids,
        query_fields=query_fields,
        print_soql=print_soql,
        progress_label="target",
        query_mode=query_mode,
        progress_callback=progress_callback,
        id_chunk_size=id_chunk_size,
    )


def compare_rows(
    object_name: str,
    expected_rows: list[dict[str, str]],
    actual_rows: dict[str, dict[str, str]],
    key_field: str,
    compare_fields: list[str],
    field_types: dict[str, str] | None = None,
    row_compare_fields: dict[str, list[str]] | None = None,
) -> tuple[int, int, int, list[dict[str, str]]]:
    mismatch_rows: list[dict[str, str]] = []
    matched_rows = 0
    missing_in_target = 0
    mismatched_row_ids: set[str] = set()

    for expected in expected_rows:
        ext_id = expected[key_field]
        actual = actual_rows.get(ext_id)
        if not actual:
            missing_in_target += 1
            mismatch_rows.append({
                "object_name": object_name,
                "external_id": ext_id,
                "field": "__row__",
                "issue": "missing_in_target",
                "expected": "present",
                "actual": "",
            })
            continue

        fields_for_row = compare_fields
        if row_compare_fields is not None:
            fields_for_row = row_compare_fields.get(ext_id, compare_fields)

        row_has_mismatch = False
        for field_name in fields_for_row:
            expected_value = normalize_value(expected.get(field_name, ""))
            actual_value = normalize_value(actual.get(field_name, ""))
            if values_equal(
                expected_value,
                actual_value,
                (field_types or {}).get(field_name),
            ):
                continue
            row_has_mismatch = True
            mismatch_rows.append({
                "object_name": object_name,
                "external_id": ext_id,
                "field": field_name,
                "issue": "value_mismatch",
                "expected": expected_value,
                "actual": actual_value,
            })

        if row_has_mismatch:
            mismatched_row_ids.add(ext_id)
        else:
            matched_rows += 1

    return matched_rows, missing_in_target, len(mismatched_row_ids), mismatch_rows


def excel_safe_value(value):
    """Return a workbook-safe scalar without interpreting migrated text as formulas."""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return f"'{value}"
    return value


def write_validation_workbook(
    path: Path,
    mismatch_rows: list[dict[str, str]],
    target_rows: dict[str, dict[str, str]],
    external_id_field: str,
    loaded_fields: list[str],
) -> None:
    """Write field-level differences and all retrieved target rows to one workbook."""
    workbook = Workbook()
    diff_sheet = workbook.active
    diff_sheet.title = "Diff"
    diff_headers = ["object_name", "external_id", "field", "issue", "expected", "actual"]
    diff_sheet.append(diff_headers)
    for row in mismatch_rows:
        diff_sheet.append([excel_safe_value(row.get(header, "")) for header in diff_headers])

    loaded_sheet = workbook.create_sheet("Actual Migrated Data")
    ordered_fields = list(dict.fromkeys(["Id", external_id_field, *loaded_fields]))
    loaded_sheet.append(ordered_fields)
    for external_id in sorted(target_rows):
        row = target_rows[external_id]
        loaded_sheet.append([
            excel_safe_value(row.get(field, ""))
            for field in ordered_fields
        ])

    for sheet in (diff_sheet, loaded_sheet):
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
    workbook.save(path)


def verify_object(
    object_name: str,
    run_dir: Path,
    object_cfg: dict,
    target,
    object_cfg_map: dict[str, dict],
    output_dir: Path,
    pipeline_id: str | None = None,
    print_soql: bool = False,
    sample_size: int | None = None,
    sample_seed: int = 42,
    query_mode: str = "auto",
) -> VerifyResult:
    external_id_field = object_cfg.get("external_id") or DEFAULT_EXTERNAL_ID
    csv_path = find_xfm_csv(run_dir, object_name, pipeline_id=pipeline_id)

    expected_rows, candidate_fields = read_expected_rows(
        csv_path, external_id_field, sample_size=sample_size, sample_seed=sample_seed
    )
    target_profile = build_field_profile(target, object_name)
    config_skipped_fields = [
        field_name for field_name in build_compare_fields(object_cfg, target_profile, object_cfg_map, target)[1]
        if field_name not in candidate_fields
    ]
    query_fields, skipped_fields = resolve_query_fields(object_name, candidate_fields, target_profile)
    skipped_fields = list(dict.fromkeys([*config_skipped_fields, *skipped_fields]))
    expected_ids = [row[external_id_field] for row in expected_rows]
    print(f"[{object_name}] Using {csv_path} | expected rows={len(expected_rows)}")
    retrieval_fields = list(dict.fromkeys(["Id", external_id_field, *query_fields]))
    target_rows = query_target_rows(
        target,
        object_name,
        external_id_field,
        retrieval_fields,
        expected_ids,
        print_soql=print_soql,
        query_mode=query_mode,
    )
    compare_fields = [field for field in query_fields if field != external_id_field]
    field_types = {
        field: target_profile.field_meta[field].field_type
        for field in compare_fields
        if field in target_profile.field_meta
    }
    matched_rows, missing_in_target, mismatched_rows, mismatch_rows = compare_rows(
        object_name=object_name,
        expected_rows=expected_rows,
        actual_rows=target_rows,
        key_field=external_id_field,
        compare_fields=compare_fields,
        field_types=field_types,
    )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    mismatch_file = output_dir / f"{object_name}_verify_mismatches_{timestamp}.csv"
    summary_file = output_dir / f"{object_name}_verify_summary_{timestamp}.csv"
    workbook_file = output_dir / f"{object_name}_verify_results_{timestamp}.xlsx"

    with open(mismatch_file, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["object_name", "external_id", "field", "issue", "expected", "actual"],
        )
        writer.writeheader()
        writer.writerows(mismatch_rows)

    write_validation_workbook(
        workbook_file,
        mismatch_rows,
        target_rows,
        external_id_field,
        retrieval_fields,
    )

    with open(summary_file, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "object_name",
                "expected_rows",
                "matched_rows",
                "missing_in_target",
                "mismatched_rows",
                "mismatched_fields",
                "skipped_fields",
                "xfm_csv",
                "mismatch_file",
                "workbook_file",
            ],
        )
        writer.writeheader()
        writer.writerow({
            "object_name": object_name,
            "expected_rows": len(expected_rows),
            "matched_rows": matched_rows,
            "missing_in_target": missing_in_target,
            "mismatched_rows": mismatched_rows,
            "mismatched_fields": len(mismatch_rows),
            "skipped_fields": ", ".join(skipped_fields),
            "xfm_csv": str(csv_path),
            "mismatch_file": str(mismatch_file),
            "workbook_file": str(workbook_file),
        })

    return VerifyResult(
        object_name=object_name,
        expected_rows=len(expected_rows),
        matched_rows=matched_rows,
        missing_in_target=missing_in_target,
        mismatched_rows=mismatched_rows,
        mismatched_fields=len(mismatch_rows),
        skipped_fields=skipped_fields,
        summary_file=summary_file,
        mismatch_file=mismatch_file,
    )


def verify_object_source_target_direct(
    object_name: str,
    object_cfg: dict,
    object_cfg_map: dict[str, dict],
    source,
    target,
    output_dir: Path,
    target_where: str,
    print_soql: bool = False,
    sample_size: int | None = None,
    sample_seed: int = 42,
    query_mode: str = "auto",
) -> VerifyResult:
    external_id_field = object_cfg.get("external_id") or DEFAULT_EXTERNAL_ID
    source_profile = build_field_profile(source, object_name)
    target_profile = build_field_profile(target, object_name)

    candidate_fields, config_skipped_fields = build_compare_fields(object_cfg, source_profile, object_cfg_map, source)
    target_query_fields, target_skipped = resolve_query_fields(object_name, candidate_fields, target_profile)
    source_query_fields, source_skipped = resolve_query_fields(object_name, candidate_fields, source_profile)
    skipped_fields = list(dict.fromkeys([*config_skipped_fields, *target_skipped, *source_skipped]))

    target_rows = query_rows_by_filter(
        connector=target,
        object_name=object_name,
        query_fields=target_query_fields,
        query_filter=target_where,
        key_field=external_id_field,
        print_soql=print_soql,
        progress_label="target",
        query_mode="bulk" if query_mode == "bulk" else "rest",
    )
    source_ids = sorted(target_rows.keys())
    if sample_size is not None and len(source_ids) > sample_size:
        source_ids = sorted(random.Random(sample_seed).sample(source_ids, sample_size))
        target_rows = {key: target_rows[key] for key in source_ids}
    print(f"[{object_name}] Target rows selected={len(source_ids)}")

    source_rows = query_rows_by_field_values(
        connector=source,
        object_name=object_name,
        match_field="Id",
        match_values=source_ids,
        query_fields=source_query_fields,
        print_soql=print_soql,
        progress_label="source",
        query_mode=query_mode,
    )
    expected_rows = [{"Id": source_id, **row} for source_id, row in source_rows.items()]
    expected_rows = sorted(expected_rows, key=lambda row: row["Id"])
    actual_rows = {key: value for key, value in target_rows.items()}

    compare_fields = [field for field in target_query_fields if field != external_id_field]
    field_types = {
        field: target_profile.field_meta[field].field_type
        for field in compare_fields
        if field in target_profile.field_meta
    }
    matched_rows, missing_in_target, mismatched_rows, mismatch_rows = compare_rows(
        object_name=object_name,
        expected_rows=expected_rows,
        actual_rows=actual_rows,
        key_field="Id",
        compare_fields=compare_fields,
        field_types=field_types,
    )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    mismatch_file = output_dir / f"{object_name}_source_target_mismatches_{timestamp}.csv"
    summary_file = output_dir / f"{object_name}_source_target_summary_{timestamp}.csv"

    with open(mismatch_file, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["object_name", "external_id", "field", "issue", "expected", "actual"],
        )
        writer.writeheader()
        writer.writerows(mismatch_rows)

    with open(summary_file, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "object_name",
                "expected_rows",
                "matched_rows",
                "missing_in_target",
                "mismatched_rows",
                "mismatched_fields",
                "skipped_fields",
                "target_where",
                "mismatch_file",
            ],
        )
        writer.writeheader()
        writer.writerow({
            "object_name": object_name,
            "expected_rows": len(expected_rows),
            "matched_rows": matched_rows,
            "missing_in_target": missing_in_target,
            "mismatched_rows": mismatched_rows,
            "mismatched_fields": len(mismatch_rows),
            "skipped_fields": ", ".join(skipped_fields),
            "target_where": target_where,
            "mismatch_file": str(mismatch_file),
        })

    return VerifyResult(
        object_name=object_name,
        expected_rows=len(expected_rows),
        matched_rows=matched_rows,
        missing_in_target=missing_in_target,
        mismatched_rows=mismatched_rows,
        mismatched_fields=len(mismatch_rows),
        skipped_fields=skipped_fields,
        summary_file=summary_file,
        mismatch_file=mismatch_file,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify transformed rows against target org data.")
    parser.add_argument(
        "--config",
        default="config/migration.yaml",
        help="Path to migration YAML or Data Seeding flat-file template JSON.",
    )
    parser.add_argument("--run-dir", default=None, help="Run directory containing exported transformed CSV files.")
    parser.add_argument("--pipeline", default=None, help="Pipeline ID under the run directory, for example Itinerary_Child.")
    parser.add_argument("--object", dest="objects", action="append", help="Object to verify. Repeat for multiple objects.")
    parser.add_argument("--target-where", default=None, help="Direct source-vs-target compare mode. Filter target rows with this WHERE clause, then compare matched source records by external ID.")
    parser.add_argument("--out-dir", default="logs/verify", help="Directory to write verification reports.")
    parser.add_argument("--print-soql", action="store_true", help="Print each target SOQL query used during verification.")
    parser.add_argument("--sample-size", type=int, default=None, help="Deterministically validate at most this many rows per object. Omit for a full scan.")
    parser.add_argument("--sample-seed", type=int, default=42, help="Seed used for repeatable reservoir sampling (default: 42).")
    parser.add_argument("--query-mode", choices=["auto", "rest", "bulk"], default="auto", help="Target retrieval mode. Auto uses Bulk API 2.0 for large validations.")
    parser.add_argument(
        "--compare",
        choices=["stage-target", "source-stage", "source-target"],
        default="stage-target",
        help="Comparison path. source-target runs the source-stage audit and then stage-target verification.",
    )
    parser.add_argument("--source-dir", default=None, help="Directory containing Data Seeding uploaded/cleaned files.")
    parser.add_argument(
        "--source-kind",
        choices=["cleaned", "uploaded"],
        default="cleaned",
        help="Use accepted cleaned files (default) or exact original uploads for source comparisons.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.sample_size is not None and args.sample_size < 1:
        raise SystemExit("--sample-size must be at least 1; omit it for a full scan.")
    effective_query_mode = "bulk" if args.query_mode == "auto" and args.sample_size is None else args.query_mode
    config_path = Path(args.config)
    if not config_path.exists():
        raise SystemExit(f"Config file not found: {config_path}")
    config = load_config(config_path)
    object_cfg_map = build_object_config_map(config)
    if not object_cfg_map:
        raise SystemExit(
            f"No objects found in {config_path}. Expected pipelines[].objects[] "
            "(migration YAML) or files[] (Data Seeding JSON)."
        )

    run_dir = Path(args.run_dir) if args.run_dir else get_latest_run_dir()
    if not args.target_where and (not run_dir or not run_dir.exists()):
        raise SystemExit("No run directory found. Pass --run-dir or create an export run first.")

    object_names = args.objects or sorted(object_cfg_map.keys())
    unknown = [name for name in object_names if name not in object_cfg_map]
    if unknown:
        raise SystemExit(f"Object(s) not found in config: {', '.join(unknown)}")
    if args.target_where and not args.objects:
        raise SystemExit("Pass --object when using --target-where.")

    output_dir = Path(args.out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    source_dir = None
    if args.compare in {"source-stage", "source-target"}:
        if config_path.suffix.lower() != ".json" or not config.get("files"):
            raise SystemExit("Source comparisons require a Data Seeding flat-file template JSON config.")
        try:
            source_dir = resolve_data_seeding_source_dir(config, config_path, args.source_dir)
        except (ValueError, FileNotFoundError) as exc:
            raise SystemExit(str(exc)) from exc

    source, target = build_connectors()
    if args.target_where:
        source.connect()
        print(f"Source: {source.instance_url}")
    if args.compare != "source-stage":
        target.connect()

    if run_dir:
        print(f"Run dir: {run_dir}")
    if args.compare != "source-stage":
        print(f"Target: {target.instance_url}")
    if source_dir:
        print(f"Data Seeding source: {source_dir} ({args.source_kind})")
    print(
        f"Validation scope: {'full scan' if args.sample_size is None else f'sample of up to {args.sample_size:,} rows per object'} "
        f"| seed={args.sample_seed} | target query mode={effective_query_mode}"
    )

    overall_rows = 0
    overall_mismatched_rows = 0
    overall_missing = 0

    for object_name in object_names:
        if args.compare in {"source-stage", "source-target"}:
            source_stage_result = verify_source_to_stage(
                object_name=object_name,
                run_dir=run_dir,
                pipeline_id=args.pipeline,
                config=config,
                source_dir=source_dir,
                source_kind=args.source_kind,
                output_dir=output_dir,
                sample_size=args.sample_size,
                sample_seed=args.sample_seed,
            )
            print(
                f"[{object_name}] source-stage rows={source_stage_result.expected_rows} "
                f"matched={source_stage_result.matched_rows} "
                f"missing={source_stage_result.missing_in_target} "
                f"mismatched_rows={source_stage_result.mismatched_rows}"
            )
            if args.compare == "source-stage":
                result = source_stage_result
                overall_rows += result.expected_rows
                overall_mismatched_rows += result.mismatched_rows
                overall_missing += result.missing_in_target
                continue

        if args.target_where:
            result = verify_object_source_target_direct(
                object_name=object_name,
                object_cfg=object_cfg_map[object_name],
                object_cfg_map=object_cfg_map,
                source=source,
                target=target,
                output_dir=output_dir,
                target_where=args.target_where,
                print_soql=args.print_soql,
                sample_size=args.sample_size,
                sample_seed=args.sample_seed,
                query_mode=effective_query_mode,
            )
        else:
            result = verify_object(
                object_name=object_name,
                run_dir=run_dir,
                object_cfg=object_cfg_map[object_name],
                target=target,
                object_cfg_map=object_cfg_map,
                output_dir=output_dir,
                pipeline_id=args.pipeline,
                print_soql=args.print_soql,
                sample_size=args.sample_size,
                sample_seed=args.sample_seed,
                query_mode=effective_query_mode,
            )
        overall_rows += result.expected_rows
        overall_mismatched_rows += result.mismatched_rows
        overall_missing += result.missing_in_target

        print(
            f"[{result.object_name}] rows={result.expected_rows} "
            f"matched={result.matched_rows} "
            f"missing={result.missing_in_target} "
            f"mismatched_rows={result.mismatched_rows} "
            f"mismatched_fields={result.mismatched_fields}"
        )
        if result.skipped_fields:
            print(f"  skipped fields: {', '.join(result.skipped_fields)}")
        print(f"  summary:   {result.summary_file}")
        print(f"  mismatch:  {result.mismatch_file}")

    print(
        f"Verification complete | rows={overall_rows} "
        f"| missing={overall_missing} "
        f"| mismatched_rows={overall_mismatched_rows}"
    )


if __name__ == "__main__":
    main()
