"""
Unit tests for the match-by-text-field load path in core.loader.

Covers:
  - _decide_load_strategy auto-detects when external_id_field can / cannot
    drive a Bulk upsert (target metadata describes the field).
  - _query_target_match_ids issues batched IN-clause SOQL and de-duplicates.
  - _process_chunk_match_by splits the chunk into update + insert sub-payloads
    based on existing target rows.
  - IDMapper is populated for both branches (matched updates use the known
    target Id; new inserts pick up sf__Id from the Bulk response).
  - Existing upsert path is byte-identical when the field IS flagged.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import polars as pl

from tests._runner import ensure_project_root_on_path, run
ensure_project_root_on_path()

from core.id_mapper import IDMapper
from core.loader import (
    DEFAULT_LOAD_BATCH_SIZE,
    Loader,
    MATCH_QUERY_BATCH_SIZE,
    MAX_LOAD_BATCH_SIZE,
    MIN_LOAD_BATCH_SIZE,
    resolve_load_batch_size,
)


# ---------------------------------------------------------------- #
#  Test doubles                                                     #
# ---------------------------------------------------------------- #


class _FakeConnector:
    """Minimal stand-in for SFConnector — only the methods the loader hits."""

    def __init__(self):
        self.instance_url = "https://target.my.salesforce.com"
        self.api_version = "v60.0"
        self._describes: dict[str, dict] = {}
        self._query_results: list[list[dict]] = []
        self.queries: list[str] = []
        self._headers = {"Authorization": "Bearer test"}

    def add_describe(self, object_name: str, fields: list[dict]) -> None:
        self._describes[object_name] = {"fields": fields}

    def queue_query_results(self, *result_batches: list[dict]) -> None:
        self._query_results.extend(list(result_batches))

    def describe_object(self, object_name: str) -> dict:
        return self._describes[object_name]

    def query(self, soql: str) -> list[dict]:
        self.queries.append(soql)
        if not self._query_results:
            return []
        return self._query_results.pop(0)


def _make_idmapper() -> IDMapper:
    tmp = tempfile.NamedTemporaryFile(suffix=".duckdb", delete=False)
    tmp.close()
    os.unlink(tmp.name)
    return IDMapper(tmp.name)


def _make_loader(connector: _FakeConnector) -> Loader:
    """Loader with stager=None and a fresh IDMapper. We never go through load()
    in these tests; we exercise the lower-level helpers directly."""
    log_root = Path(tempfile.mkdtemp(prefix="loader_mb_"))
    return Loader(connector=connector, stager=None, id_mapper=_make_idmapper(), log_root=log_root)


def _make_field(name: str, *, external_id: bool = False, unique: bool = False, **extras) -> dict:
    base = {
        "name": name,
        "type": "string",
        "createable": True,
        "updateable": True,
        "nillable": True,
        "externalId": external_id,
        "unique": unique,
        "length": 255,
    }
    base.update(extras)
    return base


# ---------------------------------------------------------------- #
#  Strategy detection                                               #
# ---------------------------------------------------------------- #


def test_decide_strategy_returns_upsert_when_field_is_external_id_and_unique():
    conn = _FakeConnector()
    conn.add_describe("Account", [_make_field("Prod_Record_Id__c", external_id=True, unique=True)])
    loader = _make_loader(conn)

    assert loader._decide_load_strategy("Account", "Prod_Record_Id__c", "upsert") == "upsert"


def test_decide_strategy_returns_match_by_when_external_id_flag_missing():
    conn = _FakeConnector()
    conn.add_describe(
        "Knowledge__kav",
        [_make_field("Prod_Record_Id__c", external_id=False, unique=True)],
    )
    loader = _make_loader(conn)

    assert (
        loader._decide_load_strategy("Knowledge__kav", "Prod_Record_Id__c", "upsert")
        == "match_by"
    )


def test_decide_strategy_returns_match_by_when_unique_flag_missing():
    conn = _FakeConnector()
    conn.add_describe(
        "SomeObj__c",
        [_make_field("Key__c", external_id=True, unique=False)],
    )
    loader = _make_loader(conn)

    assert loader._decide_load_strategy("SomeObj__c", "Key__c", "upsert") == "match_by"


def test_decide_strategy_returns_match_by_when_field_missing_from_describe():
    conn = _FakeConnector()
    conn.add_describe("EmptyObj__c", [])
    loader = _make_loader(conn)

    assert (
        loader._decide_load_strategy("EmptyObj__c", "Missing__c", "upsert") == "match_by"
    )


def test_decide_strategy_passes_through_non_upsert_operations():
    conn = _FakeConnector()
    conn.add_describe(
        "Knowledge__kav",
        [_make_field("Prod_Record_Id__c", external_id=False, unique=False)],
    )
    loader = _make_loader(conn)

    assert loader._decide_load_strategy("Knowledge__kav", "Prod_Record_Id__c", "insert") == "insert"
    assert loader._decide_load_strategy("Knowledge__kav", "Prod_Record_Id__c", "update") == "update"
    assert loader._decide_load_strategy("Knowledge__kav", "Prod_Record_Id__c", "delete") == "delete"


# ---------------------------------------------------------------- #
#  Target match query                                               #
# ---------------------------------------------------------------- #


def test_query_target_match_ids_returns_empty_for_empty_input():
    conn = _FakeConnector()
    loader = _make_loader(conn)

    assert loader._query_target_match_ids("Knowledge__kav", "Prod_Record_Id__c", []) == {}
    assert conn.queries == []


def test_query_target_match_ids_builds_correct_soql_and_returns_map():
    conn = _FakeConnector()
    conn.queue_query_results(
        [
            {"Id": "kav111111111111111", "Prod_Record_Id__c": "src_a"},
            {"Id": "kav222222222222222", "Prod_Record_Id__c": "src_b"},
        ]
    )
    loader = _make_loader(conn)

    matches = loader._query_target_match_ids(
        "Knowledge__kav", "Prod_Record_Id__c", ["src_a", "src_b", "src_c"]
    )

    assert matches == {
        "src_a": "kav111111111111111",
        "src_b": "kav222222222222222",
    }
    assert len(conn.queries) == 1
    soql = conn.queries[0]
    assert "FROM Knowledge__kav" in soql
    assert "WHERE Prod_Record_Id__c IN (" in soql
    assert "'src_a'" in soql and "'src_b'" in soql and "'src_c'" in soql


def test_query_target_match_ids_batches_large_input():
    conn = _FakeConnector()
    big_size = MATCH_QUERY_BATCH_SIZE * 2 + 5
    src_values = [f"src_{i}" for i in range(big_size)]
    conn.queue_query_results([], [], [])
    loader = _make_loader(conn)

    loader._query_target_match_ids("Knowledge__kav", "Prod_Record_Id__c", src_values)

    assert len(conn.queries) == 3, "expected three batched queries for 2*size+5 input"


def test_query_target_match_ids_escapes_quotes_in_source_values():
    conn = _FakeConnector()
    conn.queue_query_results([])
    loader = _make_loader(conn)

    loader._query_target_match_ids(
        "Knowledge__kav", "Prod_Record_Id__c", ["a'b", "c\\d"]
    )

    soql = conn.queries[0]
    assert "'a\\'b'" in soql
    assert "'c\\\\d'" in soql


def test_query_target_match_ids_logs_duplicates_keeps_first():
    conn = _FakeConnector()
    conn.queue_query_results(
        [
            {"Id": "kav111111111111111", "Prod_Record_Id__c": "src_a"},
            {"Id": "kavDIFFERENT22222", "Prod_Record_Id__c": "src_a"},
        ]
    )
    loader = _make_loader(conn)

    matches = loader._query_target_match_ids(
        "Knowledge__kav", "Prod_Record_Id__c", ["src_a"]
    )

    assert matches == {"src_a": "kav111111111111111"}


# ---------------------------------------------------------------- #
#  Match / split                                                    #
# ---------------------------------------------------------------- #


def test_split_match_by_chunk_no_target_matches_routes_all_to_insert():
    conn = _FakeConnector()
    loader = _make_loader(conn)
    chunk = pl.DataFrame(
        {"Prod_Record_Id__c": ["a", "b"], "Title": ["t1", "t2"]}
    )

    update_df, insert_df = loader._split_match_by_chunk(chunk, "Prod_Record_Id__c", {})

    assert update_df.is_empty()
    assert len(insert_df) == 2


def test_split_match_by_chunk_partial_match_splits_correctly():
    conn = _FakeConnector()
    loader = _make_loader(conn)
    chunk = pl.DataFrame(
        {
            "Prod_Record_Id__c": ["a", "b", "c"],
            "Title": ["ta", "tb", "tc"],
        }
    )

    update_df, insert_df = loader._split_match_by_chunk(
        chunk, "Prod_Record_Id__c", {"a": "kavAAA000000000000", "c": "kavCCC000000000000"}
    )

    assert "Id" in update_df.columns
    assert sorted(update_df["Prod_Record_Id__c"].to_list()) == ["a", "c"]
    assert sorted(update_df["Id"].to_list()) == [
        "kavAAA000000000000",
        "kavCCC000000000000",
    ]
    assert len(insert_df) == 1
    assert insert_df["Prod_Record_Id__c"].to_list() == ["b"]
    assert "__matched_target_id__" not in insert_df.columns


# ---------------------------------------------------------------- #
#  End-to-end match-by chunk processing                             #
# ---------------------------------------------------------------- #


def _stub_describe_for_match_by(connector: _FakeConnector) -> None:
    connector.add_describe(
        "Knowledge__kav",
        [
            _make_field("Prod_Record_Id__c", external_id=False, unique=False),
            _make_field("Title", external_id=False, unique=False),
            _make_field("UrlName", external_id=False, unique=True),
        ],
    )


def _stub_bulk_jobs(loader: Loader) -> dict:
    """
    Replace _run_bulk_job with a recording stub that returns a fabricated
    success_df for inserts and an empty failed_df. Tracks every call so
    tests can assert on what got insert-ed vs update-ed.
    """
    captured: dict[str, list] = {"insert": [], "update": [], "calls": []}

    def fake_run(object_name, operation, external_id_field, payload):
        captured["calls"].append(
            {"object": object_name, "op": operation, "ext": external_id_field, "rows": len(payload)}
        )
        captured[operation].append(payload.clone())
        if operation == "insert":
            ids = [f"kavNEW{i:013d}" for i in range(len(payload))]
            success = pl.DataFrame(
                {
                    "sf__Id": ids,
                    "sf__Created": ["true"] * len(payload),
                    "Prod_Record_Id__c": payload.get_column("Prod_Record_Id__c").to_list()
                    if "Prod_Record_Id__c" in payload.columns
                    else [None] * len(payload),
                }
            )
            return success, pl.DataFrame()
        if operation == "update":
            ids = payload.get_column("Id").to_list() if "Id" in payload.columns else []
            success = pl.DataFrame({"sf__Id": ids, "sf__Created": ["false"] * len(ids)})
            return success, pl.DataFrame()
        return pl.DataFrame(), pl.DataFrame()

    loader._run_bulk_job = fake_run  # type: ignore[method-assign]
    return captured


def test_process_chunk_match_by_runs_both_update_and_insert():
    conn = _FakeConnector()
    _stub_describe_for_match_by(conn)
    conn.queue_query_results(
        [
            {"Id": "kavMATCH1111111111", "Prod_Record_Id__c": "src_a"},
        ]
    )
    loader = _make_loader(conn)
    captured = _stub_bulk_jobs(loader)

    chunk = pl.DataFrame(
        {
            "Prod_Record_Id__c": ["src_a", "src_b"],
            "Title": ["Existing", "Brand New"],
        }
    )

    result, failed_df, blocked_df = loader._process_chunk_match_by(
        "Knowledge__kav", chunk, "Prod_Record_Id__c", writable_fields=None, dry_run=False
    )

    assert result.success_count == 2
    assert result.failed_count == 0
    assert failed_df.is_empty()
    assert blocked_df.is_empty()

    ops = [c["op"] for c in captured["calls"]]
    assert "update" in ops and "insert" in ops

    update_payload = captured["update"][0]
    assert "Id" in update_payload.columns
    assert "Prod_Record_Id__c" not in update_payload.columns
    assert update_payload["Id"].to_list() == ["kavMATCH1111111111"]
    assert update_payload["Title"].to_list() == ["Existing"]

    insert_payload = captured["insert"][0]
    assert "Id" not in insert_payload.columns
    assert insert_payload["Prod_Record_Id__c"].to_list() == ["src_b"]


def test_process_chunk_match_by_populates_idmapper_for_updates_and_inserts():
    conn = _FakeConnector()
    _stub_describe_for_match_by(conn)
    conn.queue_query_results(
        [{"Id": "kavMATCH1111111111", "Prod_Record_Id__c": "src_a"}]
    )
    loader = _make_loader(conn)
    _stub_bulk_jobs(loader)

    chunk = pl.DataFrame(
        {
            "Prod_Record_Id__c": ["src_a", "src_b"],
            "Title": ["Existing", "Brand New"],
        }
    )

    loader._process_chunk_match_by(
        "Knowledge__kav", chunk, "Prod_Record_Id__c", writable_fields=None, dry_run=False
    )

    assert (
        loader.id_mapper.get_target_id("Knowledge__kav", "src_a")
        == "kavMATCH1111111111"
    ), "matched updates must populate IDMapper with the known target Id"
    new_target = loader.id_mapper.get_target_id("Knowledge__kav", "src_b")
    assert new_target is not None and new_target.startswith("kavNEW"), (
        "inserts must populate IDMapper from sf__Id"
    )


def test_process_chunk_match_by_all_match_runs_update_only():
    conn = _FakeConnector()
    _stub_describe_for_match_by(conn)
    conn.queue_query_results(
        [
            {"Id": "kavMATCH1111111111", "Prod_Record_Id__c": "src_a"},
            {"Id": "kavMATCH2222222222", "Prod_Record_Id__c": "src_b"},
        ]
    )
    loader = _make_loader(conn)
    captured = _stub_bulk_jobs(loader)

    chunk = pl.DataFrame(
        {"Prod_Record_Id__c": ["src_a", "src_b"], "Title": ["t1", "t2"]}
    )

    result, _, _ = loader._process_chunk_match_by(
        "Knowledge__kav", chunk, "Prod_Record_Id__c", writable_fields=None, dry_run=False
    )

    assert result.success_count == 2
    ops = [c["op"] for c in captured["calls"]]
    assert ops == ["update"], "no inserts should run when every row matches"


def test_process_chunk_match_by_no_matches_runs_insert_only():
    conn = _FakeConnector()
    _stub_describe_for_match_by(conn)
    conn.queue_query_results([])
    loader = _make_loader(conn)
    captured = _stub_bulk_jobs(loader)

    chunk = pl.DataFrame(
        {"Prod_Record_Id__c": ["src_a", "src_b"], "Title": ["t1", "t2"]}
    )

    result, _, _ = loader._process_chunk_match_by(
        "Knowledge__kav", chunk, "Prod_Record_Id__c", writable_fields=None, dry_run=False
    )

    assert result.success_count == 2
    ops = [c["op"] for c in captured["calls"]]
    assert ops == ["insert"], "no updates should run when nothing matches"


def test_process_chunk_match_by_dry_run_does_no_bulk_jobs():
    conn = _FakeConnector()
    _stub_describe_for_match_by(conn)
    conn.queue_query_results([])
    loader = _make_loader(conn)
    captured = _stub_bulk_jobs(loader)

    chunk = pl.DataFrame(
        {"Prod_Record_Id__c": ["src_a", "src_b"], "Title": ["t1", "t2"]}
    )

    result, failed_df, blocked_df = loader._process_chunk_match_by(
        "Knowledge__kav", chunk, "Prod_Record_Id__c", writable_fields=None, dry_run=True
    )

    assert result.success_count == 2
    assert captured["calls"] == [], "dry run must skip Bulk jobs entirely"
    assert failed_df.is_empty() and blocked_df.is_empty()


def test_process_chunk_match_by_raises_when_match_field_missing_from_chunk():
    conn = _FakeConnector()
    _stub_describe_for_match_by(conn)
    loader = _make_loader(conn)
    _stub_bulk_jobs(loader)

    chunk = pl.DataFrame({"Title": ["t1"]})

    raised = False
    try:
        loader._process_chunk_match_by(
            "Knowledge__kav", chunk, "Prod_Record_Id__c", writable_fields=None, dry_run=False
        )
    except Exception as e:
        raised = True
        assert "Prod_Record_Id__c" in str(e)
    assert raised, "missing match field column must raise LoadError"


# ---------------------------------------------------------------- #
#  Smoke: existing upsert path still routes correctly               #
# ---------------------------------------------------------------- #


def test_decide_strategy_describe_failure_defaults_to_upsert():
    """If we can't read describe metadata, prefer the legacy fast path so
    Salesforce surfaces any actual error rather than us silently switching."""

    class _BrokenConnector(_FakeConnector):
        def describe_object(self, object_name):
            raise RuntimeError("describe blew up")

    conn = _BrokenConnector()
    loader = _make_loader(conn)

    assert loader._decide_load_strategy("Account", "X__c", "upsert") == "upsert"


# ---------------------------------------------------------------- #
#  Per-operation field filtering (createable-only / updateable-only)#
# ---------------------------------------------------------------- #


def _stub_describe_with_asymmetric_fields(connector: _FakeConnector) -> None:
    """
    Models the Knowledge__kav reality: Language is createable but NOT
    updateable, and an imaginary 'PostInsertNotes__c' is updateable but NOT
    createable. The match key Prod_Record_Id__c is fully read/write.
    """
    connector.add_describe(
        "Knowledge__kav",
        [
            _make_field("Prod_Record_Id__c", external_id=False, unique=False),
            _make_field("Title"),
            _make_field("Language", createable=True, updateable=False),
            _make_field("PostInsertNotes__c", createable=False, updateable=True),
        ],
    )


def test_get_updateable_fields_excludes_createable_only():
    conn = _FakeConnector()
    _stub_describe_with_asymmetric_fields(conn)
    loader = _make_loader(conn)

    updateable = loader._get_updateable_fields("Knowledge__kav")

    assert updateable is not None
    assert "Title" in updateable
    assert "PostInsertNotes__c" in updateable
    assert "Language" not in updateable, (
        "createable-only fields must be excluded from updateable set"
    )


def test_get_createable_fields_excludes_updateable_only():
    conn = _FakeConnector()
    _stub_describe_with_asymmetric_fields(conn)
    loader = _make_loader(conn)

    createable = loader._get_createable_fields("Knowledge__kav")

    assert createable is not None
    assert "Title" in createable
    assert "Language" in createable
    assert "PostInsertNotes__c" not in createable, (
        "updateable-only fields must be excluded from createable set"
    )


def test_get_updateable_fields_returns_none_on_describe_failure():
    class _BrokenConnector(_FakeConnector):
        def describe_object(self, object_name):
            raise RuntimeError("describe blew up")

    loader = _make_loader(_BrokenConnector())
    assert loader._get_updateable_fields("Knowledge__kav") is None


def test_get_createable_fields_returns_none_on_describe_failure():
    class _BrokenConnector(_FakeConnector):
        def describe_object(self, object_name):
            raise RuntimeError("describe blew up")

    loader = _make_loader(_BrokenConnector())
    assert loader._get_createable_fields("Knowledge__kav") is None


def test_restrict_payload_columns_drops_disallowed_keeps_special_columns():
    conn = _FakeConnector()
    loader = _make_loader(conn)
    df = pl.DataFrame(
        {
            "Title": ["t"],
            "Language": ["en_US"],
            "Id": ["kav111111111111111"],
            "Account.Prod_Record_Id__c": ["src_acct"],
        }
    )

    out = loader._restrict_payload_columns(
        "Knowledge__kav",
        df,
        allowed_fields={"Title"},
        keep_columns={"Id"},
        operation_label="match-by update",
    )

    assert "Title" in out.columns
    assert "Id" in out.columns, "explicitly kept column must survive filter"
    assert "Account.Prod_Record_Id__c" in out.columns, (
        "relationship reference columns must always survive"
    )
    assert "Language" not in out.columns


def test_restrict_payload_columns_no_op_when_allowed_is_none():
    conn = _FakeConnector()
    loader = _make_loader(conn)
    df = pl.DataFrame({"A": [1], "B": [2]})

    out = loader._restrict_payload_columns(
        "Knowledge__kav", df, allowed_fields=None, keep_columns=set(), operation_label="x"
    )

    assert out.columns == df.columns, "None allowed_fields means fail-open: no filtering"


def test_match_by_update_payload_drops_createable_only_field():
    """
    Reproduces the user's INVALID_FIELD_FOR_INSERT_UPDATE on Language.
    The match-by update sub-payload must NOT contain Language even though
    the chunk carries it (Language is createable-only on Knowledge__kav).
    """
    conn = _FakeConnector()
    _stub_describe_with_asymmetric_fields(conn)
    conn.queue_query_results(
        [{"Id": "kavMATCH1111111111", "Prod_Record_Id__c": "src_a"}]
    )
    loader = _make_loader(conn)
    captured = _stub_bulk_jobs(loader)

    chunk = pl.DataFrame(
        {
            "Prod_Record_Id__c": ["src_a"],
            "Title": ["existing article"],
            "Language": ["en_US"],
            "PostInsertNotes__c": ["audit note"],
        }
    )

    loader._process_chunk_match_by(
        "Knowledge__kav", chunk, "Prod_Record_Id__c", writable_fields=None, dry_run=False
    )

    assert len(captured["update"]) == 1, "expected exactly one update sub-job"
    update_payload = captured["update"][0]

    assert "Language" not in update_payload.columns, (
        "Language is createable-only — must be dropped from update payload "
        "to avoid INVALID_FIELD_FOR_INSERT_UPDATE on Knowledge__kav"
    )
    assert "Title" in update_payload.columns
    assert "Id" in update_payload.columns, (
        "Id must always survive — it's the row key for Bulk update"
    )
    assert "PostInsertNotes__c" in update_payload.columns


def test_match_by_insert_payload_drops_updateable_only_field():
    """
    Mirror of the update-side test: insert sub-payloads must NOT contain
    fields that are updateable-only on target.
    """
    conn = _FakeConnector()
    _stub_describe_with_asymmetric_fields(conn)
    conn.queue_query_results([])
    loader = _make_loader(conn)
    captured = _stub_bulk_jobs(loader)

    chunk = pl.DataFrame(
        {
            "Prod_Record_Id__c": ["src_b"],
            "Title": ["brand new"],
            "Language": ["en_US"],
            "PostInsertNotes__c": ["should be dropped"],
        }
    )

    loader._process_chunk_match_by(
        "Knowledge__kav", chunk, "Prod_Record_Id__c", writable_fields=None, dry_run=False
    )

    assert len(captured["insert"]) == 1
    insert_payload = captured["insert"][0]

    assert "Language" in insert_payload.columns, (
        "Language is createable — must remain on insert payload"
    )
    assert "Title" in insert_payload.columns
    assert "Prod_Record_Id__c" in insert_payload.columns, (
        "external id field must remain on insert so target carries source Id"
    )
    assert "PostInsertNotes__c" not in insert_payload.columns, (
        "updateable-only fields must be dropped from insert payload"
    )


def test_resolve_relationship_column_uses_target_id_when_legacy_id_case_differs():
    conn = _FakeConnector()
    conn.add_describe(
        "Case",
        [
            _make_field(
                "AccountId",
                type="reference",
                relationshipName="Account",
                referenceTo=["Account"],
            ),
            _make_field("External_Id__c", external_id=True, unique=True),
        ],
    )
    conn.add_describe(
        "Account",
        [_make_field("External_Id__c", external_id=True, unique=True), _make_field("Id")],
    )
    original = "a5qS9000000krPVIAY"
    conn.queue_query_results(
        [{"Id": "001TARGETACCOUNTID", "External_Id__c": original}]
    )
    loader = _make_loader(conn)
    chunk = pl.DataFrame({
        "External_Id__c": ["500CASESOURCEIDXX"],
        "Account.External_Id__c": [original],
        "Subject": ["Lookup case"],
    })

    resolved = loader._resolve_relationship_reference_columns("Case", chunk)

    assert "AccountId" in resolved.columns
    assert resolved["AccountId"].to_list() == ["001TARGETACCOUNTID"]
    assert "Account.External_Id__c" not in resolved.columns
    assert any("External_Id__c" in query for query in conn.queries)


def test_resolve_relationship_column_matches_lowercase_target_legacy_id():
    conn = _FakeConnector()
    conn.add_describe(
        "Case",
        [
            _make_field(
                "AccountId",
                type="reference",
                relationshipName="Account",
                referenceTo=["Account"],
            ),
            _make_field("External_Id__c", external_id=True, unique=True),
        ],
    )
    conn.add_describe(
        "Account",
        [_make_field("External_Id__c", external_id=True, unique=True), _make_field("Id")],
    )
    original = "0011H000020uajxQAA"
    conn.queue_query_results(
        [{"Id": "001LOWERTARGETIDXX", "External_Id__c": original.lower()}]
    )
    loader = _make_loader(conn)
    resolved = loader._resolve_relationship_reference_columns(
        "Case",
        pl.DataFrame({"Account.External_Id__c": [original]}),
    )
    assert resolved["AccountId"].to_list() == ["001LOWERTARGETIDXX"]
    assert "Account.External_Id__c" not in resolved.columns


def test_resolve_polymorphic_owner_relationship_column_adds_typed_header():
    conn = _FakeConnector()
    conn.add_describe(
        "GE_Case_Notes__c",
        [
            _make_field(
                "OwnerId",
                type="reference",
                relationshipName="Owner",
                referenceTo=["Group", "User"],
            ),
            _make_field("External_Id__c", external_id=True, unique=True),
        ],
    )
    conn.add_describe(
        "User",
        [_make_field("External_Id__c", external_id=True, unique=True), _make_field("Id")],
    )
    legacy_user = "USER-LEGACY-42"
    conn.queue_query_results(
        [{"Id": "005TARGETUSERIDXXX", "External_Id__c": legacy_user}]
    )
    loader = _make_loader(conn)
    chunk = pl.DataFrame({
        "External_Id__c": ["NOTE-001"],
        "Owner.External_Id__c": [legacy_user],
        "OwnerId": ["005SHOULDNOTUPLOADXXX"],
        "Note__c": ["Sample note"],
    })

    resolved = loader._resolve_relationship_reference_columns("GE_Case_Notes__c", chunk)

    assert "User:Owner.External_Id__c" in resolved.columns
    assert resolved["User:Owner.External_Id__c"].to_list() == [legacy_user]
    assert "Owner.External_Id__c" not in resolved.columns
    assert "OwnerId" not in resolved.columns
    assert not conn.queries


def test_validate_payload_accepts_polymorphic_owner_relationship_reference():
    conn = _FakeConnector()
    conn.add_describe(
        "GE_Case_Notes__c",
        [
            _make_field(
                "OwnerId",
                type="reference",
                relationshipName="Owner",
                referenceTo=["Group", "User"],
            ),
            _make_field("External_Id__c", external_id=True, unique=True),
        ],
    )
    conn.add_describe(
        "User",
        [_make_field("External_Id__c", external_id=True, unique=True), _make_field("Id")],
    )
    loader = _make_loader(conn)
    chunk = pl.DataFrame({
        "External_Id__c": ["NOTE-001"],
        "User:Owner.External_Id__c": ["USER-LEGACY-42"],
    })

    loader._validate_payload_chunk_metadata(
        "GE_Case_Notes__c",
        chunk,
        "upsert",
        "External_Id__c",
    )


def test_resolve_load_batch_size_accepts_value_env_and_bounds():
    old = os.environ.pop("ETL_LOAD_BATCH_SIZE", None)
    try:
        assert resolve_load_batch_size(2000) == 2000
        assert resolve_load_batch_size("5,000") == 5000
        assert resolve_load_batch_size(None) == DEFAULT_LOAD_BATCH_SIZE
        os.environ["ETL_LOAD_BATCH_SIZE"] = "1500"
        assert resolve_load_batch_size(None) == 1500
        assert resolve_load_batch_size(MIN_LOAD_BATCH_SIZE) == MIN_LOAD_BATCH_SIZE
        assert resolve_load_batch_size(MAX_LOAD_BATCH_SIZE) == MAX_LOAD_BATCH_SIZE
        try:
            resolve_load_batch_size(MIN_LOAD_BATCH_SIZE - 1)
            raise AssertionError("expected ValueError for size below minimum")
        except ValueError:
            pass
        try:
            resolve_load_batch_size(MAX_LOAD_BATCH_SIZE + 1)
            raise AssertionError("expected ValueError for size above maximum")
        except ValueError:
            pass
        try:
            resolve_load_batch_size("abc")
            raise AssertionError("expected ValueError for non-numeric size")
        except ValueError:
            pass
    finally:
        if old is None:
            os.environ.pop("ETL_LOAD_BATCH_SIZE", None)
        else:
            os.environ["ETL_LOAD_BATCH_SIZE"] = old


if __name__ == "__main__":
    import sys
    sys.exit(run(globals()))
