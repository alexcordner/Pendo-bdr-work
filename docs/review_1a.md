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
