"""Intake (task 2a): read the raw-dump files a Claude session saved under output/raw/, validate
them, and convert the rows to the Phase 1 models with the existing loaders.

Python cannot call MCP connectors. A Claude session fetches the data (docs/fetch_runbook.md) and
saves it as ``{"meta": {...}, "rows": [...]}`` files (docs/intake_schema.md). This module only
reads those files: no network, no writes.

Rules (CLAUDE.md rule 6, never fake data):
  * ``load_raw`` NEVER raises for bad data. Everything wrong goes into ``RawData.report``
    (an ``IntakeReport``): ``errors`` stop the pipeline, ``warnings`` are logged,
    ``live_blockers`` make a live run impossible (a dry run still works).
  * A malformed row is never skipped silently: it is counted per source with sample reasons.
  * Samples and warnings never contain raw emails or phone numbers (``scrub``).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src.loaders import (LoaderError, account_from_nooks, account_from_sf_row, build_disposition_index,
                         call_from_nooks, normalise_email_for_match, normalise_phone, normalise_sf_id, parse_utc, prospect_from_nooks,
                         scoop_from_zoominfo, sent_emails_from_gmail, task_from_nooks)
from src.models import CallRecord, Prospect, Scoop, SentEmail, TaskRecord

STALE_AFTER = timedelta(hours=24)
FUTURE_SLACK = timedelta(minutes=5)          # clock skew allowed on fetched_at
SAMPLE_LIMIT = 5                             # sample reasons kept per source
HIGH_MALFORMED_RATE = 0.05                   # above this share of a source: loud warning

# file stem -> (required, paged). Paged sources must say whether the LAST page was reached.
FILES: dict[str, tuple[bool, bool]] = {
    "sf_accounts": (True, True),
    "sf_contacts": (True, True),
    "nooks_calls": (True, True),
    "nooks_dispositions": (True, False),
    "nooks_prospects": (False, True),     # lazily resolved ids only (workspace table is ~3M rows): never bulk
    "nooks_accounts": (False, True),
    "nooks_tasks": (False, True),
    "zoominfo_scoops": (True, True),
    "gmail_sent": (False, False),
}
RUN_META = "run_meta"
# A partial copy of these makes a live run unsafe (missing accounts, contacts, Do Not Call history,
# or our own earlier tasks). A partial scoop or Nooks account list only loses triggers / links.
SAFETY_PAGED = ("sf_accounts", "sf_contacts", "nooks_calls", "nooks_tasks")
# id key used to de-duplicate each source
ID_KEY = {"sf_accounts": "Id", "sf_contacts": "Id"}
META_KEYS = ("source_tool", "query", "page_count", "reached_last_page", "fetched_at")

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_DIGITS = re.compile(r"\+?\d[\d\s().-]{6,}\d")


def scrub(text: str) -> str:
    """Remove anything that looks like an email address or phone number (logs and samples)."""
    return _DIGITS.sub("<number>", _EMAIL.sub("<email>", str(text)))


@dataclass
class Malformed:
    count: int = 0
    samples: list[str] = field(default_factory=list)     # first SAMPLE_LIMIT scrubbed reasons

    def add(self, reason: str) -> None:
        self.count += 1
        if len(self.samples) < SAMPLE_LIMIT:
            self.samples.append(scrub(reason))


@dataclass
class IntakeReport:
    errors: list[str] = field(default_factory=list)          # stop the pipeline
    warnings: list[str] = field(default_factory=list)        # logged
    live_blockers: list[str] = field(default_factory=list)   # a live run must refuse
    counts: dict[str, int] = field(default_factory=dict)     # file -> rows kept after de-duplication
    duplicates: dict[str, int] = field(default_factory=dict)
    malformed: dict[str, Malformed] = field(default_factory=dict)
    reached_last_page: dict[str, bool | None] = field(default_factory=dict)   # paged source -> flag
    stats: dict[str, int] = field(default_factory=dict)      # extra counters (links, joins, ...)
    fetched_at: datetime | None = None                       # OLDEST fetched_at seen
    stale: bool = False
    zoominfo_credits_used: int | None = None                 # None = run_meta did not say

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def can_go_live(self) -> bool:
        return not self.errors and not self.live_blockers

    @property
    def calls_reached_last_page(self) -> bool:
        return self.reached_last_page.get("nooks_calls") is True

    def error(self, msg: str) -> None:
        self.errors.append(scrub(msg))

    def warn(self, msg: str) -> None:
        self.warnings.append(scrub(msg))

    def block_live(self, msg: str) -> None:
        if msg not in self.live_blockers:
            self.live_blockers.append(scrub(msg))

    def bad_row(self, source: str, reason: str) -> None:
        self.malformed.setdefault(source, Malformed()).add(reason)

    def lines(self) -> list[str]:
        """Plain-text block for the run log. No raw contact details."""
        out = ["rows kept per file: " + (", ".join(f"{k}={v}" for k, v in sorted(self.counts.items())) or "none")]
        out.append(f"oldest fetched_at: {self.fetched_at.isoformat() if self.fetched_at else 'unknown'}"
                   f"{' (STALE)' if self.stale else ''}")
        out.append("reached_last_page: " + (", ".join(f"{k}={v}" for k, v in sorted(self.reached_last_page.items()))
                                            or "n/a"))
        out.append(f"zoominfo credits used: {'unknown' if self.zoominfo_credits_used is None else self.zoominfo_credits_used}")
        for k, v in sorted(self.duplicates.items()):
            out.append(f"duplicate rows dropped in {k}: {v}")
        for k, m in sorted(self.malformed.items()):
            out.append(f"malformed rows in {k}: {m.count}")
            out += [f"    e.g. {s}" for s in m.samples]
        for k, v in sorted(self.stats.items()):
            out.append(f"  {k}: {v}")
        out += [f"ERROR: {e}" for e in self.errors]
        out += [f"WARNING: {w}" for w in self.warnings]
        out += [f"LIVE BLOCKED: {b}" for b in self.live_blockers]
        return out


@dataclass
class RawData:
    """Raw rows (validated, de-duplicated) plus the models that can be built without accounts."""
    raw_dir: Path
    sf_accounts: list[dict] = field(default_factory=list)
    sf_contacts: list[dict] = field(default_factory=list)
    nooks_calls: list[dict] = field(default_factory=list)
    nooks_dispositions: list[dict] = field(default_factory=list)
    nooks_prospects: list[dict] = field(default_factory=list)
    nooks_accounts: list[dict] = field(default_factory=list)
    nooks_tasks: list[dict] = field(default_factory=list)
    zoominfo_scoops: list[dict] = field(default_factory=list)
    gmail_sent: list[dict] = field(default_factory=list)
    run_meta: dict = field(default_factory=dict)
    metas: dict[str, dict] = field(default_factory=dict)
    gmail_searched: set[str] = field(default_factory=set)       # salted hashes of the addresses actually searched
    gmail_salt: str = ""
    disposition_index: dict = field(default_factory=dict)
    calls: list[CallRecord] = field(default_factory=list)
    tasks: list[TaskRecord] = field(default_factory=list)
    sent_emails: list[SentEmail] = field(default_factory=list)
    scoops: list[Scoop] = field(default_factory=list)
    report: IntakeReport = field(default_factory=IntakeReport)


# ------------------------------------------------------------------ reading files
class IntakeError(ValueError):
    """A raw file could not be read as text. ``load_raw`` turns it into a report error (it never raises)."""


def decode_text(raw: bytes) -> str:
    """Bytes of a JSON file -> text. Accepts UTF-8 (with or without a BOM) and UTF-16 (BOM, or NUL bytes in the
    first bytes, as Windows tools write it). Anything else raises IntakeError (never UnicodeDecodeError)."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16")
        except UnicodeError as e:
            raise IntakeError("file has a UTF-16 BOM but is not valid UTF-16") from e
    if b"\x00" in raw[:8]:                  # BOM-less UTF-16 (NUL bytes are valid UTF-8, so test this first)
        for enc in ("utf-16-le", "utf-16-be"):
            try:
                text = raw.decode(enc)
            except UnicodeError:
                continue
            if text.lstrip()[:1] in ("{", "["):
                return text
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    raise IntakeError("file is not valid UTF-8 or UTF-16 text")


