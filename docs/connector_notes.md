# Connector notes (task 1a discovery)

Read-only spike. Nothing was created, updated, deleted or sent; ZoomInfo credits spent: 0. All values here are generic: ids are placeholders, emails are `user@example.com`, counts are rounded. Fixtures in `tests/fixtures/` are fully synthetic. Items not verified are marked **UNVERIFIED**.

Placeholders: `<ALEX_SF_USER_ID>`, `<ALEX_NOOKS_USER_ID>`, `<SF_ACCOUNT_ID>`, `<ZI_COMPANY_ID>`.

---

## 1. Salesforce (read-only)

**Identity.** `getUserInfo` gives the user id, role ("Commercial BDR - ANZ") and a user timezone that is **not** the prospect's (it is Europe/London); never use it for call windows. `AccountTeamMember` has no rows for this user, so it is not how BDR ownership works.

**"I am the BDR on this account" = `Account.Assigned_SDR__c`** (label "ROE SDR", a lookup to User).

| Field | Verdict |
|---|---|
| `Assigned_SDR__c` | Use this. About 20k accounts are assigned (ROE automation assigns a large book). |
| `SDR_Target__c` (bool) | Ranking boost. 41 flagged targets among ANZ non-customers. |
| `SDR_Prospecting_Priority__c`, `Prospecting_Priority__c` | Often null; optional tie-breakers. |
| `OwnerId`, `Account_Owner_Email__c` | The AE, not the BDR. |
| `AccountTeamMember` | Empty. |
| `override_ROE_SDR_automation__c` | Informational. |

**Scale.** About 20k assigned; about 12k ANZ non-customers (country test below); 41 with `SDR_Target__c`. The flagged targets all have country and full-name state filled in.

**Query shape**
```sql
SELECT Id, Name, Website, Type, BillingCountry, BillingState, HQ_Country__c,
       HQ_State_Province__c, BillingCity, ZoomInfo_Employee_Count__c, ROE_count__c,
       Current_Customer__c, SDR_Target__c, SDR_Prospecting_Priority__c,
       Prospecting_Priority__c, Prospect_Account_Type__c, Assigned_Sales_Segment__c,
       DOZISF__ZoomInfo_Id__c, Days_Since_Last_Activity__c
FROM Account
WHERE Assigned_SDR__c = '<ALEX_SF_USER_ID>'
  AND Current_Customer__c = false
  AND (BillingCountry IN ('Australia','New Zealand') OR HQ_Country__c IN ('Australia','New Zealand'))
```
Run a separate `COUNT()` first. Add `AND SDR_Target__c = true` for the flagged list.

**Country gotchas.** `BillingCountry` is blank on many accounts and mixed case (`AUSTRALIA` vs `Australia`; also seen as codes `AU`/`NZ` in `HQ_Country__c`). `HQ_Country__c` is a textarea; SOQL cannot always filter it, so confirm and filter in Python case-insensitively. A group of roughly 420 accounts has non-ANZ billing but ANZ HQ (flagged, see decisions).

**Field map**

| Need | Field | Notes |
|---|---|---|
| Pendo customer | `Current_Customer__c` | Also `Cancellation_Status__c`, `Type`. `Type` holds odd values (e.g. "2.0"): meaning **UNVERIFIED**. |
| Country / state | `BillingCountry`, `BillingState` (alt `HQ_Country__c`, `HQ_State_Province__c`) | State may be full name or abbreviation. |
| Employees | `ZoomInfo_Employee_Count__c` | Equals `ROE_count__c`. `NumberOfEmployees` does not exist. Parent size: `Ultimate_employee_Size__c`. |
| Website | `Website` | Bare host, no scheme. |
| ZoomInfo id | `DOZISF__ZoomInfo_Id__c` | Joins to ZoomInfo company id; not always populated. |
| Recency | `Days_Since_Last_Activity__c` | Wide range; sometimes null. |

**Open tasks.** `SELECT Id, Subject, Status, Type, TaskSubtype, ActivityDate, WhatId, WhoId, CreatedDate FROM Task WHERE OwnerId='<ALEX_SF_USER_ID>' AND IsClosed=false` returned about 30 stale rows (mostly "BDR Meeting Booked", `WhatId` null). Not a work queue; the real queue is Nooks tasks. Whether Nooks writes calls back to SF Tasks is **UNVERIFIED**.

**Contacts.** `Phone`, `MobilePhone`, `HasOptedOutOfEmail`, `AccountId`, `Title`. About a quarter of contacts under the book are opted out of email. **`DoNotCall` does not exist** on Contact.

**Practical.** `getObjectSchema` for Account is huge (about 180k chars) and spills to a file: request narrowly and filter with jq.

---

