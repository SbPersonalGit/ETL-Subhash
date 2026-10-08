"""
User-facing workspace folders for ETL Updated.

Layout (sibling of ETL Engine):
  Input Files / {Object API} / file
  Pre Validate Result / {Object API} / files
  Migration Result / {Object API} / files
  Post Validate Result / {Object API} / files
  ETL Engine / runtime code (this package lives here)
  Temp Files / {Object API} / extra files created during a run
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

ENGINE_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = ENGINE_ROOT.parent

INPUT_FILES_DIR = WORKSPACE_ROOT / "Input Files"
PRE_VALIDATE_DIR = WORKSPACE_ROOT / "Pre Validate Result"
MIGRATION_RESULT_DIR = WORKSPACE_ROOT / "Migration Result"
POST_VALIDATE_DIR = WORKSPACE_ROOT / "Post Validate Result"
TEMP_FILES_DIR = WORKSPACE_ROOT / "Temp Files"
ENGINE_DATA_DIR = TEMP_FILES_DIR / "_engine"

ETL_PORT = int(os.environ.get("ETL_PORT") or "5000")

_CUSTOM_OBJECT_RE = re.compile(
    r"^([A-Za-z][A-Za-z0-9_]*__(?:c|kav|mdt|e|x|b))(?=$|[._])",
    re.IGNORECASE,
)
_STANDARD_OBJECT_RE = re.compile(
    r"^(Account|Contact|User|Lead|Opportunity|Case|Task|Event|Campaign|"
    r"Product2|Pricebook2|PricebookEntry|Order|OrderItem|Asset|Contract|"
    r"ContentDocument|ContentVersion|ContentDocumentLink|Attachment|"
    r"Note|EmailMessage|KnowledgeArticle|Knowledge__kav)(?=$|[._])",
    re.IGNORECASE,
)
_SKIP_FILE_RE = re.compile(
    r"(^README\.md$|\.gitkeep$|progress\.json$|_publisher_index\.json$)",
    re.IGNORECASE,
)
_SKIP_NAME_PARTS = {"_engine", "__pycache__", ".git"}

_publisher_stop = threading.Event()
_publisher_thread: threading.Thread | None = None


def ensure_workspace_folders() -> None:
    """Create the six user folders and point engine logs/exports/state at Temp Files."""
    for path in (
        INPUT_FILES_DIR,
        PRE_VALIDATE_DIR,
        MIGRATION_RESULT_DIR,
        POST_VALIDATE_DIR,
        TEMP_FILES_DIR,
        ENGINE_DATA_DIR / "logs",
        ENGINE_DATA_DIR / "exports",
        ENGINE_DATA_DIR / "state",
    ):
        path.mkdir(parents=True, exist_ok=True)
    for name in ("logs", "exports", "state"):
        _ensure_junction(ENGINE_ROOT / name, ENGINE_DATA_DIR / name)


def validation_result_dir(phase: str, activity_key: str, object_names: list[str] | None = None) -> Path:
    """User-facing folder for one validation run.

    A single object lands in Pre/Post Validate Result / {Object API} / {run}.
    A run that covers several objects stays directly under the result folder.
    """
    root = PRE_VALIDATE_DIR if phase == "pre" else POST_VALIDATE_DIR
    names: list[str] = []
    for raw in object_names or []:
        text = str(raw or "").strip()
        if "::" in text:
            text = text.split("::", 1)[1].strip()
        if text and text not in names:
            names.append(text)
    parent = object_folder(root, names[0]) if len(names) == 1 else root
    path = parent / activity_key
    path.mkdir(parents=True, exist_ok=True)
    return path


def object_folder(root: Path, object_api: str | None) -> Path:
    name = (object_api or "").strip() or "_unassigned"
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def infer_object_api(filename: str, parent_name: str = "") -> str | None:
    """Best-effort object API name from a result filename or parent folder."""
    for candidate in (Path(parent_name).name, Path(filename).name):
        if not candidate:
            continue
        match = _CUSTOM_OBJECT_RE.match(candidate)
        if match:
            name = match.group(1)
            return name if name.endswith("__c") or "__" in name else name
        match = _STANDARD_OBJECT_RE.match(candidate)
        if match:
            return match.group(1)
    return None


def classify_result_kind(path: Path) -> str | None:
    """Return pre, migration, or post based on path parts and filename."""
    text = "/".join(path.parts).replace("\\", "/").lower()
    name = path.name.lower()
    if any(part in text for part in ("load_success", "load_failed", "load_blocked", "retry_failed")):
        return "migration"
    if "prevalidation" in text or name.endswith("_rejected.csv") or "_rejected." in name:
        return "pre"
    if name.endswith("_clean.csv") or "_clean." in name:
        return "pre"
    if any(
        token in text
        for token in (
            "file_validation",
            "postvalidation",
            "post_validation",
            "verify_records",
            "retrieved_target",
            "mismatch",
        )
    ):
        return "post"
    return None


def publish_file(source: Path, object_api: str | None = None, kind: str | None = None) -> list[Path]:
    """Copy one engine-generated file into the matching user folders."""
    if not source.is_file() or _SKIP_FILE_RE.search(source.name):
        return []
    object_name = object_api or infer_object_api(source.name, source.parent.name)
    if not object_name:
        return []
    kind = kind or classify_result_kind(source)
    run_label = _run_label_for(source)
    published: list[Path] = []
    destinations = [object_folder(TEMP_FILES_DIR, object_name) / run_label / source.name]
    if kind == "pre":
        destinations.append(object_folder(PRE_VALIDATE_DIR, object_name) / run_label / source.name)
    elif kind == "migration":
        destinations.append(object_folder(MIGRATION_RESULT_DIR, object_name) / run_label / source.name)
    elif kind == "post":
        destinations.append(object_folder(POST_VALIDATE_DIR, object_name) / run_label / source.name)
    for dest in destinations:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.stat().st_mtime >= source.stat().st_mtime and dest.stat().st_size == source.stat().st_size:
            continue
        shutil.copy2(source, dest)
        published.append(dest)
    return published


def scan_and_publish() -> int:
    """Copy new engine artifacts into object-scoped user folders."""
    copied = 0
    roots = [
        ENGINE_DATA_DIR / "logs",
        ENGINE_DATA_DIR / "exports",
        ENGINE_ROOT / "logs",
        ENGINE_ROOT / "exports",
    ]
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        try:
            resolved_root = root.resolve()
        except OSError:
            continue
        if resolved_root in seen:
            continue
        seen.add(resolved_root)
        for candidate in root.rglob("*"):
            if not candidate.is_file():
                continue
            if any(part in _SKIP_NAME_PARTS for part in candidate.parts):
                continue
            if candidate.suffix.lower() not in {".csv", ".json", ".txt", ".xlsx"}:
                continue
            if publish_file(candidate):
                copied += 1
    return copied


def start_result_publisher() -> None:
    """Publish result files in the background while the UI is running."""
    global _publisher_thread
    ensure_workspace_folders()
    if _publisher_thread and _publisher_thread.is_alive():
        return

    def _loop() -> None:
        while not _publisher_stop.wait(8):
            try:
                scan_and_publish()
            except Exception:
                logger.exception("Result folder publisher failed")

    _publisher_thread = threading.Thread(target=_loop, name="result-folder-publisher", daemon=True)
    _publisher_thread.start()


def upsert_env_values(env_path: Path, updates: dict[str, str]) -> None:
    """Create or update keys in a .env file without dropping unrelated values."""
    existing = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    seen: set[str] = set()
    rewritten: list[str] = []
    for line in existing:
        stripped = line.strip()
        key = ""
        if stripped and not stripped.startswith("#") and "=" in line:
            key = stripped.split("=", 1)[0].strip()
        if key in updates:
            rewritten.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            rewritten.append(line)
    for key, value in updates.items():
        if key not in seen:
            rewritten.append(f"{key}={value}")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")


def env_path() -> Path:
    return ENGINE_ROOT / ".env"


def read_target_setup() -> dict[str, str]:
    values = _parse_env_file(env_path())
    session = values.get("TARGET_SF_SESSION_ID") or ""
    return {
        "TARGET_SF_LOGIN_URL": values.get("TARGET_SF_LOGIN_URL") or "",
        "TARGET_SF_SESSION_ID": session,
        "TARGET_SF_SESSION_ID_MASKED": _mask_secret(session),
    }


def save_target_setup(login_url: str, session_id: str) -> dict[str, str]:
    url = (login_url or "").strip()
    session = (session_id or "").strip()
    if url and not url.lower().startswith("https://"):
        raise ValueError("TARGET_SF_LOGIN_URL must start with https://")
    if not env_path().exists():
        example = ENGINE_ROOT / ".env.example"
        if example.exists():
            shutil.copy2(example, env_path())
    updates = {}
    if url:
        updates["TARGET_SF_LOGIN_URL"] = url
    if session:
        updates["TARGET_SF_SESSION_ID"] = session
    if not updates:
        raise ValueError("Enter TARGET_SF_LOGIN_URL and/or TARGET_SF_SESSION_ID.")
    upsert_env_values(env_path(), updates)
    from dotenv import load_dotenv

    load_dotenv(env_path(), override=True)
    return read_target_setup()


def listening_pids(port: int = ETL_PORT) -> list[int]:
    try:
        output = subprocess.check_output(["netstat", "-ano"], text=True, errors="replace")
    except (OSError, subprocess.CalledProcessError):
        return []
    pids: set[int] = set()
    marker = f":{port} "
    for line in output.splitlines():
        if "LISTENING" not in line.upper() or marker not in line:
            continue
        parts = line.split()
        if not parts:
            continue
        try:
            pids.add(int(parts[-1]))
        except ValueError:
            continue
    return sorted(pid for pid in pids if pid > 0)


def server_is_running(port: int = ETL_PORT) -> bool:
    return bool(listening_pids(port))


def start_python_server(port: int = ETL_PORT) -> dict:
    """Start ui/app.py in the engine venv when nothing is listening on the port."""
    if server_is_running(port):
        return {"running": True, "port": port, "message": f"Python server is already running on port {port}."}
    python = _engine_python()
    app_path = ENGINE_ROOT / "ui" / "app.py"
    log_path = ENGINE_DATA_DIR / "logs" / "flask_server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ENGINE_ROOT)
    env["ETL_PORT"] = str(port)
    stdout = open(log_path, "a", encoding="utf-8")
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    subprocess.Popen(
        [str(python), str(app_path)],
        cwd=str(ENGINE_ROOT),
        env=env,
        stdout=stdout,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
        close_fds=os.name != "nt",
    )
    return {"running": True, "port": port, "message": f"Python server start requested on port {port}."}


def stop_python_server(port: int = ETL_PORT) -> dict:
    pids = listening_pids(port)
    killed: list[int] = []
    for pid in pids:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        killed.append(pid)
    return {"running": False, "killed": killed, "port": port}


def schedule_python_server_restart(port: int = ETL_PORT) -> dict:
    """Detach a helper that stops then starts the Flask process after this request returns."""
    helper = ENGINE_ROOT / "scripts" / "restart_flask.py"
    python = _engine_python()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ENGINE_ROOT)
    env["ETL_PORT"] = str(port)
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    subprocess.Popen(
        [str(python), str(helper), "--delay", "2"],
        cwd=str(ENGINE_ROOT),
        env=env,
        creationflags=creationflags,
        close_fds=os.name != "nt",
    )
    return {"restarting": True, "port": port, "message": f"Python server will restart on port {port}."}


def _engine_python() -> Path:
    local = ENGINE_ROOT / ".venv" / "Scripts" / "python.exe"
    if local.exists():
        return local
    return Path(os.environ.get("PYTHON") or "python")


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]}"


def _run_label_for(path: Path) -> str:
    for part in reversed(path.parts):
        lower = part.lower()
        if lower.startswith(("prevalidation_", "pipeline_prevalidation_", "file_validation_", "run_sf_", "postvalidation_")):
            return part
    return "latest"


def _ensure_junction(link: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    if link.exists():
        try:
            if link.resolve() == target.resolve():
                return
        except OSError:
            return
        return
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning("Could not create junction %s -> %s: %s", link, target, result.stderr.strip())
            link.mkdir(parents=True, exist_ok=True)
    else:
        link.symlink_to(target, target_is_directory=True)


def publisher_state_path() -> Path:
    return ENGINE_DATA_DIR / "logs" / "_publisher_index.json"


def write_publisher_marker(copied: int) -> None:
    path = publisher_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"copied": copied, "updated": time.time()}), encoding="utf-8")
