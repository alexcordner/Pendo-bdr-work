# Review of task 1f: `src/ranker.py` (branch `phase1/1f-ranker`, commit b5a9bab)

Reviewer: independent session, offline, read-only (no connector calls, worker files untouched, synthetic data only).
Reproduce every number below with `python docs/review_1f_checks.py` (scratch script, not part of the test suite).

## Verdict: PASS WITH FIXES

The scoring maths, determinism, tier boundaries, window/DST handling and daily-cap ordering are correct. Two issues should be fixed before 1g/2c build on this module: **H1** (accounts with no callable contact can fill the whole sheet) and **H2** (cooldown "today" is the UTC date, off by one for ANZ). Four medium items follow. Nothing here involves writes, network or secrets.

## Findings by severity

### HIGH
**H1. Accounts without a callable prospect crowd out callable ones.** `src/ranker.py:277-299` (design issue, checklist item 4).
Demonstrated: 45 accounts with a fresh Funding signal and no prospect, plus 10 tier-3 accounts with a callable Head of Product and no signal. Result: `ranked` holds 40 no-prospect accounts and **0 of the 10 callable ones** (80 points vs 22.5). Nooks `createTask` needs a prospectId, so none of the 40 can become a task and the night yields no usable calls. The only way the sheet builder can tell is `result.prospects[key] == []` or parsing a reasons string. See the recommendation below.

**H2. Cooldown compares against the UTC date of `now`, not the date the call happens.** `src/ranker.py:248, 154-155`.
`cooldown_until[key]` is documented as "first date callable again", active while `> today`. `today` is the UTC date, but the call happens on the account-local date of the chosen window, which for ANZ is almost always later.
- Run Tue 20:00 AEDT (= Tue 09:00 UTC), call date Wed, `cooldown_until = Wed`: the account IS penalised (-30, reason "In cooldown") although Wed is the first callable day. Cooldowns are effectively N+1 days.
- `now` = Wed 23:00 UTC = Thu 10:00 Sydney: `cooldown_until = Thu` is still penalised (UTC says Wed).
- A `datetime` (instead of `date`) value raises `TypeError` (`>` between datetime and date) and kills the run, `:155`.
Fix: compute `call_date = windows[0].start.date()` (already available at `:273`) and test `cooldown_until[i] > call_date`; coerce `datetime` to `.date()` or reject with a clear `ValueError`. Contract below.

### MEDIUM
**M1. "Same account never twice in a row" and cooldown are only penalties, not exclusions.** `:192` (and `:278`).
A top account on `last_sheet_ids` scored 65 and stayed rank 1 ahead of three 22.5-point accounts. An account in cooldown until 5 weeks out also stayed rank 1. The brief says "never twice in a row", and `config.yaml` says "don't re-queue an account touched in the last N days". With only a -30 penalty, a fresh trigger beats both rules. Decision for the coordinator: keep the penalty for cooldown if that is the intent of `cooldown_penalty`, but make `last_sheet_ids` a hard skip (`skipped_reason="on last sheet"`), or skip both and keep the penalty only as the tie-breaker for backfill when fewer than cap accounts remain.

**M2. Persona matching under-matches real titles; one false positive.** `:89-101` plus `config/config.yaml` personas. Table below. Word boundaries are the right call (see "CTO" vs "Director") but need normalisation and aliases.

**M3. An invalid timezone string crashes the whole run.** `:273` calls `next_working_windows`, which raises `ZoneInfoNotFoundError` for `"Mars/Base"` and `ValueError` for `""`. Only `None` is guarded (`:263`). One bad Account row aborts the sheet. Wrap in try/except and skip with `"invalid timezone <tz>"`; log it (CLAUDE.md rule 6).

**M4. Spec drift on unknown headcount.** `docs/interfaces.md` decision 3 and the `Account.size_unknown` docstring (`src/models.py:63-65`) say the ranker gives size-unknown accounts **no** icp_fit points; the code gives 0.3 x 25 = 7.5 (`:52, :175-176`, same as tier 3). The worker's choice is defensible (kept, flagged, neutral) and is documented in the ranker docstring and the reasons text, but docs contradict code. Update the docs, or change the constant. Note a 19-employee account scores 0 icp_fit, below an unknown-size one.

**M5. Scheduling uses only the first open window, and can squeeze a sheet into minutes.** `:281, 312-329`.
(a) `windows[0]` only: on a nightly run every account lands in "morning", `by_window["afternoon"]` is always empty, and 40 accounts share 08:45-11:30 (about 4 min each, 08:45 to 11:25:33). (b) If `now` is late in the window, all accounts are packed in what is left: 40 accounts with 1 minute left get slots 11:29:00-11:29:39. (c) With `now` inside a window, 40 guessed-tz accounts get 30 s steps so only 20 distinct minutes (pairs share a minute). Suggest a minimum slot (e.g. 3 min); if the remaining span cannot hold the group, spill the lowest-ranked into the next window (afternoon, then next day). Needs a spec decision from Alex on how many calls per window he really makes.

