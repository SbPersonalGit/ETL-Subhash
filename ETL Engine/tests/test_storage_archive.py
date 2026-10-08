"""Tests for storage archive, dependency analysis, and restore."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests._runner import ensure_project_root_on_path, run

ensure_project_root_on_path()

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
from core.storage_health import scan_storage_health


def _write(path: Path, content: bytes | str = b"x" * 50) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)


def _seed_workspace(root: Path) -> dict:
    batch_id = "batchabc123"
    template_id = "Demo_Template"
    _write(root / "config" / "archive.json", json.dumps({
        "archive_root": "runtime/archive",
        "restore_root": "runtime/restore",
        "package_staging_root": "runtime/package_staging",
    }))
    _write(
        root / "config" / "flat_file_templates" / f"{template_id}.json",
        json.dumps({
            "id": template_id,
            "name": template_id,
            "batch_id": batch_id,
            "prevalidation_status": "PASSED",
            "files": [{
                "object": "Account",
                "source_type": "drop_folder",
                "source_path": "Account/Account.csv",
            }],
        }),
    )
    _write(
        root / "config" / "pipeline_designer" / "Demo_Pipeline.json",
        json.dumps({
            "id": "Demo_Pipeline",
            "name": "Demo_Pipeline",
            "pipelineStages": [{
                "flatFileTemplateId": template_id,
                "templateId": f"flat:{template_id}",
                "pipelineName": "Demo stage",
            }],
        }),
    )
    _write(root / "state" / "flat_file_uploads" / batch_id / "Account_clean.csv", b"clean" * 20)
    _write(root / "state" / "flat_file_drop" / "Account" / "Account.csv", b"source" * 20)
    _write(root / "logs" / "verify" / "pipeline_prevalidation_done_x" / "report.json", b'{"ok":true}')
    return {"batch_id": batch_id, "template_id": template_id}


def test_template_pipeline_dependency_blocks_cleanup():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        seeded = _seed_workspace(root)
        health = enrich_health_with_templates(root, scan_storage_health(root))
        template_item = next(
            item for item in health["items"]
            if item["category"] == "templates" and item.get("template_id") == seeded["template_id"]
        )
        deps = analyze_dependencies(root, [template_item["id"]])
        assert deps["can_cleanup"] is False
        assert any("pipeline" in str(row.get("message") or "").lower() for row in deps["blockers"])


def test_archive_then_cleanup_and_restore_batch():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        seeded = _seed_workspace(root)
        # Remove pipeline reference so template/batch cleanup can proceed after archive.
        (root / "config" / "pipeline_designer" / "Demo_Pipeline.json").unlink()

        health = enrich_health_with_templates(root, scan_storage_health(root))
        batch_item = next(
            item for item in health["items"]
            if item["category"] == "flat_file_uploads" and item["name"] == seeded["batch_id"]
        )
        package = create_archive_package(root, [batch_item["id"]], include_related=True)
        assert (Path(package["path"]) / "manifest.json").is_file()
        assert (Path(package["path"]) / "payload" / "state" / "flat_file_uploads" / seeded["batch_id"]).exists()

        result = cleanup_with_backup_gate(
            root,
            [batch_item["id"]],
            confirm=True,
            archive_package_id=package["package_id"],
        )
        assert not (root / "state" / "flat_file_uploads" / seeded["batch_id"]).exists()
        assert result["deleted"]

        restored = restore_archive_package(root, package["package_id"], overwrite=True)
        assert restored["restored"]
        assert (root / "state" / "flat_file_uploads" / seeded["batch_id"] / "Account_clean.csv").exists()


def test_storage_groups_files_under_known_templates():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        seeded = _seed_workspace(root)
        health = enrich_health_with_templates(root, scan_storage_health(root))
        groups = {row["template_id"]: row for row in health["templates"]}
        demo = groups[seeded["template_id"]]
        assert demo["created_at"]
        assert "Account" in demo["objects"]
        assert demo["pipelines"]
        assert any(item["category"] == "flat_file_uploads" for item in demo["can_clean"])
        assert any(item["category"] == "flat_file_drop" for item in demo["keep"])
        assert any(item["category"] == "templates" for item in demo["keep"])
        leftover = groups.get("_unassigned")
        if leftover:
            assert leftover["name"] == "Other leftover files"
        assert health["backup_strategy"]["mode"] == "archive_then_delete"


def test_template_cleanup_selection_keeps_definition_and_drop_files():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        seeded = _seed_workspace(root)
        (root / "config" / "pipeline_designer" / "Demo_Pipeline.json").unlink()
        health = enrich_health_with_templates(root, scan_storage_health(root))
        demo = next(row for row in health["templates"] if row["template_id"] == seeded["template_id"])
        assert demo["cleanup_allowed"] is True
        selected_categories = {
            item["category"]
            for item in health["items"]
            if item["id"] in set(demo["item_ids"])
        }
        assert "flat_file_uploads" in selected_categories
        assert "templates" not in selected_categories
        assert "flat_file_drop" not in selected_categories


def test_template_workflow_history_survives_health_refresh():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        seeded = _seed_workspace(root)
        audit_rows = [
            {
                "run_key": "storage_review_demo",
                "activity_type": "STORAGE",
                "data_operation": "REVIEW",
                "status": "COMPLETED",
                "completed_at": "2026-08-29T10:00:00+00:00",
                "request_payload_json": json.dumps({"template_ids": [seeded["template_id"]]}),
                "status_payload_json": json.dumps({"can_cleanup": True}),
            },
            {
                "run_key": "storage_archive_demo",
                "activity_type": "STORAGE",
                "data_operation": "ARCHIVE",
                "status": "COMPLETED",
                "completed_at": "2026-08-29T10:05:00+00:00",
                "request_payload_json": json.dumps({"template_ids": [seeded["template_id"]]}),
                "status_payload_json": json.dumps({"package_id": "stor_pkg_demo"}),
            },
            {
                "run_key": "storage_cleanup_demo",
                "activity_type": "STORAGE",
                "data_operation": "CLEANUP",
                "status": "COMPLETED",
                "completed_at": "2026-08-29T10:10:00+00:00",
                "request_payload_json": json.dumps({
                    "template_ids": [seeded["template_id"]],
                    "archive_package_id": "stor_pkg_demo",
                }),
                "status_payload_json": json.dumps({"deleted_count": 1}),
            },
        ]
        health = enrich_health_with_templates(root, scan_storage_health(root), audit_rows=audit_rows)
        demo = next(row for row in health["templates"] if row["template_id"] == seeded["template_id"])
        assert demo["workflow"]["review"]["status"] == "COMPLETED"
        assert demo["workflow"]["archive"]["package_id"] == "stor_pkg_demo"
        assert demo["workflow"]["cleanup"]["completed_at"] == "2026-08-29T10:10:00+00:00"


def test_unassigned_workflow_history_survives_health_refresh():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _seed_workspace(root)
        audit_rows = [{
            "run_key": "storage_archive_leftovers",
            "activity_type": "STORAGE",
            "data_operation": "ARCHIVE",
            "status": "COMPLETED",
            "completed_at": "2026-08-31T07:57:32+00:00",
            "request_payload_json": json.dumps({"template_ids": ["_unassigned"]}),
            "status_payload_json": json.dumps({"package_id": "stor_pkg_leftovers"}),
        }]
        health = enrich_health_with_templates(root, scan_storage_health(root), audit_rows=audit_rows)
        leftover = next(row for row in health["templates"] if row["template_id"] == "_unassigned")
        assert leftover["workflow"]["archive"]["status"] == "COMPLETED"
        assert leftover["workflow"]["archive"]["package_id"] == "stor_pkg_leftovers"


def test_archive_manifest_preserves_template_ownership():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        seeded = _seed_workspace(root)
        (root / "config" / "pipeline_designer" / "Demo_Pipeline.json").unlink()
        health = enrich_health_with_templates(root, scan_storage_health(root))
        demo = next(row for row in health["templates"] if row["template_id"] == seeded["template_id"])
        package = create_archive_package(root, demo["item_ids"], include_related=True)
        assert seeded["template_id"] in package["manifest"]["template_ids"]


def test_cleanup_requires_archive_gate():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _seed_workspace(root)
        (root / "config" / "pipeline_designer" / "Demo_Pipeline.json").unlink()
        health = enrich_health_with_templates(root, scan_storage_health(root))
        verify_item = next(item for item in health["items"] if item["category"] == "validation_outputs")
        raised = False
        try:
            cleanup_with_backup_gate(root, [verify_item["id"]], confirm=True)
        except ValueError as exc:
            raised = True
            assert "archive" in str(exc).lower() or "skip_backup" in str(exc).lower()
        assert raised


def test_backup_folder_can_live_outside_workspace():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "project"
        backup = Path(tmp) / "backups" / "orgshift"
        _seed_workspace(root)
        (root / "config" / "pipeline_designer" / "Demo_Pipeline.json").unlink()
        cfg = save_archive_config(root, str(backup))
        assert Path(cfg["archive_root"]) == backup.resolve()
        assert cfg["inside_workspace"] is False

        health = enrich_health_with_templates(root, scan_storage_health(root))
        batch_item = next(item for item in health["items"] if item["category"] == "flat_file_uploads")
        package = create_archive_package(root, [batch_item["id"]], include_related=True)
        assert str(package["path"]).startswith(str(backup.resolve()))
        listed = list_archive_packages(root)
        assert any(row["package_id"] == package["package_id"] for row in listed["packages"])


def test_backup_folder_rejects_working_data_paths():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _seed_workspace(root)
        raised = False
        try:
            save_archive_config(root, str(root / "state" / "backups"))
        except ValueError as exc:
            raised = True
            assert "state" in str(exc).lower()
        assert raised


if __name__ == "__main__":
    raise SystemExit(run(globals()))
