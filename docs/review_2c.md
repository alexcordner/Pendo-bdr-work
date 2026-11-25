# Review of task 2c (`src/callsheet.py`, `tests/test_callsheet.py`)

Reviewed commit `16781aa` on `phase2/2c-callsheet`. I had no access to the worker's reasoning.
Fully offline and read-only: no connector was called, no worker file was edited, all data below is invented. Probe scripts were throw-away (not committed).

## Verdict: PASS WITH FIXES

The core is sound. Ordering, limit semantics, determinism, DST `dueAt`, the exact payload key set, the no-trigger honesty and the refusal of needs_prospect / spilled / skipped lines all hold.
Fix before Phase 3 can write anything (all small): **H1** (owner-id guard), **H2** (`recheck_blocked` bypasses and its link to the payload), **M1** (None / junk opener fields), **M2** (note length cap), **M3** and **M4** (email / phone leak paths). **M5** and **M6** are honesty and logging gaps. Nothing here can send or write anything today: `callsheet.py` only builds dicts.

## Checklist

| # | Item | Result |
|---|---|---|
| 1 | Tests and determinism | **PASS**. `pytest tests/ -q`: 1344 passed. `-W error`: 1344 passed. `tests/test_callsheet.py`: 128 passed. 40-account world, 6 shuffles of the input lists: one distinct output (text + markdown + JSON). `PYTHONHASHSEED=0/1/2/random`: identical sha256 prefix `d90a301f8eccb1ce`. |
| 2 | PII and safety | **FAIL** on free-text pass-through (M3) and `repr` (M4). Structured paths are clean. See below. |
| 3 | Honesty | **PASS WITH FIXES**: M5 (stale / future signals), L3 (intent). Cases a, b, e, f, h pass. |
| 4 | Payload correctness | **PASS** for dueAt, keys, tag and priority. **FAIL** for the owner placeholder (H1) and no length cap (M2). |
| 5 | Refusals | **PASS** for line types and blank fields. **FAIL**: bypasses H2, M1. |
| 6 | Sheet semantics | **PASS**. Findings M6, L2 (rendering). |
| 7 | Hard-rule scan | **PASS**, with a Phase 3 recommendation below. |
| 8 | Code quality | **PASS** with Low items (L7, L9, L10). |

## Findings

### HIGH

**H1. Placeholder owner id reaches the payload silently; no live guard.** `callsheet.py:656-681`, `:30`.
`owner_id` defaults to `"OWNER_ID_PLACEHOLDER"`, and no value is validated. The function has no `live` or `dry_run` notion, so a live Phase 3 caller that forgets the argument gets a "valid" payload that is only caught (if at all) by Nooks.
Repro:
```python
cs.nooks_task_payload(line, FULL, "NK-1")["ownerId"]                # 'OWNER_ID_PLACEHOLDER'
cs.nooks_task_payload(line, FULL, "NK-1", owner_id="")              # accepted -> ''
cs.nooks_task_payload(line, FULL, "NK-1", owner_id=None)            # accepted -> None
cs.nooks_task_payload(line, FULL, "NK-1", owner_id=5)               # accepted -> 5
```
Fix: make `owner_id` a required keyword with no default. Refuse non-str, blank, or equal to the placeholder, unless an explicit `dry_run=True` is passed. Add a test for each refusal.

**H2. `recheck_blocked` can be bypassed, and nothing ties it to the payload.** `callsheet.py:701-713`, `:668-681`.
- (a) Whitespace-padded Nooks id. `recheck_blocked` looks up `" NK-RES "` in `dnc_prospect_ids` and misses, while `nooks_task_payload` strips and sends `"NK-RES"`, which IS blocked.
  ```python
  state = State(dnc_prospect_ids={"NK-RES"})
  cs.recheck_blocked(line, replace(sp, nooks_id=" NK-RES "), state)   # (True, '')
  cs.nooks_task_payload(line, FULL, " NK-RES ")["prospectId"]         # 'NK-RES' (DNC)
  ```
- (b) Missing SF id skips the wrong-person check (`:710`): a resolved prospect with `sf_contact_id=None` returns `(True, '')`. Medium: an unverifiable identity should fail closed.
- (c) `nooks_task_payload` accepts any id string. Nothing proves it is the id that passed `recheck_blocked`, so a caller can recheck one prospect and build for another.
- (d) Only the resolved prospect's phone / email is checked. If Nooks returns it without phone or email, a phone- or email-keyed DNC is missed even though the sheet's own SF contact has them (`recheck_blocked(line, replace(base, phone_e164=None), State(dnc_phones={sp_phone}))` gives `(True, '')`). Also run `is_blocked(line.entry.prospect, state)`.

