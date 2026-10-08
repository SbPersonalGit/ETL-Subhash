"""Flat-file input adapter for the shared Salesforce ETL staging pipeline."""

from __future__ import annotations

import codecs
import csv
import json
import re
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, TextIO

import polars as pl

from core.stager import CHUNK_SIZE, Stager


class FlatFileValidationError(ValueError):
    """Raised when a flat-file package is unsafe to stage."""


# Windows / Excel CSV exports are often CP1252; latin-1 is the lossless fallback.
_CSV_ENCODING_CANDIDATES = ("utf-8-sig", "cp1252", "latin-1")
_encoding_cache: dict[tuple[str, int, int], str] = {}

_RECORD_TYPE_DEVELOPER_COLUMNS = {
    "recordtype.developername",
    "recordtypedevelopername",
    "recordtype_developername",
}


def record_type_source_column(columns: Iterable[str]) -> str:
    """Prefer RecordTypeId, otherwise recognize common DeveloperName headers."""
    normalized = [(str(column).strip(), str(column).strip().casefold()) for column in columns]
    exact_id = next((column for column, folded in normalized if folded == "recordtypeid"), "")
    if exact_id:
        return exact_id
    return next(
        (column for column, folded in normalized if folded in _RECORD_TYPE_DEVELOPER_COLUMNS),
        "",
    )


def safe_name(value: str, fallback: str = "item") -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", (value or "").strip()).strip("._")
    return normalized or fallback


def detect_csv_encoding(path: str | Path) -> str:
    """Return the first encoding that can decode the whole CSV.

    Prefer UTF-8 (with BOM), then Windows CP1252 (common Salesforce/Excel
    exports with accented names), then latin-1 as a never-fail fallback.
    """
    resolved = Path(path).resolve()
    stat = resolved.stat()
    cache_key = (str(resolved), int(stat.st_size), int(stat.st_mtime_ns))
    cached = _encoding_cache.get(cache_key)
    if cached:
        return cached

    chosen = "latin-1"
    for encoding in _CSV_ENCODING_CANDIDATES:
        decoder = codecs.getincrementaldecoder(encoding)()
        try:
            with resolved.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        decoder.decode(b"", final=True)
                        break
                    decoder.decode(chunk)
            chosen = encoding
            break
        except UnicodeDecodeError:
            continue

    _encoding_cache[cache_key] = chosen
    return chosen


def iter_csv_batches(
    path: str | Path,
    schema_overrides: dict,
    batch_size: int,
):
    """Yield CSV row batches. Polars 2 removed read_csv_batched."""
    frame = pl.scan_csv(
        str(path),
        infer_schema_length=0,
        schema_overrides=schema_overrides,
        null_values=[""],
        encoding=polars_csv_encoding(path),
    )
    yield from frame.collect_batches(
        chunk_size=batch_size,
        maintain_order=True,
        engine="streaming",
    )


def polars_csv_encoding(path: str | Path | None = None, encoding: str | None = None) -> str:
    """Map a Python codec name to a Polars CSV encoding value."""
    resolved = encoding or (detect_csv_encoding(path) if path is not None else "utf-8-sig")
    if resolved in {"utf-8", "utf-8-sig"}:
        return "utf8"
    if resolved in {"cp1252", "windows-1252"}:
        return "cp1252"
    if resolved in {"latin-1", "iso-8859-1"}:
        return "latin1"
    return "utf8"


@contextmanager
def open_csv(path: str | Path, mode: str = "r") -> Iterator[TextIO]:
    """Open a CSV for text reading using detected source encoding."""
    encoding = detect_csv_encoding(path)
    with Path(path).open(mode, encoding=encoding, newline="") as handle:
        yield handle


def read_csv_header(path: str | Path) -> list[str]:
    with open_csv(path) as handle:
        header = next(csv.reader(handle), [])
    headers = [str(column).strip() for column in header]
    while headers and not headers[-1]:
        headers.pop()
    return headers


def usable_csv_headers(headers: Iterable[str]) -> list[str]:
    """Named CSV headers only. Blank Excel extra columns are ignored."""
    return [str(column).strip() for column in headers if str(column or "").strip()]


