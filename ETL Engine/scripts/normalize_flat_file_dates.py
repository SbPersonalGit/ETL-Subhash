"""Normalize Salesforce Date fields in a flat-file package to YYYY-MM-DD."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
from pathlib import Path

from core.connector import connector_from_env


ACCEPTED_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%y",
    "%m/%d/%Y",
    "%d/%m/%y",
    "%d/%m/%Y",
)


def normalize_date(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return text
    for pattern in ACCEPTED_FORMATS:
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Unsupported date value: {text!r}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    parser.add_argument("--profile", default="default")
    args = parser.parse_args()

    folder = args.folder.resolve()
    if not folder.is_dir():
        raise FileNotFoundError(folder)
    connector = connector_from_env("TARGET", args.profile).connect()
    total_changed = 0

    for path in sorted(folder.glob("*.csv")):
        object_name = path.stem
        fields = connector.describe_object(object_name).get("fields", [])
        date_fields = {
            str(field["name"])
            for field in fields
            if field.get("name") and field.get("type") == "date"
        }
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            headers = list(reader.fieldnames or [])
            rows = list(reader)
        applicable = date_fields.intersection(headers)
        changed = 0
        for row_number, row in enumerate(rows, start=2):
            for field_name in applicable:
                original = str(row.get(field_name) or "")
                try:
                    normalized = normalize_date(original)
                except ValueError as exc:
                    raise ValueError(
                        f"{path.name} row {row_number}, {field_name}: {exc}"
                    ) from exc
                if normalized != original:
                    row[field_name] = normalized
                    changed += 1
        if changed:
            temporary = path.with_suffix(path.suffix + ".date_tmp")
            with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=headers)
                writer.writeheader()
                writer.writerows(rows)
            temporary.replace(path)
        total_changed += changed
        print(f"{path.name}: normalized {changed} date value(s)")

    print(f"Total normalized date values: {total_changed}")


if __name__ == "__main__":
    main()
