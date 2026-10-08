"""Quick unit checks for RecordType DeveloperName cache population."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from core.transformer import Transformer


class RecordTypeRemapTest(unittest.TestCase):
    def test_developer_name_values_query_target_by_developer_name(self) -> None:
        source = MagicMock()
        target = MagicMock()
        target.query.return_value = [
            {"Id": "012S9000005vykZIAQ", "DeveloperName": "DS_IT"},
            {"Id": "012S9000005vykrIAA", "DeveloperName": "Inspection"},
        ]
        transformer = Transformer(
            stager=MagicMock(),
            id_mapper=MagicMock(),
            source_connector=source,
            target_connector=target,
        )

        transformer._populate_record_type_cache(
            "GE_Installed_Base__c",
            ["DS_IT", "Inspection"],
        )

        source.query.assert_not_called()
        target.query.assert_called_once()
        soql = target.query.call_args.args[0]
        self.assertIn("DeveloperName IN", soql)
        self.assertIn("'DS_IT'", soql)
        self.assertIn("'Inspection'", soql)
        self.assertEqual(
            transformer._record_type_id_cache[("GE_Installed_Base__c", "DS_IT")],
            "012S9000005vykZIAQ",
        )
        self.assertEqual(
            transformer._record_type_id_cache[("GE_Installed_Base__c", "Inspection")],
            "012S9000005vykrIAA",
        )

    def test_salesforce_ids_still_resolve_via_source_then_target(self) -> None:
        source = MagicMock()
        target = MagicMock()
        source.query.return_value = [
            {
                "Id": "012A0000000jpV9IAI",
                "DeveloperName": "Inspection",
                "SobjectType": "GE_Installed_Base__c",
            }
        ]
        target.query.return_value = [
            {"Id": "012S9000005vykrIAA", "DeveloperName": "Inspection"},
        ]
        transformer = Transformer(
            stager=MagicMock(),
            id_mapper=MagicMock(),
            source_connector=source,
            target_connector=target,
        )

        transformer._populate_record_type_cache(
            "GE_Installed_Base__c",
            ["012A0000000jpV9IAI"],
        )

        source.query.assert_called_once()
        self.assertIn("WHERE Id IN", source.query.call_args.args[0])
        self.assertEqual(
            transformer._record_type_id_cache[("GE_Installed_Base__c", "012A0000000jpV9IAI")],
            "012S9000005vykrIAA",
        )


if __name__ == "__main__":
    unittest.main()
