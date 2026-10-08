from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests._runner import ensure_project_root_on_path

ensure_project_root_on_path()

from ui.app import _build_pipeline_entry, app

TEST_TEMP_ROOT = PROJECT_ROOT / "temp"


def test_source_mode_alias_builds_seed_pipeline_entry():
    entry = _build_pipeline_entry({
        "input_mode": "salesforce_source",
        "pipeline_id": "acct_seed",
        "pipeline_name": "Account Seed",
        "seed_object": "Account",
        "selections": [
            {
                "object": "Account",
                "selected_fields": ["Id", "Name"],
                "external_id": "External_Id__c",
                "operation": "update",
                "match_by": "External_Id__c",
            }
        ],
        "nodes": [{"id": "n1", "object": "Account", "direction": "source"}],
    }, 1)

    assert "input" not in entry
    assert entry["seed"]["object"] == "Account"
    assert entry["objects"][0]["name"] == "Account"
    assert entry["objects"][0]["operation"] == "update"
    assert entry["objects"][0]["match_by"] == "External_Id__c"


def test_save_source_template_persists_mode_and_profiles():
    with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as temp_dir:
        template_dir = Path(temp_dir)
        with patch("ui.app.TEMPLATE_DIR", template_dir):
            response = app.test_client().post(
                "/api/templates",
                json={
                    "template_name": "Source Accounts",
                    "input_mode": "salesforce_seed",
                    "template_kind": "source_mapping",
                    "source_profile": "prod",
                    "target_profile": "uat",
                    "template": {
                        "source_object": "Account",
                        "target_object": "Account",
                        "query_filter": "WHERE IsDeleted = false",
                        "target_external_id": "External_Id__c",
                        "field_mappings": [
                            {"source_field": "Legacy_Id__c", "target_field": "External_Id__c"}
                        ],
                    },
                },
            )

            assert response.status_code == 200, response.get_json()
            payload = response.get_json()
            stored = template_dir / f"{payload['template_id']}.json"
            saved = stored.read_text(encoding="utf-8")

            list_response = app.test_client().get("/api/templates")

    assert '"input_mode": "salesforce_source"' in saved
    assert '"source_profile": "prod"' in saved
    assert '"target_profile": "uat"' in saved
    assert '"source_domain"' in saved
    assert '"target_domain"' in saved
    templates = list_response.get_json()["templates"]
    assert templates[0]["input_mode"] == "salesforce_source"
    assert templates[0]["source_profile"] == "prod"
    assert templates[0]["target_profile"] == "uat"
    assert "source_domain" in templates[0]
    assert "target_domain" in templates[0]
    assert templates[0]["template_kind"] == "source_mapping"
    assert templates[0]["source_object"] == "Account"
    assert templates[0]["target_object"] == "Account"


def test_prevalidation_route_accepts_source_templates():
    with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as temp_dir:
        temp_path = Path(temp_dir)
        template_dir = temp_path / "templates"
        template_dir.mkdir()
        source_template = template_dir / "source_accounts.json"
        source_template.write_text(
            """
{
  "template_id": "source_accounts",
  "template_name": "Source Accounts",
  "input_mode": "salesforce_source",
  "source_profile": "prod",
  "target_profile": "uat",
  "template": {
    "nodes": [{"id": "root", "object": "Account"}],
    "rootId": "root",
    "nodeSelections": {"root": {"selectedFields": ["Name"], "externalId": "External_Id__c"}}
  }
}
""".strip(),
            encoding="utf-8",
        )
        with (
            patch("ui.app.TEMPLATE_DIR", template_dir),
            patch("ui.app._runner_status", return_value={"running": False}),
            patch("ui.app._active_audited_validation", return_value=None),
            patch("ui.app._launch_background_command", return_value={"running": True, "pid": 123}),
            patch.object(app, "logger", create=True),
            patch("ui.app.RUN_AUDIT.upsert_submission"),
            patch("ui.app.RUN_AUDIT.update_status"),
        ):
            response = app.test_client().post(
                "/api/validation/preflight",
                json={"template_id": "source_accounts", "objects": ["Account"]},
            )

    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["ok"] is True


def test_source_org_builder_has_dedicated_route():
    home = app.test_client().get("/")
    source = app.test_client().get("/source-org")

    assert home.status_code == 200
    assert source.status_code == 200
    assert b"Choose your migration flow" in home.data
    assert b"Source Query Mapping Template" in source.data


