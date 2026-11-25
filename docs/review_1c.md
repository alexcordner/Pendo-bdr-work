# Review of task 1c: `src/state.py` (reviewer pass, 2026-11-25)

**Verdict: PASS WITH FIXES.** The core design is sound and every DNC path I tried fails closed except the items listed below. Nothing here needs a redesign, but fixes 1 to 4 should land before state is wired into a live run.

Scope and honesty: offline, read-only, no connector calls, invented data only. Reviewed `src/state.py` (537 lines) and the fixture at the commit on `phase1/1c-state` (`2e403cc`). I did not modify the worker's files. I ran my own attack script (kept in the scratchpad, not committed). **I could not see `src/ranker.py` (task 1f is parallel and not on this branch), so the ranker contract below is checked against the brief's wording only, not the ranker code.** `tests/test_state.py` was run but not audited line by line; the "UNVERIFIED" items are listed at the end.

## 1. Test runs and determinism: PASS
- `pytest tests/ -q`: 227 passed. `pytest tests/ -q -W error`: 227 passed. `tests/test_state.py` alone: 85 passed. Python 3.13.
- Replaying the same 4 calls 3 times, 50 random shuffles, and three save/load round trips between runs all give an identical `state_to_dict`. (Compare via `state_to_dict`, not `==`: `State.report` is a dataclass field and legitimately differs between a first run and a replay.)
- Inputs are untouched (deep-compared after `record_calls`, `record_sheet`, `record_sent_emails`). Every `record_*` returns a new State.
- **Exception (F5):** two calls with the same `started_at` and different outcomes are order dependent.

## 2. Do Not Call attack results (safety-critical)

| # | Attack | Result |
|---|---|---|
| a | DNC call, `prospect_id=None`, `to_e164` matches exactly one prospect | **Blocked** (`dnc_prospect_ids` gets that prospect, reason "past call dispositioned Do Not Call") |
| b | Same phone on two prospects, unlinked DNC call | **Both blocked** via `dnc_phones` (`state.py:386`). The worker does not guess a person, but the number block still covers both. This is **fail closed**, which I recommend keeping. Cost: a shared switchboard/main number DNC blocks everyone on it. The block reason names the phone, so it is explainable. |
| b2 | Linked DNC call, second prospect shares the number | Second prospect also blocked (same mechanism). Same recommendation. |
| c | Casing / surrounding whitespace / trailing newline | `do not call`, `  DO NOT CALL `, `Do Not Call\n`: blocked |
| c2 | Trailing punctuation, double space, NBSP, hyphens, suffix (`Do Not Call.`, `Do  Not  Call`, `Do Not Call`, `Do-Not-Call`, `Do Not Call (DNC)`) | **NOT matched, fails open** (F3). `_norm` only strips and casefolds (`state.py:236`). Mitigated because the names come from the Nooks list and `verify_dispositions` checks the exact configured name, but a renamed or oddly rendered disposition would silently stop recording DNC. |
| d | DNC then a later 300s "Connect" call | **Still blocked** (sticky set, never removed) |
| e | `disposition_name=None` | No crash, no DNC recorded (cannot recognise what we cannot name). Covered only by `verify_dispositions` plus the caller's stop. |
| f | Channels | DNC blocks `call`, `email`, `any`. Email opt-out blocks `email` and `any` but not `call`. Correct. |
| g | Unlinked DNC call with `to_e164="+61 400 000 010"` (spaced) | Stored as-is (`dnc_phones={'+61 400 000 010'}`) and does **not** block the canonical `+61400000010` (F4). Real loader output is already E.164, so this needs a caller that bypasses `call_from_nooks`. Defence in depth: normalise inside state. |
| h | Prospect with `nooks_id=None` and DNC phone | Blocked by phone. `nooks_id=None` and `phone=None`: not blocked (nothing to match on; see F2). |
| i | DNC for a prospect id not in the supplied prospect list | Stored and sticky |
| j | Same person as a duplicate Nooks prospect (different id and phone, same email) | **Not blocked** (F2) |
| k | DNC memory lost (corrupt/truncated state file) | **Run continues with an empty DNC list** (F1) |
| l | Two writers (load, load, save A, save B) | A's DNC is lost (last writer wins). See F6. |

