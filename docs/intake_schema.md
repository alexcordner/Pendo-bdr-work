# Raw-dump schema (task 2a)

What the fetch step must leave in `output/raw/` (gitignored: real data never enters git) and what `src/intake.py` checks. Recipes are in `docs/fetch_runbook.md`. Field names are the connectors' own names, as in the synthetic fixtures in `tests/fixtures/` (`probe_*.json` are the shapes seen by the live read-only probe, `docs/connector_notes_v2.md`). Shapes verified by that probe are marked; everything else stays UNVERIFIED until the first live dry run, and `intake` says so loudly rather than guessing.

## Common shape

Every data file is one JSON object:

```json
{"meta": {"source_tool": "listCalls", "query": "owner=<id>, page_size=100", "page_count": 12,
          "reached_last_page": true, "fetched_at": "2026-11-25T02:10:00Z"},
 "rows": [ {...}, {...} ]}
```

- `rows`: the connector's rows EXACTLY as returned (no renaming, no trimming, no added fields). The one exception is documented under `nooks_accounts`.
- `meta.source_tool`: the tool that produced the rows. `meta.query`: the query / filters used (no secrets).
- `meta.page_count`: pages merged. `meta.fetched_at`: ISO UTC time the fetch finished.
- `meta.reached_last_page`: `true` only when the END was seen. **Nooks has no `hasMore` field: the end is `links.next == null` on the final page** (`merge_pages.py --derive-last-page` derives it from the final page; a non-null `links.next` is `false`). Salesforce keyset pages: `done` is true on every page, so the end is `done` AND a final page with fewer rows than `LIMIT` (`--paging salesforce`). ZoomInfo: every page sequence runs `meta.page.number` 1..`total` without a gap and its rows add up to `meta.totalResults` (`--paging zoominfo`). `false` if paging stopped for any other reason (error, cap, rate limit). `null` for sources that are not paged. Never guessed.

## Files

| File | Required | Paged | Source tool | Rows are |
|---|---|---|---|---|
| `sf_accounts.json` | yes | yes | Salesforce `soqlQuery` | `Account` records with `Assigned_SDR__c = <owner>`; ANZ may be pre-filtered server-side, the Python country rule is the authority |
| `sf_contacts.json` | yes | yes | Salesforce `soqlQuery` | `Contact` records of those accounts. **These ARE the prospects** (name, title, phone, opt-out, account) |
| `nooks_calls.json` | yes | yes | Nooks `listCalls` | the owner's FULL call history, owner filter ALWAYS set |
| `nooks_dispositions.json` | yes | no | Nooks `listCallDispositions` | disposition rows, all sources (~33), names resolve ids |
| `nooks_prospects.json` | no | n/a | Nooks `listProspects` | ONLY lazily resolved rows (`filter_crmId` / `filter_id`): the workspace table has ~3M rows and is never bulk-fetched |
| `nooks_accounts.json` | no | yes | Nooks `listAccounts` | Nooks accounts; links to Salesforce are derived (below) |
| `nooks_tasks.json` | no (blocks live) | yes | Nooks `listTasks` | OUR one-off call tasks only (`filter_action=call`, `filter_sequenceState_state=null`; NEVER the ~4k sequence tasks or the `manual_email` backlog); `note` carries the sheet tag |
| `zoominfo_scoops.json` | yes | yes | ZoomInfo `search_scoops` | buyer-type scoops only, fetched per type and merged: `{id, type, attributes{...}}` |
| `gmail_sent.json` | no (blocks live) | no | Gmail `search_threads` | threads from `in:sent to:<dotted address> newer_than:7d`, shortlisted prospects only; `meta.window_days` = 7 |
| `run_meta.json` | yes | n/a | the session | the run's facts (below) |

