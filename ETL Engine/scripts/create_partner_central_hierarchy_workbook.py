"""Create the Partner Central migration hierarchy workbook."""

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


OUTPUT = Path(
    r"C:\Users\puneetmalhotra\Downloads\OneDrive_2026-09-07\04. Partner Central\CSV\Partner_Central_Object_Hierarchy.xlsx"
)

phases = [
    (1, "Contact", "Root", "Load before Partner Users; defer audit fields."),
    (1, "SFPRM_Approval_List__c", "Root", ""),
    (1, "SFPRM_Document_Access__c", "Root", ""),
    (1, "SFPRM_Download_Assets__c", "Root", ""),
    (1, "SFPRM_DS_Commission_Agreement_Data__c", "Root", ""),
    (1, "SFPRM_DS_Order__c", "Root", ""),
    (1, "SFPRM_Intermediary_Workflow__c", "Root", ""),
    (1, "SFPRM_MAP_Action_Plans__c", "Root", ""),
    (1, "SFPRM_Onboarding__c", "Root", ""),
    (1, "SFPRM_Order_Approval_Matrix__c", "Root", ""),
    (1, "SFPRM_Order_Data__c", "Root", ""),
    (1, "SFPRM_PAP_Group__c", "Root", ""),
    (1, "SFPRM_Potential_Opportunity__c", "Root", ""),
    (1, "SFPRM_PRM_User_request__c", "Root", ""),
    (1, "SFPRM_SalesRep_Deals__c", "Root", ""),
    (1, "SFPRM_User_Approval_List__c", "Root", ""),
    (2, "User", "Contact", "After Contact; excluded from the original hierarchy but included here for sequencing."),
    (2, "SFPRM_DS_Invoice__c", "SFPRM_DS_Order__c", ""),
    (2, "SFPRM_DS_Order_Line_Item__c", "SFPRM_DS_Order__c", "Source-export inferred dependency."),
    (2, "SFPRM_Onboarding_Checklist__c", "SFPRM_Onboarding__c", ""),
    (2, "SFPRM_Survey__c", "SFPRM_PAP_Group__c", "Part of Survey/PMP circular reference."),
    (2, "SFPRM_PMP_Commercial_Line__c", "SFPRM_PAP_Group__c", "Part of Survey/PMP circular reference."),
    (3, "Partner User Role update", "First Partner User", "Salesforce-generated target roles; never copy source role IDs."),
    (3, "SFPRM_DS_Collection__c", "SFPRM_DS_Order__c; SFPRM_DS_Invoice__c", ""),
    (3, "SFPRM_Invoice_Line__c", "SFPRM_DS_Invoice__c", "Source-export inferred dependency."),
    (3, "SFPRM_OP_Action_Plan__c", "SFPRM_PMP_Commercial_Line__c; SFPRM_Survey__c", "Source-export inferred dependencies."),
    (3, "SFPRM_Survey_Criteria__c", "SFPRM_Survey__c", ""),
    (4, "SFPRM_Commission_Paid_Details__c", "SFPRM_DS_Collection__c; SFPRM_DS_Order__c", ""),
]

