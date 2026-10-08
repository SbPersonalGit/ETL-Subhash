import csv

from prevalidate_files import (
    _in_pipeline_key_source,
    _lookup_field_for_mapping,
    _merge_reference_failures,
    _split_rejected_error_columns,
)


def test_self_lookup_uses_object_level_external_id():
    config = {"object": "Campaign", "external_id": "External_Id__c"}
    mapping = {
        "target": "ParentId",
        "referenced_object": "Campaign",
        "lookup_resolution": "pipeline_object",
    }

    assert _lookup_field_for_mapping(config, mapping) == "External_Id__c"


def test_explicit_external_lookup_keeps_selected_key():
    config = {"object": "Campaign", "external_id": "External_Id__c"}
    mapping = {
        "target": "OwnerId",
        "referenced_object": "User",
        "lookup_resolution": "external_id",
        "lookup_external_id": "External_Id__c",
    }

    assert _lookup_field_for_mapping(config, mapping) == "External_Id__c"


def test_polymorphic_lookup_uses_matching_object_rule_external_id():
    config = {"object": "Task", "external_id": "External_Id__c"}
    mapping = {
        "target": "OwnerId",
        "referenced_object": "User",
        "lookup_resolution": "polymorphic_external_id",
        "lookup_external_id": "",
        "polymorphic_mappings": [
            {
                "identify_by": "prefix",
                "match_value": "005",
                "referenced_object": "User",
                "external_id": "External_Id__c",
            }
        ],
    }

    assert _lookup_field_for_mapping(config, mapping) == "External_Id__c"


def test_self_lookup_derives_in_file_identity_source_column():
    config = {
        "object": "Campaign",
        "external_id": "External_Id__c",
        "mappings": [
            {"source": "Id", "target": "External_Id__c"},
            {"source": "ParentId", "target": "ParentId"},
        ],
    }
    mapping = {
        "target": "ParentId",
        "referenced_object": "Campaign",
        "lookup_resolution": "pipeline_object",
    }

    assert _in_pipeline_key_source(config, mapping) == "Id"


def test_reference_failures_move_each_affected_row_to_rejected(tmp_path):
    source = tmp_path / "source.csv"
    clean = tmp_path / "clean.csv"
    rejected = tmp_path / "rejected.csv"
    text = "Id,Name,OwnerId,ParentId\nC1,One,U1,P1\nC2,Two,U2,P1\nC3,Three,U1,\n"
    source.write_text(text, encoding="utf-8")
    clean.write_text(text, encoding="utf-8-sig")
    rejected.write_text(
        "External_Id__c,Name,OwnerId,ParentId,__source_row,__validation_errors\n"
        "C2,Two,U2,P1,3,Name: existing error\n",
        encoding="utf-8-sig",
    )
    config = {"mappings": [
        {"source": "Id", "target": "External_Id__c"},
        {"source": "Name", "target": "Name"},
        {"source": "OwnerId", "target": "OwnerId"},
        {"source": "ParentId", "target": "ParentId"},
    ]}
    checks = [
        {"source": "OwnerId", "target": "OwnerId", "referenced_object": "User", "lookup_field": "External_Id__c", "_missing_values": ["U1"]},
        {"source": "ParentId", "target": "ParentId", "referenced_object": "Campaign", "lookup_field": "External_Id__c", "_missing_values": ["P1"]},
    ]

    assert _merge_reference_failures(source, clean, rejected, config, checks) == 3
    with rejected.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["__source_row"] for row in rows] == ["2", "3", "4"]
    assert "OwnerId: reference 'U1' not found" in rows[0]["__validation_errors"]
    assert "ParentId: reference 'P1' not found" in rows[0]["__validation_errors"]
    assert "Name: existing error" in rows[1]["__validation_errors"]
    with clean.open(encoding="utf-8-sig", newline="") as handle:
        assert list(csv.DictReader(handle)) == []

    columns = _split_rejected_error_columns(rejected)
    assert columns == ["__error_OwnerId", "__error_ParentId", "__error_Name"]
    with rejected.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert "__validation_errors" not in rows[0]
    assert "reference 'U1' not found" in rows[0]["__error_OwnerId"]
    assert "reference 'P1' not found" in rows[0]["__error_ParentId"]
    assert rows[1]["__error_Name"] == "existing error"
