# Phase 1 report (FINAL: core engine, offline)

Final reconcile by the 1g integration review, branch `phase1/1g-final` (origin/main 8d74800 plus the four owner fix branches 1c / 1d / 1e / 1f and the integration tests). Evidence: `docs/review_1g.md`, `tests/test_integration_pipeline.py`. Everything ran offline on synthetic data; no connector was called.

## What Phase 1 delivers

Seven modules, plain Python 3.11+, no network, no LLM, no randomness:

| Module | Job | In one line |
|---|---|---|
| `src/models.py` | Data classes | `Account`, `Prospect`, `CallRecord`, `Scoop`, `Signal`, `RankedAccount` ... |
| `src/config.py` | Config + time | `load_config`, tiers, personas, per-prospect time zones, call windows (DST via zoneinfo) |
| `src/loaders.py` | Raw rows to models | SF / Nooks / ZoomInfo / Gmail rows, phone and email normalisation, 15/18-char SF ids |
| `src/state.py` | Memory | calls, cooldowns, Do Not Call (by id, phone, hashed email), sent emails, last sheet; atomic `state.json` with a shrink guard and backup; live-run gate |
| `src/accounts.py` | Account list | merge the three sources, drop customers / out-of-size / blocked / cooling-down, flag (never drop) your own book's oddities |
| `src/signals.py` | Triggers | Pendo-buyer scoops only, role rules, recency decay, one best-signal and one combined-strength definition, `No trigger` when there is none |
| `src/ranker.py` | Score and schedule | score 0-100, persona pick, cap of 40, per-zone call times, spill, needs-prospect list, low-confidence ceiling (60), named run-log lists |

Pipeline as task 2c will wire it (implemented as `run_pipeline` in the integration test file): raw rows -> loaders -> `load_state` -> `verify_dispositions` -> `record_calls` (pages of 1,000+) -> `mark_dnc_rebuilt(..., reached_last_page=True)` -> `save_state` / `load_state` -> `require_healthy(live=True)` -> `record_sent_emails` -> `mark_do_not_call` on every prospect, immediately before ranking -> `cooldown_until` / `cooldown_until_by` -> `accounts.build_account_list(..., cooldown_until=..., call_date=...)` -> `filter_scoops` + `scoops_to_signals(group_by_zoominfo_id(...))` -> `rank_accounts(..., last_sheet_ids(state, run_date), ...)` -> `RankResult` -> `record_sheet` for `ranked` only (never in a dry run).

## Tests (final)

| File | Tests |
|---|---|
| `test_accounts.py` | 182 |
| `test_config.py` | 8 |
| `test_loaders.py` | 28 |
| `test_ranker.py` | 207 |
| `test_review_fixes.py` | 69 |
| `test_signals.py` | 339 |
| `test_state.py` | 222 |
| `test_timezones.py` | 39 |
| `test_integration_pipeline.py` (1g) | 102 |
| **Total** | **1,196 passed, 0 failed, 0 xfail, 0 skipped**, clean under `python -m pytest tests/ -q -W error` (about 45 s; the two scale tests are about 35 s of it) |

The integration tests cover brief items (a) to (n); the four 1g `xfail` repros (same-night rerun, intent as trigger, email touch, scoop age wording) are now plain regression tests (`test_fixed_*`, `test_d_same_night_rerun_is_stable`). Old-behaviour pins were flipped to the fixed behaviour (1c M1, L1, L2, L3, L4, L5, L6; 1d M1, M2; 1e L2 duplicates; 1f H2, M3, L1, L7). One test pins a KNOWN GAP (N2 below) and must be flipped when it is fixed.

## Performance (synthetic, same machine)

`python -m tests.test_integration_pipeline --scale N --call-page P`.

| Accounts | Rows in | Calls paged | Wall | Stages (s): intake / state / accounts / signals / rank | Max RSS (process) | Py heap peak |
|---|---|---|---|---|---|---|
| 3,000 | 3,000 SF + 90 ZoomInfo-only, 4,174 prospects, 620 calls, 1,534 scoops | 1,000 per page | **0.66 s** | 0.18 / 0.05 / 0.18 / 0.04 / 0.18 | 56 MB | 12 MB |
| 3,000 | same | 7 per page | 1.64 s | state 1.0 | 56 MB | 12 MB |
| 12,000 | 12,000 SF + 360 ZoomInfo-only, 16,752 prospects, 2,555 calls, 6,288 scoops | 1,000 per page | **3.3 s** | 0.87 / 0.36 / 0.85 / 0.16 / 0.91 | 131 MB | 47 MB |
| 12,000 | same | 7 per page | 20.6 s | state 17.9 | 131 MB | 47 MB |