def test_pipeline_validation_route_accepts_stage_sequence():
    with (
        patch("ui.app._runner_status", return_value={"running": False}),
        patch("ui.app._active_audited_validation", return_value=None),
        patch("ui.app._launch_background_command", return_value={"running": True, "pid": 123}),
        patch.object(app, "logger", create=True),
        patch("ui.app.RUN_AUDIT.upsert_submission"),
        patch("ui.app.RUN_AUDIT.update_status"),
        patch("ui.app._saved_pipeline_snapshot_error", return_value=None),
    ):
        response = app.test_client().post(
            "/api/pipeline/validate",
            json={
                "pipeline_name": "Account Validation",
                "pipeline_id": "account_validation",
                "stages": [
                    {
                        "pipeline_id": "source_accounts",
                        "pipeline_name": "Source Accounts",
                        "template_id": "source_accounts",
                        "input_mode": "salesforce_source",
                        "object_names": ["Account"],
                    }
                ],
            },
        )

    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["progress_url"].startswith("/validation/runs/")


def test_pipeline_validation_requires_saved_snapshot():
    with (
        patch("ui.app._runner_status", return_value={"running": False}),
        patch("ui.app._active_audited_validation", return_value=None),
    ):
        response = app.test_client().post(
            "/api/pipeline/validate",
            json={
                "pipeline_name": "Unsaved Pipeline",
                "stages": [{"template_id": "source_accounts", "input_mode": "salesforce_source"}],
            },
        )
    assert response.status_code == 409
    assert "save" in (response.get_json().get("error") or "").lower()


def test_pipeline_skip_prevalidation_marks_stages_bypassed():
    with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as temp_dir:
        template_dir = Path(temp_dir)
        template_path = template_dir / "source_accounts.json"
        template_path.write_text(
            """
{
  "template_id": "source_accounts",
  "template_name": "Source Accounts",
  "input_mode": "salesforce_source",
  "template_kind": "source_mapping",
  "prevalidation_status": "DRAFT",
  "template": {
    "source_object": "Account",
    "target_object": "Account",
    "field_mappings": []
  }
}
""".strip(),
            encoding="utf-8",
        )
        with (
            patch("ui.app.TEMPLATE_DIR", template_dir),
            patch("ui.app.RUN_AUDIT.upsert_submission"),
            patch("ui.app.RUN_AUDIT.update_status"),
            patch("ui.app._saved_pipeline_snapshot_error", return_value=None),
        ):
            response = app.test_client().post(
                "/api/pipeline/skip-prevalidation",
                json={
                    "pipeline_name": "Account Flow",
                    "stages": [
                        {
                            "pipeline_id": "source_accounts",
                            "pipeline_name": "Source Accounts",
                            "template_id": "source_accounts",
                            "input_mode": "salesforce_source",
                        }
                    ],
                },
            )
            saved = template_path.read_text(encoding="utf-8")

    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["prevalidation_status"] == "BYPASSED"
    assert '"prevalidation_status": "BYPASSED"' in saved


def test_pipeline_validate_rejects_mixed_operation_types():
    response = app.test_client().post(
        "/api/pipeline/validate",
        json={
            "pipeline_name": "Mixed Flow",
            "stages": [
                {
                    "pipeline_id": "source_accounts",
                    "pipeline_name": "Source Accounts",
                    "template_id": "source_accounts",
                    "input_mode": "salesforce_source",
                },
                {
                    "pipeline_id": "flat_accounts",
                    "pipeline_name": "Flat Accounts",
                    "template_id": "flat_accounts",
                    "input_mode": "flat_file",
                },
            ],
        },
    )
    assert response.status_code == 400
    assert "cannot mix" in (response.get_json().get("error") or "").lower()


def test_pipeline_validate_requires_unique_pipeline_name():
    empty = app.test_client().post(
        "/api/pipeline/validate",
        json={"pipeline_name": "", "stages": [{"template_id": "x", "input_mode": "salesforce_source"}]},
    )
    assert empty.status_code == 400
    assert "required" in (empty.get_json().get("error") or "").lower()

    with patch(
        "ui.app.RUN_AUDIT.list_runs",
        return_value=[{"activity_type": "MIGRATION", "migration_name": "Existing Flow - Migration"}],
    ):
        duplicate = app.test_client().post(
            "/api/pipeline/validate",
            json={
                "pipeline_name": "Existing Flow",
                "stages": [{"template_id": "x", "input_mode": "salesforce_source"}],
            },
        )
    assert duplicate.status_code == 400
    assert "already used" in (duplicate.get_json().get("error") or "").lower()


