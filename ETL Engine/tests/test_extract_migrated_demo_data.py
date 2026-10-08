from extract_migrated_demo_data import extraction_paths


def test_extraction_paths_uses_relationship_external_id_for_lookup():
    mappings = [
        {"source": "Id", "target": "External_Id__c"},
        {
            "source": "AccountId",
            "target": "AccountId",
            "referenced_object": "Account",
            "lookup_external_id": "External_Id__c",
        },
        {
            "source": "Pricebook2Id",
            "target": "Pricebook2Id",
            "referenced_object": "Pricebook2",
            "lookup_external_id": "",
        },
    ]
    fields = [
        {"name": "AccountId", "relationshipName": "Account"},
        {"name": "Pricebook2Id", "relationshipName": "Pricebook2"},
    ]

    paths = extraction_paths(mappings, fields)

    assert [path for _, path in paths] == [
        "External_Id__c",
        "Account.External_Id__c",
        "Pricebook2Id",
    ]


def test_extraction_paths_supports_custom_relationship_name():
    mappings = [{
        "source": "Parent__c",
        "target": "Parent__c",
        "referenced_object": "Parent__c",
        "lookup_external_id": "External_Id__c",
    }]
    fields = [{"name": "Parent__c", "relationshipName": "Parent__r"}]

    assert extraction_paths(mappings, fields)[0][1] == "Parent__r.External_Id__c"
