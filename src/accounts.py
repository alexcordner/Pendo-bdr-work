"""Account list builder (task 1d): merge sources, drop noise, flag doubtful accounts.

Pure and offline: takes raw connector rows, returns ``Account`` objects plus an audit trail of
every drop. Nothing is written anywhere. Input rows are never mutated.

Pipeline (``build_account_list``)
  1. Convert rows with ``src.loaders`` (a malformed row is dropped with reason ``malformed_row``).
  2. Merge sources in ``config.sources`` order (Salesforce, then Nooks, then ZoomInfo). Match keys,
     in order: ``sf_account_id``, ``zoominfo_id``, ``nooks_account_id``, then the normalised company
     name as a LAST resort (only across different sources, never when ids conflict, with a note).
     The highest-priority source keeps its fields; missing (None) fields are filled from the
     others, and the ``source_*`` flags are OR-ed.
  3. Drop, with a reason (never silently), in this order:
     customer, blocked, not_anz, noise_*, over_max_employees, under_min_employees, recently_touched.
  4. Sort deterministically (source priority, then name, then ids).

Decisions (also in docs/accounts_notes.md)
  * "Non-ANZ billing, ANZ HQ" group (~420 accounts, ``Account.non_anz_billing_anz_hq``):
    INCLUDED, never silently dropped. They get the notes ``flagged_non_anz_billing_anz_hq`` and
    ``low_confidence`` so Alex can review them (``list_flagged``). ``Account`` has no confidence
    field, so low confidence is carried as a note tag (see ``confidence``).
  * ``employees is None`` is KEPT with the note ``size_unknown`` (size rules apply only when known).
  * ZoomInfo-only rows (id and name only) are kept and flagged ``zoominfo_only`` + ``low_confidence``
    because country, size and domain cannot be established; the ranker should rank them low.
    Nooks-only rows get ``nooks_only`` + ``low_confidence`` (Nooks rows carry no country).
    Country is only enforced when Salesforce is a source (the only source that states it).
  * ``filters.drop_if_no_domain`` is NOT applied here: ZoomInfo free search returns no domain, so
    it would drop every ZoomInfo row. Flagged for the coordinator.
  * Cooldown boundary: an account touched ``d`` days ago is dropped when ``d < cooldown_days``
    (default ``limits.cooldown_days_no_answer``). Touched exactly ``cooldown_days`` ago is eligible
    again. A touch dated today or in the future is always within the window.
  * ``is_sdr_target`` is left on the account as a ranking boost; it is not a filter.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable, Mapping

from src.config import Config
from src.loaders import LoaderError, account_from_nooks, account_from_sf_row, account_from_zoominfo
from src.models import Account

# ---------------------------------------------------------------- reasons and note tags
R_CUSTOMER = "customer"
R_BLOCKED = "blocked"
R_NOT_ANZ = "not_anz"
R_NOISE_LOCATION = "noise_location"
R_NOISE_LOCAL_BUSINESS = "noise_local_business"
R_NOISE_GOVERNMENT = "noise_government"
R_NOISE_VAGUE = "noise_vague_name"
R_OVER_MAX = "over_max_employees"
R_UNDER_MIN = "under_min_employees"
R_TOUCHED = "recently_touched"
R_MALFORMED = "malformed_row"

NOTE_SIZE_UNKNOWN = "size_unknown"
NOTE_ZOOMINFO_ONLY = "zoominfo_only"
NOTE_NOOKS_ONLY = "nooks_only"
NOTE_FLAGGED_GROUP = "flagged_non_anz_billing_anz_hq"
NOTE_LOW_CONFIDENCE = "low_confidence"

# Names of well-known cities: a config drop pattern that is NOT one of these (e.g. "CBD") marks
# a locality, so "<Place> CBD Florist" is local-business noise, while "Sydney Software Pty Ltd"
# (a city name inside a real company name) is not.
_CITY_WORDS = {"sydney", "melbourne", "brisbane", "auckland", "perth", "adelaide", "hobart",
               "darwin", "canberra", "wellington", "christchurch", "gold coast", "newcastle"}
_NAME_SUFFIXES = {"pty", "ltd", "limited", "inc", "incorporated", "llc", "plc", "proprietary"}
_GENERIC_WORDS = {"funds", "fund", "group", "holdings", "company", "services", "the", "and", "of",
                  "co", "corporation", "business", "limited", "ltd", "pty", "inc"}
_PLACEHOLDER_WORDS = {"placeholder", "tbd", "unknown", "unnamed"}
_GOV_RE = re.compile(
    r"\b(government|commissioner|department of|dept of|ministry of|minister for|parliament|"
    r"public service|office of the|city council|shire council|regional council)\b")
_NOT_ANZ_RE = re.compile(r"\(not anz\)")


# ---------------------------------------------------------------- result types
@dataclass(frozen=True)
class Dropped:
    """One dropped row: why, and which source it came from (highest-priority source)."""

    name: str
    id: str | None
    reason: str          # one of the R_* slugs
    source: str          # 'salesforce' | 'nooks' | 'zoominfo'
    detail: str = ""     # human-readable specifics


@dataclass
class AccountListResult:
    accounts: list[Account] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)   # drop reason -> count


# ---------------------------------------------------------------- name helpers
def _tokens(name: str) -> list[str]:
    """Lower-case alphanumeric words ('&' becomes 'and'); punctuation and brackets vanish."""
    return re.sub(r"[^a-z0-9]+", " ", (name or "").casefold().replace("&", " and ")).split()


def normalise_name(name: str | None) -> str:
    """Matching key: case/punctuation-insensitive, trailing Pty/Ltd/Limited/Inc style suffixes
    and a leading 'The' removed. ``"The Example Widgets Pty. Ltd."`` -> ``"example widgets"``."""
    toks = _tokens(name or "")
    while toks and toks[-1] in _NAME_SUFFIXES:
        toks.pop()
    if len(toks) > 1 and toks[0] == "the":
        toks.pop(0)
    return " ".join(toks)


# ---------------------------------------------------------------- noise detection
def noise_reason(account: Account, config: Config) -> tuple[str, str] | None:
    """(reason, detail) when the account is non-company noise, else None.

    Rules: pure location names, locality + trade businesses of small/unknown size (not for
    Salesforce-book accounts, which are curated), government bodies, vague/placeholder names.
    """
    norm = normalise_name(account.name)
    toks = norm.split()
    if not toks:
        return R_NOISE_VAGUE, "empty name"
    patterns = [" ".join(_tokens(p)) for p in config.filters.drop_name_patterns if _tokens(p)]
    padded = f" {norm} "
    hits = [p for p in patterns if f" {p} " in padded]
    markers = {p for p in patterns if p not in _CITY_WORDS} | {"cbd"}
    marker_hit = any(f" {m} " in padded for m in markers)

    if hits:
        pattern_words = {w for p in hits for w in p.split()}
        others = [t for t in toks if t not in pattern_words]
        if not others or (marker_hit and len(others) <= 1):
            return R_NOISE_LOCATION, f"name is just a place ({account.name!r})"
        small = account.employees is None or account.employees < config.company_size.min_employees
        if marker_hit and small and not account.source_salesforce:
            return R_NOISE_LOCAL_BUSINESS, (
                f"locality name with trade words and small/unknown size ({account.name!r})")

    if _GOV_RE.search(" ".join(_tokens(account.name))):
        return R_NOISE_GOVERNMENT, f"government body or department ({account.name!r})"
    if set(toks) & _PLACEHOLDER_WORDS or all(t in _GENERIC_WORDS for t in toks):
        return R_NOISE_VAGUE, f"vague or placeholder name ({account.name!r})"
    return None


# ---------------------------------------------------------------- merging
def _source_of(a: Account) -> str:
    return "salesforce" if a.source_salesforce else "nooks" if a.source_nooks else "zoominfo"


_FILL_FIELDS = ("website", "country_raw", "country", "billing_state", "billing_city", "hq_state",
                "hq_country_raw", "employees", "zoominfo_id", "nooks_account_id", "sf_account_id",
                "timezone", "days_since_last_activity", "prospecting_priority")


def _merge_into(base: Account, other: Account, how: str) -> None:
    """Fold ``other`` (lower priority) into ``base`` (both are objects this module created)."""
    filled = []
    for f in _FILL_FIELDS:
        if getattr(base, f) is None and getattr(other, f) is not None:
            setattr(base, f, getattr(other, f))
            filled.append(f)
    if "timezone" in filled:
        base.timezone_guessed = other.timezone_guessed
    if "employees" in filled:
        base.notes = [n for n in base.notes if n != NOTE_SIZE_UNKNOWN]
    base.is_customer = base.is_customer or other.is_customer
    base.is_sdr_target = base.is_sdr_target or other.is_sdr_target
    base.source_salesforce |= other.source_salesforce
    base.source_nooks |= other.source_nooks
    base.source_zoominfo |= other.source_zoominfo
    base.notes.append(f"merged {_source_of(other)} row by {how}"
                      + (f"; filled {', '.join(filled)}" if filled else ""))


def _no_conflict(base: Account, other: Account) -> bool:
    """Name-match guard: two accounts with different ids for the same key are different companies."""
    for f in ("sf_account_id", "zoominfo_id", "nooks_account_id"):
        a, b = getattr(base, f), getattr(other, f)
        if a is not None and b is not None and a != b:
            return False
    return True


def _sort_key(a: Account, prio: dict[str, int]):
    return (prio.get(_source_of(a), 99), a.name.casefold(), a.sf_account_id or "",
            a.zoominfo_id or "", a.nooks_account_id or "")


# ---------------------------------------------------------------- main entry
def build_account_list(sf_rows: Iterable[dict], nooks_account_rows: Iterable[dict],
                       zoominfo_rows: Iterable[dict], config: Config,
                       touched: Mapping[str, date], today: date,
                       blocked_account_ids: set[str] = frozenset(),
                       cooldown_days: int | None = None) -> AccountListResult:
    """Merge the three sources and apply the drop rules. See the module docstring.

    ``touched`` maps an account id (sf_account_id, or zoominfo/nooks id for non-SF accounts) to the
    date it was last touched. ``cooldown_days`` overrides ``limits.cooldown_days_no_answer``.
    A Nooks row may carry the optional key ``_sf_account_id`` (set by the caller when it was
    fetched with ``listAccounts filter_crmId=<SF id>``) so it merges into the Salesforce account.
    """
    cooldown = config.limits.cooldown_days_no_answer if cooldown_days is None else cooldown_days
    prio = {s: i for i, s in enumerate(config.sources)}
    result = AccountListResult()

    # 1. load rows per source; sort each so the outcome does not depend on input order
    loaded: dict[str, list[Account]] = {"salesforce": [], "nooks": [], "zoominfo": []}
    for source, rows in (("salesforce", sf_rows), ("nooks", nooks_account_rows),
                         ("zoominfo", zoominfo_rows)):
        for row in rows:
            try:
                if source == "salesforce":
                    acct = account_from_sf_row(row, config)
                elif source == "nooks":
                    acct = account_from_nooks(row, sf_account_id=row.get("_sf_account_id"))
                else:
                    acct = account_from_zoominfo(row)
            except LoaderError as e:
                name = row.get("Name") or row.get("name") or (row.get("attributes") or {}).get("name") or ""
                rid = row.get("Id") or row.get("id")
                result.dropped.append(Dropped(str(name), None if rid is None else str(rid),
                                              R_MALFORMED, source, str(e)))
                continue
            loaded[source].append(acct)
    for lst in loaded.values():
        lst.sort(key=lambda a: _sort_key(a, prio))

    # 2. merge in config.sources priority order
    merged: list[Account] = []
    by_key: dict[tuple[str, str], Account] = {}
    by_name: dict[str, list[Account]] = defaultdict(list)

    def index(a: Account) -> None:
        for kind, val in (("sf", a.sf_account_id), ("zi", a.zoominfo_id), ("nk", a.nooks_account_id)):
            if val:
                by_key.setdefault((kind, val), a)
        n = normalise_name(a.name)
        if n and a not in by_name[n]:
            by_name[n].append(a)

    for source in config.sources:
        for acct in loaded[source]:
            match, how = None, ""
            for kind, val, label in (("sf", acct.sf_account_id, "sf_account_id"),
                                     ("zi", acct.zoominfo_id, "zoominfo_id"),
                                     ("nk", acct.nooks_account_id, "nooks_account_id")):
                if val and (kind, val) in by_key:
                    match, how = by_key[(kind, val)], label
                    break
            if match is None:
                n = normalise_name(acct.name)
                for cand in by_name.get(n, []) if n else []:
                    different_source = _source_of(cand) != source and not getattr(
                        cand, f"source_{source}")
                    if different_source and _no_conflict(cand, acct):
                        match, how = cand, "normalised name (last resort, verify)"
                        break
            if match is None:
                merged.append(acct)
                index(acct)
            else:
                _merge_into(match, acct, how)
                index(match)

    # 3. flags and drops
    kept: list[Account] = []
    for a in merged:
        _annotate(a)
        drop = _drop_reason(a, config, touched, today, blocked_account_ids, cooldown)
        if drop:
            reason, detail = drop
            result.dropped.append(Dropped(a.name, a.sf_account_id or a.zoominfo_id or a.nooks_account_id,
                                          reason, _source_of(a), detail))
        else:
            kept.append(a)

    result.accounts = sorted(kept, key=lambda a: _sort_key(a, prio))
    result.dropped.sort(key=lambda d: (d.reason, prio.get(d.source, 99), d.name.casefold(), d.id or ""))
    result.stats = dict(Counter(d.reason for d in result.dropped))
    return result


def _annotate(a: Account) -> None:
    """Add the review flags (notes) for flagged-group, ZoomInfo-only and Nooks-only accounts."""
    def add(tag: str, text: str) -> None:
        if tag not in a.notes:
            a.notes.append(text)

    if a.non_anz_billing_anz_hq:
        add(NOTE_FLAGGED_GROUP, f"{NOTE_FLAGGED_GROUP}: included for review (billing is non-ANZ, HQ is ANZ)")
        add(NOTE_LOW_CONFIDENCE, NOTE_LOW_CONFIDENCE)
    if not a.source_salesforce:
        if a.source_zoominfo and not a.source_nooks:
            add(NOTE_ZOOMINFO_ONLY, f"{NOTE_ZOOMINFO_ONLY}: no Salesforce/Nooks match; "
                                    "country, size and domain unverified, rank low")
        else:
            add(NOTE_NOOKS_ONLY, f"{NOTE_NOOKS_ONLY}: no Salesforce match; country unverified, rank low")
        add(NOTE_LOW_CONFIDENCE, NOTE_LOW_CONFIDENCE)
    if a.employees is None and not any(n.startswith(NOTE_SIZE_UNKNOWN) for n in a.notes):
        a.notes.append(NOTE_SIZE_UNKNOWN)


def _touch_date(a: Account, touched: Mapping[str, date]) -> date | None:
    dates = []
    for key in (a.sf_account_id, a.zoominfo_id, a.nooks_account_id):
        d = touched.get(key) if key else None
        if isinstance(d, datetime):
            d = d.date()
        if d is not None:
            dates.append(d)
    return max(dates) if dates else None


def _drop_reason(a: Account, config: Config, touched: Mapping[str, date], today: date,
                 blocked: set[str], cooldown: int) -> tuple[str, str] | None:
    """First matching drop rule as (reason, detail), or None to keep."""
    if a.is_customer:
        return R_CUSTOMER, "Current_Customer__c is true"
    ids = {a.sf_account_id, a.zoominfo_id, a.nooks_account_id} - {None}
    if ids & set(blocked):
        return R_BLOCKED, f"id in blocked set ({', '.join(sorted(ids & set(blocked)))})"
    # Country is only known when Salesforce is a source; Salesforce accounts with no ANZ country drop.
    if a.source_salesforce and a.country is None:
        return R_NOT_ANZ, f"BillingCountry {a.country_raw!r} / HQ {a.hq_country_raw!r} is not Australia/New Zealand"
    if _NOT_ANZ_RE.search(a.name.casefold()):
        return R_NOT_ANZ, f"name marks the company as not ANZ ({a.name!r})"
    noise = noise_reason(a, config)
    if noise:
        return noise
    if a.employees is not None:
        if a.employees > config.company_size.max_employees:
            return R_OVER_MAX, f"{a.employees} employees > max {config.company_size.max_employees}"
        if a.employees < config.company_size.min_employees:
            return R_UNDER_MIN, f"{a.employees} employees < min {config.company_size.min_employees}"
    last = _touch_date(a, touched)
    if last is not None and (today - last).days < cooldown:
        return R_TOUCHED, f"touched {last.isoformat()} ({(today - last).days} days ago) within {cooldown}-day cooldown"
    return None


# ---------------------------------------------------------------- helpers for callers
def list_flagged(accounts: Iterable[Account]) -> list[Account]:
    """Accounts in the 'non-ANZ billing, ANZ HQ' group, for Alex to review. Config-free."""
    return [a for a in accounts if a.non_anz_billing_anz_hq]


def confidence(account: Account) -> str:
    """'low' for flagged-group / ZoomInfo-only / Nooks-only accounts, else 'normal'.
    (Account has no confidence field, so this reads the ``low_confidence`` note tag.)"""
    return "low" if NOTE_LOW_CONFIDENCE in account.notes else "normal"


def summarise(result: AccountListResult) -> str:
    """Plain-text block for the run log: kept/dropped totals, per-reason counts, <=5 sample names each."""
    kept = result.accounts
    lines = [f"accounts kept: {len(kept)}",
             f"  flagged non-ANZ billing / ANZ HQ (included, low confidence): {len(list_flagged(kept))}",
             f"  zoominfo_only: {sum(1 for a in kept if any(n.startswith(NOTE_ZOOMINFO_ONLY) for n in a.notes))}",
             f"  size_unknown: {sum(1 for a in kept if a.size_unknown)}",
             f"accounts dropped: {len(result.dropped)}"]
    by_reason: dict[str, list[Dropped]] = defaultdict(list)
    for d in result.dropped:
        by_reason[d.reason].append(d)
    for reason in sorted(by_reason):
        items = by_reason[reason]
        lines.append(f"  {reason}: {len(items)}")
        names = [d.name for d in items[:5]]
        lines.append("    e.g. " + "; ".join(names) + (" ..." if len(items) > 5 else ""))
    return "\n".join(lines)
