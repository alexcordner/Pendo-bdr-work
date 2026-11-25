# Review of task 1d (`src/accounts.py`), branch `phase1/1d-accounts`

Reviewer: independent session, offline, read-only (no connector calls, worker files untouched). All names below are invented.
Python used: 3.13.18 (code targets 3.11+; nothing version-specific spotted).

## Verdict: PASS WITH FIXES

The engine is sound: deterministic, non-mutating, well tested, and it logs most drops. The noise filter is the problem. It was tuned to the synthetic fixtures and wrongly drops legitimate accounts from Alex's own Salesforce book. There is also one silent-loss merge path. Fix the items marked **High** before 1g.

## Checklist

| # | Item | Result |
|---|---|---|
| 1 | pytest plain / `-W error`; determinism | **PASS.** 220 passed plain, 220 passed with `-W error` (78 in `tests/test_accounts.py`, also stable under PYTHONHASHSEED 3 and 77). Shuffled SF/Nooks/ZI inputs (5 seeds, 50 accounts with cross-source merges) gave identical accounts, notes and drops; PYTHONHASHSEED 0/1/12345 gave identical probe output. |
| 2 | False positives | **FAIL** (see below). |
| 3 | Country / ANZ | **PASS WITH FIXES** (below). |
| 4 | Dedupe | **FAIL on one path** (High finding H2). |
| 5 | Touched / cooldown / blocked | **PASS** (minor notes). |
| 6 | `drop_if_no_domain` | **PASS** (not applied); config needs fixing. |
| 7 | Mutation, `summarise()` | **PASS** (minor). Deep-compared SF/Nooks/ZI rows before and after: unchanged. Iterators and `frozenset` blocked set work. |
| 8 | Code quality | **PASS.** No network, no secrets, imports only `src.config`, `src.loaders`, `src.models` (accounts.py:36-44). Short and commented. One half-dead idea (`_CITY_WORDS`, see M2). |

## Findings

### High
**H1. Noise filters drop Alex's own SF accounts (accounts.py:117-147, 328-331).** Only the local-business rule exempts Salesforce (accounts.py:139). Pure-location, government and vague-name rules apply to SF rows too. Wrongly dropped SF-sourced fakes from the sweep:
- `Fake Government Services Pty Ltd`, `Fake Department of Health Pty Ltd`, `Fake City Council Software Pty Ltd`, `Fake Office of the Chief Digital Officer Pty Ltd` (regex `_GOV_RE`, :74-76, matches words in private company names).
- `Fake Public Service Credit Union` (`public service`), `Fake Ministry of Sound Media Ltd` (`ministry of`), `Fake Parliament Hill Software` (`parliament`).
- `Sydney CBD Software Pty Ltd`, `Perth CBD Wealth Pty Ltd` (:136: `marker_hit and len(others) <= 1` calls these "just a place"; a Sydney CBD-named software company is dropped).
- `Fake Unknown Holdings`, `TBD Pty Ltd` (:145, any placeholder word anywhere).
- `Brisbane City Council` and `Department of Fake Transport Digital Services` (public bodies with digital services that can be real buyers; also inconsistent: `Auckland Council` is kept, `Brisbane City Council` dropped).
The drops are logged, so nothing is silent, but a human has to read the log to notice. The reasons are guesses made from a name.

**H2. A ZoomInfo id shared by two SF accounts silently collapses them (accounts.py:249-254, 160-177).** Duplicate SF accounts sharing a `DOZISF__ZoomInfo_Id__c` are common. Probe: SF `A1 "Fake One"` and SF `A2 "Fake Two"` both with ZI id `Z9` give ONE kept account (A1). A2 is not in `dropped`, and the note on A1 says only "merged salesforce row by zoominfo_id" without naming A2. Consequences: A2's `touched` entry (keyed by A2) is ignored, a `blocked` A2 is not detected, and an A2-specific customer flag is only carried by the OR. Same shape for two SF rows with the same SF id. Fix: do not auto-merge two rows from the SAME source (SF/SF). Keep both and add a `possible_duplicate_of:<id>` note, or at least record every merged id on the survivor (`merged_ids`) and check touched/blocked against all of them.

