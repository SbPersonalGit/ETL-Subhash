from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests._runner import ensure_project_root_on_path
ensure_project_root_on_path()

from core.identity_compiler import compile_stage_identities


def test_compiler_builds_load_and_reference_nodes_without_changing_runtime_shape():
    result = compile_stage_identities([
        {"name": "Opportunity", "external_id": "External_Id__c", "lookup_mappings": {
            "AccountId": {
                "referenced_object": "Account",
                "lookup_resolution": "object_template",
                "referenced_source_key": "Account_Number__c",
                "external_id": "External_Id__c",
            },
        }},
    ], "salesforce_source")

    nodes = {node["object"]: node for node in result.graph["nodes"]}
    assert nodes["Opportunity"]["load"] is True
    assert nodes["Account"]["load"] is False
    assert nodes["Account"]["identity"]["source_field"] == "Account_Number__c"
    mapping = result.objects[0]["lookup_mappings"]["AccountId"]
    assert mapping["external_id"] == "External_Id__c"


def test_compiler_uses_parent_node_identity_for_in_pipeline_lookup():
    result = compile_stage_identities([
        {"name": "Account", "external_id": "External_Id__c", "source_key_field": "Account_Key"},
        {"name": "Contact", "external_id": "Contact_Key__c", "lookup_mappings": {
            "AccountId": {"referenced_object": "Account", "external_id": "External_Id__c"},
        }},
    ], "flat_file")

    mapping = result.objects[1]["lookup_mappings"]["AccountId"]
    assert mapping["lookup_resolution"] == "pipeline_object"
    assert mapping["referenced_source_key"] == "Account_Key"
    assert len([node for node in result.graph["nodes"] if node["load"]]) == 2


def test_compiler_preserves_legacy_unresolved_lookup_for_runtime_fallback():
    result = compile_stage_identities([
        {"name": "Case", "external_id": "External_Id__c", "lookup_mappings": {
            "BusinessHoursId": {"referenced_object": "BusinessHours"},
        }},
    ], "salesforce_source")
    assert result.objects[0]["lookup_mappings"]["BusinessHoursId"]["referenced_object"] == "BusinessHours"
    assert all(node["object"] != "BusinessHours" for node in result.graph["nodes"])


if __name__ == "__main__":
    from tests._runner import run
    sys.exit(run(globals()))