dependencies = [
    ("SFPRM_DS_Order__c", "SFPRM_DS_Invoice__c", "SFPRM_Order__c", 3123, "Target Describe"),
    ("SFPRM_DS_Order__c", "SFPRM_DS_Collection__c", "SFPRM_Order__c", 3143, "Target Describe"),
    ("SFPRM_DS_Invoice__c", "SFPRM_DS_Collection__c", "SFPRM_Invoice__c", 3143, "Target Describe"),
    ("SFPRM_DS_Collection__c", "SFPRM_Commission_Paid_Details__c", "SFPRM_Collection__c", 3143, "Target Describe"),
    ("SFPRM_DS_Order__c", "SFPRM_Commission_Paid_Details__c", "SFPRM_Order_Number__c", 3143, "Target Describe"),
    ("SFPRM_DS_Order__c", "SFPRM_DS_Order_Line_Item__c", "SFPRM_Order__c", 6268, "Source ID prefix; target field absent"),
    ("SFPRM_DS_Invoice__c", "SFPRM_Invoice_Line__c", "SFPRM_Invoice_Number__c", 13, "Source ID prefix; target object absent"),
    ("SFPRM_Onboarding__c", "SFPRM_Onboarding_Checklist__c", "SFPRM_Onboarding__c", 12158, "Target Describe"),
    ("SFPRM_PAP_Group__c", "SFPRM_PMP_Commercial_Line__c", "SFPRM_PAP_Group__c", 1506, "Target Describe"),
    ("SFPRM_PAP_Group__c", "SFPRM_Survey__c", "SFPRM_PAP_Group__c", 669, "Target Describe"),
    ("SFPRM_Survey__c", "SFPRM_Survey_Criteria__c", "SFPRM_Survey__c", 20290, "Target Describe"),
    ("SFPRM_PMP_Commercial_Line__c", "SFPRM_OP_Action_Plan__c", "SFPRM_Commercial_Line__c", 1521, "Source ID prefix; target field absent"),
    ("SFPRM_Survey__c", "SFPRM_OP_Action_Plan__c", "SFPRM_Survey_Number__c", 1470, "Source ID prefix; target field absent"),
    ("SFPRM_Survey__c", "SFPRM_PMP_Commercial_Line__c", "SFPRM_Survey__c", 1470, "Circular; source inferred"),
    ("SFPRM_PMP_Commercial_Line__c", "SFPRM_Survey__c", "PMP_Commercial_Line__c", 669, "Circular; source inferred"),
]


def format_sheet(ws):
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
        cell.alignment = Alignment(wrap_text=True)
    for column in ws.columns:
        width = min(max(len(str(cell.value or "")) for cell in column) + 2, 60)
        ws.column_dimensions[get_column_letter(column[0].column)].width = width
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)


wb = Workbook()
ws = wb.active
ws.title = "Load Order"
ws.append(["Phase", "Object or Step", "Depends On", "Notes"])
for row in phases:
    ws.append(row)
format_sheet(ws)

ws = wb.create_sheet("Dependencies")
ws.append(["Parent Object", "Child Object", "Relationship Field", "Populated Rows", "Evidence"])
for row in dependencies:
    ws.append(row)
format_sheet(ws)

ws = wb.create_sheet("Critical Sequence")
ws.append(["Order", "Action", "Reason"])
critical = [
    (1, "Confirm target Accounts", "All 182 required Accounts are currently mapped and partner-enabled."),
    (2, "Load the 83 missing Contacts", "Partner Community Users require Contacts."),
    (3, "Create first Partner User per Account", "Use mapped UAT Profile; do not copy source UserRoleId."),
    (4, "Query generated UAT Partner Roles", "Role IDs are generated per target Account."),
    (5, "Map Executive/Manager/User role level", "Update first user and load remaining users."),
]
for row in critical:
    ws.append(row)
format_sheet(ws)

ws = wb.create_sheet("Rules and Exceptions")
ws.append(["Topic", "Instruction"])
rules = [
    ("Excluded hierarchy objects", "User and RecordType were excluded by instruction; User is shown only in Critical Sequence."),
    ("Absent parent workbooks", "Treat referenced objects without a workbook in the Partner Central folder as already migrated."),
    ("Contact audit fields", "Exclude CreatedById and LastModifiedById unless Create Audit Fields and verified UAT user mappings are deliberately used."),
    ("Contact owner", "Map OwnerId to an existing UAT User or approved fallback owner."),
    ("Record types", "Map by object plus DeveloperName, never by source Salesforce ID."),
    ("Survey/PMP cycle", "Insert Survey with PMP lookup deferred, insert PMP, then update Survey after both exist."),
    ("User roles", "Never copy BH Dev UserRoleId values into UAT."),
]
for row in rules:
    ws.append(row)
format_sheet(ws)

OUTPUT.parent.mkdir(parents=True, exist_ok=True)
wb.save(OUTPUT)
print(OUTPUT)
