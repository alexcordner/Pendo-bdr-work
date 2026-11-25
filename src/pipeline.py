"""Pipeline (task 2a): the whole Phase 1 engine run over the raw dump in output/raw/.

    python -m src.pipeline --raw-dir output/raw --state-dir state [--now ISO] [--limit N] [--dry-run]

Steps, in the order the Phase 1 contract requires (docs/phase1_report.md "Phase 2 risks"; the same
wiring as ``run_pipeline`` in tests/test_integration_pipeline.py, minus the synthetic world):

  1. intake.load_raw            validate the files; errors stop the run
  2. verify_dispositions        stop if a configured disposition name is missing
  3. accounts, first pass       countries and timezones (no cooldowns yet) for prospects and calls
  4. state                      record_calls over the FULL call history, then mark_dnc_rebuilt
                                (only when the last page was reached), sent emails, save, reload
  5. require_healthy(live=...)  a live run needs a trusted Do Not Call list
  6. mark_do_not_call           on the prospects, right before ranking
  7. accounts, second pass      cooldown_until + call_date from state
  8. signals                    filter_scoops, scoops_to_signals (group_by_zoominfo_id)
  9. ranker.rank_accounts
 10. record_sheet               ONLY when live, and only for ``ranked``

This module never calls a connector and never writes to Nooks, Gmail or Salesforce. A dry run (the
default) writes only the local state file (memory of calls and emails, gitignored) and the run log.
``--live`` is a gate, not a writer: Phase 3 (nooks_writer) owns writing tasks.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from src import accounts as acc_mod
from src import ranker, signals as sig_mod, state as st_mod
from src.config import Config, load_config, next_working_windows
from src.intake import (IntakeReport, RawData, build_prospects, derive_nooks_sf_links, infer_prospect_ids_from_calls,
                        last_sheet_from_tasks, load_raw, nooks_account_rows, scrub)
from src.loaders import parse_utc
from src.models import Prospect, Signal

SYDNEY = ZoneInfo("Australia/Sydney")
ROOT = Path(__file__).resolve().parent.parent
_E164 = re.compile(r"^\+\d{8,15}$")
# Last-line defence for the run log: emails, E.164 numbers, AU national numbers. Names are fine.
_LOG_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_LOG_E164 = re.compile(r"\+\d[\d ]{7,}\d")
SURROGATE = "sfc:"        # internal state key for a Contact with no Nooks prospect id (never leaves this module)
DNC_NOTE = ("DNC visibility: Nooks-dialer calls only (Outreach-sourced calls have no Do Not Call disposition); calls "
            "fetched with the owner filter; dry-run DNC match is by phone/email hash, Nooks prospect id after lazy "
            "resolution in Phase 3. Phase 3 must RE-RUN the Do Not Call / opt-out check by Nooks prospect id before "
            "writing any task.")
MIN_FIRST_RUN_CALLS = 5   # a first-ever run with fewer calls than this is suspicious (truncated history?)
_LOG_NATIONAL = re.compile(r"(?<![\w])0[2-478][\d ]{8,10}(?![\w])")


class PipelineStopped(RuntimeError):
    """The run cannot continue (intake errors, missing dispositions, wrong owner ...). ``reasons`` says why;
    ``log_lines`` is the log written up to that point."""

    def __init__(self, reasons: list[str], log_lines: list[str] | None = None):
        super().__init__("; ".join(reasons))
        self.reasons = reasons
        self.log_lines = log_lines or []


class LiveRefused(PipelineStopped):
    """--live / live=True was requested but a precondition does not hold. Nothing was written to any
    connector (a dry run is always possible instead)."""


@dataclass
class PipelineResult:
    accounts_result: "acc_mod.AccountListResult"
    verdicts: list                                   # signals.ScoopVerdict, one per scoop
    signals: dict[str, list[Signal]]                 # account key (sf id, else zoominfo id) -> signals
    rank_result: "ranker.RankResult"
    prospects: list[Prospect]                        # every prospect, do_not_call applied from state
    state: "st_mod.State"                            # final state (sheet recorded only when live)
    intake_report: IntakeReport
    log_lines: list[str]
    stats: dict[str, int]
    zoominfo_credits: int | None                     # credits spent by the fetch step (None = unknown)
    # extras
    now: datetime | None = None
    run_date: date | None = None
    call_date: date | None = None
    live: bool = False
    sheet_recorded: bool = False
    log_path: Path | None = None
    unmatched_scoops: list = field(default_factory=list)


# ------------------------------------------------------------------ log helpers
def scrub_log(text: str) -> str:
    return _LOG_NATIONAL.sub("<number>", _LOG_E164.sub("<number>", _LOG_EMAIL.sub("<email>", text)))


def write_log(lines: list[str], log_dir, now: datetime, run: str = "pipeline") -> Path:
    """logs/YYYY-MM-DD_HHMM_<run>.log (Sydney local time). Never overwrites: a same-minute rerun gets -2, -3."""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{now.astimezone(SYDNEY):%Y-%m-%d_%H%M}_{run}"
    path = log_dir / f"{stem}.log"
    n = 1
    while path.exists():
        n += 1
        path = log_dir / f"{stem}-{n}.log"
    path.write_text(scrub_log("\n".join(lines)) + "\n", encoding="utf-8")
    return path


def _surrogate(prospects: list[Prospect]) -> list[Prospect]:
    """Give Contacts without a Nooks id a stable internal key so state can link phone-matched calls to them."""
    return [replace(p, nooks_id=SURROGATE + p.sf_contact_id) if not p.nooks_id and p.sf_contact_id else p
            for p in prospects]


def _restore(prospects: list[Prospect]) -> list[Prospect]:
    return [replace(p, nooks_id=None) if p.nooks_id and p.nooks_id.startswith(SURROGATE) else p for p in prospects]


def _to_stats(raw: RawData) -> tuple[int, int]:
    """(calls whose `to` is the literal 'external', calls whose `to` is otherwise not E.164)."""
    ext = other = 0
    for r in raw.nooks_calls:
        t = r.get("to")
        if isinstance(t, str) and _E164.match(t):
            continue
        if t == "external":
            ext += 1
        else:
            other += 1
    return ext, other


def _first_call_date(cfg: Config, now: datetime) -> date:
    """The first date a task could be due (Sydney, first usable window), as the Phase 1 harness does."""
    earliest = now.replace(second=0, microsecond=0) + timedelta(minutes=2)
    wins = next_working_windows(cfg, "Australia/Sydney", earliest)
    return wins[0].start.date() if wins else now.astimezone(SYDNEY).date()


def _dnc_phone_issues(raw: RawData, cfg: Config) -> tuple[int, list[str]]:
    """Do Not Call calls whose ``to`` is not already E.164 (backlog item). Returns (count, log lines).
    Only call ids are logged, never numbers."""
    raw_to = {r.get("id"): r.get("to") for r in raw.nooks_calls}
    lines, n = [], 0
    for c in raw.calls:
        if not st_mod._is_dnc(c, cfg):          # same name test state.py uses for the Do Not Call list
            continue
        t = raw_to.get(c.nooks_id)
        if c.to_e164 is None:
            n += 1
            lines.append(f"  DNC call {c.nooks_id}: 'to' could not be normalised: phone block not recorded")
        elif not (isinstance(t, str) and _E164.match(t)):
            n += 1
            lines.append(f"  DNC call {c.nooks_id}: 'to' was not E.164 in the source (normalised for the block)")
    return n, lines


def _list_dropped(res: "acc_mod.AccountListResult", bulk_limit: int = 25) -> list[str]:
    """Every dropped account by name with its reason (rule 5). The country filter drops thousands of
    non-ANZ accounts by design: that reason is counted, with the first ``bulk_limit`` names."""
    lines: list[str] = []
    by_reason: dict[str, list] = {}
    for d in res.dropped:
        by_reason.setdefault(d.reason, []).append(d)
    for reason in sorted(by_reason):
        items = by_reason[reason]
        shown = items[:bulk_limit] if reason == acc_mod.R_NOT_ANZ else items
        lines.append(f"  dropped - {reason} ({len(items)}):")
        for d in shown:
            lines.append(f"    - {d.name or '(no name)'}: {d.detail or d.reason} [{d.source}]")
        if len(shown) < len(items):
            lines.append(f"    ... and {len(items) - len(shown)} more (counted, names not listed)")
    return lines


def _list_flagged(res: "acc_mod.AccountListResult") -> list[str]:
    lines: list[str] = []
    for a in res.accounts:
        if a.flags or a.confidence == "low":
            lines.append(f"  flagged - {a.name}: {', '.join(sorted(a.flags)) or 'low confidence'}"
                         f"{' (low confidence)' if a.confidence == 'low' else ''}")
    return sorted(lines)


# ------------------------------------------------------------------ the run
def run_pipeline(raw_dir, config: Config, state_dir, now: datetime, *, live: bool = False,
                 limit: int | None = None, log_dir=None, save_state: bool = True,
                 record_sheet: bool | None = None) -> PipelineResult:
    """Run the whole pipeline over ``raw_dir``. Pure of connectors.

    live=False (default): dry run. The sheet history is never recorded. live=True: every precondition must
    hold or ``LiveRefused`` is raised (intake blockers, last page of listCalls reached, trusted Do Not Call
    list); only then is ``record_sheet`` run, for ``ranked`` only, unless ``record_sheet=False`` (the CLI does
    that: tasks are Phase 3's job and the sheet history must describe tasks actually written).
    ``limit`` lowers the daily cap for this run (it can never raise it). ``log_dir``: write the run log there
    (also when the run stops). ``save_state=False`` keeps the state file untouched (read-only run)."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
        raise ValueError("limit must be a positive integer")
    if record_sheet and not live:
        raise ValueError("a dry run never records a sheet")
    do_sheet = live if record_sheet is None else bool(record_sheet and live)
    cfg = config
    if limit is not None:
        cfg = replace(cfg, limits=replace(cfg.limits, daily_call_cap=min(limit, cfg.limits.daily_call_cap)))
    run_date = now.astimezone(SYDNEY).date()
    mode = "LIVE (gate only, Phase 3 writes tasks)" if live else "DRY RUN"
    log: list[str] = [f"RUN LOG pipeline  now={now.isoformat()}  run_date={run_date}  mode={mode}  "
                      f"limit={limit if limit is not None else 'config cap ' + str(cfg.limits.daily_call_cap)}",
                      "Nothing is sent or written to Nooks, Gmail or Salesforce by this run. Salesforce is read-only.",
                      "Real data never enters git: output/, state/ and logs/ are gitignored.", DNC_NOTE, ""]

    def stop(reasons: list[str], cls=PipelineStopped):
        log.append("")
        log.append("== STOPPED ==")
        log.extend(f"  {r}" for r in reasons)
        if log_dir:
            write_log(log, log_dir, now)
        raise cls(reasons, list(log))

    # 1. intake -----------------------------------------------------------------------------
    raw = load_raw(raw_dir, now=now)
    rep = raw.report
    log += ["== INTAKE =="] + rep.lines()
    owner_in_meta = (raw.run_meta or {}).get("nooks_user_id")
    if owner_in_meta and owner_in_meta != cfg.owner.nooks_user_id:
        rep.error("run_meta.nooks_user_id does not match config owner.nooks_user_id: the raw data is for a "
                  "different Nooks user")
        rep.block_live("raw data belongs to a different Nooks owner")
        log.append("ERROR: run_meta.nooks_user_id does not match config owner.nooks_user_id")
    if not rep.ok:
        stop(["intake errors: " + e for e in rep.errors])
    budget = cfg.limits.zoominfo_credit_budget_per_run
    if rep.zoominfo_credits_used and rep.zoominfo_credits_used > budget:
        rep.warn(f"ZoomInfo credits used ({rep.zoominfo_credits_used}) exceed the per-run budget ({budget})")
        rep.block_live("ZoomInfo credit budget exceeded")
        log.append(f"WARNING: ZoomInfo credits used ({rep.zoominfo_credits_used}) exceed the per-run budget ({budget})")
    log += ["", f"ZoomInfo credits used this run (rule 7): "
                f"{'UNKNOWN' if rep.zoominfo_credits_used is None else rep.zoominfo_credits_used} "
                f"(budget {budget})"]
    if live and rep.live_blockers:
        stop(["live refused: " + b for b in rep.live_blockers], LiveRefused)

    # 2. dispositions -----------------------------------------------------------------------
    missing = st_mod.verify_dispositions(cfg, raw.disposition_index)
    if missing:
        stop([f"configured disposition name(s) missing from listCallDispositions: {missing}"])

    # 3. accounts, first pass -----------------------------------------------------------------
    n_warn, old_stats = len(rep.warnings), dict(rep.stats)      # joins add stats / warnings after intake was logged
    links = derive_nooks_sf_links(raw)
    nooks_rows = nooks_account_rows(raw, links)
    first = acc_mod.build_account_list(raw.sf_accounts, nooks_rows, [], cfg, {}, run_date)
    by_sf = {a.sf_account_id: a for a in first.accounts if a.sf_account_id}
    by_nooks = {a.nooks_account_id: a for a in first.accounts if a.nooks_account_id}
    prospects = infer_prospect_ids_from_calls(raw, build_prospects(raw, by_sf, by_nooks))
    task_last_ids = last_sheet_from_tasks(raw, prospects, run_date)
    rep.stats["last_sheet_accounts_from_tasks"] = len(task_last_ids)
    email_window = rep.stats.get("gmail_window_days")
    if email_window is not None and email_window < cfg.limits.email_cooldown_days:
        rep.warn(f"gmail search window ({email_window}d) is shorter than email_cooldown_days "
                 f"({cfg.limits.email_cooldown_days}d): older sent mail is invisible")
        rep.block_live("sent-email search window shorter than the email cooldown")
    log += ["", "== JOINS (prospects, contacts, Nooks to Salesforce links) =="]
    log += [f"  {k}: {v}" for k, v in sorted(rep.stats.items()) if old_stats.get(k) != v]
    log += [f"WARNING: {w}" for w in rep.warnings[n_warn:]]

    # 4. state -------------------------------------------------------------------------------
    st = st_mod.load_state(state_dir)
    log += ["", "== STATE ==", f"state loaded: new={st.is_new} reset={st.was_reset} reason={st.reset_reason} "
                               f"dnc_rebuilt_at={st.dnc_rebuilt_at}"]
    log += [f"  state warning: {scrub(w)}" for w in st.load_warnings[:25]]
    if len(st.load_warnings) > 25:
        log.append(f"  ... and {len(st.load_warnings) - 25} more state warnings")
    keyed = _surrogate(prospects)
    st = st_mod.record_calls(st, raw.calls, keyed, by_sf, config=cfg)
    calls_seen = st.report["record_calls"]["calls_seen"]
    rebuilt_now = False
    if not rep.calls_reached_last_page:
        log.append("WARNING: listCalls paging did NOT reach the last page: Do Not Call is NOT rebuilt this run "
                   "and a live run is impossible")
    elif calls_seen == 0:
        if (raw.run_meta or {}).get("empty_history_confirmed") is True:
            st = st_mod.mark_dnc_rebuilt(st, run_date, 0, reached_last_page=True, empty_history_confirmed=True)
            rebuilt_now = True
            log.append("NOTE: empty call history confirmed in run_meta (brand-new Nooks seat)")
        else:
            log.append("WARNING: no calls in the history: Do Not Call cannot be marked rebuilt from an empty history")
            rep.block_live("no call history")
    else:
        try:
            st = st_mod.mark_dnc_rebuilt(st, run_date, calls_seen, reached_last_page=True)
            rebuilt_now = True
        except ValueError as e:
            log.append(f"WARNING: Do Not Call rebuild rejected: {scrub(str(e))}")
            rep.block_live(f"Do Not Call rebuild rejected: {e}")
    if st.is_new and 0 < len(raw.calls) < MIN_FIRST_RUN_CALLS and \
            (raw.run_meta or {}).get("empty_history_confirmed") is not True:
        log.append(f"WARNING: only {len(raw.calls)} calls on a run with no earlier state: the history may be truncated")
        rep.block_live(f"call history is suspiciously small ({len(raw.calls)} calls) and there is no earlier state; "
                       "set run_meta.empty_history_confirmed=true if this seat really is new")
    if st.was_reset and rep.stats.get("tagged_tasks", 0) == 0:
        log.append("WARNING: state.json was reset and there are no tagged tasks to recover the last sheet from")
        rep.block_live("state was reset and the last sheet cannot be recovered from tagged tasks")
    n_bad_dnc, dnc_lines = _dnc_phone_issues(raw, cfg)
    log.append(f"Do Not Call calls whose 'to' is not E.164 in the source: {n_bad_dnc}")
    log += dnc_lines
    # Search results carry no dates: a hit means "sent inside the query window". The undated rule in state.py then
    # blocks until first_seen + email_cooldown_days; first_seen is the day the SEARCH ran (the window's anchor), so
    # the block lasts exactly until the window could have elapsed.
    gm = raw.metas.get("gmail_sent") or {}
    gm_stamp = parse_utc(gm.get("fetched_at"))
    email_today = gm_stamp.astimezone(SYDNEY).date() if gm_stamp else run_date
    st = st_mod.record_sent_emails(st, raw.sent_emails, keyed, by_sf, today=email_today)
    reports = dict(st.report)                  # `report` is not persisted: keep it for the log
    log.append("record_calls: " + _json(reports.get("record_calls", {})))
    log.append("record_sent_emails: " + _json(reports.get("record_sent_emails", {})))
    log.append(f"Do Not Call rebuilt from full history this run: {rebuilt_now}")
    if save_state:
        st_mod.save_state(st, state_dir)       # raises StateShrinkError rather than losing memory
        st = st_mod.load_state(state_dir)      # use the reloaded state: proves the round trip
        st.report.update(reports)
        log.append("state saved (local file, gitignored): yes")
    else:
        log.append("state saved: NO (save_state=False)")
    try:
        st_mod.require_healthy(st, live=live)
    except st_mod.StateResetError as e:
        stop([f"live refused: {e}"], LiveRefused)
    if not st_mod.dnc_is_rebuilt(st):
        log.append("WARNING: Do Not Call list is NOT trusted (dry run continues; live would stop)")
    if live and (rep.live_blockers):
        stop(["live refused: " + b for b in rep.live_blockers], LiveRefused)

    # 6. Do Not Call on the prospects, right before ranking ------------------------------------
    marked_keyed = st_mod.mark_do_not_call(keyed, st)
    n_blocked = sum(1 for a, b in zip(keyed, marked_keyed) if b.do_not_call and not a.do_not_call)
    marked = _restore(marked_keyed)
    log.append(f"prospects marked do-not-call from state: {n_blocked}")

    # 7. accounts, second pass -----------------------------------------------------------------
    call_date = _first_call_date(cfg, now)
    cu_sf = st_mod.cooldown_until(st, cfg)
    res = acc_mod.build_account_list(raw.sf_accounts, nooks_rows, [], cfg, None, run_date,
                                     blocked_account_ids=set(), cooldown_until=cu_sf, call_date=call_date)

    # 8. signals -------------------------------------------------------------------------------
    verdicts = sig_mod.filter_scoops(raw.scoops, cfg, run_date)
    batch = sig_mod.scoops_to_signals(verdicts, acc_mod.group_by_zoominfo_id(res.accounts), cfg, run_date)
    signals: dict[str, list[Signal]] = {}
    for s in batch.signals:
        signals.setdefault(s.sf_account_id if s.sf_account_id else s.zoominfo_id, []).append(s)

    # 9. rank ----------------------------------------------------------------------------------
    cooldown = dict(cu_sf)
    for kind in ("nooks_account_id", "zoominfo_id"):
        cooldown.update(st_mod.cooldown_until_by(st, cfg, kind))
    state_last_ids = set(st_mod.last_sheet_ids(st, run_date))
    last_ids = state_last_ids | task_last_ids
    log.append(f"last-sheet memory: {len(state_last_ids)} accounts from state.json, {len(task_last_ids)} from tagged "
               f"Nooks tasks ({rep.stats.get('tagged_tasks', 0)} tagged tasks seen)")
    result = ranker.rank_accounts(res.accounts, signals, marked, cooldown, last_ids, cfg, now)
    # safety net (phase1 risk 5): nobody blocked may be a chosen prospect
    unsafe = [p.nooks_id for ps in result.prospects.values() for p in ps if ranker.is_blocked(p)]
    if unsafe:
        stop([f"{len(unsafe)} blocked prospects were chosen by the ranker (bug): refusing to continue"])

    # 10. sheet history: live only, ranked only ---------------------------------------------------
    sheet_recorded = False
    if do_sheet:
        st = st_mod.record_sheet(st, run_date, [r.account.sf_account_id for r in result.ranked])
        if save_state:
            st_mod.save_state(st, state_dir)
        sheet_recorded = True

    stats = _stats(raw, res, result, marked, verdicts, batch, n_blocked, n_bad_dnc, links)
    book_zi = {str(r.get("DOZISF__ZoomInfo_Id__c")).strip() for r in raw.sf_accounts if r.get("DOZISF__ZoomInfo_Id__c")}
    stats["scoops_matched_to_book"] = sum(1 for sc in raw.scoops if str(sc.company_id).strip() in book_zi)
    stats["scoop_companies"] = len({sc.company_id for sc in raw.scoops})
    stats["scoop_companies_matched_to_book"] = len({sc.company_id for sc in raw.scoops if str(sc.company_id).strip() in book_zi})
    stats["calls_to_external"], stats["calls_to_not_e164_other"] = _to_stats(raw)
    stats.update({k: v for k, v in rep.stats.items() if isinstance(v, int) and k not in stats})
    log += _log_body(raw, res, result, verdicts, batch, stats, run_date, call_date, sheet_recorded, live)
    path = write_log(log, log_dir, now) if log_dir else None
    return PipelineResult(res, verdicts, signals, result, marked, st, rep, log, stats, rep.zoominfo_credits_used,
                          now, run_date, call_date, live, sheet_recorded, path, list(batch.unmatched))


def _json(d: dict) -> str:
    import json
    return json.dumps(d, sort_keys=True)


def _stats(raw: RawData, res, result, marked, verdicts, batch, n_blocked, n_bad_dnc, links) -> dict[str, int]:
    kept_verdicts = sum(1 for v in verdicts if v.keep)
    open_tasks = sum(1 for t in raw.tasks if t.status in ("pending", "ready") and not t.completed)
    return {
        "sf_account_rows": len(raw.sf_accounts), "accounts_kept": len(res.accounts), "accounts_dropped": len(res.dropped),
        "prospects": len(marked), "prospects_blocked_from_state": n_blocked, "calls": len(raw.calls),
        "dnc_calls_phone_not_e164": n_bad_dnc, "scoops": len(raw.scoops), "scoops_kept": kept_verdicts,
        "signals": len(batch.signals), "scoops_unmatched": len(batch.unmatched),
        "ranked": len(result.ranked), "needs_prospect": len(result.needs_prospect), "spilled": len(result.spilled),
        "skipped": len(result.skipped), "sent_email_records": len(raw.sent_emails),
        "our_open_nooks_tasks": open_tasks, "nooks_accounts_linked": len(links),
    }


def _log_body(raw, res, result, verdicts, batch, stats, run_date, call_date, sheet_recorded, live) -> list[str]:
    lines = ["", "== SIGNALS =="]
    unmatched = stats["scoops"] - stats["scoops_matched_to_book"]
    lines.append(f"  scoops matched to your book: {stats['scoops_matched_to_book']} of {stats['scoops']} "
                 f"({unmatched} unmatched, normal: only ~30% of scoop companies are in your book; companies "
                 f"{stats['scoop_companies_matched_to_book']} of {stats['scoop_companies']})")
    lines.append(f"  {stats.get('contacts_sales_opt_out_email_unverified', 0)} contacts with Sales_Opt_Out_Email__c "
                 "(meaning unverified, NOT used to block)")
    types: dict[str, int] = {}
    for sc in raw.scoops:
        for t in sc.types:
            types[t] = types.get(t, 0) + 1
    lines.append("  scoop types in the file: " + (", ".join(f"{k}={v}" for k, v in sorted(types.items())) or "none"))
    by_type = (raw.run_meta or {}).get("scoop_counts_by_type")
    if isinstance(by_type, dict):
        lines.append("  scoops fetched per type (run_meta): " + ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())))
    lines.append(f"  calls with a 'to' that is not E.164: {stats['calls_to_external']} literal 'external', "
                 f"{stats['calls_to_not_e164_other']} other (not usable for phone matching, matched by prospect id)")
    for v in verdicts:
        lines.append(f"  {v.scoop.id} ({v.scoop.company_name}): {sig_mod.verdict_summary(v)}")
    lines.append(f"  kept scoops with no matching account: {len(batch.unmatched)}")
    for s in batch.unmatched[:25]:
        lines.append(f"    - {s.id} ({s.company_name}): no account with zoominfo id (not callable here)")
    if len(batch.unmatched) > 25:
        lines.append(f"    ... and {len(batch.unmatched) - 25} more (counted)")
    lines += ["", f"== ACCOUNTS ==  (first call date {call_date})", acc_mod.summarise(res)]
    lines += _list_dropped(res) + _list_flagged(res)
    lines += ["", "== RANKER ==", ranker.summarise(result, top_n=max(15, len(result.ranked)), list_limit=10 ** 6)]
    lines += ["", "== RESULT ==", "  " + ", ".join(f"{k}={v}" for k, v in sorted(stats.items())),
              f"  sheet history recorded: {'yes (live, ranked only)' if sheet_recorded else 'NO'}",
              "  Nooks tasks / Gmail drafts written: NONE (Phase 3 owns writes)"]
    return lines


