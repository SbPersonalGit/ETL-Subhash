"""Local disk health scan and safe cleanup helpers for ETL artifacts."""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


ACTIVE_STATUSES = {
    "RUNNING",
    "QUEUED",
    "PENDING",
    "STARTING",
    "PACKAGING",
    "VERIFYING",
}

TERMINAL_STATUSES = {
    "COMPLETED",
    "FAILED",
    "STOPPED",
    "DATA_LOAD_FAILED",
    "PARTIAL_DATA_LOAD_FAILED",
    "VALIDATION_COMPLETED",
    "VALIDATION_FAILED",
    "PARTIALLY_VALIDATED",
    "BYPASSED",
}

PROTECTED_RELATIVE_PATHS = {
    "config",
    "state/run_audit.duckdb",
    "state/flat_file_drop/.gitkeep",
    "state/flat_file_drop/README.md",
}

CATEGORY_LABELS = {
    "validation_outputs": "Validation outputs (logs/verify)",
    "flat_file_uploads": "Flat-file upload batches",
    "file_validation_uploads": "File-validation upload copies",
    "duckdb": "DuckDB staging / ID maps",
    "exports": "Migration exports",
    "run_logs": "Run logs and result files",
    "temp": "Temporary folders",
    "flat_file_drop": "Drop-folder source CSVs",
    "other_state": "Other state files",
}


@dataclass(frozen=True)
class StorageItem:
    item_id: str
    category: str
    name: str
    relative_path: str
    absolute_path: str
    size_bytes: int
    modified_at: str | None
    linked_run_key: str | None
    run_status: str | None
    cleanup_allowed: bool
    protection_reason: str | None
    recommendation: str


