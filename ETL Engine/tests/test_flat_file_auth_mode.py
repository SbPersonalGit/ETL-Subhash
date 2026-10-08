from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests._runner import ensure_project_root_on_path
ensure_project_root_on_path()

from cli import build_mode_connectors, is_flat_file_mode
from ui.app import app


def test_flat_file_mode_does_not_construct_source_connector():
    old_env = os.environ.copy()
    try:
        for key in list(os.environ):
            if key.startswith("SOURCE_SF_"):
                os.environ.pop(key)
        os.environ["TARGET_SF_LOGIN_URL"] = "https://target.example"
        os.environ["TARGET_SF_SESSION_ID"] = "target-session"

        config = {"migration": {"input_mode": "flat_file"}}
        source, target = build_mode_connectors(config)

        assert is_flat_file_mode(config)
        assert source is None
        assert target.session_id == "target-session"
    finally:
        os.environ.clear()
        os.environ.update(old_env)


def test_flat_file_auth_check_skips_source():
    response = app.test_client().get("/api/auth/check?mode=flat_file&org=source")
    assert response.status_code == 200
    assert response.get_json()["status"] == "skipped"


def test_auth_check_reports_session_auth_mode():
    class _Conn:
        instance_url = "https://source.example"
        session_id = "abc123"
        grant_type = "client_credentials"

    with patch("ui.app.get_connector", return_value=_Conn()):
        response = app.test_client().get("/api/auth/check?org=source&profile=default")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["auth_mode"] == "session_id"


if __name__ == "__main__":
    import sys
    from tests._runner import run
    sys.exit(run(globals()))