Result at both sizes: 40 ranked (the cap), the rest in needs-prospect / skipped (below the cap) / dropped; the conservation identity holds (`ranked + needs_prospect + spilled + skipped + dropped` = accounts after merge plus malformed rows). Scaling is linear apart from the page-size effect (finding N1): `record_calls` re-normalises every prospect's phone on every call, so its cost is pages x prospects. 20,000 calls in pages of 100 take under 5 s (was 4.8 s before the 1c fix, now well below that). Page by 1,000 or more.

## Sample end-to-end run log

`python -m tests.test_integration_pipeline --sample-log --late`: the invented integration world, Wed 2026-11-25 16:12 AEDT (Sydney has only 5 call slots left, so one account spills and Auckland moves to Thursday), last night's sheet held one account, dry run so no sheet is recorded. Every name, id and number is invented. It shows ranked, needs-prospect, spilled, skipped, dropped and flagged accounts, each with its reason.

```
RUN LOG (offline harness, synthetic data) now=2026-11-25T05:12:00+00:00 run_date=2026-11-25 accounts_today=2026-11-25

== STATE ==
state: new=False reset=False dnc_rebuilt_at=2026-11-25 dnc_rebuilt_calls=7
record_calls: {"calls_seen": 7, "dnc_added": 3, "duplicate": 0, "new": 7, "no_account": 0, "unmatched": 0, "unmatched_call_ids": [], "unmatched_reasons": {}}
record_sent_emails: {"date_unknown": 0, "duplicate": 0, "new": 2, "no_account": 0, "skipped_no_key": 0}
prospects marked do-not-call from state: 5

== SIGNALS ==
  FAKE-SCOOP-00001 (Co 1): Kept: Executive Move, role 'Head of Product', 3 days ago
  FAKE-SCOOP-00002 (Co 2): Kept: Funding, 10 days ago
  FAKE-SCOOP-00003 (Co 3): Kept: Product Launch, 5 days ago
  FAKE-SCOOP-00004 (Co 5): Kept: Executive Move, role 'Head of Product', 1 day ago
  FAKE-SCOOP-00005 (Co 6): Kept: Executive Move, role 'Head of Product', 2 days ago
  FAKE-SCOOP-00006 (Co 7): Kept: Funding, 4 days ago
  FAKE-SCOOP-00007 (Co 8): Kept: Funding, 2 days ago
  FAKE-SCOOP-00008 (Co 9): Kept: Funding, 6 days ago
  FAKE-SCOOP-00009 (Co 10): Kept: Executive Move, role 'Head of Product', 1 day ago
  FAKE-SCOOP-00010 (Co 11): Kept: Funding, 3 days ago
  FAKE-SCOOP-00011 (Co 14): Kept: Product Launch, 4 days ago
  FAKE-SCOOP-00012 (Co 15): Kept: Executive Move, role 'Head of Product', 2 days ago
  FAKE-SCOOP-00013 (Co 19): Kept: Funding, 5 days ago
  FAKE-SCOOP-00014 (Co 26): Kept: Executive Move, role 'Head of Product', 5 days ago
  FAKE-SCOOP-00015 (Co 27): Kept: Funding, 6 days ago
  FAKE-SCOOP-00016 (Co 4): Dropped: generic hiring note (matches drop keyword 'medical & health', no buyer-role context)
  FAKE-SCOOP-00017 (Co 13): Dropped: scoop type not a Pendo trigger (types: Facilities)
  FAKE-SCOOP-00018 (Co 2): Dropped: expired, 200 days old (limit 45 for funding)
  FAKE-SCOOP-00019 (Co 99): Kept: Funding, 3 days ago
  scoop skipped (malformed): scoop_from_zoominfo: scoop 'FAKE-SCOOP-BAD' has no published date
  kept scoops with no matching account: 2

== ACCOUNTS ==
accounts kept: 24
  low confidence: 5
  size_unknown: 2
kept but flagged for review:
  country_unknown: 1
    e.g. Victor Unknowncountry Pty Ltd
  flagged_non_anz_billing_anz_hq: 1
    e.g. November Flagged Pty Ltd
  noise_location: 1
    e.g. Sydney CBD
  nooks_only: 1
    e.g. Papa Nooksonly Pty Ltd
  zoominfo_only: 1
    e.g. Oscar Analytics Pty Ltd
accounts dropped: 6
  customer: 1
    e.g. Sierra Customer Pty Ltd
  malformed_row: 1
    e.g. No Id Pty Ltd
  noise_location: 1
    e.g. Melbourne CBD
  over_max_employees: 1
    e.g. Tango Giant Pty Ltd
  recently_touched: 1
    e.g. Hotel Recent Pty Ltd
  under_min_employees: 1
    e.g. Uniform Tiny Pty Ltd
merged by name: 0
name_merge_ambiguous (not merged): 0

== RANKER ==
Ranked 14 accounts; skipped 6; needs prospect 3; spilled 1.
  window morning: 1 accounts
  window afternoon: 13 accounts
  skipped - all prospects blocked (opted out / do not call): 2
  skipped - no timezone: 3
  skipped - on last sheet: 1
  needs prospect (NOT callable, no task) - no_persona_match: 1
  needs prospect (NOT callable, no task) - no_prospects_on_account: 1
  needs prospect (NOT callable, no task) - persona_match_no_phone: 1
  no real trigger (ranked on fit only): 4
   1. Echo DNC Holdings Pty Ltd score=100 afternoon Wed 16:13 AEDT | Executive move scoop 1 day ago; SDR target; Tier 1 fit (900 employees); Prospect: Pat Callable (Product Manager)
   2. Alpha Digital Pty Ltd score=98.38 afternoon Wed 16:16 AEDT | Executive move scoop 3 days ago; SDR target; Tier 1 fit (400 employees); Prospect: Person 1-1 (Head of Product)
   3. Golf Switchboard Pty Ltd score=78.21 afternoon Wed 16:19 AEDT | Funding scoop 4 days ago; Tier 1 fit (250 employees); Prospect: Gil Own (Product Manager)
   4. Yankee Darwin Pty Ltd score=76.66 afternoon Wed 14:43 ACST | Executive move scoop 5 days ago; Tier 2 fit (150 employees); Prospect: Person 26-1 (Head of Product)
   5. Charlie Cloud Ltd score=70.13 morning Thu 08:45 NZDT | Product launch scoop 6 days ago; Tier 2 fit (80 employees); Prospect: Person 3-1 (Head of Digital)
   6. India Old Connect Pty Ltd score=65.97 afternoon Wed 16:23 AEDT | Funding scoop 6 days ago; Tier 2 fit (150 employees); Prospect: Person 9-1 (Director of Product)
   7. Zulu Brisbane Pty Ltd score=65.97 afternoon Wed 15:13 AEST | Funding scoop 6 days ago; Tier 2 fit (150 employees); Prospect: Person 27-1 (Head of Product)
   8. Bravo Software Pty Ltd score=61.8 afternoon Wed 13:30 AWST | Funding scoop 10 days ago; Tier 2 fit (120 employees); Prospect: Person 2-1 (VP Product)
   9. November Flagged Pty Ltd score=60 afternoon Wed 16:13 AEDT | Product launch scoop 4 days ago; Tier 1 fit (250 employees); Prospect: Person 14-1 (Head of Product); Non-ANZ billing, ANZ HQ (small penalty); Low-confidence record (score capped at 60); Flag: flagged_non_anz_billing_anz_hq - check before calling
  10. Romeo Nosize Pty Ltd score=59.57 afternoon Wed 16:26 AEDT | Funding scoop 5 days ago; Headcount unknown (neutral fit); Prospect: Person 19-1 (Head of Product)
  11. Alpha Two Pty Ltd score=40 afternoon Wed 16:18 AEDT | No trigger: ranked on fit only; Tier 1 fit (300 employees); Prospect: Person 28-1 (Head of Product)
  12. Bravo Two Pty Ltd score=40 afternoon Wed 16:24 AEDT | No trigger: ranked on fit only; Tier 1 fit (300 employees); Prospect: Person 29-1 (Head of Product)
  13. Delta Apps Pty Ltd score=40 afternoon Wed 15:51 AEST | No trigger: ranked on fit only; Tier 1 fit (300 employees); Prospect: Person 4-1 (Head of Product)
  14. Mike Adelaide Pty Ltd score=30 afternoon Wed 15:43 ACDT | No trigger: ranked on fit only; Tier 2 fit (60 employees); Prospect: Person 13-1 (Head of Customer Success)
  SKIPPED accounts (6):
    - Foxtrot Duplicate Pty Ltd: all prospects blocked (opted out / do not call)
    - Juliet Lastsheet Pty Ltd: on last sheet
    - Oscar Analytics Pty Ltd: no timezone
    - Papa Nooksonly Pty Ltd: no timezone
    - Victor Unknowncountry Pty Ltd: no timezone
    - Whiskey Optout Pty Ltd: all prospects blocked (opted out / do not call)
  NEEDS PROSPECT (no task) (3):
    - Kilo Noprospect Pty Ltd: no_prospects_on_account, score 64.36
    - Lima Nophone Pty Ltd: persona_match_no_phone, score 25
    - Xray Nopersona Pty Ltd: no_persona_match, score 15
  SPILLED (held, no task tonight) (1):
    - Sydney CBD: score 30, intended Thu 26 Nov 08:45
```

