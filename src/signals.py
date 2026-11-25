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
from typing import Mapping, NamedTuple, Sequence

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
    SignalKind.INTENT: 0.30,          # UNVERIFIED connector shape: interest, not a dated event; capped low
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

#: Longest a scoop of each kind stays useful (days). People moves are a long buying window, so 90; partnerships,
#: hiring notes and intent fade faster. ``config.signals.max_age_days`` is only the default for a kind missing here
#: (today every kind is listed, so it never drops a scoop here). NOTE: the ranker ALSO reads
#: config.signals.max_age_days as its recency horizon (recency points = 1 - age / max_age_days); that is a separate use. The caller must PULL scoops far enough back:
#: a 90-day window for people-move scoops (Phase 2b), otherwise the 90-day limit never gets used.
KIND_MAX_AGE_DAYS: dict[SignalKind, int] = {
    SignalKind.EXEC_MOVE: 90, SignalKind.NEW_HIRE: 90,
    SignalKind.FUNDING: 45, SignalKind.PRODUCT_LAUNCH: 45, SignalKind.MA: 45, SignalKind.PROJECT_PAIN: 45,
    SignalKind.PARTNERSHIP: 30, SignalKind.HIRING_PLAN: 30, SignalKind.INTENT: 30,
}

#: A signal weaker than this is not worth a call reason: dropped with the reason 'faded below floor'.
MIN_STRENGTH_FLOOR = 0.10

#: A kept people move whose role is probably a physical-goods 'product' role (retail, food, mining...) is KEPT but
#: flagged 'role ambiguous' and its strength is multiplied by this.
AMBIGUOUS_ROLE_FACTOR = 0.7

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
# ("Head of Product", "Chief Digital Officer", "VP, Customer Success", "Product Director", "IT Director";
# one free word may sit between: "Head of Platform Engineering") AND the words right after it do not turn it into
# a non-buyer function ("Head of Product Supply", "Director of Support Services (aged care)"). See _NEGATIVE.
# 'officer' is deliberately not a title word on its own ("Customer Support Officer" is junior); "Chief X Officer"
# matches through 'chief'. A role held by someone's assistant ("Executive Assistant to the CTO") does not count.
_TITLE = (r"chief|head|vp|svp|evp|avp|vice[\s-]+president|president|director|manager|lead|owner|"
          r"general\s+manager|gm")
_DOMAIN = (r"product|digital|customer\s+experience|cx|customer\s+success|customer\s+support|customer\s+service|"
           r"customer\s+care|customer\s+officer|support|user\s+experience|ux|engineering|technology|"
           r"information\s+technology|digital\s+workplace|employee\s+experience|transformation|mobile")
_SEP = r"[\s,/&:-]+"
_TITLE_THEN_DOMAIN = re.compile(
    rf"(?<!\w)(?:{_TITLE})(?!\w){_SEP}(?:(?:of|and)\s+)?(?:(?:global|regional|senior|group|apac|anz|asia\s+pacific)\s+)?"
    rf"(?:(?!(?:for|of|and|to|the|its|a|an|in|at|with|on)\b)\w+(?:-\w+)*[\s-]+)?(?:{_DOMAIN})(?!\w)", _FLAGS)
_DOMAIN_THEN_TITLE = re.compile(
    rf"(?<!\w)(?:{_DOMAIN})(?!\w){_SEP}(?:\w+{_SEP}){{0,1}}(?:{_TITLE})(?!\w)", _FLAGS)
# Acronyms and phrases that name a role on their own. 'IT' is matched case-sensitively so the pronoun 'it' never counts.
_ROLE_ACRONYMS = re.compile(
    r"(?<!\w)(?:CTO|CPO|CDO|CIO|CXO)(?!\w)"
    r"|(?i:chief\s+(?:technology|information|product|digital|experience|customer)\s+officer)"
    )
