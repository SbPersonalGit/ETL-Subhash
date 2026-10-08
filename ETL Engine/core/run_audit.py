"""
Run audit store for Salesforce-submitted ETL runs.

Persists lightweight execution metadata in DuckDB so we can trace:
  - which Salesforce run/pipeline triggered Python
  - who submitted the run
  - which source/target profiles were used
  - the latest derived execution status
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb


STATE_DIR = Path(__file__).parent.parent / "state"
DEFAULT_DB_PATH = Path(os.environ.get("ETL_RUN_AUDIT_DB") or STATE_DIR / "run_audit.duckdb")
CONNECT_RETRY_SECONDS = float(os.environ.get("ETL_RUN_AUDIT_CONNECT_RETRY_SECONDS") or "10")
CONNECT_RETRY_INTERVAL_SECONDS = 0.1


def _is_retryable_connect_error(exc: Exception) -> bool:
    """Return whether DuckDB failed because another process owns the file."""
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "cannot access the file because it is being used by another process",
            "could not set lock on file",
            "unique file handle conflict",
        )
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunAuditStore:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self._write_lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self):
        # DuckDB coordinates writers across processes, but on Windows a
        # competing open fails immediately instead of waiting. Validation
        # workers, scheduled launchers, and Flask are separate processes, so
        # absorb that short hand-off window here. The retry is deliberately
        # limited to known lock/handle errors; all other failures stay visible.
        deadline = time.monotonic() + max(0.0, CONNECT_RETRY_SECONDS)
        delay = CONNECT_RETRY_INTERVAL_SECONDS
        while True:
            try:
                return duckdb.connect(str(self.db_path))
            except (duckdb.IOException, duckdb.BinderException) as exc:
                if not _is_retryable_connect_error(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 1.5, 0.5)

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sf_run_audit (
                    run_key TEXT,
                    activity_type TEXT,
                    data_operation TEXT,
                    sf_pipeline_run_id TEXT,
                    sf_pipeline_id TEXT,
                    sf_pipeline_attempt TEXT,
                    start_stage TEXT,
                    effective_from_pipeline TEXT,
                    resumed_from_run_key TEXT,
                    migration_name TEXT,
                    source_profile TEXT,
                    target_profile TEXT,
                    submitted_by_user_id TEXT,
                    submitted_by_username TEXT,
                    submitted_by_name TEXT,
                    submitted_by_email TEXT,
                    callback_url TEXT,
                    callback_enabled BOOLEAN,
                    status TEXT,
                    request_payload_json TEXT,
                    status_payload_json TEXT,
                    runner_json TEXT,
                    created_at TEXT,
                    started_at TEXT,
                    last_updated_at TEXT,
                    completed_at TEXT,
                    error_message TEXT
                )
                """
            )
            for statement in (
                "ALTER TABLE sf_run_audit ADD COLUMN IF NOT EXISTS sf_pipeline_attempt TEXT",
                "ALTER TABLE sf_run_audit ADD COLUMN IF NOT EXISTS start_stage TEXT",
                "ALTER TABLE sf_run_audit ADD COLUMN IF NOT EXISTS effective_from_pipeline TEXT",
                "ALTER TABLE sf_run_audit ADD COLUMN IF NOT EXISTS resumed_from_run_key TEXT",
                "ALTER TABLE sf_run_audit ADD COLUMN IF NOT EXISTS activity_type TEXT",
                "ALTER TABLE sf_run_audit ADD COLUMN IF NOT EXISTS data_operation TEXT",
            ):
                conn.execute(statement)

    def upsert_submission(self, run_key: str, record: dict) -> None:
        existing = self.get_run(run_key) or {}
        merged = {
            **existing,
            **record,
            "run_key": run_key,
            "last_updated_at": _utc_now(),
        }
        if not merged.get("created_at"):
            merged["created_at"] = _utc_now()
        if not merged.get("started_at"):
            merged["started_at"] = merged["created_at"]
        self._replace_row(merged)

    def update_status(
        self,
        run_key: str,
        *,
        status: str,
        runner: dict | None = None,
        status_payload: dict | None = None,
        error_message: str | None = None,
        completed_at: str | None = None,
    ) -> None:
        existing = self.get_run(run_key) or {"run_key": run_key, "created_at": _utc_now(), "started_at": _utc_now()}
        existing["status"] = status
        existing["last_updated_at"] = _utc_now()
        if runner is not None:
            existing["runner_json"] = json.dumps(runner, ensure_ascii=False)
        if status_payload is not None:
            existing["status_payload_json"] = json.dumps(status_payload, ensure_ascii=False)
        if error_message is not None:
            existing["error_message"] = error_message
        if completed_at is not None:
            existing["completed_at"] = completed_at
        elif status in {"RUNNING", "QUEUED"}:
            # A stale terminal audit can be repaired when a live worker or
            # progress heartbeat proves the run is still active.
            existing["completed_at"] = None
        elif status in {"COMPLETED", "FAILED", "STOPPED"} and not existing.get("completed_at"):
            existing["completed_at"] = _utc_now()
        self._replace_row(existing)

    def get_run(self, run_key: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sf_run_audit WHERE run_key = ? LIMIT 1",
                [run_key],
            ).fetchone()
            if row is None:
                return None
            columns = [desc[0] for desc in conn.description]
        return dict(zip(columns, row))

    def count_runs(
        self,
        *,
        activity_type: str | None = None,
        data_operation: str | None = None,
        q: str | None = None,
    ) -> int:
        where_sql, params = self._runs_filter_clause(
            activity_type=activity_type,
            data_operation=data_operation,
            q=q,
        )
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM sf_run_audit{where_sql}",
                params,
            ).fetchone()
        return int(row[0] or 0) if row else 0

    def list_runs(
        self,
        limit: int = 200,
        offset: int = 0,
        *,
        activity_type: str | None = None,
        data_operation: str | None = None,
        q: str | None = None,
    ) -> list[dict]:
        safe_limit = max(1, min(int(limit), 1000))
        safe_offset = max(0, int(offset))
        where_sql, params = self._runs_filter_clause(
            activity_type=activity_type,
            data_operation=data_operation,
            q=q,
        )
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM sf_run_audit
                {where_sql}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                [*params, safe_limit, safe_offset],
            ).fetchall()
            columns = [desc[0] for desc in conn.description]
        return [dict(zip(columns, row)) for row in rows]

    @staticmethod
    def _runs_filter_clause(
        *,
        activity_type: str | None = None,
        data_operation: str | None = None,
        q: str | None = None,
    ) -> tuple[str, list]:
        clauses: list[str] = []
        params: list = []
        activity = str(activity_type or "").strip().upper()
        if activity:
            clauses.append("UPPER(COALESCE(activity_type, '')) = ?")
            params.append(activity)
        operation = str(data_operation or "").strip()
        if operation:
            clauses.append("UPPER(COALESCE(data_operation, '')) LIKE ?")
            params.append(f"%{operation.upper()}%")
        query = str(q or "").strip()
        if query:
            like = f"%{query}%"
            clauses.append(
                """(
                    CAST(run_key AS VARCHAR) ILIKE ?
                    OR CAST(COALESCE(migration_name, '') AS VARCHAR) ILIKE ?
                    OR CAST(COALESCE(status, '') AS VARCHAR) ILIKE ?
                    OR CAST(COALESCE(submitted_by_name, '') AS VARCHAR) ILIKE ?
                    OR CAST(COALESCE(submitted_by_username, '') AS VARCHAR) ILIKE ?
                    OR CAST(COALESCE(data_operation, '') AS VARCHAR) ILIKE ?
                    OR CAST(COALESCE(activity_type, '') AS VARCHAR) ILIKE ?
                    OR CAST(COALESCE(target_profile, '') AS VARCHAR) ILIKE ?
                )"""
            )
            params.extend([like] * 8)
        if not clauses:
            return "", []
        return " WHERE " + " AND ".join(clauses), params

    def _replace_row(self, record: dict) -> None:
        columns = [
            "run_key",
            "activity_type",
            "data_operation",
            "sf_pipeline_run_id",
            "sf_pipeline_id",
            "sf_pipeline_attempt",
            "start_stage",
            "effective_from_pipeline",
            "resumed_from_run_key",
            "migration_name",
            "source_profile",
            "target_profile",
            "submitted_by_user_id",
            "submitted_by_username",
            "submitted_by_name",
            "submitted_by_email",
            "callback_url",
            "callback_enabled",
            "status",
            "request_payload_json",
            "status_payload_json",
            "runner_json",
            "created_at",
            "started_at",
            "last_updated_at",
            "completed_at",
            "error_message",
        ]
        values = [record.get(column) for column in columns]
        placeholders = ", ".join(["?"] * len(columns))
        with self._write_lock, self._connect() as conn:
            conn.execute("BEGIN TRANSACTION")
            try:
                conn.execute("DELETE FROM sf_run_audit WHERE run_key = ?", [record["run_key"]])
                conn.execute(
                    f"INSERT INTO sf_run_audit ({', '.join(columns)}) VALUES ({placeholders})",
                    values,
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
