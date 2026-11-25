# Review 1g: integration review of Phase 1 (offline)

Reviewer: task 1g. Scope: `src/models.py, config.py, loaders.py, state.py, accounts.py, signals.py, ranker.py` at `main` 8d74800, run TOGETHER for the first time. Fully offline: no connector was called, no real data was read, nothing in `src/` was edited. Evidence is `tests/test_integration_pipeline.py` (93 tests: 89 pass, 4 are `xfail` repros of the bugs below) and `tests/fixtures/integration_world.json` (a hand-built synthetic world, every name and id invented).

## Verdict: PASS WITH FIXES

The safety-critical paths held in every scenario I could build: a Do Not Call contact never reaches a ranked prospect (by call history, by duplicate email, by shared phone, by an unlinked call), nothing vanishes (every account has exactly one outcome and a reason), 15/18-char ids give byte-identical output, output is byte-identical under 20 shuffles and four `PYTHONHASHSEED`s, no module can write to Nooks, Gmail or Salesforce, and 3,000 accounts run in about 2 s.

Two bugs must be fixed before Phase 2 builds on this (H1, H2). Neither can cause a send or a CRM write; H1 can make the written sheet differ from the recorded sheet, H2 can show an unverified signal as the trigger once intent is wired.

## Bugs found

Severity: **H** = fix before Phase 2/3, **M** = fix or consciously accept before the checkpoint, **L** = note / cheap fix.

| # | Sev | Where | One line |
|---|---|---|---|
| H1 | H | `state.py:720` `last_sheet_ids` (and `record_sheet:707`) | A second run on the SAME night treats the first run's sheet as "last night's": every account it ranked becomes `on last sheet`, so the sheet is replaced wholesale. |
| H2 | H (latent until 2b) | `ranker.py:240` `_best_signal`, `:276`, `:289` | Ranker ignores `signals.best_signal`'s rule "intent is never the best signal while a real dated signal exists". |
| M1 | M | `accounts.py:455-461`, `state.py:653` `last_touched` | An email touch drops an account only if it has NO call history. |
| M2 | M | `accounts.build_account_list(today=...)` vs `ranker` per-zone call date | A night run drops an account whose cooldown ends tomorrow, the day its task would be due. |
| M3 | L/M | `ranker.py` (design) | `Account.confidence` / flags never change the score, so no "low ceiling" exists for ZoomInfo-only / Nooks-only / noise accounts. They are kept off the sheet only because they have no timezone. |
| L1 | L | `signals.age_days:278` vs `ranker._age_days:248` | Same scoop reads "4 days ago" in the signals log and "3 days ago" in the ranker reasons on a night run (calendar days vs elapsed 24h). |
| L2 | L | pipeline contract (`signals.scoops_to_signals` input) | Two SF accounts sharing one ZoomInfo id: a `{zoominfo_id: account}` dict keeps one, so one duplicate gets the trigger and the other says "No trigger". |
| L3 | L | `state.py:131` `mark_dnc_rebuilt` | Rejects 0 calls, so a seat or environment with genuinely empty call history can never go live. |
| L4 | L | `state.py` `State.report` | `report` is not persisted: a harness that saves and reloads state loses the `record_calls` counts. The log must capture them before `save_state`. |
| L5 | L | `state.py:527` `record_calls` | `copy.deepcopy(state)` per call to `record_calls`: 20k calls cost 4.8 s at 100 per page, 1.3 s at 500, 0.4 s at 5,000. Quadratic in pages. |
| L6 | L (latent) | `state.py:707-717` `record_sheet`, all state keys | Everything is keyed by SF id and `None` ids are dropped silently. A ZoomInfo-only / Nooks-only account, once callable, could never be hard-skipped or cooled down. |
| L7 | L | `ranker.py:573` `summarise` | The run log counts skipped reasons but names no skipped account, lists only 10 ranked and 5 needs-prospect. CLAUDE.md rule 5 needs names. |
| L8 | info | `ranker.py` `cooldown_penalty` | Effectively dead code on the real path: a 168-hour sweep (3-hour steps) never produced a penalised account (no double counting). |

### Minimal repros (all in the test file)

**H1** `test_d_same_night_rerun_documents_the_hole` / `test_d_same_night_rerun_is_stable` (xfail).
```python
a = run(world, state_dir)      # records sheet for 2026-11-25
b = run(world, state_dir)      # same `now`
# b.result.skipped contains all 15 of a.result.ranked, reason "on last sheet"; b ranks a different set
```
Impact: dry run then live the same evening (if the dry run recorded the sheet), a crash and retry, or an accidental double run: the tasks in Nooks no longer match `state.sheets`. Fix: `last_sheet_ids(state, before=run_date)` ignoring sheets with `run_date >= run_date`; and 2c must not call `record_sheet` in a dry run (`test_d_dry_run_without_record_sheet_is_repeatable` shows the repeatable path).

