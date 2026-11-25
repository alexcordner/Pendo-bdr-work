"""Integration tests, task 1g (offline, synthetic). First part: world builder + pipeline (support code): a SYNTHETIC world builder and the
end-to-end pipeline wired the way task 2c (callsheet.py) will wire it. Fully offline.

Everything here is invented: names ("Alpha Digital Pty Ltd"), ids (``001FAKE0000nnnn`` is a 15-char
fake Salesforce id, plus ``AAA`` is its 18-char form), phones (``+614...`` placeholders), emails
(``example.com``). No connector is called, no real data is read.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from src import accounts as acc_mod
from src import ranker, signals as sig_mod, state as st_mod
from src.config import Config, load_config
from src.loaders import (build_disposition_index, call_from_nooks, normalise_sf_id, prospect_from_nooks,
                         scoop_from_zoominfo, sent_emails_from_gmail)

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
UTC = timezone.utc
NOW = datetime(2026, 11, 25, 3, 0, tzinfo=UTC)          # Wed 14:00 AEDT
SYDNEY = ZoneInfo("Australia/Sydney")


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- ids
def sf15(n: int) -> str:
    return f"001FAKE0000{n:04d}"           # 15 chars


def sf18(n: int) -> str:
    return sf15(n) + "AAA"                 # 18 chars: same record, longer form


def con15(n: int, j: int) -> str:
    return f"003FAKE{n:04d}{j:04d}"        # 15 chars


def fixed_style(style: str):
    """Id style callback: always 15 or always 18 chars."""
    return (lambda n, where: sf15(n)) if style == "15" else (lambda n, where: sf18(n))


def mixed_style(seed: int = 1):
    """Different spelling per occurrence (sf row / contact / nooks link ...), seeded."""
    rng = random.Random(seed)
    return lambda n, where: sf15(n) if rng.random() < 0.5 else sf18(n)


# ---------------------------------------------------------------- world
@dataclass
class World:
    sf_rows: list = field(default_factory=list)
    nooks_accounts: list = field(default_factory=list)
    zoominfo_rows: list = field(default_factory=list)
    contacts: list = field(default_factory=list)
    nooks_prospects: list = field(default_factory=list)
    calls: list = field(default_factory=list)
    scoops: list = field(default_factory=list)
    sent_threads: list = field(default_factory=list)
    dispositions: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)         # name -> expectation notes for the tests

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("meta", "sf_rows", "nooks_accounts", "zoominfo_rows", "contacts",
                                              "nooks_prospects", "calls", "scoops", "sent_threads", "dispositions")}

    @staticmethod
    def from_dict(d: dict) -> "World":
        return World(**{k: d[k] for k in d})


def load_dispositions() -> list:
    with open(FIX / "nooks_call_dispositions.json", encoding="utf-8") as f:
        return json.load(f)["data"]


class Builder:
    """Adds accounts, prospects, calls and scoops to a World. Every id is fake."""

    def __init__(self, idfn=None, now: datetime = NOW):
        self.w = World(dispositions=load_dispositions())
        self.idfn = idfn or fixed_style("15")
        self.now = now
        self._call_n = 0
        self._scoop_n = 0
        self._phone_n = 0

    # -- accounts
    def account(self, n: int, name: str, *, country="Australia", state=None, city=None, hq_country=None,
                hq_state=None, emp=215, customer=False, target=False, zi=True, nooks=False, nooks_link=True,
                sf=True, prio=None) -> int:
        w = self.w
        zid = f"FAKE-ZI-{n:04d}" if zi else None
        if sf:
            w.sf_rows.append({
                "Id": self.idfn(n, "sf"), "Name": name, "Website": f"www.fake{n}.example.com", "Type": "Prospect",
                "BillingCountry": country, "BillingState": state, "BillingCity": city,
                "HQ_Country__c": hq_country if hq_country is not None else country,
                "HQ_State_Province__c": hq_state, "ZoomInfo_Employee_Count__c": emp, "ROE_count__c": emp,
                "Current_Customer__c": customer, "SDR_Target__c": target, "SDR_Prospecting_Priority__c": prio,
                "Prospecting_Priority__c": None, "DOZISF__ZoomInfo_Id__c": zid, "Days_Since_Last_Activity__c": 40,
            })
        if nooks:
            row = {"id": f"FAKE-NK-ACCT-{n:04d}", "name": name, "domain": f"fake{n}.example.com",
                   "numEmployees": emp}
            if sf and nooks_link:
                row["_sf_account_id"] = self.idfn(n, "nooks_link")
            w.nooks_accounts.append(row)
        if not sf and zi:
            w.zoominfo_rows.append({"id": zid, "type": "Company", "attributes": {"name": name}})
        elif sf and zi:
            w.zoominfo_rows.append({"id": zid, "type": "Company", "attributes": {"name": name}})
        return n

    def phone(self, nz=False) -> str:
        self._phone_n += 1
        return f"+64200{self._phone_n:06d}" if nz else f"+614{self._phone_n:08d}"

    # -- prospects
    def prospect(self, n: int, j: int, title: str, *, name=None, phone="auto", email="auto", nz=False,
                 opted_out=False, nooks=True, sf_link=True) -> dict:
        w = self.w
        pid = f"FAKE-NK-P-{n:04d}-{j}"
        cid = con15(n, j)
        first = not w.nooks_prospects
        if phone == "auto":
            phone = self.phone(nz)
        if email == "auto":
            email = f"person{n}.{j}@fake{n}.example.com"
        name = name or f"Person {n}-{j}"
        w.contacts.append({"Id": cid, "AccountId": self.idfn(n, "contact") if sf_link else None, "Phone": None,
                           "MobilePhone": phone, "HasOptedOutOfEmail": opted_out, "Title": title, "Email": email})
        w.nooks_prospects.append({"id": pid, "type": "salesforce", "name": name, "firstName": name.split()[0],
                                  "lastName": name.split()[-1], "title": title, "primaryEmail": email,
                                  "crmId": cid, "account": {"id": f"FAKE-NK-ACCT-{n:04d}"}})
        p = {"id": pid, "phone": phone, "email": email, "contact": cid}
        if first and phone:      # a real seat always has history; an EMPTY history cannot be marked 'rebuilt'
            self.call(p, "Left Voicemail", 400, duration=25)
        return p

    # -- calls
    def call(self, prospect: dict, disposition: str, days_ago: float, duration=45, *, unlinked=False, to=None):
        did = {"No Answer": "disp-no-answer", "Left Voicemail": "disp-voicemail", "Do Not Call": "disp-do-not-call",
               "Gatekeeper": "disp-gatekeeper", "Objection: Not Interested": "disp-objection-not-interested",
               "Meeting Scheduled": "disp-meeting"}[disposition]
        self._call_n += 1
        t = self.now - timedelta(days=days_ago)
        self.w.calls.append({
            "id": f"FAKE-CALL-{self._call_n:06d}", "source": "nooks_sep", "duration": duration,
            "to": to or prospect["phone"], "direction": "outgoing",
            "prospect": None if unlinked else {"id": prospect["id"]},
            "callDisposition": {"id": did}, "owner": {"id": "FAKE-NOOKS-USER-ALEX"},
            "time": _iso(t), "createdAt": _iso(t)})

    # -- scoops
    def scoop(self, n: int, typ: str, desc: str, days_ago: float, topics=("Product Management",), company=None):
        self._scoop_n += 1
        sid = f"FAKE-SCOOP-{self._scoop_n:05d}"
        t = self.now - timedelta(days=days_ago)
        self.w.scoops.append({"id": sid, "type": "Scoop", "attributes": {
            "id": sid, "company": {"id": company or f"FAKE-ZI-{n:04d}", "name": f"Co {n}"}, "description": desc,
            "publishedDate": _iso(t), "originalPublishedDate": _iso(t),
            "topics": [{"id": f"T{i}", "topic": x} for i, x in enumerate(topics)],
            "types": [{"id": "Y1", "type": typ}]}})

    def email_sent(self, prospect: dict, mid: str, days_ago: float | None = 1):
        dotless = prospect["email"].replace(".", "", prospect["email"].split("@")[0].count("."))
        local, dom = prospect["email"].split("@")
        msg = {"id": mid, "threadId": "FAKE-THR-" + mid, "toRecipients": [local.replace(".", "") + "@" + dom]}
        if days_ago is not None:
            msg["internalDate"] = int((self.now - timedelta(days=days_ago)).timestamp() * 1000)
        self.w.sent_threads.append({"id": "FAKE-THR-" + mid, "messages": [msg]})


# ---------------------------------------------------------------- named world
HEAD_PROD = "Head of Product"


def named_world(idfn=None) -> World:
    """Hand-built scenarios, one account each, with the expectation recorded in ``meta``."""
    b = Builder(idfn)
    m = b.w.meta
    exec_move = "{co} has appointed a new Head of Product to lead its digital product team."

    # 1 Sydney top account: strong exec move 3 days ago, SDR target
    b.account(1, "Alpha Digital Pty Ltd", state="New South Wales", emp=400, target=True, nooks=True)
    b.prospect(1, 1, HEAD_PROD)
    b.scoop(1, "Executive Move", exec_move.format(co="Alpha"), 3, topics=("Product Management",))
    m["top_sydney"] = 1
    # 2 Perth, funding 10 days
    b.account(2, "Bravo Software Pty Ltd", state="Western Australia", emp=120, nooks=True)
    b.prospect(2, 1, "VP Product")
    b.scoop(2, "Funding", "Bravo has raised $12 million in Series B funding.", 10, topics=("Funding",))
    # 3 Auckland, product launch 5 days
    b.account(3, "Charlie Cloud Ltd", country="New Zealand", emp=80, nooks=True)
    b.prospect(3, 1, "Head of Digital", nz=True)
    b.scoop(3, "Product Launch", "Charlie launched a new mobile banking app for customers.", 5,
            topics=("Product Launch",))
    # 4 Brisbane, no scoop at all
    b.account(4, "Delta Apps Pty Ltd", state="Queensland", emp=300)
    b.prospect(4, 1, HEAD_PROD)
    m["no_signal"] = 4
    # 5 DNC: best-scoring account, but its Head of Product is Do Not Call; a weaker secondary is callable
    b.account(5, "Echo DNC Holdings Pty Ltd", state="New South Wales", emp=900, target=True)
    e1 = b.prospect(5, 1, HEAD_PROD, name="Dana Dnc")
    b.prospect(5, 2, "Product Manager", name="Pat Callable")
    b.scoop(5, "Executive Move", exec_move.format(co="Echo"), 1, topics=("Product Management",))
    b.call(e1, "Do Not Call", 200, duration=30)
    m["dnc_by_history"] = 5
    # 6 DNC via duplicate Nooks prospect: same email (different dots/case), different id and phone
    b.account(6, "Foxtrot Duplicate Pty Ltd", state="Victoria", emp=500, target=True)
    f1 = b.prospect(6, 1, HEAD_PROD, name="Fran One", email="fran.one@fake6.example.com")
    b.prospect(6, 2, HEAD_PROD, name="Fran Dup", email="Fran.One@fake6.example.com")
    b.scoop(6, "Executive Move", exec_move.format(co="Foxtrot"), 2, topics=("Product Management",))
    b.call(f1, "Do Not Call", 150, duration=20)
    m["dnc_by_dup_email"] = 6
    # 7 DNC via shared phone: G1 DNC on a switchboard number, G2 sits on that number, G3 has own phone
    b.account(7, "Golf Switchboard Pty Ltd", state="New South Wales", emp=250)
    board = b.phone()
    g1 = b.prospect(7, 1, "CTO", name="Gus Dnc", phone=board)
    b.prospect(7, 2, HEAD_PROD, name="Gail Shared", phone=board)
    b.prospect(7, 3, "Product Manager", name="Gil Own")
    b.scoop(7, "Funding", "Golf raised $8 million in funding.", 4, topics=("Funding",))
    b.call(g1, "Do Not Call", 90, duration=25)
    m["dnc_by_phone"] = 7
    # 8 touched 2 days ago (no answer): in cooldown until Thu 26 Nov, dropped by accounts
    b.account(8, "Hotel Recent Pty Ltd", state="New South Wales", emp=300)
    h1 = b.prospect(8, 1, HEAD_PROD)
    b.scoop(8, "Funding", "Hotel raised $20 million in funding.", 2, topics=("Funding",))
    b.call(h1, "No Answer", 2.1, duration=35)
    m["touched_2d"] = 8
    # 9 old connect (20 days ago): cooldown over, callable
    b.account(9, "India Old Connect Pty Ltd", state="New South Wales", emp=150)
    i1 = b.prospect(9, 1, "Director of Product")
    b.scoop(9, "Funding", "India raised $5 million in funding.", 6, topics=("Funding",))
    b.call(i1, "Objection: Not Interested", 20, duration=120)
    # 10 on last night's sheet: hard skip
    b.account(10, "Juliet Lastsheet Pty Ltd", state="New South Wales", emp=600)
    b.prospect(10, 1, HEAD_PROD)
    b.scoop(10, "Executive Move", exec_move.format(co="Juliet"), 1, topics=("Product Management",))
    m["last_sheet"] = 10
    # 11 needs prospect: no prospects at all
    b.account(11, "Kilo Noprospect Pty Ltd", state="New South Wales", emp=200)
    b.scoop(11, "Funding", "Kilo raised $9 million in funding.", 3, topics=("Funding",))
    m["needs_no_prospects"] = 11
    # 12 needs prospect: persona matches but no phone
    b.account(12, "Lima Nophone Pty Ltd", state="Victoria", emp=200)
    b.prospect(12, 1, HEAD_PROD, phone=None)
    m["needs_no_phone"] = 12
    # 13 Adelaide, small, no signal
    b.account(13, "Mike Adelaide Pty Ltd", state="South Australia", emp=60)
    b.prospect(13, 1, "Head of Customer Success")
    # 14 flagged: US billing, AU HQ (Victoria)
    b.account(14, "November Flagged Pty Ltd", country="United States", state="Washington", hq_country="Australia",
              hq_state="Victoria", emp=250)
    b.prospect(14, 1, HEAD_PROD)
    b.scoop(14, "Product Launch", "November launched a new customer app.", 4, topics=("Product Launch",))
    m["flagged"] = 14
    # 15 ZoomInfo-only company with a strong scoop (no SF, no Nooks): cannot be called
    b.account(15, "Oscar Analytics Pty Ltd", sf=False, zi=True)
    b.scoop(15, "Executive Move", exec_move.format(co="Oscar"), 2, topics=("Product Management",))
    m["zi_only"] = 15
    # 16 Nooks-only account with a prospect (no SF row, no ZoomInfo id)
    b.account(16, "Papa Nooksonly Pty Ltd", sf=False, zi=False, nooks=True)
    b.prospect(16, 1, HEAD_PROD, sf_link=False)
    m["nooks_only"] = 16
    # 17 noise-looking accounts: in Alex's book (kept, flagged) and ZoomInfo-only (dropped)
    b.account(17, "Sydney CBD", state="New South Wales", emp=100)
    b.prospect(17, 1, HEAD_PROD)
    b.account(18, "Melbourne CBD", sf=False, zi=True)
    m["noise_in_book"] = 17
    m["noise_zi_only"] = 18
    # 19 unknown size
    b.account(19, "Romeo Nosize Pty Ltd", state="New South Wales", emp=None)
    b.prospect(19, 1, HEAD_PROD)
    b.scoop(19, "Funding", "Romeo raised $3 million in funding.", 5, topics=("Funding",))
    m["size_unknown"] = 19
    # 20-23 drops
    b.account(20, "Sierra Customer Pty Ltd", state="New South Wales", emp=300, customer=True)
    b.account(21, "Tango Giant Pty Ltd", state="New South Wales", emp=5000)
    b.account(22, "Uniform Tiny Pty Ltd", state="New South Wales", emp=5)
    # 23 junk country: kept, flagged, no timezone -> skipped with a reason
    b.account(23, "Victor Unknowncountry Pty Ltd", country="Oz", hq_country="Oz", emp=100)
    # 24 only prospect opted out of email
    b.account(24, "Whiskey Optout Pty Ltd", state="New South Wales", emp=100)
    b.prospect(24, 1, HEAD_PROD, opted_out=True)
    # 25 Brisbane + prospect but only a no-persona title
    b.account(25, "Xray Nopersona Pty Ltd", state="Queensland", emp=100)
    b.prospect(25, 1, "Receptionist")
    # 26 Darwin (no DST, +9:30) strong
    b.account(26, "Yankee Darwin Pty Ltd", state="Northern Territory", emp=150)
    b.prospect(26, 1, HEAD_PROD)
    b.scoop(26, "Executive Move", exec_move.format(co="Yankee"), 5, topics=("Product Management",))
    # 27 Brisbane no DST, strong
    b.account(27, "Zulu Brisbane Pty Ltd", state="Queensland", emp=150, nooks=True)
    b.prospect(27, 1, HEAD_PROD)
    b.prospect(27, 2, "Product Manager", sf_link=False)      # Nooks-only prospect: joins by nooks account id
    b.scoop(27, "Funding", "Zulu raised $4 million in funding.", 6, topics=("Funding",))
    # 28 sent-email history (voicemail pair path): emailed 1 day ago
    b.account(28, "Alpha Two Pty Ltd", state="Victoria", emp=300)
    a28 = b.prospect(28, 1, HEAD_PROD, email="amy.mailed@fake28.example.com")
    b.email_sent(a28, "FAKE-MSG-28", 1)
    # 29 same as 28 but with call history: no-answer 10 days ago (cooldown over) AND emailed 1 day ago
    b.account(29, "Bravo Two Pty Ltd", state="Victoria", emp=300)
    a29 = b.prospect(29, 1, HEAD_PROD, email="ben.mailed@fake29.example.com")
    b.call(a29, "No Answer", 10, duration=30)
    b.email_sent(a29, "FAKE-MSG-29", 1)
    # scoop noise
    b.scoop(4, "Hiring Plans", "Delta has more open roles in Medical & Health.", 3, topics=("Customer Experience",))
    b.scoop(13, "Facilities", "Mike opened a new warehouse.", 3, topics=("Real Estate",))
    b.scoop(2, "Funding", "Bravo raised funding long ago.", 200, topics=("Funding",))            # expired
    b.scoop(99, "Funding", "Nobody Pty Ltd raised funding.", 3, topics=("Funding",), company="FAKE-ZI-9999")
    # malformed rows (must be counted, never crash)
    b.w.sf_rows.append({"Name": "No Id Pty Ltd", "BillingCountry": "Australia"})
    b.w.scoops.append({"id": "FAKE-SCOOP-BAD", "type": "Scoop", "attributes": {"id": "FAKE-SCOOP-BAD",
                       "company": {"id": "FAKE-ZI-0001"}, "description": "no date", "types": [], "topics": []}})
    # expectations
    m["sheet_last_night"] = [b.idfn(10, "sheet")]
    return b.w


# ---------------------------------------------------------------- pipeline
@dataclass
class Out:
    cfg: Config
    accounts: "acc_mod.AccountListResult"
    result: "ranker.RankResult"
    state_before: "st_mod.State"
    state_after: "st_mod.State"
    prospects: list
    signals: dict
    verdicts: list
    unmatched_scoops: list
    scoop_errors: list
    log: str
    cooldown: dict
    timings: dict
    n_prospects_blocked: int = 0
    run_date: date | None = None
    today: date | None = None


def _prospect_objects(world: World, cfg: Config, accounts_by_sf: dict, accounts_by_nooks: dict) -> list:
    contacts = {normalise_sf_id(c["Id"]): c for c in world.contacts}
    out = []
    for row in world.nooks_prospects:
        c = contacts.get(normalise_sf_id(row.get("crmId")))
        acct = None
        if c and normalise_sf_id(c.get("AccountId")) in accounts_by_sf:
            acct = accounts_by_sf[normalise_sf_id(c["AccountId"])]
        elif (row.get("account") or {}).get("id") in accounts_by_nooks:
            acct = accounts_by_nooks[row["account"]["id"]]
        out.append(prospect_from_nooks(row, sf_contact=c, account_country=acct.country if acct else None))
    return out


def run_pipeline(world: World, state_dir, now: datetime = NOW, cfg: Config | None = None, *, live: bool = True,
                 today: date | None = None, restyle=None, blocked=frozenset(), extra_touched=None,
                 call_page: int = 7, mark_sheet: bool = True, pass_touched: bool = True,
                 call_date="auto") -> Out:
    """The nightly run exactly as 2c will do it (offline). ``restyle(key)`` re-spells every SF-id key
    handed to accounts / ranker (mixed 15/18 char test). ``today`` is the date given to accounts."""
    import time
    cfg = cfg or load_config(ROOT / "config" / "config.yaml")
    restyle = restyle or (lambda k: k)
    t = {}
    t0 = time.perf_counter()
    run_date = now.astimezone(SYDNEY).date()
    today = today or run_date
    if call_date == "auto":       # 2c contract: the first date a task could be due (Sydney, first usable window)
        earliest = now.replace(second=0, microsecond=0) + timedelta(minutes=2)
        call_date = cfg_mod.next_working_windows(cfg, "Australia/Sydney", earliest)[0].start.date()

    # 1. state, dispositions
    st = st_mod.load_state(state_dir)
    state_before = st
    idx = build_disposition_index(world.dispositions)
    missing = st_mod.verify_dispositions(cfg, idx)
    if missing:
        raise RuntimeError(f"dispositions missing: {missing}")

    # 2. accounts, first pass (no touch info yet) so prospects get a country and calls an account timezone
    first = acc_mod.build_account_list(world.sf_rows, world.nooks_accounts, world.zoominfo_rows, cfg, {}, today)
    by_sf = {a.sf_account_id: a for a in first.accounts if a.sf_account_id}
    by_nooks = {a.nooks_account_id: a for a in first.accounts if a.nooks_account_id}
    prospects = _prospect_objects(world, cfg, by_sf, by_nooks)
    t["intake"] = time.perf_counter() - t0

    # 3. calls: loaders -> record_calls (pages) -> DNC rebuild marker
    t1 = time.perf_counter()
    calls, call_errors = [], 0
    from src.loaders import LoaderError
    for r in world.calls:
        try:
            calls.append(call_from_nooks(r, idx))
        except LoaderError:
            call_errors += 1
    if not st_mod.dnc_is_rebuilt(st):
        total = 0
        for i in range(0, len(calls), call_page):
            st = st_mod.record_calls(st, calls[i:i + call_page], prospects, by_sf, config=cfg)
            total += st.report["record_calls"]["calls_seen"]
        st = st_mod.mark_dnc_rebuilt(st, run_date, total, reached_last_page=True)
    else:
        st = st_mod.record_calls(st, calls, prospects, by_sf, config=cfg)
    sent = []
    for r in world.sent_threads:
        sent.extend(sent_emails_from_gmail({"threads": [r]}))
    st = st_mod.record_sent_emails(st, sent, prospects, by_sf, today=run_date)
    reports = dict(st.report)                   # `report` is NOT persisted: keep it for the run log
    st_mod.save_state(st, state_dir)
    st = st_mod.load_state(state_dir)           # prove the round trip: everything below uses the reloaded state
    st.report.update(reports)
    if live:
        st_mod.require_healthy(st, live=True)
    else:
        st_mod.require_healthy(st, live=False)
    t["state"] = time.perf_counter() - t1

    # 4. do-not-call applied to prospects BEFORE ranking
    t2 = time.perf_counter()
    marked = st_mod.mark_do_not_call(prospects, st)
    n_blocked = sum(1 for a, b in zip(prospects, marked) if b.do_not_call and not a.do_not_call)

    # 5. accounts, final pass with touch info from state
    touched = {restyle(k): v for k, v in st_mod.last_touched(st).items()} if pass_touched else {}
    if extra_touched:
        touched.update(extra_touched)
    cu_sf = {restyle(k): v for k, v in st_mod.cooldown_until(st, cfg).items()}
    # touched is ignored when cooldown_until is passed (phase1/1d-fixes M1), so an explicit extra
    # touch is expressed as the cooldown it would start.
    for k, v in (extra_touched or {}).items():
        cu_sf[k] = v + timedelta(days=cfg.limits.cooldown_days_no_answer)
    res = acc_mod.build_account_list(world.sf_rows, world.nooks_accounts, world.zoominfo_rows, cfg, touched, today,
                                     blocked_account_ids={restyle(b) for b in blocked}, cooldown_until=cu_sf,
                                     call_date=call_date)
    t["accounts"] = time.perf_counter() - t2

    # 6. signals
    t3 = time.perf_counter()
    scoops, scoop_errors = [], []
    for r in world.scoops:
        try:
            scoops.append(scoop_from_zoominfo(r))
        except LoaderError as e:
            scoop_errors.append(str(e))
    verdicts = sig_mod.filter_scoops(scoops, cfg, today)
    by_zi = acc_mod.group_by_zoominfo_id(res.accounts)
    batch = sig_mod.scoops_to_signals(verdicts, by_zi, cfg, today)
    signals: dict = {}
    for s in batch.signals:
        key = restyle(s.sf_account_id) if s.sf_account_id else s.zoominfo_id
        signals.setdefault(key, []).append(s)
    t["signals"] = time.perf_counter() - t3

    # 7. rank
    t4 = time.perf_counter()
    cooldown = dict(cu_sf)
    for kind in ("nooks_account_id", "zoominfo_id"):
        cooldown.update(st_mod.cooldown_until_by(st, cfg, kind))
    last_ids = {restyle(x) for x in st_mod.last_sheet_ids(st, run_date)}
    result = ranker.rank_accounts(res.accounts, signals, marked, cooldown, last_ids, cfg, now)
    t["rank"] = time.perf_counter() - t4

    # 8. sheet history: ranked ONLY
    after = st
    if mark_sheet:
        after = st_mod.record_sheet(st, run_date, [r.account.sf_account_id for r in result.ranked])
        st_mod.save_state(after, state_dir)
    t["total"] = time.perf_counter() - t0

    out = Out(cfg, res, result, state_before, after, marked, signals, verdicts, batch.unmatched, scoop_errors, "",
              cooldown, t, n_blocked, run_date, today)
    out.log = render_log(out, now)
    return out


def render_log(out: Out, now: datetime) -> str:
    """The run log the later callsheet task will write (logs/YYYY-MM-DD_HHMM_<run>.log)."""
    st = out.state_after
    lines = [f"RUN LOG (offline harness, synthetic data) now={now.isoformat()} run_date={out.run_date} "
             f"accounts_today={out.today}",
             "", "== STATE ==",
             f"state: new={st.is_new} reset={st.was_reset} dnc_rebuilt_at={st.dnc_rebuilt_at} "
             f"dnc_rebuilt_calls={st.dnc_rebuilt_calls}",
             f"record_calls: {json.dumps(out.state_after.report.get('record_calls', {}), sort_keys=True)}",
             f"record_sent_emails: {json.dumps(out.state_after.report.get('record_sent_emails', {}), sort_keys=True)}",
             f"prospects marked do-not-call from state: {out.n_prospects_blocked}",
             "", "== SIGNALS =="]
    for v in out.verdicts:
        lines.append(f"  {v.scoop.id} ({v.scoop.company_name}): {sig_mod.verdict_summary(v)}")
    for e in out.scoop_errors:
        lines.append(f"  scoop skipped (malformed): {e}")
    lines.append(f"  kept scoops with no matching account: {len(out.unmatched_scoops)}")
    lines += ["", "== ACCOUNTS ==", acc_mod.summarise(out.accounts), "", "== RANKER ==", ranker.summarise(out.result)]
    return "\n".join(lines)


def summary_dict(out: Out) -> dict:
    """Everything observable about a run, as plain JSON-able data (for determinism / id-style checks)."""
    r = out.result

    def ra(x):
        return {"key": ranker.account_key(x.account), "name": x.account.name, "score": x.score,
                "breakdown": x.score_breakdown, "window": x.window,
                "time": x.local_call_time.isoformat() if x.local_call_time else None, "reasons": x.reasons,
                "skipped": x.skipped_reason, "signals": [(s.kind.value, s.strength, s.source) for s in x.signals]}
    return {
        "ranked": [ra(x) for x in r.ranked], "spilled": [ra(x) for x in r.spilled],
        "needs": [ra(x) for x in r.needs_prospect], "skipped": [ra(x) for x in r.skipped],
        "codes": dict(sorted(r.needs_prospect_codes.items())),
        "prospects": {k: [p.nooks_id for p in v] for k, v in sorted(r.prospects.items())},
        "by_window": {k: [ranker.account_key(x.account) for x in v] for k, v in r.by_window.items()},
        "dropped": [(d.name, d.reason, d.source, d.detail) for d in out.accounts.dropped],
        "stats": dict(sorted(out.accounts.stats.items())),
        "accounts": [(a.name, a.timezone, a.confidence, tuple(sorted(a.flags))) for a in out.accounts.accounts],
        "sheet": out.state_after.sheets,
        "summaries": [acc_mod.summarise(out.accounts), ranker.summarise(r)],
    }


# ---------------------------------------------------------------- scale world
_ADJ = ["Alpha", "Brave", "Cobalt", "Delta", "Ember", "Fable", "Garnet", "Harbor", "Indigo", "Juniper", "Krypton",
        "Lumen", "Meridian", "Nimbus", "Onyx", "Pioneer", "Quartz", "Raven", "Summit", "Tundra"]
_NOUN = ["Software", "Logistics", "Health", "Finance", "Retail", "Energy", "Media", "Systems", "Mining", "Travel",
         "Education", "Insurance", "Foods", "Networks", "Labs"]
_STATES = [("New South Wales", 30), ("Victoria", 22), ("Queensland", 12), ("Western Australia", 8),
           ("South Australia", 5), ("Tasmania", 2), ("Australian Capital Territory", 3), ("Northern Territory", 1)]
_TITLES = ["Head of Product", "VP Product", "Chief Product Officer", "Director of Product", "Head of Digital",
           "Head of Customer Experience", "Product Manager", "CTO", "Head of IT", "Receptionist", "Office Manager",
           "Accountant", "Head of Engineering", "Sales Executive", None]
_DISPO = ["No Answer"] * 6 + ["Left Voicemail"] * 3 + ["Objection: Not Interested", "Meeting Scheduled", "Gatekeeper"]


def scale_world(n: int = 3000, seed: int = 7, now: datetime = NOW, idfn=None) -> World:
    """n synthetic accounts with random but seeded data: ~15% NZ, a few flagged / noise / customers, 0-3
    prospects each, calls on ~15% of prospects (1% Do Not Call), scoops on ~30% of accounts."""
    rng = random.Random(seed)
    b = Builder(idfn, now)
    for n_ in range(1, n + 1):
        nz = rng.random() < 0.15
        name = f"{rng.choice(_ADJ)} {rng.choice(_NOUN)} {n_} Pty Ltd"
        r = rng.random()
        emp = None if r < 0.05 else int(10 ** rng.uniform(0.9, 3.5))
        kw: dict = dict(emp=emp, target=rng.random() < 0.05, customer=rng.random() < 0.03,
                        nooks=rng.random() < 0.5)
        if nz:
            kw["country"] = "New Zealand"
        else:
            kw["state"] = rng.choices([s for s, _ in _STATES], [w for _, w in _STATES])[0]
            if rng.random() < 0.02:
                kw.update(country="United States", hq_country="Australia", hq_state=kw["state"], state="Texas")
        if rng.random() < 0.01:
            name = rng.choice(["Sydney CBD", "Melbourne", "Department of Health"]) if rng.random() < 0.5 else name
        b.account(n_, name, **kw)
        ps = []
        for j in range(rng.choice([0, 1, 1, 2, 3])):
            title = rng.choice(_TITLES)
            has_phone = rng.random() > 0.15
            ps.append(b.prospect(n_, j + 1, title or "", phone="auto" if has_phone else None, nz=nz,
                                 opted_out=rng.random() < 0.15))
        for p in ps:
            if rng.random() < 0.15:
                d = "Do Not Call" if rng.random() < 0.05 else rng.choice(_DISPO)
                b.call(p, d, rng.uniform(0.2, 40), duration=rng.choice([5, 15, 25, 60, 200]))
        if rng.random() < 0.3:
            for _ in range(rng.choice([1, 1, 2, 3])):
                k = rng.random()
                if k < 0.25:
                    b.scoop(n_, "Executive Move", "{co} has appointed a new Head of Product.", rng.uniform(0, 100))
                elif k < 0.45:
                    b.scoop(n_, "Funding", "raised $10 million in Series A funding.", rng.uniform(0, 60), ("Funding",))
                elif k < 0.6:
                    b.scoop(n_, "Product Launch", "launched a new mobile app.", rng.uniform(0, 60), ("Product Launch",))
                elif k < 0.75:
                    b.scoop(n_, "Hiring Plans", "has more open roles in Medical & Health.", rng.uniform(0, 30))
                elif k < 0.9:
                    b.scoop(n_, "Facilities", "opened a new warehouse.", rng.uniform(0, 30))
                else:
                    b.scoop(n_, "Partnership", "announced a partnership with a payments company.", rng.uniform(0, 40))
    # ZoomInfo-only companies, ~3% of n, some noise
    for k in range(int(n * 0.03)):
        nid = n + 1 + k
        b.account(nid, rng.choice(["Sydney CBD", f"Zenith {rng.choice(_NOUN)} {nid} Ltd"]), sf=False, zi=True)
    return b.w


# =====================================================================================================
# TESTS
# =====================================================================================================
import copy as _copy
import hashlib
import os
import re
import subprocess
import sys
import time

import pytest

from src import config as cfg_mod
from src.loaders import LoaderError, normalise_email_for_match
from src.models import Account, Signal, SignalKind
from src.state import AccountState, State, StateResetError

CFG_PATH = ROOT / "config" / "config.yaml"
CAP = 40


@pytest.fixture(scope="module")
def cfg():
    return load_config(CFG_PATH)


@pytest.fixture
def world():
    return named_world()


def run(world, tmp_path, **kw):
    kw.setdefault("now", NOW)
    return run_pipeline(world, tmp_path, **kw)


def seeded(tmp_path, ids=(sf15(10),), run_date=date(2026, 11, 24)):
    """A state dir whose last sheet already holds ``ids`` (the night before)."""
    st = st_mod.record_sheet(State(), run_date, list(ids))
    st_mod.save_state(st, tmp_path)
    return tmp_path


def where(out):
    """account name -> (bucket, RankedAccount | Dropped). Each name must appear exactly once."""
    r, found = out.result, {}
    def put(name, bucket, obj):
        assert name not in found, f"{name} appears in two buckets: {found[name][0]} and {bucket}"
        found[name] = (bucket, obj)
    for x in r.ranked: put(x.account.name, "ranked", x)
    for x in r.spilled: put(x.account.name, "spilled", x)
    for x in r.needs_prospect: put(x.account.name, "needs_prospect", x)
    for x in r.skipped: put(x.account.name, "skipped", x)
    for d in out.accounts.dropped: put(d.name, "dropped", d)
    return found


def name_of(world, n):
    return next(r["Name"] for r in world.sf_rows if r.get("Id") and normalise_sf_id(r["Id"]) == sf15(n))


# ----------------------------------------------------------------------------- fixture file
def test_named_world_fixture_is_in_sync():
    """tests/fixtures/integration_world.json is the committed copy of named_world(); regenerate with
    ``python -m tests.test_integration_pipeline --dump-fixture`` after changing the builder."""
    with open(FIX / "integration_world.json", encoding="utf-8") as f:
        committed = json.load(f)
    assert committed == json.loads(json.dumps(named_world().to_dict()))


def test_world_is_synthetic_only():
    blob = json.dumps(named_world().to_dict()) + json.dumps(scale_world(200).to_dict())
    emails = set(re.findall(r"[\w.+-]+@[\w.-]+", blob))
    assert emails and all(e.endswith(".example.com") for e in emails), emails
    for ph in set(re.findall(r"\+\d{9,}", blob)):
        assert ph.startswith(("+614", "+64200")), ph      # placeholder ranges only


# ----------------------------------------------------------------------------- (a) nothing vanishes
def expected_entities(w):
    valid = [r for r in w.sf_rows if r.get("Id")]
    zi_in_sf = {r.get("DOZISF__ZoomInfo_Id__c") for r in valid}
    zi_only = [z for z in w.zoominfo_rows if z["id"] not in zi_in_sf]
    linked = {n["id"] for n in w.nooks_accounts if n.get("_sf_account_id")}
    nooks_only = [n for n in w.nooks_accounts if n["id"] not in linked]
    return len(valid) + len(zi_only) + len(nooks_only), len(w.sf_rows) - len(valid)


def check_conservation(out, w):
    entities, malformed = expected_entities(w)
    r = out.result
    in_buckets = len(r.ranked) + len(r.needs_prospect) + len(r.spilled) + len(r.skipped)
    assert in_buckets == len(out.accounts.accounts)
    assert len(out.accounts.accounts) + len(out.accounts.dropped) == entities + malformed
    assert in_buckets + len(out.accounts.dropped) == entities + malformed     # the headline identity
    # every account object sits in exactly one bucket
    ids = [id(x.account) for x in r.ranked + r.needs_prospect + r.spilled + r.skipped]
    assert len(ids) == len(set(ids))
    # every non-ranked outcome carries a reason string
    for d in out.accounts.dropped:
        assert d.reason and d.detail.strip(), d
    for x in r.skipped:
        assert x.skipped_reason and x.skipped_reason.strip()
    for x in r.needs_prospect:
        assert x.reasons[0].startswith("Needs a prospect: "), x.reasons
        assert r.needs_prospect_codes[ranker.account_key(x.account)] in ranker.NEEDS_PROSPECT_CODES
        assert x.window is None and x.local_call_time is None
    for x in r.spilled:
        assert any(s.startswith("Spilled to ") for s in x.reasons)
        assert x.local_call_time is not None
    for x in r.ranked:
        assert x.reasons and x.window and x.local_call_time and x.skipped_reason is None


def test_a_every_account_has_exactly_one_outcome_and_a_reason(world, tmp_path):
    out = run(world, seeded(tmp_path))
    check_conservation(out, world)
    got = where(out)
    # one example of every outcome type is present
    assert {b for b, _ in got.values()} == {"ranked", "needs_prospect", "skipped", "dropped"}
    assert got[name_of(world, 11)][0] == "needs_prospect"
    assert got[name_of(world, 20)][1].reason == "customer"
    assert got[name_of(world, 10)][1].skipped_reason == "on last sheet"


def test_a_conservation_with_spill(tmp_path):
    w = mini_world(25)
    out = run(w, tmp_path, now=datetime(2026, 11, 25, 5, 10, tzinfo=UTC))      # Wed 16:10 AEDT
    assert out.result.spilled
    check_conservation(out, w)


def test_a_malformed_rows_are_counted_not_crashed(world, tmp_path):
    out = run(world, tmp_path)
    assert any(d.reason == "malformed_row" for d in out.accounts.dropped)
    assert any("no published date" in e for e in out.scoop_errors)


# ----------------------------------------------------------------------------- (b) Do Not Call
DNC_IDS = {"FAKE-NK-P-0005-1", "FAKE-NK-P-0006-1", "FAKE-NK-P-0006-2", "FAKE-NK-P-0007-1", "FAKE-NK-P-0007-2"}


def test_b_dnc_contacts_never_chosen_even_on_best_account(world, tmp_path):
    out = run(world, tmp_path)
    r = out.result
    chosen = {p.nooks_id for ps in r.prospects.values() for p in ps}
    assert not chosen & DNC_IDS
    got = where(out)
    echo = got[name_of(world, 5)][1]
    assert r.ranked[0] is echo and echo.score >= 99                    # best score on the sheet...
    assert [p.nooks_id for p in r.prospects[ranker.account_key(echo.account)]] == ["FAKE-NK-P-0005-2"]   # ...but not Dana
    assert "Pat Callable" in " ".join(echo.reasons)
    # duplicate-email prospect: every prospect on the account is blocked -> skipped with the reason
    fox = got[name_of(world, 6)]
    assert fox[0] == "skipped" and fox[1].skipped_reason.startswith("all prospects blocked")
    # shared switchboard: the other person on that number is blocked, a prospect with her own phone is used
    golf = got[name_of(world, 7)][1]
    assert [p.nooks_id for p in r.prospects[ranker.account_key(golf.account)]] == ["FAKE-NK-P-0007-3"]
    assert out.n_prospects_blocked == 5          # Dana, Fran One, Fran Dup, Gus, Gail


def test_b_dnc_blocked_count_matches_paths(world, tmp_path):
    """Dana (history), Fran One (history), Fran Dup (email), Gus (history), Gail (phone) = 5 marked."""
    out = run(world, tmp_path)
    marked = {p.nooks_id for p in out.prospects if p.do_not_call}
    assert marked == DNC_IDS


def test_b_dnc_by_unlinked_call_and_phone(world, tmp_path):
    """A DNC call logged outside the dialer (prospect null) still blocks the one prospect with that number."""
    w = _copy.deepcopy(world)
    victim = next(p for p in w.contacts if p["Id"] == con15(4, 1))
    b = Builder()
    b._call_n = 9000
    b.call({"id": "x", "phone": victim["MobilePhone"]}, "Do Not Call", 30, unlinked=True)
    w.calls.extend(b.w.calls)
    out = run(w, tmp_path)
    got = where(out)
    assert got[name_of(w, 4)][0] == "skipped"            # Delta's only prospect is now blocked
    assert "FAKE-NK-P-0004-1" not in {p.nooks_id for ps in out.result.prospects.values() for p in ps}


def test_b_ranker_alone_does_not_know_state(world, tmp_path):
    """CONTRACT TRAP (docs/state_notes.md): skip mark_do_not_call and the DNC person IS chosen."""
    out = run(world, tmp_path)
    raw = [replace_dnc(p) for p in out.prospects]            # undo the marking
    unguarded = ranker.rank_accounts(out.accounts.accounts, out.signals, raw, out.cooldown, set(), out.cfg, NOW)
    chosen = {p.nooks_id for ps in unguarded.prospects.values() for p in ps}
    assert chosen & DNC_IDS, "if this fails the ranker learned about state; update the contract docs"


def replace_dnc(p):
    from dataclasses import replace
    return replace(p, do_not_call=False)


def test_b_dnc_survives_state_loss_only_after_rebuild(world, tmp_path):
    """Delete the state file: live runs are refused until the history is replayed; then DNC is back."""
    out = run(world, tmp_path)
    (tmp_path / "state.json").unlink()
    st = st_mod.load_state(tmp_path)
    assert st.is_new and not st.dnc_prospect_ids
    with pytest.raises(StateResetError):
        st_mod.require_healthy(st, live=True)
    st_mod.require_healthy(st, live=False)                   # dry run only warns
    out2 = run(world, tmp_path)                              # pipeline replays all calls, marks, saves
    assert {p.nooks_id for p in out2.prospects if p.do_not_call} == DNC_IDS


def test_b_corrupt_state_is_set_aside_and_gated(world, tmp_path):
    run(world, tmp_path)
    (tmp_path / "state.json").write_text("{not json")
    st = st_mod.load_state(tmp_path)
    assert st.was_reset and (tmp_path / "state.json.corrupt-1").exists()
    with pytest.raises(StateResetError):
        st_mod.require_healthy(st, live=True)


# ----------------------------------------------------------------------------- (c) cooldown
def test_c_touched_two_days_ago_is_dropped_by_accounts_not_ranker(world, tmp_path):
    out = run(world, tmp_path)
    got = where(out)
    b, d = got[name_of(world, 8)]
    assert b == "dropped" and d.reason == "recently_touched" and "cooldown_until 2026-11-26" in d.detail
    assert name_of(world, 8) not in {x.account.name for x in out.result.ranked + out.result.skipped}


def test_c_state_dates(world, tmp_path):
    out = run(world, tmp_path)
    cu = st_mod.cooldown_until(out.state_after, out.cfg)
    assert cu[sf15(8)] == date(2026, 11, 26)                # Mon 23 Nov local + 3
    assert sf15(9) in cu and cu[sf15(9)] < date(2026, 11, 25)      # old connect: cooldown over


def test_c_no_double_counting_over_a_week_of_run_times(world, tmp_path):
    """Sweep run time hour by hour for a week, with `today` = run date. An account is never both dropped by
    accounts (cooldown) and ranked with a cooldown penalty, and the penalty is applied at most once."""
    penalised = 0
    start = datetime(2026, 11, 22, 0, 0, tzinfo=UTC)
    for h in range(0, 24 * 7, 3):
        now = start + timedelta(hours=h)
        out = run(world, tmp_path / f"h{h}", now=now)
        dropped_touched = {d.name for d in out.accounts.dropped if d.reason == "recently_touched"}
        for x in out.result.ranked + out.result.spilled + out.result.needs_prospect:
            assert x.account.name not in dropped_touched
            assert "cooldown_penalty" not in x.score_breakdown       # removed in 1f N2: skip, never penalise
            assert "In cooldown" not in x.reasons
    assert penalised == 0


def test_c_ranker_skips_an_account_whose_cooldown_is_active(world, tmp_path):
    """1f N2: an active cooldown (on the account's own zone call date) is a skip with a reason, not a penalty."""
    out = run(world, tmp_path)
    a = next(x.account for x in out.result.ranked if x.account.name == name_of(world, 1))
    r = ranker.rank_accounts([a], out.signals, out.prospects, {sf18(1): date(2026, 12, 1), sf15(1): date(2026, 12, 1)},
                             set(), out.cfg, NOW)
    assert not r.ranked and r.skipped[0].skipped_reason == "In cooldown until 2026-12-01 (account-local)"


def test_c_night_run_uses_the_first_call_date_not_the_run_date(world, tmp_path):
    """FIXED (1d call_date): a Wed 23:30 run passes call_date=Thu, so an account whose cooldown ends Thu is kept.
    The legacy path (no call_date, today=Wed) still drops it one day early; pinned so nobody drops call_date."""
    night = datetime(2026, 11, 25, 12, 30, tzinfo=UTC)
    fixed = run(world, tmp_path / "a", now=night)
    assert where(fixed)[name_of(world, 8)][0] == "ranked"
    legacy = run(world, tmp_path / "b", now=night, call_date=None)
    assert where(legacy)[name_of(world, 8)][0] == "dropped"
    # and an account whose cooldown ends AFTER the call date is still dropped on the fixed path
    again = run(world, tmp_path / "c", now=datetime(2026, 11, 24, 12, 30, tzinfo=UTC))      # call date Wed 25
    assert where(again)[name_of(world, 8)][0] == "dropped"


# ----------------------------------------------------------------------------- (d) sheet history
def mini_world(n_nsw=25, with_needs=True):
    b = Builder()
    for k in range(1, n_nsw + 1):
        b.account(k, f"Mini {k:03d} Pty Ltd", state="New South Wales", emp=300)
        b.prospect(k, 1, HEAD_PROD)
        b.scoop(k, "Funding", "Mini raised $5 million in funding.", 1 + (k % 20), topics=("Funding",))
    if with_needs:
        b.account(900, "Mini Needs Pty Ltd", state="New South Wales", emp=300)
        b.scoop(900, "Funding", "Needs raised $5 million in funding.", 1, topics=("Funding",))
    return b.w


def test_d_last_sheet_skip_next_night_and_spill_not_recorded(tmp_path):
    w = mini_world(25)
    n1 = datetime(2026, 11, 25, 5, 10, tzinfo=UTC)                       # Wed 16:10 AEDT: 6 slots left
    one = run(w, tmp_path, now=n1)
    r1 = one.result
    assert len(r1.ranked) == 6 and len(r1.spilled) == 19 and len(r1.needs_prospect) == 1
    sheet = one.state_after.sheets[-1]
    assert sheet["run_date"] == "2026-11-25"
    ranked_ids = {ra.account.sf_account_id for ra in r1.ranked}
    assert set(sheet["account_ids"]) == ranked_ids                       # ranked ONLY
    assert not ({ra.account.sf_account_id for ra in r1.spilled + r1.needs_prospect} & set(sheet["account_ids"]))
    assert all(ra.local_call_time.date() == date(2026, 11, 26) for ra in r1.spilled)
    # next night: yesterday's six are hard-skipped; the spilled ones are picked up on their own
    two = run(w, tmp_path, now=datetime(2026, 11, 26, 3, 0, tzinfo=UTC))
    r2 = two.result
    assert {x.account.sf_account_id for x in r2.skipped if x.skipped_reason == "on last sheet"} == ranked_ids
    assert {x.account.sf_account_id for x in r2.ranked} == {x.account.sf_account_id for x in r1.spilled}
    assert not r2.spilled
    assert [x.account.name for x in r2.needs_prospect] == ["Mini Needs Pty Ltd"]    # still waiting, never recorded
    check_conservation(two, w)


def test_d_needs_prospect_on_last_sheet_is_skipped_by_ranker_contract(tmp_path):
    """ranker_notes: last_sheet_ids hard-skips needs_prospect accounts too (the sheet builder never records them,
    so this only matters if it does)."""
    w = mini_world(3)
    out = run(w, tmp_path, now=NOW)
    nid = next(a for a in out.accounts.accounts if a.name == "Mini Needs Pty Ltd").sf_account_id
    r = ranker.rank_accounts(out.accounts.accounts, out.signals, out.prospects, {}, {nid}, out.cfg, NOW)
    assert next(x for x in r.skipped if x.account.sf_account_id == nid).skipped_reason == "on last sheet"


def test_d_sheet_history_keeps_three_nights(tmp_path):
    w = mini_world(3)
    for i in range(5):
        run(w, tmp_path, now=NOW + timedelta(days=i))
    st = st_mod.load_state(tmp_path)
    assert [s["run_date"] for s in st.sheets] == ["2026-11-27", "2026-11-28", "2026-11-29"]


def test_d_same_night_rerun_is_stable(world, tmp_path):
    a = run(world, tmp_path)
    b = run(world, tmp_path)
    assert [x.account.name for x in a.result.ranked] == [x.account.name for x in b.result.ranked]


def test_d_same_night_rerun_skips_nothing_from_the_same_run(world, tmp_path):
    """H1 fixed in state.last_sheet_ids(state, run_date): the sheet recorded for TODAY is ignored, so a second
    run on the same night skips nothing as 'on last sheet' and ranks the same accounts."""
    a = run(world, tmp_path)
    b = run(world, tmp_path)
    assert a.result.ranked
    assert not {x.account.name for x in b.result.skipped if x.skipped_reason == "on last sheet"} & \
        {x.account.name for x in a.result.ranked}


def test_d_dry_run_without_record_sheet_is_repeatable(world, tmp_path):
    a = run(world, tmp_path, mark_sheet=False)
    b = run(world, tmp_path, mark_sheet=False)
    assert [x.account.name for x in a.result.ranked] == [x.account.name for x in b.result.ranked]
    assert st_mod.load_state(tmp_path).sheets == []


# ----------------------------------------------------------------------------- (e) 15 vs 18 char ids
def to15(k):
    return normalise_sf_id(k)


def to18(k):
    return k + "AAA" if isinstance(k, str) and len(k) == 15 and k.isalnum() else k


def rnd_style(seed):
    rng = random.Random(seed)
    return lambda k: to18(k) if rng.random() < 0.5 else to15(k)


def e_run(tmp_path, idfn, restyle):
    w = named_world(idfn)
    d = tmp_path
    st_mod.save_state(st_mod.record_sheet(State(), date(2026, 11, 24), [restyle(sf15(10))]), d)
    return w, run_pipeline(w, d, now=NOW, restyle=restyle, blocked={sf15(13)},
                           extra_touched={restyle(sf15(25)): date(2026, 11, 24)})


def digest(out):
    return json.dumps(summary_dict(out), sort_keys=True, default=str)


def test_e_id_spelling_never_changes_the_result(tmp_path):
    base_w, base = e_run(tmp_path / "base", fixed_style("15"), to15)
    want = digest(base)
    cases = {"all18": (fixed_style("18"), to18), "mixed-ids": (mixed_style(5), to15),
             "mixed-keys": (fixed_style("15"), rnd_style(11)), "everything-mixed": (mixed_style(9), rnd_style(3)),
             "ids18-keys15": (fixed_style("18"), to15), "ids15-keys18": (fixed_style("15"), to18)}
    for label, (idfn, restyle) in cases.items():
        _, out = e_run(tmp_path / label, idfn, restyle)
        assert digest(out) == want, label
    got = where(base)
    assert got[name_of(base_w, 13)][1].reason == "blocked"
    assert got[name_of(base_w, 25)][1].reason == "recently_touched"      # touched key spelled differently
    assert got[name_of(base_w, 10)][1].skipped_reason == "on last sheet"


def test_e_state_file_with_mixed_spellings_merges_on_load(tmp_path):
    st = State(accounts={sf15(8): AccountState(last_call_date="2026-11-23", last_outcome="no_contact"),
                         sf18(8): AccountState(last_call_date="2026-11-24", last_outcome="no_contact")})
    raw = st_mod.state_to_dict(st)
    (tmp_path / "state.json").write_text(json.dumps(raw))
    loaded = st_mod.load_state(tmp_path)
    assert list(loaded.accounts) == [sf15(8)]


# ----------------------------------------------------------------------------- (f) time zones
OFFSETS_NOV = {"Australia/Perth": 8, "Australia/Sydney": 11, "Australia/Melbourne": 11, "Pacific/Auckland": 13,
               "Australia/Brisbane": 10, "Australia/Adelaide": 10.5, "Australia/Darwin": 9.5}


def local_ok(ra, cfg):
    t = ra.local_call_time
    tz = ra.account.timezone
    assert str(t.tzinfo) == tz
    wins = cfg_mod.window_bounds(cfg, tz, t.date())
    hit = [w for w in wins if w.start <= t < w.end]
    assert hit and hit[0].name == ra.window, (ra.account.name, t, ra.window)


def test_f_local_windows_and_offsets(world, tmp_path):
    out = run(world, tmp_path)
    seen = set()
    for ra in out.result.ranked:
        local_ok(ra, out.cfg)
        assert ra.local_call_time.utcoffset() == timedelta(hours=OFFSETS_NOV[ra.account.timezone])
        assert ra.local_call_time.astimezone(UTC) > NOW
        seen.add(ra.account.timezone)
    assert {"Australia/Perth", "Australia/Sydney", "Pacific/Auckland", "Australia/Brisbane"} <= seen
    # Perth is 11:00 local when Sydney is 14:00: morning window, Sydney is in the afternoon window
    perth = next(x for x in out.result.ranked if x.account.timezone == "Australia/Perth")
    syd = next(x for x in out.result.ranked if x.account.timezone == "Australia/Sydney")
    assert perth.window == "morning" and syd.window == "afternoon"
    assert perth.local_call_time.hour == 11 and perth.local_call_time.minute == 1
    assert syd.local_call_time.hour == 14 and syd.local_call_time.minute >= 1


def test_f_late_thursday_run_documented_zone_split(world, tmp_path):
    """Thu 16:28 Sydney. Sydney / Melbourne / Auckland have nothing left today: FRIDAY. Perth / Brisbane /
    Adelaide / Darwin still have the end of Thursday's afternoon window: THURSDAY. No 'spilled' label anywhere."""
    now = datetime(2026, 11, 26, 5, 28, tzinfo=UTC)
    out = run(world, tmp_path, now=now)
    assert not out.result.spilled
    days = {}
    for ra in out.result.ranked:
        local_ok(ra, out.cfg)
        assert not any(s.startswith("Spilled") for s in ra.reasons)
        days.setdefault(ra.account.timezone, set()).add(ra.local_call_time.date())
    assert days["Australia/Sydney"] == {date(2026, 11, 27)}
    assert days["Australia/Melbourne"] == {date(2026, 11, 27)}
    assert days["Pacific/Auckland"] == {date(2026, 11, 27)}
    assert days["Australia/Perth"] == {date(2026, 11, 26)}
    assert days["Australia/Brisbane"] == {date(2026, 11, 26)}
    assert days["Australia/Darwin"] == {date(2026, 11, 26)}


def test_f_later_thursday_everything_is_friday(world, tmp_path):
    now = datetime(2026, 11, 26, 11, 0, tzinfo=UTC)                       # Thu 22:00 Sydney, 19:00 Perth
    out = run(world, tmp_path, now=now)
    assert out.result.ranked and not out.result.spilled
    for ra in out.result.ranked:
        assert ra.local_call_time.date() == date(2026, 11, 27)          # Friday, in either window (spread over the day)
        assert ra.local_call_time.astimezone(UTC) > now
        assert not any(s.startswith("Spilled") for s in ra.reasons)


def test_f_weekend_and_friday_evening_go_to_monday(world, tmp_path):
    for now in (datetime(2026, 11, 27, 9, 0, tzinfo=UTC), datetime(2026, 11, 28, 1, 0, tzinfo=UTC),
                datetime(2026, 11, 29, 6, 0, tzinfo=UTC)):                # Fri 20:00, Sat, Sun (Sydney)
        out = run(world, tmp_path / now.isoformat(), now=now)
        assert out.result.ranked
        for ra in out.result.ranked:
            assert ra.local_call_time.weekday() == 0 and ra.local_call_time.date() == date(2026, 11, 30), ra.account.name
        assert not out.result.spilled


# (run time, offsets on the FIRST usable day if it is still the Friday before the change, offsets on Monday after it)
DST_CASES = [
    # Fri 2 Oct 2026 16:40 AEST: Sydney / Adelaide clocks go forward on Sun 4 Oct (NZ went forward on 27 Sep).
    (datetime(2026, 10, 2, 6, 40, tzinfo=UTC),
     {"Australia/Sydney": 10, "Australia/Melbourne": 10, "Australia/Adelaide": 9.5, "Australia/Brisbane": 10,
      "Australia/Perth": 8, "Australia/Darwin": 9.5, "Pacific/Auckland": 13},
     {"Australia/Sydney": 11, "Australia/Melbourne": 11, "Australia/Adelaide": 10.5, "Australia/Brisbane": 10,
      "Australia/Perth": 8, "Australia/Darwin": 9.5, "Pacific/Auckland": 13}),
    # Fri 2 Apr 2027 16:40 AEDT: AU and NZ clocks go back on Sun 4 Apr.
    (datetime(2027, 4, 2, 5, 40, tzinfo=UTC),
     {"Australia/Sydney": 11, "Australia/Melbourne": 11, "Australia/Adelaide": 10.5, "Australia/Brisbane": 10,
      "Australia/Perth": 8, "Australia/Darwin": 9.5, "Pacific/Auckland": 13},
     {"Australia/Sydney": 10, "Australia/Melbourne": 10, "Australia/Adelaide": 9.5, "Australia/Brisbane": 10,
      "Australia/Perth": 8, "Australia/Darwin": 9.5, "Pacific/Auckland": 12}),
]


@pytest.mark.parametrize("now,friday,monday", DST_CASES, ids=["spring-forward-Oct-2026", "fall-back-Apr-2027"])
def test_f_dst_changeover_week(world, tmp_path, now, friday, monday):
    """A run on the Friday before the clocks change. Zones whose Friday window is still open (Perth, Brisbane,
    Darwin, Adelaide) keep Friday with the OLD offset; the rest go to Monday with the NEW offset and the window
    still starts at 08:45 local."""
    out = run(world, tmp_path, now=now)
    seen_monday = set()
    for ra in out.result.ranked:
        local_ok(ra, out.cfg)
        t = ra.local_call_time
        want = (friday if t.weekday() == 4 else monday)[ra.account.timezone]
        assert t.utcoffset() == timedelta(hours=want), (ra.account.name, t)
        assert t.astimezone(UTC) > now
        if t.weekday() == 0:
            seen_monday.add(ra.account.timezone)
            assert t.date() == (date(2026, 10, 5) if now.year == 2026 else date(2027, 4, 5))
            assert (t.hour, t.minute) >= (8, 45)
    assert {"Australia/Sydney", "Pacific/Auckland"} <= seen_monday
    # the same instant converted to UTC proves the shift: Sydney 08:45 Monday is 1 hour earlier in UTC after spring-forward
    syd = min(x.local_call_time for x in out.result.ranked if x.account.timezone == "Australia/Sydney")
    assert syd.astimezone(UTC).hour == (21 if now.year == 2026 else 22) and syd.astimezone(UTC).minute == 45


def test_f_state_days_are_account_local(tmp_path):
    """The same instant is a different calendar day in Perth and in Sydney; cooldown dates follow."""
    b = Builder()
    b.account(1, "Perth Co Pty Ltd", state="Western Australia", emp=200)
    p1 = b.prospect(1, 1, HEAD_PROD)
    b.account(2, "Sydney Co Pty Ltd", state="New South Wales", emp=200)
    p2 = b.prospect(2, 1, HEAD_PROD)
    t = datetime(2026, 11, 25, 15, 30, tzinfo=UTC)               # Wed 23:30 Perth, Thu 02:30 Sydney
    for p in (p1, p2):
        b.call(p, "No Answer", (NOW - t).total_seconds() / 86400)
    out = run(b.w, tmp_path)
    cu = st_mod.cooldown_until(out.state_after, out.cfg)
    assert cu[sf15(1)] == date(2026, 11, 28)          # Wed 25 + 3
    assert cu[sf15(2)] == date(2026, 11, 29)          # Thu 26 + 3


# ----------------------------------------------------------------------------- (g) no signal
def test_g_no_signal_is_ranked_on_fit_only_and_says_so(world, tmp_path):
    out = run(world, tmp_path)
    ra = where(out)[name_of(world, 4)][1]
    assert ra.reasons[0] == "No trigger: ranked on fit only"
    assert ra.signals == []
    assert ra.score_breakdown["trigger_strength"] == 0 and ra.score_breakdown["recency"] == 0
    assert ra.score == pytest.approx(25 + 15)                        # tier-1 fit (25) + named prospect (15)
    # the dropped generic scoop did not leak in as a trigger
    text = " ".join(ra.reasons).lower()
    assert not any(k in text for k in ("funding", "hiring", "launch", "executive", "new hire", "partnership"))
    assert out.signals.get(ra.account.sf_account_id) is None
    log = out.log
    assert "no real trigger (ranked on fit only): " in log


def test_g_every_ranked_reason_matches_real_signal_data(world, tmp_path):
    out = run(world, tmp_path)
    labels = {SignalKind.EXEC_MOVE: "Executive move", SignalKind.FUNDING: "Funding",
              SignalKind.PRODUCT_LAUNCH: "Product launch"}
    for ra in out.result.ranked + out.result.needs_prospect:
        if ra.signals:
            top = max(ra.signals, key=lambda s: s.strength)
            line = ra.reasons[1] if ra.reasons[0].startswith("Needs a prospect") else ra.reasons[0]
            assert labels[top.kind] in line and not line.startswith("No trigger")
            assert ra.score_breakdown["trigger_strength"] > 0
        else:
            assert any(x.startswith("No trigger") for x in ra.reasons[:2])
            assert ra.score_breakdown["trigger_strength"] == 0
        # every signal on the account came from a scoop that was KEPT
    kept_sources = {f"zoominfo_scoop:{v.scoop.id}" for v in out.verdicts if v.keep}
    for ra in out.result.ranked + out.result.needs_prospect + out.result.skipped:
        for s in ra.signals:
            assert s.source in kept_sources and s.kind is not SignalKind.NONE


def test_g_explicit_none_signal_is_ignored(world, tmp_path):
    out = run(world, tmp_path)
    a = next(x.account for x in out.result.ranked if x.account.name == name_of(world, 4))
    none = sig_mod.no_trigger_signal(a)
    r = ranker.rank_accounts([a], {a.sf_account_id: [none]}, out.prospects, {}, set(), out.cfg, NOW)
    assert r.ranked[0].reasons[0] == "No trigger: ranked on fit only" and r.ranked[0].signals == []


def test_g_dropped_scoops_never_become_signals(world, tmp_path):
    out = run(world, tmp_path)
    dropped = {v.scoop.id for v in out.verdicts if not v.keep}
    assert dropped
    sources = {s.source for sigs in out.signals.values() for s in sigs}
    assert not any(f"zoominfo_scoop:{d}" in sources for d in dropped)
    assert all(v.reason.startswith(("Kept:", "Dropped:")) for v in out.verdicts)
    assert len(out.unmatched_scoops) == 2      # Hotel's account was dropped (cooldown); company 9999 never existed


# ----------------------------------------------------------------------------- (h) confidence / flags
def test_h_zoominfo_only_and_nooks_only_are_low_confidence_and_cannot_be_called(world, tmp_path):
    out = run(world, tmp_path)
    got = where(out)
    for n, flag in ((15, "zoominfo_only"), (16, "nooks_only")):
        b, ra = got[name_of_any(out, n)]
        assert b == "skipped" and ra.skipped_reason == "no timezone"
        assert ra.account.confidence == "low" and flag in ra.account.flags
    # a strong ZoomInfo scoop on the ZoomInfo-only account is matched, kept and carried on the skipped row
    oscar = got["Oscar Analytics Pty Ltd"][1]
    assert oscar.signals and oscar.signals[0].kind is SignalKind.EXEC_MOVE


def name_of_any(out, n):
    return {15: "Oscar Analytics Pty Ltd", 16: "Papa Nooksonly Pty Ltd"}[n]


def test_h_low_confidence_has_a_score_ceiling(world, tmp_path):
    """FIXED (1f): Account.confidence == 'low' caps the score at ranker.LOW_CONFIDENCE_CEILING (60). A strong trigger on
    a low-confidence book account (flagged noise) cannot out-rank clean accounts; the cap is itemised."""
    out = run(world, tmp_path)
    a = next(x for x in out.result.ranked if x.account.name == "Sydney CBD")
    assert a.account.confidence == "low" and "noise_location" in a.account.flags
    assert any(r.startswith("Flag: noise_location") for r in a.reasons) and any(r.startswith("Low-confidence record") for r in a.reasons)
    strong = Signal(a.account.sf_account_id, SignalKind.EXEC_MOVE, 0.9, "zoominfo_scoop:S", NOW - timedelta(days=1),
                    "new Head of Product", a.account.zoominfo_id)
    low = ranker.rank_accounts([a.account], {a.account.sf_account_id: [strong]}, out.prospects, {}, set(), out.cfg, NOW)
    clean = _copy.deepcopy(a.account)
    clean.flags, clean.confidence = [], "normal"
    ok = ranker.rank_accounts([clean], {clean.sf_account_id: [strong]}, out.prospects, {}, set(), out.cfg, NOW)
    assert ok.ranked[0].score > ranker.LOW_CONFIDENCE_CEILING
    assert low.ranked[0].score == ranker.LOW_CONFIDENCE_CEILING
    assert "confidence_ceiling_adjustment" in low.ranked[0].score_breakdown
    assert round(sum(low.ranked[0].score_breakdown.values()), 2) == low.ranked[0].score       # breakdown still sums


def test_h_flagged_group_kept_flagged_and_penalised(world, tmp_path):
    out = run(world, tmp_path)
    ra = where(out)[name_of(world, 14)][1]
    assert ra.account.non_anz_billing_anz_hq and "flagged_non_anz_billing_anz_hq" in ra.account.flags
    assert ra.account.confidence == "low"
    assert ra.score_breakdown["flagged_group_penalty"] == -3.0
    assert any("Non-ANZ billing" in s for s in ra.reasons)
    assert ra.account.timezone == "Australia/Melbourne"           # US 'Washington' billing state is ignored
    assert [a.name for a in acc_mod.list_flagged(out.accounts.accounts)] == [name_of(world, 14)]
    assert out.accounts.dropped and all(d.name != name_of(world, 14) for d in out.accounts.dropped)


def test_h_book_noise_is_flagged_not_dropped_and_zoominfo_noise_is_dropped(world, tmp_path):
    out = run(world, tmp_path)
    got = where(out)
    assert got["Sydney CBD"][0] in ("ranked", "skipped", "needs_prospect")       # kept
    assert "noise_location" in got["Sydney CBD"][1].account.flags
    b, d = got["Melbourne CBD"]
    assert b == "dropped" and d.reason == "noise_location" and d.source == "zoominfo"


def test_h_unknown_size_neutral_and_country_unknown_skipped_with_reason(world, tmp_path):
    out = run(world, tmp_path)
    got = where(out)
    ra = got[name_of(world, 19)][1]
    assert ra.account.size_unknown and ra.score_breakdown["icp_fit"] == 7.5
    assert "Headcount unknown (neutral fit)" in ra.reasons
    b, x = got[name_of(world, 23)]
    assert b == "skipped" and x.skipped_reason == "no timezone" and "country_unknown" in x.account.flags


def test_h_name_merge_when_nooks_has_no_crm_link(world, tmp_path):
    w = _copy.deepcopy(world)
    for r in w.nooks_accounts:
        r.pop("_sf_account_id", None)
    out = run(w, tmp_path)
    assert out.accounts.stats.get("name_merged", 0) >= 3                 # accounts 1, 2, 27 (nooks=True)
    top = [a for a in out.accounts.accounts if a.name == name_of(w, 1)]
    assert len(top) == 1 and top[0].source_nooks and top[0].nooks_account_id == "FAKE-NK-ACCT-0001"


# ----------------------------------------------------------------------------- (i) determinism
def shuffle_world(w, seed):
    w2 = _copy.deepcopy(w)
    rng = random.Random(seed)
    for k in ("sf_rows", "nooks_accounts", "zoominfo_rows", "contacts", "nooks_prospects", "calls", "scoops",
              "sent_threads", "dispositions"):
        rng.shuffle(getattr(w2, k))
    return w2


def digest_run(w, tmp_path, seed, now=NOW):
    sw = shuffle_world(w, seed)
    d = tmp_path / f"s{seed}"
    st_mod.save_state(st_mod.record_sheet(State(), date(2026, 11, 24), [sf15(10)]), d)
    out = run_pipeline(sw, d, now=now, call_page=random.Random(seed).randint(1, 40))
    return hashlib.sha256(digest(out).encode()).hexdigest(), out


def test_i_shuffled_inputs_give_byte_identical_output(tmp_path):
    w = named_world()
    base, _ = digest_run(w, tmp_path, 0)
    for s in range(1, 21):
        assert digest_run(w, tmp_path, s)[0] == base, f"shuffle {s}"
    big = scale_world(400, seed=3)
    b0, _ = digest_run(big, tmp_path / "big", 0)
    for s in range(1, 6):
        assert digest_run(big, tmp_path / "big", s)[0] == b0


_HASH_SNIPPET = (
    "import sys, tempfile, pathlib\n"
    "from tests.test_integration_pipeline import *\n"
    "d = pathlib.Path(tempfile.mkdtemp())\n"
    "w = named_world()\n"
    "out = []\n"
    "for s in (0, 1, 2):\n"
    "    out.append(digest_run(w, d, s)[0])\n"
    "out.append(digest_run(scale_world(300, seed=4), d / 'big', 1)[0])\n"
    "print(','.join(out))\n")


def test_i_identical_across_pythonhashseeds(tmp_path):
    results = set()
    for hs in ("0", "1", "424242", "random"):
        env = dict(os.environ, PYTHONHASHSEED=hs)
        p = subprocess.run([sys.executable, "-c", _HASH_SNIPPET], cwd=ROOT, env=env, capture_output=True, text=True,
                           timeout=300)
        assert p.returncode == 0, p.stderr[-2000:]
        results.add(p.stdout.strip().splitlines()[-1])
    assert len(results) == 1 and len(next(iter(results)).split(",")) == 4, results


# ----------------------------------------------------------------------------- (j) scale
SCALE_METRICS: dict = {}


def test_j_three_thousand_accounts_cap_spill_runtime_memory(tmp_path):
    import resource, tracemalloc
    w = scale_world(3000, seed=7)
    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t0 = time.perf_counter()
    out = run(w, tmp_path / "a")
    wall = time.perf_counter() - t0
    tracemalloc.start()                                   # tracemalloc slows everything ~5x: measured separately
    run(w, tmp_path / "t")
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    r = out.result
    check_conservation(out, w)
    assert len(r.ranked) == CAP and not r.spilled
    assert [x.score for x in r.ranked] == sorted((x.score for x in r.ranked), reverse=True)
    below = [x for x in r.skipped if x.skipped_reason.startswith("below daily cap")]
    assert below and max(x.score for x in below) <= min(x.score for x in r.ranked) + 1e-9
    assert len({tuple(p.nooks_id for p in ps) for ps in r.prospects.values()}) == CAP
    for ra in r.ranked:
        local_ok(ra, out.cfg)
    # spill at scale: 16:10 Wed leaves Sydney 6 slots, Melbourne the same, etc.
    out2 = run(w, tmp_path / "b", now=datetime(2026, 11, 25, 5, 10, tzinfo=UTC))
    check_conservation(out2, w)
    assert out2.result.spilled and len(out2.result.ranked) <= CAP
    assert {x.account.sf_account_id for x in out2.result.spilled}.isdisjoint(set(out2.state_after.sheets[-1]["account_ids"]))
    SCALE_METRICS.update(accounts=len(w.sf_rows) + len(w.zoominfo_rows), prospects=len(w.nooks_prospects),
                         calls=len(w.calls), scoops=len(w.scoops), wall_s=round(wall, 2),
                         stages={k: round(v, 3) for k, v in out.timings.items()},
                         py_heap_peak_mb=round(peak / 1e6, 1),
                         maxrss_delta_mb=round((resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - rss0) / 1024, 1))
    assert wall < 60 and peak < 1.5e9


def test_j_twelve_thousand_accounts_the_real_book_size(tmp_path):
    """~12k ANZ accounts (the real book size): conservation, cap, runtime and memory ceilings."""
    import resource
    w = scale_world(12000, seed=7)
    t0 = time.perf_counter()
    out = run(w, tmp_path)
    wall = time.perf_counter() - t0
    check_conservation(out, w)
    assert len(out.result.ranked) == CAP
    assert wall < 120 and resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 < 1500
    ranker_section = out.log.split("== RANKER ==")[1]
    assert len(ranker_section.splitlines()) < 200                              # ranker part of the log stays bounded


def test_j_record_calls_paging_is_cheap_at_20k(tmp_path):
    """FIXED (1c L5): record_calls no longer deep-copies State per page. 20k calls in pages of 100 (the worst case,
    4.8 s before) now stay well inside a few seconds, and the result does not depend on the page size."""
    w = scale_world(300, seed=2)
    idx = build_disposition_index(w.dispositions)
    cfg_ = load_config(CFG_PATH)
    first = acc_mod.build_account_list(w.sf_rows, w.nooks_accounts, w.zoominfo_rows, cfg_, {}, NOW.date())
    by_sf = {a.sf_account_id: a for a in first.accounts if a.sf_account_id}
    pass
    ps = _prospect_objects(w, cfg_, by_sf, {})
    import dataclasses
    calls = [call_from_nooks(r, idx) for r in w.calls] or []
    assert calls
    big = [dataclasses.replace(calls[i % len(calls)], nooks_id=f"FAKE-BIG-{i}") for i in range(20000)]
    t0 = time.perf_counter()
    st = State()
    for i in range(0, len(big), 100):
        st = st_mod.record_calls(st, big[i:i + 100], ps, by_sf, config=cfg_)
    assert time.perf_counter() - t0 < 5
    assert len(st.seen_calls) == 20000
    once = st_mod.record_calls(State(), big, ps, by_sf, config=cfg_)
    a_, b_ = st_mod.state_to_dict(once), st_mod.state_to_dict(st)
    a_.pop("last_run", None), b_.pop("last_run", None)             # counts of the LAST page differ by design
    assert a_ == b_


# ----------------------------------------------------------------------------- (k) logs
def test_k_log_is_readable_plain_text(world, tmp_path):
    out = run(world, seeded(tmp_path))
    log = out.log
    assert log.isascii(), [c for c in log if not c.isascii()][:5]
    for bad in ("SignalKind.", "Account(", "RankedAccount(", "datetime.", "object at 0x", "<class", "{'", "None |",
                "nan", "Traceback"):
        assert bad not in log, bad
    assert max(len(l) for l in log.splitlines()) < 600
    for section in ("== STATE ==", "== SIGNALS ==", "== ACCOUNTS ==", "== RANKER =="):
        assert section in log
    # key numbers appear in words
    r = out.result
    assert f"Ranked {len(r.ranked)} accounts; skipped {len(r.skipped)}; needs prospect {len(r.needs_prospect)}; " \
           f"spilled {len(r.spilled)}." in log
    assert f"accounts kept: {len(out.accounts.accounts)}" in log and f"accounts dropped: {len(out.accounts.dropped)}" in log
    assert "Dropped: expired, 200 days old" in log and "Dropped: generic hiring note" in log
    assert "needs prospect (NOT callable, no task)" in log


def test_k_log_has_no_raw_contact_details(world, tmp_path):
    out = run(world, seeded(tmp_path))
    blob = json.dumps(world.to_dict())
    emails = set(re.findall(r"[\w.+-]+@[\w.-]+", blob))
    phones = set(re.findall(r"\+\d{9,}", blob))
    for s in emails | phones:
        assert s not in out.log, s
    assert "@" not in out.log


def test_k_scale_log_and_state_have_no_raw_contact_details(tmp_path):
    w = scale_world(500, seed=9)
    out = run(w, tmp_path)
    blob = json.dumps(w.to_dict())
    emails = set(re.findall(r"[\w.+-]+@[\w.-]+", blob))
    phones = set(re.findall(r"\+\d{9,}", blob))
    state_text = (tmp_path / "state.json").read_text()
    assert emails and phones
    assert not any(e in out.log or e in state_text for e in emails)
    assert not any(ph in out.log for ph in phones)
    assert "@" not in out.log and "@" not in state_text
    assert set(re.findall(r"\+\d{9,}", state_text)) == set(json.loads(state_text)["dnc_phones"])


def test_k_state_file_holds_hashed_emails_and_only_dnc_phones(world, tmp_path):
    run(world, tmp_path)
    text = (tmp_path / "state.json").read_text()
    assert "@" not in text and "example.com" not in text
    raw = json.loads(text)
    phones = set(re.findall(r"\+\d{9,}", text))
    assert phones == set(raw["dnc_phones"]) and phones                       # the ONLY place a number is stored
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in raw["dnc_emails"])
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in raw["emails"])


