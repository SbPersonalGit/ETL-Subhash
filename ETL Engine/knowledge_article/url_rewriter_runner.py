"""
Knowledge Article post-load URL rewrite runner.

Responsibility: for every Knowledge__kav record that we just loaded
into the target org, rewrite its rich-text fields so that cross-article
hyperlinks, Salesforce mydomain hosts, and inline images work in the
target org.

Two modes:

  * "preserve images" (PR 3 default)
      No image_map provided. <img> tags and their hosts are left
      untouched — useful when image rehosting hasn't run yet, or has
      been disabled by config.

  * "rehost images" (PR 4)
      image_map_by_source_kav is provided by ImageRehoster. The
      rewriter is then run with preserve_image_urls=False, replacing
      every rtaImage src with the target eid/feoid/refid built from
      the per-article map.

Flow:
  1. Read every staged source row's rich-text columns from the raw table.
  2. Scan each column once to collect the universe of referenced source
     IDs and tag each with its object name (lightning links carry it
     directly; classic / query-style links go through the key-prefix
     index built off `describe_global`).
  3. Resolve every (object, source_id) pair through IDMapper to build
     a single source -> target id map.
  4. For each source row, look up its target Kav id, swap in that
     article's image_map (if any), run the rewriter, and stash the
     rewritten body in an update payload. Rows whose body did not
     change are skipped so we don't issue no-op Bulk API updates.
  5. Bulk-update the target articles in chunks via Loader.update_records.

All Salesforce I/O is delegated to existing core components (Loader for
the Bulk API call, IDMapper for the source->target lookup, SFConnector
for `describe_global`). No new HTTP calls are added by this runner.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

import polars as pl

from knowledge_article._keyprefix import build_keyprefix_index, object_for_id
from knowledge_article.config import KnowledgeArticleConfig
from knowledge_article.richtext_parser import scan as scan_richtext
from knowledge_article.richtext_rewriter import ImageRewrite, RewriteContext, rewrite

if TYPE_CHECKING:
    from core.extension import PipelineContext
    from core.loader import Loader

logger = logging.getLogger(__name__)


# Bulk API 2.0 ingest payloads have a 150 MB / 100k record per-job ceiling;
# rich-text bodies push average row size up sharply, so we cap chunk size
# below the generic loader default to keep memory + retry cost bounded.
_UPDATE_CHUNK_SIZE = 1_000


LoaderFactory = Callable[[], "Loader"]


@dataclass
class RewriteRunReport:
    """Per-invocation summary returned to the caller for logging."""
    object_name: str
    candidate_records: int = 0          # rows considered (had a target id)
    records_updated: int = 0            # rows actually persisted by Bulk API
    records_unchanged: int = 0          # rows whose body did not change
    records_unmapped: int = 0           # rows whose source id had no IDMapper entry
    references_seen: int = 0            # distinct source IDs referenced in any body
    references_rewritten: int = 0       # href IDs replaced with a target id
    references_missing: int = 0         # href IDs left intact (no mapping)
    hosts_rewritten: int = 0            # SF mydomain host substitutions
    images_rewritten: int = 0           # <img> URLs rewritten to target rtaImage
    images_missing: int = 0             # <img> URLs with no rehost mapping

    def summary(self) -> str:
        return (
            f"updated={self.records_updated} unchanged={self.records_unchanged} "
            f"unmapped={self.records_unmapped} "
            f"refs_seen={self.references_seen} refs_rewritten={self.references_rewritten} "
            f"refs_missing={self.references_missing} hosts_rewritten={self.hosts_rewritten} "
            f"images_rewritten={self.images_rewritten} images_missing={self.images_missing}"
        )


class UrlRewriteRunner:
    """
    Drives the post-load URL rewrite for a single Knowledge Article object
    inside a single pipeline. Idempotent — re-running on the same data
    produces the same payload (same target IDs in id_map, same diffs).
    """

    def __init__(
        self,
        ctx: "PipelineContext",
        config: KnowledgeArticleConfig,
        loader_factory: LoaderFactory | None = None,
        image_map_by_source_kav: dict[str, dict[str, ImageRewrite]] | None = None,
        content_version_image_map_by_source_kav: (
            dict[str, dict[str, ImageRewrite]] | None
        ) = None,
    ):
        self.ctx = ctx
        self.config = config
        self.url_cfg = config.url_rewriting
        self.richtext_fields: tuple[str, ...] = config.richtext_fields
        self._loader_factory = loader_factory or self._default_loader_factory
        self._keyprefix_index: dict[str, str] | None = None
        # Rta-style image rewrites keyed by source refid (per source kav).
        self._image_map_by_source_kav = image_map_by_source_kav or {}
        # ContentVersion-style image rewrites keyed by source ContentVersionId
        # (per source kav). Independently optional from the rta map; in
        # practice articles use one mechanism or the other but the rewriter
        # tolerates a mix.
        self._cv_image_map_by_source_kav = (
            content_version_image_map_by_source_kav or {}
        )

    # ---------------------------------------------------------------- #
    #  Public entry                                                     #
    # ---------------------------------------------------------------- #

    def run(self) -> RewriteRunReport:
        report = RewriteRunReport(object_name=self.ctx.object_name)
        if not self.url_cfg.enabled:
            logger.debug(
                "[%s] URL rewrite skipped — url_rewriting.enabled=false",
                self.ctx.object_name,
            )
            return report

        rt_cols = self._existing_richtext_columns()
        if not rt_cols:
            logger.info(
                "[%s] URL rewrite: no rich-text columns staged — skip",
                self.ctx.object_name,
            )
            return report

        rows = self._read_richtext_rows(rt_cols)
        if rows.is_empty():
            logger.info(
                "[%s] URL rewrite: no staged rows — skip", self.ctx.object_name
            )
            return report

        refs_by_id = self._collect_referenced_ids(rows, rt_cols)
        report.references_seen = len(refs_by_id)
        id_map = self._build_id_map(refs_by_id)

        # If the caller supplied at least one image_map (rta or
        # ContentVersion), let the rewriter rewrite <img> URLs;
        # otherwise preserve them so the rewrite step remains a safe
        # no-op for image rendering.
        has_image_map = bool(
            self._image_map_by_source_kav or self._cv_image_map_by_source_kav
        )
        rewrite_ctx = RewriteContext(
            image_map={},                    # populated per-article in _build_update_records
            content_version_image_map={},    # populated per-article in _build_update_records
            id_map=id_map,
            target_host=self.config.target_host,
            on_missing_image=self.config.image_handling.on_missing_image
            if has_image_map
            else "keep_broken_link",
            placeholder_image_html=self.config.image_handling.placeholder_html,
            on_missing_record=self._policy_to_rewriter(self.url_cfg.on_missing_record),
            preserve_image_urls=not has_image_map,
        )

        update_records = self._build_update_records(rows, rt_cols, rewrite_ctx, report)

        if not update_records:
            logger.info(
                "[%s] URL rewrite: nothing to update | %s",
                self.ctx.object_name, report.summary(),
            )
            return report

        report.records_updated = self._dispatch_updates(update_records, rt_cols)
        logger.info("[%s] URL rewrite: %s", self.ctx.object_name, report.summary())
        return report

    # ---------------------------------------------------------------- #
    #  Internals                                                        #
    # ---------------------------------------------------------------- #

    def _existing_richtext_columns(self) -> list[str]:
        if not self.ctx.stager.table_exists(self.ctx.stager.raw_table(self.ctx.object_name)):
            return []
        all_cols = set(self.ctx.stager.get_column_names(self.ctx.object_name, "raw"))
        return [c for c in self.richtext_fields if c in all_cols]

    def _read_richtext_rows(self, rt_cols: list[str]) -> pl.DataFrame:
        cols_quoted = ", ".join([f'"Id"'] + [f'"{c}"' for c in rt_cols])
        sql = (
            f'SELECT {cols_quoted} FROM '
            f'"{self.ctx.stager.raw_table(self.ctx.object_name)}" '
            'WHERE "Id" IS NOT NULL'
        )
        return self.ctx.stager.execute_sql(sql)

    def _collect_referenced_ids(
        self,
        rows: pl.DataFrame,
        rt_cols: list[str],
    ) -> dict[str, str | None]:
        """
        First pass: walk every body once and build {source_id -> object_name|None}.
        Lightning links carry their object explicitly; classic & query links
        leave us to resolve the prefix later.
        """
        refs: dict[str, str | None] = {}
        for col in rt_cols:
            if col not in rows.columns:
                continue
            for value in rows[col].drop_nulls().to_list():
                if not value:
                    continue
                scan_result = scan_richtext(value)
                for link in scan_result.record_links:
                    existing = refs.get(link.record_id)
                    if existing:
                        # Already resolved on a previous link — don't downgrade
                        # to None when a later classic link of the same id arrives.
                        continue
                    refs[link.record_id] = link.object_name
        return refs

    def _build_id_map(self, refs: dict[str, str | None]) -> dict[str, str]:
        """
        Resolve every referenced source ID through IDMapper to its target ID.
        Group lookups by object so the IDMapper.resolve_column path can run
        a single in-memory join per object instead of N point lookups.
        """
        if not refs:
            return {}

        unresolved = [sid for sid, obj in refs.items() if not obj]
        if unresolved:
            self._ensure_keyprefix_index()
            for sid in unresolved:
                obj = object_for_id(sid, self._keyprefix_index or {})
                if obj:
                    refs[sid] = obj

        by_object: dict[str, list[str]] = defaultdict(list)
        for sid, obj in refs.items():
            if obj:
                by_object[obj].append(sid)

        id_map: dict[str, str] = {}
        for obj, sids in by_object.items():
            resolved = self._lookup_target_ids(obj, sids)
            id_map.update(resolved)
        return id_map

    def _lookup_target_ids(self, object_name: str, source_ids: list[str]) -> dict[str, str]:
        """
        Bulk-resolve `source_ids` for `object_name` via IDMapper.resolve_column.
        Falls back to per-id lookups if the join path isn't available
        (e.g. fresh IDMapper with no table for the object).
        """
        if not source_ids:
            return {}

        try:
            df = pl.DataFrame({"source_id": source_ids})
            resolved = self.ctx.id_mapper.resolve_column(
                object_name, df, "source_id", "target_id"
            )
            mapping: dict[str, str] = {}
            for sid, tid in zip(
                resolved["source_id"].to_list(),
                resolved["target_id"].to_list(),
            ):
                if sid and tid:
                    mapping[sid] = tid
            return mapping
        except Exception:  # noqa: BLE001 — fall back to per-id lookup
            logger.exception(
                "[%s] Bulk IDMapper lookup failed for %s — falling back to per-id",
                self.ctx.object_name, object_name,
            )
            return self._lookup_target_ids_per_id(object_name, source_ids)

    def _lookup_target_ids_per_id(
        self, object_name: str, source_ids: list[str]
    ) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for sid in source_ids:
            tid = self.ctx.id_mapper.get_target_id(object_name, sid)
            if tid:
                mapping[sid] = tid
        return mapping

    def _build_update_records(
        self,
        rows: pl.DataFrame,
        rt_cols: list[str],
        rewrite_ctx: RewriteContext,
        report: RewriteRunReport,
    ) -> list[dict[str, str | None]]:
        update_records: list[dict[str, str | None]] = []
        # Cache the target id for each source kav row — single bulk call.
        source_ids = [sid for sid in rows["Id"].to_list() if sid]
        kav_target_map = self._lookup_target_ids(self.ctx.object_name, source_ids)

        for row in rows.iter_rows(named=True):
            source_id = row.get("Id")
            if not source_id:
                continue
            target_id = kav_target_map.get(source_id)
            if not target_id:
                report.records_unmapped += 1
                continue

            report.candidate_records += 1
            # Swap in this article's per-kind image maps. Both can be
            # empty for articles whose body has no inline images.
            rewrite_ctx.image_map = self._image_map_by_source_kav.get(source_id, {})
            rewrite_ctx.content_version_image_map = (
                self._cv_image_map_by_source_kav.get(source_id, {})
            )

            update_record: dict[str, str | None] = {"Id": target_id}
            any_changed = False
            for col in rt_cols:
                value = row.get(col)
                if value is None:
                    continue
                new_html, stats = rewrite(value, rewrite_ctx)
                report.references_rewritten += stats.records_rewritten
                report.references_missing += stats.records_missing
                report.hosts_rewritten += stats.hosts_rewritten
                report.images_rewritten += stats.images_rewritten
                report.images_missing += stats.images_missing
                if new_html != value:
                    update_record[col] = new_html
                    any_changed = True

            if any_changed:
                update_records.append(update_record)
            else:
                report.records_unchanged += 1
        return update_records

    def _dispatch_updates(
        self,
        update_records: list[dict[str, str | None]],
        rt_cols: list[str],
    ) -> int:
        """
        Build update DataFrame(s) and hand them to Loader.update_records().
        Splits into chunks of `_UPDATE_CHUNK_SIZE` to bound bulk job size.
        """
        loader = self._loader_factory()
        total_success = 0

        # Ensure every chunk has the union of touched columns; missing cells
        # stay null so Bulk API's "leave unchanged on null" semantics apply.
        all_cols = ["Id"] + [c for c in rt_cols if any(c in r for r in update_records)]

        for chunk_start in range(0, len(update_records), _UPDATE_CHUNK_SIZE):
            chunk = update_records[chunk_start : chunk_start + _UPDATE_CHUNK_SIZE]
            df = pl.DataFrame(
                [{col: r.get(col) for col in all_cols} for r in chunk],
                schema={col: pl.Utf8 for col in all_cols},
            )
            success = loader.update_records(self.ctx.object_name, df)
            total_success += success
        return total_success

    def _ensure_keyprefix_index(self) -> None:
        if self._keyprefix_index is None:
            self._keyprefix_index = build_keyprefix_index(self.ctx.source_connector)

    @staticmethod
    def _policy_to_rewriter(policy: str) -> str:
        """
        Map a config policy string to the rewriter's `on_missing_record`
        domain. `degrade_to_text` isn't yet implemented in the rewriter,
        so we fall back to keeping the original href intact (the rewriter
        will count it as missing for reporting).
        """
        if policy == "fail":
            return "fail"
        return "keep_broken_link"

    def _default_loader_factory(self) -> "Loader":
        from core.loader import Loader
        return Loader(
            connector=self.ctx.target_connector,
            stager=self.ctx.stager,
            id_mapper=self.ctx.id_mapper,
            log_root=self.ctx.log_root,
        )