def test_pipeline_skip_allows_reused_pipeline_name():
    with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as tmp:
        template_dir = Path(tmp)
        template_path = template_dir / "source_accounts.json"
        template_path.write_text(
            """
{
  "template_id": "source_accounts",
  "template_name": "Source Accounts",
  "input_mode": "salesforce_source",
  "template_kind": "source_mapping",
  "prevalidation_status": "DRAFT",
  "template": {
    "source_object": "Account",
    "target_object": "Account",
    "field_mappings": []
  }
}
""".strip(),
            encoding="utf-8",
        )
        with (
            patch("ui.app.TEMPLATE_DIR", template_dir),
            patch("ui.app.RUN_AUDIT.upsert_submission"),
            patch("ui.app.RUN_AUDIT.update_status"),
            patch("ui.app._saved_pipeline_snapshot_error", return_value=None),
            patch(
                "ui.app.RUN_AUDIT.list_runs",
                return_value=[{"activity_type": "MIGRATION", "migration_name": "Account Flow - Migration"}],
            ),
        ):
            response = app.test_client().post(
                "/api/pipeline/skip-prevalidation",
                json={
                    "pipeline_name": "Account Flow",
                    "stages": [
                        {
                            "pipeline_id": "source_accounts",
                            "pipeline_name": "Source Accounts",
                            "template_id": "source_accounts",
                            "input_mode": "salesforce_source",
                        }
                    ],
                },
            )

    assert response.status_code == 200, response.get_json()
    assert response.get_json().get("ok") is True


def test_pipeline_skip_rejects_mixed_operation_types():
    response = app.test_client().post(
        "/api/pipeline/skip-prevalidation",
        json={
            "pipeline_name": "Mixed Flow",
            "stages": [
                {
                    "pipeline_id": "source_accounts",
                    "pipeline_name": "Source Accounts",
                    "template_id": "source_accounts",
                    "input_mode": "salesforce_source",
                },
                {
                    "pipeline_id": "flat_accounts",
                    "pipeline_name": "Flat Accounts",
                    "template_id": "flat_accounts",
                    "input_mode": "flat_file",
                },
            ],
        },
    )
    assert response.status_code == 400
    assert "cannot mix" in (response.get_json().get("error") or "").lower()


def test_pipeline_run_rejects_mixed_operation_types():
    response = app.test_client().post(
        "/api/pipeline/run",
        json={
            "migration_name": "Mixed Flow - Migration",
            "pipelines": [
                {
                    "pipeline_id": "source_accounts",
                    "pipeline_name": "Source Accounts",
                    "input_mode": "salesforce_source",
                    "seed_object": "Account",
                    "selections": [
                        {
                            "object": "Account",
                            "selected_fields": ["Id", "Name"],
                            "external_id": "External_Id__c",
                        }
                    ],
                    "nodes": [{"id": "n1", "object": "Account", "direction": "source"}],
                },
                {
                    "pipeline_id": "flat_accounts",
                    "pipeline_name": "Flat Accounts",
                    "input_mode": "flat_file",
                    "flat_file_template_id": "flat_accounts",
                    "seed_object": "Account",
                    "selections": [
                        {
                            "object": "Account",
                            "selected_fields": ["External_Id__c", "Name"],
                            "external_id": "External_Id__c",
                        }
                    ],
                    "nodes": [{"id": "n1", "object": "Account", "direction": "flat_file"}],
                },
            ],
        },
    )
    assert response.status_code == 400
    assert "cannot mix" in (response.get_json().get("error") or "").lower()


def test_archived_template_domains_resolve_from_profiles():
    with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as temp_dir:
        template_dir = Path(temp_dir)
        (template_dir / "legacy.json").write_text(
            json.dumps({
                "template_id": "legacy",
                "template_name": "Legacy",
                "input_mode": "salesforce_source",
                "template_kind": "source_mapping",
                "source_profile": "default",
                "target_profile": "default",
                "template": {
                    "source_object": "Account",
                    "target_object": "Account",
                    "field_mappings": [{"source_field": "Name", "target_field": "Name"}],
                },
            }),
            encoding="utf-8",
        )
        with patch("ui.app.TEMPLATE_DIR", template_dir):
            with patch.dict(
                "os.environ",
                {
                    "SOURCE_SF_LOGIN_URL": "https://source-archive.example.my.salesforce.com",
                    "TARGET_SF_LOGIN_URL": "https://target-archive.example.my.salesforce.com",
                },
                clear=False,
            ):
                response = app.test_client().get("/api/templates")

    assert response.status_code == 200
    templates = response.get_json()["templates"]
    assert templates[0]["source_domain"] == "source-archive.example.my.salesforce.com"
    assert templates[0]["target_domain"] == "target-archive.example.my.salesforce.com"


