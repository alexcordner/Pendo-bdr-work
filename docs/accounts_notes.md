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
- **Cooldown contract (prefer `cooldown_until`):** callers pass `cooldown_until` (from state:
  `state.cooldown_until` / `cooldown_until_by`). When it is given (even `{}`), `touched` is ignored, so an
  email-only touch never drops an account (emails do not start call cooldowns). `touched` + `cooldown_days`
  is the legacy path for tests and callers without state. Dropped while `cooldown_until` is after the reference
  date; equal is eligible.
- `call_date` (optional): the first date the account would be called (2c passes it; account-local semantics are
  the caller's job). Reference date is `call_date` if given, else `today`, so a Wed-night run with `call_date=Thu`
  keeps an account whose cooldown ends Thu.
- `group_by_zoominfo_id(accounts)` returns every account per ZoomInfo id (possible_duplicate ones included) so the
  signals join can attach one trigger to all of them.
- Duplicates can share an `sf_account_id` (two rows with the same Id are kept and flagged `possible_duplicate`), so
  callers that key a dict on `sf_account_id` must handle collisions.
- Government rule for ZoomInfo-only rows is looser (jurisdiction word first, e.g. "Victorian Department of
  Health", "Queensland Health Department", "Tasmanian Government"); SF/Nooks only use the strict rule and are
  flagged, never dropped.
- `filters.drop_if_no_domain` is false in config and not applied here.
- Known gap (M2, not fixed): ZoomInfo-only noise outside the CBD pattern ("Bondi Florist", "Fakeville Plumbing")
  passes as `zoominfo_only` low confidence; the ranker should rank those low.

## Optional `touched` (N3)
`build_account_list(sf, nooks, zoominfo, config, touched=None, today=None, ...)`: `touched` is optional (None = no touches, the state-based path never needs it) and ignored whenever `cooldown_until` is passed. `today` may be omitted when `call_date` is given; if both are missing it raises `ValueError("... today or call_date ...")`. Existing positional calls `(..., config, touched, today, ...)` are unchanged.


## Probable duplicates (phase2/fix-dupes)
Two Salesforce records for one company (e.g. "Catapult" and "Catapult Communications") are FLAGGED, never merged or
dropped: flag `possible_duplicate` plus a note with a group id (`dup-<smallest id>`), the reason and the other
account's name and ids. Rules (all deterministic, all-accounts, any source):
- (a) same website registrable domain (`www.`, scheme, path, port removed; `.com.au`/`.co.nz`-style suffixes handled).
  Shared hosts are ignored: linkedin, facebook, twitter/x, instagram, youtube, google (sites), wix/wixsite, weebly,
  squarespace, wordpress, blogspot, github.io, business.site, linktr.ee, medium, godaddysites, myshopify, netlify,
  herokuapp, azurewebsites, pages.dev, tiktok, pinterest, bit.ly.
- (b) name containment: after removing legal suffixes and filler (pty, ltd, limited, inc, group, holdings,
  australia/australian, nz/new zealand, the, and, of, co, company, corp...), the shorter name's tokens are a
  whole-word sequence inside the longer one (or equal). The shorter name needs at least one token of 2+ characters
  that is not a weak/common word (post, office, bank, health, energy, services, digital, media, ...), so
  "Australia Post" vs "Post Office" or "Alpha Digital" vs "Beta Digital" do NOT match, while "Catapult" vs
  "Catapult Communications" and "Westpac" vs "Westpac Institutional Bank" do. Partial words never match.
- (c) shared sf / zoominfo / nooks id.
- Cap: a name (or domain) group of more than 6 accounts is a common word, not a company. Its members get
  `large_name_group` (or `large_domain_group`) and are NOT flagged as duplicates.
- Groups are connected components over the edges, so a domain link can join a name group. Chains through a
  distinctive token are possible; the 6-account cap limits the damage. Only flags are set (no model change).
- `duplicate_groups(accounts) -> list[list[Account]]` (config-free, input order inside a group, groups ordered by
  first member, oversized groups excluded) is for the sheet step: keep the higher-scoring account, mention the
  other in the 'why' line. `flag_probable_duplicates(accounts)` does the in-place flagging (idempotent).
- `summarise()` prints `possible duplicate pairs: N (top 10 listed with names)` and large-group counts.
- Drops still apply: a dropped twin (e.g. a customer) still appears in its kept twin's note.
