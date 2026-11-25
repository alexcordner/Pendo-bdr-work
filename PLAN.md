# Build plan (coordinator = this session; workers and reviewers = child sessions)

Status key: [ ] todo, [~] in progress, [x] done. Every task ends with a reviewer pass before it is reported to Alex.

## Phase 0: Setup [x]
0a connectors checked | 0b folders, CLAUDE.md, config, ICP, skills drafts, README | 0c Alex review of config and ICP

## Phase 1: Core engine, offline with fixtures [x] (merged to main; 1,213 tests, all reviewed; see docs/phase1_report.md)
- 1a Discovery spike (read-only, live): which Salesforce field = "I am BDR on this account"; Nooks call disposition names (voicemail / no-answer); shape of listCalls, listProspects, listTasks; can Gmail search see sent mail; ZoomInfo scoop shape. Output: `docs/connector_notes.md` + anonymised fixtures.
- 1b Models, config loader, fixtures
- 1c state.py + tests (who was called, when, outcome, cooldowns)
- 1d accounts.py + tests (merge/dedupe sources, drop locations and non-companies, size cap, recently-touched filter)
- 1e signals.py + tests (scoop filter to Pendo buyer topics, trigger scoring, recency)
- 1f ranker.py + tests (plain-Python score, timezone call windows, daily cap, grouping)
- 1g Independent review + full test run
- CHECKPOINT: Alex confirms

## Phase 2: Dry run (no writes to Nooks or Gmail) [~] (2a, 2c, 2d, 2e script and the 2f integration are done; 2b and the voice tuning with Alex are open)
- 2a [x] Live read-only intake: Salesforce, Nooks, ZoomInfo search (free): `src/intake.py`, `src/pipeline.py`, `scripts/merge_pages.py` (+ `recent_call_ids.py`, `gmail_queue.py`, `safeout.py`), `docs/fetch_runbook.md`. First real dry run done; fixes merged (untied recent calls classes, Gmail coverage hashes, derive-last-page rules).
- 2b [ ] Signals pull (scoops, intent) with ZoomInfo credit counter in the log. Scoops are fetched and filtered (digital launch, M&A direction, debt / fund / rumour weights, vacated roles). Intent is deferred by decision (docs/decisions.md).
- 2c [x] callsheet.py assembles the sheet, research packets and the single checked task entry point (`prepare_task`).
- 2d [x] Opener / facts / gatekeeper / email skills + deterministic lint (`src/textcheck.py`, `config/voice.yaml`, `docs/voice_guide.md`). The voice is NOT calibrated to Alex (see 2e).
- 2e [ ] Voice tuning loop with Alex until the openers sound like him (needs his three liked openers, three disliked, two proud emails: `config/voice.yaml: alex_examples`). `scripts/dry_run_sheet.py` is in.
- 2f [x] Integration (this branch `phase2/integration`): seven owner branches merged, wiring done, end-to-end tests, docs/phase2_report.md, docs/review_2f.md.
- CHECKPOINT: Alex reviews the dry-run sheet and the report.

## Phase 3: Live writes
Preconditions (all enforced or logged by the pipeline; the full list is `PHASE3_PRECONDITIONS` in src/pipeline.py and the end of docs/phase2_report.md): owner id from `getMe` (never stored), refuse dry-run payloads, tasks only for `ranked` lines of a LIVE run, `record_sheet` only after writes succeed, re-run Do Not Call / opt-out BY NOOKS PROSPECT ID for every chosen prospect, every chosen prospect Gmail-covered, `callsheet.email_draft_allowed` before any draft, `textcheck` clean (no errors) before any draft or task field is written.
- 3a nooks_writer.py (idempotent, `--live` only, tagged so test tasks are deletable)
- 3b Write 10 tasks to Nooks, verify with listTasks, show Alex
- 3c drafts.py + voicemail detection (listCalls, last 24h, skip rules)
- 3d Create 3 Gmail drafts, show links
- 3e Safety audit: grep that no send/reply/forward/sendTaskEmail or Salesforce write is reachable
- CHECKPOINT

## Phase 4: Scheduling
- 4a scripts/run_nightly.sh + prompt file | 4b pick scheduler (Claude scheduled task vs cron + `claude -p`) by testing unattended connector access | 4c one test run and show log | 4d review
- CHECKPOINT

## Phase 5: Radar and scorecard
- 5a standalone Trigger Radar ("call this today") | 5b hiring / app launch / leadership sources | 5c weekly opener scorecard (needs call transcripts; Nooks Conversation Intelligence is off, so feasibility check first) | 5d review

## Working loop per task
Coordinator writes brief -> Worker builds on a branch, runs tests -> Reviewer (fresh context, sees only the diff + brief) reviews -> findings go back to Worker if needed -> Coordinator reports to Alex -> Alex approves -> merge.
Independent tasks (e.g. 1c, 1d, 1e) run in parallel. Anything that writes to Nooks or Gmail needs Alex's explicit go.


## Backlog carried into Phase 2 (from reviews)
- 2a: page Nooks listCalls to the LAST page (hasMore == false) before mark_dnc_rebuilt(reached_last_page=True); log any DNC call whose `to` is not E.164; SOQL for Assigned_SDR accounts without HQ_Country__c in WHERE (textarea): filter in Python.
- 2c: pass cooldown_until + call_date to accounts; call mark_do_not_call right before ranking; write tasks ONLY for `ranked`; never record_sheet in a dry run; spilled / needs_prospect / skipped never become tasks.
- 2b: pull people-move scoops over 90 days; eyeball real verdict reasons; 1e parked fixes (investor verbs, former/board, CIO funds, hiring negatives) were done in 1e-fixes; re-measure on real scoops.
- Alex decisions: persona list (Product Owner, Engineering Manager, CIO, Director of Engineering), call pacing (3-min slot, calls per window), whether an email opt-out should also block calls.
