"""Call sheet assembly (task 2c). Deterministic Python only: no LLM, no I/O, no network.

Takes a RankResult plus plain arguments and returns a ``Sheet`` (what to call, in what order, and why),
a ``ResearchPacket`` per entry (the ONLY facts a later Claude session may use for the opener), text /
markdown / JSON renderers, and a builder for the Nooks task-create payload (Phase 3). Nothing here
sends or writes anything. Notes: docs/callsheet_notes.md.

Hard rules honoured here (CLAUDE.md):
  * rule 6: no invented trigger. An account without a real signal says "No trigger: ranked on fit only".
  * never a contact email or phone in a packet or a rendered output (Nooks writer looks up by prospect id).
  * needs_prospect / spilled / skipped accounts are never sheet entries and never payloads.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src import signals as sigmod
from src.config import Config, tier_for
from src.loaders import normalise_sf_id
from src.models import Account, Prospect, RankedAccount, SheetEntry, Signal, SignalKind
from src.state import State, is_blocked
from src.ranker import RankResult, account_key, pick_prospect

RUN_DATE_TZ = "Australia/Sydney"          # run_date in the task tag = 'now' in the BDR's home zone
TAG_PREFIX = "apac-callsheet"
OWNER_ID_PLACEHOLDER = "OWNER_ID_PLACEHOLDER"   # the caller resolves this (Nooks owner id)
BUCKET_LIST_LIMIT = 25

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
    "Do not pad to two facts: if fewer than two facts are available, say so (FACT 2: none verified).",
    "Do not include or guess an email address or phone number.",
)


class SheetError(ValueError):
    """Refusal to build a sheet entry or payload (never silently degrade)."""


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
        return f"{self.source} (matched by {self.matched_by})" if self.matched_by else self.source

    def summary(self) -> str | None:
        if self.last_call_date is None and not self.last_outcome and not self.calls_total:
            return None
        parts = []
        if self.last_call_date is not None:
            parts.append(f"last call {self.last_call_date.isoformat()}")
        if self.last_outcome:
            parts.append(f"outcome {self.last_outcome}")
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
    verdict_reason: str
    ambiguous: bool

    def to_dict(self) -> dict:
        return dict(self.__dict__)


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
    prospect_first_name: str | None
    prospect_title: str | None
    prospect_sf_contact_id: str | None       # identifies the contact (never email / phone); Nooks id may be None
    local_time: str | None                   # ISO with offset
    window: str | None
    timezone: str | None
    signals: list[PacketSignal]
    has_real_trigger: bool
    ambiguity_flags: list[str]
    sdr_target: bool
    contact_history: str | None
    contact_history_source: str | None
    available_facts: list[FactItem]
    must_not_claim: list[str]
    facts_note: str                          # empty when two or more facts are available

    @property
    def fact_count(self) -> int:
        return len(self.available_facts)

    def to_dict(self) -> dict:
        return {
            "account_key": self.account_key, "account_name": self.account_name, "website": self.website,
            "industry": self.industry, "state": self.state, "city": self.city, "employees": self.employees,
            "tier": self.tier, "prospect_first_name": self.prospect_first_name,
            "prospect_title": self.prospect_title,
            "prospect_sf_contact_id": self.prospect_sf_contact_id, "local_time": self.local_time, "window": self.window,
            "timezone": self.timezone, "signals": [s.to_dict() for s in self.signals],
            "has_real_trigger": self.has_real_trigger, "ambiguity_flags": list(self.ambiguity_flags),
            "sdr_target": self.sdr_target, "contact_history": self.contact_history,
            "contact_history_source": self.contact_history_source,
            "available_facts": [f.to_dict() for f in self.available_facts], "fact_count": self.fact_count,
            "facts_note": self.facts_note, "must_not_claim": list(self.must_not_claim),
        }


@dataclass
class SheetLine:
    """One sheet row: the shared ``SheetEntry`` plus everything the sheet adds."""

    entry: SheetEntry
    rank: int                                # 1 = best score (position in the ranked list)
    call_no: int                             # 1-based position in call order
    group: str                               # e.g. 'Australia/Sydney morning Thu 26 Nov'
    run_date: date
    why_chosen: list[str]
    flags: list[str]
    packet: ResearchPacket

    @property
    def ranked(self) -> RankedAccount:
        return self.entry.ranked

    @property
    def account(self) -> Account:
        return self.entry.ranked.account

    @property
    def prospect(self) -> Prospect:
        return self.entry.prospect  # type: ignore[return-value]

    @property
    def score(self) -> float:
        return self.entry.ranked.score

    @property
    def local_call_time(self) -> datetime:
        return self.entry.ranked.local_call_time  # type: ignore[return-value]


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


# ---------------------------------------------------------------- helpers
def _first_name(name: str | None) -> str | None:
    parts = (name or "").split()
    return parts[0] if parts else None


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _short(text: str, n: int = 110) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _source_label(sig: Signal) -> str:
    d = sig.source_date.date().isoformat() if sig.source_date else "date unknown"
    if sig.source.startswith(sigmod.SOURCE_PREFIX):
        return f"{sigmod.SOURCE_LABEL} {d}"
    if sig.kind is SignalKind.INTENT:
        return f"ZoomInfo intent {d} (unverified connector shape)"
    return f"{sig.source} {d}"


def _clean_evidence(sig: Signal) -> str:
    ev = sig.evidence
    if sigmod.is_ambiguous(sig):
        ev = ev[: -len(sigmod.AMBIGUOUS_SUFFIX)]
    return ev


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


def _age(sig: Signal, call_date: date | None) -> int | None:
    if sig.source_date is None or call_date is None:
        return None
    return sigmod.age_days(sig.source_date, call_date)


def _age_text(age: int | None) -> str:
    if age is None:
        return "date unknown"
    return "today" if age == 0 else f"{_plural(age, 'day')} ago"


def _verdict_reason(sig: Signal, age: int | None, config: Config) -> str:
    label = KIND_LABELS.get(sig.kind, sig.kind.value)
    if sig.kind is SignalKind.INTENT:
        return "kept as a capped interest signal, not a dated event"
    limit = sigmod.max_age_for(sig.kind, config)
    base = f"kept: {label} within {limit} day limit"
    if age is not None:
        base += f" ({_plural(age, 'day')} old)"
    if sigmod.is_ambiguous(sig):
        base += f"; {sigmod.AMBIGUOUS_NOTE}"
    return base


def _packet_signal(sig: Signal, call_date: date | None, config: Config) -> PacketSignal:
    age = _age(sig, call_date)
    return PacketSignal(
        kind=sig.kind.value, label=KIND_LABELS.get(sig.kind, sig.kind.value), evidence=_clean_evidence(sig),
        source=sig.source, source_label=_source_label(sig),
        date=sig.source_date.date().isoformat() if sig.source_date else None, age_days=age,
        verdict_reason=_verdict_reason(sig, age, config), ambiguous=sigmod.is_ambiguous(sig))


def _location(a: Account) -> str | None:
    parts = [p for p in (a.billing_city, a.billing_state or a.hq_state) if p]
    return ", ".join(parts) if parts else None


def build_why(ranked: RankedAccount, sigs: list[Signal], config: Config) -> list[str]:
    """Plain-English lines derived ONLY from data. Never claims a trigger that is not a real signal."""
    a = ranked.account
    cd = _call_date(ranked)
    out: list[str] = []
    if a.is_sdr_target:
        out.append("SDR target")
    if sigs:
        b = sigs[0]
        label = KIND_LABELS.get(b.kind, b.kind.value)
        src = "ZoomInfo scoop" if b.source.startswith(sigmod.SOURCE_PREFIX) else (
            "ZoomInfo intent" if b.kind is SignalKind.INTENT else b.source)
        out.append(f"{label}: {_short(_clean_evidence(b))} ({src}, {_age_text(_age(b, cd))})")
        if sigmod.is_ambiguous(b):
            out.append(f"Flag: {sigmod.AMBIGUOUS_NOTE}")
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
        out.append(f"Flag: {f}")
    return out


def _flags(ranked: RankedAccount, sigs: list[Signal]) -> list[str]:
    a = ranked.account
    out = []
    if any(sigmod.is_ambiguous(s) for s in sigs):
        out.append("role ambiguous")
    if not sigs:
        out.append("no trigger")
    if a.size_unknown:
        out.append("size unknown")
    if a.non_anz_billing_anz_hq:
        out.append("non-ANZ billing")
    if a.timezone_guessed:
        out.append("tz guessed")
    if a.confidence == "low":
        out.append("low confidence")
    out.extend(a.flags)
    return out


def _contact_flags(pr: Prospect) -> list[str]:
    return ["no contact id"] if not (pr.nooks_id or pr.sf_contact_id) else []


# ---------------------------------------------------------------- research packet
def build_packet(ranked: RankedAccount, prospect: Prospect, sigs: list[Signal], config: Config,
                 history: ContactHistory | None = None) -> ResearchPacket:
    a = ranked.account
    cd = _call_date(ranked)
    psigs = [_packet_signal(s, cd, config) for s in sigs]
    facts: list[FactItem] = []
    for s, ps in zip(sigs, psigs):
        text = ps.evidence
        if s.kind is SignalKind.INTENT:
            text = f"Intent signal (interest, not a dated event): {text}"
        facts.append(FactItem(text, ps.source_label))
    if a.employees is not None:
        facts.append(FactItem(f"About {a.employees} employees", "Salesforce account record (ZoomInfo employee count)"))
    loc = _location(a)
    if loc:
        facts.append(FactItem(f"Based in {loc}", "Salesforce account record"))
    industry = getattr(a, "industry", None)
    if industry:
        facts.append(FactItem(f"Industry: {industry}", "Salesforce account record"))
    summary = history.summary() if history else None
    if summary:
        facts.append(FactItem(f"Previous contact: {summary}", history.source_label))  # type: ignore[union-attr]
    note = ""
    if len(facts) < 2:
        note = (f"Only {len(facts)} verified fact(s) available. Write the missing fact as 'none verified'. "
                "Do not pad.")
    return ResearchPacket(
        account_key=account_key(a), account_name=a.name, website=a.website, industry=industry,
        state=a.billing_state or a.hq_state, city=a.billing_city, employees=a.employees,
        tier=None if a.size_unknown else tier_for(config, a.employees),
        prospect_first_name=_first_name(prospect.name), prospect_title=prospect.title,
        prospect_sf_contact_id=prospect.sf_contact_id,
        local_time=ranked.local_call_time.isoformat() if ranked.local_call_time else None, window=ranked.window,
        timezone=a.timezone, signals=psigs, has_real_trigger=bool(sigs),
        ambiguity_flags=[sigmod.AMBIGUOUS_NOTE for s in sigs if sigmod.is_ambiguous(s)],
        sdr_target=a.is_sdr_target,
        contact_history=summary, contact_history_source=history.source_label if summary else None,
        available_facts=facts, must_not_claim=list(MUST_NOT_CLAIM), facts_note=note)


def render_packet_text(p: ResearchPacket) -> str:
    """Compact text for a prompt. Contains only packet fields (no email / phone exists in a packet)."""
    L = [f"ACCOUNT: {p.account_name}" + (f" ({p.website})" if p.website else "")]
    meta = [x for x in (p.industry, p.city, p.state) if x]
    if p.employees is not None:
        meta.append(f"{p.employees} employees" + (f", {p.tier}" if p.tier else ""))
    if meta:
        L.append("PROFILE: " + "; ".join(meta))
    L.append(f"CALLING: {p.prospect_first_name or '(name unknown)'}, {p.prospect_title or 'title unknown'}"
             f" (SF contact {p.prospect_sf_contact_id or 'id unknown'}) | {p.local_time or 'unscheduled'} ({p.window or '-'}, {p.timezone or '-'})")
    if p.sdr_target:
        L.append("SDR TARGET: yes (internal flag, do not mention on the call)")
    if p.signals:
        L.append("SIGNALS (real, sourced):")
        for s in p.signals:
            L.append(f"  - {s.label} | {s.evidence} | source: {s.source_label} | {s.verdict_reason}")
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
    return [BucketItem(r.account.name, reason_of(r)) for r in items]


def build_sheet(rank_result: RankResult, accounts: Sequence[Account], signals_by_account: Mapping[str, list[Signal]],
                prospects: Sequence[Prospect], config: Config, now: datetime, *, limit: int | None = None,
                history: Mapping[str, ContactHistory] | None = None) -> Sheet:
    """Assemble the sheet. Only ``rank_result.ranked`` can become entries.

    ``limit`` keeps the top N by score, THEN orders them by call time (grouped by timezone window).
    ``accounts`` / ``prospects`` are fallbacks only (prospect lookup when the RankResult lacks one).
    ``history`` maps account_key -> ContactHistory."""
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
        if ra.skipped_reason:
            sheet.dropped.append(BucketItem(ra.account.name, f"in ranked but has skipped_reason: {ra.skipped_reason}"))
            continue
        if ra.local_call_time is None or ra.window is None:
            sheet.dropped.append(BucketItem(ra.account.name, "no scheduled call time"))
            continue
        chosen = (rank_result.prospects.get(key) or [None])[0]
        if chosen is None:
            cand = []
            for i in (normalise_sf_id(ra.account.sf_account_id), normalise_sf_id(ra.account.nooks_account_id)):
                cand.extend(by_acct.get(i, []) if i else [])
            chosen = pick_prospect(cand, config) if cand else None
        if chosen is None:
            sheet.dropped.append(BucketItem(ra.account.name, "no callable prospect found"))
            continue
        picked.append((pos, ra, chosen))
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
            why = build_why(ra, sigs, config)
            pk = account_key(ra.account)
            fn = _first_name(pr.name)
            why.insert(0, f"Prospect: {fn or 'name unknown'}, {pr.title or 'title unknown'}")
            ln = SheetLine(entry=SheetEntry(ranked=ra, prospect=pr), rank=pos, call_no=n, group=label,
                           run_date=run_date, why_chosen=why, flags=_flags(ra, sigs) + _contact_flags(pr),
                           packet=build_packet(ra, pr, sigs, config, history.get(pk)))
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
        k = (r.skipped_reason or "unknown").split(" (rank ")[0].split(" until ")[0]
        counts[k] = counts.get(k, 0) + 1
    sheet.skipped_reason_counts = dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    return sheet


# ---------------------------------------------------------------- rendering
def _when(ln: SheetLine) -> str:
    return ln.local_call_time.strftime("%a %H:%M %Z")


def _bucket_lines(title: str, items: list[BucketItem]) -> list[str]:
    if not items:
        return []
    out = [f"{title} ({len(items)}):"]
    out += [f"  - {b.name}: {b.reason}" for b in items[:BUCKET_LIST_LIMIT]]
    if len(items) > BUCKET_LIST_LIMIT:
        out.append(f"  ... and {len(items) - BUCKET_LIST_LIMIT} more")
    return out


def _bucket_section(sheet: Sheet) -> list[str]:
    out = _bucket_lines("NEEDS PROSPECT (find a contact; never a task)", sheet.needs_prospect)
    out += _bucket_lines("SPILLED (held for a later day; no task tonight)", sheet.spilled)
    out += _bucket_lines("SKIPPED", sheet.skipped)
    out += _bucket_lines("DROPPED FROM SHEET (ranked but not sheetable)", sheet.dropped)
    if sheet.skipped_reason_counts:
        out.append("Top skip reasons: " + "; ".join(f"{k} ({v})" for k, v in list(sheet.skipped_reason_counts.items())[:5]))
    return out


def render_sheet_text(sheet: Sheet) -> str:
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
            row = (f"{ln.call_no:>2}. {_when(ln):<14} {_short(ln.account.name, 34):<34} "
                   f"{_first_name(pr.name) or '?'}, {_short(pr.title or '?', 28)} | score {ln.score:.0f} (#{ln.rank}) | {_short(why, 120)}")
            if ln.flags:
                row += " | flags: " + ", ".join(ln.flags)
            L.append(row)
    extra = _bucket_section(sheet)
    if extra:
        L += ["", *extra]
    return "\n".join(L) + "\n"


def render_sheet_markdown(sheet: Sheet) -> str:
    L = [f"# Call sheet {sheet.run_date.isoformat()}", "",
         f"{len(sheet.lines)} accounts built from {sheet.ranked_total} callable"
         + (f" (top {sheet.limit} by score, then ordered by local call time)" if sheet.limit is not None else "")
         + ". Nothing was sent or written to Nooks. Contact details are not shown; the writer looks them up by prospect id.", ""]
    for label, lines in sheet.groups:
        L.append(f"## {label}")
        L.append("")
        for ln in lines:
            pr = ln.prospect
            L.append(f"### {ln.call_no}. {ln.account.name} ({_when(ln)})")
            L.append(f"- Call: {_first_name(pr.name) or 'name unknown'}, {pr.title or 'title unknown'}")
            L.append(f"- Score: {ln.score:.1f} (rank {ln.rank}); " + ", ".join(
                f"{k} {v:g}" for k, v in ln.ranked.score_breakdown.items() if v))
            L.append("- Why chosen:")
            L += [f"  - {w}" for w in ln.why_chosen]
            if ln.packet.facts_note:
                L.append(f"- Facts: {ln.packet.facts_note}")
            L.append("")
    extra = _bucket_section(sheet)
    if extra:
        L += ["## Not on the sheet", ""]
        L += [("- " + x.strip()[2:] if x.startswith("  - ") else f"**{x}**") for x in extra]
        L.append("")
    return "\n".join(L)


def sheet_to_json(sheet: Sheet) -> str:
    def line(ln: SheetLine) -> dict:
        return {
            "call_no": ln.call_no, "rank": ln.rank, "group": ln.group, "account": ln.account.name,
            "account_key": ln.packet.account_key, "sf_contact_id": ln.prospect.sf_contact_id,
            "nooks_prospect_id": ln.prospect.nooks_id,
            "prospect_first_name": _first_name(ln.prospect.name), "prospect_title": ln.prospect.title,
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


def priority_for(score: float) -> str:
    for floor, name in PRIORITY_BANDS:
        if score >= floor:
            return name
    return PRIORITY_FLOOR


def task_tag(run_date: date, rank: int) -> str:
    return f"[{TAG_PREFIX} {run_date.isoformat()} #{rank}]"


def nooks_task_payload(line: SheetLine, opener_fields: Mapping[str, str], nooks_prospect_id: str | None, *,
                       owner_id: str = OWNER_ID_PLACEHOLDER) -> dict:
    """Build the task-create ``data`` dict. Pure: no I/O. Refuses on any empty opener field, when the
    line is not a genuine callable sheet line (needs_prospect / spilled / skipped are never SheetLines),
    or when ``nooks_prospect_id`` is None/blank (the id is resolved lazily in Phase 3, one lookup per
    contact; the sheet's own prospect may have nooks_id None). Before calling this, the caller MUST have
    run ``recheck_blocked(line, resolved_prospect, state)`` on the resolved Nooks prospect."""
    if not isinstance(line, SheetLine):
        raise SheetError("payloads are built from sheet lines only")
    ra = line.ranked
    if ra.skipped_reason or ra.local_call_time is None or ra.window is None:
        raise SheetError(f"{line.account.name}: not a scheduled callable account")
    if not isinstance(nooks_prospect_id, str) or not nooks_prospect_id.strip():
        raise SheetError(f"{line.account.name}: no resolved Nooks prospect id (never guess or use None)")
    missing = [f for f in OPENER_FIELDS if not str(opener_fields.get(f, "")).strip()]
    if missing:
        raise SheetError(f"{line.account.name}: empty opener field(s): {', '.join(missing)}")
    if ra.local_call_time.tzinfo is None:
        raise SheetError("local_call_time must be timezone-aware")
    f = {k: " ".join(str(opener_fields[k]).split()) for k in OPENER_FIELDS}
    note = "\n".join([
        task_tag(line.run_date, line.rank),
        f"OPENER: {f['opener']}", f"FACT 1: {f['fact_1']}", f"FACT 2: {f['fact_2']}",
        f"GATEKEEPER: {f['gatekeeper']}", f"TRIGGER: {f['trigger']}"])
    return {"action": "call", "dueAt": ra.local_call_time.isoformat(), "ownerId": owner_id,
            "prospectId": nooks_prospect_id.strip(), "priority": priority_for(ra.score), "note": note}


# ---------------------------------------------------------------- Phase 3 resolution helpers
def sheet_needs_prospect_resolution(sheet: Sheet) -> list[str]:
    """SF contact ids whose Nooks prospect id Phase 3 must look up (one filter_crmId lookup each), in call
    order, de-duplicated. Lines whose prospect already has a nooks_id need no lookup. A line with neither
    id cannot be resolved: it is NOT listed here and ``nooks_task_payload`` refuses it (flag 'no contact id')."""
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


def recheck_blocked(line: SheetLine, resolved_prospect: Prospect, state: State) -> tuple[bool, str]:
    """(ok, reason) for a Nooks prospect resolved in Phase 3. ok=False means do NOT build a payload.

    Wraps ``state.is_blocked`` (do-not-call by Nooks prospect id, phone or email, plus email opt-out) run on the
    RESOLVED prospect, since the dry-run contact had no Nooks id. Also refuses a resolved prospect with no
    Nooks id or whose SF contact id differs from the sheet's contact (wrong person)."""
    if not (resolved_prospect.nooks_id or "").strip():
        return False, "resolved prospect has no Nooks id"
    want = line.entry.prospect.sf_contact_id if line.entry.prospect else None
    if want and resolved_prospect.sf_contact_id and want != resolved_prospect.sf_contact_id:
        return False, f"resolved prospect is a different contact ({resolved_prospect.sf_contact_id} != {want})"
    blocked, reason = is_blocked(resolved_prospect, state)
    return (False, reason or "blocked") if blocked else (True, "")
