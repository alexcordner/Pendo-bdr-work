# Interfaces (task 1b)

Foundation for 1c state.py, 1d accounts.py, 1e signals.py, 1f ranker.py. Import from `src.models`, `src.config`, `src.loaders`. Run tests with `python -m pytest tests/ -q` from the repo root. Do not edit these three modules without telling the coordinator; add your own module instead.

Conventions: datetimes are timezone-aware (UTC for source times). Phones are E.164 or None. Country is `"Australia"`, `"New Zealand"` or None. Missing data is None, never invented.

## src/models.py
| Model | Key fields |
|---|---|
| `Account` (mutable) | `sf_account_id, name, website, country_raw, country, billing_state, billing_city, hq_state, hq_country_raw, employees, is_customer, is_sdr_target, zoominfo_id, nooks_account_id, non_anz_billing_anz_hq, source_salesforce/nooks/zoominfo, timezone, timezone_guessed, days_since_last_activity, prospecting_priority, notes: list[str]` |
| `Prospect` (frozen) | `nooks_id, sf_contact_id, sf_account_id, nooks_account_id, name, title, email, phone_e164, opted_out_email, do_not_call` |
| `CallRecord` (frozen) | `nooks_id, prospect_id (None if unlinked), disposition_name, duration_s, started_at, to_e164, disposition_id, call_outcome, owner_id` |
| `Scoop` (frozen) | `id, company_id (ZoomInfo id), company_name, description, published_date, types: tuple, topics: tuple, link` |
| `Signal` (frozen) | `sf_account_id, kind, strength (0.0-1.0), source, source_date, evidence, zoominfo_id` |
| `RankedAccount` (mutable) | `account, score (0-100), score_breakdown: dict[str,float], window, local_call_time, reasons, skipped_reason, signals` |
| `SheetEntry` (mutable) | `ranked, prospect, opener, fact_1, fact_2, gatekeeper, trigger` (opener fields empty until Phase 2d; names mirror skills/opener/SKILL.md) |

Joins: Nooks `prospect.crmId` = `Prospect.sf_contact_id`; `Prospect.sf_account_id` = `Account.sf_account_id`; `Scoop.company_id` = `Account.zoominfo_id`.

## src/config.py
- `load_config(path="config/config.yaml") -> Config`; `parse_config(raw_dict) -> Config`. Raises `ConfigError` (message names the key path); `FileNotFoundError` for a missing file.
- `Config` sections: `owner, territory (priority_countries, secondary_countries, timezones[country] -> CountryTimezones(default, states)), company_size (min/max_employees), tiers[name] -> Tier(min_employees, max_employees, note), call_windows: tuple[CallWindow(name, start: time, end: time)], limits, sources, personas (primary, secondary), signals (scoop_types_keep, keywords_keep, keywords_drop, max_age_days, weights: SignalWeights), filters (drop_name_patterns, drop_if_no_domain), paths`. All frozen.
- `canonical_country(raw) -> str | None` (AU/NZ, any case).
- `normalise_au_state(raw) -> (canonical_state | None, reason)`: abbreviations, case, a few cities/misspellings; junk -> None.
- `resolve_timezone(config, country, billing_state=None, billing_city=None, hq_state=None) -> TimezoneResult(tz, source, reason, guessed)`. Chain: BillingState -> HQ state -> BillingCity -> country default (`guessed=True`). NZ always `Pacific/Auckland`. Non-ANZ -> `tz=None`.

## src/loaders.py (pure, no network)
- `normalise_phone(raw, account_country=None) -> str | None`
- `resolve_country(billing_raw, hq_raw) -> CountryResult(country, non_anz_billing_anz_hq)`
- `account_from_sf_row(row, config=None) -> Account` (sets timezone + notes when `config` is given). Every row becomes an Account, including customers and non-ANZ ones: 1d does the filtering.
- `prospect_from_nooks(row, sf_contact=None, account_country=None) -> Prospect`
- `build_disposition_index(rows) -> dict[id, Disposition(id, name, call_outcome, current)]`; `disposition_id_for_name(index, name)`
- `call_from_nooks(row, dispositions_index) -> CallRecord`; `parse_utc(str)`
- `scoop_from_zoominfo(row) -> Scoop`

## Design decisions to rely on
1. **Country rule.** Billing wins if ANZ; else HQ. Account in scope with `country` set from HQ; `non_anz_billing_anz_hq=True` only when billing had a non-ANZ value (null billing + ANZ HQ is not flagged). Non-ANZ on both -> `country=None` and a note. Decision 5 (include or drop the flagged group) is left to 1d.
2. **Timezone.** NSW/ACT use the country default (Sydney) but count as a confident state match (`guessed=False`). Only the country-default fallback is `guessed=True` (ranker: call later in the window). Unresolvable -> `timezone=None`.
3. **`Prospect.do_not_call`** is always False from the loader: no read flag exists. 1c sets it from a `Do Not Call` disposition (`CallRecord.disposition_name == "Do Not Call"`). Never compare disposition ids.
4. **Unlinked calls** have `prospect_id=None`; match on `to_e164`. Unknown disposition id -> `disposition_name=None`.
5. **Voicemail** = `disposition_name == "Left Voicemail"`; never infer from duration. Under-20s `No Answer` rule is 1c's.
6. **Phone** prefers SF `MobilePhone`, then `Phone`. A national number with no account country returns None.
7. **Signal.strength** is 0.0-1.0; the ranker multiplies by `signals.weights.trigger_strength`.
8. Config has no abbreviations for NSW/ACT in `territory.timezones` by design; the lookup uses the default.
