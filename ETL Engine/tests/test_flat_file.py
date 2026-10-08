from __future__ import annotations

import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests._runner import ensure_project_root_on_path
ensure_project_root_on_path()

from core.field_rules import (
    apply_default_owner_id_mapping,
    build_target_lookup_fields,
    enrich_flat_file_mapping_lookup,
    owner_id_uses_user_external_id,
    resolve_single_lookup_reference,
)


def test_new_polymorphic_owner_defaults_to_user_legacy_external_id():
    mapping = {"source": "OwnerId", "target": "OwnerId"}
    apply_default_owner_id_mapping(
        mapping,
        {"name": "OwnerId", "type": "reference", "referenceTo": ["Group", "User"]},
        {"referenced_object": "User", "external_id_fields": ["External_Id__c"]},
    )

    assert mapping["lookup_resolution"] == "polymorphic_external_id"
    assert mapping["lookup_external_id"] == ""
    assert mapping["polymorphic_mappings"] == [{
        "identify_by": "prefix",
        "match_value": "005",
        "companion_column": "",
        "referenced_object": "User",
        "external_id": "External_Id__c",
        "external_id_fields": ["External_Id__c"],
    }]


def test_user_only_owner_still_defaults_to_prepopulated_owner_rule():
    mapping = {"source": "OwnerId", "target": "OwnerId"}
    apply_default_owner_id_mapping(
        mapping,
        {"name": "OwnerId", "type": "reference", "referenceTo": ["User"]},
        {"referenced_object": "User", "external_id_fields": ["External_Id__c"]},
    )

    assert mapping["is_polymorphic"] is True
    assert mapping["lookup_resolution"] == "polymorphic_external_id"
    assert mapping["polymorphic_mappings"][0] == {
        "identify_by": "prefix",
        "match_value": "005",
        "companion_column": "",
        "referenced_object": "User",
        "external_id": "External_Id__c",
        "external_id_fields": ["External_Id__c"],
    }


def test_owner_default_does_not_override_explicit_target_id_choice():
    mapping = {"source": "OwnerId", "target": "OwnerId", "lookup_resolution": "target_id"}
    apply_default_owner_id_mapping(
        mapping,
        {"name": "OwnerId", "type": "reference", "referenceTo": ["Group", "User"]},
        {"referenced_object": "User", "external_id_fields": ["External_Id__c"]},
    )

    assert mapping["lookup_resolution"] == "target_id"
    assert "polymorphic_mappings" not in mapping
from core.flat_file import (
    FlatFileInput,
    FlatFileTemplateStore,
    FlatFileValidationError,
    apply_unmapped_column_policy,
    fill_missing_sibling_mappings,
    auto_map_columns,
    build_folder_mapping_files,
    clone_mapping_for_headers,
    detect_object_from_filename,
    detect_object_from_path,
    record_type_source_column,
    validate_file_config,
    validate_flat_file_external_id,
    validate_flat_file_reference,
)
from core.stager import Stager


def test_template_store_clears_stale_external_id_for_target_id_lookup():
    with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "temp") as temp_dir:
        store = FlatFileTemplateStore(temp_dir)
        saved = store.save("Target IDs", {
            "files": [{
                "mappings": [{
                    "source": "GE_HQ_Region_Tier1__c",
                    "target": "GE_HQ_Region_Tier1__c",
                    "referenced_object": "GE_HQ_Region__c",
                    "lookup_resolution": "target_id",
                    "lookup_external_id": "External_Id__c",
                }],
            }],
        })

        assert saved["files"][0]["mappings"][0]["lookup_external_id"] == ""
        loaded = store.get("Target_IDs")
        assert loaded["files"][0]["mappings"][0]["lookup_resolution"] == "target_id"
        assert loaded["files"][0]["mappings"][0]["lookup_external_id"] == ""


