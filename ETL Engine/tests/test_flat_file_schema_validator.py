from __future__ import annotations

import csv
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests._runner import ensure_project_root_on_path

ensure_project_root_on_path()

from core.flat_file_schema_validator import (
    _dependent_picklist_controller_error,
    _enforce_record_type_picklist,
    _record_type_picklist_error,
    _record_type_source_column,
    _required_field_errors,
    _safe_normalize,
    clean_csv_against_schema,
    validate_object_and_record_type_access,
    validate_strict_api_names,
    validate_value,
    validate_migration_field_access,
)
from core.stager import Stager
import polars as pl


def test_dependent_picklist_requires_mapped_and_populated_controller():
    field = {
        "dependentPicklist": True,
        "controllerName": "BA_Product_Company__c",
    }
    assert "requires mapped controlling field" in _dependent_picklist_controller_error(
        "Digital Solutions", field, {}, {}
    )
    mapping = {"BA_Product_Company__c": "ProductCompany"}
    assert "requires a value" in _dependent_picklist_controller_error(
        "Digital Solutions", field, mapping, {"ProductCompany": ""}
    )
    assert _dependent_picklist_controller_error(
        "Digital Solutions", field, mapping, {"ProductCompany": "Waygate"}
    ) is None


def test_required_dependent_picklist_reports_invisible_controller_access():
    fields = [{
        "name": "BA_Tier_2__c",
        "createable": True,
        "nillable": False,
        "defaultedOnCreate": False,
        "calculated": False,
        "autoNumber": False,
        "dependentPicklist": True,
        "controllerName": "BA_Product_Company__c",
    }]
    issues = validate_migration_field_access("BA_Questionnaire__c", fields, [])
    assert issues[0]["code"] == "required_controller_not_visible"
    assert "Grant field-level read/create access" in issues[0]["message"]


def test_exact_read_only_source_column_is_reported_and_can_be_ignored():
    mappings = [{
        "source": "Calculated_Score__c",
        "target": "",
        "status": "read_only",
        "ignored": False,
    }]
    issues = validate_migration_field_access("Account", [], mappings)
    assert issues[0]["code"] == "target_field_read_only"
    assert "cannot be loaded by insert or upsert" in issues[0]["message"]
    mappings[0]["ignored"] = True
    assert validate_migration_field_access("Account", [], mappings) == []


def test_standard_system_managed_columns_are_not_access_errors():
    mappings = [
        {"source": "LastModifiedById", "target": "", "status": "read_only"},
        {"source": "SystemModstamp", "target": "", "status": "read_only"},
        {"source": "LastModifiedDate", "target": "LastModifiedDate"},
    ]
    assert validate_migration_field_access("User", [], mappings) == []


def test_user_login_and_platform_state_columns_are_not_access_errors():
    names = [
        "LastLoginDate",
        "LastPasswordChangeDate",
        "PasswordExpirationDate",
        "NumberOfFailedLogins",
        "SuAccessExpirationDate",
        "OfflineTrialExpirationDate",
        "OfflinePdaTrialExpirationDate",
        "IsPartner",
    ]
    mappings = [
        {"source": name, "target": "", "status": "read_only"}
        for name in names
    ]
    assert validate_migration_field_access("User", [], mappings) == []


def test_formula_rollup_and_auto_number_fields_are_not_access_errors():
    fields = [
        {"name": "Formula_Total__c", "calculated": True, "createable": False, "updateable": False},
        {"name": "Rollup_Total__c", "calculated": True, "createable": False, "updateable": False},
        {"name": "Generated_Number__c", "autoNumber": True, "createable": False, "updateable": False},
    ]
    mappings = [
        {"source": field["name"], "target": field["name"]}
        for field in fields
    ]
    assert validate_migration_field_access("Example__c", fields, mappings) == []


def test_percent_precision_and_scale_are_enforced():
    field = {"type": "percent", "precision": 2, "scale": 1}
    assert validate_value("9.9", field) is None
    assert "at most 9.9" in validate_value("1250.0", field)
    assert validate_value("1.25", field)


def test_decimal_zero_and_negative_precision_boundaries():
    field = {"type": "currency", "precision": 4, "scale": 2}
    for value in ("0", "0.0", "-0", "00.00", "-99.99"):
        assert validate_value(value, field) is None
    assert validate_value("100.00", field)
    assert validate_value("-100.00", field)


