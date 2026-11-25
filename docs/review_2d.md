# Independent review: task 2d (textcheck, opener / email-draft skills, voice.yaml)

Reviewer session: `session_01QShr1H7ghpKeoYqfRwTjBe`. Reviewed commit `7b98a97` (branch `phase2/2d-opener-lint`) against the 2c packet on
`origin/phase2/2c-callsheet` @ `16781aa`. Fully offline, read-only, FAKE data only. No connector was called. The worker's files
were not edited. Added: this file, `docs/review_2d_labels.json` (committed **before** the lint was run on it),
`docs/review_2d_real_packets.json` (real 2c `ResearchPacket.to_dict()` output from the synthetic world), `tests/test_review_2d_scratch.py`.

## Verdict (round 1): PASS WITH FIXES. **Round 2 verdict is at the end of this file: FINAL PASS** (blockers fixed, held-out measured)

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

## 3. Adversarial recall (labels in `docs/review_2d_labels.json`, committed in `918d215` before the lint was run)

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


---

# Round 2: re-review of `phase2/2d-opener-lint` @ `3439150` (merged into `phase2/2d-review`)

Offline, read-only, FAKE data. No connector call. Worker files not edited. The coordinator warned that the worker tuned against my round-1 set, so the
headline here is a **new held-out set** written after the fixes, against **real `ResearchPacket` objects** built by `src/callsheet.py`.

## R0. Verdict: **FINAL PASS**

No failing item. Every round-1 blocker (B1 to B6) and high finding (H1 to H4) is fixed with evidence below; the CLI works as the skill says; the
worker's two test-support edits are acceptable (one should be scoped). The lint is **good enough to gate openers in a workflow where Alex reads every one**,
provided the remaining control is a deliberate skim with the packet beside the text (R4). It is **not** a substitute for that skim: about 1 in 6 content errors still gets through.