# "Chief X and Y Officer" in general: counts when X or Y is a buyer domain word ("Chief Customer and Digital Officer",
# "Chief Information and Technology Officer"); "Chief Risk and Compliance Officer" does not.
_CHIEF_AND = re.compile(r"(?<!\w)chief\s+((?:[\w-]+\s+){1,2}?and\s+(?:[\w-]+\s+){1,2}?)officer(?!\w)", _FLAGS)
_CHIEF_AND_DOMAIN = re.compile(rf"(?<!\w)(?:{_DOMAIN}|information)(?!\w)", _FLAGS)
_IT_ROLE = re.compile(r"(?<!\w)(?:(?i:head|director|manager|lead|chief|vp|svp)[\s,]+(?:(?i:of)\s+)?IT(?!\w)|IT\s+(?i:director|manager|head|lead)(?!\w))")
# CIO also abbreviates Chief Investment Officer: not a buyer when the sentence is about investments or assets.
_CIO_INVESTMENT = re.compile(
    r"(?<!\w)(?:invest\w*|portfolios?|funds?\s+under\s+management|assets?(?:\s+under\s+management)?|superannuation|aum)(?!\w)", _FLAGS)
# A role named as someone else's past or predecessor post ("a former CIO", "replaces the former Head of Digital",
# "previously Chief Product Officer at a retailer") is not the new leader.
_FORMER_BEFORE = re.compile(
    r"(?:(?:former|formerly|previously|ex-?)[\s-]+(?:(?:the|a|an|as|was|at|of|its|interim|acting)\s+){0,2}"
    r"|(?:replaces|replacing|replaced|succeeds|succeeding)\s+(?:the\s+)?(?:(?:former|outgoing|departing)\s+)?)$", _FLAGS)
# Board roles are not buyers, and a buyer title in a board note is background ("appointed to its Board; previously CPO").
_BOARD = re.compile(r"(?<!\w)(?:board|chair\w*|non-?\s?executive|non-?\s?exec)(?!\w)", _FLAGS)
# Someone else's role: "Executive Assistant to the CTO", "PA to the Head of Product".
_ASSISTANT_BEFORE = re.compile(
    r"(?:assistant|\bPA|\bEA|secretary|chief\s+of\s+staff)\s+(?:to|for)\s+(?:the\s+|its\s+|our\s+|a\s+)?$", _FLAGS)
# Words that, inside the role phrase or the 3 words after it, mean the role is some other function
# ("Head of Product Supply and Logistics", "Technology Procurement Director", "Head of Product Compliance").
# Marketing is here too, which is why "Product Marketing Manager" and "Digital Marketing Lead" drop.
_NEGATIVE = re.compile(
    r"(?<!\w)(?:supply|compliance|procurement|purchasing|logistics|manufactur\w*|marketing|social\s+media|finance|"
    r"financial|legal|facilit\w+|safety|sales|risk|warehouse|aged\s+care|hospital|clinical|nursing|network|"
    r"support\s+services?|human\s+resources|hr|people|recruit\w*)(?!\w)", _FLAGS)
# Physical-goods qualifiers. A 'product' role followed by one of these (within 4 words) is a goods role, not a
# digital product role: "Product Manager, Dairy", "Head of Product, Wine Portfolio", "Product Quality Assurance".
_PHYSICAL = re.compile(
    r"(?<!\w)(?:food|dairy|wine|beverage|plant|factory|fmcg|grocery|groceries|produce|supermarket|apparel|fresh|"
    r"meat|bakery|pharma\w*|mining|merchandis\w*|agricultur\w*|farm\w*|textile|quality\s+assurance|assurance)"
    r"(?:s|es)?(?!\w)",
    _FLAGS)
# Softer industry context elsewhere in the description: the 'product' role may still be physical goods. The
# scoop is KEPT but flagged ambiguous (strength x AMBIGUOUS_ROLE_FACTOR).
_INDUSTRY_CONTEXT = re.compile(
    r"(?<!\w)(?:retail\w*|supermarkets?|grocery|stores?|manufactur\w*|factory|wholesale|mining|farm\w*|food|"
    r"beverages?|consumer\s+goods|fmcg|hardware|equipment|pharma\w*|construction)(?!\w)", _FLAGS)
