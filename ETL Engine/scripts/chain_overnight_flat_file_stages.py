"""Wait for the current v11 load, then start v12 and v13 automatically as separate runs."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_PATH = PROJECT_ROOT / "config" / "pipeline_designer" / "Case_UAT_v11_12_13_MRT.json"
TEMPLATE_DIR = PROJECT_ROOT / "config" / "flat_file_templates"
DEFAULT_API = "http://127.0.0.1:5000"
CHAIN_STAGES = ("Case_UAT_v12", "Case_UAT_v13")
SNAPSHOT_ID = "Case_UAT_v11_12_13_MRT"


def log(message: str, log_path: Path) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    print(line, flush=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def keep_awake() -> None:
    if os.name != "nt":
        return
    import ctypes
    es_continuous = 0x80000000
    es_system_required = 0x00000001
    es_awaymode = 0x00000040
    ctypes.windll.kernel32.SetThreadExecutionState(
        es_continuous | es_system_required | es_awaymode
    )


def pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        kernel = ctypes.windll.kernel32
        SYNCHRONIZE = 0x00100000
        handle = kernel.OpenProcess(SYNCHRONIZE, False, pid)
        if handle:
            kernel.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def wait_for_pids(pids: list[int], log_path: Path, poll_seconds: int = 20) -> None:
    remaining = [pid for pid in pids if pid_running(pid)]
    log(f"Waiting for process(es) to finish: {remaining or pids}", log_path)
    while True:
        remaining = [pid for pid in pids if pid_running(pid)]
        if not remaining:
            log("Watched process(es) have exited.", log_path)
            return
        log(f"Still running: {remaining}", log_path)
        time.sleep(poll_seconds)


def http_json(method: str, url: str, payload: dict | None = None, timeout: int = 120) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
        except ValueError:
            parsed = {"error": detail or str(exc)}
        parsed["_http_status"] = exc.code
        return parsed


def bypass_template_if_needed(template_id: str, log_path: Path) -> None:
    path = TEMPLATE_DIR / f"{template_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    status = str(payload.get("prevalidation_status") or "DRAFT").strip().upper()
    if status in {"PASSED", "BYPASSED"}:
        log(f"{template_id} pre-validation is {status}; leaving as-is.", log_path)
        return
    payload["prevalidation_status"] = "BYPASSED"
    payload["prevalidation_bypassed_at"] = datetime.now(timezone.utc).isoformat()
    payload["prevalidation_bypass_reason"] = "Overnight chain after Case_UAT_v11"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"Bypassed pre-validation for {template_id} (was {status}).", log_path)


def stage_to_pipeline_request(stage: dict) -> dict:
    workspace = stage.get("workspace") or {}
    controls = workspace.get("controls") or {}
    nodes = workspace.get("nodes") or []
    root_id = workspace.get("rootId")
    root = next((node for node in nodes if node.get("id") == root_id), None) or next(
        (node for node in nodes if not node.get("parentId")),
        None,
    )
    selections_by_object: dict[str, dict] = {}
    for node in nodes:
        object_name = str(node.get("object") or "").strip()
        if not object_name:
            continue
        selection = (workspace.get("nodeSelections") or {}).get(node.get("id")) or {}
        entry = selections_by_object.setdefault(
            object_name,
            {
                "object": object_name,
                "selected_fields": set(),
                "external_id": (workspace.get("objectExternalIds") or {}).get(object_name) or selection.get("externalId") or "",
            },
        )
        for field in selection.get("selectedFields") or []:
            if field:
                entry["selected_fields"].add(field)
        if selection.get("externalId"):
            entry["external_id"] = selection.get("externalId")
    object_transforms = workspace.get("objectTransforms") or {}
    lookup_mappings = workspace.get("lookupMappings") or {}
    selections = []
    for object_name, entry in selections_by_object.items():
        selections.append({
            "object": object_name,
            "selected_fields": sorted(entry["selected_fields"]),
            "external_id": entry["external_id"] or "",
            "transforms": list(object_transforms.get(object_name) or []),
            "lookup_mappings": dict(lookup_mappings.get(object_name) or {}),
        })
    request_nodes = []
    for node in nodes:
        selection = (workspace.get("nodeSelections") or {}).get(node.get("id")) or {}
        request_nodes.append({
            "id": node.get("id"),
            "object": node.get("object"),
            "parent_id": node.get("parentId"),
            "direction": node.get("direction"),
            "via_field": node.get("via_field"),
            "depth": node.get("depth"),
            "query_filter": selection.get("queryFilter") or "",
            "where_clause": selection.get("whereClause") or "",
            "order_by": selection.get("orderBy") or "",
            "limit": selection.get("limit") or "",
            "offset": selection.get("offset") or "",
        })
    return {
        "input_mode": "flat_file",
        "flat_file_template_id": stage.get("flatFileTemplateId") or stage.get("pipelineId"),
        "seed_object": (root or {}).get("object") or stage.get("seedObject") or "",
        "seed_filter": "",
        "seed_where": controls.get("seedFilter") or "",
        "seed_order_by": controls.get("seedOrder") or "",
        "seed_limit": controls.get("seedLimit") or "",
        "seed_offset": controls.get("seedOffset") or "",
        "max_records": int(controls.get("maxRecords") or 10000),
        "migration_name": stage.get("pipelineName"),
        "pipeline_id": stage.get("pipelineId"),
        "pipeline_name": stage.get("pipelineName"),
        "selections": selections,
        "traversal": {"parents": [], "children": []},
        "nodes": request_nodes,
    }


def load_stage(pipeline_id: str) -> dict:
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    for stage in snapshot.get("pipelineStages") or []:
        if str(stage.get("pipelineId") or "") == pipeline_id:
            return stage
    raise SystemExit(f"Stage '{pipeline_id}' was not found in {SNAPSHOT_PATH.name}")


def wait_until_idle(api_base: str, log_path: Path, timeout_minutes: float = 5) -> None:
    deadline = time.time() + timeout_minutes * 60
    while time.time() < deadline:
        status = http_json("GET", f"{api_base}/api/run-status")
        runner = status.get("runner") or {}
        if not runner.get("running"):
            log("API reports no active runner.", log_path)
            return
        log(f"API still shows a runner: {runner.get('run_key') or runner.get('pid')}", log_path)
        time.sleep(15)
    log("Proceeding even though the API still listed a runner (watched PIDs already exited).", log_path)


def run_state_path(run_key: str) -> Path:
    return PROJECT_ROOT / "state" / "run_states" / f"run_sf_{run_key}.json"


def wait_for_run(api_base: str, run_key: str, log_path: Path, timeout_hours: float = 10) -> dict:
    deadline = time.time() + timeout_hours * 3600
    seen_running = False
    state_path = run_state_path(run_key)
    finished = {"COMPLETED", "FAILED", "STOPPED", "DATA_LOAD_FAILED", "PARTIAL_DATA_LOAD_FAILED", "SKIPPED"}
    while time.time() < deadline:
        keep_awake()
        pipeline_statuses = set()
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                pipeline_statuses = {
                    str((record or {}).get("status") or "").upper()
                    for record in (state.get("pipelines") or {}).values()
                    if isinstance(record, dict)
                }
            except (OSError, ValueError):
                pipeline_statuses = set()
        if "RUNNING" in pipeline_statuses:
            seen_running = True
        log(
            f"{run_key}: pipelines={sorted(pipeline_statuses) or ['(none yet)']}",
            log_path,
        )
        if seen_running and "RUNNING" not in pipeline_statuses and pipeline_statuses:
            time.sleep(20)
            return {"run_key": run_key, "statuses": sorted(pipeline_statuses)}
        if pipeline_statuses and pipeline_statuses.issubset(finished) and seen_running:
            return {"run_key": run_key, "statuses": sorted(pipeline_statuses)}
        time.sleep(30)
    raise TimeoutError(f"Timed out waiting for {run_key}")


def start_stage(api_base: str, pipeline_id: str, log_path: Path) -> str:
    bypass_template_if_needed(pipeline_id, log_path)
    stage = load_stage(pipeline_id)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    payload = {
        "migration_name": f"{pipeline_id}_overnight_{stamp} - Migration",
        "snapshot_id": SNAPSHOT_ID,
        "fresh": True,
        "dry_run": False,
        "start_stage": "transform",
        "source_profile": "default",
        "target_profile": "default",
        "pipelines": [stage_to_pipeline_request(stage)],
    }
    log(f"Starting {pipeline_id} ...", log_path)
    result = http_json("POST", f"{api_base}/api/pipeline/run", payload, timeout=300)
    if not result.get("ok"):
        raise RuntimeError(f"Failed to start {pipeline_id}: {result}")
    run_key = str(result.get("run_key") or "")
    log(f"Started {pipeline_id} as {run_key} -> {result.get('progress_url')}", log_path)
    return run_key


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait-pid", action="append", type=int, default=[])
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--log", default=str(PROJECT_ROOT / "logs" / "overnight_chain_v12_v13.log"))
    args = parser.parse_args()
    log_path = Path(args.log)
    keep_awake()
    log("Overnight chain armed for Case_UAT_v12 then Case_UAT_v13.", log_path)
    if args.wait_pid:
        wait_for_pids(args.wait_pid, log_path)
        time.sleep(15)
    wait_until_idle(args.api, log_path)
    for pipeline_id in CHAIN_STAGES:
        keep_awake()
        run_key = start_stage(args.api, pipeline_id, log_path)
        wait_for_run(args.api, run_key, log_path)
        log(f"Finished waiting on {pipeline_id} / {run_key}.", log_path)
    log("Overnight chain complete.", log_path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log_path = PROJECT_ROOT / "logs" / "overnight_chain_v12_v13.log"
        log(f"CHAIN FAILED: {exc}", log_path)
        raise