**Paths where a blocked person can be returned as callable:** (1) the state file is corrupt/truncated and the run proceeds with an empty State and no DNC unless Nooks call history is refetched in full; (2) the same person exists as a second Nooks prospect with a different id and phone; (3) disposition spelling variants or a renamed disposition; (4) a changed phone number for a prospect known only via the old number; (5) lost update from concurrent runs. None needs an exotic input; (1) and (2) are the realistic ones.

## 3. Cooldown arithmetic

All dates are the UTC date of `started_at`. "Until" = first callable date (`state.cooldown_until`), in cooldown while `today < until`.

| Scenario | until (first callable day) |
|---|---|
| No answer Mon 2026-11-02 | 2026-11-05 (11-04 in cooldown, 11-05 not) |
| Connect 2026-11-02 | 2026-11-16 |
| Connect 11-02, later no answer 11-05 | 2026-11-16 (14 days from connect wins) |
| Connect 11-02, later no answer 11-14 | 2026-11-17 (later of 11-16 and 11-14+3) |
| No answer 11-02, later connect 11-03 | 2026-11-17 |
| Connect 11-02, later voicemail 11-03 | 2026-11-16 |
| No answer 2026-11-29 (month boundary) | 2026-12-02 |
| No answer 2026-12-30 (year boundary) | 2027-01-02 |
| No answer 2028-02-27 (leap) | 2028-03-01 |
| Connect 2028-02-20 (leap) | 2028-03-05 |
| Future-dated no answer 2027-01-01 | 2027-01-04 (accepted as is; fine, harmless) |
| None / unknown account id | not in cooldown |

Arithmetic is correct in every case. Semantics question for Alex: "3 days" currently means a Monday no-answer is callable again on Thursday (`until = day + 3`). If he means "not within the last 3 days" that is the same thing; if he means "skip Tue, Wed, Thu", it is one day off.

### Timezone assessment (matters, recommend fixing)
Measured with `zoneinfo`:
- Sydney, local 08:45 to ~10:59 on 2026-11-02 (AEDT, UTC+11) is the **previous** UTC date, so until = 11-04 instead of 11-05. Calls at 11:30 or later land on the correct UTC date.
- Auckland (NZDT, UTC+13): everything before 13:00 local, i.e. the whole morning window (08:45 to 11:30), is the previous UTC date.
- Outside daylight saving (AEST UTC+10, NZST UTC+12) the same shift happens before 10:00 / 12:00.
- Effect: a call in the **morning window** gets a cooldown that is **one calendar day short** (no-answer effectively 2 days, connect 13). Afternoon calls are exact. Evening calls (23:30 Sydney is 12:30 UTC same date) are not affected.
- `today` has the same problem in the opposite direction: whatever date the caller passes (UTC today vs Sydney today) shifts the answer by a day for 10 to 13 hours of every day.

**Recommendation:** keep timestamps in UTC in the file, but compute "day" in the account's timezone: `cooldown_until(state, config, tz_by_account: dict[str, str] | None = None)` and `last_touched(...)` the same, and have the caller pass the account-local `today` to `is_in_cooldown`. Default to `Australia/Sydney` when the account timezone is unknown (never UTC). Without this, a morning no-answer comes back one day early. Severity: medium (over-calling by one day, not a safety issue).

### Contract with ranker (task 1f)
Per the brief, the ranker reads `cooldown_until[key]` as "first date callable again; active while value > today". **Confirmed identical on the state side:** `cooldown_until()` returns the first callable date (`state.py:420-437`) and `is_in_cooldown` is `today < until` (`state.py:445`), i.e. active exactly while `until > today`. At `today == until` the account is callable in both. I did not read the ranker code, so I cannot confirm its side. Also note `last_touched()` returns the **last touch date** (not a first-callable date), and includes dated emails; the ranker should not treat it as a cooldown end. The ranker should use the same timezone rule for `today`.