def _read_json(path: Path, rep: IntakeReport, label: str):
    """Parsed JSON or None (error recorded)."""
    try:
        return json.loads(decode_text(path.read_bytes()))
    except OSError as e:
        rep.error(f"{label}: cannot read {path.name} ({type(e).__name__})")
    except IntakeError as e:
        rep.error(f"{label}: {path.name}: {e}")
    except json.JSONDecodeError as e:
        rep.error(f"{label}: {path.name} is not valid JSON ({e.msg} at line {e.lineno})")
    except (ValueError, RecursionError) as e:
        rep.error(f"{label}: {path.name} cannot be parsed ({type(e).__name__})")
    return None


def _read_dump(raw_dir: Path, name: str, rep: IntakeReport) -> tuple[dict, list] | None:
    """One ``{"meta","rows"}`` file -> (meta, rows), or None if missing / unusable."""
    path = raw_dir / f"{name}.json"
    required, _paged = FILES[name]
    if not path.exists():
        if required:
            rep.error(f"required file missing: {name}.json")
        else:
            rep.warn(f"optional file missing: {name}.json")
        return None
    data = _read_json(path, rep, name)
    if data is None:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("meta"), dict) or not isinstance(data.get("rows"), list):
        rep.error(f"{name}.json must be an object with a 'meta' object and a 'rows' list")
        return None
    bad = [i for i, r in enumerate(data["rows"]) if not isinstance(r, dict)]
    if bad:
        rep.error(f"{name}.json: {len(bad)} rows are not objects (first at index {bad[0]})")
        return None
    return data["meta"], data["rows"]