def test_common_salesforce_types_are_validated():
    assert validate_value("true", {"type": "boolean"}) is None
    assert validate_value("1", {"type": "boolean"}) == "expected true or false"
    assert validate_value("2026-07-30", {"type": "date"}) is None
    assert validate_value("30/07/2026", {"type": "date"}) == "expected YYYY-MM-DD"
    assert validate_value("abcd", {"type": "string", "length": 3}) == "length 4 exceeds maximum 3"


def test_salesforce_id_length_policy():
    field = {"type": "reference"}
    checks = {"salesforce_id_format"}
    id_15 = "001000000000001"
    id_18 = "001000000000001AAA"

    assert validate_value(id_15, field, enabled_checks=checks, salesforce_id_length="15_or_18") is None
    assert validate_value(id_18, field, enabled_checks=checks, salesforce_id_length="15_or_18") is None
    assert validate_value(id_15, field, enabled_checks=checks, salesforce_id_length="15") is None
    assert validate_value(id_18, field, enabled_checks=checks, salesforce_id_length="15") == "expected a 15-character Salesforce ID"
    assert validate_value(id_18, field, enabled_checks=checks, salesforce_id_length="18") is None
    assert validate_value(id_15, field, enabled_checks=checks, salesforce_id_length="18") == "expected an 18-character Salesforce ID"


def test_date_value_policies_normalize_or_bypass_format_validation():
    assert _safe_normalize(
        "4/1/2016", {"type": "date", "_value_policy": "date_mdy4"}
    ) == "2016-04-01"
    assert _safe_normalize(
        "4/1/16", {"type": "date", "_value_policy": "date_mdy2"}
    ) == "2016-04-01"
    assert validate_value(
        "4/1/16", {"type": "date", "_value_policy": "date_bypass"}
    ) is None
    assert validate_value(
        "4/1/16", {"type": "date"}, enabled_checks={"text_limits"}
    ) is None


def test_url_value_policies_match_salesforce_or_prepend_scheme():
    assert validate_value(
        "industrial.ai", {"type": "url", "_value_policy": "url_salesforce"}
    ) is None
    assert validate_value(
        "industrial.ai", {"type": "url", "_value_policy": "url_bypass"}
    ) is None
    field = {"type": "url", "_value_policy": "url_prepend_https"}
    normalized = _safe_normalize("industrial.ai", field)
    assert normalized == "https://industrial.ai"
    assert validate_value(normalized, field) is None


def test_multipicklist_phone_url_and_utf8_byte_length_are_validated():
    multipicklist = {
        "type": "multipicklist",
        "restrictedPicklist": True,
        "picklistValues": [{"value": "A", "active": True}, {"value": "B", "active": True}],
    }
    assert validate_value("A;B", multipicklist) is None
    assert "C" in validate_value("A;C", multipicklist)
    assert validate_value("+1 (555) 123-4567", {"type": "phone"}) is None
    assert validate_value("call-me", {"type": "phone"})
    assert validate_value("https://example.com/a", {"type": "url"}) is None
    assert validate_value("example.com", {"type": "url"})
    assert validate_value("éé", {"type": "string", "length": 2, "byteLength": 3})


def test_strict_api_names_allows_folder_split_files():
    config = {
        "file": "Account/CMF Approved_1.csv",
        "object": "Account",
        "external_id": "External_Id__c",
        "mappings": [
            {"source": "Id", "target": "External_Id__c"},
            {"source": "Name", "target": "Name"},
        ],
    }
    assert validate_strict_api_names(config) == []


def test_strict_api_names_object_crud_and_record_type_access():
    config = {
        "file": "Accounts.csv",
        "object": "Account",
        "mappings": [{"source": "Account Name", "target": "Name"}],
        "record_type_mappings": {"source": "012-unavailable"},
        "operation": "upsert",
    }
    assert len(validate_strict_api_names(config)) == 2
    errors = validate_object_and_record_type_access(
        "Account",
        {"createable": True, "updateable": False, "recordTypeInfos": []},
        config,
    )
    assert any("cannot update" in error for error in errors)
    assert any("unavailable" in error for error in errors)


def test_strict_api_names_allows_intentional_id_to_external_id_and_casefold():
    config = {
        "file": "account.CSV",
        "object": "Account",
        "external_id": "External_Id__c",
        "mappings": [
            {"source": "Id", "target": "External_Id__c"},
            {"source": "name", "target": "Name"},
            {
                "source": "ownerid",
                "target": "OwnerId",
                "lookup_resolution": "external_id",
                "lookup_external_id": "External_Id__c",
            },
        ],
    }
    assert validate_strict_api_names(config) == []


