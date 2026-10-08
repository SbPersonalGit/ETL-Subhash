"""
Unit tests for knowledge_article.url_rewriter_runner.UrlRewriteRunner.

Uses a real (in-process, file-backed temporary) DuckDB Stager and IDMapper
plus a fake SFConnector (for describe_global) and a fake Loader so the
post_load rewrite path is exercised end-to-end without touching Salesforce.

Run any of:
    python tests/test_url_rewriter_runner.py
    python -m pytest tests/test_url_rewriter_runner.py
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import polars as pl

from tests._runner import ensure_project_root_on_path
ensure_project_root_on_path()

from core.extension import PipelineContext
from core.id_mapper import IDMapper
from core.stager import Stager
from knowledge_article.config import KnowledgeArticleConfig
from knowledge_article.richtext_rewriter import ImageRewrite
from knowledge_article.url_rewriter_runner import UrlRewriteRunner


# ---------------------------------------------------------------- #
#  Test doubles                                                     #
# ---------------------------------------------------------------- #

class _FakeConnector:
    """describe_global() is the only call we expect on the source connector."""
    def __init__(self, sobjects):
        self._sobjects = sobjects

    def describe_global(self):
        return list(self._sobjects)


class _FakeLoader:
    """Captures every update_records call so tests can assert on payload."""
    def __init__(self):
        self.update_calls: list[tuple[str, pl.DataFrame]] = []
        self.return_success_count: int | None = None

    def update_records(self, object_name: str, df: pl.DataFrame) -> int:
        self.update_calls.append((object_name, df.clone()))
        return self.return_success_count if self.return_success_count is not None else len(df)


# ---------------------------------------------------------------- #
#  Helpers                                                          #
# ---------------------------------------------------------------- #

def _make_stager() -> Stager:
    tmp = tempfile.NamedTemporaryFile(suffix=".duckdb", delete=False)
    tmp.close()
    try:
        os.unlink(tmp.name)
    except OSError:
        pass
    return Stager(db_path=tmp.name)


def _make_id_mapper() -> IDMapper:
    tmp = tempfile.NamedTemporaryFile(suffix=".duckdb", delete=False)
    tmp.close()
    try:
        os.unlink(tmp.name)
    except OSError:
        pass
    return IDMapper(db_path=tmp.name)


def _build_context(
    stager: Stager,
    id_mapper: IDMapper,
    source_connector,
    extension_config: dict | None = None,
) -> PipelineContext:
    return PipelineContext(
        pipeline_id="test_pipeline",
        object_name="Knowledge__kav",
        source_connector=source_connector,
        target_connector=source_connector,  # any object will do; runner only uses source for keyprefix
        stager=stager,
        id_mapper=id_mapper,
        log_root=Path("."),
        extension_config=extension_config or {},
    )


def _seed_kav_table(stager: Stager, rows: list[dict]) -> None:
    schema = {col: pl.Utf8 for col in rows[0].keys()}
    df = pl.DataFrame(rows, schema=schema)
    stager.write_raw_chunk("Knowledge__kav", df)


# Realistic 18-char Salesforce IDs.
KAV_SRC_A = "kA00X0000004ABCAA0"
KAV_SRC_B = "kA00X0000004BBCAA0"
KAV_TGT_A = "kA1T00000004ABCAA"
KAV_TGT_B = "kA1T00000004BBCAA"
CASE_SRC_X = "500B0000003XYZZZZZ"
CASE_TGT_X = "500T0000003XYZZZZZ"


# ---------------------------------------------------------------- #
#  Tests                                                            #
# ---------------------------------------------------------------- #

def test_run_returns_zero_when_disabled():
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    try:
        connector = _FakeConnector([])
        ctx = _build_context(stager, id_mapper, connector, extension_config={
            "url_rewriting": {"enabled": False},
        })
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        loader = _FakeLoader()
        runner = UrlRewriteRunner(ctx, cfg, loader_factory=lambda: loader)
        report = runner.run()

        assert report.records_updated == 0
        assert loader.update_calls == []
    finally:
        stager.close()
        id_mapper.close()


def test_run_with_no_staged_data_is_noop():
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    try:
        connector = _FakeConnector([])
        ctx = _build_context(stager, id_mapper, connector)
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        loader = _FakeLoader()
        runner = UrlRewriteRunner(ctx, cfg, loader_factory=lambda: loader)
        report = runner.run()

        assert report.records_updated == 0
        assert report.candidate_records == 0
        assert loader.update_calls == []
    finally:
        stager.close()
        id_mapper.close()


def test_run_skips_when_no_richtext_columns_present():
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    try:
        # Seed without any rich-text columns
        _seed_kav_table(stager, [{"Id": KAV_SRC_A, "Title": "no body"}])
        connector = _FakeConnector([])
        ctx = _build_context(stager, id_mapper, connector)
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        loader = _FakeLoader()
        runner = UrlRewriteRunner(ctx, cfg, loader_factory=lambda: loader)
        report = runner.run()

        assert report.records_updated == 0
        assert loader.update_calls == []
    finally:
        stager.close()
        id_mapper.close()


def test_run_rewrites_lightning_record_link_via_idmapper():
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    try:
        body = (
            f'<p>See <a href="https://src.lightning.force.com/lightning/r/'
            f'Knowledge__kav/{KAV_SRC_B}/view">B</a></p>'
        )
        _seed_kav_table(stager, [
            {"Id": KAV_SRC_A, "Article_Body__c": body, "Summary": None},
        ])
        # IDMapper: source -> target for both the row itself and the referenced KAv.
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_B, KAV_TGT_B)

        connector = _FakeConnector([])
        ctx = _build_context(stager, id_mapper, connector)
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        loader = _FakeLoader()
        runner = UrlRewriteRunner(ctx, cfg, loader_factory=lambda: loader)
        report = runner.run()

        assert len(loader.update_calls) == 1
        obj, df = loader.update_calls[0]
        assert obj == "Knowledge__kav"
        # Exactly one row, keyed by the target KAv id.
        assert df.shape == (1, 2)
        assert df["Id"].to_list() == [KAV_TGT_A]
        rewritten = df["Article_Body__c"].to_list()[0]
        # Reference KAv id was rewritten
        assert KAV_TGT_B in rewritten
        assert KAV_SRC_B not in rewritten
        # SF host was stripped (no target_host configured)
        assert "src.lightning.force.com" not in rewritten

        assert report.candidate_records == 1
        assert report.records_updated == 1
        assert report.records_unchanged == 0
        assert report.references_rewritten == 1
        assert report.references_missing == 0
    finally:
        stager.close()
        id_mapper.close()


def test_run_skips_records_without_id_mapping():
    """A staged source row whose KAv id isn't in IDMapper means the load
    failed (or hasn't happened yet). We must NOT include it in the update."""
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    try:
        _seed_kav_table(stager, [
            {"Id": KAV_SRC_A, "Article_Body__c": "<p>some body</p>", "Summary": None},
        ])
        # Note: no mapping for KAV_SRC_A.

        connector = _FakeConnector([])
        ctx = _build_context(stager, id_mapper, connector)
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        loader = _FakeLoader()
        runner = UrlRewriteRunner(ctx, cfg, loader_factory=lambda: loader)
        report = runner.run()

        assert loader.update_calls == []
        assert report.records_unmapped == 1
        assert report.records_updated == 0
    finally:
        stager.close()
        id_mapper.close()


def test_run_skips_records_with_unchanged_body():
    """A body with no record links and no SF hosts should not be updated."""
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    try:
        _seed_kav_table(stager, [
            {"Id": KAV_SRC_A, "Article_Body__c": "<p>plain text</p>", "Summary": None},
        ])
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)

        connector = _FakeConnector([])
        ctx = _build_context(stager, id_mapper, connector)
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        loader = _FakeLoader()
        runner = UrlRewriteRunner(ctx, cfg, loader_factory=lambda: loader)
        report = runner.run()

        assert loader.update_calls == []
        assert report.candidate_records == 1
        assert report.records_unchanged == 1
        assert report.records_updated == 0
    finally:
        stager.close()
        id_mapper.close()


