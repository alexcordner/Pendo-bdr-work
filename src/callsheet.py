"""Call sheet assembly (task 2c). Deterministic Python only: no LLM, no I/O, no network.

Takes a RankResult plus plain arguments and returns a ``Sheet`` (what to call, in what order, and why),
a ``ResearchPacket`` per entry (the ONLY facts a later Claude session may use for the opener), text /
markdown / JSON renderers, and ONE entry point for the Nooks task-create payload, ``prepare_task``
(Phase 3). Nothing here sends or writes anything. Notes: docs/callsheet_notes.md.

Hard rules honoured here (CLAUDE.md):
  * rule 6: no invented trigger. An account without a real, usable signal says so.
  * never a contact email or phone in a packet, render or note (free text is scrubbed; ``repr`` hides the
    prospect). The Nooks writer looks the contact up by prospect id.
  * needs_prospect / spilled / skipped accounts are never sheet entries and never payloads.
  * a payload can only be built by ``prepare_task``, which re-runs the do-not-contact check on the RESOLVED
    Nooks prospect and on the sheet contact, and builds from exactly the id it checked.
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from src import signals as sigmod
from src import accounts as acc_mod
from src.config import Config, tier_for
from src.loaders import normalise_sf_id
from src.models import Account, Prospect, RankedAccount, SheetEntry, Signal, SignalKind
from src.ranker import RankResult, account_key, pick_prospect
from src.state import State, dnc_is_rebuilt, is_blocked

RUN_DATE_TZ = "Australia/Sydney"          # run_date in the task tag = 'now' in the BDR's home zone (a label only)
TAG_PREFIX = "apac-callsheet"
OWNER_ID_PLACEHOLDER = "OWNER_ID_PLACEHOLDER"   # only accepted with dry_run=True
BUCKET_LIST_LIMIT = 25
NOTE_MAX_CHARS = 2000                     # conservative, UNVERIFIED Nooks note limit: refuse above it
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")   # Nooks ownerId pattern; also applied to prospect ids
REDACTED = "[redacted]"

# Nooks task priority enum (schema): low, normal, high, urgent. Score bands (documented):
#   score >= 75 urgent | >= 55 high | >= 35 normal | else low
PRIORITY_BANDS = ((75.0, "urgent"), (55.0, "high"), (35.0, "normal"))
PRIORITY_FLOOR = "low"

KIND_LABELS = {
    SignalKind.FUNDING: "Funding", SignalKind.EXEC_MOVE: "Executive Move", SignalKind.NEW_HIRE: "New Hire",
    SignalKind.MA: "M&A", SignalKind.PRODUCT_LAUNCH: "Product Launch", SignalKind.PARTNERSHIP: "Partnership",
    SignalKind.PROJECT_PAIN: "Project / Pain Point", SignalKind.HIRING_PLAN: "Hiring Plan",
    SignalKind.INTENT: "Intent (interest signal, not a dated event)",
}

MUST_NOT_CLAIM = (
    "Do not invent a trigger, quote, number, date or news item that is not in available_facts.",
    "Do not claim any Pendo customer, case study or result for this account.",
    "Do not state the company's revenue, tech stack, product roadmap or strategy unless listed in available_facts.",
    "Do not say the prospect said, posted or announced anything unless a fact below says so.",
    "Do not say you saw their website, LinkedIn or any page unless a fact below says so.",
    "Do not pad to two facts: if fewer than two facts are available, say so (FACT 2: none verified).",
    "Do not include or guess an email address or phone number.",
)


# ---------------------------------------------------------------- personal email (free / ISP mail)
# Configurable. A domain is personal when its FIRST label is a provider label AND the rest is a known suffix
# (gmail.com, yahoo.com.au, bigpond.net.au, internode.on.net, xtra.co.nz ...), or when it is an exact domain
# in PERSONAL_EMAIL_EXACT. 'live.acme.com' is NOT personal (the rest must be a mail-provider suffix).
PERSONAL_EMAIL_PROVIDERS = frozenset({
    "gmail", "googlemail", "outlook", "hotmail", "live", "msn", "yahoo", "ymail", "rocketmail", "icloud", "me", "mac",
    "protonmail", "proton", "pm", "aol", "gmx", "zoho", "fastmail", "bigpond", "bigpond", "telstra", "optusnet", "tpg",
    "iinet", "internode", "aapt", "westnet", "dodo", "exetel", "ozemail", "netspace", "xtra", "orcon", "slingshot",
    "vodafone", "spark", "clear", "paradise", "hey", "tutanota"})
PERSONAL_EMAIL_SUFFIXES = frozenset({
    "com", "net", "org", "me", "co", "com.au", "net.au", "org.au", "id.au", "co.nz", "net.nz", "org.nz", "co.uk",
    "on.net", "com.sg", "co.in", "ca"})
PERSONAL_EMAIL_EXACT = frozenset({"proton.me", "pm.me", "me.com", "mac.com", "icloud.com", "gmail.com", "hey.com"})


def _email_domain(email: object) -> str | None:
    if not isinstance(email, str):
        return None
    t = email.strip().strip("<>").strip()
    if "@" not in t:
        return None
    dom = unicodedata.normalize("NFKC", t.rsplit("@", 1)[1]).translate(_ZERO_WIDTH).strip().strip(">").strip().lower().rstrip(".")
    return dom or None


def is_personal_email(email: object) -> bool:
    """Deterministic free/ISP-mailbox check on an email address. Never returns or prints the address."""
    dom = _email_domain(email)
    if not dom:
        return False
    if dom in PERSONAL_EMAIL_EXACT:
        return True
    label, _, rest = dom.partition(".")
    return label in PERSONAL_EMAIL_PROVIDERS and rest in PERSONAL_EMAIL_SUFFIXES


def email_reason(prospect: Prospect | None) -> str:
    """'ok' | 'personal_email' | 'no_email' | 'opted_out' | 'do_not_call' (first that applies)."""
    if prospect is None:
        return "no_email"
    if prospect.do_not_call:
        return "do_not_call"
    if prospect.opted_out_email:
        return "opted_out"
    if not (isinstance(prospect.email, str) and "@" in prospect.email):
        return "no_email"
    if is_personal_email(prospect.email):
        return "personal_email"
    return "ok"


_EMAIL_REASON_TEXT = {
    "personal_email": "contact email is personal: call only, no email draft",
    "no_email": "no email address on file: call only, no email draft",
    "opted_out": "contact has opted out of email: call only, no email draft",
    "do_not_call": "contact is do-not-contact: no call, no email",
}


class SheetError(ValueError):
    """Refusal to build a sheet entry or payload (never silently degrade)."""


class BlockedError(SheetError):
    """The contact is on a do-not-contact list (state / flags): no task may be built."""


# ---------------------------------------------------------------- text hygiene
_EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
# Obfuscated emails: 'jo [at] example dot com', 'jo(at)example.com', 'jo at example dot com', 'jo @ example.com'.
_EMAIL_OBF = re.compile(
    r"[\w.+'-]+\s*[\[({<]\s*at\s*[\])}>]\s*[\w-]+(?:\s*(?:[\[({<]\s*dot\s*[\])}>]|\.|\s+dot\s+)\s*[\w-]+)+"
    r"|[\w.+'-]+\s+at\s+[\w-]+(?:\s+dot\s+[\w-]+)+"
    r"|[\w.+'-]+\s+@\s+[\w-]+(?:\.[\w-]+)+", re.IGNORECASE)
_PHONE = re.compile(
    r"(?<!\w)(?:"
    r"\+\d[\d\s().-]{6,}\d"                              # international, leading +
    r"|\(?0\d{1,2}\)?[\s.-]?\d{3,4}[\s.-]?\d{3,4}"       # AU/NZ landline (02 9999 1234, 021 123 4567)
    r"|0\d{3}[\s.-]?\d{3}[\s.-]?\d{3}"                   # AU mobile (0400 111 222)
    r"|1[38]00[\s-]?\d{3}[\s-]?\d{3}"                     # 1300 / 1800 numbers
    r"|13[\s-]\d{2}[\s-]\d{2}"                           # 13 12 34
    r"|\d{9,}"                                           # any long digit run
    r")(?!\w)"
    # 8-digit local numbers only after a phone-ish label (bare 'dddd dddd' would eat year pairs)
    r"|(?i:\b(?:ph|phone|tel|telephone|mob|mobile|call|fax)\b)[\s:.]*(?:\(\d{1,2}\)\s*)?\d{4}[\s.-]?\d{4}(?!\d)")
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u180e"), None)


def _redact(text: str) -> str:
    return _PHONE.sub(REDACTED, _EMAIL_OBF.sub(REDACTED, _EMAIL.sub(REDACTED, text)))


def _scrub(text: str) -> str:
    """Replace email addresses (plain and common obfuscations) and phone-like numbers with '[redacted]'.
    Detection runs on the NFKC form (full-width '@' and digits), but text with nothing to redact is
    returned unchanged."""
    norm = unicodedata.normalize("NFKC", text)
    out = _redact(norm)
    return text if out == norm else out


def _has_contact_data(text: str) -> bool:
    norm = unicodedata.normalize("NFKC", text)
    return _redact(norm) != norm


def _clean(text: object) -> str:
    """Single-line, invisible characters removed, whitespace collapsed, contact data redacted."""
    s = str(text).translate(_ZERO_WIDTH).replace(" ", " ")
    return _scrub(" ".join(s.split()))


_MD_SPECIALS = re.compile(r"([\\`|#*_\[\]<>])")


def _md(text: object, limit: int | None = 160) -> str:
    """Markdown-safe: collapse whitespace/newlines, shorten, backslash-escape markdown characters."""
    s = _clean(text)
    if limit is not None and len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return _MD_SPECIALS.sub(r"\\\1", s)


def _short(text: str, n: int = 110) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


_HONORIFICS = {"dr", "mr", "mrs", "ms", "miss", "mx", "prof", "professor", "sir", "dame"}
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "md"}
_NAME_TOKEN = re.compile(r"[^\W\d_][\w'’-]*")


def _first_name(name: str | None) -> str | None:
    """First name, or None when unsure (never a surname, never an honorific or initial).

    'Dr Pat Smith' -> 'Pat'; 'Smith, Pat' -> 'Pat'; 'Pat' / 'Dr Smith' / 'J. Smith' -> None."""
    s = _clean(name or "")
    if not s:
        return None
    if "," in s:                                     # 'Surname, First [Middle]'
        _, _, rest = s.partition(",")
        toks = [t for t in rest.replace(",", " ").split()]
        toks = [t for t in toks if t.strip(".").lower() not in _HONORIFICS | _SUFFIXES]
        cand = toks[0] if toks else None
    else:
        toks = s.split()
        while toks and toks[0].strip(".").lower() in _HONORIFICS:
            toks = toks[1:]
        cand = toks[0] if len(toks) >= 2 else None   # a lone token could be a surname: unsure
    if not cand or not _NAME_TOKEN.fullmatch(cand):
        return None
    return cand if len(re.sub(r"\W|\d|_", "", cand)) >= 2 else None


def _who(name: str | None) -> str:
    return _first_name(name) or "the contact"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# ---------------------------------------------------------------- data classes
@dataclass(frozen=True)
class ContactHistory:
    """Caller-filled summary of earlier contact with this account/prospect (from state / Nooks)."""

    last_call_date: date | None = None
    last_outcome: str | None = None          # e.g. 'No Answer', 'Left Voicemail'
    calls_total: int | None = None
    # How the history was matched to this contact: 'phone' (dry run: contact has no Nooks id yet) or
    # 'nooks_prospect_id' (after Phase 3 resolution). None = caller did not say.
    matched_by: str | None = None
    source: str = "Nooks call history"

    @property
    def source_label(self) -> str:
        src = _clean(self.source)
        return f"{src} (matched by {_clean(self.matched_by)})" if self.matched_by else src

    def summary(self) -> str | None:
        if self.last_call_date is None and not self.last_outcome and not self.calls_total:
            return None
        parts = []
        if self.last_call_date is not None:
            parts.append(f"last call {self.last_call_date.isoformat()}")
        if self.last_outcome:
            parts.append(f"outcome {_clean(self.last_outcome)}")
        if self.calls_total:
            parts.append(f"{self.calls_total} call(s) logged")
        return ", ".join(parts)


@dataclass(frozen=True)
class FactItem:
    text: str
    source: str                              # e.g. 'ZoomInfo scoop 2026-11-22', 'Salesforce account record'

    def to_dict(self) -> dict:
        return {"text": self.text, "source": self.source}


@dataclass(frozen=True)
class PacketSignal:
    kind: str
    label: str
    evidence: str
    source: str
    source_label: str
    date: str | None                         # ISO date
    age_days: int | None
    status: str                              # ok | undated | intent | future | stale
    verdict_reason: str
    ambiguous: bool
    notes: tuple[str, ...] = ()              # signals.signal_notes(): role ambiguous, physical launch, debt facility, same deal as ...

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["notes"] = list(self.notes)
        return d


@dataclass
class ResearchPacket:
    account_key: str
    account_name: str
    website: str | None
    industry: str | None
    state: str | None
    city: str | None
    employees: int | None
    tier: str | None
    prospect_first_name: str | None          # None when unsure: say 'the contact', never a surname
    prospect_title: str | None
    prospect_sf_contact_id: str | None       # identifies the contact (never email / phone); Nooks id may be None
    local_time: str | None                   # ISO with offset
    window: str | None
    timezone: str | None
    signals: list[PacketSignal]
    has_real_trigger: bool                   # a usable (dated-or-undated, non-intent, not future/stale) signal
    trigger_kind: str | None                 # kind of the best usable signal, else None
    ambiguity_flags: list[str]
    sdr_target: bool
    contact_history: str | None
    contact_history_source: str | None
    available_facts: list[FactItem]
    must_not_claim: list[str]
    email_ok: bool                           # False for personal / missing / opted-out email (never the address)
    email_reason: str                        # ok | personal_email | no_email | opted_out | do_not_call
    facts_note: str                          # empty when two or more facts are available and none excluded
    must_not_phrases: list[str] = field(default_factory=list)   # literal phrases textcheck rejects (from signal notes)

    @property
    def fact_count(self) -> int:
        return len(self.available_facts)

    def to_dict(self) -> dict:
        return {
            "account_key": self.account_key, "account_name": self.account_name, "website": self.website,
            "industry": self.industry, "state": self.state, "city": self.city, "employees": self.employees,
            "tier": self.tier, "prospect_first_name": self.prospect_first_name,
            "prospect_title": self.prospect_title,
            "prospect_sf_contact_id": self.prospect_sf_contact_id, "local_time": self.local_time,
            "window": self.window, "timezone": self.timezone, "signals": [s.to_dict() for s in self.signals],
            "has_real_trigger": self.has_real_trigger, "trigger_kind": self.trigger_kind,
            "ambiguity_flags": list(self.ambiguity_flags),
            "sdr_target": self.sdr_target, "contact_history": self.contact_history,
            "contact_history_source": self.contact_history_source,
            "available_facts": [f.to_dict() for f in self.available_facts], "fact_count": self.fact_count,
            "facts_note": self.facts_note, "must_not_claim": list(self.must_not_claim),
            "email_ok": self.email_ok, "email_reason": self.email_reason,
            "must_not_phrases": list(self.must_not_phrases),
        }


_SEAL = object()


class SheetLine:
    """One sheet row: the shared ``SheetEntry`` plus everything the sheet adds.

    Deliberately NOT a dataclass and slotted: ``dataclasses.asdict`` / ``replace`` / ``vars`` / ``copy`` /
    pickle all fail closed, ``repr`` hides the Prospect (email, phone, surname), and only ``build_sheet``
    can seal a line as genuine (``prepare_task`` refuses unsealed or copied lines)."""

    __slots__ = ("_entry", "rank", "call_no", "group", "run_date", "why_chosen", "flags", "packet", "_sealed",
                 "_snap", "__weakref__")

    def __init__(self, entry: SheetEntry, rank: int, call_no: int, group: str, run_date: date,
                 why_chosen: list[str], flags: list[str], packet: ResearchPacket):
        self._entry = entry
        self.rank = rank                     # 1 = best score; position among ALL ranked accounts (can skip numbers)
        self.call_no = call_no               # 1-based position in call order
        self.group = group                   # e.g. 'Australia/Sydney morning Thu 26 Nov'
        self.run_date = run_date
        self.why_chosen = why_chosen
        self.flags = flags
        self.packet = packet
        self._sealed = False
        self._snap: dict | None = None       # due time / priority / window / rank / run_date frozen at seal time

    def _seal(self, token) -> None:
        """Called by build_sheet only. Freezes what the payload is built from, so mutating the shared
        RankedAccount (score, time, window) afterwards cannot change a task."""
        ra = self._entry.ranked
        self._snap = {"due": ra.local_call_time, "priority": priority_for(ra.score), "window": ra.window,
                      "rank": self.rank, "run_date": self.run_date}
        self._sealed = token

    def __repr__(self) -> str:
        return (f"SheetLine(call_no={self.call_no!r}, rank={self.rank!r}, group={self.group!r}, "
                f"account={self.account_name!r})")

    __str__ = __repr__

    def __copy__(self):
        raise TypeError("SheetLine cannot be copied")

    __deepcopy__ = lambda self, memo: self.__copy__()      # noqa: E731

    def __reduce_ex__(self, protocol):
        raise TypeError("SheetLine cannot be pickled")

    @property
    def entry(self) -> SheetEntry:
        return self._entry

    @property
    def ranked(self) -> RankedAccount:
        return self._entry.ranked

    @property
    def account(self) -> Account:
        return self._entry.ranked.account

    @property
    def account_name(self) -> str:
        return _clean(self.account.name)

    @property
    def prospect(self) -> Prospect:
        return self._entry.prospect  # type: ignore[return-value]

    @property
    def score(self) -> float:
        return self._entry.ranked.score

    @property
    def local_call_time(self) -> datetime:
        return self._entry.ranked.local_call_time  # type: ignore[return-value]


@dataclass
class BucketItem:
    name: str
    reason: str


@dataclass
class Sheet:
    run_date: date
    lines: list[SheetLine] = field(default_factory=list)       # call order
    groups: list[tuple[str, list[SheetLine]]] = field(default_factory=list)
    needs_prospect: list[BucketItem] = field(default_factory=list)
    spilled: list[BucketItem] = field(default_factory=list)
    skipped: list[BucketItem] = field(default_factory=list)
    skipped_reason_counts: dict[str, int] = field(default_factory=dict)
    dropped: list[BucketItem] = field(default_factory=list)    # ranked but not sheetable (and why)
    ranked_total: int = 0
    limit: int | None = None

    @property
    def entries(self) -> list[SheetEntry]:
        return [ln.entry for ln in self.lines]


# ---------------------------------------------------------------- signals
def _source_label(sig: Signal) -> str:
    d = sig.source_date.date().isoformat() if sig.source_date else "date unknown"
    if sig.source.startswith(sigmod.SOURCE_PREFIX):
        return f"{sigmod.SOURCE_LABEL} {d}"
    if sig.kind is SignalKind.INTENT:
        return f"ZoomInfo intent {d} (unverified connector shape)"
    return f"{_clean(sig.source)} {d}"


def _plain_text(sig: Signal) -> str:
    """The scoop text with every trailing ' (note)' warning removed (signals.signal_notes reads them back)."""
    ev = sig.evidence
    for n in reversed(sigmod.signal_notes(sig)):
        suffix = f" ({n})"
        if ev.endswith(suffix):
            ev = ev[: -len(suffix)]
    return ev


def _clean_evidence(sig: Signal) -> str:
    return _clean(_plain_text(sig))


def _notes(sig: Signal) -> tuple[str, ...]:
    return tuple(_clean(n) for n in sigmod.signal_notes(sig))


def _real(sigs: Sequence[Signal]) -> list[Signal]:
    return [s for s in sigs if s.kind is not SignalKind.NONE]


def _signals_for(ranked: RankedAccount, signals_by_account: Mapping[str, list[Signal]]) -> list[Signal]:
    """RankedAccount.signals is the source of truth (the score came from it). The mapping is only a
    fallback for a hand-built RankedAccount with no signals."""
    sigs = _real(ranked.signals)
    if sigs:
        best = sigmod.best_signal(sigs)
        return [best] + sorted((s for s in sigs if s is not best), key=lambda s: (-s.strength, s.source))
    a = ranked.account
    seen, out = set(), []
    clean = {}
    for k, v in signals_by_account.items():
        clean.setdefault(normalise_sf_id(k) or k, []).extend(v)
    for key in (normalise_sf_id(a.sf_account_id), normalise_sf_id(a.zoominfo_id)):
        for s in clean.get(key, []) if key else []:
            if s.kind is not SignalKind.NONE and s.source not in seen:
                seen.add(s.source)
                out.append(s)
    best = sigmod.best_signal(out)
    return ([best] + sorted((s for s in out if s is not best), key=lambda s: (-s.strength, s.source))) if best else []


def _call_date(ranked: RankedAccount) -> date | None:
    t = ranked.local_call_time
    return t.date() if t else None


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _status(sig: Signal, call_date: date | None, now: datetime | None, config: Config) -> tuple[str, int | None]:
    """(status, age_days). status: intent | undated | future | stale | ok. Recomputed from the signal's own
    date against ``now`` / the account-local call date and the kind's real limit: nothing is trusted from
    upstream text."""
    if sig.source_date is None:
        return ("intent" if sig.kind is SignalKind.INTENT else "undated"), None
    d = _aware(sig.source_date)
    age = sigmod.age_days(d, call_date) if call_date else None
    if sig.kind is SignalKind.INTENT:
        return "intent", age
    if (now is not None and d > now) or (call_date is not None and d.date() > call_date):
        return "future", age
    if age is not None and age > sigmod.max_age_for(sig.kind, config):
        return "stale", age
    return "ok", age


def _age_text(status: str, age: int | None) -> str:
    if status == "future":
        return "future-dated: check the source"
    if age is None:
        return "date unknown"
    return "today" if age == 0 else f"{_plural(age, 'day')} ago"


def _verdict_reason(sig: Signal, status: str, age: int | None, config: Config) -> str:
    label = KIND_LABELS.get(sig.kind, sig.kind.value)
    if status == "intent":
        out = "interest signal, not a dated event"
    elif status == "future":
        out = "future-dated: check the source"
    elif status == "stale":
        out = f"older than its {sigmod.max_age_for(sig.kind, config)} day limit ({_plural(age or 0, 'day')} old): check before quoting"
    elif status == "undated":
        out = f"kept: {label}, date unknown"
    else:
        out = f"kept: {label} within {sigmod.max_age_for(sig.kind, config)} day limit ({_plural(age or 0, 'day')} old)"
    for n in _notes(sig):
        out += f"; {n}"
    return out


def _packet_signal(sig: Signal, call_date: date | None, now: datetime | None, config: Config) -> PacketSignal:
    status, age = _status(sig, call_date, now, config)
    return PacketSignal(
        kind=sig.kind.value, label=KIND_LABELS.get(sig.kind, sig.kind.value), evidence=_clean_evidence(sig),
        source=_clean(sig.source), source_label=_source_label(sig),
        date=sig.source_date.date().isoformat() if sig.source_date else None, age_days=age, status=status,
        verdict_reason=_verdict_reason(sig, status, age, config), ambiguous=sigmod.is_ambiguous(sig),
        notes=_notes(sig))


# What a text must NOT say about a trigger whose evidence carries a warning note (signals.signal_notes). Literal
# phrases for textcheck's must_not_phrases; a phrase the scoop text itself contains is left out (the scoop says it).
NOTE_BANNED_PHRASES: dict[str, tuple[str, ...]] = {
    "physical or unclear product launch": ("app launch", "new app", "new platform", "digital product launch", "digital launch"),
    "financial or property product launch": ("app launch", "new app", "new platform", "digital product launch",
                                             "digital launch", "product launch", "new product"),
    "debt facility": ("funding round", "equity round", "capital raise", "raised", "series a", "series b", "series c"),
    "reported talks": ("has acquired", "have acquired", "acquired", "completed the acquisition", "congrats on acquiring",
                       "congratulations on acquiring", "congrats on the acquisition", "congratulations on the acquisition"),
    "acquirer of a minority stake": ("acquired", "takeover", "acquisition of the company", "full acquisition"),
    "minority stake purchase": ("takeover", "being acquired", "acquisition of the company", "full acquisition",
                                "acquired by", "buyout"),
    "role vacated": ("has appointed", "appointed a new", "successor", "replacement", "has hired", "new head",
                     "new chief", "congrats on the new role", "congratulations on the new role", "welcome the new"),
}


def _banned_for(notes: Sequence[str], evidence: str) -> list[str]:
    ev = evidence.casefold()
    out: list[str] = []
    for note in notes:
        for prefix, phrases in NOTE_BANNED_PHRASES.items():
            if note.startswith(prefix):
                out += [p for p in phrases if p not in ev and p not in out]
    return out


def _location(a: Account) -> str | None:
    parts = [_clean(p) for p in (a.billing_city, a.billing_state or a.hq_state) if p]
    return ", ".join(parts) if parts else None


def build_why(ranked: RankedAccount, sigs: list[Signal], config: Config, now: datetime | None = None) -> list[str]:
    """Plain-English lines derived ONLY from data. Never claims a trigger that is not a real signal."""
    a = ranked.account
    cd = _call_date(ranked)
    out: list[str] = []
    if a.is_sdr_target:
        out.append("SDR target")
    if sigs:
        b = sigs[0]
        status, age = _status(b, cd, now, config)
        label = KIND_LABELS.get(b.kind, b.kind.value)
        src = "ZoomInfo scoop" if b.source.startswith(sigmod.SOURCE_PREFIX) else (
            "ZoomInfo intent" if b.kind is SignalKind.INTENT else _clean(b.source))
        out.append(f"{label}: {_short(_clean_evidence(b))} ({src}, {_age_text(status, age)})")
        if status == "intent":
            out.append("No dated trigger: interest signal only")
        elif status == "future":
            out.append("Flag: signal date in the future")
        elif status == "stale":
            out.append("Flag: signal older than its limit")
        for n in _notes(b):
            out.append(f"Flag: {n}")
        if len(sigs) > 1:
            out.append(f"{_plural(len(sigs) - 1, 'more trigger')}")
    else:
        out.append("No trigger: ranked on fit only")
    if a.size_unknown:
        out.append("Size unknown (neutral fit)")
    else:
        tier = tier_for(config, a.employees)
        out.append(f"Size {a.employees} ({tier})" if tier else f"Size {a.employees} (outside size tiers)")
    if a.non_anz_billing_anz_hq:
        out.append("Flag: non-ANZ billing, ANZ HQ")
    if a.timezone_guessed:
        out.append("Flag: timezone guessed from country default")
    if a.confidence == "low":
        out.append("Flag: low-confidence record")
    for f in a.flags:
        out.append(f"Flag: {_clean(f)}")
    return out


def _flags(ranked: RankedAccount, sigs: list[Signal], config: Config, now: datetime | None) -> list[str]:
    a = ranked.account
    cd = _call_date(ranked)
    stats = [_status(s, cd, now, config)[0] for s in sigs]
    out = []
    if any(sigmod.is_ambiguous(s) for s in sigs):
        out.append("role ambiguous")
    if sigs:
        out.extend(n for n in _notes(sigs[0]) if n != _clean(sigmod.AMBIGUOUS_NOTE))
    if not sigs:
        out.append("no trigger")
    elif not any(t in ("ok", "undated") for t in stats):
        out.append("no dated trigger")
    if "future" in stats:
        out.append("signal in the future")
    if "stale" in stats:
        out.append("signal older than limit")
    if a.size_unknown:
        out.append("size unknown")
    if a.non_anz_billing_anz_hq:
        out.append("non-ANZ billing")
    if a.timezone_guessed:
        out.append("tz guessed")
    if a.confidence == "low":
        out.append("low confidence")
    out.extend(_clean(f) for f in a.flags)
    return out


def _contact_flags(pr: Prospect) -> list[str]:
    out = ["no contact id"] if not (pr.nooks_id or pr.sf_contact_id) else []
    reason = email_reason(pr)
    if reason in ("personal_email", "no_email", "opted_out"):
        out.append(reason)
    return out


def email_draft_allowed(line: "SheetLine", resolved_prospect: Prospect | None = None) -> bool:
    """Phase 3: may an email draft be made for this sheet contact? False for a personal (free / ISP) mailbox,
    a missing email, an email opt-out or do-not-contact. When the resolved Nooks prospect is given its email
    is held to the same rule too. Never exposes the address."""
    if not isinstance(line, SheetLine):
        return False
    if email_reason(line.entry.prospect) != "ok":
        return False
    return resolved_prospect is None or email_reason(resolved_prospect) == "ok"


# ---------------------------------------------------------------- research packet
def build_packet(ranked: RankedAccount, prospect: Prospect, sigs: list[Signal], config: Config,
                 history: ContactHistory | None = None, now: datetime | None = None) -> ResearchPacket:
    a = ranked.account
    cd = _call_date(ranked)
    psigs = [_packet_signal(s, cd, now, config) for s in sigs]
    facts: list[FactItem] = []
    excluded = 0
    for s, ps in zip(sigs, psigs):
        if ps.status in ("future", "stale"):
            excluded += 1                      # not trustworthy as a fact: listed under signals only
            continue
        text = ps.evidence
        if ps.status == "intent":
            text = f"Intent signal (interest, not a dated event): {text}"
        facts.append(FactItem(text, ps.source_label))
    if a.employees is not None:
        facts.append(FactItem(f"About {a.employees} employees", "Salesforce account record (ZoomInfo employee count)"))
    loc = _location(a)
    if loc:
        facts.append(FactItem(f"Based in {loc}", "Salesforce account record"))
    industry = getattr(a, "industry", None)
    industry = _clean(industry) if industry else None
    if industry:
        facts.append(FactItem(f"Industry: {industry}", "Salesforce account record"))
    summary = history.summary() if history else None
    if summary:
        facts.append(FactItem(f"Previous contact: {summary}", history.source_label))  # type: ignore[union-attr]
    notes = []
    if len(facts) < 2:
        notes.append(f"Only {len(facts)} verified fact(s) available. Write the missing fact as 'none verified'. "
                     "Do not pad.")
    if excluded:
        notes.append(f"{_plural(excluded, 'signal')} left out of the facts (future-dated or older than its limit): "
                     "do not quote it.")
    usable = [(s, p) for s, p in zip(sigs, psigs) if p.status in ("ok", "undated")]
    e_reason = email_reason(prospect)
    claims = list(MUST_NOT_CLAIM)
    caution = list(dict.fromkeys(n for _, p in usable for n in p.notes if n != _clean(sigmod.AMBIGUOUS_NOTE)))
    if caution:
        notes.append("Trigger caution: " + "; ".join(caution) + ". State no more than the scoop text says.")
        claims.append("Do not state more about the trigger than its text says. Caution on it: " + "; ".join(caution) + ".")
    if e_reason != "ok":
        claims.append(f"Do not suggest or draft an email to this contact ({_EMAIL_REASON_TEXT[e_reason]}).")
    return ResearchPacket(
        account_key=account_key(a), account_name=_clean(a.name), website=_clean(a.website) if a.website else None,
        industry=industry, state=_clean(a.billing_state or a.hq_state) if (a.billing_state or a.hq_state) else None,
        city=_clean(a.billing_city) if a.billing_city else None, employees=a.employees,
        tier=None if a.size_unknown else tier_for(config, a.employees),
        prospect_first_name=_first_name(prospect.name),
        prospect_title=_clean(prospect.title) if prospect.title else None,
        prospect_sf_contact_id=prospect.sf_contact_id,
        local_time=ranked.local_call_time.isoformat() if ranked.local_call_time else None, window=ranked.window,
        timezone=a.timezone, signals=psigs, has_real_trigger=bool(usable),
        trigger_kind=usable[0][1].kind if usable else None,
        ambiguity_flags=[sigmod.AMBIGUOUS_NOTE for s in sigs if sigmod.is_ambiguous(s)],
        sdr_target=a.is_sdr_target,
        contact_history=summary, contact_history_source=history.source_label if summary else None,
        available_facts=facts, must_not_claim=claims, email_ok=e_reason == "ok", email_reason=e_reason,
        facts_note=" ".join(notes),
        must_not_phrases=_banned_for(caution, " ".join(p.evidence for _, p in usable)))


def render_packet_text(p: ResearchPacket) -> str:
    """Compact text for a prompt. Contains only packet fields (no email / phone exists in a packet)."""
    L = [f"ACCOUNT: {p.account_name}" + (f" ({p.website})" if p.website else "")]
    meta = [x for x in (p.industry, p.city, p.state) if x]
    if p.employees is not None:
        meta.append(f"{p.employees} employees" + (f", {p.tier}" if p.tier else ""))
    if meta:
        L.append("PROFILE: " + "; ".join(meta))
    L.append(f"CALLING: {p.prospect_first_name or 'the contact (first name unknown)'}, "
             f"{p.prospect_title or 'title unknown'}"
             f" (SF contact {p.prospect_sf_contact_id or 'id unknown'}) | "
             f"{p.local_time or 'unscheduled'} ({p.window or '-'}, {p.timezone or '-'})")
    if p.sdr_target:
        L.append("SDR TARGET: yes (internal flag, do not mention on the call)")
    usable = [x for x in p.signals if x.status not in ("future", "stale")]
    unusable = [x for x in p.signals if x.status in ("future", "stale")]
    if usable:
        L.append("SIGNALS (real, sourced):")
        for s in usable:
            L.append(f"  - {s.label} | {s.evidence} | source: {s.source_label} | {s.verdict_reason}")
    if unusable:
        L.append("NOT USABLE AS FACTS (do not quote or build an opener on these):")
        for s in unusable:
            L.append(f"  - {s.label} | {s.source_label} | {s.verdict_reason}")
    if p.signals:
        if not p.has_real_trigger:
            L.append("TRIGGER: none usable (interest signal only, or date not trustworthy): say so "
                     "(TRIGGER: None found) and write a fit-based opener.")
    else:
        L.append("SIGNALS: none. There is no real trigger: say so (TRIGGER: None found) and write a fit-based opener.")
    for f in p.ambiguity_flags:
        L.append(f"AMBIGUOUS: {f}")
    if p.contact_history:
        L.append(f"HISTORY: {p.contact_history} (source: {p.contact_history_source})")
    L.append(f"AVAILABLE FACTS ({p.fact_count}):")
    for i, f in enumerate(p.available_facts, 1):
        L.append(f"  {i}. {f.text} (source: {f.source})")
    if p.facts_note:
        L.append(f"NOTE: {p.facts_note}")
    L.append("MUST NOT INVENT / CLAIM:")
    L.extend(f"  - {m}" for m in p.must_not_claim)
    return "\n".join(L)


# ---------------------------------------------------------------- sheet
def _sort_key_rank(ra: RankedAccount):
    best = ra.signals[0] if ra.signals else None
    ts = best.source_date.timestamp() if best and best.source_date else float("-inf")
    return (-ra.score, -ts, not ra.account.is_sdr_target, ra.account.name.casefold(), account_key(ra.account))


def _bucket(items: Sequence[RankedAccount], reason_of) -> list[BucketItem]:
    return [BucketItem(_clean(r.account.name), _clean(reason_of(r))) for r in items]


def _group_index(pools: Sequence[Account]) -> dict[str, int]:
    """account_key -> duplicate group number, from ``accounts.duplicate_groups`` (the groups the account builder
    flagged with 'possible_duplicate group dup-...' notes; unflagged hand-built accounts are detected fresh).
    Accounts are de-duplicated by key; a key in two groups keeps the first."""
    seen: dict[str, Account] = {}
    for a in pools:
        seen.setdefault(account_key(a), a)
    # Only what the account builder flagged collapses (an unflagged list is not re-detected here: that would repeat
    # the builder's pairwise name work over every account on each sheet build).
    if not any(acc_mod.FLAG_POSSIBLE_DUPLICATE in a.flags or any(n.startswith("possible_duplicate") for n in a.notes)
               for a in seen.values()):
        return {}
    out: dict[str, int] = {}
    for n, members in enumerate(acc_mod.duplicate_groups(seen.values())):
        for m in members:
            out.setdefault(account_key(m), n)
    return out


def probable_duplicates(a: Account, b: Account) -> bool:
    """True when the account builder put a and b in one duplicate group (see ``_group_index``)."""
    if a is b:
        return False
    idx = _group_index([a, b])
    return account_key(a) in idx and idx.get(account_key(a)) == idx.get(account_key(b))


def build_sheet(rank_result: RankResult, accounts: Sequence[Account], signals_by_account: Mapping[str, list[Signal]],
                prospects: Sequence[Prospect], config: Config, now: datetime, *, limit: int | None = None,
                history: Mapping[str, ContactHistory] | None = None) -> Sheet:
    """Assemble the sheet. Only ``rank_result.ranked`` can become entries.

    ``limit`` keeps the top N by score, THEN orders them by call time (grouped by timezone window).
    ``accounts`` / ``prospects`` are fallbacks only (prospect lookup when the RankResult lacks one).
    ``history`` maps account_key -> ContactHistory. ``rank`` is the position among ALL ranked accounts
    (dropped ones included), so tags can skip a number."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if limit is not None and limit < 0:
        raise ValueError("limit must be >= 0")
    history = history or {}
    run_date = now.astimezone(ZoneInfo(RUN_DATE_TZ)).date()
    sheet = Sheet(run_date=run_date, limit=limit, ranked_total=len(rank_result.ranked))

    by_acct: dict[str, list[Prospect]] = {}
    for p in prospects:
        for i in {normalise_sf_id(p.sf_account_id), normalise_sf_id(p.nooks_account_id)}:
            if i:
                by_acct.setdefault(i, []).append(p)

    ordered = sorted(rank_result.ranked, key=_sort_key_rank)
    picked: list[tuple[int, RankedAccount, Prospect]] = []
    for pos, ra in enumerate(ordered, 1):
        key = account_key(ra.account)
        name = _clean(ra.account.name)
        if ra.skipped_reason:
            sheet.dropped.append(BucketItem(name, f"in ranked but has skipped_reason: {_clean(ra.skipped_reason)}"))
            continue
        if ra.local_call_time is None or ra.window is None:
            sheet.dropped.append(BucketItem(name, "no scheduled call time"))
            continue
        chosen = (rank_result.prospects.get(key) or [None])[0]
        if chosen is None:
            cand = []
            for i in (normalise_sf_id(ra.account.sf_account_id), normalise_sf_id(ra.account.nooks_account_id)):
                cand.extend(by_acct.get(i, []) if i else [])
            chosen = pick_prospect(cand, config) if cand else None
        if chosen is None:
            sheet.dropped.append(BucketItem(name, "no callable prospect found"))
            continue
        if chosen.do_not_call or chosen.opted_out_email:
            sheet.dropped.append(BucketItem(name, "chosen prospect is do-not-call / opted out"))
            continue
        picked.append((pos, ra, chosen))
    # Collapse probable duplicates BEFORE the limit: keep the higher-scoring account of a flagged pair (picked is
    # in score order), mention the other on the kept line, never put both on the sheet.
    kept: list[tuple[int, RankedAccount, Prospect]] = []
    also: dict[int, list[str]] = {}
    gidx = _group_index([*accounts, *(t[1].account for t in picked)])
    for item in picked:
        gi = gidx.get(account_key(item[1].account))
        twin = next((k for k in kept if gi is not None and gidx.get(account_key(k[1].account)) == gi), None)
        if twin is None:
            kept.append(item)
            continue
        also.setdefault(twin[0], []).append(_clean(item[1].account.name))
        sheet.dropped.append(BucketItem(_clean(item[1].account.name),
                                        f"probable duplicate of {_clean(twin[1].account.name)} (kept the higher score)"))
    picked = kept
    if limit is not None:
        picked = picked[:limit]

    def instant(t):
        return t[1].local_call_time.astimezone(timezone.utc)

    grouped: dict[tuple, list] = {}
    for t in picked:
        ra = t[1]
        gk = (ra.account.timezone, ra.window, ra.local_call_time.date())
        grouped.setdefault(gk, []).append(t)
    for v in grouped.values():
        v.sort(key=lambda t: (instant(t), t[0]))
    order = sorted(grouped, key=lambda g: (instant(grouped[g][0]), grouped[g][0][0]))
    n = 0
    for gk in order:
        tz, window, d = gk
        label = f"{tz} {window} {d:%a %d %b}"
        glines = []
        for pos, ra, pr in grouped[gk]:
            n += 1
            sigs = _signals_for(ra, signals_by_account)
            why = build_why(ra, sigs, config, now)
            pk = account_key(ra.account)
            why.insert(0, f"Prospect: {_who(pr.name)}, {_clean(pr.title) if pr.title else 'title unknown'}")
            reason = email_reason(pr)
            if reason in ("personal_email", "no_email", "opted_out"):
                why.append(f"Flag: {_EMAIL_REASON_TEXT[reason]}")
            for other in also.get(pos, []):
                why.append(f"also on your list as {other}: probable same company")
            ln = SheetLine(SheetEntry(ranked=ra, prospect=pr), pos, n, label, run_date, why,
                           _flags(ra, sigs, config, now) + _contact_flags(pr),
                           build_packet(ra, pr, sigs, config, history.get(pk), now))
            ln._seal(_SEAL)
            glines.append(ln)
            sheet.lines.append(ln)
        sheet.groups.append((label, glines))

    sheet.needs_prospect = _bucket(rank_result.needs_prospect, lambda r: (
        f"{rank_result.needs_prospect_codes.get(account_key(r.account), 'unknown')}, score {r.score:g}"))
    sheet.spilled = _bucket(rank_result.spilled, lambda r: (
        f"score {r.score:g}, held to {r.local_call_time:%a %d %b %H:%M}" if r.local_call_time else f"score {r.score:g}"))
    sheet.skipped = _bucket(rank_result.skipped, lambda r: r.skipped_reason or "unknown")
    counts: dict[str, int] = {}
    for r in rank_result.skipped:
        k = _clean((r.skipped_reason or "unknown").split(" (rank ")[0].split(" until ")[0])
        counts[k] = counts.get(k, 0) + 1
    sheet.skipped_reason_counts = dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    return sheet


