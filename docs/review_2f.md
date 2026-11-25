# Review 2f: integration of the Phase 2 branches

Integrator: task 2f. Branch `phase2/integration`. Final suite: `python -m pytest tests/ -q -W error` -> **3250 passed**, 0 xfail introduced (main was 1990). Everything offline; synthetic data only.

## 1. What was merged
Base `origin/main` e9d478f. Owned files checked out from each branch, nothing else:

| Branch (tip) | Files taken |
|---|---|
| `fix-persona` 100eb9e | `src/ranker.py`, `tests/test_ranker.py`, `docs/ranker_notes.md` |
| `fix-sheet` 2858957 | `src/callsheet.py`, `tests/test_callsheet.py`, `docs/callsheet_notes.md` |
| `fix-dupes` 0325f91 | `src/accounts.py`, `tests/test_accounts.py`, `docs/accounts_notes.md` |
| `fix-triggers` **cbabb48** (the brief said 8e2abe2; 8e2abe2 is its parent and the tip adds the debt / fund / C-suite / rumour addendum, so the tip was taken) | `src/signals.py`, `tests/test_signals.py`, `docs/signals_notes.md` |
| `2a-fixes` ed52cfa | `src/intake.py`, `src/pipeline.py`, `scripts/*`, `docs/fetch_runbook.md`, `docs/intake_schema.md`, `tests/test_intake.py`, `test_merge_pages.py`, `test_pipeline.py`, `intake_helpers.py` |
| `2d-opener-lint` b2a5bf5 | `src/textcheck.py`, `config/voice.yaml`, `skills/*`, `docs/voice_guide.md`, `tests/test_textcheck.py`, `tests/test_textcheck_review.py`, `tests/fixtures/text_*.json`, and the four data files `docs/review_2d_{labels,heldout2,heldout2_packets,real_packets}.json` (the round-2 test reads `docs/review_2d_labels.json`) |
| `2e-dryrun-script` 1ee0542 | `scripts/dry_run_sheet.py`, `tests/test_dry_run_sheet.py`, the runbook section 11 |

**Not taken from 2d:** its `src/callsheet.py`, `tests/test_callsheet.py`, `tests/fixtures/callsheet_accounts.json`, `docs/callsheet_notes.md` additions: that branch is based on f83091c and contains an OLDER callsheet than main (no obfuscated-email scrubbing, no `dnc_is_rebuilt`); main's / fix-sheet's is newer and a diff of the trees would have silently reverted it.

### Conflicts resolved
1. `docs/fetch_runbook.md` (2a and 2e both edited section 6 and the parameter table): three-way merge from the main version; 2a's wording kept for the three conflicting hunks (it contains the derive-last-page rules and the `publishedStartDate` / `scoopTypes` parameters that 2e also states), 2e's non-overlapping section 11 kept. 2e's sentence that `--derive-last-page --paging zoominfo` "returns False" is superseded by 2a (it returns True for a short / empty last page) and was removed with that hunk.
2. `tests/test_integration_pipeline.py::test_l_no_network_or_process_imports`: 2a had `if path.name == "pipeline.py"`, 2d had the dict form. Kept 2d's `{"textcheck.py": {"argparse","sys"}, "pipeline.py": {"argparse","sys"}}.get(path.name, set())` (it satisfies both `test_textcheck_review::test_import_allowlist_scoped_to_textcheck` and the 2a intent), kept the writers assertion `["pipeline.py", "state.py"]`.
3. `tests/test_pipeline.py::test_narrow_allowlist_argparse_and_sys_only_in_pipeline` now allows `textcheck.py` too (the two CLI entry points).

