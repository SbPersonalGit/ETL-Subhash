"""Focused tests for bounded-memory file-validation preparation."""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.flat_file import open_csv
from file_validate_target import _custom_soql_source_projection
from tests._runner import run


def test_file_validation_reads_cp1252_nationality_names():
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "Nationality__c.v1.csv"
        path.write_bytes(b"Id,NAME\naAZ001,Caf\xe9\n")
        with open_csv(path) as handle:
            rows = list(csv.DictReader(handle))
        assert rows[0]["NAME"] == "Café"


def test_custom_soql_projection_keeps_only_comparable_direct_fields():
    manifest = {
        "file_query_mode": "custom_soql",
        "custom_soql": "SELECT Id, Name, Amount__c, Parent.External_Id__c FROM Case",
        "lookup_mappings": [{"source_field": "ParentId"}],
    }
    projection = _custom_soql_source_projection(manifest, "Id", "External_Id__c")
    assert projection == {"Id", "External_Id__c", "Name", "Amount__c", "ParentId"}


def test_object_template_mode_does_not_project_before_metadata_resolution():
    manifest = {"file_query_mode": "object_template", "include_fields": ["Name"]}
    assert _custom_soql_source_projection(manifest, "Id", "External_Id__c") is None


if __name__ == "__main__":
    run(globals())
