# Review of task 1a (connector discovery)

Reviewer session, read-only. Reviewed commit `0bd2b30` ("1a: connector discovery notes and anonymised fixtures"). No worker files were modified. No writes, sends or ZoomInfo redeem/enrich calls were made.

Calls I made: Salesforce `getUserInfo` and 4 `soqlQuery` COUNT queries; Nooks `listCallDispositions` (`filter_source=nooks_sep`); Gmail `search_threads` x2 with `THREAD_VIEW_IDS_ONLY`, which returns no subjects, recipients or bodies.

## Verdict: PASS WITH FIXES

The connector facts check out against live data. The deliverable is useful. Three things need fixing before 1b to 1f build on it:

1. The 1a log is missing.
2. One real third-party name is in the notes.
3. Several quantitative claims have no supporting evidence in the committed files.

## Checklist

| # | Item | Result |
|---|---|---|
| 1 | PII and secrets | FAIL (minor) |
| 2 | Rule compliance | UNVERIFIED (log missing); no violation visible in the notes |
| 3 | Internal consistency | PARTIAL; see findings M2 and L1 to L3 |
| 4a | Salesforce spot-check | PASS |
| 4b | Nooks disposition ids | PASS |
| 4c | Gmail query form | PARTIAL: dotted form verified; third-party `to:` case UNVERIFIED |
| 5 | Usefulness for 1b to 1f | PASS, with gaps (below) |
| 6 | Decisions list | PARTIAL: 8 good items, 5 missing |

## Findings by severity

### Medium

**M1. The log is missing.** `logs/2026-11-25_0000_1a-discovery.log` does not exist in the checkout.
- `.gitignore` excludes `logs/*.log`, so it can never be committed.
- CLAUDE.md rule 5 requires a log on every run. That log is the only evidence that the credit count (0) and the "read-only" claim are true.
- I could not audit it. Everything about rule 2 and rule 7 rests on the worker's say-so.
- What I can say: the notes name only read tools (`getUserInfo`, `soqlQuery`, `listCalls`, `listProspects`, `listTasks`, `search_threads`, `search_companies`, `search_scoops`). The notes explicitly say `updateProspect`/`globalOptedOut` was not called. A credit count of 0 is credible, because the notes list only the free search tools.
- Fix: have the worker paste the log into a tracked location (for example `docs/logs/`, or an exception in `.gitignore`). Or the coordinator should confirm the worker session's transcript shows only reads.

**M2. Quantitative claims not backed by the fixtures.**
- `nooks_calls_sample.json` has 4 synthetic calls. All have identical timestamps, so they are clearly invented, though the file's `_note` does not say so.
- The notes' duration table (26 / 3 / 11 of 40 calls: 65% / 7.5% / 27.5%) is arithmetically consistent (40 total) but cannot be checked from anything committed.
- Likewise unsupported:
  - "Left Voicemail calls were 23 to 32s" (the fixture has only one, at 23s).
  - "No Answer logged on 41s and 64s calls" (the fixture has no such calls).
  - "8 of 40 calls have `prospect: null`".
  - "100+ manual_email tasks, 97 in one sequence".
  - "4,695 / 7,118 scoops" and the "48 of 50 (96%) not relevant" judgement.
- The rule "do not infer voicemail from duration" is the right call, but the evidence for it is not in the repo.
- Fix: either commit a trimmed real-shape sample showing a long No Answer call, or label these as "observed, not reproduced here".

### Low

**L1. A real third-party name is in the notes.** `docs/connector_notes.md:142` uses a real-looking recipient's name (a dotted first.last form and its stripped form, with the domain elided) as the dotting example. That is not an anonymised placeholder. Replace it with `first.last@example.com`.

**L2. Count wording mismatches.**
- The notes say "All 3 sampled rows" of open SF tasks; the fixture has 1.
- "Ran with `LIMIT 5`" and "returned 41 rows" cannot both be true. 41 is the count and 5 is the row limit; the fixture holds 3.
- `Days_Since_Last_Activity__c` "5 to 790": the fixture's range is 21 to 790.

**L3. `nooks_call_dispositions.json` is accurate** but is a derived list. I checked it row-for-row against live data: 16 current `nooks_sep` rows, names, ids and `callOutcome` all match. Legacy sources were not fixtured, which is correct.