### LOW
- **L1.** Future-dated signal gets full recency (age floored at 0, `:151`; tested as "treated as today"). Scoop dates cannot really be in the future; 1e should drop or flag them. A future date from clock skew of a day is harmless; a year-ahead date is a data error that ranks first on recency.
- **L2.** Naive `source_date` raises `TypeError` in `_age_days` (`:151`). The interface says aware UTC, so this is a contract guard only. Cheap fix: treat naive as UTC or skip with "date unknown".
- **L3.** Guessed-timezone later-half rule (`:325-327`, `GUESSED_TZ_START_FRACTION`) is documented (docstring lines 36-37, models.py, reasons text) and sensible for Brisbane (-1h) and Adelaide/Darwin (-0.5h), but does **not** protect Perth: the later half of the Sydney morning is 10:07-11:27 AEDT = 07:07-08:27 AWST, still before the 08:45 window. Low impact because guessed means "no state"; note it, or use the afternoon window for guessed Australian accounts.
- **L4.** Confirmed and guessed groups are spread separately, so two accounts can share a minute (max 2 per minute observed with 20 + 20 Sydney accounts). Cosmetic.
- **L5.** `mine` de-duplicates prospects by `id(p)` (`:267`), so two separate `Prospect` objects with the same `nooks_id` (e.g. loaded twice) would both count. Harmless today (`pick_prospects` takes the first), but dedupe by `nooks_id` when present.
- **L6.** `RankResult.prospects` is keyed by `account_key` (`name:<name>` fallback): two id-less accounts with the same name would collide. Negligible.

## Checklist

| # | Item | Result |
|---|---|---|
| 1 | pytest | **PASS.** Whole suite 218 passed, plain and `-W error`; ranker file 76 passed in both. |
| 1 | Determinism | **PASS.** 20 random shuffles of accounts, prospects and per-key signal lists on 120 mixed-tz accounts with ties gave identical `ranked`/`skipped` output (scores, windows, call times, reasons). Digest of a 150-account run identical under `PYTHONHASHSEED` 0, 1, 42, 999 and unset (`a94912017a9bce1c033fde8d93f51a97`). Equal-score, equal-name accounts order by sf id both ways. |
| 2 | Scoring correctness | **PASS.** Three scenarios below match by hand. Breakdown sums to score in every case (clamp recorded as `clamp_adjustment`, `:199-200`). Max raw 105 clamps to 100. |
| 2 | Edge cases | **PASS** with L1/L2. See tables. |
| 3 | Personas | **FAIL (M2).** See table. |
| 4 | Design issue | **Recommendation: (a)**, see below. |
| 5 | Cooldown contract and "today" | **FAIL (H2).** Contract written below. |
| 6 | Windows | **PASS** with M5/L3/L4. |
| 7 | Code quality | **PASS.** Constants documented at top (`:50-55`), docstring scoring table matches behaviour, no network/file/env access, imports only `src.config` and `src.models` (does not import state/accounts/signals), Python 3.11+ (`zoneinfo`, `X | None`). No dead code found by reading (no linter installed here: pyflakes/ruff unavailable, so unused imports are UNVERIFIED by tool, none seen by eye). |

## Scoring scenarios (weights: trigger 40, icp 25, recency 15, prospect 15, cooldown -30, sdr 10; flagged -3)

`now` = 2026-11-25 20:00 UTC; Sydney, callable prospect where stated.

| # | Setup | Expected by hand | Actual |
|---|---|---|---|
| S1 | 1 signal strength 0.8, 9 days old; 100 employees (tier2 0.6); callable prospect; SDR target | 40x0.8=32; 25x0.6=15; 15x(1-9/45)=12; 15; sdr 10; total **84** | 84.0: 32 / 15 / 12 / 15 / 0 / 10 / 0, no clamp |
| S2 | No signal; 2000 employees (tier1); no prospect; in cooldown; flagged group | 0+25+0+0-30+0-3 = -8, clamp to **0** (adjustment +8) | 0.0, `clamp_adjustment` +8.0 |
| S3 | 3 signals (0.95, 0.9 M&A, 0.5 hire), all fresh; 200 employees; prospect; SDR | trigger min(1, .95+.2)=1 -> 40; 25; 15; 15; 10 = 105, clamp **100** (adjustment -5) | 100.0, `clamp_adjustment` -5.0 |

Tier boundaries (icp_fit points, max 25): 200 -> tier1 25.0; 199 -> tier2 15.0; 50 -> tier2 15.0; 49 -> tier3 7.5; 20 -> tier3 7.5; 19 -> none 0; 2000 -> tier1 25.0; 2001 -> none 0; 0 and -5 -> none 0 (no exception); None -> unknown 7.5 (see M4).