def test_run_resolves_classic_link_via_keyprefix_index():
    """
    Classic / query-style links don't carry the object name in the URL,
    so the runner must consult the source connector's describe_global to
    find which object the prefix belongs to.
    """
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    try:
        body = f'<a href="https://src.salesforce.com/{CASE_SRC_X}">case</a>'
        _seed_kav_table(stager, [
            {"Id": KAV_SRC_A, "Article_Body__c": body, "Summary": None},
        ])
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)
        id_mapper.add_mapping("Case", CASE_SRC_X, CASE_TGT_X)

        connector = _FakeConnector([
            {"name": "Knowledge__kav", "keyPrefix": "kA0"},
            {"name": "Case", "keyPrefix": "500"},
        ])
        ctx = _build_context(stager, id_mapper, connector)
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        loader = _FakeLoader()
        runner = UrlRewriteRunner(ctx, cfg, loader_factory=lambda: loader)
        report = runner.run()

        assert len(loader.update_calls) == 1
        _, df = loader.update_calls[0]
        rewritten = df["Article_Body__c"].to_list()[0]
        assert CASE_TGT_X in rewritten
        assert CASE_SRC_X not in rewritten
        assert report.references_rewritten == 1
    finally:
        stager.close()
        id_mapper.close()


def test_run_keeps_broken_link_when_reference_missing_from_idmapper():
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    try:
        # B is referenced but not mapped (failed load / out of scope).
        body = (
            f'<a href="https://src.lightning.force.com/lightning/r/'
            f'Knowledge__kav/{KAV_SRC_B}/view">B</a>'
        )
        _seed_kav_table(stager, [
            {"Id": KAV_SRC_A, "Article_Body__c": body, "Summary": None},
        ])
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)

        connector = _FakeConnector([])
        ctx = _build_context(stager, id_mapper, connector)
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        loader = _FakeLoader()
        runner = UrlRewriteRunner(ctx, cfg, loader_factory=lambda: loader)
        report = runner.run()

        # The host gets stripped (URL becomes relative) but the source id
        # remains because keep_broken_link is the default policy.
        assert len(loader.update_calls) == 1
        _, df = loader.update_calls[0]
        rewritten = df["Article_Body__c"].to_list()[0]
        assert KAV_SRC_B in rewritten
        assert "src.lightning.force.com" not in rewritten
        assert report.references_missing == 1
        assert report.references_rewritten == 0
    finally:
        stager.close()
        id_mapper.close()


