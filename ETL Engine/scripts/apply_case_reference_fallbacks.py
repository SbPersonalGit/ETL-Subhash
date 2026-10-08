#!/usr/bin/env python3
"""Apply fallback IDs for Case lookup columns missing from target UAT.

Workflow
--------
1. Load object-wise existing-record CSVs (must include ``External_Id__c``).
2. Load columns to keep/scan (Column+ReferencedObject map, or a flat column list).
3. Load fallback Ids per referenced object (optional). Columns with no fallback
   (e.g. ParentId → Case) are kept as-is when missing.
4. Write either slim CSVs with only selected columns (``extract`` mode), or
   full CSV copies with only configured fields updated (``full`` mode).
5. Write per-file (and combined) fallback maps: Case Id + column + original
   value + fallback Id, so corrected Ids can be mapped back later.

Example
-------
    python scripts/apply_case_reference_fallbacks.py \\
        --existing-dir ".../Existing Record" \\
        --case-dir ".../Files" \\
        --columns-map ".../Referrence Columns.csv" \\
        --fallback-map ".../Fallback Mapping CSV.csv" \\
        --output-dir ".../output"

columns-map formats
-------------------
A) Explicit map with optional rename + original preserve:

    Column,ReferencedObject,ApplyFallback,OutputColumn,PreserveOriginalAs
    Id,,N,Id,
    CaseNumber,,N,Legacy_Case_Number__c,
    ContactId,Contact,Y,ContactId,Legacy_Contact_Id__c
    AccountId,Account,N,Legacy_Account_Id__c,
    ParentId,Case,N,ParentId,

B) Flat list (legacy):

    Id,CaseNumber,ContactId,OwnerId,ParentId,...

fallback-map CSV
----------------
    ReferencedObject,FallbackId
    User,005S900000dr9BaIAI
    Contact,003S900000xxxxxxx

If a scanned lookup column has no fallback Id, missing references are left unchanged.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_EXISTING_ID_COLUMN = "External_Id__c"

# Used when columns-map is a flat list of Case field names.
DEFAULT_COLUMN_OBJECTS: dict[str, str] = {
    "OwnerId": "User",
    "CreatedById": "User",
    "LastModifiedById": "User",
    "ContactId": "Contact",
    "AccountId": "Account",
    "ParentId": "Case",
    "Opportunity_Name__c": "Opportunity",
    "GE_OG_MCS_Defect_Code_Hierarchy__c": "GE_OG_Defect_Code_Hierarchy__c",
    "GE_OG_Site_LegalEntity_Account__c": "Account",
    "AssetId": "Asset",
    "BusinessHoursId": "BusinessHours",
    "RecordTypeId": "RecordType",
}

# Identity / non-lookup columns commonly kept in output.
KEEP_ONLY_COLUMNS = {
    "id",
    "casenumber",
    "legacy_id__c",
    "external_id__c",
}


def normalize_header(value: str | None) -> str:
    if value is None:
        return ""
    return value.strip().lower().replace(" ", "_")


def find_column(fieldnames: list[str] | None, *candidates: str) -> str | None:
    if not fieldnames:
        return None
    normalized = {normalize_header(name): name for name in fieldnames}
    for candidate in candidates:
        hit = normalized.get(normalize_header(candidate))
        if hit:
            return hit
    return None


def id_variants(value: str) -> set[str]:
    """Build lookup keys for a Salesforce Id.

    Rules:
    - Exact value always matches.
    - 15-char prefix matches are **case-sensitive** (15-char SF Ids are case-sensitive).
    - Full 18-char values also add a casefolded key (18-char SF Ids are case-insensitive
      because the checksum encodes case). Never casefold the 15-char prefix — that can
      falsely equate different records (e.g. ``...YWOrIAO`` vs ``...YwOrIAK``).
    """
    cleaned = value.strip()
    if not cleaned:
        return set()
    variants = {cleaned}
    if len(cleaned) >= 15:
        variants.add(cleaned[:15])
    if len(cleaned) == 18:
        variants.add(cleaned.casefold())
    return variants


@dataclass
class ColumnSpec:
    """One source column mapping into the slim output file.

    - ``output_column``: renamed (or same) column for the working value
    - ``preserve_original_as``: optional second column that always keeps the
      pre-fallback source value (e.g. ContactId -> Legacy_Contact_Id__c)
    - ``fallback_id`` empty means never replace missing lookups
    """

    source_column: str
    output_column: str
    referenced_object: str = ""
    fallback_id: str = ""
    preserve_original_as: str = ""
    apply_fallback: bool = False

    @property
    def has_fallback(self) -> bool:
        return bool(self.fallback_id)

    @property
    def is_lookup(self) -> bool:
        return bool(self.referenced_object)

    @property
    def is_polymorphic(self) -> bool:
        return self.referenced_object.casefold() == "polymorphic"


# Backwards-compatible alias used by older tests/imports.
ColumnRule = ColumnSpec


def parse_bool_flag(raw: str | None, default: bool = True) -> bool:
    """Parse Y/N style flags. Empty keeps default."""
    if raw is None:
        return default
    value = raw.strip().casefold()
    if value == "":
        return default
    if value in {"y", "yes", "true", "1", "apply", "fallback"}:
        return True
    if value in {"n", "no", "false", "0", "skip", "none", "leave", "keep"}:
        return False
    raise ValueError(f"Unrecognized ApplyFallback value: {raw!r}")


def parse_csv_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass
class ReplacementRecord:
    case_id: str
    case_number: str
    column: str
    referenced_object: str
    original_value: str
    fallback_id: str
    source_file: str


@dataclass
class ProcessStats:
    files: int = 0
    rows: int = 0
    replacements: int = 0
    kept_present: int = 0
    remapped_to_target: int = 0
    rows_skipped_missing_lookup: int = 0
    left_unchanged_missing: int = 0
    blank_skipped: int = 0
    by_column: Counter[str] = field(default_factory=Counter)
    by_object: Counter[str] = field(default_factory=Counter)
    missing_columns: dict[str, list[str]] = field(default_factory=dict)
    replacement_rows: list[ReplacementRecord] = field(default_factory=list)


def load_existing_records(
    existing_dir: Path,
    id_column: str = DEFAULT_EXISTING_ID_COLUMN,
) -> tuple[dict[str, set[str]], dict[str, dict[str, str]]]:
    """Load existing-record CSVs.

    Returns
    -------
    existing_by_object:
        object API name -> set of External_Id__c variants (for existence checks)
    legacy_to_target_by_object:
        object API name -> map of legacy Id variants -> target ``Id``
        (used when ``--remap-found-to-target-id`` is enabled)
    """
    if not existing_dir.is_dir():
        raise FileNotFoundError(f"Existing-records folder not found: {existing_dir}")

    by_object: dict[str, set[str]] = defaultdict(set)
    legacy_to_target: dict[str, dict[str, str]] = defaultdict(dict)

    csv_files = sorted(existing_dir.glob("*.csv"))
    csv_files.extend(sorted(existing_dir.glob("*/*.csv")))
    if not csv_files:
        raise ValueError(f"No CSV files found under existing-dir: {existing_dir}")

    for path in csv_files:
        object_name = path.stem
        if path.parent != existing_dir:
            object_name = path.parent.name

        # A header-only object CSV intentionally means that zero referenced
        # records exist in the target. Keep the object registered so every
        # source reference can receive its configured fallback.
        by_object.setdefault(object_name, set())
        legacy_to_target.setdefault(object_name, {})

        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            key_col = find_column(
                reader.fieldnames,
                id_column,
                "External_Id__c",
                "Id",
                "legacy_id__c",
            )
            target_col = find_column(reader.fieldnames, "Id", "target_id", "TargetId")
            if not key_col:
                raise ValueError(
                    f"{path}: missing identity column '{id_column}'. "
                    f"Found: {reader.fieldnames}"
                )
            for row in reader:
                raw = (row.get(key_col) or "").strip()
                if not raw:
                    continue
                target = (
                    (row.get(target_col) or "").strip() if target_col else ""
                ) or raw
                variants = id_variants(raw)
                by_object[object_name].update(variants)
                # Also allow matching when source already has target Id.
                by_object[object_name].update(id_variants(target))
                for variant in variants:
                    legacy_to_target[object_name][variant] = target
                for variant in id_variants(target):
                    legacy_to_target[object_name].setdefault(variant, target)

    if not by_object:
        raise ValueError(f"No Legacy IDs loaded from: {existing_dir}")
    return dict(by_object), {k: dict(v) for k, v in legacy_to_target.items()}


def load_existing_ids(
    existing_dir: Path,
    id_column: str = DEFAULT_EXISTING_ID_COLUMN,
) -> dict[str, set[str]]:
    """Load External_Id__c values keyed by Salesforce object API name."""
    existing_by_object, _ = load_existing_records(existing_dir, id_column)
    return existing_by_object


def resolve_target_id(
    raw: str,
    legacy_to_target: dict[str, str] | None,
) -> str | None:
    """Return target Id for a legacy/source value, if remapping is available."""
    if not raw or not legacy_to_target:
        return None
    for variant in id_variants(raw):
        hit = legacy_to_target.get(variant)
        if hit:
            return hit
    return None


def load_fallback_map(fallback_map_path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Return (object_fallback, column_fallback). Object names are stripped."""
    object_fallback: dict[str, str] = {}
    column_fallback: dict[str, str] = {}

    with fallback_map_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fb_col_key = find_column(reader.fieldnames, "Column", "Field", "source_field")
        fb_obj_key = find_column(
            reader.fieldnames,
            "ReferencedObject",
            "Object",
            "TargetObject",
            "reference_object",
        )
        fb_id_key = find_column(
            reader.fieldnames,
            "FallbackId",
            "Fallback_Id",
            "DefaultId",
            "default_user_id",
            "Id",
        )
        if not fb_id_key:
            raise ValueError(
                "fallback-map must include FallbackId. "
                f"Found: {reader.fieldnames}"
            )
        for row in reader:
            fallback = (row.get(fb_id_key) or "").strip()
            if not fallback:
                continue
            if fb_col_key:
                column = (row.get(fb_col_key) or "").strip()
                if column:
                    column_fallback[column] = fallback
            if fb_obj_key:
                obj = (row.get(fb_obj_key) or "").strip()
                if obj:
                    object_fallback[obj] = fallback

    if not object_fallback and not column_fallback:
        with fallback_map_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or len(reader.fieldnames) < 2:
                raise ValueError(f"fallback-map is empty/invalid: {fallback_map_path}")
            left = reader.fieldnames[0]
            right = reader.fieldnames[1]
            for row in reader:
                obj = (row.get(left) or "").strip()
                fallback = (row.get(right) or "").strip()
                if obj and fallback:
                    object_fallback[obj] = fallback

    return object_fallback, column_fallback