A missing optional file is a warning. `gmail_sent.json` missing blocks a live run (sent-email history unknown); so does `nooks_tasks.json` missing (no last-sheet memory beyond the ephemeral `state.json`). `nooks_accounts.json` missing means Nooks-only accounts are not merged. The ZoomInfo company search and ZoomInfo **intent are NOT fetched** (free company search returns only id and name; intent is deferred out of Phase 2: its live shape is nested JSON:API unlike our guess, topic names such as 'Product Analytics' / 'Digital Adoption' do not exist, and ~2.6k ANZ CX rows are noise): intake passes no ZoomInfo company rows to `build_account_list` and wires no intent.

### Row fields the pipeline reads

- **sf_accounts**: `Id, Name, Website, Type, BillingCountry, BillingState, BillingCity, HQ_Country__c, HQ_State_Province__c, ZoomInfo_Employee_Count__c, ROE_count__c, Current_Customer__c, SDR_Target__c, SDR_Prospecting_Priority__c, Prospecting_Priority__c, DOZISF__ZoomInfo_Id__c, Days_Since_Last_Activity__c` (a Salesforce `attributes` key is harmless). A row with no `Id` or an unusable value is reported as malformed (and listed by `accounts` in its dropped list).
- **sf_contacts**: `Id, AccountId, FirstName, LastName, Phone, MobilePhone, HasOptedOutOfEmail, Sales_Opt_Out_Email__c, Title, Email` (`Name` is used if the first / last names are absent; with none the prospect is labelled `(no name)`, never invented). Probe: `MobilePhone` ~29%, `Phone` ~91%, `HasOptedOutOfEmail` ~24%; Contact has NO `DoNotCall` field. `Sales_Opt_Out_Email__c` (meaning UNVERIFIED) is only COUNTED (`contacts_sales_opt_out_email_unverified`), it does not block anyone: the `Prospect` model has no field for it.
- **nooks_calls**: `id, source, duration, to, prospect{id}, callDisposition{id}, owner{id}, time, createdAt` (plus the rest of the `listCalls` row; verified shape `{traceId, hint, data, links{first, prev, next}}`). `callDisposition` is an id only: names come from `nooks_dispositions`. `to` is ~99% E.164; the literal `external` and odd spaced forms occur: the loader gives them `to_e164 = None` (counted in the run log), never an error. `source` varies (`nooks_sep`, `outreach`, `csv`, `salesforce`): see the Do Not Call limitation below.
- **nooks_dispositions**: `id, name, source, callOutcome, archived`.
- **nooks_prospects**: `id, name, firstName, lastName, title, primaryEmail, crmId, account{id}` (`account` is null for ~1/5 of rows; `crmId` can be a Lead id `00Q...`, only `003...` ids join to Contacts). Read only to attach a Nooks id (and name / title / email) to a Contact with the same `crmId`.
- **nooks_accounts**: `id, name, domain, numEmployees`. `listAccounts` returns no `crmId` on read, so intake derives Nooks account → Salesforce account from the (few) resolved prospect rows + Contact join (`prospect.account.id` with `Contact.AccountId` via `prospect.crmId`). A Nooks account whose prospects sit on several Salesforce accounts is left unlinked (counted). If a row already carries `_sf_account_id` (the test fixtures do) it is kept. Raw rows are never rewritten.
- **nooks_tasks**: `id, action, completed, dueAt, note, status, prospect{id}, owner{id}` (one-off tasks have null `sequence` / `sequenceState` / `sequenceStep`; verified: `note` is returned by the list). See "Last sheet" below.
- **zoominfo_scoops**: `id, attributes{id, company{id, name}, description, publishedDate, originalPublishedDate, topics[{id, topic}], types[{id, type}], link, contacts[{firstName, lastName, id}]}`. `company.id` joins to `DOZISF__ZoomInfo_Id__c` (~98% filled). Only ~30% of scoop companies are in the book: unmatched scoops are normal, and the run log reports `scoops matched to your book: X of Y`. A scoop can carry several types, so a scoop fetched under two type queries appears once after the merge.
- **gmail_sent**: threads `{id, messageCount, messages:[{id, sender, threadId, toRecipients[...]}]}` as `search_threads` returns them in metadata view (no bodies). **Verified: there is NO date field.** The query window is the only date information: `meta.window_days` (int) or `newer_than:Nd` in `meta.query` must say which (else a live run is blocked). Returned addresses have the dots stripped from the local part; the loaders compare on a dotless key.