def test_run_replaces_sf_host_with_target_host_when_configured():
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    try:
        body = (
            f'<a href="https://src.lightning.force.com/lightning/r/'
            f'Knowledge__kav/{KAV_SRC_B}/view">B</a>'
        )
        _seed_kav_table(stager, [
            {"Id": KAV_SRC_A, "Article_Body__c": body, "Summary": None},
        ])
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_B, KAV_TGT_B)

        connector = _FakeConnector([])
        ctx = _build_context(stager, id_mapper, connector, extension_config={
            "target_host": "tgt.lightning.force.com",
        })
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        loader = _FakeLoader()
        runner = UrlRewriteRunner(ctx, cfg, loader_factory=lambda: loader)
        report = runner.run()

        _, df = loader.update_calls[0]
        rewritten = df["Article_Body__c"].to_list()[0]
        assert "tgt.lightning.force.com" in rewritten
        assert "src.lightning.force.com" not in rewritten
        assert report.hosts_rewritten >= 1
    finally:
        stager.close()
        id_mapper.close()


def test_run_preserves_inline_image_urls():
    """
    PR 3 must NOT touch <img src> rtaImage URLs — that's PR 4's job.
    The image host stays, and any record link in the same body still
    gets rewritten.
    """
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    try:
        body = (
            f'<p><a href="https://src.lightning.force.com/lightning/r/'
            f'Knowledge__kav/{KAV_SRC_B}/view">B</a></p>'
            f'<p><img src="https://src.file.force.com/servlet/rtaImage'
            f'?eid={KAV_SRC_A}&refid=R1"></p>'
        )
        _seed_kav_table(stager, [
            {"Id": KAV_SRC_A, "Article_Body__c": body, "Summary": None},
        ])
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_B, KAV_TGT_B)

        connector = _FakeConnector([])
        ctx = _build_context(stager, id_mapper, connector)
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        loader = _FakeLoader()
        runner = UrlRewriteRunner(ctx, cfg, loader_factory=lambda: loader)
        runner.run()

        _, df = loader.update_calls[0]
        rewritten = df["Article_Body__c"].to_list()[0]
        # Record link rewritten + outside host stripped.
        assert KAV_TGT_B in rewritten
        assert "src.lightning.force.com" not in rewritten
        # Image URL fully preserved (PR 4 owns image rehosting).
        assert "src.file.force.com" in rewritten
        assert "refid=R1" in rewritten
    finally:
        stager.close()
        id_mapper.close()


