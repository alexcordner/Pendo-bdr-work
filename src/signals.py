"""Trigger signals from ZoomInfo scoops (and, as an UNVERIFIED placeholder, intent rows).

Pure, deterministic, offline: no I/O, no clock reads (callers pass ``today``), no LLM, no randomness.
The LLM never scores anything; this module decides which scoops are real Pendo-buyer triggers and
how strong they are. A scoop that is not a real trigger is dropped WITH a reason string, never silently.

Pipeline:
    filter_scoops        -> list[ScoopVerdict]       keep / drop + reason for every scoop
    scoops_to_signals    -> SignalBatch(signals, unmatched)   kept scoops joined to accounts
    best_signal / combined_strength                   several signals on one account
    trigger_radar        -> list[RadarEntry]          accounts ranked by trigger strength
    no_trigger_signal                                 the explicit "nothing found" signal
    intent_to_signals                                 UNVERIFIED placeholder for ZoomInfo intent
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import NamedTuple

from src.config import Config
from src.models import Account, Scoop, Signal, SignalKind

# ---------------------------------------------------------------- tunable constants
#: Base weight per signal kind (0-1): how much a fresh, kept scoop of this kind says "call now".
#: Strength = base weight x recency decay. Tune here; tests only rely on the ordering rules, not exact values.
KIND_BASE_WEIGHT: dict[SignalKind, float] = {
    SignalKind.EXEC_MOVE: 0.90,       # new product / digital / CX / tech leader: the classic buying window
    SignalKind.PRODUCT_LAUNCH: 0.80,  # a new app or product needs adoption and analytics
    SignalKind.PROJECT_PAIN: 0.80,    # explicit CX / product / digital-workplace project or pain
    SignalKind.FUNDING: 0.70,         # money to invest in the product
    SignalKind.NEW_HIRE: 0.65,        # relevant hire that is not framed as an exec move
    SignalKind.MA: 0.60,              # integration brings platform consolidation
    SignalKind.HIRING_PLAN: 0.55,     # hiring product / CX / engineering roles
    SignalKind.PARTNERSHIP: 0.45,     # often marketing noise
    SignalKind.INTENT: 0.50,          # UNVERIFIED connector shape: never stronger than half a fresh scoop
    SignalKind.NONE: 0.0,
}

#: Recency decay is exponential: factor = 0.5 ** (age_days / half_life). Default half-life 21 days:
#: 1.0 today, 0.5 at 3 weeks, ~0.23 at the 45-day config cut-off.
RECENCY_HALF_LIFE_DAYS = 21.0
#: Per-kind overrides. A new leader's buying window lasts months, so people moves decay more slowly.
#: Every kind not listed here uses RECENCY_HALF_LIFE_DAYS. The config's max_age_days cut-off still applies.
KIND_HALF_LIFE_DAYS: dict[SignalKind, float] = {
    SignalKind.EXEC_MOVE: 45.0,
    SignalKind.NEW_HIRE: 45.0,
}

#: Several signals on one account: the best counts in full, every other one counts at this fraction.
#: combined = 1 - (1 - best) * prod(1 - COMBINE_DAMPING * other). Always >= best and <= 1.0.
COMBINE_DAMPING = 0.5

#: Longest evidence text kept (characters, including the ellipsis).
EVIDENCE_MAX_CHARS = 280

SOURCE_PREFIX = "zoominfo_scoop"        # Signal.source = "zoominfo_scoop:<scoop id>" (interfaces.md convention)
SOURCE_LABEL = "ZoomInfo scoop"         # human-readable label for logs and the call sheet

#: Scoop type name (as ZoomInfo spells it) -> kind. Project / Pain Point are gated by topic, see below.
TYPE_TO_KIND: dict[str, SignalKind] = {
    "funding": SignalKind.FUNDING,
    "executive move": SignalKind.EXEC_MOVE,
    "management move": SignalKind.EXEC_MOVE,
    "new hire": SignalKind.NEW_HIRE,
    "mergers & acquisitions (m&a)": SignalKind.MA,
    "product launch": SignalKind.PRODUCT_LAUNCH,
    "partnership": SignalKind.PARTNERSHIP,
    "project": SignalKind.PROJECT_PAIN,
    "pain point": SignalKind.PROJECT_PAIN,
    "hiring plans": SignalKind.HIRING_PLAN,
    "open position": SignalKind.HIRING_PLAN,
}
#: When one scoop carries several types, the passing kind with the highest KIND_BASE_WEIGHT wins (so the
#: precedence can never disagree with the strength table). Equal weights tie-break on the kind's name.
def _by_weight(kinds) -> list[SignalKind]:
    return sorted(kinds, key=lambda k: (-KIND_BASE_WEIGHT[k], k.value))


#: Project / Pain Point scoops are kept ONLY with one of these topics (docs/decisions.md #9).
PROJECT_TOPICS = frozenset({"customer experience", "product management", "customer success",
                            "digital workplace", "product development"})
#: Hiring Plans / Open Position are kept with one of these topics, or a keywords_keep hit in the description.
HIRING_TOPICS = PROJECT_TOPICS | {"engineering", "digital transformation"}

# ---------------------------------------------------------------- text matching helpers
_FLAGS = re.IGNORECASE


def _kw_regex(keyword: str) -> re.Pattern:
    """Whole-word, case-insensitive match of ``keyword`` (an optional plural 's' is allowed).
    'app' matches 'app' and 'apps' but not 'apple' or 'application'."""
    return re.compile(r"(?<!\w)" + re.escape(keyword.strip()) + r"(?:s|es)?(?!\w)", _FLAGS)


def _first_hit(text: str, keywords) -> str | None:
    """First keyword (in the given order) found as a whole word in ``text``, else None."""
    for kw in keywords:
        if kw.strip() and _kw_regex(kw).search(text):
            return kw
    return None


# Role detection for Executive Move / New Hire / Management Move. The scoop has no title field, so we
# read the description. A role counts only when a title word sits next to a Pendo-buyer domain word
# ("Head of Product", "Chief Digital Officer", "VP, Customer Success", "Product Director", "IT Director")
# AND the words right after it do not turn it into a non-buyer function ("Head of Product Supply",
# "Director of Support Services (aged care)"). See _NEGATIVE. 'officer' is deliberately not a title word
# on its own ("Customer Support Officer" is junior); "Chief X Officer" matches through 'chief'.
_TITLE = (r"chief|head|vp|svp|evp|avp|vice[\s-]+president|president|director|manager|lead|owner|"
          r"general\s+manager|gm")
_DOMAIN = (r"product|digital|customer\s+experience|cx|customer\s+success|customer\s+support|customer\s+service|"
           r"customer\s+care|customer\s+officer|support|user\s+experience|ux|engineering|technology|"
           r"information\s+technology|digital\s+workplace|employee\s+experience|transformation|mobile")
_SEP = r"[\s,/&:-]+"
_TITLE_THEN_DOMAIN = re.compile(
    rf"(?<!\w)(?:{_TITLE})(?!\w){_SEP}(?:(?:of|and)\s+)?(?:(?:global|regional|senior|group|apac|anz|asia\s+pacific)\s+)?"
    rf"(?:{_DOMAIN})(?!\w)", _FLAGS)
_DOMAIN_THEN_TITLE = re.compile(
    rf"(?<!\w)(?:{_DOMAIN})(?!\w){_SEP}(?:\w+{_SEP}){{0,1}}(?:{_TITLE})(?!\w)", _FLAGS)
# Acronyms and phrases that name a role on their own. 'IT' is matched case-sensitively so the pronoun 'it' never counts.
_ROLE_ACRONYMS = re.compile(r"(?<!\w)(?:CTO|CPO|CDO|CIO|CXO)(?!\w)|(?i:chief\s+(?:technology|information|product|digital|experience|customer)\s+officer)")
_IT_ROLE = re.compile(r"(?<!\w)(?:(?i:head|director|manager|lead|chief|vp|svp)[\s,]+(?:(?i:of)\s+)?IT(?!\w)|IT\s+(?i:director|manager|head|lead)(?!\w))")
# Marketing titles contain a domain word but are not buyers ("Product Marketing Manager", "Digital Marketing Lead").
_MARKETING = re.compile(r"(?:product|digital)\s+marketing", _FLAGS)
# Words that, inside the role phrase or the 3 words after it, mean the role is some other function
# ("Head of Product Supply and Logistics", "Technology Procurement Director", "Head of Product Compliance").
_NEGATIVE = re.compile(
    r"(?<!\w)(?:supply|compliance|procurement|purchasing|logistics|manufactur\w*|marketing|social\s+media|finance|"
    r"financial|legal|facilit\w+|safety|sales|risk|warehouse|aged\s+care|hospital|clinical|nursing|network|"
    r"support\s+services?|human\s+resources|hr|people|recruit\w*)(?!\w)", _FLAGS)


def relevant_role(description: str) -> str | None:
    """The matched role phrase if the description names a product / digital / CX / customer success /
    support / engineering / technology / mobile / IT / digital-workplace role, else None.
    A bare 'CEO' or 'Chief Executive Officer' (or CFO, finance, legal...) is NOT relevant, and neither is a
    buyer word inside a non-buyer role (see _NEGATIVE)."""
    text = _MARKETING.sub(" ", description)
    for rx in (_ROLE_ACRONYMS, _IT_ROLE, _TITLE_THEN_DOMAIN, _DOMAIN_THEN_TITLE):
        for m in rx.finditer(text):
            tail = re.split(r"[.;]", text[m.end():], maxsplit=1)[0]
            context = m.group(0) + " " + " ".join(re.findall(r"[A-Za-z&]+", tail)[:3])
            if not _NEGATIVE.search(context):
                return " ".join(m.group(0).split())
    return None


# Hiring notes need a buyer-role context before a keep keyword may beat a drop keyword (or count at all).
# A role (relevant_role) or one of these phrases counts; bare weak words (support, platform, app, engineering,
# mobile, launch, transformation) never do, because "support staff for the hospital ward" and
# "drivers for its delivery platform" contain them.
_HIRING_CONTEXT = re.compile(
    r"(?<!\w)(?:product\s+(?:managers?|owners?|team|leads?|designers?|analysts?|operations)|ux|cx|"
    r"customer\s+(?:success|experience)|mobile\s+(?:app\s+)?(?:developers?|engineers?)|"
    r"digital\s+(?:product|experience|transformation|workplace)|software\s+(?:engineers?|developers?)|"
    r"engineering\s+(?:managers?|leads?|team)|cto)(?!\w)", _FLAGS)
#: Weak keywords from config keywords_keep: never enough on their own for a hiring note.
WEAK_KEEP_KEYWORDS = frozenset({"support", "app", "platform", "engineering", "mobile", "launch", "transformation"})


def hiring_context(description: str) -> str | None:
    """Buyer-role context in a hiring note: a relevant role, or a strong role phrase. None if absent."""
    m = _HIRING_CONTEXT.search(description)
    return " ".join(m.group(0).split()) if m else relevant_role(description)


_INVEST_VERB = re.compile(r"(?<!\w)invest(?:ed|s|ing|ment)(?!\w)", _FLAGS)
_FUNDING_RECEIVED = re.compile(r"(?<!\w)(?:raised|secured|received|closed|funding|series\s+[a-z]|seed|backed\s+by|round)(?!\w)", _FLAGS)


def _is_investor_note(scoop: Scoop) -> bool:
    """True when a Funding scoop is about the INVESTOR, not the funded company ('X invested in Y').
    Rule: the investment verb is present and either the scoop company is its grammatical subject
    (company name, then the verb, in the same clause) or nothing says the company received money."""
    text = scoop.description
    if not _INVEST_VERB.search(text):
        return False
    name = re.sub(r"[^\w&' -]", "", scoop.company_name).strip().split(" ")[0] if scoop.company_name.strip() else ""
    if name and re.search(rf"(?<!\w){re.escape(name)}(?!\w)(?:(?!\.\s)[^;:\n]){{0,60}}?(?<!\w)invest(?:ed|s|ing)(?!\w)", text, _FLAGS):
        return True
    if name and re.search(rf"invest(?:ed|s|ing|ment)(?:\s+(?:\$[\d.,]+\s*\w*|an?\s+\w+))?\s+in\s+(?:the\s+)?{re.escape(name)}(?!\w)", text, _FLAGS):
        return False       # the company is the investee: it received the money
    return not _FUNDING_RECEIVED.search(text)


# ---------------------------------------------------------------- date helpers
def _as_date(value: date | datetime) -> date:
    """Calendar date in UTC (aware datetimes are converted, naive ones are taken as UTC)."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value.date()
    return value


