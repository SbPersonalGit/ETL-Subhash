"""
Knowledge Article scope expansion.

Pre-transform hook responsibility: scan rich-text fields of every
already-staged Knowledge__kav row, find any cross-article links to
records that are NOT yet in the migration scope, and supplementary-extract
those records into the same raw staging table — so transform & load
treat them like any other seeded article.

Loops until one of:
  * no new in-scope references found
  * `max_depth` levels of expansion reached
  * `max_added_records` cap reached

Out-of-scope references (e.g. links to Cases when only KA is in scope)
are counted for reporting and left untouched here. PR 3's rewriter
applies the configured `on_excluded_reference` policy at HTML rewrite
time, so this expander does not need to mutate any data.

Pure-Python orchestration over Stager + Extractor + parser. No HTTP
calls of its own — the Extractor's Bulk API queries are the only
network I/O.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Iterable

import polars as pl

from knowledge_article._keyprefix import build_keyprefix_index, object_for_id
from knowledge_article.config import KnowledgeArticleConfig, ScopeExpansionConfig
from knowledge_article.richtext_parser import scan as scan_richtext

if TYPE_CHECKING:
    from core.extension import PipelineContext
    from core.extractor import Extractor

logger = logging.getLogger(__name__)


# Bulk API 2.0 SOQL has practical query length limits (~100KB). Each
# 18-char ID inside a quoted IN clause costs 22 chars, so 500 IDs ≈ 11KB
# — well under the threshold even with a wide field list.
_ID_BATCH_SIZE = 500

ExtractorFactory = Callable[[], "Extractor"]


@dataclass
class ExpansionReport:
    """Per-invocation summary returned to the caller for logging."""
    object_name: str
    seed_count: int
    added_count: int
    depth_reached: int
    cap_reached: bool
    references_in_scope: int
    references_out_of_scope: int

    def summary(self) -> str:
        return (
            f"depth={self.depth_reached} added={self.added_count} "
            f"in_scope_refs={self.references_in_scope} "
            f"out_of_scope_refs={self.references_out_of_scope} "
            f"cap_reached={self.cap_reached}"
        )


class ScopeExpander:
    """
    Drives the scope-expansion loop for a single Knowledge Article
    object inside a single pipeline.

    The expander is intentionally short-lived — instantiated once per
    pre_transform call. Its only persistent state is which IDs it has
    already scanned (so a re-invocation on retry doesn't re-scan).
    """

    def __init__(
        self,
        ctx: "PipelineContext",
        config: KnowledgeArticleConfig,
        extractor_factory: ExtractorFactory | None = None,
    ):
        self.ctx = ctx
        self.config = config
        self.scope_cfg: ScopeExpansionConfig = config.scope_expansion
        self.richtext_fields: tuple[str, ...] = config.richtext_fields
        self._extractor_factory = extractor_factory or self._default_extractor_factory
        self._keyprefix_index: dict[str, str] | None = None

    # ---------------------------------------------------------------- #
    #  Public entry                                                     #
    # ---------------------------------------------------------------- #

    def run(self) -> ExpansionReport:
        if not self.scope_cfg.enabled:
            return ExpansionReport(
                object_name=self.ctx.object_name,
                seed_count=0, added_count=0, depth_reached=0,
                cap_reached=False,
                references_in_scope=0, references_out_of_scope=0,
            )

        seed_count = self.ctx.stager.get_raw_count(self.ctx.object_name)
        in_scope_ids: set[str] = set(self._read_staged_ids())
        scanned_ids: set[str] = set()

        added_total = 0
        depth_reached = 0
        cap_reached = False
        refs_in_scope = 0
        refs_out_of_scope = 0

        # `max_depth` is the maximum number of hops from a seed article.
        # Each loop iteration scans articles at depth N and fetches the
        # newly discovered articles at depth N+1 — so we run exactly
        # `max_depth` iterations to bring depth-`max_depth` records in.
        for current_depth in range(self.scope_cfg.max_depth):
            unscanned = in_scope_ids - scanned_ids
            if not unscanned:
                break

            scan_batch = self._scan_articles(unscanned)
            scanned_ids.update(unscanned)
            refs_in_scope += len(scan_batch.in_scope_refs)
            refs_out_of_scope += len(scan_batch.out_of_scope_refs)

            new_ids = scan_batch.in_scope_refs - in_scope_ids
            if not new_ids:
                break

            allowed = self.scope_cfg.max_added_records - added_total
            if allowed <= 0:
                cap_reached = True
                break

            ids_to_fetch = sorted(new_ids)
            if len(ids_to_fetch) > allowed:
                cap_reached = True
                ids_to_fetch = ids_to_fetch[:allowed]

            self._extract_supplementary(ids_to_fetch)
            in_scope_ids.update(ids_to_fetch)
            added_total += len(ids_to_fetch)
            depth_reached = current_depth + 1

            if cap_reached:
                break

        report = ExpansionReport(
            object_name=self.ctx.object_name,
            seed_count=seed_count,
            added_count=added_total,
            depth_reached=depth_reached,
            cap_reached=cap_reached,
            references_in_scope=refs_in_scope,
            references_out_of_scope=refs_out_of_scope,
        )
        logger.info("[%s] Scope expansion: %s", self.ctx.object_name, report.summary())
        return report

    # ---------------------------------------------------------------- #
    #  Internals                                                        #
    # ---------------------------------------------------------------- #

    def _read_staged_ids(self) -> list[str]:
        table = self.ctx.stager.raw_table(self.ctx.object_name)
        if not self.ctx.stager.table_exists(table):
            return []
        return self.ctx.stager.get_distinct_values(self.ctx.object_name, "Id", "raw")

    def _existing_richtext_columns(self) -> list[str]:
        if not self.ctx.stager.table_exists(self.ctx.stager.raw_table(self.ctx.object_name)):
            return []
        all_cols = set(self.ctx.stager.get_column_names(self.ctx.object_name, "raw"))
        return [c for c in self.richtext_fields if c in all_cols]

    def _scan_articles(self, ids: Iterable[str]) -> "_ScanBatch":
        ids_list = [i for i in ids if i]
        result = _ScanBatch()
        if not ids_list:
            return result

        rt_cols = self._existing_richtext_columns()
        if not rt_cols:
            return result

        in_scope_objects = set(self.scope_cfg.in_scope_objects)

        # Read in chunks to keep one SQL roundtrip's result-set bounded
        # — duckdb has no hard limit but 500 rows per page balances
        # memory against round-trip count.
        for batch in _chunked(ids_list, _ID_BATCH_SIZE):
            df = self._read_richtext_rows(batch, rt_cols)
            if df.is_empty():
                continue
            for col in rt_cols:
                if col not in df.columns:
                    continue
                for value in df[col].drop_nulls().to_list():
                    self._classify_links(value, in_scope_objects, result)

        return result

    def _read_richtext_rows(self, ids_batch: list[str], rt_cols: list[str]) -> "pl.DataFrame":  # type: ignore[name-defined]
        safe_ids = _safe_sf_ids(ids_batch)
        if not safe_ids:
            return pl.DataFrame()
        cols_quoted = ", ".join(f'"{c}"' for c in rt_cols)
        ids_clause = ", ".join(f"'{i}'" for i in safe_ids)
        sql = (
            f'SELECT {cols_quoted} FROM "{self.ctx.stager.raw_table(self.ctx.object_name)}" '
            f'WHERE "Id" IN ({ids_clause})'
        )
        return self.ctx.stager.execute_sql(sql)

    def _classify_links(
        self,
        html_value: str,
        in_scope_objects: set[str],
        accumulator: "_ScanBatch",
    ) -> None:
        scan_result = scan_richtext(html_value)
        if not scan_result.record_links:
            return
        for link in scan_result.record_links:
            object_name = link.object_name or self._lookup_object_for_id(link.record_id)
            if not object_name:
                accumulator.out_of_scope_refs.add(link.record_id)
                continue
            if object_name in in_scope_objects:
                accumulator.in_scope_refs.add(link.record_id)
            else:
                accumulator.out_of_scope_refs.add(link.record_id)

    def _lookup_object_for_id(self, record_id: str) -> str | None:
        if self._keyprefix_index is None:
            self._keyprefix_index = build_keyprefix_index(self.ctx.source_connector)
        return object_for_id(record_id, self._keyprefix_index)

    def _extract_supplementary(self, ids: list[str]) -> None:
        if not ids:
            return

        # Use the same field list already staged for this object so the
        # appended rows match the existing schema exactly.
        all_cols = self.ctx.stager.get_column_names(self.ctx.object_name, "raw")
        select_fields = [c for c in all_cols if not c.startswith("__")]
        if "Id" not in select_fields:
            select_fields.insert(0, "Id")

        # No extra WHERE/LIMIT here — seed and scope are defined in pipeline
        # config. Supplementary fetches only constrain by the discovered Ids.
        extractor = self._extractor_factory()
        for batch in _chunked(ids, _ID_BATCH_SIZE):
            safe_batch = _safe_sf_ids(batch)
            if not safe_batch:
                continue
            ids_clause = ", ".join(f"'{i}'" for i in safe_batch)
            soql = (
                f"SELECT {', '.join(select_fields)} FROM {self.ctx.object_name} "
                f"WHERE Id IN ({ids_clause})"
            )
            logger.info(
                "[%s] Scope expansion: extracting %d supplementary record(s)",
                self.ctx.object_name, len(safe_batch),
            )
            extractor.extract(self.ctx.object_name, soql, overwrite=False)

    def _default_extractor_factory(self) -> "Extractor":
        from core.extractor import Extractor
        return Extractor(self.ctx.source_connector, self.ctx.stager)


# ---------------------------------------------------------------- #
#  Helpers                                                          #
# ---------------------------------------------------------------- #

@dataclass
class _ScanBatch:
    in_scope_refs: set[str] = field(default_factory=set)
    out_of_scope_refs: set[str] = field(default_factory=set)


def _chunked(items: list[str], size: int) -> list[list[str]]:
    if size <= 0:
        return [items]
    return [items[i:i + size] for i in range(0, len(items), size)]


def _safe_sf_ids(values: Iterable[str]) -> list[str]:
    """
    Filter `values` down to those that look like Salesforce IDs
    (15- or 18-character alphanumeric). Returning a known-safe set
    means we can drop SQL/SOQL string-quoting concerns entirely —
    legitimate Salesforce IDs never contain quotes or backslashes.
    """
    safe: list[str] = []
    for value in values:
        if not value:
            continue
        text = str(value)
        if len(text) not in (15, 18):
            continue
        if not text.isalnum():
            continue
        safe.append(text)
    return safe
