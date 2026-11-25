---
name: opener
description: Write a cold-call opener, two sourced research facts and a gatekeeper line in Alex's voice for one account, from a ResearchPacket (src/callsheet.py to_dict). Plain text, fixed shape, checked by python -m src.textcheck. Text only: never writes to Nooks, Gmail or Salesforce.
---

# Opener skill (v2: structure final, voice NOT yet calibrated to Alex)

Calls mostly last under 10 seconds (about two-thirds) and only about 1 in 4 pass 30 seconds. The opener and the first 10
seconds are the whole game. Write for the ear, not the eye.

## Hard rules
- **This skill returns text only.** Never write anything to Nooks, Gmail or Salesforce yourself, never send anything.
  Drafts and call tasks are created by other steps, only with `--live`.
- **The lint passing is necessary, not sufficient.** It catches invented numbers, names, events and banned wording. It
  cannot catch a true-sounding paraphrase that changes the meaning (see `docs/voice_guide.md`). Alex skims every line.
- Do not call any connector. The packet is the only source of facts. Do not fetch anything.

## Before you write
1. Read `config/voice.yaml`. If `calibrated: false`, the **first line of your output** is exactly
   `DRAFT STYLE: not yet calibrated to Alex`. If `calibrated: true`, print no label and use `alex_examples` (liked and
   disliked openers) as the style reference.
2. Read the packet. The fields that matter: `account_name`, `city`, `state`, `employees`, `prospect_first_name` (may be
   null), `prospect_title` (may be null), `signals[]` (best first; each has `kind`, `evidence`, `source` id,
   `source_label`, `date`, `age_days`, `status`), `has_real_trigger`, `ambiguity_flags`, `available_facts[]` (each
   `text` + `source`), `contact_history` (call log: see rules) and `facts_note`.

## Output: exactly this, plain text, nothing else
No markdown, no bold, no code fence, no quotes around a line, no commentary, no blank lines, in this order.

Template when `calibrated: false` (the first line is the label):
```text
DRAFT STYLE: not yet calibrated to Alex
OPENER: <one line, at most 25 words, one ask>
FACT 1: <fact> (source: <source as given>)
FACT 2: <fact> (source: <source as given>)
GATEKEEPER: <one line, under 15 words, asks for a role>
TRIGGER: <the real trigger restated> (source: <id>, <date>)
```
Template when `calibrated: true` (no label line):
```text
OPENER: <one line, at most 25 words, one ask>
FACT 1: <fact> (source: <source as given>)
FACT 2: <fact> (source: <source as given>)
GATEKEEPER: <one line, under 15 words, asks for a role>
TRIGGER: <the real trigger restated> (source: <id>, <date>)
```
With no real trigger the last line is exactly `TRIGGER: None found: cold call on ICP fit only`. With fewer than two
facts, the missing line is exactly `FACT 2: none verified` (`FACT 1: none verified` if there are none).

## Rules
- **Facts come only from `available_facts`.** Copy the fact's `source` into the tail exactly as given. Add a date to
  the tail only if the packet gives that fact a date; a fact with no date gets no date (never invent one). Rephrase
  lightly but add no number, name, place, currency, funding round or claim that the fact does not contain.
- **Fewer than two facts:** write `none verified`. Never pad, never repeat a fact, never fill from memory.
- **TRIGGER** restates the first usable signal's `evidence`, cites its `source` id (or its `source_label`) and its
  `date`. It is real only when `has_real_trigger` is true. An `intent` signal is interest, not news: it is NOT a
  trigger. If there is no real trigger, say so in the opener ("cold call, I'll be upfront") and write an honest
  fit-based opener. Never invent, stretch or borrow a trigger.
- With no real trigger: no "saw", "noticed", "congrats", "heard", and no event words (raised, launched, hired,
  appointed, announced, merged...) in the opener, even a plausible one. With a trigger, use its own words; an event
  word that is not in the packet (ended, acquired, cancelled, went public) is an error.
- Dates in the spoken opener: use `age_days` ("last week" for 7 days, "two weeks ago" for 14) or no date. Never guess.
- Never mention Pendo features (in-app guides, session replay, heatmaps, product analytics, checklists...).
- Never claim customers, results, percentages or "companies like yours". Never write "we help/make/build/improve ...".
- Never write an email address, phone number or link. Never use the contact's details.
- **`ambiguity_flags` not empty:** do not assume what the contact does: no title, no "your role", no "your team", no
  "as the ...". Ask who handles it.