# A product role followed by a comma and a category word ("Head of Product, Footwear", "Product Lead, Home Loans")
# is probably a goods or line-of-business role. Digital words and geography after the comma are fine ("Head of
# Product, Digital", "Product Manager, ANZ"); anything else makes the role ambiguous (kept, flagged).
_COMMA_QUALIFIER = re.compile(r"\s*,\s*([A-Za-z][A-Za-z&'-]*)")
_QUALIFIER_OK = frozenset({
    "digital", "mobile", "app", "apps", "platform", "platforms", "online", "web", "ecommerce", "e-commerce", "software",
    "data", "cloud", "saas", "ai", "customer", "experience", "growth", "design", "engineering", "technology", "anz",
    "apac", "australia", "new", "nz", "asia", "global", "regional", "emea", "pacific", "and", "the", "a", "an",
    "who", "will", "to", "as", "after", "from", "at", "formerly", "previously", "effective", "joining", "based"})
# The tail of a role phrase stops at these words ("Chief Customer Officer to unify sales, success and support").
_TAIL_STOP = frozenset({"to", "who", "will", "as", "after", "that", "which", "previously", "formerly", "from", "at"})


@dataclass(frozen=True)
class RoleMatch:
    """Result of role_match: the matched role phrase (None = no relevant role) and whether it is ambiguous."""

    role: str | None
    ambiguous: bool = False


def _tail_words(text: str, end: int, limit: int) -> list[str]:
    """Up to ``limit`` words after ``end``, stopping at a sentence break or a _TAIL_STOP word."""
    tail = re.split(r"[.;]", text[end:], maxsplit=1)[0]
    out: list[str] = []
    for w in re.findall(r"[A-Za-z&]+", tail):
        if w.lower() in _TAIL_STOP or len(out) >= limit:
            break
        out.append(w)
    return out


def role_match(description: str) -> RoleMatch:
    """Find a Pendo-buyer role in a people-move description.

    Relevant: product / digital / CX / customer success / support / engineering / technology / mobile / IT /
    digital-workplace roles. Not relevant: a bare 'CEO' or 'Chief Executive Officer', CFO, finance, legal, an
    assistant to a CTO, a buyer word inside a non-buyer role (_NEGATIVE), a product role in physical goods (_PHYSICAL).
    ``ambiguous`` is True for a kept 'product' role when the rest of the description sounds like retail / food /
    manufacturing (_INDUSTRY_CONTEXT): keep it, but the caller should say so and weaken the signal."""
    text = description
    if _BOARD.search(text):
        return RoleMatch(None)
    for rx in (_ROLE_ACRONYMS, _CHIEF_AND, _IT_ROLE, _TITLE_THEN_DOMAIN, _DOMAIN_THEN_TITLE):
        for m in rx.finditer(text):
            before = re.split(r"[.;]", text[max(0, m.start() - 40):m.start()])[-1]
            if _ASSISTANT_BEFORE.search(text[max(0, m.start() - 30):m.start()]) or _FORMER_BEFORE.search(before):
                continue
            if rx is _CHIEF_AND and not _CHIEF_AND_DOMAIN.search(m.group(1)):
                continue
            if m.group(0) == "CIO" or m.group(0).upper() == "CIO":
                sentence = next((x for x in re.split(r"(?<=[.;])\s+", text) if m.group(0) in x), text)
                if _CIO_INVESTMENT.search(sentence):
                    continue
            if _NEGATIVE.search(m.group(0) + " " + " ".join(_tail_words(text, m.end(), 3))):
                continue
            is_product = bool(re.search(r"(?<!\w)product", m.group(0), _FLAGS))
            if is_product and _PHYSICAL.search(m.group(0) + " " + " ".join(_tail_words(text, m.end(), 4))):
                continue
            q = _COMMA_QUALIFIER.match(text, m.end())
            qualifier = bool(q) and q.group(1).lower() not in _QUALIFIER_OK
            ambiguous = is_product and (bool(_INDUSTRY_CONTEXT.search(text)) or qualifier)
            return RoleMatch(" ".join(m.group(0).split()), ambiguous)
    return RoleMatch(None)


def relevant_role(description: str) -> str | None:
    """The matched role phrase, or None. See role_match for the rules."""
    return role_match(description).role