### Medium
**M1. Name-merge can false-merge and is not auditable (accounts.py:255-262, 176-177).** Last-resort name merge only checks "different source and no id conflict". A ZI row with no SF id and the same normalised name as an SF account always attaches to it, even if it is a different company ("Fake Pacific Pty Ltd" in SF vs an unrelated "Fake Pacific Limited" in ZI). With two SF accounts of the same normalised name, the ZI row silently picks the first by sort order (probe: attached to A1, A2 left unmerged). The merge is only visible as a note on the account; `result.stats` and `summarise()` do not count name-merges. A name-merged ZI row that happens to hit a customer SF account makes the account vanish as `customer` with a detail that does not mention the merge. Fix: if more than one candidate matches, do not merge (log ambiguous), and add counts `merged_by_name` / `ambiguous_name_match` to stats and `summarise()`.

**M2. Place detection only knows the six config cities (accounts.py:127-137, 65-69).** `_CITY_WORDS` only helps classify which pattern is a locality marker; it never adds places. ZoomInfo-only noise that slips through: `Wellington`, `Sydney, Australia`, `Bondi Florist`, `Fakeville Plumbing`, `Fakeville Primary School`. The config-driven design is fine, but the real noise cases ("<Place> Florist", plumbing, learning centres) are only caught when the name has CBD. Not a data-loss risk, just noise reaching the sheet; the ranker should rank `zoominfo_only` low (it is already tagged).

**M3. SF accounts with unknown country are dropped as `not_anz` (accounts.py:328-329).** A book account (Assigned_SDR = Alex) with BillingCountry and HQ both null is dropped with the detail "is not Australia/New Zealand", which is wrong: country is unknown. Junk like `Oz`, `Aotearoa`, `Australia (Territory)` as the only value is dropped the same way. Recommend: country null/junk on an SF row = keep, note `country_unknown`, low confidence. Drop `not_anz` only when a real non-ANZ country is stated (e.g. both billing and HQ are real non-ANZ values). Decision 4 says the test is "AU/NZ", so this is a judgement for Alex.

**M4. `(not ANZ)` in the name (accounts.py:77, 330-331).** This regex exists only because the synthetic fixture (`FAKE-ZI-9009`) has that marker. Real ZoomInfo names will not carry it. It is harmless for ZI rows but would drop an SF-sourced ANZ company whose name happens to contain it. Recommendation: delete it, or apply to ZI-only rows. Real not-ANZ detection for ZI-only rows is impossible with free search (id and name only), which the notes already admit.

### Low
- **L1.** `touched` values that are ISO strings raise `TypeError` (accounts.py:341); only `date`/`datetime` are handled (:312). Validate or coerce, or document.
- **L2.** Future-dated touches always drop (negative days < cooldown, :341). Defensible, but a clock-skew or timezone date would hide an account for days. Document it, or cap with `abs`.
- **L3.** SF 15 vs 18 char ids are not normalised. UNVERIFIED whether all callers use the same form; a mismatch silently defeats `touched` / `blocked` / `_sf_account_id` merge.
- **L4.** Only `cooldown_days_no_answer` is applied. `cooldown_days_after_connect` (14) is presumably 1c's job. Confirm the caller passes `touched` dates already adjusted for outcome.
- **L5.** Customer flag survives merge by OR (:171), which is right, but the dropped record's detail "Current_Customer__c is true" does not say if it came from a merged row.
- **L6.** `summarise()` does not print `nooks_only` count, `merged_by_name` count, or the `low_confidence` total. Sample names per reason are capped at 5 (good).

## False-positive sweep (69 invented names)
Each name run as an SF row (Billing Australia), a Nooks row (`numEmployees`) and a ZoomInfo-only row (id and name only, so employees do not apply). Today 2026-11-25, empty `touched`.

