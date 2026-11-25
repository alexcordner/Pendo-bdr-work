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
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
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
    SignalKind.FUNDING: 0.60,         # money to invest in the product (phase 2 dry run: lowered from 0.70)
    SignalKind.NEW_HIRE: 0.65,        # relevant hire that is not framed as an exec move
    SignalKind.MA: 0.50,              # being acquired (integration, leadership change) or a full acquisition; variants below
    SignalKind.HIRING_PLAN: 0.55,     # hiring product / CX / engineering roles
    SignalKind.PARTNERSHIP: 0.30,     # often marketing noise; 0.20 without product / digital / customer words
    SignalKind.INTENT: 0.30,          # UNVERIFIED connector shape: interest, not a dated event; capped low
    SignalKind.NONE: 0.0,
}

#: Variant weights inside a kind (the SignalKind enum is shared and has no sub-kinds). A variant is a multiplier on the
#: kind's base weight, applied in the verdict (``ScoopVerdict.factor``) and shown to the sheet as a note on the evidence.
#: Product launch: 0.80 for a digital product, 0.40 for a physical or unclear one.
#: M&A: 0.50 target, 0.50 acquirer of a digital business or a full acquisition, 0.25 acquirer of a minority stake or an
#: unrelated business, 0.25 direction unclear. Partnership: 0.30, or 0.20 without product / digital / customer words.
LAUNCH_NON_DIGITAL_WEIGHT = 0.40
LAUNCH_FINANCIAL_WEIGHT = 0.20      # property / fund / financial product launch (0.25 x 0.8)
FUNDING_DEBT_WEIGHT = 0.25          # debt facility, loan, bond, credit line: not a growth round
MA_TARGET_MINORITY_WEIGHT = 0.35    # target of a minority stake purchase: a holder, not a leadership/platform change
MA_RUMOUR_FACTOR = 0.5              # 'reportedly in talks to acquire': x0.5 of the acquirer weight
MA_MINORITY_OR_UNRELATED_WEIGHT = 0.25
MA_UNCLEAR_WEIGHT = 0.25
PARTNERSHIP_NO_PRODUCT_WEIGHT = 0.20

#: Recency decay is exponential: factor = 0.5 ** (age_days / half_life). Default half-life 21 days:
#: 1.0 today, 0.5 at 3 weeks, ~0.23 at the 45-day config cut-off.
RECENCY_HALF_LIFE_DAYS = 21.0
#: Per-kind overrides. A new leader's buying window lasts months, so people moves decay more slowly.
#: Every kind not listed here uses RECENCY_HALF_LIFE_DAYS. The config's max_age_days cut-off still applies.
KIND_HALF_LIFE_DAYS: dict[SignalKind, float] = {
    SignalKind.EXEC_MOVE: 45.0,
    SignalKind.NEW_HIRE: 45.0,
}

#: Several signals on one account (combined_strength): the strongest signal of each KIND counts in full for the best
#: one and at COMBINE_DAMPING for the best of every other kind. Extra signals of a kind that already counted add at most
#: SAME_KIND_EXTRA_EACH each (SAME_KIND_EXTRA_TOTAL together), and only the MAX_COMBINED_SIGNALS strongest counted entries
#: take part at all, so nine launch scoops for one company cannot push an account towards 1.0.
#: combined = 1 - (1 - best) * prod(1 - contribution). Always >= best and <= 1.0.
COMBINE_DAMPING = 0.5
SAME_KIND_EXTRA_EACH = 0.15
SAME_KIND_EXTRA_TOTAL = 0.30
MAX_COMBINED_SIGNALS = 4        # counted entries: the best plus up to three others
#: Two scoops of one company about the same product count once when this close in time.
NEAR_DUPLICATE_DAYS = 14
NEAR_DUPLICATE_CHARS = 60

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
    factor: float = 1.0                       # variant multiplier on the kind's base weight (launch / M&A / partnership)
    notes: tuple[str, ...] = ()               # warnings that travel on Signal.evidence as ' (note)' suffixes
    event_date: datetime | None = None        # incoming leader's start date, when the description gives one
    same_deal_as: tuple[str, ...] = ()        # other companies in the batch on the same M&A deal


AMBIGUOUS_NOTE = "role ambiguous: check it is a digital product role"
LAUNCH_NOTE = "physical or unclear product launch: check it is digital"
MA_TARGET_NOTE = "being acquired: leadership/platform change likely"
MA_ACQUIRER_FULL_NOTE = "acquirer of a digital business or a full acquisition"
MA_MINORITY_NOTE = "acquirer of a minority stake or unrelated business"
MA_UNCLEAR_NOTE = "M&A direction unclear: check the deal"
PARTNERSHIP_NOTE = "partnership without product, digital or customer words: check relevance"
DEBT_NOTE = "debt facility, not an equity round"
FINANCIAL_LAUNCH_NOTE = "financial or property product launch, not a digital product"
RUMOUR_NOTE = "reported talks, not a confirmed deal"
MA_TARGET_MINORITY_NOTE = "minority stake purchase: weak trigger"
VACATED_NOTE = "role vacated: new leader likely"
SAME_DEAL_PREFIX = "same deal as "


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


def _strength(kind: SignalKind, days: float, ambiguous: bool = False, factor: float = 1.0) -> float:
    """signal_strength x the variant factor x (ambiguous role: AMBIGUOUS_ROLE_FACTOR), rounded to 4 dp."""
    s = signal_strength(kind, days) * factor
    if ambiguous:
        s *= AMBIGUOUS_ROLE_FACTOR
    return round(min(1.0, max(0.0, s)), 4)


# ---------------------------------------------------------------- relevance classifiers (phase 2 dry-run fixes)
class _J(NamedTuple):
    """Result of judging one kind: keep?, a detail phrase, role-ambiguous?, variant factor, evidence notes, event date."""

    ok: bool
    detail: str = ""
    ambiguous: bool = False
    factor: float = 1.0
    notes: tuple[str, ...] = ()
    event_date: datetime | None = None