# ----------------------------------------------------------------------------- (l) hard-rule scan
SRC = sorted((ROOT / "src").glob("*.py"))
FORBIDDEN_CALLS = [
    r"\bsend_message\b", r"\bsendTaskEmail\b", r"\bcreateEmail\b", r"\bcreateTask\b", r"\bupdateTask\b",
    r"\bdeleteTask\b", r"\bcreateSobjectRecord\b", r"\bupdateSobjectRecord\b", r"\bdeleteSobjectRecord\b",
    r"\bcreateRelatedRecord\b", r"\bupdateRelatedRecord\b", r"\bdeleteRelatedRecord\b", r"\bcreateAccountNote\b",
    r"\bcreateProspectNote\b", r"\bcreateCrmProspects\b", r"\bsyncProspects\b", r"\bupdateProspect\b",
    r"\bcreate_draft\b", r"\bsoqlQuery\b", r"\bsendmail\b", r"\.reply\(", r"\bforward\(", r"\.send\("]
FORBIDDEN_IMPORTS = ["requests", "httpx", "urllib", "http", "socket", "aiohttp", "smtplib", "ssl", "ftplib",
                      "subprocess", "anthropic", "openai", "mcp", "paramiko", "telnetlib", "xmlrpc"]


@pytest.mark.parametrize("path", SRC, ids=lambda p: p.name)
def test_l_no_forbidden_call_names(path):
    text = path.read_text()
    for pat in FORBIDDEN_CALLS:
        assert not re.search(pat, text), f"{path.name}: {pat}"


