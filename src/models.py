"""Shared data models for the APAC call sheet.

Plain stdlib dataclasses, no behaviour beyond tiny helpers. Field names follow the
real connector field names in docs/connector_notes.md where one exists
(e.g. ``sf_account_id`` = Salesforce ``Account.Id``).

Conventions every module must follow:
  * All datetimes are timezone-aware (UTC for stored/source times; the prospect's
    local zone for ``RankedAccount.local_call_time``).
  * Phones are E.164 strings (``+61400000001``) or None.
  * Country is canonical: ``"Australia"``, ``"New Zealand"`` or None (not ANZ / unknown).
  * Missing data is None (or an empty list), never a made-up default.
  * Mutable: Account, RankedAccount, SheetEntry. Frozen: everything else. To change a frozen
    object use ``dataclasses.replace(obj, field=value)`` (e.g. 1c sets ``do_not_call`` that way).

Join keys:
  * Prospect.sf_contact_id == Nooks prospect.crmId; Prospect.sf_account_id == Account.sf_account_id.
  * Prospect.nooks_account_id == Account.nooks_account_id (the only link for Nooks-only prospects).
  * Scoop.company_id == Account.zoominfo_id.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


@dataclass
class Account:
    """One company from Salesforce (the book), optionally enriched from Nooks/ZoomInfo.

    Mutable on purpose: later steps append to ``notes`` and set ``source_*`` flags
    while merging sources (task 1d).
    """

    sf_account_id: str | None            # Salesforce Account.Id (None for ZoomInfo-only accounts)
    name: str
    website: str | None = None           # bare host as stored, e.g. "www.example.com"
    country_raw: str | None = None       # BillingCountry exactly as stored
    country: str | None = None           # canonical AU/NZ country, None if not ANZ
    billing_state: str | None = None     # BillingState as stored (may be abbreviation / junk)
    billing_city: str | None = None
    hq_state: str | None = None          # HQ_State_Province__c
    hq_country_raw: str | None = None    # HQ_Country__c as stored
    employees: int | None = None         # ZoomInfo_Employee_Count__c (decision 11)
    is_customer: bool = False            # Current_Customer__c
    is_sdr_target: bool = False          # SDR_Target__c (ranking boost, not a filter)
    zoominfo_id: str | None = None       # DOZISF__ZoomInfo_Id__c
    nooks_account_id: str | None = None  # Nooks account id (not SF), when known
    # True when BillingCountry is non-ANZ but HQ_Country__c is ANZ (decision 5: flag, don't silently include)
    non_anz_billing_anz_hq: bool = False
    # Which sources contributed this account.
    source_salesforce: bool = False
    source_nooks: bool = False
    source_zoominfo: bool = False
    timezone: str | None = None          # resolved IANA name, None if unresolvable
    timezone_guessed: bool = False       # True when it came from the country default fallback
    days_since_last_activity: int | None = None
    prospecting_priority: str | None = None   # SDR_Prospecting_Priority__c (optional tie-breaker)
    notes: list[str] = field(default_factory=list)   # human-readable audit trail (why dropped, guesses...)
    # Review-driven (1d): machine-readable flags for Alex to review (e.g. 'noise_location',
    # 'country_unknown', 'possible_duplicate', 'flagged_non_anz_billing_anz_hq'). Accounts that come
    # from Alex's own Salesforce/Nooks book are FLAGGED, never dropped, for noise-type reasons.
    flags: list[str] = field(default_factory=list)
    confidence: str = "normal"           # "normal" | "low" (flagged, zoominfo-only, nooks-only ...)

    @property
    def size_unknown(self) -> bool:
        """True when headcount is missing. Such accounts are KEPT and flagged (note 'size_unknown'),
        never silently dropped; the ranker gives them no icp_fit points."""
        return self.employees is None


@dataclass(frozen=True)
class Prospect:
    """A person to call. Built from a Nooks prospect, optionally joined to an SF Contact."""

    nooks_id: str | None
    sf_contact_id: str | None            # Nooks prospect.crmId = SF Contact.Id (None if not a SF prospect)
    sf_account_id: str | None            # Contact.AccountId (the account link)
    nooks_account_id: str | None         # Nooks prospect.account.id
    name: str
    title: str | None = None
    email: str | None = None             # primaryEmail / Contact.Email (real dotted form)
    phone_e164: str | None = None        # Contact.MobilePhone, else Phone, normalised
    opted_out_email: bool = False        # Contact.HasOptedOutOfEmail
    # Never-contact flag. The loader cannot know this from a prospect row (no read flag exists);
    # task 1c sets it from a past 'Do Not Call' call disposition. Default False.
    do_not_call: bool = False


@dataclass(frozen=True)
class CallRecord:
    """One logged call (Nooks listCalls row) with the disposition NAME resolved."""

    nooks_id: str
    prospect_id: str | None              # None for ~1 in 5 calls logged outside the dialer
    disposition_name: str | None         # resolved by name, None if missing/unknown id
    duration_s: float
    started_at: datetime                 # call ``time`` (UTC, aware)
    to_e164: str | None                  # normalised ``to``; fallback key when prospect_id is None
    disposition_id: str | None = None    # kept for traceability; never compare against it
    call_outcome: str | None = None      # 'No Answer' | 'Connect' | 'Meeting' from the dispositions list
    owner_id: str | None = None


@dataclass(frozen=True)
class Scoop:
    """One ZoomInfo scoop (search_scoops row, flattened)."""

    id: str
    company_id: str                      # ZoomInfo company id (joins to Account.zoominfo_id)
    company_name: str
    description: str
    published_date: datetime             # aware UTC
    types: tuple[str, ...] = ()          # e.g. ("New Hire", "Executive Move")
    topics: tuple[str, ...] = ()         # e.g. ("Customer Experience",)
    link: str | None = None


class SignalKind(str, Enum):
    """Fixed set of trigger kinds (``none`` = no real trigger; say so, never invent one)."""

    FUNDING = "funding"
    EXEC_MOVE = "exec_move"
    NEW_HIRE = "new_hire"
    MA = "ma"
    PRODUCT_LAUNCH = "product_launch"
    PARTNERSHIP = "partnership"
    PROJECT_PAIN = "project_pain"
    HIRING_PLAN = "hiring_plan"
    INTENT = "intent"                    # ZoomInfo intent topic (UNVERIFIED connector shape, capped strength)
    NONE = "none"


@dataclass(frozen=True)
class TaskRecord:
    """A Nooks task (listTasks row). Existing tasks are never modified by this project."""

    nooks_id: str
    action: str                          # 'call', 'manual_email', ...
    status: str                          # pending / ready / finished / skipped / failed
    completed: bool
    due_at: datetime | None
    prospect_id: str | None
    sequence_id: str | None              # None for one-off tasks
    email_id: str | None = None          # set on manual_email tasks
    owner_id: str | None = None

    @property
    def is_manual_email(self) -> bool:
        """Old manual_email backlog tasks: NEVER touch (decision 7)."""
        return self.action == "manual_email"

    @property
    def in_sequence(self) -> bool:
        return self.sequence_id is not None


@dataclass(frozen=True)
class SentEmail:
    """One (message, recipient) pair from a Gmail sent-mail search (metadata view).

    Gmail strips dots from the local part of returned addresses, so compare with
    ``recipient_key`` (see loaders.normalise_email_for_match), never with ``recipient``.
    """

    message_id: str
    thread_id: str | None
    recipient: str                       # as returned (dots stripped)
    recipient_key: str                   # normalised match key
    sent_at: datetime | None             # None if the search view carries no date (UNVERIFIED which key it uses)


@dataclass(frozen=True)
class Signal:
    """A real, sourced reason to call an account now. Never invented (CLAUDE.md rule 6)."""

    sf_account_id: str | None            # account link; if None, join on zoominfo_id (sf_account_id wins when both set)
    kind: SignalKind
    strength: float                      # 0.0-1.0; the ranker multiplies by the trigger_strength weight
    source: str                          # e.g. "zoominfo_scoop:FAKE-SCOOP-1"
    source_date: datetime | None         # when the underlying event was published
    evidence: str                        # the text the opener may quote (e.g. scoop description)
    zoominfo_id: str | None = None

    def __post_init__(self):
        if not isinstance(self.kind, SignalKind):
            raise ValueError(f"Signal.kind must be a SignalKind, got {self.kind!r}")
        if not 0.0 <= self.strength <= 1.0:
            raise ValueError(f"Signal.strength must be within 0.0-1.0, got {self.strength}")


@dataclass
class RankedAccount:
    """Output of the ranker for one account."""

    account: Account
    score: float = 0.0                   # 0-100
    score_breakdown: dict[str, float] = field(default_factory=dict)  # weight name -> points
    window: str | None = None            # call window name from config ("morning" / "afternoon")
    # Suggested dial time: an aware datetime in Account.timezone, falling inside ``window`` on the
    # next working day (see config.next_working_windows). Default is the window start; the ranker
    # should push it later inside the window when ``account.timezone_guessed``. None if unscheduled.
    local_call_time: datetime | None = None
    reasons: list[str] = field(default_factory=list)
    skipped_reason: str | None = None    # set when the account is excluded; score is then irrelevant
    signals: list[Signal] = field(default_factory=list)


@dataclass
class SheetEntry:
    """One line of the call sheet. Opener fields stay empty until the opener step (Phase 2d)."""

    ranked: RankedAccount
    prospect: Prospect | None = None
    opener: str = ""
    fact_1: str = ""
    fact_2: str = ""
    gatekeeper: str = ""
    trigger: str = ""
