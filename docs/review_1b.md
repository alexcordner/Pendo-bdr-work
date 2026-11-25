# Review of task 1b (branch `phase1/1b-models`, commit 9db4b14)

Reviewer: independent session, fully offline and read-only on the worker's files. No connector calls. All inputs below are invented strings or the repo's synthetic fixtures. Probe script lived in the scratchpad, not the repo.

## Verdict: PASS WITH FIXES

The foundation is clean, small and well tested, and the timezone and country rules are right. Two real correctness bugs (phone `(0)` handling, US `WA` read as Western Australia) should be fixed before 1c to 1f build on it. Config validation is thinner than the brief asked for. The interface doc is missing a few things the parallel workers will need.

## 1. Test runs
| Check | Result |
|---|---|
| `pip install -e .` | OK (it creates `src/apac_callsheet.egg-info/`, which `.gitignore` does not cover; I deleted it) |
| `python -m pytest tests/ -q` | **75 passed** |
| `python -W error -m pytest tests/ -q` | **75 passed**, no warnings |
| mypy (informational) | 1 error only: missing `types-PyYAML` stubs. No type errors in the code. |
| pyright | not installed, UNVERIFIED |

## 2. Correctness (PASS / FAIL by item)

### Timezones: PASS
Probed `resolve_timezone` (src/config.py:342-369): QLD to Brisbane, NT to Darwin, WA to Perth, SA to Adelaide, VIC to Melbourne, TAS to Hobart, NSW and ACT to Sydney, NZ to Auckland for any state input including nonsense. `zoneinfo` is used, never fixed offsets. I checked offsets in Jan and Jul 2026: Sydney 11h and 10h, Brisbane 10h both, Perth 8h, Darwin 9.5h, Adelaide 10.5h and 9.5h, Auckland 13h and 12h. Mixed case and padded input work (`" Qld "`, `"New South Wales "`). City-in-state-field and `Gold Coast`, `Canberra` work. Chain order confirmed (`WA` beats city `Sydney`; HQ state beats city).
Known gaps (low): `Broken Hill` (Adelaide time), Lord Howe, and punctuated forms `N.S.W.`, `Vic.`, `Q'land`, `West Aus` fall to the guessed Sydney default. Acceptable, they are flagged `guessed=True`.

### Country rule: PASS with minor items
Tested `resolve_country` (src/loaders.py:59-73): `" au "`, `"Aus"`, `"AUS"`, `"Au"`, `"NZ "`, `"NZL"`, `"new zealand"`, null billing with `"Australia "` HQ, blank billing with `"AU"` HQ all give the right country. US billing with AUS HQ gives Australia plus the flag; null or blank billing with ANZ HQ is not flagged. Failures:
- `"New  Zealand"` (double internal space) gives None (src/config.py:185). Low.
- `"N.Z."`, `"Australia."`, `"Aotearoa"` give None. Low, arguably correct.
- A non-string billing value (e.g. `5`) with ANZ HQ raises `AttributeError` at src/loaders.py:71. Low; SOQL rows should not do this.
- A billing value of junk like `"N/A"` with ANZ HQ sets the non-ANZ flag (src/loaders.py:71). Low.

