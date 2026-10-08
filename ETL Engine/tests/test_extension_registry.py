"""
Unit tests for core.extension_registry.

Run any of:
    python tests/test_extension_registry.py
    python -m pytest tests/test_extension_registry.py
"""

from __future__ import annotations

from tests._runner import ensure_project_root_on_path
ensure_project_root_on_path()

from core.extension import ObjectExtension
from core.extension_registry import (
    bootstrap_extensions,
    clear_registry,
    get_extension,
    register,
    resolve_for_objects,
)


def _setup_clean_registry():
    clear_registry()


def test_register_and_get_extension():
    _setup_clean_registry()

    class FooExtension(ObjectExtension):
        object_name = "FooObject"

    register(FooExtension)
    assert get_extension("FooObject") is FooExtension
    assert get_extension("Missing__c") is None


def test_register_requires_object_name():
    _setup_clean_registry()

    class BadExtension(ObjectExtension):
        pass

    raised = False
    try:
        register(BadExtension)
    except ValueError:
        raised = True
    assert raised


def test_resolve_for_objects_returns_unique_in_order():
    _setup_clean_registry()

    class A(ObjectExtension):
        object_name = "A"

    class B(ObjectExtension):
        object_name = "B"

    register(A)
    register(B)
    out = resolve_for_objects(["B", "A", "B", "C"])
    assert out == [B, A]


def test_resolve_skips_unregistered_objects():
    _setup_clean_registry()
    out = resolve_for_objects(["Unknown__c"])
    assert out == []


def test_bootstrap_extensions_imports_known_packages_and_registers_ka():
    _setup_clean_registry()
    registered = bootstrap_extensions()
    assert "Knowledge__kav" in registered

    ext_cls = get_extension("Knowledge__kav")
    assert ext_cls is not None
    assert ext_cls.object_name == "Knowledge__kav"


if __name__ == "__main__":
    import sys
    from tests._runner import run
    sys.exit(run(globals()))