| # | Fake name | emp | SF | Nooks | ZI-only |
|--|--|--|--|--|--|
| 1 | Brisbane Software Group Pty Ltd | 150 | KEEP | KEEP | KEEP |
| 2 | Melbourne Digital Co | 80 | KEEP | KEEP | KEEP |
| 3 | Sydney Analytics Ltd | 300 | KEEP | KEEP | KEEP |
| 4 | Auckland Fintech Limited | 120 | KEEP | KEEP | KEEP |
| 5 | Perth Mobile Apps Pty Ltd | None | KEEP | KEEP | KEEP |
| 6 | Adelaide Insurance Group | 600 | KEEP | KEEP | KEEP |
| 7 | Canberra Cloud Pty Ltd | 45 | KEEP | KEEP | KEEP |
| 8 | Wellington Health Tech Ltd | None | KEEP | KEEP | KEEP |
| 9 | Melbourne Cloud Services Pty Ltd | 250 | KEEP | KEEP | KEEP |
| 10 | Sydney Insurance Holdings Pty Ltd | 900 | KEEP | KEEP | KEEP |
| 11 | Brisbane City Council | 1200 | noise_government | noise_government | noise_government |
| 12 | Perth Airport Pty Ltd | 800 | KEEP | KEEP | KEEP |
| 13 | Melbourne University | 1800 | KEEP | KEEP | KEEP |
| 14 | Fake University of Technology Sydney | 1900 | KEEP | KEEP | KEEP |
| 15 | Sydney Water Corporation | 1500 | KEEP | KEEP | KEEP |
| 16 | Department of Fake Transport Digital Services | None | noise_government | noise_government | noise_government |
| 17 | Fake State Revenue Office | 600 | KEEP | KEEP | KEEP |
| 18 | Fake Digital Service Authority | 400 | KEEP | KEEP | KEEP |
| 19 | Fake Energy Commission | 150 | KEEP | KEEP | KEEP |
| 20 | Fake Retirement Funds Management Pty Ltd | 700 | KEEP | KEEP | KEEP |
| 21 | Fake Super Fund | 900 | KEEP | KEEP | KEEP |
| 22 | Fake Industry Super Funds | 500 | KEEP | KEEP | KEEP |
| 23 | Fake Funds Management Ltd | 120 | KEEP | KEEP | KEEP |
| 24 | Fake Group | 300 | KEEP | KEEP | KEEP |
| 25 | Fake Holdings Limited | None | KEEP | KEEP | KEEP |
| 26 | Fake Services Pty Ltd | 100 | KEEP | KEEP | KEEP |
| 27 | The Fake Group Pty Ltd | 250 | KEEP | KEEP | KEEP |
| 28 | The Fake Company | 80 | KEEP | KEEP | KEEP |
| 29 | Fake & Sons Pty Ltd | 60 | KEEP | KEEP | KEEP |
| 30 | Fake and Partners Group | None | KEEP | KEEP | KEEP |
| 31 | Fäke Müller Pty Ltd | 120 | KEEP | KEEP | KEEP |
| 32 | Fake Café Holdings | 90 | KEEP | KEEP | KEEP |
| 33 | Auckland Council | 1500 | KEEP | KEEP | KEEP |
| 34 | Sydney Trains Digital | None | KEEP | KEEP | KEEP |
| 35 | Fake Government Services Pty Ltd | 200 | noise_government | noise_government | noise_government |
| 36 | Fake Governance Software Ltd | 90 | KEEP | KEEP | KEEP |
| 37 | Fake Office Supplies Pty Ltd | 120 | KEEP | KEEP | KEEP |
| 38 | Fake Office Technology Pty Ltd | 120 | KEEP | KEEP | KEEP |
| 39 | Fake Department Store Group | 1800 | KEEP | KEEP | KEEP |
| 40 | Fake Authority Payments Ltd | 200 | KEEP | KEEP | KEEP |
| 41 | Fake Commission Agents Pty Ltd | 50 | KEEP | KEEP | KEEP |
| 42 | Fake Commonwealth Bank Digital | 1900 | KEEP | KEEP | KEEP |
| 43 | Fake Public Service Credit Union | 800 | noise_government | noise_government | noise_government |
| 44 | Fake Ministry of Sound Media Ltd | 100 | noise_government | noise_government | noise_government |
| 45 | Sydney CBD Software Pty Ltd | 100 | noise_location | noise_location | noise_location |
| 46 | Fake CBD Technologies Ltd | 200 | KEEP | KEEP | noise_local_business |
| 47 | Melbourne CBD Dental Group | 300 | KEEP | KEEP | noise_local_business |
| 48 | Perth CBD Wealth Pty Ltd | None | noise_location | noise_location | noise_location |
| 49 | Fake Parliament Hill Software | 50 | noise_government | noise_government | noise_government |
| 50 | Fake Co | 100 | KEEP | KEEP | KEEP |
| 51 | Fake Business Services | 100 | KEEP | KEEP | KEEP |
| 52 | Fake Company Pty Ltd | 100 | KEEP | KEEP | KEEP |
| 53 | The Fake Group | None | KEEP | KEEP | KEEP |
| 54 | Fake Unknown Holdings | 100 | noise_vague_name | noise_vague_name | noise_vague_name |
| 55 | TBD Pty Ltd | 100 | noise_vague_name | noise_vague_name | noise_vague_name |
| 56 | Gold Coast Tourism Group Pty Ltd | 200 | KEEP | KEEP | KEEP |
| 57 | Newcastle Permanent Fake Building Society | 900 | KEEP | KEEP | KEEP |
| 58 | Fake Department of Health Pty Ltd | 300 | noise_government | noise_government | noise_government |
| 59 | Fake Minister Software Pty Ltd | 100 | KEEP | KEEP | KEEP |
| 60 | Fake Pty Ltd | 100 | KEEP | KEEP | KEEP |
| 61 | Hobart Digital Ltd | None | KEEP | KEEP | KEEP |
| 62 | Darwin Tech Group | None | KEEP | KEEP | KEEP |
| 63 | Sydney Melbourne Software | 100 | KEEP | KEEP | KEEP |
| 64 | Auckland Perth Digital | 100 | KEEP | KEEP | KEEP |
| 65 | Fake Bank of Wellington | 800 | KEEP | KEEP | KEEP |
| 66 | Wellington Insurance Group | None | KEEP | KEEP | KEEP |
| 67 | Fake City Council Software Pty Ltd | 100 | noise_government | noise_government | noise_government |
| 68 | Fake Shire Council | 300 | noise_government | noise_government | noise_government |
| 69 | Fake Office of the Chief Digital Officer Pty Ltd | None | noise_government | noise_government | noise_government |