Fix: strip and normalise the Nooks id once, and use that same value for the check and the payload. Fail closed when either SF id is missing. Best: one function `nooks_task_payload(line, fields, resolved_prospect, state, owner_id=...)` that rechecks internally, so the payload can only be built from a checked prospect.
Confirmed good: DNC by Nooks id, `do_not_call` flag, DNC by phone / email when present, email opt-out (channel `any`, matches decision 2), blank / None Nooks id, and a different SF contact id are all refused.

### MEDIUM

**M1. `None` and junk opener fields pass.** `callsheet.py:670` and `:675`.
`str(opener_fields.get(f, ""))` turns `None` into `"None"`.
```python
nooks_task_payload(line, dict(FULL, opener=None), "NK-1")["note"]   # line 2: 'OPENER: None'
# also accepted: 0, ['a'], {'a': 1}, '​', '﻿', '.', 'none verified' (even as OPENER)
```
A failed LLM step that leaves a `None` produces a task whose opener reads "None". Fix: require `isinstance(str)`, strip zero-width characters (`​`, `﻿`) before the emptiness test, and require at least one alphanumeric character. Allow `none verified` only for `fact_2` and `trigger`.

**M2. No note length cap.** `callsheet.py:676-679`. Five fields of 5,000 characters give a 25,080-character note, accepted. The Nooks note limit is UNVERIFIED. Estimate: tag about 32 characters plus labels about 50. Packet evidence is capped at 280 characters (`signals.EVIDENCE_MAX_CHARS`), but the LLM-written fields are unbounded. A realistic note is about 1.0-1.5k characters, but nothing keeps it under any limit. Fix: refuse (do not truncate) when `len(note) > 2000`, as a named constant, and confirm the real limit in Phase 3.

**M3. Email and phone pass straight through free-text fields.** `callsheet.py:384`, `:407`, `:419-446`, `:606-607`. The spec was "NO email/phone anywhere"; the structured paths hold, but these fields are copied verbatim into every renderer, the packet and the payload note:
- prospect `title`: `"Head of Product +61400111222"` appears in text, markdown, JSON, packet and `why_chosen`;
- account `name`: `"boss@company.example.com Pty Ltd"` appears everywhere;
- scoop `evidence`: `"Contact jo@scoop.example.com or +61 2 9999 1234 ..."` becomes available fact 1;
- opener fields containing an email or phone are accepted into the note (probe: `note` contains `x@y.example.com`).

Not leaked: `Prospect.email` and `Prospect.phone_e164` themselves, and the surname, on any rendered output (text, markdown, JSON, packet, `why_chosen`). Fix: one `_scrub(text)` that redacts email- and phone-shaped substrings, applied to every string that enters a packet or renderer. Refuse (or redact) an email or phone in opener fields in the payload. Add the three probes above as tests. A URL in a field is passed through unchanged; that is fine for a scoop link but is not scanned.

**M4. `repr`/`str` of `SheetLine` and `Sheet` dump the whole `Prospect`.** `callsheet.py:160-171`, `:164`.
Default dataclass repr includes `entry -> Prospect(email=..., phone_e164=..., name='Pat Smithson')`. `repr(line)`, `str(line)`, `repr(sheet)` and a pytest assertion diff all leak. `repr(packet)` and the renderers are clean.
Fix: `entry: SheetEntry = field(repr=False)` on `SheetLine` (or a custom `__repr__`). Add a test that `repr(sheet)` contains no email or phone.

**M5. `verdict_reason` and age make claims the sheet never verified.** `callsheet.py:291-301`, `:285-288`.
- Funding scoop 400 days old: `verdict_reason` = `"kept: Funding within 45 day limit (400 days old)"`, and `why_chosen` says `"400 days ago"`. The "within limit" text is false. The sheet trusts upstream filtering, so say so, or compare age to `max_age_for` and add a flag `signal older than limit`.
- Signal dated 10 days in the future: shown as `today`, `age_days=0`, source label `ZoomInfo scoop 2026-12-05`, no flag (`age_days` clamps at 0). Fix: flag `signal date in the future`.
- Upstream funding-for-an-investor is not detected here (an `Investor Capital Partners led a $20M round` scoop becomes a fact). That is `signals.filter_scoops`' job; the sheet only reflects it. UNVERIFIED end to end here (I did not run raw scoops through `filter_scoops`).

**M6. The text and markdown renderers cannot serve as the CLAUDE.md rule 5 run log.** `callsheet.py:553-560`, `:618`.
`BUCKET_LIST_LIMIT = 25` (as specified): 80 skipped accounts render 25 names and `... and 55 more`. JSON lists all 80. The spec both required "every skipped account" and "capped at 25"; the cap is fine for a display sheet, but the log must name every one. Fix: `bucket_limit: int | None = BUCKET_LIST_LIMIT` argument; the log writer passes `None`. Cosmetic: in markdown the overflow line renders as `**  ... and 55 more**` (bold with stray indent).

