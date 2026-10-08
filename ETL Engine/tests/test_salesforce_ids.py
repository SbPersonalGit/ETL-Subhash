from tests._runner import ensure_project_root_on_path, run

ensure_project_root_on_path()

from core.salesforce_ids import (
    encode_salesforce_id_checksum,
    restore_salesforce_id_casing,
    restore_salesforce_ids_in_text,
)


def test_restore_salesforce_id_casing_recovers_mixed_case_from_lowercase():
    original = "a5qS9000000krPVIAY"
    assert encode_salesforce_id_checksum(original[:15]) == original
    assert restore_salesforce_id_casing(original.lower()) == original
    assert restore_salesforce_id_casing(original) == original


def test_restore_salesforce_id_casing_leaves_non_ids_alone():
    assert restore_salesforce_id_casing("ACC-100") == "ACC-100"
    assert restore_salesforce_id_casing("0011H000020uajx") == "0011H000020uajx"


def test_restore_salesforce_ids_in_frame_fixes_error_and_id_columns():
    import polars as pl
    from core.salesforce_ids import restore_salesforce_ids_in_frame

    original = "a5qS9000000krPVIAY"
    frame = pl.DataFrame({
        "sf__Error": [f"Foreign key external ID: {original.lower()} not found"],
        "Account.External_Id__c": [original],
        "Name": ["Keep me"],
    })
    restored = restore_salesforce_ids_in_frame(frame)
    assert original in restored["sf__Error"][0]
    assert original.lower() not in restored["sf__Error"][0].replace(original, "")
    assert restored["Account.External_Id__c"][0] == original


def test_restore_salesforce_ids_in_error_text():
    original = "0011H000020uajxQAA"
    error = (
        "INVALID_FIELD:Foreign key external ID: "
        f"{original.lower()} not found for field External_Id__c in entity Account"
    )
    restored = restore_salesforce_ids_in_text(error)
    assert original in restored
    assert original.lower() not in restored.replace(original, "")


if __name__ == "__main__":
    import sys

    sys.exit(run(globals()))
