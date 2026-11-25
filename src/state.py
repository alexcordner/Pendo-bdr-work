"""Persistent memory for the call sheet (task 1c): who was called, when, what happened.

Plain data in, plain data out. Nothing here touches the network. State lives in ONE json file
(``<state_dir>/state.json``, gitignored) written atomically.

Design notes
  * Every ``record_*`` function returns a NEW State (deep copy); the input is never mutated.
  * Replaying the same calls / emails / sheet twice changes nothing (calls dedupe on nooks_id).
  * Unknown, missing or None ids never raise and are never guessed: they are counted in
    ``state.report`` and skipped.
  * Dates in state are UTC. A call's "day" is the UTC date of ``started_at``.
  * Loading is tolerant: a missing, corrupt or newer-version file gives an empty State plus a
    logged warning. A corrupt file is moved aside (never overwritten or deleted).

Outcomes: a call is a ``connect`` only if its disposition is NOT one of the configured
no-answer / voicemail / gatekeeper / bad-number / do-not-call names AND duration >=
limits.no_contact_max_duration_s. Everything else is ``no_contact``. An unknown disposition (None)
therefore follows the duration rule only.

Days are ACCOUNT-LOCAL: the date a call or email happened is computed in the account's timezone
(``Account.timezone`` from ``accounts_by_id``; Australia/Sydney when unknown or invalid) and stored
as a date. Callers pass an account-local ``today`` to ``is_in_cooldown`` / ``emailed_recently``.

Safety: if state.json was unreadable the returned State has ``was_reset=True``. A live run must call
``require_healthy`` (see docs/state_notes.md); a dry run may continue.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config
from .loaders import Disposition, disposition_id_for_name, normalise_email_for_match, normalise_phone
from .models import CallRecord, Prospect, SentEmail

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2
STATE_FILENAME = "state.json"
SHEETS_KEPT = 3

CONNECT = "connect"
NO_CONTACT = "no_contact"
DEFAULT_TZ = "Australia/Sydney"   # used when an account timezone is unknown or invalid


class StateResetError(RuntimeError):
    """The state file was unreadable and the caller has not confirmed a rebuild (see require_healthy)."""


# ---------------------------------------------------------------- data classes
@dataclass
class AccountState:
    """What we know about one Salesforce account. ``*_date`` fields are ACCOUNT-LOCAL dates (iso)."""

    last_call_at: datetime | None = None
    last_call_date: str | None = None
    last_call_key: str | None = None         # tie-break for equal timestamps
    last_outcome: str | None = None          # CONNECT | NO_CONTACT (outcome of the latest call)
    last_disposition: str | None = None
    last_connect_at: datetime | None = None
    last_connect_date: str | None = None
    last_email_at: datetime | None = None    # only DATED sent emails count here
    last_email_date: str | None = None
    nooks_account_id: str | None = None      # alternate keys, when known (for lookups by those ids)
    zoominfo_id: str | None = None


@dataclass
class ProspectState:
    """What we know about one prospect (keyed by Nooks prospect id)."""

    call_count: int = 0
    connect_count: int = 0
    last_call_at: datetime | None = None
    last_call_key: str | None = None
    last_outcome: str | None = None
    last_disposition: str | None = None
    last_connect_at: datetime | None = None


@dataclass
class State:
    version: int = SCHEMA_VERSION
    accounts: dict[str, AccountState] = field(default_factory=dict)       # sf_account_id -> ...
    prospects: dict[str, ProspectState] = field(default_factory=dict)     # nooks prospect id -> ...
    seen_calls: dict[str, str] = field(default_factory=dict)              # call key -> iso start (dedupe)
    dnc_prospect_ids: set[str] = field(default_factory=set)               # sticky, never removed
    dnc_phones: set[str] = field(default_factory=set)                     # canonical phones of DNC calls
    dnc_emails: set[str] = field(default_factory=set)                     # HASHED email keys of DNC people
    # hashed recipient key -> {message_id: {"sent_on": local iso date | None, "first_seen": iso date}}
    emails: dict[str, dict[str, dict]] = field(default_factory=dict)
    sheets: list[dict] = field(default_factory=list)                      # [{run_date, account_ids}]
    # ---- below: NOT saved
    report: dict[str, dict] = field(default_factory=dict)                 # counts from record_* calls
    was_reset: bool = False                  # True when a state file existed but could not be used
    reset_reason: str | None = None
    is_new: bool = False                     # True when there was no state file at all (first run)
    load_warnings: list[str] = field(default_factory=list)                # skipped malformed entries


def require_healthy(state: State, rebuilt_from_history: bool = False) -> None:
    """Gate for LIVE runs. Raises StateResetError when the state file was unreadable (so DNC memory,
    cooldowns and email history are gone) unless the caller confirms it re-fetched the FULL Nooks
    call history and re-ran record_calls on it (rebuilding Do Not Call). The nightly runner must
    refuse to write tasks or drafts when this raises. Dry runs need not call it."""
    if state.was_reset and not rebuilt_from_history:
        raise StateResetError(
            f"state was reset ({state.reset_reason}); refusing live writes until the full Nooks call "
            "history is re-fetched to rebuild Do Not Call (pass rebuilt_from_history=True once done)")


# ---------------------------------------------------------------- small helpers
def _hash_email(addr: str | None) -> str | None:
    """sha256 of the normalised (dot-stripped, lower-case) address, so raw emails are not stored.
    Matching still uses loaders.normalise_email_for_match."""
    key = normalise_email_for_match(addr)
    return hashlib.sha256(("callsheet-v1:" + key).encode()).hexdigest() if key else None


def _phone_key(raw: str | None) -> str | None:
    """Canonical phone for DNC storage/matching: loaders.normalise_phone, else the raw value with
    spaces and punctuation stripped (fail closed: still blocks that exact number)."""
    if not raw or not isinstance(raw, str):
        return None
    return normalise_phone(raw) or (re.sub(r"[\s().\-]", "", raw) or None)


def _norm(s: str | None) -> str:
    """Disposition name key: case, punctuation, NBSP and repeated whitespace are ignored.
    'Do Not Call.', 'DO  NOT CALL' and 'Do-Not-Call' all become 'do not call'."""
    return " ".join(re.sub(r"[\W_]+", " ", s or "").casefold().split())


def _tz(acct) -> ZoneInfo:
    """Account's zone, or Australia/Sydney if missing/invalid. Never raises."""
    name = getattr(acct, "timezone", None)
    if name:
        try:
            return ZoneInfo(name)
        except Exception:      # unknown key, bad type, bad path chars
            log.warning("state: invalid timezone %r; using %s", name, DEFAULT_TZ)
    return ZoneInfo(DEFAULT_TZ)


