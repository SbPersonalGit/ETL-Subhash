"""Report non-empty CSV column counts file-by-file and across all input files.

The implementation streams rows and retains only counters, so it is suitable for
large migration extracts. A value containing only whitespace is considered empty.
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


@dataclass
class FileColumnStats:
    path: Path
    total_rows: int
    counts: dict[str, int]


def analyze_csv(path: Path) -> FileColumnStats:
    counts: dict[str, int] = {}
    total_rows = 0
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        if not headers:
            raise ValueError(f"{path}: CSV header is empty")
        if any(not str(header or "").strip() for header in headers):
            raise ValueError(f"{path}: CSV header contains a blank column name")
        if len(headers) != len(set(headers)):
            raise ValueError(f"{path}: duplicate CSV column names are not supported")
        counts = {header: 0 for header in headers}
        for row in reader:
            total_rows += 1
            for header in headers:
                if str(row.get(header) or "").strip():
                    counts[header] += 1
    return FileColumnStats(path=path, total_rows=total_rows, counts=counts)


def write_file_report(
    stats: list[FileColumnStats], output: Path, minimum: int
) -> None:
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "file",
                "column",
                "total_rows",
                "non_empty_count",
                "non_empty_percent",
                "meets_minimum",
            ]
        )
        for item in stats:
            for column, count in item.counts.items():
                percent = (count * 100 / item.total_rows) if item.total_rows else 0
                writer.writerow(
                    [
                        str(item.path),
                        column,
                        item.total_rows,
                        count,
                        f"{percent:.4f}",
                        "yes" if count >= minimum else "no",
                    ]
                )


def write_consolidated_report(
    stats: list[FileColumnStats], output: Path, minimum: int
) -> None:
    all_columns = list(dict.fromkeys(column for item in stats for column in item.counts))
    total_non_empty: dict[str, int] = defaultdict(int)
    files_present: dict[str, int] = defaultdict(int)
    files_meeting_minimum: dict[str, int] = defaultdict(int)
    total_rows = sum(item.total_rows for item in stats)
    for item in stats:
        for column, count in item.counts.items():
            files_present[column] += 1
            total_non_empty[column] += count
            if count >= minimum:
                files_meeting_minimum[column] += 1

    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "column",
                "files_present",
                "total_files",
                "total_rows",
                "non_empty_count",
                "non_empty_percent",
                "files_meeting_minimum",
                "meets_consolidated_minimum",
            ]
        )
        for column in all_columns:
            count = total_non_empty[column]
            percent = (count * 100 / total_rows) if total_rows else 0
            writer.writerow(
                [
                    column,
                    files_present[column],
                    len(stats),
                    total_rows,
                    count,
                    f"{percent:.4f}",
                    files_meeting_minimum[column],
                    "yes" if count >= minimum else "no",
                ]
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path, help="One or more CSV files")
    parser.add_argument(
        "--min-non-empty",
        type=int,
        default=1,
        help="Minimum non-empty records required for a column (default: 1)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("column_population_report"),
        help="Report directory (default: ./column_population_report)",
    )
    args = parser.parse_args()
    if args.min_non_empty < 1:
        parser.error("--min-non-empty must be at least 1")
    missing = [str(path) for path in args.files if not path.is_file()]
    if missing:
        parser.error("CSV file(s) not found: " + ", ".join(missing))
    non_csv = [str(path) for path in args.files if path.suffix.lower() != ".csv"]
    if non_csv:
        parser.error("Only .csv files are supported: " + ", ".join(non_csv))
    return args


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stats: list[FileColumnStats] = []
    for index, path in enumerate(args.files, start=1):
        print(f"[{index}/{len(args.files)}] Reading {path}...")
        item = analyze_csv(path.resolve())
        stats.append(item)
        qualified = sum(count >= args.min_non_empty for count in item.counts.values())
        print(
            f"  rows={item.total_rows:,} columns={len(item.counts):,} "
            f"columns_meeting_minimum={qualified:,}"
        )

    file_report = args.output_dir / "column_population_by_file.csv"
    consolidated_report = args.output_dir / "column_population_consolidated.csv"
    write_file_report(stats, file_report, args.min_non_empty)
    write_consolidated_report(stats, consolidated_report, args.min_non_empty)
    print(f"File-wise report: {file_report.resolve()}")
    print(f"Consolidated report: {consolidated_report.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