- **`prospect_first_name` null:** write "Hi there". Never guess a name, a title or a gender.
- **`contact_history` and any "Previous contact" fact are the call log, not research.** Never use them in the opener or
  in a FACT line: no "we spoke", "left a voicemail", "following up", no call dates.
- Permission-based or curiosity opener, **one ask only** (one question; "right?" tags do not count, a second question
  does). Plain Australian business English, short sentences.
- No "hope you're well", "reaching out", "touch base", "circle back", "quick call", no hype words (leverage, synergy,
  game-changing, revolutionise, unlock...), no exclamation marks, no emojis, no send/attach/invite language.
- Gatekeeper line: ask for the role ("whoever looks after the app"), under 15 words, no pitch, no trigger, no topic
  such as analytics.

## How to run the lint
Save the packet JSON as `packet.json` and your block as `block.txt`, then run:
```text
python -m src.textcheck opener --packet packet.json --text block.txt --voice config/voice.yaml
```
It prints one line per problem (`ERROR CODE: message`) or `OK: no violations`. Exit code 0 = no errors, 1 = errors,
2 = could not run (bad file or bad config: stop and report it, do not skip the lint). Warnings do not block: put them in
the run log.

## Retry rule
If there are errors, regenerate **at most 3 times**, changing only what the reported violations name. If errors remain
after the third try, output exactly `NEEDS HUMAN: <the error codes, comma separated>` and nothing else, and **do not
ship the text**. Never edit the lint, the config or the packet to make a block pass.

## Worked examples
**FAKE: invented companies and people. Do not copy the wording: structure only.** They show the shape, the citation
style and the honest edge cases, not Alex's voice. Shown without the DRAFT STYLE line; while `calibrated` is false add
it first. A test lints every example against its packet, so they stay correct. (`must_not_claim` and a few packet
fields are left out of the JSON for space.)

### Example 1: New leader (exec_move)
Trigger present. The opener names the event, uses the packet's own words, makes one ask. Facts cite the fact's `source`; the TRIGGER cites the signal's `source` id and date.

Packet (fake):
```json
{
 "account_name": "Larkspur Insurance",
 "industry": null,
 "state": "Victoria",
 "city": "Melbourne",
 "employees": 620,
 "prospect_first_name": "Priya",
 "prospect_title": "Head of Product",
 "signals": [
  {
   "kind": "exec_move",
   "label": "Executive Move",
   "evidence": "Larkspur Insurance appointed Dana Whitfield as Chief Digital Officer.",
   "source": "zoominfo_scoop:FAKE-101",
   "source_label": "ZoomInfo scoop 2026-11-12",
   "date": "2026-11-12",
   "age_days": 13,
   "status": "ok",
   "ambiguous": false
  }
 ],
 "has_real_trigger": true,
 "trigger_kind": "exec_move",
 "ambiguity_flags": [],
 "contact_history": null,
 "contact_history_source": null,
 "available_facts": [
  {
   "text": "Larkspur Insurance appointed Dana Whitfield as Chief Digital Officer",
   "source": "ZoomInfo scoop 2026-11-12"
  },
  {
   "text": "About 620 employees",
   "source": "Salesforce account record (ZoomInfo employee count)"
  },
  {
   "text": "Based in Melbourne, Victoria",
   "source": "Salesforce account record"
  }
 ]
}
```

Output:
```text
OPENER: Hi Priya, Alex from Pendo. Larkspur has a new Chief Digital Officer. Who owns the digital product side now?
FACT 1: Larkspur Insurance appointed Dana Whitfield as Chief Digital Officer (source: ZoomInfo scoop 2026-11-12)
FACT 2: About 620 employees (source: Salesforce account record (ZoomInfo employee count))
GATEKEEPER: Who looks after digital product there, please?
TRIGGER: Larkspur Insurance appointed Dana Whitfield as Chief Digital Officer (source: zoominfo_scoop:FAKE-101, 2026-11-12)
```

### Example 2: Funding
A reaction ('congrats') is allowed only because a real trigger exists. The amount stays out of the spoken opener.

