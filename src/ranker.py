"""Deterministic account ranker (task 1f). Plain Python: no LLM, no randomness, no I/O.

Entry point: ``rank_accounts``. Everything else is a small helper that is tested on its own.

SCORING TABLE (points; weights come from ``config.signals.weights``; total clamped to 0-100)
  trigger_strength          weight x combined strength (0-1). Combined = best real signal's strength
                            + 0.10 per additional real signal, capped at 1.0. SignalKind.NONE = 0.
  icp_fit                   weight x tier factor: tier1 1.0, tier2 0.6, tier3 0.3, size unknown 0.3
                            (neutral: kept, flagged, but treated like the smallest tier), known
                            headcount outside every tier 0.0.
  flagged_group_penalty     -3 flat when Account.non_anz_billing_anz_hq (decision 5: flagged group
                            ranks slightly lower, never silently dropped).
  recency                   weight x (1 - age_days / signals.max_age_days), floored at 0. Age is that
                            of the best signal. No dated real signal = 0.
  named_prospect_with_phone full weight when pick_prospect() finds a callable persona match.
  cooldown_penalty          the (negative) weight once if the account is in ``cooldown_until`` with a
                            date still ahead, or in ``last_sheet_ids`` (never the same account twice in
                            a row). Applied once even if both are true.
  sdr_target_boost          weight when Account.is_sdr_target. A boost, never a filter.
  clamp_adjustment          only present when the raw sum falls outside 0-100; it is the difference,
                            so score_breakdown always sums to score.

Keys: ``signals`` is keyed by sf_account_id or zoominfo_id (both are looked up and merged, de-duped by
Signal.source). ``cooldown_until`` / ``last_sheet_ids`` match on sf_account_id, zoominfo_id or
nooks_account_id. ``cooldown_until[key]`` is the first date the account may be called again, so the
cooldown is active while that date is later than today (UTC date of ``now``).

Skips (account goes to ``RankResult.skipped`` with ``skipped_reason``): no timezone (checked BEFORE
touching the window helper), no open call window, every prospect on the account blocked (opted out /
do not call), and being outside the daily cap (scored but cut). An account with no prospects, or none
with a phone, is NOT skipped: it just earns no named_prospect points.

Times: the best window is the first open window of next_working_windows. If ``now`` is inside it, the
start is clamped to ``now``. To avoid 40 accounts sharing one minute, ranked accounts in the same
(local date, window, timezone, guessed) group get evenly spaced slots across the usable span, best score
first. Guessed timezones (country default) use only the second half of the span, so a wrong guess
lands later in the window.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.config import Config, next_working_windows, tier_for
from src.models import Account, Prospect, RankedAccount, Signal, SignalKind

# --- documented constants ---------------------------------------------------------------------
TIER_FACTORS = {"tier1": 1.0, "tier2": 0.6, "tier3": 0.3}
SIZE_UNKNOWN_FACTOR = 0.3          # neutral value for unknown headcount
EXTRA_SIGNAL_BONUS = 0.10          # added to combined strength per additional real signal
FLAGGED_GROUP_PENALTY = -3.0       # non-ANZ billing / ANZ HQ accounts
GUESSED_TZ_START_FRACTION = 0.5    # guessed timezone: use the later half of the window

_KIND_LABELS = {
    SignalKind.FUNDING: "Funding", SignalKind.EXEC_MOVE: "Executive move", SignalKind.NEW_HIRE: "New hire",
    SignalKind.MA: "M&A", SignalKind.PRODUCT_LAUNCH: "Product launch", SignalKind.PARTNERSHIP: "Partnership",
    SignalKind.PROJECT_PAIN: "Project / pain point", SignalKind.HIRING_PLAN: "Hiring plan",
}


@dataclass
class RankResult:
    ranked: list[RankedAccount] = field(default_factory=list)       # top N, best first
    skipped: list[RankedAccount] = field(default_factory=list)      # each has skipped_reason
    by_window: dict[str, list[RankedAccount]] = field(default_factory=dict)  # window name -> best first
    # Chosen prospects per ranked account (key = account_key), up to limits.max_per_account_per_run.
    prospects: dict[str, list[Prospect]] = field(default_factory=dict)


# --- small helpers ----------------------------------------------------------------------------
def account_key(account: Account) -> str:
    """Stable id for an account: sf id, else zoominfo id, else nooks id, else name."""
    return (account.sf_account_id or account.zoominfo_id or account.nooks_account_id
            or f"name:{account.name}")


def _ids(account: Account) -> list[str]:
    return [i for i in (account.sf_account_id, account.zoominfo_id, account.nooks_account_id) if i]


def is_blocked(prospect: Prospect) -> bool:
    """Never-contact: opted out of email or a past Do Not Call disposition."""
    return prospect.opted_out_email or prospect.do_not_call


def _persona_rank(title: str | None, config: Config) -> tuple[int, int] | None:
    """(0=primary / 1=secondary, position in that list) of the first matching title, else None.

    Match is case-insensitive on WORD BOUNDARIES, not a bare substring: a bare substring would make
    'CTO' match 'Director'. 'Senior Product Manager' still matches 'Product Manager'.
    """
    if not title:
        return None
    for level, titles in enumerate((config.personas.primary, config.personas.secondary)):
        for idx, persona in enumerate(titles):
            if re.search(r"(?<!\w)" + re.escape(persona) + r"(?!\w)", title, re.IGNORECASE):
                return level, idx
    return None


def pick_prospects(prospects: list[Prospect], config: Config) -> list[Prospect]:
    """Up to ``limits.max_per_account_per_run`` callable prospects, best first.

    Callable = persona match + phone_e164 + not blocked. Order: primary before secondary, then
    the title's position in the config list (seniority order), then name (case-insensitive), then
    nooks_id, so the result never depends on input order.
    """
    scored = []
    for p in prospects:
        if is_blocked(p) or not p.phone_e164:
            continue
        rank = _persona_rank(p.title, config)
        if rank is not None:
            scored.append((rank, p.name.casefold(), p.nooks_id or "", p))
    scored.sort(key=lambda t: t[:3])
    return [t[3] for t in scored[:config.limits.max_per_account_per_run]]


def pick_prospect(prospects_for_account: list[Prospect], config: Config) -> Prospect | None:
    """Best callable prospect for one account, or None (see pick_prospects)."""
    chosen = pick_prospects(prospects_for_account, config)
    return chosen[0] if chosen else None


def _signals_for(account: Account, signals: Mapping[str, list[Signal]]) -> list[Signal]:
    """Real signals for an account (sf id and zoominfo id lookups merged, de-duped by source)."""
    seen, out = set(), []
    for key in (account.sf_account_id, account.zoominfo_id):
        for s in signals.get(key, []) if key else []:
            if s.kind is SignalKind.NONE or s.source in seen:
                continue
            seen.add(s.source)
            out.append(s)
    return out


def _best_signal(sigs: list[Signal]) -> Signal | None:
    """Strongest signal; ties go to the newest, then to source text (deterministic)."""
    if not sigs:
        return None
    return sorted(sigs, key=lambda s: (-s.strength, -(s.source_date.timestamp() if s.source_date else 0.0),
                                       s.source))[0]


def _age_days(sig: Signal, now: datetime) -> int | None:
    if sig.source_date is None:
        return None
    return max(0, (now - sig.source_date).days)


def _in_cooldown(account: Account, cooldown_until: Mapping[str, date], today: date) -> bool:
    return any(i in cooldown_until and cooldown_until[i] > today for i in _ids(account))


def _on_last_sheet(account: Account, last_sheet_ids: set[str]) -> bool:
    return any(i in last_sheet_ids for i in _ids(account))


def score_account(account: Account, sigs: list[Signal], has_prospect: bool, in_cooldown: bool,
                  on_last_sheet: bool, config: Config, now: datetime) -> tuple[float, dict[str, float]]:
    """Return (score 0-100, breakdown). Breakdown values sum to score (see module docstring)."""
    w = config.signals.weights
    best = _best_signal(sigs)

    # trigger strength: best + a small bonus per extra signal, capped at 1.
    if best is None:
        trigger = 0.0
    else:
        trigger = min(1.0, best.strength + EXTRA_SIGNAL_BONUS * (len(sigs) - 1))

    # icp fit
    if account.size_unknown:
        factor = SIZE_UNKNOWN_FACTOR
    else:
        factor = TIER_FACTORS.get(tier_for(config, account.employees) or "", 0.0)

    # recency of the best signal
    recency = 0.0
    if best is not None:
        age = _age_days(best, now)
        if age is not None:
            recency = max(0.0, 1.0 - age / config.signals.max_age_days)

    parts = {
        "trigger_strength": w.trigger_strength * trigger,
        "icp_fit": w.icp_fit * factor,
        "recency": w.recency * recency,
        "named_prospect_with_phone": w.named_prospect_with_phone if has_prospect else 0.0,
        "cooldown_penalty": w.cooldown_penalty if (in_cooldown or on_last_sheet) else 0.0,
        "sdr_target_boost": w.sdr_target_boost if account.is_sdr_target else 0.0,
        "flagged_group_penalty": FLAGGED_GROUP_PENALTY if account.non_anz_billing_anz_hq else 0.0,
    }
    breakdown = {k: round(v, 2) for k, v in parts.items()}
    raw = round(sum(breakdown.values()), 2)
    score = min(100.0, max(0.0, raw))
    if score != raw:                       # clamp: record it so the breakdown still sums to the score
        breakdown["clamp_adjustment"] = round(score - raw, 2)
    return score, breakdown


def _reasons(account: Account, sigs: list[Signal], best: Signal | None, prospect: Prospect | None,
             in_cooldown: bool, on_last_sheet: bool, config: Config, now: datetime) -> list[str]:
    out = []
    if best is None:
        out.append("No trigger: ranked on fit only")
    else:
        label = _KIND_LABELS.get(best.kind, best.kind.value)
        noun = "scoop" if best.source.startswith("zoominfo_scoop") else "signal"
        age = _age_days(best, now)
        out.append(f"{label} {noun} {age} day{'s' if age != 1 else ''} ago" if age is not None
                   else f"{label} {noun} (date unknown)")
        if len(sigs) > 1:
            out.append(f"{len(sigs) - 1} more trigger{'s' if len(sigs) > 2 else ''}")
    if account.is_sdr_target:
        out.append("SDR target")
    if account.size_unknown:
        out.append("Headcount unknown (neutral fit)")
    else:
        tier = tier_for(config, account.employees)
        out.append(f"{tier.replace('tier', 'Tier ')} fit ({account.employees} employees)" if tier
                   else f"Outside size tiers ({account.employees} employees)")
    out.append(f"Prospect: {prospect.name} ({prospect.title})" if prospect else "No callable persona with a phone")
    if in_cooldown:
        out.append("In cooldown")
    if on_last_sheet:
        out.append("On the last call sheet")
    if account.non_anz_billing_anz_hq:
        out.append("Non-ANZ billing, ANZ HQ (small penalty)")
    if account.timezone_guessed:
        out.append("Timezone guessed: called later in window")
    return out


def _skip(account: Account, reason: str, sigs: list[Signal] | None = None) -> RankedAccount:
    return RankedAccount(account, skipped_reason=reason, signals=list(sigs or []))


# --- main entry -------------------------------------------------------------------------------
def rank_accounts(accounts: list[Account], signals: Mapping[str, list[Signal]], prospects: list[Prospect],
                  cooldown_until: Mapping[str, date], last_sheet_ids: set[str], config: Config,
                  now: datetime) -> RankResult:
    """Score, filter, cap and schedule accounts. Pure and deterministic (input order is irrelevant)."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    today = now.astimezone(timezone.utc).date()

    # Index prospects by every account id they carry.
    by_acct: dict[str, list[Prospect]] = {}
    for p in prospects:
        for i in {p.sf_account_id, p.nooks_account_id}:
            if i:
                by_acct.setdefault(i, []).append(p)

    skipped: list[RankedAccount] = []
    scored: list[tuple[RankedAccount, Signal | None, list[Prospect], list]] = []

    for a in accounts:
        sigs = _signals_for(a, signals)
        # 1. timezone must be known BEFORE calling the window helper.
        if a.timezone is None:
            skipped.append(_skip(a, "no timezone", sigs))
            continue
        # 2. prospects: de-duplicate (a prospect can be indexed under two ids).
        mine = {id(p): p for i in _ids(a) for p in by_acct.get(i, [])}
        mine_list = sorted(mine.values(), key=lambda p: (p.name.casefold(), p.nooks_id or ""))
        if mine_list and all(is_blocked(p) for p in mine_list):
            skipped.append(_skip(a, "all prospects blocked (opted out / do not call)", sigs))
            continue
        # 3. open windows.
        windows = next_working_windows(config, a.timezone, now)
        if not windows:
            skipped.append(_skip(a, "no call window in the next 14 days", sigs))
            continue
        chosen = pick_prospects(mine_list, config)
        cool, last = _in_cooldown(a, cooldown_until, today), _on_last_sheet(a, last_sheet_ids)
        score, breakdown = score_account(a, sigs, bool(chosen), cool, last, config, now)
        best = _best_signal(sigs)
        ra = RankedAccount(a, score=score, score_breakdown=breakdown, window=windows[0].name,
                           reasons=_reasons(a, sigs, best, chosen[0] if chosen else None, cool, last, config, now),
                           signals=sorted(sigs, key=lambda s: (-s.strength, s.source)))
        scored.append((ra, best, chosen, windows))

    # Deterministic order: score desc, newest best signal, SDR target, name, sf id.
    def sort_key(t):
        ra, best, _, _ = t
        ts = best.source_date.timestamp() if best and best.source_date else float("-inf")
        return (-ra.score, -ts, not ra.account.is_sdr_target, ra.account.name.casefold(),
                ra.account.sf_account_id or "", account_key(ra.account))

    scored.sort(key=sort_key)
    cap = config.limits.daily_call_cap          # applied AFTER skipped accounts were removed
    kept, cut = scored[:cap], scored[cap:]
    for i, (ra, _, _, _) in enumerate(cut, start=cap + 1):
        ra.skipped_reason = f"below daily cap (rank {i}, cap {cap})"
        skipped.append(ra)

    _assign_times(kept, now)

    result = RankResult()
    result.ranked = [t[0] for t in kept]
    result.prospects = {account_key(t[0].account): t[2] for t in kept}
    result.by_window = {w.name: [] for w in config.call_windows}
    for ra in result.ranked:
        result.by_window[ra.window].append(ra)
    result.skipped = sorted(skipped, key=lambda r: (r.account.name.casefold(), account_key(r.account)))
    return result


