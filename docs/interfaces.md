# Interfaces (task 1b, revised after review)

Foundation for 1c state.py, 1d accounts.py, 1e signals.py, 1f ranker.py. Import from `src.models`, `src.config`, `src.loaders`. Run tests with `python -m pytest tests/ -q` (also clean under `-W error`). Do not edit these three modules without telling the coordinator; add your own module instead.

Conventions: datetimes are timezone-aware (UTC for source times). Phones are E.164 or None. Country is `"Australia"`, `"New Zealand"` or None. Missing data is None, never invented. Frozen models change via `dataclasses.replace(obj, field=value)`.

## src/models.py
| Model | Key fields |
|---|---|
| `Account` (mutable) | `sf_account_id, name, website, country_raw, country, billing_state, billing_city, hq_state, hq_country_raw, employees, is_customer, is_sdr_target, zoominfo_id, nooks_account_id, non_anz_billing_anz_hq, source_salesforce/nooks/zoominfo, timezone, timezone_guessed, days_since_last_activity, prospecting_priority, notes: list[str], flags: list[str], confidence ('normal' | 'low')`; property `size_unknown` |
| `Prospect` (frozen) | `nooks_id, sf_contact_id, sf_account_id, nooks_account_id, name, title, email, phone_e164, opted_out_email, do_not_call` |
| `CallRecord` (frozen) | `nooks_id, prospect_id (None if unlinked), disposition_name, duration_s, started_at, to_e164, disposition_id, call_outcome, owner_id` |
| `TaskRecord` (frozen) | `nooks_id, action, status, completed, due_at, prospect_id, sequence_id, email_id, owner_id`; properties `is_manual_email` (old backlog, never touch), `in_sequence` |
| `SentEmail` (frozen) | `message_id, thread_id, recipient (dots stripped), recipient_key, sent_at (None if the search view has no date)` |
| `Scoop` (frozen) | `id, company_id (ZoomInfo id), company_name, description, published_date, types: tuple, topics: tuple, link` |
| `SignalKind` (Enum) | `funding, exec_move, new_hire, ma, product_launch, partnership, project_pain, hiring_plan, none` |
| `Signal` (frozen) | `sf_account_id, kind: SignalKind, strength (0.0-1.0, validated), source, source_date, evidence, zoominfo_id` |
| `RankedAccount` (mutable) | `account, score (0-100), score_breakdown: dict[str,float], window, local_call_time, reasons, skipped_reason, signals` |
| `RankResult` (ranker) | `ranked` (callable, on the first usable day, the ONLY accounts that may become Nooks tasks), `needs_prospect` (+ `needs_prospect_codes`), `spilled` (held for a later day), `skipped` (with reasons), `by_window`, `prospects`. Never write tasks for needs_prospect / spilled / skipped; never record them in sheet history. |
| `SheetEntry` (mutable) | `ranked, prospect, opener, fact_1, fact_2, gatekeeper, trigger` (opener fields empty until Phase 2d) |

**Join keys.** `Prospect.sf_contact_id` = Nooks `crmId`; `Prospect.sf_account_id` = `Account.sf_account_id`; `Prospect.nooks_account_id` = `Account.nooks_account_id`; `Scoop.company_id` = `Account.zoominfo_id`. For signals, `sf_account_id` wins; if None, join on `zoominfo_id`.

**`RankedAccount.local_call_time`**: aware datetime in `Account.timezone`, inside `window`, on the next working day (`next_working_windows`). Default is the window start; push later inside the window when `account.timezone_guessed`. None if unscheduled. `Account` has no call time.

## src/config.py
- `load_config(path="config/config.yaml") -> Config`; `parse_config(raw_dict)`. Raises `ConfigError` naming the key path; `FileNotFoundError` for a missing file. Rejects unknown keys, non-positive limits, positive `cooldown_penalty`, tier min > max or overlap, misspelled state keys, unknown sources.
- `Config`: `owner, territory, company_size, tiers, call_windows, working_days (weekday ints, Mon=0), limits (incl. no_contact_max_duration_s), dispositions (names: voicemail, no_answer, do_not_call, gatekeeper, wrong_number), sources, personas, signals (weights incl. sdr_target_boost), filters, paths`. All frozen.
- `canonical_country(raw)`, `is_blank_or_junk(raw)`, `normalise_au_state(raw) -> (state | None, reason)`.
- `resolve_timezone(config, country, billing_state=None, billing_city=None, hq_state=None) -> TimezoneResult(tz, source, reason, guessed)`. Chain: BillingState, HQ state, BillingCity, country default (`guessed=True`). NZ is always `Pacific/Auckland`.
- `tier_for(config, employees) -> "tier1" | "tier2" | "tier3" | None`.
- `window_bounds(config, tz_name, on_date) -> list[WindowBounds(name, start, end)]` (aware; `[]` on non-working days; holidays not modelled).
- `next_working_windows(config, tz_name, now) -> list[WindowBounds]`: first working day with a window still ahead of `now` (aware).

