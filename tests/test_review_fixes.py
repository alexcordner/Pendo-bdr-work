"""Tests added after the 1b review: phones, flagged-account timezone, config validation, new loaders."""
from datetime import date, datetime, timezone

import pytest

from src.config import (ConfigError, next_working_windows, parse_config, tier_for, window_bounds)
from src.loaders import (LoaderError, account_from_nooks, account_from_sf_row, account_from_zoominfo,
                         build_disposition_index, call_from_nooks, disposition_id_for_name, match_account,
                         normalise_email_for_match, normalise_phone, prospect_from_nooks,
                         scoop_from_zoominfo, sent_emails_from_gmail, task_from_nooks)
from src.models import Account, Signal, SignalKind
from tests.conftest import load_fixture

# (input, account_country, expected) -- every case from the reviewer's probe table.
PHONES = [
    ("0400 000 001", "Australia", "+61400000001"),
    ("(02) 9000 0001", "Australia", "+61290000001"),
    ("(04) 0000 0001", "Australia", "+61400000001"),
    ("+61 (0)4 0000 0001", None, "+61400000001"),
    ("+61 (0)4 0000 0001", "Australia", "+61400000001"),
    ("61 (0)400 000 001", None, "+61400000001"),
    ("0061 4 0000 0001", None, "+61400000001"),
    ("+61 400 000 001 ext 12", None, "+61400000001"),
    ("04000 00001 x123", "Australia", "+61400000001"),
    ("+61400000001;ext=1", None, "+61400000001"),
    ("+61400000001#1", None, "+61400000001"),
    ("0011 61 400000001", None, "+61400000001"),
    ("0000000000", "Australia", None),
    ("+0061400000001", None, None),
    ("61000000000", None, None),
    ("0400000001/0400000002", "Australia", None),
    ("0400", "Australia", None), ("+61 4", None, None), ("9000 0001", "Australia", None),
    ("1300 000 000", "Australia", None), ("1800123456", "Australia", None),
    ("021 000 0001", "New Zealand", "+64210000001"),
    ("+64 21 000 0001", "Australia", "+64210000001"),
    ("64210000001", "New Zealand", "+64210000001"),
    ("0800 123 456", "New Zealand", "+64800123456"),
    ("+1 415 555 0100", "Australia", "+14155550100"),
    ("tel:+61400000001", None, "+61400000001"),
    ("０４０００００００１", "Australia", None),       # fullwidth digits
    ("123456789012345678", None, None),
    ("+61 4000 0000 01", None, None),                    # too many digits for AU
    ("+64 1", None, None),
    (None, None, None), ("", None, None), (12345, None, None),
]


@pytest.mark.parametrize("raw,country,expected", PHONES, ids=[str(p[0]) for p in PHONES])
def test_phone_review_cases(raw, country, expected):
    assert normalise_phone(raw, country) == expected


# ---- flagged account timezone
def _row(**kw):
    base = {"Id": "FAKE-X", "Name": "X Pty Ltd", "ZoomInfo_Employee_Count__c": 100}
    base.update(kw)
    return base


def test_us_wa_billing_never_becomes_perth(config):
    a = account_from_sf_row(_row(BillingCountry="United States", BillingState="WA", BillingCity="Seattle",
                                 HQ_Country__c="Australia", HQ_State_Province__c="Victoria"), config)
    assert a.non_anz_billing_anz_hq and a.timezone == "Australia/Melbourne" and not a.timezone_guessed
    b = account_from_sf_row(_row(BillingCountry="United States", BillingState="WA", BillingCity="Perth",
                                 HQ_Country__c="AU"), config)
    assert b.timezone == "Australia/Sydney" and b.timezone_guessed    # billing ignored, HQ state missing


def test_junk_billing_country_is_not_flagged():
    a = account_from_sf_row(_row(BillingCountry="N/A", HQ_Country__c="AU"))
    assert a.country == "Australia" and not a.non_anz_billing_anz_hq
    assert account_from_sf_row(_row(BillingCountry=5, HQ_Country__c="AU")).country == "Australia"
    assert account_from_sf_row(_row(BillingCountry="New  Zealand")).country == "New Zealand"


# ---- config validation
def _bad(raw_config, mutate, match):
    mutate(raw_config)
    with pytest.raises(ConfigError, match=match):
        parse_config(raw_config)