# Hiring notes need a buyer-role context before a keep keyword may beat a drop keyword (or count at all).
# A role (relevant_role) or one of these phrases counts; bare weak words (support, platform, app, engineering,
# mobile, launch, transformation) never do, because "support staff for the hospital ward" and
# "drivers for its delivery platform" contain them.
_HIRING_CONTEXT = re.compile(
    r"(?<!\w)(?:product\s+(?:managers?|owners?|team|leads?|designers?|analysts?|operations)|ux|cx|"
    r"customer\s+(?:success|experience)|mobile\s+(?:app\s+)?(?:developers?|engineers?)|"
    r"digital\s+(?:product|experience|transformation|workplace)|software\s+(?:engineers?|developers?)|"
    r"engineering\s+(?:managers?|leads?|team)|cto)(?!\w)", _FLAGS)
#: Weak keywords from config keywords_keep: never enough on their own for a hiring note. A bare 'product' is weak too
#: ("Product Compliance Officer", "Product Safety Auditor"); 'product manager / owner / team...' is in _HIRING_CONTEXT.
WEAK_KEEP_KEYWORDS = frozenset({"support", "app", "platform", "engineering", "mobile", "launch", "transformation", "product"})


def hiring_context(description: str) -> str | None:
    """Buyer-role context in a hiring note: a relevant role, or a strong role phrase. None if absent."""
    for m in _HIRING_CONTEXT.finditer(description):
        # same non-buyer word list as people moves: "Product Manager, Legal Operations" is not a buyer role
        if not _NEGATIVE.search(m.group(0) + " " + " ".join(_tail_words(description, m.end(), 3))):
            return " ".join(m.group(0).split())
    return relevant_role(description)


# Investor-side wording. A Funding scoop that says the company PUT money in (or backs someone) is about the
# investor, not a company that received funding.
_INVESTOR_VERB = re.compile(
    r"(?<!\w)(?:invest(?:ed|s|ing|ment)"
    r"|participat(?:ed|es|ing)\s+in"
    r"|(?:co-)?(?:led|leads|leading)\s+(?:an?|the)\b[^.;]{0,40}?(?:round|investment|raise|funding|series\s+[a-z]|seed)"
    r"|(?:taken|took|takes|taking|acquired|acquires|buys|bought)\s+an?\s+(?:\w+\s+)?stake\s+in"
    r"|(?:back|backed|backs|backing)(?!\s+by))(?!\w)", _FLAGS)
_BACKED_BY = re.compile(r"(?<!\w)backed\s+by(?!\w)", _FLAGS)
# Words that say the company itself received money. 'round' and 'backed by' are deliberately NOT here: investors
# also 'participate in a round'.
_FUNDING_RECEIVED = re.compile(r"(?<!\w)(?:raised|raises|secured|secures|received|receives|closed|closes|funding|series\s+[a-z]|seed)(?!\w)", _FLAGS)
_RECEIVED_VERB = re.compile(r"(?<!\w)(?:raised|raises|secured|secures|received|receives|closed|closes)(?!\w)", _FLAGS)


def _funding_drop_reason(scoop: Scoop) -> str | None:
    """Why a Funding scoop is not about a company that received money, or None when it is.
    1. No investor wording: a normal funding note (kept).
    2. The company is the grammatical subject of the investor verb (name, then the verb, in one clause, with no
       'raised/secured/received' in between): the company is the investor (dropped).
    3. The company comes AFTER the verb ('Fund Y led a round in <company>'): it is the investee (kept).
    4. Otherwise: kept only if something says money was received.
    'Backed by' with no money received is not a funding event (dropped)."""
    text = scoop.description or ""
    name = re.sub(r"[^\w&' -]", "", scoop.company_name).strip().split(" ")[0] if scoop.company_name.strip() else ""
    m = _INVESTOR_VERB.search(text)
    if not m:
        if _BACKED_BY.search(text) and not _FUNDING_RECEIVED.search(text):
            return "not a funding event: only says the company is backed by an investor, no funds received"
        return None
    if name:
        for nm in re.finditer(rf"(?<!\w){re.escape(name)}(?!\w)", text[:m.start()], _FLAGS):
            between = text[nm.end():m.start()]
            if len(between) <= 60 and not re.search(r"[;:\n]|\.\s", between) and not _RECEIVED_VERB.search(between):
                return "investor note: the company is the one investing, not a company that received funding"
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text[m.end():m.end() + 80], _FLAGS):
            return None
    if _FUNDING_RECEIVED.search(text):
        return None
    return "investor note: about the investor, not a company that received funding"


