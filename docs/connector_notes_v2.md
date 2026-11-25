# Connector notes v2 (Phase 2 live probe)

Strictly read-only probe. Nothing was created, updated, deleted, sent or labelled. **ZoomInfo credits spent: 0** (only `search_scoops`, `search_intent`, `lookup`). Gmail: search only, no bodies read. All values are placeholders or rounded counts; counts marked "est" are estimates. Items not verified are marked **UNVERIFIED**. Fixtures: `tests/fixtures/probe_*.json` (synthetic).

Placeholders: `<ALEX_SF_USER_ID>`, `<ALEX_NOOKS_USER_ID>`.

---

## 1. Nooks `listCalls`

| Question | Answer (evidence) |
|---|---|
| Max `page_size` | **100**. 500 is rejected client-side ("expected number to be <=100"). A 100-row page is about 75 KB, so it always spills to a file. |
| Paging fields | Response `{traceId, hint, data, links{first, prev, next}}`. **No `hasMore` / `nextPageAfter` field on read**: `links.next` is null on the last page. `links.next` carries `page[after]=<cursor>`; the `hint` text also names the cursor. |
| Cursor shape | base64 of `{"id":"<call uuid>","v":1}`. Ordering is **ascending by call id (a random uuid), not by time**. |
| Last page | Reached when `links.next == null`. An empty page (cursor past the end) returns `data: []`, a "No resources matched" `hint` and `next: null`, so a past-the-end cursor is harmless. Always loop until `next` is null; never infer the end from a short page. Because ids are uniform, a hand-built cursor (`id` = a uuid prefix such as `f0000000-...`) is accepted and jumps into the id space; **UNVERIFIED as a supported feature**, do not rely on it in production. |
| Owner filter | `filter_owner_id=<ALEX_NOOKS_USER_ID>` works: every row on the sampled pages has that one owner. |
| Other reps visible? | **Yes.** Without the owner filter a 100-row page held calls from about 40 distinct owners and from sources `nooks_sep` and `outreach`. So the filter is not a permission boundary: **always pass the owner filter**, otherwise other reps' calls (and their Do Not Call dispositions) mix in. Whether a Do Not Call from another rep's call should block Alex's call is a design decision for Alex (workspace-wide DNC would need the unfiltered listing). |
| Date filters | `filter_time_gte` works (call time; `filter_createdAt_*` and `filter_updatedAt_*` also exist). Combined with the owner filter it returned only rows on or after the cut-off, still id-ordered. |
| Volume (est) | Owner total roughly **1.6k to 1.8k calls**; last 90 days roughly **750** (est from id density: 100 rows spanned ~6% / ~13% of the id space). No total-count field exists. So a full history is about 18 pages, a 90-day pull about 8 pages. |
| Disposition | `callDisposition: {id, _href}` only (id, no name). Map via `listCallDispositions` (one call, 100 per page, about 33 rows incl. archived=false; single page, `next` null). |
| Disposition sources | `nooks_sep` (current: has **Do Not Call**, Wrong Person, Person Left Company, Gatekeeper, Meeting Scheduled, etc.), plus legacy `outreach`, `csv`, `salesforce`. **Outreach-sourced calls use a different disposition set with no "Do Not Call"** (e.g. "Connected - ..." / "Bad Number - Not Reachable"). In the sampled owner pages about 85% of calls were `nooks_sep`, 8 to 10% `outreach`, a few % `salesforce`. A DNC rebuild keyed on the name "Do Not Call" only sees `nooks_sep`; map by **name across all sources** and log unknown ids. Duplicate names across sources (e.g. "Left Voicemail" exists 3 times) are different ids. |
| `to` format | Sample of 300 owner calls: **99% E.164** (`+` and 8 to 15 digits). Non-E.164 about 1% (2/300): the literal string `external` (rows logged outside the dialer) and a spaced/grouped international form from a `salesforce`-sourced row. Rows with `from: "external"` were about 19% of the sample; `prospect: null` about 19%. `dialingMode` was `null` on `outreach`/`salesforce` rows (earlier notes said always `one-off`: true only for some sources). |
| Fields | Same as `connector_notes.md` plus `updatedAt`; `source` is real and varies. |

**Design impact:** (a) pass the owner filter; (b) page until `links.next` is null (the adapter field is `links.next`, not `hasMore`); (c) resolve dispositions by name across all sources; (d) treat `to == "external"` as unusable for phone matching, match by prospect id.

## 2. Nooks `listTasks`

