from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests._runner import ensure_project_root_on_path
ensure_project_root_on_path()

from ui.app import app


class _TargetConnector:
    instance_url = "https://target.example"

    def describe_global(self):
        return [
            {
                "name": "Account",
                "label": "Account",
                "queryable": True,
                "createable": True,
                "deprecatedAndHidden": False,
            },
            {
                "name": "ReadOnlyObject__c",
                "label": "Read Only",
                "queryable": True,
                "createable": False,
                "deprecatedAndHidden": False,
            },
        ]


def test_flat_file_object_catalog_is_forced_to_target():
    calls = []

    def fake_get_connector(org, profile):
        calls.append((org, profile))
        return _TargetConnector()

    with patch("ui.app.get_connector", side_effect=fake_get_connector):
        response = app.test_client().get(
            "/api/flat-files/objects?profile=uat&org=source"
        )

    assert response.status_code == 200
    payload = response.get_json()
    assert calls == [("target", "uat")]
    assert payload["org"] == "target"
    assert payload["objects"] == [{"label": "Account", "name": "Account"}]


if __name__ == "__main__":
    from tests._runner import run
    sys.exit(run(globals()))
