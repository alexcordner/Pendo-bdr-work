# Confirmed decisions (task 1a)

Placeholders: `<ALEX_SF_USER_ID>` is the Salesforce user id, `<ALEX_NOOKS_USER_ID>` the Nooks owner id.

1. **Account scope.** Every account where `Account.Assigned_SDR__c = <ALEX_SF_USER_ID>` (the "ROE SDR" field), restricted to ANZ, excluding current customers (`Current_Customer__c = true`).
2. **Country test.** `BillingCountry` OR `HQ_Country__c` is AU/NZ (Australia, New Zealand, AU, NZ), compared case-insensitively (SOQL `IN` is case-insensitive; Python must lower-case).
3. **Non-ANZ billing, ANZ HQ group.** Roughly 420 accounts have a non-ANZ `BillingCountry` but an ANZ `HQ_Country__c`. Flag them in output; do not silently include them. Decision deferred to task 1d.
4. **`SDR_Target__c`** is a ranking boost, not a filter.
5. **Never contact.** A past Nooks `Do Not Call` disposition on the prospect, or Salesforce `Contact.HasOptedOutOfEmail = true`. Log every skip.
6. **Old `manual_email` tasks** in Nooks are never touched (not skipped, completed, edited or deleted).
7. **Voicemail** = the `Left Voicemail` disposition, resolved by name at run time. Never infer voicemail from duration. A `No Answer` under 20 seconds counts as no-contact.
8. **Scoop keep rules.** Keep Pendo-buyer types: Funding, Executive Move / New Hire (product, digital, CX, CTO roles), Product Launch, Partnership, M&A, Hiring Plans/Open Position only with a relevant topic. Keep Project / Pain Point only when topics include Customer Experience, Product Management, Customer Success, Product Development or Digital Workplace. Drop generic "more open roles in Medical & Health / Legal" notes and other off-topic scoops.
9. **From numbers.** The AU/UK caller-id numbers on calls are ignored.
10. **Headcount** = `Account.ZoomInfo_Employee_Count__c` (equals `ROE_count__c`; `NumberOfEmployees` does not exist in this org).
