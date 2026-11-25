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
