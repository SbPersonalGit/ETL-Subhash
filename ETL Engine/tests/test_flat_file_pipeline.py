from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests._runner import ensure_project_root_on_path
ensure_project_root_on_path()

from ui.app import _build_pipeline_entry


def test_flat_file_stage_builds_pipeline_entry_without_seed_traversal():
    entry = _build_pipeline_entry({
        "input_mode": "flat_file",
        "flat_file_template_id": "First_Test",
        "pipeline_id": "First_Test",
        "pipeline_name": "First Test",
        "seed_object": "Account",
        "selections": [
            {
                "object": "Account",
                "selected_fields": ["External_Id__c", "Name"],
                "external_id": "External_Id__c",
            },
            {
                "object": "Opportunity",
                "selected_fields": ["External_Id__c", "AccountId", "Name"],
                "external_id": "External_Id__c",
                "lookup_mappings": {
                    "AccountId": {
                        "referenced_object": "Account",
                        "external_id": "External_Id__c",
                    }
                },
            },
        ],
        "nodes": [],
    }, 1)

    assert entry["input"] == {"type": "flat_file", "template_id": "First_Test"}
    assert [obj["name"] for obj in entry["objects"]] == ["Account", "Opportunity"]
    assert entry["objects"][0]["external_id"] == "External_Id__c"
    assert entry["objects"][1]["lookup_mappings"]["AccountId"]["external_id"] == "External_Id__c"
    assert entry["objects"][0]["identity"] == {
        "source_field": "External_Id__c",
        "source_key_type": "external_key",
        "target_field": "External_Id__c",
    }
    assert entry["objects"][1]["lookup_mappings"]["AccountId"]["lookup_resolution"] == "pipeline_object"
    assert entry["objects"][1]["lookup_mappings"]["AccountId"]["referenced_source_key"] == "External_Id__c"


def test_flat_file_knowledge_stage_compiles_safe_extension_policy():
    entry = _build_pipeline_entry({
        "pipeline_id": "knowledge_files",
        "input_mode": "flat_file",
        "flat_file_template_id": "knowledge_files",
        "selections": [{
            "object": "Knowledge__kav",
            "selected_fields": ["Id", "Title", "Article_Body__c", "PublishStatus"],
            "external_id": "Prod_Record_Id__c",
            "source_key_field": "Id",
        }],
    }, 1)
    extensions = entry["objects"][0]["extensions"]
    assert extensions["scope_expansion"]["enabled"] is False
    assert extensions["image_handling"]["enabled"] is False
    assert extensions["url_rewriting"]["enabled"] is True
    assert extensions["publishing"]["only_if_source_status"] == "Online"


def test_in_pipeline_lookup_rejects_external_id_that_conflicts_with_parent_identity():
    try:
        _build_pipeline_entry({
            "input_mode": "flat_file",
            "pipeline_id": "conflict",
            "selections": [
                {
                    "object": "Account",
                    "selected_fields": ["External_Id__c"],
                    "external_id": "External_Id__c",
                },
                {
                    "object": "Contact",
                    "selected_fields": ["Contact_Key__c", "AccountId"],
                    "external_id": "Contact_Key__c",
                    "lookup_mappings": {
                        "AccountId": {
                            "referenced_object": "Account",
                            "external_id": "Different_Key__c",
                        }
                    },
                },
            ],
        }, 1)
    except ValueError as exc:
        assert "in-pipeline parent identity" in str(exc)
    else:
        raise AssertionError("Expected conflicting parent identity to be rejected")


if __name__ == "__main__":
    import sys
    from tests._runner import run
    sys.exit(run(globals()))
