"""
State Manager — persists run state across sessions.

Tracks pipeline and object status so migrations can be:
  - Resumed after failure (skip COMPLETED, retry FAILED)
  - Retried at pipeline or object level
  - Audited (when did each step run, how many records)

State file: state/run_state.json
Audit log:  logs/audit_{migration_name}_{timestamp}.json
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from enum import Enum

logger = logging.getLogger(__name__)

STATE_DIR = Path(__file__).parent.parent / "state"
LOGS_DIR = Path(__file__).parent.parent / "logs"
STATE_FILE = STATE_DIR / "run_state.json"


class Status(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"
    SKIPPED = "SKIPPED"
    BLOCKED = "BLOCKED"       # upstream pipeline failed


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateManager:
    def __init__(self, state_file: Path = STATE_FILE):
        override_path = (os.environ.get("ETL_RUN_STATE_FILE") or "").strip()
        self.state_file = Path(override_path) if override_path else state_file
        STATE_DIR.mkdir(exist_ok=True)
        LOGS_DIR.mkdir(exist_ok=True)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state: dict = self._load()

    # ------------------------------------------------------------------ #
    #  Persist / load                                                     #
    # ------------------------------------------------------------------ #

    def _load(self) -> dict:
        if self.state_file.exists():
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        return {"migration": None, "started_at": None, "pipelines": {}}

    def _save(self):
        self.state_file.write_text(
            json.dumps(self._state, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def init_run(self, migration_name: str, fresh: bool = False, run_key: str | None = None):
        """
        Initialize (or resume) a run.
        fresh=True clears all existing state.
        """
        existing_run_key = str(self._state.get("run_key") or "").strip()
        normalized_run_key = str(run_key or "").strip()
        should_reset = (
            fresh
            or not self._state.get("migration")
            or (normalized_run_key and existing_run_key != normalized_run_key)
        )

        if should_reset:
            self._state = {
                "migration": migration_name,
                "run_key": normalized_run_key or None,
                "started_at": _now(),
                "last_updated": _now(),
                "pipelines": {},
            }
            self._save()
            logger.info(
                f"[State] Fresh run initialized: {migration_name}"
                + (f" | run_key={normalized_run_key}" if normalized_run_key else "")
            )
        else:
            if normalized_run_key and not existing_run_key:
                self._state["run_key"] = normalized_run_key
                self._state["last_updated"] = _now()
                self._save()
            logger.info(f"[State] Resuming run: {self._state['migration']}")

    # ------------------------------------------------------------------ #
    #  Pipeline state                                                     #
    # ------------------------------------------------------------------ #

    def _pipeline(self, pipeline_id: str) -> dict:
        if pipeline_id not in self._state["pipelines"]:
            self._state["pipelines"][pipeline_id] = {
                "status": Status.PENDING,
                "started_at": None,
                "completed_at": None,
                "objects": {},
            }
        return self._state["pipelines"][pipeline_id]

    def mark_pipeline_running(self, pipeline_id: str):
        p = self._pipeline(pipeline_id)
        p["status"] = Status.RUNNING
        p["started_at"] = _now()
        self._state["last_updated"] = _now()
        self._save()

    def set_pipeline_artifact_dir_name(self, pipeline_id: str, dir_name: str):
        p = self._pipeline(pipeline_id)
        p["artifact_dir_name"] = dir_name
        self._state["last_updated"] = _now()
        self._save()

    def mark_pipeline_completed(self, pipeline_id: str):
        p = self._pipeline(pipeline_id)
        p["status"] = Status.COMPLETED
        p["completed_at"] = _now()
        self._state["last_updated"] = _now()
        self._save()
        logger.info(f"[State] Pipeline COMPLETED: {pipeline_id}")

    def mark_pipeline_failed(self, pipeline_id: str, error: str = ""):
        p = self._pipeline(pipeline_id)
        p["status"] = Status.FAILED
        p["error"] = error
        p["failed_at"] = _now()
        self._state["last_updated"] = _now()
        self._save()
        logger.error(f"[State] Pipeline FAILED: {pipeline_id} | {error}")

    def mark_pipeline_stopped(self, pipeline_id: str, error: str = ""):
        p = self._pipeline(pipeline_id)
        p["status"] = Status.STOPPED
        p["error"] = error
        p["stopped_at"] = _now()
        self._state["last_updated"] = _now()
        self._save()
        logger.warning(f"[State] Pipeline STOPPED: {pipeline_id} | {error}")

    def mark_pipeline_skipped(self, pipeline_id: str):
        p = self._pipeline(pipeline_id)
        p["status"] = Status.SKIPPED
        self._save()

    def get_pipeline_status(self, pipeline_id: str) -> Status:
        return Status(self._pipeline(pipeline_id).get("status", Status.PENDING))

    def is_pipeline_completed(self, pipeline_id: str) -> bool:
        return self.get_pipeline_status(pipeline_id) == Status.COMPLETED

    # ------------------------------------------------------------------ #
    #  Object state                                                       #
    # ------------------------------------------------------------------ #

    def _object(self, pipeline_id: str, object_name: str) -> dict:
        p = self._pipeline(pipeline_id)
        if object_name not in p["objects"]:
            p["objects"][object_name] = {
                "status": Status.PENDING,
                "extracted": 0,
                "transformed": 0,
                "loaded_success": 0,
                "loaded_failed": 0,
                "failed_file": None,
                "success_file": None,
                "started_at": None,
                "completed_at": None,
            }
        return p["objects"][object_name]

    def mark_object_running(self, pipeline_id: str, object_name: str):
        obj = self._object(pipeline_id, object_name)
        obj["status"] = Status.RUNNING
        obj["started_at"] = _now()
        self._save()

    def mark_object_extracted(self, pipeline_id: str, object_name: str, count: int):
        obj = self._object(pipeline_id, object_name)
        obj["extracted"] = count
        self._save()

    def mark_object_transformed(self, pipeline_id: str, object_name: str, count: int):
        obj = self._object(pipeline_id, object_name)
        obj["transformed"] = count
        self._save()

    def mark_object_stopped(self, pipeline_id: str, object_name: str, reason: str = ""):
        obj = self._object(pipeline_id, object_name)
        obj["status"] = Status.STOPPED
        obj["error"] = reason
        obj["load_in_progress"] = False
        obj["stopped_at"] = _now()
        self._save()

    def mark_object_completed(
        self,
        pipeline_id: str,
        object_name: str,
        loaded_success: int,
        loaded_failed: int,
        failed_file: str | None = None,
        success_file: str | None = None,
    ):
        obj = self._object(pipeline_id, object_name)
        obj["status"] = Status.COMPLETED
        obj["loaded_success"] = loaded_success
        obj["loaded_failed"] = loaded_failed
        obj["failed_file"] = failed_file
        obj["success_file"] = success_file
        obj["load_in_progress"] = False
        obj["completed_at"] = _now()
        self._save()
        logger.info(
            f"[State] Object COMPLETED: {object_name} | "
            f"success={loaded_success:,} | failed={loaded_failed:,}"
        )

    def mark_object_load_progress(
        self,
        pipeline_id: str,
        object_name: str,
        loaded_success: int,
        loaded_failed: int,
        failed_file: str | None = None,
        success_file: str | None = None,
    ):
        """Publish running load counts and the failed CSV as soon as a batch fails."""
        obj = self._object(pipeline_id, object_name)
        obj["status"] = Status.RUNNING
        obj["loaded_success"] = loaded_success
        obj["loaded_failed"] = loaded_failed
        obj["load_in_progress"] = True
        if failed_file:
            obj["failed_file"] = failed_file
        if success_file:
            obj["success_file"] = success_file
        self._save()

    def apply_cyclic_outcome(
        self,
        pipeline_id: str,
        object_name: str,
        failed_record_count: int,
        failed_file: str | None = None,
    ):
        """Include Pass 2 relationship failures in the user-visible load result."""
        if failed_record_count <= 0:
            return
        obj = self._object(pipeline_id, object_name)
        obj["loaded_failed"] = int(obj.get("loaded_failed") or 0) + failed_record_count
        obj["loaded_success"] = max(
            0, int(obj.get("loaded_success") or 0) - failed_record_count
        )
        obj["cyclic_failed"] = failed_record_count
        if failed_file:
            obj["failed_file"] = failed_file
            obj["cyclic_failed_file"] = failed_file
        self._save()
        logger.warning(
            f"[State] Pass 2 failures included for {object_name} | "
            f"success={obj['loaded_success']:,} | failed={obj['loaded_failed']:,}"
        )

    def mark_object_failed(self, pipeline_id: str, object_name: str, error: str):
        obj = self._object(pipeline_id, object_name)
        obj["status"] = Status.FAILED
        obj["error"] = error
        obj["failed_at"] = _now()
        self._save()
        logger.error(f"[State] Object FAILED: {object_name} | {error}")

    def mark_object_stopped(self, pipeline_id: str, object_name: str, error: str):
        obj = self._object(pipeline_id, object_name)
        obj["status"] = Status.STOPPED
        obj["error"] = error
        obj["stopped_at"] = _now()
        self._save()
        logger.warning(f"[State] Object STOPPED: {object_name} | {error}")

    def is_object_completed(self, pipeline_id: str, object_name: str) -> bool:
        return (
            Status(self._object(pipeline_id, object_name).get("status", Status.PENDING))
            == Status.COMPLETED
        )

    # ------------------------------------------------------------------ #
    #  Run summary                                                        #
    # ------------------------------------------------------------------ #

    def get_summary(self) -> dict:
        return self._state

    def print_summary(self):
        from rich.console import Console
        from rich.table import Table

        console = Console()
        state = self._state

        console.print(f"\n[bold cyan]Migration: {state.get('migration', 'Unknown')}[/bold cyan]")
        console.print(f"Started: {state.get('started_at', '-')}")
        console.print(f"Last updated: {state.get('last_updated', '-')}\n")

        for pid, pdata in state.get("pipelines", {}).items():
            status = pdata.get("status", "PENDING")
            color = {"COMPLETED": "green", "FAILED": "red", "RUNNING": "yellow"}.get(status, "white")
            console.print(f"[bold {color}]Pipeline: {pid} → {status}[/bold {color}]")

            table = Table(show_header=True, header_style="bold")
            table.add_column("Object")
            table.add_column("Status")
            table.add_column("Extracted", justify="right")
            table.add_column("Loaded ✓", justify="right")
            table.add_column("Failed ✗", justify="right")

            for oname, odata in pdata.get("objects", {}).items():
                ostatus = odata.get("status", "PENDING")
                ocolor = {"COMPLETED": "green", "FAILED": "red", "RUNNING": "yellow"}.get(ostatus, "white")
                table.add_row(
                    oname,
                    f"[{ocolor}]{ostatus}[/{ocolor}]",
                    str(odata.get("extracted", 0)),
                    str(odata.get("loaded_success", 0)),
                    str(odata.get("loaded_failed", 0)),
                )
            console.print(table)
            console.print()

    def reset_pipeline(self, pipeline_id: str):
        """Reset a pipeline and all its objects to PENDING (for explicit re-run)."""
        if pipeline_id in self._state.get("pipelines", {}):
            self._state["pipelines"][pipeline_id] = {
                "status": Status.PENDING,
                "started_at": None,
                "completed_at": None,
                "objects": {},
                "artifact_dir_name": self._state["pipelines"][pipeline_id].get("artifact_dir_name"),
            }
            self._save()
            logger.info(f"[State] Pipeline reset to PENDING: {pipeline_id}")
