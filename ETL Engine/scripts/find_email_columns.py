#!/usr/bin/env python3
"""Find likely email columns in CSV files under a folder.

Rule used:
- A column is considered a candidate email column if it contains at least 10 email-like values.
- The first occurrence of each normalized column name is kept for review; later duplicate names are skipped.
- A review CSV is produced listing the file, column name, index, count, and sample email values.
"""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b")


def normalize_header(name: str, fallback: str | None = None) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower())
    cleaned = cleaned.strip("_")
    if cleaned:
        return cleaned
    return fallback or "column"


def extract_emails(value: str) -> list[str]:
    text = (value or "").strip()
    if not text:
        return []
    return [m.strip() for m in EMAIL_RE.findall(text)]


def iter_csv_files(root: Path) -> list[Path]:
    return sorted(
        [p for p in root.rglob("*.csv") if p.is_file()],
        key=lambda p: str(p).lower(),
    )


def scan_csv_for_email_columns(csv_path: Path, seen_headers: set[str]) -> list[dict[str, str | int]]:
    results: list[dict[str, str | int]] = []

    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
    except Exception:
        return results

    if not rows:
        return results

    header = rows[0]
    if not header:
        return results

    for idx, column_name in enumerate(header):
        count = 0
        unique_emails: set[str] = set()
        samples: list[str] = []

        for row in rows[1:]:
            if idx >= len(row):
                continue
            cell = row[idx]
            emails = extract_emails(cell)
            if not emails:
                continue
            count += 1
            for email in emails:
                unique_emails.add(email)
                if len(samples) < 10:
                    samples.append(email)

        if count < 10:
            continue

        normalized = normalize_header(column_name, fallback=f"column_{idx + 1}")
        if normalized in seen_headers:
            continue

        seen_headers.add(normalized)
        results.append(
            {
                "file": str(csv_path),
                "column_name": (column_name or f"Column {idx + 1}").strip(),
                "column_index": idx + 1,
                "email_count": count,
                "sample_values": "; ".join(samples[:10]),
                "unique_emails": len(unique_emails),
            }
        )

    return results


def write_review_csv(output_path: Path, rows: list[dict[str, str | int]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "file",
                "column_name",
                "column_index",
                "email_count",
                "unique_emails",
                "sample_values",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main() -> int:
    parser = argparse.ArgumentParser(description="Find likely email columns in CSV files under a folder.")
    parser.add_argument("folder", type=Path, help="Folder to scan for CSV files.")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional output review CSV path. Default: <folder>/email_columns_review.csv",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=10,
        help="Minimum number of email-like values in a column to consider it a likely email column.",
    )
    args = parser.parse_args()

    root = args.folder.expanduser().resolve()
    if not root.exists() or not root.is_dir():
        print(f"Folder not found: {root}", file=sys.stderr)
        return 2

    output_path = args.output.expanduser().resolve() if args.output else root / "email_columns_review.csv"
    seen_headers: set[str] = set()
    all_rows: list[dict[str, str | int]] = []

    for csv_path in iter_csv_files(root):
        for row in scan_csv_for_email_columns(csv_path, seen_headers):
            if int(row["email_count"]) >= args.threshold:
                all_rows.append(row)

    write_review_csv(output_path, all_rows)

    print(f"Scanned {len(iter_csv_files(root))} CSV file(s).")
    print(f"Found {len(all_rows)} likely email column(s).")
    print(f"Review file created: {output_path}")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main())
