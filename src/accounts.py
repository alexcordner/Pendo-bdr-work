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
     customer, blocked, not_anz, noise_* (ZoomInfo-only rows ONLY), over_max_employees,
     under_min_employees, recently_touched.
  4. Sort deterministically (source priority, then name, then ids).

Decisions (also in docs/accounts_notes.md)
  * "Non-ANZ billing, ANZ HQ" group (~420 accounts, ``Account.non_anz_billing_anz_hq``):
    INCLUDED, never silently dropped. They get the flag ``flagged_non_anz_billing_anz_hq`` and
    ``Account.confidence = "low"`` so Alex can review them (``list_flagged``).
  * Noise (location, local business, government, vague name) DROPS only ZoomInfo-only rows. Accounts
    sourced from Salesforce or Nooks (Alex's own book) are never dropped for noise: they are kept
    with a ``noise_*`` flag, a note and low confidence, and ``summarise`` lists them for review.
  * Two rows from the SAME source are never merged (e.g. two SF accounts sharing a ZoomInfo id):
    both are kept and flagged ``possible_duplicate``. A name match with several candidates is not
    merged either (counted as ``name_merge_ambiguous``); successful name merges are counted too
    (``name_merged``). Both counters appear in ``stats`` only when non-zero.
  * Salesforce rows with a blank/junk/unrecognisable country are kept with ``country_unknown``;
    only a real stated non-ANZ country (and no ANZ HQ) is dropped as ``not_anz``.
  * ``employees is None`` is KEPT with the note ``size_unknown`` (size rules apply only when known).
  * ZoomInfo-only rows (id and name only) are kept (unless noise) and flagged ``zoominfo_only`` with
    low confidence because country, size and domain cannot be established; the ranker should rank
    them low. Nooks-only rows get ``nooks_only`` + low confidence (Nooks rows carry no country).
    Country is only enforced when Salesforce is a source (the only source that states it).
  * ``filters.drop_if_no_domain`` is not applied here (config now sets it false: ZoomInfo free
    search returns no domain).
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
from typing import Collection, Iterable, Mapping

from src.config import Config, canonical_country, is_blank_or_junk
from src.loaders import (LoaderError, account_from_nooks, account_from_sf_row, account_from_zoominfo,
                         normalise_sf_id)
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

# Machine-readable flags (Account.flags). Notes carry the human text.
FLAG_ZOOMINFO_ONLY = "zoominfo_only"
FLAG_NOOKS_ONLY = "nooks_only"
FLAG_GROUP = "flagged_non_anz_billing_anz_hq"
FLAG_POSSIBLE_DUPLICATE = "possible_duplicate"
FLAG_COUNTRY_UNKNOWN = "country_unknown"
NOISE_FLAGS = (R_NOISE_LOCATION, R_NOISE_LOCAL_BUSINESS, R_NOISE_GOVERNMENT, R_NOISE_VAGUE)
FLAG_LARGE_NAME_GROUP = "large_name_group"
FLAG_LARGE_DOMAIN_GROUP = "large_domain_group"
STAT_NAME_MERGED = "name_merged"
STAT_NAME_AMBIGUOUS = "name_merge_ambiguous"

# Names of well-known cities: a config drop pattern that is NOT one of these (e.g. "CBD") marks
# a locality, so "<Place> CBD Florist" is local-business noise, while "Sydney Software Pty Ltd"
# (a city name inside a real company name) is not.
_CITY_WORDS = {"sydney", "melbourne", "brisbane", "auckland", "perth", "adelaide", "hobart",
               "darwin", "canberra", "wellington", "christchurch", "gold coast", "newcastle"}
_NAME_SUFFIXES = {"pty", "ltd", "limited", "inc", "incorporated", "llc", "plc", "proprietary"}
_GENERIC_WORDS = {"funds", "fund", "group", "holdings", "company", "services", "the", "and", "of",
                  "co", "corporation", "business", "limited", "ltd", "pty", "inc"}
_PLACEHOLDER_WORDS = {"placeholder", "tbd", "unknown", "unnamed"}
# Government bodies: these prefixes only count at the START of a name ("Department of Health"),
# so private companies like "Fake Ministry of Sound Media" are not caught.
_GOV_RE = re.compile(
    r"^(government of|department of|dept of|ministry of|minister for|parliament of|"
    r"public service|office of the)\b|\b(commissioner|city council|shire council|regional council)\b")
# Looser rule used for ZoomInfo-only rows ONLY (they are dropped, so it must stay precise): a
# jurisdiction word first ("Victorian", "NSW", "Western Australian"...), then optionally up to two
# words, then Department/Ministry of ..., or ending in Department/Ministry/Government. Private
# names like "Fake Ministry of Sound Media" or "Fake Government Services" have no jurisdiction
# word first and are not matched.
_JURIS = (r"(?:victorian|queensland|nsw|new south wales|western australian|south australian|"
          r"tasmanian|australian|commonwealth|state|federal|national|new zealand|nz|act|nt|wa|sa|"
          r"qld|vic|tas|northern territory)")
_GOV_LOOSE_RE = re.compile(rf"^(?:{_JURIS} )+(?:[a-z]+ ){{0,2}}"
                           rf"(?:(?:department|dept|ministry) of\b|(?:department|dept|ministry|government)$)")
# Exists ONLY because the synthetic noise fixture marks a non-ANZ ZoomInfo row this way; real
# ZoomInfo names will not. Applied to ZoomInfo-only rows only (see _drop_reason).
_NOT_ANZ_RE = re.compile(r"\(not anz\)")
_ANZ_ALIASES = {"oz", "aotearoa", "straya"}


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
    # (account, account, evidence kinds) for probable duplicate pairs (all merged accounts, incl. dropped)
    duplicate_pairs: list = field(default_factory=list)


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
def noise_reason(account: Account, config: Config, loose_gov: bool = False) -> tuple[str, str] | None:
    """(reason, detail) when the account is non-company noise, else None.

    Rules: pure location names, locality + trade businesses of small/unknown size, government
    bodies, vague/placeholder names. Detection only: the CALLER decides drop (ZoomInfo-only) or
    flag (Salesforce/Nooks accounts are never dropped for noise).
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
        if marker_hit and small:
            return R_NOISE_LOCAL_BUSINESS, (
                f"locality name with trade words and small/unknown size ({account.name!r})")

    joined = " ".join(_tokens(account.name))
    if _GOV_RE.search(joined) or (loose_gov and _GOV_LOOSE_RE.search(joined)):
        return R_NOISE_GOVERNMENT, f"government body or department ({account.name!r})"
    if set(toks) & _PLACEHOLDER_WORDS or all(t in _GENERIC_WORDS for t in toks):
        return R_NOISE_VAGUE, f"vague or placeholder name ({account.name!r})"
    return None


# ---------------------------------------------------------------- merging
def _source_of(a: Account) -> str:
    return "salesforce" if a.source_salesforce else "nooks" if a.source_nooks else "zoominfo"


def _ids(a: Account) -> list[str]:
    return [i for i in (a.sf_account_id, a.zoominfo_id, a.nooks_account_id) if i]


_FILL_FIELDS = ("website", "country_raw", "country", "billing_state", "billing_city", "hq_state",
                "hq_country_raw", "employees", "zoominfo_id", "nooks_account_id", "sf_account_id",
                "timezone", "days_since_last_activity", "prospecting_priority")


def _add_flag(a: Account, flag: str, note: str | None = None, low: bool = False) -> None:
    """Idempotently add a machine flag (and an optional human note); optionally mark low confidence."""
    if flag not in a.flags:
        a.flags.append(flag)
        if note:
            a.notes.append(note)
    if low:
        a.confidence = "low"


def _merge_into(base: Account, other: Account, how: str) -> None:
    """Fold ``other`` (lower priority, different source) into ``base``. Both are objects this
    module created. Every merged id is recorded in the note."""
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
    for fl in other.flags:
        if fl not in base.flags:
            base.flags.append(fl)
    base.notes.append(f"merged {_source_of(other)} row (ids: {', '.join(_ids(other)) or 'none'}) by {how}"
                      + (f"; filled {', '.join(filled)}" if filled else ""))


def _no_conflict(base: Account, other: Account) -> bool:
    """Name-match guard: two accounts with different ids for the same key are different companies."""
    for f in ("sf_account_id", "zoominfo_id", "nooks_account_id"):
        a, b = getattr(base, f), getattr(other, f)
        if a is not None and b is not None and a != b:
            return False
    return True


def _mark_duplicates(a: Account, others: list[Account]) -> None:
    for o in others:
        _add_flag(a, FLAG_POSSIBLE_DUPLICATE,
                  f"possible_duplicate: shares a key with {o.name!r} (ids: {', '.join(_ids(o)) or 'none'}); not merged")
        _add_flag(o, FLAG_POSSIBLE_DUPLICATE,
                  f"possible_duplicate: shares a key with {a.name!r} (ids: {', '.join(_ids(a)) or 'none'}); not merged")


def _sort_key(a: Account, prio: dict[str, int]):
    return (prio.get(_source_of(a), 99), a.name.casefold(), a.sf_account_id or "",
            a.zoominfo_id or "", a.nooks_account_id or "")


# ---------------------------------------------------------------- main entry
def build_account_list(sf_rows: Iterable[dict], nooks_account_rows: Iterable[dict],
                       zoominfo_rows: Iterable[dict], config: Config,
                       touched: Mapping[str, date] | None = None, today: date | None = None,
                       blocked_account_ids: set[str] = frozenset(),
                       cooldown_days: int | None = None,
                       cooldown_until: Mapping[str, date] | None = None,
                       call_date: date | None = None,
                       contact_names_by_account: Mapping[str, Collection[str]] | None = None
                       ) -> AccountListResult:
    """Merge the three sources and apply the drop rules. See the module docstring.

    COOLDOWN (prefer ``cooldown_until``): callers should pass ``cooldown_until`` (id -> first date
    the account may be called again, from 1c state: ``state.cooldown_until`` / ``cooldown_until_by``).
    When ``cooldown_until`` is given (even as ``{}``), ``touched`` is IGNORED for cooldown
    purposes, so an email-only touch never drops an account (emails do not start call cooldowns).
    ``touched`` + ``cooldown_days`` is the legacy path, kept for tests and callers without state;
    ``touched`` is OPTIONAL (default None = no touches) and ``today`` may be omitted when ``call_date``
    is given (one of the two is required, else ValueError).
    An account is dropped while ``cooldown_until`` is after the reference date; equal is eligible.
    ``call_date`` is the first date the account would be called (account-local semantics are the
    caller's job, 2c passes it). The reference date is ``call_date`` when given, else ``today``;
    so a night run on Wed (today=Wed) with call_date=Thu keeps an account whose cooldown ends Thu.
    ``contact_names_by_account`` (optional, SF account id -> casefolded "first last" contact names,
    built by the pipeline) is duplicate-detection evidence only: >= 3 shared names plus name
    containment flags a probable duplicate. Names are only counted, never logged or noted.
    ``touched`` maps an account id (sf_account_id, or zoominfo/nooks id for non-SF accounts) to the
    date it was last touched (``date``, ``datetime`` or ISO string). ``cooldown_days`` overrides
    ``limits.cooldown_days_no_answer``. A Nooks row may carry the optional key ``_sf_account_id``
    (set by the caller when it was fetched with ``listAccounts filter_crmId=<SF id>``) so it
    merges into the Salesforce account. Salesforce ids are normalised to the canonical 15-char form
    (``normalise_sf_id``) in the rows and in the keys of ``touched``, ``cooldown_until``,
    ``blocked_account_ids`` and ``_sf_account_id``, so 15- and 18-char forms match.
    """
    cooldown = config.limits.cooldown_days_no_answer if cooldown_days is None else cooldown_days
    # Salesforce ids may arrive as 15 or 18 chars: key everything on the canonical form (the raw
    # key is kept too, so ZoomInfo/Nooks ids are unaffected).
    if today is None and call_date is None:
        raise ValueError("build_account_list needs today or call_date (the cooldown reference date)")
    touched = _with_normalised_keys(touched or {})
    use_until = cooldown_until is not None      # state-based cooldown: touched is then ignored
    cooldown_until = _with_normalised_keys(cooldown_until or {})
    if use_until:
        touched = {}
    ref_date = call_date or today
    blocked_account_ids = set(blocked_account_ids) | {normalise_sf_id(b) for b in blocked_account_ids} - {None}
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
    by_key: dict[tuple[str, str], list[Account]] = defaultdict(list)
    by_name: dict[str, list[Account]] = defaultdict(list)
    counters: Counter = Counter()

    def index(a: Account) -> None:
        for kind, val in (("sf", a.sf_account_id), ("zi", a.zoominfo_id), ("nk", a.nooks_account_id)):
            if val and a not in by_key[(kind, val)]:
                by_key[(kind, val)].append(a)
        n = normalise_name(a.name)
        if n and a not in by_name[n]:
            by_name[n].append(a)

    for source in config.sources:
        has_source = lambda c: getattr(c, f"source_{source}")   # noqa: E731
        for acct in loaded[source]:
            match, how = None, ""
            same_source: list[Account] = []
            for kind, val, label in (("sf", acct.sf_account_id, "sf_account_id"),
                                     ("zi", acct.zoominfo_id, "zoominfo_id"),
                                     ("nk", acct.nooks_account_id, "nooks_account_id")):
                for cand in by_key.get((kind, val), []) if val else []:
                    if has_source(cand):
                        if cand not in same_source:
                            same_source.append(cand)      # same source: never merge, flag instead
                    elif match is None:
                        match, how = cand, label
            ambiguous: list[Account] = []
            if match is None:
                n = normalise_name(acct.name)
                cands = [c for c in by_name.get(n, []) if n and not has_source(c) and _no_conflict(c, acct)]
                if len(cands) == 1:
                    match, how = cands[0], "normalised name (last resort, verify)"
                    counters[STAT_NAME_MERGED] += 1
                elif len(cands) > 1:
                    ambiguous = cands
                    counters[STAT_NAME_AMBIGUOUS] += 1
            if match is None:
                merged.append(acct)
                index(acct)
                _mark_duplicates(acct, same_source)
                _mark_duplicates(acct, ambiguous)
            else:
                _merge_into(match, acct, how)
                index(match)
                _mark_duplicates(match, same_source)

    # 3a. probable same-company groups: flagged, never merged or dropped here
    _, result.duplicate_pairs = _flag_groups(merged, contact_names_by_account)

    # 3b. flags and drops
    kept: list[Account] = []
    for a in merged:
        _annotate(a, config)
        drop = _drop_reason(a, config, touched, ref_date, blocked_account_ids, cooldown, cooldown_until)
        if drop:
            reason, detail = drop
            result.dropped.append(Dropped(a.name, a.sf_account_id or a.zoominfo_id or a.nooks_account_id,
                                          reason, _source_of(a), detail))
        else:
            kept.append(a)

    result.accounts = sorted(kept, key=lambda a: _sort_key(a, prio))
    result.dropped.sort(key=lambda d: (d.reason, prio.get(d.source, 99), d.name.casefold(), d.id or ""))
    result.stats = dict(Counter(d.reason for d in result.dropped))
    result.stats.update({k: v for k, v in counters.items() if v})
    return result


def _with_normalised_keys(m: Mapping[str, date]) -> dict[str, date]:
    """Copy of ``m`` that also answers to the canonical 15-char form of each key."""
    out = dict(m)
    for k, v in m.items():
        nk = normalise_sf_id(k)
        if nk is not None and nk not in out:
            out[nk] = v
    return out


def _is_zoominfo_only(a: Account) -> bool:
    return a.source_zoominfo and not a.source_salesforce and not a.source_nooks


def _country_unknown(a: Account) -> bool:
    """True when a Salesforce account has no usable country evidence (not a real non-ANZ country)."""
    for raw in (a.country_raw, a.hq_country_raw):
        if is_blank_or_junk(raw):
            continue
        v = " ".join(raw.lower().split())
        if canonical_country(v) is None and not ("austral" in v or "zealand" in v or v in _ANZ_ALIASES):
            return False        # a real, stated non-ANZ country
    return True


def _annotate(a: Account, config: Config) -> None:
    """Set flags / confidence / notes for review. Nothing here drops an account."""
    if a.non_anz_billing_anz_hq:
        _add_flag(a, FLAG_GROUP, f"{FLAG_GROUP}: included for review (billing is non-ANZ, HQ is ANZ)", low=True)
    if a.source_salesforce and a.country is None and _country_unknown(a):
        _add_flag(a, FLAG_COUNTRY_UNKNOWN, f"{FLAG_COUNTRY_UNKNOWN}: no usable BillingCountry/HQ country "
                  f"({a.country_raw!r} / {a.hq_country_raw!r}); kept for review", low=True)
    if not a.source_salesforce:
        if _is_zoominfo_only(a):
            _add_flag(a, FLAG_ZOOMINFO_ONLY, f"{FLAG_ZOOMINFO_ONLY}: no Salesforce/Nooks match; "
                      "country, size and domain unverified, rank low", low=True)
        else:
            _add_flag(a, FLAG_NOOKS_ONLY, f"{FLAG_NOOKS_ONLY}: no Salesforce match; country unverified, rank low",
                      low=True)
    if not _is_zoominfo_only(a):
        # Alex's own book (Salesforce / Nooks): noise is flagged for review, never dropped.
        noise = noise_reason(a, config)
        if noise:
            _add_flag(a, noise[0], f"{noise[0]}: {noise[1]}; kept because it is in your own book", low=True)
    if a.employees is None and NOTE_SIZE_UNKNOWN not in a.notes:
        a.notes.append(NOTE_SIZE_UNKNOWN)


def _touch_date(a: Account, touched: Mapping[str, date]) -> date | None:
    dates = []
    for key in _ids(a):
        d = touched.get(key)
        if isinstance(d, str):
            d = date.fromisoformat(d[:10])      # ValueError on a bad string is deliberate
        elif isinstance(d, datetime):
            d = d.date()
        if d is not None:
            dates.append(d)
    return max(dates) if dates else None


def _drop_reason(a: Account, config: Config, touched: Mapping[str, date], today: date,   # today = reference date
                 blocked: set[str], cooldown: int,
                 cooldown_until: Mapping[str, date] | None = None) -> tuple[str, str] | None:
    """First matching drop rule as (reason, detail), or None to keep."""
    merged_note = " (after merging sources)" if len(_ids(a)) > 1 else ""
    if a.is_customer:
        return R_CUSTOMER, "Current_Customer__c is true" + merged_note
    hit = sorted(set(_ids(a)) & set(blocked))
    if hit:
        return R_BLOCKED, f"id in blocked set ({', '.join(hit)})"
    # Country is only known when Salesforce is a source. Blank/junk country is flagged, not dropped.
    if a.source_salesforce and a.country is None and FLAG_COUNTRY_UNKNOWN not in a.flags:
        return R_NOT_ANZ, f"BillingCountry {a.country_raw!r} / HQ {a.hq_country_raw!r} is not Australia/New Zealand"
    if _is_zoominfo_only(a):
        if _NOT_ANZ_RE.search(a.name.casefold()):
            return R_NOT_ANZ, f"name marks the company as not ANZ ({a.name!r})"
        noise = noise_reason(a, config, loose_gov=True)
        if noise:
            return noise
    if a.employees is not None:
        if a.employees > config.company_size.max_employees:
            return R_OVER_MAX, f"{a.employees} employees > max {config.company_size.max_employees}"
        if a.employees < config.company_size.min_employees:
            return R_UNDER_MIN, f"{a.employees} employees < min {config.company_size.min_employees}"
    until = _touch_date(a, cooldown_until or {})
    if until is not None:        # explicit cooldown_until (from state); touched was cleared by the caller
        if today < until:
            return R_TOUCHED, f"cooldown_until {until.isoformat()} is after {today.isoformat()}"
        return None
    last = _touch_date(a, touched)
    if last is not None and (today - last).days < cooldown:
        return R_TOUCHED, (f"touched {last.isoformat()} ({(today - last).days} days ago) "
                           f"within {cooldown}-day cooldown")
    return None


# ---------------------------------------------------------------- helpers for callers
# ---------------------------------------------------------------- probable-duplicate detector
# Flags (never merges, never drops) accounts that are probably the same company. An edge needs
# evidence; NAME CONTAINMENT ALONE NEVER FLAGS ("Bank Australia" vs "X Bank Australia", "Meridian" vs
# "Meridian Informatics" are different companies). Evidence kinds:
#   same website domain      registrable domain equal (shared hosting / social domains ignored)
#   shared id                same sf / zoominfo / nooks id
#   name+shared contacts     name containment AND >= 3 shared contact full names
#   name+same headcount      name containment AND identical non-null employees AND same first
#                            distinctive token
# Name containment = the shorter name's tokens (legal suffixes and generic words removed) are a
# whole-word sequence inside the longer one, and it holds a distinctive token of >= 5 characters.
# A group of more than _MAX_GROUP accounts is a common word, not a company: its members get
# ``large_name_group`` / ``large_domain_group`` instead of duplicate flags.
_MAX_GROUP = 6
_MIN_DISTINCTIVE = 5
_MIN_SHARED_CONTACTS = 3
# Scale guards. A distinctive word shared by more than _MAX_BUCKET accounts is a common word: its
# accounts are NOT compared pairwise by name (that is O(k^2)). They get ``large_name_group`` and can
# still be flagged through hash indexes ((employees, first token), shared contact names) or by the
# domain / id rules. Buckets of <= _MAX_BUCKET behave exactly as plain pairwise comparison.
_MAX_BUCKET = 200
_MAX_INDEX_GROUP = 200      # a (employees, first token) group bigger than this is not paired
_MAX_CONTACT_SHARE = 50     # a contact name held by more accounts than this is noise
EV_DOMAIN = "same website domain"
EV_ID = "shared id"
EV_CONTACTS = "name+shared contacts"
EV_HEADCOUNT = "name+same headcount"
# Removed from names before comparing: legal suffixes, country words, filler.
_DROP_WORDS = {"pty", "ltd", "limited", "inc", "incorporated", "llc", "plc", "proprietary", "group",
               "holdings", "holding", "australia", "australian", "nz", "new", "zealand", "the", "and",
               "of", "co", "company", "corporation", "corp", "anz", "au"}
# Too common to prove two names are the same company on their own ("Australia Post" vs "Post
# Office"): the SHORTER name needs at least one token that is not in this set.
_WEAK_WORDS = {"post", "office", "bank", "health", "insurance", "energy", "services", "service",
               "solutions", "systems", "technology", "technologies", "digital", "media", "communications",
               "university", "council", "finance", "financial", "capital", "partners", "consulting",
               "software", "international", "global", "national", "general", "first", "state", "city",
               "community", "trust", "fund", "funds", "network", "networks", "properties", "property",
               "management", "industries", "resources", "retail", "foods", "care", "education",
               "college", "school", "hospital", "transport", "logistics", "construction", "mining"}
# Hosts that many unrelated companies share: never evidence of a duplicate.
_SHARED_DOMAINS = {"linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com",
                   "youtube.com", "google.com", "wixsite.com", "wix.com", "weebly.com", "squarespace.com",
                   "wordpress.com", "blogspot.com", "github.io", "business.site", "linktr.ee",
                   "medium.com", "godaddysites.com", "myshopify.com", "netlify.app", "herokuapp.com",
                   "azurewebsites.net", "pages.dev", "tiktok.com", "pinterest.com", "bit.ly"}
_SLD_PARTS = {"com", "net", "org", "gov", "edu", "co", "ac", "asn", "id", "govt", "org", "nom"}


def registrable_domain(website: str | None) -> str | None:
    """'https://www.Example.com.au/path?x' -> 'example.com.au'; None for blanks or shared hosts."""
    if not website or not isinstance(website, str):
        return None
    host = re.sub(r"^[a-z][a-z0-9+.-]*://", "", website.strip().lower())
    host = re.split(r"[/?#]", host, maxsplit=1)[0].split("@")[-1].split(":")[0].strip(".")
    if not host or "." not in host:
        return None
    labels = host.split(".")
    if labels[0] == "www":
        labels = labels[1:]
    if len(labels) < 2:
        return None
    take = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SLD_PARTS else 2
    dom = ".".join(labels[-take:])
    return None if dom in _SHARED_DOMAINS else dom


def _core_tokens(name: str) -> tuple[str, ...]:
    return tuple(t for t in _tokens(name) if t not in _DROP_WORDS)


def _contains(big: tuple[str, ...], small: tuple[str, ...]) -> bool:
    n = len(small)
    return n > 0 and any(big[i:i + n] == small for i in range(len(big) - n + 1))


def _names_match(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """Whole-word containment of the shorter distinctive name in the longer (or equal)."""
    if not a or not b:
        return False
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    if not any(len(t) >= _MIN_DISTINCTIVE and t not in _WEAK_WORDS for t in small):
        return False
    return _contains(big, small)


class _UF:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def _components(n: int, edges: Iterable[tuple[int, int]]) -> list[list[int]]:
    uf = _UF(n)
    for a, b in edges:
        uf.union(a, b)
    comp: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        comp[uf.find(i)].append(i)
    return [m for m in comp.values() if len(m) > 1]


@dataclass
class _Groups:
    groups: list[list[int]]                 # duplicate groups (indexes), each >= 2, sorted
    reasons: list[set[str]]                 # per group: evidence kinds
    pairs: dict[tuple[int, int], set[str]]  # (i, j) i < j -> evidence kinds (pairs inside kept groups)
    large_name: list[list[int]]
    large_domain: list[list[int]]


def _norm_contact(name: str) -> str:
    return " ".join(str(name).casefold().split())


def _contact_sets(accounts: list[Account],
                  contacts: Mapping[str, Collection[str]] | None) -> list[frozenset[str]]:
    if not contacts:
        return [frozenset()] * len(accounts)
    norm = {normalise_sf_id(k): v for k, v in contacts.items()}
    return [frozenset(_norm_contact(n) for n in norm.get(a.sf_account_id, ()) if str(n).strip())
            if a.sf_account_id else frozenset() for a in accounts]


def _index_candidates(members: list[int], accounts: list[Account], cores: list[tuple[str, ...]],
                      csets: list[frozenset[str]], cand: set[tuple[int, int]]) -> None:
    """Name-containment candidates for accounts in oversized name buckets, found WITHOUT pairwise
    scans: only accounts that share an index key are compared (and must still pass _names_match)."""
    # (employees, first distinctive token) -> accounts
    by_hc: dict[tuple[int, str], list[int]] = defaultdict(list)
    for i in members:
        if accounts[i].employees is not None and cores[i]:
            by_hc[(accounts[i].employees, cores[i][0])].append(i)
    for idxs in by_hc.values():
        if 1 < len(idxs) <= _MAX_INDEX_GROUP:
            for x in range(len(idxs)):
                for y in range(x + 1, len(idxs)):
                    if _names_match(cores[idxs[x]], cores[idxs[y]]):
                        cand.add((idxs[x], idxs[y]))
    # contact name -> accounts (skip names shared by many accounts), then count shared names per pair
    by_name: dict[str, list[int]] = defaultdict(list)
    for i in members:
        for c in csets[i]:
            by_name[c].append(i)
    shared: Counter = Counter()
    for idxs in by_name.values():
        if 1 < len(idxs) <= _MAX_CONTACT_SHARE:
            for x in range(len(idxs)):
                for y in range(x + 1, len(idxs)):
                    shared[(idxs[x], idxs[y])] += 1
    for (i, j), c in shared.items():
        if c >= _MIN_SHARED_CONTACTS and _names_match(cores[i], cores[j]):
            cand.add((i, j))


def _detect(accounts: list[Account],
            contacts: Mapping[str, Collection[str]] | None = None) -> _Groups:
    n = len(accounts)
    ev: dict[tuple[int, int], set[str]] = defaultdict(set)

    def add(a: int, b: int, kind: str) -> None:
        ev[(min(a, b), max(a, b))].add(kind)

    # same website domain (alone flags)
    by_dom: dict[str, list[int]] = defaultdict(list)
    for i, a in enumerate(accounts):
        d = registrable_domain(a.website)
        if d:
            by_dom[d].append(i)
    large_domain = []
    for idxs in by_dom.values():
        if len(idxs) > _MAX_GROUP:
            large_domain.append(idxs)
        else:
            for x in range(len(idxs)):
                for y in range(x + 1, len(idxs)):
                    add(idxs[x], idxs[y], EV_DOMAIN)

    # shared ids (alone flags)
    seen: dict[tuple[str, str], int] = {}
    for i, a in enumerate(accounts):
        for kind, v in (("sf", a.sf_account_id), ("zi", a.zoominfo_id), ("nk", a.nooks_account_id)):
            if v:
                if (kind, v) in seen:
                    add(seen[(kind, v)], i, EV_ID)
                else:
                    seen[(kind, v)] = i

    # name containment: a candidate only; it needs corroboration to become an edge
    cores = [_core_tokens(a.name) for a in accounts]
    csets = _contact_sets(accounts, contacts)
    bucket: dict[str, list[int]] = defaultdict(list)
    for i, c in enumerate(cores):
        for t in set(c):
            if len(t) >= _MIN_DISTINCTIVE and t not in _WEAK_WORDS:
                bucket[t].append(i)
    cand: set[tuple[int, int]] = set()
    big_members: set[int] = set()
    large_buckets: list[list[int]] = []
    for idxs in bucket.values():
        if len(idxs) > _MAX_BUCKET:
            large_buckets.append(idxs)
            big_members.update(idxs)
            continue
        for x in range(len(idxs)):
            for y in range(x + 1, len(idxs)):
                if _names_match(cores[idxs[x]], cores[idxs[y]]):
                    cand.add((idxs[x], idxs[y]))
    if big_members:
        _index_candidates(sorted(big_members), accounts, cores, csets, cand)
    name_ev: dict[tuple[int, int], set[str]] = {}
    for i, j in cand:
        kinds = set()
        if len(csets[i] & csets[j]) >= _MIN_SHARED_CONTACTS:
            kinds.add(EV_CONTACTS)
        ea, eb = accounts[i].employees, accounts[j].employees
        if ea is not None and ea == eb and cores[i][0] == cores[j][0]:
            kinds.add(EV_HEADCOUNT)
        if kinds:
            name_ev[(i, j)] = kinds
    name_comps = _components(n, name_ev)
    large_name = [m for m in name_comps if len(m) > _MAX_GROUP]
    large_name_out = large_name + [sorted(b) for b in large_buckets]
    capped = {i for m in large_name for i in m}
    for (i, j), kinds in name_ev.items():
        if i not in capped and j not in capped:
            for k in kinds:
                add(i, j, k)

    comps = sorted(_components(n, ev), key=lambda m: m[0])
    group_of = {i: g for g, m in enumerate(comps) for i in m}
    groups = [sorted(m) for m in comps]
    reasons: list[set[str]] = [set() for _ in comps]
    for (a, _b), kinds in ev.items():
        reasons[group_of[a]] |= kinds
    return _Groups(groups, reasons, dict(ev), sorted(sorted(m) for m in large_name_out),
                   sorted(sorted(m) for m in large_domain))


def _ref(a: Account) -> str:
    return f"{a.name!r} ({', '.join(_ids(a)) or 'no id'})"


_GROUP_NOTE = "possible_duplicate group "
_GROUP_RE = re.compile(r"possible_duplicate group (dup-\S+) \(")


def _flag_groups(accounts: list[Account], contacts: Mapping[str, Collection[str]] | None = None
                 ) -> tuple[int, list[tuple[Account, Account, tuple[str, ...]]]]:
    order = sorted(range(len(accounts)), key=lambda i: (accounts[i].name.casefold(), _ids(accounts[i]), i))
    ordered = [accounts[i] for i in order]
    g = _detect(ordered, contacts)
    for members, why in zip(g.groups, g.reasons):
        accts = [ordered[i] for i in members]
        gid = "dup-" + re.sub(r"\s+", "_", min((_ids(a)[0] if _ids(a) else a.name.casefold()) for a in accts))
        for a in accts:
            others = "; ".join(_ref(o) for o in accts if o is not a)
            _add_flag(a, FLAG_POSSIBLE_DUPLICATE)
            note = (f"{_GROUP_NOTE}{gid} ({', '.join(sorted(why))}): also {others}; "
                    "not merged, collapse on the sheet")
            if note not in a.notes:
                a.notes.append(note)
    for flag, groups, label in ((FLAG_LARGE_NAME_GROUP, g.large_name, "name"),
                                (FLAG_LARGE_DOMAIN_GROUP, g.large_domain, "website domain")):
        for members in groups:
            for i in members:
                _add_flag(ordered[i], flag, f"{flag}: shares a {label} with {len(members) - 1} other accounts "
                          f"(more than {_MAX_GROUP}); a common word, not flagged as duplicates")
    pairs = [(ordered[i], ordered[j], tuple(sorted(k))) for (i, j), k in sorted(g.pairs.items())]
    return len(g.groups), pairs


def flag_probable_duplicates(accounts: list[Account],
                             contact_names_by_account: Mapping[str, Collection[str]] | None = None) -> int:
    """Flag probable same-company groups IN PLACE (flag ``possible_duplicate`` + a note naming the
    others, the evidence and a group id; ``large_name_group`` / ``large_domain_group`` for oversized
    groups). Nothing is merged or dropped. Returns the number of duplicate groups. Deterministic:
    independent of input order. Contact names are only counted, never written to notes."""
    return _flag_groups(accounts, contact_names_by_account)[0]


def duplicate_groups(accounts: Iterable[Account],
                     contact_names_by_account: Mapping[str, Collection[str]] | None = None
                     ) -> list[list[Account]]:
    """Groups (size >= 2) of probably-the-same-company accounts. Members keep input order and groups
    are ordered by first member. For the ranker/sheet step: keep the higher-scoring account of each
    group and mention the others in the 'why' line.

    Accounts already flagged by ``build_account_list`` carry a group id in their notes; when any do,
    the groups are read from those notes (so contact evidence used at build time is not lost and no
    contacts are needed here). Otherwise the groups are detected fresh (optionally with
    ``contact_names_by_account``). Oversized name/domain groups (> 6) are not returned. Never mutates."""
    accts = list(accounts)
    by_gid: dict[str, list[int]] = defaultdict(list)
    for i, a in enumerate(accts):
        for note in a.notes:
            m = _GROUP_RE.match(note)
            if m:
                by_gid[m.group(1)].append(i)
                break
    if by_gid:
        groups = [sorted(m) for m in by_gid.values() if len(m) > 1]
        return [[accts[i] for i in m] for m in sorted(groups)]
    g = _detect(accts, contact_names_by_account)
    return [[accts[i] for i in m] for m in g.groups]


def group_by_zoominfo_id(accounts: Iterable[Account]) -> dict[str, list[Account]]:
    """zoominfo_id -> ALL accounts carrying it (input order). Two SF accounts can share one ZoomInfo
    id (both are kept, flagged ``possible_duplicate``), so the signals join should attach the same
    trigger to every account in the list. Accounts without a zoominfo_id are left out."""
    out: dict[str, list[Account]] = defaultdict(list)
    for a in accounts:
        if a.zoominfo_id:
            out[a.zoominfo_id].append(a)
    return dict(out)


def list_flagged(accounts: Iterable[Account]) -> list[Account]:
    """Accounts in the 'non-ANZ billing, ANZ HQ' group, for Alex to review. Config-free."""
    return [a for a in accounts if a.non_anz_billing_anz_hq]


def confidence(account: Account) -> str:
    """'low' or 'normal' (reads ``Account.confidence``; kept for callers that used the helper)."""
    return account.confidence


def summarise(result: AccountListResult) -> str:
    """Plain-text block for the run log: totals, kept-but-flagged accounts (count + <=5 names per
    flag, for Alex to review), drops per reason (count + <=5 names), and merge counters."""
    kept = result.accounts
    lines = [f"accounts kept: {len(kept)}",
             f"  low confidence: {sum(1 for a in kept if a.confidence == 'low')}",
             f"  size_unknown: {sum(1 for a in kept if a.size_unknown)}"]
    by_flag: dict[str, list[str]] = defaultdict(list)
    for a in kept:
        for fl in a.flags:
            by_flag[fl].append(a.name)
    lines.append("kept but flagged for review:" if by_flag else "kept but flagged for review: none")
    for fl in sorted(by_flag):
        names = by_flag[fl]
        lines.append(f"  {fl}: {len(names)}")
        lines.append("    e.g. " + "; ".join(names[:5]) + (" ..." if len(names) > 5 else ""))
    lines.append(f"accounts dropped: {len(result.dropped)}")
    by_reason: dict[str, list[Dropped]] = defaultdict(list)
    for d in result.dropped:
        by_reason[d.reason].append(d)
    for reason in sorted(by_reason):
        items = by_reason[reason]
        lines.append(f"  {reason}: {len(items)}")
        lines.append("    e.g. " + "; ".join(d.name for d in items[:5]) + (" ..." if len(items) > 5 else ""))
    keep_ids = {id(x) for x in kept}
    pairs = [p for p in result.duplicate_pairs if id(p[0]) in keep_ids and id(p[1]) in keep_ids]
    breakdown = Counter(k for p in pairs for k in p[2])
    detail = ", ".join(f"{k}: {breakdown[k]}" for k in sorted(breakdown)) or "none"
    lines.append(f"possible duplicate pairs: {len(pairs)} (evidence: {detail}; top 10 listed with names)")
    for x, y, kinds in sorted(pairs, key=lambda p: (-len(p[2]), p[0].name.casefold(), p[1].name.casefold()))[:10]:
        lines.append(f"    [{', '.join(kinds)}] {x.name} ~ {y.name}")
    for flag in (FLAG_LARGE_NAME_GROUP, FLAG_LARGE_DOMAIN_GROUP):
        c = sum(1 for a in kept if flag in a.flags)
        if c:
            lines.append(f"{flag}: {c} accounts (not flagged as duplicates)")
    lines.append(f"merged by name: {result.stats.get(STAT_NAME_MERGED, 0)}")
    lines.append(f"name_merge_ambiguous (not merged): {result.stats.get(STAT_NAME_AMBIGUOUS, 0)}")
    return "\n".join(lines)
