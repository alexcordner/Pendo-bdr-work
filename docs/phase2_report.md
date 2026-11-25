# Phase 2 report (task 2f integration)

Branch `phase2/integration` (off `origin/main` e9d478f). Everything here was built and tested OFFLINE on invented data: no connector was called, nothing was sent or written except git. Salesforce stays read-only; no Nooks tasks and no Gmail drafts exist anywhere.

## 1. What Phase 2 delivers so far
| Piece | Module / file | State |
|---|---|---|
| Raw intake, the pipeline CLI, merge/queue/safe-output helper scripts, fetch runbook | `src/intake.py`, `src/pipeline.py`, `scripts/{merge_pages,recent_call_ids,gmail_queue,safeout}.py`, `docs/fetch_runbook.md` | merged (2a + 2a-fixes) |
| Account list with evidence-based duplicate groups | `src/accounts.py` (`duplicate_groups`, new group notes, `contact_names_by_account`) | merged (fix-dupes), wired (pipeline passes contact names) |
| Trigger quality: digital-launch test, M&A target / acquirer / rumour / minority stake, debt and fund down-weights, C-suite promotions, vacated roles, same-deal pairs | `src/signals.py` (+ addendum cbabb48) | merged (fix-triggers) |
| Persona choice: junk / assistant titles never chosen, head-of-title preferred, `junk_title` code | `src/ranker.py` | merged (fix-persona) |
| Call sheet, research packets, personal-email flag, duplicate collapse, checked task entry point | `src/callsheet.py` | merged (fix-sheet) and rewired to the new duplicate notes and signal notes |
| Voice rules and lint for openers / facts / gatekeeper / emails | `src/textcheck.py`, `config/voice.yaml`, `skills/*`, `docs/voice_guide.md` | merged (2d); NOT calibrated to Alex (label line is mandatory) |
| Dry-run sheet script | `scripts/dry_run_sheet.py` | merged (2e), now writes only through safeout |
| End-to-end tests of all of the above | `tests/test_integration_phase2*.py`, `tests/phase2_world.py`, `tests/phase2_driver.py` | new |

Not done in Phase 2: ZoomInfo intent (deferred by decision), the voice calibration with Alex (needs his examples), and everything that writes (Phase 3).

## 2. Final test counts
`python -m pytest tests/ -q -W error` -> **3250 passed**, 0 failed, 0 skipped, **0 xfail** (main alone was 1990). New in 2f: 41 tests in `test_integration_phase2.py` (scenarios i to iv and textcheck on real packets) and 36 in `test_integration_phase2_gates.py` (gates, determinism, scale smoke, hard-rule scan). The reviewer scratch files with legitimate xfails were not committed.

## 3. Scale (12,000 accounts), `python -m tests.phase2_driver scale --n 12000`
Synthetic book: 12,000 accounts, 20,000 Salesforce contacts and Nooks prospects (14 title kinds incl. assistants and the 120-character multi-role title), 2,501 calls, 2,000 scoops (7 shapes incl. debt / fund / acquired / generic hiring). Names are two invented words plus a number, so name buckets are realistic (~30 accounts share a word).

| Step | Seconds |
|---|---|
| pipeline (`run_pipeline`: intake, state, accounts, signals, rank, log) | 10.1 |
| sheet (`build_sheet`, limit 10, render) | 0.4 |
| whole run incl. world build and raw write | 10.8 |
| peak RSS | 176 MB |

Result: 40 ranked (daily cap), 4,636 needs-prospect (885 of them `junk_title`), 10 on the sheet. **Worst case, reported as a bug (B1):** if thousands of account names share one distinctive word (my first, unrealistic generator named everything "Scale Company 00001 Pty Ltd"), `accounts._detect` compares every pair in that word's bucket: 1,500 accounts 12 s, 3,000 accounts 51 s, 12,000 accounts 3 min 20 s. The real book is fine today; a common-word bucket would make the dry run crawl.

## 4. End-to-end sample run (synthetic world, `now` = Wed 25 Nov 2026 14:00 AEDT)
The world is `tests/phase2_world.py` (21 accounts, 48 contacts, 29 scoops, 7 calls). Full log: `docs/phase2_sample_run.log` (151 lines, written by the pipeline itself); the 10-line sheet: `docs/phase2_sample_sheet.txt`. Key lines:

```
RUN LOG pipeline  now=2026-11-25T03:00:00+00:00  run_date=2026-11-25  mode=DRY RUN  limit=config cap 40
Nothing is sent or written to Nooks, Gmail or Salesforce by this run. Salesforce is read-only.
ZoomInfo credits used this run (rule 7): 0 (budget 0)
== ACCOUNTS ==
possible duplicate pairs: 1 (evidence: name+same headcount: 1, name+shared contacts: 1; top 10 listed with names)
    Catapult ~ Catapult Communications [name+same headcount, name+shared contacts]
== FLAG COUNTS ==
  junk_title (needs prospect: only contacts are assistants / junk titles): 1
  personal_email (chosen contact, call only, no email draft): 1
  possible_duplicate: 2 accounts in 1 groups (kept on the account list, collapsed on the sheet)
== RANKER ==
Ranked 20 accounts; skipped 0; needs prospect 1; spilled 0.
  needs prospect (NOT callable, no task) - junk_title: 1
   1. Catapult Communications score=93.78 ... | Executive move scoop 3 days ago; 8 more triggers; ... Flag: possible_duplicate - check before calling
  ...
  11. Foo Digital Pty Ltd score=71.2 ...     (M&A target of a 30% stake; same deal as Holder Capital)
  ...
== RESULT ==
  sheet history recorded: NO
  Nooks tasks / Gmail drafts written: NONE (Phase 3 owns writes)
```
Scores show the new weights at work: a digital app launch 80.5, an equity round 73.7, a debt facility 61.8, a property fund launch 60.1, a lab-consumables launch 67.7, a rumoured acquirer 61.8 vs a confirmed one 70.3, a CFO-to-CEO move dropped (no trigger, 40.0, ranked on fit).