## 4. Connect classification (`call_outcome`, `state.py:240`)

| Disposition / duration | Outcome |
|---|---|
| No Answer 19s, 20s, 21s | no_contact, no_contact, no_contact (No Answer is never a connect regardless of duration) |
| Left Voicemail 45s | no_contact (correct) |
| Gatekeeper 60s | no_contact (as the worker decided; see judgement below) |
| Bad Number / Wrong Number 120s | no_contact |
| Connect 19s / 20s | no_contact / **connect** (boundary at `>= 20` correct) |
| Meeting 300s | connect |
| Unknown name ("Weird") 15s / 40s | no_contact / connect |
| `disposition_name=None` 15s / 40s | no_contact / connect |
| duration `None` (any) | no_contact (treated as 0) |
| **Do Not Call 60s** | **connect** (F7) |

Judgement on Gatekeeper at 60s: no_contact is the conservative and correct call for a cooldown of 3 days, because the decision-maker was not reached; I agree. Judgement on unknown disposition at 40s counting as a connect: acceptable but it triggers a 14-day cooldown on an unrecognised name, so the caller should log unknown names (it does count them as "connect" silently now). Minor.

## 5. Persistence
- **Atomic write: PASS.** Injected an exception at `os.replace` and another inside `json.dump`: the old file stays byte-identical and no `.tmp` is left behind (`state.py:213-232`). The file is 0600 (mkstemp), good.
- **Corrupt, empty, `[]`, wrong version, truncated:** all moved aside to `state.json.corrupt-N`, empty State returned, warning logged; successive corruptions get `-1`, `-2`... A later save writes a fresh `state.json` and cannot destroy the moved-aside file. Data is not lost, **but the run carries on with empty memory (F1).**
- **Tolerant loading gap (F8):** `"prospects": "x"` raises `AttributeError` out of `load_state` (only `OSError`/`ValueError` are caught, `state.py:195`). `"dnc_prospect_ids": "abc"` silently becomes `{'a','b','c'}`. Hand-edit or schema-drift problems, not realistic corruption.
- **Huge file:** 500k `seen_calls` = 20.9 MB, save 0.4s, load 0.3s. Not a problem.
- **Concurrency:** no lock; last writer wins (F6). Single nightly job, so low.
- **Git hygiene (F9):** `.gitignore` has `state/*.json` only. `git check-ignore` confirms `state/state.json` is ignored but **`state.json.corrupt-N` and `.state-*.tmp` are NOT ignored.** A corrupt-file rename followed by `git add -A` would commit prospect data.
- **PII:** state.json stores Nooks prospect ids, account ids, **recipient email addresses** (`emails` keys, dots stripped and lower-cased), phone numbers (`dnc_phones`), disposition names and timestamps. No names, no call content. Acceptable only because the directory is gitignored; the exposure is the local/container disk and anything that copies `state/`. Recommend hashing email keys (sha256 of the normalised address) since they are only ever compared, never displayed.

## 6. Email history
| Case | Result |
|---|---|
| Dotted vs dotless (`first.last@` vs `firstlast@`) | match, "emailed 2 day(s) ago" |
| Case (`FIRST.LAST@EXAMPLE.COM`) | match |
| Plus addressing (`first+x@` vs `first@`) | **no match** (F10, low; Gmail treats them as one mailbox) |
| `sent_at` None | "sent email found but its date is unknown; treated as recent" |
| Dated copy arrives after undated | upgraded; no longer recent when old (correct) |
| Undated copy arrives after dated | dated value kept (correct) |
| One old dated + one undated message | recent forever (F11) |
| Future-dated email | "emailed -37 day(s) ago", counted recent (cosmetic) |
| `None` email | (False, "no usable email address") |