def test_record_type_source_column_is_mapping_driven():
    config = {
        "mappings": [
            {"source": "SourceRecordType", "target": "RecordTypeId"},
        ]
    }
    assert _record_type_source_column(config) == "SourceRecordType"


def test_cleaner_rejects_duplicate_keys_and_wrong_reference_prefix():
    temp_dir = PROJECT_ROOT / "temp" / "flat_file_schema_validator_test"
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        source = temp_dir / "Account.csv"
        source.write_text(
            "External_Id__c,OwnerId\nDUP,001000000000000AAA\nDUP,005000000000000AAA\n",
            encoding="utf-8",
        )
        clean = temp_dir / "clean.csv"
        rejected = temp_dir / "rejected.csv"
        report = clean_csv_against_schema(
            source,
            clean,
            rejected,
            {
                "external_id": "External_Id__c",
                "mappings": [
                    {"source": "External_Id__c", "target": "External_Id__c"},
                    {"source": "OwnerId", "target": "OwnerId"},
                ],
            },
            [
                {"name": "External_Id__c", "type": "string"},
                {"name": "OwnerId", "type": "reference", "referenceTo": ["User"]},
            ],
            reference_key_prefixes={"OwnerId": {"005"}},
        )
        assert report["clean_count"] == 0
        assert report["rejected_count"] == 2
        rejected_text = rejected.read_text(encoding="utf-8-sig")
        assert "duplicate in-file key" in rejected_text
        assert "ID prefix" in rejected_text
    finally:
        for path in temp_dir.glob("*"):
            path.unlink()
        temp_dir.rmdir()


def test_stager_finds_cross_file_keys_with_duckdb():
    temp_dir = PROJECT_ROOT / "temp" / "stager_duplicate_key_test"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db_path = temp_dir / "staging.duckdb"
    stager = Stager(db_path)
    try:
        stager.write_raw_chunk(
            "Account",
            pl.DataFrame({"External_Id__c": ["A", "B"], "__source_file": ["a.csv", "a.csv"]}),
        )
        stager.write_raw_chunk(
            "Account",
            pl.DataFrame({"External_Id__c": ["A", "C"], "__source_file": ["b.csv", "b.csv"]}),
        )
        assert stager.find_duplicate_keys("Account", ["External_Id__c"]) == [
            {"External_Id__c": "A", "duplicate_count": 2}
        ]
    finally:
        stager.close()
        for path in temp_dir.glob("*"):
            path.unlink()
        temp_dir.rmdir()


def test_salesforce_semantic_ranges_and_lead_conversion_are_validated():
    assert validate_value("101", {"name": "Probability", "type": "percent"}) == (
        "value must be between 0 and 100"
    )
    assert validate_value("91", {"name": "Latitude", "type": "double"}) == (
        "latitude must be between -90 and 90"
    )
    assert validate_value("181", {"name": "Longitude", "type": "double"}) == (
        "longitude must be between -180 and 180"
    )
    assert validate_value("101", {"name": "BillingLatitude", "type": "double"}) == (
        "latitude must be between -90 and 90"
    )
    assert validate_value("-181", {"name": "ShippingLongitude", "type": "double"}) == (
        "longitude must be between -180 and 180"
    )
    assert "conversion state" in validate_value(
        "true", {"name": "IsConverted", "type": "boolean"}
    )
    assert validate_value("100", {"name": "Probability", "type": "percent"}) is None


def test_live_migration_allowed_values_are_enforced():
    field = {
        "name": "Status",
        "type": "picklist",
        "_migrationAllowedValues": ["Pending", "Draft"],
        "_migrationAllowedReason": "historical Contract insertion policy",
    }
    assert validate_value("Pending", field) is None
    assert "historical Contract insertion policy" in validate_value("Active", field)


def test_safe_normalization_preserves_picklist_text():
    assert _safe_normalize(" CCIR ", {"type": "picklist"}) == "CCIR"


def test_record_type_picklist_rejects_globally_active_but_unavailable_value():
    fields = {"BH_MCS_CameraType__c": {"PAL", "HD"}}
    assert _record_type_picklist_error(
        "CCIR", "BH_MCS_CameraType__c", "012-target", fields
    ) == "value is not available for target Record Type 012-target"
    assert _record_type_picklist_error(
        "PAL", "BH_MCS_CameraType__c", "012-target", fields
    ) is None


def test_unrestricted_picklist_is_not_treated_as_record_type_allow_list():
    assert not _enforce_record_type_picklist(
        {"type": "picklist", "restrictedPicklist": False}
    )
    assert _enforce_record_type_picklist(
        {"type": "picklist", "restrictedPicklist": True}
    )


