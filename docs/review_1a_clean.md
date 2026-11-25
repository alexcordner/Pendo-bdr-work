# Review of task 1a-clean (synthetic rebuild)

Reviewer session, read-only. Reviewed `origin/phase1/1a-clean` @ `c097867` (one commit on top of the Phase 0 base, no old history). No live connector calls were made. This file quotes no real values; leaks are described by file, line and category only.

My denylist lives outside the repo, in my scratchpad. I built it automatically from every string value, id, number and company name in the old `phase1/1a-discovery` tree, minus anything already on main. It had 217 entries, of which 205 are non-trivial. I added the exact headline counts, the real phone digits I quoted earlier in the earlier review, and the person-name hints.

## Verdict: FAIL (one item), otherwise PASS

One blocking item: a wrong expected output in a phone test vector. Everything else is a pass, with low-severity notes.

| # | Item | Result |
|---|---|---|
| 1 | Real-data leak check | PASS |
| 2 | Fake-ness | PASS (one low note) |
| 3 | Fixture usefulness | **FAIL**: one wrong phone vector; minor coverage gaps |
| 4 | Notes and decisions | PASS (unconfirmed extras flagged) |
| 5 | Main, CLAUDE.md, config | PASS |

## 1. Real-data leak check: PASS
Tree searched: `docs/`, `tests/`, `config/`, `skills/`, root md files, `.gitignore`.
- **Denylist hits:** 23 of 205 entries matched, and every match is generic and not a leak:
  - 19 are Nooks disposition names or ordinary topic words ("No Answer", "Left Voicemail", "Do Not Call", "Pain Point", "Finance" and similar).
  - 1 is `https://example.com/news` (placeholder URL).
  - 1 is "See details" (generic link text).
  - 1 is a job-title string (generic role).
  - 1 is the word "Acquire" inside a synthetic sentence ("Fakeville Software agreed to acquire...").
  - Plus one noise-fixture name, `zoominfo_noise_examples.json:48`, "Funds", a vague generic word that is also a real ZoomInfo record name (see 2).
- **Zero hits** for: real company names, domains, SF 15/18-char ids, Nooks/disposition/workspace/connector UUIDs, ZoomInfo numeric ids, real phone digits, exact headline counts, person names, scoop text fragments, recording URLs.
- **Generic shape checks:**
  - Emails: only `user@example.com`, `first.last@example.com`, `firstlast@example.com`, `firstlast@example.org`, plus Alex's own address in `config/config.yaml:5` (already on main).
  - UUIDs: 0. SF-style ids: 0 (all ids are `FAKE-…` or `disp-…`).
  - Phones: only the `+61 400 000 0xx` family and `+64 9-000 01xx`, `+64200000005`, `+65…`. All obviously fake.
  - URLs: only `example.com`-style hosts.
  - 7+ digit runs outside placeholder patterns: 0.
- **Low observation (category: real aggregate statistics).** `docs/connector_notes.md` and `docs/decisions.md` keep rounded real aggregates: "about 20k" assigned accounts, "about 12k" ANZ non-customers, 41 flagged targets, roughly 420 non-ANZ-billing accounts, and the percentage mixes for call durations and scoops. None identifies a person or company. Flagging only because Alex said "no real CRM data".
- **Low observation (category: real distribution reused).** In `tests/fixtures/nooks_calls_sample.json`, the set of long "No Answer" durations and the voicemail durations look identical to the real ones in the old branch. They are only numbers and carry no identity, but the fixture is not purely invented there. Optional: jitter them.

## 2. Fake-ness: PASS
- Companies are all obviously fake: "Example Widgets Pty Ltd", "Fakeville Software Pty Ltd", "Sample Logistics Ltd", and `.test` domains.
- No fixture looks like a real person: `<First>`, `<Name>`, `user@example.com`, generic titles only.
- Titles in `sf_contact_row.json` and `nooks_prospect.json` ("Head of Product" and similar) are generic and not tied to a real company.
- **Low:** `zoominfo_noise_examples.json:48` keeps the name "Funds", an actual ZoomInfo record name. It is a common word and cannot identify anything, but consider renaming it ("Vague Name Co") for consistency.

## 3. Usefulness: FAIL (one blocking fix)
All 18 fixture JSON files parse.