def load_prefix_map(prefix_map_path: Path) -> dict[str, dict[str, str]]:
    """Load source Salesforce key-prefix mappings by reference column."""
    mappings: dict[str, dict[str, str]] = defaultdict(dict)
    with prefix_map_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        column_key = find_column(reader.fieldnames, "Column", "Field")
        prefix_key = find_column(reader.fieldnames, "SourcePrefix", "Prefix", "KeyPrefix")
        object_key = find_column(
            reader.fieldnames, "ReferencedObject", "Object", "TargetObject"
        )
        if not column_key or not prefix_key or not object_key:
            raise ValueError(
                "prefix-map must include Column,SourcePrefix,ReferencedObject. "
                f"Found: {reader.fieldnames}"
            )
        for row in reader:
            column = (row.get(column_key) or "").strip()
            prefix = (row.get(prefix_key) or "").strip()
            referenced_object = (row.get(object_key) or "").strip()
            if not column or not prefix or not referenced_object:
                continue
            column_map = mappings[normalize_header(column)]
            if prefix in column_map and column_map[prefix] != referenced_object:
                raise ValueError(
                    f"Conflicting prefix mapping for {column}/{prefix}: "
                    f"{column_map[prefix]} vs {referenced_object}"
                )
            column_map[prefix] = referenced_object
    if not mappings:
        raise ValueError(f"No prefix mappings found in {prefix_map_path}")
    return {column: dict(values) for column, values in mappings.items()}


