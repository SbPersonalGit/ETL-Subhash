# Parked Designs Register

This register captures agreed product designs that are intentionally not scheduled for implementation yet. It prevents deferred ideas from being lost or being mistaken for active work.

Status vocabulary:

- **Parked — designed:** direction and boundaries are agreed; implementation is not started.
- **Parked — needs design:** requirement is known, but design decisions remain.
- **Activated:** moved into an implementation plan or active backlog.

---

## PARK-001 — Sequential file-based post-validation in the existing Pipeline Designer

**Status:** Parked — designed  
**Recorded:** 2026-08-24

### Problem

Sequential post-validation currently supports template-based stages. File-based post-validation can run one configuration at a time, or validate multiple files for the same object together by selecting their folder, but it cannot save and execute an ordered multi-object sequence.

Creating a separate file-validation designer would give users two different experiences for the same activity.

### Agreed product direction

Extend the existing Post-Validation experience and existing Pipeline Designer. Do not introduce a separate designer.

The Post-Validation page keeps two entry paths:

1. **Quick validation** — run one template-based or file-based validation.
2. **Sequential validation** — open the existing Pipeline Designer and create an ordered validation pipeline.

The designer presents a common stage experience: add, reorder, save, run, monitor, stop/continue on failure, open reports, and download ZIPs. A stage carries a visible source badge such as **Template** or **File**.

### Stage model

Add a `validation_source_type` discriminator to each validation stage:

```json
{
  "validation_source_type": "template | file",
  "object_name": "Case",
  "failure_policy": "stop | continue",
  "template_config": {},
  "file_config": {
    "file_source": "path",
    "file_path": "C:\\data\\Case",
    "source_id_column": "Id",
    "target_match_field": "Legacy_ID__c",
    "file_query_mode": "custom_soql",
    "custom_soql": "SELECT ...",
    "lookup_mappings": []
  }
}
```

Only the configuration matching `validation_source_type` is active.

### Delivery approach

- Design the persisted model for mixed Template/File stages from the beginning.
- First release may restrict a pipeline to one source type to reduce UI and runtime complexity.
- Enable mixed-source stages later without changing the saved-pipeline schema.
- Use the existing file-validation runner for File stages and the existing template-validation runner for Template stages.
- Create one parent sequence run with a child activity/output directory per stage.
- Reuse existing progress pages, mismatch partitions, field/source grouping, target artifacts, and ZIP downloads.
- Lock all stages to one configured target profile and reject mixed-target pipelines.
- Preserve stage-level resume and failure state; never rerun completed stages implicitly.

### Important UX distinction

Multiple source files for one object are already handled as one File stage by selecting a folder. Sequential stages are intended for different objects or meaningfully different validation configurations—not merely file splitting.

### Acceptance criteria for future activation

- Users can choose Template or File without leaving the existing Post-Validation/Pipeline Designer experience.
- Users can save, reorder, and sequentially execute validation stages.
- Each stage has independent progress, reports, partitions, errors, and ZIP downloads.
- The parent run reports aggregate status and links to every stage.
- Stop/continue-on-failure behavior is explicit and resumable.
- All stages use the single target org/profile selected by the app.
- Existing quick validation and existing template pipelines remain backward compatible.

---

## PARK-002 — Mixed Flat File and Source Salesforce migration stages in one run

**Status:** Parked — designed at architecture level

Flat File and Source Salesforce entry products remain separate, while sharing staging, transformation, loading, validation, run audit, and Pipeline Designer infrastructure. Runs remain single-mode initially. A future mixed-mode run would require a common stage envelope, consistent identity rules, profile locking, dependency calculation, and resumable orchestration.

See [Source Org to Target Org Architecture Confirmation](sf_to_sf_architecture_confirmation.md).

---

## PARK-003 — Arbitrary source-object to target-object mapping

**Status:** Parked — designed at architecture level

The initial Source Org model requires matching object API names while allowing field mappings to differ. Mapping a different source object to a target object—for example `Legacy_Customer__c` to `Account`—is deferred because it affects dependency calculation, metadata profiles, staging/table identity, lookup resolution, IDMapper keys, validation artifacts, and resume state.

See [Source Org to Target Org Architecture Confirmation](sf_to_sf_architecture_confirmation.md#object-mapping-scope).

---

## Related backlog, not a parked product design

Polymorphic `OwnerId` reference validation remains an unfixed bug backlog item rather than a parked feature. See [ISS-012 in the Issues Log](issues_log.md#iss-012--polymorphic-ownerid-reference-validation-is-skipped).

