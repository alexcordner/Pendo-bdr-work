# Review of task 1e: `src/signals.py` (branch `phase1/1e-signals`, commit a476bbf)

Reviewer: independent, offline, read-only. No connector calls. All example text below is invented. I did not modify the worker's files; mutations were run on a scratch copy.

## Verdict: PASS WITH FIXES

The module is clean, deterministic, honest about drops and unmatched scoops, and the tests are mostly real. The highest-risk heuristic (role matching) has a precision problem on non-buyer "Head of Product X" titles and the hiring-keyword path leaks. There is also one real logic bug in multi-type scoops (kind precedence overrides strength). None of it blocks Phase 1 completion, but items H1 to H3 should be fixed before Phase 2 touches live scoops.

## What I ran

| Check | Result |
|---|---|
| `pytest tests -q` (Python 3.13) | 277 passed (135 in `tests/test_signals.py`) |
| `pytest tests -q -W error` | 277 passed |
| `PYTHONHASHSEED` 0, 1, 42, 999 | 135 passed each |
| Radar over the 30-scoop fixture, 30 random shuffles of scoops and accounts | identical output every time |
| Imports in `signals.py` | only `src.config`, `src.models` + stdlib. No state/accounts/ranker. No `random`, network, clock reads (`signals.py:15-23`; grep for `now()/today()/random/requests` clean) |

## Checklist

1. **Tests / determinism / purity: PASS.** Sorting keys are total orders (`signals.py:291-293`, `402-404`). Python 3.11+ features only.
2. **Role matching: PASS WITH FIXES.** 85 invented rows, table below. 41 TP, 3 FN, 10 FP, 31 TN. Precision 80%, recall 93% on my set (set is tuned adversarially to non-buyers, so precision is pessimistic). See H1, H2.
3. **Generic-note filtering: PASS WITH FIXES.** The real sample patterns are handled. Hiring leaks via broad keep keywords (H2). Details below.
4. **Scoring: PASS WITH FIXES.** Strength table matches the spec exactly (`signals.py:28-38`). Decay and combine verified by hand (below). One bug: kind precedence (H3).
5. **Joins and honesty: PASS.** Unmatched reported, never silently lost. Minor: id normalisation (M2).
6. **Intent placeholder: PASS WITH FIXES.** Flagged UNVERIFIED in code only, not in docs (M3). Score-60 rows produce a zero-strength signal (M4).
7. **Dates: PASS.** See below.
8. **Code quality: PASS WITH FIXES.** Readable, commented. Several tests are tautological against constants (M5); mutation results below.

## Findings by severity

### High
- **H1. False positives on non-buyer "Head of Product ..." and "... Support/Technology ..." titles** (`signals.py:101-109`). `_TITLE_THEN_DOMAIN` accepts a title followed by a domain word and does not check what follows. Kept wrongly: "Head of Product Supply and Logistics" (row 56), "Head of Product Compliance" (57), "Head of Product Development, Manufacturing" (67), "Technology Procurement Director" (68, via `_DOMAIN_THEN_TITLE` with a one-word gap), "Director of Support Services (aged care)" (65), "Social Media Manager for product campaigns" (63, the `manager ... for product` pattern: title, `for`, domain). Fix: (a) drop `for` from the connector list `(?:of|for|and)`; (b) add a negative lookahead after the domain word for non-buyer nouns (`supply|compliance|procurement|manufactur|logistics|safety|marketing|sales|services?\b` after `support`); (c) in `_DOMAIN_THEN_TITLE` do not allow a gap word that is itself a non-buyer function (`procurement|supply|finance|sales|marketing|legal|hr`). Add all six rows as regression tests.
- **H2. Hiring-note keep path uses bare keywords that are too broad** (`signals.py:211-219`, config `keywords_keep`). `support`, `platform`, `app` keep: "hiring support staff for the hospital ward" (80), "apprentices and warehouse support staff" (81), "drivers for its delivery platform" (82). Because a keep keyword overrides a drop keyword, "warehouse"/"drivers" do not protect (the stated precedence). Also "nurse to support its new digital health app" and "paralegals to support a product launch" are kept (rows 76-78, 85); those are arguable and I judged them acceptable, but they show the override is strong. Recommend for Hiring Plans / Open Position: keep only on (topic hit) OR (`relevant_role(desc)`) OR (a strong keyword set: `product manager`, `ux`, `cx`, `customer success`, `mobile app`, `digital`), and let a drop keyword win when the only keep hit is a weak word (`support`, `platform`, `app`, `engineering`). This is a config-level tweak plus a small code change; do not change `keywords_keep` semantics for the other kinds.
- **H3. Kind precedence is not strength order, so the weaker kind wins on multi-type scoops** (`signals.py:69-72`, `268`). Verified: Executive Move + M&A -> `ma` (0.60 base) instead of `exec_move` (0.90); Project + Partnership with a buyer topic -> `partnership` (0.45) instead of `project_pain` (0.80). The existing test `test_precedence_exec_move_over_new_hire` only covers the pair that happens to be correct. Fix: pick the passing kind with the highest `KIND_BASE_WEIGHT` (drop `KIND_PRECEDENCE`), or reorder to match weights. Add a test for each pair above.

