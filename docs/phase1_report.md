# Phase 1 report (core engine, offline)

Prepared by the 1g integration review. Evidence: `docs/review_1g.md`, `tests/test_integration_pipeline.py`. Everything here ran offline on synthetic data; no connector was called.

## What Phase 1 delivers

Seven modules, plain Python 3.11+, no network, no LLM, no randomness:

| Module | Job | In one line |
|---|---|---|
| `src/models.py` | Data classes | `Account`, `Prospect`, `CallRecord`, `Scoop`, `Signal`, `RankedAccount` ... |
| `src/config.py` | Config + time | `load_config`, tiers, persona lists, per-prospect time zones, call windows (DST via zoneinfo) |
| `src/loaders.py` | Raw rows to models | SF / Nooks / ZoomInfo / Gmail rows, phone and email normalisation, 15/18-char SF ids |
| `src/state.py` | Memory | who was called, cooldowns, Do Not Call (by id, phone, hashed email), sent emails, last sheet; atomic `state.json`; live-run gate |
| `src/accounts.py` | Account list | merge the three sources, drop customers / out-of-size / blocked / recently touched, flag (never drop) your own book's oddities |
| `src/signals.py` | Triggers | keep Pendo-buyer scoops only, role rules, recency decay, `No trigger` when there is none |
| `src/ranker.py` | Score and schedule | score 0-100, persona pick, daily cap, per-zone call times, spill, needs-prospect list |

The pipeline wired end to end (this is what task 2c will do, in this order): raw rows -> loaders -> `state.load_state` -> `verify_dispositions` -> `record_calls` (paged) -> `mark_dnc_rebuilt` -> `save/load` -> `require_healthy(live=True)` -> `record_sent_emails` -> `mark_do_not_call` on every prospect -> `last_touched` / `cooldown_until` -> `accounts.build_account_list` -> `signals.filter_scoops` + `scoops_to_signals` -> `ranker.rank_accounts` -> `RankResult` -> `record_sheet` for `ranked` only. The harness is `run_pipeline` in the integration test file.

## Tests

| File | Tests |
|---|---|
| `test_accounts.py` | 173 |
| `test_config.py` | 8 |
| `test_loaders.py` | 28 |
| `test_ranker.py` | 189 |
| `test_review_fixes.py` | 69 |
| `test_signals.py` | 272 |
| `test_state.py` | 190 |
| `test_timezones.py` | 39 |
| **`test_integration_pipeline.py` (new, 1g)** | **93** (89 pass, 4 `xfail` bug repros) |
| Total | 1,061 collected: **1,057 passed, 4 xfailed**, also clean under `python -m pytest tests -W error` (about 26 s) |

`xfail` (non-strict) tests describe the desired behaviour for H1, H2, M1 and L1 in `docs/review_1g.md`; when a fix lands they show as XPASS and the marker should be removed.

## Verdict (1g): PASS WITH FIXES

Fix H1 (same-night rerun replaces the sheet) and H2 (ranker lets an intent signal be the trigger) before Phase 2. Details, repros and where to fix each: `docs/review_1g.md`.

## Performance (3,000 synthetic accounts)

`python -m tests.test_integration_pipeline --scale`: 3,000 SF accounts, 90 ZoomInfo-only, 4,174 prospects, 620 calls, 1,534 scoops.

| Stage | Seconds |
|---|---|
| intake (loaders, first account pass, prospects) | 0.19 |
| state (record_calls, save, reload, emails) | 1.32 (calls paged 7 at a time: an artefact, see L5) |
| accounts (merge, drops) | 0.18 |
| signals | 0.03 |
| rank | 0.20 |
| **total** | **1.97** |

Max RSS for the whole process 55 MB; Python heap peak during the run 11 MB. Result: 40 ranked (cap), 962 needs a prospect, 1,294 skipped (1,076 below the cap), 794 dropped. 20,000 calls through `record_calls`: 4.8 s at 100 per page, 0.4 s at 5,000 per page.

## Sample run log

