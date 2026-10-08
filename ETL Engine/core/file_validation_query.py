"""Helpers for scalable file-based post-validation."""
from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

# User-facing size guards (split records / fewer files if exceeded).
MAX_BYTES_PER_CSV = 1 * 1024 * 1024 * 1024  # 1 GiB
MAX_BYTES_TOTAL_SOURCES = 10 * 1024 * 1024 * 1024  # 10 GiB

# Detailed output gates (high-level report always; full dump only when safe).
MAX_DETAILED_MISMATCH_ROWS = 100_000
MAX_EXCEL_MISMATCH_ROWS = 50_000
MAX_EXCEL_TARGET_ROWS = 50_000

# Keep generated report paths below the legacy Windows MAX_PATH boundary.  The
# application may run on a machine where long-path support is not enabled.
MAX_WINDOWS_OUTPUT_PATH = 259

FILE_VALIDATION_PROGRESS_NAME = "file_validation_progress.json"
FILE_VALIDATION_STAGES = (
    "load_source",
    "connect",
    "query_target",
    "compare",
    "write_reports",
)
# Base percent when each stage starts (stage work fills toward the next).
FILE_VALIDATION_STAGE_BASE_PERCENT = {
    "starting": 1,
    "load_source": 5,
    "connect": 38,
    "query_target": 48,
    "compare": 80,
    "write_reports": 92,
    "completed": 100,
    "failed": 100,
}


def discover_file_validation_csvs(path: str | Path) -> list[Path]:
    """
    Resolve a CSV file path or a folder of CSVs for the same object.

    Folder rules:
    - Include *.csv directly in the folder
    - Include *.csv in immediate child folders (one level only)
    """
    root = Path(path).expanduser().resolve()
    if root.is_file():
        if root.suffix.lower() != ".csv":
            raise ValueError(f"Expected a .csv file or a folder containing CSV files: {root}")
        return [root]
    if not root.is_dir():
        raise ValueError(f"CSV file or folder was not found: {path}")

    found: list[Path] = []
    for item in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if item.is_file() and item.suffix.lower() == ".csv":
            found.append(item.resolve())
            continue
        if item.is_dir():
            for child in sorted(item.iterdir(), key=lambda p: p.name.lower()):
                if child.is_file() and child.suffix.lower() == ".csv":
                    found.append(child.resolve())
    if not found:
        raise ValueError(
            f"No CSV files found in folder (or one level of subfolders): {root}"
        )
    return found


def enforce_source_size_limits(paths: Iterable[Path]) -> tuple[int, list[tuple[str, int]]]:
    """Raise if any file or the total exceeds configured byte limits."""
    details: list[tuple[str, int]] = []
    total = 0
    for path in paths:
        size = path.stat().st_size
        details.append((path.name, size))
        if size > MAX_BYTES_PER_CSV:
            raise ValueError(
                f"{path.name} is {size / (1024 ** 3):.2f} GiB, which exceeds the "
                f"{MAX_BYTES_PER_CSV / (1024 ** 3):.0f} GiB per-file limit. "
                "Split records into smaller CSV files and retry."
            )
        total += size
    if total > MAX_BYTES_TOTAL_SOURCES:
        raise ValueError(
            f"Selected sources total {total / (1024 ** 3):.2f} GiB, which exceeds the "
            f"{MAX_BYTES_TOTAL_SOURCES / (1024 ** 3):.0f} GiB run limit. "
            "Split into fewer/smaller files (by records) and run in batches."
        )
    return total, details


def parse_field_list(raw: str | Iterable[str] | None) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = raw.split(",")
    else:
        parts = list(raw)
    fields: list[str] = []
    for part in parts:
        name = str(part or "").strip()
        if name and name not in fields:
            fields.append(name)
    return fields


_SELECT_RE = re.compile(
    r"^\s*SELECT\s+(?P<select>.+?)\s+FROM\s+(?P<object>[A-Za-z_][A-Za-z0-9_]*)\b",
    re.IGNORECASE | re.DOTALL,
)
_SUBQUERY_RE = re.compile(r"\(\s*SELECT\b", re.IGNORECASE)
_FIELD_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")


