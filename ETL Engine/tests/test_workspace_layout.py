from tests._runner import ensure_project_root_on_path, run
ensure_project_root_on_path()

import tempfile

import core.workspace_layout as layout
from core.workspace_layout import classify_result_kind, infer_object_api, validation_result_dir
from pathlib import Path


def test_infer_object_api_from_custom_and_standard_names():
    assert infer_object_api("PaymentProxy__c.v2_rejected.csv") == "PaymentProxy__c"
    assert infer_object_api("Promotion__c_success_20261007.csv") == "Promotion__c"
    assert infer_object_api("Account.csv") == "Account"
    assert infer_object_api("notes.txt", "Contact") == "Contact"
    assert infer_object_api("readme.csv") is None


def test_validation_result_dir_uses_object_folder_for_one_object():
    old_pre = layout.PRE_VALIDATE_DIR
    old_post = layout.POST_VALIDATE_DIR
    root = Path(tempfile.mkdtemp())
    try:
        layout.PRE_VALIDATE_DIR = root / "Pre Validate Result"
        layout.POST_VALIDATE_DIR = root / "Post Validate Result"
        pre = validation_result_dir("pre", "prevalidation_1", ["Student_School__c"])
        post = validation_result_dir("post", "validation_1", ["Stage :: Nationality__c", "Stage :: Student_School__c"])
        assert pre == layout.PRE_VALIDATE_DIR / "Student_School__c" / "prevalidation_1"
        assert pre.is_dir()
        assert post == layout.POST_VALIDATE_DIR / "validation_1"
        assert post.is_dir()
    finally:
        layout.PRE_VALIDATE_DIR = old_pre
        layout.POST_VALIDATE_DIR = old_post


def test_publish_file_copies_load_result_into_migration_folder():
    old_migration = layout.MIGRATION_RESULT_DIR
    old_temp = layout.TEMP_FILES_DIR
    root = Path(tempfile.mkdtemp())
    try:
        layout.MIGRATION_RESULT_DIR = root / "Migration Result"
        layout.TEMP_FILES_DIR = root / "Temp Files"
        source = root / "logs" / "run_sf_1" / "load_success" / "Student_School__c_success_1.csv"
        source.parent.mkdir(parents=True)
        source.write_text("Id\n1\n", encoding="utf-8")
        published = layout.publish_file(source, "Student_School__c", "migration")
        expected = layout.MIGRATION_RESULT_DIR / "Student_School__c" / "run_sf_1" / source.name
        assert expected in published
        assert expected.is_file()
    finally:
        layout.MIGRATION_RESULT_DIR = old_migration
        layout.TEMP_FILES_DIR = old_temp


def test_classify_result_kind_from_path():
    assert classify_result_kind(Path("logs/verify/prevalidation_1/PaymentProxy__c.v2_rejected.csv")) == "pre"
    assert classify_result_kind(Path("logs/run_sf_x/obj/load_success/PaymentProxy__c_success.csv")) == "migration"
    assert classify_result_kind(Path("logs/verify/file_validation_1/PaymentProxy__c_verify_summary.csv")) == "post"


if __name__ == "__main__":
    import sys
    sys.exit(run(globals()))