Packet (fake):
```json
{
 "account_name": "Bellbird Fitness",
 "industry": null,
 "state": null,
 "city": "Auckland",
 "employees": 140,
 "prospect_first_name": "Jonas",
 "prospect_title": "Product Owner",
 "signals": [
  {
   "kind": "funding",
   "label": "Funding",
   "evidence": "Bellbird Fitness raised NZ$18 million in a Series B round.",
   "source": "zoominfo_scoop:FAKE-202",
   "source_label": "ZoomInfo scoop 2026-11-03",
   "date": "2026-11-03",
   "age_days": 22,
   "status": "ok",
   "ambiguous": false
  }
 ],
 "has_real_trigger": true,
 "trigger_kind": "funding",
 "ambiguity_flags": [],
 "contact_history": null,
 "contact_history_source": null,
 "available_facts": [
  {
   "text": "Bellbird Fitness raised NZ$18 million in a Series B round",
   "source": "ZoomInfo scoop 2026-11-03"
  },
  {
   "text": "About 140 employees",
   "source": "Salesforce account record (ZoomInfo employee count)"
  },
  {
   "text": "Based in Auckland",
   "source": "Salesforce account record"
  }
 ]
}
```

Output:
```text
OPENER: Hi Jonas, Alex from Pendo. Congrats on the Series B. Who owns product there now?
FACT 1: Bellbird Fitness raised NZ$18 million in a Series B round (source: ZoomInfo scoop 2026-11-03)
FACT 2: Based in Auckland (source: Salesforce account record)
GATEKEEPER: Who looks after the product team there, please?
TRIGGER: Bellbird Fitness raised NZ$18 million in a Series B round (source: zoominfo_scoop:FAKE-202, 2026-11-03)
```

### Example 3: Product launch, relative date from age_days
'last week' is licensed by age_days 7. With no age_days in the packet, use an absolute date or no date.

Packet (fake):
```json
{
 "account_name": "Tidewater Credit Union",
 "industry": null,
 "state": "Queensland",
 "city": "Brisbane",
 "employees": 310,
 "prospect_first_name": "Marcus",
 "prospect_title": "Digital Product Lead",
 "signals": [
  {
   "kind": "product_launch",
   "label": "Product Launch",
   "evidence": "Tidewater Credit Union launched a new mobile banking app for members.",
   "source": "zoominfo_scoop:FAKE-301",
   "source_label": "ZoomInfo scoop 2026-11-18",
   "date": "2026-11-18",
   "age_days": 7,
   "status": "ok",
   "ambiguous": false
  }
 ],
 "has_real_trigger": true,
 "trigger_kind": "product_launch",
 "ambiguity_flags": [],
 "contact_history": null,
 "contact_history_source": null,
 "available_facts": [
  {
   "text": "Tidewater Credit Union launched a new mobile banking app for members",
   "source": "ZoomInfo scoop 2026-11-18"
  },
  {
   "text": "About 310 employees",
   "source": "Salesforce account record (ZoomInfo employee count)"
  },
  {
   "text": "Based in Brisbane, Queensland",
   "source": "Salesforce account record"
  }
 ]
}
```

Output:
```text
OPENER: Hi Marcus, Alex from Pendo. Saw the new Tidewater app launched last week. Curious how the first weeks are going?
FACT 1: Tidewater Credit Union launched a new mobile banking app for members (source: ZoomInfo scoop 2026-11-18)
FACT 2: About 310 employees (source: Salesforce account record (ZoomInfo employee count))
GATEKEEPER: Who is the right person for the member app, please?
TRIGGER: Tidewater Credit Union launched a new mobile banking app for members (source: zoominfo_scoop:FAKE-301, 2026-11-18)
```

### Example 4: No trigger: cold call on fit only
No signals, so no event words and no reactions anywhere in the opener, and the TRIGGER line is the exact none line.

Packet (fake):
```json
{
 "account_name": "Marlow Software Group",
 "industry": "Software",
 "state": "Western Australia",
 "city": "Perth",
 "employees": 85,
 "prospect_first_name": "Ellie",
 "prospect_title": "Engineering Manager",
 "signals": [],
 "has_real_trigger": false,
 "trigger_kind": null,
 "ambiguity_flags": [],
 "contact_history": null,
 "contact_history_source": null,
 "available_facts": [
  {
   "text": "Industry: Software",
   "source": "Salesforce account record"
  },
  {
   "text": "About 85 employees",
   "source": "Salesforce account record (ZoomInfo employee count)"
  },
  {
   "text": "Based in Perth, Western Australia",
   "source": "Salesforce account record"
  }
 ]
}
```

