from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from cli import build_connectors, get_latest_run_dir
from core.field_rules import build_field_profile
from verify_records import (
    DEFAULT_EXTERNAL_ID,
    build_object_config_map,
    chunked,
    escape_soql,
    find_xfm_csv,
    flatten_record,
    load_config,
    normalize_value,
    query_target_rows,
    read_expected_rows,
    resolve_query_fields,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare source vs target reference fields for objects at the actual last reached topology levels."
    )
    parser.add_argument("--config", default="config/migration.yaml", help="Path to migration config.")
    parser.add_argument("--run-dir", default=None, help="Run directory containing exports and topology reports.")
    parser.add_argument("--pipeline", dest="pipelines", action="append", help="Pipeline id to inspect. Repeat for multiple.")
    parser.add_argument("--out-dir", default="logs/verify", help="Directory to write reports.")
    parser.add_argument(
        "--from-last-level",
        type=int,
        default=0,
        help="How many levels back from the actual deepest reached level to include. 0=last only, 1=last two levels, 3=last four levels.",
    )
    return parser.parse_args()


def load_topology_rows(run_dir: Path, pipeline_id: str) -> list[dict[str, str]]:
    candidate_names = [
        f"topology_{pipeline_id}_actual_depth.csv",
        f"{pipeline_id}_topology_actual_depth.csv",
    ]
    candidate_paths = [run_dir / name for name in candidate_names]
    candidate_paths.extend(run_dir / pipeline_id / "extract" / name for name in candidate_names)
    for attempt_dir in sorted(run_dir.glob(f"{pipeline_id}_attempt_*")):
        if attempt_dir.is_dir():
            attempt_names = [
                *candidate_names,
                f"topology_{attempt_dir.name}_actual_depth.csv",
                f"{attempt_dir.name}_topology_actual_depth.csv",
            ]
            candidate_paths.extend(attempt_dir / "extract" / name for name in attempt_names)
    candidate_paths.extend((Path("logs") / run_dir.name / pipeline_id / "extract" / name) for name in candidate_names)
    logs_run_dir = Path("logs") / run_dir.name
    for attempt_dir in sorted(logs_run_dir.glob(f"{pipeline_id}_attempt_*")):
        if attempt_dir.is_dir():
            attempt_names = [
                *candidate_names,
                f"topology_{attempt_dir.name}_actual_depth.csv",
                f"{attempt_dir.name}_topology_actual_depth.csv",
            ]
            candidate_paths.extend(attempt_dir / "extract" / name for name in attempt_names)
    report_path = next((path for path in candidate_paths if path.exists()), None)
    if not report_path:
        fallback_candidates: list[Path] = []
        logs_root = Path("logs")
        if logs_root.exists():
            for run_path in logs_root.iterdir():
                if not run_path.is_dir() or not run_path.name.startswith("run_"):
                    continue
                for name in candidate_names:
                    candidate = run_path / pipeline_id / "extract" / name
                    if candidate.exists():
                        fallback_candidates.append(candidate)
        if fallback_candidates:
            report_path = max(fallback_candidates, key=lambda path: path.stat().st_mtime)
    if not report_path:
        raise FileNotFoundError(
            f"Topology report not found for pipeline {pipeline_id}. "
            f"Checked: {', '.join(str(path) for path in candidate_paths)}"
        )
    with open(report_path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def query_source_rows(source, object_name: str, source_ids: list[str], fields: list[str]) -> dict[str, dict[str, str]]:
    selected_fields = ["Id"] + [field for field in fields if field != "Id"]
    select_clause = ", ".join(dict.fromkeys(selected_fields))
    rows: dict[str, dict[str, str]] = {}
    for id_chunk in chunked(source_ids, 200):
        ids_csv = "', '".join(escape_soql(value) for value in id_chunk)
        soql = f"SELECT {select_clause} FROM {object_name} WHERE Id IN ('{ids_csv}')"
        for record in source.query(soql):
            flat = flatten_record(record)
            record_id = flat.get("Id", "")
            if record_id:
                rows[record_id] = flat
    return rows


def main():
    args = parse_args()
    config = load_config(Path(args.config))
    object_cfg_map = build_object_config_map(config)
    run_dir = Path(args.run_dir) if args.run_dir else get_latest_run_dir()
    if not run_dir or not run_dir.exists():
        raise SystemExit("No run directory found. Pass --run-dir or create an extract run first.")

    selected_pipeline_ids = set(args.pipelines or [])
    pipelines = [
        pipeline
        for pipeline in config.get("pipelines", [])
        if pipeline.get("id") and (not selected_pipeline_ids or pipeline.get("id") in selected_pipeline_ids)
    ]
    if not pipelines:
        raise SystemExit("No matching pipelines found in config.")

    source, target = build_connectors()
    source.connect()
    target.connect()

    print(f"Run dir: {run_dir}")
    print(f"Source: {source.instance_url}")
    print(f"Target: {target.instance_url}")
    print("Assumption: the configured external ID in xfm is the migrated source record identity for matching source rows.")

    summary_rows: list[dict[str, str]] = []
    mismatch_rows: list[dict[str, str]] = []

    for pipeline in pipelines:
        pipeline_id = pipeline["id"]
        topology_rows = load_topology_rows(run_dir, pipeline_id)
        reached_rows = [
            row for row in topology_rows
            if int(row.get("discovered_row_count") or 0) > 0
        ]
        actual_max_depth = max(
            (int(row.get("level") or 0) for row in reached_rows),
            default=-1,
        )
        min_included_depth = max(0, actual_max_depth - max(0, args.from_last_level))
        included_level_rows = [
            row for row in reached_rows
            if min_included_depth <= int(row.get("level") or 0) <= actual_max_depth
        ]

        print(
            f"\n[{pipeline_id}] actual_max_depth={actual_max_depth} "
            f"| included_levels={min_included_depth}..{actual_max_depth}"
        )
        node_details_by_object: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for row in included_level_rows:
            object_name = row.get("object_name", "")
            node_id = row.get("node_id", "")
            level = int(row.get("level") or 0)
            if object_name and node_id:
                node_details_by_object[object_name].append((node_id, level))

        for object_name, node_details in sorted(node_details_by_object.items()):
            object_cfg = object_cfg_map.get(object_name)
            if not object_cfg:
                print(f"  [{object_name}] skipped: not found in migration config")
                continue
            node_ids = sorted({node_id for node_id, _ in node_details})
            levels = sorted({level for _, level in node_details})

            external_id_field = object_cfg.get("external_id") or DEFAULT_EXTERNAL_ID
            try:
                xfm_path = find_xfm_csv(run_dir, object_name, pipeline_id=pipeline_id)
            except FileNotFoundError:
                print(f"  [{object_name}] skipped: xfm file not found")
                continue

            expected_rows, _ = read_expected_rows(xfm_path, external_id_field)
            source_ids = [
                row[external_id_field]
                for row in expected_rows
                if row.get(external_id_field)
            ]
            if not source_ids:
                print(f"  [{object_name}] skipped: no external IDs in xfm")
                continue

            profile = build_field_profile(source, object_name)
            target_profile = build_field_profile(target, object_name)
            selected_fields = set(object_cfg.get("fields", []) or [])
            omitted_lookup_fields = [
                field_name
                for field_name in sorted(profile.lookup_fields.keys())
                if selected_fields and field_name not in selected_fields
            ]

            compare_specs: list[dict[str, str]] = []
            raw_target_query_fields: list[str] = []
            for field_name in omitted_lookup_fields:
                meta = profile.field_meta.get(field_name)
                refs = profile.lookup_fields.get(field_name, [])
                target_field = field_name
                compare_mode = "presence_only"
                if meta and meta.relationship_name and len(refs) == 1:
                    ref_object = refs[0]
                    ref_external_id = object_cfg_map.get(ref_object, {}).get("external_id")
                    if ref_external_id:
                        target_field = f"{meta.relationship_name}.{ref_external_id}"
                        compare_mode = "external_id_match"

                compare_specs.append({
                    "source_field": field_name,
                    "target_field": target_field,
                    "compare_mode": compare_mode,
                    "references_to": ", ".join(refs),
                })
                raw_target_query_fields.append(target_field)

            target_query_fields, skipped_target_fields = resolve_query_fields(
                object_name,
                raw_target_query_fields,
                target_profile,
            )
            compare_specs = [
                spec for spec in compare_specs
                if spec["target_field"] in target_query_fields
            ]

            source_rows = query_source_rows(source, object_name, source_ids, omitted_lookup_fields)
            target_rows = query_target_rows(
                target,
                object_name,
                external_id_field,
                [external_id_field, *target_query_fields],
                source_ids,
            )

            object_mismatched_row_ids: set[str] = set()
            object_missing_in_target = 0

            for source_id in source_ids:
                source_row = source_rows.get(source_id, {})
                target_row = target_rows.get(source_id)
                if not target_row:
                    object_missing_in_target += 1
                    mismatch_rows.append({
                        "pipeline_id": pipeline_id,
                        "actual_max_depth": str(actual_max_depth),
                        "object_name": object_name,
                        "node_ids": ", ".join(sorted(node_ids)),
                        "source_id": source_id,
                        "source_field": "__row__",
                        "target_field": "__row__",
                        "compare_mode": "row_missing_in_target",
                        "references_to": "",
                        "source_value": "present",
                        "target_value": "",
                        "issue": "missing_in_target",
                    })
                    continue

                for spec in compare_specs:
                    source_value = normalize_value(source_row.get(spec["source_field"], ""))
                    target_value = normalize_value(target_row.get(spec["target_field"], ""))
                    source_present = bool(source_value)
                    target_present = bool(target_value)

                    issue = ""
                    if spec["compare_mode"] == "external_id_match":
                        if source_value != target_value:
                            issue = "source_target_reference_mismatch"
                    else:
                        if source_present and not target_present:
                            issue = "source_present_target_blank"
                        elif (not source_present) and target_present:
                            issue = "source_blank_target_present"

                    if not issue:
                        continue

                    object_mismatched_row_ids.add(source_id)
                    mismatch_rows.append({
                        "pipeline_id": pipeline_id,
                        "actual_max_depth": str(actual_max_depth),
                        "included_levels": f"{min_included_depth}..{actual_max_depth}",
                        "object_levels": ", ".join(str(level) for level in levels),
                        "object_name": object_name,
                        "node_ids": ", ".join(sorted(node_ids)),
                        "source_id": source_id,
                        "source_field": spec["source_field"],
                        "target_field": spec["target_field"],
                        "compare_mode": spec["compare_mode"],
                        "references_to": spec["references_to"],
                        "source_value": source_value,
                        "target_value": target_value,
                        "issue": issue,
                    })

            summary_rows.append({
                "pipeline_id": pipeline_id,
                "actual_max_depth": str(actual_max_depth),
                "included_levels": f"{min_included_depth}..{actual_max_depth}",
                "object_levels": ", ".join(str(level) for level in levels),
                "object_name": object_name,
                "node_ids": ", ".join(sorted(node_ids)),
                "source_rows_checked": str(len(source_ids)),
                "omitted_lookup_fields_checked": str(len(compare_specs)),
                "missing_in_target": str(object_missing_in_target),
                "mismatched_rows": str(len(object_mismatched_row_ids)),
                "mismatched_fields": str(
                    sum(1 for row in mismatch_rows if row["pipeline_id"] == pipeline_id and row["object_name"] == object_name)
                ),
            })

            print(
                f"  [{object_name}] rows={len(source_ids)} "
                f"levels={', '.join(str(level) for level in levels)} "
                f"omitted_lookup_fields={len(compare_specs)} "
                f"skipped_on_target={len(skipped_target_fields)} "
                f"missing_in_target={object_missing_in_target} "
                f"mismatched_rows={len(object_mismatched_row_ids)}"
            )
            if skipped_target_fields:
                print(f"    skipped target fields: {', '.join(skipped_target_fields)}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary_path = out_dir / f"last_level_source_target_ref_summary_{timestamp}.csv"
    mismatch_path = out_dir / f"last_level_source_target_ref_mismatches_{timestamp}.csv"

    with open(summary_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "pipeline_id",
                "actual_max_depth",
                "included_levels",
                "object_levels",
                "object_name",
                "node_ids",
                "source_rows_checked",
                "omitted_lookup_fields_checked",
                "missing_in_target",
                "mismatched_rows",
                "mismatched_fields",
            ],
        )
        writer.writeheader()
        writer.writerows(summary_rows)

    with open(mismatch_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "pipeline_id",
                "actual_max_depth",
                "included_levels",
                "object_levels",
                "object_name",
                "node_ids",
                "source_id",
                "source_field",
                "target_field",
                "compare_mode",
                "references_to",
                "source_value",
                "target_value",
                "issue",
            ],
        )
        writer.writeheader()
        writer.writerows(mismatch_rows)

    print(f"\nSummary:  {summary_path}")
    print(f"Mismatch: {mismatch_path}")


if __name__ == "__main__":
    main()