Totals: SF-sourced 14 of 69 dropped, Nooks-sourced 14 of 69 dropped, ZoomInfo-only 16 of 69 dropped.

Reading the table: every SF/Nooks drop except possibly the council / department rows (rows 11 and 16, public bodies, a judgement call; row 33 `Auckland Council` is kept, so the rule is inconsistent) is a plausible Pendo buyer. Rows 35, 43, 44, 45, 48, 49, 54, 55, 58, 67, 69 are clear false positives: private companies, a credit union, a media company, software firms. Rows 46 and 47 (`... CBD ...` with 200 and 300 employees) are kept for SF/Nooks but dropped as `noise_local_business` for ZI-only, which is the intended behaviour only if you accept that ZI-only has no size (see below).

### Opposite check: real noise still caught for ZI-only rows
All 10 rows in `tests/fixtures/zoominfo_noise_examples.json` are dropped (pure location, `<Place> CBD Florist`, Information Commissioner, Department of Health, `(not ANZ)`, `Fake Funds Placeholder`). Extra probes also dropped: `Melbourne`, `Sydney CBD`, `Christchurch CBD`, `Fake Shire Council`, `NSW Government`, `Fake Ministry of Health`, `Funds`, `Group Holdings`, `Placeholder`. Not caught (see M2): `Wellington`, `Sydney, Australia`, `Bondi Florist`, `Fakeville Plumbing`, `Fakeville Primary School`, `Fake Department`, `Fake Funds`, `Test Company`.