def _assign_times(kept: list, now: datetime) -> None:
    """Set local_call_time: evenly spaced slots per (date, window, tz, guessed) group, best first."""
    groups: dict[tuple, list] = {}
    for item in kept:                              # kept is already best-first
        ra, _, _, windows = item
        w = windows[0]
        key = (w.start.date(), w.name, ra.account.timezone, ra.account.timezone_guessed)
        groups.setdefault(key, []).append((ra, w))
    for (_, _, tz, guessed), members in groups.items():
        n = len(members)
        for idx, (ra, w) in enumerate(members):
            lo = max(w.start, now.astimezone(ZoneInfo(tz)))      # now inside the window: clamp
            span = w.end - lo
            if guessed:
                lo = lo + span * GUESSED_TZ_START_FRACTION
                span = w.end - lo
            step = timedelta(seconds=int(span.total_seconds() // n))
            ra.local_call_time = lo + step * idx


# --- run log ----------------------------------------------------------------------------------
def summarise(result: RankResult) -> str:
    """Plain-text summary for the run log: counts, windows, skip reasons, top accounts."""
    lines = [f"Ranked {len(result.ranked)} accounts; skipped {len(result.skipped)}."]
    for name, items in result.by_window.items():
        lines.append(f"  window {name}: {len(items)} accounts")
    reasons: dict[str, int] = {}
    for s in result.skipped:
        key = re.sub(r"\(rank \d+.*\)", "", s.skipped_reason or "unknown").strip()
        reasons[key] = reasons.get(key, 0) + 1
    for r in sorted(reasons):
        lines.append(f"  skipped - {r}: {reasons[r]}")
    no_trigger = sum(1 for r in result.ranked if r.reasons and r.reasons[0].startswith("No trigger"))
    lines.append(f"  no real trigger (ranked on fit only): {no_trigger}")
    for i, r in enumerate(result.ranked[:10], 1):
        when = r.local_call_time.strftime("%a %H:%M %Z") if r.local_call_time else "unscheduled"
        lines.append(f"  {i:>2}. {r.account.name} score={r.score:g} {r.window} {when} | {'; '.join(r.reasons)}")
    return "\n".join(lines)
