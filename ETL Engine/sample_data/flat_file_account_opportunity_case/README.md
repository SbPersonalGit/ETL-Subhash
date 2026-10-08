# Account, Opportunity, and Case flat-file sample

These CSV files were created against the currently connected target-org metadata.

## Load order

1. `Account.csv`
2. `Opportunity.csv` and `Case.csv`

All three source files contain the source Salesforce `Id`. During Transform, the
planner copies that value into the target `Legacy_ID__c` external-ID field. The
sample IDs are synthetic Salesforce-shaped values and do not identify real records.

`Opportunity.AccountId` and `Case.AccountId` contain the corresponding source
Account `Id`. After Account is loaded, the existing ID mapper resolves these
references using the target Account's `Legacy_ID__c`.

`Case.BusinessHoursId` uses the active default Business Hours record found in the
target org at sample-generation time:

```text
01md3000000fRsHAAU (Default)
```

If this package is used with another target org, select that org's active default
Business Hours record before loading Case.

## Mapping

The headers use Salesforce API names, so the Flat File Migration screen will
auto-map them. `Id` is presented as a special `migration_key` field: it remains
in raw staging and is not sent directly to the target. Review every mapping
before saving the template.

Configure these object settings in the data pipeline:

| Object | Operation | External ID |
|---|---|---|
| Account | upsert | `Legacy_ID__c` |
| Opportunity | upsert | `Legacy_ID__c` |
| Case | upsert | `Legacy_ID__c` |

The sample values use active target picklist entries:

- Account Type: `Direct Customer`, `Independent Oil Company`, `Channel Partner`
- Opportunity StageName: `Prospecting`, `Qualifying`, `Negotiating`, `Bid In Development`
- Opportunity Type: `External`, `Internal to Company`
- Case Status: `New`, `On Hold`
- Case Origin: `Email`
- Case Priority: `High`, `Medium`, `Low`
- Case Type: `Customer Service`, `Communication`, `Authorization`, `Billing`
