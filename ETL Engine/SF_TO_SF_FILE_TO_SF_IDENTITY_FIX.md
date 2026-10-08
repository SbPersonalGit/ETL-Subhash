# SF to SF and File to SF Identity and Cyclic Lookup Fix

## Issue

Data migration templates were storing the referenced parent external ID on every lookup mapping, for example:

```text
Contact.AccountId -> Account.External_Id__c
```

The parent object already defined `External_Id__c` as its target matching field. Storing it again on child lookups created two sources of truth. The values could become inconsistent if the parent identity changed.

There were also related runtime issues:

- A renamed source lookup could be resolved using the wrong target relationship name.
- SF to SF extraction could query the target external-ID field from the source org instead of the parent's source key.
- File to SF cyclic references could contain business keys rather than Salesforce source IDs.
- Cyclic processing needed to defer the same key used by the referenced parent's ID map.

## Fix

Each object now has one normalized identity:

```text
Source key -> Target match field
```

SF to SF example:

```text
Account.Account_Number__c -> Account.External_Id__c
```

File to SF example:

```text
Account_Key CSV column -> Account.External_Id__c
```

For a referenced object included in the same pipeline, the lookup stores only its relationship intent:

```text
Referenced object: Account
Resolution: pipeline_object
```

Python derives the parent's source key and target match field. The child template does not independently own `Account.External_Id__c`.

Explicit `Parent.ExternalId` remains supported when the referenced object is outside the pipeline or already exists in the target org.

## Cyclic Processing

Cycle detection and two-pass execution remain runtime responsibilities.

Pass 1:

- Detect the cyclic lookup.
- Save the current record key and referenced parent key.
- Load the record without the cyclic lookup.
- Store the successful key-to-target-ID mapping.

Pass 2:

- Resolve the deferred parent key to its target Salesforce ID.
- Update the cyclic lookup field.

For SF to SF, the deferred key can be a source Salesforce ID or another configured source key.

For File to SF, the deferred key can be a CSV business key such as `ACC-100`. It is resolved against the same object identity used during loading.

## Additional Corrections

- In-pipeline lookup external IDs are derived from the parent identity.
- Conflicting child and parent external-ID definitions are rejected.
- Renamed lookup fields use target-org relationship metadata.
- Source extraction queries the referenced parent's source key.
- Existing templates are normalized at the backend boundary for compatibility.
- Data Seed behaviour was not intentionally changed.

## Verification

- Relevant SF to SF and File to SF regression tests passed.
- Shared loader and extension tests passed.
- Python compilation and JavaScript syntax checks passed.
- No live Salesforce migration was executed as part of this change.
