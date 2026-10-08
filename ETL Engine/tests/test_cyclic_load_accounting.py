import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.state_manager import StateManager


def test_pass2_failures_change_visible_object_totals():
    state = StateManager.__new__(StateManager)
    state._state = {
        "pipelines": {
            "CampaignPipeline": {
                "objects": {
                    "Campaign": {
                        "loaded_success": 838,
                        "loaded_failed": 0,
                        "failed_file": None,
                    }
                }
            }
        }
    }
    state._save = lambda: None

    state.apply_cyclic_outcome(
        "CampaignPipeline",
        "Campaign",
        failed_record_count=254,
        failed_file="logs/cyclic_failed.csv",
    )

    record = state._state["pipelines"]["CampaignPipeline"]["objects"]["Campaign"]
    assert record["loaded_success"] == 584
    assert record["loaded_failed"] == 254
    assert record["cyclic_failed"] == 254
    assert record["failed_file"] == "logs/cyclic_failed.csv"
    assert record["cyclic_failed_file"] == "logs/cyclic_failed.csv"


if __name__ == "__main__":
    import sys
    from tests._runner import run
    sys.exit(run(globals()))
