"""
ObjectExtension — base contract for object-specific pipeline extensions.

Extensions plug into the orchestrator at well-defined hook points to handle
object-specific concerns (e.g. Knowledge Article rich-text rehosting,
Attachment binary migration) WITHOUT modifying core code.

Hook points (invoked by core/orchestrator.py — wired in PR 2):
  - pre_transform(ctx)   Invoked after each extract level finishes,
                          before transform & load. Use for: scope expansion,
                          supplementary record extracts, side-channel state.
  - post_load(ctx)       Invoked after Pass 1 + Pass 2 of load completes.
                          Use for: post-load fixups (binary uploads,
                          HTML rewrites, bulk updates that depend on
                          fully-populated IDMapper).

Extensions are registered against a specific object name. The orchestrator
resolves them per pipeline based on the objects in scope.

Design rules (do not violate):
  - Extensions interact with core ONLY through PipelineContext — never via
    direct imports of concrete pipeline classes.
  - core/* must not import from any extension package. Registration happens
    at extension-package import time.
  - Hooks must be idempotent — they may be invoked again on retry.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.connector import SFConnector
    from core.id_mapper import IDMapper
    from core.stager import Stager


@dataclass
class PipelineContext:
    """
    Read/write context handed to every extension hook invocation.

    The orchestrator builds one PipelineContext per (pipeline, object) hook
    invocation. Extensions may mutate `supplementary_extract_queue` to ask
    the orchestrator to fetch more records before the transform phase begins.
    """
    pipeline_id: str
    object_name: str
    source_connector: "SFConnector"
    target_connector: "SFConnector"
    stager: "Stager"
    id_mapper: "IDMapper"
    log_root: Path
    extension_config: dict[str, Any] = field(default_factory=dict)
    supplementary_extract_queue: list[dict] = field(default_factory=list)

    def queue_supplementary_extract(
        self,
        object_name: str,
        source_ids: list[str],
        reason: str = "",
    ) -> None:
        """
        Ask the orchestrator to extract additional records for `object_name`
        keyed by the given source IDs. Consumed by orchestrator after the
        pre_transform hook returns.
        """
        if not source_ids:
            return
        self.supplementary_extract_queue.append({
            "object": object_name,
            "ids": list(dict.fromkeys(source_ids)),
            "reason": reason,
        })


class ObjectExtension:
    """
    Base class for object-specific pipeline extensions.

    Subclasses declare which Salesforce object they apply to via the
    `object_name` class attribute and override the hooks they need.
    Default hook implementations are no-ops, so subclasses only override
    what they care about.
    """
    object_name: str = ""

    def __init__(self, ctx: PipelineContext):
        self.ctx = ctx

    def pre_transform(self) -> None:
        """Invoked after extract, before transform & load. No-op by default."""
        return None

    def post_load(self) -> None:
        """Invoked after Pass 1 + Pass 2 load completes. No-op by default."""
        return None