def _is_investor_note(scoop: Scoop) -> bool:
    """True when a Funding scoop is dropped by _funding_drop_reason (kept for older callers and tests)."""
    return _funding_drop_reason(scoop) is not None


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
    """Keep / drop decision for one scoop. ``reason`` is a short sentence for the call sheet, always filled
    ('Kept: Executive Move, role 'Head of Digital', 6 days ago' / 'Dropped: generic hiring note ...').
    ``kind`` is NONE when dropped. ``ambiguous`` marks a kept role that may be a physical-goods 'product' role."""

    scoop: Scoop
    keep: bool
    kind: SignalKind
    reason: str
    ambiguous: bool = False


AMBIGUOUS_NOTE = "role ambiguous: check it is a digital product role"


def verdict_summary(v: ScoopVerdict) -> str:
    """One-line, call-sheet-ready sentence for a verdict (the reason, whitespace-collapsed)."""
    return " ".join(v.reason.split())


def _lower_set(values) -> set[str]:
    return {v.strip().lower() for v in values}


def _age_phrase(days: int) -> str:
    if days < 0:
        return "today (future-dated, treated as today)"
    return "today" if days == 0 else ("1 day ago" if days == 1 else f"{days} days ago")


def max_age_for(kind: SignalKind, config: Config) -> int:
    """Age limit in days for a kind: KIND_MAX_AGE_DAYS, else the config default (signals.max_age_days)."""
    return KIND_MAX_AGE_DAYS.get(kind, config.signals.max_age_days)


def _strength(kind: SignalKind, days: float, ambiguous: bool = False) -> float:
    s = signal_strength(kind, days)
    return round(s * AMBIGUOUS_ROLE_FACTOR, 4) if ambiguous else s


def _judge_kind(kind: SignalKind, scoop: Scoop, config: Config) -> tuple[bool, str, bool]:
    """Apply the per-kind keep rules. Returns (keep, detail, ambiguous); the detail is a phrase, not a full sentence."""
    desc = scoop.description or ""
    topics = _lower_set(scoop.topics)
    sg = config.signals

    if kind is SignalKind.PROJECT_PAIN:
        hit = sorted(topics & PROJECT_TOPICS)
        if hit:
            return True, f"buyer topic {', '.join(hit)}", False
        return False, "Project/Pain Point without a Pendo-buyer topic (needs Customer Experience, Product Management, " \
                      "Customer Success, Product Development or Digital Workplace)", False

    if kind in (SignalKind.EXEC_MOVE, SignalKind.NEW_HIRE):
        rm = role_match(desc)
        if rm.role:
            return True, f"role '{rm.role}'", rm.ambiguous
        return False, "people move without a product/digital/CX/customer success/support/engineering/IT role in the description", False

    if kind is SignalKind.HIRING_PLAN:
        # Generic hiring (nurses, drivers, legal, 'more open roles in Medical & Health') is dropped unless the
        # note shows a buyer-role context. A topic hit counts as a keep signal but does NOT rescue a note that
        # also has a drop keyword and no role context. Weak keywords (WEAK_KEEP_KEYWORDS) never keep a note alone.
        context = hiring_context(desc)
        drop_kw = _first_hit(desc, sg.keywords_drop)
        if drop_kw and not context:
            return False, f"generic hiring note (matches drop keyword '{drop_kw}', no buyer-role context)", False
        if context:
            return True, f"buyer role '{context}'", False
        hit = sorted(topics & HIRING_TOPICS)
        if hit:
            return True, f"relevant topic {', '.join(hit)}", False
        strong = _first_hit(desc, [k for k in sg.keywords_keep if k.strip().lower() not in WEAK_KEEP_KEYWORDS])
        if strong:
            return True, f"buyer keyword '{strong}'", False
        weak = _first_hit(desc, sg.keywords_keep)
        extra = f" (only the weak keyword '{weak}')" if weak else ""
        return False, "hiring note with no relevant topic, buyer role or strong keyword" + extra, False

    if kind is SignalKind.FUNDING:
        why = _funding_drop_reason(scoop)
        if why:
            return False, why, False
    return True, "", False


