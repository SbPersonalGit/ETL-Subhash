from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import polars as pl

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests._runner import ensure_project_root_on_path
ensure_project_root_on_path()

from core.transformer import Transformer
from core.orchestrator import Orchestrator


def test_explicit_lookup_policy_handles_parent_not_in_pipeline():
    transformer = Transformer(
        stager=SimpleNamespace(),
        id_mapper=SimpleNamespace(),
    )
    profile = SimpleNamespace(
        lookup_fields={"AccountId": ["Account"]},
        field_meta={"AccountId": SimpleNamespace(relationship_name="Account")},
        polymorphic_fields=set(),
    )
    incoming = pl.DataFrame({"AccountId": ["001SOURCE"], "Name": ["Opportunity"]})

    result = transformer._remap_lookups(
        incoming,
        "Opportunity",
        profile,
        [],
        "OwnerId",
        "External_Id__c",
        {},
        {
            "AccountId": {
                "referenced_object": "Account",
                "external_id": "External_Id__c",
            }
        },
    )

    assert "AccountId" not in result.columns
    assert result["Account.External_Id__c"].to_list() == ["001SOURCE"]


def test_lookup_remap_runs_before_field_rename():
    """Renamed lookup source fields must still remap (SF→SF mapping case)."""
    from transforms.rules import apply_rules

    transformer = Transformer(
        stager=SimpleNamespace(),
        id_mapper=SimpleNamespace(),
    )
    profile = SimpleNamespace(
        lookup_fields={"Legacy_Account__c": ["Account"]},
        field_meta={"Legacy_Account__c": SimpleNamespace(relationship_name="Account")},
        polymorphic_fields=set(),
    )
    incoming = pl.DataFrame({
        "Legacy_Account__c": ["001SOURCE"],
        "Name": ["Opp"],
    })
    lookup_mappings = {
        "Legacy_Account__c": {
            "referenced_object": "Account",
            "external_id": "External_Id__c",
        }
    }
    rename_rules = [
        {"type": "field_rename", "source": "Legacy_Account__c", "target": "AccountId"},
    ]

    # Simulate new transform() order: remap, then rename.
    remapped = transformer._remap_lookups(
        incoming,
        "Opportunity",
        profile,
        [],
        "OwnerId",
        "External_Id__c",
        {},
        lookup_mappings,
    )
    result = apply_rules(remapped, rename_rules)

    assert "Legacy_Account__c" not in result.columns
    assert "AccountId" not in result.columns
    assert result["Account.External_Id__c"].to_list() == ["001SOURCE"]


def test_explicit_owner_external_id_policy_takes_precedence_over_user_idmap():
    mapper = SimpleNamespace(
        resolve_column=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("explicit Owner external-ID lookup must not use IDMapper")
        )
    )
    transformer = Transformer(stager=SimpleNamespace(), id_mapper=mapper)
    profile = SimpleNamespace(
        lookup_fields={"OwnerId": ["User"]},
        field_meta={"OwnerId": SimpleNamespace(relationship_name="Owner")},
        polymorphic_fields=set(),
    )
    result = transformer._remap_lookups(
        pl.DataFrame({"OwnerId": ["USER-LEGACY-1"]}),
        "Campaign",
        profile,
        [],
        "OwnerId",
        "External_Id__c",
        {},
        {"OwnerId": {
            "referenced_object": "User",
            "lookup_resolution": "external_id",
            "external_id": "External_Id__c",
        }},
    )

    assert result.columns == ["Owner.External_Id__c"]
    assert result["Owner.External_Id__c"].to_list() == ["USER-LEGACY-1"]


def test_polymorphic_owner_id_resolves_via_user_external_id():
    mapper = SimpleNamespace(
        resolve_column=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("polymorphic OwnerId with User external-id policy must not use IDMapper")
        )
    )
    transformer = Transformer(stager=SimpleNamespace(), id_mapper=mapper)
    profile = SimpleNamespace(
        lookup_fields={"OwnerId": ["Group", "User"]},
        field_meta={"OwnerId": SimpleNamespace(relationship_name="Owner")},
        polymorphic_fields={"OwnerId"},
    )
    result = transformer._remap_lookups(
        pl.DataFrame({"OwnerId": ["USER-LEGACY-1"]}),
        "Campaign",
        profile,
        [],
        "OwnerId",
        "External_Id__c",
        {},
        {
            "OwnerId": {
                "referenced_object": "User",
                "lookup_resolution": "external_id",
                "external_id": "External_Id__c",
            }
        },
    )

    assert result.columns == ["Owner.External_Id__c"]
    assert result["Owner.External_Id__c"].to_list() == ["USER-LEGACY-1"]


