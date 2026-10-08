from __future__ import annotations

import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ui.app import _compute_prevalidation_progress


def _write_progress(folder: Path, **overrides) -> None:
    payload = {
        "status": "RUNNING",
        "phase": "schema_clean",
        "total_files": 14,
        "completed_files": 0,
        "current_index": 1,
        "current_file": "Account_1.csv",
        "rows_processed": 50,
        "total_rows": 100,
    }
    payload.update(overrides)
    (folder / "prevalidation_progress.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_terminal_audit_does_not_force_running_prevalidation_to_100(tmp_path: Path):
    _write_progress(tmp_path)

    result = _compute_prevalidation_progress(tmp_path, {}, "FAILED")

    assert result["progress_percent"] == 5
    assert result["completed_units"] == 0
    assert result["total_units"] == 14


def test_prevalidation_is_100_only_after_all_files_are_finished(tmp_path: Path):
    _write_progress(
        tmp_path,
        status="ISSUES_FOUND",
        phase="completed",
        completed_files=14,
        current_index=14,
    )

    result = _compute_prevalidation_progress(tmp_path, {}, "FAILED")

    assert result["progress_percent"] == 100


def test_target_checks_do_not_reset_current_file_progress(tmp_path: Path):
    _write_progress(
        tmp_path,
        phase="schema_clean",
        total_files=2,
        rows_processed=100,
        total_rows=100,
    )
    schema_result = _compute_prevalidation_progress(tmp_path, {}, "RUNNING")

    _write_progress(
        tmp_path,
        phase="target_checks",
        total_files=2,
        rows_processed=100,
        total_rows=100,
    )
    target_result = _compute_prevalidation_progress(tmp_path, {}, "RUNNING")

    assert target_result["progress_percent"] >= schema_result["progress_percent"]
    assert target_result["progress_percent"] == 50