### Medium
- **M1. Recall misses on real buyer roles** (rows 19, 24, 29): "Chief Customer Officer", "Head of Customer Service and Support" (the `and` joiner breaks `Customer Service` -> `Support`), "Head of Mobile". Add `customer officer`/`chief customer`, `customer service`, `mobile`, `data`? (judgement call: Chief Data Officer was dropped, row 30; Alex should decide) to `_DOMAIN`.
- **M2. Join keys are exact string match** (`signals.py:312`). `"1"` vs `"01"` and an int-keyed dict both give `unmatched` (honest, no crash, no wrong join). Real ZoomInfo ids are numeric, so the risk is a caller building the dict with ints. Normalise both sides with `str(x).strip()` or document that keys must be `str`. Negative ids and empty company id behave correctly (empty -> unmatched).
- **M3. Intent placeholder is flagged UNVERIFIED only in `signals.py:410-414` and in evidence text.** `docs/interfaces.md`, `docs/connector_notes.md` UNVERIFIED list, and `docs/decisions.md` do not mention it. Add one line to the UNVERIFIED list ("shape of search_intent rows; topic list; scoring") and to interfaces.md.
- **M4. Intent `signal_score` 60 gives `strength 0.0` and still emits a Signal** (`signals.py:479`; observed `project_pain 0.0`). A zero-strength signal is not a trigger. Skip with a reason when `strength <= 0`. Also `Source` is `zoominfo_intent:<topic>` with no company id, so sources are not unique across accounts; use `zoominfo_intent:<company_id>:<topic>`.
- **M5. Three of ten mutations survived** (below): `COMBINE_DAMPING` 0.5 -> 1.0, half-life 21 -> 30 days, `INTENT_MAX_STRENGTH` 0.7 -> 1.0. Tests compare against the constants themselves (`test_decay_half_life`, `test_intent_strength_formula_capped_and_bounded`), so they cannot fail on a wrong constant. Add pinned numbers (examples in section 4).
- **M6. "Onboarding" intent topic** (`signals.py:415-416`) is generic (HR onboarding, customer onboarding). It can raise a trigger at 0.7 for an irrelevant buyer. Tie to "digital/customer onboarding" or drop until the real topic list is known.

### Low
- **L1.** Duplicate scoop id (same id, different content) keeps the first and drops the second without reporting it (`signals.py:308-310`). Realistically impossible; log it anyway.
- **L2.** A Hiring note that has a drop keyword and a relevant topic but no keep keyword is dropped before the topic check (`signals.py:213-217`; row: Open Position, topic "Product Management", text "more open roles in Medical & Health" dropped). I think that is right, but it means topics do not count as keep keywords. Document it.
- **L3.** Topic matching is exact string (`"Customer Experience Management"` does not match). Real ZoomInfo topic names are not fully verified; acceptable, just note it.
- **L4.** `config` argument to `scoops_to_signals` is unused (documented as symmetry). Fine, but it is the only dead-ish parameter.
- **L5.** `Funding` + "X announced it is investing in a new customer app" is dropped as an investor note; harmless.

## 2. Role matching: the 85-row table

"(judge)" rows are my own calls and are not counted as an error either way. Expected = my honest view of whether a Pendo BDR would want a call reason.

