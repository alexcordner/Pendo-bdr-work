# Decisions (task 1a)

Placeholders: `<ALEX_SF_USER_ID>` is the Salesforce user id, `<ALEX_NOOKS_USER_ID>` the Nooks owner id.

## Confirmed by Alex

1. **Account scope.** Every account where `Account.Assigned_SDR__c = <ALEX_SF_USER_ID>` (the "ROE SDR" field), ANZ only, non-customers only (`Current_Customer__c = false`).
2. **Never contact.** A past Nooks `Do Not Call` disposition on the prospect, or Salesforce `Contact.HasOptedOutOfEmail = true`. Log every skip.
3. **Data approach.** No real CRM data in the repo: discovery notes and fixtures are fully synthetic and merged only in that form.

## Defaults in force, not yet confirmed by Alex

4. **Country test.** `BillingCountry` OR `HQ_Country__c` is AU/NZ (Australia, New Zealand, AU, NZ), case-insensitive.
5. **Non-ANZ billing, ANZ HQ group.** Roughly 420 accounts. Flag in output; do not silently include. Decision deferred to task 1d.
6. **`SDR_Target__c`** is a ranking boost, not a filter.
7. **Old `manual_email` tasks** in Nooks are never touched.
8. **Voicemail** = the `Left Voicemail` disposition, resolved by name at run time. Never infer voicemail from duration. A `No Answer` under 20 seconds counts as no-contact (boundary fixtures at 19s and 21s).
9. **Scoop keep rules.** Keep Pendo-buyer types: Funding, Executive Move / New Hire / Management Move (product, digital, CX, CTO roles), Product Launch, Partnership, M&A; Hiring Plans / Open Position only with a relevant topic. Keep Project / Pain Point only when topics include Customer Experience, Product Management, Customer Success, Product Development or Digital Workplace. Drop generic "more open roles in Medical & Health / Legal" notes and other off-topic scoops.
10. **From numbers.** The AU/UK caller-id numbers on calls are ignored.
11. **Headcount** = `Account.ZoomInfo_Employee_Count__c`.