## Recommendations

### Item 2: policy for noise filters
Your proposal (SF/Nooks accounts never dropped by noise filters, only flagged; noise drops only for ZoomInfo-only rows) is **right, with two tweaks**:
1. Apply it to the whole noise family (`noise_location`, `noise_local_business`, `noise_government`, `noise_vague_name`), not just local business. In `noise_reason`, if `account.source_salesforce or account.source_nooks`, return the result as a **flag** (note `possible_noise:<reason>`, confidence low) instead of a drop. Change in `_drop_reason` (accounts.py:332-334).
2. Make the flag visible to the person who decides: put `possible_noise_*` counts and sample names in `summarise()` so Alex can review them, since these are accounts he is assigned.
Rationale: SF rows are Alex's curated book, a wrong drop is a real loss, and a wrong flag costs one glance. Noise is overwhelmingly a ZoomInfo free-search problem (id and name only). Keep SF `customer`, `blocked`, size and cooldown drops as they are. Whether SF accounts should get the pure-location drop is a separate question: a book account literally named "Sydney" is vanishingly rare, so flag, not drop.
Also tighten the ZI-only rules where it is cheap: require `Government of ...`, `Department of ...`, `Office of the ...` only at the START of the name (regex `^`), and treat `credit union`, `software`, `technology`, `digital`, `media`, `sound` etc. as vetoes. For ZI-only, `Sydney CBD Software` is still dropped; acceptable.

### Item 6: `drop_if_no_domain`
Agree not to apply it, to SF accounts or any source: ZI free search returns no domain, and SF `Website` is often null. In `config/config.yaml:102` set `drop_if_no_domain: false` with a comment ("ZoomInfo free search returns no domain; dropping would remove every ZI row; reserved for a later enrich step"), and mention it in `docs/accounts_notes.md`. Keep the key (config.py:388 requires it). Consider a ranker point for having a domain instead of a filter.

### Confidence field
Yes, add a real field. A note tag is a free-text string that can be renamed, and `confidence()` already parses notes. Suggested: `Account.confidence: Literal["normal","low"] = "normal"` plus `Account.flags: list[str]` (machine tags such as `zoominfo_only`, `flagged_non_anz_billing_anz_hq`, `possible_noise:government`), keeping `notes` for human text. That is a change to `src/models.py` (frozen by interface doc), so the coordinator must approve; until then keep the note tags but export them as constants (they already are) and have 1f import `confidence()` rather than string-match.

## What I could not verify
- Real SF/Nooks data shapes beyond the synthetic fixtures; ID length behaviour (L3).
- Whether the ~420-account group should be included (the code includes and flags it; behaviour matches the stated spec).
- How many real accounts the noise rules would hit in Alex's book; the sweep above is invented, not a measured rate.

---

# Re-review after worker fixes (commit 5d7ce56, merged into this branch as e11f6e3)

Offline, read-only, invented data only.

## Verdict: FINAL PASS

All five earlier findings are fixed. Two follow-ups are recommended (one before 1c integration); neither blocks.

### 1. Tests and determinism
- `pytest tests -q`: **287 passed**. With `-W error`: **287 passed**. `tests/test_accounts.py` alone: 145 passed (also under PYTHONHASHSEED 3 and 77).
- Probe script (72 accounts, SF/Nooks/ZI with merges, duplicates, a blocked and a touched id): 6 shuffles of all three inputs gave identical accounts, flags, notes, drops and stats; PYTHONHASHSEED 0 and 5 gave byte-identical output. Inputs, `blocked` set and `touched` dict unchanged.
- Imports (accounts.py:51-53): `src.config`, `src.loaders`, `src.models` only. No `src.state`, `src.signals`, `src.ranker`. No network.

