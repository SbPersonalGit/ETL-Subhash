"""
SF ETL UI — Flask backend.
Serves the visual migration plan builder and exposes SF metadata API endpoints.
"""

import os
import sys
import csv
import io
import yaml
import json
import re
import logging
import time
import threading
import requests
from types import SimpleNamespace
import copy
import subprocess
import signal
import hashlib
import uuid
import shutil
import zipfile
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import quote
from flask import Flask, jsonify, request, render_template, abort, send_file
from dotenv import load_dotenv, dotenv_values

# Allow imports from project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
# Load .env before importing core.* so SF_ETL_IGNORE_SYSTEM_PROXY and SF creds apply to connectors.
# override=True makes the checked-in/local .env authoritative after a real restart,
# instead of inheriting stale SOURCE_/TARGET_ values from the parent shell/process.
load_dotenv(_PROJECT_ROOT / ".env", override=True)

from core.workspace_layout import (
    ENGINE_ROOT,
    ETL_PORT,
    INPUT_FILES_DIR,
    MIGRATION_RESULT_DIR,
    POST_VALIDATE_DIR,
    PRE_VALIDATE_DIR,
    TEMP_FILES_DIR,
    WORKSPACE_ROOT,
    ensure_workspace_folders,
    read_target_setup,
    save_target_setup,
    schedule_python_server_restart,
    server_is_running,
    start_python_server,
    start_result_publisher,
    validation_result_dir,
)

ensure_workspace_folders()

from core.connector import connector_from_env, env_prefix_for, list_env_profiles
from core.object_hierarchy import generate_object_hierarchy_workbook
from core.identity_compiler import compile_stage_identities
from cli import get_latest_run_dir, build_pipeline_artifact_dir_map
from core.field_rules import (
    apply_default_owner_id_mapping,
    build_field_profile,
    build_target_lookup_fields,
    enrich_flat_file_mapping_lookup,
    MIGRATABLE_AUDIT_FIELDS,
    SYSTEM_FIELD_BLACKLIST,
    READONLY_FIELD_TYPES,
)
from core.run_audit import RunAuditStore
from core.storage_health import scan_storage_health
from core.storage_archive import (
    analyze_dependencies,
    cleanup_with_backup_gate,
    create_archive_package,
    enrich_health_with_templates,
    list_archive_packages,
    load_archive_config,
    restore_archive_package,
    save_archive_config,
)
from core.pipeline_schedule import (
    build_fire_payload,
    cancel_job,
    create_job,
    get_job,
    job_has_more_stages,
    job_ready_to_start,
    list_jobs,
    public_job,
    update_job,
)
from core.flat_file import (
    FlatFileInput,
    FlatFileTemplateStore,
    FlatFileValidationError,
    apply_unmapped_column_policy,
    auto_map_columns,
    detect_object_from_path,
    read_distinct_csv_values,
    read_csv_header,
    require_named_csv_headers,
    usable_csv_headers,
    build_folder_mapping_files,
    fill_missing_sibling_mappings,
    resolve_flat_file_path,
    validate_flat_file_reference,
    record_type_source_column,
    safe_name,
    validate_file_config,
    validate_flat_file_external_id,
)
from core.stager import Stager
from core.flat_file_schema_validator import (
    clean_csv_against_schema,
    validate_object_and_record_type_access,
    validate_strict_api_names,
    validate_migration_field_access,
    is_system_managed_field,
    is_migratable_audit_field,
    is_salesforce_derived_field,
    validate_csv_against_schema,
)
from core.file_validation_query import (
    discover_file_validation_csvs,
    discover_lookup_mapping_candidates,
    enforce_source_size_limits,
    read_file_validation_progress,
    validate_file_validation_output_paths,
    write_file_validation_progress,
)
from scripts.analyze_csv_column_population import (
    analyze_csv,
    write_consolidated_report,
    write_file_report,
)
from core.general_utility import (
    DEFAULT_FIELD_DESCRIPTION,
    apply_fallbacks,
    create_custom_field,
    discover_populated_references,
    grant_field_permissions,
    manifest_payload,
    populated_counts,
    validate_permission_principals,
    write_prepared_csv,
)
from core.bulk_fls import bulk_assign_fls, read_object_list

app = Flask(__name__)

# Cache connectors and profiles per session to avoid re-authenticating
_connectors: dict = {}
_profiles: dict = {}
TEMPLATE_DIR = Path(__file__).parent.parent / "config" / "templates"
PIPELINE_DESIGNER_DIR = Path(__file__).parent.parent / "config" / "pipeline_designer"
TRANSFORM_PROFILE_DIR = Path(__file__).parent.parent / "config" / "transform_profiles"
FLAT_FILE_TEMPLATE_DIR = Path(__file__).parent.parent / "config" / "flat_file_templates"
FLAT_FILE_UPLOAD_DIR = Path(__file__).parent.parent / "state" / "flat_file_uploads"
FLAT_FILE_DROP_DIR = INPUT_FILES_DIR
GENERAL_UTILITY_OUTPUT_DIR = Path(__file__).parent.parent / "state" / "general_utility"
GENERAL_UTILITY_JOB_DIR = GENERAL_UTILITY_OUTPUT_DIR / "jobs"
_general_utility_jobs: dict[str, dict] = {}
_general_utility_jobs_lock = threading.RLock()
FILE_VALIDATION_UPLOAD_DIR = Path(__file__).parent.parent / "state" / "file_validation_uploads"
COLUMN_ANALYSIS_DIR = Path(__file__).parent.parent / "logs" / "column_analysis"
RUN_STATE_FILE = Path(__file__).parent.parent / "state" / "run_state.json"
PROJECT_ROOT = _PROJECT_ROOT
PIPELINE_RUN_LOG = PROJECT_ROOT / "logs" / "ui_pipeline_run.log"
CONFIG_PATH = PROJECT_ROOT / "config" / "migration.yaml"
RUN_CONFIG_DIR = PROJECT_ROOT / "state" / "run_configs"
RUN_STATE_DIR = PROJECT_ROOT / "state" / "run_states"
_active_run_process: subprocess.Popen | None = None
_active_run_meta: dict = {}
_active_runs: dict = {}
_last_run_key: str | None = None
RUN_AUDIT = RunAuditStore()
_APP_STARTED_AT = datetime.now(timezone.utc)
logger = logging.getLogger(__name__)
_column_analysis_jobs: dict[str, dict] = {}
_column_analysis_lock = threading.Lock()


def _archive_flat_file_inputs(
    template: dict,
    batch_dir: Path,
    attempt_dir: Path,
) -> None:
    """Preserve exact Data Seeding inputs beside run artifacts for later verification."""
    uploaded_dir = attempt_dir / "uploaded"
    cleaned_dir = attempt_dir / "cleaned"
    uploaded_dir.mkdir(parents=True, exist_ok=True)
    cleaned_dir.mkdir(parents=True, exist_ok=True)

    for file_cfg in template.get("files", []) or []:
        if str(file_cfg.get("source_type") or "") == "drop_folder":
            reference_path = uploaded_dir / f"{safe_name(str(file_cfg.get('stored_name') or 'file'))}.reference.json"
            reference_path.write_text(json.dumps({
                "source_type": "drop_folder",
                "source_path": file_cfg.get("source_path"),
                "source_size": file_cfg.get("source_size", file_cfg.get("size")),
            }, indent=2), encoding="utf-8")
            # Cleaned output, when explicitly created, is still archived below.
            keys = (("cleaned_name", cleaned_dir),)
        else:
            keys = (("stored_name", uploaded_dir), ("cleaned_name", cleaned_dir))
        for config_key, destination_dir in keys:
            raw_filename = str(file_cfg.get(config_key) or "").strip()
            if not raw_filename:
                continue
            filename = safe_name(raw_filename)
            source_path = (batch_dir / filename).resolve()
            if source_path.parent != batch_dir.resolve() or not source_path.is_file():
                raise FileNotFoundError(f"Data Seeding artifact was not found: {source_path}")
            shutil.copy2(source_path, destination_dir / filename)


def _truthy(val) -> bool:
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


class ResumeArtifactError(ValueError):
    """Resume blocked: export folder or CSV stage missing (HTTP 409)."""

    status_code = 409


def _pipeline_ids_from_request_pipelines(payload: dict) -> list[str]:
    """Stable pipeline ids as produced by _build_pipeline_entry (same as migration.yaml)."""
    out: list[str] = []
    for order, p in enumerate(payload.get("pipelines") or [], start=1):
        out.append(_build_pipeline_entry(p, order)["id"])
    return out


def _artifact_folder_for_pipeline(pipeline_entries: list[dict], pipeline_id: str) -> str:
    """Match cli.build_pipeline_artifact_dir_map for this ordered slice."""
    ordered = sorted(pipeline_entries, key=lambda x: x.get("order", 99))
    pipes = [
        SimpleNamespace(pipeline_id=e["id"], name=e.get("name", e["id"]))
        for e in ordered
    ]
    dir_map = build_pipeline_artifact_dir_map(pipes)
    if pipeline_id not in dir_map:
        raise ValueError(
            f"pipeline_id '{pipeline_id}' is not in the execution slice "
            f"(available: {list(dir_map.keys())})."
        )
    return dir_map[pipeline_id]


def _validate_resume_artifacts(
    exports_run_key: str,
    pipeline_id: str,
    start_stage: str,
    pipeline_entries: list[dict],
) -> None:
    """
    Ensure on-disk export CSVs exist for transform (raw for Salesforce input,
    cleaned for flat-file input) or load (xfm).
    exports_run_key: logical export folder key (resume_run_key or sf_pipeline_run_id / ETL_RUN_KEY).
    """
    run_dir = _resolve_sf_run_dir(PROJECT_ROOT / "exports", exports_run_key)
    if not run_dir:
        raise ResumeArtifactError(
            f"Cannot resume {start_stage} for run '{exports_run_key}': export folder not found "
            f"(expected {PROJECT_ROOT / 'exports' / _run_dir_name_for_key(exports_run_key)}). "
            "Run extract with artifacts, or verify the run key matches exports/run_sf_*."
        )

    folder = _artifact_folder_for_pipeline(pipeline_entries, pipeline_id)
    pipeline_path = run_dir / folder
    if not pipeline_path.is_dir():
        alt = run_dir / pipeline_id
        if alt.is_dir():
            pipeline_path = alt
        else:
            raise ResumeArtifactError(
                f"Cannot resume {start_stage} for pipeline '{pipeline_id}': artifact folder "
                f"'{folder}' not found under {run_dir}."
            )

    pipeline_entry = next(
        (entry for entry in pipeline_entries if entry.get("id") == pipeline_id),
        {},
    )
    is_flat_file = (pipeline_entry.get("input") or {}).get("type") == "flat_file"
    transform_dir = pipeline_path / ("cleaned" if is_flat_file else "raw")
    xfm_dir = pipeline_path / "xfm"
    if start_stage == "transform":
        if not transform_dir.is_dir() or not any(transform_dir.glob("*.csv")):
            artifact_label = "cleaned flat-file" if is_flat_file else "raw"
            raise ResumeArtifactError(
                f"Cannot resume transform for run '{exports_run_key}' / pipeline '{pipeline_id}': "
                f"no {artifact_label} CSV files under {transform_dir}."
            )
    elif start_stage == "load":
        if not xfm_dir.is_dir() or not any(xfm_dir.glob("*.csv")):
            raise ResumeArtifactError(
                f"Cannot resume load for run '{exports_run_key}' / pipeline '{pipeline_id}': "
                f"no transformed (xfm) CSV files under {xfm_dir}."
            )


def _resolve_named_run_dir(base_dir: Path, run_name: str | None = None) -> Path | None:
    if run_name:
        candidate = (base_dir / run_name).resolve()
        base_resolved = base_dir.resolve()
        if candidate.parent != base_resolved or not candidate.name.startswith("run_") or not candidate.exists():
            return None
        return candidate
    latest = get_latest_run_dir(str(base_dir))
    return latest.resolve() if latest else None


def _run_dir_name_for_key(run_key: str | None) -> str | None:
    normalized = (run_key or "").strip()
    if not normalized:
        return None
    safe_key = re.sub(r"[^A-Za-z0-9_-]", "_", normalized)
    return f"run_sf_{safe_key}"


def _run_config_path(run_key: str | None) -> Path:
    run_dir_name = _run_dir_name_for_key(run_key) or "run_sf_default"
    return RUN_CONFIG_DIR / run_dir_name / "migration.yaml"


def _run_state_path(run_key: str | None) -> Path:
    run_dir_name = _run_dir_name_for_key(run_key) or "run_sf_default"
    return RUN_STATE_DIR / f"{run_dir_name}.json"


def _run_log_path(run_key: str | None) -> Path:
    run_dir_name = _run_dir_name_for_key(run_key) or "run_sf_default"
    return PROJECT_ROOT / "logs" / f"{run_dir_name}.log"


def _resolve_sf_run_dir(base_dir: Path, run_key: str | None) -> Path | None:
    run_dir_name = _run_dir_name_for_key(run_key)
    if not run_dir_name:
        return None
    candidate = (base_dir / run_dir_name).resolve()
    base_resolved = base_dir.resolve()
    if candidate.parent != base_resolved or not candidate.exists():
        return None
    return candidate


def _pipeline_log_dirs(pipeline_id: str, run_name: str | None = None) -> tuple[Path | None, list[Path]]:
    logs_root = PROJECT_ROOT / "logs"
    pipeline_dir_name = pipeline_id
    if RUN_STATE_FILE.exists():
        try:
            payload = json.loads(RUN_STATE_FILE.read_text(encoding="utf-8"))
            pipeline_record = ((payload.get("pipelines") or {}).get(pipeline_id) or {})
            pipeline_dir_name = pipeline_record.get("artifact_dir_name") or pipeline_id
        except Exception:
            pipeline_dir_name = pipeline_id

    def _pipeline_dirs(run_dir: Path) -> list[Path]:
        return [
            run_dir / pipeline_dir_name / "load_failed",
            run_dir / pipeline_dir_name / "load_blocked",
        ]

    def _retry_dirs(run_dir: Path) -> list[Path]:
        retry_root = run_dir / pipeline_dir_name / "retry_failed"
        if not retry_root.exists():
            return []
        rerun_dirs = [path for path in retry_root.iterdir() if path.is_dir() and path.name.startswith("rerun_")]
        rerun_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for rerun_dir in rerun_dirs:
            candidate_dirs = [
                rerun_dir / "load_failed",
                rerun_dir / "load_blocked",
            ]
            if any(path.exists() and any(path.glob("*.csv")) for path in candidate_dirs):
                return candidate_dirs
        return []

    def _root_dirs(run_dir: Path) -> list[Path]:
        return [
            run_dir / "load_failed",
            run_dir / "load_blocked",
        ]

    if run_name:
        run_dir = _resolve_named_run_dir(logs_root, run_name)
        if not run_dir:
            return None, []
        retry_dirs = _retry_dirs(run_dir)
        if retry_dirs:
            return run_dir, retry_dirs
        pipeline_dirs = _pipeline_dirs(run_dir)
        if any(path.exists() and any(path.glob("*.csv")) for path in pipeline_dirs):
            return run_dir, pipeline_dirs
        return run_dir, _root_dirs(run_dir)

    run_dirs = [
        path for path in logs_root.iterdir()
        if path.is_dir() and path.name.startswith("run_")
    ]
    run_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    for run_dir in run_dirs:
        retry_dirs = _retry_dirs(run_dir)
        if retry_dirs:
            return run_dir, retry_dirs

    for run_dir in run_dirs:
        pipeline_dirs = _pipeline_dirs(run_dir)
        if any(path.exists() and any(path.glob("*.csv")) for path in pipeline_dirs):
            return run_dir, pipeline_dirs

    for run_dir in run_dirs:
        root_dirs = _root_dirs(run_dir)
        if any(path.exists() and any(path.glob("*.csv")) for path in root_dirs):
            return run_dir, root_dirs

    latest = _resolve_named_run_dir(logs_root, None)
    if not latest:
        return None, []
    return latest, _pipeline_dirs(latest)


def _launch_background_command(
    command: list[str],
    start_message: str,
    extra_env: dict[str, str] | None = None,
    run_meta: dict | None = None,
) -> dict:
    global _active_run_process, _active_run_meta, _last_run_key

    active_run_key = str((run_meta or {}).get("run_key") or "").strip() or None
    log_path = Path((run_meta or {}).get("log_path") or _run_log_path(active_run_key))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = open(log_path, "a", encoding="utf-8")
    log_handle.write(f"\n[{_utc_now()}] {start_message}\n")
    log_handle.flush()

    creationflags = 0
    popen_kwargs = {}
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        popen_kwargs["start_new_session"] = True

    try:
        process_env = os.environ.copy()
        env_file = PROJECT_ROOT / ".env"
        if env_file.exists():
            process_env.update({
                key: str(value)
                for key, value in dotenv_values(env_file).items()
                if value is not None
            })
        if extra_env:
            process_env.update({key: str(value) for key, value in extra_env.items() if value is not None})
        # Background jobs redirect stdout to a file; without this, Python fully
        # buffers prints and the progress UI looks frozen for long runs.
        process_env.setdefault("PYTHONUNBUFFERED", "1")
        existing_pythonpath = str(process_env.get("PYTHONPATH") or "").strip()
        process_env["PYTHONPATH"] = (
            str(PROJECT_ROOT)
            if not existing_pythonpath
            else f"{PROJECT_ROOT}{os.pathsep}{existing_pythonpath}"
        )
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            env=process_env,
            **popen_kwargs,
        )
    except Exception:
        log_handle.close()
        raise

    meta = {
        "pid": process.pid,
        "started_at": _utc_now(),
        "returncode": None,
        "command": command,
    }
    if run_meta:
        meta.update(run_meta)
    meta["log_path"] = str(log_path)
    run_key = str(meta.get("run_key") or "").strip()
    if run_key:
        _active_runs[run_key] = {
            "process": process,
            "meta": meta,
        }
        _last_run_key = run_key
        _active_run_process = process
        _active_run_meta = meta
    # Do not immediately call _runner_status here. It reconciles completed
    # processes and writes to the audit database, which can contend with the
    # submission write and turn a successful launch into an HTTP 500 response.
    return {
        **meta,
        "running": True,
        "active_run_count": len(_active_runs),
    }


def get_connector(org: str, profile: str | None = None):
    """Return cached connector for 'source' or 'target'."""
    normalized_profile = (profile or "default").strip().lower() or "default"
    cache_key = f"{org}:{normalized_profile}"
    if cache_key not in _connectors:
        prefix = "SOURCE" if org == "source" else "TARGET"
        conn = connector_from_env(prefix, normalized_profile)
        conn.connect()
        _connectors[cache_key] = conn
    return _connectors[cache_key]


def _auth_mode_for_connector(conn) -> str:
    if getattr(conn, "session_id", ""):
        return "session_id"
    grant_type = str(getattr(conn, "grant_type", "") or "").strip().lower()
    if grant_type in {
        "auth:jwt:grant",
        "jwt",
        "jwt_bearer",
        "jwt-bearer",
        "urn:ietf:params:oauth:grant-type:jwt-bearer",
    }:
        return "jwt"
    return "client_credentials"


def clear_metadata_cache(org: str | None = None):
    targets = [org] if org in _connectors else list(_connectors.keys()) if org is None else []
    if org is None:
        _profiles.clear()
    else:
        stale_keys = [key for key in _profiles if key.startswith(f"{org}:")]
        for key in stale_keys:
            _profiles.pop(key, None)

    for key in targets:
        conn = _connectors.get(key)
        if not conn:
            continue
        conn._object_describe_cache = {}
        conn._global_describe_cache = None


# ------------------------------------------------------------------ #
#  UI Routes                                                          #
# ------------------------------------------------------------------ #

@app.route("/")
def index():
    return render_template("setup.html")


@app.route("/workspace")
def workspace_home():
    return render_template("home.html")


def _setup_payload(extra: dict | None = None) -> dict:
    payload = read_target_setup()
    payload.update({
        "running": server_is_running(ETL_PORT),
        "port": ETL_PORT,
        "folders": {
            "input": str(INPUT_FILES_DIR),
            "pre": str(PRE_VALIDATE_DIR),
            "migration": str(MIGRATION_RESULT_DIR),
            "post": str(POST_VALIDATE_DIR),
            "temp": str(TEMP_FILES_DIR),
            "engine": str(ENGINE_ROOT),
            "workspace": str(WORKSPACE_ROOT),
        },
    })
    if extra:
        payload.update(extra)
    return payload


def _apply_setup_from_request() -> dict:
    data = request.json or {}
    url = str(data.get("TARGET_SF_LOGIN_URL") or "").strip()
    session = str(data.get("TARGET_SF_SESSION_ID") or "").strip()
    if not url and not session:
        return read_target_setup()
    return save_target_setup(url, session)


@app.route("/api/setup", methods=["GET"])
def get_setup():
    return jsonify(_setup_payload())


@app.route("/api/setup/start", methods=["POST"])
def start_setup_server():
    try:
        _apply_setup_from_request()
        clear_metadata_cache()
        result = start_python_server(ETL_PORT)
        return jsonify(_setup_payload(result))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.exception("Failed to start Python server")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/setup/restart", methods=["POST"])
def restart_setup_server():
    try:
        _apply_setup_from_request()
        result = schedule_python_server_restart(ETL_PORT)
        return jsonify(_setup_payload(result))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.exception("Failed to restart Python server")
        return jsonify({"error": str(exc)}), 500


@app.route("/source-org")
def source_org_builder():
    return render_template("source_org_builder.html")


@app.route("/pipeline")
def pipeline_designer():
    static_js = Path(app.static_folder or (Path(__file__).parent / "static")) / "pipeline_designer.js"
    static_css = Path(app.static_folder or (Path(__file__).parent / "static")) / "style.css"
    return render_template(
        "pipeline_designer.html",
        pipeline_designer_js_v=int(static_js.stat().st_mtime) if static_js.exists() else 0,
        pipeline_designer_css_v=int(static_css.stat().st_mtime) if static_css.exists() else 0,
    )


@app.route("/flat-files")
def flat_file_designer():
    return render_template("flat_file_designer.html")


@app.route("/general-utility")
def general_utility_page():
    return render_template(
        "general_utility.html",
        default_field_description=DEFAULT_FIELD_DESCRIPTION,
    )


@app.route("/general-utility/runs/<job_id>")
def general_utility_run_progress(job_id: str):
    with _general_utility_jobs_lock:
        job = copy.deepcopy(_general_utility_jobs.get(job_id))
        job = job or _reconcile_general_utility_job(job_id)
    if not job:
        abort(404)
    return render_template("general_utility_run_progress.html", job_id=job_id, job=job)


@app.route("/pipeline/logs")
def pipeline_log_viewer():
    return render_template("pipeline_logs.html")


@app.route("/pipeline/runs/<run_key>")
def pipeline_run_progress(run_key: str):
    """Read-only, run-specific progress view for concurrent pipeline runs."""
    return render_template("pipeline_run_progress.html", run_key=run_key)


@app.route("/pipeline/runs")
def pipeline_runs_page():
    return render_template("pipeline_runs.html")


@app.route("/storage")
def storage_health_page():
    return render_template("storage_health.html")


@app.route("/validation")
def validation_page():
    return render_template(
        "validation.html",
        initial_run_key=(request.args.get("run_key") or "").strip(),
        initial_pipeline_id=(request.args.get("pipeline_id") or "").strip(),
        initial_phase=(request.args.get("phase") or "post").strip(),
        initial_template_id=(request.args.get("template_id") or "").strip(),
    )


@app.route("/validation/runs/<activity_key>")
def validation_run_progress(activity_key: str):
    return render_template("validation_run_progress.html", activity_key=activity_key)


@app.route("/object-hierarchy")
def object_hierarchy_viewer():
    return render_template("object_heirarchy_viewer.html")


# ------------------------------------------------------------------ #
#  API — Objects                                                      #
# ------------------------------------------------------------------ #

@app.route("/api/objects")
def list_objects():
    """Return all queryable/createable SF objects from the source org."""
    org = request.args.get("org", "source")
    profile = request.args.get("profile", "default")
    try:
        conn = get_connector(org, profile)
        all_objects = conn.describe_global()
        objects = [
            {"name": o["name"], "label": o["label"]}
            for o in all_objects
            if o.get("queryable") and o.get("createable") and not o.get("deprecatedAndHidden")
        ]
        objects.sort(key=lambda x: x["label"])
        return jsonify({"objects": objects})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/org-profiles")
def get_org_profiles():
    def _with_login_domain(org_prefix: str, profiles: list[dict]) -> list[dict]:
        enriched = []
        for item in profiles:
            prefix = str(item.get("env_prefix") or org_prefix)
            enriched.append({
                **item,
                "login_domain": _salesforce_domain_label(os.environ.get(f"{prefix}_SF_LOGIN_URL")),
            })
        return enriched

    return jsonify({
        "source": _with_login_domain("SOURCE", list_env_profiles("SOURCE")),
        "target": _with_login_domain("TARGET", list_env_profiles("TARGET")),
    })


# ------------------------------------------------------------------ #
#  API — Object Fields + Relationships                               #
# ------------------------------------------------------------------ #