# Product launch: the full weight needs a DIGITAL product. "Lab consumables", "a new range of cleaning products"
# are physical or unclear: kept at LAUNCH_NON_DIGITAL_WEIGHT with a visible note.
_DIGITAL_PRODUCT = re.compile(
    r"(?<!\w)(?:apps?|platforms?|software|digital|mobile|portal|online|website|web|saas|ai|artificial\s+intelligence|"
    r"cloud|api|apis|customer\s+experience|cx|ux|e-?commerce|banking\s+app|internet\s+banking|self-service|"
    r"dashboard|chatbot|login|subscription\s+service|solutions?|technology|applications?|webapps?|online\s+(?:store|shop))(?!\w)", _FLAGS)
# Hardware / lab / equipment words. When one is present the launch is NOT digital unless the text names a software
# product itself (_SOFTWARE_NOUN); weak words such as platform, user interface, AI, cloud, mobile, digital do not rescue it.
_HARDWARE_PRODUCT = re.compile(
    r"(?<!\w)(?:robotics?|robots?|instruments?|consumables?|sampling|samplers?|emitters?|cameras?|sensors?|devices?|"
    r"hardware|laborator(?:y|ies)|chromatograph\w*|equipment|powders?|reagents?|assays?|spectromet\w+|analy[sz]ers?|"
    r"wearables?|drones?|batter(?:y|ies)|microscop\w+|centrifuges?|pipettes?)(?!\w)", _FLAGS)
_SOFTWARE_NOUN = re.compile(
    r"(?<!\w)(?:software|apps?|applications?|webapps?|saas|portal|website|apis?|e-?commerce|banking\s+app|chatbot|"
    r"web\s+app\w*|online\s+(?:store|shop))(?!\w)", _FLAGS)
_CAMEL_AI = re.compile(r"[a-z]AI(?!\w)")            # product names such as 'SignalAI' (case-sensitive on purpose)
# Financial or property products are not digital products: fund, REIT, trust, portfolio, ETF, property, mortgage...
# A bank / insurer / super fund launching an APP or platform still matches _DIGITAL_PRODUCT first and counts as digital.
_FINANCIAL_PRODUCT = re.compile(
    r"(?<!\w)(?:funds?|reits?|trusts?|portfolios?|etfs?|propert(?:y|ies)|mortgages?|insurance\s+(?:policy|policies|product)|"
    r"loan\s+products?|home\s+loans?|term\s+deposits?|superannuation\s+product|annuit(?:y|ies))(?!\w)", _FLAGS)
# Funding: debt wording means a facility, not a growth round, unless equity wording is also present.
_DEBT = re.compile(
    r"(?<!\w)(?:debt|loans?|credit\s+(?:facilit\w+|lines?)|credit|(?:senior\s+|convertible\s+|secured\s+)?notes|bonds?|"
    r"debentures?|revolving|refinanc\w+|borrowing|facilit(?:y|ies)|mortgage)(?!\w)", _FLAGS)
_EQUITY = re.compile(r"(?<!\w)(?:equity|series\s+[a-f]|seed|venture|ipo|private\s+placement|pre-ipo|growth\s+round)(?!\w)", _FLAGS)
# Rumour wording on an M&A scoop.
_RUMOUR = re.compile(r"(?<!\w)(?:reportedly|in\s+talks|rumou?r\w*|understood\s+to|said\s+to|considering|exploring|eyeing|weighing|approach\w*)(?!\w)", _FLAGS)

# Compute / capacity / ARR contracts ('AI compute contracts', '$4bn ARR contracts', data-centre capacity, power supply)
# are infrastructure supply deals, not partnerships a Pendo buyer would care about: dropped unless the text also has
# product / digital / customer wording (_PARTNERSHIP_WORDS).
_INFRASTRUCTURE_DEAL = re.compile(
    r"(?<!\w)(?:compute|gpus?|data[\s-]?cent(?:re|er)s?|capacity|megawatts?|mw|arr|annual\s+recurring\s+revenue|"
    r"colocation|co-location|hyperscal\w*|power\s+purchase|offtake|take-or-pay|contracted\s+revenue|hosting)(?!\w)", _FLAGS)
# Partnership: product / digital / customer wording makes it worth 0.30, otherwise 0.20. Deliberately narrow:
# 'AI', 'data', 'cloud', 'infrastructure' alone do not count (an AI data-centre capacity deal is not a Pendo trigger).
_PARTNERSHIP_WORDS = re.compile(
    r"(?<!\w)(?:products?|digital|customers?|customer\s+experience|apps?|platforms?|software|mobile|portal|online|"
    r"e-?commerce|website|ux|cx)(?!\w)", _FLAGS)

# ---- M&A direction. SignalKind has one MA kind, so direction is a variant weight, not a new kind.
_DEAL_PASSIVE = re.compile(
    r"(?<!\w)(?:(?:agreed\s+|set\s+|expected\s+|poised\s+|likely\s+)?to\s+be\s+(?:acquired|bought|purchased|taken\s+over)\s+by"
    r"|(?:has|have|had|is|was)\s+(?:been\s+)?(?:acquired|bought|taken\s+over)\s+by"
    r"|(?:acquired|bought|taken\s+over)\s+by|sold\s+to|merg(?:er|e|ing)\s+with"
    r"|takeover\s+(?:bid|offer|approach|proposal)\s+(?:from|by))(?!\w)", _FLAGS)
_DEAL_ACTIVE = re.compile(
    r"(?<!\w)(?:(?:to\s+)?acquire|acquires|acquired|acquiring|(?:to\s+)?buy|buys|bought|(?:to\s+)?purchase|purchases|"
    r"(?:to\s+)?take\s+over|takes\s+over|snaps?\s+up|"
    r"(?:(?:to\s+)?takes?|taking|(?:has\s+)?taken|took)\s+(?:an?\s+)?(?:\S+\s+)?stake\s+in|"
    r"(?:takeover|acquisition|buyout)\s+(?:bid\s+|offer\s+)?(?:of|for)|"
    r"(?:lifts?|raises?|increases?|boosts?)\s+(?:its\s+)?(?:holding|stake|shareholding)\s+(?:in\s+\S+\s+)?to|"
    r"(?:takes?|taking|took|taken)\s+(?:an?\s+)?\d{1,3}(?:\.\d+)?\s?%\s+of)(?!\w)", _FLAGS)