**L4. Alex's own identifiers are in the repo.**
- His work email is in `config/config.yaml:5`, a Phase 0 file.
- The SF user id `005Pe00000HHNMbIAP` is in the fixtures and notes, and the Nooks owner id is in CLAUDE.md and the fixtures.
- These are his own ids, not secrets. Only worth knowing if the repo is ever made public.
- No tokens, API keys, OAuth material or connector UUID prefixes were found. The notes quote `mcp__3c43300d-…` and similar truncated prefixes only.

**L5. `hasTranscript: true` in a call fixture while Nooks Conversation Intelligence is "off".** The notes don't reconcile this. That matters for Phase 5c feasibility (call transcripts).

### Passes (evidence)

**PII scan.**
- Emails: only `first.last@example.com`, `firstlast@example.com`, `firstlast@pendo.example` and Alex's own address.
- Phones: only `+61400000000` and `+61400000001` (obvious placeholders), plus `+61 4XX XXX XXX`.
- Names: `<Name>`, `<First>`, `<Last>`, `<Person>`, `<BDR name>`, `<Contact Name>`. The manager is not named. Company names are public companies.
- CRM ids are shortened (`001-EXAMPLE-1`).
- Recording URLs are redacted.
- Other than L1, no individual is identifiable.

**Salesforce (4a), my own queries.**
- `getUserInfo` returns userId `005Pe00000HHNMbIAP`, displayName Alex Cordner, role "Commercial BDR - ANZ". Its timezone is `Europe/London`, as noted.
- `COUNT()` of Accounts with `Assigned_SDR__c` = that id gives **19,660**.
- Adding `Current_Customer__c = false` and `BillingCountry IN (Australia, New Zealand)` gives **7,604**.
- Adding `SDR_Target__c = true` gives **41**.
- All three numbers match the notes exactly.

**Nooks dispositions (4b).**
- Left Voicemail = `d957dd2b-a8fb-496b-ae99-cd2adffebd96` (callOutcome "No Answer").
- No Answer = `c106af3d-dc2f-471f-868c-017e9836979d` (callOutcome "No Answer").
- Both match the notes and the fixtures.
- Consequence: `callOutcome` cannot distinguish voicemail from no answer. Both are "No Answer". Voicemail must be detected by the disposition id, as the notes say.