| # | Type | Description (fake) | Expected | Actual | Pass |
|---|---|---|---|---|---|
| 1 | Executive Move | Example Co appointed Jordan Fake as Chief Product Officer. | keep | keep | PASS |
| 2 | Executive Move | Example Co named a new VP of Digital Experience, Sam Placeholder. | keep | keep | PASS |
| 3 | New Hire | Example Co hired a Head of CX to lead its customer programme. | keep | keep | PASS |
| 4 | New Hire | Pat Sample joins Example Co as Director, Customer Success. | keep | keep | PASS |
| 5 | Executive Move | Example Co welcomes a new CTO and co-founder, Alex Invented. | keep | keep | PASS |
| 6 | Executive Move | Example Co has appointed a Chief Digital and Information Officer. | keep | keep | PASS |
| 7 | New Hire | Example Co has hired a Head of Digital Workplace. | keep | keep | PASS |
| 8 | New Hire | Example Co has hired a Product Owner for its mobile app. | keep | keep | PASS |
| 9 | New Hire | Example Co has hired a UX lead. | keep | keep | PASS |
| 10 | Executive Move | Example Co appointed a Group Head of Customer Support. | keep | keep | PASS |
| 11 | Executive Move | Example Co appoints Chief Technology Officer. | keep | keep | PASS |
| 12 | Management Move | Example Co promoted Lee Fake to Chief Experience Officer. | keep | keep | PASS |
| 13 | Executive Move | Example Co has a new Chief Information Officer. | keep | keep | PASS |
| 14 | Management Move | Example Co has appointed a General Manager, Digital. | keep | keep | PASS |
| 15 | New Hire | Example Co hires Senior Product Manager for its platform. | keep | keep | PASS |
| 16 | Executive Move | Example Co has a new Head of Product. | keep | keep | PASS |
| 17 | Executive Move | Example Co appoints Vice President of Engineering. | keep | keep | PASS |
| 18 | New Hire | Example Co hired Director of Product Design. | keep | keep | PASS |
| 19 | Executive Move | Example Co appoints Chief Customer Officer. | keep | drop | **FAIL** |
| 20 | New Hire | Example Co has hired a Head of Customer Experience. | keep | keep | PASS |
| 21 | Executive Move | Example Co names a new Head of Digital Transformation. | keep | keep | PASS |
| 22 | Executive Move | Example Co appoints Head of Technology. | keep | keep | PASS |
| 23 | New Hire | Example Co has hired a Customer Experience Manager. | keep | keep | PASS |
| 24 | New Hire | Example Co hired a Head of Customer Service and Support. | keep | drop | **FAIL** |
| 25 | Executive Move | Example Co appointed a Chief Operating Officer who will oversee digital. | drop (judge) | drop | PASS |
| 26 | New Hire | Example Co welcomes a new Engineering Manager. | keep | keep | PASS |
| 27 | New Hire | Example Co hires a Principal Product Manager. | keep | keep | PASS |
| 28 | Executive Move | Example Co appoints a VP Product. | keep | keep | PASS |
| 29 | Executive Move | Example Co appoints a Head of Mobile. | keep | drop | **FAIL** |
| 30 | Executive Move | Example Co has a new Chief Data Officer. | drop (judge) | drop | PASS |
| 31 | Executive Move | Example Co appoints Head of Digital Channels. | keep | keep | PASS |
| 32 | New Hire | Example Co hired an Enterprise Applications Manager. | drop (judge) | drop | PASS |
| 33 | New Hire | Example Co hired a Product Operations lead. | keep | keep | PASS |
| 34 | Executive Move | Example Co appointed a Head of Employee Experience. | keep | keep | PASS |
| 35 | Management Move | Example Co appointed a Head of Customer Success. | keep | keep | PASS |
| 36 | Executive Move | Example Co appointed a Chief Product and Technology Officer. | keep | keep | PASS |
| 37 | Executive Move | Example Co appointed a Chief Digital Officer. | keep | keep | PASS |
| 38 | New Hire | Example Co hired a Digital Product Manager. | keep | keep | PASS |
| 39 | New Hire | Example Co hired a Senior Software Engineer. | drop (judge) | drop | PASS |
| 40 | Executive Move | Example Co appoints Head of IT. | keep | keep | PASS |
| 41 | Executive Move | Example Co appoints Director of IT Operations. | keep | keep | PASS |
| 42 | Executive Move | Example Co appointed Chief Financial Officer Kim Fake. | drop | drop | PASS |
| 43 | New Hire | Example Co hired a Product Marketing Manager. | drop | drop | PASS |
| 44 | New Hire | Example Co has a new Head of Procurement. | drop | drop | PASS |
| 45 | Hiring Plans | Example Co is hiring 40 Customer Service Representatives. | drop | drop | PASS |
| 46 | Executive Move | Example Co CEO steps down after eight years. | drop | drop | PASS |
| 47 | Hiring Plans | Example Co is seeking nurse practitioners across its clinics. | drop | drop | PASS |
| 48 | New Hire | Example Co hired an IT support technician. | drop | drop | PASS |
| 49 | Executive Move | Example Co appoints a Chief Information Security Officer. | drop (judge) | drop | PASS |
| 50 | New Hire | Example Co hired a Digital Marketing Manager. | drop (judge) | drop | PASS |
| 51 | Executive Move | Example Co appoints a new Chief Executive Officer. | drop | drop | PASS |
| 52 | Executive Move | Example Co appoints Chief People Officer. | drop | drop | PASS |
| 53 | New Hire | Example Co appoints Head of Legal. | drop | drop | PASS |
| 54 | New Hire | Example Co appoints Head of Sales. | drop | drop | PASS |
| 55 | New Hire | Example Co hires a Sales Director for the product range. | drop | drop | PASS |
| 56 | New Hire | Example Co hires Head of Product Supply and Logistics. | drop | keep | **FAIL** |
| 57 | New Hire | Example Co names a Head of Product Compliance. | drop | keep | **FAIL** |
| 58 | Executive Move | Example Co appoints a Chief Marketing Officer. | drop | drop | PASS |
| 59 | Executive Move | Example Co appoints Chief Risk Officer who will support the board. | drop | drop | PASS |
| 60 | New Hire | Example Co hired a Facilities Manager. | drop | drop | PASS |
| 61 | New Hire | Example Co appoints a Head of Supply Chain Technology Procurement. | drop | drop | PASS |
| 62 | Executive Move | Example Co appoints Chair of the Board and former product executive. | drop | drop | PASS |
| 63 | New Hire | Example Co hires Social Media Manager for product campaigns. | drop | keep | **FAIL** |
| 64 | Executive Move | Example Co appoints a new Chief Financial Officer; she previously led digital at a bank. | drop | drop | PASS |
| 65 | New Hire | Example Co has appointed a Director of Support Services (aged care). | drop | keep | **FAIL** |
| 66 | New Hire | Example Co hired a Warehouse Manager. | drop | drop | PASS |
| 67 | New Hire | Example Co appoints a Head of Product Development, Manufacturing. | drop | keep | **FAIL** |
| 68 | New Hire | Example Co appoints Technology Procurement Director. | drop | keep | **FAIL** |
| 69 | New Hire | Example Co welcomes a Customer Support Officer. | drop | keep | **FAIL** |
| 70 | Hiring Plans | Example Co is hiring a Senior Product Manager and two UX designers. | keep | keep | PASS |
| 71 | Open Position | Example Co is hiring mobile app developers. | keep | keep | PASS |
| 72 | Hiring Plans | Example Co is hiring more open roles in Medical & Health. | drop | drop | PASS |
| 73 | Hiring Plans | Example Co is hiring more open roles in Legal. | drop | drop | PASS |
| 74 | Hiring Plans | Example Co is hiring delivery drivers. | drop | drop | PASS |
| 75 | Open Position | Example Co has open roles for warehouse staff. | drop | drop | PASS |
| 76 | Hiring Plans | Example Co is hiring a nurse to support its new digital health app. | keep | keep | PASS |
| 77 | Hiring Plans | Example Co is hiring paralegals to support a product launch. | keep | keep | PASS |
| 78 | Hiring Plans | Example Co is hiring a Legal counsel for its product team. | keep | keep | PASS |
| 79 | Hiring Plans | Example Co is hiring accountants and payroll staff. | drop | drop | PASS |
| 80 | Hiring Plans | Example Co is hiring support staff for the hospital ward. | drop | keep | **FAIL** |
| 81 | Hiring Plans | Example Co is hiring apprentices and warehouse support staff. | drop | keep | **FAIL** |
| 82 | Open Position | Example Co is hiring drivers for its delivery platform. | drop | keep | **FAIL** |
| 83 | Hiring Plans | Example Co is hiring a Customer Success Manager. | keep | keep | PASS |
| 84 | Hiring Plans | Example Co is hiring finance analysts. | drop | drop | PASS |
| 85 | Hiring Plans | Example Co is recruiting Medical & Health staff for its new patient app. | keep | keep | PASS |

