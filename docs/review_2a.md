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

---

# Round 2: narrow re-review of `e8a1b4a` (fast-forward merged into this branch)

Offline, read-only, synthetic data only. Extra scratch tests: `tests/test_review_2a_round2.py` (22 passing probes; some only print evidence).

## Verdict: PASS WITH FIXES (no safety defect; two functional gaps must be fixed or consciously accepted before Phase 3)

`pytest` 1559 passed (plain and `-W error`); 236 passed under `PYTHONHASHSEED` 0 and 7; with my two scratch files 1559 + 22 more, no xfail left. Perf with the new code (prospects built from Contacts, `nooks_prospects` empty): 12k accounts / 25k contacts / 2k calls 3.2 s, 114 MB RSS; 19.7k / 54k 5.4 s, 176 MB (down from 210 MB).

## Round-1 findings F1-F7

| # | Finding | Status | Evidence |
|---|---|---|---|
| F1 | Contacts as prospects; `nooks_prospects` optional, not a live blocker | FIXED | `intake.py:39` `(False, True)`; `SAFETY_PAGED` (`intake.py:48`) no longer lists it; `build_prospects` (`intake.py:532`) builds from `sf_contacts`; file absent and `rows: []` both pass live (`test_prospects_optional`) |
| F2 | `hasMore` replaced by `links.next`; derive flag | FIXED, with gaps below | runbook §4 ("There is NO `hasMore` field"), `merge_pages.derive_last_page` (`merge_pages.py:185`) |
| F3 | tagged-task last-sheet source | FIXED | `intake.py:636-690` (`SHEET_TAG`, `last_sheet_from_tasks`), union in `pipeline.py` step 9; same-day/future tags ignored, untagged ignored; unresolved tagged task blocks live. Minor: multi-tag note uses the first tag, not the newest (see N6) |
| F4 | UTF-16 / BOM / garbage | FIXED | `intake.py:161` and `merge_pages.py:57` `decode_text`: BOM and BOM-less UTF-16, UTF-8-sig, garbage → loud error, never `UnicodeDecodeError` |
| F5 | other-owner calls | FIXED | `intake.py:392` blocks live (`test_gate_other_owner_blocks`) |
| F6 | scoop types/windows | FIXED (doc) | runbook §6: buyer types only, `employeeRangeMax=2000`, 90 d people / 45 d others, per-type counts in `run_meta.scoop_counts_by_type`, Open Position skipped. `config.yaml:87` still lists Open Position/Management Move in `scoop_types_keep`: harmless (a keep list for what may arrive), mention only |
| F7 | DNC limits in the log | FIXED | `pipeline.py:49` `DNC_NOTE` is written into every log header |

## Live-gate re-run (all as expected)
Blocked live: partial calls; stale (+25 h); future `fetched_at`; missing Gmail; credits unknown or 2; other-owner calls; owner mismatch (stops); empty history; unknown disposition; first run with 3 calls (floor `MIN_FIRST_RUN_CALLS = 5`, `pipeline.py:302`); reset/corrupt state without tagged tasks; missing tasks file. Passes live: full data; `nooks_prospects` deleted or empty. Floor bypass attempt (a valid `state.json` with emptied `seen_calls`, then 3 calls): blocked anyway (`mark_dnc_rebuilt` shrink check). Dry run never records a sheet; `record_sheet=True` without live raises. PASS.

## Probe table, row by row (docs vs `connector_notes_v2.md` and fixtures)

