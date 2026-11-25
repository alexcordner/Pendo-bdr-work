# Voice guide and text lint (task 2d, round 2)

Offline, fake data only. The structure is final; **Alex's real taste is not in it yet** (see "What is missing").
Round 2 answers `docs/review_2d.md` (blockers B1 to B6). That review and its labelled corpus
(`docs/review_2d_labels.json`) are kept in the repo and the corpus is a measured regression gate in the tests.

## Pieces
| File | Role |
|---|---|
| `config/voice.yaml` | Machine-readable rules. **Validated at load**: every key required, unknown keys rejected, real types |
| `src/textcheck.py` | The lint: `check_opener_block`, `check_email_draft`, `format_violations`, `has_errors`, `normalize_packet`, `parse_block`, `parse_email_block`, `load_voice_config`, `validate_voice_config`, CLI `main` |
| `skills/opener/SKILL.md`, `skills/email-draft/SKILL.md` | Instructions for the Claude session that writes the text (shape, rules, how to run the lint, retry rule) |
| `tests/test_textcheck.py`, `tests/test_textcheck_review.py` | Unit tests, the labelled-corpus gate, fresh adversarial batches, real 2c packets, CLI |
| `tests/fixtures/text_packets.json`, `text_good_blocks.json` | FAKE packets and good blocks (old packet shape) |

The LLM writes the text. Python only checks it (CLAUDE.md design rule). Nothing here can send or write anything.

## API and CLI
```python
from src.textcheck import load_voice_config, check_opener_block, check_email_draft, format_violations, has_errors
cfg = load_voice_config("config/voice.yaml")          # raises VoiceConfigError if the file is wrong
violations = check_opener_block(block_text, packet_dict, cfg)      # list[Violation(code, message, severity)]
violations = check_email_draft(subject, body, packet_dict, cfg)
print(format_violations(violations)); has_errors(violations)
```
```text
python -m src.textcheck opener --packet packet.json --text block.txt [--voice config/voice.yaml]
python -m src.textcheck email  --packet packet.json --text draft.txt [--voice config/voice.yaml]
```
`--text -` reads stdin. The email text file is `SUBJECT: ...` / `BODY:` / lines (a leading DRAFT STYLE line is fine).
Exit code **0** no errors, **1** errors (printed one per line, errors first), **2** could not run (bad file or bad
config). The check functions **raise `VoiceConfigError`** when `voice_cfg` is `None`, `{}`, has a missing or unknown key,
has a wrong type (`calibrated: "false"` is rejected), or has an invalid regex: they never silently weaken the rules.

## Calibration flag
`calibrated: false` in `config/voice.yaml` until Alex has supplied his examples and approved a tuned run (task 2e).
While false, opener blocks and email bodies must start with the line `DRAFT STYLE: not yet calibrated to Alex`
(`DRAFT_STYLE_LABEL_MISSING` otherwise). Once true, a leftover label is only a warning.

## Packet contract with task 2c (no glue)
`normalize_packet(ResearchPacket.to_dict())` works as is. Everything the lint reads:

| Lint reads | From the real 2c packet | Decision |
|---|---|---|
| account name | `account_name` (also `name`, `account.name`) | |
| allowed words and numbers | `website`, `city`, `state`, `industry`, `employees`, `location`, first name, title | city and state are in the corpus, so a place in the packet is never an unsupported name |
| first name | `prospect_first_name` (may be null) | null means "Hi there": a name in the text is then an unsupported name |
| title | `prospect_title` (may be null) | only used by the ambiguous-role rule |
| trigger | `signals[]` best first, each with `kind`, `evidence`, `source` (id), `source_label`, `date`, `age_days`, `status`; `has_real_trigger` | first signal whose `kind` is not none or intent and whose `status` is ok or undated. `has_real_trigger: false` forces no trigger. A top-level `trigger` object (older shape) wins if present. **Intent is never a trigger.** Stale and future signals are not triggers |
| facts | `available_facts[]` = `{text, source}` (+ optional `source_label`, `date`) | A fact with no source is unusable. A fact whose text starts "Previous contact" or whose source is `contact_history_source` is **never citable** (the call log is not research) |
| citation | FACT `(source: ...)` | must contain the fact's `source` OR `source_label`. TRIGGER `(source: ...)` must contain the signal's `source` id or `source_label` |
| citation date | fact `date`, signal `date` | **a dated item must carry its date; a date-less item must carry none** (`FACT_DATE_INVENTED` / `TRIGGER_DATE_INVENTED`). A label that embeds a date ("ZoomInfo scoop 2026-11-22") counts as carrying it |
| relative dates | `age_days` (top level, signals, or trigger) | licenses "N days ago", about N/7 weeks ("two weeks ago"), and phrases such as "last week" when the age is in range. No `age_days`: a relative phrase is a warning (`RELATIVE_TIME_UNVERIFIED`), a bare number is an error |
| ambiguous role | `ambiguity_flags[]` (also `flags[]` with the words "ambig" and "role"), or any `signals[].ambiguous == true` | turns the role rule ON for real packets |
| call history | `contact_history`, `contact_history_source` | never usable in the opener; email claims of earlier contact need the history to say so |
| must-not-claim | `must_not_phrases[]` (literal), `must_not_claim[]` | **Instruction sentences ("Do not claim ...") are prompt text and are not matched** (they never matched anything). Quoted phrases inside them, and short literal entries, are matched. |
| contact details | `prospect_email`, `prospect_phone`, `email`, `phone` | if present they must never appear in the text (2c never emits them) |