## run_meta.json

A plain JSON object (not `{meta, rows}`):

```json
{"fetched_at": "2026-11-25T02:10:00Z",
 "sf_user_id": "<resolved by getUserInfo at run time>",
 "nooks_user_id": "<resolved by getMe at run time>",
 "row_counts": {"sf_accounts": 20112, "sf_contacts": 41000, "nooks_calls": 12840, "...": 0},
 "reached_last_page": {"sf_accounts": true, "sf_contacts": true, "nooks_calls": true, "nooks_prospects": true},
 "zoominfo_credits_used": 0,
 "scoop_counts_by_type": {"Executive Move": 1000, "Funding": 270},
 "warnings": ["free text, one per surprise the session noticed"]}
```

- `fetched_at`, `sf_user_id`, `nooks_user_id`, `row_counts` are required. `row_counts[file]` is the number of rows written to that file (before any de-duplication). A mismatch is an ERROR (truncated or edited file).
- `nooks_user_id` must equal `config.owner.nooks_user_id`, otherwise the run stops (the data belongs to someone else).
- `scoop_counts_by_type` is optional (per-type totals from `meta.totalResults`), logged as is.
- `reached_last_page` per paged source (derived from `links.next == null` for Nooks) must agree with the file's `meta`; on disagreement the cautious value (`false`) wins and a warning is logged.
- `zoominfo_credits_used`: integer, `0` when only free searches ran. Missing is a warning and blocks live (credits unknown, CLAUDE.md rule 7). Above `limits.zoominfo_credit_budget_per_run` is a warning and blocks live.

## What intake reports (`IntakeReport`)

| Severity | Cases |
|---|---|
| **error** (pipeline stops) | a required file (`sf_accounts`, `sf_contacts`, `nooks_calls`, `nooks_dispositions`, `zoominfo_scoops`, `run_meta`) missing; invalid JSON; not `{meta, rows}`; a row that is not an object; `run_meta` missing `fetched_at` / `sf_user_id` / `nooks_user_id` / `row_counts`; row count differs from `run_meta`; no dispositions; `nooks_user_id` not the configured owner |
| **warning** (logged) | optional file missing; calls from sources other than `nooks_sep` (Do Not Call limitation); sent-email window unknown; `meta` key missing; a paged source did not reach the last page; `run_meta` / file disagree on that; stale data; duplicates (same id; first kept); malformed rows per source (count + up to 5 scrubbed sample reasons, > 5% is flagged as a possible shape change); calls by another owner; calls whose disposition id is not in the list; prospects with a `crmId` but no Contact row; credits unknown or over budget |
| **live blocker** (`can_go_live` is False) | any error; `nooks_calls` (or `sf_accounts`, `sf_contacts`, `nooks_prospects`, `nooks_tasks`) did not reach the last page; data older than 24 h (or a `fetched_at` in the future, or unknown); malformed call / contact rows; `nooks_tasks.json` missing; a tagged task from the last sheet that cannot be tied to an account; Gmail window unknown or shorter than the cooldown; calls with an unknown disposition; no call history; `gmail_sent.json` missing; credits unknown or over budget; the Do Not Call rebuild rejected |

A malformed row is never skipped silently: it is counted in `IntakeReport.malformed[source]` with sample reasons, and again in the run log. Reasons and warnings are scrubbed of anything that looks like an email address or phone number.

`load_raw` never raises for bad data: read `RawData.report`. A dry run continues on warnings; `reached_last_page == false` on `nooks_calls` means Do Not Call is NOT rebuilt that run (the older marker in `state.json` is not trusted for a live run) and a live run is impossible.

## De-duplication and order

Rows are de-duplicated by id per file (`Id` for Salesforce, normalised to 15 characters; `id` elsewhere); the FIRST row wins. File order never changes the pipeline result (tested with shuffled files) as long as duplicate ids carry identical content.