def parse_custom_soql_select(soql: str) -> tuple[str, list[str]]:
    """
    Extract object name and SELECT field list from a custom SOQL query.

    Only the SELECT list is used for file-based validation. User WHERE / ORDER BY /
    LIMIT clauses are ignored; CSV match IDs always drive retrieval.
    """
    text = str(soql or "").strip()
    if not text:
        raise ValueError("Custom SOQL is required")
    if _SUBQUERY_RE.search(text):
        raise ValueError("Custom SOQL subqueries are not supported in file validation")
    match = _SELECT_RE.search(text)
    if not match:
        raise ValueError("Custom SOQL must start with SELECT ... FROM ObjectName")
    object_name = match.group("object").strip()
    select_clause = match.group("select").strip()
    if not select_clause or select_clause == "*":
        raise ValueError("Custom SOQL must list explicit fields (SELECT * is not supported)")
    fields: list[str] = []
    for part in select_clause.split(","):
        token = part.strip()
        # Strip optional aliases: "Name Alias" or "Name AS Alias"
        token = re.sub(r"\s+AS\s+\S+$", "", token, flags=re.IGNORECASE).strip()
        if " " in token:
            token = token.split()[0].strip()
        if not token:
            continue
        if not _FIELD_TOKEN_RE.match(token):
            raise ValueError(f"Unsupported SELECT expression in Custom SOQL: {part.strip()}")
        if token not in fields:
            fields.append(token)
    if not fields:
        raise ValueError("Custom SOQL SELECT list is empty")
    return object_name, fields


def apply_include_exclude(
    candidate_fields: Iterable[str],
    include_fields: Iterable[str] | None = None,
    exclude_fields: Iterable[str] | None = None,
    *,
    required_fields: Iterable[str] | None = None,
) -> tuple[list[str], list[str]]:
    """
    Filter candidate fields for Object + Template mode.

    Empty include => keep all candidates, then apply excludes.
    Non-empty include => keep only listed fields that appear in candidates (plus required),
    then apply excludes. Include entries missing from candidates are reported as skipped.
    Required fields are retained unless explicitly excluded.
    """
    candidates = list(dict.fromkeys(
        str(field).strip() for field in candidate_fields if str(field or "").strip()
    ))
    candidate_set = set(candidates)
    include = parse_field_list(include_fields)
    exclude = set(parse_field_list(exclude_fields))
    required = parse_field_list(required_fields)
    skipped: list[str] = []

    if include:
        selected: list[str] = []
        for field in include:
            if field in candidate_set:
                if field not in selected:
                    selected.append(field)
            else:
                skipped.append(field)
        for field in required:
            if field not in selected and field not in exclude:
                selected.append(field)
    else:
        selected = list(candidates)
        for field in required:
            if field not in selected and field not in exclude:
                selected.append(field)

    filtered = [field for field in selected if field not in exclude]
    return filtered, skipped