def require_named_csv_headers(headers: Iterable[str], filename: str) -> list[str]:
    named = usable_csv_headers(headers)
    if not named:
        raise FlatFileValidationError(f"{filename}: header row is empty or contains blank columns.")
    if len(named) != len(set(named)):
        raise FlatFileValidationError(f"{filename}: duplicate column names are not supported.")
    return named


def resolve_flat_file_path(
    file_config: dict,
    upload_root: str | Path,
    drop_root: str | Path | None = None,
    *,
    prefer_cleaned: bool = True,
) -> Path:
    """Resolve a stored upload or a zero-copy drop-folder reference safely."""
    upload_root = Path(upload_root).resolve()
    raw_cleaned_name = str(file_config.get("cleaned_name") or "").strip()
    cleaned_name = safe_name(raw_cleaned_name) if raw_cleaned_name else ""
    if prefer_cleaned and cleaned_name:
        candidate = (upload_root / cleaned_name).resolve()
        if candidate.parent != upload_root:
            raise FlatFileValidationError(f"Invalid cleaned filename: {cleaned_name}")
        return candidate

    if str(file_config.get("source_type") or "").strip() == "drop_folder":
        if drop_root is None:
            raise FlatFileValidationError("Drop-folder root is required for this file reference.")
        root = Path(drop_root).resolve()
        relative = Path(str(file_config.get("source_path") or ""))
        if not str(relative) or relative.is_absolute() or ".." in relative.parts:
            raise FlatFileValidationError("Invalid drop-folder source path.")
        candidate = (root / relative).resolve()
        if not candidate.is_relative_to(root):
            raise FlatFileValidationError("Drop-folder source path escapes the configured root.")
        return candidate

    raw_stored_name = str(file_config.get("stored_name") or file_config.get("file") or "").strip()
    if not raw_stored_name:
        raise FlatFileValidationError("Flat-file stored name is missing.")
    stored_name = safe_name(raw_stored_name)
    candidate = (upload_root / stored_name).resolve()
    if candidate.parent != upload_root:
        raise FlatFileValidationError(f"Invalid uploaded filename: {stored_name}")
    return candidate


def validate_flat_file_reference(path: Path, file_config: dict) -> None:
    """Reject a drop-folder source whose content changed after registration.

    Size changes are treated as content changes and require re-import.
    mtime-only drift is common on OneDrive/cloud-synced folders and is refreshed
    in place when the file size is unchanged. Prepared clean artifacts are
    derived upload files, so the original drop-folder fingerprint does not
    apply to them.
    """
    if str(file_config.get("source_type") or "") != "drop_folder" or not path.is_file():
        return
    cleaned_name = safe_name(str(file_config.get("cleaned_name") or ""))
    if cleaned_name and path.name == cleaned_name:
        return
    stat = path.stat()
    expected_size = file_config.get("source_size", file_config.get("size"))
    expected_mtime = file_config.get("source_mtime_ns")
    if expected_size is not None and int(expected_size) != stat.st_size:
        raise FlatFileValidationError(
            f"Drop-folder file changed size after template registration: {path.name}. Re-import it."
        )
    if expected_mtime is not None and int(expected_mtime) != stat.st_mtime_ns:
        # Same size + different mtime: metadata retouch (sync/antivirus), not content.
        file_config["source_mtime_ns"] = int(stat.st_mtime_ns)
        if "size" in file_config:
            file_config["size"] = int(stat.st_size)
        if "source_size" in file_config:
            file_config["source_size"] = int(stat.st_size)


def detect_object_from_filename(filename: str, object_names: Iterable[str]) -> str | None:
    """Return an exact API-name match for the filename stem (case-insensitive)."""
    stem = Path(str(filename or "")).stem.strip().casefold()
    if not stem:
        return None
    by_name = {
        str(object_name).strip().casefold(): str(object_name).strip()
        for object_name in object_names
        if str(object_name).strip()
    }
    return by_name.get(stem)


