"""
Unit tests for knowledge_article.scope_expander.ScopeExpander.

Uses a real (in-process, file-backed temporary) DuckDB Stager and a
fake SFConnector + fake Extractor so the loop logic and SQL queries
are exercised end-to-end without touching Salesforce.

Run any of:
    python tests/test_scope_expander.py
    python -m pytest tests/test_scope_expander.py
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import polars as pl

from tests._runner import ensure_project_root_on_path
ensure_project_root_on_path()

from core.extension import PipelineContext
from core.stager import Stager
from knowledge_article.config import KnowledgeArticleConfig
from knowledge_article.scope_expander import ScopeExpander


# ---------------------------------------------------------------- #
#  Test doubles                                                     #
# ---------------------------------------------------------------- #

class _FakeConnector:
    """describe_global() is the only call ScopeExpander makes on the connector."""
    def __init__(self, sobjects):
        self._sobjects = sobjects

    def describe_global(self):
        return list(self._sobjects)


class _FakeExtractor:
    """
    Records every extract() call and writes new rows to the underlying
    Stager so the next expansion iteration sees them in raw staging.
    """
    def __init__(self, stager: Stager, supply_rows: dict):
        self.stager = stager
        self.supply_rows = supply_rows  # source_id -> row dict
        self.calls: list[tuple[str, str]] = []

    def extract(self, object_name: str, soql: str, scope_rules=None, overwrite: bool = True) -> int:
        self.calls.append((object_name, soql))
        ids = re.findall(r"'([a-zA-Z0-9]{15,18})'", soql)
        rows = []
        for sid in ids:
            row = self.supply_rows.get(sid)
            if row:
                rows.append(row)
        if not rows:
            return 0
        df = pl.DataFrame(rows, schema=_existing_schema(self.stager, object_name))
        self.stager.write_raw_chunk(object_name, df)
        return len(df)


def _existing_schema(stager: Stager, object_name: str) -> dict:
    cols = stager.get_column_names(object_name, "raw")
    return {col: pl.Utf8 for col in cols}


# ---------------------------------------------------------------- #
#  Helpers                                                          #
# ---------------------------------------------------------------- #

def _build_context(stager: Stager, source_connector, extension_config: dict | None = None) -> PipelineContext:
    return PipelineContext(
        pipeline_id="test_pipeline",
        object_name="Knowledge__kav",
        source_connector=source_connector,
        target_connector=None,  # type: ignore[arg-type]
        stager=stager,
        id_mapper=None,  # type: ignore[arg-type]
        log_root=Path("."),
        extension_config=extension_config or {},
    )


def _seed_kav_table(stager: Stager, rows: list[dict]) -> None:
    schema = {col: pl.Utf8 for col in rows[0].keys()}
    df = pl.DataFrame(rows, schema=schema)
    stager.write_raw_chunk("Knowledge__kav", df)


def _make_stager() -> Stager:
    tmp = tempfile.NamedTemporaryFile(suffix=".duckdb", delete=False)
    tmp.close()
    # DuckDB refuses to open a zero-byte file (it isn't a valid DB).
    # Remove the placeholder created by NamedTemporaryFile and let DuckDB create it.
    try:
        os.unlink(tmp.name)
    except OSError:
        pass
    return Stager(db_path=tmp.name)


# Realistic 18-char Salesforce IDs (kA0 prefix is the Knowledge__kav key prefix in Salesforce).
KAV_A = "kA00X0000004ABCAA0"
KAV_B = "kA00X0000004BBCAA0"
KAV_C = "kA00X0000004CBCAA0"
KAV_D = "kA00X0000004DBCAA0"
CASE_X = "500B0000003XYZZZZZ"


# ---------------------------------------------------------------- #
#  Tests                                                            #
# ---------------------------------------------------------------- #

def test_run_returns_zero_when_disabled():
    stager = _make_stager()
    try:
        connector = _FakeConnector([])
        ctx = _build_context(stager, connector, extension_config={
            "scope_expansion": {"enabled": False},
        })
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        expander = ScopeExpander(ctx, cfg, extractor_factory=lambda: _FakeExtractor(stager, {}))
        report = expander.run()

        assert report.depth_reached == 0
        assert report.added_count == 0
    finally:
        stager.close()


def test_run_with_no_staged_data_is_noop():
    stager = _make_stager()
    try:
        connector = _FakeConnector([{"name": "Knowledge__kav", "keyPrefix": "kA0"}])
        ctx = _build_context(stager, connector, extension_config={})
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        expander = ScopeExpander(ctx, cfg, extractor_factory=lambda: _FakeExtractor(stager, {}))
        report = expander.run()

        assert report.added_count == 0
        assert report.references_in_scope == 0
    finally:
        stager.close()


def test_run_extracts_referenced_kav_one_level_deep():
    stager = _make_stager()
    try:
        connector = _FakeConnector([
            {"name": "Knowledge__kav", "keyPrefix": "kA0"},
            {"name": "Case", "keyPrefix": "500"},
        ])
        # Seed: KAV_A links to KAV_B in its body
        _seed_kav_table(stager, [
            {"Id": KAV_A, "Article_Body__c": f'<a href="/lightning/r/Knowledge__kav/{KAV_B}/view">B</a>', "Summary": None},
        ])
        ctx = _build_context(stager, connector)
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        supply = {
            KAV_B: {"Id": KAV_B, "Article_Body__c": "B body", "Summary": None},
        }
        fake_ex = _FakeExtractor(stager, supply)

        expander = ScopeExpander(ctx, cfg, extractor_factory=lambda: fake_ex)
        report = expander.run()

        assert report.added_count == 1
        assert report.depth_reached == 1
        assert KAV_B in stager.get_distinct_values("Knowledge__kav", "Id", "raw")
        # Confirm SOQL targeted the right ID
        assert any(KAV_B in soql for _, soql in fake_ex.calls)
    finally:
        stager.close()


def test_run_walks_multiple_depths():
    stager = _make_stager()
    try:
        connector = _FakeConnector([{"name": "Knowledge__kav", "keyPrefix": "kA0"}])
        _seed_kav_table(stager, [
            {"Id": KAV_A, "Article_Body__c": f'<a href="/lightning/r/Knowledge__kav/{KAV_B}/view">B</a>', "Summary": None},
        ])
        ctx = _build_context(stager, connector)
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        # B references C, C references D
        supply = {
            KAV_B: {"Id": KAV_B, "Article_Body__c": f'<a href="/lightning/r/Knowledge__kav/{KAV_C}/view">C</a>', "Summary": None},
            KAV_C: {"Id": KAV_C, "Article_Body__c": f'<a href="/lightning/r/Knowledge__kav/{KAV_D}/view">D</a>', "Summary": None},
            KAV_D: {"Id": KAV_D, "Article_Body__c": "leaf", "Summary": None},
        }
        fake_ex = _FakeExtractor(stager, supply)

        expander = ScopeExpander(ctx, cfg, extractor_factory=lambda: fake_ex)
        report = expander.run()

        ids_after = set(stager.get_distinct_values("Knowledge__kav", "Id", "raw"))
        assert {KAV_A, KAV_B, KAV_C, KAV_D} <= ids_after
        assert report.added_count == 3
        assert report.depth_reached == 3
    finally:
        stager.close()


def test_run_respects_max_depth():
    stager = _make_stager()
    try:
        connector = _FakeConnector([{"name": "Knowledge__kav", "keyPrefix": "kA0"}])
        _seed_kav_table(stager, [
            {"Id": KAV_A, "Article_Body__c": f'<a href="/lightning/r/Knowledge__kav/{KAV_B}/view">B</a>', "Summary": None},
        ])
        ctx = _build_context(stager, connector, extension_config={
            "scope_expansion": {"max_depth": 1},
        })
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        supply = {
            KAV_B: {"Id": KAV_B, "Article_Body__c": f'<a href="/lightning/r/Knowledge__kav/{KAV_C}/view">C</a>', "Summary": None},
            KAV_C: {"Id": KAV_C, "Article_Body__c": "leaf", "Summary": None},
        }
        fake_ex = _FakeExtractor(stager, supply)

        expander = ScopeExpander(ctx, cfg, extractor_factory=lambda: fake_ex)
        report = expander.run()

        ids_after = set(stager.get_distinct_values("Knowledge__kav", "Id", "raw"))
        # B is fetched (depth=1), but C is NOT fetched (would require depth=2)
        assert KAV_B in ids_after
        assert KAV_C not in ids_after
        assert report.depth_reached == 1
    finally:
        stager.close()


def test_run_respects_max_added_records_cap():
    stager = _make_stager()
    try:
        connector = _FakeConnector([{"name": "Knowledge__kav", "keyPrefix": "kA0"}])
        # Seed with one body that links to two KAvs
        body = (
            f'<a href="/lightning/r/Knowledge__kav/{KAV_B}/view">B</a>'
            f'<a href="/lightning/r/Knowledge__kav/{KAV_C}/view">C</a>'
        )
        _seed_kav_table(stager, [
            {"Id": KAV_A, "Article_Body__c": body, "Summary": None},
        ])
        ctx = _build_context(stager, connector, extension_config={
            "scope_expansion": {"max_added_records": 1},
        })
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        supply = {
            KAV_B: {"Id": KAV_B, "Article_Body__c": "B leaf", "Summary": None},
            KAV_C: {"Id": KAV_C, "Article_Body__c": "C leaf", "Summary": None},
        }
        fake_ex = _FakeExtractor(stager, supply)

        expander = ScopeExpander(ctx, cfg, extractor_factory=lambda: fake_ex)
        report = expander.run()

        assert report.cap_reached is True
        assert report.added_count == 1
    finally:
        stager.close()


def test_run_classifies_out_of_scope_references():
    stager = _make_stager()
    try:
        connector = _FakeConnector([
            {"name": "Knowledge__kav", "keyPrefix": "kA0"},
            {"name": "Case", "keyPrefix": "500"},
        ])
        # Body links to both KAV_B (in scope) and CASE_X (out of scope)
        body = (
            f'<a href="/lightning/r/Knowledge__kav/{KAV_B}/view">B</a>'
            f'<a href="/lightning/r/Case/{CASE_X}/view">X</a>'
        )
        _seed_kav_table(stager, [
            {"Id": KAV_A, "Article_Body__c": body, "Summary": None},
        ])
        ctx = _build_context(stager, connector)
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        supply = {KAV_B: {"Id": KAV_B, "Article_Body__c": "leaf", "Summary": None}}
        fake_ex = _FakeExtractor(stager, supply)

        expander = ScopeExpander(ctx, cfg, extractor_factory=lambda: fake_ex)
        report = expander.run()

        assert report.references_in_scope >= 1
        assert report.references_out_of_scope >= 1
        # Case should NOT have been extracted
        assert all("Case" not in obj for obj, _ in fake_ex.calls)
    finally:
        stager.close()


def test_run_skips_already_in_scope_references():
    stager = _make_stager()
    try:
        connector = _FakeConnector([{"name": "Knowledge__kav", "keyPrefix": "kA0"}])
        # KAV_A's body links to KAV_B; both are already staged
        _seed_kav_table(stager, [
            {"Id": KAV_A, "Article_Body__c": f'<a href="/lightning/r/Knowledge__kav/{KAV_B}/view">B</a>', "Summary": None},
            {"Id": KAV_B, "Article_Body__c": "already here", "Summary": None},
        ])
        ctx = _build_context(stager, connector)
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        fake_ex = _FakeExtractor(stager, {})
        expander = ScopeExpander(ctx, cfg, extractor_factory=lambda: fake_ex)
        report = expander.run()

        assert report.added_count == 0
        assert fake_ex.calls == []
    finally:
        stager.close()


def test_supplementary_soql_does_not_add_publish_status():
    """Scope expansion fetches by Id only; business filters live in pipeline config."""
    stager = _make_stager()
    try:
        connector = _FakeConnector([{"name": "Knowledge__kav", "keyPrefix": "kA0"}])
        _seed_kav_table(stager, [
            {"Id": KAV_A, "Article_Body__c": f'<a href="/lightning/r/Knowledge__kav/{KAV_B}/view">B</a>', "Summary": None},
        ])
        ctx = _build_context(stager, connector)
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        supply = {KAV_B: {"Id": KAV_B, "Article_Body__c": "leaf", "Summary": None}}
        fake_ex = _FakeExtractor(stager, supply)

        expander = ScopeExpander(ctx, cfg, extractor_factory=lambda: fake_ex)
        expander.run()

        assert all("PublishStatus" not in soql for _, soql in fake_ex.calls)
        assert any("WHERE Id IN" in soql for _, soql in fake_ex.calls)
    finally:
        stager.close()


def test_classic_link_resolves_via_keyprefix():
    """A classic link without an object name relies on key-prefix lookup."""
    stager = _make_stager()
    try:
        connector = _FakeConnector([
            {"name": "Knowledge__kav", "keyPrefix": KAV_B[:3]},
            {"name": "Case", "keyPrefix": CASE_X[:3]},
        ])
        body = f'<a href="https://acme.lightning.force.com/{KAV_B}">B</a>'
        _seed_kav_table(stager, [
            {"Id": KAV_A, "Article_Body__c": body, "Summary": None},
        ])
        ctx = _build_context(stager, connector)
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        supply = {KAV_B: {"Id": KAV_B, "Article_Body__c": "leaf", "Summary": None}}
        fake_ex = _FakeExtractor(stager, supply)

        expander = ScopeExpander(ctx, cfg, extractor_factory=lambda: fake_ex)
        report = expander.run()

        assert report.added_count == 1
    finally:
        stager.close()


def test_only_richtext_columns_present_are_scanned():
    """If the staged table has only Summary (not Article_Body__c), config
    should still work — just scan what's there."""
    stager = _make_stager()
    try:
        connector = _FakeConnector([{"name": "Knowledge__kav", "keyPrefix": "kA0"}])
        # Only Summary column, no Article_Body__c
        df = pl.DataFrame(
            [{"Id": KAV_A, "Summary": f'<a href="/lightning/r/Knowledge__kav/{KAV_B}/view">B</a>'}],
            schema={"Id": pl.Utf8, "Summary": pl.Utf8},
        )
        stager.write_raw_chunk("Knowledge__kav", df)

        ctx = _build_context(stager, connector)
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        supply = {KAV_B: {"Id": KAV_B, "Summary": "leaf"}}
        fake_ex = _FakeExtractor(stager, supply)

        expander = ScopeExpander(ctx, cfg, extractor_factory=lambda: fake_ex)
        report = expander.run()

        assert report.added_count == 1
    finally:
        stager.close()


if __name__ == "__main__":
    import sys
    from tests._runner import run
    sys.exit(run(globals()))
