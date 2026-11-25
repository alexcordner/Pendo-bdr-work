---
name: email-draft
description: Write a short "just tried you" follow-up email draft in Alex's voice after a no-answer or voicemail, from a ResearchPacket. Drafts only, NEVER send. Checked by python -m src.textcheck.
---

# Email-draft skill (v2: structure final, voice NOT yet calibrated to Alex)

Output is a Gmail **draft** only. **NEVER send.** Never call Gmail `send_message`, `reply` or `forward`, Nooks
`sendTaskEmail` or `createEmail`, or any Salesforce or Nooks note tool. Writing the text needs no connector at all: the
packet is the only input. A separate step, only with `--live`, creates the draft with Gmail `create_draft`.

**The lint passing is necessary, not sufficient.** It cannot catch a true-sounding sentence that changes the meaning;
Alex reads every draft before anything leaves.

## Before you write
1. Read `config/voice.yaml`. If `calibrated: false`, the **first line of the body** is exactly
   `DRAFT STYLE: not yet calibrated to Alex` (Alex deletes it when he reviews the draft). If true, omit it and use
   `alex_examples.proud_emails` as the style reference.
2. Skip, and log why, instead of drafting, if: no email address on file, do-not-contact or unsubscribed, or emailed in the
   last 7 days. The caller checks those. Never put the contact's address in the text.

## Output: exactly this shape, plain text
Template when `calibrated: false`:
```text
SUBJECT: <2 to 5 words>
BODY:
DRAFT STYLE: not yet calibrated to Alex
Hi <first name>,
Just tried you. <one clause: the real trigger in the packet's words, or an honest no-news line>
<one more short line>
<one ask: a single question>
Alex
```
Template when `calibrated: true`: the same without the label line. `parse_email_block()` in `src/textcheck.py` splits
this into subject and body.

## Rules
- Subject 2 to 5 words, lowercase-ish, specific, one line, no "Re:" or "Fwd:" (it fakes a thread), no emoji.
- Body under 80 words in total, 3 to 5 content lines (greeting and sign-off not counted), first content line starts with
  `Just tried you`, last line is exactly `Alex`. No bullets, no numbered lists.
- **One ask only**: one question. A second question, "or should I...", "let me know" or "can you" alongside the question
  counts as a second ask and fails.
- Reference only the packet trigger (`has_real_trigger` true; restate its `evidence` in its own words). An `intent`
  signal is not a trigger. With no real trigger write an honest no-news line ("I have no news hook, Acme looks like a
  fit"). No pretend reason, no "saw", "noticed", "congrats".
- Only facts, names, numbers, currencies, funding rounds and places that are in the packet. No customer names, no
  results, no "companies like yours", no "we help/make/improve ...". No unverifiable generalisation about "teams in
  their position": ask a question instead.
- `ambiguity_flags` not empty: do not assume the contact's role (no title, "your role", "your team"). Ask who handles it.
- `prospect_first_name` null: "Hi there,". Never guess a name.
- `contact_history`: you may say you "tried" them (that is the draft's premise). Do not claim a voicemail, a chat or a
  date unless `contact_history` says so; when in doubt leave it out.
- No fluff openers, no "I hope this finds you well", no attachments, no links, no email addresses or phone numbers.
- No "quick", "touch base", "circle back", "synergy", no emojis, no exclamation marks, no send language ("sending this
  now", "I've sent", "invite"): it is a draft.

## How to run the lint
**Run the lint from the repo root** (the folder that contains `src/` and `config/`); from anywhere else it fails with
`No module named src`. Save the packet as `packet.json` and the draft (in the SUBJECT / BODY shape above) as
`draft.txt`, then run:
```text
python -m src.textcheck email --packet packet.json --text draft.txt --voice config/voice.yaml
```
Exit code 0 = no errors, 1 = errors (printed one per line), 2 = could not run (stop and report; do not skip the lint).
`--show-diff` also lists the words that are not in the packet.

### The NOT_IN_PACKET warning
`WARNING NOT_IN_PACKET: <words>` lists content words in the subject and body that the packet does not contain. It never
blocks, but Alex sees it. Rewrite the line using only packet words if a listed word is a claim (an adjective, a verb
about what they did, a number); accept it only when every word is clearly scaffolding.

## Retry rule
Regenerate **at most 3 times**, changing only what the violations name. If errors remain after the third try, output
exactly `NEEDS HUMAN: <the error codes, comma separated>` and **do not create the draft**. Never edit the lint, the
config or the packet to make a draft pass.

## Worked examples
**FAKE: invented companies and people. Do not copy the wording: structure only.** Shown without the DRAFT STYLE line;
add it as the first body line while `calibrated` is false. A test lints each example against its packet.

### Example 1: Larkspur Insurance
New leader. Names the trigger in the packet's words, one question.

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

Draft:
```text
SUBJECT: tried you re Larkspur
BODY:
Hi Priya,
Just tried you. Larkspur Insurance has a new Chief Digital Officer.
I'm Alex at Pendo.
Who owns the digital product side at Larkspur now?
Alex
```

### Example 2: Marlow Software Group
No trigger: the honest line says so. No event words, no 'saw', no invented reason.

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

Draft:
```text
SUBJECT: tried you today
BODY:
Hi Ellie,
Just tried you. I'm Alex at Pendo.
I have no news hook, Marlow just looks like a fit.
Who owns product there?
Alex
```

### Example 3: Bellbird Fitness
Funding. The amount appears because it is in the packet, with its currency.

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

Draft:
```text
SUBJECT: tried you re Series B
BODY:
Hi Jonas,
Just tried you. Bellbird Fitness raised NZ$18 million in a Series B round.
I'm Alex at Pendo.
Who owns product there now?
Alex
```

### Example 4: Tidewater Credit Union
Product launch. One ask, phrased as a question.

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

Draft:
```text
SUBJECT: tried you re Tidewater app
BODY:
Hi Marcus,
Just tried you. Tidewater Credit Union launched a new mobile banking app for members.
I'm Alex at Pendo.
How are the first weeks going?
Alex
```

### Example 5: Greenvale Dairy
Ambiguous role flag: asks who handles it, never assumes the contact's role.

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

Draft:
```text
SUBJECT: tried you re Greenvale
BODY:
Hi Tane,
Just tried you. Greenvale Dairy is hiring a Head of Digital.
I'm Alex at Pendo.
Who handles that side of the business there?
Alex
```

### Example 6: Echo Logistics Pty Ltd
No first name: 'Hi there'.

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

Draft:
```text
SUBJECT: tried you today
BODY:
Hi there,
Just tried you. I'm Alex at Pendo.
I have no news hook, Echo looks like a fit.
Who looks after digital product there?
Alex
```

## Voice notes (Alex: edit)
- TODO Alex: 2 emails you are proud of, in `config/voice.yaml` under `alex_examples.proud_emails`. Do not invent his taste.
