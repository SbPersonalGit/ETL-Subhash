"""
Knowledge Article extension package.

This package implements the KA-specific layer that wraps the core ETL
pipeline with two hooks (pre_transform + post_load) to handle:
  - Rich-text scope expansion (auto-include referenced records)
  - Inline image rehosting via richTextImageFields API
  - Cross-article link rewriting using IDMapper
  - Image dedup across re-runs (manifest + target-body diff)

Importing this package registers KnowledgeArticleExtension in
core.extension_registry. The orchestrator (PR 2) calls
core.extension_registry.bootstrap_extensions() at startup to trigger
this import.

Layering rule: this package may import from core.* but core.* must
never import from this package.
"""

from core.extension_registry import register
from knowledge_article.extension import KnowledgeArticleExtension

register(KnowledgeArticleExtension)

__all__ = ["KnowledgeArticleExtension"]
