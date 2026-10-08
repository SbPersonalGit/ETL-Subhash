from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests._runner import ensure_project_root_on_path

ensure_project_root_on_path()

from ui.app import app

TEST_TEMP_ROOT = PROJECT_ROOT / "temp"


class _TargetConnector:
    def describe_global(self):
        return [{"name": "Account"}]


def test_drop_folder_lists_csv_files_recursively():
    with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as temp_dir:
        drop_dir = Path(temp_dir) / "drop"
        drop_dir.mkdir()
        (drop_dir / "Account.csv").write_text("Id,Name\n1,Acme\n", encoding="utf-8")
        (drop_dir / "notes.txt").write_text("ignore", encoding="utf-8")
        (drop_dir / "nested").mkdir()
        (drop_dir / "nested" / "Contact.csv").write_text("Id\n1\n", encoding="utf-8")

        with patch("ui.app.FLAT_FILE_DROP_DIR", drop_dir):
            response = app.test_client().get("/api/flat-files/drop-files")

    assert response.status_code == 200
    assert [item["name"] for item in response.get_json()["files"]] == [
        "Account.csv",
        "nested/Contact.csv",
    ]


def test_import_registers_selected_drop_file_without_copying_contents():
    with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as temp_dir:
        temp_path = Path(temp_dir)
        drop_dir = temp_path / "drop"
        upload_dir = temp_path / "uploads"
        drop_dir.mkdir()
        upload_dir.mkdir()
        source_folder = drop_dir / "01_insert_files"
        source_folder.mkdir()
        source = source_folder / "Account.csv"
        source.write_text("Id,Name\n1,Acme\n", encoding="utf-8")

        with (
            patch("ui.app.FLAT_FILE_DROP_DIR", drop_dir),
            patch("ui.app.FLAT_FILE_UPLOAD_DIR", upload_dir),
            patch("ui.app.get_connector", return_value=_TargetConnector()),
        ):
            response = app.test_client().post(
                "/api/flat-files/import",
                json={"files": ["01_insert_files/Account.csv"], "profile": "default"},
            )

        assert response.status_code == 200
        payload = response.get_json()
        assert payload["files"][0]["object"] == "Account"
        assert payload["files"][0]["object_detection_source"] == "filename"
        assert payload["files"][0]["source_type"] == "drop_folder"
        assert payload["files"][0]["source_path"] == "01_insert_files/Account.csv"
        batch_dir = upload_dir / payload["batch_id"]
        assert batch_dir.is_dir()
        assert list(batch_dir.iterdir()) == []


def test_import_detects_object_from_parent_folder_for_split_files():
    with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as temp_dir:
        temp_path = Path(temp_dir)
        drop_dir = temp_path / "drop"
        upload_dir = temp_path / "uploads"
        drop_dir.mkdir()
        upload_dir.mkdir()
        account_dir = drop_dir / "Account"
        account_dir.mkdir()
        (account_dir / "part1.csv").write_text("Id,Name\n1,Acme\n", encoding="utf-8")
        (account_dir / "part2.csv").write_text("Id,Name\n2,Beta\n", encoding="utf-8")

        with (
            patch("ui.app.FLAT_FILE_DROP_DIR", drop_dir),
            patch("ui.app.FLAT_FILE_UPLOAD_DIR", upload_dir),
            patch("ui.app.get_connector", return_value=_TargetConnector()),
        ):
            response = app.test_client().post(
                "/api/flat-files/import",
                json={
                    "files": ["Account/part1.csv", "Account/part2.csv"],
                    "profile": "default",
                },
            )

        assert response.status_code == 200
        payload = response.get_json()
        assert [item["object"] for item in payload["files"]] == ["Account", "Account"]
        assert [item["object_detection_source"] for item in payload["files"]] == [
            "folder",
            "folder",
        ]


def test_import_rejects_paths_outside_drop_folder():
    with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as temp_dir:
        temp_path = Path(temp_dir)
        drop_dir = temp_path / "drop"
        upload_dir = temp_path / "uploads"
        drop_dir.mkdir()
        upload_dir.mkdir()
        (temp_path / "secret.csv").write_text("Id\n1\n", encoding="utf-8")

        with (
            patch("ui.app.FLAT_FILE_DROP_DIR", drop_dir),
            patch("ui.app.FLAT_FILE_UPLOAD_DIR", upload_dir),
        ):
            response = app.test_client().post(
                "/api/flat-files/import",
                json={"files": ["../secret.csv"]},
            )

        assert response.status_code == 400
        assert "Invalid drop-folder filename" in response.get_json()["error"]
        assert list(upload_dir.iterdir()) == []


def test_apply_folder_mapping_clones_sample_without_loading_all_files():
    with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as temp_dir:
        temp_path = Path(temp_dir)
        drop_dir = temp_path / "drop"
        upload_dir = temp_path / "uploads"
        drop_dir.mkdir()
        upload_dir.mkdir()
        case_dir = drop_dir / "Case"
        case_dir.mkdir()
        (case_dir / "2021_Case_1.csv").write_text("Id,ContactId\n1,005\n", encoding="utf-8")
        (case_dir / "2021_Case_2.csv").write_text("Id,ContactId\n2,005\n", encoding="utf-8")

        with (
            patch("ui.app.FLAT_FILE_DROP_DIR", drop_dir),
            patch("ui.app.get_connector", return_value=_TargetConnector()),
        ):
            response = app.test_client().post(
                "/api/flat-files/apply-folder-mapping",
                json={
                    "file": {
                        "file": "Case/2021_Case_1.csv",
                        "source_path": "Case/2021_Case_1.csv",
                        "object": "Case",
                        "mappings": [
                            {"source": "Id", "target": "External_Id__c", "ignored": False},
                            {"source": "ContactId", "ignored": True, "target": None},
                        ],
                    },
                    "existing_files": ["Case/2021_Case_1.csv"],
                    "profile": "default",
                },
            )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["folder"] == "Case"
    assert [item["file"] for item in payload["added"]] == ["Case/2021_Case_2.csv"]
    assert payload["added"][0]["mappings"][1]["ignored"] is True


if __name__ == "__main__":
    from tests._runner import run

    sys.exit(run(globals()))