Produced by `python -m tests.test_integration_pipeline --sample-log` (named synthetic world, Wed 2026-11-25 14:00 AEDT, last night's sheet held one account, dry run so no sheet is recorded). All names, ids, numbers are invented. The ranker's own summary prints the top 10 ranked and top 5 needs-prospect; the real log (CLAUDE.md rule 5) must list every account (review item L7).

```
RUN LOG (offline harness, synthetic data) now=2026-11-25T03:00:00+00:00 run_date=2026-11-25 accounts_today=2026-11-25

== STATE ==
state: new=False reset=False dnc_rebuilt_at=2026-11-25 dnc_rebuilt_calls=7
record_calls: {"calls_seen": 7, "dnc_added": 3, "duplicate": 0, "new": 7, "no_account": 0, "unmatched": 0, "unmatched_call_ids": [], "unmatched_reasons": {}}
record_sent_emails: {"date_unknown": 0, "duplicate": 0, "new": 2, "skipped_no_key": 0}
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
accounts kept: 23
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
accounts dropped: 7
  customer: 1
    e.g. Sierra Customer Pty Ltd
  malformed_row: 1
    e.g. No Id Pty Ltd
  noise_location: 1
    e.g. Melbourne CBD
  over_max_employees: 1
    e.g. Tango Giant Pty Ltd
  recently_touched: 2
    e.g. Alpha Two Pty Ltd; Hotel Recent Pty Ltd
  under_min_employees: 1
    e.g. Uniform Tiny Pty Ltd
merged by name: 0
name_merge_ambiguous (not merged): 0

== RANKER ==
Ranked 14 accounts; skipped 6; needs prospect 3; spilled 0.
  window morning: 1 accounts
  window afternoon: 13 accounts
  skipped - all prospects blocked (opted out / do not call): 2
  skipped - no timezone: 3
  skipped - on last sheet: 1
  needs prospect (NOT callable, no task) - no_persona_match: 1
  needs prospect (NOT callable, no task) - no_prospects_on_account: 1
  needs prospect (NOT callable, no task) - persona_match_no_phone: 1
  no real trigger (ranked on fit only): 4
   1. Echo DNC Holdings Pty Ltd score=100 afternoon Wed 14:01 AEDT | Executive move scoop 1 day ago; SDR target; Tier 1 fit (900 employees); Prospect: Pat Callable (Product Manager)
   2. Alpha Digital Pty Ltd score=98.38 afternoon Wed 14:25 AEDT | Executive move scoop 3 days ago; SDR target; Tier 1 fit (400 employees); Prospect: Person 1-1 (Head of Product)
   3. November Flagged Pty Ltd score=78.71 afternoon Wed 14:01 AEDT | Product launch scoop 4 days ago; Tier 1 fit (250 employees); Prospect: Person 14-1 (Head of Product); Non-ANZ billing, ANZ HQ (small penalty)
   4. Golf Switchboard Pty Ltd score=78.21 afternoon Wed 14:50 AEDT | Funding scoop 4 days ago; Tier 1 fit (250 employees); Prospect: Gil Own (Product Manager)
   5. Yankee Darwin Pty Ltd score=76.66 afternoon Wed 13:30 ACST | Executive move scoop 5 days ago; Tier 2 fit (150 employees); Prospect: Person 26-1 (Head of Product)
   6. Charlie Cloud Ltd score=70.46 afternoon Wed 16:01 NZDT | Product launch scoop 5 days ago; Tier 2 fit (80 employees); Prospect: Person 3-1 (Head of Digital)
   7. India Old Connect Pty Ltd score=65.97 afternoon Wed 15:15 AEDT | Funding scoop 6 days ago; Tier 2 fit (150 employees); Prospect: Person 9-1 (Director of Product)
   8. Zulu Brisbane Pty Ltd score=65.97 afternoon Wed 13:30 AEST | Funding scoop 6 days ago; Tier 2 fit (150 employees); Prospect: Person 27-1 (Head of Product)
   9. Bravo Software Pty Ltd score=61.8 morning Wed 11:01 AWST | Funding scoop 10 days ago; Tier 2 fit (120 employees); Prospect: Person 2-1 (VP Product)
  10. Romeo Nosize Pty Ltd score=59.57 afternoon Wed 15:40 AEDT | Funding scoop 5 days ago; Headcount unknown (neutral fit); Prospect: Person 19-1 (Head of Product)
  P1. Kilo Noprospect Pty Ltd score=64.36 | Needs a prospect: no_prospects_on_account; Funding scoop 3 days ago; Tier 1 fit (200 employees); No callable persona with a phone
  P2. Lima Nophone Pty Ltd score=25 | Needs a prospect: persona_match_no_phone; No trigger: ranked on fit only; Tier 1 fit (200 employees); No callable persona with a phone
  P3. Xray Nopersona Pty Ltd score=15 | Needs a prospect: no_persona_match; No trigger: ranked on fit only; Tier 2 fit (100 employees); No callable persona with a phone
```

## Known limitations (Phase 1 as delivered)

- No live data has touched any of it; every real-shape assumption is the 1a connector notes (UNVERIFIED items stay UNVERIFIED: Gmail sent-date key, intent row shape and topic names, real ZoomInfo topics).
- Bugs H1, H2, M1, M2 and the 8 minor items in `docs/review_1g.md`.
- Account confidence never changes a score (no ceiling for low-confidence accounts).
- Accounts without a Salesforce row (ZoomInfo-only, Nooks-only) have no timezone and so can never be ranked; their scoops are matched and kept but unused. They also cannot be tracked in sheet history or cooldowns (all state is keyed by SF id).
- Public holidays are not modelled (AU states, NZ). Windows and the 3-minute slot are defaults, not confirmed.
- Persona matching is exact-phrase: "Product Owner", "Engineering Manager", "CIO" do not match (config choice).
- Role matching for "product" scoops is regex: flagged `ambiguous` but the flag lives on `ScoopVerdict` only (see risk 15).
- Opted-out-of-email contacts are blocked for calls too (decision 2); likely a large share of contacts.
- 1c: partial call history is accepted as a full rebuild (M1), `save_state` can silently empty DNC (L1), UTC-date fallback on a corrupt local date (L2).
- Scale tested to 3,000 accounts, not the ~12,000 ANZ accounts in the real book.

## Phase 2 risks (prioritised)

**P0: would cause a wrong or unsafe result**

1. **State has to survive between scheduled runs.** `state/state.json` is gitignored and this environment's container is reclaimed. A fresh container is a "new" state: live is gated until Do Not Call is rebuilt from the FULL call history every run, and last-sheet / cooldown memory is gone (so the same accounts repeat). Decide where state lives (a persisted volume, or an artifact/db the nightly run reads and writes) before 2c. Also fixes the rerun story for H1.
2. **Same-night rerun / dry run then live (H1).** 2c must record the sheet only on `--live`, only for `ranked`, and `last_sheet_ids` must ignore the current night's sheet. Test: run twice, same `ranked`.
3. **Paging `listCalls` to the end and proving it.** Pages are ordered by id, not time (connector notes), so partial reads look plausible. The adapter must return `(calls, complete, total)` and only then `mark_dnc_rebuilt` (1c M1). Also confirm whether other reps' Do Not Call dispositions are visible when `listCalls` is filtered to the owner: if not, DNC set by a teammate on the same prospect is missed (UNVERIFIED). Use a large page (500+) because `record_calls` copies state per page (L5).
4. **`mark_do_not_call` must run on the final prospect list immediately before `rank_accounts`.** The ranker alone lets a DNC person through (pinned by `test_b_ranker_alone_does_not_know_state`). Make the sheet builder call it in one place and assert afterwards that no chosen prospect `is_blocked(p, state, "call")`.
5. **Writes only for `ranked`.** Never `spilled`, `needs_prospect`, `skipped`. Add an assertion in the writer: task prospect ids must be a subset of `RankResult.prospects` values. Dry-run by default; `--live` required explicitly; `require_healthy(live=...)` driven by the same flag.
6. **Intent as a trigger (H2).** Before 2b wires intent, fix the ranker or the opener could quote an interest signal as an event. Use `signals.best_signal` for the trigger the opener quotes, never `signals[0]`.

**P1: will cost time or quietly drop real accounts**

7. **Salesforce read at ~20k assigned / ~12k ANZ rows.** Run `COUNT()` first; page with `nextRecordsUrl`; check the connector's row cap and whether it spills to a file (the Account schema call already does) (UNVERIFIED). `HQ_Country__c` is a textarea and cannot be used in `WHERE`, so a server-side filter on billing country alone would hide the ~420 flagged accounts: pull the full assigned set and filter with `resolve_country` in Python. Filter `Current_Customer__c` server-side only.
8. **Contact / prospect join.** Nooks prospect rows carry no phone and no opt-out. Phone and `HasOptedOutOfEmail` come only from the SF Contact (`crmId`), so SF Contacts for every Nooks prospect on every candidate account must be pulled (chunk `AccountId IN (...)`). A prospect with no `crmId` has no phone, so its account lands in `needs_prospect: persona_match_no_phone`. Check join rate before trusting the sheet.
9. **Nooks account to SF account merge.** `listAccounts` returns no `crmId` on read; it is only a filter. One call per SF account is ~12k calls. Derive `nooks_account_id <-> sf_account_id` from the prospect + Contact join instead and pass `_sf_account_id`; otherwise accounts fall back to the name merge (`name_merged`, flagged for review) or stay Nooks-only duplicates with no timezone.
10. **Cooldown `today` (M2) and email touch (M1).** Pass the first call date, not the run date, for night runs; pass only `cooldown_until`, not `last_touched`.
11. **ZoomInfo.** Scoop pull window must be 90 days for people moves; match on `Account.zoominfo_id`, which Salesforce does not always hold, and two SF accounts can share one ZoomInfo id (L2: one gets the trigger, the other "No trigger"). ZoomInfo-only companies with a strong scoop cannot be called (no timezone): list them separately as "enrich first". Count credits in the log (CLAUDE.md rule 7); `zoominfo_credit_budget_per_run` is 0.
12. **Rate limits.** Nooks, ZoomInfo and Salesforce limits are unknown (UNVERIFIED). Build retry with backoff and make every adapter resumable; a failed page must stop that step and be logged (rule 6), never return a partial list as complete.

**P2: needed by the sheet builder**

13. **What `callsheet.py` needs from `RankResult`:** `ranked` (account, `window`, `local_call_time` aware local time, convert to UTC for Nooks `dueAt`), `prospects[account_key]` (the chosen prospect; one per account by default), `score_breakdown`, `reasons`, `signals` (and `signals.best_signal` for the trigger), `account.flags` / `confidence` / `notes` for review lines, `needs_prospect` + `needs_prospect_codes` (separate "find a contact" list, never tasks), `spilled`, `skipped` with reasons. `account_key` is the id for lookups; `record_sheet` takes `ra.account.sf_account_id` (accounts without one are dropped silently, L6).
14. **Run log.** `ranker.summarise` names no skipped account and caps lists (L7). `State.report` is not persisted (L4): capture it before `save_state`. Log every account's outcome and reason, ZoomInfo credits spent, state flags (`is_new`, `was_reset`, `load_warnings`).
15. **Carry the ambiguity flag.** `ScoopVerdict.ambiguous` ("role ambiguous: check it is a digital product role") never reaches `Signal` / `RankedAccount`; 2c must keep the verdicts and join by `Signal.source` (`zoominfo_scoop:<id>`) so the sheet can show it.
16. **Opener generation (2d).** The LLM writes text only: pass it `best_signal.evidence`, the persona title and the account facts from Salesforce / ZoomInfo; for `No trigger` the opener must say there is no trigger and not invent one. Evidence is trimmed to a fixed length; pass the source link too.
17. **Policy questions for Alex at the checkpoint:** (a) a Do Not Call on one contact does not stop calls to a colleague at the same account (the ranker picks the next persona): confirm. (b) Email-opted-out contacts are blocked for calls. (c) A cap for low-confidence accounts (M3). (d) One call date for the whole sheet versus per-zone first usable day (Perth / Brisbane can be Thursday when Sydney is Friday).
18. **Public holidays** are not modelled: tasks can land on an AU state or NZ public holiday. Add a small per-zone holiday list or accept the risk.
19. **`record_calls` ordering dependency.** Local call days need account time zones, and `accounts` needs state for cooldowns: the pipeline builds the account list twice (first without touches, to get time zones; then with). 2c should keep that two-pass shape or split the timezone lookup out of `build_account_list`.
