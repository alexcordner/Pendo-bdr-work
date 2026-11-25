"""Pure functions: raw connector dicts (or synthetic fixture rows) -> models.

No network code. Live adapters fetch raw dicts and pass them here, so tests and production
share one parsing path.

Errors: every loader that takes a connector row raises ``LoaderError`` (one clear message naming the
function) on a malformed row, never a raw KeyError/AttributeError. Callers should catch
``LoaderError``, log the skip (CLAUDE.md rule 5) and carry on. ``normalise_phone``,
``normalise_email_for_match``, ``resolve_country`` and ``parse_utc`` never raise: bad input -> None.
"""
from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from src.config import (COUNTRY_AU, COUNTRY_NZ, Config, canonical_country, is_blank_or_junk,
                        resolve_timezone)
from src.models import Account, CallRecord, Prospect, Scoop, SentEmail, TaskRecord


class LoaderError(ValueError):
    """A connector row did not have the expected shape."""


def _guard(fn):
    """Convert shape errors inside a loader into LoaderError."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except LoaderError:
            raise
        except (KeyError, AttributeError, TypeError, ValueError, IndexError) as e:
            raise LoaderError(f"{fn.__name__}: malformed row ({type(e).__name__}: {e})") from e
    return wrapper


# ---------------------------------------------------------------- phones
_CALLING_CODE = {COUNTRY_AU: "61", COUNTRY_NZ: "64"}
# (digits after the country code incl. no trunk 0: allowed lengths, allowed first digits)
_NUMBER_PLAN = {"61": ((9,), "23478"), "64": ((8, 9, 10), "23456789")}
_EXT = re.compile(r"(?i)(;|#|\bext\b|(?<![a-z])x(?=\s*\d))")


def _valid_national(cc: str, rest: str) -> bool:
    lengths, firsts = _NUMBER_PLAN[cc]
    return len(rest) in lengths and rest[0] in firsts


def normalise_phone(raw: str | None, account_country: str | None = None) -> str | None:
    """Normalise a phone to E.164, or None when anything is doubtful. Never raises.

    Rules: a leading '+' is kept and never overridden by the account country; '00' and the AU
    international prefix '0011' become '+'; a trunk '0' after +61/+64 ('+61 (0)4...') is dropped;
    extensions ('ext 12', 'x123', ';ext=1', '#1') are cut off; digits starting 61/64 and >= 11 long
    get '+'; a leading '0' takes the account country's code (AU +61, NZ +64). +61 and +64 numbers
    are length/first-digit checked (AU 9 digits after +61; NZ 8-10 after +64); other countries need
    8-15 digits with no leading 0. All-zero numbers, '+0...' and multi-number strings give None.
    """
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if s.lower().startswith("tel:"):
        s = s[4:]
    s = _EXT.split(s, maxsplit=1)[0].strip()          # drop extension and anything after it
    digits = re.sub(r"[^0-9]", "", s)                  # ASCII digits only
    if not digits:
        return None

    if s.startswith("+"):
        intl = digits
    elif digits.startswith("0011"):
        intl = digits[4:]
    elif digits.startswith("00"):
        intl = digits[2:]
    elif digits.startswith(("61", "64")) and len(digits) >= 11:
        intl = digits
    elif digits.startswith("0"):
        code = _CALLING_CODE.get(canonical_country(account_country) or "")
        if code is None:
            return None                                # national format, no country to anchor it
        intl = code + digits[1:]
    else:
        return None

    if not intl or intl[0] == "0":
        return None                                    # country codes never start with 0
    cc = intl[:2]
    if cc in _NUMBER_PLAN:
        rest = intl[2:]
        if rest.startswith("0"):
            rest = rest[1:]                            # '+61 (0)4...' trunk zero
        if not rest or not _valid_national(cc, rest):
            return None
        return "+" + cc + rest
    return "+" + intl if 8 <= len(intl) <= 15 else None


def normalise_email_for_match(addr: str | None) -> str | None:
    """Comparison key for an email: lower-case, trimmed, dots removed from the LOCAL part only.

    Gmail returns addresses with local-part dots stripped, so ``first.last@x.com`` and
    ``firstlast@x.com`` must compare equal. Use for matching only; keep the real dotted address
    for querying and display. Returns None for non-emails.
    """
    if not addr or not isinstance(addr, str) or addr.count("@") != 1:
        return None
    local, domain = addr.strip().lower().split("@")
    local = local.replace(".", "")
    return f"{local}@{domain}" if local and domain else None


# ---------------------------------------------------------------- accounts
@dataclass(frozen=True)
class CountryResult:
    country: str | None            # canonical ANZ country, or None
    non_anz_billing_anz_hq: bool   # billing country is a real non-ANZ value, but HQ is ANZ


def resolve_country(billing_country_raw, hq_country_raw) -> CountryResult:
    """Country rule (decision 4): BillingCountry OR HQ_Country__c is AU/NZ, case-insensitive.

    Billing wins when ANZ. If only HQ is ANZ the account is in scope with the HQ country, flagged
    when billing held a real non-ANZ value (decision 5). Null/blank/junk ('N/A') billing is not a
    flag. Never raises.
    """
    billing = canonical_country(billing_country_raw)
    if billing:
        return CountryResult(billing, False)
    hq = canonical_country(hq_country_raw)
    if hq:
        return CountryResult(hq, not is_blank_or_junk(billing_country_raw))
    return CountryResult(None, False)


def _int_or_none(v) -> int | None:
    try:
        return None if v is None else int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return None


@_guard
def account_from_sf_row(row: dict, config: Config | None = None) -> Account:
    """Salesforce Account SOQL row -> Account. Raises LoaderError if ``Id`` is missing.

    Every row becomes an Account (customers and non-ANZ too); filtering is task 1d.
    ``country`` is None when the country rule fails. With ``config`` the timezone is resolved via
    the fallback chain and the reason is appended to ``notes``. For the 'non-ANZ billing, ANZ HQ'
    group the billing state/city belong to a foreign address and are IGNORED: the chain starts at
    HQ state (a US 'WA' must never become Perth). Missing headcount adds the note 'size_unknown'.
    """
    cr = resolve_country(row.get("BillingCountry"), row.get("HQ_Country__c"))
    acct = Account(
        sf_account_id=row["Id"],
        name=row.get("Name") or "",
        website=row.get("Website"),
        country_raw=row.get("BillingCountry"),
        country=cr.country,
        billing_state=row.get("BillingState"),
        billing_city=row.get("BillingCity"),
        hq_state=row.get("HQ_State_Province__c"),
        hq_country_raw=row.get("HQ_Country__c"),
        employees=_int_or_none(row.get("ZoomInfo_Employee_Count__c")),
        is_customer=bool(row.get("Current_Customer__c")),
        is_sdr_target=bool(row.get("SDR_Target__c")),
        zoominfo_id=row.get("DOZISF__ZoomInfo_Id__c") or None,
        non_anz_billing_anz_hq=cr.non_anz_billing_anz_hq,
        source_salesforce=True,
        days_since_last_activity=_int_or_none(row.get("Days_Since_Last_Activity__c")),
        # SDR_Prospecting_Priority__c first, falling back to Prospecting_Priority__c.
        prospecting_priority=row.get("SDR_Prospecting_Priority__c") or row.get("Prospecting_Priority__c"),
    )
    if cr.non_anz_billing_anz_hq:
        acct.notes.append(f"non-ANZ billing ({row.get('BillingCountry')!r}) but ANZ HQ ({row.get('HQ_Country__c')!r})")
    if cr.country is None:
        acct.notes.append("not ANZ by BillingCountry or HQ_Country__c")
    if acct.employees is None:
        acct.notes.append("size_unknown")
    if config is not None:
        foreign = cr.non_anz_billing_anz_hq
        tz = resolve_timezone(config, cr.country,
                              None if foreign else acct.billing_state,
                              None if foreign else acct.billing_city,
                              acct.hq_state)
        acct.timezone, acct.timezone_guessed = tz.tz, tz.guessed
        acct.notes.append(f"timezone: {tz.tz} ({tz.source}); {tz.reason}"
                          + ("; billing address ignored (non-ANZ)" if foreign else ""))
    return acct


@_guard
def account_from_nooks(row: dict, sf_account_id: str | None = None) -> Account:
    """Nooks account row -> Account (source_nooks=True, nooks_account_id set).

    Nooks rows carry no country/state and no SF id on read: pass ``sf_account_id`` when the row was
    fetched with ``listAccounts filter_crmId=<SF id>`` so 1d can merge it into the SF account.
    Domain becomes ``website``; ``numEmployees`` becomes ``employees``.
    """
    acct = Account(
        sf_account_id=sf_account_id,
        name=row.get("name") or "",
        website=row.get("domain"),
        employees=_int_or_none(row.get("numEmployees")),
        nooks_account_id=row["id"],
        source_nooks=True,
    )
    acct.notes.append("nooks account: no country/state available")
    if acct.employees is None:
        acct.notes.append("size_unknown")
    return acct


@_guard
def account_from_zoominfo(row: dict) -> Account:
    """ZoomInfo search_companies row (``{id, type, attributes:{name}}``, or flat ``{id, name}``)
    -> Account (source_zoominfo=True, zoominfo_id and name only; free search returns nothing else)."""
    name = (row.get("attributes") or row).get("name") or ""
    acct = Account(sf_account_id=None, name=name, zoominfo_id=str(row["id"]), source_zoominfo=True)
    acct.notes.append("zoominfo search: id and name only (no country, size or domain)")
    acct.notes.append("size_unknown")
    return acct


# ---------------------------------------------------------------- prospects
@_guard
def prospect_from_nooks(row: dict, sf_contact: dict | None = None, account_country: str | None = None,
                        sf_account_id: str | None = None) -> Prospect:
    """Nooks prospect row (+ optional matching SF Contact row) -> Prospect.

    Nooks gives identity/title/email and ``account.id`` (a Nooks id). SF Contact gives phone
    (MobilePhone, then Phone), HasOptedOutOfEmail and ``AccountId``. Pass the account's country so
    national numbers normalise. ``do_not_call`` is left False: it comes from call history (1c).

    Prospect -> Account join path (see ``match_account``):
      1. SF-linked prospect: ``sf_account_id`` (from Contact.AccountId, or the argument) ==
         ``Account.sf_account_id``.
      2. Nooks-only prospect (e.g. CSV, ``crmId`` null): ``nooks_account_id`` ==
         ``Account.nooks_account_id``, which is set by ``account_from_nooks``.
    """
    c = sf_contact or {}
    phone = normalise_phone(c.get("MobilePhone"), account_country) or normalise_phone(c.get("Phone"), account_country)
    acct = row.get("account") or {}
    return Prospect(
        nooks_id=row["id"],
        sf_contact_id=row.get("crmId") or c.get("Id"),
        sf_account_id=c.get("AccountId") or sf_account_id,
        nooks_account_id=acct.get("id"),
        name=row.get("name") or " ".join(x for x in (row.get("firstName"), row.get("lastName")) if x),
        title=row.get("title") or c.get("Title"),
        email=row.get("primaryEmail") or c.get("Email"),
        phone_e164=phone,
        opted_out_email=bool(c.get("HasOptedOutOfEmail", False)),
    )


def match_account(prospect: Prospect, accounts) -> Account | None:
    """Find a prospect's Account: by sf_account_id first, then by nooks_account_id. None if no match.

    O(n); build your own dicts for bulk joins using the same two keys.
    """
    if prospect.sf_account_id:
        for a in accounts:
            if a.sf_account_id == prospect.sf_account_id:
                return a
    if prospect.nooks_account_id:
        for a in accounts:
            if a.nooks_account_id == prospect.nooks_account_id:
                return a
    return None


# ---------------------------------------------------------------- calls
@dataclass(frozen=True)
class Disposition:
    id: str
    name: str
    call_outcome: str | None
    current: bool          # source == 'nooks_sep' and not archived


def _norm_name(s: str) -> str:
    return " ".join(str(s).lower().split())


@_guard
def build_disposition_index(rows: list[dict]) -> dict[str, Disposition]:
    """listCallDispositions rows -> {disposition id: Disposition}. Never hard-code ids."""
    return {
        r["id"]: Disposition(r["id"], r["name"], r.get("callOutcome"),
                             r.get("source") == "nooks_sep" and not r.get("archived", False))
        for r in rows
    }


def disposition_id_for_name(index: dict[str, Disposition], name: str) -> str | None:
    """Reverse lookup by name (case/whitespace-insensitive), current dispositions only; None if absent."""
    want = _norm_name(name)
    for d in index.values():
        if d.current and _norm_name(d.name) == want:
            return d.id
    return None


def parse_utc(value) -> datetime | None:
    """ISO-8601 ('...Z' or offset) -> aware UTC datetime; naive is taken as UTC. None if missing/invalid."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