Tricky positives asked for in the brief (rows 1-10): all pass. Tricky negatives (rows 42-50): all pass, including `CISO` (dropped; I agree) and `Digital Marketing Manager` (dropped; I agree). "Chief Operating Officer who will oversee digital" is dropped, which is correct (a COO is not the buyer).

**Drop-vs-keep precedence.** "nurse + digital" in a Hiring Plans scoop is kept (row 76: a nurse hired for a digital health app). That is per spec ("drop wins only when no keep keyword"), and in that example the digital angle is real, but it is a one-word override: "paralegals to support a product launch" is also kept (row 77), as is "Legal counsel for its product team" (78). I consider those acceptable. The leak is the weak keywords in H2. Exec Move / New Hire do not use drop keywords at all, so a "Head of Product, hospital nursing" would be kept (judged fine).

## 3. Generic-note filtering (sample evidence patterns and variants)

All 14 evidence rows match (`test_existing_fixture_matches_expected_judgements`). My variants:

| Variant | Result |
|---|---|
| "more open roles in Medical & Health" (Hiring Plans / Open Position) | dropped (drop keyword) |
| "... Medical and Health", "... Healthcare and Medical", "... Customer Service", "... Sales and Marketing" | dropped (no keep hit: default-deny helps) |
| "... Legal & Compliance", "... Law and Legal Services" | dropped |
| "According to an internal source in finance ..." with topic Finance | dropped (no buyer topic) |
| Same sentence with topics Finance + Customer Experience | **kept** (topic gate wins, correct by decision 9) |
| Project/Pain with topic "Customer Success " (trailing space), lower-case topic | kept (strip + lower) |
| Topic "Customer Experience Management" | dropped (exact match, L3) |
| "Venture Fund X invested in Other Co", "Example Co invested $2M in Other Co", "has invested in Other Co's seed round" | dropped |
| "Fund Y led a $3M round in Example Co", "Fund Y backs Example Co", "has received an investment of $3M" | kept (correct) |
| "Fund Y invested $3M in Example Co" (scoop company = the investee) | dropped (**false drop**: the rule treats any `invest` verb with no "raised/secured/round" word as investor note, even if the company is the object). Low impact, but a real funding event is lost. Consider checking that the company name appears *before* the verb. |
| "Other Co raised $5M from Example Co" attached to Example Co | kept (ambiguous; depends on ZoomInfo attachment, UNVERIFIED) |

## 4. Scoring, decay, combine