## Known limitations

- No live data has touched any of it. Every real-shape assumption is the 1a connector notes; UNVERIFIED items stay UNVERIFIED (Gmail sent-date key, intent row shape and topic names, real ZoomInfo topics).
- N2 RESOLVED (1f-final): on a late Thursday run an early-zone account (Perth, Brisbane, Darwin, Adelaide) whose cooldown ends Friday passes accounts, but the ranker judges the cooldown on the account's own zone call date and SKIPS it ('In cooldown until <date> (account-local)'); the -30 cooldown penalty was removed.
- N5 RESOLVED (1f-final): ranker recency now uses the best signal's per-kind limit (`signals.max_age_for`), so an exec move 46-90 days old keeps recency points; `config.signals.max_age_days` is only the fallback.
- N3 RESOLVED (1f-final): `accounts.build_account_list(touched=None, today=None, ...)` makes `touched` optional.
- Accounts with no Salesforce row (ZoomInfo-only, Nooks-only) have no timezone, so they cannot be ranked, and everything in state is keyed by Salesforce id, so they could not be hard-skipped or cooled down even if they could. Their scoops are matched and kept but unused.
- Low-confidence accounts (flagged, ZoomInfo-only, Nooks-only) are capped at a score of 60; clean accounts can score up to 100.
- Public holidays (AU states, NZ) are not modelled. Windows and the 3-minute slot are defaults.
- Persona matching is exact-phrase: "Product Owner", "Engineering Manager", "Chief Information Officer" and "Director of Engineering" do not match (a `config.yaml` choice for Alex).
- Role matching for "product" scoops is regex: the ambiguity flag is carried into the signal evidence as a suffix, but the call sheet should show it prominently.
- Opted-out-of-email contacts are blocked for calls too (decision 2); likely a large share of real contacts.
- Do Not Call is learned only from the calls the adapter reads: whether another rep's Do Not Call on the same prospect is visible under the owner filter is UNVERIFIED.
- Scale tested to 12,000 accounts (the real ANZ book size), but with synthetic rows.

