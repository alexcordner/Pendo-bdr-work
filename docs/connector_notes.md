# Connector notes (task 1a discovery, 2026-11-25)

Read-only spike. Nothing was created, updated, deleted or sent. ZoomInfo credits spent: **0** (only `search_companies` and `search_scoops` with `redeem` unset). Fixtures in `tests/fixtures/` are anonymised. Company names are real and public. People, emails and phones are placeholders.

Things I could not verify are marked **UNVERIFIED**.

---

## 1. Salesforce (read-only)

**Identity.** `getUserInfo` returns Salesforce user id `005Pe00000HHNMbIAP`, role "Commercial BDR - ANZ" and title "Sales Development Representative". The manager's role is "BDR Manager". The user's timezone is Europe/London, so do not use it for call windows. `AccountTeamMember` has **0 rows** for this user, so it is not how BDR ownership works here.

**"Alex is the BDR on this account" = `Account.Assigned_SDR__c`** (label "ROE SDR", a lookup to User).

| Candidate field | Verdict |
|---|---|
| `Assigned_SDR__c` (ROE SDR) | **Use this.** 19,660 accounts point to Alex. |
| `SDR_Target__c` (SDR Target, bool) | Use as a narrowing flag. |
| `SDR_Prospecting_Priority__c` (A/..) and `Prospecting_Priority__c` (High/..) | Often null (see the sample rows). Optional tie-breaker only. |
| `OwnerId` / `Account_Owner_Email__c` | The AE, not the BDR. Do not use for "my accounts". |
| `AccountTeamMember` | Empty for Alex. |
| `override_ROE_SDR_automation__c` | Means the ROE automation is overridden. Informational. |

**Scale warning.** `Assigned_SDR__c = Alex` returns 19,660 accounts, because the ROE automation assigns a huge book. With the country filter and `Current_Customer__c = false` it is 7,604. With `SDR_Target__c = true` as well it is **41**. The 41 look like the real working list. **Decision for Alex: which is "my accounts"?** I recommend the 41 as a first-class list, then the 7,604 as an overflow pool ranked by signals.

**Proposed "my accounts" query**
```sql
SELECT Id, Name, Website, Type, BillingCountry, BillingState,
       ZoomInfo_Employee_Count__c, ROE_count__c, Current_Customer__c,
       SDR_Target__c, SDR_Prospecting_Priority__c, Prospecting_Priority__c,
       Prospect_Account_Type__c, Assigned_Sales_Segment__c,
       DOZISF__ZoomInfo_Id__c, Days_Since_Last_Activity__c
FROM Account
WHERE Assigned_SDR__c = '005Pe00000HHNMbIAP'
  AND Current_Customer__c = false
  AND BillingCountry IN ('Australia','New Zealand')
  AND SDR_Target__c = true
LIMIT 200
```
A separate `COUNT()` with the same filters returned **41**; the sample rows were pulled with `LIMIT 5` (3 anonymised rows in `sf_accounts_my_book.json`).

**Coverage hole (found in the review round).** `BillingCountry` is blank on many accounts and mixed case (`AUSTRALIA` vs `Australia`; SOQL `IN` is case-insensitive, Python must be too). Among Alex's non-customer accounts, **4,926 more** have `BillingCountry = null` but `HQ_Country__c` in (Australia, New Zealand), so the 7,604 figure undercounts. Use `(BillingCountry IN (...) OR HQ_Country__c IN (...))`. The 41 `SDR_Target__c` accounts all have both country and state filled in.

**Field map**

| Need | Field | Notes |
|---|---|---|
| Already a Pendo customer | `Current_Customer__c` (bool) | Also check `Cancellation_Status__c` and `Type`. `Type` holds odd values such as "2.0", so its meaning is **UNVERIFIED**. |
| Country | `BillingCountry` | Alternative: `HQ_Country__c` (textarea). Samples use full names ("Australia"). |
| State | `BillingState` | Full names ("New South Wales"). Matches the timezone keys in `config.yaml`. Alternative: `HQ_State_Province__c`. |
| Employees | `ZoomInfo_Employee_Count__c` | Equals `ROE_count__c` in all samples. `Account.NumberOfEmployees` does **not** exist in this org. Parent size: `Ultimate_employee_Size__c`. |
| Website | `Website` | Bare host (`www.x.com`), no scheme. |
| ZoomInfo id | `DOZISF__ZoomInfo_Id__c` (string) | Direct join to the ZoomInfo `companyId`. |
| Recency | `Days_Since_Last_Activity__c` | Ranges 5 to 790 in samples. |