## How prospects are built (Contacts, not Nooks prospects)

The Nooks prospect table is workspace-wide (~3M rows, no phone or opt-out fields, `filter_crmId` single-value): it is never listed. `build_prospects` makes one `Prospect` per Salesforce Contact (`FirstName LastName`, `Title`, `Email`, `MobilePhone` then `Phone` normalised with the account's country, `HasOptedOutOfEmail`, `AccountId`). `Prospect.nooks_id` is **None** unless resolved:

1. From `nooks_prospects.json` (lazily resolved rows) by `crmId` = Contact `Id`; those rows also supply name / title / email as `prospect_from_nooks` reads them.
2. `infer_prospect_ids_from_calls`: a Nooks prospect id seen on calls is attached to a Contact only if EVERY call with that id went to one phone number and that number belongs to exactly one Contact. Shared switchboards and conflicting numbers stay unresolved.

Stats in `IntakeReport.stats` / `PipelineResult.stats`: `contacts_total`, `contacts_with_usable_phone`, `contacts_opted_out_email`, `contacts_sales_opt_out_email_unverified`, `prospects_nooks_id_resolved`, `prospects_nooks_id_unresolved`, `prospect_ids_inferred_from_call_phone`, `call_prospect_ids_left_unresolved`.

Inside the pipeline only, a Contact with no Nooks id gets an internal state key `sfc:<contact id>` so `record_calls` can link phone-matched calls (otherwise they would be "unmatched" and cooldowns lost). It is removed before the prospects are returned (`PipelineResult.prospects` and `rank_result.prospects` never hold an `sfc:` id). `state.json` may hold `sfc:` keys; they are harmless.

**Phase 3 must RE-RUN the Do Not Call / opt-out check by Nooks prospect id** after it resolves ids (`filter_crmId`, one call per shortlisted contact), because a Do Not Call may be recorded against the Nooks prospect id while the dialled number differs from the Contact's: `state.is_blocked(prospect_with_real_id, state, "call")`.

## Do Not Call visibility (limitation, also in every log header)

Only Nooks-dialer (`nooks_sep`) calls carry a `Do Not Call` disposition; Outreach / CSV / Salesforce-sourced calls use other disposition sets with none. The call owner filter is not a permission boundary and other reps' Do Not Calls are not visible under it. The dry run matches Do Not Call by phone and hashed email from the owner's call history.

## Last sheet ("never the same account twice in a row")

Two sources, unioned: `state.json` (gitignored, starts "new" in a fresh container) and our TAGGED one-off tasks. Phase 3 writes `[apac-callsheet <YYYY-MM-DD> #<rank>]` into the task `note`, and should append ` acct=<Salesforce account id>` before the `]` (`[apac-callsheet 2026-11-25 #3 acct=001...]`): then the account needs no lookup. Without `acct=`, the task's prospect is resolved to a Contact through `nooks_prospects.json` (or the unique-phone inference). Rules: tags are parsed from the `note` of every task in the file (any status); untagged notes are ignored; the most recent tag date BEFORE the run date (Sydney) is "last night"; tags dated the run date or later are ignored (same-night reruns stay stable). A tagged task from that sheet that cannot be tied to an account is counted and blocks live.

## Sent mail (undated search hits)

A hit for `in:sent to:<addr> newer_than:7d` means "sent inside the window". `sent_at` stays None. `state.record_sent_emails` treats an undated email as recent until `first_seen + email_cooldown_days`; the pipeline passes `first_seen` = the Sydney date the SEARCH ran (`gmail_sent.json` `meta.fetched_at`), so the block lasts exactly until the window could have elapsed even if the pipeline is re-run later. A window shorter than `email_cooldown_days` blocks live.

## Further live blockers and sanity rules (review F5 to F12)

- Calls whose `owner.id` is not `run_meta.nooks_user_id`: warning in a dry run, **blocks live** (the owner filter is not a permission boundary, other reps' calls would mix in).
- `run_meta.empty_history_confirmed: true` marks a brand-new Nooks seat: an empty call history is then accepted and Do Not Call is marked rebuilt from it. Without it an empty history blocks live.
- Sanity floor: with no earlier state (new or deleted `state.json`) and fewer than 5 calls in the history, live is blocked unless `empty_history_confirmed` is true (a truncated first-ever history).
- `state.json` was reset (corrupt) and the tasks file has no tagged tasks to recover the last sheet from: live is blocked.
- Files are read as UTF-8 (BOM allowed) or UTF-16; anything else is a report error (`IntakeError`), never an exception.
- `Sales_Opt_Out_Email__c` is fetched and counted (`N contacts with Sales_Opt_Out_Email__c (meaning unverified, NOT used to block)` in the log); it never blocks until Alex decides.

## Review round 3 additions (N1 to N6)

- **Gmail coverage.** `gmail_sent.json` meta carries `searched_address_hashes` (salted, normalised hashes, 32 hex characters; never raw addresses: an `@` in a hash is an error) and `hash_salt` (default `apac-callsheet-v1`). `scripts/gmail_queue.py` produces the queue and the hashes. The pipeline reports `PipelineResult.gmail_uncovered` (hashes) and stats `gmail_ranked_prospects`, `gmail_ranked_prospects_uncovered`, `gmail_ranked_prospects_without_email`; live is refused while any ranked prospect with an email is uncovered. Missing hashes in meta = nothing covered (warning).
- **Recent calls tied to accounts.** Calls from the last 14 days whose Nooks prospect id (or, with no id, a unique phone) cannot be tied to a Contact with an account lose their cooldown. The log line `recent calls not tied to an account: X of Y` is always written; live is blocked above `MAX_UNTIED_RECENT_SHARE` (5%, `src/intake.py`). Calls with no prospect and no usable number cannot be tied by any means and are counted separately, not held against the threshold. `scripts/recent_call_ids.py` lists the ids to resolve (batches of 25 for `listProspects filter_id`), the rows go into `nooks_prospects.json`.
- **Do Not Call not matched to a contact.** Log line `DNC calls not matched to any contact: N (re-checked by Nooks prospect id in Phase 3)`. The Phase 3 re-check by Nooks prospect id and the Gmail coverage are HARD PRECONDITIONS: they are in the log header's DNC note, the `PHASE 3 PRECONDITIONS` log section and the `--live` output.
- **Task tags.** A note with several tags uses the NEWEST tag date.
- **Manifest checks and `--expect-pages`** are described in `docs/fetch_runbook.md` section 8.

## Review round 4 additions (O1 to O6)

- **Recent-call classes** (log line `recent calls not tied to an account: X of Y (...)`, stats `recent_calls_tied / _unresolved / _no_account / _not_in_book / _phone_unmatched / _unlinkable`): X = unresolved + book-contact-without-account. A call whose Nooks id resolved to a Lead (`crmId` `00Q...`), a Contact outside the book or no `crmId` is `not_in_book`: logged, not counted. Live is blocked above 5% of the judged calls AND by any unresolved call in the last 3 days (`UNRESOLVED_RECENT_DAYS`). Calls with no prospect and no usable number are `unlinkable`: a warning above 25% of all recent calls (`MAX_UNLINKABLE_SHARE`).
- **Helper-script outputs** (`scripts/gmail_queue.py`, `scripts/recent_call_ids.py`): only under `output/` (symlinks resolved and refused, git must not report the path as not ignored), created with mode 0600; any other path exits 2 and writes nothing (`scripts/safeout.py`).
- **Manifest checks** default to order-agnostic (duplicate ids, cursor continuity, full non-final pages, ZoomInfo totals) and `--expect-pages` is mandatory with `--derive-last-page`; `--strict-order` is opt-in.
- **Phase 3 preconditions** are six (owner id from getMe as `expected_owner_id`; dry-run payloads refused; tasks only for `ranked`; `record_sheet` only after the writes; Do Not Call re-check by Nooks prospect id; full Gmail coverage) in `pipeline.PHASE3_PRECONDITIONS`, the log and the `--live` output.