def test_run_writes_only_changed_columns_in_payload():
    """
    A record where only Article_Body__c changes should still produce a
    well-formed update payload — Summary should be present as a column
    so the DataFrame is rectangular but null for unchanged rows.
    """
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    try:
        # Two rows: one with a Body change, one with a Summary change.
        body_a = (
            f'<a href="https://src.lightning.force.com/lightning/r/'
            f'Knowledge__kav/{KAV_SRC_B}/view">B</a>'
        )
        summary_a = "<p>plain</p>"  # no change — won't be touched
        body_b = "<p>plain</p>"
        summary_b = (
            f'<a href="https://src.lightning.force.com/lightning/r/'
            f'Knowledge__kav/{KAV_SRC_A}/view">A</a>'
        )
        _seed_kav_table(stager, [
            {"Id": KAV_SRC_A, "Article_Body__c": body_a, "Summary": summary_a},
            {"Id": KAV_SRC_B, "Article_Body__c": body_b, "Summary": summary_b},
        ])
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_B, KAV_TGT_B)

        connector = _FakeConnector([])
        ctx = _build_context(stager, id_mapper, connector)
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        loader = _FakeLoader()
        runner = UrlRewriteRunner(ctx, cfg, loader_factory=lambda: loader)
        runner.run()

        _, df = loader.update_calls[0]
        # 2 rows updated.
        assert len(df) == 2
        assert sorted(df["Id"].to_list()) == sorted([KAV_TGT_A, KAV_TGT_B])
        # Both columns present (Article_Body__c and Summary), each row's
        # untouched column is null.
        assert "Article_Body__c" in df.columns
        assert "Summary" in df.columns
        a_row = df.filter(pl.col("Id") == KAV_TGT_A).to_dicts()[0]
        b_row = df.filter(pl.col("Id") == KAV_TGT_B).to_dicts()[0]
        assert a_row["Article_Body__c"] is not None
        assert a_row["Summary"] is None
        assert b_row["Article_Body__c"] is None
        assert b_row["Summary"] is not None
    finally:
        stager.close()
        id_mapper.close()


