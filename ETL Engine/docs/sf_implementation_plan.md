# SF ETL — Salesforce Implementation Plan

> **Context**: This document captures the full architecture for moving the ETL admin panel
> (currently a local Flask app) into Salesforce LWC. The Python ETL engine stays on AWS.
> Salesforce is the **control plane**; AWS is the **execution plane**.
>
> **Local dev bridge**: Use `ngrok http 5000` to expose the local Flask server.
> Create a Named Credential `ETL_Backend` pointing to the ngrok URL.
> When moving to AWS, only the Named Credential URL changes — no code changes.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                     SALESFORCE ORG                          │
│                                                             │
│  LWC Admin Panel                                            │
│  ├── Template Builder  (port of index.html)                 │
│  ├── Pipeline Designer (port of pipeline_designer.html)     │
│  └── Run Monitor       (real-time status)                   │
│           │ Apex Callout                  │ Platform Events  │
│  ┌────────▼────────┐          ┌───────────▼──────────┐     │
│  │ Custom Objects  │          │  ETL_Run_Status__e   │     │
│  │ (data model)    │          │  (Platform Event)    │     │
│  └────────┬────────┘          └───────────▲──────────┘     │
│           │ Named Credential              │ SF REST API     │
└───────────┼───────────────────────────────┼─────────────────┘
            │                               │
