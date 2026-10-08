"""
Unit tests for knowledge_article._hashing.

Run any of:
    python tests/test_hashing.py
    python -m pytest tests/test_hashing.py
"""

from __future__ import annotations

from tests._runner import ensure_project_root_on_path
ensure_project_root_on_path()

from knowledge_article._hashing import content_hash, is_content_hash


def test_content_hash_deterministic():
    a = content_hash(b"hello world")
    b = content_hash(b"hello world")
    assert a == b


def test_content_hash_different_for_different_bytes():
    assert content_hash(b"hello") != content_hash(b"world")


def test_content_hash_format():
    h = content_hash(b"")
    assert h.startswith("sha256:")
    assert len(h) == len("sha256:") + 64


def test_content_hash_rejects_none():
    raised = False
    try:
        content_hash(None)  # type: ignore[arg-type]
    except ValueError:
        raised = True
    assert raised


def test_is_content_hash_accepts_well_formed():
    assert is_content_hash(content_hash(b"x"))


def test_is_content_hash_rejects_bad_inputs():
    assert not is_content_hash("")
    assert not is_content_hash(None)
    assert not is_content_hash("md5:" + "a" * 32)
    assert not is_content_hash("sha256:" + "g" * 64)  # non-hex
    assert not is_content_hash("sha256:" + "a" * 63)  # wrong length


if __name__ == "__main__":
    import sys
    from tests._runner import run
    sys.exit(run(globals()))