@pytest.mark.parametrize("path", SRC, ids=lambda p: p.name)
def test_l_no_network_or_process_imports(path):
    import ast
    tree = ast.parse(path.read_text())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module.split(".")[0])
    assert not mods & set(FORBIDDEN_IMPORTS), (path.name, mods & set(FORBIDDEN_IMPORTS))
    allowed = {"__future__", "collections", "copy", "dataclasses", "datetime", "enum", "functools", "hashlib",
               "json", "logging", "math", "os", "pathlib", "re", "tempfile", "typing", "unicodedata", "yaml",
               "zoneinfo", "src", "itertools", "operator", "string"}
    assert mods <= allowed, (path.name, sorted(mods - allowed))


@pytest.mark.parametrize("path", SRC, ids=lambda p: p.name)
def test_l_no_hardcoded_real_ids_phones_emails(path):
    text = path.read_text()
    emails = re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    assert all(e.split("@")[1].startswith(("example.com", "example.org", "x.com")) or e.startswith(("a@", "a+x@"))
               for e in emails), emails
    phones = re.findall(r"\+?\b61\s?4\d{8}\b|\+614\d{8}|\+642\d{7,9}", text)
    assert all(re.fullmatch(r"\+614000000\d\d", x) for x in phones), phones        # docstring placeholders only
    assert "5koD9szETWeI1F0DcRTqkoHGPlY2" not in text and "pendo.io" not in text
    assert not re.findall(r"\b00[1-9A-Za-z]{12,15}\b", text), "looks like a hard-coded Salesforce id"