**"My open tasks".** The query below returns 30 rows:
```sql
SELECT Id, Subject, Status, Type, TaskSubtype, ActivityDate, WhatId, WhoId, CreatedDate
FROM Task WHERE OwnerId='005Pe00000HHNMbIAP' AND IsClosed=false LIMIT 100
```
The 30 open tasks are 28 stale "BDR Meeting Booked" (Jan to Mar 2026, `WhatId` null) and 2 `Call` tasks (Jan and Mar 2026, `WhatId` null); see `sf_tasks_open.json`. Open SF Tasks are therefore **not** a useful work queue. The real queue is Nooks tasks (section 2). `Task` has call fields (`CallDisposition`, `CallDurationInSeconds`, `CallType`). I did not verify that Nooks writes calls back to SF Tasks (**UNVERIFIED**).

**Contact fields.** `Phone` and `MobilePhone` exist, and the mobile matched a Nooks `call.to` exactly after normalising spaces. `HasOptedOutOfEmail` exists, with 3,762 contacts opted out under Alex's accounts. **`DoNotCall` does not exist** on Contact. The call-do-not-call flag lives elsewhere (**UNVERIFIED**; Nooks `globalOptedOut` is the other candidate, see section 2).

**Practical.** `getObjectSchema` for Account is 177k characters and spills to a file. Always request a narrow object list and filter with jq.

---

## 2. Nooks

`getMe`: `userId 5koD9s…GPlY2`, workspace products are sequencing and dialer. Conversation Intelligence is **off**, enrichment is **off**. OAuth scopes include `tasks:write`, `calls:write`, `emails:write` and `notes:write`. The write scopes exist, so the safety rules in CLAUDE.md are the only guard, not the connector.

**Call dispositions** (`listCallDispositions`). Only `source=nooks_sep` entries are current. The full list is in `nooks_call_dispositions.json`.

| Meaning | Name | id | callOutcome |
|---|---|---|---|
| No answer | No Answer | `c106af3d-dc2f-471f-868c-017e9836979d` | No Answer |
| Voicemail | Left Voicemail | `d957dd2b-a8fb-496b-ae99-cd2adffebd96` | No Answer |
| Wrong number | Bad Number / Wrong Number | `31bec2be-6544-4a77-92ff-4b42689ebc82` | No Answer |
| Gatekeeper | Gatekeeper | `29cd2c61-440e-4634-9547-767d59463bb7` | Connect |
| Do not call | Do Not Call | `2f0d6e22-b502-4284-991c-a89e15840221` | Connect |
| Wrong person | Wrong Person | `690c1f57-2350-482d-b239-e70b9a39941a` | Connect |
| Left company | Person Left Company | `23633b32-14bf-4b1c-99a4-8c2f23345af2` | Connect |
| Meeting | Meeting Scheduled | `743925cb-ce5f-4cb7-b29d-3daeb92c654e` | Meeting |

Other connect-type dispositions: Hang Up, Follow-Up (Keep / Remove), and Objection (Not Interested, Not a Priority, Budget, Already Have a Solution, Bad fit). There is **no plain "Connected"** in `nooks_sep`; connects are expressed through these.

**`listCalls`** (owner and `filter_time_gte` filters work). Shape per row:

- `id`, `source`, `duration` (seconds, can be fractional), `from`, `to` (E.164 or odd formats), `direction`
- `recordingUrl`, `prospect{id}`, `sequence`, `sequenceStep`
- `callDisposition{id}`, `owner{id}`, `dialingMode`, `hungUpBy` (rep/prospect/null)
- `time` (UTC), `createdAt`, `updatedAt`, `hasTranscript`

Findings:

- `callDisposition` is a **reference only**. Resolve the name through the disposition table.
- 8 of 40 calls have `prospect: null` and `from: "external"`. These look like calls logged from outside the dialer, so join on `to` (phone) as a fallback.
- The "Left Voicemail" calls were 23 to 32s.
- "No Answer" is also used on long calls: in the 14-day pull, 8 calls of 34 to 64s (41, 41, 39, 45, 41, 34, 41, 64) carry "No Answer", while the 4 "Left Voicemail" calls were 23 to 32s. **Do not infer voicemail from duration; use the disposition id.**
- The `dialingMode` is always `one-off`; no calls came from sequences.