# ---------------------------------------------------------------- rendering
def _when(ln: SheetLine) -> str:
    return ln.local_call_time.strftime("%a %H:%M %Z")


def _bucket_data(sheet: Sheet) -> list[tuple[str, list[BucketItem]]]:
    return [("NEEDS PROSPECT (find a contact; never a task)", sheet.needs_prospect),
            ("SPILLED (held for a later day; no task tonight)", sheet.spilled),
            ("SKIPPED", sheet.skipped),
            ("DROPPED FROM SHEET (ranked but not sheetable)", sheet.dropped)]


def _bucket_lines(title: str, items: list[BucketItem], limit: int | None, esc=lambda s: s) -> list[str]:
    if not items:
        return []
    shown = items if limit is None else items[:limit]
    out = [f"{title} ({len(items)}):"]
    out += [f"  - {esc(b.name)}: {esc(b.reason)}" for b in shown]
    if len(shown) < len(items):
        out.append(f"  ... and {len(items) - len(shown)} more")
    return out


def _skip_counts(sheet: Sheet, esc=lambda s: s) -> list[str]:
    if not sheet.skipped_reason_counts:
        return []
    return ["Top skip reasons: " + "; ".join(f"{esc(k)} ({v})" for k, v in list(sheet.skipped_reason_counts.items())[:5])]