### 2. Earlier findings
| Finding | Status | Evidence |
|---|---|---|
| H1 noise drops SF book | **FIXED** | The 69 invented names: SF-sourced 0 dropped, Nooks-sourced 0 dropped. ZoomInfo-only 10 dropped, all noise-shaped (see below). Noise in SF/Nooks is now a flag + low confidence (accounts.py:378-382); the drop only runs for ZoomInfo-only rows (:412-417). `Fake Commissioner Software Ltd` as SF gets flag `noise_government` and is kept. |
| H2 two SF accounts sharing a ZI id collapse | **FIXED** | Same-source rows are never merged (:300-308, 323-324). Probe: `A1`/`A2` sharing `Z9` both kept, both `possible_duplicate`, notes name the other id. `touched` on A2 only drops A2 (`recently_touched`); `blocked` A2 drops A2; `customer` A2 drops A2; A1 unaffected. Two SF rows with the same SF Id both kept and flagged. |
| M1 name merge ambiguity | **FIXED** | One candidate merges (`name_merged: 1`); two SF candidates or two Nooks candidates leave the ZI row unmerged and flag all `possible_duplicate`, `name_merge_ambiguous: 1`. Both counters appear in `stats` and `summarise()` (:465-466). Merge notes list the merged ids (:215). |
| M3 unknown SF country | **FIXED** | `''`, whitespace, `Oz`, `Aotearoa`, `Australia (Territory)`, `Australasia`, `Unknown`, `N/A`, null+null: kept with `country_unknown`, confidence low. `United States`, `Fiji`, `Austria`, `Estados Unidos`, `USA`+`?`: dropped `not_anz`. See minor edge cases below. |
| M4 `(not ANZ)` regex | **FIXED** (restricted) | Now applied to ZoomInfo-only rows only (:413). It is still fixture-only code; deleting it later is fine. |

### 3. Government regex narrowing (accounts.py:91-93)
Private names stay unflagged and kept as SF rows with no flags: `Public Service Credit Union`, `Ministry of Sound Media`, `Government Services Pty Ltd`, `Department Store Group`, `Governance Software`. Good.

**But it now lets most real government noise through for ZoomInfo-only rows** (the anchor requires the phrase at the START of the name). Probes, all ZoomInfo-only:
- Caught: `Office of the Victorian Information Commissioner`, `Government of Western Australia Department of Health`, `Department of Transport and Main Roads`, `Fake Shire Council`, `Fake Regional Council`, `Public Service Commission Fake`, `Fake City Council Software Pty Ltd` (this one is a software firm; drop is a false positive for ZI-only, accepted).
- **Not caught**: `Western Australian Department of Education`, `Queensland Health Department`, `Victorian Department of Health`, `NSW Department of Planning`, `Queensland Department of Education`, `South Australian Ministry of Health`, `NZ Ministry of Education`, `Tasmanian Government`, `State Government of Victoria`, `Australian Taxation Office`, `Health Department of Fake`, `Fake Local Government Authority`.

Finding **R1 (Medium, accounts.py:91-93)**: state-prefixed department names ("Victorian Department of Health") are the common real shape, and they now pass as `zoominfo_only`. No data loss (they are tagged low confidence), but it is exactly the noise the filter exists for. Since SF/Nooks accounts are now flag-only, the anchor is no longer needed to protect Alex's book, so loosen it for ZI-only rows. Suggested: allow up to two leading words, `^(\w+ ){0,2}(department|dept|ministry) of\b`, plus `\bgovernment\b` and `\bcommissioner\b` anywhere. Cost: `Ministry of Sound Media` and `Government Services Pty Ltd` become ZI-only drops; those are exactly the cases where SF/Nooks flagging already protects real accounts. Alternative: leave as is and let the ranker push `zoominfo_only` to the bottom. Alex should choose.