def _local_date(when: datetime, acct) -> str:
    return when.astimezone(_tz(acct)).date().isoformat()


# ---------------------------------------------------------------- (de)serialisation
def _dt_out(v: datetime | None) -> str | None:
    return v.astimezone(timezone.utc).isoformat() if v else None


def _dt_in(v) -> datetime | None:
    if not v or not isinstance(v, str):
        return None
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _d_in(v) -> date | None:
    try:
        return date.fromisoformat(v) if isinstance(v, str) else None
    except ValueError:
        return None


def _date_str(v) -> str | None:
    return v if _d_in(v) else None


def _str_or_none(v) -> str | None:
    return v if isinstance(v, str) else None


def state_to_dict(state: State) -> dict:
    """JSON-ready dict (sorted so files diff cleanly). Report/reset flags are not saved."""
    return {
        "version": state.version,
        "accounts": {
            k: {"last_call_at": _dt_out(a.last_call_at), "last_call_date": a.last_call_date,
                "last_call_key": a.last_call_key, "last_outcome": a.last_outcome,
                "last_disposition": a.last_disposition, "last_connect_at": _dt_out(a.last_connect_at),
                "last_connect_date": a.last_connect_date, "last_email_at": _dt_out(a.last_email_at),
                "last_email_date": a.last_email_date, "nooks_account_id": a.nooks_account_id,
                "zoominfo_id": a.zoominfo_id}
            for k, a in sorted(state.accounts.items())},
        "prospects": {
            k: {"call_count": p.call_count, "connect_count": p.connect_count,
                "last_call_at": _dt_out(p.last_call_at), "last_call_key": p.last_call_key,
                "last_outcome": p.last_outcome, "last_disposition": p.last_disposition,
                "last_connect_at": _dt_out(p.last_connect_at)}
            for k, p in sorted(state.prospects.items())},
        "seen_calls": dict(sorted(state.seen_calls.items())),
        "dnc_prospect_ids": sorted(state.dnc_prospect_ids),
        "dnc_phones": sorted(state.dnc_phones),
        "dnc_emails": sorted(state.dnc_emails),
        "emails": {k: {m: dict(v) for m, v in sorted(msgs.items())} for k, msgs in sorted(state.emails.items())},
        "sheets": state.sheets,
    }


