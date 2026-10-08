from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests._runner import ensure_project_root_on_path

ensure_project_root_on_path()

from prevalidate_source import _selected_objects, _types_compatible, _normalize_operation


def test_selected_objects_uses_target_fields_and_operation():
    template = {
        "template_kind": "source_mapping",
        "template": {
            "source_object": "Account",
            "target_object": "Account",
            "default_operation": "insert",
            "field_mappings": [
                {"source_field": "Legacy_Name__c", "target_field": "Name", "include": True},
                {"source_field": "Industry", "target_field": "Industry", "include": True},
            ],
            "match_mapping": {
                "source_field": "Legacy_Id__c",
                "target_field": "External_Id__c",
            },
            "target_external_id": "External_Id__c",
        },
    }
    configs = _selected_objects(template, set())
    assert len(configs) == 1
    config = configs[0]
    assert config["operation"] == "insert"
    assert "Legacy_Name__c" in config["source_fields"]
    assert "Name" in config["target_fields"]
    assert "Legacy_Name__c" not in config["target_fields"]
    assert ("Legacy_Name__c", "Name") in config["field_pairs"]
    assert config["external_id"] == "External_Id__c"


def test_selected_objects_defaults_upsert_and_requires_match_fields_for_upsert_path():
    template = {
        "template_kind": "source_mapping",
        "template": {
            "objects": [
                {
                    "source_object": "Contact",
                    "target_object": "Contact",
                    "allowed_operations": ["upsert"],
                    "field_mappings": [
                        {"source_field": "Email", "target_field": "Email"},
                    ],
                    "match_mapping": {
                        "source_field": "Legacy_Id__c",
                        "target_field": "External_Id__c",
                    },
                }
            ]
        },
    }
    configs = _selected_objects(template, set())
    assert configs[0]["operation"] == "upsert"
    assert configs[0]["match_source"] == "Legacy_Id__c"
    assert configs[0]["match_target"] == "External_Id__c"


def test_type_compatibility_and_operation_helpers():
    assert _types_compatible("string", "textarea")
    assert _types_compatible("double", "currency")
    assert not _types_compatible("boolean", "date")
    assert _normalize_operation("INSERT") == "insert"
    assert _normalize_operation(None, ["update"]) == "update"
    assert _normalize_operation(None, []) == "upsert"


if __name__ == "__main__":
    from tests._runner import run
    raise SystemExit(run(globals()))