def test_save_source_cycle_group_lists_multiple_objects():
    with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as temp_dir:
        template_dir = Path(temp_dir)
        with patch("ui.app.TEMPLATE_DIR", template_dir):
            response = app.test_client().post(
                "/api/templates",
                json={
                    "template_name": "Account Contact Cycle",
                    "input_mode": "salesforce_source",
                    "template_kind": "source_cycle_group",
                    "source_profile": "default",
                    "target_profile": "default",
                    "template": {
                        "objects": [
                            {
                                "source_object": "Account",
                                "target_object": "Account",
                                "query_filter": "",
                                "allowed_operations": ["upsert"],
                                "default_operation": "upsert",
                                "match_mapping": {"source_field": "Id", "target_field": "External_Id__c"},
                                "target_external_id": "External_Id__c",
                                "field_mappings": [{"source_field": "Name", "target_field": "Name"}],
                            },
                            {
                                "source_object": "Contact",
                                "target_object": "Contact",
                                "query_filter": "",
                                "allowed_operations": ["upsert"],
                                "default_operation": "upsert",
                                "match_mapping": {"source_field": "Id", "target_field": "External_Id__c"},
                                "target_external_id": "External_Id__c",
                                "field_mappings": [
                                    {"source_field": "LastName", "target_field": "LastName"},
                                    {"source_field": "AccountId", "target_field": "AccountId"},
                                ],
                            },
                        ]
                    },
                },
            )
            assert response.status_code == 200, response.get_json()
            list_response = app.test_client().get("/api/templates")

    templates = list_response.get_json()["templates"]
    assert templates[0]["template_kind"] == "source_cycle_group"
    assert templates[0]["object_count"] == 2
    assert templates[0]["object_names"] == ["Account", "Contact"]
    assert templates[0]["seed_object"] == "Account"


def test_multi_object_source_pipeline_entry_keeps_one_pipeline():
    entry = _build_pipeline_entry({
        "input_mode": "salesforce_source",
        "pipeline_id": "acct_contact_cycle",
        "pipeline_name": "Account Contact Cycle",
        "seed_object": "Account",
        "selections": [
            {
                "object": "Account",
                "selected_fields": ["Id", "Name"],
                "external_id": "External_Id__c",
                "operation": "upsert",
                "match_by": "External_Id__c",
            },
            {
                "object": "Contact",
                "selected_fields": ["Id", "LastName", "AccountId"],
                "external_id": "External_Id__c",
                "operation": "upsert",
                "match_by": "External_Id__c",
            },
        ],
        "nodes": [
            {"id": "n1", "object": "Account", "direction": "seed"},
            {"id": "n2", "object": "Contact", "direction": "child"},
        ],
    }, 1)

    assert entry["id"] == "acct_contact_cycle"
    assert entry["seed"]["object"] == "Account"
    assert [obj["name"] for obj in entry["objects"]] == ["Account", "Contact"]


def test_prevalidate_source_selected_objects_from_cycle_group():
    from prevalidate_source import _selected_objects

    template = {
        "template_kind": "source_cycle_group",
        "template": {
            "objects": [
                {
                    "source_object": "Account",
                    "target_object": "Account",
                    "field_mappings": [{"source_field": "Name", "target_field": "Name"}],
                    "match_mapping": {"source_field": "Id", "target_field": "External_Id__c"},
                },
                {
                    "source_object": "Contact",
                    "target_object": "Contact",
                    "field_mappings": [{"source_field": "LastName", "target_field": "LastName"}],
                    "match_mapping": {"source_field": "Id", "target_field": "External_Id__c"},
                },
            ]
        },
    }
    objects = _selected_objects(template, set())
    assert [item["name"] for item in objects] == ["Account", "Contact"]
    assert "Name" in objects[0]["fields"]
    assert objects[0]["external_id"] == "External_Id__c"