def _need(raw: dict, key: str, typ):
    """Container check: the key may be absent, but if present it must be the right type."""
    v = raw.get(key)
    if v is None:
        return typ()
    if not isinstance(v, typ):
        raise ValueError(f"'{key}' must be a {typ.__name__}, got {type(v).__name__}")
    return v


def _str_list(raw: dict, key: str) -> set[str]:
    v = _need(raw, key, list)
    if not all(isinstance(x, str) for x in v):
        raise ValueError(f"'{key}' must contain only strings")   # DNC data: do not half-load it
    return set(v)


def state_from_dict(raw: dict) -> State:
    """Rebuild a State. Raises ValueError when a top-level container is the wrong type or a DNC list
    is malformed (load_state turns that into a flagged reset). Single malformed account / prospect /
    email / sheet entries are skipped and listed in ``load_warnings``."""
    if not isinstance(raw, dict):
        raise ValueError("state file is not a JSON object")
    if raw.get("version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported state version {raw.get('version')!r} (expected {SCHEMA_VERSION})")
    st = State()
    warn = st.load_warnings

    accounts, prospects = _need(raw, "accounts", dict), _need(raw, "prospects", dict)
    seen, emails, sheets = _need(raw, "seen_calls", dict), _need(raw, "emails", dict), _need(raw, "sheets", list)
    st.dnc_prospect_ids = _str_list(raw, "dnc_prospect_ids")
    st.dnc_phones = _str_list(raw, "dnc_phones")
    st.dnc_emails = _str_list(raw, "dnc_emails")

    for k, a in accounts.items():
        if not isinstance(a, dict):
            warn.append(f"malformed account entry {k!r}")
            continue
        st.accounts[k] = AccountState(
            _dt_in(a.get("last_call_at")), _date_str(a.get("last_call_date")), _str_or_none(a.get("last_call_key")),
            _str_or_none(a.get("last_outcome")), _str_or_none(a.get("last_disposition")),
            _dt_in(a.get("last_connect_at")), _date_str(a.get("last_connect_date")),
            _dt_in(a.get("last_email_at")), _date_str(a.get("last_email_date")),
            _str_or_none(a.get("nooks_account_id")), _str_or_none(a.get("zoominfo_id")))

    for k, p in prospects.items():
        if not isinstance(p, dict):
            warn.append(f"malformed prospect entry {k!r}")
            continue
        cc, kc = p.get("call_count", 0), p.get("connect_count", 0)
        st.prospects[k] = ProspectState(
            cc if isinstance(cc, int) else 0, kc if isinstance(kc, int) else 0,
            _dt_in(p.get("last_call_at")), _str_or_none(p.get("last_call_key")),
            _str_or_none(p.get("last_outcome")), _str_or_none(p.get("last_disposition")),
            _dt_in(p.get("last_connect_at")))

    st.seen_calls = {k: v for k, v in seen.items() if isinstance(k, str) and isinstance(v, (str, type(None)))}

    for key, msgs in emails.items():
        if not isinstance(msgs, dict):
            warn.append("malformed email entry")
            continue
        clean = {}
        for m, v in msgs.items():
            if isinstance(v, dict) and _d_in(v.get("first_seen")):
                clean[m] = {"sent_on": _date_str(v.get("sent_on")), "first_seen": v["first_seen"]}
            else:
                warn.append(f"malformed email message entry {m!r}")
        if clean:
            st.emails[key] = clean

    for s in sheets:
        if isinstance(s, dict) and _d_in(s.get("run_date")) and isinstance(s.get("account_ids"), list):
            st.sheets.append({"run_date": s["run_date"],
                              "account_ids": [x for x in s["account_ids"] if isinstance(x, str)]})
        else:
            warn.append("malformed sheet entry")
    for w in warn:
        log.warning("state: skipped %s", w)
    return st