def filter_scoops(scoops: list[Scoop], config: Config, today: date | datetime) -> list[ScoopVerdict]:
    """One verdict per input scoop, in input order. Never drops silently: every verdict has a reason sentence.

    Checks: scoop type (must be in ``signals.scoop_types_keep``, or be Project / Pain Point which has its own topic
    gate); then for each kind the scoop could be, in weight order: age limit for that kind (KIND_MAX_AGE_DAYS;
    exactly the limit is kept, one more day is dropped), the per-kind rules in ``_judge_kind``, and the strength floor
    (MIN_STRENGTH_FLOOR). The highest-weight kind that passes everything wins.
    """
    sg = config.signals
    allowed_types = _lower_set(sg.scoop_types_keep) | {"project", "pain point"}
    verdicts: list[ScoopVerdict] = []
    for s in scoops:
        days = (_as_date(today) - _as_date(s.published_date)).days
        types = [t for t in s.types if t.strip().lower() in allowed_types]
        if not types:
            shown = ", ".join(s.types) if s.types else "none"
            verdicts.append(ScoopVerdict(s, False, SignalKind.NONE, f"Dropped: scoop type not a Pendo trigger (types: {shown})"))
            continue
        kinds = {TYPE_TO_KIND[t.strip().lower()] for t in types if t.strip().lower() in TYPE_TO_KIND}
        if not kinds:
            # Configured as keep but this module has no kind for it: say so instead of guessing.
            verdicts.append(ScoopVerdict(s, False, SignalKind.NONE,
                                         f"Dropped: type {', '.join(types)} has no SignalKind mapping in signals.py"))
            continue
        passed: dict[SignalKind, tuple[str, bool]] = {}
        failed: list[str] = []
        for k in _by_weight(kinds):
            limit = max_age_for(k, config)
            if days > limit:
                failed.append(f"expired, {days} days old (limit {limit} for {k.value})")
                continue
            ok, why, ambiguous = _judge_kind(k, s, config)
            if not ok:
                failed.append(why)
                continue
            strength = _strength(k, max(0, days), ambiguous)
            if strength < MIN_STRENGTH_FLOOR:
                failed.append(f"faded below floor ({strength:.2f} < {MIN_STRENGTH_FLOOR:.2f}, {days} days old)")
                continue
            passed[k] = (why, ambiguous)
        if passed:
            best = _by_weight(passed)[0]
            detail, ambiguous = passed[best]
            label = next(t for t in types if TYPE_TO_KIND.get(t.strip().lower()) is best)
            parts = [label] + ([detail] if detail else []) + [_age_phrase(days)]
            reason = "Kept: " + ", ".join(parts) + (f"; {AMBIGUOUS_NOTE}" if ambiguous else "")
            verdicts.append(ScoopVerdict(s, True, best, reason, ambiguous))
        else:
            verdicts.append(ScoopVerdict(s, False, SignalKind.NONE, "Dropped: " + "; ".join(failed)))
    return verdicts


# ---------------------------------------------------------------- 2. scoops -> signals
class SignalBatch(NamedTuple):
    """Result of scoops_to_signals. Unpacks as ``signals, unmatched = ...``."""

    signals: list[Signal]          # one per kept scoop matched to an account, sorted deterministically
    unmatched: list[Scoop]         # kept scoops whose company_id matches no account (never discarded silently)


def _key(value) -> str:
    """Join-key normalisation: stripped string ('' for None)."""
    return "" if value is None else str(value).strip()


def _trim_evidence(text: str, reserve: int = 0) -> str:
    """Whitespace-collapsed text of at most EVIDENCE_MAX_CHARS - reserve characters (ellipsis when cut)."""
    text = " ".join((text or "").split())
    limit = EVIDENCE_MAX_CHARS - reserve
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


#: Appended to Signal.evidence of a kept people move whose 'product' role may be physical goods. The shared Signal
#: model has no flag field, so the warning travels with the text the opener would quote. Use is_ambiguous(signal).
AMBIGUOUS_SUFFIX = f" ({AMBIGUOUS_NOTE})"