def age_days(published: datetime, today: date | datetime) -> int:
    """Whole days between the scoop's UTC publish date and ``today``. Never negative (a future-dated scoop is age 0)."""
    return max(0, (_as_date(today) - _as_date(published)).days)


def half_life_for(kind: SignalKind) -> float:
    """Half-life in days for a kind (KIND_HALF_LIFE_DAYS, else RECENCY_HALF_LIFE_DAYS)."""
    return KIND_HALF_LIFE_DAYS.get(kind, RECENCY_HALF_LIFE_DAYS)


def recency_factor(days: float, half_life: float | None = None) -> float:
    """Exponential decay 0..1: 1.0 at 0 days, 0.5 at one half-life (default RECENCY_HALF_LIFE_DAYS).
    Monotonically non-increasing in ``days``."""
    hl = RECENCY_HALF_LIFE_DAYS if half_life is None else half_life
    return 0.5 ** (max(0.0, float(days)) / hl)


def signal_strength(kind: SignalKind, days: float) -> float:
    """Base weight of the kind x recency decay (per-kind half-life), rounded to 4 dp, always within 0.0-1.0."""
    s = KIND_BASE_WEIGHT[kind] * recency_factor(days, half_life_for(kind))
    return round(min(1.0, max(0.0, s)), 4)


