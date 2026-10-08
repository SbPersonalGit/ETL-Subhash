from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cli import build_connectors, get_latest_run_dir
from core.field_rules import build_field_profile
from verify_records import (
    DEFAULT_EXTERNAL_ID,
    build_object_config_map,
    load_config,
    normalize_value,
    query_target_rows,
    read_expected_rows,
    resolve_query_fields,
)


@dataclass
class LastLevelObjectResult:
    pipeline_id: str
    max_depth: int
    object_name: str
    node_ids: list[str]
    expected_rows: int
    relationship_fields_checked: int
    missing_in_target: int
    mismatched_rows: int
    mismatched_fields: int
    skipped_fields: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report max extraction depth and verify relationship fields for last-level objects."
    )
    parser.add_argument("--config", default="config/migration.yaml", help="Path to migration config.")
    parser.add_argument("--run-dir", default=None, help="Run directory containing xfm_*.csv files.")
    parser.add_argument("--pipeline", dest="pipelines", action="append", help="Pipeline id to inspect. Repeat for multiple.")
    parser.add_argument("--out-dir", default="logs/verify", help="Directory to write reports.")
    return parser.parse_args()


def get_pipeline_last_level_nodes(config: dict, selected_pipelines: list[str] | None) -> list[tuple[str, int, list[dict]]]:
    results: list[tuple[str, int, list[dict]]] = []
    for pipeline in config.get("pipelines", []):
        pipeline_id = pipeline.get("id")
        if not pipeline_id:
            continue
        if selected_pipelines and pipeline_id not in selected_pipelines:
            continue

        nodes = pipeline.get("nodes", []) or []
        if not nodes:
            continue

        max_depth = max(int(node.get("depth", 0) or 0) for node in nodes)
        last_nodes = [node for node in nodes if int(node.get("depth", 0) or 0) == max_depth]
        results.append((pipeline_id, max_depth, last_nodes))
    return results


def verify_last_level_object_relationships(
    pipeline_id: str,
    max_depth: int,
    node_ids: list[str],
    object_name: str,
    object_cfg: dict,
    run_dir: Path,
    target,
) -> tuple[LastLevelObjectResult, list[dict[str, str]]]:
    external_id_field = object_cfg.get("external_id") or DEFAULT_EXTERNAL_ID
    csv_path = run_dir / f"xfm_{object_name}.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"Transformed CSV not found: {csv_path}")

    expected_rows, candidate_fields = read_expected_rows(csv_path, external_id_field)
    target_profile = build_field_profile(target, object_name)
    query_fields, skipped_fields = resolve_query_fields(object_name, candidate_fields, target_profile)
    relationship_fields = [field for field in query_fields if "." in field]
    expected_ids = [row[external_id_field] for row in expected_rows]

    target_rows = query_target_rows(
        target,
        object_name,
        external_id_field,
        [external_id_field, *relationship_fields],
        expected_ids,
    )

    mismatch_rows: list[dict[str, str]] = []
    missing_in_target = 0
    mismatched_row_ids: set[str] = set()

    for expected in expected_rows:
        ext_id = expected[external_id_field]
        actual = target_rows.get(ext_id)
        if not actual:
            missing_in_target += 1
            mismatch_rows.append({
                "pipeline_id": pipeline_id,
                "max_depth": str(max_depth),
                "node_ids": ", ".join(node_ids),
                "object_name": object_name,
                "external_id": ext_id,
                "field": "__row__",
                "issue": "missing_in_target",
                "expected": "present",
                "actual": "",
            })
            continue

        row_has_mismatch = False
        for field_name in relationship_fields:
            expected_value = normalize_value(expected.get(field_name, ""))
            actual_value = normalize_value(actual.get(field_name, ""))
            if expected_value == actual_value:
                continue
            row_has_mismatch = True
            mismatch_rows.append({
                "pipeline_id": pipeline_id,
                "max_depth": str(max_depth),
                "node_ids": ", ".join(node_ids),
                "object_name": object_name,
                "external_id": ext_id,
                "field": field_name,
                "issue": "relationship_mismatch",
                "expected": expected_value,
                "actual": actual_value,
            })

        if row_has_mismatch:
            mismatched_row_ids.add(ext_id)

    result = LastLevelObjectResult(
        pipeline_id=pipeline_id,
        max_depth=max_depth,
        object_name=object_name,
        node_ids=node_ids,
        expected_rows=len(expected_rows),
        relationship_fields_checked=len(relationship_fields),
        missing_in_target=missing_in_target,
        mismatched_rows=len(mismatched_row_ids),
        mismatched_fields=len(mismatch_rows),
        skipped_fields=skipped_fields,
    )
    return result, mismatch_rows


