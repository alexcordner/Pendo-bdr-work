# Review of task 2a (intake, pipeline, merge_pages, schema, runbook)

Reviewer: independent session, fully offline and read-only. No connector was called; all data is invented. Reviewed commit `44a9c6e` (branch `phase2/2a-intake`) against `origin/phase2/probe-notes` (`docs/connector_notes_v2.md`, `tests/fixtures/probe_*.json`). Scratch tests: `tests/test_review_2a_scratch.py` (25 passing probes, 6 `xfail(strict)` that pin the findings below; they flip to failures once fixed, so remove the marks then).

## Verdict: FAIL for the fetch contract, safety PASS

The Python engine and the safety gates are sound: I could not make `--live` pass in any unsafe state, a dry run cannot record a sheet or write to a connector, and the numbers are good. But **none of the probe findings was incorporated**: the runbook still tells the fetching session to use `hasMore` (which does not exist, so `reached_last_page` could never be set true and live would be impossible forever), to fetch about 10k Open Position scoops, and to bulk-fetch `nooks_prospects` (about 3M rows, not possible). `nooks_prospects.json` is still a required file and the only source of `Prospect` rows, so the realistic fetch yields zero callable prospects. A fix round is needed (runbook, schema, intake, pipeline); the diff is mostly docs plus one new prospect path.

## 1. Tests and determinism
- `pytest`: 1419 passed (worker) → 1450 passed + 6 xfailed with my scratch file. `pytest -W error`: same, clean. PASS.
- `PYTHONHASHSEED` 0/1/2 on `test_pipeline` + `test_intake`: 159 passed each. PASS.
- Shuffled rows in five files (seeded) gives an identical ranked list and scores (`test_shuffle_files_same_result`). PASS.
- Parity with the Phase 1 harness: `tests/test_pipeline.py` compares the full `RankResult` for two `now` values and passes. PASS (I did not re-derive it, only ran it).

## 2. Probe-findings table