### 4. New code
- **possible_duplicate** (:228-233, 300-328): works for SF/SF same Id, SF/SF same ZI id, Nooks/Nooks same SF hint, and ambiguous name matches. `touched`, `blocked`, `customer` act per id on each duplicate. One consequence (**R2, Low**): duplicates can share an `sf_account_id` (two kept accounts both with `A1`, e.g. the second Nooks row with `_sf_account_id=A1`). The flag says so, but 1c/1f dicts keyed by `sf_account_id` will collide. Document it in `docs/interfaces.md`, or key downstream by object identity.
- **country_unknown** (:353-370): behaves as above. Minor edges (**R3, Low**): a lone flag emoji `🇦🇺` is dropped as `not_anz` (reads as a real non-ANZ country); `Fiji` billing with `Oz` HQ is dropped (the alias is only honoured when no real non-ANZ value is seen). Neither matters in practice.
- **flags / confidence**: `confidence='low'` is set only for: flagged group, `country_unknown`, ZoomInfo-only, Nooks-only, and SF/Nooks accounts with a noise flag. Normal SF accounts, including possible duplicates, stay `normal` (duplicates are flagged but not low; acceptable). `confidence()` returns the field. `src/models.py` change (flags, confidence) is the field I asked for.
- **summarise()**: readable. Shows totals, low confidence count, size_unknown, a "kept but flagged for review" block with counts and up to 5 names per flag, drops per reason with samples, merge counters. Sample output above checked.

### 5. Items the worker did not fix
**(a) L3, 15- vs 18-character SF ids: Medium, real silent miss.** Verified with a probe: SF id 18 chars, `touched` keyed by the 15-char form -> not applied (account kept); `blocked` with the 15-char form -> not applied; Nooks `_sf_account_id` in 15-char form -> not merged, a separate `nooks_only` account with the 15-char id appears and is NOT flagged duplicate. SOQL returns 18-char ids; what Nooks `crmId`, `Contact.AccountId` and the Nooks tool filters return in real data is UNVERIFIED (fixtures are synthetic and use neither form), so the real risk depends on 1c's key sources. Recommendation: add `normalise_sf_id(raw) -> str | None` to **`src/loaders.py`** (one place, pure), returning the **15-character prefix** (`raw.strip()[:15]` when length is 15 or 18, case preserved) as the *join key*. Reason: an 18-char id is the 15-char id plus a 3-char case checksum, so comparing 15-char prefixes is exact for case-sensitive ids and needs no checksum code; keep the original string for display. Apply at ingestion: `account_from_sf_row`, `account_from_nooks(sf_account_id)`, `prospect_from_nooks`, and in `build_account_list` to the `touched` keys and `blocked` ids as a defensive step. Warn in the docstring that ids must not be lower-cased. The checksum conversion 15->18 is also fine but adds code for no gain. Not an `accounts.py` helper because 1c and 1f need the same key.
**(b) L4, `cooldown_days_after_connect`.** The signature is mildly misleading: `config.limits` has two cooldowns, `build_account_list` applies one number to every touched account, and the caller cannot express "this one connected, that one did not". Recommend the cleaner contract: `touched` carries the date the account becomes eligible again (`cooldown_until`), computed by 1c from the outcome, and `accounts.py` drops when `today < cooldown_until`; keep `cooldown_days` only as a fallback for plain dates. Until then, say in the docstring that the caller passes pre-adjusted dates.
**(c) M2, ZoomInfo-only noise like `Bondi Florist`: acceptable.** The accounts are tagged `zoominfo_only`, low confidence, and cannot be verified further without spending credits. The ranker must give them no `icp_fit` points and an explicit low ceiling. If Alex sees too many, add a small keyword list (florist, plumbing, dental, school) for ZI-only rows later.

### Summary of remaining items
| ID | Severity | Where | Action |
|---|---|---|---|
| R1 | Medium | accounts.py:91-93 | Loosen the government regex for ZI-only rows (state-prefixed departments leak). |
| L3 | Medium (unverified) | whole module, loaders.py | Add `normalise_sf_id` (15-char join key) before wiring 1c. |
| L4 | Low | accounts.py:246-256 | Pass `cooldown_until` dates, or document pre-adjusted dates. |
| R2 | Low | accounts.py:300-328 | Document that duplicates can share an `sf_account_id`. |
| R3 | Low | accounts.py:353-361 | Optional edge cases (emoji, Oz HQ). |