def _dedupe(name: str, rows: list[dict], rep: IntakeReport) -> list[dict]:
    """Keep the first row per id. Rows with no id are kept here (the loader check counts them)."""
    key = ID_KEY.get(name, "id")
    # Sort by id (rows without an id last, file order kept among equals) so the file order never changes
    # which duplicate wins, which sample reasons are kept, or the order of anything downstream.
    rows = sorted(rows, key=lambda r: (r.get(key) in (None, ""), str(r.get(key) or "")))
    seen, out, dup = set(), [], 0
    for r in rows:
        rid = r.get(key)
        if rid in (None, ""):
            out.append(r)
            continue
        k = normalise_sf_id(str(rid)) or str(rid) if name.startswith("sf_") else str(rid)
        if k in seen:
            dup += 1
            continue
        seen.add(k)
        out.append(r)
    if dup:
        rep.duplicates[name] = dup
        rep.warn(f"{name}: {dup} duplicate rows (same {key}) dropped, first kept")
    return out


# ------------------------------------------------------------------ run_meta
def _check_run_meta(raw_dir: Path, rep: IntakeReport, data: RawData) -> dict:
    path = raw_dir / f"{RUN_META}.json"
    if not path.exists():
        rep.error("required file missing: run_meta.json")
        return {}
    rm = _read_json(path, rep, RUN_META)
    if rm is None:
        return {}
    if not isinstance(rm, dict):
        rep.error("run_meta.json must be a JSON object")
        return {}
    for key in ("fetched_at", "sf_user_id", "nooks_user_id", "row_counts"):
        if rm.get(key) in (None, "", {}):
            rep.error(f"run_meta.json is missing '{key}'")
    credits = rm.get("zoominfo_credits_used")
    if isinstance(credits, int) and not isinstance(credits, bool) and credits >= 0:
        rep.zoominfo_credits_used = credits
    else:
        rep.warn("run_meta.json: zoominfo_credits_used is missing or not a non-negative integer "
                 "(credits spent are UNKNOWN, rule 7)")
        rep.block_live("ZoomInfo credits used are unknown")
    for w in rm.get("warnings") or []:
        rep.warn(f"fetch step warning: {w}")
    if not isinstance(rm.get("row_counts", {}), dict):
        rep.error("run_meta.json: row_counts must be an object")
        rm["row_counts"] = {}
    if not isinstance(rm.get("reached_last_page", {}), dict):
        rep.error("run_meta.json: reached_last_page must be an object")
        rm["reached_last_page"] = {}
    return rm