_FLAG_DISPLAY = {"personal_email": _EMAIL_REASON_TEXT["personal_email"]}


def _check_limit(bucket_limit) -> None:
    if bucket_limit is not None and (isinstance(bucket_limit, bool) or not isinstance(bucket_limit, int)
                                     or bucket_limit < 0):
        raise ValueError("bucket_limit must be None or an int >= 0")


def render_sheet_text(sheet: Sheet, bucket_limit: int | None = BUCKET_LIST_LIMIT) -> str:
    """Terminal table. ``bucket_limit`` caps each bucket's names (default 25); None lists EVERY account
    (use None for the run log: CLAUDE.md rule 5)."""
    _check_limit(bucket_limit)
    L = [f"CALL SHEET {sheet.run_date.isoformat()}: {len(sheet.lines)} of {sheet.ranked_total} callable accounts"
         + (f" (top {sheet.limit} by score)" if sheet.limit is not None else "")
         + f" | needs prospect {len(sheet.needs_prospect)}, spilled {len(sheet.spilled)}, skipped {len(sheet.skipped)}"]
    if not sheet.lines:
        L.append("(no callable accounts)")
    for label, lines in sheet.groups:
        L.append("")
        L.append(f"== {label} ==")
        for ln in lines:
            pr = ln.prospect
            why = "; ".join(ln.why_chosen[1:3]) if len(ln.why_chosen) > 1 else ln.why_chosen[0]
            row = (f"{ln.call_no:>2}. {_when(ln):<14} {_short(ln.account_name, 34):<34} "
                   f"{_who(pr.name)}, {_short(_clean(pr.title or '?'), 28)} | score {ln.score:.0f} (#{ln.rank}) | {_short(why, 120)}")
            if ln.flags:
                row += " | flags: " + ", ".join(_FLAG_DISPLAY.get(f, f) for f in ln.flags)
            L.append(row)
    extra = []
    for title, items in _bucket_data(sheet):
        extra += _bucket_lines(title, items, bucket_limit)
    extra += _skip_counts(sheet)
    if extra:
        L += ["", *extra]
    return _scrub("\n".join(L)) + "\n"


