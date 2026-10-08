"""
Unit tests for knowledge_article.image_rehoster.ImageRehoster.

Uses a real DuckDB Stager + IDMapper plus a fake SFConnector and fake
download/upload/feoid functions so the loop is exercised end-to-end
without touching Salesforce or hitting the network.

Run any of:
    python tests/test_image_rehoster.py
    python -m pytest tests/test_image_rehoster.py
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
from knowledge_article._image_api import UploadResult
from knowledge_article.config import KnowledgeArticleConfig
from knowledge_article.image_manifest import ImageManifest
from knowledge_article.image_rehoster import ImageRehoster


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


def _make_manifest() -> ImageManifest:
    return ImageManifest(path=_temp_path(".sqlite"))


def _build_ctx(stager, id_mapper, source_connector, target_connector,
               extension_config=None) -> PipelineContext:
    return PipelineContext(
        pipeline_id="test_pipeline",
        object_name="Knowledge__kav",
        source_connector=source_connector,
        target_connector=target_connector,
        stager=stager,
        id_mapper=id_mapper,
        log_root=Path("."),
        extension_config=extension_config or {},
    )


def _seed_kav_table(stager, rows: list[dict]) -> None:
    schema = {col: pl.Utf8 for col in rows[0].keys()}
    df = pl.DataFrame(rows, schema=schema)
    stager.write_raw_chunk("Knowledge__kav", df)


KAV_SRC_A = "kA00X0000004ABCAA0"
KAV_SRC_B = "kA00X0000004BBCAA0"
KAV_TGT_A = "kA1T00000004ABCAAA"
KAV_TGT_B = "kA1T00000004BBCAAA"


def _img_url(eid: str, refid: str) -> str:
    return (
        f"https://src.file.force.com/sfc/servlet/rtaImage"
        f"?eid={eid}&feoid=00N1S00000SRC&refid={refid}"
    )


def _img_html(eid: str, refid: str) -> str:
    return f'<p><img src="{_img_url(eid, refid)}"></p>'


# ---------------------------------------------------------------- #
#  Fakes                                                            #
# ---------------------------------------------------------------- #

class _FakeConnector:
    pass


def _build_fakes(*, image_bytes_by_url=None, upload_responses=None, feoid_value="00N1T00000TARGET"):
    """
    Returns (download_fn, upload_fn, feoid_fn, capture) where capture is
    a dict tracking calls.

    image_bytes_by_url: url -> bytes (or Exception)
    upload_responses: list[UploadResult or Exception], consumed FIFO
    """
    capture = {"downloads": [], "uploads": [], "feoid_lookups": []}
    image_bytes_by_url = image_bytes_by_url or {}
    upload_responses = list(upload_responses or [])

    def download_fn(connector, url):
        capture["downloads"].append(url)
        if url not in image_bytes_by_url:
            raise RuntimeError(f"unexpected download URL: {url}")
        result = image_bytes_by_url[url]
        if isinstance(result, Exception):
            raise result
        return result

    def upload_fn(connector, *, object_name, target_kav_id, target_field, content, filename=None, content_type=None):
        capture["uploads"].append({
            "object_name": object_name,
            "target_kav_id": target_kav_id,
            "target_field": target_field,
            "content_len": len(content),
        })
        if not upload_responses:
            return UploadResult(
                target_refid=f"REF_{len(capture['uploads'])}",
                target_field=target_field,
                target_kav_id=target_kav_id,
            )
        nxt = upload_responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def feoid_fn(connector, *, object_name, field_name):
        capture["feoid_lookups"].append((object_name, field_name))
        return feoid_value

    return download_fn, upload_fn, feoid_fn, capture


# ---------------------------------------------------------------- #
#  Tests                                                            #
# ---------------------------------------------------------------- #

def test_run_disabled_via_config_is_noop():
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    manifest = _make_manifest()
    try:
        ctx = _build_ctx(stager, id_mapper, _FakeConnector(), _FakeConnector(),
                         extension_config={"image_handling": {"enabled": False}})
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)
        download, upload, feoid, capture = _build_fakes()
        rehoster = ImageRehoster(ctx, cfg, manifest=manifest,
                                 download_fn=download, upload_fn=upload, feoid_fn=feoid)
        result = rehoster.run()

        assert result.report.images_seen == 0
        assert result.image_map_by_source_kav == {}
        assert capture["downloads"] == []
    finally:
        stager.close(); id_mapper.close(); manifest.close()


def test_run_with_no_richtext_columns_is_noop():
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    manifest = _make_manifest()
    try:
        ctx = _build_ctx(stager, id_mapper, _FakeConnector(), _FakeConnector())
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        # Seed only an Id column — no rich-text columns.
        _seed_kav_table(stager, [{"Id": KAV_SRC_A, "Title": "x"}])

        download, upload, feoid, capture = _build_fakes()
        rehoster = ImageRehoster(ctx, cfg, manifest=manifest,
                                 download_fn=download, upload_fn=upload, feoid_fn=feoid)
        result = rehoster.run()
        assert result.report.images_seen == 0
        assert capture["downloads"] == []
    finally:
        stager.close(); id_mapper.close(); manifest.close()


def test_run_uploads_new_image_and_records_manifest():
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    manifest = _make_manifest()
    try:
        body = _img_html(KAV_SRC_A, "REFID_SRC_1")
        _seed_kav_table(stager, [{"Id": KAV_SRC_A, "Article_Body__c": body}])
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)

        ctx = _build_ctx(stager, id_mapper, _FakeConnector(), _FakeConnector())
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        download, upload, feoid, capture = _build_fakes(
            image_bytes_by_url={_img_url(KAV_SRC_A, "REFID_SRC_1"): b"binary-png-data"},
            upload_responses=[UploadResult(
                target_refid="REFID_TGT_1",
                target_field="Article_Body__c",
                target_kav_id=KAV_TGT_A,
            )],
        )

        rehoster = ImageRehoster(ctx, cfg, manifest=manifest,
                                 download_fn=download, upload_fn=upload, feoid_fn=feoid)
        result = rehoster.run()

        assert result.report.images_seen == 1
        assert result.report.images_uploaded == 1
        assert result.report.images_cached == 0

        # The image_map is keyed by source_kav -> {source_refid -> ImageRewrite}.
        kav_map = result.image_map_by_source_kav[KAV_SRC_A]
        rewrite = kav_map["REFID_SRC_1"]
        assert rewrite.target_eid == KAV_TGT_A
        assert rewrite.target_refid == "REFID_TGT_1"
        assert rewrite.target_feoid == "00N1T00000TARGET"

        # And the manifest now caches the upload.
        cached = manifest.lookup(
            target_kav_id=KAV_TGT_A,
            target_field="Article_Body__c",
            content_hash=_sha256_of(b"binary-png-data"),
        )
        assert cached is not None
        assert cached.target_refid == "REFID_TGT_1"
    finally:
        stager.close(); id_mapper.close(); manifest.close()


def test_run_reuses_manifest_hit_and_skips_upload():
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    manifest = _make_manifest()
    try:
        body = _img_html(KAV_SRC_A, "REFID_SRC_1")
        _seed_kav_table(stager, [{"Id": KAV_SRC_A, "Article_Body__c": body}])
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)

        # Pre-seed the manifest as if a prior run uploaded these bytes.
        manifest.record(
            target_kav_id=KAV_TGT_A,
            target_field="Article_Body__c",
            content_hash=_sha256_of(b"cached-bytes"),
            target_eid=KAV_TGT_A,
            target_feoid="00N1T00000CACHED",
            target_refid="REFID_CACHED",
        )

        ctx = _build_ctx(stager, id_mapper, _FakeConnector(), _FakeConnector())
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        download, upload, feoid, capture = _build_fakes(
            image_bytes_by_url={_img_url(KAV_SRC_A, "REFID_SRC_1"): b"cached-bytes"},
        )
        rehoster = ImageRehoster(ctx, cfg, manifest=manifest,
                                 download_fn=download, upload_fn=upload, feoid_fn=feoid)
        result = rehoster.run()

        assert result.report.images_uploaded == 0
        assert result.report.images_cached == 1
        assert capture["uploads"] == []  # short-circuited

        rewrite = result.image_map_by_source_kav[KAV_SRC_A]["REFID_SRC_1"]
        assert rewrite.target_refid == "REFID_CACHED"
        assert rewrite.target_feoid == "00N1T00000CACHED"
    finally:
        stager.close(); id_mapper.close(); manifest.close()


def test_run_skips_image_when_owning_kav_unmapped():
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    manifest = _make_manifest()
    try:
        body = _img_html(KAV_SRC_A, "REFID_SRC_1")
        _seed_kav_table(stager, [{"Id": KAV_SRC_A, "Article_Body__c": body}])
        # NOTE: no IDMapper entry for KAV_SRC_A.

        ctx = _build_ctx(stager, id_mapper, _FakeConnector(), _FakeConnector())
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        download, upload, feoid, capture = _build_fakes(
            image_bytes_by_url={_img_url(KAV_SRC_A, "REFID_SRC_1"): b"x"},
        )
        rehoster = ImageRehoster(ctx, cfg, manifest=manifest,
                                 download_fn=download, upload_fn=upload, feoid_fn=feoid)
        result = rehoster.run()

        assert result.report.images_seen == 1
        assert result.report.images_skipped_unmapped == 1
        assert result.report.images_uploaded == 0
        assert capture["downloads"] == []  # never even downloaded
        assert result.image_map_by_source_kav == {}
    finally:
        stager.close(); id_mapper.close(); manifest.close()


def test_run_isolates_per_image_failures():
    """One image fails to download — others still go through."""
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    manifest = _make_manifest()
    try:
        body_a = _img_html(KAV_SRC_A, "REF1") + _img_html(KAV_SRC_A, "REF2")
        _seed_kav_table(stager, [{"Id": KAV_SRC_A, "Article_Body__c": body_a}])
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)

        ctx = _build_ctx(stager, id_mapper, _FakeConnector(), _FakeConnector())
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        download, upload, feoid, capture = _build_fakes(
            image_bytes_by_url={
                _img_url(KAV_SRC_A, "REF1"): RuntimeError("404"),
                _img_url(KAV_SRC_A, "REF2"): b"hello",
            },
            upload_responses=[
                UploadResult(target_refid="OK_2", target_field="Article_Body__c", target_kav_id=KAV_TGT_A),
            ],
        )
        rehoster = ImageRehoster(ctx, cfg, manifest=manifest,
                                 download_fn=download, upload_fn=upload, feoid_fn=feoid)
        result = rehoster.run()

        assert result.report.images_seen == 2
        assert result.report.images_failed == 1
        assert result.report.images_uploaded == 1
        # Only the successful image lands in the map.
        assert "REF1" not in result.image_map_by_source_kav.get(KAV_SRC_A, {})
        assert result.image_map_by_source_kav[KAV_SRC_A]["REF2"].target_refid == "OK_2"
    finally:
        stager.close(); id_mapper.close(); manifest.close()


def test_run_groups_image_map_by_source_kav():
    """Two source articles each with their own image — image_map keys must
    reflect the owning source kav."""
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    manifest = _make_manifest()
    try:
        _seed_kav_table(stager, [
            {"Id": KAV_SRC_A, "Article_Body__c": _img_html(KAV_SRC_A, "RA")},
            {"Id": KAV_SRC_B, "Article_Body__c": _img_html(KAV_SRC_B, "RB")},
        ])
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_B, KAV_TGT_B)

        ctx = _build_ctx(stager, id_mapper, _FakeConnector(), _FakeConnector())
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        download, upload, feoid, capture = _build_fakes(
            image_bytes_by_url={
                _img_url(KAV_SRC_A, "RA"): b"A",
                _img_url(KAV_SRC_B, "RB"): b"B",
            },
            upload_responses=[
                UploadResult(target_refid="TA", target_field="Article_Body__c", target_kav_id=KAV_TGT_A),
                UploadResult(target_refid="TB", target_field="Article_Body__c", target_kav_id=KAV_TGT_B),
            ],
        )
        rehoster = ImageRehoster(ctx, cfg, manifest=manifest,
                                 download_fn=download, upload_fn=upload, feoid_fn=feoid)
        result = rehoster.run()

        assert set(result.image_map_by_source_kav.keys()) == {KAV_SRC_A, KAV_SRC_B}
        assert result.image_map_by_source_kav[KAV_SRC_A]["RA"].target_eid == KAV_TGT_A
        assert result.image_map_by_source_kav[KAV_SRC_B]["RB"].target_eid == KAV_TGT_B
        # Two different target articles, two uploads.
        assert len(capture["uploads"]) == 2
        assert capture["uploads"][0]["target_kav_id"] in (KAV_TGT_A, KAV_TGT_B)
    finally:
        stager.close(); id_mapper.close(); manifest.close()


def test_run_dedupes_repeated_image_within_same_article():
    """The same refid in the same body should only kick off one upload job."""
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    manifest = _make_manifest()
    try:
        body = _img_html(KAV_SRC_A, "DUP") + _img_html(KAV_SRC_A, "DUP")
        _seed_kav_table(stager, [{"Id": KAV_SRC_A, "Article_Body__c": body}])
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)

        ctx = _build_ctx(stager, id_mapper, _FakeConnector(), _FakeConnector())
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        download, upload, feoid, capture = _build_fakes(
            image_bytes_by_url={_img_url(KAV_SRC_A, "DUP"): b"x"},
        )
        rehoster = ImageRehoster(ctx, cfg, manifest=manifest,
                                 download_fn=download, upload_fn=upload, feoid_fn=feoid)
        result = rehoster.run()
        assert result.report.images_seen == 1  # deduped at scan time
        assert len(capture["uploads"]) == 1
    finally:
        stager.close(); id_mapper.close(); manifest.close()


def test_run_resolves_feoid_once_per_field():
    """Multiple uploads to the same field should hit feoid_fn only once."""
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    manifest = _make_manifest()
    try:
        _seed_kav_table(stager, [
            {"Id": KAV_SRC_A, "Article_Body__c":
                _img_html(KAV_SRC_A, "R1") + _img_html(KAV_SRC_A, "R2")},
        ])
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)

        ctx = _build_ctx(stager, id_mapper, _FakeConnector(), _FakeConnector())
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        download, upload, feoid, capture = _build_fakes(
            image_bytes_by_url={
                _img_url(KAV_SRC_A, "R1"): b"a",
                _img_url(KAV_SRC_A, "R2"): b"b",
            },
        )
        rehoster = ImageRehoster(ctx, cfg, manifest=manifest,
                                 download_fn=download, upload_fn=upload, feoid_fn=feoid)
        rehoster.run()
        assert len(capture["feoid_lookups"]) == 1
        assert capture["feoid_lookups"][0] == ("Knowledge__kav", "Article_Body__c")
    finally:
        stager.close(); id_mapper.close(); manifest.close()


def test_run_records_feoid_miss_when_resolution_returns_none():
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    manifest = _make_manifest()
    try:
        body = _img_html(KAV_SRC_A, "REFID_SRC_1")
        _seed_kav_table(stager, [{"Id": KAV_SRC_A, "Article_Body__c": body}])
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)

        ctx = _build_ctx(stager, id_mapper, _FakeConnector(), _FakeConnector())
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        download, upload, feoid, capture = _build_fakes(
            image_bytes_by_url={_img_url(KAV_SRC_A, "REFID_SRC_1"): b"x"},
            feoid_value=None,
        )
        rehoster = ImageRehoster(ctx, cfg, manifest=manifest,
                                 download_fn=download, upload_fn=upload, feoid_fn=feoid)
        result = rehoster.run()
        assert result.report.feoid_misses == 1
        rewrite = result.image_map_by_source_kav[KAV_SRC_A]["REFID_SRC_1"]
        assert rewrite.target_feoid == ""  # missing feoid → empty string in URL
    finally:
        stager.close(); id_mapper.close(); manifest.close()


# ---------------------------------------------------------------- #
#  ContentVersion-style rehosting                                   #
# ---------------------------------------------------------------- #

def _cv_url(host: str, cvid: str) -> str:
    return f"https://{host}/sfc/servlet.shepherd/version/download/{cvid}"


def _cv_html(host: str, cvid: str) -> str:
    return f'<p><img src="{_cv_url(host, cvid)}"></p>'


def _build_cv_fakes(
    *,
    bytes_by_cvid=None,
    metadata_by_cvid=None,
    upload_id_responses=None,
    document_id_by_version_id=None,
):
    """
    Mimic _build_fakes but for the ContentVersion API surface. Returns
    (cv_download, cv_metadata, cv_upload, cv_get_document, capture).
    """
    capture = {
        "cv_downloads": [],
        "cv_metadata": [],
        "cv_uploads": [],
        "cv_doc_lookups": [],
    }
    bytes_by_cvid = bytes_by_cvid or {}
    metadata_by_cvid = metadata_by_cvid or {}
    upload_id_responses = list(upload_id_responses or [])
    document_id_by_version_id = document_id_by_version_id or {}

    def cv_download(connector, cvid):
        capture["cv_downloads"].append(cvid)
        if cvid not in bytes_by_cvid:
            raise RuntimeError(f"unexpected CV download: {cvid}")
        out = bytes_by_cvid[cvid]
        if isinstance(out, Exception):
            raise out
        return out

    def cv_metadata(connector, cvid):
        capture["cv_metadata"].append(cvid)
        return metadata_by_cvid.get(cvid)

    def cv_upload(
        connector, *, content, title, path_on_client,
        first_publish_location_id=None, content_type=None,
    ):
        capture["cv_uploads"].append({
            "content_len": len(content),
            "title": title,
            "path_on_client": path_on_client,
            "first_publish_location_id": first_publish_location_id,
        })
        if not upload_id_responses:
            return f"068TGT_{len(capture['cv_uploads'])}"
        nxt = upload_id_responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def cv_get_document(connector, version_id):
        capture["cv_doc_lookups"].append(version_id)
        return document_id_by_version_id.get(version_id)

    return cv_download, cv_metadata, cv_upload, cv_get_document, capture


CV_SRC_1 = "068SOURCE000000001"
CV_SRC_2 = "068SOURCE000000002"


def test_run_uploads_new_content_version_image():
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    manifest = _make_manifest()
    try:
        body = _cv_html("src.my.salesforce.com", CV_SRC_1)
        _seed_kav_table(stager, [{"Id": KAV_SRC_A, "Article_Body__c": body}])
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)

        ctx = _build_ctx(stager, id_mapper, _FakeConnector(), _FakeConnector())
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        from knowledge_article._image_api import ContentVersionMetadata

        cv_download, cv_metadata, cv_upload, cv_get_doc, capture = _build_cv_fakes(
            bytes_by_cvid={CV_SRC_1: b"png-bytes-cv-1"},
            metadata_by_cvid={
                CV_SRC_1: ContentVersionMetadata(
                    title="diagram", path_on_client="diagram.png",
                ),
            },
            upload_id_responses=["068TARGET000000001"],
            document_id_by_version_id={"068TARGET000000001": "069TARGET000000001"},
        )

        # rta seams unused but supplied so the constructor signature stays
        # explicit.
        rta_dl, rta_up, rta_feoid, _ = _build_fakes()
        rehoster = ImageRehoster(
            ctx, cfg, manifest=manifest,
            download_fn=rta_dl, upload_fn=rta_up, feoid_fn=rta_feoid,
            cv_download_fn=cv_download,
            cv_metadata_fn=cv_metadata,
            cv_upload_fn=cv_upload,
            cv_get_document_id_fn=cv_get_doc,
        )
        result = rehoster.run()

        assert result.report.images_seen == 1
        assert result.report.content_version_jobs == 1
        assert result.report.rta_jobs == 0
        assert result.report.images_uploaded == 1

        cv_map = result.content_version_image_map_by_source_kav[KAV_SRC_A]
        rewrite = cv_map[CV_SRC_1]
        assert rewrite.kind == "content_version"
        assert rewrite.target_content_version_id == "068TARGET000000001"

        # Upload received the article's target id as the publish location
        # so Salesforce auto-creates a ContentDocumentLink.
        assert capture["cv_uploads"][0]["first_publish_location_id"] == KAV_TGT_A
        assert capture["cv_uploads"][0]["title"] == "diagram"
        assert capture["cv_uploads"][0]["path_on_client"] == "diagram.png"

        cached = manifest.lookup(
            target_kav_id=KAV_TGT_A,
            target_field="Article_Body__c",
            content_hash=_sha256_of(b"png-bytes-cv-1"),
        )
        assert cached is not None
        assert cached.kind == "content_version"
        assert cached.target_content_version_id == "068TARGET000000001"
        assert cached.target_content_document_id == "069TARGET000000001"
    finally:
        stager.close(); id_mapper.close(); manifest.close()


def test_run_reuses_manifest_hit_for_content_version_image():
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    manifest = _make_manifest()
    try:
        body = _cv_html("src.my.salesforce.com", CV_SRC_1)
        _seed_kav_table(stager, [{"Id": KAV_SRC_A, "Article_Body__c": body}])
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)

        manifest.record(
            target_kav_id=KAV_TGT_A,
            target_field="Article_Body__c",
            content_hash=_sha256_of(b"already-uploaded"),
            kind="content_version",
            target_content_version_id="068CACHED00000001",
            target_content_document_id="069CACHED00000001",
        )

        ctx = _build_ctx(stager, id_mapper, _FakeConnector(), _FakeConnector())
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        cv_download, cv_metadata, cv_upload, cv_get_doc, capture = _build_cv_fakes(
            bytes_by_cvid={CV_SRC_1: b"already-uploaded"},
        )
        rta_dl, rta_up, rta_feoid, _ = _build_fakes()
        rehoster = ImageRehoster(
            ctx, cfg, manifest=manifest,
            download_fn=rta_dl, upload_fn=rta_up, feoid_fn=rta_feoid,
            cv_download_fn=cv_download,
            cv_metadata_fn=cv_metadata,
            cv_upload_fn=cv_upload,
            cv_get_document_id_fn=cv_get_doc,
        )
        result = rehoster.run()

        assert result.report.images_uploaded == 0
        assert result.report.images_cached == 1
        assert capture["cv_uploads"] == []
        rewrite = result.content_version_image_map_by_source_kav[KAV_SRC_A][CV_SRC_1]
        assert rewrite.target_content_version_id == "068CACHED00000001"
    finally:
        stager.close(); id_mapper.close(); manifest.close()


def test_run_handles_mixed_rta_and_content_version_in_same_body():
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    manifest = _make_manifest()
    try:
        body = (
            _img_html(KAV_SRC_A, "REFID_RTA")
            + _cv_html("src.my.salesforce.com", CV_SRC_1)
        )
        _seed_kav_table(stager, [{"Id": KAV_SRC_A, "Article_Body__c": body}])
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)

        ctx = _build_ctx(stager, id_mapper, _FakeConnector(), _FakeConnector())
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        download, upload, feoid, rta_capture = _build_fakes(
            image_bytes_by_url={_img_url(KAV_SRC_A, "REFID_RTA"): b"rta-bytes"},
            upload_responses=[UploadResult(
                target_refid="REFID_RTA_TGT",
                target_field="Article_Body__c",
                target_kav_id=KAV_TGT_A,
            )],
        )
        cv_download, cv_metadata, cv_upload, cv_get_doc, cv_capture = _build_cv_fakes(
            bytes_by_cvid={CV_SRC_1: b"cv-bytes"},
            upload_id_responses=["068TARGET000000099"],
        )
        rehoster = ImageRehoster(
            ctx, cfg, manifest=manifest,
            download_fn=download, upload_fn=upload, feoid_fn=feoid,
            cv_download_fn=cv_download,
            cv_metadata_fn=cv_metadata,
            cv_upload_fn=cv_upload,
            cv_get_document_id_fn=cv_get_doc,
        )
        result = rehoster.run()

        assert result.report.images_seen == 2
        assert result.report.rta_jobs == 1
        assert result.report.content_version_jobs == 1
        assert len(rta_capture["uploads"]) == 1
        assert len(cv_capture["cv_uploads"]) == 1

        assert result.image_map_by_source_kav[KAV_SRC_A]["REFID_RTA"].target_refid == \
            "REFID_RTA_TGT"
        assert result.content_version_image_map_by_source_kav[KAV_SRC_A][CV_SRC_1] \
            .target_content_version_id == "068TARGET000000099"
    finally:
        stager.close(); id_mapper.close(); manifest.close()


def test_run_skips_unknown_metadata_using_fallback_filename():
    """When SOQL fails to fetch metadata, upload still succeeds with a
    sensible default title + filename based on the source CVID."""
    stager = _make_stager()
    id_mapper = _make_id_mapper()
    manifest = _make_manifest()
    try:
        body = _cv_html("src.my.salesforce.com", CV_SRC_2)
        _seed_kav_table(stager, [{"Id": KAV_SRC_A, "Article_Body__c": body}])
        id_mapper.add_mapping("Knowledge__kav", KAV_SRC_A, KAV_TGT_A)

        ctx = _build_ctx(stager, id_mapper, _FakeConnector(), _FakeConnector())
        cfg = KnowledgeArticleConfig.from_dict(ctx.extension_config)

        cv_download, cv_metadata, cv_upload, cv_get_doc, capture = _build_cv_fakes(
            bytes_by_cvid={CV_SRC_2: b"some-bytes"},
            metadata_by_cvid={},  # no metadata
        )
        rta_dl, rta_up, rta_feoid, _ = _build_fakes()
        ImageRehoster(
            ctx, cfg, manifest=manifest,
            download_fn=rta_dl, upload_fn=rta_up, feoid_fn=rta_feoid,
            cv_download_fn=cv_download,
            cv_metadata_fn=cv_metadata,
            cv_upload_fn=cv_upload,
            cv_get_document_id_fn=cv_get_doc,
        ).run()

        upload_call = capture["cv_uploads"][0]
        assert upload_call["title"] == CV_SRC_2  # falls back to CVID
        assert upload_call["path_on_client"].endswith(".bin")
        assert upload_call["first_publish_location_id"] == KAV_TGT_A
    finally:
        stager.close(); id_mapper.close(); manifest.close()


# ---------------------------------------------------------------- #
#  Helpers (sha256)                                                 #
# ---------------------------------------------------------------- #

def _sha256_of(b: bytes) -> str:
    import hashlib
    return f"sha256:{hashlib.sha256(b).hexdigest()}"


if __name__ == "__main__":
    import sys
    from tests._runner import run
    sys.exit(run(globals()))
