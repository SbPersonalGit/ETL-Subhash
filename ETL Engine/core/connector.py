"""
SF Connector — handles authentication and raw API access for a Salesforce org.
Supports OAuth 2.0 client_credentials grant type (Connected App).
Provides helpers for Describe API, SOQL (REST), and Bulk API 2.0 HTTP calls.

Token caching: 
  OAuth access tokens are cached to state/token_cache.json keyed by (login_url, client_id).
  Cached tokens are reused across CLI runs until they expire (default 2h SF lifetime).
  A 5-minute safety buffer means tokens are refreshed at 115 minutes, not 120.
"""

import json
import html
import logging
import os
from urllib.parse import urlparse
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv
import jwt

# Load project .env before reading SF_ETL_* (must not depend on process cwd).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env", override=True)

logger = logging.getLogger(__name__)

TOKEN_CACHE_FILE = Path(__file__).parent.parent / "state" / "token_cache.json"
TOKEN_TTL_SECONDS = 7200        # Salesforce default: 2 hours
TOKEN_REFRESH_BUFFER = 300      # Refresh 5 minutes before expiry


def _rewrite_lightning_host(url: str) -> str:
    """Use the API host. Lightning URLs reject a valid session with INVALID_SESSION_ID."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    suffix = ".lightning.force.com"
    if not host.lower().endswith(suffix):
        return url
    api_host = host[: -len(suffix)] + ".my.salesforce.com"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{api_host}{port}"


def _looks_like_dead_local_proxy(proxy_url: str | None) -> bool:
    if not proxy_url:
        return False
    parsed = urlparse(str(proxy_url).strip())
    host = (parsed.hostname or "").strip().lower()
    port = parsed.port
    return host in {"127.0.0.1", "localhost", "::1"} and port == 9


def _env_truthy(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in ("1", "true", "yes", "on")


class AuthenticationError(Exception):
    pass


class SFConnector:
    """
    Wraps a Salesforce OAuth session (client_credentials flow).
    Exposes describe(), query(), and Bulk API 2.0 helpers.
    """

    def __init__(
        self,
        login_url: str,
        client_id: str,
        client_secret: str | None = None,
        grant_type: str = "client_credentials",
        username: str | None = None,
        jwt_key_file: str | None = None,
        jwt_private_key: str | None = None,
        session_id: str | None = None,
        api_version: str = "v60.0",
    ):
        # dotenv commonly preserves accidental leading spaces after '=' in .env files.
        # Strip aggressively so OAuth URLs and cache keys remain stable.
        self.login_url = (login_url or "").strip().rstrip("/")
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self.grant_type = (grant_type or "client_credentials").strip()
        self.username = (username or "").strip()
        self.jwt_key_file = (jwt_key_file or "").strip()
        self.jwt_private_key = (jwt_private_key or "").strip()
        self.session_id = (session_id or "").strip()
        self.api_version = (api_version or "v60.0").strip()
        self.access_token: str | None = None
        self.instance_url: str | None = None
        self._object_describe_cache: dict[str, dict] = {}
        self._global_describe_cache: list[dict] | None = None
        # When True, urllib3/requests honor HTTP(S)_PROXY from the environment.
        # Set SF_ETL_IGNORE_SYSTEM_PROXY=1 if a broken local proxy (e.g. 127.0.0.1:9) blocks Salesforce.
        self._session = requests.Session()
        self._session.trust_env = not _env_truthy("SF_ETL_IGNORE_SYSTEM_PROXY")
        if not self._session.trust_env:
            logger.info(
                "SF_ETL_IGNORE_SYSTEM_PROXY is set — HTTP(S)_PROXY env vars are ignored for Salesforce requests."
            )

    def _clear_cached_token(self):
        """Remove this connector's persisted token so the next auth is forced fresh."""
        if not TOKEN_CACHE_FILE.exists():
            return
        try:
            cache = json.loads(TOKEN_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return

        if self._cache_key not in cache:
            return

        cache.pop(self._cache_key, None)
        TOKEN_CACHE_FILE.write_text(
            json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # ------------------------------------------------------------------ #
    #  Authentication                                                      #
    # ------------------------------------------------------------------ #

    @property
    def _cache_key(self) -> str:
        return f"{self.login_url}|{self.client_id}"

    def connect(self) -> "SFConnector":
        """
        Authenticate using a supplied Salesforce session ID or OAuth 2.0.
        Reuses a cached token if still valid, otherwise fetches a fresh one.
        """
        if self.session_id:
            self.access_token = self.session_id
            self.instance_url = self._normalize_instance_url(self.login_url)
            if not self.instance_url:
                raise AuthenticationError(
                    "Session ID auth requires SF_LOGIN_URL to be the Salesforce instance URL."
                )
            return self

        cached = self._load_cached_token()
        if cached:
            access_token = (cached.get("access_token") or "").strip()
            instance_url = self._normalize_instance_url(cached.get("instance_url"))
            if access_token and instance_url:
                self.access_token = access_token
                self.instance_url = instance_url
                return self
            # Corrupt/partial cache entry — force refresh
            self._clear_cached_token()

        self._fetch_new_token()
        return self

    def _fetch_new_token(self):
        """Request a new access token from Salesforce and cache it."""
        token_url = f"{self.login_url}/services/oauth2/token"
        payload = self._build_token_payload()
        resp = self._session.post(token_url, data=payload)

        if not resp.ok:
            raise AuthenticationError(
                f"OAuth token request failed [{resp.status_code}]: {resp.text}"
            )

        data = resp.json()
        self.access_token = (data.get("access_token") or "").strip() or None
        self.instance_url = self._normalize_instance_url(data.get("instance_url")) or self._normalize_instance_url(
            self.login_url
        )

        if not self.access_token:
            raise AuthenticationError(f"No access_token in response: {data}")
        if not self.instance_url:
            raise AuthenticationError(
                f"No instance_url in OAuth response and login_url is not usable as a base URL: {data}"
            )

        self._save_token_to_cache()

    def _build_token_payload(self) -> dict:
        normalized_grant_type = self._normalized_grant_type()
        if normalized_grant_type == "urn:ietf:params:oauth:grant-type:jwt-bearer":
            return {
                "grant_type": normalized_grant_type,
                "assertion": self._build_jwt_assertion(),
            }
        return {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }

    def _normalized_grant_type(self) -> str:
        raw = self.grant_type.strip().lower()
        if raw in {"client_credentials", "client-credentials"}:
            return "client_credentials"
        if raw in {
            "auth:jwt:grant",
            "jwt",
            "jwt_bearer",
            "jwt-bearer",
            "urn:ietf:params:oauth:grant-type:jwt-bearer",
        }:
            return "urn:ietf:params:oauth:grant-type:jwt-bearer"
        raise AuthenticationError(
            "Unsupported Salesforce grant type "
            f"'{self.grant_type}'. Use 'client_credentials' or 'auth:jwt:grant'."
        )

    def _build_jwt_assertion(self) -> str:
        if not self.client_id:
            raise AuthenticationError("JWT auth requires SF_CLIENT_ID.")
        if not self.username:
            raise AuthenticationError("JWT auth requires SF_USERNAME.")

        private_key = self._load_jwt_private_key()
        now = datetime.now(timezone.utc)
        claims = {
            "iss": self.client_id,
            "sub": self.username,
            "aud": self.login_url,
            "exp": int((now + timedelta(minutes=3)).timestamp()),
            "iat": int(now.timestamp()),
        }
        return jwt.encode(claims, private_key, algorithm="RS256")

    def _load_jwt_private_key(self) -> str:
        if self.jwt_private_key:
            return self.jwt_private_key.replace("\\n", "\n")
        if self.jwt_key_file:
            key_path = Path(self.jwt_key_file)
            if not key_path.is_absolute():
                key_path = (_PROJECT_ROOT / key_path).resolve()
            try:
                return key_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise AuthenticationError(f"Unable to read JWT private key file: {key_path}") from exc
        raise AuthenticationError("JWT auth requires SF_JWT_KEY_FILE or SF_JWT_PRIVATE_KEY.")

    def _normalize_instance_url(self, raw: str | None) -> str | None:
        """
        Salesforce OAuth responses should include https://<mydomain>.my.salesforce.com.
        Some environments may omit instance_url; callers may also persist null/empty values
        into token_cache.json. Never return a bare 'None' string.
        """
        if raw is None:
            return None
        text = str(raw).strip()
        if not text or text.lower() == "none":
            return None
        if text.startswith("http://") or text.startswith("https://"):
            return _rewrite_lightning_host(text.rstrip("/"))
        # Extremely defensive: if someone stored a host without scheme, assume https
        return _rewrite_lightning_host(f"https://{text}".rstrip("/"))

    def _load_cached_token(self) -> dict | None:
        """
        Return cached token dict if it exists and is still valid.
        Returns None if missing, expired, or unreadable.
        """
        if not TOKEN_CACHE_FILE.exists():
            return None

        try:
            cache = json.loads(TOKEN_CACHE_FILE.read_text(encoding="utf-8"))
            entry = cache.get(self._cache_key)
            if not entry:
                return None

            expires_at = datetime.fromisoformat(entry["expires_at"])
            now = datetime.now(timezone.utc)
            if now >= expires_at - timedelta(seconds=TOKEN_REFRESH_BUFFER):
                return None  # Expired or within refresh buffer

            access_token = (entry.get("access_token") or "").strip()
            instance_url = self._normalize_instance_url(entry.get("instance_url"))
            if not access_token or not instance_url:
                return None

            return {"access_token": access_token, "instance_url": instance_url, "expires_at": entry["expires_at"]}

        except Exception:
            return None  # Corrupt cache — fall through to fresh auth

    def _save_token_to_cache(self):
        """Persist the current token to disk with an expiry timestamp."""
        TOKEN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

        cache = {}
        if TOKEN_CACHE_FILE.exists():
            try:
                cache = json.loads(TOKEN_CACHE_FILE.read_text(encoding="utf-8"))
            except Exception:
                cache = {}

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=TOKEN_TTL_SECONDS)
        cache[self._cache_key] = {
            "access_token": self.access_token,
            "instance_url": self.instance_url,
            "expires_at": expires_at.isoformat(),
        }

        TOKEN_CACHE_FILE.write_text(
            json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # ------------------------------------------------------------------ #
    #  Headers                                                            #
    # ------------------------------------------------------------------ #

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, url: str, retry_on_401: bool = True, **kwargs) -> requests.Response:
        """
        Execute an authenticated HTTP request.
        If Salesforce returns 401, refresh the token once and retry.
        """
        extra_headers = kwargs.pop("headers", {}) or {}
        headers = {**self._headers, **extra_headers}
        # A broken Salesforce/proxy connection must not hold a worker forever.
        # Callers with genuinely long requests can still override this value.
        kwargs.setdefault("timeout", (15, 120))
        try:
            resp = self._session.request(method, url, headers=headers, **kwargs)
        except requests.exceptions.ProxyError:
            dead_proxy = next(
                (
                    os.getenv(name)
                    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
                    if _looks_like_dead_local_proxy(os.getenv(name))
                ),
                None,
            )
            if not dead_proxy or not self._session.trust_env:
                raise

            logger.warning(
                "Salesforce request hit a dead local proxy (%s). Retrying without system proxy settings.",
                dead_proxy,
            )
            self._session.trust_env = False
            resp = self._session.request(method, url, headers=headers, **kwargs)

        if resp.status_code == 401 and retry_on_401:
            if self.session_id:
                raise AuthenticationError(
                    "Salesforce rejected the configured SF_SESSION_ID. "
                    "Generate a fresh session ID and update the environment."
                )
            self._clear_cached_token()
            self._fetch_new_token()
            headers = {**self._headers, **extra_headers}
            resp = self._session.request(method, url, headers=headers, **kwargs)
        if resp.status_code >= 400:
            body = ""
            try:
                body = resp.text.strip()
            except Exception:
                body = ""
            message = f"{resp.status_code} Client Error: {resp.reason} for url: {resp.url}"
            if body:
                message = f"{message} | Response: {body[:2000]}"
            raise requests.HTTPError(message, response=resp)
        return resp

    # ------------------------------------------------------------------ #
    #  REST API — Describe + SOQL                                        #
    # ------------------------------------------------------------------ #

    def describe_object(self, object_name: str) -> dict:
        """Return full describe metadata for a Salesforce object."""
        if object_name in self._object_describe_cache:
            return self._object_describe_cache[object_name]
        url = f"{self.instance_url}/services/data/{self.api_version}/sobjects/{object_name}/describe"
        resp = self._request("GET", url)
        data = resp.json()
        self._object_describe_cache[object_name] = data
        return data

    def describe_global(self) -> list[dict]:
        """Return list of all objects available in this org."""
        if self._global_describe_cache is not None:
            return self._global_describe_cache
        url = f"{self.instance_url}/services/data/{self.api_version}/sobjects"
        resp = self._request("GET", url)
        self._global_describe_cache = resp.json().get("sobjects", [])
        return self._global_describe_cache

    def describe_record_type_picklists(
        self, object_name: str, record_type_id: str
    ) -> dict[str, dict]:
        """Return Record-Type picklists, including dependent-value indexes."""
        if not self.instance_url:
            self.connect()
        url = (
            f"{self.instance_url}/services/data/{self.api_version}/ui-api/object-info/"
            f"{object_name}/picklist-values/{record_type_id}"
        )
        response = self._request("GET", url)
        if not response.ok:
            raise RuntimeError(
                f"Record Type picklist metadata failed [{response.status_code}]: "
                f"{response.text[:500]}"
            )
        payload = response.json().get("picklistFieldValues") or {}
        result = {}
        for field_name, field_config in payload.items():
            values = field_config.get("values") or []
            result[field_name] = {
                "values": {
                    html.unescape(str(item.get("value")))
                    for item in values
                    if item.get("value") is not None
                },
                "controller_values": {
                    html.unescape(str(value)): int(index)
                    for value, index in (field_config.get("controllerValues") or {}).items()
                },
                "valid_for": {
                    html.unescape(str(item.get("value"))): {
                        int(index) for index in (item.get("validFor") or [])
                    }
                    for item in values
                    if item.get("value") is not None
                },
            }
        return result

    def query(self, soql: str) -> list[dict]:
        """
        Execute a SOQL query via REST API.
        Auto-paginates using nextRecordsUrl.
        For large result sets use Bulk API 2.0 (extractor.py) instead.
        """
        url = f"{self.instance_url}/services/data/{self.api_version}/query"
        records = []
        params = {"q": soql}

        while True:
            resp = self._request("GET", url, params=params)
            data = resp.json()
            records.extend(data.get("records", []))

            if data.get("done", True):
                break
            # Follow nextRecordsUrl for pagination
            url = f"{self.instance_url}{data['nextRecordsUrl']}"
            params = {}

        return records

    # ------------------------------------------------------------------ #
    #  Bulk API 2.0 helpers                                               #
    # ------------------------------------------------------------------ #

    def bulk_url(self, path: str) -> str:
        return f"{self.instance_url}/services/data/{self.api_version}/{path}"

    def bulk_get(self, path: str) -> requests.Response:
        return self._request("GET", self.bulk_url(path))

    def bulk_post(self, path: str, payload: dict) -> dict:
        resp = self._request("POST", self.bulk_url(path), json=payload)
        return resp.json()

    def bulk_patch(self, path: str, payload: dict) -> dict:
        resp = self._request("PATCH", self.bulk_url(path), json=payload)
        return resp.json()

    def bulk_put_csv(self, path: str, csv_data: str) -> requests.Response:
        return self._request(
            "PUT",
            self.bulk_url(path),
            data=csv_data.encode("utf-8"),
            headers={"Content-Type": "text/csv"},
        )

    def bulk_get_csv(self, path: str, locator: str = None) -> requests.Response:
        params = {"locator": locator} if locator else {}
        return self._request(
            "GET",
            self.bulk_url(path),
            params=params,
            headers={"Accept": "text/csv"},
        )

    def decode_csv_response(self, resp: requests.Response) -> str:
        """
        Decode Bulk API CSV bytes as UTF-8.
        Salesforce CSV responses may omit a charset, which lets requests fall back
        to ISO-8859-1 and introduces mojibake for values like the pound sign.
        """
        return resp.content.decode("utf-8-sig")


# ------------------------------------------------------------------ #
#  Factory — build connector from .env variables                     #
# ------------------------------------------------------------------ #

def _normalize_profile_id(profile: str | None) -> str:
    text = (profile or "").strip()
    if not text:
        return "default"
    normalized = "".join(char if char.isalnum() else "_" for char in text).strip("_").lower()
    return normalized or "default"


def env_prefix_for(base_prefix: str, profile: str | None = None) -> str:
    normalized = _normalize_profile_id(profile)
    if normalized == "default":
        return base_prefix
    return f"{base_prefix}_{normalized.upper()}"


def list_env_profiles(base_prefix: str) -> list[dict]:
    profiles: list[dict] = []
    default_login_key = f"{base_prefix}_SF_LOGIN_URL"
    if os.environ.get(default_login_key):
        profiles.append({"id": "default", "label": "Default", "env_prefix": base_prefix})

    start_prefix = f"{base_prefix}_"
    suffix = "_SF_LOGIN_URL"
    for key in sorted(os.environ):
        if not key.startswith(start_prefix) or not key.endswith(suffix) or key == default_login_key:
            continue
        profile_token = key[len(start_prefix):-len(suffix)]
        if not profile_token:
            continue
        profile_id = _normalize_profile_id(profile_token)
        label = profile_token.replace("_", " ").title()
        profiles.append({
            "id": profile_id,
            "label": label,
            "env_prefix": f"{base_prefix}_{profile_token}",
        })
    return profiles


def connector_from_env(prefix: str, profile: str | None = None) -> SFConnector:
    """
    Build a connector from environment variables.
    prefix = 'SOURCE' or 'TARGET'
    Reads {prefix}_SF_LOGIN_URL, {prefix}_SF_CLIENT_ID, etc.
    """
    if prefix in {"SOURCE", "TARGET"} and profile is None:
        profile = os.environ.get(f"ACTIVE_{prefix}_PROFILE")
    resolved_prefix = env_prefix_for(prefix, profile) if prefix in {"SOURCE", "TARGET"} else prefix

    login_url = os.environ[f"{resolved_prefix}_SF_LOGIN_URL"].strip()
    client_id = os.environ.get(f"{resolved_prefix}_SF_CLIENT_ID", "").strip()
    client_secret = os.environ.get(f"{resolved_prefix}_SF_CLIENT_SECRET", "").strip()
    grant_type = os.environ.get(f"{resolved_prefix}_SF_GRANT_TYPE", "client_credentials").strip()
    username = os.environ.get(f"{resolved_prefix}_SF_USERNAME", "").strip()
    jwt_key_file = os.environ.get(f"{resolved_prefix}_SF_JWT_KEY_FILE", "").strip()
    jwt_private_key = os.environ.get(f"{resolved_prefix}_SF_JWT_PRIVATE_KEY", "").strip()
    session_id = os.environ.get(f"{resolved_prefix}_SF_SESSION_ID", "").strip()
    api_version = os.environ.get(f"{resolved_prefix}_SF_API_VERSION", "v60.0").strip()

    return SFConnector(
        login_url=login_url,
        client_id=client_id,
        client_secret=client_secret,
        grant_type=grant_type,
        username=username,
        jwt_key_file=jwt_key_file,
        jwt_private_key=jwt_private_key,
        session_id=session_id,
        api_version=api_version,
    )
