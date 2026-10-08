import csv
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import ui.app as app_module
from core.general_utility import (
    apply_fallbacks,
    create_custom_field,
    discover_populated_references,
    grant_field_permissions,
    legacy_field_name,
    write_prepared_csv,
)
from core.bulk_fls import assign_object_access, read_object_list



class FakeResponse:
    def __init__(self, payload): self.payload = payload
    def json(self): return self.payload


class FakeMetadataConnector:
    instance_url = "https://example.my.salesforce.com"
    api_version = "v61.0"
    def __init__(self):
        self._object_describe_cache = {"Account": {}}
        self.requests = []
        self.permissions_created = False
    def describe_object(self, object_name): return {"fields": []}
    def _request(self, method, url, json=None):
        self.requests.append((method, url, json))
        return FakeResponse({"id": "created-id"})
    def query(self, soql):
        if "FROM PermissionSet WHERE Name" in soql: return [{"Id": "0PS1", "Name": "Migration_User"}]
        if "FROM FieldPermissions" in soql: return []
        return []


class DuplicateFieldConnector(FakeMetadataConnector):
    def _request(self, method, url, json=None):
        raise RuntimeError(
            "DUPLICATE_DEVELOPER_NAME: There is already a field named "
            "Legacy_OwnerId_Match on Account."
        )


class GeneralUtilityCoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "Account.csv"
        with self.source.open("w", encoding="utf-8-sig", newline="") as handle:
            csv.writer(handle).writerows([
                ["Id", "OwnerId", "Name", "Region", "Blank_Custom__c"],
                ["a1", "u1", "One", "East", ""],
                ["a2", "u-missing", "Two", "", ""],
                ["a3", "", "Three", "West", ""],
            ])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_discovers_only_populated_reference_columns(self) -> None:
        result = discover_populated_references(self.source, {"fields": [
            {"name": "OwnerId", "type": "reference", "referenceTo": ["User"], "relationshipName": "Owner"},
            {"name": "Name", "type": "string", "autoNumber": True},
            {"name": "Region", "type": "string", "custom": True},
            {"name": "Blank_Custom__c", "type": "string", "custom": True},
        ]}, "Legacy_")
        self.assertEqual(result["row_count"], 3)
        self.assertEqual(result["references"], [{
            "column": "OwnerId", "populated_rows": 2,
            "referenced_objects": ["User"], "polymorphic": False,
            "relationship_name": "Owner",
            "match_expression": "Owner.External_Id__c",
            "legacy_field": "Legacy_OwnerId__c",
            "legacy_field_valid": True,
            "legacy_field_error": "",
        }])
        candidates = {item["column"]: item for item in result["preservation_candidates"]}
        self.assertEqual(set(candidates), {"OwnerId", "Name", "Region"})
        self.assertEqual(candidates["OwnerId"]["kind"], "reference")
        self.assertEqual(candidates["OwnerId"]["source_field_type"], "reference")
        self.assertEqual(candidates["OwnerId"]["referenced_objects"], ["User"])
        self.assertTrue(candidates["OwnerId"]["is_user_reference"])
        self.assertFalse(candidates["OwnerId"]["default_selected"])
        self.assertFalse(candidates["OwnerId"]["default_match"])
        self.assertEqual(candidates["Name"]["kind"], "auto_number")
        self.assertEqual(candidates["Name"]["source_field_type"], "string")
        self.assertEqual(candidates["Name"]["referenced_objects"], [])
        self.assertFalse(candidates["Name"]["default_selected"])
        self.assertFalse(candidates["Name"]["match_supported"])
        self.assertFalse(candidates["Name"]["default_match"])
        self.assertEqual(candidates["Region"]["kind"], "custom")
        self.assertFalse(candidates["Region"]["default_selected"])
        self.assertFalse(candidates["Region"]["default_match"])

    def test_non_user_references_default_selected(self) -> None:
        path = self.root / "Case.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            csv.writer(handle).writerows([
                ["Id", "OwnerId", "AccountId", "RecordTypeId", "ProfileId", "Name"],
                ["c1", "u1", "a1", "rt1", "p1", "One"],
                ["c2", "u2", "a2", "rt2", "p2", "Two"],
            ])
        result = discover_populated_references(path, {"fields": [
            {"name": "OwnerId", "type": "reference", "referenceTo": ["User", "Group"], "relationshipName": "Owner"},
            {"name": "AccountId", "type": "reference", "referenceTo": ["Account"], "relationshipName": "Account"},
            {"name": "RecordTypeId", "type": "reference", "referenceTo": ["RecordType"], "relationshipName": "RecordType"},
            {"name": "ProfileId", "type": "reference", "referenceTo": ["Profile"], "relationshipName": "Profile"},
            {"name": "Name", "type": "string", "autoNumber": True},
        ]}, "Legacy_")
        candidates = {item["column"]: item for item in result["preservation_candidates"]}
        self.assertFalse(candidates["OwnerId"]["default_selected"])
        self.assertFalse(candidates["OwnerId"]["default_match"])
        self.assertTrue(candidates["AccountId"]["default_selected"])
        self.assertTrue(candidates["AccountId"]["default_match"])
        self.assertFalse(candidates["RecordTypeId"]["default_selected"])
        self.assertFalse(candidates["RecordTypeId"]["default_match"])
        self.assertFalse(candidates["ProfileId"]["default_selected"])
        self.assertFalse(candidates["ProfileId"]["default_match"])
        self.assertFalse(candidates["Name"]["default_selected"])

        self.assertEqual(legacy_field_name("Contact__c", "Old_"), "Old_Contact__c")
        shortened = legacy_field_name("SFPRM_Additional_Info_Request_Compliance__c", "Legacy_")
        self.assertEqual(len(shortened), 40)
        with self.assertRaisesRegex(ValueError, "Prefix"):
            legacy_field_name("OwnerId", "Bad Prefix")

    def test_analysis_reports_overlength_generated_name_without_blocking(self) -> None:
        source = self.root / "long.csv"
        column = "Commercial_owner_Telesales_script_ge_og__c"
        with source.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows([["Id", column], ["1", "005old"]])
        result = discover_populated_references(source, {"fields": [
            {"name": column, "type": "reference", "referenceTo": ["User"]},
        ]}, "Legacy_")
        reference = result["references"][0]
        self.assertEqual(reference["legacy_field"], "Legacy_Commercial_owner_Telesales_script_ge_og__c")
        self.assertFalse(reference["legacy_field_valid"])
        self.assertIn("exceeds 40 characters", reference["legacy_field_error"])

    def test_prepares_preservation_and_json_columns_without_dropping_columns(self) -> None:
        output = self.root / "prepared.csv"
        result = write_prepared_csv(
            self.source, output,
            preservation=[{"source_field": "OwnerId", "output_field": "Legacy_OwnerId__c"}],
            concatenations=[{"source_fields": ["Name", "Region"], "output_field": "Legacy_Context__c"}],
        )
        with output.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(result["source_columns"], 5)
        self.assertEqual(len(rows), 3)
        self.assertIn("Blank_Custom__c", rows[0])
        self.assertEqual(rows[1]["Legacy_OwnerId__c"], "u-missing")
        self.assertEqual(json.loads(rows[0]["Legacy_Context__c"]), {"Name": "One", "Region": "East"})

    def test_fallback_preserves_valid_and_blank_values_and_audits_missing(self) -> None:
        output = self.root / "fallback.csv"
        result = apply_fallbacks(self.source, output, [{
            "column": "OwnerId", "referenced_object": "User",
            "fallback_id": "fallback", "preserve_as": "Legacy_OwnerId__c",
        }], {"User": {"u1", "fallback"}})
        with output.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([row["OwnerId"] for row in rows], ["u1", "fallback", ""])
        self.assertEqual([row["Legacy_OwnerId__c"] for row in rows], ["u1", "u-missing", ""])
        self.assertEqual(result["replacements"], 1)

    def test_rejects_unconfirmed_fallback_record(self) -> None:
        with self.assertRaisesRegex(ValueError, "was not found"):
            apply_fallbacks(self.source, self.root / "bad.csv", [{
                "column": "OwnerId", "referenced_object": "User", "fallback_id": "unknown"
            }], {"User": {"u1"}})

    def test_creates_long_text_field_and_field_permission(self) -> None:
        connector = FakeMetadataConnector()
        result = create_custom_field(connector, "Account", {
            "api_name": "Legacy_Context__c", "type": "LongTextArea",
            "length": 131072, "description": "Migration context",
        })
        permissions = grant_field_permissions(connector, "Account", "Legacy_Context__c", ["Migration_User"], [])
        self.assertTrue(result["created"])
        metadata = connector.requests[0][2]["Metadata"]
        self.assertEqual(metadata["type"], "LongTextArea")
        self.assertEqual(metadata["length"], 131072)
        self.assertEqual(permissions[0]["principal"], "PermissionSet:Migration_User")
        self.assertEqual(connector.requests[1][2]["Field"], "Account.Legacy_Context__c")

    def test_creates_match_formula_as_checkbox(self) -> None:
        connector = FakeMetadataConnector()
        result = create_custom_field(connector, "Account", {
            "api_name": "Legacy_OwnerId_Match__c",
            "type": "FormulaCheckbox",
            "formula": "Legacy_OwnerId__c = OwnerId",
        })
        self.assertTrue(result["created"])
        metadata = connector.requests[0][2]["Metadata"]
        self.assertEqual(metadata["type"], "Checkbox")
        self.assertEqual(metadata["formula"], "Legacy_OwnerId__c = OwnerId")
        self.assertNotIn("length", metadata)

    def test_rejects_internal_double_underscore_before_salesforce_request(self) -> None:
        connector = FakeMetadataConnector()
        with self.assertRaisesRegex(ValueError, "Only the final '__c'"):
            create_custom_field(connector, "Account", {
                "api_name": "Legacy_SFPRM_Country_Commercia__Match__c",
                "type": "FormulaCheckbox",
                "formula": "Legacy_Country__c = Country__c",
            })
        self.assertEqual(connector.requests, [])

    def test_duplicate_field_response_is_idempotent_during_describe_delay(self) -> None:
        connector = DuplicateFieldConnector()
        result = create_custom_field(connector, "Account", {
            "api_name": "Legacy_OwnerId_Match__c",
            "type": "FormulaCheckbox",
            "formula": "Legacy_OwnerId__c = OwnerId",
        })
        self.assertFalse(result["created"])
        self.assertEqual(result["reason"], "already_exists_salesforce_confirmed")

    def test_formula_field_permission_is_read_only(self) -> None:
        connector = FakeMetadataConnector()
        permissions = grant_field_permissions(
            connector,
            "Account",
            "Legacy_OwnerId_Match__c",
            ["Migration_User"],
            [],
            editable=False,
        )
        payload = connector.requests[0][2]
        self.assertTrue(payload["PermissionsRead"])
        self.assertFalse(payload["PermissionsEdit"])
        self.assertFalse(permissions[0]["editable"])


class GeneralUtilityApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.drop = self.root / "drop"
        self.drop.mkdir()
        with (self.drop / "sample.csv").open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows([["Id", "OwnerId"], ["1", "005old"]])
        self.client = app_module.app.test_client()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_page_and_analysis_api(self) -> None:
        connector = type("Connector", (), {"describe_object": lambda self, name: {"fields": [{"name": "OwnerId", "type": "reference", "referenceTo": ["User"]}]}})()
        with patch.object(app_module, "FLAT_FILE_DROP_DIR", self.drop), patch.object(app_module, "get_connector", return_value=connector):
            page = self.client.get("/general-utility")
            self.assertEqual(page.status_code, 200)
            self.assertIn(b"general_utility.js?v=19", page.data)
            response = self.client.post("/api/general-utility/analyze", json={"file": "sample.csv", "object": "Account", "prefix": "Legacy_"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["references"][0]["legacy_field"], "Legacy_OwnerId__c")

    def test_column_preparation_download(self) -> None:
        output_root = self.root / "outputs"
        with patch.object(app_module, "FLAT_FILE_DROP_DIR", self.drop), patch.object(app_module, "GENERAL_UTILITY_OUTPUT_DIR", output_root):
            response = self.client.post("/api/general-utility/prepare-columns", json={
                "file": "sample.csv",
                "preservation": [{"source_field": "OwnerId", "output_field": "Legacy_OwnerId__c"}],
            })
            self.assertEqual(response.status_code, 200)
            download = self.client.get(response.get_json()["download"])
        try:
            self.assertEqual(download.status_code, 200)
            self.assertIn(b"Legacy_OwnerId__c", download.data)
        finally:
            download.close()

    def test_field_creation_runs_as_progress_job(self) -> None:
        connector = FakeMetadataConnector()
        job_dir = self.root / "jobs"
        with patch.object(app_module, "get_connector", return_value=connector), \
                patch.object(app_module, "clear_metadata_cache"), \
                patch.object(app_module, "GENERAL_UTILITY_JOB_DIR", job_dir), \
                patch.object(app_module.RUN_AUDIT, "upsert_submission"), \
                patch.object(app_module.RUN_AUDIT, "update_status"):
            response = self.client.post("/api/general-utility/create-fields", json={
                "object": "Account",
                "fields": [{"api_name": "Legacy_Context__c", "type": "Text", "length": 255}],
                "permission_sets": ["Migration_User"],
                "profiles": [],
            })
            self.assertEqual(response.status_code, 202)
            status_url = response.get_json()["status_url"]
            payload = None
            for _ in range(100):
                payload = self.client.get(status_url).get_json()
                if payload["status"] in {"completed", "error"}:
                    break
                time.sleep(0.01)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["percent"], 100)
        self.assertEqual(payload["completed_steps"], payload["total_steps"])
        self.assertTrue(any("Created field" in event["message"] for event in payload["events"]))
        self.assertTrue((job_dir / f"{payload['id']}.json").is_file())
        self.assertEqual(self.client.get(f"/general-utility/runs/{payload['id']}").status_code, 200)

    def test_orphaned_field_job_is_reconciled_in_detail_and_audit(self) -> None:
        job_id = "a" * 32
        job_dir = self.root / "jobs"
        job_dir.mkdir()
        (job_dir / f"{job_id}.json").write_text(json.dumps({
            "id": job_id,
            "status": "running",
            "object": "Account",
            "message": "Granting access...",
            "events": [],
        }), encoding="utf-8")
        app_module._general_utility_jobs.pop(job_id, None)

        with patch.object(app_module, "GENERAL_UTILITY_JOB_DIR", job_dir), \
                patch.object(app_module.RUN_AUDIT, "update_status") as update_status:
            response = self.client.get(f"/api/general-utility/create-fields/{job_id}")

        payload = response.get_json()
        self.assertEqual(payload["status"], "error")
        self.assertIn("stopped or restarted", payload["error"])
        self.assertTrue(payload["finished_at"])
        update_status.assert_called_once()
        self.assertEqual(update_status.call_args.kwargs["status"], "FAILED")
        persisted = json.loads((job_dir / f"{job_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["status"], "error")


class BulkFlsCoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_reads_line_and_csv_object_lists(self) -> None:
        text_path = self.root / "objects.txt"
        text_path.write_text("Account\n# comment\nContact\nAccount\n", encoding="utf-8")
        self.assertEqual(read_object_list(text_path), ["Account", "Contact"])

        csv_path = self.root / "objects.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows([["Object"], ["Case"], ["Opportunity"]])
        self.assertEqual(read_object_list(csv_path), ["Case", "Opportunity"])

    def test_assign_object_access_grants_edit_or_read(self) -> None:
        class BulkConnector(FakeMetadataConnector):
            def describe_object(self, object_name):
                return {
                    "createable": True,
                    "updateable": True,
                    "deletable": True,
                    "fields": [
                        {"name": "Name", "permissionable": True, "updateable": True, "label": "Name"},
                        {"name": "Formula__c", "permissionable": True, "updateable": False, "label": "Formula"},
                        {"name": "Id", "permissionable": False, "updateable": False, "label": "Id"},
                    ],
                }

            def query(self, soql):
                if "FROM ObjectPermissions" in soql:
                    return []
                if "FROM FieldPermissions" in soql:
                    return []
                if "FROM RecordType" in soql:
                    return [{"Id": "0121", "DeveloperName": "Customer", "Name": "Customer", "IsActive": True}]
                if "FROM SetupEntityAccess" in soql:
                    return []
                return super().query(soql)

        connector = BulkConnector()
        parents = [{
            "parent_id": "0PS1",
            "label": "PermissionSet:Migration_User",
            "kind": "permission_set",
            "name": "Migration_User",
            "profile_owned": False,
        }]
        result = assign_object_access(connector, "Account", parents)
        self.assertEqual(result["field_count"], 2)
        field_payloads = [payload for method, url, payload in connector.requests if method == "POST" and "FieldPermissions" in url]
        self.assertEqual(len(field_payloads), 2)
        by_field = {item["Field"]: item for item in field_payloads}
        self.assertTrue(by_field["Account.Name"]["PermissionsEdit"])
        self.assertFalse(by_field["Account.Formula__c"]["PermissionsEdit"])
        self.assertTrue(any(method == "POST" and "SetupEntityAccess" in url for method, url, _ in connector.requests))


class BulkFlsApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.client = app_module.app.test_client()
        self.objects = self.root / "objects.txt"
        self.objects.write_text("Account\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_bulk_fls_runs_as_progress_job(self) -> None:
        class BulkConnector(FakeMetadataConnector):
            def describe_object(self, object_name):
                return {
                    "createable": True,
                    "updateable": True,
                    "deletable": True,
                    "fields": [
                        {"name": "Name", "permissionable": True, "updateable": True, "label": "Name"},
                    ],
                }

            def query(self, soql):
                if "FROM ObjectPermissions" in soql or "FROM FieldPermissions" in soql or "FROM SetupEntityAccess" in soql:
                    return []
                if "FROM RecordType" in soql:
                    return []
                return super().query(soql)

        connector = BulkConnector()
        job_dir = self.root / "jobs"
        with patch.object(app_module, "get_connector", return_value=connector), \
                patch.object(app_module, "clear_metadata_cache"), \
                patch.object(app_module, "GENERAL_UTILITY_JOB_DIR", job_dir), \
                patch.object(app_module.RUN_AUDIT, "upsert_submission"), \
                patch.object(app_module.RUN_AUDIT, "update_status"):
            response = self.client.post("/api/general-utility/bulk-fls", json={
                "object_list_path": str(self.objects),
                "permission_sets": ["Migration_User"],
                "profiles": [],
                "target_profile": "default",
            })
            self.assertEqual(response.status_code, 202)
            status_url = response.get_json()["status_url"]
            payload = None
            for _ in range(100):
                payload = self.client.get(status_url).get_json()
                if payload["status"] in {"completed", "error"}:
                    break
                time.sleep(0.01)
        self.assertEqual(payload["status"], "completed", payload)
        self.assertEqual(payload["job_type"], "bulk_fls")
        self.assertEqual(payload["percent"], 100)
        self.assertTrue(any("Processing object" in event["message"] for event in payload["events"]))
        page = self.client.get(f"/general-utility/runs/{payload['id']}")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'data-job-type="bulk_fls"', page.data)

    def test_general_utility_page_includes_bulk_fls(self) -> None:
        response = self.client.get("/general-utility")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Bulk FLS assignment", response.data)
        self.assertIn(b"gu-fls-object-list", response.data)


if __name__ == "__main__":
    unittest.main()
