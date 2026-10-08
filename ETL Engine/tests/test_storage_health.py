"""Tests for local storage health scanning and safe cleanup."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests._runner import ensure_project_root_on_path, run

ensure_project_root_on_path()

from core.storage_health import cleanup_storage_items, extract_linked_run_key, scan_storage_health


def _write(path: Path, content: bytes = b"x" * 100) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_extract_linked_run_key_from_common_names():
    assert extract_linked_run_key("sf_run_20260730_150656_646686") == "sf_run_20260730_150656_646686"
    assert extract_linked_run_key("run_sf_sf_run_20260730_150656_646686") == "sf_run_20260730_150656_646686"
    assert extract_linked_run_key("pipeline_prevalidation_20260806_142041_e6d0fa80").startswith("pipeline_prevalidation_")


def test_scan_marks_active_and_protected_items():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        verify_dir = root / "logs" / "verify" / "pipeline_prevalidation_done_1"
        active_dir = root / "logs" / "verify" / "sf_run_active_1"
        _write(verify_dir / "clean.csv", b"a" * 2048)
        _write(active_dir / "clean.csv", b"b" * 4096)
        _write(root / "state" / "run_audit.duckdb", b"audit")
        _write(root / "state" / "run_sf_sf_run_done_1_staging.duckdb", b"db" * 500)
        _write(root / "state" / "flat_file_drop" / "Account" / "Account.csv", b"drop")
        _write(root / "state" / "flat_file_uploads" / "batch123" / "Account_clean.csv", b"c" * 1024)

        report = scan_storage_health(
            root,
            audit_rows=[
                {"run_key": "pipeline_prevalidation_done_1", "status": "COMPLETED"},
                {"run_key": "sf_run_active_1", "status": "RUNNING"},
                {"run_key": "sf_run_done_1", "status": "COMPLETED"},
            ],
            active_run_keys={"sf_run_active_1"},
        )

        by_path = {item["relative_path"]: item for item in report["items"]}
        assert by_path["logs/verify/pipeline_prevalidation_done_1"]["cleanup_allowed"] is True
        assert by_path["logs/verify/sf_run_active_1"]["cleanup_allowed"] is False
        assert by_path["state/run_audit.duckdb"]["cleanup_allowed"] is False
        assert by_path["state/flat_file_drop/Account"]["cleanup_allowed"] is False
        assert by_path["state/flat_file_uploads/batch123"]["cleanup_allowed"] is True
        assert report["totals"]["reclaimable_bytes"] > 0


def test_cleanup_deletes_only_allowed_items():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        done = root / "logs" / "verify" / "pipeline_prevalidation_done_2"
        active = root / "logs" / "verify" / "sf_run_active_2"
        audit = root / "state" / "run_audit.duckdb"
        _write(done / "report.json", b"done")
        _write(active / "report.json", b"active")
        _write(audit, b"keep")

        report = scan_storage_health(
            root,
            audit_rows=[
                {"run_key": "pipeline_prevalidation_done_2", "status": "COMPLETED"},
                {"run_key": "sf_run_active_2", "status": "RUNNING"},
            ],
            active_run_keys={"sf_run_active_2"},
        )
        ids = [item["id"] for item in report["items"]]
        result = cleanup_storage_items(
            root,
            ids,
            audit_rows=[
                {"run_key": "pipeline_prevalidation_done_2", "status": "COMPLETED"},
                {"run_key": "sf_run_active_2", "status": "RUNNING"},
            ],
            active_run_keys={"sf_run_active_2"},
        )

        assert not done.exists()
        assert active.exists()
        assert audit.exists()
        assert result["freed_bytes"] > 0
        assert result["deleted"]
        assert result["skipped"]


if __name__ == "__main__":
    raise SystemExit(run(globals()))
