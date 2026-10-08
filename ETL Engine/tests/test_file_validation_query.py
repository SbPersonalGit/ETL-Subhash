"""Unit tests for scalable file-based validation helpers."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from core.file_validation_query import (
    MAX_BYTES_PER_CSV,
    apply_include_exclude,
    detailed_outputs_allowed,
    discover_file_validation_csvs,
    discover_lookup_mapping_candidates,
    enforce_source_size_limits,
    file_validation_progress_percent,
    intersect_compare_fields,
    parse_custom_soql_select,
    parse_field_list,
    read_file_validation_progress,
    summarize_mismatches_by_field,
    validate_file_validation_output_paths,
    write_file_validation_progress,
    write_file_wise_target_csvs,
    write_partitioned_mismatch_csvs,
)
from verify_records import compare_rows


class DiscoverCsvTests(unittest.TestCase):
    def test_single_file(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "a.csv"
            csv_path.write_text("Id,Name\n1,A\n", encoding="utf-8")
            found = discover_file_validation_csvs(csv_path)
            self.assertEqual(found, [csv_path.resolve()])

    def test_folder_and_one_subfolder_level(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "root.csv").write_text("Id,Name\n1,A\n", encoding="utf-8")
            nested = root / "batch1"
            nested.mkdir()
            (nested / "nested.csv").write_text("Id,Name\n2,B\n", encoding="utf-8")
            deeper = nested / "too_deep"
            deeper.mkdir()
            (deeper / "ignored.csv").write_text("Id,Name\n3,C\n", encoding="utf-8")
            found = discover_file_validation_csvs(root)
            names = [path.name for path in found]
            self.assertEqual(names, ["nested.csv", "root.csv"])


class SizeLimitTests(unittest.TestCase):
    def test_rejects_oversized_file(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.csv"
            path.write_bytes(b"x" * 100)
            original = MAX_BYTES_PER_CSV
            try:
                import core.file_validation_query as mod
                mod.MAX_BYTES_PER_CSV = 50
                with self.assertRaisesRegex(ValueError, "per-file limit"):
                    enforce_source_size_limits([path])
            finally:
                mod.MAX_BYTES_PER_CSV = original


class FieldSummaryTests(unittest.TestCase):
    def test_ranks_systematic_fields(self):
        differences = [
            {"field": "Status__c", "issue": "value_mismatch"},
            {"field": "Status__c", "issue": "value_mismatch"},
            {"field": "Phone", "issue": "value_mismatch"},
            {"field": "__row__", "issue": "missing_in_target"},
        ]
        summary = summarize_mismatches_by_field(differences, expected_rows=2)
        self.assertEqual(summary[0]["field"], "Status__c")
        self.assertEqual(summary[0]["hint"], "likely_systematic")
        self.assertTrue(any(row["field"].startswith("__row__:") for row in summary))


class OutputGateTests(unittest.TestCase):
    def test_gates_large_outputs(self):
        flags = detailed_outputs_allowed(mismatch_rows=200_000, target_rows=10_000)
        self.assertFalse(flags["write_mismatch_csv"])
        self.assertFalse(flags["write_excel"])
        flags = detailed_outputs_allowed(mismatch_rows=10, target_rows=10)
        self.assertTrue(flags["write_mismatch_csv"])
        self.assertTrue(flags["write_excel"])


class FileWiseTargetTests(unittest.TestCase):
    def test_writes_per_source_csv(self):
        with TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            written = write_file_wise_target_csvs(
                out_dir,
                object_name="Account",
                target_key="External_Id__c",
                retrieval_fields=["Name"],
                target_rows={
                    "A1": {"Id": "001A", "External_Id__c": "A1", "Name": "Alpha"},
                    "B1": {"Id": "001B", "External_Id__c": "B1", "Name": "Beta"},
                },
                ids_by_source_file={
                    "batch1.csv": ["A1"],
                    "batch2.csv": ["B1"],
                },
                stamp="20260101",
            )
            self.assertEqual(len(written), 2)
            text = written[0].read_text(encoding="utf-8")
            self.assertIn("External_Id__c", text)


class PartitionedMismatchTests(unittest.TestCase):
    def test_partitions_first_by_source_then_field(self):
        with TemporaryDirectory() as tmp:
            written, index = write_partitioned_mismatch_csvs(
                Path(tmp),
                object_name="Case",
                stamp="20260101",
                max_open_files=1,
                differences=[
                    {"object_name": "Case", "source_file": "one.csv", "external_id": "1", "field": "Status", "issue": "value_mismatch", "expected": "A", "actual": "B"},
                    {"object_name": "Case", "source_file": "two.csv", "external_id": "2", "field": "Status", "issue": "value_mismatch", "expected": "A", "actual": "C"},
                    {"object_name": "Case", "source_file": "one.csv", "external_id": "3", "field": "Priority", "issue": "value_mismatch", "expected": "", "actual": "High"},
                ],
            )
            self.assertEqual(len(written), 3)
            self.assertTrue(index.is_file())
            index_text = index.read_text(encoding="utf-8")
            self.assertIn("one.csv,Priority,1", index_text)
            self.assertIn("two.csv,Status,1", index_text)
            for path in written:
                self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 2)

    def test_shortens_long_partition_filename_to_windows_path_budget(self):
        with TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            max_path = len(str(out_dir.resolve())) + 145
            written, index = write_partitioned_mismatch_csvs(
                out_dir,
                object_name="Campaign",
                stamp="20260914_191653",
                max_path=max_path,
                differences=[{
                    "object_name": "Campaign",
                    "source_file": "001_R-54716_Campaign_3Aug.csv",
                    "external_id": "701S900001Ws5UgIAJ",
                    "field": "Commercial_owner_Telesales_script_ge_og__r.External_Id__c",
                    "issue": "value_mismatch",
                    "expected": "A",
                    "actual": "B",
                }],
            )
            self.assertEqual(len(written), 1)
            self.assertLessEqual(len(str(written[0].resolve())), max_path)
            self.assertRegex(written[0].name, r"__[0-9a-f]{10}\.csv$")
            self.assertIn("Commercial_owner_Telesales_script_ge_og__r.External_Id__c", index.read_text(encoding="utf-8"))

    def test_partition_rows_fit_combined_report_schema(self):
        combined_headers = {
            "object_name", "source_file", "external_id", "field", "issue", "expected", "actual"
        }
        partition_row = {
            "object_name": "Case", "source_file": "one.csv", "external_id": "1",
            "field": "Status", "issue": "value_mismatch", "expected": "A", "actual": "B",
        }
        self.assertEqual(set(partition_row), combined_headers)


class OutputPathPreflightTests(unittest.TestCase):
    def test_rejects_generated_path_over_windows_limit_before_writing(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "too long for Windows.*No validation processing"):
                validate_file_validation_output_paths(
                    Path(tmp) / ("run_" + "x" * 80),
                    object_name="PRM_Channel_Appointment_Parent_Approval__c",
                    source_names=["001_R-54681_Channel_Appointment_Parent_Approver_31July.csv"],
                    field_names=["Status_Date__c"],
                    max_path=180,
                )

    def test_accepts_generated_paths_within_limit(self):
        with TemporaryDirectory() as tmp:
            validate_file_validation_output_paths(
                Path(tmp),
                object_name="Case",
                source_names=["short.csv"],
                field_names=["Status"],
                max_path=500,
            )


class IgnoreCompareTests(unittest.TestCase):
    def test_parse_ignore_list(self):
        self.assertEqual(parse_field_list("CreatedDate, Name ,CreatedDate"), ["CreatedDate", "Name"])

    def test_row_compare_respects_reduced_fields(self):
        matched, missing, mismatched, details = compare_rows(
            "Account",
            [{"External_Id__c": "A1", "Name": "Alpha", "CreatedDate": "2020-01-01"}],
            {"A1": {"External_Id__c": "A1", "Name": "Alpha", "CreatedDate": "2024-01-01"}},
            "External_Id__c",
            ["Name", "CreatedDate"],
            row_compare_fields={"A1": ["Name"]},
        )
        self.assertEqual(matched, 1)
        self.assertEqual(mismatched, 0)
        self.assertEqual(details, [])


class ParseCustomSoqlTests(unittest.TestCase):
    def test_extracts_select_fields(self):
        object_name, fields = parse_custom_soql_select(
            "SELECT Id, Name, Parent.External_Id__c\nFROM Account WHERE Name != null ORDER BY Name LIMIT 10"
        )
        self.assertEqual(object_name, "Account")
        self.assertEqual(fields, ["Id", "Name", "Parent.External_Id__c"])

    def test_rejects_subquery_and_star(self):
        with self.assertRaisesRegex(ValueError, "subqueries"):
            parse_custom_soql_select("SELECT Id, (SELECT Id FROM Contacts) FROM Account")
        with self.assertRaisesRegex(ValueError, "explicit fields"):
            parse_custom_soql_select("SELECT * FROM Account")

    def test_rejects_invalid_shape(self):
        with self.assertRaisesRegex(ValueError, "SELECT ... FROM"):
            parse_custom_soql_select("FROM Account")


class IncludeExcludeTests(unittest.TestCase):
    def test_empty_include_keeps_all_then_excludes(self):
        selected, skipped = apply_include_exclude(
            ["Name", "Phone", "CreatedDate"],
            include_fields=[],
            exclude_fields=["CreatedDate"],
        )
        self.assertEqual(selected, ["Name", "Phone"])
        self.assertEqual(skipped, [])

    def test_include_filters_and_reports_missing(self):
        selected, skipped = apply_include_exclude(
            ["Name", "Phone", "BillingCity"],
            include_fields=["Name", "Missing__c", "Phone"],
            exclude_fields=["Phone"],
            required_fields=["External_Id__c"],
        )
        self.assertEqual(selected, ["Name", "External_Id__c"])
        self.assertEqual(skipped, ["Missing__c"])


class IntersectCompareTests(unittest.TestCase):
    def test_intersection_skips_csv_or_target_only(self):
        compare, csv_only, target_only = intersect_compare_fields(
            csv_fields=["Id", "Name", "Phone", "Extra__c"],
            target_fields=["Id", "External_Id__c", "Name", "Status__c"],
            match_key="External_Id__c",
            source_key="Id",
        )
        self.assertEqual(compare, ["Name"])
        self.assertEqual(csv_only, ["Phone", "Extra__c"])
        self.assertEqual(target_only, ["Status__c"])


class LookupDiscoveryTests(unittest.TestCase):
    def test_discovers_single_parent_and_recommends_legacy(self):
        fields = [
            {"name": "Name", "type": "string"},
            {
                "name": "ParentId",
                "type": "reference",
                "referenceTo": ["Account"],
                "relationshipName": "Parent",
            },
            {
                "name": "WhoId",
                "type": "reference",
                "referenceTo": ["Contact", "Lead"],
                "relationshipName": "Who",
            },
        ]
        mappings, skipped = discover_lookup_mapping_candidates(
            ["Id", "Name", "ParentId", "WhoId"],
            fields,
            {"Account": ["Ext__c", "External_Id__c"]},
        )
        self.assertEqual(len(mappings), 1)
        self.assertEqual(mappings[0]["source_field"], "ParentId")
        self.assertEqual(mappings[0]["recommended_external_id"], "External_Id__c")
        self.assertEqual(mappings[0]["relationship_name"], "Parent")
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["source_field"], "WhoId")

    def test_include_fields_filters_lookup_candidates(self):
        fields = [
            {
                "name": "ParentId",
                "type": "reference",
                "referenceTo": ["Account"],
                "relationshipName": "Parent",
            },
            {
                "name": "OwnerId",
                "type": "reference",
                "referenceTo": ["User"],
                "relationshipName": "Owner",
            },
        ]
        mappings, _skipped = discover_lookup_mapping_candidates(
            ["ParentId", "OwnerId"],
            fields,
            {"Account": ["External_Id__c"], "User": ["External_Id__c"]},
            include_fields=["ParentId"],
        )
        self.assertEqual([row["source_field"] for row in mappings], ["ParentId"])


class LookupEnrichCompareTests(unittest.TestCase):
    def test_compare_uses_relationship_path_after_enrichment(self):
        expected = [{
            "External_Id__c": "A1",
            "ParentId": "SRC-PARENT",
            "Parent.External_Id__c": "SRC-PARENT",
        }]
        target = {
            "A1": {
                "External_Id__c": "A1",
                "Parent.External_Id__c": "SRC-PARENT",
            }
        }
        matched, missing, mismatched, details = compare_rows(
            "Account",
            expected,
            target,
            "External_Id__c",
            ["Parent.External_Id__c"],
            row_compare_fields={"A1": ["Parent.External_Id__c"]},
        )
        self.assertEqual(matched, 1)
        self.assertEqual(mismatched, 0)
        self.assertEqual(details, [])


class FileValidationProgressTests(unittest.TestCase):
    def test_percent_advances_within_stage(self):
        start = file_validation_progress_percent("load_source", stage_fraction=0.0)
        mid = file_validation_progress_percent("load_source", stage_fraction=0.5)
        end = file_validation_progress_percent("query_target", stage_fraction=0.0)
        self.assertGreater(mid, start)
        self.assertGreaterEqual(end, mid)
        self.assertEqual(file_validation_progress_percent("completed"), 100)

    def test_write_and_read_progress_heartbeat(self):
        with TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            write_file_validation_progress(
                out_dir,
                {
                    "phase": "query_target",
                    "object_name": "Account",
                    "expected_keys": 1200,
                    "stage_fraction": 0.25,
                    "detail": "Bulk query running",
                },
            )
            payload = read_file_validation_progress(out_dir)
            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertEqual(payload["phase"], "query_target")
            self.assertEqual(payload["status"], "RUNNING")
            self.assertGreaterEqual(int(payload["progress_percent"]), 48)
            self.assertLess(int(payload["progress_percent"]), 80)
            self.assertIn("Bulk query running", payload["message"])
            self.assertEqual(payload["progress_unit"], "stages")


if __name__ == "__main__":
    unittest.main()