## 2. Nooks

`getMe`: sequencing and dialer products; Conversation Intelligence and enrichment off. OAuth scopes include tasks, calls, emails and notes write, so CLAUDE.md rules are the only guard.

**Dispositions** (`listCallDispositions`). Only `source=nooks_sep` is current. **Never hard-code ids; resolve by name at run time.**

| Meaning | Name | callOutcome |
|---|---|---|
| No answer | No Answer | No Answer |
| Voicemail | Left Voicemail | No Answer |
| Wrong number | Bad Number / Wrong Number | No Answer |
| Gatekeeper | Gatekeeper | Connect |
| Do not call | Do Not Call | Connect |
| Wrong person / left | Wrong Person, Person Left Company | Connect |
| Other connects | Hang Up, Follow-Up (Keep / Remove from Sequence), Objection: Not Interested / Not a Priority / Budget / Already Have a Solution / Bad fit | Connect |
| Meeting | Meeting Scheduled | Meeting |

There is no plain "Connected".

**`listCalls`** (owner and `filter_time_gte` filters work; pages ordered by id, not time, so page to the end before counting). Row: `id`, `source`, `duration` (seconds, may be fractional), `from`, `to`, `direction`, `recordingUrl`, `prospect{id}`, `sequence`, `sequenceStep`, `callDisposition{id}`, `owner{id}`, `dialingMode`, `hungUpBy`, `time` (UTC), `createdAt`, `hasTranscript`.

- `callDisposition` is a reference only; resolve its name via the table.
- About 1 call in 5 has `prospect: null` and `from: "external"` (logged outside the dialer). Fall back to the normalised `to` number to match.
- "Left Voicemail" calls ran about 20 to 35s, but "No Answer" is also used on 30 to 65s calls. **Do not infer voicemail from duration.**
- `dialingMode` was always `one-off`.

**Duration shares** (real pattern): roughly two-thirds of calls last under 10s, under 10% fall between 10 and 30s, and about a quarter run over 30s. The fixture `nooks_calls_sample.json` uses a deliberately different synthetic mix (about 60/12/28) for shape coverage only; do not use it to calibrate thresholds.

**Prospects.** `id`, `type`, `name`, `firstName`, `lastName`, `title`, `primaryEmail`, `linkedInUrl`, `crmId` (SF Contact id; null if type is not salesforce), `account{id}` (a Nooks id, not SF), `sequenceStates`, timestamps. **No phone and no opt-out flag on read**; phones come from SF Contact or `call.to`. `globalOptedOut` exists as a write field only; how it reads is **UNVERIFIED**. `primaryEmail` is dotted.

**Accounts.** `id`, `name`, `domain`, `source`, `numEmployees`, `linkedInUrl`, `description`. No country/state. `filter_crmId=<SF_ACCOUNT_ID>` reaches the Nooks account from SF. Filters are exact-match.

**Tasks.** Shape: `id`, `action`, `completed`, `dueAt`, `note`, `status` (pending/ready/finished/skipped/failed), `priority`, `owner`, `prospect`, `sequence`, `sequenceState`, `sequenceStep`, `email`. Existing tasks all belong to sequences; one-off tasks filter with `filter_sequenceState_state=null` (none found). `manual_email` tasks carry an email ref (body needs `include=email` or `getTaskEmail`). There is an old backlog of at least 200 `ready` `manual_email` tasks, nearly all in one sequence, due dates April to November; total **UNVERIFIED**. Never touched.

**Not authorised on this connection:** tag and play tools. Reconnecting Nooks adds them.

---

## 3. Gmail

`search_threads` can find sent mail:
```
in:sent to:user@example.com newer_than:7d
in:sent newer_than:7d
in:sent after:2026/11/18
```
Use `view=THREAD_VIEW_METADATA_ONLY` (no bodies are read).

**Dot-stripping gotcha.** Returned addresses have dots stripped from the local part (`first.last@` comes back as `firstlast@`). A query built from the stripped form finds nothing; query with the real dotted address from Nooks/SF, and compare results after stripping dots on both sides. Hyphenated local parts match fine. Thread results show only the first few messages; use `messageCount` and `get_thread` if replies matter.

---

## 4. ZoomInfo

Credits spent: 0. `userIntent` (10+ chars) is required on every call.

**`search_companies`** (`country="Australia,New Zealand"`, employee min/max, `redeem` unset). Each row is only `{id, type, attributes:{name}}`: no domain, size, city or state. Those need `enrich_companies` (credits).

- Join to Salesforce by `DOZISF__ZoomInfo_Id__c` for free website/state/size; `companyIdList=[...]` returns the same names.
- How strictly `employeeRangeMaximum` is applied is **UNVERIFIED**; `country` is not a strict HQ filter (non-ANZ companies appeared). `locationSearchType=HQ` untested.

