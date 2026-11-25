# accounts.py notes (task 1d)

## Flagged group: non-ANZ billing, ANZ HQ (~420 accounts)
**Decision (recommended default, not yet confirmed by Alex): INCLUDE, flag, never silently drop.**
Reasons: they are in Alex's assigned book (`Assigned_SDR__c`), HQ is in ANZ, and a foreign billing
address is a common artefact of a parent entity. Dropping would hide ~420 real prospects.
Each gets the notes `flagged_non_anz_billing_anz_hq` and `low_confidence`; `list_flagged(accounts)`
returns them for review, `confidence(account)` returns `"low"`, and `summarise()` prints the count.
Their timezone already starts at the HQ state (loader rule). To exclude them later, filter on
`Account.non_anz_billing_anz_hq`.

## Other rules
- Merge order: Salesforce, Nooks, ZoomInfo (config.sources). Keys: sf id, zoominfo id, nooks id, then
  normalised name (last resort; cross-source only; never when ids conflict; noted on the account).
- Nooks rows may carry `_sf_account_id` (added by the caller from `filter_crmId`) to merge by SF id.
- Drop order and reasons: customer, blocked, not_anz, noise_location / noise_local_business /
  noise_government / noise_vague_name, over_max_employees, under_min_employees, recently_touched,
  malformed_row. `employees=None` is never dropped (`size_unknown`).
- Country is enforced only when Salesforce is a source (the only source that states it).
  ZoomInfo-only / Nooks-only rows are kept, tagged `zoominfo_only` / `nooks_only` + `low_confidence`.
- Local-business noise ("<Place> CBD Florist") needs a locality marker (CBD or any config drop pattern
  that is not a major city), small/unknown size, and a non-Salesforce source. A bare city name is a
  pure location for every source; "Sydney Software Pty Ltd" is kept.
- Cooldown: dropped when `days_since_touch < cooldown` (default `limits.cooldown_days_no_answer`);
  touched exactly `cooldown` days ago is eligible.
- `filters.drop_if_no_domain` is not applied: ZoomInfo free search returns no domain.