┌───────────▼───────────────────────────────┴─────────────────┐
│                        AWS                                   │
│  API Gateway → ECS/EC2 (Python ETL Engine)                  │
│  ├── DynamoDB  (run state)                                   │
│  ├── S3        (logs, export CSVs, failed record files)      │
│  └── CloudWatch (metrics)                                    │
│           │                                                  │
│     Source SF Org ←──────────── Target SF Org               │
└─────────────────────────────────────────────────────────────┘
```

---

## Data Model — Custom Objects

### ETL_Migration__c
Top-level container for a named migration project.

| Field | Type | Notes |
|---|---|---|
| Name | Text | e.g. "CRM-Itinerary" |
| Migration_Id__c | Text (External ID) | Unique slug |
| Is_Active__c | Checkbox | Current working migration |
| Description__c | Text Area | |

---

### ETL_Template__c
One template = one seed object + traversal tree + field selections.
Replaces the current `.json` template files in `config/templates/`.

| Field | Type | Notes |
|---|---|---|
| Name | Text | e.g. "Itinerary Child" |
| Template_Id__c | Text (External ID) | e.g. "Itinerary-_Child" |
| Seed_Object__c | Text | e.g. "Itinerary__c" |
| Object_Count__c | Number | |
| Last_Used__c | DateTime | |
| Is_Active__c | Checkbox | |

---

### ETL_Template_Node__c  ← THE TREE
One record per node in the traversal tree.
**Self-lookup `Parent_Node__c` creates the parent-child hierarchy.**
Replaces the `nodes[]` array in the template JSON.

| Field | Type | Notes |
|---|---|---|
| Template__c | Master-Detail → ETL_Template__c | |
| Node_Id__c | Text | e.g. "n0", "n1775632321230260" |
| Object_Name__c | Text | e.g. "Itinerary__c", "Case" |
| Label__c | Text | Display name |
| Direction__c | Picklist: seed / parent / child | |
| Depth__c | Number | 0 = seed, 1 = direct child/parent |
| Via_Field__c | Text | Field linking to parent e.g. "Itinerary__c" |
| **Parent_Node__c** | **Lookup → ETL_Template_Node__c** | **Self-lookup = the tree** |
| Is_Included__c | Checkbox | Node active/inactive |

**Example — Itinerary Child template as records:**

| Node_Id | Object_Name | Direction | Depth | Parent_Node__c | Via_Field |
|---|---|---|---|---|---|
| n0 | Itinerary__c | seed | 0 | *(null)* | |
| n1775632321230260 | Case | child | 1 | n0 | Itinerary__c |
| n1775632327059213 | Itinerary_Line_Item__c | child | 1 | n0 | Itinerary__c |
| n1775632545156285 | Contact | child | 2 | n1775632321230260 | AccountId |

---

### ETL_Object_Config__c
Field selections and query config per object in a template.
Replaces the `objects[]` array in `migration.yaml`.

| Field | Type | Notes |
|---|---|---|
| Template__c | Master-Detail → ETL_Template__c | |
| Object_Name__c | Text | e.g. "Itinerary__c" |
| External_Id_Field__c | Text | e.g. "Prod_Record_Id__c" |
| Operation__c | Picklist: upsert / insert / update | |
| Query_Filter__c | Long Text | WHERE clause |
| Order_By__c | Text | ORDER BY clause |
| Record_Limit__c | Number | LIMIT |
| Record_Offset__c | Number | OFFSET |
| Max_Records__c | Number | Cap on records |

---

### ETL_Field_Selection__c
One record per selected field per object config.
Replaces the `fields[]` array per object in migration.yaml.

| Field | Type | Notes |
|---|---|---|
| Object_Config__c | Master-Detail → ETL_Object_Config__c | |
| Field_Name__c | Text | e.g. "Name", "Itinerary__c" |
| Field_Order__c | Number | Preserves selection order |
| Is_Lookup__c | Checkbox | Whether it's a lookup field |

---

### ETL_Transform_Rule__c
Transform rules per object.
Replaces the `transforms[]` array in migration.yaml.

| Field | Type | Notes |
|---|---|---|
| Object_Config__c | Master-Detail → ETL_Object_Config__c | |
| Rule_Type__c | Picklist: field_rename / static_value / etc. | |
| Source_Field__c | Text | |
| Target_Field__c | Text | |
| Rule_Value__c | Text | For static values |
| Rule_Order__c | Number | |

---

### ETL_Pipeline__c
An ordered sequence of templates = one full migration pipeline.
Replaces the `pipelines[]` array in migration.yaml.

| Field | Type | Notes |
|---|---|---|
| Migration__c | Master-Detail → ETL_Migration__c | |
| Name | Text | e.g. "Itinerary Child" |
| Pipeline_Id__c | Text | e.g. "Itinerary_Child" |
| Pipeline_Order__c | Number | Execution order |
| Seed_Query_Filter__c | Long Text | Seed-level WHERE (record IDs etc.) |
| Start_Stage__c | Picklist: extract / transform / load | |
| Status__c | Picklist | Derived from latest run |

---

### ETL_Pipeline_Stage__c
Links a template into a pipeline at a specific order position.

| Field | Type | Notes |
|---|---|---|
| Pipeline__c | Master-Detail → ETL_Pipeline__c | |
| Template__c | Lookup → ETL_Template__c | |
| Stage_Order__c | Number | |
| Stage_Name__c | Text | Display override |

---

### ETL_Pipeline_Run__c
One record per execution of a pipeline.
Replaces `run_state.json` in the local app.

| Field | Type | Notes |
|---|---|---|
| Pipeline__c | Lookup → ETL_Pipeline__c | |
| AWS_Job_Id__c | Text | Correlation ID from AWS |
| Status__c | Picklist: Queued / Running / Completed / Failed / Stopped | |
| Started_At__c | DateTime | |
| Completed_At__c | DateTime | |
| Error_Message__c | Long Text | |
| Log_S3_URL__c | URL | Link to S3 log file |
| Total_Extracted__c | Number | |
| Total_Loaded__c | Number | |
| Total_Failed__c | Number | |

---

### ETL_Run_Stage__c
Status per template/stage per run.

| Field | Type | Notes |
|---|---|---|
| Pipeline_Run__c | Master-Detail → ETL_Pipeline_Run__c | |
| Stage_Name__c | Text | |
| Stage_Order__c | Number | |
| Status__c | Picklist | |
| Started_At__c | DateTime | |
| Completed_At__c | DateTime | |

---

### ETL_Object_Result__c
Finest granularity — status per SF object per run.
Replaces the `pipelines.{id}.objects` section of run_state.json.

| Field | Type | Notes |
|---|---|---|
| Run_Stage__c | Master-Detail → ETL_Run_Stage__c | |
| Object_Name__c | Text | |
| Status__c | Picklist: Pending / Running / Completed / Failed | |
| Records_Extracted__c | Number | |
| Records_Loaded__c | Number | |
| Records_Failed__c | Number | |
| Failed_File_URL__c | URL | S3 presigned URL to failed CSV |
| Started_At__c | DateTime | |
| Completed_At__c | DateTime | |

---

### ETL_Run_Status__e  (Platform Event)
Real-time push from AWS Python engine → Salesforce → LWC.
Eliminates polling entirely.

| Field | Type | Notes |
|---|---|---|
| Job_Id__c | Text | AWS job correlation ID |
| Pipeline_Run_Id__c | Text | SF ETL_Pipeline_Run__c record Id |
| Stage_Name__c | Text | |
| Object_Name__c | Text | |
| Status__c | Text | |
| Records_Count__c | Number | |
| Error_Message__c | Text | |
| Timestamp__c | DateTime | |

---

## Config Reconstruction (No YAML Stored)

The migration.yaml is **never stored as a blob**. It is rebuilt on demand from records:

```apex
// ETLPipelineService.buildConfigJson(pipelineId)
// Queries ETL_Pipeline__c → ETL_Pipeline_Stage__c → ETL_Template__c
//   → ETL_Template_Node__c (tree)
//   → ETL_Object_Config__c → ETL_Field_Selection__c
//   → ETL_Transform_Rule__c
// Returns Map<String,Object> → serialized to JSON → sent to AWS
```

This avoids the Salesforce Long Text Area 131,072 character limit entirely.

---

## Apex Service Layer

| Class | Responsibility |
|---|---|
| `ETLMetadataService` | Proxy SF Describe API → getObjects, getFields, getRelatedObjects |
| `ETLTemplateService` | CRUD for templates: loadTemplate, saveTemplate, addNode, removeNode, saveFieldSelection, cloneTemplate |
| `ETLPipelineService` | Pipeline assembly + config generation: savePipeline, buildConfigJson, startRun, stopRun |
| `ETLRunMonitorService` | Run status: getActiveRuns, getRunHistory, getRunDetail, handleStatusEvent (Platform Event trigger) |

---

## LWC Component Tree

```
etlApp
├── etlTopBar
│   ├── etlAuthStatus
│   └── etlOrgSelector
│
├── etlTemplateBuilder          ← port of index.html + app_v2.js
│   ├── etlObjectBrowser        ← left panel: searchable SF object list
│   ├── etlTraversalTree        ← tree panel
│   │   └── etlTreeNode         ← recursive component (one node)
│   ├── etlFieldsPanel          ← right panel: field selector + filters
│   │   └── etlFieldRow         ← one field checkbox row
│   ├── etlBottomBar            ← seed WHERE / ORDER BY / LIMIT
│   ├── etlTopologyModal        ← SVG graph  (lwc:dom="manual")
│   ├── etlExternalIdModal      ← external ID mapping
│   └── etlTemplateManager      ← list / clone / delete templates
│
├── etlPipelineDesigner         ← port of pipeline_designer.html
│   ├── etlTemplateLibrary      ← left: saved templates
│   ├── etlPipelineFlow         ← center: ordered stage cards
│   │   └── etlStageCard        ← one stage (template + live status)
│   ├── etlRunControls          ← Start / Stop / Refresh
│   └── etlRunLog               ← live log panel
│
└── etlRunMonitor
    ├── etlRunBanner            ← top status bar
    ├── etlStageProgress        ← per-stage status
    └── etlObjectResults        ← per-object counts table
