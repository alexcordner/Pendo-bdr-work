"""Typed loader for config/config.yaml, plus timezone lookup helpers.

``load_config`` validates required keys and types and raises ``ConfigError`` with a
message naming the offending key path (e.g. ``limits.daily_call_cap``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

COUNTRY_AU = "Australia"
COUNTRY_NZ = "New Zealand"


class ConfigError(ValueError):
    """Raised for a missing/invalid config value."""


# ---------------------------------------------------------------- dataclasses
@dataclass(frozen=True)
class Owner:
    nooks_user_id: str
    email: str


@dataclass(frozen=True)
class CountryTimezones:
    default: str                                   # IANA name
    states: dict[str, str] = field(default_factory=dict)  # canonical full state name -> IANA name


@dataclass(frozen=True)
class Territory:
    priority_countries: tuple[str, ...]
    secondary_countries: tuple[str, ...]
    timezones: dict[str, CountryTimezones]         # keyed by canonical country name


@dataclass(frozen=True)
class CompanySize:
    max_employees: int
    min_employees: int


@dataclass(frozen=True)
class Tier:
    min_employees: int
    max_employees: int | None = None               # None = open-ended (not expressible in YAML today)
    note: str = ""


@dataclass(frozen=True)
class CallWindow:
    name: str
    start: time
    end: time


@dataclass(frozen=True)
class Limits:
    daily_call_cap: int
    max_per_account_per_run: int
    cooldown_days_no_answer: int
    cooldown_days_after_connect: int
    email_cooldown_days: int
    voicemail_lookback_hours: int
    max_voicemail_drafts_per_run: int
    zoominfo_credit_budget_per_run: int            # 0 = search only
    no_contact_max_duration_s: int                 # a 'No Answer' shorter than this counts as no contact


@dataclass(frozen=True)
class Dispositions:
    """Nooks disposition NAMES (never ids). Must match listCallDispositions names exactly
    (comparison is case/space-insensitive in loaders.disposition_id_for_name)."""

    voicemail: str
    no_answer: str
    do_not_call: str
    gatekeeper: str
    wrong_number: str


@dataclass(frozen=True)
class Personas:
    primary: tuple[str, ...]
    secondary: tuple[str, ...]


@dataclass(frozen=True)
class SignalWeights:
    trigger_strength: float
    icp_fit: float
    recency: float
    named_prospect_with_phone: float
    cooldown_penalty: float                        # <= 0
    sdr_target_boost: float                        # points added when Account.is_sdr_target (a boost, NOT a filter)


@dataclass(frozen=True)
class SignalsConfig:
    scoop_types_keep: tuple[str, ...]
    keywords_keep: tuple[str, ...]
    keywords_drop: tuple[str, ...]
    max_age_days: int
    weights: SignalWeights


@dataclass(frozen=True)
class Filters:
    drop_name_patterns: tuple[str, ...]
    drop_if_no_domain: bool


@dataclass(frozen=True)
class Paths:
    state_dir: str
    log_dir: str
    output_dir: str


@dataclass(frozen=True)
class Config:
    owner: Owner
    territory: Territory
    company_size: CompanySize
    tiers: dict[str, Tier]                         # "tier1" / "tier2" / "tier3"
    call_windows: tuple[CallWindow, ...]
    working_days: tuple[int, ...]                  # datetime.weekday() values, Mon=0
    limits: Limits
    dispositions: Dispositions
    sources: tuple[str, ...]
    personas: Personas
    signals: SignalsConfig
    filters: Filters
    paths: Paths


# ---------------------------------------------------------------- validation helpers
_AU_STATES = ("New South Wales", "Victoria", "Queensland", "Western Australia", "South Australia",
              "Tasmania", "Northern Territory", "Australian Capital Territory")
_SOURCES = ("salesforce", "nooks", "zoominfo")
_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _check_keys(d: dict, allowed, path: str) -> None:
    """Reject unknown keys so a typo in an optional key cannot vanish silently."""
    for k in d:
        if k not in allowed:
            raise ConfigError(f"unknown key '{path}.{k}' (allowed: {', '.join(sorted(map(str, allowed)))})")


def _section(d: dict, key: str, allowed=None, path: str = "") -> dict:
    full = f"{path}{key}"
    if not isinstance(d, dict) or key not in d:
        raise ConfigError(f"missing required section '{full}'")
    if not isinstance(d[key], dict):
        raise ConfigError(f"'{full}' must be a mapping")
    if allowed is not None:
        _check_keys(d[key], allowed, full)
    return d[key]


def _get(d: dict, key: str, typ, path: str):
    """Fetch a required key of the given type (bool is not accepted as a number)."""
    full = f"{path}.{key}"
    if key not in d or d[key] is None:
        raise ConfigError(f"missing required key '{full}'")
    v = d[key]
    ok = isinstance(v, typ) and not (typ is not bool and isinstance(v, bool))
    if not ok:
        name = typ.__name__ if isinstance(typ, type) else "number"
        raise ConfigError(f"'{full}' must be {name}, got {type(v).__name__}: {v!r}")
    return v


def _int(d: dict, key: str, path: str, minimum: int = 1) -> int:
    v = _get(d, key, int, path)
    if v < minimum:
        raise ConfigError(f"'{path}.{key}' must be >= {minimum}, got {v}")
    return v


def _text(d: dict, key: str, path: str) -> str:
    v = _get(d, key, str, path)
    if not v.strip():
        raise ConfigError(f"'{path}.{key}' must not be empty")
    return v


def _str_list(d: dict, key: str, path: str, allow_empty: bool = True) -> tuple[str, ...]:
    full = f"{path}.{key}" if path else key
    if key not in d or d[key] is None:
        raise ConfigError(f"missing required key '{full}'")
    v = d[key]
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ConfigError(f"'{full}' must be a list of strings")
    if not allow_empty and not v:
        raise ConfigError(f"'{full}' must not be empty")
    return tuple(v)


def _tz(name, path: str) -> str:
    if not isinstance(name, str):
        raise ConfigError(f"'{path}' must be an IANA timezone string, got {name!r}")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ConfigError(f"'{path}' is not a valid IANA timezone: {name!r}") from e
    return name


def _hhmm(v, path: str) -> time:
    try:
        h, m = str(v).split(":")
        return time(int(h), int(m))
    except Exception as e:
        raise ConfigError(f"'{path}' must be 'HH:MM', got {v!r}") from e


# ---------------------------------------------------------------- loader
_JUNK = {"", "other", "n/a", "na", "null", "none", "outside u.s./canada", "-", "unknown"}


def is_blank_or_junk(raw) -> bool:
    """True for None, non-strings, blanks and placeholder strings like 'n/a' / 'Other'."""
    return not isinstance(raw, str) or " ".join(raw.lower().split()) in _JUNK


def canonical_country(raw: str | None) -> str | None:
    """'AU'/'australia'/'AUSTRALIA' -> 'Australia'; 'NZ'/'new  zealand' -> 'New Zealand'; else None."""
    if not raw or not isinstance(raw, str):
        return None
    v = " ".join(raw.strip().lower().split())
    if v in ("au", "aus", "australia"):
        return COUNTRY_AU
    if v in ("nz", "nzl", "new zealand"):
        return COUNTRY_NZ
    return None


def load_config(path: str | Path = "config/config.yaml") -> Config:
    """Load and validate the YAML config. Raises ConfigError (or FileNotFoundError)."""
    with open(path, encoding="utf-8") as f:
        try:
            raw = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ConfigError(f"{path} is not valid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")
    return parse_config(raw)


def parse_config(raw: dict) -> Config:
    """Validate an already-parsed dict (tests use this to build invalid variants).

    Rejects unknown keys, non-positive limits, a positive cooldown_penalty, tier min > max or
    overlap, misspelled state keys and unknown sources.
    """
    _check_keys(raw, ("owner", "territory", "company_size", "tiers", "call_windows", "working_days",
                      "limits", "dispositions", "sources", "personas", "signals", "filters", "paths"), "")

    o = _section(raw, "owner", ("nooks_user_id", "email"))
    owner = Owner(_text(o, "nooks_user_id", "owner"), _text(o, "email", "owner"))
    if "@" not in owner.email:
        raise ConfigError(f"'owner.email' is not an email address: {owner.email!r}")

    t = _section(raw, "territory", ("priority_countries", "secondary_countries", "timezones"))
    tzs: dict[str, CountryTimezones] = {}
    for country, spec in _section(t, "timezones", None, "territory.").items():
        p = f"territory.timezones.{country}"
        if canonical_country(country) != country:
            raise ConfigError(f"'{p}': country must be 'Australia' or 'New Zealand'")
        if not isinstance(spec, dict):
            raise ConfigError(f"'{p}' must be a mapping with a 'default' key")
        default = _tz(spec.get("default"), f"{p}.default")
        states = {}
        for k, v in spec.items():
            if k == "default":
                continue
            if country == COUNTRY_NZ or k not in _AU_STATES:
                raise ConfigError(f"unknown state key '{p}.{k}' "
                                  + ("(New Zealand takes only 'default')" if country == COUNTRY_NZ
                                     else f"(use full names: {', '.join(_AU_STATES)})"))
            states[str(k)] = _tz(v, f"{p}.{k}")
        tzs[country] = CountryTimezones(default, states)
    for c in (COUNTRY_AU, COUNTRY_NZ):
        if c not in tzs:
            raise ConfigError(f"missing required section 'territory.timezones.{c}'")
    countries = []
    for key, empty in (("priority_countries", False), ("secondary_countries", True)):
        vals = _str_list(t, key, "territory", allow_empty=empty)
        for v in vals:
            if canonical_country(v) is None:
                raise ConfigError(f"'territory.{key}' has unsupported country {v!r} (ANZ only)")
        countries.append(vals)
    territory = Territory(countries[0], countries[1], tzs)

    cs = _section(raw, "company_size", ("max_employees", "min_employees"))
    size = CompanySize(_int(cs, "max_employees", "company_size"), _int(cs, "min_employees", "company_size", 0))
    if size.min_employees > size.max_employees:
        raise ConfigError("company_size.min_employees must be <= max_employees")

    tiers: dict[str, Tier] = {}
    for name, spec in _section(raw, "tiers").items():
        p = f"tiers.{name}"
        if not isinstance(spec, dict):
            raise ConfigError(f"'{p}' must be a mapping")
        _check_keys(spec, ("employees", "note"), p)
        emp = spec.get("employees")
        if (not isinstance(emp, list) or len(emp) != 2
                or not all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in emp)):
            raise ConfigError(f"'{p}.employees' must be [min, max] non-negative integers")
        if emp[0] > emp[1]:
            raise ConfigError(f"'{p}.employees' min ({emp[0]}) must be <= max ({emp[1]})")
        tiers[name] = Tier(emp[0], emp[1], str(spec.get("note", "")))
    for req in ("tier1", "tier2", "tier3"):
        if req not in tiers:
            raise ConfigError(f"missing required section 'tiers.{req}'")
    ordered = sorted(tiers.items(), key=lambda kv: kv[1].min_employees)
    for (n1, a), (n2, b) in zip(ordered, ordered[1:]):
        if b.min_employees <= a.max_employees:
            raise ConfigError(f"tiers overlap: '{n1}' and '{n2}'")

    wins_raw = raw.get("call_windows")
    if not isinstance(wins_raw, list) or not wins_raw:
        raise ConfigError("'call_windows' must be a non-empty list")
    windows = []
    for i, w in enumerate(wins_raw):
        if not isinstance(w, dict) or "name" not in w:
            raise ConfigError(f"'call_windows[{i}]' must have name, start, end")
        _check_keys(w, ("name", "start", "end"), f"call_windows[{i}]")
        start, end = _hhmm(w.get("start"), f"call_windows[{i}].start"), _hhmm(w.get("end"), f"call_windows[{i}].end")
        if start >= end:
            raise ConfigError(f"'call_windows[{i}]' start must be before end")
        windows.append(CallWindow(str(w["name"]), start, end))
    windows.sort(key=lambda w: w.start)
    for a, b in zip(windows, windows[1:]):
        if b.start < a.end:
            raise ConfigError(f"call windows '{a.name}' and '{b.name}' overlap")

    days_raw = raw.get("working_days")
    if not isinstance(days_raw, list) or not days_raw or not all(isinstance(x, str) for x in days_raw):
        raise ConfigError("'working_days' must be a non-empty list like [Mon, Tue, Wed, Thu, Fri]")
    days = []
    for x in days_raw:
        if x.strip().lower()[:3] not in _DAYS:
            raise ConfigError(f"'working_days' has unknown day {x!r}")
        days.append(_DAYS[x.strip().lower()[:3]])
    working_days = tuple(sorted(set(days)))

    lm = _section(raw, "limits", tuple(Limits.__dataclass_fields__))
    limits = Limits(**{k: _int(lm, k, "limits", 0 if k == "zoominfo_credit_budget_per_run" else 1)
                       for k in Limits.__dataclass_fields__})

    dp = _section(raw, "dispositions", tuple(Dispositions.__dataclass_fields__))
    dispositions = Dispositions(**{k: _text(dp, k, "dispositions") for k in Dispositions.__dataclass_fields__})

    sources = _str_list(raw, "sources", "", allow_empty=False)
    for s_ in sources:
        if s_ not in _SOURCES:
            raise ConfigError(f"'sources' has unknown source {s_!r} (allowed: {', '.join(_SOURCES)})")

    pr = _section(raw, "personas", ("primary", "secondary"))
    personas = Personas(_str_list(pr, "primary", "personas", allow_empty=False),
                        _str_list(pr, "secondary", "personas"))

    sg = _section(raw, "signals", ("scoop_types_keep", "keywords_keep", "keywords_drop", "max_age_days", "weights"))
    wt = _section(sg, "weights", tuple(SignalWeights.__dataclass_fields__), "signals.")
    wvals = {k: float(_get(wt, k, (int, float), "signals.weights")) for k in SignalWeights.__dataclass_fields__}
    for k, v in wvals.items():
        if k == "cooldown_penalty" and v > 0:
            raise ConfigError(f"'signals.weights.cooldown_penalty' must be <= 0 (a penalty), got {v}")
        if k != "cooldown_penalty" and v < 0:
            raise ConfigError(f"'signals.weights.{k}' must be >= 0, got {v}")
    signals = SignalsConfig(
        _str_list(sg, "scoop_types_keep", "signals"),
        _str_list(sg, "keywords_keep", "signals"),
        _str_list(sg, "keywords_drop", "signals"),
        _int(sg, "max_age_days", "signals"),
        SignalWeights(**wvals),
    )

    fl = _section(raw, "filters", ("drop_name_patterns", "drop_if_no_domain"))
    filters = Filters(_str_list(fl, "drop_name_patterns", "filters"),
                      _get(fl, "drop_if_no_domain", bool, "filters"))

    pa = _section(raw, "paths", ("state_dir", "log_dir", "output_dir"))
    paths = Paths(*(_text(pa, k, "paths") for k in ("state_dir", "log_dir", "output_dir")))

    return Config(owner, territory, size, tiers, tuple(windows), working_days, limits, dispositions,
                  sources, personas, signals, filters, paths)


# ---------------------------------------------------------------- tiers
def tier_for(config: Config, employees: int | None) -> str | None:
    """'tier1' / 'tier2' / 'tier3' for a headcount, or None if unknown or outside every tier."""
    if employees is None:
        return None
    for name, t in config.tiers.items():
        if t.min_employees <= employees <= (t.max_employees if t.max_employees is not None else employees):
            return name
    return None


# ---------------------------------------------------------------- call windows
@dataclass(frozen=True)
class WindowBounds:
    """One call window on one date, as aware datetimes in the prospect's timezone."""

    name: str
    start: datetime
    end: datetime