Output:
```text
OPENER: Hi Ellie, Alex from Pendo. Cold call, I'll be upfront. Marlow looks like a fit, so who owns product there?
FACT 1: Industry: Software (source: Salesforce account record)
FACT 2: About 85 employees (source: Salesforce account record (ZoomInfo employee count))
GATEKEEPER: Could I speak to whoever owns the product side, please?
TRIGGER: None found: cold call on ICP fit only
```

### Example 5: Ambiguous role flag
`ambiguity_flags` is not empty, so the opener asks who handles it instead of using the contact's title, 'your role' or 'your team'.

Packet (fake):
```json
{
 "account_name": "Greenvale Dairy",
 "industry": null,
 "state": null,
 "city": "Hamilton",
 "employees": 450,
 "prospect_first_name": "Tane",
 "prospect_title": "Product Manager",
 "signals": [
  {
   "kind": "hiring_plan",
   "label": "Hiring Plans",
   "evidence": "Greenvale Dairy is hiring a Head of Digital.",
   "source": "zoominfo_scoop:FAKE-501",
   "source_label": "ZoomInfo scoop 2026-11-09",
   "date": "2026-11-09",
   "age_days": 16,
   "status": "ok",
   "ambiguous": true
  }
 ],
 "has_real_trigger": true,
 "trigger_kind": "hiring_plan",
 "ambiguity_flags": [
  "role ambiguous: check it is a digital product role"
 ],
 "contact_history": null,
 "contact_history_source": null,
 "available_facts": [
  {
   "text": "Greenvale Dairy is hiring a Head of Digital",
   "source": "ZoomInfo scoop 2026-11-09"
  },
  {
   "text": "About 450 employees",
   "source": "Salesforce account record (ZoomInfo employee count)"
  },
  {
   "text": "Based in Hamilton",
   "source": "Salesforce account record"
  }
 ]
}
```

Output:
```text
OPENER: Hi Tane, Alex from Pendo. Greenvale is hiring a Head of Digital. Who handles that side of the business there?
FACT 1: Greenvale Dairy is hiring a Head of Digital (source: ZoomInfo scoop 2026-11-09)
FACT 2: About 450 employees (source: Salesforce account record (ZoomInfo employee count))
GATEKEEPER: Who looks after your digital projects, please?
TRIGGER: Greenvale Dairy is hiring a Head of Digital (source: zoominfo_scoop:FAKE-501, 2026-11-09)
```

### Example 6: Only one fact
Only one verified fact exists, so FACT 2 is `none verified`. Never pad.

Packet (fake):
```json
{
 "account_name": "Cinder Payments",
 "industry": null,
 "state": null,
 "city": null,
 "employees": null,
 "prospect_first_name": "Wei",
 "prospect_title": "Director of Engineering",
 "signals": [
  {
   "kind": "partnership",
   "label": "Partnership",
   "evidence": "Cinder Payments announced a partnership with Orbis Bank.",
   "source": "zoominfo_scoop:FAKE-601",
   "source_label": "ZoomInfo scoop 2026-10-29",
   "date": "2026-10-29",
   "age_days": 27,
   "status": "ok",
   "ambiguous": false
  }
 ],
 "has_real_trigger": true,
 "trigger_kind": "partnership",
 "ambiguity_flags": [],
 "contact_history": null,
 "contact_history_source": null,
 "available_facts": [
  {
   "text": "Cinder Payments announced a partnership with Orbis Bank",
   "source": "ZoomInfo scoop 2026-10-29"
  }
 ]
}
```

Output:
```text
OPENER: Hi Wei, Alex from Pendo. Saw the Orbis Bank partnership. Does it mean more load on engineering?
FACT 1: Cinder Payments announced a partnership with Orbis Bank (source: ZoomInfo scoop 2026-10-29)
FACT 2: none verified
GATEKEEPER: Who runs engineering there, please?
TRIGGER: Cinder Payments announced a partnership with Orbis Bank (source: zoominfo_scoop:FAKE-601, 2026-10-29)
```

### Example 7: No first name or title (greet as 'there')
`prospect_first_name` is null: write 'Hi there'. Never guess a name, a title or a gender.

