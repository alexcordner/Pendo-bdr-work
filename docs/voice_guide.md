# Voice guide and text lint (task 2d)

Offline. No live data. Everything here is structure; **Alex's real taste is not in it yet** (see "What is missing").

## Pieces
| File | Role |
|---|---|
| `config/voice.yaml` | Machine-readable rules: limits, banned phrases, patterns, `calibrated` flag, `alex_examples` slots |
| `src/textcheck.py` | Deterministic lint: `check_opener_block`, `check_email_draft`, `format_violations`, `parse_block`, `parse_email_block`, `load_voice_config` |
| `skills/opener/SKILL.md`, `skills/email-draft/SKILL.md` | Instructions for the Claude session that writes the text |
| `tests/fixtures/text_packets.json` | FAKE packets used by tests and by the skill examples |

The LLM writes the text. Python only checks it (CLAUDE.md design rule). A session must run the check and fix every
`error` before text goes into a call sheet or a Gmail draft. Drafts only: nothing here sends anything.

## Calibration flag
`calibrated: false` in `config/voice.yaml` until Alex has supplied his examples and approved a tuned run (task 2e).
While false:
- opener blocks start with the line `DRAFT STYLE: not yet calibrated to Alex`;
- email bodies start with the same line (Alex deletes it when he reviews the draft);
- the lint raises `DRAFT_STYLE_LABEL_MISSING` if it is absent. Once true, a leftover label is only a warning.

## Rules, and where each came from
Only from `skills/*/SKILL.md` and the call-length data in `docs/connector_notes.md` (about two-thirds of calls under
10 s, about a quarter over 30 s). Anything marked INFERRED in `voice.yaml` extends a general skill rule and is not
confirmed taste: the extra hype words and the list of Pendo feature words.

| Rule | Value | Source |
|---|---|---|
| Opener length | at most 25 words, one line, one ask | opener skill; call-length data |
| Gatekeeper | under 15 words (max 14), asks for a role, no pitch | opener skill |
| Email subject | 2 to 5 words | email skill |
| Email body | under 80 words, 3 to 5 content lines, one ask, opens "Just tried you", signs "Alex" | email skill |
| Banned | "hope you're well", "reaching out", "quick call", hype words; email also "quick", attachments | both skills |
| No exclamation marks, no emojis | | email skill (applied to all text) |
| No Pendo features in the opener | | opener skill |
| No customer names or result claims unless supplied | | both skills |
| Facts only from the packet, each with source and date | `FACT 2: none verified` if fewer than two | opener skill |
| No real trigger means the line `TRIGGER: None found: cold call on ICP fit only` | | opener skill, CLAUDE.md rule 6 |

## Packet shape the lint consumes
All keys optional; extra keys are ignored.
```json
{
  "account_name": "Fake Co", "website": "fake.example.com", "location": "Sydney, NSW", "employees": 120,
  "prospect_first_name": "Sam", "prospect_title": "Head of Product",
  "trigger": {"kind": "funding", "evidence": "...", "source": "zoominfo_scoop:FAKE-1", "date": "2026-11-03"},
  "available_facts": [{"text": "...", "source": "zoominfo_scoop:FAKE-1", "date": "2026-11-03"}],
  "flags": ["ambiguous_role"],
  "must_not_claim": ["existing customer"]
}
```
`trigger` is `null` / missing / `{"kind": "none"}` when there is no real trigger.

### Mapping to the real ResearchPacket (task 2c): coordinator to reconcile
| Lint reads | Also accepted | Notes |
|---|---|---|
| `account_name` | `name`, `account.name` | |
| `website`, `location`, `employees` | | only used as allowed-text for number/name checks |
| `prospect_first_name` | `first_name`, `prospect.first_name` | |
| `prospect_title` | `title`, `prospect.title` | used for the ambiguous-role check |
| `trigger` | else the strongest of `signals` (by `strength`, kind not none) | `evidence` (or `text`), `source`, `date` (or `source_date`); enum or string `kind` |
| `available_facts[]` | | `text`, `source`, `date`. **A fact with no source, or a bare string, is unusable and ignored** |
| `flags[]` | | any flag containing both "ambig" and "role" turns the ambiguous-role rule on |
| `must_not_claim` | `must_not_invent` | literal phrases, case-insensitive substring |
| `prospect_email`, `prospect_phone` (`email`, `phone`) | | if present, must never appear in the text |

## How the lint decides
- **Shape:** the five lines in order, nothing else (optional label first). Extra lines, markdown or commentary are errors.
- **Facts:** each must end with `(source: ...)`. Its words must overlap one packet fact (at least `fact_overlap_min`
  of its content words), else `FACT_UNSUPPORTED` "possibly invented". The cited text must contain that packet entry's
  source and date. Two FACT lines cannot be the same packet fact.
- **Numbers and names:** every number and capitalised word in the opener, facts, gatekeeper and trigger line must occur
  somewhere in the packet (or be on the `allowed_proper_nouns` list, e.g. Alex, Pendo). A sentence-initial word is only
  tested against `sentence_start_words` in `voice.yaml` (so an invented company at the start of a sentence is caught,
  but a legitimate new starter word is also flagged until added to that list).
- **Trigger:** with no packet trigger, the opener and email may not use reaction or event words (congrats, saw, raised,
  launched, hiring ...) and the TRIGGER line must be the exact none line. With a trigger, each event word used must
  also be in its evidence, and the TRIGGER line must restate the evidence with the packet source and date.
