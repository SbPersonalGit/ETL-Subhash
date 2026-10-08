"""Repair invalid Salesforce Checkbox literals in an existing generated CSV package."""

from __future__ import annotations

import argparse
import csv
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path


NS = {"m": "http://soap.sforce.com/2006/04/metadata"}
VALID_BOOLEAN_VALUES = {"", "true", "false", "0", "1"}


def metadata_type(field_path: Path) -> str:
    if not field_path.exists():
        return ""
    root = ET.parse(field_path).getroot()
    node = root.find("m:type", NS)
    return (node.text or "").strip() if node is not None else ""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata-root", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    args = parser.parse_args()

    metadata_root = args.metadata_root.resolve()
    package = args.package.resolve()
    backup = package / "_before_checkbox_repair"
    backup.mkdir(parents=True, exist_ok=True)

    repaired_files = 0
    repaired_fields = 0
    repaired_values = 0
    for csv_path in sorted(package.glob("*.csv")):
        if csv_path.name == "coverage.csv":
            continue
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = list(reader.fieldnames or [])
            rows = list(reader)
        if not fieldnames or not rows:
            continue

        checkbox_fields = [
            name
            for name in fieldnames
            if metadata_type(
                metadata_root / csv_path.stem / "fields" / f"{name}.field-meta.xml"
            )
            == "Checkbox"
        ]
        changed_fields = set()
        for row_index, row in enumerate(rows):
            for field_name in checkbox_fields:
                value = str(row.get(field_name) or "").strip()
                if value.lower() in VALID_BOOLEAN_VALUES:
                    continue
                row[field_name] = "true" if row_index % 2 == 0 else "false"
                changed_fields.add(field_name)
                repaired_values += 1

        if not changed_fields:
            continue
        shutil.copy2(csv_path, backup / csv_path.name)
        with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        repaired_files += 1
        repaired_fields += len(changed_fields)
        print(f"{csv_path.name}: repaired {len(changed_fields)} Checkbox field(s)")

    print(
        f"Complete: {repaired_values} value(s) across {repaired_fields} field(s) "
        f"in {repaired_files} file(s). Backup: {backup}"
    )


if __name__ == "__main__":
    main()
