# callsheet.py notes (task 2c)

Offline, deterministic, no LLM, no I/O. Fixture: `tests/fixtures/callsheet_accounts.json` (synthetic).

## API
- `build_sheet(rank_result, accounts, signals_by_account, prospects, config, now, *, limit=None, history=None) -> Sheet`
  - Only `rank_result.ranked` can become lines. `limit` = top N by score, THEN ordered by call time.
  - Call order: lines grouped by (timezone, window, local date); groups ordered by their earliest instant; lines inside a group by instant. `Sheet.groups` / `Sheet.lines`.
  - `SheetLine` wraps the shared `SheetEntry` (`.entry`) plus `rank` (position by score, 1 = best), `call_no`, `group`, `run_date`, `why_chosen`, `flags`, `packet`.
  - `RankedAccount.signals` is the source of truth for signals (the score came from it); `signals_by_account` is a fallback only when it is empty. `accounts`/`prospects` are fallbacks (prospect lookup when RankResult has none).
  - Ranked accounts that cannot be sheeted (skipped_reason set, no time, no prospect) go to `Sheet.dropped` with the reason, never silently.
  - Buckets (`needs_prospect`, `spilled`, `skipped`, `dropped`) list names + reasons, 25 per bucket with "and N more"; `skipped_reason_counts` groups the reasons.
  - `run_date` = `now` in Australia/Sydney (used in the task tag).
- `ContactHistory(last_call_date, last_outcome, calls_total, source)`: the caller fills it (`history={account_key: ContactHistory}`).
- Renderers: `render_sheet_text`, `render_sheet_markdown`, `sheet_to_json`, `render_packet_text`. None of them or the packet ever contain an email or phone; contact first name only (surname never printed).
- `prepare_task(line, resolved_prospect, state, opener_fields, owner_id, *, dry_run=False, now=None) -> dict` is the ONLY way to build the task-create `data` (`{action:'call', dueAt, ownerId, prospectId, priority, note}`); there is no public unchecked builder. In order it: validates `owner_id` (required, `[A-Za-z0-9_-]{1,128}`; `OWNER_ID_PLACEHOLDER` only with `dry_run=True`); refuses non-SheetLines (needs_prospect / spilled / skipped are never SheetLines) and skipped/unscheduled lines; strips the Nooks id and normalises SF ids (`normalise_sf_id`, 15 = 18 char) before any comparison; requires the resolved Nooks id (`[A-Za-z0-9_-]{1,128}`) AND both SF contact ids (sheet contact and resolved) to be present and equal (fail closed); runs `state.is_blocked(.., 'any')` on the resolved prospect AND the sheet contact and raises `BlockedError` (a `SheetError`); validates opener fields; builds with exactly the id it checked. `now` (aware), if given, refuses a due time already past. `dueAt` is the account-zone ISO time with offset.
- Opener field rules: every field must be a `str`; after removing zero-width/BOM and NBSP it needs an alphanumeric character and must not be none/n/a/null/nil/unknown/tbd/todo. OPENER and GATEKEEPER must be real text (no 'none verified' / 'None found'); FACT 1, FACT 2, TRIGGER may be the honest forms (`none verified`, `None found: cold call on ICP fit only`). An email or phone-like number in any field is refused. The finished note is capped at `NOTE_MAX_CHARS` = 2,000 (conservative, UNVERIFIED Nooks limit; refused, never truncated).
- Note layout: `[apac-callsheet <run_date> #<rank>]` then `OPENER:`, `FACT 1:`, `FACT 2:`, `GATEKEEPER:`, `TRIGGER:` lines (newlines inside a field are collapsed). The tag identifies tasks this tool created.
- Priority bands (Nooks enum low/normal/high/urgent): score >= 75 urgent, >= 55 high, >= 35 normal, else low (`PRIORITY_BANDS`).

