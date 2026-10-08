# Source Org to Target Org Architecture Confirmation

## Executive summary

The proposed architecture is sound with several important amendments. The recommended model is:

> Two separate product entry flows and package stores, sharing one execution engine after raw staging.

```text
Flat File Migration                       Source Org Migration
-------------------                       --------------------
Flat File UI                              Source Org Builder
Flat-file templates                       Seed/source templates
CSV validation and cleaning               Source metadata/data validation
FlatFileInput                             Runtime Salesforce extraction
             \                            /
              └──── Shared raw staging ──┘
                           ↓
                Transform → Load → Audit
                           ↓
                  Shared Validation UI
```

## Confirmed understanding

The repository already has two input paths:

| Mode | UI | Origin | Engine path |
|---|---|---|---|
| Flat file | Flat File Migration | Client CSV files | File preparation → raw staging → transform → load |
| Salesforce source | Data Seed Builder | Source Salesforce org | Runtime extraction → raw staging → transform → load |

The following architectural conclusions are correct:

- Source Salesforce extraction already exists.
- Flat files skip extraction and enter through raw staging.
- Source Salesforce data should be extracted at runtime.
- Stager, Transformer, Loader, IDMapper, Pipeline Runs and audit should remain shared.
- Source Salesforce records should not be converted into Flat File package inputs merely to reuse the CSV workflow.
- Runs should remain single-mode initially.
- Mixed Flat File and Source Salesforce pipelines in one run should be deferred.
- Most implementation effort is in validation and UX parity rather than the ETL engine.

## Product isolation with shared infrastructure

The two entry products should remain insulated:

- Flat File Migration
- Source Org Migration

Their package stores should also remain separate initially:

```text
config/flat_file_templates/   → Flat File packages
config/templates/             → Source Org packages
```

Both package types should use a small common envelope:

```json
{
  "input_mode": "flat_file | salesforce_source",
  "source_profile": "",
  "target_profile": "default",
  "prevalidation_status": "PENDING",
  "objects": []
}
```

The following components should remain shared and become mode-aware:

- Pipeline Designer
- Validation Center
- Pipeline Runs
- Runtime audit
- Transformation
- Loading
- Post-validation reporting

This approach preserves product isolation without duplicating the execution engine.

## Current Source Org capability

The existing Data Seed Builder is primarily a seed-and-topology workflow rather than a completely general Source-to-Target mapping product.

It already supports:

- Source connection
- Seed-object selection
- Relationship topology
- Source extraction
- Raw staging
- Transformation
- Loading

It does not yet provide full parity with the Flat File product for:

- Explicit source-object to target-object mapping
- Full source-field to target-field mapping
- External ID configuration UX
- Lookup-resolution policy
- Record Type mapping and validation
- Dependent-picklist validation
- Source-aware pre-validation status

Renaming the Data Seed Builder alone would therefore not complete the Source Org Migration feature.

## Object-mapping scope

For the first Source Org release, source and target object API names should be required to match:

```text
Source Account → Target Account
Source Case → Target Case
```

Field mappings may differ:

```text
Source Legacy_Id__c → Target External_Id__c
Source Customer_Name__c → Target Name
```

Supporting the same-object model is a medium-impact enhancement.

Arbitrary object mapping should be deferred:

```text
Source Legacy_Customer__c → Target Account
```

Arbitrary object mapping affects:

- Dependency calculation
- Source metadata profiles
- Transformation table naming
- Lookup relationship resolution
- IDMapper keys
- Validation reports
- Artifact naming
- Resume state

It should be introduced later unless it is an immediate business requirement.

## Runtime execution

The Source Org package should store configuration, not source records:

- Source and target profiles
- Objects
- Selected fields
- SOQL filters
- Scope and topology rules
- Field mappings
- External IDs
- Lookup policies
- Record Type policies

When a Source Org migration starts, the system should extract the source records at runtime:

```text
Run submitted
    ↓
Connect to source and target
    ↓
Bulk API source extraction
    ↓
Raw staging
    ↓
Full target-compatibility validation
    ↓
Transform
    ↓
Load
    ↓
Post-validation
```

CSV files may still be generated under the run export directory for audit and replay. These files are execution artifacts, not Flat File package inputs.

## Validation architecture

Source Org mode does not need literal CSV parsing and cleaning. Salesforce already guarantees the basic validity of data stored in the source org.

Source Org records still require target-compatibility validation:

- Source and target object permissions
- Source queryability
- Target field create/update access
- Source-to-target field compatibility
- External ID availability
- Lookup-resolution configuration
- Target Record Type availability
- Target picklist availability
- Dependent-picklist combinations
- Target string length
- Numeric precision and scale
- Required target fields
- Reference integrity
- Duplicate migration keys

