"""
KnowledgeArticleExtension — entry point for the KA layer.

Wires KA-specific logic into the two core hooks:

  pre_transform:
    Scan rich-text fields in just-staged Knowledge__kav chunks for
    cross-article links and inline image references. Resolve referenced
    source IDs by Salesforce key prefix, enqueue supplementary extracts
    up to configured depth so referenced articles ship into target.

  post_load:
    1. (Optional) Rehost inline images: download each <img> rtaImage
       from source, dedup via SHA256 manifest, upload to the target
       article's richTextImageFields endpoint, and build a per-article
       image_map keyed by source refid.
    2. Rewrite cross-article hyperlinks and Salesforce mydomain hosts
       in every loaded article's rich-text fields. If image rehosting
       ran (and produced an image_map), the same rewriter pass swaps
       <img> URLs over to the target's eid/feoid/refid; otherwise the
       <img> tags are left intact for a later run.
    3. (Optional) Publish target articles back to PublishStatus=Online
       via Tooling API executeAnonymous + KbManagement.publishArticle.

PR roadmap:
  PR 1 — skeleton with no-op hooks (shipped).
  PR 2 — pre_transform scope expansion (shipped).
  PR 3 — post_load URL rewrite (shipped).
  PR 4 — post_load image rehosting + manifest dedup (shipped).
  PR 5 — publish drafts back to "Online" (this PR).
"""

from __future__ import annotations
import logging
from dataclasses import dataclass

from core.extension import ObjectExtension
from knowledge_article.config import KnowledgeArticleConfig
from knowledge_article.image_rehoster import ImageRehoster, RehostReport, RehostResult
from knowledge_article.publisher import KnowledgeArticlePublisher, PublishReport
from knowledge_article.scope_expander import ExpansionReport, ScopeExpander
from knowledge_article.url_rewriter_runner import RewriteRunReport, UrlRewriteRunner

logger = logging.getLogger(__name__)


@dataclass
class PostLoadReport:
    """
    Combined report for post_load. Any of the component reports may be
    None depending on which steps were enabled by config.
    """
    rehost: RehostReport | None
    rewrite: RewriteRunReport | None
    publish: PublishReport | None


class KnowledgeArticleExtension(ObjectExtension):
    object_name = "Knowledge__kav"

    @property
    def config(self) -> KnowledgeArticleConfig:
        # Re-parsed each access so pipelines reusing the same orchestrator
        # but different templates pick up their own settings.
        return KnowledgeArticleConfig.from_dict(self.ctx.extension_config)

    def pre_transform(self) -> ExpansionReport | None:
        cfg = self.config
        if not cfg.scope_expansion.enabled:
            logger.debug(
                "[%s] KA pre_transform skipped — scope_expansion.enabled=false",
                self.ctx.object_name,
            )
            return None

        expander = ScopeExpander(self.ctx, cfg)
        return expander.run()

    def post_load(self) -> PostLoadReport:
        cfg = self.config

        rehost_result: RehostResult | None = None
        if cfg.image_handling.enabled:
            try:
                rehost_result = ImageRehoster(self.ctx, cfg).run()
            except Exception:  # noqa: BLE001
                # Never let image issues abort URL rewriting — links must
                # still be fixable even when image rehosting failed.
                logger.exception(
                    "[%s] KA image rehost crashed — continuing with URL rewrite only",
                    self.ctx.object_name,
                )

        rewrite_report: RewriteRunReport | None = None
        if cfg.url_rewriting.enabled:
            image_map = (
                rehost_result.image_map_by_source_kav if rehost_result is not None else None
            )
            cv_image_map = (
                rehost_result.content_version_image_map_by_source_kav
                if rehost_result is not None else None
            )
            runner = UrlRewriteRunner(
                self.ctx,
                cfg,
                image_map_by_source_kav=image_map,
                content_version_image_map_by_source_kav=cv_image_map,
            )
            rewrite_report = runner.run()
        else:
            logger.debug(
                "[%s] KA post_load URL rewrite skipped — url_rewriting.enabled=false",
                self.ctx.object_name,
            )

        publish_report: PublishReport | None = None
        if cfg.publishing.enabled:
            try:
                publish_report = KnowledgeArticlePublisher(self.ctx, cfg).run()
            except Exception:  # noqa: BLE001
                logger.exception(
                    "[%s] KA publish step crashed — continuing without publishing",
                    self.ctx.object_name,
                )

        return PostLoadReport(
            rehost=rehost_result.report if rehost_result is not None else None,
            rewrite=rewrite_report,
            publish=publish_report,
        )