**H2** `test_bug_intent_never_outranks_a_real_dated_signal` (xfail).
```python
fund   = Signal(id, FUNDING, 0.1585, "zoominfo_scoop:F", now-45d, ...)
intent = Signal(id, INTENT,  0.3,    "zoominfo_intent:Z:digital experience", now, ...)
signals.best_signal([fund, intent]).kind   # FUNDING   (documented rule)
rank_accounts(...).ranked[0].reasons[0]    # 'Intent signal 0 days ago'; trigger_strength 16.0; signals[0] is INTENT
```
Also the ranker adds `+0.10` per extra signal while `signals.combined_strength` damps by 0.5: two formulas, only the ranker's is used. Fix: have the ranker call `signals.best_signal` / `combined_strength`, and order `RankedAccount.signals` best first. A sheet builder that quotes `ranked.signals[0].evidence` would otherwise quote an interest signal as an event.

**M1** `test_bug_email_touch_treated_the_same_with_and_without_call_history` (xfail). Account A: emailed yesterday, no calls: `recently_touched`. Account B: emailed yesterday, no-answer call 10 days ago: kept (`_drop_reason` returns at `until is not None` before it looks at `touched`). `docs/state_notes.md` says emails do not start a call cooldown, yet `last_touched` includes emails. Recommend 2c passes ONLY `cooldown_until` (not `last_touched`) and lets `emailed_recently` handle email. Then both behave the same (kept).

**M2** `test_c_night_run_cooldown_is_one_day_conservative`. Run Wed 23:30 AEDT, `today` = run date Wed: an account whose cooldown ends Thu is dropped, though tasks are for Thu. With `today=Thu` (the first call date) it is ranked. Recommend 2c passes the first call date (the Sydney date of the first usable window) as `today`, or that accounts stops applying cooldown and leaves it to the ranker, which has the per-zone call date.

**M3** `test_h_confidence_does_not_affect_score_documented`: identical score with and without `confidence="low"` and flags. `test_h_zoominfo_only_and_nooks_only_are_low_confidence_and_cannot_be_called`: both end `skipped: no timezone`, correct. The brief expected "a low ceiling"; none exists. Matters the moment enrichment gives these rows a timezone, or for book accounts flagged `noise_location` ("Sydney CBD" ranks like any other, `test_h_book_noise_is_flagged_not_dropped...`). Recommend a cap (for example score <= 50, or a penalty) in 1f when `confidence == "low"`, decided with Alex.

## Scenario results (brief items a-n)