def test_l_nothing_in_src_writes_outside_state_dir():
    """The only file writes in src/ are state.save_state (temp file + rename inside state_dir)."""
    writers = []
    for p in SRC:
        t = p.read_text()
        if re.search(r"open\([^)]*[\"'][wa]b?[\"']|\.write_text\(|\.write_bytes\(|mkstemp|os\.replace|os\.unlink|shutil", t):
            writers.append(p.name)
    assert writers == ["state.py"], writers


def test_l_logging_calls_carry_no_contact_details():
    """Every log.* call in src/ is listed so a human can re-check them when Phase 2 adds more."""
    lines = []
    for p in SRC:
        for i, l in enumerate(p.read_text().splitlines(), 1):
            if re.search(r"\blog(ger)?\.(debug|info|warning|error|exception)\(", l):
                lines.append(f"{p.name}:{i}")
    assert all(x.startswith("state.py") or x.startswith("signals.py") or x.startswith("config.py")
               or x.startswith("accounts.py") or x.startswith("ranker.py") or x.startswith("loaders.py") for x in lines)
    # state.py logs paths, reasons and COUNTS of unmatched calls; none of its calls format a prospect or email
    for l in (ROOT / "src" / "state.py").read_text().splitlines():
        if re.search(r"\blog\.(warning|info|error)\(", l):
            assert not re.search(r"email|phone|prospect\.", l.lower().replace("unmatched", "")), l