def discover_lookup_mapping_candidates(
    columns: Iterable[str],
    object_fields: Iterable[dict],
    parent_external_ids: dict[str, list[str]],
    *,
    include_fields: Iterable[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Build editable lookup mapping rows from CSV columns ∩ single-parent reference fields.

    Returns (candidates, skipped_polymorphic) where each candidate has:
    source_field, target_field, referenced_object, relationship_name,
    external_id_fields, recommended_external_id.
    """
    column_list = [str(col).strip() for col in columns if str(col or "").strip()]
    column_by_case = {col.casefold(): col for col in column_list}
    include = parse_field_list(include_fields)
    include_set = set(include)
    candidates: list[dict] = []
    skipped_polymorphic: list[dict] = []

    for field in object_fields:
        field_name = str(field.get("name") or "").strip()
        if not field_name or str(field.get("type") or "").lower() != "reference":
            continue
        source_name = column_by_case.get(field_name.casefold())
        if not source_name:
            continue
        if include_set and source_name not in include_set and field_name not in include_set:
            continue
        references = [str(ref).strip() for ref in (field.get("referenceTo") or []) if str(ref or "").strip()]
        relationship_name = str(field.get("relationshipName") or "").strip()
        if len(references) != 1 or not relationship_name:
            skipped_polymorphic.append({
                "source_field": source_name,
                "target_field": field_name,
                "referenced_objects": references,
                "reason": "polymorphic_or_missing_relationship",
            })
            continue
        referenced_object = references[0]
        ext_fields = list(dict.fromkeys(
            str(name).strip()
            for name in (parent_external_ids.get(referenced_object) or [])
            if str(name or "").strip()
        ))
        recommended = (
            "External_Id__c"
            if "External_Id__c" in ext_fields
            else (ext_fields[0] if ext_fields else "")
        )
        candidates.append({
            "source_field": source_name,
            "target_field": field_name,
            "referenced_object": referenced_object,
            "relationship_name": relationship_name,
            "external_id_fields": ext_fields,
            "recommended_external_id": recommended,
            "enabled": bool(recommended),
        })
    return candidates, skipped_polymorphic


def intersect_compare_fields(
    *,
    csv_fields: Iterable[str],
    target_fields: Iterable[str],
    match_key: str,
    source_key: str | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """
    Compare only CSV ∩ target fields (excluding match/source keys).

    Returns (compare_fields, csv_only_skipped, target_only_skipped).
    """
    csv_list = [
        field for field in dict.fromkeys(str(f).strip() for f in csv_fields if str(f or "").strip())
        if field not in {match_key, source_key or ""}
    ]
    target_list = [
        field for field in dict.fromkeys(str(f).strip() for f in target_fields if str(f or "").strip())
        if field not in {match_key, "Id"}
    ]
    target_set = set(target_list)
    csv_set = set(csv_list)
    compare_fields = [field for field in csv_list if field in target_set]
    csv_only = [field for field in csv_list if field not in target_set]
    target_only = [field for field in target_list if field not in csv_set]
    return compare_fields, csv_only, target_only


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_output_stem(name: str) -> str:
    stem = Path(name).stem or "file"
    cleaned = _SAFE_NAME_RE.sub("_", stem).strip("._") or "file"
    return cleaned[:80]


def safe_output_component(name: str) -> str:
    """Sanitize a field/path component without treating dots as file suffixes."""
    cleaned = _SAFE_NAME_RE.sub("_", str(name or "")).strip("._") or "field"
    return cleaned[:120]


def bounded_output_path(
    directory: Path,
    filename: str,
    *,
    max_path: int = MAX_WINDOWS_OUTPUT_PATH,
) -> Path:
    """Return a deterministic report path that fits the Windows path budget."""
    directory = directory.resolve()
    available = max_path - len(str(directory)) - 1
    if len(filename) <= available:
        return directory / filename

    suffix = Path(filename).suffix
    digest = hashlib.sha1(filename.encode("utf-8")).hexdigest()[:10]
    marker = f"__{digest}{suffix}"
    prefix_length = available - len(marker)
    if prefix_length < 1:
        raise ValueError(
            "Generated validation report directory is too long for Windows "
            f"({len(str(directory))} characters; path limit {max_path}). "
            "Shorten the project/output path, then retry. No validation processing was started."
        )
    stem = filename[:-len(suffix)] if suffix else filename
    return directory / f"{stem[:prefix_length]}{marker}"


def validate_file_validation_output_paths(
    out_dir: Path,
    *,
    object_name: str,
    source_names: Iterable[str],
    field_names: Iterable[str],
    max_path: int = MAX_WINDOWS_OUTPUT_PATH,
) -> None:
    """Reject a run when a generated report path would exceed Windows MAX_PATH.

    This is intentionally a preflight approximation: the timestamp placeholder
    has the same length as the timestamp used by the report writer, and checking
    every CSV header covers any field that can later produce a mismatch file.
    """
    stamp = "00000000_000000"
    root = Path(out_dir).resolve()
    candidates: list[tuple[Path, str, str]] = []
    fields = list(dict.fromkeys([*field_names, "__row__"]))
    for source_name in dict.fromkeys(str(name) for name in source_names):
        source_stem = safe_output_stem(source_name)
        candidates.append((
            root / "retrieved_target_by_source" /
            f"{object_name}__{source_stem}__target_{stamp}.csv",
            source_name,
            "target report",
        ))
        for field_name in fields:
            source_dir = root / "mismatches_by_source_and_field" / source_stem
            candidates.append((
                bounded_output_path(
                    source_dir,
                    f"{object_name}__{source_stem}__"
                    f"{safe_output_component(field_name)}__mismatches_{stamp}.csv",
                    max_path=max_path,
                ),
                source_name,
                field_name,
            ))

    if not candidates:
        return
    path, source_name, field_name = max(candidates, key=lambda item: len(str(item[0])))
    length = len(str(path))
    if length > max_path:
        over_by = length - max_path
        raise ValueError(
            "Generated validation report path would be too long for Windows "
            f"({length} characters; limit {max_path}). Source '{source_name}', "
            f"field '{field_name}' exceeds the limit by {over_by} characters. "
            "Rename the source CSV to a shorter name or shorten the project path, then retry. "
            "No validation processing was started."
        )


def summarize_mismatches_by_field(
    differences: list[dict[str, str]],
    *,
    expected_rows: int,
) -> list[dict[str, str]]:
    """Build a high-level per-field mismatch ranking for discovery."""
    field_counts: Counter[str] = Counter()
    issue_counts: Counter[str] = Counter()
    for row in differences:
        field = str(row.get("field") or "").strip() or "__unknown__"
        issue = str(row.get("issue") or "").strip() or "unknown"
        if field == "__row__":
            issue_counts[issue] += 1
            continue
        if issue == "value_mismatch":
            field_counts[field] += 1
    summary: list[dict[str, str]] = []
    for field, count in field_counts.most_common():
        pct = (100.0 * count / expected_rows) if expected_rows else 0.0
        summary.append({
            "field": field,
            "mismatch_count": str(count),
            "compared_rows": str(expected_rows),
            "pct_of_rows": f"{pct:.2f}",
            "hint": "likely_systematic" if expected_rows and count >= max(1, int(expected_rows * 0.9)) else "review",
        })
    for issue, count in sorted(issue_counts.items()):
        summary.append({
            "field": f"__row__:{issue}",
            "mismatch_count": str(count),
            "compared_rows": str(expected_rows),
            "pct_of_rows": f"{(100.0 * count / expected_rows) if expected_rows else 0.0:.2f}",
            "hint": "row_level",
        })
    return summary


def write_file_wise_target_csvs(
    out_dir: Path,
    *,
    object_name: str,
    target_key: str,
    retrieval_fields: list[str],
    target_rows: dict[str, dict[str, str]],
    ids_by_source_file: dict[str, list[str]],
    stamp: str,
) -> list[Path]:
    """Write one retrieved-target CSV per source file using the local ID map."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ordered_fields = list(dict.fromkeys(["Id", target_key, *retrieval_fields]))
    written: list[Path] = []
    for source_name, ids in sorted(ids_by_source_file.items(), key=lambda item: item[0].lower()):
        path = bounded_output_path(
            out_dir,
            f"{object_name}__{safe_output_stem(source_name)}__target_{stamp}.csv",
        )
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=ordered_fields, extrasaction="ignore")
            writer.writeheader()
            for match_id in dict.fromkeys(ids):
                row = target_rows.get(match_id)
                if not row:
                    continue
                writer.writerow({field: row.get(field, "") for field in ordered_fields})
        written.append(path)
    return written


def write_partitioned_mismatch_csvs(
    out_dir: Path,
    *,
    object_name: str,
    differences: list[dict[str, str]],
    stamp: str,
    max_open_files: int = 32,
    max_path: int = MAX_WINDOWS_OUTPUT_PATH,
) -> tuple[list[Path], Path]:
    """Write mismatch rows partitioned by source file, then mismatch field.

    A bounded LRU of file handles prevents runs with many source/field
    combinations from exhausting the process file-handle limit.
    """
    root = out_dir / "mismatches_by_source_and_field"
    root.mkdir(parents=True, exist_ok=True)
    headers = ["object_name", "source_file", "external_id", "field", "issue", "expected", "actual"]
    counts: Counter[tuple[str, str]] = Counter()
    paths: dict[tuple[str, str], Path] = {}
    open_handles: OrderedDict[Path, tuple[object, csv.DictWriter]] = OrderedDict()

    try:
        for row in differences:
            source_name = str(row.get("source_file") or "__unknown_source__")
            field_name = str(row.get("field") or "__unknown_field__")
            key = (source_name, field_name)
            path = paths.get(key)
            if path is None:
                source_dir = root / safe_output_stem(source_name)
                source_dir.mkdir(parents=True, exist_ok=True)
                path = bounded_output_path(
                    source_dir,
                    f"{object_name}__{safe_output_stem(source_name)}__"
                    f"{safe_output_component(field_name)}__mismatches_{stamp}.csv",
                    max_path=max_path,
                )
                paths[key] = path

            opened = open_handles.pop(path, None)
            if opened is None:
                is_new = not path.exists()
                handle = path.open("a", encoding="utf-8", newline="")
                writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
                if is_new:
                    writer.writeheader()
                opened = (handle, writer)
            open_handles[path] = opened
            opened[1].writerow({**row, "source_file": source_name})
            counts[key] += 1

            if len(open_handles) > max(1, max_open_files):
                _, (old_handle, _) = open_handles.popitem(last=False)
                old_handle.close()
    finally:
        for handle, _ in open_handles.values():
            handle.close()

    index_path = root / f"{object_name}_mismatch_partitions_{stamp}.csv"
    with index_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["source_file", "field", "mismatch_count", "partition_csv"],
        )
        writer.writeheader()
        for key in sorted(paths, key=lambda item: (item[0].lower(), item[1].lower())):
            writer.writerow({
                "source_file": key[0],
                "field": key[1],
                "mismatch_count": counts[key],
                "partition_csv": str(paths[key]),
            })
    return [paths[key] for key in sorted(paths)], index_path