# ---------------------------------------------------------------- load / save
def _state_path(state_dir) -> Path:
    return Path(state_dir) / STATE_FILENAME


def _reset_state(reason: str) -> State:
    return State(was_reset=True, reset_reason=reason)


def load_state(state_dir) -> State:
    """Load state. Never raises for bad content.

    * No file: empty State with ``is_new=True`` (first run).
    * Unreadable / corrupt / truncated / wrong version / wrong container type: empty State with
      ``was_reset=True`` and ``reset_reason`` set, a logged warning, and the bad file moved aside
      (``state.json.corrupt-N``) so a later save cannot destroy it. Live runs must then pass
      ``require_healthy`` (rebuild Do Not Call from full call history first)."""
    path = _state_path(state_dir)
    if not path.exists():
        return State(is_new=True)
    try:
        with open(path, encoding="utf-8") as f:
            return state_from_dict(json.load(f))
    except (OSError, ValueError, AttributeError, TypeError) as e:   # JSONDecodeError is a ValueError
        reason = f"{path.name} unusable: {e}"
        log.warning("state: could not load %s (%s); starting with empty state", path, e)
        _set_aside(path)
        return _reset_state(reason)


def _set_aside(path: Path) -> None:
    for n in range(1, 1000):
        target = path.with_name(f"{path.name}.corrupt-{n}")
        if not target.exists():
            try:
                os.replace(path, target)
                log.warning("state: moved unreadable file to %s", target)
            except OSError as e:
                log.warning("state: could not move %s aside (%s)", path, e)
            return


def save_state(state: State, state_dir) -> Path:
    """Atomic write: temp file in the same directory, fsync, then rename over the target.
    On any failure the temp file is removed and the old file is untouched."""
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = _state_path(d)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state_to_dict(state), f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


# ---------------------------------------------------------------- calls
def call_outcome(call: CallRecord, config: Config) -> str:
    """CONNECT or NO_CONTACT per the module docstring. Compares disposition NAMES only (normalised)."""
    d = config.dispositions
    no_contact_names = {_norm(d.no_answer), _norm(d.voicemail), _norm(d.gatekeeper),
                        _norm(d.wrong_number), _norm(d.do_not_call)}
    if _norm(call.disposition_name) in no_contact_names:
        return NO_CONTACT
    if (call.duration_s or 0) < config.limits.no_contact_max_duration_s:
        return NO_CONTACT
    return CONNECT


def _is_dnc(call: CallRecord, config: Config) -> bool:
    n = _norm(call.disposition_name)
    return bool(n) and n == _norm(config.dispositions.do_not_call)


def do_not_call_prospects(calls: list[CallRecord], config: Config) -> set[str]:
    """Nooks prospect ids with ANY past call dispositioned 'Do Not Call' (matched by normalised NAME).
    Calls with no prospect id are skipped here (see do_not_call_phones for those)."""
    return {c.prospect_id for c in calls if c.prospect_id and _is_dnc(c, config)}


def do_not_call_phones(calls: list[CallRecord], config: Config) -> set[str]:
    """Canonical numbers of any 'Do Not Call' call, so an unlinked DNC call still blocks that number."""
    return {k for c in calls if _is_dnc(c, config) and (k := _phone_key(c.to_e164))}


