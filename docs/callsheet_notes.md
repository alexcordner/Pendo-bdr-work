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
- `nooks_task_payload(line, opener_fields, *, owner_id=OWNER_ID_PLACEHOLDER) -> dict` builds the createTask `data` only: `{action:'call', dueAt, ownerId, prospectId, priority, note}`. `dueAt` is the account-zone ISO time with offset (DST-correct). Refuses (`SheetError`) on any empty/missing opener field (`opener, fact_1, fact_2, gatekeeper, trigger`), on non-SheetLine input (needs_prospect / spilled / skipped accounts are never SheetLines), a skipped/unscheduled ranked account, or a missing Nooks prospect id.
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
