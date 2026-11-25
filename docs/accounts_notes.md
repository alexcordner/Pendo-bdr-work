# accounts.py notes (task 1d)

## Flagged group: non-ANZ billing, ANZ HQ (~420 accounts)
**Decision (recommended default, not yet confirmed by Alex): INCLUDE, flag, never silently drop.**
Reasons: they are in Alex's assigned book (`Assigned_SDR__c`), HQ is in ANZ, and a foreign billing
address is a common artefact of a parent entity. Dropping would hide ~420 real prospects.
Each gets the flag `flagged_non_anz_billing_anz_hq` (plus a note) and `Account.confidence = "low"`; `list_flagged(accounts)`
returns them for review, `confidence(account)` returns `"low"`, and `summarise()` prints the count.
Their timezone already starts at the HQ state (loader rule). To exclude them later, filter on
`Account.non_anz_billing_anz_hq`.

## Other rules (updated after review 1d)
- Merge order: Salesforce, Nooks, ZoomInfo (config.sources). Keys: sf id, zoominfo id, nooks id, then
  normalised name (last resort; cross-source only; never when ids conflict; every merged id is in the notes).
- **Two rows from the same source are never merged** (e.g. two SF accounts sharing a ZoomInfo id): both kept,
  flag `possible_duplicate`, note naming the other id; touched/blocked/customer apply to each id separately.
- **Ambiguous name match** (several candidates): not merged, flag `possible_duplicate`, counted in
  `stats["name_merge_ambiguous"]`. Successful name merges are counted in `stats["name_merged"]`. Both counters
  are in `stats` only when non-zero and are always printed by `summarise()`.
- **Noise policy:** `noise_location`, `noise_local_business`, `noise_government`, `noise_vague_name` DROP only
  ZoomInfo-only rows. Salesforce/Nooks accounts (Alex's own book) are never dropped for noise: they are kept,
  flagged with the same names, noted, and `confidence = "low"`. `summarise()` lists counts and up to 5 names per flag.
  Government prefixes (`Department of`, `Government of`, `Office of the`...) count only at the start of a name.
- Real drops for every source: customer, blocked, not_anz (a real stated non-ANZ country), over_max_employees,
  under_min_employees, recently_touched. `employees=None` is never dropped (`size_unknown`).
- **Unknown country:** a Salesforce row with blank/junk/unrecognisable country (`Oz`, `Aotearoa`, `Australia (Territory)`)
  is kept with flag `country_unknown`, low confidence, no timezone (1f skips it with a reason). Only a real
  non-ANZ country with no ANZ HQ is `not_anz`. ZoomInfo/Nooks-only rows have no country and are not tested.
- The `(not ANZ)` name marker applies to ZoomInfo-only rows only and exists for the synthetic fixture.
- Flags: `zoominfo_only`, `nooks_only` (both low confidence), `flagged_non_anz_billing_anz_hq`, `country_unknown`,
  `possible_duplicate`, `noise_*`. `confidence(account)` returns `Account.confidence`.
- Cooldown: dropped when `days_since_touch < cooldown` (default `limits.cooldown_days_no_answer`); touched exactly
  `cooldown` days ago is eligible. Future-dated touches always drop (defensible; callers should pass real dates).
  `touched` values may be `date`, `datetime` or ISO string. `cooldown_days_after_connect` is the caller's job
  (pass `touched` dates, or `cooldown_days`, already adjusted for outcome).
- Salesforce ids: the loaders canonicalise to the 15-char form; `build_account_list` also normalises the keys of
  `touched`, `cooldown_until`, `blocked_account_ids` and the Nooks `_sf_account_id`, so 15- and 18-char forms match.
- `cooldown_until` (optional `Mapping[str, date]`, e.g. from 1c state) takes precedence over `touched` +
  `cooldown_days` for the ids it covers: dropped while `today < cooldown_until`. Otherwise callers should pass
  `touched` dates already adjusted for outcome (no-answer vs connect).
- Duplicates can share an `sf_account_id` (two rows with the same Id are kept and flagged `possible_duplicate`), so
  callers that key a dict on `sf_account_id` must handle collisions.
- Government rule for ZoomInfo-only rows is looser (jurisdiction word first, e.g. "Victorian Department of
  Health", "Queensland Health Department", "Tasmanian Government"); SF/Nooks only use the strict rule and are
  flagged, never dropped.
- `filters.drop_if_no_domain` is false in config and not applied here.
- Known gap (M2, not fixed): ZoomInfo-only noise outside the CBD pattern ("Bondi Florist", "Fakeville Plumbing")
  passes as `zoominfo_only` low confidence; the ranker should rank those low.
