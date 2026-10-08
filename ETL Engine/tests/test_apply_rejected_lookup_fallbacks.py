from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.apply_rejected_lookup_fallbacks import apply_fallbacks, parse_versioned_stem


def write_csv(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)


def write_fallbacks(path: Path, rows: list[tuple[str, str]]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Object", "External_Id__c"])
    for object_name, fallback_id in rows:
        sheet.append([object_name, fallback_id])
    workbook.save(path)


class ApplyRejectedLookupFallbacksTest(unittest.TestCase):
    def test_parse_rejected_stem(self) -> None:
        object_name, version, suffix = parse_versioned_stem(
            Path("Promotion__c.v1_rejected.csv")
        )
        self.assertEqual(object_name, "Promotion__c")
        self.assertEqual(version, 1)
        self.assertEqual(suffix, "_rejected")

    def test_writes_v2_and_audit_without_changing_v1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            drop = root / "Promotion__c"
            source = drop / "Promotion__c.v1.csv"
            rejected = root / "prevalidation" / "Promotion__c.v1_rejected.csv"
            fallbacks = root / "KLG Fallback Ids.xlsx"
            write_csv(
                source,
                ["ID", "OWNERID", "CREATEDBYID", "LASTMODIFIEDBYID", "APTS_PROMOTION_CODE__C"],
                [
                    ["aAj0001", "005MISSING1", "005KEEP", "005MISSING1", "A027"],
                    ["aAj0002", "005KEEP", "005KEEP", "005KEEP", "A028"],
                ],
            )
            original = source.read_bytes()
            write_csv(
                rejected,
                ["External_Id__c", "OwnerId", "__error_OwnerId", "CreatedById", "__error_CreatedById"],
                [
                    [
                        "aAj0001",
                        "005MISSING1",
                        "reference '005MISSING1' not found in User.External_Id__c",
                        "005KEEP",
                        "",
                    ],
                    ["aAj0002", "005KEEP", "", "005KEEP", ""],
                ],
            )
            write_fallbacks(fallbacks, [("User", "005FALLBACKUSER")])

            result = apply_fallbacks(
                rejected_path=rejected,
                fallbacks_path=fallbacks,
                source_path=source,
            )

            self.assertEqual(source.read_bytes(), original)
            updated = Path(result["updated"])
            self.assertEqual(updated.name, "Promotion__c.v2.csv")
            self.assertTrue(updated.exists())
            with updated.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["OWNERID"], "005FALLBACKUSER")
            self.assertEqual(rows[0]["CREATEDBYID"], "005KEEP")
            self.assertEqual(rows[0]["LASTMODIFIEDBYID"], "005FALLBACKUSER")
            self.assertEqual(rows[1]["OWNERID"], "005KEEP")

            audit = Path(result["audit"])
            self.assertEqual(audit.name, "Promotion__c.V1.fallback.Updated.csv")
            with audit.open(encoding="utf-8-sig", newline="") as handle:
                audit_rows = list(csv.DictReader(handle))
            self.assertEqual(len(audit_rows), 2)
            self.assertEqual(audit_rows[0]["OriginalValue"], "005MISSING1")
            self.assertEqual(audit_rows[0]["UpdatedValue"], "005FALLBACKUSER")
            self.assertEqual(result["cells_replaced"], 2)


if __name__ == "__main__":
    unittest.main()
