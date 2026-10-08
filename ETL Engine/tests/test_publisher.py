"""
Unit tests for knowledge_article.publisher.KnowledgeArticlePublisher.

Uses a real DuckDB Stager + IDMapper plus a fake target connector that
captures every executeAnonymous call so tests can assert on the Apex
body, batching, retry behaviour, and reporting.
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
from knowledge_article.publisher import KnowledgeArticlePublisher


# ---------------------------------------------------------------- #
#  Test doubles                                                     #
# ---------------------------------------------------------------- #

class _FakeResponse:
    def __init__(self, *, json_body=None, text=""):
        self._json_body = json_body
        self.text = text

    def json(self):
        if self._json_body is None:
            raise ValueError("not json")
        return self._json_body


class _FakeConnector:
    """
    Captures every executeAnonymous call and returns either a default
    success or whatever response is queued via `respond_with`.
    """
    def __init__(self):
        self.instance_url = "https://target.my.salesforce.com"
        self.api_version = "v60.0"
        self.calls: list[dict] = []
        self._next_responses: list[_FakeResponse] = []
        self._success_response = _FakeResponse(
            json_body={"compiled": True, "success": True}
        )

    def respond_with(self, *responses: _FakeResponse) -> None:
        self._next_responses = list(responses)

    def _request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "params": kwargs.get("params") or {}})
        if self._next_responses:
            return self._next_responses.pop(0)
        return self._success_response


# ---------------------------------------------------------------- #
#  Helpers                                                          #
# ---------------------------------------------------------------- #

def _temp_path(suffix: str) -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.close()
    try:
        os.unlink(tmp.name)
    except OSError:
        pass
    return tmp.name


def _make_stager() -> Stager:
    return Stager(db_path=_temp_path(".duckdb"))


def _make_id_mapper() -> IDMapper:
    return IDMapper(db_path=_temp_path(".duckdb"))


def _build_ctx(stager, id_mapper, target_connector,
               extension_config=None) -> PipelineContext:
    return PipelineContext(
        pipeline_id="test_pipeline",
        object_name="Knowledge__kav",
        source_connector=None,  # type: ignore[arg-type]
        target_connector=target_connector,
        stager=stager,
        id_mapper=id_mapper,
        log_root=Path("."),
        extension_config=extension_config or {},
    )


def _seed_kav(stager, rows: list[dict]) -> None:
    schema = {col: pl.Utf8 for col in rows[0].keys()}
    df = pl.DataFrame(rows, schema=schema)
    stager.write_raw_chunk("Knowledge__kav", df)


KAV_SRC_A = "kA00X0000004ABCAA0"
KAV_SRC_B = "kA00X0000004BBCAA0"
KAV_SRC_C = "kA00X0000004CCCAA0"
KAV_TGT_A = "kA1T00000004ABCAAA"
KAV_TGT_B = "kA1T00000004BBCAAA"
KAV_TGT_C = "kA1T00000004CCCAAA"


# ---------------------------------------------------------------- #
#  Tests                                                            #
# ---------------------------------------------------------------- #

def test_publisher_skipped_when_explicitly_disabled():
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    try:
        _seed_kav(stager, [
            {"Id": KAV_SRC_A, "PublishStatus": "Online"},
        ])
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)
        ctx = _build_ctx(
            stager, id_mapper, _FakeConnector(),
            extension_config={"publishing": {"enabled": False}},
        )
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)
        publisher = KnowledgeArticlePublisher(ctx, cfg)
        report = publisher.run()
        assert report.attempted == 0
        assert ctx.target_connector.calls == []
    finally:
        stager.close(); id_mapper.close()


def test_publisher_default_enabled_with_no_staged_rows_is_noop():
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    try:
        ctx = _build_ctx(stager, id_mapper, _FakeConnector())
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)
        assert cfg.publishing.enabled is True
        publisher = KnowledgeArticlePublisher(ctx, cfg)
        report = publisher.run()
        assert report.attempted == 0
        assert ctx.target_connector.calls == []
    finally:
        stager.close(); id_mapper.close()


def test_publisher_publishes_online_articles_in_one_batch():
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    connector = _FakeConnector()
    try:
        _seed_kav(stager, [
            {"Id": KAV_SRC_A, "PublishStatus": "Online"},
            {"Id": KAV_SRC_B, "PublishStatus": "Online"},
        ])
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_B, KAV_TGT_B)

        ctx = _build_ctx(stager, id_mapper, connector, extension_config={
            "publishing": {"enabled": True},
        })
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        publisher = KnowledgeArticlePublisher(ctx, cfg)
        report = publisher.run()

        assert report.published == 2
        assert report.failed == 0
        assert report.batches == 1
        assert len(connector.calls) == 1

        apex_body = connector.calls[0]["params"]["anonymousBody"]
        assert KAV_TGT_A in apex_body
        assert KAV_TGT_B in apex_body
        assert "KbManagement.PublishingService.publishArticle" in apex_body
        # Default flag_as_new_version=false.
        assert "false" in apex_body
        # Each id should be wrapped in its own try/catch.
        assert apex_body.count("try {") == 2
    finally:
        stager.close(); id_mapper.close()


def test_publisher_filters_by_source_publish_status():
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    connector = _FakeConnector()
    try:
        _seed_kav(stager, [
            {"Id": KAV_SRC_A, "PublishStatus": "Online"},
            {"Id": KAV_SRC_B, "PublishStatus": "Draft"},
            {"Id": KAV_SRC_C, "PublishStatus": "Archived"},
        ])
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_B, KAV_TGT_B)
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_C, KAV_TGT_C)

        ctx = _build_ctx(stager, id_mapper, connector, extension_config={
            "publishing": {"enabled": True},
        })
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        publisher = KnowledgeArticlePublisher(ctx, cfg)
        report = publisher.run()
        assert report.candidates == 3
        assert report.skipped_status == 2  # Draft + Archived skipped
        assert report.attempted == 1
        assert report.published == 1

        apex_body = connector.calls[0]["params"]["anonymousBody"]
        assert KAV_TGT_A in apex_body
        assert KAV_TGT_B not in apex_body
        assert KAV_TGT_C not in apex_body
    finally:
        stager.close(); id_mapper.close()


def test_publisher_publishes_all_when_status_filter_disabled():
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    connector = _FakeConnector()
    try:
        _seed_kav(stager, [
            {"Id": KAV_SRC_A, "PublishStatus": "Online"},
            {"Id": KAV_SRC_B, "PublishStatus": "Draft"},
        ])
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_B, KAV_TGT_B)

        ctx = _build_ctx(stager, id_mapper, connector, extension_config={
            "publishing": {"enabled": True, "only_if_source_status": None},
        })
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        publisher = KnowledgeArticlePublisher(ctx, cfg)
        report = publisher.run()
        assert report.attempted == 2
        assert report.published == 2
    finally:
        stager.close(); id_mapper.close()


def test_publisher_skips_unmapped_source_rows():
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    connector = _FakeConnector()
    try:
        _seed_kav(stager, [
            {"Id": KAV_SRC_A, "PublishStatus": "Online"},
            {"Id": KAV_SRC_B, "PublishStatus": "Online"},
        ])
        # Only A is mapped — B has no IDMapper entry.
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)

        ctx = _build_ctx(stager, id_mapper, connector, extension_config={
            "publishing": {"enabled": True},
        })
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        publisher = KnowledgeArticlePublisher(ctx, cfg)
        report = publisher.run()
        assert report.candidates == 2
        assert report.skipped_unmapped == 1
        assert report.attempted == 1
        assert report.published == 1

        apex_body = connector.calls[0]["params"]["anonymousBody"]
        assert KAV_TGT_A in apex_body
        assert KAV_TGT_B not in apex_body
    finally:
        stager.close(); id_mapper.close()


def test_publisher_chunks_by_batch_size():
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    connector = _FakeConnector()
    try:
        rows = []
        for i in range(5):
            sid = f"kA00X00000ROW{i:03d}AA"
            tid = f"kA1T0X0000ROW{i:03d}AA"
            rows.append({"Id": sid, "PublishStatus": "Online"})
            id_mapper.add_mapping("Knowledge__kav", sid, tid)
        _seed_kav(stager, rows)

        ctx = _build_ctx(stager, id_mapper, connector, extension_config={
            "publishing": {"enabled": True, "batch_size": 2},
        })
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        publisher = KnowledgeArticlePublisher(ctx, cfg)
        report = publisher.run()
        # 5 ids, batch=2 → 3 batches (2,2,1)
        assert report.batches == 3
        assert report.published == 5
        assert len(connector.calls) == 3
    finally:
        stager.close(); id_mapper.close()


def test_publisher_retries_failed_batch_individually():
    """
    When a batch fails, retry each id alone. With retry mode on, only the
    truly bad article(s) end up in failed_ids.
    """
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    connector = _FakeConnector()
    try:
        _seed_kav(stager, [
            {"Id": KAV_SRC_A, "PublishStatus": "Online"},
            {"Id": KAV_SRC_B, "PublishStatus": "Online"},
        ])
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_B, KAV_TGT_B)

        # 1st call (batch of 2) fails, 2nd call (retry A) succeeds, 3rd call (retry B) fails.
        connector.respond_with(
            _FakeResponse(json_body={
                "compiled": True, "success": False,
                "exceptionMessage": "ARTICLE_NOT_FOUND",
            }),
            _FakeResponse(json_body={"compiled": True, "success": True}),
            _FakeResponse(json_body={
                "compiled": True, "success": False,
                "exceptionMessage": "ARTICLE_NOT_FOUND for second id",
            }),
        )

        ctx = _build_ctx(stager, id_mapper, connector, extension_config={
            "publishing": {"enabled": True, "retry_failed_batches_individually": True},
        })
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)
        publisher = KnowledgeArticlePublisher(ctx, cfg)
        report = publisher.run()

        assert report.attempted == 2
        assert report.published == 1
        assert report.failed == 1
        assert KAV_TGT_B in report.failed_ids
        assert len(connector.calls) == 3
    finally:
        stager.close(); id_mapper.close()


def test_publisher_retry_disabled_marks_whole_batch_failed():
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    connector = _FakeConnector()
    try:
        _seed_kav(stager, [
            {"Id": KAV_SRC_A, "PublishStatus": "Online"},
            {"Id": KAV_SRC_B, "PublishStatus": "Online"},
        ])
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_B, KAV_TGT_B)

        connector.respond_with(_FakeResponse(json_body={
            "compiled": False, "success": False,
            "compileProblem": "expecting class but found foo",
        }))

        ctx = _build_ctx(stager, id_mapper, connector, extension_config={
            "publishing": {
                "enabled": True,
                "retry_failed_batches_individually": False,
            },
        })
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)
        publisher = KnowledgeArticlePublisher(ctx, cfg)
        report = publisher.run()
        assert report.published == 0
        assert report.failed == 2
        assert sorted(report.failed_ids) == sorted([KAV_TGT_A, KAV_TGT_B])
        assert len(connector.calls) == 1
    finally:
        stager.close(); id_mapper.close()


def test_publisher_uses_flag_as_new_version_in_apex():
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    connector = _FakeConnector()
    try:
        _seed_kav(stager, [{"Id": KAV_SRC_A, "PublishStatus": "Online"}])
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)

        ctx = _build_ctx(stager, id_mapper, connector, extension_config={
            "publishing": {"enabled": True, "flag_as_new_version": True},
        })
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)
        KnowledgeArticlePublisher(ctx, cfg).run()
        apex = connector.calls[0]["params"]["anonymousBody"]
        assert f"publishArticle('{KAV_TGT_A}', true)" in apex
    finally:
        stager.close(); id_mapper.close()


def test_publisher_handles_missing_publishstatus_column():
    """If the staged table has no PublishStatus column, the filter must
    silently treat every row as a candidate."""
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    connector = _FakeConnector()
    try:
        # Note: no PublishStatus column at all.
        _seed_kav(stager, [{"Id": KAV_SRC_A, "Title": "x"}])
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)

        ctx = _build_ctx(stager, id_mapper, connector, extension_config={
            "publishing": {"enabled": True},
        })
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)
        report = KnowledgeArticlePublisher(ctx, cfg).run()
        # Filter is silently skipped — Online status assumed.
        assert report.attempted == 1
        assert report.published == 1
    finally:
        stager.close(); id_mapper.close()


def test_publisher_rejects_non_alphanumeric_ids():
    """Defensive: ids with non-alphanumeric characters should never be
    interpolated into Apex (injection guard)."""
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    connector = _FakeConnector()
    try:
        _seed_kav(stager, [
            {"Id": KAV_SRC_A, "PublishStatus": "Online"},
            {"Id": KAV_SRC_B, "PublishStatus": "Online"},
        ])
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)
        # B maps to an id with a quote in it — must be filtered out.
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_B, "kA1T'; DROP")

        ctx = _build_ctx(stager, id_mapper, connector, extension_config={
            "publishing": {"enabled": True},
        })
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)
        publisher = KnowledgeArticlePublisher(ctx, cfg)
        publisher.run()
        apex = connector.calls[0]["params"]["anonymousBody"]
        # The safe id is in the Apex body (twice — once in publishArticle, once
        # in the System.debug message), the unsafe one is filtered out entirely.
        assert KAV_TGT_A in apex
        assert "DROP" not in apex
        assert "kA1T'" not in apex  # no leftover unsafe-id fragment
    finally:
        stager.close(); id_mapper.close()


if __name__ == "__main__":
    import sys
    from tests._runner import run
    sys.exit(run(globals()))
