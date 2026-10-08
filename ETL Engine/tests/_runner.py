"""
Tiny inline test runner used when pytest isn't installed.

Each test_*.py module ends with `if __name__ == "__main__": run(globals())`
so it can be executed as a plain script:
    python tests/test_richtext_parser.py

Tests are also pytest-compatible — they're plain functions whose names
begin with 'test_' and use bare 'assert'.
"""

from __future__ import annotations
import sys
import traceback


def run(module_globals: dict) -> int:
    """Execute every test_* function in `module_globals`. Return non-zero on failure."""
    tests = [
        (name, fn)
        for name, fn in module_globals.items()
        if name.startswith("test_") and callable(fn)
    ]
    if not tests:
        print("  (no tests found)")
        return 0

    failures: list[tuple[str, str]] = []
    for name, fn in tests:
        try:
            fn()
        except AssertionError as e:
            failures.append((name, f"AssertionError: {e}"))
            print(f"  FAIL {name}: {e}")
            continue
        except Exception:  # noqa: BLE001
            failures.append((name, traceback.format_exc()))
            print(f"  ERR  {name}:")
            traceback.print_exc()
            continue
        print(f"  ok   {name}")

    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    return 1 if failures else 0


def ensure_project_root_on_path() -> None:
    """Allow `python tests/test_*.py` to import top-level packages."""
    from pathlib import Path
    project_root = str(Path(__file__).resolve().parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