```

---

## Real-Time Status Flow

```
LWC subscribes to Platform Event on load
        │
ETL Python engine (AWS/local)
  → POST /services/data/v60.0/sobjects/ETL_Run_Status__e
    (using Connected App OAuth)
        │
Apex Trigger on ETL_Run_Status__e
  → UPDATE ETL_Run_Stage__c
  → UPDATE ETL_Object_Result__c
        │
LWC receives event via lightning/empApi
  → UI updates live (no polling)
```

```js
// LWC subscription
import { subscribe } from 'lightning/empApi';
subscribe('/event/ETL_Run_Status__e', -1, (event) => {
    const p = event.data.payload;
    this.updateStageStatus(p.Stage_Name__c, p.Status__c, p.Records_Count__c);
});
```

---

## Integration: Salesforce ↔ AWS

### SF → AWS (start/stop)
```
LWC button
  → Apex ETLPipelineService.startRun()
  → HttpRequest to Named Credential "ETL_Backend"
  → POST /runs/start  { pipelineConfig, options }
  → Returns { jobId }
  → INSERT ETL_Pipeline_Run__c
```

### AWS → SF (status push)
```
Python ETL engine
  → POST to SF REST API (ETL_Run_Status__e)
  → Platform Event fires
  → Apex Trigger updates run records
  → LWC updates via empApi subscription