## 5. Known limitations
1. **The target of an acquisition looks like a bare M&A scoop on the sheet.** "being acquired: leadership/platform change likely" lives on `ScoopVerdict.reason` only; `Signal` has no field for it, so `signal_notes()` is empty for the target (`tests/test_integration_phase2.py::test_c_target_direction_is_not_recoverable_from_signal_alone` pins this). The opener LLM only sees the scoop text.
2. **textcheck knows about notes only through literal phrases.** `callsheet.NOTE_BANNED_PHRASES` turns five notes (physical launch, property / fund launch, debt facility, reported talks, minority stake) into `must_not_phrases` ("new app", "funding round", "has acquired", ...). A paraphrase ("congrats on the acquisition" is covered, "huge deal for you" is not) passes. `email_ok` is not read by textcheck.
3. **Account duplicates** are connected components: one wrong pair edge merges two groups. The 6-account cap and the evidence rules limit it; the sheet only collapses groups the account builder flagged (never re-detects).
4. **The fetch is still session-driven**: ~63 connector calls and about 12 minutes of wall time per refresh (measured in 2e), plus the lazy Nooks prospect resolution and the Gmail search loop for the shortlisted contacts.
5. **Names**: the sheet shows contact first names only and, since 2f, so does the run log (`ranker` reasons); company names are in both by design.
6. **Voice**: `calibrated: false`; every opener block must start with the DRAFT STYLE label until Alex supplies examples.
7. A `Contact` with no `FirstName`/`LastName` in the SF export contributes no evidence to duplicate detection (the 1g test builder omits names; the real query selects them).

## 6. Phase 3 preconditions (all appear in every run log, `== PHASE 3 PRECONDITIONS ==`)
1. Owner id from Nooks `getMe` at run time, passed to `callsheet.prepare_task(expected_owner_id=...)`; never stored or guessed.
2. The writer refuses dry-run payloads (`callsheet.is_dry_run_payload`).
3. Tasks only for the `ranked` lines of a LIVE run (never spilled / needs_prospect / skipped / the collapsed duplicate).
4. `state.record_sheet` only after the writes succeeded, for the accounts actually written.
5. Re-run Do Not Call and opt-out BY NOOKS PROSPECT ID for every chosen prospect after `listProspects filter_crmId` resolves ids.
6. Every chosen prospect is covered by `gmail_sent.json` (hash coverage), re-run until the ranked set is stable.
7. Before any email draft: `callsheet.email_draft_allowed(line, resolved_prospect)` AND `textcheck.check_email_draft` without errors. Before any task field: `textcheck.check_opener_block` without errors.

## 7. Phase 3 will trip over (prioritised)
1. **Chosen prospects mostly have `nooks_id = None`** (Salesforce Contacts are the source). A task needs a Nooks prospect: resolving ~40 `crmId`s is one `listProspects filter_crmId` call each (or `filter_id` batches); a Contact that has no Nooks prospect must be created (`createCrmProspects`, a CRM-adjacent write: needs Alex's explicit go) or the line becomes needs_prospect. Budget this before 3a.
2. **The same human on two accounts** (Catapult's shared VP Product): the sheet collapses the account, but two UNFLAGGED accounts can still share one person; dedupe tasks by Nooks prospect id (and by email hash) before writing.
3. **Idempotency and the memory of the last sheet.** `state.json` is ephemeral; the previous sheet is recovered from tagged tasks (`[apac-callsheet date #n acct=...]`). A crash between "task 6 written" and `record_sheet` must be recoverable: write tasks in rank order, tag each, re-read `listTasks` before retrying, never create a task whose tag already exists.
4. **Stale gates between dry run and live**: Gmail coverage and recent-call resolution are per ranked set; a live run re-ranks (cooldowns end at midnight local), so the covered set can change between the dry run and the write. Run live in one go: pipeline live -> gaps -> fetch -> pipeline live again.
5. **Task field limits** (note length, owner, due time in the prospect's zone, `callsheet.priority_for`): untested against the real `createTask` schema; do one test task on a test prospect and delete only that (rule 2).
6. **Personal email**: the line is on the sheet for calling; Phase 3 must call `email_draft_allowed` (a Gmail draft to a personal address is the failure to avoid).
7. **Opener fields**: the LLM text must pass `textcheck` with NO errors, and the DRAFT STYLE label must be stripped or kept deliberately; decide before 3b whether uncalibrated openers go into Nooks at all.
8. **Account-name noise in `large_domain_group`/`large_name_group`**: real data flags many accounts that share a domain (franchise groups); they stay callable but show a flag on the line. Decide whether they should.
9. **Performance cliff** B1 (common-word name buckets) before the book grows past ~15k.
