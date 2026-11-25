# Fetch runbook (task 2a)

How a Claude session fills `output/raw/` so `python -m src.pipeline` can run. Python cannot call MCP connectors; only a Claude session can. This file is the recipe. The file formats it must produce are in `docs/intake_schema.md`. Tool behaviour comes from `docs/connector_notes.md`. **Parameter names below marked UNVERIFIED are not confirmed against the live tool schemas: read each tool's schema (ToolSearch) before the first call and write down any difference in `run_meta.warnings`.**

Placeholders only in this file: `<SF_USER_ID>`, `<NOOKS_USER_ID>`, `<LAST_ID>`, `<ADDR>`. Never write real ids, names, emails or phone numbers into the repo (rule 4). `output/` and `state/` are gitignored; keep every fetched file there.

## 0. Read-only rules (every session, every run)

- **Read-only tools only.** Never call a create / update / delete / send tool of any connector. Specifically banned here: Gmail `send_message`, `reply`, `forward`, `create_draft` (drafts are Phase 3); Nooks `createTask`, `createEmail`, `createCall`, `sendTaskEmail`, `updateTask`, `deleteTask`, `createAccountNote`, `createProspectNote`, `syncProspects`, `updateProspect`, any sequence write; every Salesforce `create*`, `update*`, `delete*` tool.
- **Salesforce:** `soqlQuery`, `find`, `getObjectSchema`, `getUserInfo` only.
- **ZoomInfo credits (rule 7).** Search is free. NEVER call `enrich_companies`, `enrich_contacts`, `enrich_company_signals`, `contact_research`, `account_research`, `get_recommended_contacts`, `find_similar_*`, or any call with `redeem` set. The runbook needs none of them. Keep a counter: it starts at 0, and each credit-costing call (there should be none) adds its cost. Write the final number to `run_meta.json` as `zoominfo_credits_used`. If it is not 0, say so in `run_meta.warnings`: the pipeline blocks a live run on it.
- **No faking (rule 6).** If a tool is missing, errors, or a page fails: stop that source, write what you have with `reached_last_page: false`, add a line to `run_meta.warnings`, and carry on with the other sources. Never fill a gap with invented rows, never retry forever (3 tries with a short pause per page, then stop that source).
- **Context hygiene.** Large results are spilled to a file by the harness. Do not read them into the conversation: pass the file path to `scripts/merge_pages.py` (section 8). Real data never goes into a chat summary beyond counts.
- Tool names contain connector ids that change on reconnect: load tools by role with ToolSearch (`soqlQuery`, `listCalls`, `search_scoops` ...), never hard-code the prefixed names. Nooks tools need `USER_GOAL` and `REASON`; ZoomInfo needs `userIntent` (10+ chars).

Set `FETCHED_AT` per file when merging (the default is the merge time) and record the run start for `run_meta`.

## 1. Resolve the owner ids at run time