def detect_object_from_path(
    relative_path: str | Path, object_names: Iterable[str]
) -> tuple[str | None, str]:
    """Detect a Salesforce object from a drop-folder relative path.

    Preference order:
    1. Nearest parent folder whose name exactly matches an object API name
       (supports split files under ``Account/part1.csv``, ``Account/batch_2.csv``).
    2. CSV filename stem (existing single-file behavior).

    Returns ``(object_api_name, source)`` where source is ``\"folder\"``,
    ``\"filename\"``, or ``\"\"`` when unresolved.
    """
    path = Path(str(relative_path or ""))
    by_name = {
        str(object_name).strip().casefold(): str(object_name).strip()
        for object_name in object_names
        if str(object_name).strip()
    }
    for folder in reversed(path.parts[:-1]):
        match = by_name.get(str(folder).strip().casefold())
        if match:
            return match, "folder"
    filename_match = detect_object_from_filename(path.name, object_names)
    if filename_match:
        return filename_match, "filename"
    return None, ""


def clone_mapping_for_headers(source_file: dict, headers: Iterable[str]) -> list[dict]:
    """Copy one file's mapping policy onto another CSV's header row.

    Matching source columns inherit the template mapping. Extra columns that
    are not in the template are ignored so the folder keeps one mapping.
    """
    by_source = {
        str(mapping.get("source") or "").strip().casefold(): dict(mapping)
        for mapping in (source_file.get("mappings") or [])
        if str(mapping.get("source") or "").strip()
    }
    cloned: list[dict] = []
    for raw_header in headers:
        header = str(raw_header or "").strip()
        if not header:
            continue
        shared = by_source.get(header.casefold())
        if shared:
            mapping = {**shared, "source": header}
            cloned.append(mapping)
            continue
        cloned.append({
            "source": header,
            "target": None,
            "ignored": True,
            "status": "ignored",
            "unmapped_reason": "Column is not in the shared folder mapping template.",
        })
    return cloned


def fill_missing_sibling_mappings(files: Iterable[dict]) -> int:
    """Copy a mapped sample file onto same-object files that have no mappings yet."""
    groups: dict[str, list[dict]] = {}
    for file_config in files:
        object_name = str(file_config.get("object") or "").strip()
        if object_name:
            groups.setdefault(object_name, []).append(file_config)
    filled = 0
    for object_files in groups.values():
        source = next(
            (
                item
                for item in object_files
                if isinstance(item.get("mappings"), list) and item.get("mappings")
            ),
            None,
        )
        if source is None:
            continue
        for file_config in object_files:
            if file_config is source:
                continue
            if isinstance(file_config.get("mappings"), list) and file_config.get("mappings"):
                continue
            headers = [
                str(column or "").strip()
                for column in (file_config.get("columns") or [])
                if str(column or "").strip()
            ]
            if not headers:
                headers = [
                    str(mapping.get("source") or "").strip()
                    for mapping in source.get("mappings") or []
                    if str(mapping.get("source") or "").strip()
                ]
            file_config["mappings"] = clone_mapping_for_headers(source, headers)
            if not file_config.get("external_id"):
                file_config["external_id"] = source.get("external_id") or ""
            if not file_config.get("record_type_mappings"):
                file_config["record_type_mappings"] = dict(source.get("record_type_mappings") or {})
            filled += 1
    return filled


def list_same_folder_drop_csvs(drop_root: str | Path, relative_file: str) -> list[str]:
    """Return other CSV files in the same drop-folder directory as ``relative_file``."""
    root = Path(drop_root).resolve()
    relative = Path(str(relative_file or "").replace("\\", "/"))
    if not str(relative) or relative.is_absolute() or ".." in relative.parts:
        raise FlatFileValidationError("Invalid drop-folder source path.")
    folder = (root / relative.parent).resolve()
    if not folder.is_dir() or not folder.is_relative_to(root):
        raise FlatFileValidationError("Drop-folder directory was not found.")
    siblings: list[str] = []
    source_name = relative.name.casefold()
    for path in sorted(folder.glob("*.csv")):
        if not path.is_file():
            continue
        if path.name.casefold() == source_name:
            continue
        siblings.append(str(path.relative_to(root)).replace("\\", "/"))
    return siblings


