"""Safely extract and optionally apply an ETL run archive."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from archive_common import (
    COMPONENTS,
    load_json,
    project_root,
    safe_extract,
    sha256_file,
    validate_duckdb,
    verify_package,
)


def parse_args() -> argparse.Namespace:
    root = project_root()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--etl-root", type=Path, default=root / "runtime" / "active")
    parser.add_argument("--restore-root", type=Path, default=root / "runtime" / "restore")
    parser.add_argument(
        "--scope",
        choices=["all", *COMPONENTS],
        default="all",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Copy validated restored files into etl-root. Without this, extract only.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest_path = args.manifest.resolve()
    manifest = load_json(manifest_path)
    run_id = manifest["run_id"]
    archive_dir = manifest_path.parent
    restore_dir = args.restore_root.resolve() / run_id
    etl_root = args.etl_root.resolve()
    selected = (
        list(manifest.get("components", {}))
        if args.scope == "all"
        else [args.scope]
    )

    reuse_prepared_restore = restore_dir.exists() and any(restore_dir.iterdir())
    if reuse_prepared_restore and not args.apply:
        raise SystemExit(
            f"Restore destination is not empty: {restore_dir}. "
            "Inspect it, then use --apply, or choose a clean restore root."
        )
    restore_dir.mkdir(parents=True, exist_ok=True)

    for component in selected:
        info = manifest.get("components", {}).get(component)
        if not info:
            print(f"Skipping absent component: {component}")
            continue
        package_path = archive_dir / info["package"]["name"]
        errors = verify_package(package_path, info["package"])
        if errors:
            raise SystemExit("\n".join(errors))
        component_dir = restore_dir / component
        if not reuse_prepared_restore:
            safe_extract(package_path, component_dir)

        for expected in info.get("files", []):
            path = component_dir / expected["path"]
            if not path.is_file():
                raise SystemExit(f"Missing restored file: {path}")
            if path.stat().st_size != expected["size"]:
                raise SystemExit(f"Restored file size mismatch: {path}")
            if sha256_file(path) != expected["sha256"]:
                raise SystemExit(f"Restored file checksum mismatch: {path}")
            if path.suffix.lower() == ".duckdb":
                actual = validate_duckdb(path)
                archived = manifest.get("duckdb", {}).get(path.name)
                if archived and actual["tables"] != archived["tables"]:
                    raise SystemExit(f"Restored DuckDB table-count mismatch: {path}")

    print(f"Validated restore prepared at: {restore_dir}")
    if not args.apply:
        print("No active files changed. Re-run with --apply after inspection.")
        return 0

    conflicts: list[Path] = []
    copy_plan: list[tuple[Path, Path]] = []
    for component in selected:
        component_dir = restore_dir / component
        if not component_dir.is_dir():
            continue
        for source in component_dir.rglob("*"):
            if not source.is_file():
                continue
            if component == "config":
                destination = etl_root / "config" / source.name
            else:
                destination = etl_root / source.relative_to(component_dir)
            copy_plan.append((source, destination))
            if destination.exists():
                conflicts.append(destination)

    if conflicts:
        preview = "\n".join(f"  - {path}" for path in conflicts[:20])
        raise SystemExit(
            "Restore refused because destination files already exist:\n"
            f"{preview}\nNo files were overwritten."
        )

    for source, destination in copy_plan:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    print(f"Restore applied to: {etl_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