| Headline (held-out 2, `docs/review_2d_heldout2.json`, 210 cases, labels committed in `108fd01` before the run) | Result |
|---|---|
| Existing suite | **2157 passed**, plain and with `-W error` (worker's number confirmed; 2185 passed + 1 skipped + 10 xfailed with my scratch file) |
| Determinism | identical results over 3 `PYTHONHASHSEED` values on all 210 cases |
| **Recall on 148 new bad blocks** (any *error*) | **124 / 148 = 83.8%** (95% interval about 78% to 89%). 84.4% if H-B145 is dropped: my label was wrong, that email is 76 words (limit 79) |
| Recall for the intended reason | 117 / 148 = 79.1% raw; ~83% once `UNSUPPORTED_CLAIM` is accepted as the right code for wrong relative dates (my label named a different code, the lint still errors) |
| **False positives on 62 new good blocks** | **0 / 62 = 0%** (95% upper bound about 5%) |
| False positives on 15 extra natural gatekeeper lines | 0 / 15 |
| Round-1 corpora re-run (worker tuned on these) | 161 / 161 bad caught; 2 / 32 good flagged (both "2 weeks"/"14 days" on fixtures that carry no `age_days`); natural openers 2 / 84 flagged (other-city names) |

**Reading the numbers honestly.** The worker's own fresh batches scored 84%, 74%, 60% before it tuned. My 84% sits at the top of that range. Two reasons it is not higher or lower than theirs:
(1) my set mixes mechanical errors (shape, label, `!`, emoji, links, phones, send language: 33 of the 148 and about 97% caught) with content errors; on **content errors only** recall is **about 82%**; (2) I wrote every error myself, with knowledge of what a lint might not catch, which makes the set harder than real, mostly non-adversarial LLM mistakes, but it also shares my blind spots. Treat **75% to 85% on content errors** as the planning number, not 100%, and do not quote the worker's 100% (it was tuned on the set).

## R1. Round-1 findings: FIXED / NOT FIXED (evidence)

| Finding | Status | Evidence |
|---|---|---|
| **B1** hyphen / alnum company names (Smith-Jones, Zip2Pay, 3M, Co-op, O'Brien & Sons) | **FIXED** | verbatim FACT/TRIGGER with each name: 0 violations (probe f); `test_F01`, `test_F16` now pass |
| **B2** `sentence_start_words` | **FIXED** | key removed from `voice.yaml` and from `SCHEMA`; natural ANZ openers 42 / 84 flagged to **2 / 84**; "Honestly, Maybe, Perhaps, Morning, CX, WA, Nov" all pass (`test_F12` x7). Remaining benign hits: other-city names (Melbourne for a Perth account) and `Q4` |
| **B3** `ambiguity_flags` ignored | **FIXED** | `ambiguity_flags` and `signals[].ambiguous` each switch the role rule on (probe a); `test_F02`; held-out role cases 5 / 6 caught |
| **B4** source label vs id, dates | **FIXED** | either string accepted for FACT and TRIGGER (H-G62 passes with label only on the TRIGGER line); date on an undated fact `FACT_DATE_INVENTED` (H-B016), wrong trigger date `TRIGGER_DATE_INVENTED` (H-B015); `test_F03`, `test_F13` |
| **B5** config fails open | **FIXED** | `load_voice_config` validates: typo'd key `banned_phrase`, `calibrated: "false"` string, missing file, bad regex all exit **2** from the CLI (probe); junk `alex_examples` rejected at validation (`test_F14`). `check_*` also raises `VoiceConfigError` on a bad dict |
| **B6a** unicode | **FIXED** | zero-width, Cyrillic lookalikes and non-Latin letters now an error (`UNICODE_SUSPECT`); NBSP folded so `quick<NBSP>call` is `BANNED_PHRASE` (probe c); fullwidth digits/`！` normalised |
| **B6b** send language | **FIXED** | now applied to opener, gatekeeper, FACT and email; "I've sent you a calendar invite", "I'll send over", "sending you" all error (H-B094, H-B126). One gap left: "send them a free report" in a gatekeeper line (N7c) |
| **H1** event word from a packet fact | **FIXED** | `test_F08`; held-out good emails H-G54 ("You are also hiring...") pass |
| **H2** one ask is only a warning | **FIXED** | now an error; a single `?` with "can I send you a calendar invite" errors (probe d); `test_F09`. "Right?" tags exempt per the skill |
| **H3** round letters / meaning-changing paraphrase | **PARTLY FIXED** | funding round, currency, magnitude and amount are now verbatim-checked: held-out wrong round / amount / currency 22 of 22 caught. A meaning change in other words is still not caught (R3, 7 of 15 missed). The skill and guide say so plainly |
| **H4** number words | **FIXED, one hole** | "six hundred", "twenty million" caught (`test_F10`). `two days ago` passes for a 9-day-old signal because the derived numbers mix days with week counts (N8) |
| **H5** hype stems, **M1 to M7** | **FIXED** | all round-1 misses now caught; `guide` is a soft word; gatekeeper receptionist English 0 / 15 false positives; intent is never a trigger (5 of 5 held-out intent cases caught); history claims 3 / 3; invented citation dates caught; sign-off, bullets, "Re:" and quotes around a line are errors |
| **Skills** | **FIXED** | CLI command with exit codes; **retry rule** (3 tries then exactly `NEEDS HUMAN: <codes>`); two separate templates (label on / off); examples headed "FAKE ... Do not copy the wording: structure only"; `ambiguity_flags`, null first name ("Hi there"), intent, history-is-not-a-fact and "text only, never writes" are all stated; email skill forbids `createEmail` and note tools |
| **voice.yaml** | **FIXED** | INFERRED lists are now *soft* (`OPENER_PENDO_FEATURE_SOFT`, `GATEKEEPER_PITCH_SOFT` warnings) with hard phrases separate; `must_not_phrases` added |

## R2. Held-out measurement (details)

World: 14 FAKE accounts (new names, 8 trigger kinds, ages 2 to 60 days, ambiguous roles, stale and intent-only signals, missing first name, one-fact and zero-fact packets, two-signal packet, no-state NZ), built through `cs.build_sheet` / `build_packet` (`docs/review_2d_heldout2_packets.json`).
Cases: 171 openers (including 12 gatekeeper-line variants and 25 gatekeeper / shape cases) and 39 emails; 62 good, 148 bad. Errors written the way an LLM makes them: wrong round / amount / currency, invented adjectives, wrong tense, plausible employee counts, "their new CPO", mixing two accounts, copied example wording, a LinkedIn post, "congrats on the funding" for an exec-move packet, a competitor, a wrong city, "last week" for 14 days, CTA stuffing, a pitch in the gatekeeper, politeness filler, "quick", send language, a missing label, markdown / quotes / fences.

### Recall by error family (families assigned by a regex on my category tags; counts approximate)

| Family | n | caught | recall |
|---|---|---|---|
| Wrong number, currency, round, count | 22 | 22 | 100% |
| Wrong place | 6 | 6 | 100% |
| Role assumed (ambiguity flag) | 3 | 3 | 100% |
| Citation / source / date lines | 4 | 4 | 100% |
| Surface: `!`, emoji, link, phone, email address | 5 | 5 | 100% |
| Email-only: history claim, send language, two asks | 5 | 5 | 100% |
| Block shape, label, markdown, quotes, length | 13 | 12 | 92% |
| Stale or intent signal used as news | 9 | 8 | 89% |
| Wrong person / title / name / pronoun | 14 | 11 | 79% |
| Pendo pitch, stats, hype, feature, sales-speak | 18 | 14 | 78% |
| Invented fact / descriptor / career / social post | 18 | 13 | 72% |
| Relative time wrong | 7 | 5 | 71% |
| Event / meaning changed (word swap, tense, event type) | 15 | 10 | 67% |
| Competitor named | 3 | 2 | 67% |
| Gatekeeper line (pitch, trigger, send) | 5 | 3 | 60% |

### Per-code table (code I expected; n = cases; "intended" = that code or one of its alternatives fired)

| Intended code (first listed) | n | raised any error | raised an intended code | missed |
|---|---|---|---|---|
| `UNSUPPORTED_CLAIM` | 21 | 9 | 8 | B008, B023, B029, B033, B036, B040, B059, B060, B064, B087, B095, B140 |
| `PROPER_NOUN_UNSUPPORTED` | 20 | 19 | 19 | B141 |
| `NUMBER_UNSUPPORTED` | 18 | 16 | 16 | B006, B093 |
| `INVENTED_TRIGGER` | 12 | 11 | 11 | B065 |
| `TRIGGER_MISMATCH` | 10 | 8 | 8 | B046, B061 |
| `BANNED_PHRASE` | 7 | 7 | 7 |  |
| `RELATIVE_TIME_UNVERIFIED` | 6 | 5 | 0 | B022 |
| `FACT_UNSUPPORTED` | 6 | 6 | 6 |  |
| `ROLE_ASSUMED` | 6 | 5 | 5 | B051 |
| `GATEKEEPER_PITCH` | 4 | 1 | 1 | B108, B110, B114 |
| `SHAPE_EXTRA` | 4 | 4 | 4 |  |
| `HISTORY_CLAIM` | 3 | 3 | 3 |  |
| `FACT_SOURCE_UNKNOWN` | 2 | 2 | 2 |  |
| `TRIGGER_LINE_CLAIMED` | 2 | 2 | 2 |  |
| `OPENER_PENDO_FEATURE` | 2 | 2 | 2 |  |
| `GATEKEEPER_NO_ROLE` | 2 | 2 | 2 |  |
| `DRAFT_STYLE_LABEL_MISSING` | 2 | 2 | 2 |  |
| `FACT_SOURCE_MISMATCH` | 1 | 1 | 1 |  |
| `TRIGGER_DATE_INVENTED` | 1 | 1 | 1 |  |
| `FACT_DATE_INVENTED` | 1 | 1 | 1 |  |
| `FACT_DUPLICATE` | 1 | 1 | 0 |  |
| `OPENER_MULTIPLE_QUESTIONS` | 1 | 1 | 1 |  |
| `EXCLAMATION` | 1 | 1 | 1 |  |
| `EMOJI` | 1 | 1 | 1 |  |
| `LINK` | 1 | 1 | 1 |  |
| `PHONE_NUMBER` | 1 | 1 | 1 |  |
| `OPENER_TOO_LONG` | 1 | 1 | 1 |  |
| `GATEKEEPER_TOO_LONG` | 1 | 1 | 1 |  |
| `SHAPE_MISSING` | 1 | 1 | 1 |  |
| `TRIGGER_SOURCE_MISMATCH` | 1 | 1 | 1 |  |
| `EMAIL_SEND_LANGUAGE` | 1 | 1 | 1 |  |
| `EMAIL_MULTIPLE_ASKS` | 1 | 1 | 1 |  |
| `SUBJECT_FAKE_THREAD` | 1 | 1 | 1 |  |
| `EMAIL_LINE_COUNT` | 1 | 1 | 1 |  |
| `EMAIL_ADDRESS` | 1 | 1 | 1 |  |
| `EMAIL_BULLETS` | 1 | 1 | 1 |  |
| `EMAIL_SIGNOFF` | 1 | 1 | 1 |  |
| `EMAIL_TOO_LONG` | 1 | 0 | 0 | B145 |

(`UNSUPPORTED_CLAIM` row: 12 of the 21 were missed, 9 caught; `RELATIVE_TIME_UNVERIFIED` row: the lint raises `UNSUPPORTED_CLAIM` as an error when the packet has `age_days`, which is the right behaviour, so I count those 5 as caught.)

### Top miss patterns (24 misses, 23 after my one mislabel)

1. **Meaning changes in plain words in the spoken opener** (7): "completed the acquisition" / "acquired" for "agreed to acquire", "merger", app to "web portal", "small" to "big retailers", "hired a Head of Product" (the contact's own title), "Congrats on becoming Chief Experience Officer" (the hire is someone else). The lint matches names, numbers, event words and round letters, not sentences. This is the known, documented limit.
2. **Invented descriptors and career claims in lowercase prose** (5): "renewable energy company", "one of Australia's biggest banks", "patient app", "previously led digital at a major miner", "Congrats on the CTO role". No name, number or event word to trip on.
3. **A stale signal restated as news** (1): gumnut's 60-day-old "is migrating its ecommerce platform" passes because "migrating" is not an event word and the signal is correctly marked stale. The evidence of a stale or intent signal should be treated as forbidden content, not just its event word.
4. **Sales-speak and competitors** (4): "the leading digital adoption platform", "Pendo helps teams see...", "Gainsight users often switch" (a sentence-initial competitor name is skipped by the new name rule; mid-sentence is caught), email versions of the same.
5. **Gatekeeper line** (3): "product analytics" is only a *soft* warning, the trigger person's name repeated, "send them a free report".
6. **CTA with a duration** (1): "twenty minutes on your calendar" (durations in minutes are exempt from the number-word rule).
7. **Pronouns** (1): "his new role / alongside him" for a named person of unknown gender.
8. **Relative time** (2): "two days ago" for 9 days; "last week" for 14 days (debatable label).

### False positives: none on good blocks; known benign hits elsewhere
`Q4` is read as a name; a different Australian city in "I'm ringing Melbourne teams" (Perth account) is flagged as unverifiable; "2 minutes" is fine, "2 weeks" with no `age_days` in the packet is flagged.

## R3. New findings (all LOW / MEDIUM; none blocks the pass). Each has an xfail test in `tests/test_review_2d_scratch.py`

| ID | Severity | Finding | Fix |
|---|---|---|---|
| N1 | MEDIUM, by design | Meaning-changing plain-words paraphrase in the opener passes | Not lintable. Add `completed, finalised, closed, signed, went live, shipped` to `unsupported_unless_in_packet`; add "agreed to X" must not become "X-ed"; rest is the skim / verifier |
| N2 | MEDIUM | Stale / intent evidence restated as news passes (`test_N2`) | When `has_real_trigger` is false, any opener sentence sharing 2 or more content words with a stale or intent signal's evidence is an error |
| N3 | MEDIUM | Lowercase invented descriptors, careers | Add `previously, formerly, joined from, used to, one of the (biggest, largest, leading), market leader` to `unsupported_unless_in_packet`; `test_N9` stays open |
| N4 | LOW | Gendered pronouns for a named person (`test_N4`) | error on `he, she, his, her, him` in opener / email unless in the packet |
| N5 | MEDIUM | "Pendo is the leading ...", "Pendo helps / gives / lets ...", competitor at sentence start (`test_N5a`, `test_N5b`) | patterns `\bpendo (is|helps|gives|lets|makes)\b`, `\b(leading|number one|best) .* platform\b`; a `competitors` list (Gainsight, WalkMe, Whatfix, Appcues, Userpilot, Amplitude, Mixpanel, Heap, FullStory, Hotjar, Qualtrics, Medallia) checked everywhere, including sentence start |
| N6 | LOW | "minutes on your calendar" (`test_N6`) | ban `calendar, book a, grab (some )?time` in the opener |
| N7 | MEDIUM | Gatekeeper: "product analytics" is only soft; trigger person's name; "send them a free report" (`test_N7a-c`) | make `analytics` and the Pendo feature phrases hard in the gatekeeper; reject any trigger-evidence name or content word; widen send pattern to `send (them\|him\|her\|over)` |
| N8 | LOW | `_derived_numbers` (:542) mixes days and week counts, so "two days ago" fits a 9-day age (`test_N8`) | keep day numbers and week numbers separate and require the unit next to the number |
| N9 | LOW | `Q4`, other Australian city flagged | allow `Q[1-4]`; add a short list of ANZ cities to `allowed_proper_nouns` or accept the cost |
| N10 | LOW | 2c does not emit `must_not_phrases`; the six `must_not_claim` sentences have no quoted phrases so they match nothing (the built-in claim patterns do the work) | 2c: emit `must_not_phrases: ["existing customer", "case study"]`, or drop the field; keep `trigger_kind` (emitted, unused: fine) |
| N11 | LOW | Fixture `tests/fixtures/text_packets.json` is still the old packet dialect (`trigger`, `flags`, `location`) | acceptable: the legacy fallbacks are tested and the real-packet contract is covered by `tests/test_textcheck_review.py` (builds packets with `cs.build_sheet`); label the file "legacy shape" |

## R4. Is it good enough to gate openers when Alex reads every one? Yes. What the remaining control must be

**Verdict: yes, with the skim as a designed control, not an afterthought.**
- The lint reliably removes the errors that are hardest for a human to spot while skimming: wrong numbers, currencies, funding rounds, places, names, invented triggers, stale and intent signals presented as news, role assumptions, unsourced or mis-dated facts, history claims, send language, unicode tricks, wrong shape (95% to 100% in those families, 0 false positives on 62 good blocks, 2 of 84 on natural openers). A rejected block costs one regenerate; the 3-try then `NEEDS HUMAN` rule makes the failure visible.
- It misses roughly **1 in 6 content errors**, and those misses are the ones a human also finds hardest: a plausible sentence that quietly changes what the packet says ("completed" for "agreed to acquire", "a major miner", "renewable").
- **Remaining control (recommended, cheap):** (1) Alex's skim, shown **side by side**: the sheet prints the opener next to the packet's TRIGGER evidence and the two FACT lines, so he compares sentences rather than recalling the news; every opener sentence that contains a verb or adjective not found in the packet is **underlined / listed** ("not in packet: completed, renewable"), which is a mechanical diff the lint can already compute (a `NOT_IN_PACKET_WORDS` warning list); (2) optional **verifier pass**: a second, separate model call given only the packet and the opener, asked one closed question ("List any claim in the OPENER not stated in the packet; reply NONE or the list"). Its output is a *warning* in the log, never a gate, because it can be wrong in both directions; (3) keep `calibrated: false` and the DRAFT STYLE label until Alex has supplied his examples; (4) never skip the dry-run default: nothing is written without `--live`.
- Plan on a real miss rate of about 1 in 6 *of the content errors that occur*, and most LLM errors are not adversarial, so in practice a few percent of openers will carry a wrong detail that the lint cannot see. This is acceptable only because a person reads each line before it is used.

## R5. Packet reconciliation with the real 2c packet (re-check): no blocking mismatch left
Checked against `src/callsheet.py` on this branch (merged from 2c, `ResearchPacket.to_dict()`): `ambiguity_flags`, `signals[].{kind,evidence,source,source_label,date,age_days,status,ambiguous}`, `has_real_trigger`, `available_facts[].{text,source}`, `contact_history`, `city`, `state`, `industry`, `employees`, `prospect_first_name` (null handled), `prospect_title` are all read. A stale signal with `has_real_trigger: false` is treated as no trigger. The call-log "Previous contact" fact is filtered out of citable facts. Both source strings are accepted. Dates are ISO. Packets with one fact and zero facts lint correctly (`none verified`). Left over: `must_not_phrases` is read but never emitted (N10); `trigger_kind` is emitted but unused; fixture dialect (N11); `age_days` day / week mixing (N8). Nothing needs a mapping table.

## R6. CLI check: works as the skill says
`python -m src.textcheck opener|email --packet P --text FILE|- [--voice V]`: good block via stdin exit **0** `OK: no violations`; bad block exit **1** with `ERROR CODE: message` lines; missing packet file, invalid JSON, non-UTF-8 text, missing voice file, typo'd voice key, `calibrated: "false"` all exit **2** with `ERROR cannot run the lint`; bad kind exits 2 (argparse); a JSON list as packet exits 1 (`PACKET_INVALID`); empty text exits 1. Email mode parses `SUBJECT:` / `BODY:`. One real-world catch: it must be run from the repo root (`python -m src.textcheck` from another directory fails with `No module named src`); the skills say "run" without saying "from the repo root", add those three words (or `cd` into it in the scheduled-run prompt).

## R7. The worker's test-support edits: acceptable
- `tests/test_integration_pipeline.py`: the import allowlist in `test_l_no_network_or_process_imports` gains `argparse` and `sys`. The forbidden-import list (requests, urllib, socket, subprocess, anthropic, mcp ...) is untouched and the forbidden-call-name scan still passes, so no network or process access was opened. But the allowlist is shared by **every** `src/*.py`: scope it to `textcheck.py` only (`{"textcheck.py": {"argparse", "sys"}}`) so another module cannot start using `sys` unnoticed. LOW.
- `tests/fixtures/text_packets.json`: still FAKE (invented names, `.example.com`, no real email or phone found by regex); legacy packet dialect (N11). OK.
- Worker's counts hold: 2157 passed plain and `-W error`. The worker's labelled-corpus gate in the tests runs on my round-1 set, so it will pass by construction; it is a regression gate, not evidence of generalisation.

## R8. What I could not verify
Real LLM output (every block is hand-written), Alex's voice (no examples), Python 3.11 specifically (3.13 only), multi-signal ordering beyond the one two-signal packet, the true 8-second cost of 25 words, and how the DRAFT STYLE line is stripped before it reaches a Nooks field (a 2c / 3a question). The held-out set shares my blind spots; a second independent author would tighten the estimate.

## R9. Files added in this round
`docs/review_2d_heldout2.json` (labels; committed before the run), `docs/review_2d_heldout2_packets.json` (real 2c packets from a FAKE world), `tests/test_review_2d_scratch.py` (updated: round-1 findings are now regression tests, the held-out report test, 10 xfail tests for N2 to N9). Reproduce: `python -m pytest tests/test_review_2d_scratch.py -q -s -k heldout2`.
