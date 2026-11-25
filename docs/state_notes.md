# state.py notes (task 1c, after review)

Everything here is offline. State is one gitignored file, `state/state.json` (schema version 2), written atomically.

## Runner contract (Do Not Call gate)
Do Not Call lives only in `state.json` plus the Nooks call history. A new, deleted, corrupt or reset state has an EMPTY Do Not Call list, so it must not be trusted for live writes until it has been rebuilt from the FULL call history. The proof is a marker stored IN the file (`dnc_rebuilt_at` date, `dnc_rebuilt_calls` count). It is set only by `mark_dnc_rebuilt`; a fresh, reset or missing-file State starts without it, and saving an empty State does not set it, so a dry run cannot launder it.

Nightly:
1. `st = load_state(cfg.paths.state_dir)` (never raises; check `st.was_reset`, `st.reset_reason`, `st.is_new`, `st.load_warnings` for the log).
2. `verify_dispositions(config, index)`; stop if the list is non-empty.
3. If `not dnc_is_rebuilt(st)`: page `listCalls` to the very end, call `record_calls` on each page (pass the same prospects), add up `st.report["record_calls"]["calls_seen"]` over the pages, then `st = mark_dnc_rebuilt(st, run_date, total_calls_seen)` and `save_state`. `mark_dnc_rebuilt` rejects 0 calls. This also applies to the very first run.
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
- `last_touched` returns the last touch date (not a cooldown end).
- Lookups by other ids: `cooldown_until_by(state, config, "nooks_account_id" | "zoominfo_id")`.

## Do Not Call (fail closed)
- Any past call whose disposition name normalises to the configured DNC name (case, punctuation, NBSP, repeated spaces ignored) sets: the prospect id, the canonical phone (via `normalise_phone`), and a hash of the prospect's normalised email. A duplicate Nooks prospect with the same email, or any prospect on that number, stays blocked (a shared switchboard number blocks everyone on it; the reason string says which phone).
- DNC is sticky and never a "connect".
- Emails are stored only as sha256 hashes (`dnc_emails`, `emails` keys); no raw addresses at rest.
- Undated sent emails are recent until `first_seen + email_cooldown_days` (first_seen = run date they were first recorded).

## Known gaps (not fixed)
- Last writer wins if two runs save at once (F6): the nightly job is single.
- Plus-addressing (`a+x@` vs `a@`) is not matched; `normalise_email_for_match` is in loaders (shared file) (F10).
- A DNC call whose disposition id cannot be resolved to a name cannot be recognised; `verify_dispositions` is the only guard.
- A DNC learned for a prospect whose email is not in the list at that time contributes its email only once the prospect appears in a later `record_calls`.