def test_run_chunks_large_payload():
    """Bulk update payloads should be split into chunks of <= 1000 rows."""
    from knowledge_article import url_rewriter_runner as runner_module

    original = runner_module._UPDATE_CHUNK_SIZE
    runner_module._UPDATE_CHUNK_SIZE = 2  # force chunking with a tiny limit
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    try:
        rows = []
        for i in range(5):
            sid = f"kA00X00000ROW{i:03d}AA"  # 18 chars (kA0 + 12 chars + AA)
            tid = f"kA1T0X0000ROW{i:03d}AA"
            body = (
                f'<a href="https://src.lightning.force.com/lightning/r/'
                f'Knowledge__kav/{KAV_SRC_B}/view">B</a>'
            )
            rows.append({"Id": sid, "Article_Body__c": body, "Summary": None})
            id_mapper.add_mapping("Knowledge__kav", sid, tid)
        # Map B too so the rewrite has work to do.
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_B, KAV_TGT_B)
        _seed_kav_table(stager, rows)

        connector = _FakeConnector([])
        ctx = _build_context(stager, id_mapper, connector)
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        loader = _FakeLoader()
        runner = UrlRewriteRunner(ctx, cfg, loader_factory=lambda: loader)
        runner.run()

        # 5 rows / chunk size 2 -> 3 chunks.
        assert len(loader.update_calls) == 3
        assert sum(len(df) for _, df in loader.update_calls) == 5
    finally:
        runner_module._UPDATE_CHUNK_SIZE = original
        stager.close()
        id_mapper.close()


def test_run_only_richtext_columns_present_are_scanned():
    """
    Configured richtext_fields may exceed what the staged table actually
    has (e.g. a template names extra rich-text fields not in source).
    The runner should silently use only the intersection.
    """
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    try:
        body = (
            f'<a href="https://src.lightning.force.com/lightning/r/'
            f'Knowledge__kav/{KAV_SRC_B}/view">B</a>'
        )
        _seed_kav_table(stager, [
            # Note: only Article_Body__c is staged — no Summary column.
            {"Id": KAV_SRC_A, "Article_Body__c": body},
        ])
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_B, KAV_TGT_B)

        connector = _FakeConnector([])
        # Configured fields name both columns even though Summary doesn't exist.
        ctx = _build_context(stager, id_mapper, connector, extension_config={
            "richtext_fields": ["Article_Body__c", "Summary"],
        })
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        loader = _FakeLoader()
        runner = UrlRewriteRunner(ctx, cfg, loader_factory=lambda: loader)
        report = runner.run()

        assert len(loader.update_calls) == 1
        assert report.records_updated == 1
    finally:
        stager.close()
        id_mapper.close()


# ---------------------------------------------------------------- #
#  PR 4: image_map_by_source_kav integration                        #
# ---------------------------------------------------------------- #

def test_run_with_image_map_rewrites_img_urls_to_target():
    """
    When the caller supplies an image_map (PR 4 path), the runner must
    flip preserve_image_urls off and replace each <img src> with the
    target's eid/feoid/refid.
    """
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    try:
        body = (
            f'<p><img src="https://src.file.force.com/sfc/servlet/rtaImage'
            f'?eid={KAV_SRC_A}&feoid=00N1S00000SRC&refid=SRC_REF_1"></p>'
        )
        _seed_kav_table(stager, [
            {"Id": KAV_SRC_A, "Article_Body__c": body, "Summary": None},
        ])
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)

        connector = _FakeConnector([])
        ctx = _build_context(stager, id_mapper, connector)
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        image_map = {
            KAV_SRC_A: {
                "SRC_REF_1": ImageRewrite(
                    target_eid=KAV_TGT_A,
                    target_feoid="00N1T00000TGT",
                    target_refid="TGT_REF_1",
                )
            }
        }
        loader = _FakeLoader()
        runner = UrlRewriteRunner(
            ctx, cfg,
            loader_factory=lambda: loader,
            image_map_by_source_kav=image_map,
        )
        report = runner.run()

        _, df = loader.update_calls[0]
        rewritten = df["Article_Body__c"].to_list()[0]
        # Image src now points to a host-relative target URL.
        assert "TGT_REF_1" in rewritten
        assert f"eid={KAV_TGT_A}" in rewritten
        assert "feoid=00N1T00000TGT" in rewritten
        # Source refid is gone.
        assert "SRC_REF_1" not in rewritten
        # Source host is gone (host rewriter ran outside <img> too).
        assert "src.file.force.com" not in rewritten
        assert report.images_rewritten == 1
        assert report.images_missing == 0
    finally:
        stager.close()
        id_mapper.close()