**What the lint needs from 2c (small, optional):** (1) emit a separate `must_not_phrases` list of literal phrases
(for example `case study`, `existing customer`) if 2c wants substring enforcement; the current instruction sentences
are passed to the model but cannot be enforced by string match. (2) Stop adding the "Previous contact" line to
`available_facts` (the lint already ignores it, but the model sees it). (3) Fixture names with digits (`Pat1`) work now
(tested), so no change is required. The old test-only keys (`trigger`, `flags`) still work.

## How the lint decides
- **Normalisation first:** NFKC, zero-width and other format characters removed, NBSP and spaces unified, Cyrillic and
  Greek lookalikes folded to Latin, fullwidth forms folded, smart quotes made plain. Invisible characters, lookalikes
  and any other non-Latin letters raise `UNICODE_SUSPECT` (NBSP alone is silently fixed). All checks then run on the
  clean text.
- **Shape:** the five lines in order, nothing else (optional label first). Extra lines, markdown, commentary or
  quotes around a line are errors.
- **Facts:** each ends with `(source: ...)`. Its content words (single letters count: "Series B" is not "Series C")
  must be at least `fact_overlap_min` (0.9) covered by ONE packet fact, else `FACT_UNSUPPORTED` "possibly invented".
  The citation must name that item's source and carry exactly its date. FACT 1 and FACT 2 cannot be the same fact.
- **Numbers:** every digit number, number word ("six hundred", "twenty million"), amount with currency and magnitude
  ("NZ$18 million" is not "A$18 million", not "NZ$18 billion") and date must be in the packet, or derived from
  `age_days`. "one", "two minutes" and similar speech are exempt.
- **Names:** a name copied verbatim from the packet always passes (hyphens, digits, apostrophes, ampersands:
  `Smith-Jones`, `Zip2Pay`, `Co-op`, `3M`, `O'Brien & Co`). A capitalised word in mid-sentence is always checked. A
  **sentence-initial** word is checked only when it looks like a company (followed by a capitalised suffix such as
  Group, Bank, Pty, or a possessive). Common acronyms (CX, AI, WA, NSW, SaaS ...) and months are allowed. Role
  acronyms (CEO, CTO, CIO ...) need the packet to have them or their long form. A job title in the opener or email
  (`chief executive`, `head of marketing`) must be in the packet.
- **Trigger:** with no real trigger, no reaction (saw, noticed, congrats, heard, "I hear") and no event word that is
  not a restated packet fact; the TRIGGER line is the exact none line. With a trigger, every event word used must be in
  the trigger or a fact, directly or as a listed synonym (`joined` for `appointed`); opposites (ended, cancelled,
  resigned, shut, went public, replaced) are errors unless the packet says them. Funding-round words (Series A to E,
  seed, IPO, went public) must be in the packet verbatim.
