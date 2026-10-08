"""
Knowledge Article inline-image rehoster.

Sits between the post_load extract-of-rich-text-state and the URL
rewriter. For every <img> reference inside every loaded article's rich
text fields, we:

  1. Resolve the source article's target Kav id via IDMapper.
  2. Download the source image bytes (`_image_api.download_image_bytes`
     for legacy rtaImage URLs, or `download_content_version_bytes` for
     modern Salesforce-Files-based URLs).
  3. Hash the bytes (`_hashing.content_hash`) and check the persistent
     manifest. If we've already uploaded these bytes to this target
     article+field, reuse the cached coordinates — no network calls.
  4. Otherwise upload:
       * rta path → POST richTextImageFields (legacy; many orgs reject
         this with HTTP 405 on newer API versions).
       * ContentVersion path → multipart POST /sobjects/ContentVersion
         with FirstPublishLocationId = target_kav_id. Salesforce
         auto-creates a ContentDocumentLink so the article can render
         the file.
  5. Persist the resulting target coordinates in the manifest.
  6. Build per-article image maps keyed by the source identifier the
     rewriter looks up — `source_refid` for rtaImage, source
     `ContentVersionId` for shepherd URLs.

Failures are isolated per (article, image) pair: a single
download/upload failure neither aborts the rehost loop nor breaks the
URL rewriter — it simply omits that image from the relevant image_map
and the rewriter falls back to the configured `on_missing_image` policy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

import polars as pl

from knowledge_article._hashing import content_hash
from knowledge_article._image_api import (
    ImageApiError,
    ContentVersionMetadata,
    download_content_version_bytes,
    download_image_bytes,
    fetch_content_version_metadata,
    get_content_document_id,
    resolve_target_feoid,
    upload_content_version,
    upload_image,
)
from knowledge_article.config import KnowledgeArticleConfig
from knowledge_article.image_manifest import ImageManifest
from knowledge_article.richtext_parser import scan as scan_richtext
from knowledge_article.richtext_rewriter import ImageRewrite

if TYPE_CHECKING:
    from core.extension import PipelineContext

logger = logging.getLogger(__name__)


# Public facing types ------------------------------------------------- #

@dataclass
class RehostReport:
    """Summary of a single ImageRehoster.run() invocation."""
    object_name: str
    images_seen: int = 0           # distinct image references found in bodies
    images_skipped_unmapped: int = 0  # owning source kav had no IDMapper entry
    images_cached: int = 0         # served from manifest, no upload
    images_uploaded: int = 0       # new upload (rta or ContentVersion)
    images_failed: int = 0         # download/upload exception — image left out of map
    feoid_misses: int = 0          # field for which feoid couldn't be resolved (rta)
    rta_jobs: int = 0              # rta-style jobs encountered
    content_version_jobs: int = 0  # ContentVersion-style jobs encountered

    def summary(self) -> str:
        return (
            f"seen={self.images_seen} (rta={self.rta_jobs} "
            f"cv={self.content_version_jobs}) cached={self.images_cached} "
            f"uploaded={self.images_uploaded} failed={self.images_failed} "
            f"unmapped={self.images_skipped_unmapped} feoid_miss={self.feoid_misses}"
        )


@dataclass
class RehostResult:
    """
    Output of `ImageRehoster.run()`.

    Two parallel maps:
      * `image_map_by_source_kav[source_kav_id][source_refid]` → rta-style
        ImageRewrite, used by the rewriter for rtaImage URLs.
      * `content_version_image_map_by_source_kav[source_kav_id][source_cvid]`
        → ContentVersion-style ImageRewrite, used for shepherd URLs.
    """
    report: RehostReport
    image_map_by_source_kav: dict[str, dict[str, ImageRewrite]] = field(default_factory=dict)
    content_version_image_map_by_source_kav: dict[str, dict[str, ImageRewrite]] = (
        field(default_factory=dict)
    )


# ---------------------------------------------------------------- #
#  Helpers (internal)                                               #
# ---------------------------------------------------------------- #

@dataclass
class _ImageJob:
    """
    Per-image work unit collected during the scan pass.

    `kind` discriminates the upload strategy:
      "rta"             → uses source_url + source_refid; calls upload_image.
      "content_version" → uses source_content_version_id; calls
                          upload_content_version.
    """
    source_kav_id: str
    target_field: str
    kind: str  # "rta" | "content_version"
    # rta:
    source_refid: str = ""
    source_url: str = ""
    # ContentVersion:
    source_content_version_id: str = ""


# ---------------------------------------------------------------- #
#  Orchestrator                                                     #
# ---------------------------------------------------------------- #

DownloadFn = Callable[[..., str], bytes]  # pyright: ignore[reportInvalidTypeArguments]


class ImageRehoster:
    """
    Drives image download + dedup + upload for a single Knowledge Article
    object inside a single pipeline. Designed to be safe to run more than
    once per migration — the manifest makes the second invocation a no-op
    aside from re-issuing already-cached coordinates.
    """

    def __init__(
        self,
        ctx: "PipelineContext",
        config: KnowledgeArticleConfig,
        manifest: ImageManifest | None = None,
        # Test seams — production code uses _image_api defaults.
        download_fn=download_image_bytes,
        upload_fn=upload_image,
        feoid_fn=resolve_target_feoid,
        cv_download_fn=download_content_version_bytes,
        cv_metadata_fn=fetch_content_version_metadata,
        cv_upload_fn=upload_content_version,
        cv_get_document_id_fn=get_content_document_id,
    ):
        self.ctx = ctx
        self.config = config
        self.image_cfg = config.image_handling
        self.richtext_fields: tuple[str, ...] = config.richtext_fields
        self._manifest = manifest if manifest is not None else self._build_manifest()
        self._download_fn = download_fn
        self._upload_fn = upload_fn
        self._feoid_fn = feoid_fn
        self._cv_download_fn = cv_download_fn
        self._cv_metadata_fn = cv_metadata_fn
        self._cv_upload_fn = cv_upload_fn
        self._cv_get_document_id_fn = cv_get_document_id_fn
        self._feoid_cache: dict[str, str | None] = {}  # field_name -> feoid

    # ---------------------------------------------------------------- #
    #  Public entry                                                     #
    # ---------------------------------------------------------------- #

    def run(self) -> RehostResult:
        report = RehostReport(object_name=self.ctx.object_name)
        result = RehostResult(report=report)

        if not self.image_cfg.enabled:
            logger.info(
                "[%s] Image rehost skipped — image_handling.enabled=false",
                self.ctx.object_name,
            )
            return result

        logger.info(
            "[%s] Image rehost: starting (richtext_fields=%s)",
            self.ctx.object_name, list(self.richtext_fields),
        )

        rt_cols = self._existing_richtext_columns()
        if not rt_cols:
            logger.info(
                "[%s] Image rehost: no rich-text columns staged — skip",
                self.ctx.object_name,
            )
            return result

        rows = self._read_source_rows(rt_cols)
        if rows.is_empty():
            logger.info(
                "[%s] Image rehost: no source rows to scan — skip",
                self.ctx.object_name,
            )
            return result

        # Map source kav -> target kav for every row up front.
        source_ids = [sid for sid in rows["Id"].to_list() if sid]
        kav_target_map = self._lookup_kav_target_ids(source_ids)
        logger.info(
            "[%s] Image rehost: scanning %d row(s) across columns=%s | "
            "id_map_resolved=%d/%d",
            self.ctx.object_name, rows.height, rt_cols,
            len(kav_target_map), len(source_ids),
        )

        jobs = self._collect_jobs(rows, rt_cols)
        report.images_seen = len(jobs)
        report.rta_jobs = sum(1 for j in jobs if j.kind == "rta")
        report.content_version_jobs = sum(
            1 for j in jobs if j.kind == "content_version"
        )
        logger.info(
            "[%s] Image rehost: jobs collected | total=%d rta=%d content_version=%d",
            self.ctx.object_name, len(jobs),
            report.rta_jobs, report.content_version_jobs,
        )
        if not jobs:
            logger.info(
                "[%s] Image rehost: no inline images found in any rich-text "
                "column — nothing to rehost. Check that source bodies contain "
                "<img src=...> tags pointing at /sfc/servlet.shepherd/... or "
                "rtaImage URLs.",
                self.ctx.object_name,
            )
            return result

        for job in jobs:
            target_kav_id = kav_target_map.get(job.source_kav_id)
            if not target_kav_id:
                logger.warning(
                    "[%s] Image rehost: source kav %s has no IDMapper entry "
                    "— skipping job %s",
                    self.ctx.object_name, job.source_kav_id, self._job_label(job),
                )
                report.images_skipped_unmapped += 1
                continue

            logger.info(
                "[%s] Image rehost: processing %s | source_kav=%s -> target_kav=%s | field=%s",
                self.ctx.object_name, self._job_label(job),
                job.source_kav_id, target_kav_id, job.target_field,
            )
            try:
                rewrite_for_image = self._rehost_one(job, target_kav_id, report)
            except Exception:  # noqa: BLE001 — never abort rehost loop
                logger.exception(
                    "[%s] Image rehost failed for kav=%s job=%s — skipping",
                    self.ctx.object_name, job.source_kav_id,
                    self._job_label(job),
                )
                report.images_failed += 1
                continue

            if rewrite_for_image is None:
                continue

            if job.kind == "rta":
                map_for_kav = result.image_map_by_source_kav.setdefault(
                    job.source_kav_id, {}
                )
                map_for_kav[job.source_refid] = rewrite_for_image
            elif job.kind == "content_version":
                map_for_kav = result.content_version_image_map_by_source_kav.setdefault(
                    job.source_kav_id, {}
                )
                map_for_kav[job.source_content_version_id] = rewrite_for_image

        logger.info("[%s] Image rehost: %s", self.ctx.object_name, report.summary())
        return result

    @staticmethod
    def _job_label(job: _ImageJob) -> str:
        if job.kind == "rta":
            return f"rta refid={job.source_refid}"
        return f"cv id={job.source_content_version_id}"

    # ---------------------------------------------------------------- #
    #  Internals                                                        #
    # ---------------------------------------------------------------- #

    def _rehost_one(
        self,
        job: _ImageJob,
        target_kav_id: str,
        report: RehostReport,
    ) -> ImageRewrite | None:
        """
        Bring a single image into target. Returns the ImageRewrite the
        rewriter will use, or None if we couldn't establish one.
        Dispatches to the correct upload strategy based on `job.kind`.
        """
        if job.kind == "rta":
            return self._rehost_rta(job, target_kav_id, report)
        if job.kind == "content_version":
            return self._rehost_content_version(job, target_kav_id, report)
        logger.warning(
            "[%s] Unknown image job kind=%s — skipping",
            self.ctx.object_name, job.kind,
        )
        return None

    def _rehost_rta(
        self,
        job: _ImageJob,
        target_kav_id: str,
        report: RehostReport,
    ) -> ImageRewrite | None:
        """Legacy rtaImage rehost path — POST richTextImageFields."""
        bytes_ = self._download_fn(self.ctx.source_connector, job.source_url)
        if not bytes_:
            report.images_failed += 1
            return None
        digest = content_hash(bytes_)

        cached = self._manifest.lookup(
            target_kav_id=target_kav_id,
            target_field=job.target_field,
            content_hash=digest,
        )
        if cached is not None and cached.kind == "rta":
            report.images_cached += 1
            return ImageRewrite(
                target_eid=cached.target_eid,
                target_feoid=cached.target_feoid,
                target_refid=cached.target_refid,
                kind="rta",
            )

        upload = self._upload_fn(
            self.ctx.target_connector,
            object_name=self.ctx.object_name,
            target_kav_id=target_kav_id,
            target_field=job.target_field,
            content=bytes_,
            filename=self._derive_filename(job.source_refid),
        )
        report.images_uploaded += 1

        feoid = self._get_feoid(job.target_field) or ""
        if not feoid:
            report.feoid_misses += 1

        self._manifest.record(
            target_kav_id=target_kav_id,
            target_field=job.target_field,
            content_hash=digest,
            target_eid=target_kav_id,
            target_feoid=feoid,
            target_refid=upload.target_refid,
            source_kav_id=job.source_kav_id,
            source_refid=job.source_refid,
            kind="rta",
        )

        return ImageRewrite(
            target_eid=target_kav_id,
            target_feoid=feoid,
            target_refid=upload.target_refid,
            kind="rta",
        )

    def _rehost_content_version(
        self,
        job: _ImageJob,
        target_kav_id: str,
        report: RehostReport,
    ) -> ImageRewrite | None:
        """
        Modern Salesforce-Files rehost path. Downloads the source
        ContentVersion's binary, uploads a new ContentVersion to the
        target with `FirstPublishLocationId = target_kav_id` so
        Salesforce auto-creates a ContentDocumentLink, and records the
        new ContentVersionId in the manifest.
        """
        source_cvid = job.source_content_version_id
        logger.info(
            "[%s] CV: downloading source ContentVersion %s",
            self.ctx.object_name, source_cvid,
        )
        bytes_ = self._cv_download_fn(self.ctx.source_connector, source_cvid)
        if not bytes_:
            logger.warning(
                "[%s] CV: download returned empty body for %s — skipping",
                self.ctx.object_name, source_cvid,
            )
            report.images_failed += 1
            return None
        digest = content_hash(bytes_)
        logger.info(
            "[%s] CV: downloaded %d bytes (sha256=%s...) for %s",
            self.ctx.object_name, len(bytes_), digest[:12], source_cvid,
        )

        cached = self._manifest.lookup(
            target_kav_id=target_kav_id,
            target_field=job.target_field,
            content_hash=digest,
        )
        if cached is not None and cached.kind == "content_version" \
                and cached.target_content_version_id:
            logger.info(
                "[%s] CV: manifest hit — reusing target ContentVersion %s "
                "(cached for kav=%s field=%s)",
                self.ctx.object_name, cached.target_content_version_id,
                target_kav_id, job.target_field,
            )
            report.images_cached += 1
            return ImageRewrite(
                kind="content_version",
                target_content_version_id=cached.target_content_version_id,
            )

        # Best-effort metadata lookup so the target file inherits the
        # source file's title / filename. Fall back to the source CVID
        # for both if the SOQL is forbidden / record is hidden.
        meta: ContentVersionMetadata | None = None
        try:
            meta = self._cv_metadata_fn(self.ctx.source_connector, source_cvid)
        except Exception:  # noqa: BLE001
            logger.debug(
                "[%s] ContentVersion metadata lookup failed for %s — using fallback",
                self.ctx.object_name, source_cvid,
            )
        title = (meta.title if meta else source_cvid) or source_cvid
        path_on_client = (
            meta.path_on_client if meta and meta.path_on_client else f"{source_cvid}.bin"
        )

        logger.info(
            "[%s] CV: uploading new ContentVersion to target | "
            "title=%r path_on_client=%r first_publish_location_id=%s",
            self.ctx.object_name, title, path_on_client, target_kav_id,
        )
        target_cv_id = self._cv_upload_fn(
            self.ctx.target_connector,
            content=bytes_,
            title=title,
            path_on_client=path_on_client,
            first_publish_location_id=target_kav_id,
        )
        report.images_uploaded += 1
        logger.info(
            "[%s] CV: uploaded — target ContentVersion=%s linked to kav=%s",
            self.ctx.object_name, target_cv_id, target_kav_id,
        )

        # ContentDocumentId lookup is best-effort (used only for manifest
        # bookkeeping / future operations like explicit visibility tweaks).
        target_cd_id = ""
        try:
            target_cd_id = self._cv_get_document_id_fn(
                self.ctx.target_connector, target_cv_id
            ) or ""
        except Exception:  # noqa: BLE001
            logger.debug(
                "[%s] ContentDocumentId lookup failed for new version %s",
                self.ctx.object_name, target_cv_id,
            )

        self._manifest.record(
            target_kav_id=target_kav_id,
            target_field=job.target_field,
            content_hash=digest,
            target_eid=target_kav_id,
            source_kav_id=job.source_kav_id,
            source_refid=source_cvid,  # back-compat shortcut
            kind="content_version",
            target_content_version_id=target_cv_id,
            target_content_document_id=target_cd_id,
            source_content_version_id=source_cvid,
        )

        return ImageRewrite(
            kind="content_version",
            target_content_version_id=target_cv_id,
        )

    def _existing_richtext_columns(self) -> list[str]:
        if not self.ctx.stager.table_exists(self.ctx.stager.raw_table(self.ctx.object_name)):
            return []
        all_cols = set(self.ctx.stager.get_column_names(self.ctx.object_name, "raw"))
        return [c for c in self.richtext_fields if c in all_cols]

    def _read_source_rows(self, rt_cols: list[str]) -> pl.DataFrame:
        cols_quoted = ", ".join([f'"Id"'] + [f'"{c}"' for c in rt_cols])
        sql = (
            f'SELECT {cols_quoted} FROM '
            f'"{self.ctx.stager.raw_table(self.ctx.object_name)}" '
            'WHERE "Id" IS NOT NULL'
        )
        return self.ctx.stager.execute_sql(sql)

    def _collect_jobs(self, rows: pl.DataFrame, rt_cols: list[str]) -> list[_ImageJob]:
        """Walk every body once, emit one _ImageJob per (kav, image) pair."""
        jobs: list[_ImageJob] = []
        # Dedup keys are tuple(source_kav, kind, identifier, field).
        seen_keys: set[tuple[str, str, str, str]] = set()
        for row in rows.iter_rows(named=True):
            source_kav_id = row.get("Id")
            if not source_kav_id:
                continue
            for col in rt_cols:
                value = row.get(col)
                if not value:
                    continue
                scan_result = scan_richtext(value)
                for image in scan_result.images:
                    if image.kind == "rta":
                        key = (source_kav_id, "rta", image.refid, col)
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        jobs.append(_ImageJob(
                            source_kav_id=source_kav_id,
                            target_field=col,
                            kind="rta",
                            source_refid=image.refid,
                            source_url=image.full_url,
                        ))
                    elif image.kind == "content_version":
                        cvid = image.content_version_id
                        if not cvid:
                            continue
                        key = (source_kav_id, "content_version", cvid, col)
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        jobs.append(_ImageJob(
                            source_kav_id=source_kav_id,
                            target_field=col,
                            kind="content_version",
                            source_content_version_id=cvid,
                            source_url=image.full_url,
                        ))
        # Tally per-kind once, after dedup.
        return jobs

    def _lookup_kav_target_ids(self, source_ids: list[str]) -> dict[str, str]:
        if not source_ids:
            return {}
        try:
            df = pl.DataFrame({"source_id": source_ids})
            resolved = self.ctx.id_mapper.resolve_column(
                self.ctx.object_name, df, "source_id", "target_id"
            )
            return {
                sid: tid
                for sid, tid in zip(
                    resolved["source_id"].to_list(),
                    resolved["target_id"].to_list(),
                )
                if sid and tid
            }
        except Exception:  # noqa: BLE001 — fall back to per-id
            logger.exception(
                "[%s] Bulk IDMapper lookup failed — falling back to per-id",
                self.ctx.object_name,
            )
            mapping: dict[str, str] = {}
            for sid in source_ids:
                tid = self.ctx.id_mapper.get_target_id(self.ctx.object_name, sid)
                if tid:
                    mapping[sid] = tid
            return mapping

    def _get_feoid(self, field_name: str) -> str | None:
        if field_name in self._feoid_cache:
            return self._feoid_cache[field_name]
        feoid = self._feoid_fn(
            self.ctx.target_connector,
            object_name=self.ctx.object_name,
            field_name=field_name,
        )
        self._feoid_cache[field_name] = feoid
        return feoid

    @staticmethod
    def _derive_filename(source_refid: str) -> str:
        # Salesforce ignores filename for richTextImageFields, but it has
        # to be non-empty. Use the source refid so log breadcrumbs match.
        safe = "".join(ch for ch in source_refid if ch.isalnum() or ch in "._-")
        return f"{safe or 'image'}.png"

    def _build_manifest(self) -> ImageManifest:
        cfg_path = self.image_cfg.manifest_path
        if cfg_path:
            return ImageManifest(path=cfg_path)
        # Default: state/ka_image_manifest.sqlite handled by ImageManifest.
        return ImageManifest()
