# Ranker notes (task 1f, `src/ranker.py`)

Contracts and decisions for 1g / 2c (sheet builder) and for 1c (state). Code is the source of truth; the module docstring has the scoring table.

## Result shape
`rank_accounts(...) -> RankResult`
- `ranked`: **callable accounts only** (persona-matching prospect with a phone, not opted out, not do-not-call). Best first. The daily cap (`limits.daily_call_cap`) applies to this list only. Each has `window` and `local_call_time`.
- `needs_prospect`: scored accounts with no callable prospect. Uncapped, best first, `window=None`, `local_call_time=None`. Reason code per account in `needs_prospect_codes[account_key]`: `no_prospects_on_account`, `no_persona_match`, `persona_match_no_phone`. The first line of `reasons` is `Needs a prospect: <code>`.
  **Nothing in `needs_prospect` may ever become a Nooks task.** It is a prospecting to-do list (find a named contact). The sheet builder prints it separately.
- `skipped`: excluded, with `skipped_reason`: `no timezone`, `invalid timezone '<tz>'`, `all prospects blocked ...`, `on last sheet`, `no call window in the next 14 days`, `below daily cap (rank N, cap C)`, `no call capacity in the next 14 days`.
- `by_window`: window name -> ranked accounts, best first (an account's window is where its slot falls).
- `prospects`: `account_key -> [chosen Prospect]` for ranked accounts (up to `limits.max_per_account_per_run`).

## Contracts for inputs
- `cooldown_until: Mapping[id, date]`: the **first date the account may be called again, inclusive**, as a calendar date in the account's local zone. Keys: sf, zoominfo or nooks id. Active (-30) while the value is later than the **account-local date of the first open call window** (never the UTC date). A `datetime` is converted with `.date()`; any other type raises `ValueError`. Cooldown is judged on the first open day even if the account spills to a later day.
  1c should therefore store `last_contact_date + cooldown_days` (a No Answer Mon with 3 days gives Thu).
- `last_sheet_ids`: set of ids. **Hard skip** (`on last sheet`), also for needs_prospect accounts. Active cooldown stays a penalty, because recently touched accounts are already dropped upstream by 1d.
- `signals`: keyed by sf_account_id or zoominfo_id; both are looked up, merged and de-duped by `Signal.source`. `SignalKind.NONE` is ignored. Naive `source_date` is treated as UTC. A future-dated signal gets full recency (1e should not emit them).

## Scoring
Max raw is 105, so the clamp can bite (`clamp_adjustment` records it). **Unknown headcount scores 0.3 x 25 = 7.5 icp_fit points** (a deliberate neutral, same as tier 3), not zero. `docs/interfaces.md` decision 3 and the `Account.size_unknown` docstring currently say "no icp_fit points": they are out of date (coordinator to fix). A known headcount outside every tier (e.g. 19) scores 0, below an unknown one. `Account.flags` / `confidence` do not affect the score.

## Persona matching
Tokens, not substrings: lower case, `, & / ( ) . : ;` become spaces, the words `of / the / and` are dropped, plural `s` allowed on the last word, hyphens stay inside a word (so `Director of Product-Led Growth` does not match `Director of Product`, `Co-CTO` does not match `CTO`). `ALIASES` (module constant) lists equivalent titles: CTO / Chief Technology Officer, CIO, CPO, VP Product / Vice President Product, Head of CX, Customer Success Director variants, Head of (Customer) Support, Director of Product / Product Director, Head of Digital / Digital Director. A config persona that appears in an alias group also matches the others. The match with the most words wins (so `Group Head of Digital Workplace` hits the secondary `Head of Digital Workplace`, not the primary `Head of Digital`); ties go primary, then config order (seniority). Not covered: compound titles like `Chief Product & Technology Officer`, `Product Owner`, `Engineering Manager`, CIO (alias exists but CIO is not a configured persona; Alex's call).

## Scheduling
Per timezone: one timeline of all call windows of the first working day that still has time ahead (a window under way starts at `now`; a window with under 3 minutes left is unused). Callable accounts, best first, are spread evenly over that timeline (gap = total seconds // n, never under 3 minutes), best earliest. A day holds at most sum(floor(minutes/3)) accounts; the rest spill to the next working day (reason `Spilled to Mon 30 Nov (call windows full)`). Guessed and confirmed timezones share one timeline. The earlier "guessed zone calls in the later half" rule was dropped: it did not protect Perth (its window opens later than half of Sydney's) and made two groups share minutes. Public holidays are not modelled.

## Not covered / open
- How many calls per window Alex really makes (the 3-minute minimum is a default, constant `MIN_SLOT_MINUTES`).
- Weights and factors are defaults, not confirmed by Alex.
- Not tested against real 1c/1d/1e output.