def test_h_duplicate_zoominfo_id_trigger_reaches_every_duplicate(tmp_path):
    """FIXED (1d group_by_zoominfo_id + 1e list values): two SF accounts share one ZoomInfo id (flagged
    possible_duplicate, both kept); both get the same trigger, so neither is wrongly labelled 'No trigger'."""
    b = Builder()
    for n in (1, 2):
        b.account(n, f"Dup {n} Pty Ltd", state="New South Wales", emp=300)
        b.prospect(n, 1, HEAD_PROD)
    for r in b.w.sf_rows:
        r["DOZISF__ZoomInfo_Id__c"] = "FAKE-ZI-0001"
    b.w.zoominfo_rows = [{"id": "FAKE-ZI-0001", "type": "Company", "attributes": {"name": "Dup 1 Pty Ltd"}}]
    b.scoop(1, "Funding", "Dup raised $5 million in funding.", 2, topics=("Funding",))
    out = run(b.w, tmp_path)
    got = {x.account.name: x for x in out.result.ranked}
    assert len(got) == 2 and all("possible_duplicate" in x.account.flags for x in got.values())
    assert all(x.signals and x.signals[0].kind is SignalKind.FUNDING for x in got.values())
    assert len({x.signals[0].source for x in got.values()}) == 1


# ----------------------------------------------------------------------------- (m) interface contracts
def test_m_documented_names_exist():
    from src import loaders, models
    docs = {
        "src.models": ["Account", "Prospect", "CallRecord", "TaskRecord", "SentEmail", "Scoop", "SignalKind", "Signal",
                       "RankedAccount", "SheetEntry"],
        "src.config": ["load_config", "parse_config", "canonical_country", "is_blank_or_junk", "normalise_au_state",
                       "resolve_timezone", "tier_for", "window_bounds", "next_working_windows", "ConfigError"],
        "src.loaders": ["LoaderError", "account_from_sf_row", "account_from_nooks", "account_from_zoominfo",
                        "prospect_from_nooks", "match_account", "build_disposition_index", "call_from_nooks",
                        "task_from_nooks", "sent_emails_from_gmail", "scoop_from_zoominfo", "normalise_phone",
                        "normalise_email_for_match", "normalise_sf_id", "resolve_country", "parse_utc",
                        "disposition_id_for_name"],
        "src.state": ["load_state", "save_state", "record_calls", "record_sent_emails", "record_sheet",
                      "mark_dnc_rebuilt", "require_healthy", "dnc_is_rebuilt", "mark_do_not_call", "last_touched",
                      "cooldown_until", "cooldown_until_by", "is_in_cooldown", "last_sheet_ids", "was_in_last_sheet",
                      "emailed_recently", "verify_dispositions", "StateResetError", "is_blocked"],
        "src.accounts": ["build_account_list", "list_flagged", "confidence", "summarise", "AccountListResult", "Dropped"],
        "src.signals": ["filter_scoops", "scoops_to_signals", "best_signal", "combined_strength", "no_trigger_signal",
                        "trigger_radar", "intent_to_signals", "verdict_summary", "role_match"],
        "src.ranker": ["rank_accounts", "RankResult", "pick_prospect", "pick_prospects", "score_account", "summarise",
                       "is_blocked", "account_key"],
    }
    import importlib
    for mod, names in docs.items():
        m = importlib.import_module(mod)
        for n in names:
            assert hasattr(m, n), f"{mod}.{n}"
    for f in ("flags", "confidence"):                                   # fields missing from interfaces.md table
        assert f in models.Account.__dataclass_fields__
    for f in ("needs_prospect", "spilled", "needs_prospect_codes", "by_window", "prospects", "skipped", "ranked"):
        assert f in ranker.RankResult.__dataclass_fields__


