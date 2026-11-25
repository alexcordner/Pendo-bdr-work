# state.py notes (task 1c, after review)

Everything here is offline. State is one gitignored file, `state/state.json` (schema version 2), written atomically.

## What the nightly runner must do
1. `st = load_state(cfg.paths.state_dir)`.
2. **Live runs:** call `require_healthy(st, rebuilt_from_history=False)`. It raises `StateResetError` if `st.was_reset` (file unreadable, truncated, wrong version or wrong container type). In that case the runner must NOT write Nooks tasks or Gmail drafts. It may only continue after re-fetching the FULL Nooks call history, running `record_calls` over all of it (rebuilds Do Not Call), and then calling `require_healthy(st, rebuilt_from_history=True)`. Dry runs may continue and just log `st.reset_reason`.
3. A missing file is a first run (`st.is_new`), not a reset. If a previous run existed, a missing file also means lost DNC; the runner may want to treat "no file but sheets expected" the same way.
4. Run `verify_dispositions(config, index)`; stop if the list is non-empty.
5. `record_calls`, `record_sent_emails`, `record_sheet`, then `save_state`. Log `st.report`.
6. The bad file is moved to `state.json.corrupt-N` (ignored by git). Never commit it.

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