def _call_key(c: CallRecord) -> str:
    """Dedupe key: the call id, or (if the id is missing) a fingerprint of the call."""
    if c.nooks_id:
        return c.nooks_id
    return f"anon|{c.prospect_id}|{_dt_out(c.started_at)}|{c.to_e164}"


def _link_prospect(call: CallRecord, by_id: dict, by_phone: dict) -> tuple[Prospect | None, str | None, str]:
    """Return (prospect or None, prospect key for history, status).
    status: 'id' | 'id_unknown_prospect' | 'phone' | 'no_phone' | 'ambiguous_phone' | 'no_match'.
    Never guesses: a phone must identify exactly one prospect."""
    if call.prospect_id:
        p = by_id.get(call.prospect_id)
        return p, call.prospect_id, ("id" if p else "id_unknown_prospect")
    phone = _phone_key(call.to_e164)
    if not phone:
        return None, None, "no_phone"
    hits = by_phone.get(phone, [])
    if len(hits) == 1:
        return hits[0], hits[0].nooks_id, "phone"
    return None, None, ("ambiguous_phone" if hits else "no_match")


def _account_id_for(p: Prospect | None, accounts_by_id) -> str | None:
    if p is None:
        return None
    if p.sf_account_id:
        return p.sf_account_id
    if p.nooks_account_id and accounts_by_id:
        for sf_id, acct in accounts_by_id.items():
            if sf_id and getattr(acct, "nooks_account_id", None) == p.nooks_account_id:
                return sf_id
    return None


def record_calls(state: State, calls: list[CallRecord], prospects: list[Prospect],
                 accounts_by_id: dict | None = None, *, config: Config) -> State:
    """Fold calls into a copy of ``state`` and return it. ``config`` is keyword-only and required.

    Per account: last_call / last_outcome / last_connect (instants plus ACCOUNT-LOCAL dates; the zone
    is ``accounts_by_id[sf_id].timezone``, default Australia/Sydney). Per prospect: counts and last
    values. Replaying a call (same nooks_id) is a no-op. Calls that cannot be tied to exactly one
    prospect are NOT recorded and are listed in ``state.report['record_calls']``. A call tied to a
    prospect with no known account updates the prospect only. Input order never changes the result
    (latest start wins; equal starts are broken by call id).

    Do Not Call (any past call, matched by normalised name) is sticky and recorded three ways: the
    prospect id, the canonical phone dialled, and the hashed email of that prospect, so a duplicate
    Nooks prospect for the same person stays blocked.
    """
    st = copy.deepcopy(state)
    accounts_by_id = accounts_by_id or {}
    by_id = {p.nooks_id: p for p in prospects if p.nooks_id}
    by_phone: dict[str, list[Prospect]] = {}
    for p in prospects:
        if (k := _phone_key(p.phone_e164)):
            by_phone.setdefault(k, []).append(p)

    rep = {"new": 0, "duplicate": 0, "unmatched": 0, "unmatched_call_ids": [],
           "unmatched_reasons": {}, "no_account": 0, "dnc_added": 0}

    # Do Not Call is sticky and does not need an account match: record it first, for every call.
    for c in calls:
        if not _is_dnc(c, config):
            continue
        if (ph := _phone_key(c.to_e164)):
            st.dnc_phones.add(ph)
        prospect, pkey, status = _link_prospect(c, by_id, by_phone)
        if pkey and pkey not in st.dnc_prospect_ids:      # id given, or the one prospect on that number
            st.dnc_prospect_ids.add(pkey)
            rep["dnc_added"] += 1
        if prospect and (h := _hash_email(prospect.email)):
            st.dnc_emails.add(h)
    # Any known prospect already on the DNC list contributes its email (e.g. DNC learned earlier).
    for p in prospects:
        if p.nooks_id in st.dnc_prospect_ids and (h := _hash_email(p.email)):
            st.dnc_emails.add(h)

    for c in calls:
        key = _call_key(c)
        if key in st.seen_calls:
            rep["duplicate"] += 1
            continue
        prospect, pkey, status = _link_prospect(c, by_id, by_phone)
        if pkey is None:
            rep["unmatched"] += 1
            rep["unmatched_call_ids"].append(key)
            rep["unmatched_reasons"][status] = rep["unmatched_reasons"].get(status, 0) + 1
            continue

        outcome = call_outcome(c, config)
        st.seen_calls[key] = _dt_out(c.started_at)
        rep["new"] += 1

        ps = st.prospects.setdefault(pkey, ProspectState())
        ps.call_count += 1
        ps.connect_count += outcome == CONNECT
        _apply_latest(ps, c, key, outcome)

        sf_id = _account_id_for(prospect, accounts_by_id)
        if sf_id is None:
            rep["no_account"] += 1
            continue
        acct_obj = accounts_by_id.get(sf_id)
        a = st.accounts.setdefault(sf_id, AccountState())
        a.nooks_account_id = a.nooks_account_id or getattr(acct_obj, "nooks_account_id", None) \
            or (prospect.nooks_account_id if prospect else None)
        a.zoominfo_id = a.zoominfo_id or getattr(acct_obj, "zoominfo_id", None)
        _apply_latest(a, c, key, outcome, acct_obj)

    rep["unmatched_call_ids"].sort()
    st.report["record_calls"] = rep
    if rep["unmatched"]:
        log.warning("state: %d call(s) could not be matched to a prospect: %s",
                    rep["unmatched"], rep["unmatched_reasons"])
    return st