def test_m_contract_signals_keyed_by_sf_or_zoominfo_both_work(world, tmp_path):
    out = run(world, tmp_path)
    a = next(x.account for x in out.result.ranked if x.account.name == name_of(world, 1))
    sigs = out.signals[a.sf_account_id]
    by_sf = ranker.rank_accounts([a], {a.sf_account_id: sigs}, out.prospects, {}, set(), out.cfg, NOW)
    by_zi = ranker.rank_accounts([a], {a.zoominfo_id: sigs}, out.prospects, {}, set(), out.cfg, NOW)
    both = ranker.rank_accounts([a], {a.zoominfo_id: sigs, a.sf_account_id: sigs}, out.prospects, {}, set(), out.cfg, NOW)
    assert by_sf.ranked[0].score == by_zi.ranked[0].score == both.ranked[0].score


def test_m_prospect_to_account_join_paths(world, tmp_path):
    """SF-linked prospect (contact AccountId) and Nooks-only prospect (nooks account id) both reach the account."""
    out = run(world, tmp_path)
    zulu = where(out)[name_of(world, 27)][1]
    got = [p.nooks_id for p in out.result.prospects[ranker.account_key(zulu.account)]]
    assert got == ["FAKE-NK-P-0027-1"]                                  # primary persona wins, one per account
    # without the SF-linked one the Nooks-only prospect is used
    w = _copy.deepcopy(world)
    w.nooks_prospects = [p for p in w.nooks_prospects if p["id"] != "FAKE-NK-P-0027-1"]
    out2 = run(w, tmp_path / "x")
    z2 = where(out2)[name_of(w, 27)][1]
    assert [p.nooks_id for p in out2.result.prospects[ranker.account_key(z2.account)]] == ["FAKE-NK-P-0027-2"]