## src/loaders.py (pure, no network)
Raise `LoaderError` (subclass of ValueError) on malformed rows: `account_from_sf_row, account_from_nooks, account_from_zoominfo, prospect_from_nooks, build_disposition_index, call_from_nooks, task_from_nooks, sent_emails_from_gmail, scoop_from_zoominfo`. Catch it, log the skip, continue. Never raise (bad input gives None): `normalise_phone, normalise_email_for_match, resolve_country, parse_utc, disposition_id_for_name`.
- `normalise_phone(raw, account_country=None)`: handles `+`, `00`, `0011`, `(0)`, extensions, AU/NZ length checks; None when doubtful.
- `normalise_email_for_match(addr)`: lower-case, local-part dots removed. For comparison only (Gmail strips dots in returned addresses).
- `resolve_country(billing_raw, hq_raw) -> CountryResult(country, non_anz_billing_anz_hq)`.
- `account_from_sf_row(row, config=None)`, `account_from_nooks(row, sf_account_id=None)`, `account_from_zoominfo(row)`.
- `prospect_from_nooks(row, sf_contact=None, account_country=None, sf_account_id=None)`, `match_account(prospect, accounts)`.
- `build_disposition_index(rows)`, `disposition_id_for_name(index, name)`, `call_from_nooks(row, index)`, `task_from_nooks(row)`, `sent_emails_from_gmail(result)`, `scoop_from_zoominfo(row)`.

## Design decisions
1. **Country.** Billing wins if ANZ, else HQ. `non_anz_billing_anz_hq=True` only when billing held a real non-ANZ value (null/blank/junk is not flagged). Both non-ANZ gives `country=None`. Whether to include the flagged group is 1d's call (decision 5).
2. **Timezone.** NSW/ACT use the default (Sydney), confident (`guessed=False`). Only the country-default fallback is guessed. For flagged accounts billing state and city are ignored; the chain starts at HQ state. Unresolvable gives `timezone=None`.
3. **`employees=None`** is kept and flagged: note `size_unknown`, `Account.size_unknown` is True. 1d must not drop it (the size cap only applies when headcount is known); 1f gives it a neutral 7.5 icp_fit points (0.3 x 25); a KNOWN size outside every tier gets 0.
4. **`Prospect.do_not_call`** is False from the loader. 1c sets it with `replace` from `disposition_name == config.dispositions.do_not_call`. Never compare ids.
5. **Unlinked calls** have `prospect_id=None`; match on `to_e164`. Unknown disposition id gives `disposition_name=None`.
6. **Dispositions** are named in `config.dispositions` (the wrong-number name is the real `Bad Number / Wrong Number`). A test checks they all exist in the fixture list. Under-20s rule is `limits.no_contact_max_duration_s`.
7. **SDR target** (`signals.weights.sdr_target_boost`, 10) is a score boost, never a filter.
8. **Phone** prefers SF `MobilePhone`, then `Phone`. `prospecting_priority` is `SDR_Prospecting_Priority__c`, falling back to `Prospecting_Priority__c`.
9. **Gmail sent date** key is UNVERIFIED: `sent_emails_from_gmail` tries `internalDate` (epoch ms), `date`, `sentAt`; the fixture has none, so `sent_at=None`. 1c must treat None as "sent, date unknown".
10. **Persona matching** is owned by 1f (`src/ranker.py`): whole-word, case-insensitive matching of `config.personas` titles after normalisation (punctuation and the words of/the/and dropped, plural s allowed on the last word, hyphenated words stay whole), with an `ALIASES` table for equivalent titles (CTO / Chief Technology Officer, VP Product / Vice President Product, Head of CX, ...). Stale titles (Former / Ex- / Past) never match. The match with the most words wins, then primary before secondary, then config order.

## Worked examples
**1c state.py** (who was called, cooldowns, DNC):
```python
from dataclasses import replace
from src.config import load_config
from src.loaders import build_disposition_index, call_from_nooks, task_from_nooks, sent_emails_from_gmail, normalise_email_for_match
cfg = load_config()
idx = build_disposition_index(listCallDispositions_rows)
calls = [call_from_nooks(r, idx) for r in listCalls_rows]
dnc_ids = {c.prospect_id for c in calls if c.disposition_name == cfg.dispositions.do_not_call}
prospect = replace(prospect, do_not_call=prospect.nooks_id in dnc_ids)
quick = c.disposition_name == cfg.dispositions.no_answer and c.duration_s < cfg.limits.no_contact_max_duration_s
emailed = normalise_email_for_match(prospect.email) in {s.recipient_key for s in sent_emails_from_gmail(result)}
```
**1d accounts.py** (merge, filter):
```python
from src.loaders import account_from_sf_row, account_from_nooks, account_from_zoominfo
sf = [account_from_sf_row(r, cfg) for r in soql_rows]            # country, timezone, flags set
nk = account_from_nooks(row, sf_account_id=known_sf_id)           # merge into the SF Account: set nooks_account_id, source_nooks
zi = account_from_zoominfo(row)                                   # merge by Account.zoominfo_id
# keep a.country is not None and not a.is_customer; size cap only when not a.size_unknown;
# decide on a.non_anz_billing_anz_hq; log every drop into a.notes / the run log.
```
**1e signals.py** (scoops to signals):
```python
from src.loaders import scoop_from_zoominfo
from src.models import Signal, SignalKind
s = scoop_from_zoominfo(row)               # LoaderError -> log and skip
sig = Signal(None, SignalKind.FUNDING, 0.8, f"zoominfo_scoop:{s.id}", s.published_date, s.description, zoominfo_id=s.company_id)
# filter with cfg.signals.scoop_types_keep / keywords_keep / keywords_drop / max_age_days; no real trigger => SignalKind.NONE or no Signal.
```
**1f ranker.py** (score, windows):
```python
from src.config import tier_for, next_working_windows
from src.models import RankedAccount
tier = tier_for(cfg, acct.employees)                      # None => unknown size: neutral 7.5 pts; known-but-outside-tiers: 0
pts = cfg.signals.weights.sdr_target_boost if acct.is_sdr_target else 0
windows = next_working_windows(cfg, acct.timezone, now_utc)   # skip with skipped_reason if acct.timezone is None
ra = RankedAccount(acct, score=..., score_breakdown={...}, window=windows[0].name, local_call_time=windows[0].start)
```