def resolve_referenced_object(
    spec: ColumnSpec,
    raw: str,
    prefix_mappings: dict[str, dict[str, str]] | None,
) -> str | None:
    """Resolve a fixed or polymorphic reference to its concrete object."""
    if not spec.is_polymorphic:
        return spec.referenced_object
    if not raw:
        return None
    column_map = (prefix_mappings or {}).get(normalize_header(spec.source_column), {})
    matches = [prefix for prefix in column_map if raw.startswith(prefix)]
    if not matches:
        return None
    # Prefer the longest configured prefix if configurations overlap.
    return column_map[max(matches, key=len)]


def _is_flat_column_list(fieldnames: list[str] | None) -> bool:
    if not fieldnames:
        return False
    has_column = find_column(fieldnames, "Column", "Field", "source_field", "field_name")
    has_object = find_column(
        fieldnames,
        "ReferencedObject",
        "Object",
        "TargetObject",
        "reference_object",
    )
    return not (has_column and has_object)


def build_output_header(
    specs: list[ColumnSpec],
    source_header: list[str] | None = None,
    output_mode: str = "extract",
) -> list[str]:
    """Build the output header for extract or full-copy output."""
    if output_mode not in {"extract", "full"}:
        raise ValueError(f"Unsupported output mode: {output_mode}")

    header: list[str] = []
    seen: set[str] = set()
    if output_mode == "full":
        if not source_header:
            raise ValueError("source_header is required in full output mode")
        for name in source_header:
            key = normalize_header(name)
            if key in seen:
                raise ValueError(f"Duplicate source column name: {name}")
            seen.add(key)
            header.append(name)

    for spec in specs:
        for name in (spec.output_column, spec.preserve_original_as):
            if not name:
                continue
            key = normalize_header(name)
            if key in seen:
                if output_mode == "full":
                    continue
                raise ValueError(f"Duplicate output column name configured: {name}")
            seen.add(key)
            header.append(name)
    return header