**Blocking: wrong expected output in `phone_formats.json`.**
- Vector `in: "0200000102"`, `account_country: New Zealand` has `out: "+64200000002"`.
- Under the stated rule (leading `0` takes the country code), the correct output is `+64200000102`. The expected value drops digits "10".
- The other six vectors are correct under the rule: `+61 400 000 001` -> `+61400000001`; `0400000012` (AU) -> `+61400000012`; `61390000003` -> `+61390000003`; `+64 9-000 0104` (AU account) -> `+6490000104`; `+61 400 000 008` (NZ account) -> `+61400000008`; `0061400000013` -> `+61400000013`.
- Fix the expected output (or the input) so 1b's normaliser tests pass.

**Coverage against what 1b to 1f need:**
- Flagged target, customer, non-customer: covered (`sf_accounts_my_book.json`: 12 accounts).
- Null `BillingState`, abbreviated state (`NSW`, `VIC`), mixed-case country (`AUSTRALIA`), misspelt state, junk state: covered (plus a distribution fixture).
- Null `BillingCountry` with `HQ_Country__c` set (`AU` code and full name): covered.
- Non-ANZ billing with ANZ HQ (US billing, AU HQ): covered. Non-ANZ everything (Singapore): covered.
- No ZoomInfo id: covered. ZoomInfo-to-SF chain: covered.
- Opt-out true and false: covered (`sf_contact_row.json`).
- Dispositions: all 16 `nooks_sep` entries are in `nooks_call_dispositions.json`, and the call sample uses Left Voicemail (short and long), Do Not Call, Gatekeeper, Meeting Scheduled, Wrong Person, Person Left Company, Bad Number, Hang Up. Short "No Answer" (0 to 8s) and long "No Answer" (34 to 64s) are both there; null-prospect calls (8, with `from: external`) are there. Non-SF prospect (`type: csv`, null `crmId`) is there.
- Noise company rows: covered (10 rows in four groups).
- Scoops (12 rows): Funding, New Hire/Executive Move (product role and a finance role), Product Launch, M&A, Partnership, Pain Point + Project with relevant topics, Pain Point with Finance only, generic Medical & Health and Legal hiring, Hiring Plans with a digital role. Good.
- Gmail dotted vs stripped: covered, with an explicit `dot_stripped_expected_match` block.

**Minor gaps (non-blocking):**
- No "No Answer" call in the 10 to 30s range, which is the boundary around decision 7 ("under 20 seconds counts as no-contact"). Add a 19s and a 21s.
- No fixture with two calls from the same prospect on different days beyond the three-prospect pattern, so cooldown tests will have to build their own timestamps. Fine, but note it for 1c.
- `Open Position` and `Management Move` scoop types are absent (the notes already list them UNVERIFIED).
- Gmail: no empty-result example for the stripped-address query.
- `nooks_manual_email_backlog.json` is a rounded summary (fine).

## 4. Notes and decisions: PASS
- `docs/connector_notes.md` keeps its engineering value:
  - Query shape with placeholders, including the `Assigned_SDR__c` / country / customer test.
  - The join paths: prospect `crmId` to Contact to `AccountId`; ZoomInfo id to `DOZISF__ZoomInfo_Id__c`; `listAccounts filter_crmId`.
  - The disposition-by-name rule ("never hard-code ids").
  - Gotchas: Gmail dot-stripping, call page ordering by id, SF user timezone being London, unattended-run notes.
  - The timezone fallback chain, the phone rule, and a UNVERIFIED list.
- `docs/decisions.md` matches the decisions relayed by the coordinator:
  - Scope is every account where `Assigned_SDR__c` is Alex, ANZ only, non-customers.
  - Never contact is a Nooks Do Not Call disposition on the prospect or SF `HasOptedOutOfEmail`.
  - Voicemail is the `Left Voicemail` disposition by name only.
- **Flag:** decisions 3 (non-ANZ-billing group deferred to 1d), 4 (`SDR_Target__c` as a boost), 6 (manual_email never touched), 7 (the 20-second no-contact rule), 8, 9 and 10 go beyond what the coordinator listed as confirmed. I cannot tell whether Alex confirmed each. If any was only a recommendation, say so in the file.

## 5. Main, CLAUDE.md, config: PASS
`git diff 85a7eb7..HEAD` shows no changes to `CLAUDE.md`, `config/`, `PLAN.md`, `README.md`, `skills/` or `.gitignore`. They are identical to main. The only personal values are Alex's own email (`config/config.yaml:5`) and Nooks owner id (`CLAUDE.md`), both already on main and acceptable.

## Required to reach FINAL PASS
1. Fix the wrong expected output in `tests/fixtures/phone_formats.json` (the NZ `0200000102` vector).

Optional: rename "Funds", jitter the real-looking duration lists, add 19s and 21s No Answer rows, and mark which decisions Alex actually confirmed.