## ResearchPacket fields
account_key, account_name, website, industry (only if `Account.industry` exists; it does not today), state, city, employees, tier, prospect_first_name, prospect_title, local_time, window, timezone, signals[kind, label, evidence, source, source_label, date, age_days, verdict_reason, ambiguous], has_real_trigger, ambiguity_flags, sdr_target, contact_history (+source), available_facts[text, source], fact_count, facts_note (set when fewer than two facts: never pad), must_not_claim.
Facts, in order: signals (source label e.g. `ZoomInfo scoop 2026-11-22`), headcount and location (`Salesforce account record`), industry, contact history (`Nooks call history`).

## Honesty
No signal gives "No trigger: ranked on fit only" and a packet with `has_real_trigger=False`. Intent is labelled "interest signal, not a dated event". Ambiguous roles show the `role ambiguous` flag.

## UNVERIFIED / notes
- Nooks `createTask` priority enum is from the tool schema (low, normal, high, urgent); band thresholds are a judgement call for Alex to tune.
- Task `prospectId` is expected to be a UUID in Nooks; fixtures use FAKE ids, so it is not validated here.
- `Account` has no industry field today; the packet leaves it None.

## Lazy Nooks prospect resolution (coordinator update)
Nooks prospects cannot be bulk-fetched, so in the Phase 2 dry run sheet contacts are SALESFORCE CONTACTS and `Prospect.nooks_id` is None. The sheet, packets and renderers work with `nooks_id` None and identify the contact by SF contact id + first name + title (JSON: `sf_contact_id`, `nooks_prospect_id`; packet: `prospect_sf_contact_id`). Phase 3 flow:
1. `sheet_needs_prospect_resolution(sheet)` -> SF contact ids (call order, de-duplicated) needing one `filter_crmId` lookup each. A line with neither id is flagged `no contact id`, not listed, and cannot produce a payload.
2. For each resolved Nooks prospect the caller MUST run `recheck_blocked(line, resolved_prospect, state)` -> `(ok, reason)` (wraps `state.is_blocked` on the RESOLVED prospect, channel 'any'; also refuses a missing Nooks id or a different SF contact id). Only when ok build `nooks_task_payload(line, fields, resolved.nooks_id)`.
3. `ContactHistory.matched_by` is `'phone'` (dry run) or `'nooks_prospect_id'` (after resolution); it appears in the fact source, e.g. `Nooks call history (matched by phone)`.

## Review fixes (2c review)
- **Scrubbing:** `_scrub` replaces email-like and phone-like text with `[redacted]` in every free-text field that reaches a packet, render, bucket or JSON (titles, account names, evidence, flags, reasons). Phone shapes: `+` numbers, AU/NZ landline/mobile shapes, any 9+ digit run (dates and plain numbers like 2025 2026 are untouched). Identifiers (SF ids) are not scrubbed. `SheetLine.entry` is `repr=False`, so `repr(sheet)` never shows the Prospect (email, phone, surname).
- **Signals:** `verdict_reason` is recomputed from the signal's date against `now`, the account-local call date and `signals.max_age_for(kind)`: statuses `ok | undated | intent | future | stale`. Future-dated and stale signals are flagged (`signal in the future`, `signal older than limit`), kept under `signals` but left out of `available_facts`, and never count as a trigger. `has_real_trigger` is True only for a usable non-intent signal (so intent-only is False, flag `no dated trigger`); `trigger_kind` names it.
- **Rendering:** `render_sheet_text` / `render_sheet_markdown` take `bucket_limit` (default 25; `None` lists every account: use it for the run log, CLAUDE.md rule 5). Markdown escapes `\ \` | # * _ [ ] < >`, collapses newlines and shortens long text.
- **First names:** honorifics (Dr, Mr, Ms, Prof...) are stripped, `Surname, First` is handled; a lone token, an initial or anything unsure gives None and the sheet says "the contact" (never a surname).
- **Misc:** a chosen prospect with `do_not_call` / `opted_out_email` is dropped (defence in depth). `rank` is the position among ALL ranked accounts (dropped ones included), so tags can skip a number; the Phase 3 de-dupe must parse the FIRST note line only. `run_date` is Sydney's date and is only a label.
- **Not changed:** `_signals_for` is still a local copy of the ranker's helper (tie-break only, no divergence seen); a hand-built `SheetLine` is not detectable beyond its fields; a `payloads_for(sheet, ...)` batch builder is left to Phase 3.