def build_folder_mapping_files(
    drop_root: str | Path,
    source_file: dict,
    existing_files: Iterable[str] | None = None,
    object_names: Iterable[str] | None = None,
) -> dict:
    """Build mapped file entries for other CSVs in the same drop-folder directory."""
    source_path = str(
        source_file.get("source_path") or source_file.get("file") or ""
    ).replace("\\", "/").strip()
    if not source_path:
        raise FlatFileValidationError("The mapping file is missing a drop-folder path.")
    if not (source_file.get("mappings") or []):
        raise FlatFileValidationError("Map the sample file before applying it to the folder.")
    existing = {
        str(name or "").replace("\\", "/").strip().casefold()
        for name in (existing_files or [])
        if str(name or "").strip()
    }
    existing.add(source_path.casefold())
    object_name = str(source_file.get("object") or "").strip()
    added: list[dict] = []
    skipped: list[dict] = []
    for relative_name in list_same_folder_drop_csvs(drop_root, source_path):
        if relative_name.casefold() in existing:
            skipped.append({"file": relative_name, "reason": "already_in_template"})
            continue
        if object_names is not None:
            detected_object, detection_source = detect_object_from_path(
                relative_name, object_names
            )
            if detected_object and object_name and detected_object != object_name:
                skipped.append({
                    "file": relative_name,
                    "reason": f"detected object {detected_object}",
                })
                continue
        else:
            detection_source = source_file.get("object_detection_source") or "folder"
            detected_object = object_name
        path = (Path(drop_root) / relative_name).resolve()
        headers = require_named_csv_headers(read_csv_header(path), relative_name)
        stat = path.stat()
        added.append({
            "file": relative_name,
            "stored_name": safe_name(Path(relative_name).name),
            "source_type": "drop_folder",
            "source_path": relative_name,
            "columns": headers,
            "size": stat.st_size,
            "source_size": stat.st_size,
            "source_mtime_ns": stat.st_mtime_ns,
            "object": object_name,
            "object_auto_detected": bool(object_name),
            "object_detection_source": detection_source or source_file.get("object_detection_source") or "",
            "record_type_values": list(source_file.get("record_type_values") or []),
            "record_type_mappings": dict(source_file.get("record_type_mappings") or {}),
            "external_id": source_file.get("external_id") or "",
            "mappings": clone_mapping_for_headers(source_file, headers),
            "mapping_template_file": source_path,
            "collapsed": True,
        })
    folder = str(Path(source_path).parent).replace("\\", "/")
    if folder in {"", "."}:
        folder = "(drop folder root)"
    return {
        "folder": folder,
        "source_file": source_path,
        "added": added,
        "skipped": skipped,
    }


def read_distinct_csv_values(
    path: str | Path, column_name: str, limit: int = 200
) -> list[str]:
    """Read bounded, nonblank distinct values from one CSV column."""
    values: set[str] = set()
    with open_csv(path) as handle:
        for row in csv.DictReader(handle):
            value = str(row.get(column_name) or "").strip()
            if value:
                values.add(value)
                if len(values) > limit:
                    raise FlatFileValidationError(
                        f"{Path(path).name}: '{column_name}' has more than {limit} distinct "
                        "values. Split the file or reduce the Record Type set."
                    )
    return sorted(values)


def auto_map_columns(columns: Iterable[str], target_fields: Iterable[dict | str]) -> list[dict]:
    """Map by exact, case-insensitive API name. Labels are intentionally ignored."""
    field_by_folded_name: dict[str, dict] = {}
    for raw_field in target_fields:
        field = {"name": raw_field} if isinstance(raw_field, str) else dict(raw_field)
        name = str(field.get("name") or "").strip()
        if name:
            field_by_folded_name[name.casefold()] = field

    columns = list(columns)
    record_type_source = record_type_source_column(columns)
    mappings = []
    for raw_column in columns:
        column = str(raw_column).strip()
        target = field_by_folded_name.get(column.casefold())
        developer_name_mapping = (
            column == record_type_source
            and column.casefold() != "recordtypeid"
            and field_by_folded_name.get("recordtypeid")
        )
        if not target and developer_name_mapping:
            target = field_by_folded_name["recordtypeid"]
        mappings.append({
            "source": column,
            "target": target.get("name") if target else None,
            "status": "record_type_developer_name" if developer_name_mapping else "auto_mapped" if target else "unmapped",
            "ignored": False,
        })
    return mappings


