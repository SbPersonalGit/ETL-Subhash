from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests._runner import ensure_project_root_on_path
ensure_project_root_on_path()

from transforms.rules import apply_rule


def test_field_rename_source_id_overwrites_existing_external_id_without_duplicate():
    incoming = pl.DataFrame({
        "Id": ["001SOURCE"],
        "External_Id__c": ["OLD-LEGACY"],
        "Name": ["Example"],
    })

    result = apply_rule(incoming, {
        "type": "field_rename",
        "source": "Id",
        "target": "External_Id__c",
    })

    assert result.columns == ["External_Id__c", "Name"]
    assert result["External_Id__c"].to_list() == ["001SOURCE"]


def test_field_rename_keeps_supplied_external_id_when_source_id_is_absent():
    incoming = pl.DataFrame({
        "External_Id__c": ["FILE-SOURCE-KEY"],
        "Name": ["Example"],
    })

    result = apply_rule(incoming, {
        "type": "field_rename",
        "source": "Id",
        "target": "External_Id__c",
    })

    assert result["External_Id__c"].to_list() == ["FILE-SOURCE-KEY"]


def test_incomplete_null_default_rule_is_a_safe_noop():
    incoming = pl.DataFrame({"AnnualRevenue": [None, "1250"]})
    result = apply_rule(incoming, {
        "type": "null_default",
        "field": "AnnualRevenue",
    })
    assert result.to_dict(as_series=False) == incoming.to_dict(as_series=False)


def test_text_to_decimal_parses_formatted_text_and_rounds():
    incoming = pl.DataFrame({
        "AnnualRevenue": ["1,250.755", "$42.10", "(18.25)", " 7 "],
    })
    result = apply_rule(incoming, {
        "type": "text_to_decimal",
        "field": "AnnualRevenue",
        "invalid_value": "null",
        "decimal_places": 2,
    })
    assert result["AnnualRevenue"].to_list() == [1250.76, 42.1, -18.25, 7.0]


def test_text_to_decimal_supports_null_or_zero_for_invalid_values():
    incoming = pl.DataFrame({"AnnualRevenue": ["not-a-number", "", None]})
    as_null = apply_rule(incoming, {
        "type": "text_to_decimal",
        "field": "AnnualRevenue",
        "invalid_value": "null",
    })
    as_zero = apply_rule(incoming, {
        "type": "text_to_decimal",
        "field": "AnnualRevenue",
        "invalid_value": "zero",
    })
    assert as_null["AnnualRevenue"].to_list() == [None, None, None]
    assert as_zero["AnnualRevenue"].to_list() == [0.0, 0.0, 0.0]


def test_boolean_convert_maps_one_zero_to_true_false():
    incoming = pl.DataFrame({"Active__c": ["1", "0", None, "invalid"]})
    result = apply_rule(incoming, {
        "type": "boolean_convert",
        "field": "Active__c",
        "direction": "number_to_boolean",
    })
    assert result["Active__c"].to_list() == [True, False, None, None]


def test_boolean_convert_maps_true_false_to_one_zero_case_insensitively():
    incoming = pl.DataFrame({
        "Active__c": ["true", "FALSE", "1", "0", None, "invalid"],
    })
    result = apply_rule(incoming, {
        "type": "boolean_convert",
        "field": "Active__c",
        "direction": "boolean_to_number",
    })
    assert result["Active__c"].to_list() == [1, 0, 1, 0, None, None]


def test_date_convert_mdy_two_digit_year_to_salesforce_date():
    incoming = pl.DataFrame({"StartDate": ["4/1/16", "12/31/17", None, "bad"]})
    result = apply_rule(incoming, {
        "type": "date_convert",
        "field": "StartDate",
        "source_format": "mdy2",
        "invalid_value": "retain",
    })
    assert result["StartDate"].to_list() == ["2016-04-01", "2017-12-31", None, "bad"]


def test_date_convert_mdy_two_digit_year_accepts_excel_midnight_suffix():
    incoming = pl.DataFrame({"EndDate": ["7/14/27 0:00", "12/31/24 00:00:00", "7/14/27 1:00"]})
    result = apply_rule(incoming, {
        "type": "date_convert",
        "field": "EndDate",
        "source_format": "mdy2",
        "invalid_value": "retain",
    })
    assert result["EndDate"].to_list() == ["2027-07-14", "2024-12-31", "7/14/27 1:00"]


def test_date_convert_dmy_four_digit_year_with_hyphens_to_salesforce_date():
    incoming = pl.DataFrame({"End_Date__c": ["31-12-2022", "1-7-2023", None, "bad"]})
    result = apply_rule(incoming, {
        "type": "date_convert",
        "field": "End_Date__c",
        "source_format": "dmy4_dash",
        "invalid_value": "retain",
    })
    assert result["End_Date__c"].to_list() == ["2022-12-31", "2023-07-01", None, "bad"]


def test_date_convert_dmy_two_digit_year_with_hyphens_to_salesforce_date():
    incoming = pl.DataFrame({"ActivityDate": ["23-10-25", "1-7-26", None, "bad"]})
    result = apply_rule(incoming, {
        "type": "date_convert", "field": "ActivityDate",
        "source_format": "dmy2_dash", "invalid_value": "retain",
    })
    assert result["ActivityDate"].to_list() == ["2025-10-23", "2026-07-01", None, "bad"]


if __name__ == "__main__":
    import sys
    from tests._runner import run
    sys.exit(run(globals()))