## 2. Wiring done
(a) `pipeline.contact_names_by_account` builds SF account id -> casefolded 'first last' names from the SF Contacts and passes them to the second `build_account_list` call. Names are counted by accounts, never logged (test greps the log, sheet, packets for every surname).
(b) `callsheet.build_sheet` collapses by `accounts.duplicate_groups` (group notes / flags). Old note parser removed; tests rewritten to the new wording; the real notes from `build_account_list` are used in `test_integration_phase2.py::test_ii_*` (Catapult pair collapses at every limit 1..23, why_chosen "also on your list as Catapult"). Only runs when some account in the pool is flagged (no re-detection per sheet: 3 s at 12k accounts otherwise).
(c) `signals.signal_notes` -> `why_chosen` ("Flag: <note>" for every note of the best signal), `flags`, `PacketSignal.notes`, `verdict_reason`, `facts_note`, `must_not_claim`, and the quotable text (`evidence`, fact text) is stripped of ALL note suffixes (it used to strip only the role-ambiguous one: the other notes were nested in the text as "(note) (ZoomInfo scoop, ...)" and the opener could quote them). New `ResearchPacket.must_not_phrases` (`callsheet.NOTE_BANNED_PHRASES`) so textcheck rejects overstated triggers ("Congrats on the new app launch" for a lab-consumables launch). Real packets from real signals with notes are linted with good and bad blocks (`test_d_*`).
(d) textcheck reads the packet exactly as `ResearchPacket.to_dict()` emits it (test `test_d_packet_fields_are_read_as_callsheet_emits_them`: account_name, trigger kind, ambiguity via `ambiguity_flags` / `signals[].ambiguous`, status in ok / undated, `email_ok`, `email_reason`, no contact data). It does NOT read `email_ok`: see B6.
(e) ranker: the sheet builder does not enumerate needs_prospect codes (it prints whatever code the ranker gives), so `junk_title` already flows to the sheet (`test_i_junk_title_is_in_the_sheet_bucket_enumeration_and_log_counts`). Nothing to add in `callsheet.py`.
(f) ranker recency: `src/ranker.py:386` uses `sigmod.max_age_for(best.kind, config)`. Confirmed, nothing to fix.
(g) the pipeline calls `state.is_dnc_call` twice (`pipeline.py:167,186`; test asserts at least two call sites). The log now has a `== FLAG COUNTS ==` block with junk_title, personal_email and possible_duplicate counts (accounts and groups); the same numbers are in `PipelineResult.stats`.

## 3. Verified in the merged `signals.py` (my list from the brief)
All six present and working (`test_iii_*`, real `run_pipeline`): lab-consumables launch x0.5 with "physical or unclear product launch" note; "to be acquired by EQT" typed Funding -> M&A; two accounts on the same 30% stake show "same deal as <other>" (acquirer also "acquirer of a minority stake" x0.5); debt facility weight 0.25 note "debt facility, not an equity round" (strength 0.42x of an equity round); property fund launch 0.25 note "financial or property product launch, not a digital product"; CFO promoted to CEO at 86 days dropped ("neither the new nor the vacated role is a buyer role"); rumoured acquirer exactly 0.5 of a confirmed acquirer. **Nothing is missing, so no xfail repro exists.** The one gap is B3 below (a direction note that is not on the Signal).