def test_polymorphic_owner_id_defaults_to_user_external_id_without_idmap():
    mapper = SimpleNamespace(
        has_mappings=lambda _obj: False,
        resolve_column=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("flat-file OwnerId fallback must not use IDMapper")
        ),
    )
    transformer = Transformer(stager=SimpleNamespace(), id_mapper=mapper)
    profile = SimpleNamespace(
        lookup_fields={"OwnerId": ["Group", "User"]},
        field_meta={"OwnerId": SimpleNamespace(relationship_name="Owner")},
        polymorphic_fields={"OwnerId"},
    )
    result = transformer._remap_lookups(
        pl.DataFrame({"OwnerId": ["USER-LEGACY-1"]}),
        "Campaign",
        profile,
        [],
        "OwnerId",
        "External_Id__c",
        {},
        {},
    )

    assert result.columns == ["Owner.External_Id__c"]
    assert result["Owner.External_Id__c"].to_list() == ["USER-LEGACY-1"]


def test_polymorphic_target_ids_are_preserved_without_idmapper_resolution():
    mapper = SimpleNamespace(
        resolve_column=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("target_id polymorphic lookup must not use IDMapper")
        )
    )
    transformer = Transformer(stager=SimpleNamespace(), id_mapper=mapper)
    profile = SimpleNamespace(
        lookup_fields={
            "WhoId": ["Contact", "Lead"],
            "WhatId": ["Account", "Case", "Opportunity"],
        },
        field_meta={
            "WhoId": SimpleNamespace(relationship_name="Who"),
            "WhatId": SimpleNamespace(relationship_name="What"),
        },
        polymorphic_fields={"WhoId", "WhatId"},
    )
    incoming = pl.DataFrame({
        "WhoId": ["003S900000ABCDEF01"],
        "WhatId": ["500S900000ABCDEF01"],
    })

    result = transformer._remap_lookups(
        incoming,
        "Task",
        profile,
        [],
        "OwnerId",
        "External_Id__c",
        {},
        {
            "WhoId": {"lookup_resolution": "target_id"},
            "WhatId": {"lookup_resolution": "target_id"},
        },
    )

    assert result.columns == ["WhoId", "WhatId"]
    assert result.to_dicts() == incoming.to_dicts()


def test_polymorphic_mixed_policy_checks_target_then_external_id_and_preserves_original():
    class Target:
        def query(self, soql):
            if "WHERE Id IN" in soql:
                return [{"Id": "500TARGET000000001"}]
            return [{"Id": "500TARGET000000002", "External_Id__c": "500SOURCE000000002"}]

    transformer = Transformer(SimpleNamespace(), SimpleNamespace(), target_connector=Target())
    incoming = pl.DataFrame({"WhatId": ["500TARGET000000001", "500SOURCE000000002"]})
    result = transformer._resolve_polymorphic_external_ids(
        incoming,
        "WhatId",
        [{
            "identify_by": "prefix", "match_value": "500", "referenced_object": "Case",
            "external_id": "External_Id__c", "fallback_id": "", "preserve_original_as": "Legacy_WhatId__c",
        }],
        target_first=True,
    )
    assert result["WhatId"].to_list() == ["500TARGET000000001", "500TARGET000000002"]
    assert result["Legacy_WhatId__c"].to_list() == ["500TARGET000000001", "500SOURCE000000002"]


def test_renamed_lookup_uses_target_relationship_metadata():
    target = SimpleNamespace(describe_object=lambda _name: {
        "fields": [{"name": "AccountId", "relationshipName": "Account"}],
    })
    transformer = Transformer(
        stager=SimpleNamespace(),
        id_mapper=SimpleNamespace(),
        target_connector=target,
    )
    profile = SimpleNamespace(
        lookup_fields={"Legacy_Account__c": ["Account"]},
        field_meta={"Legacy_Account__c": SimpleNamespace(relationship_name="Legacy_Account__r")},
        polymorphic_fields=set(),
    )
    result = transformer._remap_lookups(
        pl.DataFrame({"Legacy_Account__c": ["ACC-100"]}),
        "Opportunity",
        profile,
        [],
        "OwnerId",
        "External_Id__c",
        {"Account": "External_Id__c"},
        {
            "Legacy_Account__c": {
                "referenced_object": "Account",
                "external_id": "External_Id__c",
                "target_field": "AccountId",
            }
        },
    )

    assert result.columns == ["Account.External_Id__c"]
    assert result["Account.External_Id__c"].to_list() == ["ACC-100"]