# Funding scoops that are really "X is being acquired" (ZoomInfo types them Funding): reclassified as M&A.
_TARGET_WORDING = _DEAL_PASSIVE
_MONEY_RECEIVED = re.compile(r"(?<!\w)(?:raised|raises|secured|secures|received|receives|closed|closes|series\s+[a-z]|seed)(?!\w)", _FLAGS)
_NUM_PCT = r"(\d{1,3}(?:\.\d+)?)\s?(?:%|per\s?cent|percent)"      # 30%, 30 per cent, 30 percent
_PERCENT = re.compile(_NUM_PCT, _FLAGS)
# '<n>% of <target>' counts only right after an acquire / holding verb: 'to acquire 30% of Acme', 'buys 19.9% of Acme',
# 'lifts holding to 30% of Acme'. A percentage about revenue or staff ('which generates 30% of its revenue') does not.
_PERCENT_OF_VERB = (r"(?:acquir\w*|buy|buys|buying|bought|purchas\w*|take|takes|taking|took|taken|holding|stake|"
                    r"shareholding|hold|holds|holding)")
_STAKE = re.compile(r"(?<!\w)(?:stake|shareholding|minority\s+interest)(?!\w)", _FLAGS)
_MAJORITY = re.compile(r"(?<!\w)(?:majority|controlling|100\s?(?:%|per\s?cent|percent)|full|all\s+of|outright|wholly|remaining)(?!\w)", _FLAGS)
_DIGITAL_BUSINESS = re.compile(
    r"(?<!\w)(?:software|digital|data|platform|cloud|technology|tech|saas|apps?|analytics|ai|product|customer\s+experience|"
    r"cx|fintech|online|e-?commerce|mobile|cyber\w*)(?!\w)", _FLAGS)
_NON_DIGITAL_SECTOR = re.compile(
    r"(?<!\w)(?:energy|mining|oil|gas|property|real\s+estate|resources|agricultur\w*|farm\w*|construction|utilit\w+|"
    r"pharma\w*|food|beverage|hotels?|hospitality|shipping|freight|steel|cement|coal|lithium|gold)(?!\w)", _FLAGS)


def _name_token(company_name: str) -> str:
    cleaned = re.sub(r"[^\w&' -]", "", company_name or "").strip()
    return cleaned.split(" ")[0] if cleaned else ""


def _mentions(text: str, name: str, possessive: bool = True) -> bool:
    """True when ``name`` appears as a whole word. With possessive=False an occurrence like "Acme's market" does not
    count (the thing named is Acme's market, not Acme)."""
    tail = "" if possessive else r"(?!['\u2019]s(?!\w))"
    return bool(name) and bool(re.search(rf"(?<!\w){re.escape(name)}(?!\w){tail}", text, _FLAGS))


# "agreement for X to acquire", "deal for X to buy", "allows X to acquire": X is the buyer, not the company named earlier.
# The buyer span follows ANY 'for' ('a scheme of arrangement for Bigco to acquire Acme', 'approval from FIRB for Bigco to
# acquire Acme', 'agreement for X to acquire'), or allow / enable / permit. A 'for' phrase only counts when the span
# names a company (a capitalised word): 'approval for the deal to acquire Zeta' has no third-party buyer.
_THIRD_PARTY_SUBJECT = re.compile(
    r"(?<!\w)(?:for|allow|allows|allowed|allowing|enable|enables|enabling|permit|permits)"
    r"\s+(?P<subject>(?:(?!\bto\b)[^.;]){1,100}?)\s*$", _FLAGS)
_CAPITALISED_WORD = re.compile(r"(?<!\w)[A-Z][\w&-]*")
# NOT third-party: 'agreement with Bigco to acquire X', 'approval from the ACCC to acquire X' (Bigco / the ACCC are
# counterparties or regulators; the company named earlier is still the buyer).


# After 'to acquire': 'it', 'the company', 'a 9.9% stake in the company': the object is the company named earlier.
_OBJECT_IS_COMPANY = re.compile(
    r"\s*(?:(?:an?\s+|the\s+)?[\w.%\s]{0,25}?(?:stake|shares?|interests?|%)\s+(?:in|of)\s+)?(?:the\s+(?:company|business|group)|it)(?!\w)",
    _FLAGS)


# A capitalised word in the verb's own clause that is not a pronoun / filler: another company is the subject.
_OTHER_ENTITY = re.compile(
    r"(?<!\w)(?!(?:It|Its|The|This|That|We|Our|They|Their|He|She|Meanwhile|Separately|Today|Also|Now|In|On|As|And|But|"
    r"However|Later|Then|After|Before|With|Last|Next)(?!\w))[A-Z][\w&-]*")