def is_ambiguous(signal: Signal) -> bool:
    """True when the signal's role was flagged ambiguous (see AMBIGUOUS_SUFFIX)."""
    return signal.evidence.endswith(AMBIGUOUS_SUFFIX)


def _signal_sort_key(sig: Signal):
    ts = sig.source_date.timestamp() if sig.source_date else float("-inf")
    return (-sig.strength, -ts, sig.kind.value, sig.source)


def scoops_to_signals(verdicts: list[ScoopVerdict],
                      accounts_by_zoominfo_id: Mapping[str, Account | Sequence[Account]],
                      config: Config, today: date | datetime) -> SignalBatch:
    """Turn KEPT verdicts into Signals joined to accounts by ``Scoop.company_id == Account.zoominfo_id``.

    ``config`` is accepted for interface symmetry (and future per-kind weights); strength comes from the
    module constants KIND_BASE_WEIGHT, RECENCY_HALF_LIFE_DAYS and KIND_HALF_LIFE_DAYS. A repeated scoop id is used once.
    Join keys are compared as stripped strings, so ' 123 ' and 123 both match '123'.
    Several scoops on one account all become signals; use ``best_signal`` / ``combined_strength``.
    A mapping value may be one Account or a list of Accounts that share the ZoomInfo id (accounts.group_by_zoominfo_id):
    every account in the list gets its own copy of the signal (same kind, strength, source, date and evidence; only
    ``sf_account_id`` differs). An empty list counts as no match. A kept role flagged ambiguous gets AMBIGUOUS_SUFFIX
    on its evidence (``is_ambiguous(signal)``).
    """
    signals: list[Signal] = []
    unmatched: list[Scoop] = []
    seen: set[str] = set()
    lookup: dict[str, list[Account]] = {}
    for k, v in accounts_by_zoominfo_id.items():
        if _key(k):
            lookup[_key(k)] = [v] if isinstance(v, Account) else list(v)
    for v in verdicts:
        if not v.keep or v.scoop.id in seen:
            continue
        seen.add(v.scoop.id)
        s = v.scoop
        accts = lookup.get(_key(s.company_id)) or []
        if not accts:
            unmatched.append(s)
            continue
        for acct in accts:
            signals.append(Signal(
                sf_account_id=acct.sf_account_id,
                kind=v.kind,
                strength=_strength(v.kind, age_days(s.published_date, today), v.ambiguous),
                source=f"{SOURCE_PREFIX}:{s.id}",
                source_date=s.published_date,
                evidence=(_trim_evidence(s.description, len(AMBIGUOUS_SUFFIX)) + AMBIGUOUS_SUFFIX) if v.ambiguous
                else _trim_evidence(s.description),
                zoominfo_id=_key(s.company_id),
            ))
    signals.sort(key=lambda x: (*_signal_sort_key(x), x.sf_account_id or ""))
    unmatched.sort(key=lambda sc: (sc.company_id, sc.id))
    return SignalBatch(signals, unmatched)


def _real_signals(signals: list[Signal]) -> list[Signal]:
    return [x for x in signals if x.kind is not SignalKind.NONE]


def best_signal(signals: list[Signal]) -> Signal | None:
    """Strongest signal; ties go to the newer one, then kind name, then source (deterministic).
    An INTENT signal is never the best while the account has any real (non-intent) signal: the opener must quote
    a dated event, not an unverified interest signal. None for an empty list: use ``no_trigger_signal(account)``."""
    real = _real_signals(signals)
    dated = [x for x in real if x.kind is not SignalKind.INTENT]
    pool = dated or real
    return min(pool, key=_signal_sort_key) if pool else None


