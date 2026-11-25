---
name: opener
description: Write a cold-call opener, two sourced research facts and a gatekeeper line in Alex's voice for one account, from a ResearchPacket. Plain text, fixed shape, checked by src/textcheck.py. Use when building call sheet notes.
---

# Opener skill (v1, structure final, voice NOT yet calibrated to Alex)

Calls mostly last under 10 seconds (about two-thirds), and only about 1 in 4 pass 30 seconds. The opener and the first
10 seconds are the whole game. Write for the ear, not the eye.

## Before you write
1. Read `config/voice.yaml`. If `calibrated: false`, the **first line of your output** must be exactly
   `DRAFT STYLE: not yet calibrated to Alex`. If `calibrated: true`, do not print that line and use `alex_examples`
   (liked / disliked openers) as the style reference.
2. Take the ResearchPacket as given (shape in `docs/voice_guide.md`). Do not fetch anything yourself. Do not call any
   connector. The packet is the only source of facts.

## Output: exactly this, plain text, nothing else
No markdown, no bold, no code fence, no commentary, no blank lines, in this order:
```
[DRAFT STYLE: not yet calibrated to Alex]      <- only while calibrated is false
OPENER: <one line, at most 25 words, about 8 seconds out loud>
FACT 1: <fact> (source: <source from the packet>, <date from the packet>)
FACT 2: <fact> (source: <source from the packet>, <date from the packet>)
GATEKEEPER: <one line, under 15 words, asks for a role, no pitch>
TRIGGER: <the real trigger restated> (source: <source>, <date>)   or   TRIGGER: None found: cold call on ICP fit only
```

## Rules
- **Facts come only from the packet's `available_facts`.** Each carries a source and a date; copy both into the
  `(source: ...)` tail. Rephrase lightly but never add a number, name, place or claim that is not in that fact.
- Fewer than two facts: write `FACT 2: none verified` (and `FACT 1: none verified` if there are none). Never pad,
  never repeat a fact, never fill from memory or a guess.