Strength table: exec_move .90, product_launch .80, project_pain .80, funding .70, new_hire .65, ma .60, hiring_plan .55, partnership .45: matches (`signals.py:28-38`).

Hand calculations (today = 2026-11-25), all matched the code to 4 dp:

| Scenario | Hand | Code |
|---|---|---|
| Exec move, 21 days old | 0.90 x 0.5 = 0.45 | 0.45 |
| Product launch, 10 days | 0.80 x 0.5^(10/21) = 0.5751 | 0.5751 |
| Funding, 45 days (kept) | 0.70 x 0.5^(45/21) = 0.1585 | 0.1585 |

Age boundary: 45 days kept (strength 0.1585), 46 dropped ("expired: 46 days old ..."), 44 -> 0.1638. Future-dated (-3 days) kept, age 0, strength 0.70, reason notes it.

Combine `1 - (1-best) * prod(1 - 0.5*other)`: [0.5] -> 0.5; [0.5, 0.4] -> 0.6 (hand: 1 - 0.5 x 0.8 = 0.6); [1.0, 1.0] -> 1.0; [0, 0] -> 0. 2000 random lists: always in [0, 1] and adding a signal never lowered the value. Same scoop id twice used once. Same company on three different days (funding 1d, launch 20d, exec 31d): strengths 0.6773 / 0.4134 / 0.3235, combined 0.7854.

**Opinion on exec_move at the top (for Alex).** Keep it at the top: for a BDR the best "reason to call now" is a named new product/digital/CX/CTO leader, because they are in their first 90 days, reviewing tools, and you can say who they are. Product launch is a close second. Funding is a weaker call reason (money, no specific need). Two cautions: (1) the 21-day half-life is the same for every kind; for an exec move the buying window is longer (months), so a slower decay for exec_move (say 45-day half-life) is defensible and would stop a 40-day-old new CPO ranking below a 10-day-old partnership; (2) exec_move depends on my fragile text heuristic (H1/M1), so a wrongly kept exec move at 0.90 is the most damaging false positive. Fix H1 first. Suggested pinned values for new tests: `signal_strength(EXEC_MOVE, 21) == 0.45`, `combined_strength([0.5, 0.4]) == 0.6`.

## 5. Joins and honesty

- Types: `"1"` matches `"1"`; `"1"` vs `"01"` -> unmatched; `{1: acct}` int-keyed dict -> unmatched (M2); `"-5"` works; empty `company_id` -> unmatched.
- `no_trigger_signal` evidence is "No trigger found" with kind `none`, strength 0.0, source `none` (`signals.py:350-353`): honest, nothing invented.
- Radar never lists kind `none` (checked: `any(kind is NONE)` is False), ordering is a total order, `top_n=None` returns all, `top_n=0` returns `[]`, negative raises.
- Accounts with several scoops: grouped, best signal chosen, `why` shows `(+N more)`.

## 6. Intent placeholder

- Capped at 0.7 (`signals.py:420`, verified); a 100/A row is 0.7, a 100/A row dated 5 days ago is 0.5935.
- It cannot raise a trigger for an off-topic row (Payroll skipped with reason), nor for out-of-range / string / bool scores, nor for unmatched companies (all go to `skipped`).
- Weaknesses: M3, M4, M6, and the kind reuse below.
- **Recommendation on `SignalKind.INTENT`:** yes, the coordinator should add `INTENT = "intent"` to `src/models.py`, with a base weight of about 0.5. Reusing `PROJECT_PAIN` (base 0.80 for a scoop) means the ranker and any call-sheet text will treat unverified intent as an explicit CX/product project, and an intent row at 0.7 can outrank a real, decayed scoop. Until then, a distinct kind is the cheapest way to keep `source` and `kind` honest. Update `interfaces.md` and `tests/test_signals.py::test_intent_kind_and_links` together.

## 7. Dates

- Age and filter use `Scoop.published_date` (`signals.py:242`, `319`), which the loader fills from `publishedDate` and falls back to `originalPublishedDate` (`loaders.py:402`). That is the right choice for "how fresh is it": the original date can be much older than a re-published item. One caveat: if ZoomInfo re-publishes an old scoop, `publishedDate` can make a stale event look fresh. UNVERIFIED against live data; worth checking in Phase 2b.
- `today` can be a `date` or `datetime`; aware datetimes are converted to UTC; naive ones are treated as UTC (`signals.py:149-155`). Verified: 23:30 at UTC-5 on Nov 24 is Nov 25 UTC; both date and datetime `today` gave age 0. Age is whole UTC calendar days, so an AU scoop can be off by one day at the boundary. Acceptable.
- Naive scoop dates are accepted as UTC (the model says aware UTC).

## 8. Code quality and test strength

Simple, commented, constants documented (`signals.py:25-78`), no dead code beyond the unused `config` argument, Python 3.11+ only. Tests: 106 test functions, mostly behaviour-driven with fixtures.

Mutations on a scratch copy (each run with `-x`):

