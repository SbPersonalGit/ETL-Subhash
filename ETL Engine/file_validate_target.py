"""Validate one or more user-supplied CSV files directly against a target org."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from cli import build_connectors
from core.field_rules import build_field_profile
from core.flat_file import open_csv
from core.file_validation_query import (
    apply_include_exclude,
    detailed_outputs_allowed,
    intersect_compare_fields,
    parse_custom_soql_select,
    parse_field_list,
    summarize_mismatches_by_field,
    write_partitioned_mismatch_csvs,
    write_file_validation_progress,
    write_file_wise_target_csvs,
)
from verify_records import compare_rows, query_target_rows, resolve_query_fields, write_validation_workbook


def _log(message: str) -> None:
    print(message, flush=True)


def _custom_soql_source_projection(
    manifest: dict,
    source_key: str,
    target_key: str,
) -> set[str] | None:
    """Return source columns worth retaining for a custom-SOQL comparison.

    Custom SOQL is the field-selection contract for file validation. Keeping
    CSV-only columns in every row wastes substantial memory and cannot affect
    the comparison result. Lookup source fields remain available for optional
    relationship enrichment.
    """
    if str(manifest.get("file_query_mode") or "object_template").strip().lower() != "custom_soql":
        return None
    custom_soql = str(manifest.get("custom_soql") or "").strip()
    if not custom_soql:
        return None
    _object_name, selected_fields = parse_custom_soql_select(custom_soql)
    projection = {source_key, target_key}
    projection.update(field for field in selected_fields if "." not in field)
    for mapping in manifest.get("lookup_mappings") or []:
        if isinstance(mapping, dict) and mapping.get("source_field"):
            projection.add(str(mapping["source_field"]).strip())
    return {field for field in projection if field}


class _Progress:
    def __init__(self, out_dir: Path, object_name: str, total_files: int) -> None:
        self.out_dir = out_dir
        self.object_name = object_name
        self.total_files = total_files
        self._last_log_key = ""

    def update(self, phase: str, *, log: str | None = None, **fields) -> None:
        payload = {
            "object_name": self.object_name,
            "total_files": self.total_files,
            "phase": phase,
            **fields,
        }
        write_file_validation_progress(self.out_dir, payload)
        message = log or payload.get("detail") or payload.get("message")
        if message and message != self._last_log_key:
            _log(str(message))
            self._last_log_key = str(message)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--sample-size", type=int)
    parser.add_argument("--query-mode", choices=["auto", "rest", "bulk"], default="auto")
    parser.add_argument("--id-chunk-size", type=int, default=200)
    parser.add_argument("--print-soql", action="store_true")
    args = parser.parse_args()
    if args.id_chunk_size is not None and not 25 <= args.id_chunk_size <= 500:
        raise SystemExit("--id-chunk-size must be between 25 and 500")
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    object_name = manifest["object_name"]
    source_key = manifest["source_id_column"]
    target_key = manifest["target_match_field"]
    file_query_mode = str(manifest.get("file_query_mode") or "object_template").strip().lower()
    if file_query_mode not in {"object_template", "custom_soql"}:
        raise SystemExit("--manifest file_query_mode must be object_template or custom_soql")
    exclude_fields = parse_field_list(manifest.get("exclude_fields"))
    ignore_fields = set(parse_field_list(manifest.get("ignore_fields")))
    ignore_fields.update(exclude_fields)
    include_fields = parse_field_list(manifest.get("include_fields"))
    custom_soql = str(manifest.get("custom_soql") or "").strip()
    file_count = len(manifest["files"])
    progress = _Progress(out_dir, object_name, file_count)

    try:
        _run_validation(
            args,
            manifest,
            out_dir,
            object_name,
            source_key,
            target_key,
            ignore_fields,
            include_fields,
            file_query_mode,
            custom_soql,
            progress,
        )
    except Exception as exc:
        progress.update(
            "failed",
            status="FAILED",
            detail=f"File validation failed for {object_name}: {exc}",
            log=f"[{object_name}] FAILED: {exc}",
        )
        raise


def _run_validation(
    args,
    manifest: dict,
    out_dir: Path,
    object_name: str,
    source_key: str,
    target_key: str,
    ignore_fields: set[str],
    include_fields: list[str],
    file_query_mode: str,
    custom_soql: str,
    progress: _Progress,
) -> None:
    rows_by_key: dict[str, dict[str, str]] = {}
    row_file_headers: dict[str, list[str]] = {}
    source_file_by_key: dict[str, str] = {}
    ids_by_source_file: dict[str, list[str]] = {}
    origins: dict[str, list[str]] = {}
    duplicate_rows: list[dict[str, str]] = []
    invalid_id_rows: list[dict[str, str]] = []
    all_headers: list[str] = []
    eligible = 0
    file_count = len(manifest["files"])
    source_projection = _custom_soql_source_projection(manifest, source_key, target_key)

    progress.update(
        "starting",
        detail=f"Starting file validation for {object_name} | files={file_count}",
        log=f"[{object_name}] Loading {file_count} source CSV file(s) into memory...",
    )

    for file_index, file_name in enumerate(manifest["files"], start=1):
        path = Path(file_name)
        source_label = path.name
        size_mb = path.stat().st_size / (1024 * 1024) if path.exists() else 0.0
        file_fraction_base = (file_index - 1) / max(file_count, 1)
        progress.update(
            "load_source",
            current_file=source_label,
            current_index=file_index,
            stage_fraction=file_fraction_base,
            rows_processed=0,
            unique_ids=len(rows_by_key),
            log=f"[{object_name}] Reading file {file_index}/{file_count}: {source_label} ({size_mb:.1f} MB)",
        )
        with open_csv(path) as handle:
            reader = csv.DictReader(handle)
            headers = [str(header) for header in (reader.fieldnames or []) if header]
            if source_key not in headers:
                raise ValueError(f"{path.name}: ID column '{source_key}' was not found")
            all_headers.extend(header for header in headers if header not in all_headers)
            retained_headers = (
                [header for header in headers if header in source_projection]
                if source_projection is not None
                else headers
            )
            if source_key not in retained_headers:
                retained_headers.insert(0, source_key)
            ids_by_source_file.setdefault(source_label, [])
            file_rows = 0
            for line_number, raw in enumerate(reader, start=2):
                key = str(raw.get(source_key) or "").strip()
                if not key:
                    invalid_id_rows.append({
                        "object_name": object_name, "external_id": "", "field": source_key,
                        "issue": "missing_source_id", "expected": "non-empty match ID",
                        "actual": f"{path.name}:{line_number}", "source_file": source_label,
                    })
                    continue
                eligible += 1
                file_rows += 1
                if file_rows == 1 or file_rows % 25000 == 0:
                    within = min(0.95, file_rows / max(file_rows + 25000, 1))
                    progress.update(
                        "load_source",
                        current_file=source_label,
                        current_index=file_index,
                        stage_fraction=file_fraction_base + (within / max(file_count, 1)),
                        rows_processed=file_rows,
                        unique_ids=len(rows_by_key),
                        log=(
                            f"[{object_name}]   {source_label}: loaded {file_rows:,} eligible rows "
                            f"({len(rows_by_key):,} unique so far)"
                        ),
                    )
                row = {
                    header: "" if raw.get(header) is None else str(raw.get(header))
                    for header in retained_headers
                }
                row[target_key] = key
                origin = f"{path.name}:{line_number}"
                ids_by_source_file[source_label].append(key)
                if key in rows_by_key:
                    existing = rows_by_key[key]
                    shared = set(existing) & set(row)
                    conflicting = any(str(existing.get(f, "")) != str(row.get(f, "")) for f in shared)
                    duplicate_rows.append({
                        "object_name": object_name, "external_id": key, "field": "__row__",
                        "issue": "conflicting_duplicate" if conflicting else "identical_duplicate",
                        "expected": ", ".join(origins[key]), "actual": origin,
                        "source_file": source_label,
                    })
                    origins[key].append(origin)
                    continue
                rows_by_key[key] = row
                source_file_by_key[key] = source_label
                row_file_headers[key] = retained_headers
                origins[key] = [origin]
            progress.update(
                "load_source",
                current_file=source_label,
                current_index=file_index,
                stage_fraction=file_index / max(file_count, 1),
                rows_processed=file_rows,
                unique_ids=len(rows_by_key),
                log=(
                    f"[{object_name}]   {source_label}: done | columns={len(headers)} "
                    f"retained={len(retained_headers)} "
                    f"eligible={file_rows:,} unique_total={len(rows_by_key):,}"
                ),
            )

    expected_rows = list(rows_by_key.values())
    if args.sample_size is not None:
        expected_rows = expected_rows[: args.sample_size]
        kept_keys = {row[target_key] for row in expected_rows}
        row_file_headers = {key: headers for key, headers in row_file_headers.items() if key in kept_keys}
        ids_by_source_file = {
            name: [key for key in keys if key in kept_keys]
            for name, keys in ids_by_source_file.items()
        }
        ids_by_source_file = {name: keys for name, keys in ids_by_source_file.items() if keys}
        _log(f"[{object_name}] Sample size applied | keeping {len(expected_rows):,} rows")

    progress.update(
        "load_source",
        stage_fraction=1.0,
        rows_processed=eligible,
        unique_ids=len(expected_rows),
        log=(
            f"[{object_name}] Source load complete | union_columns={len(all_headers)} "
            f"unique_ids={len(expected_rows):,} duplicates={len(duplicate_rows):,} "
            f"invalid_ids={len(invalid_id_rows):,}"
        ),
    )

    progress.update(
        "connect",
        stage_fraction=0.0,
        detail=f"Connecting to target org for {object_name}",
        log=f"[{object_name}] Connecting to target org...",
    )
    _, target = build_connectors()
    target.connect()
    progress.update(
        "connect",
        stage_fraction=0.5,
        detail=f"Connected — describing {object_name} field profile",
        log=f"[{object_name}] Connected | describing {object_name} field profile...",
    )
    profile = build_field_profile(target, object_name)
    progress.update(
        "connect",
        stage_fraction=0.55,
        detail=f"Describe complete for {object_name} | preparing compare fields",
        log=(
            f"[{object_name}] Describe complete | fields={len(profile.field_meta):,} "
            f"| preparing compare fields for {len(expected_rows):,} unique IDs..."
        ),
    )
    lookup_source_fields: set[str] = set()
    relationship_by_source: dict[str, str] = {}
    lookup_skipped: list[str] = []
    intersection_skipped: list[str] = []
    include_skipped: list[str] = []
    lookup_mappings = list(manifest.get("lookup_mappings") or [])

    # Enrichment runs for both Object+Template and Custom SOQL when mappings are provided.
    for mapping_index, mapping in enumerate(lookup_mappings, start=1):
        source_field = str(mapping.get("source_field") or "")
        target_field = str(mapping.get("target_field") or "")
        external_id = str(mapping.get("external_id") or "")
        if source_field not in all_headers:
            continue
        lookup_source_fields.add(source_field)
        meta = profile.field_meta.get(target_field)
        if not meta or not meta.relationship_name or len(meta.references_to) != 1:
            lookup_skipped.append(source_field)
            continue
        if not external_id:
            lookup_skipped.append(source_field)
            continue
        relationship_field = f"{meta.relationship_name}.{external_id}"
        relationship_by_source[source_field] = relationship_field
        progress.update(
            "connect",
            stage_fraction=0.55 + (0.15 * mapping_index / max(len(lookup_mappings), 1)),
            detail=f"Enriching lookup column {source_field} → {relationship_field}",
            log=(
                f"[{object_name}] Enriching lookup {mapping_index}/{len(lookup_mappings)}: "
                f"{source_field} → {relationship_field} across {len(rows_by_key):,} rows..."
            ),
        )
        enriched = 0
        for key, row in rows_by_key.items():
            file_headers = row_file_headers.get(key) or []
            if source_field not in file_headers:
                continue
            row[relationship_field] = row.get(source_field, "")
            enriched += 1
            if enriched == 1 or enriched % 100000 == 0:
                progress.update(
                    "connect",
                    stage_fraction=0.55 + (0.15 * mapping_index / max(len(lookup_mappings), 1)),
                    detail=f"Enriching {source_field}: {enriched:,} rows",
                    log=f"[{object_name}]   lookup {source_field}: enriched {enriched:,} rows...",
                )

    if file_query_mode == "custom_soql":
        if not custom_soql:
            raise ValueError("Custom SOQL is required when file_query_mode is custom_soql")
        soql_object, soql_fields = parse_custom_soql_select(custom_soql)
        if soql_object.casefold() != object_name.casefold():
            raise ValueError(
                f"Custom SOQL FROM object '{soql_object}' does not match selected target object '{object_name}'"
            )
        # Ensure mapped relationship paths are retrieved even if omitted from SELECT.
        soql_with_lookups = list(dict.fromkeys([*soql_fields, *relationship_by_source.values()]))
        progress.update(
            "connect",
            stage_fraction=0.7,
            detail=f"Custom SOQL mode | SELECT fields={len(soql_with_lookups)}",
            log=(
                f"[{object_name}] Custom SOQL mode | SELECT fields={len(soql_with_lookups)} "
                f"(CSV match IDs still drive WHERE {target_key} IN (...); "
                f"lookup_maps={len(relationship_by_source)})"
            ),
        )
        query_fields, resolve_skipped = resolve_query_fields(object_name, soql_with_lookups, profile)
        retrieval_fields = list(dict.fromkeys(["Id", target_key, *query_fields]))
        query_field_set = set(query_fields) | {target_key}
        # Direct CSV ∩ SOQL, plus mapped relationship paths that appear in query_fields.
        compare_fields, csv_only, target_only = intersect_compare_fields(
            csv_fields=[
                header for header in all_headers
                if header not in lookup_source_fields
            ],
            target_fields=query_fields,
            match_key=target_key,
            source_key=source_key,
        )
        for relationship_field in relationship_by_source.values():
            if (
                relationship_field in query_field_set
                and relationship_field not in ignore_fields
                and relationship_field not in compare_fields
            ):
                compare_fields.append(relationship_field)
        compare_fields = [field for field in compare_fields if field not in ignore_fields]
        intersection_skipped = list(dict.fromkeys([*csv_only, *target_only, *sorted(ignore_fields), *lookup_skipped]))
        skipped = list(dict.fromkeys([*resolve_skipped, *intersection_skipped]))
        compare_field_set = set(compare_fields)

        def _compare_fields_for_headers(file_headers: list[str]) -> list[str]:
            fields: list[str] = []
            for header in file_headers:
                if header == source_key or header in ignore_fields:
                    continue
                if header in lookup_source_fields:
                    relationship_field = relationship_by_source.get(header)
                    if (
                        relationship_field
                        and relationship_field not in ignore_fields
                        and relationship_field in compare_field_set
                        and relationship_field not in fields
                    ):
                        fields.append(relationship_field)
                    continue
                if header in compare_field_set and header not in fields:
                    fields.append(header)
            return [field for field in fields if field != target_key]

    else:
        candidate_fields = [
            field for field in all_headers
            if field != source_key and field not in lookup_source_fields and field not in ignore_fields
        ]
        for source_field, relationship_field in relationship_by_source.items():
            if source_field in ignore_fields or relationship_field in ignore_fields:
                continue
            candidate_fields.append(relationship_field)
        if source_key == target_key and target_key not in ignore_fields:
            candidate_fields.append(target_key)

        if include_fields:
            for field in include_fields:
                if field not in candidate_fields and field not in ignore_fields:
                    candidate_fields.append(field)

        filtered_candidates, include_skipped = apply_include_exclude(
            candidate_fields,
            include_fields=include_fields,
            exclude_fields=sorted(ignore_fields),
            required_fields=[target_key] if source_key == target_key else [],
        )
        query_fields, resolve_skipped = resolve_query_fields(object_name, filtered_candidates, profile)
        skipped = list(dict.fromkeys([
            *resolve_skipped,
            *lookup_skipped,
            *include_skipped,
            *sorted(ignore_fields),
        ]))
        retrieval_fields = list(dict.fromkeys(["Id", target_key, *query_fields]))
        query_field_set = set(query_fields)

        def _compare_fields_for_headers(file_headers: list[str]) -> list[str]:
            fields: list[str] = []
            for header in file_headers:
                if header == source_key or header in ignore_fields:
                    continue
                if header in lookup_source_fields:
                    relationship_field = relationship_by_source.get(header)
                    if (
                        relationship_field
                        and relationship_field not in ignore_fields
                        and relationship_field in query_field_set
                        and relationship_field not in fields
                    ):
                        fields.append(relationship_field)
                    continue
                if header in query_field_set and header not in fields:
                    fields.append(header)
            if source_key == target_key and target_key in query_field_set and target_key not in fields:
                fields.append(target_key)
            return [field for field in fields if field != target_key]

    total_keys = len(row_file_headers)
    progress.update(
        "connect",
        stage_fraction=0.75,
        detail=f"Building per-row compare field lists | keys={total_keys:,}",
        log=(
            f"[{object_name}] Building per-row compare fields | unique_ids={total_keys:,} "
            f"| mode={file_query_mode} | retrieval_fields={len(retrieval_fields)}"
        ),
    )

    # Rows from the same CSV share one headers list object — cache by identity.
    compare_fields_by_headers: dict[int, list[str]] = {}
    row_compare_fields: dict[str, list[str]] = {}
    prepared = 0
    for key, file_headers in row_file_headers.items():
        cache_key = id(file_headers)
        fields = compare_fields_by_headers.get(cache_key)
        if fields is None:
            fields = _compare_fields_for_headers(file_headers)
            compare_fields_by_headers[cache_key] = fields
        row_compare_fields[key] = fields
        prepared += 1
        if prepared == 1 or prepared % 100000 == 0 or prepared == total_keys:
            progress.update(
                "connect",
                stage_fraction=0.75 + (0.2 * prepared / max(total_keys, 1)),
                detail=f"Prepared compare fields {prepared:,}/{total_keys:,}",
                log=(
                    f"[{object_name}]   compare-field prep {prepared:,}/{total_keys:,} "
                    f"(header schemas cached={len(compare_fields_by_headers)})"
                ),
            )

    progress.update(
        "connect",
        stage_fraction=1.0,
        detail=f"Prepared compare fields for {object_name} ({len(retrieval_fields)} retrieval fields)",
        log=(
            f"[{object_name}] Compare-field prep complete | retrieval_fields={len(retrieval_fields)} "
            f"| header_schemas={len(compare_fields_by_headers)}"
        ),
    )

    def on_query_progress(event: dict) -> None:
        detail = str(event.get("message") or event.get("detail") or "").strip()
        fraction = float(event.get("stage_fraction") or 0.0)
        progress.update(
            "query_target",
            stage_fraction=fraction,
            expected_keys=len(expected_rows),
            detail=detail or f"Querying target {object_name}",
            log=detail or None,
            **{k: v for k, v in event.items() if k not in {"message", "detail", "stage_fraction"}},
        )

    progress.update(
        "query_target",
        stage_fraction=0.0,
        expected_keys=len(expected_rows),
        detail=(
            f"Querying target | mode={args.query_mode} match_field={target_key} "
            f"keys={len(expected_rows):,} fields={len(retrieval_fields)}"
        ),
        log=(
            f"[{object_name}] Querying target | mode={args.query_mode} "
            f"match_field={target_key} keys={len(expected_rows):,} fields={len(retrieval_fields)}"
        ),
    )
    target_rows = query_target_rows(
        target, object_name, target_key, retrieval_fields,
        [row[target_key] for row in expected_rows],
        print_soql=args.print_soql, query_mode=args.query_mode,
        progress_callback=on_query_progress,
        id_chunk_size=args.id_chunk_size,
    )
    progress.update(
        "query_target",
        stage_fraction=1.0,
        expected_keys=len(expected_rows),
        detail=f"Target retrieval complete | rows={len(target_rows):,}",
        log=f"[{object_name}] Target retrieval complete | rows={len(target_rows):,}",
    )

    if file_query_mode != "custom_soql":
        compare_fields = [field for field in query_fields if field != target_key]
    field_types = {f: profile.field_meta[f].field_type for f in compare_fields if f in profile.field_meta}
    progress.update(
        "compare",
        stage_fraction=0.1,
        detail=f"Comparing rows | compare_fields={len(compare_fields)}",
        log=f"[{object_name}] Comparing rows | compare_fields={len(compare_fields)}",
    )
    matched, missing, mismatched, differences = compare_rows(
        object_name,
        expected_rows,
        target_rows,
        target_key,
        compare_fields,
        field_types,
        row_compare_fields=row_compare_fields,
    )
    differences.extend(duplicate_rows)
    differences.extend(invalid_id_rows)
    for difference in differences:
        difference.setdefault(
            "source_file",
            source_file_by_key.get(str(difference.get("external_id") or ""), "__unknown_source__"),
        )
    progress.update(
        "compare",
        stage_fraction=1.0,
        detail=(
            f"Compare complete | matched={matched:,} missing={missing:,} "
            f"mismatched={mismatched:,} diff_rows={len(differences):,}"
        ),
        log=(
            f"[{object_name}] Compare complete | matched={matched:,} missing={missing:,} "
            f"mismatched={mismatched:,} diff_rows={len(differences):,}"
        ),
    )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    progress.update(
        "write_reports",
        stage_fraction=0.1,
        detail=f"Writing reports to {out_dir}",
        log=f"[{object_name}] Writing reports to {out_dir}",
    )
    field_summary = summarize_mismatches_by_field(differences, expected_rows=len(expected_rows))
    field_summary_file = out_dir / f"{object_name}_mismatch_by_field_{stamp}.csv"
    with field_summary_file.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["field", "mismatch_count", "compared_rows", "pct_of_rows", "hint"],
        )
        writer.writeheader()
        writer.writerows(field_summary)

    target_csv_dir = out_dir / "retrieved_target_by_source"
    target_files = write_file_wise_target_csvs(
        target_csv_dir,
        object_name=object_name,
        target_key=target_key,
        retrieval_fields=retrieval_fields,
        target_rows=target_rows,
        ids_by_source_file=ids_by_source_file,
        stamp=stamp,
    )
    progress.update("write_reports", stage_fraction=0.55, detail="Wrote field summary and target CSVs")

    mismatch_partitions, mismatch_partition_index = write_partitioned_mismatch_csvs(
        out_dir,
        object_name=object_name,
        differences=differences,
        stamp=stamp,
    )
    progress.update(
        "write_reports",
        stage_fraction=0.72,
        detail=f"Wrote {len(mismatch_partitions):,} source-file/field mismatch partitions",
    )

    output_flags = detailed_outputs_allowed(
        mismatch_rows=len(differences),
        target_rows=len(target_rows),
    )
    mismatch_file = out_dir / f"{object_name}_verify_mismatches_{stamp}.csv"
    summary_file = out_dir / f"{object_name}_verify_summary_{stamp}.csv"
    workbook_file = out_dir / f"{object_name}_verify_results_{stamp}.xlsx"
    notes: list[str] = []

    if output_flags["write_mismatch_csv"]:
        headers = ["object_name", "source_file", "external_id", "field", "issue", "expected", "actual"]
        with mismatch_file.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers)
            writer.writeheader()
            writer.writerows(differences)
    else:
        notes.append(
            f"Full mismatch CSV skipped ({len(differences):,} field-level rows exceed "
            f"the detailed-output limit). Use {field_summary_file.name} to discover systematic fields, "
            "narrow with Include fields / Custom SOQL or add Ignore fields, and re-run."
        )
        skip_note = out_dir / f"{object_name}_mismatches_SKIPPED_{stamp}.txt"
        skip_note.write_text("\n".join(notes), encoding="utf-8")

    if output_flags["write_excel"]:
        write_validation_workbook(workbook_file, differences, target_rows, target_key, retrieval_fields)
    else:
        notes.append(
            f"Excel workbook skipped (mismatches={len(differences):,}, target_rows={len(target_rows):,}). "
            f"Retrieved target CSVs are under {target_csv_dir.name}/."
        )

    with summary_file.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "object_name", "file_query_mode", "expected_rows", "matched_rows", "missing_in_target",
            "mismatched_rows", "mismatched_fields", "duplicate_rows", "invalid_id_rows", "input_rows",
            "skipped_fields", "ignored_fields", "include_fields", "retrieval_fields", "source_files",
            "target_csv_files", "mismatch_partition_files", "mismatch_partition_index",
            "wrote_mismatch_csv", "wrote_excel", "notes",
        ])
        writer.writeheader()
        writer.writerow({
            "object_name": object_name,
            "file_query_mode": file_query_mode,
            "expected_rows": len(expected_rows),
            "matched_rows": matched,
            "missing_in_target": missing,
            "mismatched_rows": mismatched,
            "mismatched_fields": len(differences),
            "duplicate_rows": len(duplicate_rows),
            "invalid_id_rows": len(invalid_id_rows),
            "input_rows": eligible + len(invalid_id_rows),
            "skipped_fields": "; ".join(skipped),
            "ignored_fields": "; ".join(sorted(ignore_fields)),
            "include_fields": "; ".join(include_fields),
            "retrieval_fields": "; ".join(retrieval_fields),
            "source_files": len(manifest["files"]),
            "target_csv_files": len(target_files),
            "mismatch_partition_files": len(mismatch_partitions),
            "mismatch_partition_index": str(mismatch_partition_index),
            "wrote_mismatch_csv": output_flags["write_mismatch_csv"],
            "wrote_excel": output_flags["write_excel"],
            "notes": " | ".join(notes),
        })

    outputs_manifest = {
        "object_name": object_name,
        "file_query_mode": file_query_mode,
        "field_summary_csv": str(field_summary_file),
        "summary_csv": str(summary_file),
        "mismatch_csv": str(mismatch_file) if output_flags["write_mismatch_csv"] else None,
        "excel_workbook": str(workbook_file) if output_flags["write_excel"] else None,
        "retrieved_target_csvs": [str(path) for path in target_files],
        "mismatch_partition_index": str(mismatch_partition_index),
        "mismatch_partition_csvs": [str(path) for path in mismatch_partitions],
        "ignored_fields": sorted(ignore_fields),
        "include_fields": include_fields,
        "notes": notes,
    }
    (out_dir / f"{object_name}_outputs_{stamp}.json").write_text(
        json.dumps(outputs_manifest, indent=2), encoding="utf-8"
    )

    completed_msg = (
        f"[{object_name}] file validation completed | mode={file_query_mode} "
        f"files={len(manifest['files'])} "
        f"union_columns={len(all_headers)} unique={len(expected_rows)} matched={matched} "
        f"missing={missing} mismatched={mismatched} field_summary={field_summary_file.name} "
        f"target_csvs={len(target_files)} detailed_mismatch={output_flags['write_mismatch_csv']} "
        f"excel={output_flags['write_excel']}"
    )
    progress.update(
        "completed",
        detail=completed_msg.replace(f"[{object_name}] ", "", 1),
        log=completed_msg,
        matched_rows=matched,
        missing_rows=missing,
        mismatched_rows=mismatched,
    )
    for note in notes:
        _log(f"[{object_name}] NOTE: {note}")


if __name__ == "__main__":
    main()