**Duration distribution** (evidence: `tests/fixtures/nooks_calls_14d_rows.json`). 40 calls by Alex from 2026-11-18 to 2026-11-25 (re-derived from per-row data; the same shares reproduce). In the full 14 days (104 calls, 2026-11-11 onwards) the shares are 68.3% / 6.7% / 25.0%.

| Bucket | Calls | Share |
|---|---|---|
| under 10s | 26 | 65% |
| 10 to 30s (inclusive) | 3 | 7.5% |
| over 30s | 11 | 27.5% |

**Prospects** (`listProspects`/`getProspect`). Fields: `id`, `type` (salesforce), `name`, `firstName`, `lastName`, `title`, `primaryEmail`, `linkedInUrl`, `crmId` (SF Contact id), `account{id}`, `sequenceStates`, `createdAt`, `updatedAt`.

- **No phone number and no `globalOptedOut` appear in the read response.** Phones are only visible on `call.to`.
- For phone, use SF `Contact.MobilePhone`/`Phone` via `crmId`.
- `globalOptedOut` exists as a write field on `updateProspect`. I did not call it. **How an opt-out shows on read is UNVERIFIED.** Treat as unknown, and also check `HasOptedOutOfEmail`.
- `primaryEmail` is dotted (`first.last@…`).

**Accounts** (`listAccounts`). Fields: `id`, `name`, `domain`, `source`, `numEmployees`, `linkedInUrl`, `description`. There is no country or state, and the SF id is only usable as a filter (`filter_crmId`). Filters are exact-match only (name, domain).

**Tasks** (`listTasks`).

- Shape: `id`, `action`, `completed`, `dueAt`, `note`, `status` (pending/ready/finished/skipped/failed), `priority`, `owner`, `prospect`, `sequence`, `sequenceState`, `sequenceStep`, `email` (ref or null).
- Every task I saw belongs to a sequence. One-off tasks (created by `createTask`) are filterable with `filter_sequenceState_state=null`; none exist right now (the query returned 0).
- An email task is `action: manual_email` with `email: {id}`; the body needs `include=email` or `getTaskEmail`.
- **Old manual_email backlog:** two full pages of 100 `ready` manual_email tasks (200 rows, all in sequences, all distinct prospects) and page 2 still had a next cursor, so there are **at least 200** and the total is **UNVERIFIED**. 189 of the 200 are in one sequence; due dates run from 2026-04-22 to 2026-11-15 (so some are recent, not only old). Evidence: `nooks_manual_email_backlog.json`. They were **not touched**.
- The same owner has open `call` tasks from sequences dated from April to August 2026.

**Not authorised on this connection** (per server notes): `listTags`, `createTag`, `listPlays` and friends. Reconnecting Nooks would add them.

---

## 3. Gmail

`search_threads` **can** find sent mail. Working syntax:

```
in:sent to:first.last@example.com newer_than:7d     # one recipient, last 7 days
in:sent newer_than:7d                               # everything sent
in:sent after:2026/11/18                            # date form
```

Use `view=THREAD_VIEW_METADATA_ONLY` (no subject, snippet or body). I read **no bodies**.

**Gotcha.** Returned addresses have the **dots stripped from the local part**: `alex.cordner@pendo.io` comes back as `alexcordner@pendo.io`, and a recipient `first.last@…` comes back as `firstlast@…`. A query built from the stripped form returned **nothing**; the dotted form found the thread. So always query with the real address from Nooks/SF, never one copied from a Gmail result.

A thread-level result shows only the first 5 messages, so use `messageCount` and `get_thread` if replies matter. 10 sent threads in the last 7 days. Hyphenated local parts (`a-b@`) matched fine.

---

## 4. ZoomInfo

Credits: 0. `userIntent` (10+ characters) is a required input on every call.

**`search_companies`** (`country="Australia,New Zealand"`, `employeeRangeMinimum=20`, `employeeRangeMaximum=2000`, `redeem` unset). Result: 192,853 matches. **Each row is only `{id, type, attributes:{name}}`.** There is **no domain, website, employee count, city or state** in a search result. Those need `enrich_companies`/`redeem`, which costs credits. Consequences:

- Join to Salesforce by `DOZISF__ZoomInfo_Id__c` to get website, state and size for free.
- For ZoomInfo-only companies there is no free way to know the size or state. The rows I saw look large (super funds, government), so the size cap may not be strictly applied, or this default ordering favours big names. **UNVERIFIED** how strictly `employeeRangeMaximum` is applied.
- `country` is not a strict HQ filter. Petrobangla and Shinsho (not ANZ) appeared. Try `locationSearchType=HQ` (not tested).