| Mutation | Caught? |
|---|---|
| age cut-off `>` -> `>=` | yes (`test_scoop_at_max_age_is_kept`) |
| drop keyword always wins | yes (`test_drop_keyword_loses_to_buyer_keyword`) |
| `management move` mapped to NEW_HIRE | yes (`test_existing_fixture_kinds`) |
| exec_move base .90 -> .60 | yes (`test_kind_ordering_same_age`) |
| remove marketing filter | yes (`test_product_marketing_not_a_buyer`) |
| remove duplicate-id dedupe | yes (`test_duplicate_scoop_ids_used_once`) |
| radar `top_n` slice removed | yes (`test_radar_top_n`) |
| `COMBINE_DAMPING` 0.5 -> 1.0 | **survived** |
| half-life 21 -> 30 days | **survived** |
| `INTENT_MAX_STRENGTH` 0.7 -> 1.0 | **survived** |

(My first run of the intent-cap mutation showed a failure that was carried over from the previous mutation; I re-ran it in isolation and it survived.)

## Recommendations, in order

1. Fix H1 (role regex false positives) and add the 6 rows as tests; fix M1 misses.
2. Fix H3 (use the highest-weight passing kind) with a test per pair.
3. Fix H2 (weak hiring keywords) in `_judge_kind`.
4. Tighten intent (M4, M6), add `SignalKind.INTENT` (coordinator), document the UNVERIFIED shape (M3).
5. Pin numeric values in tests (M5). Consider a slower decay for exec_move.
6. Normalise join keys (M2).

## What I could not verify

Real ZoomInfo scoop wording, type names for `Open Position` / `Management Move` (marked UNVERIFIED by 1a), the real topic list, the real `search_intent` shape, and whether `publishedDate` is stable for re-published scoops. My 85 rows are invented and written by me, so the precision and recall figures are indicative only.


---

# Re-review after fixes (worker commit e5d618b, merged into this branch)

Offline, read-only, invented data only. Held-out labels were written and committed first (`docs/review_1e_heldout.json`, commit ceba7d6; round 2 in `docs/review_1e_heldout2.json`), then run.

## Verdict: **PASS WITH CAVEATS** (no blocking failure; two medium items and one design decision for Alex)

## 1. Tests and determinism
| Check | Result |
|---|---|
| `pytest tests -q` | 353 passed |
| `pytest tests -q -W error` | 353 passed |
| `PYTHONHASHSEED` 0, 7, 123 on `test_signals.py` | 211 passed each |
| Radar over the fixture, 40 shuffles of scoops and accounts | identical |

## 2. Status of earlier findings
| Finding | Status | Evidence |
|---|---|---|
| H1 role false positives | **FIXED** on my original 85 rows | `_NEGATIVE` window (`signals.py:129-143`). All 10 earlier FPs and the 3 FNs (Chief Customer Officer, Customer Service and Support, Head of Mobile) now correct. Only 3 rows disagree with my original labels: 76, 77, 85, which the worker relabelled (see below). New weakness: held-out round 2 (section 3) |
| H2 weak hiring keywords | **FIXED** | `WEAK_KEEP_KEYWORDS`, `hiring_context` (`signals.py:154-170`). "support staff for the hospital ward", "drivers for its delivery platform", "warehouse support" all dropped, in both held-out sets |
| H3 kind precedence | **FIXED** | `_by_weight` (`signals.py:77`). Spot checks below |
| M1 recall misses | **FIXED** except "Chief Information and Digital Officer" (section 3) | |
| M2 join keys | **FIXED** | `_key` (`signals.py:336`): `{1: acct}` and `" 1 "` match, `"01"` does not (correct) |
| M3 UNVERIFIED docs | **PARTLY** | in code and `docs/signals_notes.md`; shared docs are for the coordinator |
| M4 zero-strength intent / source id | **FIXED** | score 60 is skipped with a reason; source `zoominfo_intent:<company>:<topic>` |
| M5 tautological tests | **FIXED** | literal values pinned; all 3 earlier survivors now killed (M3/M4/M18 below) |
| M6 "Onboarding" intent | **FIXED** | only "product onboarding" / "user onboarding" |
| L2, L3 | documented in `docs/signals_notes.md` | |
| L1 duplicate id, L4 unused `config` | not fixed (accepted) | |

**On the worker's relabel of rows 76/77/85** (nurse / paralegal / medical hire with "digital", "app" or "product launch"). I originally labelled them keep and called them arguable. Dropping them is the safer policy and I accept it, but it is a policy change, not a bug fix, and it costs a little recall. It is documented in `docs/signals_notes.md`.

## 3. Held-out test (the important one)
128 fresh rows (round 1), labels committed before running. 67 keep / 61 drop. Rows I flagged `judge` are my own borderline calls and are reported separately. Wording covers passive voice, "joins as", "steps into the role of", "has been named", promoted/departs, comma and "and" titles, acronyms (CPO/CDO/CIO/CTO/CXO/VPE), role in the 2nd sentence, mixed relevant+negative words, ANZ phrasing, hiring plans, Project/Pain with and without topics, funding/M&A/partnership/launch mixes, age boundaries.

