"""
Persistent image rehosting manifest.

Why we need it
--------------
Re-running the post_load hook for the same article must not re-upload the
same binary every time. Bulk API upload limits and rich-text image-quota
limits are tight, so each image MUST hit the wire at most once per
(target_kav, content_hash, target_field).

Storage
-------
A small SQLite file under `state/`. We use SQLite (not DuckDB like the
stager) because it is built in to the standard library, has cheap point
inserts/lookups, and is the de-facto choice for "long-lived state shared
across runs". DuckDB is great for analytical scans of the staged data
but overkill for ~1 row per image.

Key invariants
--------------
  * `target_kav_id + target_field + content_hash` is a UNIQUE key.
    Two source articles whose bodies reference the same image bytes will
    each get their own `target_kav_id` row pointing at independent
    contentReferenceIds — Salesforce scopes images to a record's rich-text
    field, so we cannot share a refid across articles.
  * `target_refid` (the contentReferenceId) is the answer the rewriter
    needs.

The manifest is intentionally append-only. Stale entries don't cause
correctness bugs — they just waste lookups — and rebuilding a fresh
manifest is always safe (`reset()` for tests / disaster recovery).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

STATE_DIR = Path(__file__).parent.parent / "state"
DEFAULT_MANIFEST_PATH = STATE_DIR / "ka_image_manifest.sqlite"


@dataclass(frozen=True)
class ImageManifestEntry:
    """
    A single cached image upload result.

    Two image kinds are supported. Which fields are populated depends on
    `kind`:

      kind="rta"
        target_eid / target_feoid / target_refid carry the rtaImage
        coordinates. ContentVersion fields are empty strings.

      kind="content_version"
        target_content_version_id (068...) and target_content_document_id
        (069...) identify the new Salesforce File on the target. rta
        fields are empty strings (target_eid is set to the article id
        purely as a back-pointer).
    """
    target_kav_id: str
    target_field: str
    content_hash: str
    target_eid: str
    target_feoid: str
    target_refid: str
    source_kav_id: str | None
    source_refid: str | None
    created_at: str  # ISO 8601
    kind: str = "rta"  # "rta" | "content_version"
    target_content_version_id: str = ""
    target_content_document_id: str = ""
    source_content_version_id: str = ""


class ImageManifest:
    """
    SQLite-backed image manifest. Thread-safe for concurrent reads/writes
    via a single re-entrant lock — image rehosting is I/O-bound so the
    GIL contention is irrelevant.
    """

    _SCHEMA_SQL = """
        CREATE TABLE IF NOT EXISTS images (
            target_kav_id   TEXT NOT NULL,
            target_field    TEXT NOT NULL,
            content_hash    TEXT NOT NULL,
            target_eid      TEXT NOT NULL,
            target_feoid    TEXT NOT NULL,
            target_refid    TEXT NOT NULL,
            source_kav_id   TEXT,
            source_refid    TEXT,
            created_at      TEXT NOT NULL,
            kind                       TEXT NOT NULL DEFAULT 'rta',
            target_content_version_id  TEXT NOT NULL DEFAULT '',
            target_content_document_id TEXT NOT NULL DEFAULT '',
            source_content_version_id  TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (target_kav_id, target_field, content_hash)
        );
    """

    # Columns added in later schema versions; we ALTER TABLE on startup so
    # manifests created by older releases keep working without a manual
    # migration step.
    _MIGRATIONS = (
        ("kind", "TEXT NOT NULL DEFAULT 'rta'"),
        ("target_content_version_id", "TEXT NOT NULL DEFAULT ''"),
        ("target_content_document_id", "TEXT NOT NULL DEFAULT ''"),
        ("source_content_version_id", "TEXT NOT NULL DEFAULT ''"),
    )

    def __init__(self, path: str | Path = DEFAULT_MANIFEST_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # Tracked so `record()` can skip its auto-commit when the caller
        # has opened an explicit `transaction()` block.
        self._in_transaction = False
        with self._lock:
            self._conn.executescript(self._SCHEMA_SQL)
            self._migrate_schema()
            self._conn.commit()

    def _migrate_schema(self) -> None:
        """Add columns introduced after the initial schema, idempotently."""
        existing = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(images)").fetchall()
        }
        for col_name, col_def in self._MIGRATIONS:
            if col_name not in existing:
                self._conn.execute(
                    f"ALTER TABLE images ADD COLUMN {col_name} {col_def}"
                )

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    # ---------------------------------------------------------------- #
    #  Read                                                             #
    # ---------------------------------------------------------------- #

    _SELECT_COLUMNS = (
        "target_kav_id, target_field, content_hash, "
        "target_eid, target_feoid, target_refid, "
        "source_kav_id, source_refid, created_at, "
        "kind, target_content_version_id, target_content_document_id, "
        "source_content_version_id"
    )

    def lookup(
        self,
        *,
        target_kav_id: str,
        target_field: str,
        content_hash: str,
    ) -> ImageManifestEntry | None:
        """
        Return the cached upload result for this (article, field, hash)
        triple, or None if we've never uploaded these bytes to that
        target article's rich-text field.
        """
        if not (target_kav_id and target_field and content_hash):
            return None
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT {self._SELECT_COLUMNS}
                FROM images
                WHERE target_kav_id = ?
                  AND target_field = ?
                  AND content_hash = ?
                """,
                [target_kav_id, target_field, content_hash],
            ).fetchone()
        if not row:
            return None
        return ImageManifestEntry(*row)

    def lookup_by_hash(
        self, content_hash: str
    ) -> list[ImageManifestEntry]:
        """
        Return every cached upload for the given content hash, across all
        target articles + fields. Useful for cross-article reuse logging
        or post-run reporting.
        """
        if not content_hash:
            return []
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT {self._SELECT_COLUMNS}
                FROM images
                WHERE content_hash = ?
                """,
                [content_hash],
            ).fetchall()
        return [ImageManifestEntry(*row) for row in rows]

    def total_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT count(*) FROM images").fetchone()
        return int(row[0] if row else 0)

    # ---------------------------------------------------------------- #
    #  Write                                                            #
    # ---------------------------------------------------------------- #

    def record(
        self,
        *,
        target_kav_id: str,
        target_field: str,
        content_hash: str,
        target_eid: str = "",
        target_feoid: str = "",
        target_refid: str = "",
        source_kav_id: str | None = None,
        source_refid: str | None = None,
        kind: str = "rta",
        target_content_version_id: str = "",
        target_content_document_id: str = "",
        source_content_version_id: str = "",
    ) -> ImageManifestEntry:
        """
        Persist a successful image upload. INSERT OR REPLACE so re-runs
        with the same triple update the row in place (e.g. if the target
        org's feoid changes after a metadata refresh).

        For `kind="rta"` we require target_eid + target_refid (the
        rtaImage coordinates). For `kind="content_version"` we require
        target_content_version_id (the new Salesforce File Id).
        """
        if not (target_kav_id and target_field and content_hash):
            raise ValueError(
                "ImageManifest.record() requires non-empty target_kav_id, "
                "target_field, and content_hash"
            )

        if kind == "rta":
            if not (target_eid and target_refid):
                raise ValueError(
                    "ImageManifest.record(kind='rta') requires non-empty "
                    "target_eid and target_refid"
                )
        elif kind == "content_version":
            if not target_content_version_id:
                raise ValueError(
                    "ImageManifest.record(kind='content_version') requires "
                    "non-empty target_content_version_id"
                )
            # target_eid is a back-pointer to the article so legacy
            # rta-shaped consumers still see the article id; default to it
            # if the caller didn't supply one.
            if not target_eid:
                target_eid = target_kav_id
        else:
            raise ValueError(
                f"ImageManifest.record(): unknown kind={kind!r}"
            )

        created_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO images (
                    target_kav_id, target_field, content_hash,
                    target_eid, target_feoid, target_refid,
                    source_kav_id, source_refid, created_at,
                    kind, target_content_version_id,
                    target_content_document_id, source_content_version_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(target_kav_id, target_field, content_hash) DO UPDATE SET
                    target_eid                 = excluded.target_eid,
                    target_feoid               = excluded.target_feoid,
                    target_refid               = excluded.target_refid,
                    source_kav_id              = excluded.source_kav_id,
                    source_refid               = excluded.source_refid,
                    created_at                 = excluded.created_at,
                    kind                       = excluded.kind,
                    target_content_version_id  = excluded.target_content_version_id,
                    target_content_document_id = excluded.target_content_document_id,
                    source_content_version_id  = excluded.source_content_version_id
                """,
                [
                    target_kav_id, target_field, content_hash,
                    target_eid, target_feoid, target_refid,
                    source_kav_id, source_refid, created_at,
                    kind, target_content_version_id,
                    target_content_document_id, source_content_version_id,
                ],
            )
            # When inside a manual transaction the surrounding context
            # manager commits / rolls back. Don't auto-commit here or
            # rollback would have nothing to undo.
            if not self._in_transaction:
                self._conn.commit()
        return ImageManifestEntry(
            target_kav_id=target_kav_id,
            target_field=target_field,
            content_hash=content_hash,
            target_eid=target_eid,
            target_feoid=target_feoid,
            target_refid=target_refid,
            source_kav_id=source_kav_id,
            source_refid=source_refid,
            created_at=created_at,
            kind=kind,
            target_content_version_id=target_content_version_id,
            target_content_document_id=target_content_document_id,
            source_content_version_id=source_content_version_id,
        )

    # ---------------------------------------------------------------- #
    #  Maintenance                                                      #
    # ---------------------------------------------------------------- #

    def reset(self) -> None:
        """Delete every entry. Test-only / disaster recovery."""
        with self._lock:
            self._conn.execute("DELETE FROM images")
            self._conn.commit()

    @contextmanager
    def transaction(self) -> Iterator["ImageManifest"]:
        """
        Group multiple `record()` calls into a single SQLite commit. Useful
        for the rehoster's per-article batch — a crash mid-batch leaves
        the manifest consistent (either all uploads visible or none).

        Re-entrant: nested `transaction()` blocks share the outermost
        BEGIN/COMMIT so callers don't need to know whether they're already
        inside one.
        """
        with self._lock:
            if self._in_transaction:
                # Re-entrant: piggyback on the outer transaction.
                yield self
                return

            self._conn.execute("BEGIN")
            self._in_transaction = True
            try:
                yield self
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            finally:
                self._in_transaction = False
