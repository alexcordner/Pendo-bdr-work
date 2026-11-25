# Independent review: task 2d (textcheck, opener / email-draft skills, voice.yaml)

Reviewer session: `session_01QShr1H7ghpKeoYqfRwTjBe`. Reviewed commit `7b98a97` (branch `phase2/2d-opener-lint`) against the 2c packet on
`origin/phase2/2c-callsheet` @ `16781aa`. Fully offline, read-only, FAKE data only. No connector was called. The worker's files
were not edited. Added: this file, `docs/review_2d_labels.json` (committed **before** the lint was run on it),
`docs/review_2d_real_packets.json` (real 2c `ResearchPacket.to_dict()` output from the synthetic world), `tests/test_review_2d_scratch.py`.

## Verdict: PASS WITH FIXES (not ready to wire into 2c / 2e until blockers B1 to B6 are fixed)

The design is sound and the code is clean: pure functions, deterministic, no I/O beyond `load_voice_config`, fails closed on most
paths, nothing in it or the skills can send or write anything. But measured on a 161-case adversarial corpus and an 84-case "normal
ANZ English" corpus the lint as shipped is **both too leaky and too strict**, and it does not fit the real 2c packet without glue:

| Measure | Result |
|---|---|
| Existing suite | **1438 passed** plain and with `-W error` (Python 3.13; `requires-python >=3.11`) |
| Adversarial recall (bad blocks that raise at least one *error*) | **113 / 161 = 70%** (openers 85/121 = 70%, emails 28/40 = 70%) |
| Recall *for the intended reason* (not an accidental hit) | **98 / 161 = 61%**. 13 of the "catches" are accidents of the sentence-start name rule, so a naive fix to the false positives would drop recall further |
| False positives on hand-written good blocks | 8 / 32 = **25%** (openers 5/22, emails 3/10) |
| False positives on 84 natural ANZ openers | **42 / 84 = 50%** (37% from ordinary sentence-starter words alone, 40% with the abbreviation CX) |
| False positives on 20 natural gatekeeper lines | **7 / 20 = 35%** |
| Real 2c packet fit | **5 mismatches**, two of which make correct text be rejected and one of which silently disables the ambiguous-role rule |
| Reviewer prototype of the recommended fixes (not the worker's code) | recall **143 / 161 = 89%**, false positives on legit blocks **15 / 116 = 13%** (some are prototype artefacts, see Section 8) |

Honesty note: my "good" and "natural" blocks are written by a model, as will be the real output, but real output will cluster near the
skill's own worked examples, so the live false-positive rate is **UNVERIFIED** and may be lower than 43%. Even at half of it, a ranked sheet of 10 accounts
would lose ~2 to 4 accounts a night to regenerate loops. Recall on the bad side is also measured on blocks *I* wrote to be nasty.

## 1. Checklist

| # | Item | Result |
|---|---|---|
| 1 | pytest plain and `-W error` | **PASS**: 1438 passed both ways (1442 passed + 24 xfailed with my scratch file) |
| 1 | Determinism | **PASS**: identical output over 4 `PYTHONHASHSEED` values on all 277 cases; inputs not mutated (`test_determinism_and_purity_of_check_functions`) |
| 1 | No I/O except `load_voice_config` | **PASS**: AST check, one `open` (`textcheck.py:126`), no os/sys/socket/random/datetime imports (`test_textcheck_has_no_io_except_load_voice_config`) |
| 1 | Python 3.11+ | **PASS** by static read (PEP 604 only in annotations behind `from __future__`); only 3.13 was run, so 3.11 itself is **UNVERIFIED** |
| 2 | Adversarial recall | **FAIL**: 70% / 61% (Section 3) |
| 3 | False positives, normal ANZ English | **FAIL**: 50% (Section 4) |
| 4 | Packet reconciliation with 2c | **FAIL** as-is, fixable (Section 5) |
| 5 | Skill quality | **PASS WITH FIXES** (Section 6) |
| 6 | voice.yaml, calibration, alex_examples | **PASS WITH FIXES** (Section 7). `calibrated:false` end to end works; FAKE `alex_examples` slots work (3 of 4 near-duplicates warned) |
| 7 | Hard-rule alignment | **PASS** (Section 9) |

## 2. Findings by severity

Each finding has an `F` id; the desired behaviour is encoded as an `xfail` test of the same id in `tests/test_review_2d_scratch.py`
(it XPASSes when fixed). Line numbers are for `src/textcheck.py` unless stated.

### BLOCKER (fix before integration)

**B1 / F-01, F-16. A hyphenated, apostrophe-less or alphanumeric company name makes even a verbatim-copied FACT line fail, and regenerating cannot fix it.**
The corpus tokenizer `_WORD` (:246) splits `smith-jones` into `smith`, `jones`, but the name-check regex (:373, `[A-Za-z][A-Za-z'&-]*`) keeps `Smith-Jones` whole, so it is
never "in the packet". Same for `Zip2Pay` (split at the digit into `Zip` and `Pay`), `3M` (`M`), `Co-op`, `Coca-Cola`. The account is then un-writable.
Repro: packet `account_name="Smith-Jones Partners"`, FACT 1 = the trigger evidence, copied exactly with its source and date
`->` `PROPER_NOUN_UNSUPPORTED name 'smith-jones'` on OPENER, FACT 1 and TRIGGER. (2c's own fixture names `Pat1` to `Pat8` hit the same bug: the real
packet's "Hi Pat1" is rejected as name `pat`; this is a fixture artefact, but the code path is real.)
Fix: tokenise names with the same regex as the corpus (add `-` to `_WORD` or split hyphenated tokens and check each part), keep alphanumerics whole.

**B2 / F-12. The `sentence_start_words` allow-list is the main false-positive source and cannot be completed** (:372-379, `voice.yaml:226`).
Any capitalised first word not in the ~120-word list is treated as a possible company name. 31 of 84 natural openers die on ordinary starters (3 more on the abbreviation CX):
*Honestly, Apologies, Maybe, Perhaps, Appreciate, Understand, Wanted, Know, Wonder, Imagine, Planning, Building, Rather, Totally, Nothing, Everything, Between,
Over, Look, Good, Morning, Afternoon, Straight, Fresh, Rolling, Ringing, Interested, Obviously* and the abbreviations *CX, AI, WA*. Place names not in the packet
(*Sydney, Melbourne, Auckland* for a Perth company) are flagged too. Gatekeeper lines suffer the same (*Morning, Might ...*).
A longer list will never converge (LLM output has unbounded starters) and the list also *hides* the real signal: the 13 accidental catches above are
openers that were rejected only because a hype or filler word happened to start a sentence.
Fix (prototype in the scratch file, FP 50% to 8% on the 84): check a sentence-initial word only if it is *company-shaped* (followed by another capitalised word,
or possessive `'s`), always check mid-sentence capitalised words, and add a small abbreviation allow-list (CX, AI, WA, NSW, QLD, VIC, SA, TAS, NT, ACT, CEO, CTO, CIO, SaaS, UX, Q1 to Q4 handled with the number rule).
Cost: a bare one-word company opener ("Acme uses us.") is no longer caught by the name rule, so the claim patterns and a new "other-company name" check must
carry it. Caveat: allow-listing CEO lets "appointed Dana as CEO" through where the packet says Chief Digital Officer (my O-B09), which only the fact/trigger overlap rule can catch.

**B3 / F-02. The real 2c packet key `ambiguity_flags` is ignored, so the ambiguous-role rule is silently OFF on real packets** (:230 reads only `flags`; the skill line 43 also says `flags`).
Repro: real-shape packet with `"ambiguity_flags": ["role ambiguous: check it is a digital product role"]`, opener "as Product Manager you must run the app" `->` no violation;
rename the key to `flags` `->` `ROLE_ASSUMED`. This is a CLAUDE.md honesty rule failing quietly. Fix: read `ambiguity_flags` (fall back to `flags`) and any `signals[].ambiguous == true`.

**B4 / F-03. Source strings do not line up with 2c: a correct, consistent citation is rejected.** 2c gives each scoop two source strings: the fact carries the *label*
("ZoomInfo scoop 2026-11-22") and has **no date**; the signal/trigger carries the *id* (`zoominfo_scoop:FAKE-S-1`) plus `date`. `_check_cited` (:464-477) requires the cited text to contain the matching
item's own source string, and the trigger and the first fact are the *same sentence*, so FACT 1 and TRIGGER must cite *different* strings for the same scoop. Measured on a real packet:
- label on FACT 1 and id on TRIGGER passes (the only accepted combination, and nothing in the skill says to do it);
- id on both (what the skill's worked examples do) `->` 2x `FACT_SOURCE_MISMATCH`;
- label on both `->` `TRIGGER_SOURCE_MISMATCH`.
Tie-break between the trigger item and the identical fact is by list order (`_match_item`, :455-461, strict `>`), which is fragile.
Fix: give every packet item `{source, source_label, date}` (2c side) and accept either string (textcheck side). See Section 5.

**B5 / F-04, F-11. The config fails open.** `_cfg` (:130-140) fills missing keys from `DEFAULTS`, whose lists are empty. `check_opener_block(text, packet, None)` or `{}`, an empty `voice.yaml`,
or a typo'd key (`banned_phrase:`) silently turns off banned phrases, claim patterns, event words, role patterns, send language and the whole sentence-start rule, with no error.
Repro: opener "Our game-changing tool helps." with `voice_cfg={}` `->` no `BANNED_PHRASE`. Also `calibrated: "false"` (a string) is truthy, so `_check_label` (:427-434) stops demanding the DRAFT STYLE label.
Fix: `load_voice_config` should validate (required keys, list types, `calibrated` must be a bool, unknown top-level keys are an error) and the check functions should raise on a config that is missing a required key instead of defaulting.

**B6 / F-05, F-06. Two cheap evasions of the deterministic rules.** (a) Invisible characters: a zero-width space, soft hyphen or NBSP inside a banned phrase (`touch<U+200B> base`, `quick<U+00A0>call`) is not matched (`_phrase_re`, :291-297 only normalises apostrophes). Cyrillic lookalikes
(Latin 'a' swapped for Cyrillic U+0430 in game-changing, launched, Congrats) defeat banned-phrase and event-word checks; fullwidth `！` is not an exclamation. An LLM will not do this on purpose, but it can emit NBSP and smart punctuation by accident, and a rule that can be dodged by a stray NBSP is not a deterministic guard.
(b) **Send language is checked only in emails** (:689-690, `voice.yaml:185`), so an opener or gatekeeper line "I've sent you an email" / "I'll send over a calendar invite" passes. Also the email patterns only match `sent (this|from|via|by)`, `send (this|it|now)`, `sending (this|now|it)`: "I've sent you a calendar invite", "I am sending you an invite" and "I'll send over an invite" all pass (E-B12 to E-B14).
Fix: NFKC-normalise, strip Cf characters, fold common Cyrillic/Greek lookalikes and raise a `UNICODE_SUSPECT` error on any remaining non-Latin letter; broaden the send pattern to `\b(sent|send|sending|forward|attach)\w*\b[^.?!]{0,30}\b(you|over|invite|calendar|now)\b` and run it on every text field.

### HIGH

**H1 / F-08. `TRIGGER_MISMATCH` compares only to the trigger evidence, not to the packet's other facts** (:389-405, 5-letter stem). A funding-triggered packet whose FACT 2 is "is hiring a Product Designer" rejects an opener or email that says "Bellbird is hiring a Product Designer" (5 natural openers, 2 of 10 good emails). Also `hired` vs evidence `hiring` and `raising` vs `raised` mismatch on the 5-letter prefix. Fix: allow an event word that appears in *any* packet item, compare by 4-letter stem.

**H2 / F-09. "One ask" is only a warning, and a single `?` hides a second ask.** `OPENER_MULTIPLE_QUESTIONS` is a warning (:519-523); "Who owns the app, and can I send you a calendar invite?" has one `?` and zero violations. The call-length data in the brief is the reason for the rule, so make it an error and count ask cues (`can I`, `could I`, `mind if`, `is it fine if`, "or"). Same single-`?` gap in emails (E-B07).

**H3. Invented-fact detection by overlap is too loose for FACT lines.** `fact_overlap_min: 0.6` lets 40% of a FACT line be invented, and single-letter tokens are dropped (`_tokens` :269), so "Series A" for "Series B", "NZ$18 million in a Series C" and "ended a partnership" for "announced a partnership" pass. Raising `fact_overlap_min` to 0.9 catches 3 more of my 10 meaning-changing cases (and 0 of 84 natural, verbatim-fact openers); keeping single-letter tokens catches the series letter. **Not fixable by lint:** meaning flips inside the *spoken opener* (`O-B06` "partnership ... ended", `O-B08` "appointed a new chief executive"): the opener has no overlap check at all, only names, numbers and event words. This needs the skill to force the opener to restate the trigger in the trigger's own words, plus Alex skimming, plus (optional) a second LLM "verifier" pass. State this limit in docs/voice_guide.md; it already lists "true-sounding paraphrase" under Known limits, but not that the opener is unchecked for it.

**H4. Number words are not checked** (the guide admits it). "about six hundred staff", "raised twenty million", "three weeks ago" pass. Prototype: flag number words from *two* upward unless they appear in the packet, exempt "(seconds|minutes|questions)". Legit "two weeks ago" is then a false positive, which is why Section 5 recommends passing `age_days`.

**H5. Weak hype/filler matching.** Literal whole-word phrases (:291-297) miss inflections and spacing: "cutting edge", "best in class", "revolutionary", "amazing", "unlocking", "touching base", "reach out / reached out", "just checking in", "I hope you're having a great week", "circling back" all pass. Fix by stemming and hyphen/space-insensitive matching, plus the additions in my prototype list (`CFG_ADD` in the scratch test).

### MEDIUM

**M1. `OPENER_PENDO_FEATURE` false positives** (:524, `voice.yaml:112`): "guide" ("who would guide the roadmap"), "analytics", "survey", "funnel", "nps" are ordinary words, and fire when the *account's own product* is analytics or a survey tool even though the packet says so (own-business repro: packet "builds product analytics and survey software", opener repeating it `->` 2 errors). Conversely real feature talk passes: "watch recordings of user sessions", "onboarding checklists". Fix: drop bare "guide(s)", let a word through if it appears in a packet fact, add "recordings", "checklist(s)", "drop-off", "segmentation".
**M2. Gatekeeper pitch list rejects receptionist English** (`voice.yaml:41-62`): `help` ("Could you help me find who looks after the app?"), `free` ("Is the head of product free?"), `our`, `we`, `software` ("who would I speak to about your software?"). 5/20 natural gatekeeper lines flagged for pitch, 2 more for sentence starters. Drop `help, helps, free, our, we, save` (they belong to the Pendo-claim patterns, not the gatekeeper), keep product-feature words.
**M3. Intent signals count as "real triggers".** 2c sets `has_real_trigger = bool(sigs)` including `kind=intent` ("interest, not a dated event"). The lint then allows "Saw Alpha is researching customer experience" (reaction words allowed because a trigger exists). Intent is private vendor data, not an observable event. Fix (either side): treat `kind == intent` as non-event: no "saw/noticed/congrats", no event words, TRIGGER line must label it as an intent signal.
**M4. Invented contact history passes.** "We spoke last week" / "Just tried you and left you a voicemail" are accepted with a packet whose history says "No Answer" or has none (E-B11). Add a pattern list (`we spoke`, `left (you )?a voicemail`, `last week`...) allowed only if `contact_history` supports it; the skill must say the history line is *not* an opener fact.
**M5. Invented date in a citation tail is not checked** (:464-477): when the packet fact has no date (all Salesforce facts in 2c), any date the model writes in `(source: ..., 2019-01-01)` passes (`F-13`). The tail is excluded from `_check_supported`. Require the tail date to equal the item's date, or be absent when the item has none.
**M6. `SIMILAR_TO_DISLIKED` crashes on non-string entries** (:530-533, `_jaccard` on `None`/int) and a blank `""` entry; and uses token Jaccard so only 3 of my 4 near-duplicates warned (it is a warning, fine). Validate `alex_examples` at load (F-14).
**M7. Warning-only rules that the skills treat as rules**: `EMAIL_SIGNOFF` ("Cheers, Alex"), `OPENER_NO_QUESTION`, `FACT_SKIPPED`, `TRIGGER_LINE_OMITTED`. "Cheers, Alex" and subject "Re: our chat" were rejected only by accident (the sentence-start rule). Quotes around a line (`OPENER: "Hi ..."`), bullets in a body and fake-thread subjects ("Re:", "Fwd:") pass.

### LOW

- `check_opener_block(None, ...)` raises `AttributeError` (callers should not pass None; fine, but a `PACKET_INVALID`-style `TEXT_INVALID` is cheap).
- `_PHONE` needs 8+ digits, `_URL` a fixed TLD list (`bit.ly` passes); spelled-out numbers or "pendo dot com" pass. Acceptable for a lint, worth a line in Known limits.
- Email greeting regex (`:609`) accepts only `hi|hello|hey|g'day`; "Morning Priya," is counted as a content line.
- `unsupported_claim_patterns` use `[\w ]{0,20}` windows that miss "Pendo customers cut tickets in half" and "customers see 40% higher adoption" (number rule catches the latter only).
- The 25-word / 8-second opener cap and 79-word email cap are from the skill and the brief; whether 25 words really is ~8 s of Alex's speech is **UNVERIFIED** (no audio); "Hi X, Alex from Pendo." already spends 5 of the 25 words.
- 2c fixture names with digits (`Pat1`) should be renamed digit-free so 2c tests do not trip the (real) tokenizer bug.

## 3. Adversarial recall (labels in `docs/review_2d_labels.json`, committed in `16cf4d3` before the lint was run)

143 opener blocks (22 good, 121 bad) and 50 email drafts (10 good, 40 bad). "Intended reason" = a violation code I wrote down as the right one before running.

| Category | n | caught (error) baseline | caught for the intended reason | caught by prototype |
|---|---|---|---|---|
| Meaning-changing paraphrase of a real fact | 10 | 5 | 5 | 3 |
| Plausible invented numbers/dates/percentages | 5 | 4 | 4 | 5 |
| Number words ('thirty') | 4 | 1 | 0 | 4 |
| Fabricated customer names / 'we help' | 5 | 5 | 4 | 4 |
| Fabricated trigger (packet has none) | 6 | 4 | 4 | 5 |
| Trigger/fact copied from another company | 4 | 4 | 4 | 4 |
| 'Saw you just launched X' unsupported | 4 | 2 | 2 | 3 |
| Contact career claim / 'congrats on the promotion' | 5 | 3 | 3 | 4 |
| Pendo statistics / results | 4 | 2 | 2 | 4 |
| Feature pitching in opener | 4 | 2 | 2 | 4 |
| Hype + filler phrases | 12 | 6 | 5 | 12 |
| Exclamation / emoji / link / email / phone | 10 | 10 | 5 | 9 |
| Multiple asks | 3 | 1 | 1 | 3 |
| Send language (opener/gatekeeper) | 3 | 0 | 0 | 3 |
| Role assumed (ambiguous flag) | 5 | 4 | 3 | 5 |
| Over-length opener | 2 | 2 | 2 | 2 |
| Gatekeeper pitching / no role / long | 7 | 6 | 6 | 6 |
| Label missing + block shape | 11 | 10 | 10 | 11 |
| FACT-line problems (source, date, padding, invented) | 8 | 8 | 8 | 8 |
| Unicode lookalikes / zero-width / fullwidth | 9 | 6 | 3 | 9 |
| Email drafts (all categories) | 40 | 28 | 25 | 35 |
| **All bad (openers 121 + emails 40)** | 161 | **113** | | **143** |

### Misses (bad blocks that raised no error), with the code that should have caught them

Openers (36): `O-B03` series letter changed, `B06` partnership ended, `B07` meaning flip in FACT, `B08` "new chief executive", `B10` fact + invented clause (FACT_UNSUPPORTED; 7/9 overlap), `B13` "went live in March", `B16` six hundred, `B18` three weeks, `B19` thirty-person (NUMBER_UNSUPPORTED, number words), `B26` "growing fast", `B28` "Big week at Marlow?" (INVENTED_TRIGGER, no event word), `B36` "shipped", `B38` "top of the store" (TRIGGER_MISMATCH), `B40` "moved over from banking", `B43` "congrats on the promotion" with another trigger present, `B45`, `B46` Pendo results without a number, `B50`, `B51` feature pitch in other words, `B53`, `B54`, `B57`, `B58`, `B60`, `B61` hype/filler variants (BANNED_PHRASE), `B75`, `B76` multiple asks, `B77` to `B79` send language in opener/gatekeeper, `B81` "you look after the product", `B89` gatekeeper "feedback tooling", `B100` quotes around the opener, `B113` Cyrillic, `B115` NBSP, `B119` Cyrillic event word.
Four of these raised only a *warning* (`B19`, `B28`, `B76`, `B115`: OPENER_MULTIPLE_QUESTIONS).
Emails (12): `E-B07` two asks with one `?`, `B10` growth claim, `B11` invented voicemail, `B12` to `B14` send language ("sent you", "sending you", "send over"), `B22` Pendo results without a number, `B31` Series C, `B34` "circling back", `B35` "Congrats on the promotion" beside a different trigger, `B37` bullets, `B40` "shipped".

### False positives on hand-written good blocks (8)
`O-G15` "3 Nov" (month abbreviation as a name), `G17` "14 days ago" (derived number, NUMBER_UNSUPPORTED), `G18` "Cold-ish call" (hyphenated starter), `G20` "Kestrel's hiring ..." (event word from a fact, TRIGGER_MISMATCH), `G21` "Do you own that area?" (ROLE_ASSUMED on a *question*; the pattern `you own` cannot tell a question from an assumption); emails `E-G02` ("Rounds like that..." starter), `E-G07`, `E-G09` (TRIGGER_MISMATCH on a fact word).

## 4. False positives on normal ANZ business English (84 natural openers, all correct and sourced)

42 flagged (**50%**): 31 from ordinary sentence starters and 3 from the abbreviation CX (`PROPER_NOUN_UNSUPPORTED`, the list in B2), 3 from place/state names not in the packet
(*Melbourne, Sydney, Auckland, WA*; arguably harmless claims about Alex's calling, but unverifiable), 5 `TRIGGER_MISMATCH` from words that are in a packet *fact* (H1; one of them also has a starter hit). Hyphenated/alphanumeric names, `CX`, `AI`, `Q4`, "2 weeks", "Sept"/"Nov"
and "5 million" vs "A$5 million" were probed separately (Section 2, B1/B2/H4): every one is flagged. What passes: possessives of a name that is in the packet (`Tidewater's`, `O'Brien's`), months written in full, ISO and "3 November" dates, "APAC / ANZ / Australia(n)".
Estimate: a legitimate opener is rejected **about 1 time in 2** with the shipped lists, **about 1 in 12** with the prototype. Real output will resemble the skill's examples more than my set (UNVERIFIED), so treat 50% as the pessimistic bound.

## 5. Packet reconciliation with 2c (`src/callsheet.py` @ `16781aa`)

Built real packets for all 8 accounts of 2c's synthetic world and ran `ResearchPacket.to_dict()` through `normalize_packet` (saved as `docs/review_2d_real_packets.json`).

| 2c field (real) | textcheck reads | Mismatch and effect | Change (side) |
|---|---|---|---|
| `ambiguity_flags: list[str]` | `flags` | key name differs; **role rule OFF** (B3) | textcheck: read `ambiguity_flags` then `flags`, plus `signals[].ambiguous` |
| (no `trigger` key); `signals[]` best-first | strongest of `signals` by `strength` | works by luck: `PacketSignal` has no `strength`, `max()` of equal 0.0 keys returns the first. Multi-signal order **UNVERIFIED** (world has at most one) | callsheet: also emit `trigger` = `signals[0].to_dict()` or `None`; textcheck: prefer `trigger` when present, else `signals[0]`, never `max(strength)` |
| `signals[].kind` ("exec_move" ...), `label`, `evidence`, `date` (ISO), `source` (id), `source_label` | `kind`, `evidence`, `source`, `date` | **ok** for those; `source_label` unused | textcheck: add `source_label` as accepted alias |
| `available_facts[]: {text, source}` where source is the *label* ("ZoomInfo scoop 2026-11-22", "Salesforce account record") and **no date** | `{text, source, date}` | trigger id vs fact label for one scoop (B4); no date on facts, skill demands "(source: x, <date>)" so the model must guess (M5) | callsheet: `FactItem(text, source, source_label, date)`; signal-derived facts copy `ps.source`, `ps.source_label`, `ps.date`; SF facts `date=None`. textcheck: item matches if cited text contains *either* string; when `item.date` is empty the tail must have no date |
| `kind == "intent"`, `has_real_trigger = bool(sigs)` | any non-"none" kind is a trigger | intent treated as an event (M3) | callsheet: `has_real_trigger` false for intent-only or add `trigger_is_event`; textcheck: kind intent never licenses reaction/event words |
| `must_not_claim`: 6 *instruction sentences* ("Do not invent a trigger ...") | literal substring match | never matches; harmless but dead; skill line 40 relies on it | callsheet: keep as prompt text; add `must_not_phrases` (`case study`, `existing customer`) for the lint, or textcheck: ignore any entry starting "Do not" |
| `city`, `state`, `industry`, `tier`, `age_days`, `local_time` | `location` only | city/state only reach the corpus through a "Based in ..." fact; `age_days` unused, so "2 weeks ago" / "14 days ago" are false positives | textcheck: add `city`, `state`, `industry` to the corpus and allow `age_days`, `round(age_days/7)` as numbers (this is the principled fix for derived relative dates) |
| `contact_history`, `contact_history_source` + a "Previous contact: ..." *fact* | not read | history can become FACT 1 or 2 (a call-log line is not a research fact) and invented history is unchecked (M4) | callsheet: do not put history in `available_facts`; textcheck: history-claim patterns gated on it |
| `prospect_first_name` "Pat1" (fixture) | name regex splits at digits | B1 | 2c fixtures: digit-free names |
| `prospect_title` None / `prospect_first_name` None | handled (empty) | skill does not say what to write ("Hi there") | skill edit |
| `fact_count`, `facts_note`, `sdr_target`, `window`, `tier` | ignored | fine (internal) | none |

Minimal "no glue" target: 2c emits `trigger`, `ambiguity_flags`, and facts as `{text, source, source_label, date}`; textcheck reads exactly those names plus the fallbacks above. Then `normalize_packet(packet.to_dict())` needs no mapping table. Also add one contract test that builds a 2c packet (the integration world) and lints a skill-shaped block, in whichever repo state first contains both modules; today they live on separate branches so no test can cover it.

## 6. Skill quality (read as the Claude session that must follow them)

What is good: fixed block shape that matches `parse_block`; honest no-trigger and one-fact paths with worked examples; "do not call any connector"; the email skill says NEVER send in its first line.
Fix these (all `skills/opener/SKILL.md` unless stated):
1. **How to run the lint is not stated, and there is no CLI.** Add `python -m src.textcheck opener --packet packet.json --block out.txt` (and `email`) printing `format_violations` and exiting non-zero on errors; the session today would improvise `python -c`.
2. **No retry cap and no failure output.** Line 53 says "fix every error and regenerate". Add: "Regenerate at most 3 times. If errors remain, output exactly `NEEDS HUMAN: <codes>` and nothing else; never output a block that has an error." Same in the email skill (line 46). Ask the caller to log the codes.
3. **Output template is ambiguous**: the fenced example contains `[DRAFT STYLE...]  <- only while ...`. A model may print the brackets or the arrow. Show two separate templates (label on, label off) with no comments.
4. **Examples teach a voice.** All six examples open "Hi X, Alex from Pendo." and the model will clone them. Add "The examples show structure, not voice. Do not reuse their sentences. They are FAKE" at the top of the examples. Several lines are speculation or presumption: Example 6 "a bigger load on your engineering team" (inference, "your team"), email Example 1 "A change at that level often puts the customer app under a closer look", Example 3 "teams learn where members get stuck", Example 2 "I'm calling engineering managers at software companies in Perth" (a claim about Alex's campaign). The email rule "one sentence on why that matters for someone in this kind of role" *invites unverifiable generalisations*; restate as a question or drop it.
5. **`flags` vs `ambiguity_flags`** (line 43) and "Each carries a source and a date" (line 28): wrong for the real packet (Section 5). Say: cite `source_label` for facts and the `source` id for the TRIGGER, date only if the packet has one.
6. **Relative dates** (line 49) "true on the packet date": the packet date is not named. Use `age_days` ("3 days ago") or absolute dates only.
7. **Missing cases**: no contact first name or title ("Hi there"; do not guess a gender or title); ambiguous role in the *email* skill (rule exists, example does not); intent-only trigger (say "no event"); `contact_history` must not be used as a fact or implied ("we spoke").
8. **Hard rules**: the opener skill should say in one line: "This skill returns text only. Never write to Nooks, Gmail or Salesforce, never send anything." (the email skill's create_draft step is correctly gated on `--live`.)
9. Do NOT add voice content for Alex: the `Voice notes` TODOs are correct and nothing in either skill claims his taste. The `DRAFT STYLE` label mechanism is correct end to end.

## 7. voice.yaml

- **INFERRED hype list** (`cutting-edge, best-in-class, world-class, unlock, supercharge, disrupt, transformative`): sensible but too literal (H5). Keep, add stems and unhyphenated forms.
- **INFERRED `pendo_feature_words`**: includes ordinary words (guide, survey, funnel, analytics, nps) that fire on the account's own business (M1); missing real feature wording (recordings, checklists, onboarding flows, drop-off). Alex to confirm.
- **INFERRED gatekeeper `pitch_words`/`role_cues`**: `help, free, our, we, software, save` are normal receptionist English (M2). `role_cues` are fine; a name-only ask ("Could you put me through to Priya?") correctly fails `GATEKEEPER_NO_ROLE` per the skill.
- `sentence_start_words` (B2) is overfitted to the skill examples (curious, saw, noticed, cold ...) and should be replaced by the rule change, not extended.
- `trigger_event_words` lacks `promotion`, `shipped`, `went live`, `expanding`, `rolled out`.
- `calibrated: false`: **works**: missing label is an error, a stale label under `true` is a warning; blocks with the label pass everything else. A non-bool `calibrated` is not rejected (B5).
- `alex_examples`: filling `disliked_openers` with a FAKE example warned (`SIMILAR_TO_DISLIKED`) on the identical line, a template line and 2 of 3 paraphrases; `liked_openers` and `proud_emails` are not read by the lint (skills only), which is fine. Junk entries crash it (M6).

## 8. Prototype (how the 89% / 13% was measured) and its limits

`tests/test_review_2d_scratch.py::proto_lint` wraps the worker's functions without editing them: Unicode fold plus `UNICODE_SUSPECT`; the B2 sentence-start rule; number words; event words from any packet item (4-letter stem); send language on opener/gatekeeper; multi-ask as error; extra hype, claim, feature, role and send patterns; gatekeeper words removed; quote/bullet/fake-thread checks. Reproduce: `python -m pytest tests/test_review_2d_scratch.py -q -s -k "report"` prints both tables and every miss / false positive.
Caveats: it is tuned by looking at the same corpus it is scored on, so the 89% / 13% are optimistic; some remaining false positives are artefacts of the quick ask-cue regex (`Mind if I ask ...?` counts twice); it allow-lists `CEO` and so misses `O-B09`; the two remaining classes it cannot touch are paraphrase meaning flips in the *spoken* opener (`O-B06`, `B08`) and unlisted-wording hype or feature pitches.

## 9. Hard-rule alignment: PASS

- `textcheck.py` imports no connector and does no I/O except reading the YAML; it cannot send or create anything. Verified by AST test.
- Opener skill: "Do not call any connector" (line 17). Email skill: "NEVER send", only Gmail `create_draft`, only with `--live`; forbids `send_message`, `reply`, `forward`, `sendTaskEmail`. Neither mentions `createEmail`, Salesforce or `createAccountNote`: add them to the forbidden list for completeness.
- No fabricated data: all fixtures and my corpus are FAKE; no real names; no Alex voice written. The lint enforces "no invented trigger" for the **no-trigger** path well (4 of 6 fabricated-trigger attempts caught; the two misses use no event word) and for other-company triggers (4/4).

## 10. Fixes ranked by value (what to do next)

1. **B2 sentence-start rule + abbreviations + hyphen/alnum tokenisation (B1)**: removes about 40 of the 50 false positives; without it the workflow is unusable. (code, ~40 lines)
2. **Packet contract (B3, B4, M3, M5)**: `trigger` / `ambiguity_flags` / `source_label` / `date` agreed once between 2c and textcheck, plus one cross-module contract test. (2c small change + textcheck ~20 lines)
3. **Config validation (B5)** so a bad or empty `voice.yaml` raises instead of disabling checks. (code, ~25 lines)
4. **Unicode normalisation + send language everywhere + multi-ask as error (B6, H2)**. (code, ~30 lines)
5. **Event words from any packet item (H1)** and **age_days-based relative dates**. (code)
6. **Config-only:** FACT overlap 0.9, keep single-letter tokens, extend hype / claim / feature / role / event-word lists, trim gatekeeper pitch words (H3, H5, M1, M2). (yaml + 3 lines)
7. **Skill edits** (retry cap + `NEEDS HUMAN`, how to run, templates, "structure not voice", missing cases) and a tiny CLI `python -m src.textcheck`. (docs + ~40 lines)
8. **Number words** (H4) after 5 (so `age_days` can license "two weeks").
9. Say plainly in docs/voice_guide.md that the lint cannot catch a meaning-changing paraphrase in the spoken opener; Alex's skim (or a verifier pass) is the control.

## 11. UNVERIFIED / not done

- Python 3.11 specifically (only 3.13 available). Real LLM output (all blocks here are hand-written). Alex's voice and taste (examples not provided). Multi-signal ordering in 2c packets. That 25 words is about 8 seconds. Gmail/Nooks behaviour of the DRAFT STYLE label line (how the label is stripped before it reaches a Nooks field is a 2c/3a question).