def render_sheet_markdown(sheet: Sheet, bucket_limit: int | None = BUCKET_LIST_LIMIT) -> str:
    """The short summary doc. Names, titles and evidence are markdown-escaped and shortened."""
    _check_limit(bucket_limit)
    L = [f"# Call sheet {sheet.run_date.isoformat()}", "",
         f"{len(sheet.lines)} accounts built from {sheet.ranked_total} callable"
         + (f" (top {sheet.limit} by score, then ordered by local call time)" if sheet.limit is not None else "")
         + ". Nothing was sent or written to Nooks. Contact details are not shown; the writer looks them up by prospect id.", ""]
    for label, lines in sheet.groups:
        L.append(f"## {_md(label)}")
        L.append("")
        for ln in lines:
            pr = ln.prospect
            L.append(f"### {ln.call_no}. {_md(ln.account_name, 80)} ({_when(ln)})")
            L.append(f"- Call: {_md(_who(pr.name), 40)}, {_md(pr.title or 'title unknown', 80)}")
            L.append(f"- Score: {ln.score:.1f} (rank {ln.rank}); " + ", ".join(
                f"{_md(k)} {v:g}" for k, v in ln.ranked.score_breakdown.items() if v))
            L.append("- Why chosen:")
            L += [f"  - {_md(w, 220)}" for w in ln.why_chosen]
            if ln.packet.facts_note:
                L.append(f"- Facts: {_md(ln.packet.facts_note, 300)}")
            L.append("")
    extra = []
    esc = lambda s: _md(s, 200)  # noqa: E731
    for title, items in _bucket_data(sheet):
        block = _bucket_lines(title, items, bucket_limit, esc)
        if block:
            extra.append(f"**{block[0]}**")
            extra += [("- " + x.strip()[2:]) if x.startswith("  - ") else f"- {x.strip()}" for x in block[1:]]
    sc = _skip_counts(sheet, esc)
    if sc:
        extra.append(sc[0])
    if extra:
        L += ["## Not on the sheet", ""] + extra + [""]
    return _scrub("\n".join(L))


