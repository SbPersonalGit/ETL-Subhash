#!/usr/bin/env python3
"""Count CSV records (rows excluding header) for given files.

Usage:
    python scripts/count_csv_records.py path1.csv path2.csv ...
"""
from __future__ import annotations
import csv
import sys
from pathlib import Path

def count_records(path: Path) -> int:
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        try:
            reader = csv.reader(fh)
            rows = list(reader)
        except csv.Error:
            # fallback: count non-empty lines
            fh.seek(0)
            lines = [l for l in fh if l.strip()]
            return max(0, len(lines) - 1)
    if not rows:
        return 0
    # assume first row is header
    return max(0, len(rows) - 1)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Provide one or more CSV file paths.")
        return 2
    total = 0
    results = []
    for p in argv[1:]:
        path = Path(p)
        try:
            c = count_records(path)
        except Exception as e:
            print(f"ERROR: {path}: {e}")
            c = None
        results.append((path, c))
        if c is not None:
            total += c
    for path, c in results:
        if c is None:
            print(f"{path}: ERROR")
        else:
            print(f"{path}: {c} record(s)")
    print(f"TOTAL: {total} record(s)")
    return 0

if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
