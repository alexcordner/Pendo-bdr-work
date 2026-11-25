# Build plan (coordinator = this session; workers and reviewers = child sessions)

Status key: [ ] todo, [~] in progress, [x] done. Every task ends with a reviewer pass before it is reported to Alex.

## Phase 0: Setup [x]
0a connectors checked | 0b folders, CLAUDE.md, config, ICP, skills drafts, README | 0c Alex review of config and ICP

## Phase 1: Core engine, offline with fixtures
- 1a Discovery spike (read-only, live): which Salesforce field = "I am BDR on this account"; Nooks call disposition names (voicemail / no-answer); shape of listCalls, listProspects, listTasks; can Gmail search see sent mail; ZoomInfo scoop shape. Output: `docs/connector_notes.md` + anonymised fixtures.
- 1b Models, config loader, fixtures
- 1c state.py + tests (who was called, when, outcome, cooldowns)
- 1d accounts.py + tests (merge/dedupe sources, drop locations and non-companies, size cap, recently-touched filter)
- 1e signals.py + tests (scoop filter to Pendo buyer topics, trigger scoring, recency)
- 1f ranker.py + tests (plain-Python score, timezone call windows, daily cap, grouping)
- 1g Independent review + full test run
- CHECKPOINT: Alex confirms

## Phase 2: Dry run (no writes to Nooks or Gmail)
- 2a Live read-only intake: Salesforce, Nooks, ZoomInfo search (free)
- 2b Signals pull (scoops, intent) with ZoomInfo credit counter in the log
- 2c callsheet.py assembles a 10-account sheet and prints it
- 2d Opener / facts / gatekeeper generated via the opener skill
- 2e Voice tuning loop with Alex until the openers sound like him
- 2f Review (honesty check: no invented triggers or facts, sources cited)
- CHECKPOINT

## Phase 3: Live writes
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
