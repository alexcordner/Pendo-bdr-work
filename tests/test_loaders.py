import pytest

from src.loaders import (account_from_sf_row, build_disposition_index, call_from_nooks,
                         disposition_id_for_name, normalise_phone, prospect_from_nooks,
                         resolve_country, scoop_from_zoominfo)
from tests.conftest import load_fixture


# ---- phones
@pytest.mark.parametrize("v", load_fixture("phone_formats.json")["test_vectors"],
                         ids=lambda v: v["in"])
def test_phone_vectors(v):
    assert normalise_phone(v["in"], v["account_country"]) == v["out"]


@pytest.mark.parametrize("raw", [None, "", "abc", "12345", "0400000000", "+1"])
def test_phone_unusable(raw):
    # national '0' number without a country, or too short/garbage -> None
    assert normalise_phone(raw) is None


def test_phone_observed_match_sf_vs_nooks():
    f = load_fixture("phone_formats.json")
    assert normalise_phone("+61 400 000 008") == "+61400000008"
    for raw in f["nooks_call_to_examples"]:
        assert normalise_phone(raw).startswith("+")


# ---- country rule: every account fixture row
EXPECTED = {
    # id: (country, non_anz_billing_anz_hq)
    "FAKE-001-001": ("Australia", False),
    "FAKE-001-002": ("Australia", False),
    "FAKE-001-003": ("New Zealand", False),
    "FAKE-001-004": ("Australia", False),
    "FAKE-001-005": ("Australia", False),
    "FAKE-001-006": ("Australia", False),   # AUSTRALIA mixed case
    "FAKE-001-007": ("Australia", False),   # null billing, HQ 'AU'
    "FAKE-001-008": ("Australia", True),    # US billing, AU HQ -> flagged
    "FAKE-001-009": ("Australia", False),   # customer: still ANZ; 1d drops it
    "FAKE-001-010": ("New Zealand", False),
    "FAKE-001-011": ("New Zealand", False),
    "FAKE-001-012": (None, False),          # Singapore
}


def test_country_rule_every_fixture_row(config):
    rows = load_fixture("sf_accounts_my_book.json")["records"]
    assert {r["Id"] for r in rows} == set(EXPECTED)
    for r in rows:
        a = account_from_sf_row(r, config)
        assert (a.country, a.non_anz_billing_anz_hq) == EXPECTED[r["Id"]], r["Id"]
        assert a.source_salesforce and a.sf_account_id == r["Id"]


def test_account_fields_and_timezone(config):
    rows = {r["Id"]: r for r in load_fixture("sf_accounts_my_book.json")["records"]}
    a = account_from_sf_row(rows["FAKE-001-001"], config)
    assert (a.name, a.employees, a.is_sdr_target, a.is_customer, a.zoominfo_id) == \
        ("Example Widgets Pty Ltd", 215, True, False, "FAKE-ZI-1001")
    assert a.timezone == "Australia/Sydney" and not a.timezone_guessed
    assert account_from_sf_row(rows["FAKE-001-009"], config).is_customer
    assert account_from_sf_row(rows["FAKE-001-010"], config).zoominfo_id is None
    assert account_from_sf_row(rows["FAKE-001-007"], config).timezone == "Australia/Brisbane"  # HQ state
    guess = account_from_sf_row(rows["FAKE-001-004"], config)
    assert guess.timezone_guessed and any("timezone" in n for n in guess.notes)
    sg = account_from_sf_row(rows["FAKE-001-012"], config)
    assert sg.timezone is None and sg.notes
    # without config, timezone is left unresolved
    assert account_from_sf_row(rows["FAKE-001-001"]).timezone is None