| Row | Status | Note |
|---|---|---|
| a1 page_size 100 | FIXED | runbook §4, §7b |
| a2 `links.next == null` | FIXED | runbook §4; derive rule on the FINAL page only (see N1) |
| a3 owner filter always | FIXED | `filter_owner_id` named (runbook §4, §7b); other-owner calls block live |
| a4 full history each run | FIXED | "about 17 pages... refetch in full every run" |
| a5 Outreach DNC limit | FIXED | `DNC_NOTE`, runbook §4 |
| a6 `to` oddities counted | FIXED | `pipeline.py:_to_stats`, log line "calls with a 'to' that is not E.164" |
| b1 one-off tasks | FIXED | runbook §5: `filter_action=call`, `filter_sequenceState_state=null`, no status filter. NOTE the probe verified `filter_status=ready,pending` only; "no status filter returns completed tasks too" is UNVERIFIED |
| b2 note tag | FIXED | see F3 |
| c1 Contacts as prospects | FIXED | F1 |
| c2 prospects not a precondition | FIXED | F1 |
| c3 lazy Nooks ids, DNC re-check in Phase 3 | FIXED (doc) | `DNC_NOTE`, intake_schema:97, runbook §5. But see N2: the dry run cannot see DNC on unresolved ids |
| d1 keyset 2000, ~10/~27 pages | FIXED | runbook §2, §3 |
| d2 `HQ_Country__c` in WHERE; Python authority | FIXED | runbook §2 (AND-ed with BillingCountry, fallback to Python-only if short) |
| d3 no `DoNotCall`; `Sales_Opt_Out_Email__c` | FIXED | fetched and COUNTED, not used to block (`intake.py` stat `contacts_sales_opt_out_email_unverified`, logged). Needs Alex's decision |
| d4 `FieldDefinition` fallback | FIXED | runbook §2 |
| e1 types, Open Position skipped | FIXED | runbook §6 |
| e2 employeeRangeMax, windows, page size | FIXED | runbook §6, §7b. Date-filter parameter name still UNVERIFIED (honestly marked) |
| e3 counts, credits, matched | FIXED | `pipeline.py` log: "scoops matched to your book: X of Y", per-type counts, credits |
| e4 intent out | FIXED | runbook §6 |
| f1 Gmail query per shortlisted contact | FIXED | runbook §7, §7e |
| f2 query-window rule | FIXED, conservative | anchored on the search day (`pipeline.py` `email_today`, window must be >= cooldown else live blocked) |
| g unattended-run notes | FIXED | runbook §7c-7f, §9 |

**Cursor parameter name.** The probe fixture's `hint` reads: "Call `listCalls` again with `page_after` set to "<cursor>" and the same filters", and `links.next` carries `page[after]=<cursor>` (URL-encoded). So the runbook should say flatly: the tool parameter is **`page_after`**; its value is the quoted cursor from `hint` (preferred) or the URL-DECODED `page[after]` value from `links.next`; never pass the percent-encoded form. The runbook currently says "read the tool schema for the exact parameter name"; acceptable but should be firmed up (N5). `filter_time_gte` is correct and unused (full history).

## New code review

**`merge_pages --derive-last-page`** (attacks run directly on `derive_last_page`):