### LOW

- **L1. `_first_name` takes the first token.** `callsheet.py:219-221`. `"Dr Pat Smith"` gives `Dr`; `"Prof. Jane Doe"` gives `Prof.`; `"Smith, Pat"` gives `Smith,` (the surname is printed). Strip honorifics, and treat `"Surname, First"` explicitly (or return None when unsure).
- **L2. Markdown is not escaped and titles are not shortened.** `callsheet.py:606-607`, `:611`. A newline in an account name or title injects headings: `"Evil\n# Injected heading"` renders as a real H1. Pipe and backtick characters pass unchanged (no tables are used, so harmless). A 500-character title is printed in full in markdown (text mode shortens with `_short`). Fix: collapse whitespace and shorten in one `_md()` helper. Unicode, emoji and CJK render without errors.
- **L3. Intent-only accounts report `has_real_trigger=True`.** `callsheet.py:410`, `:330`. The label correctly says "interest signal, not a dated event", but `flags` has no `no trigger` and `why_chosen` says `5 days ago`. Phase 1 allows intent as the best signal, so this is consistent; Alex should decide whether an intent-only line counts as a trigger. Suggest a packet field `trigger_kind` or `has_dated_trigger`.
- **L4. 15 vs 18 character SF id.** `callsheet.py:709-711` compares raw strings, so a resolved 18-char id for the same contact is refused as "different contact". Safe direction, but use `normalise_sf_id`.
- **L5. `build_sheet` trusts `rank_result.prospects`.** `callsheet.py:492`. A `RankResult` carrying a `do_not_call` or `opted_out_email` prospect still yields a line. The ranker guarantees this upstream; add a cheap `prospect.do_not_call or opted_out_email` guard as defence in depth.
- **L6. A hand-built `SheetLine` passes `isinstance`.** `callsheet.py:663-667`. A line copied from a spilled account (same fields, no `skipped_reason`) builds a payload. Needs deliberate code, but a `Sheet`-level builder (`payloads_for(sheet, ...)`) would remove the class of problem. Mutating `skipped_reason` after build IS refused.
- **L7. Unused import `timedelta`** (`:18`); `src.state` imported before `src.ranker` (cosmetic).
- **L8. Prospect id checks are shallow.** `callsheet.py:668-681`. `'​'` and `'NK 1'` are accepted; a Nooks id is a UUID in practice (UNVERIFIED).
- **L9. `rank` gaps.** `callsheet.py:484`. The rank is the position among all ranked accounts, including dropped ones, so tags can skip a number (`#1, #3`). Harmless; document it.
- **L10. Two `_signals_for` implementations** (`callsheet.py:253` and a private one in `ranker.py`), and `_sort_key_rank` ties on `ra.signals[0]` rather than `best_signal` (`:450-453`). Only a tie-break; no observed divergence. A shared helper in `signals.py` is cleaner.

## Verified details

**Honesty cases** (invented accounts, real `rank_accounts`):

