"""
Content hashing helpers used by image dedup (Mechanism A — manifest).

A 'content hash' is a deterministic fingerprint of an image's raw bytes.
The image manifest (PR 4) keys cached uploads by this hash so that
re-runs short-circuit POSTs when the same bytes have already been
uploaded for the same (source_kav_id, source_refid).

Format:
    sha256:<64 lowercase hex chars>

64 hex chars is plenty of collision resistance for image bodies measured
in MB; we don't need a faster hash, so this is the simplest correct choice.
"""

from __future__ import annotations
import hashlib

HASH_PREFIX = "sha256:"
HASH_HEX_LEN = 64


def content_hash(data: bytes) -> str:
    """Return a stable 'sha256:<hex>' content hash for `data`."""
    if data is None:
        raise ValueError("content_hash() requires bytes, got None")
    return f"{HASH_PREFIX}{hashlib.sha256(data).hexdigest()}"


def is_content_hash(value: str | None) -> bool:
    """True if `value` is a well-formed sha256 content hash string."""
    if not value or not isinstance(value, str):
        return False
    if not value.startswith(HASH_PREFIX):
        return False
    rest = value[len(HASH_PREFIX) :]
    if len(rest) != HASH_HEX_LEN:
        return False
    return all(c in "0123456789abcdef" for c in rest)