| # | Finding | Status | Evidence |
|---|---|---|---|
| a1 | `page_size` max 100 | NOT FIXED | runbook:69 says "the largest the tool accepts"; 100 appears only in the merge example (runbook:107) |
| a2 | no `hasMore`; end = `links.next == null` | **NOT FIXED (critical)** | runbook:69,71,75 and intake_schema:18 say `hasMore == false`; `src/state.py:143` docstring too. `merge_pages.py` takes the flag by hand and cannot derive it from `links.next` |
| a3 | always pass owner filter, not a permission boundary | PARTIAL | runbook:69 passes it ("owner filter works") without the param name `filter_owner_id` or the warning. `intake.py:358` only warns on other-owner calls and live still passes (xfail `test_calls_by_other_owner_only_warning`) |
| a4 | full ~1.7k history refetched each run (~18 pages) | PARTIAL | runbook:69 says FULL history; no page estimate |
| a5 | Outreach calls have no "Do Not Call": DNC visibility limited, state it in the log | NOT FIXED | no such line in `pipeline.py` or the runbook |
| a6 | `to` ~99% E.164, oddities tolerated and counted | PARTIAL | `pipeline.py:_dnc_phone_issues` counts non-E.164 only for DNC calls; `to: "external"` and null tolerated (checked), not counted overall |
| b1 | one-off call tasks only (`filter_action=call`, `filter_sequenceState_state=null`) | PARTIAL | runbook:77 has the null-state filter, not `filter_action=call`/status; the file is optional and "skip until Phase 3" |
| b2 | `note` tag `[apac-callsheet <date> #<rank>]` as 2nd source for last-sheet accounts | **NOT FIXED** | `task_from_nooks` drops `note`; `pipeline.py` step 9 uses `st_mod.last_sheet_ids(st, run_date)` only (xfail `test_probe_b_task_note_tag_feeds_last_sheet`) |
| c1 | prospects not bulk-fetchable; pipeline works with SF Contacts as prospects, `nooks_id` None, file optional | **NOT FIXED (critical)** | `intake.py:39` `nooks_prospects: (True, True)`; `build_prospects` only iterates `nooks_prospects`; without the file the run stops, with an empty file nothing ranks (2 xfails) |
| c2 | live precondition "nooks_prospects reached last page" contradicts probe | **CONTRADICTS** | `intake.py:48` `SAFETY_PAGED` includes `nooks_prospects` and `nooks_tasks`; a live run would be blocked forever |
| c3 | Nooks ids resolved lazily in Phase 3, re-check DNC by prospect id | NOT FIXED | not mentioned in runbook/schema/pipeline docs |
| d1 | keyset paging 2000/page, ~10 account and ~27 contact pages | PARTIAL | keyset and 2000 correct (runbook:~28-60); page counts not stated ("many pages") |
| d2 | `HQ_Country__c` usable in WHERE; Python stays the authority | PARTIAL | runbook:26 still says it "cannot be used in WHERE". Behaviour (pull all, filter in Python) remains valid and safe; the false claim should go. 4.9k null-billing ANZ-HQ accounts are handled by the Python rule (verified in `resolve_country`) |
| d3 | no Contact `DoNotCall`; opt-outs `HasOptedOutOfEmail`, `Sales_Opt_Out_Email__c` | NOT FIXED | contact SELECT (runbook, §3 SELECT) has `HasOptedOutOfEmail` only; `Sales_Opt_Out_Email__c` neither fetched nor logged as undecided. Ask Alex |
| d4 | `FieldDefinition` fallback for `getObjectSchema` | NOT FIXED | runbook:10 lists `getObjectSchema` as allowed, no fallback |
| e1 | only buyer types; Open Position skipped; Management Move optional/empty | **NOT FIXED** | runbook:~81-87 fetches (line 85: Hiring Plans, Open Position, 30 days) `Open Position`, `Hiring Plans`, `Management Move`; `config/config.yaml:86` keeps them |
| e2 | `employeeRangeMax=2000`, 90d people / 45d others (not 30), page size 100 | NOT FIXED | runbook uses 90/45/30 days, no employee filter, no page size |
| e3 | per-type counts, credits 0, matched/unmatched | PARTIAL | credits logged (OK). Log has "kept scoops with no matching account: N" plus a list, but no "scoops matched to your book: X of Y"; no per-type counts |
| e4 | intent out of Phase 2 | FIXED | runbook §6 "search_intent is not used in this phase" |
| f1 | Gmail: no date field; `in:sent to:<dotted> newer_than:7d` per shortlisted contact | FIXED (query) | runbook:93 has the exact query, shortlisted only, metadata view, dotted real address |
| f2 | query-window hit rule | PARTIAL | `state.emailed_recently` treats an undated message as recent until `first_seen + 7d` (up to 7 days longer than the true window; conservative, safe). Not "exactly the window" |
| g | unattended-run notes (tool names embed UUID, ToolSearch per run, required params, spill sizes, rate limits unmeasured) | PARTIAL | runbook:14 covers ToolSearch and `USER_GOAL`/`userIntent`; no spill-size warnings (75 KB Nooks, 0.6-0.8 MB SF), no runtime estimate |

Summary: FIXED 2 (e4, f1), PARTIAL 9, NOT FIXED 12 (counting c2 as contradicted). The probe branch looks not to have been merged into the worker's base.