def window_bounds(config: Config, tz_name: str, on_date: date) -> list[WindowBounds]:
    """Config call windows for ``on_date`` in ``tz_name`` (local date there), in start order.

    Empty list if ``on_date`` is not a working day (config.working_days). Public holidays are
    NOT modelled. DST is handled by zoneinfo.
    """
    if on_date.weekday() not in config.working_days:
        return []
    zi = ZoneInfo(tz_name)
    return [WindowBounds(w.name, datetime.combine(on_date, w.start, tzinfo=zi),
                         datetime.combine(on_date, w.end, tzinfo=zi)) for w in config.call_windows]


def next_working_windows(config: Config, tz_name: str, now: datetime) -> list[WindowBounds]:
    """Windows of the first working day (local to ``tz_name``) that still has a window ending
    after ``now`` (an aware datetime). Today counts if a window is still ahead. [] if none in 14 days."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local_day = now.astimezone(ZoneInfo(tz_name)).date()
    for i in range(15):
        open_ = [w for w in window_bounds(config, tz_name, local_day + timedelta(days=i)) if w.end > now]
        if open_:
            return open_
    return []


# ---------------------------------------------------------------- timezone lookup
# Abbreviation / alias -> canonical full Australian state name. Keys are lower-case.
_AU_STATE_ALIASES = {
    "nsw": "New South Wales", "new south wales": "New South Wales",
    "vic": "Victoria", "victoria": "Victoria",
    "qld": "Queensland", "queensland": "Queensland",
    "wa": "Western Australia", "western australia": "Western Australia", "west australia": "Western Australia",
    "sa": "South Australia", "south australia": "South Australia",
    "act": "Australian Capital Territory", "australian capital territory": "Australian Capital Territory",
    "tas": "Tasmania", "tasmania": "Tasmania",
    "nt": "Northern Territory", "northern territory": "Northern Territory",
}
# Major cities seen in BillingCity / state-field slips -> state (small, deliberately incomplete).
_AU_CITY_TO_STATE = {
    "sydney": "New South Wales", "newcastle": "New South Wales", "wollongong": "New South Wales",
    "melbourne": "Victoria", "geelong": "Victoria",
    "brisbane": "Queensland", "gold coast": "Queensland", "cairns": "Queensland", "townsville": "Queensland",
    "perth": "Western Australia", "adelaide": "South Australia", "hobart": "Tasmania",
    "darwin": "Northern Territory", "canberra": "Australian Capital Territory",
}


@dataclass(frozen=True)
class TimezoneResult:
    """Result of a timezone lookup. ``tz`` None means unresolvable (see ``reason``)."""

    tz: str | None
    source: str          # 'state' | 'hq_state' | 'city' | 'country_default' | 'nz' | 'none'
    reason: str          # always filled; human-readable, goes in Account.notes / logs
    guessed: bool = False  # True for the country-default fallback (call later in the window)


def normalise_au_state(raw: str | None) -> tuple[str | None, str]:
    """Map a raw state string to a canonical Australian state name.

    Handles abbreviations (NSW/VIC/QLD/WA/ACT/SA/TAS/NT), mixed case, extra spaces, and a few
    city names / misspellings. Returns ``(canonical_or_None, reason)``; junk gives None + reason.
    """
    if raw is None or not isinstance(raw, str):
        return None, "state is null"
    v = " ".join(raw.strip().lower().split())
    if v in _JUNK:
        return None, f"state {raw!r} is blank/junk"
    if v in _AU_STATE_ALIASES:
        return _AU_STATE_ALIASES[v], f"state {raw!r} recognised"
    if v in _AU_CITY_TO_STATE:
        return _AU_CITY_TO_STATE[v], f"state field {raw!r} is a city; mapped to its state"
    return None, f"state {raw!r} not recognised as an Australian state"


def resolve_timezone(
    config: Config,
    country: str | None,
    billing_state: str | None = None,
    billing_city: str | None = None,
    hq_state: str | None = None,
) -> TimezoneResult:
    """Fallback chain: BillingState -> HQ state -> BillingCity -> country default.

    ``country`` may be raw or canonical (AU/NZ in any case). New Zealand has a single zone.
    Non-ANZ or unknown country returns ``tz=None``. Never uses the Salesforce user's timezone.
    """
    c = canonical_country(country)
    if c is None:
        return TimezoneResult(None, "none", f"country {country!r} is not Australia/New Zealand")
    ctz = config.territory.timezones[c]
    if c == COUNTRY_NZ:
        return TimezoneResult(ctz.default, "nz", "New Zealand has one timezone")

    reasons: list[str] = []
    for source, raw in (("state", billing_state), ("hq_state", hq_state), ("city", billing_city)):
        state, why = normalise_au_state(raw)
        reasons.append(f"{source}: {why}")
        if state is not None:
            # States not listed in config (NSW, ACT) use the country default, which is Sydney time.
            return TimezoneResult(ctz.states.get(state, ctz.default), source, "; ".join(reasons))
    reasons.append("falling back to country default (guess)")
    return TimezoneResult(ctz.default, "country_default", "; ".join(reasons), guessed=True)