- One-off call tasks list fine: `filter_owner_id`, `filter_action=call`, `filter_status=ready,pending`, `filter_sequenceState_state=null`. For the owner this returned **4 open one-off call tasks** (3 with `note` text, 1 empty). Earlier notes said none existed; they do (created manually, e.g. at events).
- Fields per row: `id, action, completed, dueAt, note, status, priority, owner{id}, prospect{id}, sequence, sequenceState, sequenceStep, email, createdAt, updatedAt`. One-off rows have `sequence`/`sequenceState`/`sequenceStep` null. **`note` is returned in the list** (free text, can be multi-line or empty), so a tag in the note is readable without `getTask`. `dueAt` filters exist (`filter_dueAt_gte/lte`); not exercised beyond schema.
- Paging as listCalls (`links.next`, cursor base64 of `{"id":...,"v":1}`), `page_size` max 100, and tasks are also id-ordered.
- Without the one-off filter, `filter_action=call&filter_status=ready` (owner) is dominated by **sequence tasks: roughly 4k open ready call tasks (est from id density)**; all of the sampled page were sequence tasks. Never use that listing for "accounts already on a sheet"; always add `filter_sequenceState_state=null`.
- No write was attempted (no complete / skip / delete).
- **Note:** tasks hold a prospect, not an account, so "account already on a previous sheet" needs prospect to SF account resolution (see 3), or put the SF account id into the `note` tag at write time.

## 3. Nooks `listProspects`

- Fields: `id, type, name, firstName, lastName, title, primaryEmail, linkedInUrl, crmId, account{id}|null, sequenceStates[{id}], createdAt, updatedAt`. **No phone and no opt-out/DNC field on read** (re-confirmed). `account` is **null for roughly a fifth of rows** (many Salesforce *Lead*-typed prospects, `crmId` starts with the Lead prefix `00Q`, not the Contact prefix `003`). So `crmId` can be a Lead id: only `003...` ids join to SF Contact.
- `page_size` max 100; cursor paging as above. Filters: `filter_crmId` (single exact value; comma multi-value support is **UNVERIFIED**, the description says exact match only), `filter_account_id`, `filter_id` (comma UUIDs), `filter_primaryEmail` (comma list up to 10), `filter_type`, `filter_updatedAt_*`.
- **Size: the prospect table is workspace-wide, about 3M rows (est from id density of one page).** Listing it is not feasible. Resolve prospect ids from SF Contact ids with `filter_crmId` one call at a time (no bulk form), or only resolve prospects that already appear in the owner's calls/tasks. This is the largest throughput risk for Phase 2.

## 4. Salesforce (read-only)