def _apply_latest(obj, c: CallRecord, key: str, outcome: str, acct=None) -> None:
    """Update last_* on an AccountState/ProspectState. Newest start wins; equal starts go to the
    higher call key, so input order cannot change the result. Local dates only exist on accounts."""
    is_account = isinstance(obj, AccountState)
    newer = obj.last_call_at is None or (c.started_at, key) > (obj.last_call_at, obj.last_call_key or "")
    if newer:
        obj.last_call_at, obj.last_call_key = c.started_at, key
        obj.last_outcome, obj.last_disposition = outcome, c.disposition_name
        if is_account:
            obj.last_call_date = _local_date(c.started_at, acct)
    if outcome == CONNECT and (obj.last_connect_at is None or c.started_at > obj.last_connect_at):
        obj.last_connect_at = c.started_at
        if is_account:
            obj.last_connect_date = _local_date(c.started_at, acct)


# ---------------------------------------------------------------- do-not-contact
def is_blocked(prospect: Prospect, state: State, channel: str = "any") -> tuple[bool, str]:
    """(blocked?, reason). ``channel``: 'call', 'email' or 'any' (default).
    Do Not Call blocks every channel (decision 2: never contact), whether it is found by prospect id,
    phone number or (hashed) email address. An email opt-out blocks 'email' and 'any' but NOT 'call'.
    The reason is non-empty when blocked, '' when not."""
    if channel not in ("call", "email", "any"):
        raise ValueError(f"channel must be 'call', 'email' or 'any', got {channel!r}")
    if prospect.do_not_call:
        return True, "Do Not Call flag set on prospect"
    if prospect.nooks_id and prospect.nooks_id in state.dnc_prospect_ids:
        return True, "past call dispositioned Do Not Call"
    phone = _phone_key(prospect.phone_e164)
    if phone and phone in state.dnc_phones:
        return True, f"phone {phone} had a Do Not Call call"
    h = _hash_email(prospect.email)
    if h and h in state.dnc_emails:
        return True, "same email address as a prospect with a Do Not Call call"
    if channel in ("email", "any") and prospect.opted_out_email:
        return True, "email opt-out (HasOptedOutOfEmail)"
    return False, ""


def email_opt_out_keys(prospects: list[Prospect]) -> set[str]:
    """Normalised email keys of prospects with opted_out_email True (for matching against any address)."""
    return {k for p in prospects if p.opted_out_email and (k := normalise_email_for_match(p.email))}


def mark_do_not_call(prospects: list[Prospect], state: State) -> list[Prospect]:
    """Copies of ``prospects`` with do_not_call set from state (id, phone or email). Input untouched."""
    out = []
    for p in prospects:
        blocked = is_blocked(p, state, "call")[0]
        out.append(replace(p, do_not_call=True) if blocked and not p.do_not_call else p)
    return out