def sheet_to_json(sheet: Sheet) -> str:
    def line(ln: SheetLine) -> dict:
        return {
            "call_no": ln.call_no, "rank": ln.rank, "group": ln.group, "account": ln.account_name,
            "account_key": ln.packet.account_key, "sf_contact_id": ln.prospect.sf_contact_id,
            "nooks_prospect_id": ln.prospect.nooks_id,
            "prospect_first_name": _first_name(ln.prospect.name),
            "prospect_title": _clean(ln.prospect.title) if ln.prospect.title else None,
            "window": ln.ranked.window, "local_call_time": ln.local_call_time.isoformat(),
            "score": ln.score, "score_breakdown": dict(ln.ranked.score_breakdown),
            "why_chosen": list(ln.why_chosen), "flags": list(ln.flags), "packet": ln.packet.to_dict()}
    b = lambda items: [{"name": i.name, "reason": i.reason} for i in items]  # noqa: E731
    return json.dumps({
        "run_date": sheet.run_date.isoformat(), "limit": sheet.limit, "ranked_total": sheet.ranked_total,
        "lines": [line(x) for x in sheet.lines], "needs_prospect": b(sheet.needs_prospect),
        "spilled": b(sheet.spilled), "skipped": b(sheet.skipped), "dropped": b(sheet.dropped),
        "skipped_reason_counts": sheet.skipped_reason_counts}, indent=2, sort_keys=True)


