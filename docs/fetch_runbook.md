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

`HQ_Country__c` is a textarea and cannot be used in `WHERE`, so do NOT filter country in SOQL: pull everything assigned to the BDR and the pipeline filters ANZ in Python (`resolve_country`, which also keeps the ~420 non-ANZ-billing / ANZ-HQ accounts flagged instead of losing them).

Optional first check: `SELECT COUNT() FROM Account WHERE Assigned_SDR__c = '<SF_USER_ID>' AND Current_Customer__c = false` (expect about 20,000 minus customers).

Keyset paging (never OFFSET, never `nextRecordsUrl`): one query per page, 2000 rows, `ORDER BY Id`, then `Id > <LAST_ID>`:

```sql
SELECT Id, Name, Website, Type, BillingCountry, BillingState, BillingCity, HQ_Country__c,
       HQ_State_Province__c, ZoomInfo_Employee_Count__c, ROE_count__c, Current_Customer__c,
       SDR_Target__c, SDR_Prospecting_Priority__c, Prospecting_Priority__c,
       DOZISF__ZoomInfo_Id__c, Days_Since_Last_Activity__c
FROM Account
WHERE Assigned_SDR__c = '<SF_USER_ID>' AND Current_Customer__c = false
  AND Id > '<LAST_ID>'            -- omit this line on page 1
ORDER BY Id LIMIT 2000
```

Loop: run the query → note the spilled file path (or the inline result) → read ONLY the count and the last id (snippet below) → set `<LAST_ID>` → repeat. Stop when a page returns fewer than 2000 rows (or `done: true` with fewer than the limit): that is `reached_last_page = true`. If the connector caps rows lower than 2000 (UNVERIFIED), use its cap as the page size and compare against that. Append `{"path": "<spilled file>"}` to a manifest after each page.

Snippet that prints only the row count and last id of a spilled page (no row content):

```bash
python3 -I -c "import json,sys; d=json.load(open(sys.argv[1])); r=d.get('records', d if isinstance(d,list) else []); print(len(r), r[-1]['Id'] if r else None)" "<spilled file>"
```

## 3. Salesforce contacts → `sf_contacts.json`

Phones and email opt-outs come only from the Contact (Nooks prospect rows carry neither). Use the relationship filter with the same keyset paging:

```sql
SELECT Id, AccountId, Phone, MobilePhone, HasOptedOutOfEmail, Title, Email
FROM Contact
WHERE Account.Assigned_SDR__c = '<SF_USER_ID>' AND Account.Current_Customer__c = false
  AND Id > '<LAST_ID>'            -- omit on page 1
ORDER BY Id LIMIT 2000
```

Expect tens of thousands of rows (many pages). Same loop, same stop rule, same manifest approach. A partial contact list makes accounts fall into `needs_prospect: persona_match_no_phone`: record `reached_last_page` honestly.

## 4. Nooks call history → `nooks_calls.json`, `nooks_dispositions.json`

**Dispositions** (small, not paged): `listCallDispositions` (no filter beyond what the tool requires) → save the rows. Ids are never hard-coded; the pipeline resolves names and stops if a configured name is missing.

**Calls** (the Do Not Call list is rebuilt from this, so it must be the FULL history, not a recent window): `listCalls` with the owner filter set to `<NOOKS_USER_ID>` (the connector notes say the `owner` filter works) and NO time filter. Use ONE fixed `page_size` for every page (the largest the tool accepts; cursors are tied to it) and the cursor from the previous response (`page_after`, from `links.next`). Pages are ordered by id, not time, so the only valid end is `hasMore == false`.

Per page, read only `hasMore` and the next cursor from the spilled file (not the rows), add the file to the manifest, continue. Record `reached_last_page = true` ONLY when a page said `hasMore == false`. If paging stops for any other reason (error, rate limit, your own page cap), merge what you have with `--last-page-reached false`: the pipeline then does not rebuild Do Not Call and a live run is impossible. About 1 call in 5 has `prospect: null`; keep those rows.

## 5. Nooks prospects, accounts, our tasks

