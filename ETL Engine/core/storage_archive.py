"""Archive, dependency analysis, and restore for Storage Health cleanup."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from core.storage_health import (
    cleanup_storage_items,
    extract_linked_run_key,
    format_bytes,
    item_id_for,
    path_size,
    scan_storage_health,
)


UNASSIGNED_TEMPLATE_ID = "_unassigned"
KEEP_CATEGORIES = {"templates", "flat_file_drop"}
CLEANABLE_DATA_CATEGORIES = {
    "validation_outputs",
    "flat_file_uploads",
    "file_validation_uploads",
    "duckdb",
    "exports",
    "run_logs",
    "temp",
    "other_state",
}
ACTIVE_RUN_STATUSES = {"RUNNING", "QUEUED", "PENDING", "STARTING"}
ARTIFACT_KIND_LABELS = {
    "templates": "Saved template",
    "flat_file_drop": "Original drop-folder files",
    "flat_file_uploads": "Uploaded mapping files",
    "file_validation_uploads": "File-validation copies",
    "validation_outputs": "Validation reports",
    "duckdb": "Finished-run working database",
    "exports": "Exported raw/transformed files",
    "run_logs": "Run logs and failed/success CSVs",
    "temp": "Temporary files",
    "other_state": "Other working files",
}


def _payload_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            payload = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}
    return {}


def _collect_known_template_ids(records: list[dict]) -> set[str]:
    known: set[str] = set()
    for record in records:
        template_id = str(record.get("template_id") or "").strip()
        name = str(record.get("name") or "").strip()
        if template_id:
            known.add(template_id)
        if name:
            known.add(name)
    return known


def _template_ids_from_mapping(value, known_ids: set[str]) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        token = value.strip()
        if token.startswith("flat:"):
            token = token.split(":", 1)[1].strip()
        if token in known_ids:
            found.add(token)
        return found
    if isinstance(value, dict):
        for key in (
            "template_id",
            "flat_file_template_id",
            "flatFileTemplateId",
            "reference_template_id",
            "id",
            "pipeline_id",
            "pipelineId",
        ):
            found.update(_template_ids_from_mapping(value.get(key), known_ids))
        found.update(_template_ids_from_mapping(value.get("input"), known_ids))
        found.update(_template_ids_from_mapping(value.get("pipelines"), known_ids))
        found.update(_template_ids_from_mapping(value.get("pipelineStages"), known_ids))
        return found
    if isinstance(value, list):
        for entry in value:
            found.update(_template_ids_from_mapping(entry, known_ids))
    return found


def _run_template_index(
    project_root: Path,
    audit_rows: Iterable[dict] | None,
    known_ids: set[str],
) -> dict[str, set[str]]:
    mapping: dict[str, set[str]] = {}

    def add(run_key: str | None, template_ids: Iterable[str]) -> None:
        key = str(run_key or "").strip()
        ids = {str(item).strip() for item in template_ids if str(item).strip() in known_ids}
        if not key or not ids:
            return
        mapping.setdefault(key, set()).update(ids)

    for row in audit_rows or []:
        if not isinstance(row, dict):
            continue
        run_key = str(row.get("run_key") or "").strip()
        payload = _payload_dict(row.get("request_payload_json"))
        found = _template_ids_from_mapping(payload, known_ids)
        found.update(_template_ids_from_mapping(row.get("sf_pipeline_id"), known_ids))
        found.update(_template_ids_from_mapping(row.get("migration_name"), known_ids))
        found.update(_template_ids_from_mapping(row.get("effective_from_pipeline"), known_ids))
        add(run_key, found)
        add(extract_linked_run_key(run_key or ""), found)

    root = Path(project_root).resolve()
    states_dir = root / "state" / "run_states"
    if states_dir.is_dir():
        for path in states_dir.glob("*.json"):
            payload = _read_json(path) or {}
            run_key = str(payload.get("run_key") or "").strip() or extract_linked_run_key(path.stem)
            found = set()
            for pipeline_id in (payload.get("pipelines") or {}):
                found.update(_template_ids_from_mapping(pipeline_id, known_ids))
            add(run_key, found)
            add(extract_linked_run_key(path.stem), found)

    configs_dir = root / "state" / "run_configs"
    if configs_dir.is_dir():
        for path in configs_dir.glob("*/migration.yaml"):
            run_key = extract_linked_run_key(path.parent.name)
            try:
                import yaml
                payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except Exception:
                payload = {}
            add(run_key, _template_ids_from_mapping(payload, known_ids))

    requests_dir = root / "state" / "validation_requests"
    if requests_dir.is_dir():
        for path in requests_dir.glob("*.json"):
            payload = _read_json(path) or {}
            run_key = str(payload.get("run_key") or path.stem).strip()
            add(run_key, _template_ids_from_mapping(payload, known_ids))

    return mapping


def _storage_workflow_history(
    project_root: Path,
    audit_rows: Iterable[dict] | None,
    known_ids: set[str],
) -> dict[str, dict[str, dict]]:
    """Return the latest durable Review/Archive/Cleanup result per template."""
    history: dict[str, dict[str, dict]] = {}
    for row in audit_rows or []:
        if not isinstance(row, dict) or str(row.get("activity_type") or "").upper() != "STORAGE":
            continue
        operation = str(row.get("data_operation") or "").strip().upper()
        if operation not in {"REVIEW", "ARCHIVE", "CLEANUP"}:
            continue
        payload = _payload_dict(row.get("request_payload_json"))
        template_ids = _template_ids_from_mapping(payload.get("template_ids"), known_ids)
        if not template_ids:
            # Compatibility for early archives whose label was the selected template list.
            label = str(payload.get("label") or "")
            template_ids = {template_id for template_id in known_ids if template_id and template_id in label}
        result = _payload_dict(row.get("status_payload_json"))
        timestamp = row.get("completed_at") or row.get("updated_at") or row.get("created_at")
        detail = {
            "status": str(row.get("status") or "UNKNOWN").upper(),
            "completed_at": timestamp,
            "run_key": row.get("run_key"),
            "package_id": result.get("package_id") or payload.get("archive_package_id"),
            "bypassed": bool(payload.get("bypassed") or payload.get("skip_backup")),
            "error": row.get("error_message"),
        }
        for template_id in template_ids:
            previous = (history.setdefault(template_id, {})).get(operation.lower())
            previous_at = str((previous or {}).get("completed_at") or "")
            if not previous or str(timestamp or "") >= previous_at:
                history[template_id][operation.lower()] = detail
    return history


def _active_template_runs(audit_rows: Iterable[dict] | None, known_ids: set[str]) -> dict[str, list[dict]]:
    active: dict[str, list[dict]] = {}
    for row in audit_rows or []:
        if not isinstance(row, dict) or str(row.get("status") or "").upper() not in ACTIVE_RUN_STATUSES:
            continue
        payload = _payload_dict(row.get("request_payload_json"))
        found = _template_ids_from_mapping(payload, known_ids)
        found.update(_template_ids_from_mapping(row.get("sf_pipeline_id"), known_ids))
        found.update(_template_ids_from_mapping(row.get("effective_from_pipeline"), known_ids))
        detail = {
            "run_key": row.get("run_key"),
            "status": str(row.get("status") or "").upper(),
            "activity_type": row.get("activity_type"),
            "migration_name": row.get("migration_name"),
        }
        for template_id in found:
            active.setdefault(template_id, []).append(detail)
    return active


def _annotate_items_with_templates(
    items: list[dict],
    records: list[dict],
    run_index: dict[str, set[str]],
) -> None:
    known_ids = _collect_known_template_ids(records)
    templates_by_batch = {}
    objects_by_template: dict[str, set[str]] = {}
    for record in records:
        if record.get("batch_id"):
            templates_by_batch.setdefault(record["batch_id"], []).append(record["template_id"])
        objects = objects_by_template.setdefault(record["template_id"], set())
        for file_cfg in record.get("files") or []:
            if isinstance(file_cfg, dict) and file_cfg.get("object"):
                objects.add(str(file_cfg["object"]))

    drop_owners: dict[str, set[str]] = {}
    for template_id, objects in objects_by_template.items():
        for object_name in objects:
            drop_owners.setdefault(object_name, set()).add(template_id)

    for item in items:
        owners: set[str] = set()
        template_id = str(item.get("template_id") or "").strip()
        if template_id in known_ids:
            owners.add(template_id)
        for dependent in item.get("dependent_templates") or []:
            dep_id = str((dependent or {}).get("template_id") or "").strip()
            if dep_id in known_ids:
                owners.add(dep_id)
        if item.get("category") == "flat_file_uploads":
            owners.update(templates_by_batch.get(item.get("name"), []))
        if item.get("category") == "flat_file_drop":
            owners.update(drop_owners.get(item.get("name") or "", set()))
        linked = str(item.get("linked_run_key") or "").strip()
        if linked:
            owners.update(run_index.get(linked) or set())
            owners.update(run_index.get(extract_linked_run_key(linked) or "") or set())
        name = str(item.get("name") or "")
        owners.update(run_index.get(name) or set())
        owners.update(run_index.get(extract_linked_run_key(name) or "") or set())
        relative = str(item.get("relative_path") or "")
        for token in relative.replace("\\", "/").split("/"):
            if token in known_ids:
                owners.add(token)
            owners.update(run_index.get(extract_linked_run_key(token) or "") or set())
        item["template_ids"] = sorted(owners)
        item["shared"] = len(owners) > 1
        item["kind_label"] = ARTIFACT_KIND_LABELS.get(item.get("category"), item.get("category_label") or item.get("category"))


def _group_item_summary(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "category": item.get("category"),
        "kind_label": item.get("kind_label") or ARTIFACT_KIND_LABELS.get(item.get("category"), item.get("category")),
        "name": item.get("name"),
        "relative_path": item.get("relative_path"),
        "size_bytes": int(item.get("size_bytes") or 0),
        "size_human": item.get("size_human") or format_bytes(int(item.get("size_bytes") or 0)),
        "cleanup_allowed": bool(item.get("cleanup_allowed")),
        "shared": bool(item.get("shared")),
        "template_ids": list(item.get("template_ids") or []),
        "linked_run_key": item.get("linked_run_key"),
        "run_status": item.get("run_status"),
        "recommendation": item.get("recommendation"),
        "protection_reason": item.get("protection_reason"),
    }


def build_template_groups(
    records: list[dict],
    pipeline_refs: list[dict],
    items: list[dict],
    active_template_runs: dict[str, list[dict]] | None = None,
    workflow_history: dict[str, dict[str, dict]] | None = None,
) -> list[dict]:
    active_template_runs = active_template_runs or {}
    workflow_history = workflow_history or {}
    refs_by_template: dict[str, list[dict]] = {}
    for ref in pipeline_refs:
        refs_by_template.setdefault(ref["template_id"], []).append(ref)

    items_by_template: dict[str, list[dict]] = {record["template_id"]: [] for record in records}
    unassigned: list[dict] = []
    for item in items:
        owners = [template_id for template_id in (item.get("template_ids") or []) if template_id in items_by_template]
        if not owners:
            unassigned.append(item)
            continue
        for template_id in owners:
            items_by_template[template_id].append(item)

    groups = []
    for record in records:
        template_id = record["template_id"]
        linked_items = items_by_template.get(template_id) or []
        pipelines = refs_by_template.get(template_id) or []
        objects = sorted({
            str(file_cfg.get("object") or "").strip()
            for file_cfg in (record.get("files") or [])
            if isinstance(file_cfg, dict) and file_cfg.get("object")
        })
        keep_items = [
            item for item in linked_items
            if item.get("category") in KEEP_CATEGORIES or not item.get("cleanup_allowed")
        ]
        unique_clean = [
            item for item in linked_items
            if item.get("category") in CLEANABLE_DATA_CATEGORIES
            and item.get("cleanup_allowed")
            and list(item.get("template_ids") or []) == [template_id]
        ]
        shared_clean = [
            item for item in linked_items
            if item.get("category") in CLEANABLE_DATA_CATEGORIES
            and item.get("cleanup_allowed")
            and len(item.get("template_ids") or []) > 1
        ]
        size_bytes = sum(int(item.get("size_bytes") or 0) for item in linked_items)
        reclaimable_bytes = sum(int(item.get("size_bytes") or 0) for item in unique_clean)
        shared_bytes = sum(int(item.get("size_bytes") or 0) for item in shared_clean)
        active_runs = list(active_template_runs.get(template_id) or [])
        active_status = str((active_runs[0] if active_runs else {}).get("status") or "").upper() or None
        dated = [item for item in linked_items if item.get("modified_at") or item.get("run_status")]
        dated.sort(key=lambda item: str(item.get("modified_at") or ""), reverse=True)
        last_status = (
            (dated[0].get("run_status") if dated else None)
            or record.get("prevalidation_status")
        )
        if pipelines:
            used_in = ", ".join(sorted({ref["pipeline_name"] for ref in pipelines}))
            recommendation = (
                f"Used in {used_in}. Cleaning removes old run/validation files for this template; "
                "the saved template and drop-folder source files stay."
            )
            protection_reason = f"Referenced by pipeline(s): {used_in}"
        else:
            recommendation = (
                "Not used by a pipeline design. Cleaning removes related run/validation files. "
                "The saved template itself is kept unless you delete it from Flat Files."
            )
            protection_reason = None
        if not unique_clean and shared_clean:
            recommendation += (
                f" {len(shared_clean)} shared run file(s) also belong to other templates "
                "— select those templates too to include them."
            )
        if active_runs:
            active_names = ", ".join(str(row.get("run_key") or "active run") for row in active_runs[:3])
            recommendation = f"Cleanup blocked while {active_names} is active. " + recommendation
            protection_reason = f"Active run(s): {active_names}"
        groups.append({
            "template_id": template_id,
            "name": record.get("name") or template_id,
            "kind": record.get("kind") or "flat_file",
            "created_at": record.get("created_at"),
            "objects": objects,
            "pipelines": [
                {
                    "pipeline_name": ref["pipeline_name"],
                    "pipeline_path": ref["pipeline_path"],
                    "stage_name": ref.get("stage_name"),
                }
                for ref in pipelines
            ],
            "prevalidation_status": record.get("prevalidation_status"),
            "last_status": last_status,
            "has_active_run": bool(active_runs),
            "active_status": active_status,
            "active_runs": active_runs,
            "workflow": workflow_history.get(template_id) or {},
            "size_bytes": size_bytes,
            "size_human": format_bytes(size_bytes),
            "reclaimable_bytes": reclaimable_bytes,
            "reclaimable_human": format_bytes(reclaimable_bytes),
            "shared_bytes": shared_bytes,
            "shared_human": format_bytes(shared_bytes),
            "cleanup_allowed": bool(unique_clean) and not active_runs,
            "protection_reason": protection_reason,
            "recommendation": recommendation,
            "item_ids": [] if active_runs else [item["id"] for item in unique_clean],
            "shared_item_ids": [item["id"] for item in shared_clean],
            "keep": [_group_item_summary(item) for item in keep_items],
            "can_clean": [_group_item_summary(item) for item in unique_clean],
            "shared": [_group_item_summary(item) for item in shared_clean],
        })

    groups.sort(key=lambda row: (-int(row.get("reclaimable_bytes") or 0), -int(row.get("size_bytes") or 0), row.get("name") or ""))

    if unassigned:
        unique_clean = [
            item for item in unassigned
            if item.get("category") in CLEANABLE_DATA_CATEGORIES and item.get("cleanup_allowed")
        ]
        keep_items = [item for item in unassigned if item not in unique_clean]
        size_bytes = sum(int(item.get("size_bytes") or 0) for item in unassigned)
        reclaimable_bytes = sum(int(item.get("size_bytes") or 0) for item in unique_clean)
        groups.append({
            "template_id": UNASSIGNED_TEMPLATE_ID,
            "name": "Other leftover files",
            "kind": "unassigned",
            "objects": [],
            "pipelines": [],
            "prevalidation_status": None,
            "last_status": None,
            "workflow": workflow_history.get(UNASSIGNED_TEMPLATE_ID) or {},
            "size_bytes": size_bytes,
            "size_human": format_bytes(size_bytes),
            "reclaimable_bytes": reclaimable_bytes,
            "reclaimable_human": format_bytes(reclaimable_bytes),
            "shared_bytes": 0,
            "shared_human": format_bytes(0),
            "cleanup_allowed": bool(unique_clean),
            "protection_reason": None,
            "recommendation": (
                "These files are not tied to a saved template. Review before cleaning."
            ),
            "item_ids": [item["id"] for item in unique_clean],
            "shared_item_ids": [],
            "keep": [_group_item_summary(item) for item in keep_items],
            "can_clean": [_group_item_summary(item) for item in unique_clean],
            "shared": [],
        })
    return groups


ARCHIVE_SCHEMA_VERSION = 1
DEFAULT_ARCHIVE_ROOT = "runtime/archive"
DEFAULT_RESTORE_ROOT = "runtime/restore"
DEFAULT_STAGING_ROOT = "runtime/package_staging"
BLOCKED_ARCHIVE_TOP_LEVEL = {
    "state",
    "logs",
    "exports",
    "config",
    "core",
    "ui",
    "tests",
    "temp",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_under(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _raw_archive_config(project_root: Path) -> dict:
    config_path = Path(project_root).resolve() / "config" / "archive.json"
    payload = {}
    if config_path.is_file():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            payload = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            payload = {}
    return payload


def _resolve_configured_path(project_root: Path, raw: str | None, default: str) -> Path:
    root = Path(project_root).resolve()
    value = str(raw or default).strip() or default
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return path


def _store_path_value(project_root: Path, path: Path) -> str:
    root = Path(project_root).resolve()
    resolved = path.resolve()
    if _is_under(root, resolved):
        return resolved.relative_to(root).as_posix()
    return str(resolved)


def resolve_archive_root(project_root: Path, raw_path: str | None) -> Path:
    return _resolve_configured_path(project_root, raw_path, DEFAULT_ARCHIVE_ROOT)


def validate_archive_root(project_root: Path, archive_root: Path) -> Path:
    root = Path(project_root).resolve()
    resolved = Path(archive_root).expanduser()
    if not resolved.is_absolute():
        resolved = root / resolved
    resolved = resolved.resolve()
    if resolved == root:
        raise ValueError("Backup folder cannot be the project root. Choose a dedicated folder.")
    if _is_under(root, resolved):
        top = resolved.relative_to(root).parts[0].lower() if resolved != root else ""
        if top in BLOCKED_ARCHIVE_TOP_LEVEL:
            raise ValueError(
                f"Backup folder cannot sit inside {top}/. "
                "Choose a folder outside this workspace, or use runtime/archive."
            )
    try:
        resolved.mkdir(parents=True, exist_ok=True)
        probe = resolved / ".orgshift_archive_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise ValueError(f"Cannot write to backup folder '{resolved}': {exc}") from exc
    return resolved


def load_archive_config(project_root: Path) -> dict:
    root = Path(project_root).resolve()
    payload = _raw_archive_config(root)
    archive_root = _resolve_configured_path(root, payload.get("archive_root"), DEFAULT_ARCHIVE_ROOT)
    restore_root = _resolve_configured_path(root, payload.get("restore_root"), DEFAULT_RESTORE_ROOT)
    staging_root = _resolve_configured_path(root, payload.get("package_staging_root"), DEFAULT_STAGING_ROOT)
    previous = []
    seen = {str(archive_root.resolve())}
    for raw in payload.get("previous_archive_roots") or []:
        text = str(raw or "").strip()
        if not text:
            continue
        resolved_prev = str(_resolve_configured_path(root, text, text).resolve())
        if resolved_prev in seen:
            continue
        previous.append(resolved_prev)
        seen.add(resolved_prev)
    resolved_archive = archive_root.resolve()
    return {
        "config_path": str((root / "config" / "archive.json").resolve()),
        "project_root": str(root),
        "archive_root": str(resolved_archive),
        "archive_root_stored": str(payload.get("archive_root") or DEFAULT_ARCHIVE_ROOT),
        "inside_workspace": _is_under(root, resolved_archive),
        "default_workspace_root": str((root / DEFAULT_ARCHIVE_ROOT).resolve()),
        "previous_archive_roots": previous,
        "restore_root": str(restore_root.resolve()),
        "package_staging_root": str(staging_root.resolve()),
    }


def save_archive_config(project_root: Path, archive_root: str) -> dict:
    root = Path(project_root).resolve()
    config_path = root / "config" / "archive.json"
    payload = _raw_archive_config(root)
    current = load_archive_config(root)
    resolved = validate_archive_root(root, Path(str(archive_root or "").strip() or DEFAULT_ARCHIVE_ROOT))
    stored = _store_path_value(root, resolved)
    previous = []
    seen = {str(resolved)}
    old = str(current.get("archive_root") or "").strip()
    if old and old not in seen:
        previous.append(_store_path_value(root, Path(old)))
        seen.add(old)
    for raw in payload.get("previous_archive_roots") or []:
        text = str(raw or "").strip()
        if not text:
            continue
        resolved_prev = str(_resolve_configured_path(root, text, text).resolve())
        if resolved_prev in seen:
            continue
        previous.append(text)
        seen.add(resolved_prev)
    payload["archive_root"] = stored
    payload["restore_root"] = payload.get("restore_root") or DEFAULT_RESTORE_ROOT
    payload["package_staging_root"] = payload.get("package_staging_root") or DEFAULT_STAGING_ROOT
    payload["previous_archive_roots"] = previous[:12]
    payload["updated_at"] = utc_now()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return load_archive_config(root)


def _read_json(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _filesystem_path(path: Path) -> Path:
    """Return a Windows extended-length path for filesystem operations."""
    resolved = str(Path(path).resolve())
    if os.name != "nt" or resolved.startswith("\\\\?\\"):
        return Path(resolved)
    if resolved.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + resolved[2:])
    return Path("\\\\?\\" + resolved)


def _copy_path(source: Path, destination: Path) -> None:
    """Copy a file or tree without the legacy Windows MAX_PATH restriction."""
    source_fs = _filesystem_path(source)
    destination_fs = _filesystem_path(destination)
    if source_fs.is_dir():
        shutil.copytree(source_fs, destination_fs)
    else:
        destination_fs.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_fs, destination_fs)


def _iter_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        return []
    return [child for child in path.rglob("*") if child.is_file()]


def _template_records(project_root: Path) -> list[dict]:
    root = Path(project_root).resolve()
    records = []
    for folder, kind in (
        (root / "config" / "flat_file_templates", "flat_file"),
        (root / "config" / "templates", "source"),
    ):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.json")):
            payload = _read_json(path) or {}
            template_id = str(payload.get("id") or path.stem).strip() or path.stem
            created_at = str(
                payload.get("created_at")
                or payload.get("createdAt")
                or ""
            ).strip()
            if not created_at:
                created_at = datetime.fromtimestamp(path.stat().st_ctime, timezone.utc).isoformat()
            records.append({
                "kind": kind,
                "template_id": template_id,
                "name": str(payload.get("name") or template_id),
                "path": path,
                "relative_path": path.relative_to(root).as_posix(),
                "batch_id": str(payload.get("batch_id") or "").strip() or None,
                "prevalidation_status": str(payload.get("prevalidation_status") or "").strip() or None,
                "created_at": created_at,
                "files": payload.get("files") if isinstance(payload.get("files"), list) else [],
                "payload": payload,
            })
    return records


def _pipeline_template_refs(project_root: Path) -> list[dict]:
    root = Path(project_root).resolve()
    folder = root / "config" / "pipeline_designer"
    refs = []
    if not folder.is_dir():
        return refs
    for path in sorted(folder.glob("*.json")):
        payload = _read_json(path) or {}
        pipeline_name = str(payload.get("name") or payload.get("id") or path.stem)
        for stage in payload.get("pipelineStages") or []:
            if not isinstance(stage, dict):
                continue
            template_id = str(
                stage.get("flatFileTemplateId")
                or stage.get("flat_file_template_id")
                or ""
            ).strip()
            template_token = str(stage.get("templateId") or stage.get("template_id") or "").strip()
            if template_token.startswith("flat:") and not template_id:
                template_id = template_token.split(":", 1)[1].strip()
            if not template_id and template_token and not template_token.startswith("flat:"):
                # Source-org style template ids may appear directly.
                template_id = template_token
            if not template_id:
                continue
            refs.append({
                "pipeline_path": path.relative_to(root).as_posix(),
                "pipeline_name": pipeline_name,
                "template_id": template_id,
                "stage_name": str(stage.get("pipelineName") or stage.get("templateName") or template_id),
            })
    return refs


def enrich_health_with_templates(
    project_root: Path,
    health: dict,
    *,
    audit_rows: Iterable[dict] | None = None,
) -> dict:
    """Attach template artifacts, grouping, and dependency hints into a health payload."""
    root = Path(project_root).resolve()
    items = list(health.get("items") or [])
    by_path = {item["relative_path"]: item for item in items}
    pipeline_refs = _pipeline_template_refs(root)
    refs_by_template: dict[str, list[dict]] = {}
    for ref in pipeline_refs:
        refs_by_template.setdefault(ref["template_id"], []).append(ref)
    records = _template_records(root)

    for record in records:
        relative = record["relative_path"]
        if relative in by_path:
            continue
        size = path_size(record["path"])
        linked_pipelines = refs_by_template.get(record["template_id"]) or []
        batch_path = (
            f"state/flat_file_uploads/{record['batch_id']}"
            if record.get("batch_id")
            else None
        )
        cleanup_allowed = not linked_pipelines
        protection_reason = None
        recommendation = "Archive before delete; pipelines and mappings may depend on this template"
        if linked_pipelines:
            names = ", ".join(sorted({ref["pipeline_name"] for ref in linked_pipelines})[:3])
            protection_reason = f"Referenced by pipeline(s): {names}"
            recommendation = "Keep or archive+update pipelines first — deleting breaks migration design"
        item = {
            "id": item_id_for(relative),
            "category": "templates",
            "category_label": "Templates (flat-file / source)",
            "name": record["name"],
            "relative_path": relative,
            "size_bytes": size,
            "size_human": format_bytes(size),
            "modified_at": None,
            "linked_run_key": None,
            "run_status": record.get("prevalidation_status"),
            "cleanup_allowed": cleanup_allowed,
            "protection_reason": protection_reason,
            "recommendation": recommendation,
            "template_id": record["template_id"],
            "batch_id": record.get("batch_id"),
            "batch_path": batch_path,
            "pipeline_refs": linked_pipelines,
        }
        items.append(item)

    templates_by_batch = {}
    for record in records:
        if record.get("batch_id"):
            templates_by_batch.setdefault(record["batch_id"], []).append(record)

    for item in items:
        if item.get("category") != "flat_file_uploads":
            continue
        batch_id = item.get("name")
        dependents = templates_by_batch.get(batch_id) or []
        if not dependents:
            continue
        names = ", ".join(sorted({row["name"] for row in dependents})[:3])
        item["dependent_templates"] = [
            {"template_id": row["template_id"], "name": row["name"], "relative_path": row["relative_path"]}
            for row in dependents
        ]
        if item.get("cleanup_allowed"):
            item["recommendation"] = (
                f"Archive first — used by template(s): {names}. "
                "Deleting without restore breaks remapping/revalidation."
            )

    known_ids = _collect_known_template_ids(records)
    run_index = _run_template_index(root, audit_rows, known_ids)
    _annotate_items_with_templates(items, records, run_index)
    template_groups = build_template_groups(
        records,
        pipeline_refs,
        items,
        _active_template_runs(audit_rows, known_ids),
        _storage_workflow_history(root, audit_rows, known_ids | {UNASSIGNED_TEMPLATE_ID}),
    )

    items = sorted(items, key=lambda row: (-int(row.get("size_bytes") or 0), row.get("relative_path") or ""))
    categories = list(health.get("categories") or [])
    template_items = [item for item in items if item.get("category") == "templates"]
    if template_items:
        categories.append({
            "category": "templates",
            "label": "Templates (flat-file / source)",
            "size_bytes": sum(int(item.get("size_bytes") or 0) for item in template_items),
            "size_human": format_bytes(sum(int(item.get("size_bytes") or 0) for item in template_items)),
            "item_count": len(template_items),
            "cleanup_allowed_bytes": sum(
                int(item.get("size_bytes") or 0) for item in template_items if item.get("cleanup_allowed")
            ),
            "cleanup_allowed_human": format_bytes(
                sum(int(item.get("size_bytes") or 0) for item in template_items if item.get("cleanup_allowed"))
            ),
            "cleanup_allowed_count": sum(1 for item in template_items if item.get("cleanup_allowed")),
        })
        categories = sorted(categories, key=lambda row: -int(row.get("size_bytes") or 0))

    totals = dict(health.get("totals") or {})
    totals["item_count"] = len(items)
    totals["tracked_bytes"] = sum(int(item.get("size_bytes") or 0) for item in items)
    totals["tracked_human"] = format_bytes(totals["tracked_bytes"])
    totals["reclaimable_bytes"] = sum(
        int(item.get("size_bytes") or 0) for item in items if item.get("cleanup_allowed")
    )
    totals["reclaimable_human"] = format_bytes(totals["reclaimable_bytes"])
    totals["cleanup_allowed_count"] = sum(1 for item in items if item.get("cleanup_allowed"))
    totals["template_count"] = sum(1 for row in template_groups if row.get("template_id") != UNASSIGNED_TEMPLATE_ID)
    totals["template_reclaimable_bytes"] = sum(int(row.get("reclaimable_bytes") or 0) for row in template_groups)
    totals["template_reclaimable_human"] = format_bytes(totals["template_reclaimable_bytes"])

    archive_cfg = load_archive_config(root)
    return {
        **health,
        "items": items,
        "categories": categories,
        "totals": totals,
        "templates": template_groups,
        "backup_strategy": {
            "mode": "archive_then_delete",
            "archive_root": archive_cfg.get("archive_root"),
            "restore_root": archive_cfg.get("restore_root"),
            "summary": (
                "Cleanup first copies selected files into a backup folder you choose, "
                "then deletes the working copies. Restore/rollback reads that backup package "
                "and puts files back. Prefer a folder outside this workspace so project cleanup "
                "cannot remove the backups."
            ),
            "steps": [
                "Set the backup folder (outside this workspace is recommended)",
                "Select the templates whose old run/validation files you no longer need",
                "Review impact — shared run files are included only when every related template is selected",
                "Archive copies files into the backup folder with a checksum manifest",
                "Clean deletes the working copies after the backup package contains them",
                "Restore from Backup packages if something was removed by mistake",
            ],
        },
        "archive": archive_cfg,
    }


def analyze_dependencies(
    project_root: Path,
    item_ids: Iterable[str],
    *,
    audit_rows: Iterable[dict] | None = None,
    active_run_keys: Iterable[str] | None = None,
) -> dict:
    root = Path(project_root).resolve()
    health = enrich_health_with_templates(
        root,
        scan_storage_health(root, audit_rows=audit_rows, active_run_keys=active_run_keys),
        audit_rows=audit_rows,
    )
    by_id = {item["id"]: item for item in health["items"]}
    wanted_ids = [str(item_id).strip() for item_id in item_ids if str(item_id).strip()]
    selected = [by_id[item_id] for item_id in wanted_ids if item_id in by_id]
    missing = [item_id for item_id in wanted_ids if item_id not in by_id]

    impacts: list[dict] = []
    blockers: list[dict] = []
    warnings: list[dict] = []
    related_paths: set[str] = set()

    templates = _template_records(root)
    templates_by_batch = {}
    for record in templates:
        if record.get("batch_id"):
            templates_by_batch.setdefault(record["batch_id"], []).append(record)
    pipeline_refs = _pipeline_template_refs(root)
    refs_by_template: dict[str, list[dict]] = {}
    for ref in pipeline_refs:
        refs_by_template.setdefault(ref["template_id"], []).append(ref)

    for item in selected:
        category = item.get("category")
        relative = item.get("relative_path") or ""
        related_paths.add(relative)

        if not item.get("cleanup_allowed"):
            blockers.append({
                "item_id": item["id"],
                "relative_path": relative,
                "severity": "blocker",
                "message": item.get("protection_reason") or "Item is protected from cleanup",
            })

        if category == "templates":
            template_id = item.get("template_id") or Path(relative).stem
            for ref in refs_by_template.get(template_id) or []:
                blockers.append({
                    "item_id": item["id"],
                    "relative_path": relative,
                    "severity": "blocker",
                    "message": (
                        f"Template '{template_id}' is used by pipeline "
                        f"'{ref['pipeline_name']}' ({ref['pipeline_path']}). "
                        "Removing it breaks pipeline mapping/prevalidation gates."
                    ),
                    "depends_on": ref["pipeline_path"],
                })
            batch_id = item.get("batch_id")
            if batch_id:
                batch_rel = f"state/flat_file_uploads/{batch_id}"
                related_paths.add(batch_rel)
                warnings.append({
                    "item_id": item["id"],
                    "relative_path": relative,
                    "severity": "warning",
                    "message": (
                        f"Template points at upload batch '{batch_id}'. "
                        "Keep/restore that batch to rematerialize clean/rejected CSVs."
                    ),
                    "related_path": batch_rel,
                })
            payload_files = []
            for record in templates:
                if record["relative_path"] == relative:
                    payload_files = record.get("files") or []
                    break
            for file_cfg in payload_files:
                if not isinstance(file_cfg, dict):
                    continue
                if str(file_cfg.get("source_type") or "") == "drop_folder":
                    source_path = str(file_cfg.get("source_path") or file_cfg.get("file") or "").replace("\\", "/")
                    object_name = str(file_cfg.get("object") or "").strip()
                    if object_name:
                        drop_rel = f"state/flat_file_drop/{object_name}"
                        related_paths.add(drop_rel)
                        warnings.append({
                            "item_id": item["id"],
                            "relative_path": relative,
                            "severity": "warning",
                            "message": (
                                f"Template references drop-folder source '{source_path}'. "
                                "Deleting drop files removes the original working tree input."
                            ),
                            "related_path": drop_rel,
                        })

        if category == "flat_file_uploads":
            batch_id = item.get("name")
            for record in templates_by_batch.get(batch_id) or []:
                related_paths.add(record["relative_path"])
                warnings.append({
                    "item_id": item["id"],
                    "relative_path": relative,
                    "severity": "warning",
                    "message": (
                        f"Batch is bound to template '{record['name']}' "
                        f"({record['relative_path']}). Deleting it leaves the template "
                        "unable to reload clean/rejected files for remapping."
                    ),
                    "related_path": record["relative_path"],
                })
                for ref in refs_by_template.get(record["template_id"]) or []:
                    warnings.append({
                        "item_id": item["id"],
                        "relative_path": relative,
                        "severity": "warning",
                        "message": (
                            f"Pipeline '{ref['pipeline_name']}' depends on template "
                            f"'{record['template_id']}', which uses this batch."
                        ),
                        "depends_on": ref["pipeline_path"],
                    })

        if category == "duckdb":
            name = item.get("name") or ""
            if name.endswith("_id_map.duckdb") or name == "id_map.duckdb":
                warnings.append({
                    "item_id": item["id"],
                    "relative_path": relative,
                    "severity": "warning",
                    "message": (
                        "ID-map DuckDB stores source→target identity mappings. "
                        "Deleting it breaks child-object remaps and relationship retries "
                        "for the linked run."
                    ),
                })
            if name.endswith("_staging.duckdb") or name == "staging.duckdb":
                warnings.append({
                    "item_id": item["id"],
                    "relative_path": relative,
                    "severity": "warning",
                    "message": (
                        "Staging DuckDB holds extracted/transformed rows. "
                        "Deleting it prevents resume/transform without re-extract."
                    ),
                })
            if relative.endswith(".wal"):
                blockers.append({
                    "item_id": item["id"],
                    "relative_path": relative,
                    "severity": "blocker",
                    "message": "DuckDB WAL is open/unfinished — close processes before archive/cleanup",
                })

        if category == "flat_file_drop":
            blockers.append({
                "item_id": item["id"],
                "relative_path": relative,
                "severity": "blocker",
                "message": "Drop-folder sources are protected; archive manually and confirm before any delete",
            })

        impacts.append({
            "item_id": item["id"],
            "category": category,
            "relative_path": relative,
            "cleanup_allowed": bool(item.get("cleanup_allowed")),
            "recommendation": item.get("recommendation"),
        })

    # Suggested related archive members (not automatically deleted).
    suggested = []
    for path in sorted(related_paths):
        match = next((item for item in health["items"] if item.get("relative_path") == path), None)
        if match and match["id"] not in wanted_ids:
            suggested.append({
                "id": match["id"],
                "relative_path": match["relative_path"],
                "category": match.get("category"),
                "size_human": match.get("size_human"),
                "reason": "Related dependency suggested for the same archive package",
            })

    return {
        "selected_count": len(selected),
        "missing_ids": missing,
        "impacts": impacts,
        "blockers": blockers,
        "warnings": warnings,
        "suggested_related_items": suggested,
        "can_cleanup": not blockers and all(item.get("cleanup_allowed") for item in selected) and not missing,
        "archive": load_archive_config(root),
    }


def _package_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"stor_pkg_{stamp}_{uuid.uuid4().hex[:8]}"


def create_archive_package(
    project_root: Path,
    item_ids: Iterable[str],
    *,
    include_related: bool = True,
    label: str | None = None,
    archive_root: str | None = None,
    audit_rows: Iterable[dict] | None = None,
    active_run_keys: Iterable[str] | None = None,
) -> dict:
    root = Path(project_root).resolve()
    if str(archive_root or "").strip():
        save_archive_config(root, str(archive_root).strip())
    cfg = load_archive_config(root)
    health = enrich_health_with_templates(
        root,
        scan_storage_health(root, audit_rows=audit_rows, active_run_keys=active_run_keys),
        audit_rows=audit_rows,
    )
    deps = analyze_dependencies(
        root,
        item_ids,
        audit_rows=audit_rows,
        active_run_keys=active_run_keys,
    )
    by_id = {item["id"]: item for item in health["items"]}
    selected_ids = {str(item_id).strip() for item_id in item_ids if str(item_id).strip()}
    if include_related:
        for row in deps.get("suggested_related_items") or []:
            selected_ids.add(row["id"])

    selected_items = [by_id[item_id] for item_id in selected_ids if item_id in by_id]
    if not selected_items:
        raise ValueError("No valid storage items selected for archive")

    package_id = _package_id()
    now = datetime.now(timezone.utc)
    package_dir = Path(cfg["archive_root"]) / f"{now:%Y}" / f"{now:%m}" / package_id
    staging_dir = Path(cfg["package_staging_root"]) / package_id
    payload_dir = staging_dir / "payload"
    if package_dir.exists() or staging_dir.exists():
        raise RuntimeError(f"Archive package path already exists for {package_id}")

    payload_dir.mkdir(parents=True, exist_ok=True)
    file_records = []
    copied_roots = []

    for item in selected_items:
        relative = str(item["relative_path"])
        source = (root / relative).resolve()
        if not _is_under(root, source):
            raise ValueError(f"Refusing to archive path outside project: {relative}")
        if not source.exists():
            raise FileNotFoundError(f"Missing artifact for archive: {relative}")
        if source.suffix.lower() == ".wal" or relative.endswith(".duckdb.wal"):
            raise ValueError(f"Refusing to archive open DuckDB WAL: {relative}")

        destination = payload_dir / relative
        _copy_path(source, destination)
        copied_roots.append(relative)
        payload_fs = _filesystem_path(payload_dir)
        for file_path in _iter_files(_filesystem_path(destination)):
            rel = file_path.relative_to(payload_fs).as_posix()
            file_records.append({
                "path": rel,
                "size": file_path.stat().st_size,
                "sha256": _sha256_file(file_path),
            })

    manifest = {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "package_id": package_id,
        "label": (label or "").strip() or package_id,
        "created_at": utc_now(),
        "project_root": str(root),
        "status": "PACKAGED",
        "template_ids": sorted({
            str(template_id)
            for item in selected_items
            for template_id in ([item.get("template_id")] + list(item.get("template_ids") or []))
            if str(template_id or "").strip()
        }),
        "items": [
            {
                "id": item["id"],
                "category": item.get("category"),
                "relative_path": item.get("relative_path"),
                "size_bytes": item.get("size_bytes"),
                "template_id": item.get("template_id"),
                "template_ids": list(item.get("template_ids") or []),
                "batch_id": item.get("batch_id"),
                "linked_run_key": item.get("linked_run_key"),
            }
            for item in selected_items
        ],
        "dependencies": {
            "blockers": deps.get("blockers") or [],
            "warnings": deps.get("warnings") or [],
        },
        "copied_roots": copied_roots,
        "files": file_records,
        "totals": {
            "item_count": len(selected_items),
            "file_count": len(file_records),
            "bytes": sum(int(row["size"]) for row in file_records),
            "bytes_human": format_bytes(sum(int(row["size"]) for row in file_records)),
        },
    }

    package_dir.mkdir(parents=True, exist_ok=True)
    _copy_path(payload_dir, package_dir / "payload")
    manifest_path = package_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    checksum_path = package_dir / "checksums.sha256"
    checksum_path.write_text(
        "\n".join(f"{row['sha256']}  {row['path']}" for row in file_records) + ("\n" if file_records else ""),
        encoding="utf-8",
    )
    shutil.rmtree(_filesystem_path(staging_dir), ignore_errors=True)

    catalog_path = Path(cfg["archive_root"]) / "catalog.json"
    catalog = _read_json(catalog_path) or {"packages": []}
    packages = catalog.get("packages") if isinstance(catalog.get("packages"), list) else []
    packages.insert(0, {
        "package_id": package_id,
        "label": manifest["label"],
        "created_at": manifest["created_at"],
        "path": str(package_dir),
        "relative_path": package_dir.relative_to(root).as_posix() if _is_under(root, package_dir) else str(package_dir),
        "item_count": manifest["totals"]["item_count"],
        "bytes": manifest["totals"]["bytes"],
        "bytes_human": manifest["totals"]["bytes_human"],
        "status": "PACKAGED",
    })
    catalog = {"updated_at": utc_now(), "packages": packages[:200]}
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text(json.dumps(catalog, indent=2, sort_keys=True), encoding="utf-8")

    return {
        "package_id": package_id,
        "path": str(package_dir),
        "manifest": manifest,
        "archive": cfg,
    }


def _archive_search_roots(cfg: dict) -> list[Path]:
    roots: list[Path] = []
    seen: set[str] = set()
    for raw in [cfg.get("archive_root"), *(cfg.get("previous_archive_roots") or [])]:
        text = str(raw or "").strip()
        if not text:
            continue
        path = Path(text)
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        roots.append(path)
    return roots


def list_archive_packages(project_root: Path) -> dict:
    root = Path(project_root).resolve()
    cfg = load_archive_config(root)
    discovered: dict[str, dict] = {}
    current_root = str(Path(cfg["archive_root"]).resolve())
    for archive_root in _archive_search_roots(cfg):
        catalog = _read_json(archive_root / "catalog.json") or {"packages": []}
        packages = catalog.get("packages") if isinstance(catalog.get("packages"), list) else []
        location = str(archive_root.resolve()) if archive_root.exists() else str(archive_root)
        for row in packages:
            if not isinstance(row, dict):
                continue
            package_id = str(row.get("package_id") or "").strip()
            if not package_id or package_id in discovered:
                continue
            discovered[package_id] = {
                **row,
                "archive_root": location,
                "current_location": location == current_root,
            }
        if archive_root.is_dir():
            for manifest_path in archive_root.glob("**/manifest.json"):
                manifest = _read_json(manifest_path) or {}
                package_id = str(manifest.get("package_id") or manifest_path.parent.name)
                if package_id in discovered:
                    continue
                discovered[package_id] = {
                    "package_id": package_id,
                    "label": manifest.get("label") or package_id,
                    "created_at": manifest.get("created_at"),
                    "path": str(manifest_path.parent),
                    "item_count": (manifest.get("totals") or {}).get("item_count"),
                    "bytes": (manifest.get("totals") or {}).get("bytes"),
                    "bytes_human": (manifest.get("totals") or {}).get("bytes_human"),
                    "status": manifest.get("status") or "PACKAGED",
                    "archive_root": location,
                    "current_location": location == current_root,
                }
    ordered = sorted(
        discovered.values(),
        key=lambda row: str(row.get("created_at") or ""),
        reverse=True,
    )
    return {"archive": cfg, "packages": ordered}


def _resolve_package_dir(project_root: Path, package_id: str) -> Path:
    root = Path(project_root).resolve()
    cfg = load_archive_config(root)
    package_id = str(package_id or "").strip()
    if not package_id or "/" in package_id or "\\" in package_id:
        raise ValueError("Invalid package_id")
    listing = list_archive_packages(root)
    for row in listing["packages"]:
        if row.get("package_id") == package_id and row.get("path"):
            path = Path(str(row["path"]))
            if path.is_dir():
                return path.resolve()
    matches = []
    for archive_root in _archive_search_roots(cfg):
        if archive_root.is_dir():
            matches.extend(archive_root.glob(f"**/{package_id}"))
    if not matches:
        raise FileNotFoundError(f"Archive package not found: {package_id}")
    return matches[0].resolve()


def restore_archive_package(
    project_root: Path,
    package_id: str,
    *,
    overwrite: bool = False,
) -> dict:
    root = Path(project_root).resolve()
    cfg = load_archive_config(root)
    package_dir = _resolve_package_dir(root, package_id)
    manifest = _read_json(package_dir / "manifest.json")
    if not manifest:
        raise FileNotFoundError(f"Archive manifest missing for {package_id}")
    payload_dir = package_dir / "payload"
    if not payload_dir.is_dir():
        raise FileNotFoundError(f"Archive payload missing for {package_id}")

    restored = []
    skipped = []
    errors = []
    for expected in manifest.get("files") or []:
        rel = str(expected.get("path") or "")
        source = (payload_dir / rel).resolve()
        source_fs = _filesystem_path(source)
        if not _is_under(payload_dir, source) or not source_fs.is_file():
            errors.append({"path": rel, "reason": "Missing archived file"})
            continue
        actual_hash = _sha256_file(source_fs)
        if actual_hash != expected.get("sha256"):
            errors.append({"path": rel, "reason": "Checksum mismatch in archive payload"})
            continue
        target = (root / rel).resolve()
        if not _is_under(root, target):
            errors.append({"path": rel, "reason": "Target escapes project root"})
            continue
        target_fs = _filesystem_path(target)
        if target_fs.exists() and not overwrite:
            skipped.append({"path": rel, "reason": "Already exists (set overwrite=true to replace)"})
            continue
        try:
            target_fs.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_fs, target_fs)
            restored.append({"path": rel, "size": target_fs.stat().st_size})
        except OSError as exc:
            errors.append({"path": rel, "reason": str(exc)})

    restore_note = {
        "package_id": package_id,
        "restored_at": utc_now(),
        "restored_count": len(restored),
        "skipped_count": len(skipped),
        "error_count": len(errors),
    }
    note_path = Path(cfg["restore_root"]) / package_id / "restore_result.json"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(json.dumps({**restore_note, "restored": restored, "skipped": skipped, "errors": errors}, indent=2), encoding="utf-8")

    return {
        "package_id": package_id,
        "package_path": str(package_dir),
        "restored": restored,
        "skipped": skipped,
        "errors": errors,
        "archive": cfg,
    }


def cleanup_with_backup_gate(
    project_root: Path,
    item_ids: Iterable[str],
    *,
    confirm: bool,
    archive_package_id: str | None = None,
    skip_backup: bool = False,
    audit_rows: Iterable[dict] | None = None,
    active_run_keys: Iterable[str] | None = None,
) -> dict:
    if not confirm:
        raise ValueError("Set confirm=true after reviewing dependency and backup choices")

    root = Path(project_root).resolve()
    deps = analyze_dependencies(
        root,
        item_ids,
        audit_rows=audit_rows,
        active_run_keys=active_run_keys,
    )
    if deps.get("blockers"):
        raise ValueError(
            "Cleanup blocked by dependencies. Review blockers, archive/update pipelines, then retry."
        )

    package_id = str(archive_package_id or "").strip() or None
    if not package_id and not skip_backup:
        raise ValueError(
            "Create an archive package first (archive_package_id) or explicitly set skip_backup=true"
        )

    if package_id:
        package_dir = _resolve_package_dir(root, package_id)
        manifest = _read_json(package_dir / "manifest.json") or {}
        archived_ids = {
            str(item.get("id") or "")
            for item in (manifest.get("items") or [])
            if isinstance(item, dict)
        }
        missing = [item_id for item_id in item_ids if str(item_id) not in archived_ids]
        if missing:
            raise ValueError(
                "Selected items are not all present in the archive package. "
                "Archive the same selection before cleanup."
            )

    # Templates live under config and are not deleted by the generic cleanup helper.
    health = enrich_health_with_templates(
        root,
        scan_storage_health(root, audit_rows=audit_rows, active_run_keys=active_run_keys),
        audit_rows=audit_rows,
    )
    by_id = {item["id"]: item for item in health["items"]}
    template_ids = []
    other_ids = []
    for item_id in item_ids:
        item = by_id.get(str(item_id))
        if not item:
            continue
        if item.get("category") == "templates":
            template_ids.append(item["id"])
        else:
            other_ids.append(item["id"])

    result = cleanup_storage_items(
        root,
        other_ids,
        audit_rows=audit_rows,
        active_run_keys=active_run_keys,
    ) if other_ids else {"deleted": [], "skipped": [], "errors": [], "freed_bytes": 0, "freed_human": "0 B"}

    for item_id in template_ids:
        item = by_id[item_id]
        path = (root / item["relative_path"]).resolve()
        if not item.get("cleanup_allowed"):
            result["skipped"].append({
                "id": item_id,
                "relative_path": item.get("relative_path"),
                "reason": item.get("protection_reason") or "Template protected",
            })
            continue
        if not _is_under(root, path) or not path.is_file():
            result["skipped"].append({
                "id": item_id,
                "relative_path": item.get("relative_path"),
                "reason": "Template missing or invalid path",
            })
            continue
        try:
            size = int(item.get("size_bytes") or path.stat().st_size)
            path.unlink()
            result["deleted"].append({
                "id": item_id,
                "relative_path": item.get("relative_path"),
                "size_bytes": size,
                "size_human": format_bytes(size),
            })
            result["freed_bytes"] = int(result.get("freed_bytes") or 0) + size
        except OSError as exc:
            result["errors"].append({
                "id": item_id,
                "relative_path": item.get("relative_path"),
                "reason": str(exc),
            })

    result["freed_human"] = format_bytes(int(result.get("freed_bytes") or 0))
    result["dependencies"] = deps
    result["archive_package_id"] = package_id
    result["skip_backup"] = bool(skip_backup and not package_id)
    return result
