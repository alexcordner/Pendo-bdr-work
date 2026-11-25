# Raw-dump schema (task 2a)

What the fetch step must leave in `output/raw/` (gitignored: real data never enters git) and what `src/intake.py` checks. Recipes are in `docs/fetch_runbook.md`. Field names are the connectors' own names, as in the synthetic fixtures in `tests/fixtures/`. **Live shapes are UNVERIFIED until the first live dry run**; `intake` is built to say so loudly rather than guess.

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
- `meta.reached_last_page`: `true` only when the session SAW the end (Nooks `hasMore == false`, Salesforce `done == true`, an empty or short final page). `false` if paging stopped for any other reason (error, cap, rate limit). `null` for sources that are not paged. Never guessed; `scripts/merge_pages.py` writes whatever `--last-page-reached` says.

## Files

| File | Required | Paged | Source tool | Rows are |
|---|---|---|---|---|
| `sf_accounts.json` | yes | yes | Salesforce `soqlQuery` | `Account` records with `Assigned_SDR__c = <owner>`, NOT filtered by country (ANZ is filtered in Python) |
| `sf_contacts.json` | yes | yes | Salesforce `soqlQuery` | `Contact` records of those accounts |
| `nooks_calls.json` | yes | yes | Nooks `listCalls` | the owner's FULL call history |
| `nooks_dispositions.json` | yes | no | Nooks `listCallDispositions` | disposition rows (names resolve ids) |
| `nooks_prospects.json` | yes | yes | Nooks `listProspects` | prospects on the candidate accounts (or the owner's) |
| `nooks_accounts.json` | no | yes | Nooks `listAccounts` | Nooks accounts; links to Salesforce are derived (below) |
| `nooks_tasks.json` | no | yes | Nooks `listTasks` | OUR previously created one-off call tasks (never the old `manual_email` backlog) |
| `zoominfo_scoops.json` | yes | yes | ZoomInfo `search_scoops` | scoop rows: `{id, type, attributes{...}}` |
| `gmail_sent.json` | no (dry run) | no | Gmail `search_threads` | threads from `in:sent to:<dotted address> newer_than:7d`, shortlisted prospects only |
| `run_meta.json` | yes | n/a | the session | the run's facts (below) |

A missing optional file is a warning. `gmail_sent.json` missing blocks a live run (sent-email history unknown). `nooks_accounts.json` missing means Nooks-only accounts are not merged. The ZoomInfo company search is not fetched (free search returns only id and name; docs/connector_notes.md): intake passes no ZoomInfo company rows to `build_account_list`.

### Row fields the pipeline reads

- **sf_accounts**: `Id, Name, Website, Type, BillingCountry, BillingState, BillingCity, HQ_Country__c, HQ_State_Province__c, ZoomInfo_Employee_Count__c, ROE_count__c, Current_Customer__c, SDR_Target__c, SDR_Prospecting_Priority__c, Prospecting_Priority__c, DOZISF__ZoomInfo_Id__c, Days_Since_Last_Activity__c` (a Salesforce `attributes` key is harmless). A row with no `Id` or an unusable value is reported as malformed (and listed by `accounts` in its dropped list).
- **sf_contacts**: `Id, AccountId, Phone, MobilePhone, HasOptedOutOfEmail, Title, Email`. `Prospect.phone_e164` and the email opt-out come from here (Nooks prospects carry neither).
- **nooks_calls**: `id, duration, to, prospect{id}, callDisposition{id}, owner{id}, time, createdAt` (plus the rest of the `listCalls` row). `callDisposition` is an id only: names come from `nooks_dispositions`.
- **nooks_dispositions**: `id, name, source, callOutcome, archived`.
- **nooks_prospects**: `id, name, firstName, lastName, title, primaryEmail, crmId, account{id}`.
- **nooks_accounts**: `id, name, domain, numEmployees`. `listAccounts` returns no `crmId` on read, so intake derives Nooks account → Salesforce account from the prospect + Contact join (`prospect.account.id` with `Contact.AccountId` via `prospect.crmId`). A Nooks account whose prospects sit on several Salesforce accounts is left unlinked (counted). If a row already carries `_sf_account_id` (the test fixtures do) it is kept. Raw rows are never rewritten.
- **nooks_tasks**: `id, action, completed, dueAt, status, prospect{id}, sequence{id}, email{id}, owner{id}`.
- **zoominfo_scoops**: `id, attributes{id, company{id, name}, description, publishedDate, originalPublishedDate, topics[{id, topic}], types[{id, type}], link}`.
- **gmail_sent**: threads `{id, messages:[{id, threadId, toRecipients[...], internalDate?}]}` as `search_threads` returns them in metadata view (no bodies). Returned addresses have the dots stripped from the local part; the loaders compare on a dotless key.

## run_meta.json

A plain JSON object (not `{meta, rows}`):

```json
{"fetched_at": "2026-11-25T02:10:00Z",
 "sf_user_id": "<resolved by getUserInfo at run time>",
 "nooks_user_id": "<resolved by getMe at run time>",
 "row_counts": {"sf_accounts": 20112, "sf_contacts": 41000, "nooks_calls": 12840, "...": 0},
 "reached_last_page": {"sf_accounts": true, "sf_contacts": true, "nooks_calls": true, "nooks_prospects": true},
 "zoominfo_credits_used": 0,
 "warnings": ["free text, one per surprise the session noticed"]}
```

- `fetched_at`, `sf_user_id`, `nooks_user_id`, `row_counts` are required. `row_counts[file]` is the number of rows written to that file (before any de-duplication). A mismatch is an ERROR (truncated or edited file).
- `nooks_user_id` must equal `config.owner.nooks_user_id`, otherwise the run stops (the data belongs to someone else).
- `reached_last_page` per paged source must agree with the file's `meta`; on disagreement the cautious value (`false`) wins and a warning is logged.
- `zoominfo_credits_used`: integer, `0` when only free searches ran. Missing is a warning and blocks live (credits unknown, CLAUDE.md rule 7). Above `limits.zoominfo_credit_budget_per_run` is a warning and blocks live.

## What intake reports (`IntakeReport`)

| Severity | Cases |
|---|---|
| **error** (pipeline stops) | a required file missing; invalid JSON; not `{meta, rows}`; a row that is not an object; `run_meta` missing `fetched_at` / `sf_user_id` / `nooks_user_id` / `row_counts`; row count differs from `run_meta`; no dispositions; `nooks_user_id` not the configured owner |
| **warning** (logged) | optional file missing; `meta` key missing; a paged source did not reach the last page; `run_meta` / file disagree on that; stale data; duplicates (same id; first kept); malformed rows per source (count + up to 5 scrubbed sample reasons, > 5% is flagged as a possible shape change); calls by another owner; calls whose disposition id is not in the list; prospects with a `crmId` but no Contact row; credits unknown or over budget |
| **live blocker** (`can_go_live` is False) | any error; `nooks_calls` (or `sf_accounts`, `sf_contacts`, `nooks_prospects`, `nooks_tasks`) did not reach the last page; data older than 24 h (or a `fetched_at` in the future, or unknown); malformed call / contact / prospect rows; calls with an unknown disposition; no call history; `gmail_sent.json` missing; credits unknown or over budget; the Do Not Call rebuild rejected |

A malformed row is never skipped silently: it is counted in `IntakeReport.malformed[source]` with sample reasons, and again in the run log. Reasons and warnings are scrubbed of anything that looks like an email address or phone number.

`load_raw` never raises for bad data: read `RawData.report`. A dry run continues on warnings; `reached_last_page == false` on `nooks_calls` means Do Not Call is NOT rebuilt that run (the older marker in `state.json` is not trusted for a live run) and a live run is impossible.

## De-duplication and order

Rows are de-duplicated by id per file (`Id` for Salesforce, normalised to 15 characters; `id` elsewhere); the FIRST row wins. File order never changes the pipeline result (tested with shuffled files) as long as duplicate ids carry identical content.