def main():
    args = parse_args()
    config = load_config(Path(args.config))
    object_cfg_map = build_object_config_map(config)
    pipeline_levels = get_pipeline_last_level_nodes(config, args.pipelines)

    if not pipeline_levels:
        raise SystemExit("No pipeline nodes found for the selected pipeline(s).")

    run_dir = Path(args.run_dir) if args.run_dir else get_latest_run_dir()
    if not run_dir or not run_dir.exists():
        raise SystemExit("No run directory found. Pass --run-dir or create an export run first.")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    _, target = build_connectors()
    target.connect()

    print(f"Run dir: {run_dir}")
    print(f"Target: {target.instance_url}")
    print("Note: comparison is object-level for last-depth objects. If the same object appears on multiple depths, rows are not separated by node in current exports.")

    summary_rows: list[dict[str, str]] = []
    mismatch_rows_all: list[dict[str, str]] = []

    for pipeline_id, max_depth, last_nodes in pipeline_levels:
        print(f"\n[{pipeline_id}] max_depth={max_depth}")
        for node in last_nodes:
            print(f"  last node: {node.get('id')} -> {node.get('object')}")

        node_ids_by_object: dict[str, list[str]] = defaultdict(list)
        for node in last_nodes:
            object_name = node.get("object")
            node_id = node.get("id")
            if object_name and node_id:
                node_ids_by_object[object_name].append(node_id)

        for object_name, node_ids in sorted(node_ids_by_object.items()):
            object_cfg = object_cfg_map.get(object_name)
            if not object_cfg:
                print(f"  [{object_name}] skipped: object not found in migration config")
                continue

            result, mismatch_rows = verify_last_level_object_relationships(
                pipeline_id=pipeline_id,
                max_depth=max_depth,
                node_ids=node_ids,
                object_name=object_name,
                object_cfg=object_cfg,
                run_dir=run_dir,
                target=target,
            )
            mismatch_rows_all.extend(mismatch_rows)
            summary_rows.append({
                "pipeline_id": result.pipeline_id,
                "max_depth": str(result.max_depth),
                "object_name": result.object_name,
                "node_ids": ", ".join(result.node_ids),
                "expected_rows": str(result.expected_rows),
                "relationship_fields_checked": str(result.relationship_fields_checked),
                "missing_in_target": str(result.missing_in_target),
                "mismatched_rows": str(result.mismatched_rows),
                "mismatched_fields": str(result.mismatched_fields),
                "skipped_fields": ", ".join(result.skipped_fields),
            })
            print(
                f"  [{object_name}] rows={result.expected_rows} "
                f"relationship_fields={result.relationship_fields_checked} "
                f"missing={result.missing_in_target} "
                f"mismatched_rows={result.mismatched_rows} "
                f"mismatched_fields={result.mismatched_fields}"
            )

    summary_file = out_dir / f"last_level_reference_summary_{timestamp}.csv"
    mismatch_file = out_dir / f"last_level_reference_mismatches_{timestamp}.csv"

    with open(summary_file, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "pipeline_id",
                "max_depth",
                "object_name",
                "node_ids",
                "expected_rows",
                "relationship_fields_checked",
                "missing_in_target",
                "mismatched_rows",
                "mismatched_fields",
                "skipped_fields",
            ],
        )
        writer.writeheader()
        writer.writerows(summary_rows)

    with open(mismatch_file, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "pipeline_id",
                "max_depth",
                "node_ids",
                "object_name",
                "external_id",
                "field",
                "issue",
                "expected",
                "actual",
            ],
        )
        writer.writeheader()
        writer.writerows(mismatch_rows_all)

    print(f"\nSummary:  {summary_file}")
    print(f"Mismatch: {mismatch_file}")


if __name__ == "__main__":
    main()
