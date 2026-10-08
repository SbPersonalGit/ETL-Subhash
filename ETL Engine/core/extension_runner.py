"""
Extension runner — invokes registered ObjectExtension hooks at the
right pipeline stages.

Decouples the CLI / orchestrator from extension internals: the runner
discovers which extensions apply to a pipeline, builds a PipelineContext
for each match, calls the requested hook, and (optionally) drains any
supplementary extracts queued during pre_transform.

Failure isolation: a failing extension never aborts the pipeline; it is
logged loudly and execution continues. Extensions are an enrichment
layer, not a load-blocking dependency.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from core.extension import PipelineContext
from core.extension_registry import resolve_for_objects

if TYPE_CHECKING:
    from core.connector import SFConnector
    from core.id_mapper import IDMapper
    from core.orchestrator import ObjectPlan, PipelinePlan
    from core.stager import Stager

logger = logging.getLogger(__name__)


def run_pipeline_extensions(
    *,
    stage: str,
    pipeline: "PipelinePlan",
    source_connector: "SFConnector",
    target_connector: "SFConnector",
    stager: "Stager",
    id_mapper: "IDMapper",
    log_root: Path,
) -> None:
    """
    Invoke `pre_transform` or `post_load` for every extension that
    matches an object in `pipeline`.

    Parameters
    ----------
    stage : "pre_transform" | "post_load"
        Hook to invoke.
    pipeline : PipelinePlan
        The pipeline currently being executed; supplies object plans.
    source_connector / target_connector / stager / id_mapper / log_root :
        Plumbing handed to extensions via PipelineContext.
    """
    if stage not in ("pre_transform", "post_load"):
        raise ValueError(f"Unknown extension stage: {stage}")

    object_plans = _flat_object_plans(pipeline)
    object_names = [plan.object_name for plan in object_plans]
    extensions = resolve_for_objects(object_names)
    if not extensions:
        return

    plans_by_name = {plan.object_name: plan for plan in object_plans}

    for ext_cls in extensions:
        plan = plans_by_name.get(ext_cls.object_name)
        if plan is None:
            # An extension's object isn't in the pipeline — skip silently.
            continue

        ctx = PipelineContext(
            pipeline_id=pipeline.pipeline_id,
            object_name=plan.object_name,
            source_connector=source_connector,
            target_connector=target_connector,
            stager=stager,
            id_mapper=id_mapper,
            log_root=log_root,
            extension_config=plan.extension_config or {},
        )

        try:
            extension = ext_cls(ctx)
            hook = getattr(extension, stage)
            hook()
        except Exception:  # noqa: BLE001 — extensions must never abort the pipeline
            logger.exception(
                "[%s] Extension '%s' failed during %s — pipeline continues",
                plan.object_name, ext_cls.__name__, stage,
            )


def _flat_object_plans(pipeline: "PipelinePlan") -> list["ObjectPlan"]:
    return [plan for level in pipeline.object_levels for plan in level]