@app.route("/api/object/<object_name>")
def get_object_fields(object_name: str):
    """
    Return field metadata for an object.
    Groups fields into: required, optional, lookup, children.
    """
    org = request.args.get("org", "source")
    profile = request.args.get("profile", "default")
    force_refresh = request.args.get("refresh") in ("1", "true", "yes")
    try:
        conn = get_connector(org, profile)
        cache_key = f"{org}:{profile}:{object_name}"

        if force_refresh:
            _profiles.pop(cache_key, None)
            conn._object_describe_cache.pop(object_name, None)

        if cache_key not in _profiles:
            _profiles[cache_key] = build_field_profile(conn, object_name)

        profile = _profiles[cache_key]
        describe = conn.describe_object(object_name)
        describe_fields = describe.get("fields", [])

        # Build children list — objects that reference this object
        children = _get_child_relationships(conn, object_name)

        fields = []
        seen_field_names: set[str] = set()
        profile_required = set(profile.required_fields)

        def include_ui_field(field_meta: dict) -> bool:
            name = field_meta.get("name")
            ftype = field_meta.get("type")
            if not name or name in SYSTEM_FIELD_BLACKLIST:
                return False
            if field_meta.get("deprecatedAndHidden", False):
                return False
            if ftype in READONLY_FIELD_TYPES:
                return False
            if field_meta.get("calculated", False):
                return False
            if field_meta.get("autoNumber", False):
                return False
            return bool(
                field_meta.get("queryable", True)
                or field_meta.get("createable", False)
                or field_meta.get("updateable", False)
            )

        for raw_meta in describe_fields:
            if not include_ui_field(raw_meta):
                continue
            fname = raw_meta["name"]
            seen_field_names.add(fname)
            refs = [r for r in (raw_meta.get("referenceTo") or []) if r]
            is_reference = raw_meta.get("type") == "reference"
            is_record_type = fname == "RecordTypeId" or (len(refs) == 1 and refs[0] == "RecordType")
            is_master_detail = is_reference and raw_meta.get("relationshipOrder") is not None
            is_user_reference = is_reference and "User" in refs
            supports_lookup_resolution = bool(
                is_reference
                and not is_record_type
                and (is_master_detail or is_user_reference or is_reference)
            )
            fields.append({
                "name": fname,
                "label": raw_meta.get("label", fname),
                "type": raw_meta.get("type"),
                "required": fname in profile_required,
                "is_lookup": is_reference,
                "is_master_detail": is_master_detail,
                "is_user_reference": is_user_reference,
                "is_record_type": is_record_type,
                "supports_lookup_resolution": supports_lookup_resolution,
                "is_external_id": raw_meta.get("externalId", False),
                "references_to": refs,
                "is_polymorphic": len(refs) > 1,
                "picklist_values": [
                    pv["value"]
                    for pv in raw_meta.get("picklistValues", [])
                    if pv.get("active")
                ][:20],
                "writable": bool(raw_meta.get("createable", False) or raw_meta.get("updateable", False)),
                "is_audit_field": fname in MIGRATABLE_AUDIT_FIELDS,
            })

        for fname in profile.include_fields:
            if fname in seen_field_names:
                continue
            meta = profile.field_meta[fname]
            refs = list(meta.references_to or [])
            is_record_type = meta.name == "RecordTypeId" or (len(refs) == 1 and refs[0] == "RecordType")
            is_user_reference = "User" in refs
            fields.append({
                "name": meta.name,
                "label": meta.label,
                "type": meta.field_type,
                "required": meta.is_required,
                "is_lookup": meta.is_lookup,
                "is_master_detail": False,
                "is_user_reference": is_user_reference,
                "is_record_type": is_record_type,
                "supports_lookup_resolution": bool(meta.is_lookup and not is_record_type),
                "is_external_id": meta.is_external_id,
                "references_to": refs,
                "is_polymorphic": meta.is_polymorphic,
                "picklist_values": meta.picklist_values[:20],
                "writable": bool(meta.createable or meta.updateable),
                "is_audit_field": meta.name in MIGRATABLE_AUDIT_FIELDS,
            })

        # Sort: required first, then lookups, then rest — all alphabetical within group
        fields.sort(key=lambda f: (0 if f["required"] else 1 if f["is_lookup"] else 2, f["label"]))

        return jsonify({
            "object_name": object_name,
            "fields": fields,
            "children": children,
            "required_fields": profile.required_fields,
            "external_id_fields": profile.external_id_fields,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/object/<object_name>/record-types")
def get_object_record_types(object_name: str):
    """Return active Record Types for an object (Id + DeveloperName)."""
    org = request.args.get("org", "source")
    profile = request.args.get("profile", "default")
    try:
        conn = get_connector(org, profile)
        escaped_object = str(object_name or "").replace("\\", "\\\\").replace("'", "\\'")
        rows = conn.query(
            "SELECT Id, DeveloperName, Name FROM RecordType "
            f"WHERE SobjectType = '{escaped_object}' AND IsActive = true "
            "ORDER BY DeveloperName"
        )
        record_types = [
            {
                "id": row.get("Id"),
                "developer_name": row.get("DeveloperName"),
                "name": row.get("Name") or row.get("DeveloperName"),
            }
            for row in rows
            if row.get("Id") and row.get("DeveloperName")
        ]
        return jsonify({"object_name": object_name, "record_types": record_types})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/metadata/refresh", methods=["POST"])
def refresh_metadata():
    data = request.json or {}
    org = (data.get("org") or "").strip() or None
    if org not in (None, "source", "target"):
        return jsonify({"error": f"Unsupported org '{org}'"}), 400
    clear_metadata_cache(org)
    return jsonify({
        "ok": True,
        "org": org or "all",
        "message": f"Metadata cache cleared for {org or 'all orgs'}",
    })


def _get_child_relationships(conn, object_name: str) -> list[dict]:
    """Return objects that have a lookup/master-detail field pointing to object_name."""
    try:
        describe = conn.describe_object(object_name)
        children = []
        for rel in describe.get("childRelationships", []):
            child_obj = rel.get("childSObject")
            field = rel.get("field")
            rel_name = rel.get("relationshipName")
            if child_obj and field and not rel.get("deprecatedAndHidden"):
                children.append({
                    "object": child_obj,
                    "field": field,
                    "relationship_name": rel_name,
                })
        return children
    except Exception:
        return []


@app.route("/api/object-hierarchy/export", methods=["POST"])
def export_object_hierarchy():
    data = request.json or {}
    org = (data.get("org") or "source").strip().lower()
    profile = (data.get("profile") or "default").strip() or "default"
    output_rel_path = (data.get("output_path") or "ui/object_hierarchy.xlsx").strip() or "ui/object_hierarchy.xlsx"
    raw_objects = data.get("objects") or []

    if org not in {"source", "target"}:
        return jsonify({"error": f"Unsupported org '{org}'"}), 400
    if not isinstance(raw_objects, list):
        return jsonify({"error": "objects must be a JSON array of Salesforce object API names"}), 400

    object_names = []
    seen: set[str] = set()
    for item in raw_objects:
        name = str(item or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        object_names.append(name)

    if not object_names:
        return jsonify({"error": "At least one in-scope object is required"}), 400

    output_path = (PROJECT_ROOT / output_rel_path).resolve()
    if output_path.parent != PROJECT_ROOT.resolve() and PROJECT_ROOT.resolve() not in output_path.parents:
        return jsonify({"error": "output_path must stay within the project workspace"}), 400

    try:
        conn = get_connector(org, profile)
        result = generate_object_hierarchy_workbook(conn, object_names, output_path)
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _slugify_template_name(name: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip())
    return text.strip("._-") or "template"


def _template_root_node(template_data: dict) -> dict:
    root_id = template_data.get("rootId")
    nodes = template_data.get("nodes") or []
    for node in nodes:
        if node.get("id") == root_id:
            return node
    for node in nodes:
        if not node.get("parentId"):
            return node
    return {}


def _is_sandbox_target(data: dict) -> bool:
    """Return True if target domain looks like a Salesforce sandbox."""
    domain = str(data.get("target_domain") or "").lower()
    return ".sandbox." in domain or "--" in domain


def _is_production_target(data: dict) -> bool:
    domain = str(data.get("target_domain") or "").strip().lower()
    return bool(domain) and not _is_sandbox_target(data)


def _salesforce_domain_label(url: str | None) -> str:
    host = str(url or "").strip()
    if host.lower().startswith("https://"):
        host = host[8:]
    elif host.lower().startswith("http://"):
        host = host[7:]
    host = host.split("/", 1)[0].strip()
    return host


def _profile_login_domain(org_prefix: str, profile: str | None) -> str:
    """Best-effort domain from env login URL (no Salesforce round-trip)."""
    try:
        resolved = env_prefix_for(org_prefix, (profile or "default").strip() or "default")
        return _salesforce_domain_label(os.environ.get(f"{resolved}_SF_LOGIN_URL"))
    except Exception:
        return ""


def _resolve_template_org_domains(
    *,
    input_mode: str,
    source_profile: str | None = None,
    target_profile: str | None = None,
    source_domain: str | None = None,
    target_domain: str | None = None,
) -> dict[str, str]:
    stored_source = _salesforce_domain_label(source_domain)
    stored_target = _salesforce_domain_label(target_domain)
    resolved_target = stored_target or _profile_login_domain("TARGET", target_profile)
    if input_mode == "flat_file":
        return {"source_domain": "", "target_domain": resolved_target}
    resolved_source = stored_source or _profile_login_domain("SOURCE", source_profile)
    return {"source_domain": resolved_source, "target_domain": resolved_target}


def _template_summary(path: Path, payload: dict) -> dict:
    kind = str(payload.get("template_kind") or "").strip().lower()
    template_data = payload.get("template") if isinstance(payload.get("template"), dict) else {}
    source_profile = payload.get("source_profile") or "default"
    target_profile = payload.get("target_profile") or "default"
    domains = _resolve_template_org_domains(
        input_mode=_normalize_input_mode(payload.get("input_mode")),
        source_profile=source_profile,
        target_profile=target_profile,
        source_domain=payload.get("source_domain"),
        target_domain=payload.get("target_domain"),
    )
    if kind in {"object_external_id", "reference_identity"}:
        objects = template_data.get("objects") if isinstance(template_data.get("objects"), list) else []
        object_names: list[str] = []
        object_external_ids: dict[str, str] = {}
        object_source_keys: dict[str, str] = {}
        if objects:
            for item in objects:
                if not isinstance(item, dict):
                    continue
                name = str(
                    item.get("object")
                    or item.get("target_object")
                    or item.get("source_object")
                    or ""
                ).strip()
                match = item.get("match_mapping") if isinstance(item.get("match_mapping"), dict) else {}
                external_id = str(
                    (match or {}).get("target_field")
                    or item.get("target_external_id")
                    or ""
                ).strip()
                if name and name not in object_names:
                    object_names.append(name)
                if name and external_id and name not in object_external_ids:
                    object_external_ids[name] = external_id
                source_key = str((match or {}).get("source_field") or "Id").strip() or "Id"
                if name:
                    object_source_keys[name] = source_key
        else:
            object_name = str(
                template_data.get("object")
                or template_data.get("target_object")
                or template_data.get("source_object")
                or ""
            ).strip()
            match = template_data.get("match_mapping") if isinstance(template_data.get("match_mapping"), dict) else {}
            external_id = str(
                (match or {}).get("target_field")
                or template_data.get("target_external_id")
                or ""
            ).strip()
            if object_name:
                object_names = [object_name]
            if object_name and external_id:
                object_external_ids = {object_name: external_id}
        first_object = object_names[0] if object_names else ""
        first_ext = object_external_ids.get(first_object, "")
        return {
            "id": path.stem,
            "name": payload.get("template_name", path.stem),
            "pipeline_id": path.stem,
            "pipeline_name": payload.get("template_name", path.stem),
            "seed_object": first_object,
            "object_count": len(object_names),
            "node_count": len(object_names),
            "object_names": object_names,
            "object_external_ids": object_external_ids,
            "object_source_keys": object_source_keys,
            "input_mode": _normalize_input_mode(payload.get("input_mode")),
            "source_profile": source_profile,
            "target_profile": target_profile,
            "source_domain": domains["source_domain"],
            "target_domain": domains["target_domain"],
            "prevalidation_status": payload.get("prevalidation_status") or "DRAFT",
            "template_kind": kind,
            "source_object": first_object,
            "target_object": first_object,
            "target_external_id": first_ext,
        }
    if kind == "record_type_mapping":
        object_name = str(
            template_data.get("object")
            or template_data.get("target_object")
            or template_data.get("source_object")
            or ""
        ).strip()
        mappings = template_data.get("record_type_mappings") if isinstance(template_data.get("record_type_mappings"), dict) else {}
        return {
            "id": path.stem,
            "name": payload.get("template_name", path.stem),
            "pipeline_id": path.stem,
            "pipeline_name": payload.get("template_name", path.stem),
            "seed_object": object_name,
            "object_count": 1 if object_name else 0,
            "node_count": 1 if object_name else 0,
            "object_names": [object_name] if object_name else [],
            "object_external_ids": {},
            "record_type_mapping_count": len(mappings),
            "input_mode": _normalize_input_mode(payload.get("input_mode")),
            "source_profile": source_profile,
            "target_profile": target_profile,
            "source_domain": domains["source_domain"],
            "target_domain": domains["target_domain"],
            "prevalidation_status": payload.get("prevalidation_status") or "DRAFT",
            "template_kind": "record_type_mapping",
            "source_object": object_name,
            "target_object": object_name,
        }
    if kind in {"source_mapping", "source_cycle_group"}:
        mapping_objects = _source_template_object_mappings(payload)
        first = mapping_objects[0] if mapping_objects else {}
        source_object = str(first.get("source_object") or "").strip()
        target_object = str(first.get("target_object") or "").strip()
        object_names = []
        object_external_ids: dict[str, str] = {}
        for item in mapping_objects:
            name = str(item.get("target_object") or item.get("source_object") or "").strip()
            if name and name not in object_names:
                object_names.append(name)
            match = item.get("match_mapping") if isinstance(item.get("match_mapping"), dict) else {}
            external_id = str(
                (match or {}).get("target_field")
                or item.get("target_external_id")
                or ""
            ).strip()
            if name and external_id and name not in object_external_ids:
                object_external_ids[name] = external_id
        template_name = payload.get("template_name", path.stem)
        source_profile = payload.get("source_profile") or "default"
        target_profile = payload.get("target_profile") or "default"
        domains = _resolve_template_org_domains(
            input_mode=_normalize_input_mode(payload.get("input_mode")),
            source_profile=source_profile,
            target_profile=target_profile,
            source_domain=payload.get("source_domain"),
            target_domain=payload.get("target_domain"),
        )
        return {
            "id": path.stem,
            "name": template_name,
            "pipeline_id": path.stem,
            "pipeline_name": template_name,
            "seed_object": source_object or target_object or (object_names[0] if object_names else ""),
            "object_count": len(object_names),
            "node_count": len(object_names),
            "object_names": object_names,
            "object_external_ids": object_external_ids,
            "input_mode": _normalize_input_mode(payload.get("input_mode")),
            "source_profile": source_profile,
            "target_profile": target_profile,
            "source_domain": domains["source_domain"],
            "target_domain": domains["target_domain"],
            "prevalidation_status": payload.get("prevalidation_status") or "DRAFT",
            "template_kind": kind or "source_mapping",
            "source_object": source_object,
            "target_object": target_object,
        }
    template_data = payload.get("template") or {}
    nodes = template_data.get("nodes") or []
    controls = template_data.get("controls") or {}
    unique_objects = sorted({node.get("object") for node in nodes if node.get("object")})
    root_node = _template_root_node(template_data)
    template_name = payload.get("template_name", path.stem)
    pipeline_id = (controls.get("pipelineId") or payload.get("template_id") or path.stem).strip() or path.stem
    pipeline_name = (controls.get("migrationName") or template_name).strip() or template_name
    input_mode = _normalize_input_mode(payload.get("input_mode"))
    source_profile = payload.get("source_profile") or template_data.get("org_profile") or "default"
    target_profile = payload.get("target_profile") or "default"
    domains = _resolve_template_org_domains(
        input_mode=input_mode,
        source_profile=source_profile,
        target_profile=target_profile,
        source_domain=payload.get("source_domain"),
        target_domain=payload.get("target_domain"),
    )
    return {
        "id": path.stem,
        "name": template_name,
        "pipeline_id": pipeline_id,
        "pipeline_name": pipeline_name,
        "seed_object": root_node.get("object"),
        "object_count": len(unique_objects),
        "node_count": len(nodes),
        "object_names": unique_objects,
        "input_mode": input_mode,
        "source_profile": source_profile,
        "target_profile": target_profile,
        "source_domain": domains["source_domain"],
        "target_domain": domains["target_domain"],
        "prevalidation_status": payload.get("prevalidation_status") or "DRAFT",
    }


def _source_template_object_mappings(payload: dict) -> list[dict]:
    """Normalize single-object and multi-object Source Org templates to a mapping list."""
    raw = payload.get("template") if isinstance(payload.get("template"), dict) else {}
    objects = raw.get("objects") if isinstance(raw, dict) else None
    if isinstance(objects, list) and objects:
        return [item for item in objects if isinstance(item, dict)]
    if isinstance(raw, dict) and (raw.get("source_object") or raw.get("target_object")):
        return [raw]
    return []


def _normalize_input_mode(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"salesforce_seed", "salesforce_source"}:
        return "salesforce_source"
    if normalized == "flat_file":
        return "flat_file"
    return "salesforce_source"


def _base_pipeline_activity_name(value: str | None) -> str:
    text = str(value or "").strip()
    for suffix in (" - Migration", " - Pre-validation", " - Post Validation"):
        if text.endswith(suffix):
            return text[: -len(suffix)].strip()
    return text


def _collect_used_pipeline_base_names(*, migrations_only: bool = False) -> set[str]:
    used: set[str] = set()
    for row in RUN_AUDIT.list_runs(500):
        activity_type = str(row.get("activity_type") or "MIGRATION").upper()
        if migrations_only and activity_type != "MIGRATION":
            continue
        base = _base_pipeline_activity_name(row.get("migration_name"))
        if base:
            used.add(base.casefold())
    return used


def _assert_required_unique_pipeline_name(
    value: str | None,
    *,
    for_migration: bool = False,
    require_unique: bool = True,
) -> str:
    """Require a non-empty pipeline base name.

    Unique-name checks apply to a fresh Pre-validation start. Skip and later
    migration retries may reuse a name that already has All Runs history.
    """
    base = _base_pipeline_activity_name(value) or str(value or "").strip()
    if not base:
        raise ValueError("Pipeline Name is required.")
    if require_unique:
        used = _collect_used_pipeline_base_names(migrations_only=True)
        if base.casefold() in used:
            raise ValueError(
                f"Pipeline Name '{base}' is already used by a previous migration. Choose a unique name."
            )
    return base


def _normalize_migration_object_identities(objects: list[dict], input_mode: str) -> list[dict]:
    """Compile SF->SF/File->SF identity and in-pipeline lookup contracts.

    Object identity is the single source of truth. Lookup-level external IDs are
    retained only as the compiled runtime value (or as an explicit override for
    a referenced object that is outside this pipeline).
    """
    return compile_stage_identities(objects, input_mode).objects


def _build_pipeline_entry(data: dict, order: int) -> dict:
    from knowledge_article.policy import compile_knowledge_policy

    nodes = data.get("nodes", [])
    source_object = str(data.get("source_object") or "").strip()
    target_object = str(data.get("target_object") or "").strip()
    if source_object and target_object and source_object != target_object:
        raise ValueError(
            f"Source template runtime currently supports same object API names only; got '{source_object}' -> '{target_object}'."
        )
    inferred_flat_file = any(
        str(node.get("direction") or "").strip().lower() == "flat_file"
        for node in nodes
        if isinstance(node, dict)
    )
    input_mode = _normalize_input_mode(
        data.get("input_mode") or ("flat_file" if inferred_flat_file else "salesforce_source")
    )
    seed_object = (data.get("seed_object") or "").strip()
    selections = data.get("selections", [])
    traversal = data.get("traversal", {})
    pipeline_id = _slugify_template_name(data.get("pipeline_id") or f"pipeline_{order}")
    pipeline_name = (data.get("pipeline_name") or data.get("migration_name") or pipeline_id).strip() or pipeline_id

    if input_mode == "flat_file":
        if not selections:
            raise ValueError("Flat-file pipeline stages require at least one mapped object.")
        identity_compilation = compile_stage_identities(
            _merge_object_selections(selections), input_mode
        )
        objects = compile_knowledge_policy(identity_compilation.objects, input_mode)
        if not seed_object:
            seed_object = objects[0]["name"] if objects else ""
        return {
            "id": pipeline_id,
            "name": pipeline_name,
            "order": order,
            "input": {
                "type": "flat_file",
                "template_id": str(data.get("flat_file_template_id") or pipeline_id).strip(),
            },
            "seed": {"object": seed_object, "query_filter": "", "max_records": data.get("max_records", 10000)},
            "nodes": _dedupe_nodes(nodes),
            "traverse": {},
            "objects": objects,
            "identity_graph": identity_compilation.graph,
        }

    if not seed_object or not selections:
        raise ValueError("seed_object and selections are required")

    nodes = _dedupe_nodes(nodes)
    identity_compilation = compile_stage_identities(
        _merge_object_selections(selections), input_mode
    )
    objects = compile_knowledge_policy(identity_compilation.objects, input_mode)

    return {
        "id": pipeline_id,
        "name": pipeline_name,
        "order": order,
        "seed": {
            "object": seed_object,
            "query_filter": _compose_query_parts(
                data.get("seed_where", ""),
                data.get("seed_order_by", ""),
                data.get("seed_limit", ""),
                data.get("seed_offset", ""),
            ) or data.get("seed_filter", ""),
            "max_records": data.get("max_records", 10000),
        },
        "nodes": nodes,
        "traverse": traversal,
        "objects": objects,
        "identity_graph": identity_compilation.graph,
    }


def _pipeline_prevalidation_gate_errors(pipeline_entries: list[dict]) -> list[str]:
    """Return stages that have not passed or explicitly bypassed pre-validation."""
    allowed = {"PASSED", "BYPASSED"}
    errors = []
    for entry in pipeline_entries:
        input_config = entry.get("input") or {}
        if str(input_config.get("type") or "").strip().lower() != "flat_file":
            continue
        template_id = safe_name(
            str(input_config.get("template_id") or entry.get("id") or "").strip()
        )
        template_path = (FLAT_FILE_TEMPLATE_DIR / f"{template_id}.json").resolve()
        if (
            template_path.parent != FLAT_FILE_TEMPLATE_DIR.resolve()
            or not template_path.is_file()
        ):
            errors.append(f"{entry.get('name') or template_id}: template not found")
            continue
        try:
            template = json.loads(template_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            errors.append(f"{entry.get('name') or template_id}: cannot read template ({exc})")
            continue
        status = str(template.get("prevalidation_status") or "DRAFT").strip().upper()
        if status not in allowed:
            errors.append(
                f"{entry.get('name') or template_id}: pre-validation status is {status}; "
                "run full pre-validation or explicitly bypass it"
            )
    return errors


def _ensure_unique_pipeline_entry_id(entry: dict, seen_ids: set[str]) -> dict:
    base_id = (entry.get("id") or "").strip() or "pipeline"
    if base_id not in seen_ids:
        entry["id"] = base_id
        return entry

    for suffix in range(2, 1000):
        candidate = f"{base_id}_{suffix}"
        if candidate not in seen_ids:
            entry["id"] = candidate
            return entry

    entry["id"] = f"{base_id}_{int(time.time())}"
    return entry


def _resolve_target_external_id_candidates(target_connector, object_name: str) -> list[str]:
    try:
        describe = target_connector.describe_object(object_name)
    except requests.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 404:
            raise ValueError(
                f"Target org does not contain object '{object_name}'. "
                "This template was built for a different org or package set."
            ) from exc
        raise ValueError(
            f"Could not describe target object '{object_name}' before pipeline start: {exc}"
        ) from exc
    fields = describe.get("fields", []) if isinstance(describe, dict) else []
    return sorted(
        field.get("name")
        for field in fields
        if field.get("externalId") and field.get("name")
    )


def _target_object_has_field(target_connector, object_name: str, field_name: str) -> bool:
    """True if `field_name` exists on target `object_name`, regardless of the
    External ID flag. Used to allow the match-by load path on objects where
    Salesforce forbids the External ID flag (e.g. Knowledge__kav)."""
    try:
        describe = target_connector.describe_object(object_name)
    except Exception:
        return False
    fields = describe.get("fields", []) if isinstance(describe, dict) else []
    return any(f.get("name") == field_name for f in fields)


def _describe_field_metadata(
    target_connector, object_name: str, field_name: str
) -> dict | None:
    describe = target_connector.describe_object(object_name)
    fields = describe.get("fields", []) if isinstance(describe, dict) else []
    return next((f for f in fields if f.get("name") == field_name), None)


def _external_id_validation_hint(
    target_connector, object_name: str, configured_external_id: str, candidates: list[str]
) -> str:
    """Append actionable context when the chosen upsert key is not valid on the target org."""
    parts: list[str] = []
    meta = _describe_field_metadata(target_connector, object_name, configured_external_id)
    if meta:
        if not meta.get("externalId"):
            parts.append(
                f"Field '{configured_external_id}' exists on target {object_name} but is not marked "
                "as an External ID (in Setup: Unique + External ID). Upsert requires an External ID field."
            )
    else:
        parts.append(
            f"Field '{configured_external_id}' is not defined on target {object_name}. "
            "Create or deploy it on the target org, or pick an external ID field that already exists there."
        )
    if not candidates:
        parts.append(
            f"Target {object_name} currently has no External ID fields in metadata — add at least one "
            "(for example a text field External_Id__c marked External ID + Unique) to use upsert."
        )
    return " " + " ".join(parts) if parts else ""


def _apply_flat_file_semantic_metadata(connector, object_name: str, fields: list[dict]) -> None:
    """Attach live target-org migration constraints not exposed by field Describe."""
    if object_name == "Contract":
        statuses = connector.query(
            "SELECT ApiName, StatusCode FROM ContractStatus ORDER BY SortOrder"
        )
        allowed = {
            str(row.get("ApiName") or "").strip()
            for row in statuses
            if row.get("StatusCode") == "Draft" and row.get("ApiName")
        }
        for field in fields:
            if field.get("name") == "Status":
                field["_migrationAllowedValues"] = sorted(allowed)
                field["_migrationAllowedReason"] = (
                    "historical Contract insertion policy (draft statuses only)"
                )
                break
    elif object_name == "Case":
        business_hours = connector.query(
            "SELECT Id FROM BusinessHours WHERE IsActive = true"
        )
        allowed = {
            str(row.get("Id") or "").strip()
            for row in business_hours
            if row.get("Id")
        }
        for field in fields:
            if field.get("name") == "BusinessHoursId":
                field["_migrationAllowedValues"] = sorted(allowed)
                field["_migrationAllowedReason"] = "active target Business Hours policy"
                break


def _normalize_pipeline_external_ids(pipeline_entries: list[dict], target_profile: str) -> list[dict]:
    target_connector = connector_from_env("TARGET", profile=target_profile)
    # connector_from_env does not authenticate by itself; describe_object requires connect()
    target_connector.connect()

    for entry in pipeline_entries:
        pipeline_name = entry.get("name") or entry.get("id") or "Unnamed Pipeline"
        for obj_cfg in entry.get("objects", []) or []:
            object_name = obj_cfg.get("name")
            if not object_name:
                continue
            configured_external_id = (obj_cfg.get("external_id") or "").strip()
            candidates = _resolve_target_external_id_candidates(target_connector, object_name)

            if configured_external_id:
                if configured_external_id in candidates:
                    continue

                # Field isn't flagged External ID. If it nonetheless exists on
                # target, the loader will fall back to the match-by-text-field
                # path (auto-detected from describe metadata at load time).
                # This is the only way to migrate objects that Salesforce
                # forbids External ID flags on — Knowledge__kav being the
                # canonical case.
                if _target_object_has_field(
                    target_connector, object_name, configured_external_id
                ):
                    logger.info(
                        "[%s] Configured key '%s' is not flagged External ID on target — "
                        "loader will use the match-by load path (insert + update split).",
                        object_name,
                        configured_external_id,
                    )
                    continue

                available = ", ".join(candidates) if candidates else "none"
                hint = _external_id_validation_hint(
                    target_connector,
                    object_name,
                    configured_external_id,
                    candidates,
                )
                raise ValueError(
                    f"Pipeline '{pipeline_name}' cannot run '{object_name}' with external ID "
                    f"'{configured_external_id}'. Available target external ID fields: {available}."
                    f"{hint}"
                )

            if len(candidates) == 1:
                obj_cfg["external_id"] = candidates[0]
                continue

            if len(candidates) > 1:
                raise ValueError(
                    f"Pipeline '{pipeline_name}' has multiple target external ID choices for '{object_name}': "
                    f"{', '.join(candidates)}. Pick one explicitly in the template."
                )

            raise ValueError(
                f"Pipeline '{pipeline_name}' cannot upsert '{object_name}' because the target org has no "
                "external ID field for it. Configure a target external ID field or change the load strategy."
            )

    return pipeline_entries


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _update_audit_status_resilient(run_key: str, **kwargs) -> bool:
    """Retry transient DuckDB writer contention without failing an HTTP launch response."""
    for attempt in range(5):
        try:
            RUN_AUDIT.update_status(run_key, **kwargs)
            return True
        except Exception:
            if attempt == 4:
                logger.exception("Could not update audit status for %s", run_key)
                return False
            time.sleep(0.1 * (attempt + 1))
    return False


def _find_external_cli_pid() -> int | None:
    """Find the PID of an externally-launched 'python cli.py run' process."""
    try:
        if os.name == "nt":
            # Use PowerShell — wmic outputs UTF-16 and is deprecated on Win11
            result = subprocess.run(
                [
                    "powershell", "-NoProfile", "-Command",
                    "Get-WmiObject Win32_Process | "
                    "Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*cli.py*' } | "
                    "Select-Object -ExpandProperty ProcessId",
                ],
                capture_output=True, text=True, check=False, timeout=10,
            )
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.isdigit():
                    return int(line)
        else:
            result = subprocess.run(
                ["pgrep", "-f", "cli.py.*run"],
                capture_output=True, text=True, check=False,
            )
            pid_str = result.stdout.strip().splitlines()
            if pid_str:
                return int(pid_str[0])
    except Exception:
        pass
    return None


def _state_has_running_pipeline() -> bool:
    """Return True if run_state.json contains any pipeline with status RUNNING."""
    try:
        state = json.loads(RUN_STATE_FILE.read_text(encoding="utf-8"))
        return any(
            p.get("status") == "RUNNING"
            for p in state.get("pipelines", {}).values()
        )
    except Exception:
        return False


def _mark_external_run_stopped():
    """Update run_state.json: set any RUNNING pipeline/object to STOPPED when the process is killed."""
    try:
        state = json.loads(RUN_STATE_FILE.read_text(encoding="utf-8"))
        changed = False
        for p in state.get("pipelines", {}).values():
            if p.get("status") == "RUNNING":
                p["status"] = "STOPPED"
                p["error"] = "Stopped by user via UI"
                p["stopped_at"] = _utc_now()
                changed = True
            for obj in (p.get("objects") or {}).values():
                if obj.get("status") == "RUNNING":
                    obj["status"] = "STOPPED"
                    obj["error"] = "Stopped by user via UI"
                    obj["stopped_at"] = _utc_now()
                    changed = True
        if changed:
            state["last_updated"] = _utc_now()
            RUN_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


def _mark_run_stopped(run_key: str) -> None:
    """Mark RUNNING pipeline/object entries stopped in one run-specific state file."""
    state_path = _run_state_path(run_key)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        changed = False
        for pipeline in (state.get("pipelines") or {}).values():
            if pipeline.get("status") == "RUNNING":
                pipeline["status"] = "STOPPED"
                pipeline["error"] = "Stopped by user via UI"
                pipeline["stopped_at"] = _utc_now()
                changed = True
            for object_record in (pipeline.get("objects") or {}).values():
                if object_record.get("status") == "RUNNING":
                    object_record["status"] = "STOPPED"
                    object_record["error"] = "Stopped by user via UI"
                    object_record["stopped_at"] = _utc_now()
                    changed = True
        if changed:
            state["last_updated"] = _utc_now()
            state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        logger.exception("Could not mark run '%s' stopped", run_key)


def _pipeline_artifacts_exist(pipeline_id: str, pipeline_record: dict | None) -> bool:
    """Return True when on-disk artifacts still exist for a pipeline."""
    if not pipeline_id or not pipeline_record:
        return False

    for obj in (pipeline_record.get("objects") or {}).values():
        failed_file = obj.get("failed_file")
        if failed_file and (PROJECT_ROOT / failed_file).exists():
            return True
        success_file = obj.get("success_file")
        if success_file and (PROJECT_ROOT / success_file).exists():
            return True

    for root_name in ("exports", "logs"):
        root = PROJECT_ROOT / root_name
        if not root.exists():
            continue
        try:
            for run_dir in root.glob("run_*"):
                pipeline_dir_name = (pipeline_record.get("artifact_dir_name") if isinstance(pipeline_record, dict) else None) or pipeline_id
                if (run_dir / pipeline_dir_name).exists():
                    return True
        except Exception:
            continue
    return False


def _is_shared_token_valid(token: str | None) -> bool:
    shared_token = (os.environ.get("SF_API_SHARED_TOKEN") or "").strip()
    if not shared_token:
        return True
    return (token or "").strip() == shared_token


def _artifact_type_for_path(path: Path, root_name: str) -> str:
    lowered_parts = {part.lower() for part in path.parts}
    filename = path.name.lower()
    if "load_failed" in lowered_parts or "load_blocked" in lowered_parts or "retry_failed" in lowered_parts:
        return "Data"
    if "raw" in lowered_parts or "xfm" in lowered_parts:
        return "Data"
    if "extract" in lowered_parts or "verify" in lowered_parts or "topology" in filename or "report" in filename:
        return "Report"
    if root_name == "logs":
        return "Log"
    return "Other"


def _collect_run_artifacts(run_key: str, base_url: str) -> list[dict]:
    artifacts: list[dict] = []
    token_suffix = ""
    shared_token = (os.environ.get("SF_API_SHARED_TOKEN") or "").strip()
    if shared_token:
        token_suffix = f"?token={quote(shared_token, safe='')}"

    for root_name in ("exports", "logs"):
        run_dir = _resolve_sf_run_dir(PROJECT_ROOT / root_name, run_key)
        if not run_dir:
            continue

        for path in sorted(run_dir.rglob("*")):
            if not path.is_file():
                continue
            rel_path = path.relative_to(run_dir).as_posix()
            rel_path_lower = rel_path.lower()
            if path.suffix.lower() not in {".csv", ".json", ".log", ".txt"}:
                continue
            if root_name == "exports" and "__pycache__" in rel_path_lower:
                continue

            artifact_url = (
                f"{base_url}/sf-api/v1/runs/{quote(run_key, safe='')}/artifacts/"
                f"{root_name}/{quote(rel_path, safe='/')}{token_suffix}"
            )
            checksum = hashlib.sha256(f"{root_name}:{rel_path}:{int(path.stat().st_mtime)}:{path.stat().st_size}".encode("utf-8")).hexdigest()
            artifacts.append({
                "name": f"{root_name}/{rel_path}",
                "type": _artifact_type_for_path(path, root_name),
                "url": artifact_url,
                "storage_provider": "Python",
                "checksum": checksum,
            })

    return artifacts


def _normalize_pipeline_artifact_status(payload: dict) -> dict:
    """Downgrade stale pipeline statuses whose artifact folders/files are gone."""
    normalized = copy.deepcopy(payload)
    pipelines = normalized.get("pipelines") or {}
    changed = False

    for pipeline_id, record in pipelines.items():
        if not isinstance(record, dict):
            continue
        status = (record.get("status") or "").upper()
        if status not in {"COMPLETED", "FAILED"}:
            continue
        if _pipeline_artifacts_exist(pipeline_id, record):
            continue

        record["status"] = "PENDING"
        record["started_at"] = None
        record["completed_at"] = None
        record.pop("failed_at", None)
        record.pop("error", None)
        record["objects"] = {}
        changed = True

    if changed:
        normalized["last_updated"] = _utc_now()
    return normalized


def _derive_pipeline_data_load_status(record: dict | None) -> str | None:
    """Infer pipeline-level load outcome from tracked object counts."""
    if not isinstance(record, dict):
        return None

    objects = record.get("objects") or {}
    if not isinstance(objects, dict) or not objects:
        return None

    tracked = 0
    any_failed_rows = False
    any_success_rows = False
    all_tracked_have_failed_rows = True

    for obj in objects.values():
        if not isinstance(obj, dict):
            continue
        success_count = int(obj.get("loaded_success") or 0)
        failed_count = int(obj.get("loaded_failed") or 0)
        if obj.get("load_in_progress"):
            continue
        if success_count <= 0 and failed_count <= 0:
            continue
        tracked += 1
        if failed_count > 0:
            any_failed_rows = True
        else:
            all_tracked_have_failed_rows = False
        if success_count > 0:
            any_success_rows = True

    if not tracked or not any_failed_rows:
        return None
    if all_tracked_have_failed_rows and not any_success_rows:
        return "DATA_LOAD_FAILED"
    return "PARTIAL_DATA_LOAD_FAILED"


def _derive_object_data_load_status(record: dict | None) -> str | None:
    """Infer an object's real load outcome from row counters."""
    if not isinstance(record, dict):
        return None
    if record.get("load_in_progress"):
        return None
    success_count = int(record.get("loaded_success") or 0)
    failed_count = int(record.get("loaded_failed") or 0)
    if failed_count > 0 and success_count <= 0:
        return "DATA_LOAD_FAILED"
    if failed_count > 0 and success_count > 0:
        return "PARTIAL_DATA_LOAD_FAILED"
    return None


def _apply_pipeline_outcome_overrides(payload: dict) -> dict:
    """Overlay row-level load outcomes onto coarse object and pipeline statuses."""
    normalized = copy.deepcopy(payload)
    pipelines = normalized.get("pipelines") or {}
    changed = False

    for record in pipelines.values():
        if not isinstance(record, dict):
            continue
        for object_record in (record.get("objects") or {}).values():
            if (object_record.get("status") or "").upper() in {"STOPPED", "QUEUED"}:
                continue
            derived_object_status = _derive_object_data_load_status(object_record)
            if not derived_object_status:
                continue
            if (object_record.get("status") or "").upper() == derived_object_status:
                continue
            object_record["execution_status"] = object_record.get("status")
            object_record["status"] = derived_object_status
            changed = True
        if (record.get("status") or "").upper() in {"STOPPED", "QUEUED"}:
            continue
        derived_status = _derive_pipeline_data_load_status(record)
        if not derived_status:
            continue
        if (record.get("status") or "").upper() == derived_status:
            continue
        record["status"] = derived_status
        changed = True

    if changed:
        normalized["last_updated"] = _utc_now()
    return normalized


def _runner_status(run_key: str | None = None) -> dict:
    global _active_run_process, _active_run_meta, _last_run_key

    for active_key, entry in list(_active_runs.items()):
        process = entry.get("process")
        meta = entry.get("meta") or {}
        returncode = process.poll() if process else None
        if returncode is None:
            continue
        meta["returncode"] = returncode
        _active_runs.pop(active_key, None)
        _active_run_meta = meta
        if meta.get("origin") in {"ui-validation", "ui-prevalidation"}:
            RUN_AUDIT.update_status(
                active_key,
                status="COMPLETED" if returncode == 0 else "FAILED",
                runner={**meta, "running": False},
                error_message=None if returncode == 0 else f"Validation process exited with code {returncode}",
            )
        if _last_run_key == active_key:
            _active_run_process = None

    explicit_run_key = str(run_key or "").strip()
    selected_run_key = explicit_run_key or str(_last_run_key or "").strip()
    selected_entry = _active_runs.get(selected_run_key) if selected_run_key else None
    if not selected_entry and not explicit_run_key and _active_runs:
        selected_run_key, selected_entry = list(_active_runs.items())[-1]

    if selected_entry:
        process = selected_entry.get("process")
        meta = selected_entry.get("meta") or {}
        _active_run_process = process
        _active_run_meta = meta
        _last_run_key = str(meta.get("run_key") or selected_run_key or "").strip() or _last_run_key
        return {
            **meta,
            "running": True,
            "pid": process.pid if process else meta.get("pid"),
            "started_at": meta.get("started_at"),
            "log_path": str(meta.get("log_path") or PIPELINE_RUN_LOG),
            "config_path": str(meta.get("config_path") or CONFIG_PATH),
            "state_path": str(meta.get("state_path") or RUN_STATE_FILE),
            "returncode": None,
            "command": meta.get("command"),
            "run_key": meta.get("run_key"),
            "origin": meta.get("origin"),
            "active_run_count": len(_active_runs),
        }

    if explicit_run_key:
        audited_runner = _audit_runner_status(explicit_run_key)
        if audited_runner:
            return audited_runner

    # Check if a run was launched externally (e.g. via `python cli.py run`)
    if _state_has_running_pipeline():
        ext_pid = _find_external_cli_pid()
        if ext_pid is not None:
            return {
                "running": True,
                "pid": ext_pid,
                "external": True,
                "started_at": None,
                "log_path": str(PIPELINE_RUN_LOG),
                "config_path": str(CONFIG_PATH),
                "state_path": str(RUN_STATE_FILE),
                "returncode": None,
                "command": "python cli.py run",
                "active_run_count": 1,
            }

    return {
        "running": False,
        "pid": _active_run_meta.get("pid"),
        "started_at": _active_run_meta.get("started_at"),
        "log_path": str(_active_run_meta.get("log_path") or PIPELINE_RUN_LOG),
        "config_path": str(_active_run_meta.get("config_path") or CONFIG_PATH),
        "state_path": str(_active_run_meta.get("state_path") or RUN_STATE_FILE),
        "returncode": _active_run_meta.get("returncode"),
        "command": _active_run_meta.get("command"),
        "run_key": _active_run_meta.get("run_key"),
        "origin": _active_run_meta.get("origin"),
        "active_run_count": len(_active_runs),
    }


def _require_sf_api_auth():
    shared_token = (os.environ.get("SF_API_SHARED_TOKEN") or "").strip()
    if not shared_token:
        return None

    auth_header = (request.headers.get("Authorization") or "").strip()
    custom_token = (request.headers.get("X-ETL-Shared-Token") or "").strip()
    query_token = (request.args.get("token") or "").strip()
    bearer_token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
    if bearer_token == shared_token or custom_token == shared_token or query_token == shared_token:
        return None
    return jsonify({"error": "Unauthorized"}), 401


def _build_pipeline_entries_from_request(data: dict) -> list[dict]:
    pipeline_requests = data.get("pipelines") or []
    if pipeline_requests:
        seen_ids: set[str] = set()
        pipeline_entries: list[dict] = []
        previous_pipeline_id = None
        for order, pipeline_data in enumerate(pipeline_requests, start=1):
            entry = _build_pipeline_entry(pipeline_data, order)
            if entry["id"] in seen_ids:
                raise ValueError(f"Duplicate pipeline id: {entry['id']}")
            seen_ids.add(entry["id"])
            if previous_pipeline_id:
                entry["depends_on"] = [previous_pipeline_id]
            previous_pipeline_id = entry["id"]
            pipeline_entries.append(entry)
        return pipeline_entries
    return [_build_pipeline_entry(data, 1)]


def _derive_run_status(payload: dict, runner: dict, stopped_by_user: bool = False) -> str:
    if stopped_by_user:
        return "STOPPED"
    if runner.get("running"):
        return "RUNNING"

    pipelines = (payload or {}).get("pipelines") or {}
    if not pipelines:
        return "QUEUED" if runner.get("run_key") else "IDLE"

    statuses = {(record.get("status") or "").upper() for record in pipelines.values() if isinstance(record, dict)}
    if "STOPPED" in statuses:
        return "STOPPED"
    if "RUNNING" in statuses:
        return "RUNNING"
    if statuses & {"FAILED", "DATA_LOAD_FAILED", "PARTIAL_DATA_LOAD_FAILED"}:
        errors = [
            (record.get("error") or "")
            for record in pipelines.values()
            if isinstance(record, dict) and (record.get("status") or "").upper() == "FAILED"
        ]
        if stopped_by_user or any("Stopped by user" in error for error in errors):
            return "STOPPED"
        return "FAILED"
    if statuses and statuses.issubset({"COMPLETED", "SKIPPED"}):
        return "COMPLETED"
    return "QUEUED"


def _current_status_payload(run_key: str | None = None) -> dict:
    state_path = _run_state_path(run_key) if run_key else RUN_STATE_FILE
    if not state_path.exists():
        return {
            "migration": None,
            "run_key": None,
            "started_at": None,
            "last_updated": None,
            "pipelines": {},
        }
    try:
        raw_state = state_path.read_text(encoding="utf-8")
        if not raw_state.strip():
            raise ValueError("state file is empty")
        payload = json.loads(raw_state)
        if not isinstance(payload, dict):
            raise ValueError("state file root must be a JSON object")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring unreadable run state %s: %s", state_path, exc)
        return {
            "migration": None,
            "run_key": str(run_key or "").strip() or None,
            "started_at": None,
            "last_updated": None,
            "pipelines": {},
            "state_warning": f"Unreadable run-state file: {state_path.name}",
        }
    payload = _normalize_pipeline_artifact_status(payload)
    payload = _apply_live_load_progress(payload, run_key)
    return _apply_pipeline_outcome_overrides(payload)


_PIPELINE_HEADER_RE = re.compile(r"(?:==>|▶)\s*Pipeline:\s*(?P<pipeline>\S+)")
_INGEST_JOB_PROGRESS_RE = re.compile(
    r"\[(?P<object>[A-Za-z0-9_]+)\] Waiting on ingest job (?P<job_id>[0-9A-Za-z]+)"
    r".*?processed=(?P<processed>\d+)\s*\|\s*failed=(?P<failed>\d+)",
    re.DOTALL,
)
_TRANSFORM_COUNT_RE = re.compile(
    r"Transforming (?P<object>\S+) \((?P<count>[\d,]+) records\)"
)
_LOAD_COUNT_RE = re.compile(
    r"Loading (?P<object>\S+) \(level \d+, (?P<count>[\d,]+) records\)"
)


def _flatten_run_log_text(text: str) -> str:
    return re.sub(r"[ \t]*\n[ \t]+", " ", text or "")


def _empty_progress_slot() -> dict:
    return {
        "transformed": 0,
        "loaded_processed": 0,
        "loaded_failed": 0,
        "loaded_success": 0,
    }


def _add_job_counts(slot: dict, processed: int, failed: int) -> None:
    slot["loaded_processed"] = int(slot.get("loaded_processed") or 0) + processed
    slot["loaded_failed"] = int(slot.get("loaded_failed") or 0) + failed
    slot["loaded_success"] = max(
        0,
        int(slot.get("loaded_processed") or 0) - int(slot.get("loaded_failed") or 0),
    )


def parse_live_load_progress_from_log(text: str) -> dict:
    """Parse Bulk ingest poll lines into per-pipeline and per-object counts."""
    flattened = _flatten_run_log_text(text)
    events: list[tuple[int, str, re.Match]] = []
    for match in _PIPELINE_HEADER_RE.finditer(flattened):
        events.append((match.start(), "pipeline", match))
    for match in _TRANSFORM_COUNT_RE.finditer(flattened):
        events.append((match.start(), "transform", match))
    for match in _LOAD_COUNT_RE.finditer(flattened):
        events.append((match.start(), "load", match))
    for match in _INGEST_JOB_PROGRESS_RE.finditer(flattened):
        events.append((match.start(), "ingest", match))
    events.sort(key=lambda item: item[0])

    by_pipeline: dict[str, dict[str, dict]] = {}
    objects: dict[str, dict] = {}
    jobs: dict[tuple[str | None, str, str], tuple[int, int]] = {}
    current_pipeline: str | None = None
    active_pipeline: str | None = None
    phase: str | None = None

    for _start, kind, match in events:
        if kind == "pipeline":
            current_pipeline = match.group("pipeline")
            by_pipeline.setdefault(current_pipeline, {})
            continue
        if kind == "transform":
            phase = phase or "transform"
            object_name = match.group("object")
            count = int(match.group("count").replace(",", ""))
            objects.setdefault(object_name, _empty_progress_slot())["transformed"] = count
            if current_pipeline:
                by_pipeline.setdefault(current_pipeline, {}).setdefault(
                    object_name, _empty_progress_slot()
                )["transformed"] = count
                if phase != "load":
                    active_pipeline = current_pipeline
            continue
        if kind == "load":
            phase = "load"
            object_name = match.group("object")
            count = int(match.group("count").replace(",", ""))
            objects.setdefault(object_name, _empty_progress_slot())["transformed"] = count
            if current_pipeline:
                by_pipeline.setdefault(current_pipeline, {}).setdefault(
                    object_name, _empty_progress_slot()
                )["transformed"] = count
                active_pipeline = current_pipeline
            continue
        object_name = match.group("object")
        job_id = match.group("job_id")
        processed = int(match.group("processed"))
        failed = int(match.group("failed"))
        phase = "load"
        key = (current_pipeline, object_name, job_id)
        previous = jobs.get(key, (0, 0))
        if processed >= previous[0]:
            jobs[key] = (processed, failed)
        if current_pipeline:
            active_pipeline = current_pipeline

    pipeline_jobs: dict[tuple[str | None, str], dict] = {}
    for (pipeline_id, object_name, _job_id), (processed, failed) in jobs.items():
        slot_key = (pipeline_id, object_name)
        slot = pipeline_jobs.setdefault(slot_key, _empty_progress_slot())
        _add_job_counts(slot, processed, failed)

    for (pipeline_id, object_name), slot in pipeline_jobs.items():
        object_slot = objects.setdefault(object_name, _empty_progress_slot())
        if not pipeline_id:
            _add_job_counts(object_slot, int(slot["loaded_processed"]), int(slot["loaded_failed"]))
            continue
        target = by_pipeline.setdefault(pipeline_id, {}).setdefault(
            object_name, _empty_progress_slot()
        )
        target["loaded_processed"] = slot["loaded_processed"]
        target["loaded_failed"] = slot["loaded_failed"]
        target["loaded_success"] = slot["loaded_success"]
        if not object_slot.get("loaded_processed"):
            object_slot["loaded_processed"] = slot["loaded_processed"]
            object_slot["loaded_failed"] = slot["loaded_failed"]
            object_slot["loaded_success"] = slot["loaded_success"]

    if not pipeline_jobs and jobs:
        for (pipeline_id, object_name, _job_id), (processed, failed) in jobs.items():
            _add_job_counts(objects.setdefault(object_name, _empty_progress_slot()), processed, failed)

    return {
        "objects": objects,
        "pipelines": by_pipeline,
        "active_pipeline": active_pipeline,
        "phase": phase,
    }


def _load_progress_from_run_log(run_key: str | None) -> dict:
    log_path = _run_log_path(run_key)
    if not log_path.exists():
        return {}
    try:
        size = log_path.stat().st_size
        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            if size > 2_000_000:
                handle.seek(max(0, size - 2_000_000))
                handle.readline()
            text = handle.read()
    except OSError:
        return {}
    return parse_live_load_progress_from_log(text)


def _object_has_final_load_counts(obj: dict) -> bool:
    status = str(obj.get("status") or "").upper()
    loaded = int(obj.get("loaded_success") or 0) + int(obj.get("loaded_failed") or 0)
    return status in {"COMPLETED", "FAILED", "STOPPED", "DATA_LOAD_FAILED", "PARTIAL_DATA_LOAD_FAILED"} and loaded > 0


def _mark_pipeline_queued(record: dict) -> None:
    if str(record.get("status") or "").upper() in {"COMPLETED", "FAILED", "STOPPED", "DATA_LOAD_FAILED", "PARTIAL_DATA_LOAD_FAILED"}:
        return
    record["status"] = "QUEUED"
    objects = record.get("objects") or {}
    if not isinstance(objects, dict):
        return
    for obj in objects.values():
        if not isinstance(obj, dict) or _object_has_final_load_counts(obj):
            continue
        if obj.get("load_in_progress"):
            continue
        obj["status"] = "QUEUED"
        obj["load_in_progress"] = False


def _apply_object_live_counts(record: dict, live_objs: dict[str, dict]) -> None:
    objects = record.setdefault("objects", {})
    if not isinstance(objects, dict):
        return
    for object_name, live_obj in live_objs.items():
        obj = objects.get(object_name)
        if not isinstance(obj, dict):
            obj = {
                "status": "RUNNING",
                "extracted": 0,
                "transformed": 0,
                "loaded_success": 0,
                "loaded_failed": 0,
            }
            objects[object_name] = obj
        if live_obj.get("transformed") and not obj.get("transformed"):
            obj["transformed"] = live_obj["transformed"]
        if _object_has_final_load_counts(obj):
            continue
        if live_obj.get("loaded_processed"):
            obj["loaded_success"] = int(live_obj.get("loaded_success") or 0)
            obj["loaded_failed"] = int(live_obj.get("loaded_failed") or 0)
            obj["load_in_progress"] = True
            if str(obj.get("status") or "").upper() in {"", "PENDING", "QUEUED"}:
                obj["status"] = "RUNNING"
            record["status"] = "RUNNING"


def apply_live_load_progress_overlay(payload: dict, live: dict | None) -> dict:
    """Overlay in-progress Bulk counts without changing run_state on disk."""
    if not live:
        return payload
    pipelines = payload.get("pipelines") or {}
    if not isinstance(pipelines, dict):
        return payload
    by_pipeline = live.get("pipelines") if isinstance(live.get("pipelines"), dict) else {}
    objects_live = live.get("objects") if isinstance(live.get("objects"), dict) else {}
    if "objects" not in live and "pipelines" not in live:
        objects_live = {key: value for key, value in live.items() if isinstance(value, dict)}
        by_pipeline = {}
    logged_active = live.get("active_pipeline")
    phase = live.get("phase")
    pipeline_ids = [pid for pid, record in pipelines.items() if isinstance(record, dict)]
    active = logged_active if phase == "load" and logged_active in pipeline_ids else None
    if not active:
        for pid in pipeline_ids:
            record = pipelines[pid]
            objects = record.get("objects") or {}
            if any(_object_has_final_load_counts(obj) for obj in objects.values() if isinstance(obj, dict)):
                continue
            active = pid
            break
    payload["active_pipeline"] = active
    payload["run_phase"] = phase

    active_index = pipeline_ids.index(active) if active in pipeline_ids else -1
    for index, pid in enumerate(pipeline_ids):
        record = pipelines[pid]
        if active and pid != active and index > active_index:
            _mark_pipeline_queued(record)
            continue
        live_objs = by_pipeline.get(pid) or {}
        if not live_objs and (pid == active or not by_pipeline):
            live_objs = objects_live
        if live_objs:
            _apply_object_live_counts(record, live_objs)
    return payload


def _apply_live_load_progress(payload: dict, run_key: str | None) -> dict:
    return apply_live_load_progress_overlay(payload, _load_progress_from_run_log(run_key))


def _audit_runner_status(run_key: str | None) -> dict | None:
    normalized_run_key = str(run_key or "").strip()
    if not normalized_run_key:
        return None
    audit_row = RUN_AUDIT.get_run(normalized_run_key)
    if not audit_row:
        return None
    runner_json = (audit_row.get("runner_json") or "").strip()
    if not runner_json:
        return None
    try:
        runner = json.loads(runner_json)
    except Exception:
        return None
    if not isinstance(runner, dict):
        return None
    runner["run_key"] = normalized_run_key
    pid = runner.get("pid")
    running = False
    if pid:
        try:
            if os.name == "nt":
                check = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
                    capture_output=True, text=True, check=False,
                )
                running = str(pid) in check.stdout and "No tasks are running" not in check.stdout
            else:
                os.kill(int(pid), 0)
                running = True
        except (OSError, ValueError):
            running = False
    runner["running"] = running
    if running:
        runner["external"] = True
    runner["active_run_count"] = len(_active_runs)
    if not runner.get("log_path"):
        runner["log_path"] = str(_run_log_path(normalized_run_key))
    if not runner.get("config_path"):
        runner["config_path"] = str(_run_config_path(normalized_run_key))
    if not runner.get("state_path"):
        runner["state_path"] = str(_run_state_path(normalized_run_key))
    return runner


def _reconcile_finished_validation(record: dict) -> dict:
    """Repair a validation audit left RUNNING after its launcher restarted."""
    if str(record.get("activity_type") or "").upper() not in {"VALIDATION", "PRE_VALIDATION"}:
        return record
    stale_worker_error = "Validation worker is no longer running and did not write a completion marker"
    stored_status = str(record.get("status") or "").upper()
    # A worker can survive a Flask debug reload or an audit-write failure. In
    # that case the nested progress heartbeat is more authoritative than a
    # stale FAILED/QUEUED audit row.
    out_dir = _validation_output_dir(record)
    progress = _latest_prevalidation_progress(out_dir) if out_dir else None
    if progress and str(progress.get("status") or "").upper() == "RUNNING":
        try:
            updated_at = datetime.fromisoformat(
                str(progress.get("updated_at") or "").replace("Z", "+00:00")
            )
            heartbeat_fresh = (datetime.now(timezone.utc) - updated_at).total_seconds() < 180
        except (TypeError, ValueError):
            heartbeat_fresh = False
        if heartbeat_fresh:
            repaired = {
                **record,
                "status": "RUNNING",
                "error_message": None,
                "completed_at": None,
            }
            _update_audit_status_resilient(
                str(record.get("run_key") or ""), status="RUNNING", error_message=""
            )
            return repaired
    if stored_status == "COMPLETED" and record.get("error_message") == stale_worker_error:
        RUN_AUDIT.update_status(str(record.get("run_key")), status="COMPLETED", error_message="")
        return RUN_AUDIT.get_run(str(record.get("run_key"))) or record
    if stored_status not in {"RUNNING", "QUEUED"} and not (
        stored_status == "FAILED" and record.get("error_message") == stale_worker_error
    ):
        return record
    runner = _audit_runner_status(str(record.get("run_key") or "")) or {}
    if runner.get("running"):
        return record
    log_path = Path(str(runner.get("log_path") or _run_log_path(str(record.get("run_key") or ""))))
    log_tail = ""
    try:
        log_tail = log_path.read_text(encoding="utf-8", errors="replace")[-20000:]
    except OSError:
        pass
    if "Verification complete" in log_tail or "Pre-validation PASSED" in log_tail:
        status, error = "COMPLETED", ""
    elif "Pre-validation ISSUES_FOUND" in log_tail:
        status, error = "FAILED", "Pre-validation completed with issues"
    else:
        # The process can exit just before redirected stdout flushes its final
        # line. Keep the activity running briefly instead of declaring a false
        # failure during that finalization window.
        try:
            if log_path.is_file() and datetime.now().timestamp() - log_path.stat().st_mtime < 60:
                return record
        except OSError:
            pass
        status, error = "FAILED", stale_worker_error
    RUN_AUDIT.update_status(
        str(record.get("run_key")), status=status,
        runner={**runner, "running": False}, error_message=error,
    )
    return RUN_AUDIT.get_run(str(record.get("run_key"))) or record


def _repair_pipeline_prevalidation_audit(record: dict) -> dict:
    """Recover validation metadata from its durable request after an interrupted audit write."""
    run_key = str(record.get("run_key") or "").strip()
    if not run_key.startswith("pipeline_prevalidation_"):
        return record
    if str(record.get("activity_type") or "").upper() == "PRE_VALIDATION" and record.get("request_payload_json"):
        return record
    request_path = PROJECT_ROOT / "state" / "validation_requests" / f"{run_key}.json"
    request_payload = _load_json_file(request_path)
    if not request_payload:
        return record
    stages = request_payload.get("stages") or []
    objects = [
        f"{stage.get('pipeline_name') or stage.get('pipeline_id')} :: {object_name}"
        for stage in stages
        for object_name in (stage.get("object_names") or [])
    ]
    legacy_dir = PROJECT_ROOT / "logs" / "verify" / run_key
    out_dir = legacy_dir if legacy_dir.exists() else validation_result_dir("pre", run_key, objects)
    request_payload = {
        **request_payload,
        "template_name": request_payload.get("pipeline_name") or request_payload.get("pipeline_id"),
        "objects": objects,
        "out_dir": str(out_dir),
    }
    repaired = {
        **record,
        "activity_type": "PRE_VALIDATION",
        "data_operation": "PIPELINE SEQUENCE",
        "migration_name": f"{request_payload.get('pipeline_name') or run_key} - Pre-validation",
        "request_payload_json": json.dumps(request_payload, ensure_ascii=False),
    }
    try:
        RUN_AUDIT.upsert_submission(run_key, repaired)
    except Exception:
        logger.exception("Could not persist recovered validation audit for %s", run_key)
    return repaired


def _status_payload_matches_run(payload: dict | None, run_key: str | None) -> bool:
    payload_run_key = str((payload or {}).get("run_key") or "").strip()
    expected_run_key = str(run_key or "").strip()
    return bool(payload_run_key and expected_run_key and payload_run_key == expected_run_key)


def _resume_merge_from_audit(resume_run_key: str, incoming: dict) -> dict:
    """
    Rebuild a full POST /runs payload from a prior run stored in RUN_AUDIT.

    Used when start_stage is transform or load: reuse the saved pipeline template and
    export folder (same sf_pipeline_run_id / run_key), without resending the whole JSON.

    Profile safety: source_profile / target_profile cannot change unless allow_profile_override
    is true (avoids mixing orgs with on-disk artifacts).
    """
    row = RUN_AUDIT.get_run(resume_run_key)
    if not row or not (row.get("request_payload_json") or "").strip():
        raise ValueError(
            f"No stored request payload for run_key '{resume_run_key}'. "
            "Complete at least one full run so the audit store captures the template, or send the full pipelines[] body."
        )
    base = json.loads(row["request_payload_json"])
    merged = dict(base)
    start_stage = (incoming.get("start_stage") or "").strip().lower()
    if start_stage not in ("transform", "load"):
        raise ValueError("When using resume_run_key, start_stage must be 'transform' or 'load'")
    pipeline_id = (incoming.get("pipeline_id") or incoming.get("from_pipeline") or "").strip()
    if not pipeline_id:
        raise ValueError("pipeline_id (or from_pipeline) is required when using resume_run_key")

    valid_ids = _pipeline_ids_from_request_pipelines(base)
    if not valid_ids:
        raise ValueError("Stored run template has no pipelines[] — cannot resume.")
    if pipeline_id not in valid_ids:
        raise ValueError(
            f"pipeline_id '{pipeline_id}' not found in stored template. Valid ids: {valid_ids}"
        )

    allow_po = _truthy(incoming.get("allow_profile_override"))
    for key in ("source_profile", "target_profile"):
        if key not in incoming:
            continue
        inc = (incoming.get(key) or "default").strip().lower() or "default"
        base_v = (base.get(key) or "default").strip().lower() or "default"
        if inc != base_v and not allow_po:
            raise ValueError(
                f"Resume cannot change {key} from '{base_v}' to '{inc}' without allow_profile_override=true "
                "(artifacts were produced under the original source/target profiles)."
            )
        if allow_po and inc != base_v:
            logger.warning(
                "Resume allow_profile_override: changing %s from %s to %s — ensure staging matches this choice.",
                key,
                base_v,
                inc,
            )
            merged[key] = incoming[key]

    merged["start_stage"] = start_stage
    merged["from_pipeline"] = pipeline_id
    merged["fresh"] = bool(incoming["fresh"]) if "fresh" in incoming else False
    merged["dry_run"] = bool(incoming.get("dry_run", False))
    merged["_resume_source_run_key"] = resume_run_key

    for key in (
        "migration_name",
        "sf_pipeline_run_id",
        "sf_pipeline_id",
        "submitted_by_user_id",
        "submitted_by_username",
        "submitted_by_name",
        "submitted_by_email",
        "callback",
    ):
        if key in incoming and incoming[key] is not None:
            merged[key] = incoming[key]

    merged["resume_run_key"] = resume_run_key
    return merged


def _start_pipeline_run_request(data: dict, origin: str = "ui") -> tuple[dict, int]:
    """
    Start a pipeline run (writes config/migration.yaml and spawns cli).

    **Request precedence**
    1. If ``pipelines`` is present and non-empty → use the body as the full template.
    2. Else if ``resume_run_key`` is set → hydrate from ``RUN_AUDIT`` (``start_stage`` must be
       ``transform`` or ``load``); validates ``pipeline_id`` against the stored template.
    3. Else → error (missing ``pipelines``).

    For (2), on-disk exports under ``exports/run_sf_<key>/`` (``resume_run_key`` if set, else
    ``sf_pipeline_run_id``) must contain the expected ``raw/`` or ``xfm/`` CSVs for the chosen
    pipeline (see ``_validate_resume_artifacts``).
    """
    global _active_run_process, _active_run_meta

    resume_run_key = (data.get("resume_run_key") or "").strip()
    pipelines_in = data.get("pipelines")
    resume_applied = False
    if resume_run_key and (pipelines_in is None or (isinstance(pipelines_in, list) and len(pipelines_in) == 0)):
        try:
            data = _resume_merge_from_audit(resume_run_key, data)
            resume_applied = True
        except ValueError as e:
            return {"error": str(e)}, 400

    migration_name = str(data.get("migration_name") or "").strip()
    fresh = bool(data.get("fresh", True))
    if not resume_applied and fresh:
        try:
            _assert_required_unique_pipeline_name(
                migration_name,
                for_migration=True,
                require_unique=False,
            )
        except ValueError as e:
            return {"error": str(e)}, 400
    elif not migration_name:
        return {"error": "Pipeline Name is required."}, 400
    dry_run = bool(data.get("dry_run", False))
    from_pipeline = (data.get("from_pipeline") or "").strip() or None
    start_stage = (data.get("start_stage") or "extract").strip().lower()
    source_profile = (data.get("source_profile") or "default").strip().lower() or "default"
    target_profile = (data.get("target_profile") or "default").strip().lower() or "default"
    sf_pipeline_run_id = (data.get("sf_pipeline_run_id") or "").strip()
    sf_pipeline_attempt = str(data.get("sf_pipeline_attempt") or "").strip()
    resume_rk = (data.get("resume_run_key") or "").strip()
    sf_pipeline_id = (data.get("sf_pipeline_id") or "").strip()
    run_key = sf_pipeline_run_id or f"sf_run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"

    try:
        pipeline_entries = _build_pipeline_entries_from_request(data)
        pipeline_entries = _subset_pipeline_entries(pipeline_entries, from_pipeline)
    except ValueError as e:
        return {"error": str(e)}, 400

    has_flat_file_stage = any(
        (entry.get("input") or {}).get("type") == "flat_file"
        for entry in pipeline_entries
    )
    has_source_stage = any(
        (entry.get("input") or {}).get("type") != "flat_file"
        for entry in pipeline_entries
    )
    if has_flat_file_stage and has_source_stage:
        return {
            "error": (
                "A pipeline cannot mix SF → SF and Files → SF stages. "
                "Create separate pipelines for each operation type."
            ),
        }, 400

    if origin == "ui" and not resume_applied:
        snapshot_error = _saved_pipeline_snapshot_error(data)
        if snapshot_error:
            return {"error": snapshot_error}, 409

    if not resume_applied:
        gate_errors = _pipeline_prevalidation_gate_errors(pipeline_entries)
        if gate_errors:
            return {
                "error": "Migration blocked by pre-validation gate.",
                "prevalidation_errors": gate_errors,
            }, 400

    # Flat-file runs do not use a Salesforce source; keep target profile authoritative.
    if has_flat_file_stage and not has_source_stage:
        source_profile = "default"

    try:
        pipeline_entries = _normalize_pipeline_external_ids(pipeline_entries, target_profile)
    except ValueError as e:
        return {"error": str(e)}, 400

    if resume_applied and start_stage in ("transform", "load"):
        exports_key = (
            (data.get("resume_run_key") or "").strip()
            or (data.get("sf_pipeline_run_id") or "").strip()
            or resume_run_key
        )
        if not exports_key:
            return {
                "error": "Resume requires resume_run_key (or the original run id) to locate export artifacts.",
            }, 400
        if not from_pipeline:
            return {"error": "from_pipeline / pipeline_id is required for resume."}, 400
        try:
            _validate_resume_artifacts(exports_key, from_pipeline, start_stage, pipeline_entries)
        except ResumeArtifactError as e:
            return {"error": str(e)}, getattr(e, "status_code", 409)

    from core.loader import DEFAULT_LOAD_BATCH_SIZE, resolve_load_batch_size
    try:
        load_batch_size = resolve_load_batch_size(
            data.get("load_batch_size", DEFAULT_LOAD_BATCH_SIZE)
        )
    except ValueError as e:
        return {"error": str(e)}, 400

    config_payload = {
        "migration": {
            "name": migration_name,
            "input_mode": "flat_file" if has_flat_file_stage else "salesforce_source",
            "load_batch_size": load_batch_size,
        },
        "pipelines": pipeline_entries,
    }
    config_path = _run_config_path(run_key)
    state_path = _run_state_path(run_key)
    log_path = _run_log_path(run_key)
    staging_db_path = PROJECT_ROOT / "state" / f"{_run_dir_name_for_key(run_key) or 'run_sf_default'}_staging.duckdb"
    id_map_db_path = PROJECT_ROOT / "state" / f"{_run_dir_name_for_key(run_key) or 'run_sf_default'}_id_map.duckdb"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.dump(config_payload, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    if has_flat_file_stage:
        try:
            flat_stager = Stager(staging_db_path)
            try:
                staged_templates: set[str] = set()
                pipeline_models = [
                    SimpleNamespace(
                        pipeline_id=str(entry.get("id") or ""),
                        name=str(entry.get("name") or entry.get("id") or ""),
                    )
                    for entry in pipeline_entries
                ]
                artifact_dir_map = build_pipeline_artifact_dir_map(pipeline_models)
                export_run_dir = PROJECT_ROOT / "exports" / (
                    _run_dir_name_for_key(run_key) or f"run_{run_key}"
                )
                for entry in pipeline_entries:
                    input_cfg = entry.get("input") or {}
                    if input_cfg.get("type") != "flat_file":
                        continue
                    template_id = str(input_cfg.get("template_id") or "").strip()
                    if not template_id or template_id in staged_templates:
                        continue
                    template = FlatFileTemplateStore(FLAT_FILE_TEMPLATE_DIR).get(template_id)
                    batch_dir = FLAT_FILE_UPLOAD_DIR / str(template.get("batch_id") or "")
                    pipeline_id = str(entry.get("id") or "")
                    attempt_dir = export_run_dir / artifact_dir_map[pipeline_id]
                    _archive_flat_file_inputs(template, batch_dir, attempt_dir)
                    FlatFileInput(flat_stager, batch_dir, FLAT_FILE_DROP_DIR).ingest(template, replace=True)
                    staged_templates.add(template_id)
            finally:
                flat_stager.close()
        except (FileNotFoundError, FlatFileValidationError, ValueError) as e:
            return {"error": f"Flat-file staging failed: {e}"}, 400
        if start_stage == "extract":
            start_stage = "transform"

    try:
        command = _build_ui_run_command(
            start_stage,
            config_path,
            dry_run,
            fresh,
            stop_after_transform=bool(data.get("stop_after_transform", False)),
            from_pipeline=from_pipeline,
        )
    except ValueError as e:
        return {"error": str(e)}, 400
    submitter = {
        "submitted_by_user_id": (data.get("submitted_by_user_id") or "").strip() or None,
        "submitted_by_username": (data.get("submitted_by_username") or "").strip() or None,
        "submitted_by_name": (data.get("submitted_by_name") or "").strip() or None,
        "submitted_by_email": (data.get("submitted_by_email") or "").strip() or None,
    }
    callback = data.get("callback") if isinstance(data.get("callback"), dict) else {}

    # Email masking defaults by target type.
    if "mask_emails" in data:
        mask_emails = bool(data["mask_emails"])
    else:
        mask_emails = _is_sandbox_target(data)
    if "remove_email_mask" in data:
        remove_email_mask = bool(data["remove_email_mask"])
    else:
        remove_email_mask = _is_production_target(data)
    if mask_emails and remove_email_mask:
        remove_email_mask = False
    stop_after_transform = bool(data.get("stop_after_transform", False))

    extra_env = {
        "ETL_MASK_EMAILS": "1" if mask_emails else "",
        "ETL_REMOVE_EMAIL_MASK": "1" if remove_email_mask else "",
        "ETL_STOP_AFTER_TRANSFORM": "1" if stop_after_transform else "",
        "ETL_LOAD_BATCH_SIZE": str(load_batch_size),
        "ACTIVE_SOURCE_PROFILE": source_profile,
        "ACTIVE_TARGET_PROFILE": target_profile,
        "ETL_RUN_ORIGIN": origin,
        "ETL_RUN_KEY": run_key,
        "ETL_RUN_DIR_NAME": _run_dir_name_for_key(run_key) or "",
        "ETL_RUN_STATE_FILE": str(state_path),
        "ETL_STAGING_DB_PATH": str(staging_db_path),
        "ETL_ID_MAP_DB_PATH": str(id_map_db_path),
        "ETL_MIGRATION_NAME": migration_name,
        "SF_PIPELINE_RUN_ID": sf_pipeline_run_id,
        "SF_PIPELINE_ID": sf_pipeline_id,
        "ETL_SUBMITTED_BY_USER_ID": submitter["submitted_by_user_id"] or "",
        "ETL_SUBMITTED_BY_USERNAME": submitter["submitted_by_username"] or "",
        "ETL_SUBMITTED_BY_NAME": submitter["submitted_by_name"] or "",
        "ETL_SUBMITTED_BY_EMAIL": submitter["submitted_by_email"] or "",
        "ETL_CALLBACK_URL": (callback.get("url") or "").strip(),
        "ETL_CALLBACK_TOKEN": (callback.get("token") or "").strip(),
    }

    try:
        runner = _launch_background_command(
            command,
            f"Starting {origin}-triggered pipeline run | start_stage={start_stage} | run_key={run_key}",
            extra_env=extra_env,
            run_meta={
                "run_key": run_key,
                "origin": origin,
                "sf_pipeline_run_id": sf_pipeline_run_id or None,
                "sf_pipeline_id": sf_pipeline_id or None,
                "config_path": str(config_path),
                "state_path": str(state_path),
                "log_path": str(log_path),
            },
        )
    except Exception as e:
        return {"error": str(e)}, 500

    resume_meta: dict = {}
    rsk = data.pop("_resume_source_run_key", None)
    if rsk:
        resume_meta["resumed_from_run_key"] = rsk
        resume_meta["effective_start_stage"] = start_stage
        resume_meta["effective_from_pipeline"] = from_pipeline

    # Persist every start (UI + Salesforce) so transform/load resume can reload the template from audit.
    data_operations = sorted({
        str(obj.get("operation") or "upsert").strip().upper()
        for entry in pipeline_entries
        for obj in (entry.get("objects") or [])
        if isinstance(obj, dict)
    }) or ["UPSERT"]
    RUN_AUDIT.upsert_submission(
        run_key,
        {
            "activity_type": "MIGRATION",
            "data_operation": ", ".join(data_operations),
            "sf_pipeline_run_id": sf_pipeline_run_id or None,
            "sf_pipeline_id": sf_pipeline_id or None,
            "sf_pipeline_attempt": sf_pipeline_attempt or None,
            "start_stage": start_stage,
            "effective_from_pipeline": from_pipeline,
            "resumed_from_run_key": rsk or (resume_rk or None),
            "migration_name": migration_name,
            "source_profile": source_profile,
            "target_profile": target_profile,
            "submitted_by_user_id": submitter["submitted_by_user_id"],
            "submitted_by_username": submitter["submitted_by_username"],
            "submitted_by_name": submitter["submitted_by_name"],
            "submitted_by_email": submitter["submitted_by_email"],
            "callback_url": (callback.get("url") or "").strip() or None,
            "callback_enabled": bool((callback.get("url") or "").strip()),
            "status": "QUEUED",
            "request_payload_json": json.dumps(data, ensure_ascii=False),
            "runner_json": json.dumps(runner, ensure_ascii=False),
        },
    )

    response_body: dict = {
        "ok": True,
        "accepted": True,
        "message": "Pipeline run started",
        "run_key": run_key,
        "sf_pipeline_run_id": sf_pipeline_run_id or None,
        "sf_pipeline_id": sf_pipeline_id or None,
        "sf_pipeline_attempt": sf_pipeline_attempt or None,
        "start_stage": start_stage,
        "effective_from_pipeline": from_pipeline,
        "status": "QUEUED",
        "runner": runner,
        "progress_url": f"/pipeline/runs/{run_key}",
    }
    if resume_meta:
        response_body["resume"] = resume_meta
    return response_body, 200


def _stop_pipeline_run_request(origin: str = "ui", expected_run_key: str | None = None) -> tuple[dict, int]:
    global _active_run_process, _active_run_meta, _last_run_key

    if expected_run_key:
        active_entry = _active_runs.get(expected_run_key)
        if not active_entry:
            runner = _runner_status(expected_run_key)
            audit = RUN_AUDIT.get_run(expected_run_key) or {}
            if runner.get("running") and str(audit.get("activity_type") or "").upper() in {"VALIDATION", "PRE_VALIDATION"}:
                pid = runner.get("pid")
                try:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture_output=True, text=True, check=False)
                    else:
                        os.kill(int(pid), signal.SIGTERM)
                except (OSError, ValueError) as exc:
                    return {"error": str(exc), "runner": runner}, 500
                stopped_runner = {**runner, "running": False, "external": True}
                RUN_AUDIT.update_status(expected_run_key, status="STOPPED", runner=stopped_runner, error_message="Stopped by user")
                return {"ok": True, "message": "Validation run stopped", "runner": stopped_runner, "run_key": expected_run_key, "status": "STOPPED"}, 200
            return {
                "error": f"Run '{expected_run_key}' is not currently running",
                "runner": runner,
            }, 409
        process = active_entry.get("process")
        meta = active_entry.get("meta") or {}
        if not process or process.poll() is not None:
            _active_runs.pop(expected_run_key, None)
            return {"error": f"Run '{expected_run_key}' is not currently running"}, 409
        try:
            _terminate_process_tree(process)
            process.wait(timeout=10)
        except Exception as exc:
            return {"error": str(exc), "runner": _runner_status(expected_run_key)}, 500

        meta["returncode"] = process.returncode
        _active_runs.pop(expected_run_key, None)
        _mark_run_stopped(expected_run_key)
        if _last_run_key == expected_run_key:
            _last_run_key = next(reversed(_active_runs), None) if _active_runs else None
        if _active_run_process is process:
            replacement = _active_runs.get(_last_run_key or "") or {}
            _active_run_process = replacement.get("process")
            _active_run_meta = replacement.get("meta") or meta
        stopped_runner = {
            **meta,
            "running": False,
            "run_key": expected_run_key,
            "active_run_count": len(_active_runs),
        }
        RUN_AUDIT.update_status(
            expected_run_key,
            status="STOPPED",
            runner=stopped_runner,
            status_payload=_current_status_payload(expected_run_key),
            error_message="Stopped by user",
        )
        return {
            "ok": True,
            "message": "Pipeline run stopped",
            "runner": stopped_runner,
            "run_key": expected_run_key,
            "status": "STOPPED",
        }, 200

    current_runner = _runner_status()
    if not current_runner.get("running"):
        return {"error": "No active pipeline run found", "runner": current_runner}, 409

    active_run_key = current_runner.get("run_key") or _active_run_meta.get("run_key")
    if expected_run_key and active_run_key and expected_run_key != active_run_key:
        return {
            "error": f"Run '{expected_run_key}' is not the active run",
            "active_run_key": active_run_key,
            "runner": current_runner,
        }, 409

    if current_runner.get("external"):
        ext_pid = current_runner.get("pid")
        if ext_pid is None:
            return {"error": "Cannot determine PID of external run"}, 500
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(ext_pid), "/T", "/F"],
                    capture_output=True, text=True, check=False,
                )
            else:
                os.kill(ext_pid, signal.SIGTERM)
        except Exception as e:
            return {"error": str(e), "runner": _runner_status()}, 500
        _mark_external_run_stopped()
        stopped_runner = _runner_status()
        if origin == "sf" and active_run_key:
            RUN_AUDIT.update_status(
                active_run_key,
                status="STOPPED",
                runner=stopped_runner,
                status_payload=_current_status_payload(),
                error_message="Stopped by user via Salesforce",
            )
        return {
            "ok": True,
            "message": "External pipeline run stopped",
            "runner": stopped_runner,
            "run_key": active_run_key,
            "status": "STOPPED",
        }, 200

    if _active_run_process is None:
        return {"error": "No active pipeline run found", "runner": current_runner}, 409

    try:
        _terminate_process_tree(_active_run_process)
        _active_run_process.wait(timeout=10)
    except Exception as e:
        return {"error": str(e), "runner": _runner_status()}, 500

    _active_run_meta["returncode"] = _active_run_process.returncode
    _active_run_process = None
    stopped_runner = _runner_status()
    if origin == "sf" and active_run_key:
        RUN_AUDIT.update_status(
            active_run_key,
            status="STOPPED",
            runner=stopped_runner,
            status_payload=_current_status_payload(),
            error_message="Stopped by user via Salesforce",
        )
    return {
        "ok": True,
        "message": "Pipeline run stopped",
        "runner": stopped_runner,
        "run_key": active_run_key,
        "status": "STOPPED",
    }, 200


def _active_audited_validation(exclude_run_key: str | None = None) -> dict | None:
    """Find a live validation worker that survived an application restart."""
    for record in RUN_AUDIT.list_runs(200):
        if str(record.get("run_key") or "") == str(exclude_run_key or ""):
            continue
        if str(record.get("activity_type") or "").upper() not in {"VALIDATION", "PRE_VALIDATION"}:
            continue
        record = _reconcile_finished_validation(record)
        if str(record.get("status") or "").upper() not in {"RUNNING", "QUEUED"}:
            continue
        runner = _audit_runner_status(str(record.get("run_key") or "")) or {}
        if runner.get("running"):
            return {"run_key": record.get("run_key"), "activity_type": record.get("activity_type"), "runner": runner}
    return None


def _subset_pipeline_entries(pipeline_entries: list[dict], from_pipeline: str | None) -> list[dict]:
    if not from_pipeline:
        return pipeline_entries
    ids = [entry.get("id") for entry in pipeline_entries]
    if from_pipeline not in ids:
        raise ValueError(f"Pipeline '{from_pipeline}' not found in request")
    start_idx = ids.index(from_pipeline)
    subset = pipeline_entries[start_idx:]
    subset_ids = {entry.get("id") for entry in subset}
    for entry in subset:
        depends_on = entry.get("depends_on") or []
        entry["depends_on"] = [dep for dep in depends_on if dep in subset_ids]
    return subset


def _build_ui_run_command(
    start_stage: str,
    config_path: Path,
    dry_run: bool,
    fresh: bool,
    stop_after_transform: bool = False,
    from_pipeline: str | None = None,
) -> list[str]:
    base_python = sys.executable
    if start_stage == "extract":
        command = [base_python, "cli.py", "run", "--config", str(config_path), "--write-artifacts"]
        if from_pipeline:
            command.extend(["--from-pipeline", from_pipeline])
        if fresh:
            command.append("--fresh")
        if dry_run:
            command.append("--dry-run")
        return command

    if start_stage == "load":
        command = [base_python, "cli.py", "load", "--config", str(config_path)]
        if dry_run:
            command.append("--dry-run")
        return command

    if start_stage == "transform":
        if stop_after_transform:
            return [base_python, "cli.py", "transform", "--config", str(config_path)]
        steps = [
            [base_python, "cli.py", "transform", "--config", str(config_path)],
            [base_python, "cli.py", "load", "--config", str(config_path)],
        ]
        if dry_run:
            steps[-1].append("--dry-run")
        script = (
            "import subprocess, sys\n"
            f"steps = {steps!r}\n"
            "for step in steps:\n"
            "    result = subprocess.run(step)\n"
            "    if result.returncode:\n"
            "        sys.exit(result.returncode)\n"
        )
        return [base_python, "-c", script]

    raise ValueError(f"Unsupported start stage: {start_stage}")


def _terminate_process_tree(process: subprocess.Popen):
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        return

    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except Exception:
        process.terminate()


# ------------------------------------------------------------------ #
#  API — Generate Migration Config                                    #
# ------------------------------------------------------------------ #

@app.route("/api/config/generate", methods=["POST"])
def generate_config():
    """
    Generate a migration YAML config from the user's selections in the UI.
    Body: { seed_object, selections: [{object, fields, query_filter, external_id}], traversal }
    """
    data = request.json or {}
    migration_name = data.get("migration_name", "Custom Migration")
    pipeline_requests = data.get("pipelines") or []

    try:
        if pipeline_requests:
            seen_ids: set[str] = set()
            pipeline_entries: list[dict] = []
            previous_pipeline_id = None
            for order, pipeline_data in enumerate(pipeline_requests, start=1):
                entry = _build_pipeline_entry(pipeline_data, order)
                entry = _ensure_unique_pipeline_entry_id(entry, seen_ids)
                seen_ids.add(entry["id"])
                if previous_pipeline_id:
                    entry["depends_on"] = [previous_pipeline_id]
                previous_pipeline_id = entry["id"]
                pipeline_entries.append(entry)
        else:
            pipeline_entries = [_build_pipeline_entry(data, 1)]
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    has_flat_file_stage = any(
        (entry.get("input") or {}).get("type") == "flat_file"
        for entry in pipeline_entries
    )
    config = {
        "migration": {
            "name": migration_name,
            "input_mode": "flat_file" if has_flat_file_stage else "salesforce_source",
        },
        "pipelines": pipeline_entries,
    }

    yaml_str = yaml.dump(config, default_flow_style=False, allow_unicode=True, sort_keys=False)

    # Optionally save to file
    if data.get("save_to_file"):
        config_path = Path(__file__).parent.parent / "config" / "migration.yaml"
        config_path.write_text(yaml_str, encoding="utf-8")

    return jsonify({"yaml": yaml_str})


@app.route("/api/templates", methods=["GET"])
def list_templates():
    TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    templates = []
    for path in sorted(TEMPLATE_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            templates.append(_template_summary(path, payload))
        except Exception:
            templates.append({"id": path.stem, "name": path.stem})
    return jsonify({"templates": templates})


@app.route("/api/pipeline-designer/snapshots", methods=["GET"])
def list_pipeline_designer_snapshots():
    PIPELINE_DESIGNER_DIR.mkdir(parents=True, exist_ok=True)
    snapshots = []
    for path in sorted(PIPELINE_DESIGNER_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        snapshots.append({
            "id": path.stem,
            "name": payload.get("name") or path.stem,
            "migration_name": payload.get("migrationName") or "",
            "stage_count": len(payload.get("pipelineStages") or []),
            "updated_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
        })
    snapshots.sort(key=lambda entry: entry.get("updated_at", ""), reverse=True)
    return jsonify({"snapshots": snapshots})


@app.route("/api/transform-profiles", methods=["GET"])
def list_transform_profiles():
    """List reusable object-scoped transformation rule sets."""
    object_name = str(request.args.get("object") or "").strip()
    TRANSFORM_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    profiles = []
    for path in sorted(TRANSFORM_PROFILE_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if object_name and payload.get("object") != object_name:
            continue
        profiles.append(payload)
    profiles.sort(key=lambda item: str(item.get("name") or "").casefold())
    return jsonify({"profiles": profiles})


@app.route("/api/transform-profiles", methods=["POST"])
def save_transform_profile():
    data = request.json or {}
    name = str(data.get("name") or "").strip()
    object_name = str(data.get("object") or "").strip()
    rules = data.get("rules")
    if not name or not object_name:
        return jsonify({"error": "Profile name and Salesforce object are required."}), 400
    if not isinstance(rules, list) or not rules:
        return jsonify({"error": "Add at least one transformation rule before saving."}), 400
    invalid = [
        index + 1
        for index, rule in enumerate(rules)
        if not isinstance(rule, dict)
        or not str(rule.get("type") or "").strip()
        or not str(rule.get("field") or "").strip()
    ]
    if invalid:
        return jsonify({
            "error": f"Transformation rule(s) {', '.join(map(str, invalid))} require type and field."
        }), 400
    profile_id = (
        f"{_slugify_pipeline_snapshot_name(object_name)}--"
        f"{_slugify_pipeline_snapshot_name(name)}"
    )
    payload = {
        "id": profile_id,
        "name": name,
        "object": object_name,
        "rules": [dict(rule) for rule in rules],
        "saved_at": _utc_now(),
    }
    TRANSFORM_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    path = (TRANSFORM_PROFILE_DIR / f"{profile_id}.json").resolve()
    if path.parent != TRANSFORM_PROFILE_DIR.resolve():
        return jsonify({"error": "Invalid transformation profile path."}), 400
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return jsonify({"ok": True, "profile": payload})


@app.route("/api/transform-profiles/<profile_id>", methods=["DELETE"])
def delete_transform_profile(profile_id: str):
    normalized_parts = profile_id.split("--", 1)
    if (
        len(normalized_parts) != 2
        or any(_slugify_pipeline_snapshot_name(part) != part for part in normalized_parts)
    ):
        return jsonify({"error": "Invalid transformation profile ID."}), 400
    path = (TRANSFORM_PROFILE_DIR / f"{profile_id}.json").resolve()
    if path.parent != TRANSFORM_PROFILE_DIR.resolve() or not path.exists():
        return jsonify({"error": "Transformation profile not found."}), 404
    path.unlink()
    return jsonify({"ok": True, "profile_id": profile_id})


@app.route("/api/pipeline-designer/snapshots", methods=["POST"])
def save_pipeline_designer_snapshot():
    data = request.json or {}
    name = (data.get("name") or "").strip()
    snapshot = data.get("snapshot") or {}
    if not name:
        return jsonify({"error": "Snapshot name is required"}), 400
    if not isinstance(snapshot, dict):
        return jsonify({"error": "snapshot must be an object"}), 400

    snapshot_id = _slugify_pipeline_snapshot_name(name)
    payload = {
        "id": snapshot_id,
        "name": name,
        "migrationName": snapshot.get("migrationName") or "",
        "pipelineType": snapshot.get("pipelineType") or "",
        "startStage": snapshot.get("startStage") or "extract",
        "sourceProfile": snapshot.get("sourceProfile") or "default",
        "targetProfile": snapshot.get("targetProfile") or "default",
        "pipelineStages": snapshot.get("pipelineStages") or [],
        "panels": snapshot.get("panels") or {},
        "failedFiles": snapshot.get("failedFiles") or {},
        "saved_at": _utc_now(),
    }

    PIPELINE_DESIGNER_DIR.mkdir(parents=True, exist_ok=True)
    path = (PIPELINE_DESIGNER_DIR / f"{snapshot_id}.json").resolve()
    if path.parent != PIPELINE_DESIGNER_DIR.resolve():
        return jsonify({"error": "Invalid snapshot path"}), 400
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return jsonify({"ok": True, "snapshot": {"id": snapshot_id, "name": name}})


@app.route("/api/pipeline-designer/snapshots/<snapshot_id>", methods=["GET"])
def get_pipeline_designer_snapshot(snapshot_id: str):
    normalized_id = _slugify_pipeline_snapshot_name(snapshot_id)
    if normalized_id != snapshot_id:
        return jsonify({"error": "Invalid snapshot id"}), 400
    path = (PIPELINE_DESIGNER_DIR / f"{snapshot_id}.json").resolve()
    if path.parent != PIPELINE_DESIGNER_DIR.resolve() or not path.exists():
        return jsonify({"error": "Snapshot not found"}), 404
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return jsonify(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _saved_pipeline_snapshot_error(data: dict) -> str | None:
    """Require UI pipeline actions to reference an existing saved snapshot."""
    snapshot_id = str(data.get("snapshot_id") or "").strip()
    if not snapshot_id:
        return "Save the current pipeline snapshot before running pre-validation or migration."
    if _slugify_pipeline_snapshot_name(snapshot_id) != snapshot_id:
        return "The selected pipeline snapshot ID is invalid. Save the pipeline again."
    snapshot_path = (PIPELINE_DESIGNER_DIR / f"{snapshot_id}.json").resolve()
    if snapshot_path.parent != PIPELINE_DESIGNER_DIR.resolve() or not snapshot_path.is_file():
        return "The saved pipeline snapshot no longer exists. Save the pipeline again."
    return None


@app.route("/api/pipeline-designer/snapshots/<snapshot_id>", methods=["DELETE"])
def delete_pipeline_designer_snapshot(snapshot_id: str):
    normalized_id = _slugify_pipeline_snapshot_name(snapshot_id)
    if normalized_id != snapshot_id:
        return jsonify({"error": "Invalid snapshot id"}), 400
    path = (PIPELINE_DESIGNER_DIR / f"{snapshot_id}.json").resolve()
    if path.parent != PIPELINE_DESIGNER_DIR.resolve():
        return jsonify({"error": "Invalid snapshot path"}), 400
    if not path.exists():
        return jsonify({"error": "Snapshot not found"}), 404
    try:
        path.unlink()
        return jsonify({"ok": True, "snapshot_id": snapshot_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ------------------------------------------------------------------ #
#  API - Flat-file migration designer                                #
# ------------------------------------------------------------------ #

def _general_utility_drop_file(relative_name: str) -> Path:
    relative = Path(str(relative_name or "").strip())
    root = FLAT_FILE_DROP_DIR.resolve()
    if not str(relative) or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Invalid state-folder CSV path.")
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root) or resolved.suffix.casefold() != ".csv" or not resolved.is_file():
        raise ValueError(f"State-folder CSV was not found: {relative_name}")
    return resolved


def _general_utility_object_list_path(raw_path: str) -> Path:
    """Resolve an absolute or project-relative object-list file path."""
    text = str(raw_path or "").strip().strip('"')
    if not text:
        raise ValueError("Provide the file path of the object list.")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = (PROJECT_ROOT / path).resolve()
    else:
        path = path.resolve()
    if not path.is_file():
        raise ValueError(f"Object list file not found: {path}")
    if path.suffix.lower() not in {".csv", ".txt", ".text", ".list"}:
        raise ValueError("Object list must be a .csv or .txt file.")
    return path


def _bulk_fls_result_rows(objects: list[dict]) -> list[dict]:
    """Flatten bulk FLS object results into progress-table rows."""
    rows = []
    for item in objects or []:
        object_name = str(item.get("object") or "")
        if item.get("error"):
            rows.append({
                "field": object_name,
                "created": False,
                "reason": "error",
                "id": "",
                "permissions": [{"principal": "—", "created": False, "reason": item["error"]}],
            })
            continue
        for principal in item.get("principals") or []:
            object_perm = principal.get("object_permissions") or {}
            reason_parts = [
                f"fields +{principal.get('field_created', 0)}/~{principal.get('field_updated', 0)}",
                f"recordTypes {principal.get('record_type_granted', 0)}",
            ]
            if principal.get("field_errors") or principal.get("record_type_errors") or object_perm.get("error"):
                reason_parts.append(
                    f"errors field={principal.get('field_errors', 0)} "
                    f"rt={principal.get('record_type_errors', 0)}"
                )
            rows.append({
                "field": object_name,
                "created": bool(object_perm.get("created")),
                "reason": object_perm.get("reason") or ("updated" if object_perm.get("updated") else "ok"),
                "id": object_perm.get("id") or "",
                "permissions": [{
                    "principal": principal.get("principal") or "—",
                    "created": bool(object_perm.get("created") or object_perm.get("updated")),
                    "reason": "; ".join(reason_parts),
                    "id": object_perm.get("id") or "",
                }],
            })
    return rows


def _general_utility_run_dir() -> Path:
    run_dir = GENERAL_UTILITY_OUTPUT_DIR / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _general_utility_job_path(job_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", str(job_id or "")):
        raise ValueError("Invalid General Utility job ID.")
    return GENERAL_UTILITY_JOB_DIR / f"{job_id}.json"


def _persist_general_utility_job(job: dict) -> None:
    GENERAL_UTILITY_JOB_DIR.mkdir(parents=True, exist_ok=True)
    path = _general_utility_job_path(str(job.get("id") or ""))
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _read_general_utility_job(job_id: str) -> dict | None:
    try:
        path = _general_utility_job_path(job_id)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _reconcile_general_utility_job(job_id: str, job: dict | None = None) -> dict | None:
    """Make a persisted active job terminal after its worker disappeared."""
    stored = job or _read_general_utility_job(job_id)
    if not stored or str(stored.get("status") or "").casefold() not in {"queued", "running"}:
        return stored
    with _general_utility_jobs_lock:
        if job_id in _general_utility_jobs:
            return stored

    finished_at = datetime.now(timezone.utc).isoformat()
    message = "Failed: the job worker stopped during an application stop or restart. Re-run the operation."
    repaired = {
        **stored,
        "status": "error",
        "message": message,
        "error": (
            "The General Utility worker is no longer running (the application was stopped or restarted). "
            "Salesforce changes completed before the interruption are preserved; re-run Create fields to "
            "verify them and continue safely."
        ),
        "finished_at": finished_at,
    }
    repaired.setdefault("events", []).append({"time": finished_at, "message": message})
    _persist_general_utility_job(repaired)
    RUN_AUDIT.update_status(
        job_id,
        status="FAILED",
        status_payload=repaired,
        error_message=repaired["error"],
        completed_at=finished_at,
    )
    return repaired


def _csv_identity_inventory(path: Path, column: str = "External_Id__c") -> set[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        actual = next((name for name in reader.fieldnames or [] if name.casefold() == column.casefold()), None)
        if not actual:
            raise ValueError(f"{path.name} does not contain {column}.")
        return {str(row.get(actual) or "").strip() for row in reader if str(row.get(actual) or "").strip()}


def _target_identity_inventory(connector, object_name: str, values: set[str], external_id: str = "External_Id__c") -> set[str]:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", object_name) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", external_id):
        raise ValueError("Invalid Salesforce object or external-ID field name.")
    field = next((item for item in connector.describe_object(object_name).get("fields", []) if str(item.get("name") or "").casefold() == external_id.casefold()), None)
    if not field:
        raise ValueError(f"Target {object_name}.{external_id} does not exist.")
    found = set()
    ordered = sorted(values)
    for start in range(0, len(ordered), 200):
        literals = ",".join("'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'" for value in ordered[start:start + 200])
        for row in connector.query(f"SELECT {external_id} FROM {object_name} WHERE {external_id} IN ({literals})"):
            value = str(row.get(external_id) or "").strip()
            if value:
                found.add(value)
    return found


@app.route("/api/general-utility/analyze", methods=["POST"])
def analyze_general_utility_file():
    body = request.json or {}
    try:
        source = _general_utility_drop_file(body.get("file"))
        object_name = str(body.get("object") or "").strip()
        profile = str(body.get("target_profile") or "default").strip() or "default"
        prefix = str(body.get("prefix") or "Legacy_").strip()
        connector = get_connector("target", profile)
        describe = connector.describe_object(object_name)
        result = discover_populated_references(source, describe, prefix)
        existing = {str(field.get("name") or "").casefold(): field for field in describe.get("fields", [])}
        for item in result["preservation_candidates"]:
            meta = existing.get(item["legacy_field"].casefold())
            item["target_field_exists"] = bool(meta)
            item["target_field_type"] = str((meta or {}).get("type") or "")
        return jsonify({**result, "file": body.get("file"), "object": object_name, "prefix": prefix})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/general-utility/prepare-columns", methods=["POST"])
def prepare_general_utility_columns():
    body = request.json or {}
    try:
        source = _general_utility_drop_file(body.get("file"))
        run_dir = _general_utility_run_dir()
        output = run_dir / f"{source.stem}_prepared.csv"
        result = write_prepared_csv(source, output, preservation=body.get("preservation"), concatenations=body.get("concatenations"))
        manifest = manifest_payload("COLUMN_PREPARATION", source, output, result)
        (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return jsonify({**manifest, "run_id": run_dir.name, "download": f"/api/general-utility/runs/{run_dir.name}/download/{output.name}", "manifest_download": f"/api/general-utility/runs/{run_dir.name}/download/manifest.json"})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/general-utility/apply-fallbacks", methods=["POST"])
def apply_general_utility_fallbacks():
    body = request.json or {}
    try:
        source = _general_utility_drop_file(body.get("file"))
        rules = body.get("rules") or []
        if not rules:
            raise ValueError("Select at least one populated reference field.")
        mode = str(body.get("inventory_mode") or "local").casefold()
        inventories = {}
        source_values = {}
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if any(str(name or "").casefold() in {"sf__error", "sf__id"} for name in reader.fieldnames or []):
                raise ValueError("Salesforce result CSVs cannot be used for fallback preparation. Select the clean original source CSV.")
            source_rows = list(reader)
        for rule in rules:
            column = str(rule.get("column") or "")
            referenced_object = str(rule.get("referenced_object") or "")
            source_values.setdefault(referenced_object, set()).update(str(row.get(column) or "").strip() for row in source_rows if str(row.get(column) or "").strip())
        if mode == "target":
            connector = get_connector("target", str(body.get("target_profile") or "default"))
            for rule in rules:
                source_values.setdefault(str(rule.get("referenced_object") or ""), set()).add(str(rule.get("fallback_id") or "").strip())
            for object_name, values in source_values.items():
                inventories[object_name] = _target_identity_inventory(connector, object_name, values, str(body.get("external_id_field") or "External_Id__c"))
        elif mode == "local":
            inventory_files = body.get("inventory_files") or {}
            for object_name in source_values:
                selected = inventory_files.get(object_name)
                if not selected:
                    raise ValueError(f"Select a local inventory CSV for {object_name}.")
                inventories[object_name] = _csv_identity_inventory(_general_utility_drop_file(selected), str(body.get("external_id_field") or "External_Id__c"))
        else:
            raise ValueError("Inventory mode must be local or target.")
        run_dir = _general_utility_run_dir()
        output = run_dir / f"{source.stem}_fallback_prepared.csv"
        result = apply_fallbacks(source, output, rules, inventories)
        result["inventory_mode"] = mode
        result["inventory_counts"] = {name: len(values) for name, values in inventories.items()}
        manifest = manifest_payload("FALLBACK_ID", source, output, result)
        (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return jsonify({**manifest, "run_id": run_dir.name, "download": f"/api/general-utility/runs/{run_dir.name}/download/{output.name}", "audit_download": f"/api/general-utility/runs/{run_dir.name}/download/{result['audit_file']}"})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/general-utility/create-fields", methods=["POST"])
def create_general_utility_fields():
    body = request.json or {}
    try:
        object_name = str(body.get("object") or "").strip()
        specs = body.get("fields") or []
        if not specs:
            raise ValueError("No fields were selected for creation.")
        spec_names = [str(spec.get("api_name") or "").strip().casefold() for spec in specs]
        if len(spec_names) != len(set(spec_names)):
            raise ValueError("Each Salesforce field API name must be unique.")
        permission_sets = [str(value).strip() for value in body.get("permission_sets") or [] if str(value).strip()]
        profiles = [str(value).strip() for value in body.get("profiles") or [] if str(value).strip()]
        job_id = uuid.uuid4().hex
        total_steps = 3 + (len(specs) * 2)
        job = {
            "id": job_id,
            "job_type": "create_fields",
            "status": "queued",
            "object": object_name,
            "total_steps": total_steps,
            "completed_steps": 0,
            "percent": 0,
            "message": "Waiting to connect to the Target Org...",
            "events": [],
            "fields": [],
            "execution_api": "Salesforce Tooling API + REST FieldPermissions",
            "salesforce_deployment_id": None,
            "target_profile": str(body.get("target_profile") or "default"),
            "permission_sets": permission_sets,
            "profiles": profiles,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        with _general_utility_jobs_lock:
            _general_utility_jobs[job_id] = job
            if len(_general_utility_jobs) > 100:
                finished = [key for key, value in _general_utility_jobs.items() if value.get("status") in {"completed", "error"}]
                for key in finished[: len(_general_utility_jobs) - 100]:
                    _general_utility_jobs.pop(key, None)
            _persist_general_utility_job(job)
        RUN_AUDIT.upsert_submission(job_id, {
            "activity_type": "GENERAL_UTILITY",
            "data_operation": "CREATE FIELDS",
            "migration_name": f"General Utility - {object_name}",
            "target_profile": job["target_profile"],
            "status": "QUEUED",
            "request_payload_json": json.dumps({
                "object": object_name,
                "fields": specs,
                "permission_sets": permission_sets,
                "profiles": profiles,
                "execution_api": job["execution_api"],
            }, ensure_ascii=False),
            "status_payload_json": json.dumps(job, ensure_ascii=False),
        })

        def update_job(message: str, *, advance: bool = False, status: str | None = None, event: bool = True, **values) -> None:
            with _general_utility_jobs_lock:
                current = _general_utility_jobs[job_id]
                if advance:
                    current["completed_steps"] = min(current["completed_steps"] + 1, current["total_steps"])
                if status:
                    current["status"] = status
                current["message"] = message
                current["percent"] = round((current["completed_steps"] / current["total_steps"]) * 100)
                current.update(values)
                if event:
                    current["events"].append({"time": datetime.now(timezone.utc).isoformat(), "message": message})
                snapshot = copy.deepcopy(current)
                _persist_general_utility_job(snapshot)
            audit_status = "COMPLETED" if snapshot["status"] == "completed" else "FAILED" if snapshot["status"] == "error" else "RUNNING"
            RUN_AUDIT.update_status(
                job_id,
                status=audit_status,
                status_payload=snapshot,
                error_message=snapshot.get("error") if audit_status == "FAILED" else None,
                completed_at=snapshot.get("finished_at") if audit_status in {"COMPLETED", "FAILED"} else None,
            )

        def worker() -> None:
            try:
                update_job("Connecting to the selected Target Org...", status="running")
                connector = get_connector("target", str(body.get("target_profile") or "default"))
                update_job("Connected to the Target Org.", advance=True)
                update_job("Validating selected Permission Sets and Profiles...")
                validate_permission_principals(connector, permission_sets, profiles)
                update_job("Permission Sets and Profiles validated.", advance=True)
                results = []
                for index, spec in enumerate(specs, 1):
                    api_name = str(spec.get("api_name") or "")
                    update_job(f"Checking field {index}/{len(specs)}: {object_name}.{api_name}...")
                    field_result = create_custom_field(connector, object_name, spec)
                    action = "Created" if field_result.get("created") else "Verified existing"
                    update_job(f"{action} field {index}/{len(specs)}: {api_name}.", advance=True)
                    update_job(f"Granting access for {api_name}...")
                    permissions = grant_field_permissions(
                        connector,
                        object_name,
                        api_name,
                        permission_sets,
                        profiles,
                        editable=str(spec.get("type") or "").casefold() != "formulacheckbox",
                    )
                    results.append({**field_result, "permissions": permissions})
                    update_job(
                        f"Access completed for {api_name} ({len(permissions)} principal(s)).",
                        advance=True,
                        fields=copy.deepcopy(results),
                    )
                update_job("Refreshing Target Org metadata cache...")
                clear_metadata_cache("target")
                update_job(
                    f"Metadata completed for {len(results)} field(s).",
                    advance=True,
                    status="completed",
                    fields=results,
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )
            except Exception as exc:
                clear_metadata_cache("target")
                update_job(
                    f"Metadata operation failed: {exc}",
                    status="error",
                    error=str(exc),
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )

        threading.Thread(target=worker, name=f"general-utility-{job_id[:8]}", daemon=True).start()
        return jsonify({
            "ok": True,
            "job_id": job_id,
            "status_url": f"/api/general-utility/create-fields/{job_id}",
            "progress_url": f"/general-utility/runs/{job_id}",
        }), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/general-utility/create-fields/<job_id>", methods=["GET"])
def general_utility_field_job_status(job_id: str):
    with _general_utility_jobs_lock:
        job = _general_utility_jobs.get(job_id) or _reconcile_general_utility_job(job_id)
        if not job:
            return jsonify({"error": "Metadata job not found."}), 404
        return jsonify(copy.deepcopy(job))


@app.route("/api/general-utility/access-principals", methods=["GET"])
def list_general_utility_access_principals():
    profile = str(request.args.get("target_profile") or "default").strip() or "default"
    try:
        connector = get_connector("target", profile)
        permission_sets = [
            {"name": str(row.get("Name") or ""), "label": str(row.get("Label") or row.get("Name") or "")}
            for row in connector.query(
                "SELECT Name, Label FROM PermissionSet "
                "WHERE IsOwnedByProfile = false ORDER BY Label"
            )
            if row.get("Name")
        ]
        profiles = [
            {"name": str(row.get("Name") or ""), "label": str(row.get("Name") or "")}
            for row in connector.query("SELECT Name FROM Profile ORDER BY Name")
            if row.get("Name")
        ]
        return jsonify({
            "target_profile": profile,
            "permission_sets": permission_sets,
            "profiles": profiles,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/general-utility/bulk-fls", methods=["POST"])
def start_general_utility_bulk_fls():
    body = request.json or {}
    try:
        object_list_path = _general_utility_object_list_path(body.get("object_list_path"))
        object_names = read_object_list(object_list_path)
        permission_sets = [str(value).strip() for value in body.get("permission_sets") or [] if str(value).strip()]
        profiles = [str(value).strip() for value in body.get("profiles") or [] if str(value).strip()]
        if not permission_sets and not profiles:
            raise ValueError("Select at least one Permission Set or Profile.")
        target_profile = str(body.get("target_profile") or "default").strip() or "default"
        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            "job_type": "bulk_fls",
            "status": "queued",
            "object": f"{len(object_names)} object(s)",
            "object_list_path": str(object_list_path),
            "object_names": object_names,
            "total_steps": max(2, len(object_names) + 2),
            "completed_steps": 0,
            "percent": 0,
            "message": "Waiting to connect to the Target Org...",
            "events": [],
            "objects": [],
            "fields": [],
            "execution_api": "REST ObjectPermissions + FieldPermissions + RecordType access",
            "target_profile": target_profile,
            "permission_sets": permission_sets,
            "profiles": profiles,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        with _general_utility_jobs_lock:
            _general_utility_jobs[job_id] = job
            if len(_general_utility_jobs) > 100:
                finished = [key for key, value in _general_utility_jobs.items() if value.get("status") in {"completed", "error"}]
                for key in finished[: len(_general_utility_jobs) - 100]:
                    _general_utility_jobs.pop(key, None)
            _persist_general_utility_job(job)
        RUN_AUDIT.upsert_submission(job_id, {
            "activity_type": "GENERAL_UTILITY",
            "data_operation": "BULK FLS",
            "migration_name": f"General Utility - Bulk FLS ({len(object_names)} objects)",
            "target_profile": target_profile,
            "status": "QUEUED",
            "request_payload_json": json.dumps({
                "object_list_path": str(object_list_path),
                "objects": object_names,
                "permission_sets": permission_sets,
                "profiles": profiles,
                "execution_api": job["execution_api"],
            }, ensure_ascii=False),
            "status_payload_json": json.dumps(job, ensure_ascii=False),
        })

        def update_job(message: str, *, advance: bool = False, status: str | None = None, event: bool = True, **values) -> None:
            with _general_utility_jobs_lock:
                current = _general_utility_jobs[job_id]
                if advance:
                    current["completed_steps"] = min(current["completed_steps"] + 1, current["total_steps"])
                if status:
                    current["status"] = status
                current["message"] = message
                current["percent"] = round((current["completed_steps"] / current["total_steps"]) * 100)
                current.update(values)
                if event:
                    current["events"].append({"time": datetime.now(timezone.utc).isoformat(), "message": message})
                snapshot = copy.deepcopy(current)
                _persist_general_utility_job(snapshot)
            audit_status = "COMPLETED" if snapshot["status"] == "completed" else "FAILED" if snapshot["status"] == "error" else "RUNNING"
            RUN_AUDIT.update_status(
                job_id,
                status=audit_status,
                status_payload=snapshot,
                error_message=snapshot.get("error") if audit_status == "FAILED" else None,
                completed_at=snapshot.get("finished_at") if audit_status in {"COMPLETED", "FAILED"} else None,
            )

        def worker() -> None:
            try:
                update_job("Connecting to the selected Target Org...", status="running")
                connector = get_connector("target", target_profile)
                update_job("Connected to the Target Org.", advance=True)
                update_job(
                    f"Validating principals and granting access for {len(object_names)} object(s)..."
                )

                def on_progress(message: str) -> None:
                    update_job(message, advance=True)

                result = bulk_assign_fls(
                    connector,
                    object_names,
                    permission_sets,
                    profiles,
                    progress_callback=on_progress,
                )
                clear_metadata_cache("target")
                error_count = int(result.get("error_count") or 0)
                completed_objects = result.get("objects") or []
                update_job(
                    f"Bulk FLS completed for {len(object_names)} object(s)"
                    + (f" with {error_count} object error(s)." if error_count else "."),
                    advance=True,
                    status="completed",
                    objects=completed_objects,
                    fields=_bulk_fls_result_rows(completed_objects),
                    summary=result,
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )
            except Exception as exc:
                clear_metadata_cache("target")
                update_job(
                    f"Bulk FLS operation failed: {exc}",
                    status="error",
                    error=str(exc),
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )

        threading.Thread(target=worker, name=f"general-utility-fls-{job_id[:8]}", daemon=True).start()
        return jsonify({
            "ok": True,
            "job_id": job_id,
            "object_count": len(object_names),
            "objects": object_names,
            "status_url": f"/api/general-utility/bulk-fls/{job_id}",
            "progress_url": f"/general-utility/runs/{job_id}",
        }), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/general-utility/bulk-fls/<job_id>", methods=["GET"])
def general_utility_bulk_fls_job_status(job_id: str):
    with _general_utility_jobs_lock:
        job = _general_utility_jobs.get(job_id) or _reconcile_general_utility_job(job_id)
        if not job:
            return jsonify({"error": "Bulk FLS job not found."}), 404
        return jsonify(copy.deepcopy(job))


@app.route("/api/general-utility/jobs/<job_id>", methods=["GET"])
def general_utility_job_status(job_id: str):
    with _general_utility_jobs_lock:
        job = _general_utility_jobs.get(job_id) or _reconcile_general_utility_job(job_id)
        if not job:
            return jsonify({"error": "General Utility job not found."}), 404
        return jsonify(copy.deepcopy(job))


@app.route("/api/general-utility/runs/<run_id>/download/<filename>")
def download_general_utility_artifact(run_id: str, filename: str):
    root = GENERAL_UTILITY_OUTPUT_DIR.resolve()
    path = (root / run_id / filename).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name=path.name)

@app.route("/api/flat-files/templates", methods=["GET"])
def list_flat_file_templates():
    templates = []
    for item in FlatFileTemplateStore(FLAT_FILE_TEMPLATE_DIR).list():
        domains = _resolve_template_org_domains(
            input_mode="flat_file",
            target_profile=item.get("target_profile") or "default",
            target_domain=item.get("target_domain"),
        )
        templates.append({
            **item,
            "source_domain": "",
            "target_domain": domains["target_domain"],
        })
    return jsonify({"templates": templates})


@app.route("/api/flat-files/objects", methods=["GET"])
def list_flat_file_target_objects():
    """Return only live, loadable objects from the selected target profile."""
    profile = str(request.args.get("profile") or "default").strip()
    try:
        conn = get_connector("target", profile)
        objects = [
            {"name": item["name"], "label": item.get("label") or item["name"]}
            for item in conn.describe_global()
            if item.get("name")
            and item.get("queryable")
            and item.get("createable")
            and not item.get("deprecatedAndHidden")
        ]
        objects.sort(key=lambda item: (item["label"].casefold(), item["name"].casefold()))
        return jsonify({
            "org": "target",
            "profile": profile,
            "instance_url": conn.instance_url,
            "objects": objects,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/flat-files/reference-metadata", methods=["GET"])
def flat_file_reference_metadata():
    profile = str(request.args.get("profile") or "default").strip()
    object_name = str(request.args.get("object") or "").strip()
    if not object_name:
        return jsonify({"error": "Referenced object is required."}), 400
    try:
        describe = get_connector("target", profile).describe_object(object_name)
        external_ids = sorted(
            field.get("name") for field in describe.get("fields", [])
            if field.get("name") and field.get("externalId")
        )
        return jsonify({"object": object_name, "key_prefix": describe.get("keyPrefix") or "", "external_id_fields": external_ids})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/flat-files/templates/<template_id>", methods=["GET"])
def get_flat_file_template(template_id: str):
    try:
        return jsonify(FlatFileTemplateStore(FLAT_FILE_TEMPLATE_DIR).get(template_id))
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404


@app.route("/api/flat-files/templates/<template_id>", methods=["DELETE"])
def delete_flat_file_template(template_id: str):
    try:
        FlatFileTemplateStore(FLAT_FILE_TEMPLATE_DIR).delete(template_id)
        return jsonify({"ok": True, "template_id": template_id})
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except OSError as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/flat-files/drop-files", methods=["GET"])
def list_flat_file_drop_files():
    """List CSV files placed anywhere beneath the local drop folder."""
    FLAT_FILE_DROP_DIR.mkdir(parents=True, exist_ok=True)
    drop_dir = FLAT_FILE_DROP_DIR.resolve()
    files = []
    for candidate in FLAT_FILE_DROP_DIR.rglob("*"):
        resolved = candidate.resolve()
        if (
            candidate.suffix.lower() == ".csv"
            and resolved.is_relative_to(drop_dir)
            and resolved.is_file()
        ):
            relative_name = candidate.relative_to(FLAT_FILE_DROP_DIR).as_posix()
            files.append({"name": relative_name, "size": resolved.stat().st_size})
    files.sort(key=lambda item: item["name"].casefold())
    return jsonify({"folder": str(drop_dir), "files": files})


@app.route("/api/flat-files/import", methods=["POST"])
def import_flat_files():
    data = request.json or {}
    requested_names = data.get("files") or []
    if not isinstance(requested_names, list) or not requested_names:
        return jsonify({"error": "Select at least one CSV file from the drop folder."}), 400
    if len(requested_names) != len(set(map(str, requested_names))):
        return jsonify({"error": "Each drop-folder file may only be selected once."}), 400

    FLAT_FILE_DROP_DIR.mkdir(parents=True, exist_ok=True)
    drop_dir = FLAT_FILE_DROP_DIR.resolve()
    selected_sources = []
    resolved_sources = set()
    for requested_name in requested_names:
        original_name = str(requested_name).strip()
        relative_path = Path(original_name)
        if not original_name or relative_path.is_absolute() or ".." in relative_path.parts:
            return jsonify({"error": f"Invalid drop-folder filename: {original_name}"}), 400
        if Path(original_name).suffix.lower() != ".csv":
            return jsonify({"error": f"Only CSV files are currently supported: {original_name}"}), 400
        source_path = (FLAT_FILE_DROP_DIR / original_name).resolve()
        if not source_path.is_relative_to(drop_dir) or not source_path.is_file():
            return jsonify({"error": f"Drop-folder CSV was not found: {original_name}"}), 400
        if source_path in resolved_sources:
            return jsonify({"error": "Each drop-folder file may only be selected once."}), 400
        resolved_sources.add(source_path)
        selected_sources.append((original_name, source_path))

    batch_id = uuid.uuid4().hex
    batch_dir = (FLAT_FILE_UPLOAD_DIR / batch_id).resolve()
    batch_dir.mkdir(parents=True, exist_ok=False)
    uploaded = []
    try:
        profile = str(data.get("profile") or "default").strip()
        target_conn = get_connector("target", profile)
        target_objects = [
            item.get("name")
            for item in target_conn.describe_global()
            if item.get("name")
        ]
        for index, (original_name, source_path) in enumerate(selected_sources, start=1):
            stored_name = safe_name(Path(original_name).name, f"file_{index}.csv")
            headers = require_named_csv_headers(read_csv_header(source_path), original_name)
            detected_object, detection_source = detect_object_from_path(
                original_name, target_objects
            )
            record_type_source = record_type_source_column(headers)
            record_type_values = (
                # Distinct Record Types are intentionally deferred to the
                # asynchronous prevalidation process for large files.
                []
                if record_type_source
                else []
            )
            uploaded.append({
                "file": original_name,
                "stored_name": stored_name,
                "source_type": "drop_folder",
                "source_path": original_name,
                "columns": headers,
                "size": source_path.stat().st_size,
                "source_size": source_path.stat().st_size,
                "source_mtime_ns": source_path.stat().st_mtime_ns,
                "object": detected_object or "",
                "object_auto_detected": bool(detected_object),
                "object_detection_source": detection_source,
                "record_type_values": record_type_values,
                "record_type_mappings": {},
            })
        return jsonify({"batch_id": batch_id, "files": uploaded})
    except Exception as exc:
        if batch_dir.is_dir() and not any(batch_dir.iterdir()):
            batch_dir.rmdir()
        return jsonify({"error": str(exc)}), 400


@app.route("/api/flat-files/apply-folder-mapping", methods=["POST"])
def apply_folder_mapping():
    """Clone one mapped sample file onto the other CSVs in the same drop folder."""
    data = request.json or {}
    source_file = data.get("file") or {}
    existing_files = data.get("existing_files") or []
    if not isinstance(source_file, dict):
        return jsonify({"error": "A mapped sample file is required."}), 400
    if not isinstance(existing_files, list):
        return jsonify({"error": "existing_files must be a list of file paths."}), 400
    try:
        profile = str(data.get("profile") or "default").strip()
        target_conn = get_connector("target", profile)
        object_names = [
            item.get("name")
            for item in target_conn.describe_global()
            if item.get("name")
        ]
        result = build_folder_mapping_files(
            FLAT_FILE_DROP_DIR,
            source_file,
            existing_files,
            object_names,
        )
        return jsonify(result)
    except FlatFileValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/flat-files/auto-map", methods=["POST"])
def auto_map_flat_file():
    data = request.json or {}
    object_name = str(data.get("object") or "").strip()
    columns = data.get("columns") or []
    profile = str(data.get("profile") or "default").strip()
    if not object_name or not isinstance(columns, list):
        return jsonify({"error": "Target object and columns are required."}), 400
    try:
        conn = get_connector("target", profile)
        # Refresh at the mapping boundary so newly deployed fields and FLS
        # changes are visible without restarting the UI process.
        conn._object_describe_cache.pop(object_name, None)
        describe = conn.describe_object(object_name)
        fields = [
            {
                "name": field.get("name"),
                "label": field.get("label") or field.get("name"),
                "type": field.get("type"),
                "required": not field.get("nillable", True) and not field.get("defaultedOnCreate", False),
                "external_id": bool(field.get("externalId")),
                "references_to": field.get("referenceTo") or [],
            }
            for field in describe.get("fields", [])
            if field.get("name") and (
                field.get("createable")
                or field.get("updateable")
                or field.get("name") in MIGRATABLE_AUDIT_FIELDS
            )
        ]
        # Source Salesforce Id is not writable in the target, but flat-file
        # migrations must retain it in raw staging so the planner can copy it
        # into the configured target external-ID field.
        if not any(field["name"] == "Id" for field in fields):
            fields.insert(0, {
                "name": "Id",
                "label": "Source Salesforce ID",
                "type": "migration_key",
                "required": True,
                "external_id": False,
                "references_to": [],
            })
        fields.sort(key=lambda field: field["name"].lower())
        mappings = auto_map_columns(columns, fields)
        all_fields_by_name = {
            str(field.get("name") or "").casefold(): field
            for field in describe.get("fields", [])
            if field.get("name")
        }
        all_fields = [
            {
                "name": field.get("name"),
                "label": field.get("label") or field.get("name"),
                "type": field.get("type"),
                "writable": bool(field.get("createable") or field.get("updateable")),
            }
            for field in describe.get("fields", [])
            if field.get("name")
        ]
        all_fields.sort(key=lambda field: str(field["name"]).lower())
        for mapping in mappings:
            source_key = str(mapping.get("source") or "").strip().casefold()
            corresponding = all_fields_by_name.get(source_key)
            mapping["corresponding_exists"] = bool(corresponding)
            mapping["corresponding_type"] = (
                corresponding.get("type") if corresponding else None
            )
            mapping["corresponding_writable"] = bool(
                corresponding
                and (corresponding.get("createable") or corresponding.get("updateable"))
            )
            if mapping.get("target"):
                continue
            if corresponding and not (
                corresponding.get("createable") or corresponding.get("updateable")
            ):
                if (
                    is_system_managed_field(mapping.get("source"))
                    or is_salesforce_derived_field(corresponding)
                ) and not is_migratable_audit_field(mapping.get("source")):
                    mapping["ignored"] = True
                    mapping["status"] = "derived_field" if is_salesforce_derived_field(corresponding) else "system_managed"
                    mapping["unmapped_reason"] = (
                        "Salesforce-calculated/generated field; excluded from migration."
                        if is_salesforce_derived_field(corresponding)
                        else "Salesforce-managed audit/system field; excluded from migration."
                    )
                else:
                    mapping["status"] = "read_only"
                    mapping["unmapped_reason"] = (
                        "Exact API name exists, but the target field is read-only."
                    )
            elif not corresponding:
                mapping["unmapped_reason"] = (
                    "No field with this API name exists on the target object."
                )
        access_issues = validate_migration_field_access(
            object_name, describe.get("fields", []), mappings
        )
        external_id_fields = [
            field.get("name")
            for field in describe.get("fields", [])
            if field.get("externalId") and (field.get("createable") or field.get("updateable"))
        ]
        recommended_external_id = (
            "External_Id__c"
            if "External_Id__c" in external_id_fields
            else external_id_fields[0] if external_id_fields else ""
        )
        incoming_names = {str(column).strip().casefold() for column in columns}
        lookup_fields = build_target_lookup_fields(conn, describe.get("fields", []), incoming_names)
        for mapping in mappings:
            target_name = str(mapping.get("target") or "").strip()
            apply_default_owner_id_mapping(
                mapping,
                all_fields_by_name.get(target_name.casefold()),
                lookup_fields.get(target_name),
            )
        record_types = []
        if record_type_source_column(columns):
            escaped_object = object_name.replace("\\", "\\\\").replace("'", "\\'")
            record_types = [
                {
                    "id": row.get("Id"),
                    "developer_name": row.get("DeveloperName"),
                    "name": row.get("Name") or row.get("DeveloperName"),
                }
                for row in conn.query(
                    "SELECT Id, DeveloperName, Name FROM RecordType "
                    f"WHERE SobjectType = '{escaped_object}' AND IsActive = true "
                    "ORDER BY Name"
                )
                if row.get("Id") and row.get("DeveloperName")
            ]
        return jsonify({
            "fields": fields,
            "all_fields": all_fields,
            "mappings": mappings,
            "external_id_fields": external_id_fields,
            "recommended_external_id": recommended_external_id,
            "lookup_fields": lookup_fields,
            "record_types": record_types,
            "access_issues": access_issues,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/flat-files/clean", methods=["POST"])
def clean_flat_files():
    """Create immutable clean/rejected derivatives from an uploaded package."""
    data = request.json or {}
    batch_id = str(data.get("batch_id") or "").strip()
    files = data.get("files") or []
    target_profile = str(data.get("target_profile") or "default").strip()
    batch_dir = (FLAT_FILE_UPLOAD_DIR / batch_id).resolve()
    if (
        not batch_id
        or batch_dir.parent != FLAT_FILE_UPLOAD_DIR.resolve()
        or not batch_dir.is_dir()
        or not isinstance(files, list)
        or not files
    ):
        return jsonify({"error": "A valid uploaded file package is required."}), 400
    try:
        conn = get_connector("target", target_profile)
        results = []
        for file_config in files:
            if _truthy(file_config.get("skip_staging")):
                results.append({
                    "file": file_config.get("file") or file_config.get("stored_name"),
                    "object": file_config.get("object") or "",
                    "skipped": True,
                    "skip_reason": "File is explicitly excluded from staging.",
                    "cleaned_name": None,
                    "rejected_name": None,
                    "clean_count": 0,
                    "rejected_count": 0,
                    "normalized_count": 0,
                    "examples": [],
                    "field_counts": {},
                })
                continue
            object_name = str(file_config.get("object") or "").strip()
            stored_name = safe_name(str(file_config.get("stored_name") or ""))
            source_path = resolve_flat_file_path(
                file_config, batch_dir, FLAT_FILE_DROP_DIR, prefer_cleaned=False
            )
            if not object_name or not source_path.is_file():
                raise FlatFileValidationError(
                    f"{file_config.get('file') or stored_name}: select a target object first."
                )
            # Validate/Clean is the final pre-staging gate and must use current
            # target metadata rather than an earlier cached Describe response.
            conn._object_describe_cache.pop(object_name, None)
            object_describe = conn.describe_object(object_name)
            described_fields = object_describe.get("fields", [])
            _apply_flat_file_semantic_metadata(conn, object_name, described_fields)
            preflight_errors = validate_strict_api_names(file_config)
            preflight_errors.extend(
                validate_object_and_record_type_access(
                    object_name, object_describe, file_config
                )
            )
            if preflight_errors:
                raise FlatFileValidationError(" ".join(preflight_errors))
            external_id_errors = validate_flat_file_external_id(
                file_config,
                described_fields,
            )
            if external_id_errors:
                raise FlatFileValidationError(" ".join(external_id_errors))
            access_issues = validate_migration_field_access(
                object_name, described_fields, file_config.get("mappings") or []
            )
            if access_issues:
                raise FlatFileValidationError(
                    "Migration-user field access validation failed: "
                    + " ".join(issue["message"] for issue in access_issues)
                )
            target_record_type_ids = {
                str(target_id).strip()
                for target_id in (file_config.get("record_type_mappings") or {}).values()
                if str(target_id).strip()
            }
            default_record_type_id = next(
                (
                    str(info.get("recordTypeId") or "").strip()
                    for info in (object_describe.get("recordTypeInfos") or [])
                    if info.get("defaultRecordTypeMapping")
                    and info.get("available", True)
                    and str(info.get("recordTypeId") or "").strip()
                ),
                "",
            )
            if default_record_type_id:
                target_record_type_ids.add(default_record_type_id)
            record_type_picklists = {
                record_type_id: conn.describe_record_type_picklists(
                    object_name, record_type_id
                )
                for record_type_id in sorted(target_record_type_ids)
            }
            global_prefixes = {
                str(item.get("name") or ""): str(item.get("keyPrefix") or "")
                for item in conn.describe_global()
                if item.get("name") and item.get("keyPrefix")
            }
            reference_key_prefixes = {
                str(field.get("name") or ""): {
                    global_prefixes[reference]
                    for reference in field.get("referenceTo") or []
                    if reference in global_prefixes
                }
                for field in described_fields
                if field.get("name") and field.get("referenceTo")
            }
            stem = Path(stored_name).stem
            clean_name = safe_name(f"{stem}_clean.csv")
            rejected_name = safe_name(f"{stem}_rejected.csv")
            report = clean_csv_against_schema(
                source_path,
                batch_dir / clean_name,
                batch_dir / rejected_name,
                file_config,
                described_fields,
                record_type_picklists,
                default_record_type_id,
                reference_key_prefixes,
            )
            results.append({
                "file": file_config.get("file") or stored_name,
                "object": object_name,
                "cleaned_name": clean_name,
                "rejected_name": rejected_name,
                "clean_download": (
                    f"/api/flat-files/batches/{batch_id}/download/{clean_name}"
                ),
                "rejected_download": (
                    f"/api/flat-files/batches/{batch_id}/download/{rejected_name}"
                ),
                **report,
            })
        return jsonify({
            "ok": True,
            "batch_id": batch_id,
            "clean_count": sum(item["clean_count"] for item in results),
            "rejected_count": sum(item["rejected_count"] for item in results),
            "normalized_count": sum(item["normalized_count"] for item in results),
            "files": results,
        })
    except (FlatFileValidationError, ValueError, FileNotFoundError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.exception("Flat-file cleaning failed")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/flat-files/batches/<batch_id>/download/<filename>")
def download_flat_file_derivative(batch_id: str, filename: str):
    safe_batch = safe_name(batch_id)
    safe_filename = safe_name(filename)
    if safe_batch != batch_id or safe_filename != filename:
        abort(404)
    batch_dir = (FLAT_FILE_UPLOAD_DIR / batch_id).resolve()
    candidate = (batch_dir / filename).resolve()
    if candidate.parent != batch_dir or not candidate.is_file():
        abort(404)
    return send_file(candidate, as_attachment=True, download_name=candidate.name)


@app.route("/api/flat-files/templates", methods=["POST"])
def save_flat_file_template():
    data = request.json or {}
    draft = _truthy(data.get("draft"))
    name = str(data.get("name") or "").strip()
    batch_id = str(data.get("batch_id") or "").strip()
    files = data.get("files") or []
    allow_unmapped = _truthy(data.get("allow_unmapped"))
    if isinstance(files, list):
        fill_missing_sibling_mappings(files)
    if not name or not batch_id or not isinstance(files, list) or not files:
        return jsonify({"error": "Template name, upload batch, and mapped files are required."}), 400
    if not any(not _truthy(file_config.get("skip_staging")) for file_config in files):
        return jsonify({"error": "At least one file must remain enabled for staging."}), 400
    batch_dir = (FLAT_FILE_UPLOAD_DIR / batch_id).resolve()
    if batch_dir.parent != FLAT_FILE_UPLOAD_DIR.resolve() or not batch_dir.is_dir():
        return jsonify({"error": "The upload batch was not found."}), 400
    errors = []
    data_validation_files = []
    target_profile = str(data.get("target_profile") or "default")
    target_conn = None
    included_objects = {
        str(file_config.get("object") or "").strip()
        for file_config in files
        if str(file_config.get("object") or "").strip()
        and not _truthy(file_config.get("skip_staging"))
    }
    ignored_unmapped_count = sum(
        apply_unmapped_column_policy(file_config, allow_unmapped)
        for file_config in files
        if not _truthy(file_config.get("skip_staging"))
    )
    for file_config in files:
        if _truthy(file_config.get("skip_staging")):
            continue
        target_fields = []
        described_fields = []
        object_name = str(file_config.get("object") or "").strip()
        if object_name:
            try:
                target_conn = target_conn or get_connector("target", target_profile)
                # Reconfirm metadata when saving the template as fields may
                # have been deployed since the file was initially mapped.
                target_conn._object_describe_cache.pop(object_name, None)
                described_fields = target_conn.describe_object(object_name).get("fields", [])
                _apply_flat_file_semantic_metadata(
                    target_conn,
                    object_name,
                    described_fields,
                )
                target_fields = [
                    field
                    for field in described_fields
                    if field.get("name") and (
                        field.get("name") == "Id"
                        or field.get("createable")
                        or field.get("updateable")
                    )
                ]
                # Allow explicit relationship external-ID targets entered in
                # the editable mapping control, e.g. Account.External_Id__c.
                relationship_map = {
                    field.get("relationshipName"): field
                    for field in described_fields
                    if field.get("relationshipName")
                }
                manual_relationship_targets = {
                    str(mapping.get("target") or "").strip()
                    for mapping in file_config.get("mappings") or []
                    if "." in str(mapping.get("target") or "")
                }
                for relationship_target in sorted(manual_relationship_targets):
                    relationship_name, ref_field_name = relationship_target.split(".", 1)
                    relationship_field = relationship_map.get(relationship_name)
                    ref_objects = [
                        ref
                        for ref in ((relationship_field or {}).get("referenceTo") or [])
                        if ref
                    ]
                    if len(ref_objects) != 1:
                        continue
                    referenced_fields = target_conn.describe_object(ref_objects[0]).get("fields", [])
                    referenced_field = next(
                        (
                            field
                            for field in referenced_fields
                            if field.get("name") == ref_field_name
                            and field.get("externalId")
                        ),
                        None,
                    )
                    if referenced_field:
                        target_fields.append({
                            "name": relationship_target,
                            "label": f"{relationship_name} by {ref_field_name}",
                            "type": "relationship_external_id",
                        })
                external_id_candidates = [
                    field.get("name")
                    for field in described_fields
                    if field.get("externalId") and (field.get("createable") or field.get("updateable"))
                ]
                if not file_config.get("external_id"):
                    file_config["external_id"] = (
                        "External_Id__c"
                        if "External_Id__c" in external_id_candidates
                        else (external_id_candidates[0] if external_id_candidates else "")
                    )
                for mapping in file_config.get("mappings") or []:
                    target_field = str(mapping.get("target") or "").strip()
                    field_describe = next(
                        (field for field in described_fields if field.get("name") == target_field),
                        None,
                    )
                    enrich_flat_file_mapping_lookup(mapping, field_describe, target_conn)
                    referenced_object = str(mapping.get("referenced_object") or "").strip()
                    lookup_external_id = str(mapping.get("lookup_external_id") or "").strip()
                    lookup_resolution = str(mapping.get("lookup_resolution") or "external_id").strip()
                    # Some reference fields (notably EmailMessage.ActivityId)
                    # expose referenceTo metadata but no relationshipName. They
                    # cannot use Relationship.ExternalId syntax, so resolve the
                    # external key to a concrete target ID before insert.
                    if (
                        lookup_resolution == "external_id"
                        and referenced_object
                        and lookup_external_id
                        and field_describe
                        and not str(field_describe.get("relationshipName") or "").strip()
                        and len([ref for ref in (field_describe.get("referenceTo") or []) if ref]) == 1
                    ):
                        mapping["lookup_resolution"] = "target_external_id"
                        lookup_resolution = "target_external_id"
                    if lookup_resolution in {"polymorphic_external_id", "polymorphic_target_first"}:
                        rules = mapping.get("polymorphic_mappings") or []
                        if not isinstance(rules, list) or not rules:
                            errors.append(f"{object_name}.{target_field}: add at least one polymorphic object mapping.")
                            continue
                        seen_poly_keys = set()
                        source_columns = {str(value) for value in (file_config.get("columns") or [])}
                        for rule in rules:
                            method = str(rule.get("identify_by") or "").strip()
                            match_value = str(rule.get("match_value") or "").strip()
                            ref_object = str(rule.get("referenced_object") or "").strip()
                            ext_field = str(rule.get("external_id") or "").strip()
                            companion = str(rule.get("companion_column") or "").strip()
                            rule_key = (method, companion, match_value.casefold())
                            if method not in {"prefix", "companion", "fixed"} or not ref_object or not ext_field:
                                errors.append(f"{object_name}.{target_field}: each polymorphic rule needs an identification method, object, and external ID.")
                                continue
                            if method == "prefix" and len(match_value) != 3:
                                errors.append(f"{object_name}.{target_field}: prefix '{match_value}' must contain exactly 3 characters.")
                            if method == "companion" and (not companion or companion not in source_columns or not match_value):
                                errors.append(f"{object_name}.{target_field}: companion rules need an existing companion column and type value.")
                            if rule_key in seen_poly_keys:
                                errors.append(f"{object_name}.{target_field}: duplicate polymorphic rule for {rule_key}.")
                            seen_poly_keys.add(rule_key)
                            allowed_objects = set((field_describe or {}).get("referenceTo") or [])
                            if allowed_objects and ref_object not in allowed_objects:
                                errors.append(f"{object_name}.{target_field}: {ref_object} is not an allowed referenced object.")
                            ref_describe = target_conn.describe_object(ref_object)
                            ref_fields = ref_describe.get("fields", [])
                            if method == "prefix" and ref_describe.get("keyPrefix") and match_value != ref_describe.get("keyPrefix"):
                                errors.append(f"{object_name}.{target_field}: prefix {match_value} does not match {ref_object} ({ref_describe.get('keyPrefix')}).")
                            if not any(field.get("name") == ext_field and field.get("externalId") for field in ref_fields):
                                errors.append(f"{object_name}.{target_field}: {ref_object}.{ext_field} is not a target External ID.")
                            preserve = str(rule.get("preserve_original_as") or "").strip()
                            if preserve and not any(field.get("name") == preserve and (field.get("createable") or field.get("updateable")) for field in described_fields):
                                errors.append(f"{object_name}.{target_field}: preservation field {preserve} is not writable on {object_name}.")
                            fallback = str(rule.get("fallback_id") or "").strip()
                            if fallback:
                                escaped_fallback = fallback.replace("\\", "\\\\").replace("'", "\\'")
                                if not target_conn.query(f"SELECT Id FROM {ref_object} WHERE Id = '{escaped_fallback}' LIMIT 1"):
                                    errors.append(f"{object_name}.{target_field}: fallback {fallback} does not exist on target {ref_object}.")
                        continue
                    if not referenced_object:
                        continue
                    if lookup_resolution == "target_id":
                        continue
                    if referenced_object not in included_objects and not lookup_external_id:
                        errors.append(
                            f"{object_name}.{mapping.get('target')}: referenced object "
                            f"'{referenced_object}' is not in the migration; choose its external ID."
                        )
                        continue
                    if lookup_external_id:
                        referenced_fields = target_conn.describe_object(referenced_object).get("fields", [])
                        valid_lookup_external_id = any(
                            field.get("name") == lookup_external_id and field.get("externalId")
                            for field in referenced_fields
                        )
                        if not valid_lookup_external_id:
                            errors.append(
                                f"{object_name}.{mapping.get('target')}: "
                                f"{referenced_object}.{lookup_external_id} is not a target External ID."
                            )
            except Exception as exc:
                errors.append(f"{object_name}: target metadata validation failed: {exc}")
        errors.extend(validate_file_config(file_config, target_fields))
        stored_name = safe_name(str(file_config.get("stored_name") or ""))
        cleaned_name = safe_name(str(file_config.get("cleaned_name") or ""))
        if not draft and not file_config.get("cleaned_name"):
            errors.append(
                f"{file_config.get('file') or stored_name}: run Prepare Clean Files before saving."
            )
        validation_name = cleaned_name if file_config.get("cleaned_name") else stored_name
        try:
            path = resolve_flat_file_path(
                file_config, batch_dir, FLAT_FILE_DROP_DIR,
                prefer_cleaned=bool(file_config.get("cleaned_name")),
            )
        except (FlatFileValidationError, OSError, ValueError) as exc:
            errors.append(f"{file_config.get('file') or validation_name}: {exc}")
            path = None
        if path is None or not path.is_file():
            if path is not None:
                errors.append(f"Flat file was not found: {validation_name}")
        elif described_fields and not draft:
            report = validate_csv_against_schema(path, file_config, described_fields)
            if report["issue_count"]:
                data_validation_files.append({
                    "file": file_config.get("file") or stored_name,
                    "object": object_name,
                    **report,
                })
    object_external_ids = {
        str(file_config.get("object") or "").strip(): str(file_config.get("external_id") or "").strip()
        for file_config in files
        if not _truthy(file_config.get("skip_staging"))
        and str(file_config.get("object") or "").strip()
    }
    for file_config in files:
        if _truthy(file_config.get("skip_staging")):
            continue
        object_name = str(file_config.get("object") or "").strip()
        for mapping in file_config.get("mappings") or []:
            referenced_object = str(mapping.get("referenced_object") or "").strip()
            configured_key = str(mapping.get("lookup_external_id") or "").strip()
            parent_key = object_external_ids.get(referenced_object, "")
            if referenced_object in included_objects and configured_key and parent_key and configured_key != parent_key:
                errors.append(
                    f"{object_name}.{mapping.get('target')}: uses {referenced_object}.{configured_key}, "
                    f"but the in-pipeline parent identity is {referenced_object}.{parent_key}."
                )
        source_path = resolve_flat_file_path(
            file_config, batch_dir, FLAT_FILE_DROP_DIR, prefer_cleaned=False,
        )
        try:
            validate_flat_file_reference(source_path, file_config)
        except FlatFileValidationError as exc:
            errors.append(str(exc))

    if errors:
        affected_files = sorted({
            error.split(":", 1)[0].strip()
            for error in errors
            if ":" in error
        })
        return jsonify({
            "error": (
                f"Validation found {len(errors)} issue(s) across "
                f"{len(affected_files) or 1} file/object mapping(s). "
                "Use each object's 'Unmapped only' view to resolve or ignore them."
            ),
            "error_count": len(errors),
            "affected_files": affected_files,
            "errors": errors,
        }), 400

    # Package-local lookups inherit identity from their referenced object's
    # file configuration. Persisting Parent.ExternalId on every child would
    # create a second source of truth; keep explicit IDs only for objects that
    # are outside this migration package.
    for file_config in files:
        for mapping in file_config.get("mappings") or []:
            referenced_object = str(mapping.get("referenced_object") or "").strip()
            if referenced_object not in included_objects:
                continue
            if str(mapping.get("lookup_resolution") or "").strip() == "target_id":
                continue
            mapping["lookup_resolution"] = "pipeline_object"
            mapping.pop("lookup_external_id", None)
    record = FlatFileTemplateStore(FLAT_FILE_TEMPLATE_DIR).save(name, {
        "batch_id": batch_id,
        "target_profile": target_profile,
        "target_domain": _profile_login_domain("TARGET", target_profile),
        "allow_unmapped": allow_unmapped,
        "ignored_unmapped_count": ignored_unmapped_count,
        "files": files,
        "data_validation": {
            "issue_count": sum(item["issue_count"] for item in data_validation_files),
            "files": data_validation_files,
        },
        "prevalidation_status": "DRAFT" if draft else "PREPARED",
    })
    data_issue_count = sum(item["issue_count"] for item in data_validation_files)
    return jsonify({
        "ok": True,
        "template": record,
        "ignored_unmapped_count": ignored_unmapped_count,
        "data_validation": {
            "issue_count": data_issue_count,
            "files": data_validation_files,
        },
    })


@app.route("/api/flat-files/templates/<template_id>/ingest", methods=["POST"])
def ingest_flat_file_template(template_id: str):
    try:
        template = FlatFileTemplateStore(FLAT_FILE_TEMPLATE_DIR).get(template_id)
        batch_dir = FLAT_FILE_UPLOAD_DIR / str(template.get("batch_id") or "")
        stager = Stager()
        try:
            result = FlatFileInput(stager, batch_dir, FLAT_FILE_DROP_DIR).ingest(template, replace=True)
        finally:
            stager.close()
        return jsonify({
            "ok": True,
            "template_id": template_id,
            "object_counts": result.object_counts,
            "file_counts": result.file_counts,
            "next_stage": "transform",
        })
    except (FileNotFoundError, FlatFileValidationError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.exception("Flat-file ingestion failed")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/run-status", methods=["GET"])
def get_run_status():
    try:
        run_key = (request.args.get("run_key") or "").strip() or None
        runner = _runner_status(run_key)
        if not runner.get("running") and _state_has_running_pipeline():
            _mark_external_run_stopped()
        payload = _current_status_payload((runner.get("run_key") or run_key or "").strip() or None)
        effective_run_key = (runner.get("run_key") or run_key or "").strip()
        artifact_dir = _resolve_sf_run_dir(PROJECT_ROOT / "exports", effective_run_key) if effective_run_key else None
        return jsonify({
            **payload,
            "runner": runner,
            "output_dir": str(artifact_dir) if artifact_dir else str(PROJECT_ROOT / "exports" / (_run_dir_name_for_key(effective_run_key) or f"run_{effective_run_key}")),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/pipeline/run", methods=["POST"])
def start_pipeline_run():
    try:
        payload, status_code = _start_pipeline_run_request(request.get_json(silent=True) or {}, origin="ui")
        return jsonify(payload), status_code
    except Exception as exc:
        logger.exception("Unhandled error while starting UI pipeline run")
        return jsonify({"error": f"Failed to start pipeline: {exc}"}), 500


_SCHEDULER_INTERVAL_SEC = 15
_scheduler_thread: threading.Thread | None = None
_scheduler_stop = threading.Event()
_SCHEDULE_FAILED_STATUSES = {
    "FAILED",
    "STOPPED",
    "DATA_LOAD_FAILED",
    "PARTIAL_DATA_LOAD_FAILED",
}


def _any_run_state_running() -> bool:
    if _state_has_running_pipeline():
        return True
    try:
        for path in RUN_STATE_DIR.glob("run_sf_*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            for pipeline in (payload.get("pipelines") or {}).values():
                if str((pipeline or {}).get("status") or "").upper() == "RUNNING":
                    return True
    except Exception:
        pass
    return False


def _migration_is_busy() -> bool:
    if _runner_status().get("running"):
        return True
    if _active_runs:
        return True
    if _active_audited_validation():
        return True
    if _find_external_cli_pid() is not None:
        return True
    if _any_run_state_running():
        return True
    return False


def _scheduled_run_outcome(run_key: str, launched_at: str | None = None) -> str | None:
    """Return running, completed, failed, or None when the result is still unknown."""
    normalized = str(run_key or "").strip()
    if not normalized:
        return None
    runner = _runner_status(normalized)
    if runner.get("running"):
        return "running"
    audit = RUN_AUDIT.get_run(normalized) or {}
    status = str(audit.get("status") or "").upper()
    if status in _SCHEDULE_FAILED_STATUSES:
        return "failed"
    if status == "COMPLETED":
        return "completed"
    returncode = runner.get("returncode")
    if returncode not in (None, 0):
        return "failed"
    if returncode == 0:
        return "completed"
    if launched_at and not _migration_is_busy():
        try:
            started = datetime.fromisoformat(str(launched_at).replace("Z", "+00:00"))
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - started.astimezone(timezone.utc)).total_seconds()
            if age > 180:
                return "failed"
        except (TypeError, ValueError):
            pass
    return None


def _scheduler_log(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info("pipeline-scheduler: %s", message)
    log_path = PROJECT_ROOT / "logs" / "pipeline_scheduler.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


def _prevent_sleep_while_scheduled(pending: bool) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        es_continuous = 0x80000000
        es_system_required = 0x00000001
        es_awaymode = 0x00000040
        flags = es_continuous
        if pending:
            flags |= es_system_required | es_awaymode
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass


def _fire_scheduled_job(job: dict) -> None:
    job_id = str(job.get("id") or "")
    now_iso = datetime.now(timezone.utc).isoformat()
    update_job(
        PROJECT_ROOT,
        job_id,
        {
            "status": "running",
            "launching": True,
            "started_at": job.get("started_at") or now_iso,
            "error": None,
        },
    )
    suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        payload = build_fire_payload(job, unique_suffix=suffix)
        result, status_code = _start_pipeline_run_request(payload, origin="ui-schedule")
        error_text = str((result or {}).get("error") or "")
        if status_code >= 400 and "already used" in error_text.lower():
            payload = build_fire_payload(job, unique_suffix=f"{suffix}_{uuid.uuid4().hex[:6]}")
            result, status_code = _start_pipeline_run_request(payload, origin="ui-schedule")
    except Exception as exc:
        update_job(
            PROJECT_ROOT,
            job_id,
            {
                "status": "failed",
                "launching": False,
                "active_run_key": "",
                "completed_at": now_iso,
                "error": str(exc),
            },
        )
        _scheduler_log(f"{job_id} failed to start: {exc}")
        return
    if status_code >= 400 or not (result or {}).get("ok"):
        error = str((result or {}).get("error") or f"HTTP {status_code}")
        update_job(
            PROJECT_ROOT,
            job_id,
            {
                "status": "failed",
                "launching": False,
                "active_run_key": "",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "error": error,
            },
        )
        _scheduler_log(f"{job_id} rejected: {error}")
        return
    run_key = str((result or {}).get("run_key") or "").strip()
    run_keys = list(job.get("run_keys") or [])
    if run_key:
        run_keys.append(run_key)
    update_job(
        PROJECT_ROOT,
        job_id,
        {
            "status": "running",
            "launching": False,
            "active_run_key": run_key,
            "run_keys": run_keys,
            "launched_at": datetime.now(timezone.utc).isoformat(),
            "error": None,
        },
    )
    _scheduler_log(f"{job_id} started run {run_key}")


def _pipeline_scheduler_tick() -> None:
    jobs = list_jobs(PROJECT_ROOT)
    pending = any(str(job.get("status") or "") in {"queued", "running"} for job in jobs)
    _prevent_sleep_while_scheduled(pending)
    if not pending:
        return
    busy = _migration_is_busy()
    for job in jobs:
        job_id = str(job.get("id") or "")
        status = str(job.get("status") or "")
        if status not in {"queued", "running"}:
            continue
        active_key = str(job.get("active_run_key") or "").strip()
        if active_key:
            outcome = _scheduled_run_outcome(active_key, job.get("launched_at"))
            if outcome in {None, "running"}:
                continue
            if outcome == "failed":
                update_job(
                    PROJECT_ROOT,
                    job_id,
                    {
                        "status": "failed",
                        "active_run_key": "",
                        "launching": False,
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "error": f"Run {active_key} did not complete successfully.",
                    },
                )
                _scheduler_log(f"{job_id} failed after {active_key}")
                continue
            if job_has_more_stages(job):
                update_job(
                    PROJECT_ROOT,
                    job_id,
                    {
                        "stage_index": int(job.get("stage_index") or 0) + 1,
                        "active_run_key": "",
                        "launching": False,
                        "launched_at": None,
                    },
                )
                _scheduler_log(f"{job_id} finished {active_key}; next stage queued")
            else:
                update_job(
                    PROJECT_ROOT,
                    job_id,
                    {
                        "status": "completed",
                        "active_run_key": "",
                        "launching": False,
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "error": None,
                    },
                )
                _scheduler_log(f"{job_id} completed after {active_key}")
            continue
        current = get_job(PROJECT_ROOT, job_id) or job
        if str(current.get("status") or "") not in {"queued", "running"}:
            continue
        if not job_ready_to_start(current, busy=busy):
            continue
        _fire_scheduled_job(current)
        busy = True
        break


def _pipeline_scheduler_loop() -> None:
    _scheduler_log("Scheduler started")
    while True:
        try:
            _pipeline_scheduler_tick()
        except Exception:
            logger.exception("pipeline scheduler tick failed")
            _scheduler_log("Tick failed; see application log")
        if _scheduler_stop.wait(_SCHEDULER_INTERVAL_SEC):
            break


def _start_pipeline_scheduler() -> None:
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return
    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(
        target=_pipeline_scheduler_loop,
        name="pipeline-scheduler",
        daemon=True,
    )
    _scheduler_thread.start()


@app.route("/api/pipeline/schedule", methods=["GET"])
def list_pipeline_schedule():
    jobs = [public_job(job) for job in list_jobs(PROJECT_ROOT)]
    return jsonify({
        "jobs": jobs,
        "busy": _migration_is_busy(),
        "runner": _runner_status(),
    })


@app.route("/api/pipeline/schedule", methods=["POST"])
def create_pipeline_schedule():
    data = request.json or {}
    run_payload = data.get("payload") if isinstance(data.get("payload"), dict) else data
    snapshot_error = _saved_pipeline_snapshot_error(run_payload)
    if snapshot_error:
        return jsonify({"error": snapshot_error}), 409
    try:
        pipeline_entries = _build_pipeline_entries_from_request(run_payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    gate_errors = _pipeline_prevalidation_gate_errors(pipeline_entries)
    if gate_errors:
        return jsonify({
            "error": "Migration blocked by pre-validation gate.",
            "prevalidation_errors": gate_errors,
        }), 400
    try:
        job = create_job(PROJECT_ROOT, {
            "after_current": data.get("after_current"),
            "run_at": data.get("run_at"),
            "stage_mode": data.get("stage_mode"),
            "start_from_index": data.get("start_from_index"),
            "label": data.get("label") or run_payload.get("migration_name"),
            "snapshot_id": data.get("snapshot_id") or run_payload.get("snapshot_id"),
            "payload": run_payload,
        })
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    _scheduler_log(f"Queued {job.get('id')} ({job.get('label')})")
    return jsonify({"ok": True, "job": public_job(job)})


@app.route("/api/pipeline/schedule/<job_id>/cancel", methods=["POST"])
def cancel_pipeline_schedule(job_id: str):
    job = cancel_job(PROJECT_ROOT, job_id)
    if job is None:
        return jsonify({"error": "Scheduled job not found"}), 404
    _scheduler_log(f"Cancelled {job_id}")
    return jsonify({"ok": True, "job": public_job(job)})


@app.route("/api/pipeline/used-names", methods=["GET"])
def list_used_pipeline_names():
    """Return base pipeline names already used by migration runs (for uniqueness checks)."""
    display: dict[str, str] = {}
    for row in RUN_AUDIT.list_runs(500):
        if str(row.get("activity_type") or "MIGRATION").upper() != "MIGRATION":
            continue
        base = _base_pipeline_activity_name(row.get("migration_name"))
        if base and base.casefold() not in display:
            display[base.casefold()] = base
    return jsonify({"names": sorted(display.values(), key=lambda value: value.casefold())})


@app.route("/api/pipeline/validate", methods=["POST"])
def start_pipeline_validation_run():
    current_runner = _runner_status()
    if current_runner.get("running"):
        return jsonify({"error": "A pipeline or validation command is already running", "runner": current_runner}), 409
    persisted_validation = _active_audited_validation()
    if persisted_validation:
        return jsonify({"error": f"Validation run '{persisted_validation['run_key']}' is already running", **persisted_validation}), 409

    data = request.json or {}
    try:
        pipeline_name = _assert_required_unique_pipeline_name(data.get("pipeline_name") or data.get("pipeline_id"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    pipeline_id = _slugify_template_name(data.get("pipeline_id") or pipeline_name)
    raw_stages = data.get("stages") or []
    if not isinstance(raw_stages, list) or not raw_stages:
        return jsonify({"error": "At least one pipeline stage is required"}), 400

    pipeline_optional_check_ids = {
        "duplicate_keys", "required_fields", "restricted_picklists", "dependent_picklists",
        "master_detail_references", "lookup_references", "text_limits", "numeric_precision",
        "salesforce_id_format", "contact_formats", "date_formats",
    }
    optional_checks = data.get("optional_checks")
    if not isinstance(optional_checks, list):
        optional_checks = ["restricted_picklists", "dependent_picklists", "master_detail_references"]
    optional_checks = sorted({
        str(value).strip() for value in optional_checks
        if str(value).strip() in pipeline_optional_check_ids
    })
    salesforce_id_length = str(data.get("salesforce_id_length") or "15_or_18")
    if salesforce_id_length not in {"15_or_18", "15", "18"}:
        return jsonify({"error": "Salesforce ID length must be 15, 18, or 15-or-18 characters"}), 400
    scan_mode = str(data.get("scan_mode") or "full").strip().lower()
    query_mode = str(data.get("query_mode") or "auto").strip().lower()
    try:
        sample_size = int(data.get("sample_size") or 1000)
        sample_seed = int(data.get("sample_seed") or 42)
    except (TypeError, ValueError):
        return jsonify({"error": "Sample size and seed must be whole numbers"}), 400
    if scan_mode not in {"sample", "full"} or query_mode not in {"auto", "rest", "bulk"}:
        return jsonify({"error": "Unsupported pipeline validation option"}), 400
    if scan_mode == "sample" and not 1 <= sample_size <= 1_000_000:
        return jsonify({"error": "Sample size must be between 1 and 1,000,000 rows per object"}), 400
    stages: list[dict] = []
    planned_objects: list[str] = []
    seen_modes: set[str] = set()
    for index, stage in enumerate(raw_stages, start=1):
        template_id = str(stage.get("template_id") or "").strip()
        input_mode = _normalize_input_mode(stage.get("input_mode"))
        seen_modes.add(input_mode)
        stage_id = _slugify_template_name(stage.get("pipeline_id") or f"stage_{index}")
        stage_name = str(stage.get("pipeline_name") or stage_id).strip() or stage_id
        if not template_id:
            return jsonify({"error": f"Stage '{stage_name}' is missing a template reference"}), 400
        stages.append({
            "pipeline_id": stage_id,
            "pipeline_name": stage_name,
            "template_id": template_id,
            "input_mode": input_mode,
        })
        object_names = [
            str(value).strip()
            for value in (stage.get("object_names") or [])
            if str(value).strip()
        ]
        stages[-1]["object_names"] = object_names
        planned_objects.extend(
            f"{stage_name} :: {object_name}"
            for object_name in object_names
        )
    if len(seen_modes) > 1:
        return jsonify({
            "error": "A pipeline cannot mix SF → SF and Files → SF stages. Create separate pipelines for each operation type.",
        }), 400
    snapshot_error = _saved_pipeline_snapshot_error(data)
    if snapshot_error:
        return jsonify({"error": snapshot_error}), 409

    activity_key = f"pipeline_prevalidation_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    out_dir = validation_result_dir("pre", activity_key, planned_objects)
    request_dir = PROJECT_ROOT / "state" / "validation_requests"
    request_dir.mkdir(parents=True, exist_ok=True)
    request_path = request_dir / f"{activity_key}.json"
    request_payload = {
        "pipeline_id": pipeline_id,
        "pipeline_name": pipeline_name,
        "stages": stages,
        "optional_checks": optional_checks,
        "scan_mode": scan_mode,
        "sample_size": sample_size,
        "sample_seed": sample_seed,
        "query_mode": query_mode,
        "salesforce_id_length": salesforce_id_length,
    }
    request_path.write_text(json.dumps(request_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    command = [
        sys.executable,
        "prevalidate_pipeline.py",
        "--request",
        str(request_path),
        "--out-dir",
        str(out_dir),
    ]
    audit_payload = {
        "pipeline_id": pipeline_id,
        "pipeline_name": pipeline_name,
        "template_name": pipeline_name,
        "objects": planned_objects,
        "stages": stages,
        "optional_checks": optional_checks,
        "scan_mode": scan_mode,
        "sample_size": sample_size,
        "sample_seed": sample_seed,
        "query_mode": query_mode,
        "salesforce_id_length": salesforce_id_length,
        "out_dir": str(out_dir),
    }
    try:
        RUN_AUDIT.upsert_submission(activity_key, {
            "activity_type": "PRE_VALIDATION",
            "data_operation": "PIPELINE SEQUENCE",
            "migration_name": f"{pipeline_name} - Pre-validation",
            "status": "QUEUED",
            "request_payload_json": json.dumps(audit_payload, ensure_ascii=False),
        })
        runner = _launch_background_command(
            command,
            f"Starting pipeline pre-validation | pipeline={pipeline_id}",
            extra_env={"ETL_RUN_ORIGIN": "ui-pipeline-prevalidation"},
            run_meta={"origin": "ui-pipeline-prevalidation", "run_key": activity_key, "pipeline_id": pipeline_id, "out_dir": str(out_dir)},
        )
    except Exception as exc:
        _update_audit_status_resilient(activity_key, status="FAILED", error_message=str(exc))
        return jsonify({"error": str(exc)}), 500
    _update_audit_status_resilient(activity_key, status="RUNNING", runner=runner)
    return jsonify({
        "ok": True,
        "activity_key": activity_key,
        "runner": runner,
        "out_dir": str(out_dir),
        "progress_url": f"/validation/runs/{quote(activity_key, safe='')}",
    })


@app.route("/api/pipeline/skip-prevalidation", methods=["POST"])
def skip_pipeline_prevalidation():
    """Mark every stage template as BYPASSED so migration can start without pre-validation."""
    data = request.json or {}
    try:
        pipeline_name = _assert_required_unique_pipeline_name(
            data.get("pipeline_name") or data.get("pipeline_id"),
            require_unique=False,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    raw_stages = data.get("stages") or []
    if not isinstance(raw_stages, list) or not raw_stages:
        return jsonify({"error": "At least one pipeline stage is required"}), 400

    seen_modes: set[str] = set()
    for stage in raw_stages:
        seen_modes.add(_normalize_input_mode(stage.get("input_mode")))
    if len(seen_modes) > 1:
        return jsonify({
            "error": "A pipeline cannot mix SF → SF and Files → SF stages. Create separate pipelines for each operation type.",
        }), 400
    snapshot_error = _saved_pipeline_snapshot_error(data)
    if snapshot_error:
        return jsonify({"error": snapshot_error}), 409

    updated: list[dict] = []
    errors: list[str] = []
    for index, stage in enumerate(raw_stages, start=1):
        template_id = safe_name(str(stage.get("template_id") or "").strip())
        input_mode = _normalize_input_mode(stage.get("input_mode"))
        stage_name = str(stage.get("pipeline_name") or stage.get("pipeline_id") or f"stage_{index}").strip()
        if not template_id:
            errors.append(f"{stage_name}: missing template id")
            continue
        if input_mode == "flat_file":
            template_path = (FLAT_FILE_TEMPLATE_DIR / f"{template_id}.json").resolve()
            if template_path.parent != FLAT_FILE_TEMPLATE_DIR.resolve() or not template_path.is_file():
                errors.append(f"{stage_name}: flat-file template '{template_id}' not found")
                continue
        else:
            template_path = (TEMPLATE_DIR / f"{template_id}.json").resolve()
            if template_path.parent != TEMPLATE_DIR.resolve() or not template_path.is_file():
                errors.append(f"{stage_name}: source template '{template_id}' not found")
                continue
        try:
            payload = json.loads(template_path.read_text(encoding="utf-8"))
            payload["prevalidation_status"] = "BYPASSED"
            payload["prevalidation_bypassed_at"] = _utc_now()
            template_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            updated.append({
                "pipeline_id": str(stage.get("pipeline_id") or "").strip(),
                "pipeline_name": stage_name,
                "template_id": template_id,
                "input_mode": input_mode,
                "prevalidation_status": "BYPASSED",
            })
        except Exception as exc:
            errors.append(f"{stage_name}: {exc}")

    if errors and not updated:
        return jsonify({"error": "; ".join(errors)}), 400

    activity_key = f"pipeline_prevalidation_bypass_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    audit_payload = {
        "pipeline_name": pipeline_name,
        "stages": updated,
        "bypassed": True,
        "errors": errors,
    }
    RUN_AUDIT.upsert_submission(activity_key, {
        "activity_type": "PRE_VALIDATION",
        "data_operation": "BYPASSED",
        "migration_name": f"{pipeline_name} - Pre-validation",
        "status": "COMPLETED",
        "request_payload_json": json.dumps(audit_payload, ensure_ascii=False),
        "runner_json": json.dumps({"running": False, "origin": "ui-pipeline-prevalidation-bypass"}, ensure_ascii=False),
    })
    RUN_AUDIT.update_status(activity_key, status="COMPLETED")
    return jsonify({
        "ok": True,
        "activity_key": activity_key,
        "updated": updated,
        "errors": errors,
        "prevalidation_status": "BYPASSED",
    })


@app.route("/api/pipeline/resume", methods=["POST"])
def resume_pipeline_run():
    """
    Same as POST /sf-api/v1/runs with resume_run_key, for the Run Monitor (no SF API auth).
    Body must include run_key plus pipeline_id and start_stage (transform|load).
    """
    data = dict(request.json or {})
    rk = (data.get("run_key") or "").strip()
    if not rk:
        return jsonify({"error": "run_key is required"}), 400
    data["resume_run_key"] = rk
    payload, status_code = _start_pipeline_run_request(data, origin="ui")
    return jsonify(payload), status_code


@app.route("/api/pipeline/stop", methods=["POST"])
def stop_pipeline_run():
    data = request.json or {}
    run_key = str(data.get("run_key") or "").strip() or None
    payload, status_code = _stop_pipeline_run_request(origin="ui", expected_run_key=run_key)
    return jsonify(payload), status_code


@app.route("/api/pipeline/verify-self-lookup", methods=["POST"])
def verify_self_lookup_from_ui():
    current_runner = _runner_status()
    if current_runner.get("running"):
        return jsonify({"error": "A pipeline run is already in progress", "runner": current_runner}), 409

    data = request.json or {}
    pipeline_id = (data.get("pipeline_id") or "").strip()
    object_name = (data.get("object_name") or "").strip()
    field_name = (data.get("field_name") or "").strip()
    sample_size = int(data.get("sample_size") or 25)
    if not pipeline_id or not object_name:
        return jsonify({"error": "pipeline_id and object_name are required"}), 400

    command = [
        sys.executable,
        "cli.py",
        "verify-self-lookup",
        "--config",
        str(CONFIG_PATH),
        "--pipeline",
        pipeline_id,
        "--object",
        object_name,
        "--sample-size",
        str(sample_size),
    ]
    if field_name:
        command.extend(["--field", field_name])

    try:
        runner = _launch_background_command(
            command,
            f"Starting UI verify-self-lookup | pipeline={pipeline_id} | object={object_name} | field={field_name or 'auto'}",
            extra_env={"ETL_RUN_ORIGIN": "ui-verify"},
            run_meta={"origin": "ui-verify", "pipeline_id": pipeline_id, "object_name": object_name},
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"ok": True, "runner": runner})


@app.route("/api/pipeline/verify-records", methods=["POST"])
def verify_records_from_ui():
    current_runner = _runner_status()
    if current_runner.get("running"):
        return jsonify({"error": "A pipeline run is already in progress", "runner": current_runner}), 409

    data = request.json or {}
    run_dir = (data.get("run_dir") or "").strip()
    pipeline_id = (data.get("pipeline_id") or "").strip()
    object_name = (data.get("object_name") or "").strip()
    if not run_dir or not pipeline_id or not object_name:
        return jsonify({"error": "run_dir, pipeline_id, and object_name are required"}), 400

    command = [
        sys.executable,
        "verify_records.py",
        "--run-dir",
        run_dir,
        "--pipeline",
        pipeline_id,
        "--object",
        object_name,
    ]
    try:
        runner = _launch_background_command(
            command,
            f"Starting UI verify_records | run_dir={run_dir} | pipeline={pipeline_id} | object={object_name}",
            extra_env={"ETL_RUN_ORIGIN": "ui-verify-records"},
            run_meta={"origin": "ui-verify-records", "pipeline_id": pipeline_id, "object_name": object_name},
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "runner": runner})


@app.route("/api/pipeline/runs", methods=["GET"])
def list_pipeline_runs():
    try:
        _runner_status()
        try:
            limit = int(request.args.get("limit", 200))
        except ValueError:
            limit = 200
        try:
            offset = int(request.args.get("offset", 0))
        except ValueError:
            offset = 0
        offset = max(0, offset)
        activity_type = (request.args.get("activity_type") or "").strip() or None
        data_operation = (request.args.get("data_operation") or "").strip() or None
        q = (request.args.get("q") or "").strip() or None
        total = RUN_AUDIT.count_runs(
            activity_type=activity_type,
            data_operation=data_operation,
            q=q,
        )
        records = []
        for row in RUN_AUDIT.list_runs(
            limit,
            offset,
            activity_type=activity_type,
            data_operation=data_operation,
            q=q,
        ):
            try:
                row = _repair_pipeline_prevalidation_audit(row)
                row = _reconcile_finished_validation(row)
                activity_type = str(row.get("activity_type") or "MIGRATION").upper()
                if activity_type == "GENERAL_UTILITY":
                    job_id = str(row.get("run_key") or "")
                    job = _reconcile_general_utility_job(job_id)
                    job_status = str((job or {}).get("status") or "").casefold()
                    expected_audit_status = {"completed": "COMPLETED", "error": "FAILED"}.get(job_status)
                    if expected_audit_status and str(row.get("status") or "").upper() != expected_audit_status:
                        RUN_AUDIT.update_status(
                            job_id,
                            status=expected_audit_status,
                            status_payload=job,
                            error_message=(job or {}).get("error") if expected_audit_status == "FAILED" else None,
                            completed_at=(job or {}).get("finished_at"),
                        )
                    if expected_audit_status:
                        refreshed = RUN_AUDIT.get_run(job_id)
                        if refreshed:
                            row = refreshed
                status_payload = {}
                request_payload = {}
                try:
                    status_payload = json.loads(row.get("status_payload_json") or "{}")
                except (TypeError, ValueError):
                    pass
                if activity_type == "MIGRATION":
                    state_path = _run_state_path(str(row.get("run_key") or ""))
                    if state_path.is_file():
                        try:
                            disk_status = json.loads(state_path.read_text(encoding="utf-8"))
                            if disk_status.get("pipelines"):
                                status_payload = disk_status
                        except (OSError, ValueError):
                            pass
                try:
                    request_payload = json.loads(row.get("request_payload_json") or "{}")
                except (TypeError, ValueError):
                    pass
                pipelines = []
                for pipeline_id, pipeline in (status_payload.get("pipelines") or {}).items():
                    pipeline = pipeline if isinstance(pipeline, dict) else {}
                    pipelines.append({
                        "id": pipeline_id,
                        "name": pipeline.get("name") or pipeline_id,
                        "status": pipeline.get("status") or "PENDING",
                        "objects": pipeline.get("objects") or {},
                    })
                if not pipelines:
                    for pipeline in request_payload.get("pipelines") or []:
                        pipeline_id = str(pipeline.get("pipeline_id") or pipeline.get("id") or "").strip()
                        if pipeline_id:
                            pipelines.append({"id": pipeline_id, "name": pipeline.get("pipeline_name") or pipeline_id, "status": row.get("status") or "PENDING", "objects": {}})
                if activity_type in {"VALIDATION", "PRE_VALIDATION"} and not pipelines:
                    validation_pipeline = str(request_payload.get("pipeline_id") or "").strip()
                    if activity_type == "PRE_VALIDATION":
                        validation_pipeline = str(request_payload.get("template_id") or "").strip()
                    if validation_pipeline:
                        validation_name = str(request_payload.get("template_name") or validation_pipeline).strip()
                        if activity_type == "PRE_VALIDATION" and not request_payload.get("template_name"):
                            template_file = FLAT_FILE_TEMPLATE_DIR / f"{safe_name(validation_pipeline)}.json"
                            if template_file.is_file():
                                try:
                                    validation_name = str(json.loads(template_file.read_text(encoding="utf-8")).get("name") or validation_name)
                                except (OSError, ValueError):
                                    pass
                        pipelines.append({"id": validation_pipeline, "name": validation_name, "status": row.get("status") or "PENDING", "objects": {name: {} for name in request_payload.get("objects") or []}})
                audit_status = str(row.get("status") or "UNKNOWN").upper()
                if activity_type == "MIGRATION" and pipelines:
                    pipeline_statuses = {str(pipeline.get("status") or "PENDING").upper() for pipeline in pipelines}
                    if pipeline_statuses == {"COMPLETED"}:
                        audit_status = "COMPLETED"
                    elif "RUNNING" in pipeline_statuses:
                        audit_status = "RUNNING"
                    elif "STOPPED" in pipeline_statuses:
                        audit_status = "STOPPED"
                    elif pipeline_statuses <= {"COMPLETED", "FAILED"} and "FAILED" in pipeline_statuses:
                        audit_status = "FAILED"
                has_loaded_success = any(
                    int(obj.get("loaded_success") or 0) > 0
                    for pipeline in pipelines
                    for obj in (pipeline.get("objects") or {}).values()
                    if isinstance(obj, dict)
                )
                is_partial_terminal = audit_status == "FAILED" and has_loaded_success
                validation_eligible = activity_type == "MIGRATION" and (
                    audit_status in {"COMPLETED", "STOPPED"} or is_partial_terminal
                )
                validation_status = "COMPLETED (PARTIAL)" if is_partial_terminal else audit_status
                progress_percent = None
                completed_units = None
                total_units = None
                if activity_type in {"VALIDATION", "PRE_VALIDATION"}:
                    validation_dir = _validation_output_dir(row)
                    if activity_type == "PRE_VALIDATION":
                        progress_info = _compute_prevalidation_progress(
                            validation_dir,
                            request_payload,
                            audit_status,
                        )
                        total_units = progress_info["total_units"]
                        completed_units = progress_info["completed_units"]
                        progress_percent = progress_info["progress_percent"]
                    else:
                        planned = {str(name) for name in request_payload.get("objects") or [] if str(name)}
                        completed_names: set[str] = set()
                        if validation_dir and validation_dir.is_dir():
                            for summary in validation_dir.glob("*_verify_summary_*.csv"):
                                try:
                                    with summary.open("r", encoding="utf-8-sig", newline="") as handle:
                                        name = str(next(csv.DictReader(handle), {}).get("object_name") or "").strip()
                                    if name:
                                        completed_names.add(name)
                                except OSError:
                                    continue
                        total_units = max(len(planned), len(completed_names))
                        completed_units = len(completed_names)
                        progress_percent = 100 if audit_status in {"COMPLETED", "FAILED", "STOPPED"} else (round(completed_units * 100 / total_units) if total_units else 0)
                elif activity_type == "STORAGE":
                    progress_percent = int(status_payload.get("progress_percent") or (100 if audit_status in {"COMPLETED", "FAILED", "STOPPED"} else 5))
                elif activity_type == "GENERAL_UTILITY":
                    progress_percent = int(status_payload.get("percent") or (100 if audit_status in {"COMPLETED", "FAILED", "STOPPED"} else 0))
                    completed_units = int(status_payload.get("completed_steps") or 0)
                    total_units = int(status_payload.get("total_steps") or 0)
                template_id = str(request_payload.get("template_id") or "").strip() or None
                template_name = str(request_payload.get("template_name") or "").strip() or None
                if activity_type == "PRE_VALIDATION" and template_id and not template_name:
                    template_file = FLAT_FILE_TEMPLATE_DIR / f"{safe_name(template_id)}.json"
                    if template_file.is_file():
                        try:
                            template_name = str(json.loads(template_file.read_text(encoding="utf-8")).get("name") or "").strip() or template_id
                        except (OSError, ValueError):
                            template_name = template_id
                migration_pipeline_name = str(
                    request_payload.get("pipeline_name")
                    or request_payload.get("migration_pipeline_name")
                    or ""
                ).strip() or None
                records.append({
                    "run_key": row.get("run_key"),
                    "migration_name": row.get("migration_name"),
                    "template_id": template_id,
                    "template_name": template_name,
                    "migration_pipeline_name": migration_pipeline_name,
                    "activity_type": activity_type,
                    "data_operation": row.get("data_operation") or ("UPSERT" if activity_type == "MIGRATION" else "VALIDATION"),
                    "source_run_key": request_payload.get("source_run_key"),
                    "status": audit_status,
                    "validation_eligible": validation_eligible,
                    "validation_status": validation_status,
                    "progress_percent": progress_percent,
                    "completed_units": completed_units,
                    "total_units": total_units,
                    "source_profile": row.get("source_profile"),
                    "target_profile": row.get("target_profile"),
                    "submitted_by_name": row.get("submitted_by_name") or row.get("submitted_by_username"),
                    "created_at": row.get("created_at"),
                    "completed_at": row.get("completed_at"),
                    "error_message": row.get("error_message"),
                    "pipelines": pipelines,
                })
            except Exception as row_exc:
                records.append({
                    "run_key": str((row or {}).get("run_key") or ""),
                    "migration_name": str((row or {}).get("migration_name") or (row or {}).get("run_key") or "Unknown run"),
                    "activity_type": str((row or {}).get("activity_type") or "MIGRATION").upper(),
                    "status": "UNKNOWN",
                    "validation_eligible": False,
                    "validation_status": "UNKNOWN",
                    "error_message": f"Skipped corrupt run metadata: {row_exc}",
                    "pipelines": [],
                })
        return jsonify({
            "runs": records,
            "total": total,
            "limit": limit,
            "offset": offset,
        })
    except Exception as exc:
        return jsonify({"error": str(exc), "runs": [], "total": 0, "limit": 0, "offset": 0}), 500

@app.route("/api/validation/run", methods=["POST"])
def start_validation_run():
    current_runner = _runner_status()
    if current_runner.get("running"):
        return jsonify({"error": "A pipeline or validation command is already running", "runner": current_runner}), 409
    persisted_validation = _active_audited_validation()
    if persisted_validation:
        return jsonify({"error": f"Validation run '{persisted_validation['run_key']}' is already running", **persisted_validation}), 409

    data = request.json or {}
    run_key = str(data.get("run_key") or "").strip()
    pipeline_id = str(data.get("pipeline_id") or "").strip()
    compare = str(data.get("compare") or "source-target").strip()
    source_kind = str(data.get("source_kind") or "uploaded").strip()
    scan_mode = str(data.get("scan_mode") or "full").strip()
    query_mode = str(data.get("query_mode") or "auto").strip()
    try:
        sample_size = int(data.get("sample_size") or 1000)
        sample_seed = int(data.get("sample_seed") or 42)
    except (TypeError, ValueError):
        return jsonify({"error": "Sample size and seed must be whole numbers"}), 400
    objects = [str(value).strip() for value in (data.get("objects") or []) if str(value).strip()]
    if not run_key or not pipeline_id:
        return jsonify({"error": "run_key and pipeline_id are required"}), 400
    if compare not in {"source-target", "source-stage", "stage-target"}:
        return jsonify({"error": "Unsupported validation mode"}), 400
    if source_kind not in {"uploaded", "cleaned"}:
        return jsonify({"error": "Unsupported source kind"}), 400
    if scan_mode not in {"sample", "full"}:
        return jsonify({"error": "Unsupported scan mode"}), 400
    if query_mode not in {"auto", "rest", "bulk"}:
        return jsonify({"error": "Unsupported target retrieval mode"}), 400
    if scan_mode == "sample" and not 1 <= sample_size <= 1_000_000:
        return jsonify({"error": "Sample size must be between 1 and 1,000,000 rows per object"}), 400
    if not RUN_AUDIT.get_run(run_key):
        return jsonify({"error": f"Run '{run_key}' was not found"}), 404

    run_dir = _resolve_sf_run_dir(PROJECT_ROOT / "exports", run_key)
    if not run_dir:
        return jsonify({"error": f"Export artifacts for run '{run_key}' were not found"}), 404
    candidates = [path for path in run_dir.iterdir() if path.is_dir() and (path.name == pipeline_id or path.name.startswith(f"{pipeline_id}_attempt_"))]
    if not candidates:
        return jsonify({"error": f"Pipeline artifacts for '{pipeline_id}' were not found under {run_dir}"}), 404
    pipeline_dir = sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)[0]

    template_id = pipeline_id.split("_attempt_", 1)[0]
    config_path = FLAT_FILE_TEMPLATE_DIR / f"{safe_name(template_id)}.json"
    if not config_path.is_file():
        config_path = CONFIG_PATH
    source_dir = pipeline_dir / source_kind
    has_source_artifact = source_dir.is_dir() and (
        any(source_dir.glob("*.csv"))
        or (source_kind == "uploaded" and any(source_dir.glob("*.csv.reference.json")))
    )
    if compare != "stage-target" and not has_source_artifact:
        return jsonify({"error": f"No {source_kind} source CSV files or archived references found under {source_dir}"}), 409

    validation_key = f"validation_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    # A validation retry must never inherit reports from an earlier activity.
    # Keep the human context in audit metadata and use the unique activity key
    # as the immutable artifact directory.
    out_dir = validation_result_dir("post", validation_key, objects)
    command = [
        sys.executable, "verify_records.py",
        "--config", str(config_path),
        "--run-dir", str(run_dir),
        "--pipeline", pipeline_dir.name,
        "--compare", compare,
        "--out-dir", str(out_dir),
        "--sample-seed", str(sample_seed),
        "--query-mode", query_mode,
    ]
    if scan_mode == "sample":
        command.extend(["--sample-size", str(sample_size)])
    if compare != "stage-target":
        command.extend(["--source-dir", str(source_dir), "--source-kind", source_kind])
    for object_name in objects:
        command.extend(["--object", object_name])
    if data.get("print_soql"):
        command.append("--print-soql")
    source_run = RUN_AUDIT.get_run(run_key) or {}
    base_pipeline_name = _base_pipeline_activity_name(source_run.get("migration_name") or pipeline_id) or pipeline_id
    try:
        RUN_AUDIT.upsert_submission(validation_key, {
            "activity_type": "VALIDATION",
            "data_operation": "FULL SCAN" if scan_mode == "full" else f"SAMPLE ({sample_size:,}/OBJECT)",
            "migration_name": f"{base_pipeline_name} - Post Validation",
            "source_profile": None,
            "target_profile": (RUN_AUDIT.get_run(run_key) or {}).get("target_profile") or "default",
            "status": "QUEUED",
            "request_payload_json": json.dumps({
                "source_run_key": run_key, "pipeline_id": pipeline_id, "compare": compare,
                "source_kind": source_kind, "scan_mode": scan_mode, "sample_size": sample_size,
                "sample_seed": sample_seed, "query_mode": query_mode, "objects": objects,
                "out_dir": str(out_dir), "pipeline_name": base_pipeline_name,
            }, ensure_ascii=False),
        })
        runner = _launch_background_command(
            command,
            f"Starting validation | run={run_key} | pipeline={pipeline_id} | compare={compare} | scope={scan_mode}",
            extra_env={"ETL_RUN_ORIGIN": "ui-validation"},
            run_meta={"origin": "ui-validation", "run_key": validation_key, "source_run_key": run_key, "pipeline_id": pipeline_id, "out_dir": str(out_dir), "scan_mode": scan_mode, "sample_size": sample_size if scan_mode == "sample" else None, "query_mode": query_mode},
        )
    except Exception as e:
        RUN_AUDIT.update_status(validation_key, status="FAILED", error_message=str(e))
        return jsonify({"error": str(e)}), 500
    RUN_AUDIT.update_status(validation_key, status="RUNNING", runner=runner)
    return jsonify({"ok": True, "runner": runner, "out_dir": str(out_dir), "activity_key": validation_key, "progress_url": f"/validation/runs/{quote(validation_key, safe='')}"})


def _run_column_population_analysis(
    activity_key: str, paths: list[Path], minimum: int, out_dir: Path
) -> None:
    try:
        stats = []
        for index, path in enumerate(paths, start=1):
            with _column_analysis_lock:
                _column_analysis_jobs[activity_key].update({
                    "status": "RUNNING",
                    "current_file": path.name,
                    "current_index": index,
                    "total_files": len(paths),
                    "message": f"Reading {path.name} ({index}/{len(paths)})",
                })
            item = analyze_csv(path)
            stats.append(item)
            qualified = sum(count >= minimum for count in item.counts.values())
            with _column_analysis_lock:
                _column_analysis_jobs[activity_key].setdefault("file_summaries", []).append({
                    "file": path.name,
                    "path": str(path),
                    "total_rows": item.total_rows,
                    "total_columns": len(item.counts),
                    "qualifying_columns": qualified,
                })

        file_report = out_dir / "column_population_by_file.csv"
        consolidated_report = out_dir / "column_population_consolidated.csv"
        write_file_report(stats, file_report, minimum)
        write_consolidated_report(stats, consolidated_report, minimum)
        all_columns = set().union(*(set(item.counts) for item in stats)) if stats else set()
        consolidated_qualified = sum(
            sum(item.counts.get(column, 0) for item in stats) >= minimum
            for column in all_columns
        )
        empty_columns = sum(
            sum(item.counts.get(column, 0) for item in stats) == 0
            for column in all_columns
        )
        with _column_analysis_lock:
            _column_analysis_jobs[activity_key].update({
                "status": "COMPLETED",
                "message": "Column population analysis completed",
                "current_file": "",
                "total_rows": sum(item.total_rows for item in stats),
                "unique_columns": len(all_columns),
                "consolidated_qualifying_columns": consolidated_qualified,
                "completely_empty_columns": empty_columns,
                "downloads": {
                    "file_wise": f"/api/validation/column-analysis/{quote(activity_key, safe='')}/files/{file_report.name}",
                    "consolidated": f"/api/validation/column-analysis/{quote(activity_key, safe='')}/files/{consolidated_report.name}",
                },
            })
    except Exception as exc:
        logger.exception("Column population analysis failed: %s", activity_key)
        with _column_analysis_lock:
            _column_analysis_jobs[activity_key].update({
                "status": "FAILED",
                "message": str(exc),
                "error": str(exc),
            })


@app.route("/api/validation/column-analysis", methods=["POST"])
def start_column_population_analysis():
    data = request.get_json(silent=True) or {}
    raw_paths = data.get("paths") or data.get("file_paths") or []
    if isinstance(raw_paths, str):
        raw_paths = raw_paths.splitlines()
    raw_paths = [str(value or "").strip().strip('"') for value in raw_paths]
    raw_paths = [value for value in raw_paths if value]
    if not raw_paths:
        return jsonify({"error": "Enter at least one CSV file or folder path"}), 400
    try:
        minimum = int(data.get("min_non_empty") or 1)
    except (TypeError, ValueError):
        return jsonify({"error": "Minimum non-empty records must be a whole number"}), 400
    if not 1 <= minimum <= 100_000_000:
        return jsonify({"error": "Minimum non-empty records must be between 1 and 100,000,000"}), 400

    try:
        paths: list[Path] = []
        seen: set[Path] = set()
        for raw_path in raw_paths:
            for path in discover_file_validation_csvs(raw_path):
                resolved = path.resolve()
                if resolved not in seen:
                    paths.append(resolved)
                    seen.add(resolved)
        enforce_source_size_limits(paths)
    except (OSError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400

    activity_key = f"column_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    out_dir = COLUMN_ANALYSIS_DIR / activity_key
    out_dir.mkdir(parents=True, exist_ok=False)
    job = {
        "activity_key": activity_key,
        "status": "QUEUED",
        "message": "Column population analysis queued",
        "minimum_non_empty": minimum,
        "total_files": len(paths),
        "current_index": 0,
        "current_file": "",
        "file_summaries": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with _column_analysis_lock:
        _column_analysis_jobs[activity_key] = job
    threading.Thread(
        target=_run_column_population_analysis,
        args=(activity_key, paths, minimum, out_dir),
        daemon=True,
        name=f"column-analysis-{activity_key[-8:]}",
    ).start()
    return jsonify(job), 202


@app.route("/api/validation/column-analysis/<activity_key>")
def column_population_analysis_status(activity_key: str):
    with _column_analysis_lock:
        job = copy.deepcopy(_column_analysis_jobs.get(activity_key))
    if not job:
        return jsonify({"error": "Column analysis job was not found"}), 404
    return jsonify(job)


@app.route("/api/validation/column-analysis/<activity_key>/files/<filename>")
def download_column_population_report(activity_key: str, filename: str):
    if not re.fullmatch(r"column_analysis_[A-Za-z0-9_]+", activity_key):
        abort(404)
    allowed = {"column_population_by_file.csv", "column_population_consolidated.csv"}
    if filename not in allowed:
        abort(404)
    report = COLUMN_ANALYSIS_DIR / activity_key / filename
    if not report.is_file():
        abort(404)
    return send_file(report, as_attachment=True, download_name=filename)


@app.route("/api/validation/file-run", methods=["POST"])
def start_file_validation_run():
    current_runner = _runner_status()
    if current_runner.get("running"):
        return jsonify({"error": "A pipeline or validation command is already running", "runner": current_runner}), 409
    persisted_validation = _active_audited_validation()
    if persisted_validation:
        return jsonify({"error": f"Validation run '{persisted_validation['run_key']}' is already running", **persisted_validation}), 409
    data = request.get_json(silent=True) if request.is_json else request.form
    data = data or {}
    file_source = str(data.get("file_source") or "upload").strip()
    files = request.files.getlist("files") if file_source == "upload" else []
    drop_files = data.get("drop_files") or []
    file_path = str(data.get("file_path") or "").strip()
    object_name = str(data.get("object_name") or "").strip()
    source_id_column = str(data.get("source_id_column") or "").strip()
    target_match_field = str(data.get("target_match_field") or "").strip()
    reference_template_id = str(data.get("reference_template_id") or "").strip()
    ignore_fields_raw = data.get("ignore_fields") or []
    if isinstance(ignore_fields_raw, str):
        ignore_fields = [part.strip() for part in ignore_fields_raw.split(",") if part.strip()]
    else:
        ignore_fields = [str(part).strip() for part in ignore_fields_raw if str(part).strip()]
    exclude_fields_raw = data.get("exclude_fields") or []
    if isinstance(exclude_fields_raw, str):
        exclude_fields = [part.strip() for part in exclude_fields_raw.split(",") if part.strip()]
    else:
        exclude_fields = [str(part).strip() for part in exclude_fields_raw if str(part).strip()]
    # Keep Ignore and Exclude aliases in sync for the Object + Template path.
    for field in exclude_fields:
        if field not in ignore_fields:
            ignore_fields.append(field)
    include_fields_raw = data.get("include_fields") or []
    if isinstance(include_fields_raw, str):
        include_fields = [part.strip() for part in include_fields_raw.split(",") if part.strip()]
    else:
        include_fields = [str(part).strip() for part in include_fields_raw if str(part).strip()]
    file_query_mode = str(data.get("file_query_mode") or "object_template").strip().lower()
    custom_soql = str(data.get("custom_soql") or "").strip()
    scan_mode = str(data.get("scan_mode") or "full").strip()
    query_mode = str(data.get("query_mode") or "auto").strip()
    if file_source not in {"upload", "drop", "path"}:
        return jsonify({"error": "Unsupported file source"}), 400
    if (file_source == "upload" and not files) or (file_source == "drop" and not drop_files) or (file_source == "path" and not file_path) or not object_name or not source_id_column or not target_match_field:
        return jsonify({"error": "CSV file or folder path, target object, CSV ID column, and target match field are required"}), 400
    if file_source == "upload" and any(Path(item.filename or "").suffix.lower() != ".csv" for item in files):
        return jsonify({"error": "Only CSV files are supported"}), 400
    if file_query_mode not in {"object_template", "custom_soql"}:
        return jsonify({"error": "Unsupported file query mode"}), 400
    if file_query_mode == "custom_soql" and not custom_soql:
        return jsonify({"error": "Custom SOQL is required for Custom SOQL mode"}), 400
    if file_query_mode == "custom_soql":
        try:
            from core.file_validation_query import parse_custom_soql_select
            soql_object, _soql_fields = parse_custom_soql_select(custom_soql)
            if soql_object.casefold() != object_name.casefold():
                return jsonify({
                    "error": (
                        f"Custom SOQL FROM object '{soql_object}' does not match "
                        f"selected target object '{object_name}'"
                    )
                }), 400
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    if scan_mode not in {"sample", "full"} or query_mode not in {"auto", "rest", "bulk"}:
        return jsonify({"error": "Unsupported validation scope or target retrieval mode"}), 400
    try:
        sample_size = int(data.get("sample_size") or 1000)
    except (TypeError, ValueError):
        return jsonify({"error": "Sample size must be a whole number"}), 400
    if scan_mode == "sample" and not 1 <= sample_size <= 1_000_000:
        return jsonify({"error": "Sample size must be between 1 and 1,000,000 rows"}), 400
    try:
        id_chunk_size = int(data.get("id_chunk_size") or 200)
    except (TypeError, ValueError):
        return jsonify({"error": "REST ID batch size must be a whole number"}), 400
    if not 25 <= id_chunk_size <= 500:
        return jsonify({"error": "REST ID batch size must be between 25 and 500"}), 400
    try:
        target = get_connector("target", "default")
        describe = target.describe_object(object_name)
        field_names = {str(field.get("name")) for field in describe.get("fields", [])}
        if target_match_field not in field_names:
            return jsonify({"error": f"Target field '{target_match_field}' was not found on {object_name}"}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    activity_key = f"file_validation_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    upload_dir = FILE_VALIDATION_UPLOAD_DIR / activity_key
    out_dir = validation_result_dir("post", activity_key, [object_name])
    upload_dir.mkdir(parents=True, exist_ok=False)
    saved_files = []
    try:
        if file_source == "path":
            sources = discover_file_validation_csvs(file_path)
            enforce_source_size_limits(sources)
            for index, source in enumerate(sources, start=1):
                headers = usable_csv_headers(read_csv_header(source))
                if not headers:
                    raise ValueError(f"{source.name}: the CSV header is empty or contains blank columns")
                if len(headers) != len(set(headers)):
                    raise ValueError(f"{source.name}: duplicate CSV column names are not supported")
                if source_id_column not in headers:
                    raise ValueError(f"CSV column '{source_id_column}' was not found in {source.name}")
                saved = upload_dir / f"{index:03d}_{safe_name(source.name, f'file_{index}.csv')}"
                shutil.copy2(source, saved)
                saved_files.append(str(saved))
        elif file_source == "drop":
            drop_root = FLAT_FILE_DROP_DIR.resolve()
            drop_sources = []
            for index, raw_name in enumerate(drop_files, start=1):
                relative = Path(str(raw_name))
                source = (drop_root / relative).resolve()
                if relative.is_absolute() or ".." in relative.parts or not source.is_relative_to(drop_root) or not source.is_file() or source.suffix.lower() != ".csv":
                    raise ValueError(f"Invalid or missing drop-folder CSV: {raw_name}")
                drop_sources.append(source)
            enforce_source_size_limits(drop_sources)
            for index, source in enumerate(drop_sources, start=1):
                saved = upload_dir / f"{index:03d}_{safe_name(source.name, f'file_{index}.csv')}"
                shutil.copy2(source, saved)
                saved_files.append(str(saved))
        else:
            for index, item in enumerate(files, start=1):
                saved = upload_dir / f"{index:03d}_{safe_name(Path(item.filename or f'file_{index}.csv').name, f'file_{index}.csv')}"
                item.save(saved)
                saved_files.append(str(saved))
            enforce_source_size_limits(Path(path) for path in saved_files)
        preflight_fields: list[str] = []
        for saved_file in saved_files:
            for header in usable_csv_headers(read_csv_header(Path(saved_file))):
                if header not in preflight_fields:
                    preflight_fields.append(header)
        validate_file_validation_output_paths(
            out_dir,
            object_name=object_name,
            source_names=[Path(path).name for path in saved_files],
            field_names=preflight_fields,
        )
        lookup_mappings = []
        client_mappings = data.get("lookup_mappings") or []
        if isinstance(client_mappings, list) and client_mappings:
            target_profile = build_field_profile(target, object_name)
            for raw in client_mappings:
                if not isinstance(raw, dict):
                    continue
                enabled_raw = raw.get("enabled", True)
                if isinstance(enabled_raw, str):
                    enabled = enabled_raw.strip().lower() not in {"0", "false", "no", "off", ""}
                else:
                    enabled = bool(enabled_raw)
                if not enabled:
                    continue
                source_field = str(raw.get("source_field") or "").strip()
                target_field = str(raw.get("target_field") or source_field).strip()
                external_id = str(raw.get("external_id") or "").strip()
                referenced_object = str(raw.get("referenced_object") or "").strip()
                if not source_field or not target_field or not external_id:
                    continue
                meta = target_profile.field_meta.get(target_field)
                if not meta or not meta.relationship_name or len(meta.references_to) != 1:
                    continue
                if referenced_object and referenced_object != meta.references_to[0]:
                    continue
                reference_profile = build_field_profile(target, meta.references_to[0])
                if external_id not in reference_profile.field_meta:
                    continue
                lookup_mappings.append({
                    "source_field": source_field,
                    "target_field": target_field,
                    "referenced_object": meta.references_to[0],
                    "external_id": external_id,
                })
        elif file_query_mode == "object_template" and reference_template_id:
            template_path = FLAT_FILE_TEMPLATE_DIR / f"{safe_name(reference_template_id)}.json"
            if not template_path.is_file():
                raise ValueError(f"Reference template '{reference_template_id}' was not found")
            template = json.loads(template_path.read_text(encoding="utf-8"))
            file_cfg = next((item for item in template.get("files", []) or [] if str(item.get("object") or "") == object_name), None)
            target_profile = build_field_profile(target, object_name)
            for mapping in (file_cfg or {}).get("mappings", []) or []:
                source_field = str(mapping.get("source") or "").strip()
                target_field = str(mapping.get("target") or "").strip()
                external_id = str(mapping.get("lookup_external_id") or mapping.get("external_id") or "").strip()
                if not mapping.get("ignored") and source_field and target_field and external_id and str(mapping.get("lookup_resolution") or "external_id") == "external_id":
                    meta = target_profile.field_meta.get(target_field)
                    if not meta or not meta.relationship_name or len(meta.references_to) != 1:
                        continue
                    reference_profile = build_field_profile(target, meta.references_to[0])
                    if external_id not in reference_profile.field_meta:
                        continue
                    lookup_mappings.append({"source_field": source_field, "target_field": target_field, "referenced_object": mapping.get("referenced_object") or meta.references_to[0], "external_id": external_id})
        manifest = {
            "object_name": object_name,
            "source_id_column": source_id_column,
            "target_match_field": target_match_field,
            "reference_template_id": (reference_template_id or None) if file_query_mode == "object_template" else None,
            "file_query_mode": file_query_mode,
            "custom_soql": custom_soql if file_query_mode == "custom_soql" else "",
            "include_fields": include_fields if file_query_mode == "object_template" else [],
            "exclude_fields": ignore_fields,
            "ignore_fields": ignore_fields,
            "lookup_mappings": lookup_mappings,
            "files": saved_files,
        }
        manifest_path = upload_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        command = [sys.executable, "file_validate_target.py", "--manifest", str(manifest_path), "--out-dir", str(out_dir), "--query-mode", query_mode]
        if scan_mode == "sample":
            command.extend(["--sample-size", str(sample_size)])
        if query_mode == "rest":
            command.extend(["--id-chunk-size", str(id_chunk_size)])
        if str(data.get("print_soql") or "").lower() in {"1", "true", "on"}:
            command.append("--print-soql")
        payload = {**manifest, "validation_source": "file_upload", "file_source": file_source, "manifest_path": str(manifest_path), "files": [Path(path).name for path in saved_files], "objects": [object_name], "out_dir": str(out_dir), "scan_mode": scan_mode, "sample_size": sample_size, "query_mode": query_mode, "id_chunk_size": id_chunk_size}
        write_file_validation_progress(out_dir, {
            "phase": "starting",
            "object_name": object_name,
            "total_files": len(saved_files),
            "detail": f"Queued file validation for {object_name} | files={len(saved_files)}",
        })
        RUN_AUDIT.upsert_submission(activity_key, {"activity_type": "VALIDATION", "data_operation": "FILE COMPARISON", "migration_name": f"{object_name} - File Validation", "target_profile": "default", "status": "QUEUED", "request_payload_json": json.dumps(payload, ensure_ascii=False)})
        runner = _launch_background_command(command, f"Starting file validation | object={object_name} | files={len(saved_files)} | source={file_source}", extra_env={"ETL_RUN_ORIGIN": "ui-file-validation"}, run_meta={"origin": "ui-file-validation", "run_key": activity_key, "out_dir": str(out_dir)})
        RUN_AUDIT.update_status(activity_key, status="RUNNING", runner=runner)
    except Exception as exc:
        RUN_AUDIT.update_status(activity_key, status="FAILED", error_message=str(exc))
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True, "activity_key": activity_key, "progress_url": f"/validation/runs/{quote(activity_key, safe='')}"})


@app.route("/api/validation/file-lookup-maps", methods=["POST"])
def file_validation_lookup_maps():
    """Discover CSV lookup columns and recommended parent External IDs for File-based validation."""
    data = request.json or {}
    file_path = str(data.get("file_path") or "").strip()
    object_name = str(data.get("object_name") or "").strip()
    include_raw = data.get("include_fields") or []
    if isinstance(include_raw, str):
        include_fields = [part.strip() for part in include_raw.split(",") if part.strip()]
    else:
        include_fields = [str(part).strip() for part in include_raw if str(part).strip()]
    if not file_path or not object_name:
        return jsonify({"error": "CSV file or folder path and target object are required"}), 400
    try:
        sources = discover_file_validation_csvs(file_path)
        headers: list[str] = []
        for source in sources:
            for header in read_csv_header(source):
                if header and header not in headers:
                    headers.append(header)
        target = get_connector("target", "default")
        describe = target.describe_object(object_name)
        object_fields = list(describe.get("fields") or [])
        parent_external_ids: dict[str, list[str]] = {}
        parent_names: set[str] = set()
        for field in object_fields:
            if str(field.get("type") or "").lower() != "reference":
                continue
            refs = [str(ref).strip() for ref in (field.get("referenceTo") or []) if str(ref or "").strip()]
            if len(refs) == 1:
                parent_names.add(refs[0])
        for parent_name in sorted(parent_names):
            parent_describe = target.describe_object(parent_name)
            parent_external_ids[parent_name] = [
                str(ref_field.get("name") or "").strip()
                for ref_field in (parent_describe.get("fields") or [])
                if ref_field.get("externalId") and str(ref_field.get("name") or "").strip()
            ]
        mappings, skipped = discover_lookup_mapping_candidates(
            headers,
            object_fields,
            parent_external_ids,
            include_fields=include_fields,
        )
        return jsonify({
            "object_name": object_name,
            "columns": headers,
            "mappings": mappings,
            "skipped_polymorphic": skipped,
            "files_checked": len(sources),
        })
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/validation/file-template-check", methods=["POST"])
def check_file_validation_template():
    data = request.json or {}
    file_path = str(data.get("file_path") or "").strip()
    object_name = str(data.get("object_name") or "").strip()
    template_id = str(data.get("reference_template_id") or "").strip()
    if not file_path or not object_name or not template_id:
        return jsonify({"compatible": True, "warnings": [], "reference_mappings": 0})
    try:
        sources = discover_file_validation_csvs(file_path)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    template_path = FLAT_FILE_TEMPLATE_DIR / f"{safe_name(template_id)}.json"
    if not template_path.is_file():
        return jsonify({"error": f"Reference template '{template_id}' was not found"}), 400
    try:
        headers: set[str] = set()
        for source in sources:
            headers.update(read_csv_header(source))
        template = json.loads(template_path.read_text(encoding="utf-8"))
        file_cfg = next((item for item in template.get("files", []) or [] if str(item.get("object") or "") == object_name), None)
        if not file_cfg:
            return jsonify({"compatible": False, "warnings": [f"Template '{template_id}' does not contain object {object_name}."], "reference_mappings": 0})
        target = get_connector("target", "default")
        target_profile = build_field_profile(target, object_name)
        warnings = []
        checked = 0
        usable = 0
        for mapping in file_cfg.get("mappings", []) or []:
            if mapping.get("ignored"):
                continue
            source_field = str(mapping.get("source") or "").strip()
            target_field = str(mapping.get("target") or "").strip()
            external_id = str(mapping.get("lookup_external_id") or mapping.get("external_id") or "").strip()
            if not source_field or not target_field or not external_id:
                continue
            checked += 1
            if source_field not in headers:
                warnings.append(f"CSV lookup column '{source_field}' is not present; this reference will be skipped.")
                continue
            meta = target_profile.field_meta.get(target_field)
            if not meta or not meta.is_lookup or not meta.relationship_name or len(meta.references_to) != 1:
                warnings.append(f"{object_name}.{target_field} is not a single-object reference that can be validated; it will be skipped.")
                continue
            referenced_object = meta.references_to[0]
            reference_profile = build_field_profile(target, referenced_object)
            reference_meta = reference_profile.field_meta.get(external_id)
            if not reference_meta:
                warnings.append(f"{referenced_object}.{external_id} does not exist in the target; {source_field} will be skipped.")
                continue
            if not (reference_meta.is_external_id or external_id == "External_Id__c"):
                warnings.append(f"{referenced_object}.{external_id} is not marked External ID; matches may be ambiguous.")
            usable += 1
        if checked == 0:
            warnings.append(f"Template '{template_id}' has no external-ID reference mappings for {object_name}; validation will compare direct fields only.")
        return jsonify({"compatible": not warnings, "warnings": warnings, "reference_mappings": checked, "usable_reference_mappings": usable, "files_checked": len(sources)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/validation/preflight", methods=["POST"])
def start_prevalidation_run():
    current_runner = _runner_status()
    if current_runner.get("running"):
        return jsonify({"error": "A pipeline or validation command is already running", "runner": current_runner}), 409
    persisted_validation = _active_audited_validation()
    if persisted_validation:
        return jsonify({"error": f"Validation run '{persisted_validation['run_key']}' is already running", **persisted_validation}), 409
    data = request.json or {}
    raw_template_id = str(data.get("template_id") or "").strip()
    if not raw_template_id:
        return jsonify({"error": "Select a saved Flat Files template"}), 400
    template_id = safe_name(raw_template_id)
    target_aware = bool(data.get("target_aware"))
    allowed_optional_checks = {
        "duplicate_keys", "required_fields", "restricted_picklists",
        "dependent_picklists", "master_detail_references", "lookup_references",
        "text_limits", "numeric_precision", "salesforce_id_format", "contact_formats",
        "date_formats",
    }
    optional_checks = {
        str(value).strip() for value in (data.get("optional_checks") or [])
        if str(value).strip() in allowed_optional_checks
    }
    salesforce_id_length = str(data.get("salesforce_id_length") or "15_or_18")
    if salesforce_id_length not in {"15_or_18", "15", "18"}:
        return jsonify({"error": "Salesforce ID length must be 15, 18, or 15-or-18 characters"}), 400
    target_aware = target_aware or bool(optional_checks & {"master_detail_references", "lookup_references"})
    scan_mode = str(data.get("scan_mode") or "full").strip()
    query_mode = str(data.get("query_mode") or "auto").strip()
    try:
        sample_size = int(data.get("sample_size") or 1000)
        sample_seed = int(data.get("sample_seed") or 42)
    except (TypeError, ValueError):
        return jsonify({"error": "Sample size and seed must be whole numbers"}), 400
    flat_template_path = (FLAT_FILE_TEMPLATE_DIR / f"{template_id}.json").resolve()
    source_template_path = (TEMPLATE_DIR / f"{template_id}.json").resolve()
    if flat_template_path.parent != FLAT_FILE_TEMPLATE_DIR.resolve() or source_template_path.parent != TEMPLATE_DIR.resolve():
        return jsonify({"error": "Invalid template path"}), 400
    if flat_template_path.is_file():
        template_path = flat_template_path
        template_kind = "flat_file"
    elif source_template_path.is_file():
        template_path = source_template_path
        template_kind = "salesforce_source"
    else:
        return jsonify({"error": f"Template '{template_id}' was not found"}), 404
    if scan_mode not in {"sample", "full"} or query_mode not in {"auto", "rest", "bulk"}:
        return jsonify({"error": "Unsupported target-aware validation option"}), 400
    if target_aware and scan_mode == "full" and query_mode == "rest":
        return jsonify({"error": "Full target-aware preflight requires Auto or Bulk API 2.0; REST is available for sampled checks"}), 400
    if scan_mode == "sample" and not 1 <= sample_size <= 1_000_000:
        return jsonify({"error": "Sample size must be between 1 and 1,000,000 rows per object"}), 400
    template = json.loads(template_path.read_text(encoding="utf-8"))
    objects = [str(value).strip() for value in (data.get("objects") or []) if str(value).strip()]
    activity_key = f"prevalidation_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    result_objects = objects or [
        str(file_config.get("object") or "").strip()
        for file_config in (template.get("files") or [])
        if str(file_config.get("object") or "").strip()
    ]
    out_dir = validation_result_dir("pre", activity_key, result_objects)
    if template_kind == "flat_file":
        command = [sys.executable, "prevalidate_files.py", "--template", str(template_path), "--out-dir", str(out_dir), "--scan-mode", scan_mode, "--sample-size", str(sample_size), "--sample-seed", str(sample_seed), "--query-mode", query_mode, "--salesforce-id-length", salesforce_id_length]
        if target_aware:
            command.append("--target-aware")
        for check_id in sorted(optional_checks):
            command.extend(["--optional-check", check_id])
        if scan_mode == "sample":
            data_operation = f"{'TARGET-AWARE ' if target_aware else ''}SAMPLE ({sample_size:,}/OBJECT)"
        else:
            data_operation = "TARGET-AWARE FULL" if target_aware else "FULL FILE & METADATA"
    else:
        command = [sys.executable, "prevalidate_source.py", "--template", str(template_path), "--out-dir", str(out_dir)]
        data_operation = "SOURCE METADATA & MAPPING"
    for object_name in objects:
        command.extend(["--object", object_name])
    payload = {"template_id": template_id, "template_name": template.get("name") or template_id, "target_aware": target_aware, "optional_checks": sorted(optional_checks), "salesforce_id_length": salesforce_id_length, "scan_mode": scan_mode, "sample_size": sample_size, "sample_seed": sample_seed, "query_mode": query_mode, "objects": objects, "out_dir": str(out_dir)}
    try:
        RUN_AUDIT.upsert_submission(activity_key, {
            "activity_type": "PRE_VALIDATION",
            "data_operation": data_operation,
            "migration_name": f"Pre-validate {template.get('name') or template_id}",
            "target_profile": template.get("target_profile") or "default",
            "status": "QUEUED", "request_payload_json": json.dumps(payload, ensure_ascii=False),
        })
        runner = _launch_background_command(command, f"Starting pre-validation | template={template_id}", extra_env={"ETL_RUN_ORIGIN": "ui-prevalidation"}, run_meta={"origin": "ui-prevalidation", "run_key": activity_key, "pipeline_id": template_id, "out_dir": str(out_dir), **payload})
    except Exception as exc:
        RUN_AUDIT.update_status(activity_key, status="FAILED", error_message=str(exc))
        return jsonify({"error": str(exc)}), 500
    RUN_AUDIT.update_status(activity_key, status="RUNNING", runner=runner)
    return jsonify({"ok": True, "runner": runner, "out_dir": str(out_dir), "activity_key": activity_key, "progress_url": f"/validation/runs/{quote(activity_key, safe='')}"})


@app.route("/api/validation/preflight/bypass", methods=["POST"])
def bypass_prevalidation():
    data = request.json or {}
    raw_template_id = str(data.get("template_id") or "").strip()
    if not raw_template_id:
        return jsonify({"error": "Select a saved Flat Files template"}), 400
    template_id = safe_name(raw_template_id)
    template_path = (FLAT_FILE_TEMPLATE_DIR / f"{template_id}.json").resolve()
    if template_path.parent != FLAT_FILE_TEMPLATE_DIR.resolve() or not template_path.is_file():
        return jsonify({"error": f"Flat Files template '{template_id}' was not found"}), 404
    try:
        template = json.loads(template_path.read_text(encoding="utf-8"))
        batch_dir = (FLAT_FILE_UPLOAD_DIR / str(template.get("batch_id") or "")).resolve()
        if batch_dir.parent != FLAT_FILE_UPLOAD_DIR.resolve() or not batch_dir.is_dir():
            raise FileNotFoundError("The template upload batch was not found")
        activity_key = f"prevalidation_bypass_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        result_objects = [
            str(file_config.get("object") or "").strip()
            for file_config in (template.get("files") or [])
            if not file_config.get("skip_staging") and str(file_config.get("object") or "").strip()
        ]
        out_dir = validation_result_dir("pre", activity_key, result_objects)
        report = {"template_id": template_id, "target_aware": False, "status": "BYPASSED", "files": []}
        objects = []
        for file_config in template.get("files") or []:
            if file_config.get("skip_staging"):
                continue
            stored_name = safe_name(str(file_config.get("stored_name") or file_config.get("file") or ""))
            source_path = resolve_flat_file_path(
                file_config, batch_dir, FLAT_FILE_DROP_DIR, prefer_cleaned=False
            )
            if not source_path.is_file():
                raise FileNotFoundError(f"Original uploaded file was not found: {stored_name}")
            # Bypass means exactly that: do not manufacture a second multi-GB
            # "clean" copy or scan every row on the request thread.
            row_count = 0
            file_config.pop("cleaned_name", None)
            file_config.pop("rejected_name", None)
            file_config["cleaning_result"] = {"clean_count": row_count, "rejected_count": 0, "normalized_count": 0, "bypassed": True}
            object_name = str(file_config.get("object") or stored_name)
            objects.append(object_name)
            report["files"].append({"file": stored_name, "object": object_name, "errors": [], "field_access_issues": [], "bypassed": True, "file_validation": file_config["cleaning_result"]})
        template["prevalidation_status"] = "BYPASSED"
        template["prevalidation_bypassed_at"] = _utc_now()
        template_path.write_text(json.dumps(template, indent=2, ensure_ascii=False), encoding="utf-8")
        (out_dir / "prevalidation_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        stager = Stager()
        try:
            staged = FlatFileInput(stager, batch_dir, FLAT_FILE_DROP_DIR).ingest(template, replace=True)
        finally:
            stager.close()
        payload = {"template_id": template_id, "template_name": template.get("name") or template_id, "objects": objects, "out_dir": str(out_dir), "bypassed": True}
        runner = {"origin": "ui-prevalidation-bypass", "run_key": activity_key, "out_dir": str(out_dir), "running": False}
        RUN_AUDIT.upsert_submission(activity_key, {"activity_type": "PRE_VALIDATION", "data_operation": "BYPASSED & STAGED", "migration_name": f"Bypass pre-validation {template.get('name') or template_id}", "target_profile": template.get("target_profile") or "default", "status": "COMPLETED", "request_payload_json": json.dumps(payload, ensure_ascii=False), "runner_json": json.dumps(runner, ensure_ascii=False)})
        RUN_AUDIT.update_status(activity_key, status="COMPLETED", runner=runner)
        return jsonify({"ok": True, "activity_key": activity_key, "out_dir": str(out_dir), "progress_url": f"/validation/runs/{quote(activity_key, safe='')}", "pipeline_url": f"/pipeline?add_flat={quote(template_id, safe='')}", "staged": {"object_counts": staged.object_counts, "file_counts": staged.file_counts}})
    except Exception as exc:
        logger.exception("Pre-validation bypass failed")
        return jsonify({"error": str(exc)}), 500


def _validation_output_dir(record: dict) -> Path | None:
    try:
        runner = json.loads(record.get("runner_json") or "{}")
    except (TypeError, ValueError):
        runner = {}
    try:
        request_payload = json.loads(record.get("request_payload_json") or "{}")
    except (TypeError, ValueError):
        request_payload = {}
    configured = str(runner.get("out_dir") or request_payload.get("out_dir") or "").strip()
    if not configured and str(record.get("activity_type") or "").upper() == "PRE_VALIDATION":
        configured = str(PROJECT_ROOT / "logs" / "verify" / str(record.get("run_key") or ""))
    candidate = Path(configured).resolve() if configured else Path()
    roots = [
        (PROJECT_ROOT / "logs" / "verify").resolve(),
        PRE_VALIDATE_DIR.resolve(),
        POST_VALIDATE_DIR.resolve(),
    ]
    return candidate if any(candidate == root or root in candidate.parents for root in roots) else None


def _load_json_file(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _latest_prevalidation_progress(out_dir: Path | None) -> dict | None:
    """Prefer nested file-level progress over coarser pipeline-stage progress."""
    if not out_dir or not out_dir.is_dir():
        return None
    candidates = []
    for path in out_dir.rglob("prevalidation_progress.json"):
        payload = _load_json_file(path)
        if not payload:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0
        # File-level progress includes total_files; prefer the freshest of those.
        rank = 1 if payload.get("total_files") else 0
        candidates.append((rank, mtime, payload))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[-1][2]


def _mark_prevalidation_progress_stopped(out_dir: Path | None) -> int:
    """Persist an explicit stop in every active pre-validation progress file."""
    if not out_dir or not out_dir.is_dir():
        return 0
    stopped_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    updated = 0
    for path in out_dir.rglob("prevalidation_progress.json"):
        payload = _load_json_file(path)
        if not payload or str(payload.get("status") or "").upper() not in {"RUNNING", "QUEUED"}:
            continue
        payload.update({
            "status": "STOPPED",
            "phase": "stopped",
            "message": "Validation stopped by user.",
            "stopped_at": stopped_at,
            "updated_at": stopped_at,
        })
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        try:
            temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
            updated += 1
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return updated


def _prevalidation_report_items(out_dir: Path | None) -> list[dict]:
    if not out_dir or not out_dir.is_dir():
        return []
    root_report = out_dir / "prevalidation_report.json"
    root_payload = _load_json_file(root_report) if root_report.is_file() else None
    if root_payload and (root_payload.get("files") or root_payload.get("status")):
        return [item for item in (root_payload.get("files") or []) if isinstance(item, dict)]
    items: list[dict] = []
    for path in sorted(out_dir.rglob("prevalidation_report.json")):
        if path == root_report:
            continue
        payload = _load_json_file(path)
        if not payload:
            continue
        for item in payload.get("files") or []:
            if isinstance(item, dict):
                items.append(item)
    return items


def _compute_prevalidation_progress(
    out_dir: Path | None,
    request_payload: dict,
    audit_status: str,
    completed_items: list[dict] | None = None,
) -> dict:
    """Derive percent/message for pre-validation from progress + report files."""
    progress = _latest_prevalidation_progress(out_dir) or {}
    items = completed_items if completed_items is not None else _prevalidation_report_items(out_dir)
    finished_items = [
        item for item in items
        if str(item.get("status") or "").upper() != "RUNNING"
        and not (item.get("file_validation") or {}).get("in_progress")
    ]
    planned = {str(name) for name in request_payload.get("objects") or [] if str(name)}
    total_files = int(progress.get("total_files") or 0)
    completed_files = int(progress.get("completed_files") or 0)
    if not total_files:
        total_files = max(len(planned), len(finished_items), int(progress.get("total_stages") or 0))
    if not completed_files:
        completed_files = len(finished_items) if finished_items else int(progress.get("completed_stages") or 0)

    file_fraction = 0.0
    phase = str(progress.get("phase") or "").strip().lower()
    rows = progress.get("rows_processed")
    total_rows = progress.get("total_rows")
    if phase == "duplicate_scan":
        if total_rows:
            file_fraction = 0.45 * min(1.0, float(rows or 0) / float(total_rows))
        elif rows:
            file_fraction = min(0.4, 0.05 + (float(rows) / 500_000.0) * 0.35)
        else:
            file_fraction = 0.05
    elif phase == "schema_clean":
        if total_rows:
            file_fraction = 0.45 + 0.55 * min(1.0, float(rows or 0) / float(total_rows))
        elif rows:
            file_fraction = 0.5
        else:
            file_fraction = 0.45
    elif phase == "target_checks":
        # Target-reference validation runs after both local row passes.  Treat it
        # as the final portion of the current file; resetting to the starting
        # fraction makes the displayed percentage move backwards.
        file_fraction = 0.99
    elif phase in {"starting", "stage"}:
        file_fraction = 0.02
    elif phase in {"completed", "stage_complete"}:
        file_fraction = 0.0

    # completed_files already includes the current file after it finishes, so only
    # add a fractional bump while a file is actively being processed.
    active = (
        audit_status in {"RUNNING", "QUEUED"}
        and phase not in {"", "completed", "stage_complete"}
        and str(progress.get("status") or "").upper() == "RUNNING"
    )
    units_done = float(completed_files) + (file_fraction if active and completed_files < max(total_files, 1) else 0.0)
    if active and completed_files >= total_files and total_files:
        # Current file in progress but completed_files still points at previous.
        current_index = int(progress.get("current_index") or 0)
        if current_index > completed_files:
            units_done = float(completed_files) + file_fraction

    # Audit state can become terminal independently of the worker's progress
    # file (for example when the UI process restarts and temporarily cannot see
    # the validation subprocess).  Do not turn a partially processed run into
    # 100% merely because its audit row says FAILED/STOPPED.  A genuinely
    # finished validator records every attempted file as completed.
    all_units_finished = bool(total_files and completed_files >= total_files)
    no_planned_units_finished = (
        not total_files
        and audit_status in {"COMPLETED", "FAILED", "STOPPED"}
        and str(progress.get("phase") or "").strip().lower() == "completed"
    )
    if all_units_finished or no_planned_units_finished:
        percent = 100
    elif total_files:
        percent = max(1 if active else 0, min(99, round(units_done * 100 / total_files)))
    elif active:
        percent = 1
    else:
        percent = 0

    message = str(progress.get("message") or "").strip()
    if audit_status == "STOPPED" and str(progress.get("status") or "").upper() == "RUNNING":
        message = f"Validation stopped after {completed_files} of {total_files} file(s) finished"
    elif audit_status == "FAILED" and str(progress.get("status") or "").upper() == "RUNNING":
        message = f"Validation failed after {completed_files} of {total_files} file(s) finished"
    if not message and active:
        current_file = str(progress.get("current_file") or progress.get("current_stage") or "").strip()
        message = f"Validating {current_file}" if current_file else "Pre-validation is running..."
    if not message and finished_items:
        message = f"{len(finished_items)} file(s) finished"
    if not message:
        message = "Run accepted. Waiting for the first file result..."

    return {
        "progress": progress,
        "progress_percent": percent,
        "completed_units": completed_files if total_files else len(finished_items),
        "total_units": total_files or max(len(planned), len(finished_items)),
        "progress_message": message,
    }


def _compute_file_validation_progress(
    out_dir: Path | None,
    request_payload: dict,
    audit_status: str,
) -> dict:
    """Derive percent/message for file-based post-validation."""
    progress = read_file_validation_progress(out_dir) or {}
    run_status = str(audit_status or "").upper()
    active = run_status in {"RUNNING", "QUEUED"}

    if progress:
        percent = int(progress.get("progress_percent") or 0)
        completed_units = int(progress.get("completed_units") or 0)
        total_units = int(progress.get("total_units") or 5)
        message = str(progress.get("message") or "").strip()
        phase = str(progress.get("phase") or "").strip().lower()
        if run_status in {"COMPLETED", "FAILED", "STOPPED"} and phase in {"completed", "failed", ""}:
            percent = 100
            completed_units = total_units
        elif active:
            percent = max(1, min(99, percent or 1))
        if run_status == "STOPPED" and str(progress.get("status") or "").upper() == "RUNNING":
            message = message or "Validation was stopped"
        elif run_status == "FAILED" and str(progress.get("status") or "").upper() == "RUNNING":
            message = message or "Validation failed"
        return {
            "progress": progress,
            "progress_percent": percent,
            "completed_units": completed_units,
            "total_units": total_units,
            "progress_message": message,
            "progress_unit": str(progress.get("progress_unit") or "stages"),
        }

    object_name = str(
        (request_payload.get("object_name") or (request_payload.get("objects") or ["object"])[0])
        or "object"
    )
    if run_status in {"COMPLETED", "FAILED", "STOPPED"}:
        return {
            "progress": {},
            "progress_percent": 100,
            "completed_units": 5,
            "total_units": 5,
            "progress_message": f"File validation {run_status.lower()} for {object_name}",
            "progress_unit": "stages",
        }
    return {
        "progress": {},
        "progress_percent": 1 if active else 0,
        "completed_units": 0,
        "total_units": 5,
        "progress_message": f"Starting file validation for {object_name}",
        "progress_unit": "stages",
    }


def _is_file_validation_record(record: dict, request_payload: dict | None = None) -> bool:
    payload = request_payload if request_payload is not None else {}
    if str(record.get("data_operation") or "").upper() == "FILE COMPARISON":
        return True
    if str(payload.get("validation_source") or "") == "file_upload":
        return True
    run_key = str(record.get("run_key") or "")
    return run_key.startswith("file_validation_")


@app.route("/api/validation/runs/<activity_key>")
def validation_run_status(activity_key: str):
    try:
        return _validation_run_status_payload(activity_key)
    except Exception as exc:
        logger.exception("Validation run status failed for %s", activity_key)
        return jsonify({
            "error": f"Unable to load validation progress: {exc}",
            "activity_key": activity_key,
        }), 500


def _validation_run_status_payload(activity_key: str):
    _runner_status(activity_key)
    record = RUN_AUDIT.get_run(activity_key)
    if record:
        record = _repair_pipeline_prevalidation_audit(record)
        record = _reconcile_finished_validation(record)
    if not record or str(record.get("activity_type") or "").upper() not in {"VALIDATION", "PRE_VALIDATION"}:
        return jsonify({"error": "Validation run was not found"}), 404
    try:
        request_payload = json.loads(record.get("request_payload_json") or "{}")
    except (TypeError, ValueError):
        request_payload = {}
    record_status = str(record.get("status") or "").upper()
    out_dir = _validation_output_dir(record)
    objects = []
    files = []
    created_timestamp = None
    try:
        created_timestamp = datetime.fromisoformat(str(record.get("created_at") or "").replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        pass
    is_file_validation = _is_file_validation_record(record, request_payload)
    if out_dir and out_dir.is_dir():
        try:
            artifact_paths = sorted(out_dir.rglob("*"))
        except OSError:
            artifact_paths = []
        for path in artifact_paths:
            # Older versions reused one directory for retries. Ignore artifacts
            # that predate this audit activity so a running retry starts empty.
            try:
                if not path.is_file():
                    continue
                mtime = path.stat().st_mtime
                size = path.stat().st_size
            except OSError:
                # Files can appear/disappear while a long validation run writes reports.
                continue
            belongs_to_activity = created_timestamp is None or mtime >= created_timestamp - 2
            if belongs_to_activity and path.suffix.lower() in {".csv", ".xlsx", ".json", ".txt", ".log"}:
                rel = path.relative_to(out_dir).as_posix()
                files.append({"name": rel, "size": size, "download_url": f"/api/validation/runs/{quote(activity_key, safe='')}/files/{quote(rel, safe='/')}"})
        report_items = _prevalidation_report_items(out_dir) if str(record.get("activity_type") or "").upper() == "PRE_VALIDATION" else []
        if report_items:
            for item in report_items:
                validation = item.get("file_validation") or {}
                target_checks = item.get("target_checks") or []
                reasons = list(item.get("errors") or []) + [issue.get("message") for issue in item.get("field_access_issues") or [] if issue.get("message")]
                knowledge = item.get("knowledge_dependencies") or {}
                knowledge_issues = knowledge.get("issues") or []
                reasons.extend(
                    issue.get("message")
                    for issue in knowledge_issues
                    if issue.get("message")
                )
                knowledge_failures = sum(
                    int(issue.get("count") or 0) for issue in knowledge_issues
                )
                missing = sum(int(check.get("missing_count") or 0) for check in target_checks)
                rejected = int(validation.get("rejected_count") or 0)
                bypassed = bool(item.get("bypassed") or validation.get("bypassed"))
                progress_marked_running = (
                    str(item.get("status") or "").upper() == "RUNNING"
                    or bool(validation.get("in_progress"))
                )
                in_progress = progress_marked_running and record_status in {"RUNNING", "QUEUED"}
                stem = Path(str(item.get("file") or "")).stem
                # Nested pipeline stage outputs live under 01_stage/...; match by basename.
                downloads = [
                    file for file in files
                    if Path(file["name"]).name == f"{stem}_clean.csv"
                    or file["name"].endswith(f"/{stem}_clean.csv")
                ]
                if rejected:
                    downloads.extend(
                        file for file in files
                        if Path(file["name"]).name == f"{stem}_rejected.csv"
                        or file["name"].endswith(f"/{stem}_rejected.csv")
                    )
                if missing:
                    failure_name = f"{stem}_target_reference_failures.csv"
                    downloads.append({"name": failure_name, "size": None, "download_url": f"/api/validation/runs/{quote(activity_key, safe='')}/reference-failures/{quote(str(item.get('object') or item.get('file') or ''), safe='')}"})
                if in_progress:
                    object_status = "RUNNING"
                    object_reasons = reasons or ["Validation is still running for this file."]
                elif progress_marked_running and record_status in {"STOPPED", "FAILED"}:
                    object_status = record_status
                    object_reasons = [
                        "Validation was stopped before this file finished."
                        if record_status == "STOPPED"
                        else "Validation failed before this file finished."
                    ]
                else:
                    object_status = "BYPASSED" if bypassed else ("FAILED" if reasons or rejected or missing or knowledge_failures else "COMPLETED")
                    object_reasons = (["Pre-validation was explicitly bypassed; runtime failures remain possible"] if bypassed else reasons) + ([f"{rejected} row(s) rejected by file/schema validation"] if rejected else []) + ([f"{missing} target reference value(s) not found"] if missing else [])
                checked = int(validation.get("clean_count") or 0) + rejected
                if in_progress and validation.get("rows_processed"):
                    checked = int(validation.get("rows_processed") or checked)
                objects.append({
                    "name": item.get("object") or item.get("file"),
                    "file": item.get("file"),
                    "status": object_status,
                    "checked": checked,
                    "failed": rejected + missing + knowledge_failures,
                    "elapsed_seconds": item.get("elapsed_seconds"),
                    "reasons": object_reasons,
                    "downloads": downloads,
                })
        elif str(record.get("activity_type") or "").upper() != "PRE_VALIDATION":
            summaries_by_object: dict[str, dict[str, tuple[Path, dict]]] = {}
            for summary in sorted(out_dir.glob("*_summary_*.csv")):
                if created_timestamp is not None and summary.stat().st_mtime < created_timestamp - 2:
                    continue
                try:
                    with summary.open("r", encoding="utf-8-sig", newline="") as handle:
                        row = next(csv.DictReader(handle), {})
                    name = str(row.get("object_name") or "").strip()
                    if not name:
                        name = summary.name.split("_source_", 1)[0].split("_verify_", 1)[0]
                    phase = (
                        "verify" if "_verify_summary_" in summary.name
                        else "source_target" if "_source_target_summary_" in summary.name
                        else "source_stage"
                    )
                    summaries_by_object.setdefault(name, {})[phase] = (summary, row)
                except (OSError, ValueError):
                    continue
            for name, phases in sorted(summaries_by_object.items()):
                phase = next(candidate for candidate in ("verify", "source_target", "source_stage") if candidate in phases)
                _summary, row = phases[phase]
                missing = int(row.get("missing_in_target") or row.get("missing_in_stage") or row.get("missing") or 0)
                mismatched = int(row.get("mismatched_rows") or 0)
                duplicates = int(row.get("duplicate_rows") or 0)
                invalid_ids = int(row.get("invalid_id_rows") or 0)
                expected = int(row.get("expected_rows") or row.get("source_rows") or 0)
                reasons = []
                if missing: reasons.append(f"{missing} record(s) missing in compared data")
                if mismatched: reasons.append(f"{mismatched} record(s) have field mismatches")
                if duplicates: reasons.append(f"{duplicates} duplicate row(s) detected across the selected files")
                if invalid_ids: reasons.append(f"{invalid_ids} CSV row(s) have no match ID")
                exact_prefix = f"{name}_{phase}_"
                related = [file for file in files if file["name"].startswith(exact_prefix)]
                if not missing and not mismatched:
                    related = [file for file in related if "mismatch" not in file["name"].lower()]
                # Prefer field-summary / outputs for file validation downloads.
                if is_file_validation:
                    related = [
                        file for file in files
                        if file["name"].startswith(f"{name}_")
                        or file["name"].startswith("retrieved_target_by_source/")
                        or file["name"].startswith("mismatches_by_source_and_field/")
                        or file["name"] == "file_validation_progress.json"
                    ]
                objects.append({"name": name, "status": "FAILED" if missing or mismatched or duplicates or invalid_ids else "COMPLETED", "checked": expected + invalid_ids, "failed": missing + mismatched + duplicates + invalid_ids, "reasons": reasons, "downloads": related})
    progress_info = {}
    if str(record.get("activity_type") or "").upper() == "PRE_VALIDATION":
        progress_info = _compute_prevalidation_progress(
            out_dir,
            request_payload,
            str(record.get("status") or "").upper(),
            _prevalidation_report_items(out_dir) if out_dir else [],
        )
        # Ensure the current file has an in-progress card even before its report
        # entry is written.  Partial artifacts may already exist and should be
        # downloadable from the tile as well as the reports panel.
        if progress_info.get("progress"):
            progress = progress_info["progress"]
            current_name = str(
                progress.get("current_object")
                or progress.get("current_file")
                or progress.get("current_stage")
                or ""
            ).strip()
            if (
                current_name
                and record_status in {"RUNNING", "QUEUED"}
                and str(progress.get("status") or "").upper() == "RUNNING"
                and not any(
                    str(item.get("file") or "") == str(progress.get("current_file") or "")
                    for item in objects
                )
            ):
                current_stem = Path(str(progress.get("current_file") or "")).stem
                current_downloads = [
                    file for file in files
                    if Path(file["name"]).name in {
                        f"{current_stem}_clean.csv",
                        f"{current_stem}_rejected.csv",
                    }
                ]
                objects.append({
                    "name": current_name,
                    "file": progress.get("current_file"),
                    "status": "RUNNING",
                    "checked": int(progress.get("rows_processed") or 0),
                    "failed": int(progress.get("rejected_count") or 0),
                    "started_at": progress.get("current_file_started_at"),
                    "reasons": [progress_info.get("progress_message") or "Validation is running..."],
                    "downloads": current_downloads,
                })
    elif is_file_validation:
        progress_info = _compute_file_validation_progress(
            out_dir,
            request_payload,
            str(record.get("status") or "").upper(),
        )
        progress = progress_info.get("progress") or {}
        object_name = str(
            progress.get("object_name")
            or request_payload.get("object_name")
            or (request_payload.get("objects") or ["Account"])[0]
            or "Account"
        )
        if record_status in {"RUNNING", "QUEUED"} and not any(
            str(item.get("name") or "").lower() == object_name.lower()
            for item in objects
        ):
            objects.append({
                "name": object_name,
                "file": progress.get("current_file"),
                "status": "RUNNING",
                "checked": int(progress.get("rows_processed") or progress.get("unique_ids") or 0),
                "failed": 0,
                "started_at": progress.get("updated_at") or record.get("created_at"),
                "reasons": [progress_info.get("progress_message") or "File validation is running..."],
                "downloads": [
                    file for file in files
                    if file["name"] == "file_validation_progress.json"
                ],
            })
        elif objects and record_status in {"RUNNING", "QUEUED"} and progress_info.get("progress_message"):
            for item in objects:
                if str(item.get("status") or "").upper() == "RUNNING":
                    item["reasons"] = [progress_info["progress_message"]]
                    if progress.get("rows_processed") or progress.get("unique_ids"):
                        item["checked"] = int(
                            progress.get("rows_processed") or progress.get("unique_ids") or item.get("checked") or 0
                        )
    if objects:
        relevant_names = {
            download.get("name")
            for item in objects
            for download in item.get("downloads") or []
            if download.get("name")
        }
        relevant_names.update(
            file["name"] for file in files
            if file["name"].endswith(".json")
            or file["name"].endswith("prevalidation_progress.json")
            or file["name"].endswith("file_validation_progress.json")
        )
        # Keep nested stage clean/rejected artifacts visible while a file is running.
        for file in files:
            basename = Path(file["name"]).name.lower()
            if basename.endswith("_clean.csv") or basename.endswith("_rejected.csv"):
                relevant_names.add(file["name"])
            if is_file_validation and (
                basename.endswith("_summary.csv")
                or "_mismatch_by_field_" in basename
                or basename.endswith(".txt")
                or file["name"].startswith("retrieved_target_by_source/")
                or file["name"].startswith("mismatches_by_source_and_field/")
            ):
                relevant_names.add(file["name"])
        files = [file for file in files if file["name"] in relevant_names]
    template_name = request_payload.get("template_name") or request_payload.get("template_id")
    if request_payload.get("template_id") and not request_payload.get("template_name"):
        template_file = FLAT_FILE_TEMPLATE_DIR / f"{safe_name(str(request_payload['template_id']))}.json"
        if template_file.is_file():
            try:
                template_name = json.loads(template_file.read_text(encoding="utf-8")).get("name") or template_name
            except (OSError, ValueError):
                pass
    run_status = str(record.get("status") or "").upper()
    if progress_info:
        progress_percent = progress_info["progress_percent"]
        completed_units = progress_info["completed_units"]
        total_units = progress_info["total_units"]
        progress_message = progress_info["progress_message"]
        progress_unit = progress_info.get("progress_unit") or ("stages" if is_file_validation else "files")
    else:
        planned_objects = {str(name) for name in request_payload.get("objects") or [] if str(name)}
        total_units = max(len(planned_objects), len(objects))
        completed_units = len([item for item in objects if str(item.get("status") or "").upper() not in {"RUNNING", "PENDING"}])
        progress_percent = 100 if run_status in {"COMPLETED", "FAILED", "STOPPED"} else (round(completed_units * 100 / total_units) if total_units else 0)
        progress_message = ""
        progress_unit = "files"
    return jsonify({
        "activity_key": activity_key,
        "activity_type": record.get("activity_type"),
        "name": record.get("migration_name"),
        "template_id": request_payload.get("template_id"),
        "template_name": template_name,
        "status": record.get("status"),
        "created_at": record.get("created_at"),
        "completed_at": record.get("completed_at"),
        "error_message": record.get("error_message"),
        "output_dir": str(out_dir) if out_dir else None,
        "request": request_payload,
        "objects": objects,
        "files": files,
        "progress_percent": progress_percent,
        "completed_units": completed_units,
        "total_units": total_units,
        "progress_message": progress_message,
        "progress_unit": progress_unit,
    })


@app.route("/api/validation/runs/<activity_key>/stop", methods=["POST"])
def stop_validation_run(activity_key: str):
    record = RUN_AUDIT.get_run(activity_key)
    if not record or str(record.get("activity_type") or "").upper() not in {"VALIDATION", "PRE_VALIDATION"}:
        return jsonify({"error": "Validation run was not found"}), 404
    record_status = str(record.get("status") or "").upper()
    if record_status == "STOPPED":
        return jsonify({
            "ok": True,
            "message": "Validation run is already stopped",
            "run_key": activity_key,
            "status": "STOPPED",
        })
    stale_worker_failure = (
        record_status == "FAILED"
        and record.get("error_message")
        == "Validation worker is no longer running and did not write a completion marker"
    )
    if record_status not in {"RUNNING", "QUEUED"} and not stale_worker_failure:
        return jsonify({"error": f"Validation run is already {record.get('status') or 'finished'}"}), 409
    payload, status_code = _stop_pipeline_run_request(origin="ui", expected_run_key=activity_key)
    if status_code == 200:
        payload["message"] = "Validation run stopped"
        _mark_prevalidation_progress_stopped(_validation_output_dir(record))
    elif status_code == 409:
        # Stopping a subprocess and persisting its terminal state are separate
        # operations.  A Flask reload (or a rapid second click) can leave the
        # durable progress marker RUNNING even though the worker has exited.
        runner = payload.get("runner") if isinstance(payload, dict) else None
        runner = runner if isinstance(runner, dict) else _runner_status(activity_key)
        if not runner.get("running"):
            stopped_runner = {**runner, "running": False, "run_key": activity_key}
            progress_files_updated = _mark_prevalidation_progress_stopped(
                _validation_output_dir(record)
            )
            RUN_AUDIT.update_status(
                activity_key,
                status="STOPPED",
                runner=stopped_runner,
                error_message="Stopped by user",
            )
            payload = {
                "ok": True,
                "message": "Validation run stopped",
                "run_key": activity_key,
                "status": "STOPPED",
                "already_exited": True,
                "progress_files_updated": progress_files_updated,
                "runner": stopped_runner,
            }
            status_code = 200
    return jsonify(payload), status_code


@app.route("/api/validation/runs/<activity_key>/resume", methods=["POST"])
def resume_validation_run(activity_key: str):
    """Re-run a stopped/failed validation from its immutable stored request."""
    current_runner = _runner_status()
    if current_runner.get("running"):
        return jsonify({"error": "A pipeline or validation command is already running", "runner": current_runner}), 409
    record = RUN_AUDIT.get_run(activity_key)
    activity_type = str((record or {}).get("activity_type") or "").upper()
    if not record or activity_type not in {"VALIDATION", "PRE_VALIDATION"}:
        return jsonify({"error": "Validation run was not found"}), 404
    if str(record.get("status") or "").upper() in {"RUNNING", "QUEUED"}:
        return jsonify({"error": "Validation run is still active"}), 409
    try:
        payload = json.loads(record.get("request_payload_json") or "{}")
    except (TypeError, ValueError):
        return jsonify({"error": "Stored validation request is invalid"}), 409

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = uuid.uuid4().hex[:8]
    prefix = "validation" if activity_type == "VALIDATION" else "prevalidation"
    resumed_key = f"{prefix}_{stamp}_{suffix}"
    resume_objects = [str(name).strip() for name in (payload.get("objects") or []) if str(name).strip()]
    if not resume_objects and str(payload.get("object_name") or "").strip():
        resume_objects = [str(payload.get("object_name")).strip()]
    out_dir = validation_result_dir(
        "pre" if activity_type == "PRE_VALIDATION" else "post",
        resumed_key,
        resume_objects,
    )
    resumed_payload = {**payload, "out_dir": str(out_dir), "resumed_from_run_key": activity_key}

    if activity_type == "VALIDATION" and payload.get("validation_source") == "file_upload":
        manifest_path = Path(str(payload.get("manifest_path") or ""))
        if not manifest_path.is_file() or not manifest_path.resolve().is_relative_to(FILE_VALIDATION_UPLOAD_DIR.resolve()):
            return jsonify({"error": "The immutable uploaded-file manifest is no longer available"}), 409
        command = [sys.executable, "file_validate_target.py", "--manifest", str(manifest_path), "--out-dir", str(out_dir), "--query-mode", str(payload.get("query_mode") or "auto")]
        if str(payload.get("scan_mode") or "full") == "sample":
            command.extend(["--sample-size", str(payload.get("sample_size") or 1000)])
        if str(payload.get("query_mode") or "auto") == "rest":
            command.extend(["--id-chunk-size", str(payload.get("id_chunk_size") or 200)])
        origin = "ui-file-validation-resume"
    elif activity_type == "VALIDATION":
        source_run_key = str(payload.get("source_run_key") or "").strip()
        pipeline_id = str(payload.get("pipeline_id") or "").strip()
        run_dir = _resolve_sf_run_dir(PROJECT_ROOT / "exports", source_run_key)
        if not run_dir:
            return jsonify({"error": f"Export artifacts for run '{source_run_key}' were not found"}), 409
        candidates = [path for path in run_dir.iterdir() if path.is_dir() and (path.name == pipeline_id or path.name.startswith(f"{pipeline_id}_attempt_"))]
        if not candidates:
            return jsonify({"error": f"Pipeline artifacts for '{pipeline_id}' were not found"}), 409
        pipeline_dir = sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)[0]
        template_id = pipeline_id.split("_attempt_", 1)[0]
        config_path = FLAT_FILE_TEMPLATE_DIR / f"{safe_name(template_id)}.json"
        if not config_path.is_file():
            config_path = CONFIG_PATH
        compare = str(payload.get("compare") or "source-target")
        source_kind = str(payload.get("source_kind") or "uploaded")
        command = [sys.executable, "verify_records.py", "--config", str(config_path), "--run-dir", str(run_dir), "--pipeline", pipeline_dir.name, "--compare", compare, "--out-dir", str(out_dir), "--sample-seed", str(payload.get("sample_seed") or 42), "--query-mode", str(payload.get("query_mode") or "auto")]
        if str(payload.get("scan_mode") or "full") == "sample":
            command.extend(["--sample-size", str(payload.get("sample_size") or 1000)])
        if compare != "stage-target":
            command.extend(["--source-dir", str(pipeline_dir / source_kind), "--source-kind", source_kind])
        for name in payload.get("objects") or []:
            command.extend(["--object", str(name)])
        origin = "ui-validation-resume"
    elif payload.get("stages"):
        request_dir = PROJECT_ROOT / "state" / "validation_requests"
        request_dir.mkdir(parents=True, exist_ok=True)
        request_path = request_dir / f"{resumed_key}.json"
        request_body = {key: value for key, value in payload.items() if key not in {"out_dir", "objects", "template_name", "resumed_from_run_key"}}
        request_path.write_text(json.dumps(request_body, indent=2, ensure_ascii=False), encoding="utf-8")
        command = [sys.executable, "prevalidate_pipeline.py", "--request", str(request_path), "--out-dir", str(out_dir)]
        origin = "ui-pipeline-prevalidation-resume"
    else:
        template_id = safe_name(str(payload.get("template_id") or ""))
        flat_path = FLAT_FILE_TEMPLATE_DIR / f"{template_id}.json"
        source_path = TEMPLATE_DIR / f"{template_id}.json"
        if flat_path.is_file():
            command = [sys.executable, "prevalidate_files.py", "--template", str(flat_path), "--out-dir", str(out_dir), "--scan-mode", str(payload.get("scan_mode") or "full"), "--sample-size", str(payload.get("sample_size") or 1000), "--sample-seed", str(payload.get("sample_seed") or 42), "--query-mode", str(payload.get("query_mode") or "auto"), "--salesforce-id-length", str(payload.get("salesforce_id_length") or "15_or_18")]
            if payload.get("target_aware"):
                command.append("--target-aware")
            for check_id in payload.get("optional_checks") or []:
                command.extend(["--optional-check", str(check_id)])
            for name in payload.get("objects") or []:
                command.extend(["--object", str(name)])
        elif source_path.is_file():
            command = [sys.executable, "prevalidate_source.py", "--template", str(source_path), "--out-dir", str(out_dir)]
        else:
            return jsonify({"error": f"Template '{template_id}' was not found"}), 409
        origin = "ui-prevalidation-resume"

    try:
        RUN_AUDIT.upsert_submission(resumed_key, {
            "activity_type": activity_type,
            "data_operation": record.get("data_operation"),
            "migration_name": record.get("migration_name"),
            "source_profile": record.get("source_profile"),
            "target_profile": record.get("target_profile"),
            "resumed_from_run_key": activity_key,
            "status": "QUEUED",
            "request_payload_json": json.dumps(resumed_payload, ensure_ascii=False),
        })
        runner = _launch_background_command(command, f"Resuming validation | source={activity_key}", extra_env={"ETL_RUN_ORIGIN": origin}, run_meta={"origin": origin, "run_key": resumed_key, "resumed_from_run_key": activity_key, "out_dir": str(out_dir)})
        RUN_AUDIT.update_status(resumed_key, status="RUNNING", runner=runner)
    except Exception as exc:
        RUN_AUDIT.update_status(resumed_key, status="FAILED", error_message=str(exc))
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True, "activity_key": resumed_key, "resumed_from_run_key": activity_key, "progress_url": f"/validation/runs/{quote(resumed_key, safe='')}"})


@app.route("/api/validation/runs/<activity_key>/files/<path:filename>")
def download_validation_file(activity_key: str, filename: str):
    record = RUN_AUDIT.get_run(activity_key)
    out_dir = _validation_output_dir(record or {})
    if not out_dir:
        abort(404)
    candidate = (out_dir / filename).resolve()
    if out_dir not in candidate.parents or not candidate.is_file():
        abort(404)
    return send_file(candidate, as_attachment=True, download_name=candidate.name)


@app.route("/api/validation/runs/<activity_key>/download-zip")
def download_validation_zip(activity_key: str):
    """Download all reports, all mismatch partitions, or one source tab as ZIP."""
    record = RUN_AUDIT.get_run(activity_key)
    out_dir = _validation_output_dir(record or {})
    if not out_dir or not out_dir.is_dir():
        abort(404)

    scope = str(request.args.get("scope") or "all").strip().lower()
    partition_root = (out_dir / "mismatches_by_source_and_field").resolve()
    source_name = str(request.args.get("source") or "").strip()
    if scope == "all":
        candidates = [
            path for path in out_dir.rglob("*")
            if path.is_file() and "zip_downloads" not in path.relative_to(out_dir).parts
        ]
        archive_name = f"{safe_name(activity_key)}_all_reports.zip"
    elif scope == "partitions":
        if not partition_root.is_dir():
            abort(404)
        candidates = [path for path in partition_root.rglob("*.csv") if path.is_file()]
        archive_name = f"{safe_name(activity_key)}_mismatch_partitions.zip"
    elif scope == "source":
        if not source_name or not partition_root.is_dir():
            abort(400)
        source_dir = (partition_root / source_name).resolve()
        if partition_root not in source_dir.parents or not source_dir.is_dir():
            abort(404)
        candidates = [path for path in source_dir.glob("*.csv") if path.is_file()]
        archive_name = f"{safe_name(source_name)}_mismatch_fields.zip"
    else:
        abort(400)
    if not candidates:
        abort(404)

    zip_dir = out_dir / "zip_downloads"
    zip_dir.mkdir(parents=True, exist_ok=True)
    archive_path = zip_dir / archive_name
    temp_path = zip_dir / f".{archive_name}.{uuid.uuid4().hex}.tmp"
    try:
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(candidates, key=lambda item: str(item).lower()):
                archive.write(path, path.relative_to(out_dir).as_posix())
        temp_path.replace(archive_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return send_file(archive_path, as_attachment=True, download_name=archive_name)


@app.route("/api/validation/runs/<activity_key>/reference-failures/<object_name>")
def download_validation_reference_failures(activity_key: str, object_name: str):
    record = RUN_AUDIT.get_run(activity_key)
    out_dir = _validation_output_dir(record or {})
    report_path = (out_dir / "prevalidation_report.json") if out_dir else None
    if not report_path or not report_path.is_file():
        abort(404)
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        item = next((entry for entry in report.get("files") or [] if str(entry.get("object") or entry.get("file") or "") == object_name), None)
        if not item:
            abort(404)
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=["object", "source_field", "target_field", "referenced_object", "lookup_field", "missing_value", "total_missing_for_check"])
        writer.writeheader()
        for check in item.get("target_checks") or []:
            for value in check.get("missing_examples") or []:
                writer.writerow({"object": object_name, "source_field": check.get("source"), "target_field": check.get("target"), "referenced_object": check.get("referenced_object"), "lookup_field": check.get("lookup_field"), "missing_value": value, "total_missing_for_check": check.get("missing_count")})
        data = io.BytesIO(output.getvalue().encode("utf-8-sig"))
        return send_file(data, mimetype="text/csv", as_attachment=True, download_name=f"{safe_name(object_name)}_target_reference_failures.csv")
    except (OSError, ValueError):
        abort(404)


@app.route("/api/pipeline/report-last-level-refs", methods=["POST"])
def report_last_level_refs_from_ui():
    current_runner = _runner_status()
    if current_runner.get("running"):
        return jsonify({"error": "A pipeline run is already in progress", "runner": current_runner}), 409

    data = request.json or {}
    run_dir = (data.get("run_dir") or "").strip()
    pipeline_id = (data.get("pipeline_id") or "").strip()
    from_last_level = int(data.get("from_last_level") or 0)
    if not run_dir or not pipeline_id:
        return jsonify({"error": "run_dir and pipeline_id are required"}), 400

    command = [
        sys.executable,
        "report_last_level_source_target_refs.py",
        "--run-dir",
        run_dir,
        "--pipeline",
        pipeline_id,
        "--from-last-level",
        str(from_last_level),
    ]
    try:
        runner = _launch_background_command(
            command,
            f"Starting UI report_last_level_source_target_refs | run_dir={run_dir} | pipeline={pipeline_id} | from_last_level={from_last_level}",
            extra_env={"ETL_RUN_ORIGIN": "ui-report-refs"},
            run_meta={"origin": "ui-report-refs", "pipeline_id": pipeline_id},
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "runner": runner})


@app.route("/sf-api/v1/health", methods=["GET"])
def sf_health():
    auth_error = _require_sf_api_auth()
    if auth_error:
        return auth_error
    runner = _runner_status()
    return jsonify({
        "ok": True,
        "service": "sf-etl-python-backend",
        "timestamp": _utc_now(),
        "runner": runner,
        "profiles": {
            "source": list_env_profiles("SOURCE"),
            "target": list_env_profiles("TARGET"),
        },
    })


@app.route("/sf-api/v1/runs", methods=["POST"])
def sf_start_run():
    auth_error = _require_sf_api_auth()
    if auth_error:
        return auth_error
    payload, status_code = _start_pipeline_run_request(request.json or {}, origin="sf")
    return jsonify(payload), status_code


@app.route("/sf-api/v1/runs/<run_key>/resume", methods=["POST"])
def sf_resume_run(run_key: str):
    """
    Transform/load only: reload the saved pipeline template from run audit for this run_key.
    Body: { "pipeline_id", "start_stage": "transform"|"load", optional: dry_run, fresh, source_profile, target_profile, ... }
    """
    auth_error = _require_sf_api_auth()
    if auth_error:
        return auth_error
    body = dict(request.json or {})
    body["resume_run_key"] = run_key
    payload, status_code = _start_pipeline_run_request(body, origin="sf")
    return jsonify(payload), status_code


@app.route("/sf-api/v1/runs/<run_key>/status", methods=["GET"])
def sf_run_status(run_key: str):
    auth_error = _require_sf_api_auth()
    if auth_error:
        return auth_error

    audit_row = RUN_AUDIT.get_run(run_key)
    if not audit_row:
        return jsonify({"error": f"Run '{run_key}' not found"}), 404

    try:
        runner = _runner_status()
        if not runner.get("running") and _state_has_running_pipeline():
            _mark_external_run_stopped()
        payload = _current_status_payload()
        payload_matches_run = _status_payload_matches_run(payload, run_key)
        effective_payload = payload if payload_matches_run else {
            "migration": audit_row.get("migration_name"),
            "run_key": run_key,
            "started_at": audit_row.get("started_at"),
            "last_updated": audit_row.get("last_updated_at"),
            "pipelines": {},
        }
        overall_status = _derive_run_status(
            effective_payload,
            runner if (runner.get("run_key") or "").strip() == run_key else {"running": False, "run_key": run_key},
        )
        if not payload_matches_run:
            stored_status = str(audit_row.get("status") or "").strip().upper()
            if stored_status in {"QUEUED", "RUNNING", "COMPLETED", "FAILED", "STOPPED"}:
                overall_status = stored_status
        error_message = None
        for record in (effective_payload.get("pipelines") or {}).values():
            if isinstance(record, dict) and (record.get("status") or "").upper() == "FAILED":
                error_message = record.get("error") or error_message
                if error_message:
                    break
        RUN_AUDIT.update_status(
            run_key,
            status=overall_status,
            runner=runner,
            status_payload=effective_payload,
            error_message=error_message,
        )
        refreshed_audit = RUN_AUDIT.get_run(run_key) or audit_row
        artifacts = _collect_run_artifacts(run_key, request.url_root.rstrip("/"))
        return jsonify({
            "ok": True,
            "run_key": run_key,
            "status": overall_status,
            "attempt_context": {
                "sf_pipeline_run_id": refreshed_audit.get("sf_pipeline_run_id"),
                "sf_pipeline_id": refreshed_audit.get("sf_pipeline_id"),
                "sf_pipeline_attempt": refreshed_audit.get("sf_pipeline_attempt"),
                "start_stage": refreshed_audit.get("start_stage"),
                "effective_from_pipeline": refreshed_audit.get("effective_from_pipeline"),
                "resumed_from_run_key": refreshed_audit.get("resumed_from_run_key"),
            },
            "audit": refreshed_audit,
            "runner": runner,
            "state": effective_payload,
            "artifacts": artifacts,
            "state_matches_run": payload_matches_run,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/sf-api/v1/runs/<run_key>/artifacts/<root_name>/<path:artifact_path>", methods=["GET"])
def sf_download_artifact(run_key: str, root_name: str, artifact_path: str):
    auth_error = _require_sf_api_auth()
    if auth_error:
        return auth_error

    if root_name not in {"exports", "logs"}:
        return jsonify({"error": "Unsupported artifact root"}), 400

    run_dir = _resolve_sf_run_dir(PROJECT_ROOT / root_name, run_key)
    if not run_dir:
        return jsonify({"error": f"Artifact run '{run_key}' not found"}), 404

    candidate = (run_dir / artifact_path).resolve()
    if run_dir not in candidate.parents or not candidate.exists() or not candidate.is_file():
        return jsonify({"error": "Artifact not found"}), 404

    return send_file(candidate, as_attachment=True, download_name=candidate.name)


@app.route("/sf-api/v1/runs/<run_key>/artifacts/<root_name>/<path:artifact_path>", methods=["PUT"])
def sf_upload_artifact(run_key: str, root_name: str, artifact_path: str):
    """Replace an artifact file in place (same path/name) for resume / manual corrections."""
    auth_error = _require_sf_api_auth()
    if auth_error:
        return auth_error

    if root_name not in {"exports", "logs"}:
        return jsonify({"error": "Unsupported artifact root"}), 400

    run_dir = _resolve_sf_run_dir(PROJECT_ROOT / root_name, run_key)
    if not run_dir:
        return jsonify({"error": f"Artifact run '{run_key}' not found"}), 404

    candidate = (run_dir / artifact_path).resolve()
    if run_dir not in candidate.parents:
        return jsonify({"error": "Invalid artifact path"}), 400

    candidate.parent.mkdir(parents=True, exist_ok=True)
    payload = request.get_data() or b""
    candidate.write_bytes(payload)
    return jsonify({"ok": True, "path": str(candidate), "size": len(payload)})


@app.route("/sf-api/v1/runs/<run_key>/stop", methods=["POST"])
def sf_stop_run(run_key: str):
    auth_error = _require_sf_api_auth()
    if auth_error:
        return auth_error
    payload, status_code = _stop_pipeline_run_request(origin="sf", expected_run_key=run_key)
    return jsonify(payload), status_code


@app.route("/api/pipeline/logs", methods=["GET"])
def get_pipeline_logs():
    try:
        run_key = (request.args.get("run_key") or "").strip() or None
        log_path = _run_log_path(run_key) if run_key else Path((_active_run_meta.get("log_path") or PIPELINE_RUN_LOG))
        if not log_path.exists():
            return jsonify({
                "log_path": str(log_path),
                "content": "",
                "size": 0,
            })

        try:
            max_bytes = int(request.args.get("max_bytes", 40000))
        except ValueError:
            max_bytes = 40000
        max_bytes = max(1000, min(max_bytes, 200000))

        raw = log_path.read_bytes()
        tail = raw[-max_bytes:]
        text = tail.decode("utf-8", errors="replace")
        return jsonify({
            "log_path": str(log_path),
            "content": text,
            "size": len(raw),
            "truncated": len(raw) > len(tail),
        })
    except Exception as exc:
        logger.exception("Pipeline log read failed")
        return jsonify({"error": str(exc), "content": "", "size": 0}), 500


@app.route("/api/pipeline/logs", methods=["DELETE"])
def clear_pipeline_logs():
    try:
        run_key = (request.args.get("run_key") or "").strip() or None
        log_path = _run_log_path(run_key) if run_key else Path((_active_run_meta.get("log_path") or PIPELINE_RUN_LOG))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("", encoding="utf-8")
        return jsonify({
            "ok": True,
            "message": "Pipeline logs cleared",
            "log_path": str(log_path),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _resolve_recorded_artifact(stored_path: str) -> Path | None:
    """Resolve a success or failed CSV stored under the engine logs or Migration Result."""
    raw = Path(str(stored_path or "").strip())
    if not str(raw):
        return None
    candidate = raw.resolve() if raw.is_absolute() else (PROJECT_ROOT / raw).resolve()
    if not candidate.is_file():
        return None
    roots = [
        (PROJECT_ROOT / "logs").resolve(),
        (PROJECT_ROOT / "exports").resolve(),
        MIGRATION_RESULT_DIR.resolve(),
    ]
    if any(candidate == root or root in candidate.parents for root in roots):
        return candidate
    return None


@app.route(
    "/api/pipeline/runs/<run_key>/failed-files/<pipeline_id>/<object_name>",
    methods=["GET"],
)
def download_run_object_failed_file(run_key: str, pipeline_id: str, object_name: str):
    """Download the failed CSV recorded for one object in one immutable run."""
    payload = _current_status_payload(run_key)
    pipeline = (payload.get("pipelines") or {}).get(pipeline_id)
    if not isinstance(pipeline, dict):
        abort(404)
    object_record = (pipeline.get("objects") or {}).get(object_name)
    if not isinstance(object_record, dict):
        abort(404)
    failed_file = str(object_record.get("failed_file") or "").strip()
    if not failed_file:
        abort(404)

    candidate = _resolve_recorded_artifact(failed_file)
    if candidate is None:
        abort(404)
    return send_file(candidate, as_attachment=True, download_name=candidate.name)


@app.route(
    "/api/pipeline/runs/<run_key>/success-files/<pipeline_id>/<object_name>",
    methods=["GET"],
)
def download_run_object_success_file(run_key: str, pipeline_id: str, object_name: str):
    """Download the successful-results CSV recorded for one immutable run object."""
    payload = _current_status_payload(run_key)
    pipeline = (payload.get("pipelines") or {}).get(pipeline_id)
    if not isinstance(pipeline, dict):
        abort(404)
    object_record = (pipeline.get("objects") or {}).get(object_name)
    if not isinstance(object_record, dict):
        abort(404)
    success_file = str(object_record.get("success_file") or "").strip()
    if not success_file:
        abort(404)

    candidate = _resolve_recorded_artifact(success_file)
    if candidate is None:
        abort(404)
    return send_file(candidate, as_attachment=True, download_name=candidate.name)


@app.route(
    "/api/pipeline/runs/<run_key>/xfm-files/<pipeline_id>/<object_name>",
    methods=["GET"],
)
def download_run_object_xfm_file(run_key: str, pipeline_id: str, object_name: str):
    """Download the transformed xfm CSV for one object in one immutable run."""
    payload = _current_status_payload(run_key)
    pipeline = (payload.get("pipelines") or {}).get(pipeline_id)
    if not isinstance(pipeline, dict):
        abort(404)

    artifact_dir = _resolve_sf_run_dir(PROJECT_ROOT / "exports", run_key)
    if not artifact_dir:
        abort(404)

    artifact_dir_name = str(pipeline.get("artifact_dir_name") or pipeline_id).strip() or pipeline_id
    candidate = (artifact_dir / artifact_dir_name / "xfm" / f"{object_name}.csv").resolve()
    exports_root = (PROJECT_ROOT / "exports").resolve()
    if exports_root not in candidate.parents or not candidate.is_file():
        abort(404)
    safe_object = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in object_name).strip("._-") or "object"
    return send_file(
        candidate,
        as_attachment=True,
        mimetype="text/csv",
        download_name=f"{safe_object}_transformed.csv",
    )


@app.route("/api/pipeline/failed-files", methods=["GET"])
def list_failed_files():
    pipeline_id = (request.args.get("pipeline_id") or "").strip()
    if not pipeline_id:
        return jsonify({"error": "pipeline_id is required"}), 400

    run_name = (request.args.get("run_name") or "").strip() or None
    run_dir, log_dirs = _pipeline_log_dirs(pipeline_id, run_name)
    if not run_dir:
        return jsonify({
            "run_name": None,
            "pipeline_id": pipeline_id,
            "directories": [],
            "files": [],
        })

    files = []
    directories = []
    for log_dir in log_dirs:
        if not log_dir.exists():
            continue
        directories.append(str(log_dir))
        for path in log_dir.glob("*.csv"):
            files.append({
                "name": path.name,
                "path": str(path),
                "object_name": path.name.split("_failed_", 1)[0].split("_retry_failed_", 1)[0].split("_blocked_", 1)[0],
                "size": path.stat().st_size,
                "modified_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
                "stage": log_dir.name,
            })
    files.sort(key=lambda entry: entry.get("modified_at", ""), reverse=True)

    return jsonify({
        "run_name": run_dir.name,
        "pipeline_id": pipeline_id,
        "directories": directories,
        "files": files,
    })


@app.route("/api/pipeline/retry-failed", methods=["POST"])
def retry_failed_files():
    current_runner = _runner_status()
    if current_runner.get("running"):
        return jsonify({"error": "A pipeline run is already in progress", "runner": current_runner}), 409

    data = request.json or {}
    file_paths = data.get("files") or []
    pipeline_id = (data.get("pipeline_id") or "").strip() or None
    if not isinstance(file_paths, list) or not file_paths:
        return jsonify({"error": "files must be a non-empty list"}), 400

    logs_root = (PROJECT_ROOT / "logs").resolve()
    validated_paths: list[str] = []
    for raw_path in file_paths:
        try:
            path = Path(str(raw_path)).resolve()
        except Exception:
            return jsonify({"error": f"Invalid file path: {raw_path}"}), 400
        if logs_root not in path.parents or path.suffix.lower() != ".csv" or not path.exists():
            return jsonify({"error": f"Invalid failed CSV path: {raw_path}"}), 400
        validated_paths.append(str(path))

    try:
        runner = _launch_background_command(
            [
                sys.executable,
                "cli.py",
                "retry-failed",
                "--config",
                str(CONFIG_PATH),
                *(["--pipeline-id", pipeline_id] if pipeline_id else []),
                *[
                    arg
                    for path in validated_paths
                    for arg in ("--file", path)
                ],
            ],
            f"Starting UI-triggered failed-file reprocess | files={len(validated_paths)}",
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "ok": True,
        "message": f"Retry started for {len(validated_paths)} failed file(s)",
        "runner": runner,
    })


@app.route("/api/templates/<template_id>", methods=["GET"])
def get_template(template_id: str):
    path = TEMPLATE_DIR / f"{template_id}.json"
    if not path.exists():
        return jsonify({"error": "Template not found"}), 404
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return jsonify(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/templates/<template_id>", methods=["DELETE"])
def delete_template(template_id: str):
    normalized_id = _slugify_template_name(template_id)
    if normalized_id != template_id:
        return jsonify({"error": "Invalid template id"}), 400

    path = (TEMPLATE_DIR / f"{template_id}.json").resolve()
    template_root = TEMPLATE_DIR.resolve()
    if path.parent != template_root:
        return jsonify({"error": "Invalid template path"}), 400
    if not path.exists():
        return jsonify({"error": "Template not found"}), 404

    try:
        path.unlink()
        return jsonify({"ok": True, "template_id": template_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/templates", methods=["POST"])
def save_template():
    data = request.json or {}
    template_name = (data.get("template_name") or "").strip()
    template_data = data.get("template")
    template_id = (data.get("template_id") or "").strip()

    if not template_name or not template_data:
        return jsonify({"error": "template_name and template are required"}), 400

    TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    template_id = template_id or _slugify_template_name(template_name)
    input_mode = _normalize_input_mode(data.get("input_mode"))
    source_profile = str(data.get("source_profile") or template_data.get("org_profile") or "default").strip() or "default"
    target_profile = str(data.get("target_profile") or "default").strip() or "default"
    domains = _resolve_template_org_domains(
        input_mode=input_mode,
        source_profile=source_profile,
        target_profile=target_profile,
        source_domain=data.get("source_domain"),
        target_domain=data.get("target_domain"),
    )
    # Prefer live profile login URLs on save so new templates get durable stamps.
    live_source = _profile_login_domain("SOURCE", source_profile) if input_mode != "flat_file" else ""
    live_target = _profile_login_domain("TARGET", target_profile)
    payload = {
        "template_id": template_id,
        "template_name": template_name,
        "template": template_data,
        "input_mode": input_mode,
        "template_kind": str(data.get("template_kind") or "").strip().lower(),
        "source_profile": source_profile,
        "target_profile": target_profile,
        "source_domain": live_source or domains["source_domain"],
        "target_domain": live_target or domains["target_domain"],
        "prevalidation_status": str(data.get("prevalidation_status") or "DRAFT").strip() or "DRAFT",
    }
    path = TEMPLATE_DIR / f"{template_id}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return jsonify({"ok": True, "template_id": template_id, "template_name": template_name})


def _merge_object_selections(selections: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}

    for sel in selections:
        object_name = sel["object"]
        entry = merged.setdefault(
            object_name,
            {
                "name": object_name,
                "external_id": (sel.get("external_id") or "").strip(),
                "operation": str(sel.get("operation") or "").strip(),
                "match_by": str(sel.get("match_by") or "").strip(),
                "source_key_field": str(sel.get("source_key_field") or "").strip(),
                "_fields": set(),
                "transforms": [],
                "lookup_mappings": {},
                "record_type_mappings": {},
                "record_type_resolution": "developer_name",
                "extensions": {},
            },
        )

        for field_name in sel.get("selected_fields", []) or []:
            entry["_fields"].add(field_name)

        external_id = sel.get("external_id")
        if external_id and not entry.get("external_id"):
            entry["external_id"] = external_id

        operation = str(sel.get("operation") or "").strip()
        if operation and not entry.get("operation"):
            entry["operation"] = operation

        match_by = str(sel.get("match_by") or "").strip()
        if match_by and not entry.get("match_by"):
            entry["match_by"] = match_by

        source_key_field = str(sel.get("source_key_field") or "").strip()
        if source_key_field and not entry.get("source_key_field"):
            entry["source_key_field"] = source_key_field

        transforms = sel.get("transforms")
        if transforms and not entry["transforms"]:
            entry["transforms"] = [dict(rule) for rule in transforms if isinstance(rule, dict)]
        lookup_mappings = sel.get("lookup_mappings")
        if isinstance(lookup_mappings, dict):
            for field_name, mapping in lookup_mappings.items():
                if isinstance(mapping, dict) and field_name != "RecordTypeId":
                    entry["lookup_mappings"][field_name] = dict(mapping)
        record_type_mappings = sel.get("record_type_mappings")
        if isinstance(record_type_mappings, dict) and record_type_mappings and not entry["record_type_mappings"]:
            entry["record_type_mappings"] = {
                str(source): str(target)
                for source, target in record_type_mappings.items()
                if str(source or "").strip() and str(target or "").strip()
            }
        record_type_resolution = str(sel.get("record_type_resolution") or "").strip()
        if record_type_resolution:
            entry["record_type_resolution"] = record_type_resolution
        extensions = sel.get("extensions")
        if isinstance(extensions, dict) and extensions:
            entry["extensions"].update(copy.deepcopy(extensions))

    objects = []
    for entry in merged.values():
        obj_cfg = {
            "name": entry["name"],
        }
        if entry["external_id"]:
            obj_cfg["external_id"] = entry["external_id"]
        if entry["operation"]:
            obj_cfg["operation"] = entry["operation"]
        if entry["match_by"]:
            obj_cfg["match_by"] = entry["match_by"]
        if entry["source_key_field"]:
            obj_cfg["source_key_field"] = entry["source_key_field"]
        if entry["_fields"]:
            obj_cfg["fields"] = sorted(entry["_fields"])
        if entry["transforms"]:
            obj_cfg["transforms"] = entry["transforms"]
        if entry["lookup_mappings"]:
            obj_cfg["lookup_mappings"] = entry["lookup_mappings"]
        if entry.get("record_type_mappings"):
            obj_cfg["record_type_mappings"] = entry["record_type_mappings"]
        if entry.get("record_type_resolution"):
            obj_cfg["record_type_resolution"] = entry["record_type_resolution"]
        if entry.get("extensions"):
            obj_cfg["extensions"] = entry["extensions"]
        objects.append(obj_cfg)

    return objects


def _dedupe_nodes(nodes: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    seen_keys: set[tuple] = set()

    for node in nodes or []:
        node = dict(node)
        node["query_filter"] = _compose_query_parts(
            node.get("where_clause", ""),
            node.get("order_by", ""),
            node.get("limit", ""),
            node.get("offset", ""),
        ) or node.get("query_filter", "")
        node.pop("where_clause", None)
        node.pop("order_by", None)
        node.pop("limit", None)
        node.pop("offset", None)
        if node.get("direction") == "seed":
            key = ("seed", node.get("object"))
        else:
            key = (
                node.get("parent_id"),
                node.get("direction"),
                node.get("object"),
                node.get("via_field"),
            )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(node)

    return deduped


def _compose_query_parts(where_clause: str, order_by: str, limit: str | int, offset: str | int) -> str:
    clauses: list[str] = []
    where_text = (where_clause or "").strip()
    order_text = (order_by or "").strip()
    limit_text = _normalize_non_negative_int(limit)
    offset_text = _normalize_non_negative_int(offset)

    if where_text:
        clauses.append(f"WHERE {where_text}")
    if order_text:
        clauses.append(f"ORDER BY {order_text}")
    if limit_text:
        clauses.append(f"LIMIT {limit_text}")
    if offset_text:
        clauses.append(f"OFFSET {offset_text}")
    return " ".join(clauses)


def _normalize_non_negative_int(value: str | int) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = int(text)
    except (TypeError, ValueError):
        return ""
    return str(parsed) if parsed >= 0 else ""


def _build_scoped_filters(nodes: list[dict], seed_object: str, seed_filter: str) -> dict[str, str]:
    if not nodes:
        return {}

    node_by_id = {node["id"]: node for node in nodes}
    clauses_by_object: dict[str, list[str]] = {}
    cache: dict[str, str] = {}

    def node_tail(node_id: str) -> str:
        if node_id in cache:
            return cache[node_id]

        node = node_by_id[node_id]
        direction = node.get("direction")
        parent_id = node.get("parent_id")

        if direction == "seed" or not parent_id:
            tail = _normalize_soql_tail(seed_filter)
            cache[node_id] = tail
            return tail

        parent = node_by_id[parent_id]
        parent_tail = node_tail(parent_id)
        via_field = node.get("via_field")

        if direction == "child":
            if not parent_tail:
                tail = ""
            else:
                subquery = _build_subquery(parent["object"], "Id", parent_tail)
                tail = f"WHERE {via_field} IN ({subquery})" if via_field else ""
        elif direction == "parent":
            if not parent_tail:
                tail = ""
            else:
                subquery = _build_subquery(parent["object"], via_field or "Id", parent_tail)
                tail = f"WHERE Id IN ({subquery})"
        else:
            tail = ""

        cache[node_id] = tail
        return tail

    for node in nodes:
        if node.get("direction") == "seed":
            continue
        if node.get("object") == seed_object:
            continue
        tail = node_tail(node["id"])
        if tail:
            clauses_by_object.setdefault(node["object"], [])
            if tail not in clauses_by_object[node["object"]]:
                clauses_by_object[node["object"]].append(tail)

    scoped_filters = {}
    for object_name, tails in clauses_by_object.items():
        if not tails:
            continue
        clauses = [_strip_where(tail) for tail in tails if _strip_where(tail)]
        if not clauses:
            continue
        if len(clauses) == 1:
            scoped_filters[object_name] = f"WHERE {clauses[0]}"
        else:
            scoped_filters[object_name] = "WHERE " + " OR ".join(f"({clause})" for clause in clauses)
    return scoped_filters


def _build_subquery(object_name: str, select_field: str, tail: str) -> str:
    query = f"SELECT {select_field} FROM {object_name}"
    if tail:
        query += f" {tail}"
    return query


def _strip_where(value: str) -> str:
    text = (value or "").strip()
    if text[:5].upper() == "WHERE":
        return text[5:].strip()
    return text


def _ensure_where(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    return text if text[:5].upper() == "WHERE" else f"WHERE {text}"


def _compose_query_filter(generated_filter: str, explicit_filter: str) -> str:
    gen_where, gen_tail = _split_query_filter(generated_filter)
    exp_where, exp_tail = _split_query_filter(explicit_filter)

    where_parts = [part for part in (gen_where, exp_where) if part]
    tails = [part for part in (gen_tail, exp_tail) if part]

    merged = ""
    if where_parts:
        merged = "WHERE " + " AND ".join(f"({part})" for part in where_parts)
    if tails:
        merged = f"{merged} {' '.join(tails)}".strip()
    return merged


def _slugify_pipeline_snapshot_name(name: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_-]+", "_", (name or "").strip())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized[:80] or "pipeline_snapshot"


def _split_query_filter(value: str) -> tuple[str, str]:
    text = (value or "").strip()
    if not text:
        return "", ""

    where_idx = _find_top_level_keyword(text, "WHERE")
    order_idx = _find_top_level_keyword(text, "ORDER BY")
    limit_idx = _find_top_level_keyword(text, "LIMIT")
    offset_idx = _find_top_level_keyword(text, "OFFSET")

    clause_points = [idx for idx in (order_idx, limit_idx, offset_idx) if idx != -1]
    tail_start = min(clause_points) if clause_points else -1

    where_part = ""
    tail_part = ""

    if where_idx != -1:
        where_end = tail_start if tail_start > where_idx else len(text)
        where_part = text[where_idx + len("WHERE"):where_end].strip()
    elif tail_start == -1:
        where_part = text

    if tail_start != -1:
        tail_part = text[tail_start:].strip()

    return where_part, tail_part


def _normalize_soql_tail(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""

    where_idx = _find_top_level_keyword(text, "WHERE")
    if where_idx == -1:
        return ""

    tail = text[where_idx:].strip()
    end_markers = [
        _find_top_level_keyword(tail, "ORDER BY"),
        _find_top_level_keyword(tail, "LIMIT"),
        _find_top_level_keyword(tail, "OFFSET"),
    ]
    end_points = [idx for idx in end_markers if idx > 0]
    if end_points:
        tail = tail[:min(end_points)].rstrip()
    return tail


def _find_top_level_keyword(text: str, keyword: str) -> int:
    upper = text.upper()
    target = keyword.upper()
    depth = 0
    in_quote = False
    i = 0

    while i <= len(text) - len(target):
        char = text[i]

        if char == "'" and (i == 0 or text[i - 1] != "\\"):
            in_quote = not in_quote
            i += 1
            continue

        if not in_quote:
            if char == "(":
                depth += 1
            elif char == ")" and depth > 0:
                depth -= 1
            elif depth == 0 and upper[i:i + len(target)] == target:
                before_ok = i == 0 or upper[i - 1].isspace()
                after_idx = i + len(target)
                after_ok = after_idx >= len(text) or upper[after_idx].isspace()
                if before_ok and after_ok:
                    return i
        i += 1

    return -1


# ------------------------------------------------------------------ #
#  API — Auth check                                                   #
# ------------------------------------------------------------------ #

@app.route("/api/auth/check")
def auth_check():
    org = (request.args.get("org") or "").strip().lower()
    profile = (request.args.get("profile") or "default").strip().lower() or "default"
    mode = (request.args.get("mode") or "").strip().lower()

    if mode == "flat_file" and org == "source":
        return jsonify({
            "org": "source",
            "profile": profile,
            "status": "skipped",
            "message": "Source org connection is not required for flat-file migration.",
        })
    if mode == "flat_file" and not org:
        org = "target"

    if org in ("source", "target"):
        try:
            conn = get_connector(org, profile)
            return jsonify({
                "org": org,
                "profile": profile,
                "status": "ok",
                "instance_url": conn.instance_url,
                "auth_mode": _auth_mode_for_connector(conn),
            })
        except Exception as e:
            return jsonify({
                "org": org,
                "profile": profile,
                "status": "error",
                "message": str(e),
            })

    results = {}
    for current_org in ("source", "target"):
        try:
            conn = get_connector(current_org, "default")
            results[current_org] = {"status": "ok", "instance_url": conn.instance_url}
        except Exception as e:
            results[current_org] = {"status": "error", "message": str(e)}
    return jsonify(results)


# ------------------------------------------------------------------ #
#  API — Storage health / cleanup                                     #
# ------------------------------------------------------------------ #

def _storage_audit_rows(limit: int = 500) -> list[dict]:
    try:
        rows = list(RUN_AUDIT.list_runs(limit) or [])
        repaired = False
        for row in rows:
            if str(row.get("activity_type") or "").upper() != "STORAGE":
                continue
            if str(row.get("status") or "").upper() not in {"RUNNING", "QUEUED", "PENDING", "STARTING"}:
                continue
            try:
                created_at = datetime.fromisoformat(str(row.get("created_at") or "").replace("Z", "+00:00"))
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            if created_at >= _APP_STARTED_AT:
                continue
            RUN_AUDIT.update_status(
                str(row.get("run_key") or ""),
                status="FAILED",
                status_payload={"progress_percent": 100, "phase": "interrupted"},
                error_message="Storage operation was interrupted by an application restart; completion was not recorded.",
            )
            repaired = True
        return list(RUN_AUDIT.list_runs(limit) or []) if repaired else rows
    except Exception:
        return []


def _storage_health_payload() -> dict:
    audit_rows = _storage_audit_rows()
    return enrich_health_with_templates(
        PROJECT_ROOT,
        scan_storage_health(
            PROJECT_ROOT,
            audit_rows=audit_rows,
            active_run_keys=_storage_active_run_keys(),
        ),
        audit_rows=audit_rows,
    )


def _storage_active_run_keys() -> set[str]:
    _runner_status()
    keys = {str(key).strip() for key in _active_runs.keys() if str(key).strip()}
    for row in _storage_audit_rows():
        status = str(row.get("status") or "").upper()
        run_key = str(row.get("run_key") or "").strip()
        if run_key and status in {"RUNNING", "QUEUED", "PENDING", "STARTING"}:
            keys.add(run_key)
    return keys


def _start_storage_activity(operation: str, payload: dict) -> str:
    normalized = str(operation or "STORAGE").strip().upper() or "STORAGE"
    activity_key = f"storage_{normalized.lower()}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    RUN_AUDIT.upsert_submission(activity_key, {
        "activity_type": "STORAGE",
        "data_operation": normalized,
        "migration_name": f"Storage Health - {normalized.title()}",
        "target_profile": "local-backup",
        "status": "RUNNING",
        "request_payload_json": json.dumps(payload, ensure_ascii=False),
        "status_payload_json": json.dumps({"progress_percent": 5, "phase": "starting"}),
    })
    return activity_key


def _finish_storage_activity(activity_key: str, *, result: dict | None = None, error: Exception | None = None) -> None:
    if error is not None:
        RUN_AUDIT.update_status(
            activity_key,
            status="FAILED",
            status_payload={"progress_percent": 100, "phase": "failed"},
            error_message=str(error),
        )
        return
    payload = dict(result or {})
    payload.update({"progress_percent": 100, "phase": "completed"})
    RUN_AUDIT.update_status(activity_key, status="COMPLETED", status_payload=payload, error_message="")


def _storage_template_ids(body: dict) -> list[str]:
    values = body.get("template_ids") or []
    if not isinstance(values, list):
        return []
    return sorted({str(value).strip() for value in values if str(value).strip()})


@app.route("/api/storage/health", methods=["GET"])
def api_storage_health():
    try:
        payload = _storage_health_payload()
        return jsonify(payload)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/storage/dependencies", methods=["POST"])
def api_storage_dependencies():
    body = request.get_json(silent=True) or {}
    item_ids = body.get("item_ids") or []
    if not isinstance(item_ids, list) or not item_ids:
        return jsonify({"error": "item_ids must be a non-empty list"}), 400
    template_ids = _storage_template_ids(body)
    activity_key = _start_storage_activity("REVIEW", {
        "item_ids": item_ids,
        "template_ids": template_ids,
    })
    try:
        result = analyze_dependencies(
                PROJECT_ROOT,
                item_ids,
                audit_rows=_storage_audit_rows(),
                active_run_keys=_storage_active_run_keys(),
            )
        _finish_storage_activity(activity_key, result={
            "can_cleanup": bool(result.get("can_cleanup")),
            "blocker_count": len(result.get("blockers") or []),
            "warning_count": len(result.get("warnings") or []),
        })
        return jsonify({**result, "activity_key": activity_key})
    except Exception as exc:
        _finish_storage_activity(activity_key, error=exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/storage/archive-location", methods=["POST"])
def api_storage_archive_location():
    body = request.get_json(silent=True) or {}
    archive_root = str(body.get("archive_root") or "").strip()
    if not archive_root:
        return jsonify({"error": "archive_root is required"}), 400
    try:
        archive = save_archive_config(PROJECT_ROOT, archive_root)
        return jsonify({"ok": True, "archive": archive, "health": _storage_health_payload()})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/storage/archive", methods=["POST"])
def api_storage_archive():
    body = request.get_json(silent=True) or {}
    item_ids = body.get("item_ids") or []
    if not isinstance(item_ids, list) or not item_ids:
        return jsonify({"error": "item_ids must be a non-empty list"}), 400
    activity_key = _start_storage_activity("ARCHIVE", {
        "item_ids": item_ids,
        "template_ids": _storage_template_ids(body),
        "label": str(body.get("label") or "").strip() or None,
        "archive_root": str(body.get("archive_root") or "").strip() or None,
    })
    try:
        result = create_archive_package(
            PROJECT_ROOT,
            item_ids,
            include_related=bool(body.get("include_related", True)),
            label=str(body.get("label") or "").strip() or None,
            archive_root=str(body.get("archive_root") or "").strip() or None,
            audit_rows=_storage_audit_rows(),
            active_run_keys=_storage_active_run_keys(),
        )
        _finish_storage_activity(activity_key, result={
            "package_id": result.get("package_id"),
            "path": result.get("path"),
            "bytes": ((result.get("manifest") or {}).get("totals") or {}).get("bytes"),
        })
        return jsonify({**result, "activity_key": activity_key})
    except Exception as exc:
        _finish_storage_activity(activity_key, error=exc)
        return jsonify({"error": str(exc), "activity_key": activity_key}), 400


@app.route("/api/storage/archives", methods=["GET"])
def api_storage_archives():
    try:
        return jsonify(list_archive_packages(PROJECT_ROOT))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/storage/restore", methods=["POST"])
def api_storage_restore():
    body = request.get_json(silent=True) or {}
    package_id = str(body.get("package_id") or "").strip()
    if not package_id:
        return jsonify({"error": "package_id is required"}), 400
    if not bool(body.get("confirm")):
        return jsonify({"error": "Set confirm=true to restore into the working tree"}), 400
    activity_key = _start_storage_activity("RESTORE", {
        "package_id": package_id,
        "overwrite": bool(body.get("overwrite")),
    })
    try:
        result = restore_archive_package(
            PROJECT_ROOT,
            package_id,
            overwrite=bool(body.get("overwrite")),
        )
        refreshed = _storage_health_payload()
        _finish_storage_activity(activity_key, result={
            "package_id": package_id,
            "restored_count": len(result.get("restored") or []),
            "skipped_count": len(result.get("skipped") or []),
        })
        return jsonify({**result, "health": refreshed, "activity_key": activity_key})
    except Exception as exc:
        _finish_storage_activity(activity_key, error=exc)
        return jsonify({"error": str(exc), "activity_key": activity_key}), 400


@app.route("/api/storage/cleanup", methods=["POST"])
def api_storage_cleanup():
    body = request.get_json(silent=True) or {}
    item_ids = body.get("item_ids") or []
    confirm = bool(body.get("confirm"))
    if not isinstance(item_ids, list) or not item_ids:
        return jsonify({"error": "item_ids must be a non-empty list"}), 400
    if not confirm:
        return jsonify({"error": "Set confirm=true after reviewing selected items"}), 400
    archive_package_id = str(body.get("archive_package_id") or "").strip()
    skip_backup = bool(body.get("skip_backup"))
    bypass_confirmed = bool(body.get("bypass_confirmed"))
    if not archive_package_id and not (skip_backup and bypass_confirmed):
        return jsonify({"error": "Link an archive package or explicitly confirm archive bypass before cleanup"}), 400
    activity_key = _start_storage_activity("CLEANUP", {
        "item_ids": item_ids,
        "template_ids": _storage_template_ids(body),
        "archive_package_id": archive_package_id,
        "skip_backup": skip_backup,
        "bypass_confirmed": bypass_confirmed,
    })
    try:
        result = cleanup_with_backup_gate(
            PROJECT_ROOT,
            item_ids,
            confirm=True,
            archive_package_id=archive_package_id,
            skip_backup=skip_backup,
            audit_rows=_storage_audit_rows(),
            active_run_keys=_storage_active_run_keys(),
        )
        _finish_storage_activity(activity_key, result={
            "archive_package_id": result.get("archive_package_id"),
            "freed_bytes": result.get("freed_bytes"),
            "deleted_count": len(result.get("deleted") or []),
            "skipped_count": len(result.get("skipped") or []),
        })
        refreshed = _storage_health_payload()
        return jsonify({**result, "health": refreshed, "archive": load_archive_config(PROJECT_ROOT), "activity_key": activity_key})
    except Exception as exc:
        _finish_storage_activity(activity_key, error=exc)
        return jsonify({"error": str(exc), "activity_key": activity_key}), 400


if __name__ == "__main__":
    use_debug = True
    # Werkzeug's reloader parent must not run the scheduler; the child has WERKZEUG_RUN_MAIN=true.
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not use_debug:
        _start_pipeline_scheduler()
        start_result_publisher()
    app.run(debug=use_debug, port=ETL_PORT, use_reloader=True)