def test_dependent_picklist_checks_controlling_value():
    fields = {
        "ApplicationType__c": {
            "values": {"Batteries", "Aviation"},
            "controller_values": {"Industrial": 0, "Aerospace": 1},
            "valid_for": {"Batteries": {0}, "Aviation": {1}},
        }
    }
    assert "not valid for controlling value" in _record_type_picklist_error(
        "Batteries",
        "ApplicationType__c",
        "012-target",
        fields,
        controller_value="Aerospace",
    )
    assert _record_type_picklist_error(
        "Batteries",
        "ApplicationType__c",
        "012-target",
        fields,
        controller_value="Industrial",
    ) is None


def test_dependent_multipicklist_checks_every_selected_value():
    fields = {
        "Capabilities__c": {
            "values": {"Battery", "Repair", "Flight"},
            "controller_values": {"Industrial": 0, "Aerospace": 1},
            "valid_for": {
                "Battery": {0},
                "Repair": {0, 1},
                "Flight": {1},
            },
        }
    }
    message = _record_type_picklist_error(
        "Battery;Flight",
        "Capabilities__c",
        "012-target",
        fields,
        controller_value="Industrial",
        field_type="multipicklist",
    )
    assert "Flight" in message
    assert _record_type_picklist_error(
        "Battery;Repair",
        "Capabilities__c",
        "012-target",
        fields,
        controller_value="Industrial",
        field_type="multipicklist",
    ) is None


def test_single_picklist_keeps_semicolon_in_label():
    fields = {
        "APTS_Sales_Team__c": {
            "values": {"WE3 - Spain; Portugal", "Brazil"},
            "controller_values": {"Western Europe (WE)": 0, "Brazil (Brazil)": 1},
            "valid_for": {
                "WE3 - Spain; Portugal": {0},
                "Brazil": {1},
            },
        }
    }
    assert _record_type_picklist_error(
        "WE3 - Spain; Portugal",
        "APTS_Sales_Team__c",
        "012000000000000AAA",
        fields,
        controller_value="Western Europe (WE)",
        field_type="picklist",
    ) is None


def test_cleaner_rejects_missing_record_type_developer_name_per_row():
    temp_dir = PROJECT_ROOT / "temp" / "record_type_developer_name_test"
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        source = temp_dir / "Account.csv"
        source.write_text(
            "Id,RecordTypeId,Name\nA1,Known_Type,Accepted\nA2,Missing_Type,Rejected\n",
            encoding="utf-8",
        )
        clean = temp_dir / "clean.csv"
        rejected = temp_dir / "rejected.csv"
        report = clean_csv_against_schema(
            source,
            clean,
            rejected,
            {
                "mappings": [
                    {"source": "Id", "target": "External_Id__c"},
                    {"source": "RecordTypeId", "target": "RecordTypeId"},
                    {"source": "Name", "target": "Name"},
                ],
                "record_type_mappings": {"Known_Type": "012000000000001AAA"},
            },
            [
                {"name": "External_Id__c", "type": "string"},
                {"name": "RecordTypeId", "type": "reference"},
                {"name": "Name", "type": "string"},
            ],
            {"012000000000001AAA": {}},
            valid_record_type_ids={"012000000000001AAA"},
        )
        assert report["clean_count"] == 1
        assert report["rejected_count"] == 1
        rejected_text = rejected.read_text(encoding="utf-8-sig")
        assert "Missing_Type" in rejected_text
        assert "does not exist or is unavailable" in rejected_text
    finally:
        for path in temp_dir.glob("*"):
            path.unlink()
        temp_dir.rmdir()


def test_controller_value_is_checked_for_target_record_type():
    fields = {"Country__c": {"values": {"US", "IN"}}}
    assert _record_type_picklist_error(
        "XX", "Country__c", "012-target", fields
    ) == "value is not available for target Record Type 012-target"


def test_required_fields_detect_missing_mapping_and_blank_values():
    required = {"Name": {}, "Required_Code__c": {}}
    assert _required_field_errors(
        {"AccountName": "", "Code": "ABC"},
        required,
        {"Name": "AccountName"},
    ) == [
        ("Name", "required value is blank"),
        ("Required_Code__c", "required target field is not mapped"),
    ]


