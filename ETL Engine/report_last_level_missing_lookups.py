from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from cli import build_connectors, get_latest_run_dir
from core.field_rules import build_field_profile
from verify_records import load_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List source-org lookup fields on the actual last reached level that were not selected in config."
    )
    parser.add_argument("--config", default="config/migration.yaml", help="Path to migration config.")
    parser.add_argument("--run-dir", default=None, help="Run directory containing topology_<pipeline>_actual_depth.csv files.")
    parser.add_argument("--pipeline", dest="pipelines", action="append", help="Pipeline id to inspect. Repeat for multiple.")
    parser.add_argument("--out-dir", default="logs/verify", help="Directory to write reports.")
    return parser.parse_args()


def load_topology_rows(run_dir: Path, pipeline_id: str) -> list[dict[str, str]]:
    report_path = run_dir / f"topology_{pipeline_id}_actual_depth.csv"
    if not report_path.exists():
        raise FileNotFoundError(f"Topology report not found: {report_path}")
    with open(report_path, encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader)


def get_pipeline_configs(config: dict, selected_pipelines: list[str] | None) -> list[dict]:
    pipelines = []
    for pipeline in config.get("pipelines", []):
        pipeline_id = pipeline.get("id")
        if not pipeline_id:
            continue
        if selected_pipelines and pipeline_id not in selected_pipelines:
            continue
        pipelines.append(pipeline)
    return pipelines


def main():
    args = parse_args()
    config = load_config(args.config)
    run_dir = Path(args.run_dir) if args.run_dir else get_latest_run_dir()
    if not run_dir or not run_dir.exists():
        raise SystemExit("No run directory found. Pass --run-dir or create an extract run first.")

    pipelines = get_pipeline_configs(config, args.pipelines)
    if not pipelines:
        raise SystemExit("No matching pipelines found in config.")

    source, _ = build_connectors()
    source.connect()

    report_rows: list[dict[str, str]] = []

    print(f"Run dir: {run_dir}")
    print(f"Source: {source.instance_url}")

    for pipeline in pipelines:
        pipeline_id = pipeline["id"]
        rows = load_topology_rows(run_dir, pipeline_id)
        actual_reached_rows = [
            row for row in rows
            if int(row.get("discovered_row_count") or 0) > 0
        ]
        actual_max_depth = max(
            (int(row.get("level") or 0) for row in actual_reached_rows),
            default=-1,
        )
        last_level_rows = [
            row for row in actual_reached_rows
            if int(row.get("level") or 0) == actual_max_depth
        ]

        object_cfg_by_name = {
            obj["name"]: obj
            for obj in pipeline.get("objects", [])
            if obj.get("name")
        }
        node_ids_by_object: dict[str, list[str]] = defaultdict(list)
        for row in last_level_rows:
            object_name = row.get("object_name", "")
            node_id = row.get("node_id", "")
            if object_name and node_id:
                node_ids_by_object[object_name].append(node_id)

        print(f"\n[{pipeline_id}] actual_max_depth={actual_max_depth}")
        for object_name, node_ids in sorted(node_ids_by_object.items()):
            object_cfg = object_cfg_by_name.get(object_name, {})
            selected_fields = set(object_cfg.get("fields", []) or [])
            profile = build_field_profile(source, object_name)
            missing_lookups = []
            for field_name, refs in sorted(profile.lookup_fields.items()):
                if selected_fields and field_name in selected_fields:
                    continue
                if not selected_fields and field_name in profile.include_fields:
                    continue
                meta = profile.field_meta.get(field_name)
                missing_lookups.append({
                    "pipeline_id": pipeline_id,
                    "actual_max_depth": str(actual_max_depth),
                    "object_name": object_name,
                    "node_ids": ", ".join(sorted(node_ids)),
                    "lookup_field": field_name,
                    "relationship_name": meta.relationship_name if meta else "",
                    "references_to": ", ".join(refs),
                    "is_required": str(bool(meta.is_required)) if meta else "",
                    "selected_fields_configured": "yes" if selected_fields else "no",
                })

            print(
                f"  [{object_name}] nodes={', '.join(sorted(node_ids))} "
                f"missing_lookup_fields={len(missing_lookups)}"
            )
            report_rows.extend(missing_lookups)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = out_dir / f"last_level_missing_lookups_{timestamp}.csv"

    with open(report_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "pipeline_id",
                "actual_max_depth",
                "object_name",
                "node_ids",
                "lookup_field",
                "relationship_name",
                "references_to",
                "is_required",
                "selected_fields_configured",
            ],
        )
        writer.writeheader()
        writer.writerows(report_rows)

    print(f"\nReport: {report_path}")


if __name__ == "__main__":
    main()