## 3. Safety (all PASS unless noted)
Scenarios run through `run_pipeline(live=True)`; "refused" means `LiveRefused`/`PipelineStopped`:
- partial Nooks history (file flag false; run_meta false, file true): refused. Stale (+25 h): refused. `fetched_at` in the future: refused. Missing gmail file: refused. Credits unknown / string / `True` / 3 (budget 0): refused. Owner mismatch in `run_meta`: stopped (error). Empty call history: refused. Unknown disposition id: refused. Missing "Do Not Call" disposition name: stopped. Null `callDisposition` on 3 calls: refused (via malformed/unresolved).
- Truncated history that claims last page after a fuller earlier run: refused (`calls_seen < len(seen_calls)`). A truncated FIRST ever run that claims last page is accepted: nothing can detect it (no floor). **Low**; suggest a sanity floor (e.g. warn when calls < 1000 for a seat known to have ~1.7k, or when run_meta/owner row count shrinks).
- Corrupt `state.json` or a stripped DNC marker: live still passes, because the marker is re-derived in the same run from the full history. Correct, but a reset loses sheet history (cooldowns are rebuilt from calls). **Medium** until tagged-task recovery (b2) exists; the log does print the reset reason.
- Dry run: never records a sheet (`record_sheet=True` without live raises `ValueError`; state file has no sheet history after a dry run). A dry run on partial calls saves state with `dnc_rebuilt_at=None`. Saving state in a dry run is **safe**: the live gate does not trust the saved marker; it requires the current raw to reach the last page and re-marks in that run. Residual: `--now` far in the future stamps `first_seen` on undated emails, which lengthens the email suppression (conservative).
- `mark_do_not_call` right before ranking, then the ranker's `is_blocked` safety net: present (`pipeline.py` steps 6, 9). DNC match is by hashed email / phone / prospect id; the dry-run log does not state the Outreach limitation (see a5).
- Hard-rule scan: no banned tool names in `src/` or `merge_pages.py` (the one grep hit is a docstring). **Edit to `test_integration_pipeline.py`:** adding `argparse`/`sys` to the import allowlist for every `src/*.py` and `["pipeline.py","state.py"]` as writers is broader than needed. My narrower tests (`test_narrow_writers_pipeline_only_write_log`, `test_narrow_argparse_sys_only_in_pipeline`) pass today: `argparse`/`sys` only in `pipeline.py`, and the only write helper in `pipeline.py` is `write_log`. Recommend replacing the worker's edit with those.

## 4. Intake
- Required files, JSON validity, `{meta, rows}` shape, non-object rows, `run_meta` fields, row counts vs `run_meta` (error), duplicates (first wins, sorted-by-id), 15/18-char ids, null values, numbers-as-strings, extra keys, a 5 MB row, BOM (loud error): all behave well and malformed rows are counted with scrubbed samples.
- PII: a world run's log and a hostile sample (email in `to`/`callDisposition`) contain no addresses or phone numbers. PASS.
- **F4 (low-medium):** a UTF-16 or non-UTF-8 file raises `UnicodeDecodeError` out of `load_raw` (`intake.py:160`), breaking "never raises for bad data"; `merge_pages.load_pages` and `read_manifest` have the same gap (traceback, exit 1 instead of 2). Fix: catch `ValueError`/`UnicodeError`, read with `utf-8-sig`.
- Missing `meta` for a dump file: warning only; fine.

## 5. merge_pages.py
All documented envelopes work, including the real probe fixtures (Nooks `data`+`links`, SF `records`, ZoomInfo `data`+`meta`, Gmail `threads`). De-dupe by first id wins, order kept, manifest `page` numbers sort, missing file and malformed JSON exit 2, idempotent re-run, paths with spaces fine. 54k contact rows over 27 pages: 0.2 s, 65 MB RSS, 9 MB output. Gaps:
- It cannot derive `reached_last_page`: the session must read `links.next`/`done`/`meta` itself and type the flag. Add `--derive-last-page` that reads the LAST manifest entry (`links.next is None`; SF `done is True` and rows < limit; ZoomInfo `page.number >= page.total` or `totalResults <= rows`) and refuses `true` if the evidence is absent.
- SF dedupe is on the raw `Id` string, not 15-char normalised (intake does normalise, so harmless).
- Duplicate pages are silently dropped but counted in the output line; fine.