## Phase 2 risks (prioritised)

**P0: would cause a wrong or unsafe result**

1. **State must survive between scheduled runs.** `state/state.json` is gitignored and the cloud container is reclaimed. A fresh container is a "new" state: live is gated until Do Not Call is rebuilt from the FULL call history every run, and last-sheet / cooldown memory is gone, so the same accounts repeat. Decide where state lives (persisted volume, or an artifact/db the nightly run reads and writes) before 2c. `save_state` now refuses to shrink a populated file (`StateShrinkError`) and keeps a `.prev` backup, but only helps if the file persists.
2. **`mark_dnc_rebuilt` contract.** It needs `reached_last_page=True` (from the adapter, not a guess) and a call count at least as large as the distinct calls already recorded; it also records the earliest call date seen. An adapter that stops at a page cap or a rate-limit error must pass `reached_last_page=False`. A seat with genuinely no history needs `empty_history_confirmed=True`. Page `listCalls` to the very end (pages are ordered by id, not time) and use pages of 1,000+ for `record_calls`.
3. **`last_sheet_ids(state, run_date)` needs the run date.** It ignores the sheet recorded for `run_date` itself, so a second run on the same night is stable. A dry run must NEVER call `record_sheet` (it would still be harmless for H1 now, but the sheet history must describe tasks actually written). Only `ranked` accounts are recorded, never `spilled`, `needs_prospect` or `skipped`.
4. **`accounts.build_account_list` takes `cooldown_until` plus `call_date`.** Pass the state's `cooldown_until` (even `{}`: `touched` is then ignored) and `call_date` = the first date a task could be due. See N2 for the multi-zone gap. `touched` / `last_touched` stays a legacy path.
5. **The sheet builder must call `mark_do_not_call(prospects, state)` immediately before `rank_accounts`.** The ranker alone lets a Do Not Call person through (pinned by `test_b_ranker_alone_does_not_know_state`). Assert afterwards that no chosen prospect is blocked.
6. **Writes only for `ranked`.** Never `spilled`, `needs_prospect`, `skipped`. The writer should assert its task prospect ids are a subset of `RankResult.prospects`. Dry-run by default; `--live` explicit; `require_healthy(live=...)` driven by the same flag.
7. **Use `signals.best_signal` for the trigger.** The ranker now does, and `RankedAccount.signals[0]` is the best signal, so an intent signal is never the trigger while a dated one exists. Intent row shape is still UNVERIFIED.