def test_template_store_deletes_saved_template():
    with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "temp") as temp_dir:
        store = FlatFileTemplateStore(temp_dir)
        saved = store.save("Delete Me", {"files": []})

        store.delete(saved["id"])

        assert store.list() == []
        try:
            store.get(saved["id"])
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("Deleted template should not be loadable")


def test_auto_map_uses_api_name_case_insensitively_and_not_label():
    fields = [
        {"name": "Name", "label": "Account Name"},
        {"name": "External_Id__c", "label": "Legacy ID"},
    ]
    mappings = auto_map_columns(["name", "Legacy ID", "Other"], fields)
    assert mappings[0]["target"] == "Name"
    assert mappings[1]["target"] is None
    assert mappings[2]["status"] == "unmapped"


def test_record_type_developer_name_header_maps_to_record_type_id():
    fields = [{"name": "RecordTypeId"}, {"name": "Name"}]
    mappings = auto_map_columns(["RecordType.DeveloperName", "Name"], fields)
    assert record_type_source_column(["RecordType.DeveloperName", "Name"]) == "RecordType.DeveloperName"
    assert mappings[0]["target"] == "RecordTypeId"
    assert mappings[0]["status"] == "record_type_developer_name"


def test_validation_requires_unmapped_columns_to_be_ignored():
    errors = validate_file_config({
        "file": "Account.csv",
        "object": "Account",
        "mappings": [{"source": "Unused", "target": None, "ignored": False}],
    })
    assert "neither mapped nor ignored" in " ".join(errors)


def test_package_policy_excludes_only_unmapped_columns():
    config = {
        "file": "Account.csv",
        "object": "Account",
        "mappings": [
            {"source": "Name", "target": "Name", "ignored": False},
            {"source": "Unknown__c", "target": None, "ignored": False},
            {"source": "AlreadyIgnored__c", "target": None, "ignored": True},
        ],
    }
    assert apply_unmapped_column_policy(config, True) == 1
    assert config["mappings"][0] == {
        "source": "Name", "target": "Name", "ignored": False
    }
    assert config["mappings"][1]["ignored"] is True
    assert config["mappings"][1]["status"] == "ignored_by_package_policy"
    assert validate_file_config(config) == []


def test_filename_detects_exact_object_api_name_only():
    objects = ["Account", "Case", "GE_OG_GS_Passport__c"]
    assert detect_object_from_filename("Account.csv", objects) == "Account"
    assert detect_object_from_filename("ge_og_gs_passport__c.CSV", objects) == "GE_OG_GS_Passport__c"
    assert detect_object_from_filename("Account_export.csv", objects) is None


def test_path_prefers_nearest_matching_folder_then_filename():
    objects = ["Account", "User", "Case"]
    assert detect_object_from_path("Account/part1.csv", objects) == ("Account", "folder")
    assert detect_object_from_path("Account/CMF Approved/batch_2.csv", objects) == ("Account", "folder")
    assert detect_object_from_path("Users/User.csv", objects) == ("User", "filename")
    assert detect_object_from_path("Account.csv", objects) == ("Account", "filename")
    assert detect_object_from_path("misc/export_1.csv", objects) == (None, "")


def test_validation_requires_all_source_record_types_to_be_mapped():
    config = {
        "file": "Case.csv",
        "object": "Case",
        "mappings": [{"source": "RecordTypeId", "target": "RecordTypeId"}],
        "record_type_values": ["012SOURCEA", "012SOURCEB"],
        "record_type_mappings": {"012SOURCEA": "012TARGETA"},
    }
    errors = validate_file_config(config)
    assert "012SOURCEB" in " ".join(errors)
    config["record_type_mappings"]["012SOURCEB"] = "012TARGETB"
    assert validate_file_config(config) == []


