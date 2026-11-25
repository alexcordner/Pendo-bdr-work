"""Typed loader for config/config.yaml, plus timezone lookup helpers.

``load_config`` validates required keys and types and raises ``ConfigError`` with a
message naming the offending key path (e.g. ``limits.daily_call_cap``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
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
    max_employees: int | None = None               # None = open-ended
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
    zoominfo_credit_budget_per_run: int


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
    cooldown_penalty: float                        # negative


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
    limits: Limits
    sources: tuple[str, ...]
    personas: Personas
    signals: SignalsConfig
    filters: Filters
    paths: Paths


# ---------------------------------------------------------------- validation helpers
def _section(d: dict, key: str, path: str = "") -> dict:
    full = f"{path}{key}"
    if not isinstance(d, dict) or key not in d:
        raise ConfigError(f"missing required section '{full}'")
    if not isinstance(d[key], dict):
        raise ConfigError(f"'{full}' must be a mapping")
    return d[key]


def _get(d: dict, key: str, typ, path: str):
    """Fetch a required key of the given type (bool is not accepted as int)."""
    full = f"{path}.{key}"
    if key not in d or d[key] is None:
        raise ConfigError(f"missing required key '{full}'")
    v = d[key]
    ok = isinstance(v, typ) and not (typ in (int, (int, float)) and isinstance(v, bool))
    if not ok:
        name = typ.__name__ if isinstance(typ, type) else "number"
        raise ConfigError(f"'{full}' must be {name}, got {type(v).__name__}: {v!r}")
    return v


def _str_list(d: dict, key: str, path: str, allow_empty: bool = True) -> tuple[str, ...]:
    full = f"{path}.{key}"
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
def canonical_country(raw: str | None) -> str | None:
    """'AU'/'australia'/'AUSTRALIA' -> 'Australia'; 'NZ'/'new zealand' -> 'New Zealand'; else None."""
    if not raw or not isinstance(raw, str):
        return None
    v = raw.strip().lower()
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
    """Validate an already-parsed dict (used by tests to build invalid variants)."""
    o = _section(raw, "owner")
    owner = Owner(_get(o, "nooks_user_id", str, "owner"), _get(o, "email", str, "owner"))

    t = _section(raw, "territory")
    tzs: dict[str, CountryTimezones] = {}
    for country, spec in _section(t, "timezones", "territory.").items():
        p = f"territory.timezones.{country}"
        if canonical_country(country) != country:
            raise ConfigError(f"'{p}': country must be 'Australia' or 'New Zealand'")
        if not isinstance(spec, dict):
            raise ConfigError(f"'{p}' must be a mapping with a 'default' key")
        default = _tz(spec.get("default"), f"{p}.default")
        states = {str(k): _tz(v, f"{p}.{k}") for k, v in spec.items() if k != "default"}
        tzs[country] = CountryTimezones(default, states)
    for c in (COUNTRY_AU, COUNTRY_NZ):
        if c not in tzs:
            raise ConfigError(f"missing required section 'territory.timezones.{c}'")
    territory = Territory(
        _str_list(t, "priority_countries", "territory", allow_empty=False),
        _str_list(t, "secondary_countries", "territory"),
        tzs,
    )

    cs = _section(raw, "company_size")
    size = CompanySize(_get(cs, "max_employees", int, "company_size"),
                       _get(cs, "min_employees", int, "company_size"))
    if size.min_employees > size.max_employees:
        raise ConfigError("company_size.min_employees must be <= max_employees")

    tiers: dict[str, Tier] = {}
    for name, spec in _section(raw, "tiers").items():
        p = f"tiers.{name}"
        if not isinstance(spec, dict):
            raise ConfigError(f"'{p}' must be a mapping")
        emp = spec.get("employees")
        if (not isinstance(emp, list) or len(emp) != 2
                or not all(isinstance(x, int) and not isinstance(x, bool) for x in emp)):
            raise ConfigError(f"'{p}.employees' must be [min, max] integers")
        tiers[name] = Tier(emp[0], emp[1], str(spec.get("note", "")))

    wins_raw = raw.get("call_windows")
    if not isinstance(wins_raw, list) or not wins_raw:
        raise ConfigError("'call_windows' must be a non-empty list")
    windows = []
    for i, w in enumerate(wins_raw):
        if not isinstance(w, dict) or "name" not in w:
            raise ConfigError(f"'call_windows[{i}]' must have name, start, end")
        start, end = _hhmm(w.get("start"), f"call_windows[{i}].start"), _hhmm(w.get("end"), f"call_windows[{i}].end")
        if start >= end:
            raise ConfigError(f"'call_windows[{i}]' start must be before end")
        windows.append(CallWindow(str(w["name"]), start, end))

    lm = _section(raw, "limits")
    limits = Limits(**{k: _get(lm, k, int, "limits") for k in Limits.__dataclass_fields__})

    sources = _str_list(raw, "sources", "", allow_empty=False)

    pr = _section(raw, "personas")
    personas = Personas(_str_list(pr, "primary", "personas", allow_empty=False),
                        _str_list(pr, "secondary", "personas"))

    sg = _section(raw, "signals")
    wt = _section(sg, "weights", "signals.")
    weights = SignalWeights(**{k: float(_get(wt, k, (int, float), "signals.weights"))
                               for k in SignalWeights.__dataclass_fields__})
    signals = SignalsConfig(
        _str_list(sg, "scoop_types_keep", "signals"),
        _str_list(sg, "keywords_keep", "signals"),
        _str_list(sg, "keywords_drop", "signals"),
        _get(sg, "max_age_days", int, "signals"),
        weights,
    )

    fl = _section(raw, "filters")
    filters = Filters(_str_list(fl, "drop_name_patterns", "filters"),
                      _get(fl, "drop_if_no_domain", bool, "filters"))

    pa = _section(raw, "paths")
    paths = Paths(*(_get(pa, k, str, "paths") for k in ("state_dir", "log_dir", "output_dir")))

    return Config(owner, territory, size, tiers, tuple(windows), limits, sources,
                  personas, signals, filters, paths)


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
_JUNK = {"", "other", "n/a", "na", "null", "none", "outside u.s./canada", "-", "unknown"}


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
