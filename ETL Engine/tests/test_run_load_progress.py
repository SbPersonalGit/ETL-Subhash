from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ui.app import (
    _apply_pipeline_outcome_overrides,
    apply_live_load_progress_overlay,
    parse_live_load_progress_from_log,
)


WRAPPED_LOG = """
  <-> Transforming Case (319,004 records)...
  ^ Pass 1 / Attempt 1: Loading Case (level 0, 319,004 records)...
[08/17/26 17:47:56] INFO     [Case] Waiting on ingest job 750S900000X4vskIAB |  
                             poll=30/720 | state=InProgress | processed=6000 |  
                             failed=217                                         
[08/17/26 17:59:06] INFO     [Case] Waiting on ingest job 750S900000X4vskIAB |  
                             poll=150/720 | state=InProgress | processed=50000 |
                             failed=217                                         
[08/17/26 18:14:45] INFO     [Case] Waiting on ingest job 750S900000X4rkJIAR |  
                             poll=150/720 | state=InProgress | processed=50000 |
                             failed=24                                          
[08/17/26 18:17:21] INFO     [Case] Waiting on ingest job 750S900000X4rsUIAR |  
                             poll=18/720 | state=InProgress | processed=4800 |  
                             failed=102                                         
"""

MULTI_STAGE_LOG = """
==> Pipeline: Case_UAT_v11
  <-> Transforming Case (399,328 records)...
==> Pipeline: Case_UAT_v12
  <-> Transforming Case (399,328 records)...
==> Pipeline: Case_UAT_v13
  <-> Transforming Case (399,328 records)...
==> Pipeline: Case_UAT_v11
  ^ Pass 1 / Attempt 1: Loading Case (level 0, 399,328 records)...
[08/17/26 19:41:00] INFO     [Case] Waiting on ingest job 750S900000AAAAAA |  
                             poll=12/720 | state=InProgress | processed=8000 |  
                             failed=10                                          
"""


def test_parse_sums_max_processed_per_ingest_job():
    live = parse_live_load_progress_from_log(WRAPPED_LOG)
    case = live["objects"]["Case"]
    assert case["transformed"] == 319004
    assert case["loaded_processed"] == 104800
    assert case["loaded_failed"] == 343
    assert case["loaded_success"] == 104457
    assert live["phase"] == "load"


def test_live_overlay_does_not_mark_in_progress_load_as_failed():
    payload = {
        "pipelines": {
            "Case_UAT_v10": {
                "status": "RUNNING",
                "objects": {
                    "Case": {
                        "status": "PENDING",
                        "extracted": 0,
                        "transformed": 319004,
                        "loaded_success": 0,
                        "loaded_failed": 0,
                    }
                },
            }
        }
    }
    overlayed = apply_live_load_progress_overlay(
        payload, parse_live_load_progress_from_log(WRAPPED_LOG)
    )
    final = _apply_pipeline_outcome_overrides(overlayed)
    case = final["pipelines"]["Case_UAT_v10"]["objects"]["Case"]
    assert case["status"] == "RUNNING"
    assert case["load_in_progress"] is True
    assert case["loaded_success"] == 104457
    assert case["loaded_failed"] == 343
    assert final["pipelines"]["Case_UAT_v10"]["status"] == "RUNNING"


def _three_stage_payload():
    objects = {
        "Case": {
            "status": "PENDING",
            "extracted": 0,
            "transformed": 399328,
            "loaded_success": 0,
            "loaded_failed": 0,
        }
    }
    return {
        "pipelines": {
            "Case_UAT_v11": {"status": "RUNNING", "name": "Case_UAT_v11", "objects": {"Case": dict(objects["Case"])}},
            "Case_UAT_v12": {"status": "RUNNING", "name": "Case_UAT_v12", "objects": {"Case": dict(objects["Case"])}},
            "Case_UAT_v13": {"status": "RUNNING", "name": "Case_UAT_v13", "objects": {"Case": dict(objects["Case"])}},
        }
    }


def test_multi_stage_log_scopes_load_counts_to_active_pipeline():
    live = parse_live_load_progress_from_log(MULTI_STAGE_LOG)
    assert live["active_pipeline"] == "Case_UAT_v11"
    assert live["phase"] == "load"
    assert live["pipelines"]["Case_UAT_v11"]["Case"]["loaded_success"] == 7990

    overlayed = apply_live_load_progress_overlay(_three_stage_payload(), live)
    final = _apply_pipeline_outcome_overrides(overlayed)
    v11 = final["pipelines"]["Case_UAT_v11"]
    v12 = final["pipelines"]["Case_UAT_v12"]
    v13 = final["pipelines"]["Case_UAT_v13"]
    assert v11["status"] == "RUNNING"
    assert v11["objects"]["Case"]["loaded_success"] == 7990
    assert v11["objects"]["Case"]["load_in_progress"] is True
    assert v12["status"] == "QUEUED"
    assert v13["status"] == "QUEUED"
    assert v12["objects"]["Case"]["loaded_success"] == 0
    assert v13["objects"]["Case"]["status"] == "QUEUED"
    assert final["active_pipeline"] == "Case_UAT_v11"


def test_live_overlay_keeps_failed_file_for_in_progress_download():
    payload = {
        "pipelines": {
            "Case_UAT_v10": {
                "status": "RUNNING",
                "objects": {
                    "Case": {
                        "status": "RUNNING",
                        "extracted": 0,
                        "transformed": 319004,
                        "loaded_success": 49900,
                        "loaded_failed": 100,
                        "failed_file": "logs/run_x/Case_UAT_v10_attempt_1/load_failed/Case_failed.csv",
                        "load_in_progress": True,
                    }
                },
            }
        }
    }
    overlayed = apply_live_load_progress_overlay(
        payload, parse_live_load_progress_from_log(WRAPPED_LOG)
    )
    final = _apply_pipeline_outcome_overrides(overlayed)
    case = final["pipelines"]["Case_UAT_v10"]["objects"]["Case"]
    assert case["failed_file"] == "logs/run_x/Case_UAT_v10_attempt_1/load_failed/Case_failed.csv"
    assert case["load_in_progress"] is True
    assert case["loaded_failed"] > 0
    assert case["status"] == "RUNNING"


def test_mark_object_load_progress_exposes_failed_file_before_completion():
    from core.state_manager import StateManager, Status

    state = StateManager.__new__(StateManager)
    state._state = {"pipelines": {}}
    state._save = lambda: None
    state.mark_object_load_progress(
        "Case_UAT_v11",
        "Case",
        loaded_success=0,
        loaded_failed=50000,
        failed_file="logs/run_x/load_failed/Case_failed.csv",
    )
    record = state._state["pipelines"]["Case_UAT_v11"]["objects"]["Case"]
    assert record["status"] == Status.RUNNING
    assert record["load_in_progress"] is True
    assert record["loaded_failed"] == 50000
    assert record["failed_file"] == "logs/run_x/load_failed/Case_failed.csv"
    assert record.get("completed_at") is None


if __name__ == "__main__":
    import sys
    from tests._runner import run
    sys.exit(run(globals()))
