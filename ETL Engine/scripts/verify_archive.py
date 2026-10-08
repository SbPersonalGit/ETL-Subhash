"""Verify ETL archive package hashes, ZIP integrity, members, and DuckDB copies."""

from __future__ import annotations

import argparse
import shutil
import uuid
from pathlib import Path

from archive_common import (
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
    parser.add_argument("--deep", action="store_true", help="Extract and verify every member.")
    parser.add_argument(
        "--work-root",
        type=Path,
        default=root / "runtime" / "package_staging" / "verify_work",
        help="Explicit temporary extraction root used for deep verification.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest_path = args.manifest.resolve()
    manifest = load_json(manifest_path)
    archive_dir = manifest_path.parent
    errors: list[str] = []

    for component, info in manifest.get("components", {}).items():
        package = info["package"]
        package_path = archive_dir / package["name"]
        errors.extend(verify_package(package_path, package))
        if not args.deep or errors:
            continue

        extracted = (
            args.work_root.resolve()
            / f"verify_{manifest.get('run_id', 'archive')}_{component}_{uuid.uuid4().hex}"
        )
        try:
            safe_extract(package_path, extracted)
            for expected in info.get("files", []):
                path = extracted / expected["path"]
                if not path.is_file():
                    errors.append(f"Missing archive member: {component}/{expected['path']}")
                    continue
                if path.stat().st_size != expected["size"]:
                    errors.append(f"Member size mismatch: {component}/{expected['path']}")
                if sha256_file(path) != expected["sha256"]:
                    errors.append(f"Member hash mismatch: {component}/{expected['path']}")
                if path.suffix.lower() == ".duckdb":
                    try:
                        actual = validate_duckdb(path)
                        expected_db = manifest.get("duckdb", {}).get(path.name)
                        if expected_db and actual["tables"] != expected_db["tables"]:
                            errors.append(f"DuckDB table-count mismatch: {path.name}")
                    except Exception as exc:
                        errors.append(f"DuckDB validation failed for {path.name}: {exc}")
        finally:
            if extracted.exists():
                shutil.rmtree(extracted)

    if errors:
        print("Archive verification FAILED:")
        for error in errors:
            print(f"  - {error}")
        return 1

    level = "deep" if args.deep else "package"
    print(f"Archive verification passed ({level}): {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