**F11: undated = recent forever** can block a prospect indefinitely (for example every message the search view returns without a date). Recommendation: when an undated message is first recorded, store `first_seen=today` alongside it. The email was sent on or before first sight, so `today - first_seen >= days` proves it is outside the window; treat it as recent only until `first_seen + email_cooldown_days`. This stays conservative (never under-blocks) and expires.

## 7. `seen_calls` growth
150 calls/week is about 7,800 a year. At about 70 bytes per entry (id plus ISO time, indent=1) that is about 0.55 MB per year; 500k entries is 21 MB and still loads in 0.3s. **No pruning needed.** If a cap is wanted anyway, drop entries older than 24 months **and** make `record_calls` ignore calls older than that horizon, otherwise a refetch would double count.

## 8. Code quality: PASS
No network, no secrets, Python 3.11+ syntax, `from __future__` annotations, commented, small functions. Returns a new State and inputs are untouched. Notes: `mark_do_not_call` imports `replace` inside the function (`state.py:400`, move to the top); `email_opt_out_keys` and `last_touched` are used only by tests (not dead, they are for 1d/1f, but confirm they are wanted); `State.report` is a dataclass field that takes part in `==`.

## Findings by severity

**High**
- **F1 `state.py:195-198`** A corrupt/unreadable state file gives an empty State, so DNC memory, cooldowns and email history are silently gone for that run, and the only signal is a log line. Add a flag the caller can test (for example `state.report["load"]["reset"] = True`, or a `LoadResult`) and make live runs refuse to proceed unless full Nooks call history was refetched to rebuild DNC. Dry-run can continue.

**Medium**
- **F2 `state.py:376-390`** DNC is keyed by prospect id and phone only. A duplicate prospect of the same person (different id, different or no phone, same email) is callable and emailable. Add `dnc_emails` (hashed normalised key) set from DNC calls whose prospect email is known, and check it in `is_blocked`.
- **F3 `state.py:236`** DNC name match fails open for punctuation, double space or NBSP variants. Normalise with `" ".join(s.split()).casefold()` and strip trailing punctuation, or log every disposition name that merely *contains* "do not call" and is not an exact match.
- **F12 timezone/day (section 3)** UTC-date cooldown is one day short for morning calls in ANZ; caller-supplied `today` needs the same timezone.

**Low**
- **F4 `state.py:323-324, 386`** DNC phones are not normalised inside state (`+61 400 000 010` does not block `+61400000010`). Run both sides through `loaders.normalise_phone` or strip spaces.
- **F5 `state.py:365-372`** `_apply_latest` ties go to whichever call is processed last, so equal `started_at` with different outcomes depends on input order. Tie-break deterministically (for example higher `nooks_id`, or prefer no-contact for the cooldown rule).
- **F6 `state.py:213`** Last-writer-wins; no lock. Union the DNC sets on save, or use a lock file.
- **F7 `state.py:243`** `Do Not Call` is not in the no-contact list, so a long DNC call counts as a connect (adds a 14-day cooldown and a connect count). Harmless for blocking; fix for tidy stats.
- **F8 `state.py:143-170`** Loader crashes on wrong container types (`"prospects": "x"`) and splits a string into characters (`"dnc_prospect_ids": "abc"`). Check container types.
- **F9 `.gitignore`** Add `state/*` with `!state/.gitkeep` so `.corrupt-N` and `.state-*.tmp` cannot be committed.
- **F10 / F11 email**: plus addressing not matched; undated emails recent forever (recommendation above).
- Minor: spaced/hyphenated names; unknown dispositions silently count as a connect at 20s or more; future-dated emails show negative age.

## Recommendations (ordered)
1. F1 and F9 first (data safety, 10 minutes each).
2. F12: local-date cooldowns, and agree the rule with 1f so both use the same `today`.
3. F2, F3, F4 (DNC hardening); add tests for each attack row above that currently fails open.
4. F11 first-seen expiry for undated emails; F5 tie-break.
5. Keep fail-closed behaviour for the shared-phone DNC (b, b2).