### Pre-run validation

Before starting extraction, validate:

- Source and target connectivity
- Object permissions
- Source queryability
- Target create/update permissions
- Field mappings
- External IDs
- Lookup configuration
- Target Record Types
- Target picklist metadata
- SOQL syntax
- An optional sample of source records

### Post-extraction validation

After extraction and before transformation/loading, validate every staged record for:

- Missing target Record Type DeveloperName
- Invalid picklist values for the resolved target Record Type
- Invalid dependent-picklist combinations
- Target field length, precision and scale
- Required-field availability
- Unresolvable references
- Duplicate external IDs

Sampled pre-validation must not be treated as proof that every runtime record is valid. Full post-extraction validation should be a mandatory gate.

## Pipeline contract

The shared pipeline should use an explicit input contract.

Flat File input:

```yaml
input:
  type: flat_file
  template_id: customer_files
```

Source Salesforce input:

```yaml
input:
  type: salesforce_source
  source_profile: production
```

Execution routing should be:

```text
flat_file
  → ingest prepared files
  → force start stage to transform

salesforce_source
  → construct source connector
  → start at extract
```

The current global behavior changes the entire run to Flat File mode when any Flat File stage exists. This is the primary reason mixed-mode runs should remain unsupported initially.

## Shared staging boundary

Both input modes should produce the same logical raw-staging contract:

```text
FlatFileInput ─────────────┐
                          ├── Raw staging → Validate → Transform → Load
SalesforceOrgInput ───────┘
```

The mode-specific responsibilities end at the staging boundary:

| Responsibility | Flat File | Source Org |
|---|---|---|
| Source discovery | Folder and CSV headers | Salesforce describe metadata |
| Data acquisition | Read prepared CSV | Bulk API runtime extraction |
| Input-specific checks | Encoding, headers, types and malformed rows | Connectivity, queryability and SOQL |
| Target compatibility | Shared validation rules | Shared validation rules |
| Raw staging | Shared contract | Shared contract |
| Transform and load | Shared | Shared |

## Impact assessment

| Area | Impact | Reason |
|---|---:|---|
| Source and target connectors | Low | Source and target profiles already exist |
| Source extraction | Low | Bulk and topology extraction already exist |
| Raw staging | Low | Both modes already converge on staging |
| Loader and IDMapper | Low | Downstream behavior remains shared |
| Transformer | Low–Medium | Must respect explicit source-field mappings and mode-specific metadata |
| Source package contract | Medium | Requires explicit input mode and lifecycle metadata |
| Object and field mapping UX | Medium–High | Source builder needs Flat-File-style mapping parity |
| Source pre-validation | High | Requires source/target metadata comparison and optional source queries |
| Full post-extraction validation | Medium–High | Every staged record must be checked against target metadata |
| Post-validation | Medium–High | Must compare raw/transformed Source Org artifacts with target records |
| Pipeline Designer and Runs | Medium | Must become mode-aware and show extraction stages appropriately |
| Mixed-mode runs | High | Requires per-stage connector and input routing; defer initially |

The overall architecture impact is medium rather than a rewrite. Approximately 70% of the execution engine can be reused, with most new work concentrated in validation and user-experience parity.

## Recommended delivery phases

1. Keep Flat File and Source Org pages and package stores separate.
2. Add `input_mode` and common package lifecycle metadata.
3. Require matching source and target object API names for the initial release.
4. Enhance Source Org field, external-ID and lookup mapping.
5. Add source metadata and sampled-data pre-validation.
6. Add mandatory post-extraction full-record validation.
7. Make Pipeline Designer, Pipeline Runs and Validation Center mode-aware.
8. Add Source Org post-validation using raw and transformed run artifacts.
9. Consider arbitrary source-object to target-object mapping later.
10. Consider mixed-mode runs only after both isolated modes are stable.

## Architectural decisions

The current decisions are:

- Develop the Source Org capability on the `SF_to_SF` branch.
- Keep `File_to_SF` unchanged as a stable backup.
- Keep Flat File Migration and Source Org Migration insulated as user-facing products.
- Reuse the existing execution engine and staging model.
- Extract Source Org data at runtime.
- Do not force Source Org data through the Flat File package workflow.
- Use single-mode runs initially.
- Defer mixed-mode runs and arbitrary cross-object mapping.

## Conclusion

The two product entry flows should remain insulated, while execution and reporting infrastructure remain shared. Source records should be extracted at runtime into raw staging and should then pass through target-compatibility validation before transformation and loading.

This design protects the stable Flat File product, reuses the mature Source extraction capability, avoids parallel Transformer and Loader implementations, and provides a controlled path toward full Source Org Migration parity.