| Item | Result | Tests |
|---|---|---|
| (a) nothing vanishes | PASS. `ranked + needs_prospect + spilled + skipped + dropped == accounts after merge + malformed rows`, each object in exactly one bucket, every row has a reason. Holds in the named world, a spill world and the 3,000 world. Merging reduces row counts: the identity is on merged accounts (+ malformed rows as `malformed_row` drops). | `test_a_*` |
| (b) DNC | PASS. Four paths: call history, duplicate email (different dots/case/id/phone), shared switchboard phone (the other person on the number is blocked too), unlinked DNC call matched by phone. DNC account with the best score (100) is ranked using its other callable contact; an account whose only contacts are blocked is skipped. Ranker alone does NOT know state: skipping `mark_do_not_call` lets the DNC person through (pinned as a contract trap). Losing or corrupting the state file gates live runs until the history is replayed. | `test_b_*` |
| (c) cooldown | PASS with M1, M2, L8. Touched 2 days ago: dropped by accounts only, never also in the ranker. No double counting at any run time in a week. | `test_c_*` |
| (d) sheet history | PASS with H1. Last-sheet accounts are hard-skipped next night; spilled and needs_prospect are NOT recorded and are picked up again on their own; history keeps 3 nights. | `test_d_*` |
| (e) 15 vs 18 char ids | PASS. Six spelling combinations across SF rows, contacts, Nooks links, touched, blocked, signals, cooldown, last-sheet, state file: byte-identical summaries. Stale docstring noted below. | `test_e_*` |
| (f) time zones | PASS (brief corrected, see below). Perth/Sydney/Melbourne/Adelaide/Brisbane/Darwin/Auckland offsets and windows right in November; DST week Oct 2026 and Apr 2027 correct on Monday (`+11`/`+10`, NZ `+13`/`+12`), window starts 08:45 local; weekend and Friday-evening runs go to Monday; state days are account-local (same instant is Wed in Perth, Thu in Sydney, cooldowns differ by a day). | `test_f_*` |
| (g) no signal | PASS. Ranked on fit only (score 40 = tier-1 fit 25 + named prospect 15), `reasons[0] == "No trigger: ranked on fit only"`, empty `signals`, no trigger words leak from a dropped scoop; an explicit `SignalKind.NONE` is ignored; every signal on every account traces to a KEPT scoop. | `test_g_*` |
| (h) low confidence / flags | PASS with M3. ZoomInfo-only and Nooks-only are low confidence and `skipped: no timezone`. Non-ANZ billing/ANZ HQ is kept, flagged, -3, and its US billing state is ignored for the timezone. Book noise is flagged not dropped; ZoomInfo-only noise is dropped. Unknown size gets 7.5 pts; junk country is kept and skipped with a reason. | `test_h_*` |
| (i) determinism | PASS. 20 shuffles of every input list and the call paging (named world), 5 on a 400-account world; `PYTHONHASHSEED` 0, 1, 424242, random: identical SHA-256. | `test_i_*` |
| (j) scale | PASS. 3,000 SF accounts + 90 ZoomInfo-only + 4,174 prospects + 620 calls + 1,534 scoops: **1.97 s** wall (state 1.3 s of it, an artefact of paging calls 7 at a time), whole-process max RSS **55 MB**, Python-heap peak during the run **11 MB**. Cap holds (40 ranked, best 40 by score, 1,076 `below daily cap`). At Wed 16:10 AEDT the same world spills (Sydney has 6 slots left); spilled accounts use no cap slot and are not in sheet history. | `test_j_*`, `python -m tests.test_integration_pipeline --scale` |
| (k) logs | PASS. Plain ASCII, no repr noise, counts in words, no raw email or phone. One full example is in `docs/phase1_report.md`. L7 for missing names. | `test_k_*` |
| (l) hard rules | PASS. See below. | `test_l_*` |
| (m) contracts | 8 doc/code inconsistencies, listed below. | `test_m_*` |
| (n) tracked leftovers | All five still present, see below. | `test_n_*` |

**Brief correction for (f).** The brief expected the Thu 16:28 Sydney run to put everything on Friday. By the documented design (ranker_notes N1-N3) each zone has its own first usable day: Sydney, Melbourne and Auckland go to Friday, but Perth (13:28), Brisbane, Adelaide and Darwin still have the end of Thursday's afternoon window and stay on Thursday. Nothing is labelled spilled. A run at Thu 22:00 Sydney puts every zone on Friday (`test_f_later_thursday_everything_is_friday`). I assert the documented behaviour; if Alex wants "one call date for the whole sheet", that is a 1f change.

### (l) hard-rule scan of `src/`

Searched all seven modules by regex and by AST (`test_l_*`): no occurrence of `send_message`, `sendTaskEmail`, `createEmail`, `createTask`/`updateTask`/`deleteTask`, any Salesforce `create/update/delete*` tool name, `createAccountNote`, `createProspectNote`, `createCrmProspects`, `syncProspects`, `updateProspect`, `create_draft`, `soqlQuery`, `.send(`, `.reply(`, `forward(`. Imports are stdlib plus `yaml`; no `requests`, `httpx`, `urllib`, `http`, `socket`, `smtplib`, `ssl`, `subprocess`, `mcp`, `anthropic`. The only file writes are in `state.py` (temp file + rename inside `state_dir`). No hard-coded real ids, phones, emails, the Nooks owner id or `pendo.io` in `src/` (the owner id lives only in `config/config.yaml` as CLAUDE.md allows). The only `log.*` calls are in `state.py`; they log paths, reasons, counts, SF ids and Nooks ids, never an email or phone. `state.json` stores emails only as sha256 hashes and phone numbers only in `dnc_phones` (that is the one place a number is held at rest, by design, to block a shared number). A fresh grep in this review found no leakage of any world email or phone into the run log.

### (m) doc / code inconsistencies (fix the doc unless noted)