Recency (15 max, `max_age_days` 45): age 0 -> 15; 1 -> 14.67; 22 -> 7.67; 45 -> 0; 46 -> 0 (floored); future-dated -> 15 (L1); no date -> 0; naive datetime -> TypeError (L2). Age uses whole days (`timedelta.days`), so a signal 44.9 days old counts as 44.

Extra-signal bonus: +0.10 per additional real signal, capped at 1.0 (tested in worker tests and S3). `SignalKind.NONE` ignored, reason "No trigger: ranked on fit only" (worker test, and seen in the 3-account run).

## Persona matching (current word-boundary rule vs titles)

Matches today: "CTO & Co-founder" -> secondary CTO; "Head of Product & Design" -> primary Head of Product; "Product Manager (Growth)", "Senior/Group/Principal Product Manager" -> secondary; "Digital Transformation Manager" -> secondary; "Group Head of Digital Workplace" -> **primary** "Head of Digital" (the intended secondary "Head of Digital Workplace" is shadowed because the primary list is checked first and the phrase is a word-boundary prefix; minor, but it promotes employee-facing digital roles to primary); "Director of Product Management" -> primary Director of Product; "Chief Product Officer", "VP Product", "Head of Product, ANZ" -> primary; "Director" alone and None/empty -> no match; "Director" never matches "CTO" (boundary rule works).

Misses (all returned None):