## UNVERIFIED
- Ranker's use of `cooldown_until` and `today` (1f not visible).
- Real Nooks disposition strings and whether DNC ever arrives with an unresolvable disposition id (state cannot recognise it).
- Real Gmail `sent_at` availability (decision 9).
- I did not audit every one of the 85 worker tests; counts and my own attacks were verified instead.

---

# Re-review (2026-11-25): worker fixes `c162867` plus main `9f5704d`

Scope: offline, read-only, synthetic data only. Reviewed `src/state.py` at 728 lines (schema v2) after merging `origin/phase1/1c-state` and `origin/main` (including `9f5704d`) into this branch. The ranker (`origin/phase1/1f-ranker`, `28980ed`) was run from a scratch copy; it does not contain `9f5704d`, which does not matter for the checks below. Scripts are in the scratchpad and not committed.

**FINAL VERDICT: FAIL, on one item (R1, High, small fix).** Everything else is fixed or low. R1 is a safety hole in the new reset gate, so I will not pass it until it is closed.

## A. Tests and determinism
- `pytest tests -q`: **300 passed**. `-W error`: **300 passed** (298 on the worker branch, +2 from main).
- Replay 3x, 50 shuffles and 3 save/load rounds: identical `state_to_dict`. Inputs untouched (deep compare). Same-timestamp calls in either order: same result.

## B. Status of my earlier findings

| ID | Status | Evidence |
|---|---|---|
| F1 reset flag | **PARTLY FIXED, see R1** | Truncated file: `was_reset=True`, reason set, `require_healthy` raises `StateResetError`, `rebuilt_from_history=True` passes, file kept as `state.json.corrupt-1` byte-identical (`state.py:111-119, 202-227`). But the flag is not saved: a dry run that saves the reset state makes the next load look healthy with an empty DNC list (R1). |
| F2 duplicate prospect | **FIXED** | DNC call stores a hash of the prospect's email (`state.py:455-466`). A different id and phone with `firstlast@`, `FIRST.LAST@EXAMPLE.COM`, `first.last+tag@`, `firstlast+x@`, ` first.last@example.com ` are all blocked on call, email and any. A different address or domain is not blocked. Gap: the email is only learned when that prospect is in the `prospects` list passed to `record_calls` (the worker lists this). |
| F3 name variants | **FIXED** | `Do Not Call.`, `DO  NOT CALL`, `Do-Not-Call`, NBSP, `do_not_call` all recognised (`_norm`, `state.py:136-140`). Not recognised, by design: `Do Not Call (DNC)`, `Do Not Call - Remove`, `DNC`, `Do Not Contact`. Recommend logging any call disposition that contains "do not" or "dnc" but is not the configured name. |
| F4 phone | **FIXED for E.164 input** | `+61 400 000 010` stores as `+61400000010` and blocks the canonical number. Residual: a national-format value (`0400 000 010`) falls back to `0400000010` and would not match `+61400000010`. Real `to_e164` is already normalised by the loader, so low. |
| F5 tie-break | **FIXED** | Equal timestamps go to the higher call key; both orders give `connect`. |
| F7 long DNC | **FIXED** | 300s `Do Not Call` is `no_contact`, `connect_count` 0. |
| F8 container types | **FIXED** | `prospects` as a string, a DNC list holding an int or being a string, and a v1 file all give `was_reset=True` (fail closed). |
| F9 gitignore | **FIXED** | `state/*` with `!state/.gitkeep`; `state.json`, `.corrupt-N`, `.state-*.tmp` all ignored. |
| F10 plus-address | **FIXED via main** | `a.b+news@` matches `a.b@` and `ab@` in both directions (loaders now strips `+tag`). |
| F11 undated email | **FIXED** | First seen 11-02, 7 days: recent through 11-08, not recent from 11-09; a later re-sighting does not reset `first_seen`; a dated copy upgrades it. |
| F12 local days | **FIXED** | See table below. |
| F6 concurrency | Open, accepted | Single nightly job. |