def combined_strength(signals: list[Signal]) -> float:
    """Diminishing returns over all signals of one account, in 0.0-1.0.
    The best counts fully, each other counts at COMBINE_DAMPING: 1 - (1-best) * prod(1 - d*other).
    When the account has any real (non-intent) signal, the best is chosen among those only, so intent can only
    count as a damped 'other'."""
    real = _real_signals(signals)
    dated = sorted((x.strength for x in real if x.kind is not SignalKind.INTENT), reverse=True)
    intent = sorted((x.strength for x in real if x.kind is SignalKind.INTENT), reverse=True)
    strengths = dated + intent if dated else intent
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
    (use ``no_trigger_signal`` when you need an explicit "none"). Accounts that share a zoominfo_id all get an entry
    (the same scoops attach to each).
    """
    if top_n is not None and top_n < 0:
        raise ValueError(f"top_n must be >= 0 or None, got {top_n}")
    by_zi: dict[str, list[Account]] = {}
    for a in accounts:
        if _key(a.zoominfo_id):
            by_zi.setdefault(_key(a.zoominfo_id), []).append(a)
    batch = scoops_to_signals(filter_scoops(scoops, config, today), by_zi, config, today)

    grouped: dict[tuple[str, str], list[Signal]] = {}
    for sig in batch.signals:
        grouped.setdefault((sig.zoominfo_id or "", sig.sf_account_id or ""), []).append(sig)

    entries: list[RadarEntry] = []
    for (zi, sf), sigs in grouped.items():
        sigs.sort(key=_signal_sort_key)
        best = best_signal(sigs)
        acct = next(a for a in by_zi[zi] if (a.sf_account_id or "") == sf)
        entries.append(RadarEntry(acct, best, combined_strength(sigs), _why(best, len(sigs), today), tuple(sigs)))

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
#: so the strength is also capped at INTENT_MAX_STRENGTH (= the INTENT base weight, 0.3) while the shape is UNVERIFIED.
INTENT_AUDIENCE_FACTOR = {"A": 1.0, "B": 0.85, "C": 0.7, "D": 0.55, "E": 0.4}
INTENT_MAX_STRENGTH = KIND_BASE_WEIGHT[SignalKind.INTENT]   # 0.3
#: A row with no recorded_date is treated as this many days old (never as brand new).
INTENT_UNDATED_AGE_DAYS = 21
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
    INTENT_MAX_STRENGTH (0.3), then decayed with the intent half-life (21 days) from ``recorded_date``; a missing date
    counts as INTENT_UNDATED_AGE_DAYS (21) old. Older than KIND_MAX_AGE_DAYS[INTENT] (30), or below MIN_STRENGTH_FLOOR
    ('faded below floor'), is skipped with a reason. A strength of 0.0 (a score of exactly 60) is skipped too, never
    emitted as a zero signal. Evidence says 'interest signal, not a dated event' so the call sheet can label it.
    Signal.source is 'zoominfo_intent:<company_id>:<topic>'.
    One signal per (company, topic): the highest score wins. Every row that is not turned into a signal is appended to
    ``skipped`` as (row, reason) when a list is passed, so nothing disappears silently.
    """
    skipped = skipped if skipped is not None else []
    by_zi: dict[str, list[Account]] = {}
    for a in accounts:
        if _key(a.zoominfo_id):
            by_zi.setdefault(_key(a.zoominfo_id), []).append(a)
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
        days = age_days(when, today) if when else INTENT_UNDATED_AGE_DAYS
        strength = round(min(1.0, max(0.0, base * recency_factor(days, half_life_for(SignalKind.INTENT)))), 4)
        topic = str(row["topic"]).strip()
        if days > KIND_MAX_AGE_DAYS[SignalKind.INTENT]:
            skipped.append((row, f"expired, {days} days old (limit {KIND_MAX_AGE_DAYS[SignalKind.INTENT]} for intent)"))
            continue
        if base <= 0.0:
            skipped.append((row, f"strength 0.0 (signal_score {int(score)} is at the bottom of the {lo}-{hi} range): not a trigger"))
            continue
        if strength < MIN_STRENGTH_FLOOR:
            skipped.append((row, f"faded below floor ({strength:.2f} < {MIN_STRENGTH_FLOOR:.2f})"))
            continue
        for acct in by_zi[cid]:        # accounts sharing a ZoomInfo id each get the signal
            out.append(Signal(
                sf_account_id=acct.sf_account_id, kind=SignalKind.INTENT, strength=strength,
                source=f"{INTENT_SOURCE_PREFIX}:{cid}:{topic.lower()}", source_date=when,
                evidence=(f"ZoomInfo intent (UNVERIFIED shape): interest signal, not a dated event. "
                          f"Researching '{topic}', score {int(score)}, audience {aud}"),
                zoominfo_id=cid,
            ))
    out.sort(key=_signal_sort_key)
    return out