def test_m_ranker_uses_the_single_signals_combination_formula():
    """review_1g L (fixed in 1f-fixes): ranker.trigger_strength points come from signals.combined_strength."""
    s = [Signal("x", SignalKind.FUNDING, 0.5, "a", None, "e"), Signal("x", SignalKind.FUNDING, 0.4, "b", None, "e")]
    assert sig_mod.combined_strength(s) == 0.6
    s3 = s + [Signal("x", SignalKind.FUNDING, 0.2, "c", None, "e")]
    assert sig_mod.combined_strength(s3) == 0.64
    cfg = load_config()
    acct = Account("FAKE-X", "X", employees=500, timezone="Australia/Sydney")
    _, b = ranker.score_account(acct, s3, False, cfg, date(2026, 11, 26))
    assert b["trigger_strength"] == round(cfg.signals.weights.trigger_strength * 0.64, 2)
    assert not hasattr(ranker, "EXTRA_SIGNAL_BONUS")


def test_m_accounts_docstring_matches_id_normalisation():
    """FIXED (1d): the docstring no longer says 15/18-char ids are NOT normalised; the code does normalise."""
    assert "NOT normalised" not in acc_mod.build_account_list.__doc__     # docstring fixed in phase1/1d-fixes
    r = acc_mod.build_account_list([{"Id": sf18(5), "Name": "A Pty Ltd", "BillingCountry": "Australia",
                                     "BillingState": "New South Wales", "ZoomInfo_Employee_Count__c": 100}], [], [],
                                   load_config(CFG_PATH), {sf15(5): date(2026, 11, 24)}, date(2026, 11, 25))
    assert r.dropped and r.dropped[0].reason == "recently_touched"       # 18-char row, 15-char touched key


def test_m_plus_addressing_is_matched_and_state_notes_say_so():
    assert "Plus-addressing (`a+x@` vs `a@`) is not matched" not in (ROOT / "docs" / "state_notes.md").read_text()
    assert normalise_email_for_match("a+x@Example.com") == normalise_email_for_match("A@example.com") == "a@example.com"


def test_m_guessed_timezone_no_longer_calls_later_but_models_comment_says_so(world, tmp_path):
    """models.RankedAccount / interfaces.md say 'push later inside the window when timezone_guessed'; the ranker
    dropped that rule. Prove it: guessed and confirmed zone share one timeline."""
    b = Builder()
    b.account(1, "Guess Co Pty Ltd", state=None, emp=200)                         # country default -> guessed
    b.prospect(1, 1, HEAD_PROD)
    b.account(2, "Sure Co Pty Ltd", state="New South Wales", emp=200)
    b.prospect(2, 1, HEAD_PROD)
    out = run(b.w, tmp_path)
    g = next(x for x in out.result.ranked if x.account.timezone_guessed)
    s = next(x for x in out.result.ranked if not x.account.timezone_guessed)
    assert g.account.timezone == s.account.timezone
    assert "Timezone guessed (country default)" in g.reasons
    assert min(g.local_call_time, s.local_call_time) == out.result.ranked[0].local_call_time


# ----------------------------------------------------------------------------- (n) tracked leftovers
def test_n_1c_M1_partial_history_is_rejected(world, tmp_path):
    """Fixed: mark_dnc_rebuilt needs reached_last_page=True and calls_seen >= distinct calls already recorded."""
    idx = build_disposition_index(world.dispositions)
    cfg_ = load_config(CFG_PATH)
    calls = [call_from_nooks(r, idx) for r in world.calls]
    st = st_mod.record_calls(State(), calls[:1], [], {}, config=cfg_)       # page 1 of N only
    with pytest.raises(ValueError):                                         # adapter says hasMore is still true
        st_mod.mark_dnc_rebuilt(st, date(2026, 11, 25), 1, reached_last_page=False)
    full = st_mod.record_calls(State(), calls, [], {}, config=cfg_)
    with pytest.raises(ValueError):                                         # count smaller than calls already held
        st_mod.mark_dnc_rebuilt(full, date(2026, 11, 25), 1, reached_last_page=True)
    with pytest.raises(st_mod.StateResetError):
        st_mod.require_healthy(st, live=True)


def test_n_1c_L1_saving_an_empty_state_over_a_populated_file_is_refused(world, tmp_path):
    """Fixed: StateShrinkError unless allow_shrink=True; the old file is kept and a .prev backup exists."""
    out = run(world, tmp_path)
    before = (tmp_path / "state.json").read_text()
    assert json.loads(before)["dnc_prospect_ids"]
    with pytest.raises(st_mod.StateShrinkError):
        st_mod.save_state(State(), tmp_path)
    assert (tmp_path / "state.json").read_text() == before
    st_mod.save_state(State(), tmp_path, allow_shrink=True)                 # explicit override
    assert (tmp_path / "state.json.prev").read_text() == before
    assert not st_mod.dnc_is_rebuilt(st_mod.load_state(tmp_path))            # and the marker gate still catches it


def test_n_1c_L2_malformed_local_date_falls_back_to_the_account_zone(tmp_path):
    """Fixed: Tue 23:00 UTC = Wed 10:00 Sydney; with a corrupt stored date the fallback uses the account zone."""
    st = State(accounts={sf15(1): AccountState(last_call_at=datetime(2026, 11, 24, 23, 0, tzinfo=UTC),
                                              last_call_date="2026-11-25", last_outcome="no_contact")})
    raw = st_mod.state_to_dict(st)
    good = st_mod.state_from_dict(json.loads(json.dumps(raw)))
    raw["accounts"][sf15(1)]["last_call_date"] = "garbage"
    bad = st_mod.state_from_dict(json.loads(json.dumps(raw)))
    cfg_ = load_config(CFG_PATH)
    assert st_mod.cooldown_until(good, cfg_)[sf15(1)] == date(2026, 11, 28)
    assert st_mod.cooldown_until(bad, cfg_)[sf15(1)] == date(2026, 11, 28)  # no longer a day short
    assert any("malformed last_call_date" in w for w in bad.load_warnings)


def test_n_1e_ranker_recency_uses_the_per_kind_limit(world, tmp_path):
    """RESOLVED (1f N5): ranker recency uses signals.max_age_for(best kind): an exec move 60 days old (limit 90) keeps
    partial recency points; config.signals.max_age_days (45) is only the fallback for a kind with no entry."""
    out = run(world, tmp_path)
    a = next(x.account for x in out.result.ranked if x.account.name == name_of(world, 1))
    sig = Signal(a.sf_account_id, SignalKind.EXEC_MOVE, sig_mod.signal_strength(SignalKind.EXEC_MOVE, 60),
                 "zoominfo_scoop:OLD", NOW - timedelta(days=60), "old", a.zoominfo_id)
    r = ranker.rank_accounts([a], {a.sf_account_id: [sig]}, out.prospects, {}, set(), out.cfg, NOW)
    bd = r.ranked[0].score_breakdown
    assert bd["recency"] > 0 and bd["trigger_strength"] > 0
    assert out.cfg.signals.max_age_days == 45
    assert sig_mod.max_age_for(SignalKind.EXEC_MOVE, out.cfg) == 90