def apply_unmapped_column_policy(file_config: dict, allow_unmapped: bool) -> int:
    """Mark unresolved columns ignored when the user explicitly permits exclusion."""
    if not allow_unmapped:
        return 0
    ignored_count = 0
    for mapping in file_config.get("mappings") or []:
        if mapping.get("ignored") or str(mapping.get("target") or "").strip():
            continue
        mapping["ignored"] = True
        mapping["status"] = "ignored_by_package_policy"
        ignored_count += 1
    return ignored_count


def validate_file_config(file_config: dict, target_fields: Iterable[dict | str] = ()) -> list[str]:
    errors: list[str] = []
    filename = str(file_config.get("stored_name") or file_config.get("file") or "").strip()
    object_name = str(file_config.get("object") or "").strip()
    mappings = file_config.get("mappings") or []
    if not filename:
        errors.append("File is required.")
    if not object_name:
        errors.append(f"{filename or 'File'}: target object is required.")
    if not isinstance(mappings, list) or not mappings:
        errors.append(f"{filename or 'File'}: field mappings are required.")
        return errors

    known_fields = {
        (field if isinstance(field, str) else field.get("name"))
        for field in target_fields
    }
    known_fields.discard(None)
    used_targets: set[str] = set()
    for mapping in mappings:
        source = str(mapping.get("source") or "").strip()
        target = str(mapping.get("target") or "").strip()
        ignored = bool(mapping.get("ignored"))
        if not source:
            errors.append(f"{filename}: a mapping has no source column.")
        if ignored:
            continue
        if not target:
            errors.append(f"{filename}: column '{source}' is neither mapped nor ignored.")
            continue
        if known_fields and target not in known_fields:
            errors.append(f"{filename}: target field '{target}' does not exist or is not writable.")
        if target in used_targets:
            errors.append(f"{filename}: target field '{target}' is mapped more than once.")
        used_targets.add(target)

    record_type_values = [
        str(value).strip()
        for value in file_config.get("record_type_values") or []
        if str(value).strip()
    ]
    record_type_mappings = {
        str(source).strip(): str(target).strip()
        for source, target in (file_config.get("record_type_mappings") or {}).items()
        if str(source).strip() and str(target).strip()
    }
    missing_record_types = [
        source_id for source_id in record_type_values if source_id not in record_type_mappings
    ]
    if missing_record_types:
        errors.append(
            f"{filename}: map every source RecordTypeId before saving "
            f"({', '.join(missing_record_types)})."
        )
    return errors


def validate_flat_file_external_id(
    file_config: dict,
    target_fields: Iterable[dict | str],
) -> list[str]:
    """Validate the source-ID to target External ID prerequisite for flat files.

    A source ``Id`` is retained as the migration key and copied to the selected
    target External ID during transformation. Catch missing target metadata at
    file-cleaning time instead of waiting for pipeline execution.
    """
    filename = str(file_config.get("file") or file_config.get("stored_name") or "File").strip()
    object_name = str(file_config.get("object") or "target object").strip()
    source_columns = {
        str(mapping.get("source") or "").strip().casefold()
        for mapping in file_config.get("mappings") or []
    }
    if "id" not in source_columns:
        return []

    field_metadata = [field for field in target_fields if isinstance(field, dict)]
    candidates = sorted({
        str(field.get("name") or "").strip()
        for field in field_metadata
        if field.get("name")
        and field.get("externalId")
        and (field.get("createable") or field.get("updateable"))
    })
    configured = str(file_config.get("external_id") or "").strip()
    if not candidates:
        return [
            f"{filename}: Target {object_name} has no writable External ID fields. "
            "Create or deploy one (for example External_Id__c as Text, External ID, "
            "and Unique) before validating this file."
        ]
    if not configured:
        return [
            f"{filename}: Select the target External ID for {object_name} before "
            f"validating this file. Available fields: {', '.join(candidates)}."
        ]
    if configured not in candidates:
        return [
            f"{filename}: Configured external ID '{configured}' is not a writable "
            f"External ID on target {object_name}. Available fields: {', '.join(candidates)}."
        ]
    return []