def detailed_outputs_allowed(
    *,
    mismatch_rows: int,
    target_rows: int,
) -> dict[str, bool]:
    return {
        "write_mismatch_csv": mismatch_rows <= MAX_DETAILED_MISMATCH_ROWS,
        "write_excel": (
            mismatch_rows <= MAX_EXCEL_MISMATCH_ROWS
            and target_rows <= MAX_EXCEL_TARGET_ROWS
        ),
    }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stage_index(phase: str) -> int:
    try:
        return FILE_VALIDATION_STAGES.index(phase) + 1
    except ValueError:
        if phase == "completed":
            return len(FILE_VALIDATION_STAGES)
        return 0


def file_validation_progress_percent(
    phase: str,
    *,
    stage_fraction: float = 0.0,
) -> int:
    """Map a phase (+ optional 0..1 fraction within the phase) to 1..99 or 100."""
    phase_key = str(phase or "").strip().lower() or "starting"
    if phase_key in {"completed", "failed"}:
        return 100
    base = int(FILE_VALIDATION_STAGE_BASE_PERCENT.get(phase_key, 1))
    if phase_key not in FILE_VALIDATION_STAGES:
        return max(1, min(99, base))
    idx = FILE_VALIDATION_STAGES.index(phase_key)
    next_base = (
        FILE_VALIDATION_STAGE_BASE_PERCENT["completed"]
        if idx + 1 >= len(FILE_VALIDATION_STAGES)
        else FILE_VALIDATION_STAGE_BASE_PERCENT[FILE_VALIDATION_STAGES[idx + 1]]
    )
    fraction = max(0.0, min(1.0, float(stage_fraction or 0.0)))
    value = base + (next_base - base) * fraction
    return max(1, min(99, int(round(value))))