### Cooldown table (account-local dates; no-answer +3, connect +14)

| Call | until (first callable day) |
|---|---|
| Sydney 08:50 AEDT Mon 2026-11-02, no answer | 2026-11-05 (was 11-04 under the old UTC rule) |
| Auckland 08:50 NZDT Mon 2026-11-02 | 2026-11-05 |
| Perth 08:50 AWST Mon 2026-11-02; Perth 15:00 | 2026-11-05 both |
| Brisbane 08:50 AEST | 2026-11-05 |
| Sydney 23:30 AEST Mon 2026-06-01 | 2026-06-04 |
| Sydney 00:10 AEST Tue 2026-06-02 | 2026-06-05 |
| Sydney Fri 2026-10-02 (DST starts 10-04) | 2026-10-05 |
| Sydney Sat 2026-10-03 23:30 (eve of DST) | 2026-10-06 |
| Sydney Fri 2027-04-02 (DST ends 04-04) | 2027-04-05 |
| Auckland Thu 2026-09-24 (NZDT starts 09-27) | 2026-09-27 |
| Sydney 2026-12-30 / 2028-02-27 | 2027-01-02 / 2028-03-01 |
| Connect Sydney 08:50 2026-11-02 | 2026-11-16 |
| Connect 11-02 then no answer 11-14 | 2026-11-17 |
| No tz, or invalid tz (`Mars/Base`) | Sydney default, 2026-11-05 |
| `is_in_cooldown` Wed 11-04 / Thu 11-05 (after the 11-02 call) | True / False |

DST arithmetic is by calendar day, which is the right behaviour.

### Email hashing
No raw address appears in `state.json` (checked for `@` and the domain after a DNC plus email round trip). Hashes are `sha256("callsheet-v1:" + normalised email)`: unsalted, so guessable for anyone who has a candidate address list (I confirmed the hash is reproducible offline). **One-line recommendation: accept it.** The file is local and gitignored, and `dnc_phones` and the Nooks ids are stored in plain text anyway, so a keyed hash would add little; the real control is that `state/` never leaves the machine.

## C. New findings

**R1 (HIGH) `state.py:111-119, 202-227`: a reset can be laundered by one dry-run save.** `was_reset` and `is_new` are not persisted. Sequence: file corrupt, `load_state` gives `was_reset=True`, a dry run continues (allowed by the notes) and calls `save_state`, next night's `load_state` gives `was_reset=False, is_new=False`, `require_healthy` passes and DNC is empty. I reproduced it: after the save, the second load had `dnc_prospect_ids=set()` and `require_healthy` did not raise. Also `rebuilt_from_history=True` is an honour-system boolean.
Fix: persist a marker in the file, for example `dnc_rebuilt_at` (date) plus `dnc_rebuilt_calls` (count), written only by an explicit `mark_dnc_rebuilt(state, calls_seen, earliest_call)` after `record_calls` over the full history. `require_healthy(live=True)` raises unless the marker is set. Fresh State, reset State and v1 migration all start with it unset, so the dry-run save cannot clear it.

**Answer to your first-run question: yes, a first run must be gated the same way.** A missing `state.json` is indistinguishable from a deleted one, and DNC exists only in this file plus Nooks call history. With the persisted marker above, `is_new` and `was_reset` collapse into one rule: no live write until DNC has been rebuilt from full history and the marker is saved. Dry runs can proceed.