- **Claims:** "we help/make/improve ...", "companies like yours", "customers such as", result words ("see higher
  adoption"), growth claims, product-quality claims about them (crashes, poor reviews) unless the packet says it,
  claims about the contact's career, and "X said/tweeted" are errors.
- **One ask:** opener: one question mark (or one cue such as "can I" in a sentence without one); a second question, or
  "Who owns it, and can I send an invite?", is `OPENER_MULTIPLE_QUESTIONS`, an **error**. A short ", right?" tag does not
  count. Email: each question mark is an ask, a cue ("let me know", "can you", "or should I") in a sentence with no
  question mark is an ask, and a cue that follows a question in the same sentence is a second ask. Heuristic by design;
  one ask is `EMAIL_MULTIPLE_ASKS` when it fails.
- **Send language** ("I've sent", "sending you", "send over", "attached", "calendar invite") is an error in the opener,
  gatekeeper and email alike (code `EMAIL_SEND_LANGUAGE`).
- **Gatekeeper:** under 15 words, asks for a role or person, and no clear pitch (we help, our platform, a demo, a topic
  such as "about analytics"). Everyday words (help, free, software, our) are NOT pitch. A soft INFERRED word list gives
  warnings only.
- **Pendo features** in the opener: clear phrases (in-app guides, session replay, heatmaps, product analytics,
  checklists) are errors; one ambiguous word (analytics, survey, funnel) is a warning. A phrase that appears in the
  packet (the account's own business) is exempt. Both lists are INFERRED.

## Lint codes
| Code | Meaning |
|---|---|
| `PACKET_INVALID` | the packet is not a mapping |
| `TEXT_INVALID` | the text to check is not a string |
| `UNICODE_SUSPECT` | zero-width character, non-Latin lookalike letter or other text that could dodge the checks |
| `SHAPE_MISSING` | a required line (OPENER / FACT 1 / FACT 2 / GATEKEEPER / TRIGGER) is missing |
| `SHAPE_ORDER` | the required lines are out of order |
| `SHAPE_EXTRA` | extra, duplicate or malformed lines (markdown, commentary, blank lines, quotes) |
| `DRAFT_STYLE_LABEL_MISSING` | voice not calibrated but the DRAFT STYLE label is missing |
| `DRAFT_STYLE_LABEL_STALE` | voice calibrated but the DRAFT STYLE label is still present (warning) |
| `OPENER_TOO_LONG` | opener is over the word limit |
| `OPENER_NO_QUESTION` | opener has no question / ask (warning) |
| `OPENER_MULTIPLE_QUESTIONS` | opener asks more than one thing (one ask only) |
| `OPENER_PENDO_FEATURE` | opener names a Pendo feature (clear phrase) |
| `OPENER_PENDO_FEATURE_SOFT` | opener has a word that may be feature talk (warning, INFERRED list) |
| `ROLE_ASSUMED` | packet flags an ambiguous role but the text assumes the contact's role |
| `BANNED_PHRASE` | banned phrase or hype word |
| `EXCLAMATION` | exclamation mark |
| `EMOJI` | emoji |
| `UNSUPPORTED_CLAIM` | claim about Pendo results, customers, growth or round/verb details the packet cannot support |
| `MUST_NOT_CLAIM` | text contains a phrase on the packet's must-not-claim list |
| `HISTORY_CLAIM` | text implies earlier contact (we spoke, left a voicemail) that the packet cannot support |
| `EMAIL_ADDRESS` | an email address appears in the text |
| `PHONE_NUMBER` | a phone number appears in the text |
| `LINK` | a link or web address appears in the text |
| `FACT_NO_SOURCE` | a FACT line does not end with (source: ...) |
| `FACT_NONE_FORMAT` | 'none verified' written in the wrong form |
| `FACT_UNSUPPORTED` | fact text does not match a packet fact closely enough: possibly invented |
| `FACT_SOURCE_UNKNOWN` | cited source is not one of the packet's sources |
| `FACT_SOURCE_MISMATCH` | cited source is not the source of the fact it matches |
| `FACT_DATE_MISSING` | packet fact has a date but the citation does not carry it |
| `FACT_DATE_INVENTED` | citation carries a date the packet fact does not have |
| `FACT_DUPLICATE` | FACT 1 and FACT 2 are the same packet fact: padding |
| `FACT_SKIPPED` | 'none verified' written although the packet has usable facts (warning) |
| `NUMBER_UNSUPPORTED` | a number, number word or date in the text is not in the packet |
| `PROPER_NOUN_UNSUPPORTED` | a name (company, person, place, title) in the text is not in the packet |
| `INVENTED_TRIGGER` | text claims or reacts to an event but the packet has no real trigger |
| `TRIGGER_MISMATCH` | an event word in the text is not in the packet's trigger or facts |
| `TRIGGER_LINE_CLAIMED` | TRIGGER line claims a trigger but the packet has none |
| `TRIGGER_LINE_OMITTED` | packet has a trigger but the TRIGGER line says none (warning) |
| `TRIGGER_LINE_UNSUPPORTED` | TRIGGER line does not match the packet trigger evidence |
| `TRIGGER_SOURCE_MISMATCH` | TRIGGER line cites a source other than the packet trigger's |
| `TRIGGER_DATE_MISSING` | packet trigger has a date but the TRIGGER line does not carry it |
| `TRIGGER_DATE_INVENTED` | TRIGGER line carries a date the packet trigger does not have |
| `GATEKEEPER_TOO_LONG` | gatekeeper line is over the word limit |
| `GATEKEEPER_PITCH` | gatekeeper line pitches or mentions the trigger |
| `GATEKEEPER_PITCH_SOFT` | gatekeeper line has a word that may be pitch (warning, INFERRED list) |
| `GATEKEEPER_NO_ROLE` | gatekeeper line does not ask for a role or person |
| `RELATIVE_TIME_UNVERIFIED` | 'last month' style phrase the packet has no age_days to check (warning) |
| `SIMILAR_TO_DISLIKED` | near-duplicate of an opener Alex dislikes (warning) |
| `EMAIL_SUBJECT_LENGTH` | subject is not one line of the allowed word count |
| `EMAIL_SUBJECT_CAPS` | subject is shouting in capitals (warning) |
| `SUBJECT_FAKE_THREAD` | subject starts with Re:/Fwd: to fake a thread |
| `EMAIL_TOO_LONG` | body is over the word limit |
| `EMAIL_EMPTY` | subject or body is empty |
| `EMAIL_LINE_COUNT` | body does not have 3 to 5 content lines |
| `EMAIL_BULLETS` | bullets in the body |
| `EMAIL_OPENING` | first line does not start with the required opening |
| `EMAIL_NO_ASK` | body has no ask |
| `EMAIL_MULTIPLE_ASKS` | body has more than one ask |
| `EMAIL_SEND_LANGUAGE` | send, attach or invite language in any generated text (drafts only) |
| `EMAIL_SIGNOFF` | body does not end with the sign-off |

Severity is `error` unless the meaning says (warning). Fix every error. Warnings go in the run log.

## Measured results
All on FAKE blocks written by models, so a real run will differ (UNVERIFIED on real LLM output).

| Set | Before (commit `7b98a97`, from the review) | After (this commit) |
|---|---|---|
| Reviewer's adversarial set, 161 bad blocks: at least one error | 113 (70%) | **161 (100%)** |
| ... caught for the intended reason (the reviewer's code) | 98 (61%) | **147 (91%)** |
| Good blocks (32): false positives | 8 (25%) | **0 (0%)** |
| Natural ANZ openers (84): false positives | 42 (50%) | **2 (2%)** (a city the packet does not hold) |

The 14 blocks caught for a different but correct reason: series letters, "growth" claims, store ranking, career claims
and promotions (now `UNSUPPORTED_CLAIM`), a wrong citation date (`FACT_DATE_INVENTED` instead of `_MISSING`), a
paraphrased duplicate (`FACT_UNSUPPORTED` instead of `FACT_DUPLICATE`), an invented voicemail (`HISTORY_CLAIM`).
**Overfitting warning:** the rules were tuned while looking at that set, so those numbers are optimistic. Fresh cases
written by me, run **before** changing any rule for them:

| Fresh batch | Bad blocks caught | Good blocks wrongly failed |
|---|---|---|
| 1 (38 bad, 21 good) | 31 (84%) | 1 |
| 2 (27 bad, 14 good) | 20 (74%) | 1 (two short questions, kept strict) |
| 3 (15 bad, 10 good) | 9 (60%) | 0 |

After each batch the generalisable gaps were closed (event verbs and synonyms, currency and magnitude, relative time
against `age_days`, quality claims, "we <verb>", hearsay, filler). **Batch 3 is the best estimate of what a new,
different bad block faces: about 60%.** The after-fix tests for all three batches are regression tests, not estimates.

## What the lint CANNOT catch (read this)
- **A meaning-changing paraphrase in the spoken opener.** The opener is checked for names, numbers, event words,
  roles, claims and wording, but not sentence by sentence against the packet. "The partnership grew" for "announced a
  partnership", or "appointed a leader" when the packet says a different job, can pass if no checked word changes.
  FACT lines are held to 90% overlap, the opener is not. The controls are Alex skimming and, optionally, a second
  verifier pass by another model.
- **Invented facts that use no listed word.** "Their app is slow", "Dana is your new boss", "the board backed it": no
  list can cover English. Only the words in `unsupported_unless_in_packet`, the event, claim and reaction lists catch
  these.
- A **one-word company at the start of a sentence** (the checker cannot tell "Ringing software teams" from a name).
- Place names the packet does not hold (flagged as unsupported names, even when harmless).
- Number words other than the listed ones; links and phone numbers disguised beyond the covered patterns; anything
  not in English.
- Whether 25 words is really about 8 seconds out loud (no audio), and whether the text sounds like Alex.

## What is missing: needed from Alex (task 2e)
In `config/voice.yaml` under `alex_examples` (all empty, marked TODO):
1. `liked_openers`: 3 openers he likes.
2. `disliked_openers`: 3 he hates (the lint warns on near-duplicates once filled).
3. `proud_emails`: 2 emails he is proud of.
Then he reviews tuned output and `calibrated` flips to `true`. Nothing in this repo claims to know his voice before that.
The INFERRED lists (extra hype words, Pendo feature phrases, soft gatekeeper words) also need his confirmation.
