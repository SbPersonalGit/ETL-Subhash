"""Persist and evaluate Pipeline Designer scheduled migrations."""

from __future__ import annotations

import copy
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEDULE_FILENAME = "pipeline_schedule.json"
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
ACTIVE_STATUSES = frozenset({"queued", "running"})
STAGE_MODES = frozenset({"single_run", "separate_runs"})

_LOCK = threading.Lock()


def schedule_path(project_root: str | Path) -> Path:
    return Path(project_root) / "state" / SCHEDULE_FILENAME


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_run_at(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def strip_migration_suffix(value: str | None) -> str:
    text = str(value or "").strip()
    for suffix in (" - Migration", " - Pre-validation", " - Post Validation"):
        if text.endswith(suffix):
            return text[: -len(suffix)].strip()
    return text


def empty_store() -> dict[str, Any]:
    return {"jobs": []}


def load_store(project_root: str | Path) -> dict[str, Any]:
    path = schedule_path(project_root)
    if not path.is_file():
        return empty_store()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_store()
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        return empty_store()
    return {"jobs": [job for job in jobs if isinstance(job, dict)]}


def save_store(project_root: str | Path, store: dict[str, Any]) -> None:
    path = schedule_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(path)


def list_jobs(project_root: str | Path) -> list[dict[str, Any]]:
    with _LOCK:
        return copy.deepcopy(load_store(project_root).get("jobs") or [])


def get_job(project_root: str | Path, job_id: str) -> dict[str, Any] | None:
    wanted = str(job_id or "").strip()
    if not wanted:
        return None
    for job in list_jobs(project_root):
        if str(job.get("id") or "") == wanted:
            return job
    return None


def remaining_pipelines(job: dict[str, Any]) -> list[dict[str, Any]]:
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    pipelines = payload.get("pipelines") if isinstance(payload.get("pipelines"), list) else []
    start_from = max(0, int(job.get("start_from_index") or 0))
    return [item for item in pipelines[start_from:] if isinstance(item, dict)]


def remaining_stage_count(job: dict[str, Any]) -> int:
    remaining = remaining_pipelines(job)
    if str(job.get("stage_mode") or "single_run") != "separate_runs":
        return 1 if remaining else 0
    return len(remaining)


def job_has_more_stages(job: dict[str, Any]) -> bool:
    if str(job.get("stage_mode") or "single_run") != "separate_runs":
        return False
    return int(job.get("stage_index") or 0) + 1 < remaining_stage_count(job)


def clock_ready(job: dict[str, Any], now: datetime | None = None) -> bool:
    run_at = parse_run_at(job.get("run_at"))
    if run_at is None:
        return True
    current = now or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current >= run_at


def job_ready_to_start(job: dict[str, Any], *, busy: bool, now: datetime | None = None) -> bool:
    status = str(job.get("status") or "").strip().lower()
    if status not in ACTIVE_STATUSES:
        return False
    if job.get("launching"):
        return False
    if str(job.get("active_run_key") or "").strip():
        return False
    if not clock_ready(job, now):
        return False
    return not busy


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    pipelines = remaining_pipelines(job)
    stage_names = [
        str(item.get("pipeline_name") or item.get("pipeline_id") or f"stage_{index}")
        for index, item in enumerate(pipelines, start=1)
    ]
    return {
        "id": job.get("id"),
        "label": job.get("label"),
        "status": job.get("status"),
        "after_current": bool(job.get("after_current")),
        "run_at": job.get("run_at"),
        "stage_mode": job.get("stage_mode") or "single_run",
        "start_from_index": int(job.get("start_from_index") or 0),
        "stage_index": int(job.get("stage_index") or 0),
        "snapshot_id": job.get("snapshot_id") or payload.get("snapshot_id") or "",
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "started_at": job.get("started_at"),
        "completed_at": job.get("completed_at"),
        "error": job.get("error"),
        "run_keys": list(job.get("run_keys") or []),
        "active_run_key": job.get("active_run_key") or "",
        "pipeline_count": len(pipelines),
        "stage_names": stage_names,
        "launching": bool(job.get("launching")),
    }


def validate_new_job(data: dict[str, Any]) -> dict[str, Any]:
    payload = data.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Scheduled job requires a pipeline run payload.")
    pipelines = payload.get("pipelines")
    if not isinstance(pipelines, list) or not pipelines:
        raise ValueError("Add at least one pipeline stage before scheduling.")
    after_current = bool(data.get("after_current"))
    run_at_raw = str(data.get("run_at") or "").strip() or None
    if run_at_raw:
        parsed = parse_run_at(run_at_raw)
        if parsed is None:
            raise ValueError("Scheduled time is invalid.")
        run_at_raw = parsed.isoformat()
    if not after_current and not run_at_raw:
        raise ValueError("Choose a start time, wait for the current run, or both.")
    stage_mode = str(data.get("stage_mode") or "single_run").strip().lower()
    if stage_mode not in STAGE_MODES:
        raise ValueError("Stage mode must be single_run or separate_runs.")
    start_from_index = int(data.get("start_from_index") or 0)
    if start_from_index < 0 or start_from_index >= len(pipelines):
        raise ValueError("Start-from stage is out of range.")
    snapshot_id = str(data.get("snapshot_id") or payload.get("snapshot_id") or "").strip()
    label = str(data.get("label") or payload.get("migration_name") or snapshot_id or "Scheduled pipeline").strip()
    return {
        "after_current": after_current,
        "run_at": run_at_raw,
        "stage_mode": stage_mode,
        "start_from_index": start_from_index,
        "snapshot_id": snapshot_id,
        "label": label,
        "payload": copy.deepcopy(payload),
    }


def create_job(project_root: str | Path, data: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    validated = validate_new_job(data)
    current = now or utc_now()
    stamp = current.strftime("%Y%m%d_%H%M%S")
    job = {
        "id": f"psched_{stamp}_{uuid.uuid4().hex[:8]}",
        "label": validated["label"],
        "status": "queued",
        "after_current": validated["after_current"],
        "run_at": validated["run_at"],
        "stage_mode": validated["stage_mode"],
        "start_from_index": validated["start_from_index"],
        "stage_index": 0,
        "snapshot_id": validated["snapshot_id"],
        "payload": validated["payload"],
        "run_keys": [],
        "active_run_key": "",
        "launching": False,
        "error": None,
        "created_at": current.isoformat(),
        "updated_at": current.isoformat(),
        "started_at": None,
        "completed_at": None,
    }
    with _LOCK:
        store = load_store(project_root)
        store.setdefault("jobs", []).append(job)
        save_store(project_root, store)
    return copy.deepcopy(job)


def update_job(project_root: str | Path, job_id: str, changes: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any] | None:
    wanted = str(job_id or "").strip()
    current = now or utc_now()
    with _LOCK:
        store = load_store(project_root)
        updated = None
        for index, job in enumerate(store.get("jobs") or []):
            if str(job.get("id") or "") != wanted:
                continue
            merged = {**job, **changes, "updated_at": current.isoformat()}
            store["jobs"][index] = merged
            updated = copy.deepcopy(merged)
            break
        if updated is None:
            return None
        save_store(project_root, store)
        return updated


def cancel_job(project_root: str | Path, job_id: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    job = get_job(project_root, job_id)
    if job is None:
        return None
    status = str(job.get("status") or "").strip().lower()
    if status in TERMINAL_STATUSES:
        return job
    return update_job(
        project_root,
        job_id,
        {
            "status": "cancelled",
            "launching": False,
            "completed_at": (now or utc_now()).isoformat(),
            "error": "Cancelled from Pipeline Designer. Any already-started run is left running.",
        },
        now=now,
    )


def build_fire_payload(job: dict[str, Any], *, unique_suffix: str) -> dict[str, Any]:
    payload = copy.deepcopy(job.get("payload") or {})
    if not isinstance(payload, dict):
        raise ValueError("Scheduled job payload is invalid.")
    remaining = remaining_pipelines(job)
    if not remaining:
        raise ValueError("Scheduled job has no remaining stages.")
    stage_mode = str(job.get("stage_mode") or "single_run")
    stage_index = int(job.get("stage_index") or 0)
    base = strip_migration_suffix(
        payload.get("migration_name") or job.get("label") or "scheduled"
    ) or "scheduled"
    if stage_mode == "separate_runs":
        if stage_index < 0 or stage_index >= len(remaining):
            raise ValueError("Scheduled job stage index is out of range.")
        chosen = remaining[stage_index]
        payload["pipelines"] = [chosen]
        stage_name = str(chosen.get("pipeline_name") or chosen.get("pipeline_id") or f"stage_{stage_index + 1}")
        payload["migration_name"] = f"{base} - {stage_name} - {unique_suffix} - Migration"
    else:
        payload["pipelines"] = remaining
        payload["migration_name"] = f"{base} - {unique_suffix} - Migration"
    payload["fresh"] = True
    payload["dry_run"] = False
    if job.get("snapshot_id") and not payload.get("snapshot_id"):
        payload["snapshot_id"] = job["snapshot_id"]
    return payload