# ---------------------------------------------------------------- cooldowns
def _d(v: str | None) -> date | None:
    return _d_in(v)


def last_touched(state: State) -> dict[str, date]:
    """sf_account_id -> ACCOUNT-LOCAL date of the latest call or (dated) email."""
    out: dict[str, date] = {}
    for sf_id, a in state.accounts.items():
        stamps = [d for d in (_d(a.last_call_date), _d(a.last_email_date)) if d]
        if stamps:
            out[sf_id] = max(stamps)
    return out


def cooldown_until(state: State, config: Config) -> dict[str, date]:
    """sf_account_id -> first ACCOUNT-LOCAL date the account may be called again (inclusive).
    The account is in cooldown while this date is later than the account's local today.

    A no-contact latest call blocks for cooldown_days_no_answer local days from that call's local
    date; a connect blocks for cooldown_days_after_connect from the connect's local date. The later
    applies. Emails do not start a call cooldown (they have their own: emailed_recently)."""
    out: dict[str, date] = {}
    for sf_id, a in state.accounts.items():
        ends = []
        if (cd := _d(a.last_connect_date)):
            ends.append(cd + timedelta(days=config.limits.cooldown_days_after_connect))
        if (ld := _d(a.last_call_date)) and a.last_outcome == NO_CONTACT:
            ends.append(ld + timedelta(days=config.limits.cooldown_days_no_answer))
        if ends:
            out[sf_id] = max(ends)
    return out


def cooldown_until_by(state: State, config: Config, id_kind: str) -> dict[str, date]:
    """Same as cooldown_until but keyed by 'nooks_account_id' or 'zoominfo_id' (where state holds
    one). Accounts without that id are left out; if two accounts share one id the later date wins."""
    if id_kind not in ("nooks_account_id", "zoominfo_id"):
        raise ValueError(f"id_kind must be 'nooks_account_id' or 'zoominfo_id', got {id_kind!r}")
    out: dict[str, date] = {}
    for sf_id, until in cooldown_until(state, config).items():
        alt = getattr(state.accounts[sf_id], id_kind)
        if alt and (alt not in out or until > out[alt]):
            out[alt] = until
    return out


def is_in_cooldown(state: State, account_id: str | None, today: date, config: Config) -> bool:
    """True while the account's cooldown end is later than ``today``. ``today`` must be the
    account-LOCAL date (the ranker uses windows[0].start.date() in the account timezone).
    Unknown / None account is not in cooldown."""
    if not account_id:
        return False
    until = cooldown_until(state, config).get(account_id)
    return until is not None and until > today


# ---------------------------------------------------------------- sheet history
def record_sheet(state: State, run_date: date, account_ids: list[str | None]) -> State:
    """Remember which accounts were on the sheet for ``run_date`` (None ids dropped, order kept, no
    duplicates). A second record for the same date replaces the first (re-runs are idempotent).
    Only the newest SHEETS_KEPT sheets are kept."""
    st = copy.deepcopy(state)
    ids = list(dict.fromkeys(a for a in account_ids if a))
    iso = run_date.isoformat()
    st.sheets = [s for s in st.sheets if s["run_date"] != iso] + [{"run_date": iso, "account_ids": ids}]
    st.sheets.sort(key=lambda s: s["run_date"])
    st.sheets = st.sheets[-SHEETS_KEPT:]
    return st


def last_sheet_ids(state: State) -> set[str]:
    return set(state.sheets[-1]["account_ids"]) if state.sheets else set()


def was_in_last_sheet(state: State, account_id: str | None) -> bool:
    """True if the account was on the most recent recorded sheet (so don't serve it twice in a row)."""
    return bool(account_id) and account_id in last_sheet_ids(state)