# ---------------------------------------------------------------- 1. filter
@dataclass(frozen=True)
class ScoopVerdict:
    """Keep / drop decision for one scoop. ``reason`` is always filled; ``kind`` is NONE when dropped."""

    scoop: Scoop
    keep: bool
    kind: SignalKind
    reason: str


def _lower_set(values) -> set[str]:
    return {v.strip().lower() for v in values}


def _judge_kind(kind: SignalKind, scoop: Scoop, config: Config) -> tuple[bool, str]:
    """Apply the per-kind keep rules. Returns (keep, reason)."""
    desc = scoop.description or ""
    topics = _lower_set(scoop.topics)
    sg = config.signals

    if kind is SignalKind.PROJECT_PAIN:
        hit = sorted(topics & PROJECT_TOPICS)
        if hit:
            return True, f"Project/Pain Point with buyer topic: {', '.join(hit)}"
        return False, "Project/Pain Point without a Pendo-buyer topic (needs Customer Experience, Product Management, " \
                      "Customer Success, Product Development or Digital Workplace)"

    if kind in (SignalKind.EXEC_MOVE, SignalKind.NEW_HIRE):
        role = relevant_role(desc)
        if role:
            return True, f"{'executive/management move' if kind is SignalKind.EXEC_MOVE else 'new hire'} in a relevant role: '{role}'"
        return False, "people move without a product/digital/CX/customer success/support/engineering/IT role in the description"

    if kind is SignalKind.HIRING_PLAN:
        # Generic hiring (nurses, drivers, legal, 'more open roles in Medical & Health') is dropped unless the
        # note shows a buyer-role context. A topic hit counts as a keep signal but does NOT rescue a note that
        # also has a drop keyword and no role context. Weak keywords (WEAK_KEEP_KEYWORDS) never keep a note alone.
        context = hiring_context(desc)
        drop_kw = _first_hit(desc, sg.keywords_drop)
        if drop_kw and not context:
            return False, f"generic hiring note (matches drop keyword '{drop_kw}', no buyer-role context)"
        if context:
            return True, f"hiring with buyer-role context: '{context}'"
        hit = sorted(topics & HIRING_TOPICS)
        if hit:
            return True, f"hiring with relevant topic: {', '.join(hit)}"
        strong = _first_hit(desc, [k for k in sg.keywords_keep if k.strip().lower() not in WEAK_KEEP_KEYWORDS])
        if strong:
            return True, f"hiring with buyer keyword '{strong}' in the description"
        weak = _first_hit(desc, sg.keywords_keep)
        extra = f" (only the weak keyword '{weak}')" if weak else ""
        return False, "hiring note with no relevant topic, buyer role or strong keyword" + extra

    if kind is SignalKind.FUNDING:
        if _is_investor_note(scoop):
            return False, "investor note: about the investor, not a company that received funding"
        return True, "funding"

    labels = {SignalKind.MA: "M&A", SignalKind.PRODUCT_LAUNCH: "product launch", SignalKind.PARTNERSHIP: "partnership"}
    return True, labels[kind]


