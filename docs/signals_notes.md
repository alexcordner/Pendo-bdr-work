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
| intent (UNVERIFIED) | 0.50, hard cap 0.50 | 21 |
| partnership | 0.45 | 21 |

`strength = round(base * 0.5 ** (age_days / half_life), 4)`. Constants: `KIND_BASE_WEIGHT`, `RECENCY_HALF_LIFE_DAYS` (21), `KIND_HALF_LIFE_DAYS` (exec_move, new_hire: 45). `max_age_days` (config, 45) still cuts scoops off: exactly 45 days is kept, 46 dropped.
Literal checks: exec_move at 21 days 0.6513, at 45 days 0.45; funding at 45 days 0.1585; product_launch at 10 days 0.5751.
Several signals on one account: `combined = 1 - (1 - best) * prod(1 - 0.5 * other)` (`COMBINE_DAMPING`), e.g. [0.5, 0.4] gives 0.6, [0.5, 0.4, 0.2] gives 0.64.

A multi-type scoop takes the passing kind with the highest base weight (so Executive Move + M&A is exec_move, Project + Partnership is project_pain). Equal weights (product_launch / project_pain, both 0.80) tie-break alphabetically by kind name. There is no separate precedence list.

## Role rules (Executive Move / New Hire / Management Move)
A title word (chief, head, vp, director, manager, lead, owner...) next to a domain word (product, digital, customer experience / success / support / service / care, CX, UX, engineering, technology, IT, mobile, digital workplace, employee experience, transformation), or CTO / CPO / CDO / CIO / CXO / "Chief Customer Officer". Rejected when the role phrase or the 3 words after it contain a non-buyer function (supply, compliance, procurement, marketing, social media, manufacturing, finance, legal, facilities, safety, sales, risk, warehouse, aged care, hospital, network, support services, HR, people, recruiting). The connector `for` no longer links a title to a domain. `officer` is not a title word by itself.
Role table: `tests/fixtures/signals_role_table.json` (the reviewer's 85 invented rows). Result: 41 TP, 0 FP, 0 FN, 44 TN, precision 100%, recall 100%. Rows 76, 77 and 85 (a nurse / paralegal / medical hire with "digital", "app" or "product launch" in the text) were relabelled from keep to drop to follow decision H2; the original labels would give recall 93%.
Caution: the rules were tuned while looking at this table, so 100% is optimistic. Held-out wording will miss. Phase 2b should read real verdict reasons.

### Known remaining weaknesses
- "Head of Mobile Network Operations" is dropped by the `network` negative; "Head of Mobile" is kept. Telco wording is a guess.
- A negative word within 3 words after a real buyer role drops it ("Head of Digital, Finance and Legal").
- Chief Data Officer, Chief Operating Officer, CISO and Enterprise Applications Manager are dropped by design (reviewer agreed). Chief Data Officer is a call for Alex.
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
- Kind `SignalKind.INTENT`, strength = (score - 60) / 40 x audience factor (A 1.0, B 0.85, C 0.7, D 0.55, E 0.4), capped at 0.5, decayed (21 days) when `recorded_date` is given.
- A score of exactly 60 gives strength 0, so the row is SKIPPED with a reason (not floored).
- Topics: digital experience, product analytics, customer experience, digital adoption, product onboarding, user onboarding. "Onboarding" alone is not accepted.
- `Signal.source` = `zoominfo_intent:<company_id>:<topic>`.
- The 0.5 cap keeps intent below any fresh scoop, but a heavily decayed scoop (for example a 45-day-old funding scoop at 0.16) can still be below intent. Intent is not part of `trigger_radar`.
- Phase 2b must check the real shape and topic names, and add the line to the UNVERIFIED list in `docs/connector_notes.md` (shared doc, not edited here).

## Not fixed
- Review L1: a repeated scoop id (same id, different content) keeps the first silently. Impossible in practice, no logger in this module.
- Review L4: `config` is unused in `scoops_to_signals` (kept for interface symmetry).
- Review M3 (partly): the UNVERIFIED note is in this file and the code; `interfaces.md` and `connector_notes.md` are shared files and were not touched.
