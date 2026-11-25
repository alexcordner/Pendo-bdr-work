"""Deterministic account ranker (task 1f). Plain Python: no LLM, no randomness, no I/O.

Entry point: ``rank_accounts``. Everything else is a small helper that is tested on its own.
Fuller notes (contracts, decisions, what is NOT covered): docs/ranker_notes.md.

SCORING TABLE (points; weights come from ``config.signals.weights``; total clamped to 0-100)
  trigger_strength          weight x combined strength (0-1). Combined = best real signal's strength
                            + 0.10 per additional real signal, capped at 1.0. SignalKind.NONE = 0.
  icp_fit                   weight x tier factor: tier1 1.0, tier2 0.6, tier3 0.3, size unknown 0.3
                            (a deliberate neutral: 0.3 x 25 = 7.5 points, same as tier 3), known
                            headcount outside every tier 0.0.
  flagged_group_penalty     -3 flat when Account.non_anz_billing_anz_hq (decision 5: flagged group
                            ranks slightly lower, never silently dropped).
  recency                   weight x (1 - age_days / signals.max_age_days), floored at 0. Age is that
                            of the best signal. No dated real signal = 0.
  named_prospect_with_phone full weight when pick_prospect() finds a callable persona match.
  cooldown_penalty          the (negative) weight once if the account is in ``cooldown_until`` and the
                            cooldown is still active on the call date (see contract below).
  sdr_target_boost          weight when Account.is_sdr_target. A boost, never a filter.
  clamp_adjustment          only present when the raw sum falls outside 0-100; it is the difference,
                            so score_breakdown always sums to score.

RESULT BUCKETS
  ranked           callable accounts only (a persona-matching, phoned, non-blocked prospect exists),
                   best first, daily cap applied to this list only, each with a window + local_call_time.
  needs_prospect   scored accounts with NO callable prospect (codes: no_prospects_on_account,
                   no_persona_match, persona_match_no_phone). Uncapped, best first, NO window and NO
                   local_call_time. NOTHING IN THIS BUCKET MAY EVER BECOME A NOOKS TASK: it is a
                   prospecting to-do list (find a contact), not a call list.
  skipped          excluded with ``skipped_reason``: no timezone, invalid timezone, all prospects
                   blocked, on last sheet, no open call window, below daily cap.

CONTRACTS
  signals          keyed by sf_account_id or zoominfo_id (both looked up and merged, de-duped by
                   Signal.source).
  cooldown_until   Mapping[id, date]. Value = the FIRST date the account may be called again,
                   inclusive, as a ``date`` in the account's LOCAL timezone. Keyed by sf, zoominfo or
                   nooks id. Active (-30) while value > call date, where call date is the account-local
                   date of its first open window (never the UTC date). A ``datetime`` is converted with
                   .date(); any other type raises ValueError up front.
  last_sheet_ids   set of ids (sf / zoominfo / nooks). A HARD skip ('on last sheet'): never the same
                   account twice in a row.

SCHEDULING: per timezone, all call windows of the first working day that still have time ahead of
``now`` form one timeline (a window already under way starts at ``now``). Callable accounts, best
first, are spread evenly over that timeline with at least MIN_SLOT_MINUTES per account; anything that
does not fit spills to the next working day's windows. A window with under MIN_SLOT_MINUTES left is
not used.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.config import Config, next_working_windows, tier_for
from src.models import Account, Prospect, RankedAccount, Signal, SignalKind

# --- documented constants ---------------------------------------------------------------------
TIER_FACTORS = {"tier1": 1.0, "tier2": 0.6, "tier3": 0.3}
SIZE_UNKNOWN_FACTOR = 0.3          # neutral value for unknown headcount
EXTRA_SIGNAL_BONUS = 0.10          # added to combined strength per additional real signal
FLAGGED_GROUP_PENALTY = -3.0       # non-ANZ billing / ANZ HQ accounts
MIN_SLOT_MINUTES = 3               # minimum wall-clock gap between two calls in one timezone

_KIND_LABELS = {
    SignalKind.FUNDING: "Funding", SignalKind.EXEC_MOVE: "Executive move", SignalKind.NEW_HIRE: "New hire",
    SignalKind.MA: "M&A", SignalKind.PRODUCT_LAUNCH: "Product launch", SignalKind.PARTNERSHIP: "Partnership",
    SignalKind.PROJECT_PAIN: "Project / pain point", SignalKind.HIRING_PLAN: "Hiring plan",
    SignalKind.INTENT: "Intent",
}


@dataclass
class RankResult:
    ranked: list[RankedAccount] = field(default_factory=list)       # callable accounts, top N, best first
    # Scored accounts with no callable prospect; uncapped, best first, no window/time. NEVER make
    # Nooks tasks from these. Code per account (account_key -> code) is in needs_prospect_codes.
    needs_prospect: list[RankedAccount] = field(default_factory=list)
    needs_prospect_codes: dict[str, str] = field(default_factory=dict)
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


# Groups of phrases that mean the same title. Phrases are written in normalised form (see _norm:
# lower case, punctuation and the words of/the/and dropped). A config persona that appears in a group
# also matches every other phrase in that group. Plurals are handled separately (_phrase_in).
ALIASES = [
    {"cto", "chief technology officer", "chief technical officer"},
    {"cio", "chief information officer"},
    {"cpo", "chief product officer"},
    {"vp product", "vice president product"},
    {"head customer experience", "head cx"},
    {"head customer success", "director customer success", "customer success director"},
    {"head support", "head customer support"},
    {"director product", "product director"},
    {"head digital", "digital director", "director digital"},
]
_DROP_WORDS = {"of", "the", "and"}


def _norm(text: str) -> list[str]:
    """Tokens of a title: lower case, , & / ( ) . : ; and quotes become spaces, of/the/and dropped.
    Hyphens are kept INSIDE a token, so 'product-led' stays one word and never equals 'product'."""
    cleaned = re.sub(r"[,&/()\[\].:;'\"+|]", " ", text.lower())
    return [t for t in cleaned.split() if t not in _DROP_WORDS]


def _phrase_in(phrase: list[str], tokens: list[str]) -> bool:
    """True when ``phrase`` appears as consecutive whole tokens (last word may be plural: +s)."""
    n = len(phrase)
    for k in range(len(tokens) - n + 1):
        window = tokens[k:k + n]
        if window[:-1] == phrase[:-1] and window[-1] in (phrase[-1], phrase[-1] + "s"):
            return True
    return False


def _variants(persona: str) -> list[list[str]]:
    base = " ".join(_norm(persona))
    names = {base}
    for group in ALIASES:
        if base in group:
            names |= group
    return [n.split() for n in sorted(names)]


def _persona_rank(title: str | None, config: Config) -> tuple[int, int, int] | None:
    """Best persona match for a title as a sort key (lower is better), else None.

    Key = (level 0 primary / 1 secondary, -words in the matched persona, position in its list)
    would let a long secondary beat a short primary, so the order is instead decided in two steps:
    among all matching personas the one with the MOST words wins (so 'Group Head of Digital
    Workplace' hits the secondary 'Head of Digital Workplace', not the primary 'Head of Digital'),
    ties go to primary before secondary, then to list position (seniority order in the config).
    Returned tuple is (level, position, 0) of that winner.

    Matching is on whole tokens (see _norm / _phrase_in / ALIASES): 'CTO' does not match 'Director',
    'Director of Product-Led Growth' does not match 'Director of Product'.
    """
    if not title:
        return None
    tokens = _norm(title)
    best = None   # (-words, level, idx)
    for level, titles in enumerate((config.personas.primary, config.personas.secondary)):
        for idx, persona in enumerate(titles):
            if any(_phrase_in(v, tokens) for v in _variants(persona)):
                cand = (-len(_norm(persona)), level, idx)
                if best is None or cand < best:
                    best = cand
    return None if best is None else (best[1], best[2], 0)


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


def _ts(sig: Signal) -> float:
    """Sort timestamp of a signal (naive treated as UTC; no date = 0)."""
    if sig.source_date is None:
        return 0.0
    d = sig.source_date
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def _best_signal(sigs: list[Signal]) -> Signal | None:
    """Strongest signal; ties go to the newest, then to source text (deterministic)."""
    if not sigs:
        return None
    return sorted(sigs, key=lambda s: (-s.strength, -_ts(s),
                                       s.source))[0]


def _age_days(sig: Signal, now: datetime) -> int | None:
    if sig.source_date is None:
        return None
    when = sig.source_date
    if when.tzinfo is None:                 # contract says aware UTC; treat naive as UTC, never crash
        when = when.replace(tzinfo=timezone.utc)
    return max(0, (now - when).days)


def _in_cooldown(account: Account, cooldown_until: Mapping[str, date], call_date: date) -> bool:
    """Active while the first-callable date is later than the account-local call date."""
    return any(i in cooldown_until and cooldown_until[i] > call_date for i in _ids(account))


def _on_last_sheet(account: Account, last_sheet_ids: set[str]) -> bool:
    return any(i in last_sheet_ids for i in _ids(account))


def score_account(account: Account, sigs: list[Signal], has_prospect: bool, in_cooldown: bool,
                  config: Config, now: datetime) -> tuple[float, dict[str, float]]:
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
        "cooldown_penalty": w.cooldown_penalty if (in_cooldown) else 0.0,
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
             in_cooldown: bool, config: Config, now: datetime) -> list[str]:
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
    if account.non_anz_billing_anz_hq:
        out.append("Non-ANZ billing, ANZ HQ (small penalty)")
    if account.timezone_guessed:
        out.append("Timezone guessed (country default)")
    return out


def _skip(account: Account, reason: str, sigs: list[Signal] | None = None) -> RankedAccount:
    return RankedAccount(account, skipped_reason=reason, signals=list(sigs or []))


# --- main entry -------------------------------------------------------------------------------
NEEDS_PROSPECT_CODES = ("no_prospects_on_account", "no_persona_match", "persona_match_no_phone")


def _needs_prospect_code(unblocked: list[Prospect], config: Config) -> str:
    """Why an account has no callable prospect (blocked ones are ignored here)."""
    if not unblocked:
        return "no_prospects_on_account"
    if any(_persona_rank(p.title, config) is not None for p in unblocked):
        return "persona_match_no_phone"
    return "no_persona_match"


def _valid_tz(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError, TypeError, OSError):
        return False


def _clean_cooldown(cooldown_until: Mapping[str, date]) -> dict[str, date]:
    """Accept dates; convert datetimes with .date(); reject anything else (ValueError, not TypeError)."""
    out = {}
    for k, v in cooldown_until.items():
        if isinstance(v, datetime):
            v = v.date()
        elif not isinstance(v, date):
            raise ValueError(f"cooldown_until[{k!r}] must be a date, got {type(v).__name__}")
        out[k] = v
    return out


def rank_accounts(accounts: list[Account], signals: Mapping[str, list[Signal]], prospects: list[Prospect],
                  cooldown_until: Mapping[str, date], last_sheet_ids: set[str], config: Config,
                  now: datetime) -> RankResult:
    """Score, bucket, cap and schedule accounts. Pure and deterministic (input order is irrelevant)."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    cooldowns = _clean_cooldown(cooldown_until)

    # Index prospects by every account id they carry.
    by_acct: dict[str, list[Prospect]] = {}
    for p in prospects:
        for i in {p.sf_account_id, p.nooks_account_id}:
            if i:
                by_acct.setdefault(i, []).append(p)

    skipped: list[RankedAccount] = []
    callable_: list[tuple[RankedAccount, Signal | None, list[Prospect], date]] = []
    needs: list[tuple[RankedAccount, Signal | None, list[Prospect], str]] = []

    for a in accounts:
        sigs = _signals_for(a, signals)
        # 1. timezone must be present and valid BEFORE the window helper is called.
        if a.timezone is None:
            skipped.append(_skip(a, "no timezone", sigs))
            continue
        if not _valid_tz(a.timezone):
            skipped.append(_skip(a, f"invalid timezone {a.timezone!r}", sigs))
            continue
        # 2. prospects: de-duplicate by nooks id / contact id when present.
        uniq: dict = {}
        for i in _ids(a):
            for p in by_acct.get(i, []):
                uniq[p.nooks_id or p.sf_contact_id or id(p)] = p
        mine = sorted(uniq.values(), key=lambda p: (p.name.casefold(), p.nooks_id or ""))
        if mine and all(is_blocked(p) for p in mine):
            skipped.append(_skip(a, "all prospects blocked (opted out / do not call)", sigs))
            continue
        # 3. never the same account twice in a row (hard skip).
        if _on_last_sheet(a, last_sheet_ids):
            skipped.append(_skip(a, "on last sheet", sigs))
            continue
        # 4. call date = account-local date of the first open window (never the UTC date).
        windows = next_working_windows(config, a.timezone, now)
        call_date = windows[0].start.date() if windows else now.astimezone(ZoneInfo(a.timezone)).date()
        chosen = pick_prospects(mine, config)
        if chosen and not windows:
            skipped.append(_skip(a, "no call window in the next 14 days", sigs))
            continue
        cool = _in_cooldown(a, cooldowns, call_date)
        score, breakdown = score_account(a, sigs, bool(chosen), cool, config, now)
        best = _best_signal(sigs)
        ra = RankedAccount(a, score=score, score_breakdown=breakdown,
                           reasons=_reasons(a, sigs, best, chosen[0] if chosen else None, cool, config, now),
                           signals=sorted(sigs, key=lambda s: (-s.strength, s.source)))
        if chosen:
            callable_.append((ra, best, chosen, call_date))
        else:
            code = _needs_prospect_code([p for p in mine if not is_blocked(p)], config)
            ra.reasons.insert(0, f"Needs a prospect: {code}")
            needs.append((ra, best, [], code))

    # Deterministic order: score desc, newest best signal, SDR target, name, sf id.
    def sort_key(t):
        ra, best = t[0], t[1]
        return (-ra.score, -(_ts(best) if best and best.source_date else float("-inf")),
                not ra.account.is_sdr_target, ra.account.name.casefold(),
                ra.account.sf_account_id or "", account_key(ra.account))

    callable_.sort(key=sort_key)
    needs.sort(key=sort_key)
    cap = config.limits.daily_call_cap          # cap counts CALLABLE accounts only
    kept, cut = callable_[:cap], callable_[cap:]
    for i, (ra, _, _, _) in enumerate(cut, start=cap + 1):
        ra.skipped_reason = f"below daily cap (rank {i}, cap {cap})"
        skipped.append(ra)

    unscheduled = _assign_times(kept, config, now)
    for ra in unscheduled:
        ra.window, ra.local_call_time = None, None
        ra.skipped_reason = "no call capacity in the next 14 days"
        skipped.append(ra)

    result = RankResult()
    result.ranked = [t[0] for t in kept if t[0].skipped_reason is None]
    result.prospects = {account_key(t[0].account): t[2] for t in kept if t[0].skipped_reason is None}
    result.needs_prospect = [t[0] for t in needs]
    result.needs_prospect_codes = {account_key(t[0].account): t[3] for t in needs}
    result.by_window = {w.name: [] for w in config.call_windows}
    for ra in result.ranked:
        result.by_window[ra.window].append(ra)
    result.skipped = sorted(skipped, key=lambda r: (r.account.name.casefold(), account_key(r.account)))
    return result


def _timeline(config: Config, tz: str, now: datetime) -> list[list[tuple[str, datetime, datetime]]]:
    """Per working day (first open day first), usable segments (name, start, end) in ``tz``.

    The first day's windows start no earlier than ``now``. Segments shorter than MIN_SLOT_MINUTES are
    dropped. Looks up to 15 working days ahead (same horizon as next_working_windows)."""
    days, cursor = [], now
    for _ in range(15):
        ws = next_working_windows(config, tz, cursor)
        if not ws:
            break
        segs = [(w.name, max(w.start, now.astimezone(w.start.tzinfo)), w.end) for w in ws]
        days.append([x for x in segs if (x[2] - x[1]) >= timedelta(minutes=MIN_SLOT_MINUTES)])
        cursor = ws[-1].end
    return days


def _assign_times(kept: list, config: Config, now: datetime) -> list[RankedAccount]:
    """Set window + local_call_time. Returns accounts that found no slot (practically never).

    Per timezone, accounts (already best first) fill the timeline day by day. A day takes at most
    sum(floor(segment minutes / MIN_SLOT_MINUTES)) accounts; when it takes fewer than that they are
    spread evenly over the whole day's usable time (gap = total seconds // n, so >= 3 min), best
    account earliest. Excess spills to the next working day. Confirmed and guessed timezones are
    treated the same: the earlier 'call later when guessed' rule was dropped (it did not protect Perth
    and made two groups share minutes)."""
    by_tz: dict[str, list] = {}
    for item in kept:
        by_tz.setdefault(item[0].account.timezone, []).append(item)
    unscheduled = []
    min_slot = timedelta(minutes=MIN_SLOT_MINUTES)
    for tz, members in sorted(by_tz.items()):
        remaining = members
        for segs in _timeline(config, tz, now):
            if not remaining:
                break
            cap = sum(int((end - start) // min_slot) for _, start, end in segs)
            if cap == 0:
                continue
            take, remaining = remaining[:cap], remaining[cap:]
            total = sum(int((end - start).total_seconds()) for _, start, end in segs)
            gap = total // len(take)
            for i, (ra, _, _, call_date) in enumerate(take):
                off = i * gap
                for name, start, end in segs:
                    dur = int((end - start).total_seconds())
                    if off < dur:
                        ra.window, ra.local_call_time = name, start + timedelta(seconds=off)
                        break
                    off -= dur
                if ra.local_call_time.date() > call_date:
                    ra.reasons.append(f"Spilled to {ra.local_call_time:%a %d %b} (call windows full)")
        unscheduled.extend(t[0] for t in remaining)
    return unscheduled


# --- run log ----------------------------------------------------------------------------------
def summarise(result: RankResult) -> str:
    """Plain-text summary for the run log: counts, windows, skip reasons, top accounts."""
    lines = [f"Ranked {len(result.ranked)} accounts; skipped {len(result.skipped)}; "
             f"needs prospect {len(result.needs_prospect)}."]
    for name, items in result.by_window.items():
        lines.append(f"  window {name}: {len(items)} accounts")
    reasons: dict[str, int] = {}
    for s in result.skipped:
        key = re.sub(r"\(rank \d+.*\)", "", s.skipped_reason or "unknown").strip()
        reasons[key] = reasons.get(key, 0) + 1
    for r in sorted(reasons):
        lines.append(f"  skipped - {r}: {reasons[r]}")
    codes: dict[str, int] = {}
    for c in result.needs_prospect_codes.values():
        codes[c] = codes.get(c, 0) + 1
    for c in sorted(codes):
        lines.append(f"  needs prospect (NOT callable, no task) - {c}: {codes[c]}")
    no_trigger = sum(1 for r in result.ranked if any(x.startswith("No trigger") for x in r.reasons))
    lines.append(f"  no real trigger (ranked on fit only): {no_trigger}")
    for i, r in enumerate(result.ranked[:10], 1):
        when = r.local_call_time.strftime("%a %H:%M %Z") if r.local_call_time else "unscheduled"
        lines.append(f"  {i:>2}. {r.account.name} score={r.score:g} {r.window} {when} | {'; '.join(r.reasons)}")
    for i, r in enumerate(result.needs_prospect[:5], 1):
        lines.append(f"  P{i}. {r.account.name} score={r.score:g} | {'; '.join(r.reasons)}")
    return "\n".join(lines)
