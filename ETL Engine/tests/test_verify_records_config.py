import unittest
import json
import tempfile
from pathlib import Path

from verify_records import (
    build_object_config_map,
    compare_rows,
    excel_safe_value,
    load_config,
    resolve_data_seeding_source_dir,
    values_equal,
)


class VerifyRecordsConfigTests(unittest.TestCase):
    def test_excel_safe_value_prevents_formula_interpretation(self):
        self.assertEqual(excel_safe_value("=dangerous text"), "'=dangerous text")
        self.assertEqual(excel_safe_value("+123"), "'+123")
        self.assertEqual(excel_safe_value("ordinary text"), "ordinary text")

    def test_load_migration_yaml_config(self):
        config_path = Path("config/migration_example.yaml")
        objects = build_object_config_map(load_config(config_path))

        self.assertIn("Account", objects)
        self.assertEqual(objects["Account"]["external_id"], "External_Id__c")

    def test_load_data_seeding_json_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "template.json"
            config_path.write_text(json.dumps({"files": [
                {"object": "Account", "external_id": "External_Id__c", "mappings": [
                    {"source": "Name", "target": "Name", "ignored": False},
                ]},
            ]}), encoding="utf-8")
            objects = build_object_config_map(load_config(config_path))

        self.assertEqual(len(objects), 1)
        self.assertEqual(objects["Account"]["external_id"], "External_Id__c")
        self.assertIn("Name", objects["Account"]["fields"])
        self.assertEqual(objects["Account"]["_config_source"], "data_seeding_json")

    def test_data_seeding_json_defaults_external_id(self):
        objects = build_object_config_map({
            "files": [{"object": "Contact", "mappings": []}],
        })

        self.assertEqual(objects["Contact"]["external_id"], "Prod_Record_Id__c")

    def test_values_equal_normalizes_salesforce_representations(self):
        self.assertTrue(values_equal("1250", "1250.0"))
        self.assertTrue(values_equal("FALSE", "false"))
        self.assertTrue(values_equal("true", True))
        self.assertTrue(values_equal("00ed300000223Gn", "00ed300000223GnAAI"))
        self.assertTrue(values_equal(
            "2026-08-15T08:30:00.000Z",
            "2026-08-15T08:30:00.000+0000",
        ))
        self.assertTrue(values_equal("line one\r\nline two", "line one\nline two"))
        self.assertTrue(values_equal("10000000", "1.0E7"))
        self.assertTrue(values_equal(
            "North America;Europe & North Sea",
            "Europe & North Sea;North America",
            "multipicklist",
        ))
        self.assertTrue(values_equal(
            "Purchasing;Quality;Engineer;Operations",
            "Engineer;Operations;Purchasing;Quality",
            "multiselectpicklist",
        ))
        self.assertFalse(values_equal(
            "North America;Europe & North Sea",
            "Europe & North Sea;North America",
        ))
        self.assertFalse(values_equal("One;One;Two", "One;Two", "multipicklist"))
        self.assertFalse(values_equal("1250", "1251"))
        self.assertFalse(values_equal("alpha", "beta"))

    def test_compare_rows_uses_multiselect_field_metadata(self):
        matched, missing, mismatched, details = compare_rows(
            object_name="Campaign",
            expected_rows=[{
                "Id": "701000000000001",
                "Target_Regions__c": "North America;Europe & North Sea",
                "Description": "first;second",
            }],
            actual_rows={
                "701000000000001": {
                    "Target_Regions__c": "Europe & North Sea;North America",
                    "Description": "second;first",
                },
            },
            key_field="Id",
            compare_fields=["Target_Regions__c", "Description"],
            field_types={"Target_Regions__c": "multipicklist", "Description": "textarea"},
        )

        self.assertEqual((matched, missing, mismatched), (0, 0, 1))
        self.assertEqual([item["field"] for item in details], ["Description"])

    def test_data_seeding_source_dir_is_inferred_from_batch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config" / "flat_file_templates" / "fixture.json"
            config_path.parent.mkdir(parents=True)
            source_dir = root / "state" / "flat_file_uploads" / "batch-1"
            source_dir.mkdir(parents=True)
            (source_dir / "Account_clean.csv").write_text("Id\n001\n", encoding="utf-8")
            config_path.write_text(json.dumps({"batch_id": "batch-1", "files": []}), encoding="utf-8")
            config = load_config(config_path)
            resolved = resolve_data_seeding_source_dir(config, config_path, None)

            self.assertEqual(resolved.resolve(), source_dir.resolve())
            self.assertTrue((resolved / "Account_clean.csv").is_file())


if __name__ == "__main__":
    unittest.main()