## 6. Runbook usability (docs/fetch_runbook.md)
Read-only rules are at the top (§0) and the ban list covers enrich/redeem/account_research and the write tools. Problems for a session that has only deferred MCP tools and spilled results:
1. §4/§5 use `hasMore` and `page_after`: unexecutable. Replace with: loop until `links.next` is null; cursor = value of `page[after]` in `links.next`; record the page count.
2. Parameter names are all "UNVERIFIED" although the probe verified them. Give exact names: `filter_owner_id`, `filter_action=call`, `filter_status=ready,pending`, `filter_sequenceState_state=null`, `page_size=100`, `page[after]`; ZoomInfo `pageSize=100`, `employeeRangeMax=2000`, `country`, scoop type names verbatim, `meta.totalResults`.
3. §6 fetches Open Position (~10k rows, ~100 pages): drop it, Hiring Plans and Management Move; use 90 days for people moves and 45 for the rest; the "30 days" third call should go.
4. §5 prospects: remove as required; fetch nothing in Phase 2 (Contacts are the prospects).
5. §2/§3: state the page counts (about 10 and 27) and "stop after the first short page" rule is correct; add the `FieldDefinition` fallback and a line saying `Sales_Opt_Out_Email__c` is not used until Alex decides.
6. Context hygiene: the only snippet is for SF files. Add one for Nooks (print `len(data)` and `links.next` only), one for ZoomInfo (`meta.totalResults`, `page`), and a rule "never `Read` a spilled file, never `cat` it". Warn about the sizes (75 KB / 0.6-0.8 MB / 60 KB per page).
7. §7: how does the session get shortlisted addresses without printing them? Specify a small script reading `PipelineResult.rank_result.prospects` and writing a scratch query list under `output/` (gitignored), never into chat.
8. Estimated calls: add "about 18 (calls) + 1 + 10 + 27 + 1 + 6 to 10 (scoops) + <= 40 (Gmail) = 65 to 110 calls, 5 to 10 minutes". Realistic per the probe.
9. Retries: "3 tries with a short pause" is fine; add "do not retry a write-looking error" (n/a here) and what to do after a timeout mid-page: re-run that page with the same cursor, never skip it.

## 7. Pipeline logic vs the Phase 1 contract
Order and wiring match the contract: first-pass accounts, `record_calls` over the full history, `mark_dnc_rebuilt(run_date, calls_seen, reached_last_page=True)` only when the last page was seen, `require_healthy(live)`, `mark_do_not_call`, second pass with `cooldown_until` + `call_date`, `group_by_zoominfo_id`, ranker with cooldowns by sf/nooks/zoominfo ids; the sheet is recorded only when live and ranked only (the CLI passes `record_sheet=False` so even live records nothing). `--limit` can only lower the cap (`min`). No raw emails/phones in the log; every needs_prospect / skipped / flagged / unmatched-scoop account is listed by name with reason (5,948 names in the 12k run).
- **Rule 5 vs the 25-name cap on `not_anz` drops (`pipeline.py:_list_dropped`):** I could not exercise it with real non-ANZ rows in my perf world (none dropped as `not_anz`), but by reading the code the other reasons print every name and `not_anz` prints 25 plus a count. CLAUDE.md rule 5 says log what was skipped and why; a count with 25 samples is a defensible reading for a bulk country filter (about 7k accounts), provided the count is in the log (it is). I accept it, with a suggestion to write the full list to a separate `logs/..._not_anz.txt`.
- **Decision 1 (no ZoomInfo-only accounts) is right:** free search returns only id+name, the SF account already carries `DOZISF__ZoomInfo_Id__c` for about 98% of accounts, and a ZoomInfo-only account would have no country, size or phone. It does depart from the original brief's "source c"; record it as an accepted change and put "scoops matched to your book: X of Y" in the log so coverage is visible (probe: ~30% of scoop companies are in the book).
- **Decision 2 (Nooks→SF account link in Python)** is fine for fixtures, but in production it needs prospects (see c1); with Contacts as prospects the link is unnecessary for the dry run.

