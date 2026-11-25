# apac-callsheet

Personal BDR automation for Alex Cordner (Pendo, APAC). Builds tomorrow's dial list as Nooks call tasks,
drafts "just tried you" emails after no-answers, and watches ANZ accounts for triggers.

**Safety model:** drafts only, Salesforce read-only, dry-run by default. See `CLAUDE.md`.

## Layout
| Path | Purpose |
|---|---|
| `config/config.yaml` | territory, size cap, tiers, personas, call windows, limits |
| `config/icp.md` | plain-English ICP (you edit) |
| `skills/opener`, `skills/email-draft` | voice and format rules for the LLM-written text |
| `src/` | accounts, signals, ranker, callsheet, nooks_writer, drafts, state (built in Phase 1+) |
| `state/` | JSON memory: who was called, when, outcome, cooldowns |
| `logs/` | one plain-text log per run |
| `tests/` | unit tests with fixtures |
| `scripts/run_nightly.sh` | scheduler entry point (Phase 4) |

## Connectors (checked in Phase 0)
Present in this session: Nooks, ZoomInfo, Gmail, Salesforce (Prod), Gong, Granola, Canary, Google Calendar.
Nooks identity verified (`getMe` returned your user ID). Nooks workspace has Dialer and Sequencing,
but **Conversation Intelligence is off**, which may limit call transcripts in Phase 5.

## How to run (filled in as phases land)
- Tests: `python -m pytest tests/` (Phase 1)
- Dry run: `python -m src.callsheet --dry-run --limit 10` (Phase 2)
- Live: add `--live` (Phase 3). Nightly: `scripts/run_nightly.sh` (Phase 4)

## Troubleshooting
- Connector missing in a scheduled run: the run logs it and skips that step; it never fakes data.
- ZoomInfo noise (locations, non-companies): see `filters` in config and `src/accounts.py`.
- Check `logs/` first; every run says what it created, skipped and why.