def format_bytes(size: int) -> str:
    value = float(max(0, int(size or 0)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{int(size)} B"


def item_id_for(relative_path: str) -> str:
    digest = hashlib.sha1(relative_path.encode("utf-8")).hexdigest()[:16]
    return f"stor_{digest}"


def _utc_iso_from_mtime(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return None


def path_size(path: Path) -> int:
    try:
        if path.is_file():
            return int(path.stat().st_size)
        if not path.exists():
            return 0
        total = 0
        for child in path.rglob("*"):
            if child.is_file():
                try:
                    total += int(child.stat().st_size)
                except OSError:
                    continue
        return total
    except OSError:
        return 0


def _relative_posix(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _is_under(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def extract_linked_run_key(name: str) -> str | None:
    text = (name or "").strip()
    if not text:
        return None
    patterns = (
        r"^(?:run_sf_)*(sf_run_[A-Za-z0-9_-]+)",
        r"^(pipeline_prevalidation_[A-Za-z0-9_-]+)",
        r"^(validation_[A-Za-z0-9_-]+)",
        r"^(file_validation_[A-Za-z0-9_-]+)",
        r"^(prevalidation_[A-Za-z0-9_-]+)",
    )
    for pattern in patterns:
        match = re.match(pattern, text)
        if match:
            return match.group(1)
    if text.startswith("run_sf_"):
        return text[len("run_sf_") :]
    return None


def _status_map(audit_rows: Iterable[dict]) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for row in audit_rows or []:
        key = str(row.get("run_key") or "").strip()
        if not key:
            continue
        mapped[key] = str(row.get("status") or "UNKNOWN").upper()
    return mapped


def _decide_cleanup(
    *,
    category: str,
    relative_path: str,
    linked_run_key: str | None,
    run_status: str | None,
    active_run_keys: set[str],
) -> tuple[bool, str | None, str]:
    top = relative_path.split("/", 1)[0]
    if relative_path in PROTECTED_RELATIVE_PATHS or top == "config":
        return False, "Protected application file", "Keep — required by the application"

    if linked_run_key and linked_run_key in active_run_keys:
        return False, "Linked run is currently active", "Keep — active run"

    if run_status and run_status in ACTIVE_STATUSES:
        return False, f"Linked run status is {run_status}", "Keep — run still in progress"

    if category == "flat_file_drop":
        return False, "Source drop files require manual archive first", "Archive to backup location before deleting"

    if category == "duckdb" and relative_path.endswith("run_audit.duckdb"):
        return False, "Run audit must be retained", "Keep — audit history"

    if category in {
        "validation_outputs",
        "flat_file_uploads",
        "file_validation_uploads",
        "exports",
        "run_logs",
        "temp",
        "duckdb",
        "other_state",
    }:
        if run_status and run_status not in TERMINAL_STATUSES and run_status != "UNKNOWN":
            return False, f"Linked run status is {run_status}", "Review before cleanup"
        if linked_run_key and not run_status:
            return True, None, "Safe to clean if this activity is finished and reports are archived"
        return True, None, "Safe to clean after reports are archived"

    return False, "Unknown category", "Review manually"


def _make_item(
    *,
    root: Path,
    path: Path,
    category: str,
    status_by_run: dict[str, str],
    active_run_keys: set[str],
) -> StorageItem | None:
    if not path.exists():
        return None
    try:
        relative = _relative_posix(root, path)
    except ValueError:
        return None
    name = path.name
    linked = extract_linked_run_key(name)
    if not linked and category == "duckdb":
        # run_sf_<key>_staging.duckdb / _id_map.duckdb
        match = re.match(r"^(?:run_sf_)*(sf_run_[A-Za-z0-9_-]+?)_(?:staging|id_map)\.duckdb$", name)
        if match:
            linked = match.group(1)
        else:
            match = re.match(r"^run_sf_([A-Za-z0-9_-]+)_(?:staging|id_map)\.duckdb$", name)
            if match:
                linked = match.group(1)
    run_status = status_by_run.get(linked) if linked else None
    allowed, reason, recommendation = _decide_cleanup(
        category=category,
        relative_path=relative,
        linked_run_key=linked,
        run_status=run_status,
        active_run_keys=active_run_keys,
    )
    return StorageItem(
        item_id=item_id_for(relative),
        category=category,
        name=name,
        relative_path=relative,
        absolute_path=str(path.resolve()),
        size_bytes=path_size(path),
        modified_at=_utc_iso_from_mtime(path),
        linked_run_key=linked,
        run_status=run_status,
        cleanup_allowed=allowed,
        protection_reason=reason,
        recommendation=recommendation,
    )


def scan_storage_health(
    project_root: Path,
    *,
    audit_rows: Iterable[dict] | None = None,
    active_run_keys: Iterable[str] | None = None,
) -> dict:
    root = Path(project_root).resolve()
    status_by_run = _status_map(audit_rows or [])
    active_keys = {str(key).strip() for key in (active_run_keys or []) if str(key).strip()}

    items: list[StorageItem] = []

    verify_root = root / "logs" / "verify"
    if verify_root.is_dir():
        for child in sorted(verify_root.iterdir(), key=lambda p: p.name.lower()):
            if child.name.startswith("."):
                continue
            item = _make_item(
                root=root,
                path=child,
                category="validation_outputs",
                status_by_run=status_by_run,
                active_run_keys=active_keys,
            )
            if item:
                items.append(item)

    uploads_root = root / "state" / "flat_file_uploads"
    if uploads_root.is_dir():
        for child in sorted(uploads_root.iterdir(), key=lambda p: p.name.lower()):
            if child.name.startswith("."):
                continue
            item = _make_item(
                root=root,
                path=child,
                category="flat_file_uploads",
                status_by_run=status_by_run,
                active_run_keys=active_keys,
            )
            if item:
                items.append(item)

    file_validation_root = root / "state" / "file_validation_uploads"
    if file_validation_root.is_dir():
        for child in sorted(file_validation_root.iterdir(), key=lambda p: p.name.lower()):
            if child.name.startswith("."):
                continue
            item = _make_item(
                root=root,
                path=child,
                category="file_validation_uploads",
                status_by_run=status_by_run,
                active_run_keys=active_keys,
            )
            if item:
                items.append(item)

    drop_root = root / "state" / "flat_file_drop"
    if drop_root.is_dir():
        for child in sorted(drop_root.iterdir(), key=lambda p: p.name.lower()):
            if child.name.startswith(".") or child.name in {".gitkeep", "README.md"}:
                continue
            item = _make_item(
                root=root,
                path=child,
                category="flat_file_drop",
                status_by_run=status_by_run,
                active_run_keys=active_keys,
            )
            if item:
                items.append(item)

    state_dir = root / "state"
    if state_dir.is_dir():
        for path in sorted(state_dir.glob("*.duckdb")):
            if path.name == "run_audit.duckdb":
                item = _make_item(
                    root=root,
                    path=path,
                    category="duckdb",
                    status_by_run=status_by_run,
                    active_run_keys=active_keys,
                )
                if item:
                    items.append(
                        replace(
                            item,
                            cleanup_allowed=False,
                            protection_reason="Run audit must be retained",
                            recommendation="Keep — audit history",
                        )
                    )
                continue
            item = _make_item(
                root=root,
                path=path,
                category="duckdb",
                status_by_run=status_by_run,
                active_run_keys=active_keys,
            )
            if item:
                items.append(item)
        for path in sorted(state_dir.glob("*.duckdb.wal")):
            item = _make_item(
                root=root,
                path=path,
                category="duckdb",
                status_by_run=status_by_run,
                active_run_keys=active_keys,
            )
            if item:
                items.append(
                    replace(
                        item,
                        cleanup_allowed=False,
                        protection_reason="Open DuckDB WAL — close processes first",
                        recommendation="Keep until the related DuckDB file is closed",
                    )
                )

    exports_root = root / "exports"
    if exports_root.is_dir():
        for child in sorted(exports_root.iterdir(), key=lambda p: p.name.lower()):
            if child.name.startswith("."):
                continue
            item = _make_item(
                root=root,
                path=child,
                category="exports",
                status_by_run=status_by_run,
                active_run_keys=active_keys,
            )
            if item:
                items.append(item)

    logs_root = root / "logs"
    if logs_root.is_dir():
        for path in sorted(logs_root.iterdir(), key=lambda p: p.name.lower()):
            if not path.is_dir() or path.name.startswith(".") or path.name == "verify":
                continue
            if not (
                path.name.startswith("run_sf_")
                or path.name.startswith("sf_run_")
            ):
                continue
            item = _make_item(
                root=root,
                path=path,
                category="run_logs",
                status_by_run=status_by_run,
                active_run_keys=active_keys,
            )
            if item:
                items.append(item)
        for path in sorted(logs_root.glob("run_sf_*.log")):
            item = _make_item(
                root=root,
                path=path,
                category="run_logs",
                status_by_run=status_by_run,
                active_run_keys=active_keys,
            )
            if item:
                items.append(item)
        shared_log = logs_root / "ui_pipeline_run.log"
        if shared_log.exists():
            item = _make_item(
                root=root,
                path=shared_log,
                category="run_logs",
                status_by_run=status_by_run,
                active_run_keys=active_keys,
            )
            if item:
                items.append(item)
        sf_log = logs_root / "sf_etl.log"
        if sf_log.exists():
            item = _make_item(
                root=root,
                path=sf_log,
                category="run_logs",
                status_by_run=status_by_run,
                active_run_keys=active_keys,
            )
            if item:
                items.append(item)

    for temp_candidate in (root / "temp", root / "tmp"):
        if temp_candidate.exists():
            item = _make_item(
                root=root,
                path=temp_candidate,
                category="temp",
                status_by_run=status_by_run,
                active_run_keys=active_keys,
            )
            if item:
                items.append(item)
    if state_dir.is_dir():
        for path in sorted(state_dir.glob("tmp*")):
            item = _make_item(
                root=root,
                path=path,
                category="temp",
                status_by_run=status_by_run,
                active_run_keys=active_keys,
            )
            if item:
                items.append(item)

    categories: dict[str, dict] = {}
    for category, label in CATEGORY_LABELS.items():
        categories[category] = {
            "category": category,
            "label": label,
            "size_bytes": 0,
            "item_count": 0,
            "cleanup_allowed_bytes": 0,
            "cleanup_allowed_count": 0,
        }

    for item in items:
        bucket = categories.setdefault(
            item.category,
            {
                "category": item.category,
                "label": CATEGORY_LABELS.get(item.category, item.category),
                "size_bytes": 0,
                "item_count": 0,
                "cleanup_allowed_bytes": 0,
                "cleanup_allowed_count": 0,
            },
        )
        bucket["size_bytes"] += item.size_bytes
        bucket["item_count"] += 1
        if item.cleanup_allowed:
            bucket["cleanup_allowed_bytes"] += item.size_bytes
            bucket["cleanup_allowed_count"] += 1

    total_bytes = sum(item.size_bytes for item in items)
    reclaimable_bytes = sum(item.size_bytes for item in items if item.cleanup_allowed)

    try:
        usage = shutil.disk_usage(str(root))
        disk = {
            "total_bytes": int(usage.total),
            "used_bytes": int(usage.used),
            "free_bytes": int(usage.free),
            "total_human": format_bytes(usage.total),
            "used_human": format_bytes(usage.used),
            "free_human": format_bytes(usage.free),
            "used_percent": round((usage.used / usage.total) * 100, 1) if usage.total else 0.0,
        }
    except OSError:
        disk = {
            "total_bytes": 0,
            "used_bytes": 0,
            "free_bytes": 0,
            "total_human": "n/a",
            "used_human": "n/a",
            "free_human": "n/a",
            "used_percent": 0.0,
        }

    if disk["free_bytes"] and disk["free_bytes"] < 5 * 1024**3:
        health = "critical"
        health_message = "Disk free space is under 5 GB"
    elif reclaimable_bytes >= 2 * 1024**3 or disk.get("used_percent", 0) >= 90:
        health = "warning"
        health_message = "Large reclaimable ETL artifacts or high disk usage"
    else:
        health = "ok"
        health_message = "Local ETL artifact usage looks manageable"

    sorted_items = sorted(items, key=lambda item: (-item.size_bytes, item.relative_path))
    category_list = sorted(
        (
            {
                **bucket,
                "size_human": format_bytes(bucket["size_bytes"]),
                "cleanup_allowed_human": format_bytes(bucket["cleanup_allowed_bytes"]),
            }
            for bucket in categories.values()
            if bucket["item_count"] or bucket["size_bytes"]
        ),
        key=lambda row: -row["size_bytes"],
    )

    return {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(root),
        "health": health,
        "health_message": health_message,
        "disk": disk,
        "totals": {
            "tracked_bytes": total_bytes,
            "tracked_human": format_bytes(total_bytes),
            "reclaimable_bytes": reclaimable_bytes,
            "reclaimable_human": format_bytes(reclaimable_bytes),
            "item_count": len(sorted_items),
            "cleanup_allowed_count": sum(1 for item in sorted_items if item.cleanup_allowed),
        },
        "categories": category_list,
        "items": [
            {
                "id": item.item_id,
                "category": item.category,
                "category_label": CATEGORY_LABELS.get(item.category, item.category),
                "name": item.name,
                "relative_path": item.relative_path,
                "size_bytes": item.size_bytes,
                "size_human": format_bytes(item.size_bytes),
                "modified_at": item.modified_at,
                "linked_run_key": item.linked_run_key,
                "run_status": item.run_status,
                "cleanup_allowed": item.cleanup_allowed,
                "protection_reason": item.protection_reason,
                "recommendation": item.recommendation,
            }
            for item in sorted_items
        ],
    }


def cleanup_storage_items(
    project_root: Path,
    item_ids: Iterable[str],
    *,
    audit_rows: Iterable[dict] | None = None,
    active_run_keys: Iterable[str] | None = None,
) -> dict:
    report = scan_storage_health(
        project_root,
        audit_rows=audit_rows,
        active_run_keys=active_run_keys,
    )
    wanted = {str(item_id).strip() for item_id in item_ids if str(item_id).strip()}
    by_id = {item["id"]: item for item in report["items"]}
    root = Path(project_root).resolve()

    deleted: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    freed = 0

    for item_id in wanted:
        item = by_id.get(item_id)
        if not item:
            skipped.append({"id": item_id, "reason": "Unknown item id"})
            continue
        if not item.get("cleanup_allowed"):
            skipped.append({
                "id": item_id,
                "relative_path": item.get("relative_path"),
                "reason": item.get("protection_reason") or "Not eligible for cleanup",
            })
            continue
        path = (root / str(item["relative_path"])).resolve()
        if not _is_under(root, path):
            skipped.append({
                "id": item_id,
                "relative_path": item.get("relative_path"),
                "reason": "Path escapes project root",
            })
            continue
        if not path.exists():
            skipped.append({
                "id": item_id,
                "relative_path": item.get("relative_path"),
                "reason": "Already removed",
            })
            continue
        try:
            size = int(item.get("size_bytes") or 0)
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            # Remove companion DuckDB WAL when deleting a closed DB file.
            if path.suffix == ".duckdb":
                wal = Path(str(path) + ".wal")
                if wal.exists() and _is_under(root, wal):
                    try:
                        wal.unlink()
                    except OSError:
                        pass
            freed += size
            deleted.append({
                "id": item_id,
                "relative_path": item.get("relative_path"),
                "size_bytes": size,
                "size_human": format_bytes(size),
            })
        except OSError as exc:
            errors.append({
                "id": item_id,
                "relative_path": item.get("relative_path"),
                "reason": str(exc),
            })

    return {
        "deleted": deleted,
        "skipped": skipped,
        "errors": errors,
        "freed_bytes": freed,
        "freed_human": format_bytes(freed),
    }