| Input | Result |
|---|---|
| Nooks 3 pages, last `next=null` | True (correct) |
| Nooks last page `next=""`, final empty page | True (correct) |
| Nooks reordered so a page with a cursor is last | False (safe) |
| **Nooks middle page missing** (a, c) | **True (wrong, undetectable from cursors alone)** |
| **Nooks `next=null` page in the middle, more pages after** | **True (wrong, cheap to detect)** |
| **Nooks same terminal page twice** | **True (wrong, cheap to detect)** |
| **Nooks only the last page of the history in the manifest** | **True** |
| Salesforce 2000 then 1999 | True (correct) |
| Salesforce last page exactly 2000 rows with `done` true | False (safe; the session must fetch the next, empty, page → True) |
| Salesforce 2000 + empty page | True (correct) |
| **Salesforce middle page short (1000, harness cap), then 500** | **True (wrong)** |
| **Salesforce duplicate/reordered ids, or a single short page** | **True** |
| ZoomInfo 1..3 complete | True (correct) |
| ZoomInfo missing / duplicated / reordered page, rows < totalResults, truncated second type | False (safe) |
| ZoomInfo `totalResults` 250 → 400 on the last page | True (only the first page's total is used) |
| ZoomInfo `page.total` changes mid-sequence (3,3,2) | True (wrong, edge) |
| ZoomInfo empty last page with total 0 | True (correct) |

N1 (**Medium**): the Nooks and Salesforce rules only inspect the FINAL page, so a wrong or partial manifest (the typical session mistake) yields `reached_last_page=true` and a DNC rebuild on a partial history. Cheap fixes in `derive_last_page`: Nooks: every non-final page must have a non-null `links.next` and row ids must be strictly ascending across the whole manifest; Salesforce: every non-final page must hold exactly `sf_limit` rows and Ids must be strictly ascending across pages (kills the short-middle-page and duplicate cases); ZoomInfo: `meta.page.total` and `totalResults` constant within a sequence. A missing middle page stays undetectable from the pages; add `--expect-pages N` (the session counts its calls) and fail on a mismatch, and have the runbook tell the session to write the count.

**State keys (`sfc:<contact id>`).** `pipeline.py` `_surrogate` / `_restore`: the key never appears in the log, in `PipelineResult.prospects`, in `rank_result.prospects`, or in `state.json` (0 occurrences in a world run). PASS. **Shared phone, two Contacts** with a Do Not Call call by phone and no prospect id: BOTH are blocked (over-block, safe; `state._link_prospect` returns "ambiguous" for recording but the phone block applies). PASS.

**Phone inference of Nooks ids** (`intake.infer_prospect_ids_from_calls`): one id to two phones → unresolved; one phone on two Contacts → unresolved; two Nooks ids on one single-Contact phone → the first (sorted) id wins, the second stays unresolved; a colleague's prospect id on a call to a number only one Contact lists would attach to that Contact (wrong-person attach). Residual risk is low (the dialled number normally belongs to the person dialled) and the direction for DNC is over-blocking; but it is not exact: UNVERIFIED on real data. **Low (N7).**

**Tagged tasks** (`parse_sheet_tag`): impossible date (`2026-02-30`) → ignored; 5 MB note with a tag at the end → parsed without trouble; tag in the middle of a note → accepted; acct id of 16 chars is accepted (`[A-Za-z0-9]{15,18}`), 15/18 are the valid lengths (cosmetic); future/same-day ignored; a forged past tag can only suppress an account for one night (harmless). **N6 (Low):** with several tags in one note, `search` takes the FIRST (oldest) tag; use `finditer` and take the newest.

**Gmail window anchoring:** anchored on `gmail_sent.json` `meta.fetched_at` (Sydney date), live blocked when `window_days` < cooldown. Correct. F13 (block up to ~7 days past the window): **acceptable** (conservative; the alternative needs message bodies).

**UTF-16/BOM:** PASS in both `intake` and `merge_pages` (BOM UTF-16, BOM-less UTF-16-LE, UTF-8-sig, garbage).

## New findings

- **N2 (High): account-level cooldown memory is mostly lost when call prospect ids are not resolved.** `state.record_calls` records a call whose Nooks `prospect.id` is unknown on the PROSPECT only (`state.py:739` warning "no Salesforce account... cannot be cooled down"). With `nooks_prospects` no longer fetched, only phone-unique inference resolves ids. Repro `test_cooldown_lost_when_call_prospect_ids_unresolved`: 7 of 7 calls recorded with no account; in the 12k perf world 1,112 of 2,000 calls (56%) had no account, so only ~590 accounts carried call state. Live passes regardless. Effect: accounts dialled within the 3-day (no answer) or 14-day (connect) cooldown can be ranked again; the tagged-task source only covers the last sheet. Real-world rate is UNVERIFIED (depends on how many Contacts have a unique phone), but the log only prints `call_prospect_ids_left_unresolved`. Fix: (1) add a runbook step that fetches `listProspects filter_id=<uuid,...>` (10 ids per call is not needed; comma lists of uuids are supported) for every distinct prospect id on calls inside the longest cooldown window (about 14 days, probably under 100 ids), written to `nooks_prospects.json` (their `crmId` joins to Contacts); (2) make the pipeline report "calls in the last 14 days not tied to an account: X of Y" and block live above a threshold.
- **N3 (High): Gmail coverage is not checked.** Live requires `gmail_sent.json` but not that every ranked prospect was searched. The ranked set changes once Gmail hits block people (the next account backfills), and those backfills were never searched. Fix: record the searched addresses (as normalised hashes) in the file's `meta`/`run_meta`, and make live require that every chosen prospect with an email is covered; re-run the dry run until the set is stable (fixpoint).
- **N4 (Medium): a Do Not Call on a Nooks prospect id that matches no Contact is invisible to the dry run.** Repro `test_dnc_call_external_to_unresolved_prospect_id_is_invisible`: DNC call, `to="external"`, id unresolved → the id is stored in `state.dnc_prospect_ids` but no Contact is blocked and the log has no line for it. The design says Phase 3 re-checks by Nooks id, which is the right mitigation; add a log line "DNC prospect ids not tied to any Contact: N" and make that re-check a hard Phase 3 precondition (list it in PLAN.md).
- **N5 (Low):** firm up the cursor parameter (`page_after`) and the URL-decoding in the runbook; add `getObjectSchema` to the ban list instead of the allow list (runbook §0 still lists it as allowed while §2 forbids it).
- **N8 (Low):** runbook §0 bans by name; use an allow-list (`soqlQuery`, `getUserInfo`, `listCalls`, `listCallDispositions`, `listTasks`, `listProspects`, `search_scoops`, `search_threads`, `getMe`) so an unlisted ZoomInfo tool (`browse_engagements`, `conversation_intelligence`, `open_zoominfo`, `get_gtm_context`) is not called by accident. §7e says "never print the list" but then needs the session to read each address: the addresses will enter the session context (not git); say so explicitly.

## Runbook as the executing session
Read-only rules are at the top (§0); credit-spending tools are named; every spill snippet prints counts or a single cursor only (none prints a row; the Nooks cursor is an opaque base64 of an id). Steps are executable with exact parameters (§7b). The 65-110 call estimate is plausible (probe: SF 10 + 27, Nooks 17 + 1 + 1-3, scoops 6-10, Gmail <= 40). Gaps: N2 (no step for lazy prospect resolution of recent calls), N3 (no coverage record), N1 (`--expect-pages`), the date-filter parameter name for scoops is still unverified, and `filter_status` handling for completed tasks is unverified.

## Leftover judgement calls
- F13 undated email block up to ~7 days past the window: **acceptable.**
- `not_anz` drop list capped at 25 + count: **acceptable** for rule 5 (server-side ANZ filter now shrinks it to a few hundred accounts; the count is logged); optional full list in a side file.
- Decision 1 (no ZoomInfo company search source): **accept**. Decision 2 (Nooks→SF link in Python): **accept**, now mostly moot. Decision 3 (dry run saves `state.json`): **accept**, safe (live re-derives the marker each run from the current full file). Decision 4 (DNC re-marked rebuilt every run): **accept**. Decision 5 (Gmail optional for dry, required for live): **accept**, subject to N3.

## Final: PASS WITH FIXES
Blocking Phase 3 (not the dry run): **N2**, **N3**. Should fix with them: N1, N4. Nice to have: N5-N8.

---

# Round 3: narrow re-review of `phase2/2a-fixes` (`2e5d58a`, from `main` `e9d478f`)

Offline, read-only, synthetic. `phase2/2a-fixes` conflicts with this branch (add/add in `fetch_runbook.md`, `intake_schema.md`, two test files), so it was reviewed in a detached worktree of `2e5d58a` with my scratch tests copied in, NOT merged here. New scratch tests: `tests/test_review_2a_round3.py` (skips itself on this branch because it needs the new code); `tests/test_review_2a_round2.py` updated for case-insensitive DNC ids.

## Verdict: PASS WITH FIXES (no safety defect; one operational blocker for the live gate, three small fixes)

`pytest` in the worktree: 2084 passed (plain and `-W error` identical; the 1 earlier failure was my own test asserting a case-sensitive DNC id, since fixed). On this branch (no worker changes): all pass, round-3 file skipped.

## N1-N8

| # | Status | Evidence / attacks |
|---|---|---|
| N1 manifest checks | FIXED | `merge_pages.derive_last_page` + `_ascending` + `--expect-pages`. Attacks, all rejected with exit 2: Nooks `next=null` in the middle, same terminal page twice, duplicated page, reordered pages, overlapping boundary row; Salesforce short middle page, duplicated pages; ZoomInfo changing `totalResults` or `page.total`; `--expect-pages` mismatch for Nooks, SF and ZoomInfo. Correct results: exactly-2000 SF last page → False; 2000 + empty page → True; ZoomInfo gap / missing page → False; two complete types → True. **Residual:** a missing middle page with FULL neighbours is only caught by `--expect-pages` (the runbook says "always pass it": good, but it is optional in code; consider requiring it for `--derive-last-page`) |
| N2 untied recent calls | FIXED, but see **O1** | `intake.untied_recent_calls`, `MAX_UNTIED_RECENT_SHARE = 0.05` (blocks only when strictly above). Boundaries: a call exactly 14 d old counts, 14 d + 1 s does not; future-dated calls count as recent (harmless); zero recent calls → share 0 (passes); exactly 5/100 → passes, 6/101 → blocked; prospect id with no account → untied; phone on two prospects → untied. |
| N3 Gmail coverage | FIXED | `address_hash`, `gmail_uncovered`, `scripts/gmail_queue.py`; live refused while any ranked prospect with an email is uncovered (`pipeline.py`, `stop(... LiveRefused)`). |
| N4 DNC unmatched | FIXED | log line `DNC calls not matched to any contact: N` and `PHASE3_PRECONDITIONS` (see below) |
| N5 cursor wording | FIXED | runbook §4 and §7b: `page_after`, value = quoted cursor from `hint` or URL-decoded `page[after]`. Matches the probe fixture hint ("Call `listCalls` again with `page_after` set to ..."). The probe notes themselves never name the parameter; the hint text is the evidence |
| N6 newest tag wins | FIXED | `parse_sheet_tag` uses `finditer`, newest date |
| N7 phone inference | unchanged (accepted, Low) | |
| N8 allow-list | FIXED, one slip | runbook §0 allow-list. **Remove `search_companies` from it** (O3): §6 says "do not call `search_companies`" and the allow-list says it is permitted |

## Specific questions

**N3 hashing.** `sha256("<salt>:<dotless lowercase address>")[:32]` with the public default salt `apac-callsheet-v1` is NOT secrecy: anyone with a guessable address list (first.last@company) can confirm membership by hashing it (`test_hash_and_normalisation` shows a guess matches). It only serves coverage matching and keeps raw addresses out of `gmail_sent.json` meta (an `@` in a hash is a load error: tested) and out of the log (no address in a world run: tested). Treat the hash file as sensitive-as-the-addresses-are-guessable, and keep it in `output/`. Normalisation: case and dots are ignored, a `+tag` is kept (so `jane.doe+news@x` equals `janedoe+news@x` but not `janedoe@x`): fine, since the search uses the real address. Dots-ignored also merges `a.b@corp.com` with `ab@corp.com`: for corporate domains these can be two people; a false "covered" is possible but the chance is negligible. Backfills: coverage is recomputed on each run from the final ranked set, so a replaced prospect is flagged until searched (round loop in §7e works). Prospects without an email are counted separately and never block.

**`gmail_queue.py` hygiene.** Prints counts only; `queue` writes no state or log (verified: no state dir created). But: (a) the output file is mode **0644** (world-readable inside the container); (b) `--out` accepts ANY path: I wrote the address list into `docs/` (a tracked directory) without a complaint. **O2 (Medium):** refuse any `--out` outside `output/` (or require the path to be gitignored via `git check-ignore`) and create the file with mode 0600. The same applies to `recent_call_ids.py` (ids only, uuids, lower risk).

**N1 lexicographic ids (risk of false rejection).** The check compares ids as Python strings. Lower-case UUIDs sort identically to Postgres' byte order, and my tests pass for them; mixed case (`0B` vs `0a`) and Salesforce ids with mixed-case 15-character values (lower- vs upper-case ordering) are rejected. Whether real Nooks pages are lexicographically ascending and how Salesforce orders mixed-case Ids is UNVERIFIED (the probe says "ascending by call id" only). A false rejection fails safe (exit 2, nothing written, live impossible) but would stall every run. **Recommendation:** make duplicate detection order-agnostic (a set of ids across the manifest rejects duplicated or overlapping pages regardless of ordering) and replace strict ascending by cursor continuity: decode each non-final Nooks `links.next` cursor (base64 `{"id": ...}`) and require it to equal the last row id of that page; keep strict ascending only behind `--strict-order`. Keep `--expect-pages` as the real guard for gaps.

**Runbook §5 and §7e as the executing session.** Executable and unambiguous: exact parameters, the snippets and scripts print counts or one cursor only, the 25-id batches are small, and the loop (dry run → queue → search → hashes → merge → re-run) is clear. Addresses enter the session's context for the search (stated openly). The `--also` flag in the example command works on the first round too (a missing file is skipped), though the text says to omit it. Estimate 65-110 calls remains plausible; the tie-out step adds ≤ 4 calls (≤ 100 ids / 25).

**Phase 3 preconditions text (`pipeline.PHASE3_PRECONDITIONS`, log section and `--live` output).** Contains two items: the Nooks-id DNC/opt-out re-check and Gmail coverage. **Incomplete (O4, Low):** the owner id from `getMe` passed as `expected_owner_id` (documented in `docs/callsheet_notes.md:58`, enforced by `prepare_task`), refusal of dry-run payloads, tasks only for `ranked` (never `spilled` / `needs_prospect` / `skipped`), and `record_sheet` only after the writes succeed are not in the log text a Phase 3 session will actually read. Add them (one line each, pointing at `callsheet`).

## New findings

- **O1 (Medium-High, operational): the 5% gate can be unsatisfiable.** A recent call whose prospect id resolves to a Lead (`crmId` `00Q...`) or to a Contact outside Alex's book (or a null account) can never get a Salesforce account, yet `untied_recent_calls` counts it as untied. Repro `test_calls_to_resolved_lead_or_non_book_prospects_still_count_as_untied`: 3 calls on Lead-typed prospects, 3 of 5 recent calls untied, live blocked, and fetching the prospects (the runbook's remedy) does not change that. The probe says ~1/5 of Nooks prospects have a null account and many are Leads. Fails safe, but may stall every live run. Fix: after the resolve step, classify a call whose prospect resolved to a non-book / Lead record as "resolved, not in book" (cannot cool down a book account, so excluded from the threshold, still logged), and threshold only on ids that are still unresolved or resolved to a book Contact lacking an account.
- **O5 (Low):** calls with no prospect and no usable number are excluded from the share by design. That is reasonable (nothing can tie them) but 19% of calls look like this; log their share of recent calls and warn above, say, 25%, so a mass of manual dials is visible rather than silent. With 100% of calls `external` the gate passes (`test_all_external_does_not_block`).
- **O6 (Low):** a 5% tolerance is about 20+ calls out of ~450 in 14 days; the cooldown that matters most is the 3-day one. Consider also requiring 0 untied calls in the last `cooldown_days_no_answer` days (small numbers, so cheap to resolve).

## Leftover judgement calls
Unchanged from round 2: all five worker decisions, F13 and the `not_anz` cap remain acceptable. "Unlinkable" `external` calls not counted against the threshold: acceptable with O5.

## FINAL: PASS WITH FIXES
Safe to keep running the live DRY RUN and to start Phase 3 design. Before the first live write: O1 (else the gate may never open), O2 (address-file path/mode), O3 (`search_companies` in the allow-list), O4 (precondition text). Optional: order-agnostic duplicate check instead of strict ascending, `--expect-pages` mandatory, O5/O6.