def test_cycle_defers_referenced_external_key_instead_of_source_salesforce_id():
    captured = {}
    mapper = SimpleNamespace(
        save_deferred_column=lambda obj, field, record_keys, parent_keys: captured.update({
            "object": obj,
            "field": field,
            "record_keys": record_keys.to_list(),
            "parent_keys": parent_keys.to_list(),
        })
    )
    transformer = Transformer(stager=SimpleNamespace(), id_mapper=mapper)
    profile = SimpleNamespace(
        lookup_fields={"ParentId": ["Account"]},
        field_meta={"ParentId": SimpleNamespace(relationship_name="Parent")},
        polymorphic_fields=set(),
    )
    result = transformer._remap_lookups(
        pl.DataFrame({
            "Id": ["001SOURCE100"],
            "Account_Key": ["ACC-100"],
            "ParentId": ["001SOURCE200"],
            "Parent.Account_Key": ["ACC-200"],
        }),
        "Account",
        profile,
        ["ParentId"],
        "OwnerId",
        "External_Id__c",
        {"Account": "External_Id__c"},
        {
            "ParentId": {
                "referenced_object": "Account",
                "referenced_source_key": "Account_Key",
                "external_id": "External_Id__c",
            }
        },
        source_key_field="Account_Key",
    )

    assert captured["record_keys"] == ["ACC-100"]
    assert captured["parent_keys"] == ["ACC-200"]
    assert "ParentId" not in result.columns
    assert "Parent.Account_Key" not in result.columns


def test_cycle_pass2_resolves_parent_already_present_in_target_by_external_id():
    mapper = SimpleNamespace(
        deferred_count=lambda *_args: 2,
        get_deferred_updates=lambda *_args: pl.DataFrame({
            "target_record_id": ["CHILD-TARGET-1"],
            "resolved_target_id": ["PARENT-TARGET-IN-RUN"],
        }),
        get_deferred_records=lambda *_args: pl.DataFrame({
            "target_record_id": ["CHILD-TARGET-1", "CHILD-TARGET-2"],
            "deferred_source_id": ["PARENT-SOURCE-1", "PARENT-EXISTING"],
        }),
    )
    target = SimpleNamespace(query=lambda soql: [
        {"Id": "PARENT-TARGET-EXISTING", "External_Id__c": "PARENT-EXISTING"}
    ])
    profile = SimpleNamespace(lookup_fields={"ParentId": ["Campaign"]})
    captured = {}

    from core.loader import Loader
    original_update = Loader.update_records
    Loader.update_records = lambda _self, _object, frame: (
        captured.update({"rows": frame.sort("Id").to_dicts()}) or len(frame)
    )
    try:
        result = Transformer(SimpleNamespace(), mapper).resolve_cyclic_pass2(
            "Campaign", profile, ["ParentId"], target,
            referenced_external_id_field="External_Id__c",
        )
    finally:
        Loader.update_records = original_update

    assert result == {"ParentId": 2}
    assert captured["rows"] == [
        {"Id": "CHILD-TARGET-1", "ParentId": "PARENT-TARGET-IN-RUN"},
        {"Id": "CHILD-TARGET-2", "ParentId": "PARENT-TARGET-EXISTING"},
    ]