def test_external_id_validation_catches_missing_target_external_id_before_cleaning():
    config = {
        "file": "Lead.csv",
        "object": "Lead",
        "external_id": "External_Id__c",
        "mappings": [{"source": "Id", "target": "External_Id__c"}],
    }
    errors = validate_flat_file_external_id(
        config,
        [{"name": "Company", "createable": True, "externalId": False}],
    )
    message = " ".join(errors)
    assert "Lead.csv" in message
    assert "Target Lead has no writable External ID fields" in message


def test_external_id_validation_requires_selection_and_accepts_valid_field():
    fields = [{
        "name": "External_Id__c",
        "createable": True,
        "updateable": True,
        "externalId": True,
    }]
    config = {
        "file": "Account.csv",
        "object": "Account",
        "external_id": "",
        "mappings": [{"source": "Id", "target": "Id"}],
    }
    assert "Select the target External ID" in " ".join(
        validate_flat_file_external_id(config, fields)
    )
    config["external_id"] = "External_Id__c"
    assert validate_flat_file_external_id(config, fields) == []


def test_multiple_files_for_same_object_are_combined_with_provenance():
    root = PROJECT_ROOT / "tests" / "fixtures" / "flat_file"
    stager = Stager(":memory:")
    try:
        mappings = [
            {"source": "Legacy_Id__c", "target": "Legacy_Id__c"},
            {"source": "Name", "target": "Name"},
        ]
        result = FlatFileInput(stager, root).ingest({
            "files": [
                {"stored_name": "one.csv", "object": "Account", "mappings": mappings},
                {"stored_name": "two.csv", "object": "Account", "mappings": mappings},
            ]
        })
        assert result.object_counts == {"Account": 3}
        assert result.file_counts == {"one.csv": 1, "two.csv": 2}
        assert stager.get_raw_count("Account") == 3
        assert "__source_file" in stager.get_column_names("Account")
    finally:
        stager.close()


def test_ingest_replaces_source_record_type_with_target_record_type():
    root = PROJECT_ROOT / "tests" / "fixtures" / "flat_file"
    stager = Stager(":memory:")
    try:
        FlatFileInput(stager, root).ingest({
            "files": [{
                "stored_name": "record_types.csv",
                "object": "Case",
                "mappings": [
                    {"source": "Id", "target": "Id"},
                    {"source": "RecordTypeId", "target": "RecordTypeId"},
                    {"source": "Subject", "target": "Subject"},
                ],
                "record_type_values": ["012SOURCE"],
                "record_type_mappings": {"012SOURCE": "012TARGET"},
            }]
        })
        staged = next(stager.read_raw_chunks("Case"))
        assert staged["RecordTypeId"].to_list() == ["012TARGET"]
    finally:
        stager.close()


def test_ingest_retains_source_id_when_mapped_to_external_id():
    root = PROJECT_ROOT / "tests" / "fixtures" / "flat_file"
    stager = Stager(":memory:")
    try:
        FlatFileInput(stager, root).ingest({
            "files": [{
                "stored_name": "record_types.csv",
                "object": "Knowledge__kav",
                "mappings": [
                    {"source": "Id", "target": "Prod_Record_Id__c"},
                    {"source": "Subject", "target": "Title"},
                ],
            }]
        })
        staged = next(stager.read_raw_chunks("Knowledge__kav"))
        assert "Id" in staged.columns
        assert "Prod_Record_Id__c" in staged.columns
        assert staged["Id"].to_list() == staged["Prod_Record_Id__c"].to_list()
    finally:
        stager.close()


def test_ingest_streams_zero_copy_drop_folder_reference():
    root = PROJECT_ROOT / "tests" / "fixtures" / "flat_file"
    upload_root = PROJECT_ROOT / "state" / "flat_file_uploads" / "test_zero_copy_not_created"
    stager = Stager(":memory:")
    try:
        result = FlatFileInput(stager, upload_root, root).ingest({
            "files": [{
                "file": "one.csv",
                "stored_name": "one.csv",
                "source_type": "drop_folder",
                "source_path": "one.csv",
                "object": "Account",
                "mappings": [
                    {"source": "Legacy_Id__c", "target": "Legacy_Id__c"},
                    {"source": "Name", "target": "Name"},
                ],
            }]
        })
        assert result.object_counts == {"Account": 1}
        assert not upload_root.exists()
    finally:
        stager.close()