# ---------------------------------------------------------------- email history
def record_sent_emails(state: State, sent: list[SentEmail], prospects: list[Prospect],
                       accounts_by_id: dict | None = None, *, today: date | None = None) -> State:
    """Remember sent emails per (hashed) recipient and, when the prospect (matched on normalised email)
    has a Salesforce account and the date is known, that account's last_email_at / local date.

    ``today`` (default: today in UTC) is stored as ``first_seen`` for every new message. A message
    with ``sent_at=None`` ("sent, date unknown") is treated as recent only until
    first_seen + email_cooldown_days (see emailed_recently). Dedupes on message_id; a known date
    replaces an earlier unknown one. The sent date is stored in the account's local zone
    (Australia/Sydney if no account is known)."""
    st = copy.deepcopy(state)
    seen_on = (today or datetime.now(timezone.utc).date()).isoformat()
    accounts_by_id = accounts_by_id or {}
    acct_by_key: dict[str, str] = {}
    for p in prospects:
        k = normalise_email_for_match(p.email)
        if k and p.sf_account_id:
            acct_by_key[k] = p.sf_account_id

    rep = {"new": 0, "duplicate": 0, "skipped_no_key": 0, "date_unknown": 0}
    for s in sent:
        nk = normalise_email_for_match(s.recipient_key) or normalise_email_for_match(s.recipient)
        h = _hash_email(nk)
        if not h or not s.message_id:
            rep["skipped_no_key"] += 1
            continue
        sf_id = acct_by_key.get(nk)
        acct_obj = accounts_by_id.get(sf_id) if sf_id else None
        sent_on = _local_date(s.sent_at, acct_obj) if s.sent_at else None
        msgs = st.emails.setdefault(h, {})
        if s.message_id in msgs:
            if msgs[s.message_id]["sent_on"] is None and sent_on:   # date now known: upgrade
                msgs[s.message_id]["sent_on"] = sent_on
            else:
                rep["duplicate"] += 1
                continue
        else:
            msgs[s.message_id] = {"sent_on": sent_on, "first_seen": seen_on}
            rep["new"] += 1
        if sent_on is None:
            rep["date_unknown"] += 1
        elif sf_id:
            a = st.accounts.setdefault(sf_id, AccountState())
            if a.last_email_at is None or s.sent_at > a.last_email_at:
                a.last_email_at, a.last_email_date = s.sent_at, sent_on
    st.report["record_sent_emails"] = rep
    return st


def emailed_recently(state: State, email: str | None, today: date, days: int) -> tuple[bool, str]:
    """(recent?, reason). Matches on normalise_email_for_match (dots, case ignored).
    ``today`` is the account-local date; ``days`` is normally config.limits.email_cooldown_days.

    A dated email is recent while (today - sent_on) < days. A message with an UNKNOWN date counts as
    recent (conservative) only until first_seen + days, where first_seen is the run date it was first
    recorded (the email was sent on or before that day); the reason says so."""
    h = _hash_email(email)
    if not h:
        return False, "no usable email address"
    msgs = state.emails.get(h)
    if not msgs:
        return False, "no sent email on record"
    notes = []
    for mid, m in sorted(msgs.items()):
        sent_on, first_seen = _d_in(m.get("sent_on")), _d_in(m.get("first_seen"))
        if sent_on:
            age = (today - sent_on).days
            if age < days:
                return True, f"emailed on {sent_on} ({age} day(s) ago, within {days})"
            notes.append(f"last dated email {sent_on} is outside {days} days")
        elif first_seen:
            expires = first_seen + timedelta(days=days)
            if today < expires:
                return True, (f"sent email found but its date is unknown (first seen {first_seen}); "
                              f"treated as recent until {expires}")
            notes.append(f"undated email first seen {first_seen} expired {expires}")
    return False, "; ".join(notes) or "no usable email date"


# ---------------------------------------------------------------- disposition check
def verify_dispositions(config: Config, disposition_index: dict[str, Disposition]) -> list[str]:
    """Configured disposition names that are missing from the live list (empty = all good).
    The caller must log and STOP when this is non-empty."""
    d = config.dispositions
    names = [d.voicemail, d.no_answer, d.do_not_call, d.gatekeeper, d.wrong_number]
    return [n for n in names if disposition_id_for_name(disposition_index, n) is None]
