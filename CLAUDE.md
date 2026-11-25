# APAC Call Sheet: project rules

Personal BDR automation for Alex Cordner (Pendo, APAC / mainly ANZ). Nooks owner ID: `5koD9szETWeI1F0DcRTqkoHGPlY2`.

## Hard rules (every session, every scheduled run)
1. **DRAFTS ONLY.** Never send an email, LinkedIn message or SMS. Use Gmail `create_draft` only.
   Never call Gmail `send_message`, `reply` or `forward`, or Nooks `sendTaskEmail`.
   Do not call Nooks `createEmail` with `send: true`. Prefer Gmail drafts.
2. **Never delete or overwrite existing data.** You may create Nooks call tasks and Gmail drafts.
   You may delete only test records you created yourself in this run or session (and say so in the log).
3. **Salesforce is READ-ONLY.** Use `soqlQuery`, `find`, `getObjectSchema` and similar reads only.
   Never call create/update/delete Salesforce tools. Nooks `createAccountNote`/`createProspectNote` write to the CRM, so they are also banned.
4. **No secrets in the repo.** Use env vars or the existing connector auth.
5. **Every run writes a plain-text log** to `logs/YYYY-MM-DD_HHMM_<run>.log`: what was created, skipped, and why.
6. **Do not fake data.** If a connector is missing or a call fails, log it and stop that step. If there is no real trigger for an account, say so. Never invent one.
7. **Count ZoomInfo credits.** Search is free. Redeem, enrich and `account_research` cost credits, so avoid them unless needed and log every credit spent.

## Design rules
- Scoring and ranking are plain Python (`src/ranker.py`). The LLM writes text only (openers, facts, emails).
- Python 3.11+, small testable modules, no frameworks.
- Tests use fixtures in `tests/fixtures/`, never live data.
- Filter ZoomInfo noise: drop locations (e.g. "Sydney CBD") and non-company records.
- Filter scoops to Pendo-buyer topics only. Ignore generic "more open roles in Medical & Health / Legal" notes.
- Call windows are local to each prospect (AEST/AEDT, NZST/NZDT, SGT etc.).
- Dry-run by default. Writes to Nooks and Gmail need an explicit `--live` flag.

## Voice
Write as Alex: short, specific, no fluff, no hype, one ask. See `skills/opener/SKILL.md` and `skills/email-draft/SKILL.md`.