| Set | TP | FN | FP | TN | Precision | Recall |
|---|---|---|---|---|---|---|
| Round 1, all 128 rows | 62 | 5 | 1 | 60 | 98.4% | 92.5% |
| Round 1, excluding my judge rows (111) | 57 | 2 | 0 | 52 | **100%** | **96.6%** |
| Round 2 (28 rows, deliberately NEW negative vocabulary), excluding judge | 8 | 1 | 6 | 1 | **57%** | 89% |

Round 1 is mostly the same vocabulary family the worker tuned against, so it flatters the rules. Round 2 was written to avoid the worker's `_NEGATIVE` words, and it shows what real-world data will do.

**Round 1 misses**
| ID | Text (fake) | Why |
|---|---|---|
| H015 | "recruited a Head of Platform Engineering" | a word sits between title and domain; `_TITLE_THEN_DOMAIN` allows only `global/regional/senior/group/...` |
| H019 | "new Chief Information and Digital Officer" | `Chief` + `Information` is not a domain pair; the acronym regex needs `chief information officer` literally |
| H029 (judge) | "Head of Self-Service Experience" | not a listed domain; acceptable |
| H033 (judge) | "Chief Customer Officer to unify sales, success and support" | the `sales` word within 3 tokens after the role triggers `_NEGATIVE`; this is the known "negative word after a real buyer role" weakness |
| H064 (judge) | "plans to hire 30 engineers ... to build its customer app" | bare `engineers` is weak; acceptable |
| H030 (judge, FP) | "formerly Head of Product, will step up as interim CEO" | role appears only as a past title |

**Round 2 misses**
| ID | Text (fake) | Pattern |
|---|---|---|
| G001-G005 | "Product Manager, Fresh Produce", "Product Manager, Dairy", "Head of Product, Wine Portfolio", "Head of Product Quality Assurance (food plant)", "Director of Product Assurance, Pharmaceuticals" | the word *product* in a company that sells physical products or retail category management. Text alone cannot separate this from a software product manager |
| G026 | "Executive Assistant to the CTO" | `CTO` alone is accepted as a role; the person is an assistant |
| G013 | "Chief Information and Technology Officer" | same hole as H019 |
| G006/7/9/10/17/19 (judge) | plant "Technology Manager", retail "Head of Customer Service", "Head of Product Engineering, Mining Equipment", "Head of Digital Mining Operations", "Product Lead, Home Loans", "CMO replacing the Chief Digital Officer" | industry-context ambiguity; I labelled these drop, the rules keep them |

**Funding / M&A / partnership / launch / Project / ages** in round 1: all labelled rows correct, including "Fund Y has invested $3 million in Example Co" (keep), investor notes (drop), age 45 keep / 46 drop, unknown types dropped.

**Recommended fixes, ranked by value**
1. **Make the industry problem visible rather than solved by regex** (value: high). "Product Manager, Dairy" will keep getting through. Pass the account's own signal (does the account have a digital product? ICP/ZoomInfo industry) into the ranker, and have the call sheet show `reason` so Alex sees "kept: new hire in a relevant role: 'Product Manager'" and can judge. Do not keep tuning the regex against retail vocabulary.
2. Add a negative-lookbehind for "assistant to", "executive assistant", "PA to" before a role acronym or title (`signals.py:123`); one-line fix for G026.
3. Add "chief information and digital", "chief information and technology", "chief data and ..." handling: extend `_ROLE_ACRONYMS` with `chief\s+information\s+and\s+(?:digital|technology)\s+officer` (`signals.py:123`); fixes H019, G013.
4. Allow one free word between title+of and domain in `_TITLE_THEN_DOMAIN` (`Head of Platform Engineering`, `Head of Cloud Platform Engineering`), and add a pinned test.
5. Limit `_NEGATIVE` to the role phrase plus words up to the first clause break or `to/who/will`, so "Chief Customer Officer to unify sales, success and support" is kept (H033).
6. `_MARKETING` (`signals.py:126`) is now redundant: mutation M9 (removing it) changes no test and no output on my sets because `_NEGATIVE` already contains `marketing`. Remove it or add a test showing it matters.

## 4. Competing types, 5 hand-checked pairs (expected written first)
| Types | Expected | Actual |
|---|---|---|
| Executive Move + Funding | exec_move (0.90 > 0.70) | exec_move |
| Funding + Partnership | funding (0.70 > 0.45) | funding |
| Product Launch + Project (CX topic) | product_launch (tie 0.80, alphabetical) | product_launch |
| New Hire + Hiring Plans | new_hire (0.65 > 0.55) | new_hire |
| M&A + Funding + Partnership | funding (0.70 > 0.60 > 0.45) | funding |
| Exec (fails: CFO) + Product Launch | product_launch | product_launch |
| Project (no buyer topic, fails) + Hiring (ok) | hiring_plan | hiring_plan |