def build_file_validation_progress_message(payload: dict) -> str:
    phase = str(payload.get("phase") or "").strip().lower()
    object_name = str(payload.get("object_name") or "object").strip() or "object"
    current_file = str(payload.get("current_file") or "").strip()
    rows = payload.get("rows_processed")
    unique_ids = payload.get("unique_ids")
    total_files = int(payload.get("total_files") or 0)
    current_index = int(payload.get("current_index") or 0)
    detail = str(payload.get("detail") or "").strip()

    if phase == "starting":
        return f"Starting file validation for {object_name}"
    if phase == "load_source":
        prefix = (
            f"Loading source file {current_index} of {total_files}: {current_file}"
            if total_files and current_file
            else f"Loading source CSV for {object_name}"
        )
        if rows is not None:
            unique_part = f", {int(unique_ids):,} unique" if unique_ids is not None else ""
            return f"{prefix} - {int(rows):,} rows{unique_part}"
        return prefix
    if phase == "connect":
        return detail or f"Connecting to target and describing {object_name}"
    if phase == "query_target":
        keys = payload.get("expected_keys")
        if detail:
            return detail
        if keys is not None:
            return f"Querying target {object_name} ({int(keys):,} match keys)"
        return f"Querying target {object_name}"
    if phase == "compare":
        return detail or f"Comparing source vs target for {object_name}"
    if phase == "write_reports":
        return detail or f"Writing validation reports for {object_name}"
    if phase == "completed":
        return detail or f"File validation completed for {object_name}"
    if phase == "failed":
        return detail or f"File validation failed for {object_name}"
    return detail or f"File validation running for {object_name}"


def write_file_validation_progress(out_dir: Path, payload: dict) -> Path:
    """Atomic-ish progress heartbeat consumed by the validation progress UI."""
    out_dir.mkdir(parents=True, exist_ok=True)
    phase = str(payload.get("phase") or "starting")
    stage_fraction = float(payload.get("stage_fraction") or 0.0)
    percent = payload.get("progress_percent")
    if percent is None:
        percent = file_validation_progress_percent(phase, stage_fraction=stage_fraction)
    body = {
        **payload,
        "phase": phase,
        "status": str(payload.get("status") or ("COMPLETED" if phase == "completed" else "RUNNING")),
        "progress_percent": int(percent),
        "completed_units": int(payload.get("completed_units") or max(0, stage_index(phase) - (0 if phase == "completed" else 1))),
        "total_units": int(payload.get("total_units") or len(FILE_VALIDATION_STAGES)),
        "progress_unit": "stages",
        "message": str(payload.get("message") or build_file_validation_progress_message(payload)),
        "updated_at": _utc_now_iso(),
    }
    if phase == "completed":
        body["completed_units"] = body["total_units"]
        body["status"] = "COMPLETED"
    path = out_dir / FILE_VALIDATION_PROGRESS_NAME
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return path


def read_file_validation_progress(out_dir: Path | None) -> dict | None:
    if not out_dir or not out_dir.is_dir():
        return None
    path = out_dir / FILE_VALIDATION_PROGRESS_NAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None
