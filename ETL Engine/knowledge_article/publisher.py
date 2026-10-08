"""
Knowledge Article publisher.

After the URL rewrite + image rehost steps land, every loaded
Knowledge__kav row sits in the target org as PublishStatus = 'Draft'.
Salesforce does NOT allow flipping PublishStatus through the Bulk or
REST update APIs — the field is read-only. The supported way to move
an article from Draft -> Online is via the Apex method:

    KbManagement.PublishingService.publishArticle(articleId, flagAsNewVersion);

We invoke that method via the Tooling API's `executeAnonymous`
endpoint, which runs server-side Apex without requiring any custom
class deployment in the target org.

Batching
--------
`executeAnonymous` accepts up to ~32 KB of Apex per call. Each publish
statement is ~70 chars, so we cap batch size at `PublishingConfig.batch_size`
(default 200). The body wraps each call in try/catch so a single bad
article doesn't abort the rest of the batch — failures are surfaced
through `System.debug` and a final assertion that we record below.

If `retry_failed_batches_individually` is true, a batch with a failed
compile / runtime exception triggers per-id retries so we can identify
exactly which articles failed.

Source-status filter
--------------------
By default we only publish articles whose source row had
`PublishStatus = 'Online'`. Source-side drafts stay drafts in target —
those were never visible to end users on the source either.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import polars as pl

from knowledge_article.config import KnowledgeArticleConfig

if TYPE_CHECKING:
    from core.extension import PipelineContext

logger = logging.getLogger(__name__)


@dataclass
class PublishReport:
    """Summary of a single Publisher.run() invocation."""
    object_name: str
    candidates: int = 0          # source rows considered
    skipped_unmapped: int = 0    # source row had no IDMapper entry
    skipped_status: int = 0      # source PublishStatus didn't match filter
    attempted: int = 0           # target ids actually included in publish calls
    published: int = 0           # successful publishes
    failed: int = 0              # failed publishes (after retries)
    failed_ids: list[str] = field(default_factory=list)
    batches: int = 0             # executeAnonymous calls issued

    def summary(self) -> str:
        return (
            f"published={self.published} failed={self.failed} "
            f"attempted={self.attempted} candidates={self.candidates} "
            f"skipped_unmapped={self.skipped_unmapped} "
            f"skipped_status={self.skipped_status} batches={self.batches}"
        )


class KnowledgeArticlePublisher:
    """
    Drives the Draft -> Online flip for a single Knowledge Article object
    inside a single pipeline. Idempotent — calling publishArticle on an
    already-Online article is a server-side no-op (KbManagement throws
    `ARTICLE_ALREADY_PUBLISHED` which we catch in the Apex wrapper).
    """

    def __init__(
        self,
        ctx: "PipelineContext",
        config: KnowledgeArticleConfig,
    ):
        self.ctx = ctx
        self.config = config
        self.publish_cfg = config.publishing

    # ---------------------------------------------------------------- #
    #  Public entry                                                     #
    # ---------------------------------------------------------------- #

    def run(self) -> PublishReport:
        report = PublishReport(object_name=self.ctx.object_name)
        if not self.publish_cfg.enabled:
            logger.debug(
                "[%s] Publishing skipped — publishing.enabled=false",
                self.ctx.object_name,
            )
            return report

        target_ids = self._collect_target_ids(report)
        if not target_ids:
            logger.info(
                "[%s] Publishing: no candidate target ids | %s",
                self.ctx.object_name, report.summary(),
            )
            return report

        report.attempted = len(target_ids)
        batch_size = max(1, self.publish_cfg.batch_size)
        for chunk_start in range(0, len(target_ids), batch_size):
            chunk = target_ids[chunk_start:chunk_start + batch_size]
            self._publish_chunk(chunk, report)

        logger.info("[%s] Publishing: %s", self.ctx.object_name, report.summary())
        return report

    # ---------------------------------------------------------------- #
    #  Internals                                                        #
    # ---------------------------------------------------------------- #

    def _collect_target_ids(self, report: PublishReport) -> list[str]:
        """
        Return the target Kav ids that should be published.

        Reads the staged source rows so we can apply the
        `only_if_source_status` filter, then resolves each source id to
        a target id via IDMapper.
        """
        rows = self._read_source_rows()
        if rows.is_empty():
            return []

        report.candidates = rows.shape[0]

        filter_status = self.publish_cfg.only_if_source_status
        if filter_status is not None and "PublishStatus" in rows.columns:
            keep = rows.filter(pl.col("PublishStatus") == filter_status)
            report.skipped_status = rows.shape[0] - keep.shape[0]
            rows = keep

        source_ids = [sid for sid in rows["Id"].to_list() if sid]
        if not source_ids:
            return []

        target_map = self._lookup_target_ids(source_ids)
        result: list[str] = []
        for sid in source_ids:
            tid = target_map.get(sid)
            if tid:
                result.append(tid)
            else:
                report.skipped_unmapped += 1
        return result

    def _read_source_rows(self) -> pl.DataFrame:
        if not self.ctx.stager.table_exists(self.ctx.stager.raw_table(self.ctx.object_name)):
            return pl.DataFrame()
        all_cols = set(self.ctx.stager.get_column_names(self.ctx.object_name, "raw"))
        select_cols = ['"Id"']
        if "PublishStatus" in all_cols:
            select_cols.append('"PublishStatus"')
        sql = (
            f'SELECT {", ".join(select_cols)} FROM '
            f'"{self.ctx.stager.raw_table(self.ctx.object_name)}" '
            'WHERE "Id" IS NOT NULL'
        )
        return self.ctx.stager.execute_sql(sql)

    def _lookup_target_ids(self, source_ids: list[str]) -> dict[str, str]:
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
        except Exception:  # noqa: BLE001
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

    def _publish_chunk(self, target_ids: list[str], report: PublishReport) -> None:
        """
        Publish a batch via executeAnonymous. On failure (compile or
        runtime), optionally retry per-id to localise the bad article.
        """
        report.batches += 1
        apex = self._build_batch_apex(target_ids)
        success, message = self._execute_anonymous(apex)
        if success:
            report.published += len(target_ids)
            return

        logger.warning(
            "[%s] Publish batch of %d failed: %s",
            self.ctx.object_name, len(target_ids), message,
        )

        if not self.publish_cfg.retry_failed_batches_individually:
            report.failed += len(target_ids)
            report.failed_ids.extend(target_ids)
            return

        for tid in target_ids:
            single_apex = self._build_batch_apex([tid])
            ok, msg = self._execute_anonymous(single_apex)
            if ok:
                report.published += 1
            else:
                report.failed += 1
                report.failed_ids.append(tid)
                logger.warning(
                    "[%s] Publish failed for %s: %s",
                    self.ctx.object_name, tid, msg,
                )

    def _build_batch_apex(self, target_ids: list[str]) -> str:
        """
        Build the anonymous Apex body. Each id gets its own try/catch so
        an `ARTICLE_ALREADY_PUBLISHED` (or any other recoverable error on
        a single article) doesn't kill the whole batch.
        """
        flag = "true" if self.publish_cfg.flag_as_new_version else "false"
        # Salesforce IDs are alphanumeric; reject anything else just in case
        # to keep the Apex body free of injection risk.
        safe_ids = [tid for tid in target_ids if tid and tid.isalnum()]
        if not safe_ids:
            return ""

        return "\n".join(
            f"try {{ KbManagement.PublishingService.publishArticle('{tid}', {flag}); }} "
            f"catch (Exception e) {{ System.debug('PUBLISH_FAIL {tid}: ' + e.getMessage()); }}"
            for tid in safe_ids
        )

    def _execute_anonymous(self, apex: str) -> tuple[bool, str | None]:
        """
        Run `apex` via the Tooling API's executeAnonymous endpoint.

        Returns (success, message). Success is true when Salesforce reports
        both `compiled` and `success` true. `message` is non-None when
        Salesforce reports a compileProblem or exceptionMessage.
        """
        if not apex.strip():
            return True, None

        connector = self.ctx.target_connector
        url = (
            f"{connector.instance_url}/services/data/{connector.api_version}"
            f"/tooling/executeAnonymous"
        )
        try:
            resp = connector._request("GET", url, params={"anonymousBody": apex})
        except Exception as e:  # noqa: BLE001
            return False, f"executeAnonymous transport error: {e}"

        try:
            body = resp.json()
        except ValueError:
            return False, f"non-JSON executeAnonymous response: {resp.text[:200]}"

        compiled = bool(body.get("compiled"))
        success = bool(body.get("success"))
        if compiled and success:
            return True, None

        problem = body.get("compileProblem") or body.get("exceptionMessage") or ""
        return False, str(problem)