Packet (fake):
```json
{
 "account_name": "Echo Logistics Pty Ltd",
 "industry": null,
 "state": "New South Wales",
 "city": null,
 "employees": null,
 "prospect_first_name": null,
 "prospect_title": null,
 "signals": [],
 "has_real_trigger": false,
 "trigger_kind": null,
 "ambiguity_flags": [],
 "contact_history": null,
 "contact_history_source": null,
 "available_facts": [
  {
   "text": "Based in New South Wales",
   "source": "Salesforce account record"
  }
 ]
}
```

Output:
```text
OPENER: Hi there, Alex from Pendo. Cold call, I'll be upfront. Who looks after digital product at Echo?
FACT 1: Based in New South Wales (source: Salesforce account record)
FACT 2: none verified
GATEKEEPER: Who looks after digital product there, please?
TRIGGER: None found: cold call on ICP fit only
```

### Example 8: Intent signal only: not a trigger
`has_real_trigger` is false: an intent signal is interest, not news. No 'saw', no 'noticed', and the TRIGGER line is the none line. The intent text may appear as a fact.

Packet (fake):
```json
{
 "account_name": "Delta Health Pty Ltd",
 "industry": null,
 "state": "Queensland",
 "city": "Brisbane",
 "employees": 75,
 "prospect_first_name": "Sam",
 "prospect_title": "Head of Product",
 "signals": [
  {
   "kind": "intent",
   "label": "Intent",
   "evidence": "researching customer experience",
   "source": "zoominfo_intent:FAKE-I-1",
   "source_label": "ZoomInfo intent signal",
   "date": null,
   "age_days": null,
   "status": "intent",
   "ambiguous": false
  }
 ],
 "has_real_trigger": false,
 "trigger_kind": null,
 "ambiguity_flags": [],
 "contact_history": null,
 "contact_history_source": null,
 "available_facts": [
  {
   "text": "Intent signal (interest, not a dated event): researching customer experience",
   "source": "ZoomInfo intent signal"
  },
  {
   "text": "About 75 employees",
   "source": "Salesforce account record (ZoomInfo employee count)"
  },
  {
   "text": "Based in Brisbane, Queensland",
   "source": "Salesforce account record"
  }
 ]
}
```

Output:
```text
OPENER: Hi Sam, Alex from Pendo. Cold call, I'll be upfront. Who looks after customer experience at Delta?
FACT 1: Intent signal (interest, not a dated event): researching customer experience (source: ZoomInfo intent signal)
FACT 2: About 75 employees (source: Salesforce account record (ZoomInfo employee count))
GATEKEEPER: Who looks after customer experience there, please?
TRIGGER: None found: cold call on ICP fit only
```

### Example 9: Call history present: never used
`contact_history` and the 'Previous contact' fact are for the call log, not for the opener or the FACT lines: no 'we spoke', no 'left a voicemail', no call dates.

Packet (fake):
```json
{
 "account_name": "Fennel Software",
 "industry": "Software",
 "state": "New South Wales",
 "city": "Sydney",
 "employees": 40,
 "prospect_first_name": "Ellie",
 "prospect_title": "Engineering Manager",
 "signals": [],
 "has_real_trigger": false,
 "trigger_kind": null,
 "ambiguity_flags": [],
 "contact_history": "last call 2026-11-20, outcome No Answer",
 "contact_history_source": "Nooks call log",
 "available_facts": [
  {
   "text": "Industry: Software",
   "source": "Salesforce account record"
  },
  {
   "text": "About 40 employees",
   "source": "Salesforce account record (ZoomInfo employee count)"
  },
  {
   "text": "Previous contact: last call 2026-11-20, outcome No Answer",
   "source": "Nooks call log"
  }
 ]
}
```

Output:
```text
OPENER: Hi Ellie, Alex from Pendo. Cold call, I'll be upfront. Fennel looks like a fit, so who owns product there?
FACT 1: Industry: Software (source: Salesforce account record)
FACT 2: About 40 employees (source: Salesforce account record (ZoomInfo employee count))
GATEKEEPER: Could I speak to whoever owns the product side, please?
TRIGGER: None found: cold call on ICP fit only
```

## Voice notes (Alex: edit)
- TODO Alex: 3 openers you like, 3 you hate, 2 emails you are proud of, in `config/voice.yaml` under `alex_examples`.
  Until then the style above is a structural draft, not your voice. Do not invent his taste.