def test_ingest_rejects_drop_file_changed_after_registration():
    root = PROJECT_ROOT / "tests" / "fixtures" / "flat_file"
    stager = Stager(":memory:")
    try:
        try:
            FlatFileInput(stager, root, root).ingest({
                "files": [{
                    "file": "one.csv",
                    "stored_name": "one.csv",
                    "source_type": "drop_folder",
                    "source_path": "one.csv",
                    "source_size": 1,
                    "object": "Account",
                    "mappings": [
                        {"source": "Legacy_Id__c", "target": "Legacy_Id__c"},
                        {"source": "Name", "target": "Name"},
                    ],
                }]
            })
        except Exception as exc:
            assert "changed size" in str(exc)
        else:
            raise AssertionError("Expected a changed drop-folder file to be rejected")
    finally:
        stager.close()


def test_ingest_omits_files_explicitly_skipped_from_staging():
    root = PROJECT_ROOT / "tests" / "fixtures" / "flat_file"
    stager = Stager(":memory:")
    try:
        mappings = [
            {"source": "Legacy_Id__c", "target": "Legacy_Id__c"},
            {"source": "Name", "target": "Name"},
        ]
        result = FlatFileInput(stager, root).ingest({
            "files": [
                {
                    "stored_name": "one.csv",
                    "object": "Account",
                    "mappings": mappings,
                    "skip_staging": True,
                },
                {
                    "stored_name": "two.csv",
                    "object": "Account",
                    "mappings": mappings,
                },
            ]
        })
        assert result.object_counts == {"Account": 2}
        assert result.file_counts == {"two.csv": 2}
        assert stager.get_raw_count("Account") == 2
    finally:
        stager.close()


def test_drop_folder_mtime_drift_same_size_is_refreshed():
    with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "temp") as temp_dir:
        path = Path(temp_dir) / "User.csv"
        path.write_text("Id,Username\n005,a@x.com\n", encoding="utf-8")
        stat = path.stat()
        file_config = {
            "source_type": "drop_folder",
            "source_size": stat.st_size,
            "size": stat.st_size,
            "source_mtime_ns": stat.st_mtime_ns - 1_000_000,
        }
        validate_flat_file_reference(path, file_config)
        assert file_config["source_mtime_ns"] == path.stat().st_mtime_ns


def test_drop_folder_size_change_requires_reimport():
    with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "temp") as temp_dir:
        path = Path(temp_dir) / "User.csv"
        path.write_text("Id,Username\n005,a@x.com\n", encoding="utf-8")
        stat = path.stat()
        file_config = {
            "source_type": "drop_folder",
            "source_size": stat.st_size + 10,
            "source_mtime_ns": stat.st_mtime_ns,
        }
        try:
            validate_flat_file_reference(path, file_config)
            raise AssertionError("Expected FlatFileValidationError for size change")
        except FlatFileValidationError as exc:
            assert "changed size" in str(exc)


def test_drop_folder_raw_fingerprint_is_not_applied_to_clean_artifact():
    with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "temp") as temp_dir:
        clean_path = Path(temp_dir) / "User_clean.csv"
        clean_path.write_text("Id,Username\n005,a@x.com\n", encoding="utf-8")
        file_config = {
            "source_type": "drop_folder",
            "source_size": clean_path.stat().st_size - 3,
            "source_mtime_ns": clean_path.stat().st_mtime_ns - 1_000_000,
            "cleaned_name": clean_path.name,
        }

        validate_flat_file_reference(clean_path, file_config)