- **Claims:** patterns such as "we help", "companies like yours", "increase X by", "case study" are errors because the
  packet cannot supply customer evidence.

## Lint codes
| Code | Meaning |
|---|---|
| `PACKET_INVALID` | the packet is not a mapping |
| `SHAPE_MISSING` | a required line (OPENER / FACT 1 / FACT 2 / GATEKEEPER / TRIGGER) is missing |
| `SHAPE_ORDER` | the required lines are out of order |
| `SHAPE_EXTRA` | extra, duplicate or malformed lines (markdown, commentary, blank lines) |
| `DRAFT_STYLE_LABEL_MISSING` | voice not calibrated but the DRAFT STYLE label is missing |
| `DRAFT_STYLE_LABEL_STALE` | voice calibrated but the DRAFT STYLE label is still present (warning) |
| `OPENER_TOO_LONG` | opener is over the word limit |
| `OPENER_NO_QUESTION` | opener has no question / ask (warning) |
| `OPENER_MULTIPLE_QUESTIONS` | opener asks more than one thing (warning) |
| `OPENER_PENDO_FEATURE` | opener mentions a Pendo feature |
| `ROLE_ASSUMED` | packet flags an ambiguous role but the text assumes the contact's role |
| `BANNED_PHRASE` | banned phrase or hype word |
| `EXCLAMATION` | exclamation mark |
| `EMOJI` | emoji |
| `UNSUPPORTED_CLAIM` | claim about Pendo results or customers that the packet cannot support |
| `MUST_NOT_CLAIM` | text contains something on the packet's must-not-claim list |
| `EMAIL_ADDRESS` | an email address appears in the text |
| `PHONE_NUMBER` | a phone number appears in the text |
| `LINK` | a link or web address appears in the text |
| `FACT_NO_SOURCE` | a FACT line does not end with (source: ...) |
| `FACT_NONE_FORMAT` | 'none verified' written in the wrong form |
| `FACT_UNSUPPORTED` | fact text does not overlap any packet fact: possibly invented |
| `FACT_SOURCE_UNKNOWN` | cited source is not one of the packet's sources |
| `FACT_SOURCE_MISMATCH` | cited source is not the source of the fact it matches |
| `FACT_DATE_MISSING` | cited source line does not carry the packet date |
| `FACT_DUPLICATE` | FACT 1 and FACT 2 are the same packet fact: padding |
| `FACT_SKIPPED` | 'none verified' written although the packet has usable facts (warning) |
| `NUMBER_UNSUPPORTED` | a number or date in the text is not in the packet |
| `PROPER_NOUN_UNSUPPORTED` | a name (company, person, place, title) in the text is not in the packet |
| `INVENTED_TRIGGER` | text claims or reacts to an event but the packet has no real trigger |
| `TRIGGER_MISMATCH` | an event word in the text is not in the packet's trigger evidence |
| `TRIGGER_LINE_CLAIMED` | TRIGGER line claims a trigger but the packet has none |
| `TRIGGER_LINE_OMITTED` | packet has a trigger but the TRIGGER line says none (warning) |
| `TRIGGER_LINE_UNSUPPORTED` | TRIGGER line does not match the packet trigger evidence |
| `TRIGGER_SOURCE_MISMATCH` | TRIGGER line cites a source other than the packet trigger's |
| `TRIGGER_DATE_MISSING` | TRIGGER line does not carry the packet trigger date |
| `GATEKEEPER_TOO_LONG` | gatekeeper line is over the word limit |
| `GATEKEEPER_PITCH` | gatekeeper line contains pitch language |
| `GATEKEEPER_NO_ROLE` | gatekeeper line does not ask for a role or person |
| `SIMILAR_TO_DISLIKED` | near-duplicate of an opener Alex dislikes (warning) |
| `EMAIL_SUBJECT_LENGTH` | subject is not one line of the allowed word count |
| `EMAIL_SUBJECT_CAPS` | subject is shouting in capitals (warning) |
| `EMAIL_TOO_LONG` | body is over the word limit |
| `EMAIL_EMPTY` | subject or body is empty |
| `EMAIL_LINE_COUNT` | body does not have 3 to 5 content lines |
| `EMAIL_OPENING` | first line does not start with the required opening |
| `EMAIL_NO_ASK` | body has no ask |
| `EMAIL_MULTIPLE_ASKS` | body has more than one ask |
| `EMAIL_SEND_LANGUAGE` | body contains send language (drafts only) |
| `EMAIL_SIGNOFF` | body does not end with the sign-off (warning) |

Severity is `error` unless the meaning says (warning). Callers must fix every error.

## Known limits (honest)
- Token overlap catches invented facts and numbers; it cannot catch a true-sounding paraphrase that changes meaning.
- Number words ("thirty") are not checked. The sentence-starter list will need extending as real output shows new openers.
- The Pendo-feature list, hype list and gatekeeper pitch/role word lists are heuristics and will need tuning.
- The lint says nothing about whether text sounds like Alex. Only his examples can fix that.

## What is missing: needed from Alex (task 2e)
In `config/voice.yaml` under `alex_examples` (all empty, marked TODO):
1. `liked_openers`: 3 openers he likes.
2. `disliked_openers`: 3 he hates (the lint already warns on near-duplicates once filled).
3. `proud_emails`: 2 emails he is proud of.
Then he reviews tuned output and `calibrated` flips to `true`. Nothing in this repo claims to know his voice before that.