@pytest.mark.parametrize("mutate,match", [
    (lambda r: r["limits"].__setitem__("daily_call_cap", -5), r"limits\.daily_call_cap"),
    (lambda r: r["limits"].__setitem__("daily_call_cap", 0), r"limits\.daily_call_cap"),
    (lambda r: r["limits"].__setitem__("zoominfo_credit_budget_per_run", -1), "zoominfo_credit"),
    (lambda r: r["signals"].__setitem__("max_age_days", 0), "max_age_days"),
    (lambda r: r["signals"]["weights"].__setitem__("cooldown_penalty", 30), "cooldown_penalty"),
    (lambda r: r["signals"]["weights"].__setitem__("icp_fit", -1), "icp_fit"),
    (lambda r: r["tiers"]["tier1"].__setitem__("employees", [2000, 200]), "tier1"),
    (lambda r: r["tiers"].__delitem__("tier1"), "tiers.tier1"),
    (lambda r: r["tiers"]["tier2"].__setitem__("employees", [50, 250]), "overlap"),
    (lambda r: r["limits"].__setitem__("daily_cal_cap", 3), "daily_cal_cap"),
    (lambda r: r.__setitem__("surprise", 1), "surprise"),
    (lambda r: r["territory"]["timezones"]["Australia"].__setitem__("Queenland", "Australia/Brisbane"), "Queenland"),
    (lambda r: r["territory"]["timezones"]["New Zealand"].__setitem__("Auckland", "Pacific/Auckland"), "Auckland"),
    (lambda r: r.__setitem__("sources", ["salesforce", "hubspot"]), "hubspot"),
    (lambda r: r["territory"].__setitem__("priority_countries", ["France"]), "France"),
    (lambda r: r["owner"].__setitem__("email", "nope"), "owner.email"),
    (lambda r: r["paths"].__setitem__("log_dir", ""), "paths.log_dir"),
    (lambda r: r.__setitem__("working_days", ["Mon", "Funday"]), "Funday"),
    (lambda r: r.__delitem__("dispositions"), "dispositions"),
    (lambda r: r["dispositions"].__setitem__("voicemail", ""), "dispositions.voicemail"),
])
def test_invalid_config_variants(raw_config, mutate, match):
    _bad(raw_config, mutate, match)


def test_new_config_sections(config):
    assert config.dispositions.voicemail == "Left Voicemail"
    assert config.limits.no_contact_max_duration_s == 20
    assert config.signals.weights.sdr_target_boost == 10
    assert config.working_days == (0, 1, 2, 3, 4)


def test_configured_disposition_names_exist_in_fixture(config):
    idx = build_disposition_index(load_fixture("nooks_call_dispositions.json")["data"])
    for name in vars(config.dispositions).values():
        assert disposition_id_for_name(idx, name), name
    assert disposition_id_for_name(idx, "  left   VOICEMAIL ") == "disp-voicemail"


def test_tier_for(config):
    assert [tier_for(config, n) for n in (None, 10, 20, 49, 50, 199, 200, 2000, 2001)] == \
        [None, None, "tier3", "tier3", "tier2", "tier2", "tier1", "tier1", None]


# ---- call windows
def test_window_bounds_and_weekends(config):
    w = window_bounds(config, "Australia/Sydney", date(2026, 11, 25))     # Wednesday, AEDT
    assert [x.name for x in w] == ["morning", "afternoon"]
    assert w[0].start.isoformat() == "2026-11-25T08:45:00+11:00"
    assert window_bounds(config, "Australia/Brisbane", date(2026, 11, 25))[0].start.utcoffset().total_seconds() == 36000
    assert window_bounds(config, "Australia/Sydney", date(2026, 11, 28)) == []   # Saturday


def test_next_working_windows(config):
    # Fri 2026-11-27 17:00 Sydney (06:00 UTC) -> windows are over, next is Monday
    now = datetime(2026, 11, 27, 6, 0, tzinfo=timezone.utc)
    nxt = next_working_windows(config, "Australia/Sydney", now)
    assert nxt[0].start.date() == date(2026, 11, 30) and len(nxt) == 2
    # Wed 10:00 Sydney -> today's morning (still open) and afternoon
    now = datetime(2026, 11, 24, 23, 0, tzinfo=timezone.utc)   # Wed 10:00 AEDT
    assert [w.name for w in next_working_windows(config, "Australia/Sydney", now)] == ["morning", "afternoon"]
    now = datetime(2026, 11, 25, 3, 0, tzinfo=timezone.utc)    # Wed 14:00 AEDT -> afternoon only
    assert [w.name for w in next_working_windows(config, "Australia/Sydney", now)] == ["afternoon"]
    with pytest.raises(ValueError):
        next_working_windows(config, "Australia/Sydney", datetime(2026, 11, 25))


# ---- new loaders
def test_account_from_nooks_and_zoominfo_and_join(config):
    n = account_from_nooks(load_fixture("nooks_account.json"), sf_account_id="FAKE-001-001")
    assert (n.source_nooks, n.nooks_account_id, n.sf_account_id, n.employees, n.website) == \
        (True, "FAKE-NOOKS-ACCT-1", "FAKE-001-001", 215, "example-widgets.test")
    z = account_from_zoominfo(load_fixture("zoominfo_search_companies.json")["data"][0])
    assert z.source_zoominfo and z.zoominfo_id == "FAKE-ZI-1001" and z.name == "Example Widgets Pty Ltd"
    assert z.size_unknown and "size_unknown" in z.notes
    flat = account_from_zoominfo(load_fixture("zoominfo_noise_examples.json")["location_or_local_business"][0])
    assert flat.name == "Fakeville CBD"
    # Nooks-only prospect (no SF contact) joins via nooks_account_id
    p = prospect_from_nooks(load_fixture("nooks_prospect.json")["data"][0])
    assert p.sf_account_id is None and match_account(p, [z, n]) is n
    # SF-linked prospect joins via sf_account_id
    contact = load_fixture("sf_contact_row.json")["records"][0]
    p2 = prospect_from_nooks(load_fixture("nooks_prospect.json")["data"][0], contact)
    assert match_account(p2, [Account(None, "x"), n]) is n
    assert match_account(p, [Account(None, "other")]) is None


