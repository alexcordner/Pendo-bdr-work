# Ranker notes (task 1f, `src/ranker.py`)

Contracts and decisions for 1g / 2c (sheet builder) and for 1c (state). Code is the source of truth; the module docstring has the scoring table.

## Result shape
`rank_accounts(...) -> RankResult`
- `ranked`: **callable accounts only** (persona-matching prospect with a phone, not opted out, not do-not-call). Best first. The daily cap (`limits.daily_call_cap`) applies to this list only. Each has `window` and `local_call_time`.
- `needs_prospect`: scored accounts with no callable prospect. Uncapped, best first, `window=None`, `local_call_time=None`. Reason code per account in `needs_prospect_codes[account_key]`: `no_prospects_on_account`, `no_persona_match` (also assistants, stale titles and other non-buyers), `persona_match_no_phone`, `junk_title` (the only persona matches are junk-shaped titles). The first line of `reasons` is `Needs a prospect: <code>`.
  **Nothing in `needs_prospect` may ever become a Nooks task.** It is a prospecting to-do list (find a named contact). The sheet builder prints it separately.
- `skipped`: excluded, with `skipped_reason`: `no timezone`, `invalid timezone '<tz>'`, `all prospects blocked ...`, `on last sheet`, `no call window in the next 14 days`, `below daily cap (rank N, cap C)`, `no call capacity in the next 14 days`.
- `by_window`: window name -> ranked accounts, best first (an account's window is where its slot falls).
- `prospects`: `account_key -> [chosen Prospect]` for ranked accounts (up to `limits.max_per_account_per_run`).

## Contracts for inputs
- `cooldown_until: Mapping[id, date]`: the **first date the account may be called again, inclusive**, as a calendar date in the account's local zone. Keys: sf, zoominfo or nooks id. Active while the value is later than the **account-local date of the account's own first usable day** (never the UTC date, never another zone's date); an active cooldown SKIPS the account (see Scoring). A `datetime` is converted with `.date()`; any other type raises `ValueError`. Cooldown is judged on the first usable day even if the account later spills.
  1c should therefore store `last_contact_date + cooldown_days` (a No Answer Mon with 3 days gives Thu).
- `last_sheet_ids`: set of ids. **Hard skip** (`on last sheet`), also for needs_prospect accounts. Active cooldown stays a penalty, because recently touched accounts are already dropped upstream by 1d.
- `signals`: keyed by sf_account_id or zoominfo_id; both are looked up, merged and de-duped by `Signal.source`. `SignalKind.NONE` is ignored. Naive `source_date` is treated as UTC. A future-dated signal gets full recency (1e should not emit them).

## Scoring
Max raw is 105, so the clamp can bite (`clamp_adjustment` records it). **Unknown headcount scores 0.3 x 25 = 7.5 icp_fit points**: a deliberate neutral, the same as tier 3, not zero (the shared docs now say so too). A known headcount outside every tier (e.g. 19) scores 0, below an unknown one.
- **One definition of the trigger.** `trigger_strength` points = weight x `signals.combined_strength(signals)`; the best signal (reason text, `RankedAccount.signals[0]`) is `signals.best_signal`, so an intent signal is never best while a dated real signal exists. The ranker has no combination formula of its own.
- **Recency horizon is per kind (N5).** recency = weight x (1 - age / `signals.max_age_for(best signal's kind, config)`), floored at 0 (funding 45 days, exec move / new hire 90, partnership 30, ...); `config.signals.max_age_days` (45) is only the fallback for a kind with no entry. An exec move 60 days old therefore keeps 5 of its 15 recency points.
- **One age function.** Reason text and recency use `signals.age_days(published, call_date)`, where `call_date` is the account-LOCAL date of its first usable day (same `today` semantics as the signals log, so a scoop reads the same number in both). Perth, Sydney and Auckland at one instant can differ only if their local dates differ.
- **Confidence ceiling.** `Account.confidence == "low"` caps the score at `LOW_CONFIDENCE_CEILING` (60), applied before the 0-100 clamp; `confidence_ceiling_adjustment` appears in the breakdown only when the cap bites. Low-confidence accounts say so in `reasons`, and every `Account.flags` entry is shown as `Flag: <flag> - <hint>` (`noise_location` -> "check this is a real company", others "check before calling"). Flags alone do not change the score. (In practice zoominfo-only / nooks-only accounts have no timezone and are skipped anyway; the ceiling is the safety net.)
- **Cooldown is a SKIP, not a penalty (N2).** accounts gets one `call_date` (the first zone's first usable day), but each zone has its own: on a Thu 16:28 Sydney run Sydney calls Friday while Perth still calls Thursday, so a Perth account whose cooldown ends Friday passes accounts. The ranker therefore judges `cooldown_until` against the account's OWN zone call date: active (value later than that date) means the account goes to `skipped` with `In cooldown until <date> (account-local)` (needs_prospect accounts too), never ranked, never a task. The old -30 `cooldown_penalty` component was DELETED (no path can reach it any more); `signals.weights.cooldown_penalty` stays in the config but this module does not use it.

## Persona matching
Tokens, not substrings: lower case, `, & / ( ) . : ;` become spaces, the words `of / the / and` are dropped, plural `s` allowed on the last word, hyphens stay inside a word (so `Director of Product-Led Growth` does not match `Director of Product`, `Co-CTO` does not match `CTO`). `ALIASES` (module constant) lists equivalent titles: CTO / Chief Technology Officer, CIO, CPO, VP Product / Vice President Product, Head of CX, Customer Success Director variants, Head of (Customer) Support, Director of Product / Product Director, Head of Digital / Digital Director. A config persona that appears in an alias group also matches the others. The match with the most words wins (so `Group Head of Digital Workplace` hits the secondary `Head of Digital Workplace`, not the primary `Head of Digital`); ties go primary, then config order (seniority). Not covered: compound titles like `Chief Product & Technology Officer`, `Product Owner`, `Engineering Manager`, CIO (alias exists but CIO is not a configured persona; Alex's call).

## Scheduling (N1-N3)
- **First usable day** per timezone = first working day with a window that still has at least 3 minutes (`MIN_SLOT_MINUTES`) after `earliest` = now rounded UP to a whole minute + 1 minute lead (`LEAD_MINUTES`; 09:30:20 gives 09:32, 09:30:00 gives 09:31). Its local date is the account's **call date**, used for cooldown. Zones can legitimately have different first days (Thu 16:28 Sydney is Friday for Sydney but still Thursday for Perth).
- Callable accounts, best first, take slots on the first usable day, spread evenly over all its windows (gap = total seconds // n, never under 3 minutes), best earliest. Guessed and confirmed zones share one timeline (the "guessed calls later" rule was dropped: it did not protect Perth).
- When a zone's first usable day is full, the account is **spilled**: held for a later working day, in `RankResult.spilled` with its intended later `window` / `local_call_time` and reason `Spilled to Mon 30 Nov (...)`. Spilled means pushed past the first usable day because it was full, never just "next working day".
- Spilled accounts do **not** use a daily-cap slot: the next-ranked account that fits takes it. The cap counts only `ranked`.
- **Sheet builder contract:** tasks are written ONLY for `ranked`. `spilled` and `needs_prospect` never become tasks and are never recorded in sheet history / `last_sheet_ids` (the next run will pick the spilled ones up again on its own). Public holidays are not modelled.

## Titles
Stale titles never match: `Former ...`, `Ex-...`, `Ex ...`, `Past ...`, `Interim/Acting former ...`. `Chief Product & Technology Officer` (and the "and" form) is in the CPO alias group. Open (not configured, Alex's call): Product Owner, Engineering Manager, CIO as a persona.

## Not covered / open
- How many calls per window Alex really makes (the 3-minute minimum is a default, constant `MIN_SLOT_MINUTES`).
- Weights and factors are defaults, not confirmed by Alex.
- Not tested against real 1c/1d/1e output.

## Id keys
Every lookup key (account sf/zoominfo/nooks id, `signals` keys, `cooldown_until` keys, `last_sheet_ids` members, prospect `sf_account_id` / `nooks_account_id` joins, `account_key`) goes through `src.loaders.normalise_sf_id`, so an 18-char Salesforce id finds a 15-char key and the reverse. Case is never folded; FAKE- and other non-SF ids pass through. If two spellings of one id appear in `signals` their lists are merged; in `cooldown_until` the later date wins. `RankResult.prospects` / `needs_prospect_codes` are keyed by the normalised `account_key`.

## Run log
`summarise(result, top_n=15, list_limit=25)` prints counts per bucket and skip reason, the top `top_n` ranked accounts, then NAMES every skipped, needs-prospect and spilled account with its reason (up to `list_limit` each, plus the total and "... and N more"), so the run log always says what was skipped and why.

## Suggested wording for docs/interfaces.md (persona matching, decision 10)
"Persona matching is owned by 1f (`src/ranker.py`): whole-word, case-insensitive matching of `config.personas` titles after normalisation (punctuation and the words of/the/and dropped, plural s allowed on the last word, hyphenated words stay whole), with an `ALIASES` table for equivalent titles (CTO / Chief Technology Officer, VP Product / Vice President Product, Head of CX, ...); stale titles (Former / Ex- / Past) never match; the match with the most words wins, then primary before secondary, then config order."

## Title hygiene (found by the first real dry run)
A ~120-character "Executive Assistant To Chief Financial Officer, Chief Technology Officer And Chief Risk Officer" matched the CTO persona and took rank #2. Titles are now cleaned BEFORE persona matching (`_title_class` -> ok / junk / non_buyer / none):
1. **Non-buyers never match:** titles containing assistant, personal / executive assistant, PA to, EA to, secretary, coordinator, administrator, intern, graduate, trainee; "to [the] <C-level>" (Chief of Staff to the CTO); titles starting Former / Ex- / Past / Interim former / Acting former. "reporting to the CTO" is NOT a "to the" title and the text after "reporting to" is ignored for matching ("Interim Chief Digital Officer" still matches). Whole words only (Internal is not Intern).
2. **Junk shape never matches:** more than `MAX_TITLE_CHARS` (80) characters, or more than `MAX_ROLE_PHRASES` (2) comma / slash / semicolon / "and" / "&" separated parts that contain a role word (head, director, chief, vp, manager, officer, lead, CxO acronyms, ...). "VP, Product & Design" is one role, so it is fine. Junk is never chosen, even over nothing; if the account's only persona matches are junk it goes to `needs_prospect` with `junk_title` (a clean title without a phone gives `persona_match_no_phone` instead).
3. **Head of the title wins:** a match that starts in the first segment (before the first comma, semicolon, slash, spaced dash or bracket) beats one buried in a later segment; then most words, primary before secondary, config order, name. Across prospects the order is head-match first, then level, then position. Example: "Head of Product, Digital" beats "Strategy Consultant, Head of Product Programs".
4. Functions that only sound like a buyer ("Product Manager - Pathology") still match: role ambiguity is handled in signals, not here. "Program Director Digital Transformation" matches Head of Digital through the alias "director digital" (documented, a loose match).
Open: titles that mention buyers only inside long free text still depend on the length / role-count limits; "Assistant Director of Product" is rejected (any "assistant").

