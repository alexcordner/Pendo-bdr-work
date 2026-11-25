# Signals notes (task 1e, after review)

Module: `src/signals.py`. Offline, deterministic, no LLM. Tests: `tests/test_signals.py` (fixtures `tests/fixtures/signals_*.json`, all synthetic).

## Strength = base weight x recency decay
| Kind | Base weight | Half-life (days) |
|---|---|---|
| exec_move | 0.90 | 45 |
| product_launch | 0.80 | 21 |
| project_pain | 0.80 | 21 |
| funding | 0.70 | 21 |
| new_hire | 0.65 | 45 |
| ma | 0.60 | 21 |
| hiring_plan | 0.55 | 21 |
| intent (UNVERIFIED) | 0.30, hard cap 0.30 | 21 |
| partnership | 0.45 | 21 |

### Age limits and floor
| Kind | Max age (days) |
|---|---|
| exec_move, new_hire | 90 |
| funding, product_launch, ma, project_pain | 45 |
| partnership, hiring_plan, intent | 30 |

`KIND_MAX_AGE_DAYS`. A kept scoop is exactly at the limit on its last day and dropped one day later ('Dropped: expired, 46 days old (limit 45 for funding)'). 
**Two different age settings, do not mix them up:**
- `KIND_MAX_AGE_DAYS` (this module) decides when a scoop is DROPPED by `filter_scoops` ('Dropped: expired...'). `config.signals.max_age_days` is used here only for a kind missing from that table (none today).
- `config.signals.max_age_days` (45) is only a FALLBACK now. The ranker's recency is `1 - age / signals.max_age_for(best signal's kind, config)`, floored at 0, so an exec move 60 days old (limit 90) still earns recency points. (Resolved in 1f-final; earlier notes said the ranker used the config value as its horizon.)
**Phase 2b: the scoop pull window must be 90 days for people-move scoops** (Executive Move, New Hire, Management Move), otherwise the 90-day limit is never used. That is the caller's job; this module only filters what it is given.
Minimum strength floor `MIN_STRENGTH_FLOOR = 0.10`: a signal weaker than that is dropped with the reason 'faded below floor (0.09 < 0.10, N days old)'. With the default tables every kind is still at or above 0.10 on its last allowed day (exec_move 0.225, new_hire 0.1625, funding 0.1585, partnership 0.1672, hiring 0.2043), so the floor only bites on ambiguous roles, low intent scores or when the tables are tuned.

`strength = round(base * 0.5 ** (age_days / half_life), 4)`. Constants: `KIND_BASE_WEIGHT`, `RECENCY_HALF_LIFE_DAYS` (21), `KIND_HALF_LIFE_DAYS` (exec_move, new_hire: 45).
Literal checks: exec_move at 21 days 0.6513, at 45 days 0.45; funding at 45 days 0.1585; product_launch at 10 days 0.5751.
An ambiguous role (below) multiplies the strength by `AMBIGUOUS_ROLE_FACTOR` = 0.7 (so exec_move 5 days old: 0.8333 clear, 0.5833 ambiguous).
Several signals on one account: `combined = 1 - (1 - best) * prod(1 - 0.5 * other)` (`COMBINE_DAMPING`), e.g. [0.5, 0.4] gives 0.6, [0.5, 0.4, 0.2] gives 0.64.

A multi-type scoop takes the passing kind with the highest base weight (so Executive Move + M&A is exec_move, Project + Partnership is project_pain). Equal weights (product_launch / project_pain, both 0.80) tie-break alphabetically by kind name. There is no separate precedence list.

## Role rules (Executive Move / New Hire / Management Move)
`role_match(desc) -> RoleMatch(role, ambiguous)` (`relevant_role(desc)` returns just the role).
- **Match:** a title word (chief, head, vp, director, manager, lead, owner...) next to a domain word (product, digital, customer experience / success / support / service / care, CX, UX, engineering, technology, IT, mobile, digital workplace, employee experience, transformation), with at most ONE free word between them ("Head of Platform Engineering"), or CTO / CPO / CDO / CIO / CXO, "Chief Customer Officer", "Chief Information and Digital/Technology Officer". The connector `for` does not link a title to a domain, and `officer` is not a title word by itself.
- **Someone else's role:** "Executive Assistant to the CTO", "PA to the Head of Product" do not count (lookbehind for assistant / PA / EA / secretary / chief of staff + to/for).
- **Non-buyer function:** rejected when the role phrase or up to 3 words after it contain supply, compliance, procurement, marketing, social media, manufacturing, finance, legal, facilities, safety, sales, risk, warehouse, aged care, hospital, network, support services, HR, people, recruiting. The window stops at to / who / will / as / after / that / which / previously / formerly / from / at, so "Chief Customer Officer to unify sales, success and support" is kept.
- **Physical-goods product roles:** a `product` role followed (within 4 words) by food, dairy, wine, beverage, plant, factory, FMCG, grocery, produce, supermarket, apparel, fresh, meat, bakery, pharma, mining, merchandise, agriculture, farm, textile, quality assurance, assurance is dropped ("Product Manager, Dairy", "Head of Product Quality Assurance for its food plant").
- **Ambiguous (kept, flagged):** a `product` role where either the rest of the description has retail / store / manufacturing / food / equipment / pharma / construction context, or the role is followed by a comma and a category word that is not a digital word or geography ("Head of Product, Footwear", "Product Lead, Home Loans"). The verdict reason ends 'role ambiguous: check it is a digital product role' and the strength is x0.7. Regex cannot settle these; the call sheet shows the reason so Alex can judge.
- `_MARKETING` pre-filter removed: `marketing` in the negative list already drops "Product Marketing Manager" and "Digital Marketing Lead".
Role table: `tests/fixtures/signals_role_table.json` (the reviewer's 85 invented rows): 41 TP, 0 FP, 0 FN, 44 TN. Rows 76, 77 and 85 (a nurse / paralegal / medical hire with "digital", "app" or "product launch" in the text) were relabelled from keep to drop (decision H2).

### Held-out results (tests: `test_heldout_*`; files `signals_heldout1.json`, `signals_heldout2.json`, written by the reviewer before the filter was run, judge rows excluded)
| Set | Reviewer's numbers on e5d618b | Now |
|---|---|---|
| Round 1 (111 rows) | precision 100%, recall 96.6% | precision 100%, recall 100% |
| Round 2 (16 rows, new negative vocabulary) | precision 57%, recall 89% | precision 100%, recall 100% |
Caution: round 2 is no longer held out. Its misses drove the physical-goods list, the assistant rule and the extra acronyms, so 100% there is a regression guard, not a generalisation estimate. Unseen phrasings tried afterwards (not in any fixture): "Product Manager, Frozen Foods" dropped; "Head of Product, Footwear", "Category Product Manager, Homewares", "Product Manager, Building Materials", "Head of Product, Home Loans" kept but flagged ambiguous; "Head of Product, ANZ" / "Head of Product, Digital" kept clean. Judge rows from both sets still disagree with the reviewer's labels on: G006 (plant Technology Manager) and G007 (retail Head of Customer Service) kept; G010 (Digital Mining Operations) kept; G017 (Product Lead, Home Loans) kept but now flagged ambiguous; G019 (CMO replacing the Chief Digital Officer) kept on the old title; H030 (formerly Head of Product, now interim CEO) kept; H029, H064 dropped though the reviewer wanted keep.

### Known remaining weaknesses
- Only one free word between title and domain: "Head of Cloud Platform Engineering" is missed.
- "Head of Mobile Network Operations" is dropped by the `network` negative; "Head of Mobile" is kept.
- A negative word within 3 words after a real buyer role (before a stop word) drops it ("Head of Digital, Finance and Legal").
- Retail / goods industry cannot be judged from text alone; only the ambiguity flag helps. The ranker should compare with the account's own industry or product signals.
- Chief Data Officer, Chief Operating Officer, CISO and Enterprise Applications Manager are dropped by design. Chief Data Officer is a call for Alex.
- Hiring notes do not use the physical-goods or assistant rules.
- Role text is the description only; the scoop has no title field.

## Hiring Plans / Open Position
Kept when there is buyer-role context (a relevant role, or product manager / owner / team, UX, CX, customer success, mobile app developers, digital product / experience / transformation, software engineers, engineering manager / lead / team, CTO), or a relevant topic, or a strong keyword (`keywords_keep` minus the weak words). Weak words never keep a note on their own: support, app, platform, engineering, mobile, launch, transformation (`WEAK_KEEP_KEYWORDS`). A drop keyword (nurse, driver, warehouse, legal, medical & health, paralegal) wins unless there is buyer-role context.
Review L2, documented: a topic does not rescue a note that has a drop keyword and no role context ("more open roles in Medical & Health" with topic Product Management is dropped).
Review L3: topic matching is exact (case and whitespace aside); "Customer Experience Management" does not match. Real ZoomInfo topic names are unverified.

## Funding
Investor notes ("Fund X invested in Y" when the scoop company is the investor) are dropped. If the scoop company is the object ("invested $3M in <company>") it is kept.

## Join keys
Both sides are compared as stripped strings, so int ids and padded strings match; "01" and "1" do not.

## Intent (UNVERIFIED)
`intent_to_signals(rows, accounts, config, today, skipped=None)`. The row shape is our guess and has not been checked against the live ZoomInfo `search_intent` connector:
`{company_id, topic, signal_score 60-100, audience_strength A-E, recorded_date?}`. Never called live.
- Kind `SignalKind.INTENT`, strength = (score - 60) / 40 x audience factor (A 1.0, B 0.85, C 0.7, D 0.55, E 0.4), capped at 0.3 while UNVERIFIED, then decayed with a 21-day half-life.
- A row with no `recorded_date` is treated as 21 days old, never as brand new (so undated 100/A = 0.15). Older than 30 days is skipped ('expired').
- A score of exactly 60 gives strength 0, so the row is SKIPPED with a reason (not floored). Anything below the 0.10 floor is skipped as 'faded below floor'.
- Evidence reads 'ZoomInfo intent (UNVERIFIED shape): interest signal, not a dated event. ...' so the call sheet can label it honestly.
- `best_signal` never returns an intent signal while the account has any real (non-intent) signal; `combined_strength` then counts intent only as a damped extra (x0.5). Example: real 0.16 + intent 0.3 = 0.286. Intent-only accounts are labelled by their evidence.
- Topics: digital experience, product analytics, customer experience, digital adoption, product onboarding, user onboarding. "Onboarding" alone is not accepted.
- `Signal.source` = `zoominfo_intent:<company_id>:<topic>`.
- A 0.3 cap on its own would not stop intent beating a decayed scoop (a 45-day-old funding scoop is 0.16), which is why `best_signal` / `combined_strength` rank real signals first. Intent is not part of `trigger_radar`.
- Phase 2b must check the real shape and topic names, and add the line to the UNVERIFIED list in `docs/connector_notes.md` (shared doc, not edited here).

## Not fixed
- Review L1: a repeated scoop id (same id, different content) keeps the first silently. Impossible in practice, no logger in this module.
- Review L4: `config` is unused in `scoops_to_signals` (kept for interface symmetry).
- Review M3 (partly): the UNVERIFIED note is in this file and the code; `interfaces.md` and `connector_notes.md` are shared files and were not touched.

## Verdict reasons
Every `ScoopVerdict.reason` is a call-sheet sentence: 'Kept: Executive Move, role 'VP of Product', 5 days ago', 'Kept: Product Launch, today (future-dated, treated as today)', 'Dropped: generic hiring note (matches drop keyword 'medical & health', no buyer-role context)', 'Dropped: expired, 46 days old (limit 45 for product_launch)', 'Dropped: faded below floor (...)'. `verdict_summary(v)` returns it as one whitespace-collapsed line. `ScoopVerdict.ambiguous` carries the role-ambiguity flag (Signal has no field for it, so the flag lives on the verdict and in the strength).

## Round 4 fixes (branch phase1/1e-fixes)
- **Shared ZoomInfo ids:** `scoops_to_signals` takes `Mapping[str, Account | list[Account]]`. Every account in a list gets its own copy of the signal (same kind, strength, source, date, evidence; only `sf_account_id` differs). `trigger_radar` and `intent_to_signals` do the same (earlier: first account only). An empty list counts as unmatched.
- **Ambiguity travels with the signal:** a kept ambiguous role gets the suffix ` (role ambiguous: check it is a digital product role)` on `Signal.evidence`; `signals.is_ambiguous(signal)` tests for it. The suffix always fits inside `EVIDENCE_MAX_CHARS` (the description is shortened). The shared `Signal` model has no flag field, so this is the minimal carrier; a real field would be cleaner.
- **Investor notes (general):** investor wording now covers invest(ed/ment), participated in, led/co-led a round / investment / Series / seed, took/taken/acquired a stake in, backed/backing. If the company comes before the verb in the same clause (no 'raised/secured/received' between) it is the investor: dropped. If it comes after ('Fund Y led a round in <company>') it is the investee: kept. Otherwise kept only if money received is mentioned (raised, secured, received, closed, funding, Series X, seed). 'Backed by' alone is not a funding event; 'raised $5M, backed by Fund Z' is. 'Round' and 'backed by' no longer count as money received.
- **Role context:** a role preceded within 2 filler words by former / formerly / previously / ex-, or right after replaces / replacing / succeeds (optionally 'the former'), is not the new leader; 'She succeeds Jane as Chief Digital Officer' still counts. Any description that mentions board, chair or non-executive rejects every role in it ('The Board welcomed a new Chief Digital Officer' is dropped: a known false negative).
- **CIO:** the acronym is rejected when its sentence mentions investment, portfolio, funds / assets under management, assets, superannuation or AUM (Chief Investment Officer). The spelled-out 'Chief Information Officer' is unaffected.
- **Hiring path:** the non-buyer word list applies to hiring-role phrases, and bare `product` is a weak keyword ('Product Compliance Officer', 'Product Safety Auditor' drop; 'Product Manager / Owner / Designer / Lead / Team' still count).
- **Chief X and Y Officer:** parsed generally; counts when X or Y is a buyer domain word or 'information' ('Chief Customer and Digital Officer', 'Chief Product and Growth Officer'); 'Chief Risk and Compliance Officer' does not. Hyphenated free words are allowed ('Head of E-commerce Product').

### Held-out round 3 (`signals_heldout3.json`, reviewer's 170 rows, judge rows excluded, T169/T170 relabelled drop as the reviewer agreed)
| | Precision | Recall |
|---|---|---|
| Before this round | 86.8% (59 TP, 9 FP) | 96.7% (2 FN) |
| After | 100% | 100% |
Caution: like round 2, round 3 now drove the fixes, so it is a regression guard, not a fresh estimate. The fixes are general rules (verbs, former/board/CIO context, negative list in hiring), but fresh vocabulary will still cost precision. Not tuned: the reviewer's judge rows (industry context such as store or depot operations, mine automation, REIT deals, sponsorships), which regex cannot settle; the ambiguity flag and the visible 'Kept:' reason are the answer there.