```

### Named Credential
| Environment | URL |
|---|---|
| Local dev | `https://abc123.ngrok-free.app` (ngrok tunnel to localhost:5000) |
| AWS | `https://etl-api.yourdomain.com` (ALB / API Gateway) |

Only the Named Credential URL changes between environments. Zero code changes.

---

## Development Phases

### Phase 1 — SF Foundation
- [ ] Deploy all 12 custom objects + Platform Event via SFDX
- [ ] Set up Named Credential (ngrok URL for now)
- [ ] Connected App for AWS→SF Platform Event push
- [ ] Apex `ETLMetadataService` (Describe API proxy)
- [ ] Apex `ETLTemplateService` CRUD + round-trip test

### Phase 2 — Template Builder LWC
- [ ] `etlObjectBrowser` — object list from Describe
- [ ] `etlTraversalTree` + `etlTreeNode` — recursive, immediate save on node add
- [ ] `etlFieldsPanel` — field selection → ETL_Field_Selection__c
- [ ] `etlBottomBar` — seed filters
- [ ] `etlTemplateManager` — list / clone / delete

### Phase 3 — Pipeline Designer LWC
- [ ] `etlTemplateLibrary` — reads ETL_Template__c
- [ ] `etlPipelineFlow` — stage ordering → ETL_Pipeline__c
- [ ] `etlRunControls` — Start/Stop via Apex → Named Credential
- [ ] `etlRunLog` — log streaming from S3

### Phase 4 — Real-Time Monitor
- [ ] Python ETL engine: add Platform Event push on status change
- [ ] Apex Trigger: `ETL_Run_Status__e` → update run records
- [ ] `etlRunMonitor` — empApi subscription + live UI updates
- [ ] `etlObjectResults` — per-object counts

### Phase 5 — History + Reports
- [ ] Run history view from ETL_Pipeline_Run__c
- [ ] SF Reports / Dashboards on outcomes
- [ ] S3 presigned URL for failed record CSVs

---

## Key Decisions Summary

| Concern | Decision |
|---|---|
| YAML size limit (128KB SF cap) | No YAML stored — config rebuilt from records by Apex at run time |
| Template storage | SF Custom Objects (not ContentVersion/S3) — queryable, auditable |
| Tree structure | ETL_Template_Node__c self-lookup (Parent_Node__c) |
| Real-time status | Platform Events — not polling |
| Local dev without AWS | ngrok tunnel → Named Credential |
| Auth SF→AWS | Named Credential (JWT/Basic) |
| Auth AWS→SF | Connected App (OAuth JWT flow) |
| Two-pass cyclic resolution | Python engine handles — SF just tracks status |
| Governor limits | Bulk DML for field selections; cascade delete via Apex loop |
| Self-lookup tree depth | Unlimited — cascade delete walks descendants in Apex |