- **`nooks_prospects.json`**: `listProspects`, owner filter if the tool has one (UNVERIFIED), paged to `hasMore == false` like calls. Prospects are needed for every account that may be ranked, so prefer the full list. Only if it is too large, fetch prospects for a shortlist of accounts with the tool's account filter (UNVERIFIED parameter name) and record `reached_last_page = false`: the sheet is then a shortlist run and live is blocked.
- **`nooks_accounts.json`** (optional): `listAccounts`, paged. No country or state; no `crmId` on read, so do not try `filter_crmId` per account (12,000 calls). The pipeline derives the Nooks to Salesforce link from prospects + contacts.
- **`nooks_tasks.json`** (optional): `listTasks` for the owner with `filter_sequenceState_state=null`, i.e. one-off tasks (the ones Phase 3 will create). Never fetch or touch the old `manual_email` sequence backlog. Until Phase 3 exists, skip this file.

## 6. ZoomInfo scoops → `zoominfo_scoops.json`

`search_scoops` is free but counts against a record limit. Countries Australia and New Zealand (`country="Australia,New Zealand"`; not a strict HQ filter, non-ANZ companies appear and are simply unmatched). `userIntent` required. Three calls, merged and de-duplicated by scoop id:

1. People moves, last **90 days**: types `Executive Move`, `New Hire`, `Management Move`.
2. Events, last **45 days**: types `Funding`, `Product Launch`, `Mergers & Acquisitions (M&A)`.
3. Softer signals, last **30 days**: `Partnership`, `Hiring Plans`, `Open Position`, and `Project`/`Pain Point` (the pipeline keeps Project/Pain Point only with a Pendo-buyer topic).

Type names come from `config.signals.scoop_types_keep` (copy them exactly; the ZoomInfo spelling of the M&A type, and whether `Open Position` and `Management Move` exist at all, are UNVERIFIED). The date-filter parameter names are UNVERIFIED: read the schema. Page each call to its end (note `reached_last_page` for the merged file: true only if all three calls ended). **Do not call `search_companies`** (free search returns only id and name, and the Salesforce account already holds `DOZISF__ZoomInfo_Id__c`) **and never enrich or redeem.** `search_intent` is not used in this phase.

## 7. Gmail sent history → `gmail_sent.json` (last, shortlisted only)

Needed to avoid emailing someone emailed in the last 7 days; not needed for the ranking. Do it AFTER a first dry run, for the shortlisted prospects only (the ranked accounts' chosen prospects, `PipelineResult.rank_result.prospects`), never for all ~12,000:

For each shortlisted prospect, `search_threads` with query `in:sent to:<ADDR> newer_than:7d` and `view=THREAD_VIEW_METADATA_ONLY` (no bodies). `<ADDR>` is the REAL dotted address from Nooks / Salesforce: a query built from the dot-stripped form finds nothing. The returned addresses have dots stripped; the loaders compare on a dotless key. Merge all the threads (de-duplicated by thread id, the envelope is `{"threads": [...]}`) into `gmail_sent.json`. An empty result is a valid empty list. Then re-run the pipeline. The addresses used for searching must not be written into the repo, the log or the manifest.

## 8. Merging spilled pages: `scripts/merge_pages.py`

For each source, write a manifest (JSON list, in page order) in `output/raw/` or the scratchpad, never in git:

```json
[{"path": "/path/to/spilled/page1.json"}, {"path": "/path/to/spilled/page2.json"}]
```

then:

```bash
python scripts/merge_pages.py --manifest manifest_calls.json --name nooks_calls --raw-dir output/raw \
    --source-tool listCalls --query "owner=<NOOKS_USER_ID>, page_size=100" --last-page-reached true
```

- `--name` is the file stem from `docs/intake_schema.md` (`sf_accounts`, `sf_contacts`, `nooks_calls`, `nooks_dispositions`, `nooks_prospects`, `nooks_accounts`, `nooks_tasks`, `zoominfo_scoops`, `gmail_sent`).
- Omit `--last-page-reached` for non-paged sources (`nooks_dispositions`, `gmail_sent`); pass `true` or `false` for paged ones exactly as you SAW it.
- `--id-field Id` is not needed: `Id` is accepted as a fallback. `--allow-no-id` keeps rows with no id (default: error).
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

## 10. Checklist before reporting a fetch as done

- [ ] Both owner ids resolved at run time; `nooks_user_id` equals the config owner.
- [ ] Every required file written; counts in `run_meta.row_counts` equal the merge output.
- [ ] `reached_last_page` is `true` only where a last page was actually seen (calls especially).
- [ ] `zoominfo_credits_used` is 0 (or explained).
- [ ] No write / send / draft / enrich tool was called.
- [ ] Nothing from `output/`, `state/` or `logs/` is staged in git.
