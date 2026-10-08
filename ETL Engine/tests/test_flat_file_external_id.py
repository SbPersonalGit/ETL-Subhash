from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests._runner import ensure_project_root_on_path
ensure_project_root_on_path()

from core.connector import SFConnector
from core.orchestrator import Orchestrator
from ui.app import _build_pipeline_entry


def test_flat_file_is_inferred_from_flat_nodes_for_stale_canvas_state():
    entry = _build_pipeline_entry({
        "pipeline_id": "First_Test",
        "pipeline_name": "First Test",
        "seed_object": "Account",
        "selections": [{
            "object": "Account",
            "selected_fields": ["External_Id__c", "Name"],
            "external_id": "External_Id__c",
        }],
        "nodes": [
            {"id": "a", "object": "Account", "direction": "seed"},
            {"id": "b", "object": "Case", "direction": "flat_file"},
        ],
    }, 1)
    assert entry["input"] == {"type": "flat_file", "template_id": "First_Test"}


def test_present_external_id_still_receives_source_id_rename_rule():
    connector = SFConnector("https://target.example", "", session_id="token")
    orchestrator = Orchestrator(connector)
    profile = type("Profile", (), {})()
    plan = orchestrator._build_object_plan(
        "Account",
        [{
            "name": "Account",
            "external_id": "External_Id__c",
            "fields": ["External_Id__c", "Name"],
            "soql": "SELECT External_Id__c, Name FROM Account",
        }],
        {"Account": profile},
        [],
        {},
        {},
    )
    assert plan.transform_rules == [
        {"type": "field_rename", "source": "Id", "target": "External_Id__c"}
    ]


if __name__ == "__main__":
    import sys
    from tests._runner import run
    sys.exit(run(globals()))
