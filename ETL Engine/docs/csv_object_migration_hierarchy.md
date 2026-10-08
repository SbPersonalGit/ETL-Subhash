# Partner Central CSV Object Migration Hierarchy

Source: `C:\\Users\\puneetmalhotra\\Downloads\\OneDrive_2026-09-07\\04. Partner Central`

## Scope rules

- Each XLSX filename is treated as its Salesforce object API name.
- `User` and `RecordType` are excluded from hierarchy ordering.
- Referenced objects without a workbook in this folder are treated as already migrated.
- Only populated relationships affect ordering.
- Target Org Describe metadata was used where available. Source-export fields absent from the Target Org were also identified by matching their populated Salesforce ID prefixes to an in-scope object's `Id` prefix; those inferred edges are marked below.

## Recommended migration sequence

### Phase 1 - roots

These objects have no outstanding cross-object dependency within this folder:

- `Contact`
- `SFPRM_Approval_List__c`
- `SFPRM_Document_Access__c`
- `SFPRM_Download_Assets__c`
- `SFPRM_DS_Commission_Agreement_Data__c`
- `SFPRM_DS_Order__c`
- `SFPRM_Intermediary_Workflow__c`
- `SFPRM_MAP_Action_Plans__c`
- `SFPRM_Onboarding__c`
- `SFPRM_Order_Approval_Matrix__c`
- `SFPRM_Order_Data__c`
- `SFPRM_PAP_Group__c`
- `SFPRM_Potential_Opportunity__c`
- `SFPRM_PRM_User_request__c`
- `SFPRM_SalesRep_Deals__c`
- `SFPRM_User_Approval_List__c`

`Contact.ReportsToId` contains one self-reference. Load Contacts using external-ID relationship resolution or defer that one value and update it after the base Contact insert.

### Phase 2 - direct children and Survey/PMP cycle

- `SFPRM_DS_Invoice__c` after `SFPRM_DS_Order__c`
- `SFPRM_DS_Order_Line_Item__c` after `SFPRM_DS_Order__c` (source-export inferred)
- `SFPRM_Onboarding_Checklist__c` after `SFPRM_Onboarding__c`
- `SFPRM_Survey__c` and `SFPRM_PMP_Commercial_Line__c` after `SFPRM_PAP_Group__c`, using the cycle procedure below

The source exports contain a circular reference:

```text
SFPRM_Survey__c
<-> SFPRM_PMP_Commercial_Line__c
```

Safe procedure:

1. Insert `SFPRM_Survey__c` with `PMP_Commercial_Line__c` deferred.
2. Insert `SFPRM_PMP_Commercial_Line__c`, resolving `SFPRM_PAP_Group__c` and `SFPRM_Survey__c`.
3. Update `SFPRM_Survey__c.PMP_Commercial_Line__c` after both objects exist.

The two cross-reference fields are present in the source workbooks but absent from the current Target Org Describe. Confirm whether they must be created, renamed, or intentionally omitted before building the final templates.

### Phase 3 - grandchildren

- `SFPRM_DS_Collection__c` after both `SFPRM_DS_Order__c` and `SFPRM_DS_Invoice__c`
- `SFPRM_Invoice_Line__c` after `SFPRM_DS_Invoice__c` (source-export inferred; object is absent from current Target Org Describe)
- `SFPRM_OP_Action_Plan__c` after both `SFPRM_PMP_Commercial_Line__c` and `SFPRM_Survey__c` (source-export inferred)
- `SFPRM_Survey_Criteria__c` after `SFPRM_Survey__c`

### Phase 4 - final child

- `SFPRM_Commission_Paid_Details__c` after both `SFPRM_DS_Collection__c` and `SFPRM_DS_Order__c`

## Dependency tree

```text
SFPRM_DS_Order__c
|-- SFPRM_DS_Order_Line_Item__c [inferred]
|-- SFPRM_DS_Invoice__c
|   |-- SFPRM_Invoice_Line__c [inferred]
|   `-- SFPRM_DS_Collection__c
|       `-- SFPRM_Commission_Paid_Details__c
`-- SFPRM_Commission_Paid_Details__c

SFPRM_Onboarding__c
`-- SFPRM_Onboarding_Checklist__c

SFPRM_PAP_Group__c
|-- SFPRM_Survey__c
|   |-- SFPRM_Survey_Criteria__c
|   |-- SFPRM_OP_Action_Plan__c [inferred]
|   `-- SFPRM_PMP_Commercial_Line__c [circular source reference]
`-- SFPRM_PMP_Commercial_Line__c
    |-- SFPRM_OP_Action_Plan__c [inferred]
    `-- SFPRM_Survey__c [circular source reference]
```

## Populated in-scope relationships

| Parent | Child | Child relationship field | Populated rows | Evidence |
|---|---|---|---:|---|
| `SFPRM_DS_Order__c` | `SFPRM_DS_Invoice__c` | `SFPRM_Order__c` | 3,123 | Target Describe |
| `SFPRM_DS_Order__c` | `SFPRM_DS_Collection__c` | `SFPRM_Order__c` | 3,143 | Target Describe |
| `SFPRM_DS_Invoice__c` | `SFPRM_DS_Collection__c` | `SFPRM_Invoice__c` | 3,143 | Target Describe |
| `SFPRM_DS_Collection__c` | `SFPRM_Commission_Paid_Details__c` | `SFPRM_Collection__c` | 3,143 | Target Describe |
| `SFPRM_DS_Order__c` | `SFPRM_Commission_Paid_Details__c` | `SFPRM_Order_Number__c` | 3,143 | Target Describe |
| `SFPRM_DS_Order__c` | `SFPRM_DS_Order_Line_Item__c` | `SFPRM_Order__c` | 6,268 | Source ID prefix; field absent in target |
| `SFPRM_DS_Invoice__c` | `SFPRM_Invoice_Line__c` | `SFPRM_Invoice_Number__c` | 13 | Source ID prefix; object absent in target |
| `SFPRM_Onboarding__c` | `SFPRM_Onboarding_Checklist__c` | `SFPRM_Onboarding__c` | 12,158 | Target Describe |
| `SFPRM_PAP_Group__c` | `SFPRM_PMP_Commercial_Line__c` | `SFPRM_PAP_Group__c` | 1,506 | Target Describe |
| `SFPRM_PAP_Group__c` | `SFPRM_Survey__c` | `SFPRM_PAP_Group__c` | 669 | Target Describe |
| `SFPRM_Survey__c` | `SFPRM_Survey_Criteria__c` | `SFPRM_Survey__c` | 20,290 | Target Describe |
| `SFPRM_PMP_Commercial_Line__c` | `SFPRM_OP_Action_Plan__c` | `SFPRM_Commercial_Line__c` | 1,521 | Source ID prefix; field absent in target |
| `SFPRM_Survey__c` | `SFPRM_OP_Action_Plan__c` | `SFPRM_Survey_Number__c` | 1,470 | Source ID prefix; field absent in target |
| `SFPRM_Survey__c` | `SFPRM_PMP_Commercial_Line__c` | `SFPRM_Survey__c` | 1,470 | Source ID prefix; field absent in target |
| `SFPRM_PMP_Commercial_Line__c` | `SFPRM_Survey__c` | `PMP_Commercial_Line__c` | 669 | Source ID prefix; field absent in target |

References to `Account`, `Opportunity`, `GE_HQ_Region__c`, `PRM_Channel_Appointment_GE_OG__c`, and all other objects without a workbook are intentionally excluded as already migrated. All `User`, audit-user, and `RecordType` references are excluded by instruction.