- `getUserInfo` gives the user id and role (user timezone is the London one, not the prospect's).
- **Account paging recipe works.** `SELECT ... FROM Account WHERE Assigned_SDR__c='<ALEX_SF_USER_ID>' AND Id > '<last>' ORDER BY Id LIMIT 2000` returns exactly 2000 rows per call (`totalSize` equals the page size, `done: true`, no `nextRecordsUrl`), so keyset on Id is the only paging. First page of 10 fields (Id, Name, Website, BillingCountry, BillingState, HQ_Country__c, DOZISF__ZoomInfo_Id__c, Current_Customer__c, SDR_Target__c, ZoomInfo_Employee_Count__c) was **about 760 KB (about 380 B/row)** and spills to a file. Final page is short.
- **Total assigned accounts: `COUNT()` about 19.7k** (about 10 pages of 2000). Of those about 6.9k are non-customers with null `BillingCountry`.
- **`HQ_Country__c` CAN be used in `WHERE` for equality/IN** (e.g. `= 'Australia'` returned about 11.5k; `IN ('Australia','New Zealand')` accepted). This contradicts the earlier note. Values are mixed case (`AUSTRALIA`), so equality is case-insensitive in SOQL but still filter again in Python. Still pull all rows and filter in Python if the textarea behaviour varies; **UNVERIFIED for long/odd values (codes `AU`/`NZ` not tested)**.
- **`BillingCountry` null and `HQ_Country__c` in (Australia, New Zealand): about 4.9k accounts (about a quarter of the book).** Surprise: the first 2000 ids held only about 20 null-billing rows, so the nulls sit in newer ids. A Billing-only country filter would lose a quarter of the ANZ book; the earlier "about 420" figure referred to non-ANZ billing with ANZ HQ.
- ZoomInfo id on accounts: **about 98% populated** in the first page; always a digit string of 5 to 10 chars (stored as text).
- **Contacts via relationship filter works:** `SELECT ... FROM Contact WHERE Account.Assigned_SDR__c='<ALEX_SF_USER_ID>' AND Id > '<last>' ORDER BY Id LIMIT 2000` returns 2000 rows (7 fields about 600 KB, about 300 B/row). **Total contacts about 54k** (about 27 pages, roughly 16 MB spilled in total). First-page shares: **MobilePhone about 29%, Phone about 91%, either about 94%**, Email 100%, Title about 94%, `HasOptedOutOfEmail` about 24%.
- **Do-not-call fields on Contact:** `DoNotCall` and `Do_Not_Call__c` do not exist (INVALID_FIELD). A `FieldDefinition` query for names matching call/opt/phone/consent found only: `HasOptedOutOfEmail`, `Sales_Opt_Out_Email__c` (checkbox), `Pendo_Power_User_Sales_Opt_Out__c` (checkbox), `GDPR_Consent_Date__c` (datetime), a long-text `Clay_Nooks_Call__c`, and phone fields `Phone, MobilePhone, HomePhone, OtherPhone, AssistantPhone`. No call-specific opt-out exists; the two sales opt-out checkboxes are candidates for "do not contact" (meaning **UNVERIFIED**, ask Alex).
- **`getObjectSchema` failed** this session for Contact (`UnsupportedMockSessionOperationException`). Workaround that works: `SELECT QualifiedApiName, DataType FROM FieldDefinition WHERE EntityDefinition.QualifiedApiName='Contact' AND ...` (also returned fine). Use it instead of `getObjectSchema` in unattended runs.
- Spill: results above roughly 20 to 30k characters are saved to a file by the harness (single-line JSON). Process with jq/Python; never read them into context.

## 5. ZoomInfo

- **`search_scoops`**: `pageSize` max **100** (a 100-row page is about 60 KB, spills). `meta.totalResults` gives a free total; **use `pageSize=1` to count**. `meta.page.total` is the page count.
- All eight requested names are valid per `lookup scoop-types` (24 types exist, e.g. also `Left Company`, `Lateral Move`, `Promotion`, `Layoffs`, `Hiring Plans`, `Exit Investment`). Counts, AU+NZ, last 90 days (published 2026-08-27 onward), rounded:

| Type | Count |
|---|---|
| Open Position | ~10k |
| Mergers & Acquisitions (M&A) | ~700 |
| Executive Move | ~1k |
| Funding | ~270 |
| Partnership | ~250 |
| New Hire | ~220 |
| Product Launch | ~35 |
| **Management Move** | **0** (accepted but empty; the earlier "unseen" is confirmed: do not rely on it) |
| All eight types together | ~8.6k |

  (The combined total is below the sum because scoops can carry several types, e.g. "Executive Move" and "Left Company" together, and the combined search returned other types too.) A single combined 100-row page contained mostly Open Position and M&A.
- **Employee filter on scoops works**: `employeeRangeMax=2000` cut Product Launch from about 35 to about 29.
- **Funding attaches to the investee** in the observed example: `company` = the company that raised, and investors appear only in the description (fake example: company "Example Funding Co", description "Example Funding Co has secured $5 million ... Investors included Fake Capital."). The earlier "X invested in Y" wording (company = investor) can also occur, so keep the investor-verb rule.
- Person scoops carry `contacts[{firstName, lastName, id}]` (numeric id, no title); `link`/`linkText` optional.
- **Join to Salesforce**: ZoomInfo company ids are numeric strings (5 to 10 digits) in the same format as `DOZISF__ZoomInfo_Id__c`. Test with 80 distinct scoop company ids: **about 37% (30/80) match some SF account; about 30% (24/80) match accounts assigned to Alex** (the rest are non-book companies). Join works; the match rate is a coverage fact, not a format problem.
- **`search_intent`**: topic names via `lookup intent-topics`: `Digital Experience`, `Customer Experience Software`, `Customer Experience Management`, `Customer Journey`, `Customer Feedback Management`, `Customer Engagement`, `Customer Insight`, `Digital Transformation`, `Product Lifecycle Management`, `Product Data Management`, `Onboarding` (HR-type, not Pendo-relevant). **"Product Analytics" and "Digital Adoption" did not appear** in fuzzy lookups ("product", "adoption" returned the product-management-type topics above only), so the guessed names in `signals_intent.json` are not valid. Shape (real) is nested JSON:API: `data[{id, type:"Intent", attributes{audienceStrength, category, company{id,name,website,hasOtherTopicConsumption}, signalDate, signalScore, spikesInDateRange, topic}}]` plus `meta{page, totalResults, topicsRequested, fieldResolution}`; ids look like `<topicId>#<companyId>#<yyyymmdd>`. `signalScore` 60 to 100, `audienceStrength` A to E. Search `country=Australia,New Zealand` with 4 CX topics, score at least 70, returned about **2.6k rows** in total (mostly non-buyers, small local businesses and universities); that is far too noisy to use unfiltered. Intersection with Alex's accounts: **UNVERIFIED** (not run; it needs SF id join over the 2.6k rows, free but many pages).
- Credits spent: **0**.

## 6. Gmail

- `search_threads` accepts `in:sent newer_than:7d` and `in:sent newer_than:2d` (returned results). A query with the owner's own dotted address as recipient returned `{}` for 30 days (no self-sent mail; not evidence either way). The dotted-recipient behaviour from `connector_notes.md` is therefore **not re-verified here**.
- **Surprise: there is NO date field on `search_threads` messages.** `THREAD_VIEW_METADATA_ONLY` returned only `id, sender, toRecipients, threadId` (plus `messageCount` and `resultCountEstimate`); `THREAD_VIEW_SUMMARY` added `subject`. The tool description lists `date` but live results omitted it. **A "last emailed" date cannot be read from search results**; make the date filter part of the query (`newer_than:7d`, `after:YYYY/MM/DD`) and treat "thread exists in the window" as the signal. Reading the date would need `get_thread` (bodies); not tested because of the no-bodies rule: the date field name there is **UNVERIFIED**.
- Senders are returned dotless (as before).

## 7. Unattended-run notes

- Tool names embed a connector UUID (`mcp__<uuid>__<tool>`); resolve by role with ToolSearch each run. All tools are deferred: load schemas with ToolSearch (`select:`), ZoomInfo/Salesforce/Gmail/Nooks need separate loads. Several unrelated MCP servers and one needing OAuth are present; ignore them.
- Required params: Nooks every tool `USER_GOAL` and `REASON` (same `USER_GOAL` per run); ZoomInfo `userIntent` (10 to 500 chars) on every call; Salesforce none; Gmail none.
- Result-size spill: any result above about 20 to 30k characters is written to a file with a line-count-1 JSON; Nooks 100-row pages (75 KB) always spill, SF 2000-row pages (0.6 to 0.8 MB) always spill, ZoomInfo 100-row scoop pages (60 KB) spill. Prefer small pages only when context matters; otherwise take the max page and parse the file with Python (`-I`).
- Rate limits: **none hit** (about 35 calls in a few minutes). Not measured systematically; **UNVERIFIED**.
- Runtime per call was **not timed**. Rough planning figure only (unmeasured): 2 to 6 s per call incl. spill.
- Nooks failures are recoverable via `getRecentFailures`; `getObjectSchema` on Salesforce can fail in this session type (see 4).
- **Full-fetch call budget (est):** SF accounts about 10 calls; SF contacts about 27; Nooks calls (owner, full history) about 18 or about 8 for 90 days; dispositions 1; scoops per type with `pageSize=100` about 3 to 6 calls for the buyer types (Open Position is about 100 pages: filter it out or restrict to a topic/department); intent about 27 pages of 100 for the CX topics; Gmail 1 per candidate account. Prospect resolution is the unbounded part (one call per contact via `filter_crmId`). Total core fetch about 70 to 100 calls: about **5 to 10 minutes** if calls take 3 to 6 s, plus Python processing (unmeasured).

## Surprises that change the design

1. The call owner filter is not a permission boundary (other reps' calls visible): always filter; decide whether workspace-wide Do Not Call matters.
2. No `date` in Gmail search results: use query-side date windows.
3. About a quarter of the book has null `BillingCountry` but ANZ `HQ_Country__c`: country must use both fields.
4. Prospect table is workspace-wide (about 3M) and `filter_crmId` is single-value: prospect resolution at scale needs a different join (from calls/tasks, or lazily per shortlisted account).
5. Open one-off call tasks exist (4); the task `note` is listable, so a tag-in-note scheme works. Unfiltered call tasks are about 4k sequence tasks.
6. `Management Move` returns nothing; guessed intent topic names ("Product Analytics", "Digital Adoption") do not exist.
7. `getObjectSchema` can fail; use `FieldDefinition` queries.

## What could not be verified

Per-call runtime and rate limits; `filter_crmId` multi-value; hand-built cursor as a supported feature; Gmail message date field name (needs bodies); dotted-recipient `to:` query re-check; intent intersection with Alex's accounts; the meaning of `Sales_Opt_Out_Email__c` / `Pendo_Power_User_Sales_Opt_Out__c`; headless/scheduled connector auth.