---

# Re-check of `phase1/1a-clean` @ `bc3db39`

Merged the updated `phase1/1a-clean` into this review branch first. Read-only, no live calls, denylist kept outside the repo. No real values quoted below.

**Verdict: FAIL on one item (5).** Items 1, 2, 3, 4 and 6 pass.

| # | Item | Result |
|---|---|---|
| 1 | Phone vectors | PASS |
| 2 | Call fixture shape and no real durations | PASS |
| 3 | New cases present | PASS |
| 4 | Confirmed vs defaults in `decisions.md` | PASS |
| 5 | Notes distinguish real behaviour from fixture mix | **FAIL** |
| 6 | Leak grep and JSON validity | PASS |

## 1. Phone vectors: PASS
I implemented the stated rule (keep a leading `+`; `00` -> `+`; strip other non-digits; 61/64 with at least 11 digits get `+`; leading `0` takes the account country's code) and ran all 7 vectors. All 7 match, including `0200000102` with a New Zealand account -> `+64200000102`.

## 2. Call fixture: PASS
- 40 calls. Left Voicemail: 4. Do Not Call: 1. Null prospect: 8 (all `from: external`).
- Long "No Answer" calls (over 30s): 8, at 35 to 63s. This is within the intended 34 to 64s band, just not identical to it. "No Answer" at 19s and 21s are present, so decision 8's 20-second boundary is covered. Short "No Answer" calls from 0 to 8s are present.
- Real-duration check: none of the 8 long "No Answer" durations equals any real one in the old branch (zero overlaps). One Left Voicemail duration coincides numerically with a real one (a single value). That is not identifying, so no action.

## 3. New cases: PASS
- Empty dot-stripped Gmail result: `empty_result_example` is in `gmail_search_sent.json`, with estimate 0 and no threads.
- Scoops: `Open Position` (with a Product Management topic) and `Management Move` (with a Digital Transformation topic) are present, 14 scoops in total.
- Noise row renamed "Fake Funds Placeholder".

## 4. Decisions split: PASS
"Confirmed by Alex" holds exactly three items: account scope (Assigned_SDR__c, ANZ, non-customers), the never-contact rule (Do Not Call history or `HasOptedOutOfEmail`), and the synthetic-data approach. Items 4 to 11 are under "Defaults in force, not yet confirmed". That matches what the coordinator told me was confirmed.

## 5. Real behaviour vs fixture mix: FAIL
`docs/connector_notes.md` line 87 now reads, in substance: "under 10s about 60%, 10 to 30s about 12%, over 30s about 28% (fixture shape; fortnight real mix was similar)".

The problem:
- The 60 / 12 / 28 split is the fixture's composition, but it appears in the "Duration shares" sentence as if it were the real pattern.
- The real pattern is roughly two-thirds under 10s, under 10% between 10 and 30s, and about a quarter over 30s (week and fortnight both). The "12%" middle bucket is materially different from the real under-10% figure, and "fortnight real mix was similar" is not accurate for that bucket.
- The sentence still carries "a fortnight gave 68/7/25", which contradicts the numbers beside it. A reader (or 1f's ranker author) cannot tell which numbers describe Alex's behaviour and which describe test data.

Suggested fix: split into two sentences.
- **Real behaviour (rounded):** "Alex's real call lengths: roughly two-thirds under 10s, under 10% between 10 and 30s, about a quarter over 30s."
- **Fixture composition:** "The synthetic `nooks_calls_sample.json` is a different mix on purpose (about 60/12/28); do not use it to calibrate thresholds."

## 6. Leak grep and JSON: PASS
- Denylist: 23 entries matched, as before. 21 are generic Nooks disposition names or topic words and placeholder URLs, and the other 2 are the plain words "Acquire" and "Funds" inside synthetic strings or this review. Zero hits for real company names, domains, ids, numbers, names or scoop text.
- Emails: `user@example.com`, `first.last@example.com`, `firstlast@example.com`, `firstlast@example.org`, and Alex's own address (already on main).
- UUIDs: 0. SF-style ids: 0. 7+ digit runs outside placeholder patterns: 0. Phones: only the fake `+61 400 000 0xx` and `+64 9-000 01xx` families.
- URLs: only `example.com` hosts.
- All 18 fixture JSON files parse with `python -m json.tool`.

## Required to reach FINAL PASS
1. Rewrite the duration-share sentence in `docs/connector_notes.md` (line 87) so real rounded behaviour and fixture composition are separate, as suggested above.