# ---------------------------------------------------------------- Nooks task payload (build only)
OPENER_FIELDS = ("opener", "fact_1", "fact_2", "gatekeeper", "trigger")
_PLACEHOLDER_TEXT = {"none", "na", "nil", "null", "nan", "undefined", "unknown", "tbd", "todo"}
# OPENER and GATEKEEPER must be real text; the three honest 'none verified' forms are only for the others.
_STRICT_FIELDS = ("opener", "gatekeeper")


def priority_for(score: float) -> str:
    for floor, name in PRIORITY_BANDS:
        if score >= floor:
            return name
    return PRIORITY_FLOOR


def task_tag(run_date: date, rank: int) -> str:
    return f"[{TAG_PREFIX} {run_date.isoformat()} #{rank}]"


def _field_text(name: str, value: object, account: str) -> str:
    if not isinstance(value, str):
        raise SheetError(f"{account}: opener field {name} must be a string, got {type(value).__name__}")
    text = " ".join(value.translate(_ZERO_WIDTH).replace(" ", " ").split())
    core = re.sub(r"[\W_]+", "", text).lower()
    if not core:
        raise SheetError(f"{account}: empty opener field: {name}")
    if core in _PLACEHOLDER_TEXT:
        raise SheetError(f"{account}: opener field {name} is a placeholder ({text!r}), not real text")
    if name in _STRICT_FIELDS and core.startswith(("noneverified", "nonefound")):
        raise SheetError(f"{account}: opener field {name} must be real text, not 'none verified'")
    if _has_contact_data(text):
        raise SheetError(f"{account}: opener field {name} contains an email address or phone number")
    return text


