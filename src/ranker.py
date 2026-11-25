"""Deterministic account ranker (task 1f). Plain Python: no LLM, no randomness, no I/O.

Entry point: ``rank_accounts``. Everything else is a small helper that is tested on its own.
Fuller notes (contracts, decisions, what is NOT covered): docs/ranker_notes.md.

SCORING TABLE (points; weights come from ``config.signals.weights``; total clamped to 0-100)
  trigger_strength          weight x signals.combined_strength(signals) (0-1): the best signal counts in
                            full, others are damped (one definition, in src/signals.py). Intent is never
                            the best signal while a dated real signal exists (signals.best_signal).
                            SignalKind.NONE = 0.
  icp_fit                   weight x tier factor: tier1 1.0, tier2 0.6, tier3 0.3, size unknown 0.3
                            (a deliberate neutral: 0.3 x 25 = 7.5 points, same as tier 3), known
                            headcount outside every tier 0.0.
  flagged_group_penalty     -3 flat when Account.non_anz_billing_anz_hq (decision 5: flagged group
                            ranks slightly lower, never silently dropped).
  recency                   weight x (1 - age_days / limit), floored at 0, where limit is
                            signals.max_age_for(best signal's kind, config) (per-kind, 30-90 days;
                            config max_age_days only as the fallback). Age is that of the best signal, from signals.age_days(published, call_date) where call
                            date is the account-LOCAL date of its first usable day (the same function and
                            'today' semantics as the signals log). No dated real signal = 0.
  named_prospect_with_phone full weight when pick_prospect() finds a callable persona match.
  (no cooldown component)   an account whose cooldown is still active on ITS OWN zone call date is
                            SKIPPED, not penalised (see CONTRACTS), so no score ever carries a cooldown
                            penalty; signals.weights.cooldown_penalty is therefore unused here.
  sdr_target_boost          weight when Account.is_sdr_target. A boost, never a filter.
  confidence_ceiling_adjustment  only present when it bites: accounts with Account.confidence 'low'
                            (zoominfo-only / nooks-only / flagged records) score at most
                            LOW_CONFIDENCE_CEILING (60). Applied before the 0-100 clamp.
  clamp_adjustment          only present when the raw sum falls outside 0-100; it is the difference,
                            so score_breakdown always sums to score.

RESULT BUCKETS
  ranked           callable accounts only (a persona-matching, phoned, non-blocked prospect exists),
                   best first, daily cap applied to this list only, each with a window + local_call_time.
  spilled          callable accounts held for a later working day because their timezone's first
                   usable day was full (machine-readable; also reason 'Spilled to ...'). NOT tasks, NOT
                   in ranked, NOT counted against the cap, NOT sheet history.
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
                   nooks id. Active while value > call date, where call date is the account-local date
                   of its first usable day IN ITS OWN TIMEZONE (never the UTC date, never another zone's
                   date). An active cooldown SKIPS the account with 'In cooldown until <date>
                   (account-local)' (needs_prospect accounts too): never ranked, never a task. A
                   ``datetime`` is converted with .date(); any other type raises ValueError up front.
  last_sheet_ids   set of ids (sf / zoominfo / nooks). A HARD skip ('on last sheet'): never the same
                   account twice in a row.

SCHEDULING: per timezone, the FIRST USABLE DAY is the first working day with at least one window
that still has MIN_SLOT_MINUTES left after ``earliest`` (= now rounded up to a whole minute + 1 minute
of lead, so the first task is never due before the run could finish). Its date is the account's call
date (used for cooldown and for deciding what 'spilled' means). Callable accounts, best first, take
slots on that day (spread evenly, >= MIN_SLOT_MINUTES apart) until it is full; the rest are SPILLED
(held, later local_call_time, no cap slot used) and the next-ranked account that fits takes the place.
SHEET BUILDER CONTRACT: write tasks ONLY for ``ranked`` (all on the first usable day of their own
timezone; zones may differ). ``spilled`` and ``needs_prospect`` never become tasks and are never
recorded in sheet history / last_sheet_ids.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.config import Config, next_working_windows, tier_for
from src import signals as sigmod
from src.loaders import normalise_sf_id
from src.models import Account, Prospect, RankedAccount, Signal, SignalKind

# --- documented constants ---------------------------------------------------------------------
TIER_FACTORS = {"tier1": 1.0, "tier2": 0.6, "tier3": 0.3}
SIZE_UNKNOWN_FACTOR = 0.3          # neutral value for unknown headcount
LOW_CONFIDENCE_CEILING = 60.0      # Account.confidence == 'low' can never score above this
FLAGGED_GROUP_PENALTY = -3.0       # non-ANZ billing / ANZ HQ accounts
MIN_SLOT_MINUTES = 3               # minimum wall-clock gap between two calls in one timezone
LEAD_MINUTES = 1                   # first slot = now rounded up to a whole minute + this lead

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
    # Callable accounts HELD for a later working day because their timezone's first usable day was
    # full. They carry their intended later window/local_call_time (for the run log) but are NOT in
    # ranked, do not use cap slots, and must never become tasks or be recorded as sheet history.
    spilled: list[RankedAccount] = field(default_factory=list)
    needs_prospect_codes: dict[str, str] = field(default_factory=dict)
    skipped: list[RankedAccount] = field(default_factory=list)      # each has skipped_reason
    by_window: dict[str, list[RankedAccount]] = field(default_factory=dict)  # window name -> best first
    # Chosen prospects per ranked account (key = account_key), up to limits.max_per_account_per_run.
    prospects: dict[str, list[Prospect]] = field(default_factory=dict)


# --- small helpers ----------------------------------------------------------------------------
def _k(value: str | None) -> str | None:
    """Lookup key for any id: Salesforce ids reduce to their 15-char form (18-char ids find 15-char
    keys and vice versa); other ids (FAKE-, zoominfo, nooks) pass through trimmed."""
    return normalise_sf_id(value)


def account_key(account: Account) -> str:
    """Stable id for an account: sf id (normalised), else zoominfo id, else nooks id, else name."""
    return (_k(account.sf_account_id) or _k(account.zoominfo_id) or _k(account.nooks_account_id)
            or f"name:{account.name}")


def _ids(account: Account) -> list[str]:
    """All normalised ids an account can be looked up by."""
    return [k for k in (_k(account.sf_account_id), _k(account.zoominfo_id), _k(account.nooks_account_id)) if k]


def is_blocked(prospect: Prospect) -> bool:
    """Never-contact: opted out of email or a past Do Not Call disposition."""
    return prospect.opted_out_email or prospect.do_not_call


# Groups of phrases that mean the same title. Phrases are written in normalised form (see _norm:
# lower case, punctuation and the words of/the/and dropped). A config persona that appears in a group
# also matches every other phrase in that group. Plurals are handled separately (_phrase_in).
ALIASES = [
    {"cto", "chief technology officer", "chief technical officer"},
    {"cio", "chief information officer"},
    {"cpo", "chief product officer", "chief product technology officer"},
    {"vp product", "vice president product"},
    {"head customer experience", "head cx"},
    {"head customer success", "director customer success", "customer success director"},
    {"head support", "head customer support"},
    {"director product", "product director"},
    {"head digital", "digital director", "director digital"},
]
_DROP_WORDS = {"of", "the", "and"}
# Stale titles: 'Former Head of Product', 'Ex-CTO', 'Past VP Product', 'Interim former ...' never match.
_STALE = re.compile(r"^\s*(?:(?:interim|acting)\s+)?(?:former|past|ex)(?:[-\s]|$)", re.IGNORECASE)


def _norm(text: str) -> list[str]:
    """Tokens of a title: lower case, , & / ( ) . : ; and quotes become spaces, of/the/and dropped.
    Hyphens are kept INSIDE a token, so 'product-led' stays one word and never equals 'product'."""
    cleaned = re.sub(r"[,&/()\[\].:;'\"+|]", " ", text.lower())
    return [t for t in cleaned.split() if t not in _DROP_WORDS]


def _phrase_starts(phrase: list[str], tokens: list[str]) -> list[int]:
    """Token indexes where ``phrase`` appears as consecutive whole tokens (last word may be plural: +s)."""
    n = len(phrase)
    out = []
    for k in range(len(tokens) - n + 1):
        window = tokens[k:k + n]
        if window[:-1] == phrase[:-1] and window[-1] in (phrase[-1], phrase[-1] + "s"):
            out.append(k)
    return out


def _phrase_in(phrase: list[str], tokens: list[str]) -> bool:
    """True when ``phrase`` appears as consecutive whole tokens (last word may be plural: +s)."""
    return bool(_phrase_starts(phrase, tokens))


def _variants(persona: str) -> list[list[str]]:
    base = " ".join(_norm(persona))
    names = {base}
    for group in ALIASES:
        if base in group:
            names |= group
    return [n.split() for n in sorted(names)]


# --- title hygiene (applied BEFORE persona matching) ------------------------------------------
# Titles that are never a buyer: assistants / admin support / trainees / 'to the <C-level>' / stale titles.
# 'Assistant' alone is NOT rejected: Assistant Director / Assistant Manager / Assistant General Manager /
# Assistant Secretary / Assistant Commissioner / Assistant Vice President are real buyer-level titles
# (Australian public service, banking). Only the PA/EA family is: a title ENDING in Assistant ('Marketing
# Assistant'), the whole title 'Assistant', 'Assistant to ...', PA to / EA to, and the named support roles
# below. 'Secretary' is rejected only as Company / Corporate / Executive / Legal Secretary or 'Secretary to ...'.
_NON_BUYER = re.compile(
    r"(?<!\w)(?:assistant\s+to|personal\s+assistant|executive\s+assistant|administrative\s+assistant|"
    r"admin\s+assistant|virtual\s+assistant|pa\s+to|ea\s+to|"
    r"(?:company|corporate|executive|legal)\s+secretary|secretary\s+to|"
    r"coordinator|administrator|intern|graduate|trainee)(?!\w)|"
    r"(?<!\w)assistant\W*$", re.IGNORECASE)
_C_LEVEL = r"(?:chief|c[a-z]o|ceo|cfo|cto|cio|cpo|coo|cro|cmo|cdo|president|managing\s+director)"
_TO_C_LEVEL = re.compile(r"(?<!\w)to\s+(?:the\s+)?" + _C_LEVEL + r"(?!\w)", re.IGNORECASE)
_REPORTING = re.compile(r"(?<!\w)(?:reporting|reports|report)\s+to\b.*$", re.IGNORECASE)
_SEPARATORS = re.compile(r"[,;/|]|\s[-\u2013\u2014]\s|[()]")      # ends the first ('head') segment of a title
_LIST_SPLIT = re.compile(r"[,;/|&]|\band\b|\s[-\u2013\u2014]\s", re.IGNORECASE)
_ROLE_WORDS = {"head", "director", "chief", "vp", "svp", "evp", "avp", "vice", "president", "manager", "officer",
               "lead", "ceo", "cfo", "cto", "cio", "cpo", "coo", "cro", "cmo", "cdo", "founder", "owner", "partner",
               "principal"}
MAX_TITLE_CHARS = 80
MAX_ROLE_PHRASES = 2


def _strip_reporting(title: str) -> str:
    """'X, reporting to the CTO' -> 'X' (the manager named after 'reporting to' is not this person's role)."""
    return _REPORTING.sub("", title).strip()


def _is_non_buyer(title: str) -> bool:
    """Stale titles (Former/Ex-/Past), assistants and admin/trainee roles, and 'to the <C-level>' titles
    (PA to the CEO, Chief of Staff to the CTO). 'reporting to the CTO' is NOT a 'to the' title."""
    if _STALE.match(title) or _NON_BUYER.search(title):
        return True
    return bool(_TO_C_LEVEL.search(_strip_reporting(title)))


def _role_phrase_count(title: str) -> int:
    """How many comma / slash / and / & separated parts of the title contain a role word."""
    parts = [p for p in _LIST_SPLIT.split(_strip_reporting(title).lower()) if p.strip()]
    return sum(1 for p in parts if _ROLE_WORDS & set(re.findall(r"[a-z]+", p)))


def _is_junk_shape(title: str) -> bool:
    """Too long, or lists more than two roles: a data-entry dump, not one person's role."""
    return len(title.strip()) > MAX_TITLE_CHARS or _role_phrase_count(title) > MAX_ROLE_PHRASES


def _raw_persona_rank(title: str, config: Config) -> tuple[int, int, int] | None:
    """Persona match on the cleaned title, ignoring hygiene rules. (level, position, later) or None.

    ``later`` is 0 when the matched phrase starts in the HEAD segment of the title (before the first
    comma / semicolon / slash / spaced dash / bracket: the role itself) and 1 when it only appears in a
    later segment (a qualifier). Among matching personas: head match first, then the MOST words (so
    'Group Head of Digital Workplace' hits the secondary 'Head of Digital Workplace', not the primary
    'Head of Digital'), then primary before secondary, then config order. Text after 'reporting to' is
    ignored. Whole-token matching (see _norm / _phrase_in / ALIASES): 'CTO' does not match 'Director',
    'Director of Product-Led Growth' does not match 'Director of Product'.
    """
    cleaned = _strip_reporting(title)
    tokens = _norm(cleaned)
    seg = _SEPARATORS.search(cleaned)
    head_end = len(_norm(cleaned[:seg.start()])) if seg else len(tokens)
    best = None   # (later, -words, level, idx)
    for level, titles in enumerate((config.personas.primary, config.personas.secondary)):
        for idx, persona in enumerate(titles):
            starts = [k for v in _variants(persona) for k in _phrase_starts(v, tokens)]
            if not starts:
                continue
            cand = (0 if min(starts) < max(head_end, 1) else 1, -len(_norm(persona)), level, idx)
            if best is None or cand < best:
                best = cand
    return None if best is None else (best[2], best[3], best[0])


def _title_class(title: str | None, config: Config) -> tuple[str, tuple[int, int, int] | None]:
    """('ok', rank) | ('junk', None) | ('non_buyer', None) | ('none', None).

    junk / non_buyer are reported only for titles that WOULD match a persona, so the needs-prospect
    code can say why a matching person was refused. Junk shape wins over non-buyer (a 120-character
    'Executive Assistant To CFO, CTO And CRO' is a junk title)."""
    if not title or not title.strip():
        return "none", None
    t = title.strip()
    raw = _raw_persona_rank(t, config)
    if raw is None:
        return "none", None
    if _is_junk_shape(t):
        return "junk", None
    if _is_non_buyer(t):
        return "non_buyer", None
    return "ok", raw


def _persona_rank(title: str | None, config: Config) -> tuple[int, int, int] | None:
    """(level, position, later) of the persona a clean buyer title matches, else None.
    None for stale / assistant / junk-shaped titles (see _title_class)."""
    return _title_class(title, config)[1]


def pick_prospects(prospects: list[Prospect], config: Config) -> list[Prospect]:
    """Up to ``limits.max_per_account_per_run`` callable prospects, best first.

    Callable = clean buyer title + persona match + phone_e164 + not blocked. Order: head-of-title
    match before a match buried in a qualifier, then primary before secondary, then the title's
    position in the config list (seniority order), then name (case-insensitive), then nooks_id, so
    the result never depends on input order.
    """
    scored = []
    for p in prospects:
        if is_blocked(p) or not p.phone_e164:
            continue
        rank = _persona_rank(p.title, config)
        if rank is not None:
            level, idx, later = rank
            scored.append(((later, level, idx), p.name.casefold(), p.nooks_id or "", p))
    scored.sort(key=lambda t: t[:3])
    return [t[3] for t in scored[:config.limits.max_per_account_per_run]]


def pick_prospect(prospects_for_account: list[Prospect], config: Config) -> Prospect | None:
    """Best callable prospect for one account, or None (see pick_prospects)."""
    chosen = pick_prospects(prospects_for_account, config)
    return chosen[0] if chosen else None


def _signals_for(account: Account, signals: Mapping[str, list[Signal]]) -> list[Signal]:
    """Real signals for an account (sf id and zoominfo id lookups merged, de-duped by source)."""
    seen, out = set(), []
    for key in (_k(account.sf_account_id), _k(account.zoominfo_id)):
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
    """The single definition lives in src/signals.py (intent never beats a dated real signal)."""
    return sigmod.best_signal(sigs)


def _ordered(sigs: list[Signal], best: Signal | None) -> list[Signal]:
    """Signals for RankedAccount.signals: the best one FIRST (so signals[0] matches the reason), then the
    rest by strength."""
    rest = sorted((x for x in sigs if x is not best), key=lambda x: (-x.strength, x.source))
    return ([best] if best is not None else []) + rest


def _age_days(sig: Signal, call_date: date) -> int | None:
    """Whole days from the signal's publish date to the account-local call date (signals.age_days)."""
    if sig.source_date is None:
        return None
    return sigmod.age_days(sig.source_date, call_date)


def _active_cooldown(account: Account, cooldown_until: Mapping[str, date], call_date: date) -> date | None:
    """The latest first-callable date still ahead of the account-local call date, else None."""
    ahead = [cooldown_until[i] for i in _ids(account) if i in cooldown_until and cooldown_until[i] > call_date]
    return max(ahead) if ahead else None


def _on_last_sheet(account: Account, last_sheet_ids: set[str]) -> bool:
    return any(i in last_sheet_ids for i in _ids(account))


def score_account(account: Account, sigs: list[Signal], has_prospect: bool,
                  config: Config, call_date: date) -> tuple[float, dict[str, float]]:
    """Return (score 0-100, breakdown). Breakdown values sum to score (see module docstring).

    ``call_date`` is the account-local date of its first usable day (used for signal age)."""
    w = config.signals.weights
    best = _best_signal(sigs)

    # trigger strength: ONE definition, signals.combined_strength (0 when there is no real signal).
    trigger = sigmod.combined_strength(sigs) if best is not None else 0.0

    # icp fit
    if account.size_unknown:
        factor = SIZE_UNKNOWN_FACTOR
    else:
        factor = TIER_FACTORS.get(tier_for(config, account.employees) or "", 0.0)

    # recency of the best signal
    recency = 0.0
    if best is not None:
        age = _age_days(best, call_date)
        if age is not None:
            recency = max(0.0, 1.0 - age / sigmod.max_age_for(best.kind, config))

    parts = {
        "trigger_strength": w.trigger_strength * trigger,
        "icp_fit": w.icp_fit * factor,
        "recency": w.recency * recency,
        "named_prospect_with_phone": w.named_prospect_with_phone if has_prospect else 0.0,
        "sdr_target_boost": w.sdr_target_boost if account.is_sdr_target else 0.0,
        "flagged_group_penalty": FLAGGED_GROUP_PENALTY if account.non_anz_billing_anz_hq else 0.0,
    }
    breakdown = {k: round(v, 2) for k, v in parts.items()}
    raw = round(sum(breakdown.values()), 2)
    # Low-confidence records (zoominfo-only, nooks-only, flagged) can never top the sheet.
    if account.confidence == "low" and raw > LOW_CONFIDENCE_CEILING:
        breakdown["confidence_ceiling_adjustment"] = round(LOW_CONFIDENCE_CEILING - raw, 2)
        raw = LOW_CONFIDENCE_CEILING
    score = min(100.0, max(0.0, raw))
    if score != raw:                       # clamp: record it so the breakdown still sums to the score
        breakdown["clamp_adjustment"] = round(score - raw, 2)
    return score, breakdown


_FLAG_HINTS = {"noise_location": "check this is a real company"}


def _reasons(account: Account, sigs: list[Signal], best: Signal | None, prospect: Prospect | None,
             config: Config, call_date: date) -> list[str]:
    out = []
    if best is None:
        out.append("No trigger: ranked on fit only")
    else:
        label = _KIND_LABELS.get(best.kind, best.kind.value)
        noun = "scoop" if best.source.startswith("zoominfo_scoop") else "signal"
        age = _age_days(best, call_date)
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
    if account.non_anz_billing_anz_hq:
        out.append("Non-ANZ billing, ANZ HQ (small penalty)")
    if account.timezone_guessed:
        out.append("Timezone guessed (country default)")
    if account.confidence == "low":
        out.append(f"Low-confidence record (score capped at {LOW_CONFIDENCE_CEILING:g})")
    for f in account.flags:
        out.append(f"Flag: {f} - {_FLAG_HINTS.get(f, 'check before calling')}")
    return out


def _skip(account: Account, reason: str, sigs: list[Signal] | None = None) -> RankedAccount:
    return RankedAccount(account, skipped_reason=reason, signals=list(sigs or []))


# --- main entry -------------------------------------------------------------------------------
NEEDS_PROSPECT_CODES = ("no_prospects_on_account", "no_persona_match", "persona_match_no_phone", "junk_title")


def _needs_prospect_code(unblocked: list[Prospect], config: Config) -> str:
    """Why an account has no callable prospect (blocked ones are ignored here).

    persona_match_no_phone: a clean buyer title but no phone. junk_title: the only persona matches are
    junk-shaped titles (too long / lists many roles). no_persona_match: nothing usable (includes
    assistants, stale titles and other non-buyers)."""
    if not unblocked:
        return "no_prospects_on_account"
    kinds = {_title_class(p.title, config)[0] for p in unblocked}
    if "ok" in kinds:
        return "persona_match_no_phone"
    if "junk" in kinds:
        return "junk_title"
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
        key = _k(k) or k
        out[key] = max(v, out[key]) if key in out else v     # two spellings of one id: later date wins
    return out


def _clean_signals(signals: Mapping[str, list[Signal]]) -> dict[str, list[Signal]]:
    """Re-key signals by normalised id (lists for two spellings of one id are concatenated)."""
    out: dict[str, list[Signal]] = {}
    for k, v in signals.items():
        out.setdefault(_k(k) or k, []).extend(v)
    return out


def rank_accounts(accounts: list[Account], signals: Mapping[str, list[Signal]], prospects: list[Prospect],
                  cooldown_until: Mapping[str, date], last_sheet_ids: set[str], config: Config,
                  now: datetime) -> RankResult:
    """Score, bucket, cap and schedule accounts. Pure and deterministic (input order is irrelevant)."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    cooldowns = _clean_cooldown(cooldown_until)
    signals = _clean_signals(signals)
    last_sheet_ids = {_k(x) or x for x in last_sheet_ids}
    timelines: dict[str, list] = {}          # tz -> usable days (first usable day first)

    def days_for(tz: str) -> list:
        if tz not in timelines:
            timelines[tz] = _timeline(config, tz, now)
        return timelines[tz]

    # Index prospects by every account id they carry.
    by_acct: dict[str, list[Prospect]] = {}
    for p in prospects:
        for i in {_k(p.sf_account_id), _k(p.nooks_account_id)}:
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
        # 4. call date = account-local date of the FIRST USABLE day (a window with under
        #    MIN_SLOT_MINUTES left does not count). Never the UTC date.
        days = days_for(a.timezone)
        call_date = days[0][0][1].date() if days else now.astimezone(ZoneInfo(a.timezone)).date()
        # 5. cooldown on THIS zone's call date is a skip, not a penalty (an early zone can still be
        #    inside its cooldown on a day the later zones are not).
        until = _active_cooldown(a, cooldowns, call_date)
        if until is not None:
            skipped.append(_skip(a, f"In cooldown until {until.isoformat()} (account-local)", sigs))
            continue
        chosen = pick_prospects(mine, config)
        if chosen and not days:
            skipped.append(_skip(a, "no call window in the next 14 days", sigs))
            continue
        score, breakdown = score_account(a, sigs, bool(chosen), config, call_date)
        best = _best_signal(sigs)
        ra = RankedAccount(a, score=score, score_breakdown=breakdown,
                           reasons=_reasons(a, sigs, best, chosen[0] if chosen else None, config, call_date),
                           signals=_ordered(sigs, best))
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

    # Select, best first. An account takes a cap slot only if its timezone's first usable day still
    # has room; otherwise it is SPILLED (held) and the next-ranked account that fits replaces it.
    cap = config.limits.daily_call_cap
    day_room = {tz: _day_capacity(days[0]) for tz, days in timelines.items() if days}
    kept, held, cut = [], [], []
    for item in callable_:
        tz = item[0].account.timezone
        if len(kept) >= cap:
            cut.append(item)
        elif day_room[tz] > 0:
            day_room[tz] -= 1
            kept.append(item)
        else:
            held.append(item)
    for n, (ra, _, _, _) in enumerate(cut, start=cap + 1):
        ra.skipped_reason = f"below daily cap (rank {n}, cap {cap})"
        skipped.append(ra)

    _place(kept, timelines, first_day_only=True)
    unplaced = _place(held, timelines, first_day_only=False)
    unplaced_ids = {id(t) for t in unplaced}
    spilled = [t for t in held if id(t) not in unplaced_ids]
    for ra, _, _, _ in spilled:
        ra.reasons.append(f"Spilled to {ra.local_call_time:%a %d %b} (first day's call windows full): held, no task tonight")
    for ra, _, _, _ in unplaced:
        ra.skipped_reason = "no call capacity in the next 14 days"
        skipped.append(ra)

    result = RankResult()
    result.ranked = [t[0] for t in kept]
    result.prospects = {account_key(t[0].account): t[2] for t in kept}
    result.spilled = [t[0] for t in spilled]
    result.needs_prospect = [t[0] for t in needs]
    result.needs_prospect_codes = {account_key(t[0].account): t[3] for t in needs}
    result.by_window = {w.name: [] for w in config.call_windows}
    for ra in result.ranked:
        result.by_window[ra.window].append(ra)
    result.skipped = sorted(skipped, key=lambda r: (r.account.name.casefold(), account_key(r.account)))
    return result


def _earliest(now: datetime) -> datetime:
    """First dialable moment: now rounded UP to a whole minute, plus LEAD_MINUTES."""
    base = now.replace(second=0, microsecond=0)
    if base < now:
        base += timedelta(minutes=1)
    return base + timedelta(minutes=LEAD_MINUTES)


def _timeline(config: Config, tz: str, now: datetime) -> list[list[tuple[str, datetime, datetime]]]:
    """Usable working days in ``tz`` (first usable day first), each a list of segments
    (window name, start, end) as local aware datetimes.

    A segment starts no earlier than _earliest(now) and is dropped when under MIN_SLOT_MINUTES long;
    a day with no usable segment is dropped entirely. Looks up to 15 working days ahead."""
    earliest = _earliest(now)
    days, cursor = [], now
    for _ in range(15):
        ws = next_working_windows(config, tz, cursor)
        if not ws:
            break
        segs = [(w.name, max(w.start, earliest.astimezone(w.start.tzinfo)), w.end) for w in ws]
        usable = [x for x in segs if (x[2] - x[1]) >= timedelta(minutes=MIN_SLOT_MINUTES)]
        if usable:
            days.append(usable)
        cursor = ws[-1].end
    return days


def _day_capacity(segs) -> int:
    """How many accounts one day can hold at MIN_SLOT_MINUTES each."""
    return sum(int((end - start) // timedelta(minutes=MIN_SLOT_MINUTES)) for _, start, end in segs)


def _place(items: list, timelines: dict, first_day_only: bool) -> list:
    """Set window + local_call_time on items (best first). Returns items that found no slot.

    Per timezone the items fill usable days in order (only the first when ``first_day_only``). A day
    holding fewer items than its capacity spreads them evenly over its whole usable time
    (gap = total seconds // n, so >= MIN_SLOT_MINUTES), best item earliest; a full day is packed at
    MIN_SLOT_MINUTES and the excess goes to the next day. Guessed and confirmed timezones share one
    timeline (the earlier 'guessed calls later' rule was dropped: it did not protect Perth)."""
    by_tz: dict[str, list] = {}
    for item in items:
        by_tz.setdefault(item[0].account.timezone, []).append(item)
    unplaced = []
    for tz, members in sorted(by_tz.items()):
        remaining = members
        days = timelines[tz][:1] if first_day_only else timelines[tz][1:]
        for segs in days:
            if not remaining:
                break
            take, remaining = remaining[:_day_capacity(segs)], remaining[_day_capacity(segs):]
            total = sum(int((end - start).total_seconds()) for _, start, end in segs)
            gap = total // len(take)
            for i, item in enumerate(take):
                off = i * gap
                for name, start, end in segs:
                    dur = int((end - start).total_seconds())
                    if off < dur:
                        item[0].window, item[0].local_call_time = name, start + timedelta(seconds=off)
                        break
                    off -= dur
        unplaced.extend(remaining)
    return unplaced


# --- run log ----------------------------------------------------------------------------------
def _name_list(label: str, items: list[tuple[str, str]], limit: int) -> list[str]:
    """'  label (N):' then up to ``limit`` '    - name: reason' lines and an 'and M more' tail."""
    if not items:
        return []
    lines = [f"  {label} ({len(items)}):"]
    lines += [f"    - {name}: {why}" for name, why in items[:limit]]
    if len(items) > limit:
        lines.append(f"    ... and {len(items) - limit} more")
    return lines


def summarise(result: RankResult, top_n: int = 15, list_limit: int = 25) -> str:
    """Plain-text summary for the run log (hard rule 5: say what was skipped and why).

    Counts, then the top ``top_n`` ranked accounts, then every skipped / needs_prospect / spilled
    account by NAME with its reason (up to ``list_limit`` each, with the total and an 'and N more' tail)."""
    lines = [f"Ranked {len(result.ranked)} accounts; skipped {len(result.skipped)}; "
             f"needs prospect {len(result.needs_prospect)}; spilled {len(result.spilled)}."]
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
    for i, r in enumerate(result.ranked[:top_n], 1):
        when = r.local_call_time.strftime("%a %H:%M %Z") if r.local_call_time else "unscheduled"
        lines.append(f"  {i:>2}. {r.account.name} score={r.score:g} {r.window} {when} | {'; '.join(r.reasons)}")
    if len(result.ranked) > top_n:
        lines.append(f"      ... and {len(result.ranked) - top_n} more ranked")
    lines += _name_list("SKIPPED accounts", [(r.account.name, r.skipped_reason or "unknown") for r in result.skipped],
                        list_limit)
    lines += _name_list("NEEDS PROSPECT (no task)",
                        [(r.account.name, f"{result.needs_prospect_codes.get(account_key(r.account), '?')}, "
                                          f"score {r.score:g}") for r in result.needs_prospect], list_limit)
    lines += _name_list("SPILLED (held, no task tonight)",
                        [(r.account.name, f"score {r.score:g}, intended {r.local_call_time:%a %d %b %H:%M}"
                          if r.local_call_time else f"score {r.score:g}") for r in result.spilled], list_limit)
    return "\n".join(lines)