**R2 (MEDIUM) Salesforce id keys, answer to your item (4): yes, normalise inside state.** `state.py` keys on whatever `sf_account_id` it receives (`state.py:420, 490, 570`, `record_sheet`, `last_sheet_ids`, `cooldown_until`). With loader output (15-char) and an 18-char lookup, it silently misses. Reproduced: an account stored as `0015g00000AbCdE`; `is_in_cooldown(..., 15-char)` is True, `is_in_cooldown(..., 18-char)` is False, `was_in_last_sheet(18-char)` is False, `cooldown_until` keys are 15-char only. `accounts_by_id` keyed 18-char would also miss in `accounts_by_id.get(sf_id)` (timezone falls back to Sydney, nooks/zoominfo ids not captured). Fix: apply `normalise_sf_id` at every API boundary (`record_calls` prospect and `accounts_by_id` keys, `record_sheet`, `is_in_cooldown`, `was_in_last_sheet`, `cooldown_until` output keys) and when loading a file (so old keys migrate). Synthetic `FAKE-` ids pass through unchanged, so existing tests keep working. The ranker also has to normalise its lookups (`_ids`), or both sides are 15-char only by convention.

**R3 (LOW) `state.py:260-290`:** an account entry with a bad `last_call_date` (for example `"junk"`) loads with no warning (`_date_str` returns None), so `cooldown_until` silently drops that account (fails open on cooldown, not on DNC). Derive the date from `last_call_at` when the stored date is bad, or add a warning.

**R4 (LOW) `state.py:260-345`:** `load_warnings` is not gated by `require_healthy`. I checked: skipped entries are accounts, prospects, email messages and sheets only. A malformed DNC list (ids, phones, emails) makes the whole file a reset, so **a skipped bad entry cannot fail open on DNC**. A skipped account loses its cooldown, a skipped email loses its history, a skipped sheet can serve an account twice in a row. Acceptable, but log the counts.

**R5 (LOW)** The module docstring (`state.py:11-13`) still says days are UTC and loading gives only a warning; it contradicts the new account-local behaviour. Update it.

**R6 (LOW, integration)** The ranker's `is_blocked` reads only `prospect.do_not_call` and `opted_out_email`. State's id, phone and email blocks only reach it if the caller runs `mark_do_not_call(prospects, state)` first (it does set `do_not_call` for the duplicate-email case, verified). Make that a documented step of the sheet builder, or add a one-call helper.

## D. New-code review
- **`StateResetError` / `require_healthy`:** correct as far as it goes; nothing enforces that a live runner calls it, and the boolean is trusted (R1 fix addresses both).
- **Schema v2:** v1 files reset (fail closed). No real v1 file exists, so no migration is needed.
- **`load_warnings`:** see R4. DNC malformed means reset, so no fail-open.
- **`cooldown_until_by(state, config, kind)`:** works for `nooks_account_id` and `zoominfo_id`; shared id keeps the later date. Alternate ids are captured only when `accounts_by_id` carries them at `record_calls` time.
- **Ambiguous shared phone:** still blocks both prospects (fail closed). Keep.

## E. Contract with the ranker (run together on synthetic data)
I ran `state.record_calls`, `record_sheet`, `mark_do_not_call`, `cooldown_until`, `last_sheet_ids` into `rank_accounts` (copy of `28980ed`) on six synthetic accounts (Sydney, Melbourne, Auckland, Perth). Results:
- `cooldown_until` values are `date` objects, first callable date inclusive. A Sydney 08:50 call on 11-10 gives until 11-13. The ranker, with the first window on 11-11 (09:00 AEDT), applied the -30 cooldown penalty; with the first window on 11-13 (equal to `until`) it did not. Auckland and Perth accounts used their own local dates. **Unchanged plug-in: PASS.**
- `last_sheet_ids` (a set of ids) gave `skipped: on last sheet` (hard skip). PASS.
- Duplicate-email DNC prospect, after `mark_do_not_call`: `skipped: all prospects blocked`. PASS.
- `cooldown_until_by` keyed by nooks and zoominfo ids is picked up by the ranker's `_ids` lookup (cooldown applied to the right accounts). PASS.
- Not tested: real 1d output, and 18-char ids end to end (R2).

## F. Remaining items needed for PASS
1. **R1 (High):** persisted `dnc_rebuilt` marker and gate; first run included. `state.py:111-119`.
2. **R2 (Medium):** `normalise_sf_id` at state's API boundary and on load.
3. R3 to R6 are low; can be done in the same pass.