### Billing state collision for flagged accounts: **FAIL (medium-high)**
src/loaders.py:115 passes `billing_state` to the AU state normaliser regardless of the billing country. For the flagged group (non-ANZ billing, ANZ HQ, about 420 accounts per decisions.md #5) a US billing state of `WA` resolves to `Australia/Perth` with `guessed=False`. Probe: BillingCountry "United States", BillingState "WA", HQ_State "Victoria" gave `Australia/Perth (state)`. Fix: when `account.non_anz_billing_anz_hq` is True, skip billing state and billing city (they belong to the foreign address) and start the chain at HQ state. Add a test with `WA`/Seattle + Victoria HQ.

### Phone normaliser: **FAIL on several edge cases**
Rules at src/loaders.py:20-49. My cases (country arg in brackets; "expected" is what a dialler needs):

| Input | Result | Expected | Verdict |
|---|---|---|---|
| `0400 000 001` [AU] | +61400000001 | same | pass |
| `(02) 9000 0001` [AU] | +61290000001 | same | pass |
| `(04) 0000 0001` [AU] | +61400000001 | same | pass |
| `+61 (0)4 0000 0001` [none or AU] | **+610400000001** | +61400000001 | **FAIL, wrong number** |
| `61 (0)400 000 001` | **+610400000001** | +61400000001 | **FAIL** |
| `0061 4 0000 0001` | +61400000001 | same | pass |
| `+61 400 000 001 ext 12` | **+6140000000112** | +61400000001 (drop ext) or None | **FAIL, digits appended** |
| `04000 00001 x123` | **+61400000001123** | None or strip ext | **FAIL** |
| `+61400000001;ext=1` | **+614000000011** | strip ext | **FAIL** |
| `0011 61 400000001` (real AU international prefix) | **+1161400000001** | +61400000001 | **FAIL** |
| `0000000000` [AU] | **+00000000** | None | **FAIL, garbage passes** |
| `+0061400000001` | **+0061400000001** | None | **FAIL** (country code cannot start with 0) |
| `61000000000` | **+61000000000** | None | **FAIL** (AU numbers cannot start 61 0) |
| `0400000001/0400000002` | None | None | pass (conservative) |
| `0400` / `+61 4` / `9000 0001` | None | None | pass |
| `1300 000 000`, `1800123456` [AU] | None | acceptable (13/1800 are not dialable mobiles) | pass |
| `021 000 0001` [NZ] | +64210000001 | same | pass |
| `021 000 0001` [AU] | +61210000001 | ambiguous, cannot know | note |
| `+64 21 000 0001` [AU] | +64210000001 | explicit + wins | pass |
| `64210000001` [NZ] | +64210000001 | same | pass |
| `0800 123 456` [NZ] | +64800123456 | same | pass |
| `+1 415 555 0100` [AU] | +14155550100 | kept | pass |
| `tel:+61400000001` | +61400000001 | same | pass |
| fullwidth digits | None | acceptable | pass |
| 18-digit number | None | None | pass |

Fixes: (a) strip `(0)` / a `0` immediately after a 61 or 64 country code; (b) cut anything after `ext`, `x`, `;` or `#` before digit extraction, or return None if present; (c) require the first digit after `+` is 1-9; (d) per-country length check for 61 and 64 (AU 11 digits, NZ 10-12, and the next digit not 0); (e) either handle `0011` or leave it None, never mangle it; (f) reject all-zero. The existing fixture vectors (tests/fixtures/phone_formats.json) cover none of these, so add them.

### Disposition resolution: PASS with small items
Resolution is by lookup (src/loaders.py:155-204); `test_ids_are_not_hard_coded` genuinely remaps ids. Unknown id gives `disposition_name=None` and the id is kept; missing or null `callDisposition` is handled. A dispositions list with duplicate names where one is archived/legacy returns the current id (good). Small items:
- `disposition_id_for_name` is case and whitespace sensitive (`"left voicemail"` gives None). Low; a case-insensitive match would be safer for 1c.
- `build_disposition_index` raises `KeyError` on a row missing `id` or `name` (src/loaders.py:158). Low.
- `call_from_nooks` raises `ValueError` on a non-numeric duration (src/loaders.py:198) while treating a bad time as `ValueError` too; fine, but the doc should say which functions raise.
- `scoop_from_zoominfo` raises `KeyError` on a type or topic entry without the `type`/`topic` key (src/loaders.py:221-222). Low.
- A call row whose disposition arrives only as a name (no id) resolves to `None`. Fine for the documented shape.

## 3. Rule alignment: PASS
- No hard-coded disposition ids or user ids in `src/` (grep for the owner id, `disp-`, SF id patterns: none). The owner id exists only in config/config.yaml, which CLAUDE.md allows.
- No network, socket, HTTP, mail or subprocess code in `src/`. Only `open()` is the config file read.
- No secrets, no writes, no sends. No scoring/ranking/filtering/state logic in 1b (the loaders keep customers and non-ANZ rows as Accounts, as briefed).
- `Prospect.do_not_call` is always False from the loader (src/loaders.py:142, documented at src/models.py:68-70). Note `Prospect` is frozen, so 1c must use `dataclasses.replace`; say so in the interfaces doc.
- Fixtures are synthetic (`FAKE-*` ids, example.com).

## 4. Config
Real `config/config.yaml` loads on this branch, and it is byte-identical to the Phase 0 and `main` (a53e3f3) version, so it loads there too. Section coverage: owner, territory, company_size, tiers, call_windows, limits, sources, personas, signals (incl. weights), filters, paths are all typed. Error messages name the key path (checked: missing section, wrong type, bool-as-int, bad IANA name, bad HH:MM, reversed window, empty file, bad YAML, null section).

Silently ACCEPTED when it should not be (src/config.py:205-288):
- Typo in a state key, e.g. `Queenland: Australia/Brisbane`; the zone is never used. Validate state keys against the canonical set (`_AU_STATE_ALIASES` values).
- Negative or zero `limits.*` (`daily_call_cap: -5`, `0`), `signals.max_age_days: 0`.
- Positive `cooldown_penalty` (comment says negative), negative other weights.
- Tier with min greater than max (`[2000, 200]`); missing `tier1`; overlapping tiers. Tier open-ended (`max_employees: None`) cannot be expressed in YAML because the loader demands two ints.
- Unknown keys anywhere (`limits.daily_cal_cap: 3`), so a typo in an optional key vanishes. Rejecting unknown keys would catch drift.
- `sources` values not checked against `salesforce|nooks|zoominfo`; `priority_countries` not checked against ANZ; `owner.email` not checked; empty `paths.*` accepted.
- `NZ` accepts extra state keys that are never read.
Missing for later tasks (not in typed Config; workers will hard-code them in modules):
- Disposition names `"Do Not Call"`, `"Left Voicemail"`, `"No Answer"` (decisions 2 and 8), and the 20-second no-contact threshold. Add a `dispositions:` / `limits.no_answer_min_duration_s: 20` section.
- An SDR-target boost weight (decision 6 says boost). `signals.weights` has no key for it.
- Working days (Mon-Fri is only a YAML comment), and a tiers helper `tier_for(employees)`.
- `icp.md` is prose; fine.
Low: `Limits(**{k: ... for k in Limits.__dataclass_fields__})` (src/config.py:260) is compact but couples YAML key names to field names; acceptable and gives clear errors.

## 5. Interfaces: read as the 1c / 1d / 1e / 1f worker
Ambiguous or missing, with suggested additions:
1. **No loaders for Nooks accounts, ZoomInfo search_companies rows, Nooks tasks, SF tasks or Gmail sent mail.** 1d needs source merge (`Account.source_nooks`, `source_zoominfo`, `nooks_account_id` are defined but nothing sets them); 1c needs last-touched dates and `email_cooldown_days` (needs sent-mail data). Recommend: `account_from_nooks(row)`, `account_from_zoominfo(row)` and a small `TaskRecord`/`SentEmail` model plus loaders, or state explicitly that 1c/1d own them.
2. **Prospect-to-account join.** `Prospect.sf_account_id` is set only from the SF Contact row (src/loaders.py:136), so a Nooks-only prospect has `sf_account_id=None` and only `nooks_account_id`; `Account.nooks_account_id` is never populated, so the join fails. Document the join key and fill both ids.
3. **Last-touched dates.** `Account.days_since_last_activity` is an SF integer snapshot, not a date. The cooldown rules (`cooldown_days_no_answer`, `cooldown_days_after_connect`) need per-account last call and last connect datetimes; that is 1c's output, so say how it is handed to 1d/1f (a `dict[sf_account_id, AccountState]` type in models?).
4. **Employees floor and tiers.** `company_size.min_employees` exists, but `employees=None` handling (include, drop, or low score?) is undefined. Add one line each for 1d and 1f. `tier_for(employees)` helper should live in config.
5. **SDR target boost.** `Account.is_sdr_target` exists, no weight; see section 4.
6. **Phone presence.** Only `Prospect.phone_e164`. The ranker's `named_prospect_with_phone` weight needs the chosen prospect per account; say which prospect (persona order primary then secondary, `max_per_account_per_run`) and who selects it (1f?), and that titles must be matched against `config.personas` (no helper exists, so substring vs exact match is undefined).
7. **Local call window computation.** Config windows are naive `time` objects; nothing converts them to an aware window for `Account.timezone`. `RankedAccount.local_call_time` is ambiguous (start of window? suggested time? `guessed` means "later in window" but no rule). Weekends and holidays not mentioned. Recommend a shared pure helper `window_bounds(config, tz_name, on_date) -> list[(name, start_dt, end_dt)]` in config.py.
8. **Signals.** `Signal.kind` values and `strength` semantics are only examples (src/models.py:107-108); 1e and 1f need a fixed `kind` set and recency handling. `Signal` has no `topics`/`types`, and `Scoop` to `Signal` happens by `zoominfo_id` only; if the account has no `zoominfo_id`, the signal is orphaned (decision: which wins, `sf_account_id` or `zoominfo_id`?).
9. **Mutability.** Account and RankedAccount are mutable (fine); Prospect and CallRecord frozen, so 1c must `dataclasses.replace` for `do_not_call`. Say so.
10. **Exceptions.** Doc does not list which loaders raise (`call_from_nooks`, `scoop_from_zoominfo`: ValueError on no date; `build_disposition_index`: KeyError). Workers need to know whether to catch and log (CLAUDE.md rule 5).
11. **Doc vs code mismatch (minor).** Doc says `Tier(min_employees, max_employees, note)` correct; doc says config has "no abbreviations for NSW/ACT in `territory.timezones` by design", but ACT as a canonical state key is absent from config and works only by default fallback; fine, but a future reader editing config could add `"New South Wales"` and it would work, while `NSW` would not. Not a bug.
12. `Account.prospecting_priority` falls back across two SF fields (src/loaders.py:108) without the doc saying so; note it.

## 6. Code quality: PASS
Small, commented, stdlib plus PyYAML, Python 3.11 syntax, frozen vs mutable choices sensible, no dead code found. Minor: `Disposition` and `CountryResult` live in loaders.py while other types live in models.py (acceptable, they are loader-internal); `parse_utc` assumes naive times are UTC (documented); `_int_or_none` accepts `"1,200"` as None silently.

## Findings by severity
| # | Sev | Where | Finding |
|---|---|---|---|
| 1 | High | src/loaders.py:36-49 | `+61 (0)4...` gives `+610400000001` (wrong number); trunk `0` after country code not stripped |
| 2 | Med-high | src/loaders.py:115 | US `WA` billing state on an ANZ-HQ account resolves to Perth with `guessed=False` |
| 3 | Medium | src/loaders.py:28-49 | extensions append digits; `0011` mangled; all-zero / leading-zero country codes accepted |
| 4 | Medium | src/config.py:205-288 | no range checks, no unknown-key rejection, state-key typos silently ignored |
| 5 | Medium | docs/interfaces.md | no Nooks/ZoomInfo account loaders, no task/sent-mail models, prospect-account join incomplete, no window helper |
| 6 | Low | src/config.py (Config) | disposition names, 20s threshold, SDR boost weight, working days not in config |
| 7 | Low | src/loaders.py:71,158,198,221 | non-string billing, missing disposition keys, bad duration, bad scoop types raise raw exceptions |
| 8 | Low | .gitignore | add `*.egg-info/` |

## What I could not verify
No live data, so the real shapes in docs/connector_notes.md are taken on trust. The `Do Not Call` and `Left Voicemail` names are assumed from fixtures. I did not test on Windows or with another tzdata version.
