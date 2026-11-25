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