_PLACEHOLDER_WORDS = ("placeholder", "replace", "changeme", "todo", "example", "test", "dummy", "fake", "owner_id",
                      "ownerid", "your", "xxx", "sample", "demo", "none", "null")
OWNER_ID_MIN_LEN = 20                     # real Nooks owner ids are long (28-char Firebase UIDs)
DRY_RUN_PREFIX = "[DRY RUN] "


class DryRunPayload(dict):
    """A payload built with ``dry_run=True``. Never write one to Nooks: check ``isinstance(p, DryRunPayload)``."""


def _check_owner(owner_id: object, live: bool, expected: str | None = None) -> str:
    if not isinstance(owner_id, str) or not owner_id.strip():
        raise SheetError("owner_id is required: a non-blank string")
    oid = owner_id.strip()
    if not ID_PATTERN.fullmatch(oid):
        raise SheetError("owner_id must match [A-Za-z0-9_-]{1,128}")
    if expected is not None:
        if not isinstance(expected, str) or not ID_PATTERN.fullmatch(expected.strip() if isinstance(expected, str) else ""):
            raise SheetError("expected_owner_id must be a valid owner id string")
        if oid.lower() == OWNER_ID_PLACEHOLDER.lower() and live:
            raise SheetError("owner_id is still the placeholder: pass the real Nooks owner id (or dry_run=True)")
        if live and oid != expected.strip():
            raise SheetError("owner_id does not match expected_owner_id (the id Nooks getMe returned)")
        return oid
    if live:
        low = oid.lower()
        if len(oid) < OWNER_ID_MIN_LEN or any(w in low for w in _PLACEHOLDER_WORDS):
            raise SheetError("owner_id looks like a placeholder: pass the real Nooks owner id (or dry_run=True)")
    return oid


def _fold_id(value: object) -> str:
    """Comparison form for Nooks ids (the same rule state.py stores, made stricter): NFKC, accents and
    combining marks dropped, zero-width / BOM / NBSP removed, stripped, casefolded. Only ever merges more
    ids together, which fails closed."""
    t = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", str(value)).translate(_ZERO_WIDTH))
    t = "".join(c for c in t if not unicodedata.combining(c)).replace("\u00a0", " ")
    return t.strip().casefold()


def is_dry_run_payload(payload: object) -> bool:
    """True when ``payload`` is (or looks like) a dry-run payload, or is not a usable mapping at all.
    Survives ``dict(p)``, ``{**p}``, ``p.copy()`` and a JSON round trip: it also checks the note prefix
    and the placeholder owner id. The Phase 3 writer MUST refuse when this is True."""
    if isinstance(payload, DryRunPayload):
        return True
    if not isinstance(payload, Mapping):
        return True
    note = payload.get("note")
    owner = payload.get("ownerId")
    if not isinstance(note, str) or not isinstance(owner, str):
        return True
    return (note.lstrip("\u200b\ufeff \u00a0\t\r\n").upper().startswith(DRY_RUN_PREFIX.strip())
            or owner.strip().lower() == OWNER_ID_PLACEHOLDER.lower())


def _blocked_code(reason: str) -> str:
    r = reason.lower()
    for needle, code in (("flag set", "dnc_flag"), ("dispositioned", "dnc_prospect_id"), ("phone", "dnc_phone"),
                         ("same email", "dnc_email"), ("opt-out", "opt_out_email")):
        if needle in r:
            return code
    return "blocked"


