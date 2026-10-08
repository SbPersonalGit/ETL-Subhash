"""Reconcile archived CampaignMember artifacts and build a client mapping workbook."""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter


def rows(path: Path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle)


def values(path: Path, field: str) -> set[str]:
    return {str(row.get(field) or "").strip() for row in rows(path) if str(row.get(field) or "").strip()}


def style_sheet(ws, widths=None):
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
    for index, width in enumerate(widths or [], start=1):
        ws.column_dimensions[get_column_letter(index)].width = width


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = args.folder
    source = root / "pre-load" / "FULL_CampaignMember_source_45k.csv"
    inventory = root / "pre-load" / "03_existing_record"
    campaign_ids = values(inventory / "Campaign.csv", "Legacy_ID__c")
    contact_ids = values(inventory / "Contact.csv", "Legacy_ID__c")

    successful_ids: set[str] = set()
    for path in (root / "post-load").rglob("CampaignMember_success_*.csv"):
        successful_ids.update(values(path, "Legacy_ID__c"))

    reason_counts = Counter()
    excluded = []
    simple_excluded = []
    mapping = defaultdict(lambda: {"count": 0, "samples": []})
    source_count = 0
    loaded_count = 0
    source_ids: set[str] = set()

    for row in rows(source):
        source_count += 1
        cm_id = str(row.get("Id") or "").strip()
        source_ids.add(cm_id)
        campaign_id = str(row.get("CampaignId") or "").strip()
        contact_id = str(row.get("ContactId") or "").strip()
        lead_id = str(row.get("LeadId") or "").strip()
        member_type = str(row.get("Type") or "").strip()
        if cm_id in successful_ids:
            loaded_count += 1
            continue

        reasons = []
        if campaign_id and campaign_id not in campaign_ids:
            reasons.append("CAMPAIGN_NOT_IN_TARGET_INVENTORY")
            item = mapping[("Campaign", campaign_id, "Campaign missing from target inventory")]
            item["count"] += 1
            if len(item["samples"]) < 5:
                item["samples"].append(cm_id)
        if lead_id:
            reasons.append("LEAD_DATA_NOT_PROVIDED")
            item = mapping[("Lead", lead_id, "Lead data/inventory was not provided for migration")]
            item["count"] += 1
            if len(item["samples"]) < 5:
                item["samples"].append(cm_id)
        elif contact_id and contact_id not in contact_ids:
            reasons.append("CONTACT_NOT_IN_TARGET_INVENTORY")
            item = mapping[("Contact", contact_id, "Contact missing from target inventory")]
            item["count"] += 1
            if len(item["samples"]) < 5:
                item["samples"].append(cm_id)
        elif not contact_id:
            reasons.append("NO_LEAD_OR_CONTACT_REFERENCE")
        if not reasons:
            reasons.append("NOT_IN_FINAL_SUCCESS_ARTIFACTS")

        reason = "; ".join(reasons)
        reason_counts[reason] += 1
        excluded.append([
            cm_id, member_type, campaign_id, lead_id, contact_id,
            str(row.get("Status") or "").strip(), reason,
            "Provide/load the missing parent record with its source ID in Legacy_ID__c, then return this CampaignMember for retry.",
        ])
        simple_excluded.append([
            cm_id,
            str(row.get("Name") or "").strip(),
            lead_id,
            contact_id,
        ])

    unmatched_success = sorted(successful_ids - source_ids)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    summary = wb.active
    summary.title = "Summary"
    summary.append(["Metric", "Count", "Explanation"])
    summary_rows = [
        ("Full source CampaignMembers", source_count, "Records in the archived full source CSV"),
        ("Confirmed inserted", loaded_count, "Distinct source IDs found in final/archived success artifacts"),
        ("Not inserted / parked", len(excluded), "Source records absent from all success artifacts"),
        ("Distinct client mapping requests", len(mapping), "Deduplicated missing Campaign, Contact, or Lead source IDs"),
        ("Success IDs not found in source", len(unmatched_success), "Should be zero; reconciliation control"),
    ]
    for item in summary_rows:
        summary.append(item)
    summary.append([])
    summary.append(["Not-inserted classification", "Count", "Meaning"])
    for reason, count in reason_counts.most_common():
        summary.append([reason, count, "Derived from archived source, target inventories, and success artifacts"])
    style_sheet(summary, [42, 16, 80])

    request = wb.create_sheet("Client_Mapping_Request")
    request.append([
        "Referenced Object", "Source Legacy ID", "Affected CampaignMembers",
        "Reason", "Client Action Required", "Sample Source CampaignMember IDs",
    ])
    for (object_name, legacy_id, reason), detail in sorted(mapping.items()):
        request.append([
            object_name, legacy_id, detail["count"], reason,
            f"Provide the {object_name} record/mapping and ensure target {object_name}.Legacy_ID__c equals this Source Legacy ID.",
            ", ".join(detail["samples"]),
        ])
    style_sheet(request, [22, 24, 25, 48, 90, 80])

    detail = wb.create_sheet("Not_Inserted_Records")
    detail.append([
        "Source CampaignMember ID", "Type", "Source Campaign ID", "Source Lead ID",
        "Source Contact ID", "Status", "Why Not Inserted", "Required Action",
    ])
    for item in excluded:
        detail.append(item)
    style_sheet(detail, [25, 14, 24, 24, 24, 16, 52, 100])

    simple = wb.create_sheet("Not_Loaded_Simple")
    simple.append(["CampaignMemberId", "Name", "Lead", "Contact"])
    for item in simple_excluded:
        simple.append(item)
    style_sheet(simple, [26, 45, 26, 26])

    errors = wb.create_sheet("Load_Error_History")
    errors.append(["Historical Run Stage", "Error Category", "Rows", "Final Disposition"])
    errors.append(["Initial full attempt", "Campaign status validation rule", 2164, "Temporary validation bypass; affected in-scope rows later loaded"])
    errors.append(["Initial full attempt", "Inactive/non-commercial Contact validation rule", 10, "Temporary validation bypass; affected in-scope rows later loaded"])
    errors.append(["Full pass 1", "Audit fields not createable/updateable", 284, "Retried without audit fields; all 284 loaded"])
    style_sheet(errors, [28, 55, 12, 75])

    readme = wb.create_sheet("Instructions")
    readme.append(["Topic", "Guidance"])
    guidance = [
        ("Purpose", "Client_Mapping_Request is the deduplicated list of missing parent data/mappings required before parked CampaignMembers can be retried."),
        ("Legacy ID contract", "The target parent record must store the supplied source Salesforce ID in Legacy_ID__c."),
        ("Lead dependency", "Lead-based CampaignMembers cannot be loaded until the corresponding Leads exist in the target; LeadId is create-only for this migration strategy."),
        ("Contact dependency", "Contact-only CampaignMembers require the corresponding Contact in the target inventory."),
        ("Campaign dependency", "Every CampaignMember also requires its Campaign in the target."),
        ("No fallback", "No dummy/fallback Campaign, Contact, or Lead was approved. Real parent data/mapping is required."),
        ("Historical errors", "Validation-rule and audit-field errors shown in Load_Error_History were remediated for the 2,456 in-scope records and are not outstanding mapping requests."),
    ]
    for item in guidance:
        readme.append(item)
    style_sheet(readme, [28, 120])

    wb.save(args.output)
    print(f"source={source_count:,}")
    print(f"loaded={loaded_count:,}")
    print(f"not_inserted={len(excluded):,}")
    print(f"mapping_requests={len(mapping):,}")
    print(f"output={args.output}")


if __name__ == "__main__":
    main()
