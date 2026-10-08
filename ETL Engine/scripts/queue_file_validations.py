"""Queue file-validation folders behind an already-running validation job."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


TERMINAL_STATUSES = {"COMPLETED", "FAILED", "STOPPED", "CANCELLED", "CANCELED"}
POLL_SECONDS = 30


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_log(log_path: Path, message: str) -> None:
    line = f"{timestamp()} {message}"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")


def request_json(base_url: str, path: str, payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=body,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def get_with_retry(base_url: str, path: str, log_path: Path) -> dict:
    while True:
        try:
            return request_json(base_url, path)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            append_log(log_path, f"Server/status request unavailable; retrying: {exc}")
            time.sleep(POLL_SECONDS)


def wait_for_terminal(base_url: str, activity_key: str, log_path: Path) -> dict:
    last_summary = None
    while True:
        result = get_with_retry(base_url, f"/api/validation/runs/{activity_key}", log_path)
        status = str(result.get("status") or "UNKNOWN").upper()
        summary = (status, result.get("progress_percent"), result.get("progress_message"))
        if summary != last_summary:
            append_log(
                log_path,
                f"{activity_key} status={status} progress={result.get('progress_percent')} "
                f"message={result.get('progress_message') or ''}",
            )
            last_summary = summary
        if status in TERMINAL_STATUSES:
            return result
        time.sleep(POLL_SECONDS)


def submission_from_request(source_request: dict, folder: Path) -> dict:
    return {
        "file_source": "path",
        "file_path": str(folder),
        "object_name": source_request["object_name"],
        "source_id_column": source_request["source_id_column"],
        "target_match_field": source_request["target_match_field"],
        "reference_template_id": source_request.get("reference_template_id") or "",
        "ignore_fields": source_request.get("ignore_fields") or [],
        "exclude_fields": source_request.get("exclude_fields") or [],
        "include_fields": source_request.get("include_fields") or [],
        "lookup_mappings": source_request.get("lookup_mappings") or [],
        "file_query_mode": source_request.get("file_query_mode") or "object_template",
        "custom_soql": source_request.get("custom_soql") or "",
        "scan_mode": source_request.get("scan_mode") or "sample",
        "sample_size": source_request.get("sample_size") or 1000,
        "query_mode": source_request.get("query_mode") or "auto",
        "id_chunk_size": source_request.get("id_chunk_size") or 200,
    }


def apply_metadata_field_policy(
    base_url: str,
    source_request: dict,
    excluded_types: set[str],
    exclude_lookups: bool,
    log_path: Path,
) -> None:
    object_name = str(source_request["object_name"])
    encoded_object = urllib.parse.quote(object_name, safe="")
    metadata = get_with_retry(
        base_url,
        f"/api/object/{encoded_object}?org=target&profile=default",
        log_path,
    )
    fields = list(metadata.get("fields") or [])
    allowed = []
    excluded = []
    for field in fields:
        name = str(field.get("name") or "").strip()
        field_type = str(field.get("type") or "").strip().lower()
        if not name:
            continue
        if field_type in excluded_types or (exclude_lookups and field.get("is_lookup")):
            excluded.append(name)
        else:
            allowed.append(name)

    target_key = str(source_request["target_match_field"])
    if target_key not in allowed:
        allowed.insert(0, target_key)
    # Id is required for target retrieval but is never compared with the source key.
    if "Id" not in allowed:
        allowed.insert(0, "Id")
    allowed = list(dict.fromkeys(allowed))
    source_request["file_query_mode"] = "custom_soql"
    source_request["custom_soql"] = f"SELECT {', '.join(allowed)} FROM {object_name}"
    source_request["ignore_fields"] = []
    source_request["exclude_fields"] = []
    append_log(
        log_path,
        f"Metadata policy prepared: allowed={len(allowed)} excluded={len(excluded)} "
        f"excluded_types={sorted(excluded_types)} exclude_lookups={exclude_lookups}",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--current-run", required=True)
    parser.add_argument("--folder", action="append", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:5000")
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--exclude-field-types", default="")
    parser.add_argument("--exclude-lookups", action="store_true")
    args = parser.parse_args()

    folders = [Path(value).resolve() for value in args.folder]
    missing = [str(path) for path in folders if not path.is_dir()]
    if missing:
        append_log(args.log, f"Aborting; source folder(s) missing: {missing}")
        return 2

    original = get_with_retry(
        args.base_url, f"/api/validation/runs/{args.current_run}", args.log
    )
    source_request = original.get("request") or {}
    required = {"object_name", "source_id_column", "target_match_field"}
    absent = sorted(key for key in required if not source_request.get(key))
    if absent:
        append_log(args.log, f"Aborting; current run is missing configuration: {absent}")
        return 2

    excluded_types = {
        value.strip().lower()
        for value in args.exclude_field_types.split(",")
        if value.strip()
    }
    if excluded_types or args.exclude_lookups:
        apply_metadata_field_policy(
            args.base_url,
            source_request,
            excluded_types,
            args.exclude_lookups,
            args.log,
        )

    append_log(args.log, f"Queue initialized behind {args.current_run}; folders={folders}")
    current_key = args.current_run
    for folder in folders:
        finished = wait_for_terminal(args.base_url, current_key, args.log)
        append_log(
            args.log,
            f"Predecessor {current_key} finished with status={finished.get('status')}; "
            f"submitting {folder}",
        )
        payload = submission_from_request(source_request, folder)
        while True:
            try:
                submitted = request_json(args.base_url, "/api/validation/file-run", payload)
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                append_log(args.log, f"Submission deferred (HTTP {exc.code}): {detail}")
                time.sleep(POLL_SECONDS)
            except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
                append_log(args.log, f"Submission unavailable; retrying: {exc}")
                time.sleep(POLL_SECONDS)
        current_key = str(submitted["activity_key"])
        append_log(args.log, f"Submitted {folder} as {current_key}")

    final = wait_for_terminal(args.base_url, current_key, args.log)
    append_log(args.log, f"Queue finished; final run {current_key} status={final.get('status')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
