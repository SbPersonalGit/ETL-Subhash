"""Run pre-validation for a pipeline sequence using existing stage validators."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from core.flat_file import safe_name

ROOT = Path(__file__).resolve().parent
FLAT_FILE_TEMPLATE_DIR = ROOT / "config" / "flat_file_templates"
SOURCE_TEMPLATE_DIR = ROOT / "config" / "templates"


def _log(message: str) -> None:
    print(message, flush=True)


def _stage_template_path(stage: dict) -> Path:
    template_id = safe_name(str(stage.get("template_id") or "").strip())
    input_mode = str(stage.get("input_mode") or "").strip().lower()
    if input_mode == "flat_file":
      return FLAT_FILE_TEMPLATE_DIR / f"{template_id}.json"
    return SOURCE_TEMPLATE_DIR / f"{template_id}.json"


def _stage_command(stage: dict, out_dir: Path, request: dict | None = None) -> list[str]:
    template_path = _stage_template_path(stage)
    input_mode = str(stage.get("input_mode") or "").strip().lower()
    script_name = "prevalidate_files.py" if input_mode == "flat_file" else "prevalidate_source.py"
    command = [
        sys.executable,
        str(ROOT / script_name),
        "--template",
        str(template_path),
        "--out-dir",
        str(out_dir),
    ]
    request = request or {}
    if input_mode == "flat_file":
        scan_mode = str(request.get("scan_mode") or "full")
        command.extend([
            "--scan-mode", scan_mode,
            "--sample-size", str(request.get("sample_size") or 1000),
            "--sample-seed", str(request.get("sample_seed") or 42),
            "--query-mode", str(request.get("query_mode") or "auto"),
            "--salesforce-id-length", str(request.get("salesforce_id_length") or "15_or_18"),
        ])
        for check_id in stage.get("optional_checks") or []:
            command.extend(["--optional-check", str(check_id)])
        if set(stage.get("optional_checks") or []) & {"master_detail_references", "lookup_references"}:
            command.append("--target-aware")
    for object_name in stage.get("object_names") or []:
        command.extend(["--object", str(object_name)])
    return command


def _merge_stage_report(stage: dict, report: dict, combined: dict) -> None:
    stage_id = str(stage.get("pipeline_id") or stage.get("template_id") or "").strip()
    stage_name = str(stage.get("pipeline_name") or stage_id).strip() or stage_id
    stage_mode = str(stage.get("input_mode") or "").strip().lower() or "salesforce_source"
    for item in report.get("files") or []:
        original_object = str(item.get("object") or item.get("file") or stage_id).strip() or stage_id
        merged = dict(item)
        merged["object"] = f"{stage_name} :: {original_object}"
        merged["file"] = f"{stage_name} :: {str(item.get('file') or original_object).strip() or original_object}"
        merged["stage_id"] = stage_id
        merged["stage_name"] = stage_name
        merged["stage_input_mode"] = stage_mode
        merged["template_id"] = str(stage.get("template_id") or "").strip()
        combined["files"].append(merged)


def _write_pipeline_progress(out_dir: Path, payload: dict) -> None:
    body = {**payload, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    (out_dir / "prevalidation_progress.json").write_text(json.dumps(body, indent=2), encoding="utf-8")


def run(args) -> int:
    request_path = Path(args.request).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    request = json.loads(request_path.read_text(encoding="utf-8"))
    stages = request.get("stages") or []
    combined = {
        "template_id": request.get("pipeline_id") or "",
        "template_name": request.get("pipeline_name") or request.get("pipeline_id") or "Pipeline Validation",
        "target_aware": False,
        "files": [],
        "stages": [],
        "selected_optional_checks": request.get("optional_checks") or [],
        "salesforce_id_length": request.get("salesforce_id_length") or "15_or_18",
        "mandatory_checks": ["csv_structure", "scientific_notation", "migration_key_present", "record_type_mapping", "record_type_picklists"],
        "skipped_checks": [],
    }
    had_issues = False
    available_optional = {
        "duplicate_keys", "required_fields", "restricted_picklists", "dependent_picklists",
        "master_detail_references", "lookup_references", "text_limits", "numeric_precision",
        "salesforce_id_format", "contact_formats", "date_formats",
    }
    selected_optional = set(combined["selected_optional_checks"])
    combined["executed_checks"] = sorted(set(combined["mandatory_checks"]) | selected_optional)
    combined["skipped_checks"] = [
        {"id": check_id, "status": "SKIPPED_BY_USER"}
        for check_id in sorted(available_optional - selected_optional)
    ]

    _log(
        f"Pipeline pre-validation | pipeline={combined['template_name']} | stages={len(stages)} | reports={out_dir}"
    )
    _write_pipeline_progress(out_dir, {
        "status": "RUNNING",
        "phase": "starting",
        "total_stages": len(stages),
        "completed_stages": 0,
        "current_stage_index": 0,
        "current_stage": "",
        "message": f"Starting pipeline pre-validation ({len(stages)} stage(s))",
    })

    for index, stage in enumerate(stages, start=1):
        stage.setdefault("optional_checks", request.get("optional_checks") or [])
        stage_id = str(stage.get("pipeline_id") or stage.get("template_id") or "").strip()
        stage_name = str(stage.get("pipeline_name") or stage_id).strip() or stage_id
        template_path = _stage_template_path(stage)
        if not template_path.is_file():
            raise FileNotFoundError(f"Template not found for stage '{stage_name}': {template_path}")
        stage_out_dir = out_dir / f"{index:02d}_{safe_name(stage_id or stage_name)}"
        stage_out_dir.mkdir(parents=True, exist_ok=True)
        command = _stage_command(stage, stage_out_dir, request)
        _write_pipeline_progress(out_dir, {
            "status": "RUNNING",
            "phase": "stage",
            "total_stages": len(stages),
            "completed_stages": index - 1,
            "current_stage_index": index,
            "current_stage": stage_name,
            "stage_out_dir": str(stage_out_dir),
            "message": f"Running stage {index} of {len(stages)}: {stage_name}",
        })
        _log(f"[{index}/{len(stages)}] {stage_name} | START | mode={stage.get('input_mode')} | template={template_path.name}")
        stage_started = time.perf_counter()
        completed = subprocess.run(command, cwd=str(ROOT), check=False)
        stage_elapsed_seconds = round(time.perf_counter() - stage_started, 3)
        report_path = stage_out_dir / "prevalidation_report.json"
        stage_report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {
            "status": "ISSUES_FOUND" if completed.returncode else "PASSED",
            "files": [{
                "file": stage_name,
                "object": stage_name,
                "errors": [f"Stage validator did not create a report. Exit code: {completed.returncode}"],
                "field_access_issues": [],
                "file_validation": {"clean_count": 0, "rejected_count": 0, "normalized_count": 0},
                "target_checks": [],
            }],
        }
        stage_status = str(stage_report.get("status") or "").strip().upper() or ("PASSED" if completed.returncode == 0 else "ISSUES_FOUND")
        combined["stages"].append({
            "pipeline_id": stage_id,
            "pipeline_name": stage_name,
            "template_id": str(stage.get("template_id") or "").strip(),
            "input_mode": str(stage.get("input_mode") or "").strip().lower() or "salesforce_source",
            "status": stage_status,
            "out_dir": str(stage_out_dir),
            "elapsed_seconds": stage_elapsed_seconds,
        })
        _merge_stage_report(stage, stage_report, combined)
        if completed.returncode != 0 or stage_status != "PASSED":
            had_issues = True
        if str(request.get("scan_mode") or "full") == "sample":
            combined["status"] = "SAMPLE_ISSUES_FOUND" if had_issues else "SAMPLE_PASSED"
        else:
            combined["status"] = "ISSUES_FOUND" if had_issues else "PASSED"
        (out_dir / "prevalidation_report.json").write_text(json.dumps(combined, indent=2), encoding="utf-8")
        _write_pipeline_progress(out_dir, {
            "status": "RUNNING" if index < len(stages) else combined["status"],
            "phase": "stage_complete" if index < len(stages) else "completed",
            "total_stages": len(stages),
            "completed_stages": index,
            "current_stage_index": index,
            "current_stage": stage_name,
            "message": f"Finished stage {index} of {len(stages)}: {stage_name} ({stage_status})",
        })
        _log(f"[{index}/{len(stages)}] {stage_name} | DONE | status={stage_status} | elapsed={stage_elapsed_seconds:.2f}s")

    if not stages:
        combined["status"] = "ISSUES_FOUND"
        combined["files"].append({
            "file": "Pipeline",
            "object": "Pipeline",
            "errors": ["No pipeline stages were supplied for validation."],
            "field_access_issues": [],
            "file_validation": {"clean_count": 0, "rejected_count": 0, "normalized_count": 0},
            "target_checks": [],
        })
        (out_dir / "prevalidation_report.json").write_text(json.dumps(combined, indent=2), encoding="utf-8")
        _write_pipeline_progress(out_dir, {
            "status": "ISSUES_FOUND",
            "phase": "completed",
            "message": "No pipeline stages were supplied for validation.",
        })
        return 1

    _write_pipeline_progress(out_dir, {
        "status": combined["status"],
        "phase": "completed",
        "total_stages": len(stages),
        "completed_stages": len(stages),
        "message": f"Pipeline pre-validation complete ({combined['status']})",
    })
    _log(f"Pipeline pre-validation complete | status={combined['status']}")
    return 1 if had_issues else 0


def parse_args():
    parser = argparse.ArgumentParser(description="Run pre-validation for a pipeline sequence.")
    parser.add_argument("--request", required=True)
    parser.add_argument("--out-dir", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
