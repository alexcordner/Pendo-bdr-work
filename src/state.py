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
no-answer / voicemail / gatekeeper / bad-number names AND duration >= limits.no_contact_max_duration_s.
Everything else is ``no_contact``. An unknown disposition (None) therefore follows the duration
rule only (the brief's literal definition).
"""
from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .loaders import Disposition, disposition_id_for_name, normalise_email_for_match
from .models import CallRecord, Prospect, SentEmail

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
STATE_FILENAME = "state.json"
SHEETS_KEPT = 3

CONNECT = "connect"
NO_CONTACT = "no_contact"


# ---------------------------------------------------------------- data classes
@dataclass
class AccountState:
    """What we know about one Salesforce account."""

    last_call_at: datetime | None = None
    last_outcome: str | None = None          # CONNECT | NO_CONTACT (outcome of the latest call)
    last_disposition: str | None = None
    last_connect_at: datetime | None = None
    last_email_at: datetime | None = None    # only DATED sent emails count here


@dataclass
class ProspectState:
    """What we know about one prospect (keyed by Nooks prospect id)."""

    call_count: int = 0
    connect_count: int = 0
    last_call_at: datetime | None = None
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
    dnc_phones: set[str] = field(default_factory=set)                     # E.164 of Do Not Call calls
    # recipient_key -> {message_id: iso sent_at | None}; None = "sent, date unknown"
    emails: dict[str, dict[str, str | None]] = field(default_factory=dict)
    sheets: list[dict] = field(default_factory=list)                      # [{run_date, account_ids}]
    # Counts from the latest record_* calls (NOT saved). Callers log these.
    report: dict[str, dict] = field(default_factory=dict)


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


def _str_or_none(v) -> str | None:
    return v if isinstance(v, str) else None


def state_to_dict(state: State) -> dict:
    """JSON-ready dict (sorted so files diff cleanly). ``report`` is not saved."""
    return {
        "version": state.version,
        "accounts": {
            k: {"last_call_at": _dt_out(a.last_call_at), "last_outcome": a.last_outcome,
                "last_disposition": a.last_disposition, "last_connect_at": _dt_out(a.last_connect_at),
                "last_email_at": _dt_out(a.last_email_at)}
            for k, a in sorted(state.accounts.items())},
        "prospects": {
            k: {"call_count": p.call_count, "connect_count": p.connect_count,
                "last_call_at": _dt_out(p.last_call_at), "last_outcome": p.last_outcome,
                "last_disposition": p.last_disposition, "last_connect_at": _dt_out(p.last_connect_at)}
            for k, p in sorted(state.prospects.items())},
        "seen_calls": dict(sorted(state.seen_calls.items())),
        "dnc_prospect_ids": sorted(state.dnc_prospect_ids),
        "dnc_phones": sorted(state.dnc_phones),
        "emails": {k: dict(sorted(v.items())) for k, v in sorted(state.emails.items())},
        "sheets": state.sheets,
    }


def state_from_dict(raw: dict) -> State:
    """Rebuild a State. Bad individual entries are skipped with a warning; raises ValueError
    only when the top level is unusable (caller turns that into an empty State)."""
    if not isinstance(raw, dict):
        raise ValueError("state file is not a JSON object")
    if raw.get("version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported state version {raw.get('version')!r} (expected {SCHEMA_VERSION})")
    st = State()

    for k, a in (raw.get("accounts") or {}).items():
        if not isinstance(a, dict):
            log.warning("state: skipping malformed account entry %r", k)
            continue
        st.accounts[k] = AccountState(
            _dt_in(a.get("last_call_at")), _str_or_none(a.get("last_outcome")),
            _str_or_none(a.get("last_disposition")), _dt_in(a.get("last_connect_at")),
            _dt_in(a.get("last_email_at")))

    for k, p in (raw.get("prospects") or {}).items():
        if not isinstance(p, dict):
            log.warning("state: skipping malformed prospect entry %r", k)
            continue
        cc, kc = p.get("call_count", 0), p.get("connect_count", 0)
        st.prospects[k] = ProspectState(
            cc if isinstance(cc, int) else 0, kc if isinstance(kc, int) else 0,
            _dt_in(p.get("last_call_at")), _str_or_none(p.get("last_outcome")),
            _str_or_none(p.get("last_disposition")), _dt_in(p.get("last_connect_at")))

    seen = raw.get("seen_calls") or {}
    st.seen_calls = {k: v for k, v in seen.items() if isinstance(k, str)} if isinstance(seen, dict) else {}
    st.dnc_prospect_ids = {x for x in (raw.get("dnc_prospect_ids") or []) if isinstance(x, str)}
    st.dnc_phones = {x for x in (raw.get("dnc_phones") or []) if isinstance(x, str)}

    for key, msgs in (raw.get("emails") or {}).items():
        if not isinstance(msgs, dict):
            log.warning("state: skipping malformed email entry")
            continue
        st.emails[key] = {m: (d if isinstance(d, str) else None) for m, d in msgs.items()}

    for s in raw.get("sheets") or []:
        if isinstance(s, dict) and _d_in(s.get("run_date")) and isinstance(s.get("account_ids"), list):
            st.sheets.append({"run_date": s["run_date"],
                              "account_ids": [x for x in s["account_ids"] if isinstance(x, str)]})
        else:
            log.warning("state: skipping malformed sheet entry")
    return st


def _d_in(v) -> date | None:
    try:
        return date.fromisoformat(v) if isinstance(v, str) else None
    except ValueError:
        return None


# ---------------------------------------------------------------- load / save
def _state_path(state_dir) -> Path:
    return Path(state_dir) / STATE_FILENAME


def load_state(state_dir) -> State:
    """Load state. Missing file -> empty State. Corrupt / unreadable / wrong version -> empty State,
    a warning, and the bad file is moved aside (``state.json.corrupt-N``) so a later save cannot
    destroy it. Never raises for bad content."""
    path = _state_path(state_dir)
    if not path.exists():
        return State()
    try:
        with open(path, encoding="utf-8") as f:
            return state_from_dict(json.load(f))
    except (OSError, ValueError) as e:     # JSONDecodeError and UnicodeDecodeError are ValueErrors
        log.warning("state: could not load %s (%s); starting with empty state", path, e)
        _set_aside(path)
        return State()


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
def _norm(s: str | None) -> str:
    return (s or "").strip().casefold()


def call_outcome(call: CallRecord, config: Config) -> str:
    """CONNECT or NO_CONTACT per the module docstring. Compares disposition NAMES only."""
    d = config.dispositions
    no_contact_names = {_norm(d.no_answer), _norm(d.voicemail), _norm(d.gatekeeper), _norm(d.wrong_number)}
    if _norm(call.disposition_name) in no_contact_names:
        return NO_CONTACT
    if (call.duration_s or 0) < config.limits.no_contact_max_duration_s:
        return NO_CONTACT
    return CONNECT


def _is_dnc(call: CallRecord, config: Config) -> bool:
    return bool(call.disposition_name) and _norm(call.disposition_name) == _norm(config.dispositions.do_not_call)


def do_not_call_prospects(calls: list[CallRecord], config: Config) -> set[str]:
    """Nooks prospect ids with ANY past call dispositioned 'Do Not Call' (matched by NAME).
    Calls with no prospect id are skipped here (see do_not_call_phones for those)."""
    return {c.prospect_id for c in calls if c.prospect_id and _is_dnc(c, config)}


def do_not_call_phones(calls: list[CallRecord], config: Config) -> set[str]:
    """E.164 numbers of any 'Do Not Call' call, so an unlinked DNC call still blocks that number."""
    return {c.to_e164 for c in calls if c.to_e164 and _is_dnc(c, config)}


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
    if not call.to_e164:
        return None, None, "no_phone"
    hits = by_phone.get(call.to_e164, [])
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
    """Fold calls into a copy of ``state`` and return it. ``config`` is keyword-only and required
    (outcome thresholds and the Do Not Call name come from it).

    Per account: last_call_at / last_outcome / last_connect_at. Per prospect: counts and last values.
    Replaying a call (same nooks_id) is a no-op. Calls that cannot be tied to exactly one prospect
    are NOT recorded and are listed in ``state.report['record_calls']``. A call tied to a prospect
    with no known account updates the prospect only. Out-of-order input is fine (latest wins).
    """
    st = copy.deepcopy(state)
    by_id = {p.nooks_id: p for p in prospects if p.nooks_id}
    by_phone: dict[str, list[Prospect]] = {}
    for p in prospects:
        if p.phone_e164:
            by_phone.setdefault(p.phone_e164, []).append(p)

    rep = {"new": 0, "duplicate": 0, "unmatched": 0, "unmatched_call_ids": [],
           "unmatched_reasons": {}, "no_account": 0, "dnc_added": 0}

    # Do Not Call is sticky and does not need a prospect match: record ids and numbers first.
    for c in calls:
        if _is_dnc(c, config):
            if c.to_e164:
                st.dnc_phones.add(c.to_e164)
            if c.prospect_id and c.prospect_id not in st.dnc_prospect_ids:
                st.dnc_prospect_ids.add(c.prospect_id)
                rep["dnc_added"] += 1

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
        if status == "phone" and _is_dnc(c, config):    # exact single-number match: block that person
            st.dnc_prospect_ids.add(pkey)

        outcome = call_outcome(c, config)
        st.seen_calls[key] = _dt_out(c.started_at)
        rep["new"] += 1

        ps = st.prospects.setdefault(pkey, ProspectState())
        ps.call_count += 1
        ps.connect_count += outcome == CONNECT
        _apply_latest(ps, c, outcome)

        sf_id = _account_id_for(prospect, accounts_by_id)
        if sf_id is None:
            rep["no_account"] += 1
            continue
        _apply_latest(st.accounts.setdefault(sf_id, AccountState()), c, outcome)

    st.report["record_calls"] = rep
    if rep["unmatched"]:
        log.warning("state: %d call(s) could not be matched to a prospect: %s",
                    rep["unmatched"], rep["unmatched_reasons"])
    return st


def _apply_latest(obj, c: CallRecord, outcome: str) -> None:
    """Update last_* fields on an AccountState/ProspectState; the newest call wins, ties go to the new call."""
    if obj.last_call_at is None or c.started_at >= obj.last_call_at:
        obj.last_call_at = c.started_at
        obj.last_outcome = outcome
        obj.last_disposition = c.disposition_name
    if outcome == CONNECT and (obj.last_connect_at is None or c.started_at > obj.last_connect_at):
        obj.last_connect_at = c.started_at


# ---------------------------------------------------------------- do-not-contact
def is_blocked(prospect: Prospect, state: State, channel: str = "any") -> tuple[bool, str]:
    """(blocked?, reason). ``channel``: 'call', 'email' or 'any' (default).
    Do Not Call blocks every channel (decision 2: never contact). An email opt-out blocks 'email' and 'any'
    but NOT 'call'. The reason is always non-empty when blocked, '' when not."""
    if channel not in ("call", "email", "any"):
        raise ValueError(f"channel must be 'call', 'email' or 'any', got {channel!r}")
    if prospect.do_not_call:
        return True, "Do Not Call flag set on prospect"
    if prospect.nooks_id and prospect.nooks_id in state.dnc_prospect_ids:
        return True, "past call dispositioned Do Not Call"
    if prospect.phone_e164 and prospect.phone_e164 in state.dnc_phones:
        return True, f"phone {prospect.phone_e164} had a Do Not Call call"
    if channel in ("email", "any") and prospect.opted_out_email:
        return True, "email opt-out (HasOptedOutOfEmail)"
    return False, ""


def email_opt_out_keys(prospects: list[Prospect]) -> set[str]:
    """Normalised email keys of prospects with opted_out_email True (for matching against any address)."""
    return {k for p in prospects if p.opted_out_email and (k := normalise_email_for_match(p.email))}


def mark_do_not_call(prospects: list[Prospect], state: State) -> list[Prospect]:
    """Copies of ``prospects`` with do_not_call set from state (ids and numbers). Input untouched."""
    from dataclasses import replace
    out = []
    for p in prospects:
        blocked = bool(p.nooks_id and p.nooks_id in state.dnc_prospect_ids) or \
            bool(p.phone_e164 and p.phone_e164 in state.dnc_phones)
        out.append(replace(p, do_not_call=True) if blocked and not p.do_not_call else p)
    return out


# ---------------------------------------------------------------- cooldowns
def last_touched(state: State) -> dict[str, date]:
    """sf_account_id -> date of the latest call or (dated) email."""
    out: dict[str, date] = {}
    for sf_id, a in state.accounts.items():
        stamps = [t for t in (a.last_call_at, a.last_email_at) if t]
        if stamps:
            out[sf_id] = max(stamps).astimezone(timezone.utc).date()
    return out


def cooldown_until(state: State, config: Config) -> dict[str, date]:
    """sf_account_id -> first date the account may be called again.

    A no-contact latest call blocks for cooldown_days_no_answer from that call; any connect blocks
    for cooldown_days_after_connect from the connect. The later of the two applies. Emails do not
    start a call cooldown (they have their own: emailed_recently)."""
    out: dict[str, date] = {}
    for sf_id, a in state.accounts.items():
        ends = []
        if a.last_connect_at:
            ends.append(a.last_connect_at.astimezone(timezone.utc).date()
                        + timedelta(days=config.limits.cooldown_days_after_connect))
        if a.last_call_at and a.last_outcome == NO_CONTACT:
            ends.append(a.last_call_at.astimezone(timezone.utc).date()
                        + timedelta(days=config.limits.cooldown_days_no_answer))
        if ends:
            out[sf_id] = max(ends)
    return out


def is_in_cooldown(state: State, account_id: str | None, today: date, config: Config) -> bool:
    """True if ``today`` is before the account's cooldown end. Unknown / None account is not in cooldown."""
    if not account_id:
        return False
    until = cooldown_until(state, config).get(account_id)
    return until is not None and today < until


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
def record_sent_emails(state: State, sent: list[SentEmail], prospects: list[Prospect]) -> State:
    """Remember sent emails per recipient_key and, when the prospect (matched on normalised email)
    has a Salesforce account and the date is known, that account's last_email_at.
    Dedupes on message_id. A known date replaces an earlier 'date unknown' entry for the same message."""
    st = copy.deepcopy(state)
    acct_by_key: dict[str, str] = {}
    for p in prospects:
        k = normalise_email_for_match(p.email)
        if k and p.sf_account_id:
            acct_by_key[k] = p.sf_account_id

    rep = {"new": 0, "duplicate": 0, "skipped_no_key": 0, "date_unknown": 0}
    for s in sent:
        key = s.recipient_key or normalise_email_for_match(s.recipient)
        if not key or not s.message_id:
            rep["skipped_no_key"] += 1
            continue
        msgs = st.emails.setdefault(key, {})
        iso = _dt_out(s.sent_at)
        if s.message_id in msgs:
            if msgs[s.message_id] is None and iso:     # date now known: upgrade
                msgs[s.message_id] = iso
            else:
                rep["duplicate"] += 1
                continue
        else:
            msgs[s.message_id] = iso
            rep["new"] += 1
        if iso is None:
            rep["date_unknown"] += 1
        elif key in acct_by_key:
            a = st.accounts.setdefault(acct_by_key[key], AccountState())
            if a.last_email_at is None or s.sent_at > a.last_email_at:
                a.last_email_at = s.sent_at
    st.report["record_sent_emails"] = rep
    return st


def emailed_recently(state: State, email: str | None, today: date, days: int) -> tuple[bool, str]:
    """(recent?, reason). Matches on normalise_email_for_match (dots in the local part ignored).
    A sent email with an unknown date counts as recent (conservative) and the reason says so.
    ``days`` is normally config.limits.email_cooldown_days."""
    key = normalise_email_for_match(email)
    if not key:
        return False, "no usable email address"
    msgs = state.emails.get(key)
    if not msgs:
        return False, "no sent email on record"
    dated = [d for d in (_dt_in(v) for v in msgs.values()) if d]
    undated = [m for m, v in msgs.items() if _dt_in(v) is None]   # None or unparseable
    if dated:
        age = (today - max(dated).astimezone(timezone.utc).date()).days
        if age < days:
            return True, f"emailed {age} day(s) ago (within {days})"
    if undated:
        return True, "sent email found but its date is unknown; treated as recent"
    return False, f"last emailed {age} day(s) ago (outside {days})"


# ---------------------------------------------------------------- disposition check
def verify_dispositions(config: Config, disposition_index: dict[str, Disposition]) -> list[str]:
    """Configured disposition names that are missing from the live list (empty = all good).
    The caller must log and STOP when this is non-empty."""
    d = config.dispositions
    names = [d.voicemail, d.no_answer, d.do_not_call, d.gatekeeper, d.wrong_number]
    return [n for n in names if disposition_id_for_name(disposition_index, n) is None]
