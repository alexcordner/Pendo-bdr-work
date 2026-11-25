---
name: email-draft
description: Write a short "just tried you" follow-up email draft in Alex's voice after a no-answer or voicemail, from a ResearchPacket. Drafts only, never send. Checked by src/textcheck.py.
---

# Email-draft skill (v1, structure final, voice NOT yet calibrated to Alex)

Output is a Gmail **draft** only. **NEVER send.** Create it with Gmail `create_draft` and nothing else. Never call
`send_message`, `reply`, `forward` or Nooks `sendTaskEmail`. Writing the draft text never needs a connector at all:
the packet is the only input.

## Before you write
1. Read `config/voice.yaml`. If `calibrated: false`, the **first line of the body** must be exactly
   `DRAFT STYLE: not yet calibrated to Alex` (Alex deletes it when reviewing the draft). If true, omit it and use
   `alex_examples.proud_emails` as the style reference.
2. Skip, and log why, instead of drafting, if: no email address on file, do-not-contact / unsubscribed, or emailed in the
   last 7 days. Those checks belong to the caller; never use the contact's address inside the text.

## Output: exactly this shape, plain text
```
SUBJECT: <2 to 5 words>
BODY:
Hi <first name>,
Just tried you. <one clause: why, the real trigger, or an honest role-based line>
<one sentence on why that matters for someone in this kind of role>
<one ask: a single question>
Alex
```
`parse_email_block()` in `src/textcheck.py` splits this into subject and body.

## Rules
- Subject 2 to 5 words, lowercase-ish, specific, not clickbait. One line. No "re:" tricks that fake a thread.
- Body under 80 words in total, 3 to 5 content lines (greeting and sign-off not counted), first content line starts
  with `Just tried you`.
- **One ask only**: one question, no second question, no "or let me know". No 15-minute slot unless the packet supports
  it (a number must appear in the packet).
- Reference only the packet trigger (its evidence). If there is none, no pretend trigger: use an honest role-based line
  ("I'm calling engineering managers at software companies in Perth", "no specific reason for today").
- Only facts, names, numbers and places that are in the packet. No customer names, no results, no "companies like yours".
- With an ambiguous-role flag do not assume the contact's role.
- No fluff openers, no "I hope this finds you well", no attachments, no links, no email addresses or phone numbers.
- No "quick", "touch base", "circle back", "synergy", no emojis, no exclamation marks, no send language ("sending this
  now"): it is a draft.

## After you write
Run `check_email_draft(subject, body, packet, voice_cfg)`. Fix every `error` before creating the draft. Create it with
Gmail `create_draft` only, and only when the caller ran with `--live`.

## Worked examples
**FAKE. Invented companies and people. Not real data. Shown without the DRAFT STYLE label; add it first while
`calibrated` is false.** A test lints each example against its packet.

### Example 1: New leader

Packet: the fake `exec_move` packet from the opener skill (`tests/fixtures/text_packets.json`).

```text
SUBJECT: tried you re Larkspur
BODY:
Hi Priya,
Just tried you. Larkspur Insurance appointed a new Chief Digital Officer.
A change at that level often puts the customer app under a closer look.
Who is the best person to talk to about it?
Alex
```

### Example 2: No trigger: honest role-based line

Packet: the fake `none_cold` packet from the opener skill (`tests/fixtures/text_packets.json`).

```text
SUBJECT: tried you today
BODY:
Hi Ellie,
Just tried you. I'm Alex at Pendo, and I'm calling engineering managers at software companies in Perth.
I don't have a specific reason for today. Marlow just looks like a fit for what we do.
Would you be open to a short chat this week or next?
Alex
```

### Example 3: Funding

Packet: the fake `funding` packet from the opener skill (`tests/fixtures/text_packets.json`).

```text
SUBJECT: tried you re Series B
BODY:
Hi Jonas,
Just tried you. Bellbird Fitness raised NZ$18 million in a Series B round.
A raise like that often makes the product roadmap busier.
Who should I speak to about how Bellbird learns how people use the product?
Alex
```

### Example 4: Product launch

Packet: the fake `product_launch` packet from the opener skill (`tests/fixtures/text_packets.json`).

```text
SUBJECT: tried you re Tidewater app
BODY:
Hi Marcus,
Just tried you. Tidewater Credit Union launched a new mobile banking app for members.
The weeks after a launch are when teams learn where members get stuck.
Could we find time next week to compare notes?
Alex
```

## Voice notes (Alex: edit)
- TODO Alex: 2 emails you are proud of, in `config/voice.yaml` under `alex_examples.proud_emails`. Do not invent his taste.
