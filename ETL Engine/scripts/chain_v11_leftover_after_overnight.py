"""Start the v11 leftover Case load after the overnight v12 then v13 chain finishes.

Loads only the 162,507 Cases that were not confirmed in the v11 id map.
Does not re-upsert the 399,813 already loaded records.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from chain_overnight_flat_file_stages import (  # noqa: E402
    DEFAULT_API,
    http_json,
    keep_awake,
    log,
    wait_until_idle,
)
from core.run_audit import RunAuditStore  # noqa: E402

SOURCE_RUN_KEY = "sf_run_20260817_144128_958749"
PIPELINE_ID = "Case_UAT_v11"
PIPELINE_DIR_NAME = "Case_UAT_v11_attempt_1"
MIGRATION_NAME = "Case_UAT_v11_MRT_v2_leftOver - Migration"
OVERNIGHT_LOG = PROJECT_ROOT / "logs" / "overnight_chain_v12_v13.log"
LEFTOVER_PAYLOAD = (
    PROJECT_ROOT
    / "exports"
    / f"run_sf_{SOURCE_RUN_KEY}"
    / PIPELINE_DIR_NAME
    / "leftover"
    / "Case_not_moved_payload.csv"
)
SOURCE_CONFIG = (
    PROJECT_ROOT / "state" / "run_configs" / f"run_sf_{SOURCE_RUN_KEY}" / "migration.yaml"
)


def wait_for_overnight_chain(watcher_log: Path, poll_seconds: int = 30) -> str:
    log(f"Waiting for overnight chain log: {OVERNIGHT_LOG}", watcher_log)
    while True:
        keep_awake()
        text = ""
        if OVERNIGHT_LOG.exists():
            text = OVERNIGHT_LOG.read_text(encoding="utf-8", errors="replace")
        if "Overnight chain complete." in text:
            log("Overnight v12/v13 chain completed.", watcher_log)
            return "complete"
        if "CHAIN FAILED:" in text:
            log("Overnight chain failed; leftover will still start once the API is idle.", watcher_log)
            return "failed"
        last = ""
        for line in reversed(text.splitlines()):
            if line.strip():
                last = line.strip()
                break
        log(f"Overnight chain still running. Last: {last or '(no log yet)'}", watcher_log)
        time.sleep(poll_seconds)


def new_run_key() -> str:
    return f"sf_run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"


def run_dir_name(run_key: str) -> str:
    return f"run_sf_{run_key}"


def register_leftover_audit(
    run_key: str,
    config_path: Path,
    state_path: Path,
    log_path: Path,
    *,
    status: str,
    returncode: int | None = None,
) -> None:
    """Record leftover CLI loads in run_audit so they appear on All Runs."""
    state = {}
    if state_path.exists():
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                state = payload
        except (OSError, ValueError):
            state = {}
    now = datetime.now(timezone.utc).isoformat()
    audit = RunAuditStore()
    record = {
        "activity_type": "MIGRATION",
        "data_operation": "UPSERT",
        "start_stage": "load",
        "resumed_from_run_key": SOURCE_RUN_KEY,
        "migration_name": MIGRATION_NAME,
        "source_profile": "default",
        "target_profile": "default",
        "status": status,
        "request_payload_json": json.dumps({
            "migration_name": MIGRATION_NAME,
            "start_stage": "load",
            "fresh": True,
            "dry_run": False,
            "source_profile": "default",
            "target_profile": "default",
            "pipelines": [{
                "pipeline_id": PIPELINE_ID,
                "pipeline_name": PIPELINE_ID,
                "input_mode": "flat_file",
                "flat_file_template_id": PIPELINE_ID,
                "seed_object": "Case",
            }],
        }, ensure_ascii=False),
        "status_payload_json": json.dumps(state, ensure_ascii=False) if state else None,
        "runner_json": json.dumps({
            "origin": "leftover-chain",
            "run_key": run_key,
            "running": status in {"QUEUED", "RUNNING"},
            "returncode": returncode,
            "log_path": str(log_path),
            "state_path": str(state_path),
            "config_path": str(config_path),
        }, ensure_ascii=False),
    }
    if status in {"QUEUED", "RUNNING"}:
        record["created_at"] = state.get("started_at") or now
        record["started_at"] = state.get("started_at") or now
    if status in {"COMPLETED", "FAILED"}:
        record["completed_at"] = state.get("last_updated") or now
    audit.upsert_submission(run_key, record)


def stage_leftover_xfm(run_key: str, watcher_log: Path) -> Path:
    if not LEFTOVER_PAYLOAD.exists():
        raise FileNotFoundError(f"Leftover payload not found: {LEFTOVER_PAYLOAD}")
    xfm_dir = PROJECT_ROOT / "exports" / run_dir_name(run_key) / PIPELINE_DIR_NAME / "xfm"
    xfm_dir.mkdir(parents=True, exist_ok=True)
    dest = xfm_dir / "Case.csv"
    log(f"Copying leftover Case payload ({LEFTOVER_PAYLOAD.stat().st_size:,} bytes) to {dest}", watcher_log)
    shutil.copy2(LEFTOVER_PAYLOAD, dest)
    return dest


def start_leftover_load(run_key: str, watcher_log: Path) -> int:
    if not SOURCE_CONFIG.exists():
        raise FileNotFoundError(f"v11 run config not found: {SOURCE_CONFIG}")
    config_dir = PROJECT_ROOT / "state" / "run_configs" / run_dir_name(run_key)
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "migration.yaml"
    shutil.copy2(SOURCE_CONFIG, config_path)

    state_path = PROJECT_ROOT / "state" / "run_states" / f"{run_dir_name(run_key)}.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = PROJECT_ROOT / "logs" / f"{run_dir_name(run_key)}.log"
    staging_db = PROJECT_ROOT / "state" / f"{run_dir_name(run_key)}_staging.duckdb"
    id_map_db = PROJECT_ROOT / "state" / f"{run_dir_name(run_key)}_id_map.duckdb"

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["ETL_RUN_KEY"] = run_key
    env["ETL_RUN_DIR_NAME"] = run_dir_name(run_key)
    env["ETL_RUN_STATE_FILE"] = str(state_path)
    env["ETL_STAGING_DB_PATH"] = str(staging_db)
    env["ETL_ID_MAP_DB_PATH"] = str(id_map_db)
    env["ETL_MIGRATION_NAME"] = MIGRATION_NAME
    env["ETL_RUN_ORIGIN"] = "leftover-chain"

    command = [
        sys.executable,
        "cli.py",
        "load",
        "--config",
        str(config_path),
        "--pipeline",
        PIPELINE_ID,
    ]
    log(f"Starting leftover load {run_key} | {MIGRATION_NAME}", watcher_log)
    log(f"Progress: /pipeline/runs/{run_key}", watcher_log)
    register_leftover_audit(run_key, config_path, state_path, log_path, status="QUEUED")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n[{datetime.now(timezone.utc).isoformat()}] Starting leftover Case load | run_key={run_key}\n")
        handle.flush()
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=env,
        )
        log(f"Leftover load pid={process.pid}", watcher_log)
        while process.poll() is None:
            keep_awake()
            time.sleep(30)
            log(f"Leftover load still running pid={process.pid}", watcher_log)
        returncode = int(process.returncode or 0)
        register_leftover_audit(
            run_key,
            config_path,
            state_path,
            log_path,
            status="FAILED" if returncode else "COMPLETED",
            returncode=returncode,
        )
        return returncode


def write_launch_record(run_key: str, returncode: int) -> None:
    leftover_dir = LEFTOVER_PAYLOAD.parent
    leftover_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "migration_name": MIGRATION_NAME,
        "source_run_key": SOURCE_RUN_KEY,
        "leftover_run_key": run_key,
        "progress_url": f"/pipeline/runs/{run_key}",
        "returncode": returncode,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    (leftover_dir / "leftover_launch.json").write_text(json.dumps(record, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument(
        "--log",
        default=str(PROJECT_ROOT / "logs" / "overnight_chain_v11_leftover.log"),
    )
    args = parser.parse_args()
    watcher_log = Path(args.log)
    keep_awake()
    log(f"Armed leftover chain for {MIGRATION_NAME} after v12 and v13.", watcher_log)
    wait_for_overnight_chain(watcher_log)
    time.sleep(15)
    wait_until_idle(args.api, watcher_log)
    run_key = new_run_key()
    stage_leftover_xfm(run_key, watcher_log)
    returncode = start_leftover_load(run_key, watcher_log)
    write_launch_record(run_key, returncode)
    if returncode:
        log(f"Leftover load failed rc={returncode} run_key={run_key}", watcher_log)
        return returncode
    log(f"Leftover load completed run_key={run_key}", watcher_log)
    return 0


if __name__ == "__main__":
    log_path = PROJECT_ROOT / "logs" / "overnight_chain_v11_leftover.log"
    try:
        raise SystemExit(main())
    except Exception as exc:
        log(f"LEFTOVER CHAIN FAILED: {exc}", log_path)
        raise