Strength hand checks: exec_move 21 d = 0.9 x 0.5^(21/45) = 0.6513 (code 0.6513); exec_move 45 d = 0.45; new_hire 30 d = 0.4095; funding 45 d = 0.1585. All match.

## 5. Mutation re-run (21 mutations in a scratch copy of the merged tree)
Killed by a test: age `>=`; hiring drop-wins-always; `COMBINE_DAMPING` 1.0 (was a survivor); default half-life 30 (survivor); exec half-life 45 -> 21; new_hire half-life 45 -> 21; exec weight 0.9 -> 0.6; intent weight 0.5 -> 1.0; dedupe off; `top_n` slice off; kind choice reversed; tie-break reversed (my first version was an equivalent mutant; the corrected one is killed by `test_every_type_pair_picks_highest_weight_kind[product_launch-project_pain]`); `_NEGATIVE` off; negative window 3 -> 0 words; weak words emptied; zero-strength intent emitted; intent cap off (was a survivor); "onboarding" topic allowed; investee rule off; future-dated not clamped.
**Survived:** only the `_MARKETING` removal (M9), an equivalent mutant (redundant code, recommendation 6). Baseline 211 passed.

## 6. Intent versus a real scoop (item 4)
Today `intent_to_signals` is not used by `trigger_radar`, and the row shape is UNVERIFIED, so no ranking effect yet. If the ranker adds it, the practical effect with the trigger weight of 40 points (assuming points = 40 x strength):
- Maximum intent (score 100, audience A, undated) = 0.50 = 20 points.
- Funding 30 days old = 0.7 x 0.5^(30/21) = 0.26 = 10.4 points; funding 45 days old = 0.1585 = 6.3 points.
So the notes are right: a top-scoring unverified intent row beats a real, dated but decayed scoop, and a lower cap of 0.3 (12 points) would still beat the 30-day funding scoop. An undated intent row also never decays.

**Recommendation.** A cap alone does not give the property you want. Use both: (a) when an account has any real scoop signal, intent counts only as an "other" in `combined_strength` (damped by 0.5) and is never the `best_signal`, so the opener quotes the dated event; (b) while the shape is UNVERIFIED, cap standalone intent at 0.3 and give it a 21-day decay even when `recorded_date` is missing (treat missing date as "age unknown, use 21 days", not "age zero"). The call sheet must label an intent-only account "interest signal, not a dated event" so no opener presents it as news (CLAUDE.md rule 6).

## 7. Half-life versus `max_age_days` (item 5)
With a 45-day half-life the exec_move strength is 0.45 on day 45 and the account vanishes on day 46: a cliff. At that point it still outranks a 7-day partnership (0.357) and a 20-day product launch (0.4134). Other kinds are at 0.16 on day 45, so the cliff is harmless for them. In practice the connector pull was last-30-days in 1a, so the cliff rarely bites until the pull window is widened.

**Recommendation.** Make the cut-off per kind: exec_move and new_hire 90 days (factor 0.25, strength about 0.22), all others 45 (or 30 for partnership and hiring plans), plus a minimum strength floor (about 0.10) with a logged reason, instead of a single global number. This needs a config key (`signals.max_age_days_by_kind`) and a wider scoop pull window for people moves; both belong to the coordinator (`src/config.py`, scoop query in Phase 2b). Scoops count against a record limit, so widen only the people-move query.

## 8. Other
- **Chief Data Officer.** I agree it stays a drop by default. Pendo's buyers (product, digital, CX, support, IT, digital workplace, engineering) own the in-app experience; a CDO buys data and BI platforms. A "Chief Data and Analytics Officer" at a product-led company is borderline, but the dropped verdict carries a visible reason, so Alex can override it. If he wants it, add `chief data and analytics` to `_ROLE_ACRONYMS` deliberately rather than loosening `_DOMAIN`.
- UNVERIFIED intent text is still missing from the shared docs (`docs/interfaces.md`, `connector_notes.md` UNVERIFIED list); the coordinator said they will add it.

## Failing items and severities
None blocking. In priority order:
1. **Medium, `signals.py:123`:** acronym and title matching accepts roles held by someone else (`Executive Assistant to the CTO`) and misses "Chief Information and Digital/Technology Officer" (H019, G013, G026).
2. **Medium, `signals.py:129-143` and `115-121`:** negative window drops "Chief Customer Officer to unify sales, success and support"; `Head of Platform Engineering` is missed (H015, H033).
3. **Design, whole module:** the word *product* in non-software companies (round 2 precision 57%) cannot be fixed by regex; needs account-level context in the ranker and visible reasons on the call sheet.
4. **Low, `signals.py:126`:** `_MARKETING` redundant (equivalent mutant).
5. **Intent ranking risk and exec_move cut-off cliff:** see sections 6 and 7; decisions for the coordinator, not defects in the module today.

## What I could not verify
Real ZoomInfo wording, real topic names, the real intent shape, whether `publishedDate` is stable on republished scoops, and how the 1f ranker will use the numbers (I assumed points = 40 x strength). Round 2 is small (28 rows), so its 57% figure is a warning sign, not a measured rate.