def ma_role(description: str, company_name: str) -> str:
    """'target', 'acquirer' or 'unclear': the scoop company's role in an M&A description. Deterministic text rules.
    Passive wording ('to be acquired by', 'acquired by', 'merger with', 'sold to') makes the company the target unless
    it is named AFTER the phrase (then it is the buyer). Active wording ('to acquire', 'acquisition of', 'takes a stake
    in') makes the company the acquirer if it is named before the verb, the target if only after it. If the company is
    not named in the text, or no wording is found: 'unclear'."""
    text = description or ""
    name = _name_token(company_name)
    if not _mentions(text, name):
        return "unclear"
    pas, act = _DEAL_PASSIVE.search(text), _DEAL_ACTIVE.search(text)
    m = pas if pas and (not act or pas.start() <= act.start()) else act
    if m is None:
        return "unclear"
    before, after = text[:m.start()], text[m.end():]
    if m is pas:
        return "acquirer" if _mentions(after, name, False) and not _mentions(before, name) else "target"
    third = _THIRD_PARTY_SUBJECT.search(before)
    if third and not _CAPITALISED_WORD.search(third.group("subject")):
        third = None
    if third:
        # 'NEXTDC has entered into an agreement for Goodman and Digital Realty to acquire a 9.9% stake in the company':
        # the buyers are named between 'for' / 'allow' / 'enable' and 'to acquire'; the company named BEFORE that is the target
        if _mentions(third.group("subject"), name):
            return "acquirer"
        # the company is the target only when the buyer is acquiring THAT company (named after the verb, or 'it' /
        # 'the company'): 'Acme seeks approval for Bigco to acquire Zeta' says nothing about Acme being bought
        if _mentions(after, name, False) or _OBJECT_IS_COMPANY.match(after):
            return "target"
        return "unclear"
    # the company named in the SAME clause before the verb is the buyer; named only in an earlier sentence
    # ('Acme is for sale; Bigco to acquire Acme') and again after the verb, it is the target
    clause = re.split(r"[.;]", before)[-1]
    if _mentions(m.group(0), name, False) and not _mentions(clause, name):
        return "target"           # 'lifts holding in Acme to 30%': the company is inside the verb phrase as its object
    if _mentions(clause, name):
        return "acquirer"
    if _mentions(after, name, False):
        return "target"
    # named only earlier and not in the verb's own clause: the buyer is whoever the clause names ('Acme opens an office.
    # Bigco to acquire Zeta' says nothing about Acme); a bare pronoun subject ('It plans to acquire Zeta') keeps Acme
    if _mentions(before, name) and not _OTHER_ENTITY.search(clause):
        return "acquirer"
    return "unclear"


def _percent_of_target(text: str, name: str) -> float | None:
    """The percentage in '<verb> <n>% of <company name>' (acquire / buy / purchase / take / holding / stake to ...), also
    '30 per cent', '30 percent' and 'up to 30%', or 'lifts holding in <company> to <n>%'; else None. The verb must sit
    directly before the percentage and the company right after 'of' (a possessive, "Acme's market", is not the company)."""
    if not name:
        return None
    nm = re.escape(name)
    m = re.search(rf"(?<!\w){_PERCENT_OF_VERB}\s+(?:to\s+)?(?:an?\s+|a\s+further\s+|further\s+)?(?:up\s+to\s+)?{_NUM_PCT}"
                  rf"\s+of\s+(?:the\s+)?{nm}(?!\w)(?!['\u2019]s(?!\w))", text, _FLAGS)
    if not m:
        m = re.search(rf"(?<!\w)(?:lifts?|raises?|increases?|boosts?)\s+(?:its\s+)?(?:holding|stake|shareholding)\s+in\s+"
                      rf"(?:the\s+)?{nm}(?!\w)(?!['\u2019]s(?!\w))\s+to\s+(?:up\s+to\s+)?{_NUM_PCT}", text, _FLAGS)
    return float(m.group(1)) if m else None


def _is_minority_stake(description: str, name: str = "") -> bool:
    """True for a minority-stake purchase: a percentage under 50, 'minority', or a 'stake' with no majority wording."""
    text = description or ""
    pct = _PERCENT.search(text)
    pct_val = float(pct.group(1)) if pct else None
    if pct_val is not None and pct_val < 50 and _STAKE.search(text):
        return True
    # '30% of Acme', '19.9% of Acme' (no word 'stake'); 'the remaining 49% of Acme' / 'all' / 'full' stay full acquisitions
    pct_of = _percent_of_target(text, name)
    if pct_of is not None and pct_of < 50 and not _MAJORITY.search(text):
        return True
    if re.search(r"(?<!\w)minority(?!\w)", text, _FLAGS):
        return True
    return bool(_STAKE.search(text)) and not _MAJORITY.search(text) and pct_val is None


def _acquirer_variant(description: str) -> tuple[float, str]:
    """(weight, note) for an acquirer: 0.25 for a minority stake or an unrelated (non-digital) business, else 0.50."""
    m = _DEAL_ACTIVE.search(description or "")
    after = (description or "")[m.start():] if m else (description or "")
    pct = _PERCENT.search(after)
    pct_val = float(pct.group(1)) if pct else None
    stake = bool(_STAKE.search(after))
    majority = bool(_MAJORITY.search(after))        # 'majority', 'controlling', 'full', '100%'...
    minority = (pct_val is not None and pct_val < 50) or bool(re.search(r"(?<!\w)minority(?!\w)", after, _FLAGS)) \
        or (stake and not majority and pct_val is None)
    digital = bool(_DIGITAL_BUSINESS.search(after))
    unrelated = bool(_NON_DIGITAL_SECTOR.search(after)) and not digital
    if minority or unrelated or (stake and not digital):
        return MA_MINORITY_OR_UNRELATED_WEIGHT, MA_MINORITY_NOTE
    return KIND_BASE_WEIGHT[SignalKind.MA], MA_ACQUIRER_FULL_NOTE


def classify_ma(description: str, company_name: str) -> tuple[str, float, str]:
    """(role, weight, note) for an M&A scoop. Weights: target 0.50, acquirer of a digital business or a full
    acquisition 0.50, acquirer of a minority stake or unrelated business 0.25, unclear 0.25."""
    role = ma_role(description, company_name)
    if role == "target":
        if _is_minority_stake(description, _name_token(company_name)):
            return role, MA_TARGET_MINORITY_WEIGHT, MA_TARGET_MINORITY_NOTE
        return role, KIND_BASE_WEIGHT[SignalKind.MA], MA_TARGET_NOTE
    if role == "acquirer":
        w, note = _acquirer_variant(description)
        return role, w, note
    return role, MA_UNCLEAR_WEIGHT, MA_UNCLEAR_NOTE


def looks_like_acquisition_of_company(description: str) -> bool:
    """True for funding-typed scoops that are really 'to be acquired by ...' with no money received."""
    return bool(_TARGET_WORDING.search(description or "")) and not _MONEY_RECEIVED.search(description or "")


# ---- same-deal detection (two companies, one deal)
_DEAL_STOP = frozenset({"the", "this", "that", "with", "for", "and", "pty", "ltd", "limited", "inc", "group",
                        "holdings", "company", "corp", "has", "will", "its", "per", "cent"})