@_guard
def call_from_nooks(row: dict, dispositions: dict[str, Disposition]) -> CallRecord:
    """Nooks listCalls row -> CallRecord, resolving the disposition NAME via the index.

    Unknown/missing disposition gives ``disposition_name=None`` (the id is kept). ``to`` is
    normalised with no country hint (Nooks numbers carry '+'). Raises LoaderError on a missing id,
    no usable time, or a non-numeric duration.
    """
    did = (row.get("callDisposition") or {}).get("id")
    d = dispositions.get(did) if did else None
    started = parse_utc(row.get("time")) or parse_utc(row.get("createdAt"))
    if started is None:
        raise LoaderError(f"call_from_nooks: call {row.get('id')!r} has no usable time")
    return CallRecord(
        nooks_id=row["id"],
        prospect_id=(row.get("prospect") or {}).get("id"),
        disposition_name=d.name if d else None,
        duration_s=float(row.get("duration") or 0),
        started_at=started,
        to_e164=normalise_phone(row.get("to")),
        disposition_id=did,
        call_outcome=d.call_outcome if d else None,
        owner_id=(row.get("owner") or {}).get("id"),
    )


# ---------------------------------------------------------------- tasks
@_guard
def task_from_nooks(row: dict) -> TaskRecord:
    """Nooks listTasks row -> TaskRecord. ``is_manual_email`` marks the old backlog (never touch)."""
    return TaskRecord(
        nooks_id=row["id"],
        action=row.get("action") or "",
        status=row.get("status") or "",
        completed=bool(row.get("completed")),
        due_at=parse_utc(row.get("dueAt")),
        prospect_id=(row.get("prospect") or {}).get("id"),
        sequence_id=(row.get("sequence") or {}).get("id"),
        email_id=(row.get("email") or {}).get("id"),
        owner_id=(row.get("owner") or {}).get("id"),
    )