## 4. Bugs and findings (module:line, minimal repro)
| # | Sev | Where | What | Status |
|---|---|---|---|---|
| B1 | **high for scale** | `src/accounts.py:640-650` (`_detect`, name buckets), `_names_match:565` | The name-containment candidate loop compares every pair inside one distinctive-word bucket: O(k^2) per word. Repro: `python -m tests.phase2_driver scale --n 3000` with every name sharing one word ("Scale Company 00001 Pty Ltd"): pipeline 51 s (1,500: 12 s; 12,000: 3 min 20 s, 3M `_names_match` calls per 1,500). With realistic names 12,000 accounts take 10 s. | **OPEN** (owner: 1d / fix-dupes). Suggested fix: skip or sub-index a bucket above ~200 members (require two shared tokens), or compare sorted core-token tuples. Not changed (accounts.py is not mine to rewrite). |
| B2 | medium | `src/ranker.py:432` (reason text) -> run log | The chosen prospect's FULL name went into `RankedAccount.reasons`, hence into the run log (and `ranker.summarise`), while the sheet is first-name-only. Repro: any run, `grep "Prospect: Morgan Sharedvp" logs/*.log`. | **FIXED** (first name only via `_log_name`; one existing test adjusted; `test_iv_the_personal_address_and_no_other_pii_appear_anywhere` greps every surname). |
| B3 | medium | `src/signals.py` (`scoops_to_signals`, verdict reason only) | "being acquired" (M&A target) and "role vacated" exist only on `ScoopVerdict.reason`; `Signal` carries notes for everything else. `signal_notes(signal)` is `[]` for the target of a full acquisition, so the sheet, packet and opener cannot tell target from acquirer. Repro: `test_c_target_direction_is_not_recoverable_from_signal_alone`. | **OPEN** (owner: 1e / fix-triggers; needs a note or field on Signal). The coordinator's list expected "being acquired" and "role vacated" in why_chosen: they cannot be shown from the Signal alone. |
| B4 | medium | `scripts/merge_pages.py:main` (write of `<raw-dir>/<name>.json`), `scripts/dry_run_sheet.py:main` | Both wrote real data to ANY path given (`--raw-dir docs/` would have put a raw dump in a committable folder); only `gmail_queue` and `recent_call_ids` used safeout. | **FIXED**: both go through `scripts/safeout.py` (exit 2 on refusal, mode 0600 for the sheet). `tests/conftest.py` sets `APAC_OUTPUT_ROOT` to pytest's temp tree for the session; one pipeline test moved its raw dir under `output/`. |
| B5 | low | `src/callsheet.py` `_clean_evidence` (as merged from fix-sheet) | Stripped only the role-ambiguous suffix; the 6 new fix-triggers notes stayed in the quotable evidence and fact text. | **FIXED** (see 2c). |
| B6 | low | `src/textcheck.py` (no reader of `email_ok` / `email_reason`) | A personal-email contact's packet still lints clean for an EMAIL DRAFT: refusing is `callsheet.email_draft_allowed`'s job only. | **OPEN** by design; Phase 3 precondition 7. `test_d_email_draft_for_a_personal_email_packet_still_lints_as_text_only`. |
| B7 | low | `src/textcheck.py` vs notes | Before the wiring, "Congrats on the new app launch" (physical launch), "Congrats on the funding round" (debt facility), "Congrats on acquiring X" (rumour) all linted clean. | **FIXED for the five noted kinds** via `must_not_phrases`; paraphrases still pass (limitation 2 in the report). |
| B8 | info | `tests/test_integration_pipeline.py` `Builder.account` | Every synthetic account got `www.fakeN.example.com`, whose registrable domain is `example.com`: 19 accounts become `large_domain_group` and any account with an explicit `.example.com` domain joins the same duplicate group (a first pass of my scenario dropped Bank Australia and Arab Bank "as duplicates of Catapult" for that reason). Not a product bug; the phase 2 world uses distinct `.com.au` domains. | noted |
| B9 | info | `src/accounts.py` group notes | Duplicate groups are connected components: a single wrong edge (domain or id) joins two groups into one, and then the sheet collapses them all. Capped at 6 members; `large_*_group` accounts are not collapsed. | noted |

## 5. Things that did not fit / not done
- 2b (signals pull wiring of ZoomInfo intent) is deferred by decision; nothing built.
- The voice calibration (`alex_examples`) needs Alex.
- `docs/review_2d.md` and the other review notes (`2a-review`, `2c-review`, `probe-notes`) were NOT merged (reference only, as instructed).
- The 12,000-account run uses synthetic names; real-name bucket sizes are unknown until a real dry run reports `accounts._detect` time (add a timing line to the log if B1 is not fixed).
- `tests/test_integration_phase2_gates.py::test_vi_*` runs the 20-shuffle x 3-hash-seed determinism check in subprocesses (about 25 s total): digest of sheet text + markdown + JSON + every packet + the full run log is identical across all 60 runs.

## 6. Hard-rule scan results (tests `test_viii_*`)
- No call of any send / draft / create / update / delete tool name in `src/` or `scripts/` (AST).
- The only writers in `src/`: `state.save_state` / `state._set_aside` and `pipeline.write_log`. Scripts that write: `merge_pages`, `dry_run_sheet`, `gmail_queue`, `recent_call_ids`, all via `safeout` (under `output/`, git-ignored, no symlinks).
- No network imports anywhere; the only process import is `subprocess` in `scripts/safeout.py` (fixed argv `git check-ignore`).
- No email address (including the fake gmail address), phone number or surname from the world in the run log, log file, sheet text / markdown / JSON, any packet or packet text.
