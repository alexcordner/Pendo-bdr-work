"""Pure functions: raw connector dicts (or synthetic fixture rows) -> models.

No network code. Live adapters fetch raw dicts and pass them here, so tests and
production share one parsing path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from src.config import COUNTRY_AU, COUNTRY_NZ, Config, canonical_country, resolve_timezone
from src.models import Account, CallRecord, Prospect, Scoop

_CALLING_CODE = {COUNTRY_AU: "61", COUNTRY_NZ: "64"}
_MIN_DIGITS, _MAX_DIGITS = 8, 15   # E.164 allows up to 15 digits


# ---------------------------------------------------------------- phones
def normalise_phone(raw: str | None, account_country: str | None = None) -> str | None:
    """Normalise a phone to E.164, or None if it cannot be done safely.

    Rules (docs/connector_notes.md 6d): keep a leading '+'; '00' prefix becomes '+';
    strip other non-digits; digits starting 61/64 and >= 11 long get '+'; a leading '0'
    takes the account country's calling code (AU +61, NZ +64); an explicit '+' is never
    overridden by the account country.
    """
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    if s.startswith("+"):
        out = digits
    elif digits.startswith("00"):
        out = digits[2:]
    elif digits.startswith(("61", "64")) and len(digits) >= 11:
        out = digits
    elif digits.startswith("0"):
        code = _CALLING_CODE.get(canonical_country(account_country) or "")
        if code is None:
            return None  # national format but no country to anchor it
        out = code + digits[1:]
    else:
        return None
    if not (_MIN_DIGITS <= len(out) <= _MAX_DIGITS):
        return None
    return "+" + out


# ---------------------------------------------------------------- accounts
@dataclass(frozen=True)
class CountryResult:
    country: str | None            # canonical ANZ country, or None
    non_anz_billing_anz_hq: bool   # billing country present and non-ANZ, but HQ is ANZ


def resolve_country(billing_country_raw: str | None, hq_country_raw: str | None) -> CountryResult:
    """Country rule (decision 4): BillingCountry OR HQ_Country__c is AU/NZ, case-insensitive.

    Billing wins when it is ANZ. If only HQ is ANZ the account is in scope with the HQ
    country; it is flagged when billing was a non-ANZ value (decision 5). A null billing
    country with ANZ HQ is not flagged.
    """
    billing = canonical_country(billing_country_raw)
    if billing:
        return CountryResult(billing, False)
    hq = canonical_country(hq_country_raw)
    if hq:
        has_other_billing = bool(billing_country_raw and billing_country_raw.strip())
        return CountryResult(hq, has_other_billing)
    return CountryResult(None, False)


def _int_or_none(v) -> int | None:
    try:
        return None if v is None else int(float(v))
    except (TypeError, ValueError):
        return None


def account_from_sf_row(row: dict, config: Config | None = None) -> Account:
    """Salesforce Account SOQL row -> Account.

    Every row becomes an Account (including customers and non-ANZ); filtering is task 1d.
    ``account.country`` is None when the country rule fails. With ``config`` the timezone
    is resolved via the fallback chain; the reason (and any guess) is appended to ``notes``.
    """
    cr = resolve_country(row.get("BillingCountry"), row.get("HQ_Country__c"))
    acct = Account(
        sf_account_id=row.get("Id"),
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
        prospecting_priority=row.get("SDR_Prospecting_Priority__c") or row.get("Prospecting_Priority__c"),
    )
    if cr.non_anz_billing_anz_hq:
        acct.notes.append(f"non-ANZ billing ({row.get('BillingCountry')!r}) but ANZ HQ ({row.get('HQ_Country__c')!r})")
    if cr.country is None:
        acct.notes.append("not ANZ by BillingCountry or HQ_Country__c")
    if config is not None:
        tz = resolve_timezone(config, cr.country, acct.billing_state, acct.billing_city, acct.hq_state)
        acct.timezone, acct.timezone_guessed = tz.tz, tz.guessed
        acct.notes.append(f"timezone: {tz.tz} ({tz.source}); {tz.reason}")
    return acct


# ---------------------------------------------------------------- prospects
def prospect_from_nooks(row: dict, sf_contact: dict | None = None,
                        account_country: str | None = None) -> Prospect:
    """Nooks prospect row (+ optional matching SF Contact row) -> Prospect.

    Nooks gives identity/title/email; SF Contact gives phone (MobilePhone preferred, then
    Phone) and HasOptedOutOfEmail. Pass the account's country so national numbers normalise.
    ``do_not_call`` is left False: it comes from call history (task 1c).
    """
    c = sf_contact or {}
    phone = normalise_phone(c.get("MobilePhone"), account_country) or normalise_phone(c.get("Phone"), account_country)
    acct = row.get("account") or {}
    return Prospect(
        nooks_id=row.get("id"),
        sf_contact_id=row.get("crmId") or c.get("Id"),
        sf_account_id=c.get("AccountId"),
        nooks_account_id=acct.get("id"),
        name=row.get("name") or " ".join(x for x in (row.get("firstName"), row.get("lastName")) if x),
        title=row.get("title") or c.get("Title"),
        email=row.get("primaryEmail") or c.get("Email"),
        phone_e164=phone,
        opted_out_email=bool(c.get("HasOptedOutOfEmail", False)),
    )


# ---------------------------------------------------------------- calls
@dataclass(frozen=True)
class Disposition:
    id: str
    name: str
    call_outcome: str | None
    current: bool          # source == 'nooks_sep' and not archived


def build_disposition_index(rows: list[dict]) -> dict[str, Disposition]:
    """listCallDispositions rows -> {disposition id: Disposition}. Never hard-code ids."""
    return {
        r["id"]: Disposition(r["id"], r["name"], r.get("callOutcome"),
                             r.get("source") == "nooks_sep" and not r.get("archived", False))
        for r in rows
    }


def disposition_id_for_name(index: dict[str, Disposition], name: str) -> str | None:
    """Reverse lookup by exact name among current dispositions (for writers); None if absent."""
    for d in index.values():
        if d.name == name and d.current:
            return d.id
    return None


def parse_utc(value: str | None) -> datetime | None:
    """ISO-8601 ('...Z' or offset) -> aware UTC datetime; None if missing/invalid."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def call_from_nooks(row: dict, dispositions: dict[str, Disposition]) -> CallRecord:
    """Nooks listCalls row -> CallRecord, resolving the disposition NAME via the index.

    Unknown/missing disposition gives ``disposition_name=None`` (the id is kept). ``to`` is
    normalised with no country hint (Nooks numbers carry '+').
    """
    did = (row.get("callDisposition") or {}).get("id")
    d = dispositions.get(did) if did else None
    started = parse_utc(row.get("time")) or parse_utc(row.get("createdAt"))
    if started is None:
        raise ValueError(f"call {row.get('id')!r} has no usable time")
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


# ---------------------------------------------------------------- scoops
def scoop_from_zoominfo(row: dict) -> Scoop:
    """ZoomInfo search_scoops row (``{id, type, attributes{...}}``) -> Scoop."""
    a = row.get("attributes", row)
    company = a.get("company") or {}
    published = parse_utc(a.get("publishedDate") or a.get("originalPublishedDate"))
    if published is None:
        raise ValueError(f"scoop {a.get('id')!r} has no published date")
    return Scoop(
        id=str(a.get("id") or row.get("id")),
        company_id=str(company.get("id", "")),
        company_name=company.get("name", ""),
        description=a.get("description", ""),
        published_date=published,
        types=tuple(t["type"] for t in a.get("types") or []),
        topics=tuple(t["topic"] for t in a.get("topics") or []),
        link=a.get("link"),
    )