**P1: will cost time or quietly drop real accounts**

8. **Salesforce read at ~20k assigned / ~12k ANZ rows.** Run `COUNT()` first; page with `nextRecordsUrl`; check the connector row cap and spill-to-file behaviour (UNVERIFIED). `HQ_Country__c` is a textarea and cannot be used in `WHERE`: pull the assigned set and filter with `resolve_country` in Python, or the ~420 flagged accounts vanish.
9. **Contact / prospect join.** Nooks prospect rows carry no phone and no opt-out. Both come only from the SF Contact (`crmId`), so SF Contacts for every Nooks prospect on every candidate account must be pulled (chunk `AccountId IN (...)`). A prospect with no `crmId` has no phone, so its account lands in `needs_prospect: persona_match_no_phone`. Check the join rate before trusting a sheet.
10. **Nooks account to SF account merge.** `listAccounts` returns no `crmId` on read; it is only a filter, so one call per SF account is ~12k calls. Derive `nooks_account_id <-> sf_account_id` from the prospect + Contact join and pass `_sf_account_id`; otherwise accounts fall to the name merge (flagged) or stay Nooks-only duplicates with no timezone.
11. **Two-pass account build.** Local call days need account timezones, and cooldowns need state: the pipeline builds the account list twice (first without cooldowns, for timezones and countries; then with). 2c should keep that shape or split the timezone lookup out.
12. **ZoomInfo.** Scoop pull window 90 days for people moves; match on `Account.zoominfo_id` (Salesforce does not always hold it; two SF accounts can share one, and `group_by_zoominfo_id` now gives both the trigger). ZoomInfo-only companies with a strong scoop cannot be called (no timezone): list them separately as "enrich first". Count credits in the log (rule 7); `zoominfo_credit_budget_per_run` is 0.
13. **Rate limits** for Nooks, ZoomInfo and Salesforce are unknown. Retry with backoff; make adapters resumable; a failed or partial page must stop that step and be logged (rule 6), never be returned as complete.

**P2: needed by the sheet builder**

14. **What `callsheet.py` needs from `RankResult`:** `ranked` (account, `window`, aware `local_call_time`, convert to UTC for Nooks `dueAt`), `prospects[account_key]`, `score_breakdown`, `reasons`, `signals` (best first), `account.flags` / `confidence` / `notes`, `needs_prospect` + `needs_prospect_codes` (separate "find a contact" list, never tasks), `spilled`, `skipped` with reasons. `account_key` is the lookup id; `record_sheet` takes `ra.account.sf_account_id`.
15. **Run log (rule 5).** `ranker.summarise` now names skipped / needs-prospect / spilled accounts (25 per list plus "and N more"), and `accounts.summarise` names up to 5 per flag and per drop reason. For a full audit trail 2c should still write one line per account. Capture `st.report` before saving; `st.last_run` holds counts (no ids) from the previous saves.
16. **Opener generation (2d).** The LLM writes text only: pass `best_signal.evidence` (an ambiguous role carries a "role ambiguous" suffix), the persona title and Salesforce / ZoomInfo facts; for `No trigger` the opener must say so and invent nothing. Pass the scoop link.
17. **Policy questions for Alex at the checkpoint:** (a) a Do Not Call on one contact does not stop calls to a colleague at the same account; (b) email-opted-out contacts are blocked for calls; (c) the low-confidence ceiling of 60; (d) per-zone first usable day (Perth / Brisbane can be Thursday when Sydney is Friday) versus one call date for the whole sheet.
18. **Public holidays** are not modelled; tasks can land on an AU state or NZ public holiday.
19. **Page size.** Keep `record_calls` pages at 1,000 or more, or pass all calls at once (N1).

## New findings after the merge

See `docs/review_1g.md`, section "Final reconcile": N1 (`record_calls` cost depends on pages x prospects), N2 (early-zone cooldown), N3 (one required-but-ignored argument), N4 and N5 (stale docs).