1. `accounts.build_account_list` docstring: "Salesforce ids are compared as given (15 vs 18 char forms are NOT normalised)". The code normalises (`test_m_accounts_docstring_stale_about_id_forms`). Docstring is wrong.
2. `state_notes.md` Known gaps: "Plus-addressing is not matched (F10)". `loaders.normalise_email_for_match` now strips `+tag` (`test_m_state_notes_stale_about_plus_addressing`).
3. `interfaces.md` decision 10: persona matching is a "case-insensitive substring". `ranker.py` matches whole tokens with aliases (ranker_notes is right).
4. `interfaces.md` Account table omits `flags` and `confidence`; `RankResult` (needs_prospect, spilled, needs_prospect_codes, by_window, prospects) is not documented there at all.
5. `interfaces.md` line 23 and the comment on `RankedAccount.local_call_time` (`models.py:204`): "push later inside the window when timezone_guessed". The ranker dropped that rule (`test_m_guessed_timezone_no_longer_calls_later...`).
6. `ranker_notes.md`: "interfaces.md decision 3 and the Account.size_unknown docstring currently say no icp_fit points: out of date". Both already say 7.5; the ranker note is the stale one.
7. `signals_notes.md`: "config.signals.max_age_days ... caps nothing today". `ranker.py:289` still uses it as the recency horizon (45 days): an exec move 60 days old (allowed to 90 by signals) scores trigger points but zero recency points (`test_n_1e_max_age_days_still_drives_ranker_recency`).
8. Two combination formulas for several signals (H2): `ranker.py` docstring and `score_account` (+0.10 each) versus `signals.combined_strength` and `signals_notes.md` (damped 0.5).
9. `state_notes.md` "emails do not start a call cooldown" versus `last_touched` including emails and `accounts_notes.md` telling callers to pass `touched` (M1).

### (n) leftovers the coordinator tracks

| Item | Still present? | Evidence | Where to fix |
|---|---|---|---|
| 1c M1: `mark_dnc_rebuilt` cannot detect a partial history | YES. One call from one page is accepted and `require_healthy(live=True)` then passes. | `test_n_1c_M1_*` | In the Phase 2a Nooks adapter, not in `state.py`: it alone knows whether `listCalls` paging reached the end and the API's total. Make the adapter return `(calls, complete: bool, total: int)` and call `mark_dnc_rebuilt` only when `complete and len(calls) == total`. Optionally have `mark_dnc_rebuilt` take `expected_total` and refuse a smaller count. |
| 1c L1: `save_state` over a populated file wipes DNC silently | YES. No exception, no backup. The marker gate still blocks live runs afterwards, which is the only protection. | `test_n_1c_L1_*` | `state.save_state`: if the file on disk has DNC entries and the new State has fewer, refuse (or copy the old file to `state.json.bak-N` first). One function, 10 lines. |
| 1c L2: malformed local date falls back to UTC date | YES. Call Tue 23:00 UTC (Wed 10:00 Sydney) with a corrupt stored date: cooldown ends Fri 27 instead of Sat 28 (one day early); a warning is logged. | `test_n_1c_L2_*` | `state._local_or_utc`: when the instant exists use the account timezone (it is available at the one caller via accounts), else keep the warning. Low value; fine to accept (the file is machine-written). |
| 1e `config.signals.max_age_days` now only a fallback | NOT TRUE. Ranker recency still reads it (`ranker.py:289`). | `test_n_1e_*` | Either make the ranker use `signals.max_age_for(kind, config)` of the best signal (preferred: one source of truth) or rename the key to `recency_horizon_days` in config and docs. Do it together with H2. |
| 1f Product Owner / Engineering Manager unmatched | YES, also Chief Information Officer and Director of Engineering. Chief Product & Technology Officer matches via the alias group. | `test_n_1f_*` | `config/config.yaml` `personas.secondary` (Alex's call, no code change): add "Product Owner", "Engineering Manager", "CIO" if wanted. Titles are matched as whole-token phrases, so each new entry is safe. |

## What I could NOT verify

- Anything against real connector shapes: Salesforce, Nooks, ZoomInfo and Gmail rows are synthetic copies of the shapes in `docs/connector_notes.md`. Items that note called UNVERIFIED stay unverified (Gmail sent-date key, intent row shape, real ZoomInfo topic names).
- Whether Alex really wants an email-opted-out contact blocked for CALLS (decision 2 says yes; `ranker.is_blocked` does it; in my 15%-opt-out synthetic world 175 of 3,000 accounts are skipped for it). Confirm at the checkpoint, the effect on a real book could be large (the notes say about a quarter of contacts are opted out).
- Real call volume per window (3-minute slot is a default), public holidays (not modelled anywhere), and behaviour at the real ~12k-account scale. The 3,000 run is linear-looking; I did not test 12k.
- Whether Nooks marks Do Not Call at the prospect level for dispositions logged by OTHER reps; `listCalls` is filtered to Alex's owner id, so DNC set by a teammate would be invisible (Phase 2 risk 3).
- `-W error` is clean for the new tests; I did not test Python 3.11 (this box runs 3.13), though nothing used 3.12+ syntax.