def filter_scoops(scoops: list[Scoop], config: Config, today: date | datetime) -> list[ScoopVerdict]:
    """One verdict per input scoop, in input order. Never drops silently: every verdict has a reason.

    Order of checks: age (older than ``signals.max_age_days`` is dropped; exactly that many days is kept),
    then type (must be in ``signals.scoop_types_keep``, or be Project / Pain Point which has its own topic gate),
    then the per-kind rules in ``_judge_kind``.
    """
    sg = config.signals
    allowed_types = _lower_set(sg.scoop_types_keep) | {"project", "pain point"}
    verdicts: list[ScoopVerdict] = []
    for s in scoops:
        days = (_as_date(today) - _as_date(s.published_date)).days
        if days > sg.max_age_days:
            verdicts.append(ScoopVerdict(s, False, SignalKind.NONE,
                                         f"expired: {days} days old, older than max_age_days={sg.max_age_days}"))
            continue
        types = [t for t in s.types if t.strip().lower() in allowed_types]
        if not types:
            shown = ", ".join(s.types) if s.types else "none"
            verdicts.append(ScoopVerdict(s, False, SignalKind.NONE, f"scoop type not a Pendo trigger (types: {shown})"))
            continue
        kinds = {TYPE_TO_KIND[t.strip().lower()] for t in types if t.strip().lower() in TYPE_TO_KIND}
        if not kinds:
            # Configured as keep but this module has no kind for it: say so instead of guessing.
            verdicts.append(ScoopVerdict(s, False, SignalKind.NONE,
                                         f"type {', '.join(types)} has no SignalKind mapping in signals.py"))
            continue
        passed: dict[SignalKind, str] = {}
        failed: list[str] = []
        for k in _by_weight(kinds):
            ok, why = _judge_kind(k, s, config)
            if ok:
                passed[k] = why
            else:
                failed.append(why)
        if passed:
            best = _by_weight(passed)[0]
            note = " (future-dated, treated as today)" if days < 0 else ""
            verdicts.append(ScoopVerdict(s, True, best, passed[best] + note))
        else:
            verdicts.append(ScoopVerdict(s, False, SignalKind.NONE, "; ".join(failed)))
    return verdicts