@pytest.mark.parametrize("b,h,exp", [
    ("australia", None, ("Australia", False)), ("NZ", "AU", ("New Zealand", False)),
    (None, "new zealand", ("New Zealand", False)), ("United States", "NZ", ("New Zealand", True)),
    ("", "AU", ("Australia", False)), ("France", "Germany", (None, False)), (None, None, (None, False)),
])
def test_resolve_country(b, h, exp):
    r = resolve_country(b, h)
    assert (r.country, r.non_anz_billing_anz_hq) == exp


# ---- dispositions and calls
def test_disposition_resolution_by_name():
    idx = build_disposition_index(load_fixture("nooks_call_dispositions.json")["data"])
    rows = load_fixture("nooks_calls_sample.json")["data"]
    calls = [call_from_nooks(r, idx) for r in rows]
    names = {c.disposition_name for c in calls}
    assert {"No Answer", "Left Voicemail", "Do Not Call", "Gatekeeper", "Meeting Scheduled"} <= names
    assert None not in names
    vm = [c for c in calls if c.disposition_name == "Left Voicemail"]
    assert len(vm) == 4 and all(c.call_outcome == "No Answer" for c in vm)
    assert disposition_id_for_name(idx, "Do Not Call") == "disp-do-not-call"
    assert disposition_id_for_name(idx, "Nope") is None


def test_ids_are_not_hard_coded():
    """Changing the id for a name changes what calls resolve to: resolution is by lookup only."""
    rows = load_fixture("nooks_call_dispositions.json")["data"]
    remapped = [dict(r, id="X-" + r["id"]) for r in rows]
    idx = build_disposition_index(remapped)
    call = dict(load_fixture("nooks_calls_sample.json")["data"][0],
                callDisposition={"id": "X-disp-voicemail"})
    assert call_from_nooks(call, idx).disposition_name == "Left Voicemail"


def test_call_shape_and_unlinked_and_unknown_disposition():
    idx = build_disposition_index(load_fixture("nooks_call_dispositions.json")["data"])
    rows = load_fixture("nooks_calls_sample.json")["data"]
    c = call_from_nooks(rows[0], idx)
    assert c.prospect_id == "FAKE-NOOKS-PROSPECT-2" and c.to_e164 == "+61400000001"
    assert c.started_at.tzinfo is not None and c.duration_s == 0
    unlinked = [call_from_nooks(r, idx) for r in rows if r["prospect"] is None]
    assert unlinked and all(u.prospect_id is None and u.to_e164 for u in unlinked)
    odd = call_from_nooks(dict(rows[0], callDisposition={"id": "ghost"}), idx)
    assert odd.disposition_name is None and odd.disposition_id == "ghost"
    none = call_from_nooks(dict(rows[0], callDisposition=None), idx)
    assert none.disposition_name is None


# ---- prospects and scoops
def test_prospect_joined_to_contact():
    nooks = load_fixture("nooks_prospect.json")["data"]
    contacts = {c["Id"]: c for c in load_fixture("sf_contact_row.json")["records"]}
    p = prospect_from_nooks(nooks[0], contacts["FAKE-003-001"], "Australia")
    assert (p.sf_contact_id, p.sf_account_id, p.phone_e164) == ("FAKE-003-001", "FAKE-001-001", "+61400000001")
    assert p.email == "first.last@example.com" and not p.opted_out_email and not p.do_not_call
    p2 = prospect_from_nooks(nooks[1], contacts["FAKE-003-002"], "Australia")
    assert p2.opted_out_email and p2.phone_e164 == "+61200000002"
    p3 = prospect_from_nooks(nooks[2])           # csv prospect, no contact
    assert p3.sf_contact_id is None and p3.phone_e164 is None


def test_scoops_load():
    rows = load_fixture("zoominfo_scoops.json")["data"]
    scoops = [scoop_from_zoominfo(r) for r in rows]
    assert len(scoops) == 14
    s = scoops[1]
    assert s.id == "FAKE-SCOOP-2" and s.company_id == "FAKE-ZI-1002"
    assert s.types == ("New Hire", "Executive Move") and s.published_date.tzinfo is not None
