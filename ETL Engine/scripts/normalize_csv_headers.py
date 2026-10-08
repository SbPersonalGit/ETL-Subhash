#!/usr/bin/env python3
"""Normalize CSV headers across all files in a folder.

Requirements:
- Input: a folder path.
- Read each CSV in that folder.
- Detect header row from the first line (field names).
- Collect the full union of all column names across files.
- For each CSV file, add any missing columns at the end with blank values.
- Preserve existing data and column order as much as possible.
- Do not remove or rename existing columns.

Example:
    python scripts/normalize_csv_headers.py --folder "state/flat_file_drop/Case"
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


def is_csv_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() == ".csv"


def read_csv_rows(path: Path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            rows = list(reader)
        except csv.Error as exc:
            raise ValueError(f"Unable to parse CSV: {path}: {exc}") from exc
    if not rows:
        return []
    return rows


def detect_header_row(rows):
    if not rows:
        return []
    return rows[0]


def normalize_value(value):
    return "" if value is None else str(value)


def ensure_missing_columns_in_file(path: Path, all_columns: list[str]) -> bool:
    rows = read_csv_rows(path)
    if not rows:
        return False

    header = detect_header_row(rows)
    if not header:
        return False

    existing_columns = list(header)
    missing = [col for col in all_columns if col not in existing_columns]
    if not missing:
        return False

    # Preserve original order; append missing columns at the end.
    new_header = existing_columns + missing
    row_lists = []
    for index, row in enumerate(rows[1:]):
        row_values = list(row)
        while len(row_values) < len(new_header):
            row_values.append("")
        row_values = row_values[: len(new_header)]
        row_lists.append(row_values)

    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(new_header)
        for row_values in row_lists:
            writer.writerow(row_values)

    return True


def collect_all_columns(folder: Path) -> list[str]:
    columns_seen: list[str] = []
    seen_set: set[str] = set()

    for csv_file in sorted(folder.rglob("*.csv")):
        rows = read_csv_rows(csv_file)
        if not rows:
            continue
        header = detect_header_row(rows)
        for col in header:
            if col not in seen_set:
                seen_set.add(col)
                columns_seen.append(col)

    return columns_seen


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add missing CSV columns across files in a folder so they all share the same header union."
    )
    parser.add_argument("--folder", type=Path, required=True, help="Folder containing CSV files to normalize.")
    parser.add_argument("--dry-run", action="store_true", help="Show files that would be updated without writing changes.")
    args = parser.parse_args()

    folder = args.folder.resolve()
    if not folder.exists() or not folder.is_dir():
        raise SystemExit(f"Folder does not exist: {folder}")

    csv_files = sorted([p for p in folder.rglob("*.csv") if is_csv_file(p)])
    if not csv_files:
        print(f"No CSV files found in folder: {folder}")
        return 0

    print(f"Scanning {len(csv_files)} CSV file(s) under: {folder}")

    all_columns = collect_all_columns(folder)
    if not all_columns:
        print(f"No columns found in CSV files under: {folder}")
        return 0

    print(f"Unified column count: {len(all_columns)}")

    files_updated = 0
    for index, csv_file in enumerate(csv_files, start=1):
        print(f"[{index}/{len(csv_files)}] Processing: {csv_file}")
        rows = read_csv_rows(csv_file)
        if not rows:
            print("  -> empty file; skipping")
            continue

        header = detect_header_row(rows)
        missing = [col for col in all_columns if col not in header]
        if not missing:
            print("  -> already aligned; skipping")
            continue

        if args.dry_run:
            print(f"  -> WOULD UPDATE (missing: {missing})")
            files_updated += 1
            continue

        changed = ensure_missing_columns_in_file(csv_file, all_columns)
        if changed:
            files_updated += 1
            print(f"  -> UPDATED (missing: {missing})")
        else:
            print("  -> no change needed")

    print(f"Completed. Files updated: {files_updated}")
    print("Columns:")
    for col in all_columns:
        print(f" - {col}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