## UNVERIFIED (added by coordinator, task 1e)
- **ZoomInfo intent rows** (`intent_to_signals`): the row shape `{company_id, topic, signal_score 60-100, audience_strength A-E, recorded_date?}` is a placeholder and has NOT been checked against the live connector. Intent is capped at 0.3, can never be the best signal when a real scoop exists, and its evidence reads "interest signal, not a dated event". The call sheet must label it that way.
- **Scoop pull window**: people-move scoops (Executive Move / New Hire) are kept up to 90 days (others 45, partnership / hiring plan / intent 30), so the Phase 2b ZoomInfo pull must request 90 days. `config.signals.max_age_days` is now only a fallback for kinds missing from `KIND_MAX_AGE_DAYS` in `src/signals.py`.
- **Ambiguous roles**: a product-role scoop that cannot be confirmed as a digital product role is kept with `ScoopVerdict.ambiguous=True` and strength x0.7; the sheet should show "role ambiguous: check it is a digital product role".


## Phase 2 integration changes (task 2f)
Signatures that changed or were added when the seven Phase 2 branches were merged and wired. Full detail is in each module's notes file.

- `accounts.build_account_list(..., contact_names_by_account: Mapping[str, Collection[str]] | None = None)` -> `AccountListResult` now also has `duplicate_pairs`. `pipeline.contact_names_by_account(sf_contacts)` builds the mapping (SF account id -> casefolded 'first last' names, evidence only, never logged). New note wording on every member of a group: `possible_duplicate group dup-<id> (<evidence>): also '<name>' (<ids>); ...; not merged, collapse on the sheet`.
- `accounts.duplicate_groups(accounts, contact_names_by_account=None) -> list[list[Account]]` (reads the group notes; detects fresh only when nothing is flagged). `accounts.flag_probable_duplicates` flags in place.
- `callsheet.build_sheet(...)` collapses duplicate groups by `accounts.duplicate_groups` (group notes), only when some account is flagged. `callsheet.probable_duplicates(a, b)` = same group. The old note parser (`_dup_targets`) is gone.
- `callsheet.PacketSignal` has `notes: tuple[str, ...]` (= `signals.signal_notes`); `to_dict()` gives a list. `ResearchPacket` has `must_not_phrases: list[str]` (literal phrases textcheck rejects, derived from the notes: `callsheet.NOTE_BANNED_PHRASES`) and its `to_dict()` carries it. `evidence` and `available_facts` text no longer contain the `(note)` suffixes; the notes are in `signals[].notes`, `verdict_reason`, `facts_note` and `must_not_claim`.
- `signals`: `ScoopVerdict.factor / .notes / .event_date / .same_deal_as`, `signal_notes(signal) -> list[str]`, `is_ambiguous(signal)`, `max_age_for(kind, config)`.
- `ranker.NEEDS_PROSPECT_CODES = (no_prospects_on_account, no_persona_match, persona_match_no_phone, junk_title)`. `RankedAccount.reasons` names the chosen prospect by FIRST name only (the reasons end up in the run log).
- `pipeline.PipelineResult.gmail_uncovered`; `stats` gains `needs_prospect_junk_title, chosen_personal_email, possible_duplicate_accounts, possible_duplicate_groups` (log block `== FLAG COUNTS ==`).
- `textcheck.check_opener_block(text, packet_dict, voice_cfg)` / `check_email_draft(subject, body, packet_dict, voice_cfg)` read `must_not_phrases`, `must_not_claim`, `signals[].status|ambiguous|evidence`, `ambiguity_flags`, `available_facts`, `has_real_trigger` exactly as `ResearchPacket.to_dict()` emits them (a test pins this). `email_ok` / `email_reason` are NOT read by textcheck: Phase 3 must call `callsheet.email_draft_allowed(line)` before any draft.
- `scripts/merge_pages.py` and `scripts/dry_run_sheet.py` now write only through `scripts/safeout.py` (under `APAC_OUTPUT_ROOT` or `output/`, never through a symlink, refused when git does not ignore the path). Exit code 2 when refused.