def test_multi_object_pipeline_entry_preserves_lookup_external_ids():
    entry = _build_pipeline_entry({
        "input_mode": "salesforce_source",
        "pipeline_id": "acct_contact_cycle",
        "pipeline_name": "Account Contact Cycle",
        "seed_object": "Account",
        "selections": [
            {
                "object": "Account",
                "selected_fields": ["Id", "Name", "ParentId"],
                "external_id": "External_Id__c",
                "operation": "upsert",
                "match_by": "External_Id__c",
                "lookup_mappings": {
                    "ParentId": {"referenced_object": "Account", "external_id": "External_Id__c"},
                },
            },
            {
                "object": "Contact",
                "selected_fields": ["Id", "LastName", "AccountId"],
                "external_id": "External_Id__c",
                "operation": "upsert",
                "match_by": "External_Id__c",
                "lookup_mappings": {
                    "AccountId": {"referenced_object": "Account", "external_id": "External_Id__c"},
                },
            },
        ],
        "nodes": [
            {"id": "n1", "object": "Account", "direction": "seed"},
            {"id": "n2", "object": "Contact", "direction": "child"},
        ],
    }, 1)

    contact = next(obj for obj in entry["objects"] if obj["name"] == "Contact")
    assert contact["lookup_mappings"]["AccountId"]["referenced_object"] == "Account"
    assert contact["lookup_mappings"]["AccountId"]["external_id"] == "External_Id__c"


def test_source_pipeline_derives_parent_identity_for_package_lookup():
    entry = _build_pipeline_entry({
        "input_mode": "salesforce_source",
        "pipeline_id": "derived_identity",
        "seed_object": "Account",
        "selections": [
            {
                "object": "Account",
                "selected_fields": ["Id", "Account_Number__c", "Name"],
                "source_key_field": "Account_Number__c",
                "external_id": "External_Id__c",
                "transforms": [{
                    "type": "field_rename",
                    "source": "Account_Number__c",
                    "target": "External_Id__c",
                }],
            },
            {
                "object": "Contact",
                "selected_fields": ["Id", "Legacy_Account__c", "LastName"],
                "external_id": "External_Id__c",
                "transforms": [{
                    "type": "field_rename",
                    "source": "Legacy_Account__c",
                    "target": "AccountId",
                }],
                "lookup_mappings": {
                    "Legacy_Account__c": {
                        "referenced_object": "Account",
                        "lookup_resolution": "pipeline_object",
                    }
                },
            },
        ],
    }, 1)

    account, contact = entry["objects"]
    assert account["identity"]["source_field"] == "Account_Number__c"
    lookup = contact["lookup_mappings"]["Legacy_Account__c"]
    assert lookup["target_field"] == "AccountId"
    assert lookup["referenced_source_key"] == "Account_Number__c"
    assert lookup["external_id"] == "External_Id__c"
    assert lookup["lookup_resolution"] == "pipeline_object"


def test_source_pipeline_preserves_explicit_execution_policies():
    entry = _build_pipeline_entry({
        "input_mode": "salesforce_source",
        "pipeline_id": "preserved_policies",
        "seed_object": "Account",
        "selections": [{
            "object": "Account",
            "selected_fields": ["Id", "ParentId", "OwnerId", "Region__c"],
            "external_id": "External_Id__c",
            "lookup_mappings": {
                "ParentId": {
                    "lookup_resolution": "preserve",
                    "referenced_object": "Account",
                },
                "OwnerId": {
                    "lookup_resolution": "idmapper",
                    "referenced_object": "User",
                },
                "Region__c": {
                    "lookup_resolution": "object_template",
                    "referenced_object": "Region__c",
                    "external_id": "Region_Code__c",
                    "template_id": "region_identity",
                },
            },
        }],
    }, 1)

    mappings = entry["objects"][0]["lookup_mappings"]
    assert mappings["ParentId"]["lookup_resolution"] == "preserve"
    assert mappings["OwnerId"]["lookup_resolution"] == "idmapper"
    assert mappings["Region__c"]["lookup_resolution"] == "object_template"
    assert mappings["Region__c"]["external_id"] == "Region_Code__c"
    assert mappings["Region__c"]["template_id"] == "region_identity"


def test_reference_identity_summary_exposes_source_and_target_keys():
    from ui.app import _template_summary

    summary = _template_summary(Path("reference_accounts.json"), {
        "template_name": "Reference Accounts",
        "template_kind": "reference_identity",
        "input_mode": "salesforce_source",
        "template": {
            "objects": [{
                "object": "Account",
                "match_mapping": {
                    "source_field": "Account_Number__c",
                    "target_field": "External_Id__c",
                },
            }],
        },
    })

    assert summary["template_kind"] == "reference_identity"
    assert summary["object_source_keys"]["Account"] == "Account_Number__c"
    assert summary["object_external_ids"]["Account"] == "External_Id__c"


if __name__ == "__main__":
    from tests._runner import run

    sys.exit(run(globals()))