1. Salesforce `getUserInfo` → `identity.userId` is `<SF_USER_ID>`. (Its timezone is NOT the prospect's: ignore it for call windows.)
2. Nooks `getMe` → the user id is `<NOOKS_USER_ID>`. It must equal `owner.nooks_user_id` in `config/config.yaml`; if not, STOP: this is someone else's seat (the pipeline also refuses).
3. Put both in `run_meta.json` (section 9). Do not store them anywhere else.

## 2. Salesforce accounts → `sf_accounts.json`

Keyset paging (never OFFSET, never `nextRecordsUrl`; verified: exactly 2000 rows per call, `done: true` every time, ~760 KB per page so it always spills, ~19.7k assigned accounts = about 10 pages). `HQ_Country__c` IS usable in `WHERE` (`=` and `IN`; mixed case such as `AUSTRALIA` matches), so ANZ may be filtered server-side. `BillingCountry` is null for ~25% of the book, so ALWAYS test both fields; the Python country rule (`resolve_country`) stays the authority and still flags non-ANZ-billing / ANZ-HQ accounts (codes `AU` / `NZ` in `HQ_Country__c` were not tested: if a first run looks short, drop the country line and filter in Python only).

Optional check first: `SELECT COUNT() FROM Account WHERE Assigned_SDR__c = '<SF_USER_ID>' AND Current_Customer__c = false`.

```sql
SELECT Id, Name, Website, Type, BillingCountry, BillingState, BillingCity, HQ_Country__c,
       HQ_State_Province__c, ZoomInfo_Employee_Count__c, ROE_count__c, Current_Customer__c,
       SDR_Target__c, SDR_Prospecting_Priority__c, Prospecting_Priority__c,
       DOZISF__ZoomInfo_Id__c, Days_Since_Last_Activity__c
FROM Account
WHERE Assigned_SDR__c = '<SF_USER_ID>' AND Current_Customer__c = false
  AND (BillingCountry IN ('Australia','New Zealand') OR HQ_Country__c IN ('Australia','New Zealand'))
  AND Id > '<LAST_ID>'            -- omit this line on page 1
ORDER BY Id LIMIT 2000
```

Loop: run the query → note the spilled file path → read ONLY the count and the last id (snippet below) → set `<LAST_ID>` → repeat. Stop when a page returns fewer than 2000 rows: that is `reached_last_page = true` (merge with `--derive-last-page --paging salesforce --sf-limit 2000`: `done` is true on EVERY keyset page, so the script requires `done` AND a final page shorter than the limit; it never returns true on a guess). Add `{"path": "<spilled file>"}` to the manifest after each page.

Snippet that prints only the row count and last id of a spilled page (no row content):

```bash
python3 -I -c "import json,sys; d=json.load(open(sys.argv[1])); r=d.get('records', d if isinstance(d,list) else []); print(len(r), r[-1]['Id'] if r else None)" "<spilled file>"
```

If a query is rejected (field missing), list real field names with `SELECT QualifiedApiName, DataType FROM FieldDefinition WHERE EntityDefinition.QualifiedApiName = 'Account' AND QualifiedApiName LIKE '%ZoomInfo%'`. **Do not use `getObjectSchema`**: it is huge and failed in the probe session (`UnsupportedMockSessionOperationException`); the `FieldDefinition` query is the working fallback.

## 3. Salesforce contacts → `sf_contacts.json` (these ARE the prospects)

Prospects come from Salesforce Contacts: name, title, email, phone and the email opt-out live only here. Same keyset recipe through the relationship filter (verified: ~54k contacts, about 27 pages of ~600 KB, ~16 MB spilled in total):

```sql
SELECT Id, AccountId, FirstName, LastName, Phone, MobilePhone, HasOptedOutOfEmail,
       Sales_Opt_Out_Email__c, Title, Email
FROM Contact
WHERE Account.Assigned_SDR__c = '<SF_USER_ID>' AND Account.Current_Customer__c = false
  AND Id > '<LAST_ID>'            -- omit on page 1
ORDER BY Id LIMIT 2000
```

Optionally add `AND (Account.BillingCountry IN ('Australia','New Zealand') OR Account.HQ_Country__c IN ('Australia','New Zealand'))` to shrink it. Contact has NO `DoNotCall` or `Do_Not_Call__c` field (INVALID_FIELD). `Sales_Opt_Out_Email__c` has an UNVERIFIED meaning: it is fetched and COUNTED only (it does not block anyone). Same loop, same stop rule (a page under 2000 rows). Probe: `MobilePhone` ~29%, `Phone` ~91%, either ~94%, `HasOptedOutOfEmail` ~24%. A partial contact list pushes accounts into `needs_prospect`: record `reached_last_page` honestly.

## 4. Nooks call history → `nooks_calls.json`, `nooks_dispositions.json`

**Dispositions** (one call, ~33 rows, `page_size` 100, `links.next` null): `listCallDispositions` → save the rows. Ids are never hard-coded: the pipeline resolves by NAME across ALL sources (duplicate names such as "Left Voicemail" exist as separate ids), counts calls whose disposition id is unknown, and stops if a configured name is missing.

**Calls** (the Do Not Call list is rebuilt from this, so it must be the FULL history): `listCalls` with **`filter_owner_id=<NOOKS_USER_ID>` ALWAYS** (verified: without it a page mixes ~40 reps and the sources `nooks_sep`, `outreach`, `csv`, `salesforce`; the filter is NOT a permission boundary, and other reps' Do Not Calls are invisible under it), no time filter, `page_size=100` (the maximum; 500 is rejected), the same page size on every page, and the cursor from the previous response: take it from `links.next` (it carries `page[after]=<cursor>`; the response `hint` names the tool parameter, `page_after`, so read the tool schema for the exact parameter name and pass the cursor value, an opaque base64 string, unchanged; ids ascend by uuid, not by time). Volume: about 1.6k to 1.8k calls (~17 pages, ~75 KB per page, always spilled). Cheap enough to refetch in full every run.

**There is NO `hasMore` field.** The end is `links.next == null` on the final page. Never infer the end from a short page. Per page read only `links.next` from the spilled file, add the file to the manifest, continue. Merge with `--derive-last-page` (paging `nooks`, the default): it derives the flag from the FINAL page's `links.next` (null → `true`, a cursor → `false`). If paging stops for any other reason (error, rate limit, your own cap) the final page still has a `links.next`, so `auto` correctly writes `false`: the pipeline then does not rebuild Do Not Call and a live run is impossible. A hand-built cursor is UNVERIFIED as a feature: do not use it.

Known limits, also stated in every log header: (a) **Do Not Call is visible only for Nooks-dialer (`nooks_sep`) calls**: Outreach / CSV / Salesforce-sourced calls use other disposition sets with no "Do Not Call" (about 85% of the owner's calls are `nooks_sep`); (b) about 19% of calls have `prospect: null` and `from`/`to` = `external`: the literal `external` is not a phone (`to_e164` becomes None, counted in the log) and such calls cannot be matched to anyone; (c) `to` is ~99% E.164, with the odd spaced international form from a `salesforce`-sourced row. Keep these rows; the loader tolerates them.

## 5. Nooks tasks, prospects, accounts

- **`nooks_tasks.json`** (needed for a live run: it is the "last sheet" memory because `state.json` is ephemeral): `listTasks` with `filter_owner_id=<NOOKS_USER_ID>`, `filter_action=call`, **`filter_sequenceState_state=null`** (one-off tasks only; verified: without that filter the listing is dominated by ~4k sequence tasks, never use it) and NO status filter (completed and skipped tasks count as "on a sheet"). `page_size=100`, `links.next` paging, merge with `--derive-last-page`. `note` is returned in the list. Rows tagged `[apac-callsheet <date> #<rank>]` (Phase 3 writes these, ideally with ` acct=<SF account id>` before the `]`) define last night's accounts; untagged rows (a few hand-made tasks exist) are ignored. Never fetch or touch the `manual_email` sequence backlog. Before Phase 3 has written anything this file holds only untagged rows (or none): that is fine, write it anyway.
- **`nooks_prospects.json`** (optional, lazy): NEVER list prospects in bulk (the table is workspace-wide, about 3M rows, no phone or opt-out field). Only after the first dry run, and only for what is needed: `listProspects` with `filter_crmId=<Contact id>` (single value; comma lists are UNVERIFIED) for shortlisted Contacts whose Nooks id Phase 3 needs, and `filter_id=<comma-separated prospect uuids>` for the prospects named in tagged tasks. Merge them as one file (`reached_last_page` is not meaningful; pass no flag). `crmId` can be a Lead id (`00Q...`): only `003...` ids join to Contacts; `account` is null for ~1/5 of rows. Without this file `Prospect.nooks_id` stays None (the pipeline infers some ids from unique call phones).
- **`nooks_accounts.json`** (optional): `listAccounts`, `page_size=100`, paged. No country or state; no `crmId` on read. Skip unless needed.

## 6. ZoomInfo scoops → `zoominfo_scoops.json`

`search_scoops` is free (credits must stay 0). `userIntent` (10 to 500 chars) on every call. Verified: `pageSize` max 100 (~60 KB per page, spills); the employee filter works. **CORRECTED by the first live dry run (2e): `meta.page.total` is the ROW COUNT OF THAT PAGE (100 on a full page), NOT the page count, and `meta.totalResults` DRIFTS between pages and is not exact (see section 11). The end of a type is the first page with fewer than 100 rows or with no `data` key.** Countries `Australia` and `New Zealand`, `employeeRangeMax=2000`. **Fetch ONLY the buyer types, ONE CALL SEQUENCE PER TYPE** so each count can be logged (90-day AU+NZ counts from the probe in brackets):

| Window | Types |
|---|---|
| last **90 days** (people moves) | `Executive Move` (~1k), `New Hire` (~220), `Management Move` (0: optional, expect nothing) |
| last **45 days** | `Mergers & Acquisitions (M&A)` (~700), `Funding` (~270), `Partnership` (~250), `Product Launch` (~35) |

**Skip `Open Position`** (~10k rows, noise) and `Hiring Plans` / `Project` / `Pain Point` in Phase 2. Funding attaches to the investee (company = the company that raised; investors only in the description; the investor-verb rule still applies). The date-filter parameter name is UNVERIFIED: read the schema. Page each type to its end (a page shorter than `pageSize`, or a response with no `data`; NOT `page.number == page.total`), merge ALL types into one file with `merge_pages.py` (a scoop with several types, e.g. "Executive Move" plus "Left Company", is de-duplicated by scoop id), and put the per-type totals into `run_meta.scoop_counts_by_type`. `reached_last_page` for the merged file is `true` only if every type ended. `--derive-last-page --paging zoominfo` returns False because it assumes `page.total` is the page count: after seeing every type end yourself, merge with `--last-page-reached true` and say so in `run_meta.warnings`.

Join: scoop `company.id` equals `DOZISF__ZoomInfo_Id__c` (~98% filled). Only ~30% of scoop companies are in Alex's book; unmatched scoops are normal and the log reports `scoops matched to your book: X of Y`. **Do not call `search_companies`** and **never enrich or redeem**.

**ZoomInfo intent is OUT of Phase 2 (deferred): do not call `search_intent` and do not fetch `lookup intent-topics`.** Its live row shape is nested JSON:API unlike our placeholder, the guessed topic names do not exist, and ~2.6k ANZ CX rows are noisy. `signals.intent_to_signals` is not wired.

## 7. Gmail sent history → `gmail_sent.json` (last, shortlisted only)

Needed to avoid emailing someone emailed in the last 7 days; not needed for the ranking. Do it AFTER a first dry run, for the shortlisted prospects only (the chosen contacts of the ranked accounts, `PipelineResult.rank_result.prospects`), never for all ~12,000: for each, `search_threads` with query `in:sent to:<ADDR> newer_than:7d` and `view=THREAD_VIEW_METADATA_ONLY` (no bodies). `<ADDR>` is the REAL dotted address from Salesforce (a query built from the dot-stripped form finds nothing; returned addresses are dotless and the loaders compare on a dotless key).

**Verified: search results carry NO date** (only `id`, `sender`, `toRecipients`, `threadId`, `messageCount`). So the QUERY WINDOW is the date: any hit means "emailed within the last 7 days"; `sent_at` stays None. Rule: the pipeline blocks such a prospect until the search day plus `email_cooldown_days` (7), i.e. exactly until the window could have elapsed (it anchors on `gmail_sent.json` `meta.fetched_at`). So: the window in the query must be at least `email_cooldown_days`, `meta.window_days` must be set (merge with `--source-tool search_threads --query "in:sent to:<addr> newer_than:7d"`: the `newer_than:7d` is also parsed), and the search must be done on the day of the run. Merge all threads (de-duplicated by thread id; envelope `{"threads": [...]}`; no `--last-page-reached`) into `gmail_sent.json`; an empty result is valid. Then re-run the pipeline. Addresses must not be written into the repo, the log or the manifest. Reading a real send date would need `get_thread` (bodies): not allowed here.

## 7b. Exact parameters (from the probe, `docs/connector_notes_v2.md`)

| Tool | Parameters (names exactly) |
|---|---|
| Nooks `listCalls` | `filter_owner_id=<NOOKS_USER_ID>` (ALWAYS), `page_size=100`, cursor from `links.next` (`page[after]`; tool parameter `page_after`, confirm in the schema); optional `filter_time_gte` (not used: full history); plus `USER_GOAL`, `REASON` |
| Nooks `listCallDispositions` | `page_size=100`, `USER_GOAL`, `REASON` |
| Nooks `listTasks` | `filter_owner_id`, `filter_action=call`, `filter_sequenceState_state=null`, `page_size=100`, cursor as above (no status filter) |
| Nooks `listProspects` (lazy only) | `filter_crmId=<Contact id>` (single value) or `filter_id=<uuid,uuid,...>`; `page_size=100` |
| Salesforce `soqlQuery` | the query text only (no other parameter) |
| Salesforce `getUserInfo` | none |
| ZoomInfo `search_scoops` | `userIntent` (10 to 500 chars), `country="Australia,New Zealand"`, scoop type names verbatim in `scoopTypes` (`Executive Move`, `New Hire`, `Management Move`, `Mergers & Acquisitions (M&A)`, `Funding`, `Partnership`, `Product Launch`), `employeeRangeMax=2000`, `pageSize=100` (`pageSize=1` for a free count: read `meta.totalResults`), `scoopTypes` (ARRAY, e.g. `["Executive Move"]`), `publishedStartDate=YYYY-MM-DD` (VERIFIED name), `page=N` |
| Gmail `search_threads` | `query="in:sent to:<ADDR> newer_than:7d"`, `view=THREAD_VIEW_METADATA_ONLY` |

## 7c. Spill-safe handling (never read a page into context)

Never `Read`, `cat` or print a spilled file. Results over ~20 to 30k characters are saved to a single-line JSON file by the harness (Nooks 100-row page ~75 KB, Salesforce 2000-row page 0.6 to 0.8 MB, ZoomInfo 100-row page ~60 KB). Only these snippets (they print counts and one cursor, nothing else):

```bash
# Nooks (listCalls / listTasks): row count and whether a next page exists
python3 -I -c "import json,sys; d=json.load(open(sys.argv[1])); print(len(d.get('data',[])), bool((d.get('links') or {}).get('next')))" "<spilled file>"
# Nooks: the next cursor value, only when you need to pass it (this prints the cursor, not data)
python3 -I -c "import json,sys; print((json.load(open(sys.argv[1])).get('links') or {}).get('next'))" "<spilled file>"
# ZoomInfo (search_scoops): rows, page number / total, totalResults
python3 -I -c "import json,sys; d=json.load(open(sys.argv[1])); m=d.get('meta',{}); print(len(d.get('data',[])), m.get('page'), m.get('totalResults'))" "<spilled file>"
```

(the Salesforce snippet is in section 2). A tool result that came back INLINE (small) is written to a scratch file with `Write` and added to the manifest the same way; do not paste rows into a command.

## 7d. Stop / retry rule per source

Per page: up to 3 tries with a short pause. After a timeout or error in the MIDDLE of a source, re-run THAT page with the same cursor or `<LAST_ID>`; never skip a page and never restart with a different page size. If a page still fails: stop that source, merge the pages already fetched with the end flag derived as usual (it will be `false`), add a line to `run_meta.warnings`, continue with the next source. Nooks failures can be inspected with `getRecentFailures` (read-only). Never retry anything that looks like a write (none is called here). Salesforce and Nooks pages are cheap to refetch: if in doubt, redo the whole source.

## 7e. Shortlisted addresses for the Gmail step without printing them

After the first dry run, write the search list to a gitignored scratch file and read ONE address at a time into the tool call (never print the list):

```bash
python3 -I - <<'EOF'
import json
# run_pipeline(...) result `r` from the dry run; the dotted addresses never go to chat or git
rows = [p.email for ps in r.rank_result.prospects.values() for p in ps if p.email]
open("output/gmail_queries.json", "w").write(json.dumps(sorted(set(rows))))
print(len(rows), "addresses written to output/gmail_queries.json")
EOF
```

(Run it in the same Python process as the pipeline run, or have the run save it; `output/` is gitignored.) Use the addresses from that file one by one in the `search_threads` query text; the printed output stays at a count.

## 7f. Call-count estimate

About 18 (Nooks calls) + 1 (dispositions) + 1 to 3 (tasks) + 10 (SF accounts) + 27 (SF contacts) + 6 to 10 (scoops, per type) + at most 40 (Gmail, shortlist) = **65 to 110 calls**, roughly 5 to 10 minutes if a call takes 3 to 6 s (per-call time NOT measured; no rate limit was hit in ~35 probe calls).

## 8. Merging spilled pages: `scripts/merge_pages.py`

For each source, write a manifest (JSON list, in page order) in `output/raw/` or the scratchpad, never in git:

```json
[{"path": "/path/to/spilled/page1.json"}, {"path": "/path/to/spilled/page2.json"}]
```

then:

```bash
python scripts/merge_pages.py --manifest manifest_calls.json --name nooks_calls --raw-dir output/raw \
    --source-tool listCalls --query "filter_owner_id=<NOOKS_USER_ID>, page_size=100" --derive-last-page
```

- `--name` is the file stem from `docs/intake_schema.md` (`sf_accounts`, `sf_contacts`, `nooks_calls`, `nooks_dispositions`, `nooks_prospects`, `nooks_accounts`, `nooks_tasks`, `zoominfo_scoops`, `gmail_sent`).
- Omit both `--derive-last-page` and `--last-page-reached` for non-paged sources (`nooks_dispositions`, `gmail_sent`, `nooks_prospects`). For paged ones use `--derive-last-page --paging nooks|salesforce|zoominfo` (the script reads the pages and decides; `zoominfo` requires every per-type page sequence to run 1..total with no gap and the rows to add up to `meta.totalResults`) or, as a last resort, `--last-page-reached true|false` exactly as you SAW it. The two options cannot be combined.
- `--id-field Id` is not needed: `Id` is accepted as a fallback. `--allow-no-id` keeps rows with no id (default: error).
- With no evidence for the chosen `--paging` rule the script exits 2 and writes nothing (it never defaults to `true`).
- Understood page envelopes: a bare list; `{"data": [...]}` (Nooks, ZoomInfo); `{"records": [...]}` (Salesforce); `{"threads": [...]}` (Gmail); `{"results"}`, `{"items"}`, `{"rows"}`; any object with exactly one list value; and an MCP text wrapper (`[{"type":"text","text":"<json>"}]` or `{"content":[...]}`) holding one of those. Anything else is an error and nothing is written.
- It de-duplicates by id (first wins), keeps order, and prints only counts. Read that line; it is the only output.
- For a small result that was NOT spilled (it came back inline), write it to a scratch file first (`Write` the tool result) and add that file to the manifest; do not paste data into commands.

## 9. `run_meta.json` and the run

After all sources are merged, write `output/raw/run_meta.json` (format in `docs/intake_schema.md`): `fetched_at` (UTC, the time the fetch finished), the two resolved ids, `row_counts` (read each merged file's row count from the `merge_pages.py` output), `reached_last_page` per paged source, `zoominfo_credits_used`, `warnings`.

Then:

```bash
python -m src.pipeline --raw-dir output/raw --state-dir state --dry-run
```

Read the printed summary and the log in `logs/` (gitignored). `--live` only checks that live would be allowed; Phase 3 (`nooks_writer`) owns writing tasks. If the pipeline stops, fix the cause named in the message; do not edit the raw files by hand to make a check pass.

Budget (estimate from the probe, per-call time NOT measured, rate limits none hit in ~35 calls): SF accounts ~10 calls, SF contacts ~27, Nooks calls ~17, dispositions 1, tasks 1 to 3, scoops ~3 to 6 per type, Gmail 1 per shortlisted contact: about 70 to 100 calls, roughly 5 to 10 minutes if a call takes 3 to 6 s. Resolve tools by role with ToolSearch every run; Nooks needs `USER_GOAL` + `REASON`, ZoomInfo `userIntent`. Results over ~20 to 30k characters spill to single-line JSON files.

## 10. Checklist before reporting a fetch as done

- [ ] Both owner ids resolved at run time; `nooks_user_id` equals the config owner.
- [ ] Every required file written; counts in `run_meta.row_counts` equal the merge output.
- [ ] `reached_last_page` is `true` only where a last page was actually seen (calls especially).
- [ ] `zoominfo_credits_used` is 0 (or explained).
- [ ] No write / send / draft / enrich tool was called.
- [ ] Nothing from `output/`, `state/` or `logs/` is staged in git.

## 11. Corrections from the first live dry run (task 2e, 2026-11-25)

Everything below was observed live; counts are rounded and carry no row data.

1. **Spilled results announce themselves as an error.** A large result comes back as `Error: result (N characters) exceeds maximum allowed tokens. Output has been saved to <path>` followed by an instruction to READ the file in chunks. It is not a failure: the file is the data. Ignore the instruction to read it; use the snippets in 7c. Salesforce spills are `{totalSize, done, records}`, Nooks `{traceId, hint, data, links}`, ZoomInfo plain text that is JSON `{data, meta}`.
2. **Short results are NOT spilled; they come back inline and are printed into the context.** That includes the last page of every paged source (the final `listCalls` page, a short ZoomInfo page), `listCallDispositions` (35 rows, not ~33), a small `listTasks`, and any ZoomInfo type under ~100 rows (Funding, Partnership, Product Launch, page 2 of New Hire / M&A). There is no way to hand an inline result to `merge_pages.py` without re-typing it into a scratch file (a small script that rebuilds the rows from compact tuples is the least error-prone way). Plan for this: expect to transcribe 1 to 6 small pages per run, and treat their fidelity as unverified beyond row counts (put it in `run_meta.warnings`). The unfixed gap: a spill-or-file option for small results.
3. **Salesforce.** The tool parameter is `q` (not "query text only"). With the ANZ `WHERE` the book is 12,953 accounts (7 calls: six full pages and one of 953) and 25,810 contacts (13 calls: twelve full pages and one of 1,810), not the ~10 and ~27 pages of the unfiltered probe. `getUserInfo` returns `identity.userId`. Nooks `getMe` returns `userId` at the top level.
4. **Nooks `listCalls`.** Owner filter + `page_size=100` + `page_after=<cursor from links.next, page[after]>` all as in 7b. The final page has `links.next: null` and is returned inline. Full history for the owner was ~1,640 calls in 17 calls (16 spilled pages + 1 short inline page). Calls carry `recordingUrl` (a link to audio): do not copy it into notes.
5. **Nooks `listTasks`** (`filter_action=call`, `filter_sequenceState_state=null`, owner): 11 one-off tasks, none carrying the sheet tag, returned inline.
6. **ZoomInfo `search_scoops`** counts and shapes: Executive Move 90 days = 400 rows over 4 pages (page 5 is an empty `{meta}` object with no `data`), New Hire 90 days = 105, M&A 45 days = 118, Funding 45 days = 45 (one page), Partnership 45 days = 62 (one page), Product Launch 45 days = 24 (one page): far fewer than the probe's ~1k / ~220 / ~700 / ~270 / ~250 / ~35 estimates (those were per 90 days and before the 2,000-employee cap). `meta.totalResults` was 458, 435, 437, 383 on the four Executive Move pages (400 rows) and 53 for a Partnership search that returned 62 rows: do not use it as an end check or as a log of "expected rows". A scoop can match several types (754 rows in, 648 after id de-duplication). Company ids join to `DOZISF__ZoomInfo_Id__c` as described.
7. **Call budget measured.** Fetch calls: Salesforce 21 (getUserInfo + 7 accounts + 13 contacts), Nooks 20 (getMe, dispositions, 17 calls, 1 tasks), ZoomInfo 12, Gmail 10 (one per shortlisted contact): 63 in total. Wall time from the first call to the pipeline result was about 11 to 12 minutes, dominated by model turns between pages (a hand-copied cursor / last id per page): Salesforce accounts ~1 min, contacts ~2 min, Nooks calls ~3.5 min, ZoomInfo ~4.5 min. Connector latency itself was a few seconds per call.
8. **Gmail.** `search_threads` with `in:sent to:<dotted address> newer_than:7d` and `THREAD_VIEW_METADATA_ONLY` returns exactly `{}` when there is no match. Reading each shortlisted address into a tool call means the address appears once in the session's own context: do not print the whole list, one `python3 -I -c` per address as in 7e. The list file is written by `scripts/dry_run_sheet.py`'s `build()` (`ln.entry.prospect.email` over `sheet.lines`).
9. **Sheet.** `python scripts/dry_run_sheet.py --raw-dir output/raw --state-dir state --log-dir logs --limit 10` runs the pipeline in dry-run mode, builds the 10-account sheet and writes `output/dry_run_sheet.txt` / `.md` (gitignored). Each run of the pipeline also writes its own log under `logs/`.