| Case | Result |
|---|---|
| (a) no signal, no size, no location | `has_real_trigger=False`, 0 facts, note "Only 0 verified fact(s)... Do not pad." |
| (e) one fact only | 1 fact, note set |
| (f) zero facts | 0 facts, note set |
| (b) ambiguous role | `role ambiguous` flag in why / flags / packet, suffix stripped from evidence |
| (c) intent | labelled "interest, not a dated event"; see L3 |
| (d) investor funding | passes (upstream's job), see M5 |
| (g) future / very old | see M5 |
| (h) funding + exec move + intent | best = exec move; "2 more triggers"; intent fact labelled; facts in a stable order |
| NONE-kind signal | treated as no trigger |

Facts never include anything not in the account, signal or history data. `Account.industry` does not exist and is handled via `getattr` (`:393`): PASS. The `must_not_claim` list is sensible; it could add "do not say you saw their website or LinkedIn". Facts 2 and 3 are fit data (headcount, city), not triggers, which is honest but weak.

**dueAt and DST** (real ranker output, all four zones, same account set). Offsets match `zoneinfo`; `datetime.fromisoformat(dueAt) == local_call_time`; every time is in its window on the first working day:
- Fri 2 Oct 2026 12:00 UTC: Mon 5 Oct 08:45 Sydney `+11:00`, Auckland `+13:00`, Brisbane `+10:00`, Perth `+08:00`.
- Fri 25 Sep 2026: Auckland `+13:00`, Sydney `+10:00` on 28 Sep.
- Fri 2 Apr 2027: Mon 5 Apr Sydney `+10:00`, Auckland `+12:00`.
- 23:30 UTC and 13:30 UTC on 25 Nov: correct dates and offsets.

Note: 4 Oct 2026 and 4 Apr 2027 are Sundays, so the transitions fall on non-working days and the first call is the Monday after.
`run_date` = `now` in Australia/Sydney. Sydney's date can differ from Perth's (at 14:00 UTC) or Auckland's (at 11:30 UTC) for a few hours a day. It is only a label in the tag, never a due date, and I found no case where the due date is earlier than `run_date`. Acceptable; document it.

**Priority bands** (75 urgent, 55 high, 35 normal, else low): boundaries correct (75 urgent, 74.999 high, 55 high, 54.99 normal, 35 normal, 34.99 low). A fit-only account scores 40 (normal), size-unknown 22.5 (low), and any account with a 3-day-old scoop 72-90 (high or urgent), so on this world the top bands may saturate. Tune on real scores (Alex's call).

**Tag format.** `^\[apac-callsheet (\d{4}-\d{2}-\d{2}) #(\d+)\]$` on the first note line round-trips for ranks 1..40 (multi-digit included) for three dates, including a leap day: 0 failures. A spoofed tag inside an opener field is on a later line (newlines collapsed to spaces), so anchored first-line parsing is safe; an unanchored regex would match twice, so the Phase 3 de-dupe must parse line 1 only.

**Payload keys.** Exactly `{action, dueAt, ownerId, prospectId, priority, note}` and nothing extra (checked); `action` is `"call"`. Against the real Nooks schema this is UNVERIFIED by me (I read it from the task spec only).

**Refusals confirmed.** A needs_prospect, spilled or skipped account never becomes a `SheetLine`; ranked accounts carrying a `skipped_reason`, no time or no prospect land in `Sheet.dropped` with the reason. `nooks_task_payload` refuses a `RankedAccount`, `SheetEntry`, `dict` and `None`; blank, whitespace or `None` prospect id; each of the five opener fields when empty (and `\xa0`, `\t\n`, em-space); a line mutated to `skipped_reason`; a naive `local_call_time`.

**Sheet semantics.**
- With the top score on the latest-calling account (Perth), `limit=2` keeps Perth plus the best Sydney, then orders by time (Sydney first, Perth second); `limit=1` keeps Perth; `limit=0` gives an empty sheet.
- Groups are keyed by (tz, window, local date) and ordered by instant, across zones (Auckland 09:01+13 before Sydney 08:45+11 before Perth 08:45+08 on the same day).
- Ranks match the ranker's order (40 accounts with many ties).
- Empty sheet renders cleanly in all three formats.
- 40 lines render fine.
- JSON uses `ensure_ascii` (unicode escaped but valid).

## Hard-rule scan (item 7)

`callsheet.py` imports only `json`, `collections.abc`, `dataclasses`, `datetime`, `zoneinfo` and `src.*`. It has no `open`, `print`, `os`, `logging`, network or subprocess use, and none of the forbidden names (`grep` for `createTask|create_draft|send_message|sendTaskEmail|createEmail` over `src/*.py` returns nothing). The docstrings say "task-create", never `createTask`, so the repo scans (`tests/test_integration_pipeline.py:1440-1481`, which also bans network / process imports and restricts imports to an allowlist) pass today.

Phase 3 recommendation: do not remove or loosen the scan. Add one new writer module (e.g. `src/nooks_writer.py`) as the only place the call name `createTask` may appear, via a per-file allowlist (`ALLOWED_CALLS = {"nooks_writer.py": [r"\bcreateTask\b"]}`) that exempts only that pattern in only that file; every other file, including `callsheet.py`, keeps the full ban. Keep `FORBIDDEN_IMPORTS` for all files, including the writer (the connector call is made by the Claude session through the MCP tool, not by an HTTP client in code). Add a test that the writer refuses to run without `--live` and without a real (non-placeholder) owner id (see H1), and one that asserts the allowlist contains exactly one file.

## What I could not verify

Real Nooks `createTask` behaviour (note length limit, `prospectId` format, `dueAt` handling) since no connector was called; whether Nooks returns `crmId` on resolved prospects (affects H2b); the end-to-end investor-funding drop (M5); and Phase 3 wiring, which does not exist yet.

---

# Re-review of the fixes (worker commit `931e866`, merged into this branch as `ab92f0a`)

Narrow and adversarial, offline, read-only, invented data. `nooks_task_payload` and `recheck_blocked` are gone; `prepare_task` is the only function that builds a payload (checked: the only `"action"` literal in `callsheet.py` is inside it, and no underscore helper can build a payload: `_field_text`, `_check_owner`, `_resolve_prospect` only validate).

## Final verdict: FAIL (narrow). Three small fixes turn it into PASS

Tests: `pytest tests/ -q` 1519 passed; `-W error` 1519 passed. Determinism: 6 shuffles give one output; `PYTHONHASHSEED` 0 and 7 give the same hash (`06004fc9e18b4947`). DST `dueAt` re-checked through `prepare_task` for 2 Oct 2026, 2 Apr 2027 and 25 Sep 2026: all four zones correct (Auckland `+13`/`+12`, Sydney `+11`/`+10`, Brisbane `+10`, Perth `+08`).

### Blocking (fix before Phase 3)

**R1. HIGH: do-not-call bypass by the case of the Nooks id.** `callsheet.py:848-859` (the id is only trimmed), `:896` (`is_blocked` compares exactly).
```python
state = State(dnc_prospect_ids={"abc-123-def"})
prepare_task(line, replace(sp, nooks_id="ABC-123-DEF"), state, FIELDS, OWN)   # BUILT, prospectId 'ABC-123-DEF'
```
Whether Nooks ever returns the same id in another case is UNVERIFIED, but the failure is silent and the fix is one line. Fix: also check `nid.lower()` and `nid.upper()` (build a copy of the prospect per variant and call `is_blocked` on each), or refuse ids that are not exactly lower-case. Add a test. Other id attacks all failed correctly: NBSP / tab / newline / trailing-space padding (trimmed, then blocked), zero-width inside or BOM prefix, full-width letters, Arabic-Indic digits (refused by `ID_PATTERN`), 15 vs 18 character SF ids (same contact built, different contact refused, case-only SF difference refused, missing SF id on either side refused).

**R2. HIGH (defence in depth): an empty or reset state builds live payloads.** `callsheet.py:869-905`.
```python
prepare_task(line, good, State(is_new=True), FIELDS, OWN)               # BUILT
prepare_task(line, good, State(was_reset=True, reset_reason="corrupt"), FIELDS, OWN)   # BUILT
```
`dnc_is_rebuilt` is False for both, so an empty Do Not Call memory simply finds nobody blocked. Recommend: unless `dry_run is True`, call `state.require_healthy(state, live=True)` (or `if not dnc_is_rebuilt(state): raise SheetError`) at the top of `prepare_task`. Also `isinstance(state, State)` (a `None`, `dict` or `str` state gives `AttributeError`, not `SheetError`: still fails closed, but with an unclear error).

**R3. MEDIUM: a phone number leaks into the `BlockedError` message.** `callsheet.py:896-898`. The reason comes from `state.is_blocked`: `"A: resolved prospect blocked: phone +61400000001 had a Do Not Call call"` (and the same for the sheet contact). Exception text ends up in logs. The earlier fix covered renderers, packets and `repr`, but not this path. Fix: raise with a fixed reason (`"phone number is on the do-not-call list"`), or `_scrub(reason)`.

### Medium / Low (should fix, not blocking on their own)

- **R4. MEDIUM: owner-id and dry-run guards are weaker than they look.** `callsheet.py:836-846`, `:841`. Accepted live: `'owner_id_placeholder'`, `'OWNER_ID_PLACEHOLDER2'`, `'REPLACE_ME'`, `'test'`, `'x'`. `dry_run="no"` (any truthy value) unlocks the placeholder. A `dry_run=True` payload carries nothing marking it as a dry run (it is a plain dict with the same keys; only `ownerId` differs). Fix: `dry_run is True`; compare the placeholder case-insensitively; require a plausible owner id (e.g. `[A-Za-z0-9]{20,40}`, matching the one id shape in CLAUDE.md, UNVERIFIED for all owners); and for dry runs prefix the note with `[DRY RUN]` or return a `dict` subclass `DryRunPayload` so a dry payload cannot be mistaken for a live one.
- **R5. MEDIUM-LOW (L6 left unfixed; now judged Medium-Low): a `SheetLine` is mutable and is not verified.** `callsheet.py:869`, `:670`, `:901`. Verified paths that still build:
  - a line copied with `dataclasses.replace` from a spilled or never-sheeted `RankedAccount` (same fields, no `skipped_reason`);
  - `line.entry.prospect` swapped after the build, then resolved as the "other" contact: built (identity is compared to the mutated entry);
  - a forged `rank` / `run_date` goes into the tag unvalidated (`#-3`, `1999-01-01`, and `rank="x\n[evil]"` gives a broken first line `[apac-callsheet 2026-11-25 #x`).

  The do-not-contact check still runs on whatever prospect is used, so this cannot bypass R1-class rules; it can send the wrong (scheduled, non-blocked) person or break the tag. Cheap guard: in `prepare_task` verify `isinstance(line.rank, int) and line.rank >= 1` and that the packet snapshot still agrees with the entry (`line.packet.account_key == account_key(line.account)` and `line.packet.prospect_sf_contact_id == line.entry.prospect.sf_contact_id`). Better still: `sheet_tasks(sheet)` that yields lines it created (an identity set kept on the `Sheet`).
- **R6. LOW: `now` is optional.** `callsheet.py:869`, `:882`. With `now=None` a stale line with a past due time is built (verified). Require `now` unless `dry_run is True`. `now == due` is refused, `now` before due builds, naive `now` raises `ValueError` (good).
- **R7. LOW: resolved prospect's `sf_account_id` is not compared to the line's account** (a prospect with the right SF contact id and a different account builds). The contact id is the real identity check, so Low.
- **R8. LOW (M3 residual): scrubbing misses some forms.** `callsheet.py:72-80`. Not redacted: `1300 123 456`, `1800 555 123`, `13 12 34`, 8-digit local numbers (`9999 1234`, `ph 9999-1234`), spelled-out digits ("zero four one two ..."), obfuscated emails (`jo at example dot com`, `jo[at]example.com`, `jo＠example.com` with a full-width `@`, `jo @ example.com`). False positive: `$1000000000` becomes `$[redacted]` (a 9+ digit run). Redacted correctly: all `+61`/`+64` forms, `04xx`, `02 9999 1234`, `(02)`, `+61 (0)2`, full-width and Arabic-Indic digit runs, zero-width-split emails (after `_clean`). Suggest `unicodedata.normalize("NFKC", ...)` first (fixes `＠`), add the 13/1300/1800 and 8-digit patterns, and treat the rest as an accepted limit (document it). The prepare-time opener check uses the same patterns, so the same gaps apply there.
- **R9. LOW: `ContactHistory.source` is not scrubbed.** `callsheet.py:163`, `:512`, `:524`, `:546`. A caller-supplied `source` containing an email or phone appears in `available_facts[].source` and `contact_history_source` (JSON, packet text, `repr(packet)`). Caller-controlled constant in practice; wrap it in `_clean`.
- **R10. LOW: `dataclasses.asdict(line)` still includes the full `entry` (email, phone, surname).** `repr`/`str` are fixed, but `asdict` bypasses `repr=False`. Do not log `asdict(line)`; or give `SheetLine` a `to_log()` and remove `entry` from dataclass fields' reach (note only).
- **R11. LOW: render options.** `render_sheet_text(sheet, -1)` is accepted; the stale / future signal's evidence is still printed under the heading `SIGNALS (real, sourced)` in packet text (the facts list does exclude it and the `facts_note` says "do not quote it", good), so a later session could still quote it. Print stale / future signals under a separate `NOT USABLE AS FACTS` heading.
- **R12. INFO: `_first_name`.** `'Jr Pat Smith'` gives `Jr`; `"O'Neil Pat"` gives `O'Neil`; surname-first names without a comma (`'Ng Wei'`) cannot be detected. Single-token names return None (shown as "the contact"). Acceptable.
- **R13. INFO: monkeypatching.** Replacing `callsheet.is_blocked` in-process disables the check; an in-process attacker is out of scope. Mutating the returned dict afterwards is also outside the function's control (a fresh dict is returned each call). Duplicate calls for the same line give identical dicts (no idempotency; the de-dupe belongs to the Phase 3 writer: parse line 1 of the note).

### Status of every earlier finding

| Finding | Status | Evidence |
|---|---|---|
| H1 owner guard | FIXED, residual R4 | required argument; blank / None / `5` / `<...>` / full-width refused; exact placeholder (and padded) refused unless `dry_run` |
| H2a padded id | FIXED | NBSP / tab / newline padding now trimmed before the check (blocked) |
| H2b missing SF id | FIXED | both sides required, refused when either is missing |
| H2c link between check and payload | FIXED | single entry point builds from the id it checked (residual: R1 case, R2 state health) |
| H2d sheet contact's phone / email | FIXED | `BlockedError` for a DNC phone / email present only on the sheet contact |
| M1 junk opener fields | FIXED | None, zero-width, BOM, `.`, `N/A`, `tbd`, emoji-only refused; `none verified` / `none found` refused as OPENER and GATEKEEPER, allowed for the other three by design |
| M2 note cap | FIXED | 2,000 characters, refuses (does not truncate). It counts characters, not bytes (non-ASCII text is up to 4x bigger in bytes; the Nooks limit is still UNVERIFIED) |
| M3 contact data in free text | PARTIAL | title, name, evidence, flags, notes, city, state, website, skipped reasons all redacted; see R8, R9 |
| M4 `repr` / `str` | FIXED | `repr(line)`, `str(line)`, `repr(sheet)`, `repr(packet)` clean; see R10 for `asdict` |
| M5 stale / future | FIXED | statuses recomputed from the signal date and the real per-kind limit; boundaries correct (45 ok / 46 stale, 90 ok / 91 stale); future-dated shown as "future-dated: check the source" (not "today"); stale and future signals are excluded from the facts and `has_real_trigger` is False; see R11 |
| M6 bucket limit | FIXED | `bucket_limit=None` lists all 80 skipped names in text and markdown; the default still shows 25 plus `... and N more` |
| L1 first names | FIXED, residual R12 | `Dr Pat Smith` gives `Pat`; `Smith, Pat` gives `Pat`; unsure gives None |
| L2 markdown | FIXED | newlines collapsed, titles shortened, `# | * _ [ ] < >` backslash-escaped; no injected heading |
| L3 intent-only | FIXED | `has_real_trigger` False, new `trigger_kind`, flag `no dated trigger` |
| L4 SF 15 / 18 | FIXED | 15 vs 18 same contact builds; different contact refused |
| L5 blocked prospect on RankResult | FIXED | dropped with a reason |
| L6 batch builder | NOT FIXED | Medium-Low, see R5 |
| L7 unused import | FIXED | `timedelta` removed; no unused imports or private functions |
| L8 id shape | FIXED | `ID_PATTERN` ASCII only |
| L9 rank gaps | NOT FIXED, documented | Low: acceptable, now commented on `SheetLine.rank` and `build_sheet` |
| L10 duplicate `_signals_for` | NOT FIXED | Low: only a tie-break; acceptable |

Not verified: whether Nooks ids are case-sensitive or what its note limit is (R1, M2); real `crmId` presence on resolved prospects.

Fix R1, R2 and R3 (each a few lines plus a test) and this is a PASS. R4-R6 are strongly recommended in the same pass because they are one-liners.

---

# Final re-review (worker commit `0eeb78e`, merged as `b818c6c`)

Offline, read-only, invented data. `pytest tests/ -q`: 1621 passed; `-W error`: 1621 passed.

## FINAL VERDICT: PASS

Every blocking and recommended item (R1-R13) is fixed and re-tested with the same repros. What remains is Low or Info and can be fixed or documented during Phase 3.

### R1-R13 status (same repros as before)

| Item | Result | Evidence |
|---|---|---|
| R1 case-variant DNC id | FIXED | Resolved `ABC-123-DEF` / `Abc-123-Def` against state `abc-123-def`: blocked. State holding the upper-case id against a lower-case resolved id: blocked. State entry with padding: blocked. NBSP / newline padding on the resolved id: trimmed, then blocked. Zero-width, full-width and non-ASCII digits in the resolved id: refused by the id pattern. |
| R2 unhealthy / wrong-type state | FIXED | Fresh `State(is_new=True)` and reset state refused live (`Do Not Call memory is not rebuilt`); allowed only with `dry_run=True`. `None`, `dict`, `str` give a `SheetError`, not `AttributeError`. |
| R3 phone in exception | FIXED | `BlockedError` text is fixed plus a code, e.g. `... blocked: Do Not Call / opt-out [dnc_phone]`; no number. |
| R4 owner / dry-run | FIXED | `dry_run` must be exactly `True`/`False` (`'no'`, `1`, `0`, `None`, `'True'` refused). Live owner ids refused: the placeholder in any case, `REPLACE_ME`, `test`, `x`, anything under 20 characters or containing placeholder words. The real-shaped id passes. Dry run returns a `DryRunPayload` with `[DRY RUN] ` at the start of the note. |
| R5 mutable / forged line | FIXED for accidents (see N1, N2 for deliberate use) | `copy.copy`, `deepcopy`, `pickle`, `dataclasses.replace`, `asdict`, `vars()` all fail; hand-built `SheetLine(...)` and an unsealed subclass are refused; no `__dict__`. `rank` (negative, bool, huge-int ok), `run_date` (str, datetime, date subclass) and changed account / contact id after build are refused. |
| R6 `now` | FIXED | Required live; `now == due` refused; `due - 1 µs` builds; `due + 1 µs` refused; the same instant in another zone is compared correctly; naive or non-datetime `now` raises. Checked on DST days (3 Oct 2026, 2 Apr 2027) and across Sydney midnight (13:30 and 23:30 UTC): all `dueAt` after `now`, all `now == due` refused. |
| R7 account mismatch | FIXED | `resolved prospect belongs to a different account`. |
| R8 scrub | MOSTLY FIXED | Now redacted: `1300`/`1800`/`13 xx xx`, `ph 9999-1234`, `jo at example dot com`, `jo[at]example.com`, `jo(at)example(dot)com`, full-width `＠`, `jo @ example.com`, zero-width-split emails. Still not caught: bare 8-digit `9999 1234`, spelled-out digits, letter-spaced `j o @ e x a m p l e . c o m`, `4 1 2 3 ...`. False positive remains: `$1000000000` becomes `$[redacted]`. Opener fields containing any of these forms are refused at prepare time. Accepted limit; document it. |
| R9 history source | FIXED | email / phone in `ContactHistory.source` no longer appears in facts, JSON, packet text or `repr`. |
| R10 `asdict` | FIXED | refused. |
| R11 render options | FIXED | `bucket_limit` -1, `'x'`, `True` raise; stale and future signals are printed under `NOT USABLE AS FACTS (do not quote ...)`. |
| R12 first names | UNCHANGED (Info) | `Jr Pat Smith` gives `Jr`; `O'Neil Pat` gives `O'Neil`; surname-first names without a comma (`Ng Wei`) cannot be detected. Acceptable. |
| R13 monkeypatching / duplicates | Info | Unchanged: in-process patching is out of scope; the Phase 3 writer de-dupes on line 1 of the note. |

All earlier findings (M1-M6, L1-L5, L7, L8) re-verified still fixed: junk opener values and `none verified` as OPENER/GATEKEEPER refused, contact data in opener fields refused (including `1300` and `04xx`), markdown escaped, 80/80 skipped names listed with `bucket_limit=None`, `repr(line)` and `repr(sheet)` clean, determinism unchanged.

### Sealing mechanism (how it works and how it can be attacked)

`_SEAL = object()` is a module-level token (`callsheet.py:273`); `SheetLine` is a slotted non-dataclass; `build_sheet` sets `line._sealed = _SEAL` (`:728`); `_check_line` compares `line._sealed is _SEAL` (`:958`) and re-checks rank, `run_date`, scheduling, and that the packet's account key and SF contact id still match the entry.
- Accidental routes all fail closed: copy, deepcopy, pickle, `replace`, `asdict`, `vars`, plain construction, unsealed subclass.
- Deliberate forging works: `l._sealed = cs._SEAL`, `object.__new__(SheetLine)` plus `_SEAL`, a subclass plus `_SEAL`, or copying `genuine_line._sealed` all BUILT a payload, and `__class__` assignment to a subclass plus `_SEAL` also built. Replacing `cs._check_line` also built. Python cannot stop in-process code, so this is acceptable: the seal stops accidents, not hostile code. Even a forged line cannot bypass the do-not-contact, state-health, owner, `now` or note checks, which all run on the data.
- A `RankResult` doctored by hand (a spilled account placed in `ranked`) is turned into a sealed line by `build_sheet`; `RankResult` is the authority by design. Phase 3 should call `build_sheet` only on the object straight from `rank_accounts`.

### New findings (all Low or Info)

- **N1. LOW: the seal token is readable.** `ln._sealed` returns `_SEAL` itself, so any genuine line leaks the token. Optional: keep genuine lines in a `weakref.WeakSet` registry instead and drop the `_sealed` slot.
- **N2. LOW: due time and priority are read live from shared mutable objects.** `callsheet.py:1010-1030`. Mutating `line.ranked.local_call_time` (+1 day) or `line.ranked.score` after the build changes `dueAt` and `priority` of the payload (verified: due +1 day, priority `low`). `RankedAccount` is a mutable Phase 1 model. Cheap guard: store `due` ISO text and priority on the line at seal time and use (or compare) those.
- **N3. LOW: the case-insensitive DNC scan only lower-cases and trims.** `callsheet.py:1013-1018`. A state entry with a zero-width suffix (`'ABC-123-DEF​'`) or a casefold edge (`'İD-1'` vs `'id-1'`) is not matched (both BUILT). State ids come from Nooks and should be plain, so use `NFKC` + `casefold` + zero-width removal on both sides for robustness.
- **N4. LOW: owner-id check is a substring list.** `callsheet.py:899-920`. It can refuse a genuine id that happens to contain `test`, `demo`, `none`, `your`, etc. (fail closed, about 1 in 10,000 random ids), and accepts `'a'*20`. Best: Phase 3 reads the real id from env or config and compares exactly.
- **N5. INFO: `DryRunPayload` marker behaviour.**
  - `json.dumps` writes a plain object; the marker survives only in the note.
  - `dict(p)`, `{**p}`, `p.copy()` and a JSON round trip give a plain `dict`. `copy.copy` and `deepcopy` keep the type.
  - The `[DRY RUN] ` note prefix survives in all of those, and the anchored tag regex `^\[apac-callsheet ... #n\]$` does not match a dry-run first line, so de-dupe will not count it.
  - `dry_run=True` with a real owner id builds a marked payload. Recommendation: allow it (useful for rehearsal; dry runs still run the DNC and line checks and refuse a DNC id), and make the Phase 3 writer the single gate: refuse if `isinstance(p, DryRunPayload)` or `p["note"].startswith(DRY_RUN_PREFIX)`, with a test for each.
  - The prefix counts toward the 2,000-character cap, so a dry run is 10 characters stricter than live.
- **N6. INFO: performance.** Case-insensitive DNC scan: 40 lines against 50,000 DNC ids take 0.25 s (6 ms per line); one line against 500,000 ids takes 0.11 s. No concern.

Still UNVERIFIED (no connector): the real Nooks note limit, `prospectId` / `ownerId` formats and case behaviour, and whether resolved prospects carry `crmId`.
