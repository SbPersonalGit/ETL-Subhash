from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests._runner import ensure_project_root_on_path

ensure_project_root_on_path()

from core.source_data_prevalidate import validate_incoming_source_data


class FakeConnector:
    def __init__(self, responses: list[tuple[str, list[dict]]], describes: dict[str, dict] | None = None):
        self.responses = responses
        self.describes = describes or {}

    def query(self, soql: str):
        for key, rows in self.responses:
            if key in soql:
                return rows
        raise AssertionError(f"Unexpected SOQL: {soql}")

    def describe_object(self, object_name: str):
        return self.describes.get(object_name) or {"recordTypeInfos": []}

    def describe_record_type_picklists(self, object_name: str, record_type_id: str):
        return {
            "State__c": {
                "values": {"CA", "NY", "ON"},
                "controller_values": {"US": 0, "CA": 1},
                "valid_for": {
                    "CA": {0},
                    "NY": {0},
                    "ON": {1},
                },
            }
        }


def test_record_type_and_picklist_and_dependent_checks():
    source = FakeConnector([
        ("GROUP BY RecordType.DeveloperName", [
            {"developerName": "Enterprise"},
            {"developerName": "Missing_RT"},
        ]),
        ("FROM RecordType WHERE SobjectType = 'Account'", [
            {"Id": "012SRC1", "DeveloperName": "Enterprise", "Name": "Enterprise"},
            {"Id": "012SRC2", "DeveloperName": "Missing_RT", "Name": "Missing"},
        ]),
        ("GROUP BY Industry", [
            {"fieldValue": "Agriculture"},
            {"fieldValue": "NotARealIndustry"},
        ]),
        ("GROUP BY Country__c, State__c", [
            {"controllerValue": "US", "dependentValue": "CA"},
            {"controllerValue": "US", "dependentValue": "ON"},
        ]),
        ("GROUP BY Country__c", [
            {"fieldValue": "US"},
        ]),
        ("GROUP BY State__c", [
            {"fieldValue": "CA"},
            {"fieldValue": "ON"},
        ]),
    ])
    target = FakeConnector(
        [
            ("IsActive = true", [
                {"Id": "012TGT1", "DeveloperName": "Enterprise", "Name": "Enterprise", "IsActive": True},
            ]),
        ],
        describes={
            "Account": {
                "recordTypeInfos": [{
                    "recordTypeId": "012TGT1",
                    "defaultRecordTypeMapping": True,
                    "available": True,
                }],
            }
        },
    )
    issues = validate_incoming_source_data(
        config={
            "source_object": "Account",
            "target_object": "Account",
            "source_fields": ["RecordTypeId", "Industry", "Country__c", "State__c"],
            "field_pairs": [
                ("RecordTypeId", "RecordTypeId"),
                ("Industry", "Industry"),
                ("Country__c", "Country__c"),
                ("State__c", "State__c"),
            ],
            "record_type_mappings": {},
        },
        source=source,
        target=target,
        source_fields={},
        target_fields={
            "Industry": {
                "type": "picklist",
                "picklistValues": [
                    {"value": "Agriculture", "active": True},
                    {"value": "Banking", "active": True},
                ],
            },
            "Country__c": {
                "type": "picklist",
                "picklistValues": [
                    {"value": "US", "active": True},
                    {"value": "CA", "active": True},
                ],
            },
            "State__c": {
                "type": "picklist",
                "dependentPicklist": True,
                "controllerName": "Country__c",
                "picklistValues": [
                    {"value": "CA", "active": True},
                    {"value": "NY", "active": True},
                    {"value": "ON", "active": True},
                ],
            },
        },
    )

    messages = [issue["message"] for issue in issues]
    assert any("Missing_RT" in message for message in messages)
    assert any("NotARealIndustry" in message for message in messages)
    assert any("controlling/dependent" in message for message in messages)


if __name__ == "__main__":
    from tests._runner import run
    raise SystemExit(run(globals()))