def test_cp1252_csv_with_accented_names_is_readable():
    from core.flat_file import detect_csv_encoding, open_csv, polars_csv_encoding, read_csv_header

    with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "temp") as temp_dir:
        path = Path(temp_dir) / "User.csv"
        # Byte 0xe9 is 'é' in CP1252 and invalid UTF-8 continuation.
        path.write_bytes(b"Id,Name\n005xxx,Ren\xe9 von Busch\n")
        assert detect_csv_encoding(path) == "cp1252"
        assert polars_csv_encoding(path) == "cp1252"
        assert read_csv_header(path) == ["Id", "Name"]
        with open_csv(path) as handle:
            rows = list(__import__("csv").DictReader(handle))
        assert rows[0]["Name"] == "René von Busch"


def test_read_csv_header_strips_trailing_blank_columns():
    from core.flat_file import read_csv_header, require_named_csv_headers

    with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "temp") as temp_dir:
        path = Path(temp_dir) / "Case.csv"
        path.write_text("Id,Subject,RecordTypeId,,\n500xxx,Test,,\n", encoding="utf-8")
        assert read_csv_header(path) == ["Id", "Subject", "RecordTypeId"]
        assert require_named_csv_headers(read_csv_header(path), path.name) == [
            "Id", "Subject", "RecordTypeId"
        ]


def test_require_named_csv_headers_ignores_blank_middle_column():
    from core.flat_file import require_named_csv_headers

    headers = ["Priority", "Procedure__c", "", "Reason", "RecordTypeId"]
    assert require_named_csv_headers(headers, "Case_4_attempt_2.csv") == [
        "Priority", "Procedure__c", "Reason", "RecordTypeId"
    ]


def test_utf8_csv_still_preferred_when_valid():
    from core.flat_file import detect_csv_encoding

    with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "temp") as temp_dir:
        path = Path(temp_dir) / "User.csv"
        path.write_text("Id,Name\n005xxx,René von Busch\n", encoding="utf-8")
        assert detect_csv_encoding(path) == "utf-8-sig"


def test_clone_mapping_for_headers_copies_template_and_ignores_extra_columns():
    source = {
        "mappings": [
            {"source": "Id", "target": "External_Id__c", "ignored": False, "status": "external_id_mapped"},
            {"source": "ContactId", "target": None, "ignored": True, "status": "ignored"},
        ]
    }
    cloned = clone_mapping_for_headers(source, ["Id", "ContactId", "Extra__c"])
    by_source = {item["source"]: item for item in cloned}
    assert by_source["Id"]["target"] == "External_Id__c"
    assert by_source["ContactId"]["ignored"] is True
    assert by_source["Extra__c"]["ignored"] is True
    assert by_source["Extra__c"]["status"] == "ignored"


def test_fill_missing_sibling_mappings_copies_sample_onto_unmapped_files():
    files = [
        {
            "file": "Case/2021_Case_1.csv",
            "object": "Case",
            "external_id": "External_Id__c",
            "mappings": [
                {"source": "Id", "target": "External_Id__c", "ignored": False},
                {"source": "ContactId", "target": None, "ignored": True},
            ],
        },
        {
            "file": "Case/2021_Case_2.csv",
            "object": "Case",
            "columns": ["Id", "ContactId"],
        },
        {
            "file": "Case/2021_Case_3.csv",
            "object": "Case",
        },
    ]
    assert fill_missing_sibling_mappings(files) == 2
    assert files[1]["mappings"][0]["target"] == "External_Id__c"
    assert files[1]["mappings"][1]["ignored"] is True
    assert files[2]["mappings"][0]["target"] == "External_Id__c"
    assert files[2]["external_id"] == "External_Id__c"