# ---------------------------------------------------------------- 2. scoops -> signals
class SignalBatch(NamedTuple):
    """Result of scoops_to_signals. Unpacks as ``signals, unmatched = ...``."""

    signals: list[Signal]          # one per kept scoop matched to an account, sorted deterministically
    unmatched: list[Scoop]         # kept scoops whose company_id matches no account (never discarded silently)


def _key(value) -> str:
    """Join-key normalisation: stripped string ('' for None)."""
    return "" if value is None else str(value).strip()


def _trim_evidence(text: str) -> str:
    text = " ".join((text or "").split())
    if len(text) <= EVIDENCE_MAX_CHARS:
        return text
    return text[: EVIDENCE_MAX_CHARS - 1].rstrip() + "…"


def _signal_sort_key(sig: Signal):
    ts = sig.source_date.timestamp() if sig.source_date else float("-inf")
    return (-sig.strength, -ts, sig.kind.value, sig.source)


def scoops_to_signals(verdicts: list[ScoopVerdict], accounts_by_zoominfo_id: dict[str, Account],
                      config: Config, today: date | datetime) -> SignalBatch:
    """Turn KEPT verdicts into Signals joined to accounts by ``Scoop.company_id == Account.zoominfo_id``.

    ``config`` is accepted for interface symmetry (and future per-kind weights); strength comes from the
    module constants KIND_BASE_WEIGHT, RECENCY_HALF_LIFE_DAYS and KIND_HALF_LIFE_DAYS. A repeated scoop id is used once.
    Join keys are compared as stripped strings, so ' 123 ' and 123 both match '123'.
    Several scoops on one account all become signals; use ``best_signal`` / ``combined_strength``.
    """
    signals: list[Signal] = []
    unmatched: list[Scoop] = []
    seen: set[str] = set()
    accounts_by_zoominfo_id = {_key(k): a for k, a in accounts_by_zoominfo_id.items() if _key(k)}
    for v in verdicts:
        if not v.keep or v.scoop.id in seen:
            continue
        seen.add(v.scoop.id)
        s = v.scoop
        acct = accounts_by_zoominfo_id.get(_key(s.company_id))
        if acct is None:
            unmatched.append(s)
            continue
        signals.append(Signal(
            sf_account_id=acct.sf_account_id,
            kind=v.kind,
            strength=signal_strength(v.kind, age_days(s.published_date, today)),
            source=f"{SOURCE_PREFIX}:{s.id}",
            source_date=s.published_date,
            evidence=_trim_evidence(s.description),
            zoominfo_id=_key(s.company_id),
        ))
    signals.sort(key=_signal_sort_key)
    unmatched.sort(key=lambda sc: (sc.company_id, sc.id))
    return SignalBatch(signals, unmatched)


