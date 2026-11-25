# state.py notes (task 1c, after review)

Everything here is offline. State is one gitignored file, `state/state.json` (schema version 2), written atomically.

## Runner contract (Do Not Call gate)
Do Not Call lives only in `state.json` plus the Nooks call history. A new, deleted, corrupt or reset state has an EMPTY Do Not Call list, so it must not be trusted for live writes until it has been rebuilt from the FULL call history. The proof is a marker stored IN the file (`dnc_rebuilt_at` date, `dnc_rebuilt_calls` count, `dnc_rebuilt_earliest` = earliest call date seen, `dnc_rebuilt_empty` for a confirmed empty history). It is set only by `mark_dnc_rebuilt`; a fresh, reset or missing-file State starts without it, and saving an empty State does not set it, so a dry run cannot launder it.

Nightly:
1. `st = load_state(cfg.paths.state_dir)` (never raises; check `st.was_reset`, `st.reset_reason`, `st.is_new`, `st.load_warnings` for the log).
2. `verify_dispositions(config, index)`; stop if the list is non-empty.
3. If `not dnc_is_rebuilt(st)`: page `listCalls` to the very end, call `record_calls` on each page (pass the same prospects), add up `st.report["record_calls"]["calls_seen"]` over the pages, then `st = mark_dnc_rebuilt(st, run_date, total_calls_seen, reached_last_page=True)` and `save_state`. `reached_last_page` must come from the adapter's `hasMore == false`; `False` is rejected, and so is a `calls_seen` smaller than the distinct calls already recorded. A rebuild from 0 calls is rejected unless the caller passes `empty_history_confirmed=True` (a brand-new Nooks seat) together with `reached_last_page=True`. This also applies to the very first run.
4. Live run: `require_healthy(st, live=True)` raises `StateResetError` unless the marker is set. The runner must refuse to write tasks or drafts when it raises. Dry run: `require_healthy(st, live=False)` only logs a warning.
5. Normal night: `record_calls` (recent calls), `record_sent_emails(..., today=run_date)`, `record_sheet`, `save_state`. Log `st.report`.
6. The bad file is moved to `state.json.corrupt-N` (ignored by git). Never commit it.

`rebuilt_from_history` no longer exists (it was an honour-system boolean).

## Sheet builder must apply state first
The ranker's `is_blocked` only reads `prospect.do_not_call` and `opted_out_email`; it knows nothing about state. The sheet builder MUST call `mark_do_not_call(prospects, state)` before ranking, which sets `do_not_call` from the stored id, phone and (hashed) email blocks.

## Salesforce ids
`normalise_sf_id` (15-char canonical form, `FAKE-` ids unchanged) is applied at every API boundary and on load: `record_calls`/`accounts_by_id` keys, prospect account ids, `record_sheet`, `last_sheet_ids`, `was_in_last_sheet`, `is_in_cooldown`, `record_sent_emails`. Keys in `cooldown_until` / `last_touched` / `cooldown_until_by` results are canonical (15-char); look them up with `normalise_sf_id(id)` or any 15/18-char form via the state functions. Old files with mixed forms are merged on load.

## Malformed entries on load
A malformed stored `last_*_date` falls back to the UTC date of the stored instant (with a warning), so a cooldown is not silently dropped. Other skipped entries (accounts with unusable ids, prospects, email messages, sheets) are listed in `load_warnings` and counted in one summary warning. A skipped sheet can serve an account twice in a row; a skipped email loses its history. Malformed Do Not Call data resets the whole file instead.

## Days are account-local
- A call or email day is computed in `Account.timezone` (from `accounts_by_id`), default `Australia/Sydney` when unknown, None or invalid. Timestamps stay UTC; the local DATE is stored beside them.
- `cooldown_until` values are dates: the first local date the account may be called again (inclusive). In cooldown while `until > today`. No-answer = last call date + 3, connect = connect date + 14 (later wins).
- `is_in_cooldown(..., today, ...)` and `emailed_recently(..., today, ...)` need the account-LOCAL today. The ranker should pass `windows[0].start.date()` in the account's zone.
- `last_touched` returns the last touch date (not a cooldown end) and INCLUDES dated emails, while emails do not start a call cooldown. A caller that passes `last_touched` to 1d as `touched` will therefore treat an email-only account differently from one with call history (1g M1). Prefer passing only `cooldown_until` and letting `emailed_recently` handle email.
- Lookups by other ids: `cooldown_until_by(state, config, "nooks_account_id" | "zoominfo_id")`.

## Do Not Call (fail closed)
- Any past call whose disposition name normalises to the configured DNC name (case, punctuation, NBSP, repeated spaces ignored) sets: the prospect id, the canonical phone (via `normalise_phone`), and a hash of the prospect's normalised email. A duplicate Nooks prospect with the same email, or any prospect on that number, stays blocked (a shared switchboard number blocks everyone on it; the reason string says which phone).
- DNC is sticky and never a "connect".
- Emails are stored only as sha256 hashes (`dnc_emails`, `emails` keys); no raw addresses at rest.
- Undated sent emails are recent until `first_seen + email_cooldown_days` (first_seen = run date they were first recorded).

## Known gaps (not fixed)
- Last writer wins if two runs save at once (F6): the nightly job is single.
- A DNC call whose disposition id cannot be resolved to a name cannot be recognised; `verify_dispositions` is the only guard.
- A DNC learned for a prospect whose email is not in the list at that time contributes its email only once the prospect appears in a later `record_calls`.

## Added after the 1g integration review
- **Same-night reruns.** `last_sheet_ids(state, run_date)` and `was_in_last_sheet(state, id, run_date)` take a required `run_date` and only look at sheets recorded for an EARLIER date, so a second run on the same night is stable. `record_sheet` for a date replaces that date's sheet. A dry run must never call `record_sheet`.
- **Save guard.** `save_state(state, dir, allow_shrink=False)` raises `StateShrinkError` (nothing written) if the file on disk has more Do Not Call ids/phones/emails or accounts than the new State, or has a rebuild marker the new State lacks. Before every overwrite of a readable file it keeps one rotating backup, `state.json.prev` (git-ignored). An unreadable file on disk is not compared.
- **Fallback dates** for a malformed stored date use the account's stored zone (`AccountState.tz`, set when `accounts_by_id` carried a valid timezone), else Australia/Sydney.
- **Run counts** are saved under `last_run` (counts only, no ids): `record_calls`, `record_sent_emails`, `record_sheet`. After `load_state` read `st.last_run` for last night's numbers; `st.report` holds this run's.
- **Performance.** `record_calls` copies the State once per call with a field-level copy (no deepcopy). 20,000 calls in pages of 1,000 take about 0.5 s. Pass all calls at once or pages of 1,000 or more.
- **None ids.** Accounts with no usable id are counted (`record_sheet` `dropped_no_id`, `record_calls` `no_account`, `record_sent_emails` `no_account`) and logged as a warning. They still cannot be hard-skipped or cooled down because everything is keyed by Salesforce id.
- Salesforce ids are normalised on every function that takes one: 15- and 18-character forms reach the same account.