**Noise** (`zoominfo_noise_examples.json`): pure locations ("<Place> CBD"), local businesses with a place name ("<Place> CBD Florist", plumbing, furniture, learning centres), government bodies, non-ANZ companies, vague names ("Funds"). A "whole name is just a place" rule catches only the first; the rest need a pattern or headcount rule.

**`search_scoops`** (AU+NZ, last 30 days). Shape: `attributes{company{id,name}, description, id, originalPublishedDate, publishedDate, topics[{id,topic}], types[{id,type}], link?, linkText?, contacts?[{firstName,lastName,id}]}`. No company domain or size; contacts have **no job title** (title only in the description text).

- Unfiltered newest-50 sample: about 90% generic vendor-survey style Project/Pain Point notes (finance, HR, marketing, AI tooling), concentrated in about a dozen companies. About 10% matched Pendo topics.
- Type-filtered sample: real events (funding, exec moves, M&A, partnerships), many from non-buyer industries; about 1 in 6 were generic "more open roles in Medical & Health".
- `Open Position` and `Management Move` (in config) were not seen; **UNVERIFIED**.

**Filter rules.** Keep Product Launch, Funding, Executive Move / New Hire (product, digital, CX, CTO roles), Partnership, M&A. Keep Project / Pain Point only if topics include Customer Experience, Product Management, Customer Success, Product Development or Digital Workplace. Drop generic "more open roles in Medical & Health / Legal" notes. "X invested in Y" notes are about the investor, not the target. `search_intent` not run.

---

## 5. Unattended-run gotchas

- Tool names contain connector UUIDs; they may change on reconnect. Resolve roles to tools at run time (ToolSearch), never hard-code.
- All tools are deferred: load schemas with ToolSearch first.
- Nooks tools require `USER_GOAL` and `REASON`; ZoomInfo requires `userIntent`.
- Large responses spill to files (SF schema, 100 Nooks tasks). Use `page_size` 25 to 50 or jq.
- Nooks pagination is cursor-based (`page_after`, `hasMore` / `links.next`).
- One extra MCP server reported needing OAuth and was unused.
- Scheduled/headless connector auth is **UNVERIFIED** (task 4b). Rate limits not measured.
- ZoomInfo scoops count against a record limit even though free.
- Timezone: derive from the prospect's state/country, not the SF user's.

---

## 6. Join paths and normalisation

(a) **Nooks prospect to SF.** `prospect.crmId` = SF Contact id; `Contact.AccountId` = SF Account id. Nooks `prospect.account.id` is a Nooks id; reach it from SF with `listAccounts filter_crmId=<SF_ACCOUNT_ID>`.

(b) **ZoomInfo to SF.** `Account.DOZISF__ZoomInfo_Id__c` = ZoomInfo company id. Not always populated; fill rate not measured.

(c) **Timezone fallback chain.** Prospect state (full name or abbreviation, case-insensitive) → `HQ_State_Province__c` / `BillingCity` hint → country default (`Australia/Sydney`, `Pacific/Auckland`), logged as a guess and called later in the window. NZ needs only country (`Pacific/Auckland`). `Queensland` is `Australia/Brisbane` (no DST), WA `Australia/Perth`, SA `Australia/Adelaide`, NT `Australia/Darwin`, VIC `Australia/Melbourne`, TAS `Australia/Hobart`, NSW/ACT `Australia/Sydney`. Among ANZ non-customers about 8% have null state; a few percent hold abbreviations; junk exists ("Other", "n/a", US states, city names, misspellings like "Aukland"). Unmapped falls back to the country default.

(d) **Phone normalisation** (vectors in `phone_formats.json`): keep a leading `+`; `00` prefix becomes `+`; strip other non-digits; digits starting 61/64 and long enough get `+`; leading `0` takes the account country's code (AU +61, NZ +64); never override an explicit `+` with the account country.

(e) **Recent-call lookup.** Page to the end (ordering is by id). Cooldown logic keyed on `prospect.id` misses unlinked calls; fall back to the normalised `to` number.

---

## UNVERIFIED list

- Meaning of `Account.Type` values such as "2.0".
- Whether Nooks writes calls back to SF Tasks.
- Where a call-do-not-call flag lives besides Nooks dispositions; how `globalOptedOut` reads.
- Total size of the `manual_email` backlog.
- Strictness of ZoomInfo size and country filters; `locationSearchType=HQ`.
- `Open Position` / `Management Move` scoop types.
- ZoomInfo id fill rate on accounts.
- Headless/scheduled connector auth and rate limits.

Decisions are in `docs/decisions.md`.