def load_column_rules(
    columns_map_path: Path,
    fallback_map_path: Path,
    no_fallback_columns: set[str] | None = None,
) -> list[ColumnSpec]:
    """Load dynamic column specs from the columns-map CSV.

    Supported headers (optional ones in brackets):
        Column, ReferencedObject, ApplyFallback, [OutputColumn], [PreserveOriginalAs]

    Examples
    --------
    Rename only (no fallback)::

        CaseNumber,,N,Legacy_Case_Number__c,
        AccountId,Account,N,Legacy_Account_Id__c,

    Fallback + keep original copy::

        ContactId,Contact,Y,ContactId,Legacy_Contact_Id__c
    """
    object_fallback, column_fallback = load_fallback_map(fallback_map_path)
    no_fallback = {normalize_header(name) for name in (no_fallback_columns or set())}

    with columns_map_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if not header:
            raise ValueError(f"columns-map is empty: {columns_map_path}")
        header = [cell.strip() for cell in header if str(cell).strip()]

    specs: list[ColumnSpec] = []

    if _is_flat_column_list(header):
        for column in header:
            obj = ""
            apply_fallback = False
            if normalize_header(column) not in KEEP_ONLY_COLUMNS:
                obj = DEFAULT_COLUMN_OBJECTS.get(column, "")
                apply_fallback = bool(obj) and normalize_header(column) not in no_fallback
            fallback = ""
            if apply_fallback and obj:
                fallback = column_fallback.get(column) or object_fallback.get(obj) or ""
            specs.append(
                ColumnSpec(
                    source_column=column,
                    output_column=column,
                    referenced_object=obj,
                    fallback_id=fallback,
                    apply_fallback=apply_fallback,
                )
            )
    else:
        with columns_map_path.open("r", encoding="utf-8-sig", newline="") as handle:
            dict_reader = csv.DictReader(handle)
            col_key = find_column(
                dict_reader.fieldnames, "Column", "Field", "source_field", "field_name"
            )
            obj_key = find_column(
                dict_reader.fieldnames,
                "ReferencedObject",
                "Object",
                "TargetObject",
                "reference_object",
            )
            flag_key = find_column(
                dict_reader.fieldnames,
                "ApplyFallback",
                "Fallback",
                "UseFallback",
                "apply_fallback",
            )
            out_key = find_column(
                dict_reader.fieldnames,
                "OutputColumn",
                "Output",
                "TargetColumn",
                "RenameTo",
                "output_column",
            )
            preserve_key = find_column(
                dict_reader.fieldnames,
                "PreserveOriginalAs",
                "OriginalColumn",
                "CopyOriginalTo",
                "PreserveAs",
                "preserve_original_as",
            )
            if not col_key:
                raise ValueError(
                    "columns-map must include a Column header. "
                    f"Found: {dict_reader.fieldnames}"
                )
            for row in dict_reader:
                column = (row.get(col_key) or "").strip()
                if not column:
                    continue
                obj = (row.get(obj_key) or "").strip() if obj_key else ""
                output_column = (
                    (row.get(out_key) or "").strip() if out_key else ""
                ) or column
                preserve_as = (
                    (row.get(preserve_key) or "").strip() if preserve_key else ""
                )
                apply_fallback = False
                if obj:
                    apply_fallback = parse_bool_flag(
                        row.get(flag_key) if flag_key else None,
                        default=True,
                    )
                    if normalize_header(column) in no_fallback:
                        apply_fallback = False
                fallback = ""
                if apply_fallback and obj:
                    fallback = (
                        column_fallback.get(column)
                        or object_fallback.get(obj)
                        or ""
                    )
                specs.append(
                    ColumnSpec(
                        source_column=column,
                        output_column=output_column,
                        referenced_object=obj,
                        fallback_id=fallback,
                        preserve_original_as=preserve_as,
                        apply_fallback=apply_fallback,
                    )
                )

    if not specs:
        raise ValueError(f"No columns found in {columns_map_path}")
    # Validate header uniqueness early.
    build_output_header(specs)
    return specs


