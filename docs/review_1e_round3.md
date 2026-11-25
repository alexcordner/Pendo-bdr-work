# 1e round 3: fresh held-out test against merged `main` (8d74800)

Reviewer, offline, read-only, no src edits, invented data only. 170 new fake scoops (`docs/review_1e_heldout3.json`, ids T001-T170). Labels were written and committed first (commit 661fc20), then run through `filter_scoops` from `origin/main`. `pytest tests -q` on main: 968 passed.

Industries: banks, super funds, universities, councils, telcos, retailers, mining, healthcare, logistics, SaaS, government digital services, AU/NZ phrasing; passive voice, multi-sentence, abbreviations, assistants, interim/acting/former roles, board roles, hiring plans, Project/Pain notes, funding as investor vs investee, M&A / partnership / launch, ages. 77 keep / 93 drop labels; 33 rows flagged `judge` (my own borderline calls, reported separately).

## Numbers
| Set | TP | FN | FP | TN | Precision | Recall |
|---|---|---|---|---|---|---|
| All 170 rows, as labelled | 66 | 11 | 21 | 72 | 75.9% | 85.7% |
| **Excluding judge rows (137), as labelled** | 59 | 4 | 9 | 65 | 86.8% | 93.7% |
| **Excluding judge rows, with 2 label corrections (below)** | 59 | 2 | 9 | 67 | **86.8%** | **96.7%** |
| Judge rows only (33) | 7 | 7 | 12 | 7 | 36.8% | 50.0% |

