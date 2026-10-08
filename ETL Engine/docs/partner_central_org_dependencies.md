# Partner Central Source-to-UAT Org Dependencies

## Live-org audit result

Source alias: `BH Dev`  
Target alias: `Waygate_UAT`

The audit covered the 341 records in `User.csv` and queried both connected Salesforce orgs. No Salesforce records were modified.

| Dependency | Source population | UAT result | Required handling |
|---|---:|---|---|
| Profile | 341 users; 1 distinct profile | Matched | Replace source ID with UAT `Salesforce PRM Partner User` profile ID. License matches `Partner Community`. |
| User Role | 341 users; 186 distinct roles | No generated partner roles currently resolvable in UAT | Do not copy source IDs. Use the two-pass partner-role procedure below. |
| Account | 341 users; 182 distinct Accounts | All 182 matched through `Account.Legacy_ID__c` | Replace with verified UAT Account IDs. |
| Contact | 341 distinct Contacts | 258 matched through `Contact.Legacy_ID__c`; 83 absent | Migrate/resolve the 83 Contacts before their Users. |
| Manager | 0 | Not applicable | Leave blank. |
| Delegated Approver | 0 | Not applicable | Leave blank. |
| Call Center | 0 | Not applicable | Leave blank. |
| Individual | 0 | Not applicable | Leave blank. |
| Portal Role field | 0 | Not applicable | Leave blank. |
| Permission Set assignments | 0 assignments found for these source users | None to reproduce | No post-load action. |
| Permission Set Group assignments | 0 assignments found | None to reproduce | No post-load action. |
| Permission Set License assignments | 0 assignments found | None to reproduce | No post-load action. |
| Package License assignments | 0 assignments found | None to reproduce | No post-load action. |
| Public Group/Queue memberships | 0 memberships found | None to reproduce | No post-load action. |

## Correct partner-role procedure

Partner roles are generated in the target org and their Salesforce IDs are never portable between orgs. Developer-name matching also cannot be used before the target roles have been generated.

1. Load all required Accounts and enable them as partner Accounts. The audited 182 UAT Accounts are already partner-enabled.
2. Load all required Contacts. There are currently 83 unresolved Contacts.
3. For the first User under each Account, use the mapped UAT Profile and Contact/Account IDs, and do not send the source `UserRoleId`.
4. After Salesforce generates the Account's target partner-role hierarchy, query the target roles.
5. Map the source role by its Account and role level: `Partner Executive`, `Partner Manager`, or `Partner User`.
6. Apply the generated UAT role ID to the remaining Users and update the first User when its intended level differs from the automatically assigned role.

## Other source-org-specific dependencies in the object files

- `OwnerId` and every other User lookup must resolve to a UAT User or Queue; source IDs cannot be copied.
- `CreatedById` and `LastModifiedById` are audit fields. Exclude them unless UAT has Create Audit Fields enabled and a deliberate target-user mapping exists.
- `RecordTypeId` must be mapped by object plus Record Type `DeveloperName`, never by source ID.
- Business-record lookups (`Account`, `Contact`, `Opportunity`, and the in-scope Partner Central objects) must resolve by `Legacy_ID__c` or another verified external ID.
- `CurrencyIsoCode` is not an ID mapping, but every populated currency must be active in UAT before load.
- Experience Cloud site/profile membership is org configuration. Confirm that the mapped Partner Community profile is a member of the intended UAT site before activating users.
- Username values are globally unique across Salesforce. Apply the agreed UAT username convention if a source username already exists anywhere in Salesforce.
- User email delivery, activation emails, and password/reset behavior must be controlled during the load to avoid notifying users unintentionally.

## Generated mapping artifacts

The `org_dependency_mapping` output contains:

- `profile_mapping.csv`
- `role_mapping.csv`
- `user_dependency_audit.csv`
- `User_target_ids.csv`
- `post_user_dependencies.csv`
- `dependency_summary.json`

`User_target_ids.csv` is intentionally not marked load-ready while 83 Contacts and all generated partner-role mappings remain unresolved. Blank target values represent unresolved dependencies; they are not permission to discard the source relationship. Use the audit file to retain the source IDs during remediation.