## 8. Performance (synthetic, offline)
| Scale | Raw size | Runtime | Peak RSS | Log | Result |
|---|---|---|---|---|---|
| 12,000 accounts / 25,000 contacts+prospects / 2,000 calls / 1,500 scoops | 19 MB | 3.0 s | 132 MB | 874 KB, 14k lines | 40 ranked |
| 19,700 / 54,000 / 1,800 / 1,500 (real probe scale) | 36 MB | 5.2 s | 210 MB | ~1.1 MB | 40 ranked |
Fast enough by two orders of magnitude; the runtime is dominated by the fetch (5 to 10 minutes of connector calls), not Python. (Synthetic contacts share the `Prospect` list, so this also approximates the Contact-as-prospect design.)

## Findings by severity

**Critical**
- **F1** `nooks_prospects` is required and the only prospect source (`intake.py:39`, `build_prospects`). Probe: 3M rows, not bulk-fetchable. Repro: delete `nooks_prospects.json` → stopped; empty rows → 0 ranked (`test_probe_c_*`). Fix: build `Prospect` from `sf_contacts` (id = Contact id, `nooks_id=None`, phone, opt-out, title, email), make the file optional, drop it from `SAFETY_PAGED`, resolve Nooks ids lazily in Phase 3 and re-check DNC by prospect id then.
- **F2** `hasMore`/`page_after` throughout runbook/schema/docstrings (runbook:69,71,75; intake_schema:18). Repro: read both. Fix: `links.next == null`; add `--derive-last-page` to merge_pages.

**High**
- **F3** Task `note` tag is not read, so "last-sheet accounts from tagged tasks" does not exist (`task_from_nooks`, `pipeline.py` step 9). Needed for recovery after a state reset. Fix: parse `[apac-callsheet <date> #<rank>]` in intake, map the task prospect to an account via the Contact join, union with `state.last_sheet_ids`.
- **F6** Runbook fetches Open Position/Hiring Plans/Management Move for 30 days, no `employeeRangeMax`, wrong windows, no per-type counts (probe e1-e3). Config `scoop_types_keep` still lists them.
- **F7** DNC limits not stated in the log (Outreach calls have no Do Not Call; owner-filtered history only). Fix: add a fixed header line.

**Medium**
- **F5** Calls by another owner only warn; live passes (`test_calls_by_other_owner_only_warning`). Decide: block live, or drop them from DNC/cooldown by `owner.id`.
- **F8** `Sales_Opt_Out_Email__c` / `Pendo_Power_User_Sales_Opt_Out__c` neither fetched nor flagged; `FieldDefinition` fallback missing; false "HQ not usable in WHERE" claim (runbook:26).
- **F9** Runbook gives no exact parameter names, no Nooks/ZoomInfo spill-safe snippets, no call estimate, no way to get shortlisted Gmail addresses without printing them.
- **F10** Corrupt/reset `state.json` silently loses sheet history on a live run (until F3 exists). Consider a live blocker when `st.was_reset` and no tagged-task recovery.

**Low**
- **F4** UnicodeDecodeError escapes `load_raw`, `merge_pages` (xfail tests).
- **F11** `to` oddities counted only for DNC calls; no overall count of `external`/non-E.164.
- **F12** No sanity floor for a truncated first-ever call history.
- **F13** Gmail undated-hit rule is conservative (up to ~14 days after the mail) rather than exactly the 7-day query window.
- **F14** Replace the worker's broad edit to `test_integration_pipeline.py` with the narrower allowlist.

## Recommendations
1. Merge `origin/phase2/probe-notes` into the 2a branch and run a fix round for F1, F2, F3, F6, F7 first (they gate any real run); then F5, F8-F10.
2. Re-review only the changed pieces (prospect path, `reached_last_page` derivation, runbook), then do the first real dry run supervised.
3. Ask Alex: what the two sales opt-out checkboxes mean; whether another rep's Do Not Call should block his calls (needs the unfiltered listing); whether `Management Move` can stay out for good.
4. Keep the engine as is: the safety gates, determinism and performance need no change.

## What I could not verify
Live connector shapes and runtimes (offline by rule); `links.next`-based paging against a real spill; how a deferred-tool session handles the runbook end to end (read, not executed); the meaning of the sales opt-out fields; behaviour of `emailed_recently` against real Gmail dates (none exist in search results).