def test_cleaner_row_limit_produces_a_true_sample():
    temp_dir = PROJECT_ROOT / "temp" / "flat_file_schema_sample_limit_test"
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        source = temp_dir / "Account.csv"
        source.write_text(
            "External_Id__c,Name\nA,One\nB,Two\nC,Three\n",
            encoding="utf-8",
        )
        report = clean_csv_against_schema(
            source,
            temp_dir / "clean.csv",
            temp_dir / "rejected.csv",
            {
                "external_id": "External_Id__c",
                "mappings": [
                    {"source": "External_Id__c", "target": "External_Id__c"},
                    {"source": "Name", "target": "Name"},
                ],
            },
            [
                {"name": "External_Id__c", "type": "string"},
                {"name": "Name", "type": "string"},
            ],
            row_limit=2,
        )
        assert report["rows_scanned"] == 2
        assert report["clean_count"] == 2
        assert report["sample_only"] is True
        assert "Three" not in (temp_dir / "clean.csv").read_text(encoding="utf-8-sig")
    finally:
        for path in temp_dir.glob("*"):
            path.unlink()
        temp_dir.rmdir()

def test_optional_duplicate_and_scalar_checks_can_be_skipped():
    temp_dir = PROJECT_ROOT / "temp" / "flat_file_optional_checks_test"
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        source = temp_dir / "Account.csv"
        source.write_text("External_Id__c,Website\nDUP,not-a-url\nDUP,also-bad\n", encoding="utf-8")
        report = clean_csv_against_schema(
            source, temp_dir / "clean.csv", temp_dir / "rejected.csv",
            {
                "operation": "upsert",
                "external_id": "External_Id__c",
                "mappings": [
                    {"source": "External_Id__c", "target": "External_Id__c"},
                    {"source": "Website", "target": "Website"},
                ],
            },
            [{"name": "External_Id__c", "type": "string"}, {"name": "Website", "type": "url"}],
            enabled_checks={"date_formats", "migration_key_present", "record_type_mapping", "record_type_picklists"},
        )
        assert report["clean_count"] == 2
        assert report["rejected_count"] == 0
    finally:
        for path in temp_dir.glob("*"):
            path.unlink()
        temp_dir.rmdir()


def test_migration_key_presence_and_scientific_notation_are_mandatory():
    temp_dir = PROJECT_ROOT / "temp" / "flat_file_mandatory_checks_test"
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        source = temp_dir / "Account.csv"
        source.write_text('External_Id__c\n""\n1.23E+17\n', encoding="utf-8")
        report = clean_csv_against_schema(
            source, temp_dir / "clean.csv", temp_dir / "rejected.csv",
            {"operation": "upsert", "external_id": "External_Id__c", "mappings": [{"source": "External_Id__c", "target": "External_Id__c"}]},
            [{"name": "External_Id__c", "type": "string"}], enabled_checks=set(),
        )
        assert report["rejected_count"] == 2
        rejected = (temp_dir / "rejected.csv").read_text(encoding="utf-8-sig")
        assert "migration key is blank" in rejected
        assert "scientific notation" in rejected
    finally:
        for path in temp_dir.glob("*"):
            path.unlink()
        temp_dir.rmdir()


def test_rejected_report_uses_active_target_fields_and_ignores_blank_targets():
    temp_dir = PROJECT_ROOT / "temp" / "flat_file_rejected_columns_test"
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        source = temp_dir / "CampaignMember.csv"
        source.write_text(
            "Id,Description,EventDate\n701000000000001AAA,ignored,8/6/26\n",
            encoding="utf-8",
        )
        rejected = temp_dir / "rejected.csv"
        report = clean_csv_against_schema(
            source,
            temp_dir / "clean.csv",
            rejected,
            {
                "operation": "upsert",
                "external_id": "External_Id__c",
                "mappings": [
                    {"source": "Id", "target": "External_Id__c", "ignored": False},
                    {"source": "Description", "target": None, "ignored": True},
                    {"source": "EventDate", "target": "EventDate", "ignored": False},
                ],
            },
            [
                {"name": "External_Id__c", "type": "string"},
                {"name": "EventDate", "type": "date"},
            ],
        )

        assert report["rejected_count"] == 1
        with rejected.open("r", encoding="utf-8-sig", newline="") as handle:
            row = next(csv.DictReader(handle))
        assert row["External_Id__c"] == "701000000000001AAA"
        assert "Description" not in row
        assert "Description: migration key" not in row["__validation_errors"]
        assert "EventDate" in row["__validation_errors"]
    finally:
        for path in temp_dir.glob("*"):
            path.unlink()
        temp_dir.rmdir()


if __name__ == "__main__":
    from tests._runner import run
    sys.exit(run(globals()))