def test_deferred_activity_uses_task_external_id_in_pass2():
    mapper = SimpleNamespace(
        deferred_count=lambda *_args: 1,
        get_deferred_updates=lambda *_args: pl.DataFrame(
            schema={"target_record_id": pl.Utf8, "resolved_target_id": pl.Utf8}
        ),
        get_deferred_records=lambda *_args: pl.DataFrame({
            "target_record_id": ["02sTARGET"],
            "deferred_source_id": ["00TSOURCE"],
        }),
    )
    queries = []
    target = SimpleNamespace(query=lambda soql: (
        queries.append(soql)
        or [{"Id": "00TTARGET", "External_Id__c": "00TSOURCE"}]
    ))
    profile = SimpleNamespace(lookup_fields={"ActivityId": ["Task"]})
    captured = {}

    from core.loader import Loader
    original_update = Loader.update_records
    Loader.update_records = lambda _self, _object, frame: (
        captured.update({"rows": frame.to_dicts()}) or len(frame)
    )
    try:
        result = Transformer(SimpleNamespace(), mapper).resolve_cyclic_pass2(
            "EmailMessage",
            profile,
            ["ActivityId"],
            target,
            referenced_external_id_field="Email_Legacy_ID__c",
            lookup_mappings={"ActivityId": {"external_id": "External_Id__c"}},
        )
    finally:
        Loader.update_records = original_update

    assert "FROM Task WHERE External_Id__c IN" in queries[0]
    assert result == {"ActivityId": 1}
    assert captured["rows"] == [{"Id": "02sTARGET", "ActivityId": "00TTARGET"}]


def test_missing_relationship_name_never_becomes_string_none():
    target = SimpleNamespace(describe_object=lambda _name: {
        "fields": [{"name": "ActivityId", "relationshipName": None}]
    })
    transformer = Transformer(SimpleNamespace(), SimpleNamespace(), target_connector=target)
    assert transformer._target_relationship_name("EmailMessage", "ActivityId", None) == ""


def test_create_only_activity_external_id_mapping_is_not_deferred():
    orchestrator = Orchestrator(SimpleNamespace())
    profile = SimpleNamespace(
        lookup_fields={"ActivityId": ["Task"]},
        field_meta={"ActivityId": SimpleNamespace(relationship_name=None, updateable=False)},
    )
    fields = orchestrator._resolve_cyclic_fields(
        "EmailMessage",
        profile,
        {
            "fields": ["ActivityId"],
            "lookup_mappings": {
                "ActivityId": {
                    "referenced_object": "Task",
                    "lookup_resolution": "external_id",
                    "external_id": "External_Id__c",
                }
            },
        },
        [],
    )
    assert fields == []


def test_relationshipless_activity_is_resolved_before_insert():
    queries = []
    target = SimpleNamespace(query=lambda soql: (
        queries.append(soql)
        or [{"Id": "00TTARGET", "External_Id__c": "00TSOURCE"}]
    ))
    transformer = Transformer(
        stager=SimpleNamespace(),
        id_mapper=SimpleNamespace(),
        target_connector=target,
    )
    profile = SimpleNamespace(
        lookup_fields={"ActivityId": ["Task"]},
        field_meta={"ActivityId": SimpleNamespace(relationship_name=None)},
        polymorphic_fields=set(),
    )
    result = transformer._remap_lookups(
        pl.DataFrame({"ActivityId": ["00TSOURCE"]}),
        "EmailMessage",
        profile,
        [],
        "OwnerId",
        "External_Id__c",
        {},
        {"ActivityId": {
            "referenced_object": "Task",
            "lookup_resolution": "deferred_external_id",
            "external_id": "External_Id__c",
        }},
    )
    assert "FROM Task WHERE External_Id__c IN" in queries[0]
    assert result["ActivityId"].to_list() == ["00TTARGET"]


def test_source_extraction_queries_parent_source_key_not_target_external_id():
    orchestrator = Orchestrator(SimpleNamespace())
    profile = SimpleNamespace(
        include_fields=["Legacy_Account__c"],
        lookup_fields={"Legacy_Account__c": ["Account"]},
        field_meta={"Legacy_Account__c": SimpleNamespace(relationship_name="Legacy_Account__r")},
    )
    soql = orchestrator._build_soql_for_object(
        "Opportunity",
        {
            "name": "Opportunity",
            "fields": ["Legacy_Account__c"],
            "lookup_mappings": {
                "Legacy_Account__c": {
                    "referenced_object": "Account",
                    "referenced_source_key": "Account_Number__c",
                    "external_id": "External_Id__c",
                }
            },
        },
        profile,
        [
            {"name": "Account", "external_id": "External_Id__c"},
            {"name": "Opportunity"},
        ],
    )

    assert "Legacy_Account__r.Account_Number__c" in soql
    assert "Legacy_Account__r.External_Id__c" not in soql


if __name__ == "__main__":
    import sys
    from tests._runner import run
    sys.exit(run(globals()))