_FIGURE = re.compile(r"\d+(?:\.\d+)?\s?%|\$\s?\d[\d.,]*\s?(?:m|bn|b|million|billion)?", _FLAGS)


def _deal_tokens(text: str) -> set[str]:
    toks = {w.lower() for w in re.findall(r"\b[A-Z][A-Za-z0-9&]{2,}\b", text or "")} - _DEAL_STOP
    toks |= {re.sub(r"\s", "", f).lower() for f in _FIGURE.findall(text or "")}
    return toks


def _same_deal_text(a: str, b: str) -> bool:
    ta, tb = _deal_tokens(a), _deal_tokens(b)
    common = ta & tb
    return len(common) >= 2 and len(common) / max(1, min(len(ta), len(tb))) >= 0.6


def _same_deal_scoops(a: Scoop, b: Scoop) -> bool:
    """Two scoops on different companies describe the same M&A deal: each mentions the other's company, or
    one mentions the other's company and they share most of their names and figures."""
    na, nb = _name_token(a.company_name), _name_token(b.company_name)
    a_has_b, b_has_a = _mentions(a.description, nb), _mentions(b.description, na)
    if a_has_b and b_has_a:
        return True
    return (a_has_b or b_has_a) and _same_deal_text(a.description, b.description)


# ---- exec move extras: vacated roles and an incoming leader's start date
_DEPARTURE = re.compile(
    r"(?<!\w)(?:has\s+)?(?:left|leaves|leaving|departs|departed|departing|resign\w*|steps?\s+down|stepping\s+down|"
    r"stepped\s+down|exits?|exiting|exited|moves?\s+on|to\s+leave)(?!\w)", _FLAGS)
_LEFT_TO_JOIN = re.compile(r"(?:left|leaves|leaving|departs|departed|departing|to\s+leave)\s+to\s+join\b[^.;]*", _FLAGS)
_INCOMING = re.compile(
    r"(?<!\w)(?:joins?|joined|appoint\w*|named|hires?|hired|welcomes?|promot\w+|takes?\s+over|will\s+lead|succeed\w*|"
    r"replac\w*|incoming|successor|will\s+start|starts?)(?!\w)", _FLAGS)
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], 1)}
_MONTH_RX = "|".join(_MONTHS)
_START_TRIGGER = r"(?:effective|starting|starts?|start\s+date|commenc\w+|begin\w*|from|on|by)"
_START_DATE = re.compile(
    rf"(?<!\w){_START_TRIGGER}\s+(?:the\s+)?(?:"
    rf"(?P<d1>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<m1>{_MONTH_RX})(?:,?\s+(?P<y1>\d{{4}}))?"
    rf"|(?P<m2>{_MONTH_RX})\s+(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<y2>\d{{4}}))?"
    rf"|(?P<iso>\d{{4}}-\d{{2}}-\d{{2}}))(?!\w)", _FLAGS)
_START_HINT = re.compile(r"(?<!\w)(?:join\w*|start\w*|commenc\w+|effective|begin\w*|assum\w+|take\s+up|incoming|successor|"
                         r"appointment|appointed)(?!\w)", _FLAGS)