def best_signal(signals: list[Signal]) -> Signal | None:
    """Strongest signal; ties go to the newer one, then kind name, then source (deterministic).
    None for an empty list: use ``no_trigger_signal(account)`` when a Signal is needed."""
    real = [s for s in signals if s.kind is not SignalKind.NONE]
    return min(real, key=_signal_sort_key) if real else None


def combined_strength(signals: list[Signal]) -> float:
    """Diminishing returns over all signals of one account, in 0.0-1.0.
    The best counts fully, each other counts at COMBINE_DAMPING: 1 - (1-best) * prod(1 - d*other)."""
    strengths = sorted((s.strength for s in signals if s.kind is not SignalKind.NONE), reverse=True)
    if not strengths:
        return 0.0
    rest = 1.0
    for x in strengths[1:]:
        rest *= 1.0 - COMBINE_DAMPING * x
    return round(min(1.0, 1.0 - (1.0 - strengths[0]) * rest), 4)


# ---------------------------------------------------------------- 3. no trigger
def no_trigger_signal(account: Account) -> Signal:
    """The explicit 'nothing found' signal so callers never invent a trigger (CLAUDE.md rule 6)."""
    return Signal(account.sf_account_id, SignalKind.NONE, 0.0, "none", None, "No trigger found",
                  zoominfo_id=account.zoominfo_id)