def test_n_1f_unmatched_titles_still_unmatched(world, tmp_path, cfg):
    from src.models import Prospect
    for title in ("Product Owner", "Engineering Manager", "Chief Information Officer", "Director of Engineering",
                  "Chief Product & Technology Officer"):
        p = Prospect("n", None, None, None, "X Y", title=title, phone_e164="+61400000001")
        got = ranker.pick_prospect([p], cfg)
        if title == "Chief Product & Technology Officer":
            assert got is not None                      # alias group, documented
        else:
            assert got is None, title                   # Product Owner / Engineering Manager / CIO unmatched


# ----------------------------------------------------------------------------- former 1g findings, now fixed (regression guards)
def test_fixed_h2_intent_never_outranks_a_real_dated_signal(world, tmp_path):
    out = run(world, tmp_path)
    a = next(x.account for x in out.result.ranked if x.account.name == name_of(world, 4))
    fund = Signal(a.sf_account_id, SignalKind.FUNDING, 0.1585, "zoominfo_scoop:F", NOW - timedelta(days=45), "raised", a.zoominfo_id)
    intent = Signal(a.sf_account_id, SignalKind.INTENT, 0.3, "zoominfo_intent:Z:digital experience", NOW,
                    "ZoomInfo intent (UNVERIFIED shape): interest signal, not a dated event.", a.zoominfo_id)
    r = ranker.rank_accounts([a], {a.sf_account_id: [fund, intent]}, out.prospects, {}, set(), out.cfg, NOW)
    ra = r.ranked[0]
    assert ra.reasons[0].startswith("Funding"), ra.reasons[0]
    assert ra.signals[0].kind is SignalKind.FUNDING


def test_fixed_m1_email_touch_treated_the_same_with_and_without_call_history(world, tmp_path):
    out = run(world, tmp_path)
    got = where(out)
    assert got[name_of(world, 28)][0] == got[name_of(world, 29)][0]


def test_fixed_l1_scoop_age_wording_agrees_between_signals_and_ranker(world, tmp_path):
    out = run(world, tmp_path, now=datetime(2026, 11, 25, 20, 30, tzinfo=UTC))
    ra = where(out)[name_of(world, 1)][1]
    v = next(v for v in out.verdicts if v.scoop.company_id == "FAKE-ZI-0001")
    days_v = int(re.search(r"(\d+) days? ago", v.reason).group(1))
    days_r = int(re.search(r"(\d+) days? ago", ra.reasons[0]).group(1))
    assert days_v == days_r


# ----------------------------------------------------------------------------- new contracts after the fixes
def test_contract_signatures_require_the_new_arguments(world, tmp_path):
    """Phase 2 callers cannot forget them: reached_last_page and run_date are required, call_date is accepted."""
    import inspect
    assert inspect.signature(st_mod.mark_dnc_rebuilt).parameters["reached_last_page"].default is inspect.Parameter.empty
    assert inspect.signature(st_mod.last_sheet_ids).parameters["run_date"].default is inspect.Parameter.empty
    assert inspect.signature(st_mod.was_in_last_sheet).parameters["run_date"].default is inspect.Parameter.empty
    assert "call_date" in inspect.signature(acc_mod.build_account_list).parameters
    st = State()
    with pytest.raises(TypeError):
        st_mod.mark_dnc_rebuilt(st, date(2026, 11, 25), 3)                      # type: ignore[call-arg]
    with pytest.raises(TypeError):
        st_mod.last_sheet_ids(st)                                                # type: ignore[call-arg]
    with pytest.raises(ValueError):
        st_mod.mark_dnc_rebuilt(st, date(2026, 11, 25), 3, reached_last_page=False)


def test_contract_new_seat_with_no_calls_needs_explicit_confirmation():
    """FIXED (1c L3): an empty history can be marked rebuilt only with empty_history_confirmed=True."""
    st = State()
    with pytest.raises(ValueError):
        st_mod.mark_dnc_rebuilt(st, date(2026, 11, 25), 0, reached_last_page=True)
    st = st_mod.mark_dnc_rebuilt(st, date(2026, 11, 25), 0, reached_last_page=True, empty_history_confirmed=True)
    st_mod.require_healthy(st, live=True)


def test_contract_accounts_ignore_touched_when_cooldown_until_is_given(world, tmp_path):
    """FIXED (1d M1): an explicit touch map cannot drop an account when cooldown_until is supplied (even empty)."""
    cfg_ = load_config(CFG_PATH)
    kw = dict(blocked_account_ids=set(), cooldown_until={}, call_date=date(2026, 11, 26))
    r = acc_mod.build_account_list(world.sf_rows, world.nooks_accounts, world.zoominfo_rows, cfg_,
                                   {sf15(1): date(2026, 11, 25)}, date(2026, 11, 25), **kw)
    assert name_of(world, 1) in {a.name for a in r.accounts}
    legacy = acc_mod.build_account_list(world.sf_rows, world.nooks_accounts, world.zoominfo_rows, cfg_,
                                        {sf15(1): date(2026, 11, 25)}, date(2026, 11, 25))
    assert name_of(world, 1) in {d.name for d in legacy.dropped}                 # legacy path still available


def test_contract_run_log_names_every_skipped_needs_prospect_and_spilled_account(tmp_path):
    """FIXED (1f L7, rule 5): the log names accounts and says why; bounded by list_limit with an 'and N more' tail."""
    w = mini_world(25)
    out = run(w, tmp_path, now=datetime(2026, 11, 25, 5, 10, tzinfo=UTC))
    log = out.log
    r = out.result
    assert r.spilled and r.needs_prospect
    for x in r.spilled + r.needs_prospect:
        assert x.account.name in log
    assert "SPILLED" in log and "NEEDS PROSPECT" in log
    big = run(scale_world(600, seed=5), tmp_path / "big")
    names = [x.account.name for x in big.result.skipped[:25]]
    assert all(n in big.log for n in names)
    assert re.search(r"and \d+ more", big.log)
    assert len(big.log.split("== RANKER ==")[1].splitlines()) < 200              # ranker section bounded at scale


def test_contract_state_last_run_counts_survive_save_and_load(world, tmp_path):
    """FIXED (1c L4): counts from record_calls / record_sent_emails / record_sheet are saved (counts only, no ids)."""
    run(world, tmp_path)
    st = st_mod.load_state(tmp_path)
    assert st.last_run["record_calls"]["calls_seen"] >= 1
    assert st.last_run["record_sheet"]["recorded"] >= 1
    assert st.last_run["record_sent_emails"]["new"] == 2
    text = (tmp_path / "state.json").read_text()
    assert "@" not in text


def test_contract_accounts_without_a_salesforce_id_are_counted_not_silent(tmp_path):
    """FIXED (1c L6, partly): record_sheet counts ids it cannot store. They still cannot be hard-skipped."""
    st = st_mod.record_sheet(State(), date(2026, 11, 25), [sf15(1), None, None])
    assert st.report["record_sheet"] == {"recorded": 1, "dropped_no_id": 2}


def test_n2_early_zone_account_in_cooldown_is_skipped_not_scheduled(tmp_path):
    """FIXED (review_1g N2, 1f-final). accounts gets ONE call_date (Sydney's first usable day: Friday on a Thu 16:28 run), so
    a Perth account whose cooldown ends Friday is KEPT by accounts. The ranker judges the cooldown on the account's OWN
    zone call date (Perth: Thursday) and SKIPS it ('In cooldown until ...'), so no task falls inside a cooldown."""
    b = Builder()
    b.account(1, "Perth Cool Pty Ltd", state="Western Australia", emp=200)
    p = b.prospect(1, 1, HEAD_PROD)
    b.scoop(1, "Funding", "Perth raised $5 million in funding.", 2, topics=("Funding",))
    b.call(p, "No Answer", 0.6, duration=30)                                    # Tue 24 Nov local: cooldown ends Fri 27
    b.account(2, "Syd Cool Pty Ltd", state="New South Wales", emp=200)
    q = b.prospect(2, 1, HEAD_PROD)
    b.scoop(2, "Funding", "Syd raised $5 million in funding.", 2, topics=("Funding",))
    b.call(q, "No Answer", 0.6, duration=30)
    out = run(b.w, tmp_path, now=datetime(2026, 11, 26, 5, 28, tzinfo=UTC))
    assert out.cooldown[sf15(1)] == date(2026, 11, 27)
    got = {x.account.name: x for x in out.result.ranked}
    assert "Perth Cool Pty Ltd" not in got
    assert got["Syd Cool Pty Ltd"].local_call_time.date() == date(2026, 11, 27)
    sk = {x.account.name: x.skipped_reason for x in out.result.skipped}
    assert sk["Perth Cool Pty Ltd"] == "In cooldown until 2026-11-27 (account-local)"
    assert all(x.local_call_time.date() >= date(2026, 11, 27) for x in out.result.ranked + out.result.spilled
               if x.account.name.endswith("Cool Pty Ltd"))


# ----------------------------------------------------------------------------- state gate / dry run wiring
def test_gate_live_requires_marker_and_dry_run_only_warns(tmp_path):
    st = st_mod.load_state(tmp_path)
    assert st.is_new
    with pytest.raises(StateResetError):
        st_mod.require_healthy(st, live=True)
    st_mod.require_healthy(st, live=False)
    with pytest.raises(ValueError):
        st_mod.mark_dnc_rebuilt(st, date(2026, 11, 25), 0, reached_last_page=True)      # 0 needs confirmation
    with pytest.raises(ValueError):
        st_mod.mark_dnc_rebuilt(st, date(2026, 11, 25), 3, reached_last_page=True)      # nothing was recorded
    st = st_mod.mark_dnc_rebuilt(st, date(2026, 11, 25), 0, reached_last_page=True, empty_history_confirmed=True)
    st_mod.require_healthy(st, live=True)
    st_mod.save_state(st, tmp_path)
    assert st_mod.dnc_is_rebuilt(st_mod.load_state(tmp_path))


def test_sent_email_history_and_hashed_at_rest(world, tmp_path):
    out = run(world, tmp_path)
    st = out.state_after
    assert st.report["record_sent_emails"]["new"] == 2
    emailed, why = st_mod.emailed_recently(st, "amy.mailed@fake28.example.com", date(2026, 11, 25), 7)
    assert emailed and "1 day" in why
    assert not st_mod.emailed_recently(st, "amy.mailed@fake28.example.com", date(2026, 12, 20), 7)[0]
    assert normalise_email_for_match("Amy.Mailed@fake28.example.com") == "amymailed@fake28.example.com"


def test_ranker_never_returns_more_than_cap_even_with_many_zones(tmp_path):
    out = run(scale_world(800, seed=11), tmp_path)
    assert len(out.result.ranked) <= CAP
    assert len(out.result.ranked) + len([s for s in out.result.skipped if s.skipped_reason.startswith("below daily cap")]) \
        >= len(out.result.ranked)


# ----------------------------------------------------------------------------- command line helpers
def _main(argv):
    if "--dump-fixture" in argv:
        (FIX / "integration_world.json").write_text(json.dumps(named_world().to_dict(), indent=1, sort_keys=False) + "\n")
        print("wrote", FIX / "integration_world.json")
    if "--sample-log" in argv:
        import tempfile
        d = pathlib_Path(tempfile.mkdtemp())
        st_mod.save_state(st_mod.record_sheet(State(), date(2026, 11, 24), [sf18(10)]), d)
        late = "--late" in argv                  # Wed 16:12 AEDT: Sydney has 5 slots left, so some accounts spill
        print(run_pipeline(named_world(), d, now=datetime(2026, 11, 25, 5, 12, tzinfo=UTC) if late else NOW,
                           mark_sheet=False).log)
    if "--scale" in argv:
        import tempfile, resource, tracemalloc
        n_ = int(argv[argv.index("--scale") + 1]) if argv.index("--scale") + 1 < len(argv) else 3000
        page_ = int(argv[argv.index("--call-page") + 1]) if "--call-page" in argv else 7
        w = scale_world(n_, seed=7)
        t0 = time.perf_counter()
        out = run_pipeline(w, pathlib_Path(tempfile.mkdtemp()), now=NOW, call_page=page_)
        wall = time.perf_counter() - t0
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        tracemalloc.start()
        run_pipeline(w, pathlib_Path(tempfile.mkdtemp()), now=NOW, call_page=page_)
        peak = tracemalloc.get_traced_memory()[1] / 1e6
        print(json.dumps({"wall_s": round(wall, 2), "stages_s": {k: round(v, 3) for k, v in out.timings.items()},
                          "py_heap_peak_mb_during_run": round(peak, 1), "maxrss_mb_whole_process": round(rss, 1),
                          "ranked": len(out.result.ranked), "needs": len(out.result.needs_prospect),
                          "skipped": len(out.result.skipped), "dropped": len(out.accounts.dropped),
                          "sf_accounts": len(w.sf_rows), "zoominfo_rows": len(w.zoominfo_rows),
                          "prospects": len(w.nooks_prospects), "calls": len(w.calls), "scoops": len(w.scoops)}, indent=1))


from pathlib import Path as pathlib_Path  # noqa: E402

if __name__ == "__main__":
    _main(sys.argv[1:])
