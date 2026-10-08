"""
Unit tests for Salesforce auth mode selection in core.connector.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests._runner import ensure_project_root_on_path
ensure_project_root_on_path()

from core.connector import AuthenticationError, SFConnector, connector_from_env


def test_client_credentials_payload_uses_client_id_and_secret():
    conn = SFConnector(
        login_url="https://target.my.salesforce.com",
        client_id="target-client",
        client_secret="target-secret",
        grant_type="client_credentials",
    )

    payload = conn._build_token_payload()

    assert payload == {
        "grant_type": "client_credentials",
        "client_id": "target-client",
        "client_secret": "target-secret",
    }


def test_jwt_payload_uses_assertion_only():
    conn = SFConnector(
        login_url="https://source.my.salesforce.com",
        client_id="source-client",
        grant_type="auth:jwt:grant",
        username="integration.user@example.com",
        jwt_private_key="dummy-key",
    )
    conn._build_jwt_assertion = lambda: "signed-jwt"

    payload = conn._build_token_payload()

    assert payload == {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": "signed-jwt",
    }


def test_jwt_private_key_file_is_loaded_relative_to_project_root():
    conn = SFConnector(
        login_url="https://source.my.salesforce.com",
        client_id="source-client",
        grant_type="auth:jwt:grant",
        username="integration.user@example.com",
        jwt_key_file="tests/test_connector_auth.py",
    )
    assert 'Unit tests for Salesforce auth mode selection' in conn._load_jwt_private_key()


def test_jwt_requires_username():
    conn = SFConnector(
        login_url="https://source.my.salesforce.com",
        client_id="source-client",
        grant_type="auth:jwt:grant",
        jwt_private_key="dummy-key",
    )

    raised = False
    try:
        conn._build_jwt_assertion()
    except AuthenticationError:
        raised = True
    assert raised


def test_connector_from_env_supports_source_jwt_and_target_client_credentials():
    old_env = os.environ.copy()
    try:
        os.environ["SOURCE_SF_LOGIN_URL"] = "https://source.my.salesforce.com"
        os.environ["SOURCE_SF_CLIENT_ID"] = "source-client"
        os.environ["SOURCE_SF_GRANT_TYPE"] = "auth:jwt:grant"
        os.environ["SOURCE_SF_USERNAME"] = "integration.user@example.com"
        os.environ["SOURCE_SF_JWT_PRIVATE_KEY"] = "private\\nkey"

        os.environ["TARGET_SF_LOGIN_URL"] = "https://target.my.salesforce.com"
        os.environ["TARGET_SF_CLIENT_ID"] = "target-client"
        os.environ["TARGET_SF_CLIENT_SECRET"] = "target-secret"
        os.environ["TARGET_SF_GRANT_TYPE"] = "client_credentials"

        source = connector_from_env("SOURCE")
        target = connector_from_env("TARGET")

        assert source.grant_type == "auth:jwt:grant"
        assert source.username == "integration.user@example.com"
        assert source.jwt_private_key == "private\\nkey"
        assert target.grant_type == "client_credentials"
        assert target.client_secret == "target-secret"
    finally:
        os.environ.clear()
        os.environ.update(old_env)


def test_session_id_auth_rewrites_lightning_url_to_api_host():
    conn = SFConnector(
        login_url="https://aspect-edu--devfull.sandbox.lightning.force.com/",
        client_id="",
        session_id="session-token",
    )

    conn.connect()

    assert conn.instance_url == "https://aspect-edu--devfull.sandbox.my.salesforce.com"
    assert conn.access_token == "session-token"


def test_session_id_auth_keeps_my_salesforce_host():
    conn = SFConnector(
        login_url="https://example.my.salesforce.com/",
        client_id="",
        session_id="session-token",
    )

    conn.connect()

    assert conn.instance_url == "https://example.my.salesforce.com"


def test_session_id_auth_uses_login_url_as_instance_and_skips_oauth():
    conn = SFConnector(
        login_url="https://example.my.salesforce.com/",
        client_id="",
        session_id="session-token",
    )

    connected = conn.connect()

    assert connected is conn
    assert conn.access_token == "session-token"
    assert conn.instance_url == "https://example.my.salesforce.com"


def test_connector_from_env_supports_session_id_without_client_id():
    old_env = os.environ.copy()
    try:
        os.environ["SOURCE_SF_LOGIN_URL"] = "https://example.my.salesforce.com"
        os.environ.pop("SOURCE_SF_CLIENT_ID", None)
        os.environ["SOURCE_SF_SESSION_ID"] = "session-token"

        source = connector_from_env("SOURCE")

        assert source.client_id == ""
        assert source.session_id == "session-token"
    finally:
        os.environ.clear()
        os.environ.update(old_env)


if __name__ == "__main__":
    import sys
    from tests._runner import run
    sys.exit(run(globals()))
