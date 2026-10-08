"""
Unit tests for knowledge_article._keyprefix.

Run any of:
    python tests/test_keyprefix.py
    python -m pytest tests/test_keyprefix.py
"""

from __future__ import annotations

from tests._runner import ensure_project_root_on_path
ensure_project_root_on_path()

from knowledge_article._keyprefix import build_keyprefix_index, object_for_id


class _FakeConnector:
    def __init__(self, sobjects):
        self._sobjects = sobjects

    def describe_global(self):
        return list(self._sobjects)


def test_build_keyprefix_index_maps_prefix_to_object_name():
    fake = _FakeConnector([
        {"name": "Account", "keyPrefix": "001"},
        {"name": "Knowledge__kav", "keyPrefix": "ka0"},
        {"name": "Case", "keyPrefix": "500"},
    ])
    idx = build_keyprefix_index(fake)
    assert idx == {"001": "Account", "ka0": "Knowledge__kav", "500": "Case"}


def test_build_keyprefix_index_skips_objects_without_prefix():
    fake = _FakeConnector([
        {"name": "Name", "keyPrefix": None},
        {"name": "AggregateResult", "keyPrefix": None},
        {"name": "Account", "keyPrefix": "001"},
    ])
    idx = build_keyprefix_index(fake)
    assert idx == {"001": "Account"}


def test_build_keyprefix_index_first_write_wins_on_collision():
    fake = _FakeConnector([
        {"name": "FirstObject__c", "keyPrefix": "xx0"},
        {"name": "SecondObject__c", "keyPrefix": "xx0"},
    ])
    idx = build_keyprefix_index(fake)
    assert idx == {"xx0": "FirstObject__c"}


def test_object_for_id_resolves_known_prefix():
    idx = {"001": "Account", "ka0": "Knowledge__kav"}
    assert object_for_id("001B0000003ABCDEFG", idx) == "Account"
    assert object_for_id("ka0XX000000001AAA", idx) == "Knowledge__kav"


def test_object_for_id_returns_none_for_unknown_prefix():
    idx = {"001": "Account"}
    assert object_for_id("999X0000000001AAA", idx) is None


def test_object_for_id_handles_empty_input():
    idx = {"001": "Account"}
    assert object_for_id(None, idx) is None
    assert object_for_id("", idx) is None
    assert object_for_id("01", idx) is None  # too short


if __name__ == "__main__":
    import sys
    from tests._runner import run
    sys.exit(run(globals()))