**Gmail (4c).**
- `in:sent from:<dotted address> newer_than:7d` (Alex's own address, ids only) returned 10 threads. This matches the notes' "10 sent threads in the last 7 days".
- The same query with the dots stripped (`alexcordner@…`) returned nothing `{}`.
- The "query with the real dotted address" gotcha is therefore confirmed for the `from:` side.
- UNVERIFIED: the `to:<third party>` form and the "returned addresses are stripped" claim. Verifying them would mean reading real recipients, which my rules forbid. The fixture's `first.last@example.com` placeholder would return nothing, so it proves nothing.

## Gaps for the next tasks (1b to 1f)

1. **Contact to Account join.** Nooks `prospect.crmId` is the SF Contact id, but there is no fixture or note showing `Contact.AccountId`, or how to get from a Nooks prospect to an SF Account. The only documented Nooks-account to SF join is the `filter_crmId` filter. 1d needs a documented path: Nooks prospect -> `crmId` -> SF Contact -> `AccountId`.
2. **ZoomInfo to Salesforce join is clear** (`DOZISF__ZoomInfo_Id__c` = ZoomInfo `companyId`) **but untested end to end.** No fixture pairs a ZoomInfo company id with an SF row. Add one, so 1d and 1e can test the join. Also state what happens for the ZoomInfo-only majority of the 192,853 results: they have only id and name and no free size or state.
3. **Timezone source.**
   - `BillingState` full names map to the config timezone keys. There is no fixture row with a null or abbreviated state ("NSW"), and no handling for NZ (no state values seen).
   - There is no documented fallback when `BillingState` is blank. Add the distinct `BillingState` values for the 41 and the 7,604 (a free read) as a fixture.
   - The config has no ACT (Canberra, same as Sydney).
4. **Voicemail detection.** The method is clear (the disposition id). Missing: a fixture of the same prospect called more than once in the lookback window, and whether `time` is call start or end. Also missing: the 7-day-only sample means 14-day cooldowns (`cooldown_days_after_connect: 14`) were never tested against `listCalls`.
5. **Calls without a prospect.** The notes say to join on `to`. The matching rule (normalise the number to E.164, `+61 4XX XXX XXX` -> `+614…`) is not in the notes. SF `MobilePhone` has spaces and Nooks `to` has an unusual format; 1b needs a phone normaliser.
6. **Recently-touched filter inputs.** The sources are `Days_Since_Last_Activity__c` (SF), Nooks calls, and Gmail sent. The notes don't say which is authoritative or that SF activity may lag Nooks.
7. **Scoop fixtures are small.** `zoominfo_scoops.json` has 6 rows, and none are the `Pain Point`/`Project` shape that makes up most of the volume. 1e's topic filter needs at least one `Pain Point` row with topics and one generic "more open roles" row. The latter is present; the former is not.
8. **No `search_intent` shape.** Acceptable (not used yet), but 1e may be asked for it.
9. **Tool-name resolution.** Notes warn of connector UUID prefixes. The offline engine (1b to 1f) won't call tools, so this only affects Phase 2. No action now.

## "Decisions needed from Alex": review

The 8 questions are clearly worded. Recommended defaults below, plus 5 missing questions.

| # | Question | Recommended default |
|---|---|---|
| 1 | Which accounts are "mine"? | The 41 (`SDR_Target__c`) as tier 0; the 7,604 as an overflow pool ranked by signals. The worker's recommendation is fine. |
| 2 | Is `SDR_Target__c` the right flag? | Yes for now, behind a config switch (`accounts.require_sdr_target: true`). Ask Alex whether a saved SF report or view is his real list. |
| 3 | Employee floor / source | Keep 20-2000. Use `ZoomInfo_Employee_Count__c`; fall back to `ROE_count__c`; if both are null, keep the account and flag "size unknown". |
| 4 | Opt-out / do-not-call | Block if any of: Nooks "Do Not Call" disposition in call history for that prospect, `HasOptedOutOfEmail` true (email drafts only), or `Person Left Company`/`Wrong Person`. Treat unknown as callable but log it. |
| 5 | Old manual_email backlog (100+) | Leave alone. Rule 2 forbids deleting or overwriting. Exclude those prospects from the recently-touched count, but never skip or complete the tasks. |
| 6 | Scoop keep list | Add `Project` and `Pain Point` only when topics include Customer Experience, Product Management, Customer Success or Digital Workplace; keep the `Funding`, `Executive Move`, `New Hire`, `Partnership`, `M&A`, `Product Launch` types. Drop "more open roles in Medical & Health / Legal". |
| 7 | Voicemail rule | `Left Voicemail` disposition only. Never infer from duration. Optionally flag long No Answer calls for Alex to review. |
| 8 | Mixed `from` numbers | Ignore for the call sheet; log the caller number only. |

**Missing questions (suggest adding):**

- **9. ZoomInfo-only accounts.** Should accounts with no SF row be allowed on the sheet, given ZoomInfo search returns no size or state and the credit budget is 0? Default: no. Surface them only as "not in your book" signals unless Alex raises the credit budget.
- **10. Timezone for unknown state.** Default: Australia/Sydney, with the account flagged "tz assumed". For NZ, Pacific/Auckland.
- **11. Name-match noise rule.** ZoomInfo noise like "Sydney CBD Florist" slips through a "whole name is a place" rule. Default: drop when `employees < 20` or unknown; drop names matching known place tokens plus no domain. This only matters for ZoomInfo-only rows, so it is moot if 9 is "no".
- **12. Lookback for "touched recently".** Default: use the config cooldowns, and read 14 days of `listCalls`. The 7 days sampled in 1a are not enough.
- **13. SF Task writeback.** Does Nooks write calls to SF Tasks? Notes say UNVERIFIED. Default: ignore SF Tasks as a signal; use Nooks calls only.

## What I could not verify

- The 1a log (missing).
- Duration shares, the 40-call count, the manual_email backlog size and scoop-sample percentages. These need live Nooks/ZoomInfo reads I chose not to repeat. They can be re-checked with read-only calls if the coordinator wants.
- The Gmail `to:` form and the dot-stripping behaviour on returned recipient addresses (needs reading real recipients).
- Whether the repo has secrets in any commit before `0bd2b30`. I scanned only the current tree.

---

# Re-review of worker commit 8b16df8 (narrow, read-only)

Reviewed `origin/phase1/1a-discovery` @ `8b16df8` in a scratch worktree. My only external call was one read-only SOQL `COUNT()`. I recomputed statistics from the committed fixtures myself.

**Verdict: FAIL on item 4 (PII), PASS on everything else, with one small inconsistency.** Fix the phone numbers, then it is a PASS.

| # | Item | Result |
|---|---|---|
| 1 | M1: run log | PASS |
| 2 | M2: evidence fixtures | PASS (one inconsistency, below) |
| 3 | L1/L2 | PASS |
| 4 | PII/secret grep | **FAIL** |
| 5 | Gaps (a) to (f) | PASS |
| 6 | HQ_Country coverage hole | PASS (4,926 verified) |

## 1. M1, run log: PASS
`docs/1a_run_log.txt` exists. It states up front that it was reconstructed from the session transcript after the fact, and it ends with "counts for round 1 were recorded from memory... may be off by one or two". It lists only reads: SOQL SELECT, getObjectSchema, getUserInfo, Nooks get/list calls, Gmail `search_threads` (metadata view, no bodies), ZoomInfo `search_*`. It says ZoomInfo credits are 0 with no redeem, enrich, `account_research` or `conversation_intelligence`. There is no write, send, draft or note. It is an honest after-the-fact reconstruction, not a machine-captured log. That limits its value as an audit trail, but it is not misleading.

## 2. M2, evidence: PASS
I recomputed from `nooks_calls_14d_rows.json` (104 per-call rows):
- 14 days: 104 calls, 71 / 7 / 26 = **68.3 / 6.7 / 25.0%**. Correct.
- 7 days since 11-18: 40 calls, 26 / 3 / 11 = **65.0 / 7.5 / 27.5%**. Correct.
- "No Answer" calls over 30s: 8, with durations 34 / 39 / 41 / 41 / 41 / 41 / 45 / 64. Correct.
- Left Voicemail: 4 calls, 23 to 32s. Correct.
- 20 calls without a prospect, 74 distinct prospects, 94 distinct numbers. Correct.
- Manual-email backlog (`nooks_manual_email_backlog.json`): 200 rows, 189 in one sequence, due 2026-04-22 to 2026-11-15, total UNVERIFIED beyond 200. This is a summary only, with no per-row data, so I can't recompute it. It is internally consistent (189 + 2+1+3+1+1+1+1+1 = 200).
- Scoops (`zoominfo_scoop_sample_evidence.json`): 5 of 50 match the stated rule, so 10% weakly relevant and 90% generic. Sample B is 25 with 4 generic "Medical & Health" rows (16%). Correct. The worker disclosed the 96% to 90% correction. The relevance rule is the worker's judgement and is stated as such.

**Inconsistency (low):** `connector_notes.md` still says the 50 scoops came from "just **9 companies**". The evidence fixture lists **12 distinct companies** (it also counts "Funds", "Hostplus", "Hesta" and others). Change the notes to 12.

## 3. L1/L2: PASS
- The third-party name at the old line 142 is now `first.last@…`.
- "LIMIT 5 / 41 rows" is reworded: a separate `COUNT()` returned 41, and the sample used `LIMIT 5` (3 anonymised rows).
- The open-tasks wording is now "30 open tasks: 28 BDR Meeting Booked and 2 Call", and the fixture matches (`count_by_type`, 3 sample rows).

## 4. PII/secrets: FAIL
Clean: no tokens or keys, no recording URLs (all `<redacted>`), no personal names, no LinkedIn profile URLs. The email grep shows only placeholders plus Alex's own address.

**Problem: `tests/fixtures/phone_formats.json` contains what look like unmasked real phone numbers.** Its `_note` says "digits masked", but these are not masked:
- `observed_match`: "SF MobilePhone '+61 409 074 447' == Nooks call.to '+61409074447'". These are Australian mobile numbers belonging to a real contact.
- Test vectors: `+61 474 771 228` (also an AU mobile), `0226431011` (NZ mobile), `61390956533` (AU landline), and `+64 9-377 9779` (Auckland landline).
- Fix: replace all of them with obviously fake numbers in the same formats (for example `+61 400 000 000`, `0220000000`, `61390000000`, `+64 9-000 0000`). Check the test outputs still follow the stated rule. The landlines may be business numbers, but the mobiles identify an individual. Scrub them all and re-grep.

**Lower-risk re-identification (judgement call):** `sf_nooks_zoominfo_chain.json` pairs a prospect job title ("Chief Product Officer") with a named company ("Shift"), and another ("Head of Product") with "Xemplo". Names and emails are anonymised, but title plus company is probably enough to identify one person. Suggest dropping the title or the company name from those rows. This file also holds real SF record ids (`0030a00002P01LuAAJ` etc.); these are not secrets, but they point at real records.

The phone format list in `nooks_call_dispositions`/other fixtures is fine; `+6590XXXXXX`-style entries are masked.

## 5. Gaps (a) to (f): PASS
Documented in notes section 6, and consistent with the fixtures:
- (a) Prospect to Contact to Account join: the two-row chain fixture matches the text.
- (b) ZoomInfo id to SF Account: two verified pairs (Shippit, Phocas) in the chain fixture.
- (c) Timezone fallback: the state-value distribution fixture is present, and the notes give a sensible rule (full name or abbreviation, case-insensitive, fall back to the country default and log a guess).
- (d) Phone normalisation: the rule and vectors are present, but see the PII failure above.
- (e) 14-day call window: the fixture supports it. Note the useful warning that pages are ordered by id, not time.
- (f) Pain Point scoop: a Pain Point-only row is in `zoominfo_scoops.json`.

## 6. HQ_Country coverage hole: PASS (verified)
My read-only query:

```sql
SELECT COUNT() FROM Account
WHERE Assigned_SDR__c = '005Pe00000HHNMbIAP' AND Current_Customer__c = false
  AND BillingCountry = null AND HQ_Country__c IN ('Australia','New Zealand')
```

returned **4,926**, which matches the worker's number exactly. A broader variant I ran first (any non-AU/NZ `BillingCountry`, plus `'AU'`/`'NZ'` values in `HQ_Country__c`) gave 5,349, so about 420 more accounts have a different `BillingCountry` and an ANZ HQ. That is outside the worker's claim, but 1d should probably decide whether those count as ANZ. The 4,926 figure is correct as stated; note that it makes the pool about 12,500, not 7,604.

## Remaining actions for the worker
1. Mask the phone numbers in `phone_formats.json` (blocking).
2. Change "9 companies" to 12 in `connector_notes.md`.
3. Optionally drop job title or company from the chain fixture rows.

---

# Final check of worker commit b9d0d2c (read-only, no live calls)

Reviewed `origin/phase1/1a-discovery` @ `b9d0d2c` in a scratch worktree.

**FINAL PASS.**

1. **Full-tree PII/secret grep on the current tree: PASS.**
   - Scope: `docs/`, `tests/`, `config/`, root md files and the run log.
   - Emails: only `first.last@example.com`, `firstlast@example.com`, `firstlast@pendo.example` and Alex's own address.
   - Phone-like strings, in every format I tried (`+61`, `+64`, `+1`, `+65`, `+91`, `0[2-9]xxxxxxxx`, bare `61xxxxxxxxx`, runs of 9+ digits, excluding ZoomInfo ids and UUIDs): every hit is an obvious placeholder (all-zero patterns). The earlier real-looking numbers are gone.
   - No person names, no tokens, keys or passwords, and no real recording URLs (only `<redacted>.wav`).
2. **`phone_formats.json`: PASS.** All numbers are obviously fake (`+61 400 000 001`, `+64 9-000 0004`, `0200000002` and so on). I checked each test vector against the stated rule. `observed_match` now also uses the masked form.

   | Input | Expected output | Under the rule |
   |---|---|---|
   | `+61 400 000 001` | `+61400000001` | keep `+`, strip spaces: OK |
   | `0200000002` (NZ account) | `+64200000002` | drop `0`, prefix +64: OK |
   | `61390000003` | `+61390000003` | starts 61 and is 11 digits, so add `+`: OK |
   | `+64 9-000 0004` | `+6490000004` | keep `+`, strip non-digits: OK |
   | `+61 400 000 008` | `+61400000008` | OK |
3. **Chain fixture and company count: PASS.** `sf_nooks_zoominfo_chain.json` titles are now `<title>`, and `connector_notes.md` says "12 companies". The only remaining job title in the tree is "Head of Product" in `nooks_prospect.json`, which has no company attached, so it is not identifying.
4. **JSON validity: PASS.** All 19 fixture files parse with `python -m json.tool`.

No open blockers from 1a. The one scope note carried forward is the optional roughly 420 accounts with a non-ANZ `BillingCountry` but an ANZ `HQ_Country__c`, for 1d to decide.