**Label corrections (my error, not the filter's).** T169 (partnership, 40 days) and T170 (hiring plan, 44 days) were dropped as "expired: limit 30 for partnership / hiring_plan". I had labelled them keep from the old 45-day cut-off; the per-kind limit is the policy I recommended, so the filter is right.

For comparison, my round 1 and round 2 sets (now regression tests, no longer held out): round 1 TP 65 FN 2 FP 1; round 2 TP 9 FN 0 FP 5; every remaining miss there is a judge row. So the earlier tuning held, but fresh vocabulary still costs about 13 points of precision (round 3 non-judge precision 86.8%).

## Miss patterns (non-judge rows)
**False positives (9)**
| ID | Text (fake) | Pattern |
|---|---|---|
| T010 | "Chair of the Risk Committee, a former CIO of a rival lender" | role belongs to a *former* post in an appositive; board role |
| T077 | "appointed to its Board; was previously Chief Product Officer at a large retailer" (flagged ambiguous) | board appointment with a past buyer title |
| T078 | "appointed a Chair. She is also a director of two technology companies" | `director of two technology` matches title + free word + domain |
| T013 | "a CIO, who will oversee its $40 billion investment portfolio" | CIO is Chief Investment Officer here; acronym accepted blindly |
| T089 | "welcomed a Head of Procurement. She replaces the former Head of Digital" | the buyer role is the predecessor's |
| T122 | "hiring a Product Compliance Officer and a Product Safety Auditor" | hiring path keeps on bare keyword `product`; the `_NEGATIVE` list is not applied there |
| T148 | "Example Capital participated in a $15 million round for Other Co" | investor note not caught: no `invest` verb |
| T149 | "Example Ventures led a seed investment in Other Co" | same (`led`) |
| T152 | "Example Super has taken a stake in Other Co's infrastructure arm" | same (`taken a stake`) |

**False negatives (2, after label corrections)**
| ID | Text (fake) | Pattern |
|---|---|---|
| T035 | "Head of E-commerce Product" | free word is hyphenated (`E-commerce`) |
| T106 | "Chief Customer and Digital Officer" | `Chief Customer` is not followed by `Officer` directly; the `and` list is not parsed (the earlier Chief Information and Digital fix did not generalise) |

**Judge rows that went wrong (for Alex to decide, not counted)**
- Kept but I would drop: retail / logistics "Head of Customer Service" (store or depot operations), mining "Head of Technology, Mine Automation", "Head of Fleet Technology and Telematics", "Head of Wholesale Product" (flagged ambiguous, kept at x0.7), CTO resigned / former CTO joined a competitor (a departure is not a new leader), a REIT buying a shopping centre (M&A), a sponsorship partnership, a frozen-meals launch, a bank loan facility, "Customer Success Manager" (IC).
- Dropped but I would keep: "Group Executive, Technology and Operations", "Chief Member Officer", "Director of Student Experience Technology", "Head of Conversion and Experimentation", "Staff Product Designer", "Head of Intranet and Collaboration", a Project note about a customer feedback platform whose topic is "Customer Experience Management".

Rows that behaved well (all as labelled): assistant / chief-of-staff / PA rules (T067-T069), Chief Nursing / Credit / Risk Officers, product marketing / compliance / procurement titles, "Head of Product, who previously worked in finance and legal technology" (kept), "Director of Engineering, formerly of a hospital group" (kept), "Head of Digital. She replaces the former Head of Procurement" (kept), generic "more open roles in X" notes (all dropped), warehouse / driver / aged care / trades / lecturer hiring (dropped), investee vs investor in the `invest` form, age cut-offs (exec 50 days kept, 95 dropped; funding 50 days dropped).

## Ambiguous-role flag
Across all 3 held-out sets (326 rows) the flag fired on exactly **3 kept rows**: G017 "Product Lead, Home Loans", T031 "Head of Wholesale Product", T077 (the board appointment above). All three were truly non-digital or non-buyer, so the flag was right 3 of 3 times and fired **0 times on a truly digital role** (none of the 77+ real keeps in round 3, nor round 1 or 2, was flagged).
Its weakness is recall and consequence:
- One kept non-digital "product" row was not flagged: T122 (hiring path never sets the flag).
- Flagged rows are only discounted to 70% of strength, so a flagged non-buyer still ranks around 0.5 for a fresh new hire. The flag is good as a warning label, weak as a filter.
- The 5 physical-goods rows from round 2 (Dairy, Wine, Quality Assurance, Pharmaceutical Product Assurance, Fresh Produce) are now dropped or flagged, so the physical-goods list works on that vocabulary; it has not been tested on a vocabulary outside round 2 except T031.

## Top 5 recommended fixes, ranked by value
1. **Investor-note rule (funding), `signals.py` `_is_investor_note` (about line 253).** Add investor verbs beyond `invest`: `participated`, `led`, `co-led`, `backed`, `took a stake`, `taken a stake`, `acquired a stake`, `provided a loan/facility`, `committed`. Three of nine non-judge FPs and one judge FP (T148, T149, T152, T151). A funding scoop with no `raised/secured/received/closed/series/seed` word and the company as grammatical subject should default to investor. Highest value because funding is the second-strongest kind and these are cheap, certain errors.
2. **Predecessor, former, and board context for roles (`role_match`, about line 200).** Reject a role phrase that is preceded within about 4 words by `former`, `previously`, `replaces`, `replacing`, `succeeds` (the buyer role is someone else's) and reject any role when the description has `board`, `chair`, `non-executive`, `non-exec director`. Fixes T010, T077, T078, T089, plus judge rows (former CTO, G019). This is the largest single group of false positives and is the same error as the "role belongs to someone else" assistant rule the worker already added.
3. **CIO disambiguation (`_ROLE_ACRONYMS`, about line 141).** Do not accept `CIO` when the description contains `investment`, `portfolio`, `funds under management`, `assets under management`, `superannuation` within the same sentence. Fixes T013, and applies to every super fund and asset manager, a very common AU/NZ account type.
4. **Hiring path: reuse the negative list and drop bare `product` as a keep keyword.** In `_judge_kind` for HIRING_PLAN, run `_NEGATIVE` over the tail of the match and treat a bare `product` (as in "Product Compliance Officer", "Product Safety Auditor") as weak unless it is `product manager/owner/designer/team/lead` (already in `_HIRING_CONTEXT`). Fixes T122.
5. **List parsing for "Chief X and Y Officer" and hyphenated free words (`_TITLE_THEN_DOMAIN`, about line 135).** Allow `chief <word> and <domain> officer` and a free word with a hyphen. Fixes T106 and T035; the earlier "Chief Information and Digital Officer" fix should become a general pattern rather than a literal.

Not recommended: tuning more for judge rows. They split along industry context (store, depot, plant), which regex cannot resolve; the right response is account-level context in the ranker and the visible `Kept:` reason, which is now in place.

## What I could not verify
Real scoop wording; whether ZoomInfo gives the investor or the investee as the scoop's company in investor-style notes (decides how many of fix 1 matter); how the ranker weighs a flagged row (I did not read `ranker.py`). Round 3 is 170 invented rows written by me, so the 86.8% / 96.7% are indicative, not production rates.
