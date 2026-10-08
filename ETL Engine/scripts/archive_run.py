"""Create immutable, checksummed component archives for one ETL run."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from archive_common import (
    ARCHIVE_SCHEMA_VERSION,
    COMPONENTS,
    copy_file,
    create_zip,
    file_record,
    is_sensitive,
    project_root,
    sha256_file,
    utc_now,
    validate_duckdb,
    write_json,
)


def parse_args() -> argparse.Namespace:
    root = project_root()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--etl-root", type=Path, default=root / "runtime" / "active")
    parser.add_argument("--archive-root", type=Path, default=root / "runtime" / "archive")
    parser.add_argument(
        "--staging-root",
        type=Path,
        default=root / "runtime" / "package_staging",
    )
    parser.add_argument(
        "--config-file",
        action="append",
        type=Path,
        default=[],
        help="Non-secret configuration/template file to include. Repeat as needed.",
    )
    return parser.parse_args()


def matching_files(root: Path, run_id: str) -> dict[str, list[Path]]:
    discovered: dict[str, list[Path]] = {name: [] for name in COMPONENTS}

    export_dir = root / "exports" / run_id
    if export_dir.is_dir():
        discovered["exports"].extend(path for path in export_dir.rglob("*") if path.is_file())

    state_dir = root / "state"
    if state_dir.is_dir():
        discovered["state"].extend(
            path for path in state_dir.rglob("*")
            if path.is_file() and run_id in path.as_posix()
        )

    log_dir = root / "logs" / run_id
    if log_dir.is_dir():
        discovered["logs"].extend(path for path in log_dir.rglob("*") if path.is_file())

    verification_dir = root / "verification" / run_id
    if verification_dir.is_dir():
        discovered["verification"].extend(
            path for path in verification_dir.rglob("*") if path.is_file()
        )
    return discovered


def main() -> int:
    args = parse_args()
    etl_root = args.etl_root.resolve()
    archive_root = args.archive_root.resolve()
    staging_root = args.staging_root.resolve()
    run_id = args.run_id.strip()
    if not run_id or "/" in run_id or "\\" in run_id:
        raise SystemExit("run-id must be one folder-safe name.")
    if not etl_root.is_dir():
        raise SystemExit(f"ETL root not found: {etl_root}")

    discovered = matching_files(etl_root, run_id)
    for config_path in args.config_file:
        resolved = config_path.resolve()
        if not resolved.is_file():
            raise SystemExit(f"Config file not found: {resolved}")
        discovered["config"].append(resolved)
    config_names = [path.name.lower() for path in discovered["config"]]
    if len(config_names) != len(set(config_names)):
        raise SystemExit("Config files must have unique filenames inside the archive.")

    all_sources = [path for paths in discovered.values() for path in paths]
    if not all_sources:
        raise SystemExit(f"No artifacts found for run {run_id} under {etl_root}")
    sensitive = [path for path in all_sources if is_sensitive(path)]
    if sensitive:
        names = ", ".join(str(path) for path in sensitive)
        raise SystemExit(f"Refusing to archive sensitive file(s): {names}")

    duckdb_sources = [
        path for path in discovered["state"] if path.suffix.lower() == ".duckdb"
    ]
    wal_files = [
        path for path in discovered["state"] if path.name.lower().endswith(".duckdb.wal")
    ]
    if wal_files:
        names = ", ".join(str(path) for path in wal_files)
        raise SystemExit(
            "Refusing to archive while DuckDB WAL file(s) exist. "
            f"Checkpoint and close the databases first: {names}"
        )

    package_staging = staging_root / run_id
    if package_staging.exists():
        raise SystemExit(
            f"Package staging already exists: {package_staging}. "
            "Move or remove it after confirming it is not needed."
        )
    package_staging.mkdir(parents=True)

    manifest: dict = {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": utc_now(),
        "status": "PACKAGING",
        "etl_root": str(etl_root),
        "components": {},
        "duckdb": {},
    }

    try:
        for component, sources in discovered.items():
            if not sources:
                continue
            component_root = package_staging / component
            copied: list[Path] = []
            for source in sorted(set(sources)):
                if component == "config":
                    relative = Path(source.name)
                else:
                    relative = source.relative_to(etl_root)
                destination = component_root / relative
                copy_file(source, destination)
                copied.append(destination)

            manifest["components"][component] = {
                "files": [file_record(path, component_root) for path in copied],
            }

        for source in duckdb_sources:
            copied_matches = list(package_staging.rglob(source.name))
            if len(copied_matches) != 1:
                raise RuntimeError(f"Could not uniquely locate copied DuckDB: {source.name}")
            manifest["duckdb"][source.name] = validate_duckdb(copied_matches[0])

        created_date = manifest["created_at"]
        run_archive_dir = archive_root / created_date[:4] / created_date[5:7] / run_id
        if run_archive_dir.exists() and any(run_archive_dir.iterdir()):
            raise RuntimeError(
                f"Archive destination already exists and is not empty: {run_archive_dir}"
            )
        run_archive_dir.mkdir(parents=True, exist_ok=True)
        for component in list(manifest["components"]):
            component_root = package_staging / component
            files = [path for path in component_root.rglob("*") if path.is_file()]
            package_path = run_archive_dir / f"{component}.zip"
            create_zip(component_root, package_path, files)
            manifest["components"][component]["package"] = {
                "name": package_path.name,
                "size": package_path.stat().st_size,
                "sha256": sha256_file(package_path),
            }

        manifest["status"] = "PACKAGED_LOCAL"
        manifest["archive_dir"] = str(run_archive_dir)
        manifest_path = run_archive_dir / "manifest.json"
        write_json(manifest_path, manifest)
        write_json(run_archive_dir / "checksums.sha256.json", {
            name: info["package"]
            for name, info in manifest["components"].items()
        })
        print(f"Archive created: {run_archive_dir}")
        print(f"Manifest: {manifest_path}")
        print("No active ETL files were deleted.")
        return 0
    except Exception:
        manifest["status"] = "ARCHIVE_FAILED"
        failure_manifest = package_staging / "manifest.failed.json"
        write_json(failure_manifest, manifest)
        raise
    finally:
        # Retain failed staging for diagnosis; remove successful duplicate staging.
        if manifest["status"] == "PACKAGED_LOCAL" and package_staging.exists():
            shutil.rmtree(package_staging)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit("Archive cancelled.")
