from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from scripts.apply_case_reference_fallbacks import (
    ColumnSpec,
    ProcessStats,
    build_output_header,
    id_variants,
    load_column_rules,
    load_existing_records,
    load_existing_ids,
    load_prefix_map,
    process_case_file,
)


def write_csv(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)


class ApplyCaseReferenceFallbacksTest(unittest.TestCase):
    def test_id_variants_are_case_sensitive_on_15_char_prefix(self) -> None:
        # Real false-positive pair from Case_5 review:
        # source 003A0000017YWOrIAO vs existing 003A0000017YwOrIAK
        source = "003A0000017YWOrIAO"
        existing_legacy = "003A0000017YwOrIAK"
        existing_keys = id_variants(existing_legacy)
        self.assertFalse(any(key in existing_keys for key in id_variants(source)))
        # Same record via 15-char exact case should still match.
        self.assertTrue(
            any(key in id_variants(source) for key in id_variants(source[:15]))
        )

    def test_id_variants_match_15_and_18_same_case(self) -> None:
        short = "005S900000dr9Ba"
        long = short + "IAI"
        variants = id_variants(long)
        self.assertIn(short, variants)
        self.assertIn(long, variants)
        # casefolded 15-char must NOT be present
        self.assertNotIn(short.casefold(), variants)

    def test_rename_and_preserve_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = root / "existing"
            write_csv(existing / "User.csv", ["External_Id__c"], [["005SOURCEUSER0001"]])
            write_csv(existing / "Contact.csv", ["External_Id__c"], [["003SOURCECONTACT1"]])

            columns_map = root / "columns.csv"
            write_csv(
                columns_map,
                [
                    "Column",
                    "ReferencedObject",
                    "ApplyFallback",
                    "OutputColumn",
                    "PreserveOriginalAs",
                ],
                [
                    ["Id", "", "N", "Id", ""],
                    ["CaseNumber", "", "N", "Legacy_Case_Number__c", ""],
                    ["ContactId", "Contact", "Y", "ContactId", "Legacy_Contact_Id__c"],
                    ["OwnerId", "User", "Y", "OwnerId", ""],
                    ["AccountId", "Account", "N", "Legacy_Account_Id__c", ""],
                    ["ParentId", "Case", "N", "ParentId", ""],
                ],
            )
            fallback_map = root / "fallback.csv"
            write_csv(
                fallback_map,
                ["ReferencedObject", "FallbackId"],
                [
                    ["Contact", "003FALLBACKCONTACT"],
                    ["User", "005FALLBACKUSER01"],
                    ["Account", "001FALLBACKACCT01"],
                ],
            )

            case_dir = root / "cases"
            write_csv(
                case_dir / "Case.csv",
                ["Id", "CaseNumber", "ContactId", "OwnerId", "AccountId", "ParentId"],
                [
                    [
                        "5001",
                        "0001",
                        "003MISSINGCONTACT1",
                        "005SOURCEUSER0001",
                        "001SOURCEACCOUNT1",
                        "500PARENT0000001",
                    ],
                ],
            )

            specs = load_column_rules(columns_map, fallback_map)
            self.assertEqual(
                build_output_header(specs),
                [
                    "Id",
                    "Legacy_Case_Number__c",
                    "ContactId",
                    "Legacy_Contact_Id__c",
                    "OwnerId",
                    "Legacy_Account_Id__c",
                    "ParentId",
                ],
            )
            contact = next(s for s in specs if s.source_column == "ContactId")
            self.assertEqual(contact.preserve_original_as, "Legacy_Contact_Id__c")
            self.assertTrue(contact.has_fallback)
            account = next(s for s in specs if s.source_column == "AccountId")
            self.assertEqual(account.output_column, "Legacy_Account_Id__c")
            self.assertFalse(account.has_fallback)

            stats = ProcessStats()
            out = root / "out" / "Case.csv"
            replacements = process_case_file(
                source=case_dir / "Case.csv",
                output_path=out,
                specs=specs,
                existing_by_object=load_existing_ids(existing),
                stats=stats,
            )

            with out.open("r", encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))

            self.assertEqual(row["Legacy_Case_Number__c"], "0001")
            self.assertEqual(row["ContactId"], "003FALLBACKCONTACT")
            self.assertEqual(row["Legacy_Contact_Id__c"], "003MISSINGCONTACT1")
            self.assertEqual(row["OwnerId"], "005SOURCEUSER0001")
            self.assertEqual(row["Legacy_Account_Id__c"], "001SOURCEACCOUNT1")
            self.assertEqual(row["ParentId"], "500PARENT0000001")
            self.assertEqual(stats.replacements, 1)
            self.assertEqual(replacements[0].original_value, "003MISSINGCONTACT1")

    def test_no_fallback_columns_overrides_fallback_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            columns_map = root / "columns.csv"
            with columns_map.open("w", encoding="utf-8", newline="") as handle:
                handle.write("Id,AccountId,OwnerId,ParentId\n")
            fallback_map = root / "fallback.csv"
            write_csv(
                fallback_map,
                ["ReferencedObject", "FallbackId"],
                [
                    ["Account", "001FALLBACKACCT01"],
                    ["User", "005FALLBACKUSER01"],
                    ["Case", "500FALLBACKCASE01"],
                ],
            )
            specs = load_column_rules(
                columns_map,
                fallback_map,
                no_fallback_columns={"AccountId", "ParentId"},
            )
            by_col = {spec.source_column: spec for spec in specs}
            self.assertFalse(by_col["AccountId"].has_fallback)
            self.assertFalse(by_col["ParentId"].has_fallback)
            self.assertTrue(by_col["OwnerId"].has_fallback)

    def test_apply_fallback_flag_in_columns_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            columns_map = root / "columns.csv"
            write_csv(
                columns_map,
                ["Column", "ReferencedObject", "ApplyFallback"],
                [
                    ["AccountId", "Account", "N"],
                    ["OwnerId", "User", "Y"],
                    ["ParentId", "Case", "N"],
                ],
            )
            fallback_map = root / "fallback.csv"
            write_csv(
                fallback_map,
                ["ReferencedObject", "FallbackId"],
                [
                    ["Account", "001FALLBACKACCT01"],
                    ["User", "005FALLBACKUSER01"],
                    ["Case", "500FALLBACKCASE01"],
                ],
            )
            specs = load_column_rules(columns_map, fallback_map)
            by_col = {spec.source_column: spec for spec in specs}
            self.assertFalse(by_col["AccountId"].has_fallback)
            self.assertFalse(by_col["ParentId"].has_fallback)
            self.assertEqual(by_col["OwnerId"].fallback_id, "005FALLBACKUSER01")

    def test_full_output_mode_preserves_unconfigured_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Task.csv"
            write_csv(
                source,
                ["Id", "Subject", "OwnerId", "Description"],
                [["00T1", "Call", "005MISSINGUSER01", "Keep this text"]],
            )
            specs = [
                ColumnSpec(
                    source_column="OwnerId",
                    output_column="OwnerId",
                    referenced_object="User",
                    fallback_id="005FALLBACKUSER01",
                )
            ]
            output = root / "out" / "Task.csv"
            process_case_file(
                source=source,
                output_path=output,
                specs=specs,
                existing_by_object={"User": set()},
                stats=ProcessStats(),
                output_mode="full",
            )

            with output.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                row = next(reader)

            self.assertEqual(
                reader.fieldnames, ["Id", "Subject", "OwnerId", "Description"]
            )
            self.assertEqual(row["Subject"], "Call")
            self.assertEqual(row["Description"], "Keep this text")
            self.assertEqual(row["OwnerId"], "005FALLBACKUSER01")

    def test_polymorphic_prefix_remap_and_empty_object_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = root / "existing"
            write_csv(existing / "Lead.csv", ["Id", "External_Id__c"], [])
            write_csv(
                existing / "Contact.csv",
                ["Id", "External_Id__c"],
                [["003TARGETCONTACT1", "003SOURCECONTACT1"]],
            )
            existing_ids, legacy_to_target = load_existing_records(existing)
            self.assertIn("Lead", existing_ids)
            self.assertEqual(existing_ids["Lead"], set())

            prefix_path = root / "prefixes.csv"
            write_csv(
                prefix_path,
                ["Column", "SourcePrefix", "ReferencedObject"],
                [["WhoId", "003", "Contact"], ["WhoId", "00Q", "Lead"]],
            )
            source = root / "Task.csv"
            write_csv(
                source,
                ["Id", "WhoId"],
                [
                    ["00T1", "003SOURCECONTACT1"],
                    ["00T2", "00QSOURCELEAD001"],
                ],
            )
            output = root / "out" / "Task.csv"
            specs = [
                ColumnSpec(
                    source_column="WhoId",
                    output_column="WhoId",
                    referenced_object="Polymorphic",
                    preserve_original_as="Legacy_WhoId__c",
                    apply_fallback=True,
                )
            ]
            stats = ProcessStats()
            replacements = process_case_file(
                source=source,
                output_path=output,
                specs=specs,
                existing_by_object=existing_ids,
                stats=stats,
                legacy_to_target_by_object=legacy_to_target,
                remap_found_to_target_id=True,
                prefix_mappings=load_prefix_map(prefix_path),
                object_fallback={"Lead": "00QFALLBACKLEAD1"},
            )

            with output.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["WhoId"], "003TARGETCONTACT1")
            self.assertEqual(rows[0]["Legacy_WhoId__c"], "003SOURCECONTACT1")
            self.assertEqual(rows[1]["WhoId"], "00QFALLBACKLEAD1")
            self.assertEqual(rows[1]["Legacy_WhoId__c"], "00QSOURCELEAD001")
            self.assertEqual(replacements[0].referenced_object, "Lead")


if __name__ == "__main__":
    unittest.main()