@dataclass
class FlatFileIngestResult:
    object_counts: dict[str, int]
    file_counts: dict[str, int]


class FlatFileTemplateStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def save(self, name: str, payload: dict) -> dict:
        template_id = safe_name(name, "flat_file_template")
        self.root.mkdir(parents=True, exist_ok=True)
        record = dict(payload)
        self._normalize_lookup_resolution(record)
        record.update({"id": template_id, "name": name, "input_mode": "flat_file"})
        (self.root / f"{template_id}.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return record

    def get(self, template_id: str) -> dict:
        if safe_name(template_id) != template_id:
            raise FileNotFoundError("Invalid flat-file template ID.")
        path = self.root / f"{template_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Flat-file template '{template_id}' was not found.")
        record = json.loads(path.read_text(encoding="utf-8"))
        self._normalize_lookup_resolution(record)
        return record

    def delete(self, template_id: str) -> None:
        if safe_name(template_id) != template_id:
            raise FileNotFoundError("Invalid flat-file template ID.")
        path = (self.root / f"{template_id}.json").resolve()
        if path.parent != self.root.resolve() or not path.is_file():
            raise FileNotFoundError(f"Flat-file template '{template_id}' was not found.")
        path.unlink()

    @staticmethod
    def _normalize_lookup_resolution(record: dict) -> None:
        """Make lookup resolution authoritative over stale UI metadata."""
        for file_config in record.get("files") or []:
            for mapping in file_config.get("mappings") or []:
                if str(mapping.get("lookup_resolution") or "").strip() == "target_id":
                    mapping["lookup_external_id"] = ""

    def list(self) -> list[dict]:
        self.root.mkdir(parents=True, exist_ok=True)
        records = []
        for path in sorted(self.root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                object_names = sorted({
                    str(file_config.get("object") or "").strip()
                    for file_config in payload.get("files") or []
                    if str(file_config.get("object") or "").strip()
                    and not file_config.get("skip_staging")
                })
                active_files = [
                    file_config
                    for file_config in payload.get("files") or []
                    if not file_config.get("skip_staging")
                ]
                records.append({
                    "id": payload.get("id") or path.stem,
                    "name": payload.get("name") or path.stem,
                    "file_count": len(active_files),
                    "object_count": len(object_names),
                    "objects": object_names,
                    "input_mode": "flat_file",
                    "target_profile": payload.get("target_profile") or "default",
                    "target_domain": str(payload.get("target_domain") or "").strip(),
                    "source_domain": "",
                    "prevalidation_status": payload.get("prevalidation_status") or "DRAFT",
                })
            except (OSError, ValueError):
                continue
        return records


class FlatFileInput:
    """Normalize one or more CSV files into existing raw DuckDB staging tables."""

    def __init__(
        self,
        stager: Stager,
        upload_root: str | Path,
        drop_root: str | Path | None = None,
    ):
        self.stager = stager
        self.upload_root = Path(upload_root).resolve()
        self.drop_root = Path(drop_root).resolve() if drop_root is not None else None

    def ingest(self, template: dict, replace: bool = True) -> FlatFileIngestResult:
        files = [
            config
            for config in (template.get("files") or [])
            if not config.get("skip_staging")
        ]
        if not files:
            raise FlatFileValidationError("The template has no files enabled for staging.")

        grouped: dict[str, list[tuple[dict, Path, list[str]]]] = defaultdict(list)
        for config in files:
            object_name = str(config.get("object") or "").strip()
            stored_name = str(
                config.get("cleaned_name") or config.get("stored_name") or config.get("file") or ""
            )
            path = resolve_flat_file_path(
                config, self.upload_root, self.drop_root, prefer_cleaned=True
            )
            if not path.is_file():
                raise FlatFileValidationError(f"Flat file was not found: {path}")
            validate_flat_file_reference(path, config)
            headers = read_csv_header(path)
            errors = validate_file_config(config)
            if errors:
                raise FlatFileValidationError(" ".join(errors))
            missing = sorted({
                str(m.get("source") or "").strip()
                for m in config.get("mappings") or []
                if not m.get("ignored") and str(m.get("source") or "").strip() not in headers
            })
            if missing:
                raise FlatFileValidationError(
                    f"{stored_name}: mapped columns are missing: {', '.join(missing)}"
                )
            grouped[object_name].append((config, path, headers))

        object_counts: dict[str, int] = {}
        file_counts: dict[str, int] = {}
        for object_name, object_files in grouped.items():
            target_columns = sorted({
                str(mapping.get("target") or "").strip()
                for config, _, _ in object_files
                for mapping in config.get("mappings") or []
                if not mapping.get("ignored") and mapping.get("target")
            })
            # Keep the original Salesforce source Id as an internal staging
            # identity even when the UI maps it to a target External ID.  The
            # loader filters non-writable Id on insert, while IDMapper and the
            # Knowledge rich-text rewriter retain the source record identity.
            retain_source_id = any(
                str(mapping.get("source") or "").strip() == "Id"
                and str(mapping.get("target") or "").strip() != "Id"
                and not mapping.get("ignored")
                for config, _, _ in object_files
                for mapping in config.get("mappings") or []
            )
            if retain_source_id and "Id" not in target_columns:
                target_columns.append("Id")
                target_columns.sort()
            if replace:
                self.stager.drop_table(object_name, "raw")
            object_total = 0
            for config, path, headers in object_files:
                rename_map = {
                    str(mapping["source"]).strip(): str(mapping["target"]).strip()
                    for mapping in config.get("mappings") or []
                    if not mapping.get("ignored") and mapping.get("source") and mapping.get("target")
                }
                schema = {column: pl.Utf8 for column in headers if column}
                file_total = 0
                source_row = 1
                for batch in iter_csv_batches(path, schema, CHUNK_SIZE):
                    batch = batch.select(list(rename_map))
                    if retain_source_id and "Id" in batch.columns and rename_map.get("Id") != "Id":
                        batch = batch.with_columns(pl.col("Id").alias("__flat_source_id"))
                    batch = batch.rename(rename_map)
                    if "__flat_source_id" in batch.columns:
                        batch = batch.rename({"__flat_source_id": "Id"})
                    record_type_mappings = config.get("record_type_mappings") or {}
                    if "RecordTypeId" in batch.columns and record_type_mappings:
                        batch = batch.with_columns(
                            pl.col("RecordTypeId")
                            .cast(pl.Utf8)
                            .replace_strict(
                                record_type_mappings,
                                default=pl.col("RecordTypeId"),
                                return_dtype=pl.Utf8,
                            )
                            .alias("RecordTypeId")
                        )
                    for column in target_columns:
                        if column not in batch.columns:
                            batch = batch.with_columns(pl.lit(None, dtype=pl.Utf8).alias(column))
                    batch = batch.select(target_columns).with_columns(
                        pl.lit(path.name).alias("__source_file"),
                        pl.int_range(source_row + 1, source_row + len(batch) + 1, dtype=pl.Int64)
                        .alias("__source_row"),
                    )
                    self.stager.write_raw_chunk(object_name, batch)
                    file_total += len(batch)
                    source_row += len(batch)
                file_counts[path.name] = file_total
                object_total += file_total
            external_id_fields = sorted({
                str(config.get("external_id") or "").strip()
                for config, _, _ in object_files
                if str(config.get("external_id") or "").strip()
            })
            for external_id_field in external_id_fields:
                duplicates = self.stager.find_duplicate_keys(
                    object_name, [external_id_field], "raw"
                )
                if duplicates:
                    examples = ", ".join(
                        f"{item[external_id_field]} ({item['duplicate_count']} rows)"
                        for item in duplicates[:5]
                    )
                    if replace:
                        self.stager.drop_table(object_name, "raw")
                    raise FlatFileValidationError(
                        f"{object_name}: duplicate migration keys found across staged files "
                        f"for {external_id_field}. Examples: {examples}."
                    )
            object_counts[object_name] = object_total
        return FlatFileIngestResult(object_counts=object_counts, file_counts=file_counts)