def incoming_start_date(description: str, published: datetime) -> datetime | None:
    """Start date of an incoming leader ('effective 1 February', 'will start on 3 March 2027', 'starts 2026-12-01'),
    as an aware UTC datetime, else None. Needs a hint word (joins, starts, effective, appointed...) in the same
    sentence. A date without a year takes the scoop's publish year, or the next year when that would be more than
    60 days before the publish date."""
    for sentence in re.split(r"(?<=[.;])\s+", description or ""):
        if not _START_HINT.search(sentence):
            continue
        m = _START_DATE.search(sentence)
        if not m:
            continue
        try:
            if m.group("iso"):
                y, mo, d = (int(x) for x in m.group("iso").split("-"))
            else:
                d = int(m.group("d1") or m.group("d2"))
                mo = _MONTHS[(m.group("m1") or m.group("m2")).lower()]
                y_raw = m.group("y1") or m.group("y2")
                y = int(y_raw) if y_raw else published.year
            dt = datetime(y, mo, d, tzinfo=timezone.utc)
            if not (m.group("iso") or m.group("y1") or m.group("y2")) and dt < published - timedelta(days=60):
                dt = datetime(y + 1, mo, d, tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


# 'promoted from Chief Financial Officer to the role of CEO', 'moves from CTO to CEO'
_TRANSITION = re.compile(
    r"(?<!\w)(?:promoted|moves?|moved|moving|elevated|appointed|transitions?|transitioned|steps?\s+up|stepped\s+up)\s+from\s+"
    r"(?:the\s+(?:role|position)\s+of\s+)?(?P<old>[^.;]+?)\s+to\s+(?:the\s+)?(?:(?:role|position)\s+of\s+)?(?P<new>[^.;,]+)",
    _FLAGS)


def role_vacated(description: str) -> bool:
    """True when a relevant role holder LEFT and the text names no incoming leader ('CTO left to join another org',
    'Interim Chief Digital Officer leaving'). A vacancy is a valid trigger: a new leader is coming."""
    text = _LEFT_TO_JOIN.sub(" ", description or "")
    return bool(_DEPARTURE.search(description or "")) and not _INCOMING.search(text)


def _judge_kind(kind: SignalKind, scoop: Scoop, config: Config) -> _J:
    """Apply the per-kind keep rules; the detail is a phrase, not a full sentence."""
    desc = scoop.description or ""
    topics = _lower_set(scoop.topics)
    sg = config.signals

    if kind is SignalKind.PROJECT_PAIN:
        hit = sorted(topics & PROJECT_TOPICS)
        if hit:
            return _J(True, f"buyer topic {', '.join(hit)}")
        return _J(False, "Project/Pain Point without a Pendo-buyer topic (needs Customer Experience, Product Management, "
                         "Customer Success, Product Development or Digital Workplace)")

    if kind in (SignalKind.EXEC_MOVE, SignalKind.NEW_HIRE):
        tr = _TRANSITION.search(desc)
        if tr:
            # 'promoted from CFO to CEO': judge the NEW role and the VACATED role separately, never the whole sentence
            new_rm, old_rm = role_match(tr.group("new")), role_match(tr.group("old"))
            if not new_rm.role and not old_rm.role:
                return _J(False, f"C-suite move from '{tr.group('old').strip()}' to '{tr.group('new').strip()}': "
                                 "neither the new nor the vacated role is a buyer role")
            rm = new_rm if new_rm.role else old_rm
            start = incoming_start_date(desc, scoop.published_date)
            where = "new role" if new_rm.role else "vacated role"
            detail = f"{where} '{rm.role}'" + ("" if new_rm.role else ", new leader likely")
            if start:
                detail += f", incoming leader starts {start.date().isoformat()}"
            return _J(True, detail, rm.ambiguous, event_date=start, notes=() if new_rm.role else (VACATED_NOTE,))
        rm = role_match(desc)
        if not rm.role:
            return _J(False, "people move without a product/digital/CX/customer success/support/engineering/IT role in the description")
        start = incoming_start_date(desc, scoop.published_date)
        detail = f"role '{rm.role}'"
        vacated = role_vacated(desc)
        if vacated:
            detail += " vacated, new leader likely"
        if start:
            detail += f", incoming leader starts {start.date().isoformat()}"
        return _J(True, detail, rm.ambiguous, event_date=start, notes=(VACATED_NOTE,) if vacated else ())

    if kind is SignalKind.HIRING_PLAN:
        # Generic hiring (nurses, drivers, legal, 'more open roles in Medical & Health') is dropped unless the
        # note shows a buyer-role context. A topic hit counts as a keep signal but does NOT rescue a note that
        # also has a drop keyword and no role context. Weak keywords (WEAK_KEEP_KEYWORDS) never keep a note alone.
        context = hiring_context(desc)
        drop_kw = _first_hit(desc, sg.keywords_drop)
        if drop_kw and not context:
            return _J(False, f"generic hiring note (matches drop keyword '{drop_kw}', no buyer-role context)")
        if context:
            return _J(True, f"buyer role '{context}'")
        hit = sorted(topics & HIRING_TOPICS)
        if hit:
            return _J(True, f"relevant topic {', '.join(hit)}")
        strong = _first_hit(desc, [k for k in sg.keywords_keep if k.strip().lower() not in WEAK_KEEP_KEYWORDS])
        if strong:
            return _J(True, f"buyer keyword '{strong}'")
        weak = _first_hit(desc, sg.keywords_keep)
        extra = f" (only the weak keyword '{weak}')" if weak else ""
        return _J(False, "hiring note with no relevant topic, buyer role or strong keyword" + extra)

    if kind is SignalKind.FUNDING:
        why = _funding_drop_reason(scoop)
        if why:
            return _J(False, why)
        if _DEBT.search(desc) and not _EQUITY.search(desc):
            return _J(True, DEBT_NOTE, factor=FUNDING_DEBT_WEIGHT / KIND_BASE_WEIGHT[kind], notes=(DEBT_NOTE,))
        return _J(True)

    if kind is SignalKind.PRODUCT_LAUNCH:
        if _HARDWARE_PRODUCT.search(desc) and not _SOFTWARE_NOUN.search(desc):
            # robotics / instruments / cameras / consumables: 'platform', 'user interface', 'AI' do not make it software
            return _J(True, LAUNCH_NOTE, factor=LAUNCH_NON_DIGITAL_WEIGHT / KIND_BASE_WEIGHT[kind], notes=(LAUNCH_NOTE,))
        if _DIGITAL_PRODUCT.search(desc) or _CAMEL_AI.search(desc):
            return _J(True)
        if _FINANCIAL_PRODUCT.search(desc):
            return _J(True, FINANCIAL_LAUNCH_NOTE, factor=LAUNCH_FINANCIAL_WEIGHT / KIND_BASE_WEIGHT[kind],
                      notes=(FINANCIAL_LAUNCH_NOTE,))
        return _J(True, LAUNCH_NOTE, factor=LAUNCH_NON_DIGITAL_WEIGHT / KIND_BASE_WEIGHT[kind], notes=(LAUNCH_NOTE,))

    if kind is SignalKind.MA:
        role, weight, note = classify_ma(desc, scoop.company_name)
        # reduced weights and the 'being acquired' target both travel on the signal (the sheet shows why it is a trigger)
        notes = [note] if (weight < KIND_BASE_WEIGHT[kind] or role == "target") else []
        if role == "acquirer" and _RUMOUR.search(desc):
            weight *= MA_RUMOUR_FACTOR
            note += "; " + RUMOUR_NOTE
            notes.append(RUMOUR_NOTE)
        return _J(True, note, factor=weight / KIND_BASE_WEIGHT[kind], notes=tuple(notes))

    if kind is SignalKind.PARTNERSHIP:
        if _PARTNERSHIP_WORDS.search(desc):
            return _J(True)
        if _INFRASTRUCTURE_DEAL.search(desc):
            return _J(False, "infrastructure or capacity contract (compute, data centre, ARR, power), not a product partnership")
        return _J(True, PARTNERSHIP_NOTE, factor=PARTNERSHIP_NO_PRODUCT_WEIGHT / KIND_BASE_WEIGHT[kind],
                  notes=(PARTNERSHIP_NOTE,))
    return _J(True)


def filter_scoops(scoops: list[Scoop], config: Config, today: date | datetime) -> list[ScoopVerdict]:
    """One verdict per input scoop, in input order. Never drops silently: every verdict has a reason sentence.

    Checks: scoop type (must be in ``signals.scoop_types_keep``, or be Project / Pain Point which has its own topic
    gate); a Funding scoop that says the company is being acquired (and received no money) becomes an M&A scoop;
    then for each kind the scoop could be, in weight order: age limit for that kind (KIND_MAX_AGE_DAYS; exactly the
    limit is kept, one more day is dropped; an incoming leader's start date replaces the publish date as the age
    basis), the per-kind rules in ``_judge_kind``, and the strength floor (MIN_STRENGTH_FLOOR). The highest-weight
    kind that passes everything wins. Finally, kept M&A scoops of different companies in this batch that describe the
    same deal each get 'same deal as <other>' (both are kept).
    """
    sg = config.signals
    allowed_types = _lower_set(sg.scoop_types_keep) | {"project", "pain point"}
    verdicts: list[ScoopVerdict] = []
    for s in scoops:
        pub_days = (_as_date(today) - _as_date(s.published_date)).days
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
        reclassified = False
        if SignalKind.FUNDING in kinds and looks_like_acquisition_of_company(s.description):
            kinds.discard(SignalKind.FUNDING)
            kinds.add(SignalKind.MA)
            reclassified = True
        passed: dict[SignalKind, tuple[_J, int]] = {}
        failed: list[str] = []
        for k in _by_weight(kinds):
            limit = max_age_for(k, config)
            j = _judge_kind(k, s, config)
            if not j.ok:
                failed.append(j.detail)
                continue
            days = (_as_date(today) - _as_date(j.event_date)).days if j.event_date else pub_days
            if days > limit:
                failed.append(f"expired, {days} days old (limit {limit} for {k.value})")
                continue
            strength = _strength(k, max(0, days), j.ambiguous, j.factor)
            if strength < MIN_STRENGTH_FLOOR:
                failed.append(f"faded below floor ({strength:.2f} < {MIN_STRENGTH_FLOOR:.2f}, {days} days old)")
                continue
            passed[k] = (j, days)
        if passed:
            best = _by_weight(passed)[0]
            j, days = passed[best]
            label = next((t for t in types if TYPE_TO_KIND.get(t.strip().lower()) is best),
                         f"{types[0]} (reclassified as M&A: company is being acquired)" if reclassified else types[0])
            if j.event_date and days <= 0:
                age_text = ""          # the detail already says when the incoming leader starts
            elif j.event_date:
                age_text = f"started {days} day{'s' if days != 1 else ''} ago"
            else:
                age_text = _age_phrase(days)
            parts = [label] + ([j.detail] if j.detail else []) + ([age_text] if age_text else [])
            reason = "Kept: " + ", ".join(parts) + (f"; {AMBIGUOUS_NOTE}" if j.ambiguous else "")
            verdicts.append(ScoopVerdict(s, True, best, reason, j.ambiguous, j.factor, j.notes, j.event_date))
        else:
            verdicts.append(ScoopVerdict(s, False, SignalKind.NONE, "Dropped: " + "; ".join(failed)))
    return _mark_same_deals(verdicts)


def _mark_same_deals(verdicts: list[ScoopVerdict]) -> list[ScoopVerdict]:
    """Add 'same deal as <other company>' to kept M&A verdicts of different companies on the same deal."""
    idx = [i for i, v in enumerate(verdicts) if v.keep and v.kind is SignalKind.MA]
    others: dict[int, list[str]] = {i: [] for i in idx}
    for a in range(len(idx)):
        for b in range(a + 1, len(idx)):
            va, vb = verdicts[idx[a]], verdicts[idx[b]]
            if _key(va.scoop.company_id) == _key(vb.scoop.company_id):
                continue
            if _same_deal_scoops(va.scoop, vb.scoop):
                others[idx[a]].append(vb.scoop.company_name or vb.scoop.company_id)
                others[idx[b]].append(va.scoop.company_name or va.scoop.company_id)
    out = list(verdicts)
    for i, names in others.items():
        if names:
            names = sorted(set(names))
            v = verdicts[i]
            out[i] = replace(v, reason=v.reason + "; " + SAME_DEAL_PREFIX + ", ".join(names),
                             notes=v.notes + (SAME_DEAL_PREFIX + ", ".join(names),), same_deal_as=tuple(names))
    return out


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


#: The EXACT note strings (a scoop that happens to end in '(being acquired by Bigco)' is plain text, not a note).
#: 'same deal as <names>' is dynamic, so it is matched as that exact prefix followed by names.
KNOWN_NOTES = frozenset({
    AMBIGUOUS_NOTE, LAUNCH_NOTE, FINANCIAL_LAUNCH_NOTE, DEBT_NOTE, RUMOUR_NOTE, MA_TARGET_NOTE, MA_TARGET_MINORITY_NOTE,
    MA_ACQUIRER_FULL_NOTE, MA_MINORITY_NOTE, MA_UNCLEAR_NOTE, PARTNERSHIP_NOTE, VACATED_NOTE})
_SAME_DEAL_NOTE = re.compile(rf"^{re.escape(SAME_DEAL_PREFIX)}\S.*$")


def _is_known_note(text: str) -> bool:
    return text in KNOWN_NOTES or bool(_SAME_DEAL_NOTE.match(text))


def _suffix(notes) -> str:
    return "".join(f" ({n})" for n in notes)


def signal_notes(signal: Signal) -> list[str]:
    """The warning notes carried at the end of ``signal.evidence`` (role ambiguous, physical launch, minority stake,
    unclear M&A direction, weak partnership, same deal as ...). Empty when the evidence is the plain scoop text."""
    notes: list[str] = []
    text = signal.evidence
    while True:
        m = re.search(r" \(([^()]*)\)$", text)
        if not m or not _is_known_note(m.group(1)):
            break
        notes.insert(0, m.group(1))
        text = text[:m.start()]
    return notes


def is_ambiguous(signal: Signal) -> bool:
    """True when the signal's role was flagged ambiguous (see AMBIGUOUS_SUFFIX)."""
    return AMBIGUOUS_NOTE in signal_notes(signal)


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
        notes = ((AMBIGUOUS_NOTE,) if v.ambiguous else ()) + v.notes
        # an incoming leader's start date (when already past) is the age basis and the signal's date
        basis = v.event_date if v.event_date and _as_date(v.event_date) <= _as_date(today) else s.published_date
        for acct in accts:
            signals.append(Signal(
                sf_account_id=acct.sf_account_id,
                kind=v.kind,
                strength=_strength(v.kind, age_days(v.event_date or s.published_date, today), v.ambiguous, v.factor),
                source=f"{SOURCE_PREFIX}:{s.id}",
                source_date=basis,
                evidence=_trim_evidence(s.description, len(_suffix(notes))) + _suffix(notes),
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


def _plain_evidence(signal: Signal) -> str:
    text = signal.evidence
    for _ in signal_notes(signal):
        text = re.sub(r" \([^()]*\)$", "", text)
    return text


def dedupe_deals(signals: list[Signal]) -> list[Signal]:
    """Within one account, several M&A signals about the SAME deal (shared names and figures) count once: the
    strongest is kept. Other kinds and different deals are untouched."""
    kept: list[Signal] = []
    for sig in sorted(signals, key=_signal_sort_key):
        if sig.kind is SignalKind.MA and any(
                k.kind is SignalKind.MA and _same_deal_text(_plain_evidence(k), _plain_evidence(sig)) for k in kept):
            continue
        kept.append(sig)
    return kept


_LAUNCH_VERB = (r"(?i:launch(?:ed|es|ing)?(?:\s+of)?|unveil(?:ed|s|ing)?|introduc(?:ed|es|ing)|releas(?:ed|es|ing)|"
                r"debut(?:ed|s)?|rolls?\s+out|rolled\s+out|announc(?:ed|es)(?:\s+the\s+launch\s+of)?)")
_PRODUCT_NAME = re.compile(
    rf"(?<!\w){_LAUNCH_VERB}\s+(?:(?i:the|its|a|an|new|latest|next-generation)\s+)*"
    r"(?P<name>[A-Z][\w-]*(?:\s+[A-Z][\w-]*){0,3})")


# Words that are not product names: places and generic nouns ('launches New Zealand expansion', 'launched Australian range').
_NOT_A_NAME = frozenset(
    "australia australian new zealand nz sydney melbourne brisbane perth adelaide canberra hobart darwin auckland "
    "wellington christchurch queensland victoria tasmania asia pacific apac anz europe european uk us usa america "
    "american north south east west global international regional national local expansion version range product "
    "products service services platform solution solutions program programme offering offerings update upgrade "
    "edition line series the its a an".split())


def _product_name(text: str) -> str | None:
    """The capitalised product name after a launch verb ('Catapult launches PerformanceHub 2'), lower-cased, else None.
    Place and generic words (Australia, New Zealand, Sydney, expansion, range...) are not names and are dropped; if
    nothing is left the answer is None. KNOWN LIMIT: a lower-case product name ('launches the pro tracker') has no
    capital to find, so it is not matched by name (only by the first 60 characters of the text)."""
    m = _PRODUCT_NAME.search(text or "")
    if not m:
        return None
    kept = [t for t in m.group("name").split() if t.lower() not in _NOT_A_NAME]
    return " ".join(kept).lower() if kept else None


def _normalised_start(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()[:NEAR_DUPLICATE_CHARS]


def _near_duplicate(a: Signal, b: Signal) -> bool:
    """Same kind and either the same first NEAR_DUPLICATE_CHARS normalised characters of the evidence, or the same
    product name within NEAR_DUPLICATE_DAYS days (one launch announced in several scoops)."""
    if a.kind is not b.kind:
        return False
    ta, tb = _plain_evidence(a), _plain_evidence(b)
    if _normalised_start(ta) == _normalised_start(tb):
        return True
    na, nb = _product_name(ta), _product_name(tb)
    if na and na == nb:
        if a.source_date is None or b.source_date is None:
            return True
        return abs((a.source_date - b.source_date).days) <= NEAR_DUPLICATE_DAYS
    return False


def dedupe_signals(signals: list[Signal]) -> list[Signal]:
    """Within one account, scoops that are near-identical (see _near_duplicate) count once: the strongest is kept."""
    kept: list[Signal] = []
    for sig in sorted(signals, key=_signal_sort_key):
        if not any(_near_duplicate(k, sig) for k in kept):
            kept.append(sig)
    return kept


def combined_strength(signals: list[Signal]) -> float:
    """Diminishing returns over all signals of one account, in 0.0-1.0.

    1. Same-deal M&A scoops and near-identical scoops (same first 60 characters, or the same product name within 14
       days) count once (dedupe_deals, dedupe_signals).
    2. Pick the best signal of each KIND. The strongest of those counts in full; the best of every other kind counts at
       COMBINE_DAMPING (0.5).
    3. Then further signals of a kind that already has a best add at most SAME_KIND_EXTRA_EACH (0.15) each,
       SAME_KIND_EXTRA_TOTAL (0.30) in total.
    4. Only the MAX_COMBINED_SIGNALS strongest counted entries (the best plus up to three others) are used; the rest
       add nothing. Kind diversity is decided BEFORE this cap, so a different kind is never dropped just because several
       same-kind signals are stronger.
    combined = 1 - (1 - best) * prod(1 - contribution). While the account has any real (non-intent) signal the best is
    chosen among those, so intent can only be a damped extra."""
    real = dedupe_signals(dedupe_deals(_real_signals(signals)))
    if not real:
        return 0.0
    by_kind: dict[SignalKind, list[Signal]] = {}
    for sig in sorted(real, key=_signal_sort_key):
        by_kind.setdefault(sig.kind, []).append(sig)
    leads = {k: v[0] for k, v in by_kind.items()}
    dated = [x for k, x in leads.items() if k is not SignalKind.INTENT]
    best = min(dated or leads.values(), key=_signal_sort_key)
    contributions: list[float] = []
    for k, lead in leads.items():
        if lead is not best:
            contributions.append(COMBINE_DAMPING * lead.strength)
    extras = 0.0
    for k, group in by_kind.items():
        for sig in group[1:]:
            # at most the damped value (never more than the old formula gave) and at most the per-extra / total caps
            c = min(COMBINE_DAMPING * sig.strength, SAME_KIND_EXTRA_EACH, max(0.0, SAME_KIND_EXTRA_TOTAL - extras))
            extras += c
            contributions.append(c)
    rest = 1.0
    for c in sorted(contributions, reverse=True)[:MAX_COMBINED_SIGNALS - 1]:
        rest *= 1.0 - c
    return round(min(1.0, 1.0 - (1.0 - best.strength) * rest), 4)


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