**Noise examples** (`zoominfo_noise_examples.json`; `companyName="Sydney CBD"` returned 65 hits):

1. Sydney CBD (a pure location)
2. Sydney CBD Florist
3. The Co-op Sydney CBD
4. Lighthouse Early Learning Centre (Sydney CBD
5. CBD Quality Used Office Furniture Sydney
6. Sydney Cbd Plumbing
7. Office of the Victorian Information Commissioner (government body)
8. Government of Western Australia Department of Health (government)
9. Petrobangla (not ANZ)
10. Funds (vague non-company name)

The filter rule "drop when the whole name is just a place" catches only #1. Local businesses with a place name in them (#2 to #6) need a rule on the pattern or on the headcount.

**`search_scoops`** (AU+NZ, last 30 days, `employeeRangeMax=2000`). Shape: `attributes{company{id,name}, description, id, originalPublishedDate, publishedDate, topics[{id,topic}], types[{id,type}], link?, linkText?, contacts?[{firstName,lastName,id}]}`. The company here has **no domain and no size**.

Samples (small and biased, so treat the shares as rough):

- **Newest 50, no type filter** (of 4,695 in 30 days). Types: 49 Project or Pain Point (some with both), 1 Funding. They came from just **9 companies**: The iGroup 14, North Ridge Partners 13, E-VIS 8, Car Distribution Group 6, and a few others. They are vendor-survey style notes ("according to an internal source in finance…"), not news. By an explicit rule (topics include Customer Experience, Customer Success, Product Management, Product Development or intranet/digital workplace), **5 of 50 (10%) match and 45 of 50 (90%) are generic**: finance, HR, marketing and AI-tooling scoops, plus 5 "X invested in Myriota/WearOptimo" notes that are about the investor. The rule is my judgment. Per-row evidence: `zoominfo_scoop_sample_evidence.json`. (My first draft said 96%; this corrects it.)
- **Type-filtered** (Funding, Executive Move, New Hire, Hiring Plans, Partnership, M&A, etc.; 25 of 7,118). Real events appear: funding (WearOptimo, Myriota, and one more), exec moves (SCEE CEO change, RNZ people chief leaving), M&A, partnerships. But many are not Pendo buyers (mining, energy). 4 of 25 were the generic **"more open roles in Medical & Health"** pattern, which CLAUDE.md says to ignore.
- The config's `scoop_types_keep` includes `Open Position` and `Management Move`. Neither appeared in the sample, so I could not check them. `Project` and `Pain Point` are **not** in the keep list, and that is where most of the volume sits.

Relevant to Pendo buyers (see `config/icp.md`): `Product Launch`, `Funding`, `Executive Move`/`New Hire` where the person is product, digital, CX or CTO (the scoop has `contacts[]` with a name but **no job title field**, so the title is only in the description text), `Partnership`, `M&A`. A `Pain Point` or `Project` scoop is only useful when its topics include Customer Experience, Product Management, Customer Success or Digital Workplace.

I did not run `search_intent`.

---

## 5. What will break unattended runs

- **Tool names contain connector UUIDs** (`mcp__3c43300d-…__soqlQuery`, `mcp__745dfc86-…__listCalls`). If a connector is reconnected the prefix may change. Code and prompts should map roles to tools at run time via ToolSearch, not hard-code ids.
- **All tools were deferred**: schemas must be loaded with ToolSearch before calling, or the call fails.
- **Nooks tools require `USER_GOAL` and `REASON`**; ZoomInfo requires `userIntent`.
- **Large responses spill to files** (SF schema 177k, 100 Nooks tasks 95k). Use `page_size` of 25 to 50 or jq on the saved file.
- **Nooks pagination** is cursor-based (`page_after`), with `hasMore` or `links.next`.
- **Auth.** All four connectors answered without prompts in this session. One further MCP server (`76a9b493…`) reported needing OAuth; it was not used and I do not know what it is. I have **not tested a scheduled or headless run**; that is task 4b.
- **Rate limits:** none hit. Not measured.
- **ZoomInfo scoops count against a record limit** even though they cost no credits.
- **Timezone.** The SF user timezone is London. Derive call windows from the prospect's `BillingState`.

---

## 6. Join paths and normalisation (review follow-ups)

Fixtures: `sf_nooks_zoominfo_chain.json`, `sf_billing_state_values.json`, `phone_formats.json`.

**(a) Nooks prospect to Salesforce.** `prospect.crmId` is the SF **Contact Id** (`003…`). `Contact.AccountId` gives the SF Account. Verified on two prospects. Nooks `prospect.account.id` is a Nooks UUID, not a SF id; to reach the Nooks account from SF, use `listAccounts filter_crmId=<SF Account Id>` (verified on one account). Prospects with `type` other than `salesforce` have no `crmId`.

**(b) ZoomInfo id to SF Account.** `Account.DOZISF__ZoomInfo_Id__c` equals the ZoomInfo `companyId`. A free `search_companies companyIdList=[...]` for two SF ids returned the same company names (Shippit, Phocas Software). Paired rows are in `sf_nooks_zoominfo_chain.json`. Not every account will have the field filled in; I did not measure that.

**(c) Timezone fallback.** For 7,604 non-customer AU/NZ accounts there are 107 distinct `BillingState` values:
- Full names dominate (NSW 2,614, VIC 1,925, QLD 1,031, WA 395, SA 214, ACT 98, TAS 43, NT 26).
- NZ states are regions (Auckland 272, Wellington 59, Canterbury 35, Waikato 20, …). **All NZ maps to `Pacific/Auckland`, so NZ needs only the country.**
- **Abbreviations occur** (NSW 41, VIC 18, QLD 12, WA 5, ACT 2, SA 1; TAS and NT not seen but expected), so the lookup must accept both forms case-insensitively.
- **NULL for 624 (8.2%)**; 622 of those have `BillingCity` or `HQ_State_Province__c`, which can be used as a second hint.
- Junk exists ("Other", "n/a", "NULL", US states, cities in the state field such as "Sydney", misspellings such as "Aukland"). Anything unmapped should fall back to the country default (`Australia/Sydney`, `Pacific/Auckland`) and be **logged as a guess**, and ideally be called later in the window (Australia spans 3 offsets, up to 2.5 hours).
- The 41 `SDR_Target__c` accounts all have a full-name state.

**(d) Phone normalisation.** SF `Phone`/`MobilePhone` are free text: `+61 4XX XXX XXX`, `+64 9-377 9779`, `0226…` (no country code), `61390…` (no plus), `+6496…`. Nooks `call.to` is mostly E.164 but can contain spaces (`+61 474 …`). Rule and test vectors are in `phone_formats.json`: keep a leading `+`, strip other non-digits, add `+` when digits start with 61 or 64 and are long enough, convert a leading `0` using the country calling code, and **never override an explicit `+` with the account country** (a +61 mobile exists on an NZ account). One SF mobile matched a Nooks `call.to` after stripping spaces.

**(e) 14-day call window.** 104 calls since 2026-11-11 (page 1 held 100, page 2 held 4), 74 distinct prospects, 94 distinct numbers, 20 calls with no prospect link. Pages are ordered by call id, not time, so always page to the end before counting. Cooldown logic that keys on `prospect.id` will miss the 20 unlinked calls; fall back to the normalised `to` number.

**(f) Pain Point scoop.** A Pain Point-only row (type Pain Point, no Project) is now in `zoominfo_scoops.json`.

---

## Decisions needed from Alex

1. **Which accounts are "mine"?** The 41 `SDR_Target__c = true` accounts, or the 7,604 AU/NZ non-customer accounts assigned to you, or a ranked mix? (I recommend: the 41 first, then the rest ranked by signals.)
2. **Is `SDR_Target__c` the flag you work from?** Or is there a list, view or report you really use?
3. **Employee floor.** Config says 20 to 2000. In SF use `ZoomInfo_Employee_Count__c`; is that the number you trust?
4. **Opt-out and do-not-call.** SF Contact has no `DoNotCall`, and Nooks does not return `globalOptedOut` on read. Which should block a call: Nooks "Do Not Call" disposition history, `HasOptedOutOfEmail`, or something else?
5. **Old manual_email tasks** (200+, mostly one sequence, due Apr to Nov). Leave alone, or may a later task skip or complete them?
6. **Scoop keep list.** Add `Project` and `Pain Point` scoops filtered by topic (Customer Experience, Product Management, Customer Success, Digital Workplace)? Today they would all be dropped.
7. **Voicemail rule.** Use only the `Left Voicemail` disposition, or also long "No Answer" calls (8 calls of 34 to 64s were logged as No Answer)?
8. **Mixed `from` numbers.** Calls come from an AU number and a UK number. Does it matter for the call sheet?