# ------------------------------------------------------------------ CLI
def _parse_now(text: str | None) -> datetime:
    if not text:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as e:
        raise SystemExit(f"error: --now must be an ISO timestamp ({e})")
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _print_result(r: PipelineResult) -> None:
    rr = r.rank_result
    print(f"\nRanked {len(rr.ranked)} | needs prospect {len(rr.needs_prospect)} | spilled {len(rr.spilled)} | "
          f"skipped {len(rr.skipped)} | accounts kept {r.stats['accounts_kept']} dropped {r.stats['accounts_dropped']}")
    for i, x in enumerate(rr.ranked, 1):
        when = x.local_call_time.strftime("%a %H:%M %Z") if x.local_call_time else "unscheduled"
        print(f" {i:>2}. {x.account.name}  score={x.score:g}  {x.window} {when}  | {'; '.join(x.reasons)}")
    cr = "unknown" if r.zoominfo_credits is None else r.zoominfo_credits
    print(f"ZoomInfo credits used: {cr}")
    if r.log_path:
        print(f"log: {r.log_path}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the call-sheet pipeline over output/raw (offline).")
    ap.add_argument("--raw-dir", default="output/raw")
    ap.add_argument("--state-dir", default="state")
    ap.add_argument("--config", default=str(ROOT / "config" / "config.yaml"))
    ap.add_argument("--now", default=None, help="ISO timestamp (default: now, UTC)")
    ap.add_argument("--limit", type=int, default=None, help="lower the daily cap for this run")
    ap.add_argument("--log-dir", default="logs")
    ap.add_argument("--no-save-state", action="store_true", help="do not write the local state file")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="(default) no sheet history, nothing written")
    mode.add_argument("--live", action="store_true",
                      help="gate only: refuses unless every precondition holds; Phase 3 owns writing tasks")
    args = ap.parse_args(argv)
    now = _parse_now(args.now)
    cfg = load_config(args.config)
    try:
        res = run_pipeline(args.raw_dir, cfg, args.state_dir, now, live=args.live, limit=args.limit,
                           log_dir=args.log_dir, save_state=not args.no_save_state,
                           record_sheet=False if args.live else None)
    except LiveRefused as e:
        print("LIVE REFUSED. Nothing was written to Nooks, Gmail or Salesforce. Reasons:", file=sys.stderr)
        for r in e.reasons:
            print(f"  - {r}", file=sys.stderr)
        print("Run without --live for a dry run.", file=sys.stderr)
        return 2
    except PipelineStopped as e:
        print("STOPPED. Nothing was written to Nooks, Gmail or Salesforce. Reasons:", file=sys.stderr)
        for r in e.reasons:
            print(f"  - {r}", file=sys.stderr)
        return 2
    _print_result(res)
    if args.live:
        print("\nAll live preconditions hold. Phase 3 (nooks_writer) owns writing tasks: this command wrote "
              "nothing to Nooks, Gmail or Salesforce and recorded no sheet history.")
    else:
        print("\nDry run: nothing written to Nooks, Gmail or Salesforce; no sheet history recorded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