def value_exists(raw: str, existing_ids: set[str]) -> bool:
    return any(variant in existing_ids for variant in id_variants(raw))


REPLACEMENT_HEADERS = [
    "SourceFile",
    "Id",
    "CaseNumber",
    "Column",
    "ReferencedObject",
    "OriginalValue",
    "FallbackId",
]


def _row_identity(row: dict[str, str], header_lookup: dict[str, str]) -> tuple[str, str]:
    id_col = header_lookup.get("id")
    case_number_col = header_lookup.get("casenumber")
    case_id = (row.get(id_col) or "").strip() if id_col else ""
    case_number = (row.get(case_number_col) or "").strip() if case_number_col else ""
    return case_id, case_number


def write_replacement_map(path: Path, rows: list[ReplacementRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPLACEMENT_HEADERS)
        writer.writeheader()
        for item in rows:
            writer.writerow(
                {
                    "SourceFile": item.source_file,
                    "Id": item.case_id,
                    "CaseNumber": item.case_number,
                    "Column": item.column,
                    "ReferencedObject": item.referenced_object,
                    "OriginalValue": item.original_value,
                    "FallbackId": item.fallback_id,
                }
            )


def process_case_file(
    source: Path,
    output_path: Path,
    specs: list[ColumnSpec],
    existing_by_object: dict[str, set[str]],
    stats: ProcessStats,
    legacy_to_target_by_object: dict[str, dict[str, str]] | None = None,
    remap_found_to_target_id: bool = False,
    skip_if_missing_lookups: set[str] | None = None,
    output_mode: str = "extract",
    prefix_mappings: dict[str, dict[str, str]] | None = None,
    object_fallback: dict[str, str] | None = None,
    column_fallback: dict[str, str] | None = None,
) -> list[ReplacementRecord]:
    file_replacements: list[ReplacementRecord] = []
    skip_missing = {
        normalize_header(name) for name in (skip_if_missing_lookups or set())
    }

    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"{source}: missing header row")

        output_header = build_output_header(
            specs,
            source_header=reader.fieldnames,
            output_mode=output_mode,
        )
        full_header_lookup = {normalize_header(name): name for name in reader.fieldnames}
        missing = [
            spec.source_column
            for spec in specs
            if normalize_header(spec.source_column) not in full_header_lookup
        ]
        if missing:
            stats.missing_columns[source.name] = missing
        if not output_header:
            raise ValueError(f"{source}: no output columns configured.")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8", newline="") as out_handle:
            writer = csv.DictWriter(out_handle, fieldnames=output_header, extrasaction="ignore")
            writer.writeheader()

            for row in reader:
                stats.rows += 1
                if output_mode == "full":
                    out_row = {col: row.get(col, "") for col in output_header}
                else:
                    out_row = {col: "" for col in output_header}
                case_id, case_number = _row_identity(row, full_header_lookup)
                drop_row = False

                for spec in specs:
                    source_actual = full_header_lookup.get(
                        normalize_header(spec.source_column)
                    )
                    raw = (row.get(source_actual) or "").strip() if source_actual else ""

                    if spec.preserve_original_as:
                        out_row[spec.preserve_original_as] = raw

                    value = raw
                    if spec.is_lookup:
                        if not raw:
                            stats.blank_skipped += 1
                            if normalize_header(spec.source_column) in skip_missing:
                                drop_row = True
                        else:
                            referenced_object = resolve_referenced_object(
                                spec, raw, prefix_mappings
                            )
                            if not referenced_object:
                                stats.left_unchanged_missing += 1
                                if normalize_header(spec.source_column) in skip_missing:
                                    drop_row = True
                                out_row[spec.output_column] = value
                                continue
                            fallback_id = spec.fallback_id
                            if spec.is_polymorphic and spec.apply_fallback:
                                fallback_id = (
                                    (column_fallback or {}).get(spec.source_column)
                                    or (object_fallback or {}).get(referenced_object)
                                    or ""
                                )
                            existing_ids = existing_by_object.get(referenced_object)
                            remap = (
                                (legacy_to_target_by_object or {}).get(
                                    referenced_object
                                )
                                if remap_found_to_target_id
                                else None
                            )
                            if existing_ids is None:
                                stats.left_unchanged_missing += 1
                                if normalize_header(spec.source_column) in skip_missing:
                                    drop_row = True
                            elif value_exists(raw, existing_ids):
                                stats.kept_present += 1
                                if remap_found_to_target_id:
                                    target = resolve_target_id(raw, remap)
                                    if target:
                                        value = target
                                        stats.remapped_to_target += 1
                            elif not fallback_id:
                                stats.left_unchanged_missing += 1
                                if normalize_header(spec.source_column) in skip_missing:
                                    drop_row = True
                            else:
                                value = fallback_id
                                stats.replacements += 1
                                stats.by_column[spec.source_column] += 1
                                stats.by_object[referenced_object] += 1
                                record = ReplacementRecord(
                                    case_id=case_id,
                                    case_number=case_number,
                                    column=spec.source_column,
                                    referenced_object=referenced_object,
                                    original_value=raw,
                                    fallback_id=fallback_id,
                                    source_file=source.name,
                                )
                                file_replacements.append(record)
                                stats.replacement_rows.append(record)

                    out_row[spec.output_column] = value

                if drop_row:
                    stats.rows_skipped_missing_lookup += 1
                    continue

                writer.writerow(out_row)

    stats.files += 1
    return file_replacements


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Replace missing Case lookup references with fallback Ids when the "
            "referenced External_Id__c is not present in the existing UAT export."
        )
    )
    parser.add_argument(
        "--existing-dir",
        required=True,
        type=Path,
        help="Folder of object-wise CSVs with External_Id__c (existing UAT records).",
    )
    parser.add_argument(
        "--case-dir",
        required=True,
        type=Path,
        help="Folder containing Case CSV files to scan.",
    )
    parser.add_argument(
        "--columns-map",
        required=True,
        type=Path,
        help="Flat column list CSV, or Column,ReferencedObject map.",
    )
    parser.add_argument(
        "--fallback-map",
        required=True,
        type=Path,
        help="CSV of ReferencedObject,FallbackId (optional Column override).",
    )
    parser.add_argument(
        "--prefix-map",
        type=Path,
        help=(
            "CSV of Column,SourcePrefix,ReferencedObject mappings required "
            "when a column uses ReferencedObject=Polymorphic."
        ),
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="Folder where updated CSV copies are written.",
    )
    parser.add_argument(
        "--output-mode",
        choices=("extract", "full"),
        default="extract",
        help=(
            "extract writes only configured columns (default); full preserves "
            "all source columns and updates only configured fields."
        ),
    )
    parser.add_argument(
        "--existing-id-column",
        default=DEFAULT_EXISTING_ID_COLUMN,
        help=f"Identity column in existing-record CSVs (default: {DEFAULT_EXISTING_ID_COLUMN}).",
    )
    parser.add_argument(
        "--no-fallback-columns",
        default="ParentId",
        help=(
            "Comma-separated Case columns to include/check without applying a "
            "fallback Id (default: ParentId). Example: ParentId,AccountId"
        ),
    )
    parser.add_argument(
        "--remap-found-to-target-id",
        action="store_true",
        help=(
            "When a lookup External_Id__c exists in Existing Record, write the "
            "target Id column value instead of keeping the source/legacy Id."
        ),
    )
    parser.add_argument(
        "--skip-if-missing-lookups",
        default="",
        help=(
            "Comma-separated lookup columns that must exist in Existing Record "
            "(or be blank-skipped). Rows failing the check are omitted from output. "
            "Example: CampaignId,ContactId"
        ),
    )
    parser.add_argument(
        "--glob",
        default="*.csv",
        help="Glob for Case files inside --case-dir (default: *.csv).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    existing_by_object, legacy_to_target = load_existing_records(
        args.existing_dir, args.existing_id_column
    )
    no_fallback_columns = set(parse_csv_list(args.no_fallback_columns))
    skip_if_missing_lookups = set(parse_csv_list(args.skip_if_missing_lookups))
    specs = load_column_rules(
        args.columns_map,
        args.fallback_map,
        no_fallback_columns=no_fallback_columns,
    )
    polymorphic_specs = [spec for spec in specs if spec.is_polymorphic]
    if polymorphic_specs and not args.prefix_map:
        print(
            "--prefix-map is required for polymorphic column(s): "
            + ", ".join(spec.source_column for spec in polymorphic_specs),
            file=sys.stderr,
        )
        return 1
    prefix_mappings = load_prefix_map(args.prefix_map) if args.prefix_map else {}
    missing_prefix_columns = [
        spec.source_column
        for spec in polymorphic_specs
        if normalize_header(spec.source_column) not in prefix_mappings
    ]
    if missing_prefix_columns:
        print(
            "Missing prefix mappings for polymorphic column(s): "
            + ", ".join(missing_prefix_columns),
            file=sys.stderr,
        )
        return 1
    object_fallback, column_fallback = load_fallback_map(args.fallback_map)
    case_files = sorted(path for path in args.case_dir.glob(args.glob) if path.is_file())
    if not case_files:
        print(f"No Case CSV files matched {args.case_dir / args.glob}", file=sys.stderr)
        return 1

    # Existing export is required for fallback objects and for remapped/required lookups.
    needed_objects = {
        spec.referenced_object
        for spec in specs
        if not spec.is_polymorphic
        and (
            spec.has_fallback
            or (
                spec.is_lookup
                and (
                    args.remap_found_to_target_id
                    or normalize_header(spec.source_column)
                    in {normalize_header(c) for c in skip_if_missing_lookups}
                )
            )
        )
    }
    for spec in polymorphic_specs:
        for referenced_object in prefix_mappings[normalize_header(spec.source_column)].values():
            # Objects with a fallback need an existing-record inventory, even
            # when that inventory is intentionally empty (for example Lead).
            if referenced_object in object_fallback:
                needed_objects.add(referenced_object)
    needed_objects = sorted(needed_objects)
    missing_objects = [obj for obj in needed_objects if obj not in existing_by_object]
    if missing_objects:
        print(
            "Missing existing-record CSV(s) for referenced object(s): "
            + ", ".join(missing_objects),
            file=sys.stderr,
        )
        print(f"Loaded objects: {', '.join(sorted(existing_by_object))}", file=sys.stderr)
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stats = ProcessStats()

    print(f"Output mode: {args.output_mode}")
    if args.output_mode == "extract":
        print("Output columns:")
        print("  " + ", ".join(build_output_header(specs)))
    else:
        print("Output columns: all source columns; configured fields are updated")

    print("\nExisting ID counts (objects used for lookup checks):")
    for obj in needed_objects:
        print(f"  {obj}: {len(existing_by_object[obj]):,} id variants loaded")

    print("\nColumn specs:")
    for spec in specs:
        parts = [f"{spec.source_column} -> {spec.output_column}"]
        if spec.preserve_original_as:
            parts.append(f"original copy -> {spec.preserve_original_as}")
        if spec.is_lookup:
            if spec.is_polymorphic:
                parts.append("fallback determined by source ID prefix")
            elif spec.has_fallback:
                parts.append(f"fallback {spec.fallback_id}")
            else:
                parts.append("no fallback (leave missing unchanged)")
            if args.remap_found_to_target_id:
                parts.append("remap found -> target Id")
            if normalize_header(spec.source_column) in {
                normalize_header(c) for c in skip_if_missing_lookups
            }:
                parts.append("skip row if missing")
            if not spec.is_polymorphic:
                parts.append(
                    f"existing={'yes' if spec.referenced_object in existing_by_object else 'no'}"
                )
        else:
            parts.append("keep/rename only")
        print("  " + " | ".join(parts))

    maps_dir = args.output_dir / "fallback_maps"
    maps_dir.mkdir(parents=True, exist_ok=True)

    for source in case_files:
        output_path = args.output_dir / source.name
        file_replacements = process_case_file(
            source=source,
            output_path=output_path,
            specs=specs,
            existing_by_object=existing_by_object,
            stats=stats,
            legacy_to_target_by_object=legacy_to_target,
            remap_found_to_target_id=args.remap_found_to_target_id,
            skip_if_missing_lookups=skip_if_missing_lookups,
            output_mode=args.output_mode,
            prefix_mappings=prefix_mappings,
            object_fallback=object_fallback,
            column_fallback=column_fallback,
        )
        map_path = maps_dir / f"{source.stem}_fallback_map.csv"
        write_replacement_map(map_path, file_replacements)
        print(f"Wrote {output_path}")
        print(f"  fallback map: {map_path} ({len(file_replacements):,} row(s))")

    combined_map = args.output_dir / "all_fallback_map.csv"
    write_replacement_map(combined_map, stats.replacement_rows)
    print(f"Wrote combined fallback map: {combined_map}")

    print("\nSummary")
    print(f"  files:                    {stats.files}")
    print(f"  rows:                     {stats.rows:,}")
    print(f"  kept present:             {stats.kept_present:,}")
    print(f"  remapped to target Id:    {stats.remapped_to_target:,}")
    print(f"  skipped missing lookup:   {stats.rows_skipped_missing_lookup:,}")
    print(f"  left unchanged (missing): {stats.left_unchanged_missing:,}")
    print(f"  blank skipped:            {stats.blank_skipped:,}")
    print(f"  replacements:             {stats.replacements:,}")
    if stats.by_column:
        print("  replacements by column:")
        for column, count in stats.by_column.most_common():
            print(f"    {column}: {count:,}")
    if stats.by_object:
        print("  replacements by object:")
        for obj, count in stats.by_object.most_common():
            print(f"    {obj}: {count:,}")
    if stats.missing_columns:
        print("  warning: scan columns missing from some files:")
        for filename, cols in stats.missing_columns.items():
            print(f"    {filename}: {', '.join(cols)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
