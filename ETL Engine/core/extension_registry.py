"""
Extension registry — central catalog of object-specific extensions.

Extension packages register themselves at import time via `register()`.
The orchestrator queries this registry per pipeline to discover which
extensions apply to the objects in scope.

Bootstrapping: core does NOT import any extension package directly.
Extensions are loaded by `bootstrap_extensions()` which imports a
known list of extension package names. The CLI / orchestrator calls
this once at startup (wired in PR 2).

This indirection keeps `core/` decoupled from concrete extensions.
"""

from __future__ import annotations
import importlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.extension import ObjectExtension

logger = logging.getLogger(__name__)

_EXTENSIONS: dict[str, type["ObjectExtension"]] = {}

# Known extension packages. Adding a new entry here is the only core-side
# change required to enable a new object-specific extension layer.
_KNOWN_EXTENSION_PACKAGES: tuple[str, ...] = (
    "knowledge_article",
)


def register(extension_cls: type["ObjectExtension"]) -> type["ObjectExtension"]:
    """
    Register an extension class for its declared `object_name`.
    Returns the class unchanged so it can be used as a decorator.
    """
    name = getattr(extension_cls, "object_name", "")
    if not name:
        raise ValueError(
            f"Extension {extension_cls.__name__} must declare a non-empty 'object_name' attribute"
        )
    existing = _EXTENSIONS.get(name)
    if existing is not None and existing is not extension_cls:
        logger.warning(
            "Overriding extension for '%s': %s -> %s",
            name, existing.__name__, extension_cls.__name__,
        )
    _EXTENSIONS[name] = extension_cls
    return extension_cls


def get_extension(object_name: str) -> type["ObjectExtension"] | None:
    """Return the extension class registered for `object_name`, if any."""
    return _EXTENSIONS.get(object_name)


def resolve_for_objects(object_names: list[str]) -> list[type["ObjectExtension"]]:
    """
    Return all extension classes whose `object_name` matches any of the
    given object names, preserving the order objects were declared.
    """
    seen: set[str] = set()
    extensions: list[type["ObjectExtension"]] = []
    for name in object_names:
        ext_cls = _EXTENSIONS.get(name)
        if ext_cls and ext_cls.__name__ not in seen:
            extensions.append(ext_cls)
            seen.add(ext_cls.__name__)
    return extensions


def bootstrap_extensions(packages: tuple[str, ...] | None = None) -> list[str]:
    """
    Import every known extension package so its `register()` calls execute.
    Returns the list of object_names that ended up registered.
    Idempotent — calling more than once is safe.
    """
    targets = packages if packages is not None else _KNOWN_EXTENSION_PACKAGES
    for module_name in targets:
        try:
            importlib.import_module(module_name)
        except ImportError as e:
            logger.warning("Skipping extension package '%s' (not importable): %s", module_name, e)
        except Exception as e:  # noqa: BLE001 — registration failures must not crash core
            logger.error("Failed to bootstrap extension package '%s': %s", module_name, e)
    return sorted(_EXTENSIONS.keys())


def clear_registry() -> None:
    """Test-only: wipe the registry so individual tests start clean."""
    _EXTENSIONS.clear()
