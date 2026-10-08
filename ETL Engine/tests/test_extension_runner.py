"""
Unit tests for core.extension_runner.

Run any of:
    python tests/test_extension_runner.py
    python -m pytest tests/test_extension_runner.py
"""

from __future__ import annotations

from pathlib import Path

from tests._runner import ensure_project_root_on_path
ensure_project_root_on_path()

from core.extension import ObjectExtension
from core.extension_registry import clear_registry, register
from core.extension_runner import run_pipeline_extensions
from core.orchestrator import ObjectPlan, PipelinePlan


def _setup_clean_registry():
    clear_registry()


def _make_pipeline(object_plans: list[ObjectPlan]) -> PipelinePlan:
    return PipelinePlan(
        pipeline_id="p1",
        name="Pipeline 1",
        order=1,
        seed_cfg={},
        nodes_cfg=[],
        object_configs=[],
        extract_levels=[],
        object_levels=[object_plans],
        cyclic_pairs=[],
    )


def _make_object_plan(name: str, extension_config: dict | None = None) -> ObjectPlan:
    return ObjectPlan(
        object_name=name,
        soql=f"SELECT Id FROM {name}",
        external_id_field="Id",
        operation="upsert",
        transform_rules=[],
        cyclic_fields=[],
        depends_on=[],
        extension_config=dict(extension_config or {}),
    )


def test_runs_pre_transform_for_matching_extension():
    _setup_clean_registry()
    invocations: list[str] = []

    class FakeExt(ObjectExtension):
        object_name = "Foo__c"

        def pre_transform(self) -> None:
            invocations.append(f"pre:{self.ctx.object_name}:{self.ctx.extension_config.get('marker', '')}")

    register(FakeExt)
    pipeline = _make_pipeline([_make_object_plan("Foo__c", {"marker": "abc"})])

    run_pipeline_extensions(
        stage="pre_transform",
        pipeline=pipeline,
        source_connector=None,
        target_connector=None,
        stager=None,
        id_mapper=None,
        log_root=Path("."),
    )

    assert invocations == ["pre:Foo__c:abc"]


def test_skips_objects_without_a_registered_extension():
    _setup_clean_registry()
    invocations: list[str] = []

    class FakeExt(ObjectExtension):
        object_name = "Foo__c"

        def pre_transform(self) -> None:
            invocations.append("called")

    register(FakeExt)
    pipeline = _make_pipeline([
        _make_object_plan("Bar__c"),  # no extension registered
    ])

    run_pipeline_extensions(
        stage="pre_transform",
        pipeline=pipeline,
        source_connector=None,
        target_connector=None,
        stager=None,
        id_mapper=None,
        log_root=Path("."),
    )

    assert invocations == []


def test_runs_post_load_hook():
    _setup_clean_registry()
    invocations: list[str] = []

    class FakeExt(ObjectExtension):
        object_name = "Foo__c"

        def post_load(self) -> None:
            invocations.append("post")

    register(FakeExt)
    pipeline = _make_pipeline([_make_object_plan("Foo__c")])

    run_pipeline_extensions(
        stage="post_load",
        pipeline=pipeline,
        source_connector=None,
        target_connector=None,
        stager=None,
        id_mapper=None,
        log_root=Path("."),
    )

    assert invocations == ["post"]


def test_extension_failure_does_not_propagate():
    _setup_clean_registry()
    invocations: list[str] = []

    class GoodExt(ObjectExtension):
        object_name = "Foo__c"

        def pre_transform(self) -> None:
            invocations.append("good")

    class BadExt(ObjectExtension):
        object_name = "Bar__c"

        def pre_transform(self) -> None:
            raise RuntimeError("boom")

    register(GoodExt)
    register(BadExt)

    pipeline = _make_pipeline([
        _make_object_plan("Foo__c"),
        _make_object_plan("Bar__c"),
    ])

    # Should not raise — pipeline continues even though BadExt blows up.
    run_pipeline_extensions(
        stage="pre_transform",
        pipeline=pipeline,
        source_connector=None,
        target_connector=None,
        stager=None,
        id_mapper=None,
        log_root=Path("."),
    )

    assert "good" in invocations


def test_unknown_stage_raises_value_error():
    _setup_clean_registry()
    pipeline = _make_pipeline([_make_object_plan("Foo__c")])
    raised = False
    try:
        run_pipeline_extensions(
            stage="unknown_stage",
            pipeline=pipeline,
            source_connector=None,
            target_connector=None,
            stager=None,
            id_mapper=None,
            log_root=Path("."),
        )
    except ValueError:
        raised = True
    assert raised


def test_no_extensions_short_circuits_silently():
    _setup_clean_registry()  # nothing registered
    pipeline = _make_pipeline([_make_object_plan("Foo__c")])

    run_pipeline_extensions(
        stage="pre_transform",
        pipeline=pipeline,
        source_connector=None,
        target_connector=None,
        stager=None,
        id_mapper=None,
        log_root=Path("."),
    )


if __name__ == "__main__":
    import sys
    from tests._runner import run
    sys.exit(run(globals()))