def test_run_with_image_map_uses_per_article_keys():
    """The runner must look up image_map per source kav id — image entries
    for one article must NOT bleed into another article's body."""
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    try:
        body_a = (
            f'<p><img src="https://src.file.force.com/sfc/servlet/rtaImage'
            f'?eid={KAV_SRC_A}&refid=REF_FOR_A"></p>'
        )
        body_b = (
            f'<p><img src="https://src.file.force.com/sfc/servlet/rtaImage'
            f'?eid={KAV_SRC_B}&refid=REF_FOR_B"></p>'
        )
        _seed_kav_table(stager, [
            {"Id": KAV_SRC_A, "Article_Body__c": body_a, "Summary": None},
            {"Id": KAV_SRC_B, "Article_Body__c": body_b, "Summary": None},
        ])
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_B, KAV_TGT_B)

        connector = _FakeConnector([])
        ctx = _build_context(stager, id_mapper, connector)
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        image_map = {
            KAV_SRC_A: {
                "REF_FOR_A": ImageRewrite(
                    target_eid=KAV_TGT_A,
                    target_feoid="00N",
                    target_refid="TGT_A",
                )
            },
            KAV_SRC_B: {
                "REF_FOR_B": ImageRewrite(
                    target_eid=KAV_TGT_B,
                    target_feoid="00N",
                    target_refid="TGT_B",
                )
            },
        }
        loader = _FakeLoader()
        runner = UrlRewriteRunner(
            ctx, cfg,
            loader_factory=lambda: loader,
            image_map_by_source_kav=image_map,
        )
        report = runner.run()

        _, df = loader.update_calls[0]
        rows_by_id = {r["Id"]: r for r in df.to_dicts()}
        a_rewritten = rows_by_id[KAV_TGT_A]["Article_Body__c"]
        b_rewritten = rows_by_id[KAV_TGT_B]["Article_Body__c"]
        assert "TGT_A" in a_rewritten
        assert "TGT_B" not in a_rewritten
        assert "TGT_B" in b_rewritten
        assert "TGT_A" not in b_rewritten
        assert report.images_rewritten == 2
    finally:
        stager.close()
        id_mapper.close()


def test_run_with_image_map_replaces_missing_image_with_placeholder():
    """When image_handling.on_missing_image=replace_with_placeholder, an
    <img> in the body without a corresponding image_map entry must be
    swapped for the configured placeholder HTML."""
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    try:
        body = (
            f'<p><img src="https://src.file.force.com/sfc/servlet/rtaImage'
            f'?eid={KAV_SRC_A}&refid=ORPHAN_REF"></p>'
        )
        _seed_kav_table(stager, [
            {"Id": KAV_SRC_A, "Article_Body__c": body, "Summary": None},
        ])
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)

        connector = _FakeConnector([])
        ctx = _build_context(stager, id_mapper, connector, extension_config={
            "image_handling": {
                "enabled": True,
                "on_missing_image": "replace_with_placeholder",
                "placeholder_html": "<span class=\"missing\"></span>",
            },
        })
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        # image_map provided but does NOT contain ORPHAN_REF, so the
        # rewriter falls back to the configured policy.
        loader = _FakeLoader()
        runner = UrlRewriteRunner(
            ctx, cfg,
            loader_factory=lambda: loader,
            image_map_by_source_kav={KAV_SRC_A: {}},
        )
        report = runner.run()

        _, df = loader.update_calls[0]
        rewritten = df["Article_Body__c"].to_list()[0]
        assert '<span class="missing"></span>' in rewritten
        assert "<img" not in rewritten
        assert report.images_missing == 1
    finally:
        stager.close()
        id_mapper.close()


if __name__ == "__main__":
    import sys
    from tests._runner import run
    sys.exit(run(globals()))
