"""Tests for Pipeline Designer scheduled migrations."""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import contextmanager

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests._runner import ensure_project_root_on_path, run

ensure_project_root_on_path()

from core.pipeline_schedule import (
    build_fire_payload,
    cancel_job,
    create_job,
    job_has_more_stages,
    job_ready_to_start,
    list_jobs,
    public_job,
    remaining_stage_count,
)


def _payload(stage_ids: list[str]) -> dict:
    return {
        "migration_name": "Case overnight - Migration",
        "snapshot_id": "Case_UAT_v11_12_13_MRT",
        "start_stage": "transform",
        "pipelines": [
            {"pipeline_id": stage_id, "pipeline_name": stage_id, "input_mode": "flat_file"}
            for stage_id in stage_ids
        ],
    }


@contextmanager
def _workspace(tmp_path: Path | None = None):
    if tmp_path is not None:
        yield tmp_path
        return
    with tempfile.TemporaryDirectory() as folder:
        yield Path(folder)


def test_create_job_requires_trigger(tmp_path: Path | None = None) -> None:
    with _workspace(tmp_path) as root:
        try:
            create_job(root, {"payload": _payload(["Case_UAT_v12"])})
            raise AssertionError("expected missing trigger to fail")
        except ValueError as exc:
            assert "start time" in str(exc).lower() or "current run" in str(exc).lower()


def test_after_current_is_due_only_when_idle(tmp_path: Path | None = None) -> None:
    with _workspace(tmp_path) as root:
        job = create_job(
            root,
            {
                "after_current": True,
                "stage_mode": "separate_runs",
                "payload": _payload(["Case_UAT_v12", "Case_UAT_v13"]),
            },
        )
        assert job["status"] == "queued"
        assert not job_ready_to_start(job, busy=True)
        assert job_ready_to_start(job, busy=False)
        assert remaining_stage_count(job) == 2
        assert job_has_more_stages(job)


def test_run_at_waits_for_clock(tmp_path: Path | None = None) -> None:
    with _workspace(tmp_path) as root:
        now = datetime(2026, 8, 17, 16, 30, tzinfo=timezone.utc)
        later = now + timedelta(hours=2)
        job = create_job(
            root,
            {
                "after_current": False,
                "run_at": later.isoformat(),
                "stage_mode": "single_run",
                "payload": _payload(["Case_UAT_v12"]),
            },
            now=now,
        )
        assert not job_ready_to_start(job, busy=False, now=now)
        assert job_ready_to_start(job, busy=False, now=later)
        assert not job_ready_to_start(job, busy=True, now=later)


def test_separate_runs_fire_one_stage_at_a_time(tmp_path: Path | None = None) -> None:
    with _workspace(tmp_path) as root:
        job = create_job(
            root,
            {
                "after_current": True,
                "stage_mode": "separate_runs",
                "start_from_index": 1,
                "payload": _payload(["Case_UAT_v11", "Case_UAT_v12", "Case_UAT_v13"]),
            },
        )
        first = build_fire_payload(job, unique_suffix="20260817_2230")
        assert [item["pipeline_id"] for item in first["pipelines"]] == ["Case_UAT_v12"]
        assert "Case_UAT_v12" in first["migration_name"]
        assert first["fresh"] is True
        job["stage_index"] = 1
        second = build_fire_payload(job, unique_suffix="20260817_2330")
        assert [item["pipeline_id"] for item in second["pipelines"]] == ["Case_UAT_v13"]
        assert not job_has_more_stages(job)


def test_cancel_queued_job(tmp_path: Path | None = None) -> None:
    with _workspace(tmp_path) as root:
        job = create_job(root, {"after_current": True, "payload": _payload(["Case_UAT_v12"])})
        cancelled = cancel_job(root, job["id"])
        assert cancelled is not None
        assert cancelled["status"] == "cancelled"
        stored = list_jobs(root)
        assert stored[0]["status"] == "cancelled"
        assert public_job(stored[0])["pipeline_count"] == 1


if __name__ == "__main__":
    raise SystemExit(run(globals()))