| Title | Why | Fix |
|---|---|---|
| Chief Technology Officer | config only has "CTO" | alias "chief technology officer" / "chief technical officer" -> CTO |
| VP, Product / VP of Product / Vice President, Product / Vice President of Product | comma, "of", spelled out | normalise: lower-case, replace `, & / ( ) -` runs with a space, drop "of" and "the", map "vice president" -> "vp" |
| Head of CX | abbreviation | alias "cx" -> "customer experience" |
| Customer Success Director / Product Director / Digital Director | word order | alias patterns "director of X" == "X director" (or add to config) |
| Head of Products / Product Managers | plural | allow optional trailing `s` on the last word |
| Chief Product & Technology Officer | compound | treat "chief product" prefix as CPO-equivalent, or add to config |
| Head of Customer Support | config has "Head of Support" | add title to config |
| Director of Customer Experience, VP Customer Success | config lacks these | add to config (Alex's call: "anyone Pendo helps") |
| Product Owner, Engineering Manager, CIO, Head of IT Operations as primary | not in config | Alex's call |

False positive: **"Director of Product-Led Growth"** matches "Director of Product" because `-` is a non-word character, so the lookahead `(?!\w)` passes. Use `(?![\w-])` after the phrase (and `(?<![\w-])` before). That also stops "Co-CTO" matching.

Word boundaries do not wrongly fail "CTO & Co-founder" or "(Growth)"; they only fail abbreviations, plurals, word order and reordered "of". Recommendation: keep the boundary match, add a small `_normalise(title)` (lower, punctuation to space, collapse spaces, drop "of"/"the") applied to both title and persona, plus an alias table in config (`personas.aliases`) so Alex can extend it without code changes. Also longest-match first within a level so "Head of Digital Workplace" is not shadowed by "Head of Digital".

## Item 4: accounts with no named prospect or phone

**Recommendation: (a), with (c) semantics kept for scoring.** Score all accounts as now (Alex's brief makes "named prospect with a phone" a scoring factor), then split after scoring:

- `RankResult.ranked`: **callable** accounts only (persona match + phone + not blocked), best first, daily cap applied to this list only, scheduled into windows. Only these ever become Nooks tasks.
- `RankResult.needs_prospect` (new): scored, best first, **not counted against the cap**, no window or call time, each with an explicit reason code: `no_prospects_on_account`, `no_persona_match`, `persona_match_no_phone` (best candidates for a lookup), kept distinct in `summarise()`.
- Still skipped with a reason: all prospects blocked, no timezone, invalid timezone.

Why: (b) skipping loses information Alex wants to see (a strong trigger with no contact is a prospecting task: find a named contact). (c) as built lets them eat the cap (H1) and leaves the builder guessing. Separate buckets let the 2c sheet print "Call tonight (10)" from `ranked` and "Strong triggers, no callable contact (top N)" from `needs_prospect`, without ever calling `createTask` for the latter. The caller must never be tempted to pick a non-callable prospect: `pick_prospects` already refuses.

Can the builder distinguish today? Only implicitly: `result.prospects[account_key]` is `[]`, or `reasons[-n]` contains "No callable persona with a phone". There is no boolean on `RankedAccount` (frozen model, coordinator's call) and no reason code. If the coordinator prefers not to change the result shape, the minimum is a `callable: dict[str, bool]` on `RankResult` plus counting the cap on callable accounts only.

## Item 5: cooldown contract (for verifying task 1c)

What the ranker implements (`src/ranker.py:154-155`, docstring lines 24-26):

- `cooldown_until: Mapping[str, datetime.date]`. Keys may be any of `sf_account_id`, `zoominfo_id`, `nooks_account_id` (matched against all three; unmatched keys silently ignored).
- Value = **the first calendar date on which the account may be called again** (inclusive).
- Active (penalty applied, reason "In cooldown") when `value > reference_date`; so `value == reference_date` is callable, `value == reference_date + 1` is not.
- Value must be a `date`, **not** a `datetime` (a `datetime` raises `TypeError`, H2).
- Reference date today = UTC date of `now`. **Wrong**: it should be the account-local date of the call window (`windows[0].start.date()`), see H2.
- It is a -30 penalty (clamped at score 0), not a skip (M1). `last_sheet_ids`: set of ids, same matching, same single -30 penalty (applied once if both).

1c should therefore produce `last_contact_date + cooldown_days` (e.g. a No Answer on Mon with `cooldown_days_no_answer: 3` -> Thu = first callable date) as a `date`, keyed by at least `sf_account_id`. The coordinator must confirm 1c's meaning of "N days" (first callable date vs last blocked date) and which timezone 1c uses to turn a call timestamp into a date (recommend the account-local date of the call, or Sydney as a single fixed zone).

Timezone of "today": tested. `now` = 2026-11-25 23:00 UTC is already 2026-11-26 in Sydney; `cooldown_until` 2026-11-26 -> penalised (wrong); 2026-11-25 -> not. Evening run example is in H2. The UTC date disagrees with the call date for essentially every realistic ANZ run time, so this is not an edge case.

## Windows (item 6)

At `now` = Wed 20:00 UTC: Sydney Thu 07:00 -> window Thu 08:45-11:30 +11:00; Perth Thu 04:00 -> Thu 08:45 +08:00; Auckland Thu 09:00 -> already inside, clamped to 09:00 +13:00; Brisbane Thu 06:00 -> 08:45 +10:00; Adelaide Thu 06:30 -> 08:45 +10:30. Each correct for its own zone.
- Friday 19:30 Sydney -> Mon 30 Nov 08:45 (morning) in 14-day search. PASS.
- DST: Fri 2 Oct 2026 -> Mon 5 Oct 08:45 **+11:00** (DST began Sun 4 Oct); Fri 2 Apr 2027 -> Mon 5 Apr 08:45 **+10:00** Sydney and **+12:00** Auckland (both ended Sun 4 Apr). PASS. (Public holidays not modelled, by design.)
- `now` exactly 08:45:00: morning, time 08:45:00 (start inclusive). `now` 11:29:59: morning, clamped to 11:29:59. `now` exactly 11:30:00: afternoon 13:30 (end exclusive). `now` 16:30:00: next day morning. PASS.
- Spread, 40 accounts one window, now before it: 40 distinct minutes, 08:45:00 to 11:25:33, none at or after the window end, no duplicate times; best-ranked gets the earliest slot. All guessed: 10:07:30 to 11:27:27, 40 distinct. PASS; see M5/L4 for the squeeze cases.
- Cap after skips: 60 accounts with 10 no-timezone -> 40 ranked, 10 "no timezone", 10 "below daily cap"; `summarise()` groups the cap reasons correctly. PASS.
- Guessed-tz rule: documented and deterministic; limits in L3.

## Other spec checks (all PASS)
- `sdr_target_boost` is a boost: a non-SDR account outranks an SDR account when it scores higher (worker test; also S1).
- Primary before secondary; blocked (do not call / opted out) and phone-less prospects are never picked; a mix of blocked and phone-less prospects does not skip the account, all-blocked does (`all prospects blocked ...`); same prospect indexed by sf and nooks ids is counted once.
- `now` must be timezone-aware (`ValueError`).
- Flagged group: -3 flat, reason "Non-ANZ billing, ANZ HQ (small penalty)".
- Honest reasons: "No trigger: ranked on fit only" and "Headcount unknown (neutral fit)" appear when true.

## Not verified
- Behaviour against real Salesforce/Nooks/ZoomInfo data (none used by design); real-world title variety beyond the 50-odd invented titles.
- Whether `state.py` (1c) and `signals.py` (1e) match the contracts above: those modules are not on this branch.
- Lint/unused-import tool results (pyflakes/ruff not installed).

## Suggested order of fixes
1. H1 (callable/needs_prospect split, cap on callable only). 2. H2 (call-date based cooldown, `date` coercion). 3. M1 decision, M3 guard. 4. M2 title normalisation plus config aliases. 5. M4 doc/code alignment, M5 scheduling capacity after Alex says how many calls per window.