# ---------------------------------------------------------------- 4. trigger radar
@dataclass(frozen=True)
class RadarEntry:
    """One account on the radar: its best signal, the combined strength over all its signals, and a plain-text why."""

    account: Account
    signal: Signal                  # best signal
    combined_strength: float
    why: str
    signals: tuple[Signal, ...] = ()   # all kept signals for the account, strongest first


def _why(sig: Signal, total: int, today: date | datetime) -> str:
    days = age_days(sig.source_date, today) if sig.source_date else None
    when = f"{days}d ago" if days is not None else "date unknown"
    more = f" (+{total - 1} more)" if total > 1 else ""
    return f"{sig.kind.value} ({when}, strength {sig.strength:.2f}){more}: {sig.evidence}"


def trigger_radar(scoops: list[Scoop], accounts: list[Account], config: Config, today: date | datetime,
                  top_n: int | None = 20) -> list[RadarEntry]:
    """Accounts with at least one kept scoop, strongest trigger first. Pure: no I/O.

    Sort: combined strength desc, then newest best-signal date, then account name (case-insensitive),
    then sf_account_id / zoominfo_id so the order never depends on input order.
    ``top_n=None`` returns everything; ``top_n=0`` returns nothing. Accounts without a trigger are not listed
    (use ``no_trigger_signal`` when you need an explicit "none"). If two accounts share a zoominfo_id the first wins.
    """
    if top_n is not None and top_n < 0:
        raise ValueError(f"top_n must be >= 0 or None, got {top_n}")
    by_zi: dict[str, Account] = {}
    for a in accounts:
        if _key(a.zoominfo_id) and _key(a.zoominfo_id) not in by_zi:
            by_zi[_key(a.zoominfo_id)] = a
    batch = scoops_to_signals(filter_scoops(scoops, config, today), by_zi, config, today)

    grouped: dict[str, list[Signal]] = {}
    for sig in batch.signals:
        grouped.setdefault(sig.zoominfo_id or "", []).append(sig)

    entries: list[RadarEntry] = []
    for zi, sigs in grouped.items():
        sigs.sort(key=_signal_sort_key)
        best = sigs[0]
        entries.append(RadarEntry(by_zi[zi], best, combined_strength(sigs), _why(best, len(sigs), today), tuple(sigs)))

    def key(e: RadarEntry):
        ts = e.signal.source_date.timestamp() if e.signal.source_date else float("-inf")
        return (-e.combined_strength, -ts, e.account.name.lower(), e.account.sf_account_id or "", e.account.zoominfo_id or "")

    entries.sort(key=key)
    return entries if top_n is None else entries[:top_n]


# ---------------------------------------------------------------- 5. intent (UNVERIFIED placeholder)
# UNVERIFIED: the row shape below is OUR GUESS at what ZoomInfo search_intent returns once flattened. It has not
# been checked against the live connector (Phase 2b does that). Nothing here calls a connector. Expected row:
#   {"company_id": "<ZoomInfo company id>", "topic": "Digital Experience",
#    "signal_score": 60-100, "audience_strength": "A".."E", "recorded_date": "2026-11-20" (optional)}
# 'onboarding' alone is NOT a topic (HR onboarding, customer onboarding...): it only counts combined with product / user words.
INTENT_TOPICS = frozenset({"digital experience", "product analytics", "customer experience", "digital adoption",
                           "product onboarding", "user onboarding"})
#: Audience strength A (strongest) to E multiplies the score. Intent is weaker evidence than a dated event,
#: so the strength is also capped at INTENT_MAX_STRENGTH (= the INTENT base weight): half of a fresh scoop at most.
INTENT_AUDIENCE_FACTOR = {"A": 1.0, "B": 0.85, "C": 0.7, "D": 0.55, "E": 0.4}
INTENT_MAX_STRENGTH = KIND_BASE_WEIGHT[SignalKind.INTENT]   # 0.5
INTENT_SCORE_RANGE = (60, 100)
INTENT_SOURCE_PREFIX = "zoominfo_intent"