def _resolve_prospect(line: SheetLine, resolved: Prospect) -> Prospect:
    """Normalise and verify the resolved Nooks prospect. Returns a copy carrying the normalised ids."""
    name = line.account_name
    if not isinstance(resolved, Prospect):
        raise SheetError(f"{name}: resolved_prospect must be a Prospect")
    nid = (resolved.nooks_id or "").strip() if isinstance(resolved.nooks_id, str) else ""
    if not nid:
        raise SheetError(f"{name}: no resolved Nooks prospect id (never guess or use None)")
    if not ID_PATTERN.fullmatch(nid):
        raise SheetError(f"{name}: resolved Nooks prospect id has unexpected characters")
    want = normalise_sf_id(line.entry.prospect.sf_contact_id) if line.entry.prospect else None
    got = normalise_sf_id(resolved.sf_contact_id)
    if not want:
        raise SheetError(f"{name}: sheet contact has no SF contact id: cannot verify who Nooks resolved")
    if not got:
        raise SheetError(f"{name}: resolved prospect has no SF contact id: cannot verify identity")
    if want != got:
        raise SheetError(f"{name}: resolved prospect is a different contact ({got} != {want})")
    acct, racct = normalise_sf_id(line.account.sf_account_id), normalise_sf_id(resolved.sf_account_id)
    if acct and racct and acct != racct:
        raise SheetError(f"{name}: resolved prospect belongs to a different account")
    return replace(resolved, nooks_id=nid, sf_contact_id=got)


def _check_line(line: SheetLine) -> None:
    """The line must be one build_sheet made, still consistent with itself."""
    name = line.account_name
    if line._sealed is not _SEAL or not isinstance(line._snap, dict):
        raise SheetError(f"{name}: not a sheet line made by build_sheet (hand-built or copied)")
    snap = line._snap
    if isinstance(line.rank, bool) or not isinstance(line.rank, int) or line.rank < 1:
        raise SheetError(f"{name}: rank must be an int >= 1")
    if type(line.run_date) is not date:
        raise SheetError(f"{name}: run_date must be a date")
    if line.rank != snap["rank"] or line.run_date != snap["run_date"]:
        raise SheetError(f"{name}: sheet line changed after build (rank / run date)")
    ra = line.ranked
    if ra.skipped_reason or snap["due"] is None or snap["window"] is None:
        raise SheetError(f"{name}: not a scheduled callable account")
    if snap["due"].tzinfo is None:
        raise SheetError("local_call_time must be timezone-aware")
    pr = line.entry.prospect
    if pr is None:
        raise SheetError(f"{name}: sheet line has no prospect")
    if line.packet.account_key != account_key(line.account):
        raise SheetError(f"{name}: sheet line changed after build (account)")
    if line.packet.prospect_sf_contact_id != pr.sf_contact_id:
        raise SheetError(f"{name}: sheet line changed after build (contact)")


def prepare_task(line: SheetLine, resolved_prospect: Prospect, state: State, opener_fields: Mapping[str, str],
                 owner_id: str, *, dry_run: bool = False, now: datetime | None = None,
                 expected_owner_id: str | None = None) -> dict:
    """THE way to build a Nooks task-create ``data`` dict. Pure: no I/O.

    Live (``dry_run is False``, the default) requires: a real owner id (placeholder-looking ids refused), a
    healthy ``State`` (Do Not Call rebuilt, ``dnc_is_rebuilt``) and an aware ``now``. ``dry_run`` must be
    exactly True or False; True allows a placeholder owner, a fresh state and no ``now``, and returns a
    ``DryRunPayload`` whose note starts '[DRY RUN] '.

    In order: line integrity (sealed by build_sheet, int rank >= 1, date run_date, packet still matches the
    entry, scheduled, not skipped); due time not already passed; the resolved Nooks id is trimmed and the SF
    ids normalised BEFORE any comparison; both SF contact ids must be present and equal (fail closed); the
    Do Not Call / opt-out check (``state.is_blocked(.., 'any')``) runs on the resolved prospect with the
    Nooks id in its exact, lower and upper case forms (plus a scan of the DNC id set with both sides folded: NFKC, accents, zero-width, casefold) AND on the sheet contact, raising ``BlockedError`` with
    a fixed message and a short code (never the raw reason, which can hold a phone number); opener fields are
    validated (real text, no email / phone, ``NOTE_MAX_CHARS`` cap); the payload is built from exactly the id
    that was checked, with due time, priority, window, rank and run date taken from the snapshot frozen when
    ``build_sheet`` sealed the line (later changes to the shared RankedAccount cannot alter it).

    ``expected_owner_id``: the owner id Phase 3 resolved at run time (Nooks getMe). Phase 3 MUST pass it: live
    then requires an exact match and the placeholder-word blocklist is skipped (a real id may contain 'test')."""
    if not isinstance(line, SheetLine):
        raise SheetError("payloads are built from sheet lines only")
    if dry_run is not True and dry_run is not False:
        raise SheetError("dry_run must be exactly True or False")
    live = dry_run is False
    name = line.account_name
    oid = _check_owner(owner_id, live, expected_owner_id)
    if not isinstance(state, State):
        raise SheetError(f"{name}: state must be a State (got {type(state).__name__})")
    if live and not dnc_is_rebuilt(state):
        raise SheetError(f"{name}: Do Not Call memory is not rebuilt: refusing a live payload")
    if now is None:
        if live:
            raise SheetError(f"{name}: now (aware datetime) is required for a live payload")
    elif not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    _check_line(line)
    ra = line.ranked
    snap = line._snap
    due = snap["due"]
    if now is not None and due <= now:
        raise SheetError(f"{name}: due time {due.isoformat()} has already passed")
    checked = _resolve_prospect(line, resolved_prospect)
    candidates = [("resolved prospect", checked)]
    for variant in (checked.nooks_id.lower(), checked.nooks_id.upper()):
        if variant != checked.nooks_id:
            candidates.append(("resolved prospect", replace(checked, nooks_id=variant)))
    candidates.append(("sheet contact", line.entry.prospect))
    folded = _fold_id(checked.nooks_id)
    if any(_fold_id(i) == folded for i in state.dnc_prospect_ids if isinstance(i, str)):
        raise BlockedError(f"{name}: resolved prospect blocked: Do Not Call / opt-out [dnc_prospect_id]")
    for who, p in candidates:
        blocked, reason = is_blocked(p, state, "any")
        if blocked:
            raise BlockedError(f"{name}: {who} blocked: Do Not Call / opt-out [{_blocked_code(reason)}]")
    f = {k: _field_text(k, opener_fields.get(k), name) for k in OPENER_FIELDS}
    note = "\n".join([
        ("" if live else DRY_RUN_PREFIX) + task_tag(snap["run_date"], snap["rank"]),
        f"OPENER: {f['opener']}", f"FACT 1: {f['fact_1']}", f"FACT 2: {f['fact_2']}",
        f"GATEKEEPER: {f['gatekeeper']}", f"TRIGGER: {f['trigger']}"])
    if len(note) > NOTE_MAX_CHARS:
        raise SheetError(f"{name}: note is {len(note)} characters, over the {NOTE_MAX_CHARS} cap (not truncated)")
    payload = {"action": "call", "dueAt": due.isoformat(), "ownerId": oid,
               "prospectId": checked.nooks_id, "priority": snap["priority"], "note": note}
    return payload if live else DryRunPayload(payload)


def sheet_needs_prospect_resolution(sheet: Sheet) -> list[str]:
    """SF contact ids whose Nooks prospect id Phase 3 must look up (one filter_crmId lookup each), in call
    order, de-duplicated. Lines whose prospect already has a nooks_id need no lookup. A line with neither
    id cannot be resolved: it is NOT listed here and ``prepare_task`` refuses it (flag 'no contact id').
    After resolving, call ``prepare_task``: it re-runs the do-not-contact check by Nooks prospect id."""
    out, seen = [], set()
    for ln in sheet.lines:
        p = ln.entry.prospect
        if p is None or p.nooks_id:
            continue
        cid = p.sf_contact_id
        if cid and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out