# ------------------------------------------------------------------ the main entry point
def load_raw(raw_dir, *, now: datetime | None = None) -> RawData:
    """Read, validate and convert ``raw_dir``. Never raises for bad data: read ``RawData.report``.

    ``now`` (aware) is only used for the staleness check; default is the current UTC time."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    raw_dir = Path(raw_dir)
    data = RawData(raw_dir=raw_dir)
    rep = data.report
    if not raw_dir.is_dir():
        rep.error(f"raw directory not found: {raw_dir}")
        return data
    data.run_meta = rm = _check_run_meta(raw_dir, rep, data)
    stamps: list[datetime] = []
    rm_stamp = parse_utc(rm.get("fetched_at")) if rm else None
    if rm and rm.get("fetched_at") and rm_stamp is None:
        rep.error("run_meta.json: fetched_at is not an ISO timestamp")
    if rm_stamp:
        stamps.append(rm_stamp)

    counts_meta = rm.get("row_counts") or {}
    flags_meta = rm.get("reached_last_page") or {}
    for name, (required, paged) in FILES.items():
        got = _read_dump(raw_dir, name, rep)
        if got is None:
            continue
        meta, rows = got
        data.metas[name] = meta
        for k in META_KEYS:
            if k not in meta:
                rep.warn(f"{name}.json meta is missing '{k}'")
        stamp = parse_utc(meta.get("fetched_at"))
        if stamp is None:
            rep.warn(f"{name}.json: fetched_at missing or invalid (age unknown)")
            rep.block_live(f"{name}: fetch time unknown")
        else:
            stamps.append(stamp)
        # row count vs run_meta (BEFORE de-duplication: it counts what the fetch step wrote)
        if name in counts_meta:
            if counts_meta[name] != len(rows):
                rep.error(f"{name}: run_meta says {counts_meta[name]} rows but the file holds {len(rows)} "
                          "(truncated or edited file)")
        elif rm:
            rep.warn(f"run_meta.row_counts has no entry for {name}")
        # reached_last_page
        if paged:
            flag = meta.get("reached_last_page")
            if name in flags_meta and flags_meta[name] != flag:
                rep.warn(f"{name}: reached_last_page differs between run_meta ({flags_meta[name]!r}) and the "
                         f"file ({flag!r}); the cautious value (not reached) is used")
                flag = False if (flag is not True or flags_meta[name] is not True) else True
            if flag is not True:
                flag = False
                rep.warn(f"{name}: last page NOT reached (or not recorded): the data may be partial")
                if name in SAFETY_PAGED:
                    rep.block_live(f"{name}: paging did not reach the last page")
            rep.reached_last_page[name] = flag
        setattr(data, name, _dedupe(name, rows, rep))
        rep.counts[name] = len(getattr(data, name))

    if stamps:
        rep.fetched_at = min(stamps)
        if now - rep.fetched_at > STALE_AFTER:
            rep.stale = True
            rep.warn(f"raw data is stale: oldest fetch {rep.fetched_at.isoformat()} is more than 24h old")
            rep.block_live("raw data is older than 24 hours")
        if max(stamps) - now > FUTURE_SLACK:
            rep.warn("a fetched_at is in the future (clock problem?)")
            rep.block_live("a fetched_at is in the future")
    if not rep.ok:
        rep.block_live("intake has errors")
    _convert(data, now)
    _read_gmail_coverage(data)
    return data


# ------------------------------------------------------------------ conversion + row validation
def _convert(data: RawData, now: datetime) -> None:
    rep = data.report
    # dispositions first: calls need the index
    if data.nooks_dispositions:
        try:
            data.disposition_index = build_disposition_index(data.nooks_dispositions)
        except LoaderError as e:
            rep.error(f"nooks_dispositions: {e}")
        if not data.disposition_index and rep.ok:
            rep.error("nooks_dispositions has no usable rows")
    elif "nooks_dispositions" in data.metas:
        rep.error("nooks_dispositions has no rows (call outcomes cannot be resolved)")

    unresolved = 0
    for r in data.nooks_calls:
        try:
            c = call_from_nooks(r, data.disposition_index)
        except LoaderError as e:
            rep.bad_row("nooks_calls", str(e))
            continue
        if c.disposition_name is None:
            unresolved += 1
        data.calls.append(c)
    if unresolved:
        rep.stats["calls_with_unknown_disposition"] = unresolved
        rep.warn(f"nooks_calls: {unresolved} calls have a disposition id missing from listCallDispositions "
                 "(a Do Not Call among them could not be recognised)")
        rep.block_live("some calls have an unresolvable disposition")
    owner = (data.run_meta or {}).get("nooks_user_id")
    if owner:
        other = sum(1 for c in data.calls if c.owner_id and c.owner_id != owner)
        if other:
            rep.stats["calls_by_other_owner"] = other
            rep.warn(f"nooks_calls: {other} calls belong to an owner other than run_meta.nooks_user_id "
                     "(the owner filter is not a permission boundary: another rep's calls would mix in)")
            rep.block_live("some calls belong to an owner other than run_meta.nooks_user_id")

    for r in data.nooks_tasks:
        try:
            data.tasks.append(task_from_nooks(r))
        except LoaderError as e:
            rep.bad_row("nooks_tasks", str(e))
    for r in data.zoominfo_scoops:
        try:
            data.scoops.append(scoop_from_zoominfo(r))
        except LoaderError as e:
            rep.bad_row("zoominfo_scoops", str(e))
    for r in data.gmail_sent:                    # per thread, so one bad thread does not hide the rest
        try:
            data.sent_emails.extend(sent_emails_from_gmail({"threads": [r]}))
        except LoaderError as e:
            rep.bad_row("gmail_sent", str(e))
    # shape checks that mirror what the account / prospect builders will do (rows are kept either way:
    # accounts.build_account_list lists a malformed account row in its own dropped list)
    for r in data.sf_accounts:
        try:
            account_from_sf_row(r)
        except LoaderError as e:
            rep.bad_row("sf_accounts", str(e))
    for r in data.nooks_accounts:
        try:
            account_from_nooks(r)
        except LoaderError as e:
            rep.bad_row("nooks_accounts", str(e))
    for r in data.nooks_prospects:
        try:
            prospect_from_nooks(r)
        except LoaderError as e:
            rep.bad_row("nooks_prospects", str(e))
    for r in data.sf_contacts:
        if not normalise_sf_id(r.get("Id")):
            rep.bad_row("sf_contacts", "contact row has no Id")

    for name, m in rep.malformed.items():
        total = max(len(getattr(data, name)), 1)
        rep.warn(f"{name}: {m.count} of {total} rows are malformed and were not used")
        if m.count / total > HIGH_MALFORMED_RATE:
            rep.warn(f"{name}: more than 5% malformed: the real response shape may have changed")
    if rep.malformed.get("nooks_calls"):
        rep.block_live("some call rows are malformed (a Do Not Call could be hidden in them)")
    if rep.malformed.get("sf_contacts"):
        rep.block_live("some contact rows are malformed")

    if "gmail_sent" not in data.metas:
        rep.block_live("no sent-email history (gmail_sent.json missing)")
    else:
        days = gmail_window_days(data.metas["gmail_sent"])
        if days is None:
            rep.warn("gmail_sent.json: the search window is unknown (meta.window_days or newer_than:Nd in meta.query): "
                     "search results carry no dates, so the window IS the only date information")
            rep.block_live("sent-email search window unknown")
        else:
            rep.stats["gmail_window_days"] = days
    if "nooks_tasks" not in data.metas:
        rep.block_live("no Nooks task list (nooks_tasks.json missing): last-sheet memory from tagged tasks unavailable")
    srcs: dict[str, int] = {}
    for r in data.nooks_calls:
        k = str(r.get("source") or "unknown")
        srcs[k] = srcs.get(k, 0) + 1
    for k, v in srcs.items():
        rep.stats[f"calls_source_{k}"] = v
    if any(k != "nooks_sep" for k in srcs):
        rep.warn("nooks_calls include sources other than nooks_sep (" + ", ".join(sorted(srcs)) + "): Outreach / CSV / "
                 "Salesforce-sourced calls use other disposition sets with no 'Do Not Call', so Do Not Call is only "
                 "visible for Nooks-dialer calls")
    if not data.calls and data.nooks_calls == [] and "nooks_calls" in data.metas:
        if (data.run_meta or {}).get("empty_history_confirmed") is True:
            rep.warn("nooks_calls is empty and run_meta.empty_history_confirmed is true: treated as a brand-new seat")
        else:
            rep.warn("nooks_calls is empty: Do Not Call cannot be rebuilt from an empty history")
            rep.block_live("no call history")


DEFAULT_ADDRESS_SALT = "apac-callsheet-v1"


def address_hash(addr: str | None, salt: str = DEFAULT_ADDRESS_SALT) -> str | None:
    """Salted hash of an address in its comparison form (lower case, dots removed from the local part), so the
    Gmail coverage record never holds a raw address. 32 hex characters; None for an unusable address."""
    key = normalise_email_for_match(addr)
    return hashlib.sha256(f"{salt}:{key}".encode("utf-8")).hexdigest()[:32] if key else None


def _read_gmail_coverage(data: "RawData") -> None:
    """``gmail_sent.json`` meta may carry ``searched_address_hashes`` (list of ``address_hash`` values) and
    ``hash_salt``. Without them nothing counts as searched (the pipeline then reports every chosen prospect)."""
    meta = data.metas.get("gmail_sent")
    if meta is None:
        return
    salt = meta.get("hash_salt")
    data.gmail_salt = salt if isinstance(salt, str) and salt else DEFAULT_ADDRESS_SALT
    hashes = meta.get("searched_address_hashes")
    if hashes is None:
        data.report.warn("gmail_sent.json meta has no searched_address_hashes: Gmail coverage of the ranked "
                         "prospects cannot be shown (live needs full coverage)")
        return
    if not isinstance(hashes, list) or not all(isinstance(h, str) and h for h in hashes):
        data.report.error("gmail_sent.json: meta.searched_address_hashes must be a list of strings")
        return
    if any("@" in h for h in hashes):
        data.report.error("gmail_sent.json: meta.searched_address_hashes holds raw addresses (it must hold hashes only)")
        return
    data.gmail_searched = set(hashes)
    data.report.stats["gmail_addresses_searched"] = len(data.gmail_searched)


_WINDOW = re.compile(r"newer_than:(\d+)d\b")


def gmail_window_days(meta: dict) -> int | None:
    """Days covered by the Gmail sent search: ``meta.window_days`` (int) or ``newer_than:Nd`` in ``meta.query``.
    None if neither says. (Search results carry no dates: the window is the only date information.)"""
    w = meta.get("window_days")
    if isinstance(w, int) and not isinstance(w, bool) and w > 0:
        return w
    m = _WINDOW.search(str(meta.get("query") or ""))
    return int(m.group(1)) if m else None


# ------------------------------------------------------------------ joins that need accounts
def derive_nooks_sf_links(data: RawData) -> dict[str, str]:
    """Nooks account id -> canonical Salesforce account id, from the prospect + Contact join
    (listAccounts returns no crmId). A Nooks account whose prospects point at SEVERAL Salesforce
    accounts is ambiguous and left unlinked. Counts go into ``report.stats``."""
    contacts = {normalise_sf_id(c.get("Id")): c for c in data.sf_contacts if c.get("Id")}
    found: dict[str, set[str]] = {}
    for p in data.nooks_prospects:
        nk = (p.get("account") or {}).get("id")
        c = contacts.get(normalise_sf_id(p.get("crmId")))
        sf = normalise_sf_id((c or {}).get("AccountId"))
        if nk and sf:
            found.setdefault(nk, set()).add(sf)
    links = {nk: next(iter(s)) for nk, s in found.items() if len(s) == 1}
    ambiguous = sorted(nk for nk, s in found.items() if len(s) > 1)
    data.report.stats["nooks_accounts_linked_to_salesforce"] = len(links)
    if ambiguous:
        data.report.stats["nooks_accounts_link_ambiguous"] = len(ambiguous)
        data.report.warn(f"{len(ambiguous)} Nooks accounts have prospects on several Salesforce accounts: not linked")
    return links


def nooks_account_rows(data: RawData, links: dict[str, str]) -> list[dict]:
    """Nooks account rows with ``_sf_account_id`` added where the join found one (copies; the raw rows
    are never modified). A row that already carries ``_sf_account_id`` keeps it."""
    out = []
    for r in data.nooks_accounts:
        if not r.get("_sf_account_id") and r.get("id") in links:
            r = {**r, "_sf_account_id": links[r["id"]]}
        out.append(r)
    return out


def _contact_name(c: dict) -> str:
    n = " ".join(x for x in (c.get("FirstName"), c.get("LastName")) if x) or c.get("Name") or ""
    return n.strip() or "(no name)"


def prospect_from_contact(c: dict, account_country: str | None = None) -> Prospect:
    """A Prospect built from a Salesforce Contact alone (no Nooks prospect row): ``nooks_id`` is None until it
    is resolved. Phone prefers MobilePhone, then Phone; ``HasOptedOutOfEmail`` is the opt-out."""
    return Prospect(nooks_id=None, sf_contact_id=normalise_sf_id(c.get("Id")),
                    sf_account_id=normalise_sf_id(c.get("AccountId")), nooks_account_id=None,
                    name=_contact_name(c), title=c.get("Title"), email=c.get("Email"),
                    phone_e164=normalise_phone(c.get("MobilePhone"), account_country)
                    or normalise_phone(c.get("Phone"), account_country),
                    opted_out_email=bool(c.get("HasOptedOutOfEmail", False)))


def build_prospects(data: RawData, accounts_by_sf: dict, accounts_by_nooks: dict) -> list[Prospect]:
    """Prospects come from SALESFORCE CONTACTS (phone, opt-out, title, name, account). The Nooks prospect table
    is workspace-wide (~3M rows) and is never bulk-fetched: ``nooks_prospects.json`` holds only lazily resolved
    rows. A Contact with a matching Nooks row (``crmId``) gets that row's Nooks id (and name / title / email, as
    ``prospect_from_nooks`` reads them); every other Contact has ``nooks_id = None``. Nooks rows with no Contact
    row are kept too (no phone: they land in needs_prospect). Counts go into ``report.stats``.
    Malformed rows were already counted by ``load_raw`` (never skipped silently)."""
    rep = data.report
    nooks_by_crm: dict[str, dict] = {}
    dup_crm = 0
    for row in data.nooks_prospects:                      # already sorted by id: first wins, deterministically
        crm = normalise_sf_id(row.get("crmId"))
        if crm:
            if crm in nooks_by_crm:
                dup_crm += 1
            else:
                nooks_by_crm[crm] = row
    out: list[Prospect] = []
    used: set[str] = set()
    failed = 0
    for c in data.sf_contacts:
        cid = normalise_sf_id(c.get("Id"))
        if not cid:
            continue                                      # counted as malformed by load_raw
        acct = accounts_by_sf.get(normalise_sf_id(c.get("AccountId")))
        country = acct.country if acct else None
        row = nooks_by_crm.get(cid)
        try:
            if row is not None:
                out.append(prospect_from_nooks(row, sf_contact=c, account_country=country))
                used.add(cid)
            else:
                out.append(prospect_from_contact(c, country))
        except LoaderError:
            failed += 1
    unmatched_rows = 0
    for row in data.nooks_prospects:                      # Nooks rows whose Contact is not in sf_contacts
        crm = normalise_sf_id(row.get("crmId"))
        if crm and (crm in used or nooks_by_crm.get(crm) is not row):
            continue                                      # matched to a Contact, or a duplicate of a matched row
        acct = accounts_by_nooks.get((row.get("account") or {}).get("id"))
        try:
            out.append(prospect_from_nooks(row, sf_contact=None, account_country=acct.country if acct else None))
            unmatched_rows += 1
        except LoaderError:
            failed += 1
    contacts = [p for p in out if p.sf_contact_id]
    rep.stats["contacts_total"] = len(data.sf_contacts)
    rep.stats["contacts_with_usable_phone"] = sum(1 for p in out if p.sf_contact_id and p.phone_e164)
    rep.stats["contacts_opted_out_email"] = sum(1 for p in contacts if p.opted_out_email)
    # Sales_Opt_Out_Email__c: meaning UNVERIFIED, not modelled on Prospect: counted only (coordinator decides)
    rep.stats["contacts_sales_opt_out_email_unverified"] = sum(
        1 for c in data.sf_contacts if c.get("Sales_Opt_Out_Email__c") is True)
    rep.stats["prospects_nooks_id_resolved"] = sum(1 for p in out if p.nooks_id)
    rep.stats["prospects_nooks_id_unresolved"] = sum(1 for p in out if not p.nooks_id)
    rep.stats["nooks_rows_without_contact_row"] = unmatched_rows
    if dup_crm:
        rep.stats["nooks_prospects_duplicate_crm_id"] = dup_crm
        rep.warn(f"{dup_crm} Nooks prospect rows share a crmId with another row: the first (by id) is used")
    if failed:
        rep.stats["prospects_failed_in_join"] = failed
    if unmatched_rows:
        rep.warn(f"{unmatched_rows} Nooks prospects have no Salesforce Contact row in sf_contacts: "
                 "phone and email opt-out are unknown for them (they cannot be called)")
    return out


def infer_prospect_ids_from_calls(data: RawData, prospects: list[Prospect]) -> list[Prospect]:
    """Resolve Nooks prospect ids WITHOUT extra connector calls, conservatively: a Nooks prospect id seen on calls
    is attached to a Contact only when EVERY call carrying that id was dialled to one phone number, that number
    belongs to exactly ONE Contact, and the Contact has no Nooks id yet. Shared switchboards (several Contacts on
    one number) and conflicting numbers stay unresolved. Counts go into ``report.stats``. Returns a new list."""
    by_phone: dict[str, list[int]] = {}
    for i, p in enumerate(prospects):
        if p.phone_e164 and p.sf_contact_id:
            by_phone.setdefault(p.phone_e164, []).append(i)
    known = {p.nooks_id for p in prospects if p.nooks_id}
    phones_for: dict[str, set[str | None]] = {}
    for c in data.calls:
        if c.prospect_id and c.prospect_id not in known:
            phones_for.setdefault(c.prospect_id, set()).add(c.to_e164)
    out = list(prospects)
    taken: set[int] = set()
    inferred = ambiguous = 0
    for pid in sorted(phones_for):
        phones = phones_for[pid]
        if len(phones) != 1 or None in phones:
            ambiguous += 1
            continue
        hits = by_phone.get(next(iter(phones)), [])
        if len(hits) != 1 or hits[0] in taken or out[hits[0]].nooks_id:
            ambiguous += 1
            continue
        taken.add(hits[0])
        out[hits[0]] = replace(out[hits[0]], nooks_id=pid)
        inferred += 1
    data.report.stats["prospect_ids_inferred_from_call_phone"] = inferred
    data.report.stats["call_prospect_ids_left_unresolved"] = ambiguous
    data.report.stats["prospects_nooks_id_resolved"] = sum(1 for p in out if p.nooks_id)
    data.report.stats["prospects_nooks_id_unresolved"] = sum(1 for p in out if not p.nooks_id)
    return out


# ------------------------------------------------------------------ recent calls tied to accounts (cooldown memory)
RECENT_CALL_DAYS = 14                 # the longest cooldown (cooldown_days_after_connect)
MAX_UNTIED_RECENT_SHARE = 0.05        # live is blocked above this share of recent calls not tied to an account
ID_BATCH = 25                         # prospect ids per listProspects filter_id call


def _recent(data: RawData, now: datetime, days: int):
    cutoff = now - timedelta(days=days)
    return [c for c in data.calls if c.started_at >= cutoff]


def recent_call_prospect_ids(data: RawData, now: datetime, days: int = RECENT_CALL_DAYS) -> list[str]:
    """Distinct Nooks prospect ids on calls from the last ``days`` days (sorted): the ids whose crmId must be
    fetched (``listProspects filter_id=<uuids>``, batches of ``ID_BATCH``) so those calls can be tied to accounts."""
    return sorted({c.prospect_id for c in _recent(data, now, days) if c.prospect_id})


def id_batches(ids: list[str], size: int = ID_BATCH) -> list[list[str]]:
    return [ids[i:i + size] for i in range(0, len(ids), size)]


def untied_recent_calls(data: RawData, prospects: list[Prospect], now: datetime, days: int = RECENT_CALL_DAYS) -> dict:
    """How many of the last ``days`` days of calls can be tied to a Salesforce account (state records a call on an
    account, and so starts a cooldown, only then). A call with a prospect id is tied when a prospect carries that
    Nooks id AND an account; a call with no prospect id is tied by a phone number that belongs to exactly one
    prospect with an account. A call with neither a prospect id nor a usable number can never be tied: it is
    counted separately and NOT held against the threshold. Returns counts and the ids still to resolve."""
    by_id = {p.nooks_id: p for p in prospects if p.nooks_id}
    by_phone: dict[str, list[Prospect]] = {}
    for p in prospects:
        if p.phone_e164:
            by_phone.setdefault(p.phone_e164, []).append(p)
    total = untied = unlinkable = 0
    missing: set[str] = set()
    for c in _recent(data, now, days):
        if not c.prospect_id and not c.to_e164:
            unlinkable += 1
            continue
        total += 1
        if c.prospect_id:
            p = by_id.get(c.prospect_id)
            if p is None:
                missing.add(c.prospect_id)
        else:
            hits = by_phone.get(c.to_e164, [])
            p = hits[0] if len(hits) == 1 else None
        if p is None or not p.sf_account_id:
            untied += 1
    return {"total": total, "untied": untied, "unlinkable": unlinkable, "ids_to_resolve": sorted(missing),
            "share": (untied / total) if total else 0.0}


# ------------------------------------------------------------------ last sheet from our tagged tasks
SHEET_TAG = re.compile(r"\[apac-callsheet (\d{4}-\d{2}-\d{2}) #(\d+)(?: acct=([A-Za-z0-9]{15,18}))?\]")


def parse_sheet_tag(note) -> tuple[date, int, str | None] | None:
    """``[apac-callsheet 2026-11-25 #3]`` (optionally ``... #3 acct=<SF id>]``) in a task note ->
    (sheet date, rank, SF account id or None). Untagged or malformed notes give None (ignored)."""
    if not isinstance(note, str):
        return None
    best = None
    for m in SHEET_TAG.finditer(note):                    # several tags in one note: the NEWEST date wins
        try:
            d = date.fromisoformat(m.group(1))
        except ValueError:
            continue
        cand = (d, int(m.group(2)), normalise_sf_id(m.group(3)) if m.group(3) else None)
        if best is None or cand[0] > best[0]:
            best = cand
    return best


def last_sheet_from_tasks(data: RawData, prospects: list[Prospect], run_date: date) -> set[str]:
    """Account ids on the most recent sheet BEFORE ``run_date`` according to our tagged one-off tasks (any status):
    the second source of "never the same account twice in a row" besides state.json, which is gitignored and
    starts 'new' in every fresh container. Untagged tasks are ignored; tasks tagged ``run_date`` or later are
    ignored (same-night reruns stay stable). The account is the tag's ``acct=`` id when present, else the task's
    prospect resolved to a Contact. A tagged task whose account cannot be found is counted and BLOCKS live."""
    rep = data.report
    prospect_acct = {p.nooks_id: normalise_sf_id(p.sf_account_id) for p in prospects if p.nooks_id and p.sf_account_id}
    tagged = []
    for r in data.nooks_tasks:
        t = parse_sheet_tag(r.get("note"))
        if t:
            tagged.append((t, (r.get("prospect") or {}).get("id")))
    rep.stats["tagged_tasks"] = len(tagged)
    before = [x for x in tagged if x[0][0] < run_date]
    if not before:
        return set()
    last = max(x[0][0] for x in before)
    ids: set[str] = set()
    unresolved = 0
    for (d, _rank, acct), pid in before:
        if d != last:
            continue
        sf = acct or prospect_acct.get(pid)
        if sf:
            ids.add(sf)
        else:
            unresolved += 1
    rep.stats["last_sheet_date_from_tasks"] = int(last.strftime("%Y%m%d"))
    if unresolved:
        rep.stats["tagged_tasks_unresolved"] = unresolved
        rep.warn(f"{unresolved} tagged tasks from the last sheet ({last}) could not be tied to an account "
                 "(no acct= in the tag and the prospect is not resolved)")
        rep.block_live("tagged tasks from the last sheet could not be tied to accounts")
    return ids