def test_size_unknown_kept_and_flagged():
    a = account_from_sf_row(_row(ZoomInfo_Employee_Count__c=None, BillingCountry="Australia"))
    assert a.size_unknown and "size_unknown" in a.notes and a.country == "Australia"
    assert account_from_sf_row(_row(ZoomInfo_Employee_Count__c="1,200")).employees == 1200


def test_tasks_and_manual_email_backlog():
    tasks = [task_from_nooks(r) for r in load_fixture("nooks_tasks_sample.json")["data"]]
    assert tasks[0].action == "call" and not tasks[0].is_manual_email and tasks[0].in_sequence
    assert tasks[1].is_manual_email and tasks[1].email_id == "FAKE-EMAIL-1"
    assert tasks[1].due_at.tzinfo is not None and tasks[1].prospect_id == "FAKE-NOOKS-PROSPECT-2"


def test_email_match_key_and_sent_mail():
    assert normalise_email_for_match(" First.Last@Example.COM ") == "firstlast@example.com"
    assert normalise_email_for_match("first-last@example.com") == "first-last@example.com"
    assert normalise_email_for_match("nope") is None and normalise_email_for_match(None) is None
    sent = sent_emails_from_gmail(load_fixture("gmail_search_sent.json"))
    assert len(sent) == 1 and sent[0].recipient == "firstlast@example.com"
    assert sent[0].recipient_key == normalise_email_for_match("first.last@example.com")
    assert sent[0].thread_id == "FAKE-THREAD-1" and sent[0].sent_at is None   # fixture has no date
    dated = {"threads": [{"id": "T", "messages": [{"id": "M", "toRecipients": ["a.b@x.com"],
                                                   "internalDate": "1795600000000"}]}]}
    assert sent_emails_from_gmail(dated)[0].sent_at.tzinfo is not None
    assert sent_emails_from_gmail({"threads": []}) == []


def test_malformed_rows_raise_loader_error():
    idx = build_disposition_index(load_fixture("nooks_call_dispositions.json")["data"])
    good_call = load_fixture("nooks_calls_sample.json")["data"][0]
    for fn, arg in [
        (account_from_sf_row, {"Name": "no id"}), (account_from_sf_row, None),
        (account_from_nooks, {"name": "no id"}), (account_from_zoominfo, {"attributes": {}}),
        (prospect_from_nooks, {"name": "no id"}), (task_from_nooks, {}),
        (scoop_from_zoominfo, {"attributes": {"id": "S"}}),
        (scoop_from_zoominfo, {"id": "S", "attributes": {"publishedDate": "2026-11-20T00:00:00Z",
                                                          "types": [{"nope": 1}]}}),
        (sent_emails_from_gmail, {"threads": [{"messages": [{"toRecipients": ["a@b.com"]}]}]}),
    ]:
        with pytest.raises(LoaderError):
            fn(arg)
    with pytest.raises(LoaderError):
        build_disposition_index([{"id": "x"}])
    with pytest.raises(LoaderError):
        call_from_nooks(dict(good_call, duration="abc"), idx)
    with pytest.raises(LoaderError):
        call_from_nooks({"id": "C"}, idx)
    with pytest.raises(LoaderError):
        call_from_nooks(None, idx)


# ---- models
def test_signal_validation():
    ok = Signal("A", SignalKind.FUNDING, 0.5, "zoominfo_scoop:1", None, "x")
    assert ok.kind.value == "funding"
    with pytest.raises(ValueError):
        Signal("A", "funding", 0.5, "s", None, "x")
    with pytest.raises(ValueError):
        Signal("A", SignalKind.NONE, 1.5, "s", None, "x")
    assert {k.value for k in SignalKind} == {"funding", "exec_move", "new_hire", "ma", "product_launch",
                                             "partnership", "project_pain", "hiring_plan", "intent", "none"}


# ---- review-driven (1d L3, 1c F10): shared id / email normalisers --------------------------------
def test_normalise_sf_id_15_vs_18():
    from src.loaders import normalise_sf_id
    assert normalise_sf_id("001000000000001AAA") == "001000000000001"
    assert normalise_sf_id("001000000000001") == "001000000000001"
    assert normalise_sf_id(" 001Ab000000XyZq ") == "001Ab000000XyZq"       # case kept, trimmed
    assert normalise_sf_id("FAKE-001") == "FAKE-001"                      # synthetic ids untouched
    assert normalise_sf_id("") is None and normalise_sf_id(None) is None


def test_normalise_email_plus_addressing():
    from src.loaders import normalise_email_for_match as n
    assert n("First.Last+crm@Example.com") == n("firstlast@example.com") == "firstlast@example.com"
