"""Loss-aware XLSX-to-CSV conversion for Salesforce migration exports."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from openpyxl.worksheet.formula import ArrayFormula


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._")
    return cleaned or "sheet"


def formula_text(value: Any) -> str:
    if isinstance(value, ArrayFormula):
        value = value.text
    text = str(value or "")
    # Excel normalized phone numbers entered with a leading plus into formulas.
    if re.match(r"^=\s*\d", text):
        return "+" + text[1:]
    # Two multiline checklist answers were stored as invalid array formulas.
    return text[1:] if text.startswith("=") else text


def csv_value(cell: Any, repairs: dict[str, int]) -> str:
    value = cell.value
    if value is None:
        return ""
    if cell.data_type == "f" or isinstance(value, ArrayFormula):
        repairs["formula_cells_restored_as_text"] += 1
        return formula_text(value)
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    return str(value)


def nonempty_worksheets(workbook: Any) -> list[Any]:
    result = []
    for sheet in workbook.worksheets:
        if any(cell.value is not None for row in sheet.iter_rows() for cell in row):
            result.append(sheet)
    return result


def verify_csv(path: Path, expected: list[list[str]], columns: int) -> None:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        actual = list(csv.reader(handle))
    if actual != expected:
        raise RuntimeError(f"Round-trip cell validation failed for {path}")
    if any(len(row) != columns for row in actual):
        raise RuntimeError(f"Column-count validation failed for {path}")


def convert(source_dir: Path, output_dir: Path) -> dict[str, Any]:
    sources = sorted(source_dir.glob("*.xlsx"))
    if not sources:
        raise FileNotFoundError(f"No XLSX files found in {source_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "source_directory": str(source_dir.resolve()),
        "output_directory": str(output_dir.resolve()),
        "encoding": "UTF-8 with BOM",
        "newline": "CRLF via Python csv writer",
        "date_policy": "ISO-8601",
        "boolean_policy": "TRUE/FALSE",
        "files": [],
    }

    for source in sources:
        workbook = load_workbook(source, read_only=True, data_only=False)
        sheets = nonempty_worksheets(workbook)
        multiple = len(sheets) > 1
        source_entry: dict[str, Any] = {
            "source": source.name,
            "source_sha256": sha256(source),
            "outputs": [],
        }
        for sheet in sheets:
            filename = (
                f"{source.stem}__{safe_name(sheet.title)}.csv"
                if multiple
                else f"{source.stem}.csv"
            )
            destination = output_dir / filename
            repairs = {"formula_cells_restored_as_text": 0}
            expected: list[list[str]] = []
            with destination.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle, lineterminator="\r\n")
                for cells in sheet.iter_rows(
                    min_row=1,
                    max_row=sheet.max_row,
                    min_col=1,
                    max_col=sheet.max_column,
                ):
                    row = [csv_value(cell, repairs) for cell in cells]
                    expected.append(row)
                    writer.writerow(row)
            verify_csv(destination, expected, sheet.max_column)
            source_entry["outputs"].append(
                {
                    "sheet": sheet.title,
                    "csv": filename,
                    "rows_including_header": sheet.max_row,
                    "data_rows": max(sheet.max_row - 1, 0),
                    "columns": sheet.max_column,
                    "csv_sha256": sha256(destination),
                    **repairs,
                }
            )
        workbook.close()
        manifest["files"].append(source_entry)

    manifest_path = output_dir / "conversion_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    manifest = convert(args.source_dir, args.output_dir)
    output_count = sum(len(item["outputs"]) for item in manifest["files"])
    repair_count = sum(
        output["formula_cells_restored_as_text"]
        for item in manifest["files"]
        for output in item["outputs"]
    )
    print(
        f"Converted {len(manifest['files'])} workbooks into {output_count} CSV files; "
        f"restored {repair_count} formula-corrupted text cells."
    )


if __name__ == "__main__":
    main()