def _parse_intent_date(raw) -> datetime | None:
    if raw in (None, ""):
        return None
    try:
        d = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def intent_to_signals(rows: list[dict], accounts: list[Account], config: Config, today: date | datetime,
                      skipped: list[tuple[dict, str]] | None = None) -> list[Signal]:
    """UNVERIFIED placeholder: intent rows -> Signals (kind INTENT).

    Only Pendo-relevant topics (INTENT_TOPICS) count. strength = (score-60)/40 x audience factor, capped at
    INTENT_MAX_STRENGTH (0.5), then decayed by recency when ``recorded_date`` is present (no date = no decay).
    A row whose strength comes out at 0.0 (a score of exactly 60) is SKIPPED with a reason, never emitted as a zero signal.
    Signal.source is 'zoominfo_intent:<company_id>:<topic>'.
    One signal per (company, topic): the highest score wins. Every row that is not turned into a signal is appended to
    ``skipped`` as (row, reason) when a list is passed, so nothing disappears silently.
    """
    skipped = skipped if skipped is not None else []
    by_zi = {}
    for a in accounts:
        if _key(a.zoominfo_id) and _key(a.zoominfo_id) not in by_zi:
            by_zi[_key(a.zoominfo_id)] = a
    best: dict[tuple[str, str], tuple[float, dict]] = {}
    lo, hi = INTENT_SCORE_RANGE
    for row in rows:
        if not isinstance(row, dict):
            skipped.append(({"row": row}, "not a dict"))
            continue
        topic = str(row.get("topic") or "").strip()
        cid = _key(row.get("company_id"))
        score, aud = row.get("signal_score"), str(row.get("audience_strength") or "").strip().upper()
        if not topic or not cid:
            skipped.append((row, "missing topic or company_id"))
        elif topic.lower() not in INTENT_TOPICS:
            skipped.append((row, f"topic '{topic}' is not a Pendo-relevant intent topic"))
        elif isinstance(score, bool) or not isinstance(score, (int, float)) or not lo <= score <= hi:
            skipped.append((row, f"signal_score {score!r} outside {lo}-{hi}"))
        elif aud not in INTENT_AUDIENCE_FACTOR:
            skipped.append((row, f"audience_strength {row.get('audience_strength')!r} not in A-E"))
        elif cid not in by_zi:
            skipped.append((row, "no matching account"))
        else:
            k = (cid, topic.lower())
            if k not in best or score > best[k][0]:
                if k in best:
                    skipped.append((best[k][1], "superseded by a higher-scoring row for the same company and topic"))
                best[k] = (score, row)
            else:
                skipped.append((row, "superseded by a higher-scoring row for the same company and topic"))
    out: list[Signal] = []
    for (cid, _), (score, row) in sorted(best.items()):
        aud = str(row["audience_strength"]).strip().upper()
        base = min(INTENT_MAX_STRENGTH, (score - lo) / (hi - lo) * INTENT_AUDIENCE_FACTOR[aud])
        when = _parse_intent_date(row.get("recorded_date"))
        strength = round(min(1.0, max(0.0, base * (recency_factor(age_days(when, today), half_life_for(SignalKind.INTENT)) if when else 1.0))), 4)
        topic = str(row["topic"]).strip()
        if strength <= 0.0:
            skipped.append((row, f"strength 0.0 (signal_score {int(score)} is at the bottom of the {lo}-{hi} range): not a trigger"))
            continue
        out.append(Signal(
            sf_account_id=by_zi[cid].sf_account_id, kind=SignalKind.INTENT, strength=strength,
            source=f"{INTENT_SOURCE_PREFIX}:{cid}:{topic.lower()}", source_date=when,
            evidence=f"ZoomInfo intent (UNVERIFIED shape): researching '{topic}', score {int(score)}, audience {aud}",
            zoominfo_id=cid,
        ))
    out.sort(key=_signal_sort_key)
    return out