- **TRIGGER** restates the packet trigger (its `evidence`, `source`, `date`). If the packet has no trigger (kind none or
  missing) write exactly `TRIGGER: None found: cold call on ICP fit only`, say so in the opener ("cold call, I'll be
  upfront" style), and write an honest fit-based opener. Never invent, stretch or borrow a trigger. No "saw", "noticed",
  "congrats", "raised", "launched", "hiring" and similar event words in the opener when there is no trigger.
- With a trigger, an event word in the opener must also be in the trigger evidence.
- The opener never mentions Pendo features (analytics, guides, in-app, session replay and so on).
- Never claim customer names, results, percentages or "companies like yours". The packet's `must_not_claim` list is
  also off limits.
- Never use the contact's email or phone, and never write any email address, phone number or link.
- If `flags` has an ambiguous role, the opener must not assume what the contact does: do not use their title, "your
  role", "your team", "as the ...". Ask who handles it instead.
- Permission-based or curiosity opener, one ask only. Plain Australian business English, short sentences.
- No "hope you're well", no "I'm reaching out", no "quick call", no hype words (leverage, synergy, game-changing,
  revolutionise), no exclamation marks, no emojis.
- Gatekeeper line: ask for the role ("whoever looks after your app"), under 15 words, no pitch, no trigger, no product.
- Dates: copy the packet's date in the source tail. Relative phrases in the opener ("this month") only if true on the
  packet date.

## After you write
Run `check_opener_block(text, packet, voice_cfg)` from `src/textcheck.py`. Fix every `error` and regenerate; do not hand
over text that has errors. Warnings go in the call log.

## Worked examples
**FAKE. Invented companies and people. Not real data. Shown without the DRAFT STYLE label; while `calibrated` is false
add that line first.** A test lints every example below against its packet, so they stay correct.

### Example 1: New leader (exec_move)
Trigger present; the opener names the event but no Pendo feature and no claim about results.

Packet (fake):
```json
{
  "account_name": "Larkspur Insurance",
  "website": "larkspur-insurance.example.com",
  "location": "Melbourne, Victoria",
  "employees": 620,
  "prospect_first_name": "Priya",
  "prospect_title": "Head of Product",
  "trigger": {
    "kind": "exec_move",
    "evidence": "Larkspur Insurance appointed Dana Whitfield as Chief Digital Officer.",
    "source": "zoominfo_scoop:FAKE-101",
    "date": "2026-11-12"
  },
  "available_facts": [
    {
      "text": "Larkspur Insurance appointed Dana Whitfield as Chief Digital Officer",
      "source": "zoominfo_scoop:FAKE-101",
      "date": "2026-11-12"
    },
    {
      "text": "Larkspur Insurance runs a customer app for policy claims",
      "source": "website:larkspur-insurance.example.com/app",
      "date": "2026-11-20"
    }
  ],
  "flags": [],
  "must_not_claim": [
    "existing customer",
    "case study"
  ]
}
```

Output:
```text
OPENER: Hi Priya, Alex from Pendo. Larkspur just appointed a new Chief Digital Officer. Is now a bad time for a short question?
FACT 1: Larkspur Insurance appointed Dana Whitfield as Chief Digital Officer (source: zoominfo_scoop:FAKE-101, 2026-11-12)
FACT 2: Larkspur Insurance runs a customer app for policy claims (source: website:larkspur-insurance.example.com/app, 2026-11-20)
GATEKEEPER: Could you point me to whoever looks after your customer app?
TRIGGER: Larkspur Insurance appointed Dana Whitfield as Chief Digital Officer (source: zoominfo_scoop:FAKE-101, 2026-11-12)
```

### Example 2: Funding
Reaction plus a curious question. The amount stays in FACT 1 only.

Packet (fake):
```json
{
  "account_name": "Bellbird Fitness",
  "website": "bellbird-fitness.example.com",
  "location": "Auckland, New Zealand",
  "employees": 140,
  "prospect_first_name": "Jonas",
  "prospect_title": "Product Owner",
  "trigger": {
    "kind": "funding",
    "evidence": "Bellbird Fitness raised NZ$18 million in a Series B round.",
    "source": "zoominfo_scoop:FAKE-202",
    "date": "2026-11-03"
  },
  "available_facts": [
    {
      "text": "Bellbird Fitness raised NZ$18 million in a Series B round",
      "source": "zoominfo_scoop:FAKE-202",
      "date": "2026-11-03"
    },
    {
      "text": "Bellbird Fitness is hiring a Product Designer",
      "source": "zoominfo_scoop:FAKE-203",
      "date": "2026-11-17"
    }
  ],
  "flags": [],
  "must_not_claim": [
    "existing customer"
  ]
}
```

Output:
```text
OPENER: Hi Jonas, Alex from Pendo. Congratulations on the Series B. Can I ask one question about where the product goes next?
FACT 1: Bellbird Fitness raised NZ$18 million in a Series B round (source: zoominfo_scoop:FAKE-202, 2026-11-03)
FACT 2: Bellbird Fitness is hiring a Product Designer (source: zoominfo_scoop:FAKE-203, 2026-11-17)
GATEKEEPER: Who looks after the product team there, please?
TRIGGER: Bellbird Fitness raised NZ$18 million in a Series B round (source: zoominfo_scoop:FAKE-202, 2026-11-03)
```

### Example 3: Product launch
The opener uses the launch; FACT 2 is a Salesforce field, so its source says so.

Packet (fake):
```json
{
  "account_name": "Tidewater Credit Union",
  "website": "tidewater-cu.example.com",
  "location": "Brisbane, Queensland",
  "employees": 310,
  "prospect_first_name": "Marcus",
  "prospect_title": "Digital Product Lead",
  "trigger": {
    "kind": "product_launch",
    "evidence": "Tidewater Credit Union launched a new mobile banking app for members.",
    "source": "zoominfo_scoop:FAKE-301",
    "date": "2026-11-18"
  },
  "available_facts": [
    {
      "text": "Tidewater Credit Union launched a new mobile banking app for members",
      "source": "zoominfo_scoop:FAKE-301",
      "date": "2026-11-18"
    },
    {
      "text": "Tidewater Credit Union has about 310 employees",
      "source": "salesforce:Account.ZoomInfo_Employee_Count__c",
      "date": "2026-11-01"
    }
  ],
  "flags": [],
  "must_not_claim": []
}
```

Output:
```text
OPENER: Hi Marcus, Alex from Pendo. Saw the new Tidewater app launched this month. Curious how the first weeks are going?
FACT 1: Tidewater Credit Union launched a new mobile banking app for members (source: zoominfo_scoop:FAKE-301, 2026-11-18)
FACT 2: Tidewater Credit Union has about 310 employees (source: salesforce:Account.ZoomInfo_Employee_Count__c, 2026-11-01)
GATEKEEPER: Who is the right person for the member app, please?
TRIGGER: Tidewater Credit Union launched a new mobile banking app for members (source: zoominfo_scoop:FAKE-301, 2026-11-18)
```

### Example 4: No trigger (cold call on fit only)
No trigger, so the opener says it is a cold call and uses only a fit fact. No event words anywhere in the opener.

Packet (fake):
```json
{
  "account_name": "Marlow Software Group",
  "website": "marlow-software.example.com",
  "location": "Perth, Western Australia",
  "employees": 85,
  "prospect_first_name": "Ellie",
  "prospect_title": "Engineering Manager",
  "trigger": null,
  "available_facts": [
    {
      "text": "Marlow Software Group builds project software for builders",
      "source": "website:marlow-software.example.com",
      "date": "2026-11-20"
    },
    {
      "text": "Marlow Software Group has about 85 employees",
      "source": "salesforce:Account.ZoomInfo_Employee_Count__c",
      "date": "2026-11-01"
    }
  ],
  "flags": [],
  "must_not_claim": [
    "existing customer"
  ]
}
```

Output:
```text
OPENER: Hi Ellie, Alex from Pendo. Cold call, I'll be upfront. Marlow builds software for builders, and I'd like to ask one question. Fair?
FACT 1: Marlow Software Group builds project software for builders (source: website:marlow-software.example.com, 2026-11-20)
FACT 2: Marlow Software Group has about 85 employees (source: salesforce:Account.ZoomInfo_Employee_Count__c, 2026-11-01)
GATEKEEPER: Could I speak to whoever owns the product side, please?
TRIGGER: None found: cold call on ICP fit only
```

### Example 5: Ambiguous role flag
The packet flags the role as ambiguous, so the opener asks who handles it instead of assuming 'product' means software.

Packet (fake):
```json
{
  "account_name": "Greenvale Dairy",
  "website": "greenvale-dairy.example.com",
  "location": "Hamilton, New Zealand",
  "employees": 450,
  "prospect_first_name": "Tane",
  "prospect_title": "Product Manager",
  "trigger": {
    "kind": "hiring_plan",
    "evidence": "Greenvale Dairy is hiring a Head of Digital.",
    "source": "zoominfo_scoop:FAKE-501",
    "date": "2026-11-09"
  },
  "available_facts": [
    {
      "text": "Greenvale Dairy is hiring a Head of Digital",
      "source": "zoominfo_scoop:FAKE-501",
      "date": "2026-11-09"
    },
    {
      "text": "Greenvale Dairy has a customer ordering portal",
      "source": "website:greenvale-dairy.example.com",
      "date": "2026-11-15"
    }
  ],
  "flags": [
    "ambiguous_role: 'product' may mean physical goods here"
  ],
  "must_not_claim": []
}
```

Output:
```text
OPENER: Hi Tane, Alex from Pendo. Greenvale is hiring a Head of Digital. Who handles that side of the business there?
FACT 1: Greenvale Dairy is hiring a Head of Digital (source: zoominfo_scoop:FAKE-501, 2026-11-09)
FACT 2: Greenvale Dairy has a customer ordering portal (source: website:greenvale-dairy.example.com, 2026-11-15)
GATEKEEPER: Who looks after your digital projects, please?
TRIGGER: Greenvale Dairy is hiring a Head of Digital (source: zoominfo_scoop:FAKE-501, 2026-11-09)
```

### Example 6: Only one fact
Only one verified fact exists, so FACT 2 is 'none verified'. Never pad it.

Packet (fake):
```json
{
  "account_name": "Cinder Payments",
  "website": "cinder-payments.example.com",
  "location": "Sydney, New South Wales",
  "employees": 220,
  "prospect_first_name": "Wei",
  "prospect_title": "Director of Engineering",
  "trigger": {
    "kind": "partnership",
    "evidence": "Cinder Payments announced a partnership with Orbis Bank.",
    "source": "zoominfo_scoop:FAKE-601",
    "date": "2026-10-29"
  },
  "available_facts": [
    {
      "text": "Cinder Payments announced a partnership with Orbis Bank",
      "source": "zoominfo_scoop:FAKE-601",
      "date": "2026-10-29"
    }
  ],
  "flags": [],
  "must_not_claim": []
}
```

Output:
```text
OPENER: Hi Wei, Alex from Pendo. Saw the Orbis Bank partnership. Does it mean a bigger load on your engineering team?
FACT 1: Cinder Payments announced a partnership with Orbis Bank (source: zoominfo_scoop:FAKE-601, 2026-10-29)
FACT 2: none verified
GATEKEEPER: Who runs engineering there, please?
TRIGGER: Cinder Payments announced a partnership with Orbis Bank (source: zoominfo_scoop:FAKE-601, 2026-10-29)
```

## Voice notes (Alex: edit)
- TODO Alex: 3 openers you like, 3 you hate, in `config/voice.yaml` under `alex_examples`. Until then the style above is
  a structural draft, not your voice. Do not invent his taste.