def test_build_folder_mapping_files_adds_same_folder_csvs():
    with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "temp") as temp_dir:
        drop = Path(temp_dir) / "drop"
        case_dir = drop / "Case"
        case_dir.mkdir(parents=True)
        (case_dir / "2021_Case_1.csv").write_text("Id,ContactId,Subject\n1,005,A\n", encoding="utf-8")
        (case_dir / "2021_Case_2.csv").write_text("Id,ContactId,Subject,Extra__c\n2,005,B,x\n", encoding="utf-8")
        (drop / "Account.csv").write_text("Id,Name\n1,Acme\n", encoding="utf-8")
        source = {
            "file": "Case/2021_Case_1.csv",
            "source_path": "Case/2021_Case_1.csv",
            "object": "Case",
            "object_detection_source": "folder",
            "external_id": "External_Id__c",
            "mappings": [
                {"source": "Id", "target": "External_Id__c", "ignored": False, "status": "external_id_mapped"},
                {"source": "ContactId", "target": None, "ignored": True, "status": "ignored"},
                {"source": "Subject", "target": "Subject", "ignored": False, "status": "auto_mapped"},
            ],
        }
        result = build_folder_mapping_files(drop, source, ["Case/2021_Case_1.csv"], ["Case", "Account"])
        assert result["folder"] == "Case"
        assert [item["file"] for item in result["added"]] == ["Case/2021_Case_2.csv"]
        cloned = result["added"][0]
        assert cloned["object"] == "Case"
        assert cloned["mapping_template_file"] == "Case/2021_Case_1.csv"
        by_source = {item["source"]: item for item in cloned["mappings"]}
        assert by_source["ContactId"]["ignored"] is True
        assert by_source["Extra__c"]["ignored"] is True
        assert by_source["Subject"]["target"] == "Subject"


def test_resolve_single_lookup_reference_treats_owner_id_as_user():
    assert resolve_single_lookup_reference("OwnerId", ["Group", "User"]) == "User"
    assert resolve_single_lookup_reference("OwnerId", ["User"]) == "User"
    assert resolve_single_lookup_reference("WhatId", ["Account", "Opportunity"]) is None


def test_owner_id_user_external_id_policy_detects_explicit_lookup_only():
    refs = ["Group", "User"]
    assert owner_id_uses_user_external_id("OwnerId", refs, {}) is False
    assert owner_id_uses_user_external_id(
        "OwnerId",
        refs,
        {
            "referenced_object": "User",
            "lookup_resolution": "external_id",
            "external_id": "External_Id__c",
        },
    ) is True
    assert owner_id_uses_user_external_id(
        "OwnerId",
        refs,
        {"lookup_resolution": "idmapper"},
    ) is False


def test_build_target_lookup_fields_includes_polymorphic_owner_id():
    class FakeConnector:
        def describe_object(self, object_name):
            assert object_name == "User"
            return {
                "fields": [
                    {"name": "External_Id__c", "externalId": True},
                    {"name": "Username", "externalId": False},
                ]
            }

    fields = [{
        "name": "OwnerId",
        "type": "reference",
        "referenceTo": ["Group", "User"],
    }]
    lookup_fields = build_target_lookup_fields(
        FakeConnector(),
        fields,
        {"ownerid"},
    )
    assert lookup_fields["OwnerId"]["referenced_object"] == "User"
    assert lookup_fields["OwnerId"]["recommended_external_id"] == "External_Id__c"


def test_enrich_flat_file_mapping_lookup_backfills_owner_metadata():
    class FakeConnector:
        def describe_object(self, object_name):
            return {"fields": [{"name": "External_Id__c", "externalId": True}]}

    mapping = {"target": "OwnerId", "source": "OwnerId"}
    enrich_flat_file_mapping_lookup(
        mapping,
        {"name": "OwnerId", "type": "reference", "referenceTo": ["Group", "User"]},
        FakeConnector(),
    )
    assert mapping["referenced_object"] == "User"
    assert mapping["lookup_external_id"] == "External_Id__c"
    assert mapping["lookup_resolution"] == "external_id"


if __name__ == "__main__":
    import sys
    from tests._runner import run
    sys.exit(run(globals()))