# ---------------------------------------------------------------- sent mail
def _sent_at(msg: dict) -> datetime | None:
    """Best-effort sent time. Key names are UNVERIFIED against the live connector, so try several:
    ``internalDate`` (epoch ms), then ``date`` / ``sentAt`` / ``sent_at`` (ISO). None if absent."""
    ms = msg.get("internalDate")
    if ms is not None:
        try:
            return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    for k in ("date", "sentAt", "sent_at"):
        dt = parse_utc(msg.get(k))
        if dt:
            return dt
    return None


@_guard
def sent_emails_from_gmail(result: dict) -> list[SentEmail]:
    """Gmail ``search_threads`` result (metadata view) -> one SentEmail per (message, recipient).

    Compare with ``SentEmail.recipient_key == normalise_email_for_match(real_address)``.
    Only toRecipients are read (cc/bcc are not a 'mail to the prospect').
    """
    out: list[SentEmail] = []
    for t in result.get("threads") or []:
        for m in t.get("messages") or []:
            for rcpt in m.get("toRecipients") or []:
                key = normalise_email_for_match(rcpt)
                if key:
                    out.append(SentEmail(m["id"], m.get("threadId") or t.get("id"), rcpt, key, _sent_at(m)))
    return out


# ---------------------------------------------------------------- scoops
@_guard
def scoop_from_zoominfo(row: dict) -> Scoop:
    """ZoomInfo search_scoops row (``{id, type, attributes{...}}``) -> Scoop. LoaderError if malformed
    (no published date, or a type/topic entry without its key)."""
    a = row.get("attributes", row)
    company = a.get("company") or {}
    published = parse_utc(a.get("publishedDate") or a.get("originalPublishedDate"))
    if published is None:
        raise LoaderError(f"scoop_from_zoominfo: scoop {a.get('id')!r} has no published date")
    return Scoop(
        id=str(a.get("id") or row["id"]),
        company_id=str(company.get("id", "")),
        company_name=company.get("name", ""),
        description=a.get("description", ""),
        published_date=published,
        types=tuple(t["type"] for t in a.get("types") or []),
        topics=tuple(t["topic"] for t in a.get("topics") or []),
        link=a.get("link"),
    )
