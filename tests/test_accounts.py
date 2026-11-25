"""Tests for src/accounts.py (task 1d). Synthetic fixtures only; fully offline."""
import copy
from datetime import date, timedelta

import pytest

from src.accounts import (AccountListResult, Dropped, build_account_list, confidence, duplicate_groups,
                          flag_probable_duplicates, group_by_zoominfo_id, list_flagged, registrable_domain,
                          noise_reason, normalise_name, summarise)
from src.models import Account
from tests.conftest import load_fixture

TODAY = date(2026, 11, 25)
CASES = load_fixture("accounts_cases.json")
NOISE = load_fixture("zoominfo_noise_examples.json")
BOOK = load_fixture("sf_accounts_my_book.json")["records"]


def build(sf=(), nk=(), zi=(), config=None, touched=None, blocked=frozenset(), **kw):
    return build_account_list(list(sf), list(nk), list(zi), config, touched or {}, TODAY, blocked, **kw)


def sf_row(i="FAKE-T-1", name="Test Co Pty Ltd", emp=100, **kw):
    r = {"Id": i, "Name": name, "BillingCountry": "Australia", "BillingState": "Victoria",
         "HQ_Country__c": "Australia", "ZoomInfo_Employee_Count__c": emp, "Current_Customer__c": False}
    r.update(kw)
    return r


def by_name(res, name):
    return next(a for a in res.accounts if a.name == name)


def dropped_for(res, name):
    return [d for d in res.dropped if d.name == name]


# ------------------------------------------------------------ noise fixtures (spec)
LOCATION = NOISE["location_or_local_business"]
ALL_NOISE = [r for k, v in NOISE.items() if not k.startswith("_") for r in v]


@pytest.mark.parametrize("row", LOCATION, ids=lambda r: r["name"])
def test_location_noise_example_dropped(config, row):
    res = build(zi=[row], config=config)
    assert res.accounts == []
    assert res.dropped[0].reason in ("noise_location", "noise_local_business")


def test_pure_location_reason(config):
    res = build(zi=[LOCATION[0]], config=config)
    assert res.dropped[0].reason == "noise_location"


def test_local_business_reason(config):
    res = build(zi=[LOCATION[1]], config=config)
    assert res.dropped[0].reason == "noise_local_business"


@pytest.mark.parametrize("row", NOISE["government_or_non_company"], ids=lambda r: r["name"])
def test_government_example_dropped(config, row):
    res = build(zi=[row], config=config)
    assert res.dropped[0].reason == "noise_government"


def test_not_anz_example_dropped(config):
    res = build(zi=NOISE["not_anz"], config=config)
    assert res.dropped[0].reason == "not_anz"


def test_vague_example_dropped(config):
    res = build(zi=NOISE["vague_name"], config=config)
    assert res.dropped[0].reason == "noise_vague_name"


def test_all_ten_noise_examples_dropped_none_kept(config):
    res = build(zi=ALL_NOISE, config=config)
    assert len(ALL_NOISE) == 10
    assert res.accounts == [] and len(res.dropped) == 10
    assert sum(res.stats.values()) == 10


@pytest.mark.parametrize("row", CASES["extra_noise_zoominfo"], ids=lambda r: r["name"])
def test_extra_noise_dropped(config, row):
    assert build(zi=[row], config=config).accounts == []


def test_bare_city_name_is_location(config):
    assert build(zi=[{"id": "Z", "name": "Sydney"}], config=config).dropped[0].reason == "noise_location"


def test_city_inside_real_company_name_kept(config):
    res = build(zi=[{"id": "Z1", "name": "Sydney Software Pty Ltd"}], config=config)
    assert [a.name for a in res.accounts] == ["Sydney Software Pty Ltd"]


def test_salesforce_book_cbd_name_kept_and_flagged(config):
    res = build(sf=[sf_row(name="Fakeville CBD Florist", emp=None)], config=config)
    a = res.accounts[0]
    assert a.size_unknown and "noise_local_business" in a.flags and a.confidence == "low"


def test_large_cbd_named_company_kept(config):
    nk = {"id": "N1", "name": "Fakeville CBD Logistics", "numEmployees": 300}
    assert len(build(nk=[nk], config=config).accounts) == 1


def test_noise_reason_helper_none_for_real_company(config):
    assert noise_reason(Account(None, "Example Widgets Pty Ltd"), config) is None


# ------------------------------------------------------------ basic keep / drop
def test_real_book_fixture_outcomes(config):
    res = build(sf=BOOK, config=config)
    kept = {a.sf_account_id for a in res.accounts}
    assert "FAKE-001-001" in kept and "FAKE-001-008" in kept   # normal + flagged group
    reasons = {d.id: d.reason for d in res.dropped}
    assert reasons["FAKE-001-009"] == "customer"
    assert reasons["FAKE-001-012"] == "not_anz"
    assert "FAKE-001-011" in kept                              # 33 employees is above the floor of 20
    assert len(res.accounts) == 10 and len(res.dropped) == 2


# ------------------------------------------------------------ customers, ANZ, size
def test_customer_dropped(config):
    res = build(sf=[sf_row(Current_Customer__c=True)], config=config)
    assert res.dropped[0].reason == "customer" and res.dropped[0].source == "salesforce"


def test_customer_flag_survives_merge_from_other_source(config):
    # Nooks row would not say customer; SF row does and wins
    res = build(sf=[sf_row("S1", "Acme Pty Ltd", Current_Customer__c=True)],
                nk=[{"id": "N1", "name": "Acme", "_sf_account_id": "S1"}], config=config)
    assert res.stats == {"customer": 1}


def test_non_anz_salesforce_dropped(config):
    res = build(sf=[sf_row(BillingCountry="United States", HQ_Country__c="United States")], config=config)
    assert res.dropped[0].reason == "not_anz"


def test_nz_kept(config):
    res = build(sf=[sf_row(BillingCountry="New Zealand", HQ_Country__c="NZ", BillingState="Auckland")],
                config=config)
    assert res.accounts[0].country == "New Zealand"


def test_size_above_cap_dropped(config):
    assert build(sf=[sf_row(emp=2001)], config=config).dropped[0].reason == "over_max_employees"


def test_size_at_cap_kept(config):
    assert len(build(sf=[sf_row(emp=2000)], config=config).accounts) == 1


def test_size_below_floor_dropped(config):
    assert build(sf=[sf_row(emp=19)], config=config).dropped[0].reason == "under_min_employees"


def test_size_at_floor_kept(config):
    assert len(build(sf=[sf_row(emp=20)], config=config).accounts) == 1


def test_size_none_kept_with_note(config):
    res = build(sf=[sf_row(emp=None)], config=config)
    a = res.accounts[0]
    assert a.size_unknown and "size_unknown" in a.notes and not res.dropped


def test_size_filled_from_nooks_removes_size_unknown(config):
    res = build(sf=[sf_row("S1", "Acme Pty Ltd", emp=None)],
                nk=[{"id": "N1", "name": "Acme", "numEmployees": 55, "_sf_account_id": "S1"}], config=config)
    a = res.accounts[0]
    assert a.employees == 55 and "size_unknown" not in a.notes


def test_salesforce_size_wins_over_nooks(config):
    res = build(sf=[sf_row("S1", "Acme Pty Ltd", emp=100)],
                nk=[{"id": "N1", "name": "Acme", "numEmployees": 999, "_sf_account_id": "S1"}], config=config)
    assert res.accounts[0].employees == 100


# ------------------------------------------------------------ flagged group
def test_flagged_group_retained_with_flags(config):
    res = build(sf=[BOOK[7]], config=config)
    a = res.accounts[0]
    assert a.non_anz_billing_anz_hq and a.country == "Australia"
    assert "flagged_non_anz_billing_anz_hq" in a.flags
    assert any(n.startswith("flagged_non_anz_billing_anz_hq") for n in a.notes)
    assert confidence(a) == "low" and a.confidence == "low"


def test_list_flagged_returns_only_group(config):
    res = build(sf=BOOK, config=config)
    assert [a.sf_account_id for a in list_flagged(res.accounts)] == ["FAKE-001-008"]


def test_list_flagged_needs_no_config():
    a, b = Account("1", "A", non_anz_billing_anz_hq=True), Account("2", "B")
    assert list_flagged([a, b]) == [a]


def test_normal_account_confidence_normal(config):
    assert confidence(build(sf=[sf_row()], config=config).accounts[0]) == "normal"


# ------------------------------------------------------------ dedupe
def test_dedupe_by_sf_account_id_nooks_hint(config):
    res = build(sf=[sf_row("S1", "Acme Pty Ltd")], nk=[{"id": "N1", "name": "Completely Different", "_sf_account_id": "S1"}],
                config=config)
    a = res.accounts[0]
    assert len(res.accounts) == 1 and a.source_salesforce and a.source_nooks and a.nooks_account_id == "N1"


def test_dedupe_by_zoominfo_id(config):
    res = build(sf=[sf_row("S1", "Acme Pty Ltd", **{"DOZISF__ZoomInfo_Id__c": "Z1"})],
                zi=[{"id": "Z1", "name": "Unrelated Spelling"}], config=config)
    a = res.accounts[0]
    assert len(res.accounts) == 1 and a.source_zoominfo and "zoominfo_id" in a.notes[-1]


def test_same_nooks_id_twice_kept_and_flagged(config):
    nk = {"id": "N1", "name": "Acme", "numEmployees": 50}
    res = build(nk=[nk, dict(nk, name="Acme (dup)")], config=config)
    assert len(res.accounts) == 2
    assert all("possible_duplicate" in x.flags for x in res.accounts)


def test_same_sf_id_twice_kept_and_flagged(config):
    res = build(sf=[sf_row("S1", "Acme Pty Ltd"), sf_row("S1", "Acme Pty Ltd")], config=config)
    assert len(res.accounts) == 2
    assert all("possible_duplicate" in x.flags for x in res.accounts)


@pytest.mark.parametrize("variant", ["EXAMPLE WIDGETS LIMITED", "Example Widgets Ltd.", "the example widgets pty. ltd.",
                                     "Example  Widgets", "Example Widgets Pty Limited"])
def test_dedupe_by_name_suffix_variants(config, variant):
    res = build(sf=[sf_row("S1", "Example Widgets Pty Ltd")], zi=[{"id": "Z9", "name": variant}], config=config)
    assert len(res.accounts) == 1
    assert "normalised name" in res.accounts[0].notes[-1]
    assert res.accounts[0].zoominfo_id == "Z9"


def test_same_name_different_sf_ids_not_merged(config):
    res = build(sf=[sf_row("S1", "Acme Pty Ltd"), sf_row("S2", "Acme Pty Ltd")], config=config)
    assert len(res.accounts) == 2


def test_name_match_blocked_by_conflicting_zoominfo_id(config):
    res = build(sf=[sf_row("S1", "Acme Pty Ltd", **{"DOZISF__ZoomInfo_Id__c": "Z1"})],
                zi=[{"id": "Z2", "name": "Acme"}], config=config)
    assert len(res.accounts) == 2


def test_normalise_name_forms():
    assert normalise_name("The Example Widgets Pty. Ltd.") == "example widgets"
    assert normalise_name("Smith & Sons Inc") == normalise_name("Smith and Sons")
    assert normalise_name(None) == ""


def test_merge_keeps_highest_priority_fields_and_flags(config):
    res = build(sf=[sf_row("S1", "Alpha Pty Ltd", **{"DOZISF__ZoomInfo_Id__c": "Z1"})],
                nk=[{"id": "N1", "name": "Alpha", "domain": "alpha.test", "_sf_account_id": "S1"}],
                zi=[{"id": "Z1", "name": "Alpha Pty Ltd"}], config=config)
    a = res.accounts[0]
    assert (a.source_salesforce, a.source_nooks, a.source_zoominfo) == (True, True, True)
    assert a.name == "Alpha Pty Ltd" and a.website == "alpha.test" and a.country == "Australia"
    assert "zoominfo_only" not in " ".join(a.notes)


def test_cases_fixture_merge_outcomes(config):
    res = build(sf=CASES["sf_rows"], nk=CASES["nooks_rows"], zi=CASES["zoominfo_rows"], config=config)
    names = {a.name: a for a in res.accounts}
    alpha = names["Alpha Analytics Pty Ltd"]
    assert alpha.source_salesforce and alpha.source_nooks and alpha.source_zoominfo
    assert alpha.nooks_account_id == "FAKE-NK-A01"
    assert names["Kilo Kiwi Ltd"].source_nooks            # nooks merged by name
    assert names["Lima Logistics Pty Ltd"].source_zoominfo
    mike = names["Mike Media Pty Ltd"]
    assert mike.source_nooks and mike.source_zoominfo and not mike.source_salesforce


# ------------------------------------------------------------ zoominfo / nooks only
def test_zoominfo_only_kept_and_flagged(config):
    res = build(zi=[{"id": "Z1", "name": "Oscar Optics Pty Ltd"}], config=config)
    a = res.accounts[0]
    assert "zoominfo_only" in a.flags and confidence(a) == "low"
    assert a.size_unknown and a.country is None


def test_nooks_only_kept_and_flagged(config):
    res = build(nk=[{"id": "N1", "name": "Nov Networks", "numEmployees": 60}], config=config)
    a = res.accounts[0]
    assert "nooks_only" in a.flags and confidence(a) == "low"


def test_zoominfo_only_not_flagged_when_in_salesforce(config):
    res = build(sf=[sf_row("S1", "Acme Pty Ltd", **{"DOZISF__ZoomInfo_Id__c": "Z1"})],
                zi=[{"id": "Z1", "name": "Acme Pty Ltd"}], config=config)
    assert "zoominfo_only" not in res.accounts[0].flags


def test_malformed_rows_dropped_with_reason(config):
    res = build(sf=[{"Name": "No Id Pty Ltd"}], zi=[{"name": "No id either"}], config=config)
    assert res.stats == {"malformed_row": 2} and res.accounts == []


# ------------------------------------------------------------ touched
def test_touched_within_window_dropped(config):
    res = build(sf=[sf_row("S1")], touched={"S1": TODAY - timedelta(days=1)}, config=config)
    assert res.dropped[0].reason == "recently_touched"


def test_touched_today_dropped(config):
    assert build(sf=[sf_row("S1")], touched={"S1": TODAY}, config=config).dropped[0].reason == "recently_touched"


def test_touched_day_before_boundary_dropped(config):
    days = config.limits.cooldown_days_no_answer - 1
    res = build(sf=[sf_row("S1")], touched={"S1": TODAY - timedelta(days=days)}, config=config)
    assert res.stats == {"recently_touched": 1}


def test_touched_on_boundary_day_eligible(config):
    days = config.limits.cooldown_days_no_answer
    res = build(sf=[sf_row("S1")], touched={"S1": TODAY - timedelta(days=days)}, config=config)
    assert len(res.accounts) == 1


def test_touched_outside_window_kept(config):
    res = build(sf=[sf_row("S1")], touched={"S1": TODAY - timedelta(days=60)}, config=config)
    assert len(res.accounts) == 1


def test_touched_other_account_does_not_affect(config):
    assert len(build(sf=[sf_row("S1")], touched={"S2": TODAY}, config=config).accounts) == 1


def test_touched_custom_cooldown(config):
    res = build(sf=[sf_row("S1")], touched={"S1": TODAY - timedelta(days=10)}, config=config, cooldown_days=14)
    assert res.dropped[0].reason == "recently_touched"


def test_touched_via_zoominfo_id_for_non_sf_account(config):
    res = build(zi=[{"id": "Z1", "name": "Oscar Optics Pty Ltd"}], touched={"Z1": TODAY}, config=config)
    assert res.stats == {"recently_touched": 1}


# ------------------------------------------------------------ blocked
def test_blocked_sf_id_dropped(config):
    res = build(sf=[sf_row("S1")], blocked={"S1"}, config=config)
    assert res.dropped[0].reason == "blocked"


def test_blocked_nooks_id_dropped_after_merge(config):
    res = build(sf=[sf_row("S1", "Acme Pty Ltd")], nk=[{"id": "N1", "name": "Acme", "_sf_account_id": "S1"}],
                blocked={"N1"}, config=config)
    assert res.stats == {"blocked": 1}


def test_blocked_set_not_mutated(config):
    blocked = {"S1"}
    build(sf=[sf_row("S1")], blocked=blocked, config=config)
    assert blocked == {"S1"}


# ------------------------------------------------------------ ordering, purity, summary
def test_ordering_by_source_then_name(config):
    res = build(sf=[sf_row("S2", "Zulu Pty Ltd"), sf_row("S1", "Alpha Pty Ltd")],
                nk=[{"id": "N1", "name": "Beta Nooks", "numEmployees": 50}],
                zi=[{"id": "Z1", "name": "Aardvark Zoom Pty Ltd"}], config=config)
    assert [a.name for a in res.accounts] == ["Alpha Pty Ltd", "Zulu Pty Ltd", "Beta Nooks", "Aardvark Zoom Pty Ltd"]


def test_output_independent_of_input_order(config):
    args = (CASES["sf_rows"], CASES["nooks_rows"], CASES["zoominfo_rows"] + CASES["extra_noise_zoominfo"])
    a = build(*args, config=config)
    b = build(*(list(reversed(x)) for x in args), config=config)
    assert [x.name for x in a.accounts] == [x.name for x in b.accounts]
    assert a.dropped == b.dropped and a.stats == b.stats


def test_repeat_runs_identical(config):
    args = (CASES["sf_rows"], CASES["nooks_rows"], CASES["zoominfo_rows"])
    assert build(*args, config=config) == build(*args, config=config)


def test_input_rows_not_mutated(config):
    sf, nk, zi = (copy.deepcopy(CASES[k]) for k in ("sf_rows", "nooks_rows", "zoominfo_rows"))
    before = copy.deepcopy((sf, nk, zi))
    touched, blocked = {"FAKE-ACC-001": TODAY}, {"x"}
    build_account_list(sf, nk, zi, config, touched, TODAY, blocked)
    assert (sf, nk, zi) == before and touched == {"FAKE-ACC-001": TODAY} and blocked == {"x"}


def test_cases_fixture_every_drop_has_reason(config):
    res = build(sf=CASES["sf_rows"], nk=CASES["nooks_rows"], zi=CASES["zoominfo_rows"], config=config)
    got = {d.name: d.reason for d in res.dropped}
    assert got["Charlie Care Ltd"] == "over_max_employees"
    assert got["Delta Dental Pty Ltd"] == "under_min_employees"
    assert got["India Imports Inc"] == "not_anz"
    assert got["Juliet Jewellers Pty Ltd"] == "customer"
    assert all(d.reason and d.source for d in res.dropped)
    assert "Echo Energy Pty Ltd" in {a.name for a in res.accounts}      # size None kept
    assert "Hotel Holdings Pty Ltd" in {a.name for a in res.accounts}   # flagged group kept


def test_result_types(config):
    res = build(sf=[sf_row()], config=config)
    assert isinstance(res, AccountListResult)
    assert isinstance(Dropped("n", "i", "r", "s"), Dropped)


def test_dropped_source_is_highest_priority(config):
    res = build(sf=[sf_row("S1", "Acme Pty Ltd", emp=5000)], zi=[{"id": "Z", "name": "Acme"}], config=config)
    assert res.dropped[0].source == "salesforce" and len(res.dropped) == 1


def test_summary_counts_and_samples(config):
    res = build(sf=[sf_row(f"S{i}", f"Big Co {i} Pty Ltd", emp=5000) for i in range(7)] + [sf_row("OK")],
                config=config)
    text = summarise(res)
    assert "accounts kept: 1" in text and "accounts dropped: 7" in text
    assert "over_max_employees: 7" in text
    sample = [l for l in text.splitlines() if "e.g." in l][0]
    assert sample.count("Big Co") == 5 and sample.endswith("...")


def test_summary_lists_each_reason(config):
    res = build(sf=[sf_row("S1", Current_Customer__c=True)], zi=ALL_NOISE, config=config)
    text = summarise(res)
    for reason in ("customer", "noise_location", "noise_government", "not_anz", "noise_vague_name"):
        assert f"  {reason}:" in text


def test_summary_empty_result():
    text = summarise(AccountListResult())
    assert "accounts kept: 0" in text and "accounts dropped: 0" in text


# ------------------------------------------------------------ review fixes (H1)
# (name, SF/Nooks flag expected or None, dropped when ZoomInfo-only)
REVIEW_NAMES = [
    ("Fake Government Services Pty Ltd", None, False),
    ("Fake Department of Health Pty Ltd", None, False),
    ("Fake City Council Software Pty Ltd", "noise_government", True),
    ("Fake Office of the Chief Digital Officer Pty Ltd", None, False),
    ("Fake Public Service Credit Union", None, False),
    ("Fake Ministry of Sound Media Ltd", None, False),
    ("Fake Parliament Hill Software", None, False),
    ("Sydney CBD Software Pty Ltd", "noise_location", True),
    ("Perth CBD Wealth Pty Ltd", "noise_location", True),
    ("Fake Unknown Holdings", "noise_vague_name", True),
    ("TBD Pty Ltd", "noise_vague_name", True),
    ("Brisbane City Council", "noise_government", True),
    ("Department of Fake Transport Digital Services", "noise_government", True),
]


@pytest.mark.parametrize("name,flag,_zi", REVIEW_NAMES, ids=[n[0] for n in REVIEW_NAMES])
def test_review_names_kept_from_salesforce(config, name, flag, _zi):
    res = build(sf=[sf_row("S1", name, emp=150)], config=config)
    assert [a.name for a in res.accounts] == [name] and not res.dropped
    if flag:
        a = res.accounts[0]
        assert flag in a.flags and a.confidence == "low"
        assert any(n.startswith(flag) for n in a.notes)


@pytest.mark.parametrize("name,flag,_zi", REVIEW_NAMES, ids=[n[0] for n in REVIEW_NAMES])
def test_review_names_kept_from_nooks(config, name, flag, _zi):
    res = build(nk=[{"id": "N1", "name": name, "numEmployees": 150}], config=config)
    assert len(res.accounts) == 1 and not res.dropped
    if flag:
        assert flag in res.accounts[0].flags


@pytest.mark.parametrize("name,flag,zi_drop", REVIEW_NAMES, ids=[n[0] for n in REVIEW_NAMES])
def test_review_names_zoominfo_only(config, name, flag, zi_drop):
    res = build(zi=[{"id": "Z1", "name": name}], config=config)
    assert bool(res.dropped) == zi_drop
    if zi_drop:
        assert res.dropped[0].reason == flag


def test_noise_flags_not_added_to_clean_names(config):
    res = build(sf=[sf_row("S1", "Example Widgets Pty Ltd")], config=config)
    assert res.accounts[0].flags == [] and res.accounts[0].confidence == "normal"


def test_noise_still_drops_for_zoominfo_only_even_when_big_flag_absent(config):
    assert build(zi=[{"id": "Z1", "name": "Government of Exampleland"}], config=config).stats == {"noise_government": 1}


def test_real_drops_still_apply_to_noisy_sf_names(config):
    res = build(sf=[sf_row("S1", "Brisbane City Council", emp=5000)], config=config)
    assert res.stats == {"over_max_employees": 1}
    res = build(sf=[sf_row("S2", "Brisbane City Council")], touched={"S2": TODAY}, config=config)
    assert res.stats == {"recently_touched": 1}
    res = build(sf=[sf_row("S3", "TBD Pty Ltd", Current_Customer__c=True)], config=config)
    assert res.stats == {"customer": 1}


# ------------------------------------------------------------ H2: same-source duplicates
def shared_zi_rows():
    z = {"DOZISF__ZoomInfo_Id__c": "Z9"}
    return [sf_row("A1", "Fake One Pty Ltd", **z), sf_row("A2", "Fake Two Pty Ltd", **z)]


def test_shared_zoominfo_id_keeps_both_sf_accounts(config):
    res = build(sf=shared_zi_rows(), config=config)
    assert [a.sf_account_id for a in res.accounts] == ["A1", "A2"]
    assert all("possible_duplicate" in a.flags for a in res.accounts)
    assert "A2" in " ".join(res.accounts[0].notes) and "A1" in " ".join(res.accounts[1].notes)


def test_shared_zoominfo_id_touched_applies_to_second_account(config):
    res = build(sf=shared_zi_rows(), touched={"A2": TODAY}, config=config)
    assert [a.sf_account_id for a in res.accounts] == ["A1"]
    assert res.dropped[0].id == "A2" and res.dropped[0].reason == "recently_touched"


def test_shared_zoominfo_id_blocked_applies_to_second_account(config):
    res = build(sf=shared_zi_rows(), blocked={"A2"}, config=config)
    assert [a.sf_account_id for a in res.accounts] == ["A1"] and res.stats == {"blocked": 1}


def test_shared_zoominfo_id_customer_flag_on_second_account_is_not_lost(config):
    rows = shared_zi_rows()
    rows[1]["Current_Customer__c"] = True
    res = build(sf=rows, config=config)
    assert [a.sf_account_id for a in res.accounts] == ["A1"] and res.stats == {"customer": 1}


def test_zoominfo_row_with_shared_id_merges_once_and_records_ids(config):
    res = build(sf=shared_zi_rows(), zi=[{"id": "Z9", "name": "Shared Zoom"}], config=config)
    assert len(res.accounts) == 2
    assert "Z9" in " ".join(res.accounts[0].notes) and res.accounts[0].source_zoominfo


def test_cross_source_merge_notes_record_all_ids(config):
    res = build(sf=[sf_row("S1", "Acme Pty Ltd", **{"DOZISF__ZoomInfo_Id__c": "Z1"})],
                nk=[{"id": "N7", "name": "Acme", "_sf_account_id": "S1"}],
                zi=[{"id": "Z1", "name": "Acme"}], config=config)
    text = " ".join(res.accounts[0].notes)
    assert "N7" in text and "Z1" in text


# ------------------------------------------------------------ M1: ambiguous name merge
def test_ambiguous_name_match_not_merged(config):
    sf = [sf_row("S1", "Acme Pty Ltd"), sf_row("S2", "ACME Limited")]
    res = build(sf=sf, zi=[{"id": "Z1", "name": "Acme"}], config=config)
    assert len(res.accounts) == 3
    assert res.stats["name_merge_ambiguous"] == 1 and "name_merged" not in res.stats
    assert all("possible_duplicate" in a.flags for a in res.accounts)
    assert not any(a.source_zoominfo and a.source_salesforce for a in res.accounts)


def test_name_merge_counted(config):
    res = build(sf=[sf_row("S1", "Acme Pty Ltd")], zi=[{"id": "Z1", "name": "Acme"}], config=config)
    assert res.stats == {"name_merged": 1}


def test_id_merge_is_not_counted_as_name_merge(config):
    res = build(sf=[sf_row("S1", "Acme Pty Ltd", **{"DOZISF__ZoomInfo_Id__c": "Z1"})],
                zi=[{"id": "Z1", "name": "Acme"}], config=config)
    assert "name_merged" not in res.stats


def test_summary_shows_merge_counters(config):
    res = build(sf=[sf_row("S1", "Acme Pty Ltd"), sf_row("S2", "Acme Ltd")], zi=[{"id": "Z1", "name": "Acme"}],
                nk=[{"id": "N1", "name": "Beta Co", "numEmployees": 50}], config=config)
    text = summarise(res)
    assert "name_merge_ambiguous (not merged): 1" in text and "merged by name: 0" in text


# ------------------------------------------------------------ M3: unknown country
@pytest.mark.parametrize("billing,hq", [(None, None), ("", None), ("n/a", "Other"), ("Oz", None),
                                         ("Australia (Territory)", None), ("Aotearoa", "unknown")])
def test_unknown_country_kept_and_flagged(config, billing, hq):
    res = build(sf=[sf_row(BillingCountry=billing, HQ_Country__c=hq)], config=config)
    a = res.accounts[0]
    assert "country_unknown" in a.flags and a.confidence == "low" and not res.dropped
    assert a.timezone is None


def test_real_non_anz_country_still_dropped(config):
    res = build(sf=[sf_row(BillingCountry="Singapore", HQ_Country__c=None)], config=config)
    assert res.dropped[0].reason == "not_anz"


def test_non_anz_billing_junk_hq_dropped(config):
    res = build(sf=[sf_row(BillingCountry="United States", HQ_Country__c="n/a")], config=config)
    assert res.dropped[0].reason == "not_anz"


def test_null_billing_anz_hq_is_anz(config):
    res = build(sf=[sf_row(BillingCountry=None, HQ_Country__c="AU")], config=config)
    assert res.accounts[0].country == "Australia" and "country_unknown" not in res.accounts[0].flags


# ------------------------------------------------------------ M4: (not ANZ) marker
def test_not_anz_name_marker_ignored_for_salesforce(config):
    res = build(sf=[sf_row("S1", "Acme (not ANZ) Pty Ltd")], config=config)
    assert len(res.accounts) == 1


def test_not_anz_name_marker_applies_to_zoominfo_only(config):
    assert build(zi=[{"id": "Z1", "name": "Acme (not ANZ)"}], config=config).stats == {"not_anz": 1}


# ------------------------------------------------------------ flags in summary, ISO touched
def test_summary_lists_flagged_counts_and_samples(config):
    sf = [sf_row(f"S{i}", f"Perth CBD {chr(65 + i)}", emp=100) for i in range(7)]
    res = build(sf=sf + [sf_row("OK", "Real Software Pty Ltd")], config=config)
    text = summarise(res)
    assert "kept but flagged for review:" in text and "  noise_location: 7" in text
    block = text.split("  noise_location: 7")[1].splitlines()[1]
    assert block.count("Perth CBD") == 5 and block.endswith("...")
    assert "accounts dropped: 0" in text


def test_summary_without_flags_says_none(config):
    assert "kept but flagged for review: none" in summarise(build(sf=[sf_row()], config=config))


def test_touched_iso_string_accepted(config):
    iso = (TODAY - timedelta(days=1)).isoformat()
    assert build(sf=[sf_row("S1")], touched={"S1": iso}, config=config).stats == {"recently_touched": 1}


def test_drop_detail_mentions_merge(config):
    res = build(sf=[sf_row("S1", "Acme Pty Ltd")], nk=[{"id": "N1", "name": "Acme", "_sf_account_id": "S1"}],
                blocked=set(), config=config)
    assert not res.dropped
    res = build(sf=[sf_row("S1", "Acme Pty Ltd", Current_Customer__c=True)],
                nk=[{"id": "N1", "name": "Acme", "_sf_account_id": "S1"}], config=config)
    assert "after merging" in res.dropped[0].detail


# ------------------------------------------------------------ R1: loose government rule (ZoomInfo-only)
GOV_ZI_DROPPED = ["Victorian Department of Health", "Western Australian Department of Education",
                  "Queensland Health Department", "NSW Department of Planning", "Tasmanian Government",
                  "Office of the Victorian Information Commissioner"]
PRIVATE_NAMES = ["Fake Ministry of Sound Media Ltd", "Fake Government Services Pty Ltd"]


@pytest.mark.parametrize("name", GOV_ZI_DROPPED)
def test_loose_gov_dropped_for_zoominfo_only(config, name):
    res = build(zi=[{"id": "Z1", "name": name}], config=config)
    assert res.dropped and res.dropped[0].reason == "noise_government"


@pytest.mark.parametrize("name", GOV_ZI_DROPPED[:5])
def test_loose_gov_not_applied_to_salesforce_or_nooks(config, name):
    assert [a.name for a in build(sf=[sf_row("S1", name)], config=config).accounts] == [name]
    assert len(build(nk=[{"id": "N1", "name": name, "numEmployees": 100}], config=config).accounts) == 1


@pytest.mark.parametrize("name", PRIVATE_NAMES)
@pytest.mark.parametrize("source", ["sf", "nooks", "zi"])
def test_private_names_never_dropped(config, name, source):
    kw = {"sf": dict(sf=[sf_row("S1", name)]), "nooks": dict(nk=[{"id": "N1", "name": name, "numEmployees": 90}]),
          "zi": dict(zi=[{"id": "Z1", "name": name}])}[source]
    res = build(config=config, **kw)
    assert len(res.accounts) == 1 and not res.dropped
    assert "noise_government" not in res.accounts[0].flags


# ------------------------------------------------------------ L3: 15 vs 18 char Salesforce ids
SF18 = "0015g00000ABCDEFGH"      # 18 chars, synthetic
SF15 = SF18[:15]


def test_18char_row_matches_15char_touched(config):
    res = build(sf=[sf_row(SF18)], touched={SF15: TODAY}, config=config)
    assert res.stats == {"recently_touched": 1}


def test_15char_row_matches_18char_touched(config):
    assert build(sf=[sf_row(SF15)], touched={SF18: TODAY}, config=config).stats == {"recently_touched": 1}


def test_18char_row_matches_15char_blocked(config):
    assert build(sf=[sf_row(SF18)], blocked={SF15}, config=config).stats == {"blocked": 1}


def test_15char_row_matches_18char_blocked(config):
    assert build(sf=[sf_row(SF15)], blocked={SF18}, config=config).stats == {"blocked": 1}


def test_nooks_merge_key_18_vs_15(config):
    res = build(sf=[sf_row(SF18, "Acme Pty Ltd")],
                nk=[{"id": "N1", "name": "Other Name", "_sf_account_id": SF15}], config=config)
    assert len(res.accounts) == 1 and res.accounts[0].source_nooks


def test_blocked_input_set_unchanged_by_normalisation(config):
    blocked = {SF18}
    build(sf=[sf_row(SF15)], blocked=blocked, config=config)
    assert blocked == {SF18}


# ------------------------------------------------------------ L4: cooldown_until
def test_cooldown_until_in_future_drops(config):
    res = build(sf=[sf_row("S1")], cooldown_until={"S1": TODAY + timedelta(days=1)}, config=config)
    assert res.stats == {"recently_touched": 1}


def test_cooldown_until_today_is_eligible(config):
    assert len(build(sf=[sf_row("S1")], cooldown_until={"S1": TODAY}, config=config).accounts) == 1


def test_cooldown_until_overrides_touched(config):
    res = build(sf=[sf_row("S1")], touched={"S1": TODAY}, cooldown_until={"S1": TODAY - timedelta(days=1)},
                config=config)
    assert len(res.accounts) == 1


def test_touched_ignored_when_cooldown_until_passed(config):
    """M1: with cooldown_until given, touched (e.g. an email touch) never drops an account."""
    res = build(sf=[sf_row("S1"), sf_row("S2")], touched={"S2": TODAY},
                cooldown_until={"S1": TODAY - timedelta(days=5)}, config=config)
    assert [a.sf_account_id for a in res.accounts] == ["S1", "S2"] and not res.dropped


def test_email_only_touch_never_drops_when_cooldown_until_passed(config):
    # S1 emailed today (touched), no call cooldown: eligible. S2 has a call cooldown: dropped.
    res = build(sf=[sf_row("S1"), sf_row("S2")], touched={"S1": TODAY, "S2": TODAY},
                cooldown_until={"S2": TODAY + timedelta(days=2)}, config=config)
    assert [a.sf_account_id for a in res.accounts] == ["S1"] and res.dropped[0].id == "S2"


def test_empty_cooldown_until_still_ignores_touched(config):
    assert len(build(sf=[sf_row("S1")], touched={"S1": TODAY}, cooldown_until={}, config=config).accounts) == 1


def test_touched_legacy_path_without_cooldown_until(config):
    assert build(sf=[sf_row("S1")], touched={"S1": TODAY}, config=config).stats == {"recently_touched": 1}


# ------------------------------------------------------------ M2: call_date
def test_night_run_cooldown_ending_on_call_day_is_kept(config):
    wed, thu = date(2026, 11, 25), date(2026, 11, 26)
    kw = dict(sf=[sf_row("S1")], cooldown_until={"S1": thu}, config=config)
    assert build(**kw).stats == {"recently_touched": 1}                    # today=Wed only: dropped
    res = build_account_list(list(kw["sf"]), [], [], config, {}, wed, frozenset(), cooldown_until={"S1": thu},
                             call_date=thu)
    assert len(res.accounts) == 1 and not res.dropped                      # first call day Thu: eligible


def test_call_date_before_cooldown_end_still_drops(config):
    wed, thu = date(2026, 11, 25), date(2026, 11, 26)
    res = build_account_list([sf_row("S1")], [], [], config, {}, wed, frozenset(),
                             cooldown_until={"S1": thu + timedelta(days=1)}, call_date=thu)
    assert res.stats == {"recently_touched": 1}


def test_call_date_applies_to_legacy_touched_path(config):
    touched_day = TODAY - timedelta(days=config.limits.cooldown_days_no_answer - 1)   # still cooling today
    assert build(sf=[sf_row("S1")], touched={"S1": touched_day}, config=config).stats == {"recently_touched": 1}
    res = build_account_list([sf_row("S1")], [], [], config, {"S1": touched_day}, TODAY, frozenset(),
                             call_date=TODAY + timedelta(days=1))
    assert len(res.accounts) == 1


# ------------------------------------------------------------ L2: group_by_zoominfo_id
def test_group_by_zoominfo_id_keeps_all_accounts(config):
    res = build(sf=shared_zi_rows() + [sf_row("A3", "Fake Three Pty Ltd", **{"DOZISF__ZoomInfo_Id__c": "Z3"}),
                                       sf_row("A4", "Fake Four Pty Ltd")], config=config)
    groups = group_by_zoominfo_id(res.accounts)
    assert [a.sf_account_id for a in groups["Z9"]] == ["A1", "A2"]
    assert all("possible_duplicate" in a.flags for a in groups["Z9"])
    assert [a.sf_account_id for a in groups["Z3"]] == ["A3"]
    assert set(groups) == {"Z9", "Z3"}                                      # A4 has no id: left out


def test_group_by_zoominfo_id_empty():
    assert group_by_zoominfo_id([]) == {}


def test_docstring_says_ids_are_normalised():
    doc = build_account_list.__doc__
    assert "NOT normalised" not in doc and "normalise_sf_id" in doc


def test_cooldown_until_accepts_18char_key_and_iso(config):
    res = build(sf=[sf_row(SF15)], cooldown_until={SF18: (TODAY + timedelta(days=2)).isoformat()}, config=config)
    assert res.stats == {"recently_touched": 1}


# ------------------------------------------------------------ N3: touched is optional
def test_touched_is_optional_with_cooldown_until(config):
    thu = date(2026, 11, 26)
    res = build_account_list([sf_row("S1")], [], [], config, today=TODAY, cooldown_until={"S1": thu})
    assert res.stats == {"recently_touched": 1} and not res.accounts


def test_touched_omitted_means_no_touches(config):
    res = build_account_list([sf_row("S1")], [], [], config, today=TODAY)
    assert len(res.accounts) == 1 and not res.dropped


def test_touched_none_equals_empty_dict(config):
    a = build_account_list([sf_row("S1")], [], [], config, None, TODAY)
    b = build_account_list([sf_row("S1")], [], [], config, {}, TODAY)
    assert [x.sf_account_id for x in a.accounts] == [x.sf_account_id for x in b.accounts] and a.stats == b.stats


def test_touched_still_works_positionally_and_by_keyword(config):
    touched_day = TODAY - timedelta(days=config.limits.cooldown_days_no_answer - 1)
    pos = build_account_list([sf_row("S1")], [], [], config, {"S1": touched_day}, TODAY)
    kw = build_account_list([sf_row("S1")], [], [], config, touched={"S1": touched_day}, today=TODAY)
    assert pos.stats == kw.stats == {"recently_touched": 1}


def test_call_date_alone_is_enough_as_reference(config):
    thu = date(2026, 11, 26)
    res = build_account_list([sf_row("S1")], [], [], config, cooldown_until={"S1": thu}, call_date=thu)
    assert len(res.accounts) == 1


def test_today_and_call_date_both_missing_is_a_clear_error(config):
    with pytest.raises(ValueError, match="today or call_date"):
        build_account_list([sf_row("S1")], [], [], config)


# ------------------------------------------------------------ probable duplicates (evidence required)
def dup_names(res):
    return {a.name for a in res.accounts if "possible_duplicate" in a.flags}


def names(prefix, n):
    return [f"{prefix} person{i}" for i in range(n)]


def two(config, n1, n2, ea=100, eb=200, ca=None, cb=None, wa=None, wb=None, **kw):
    """Two SF accounts S1/S2 with different headcounts unless told otherwise."""
    contacts = {}
    if ca is not None:
        contacts["S1"] = ca
    if cb is not None:
        contacts["S2"] = cb
    res = build_account_list([sf_row("S1", n1, ea, Website=wa), sf_row("S2", n2, eb, Website=wb)], [], [], config,
                             {}, TODAY, frozenset(), contact_names_by_account=contacts or None)
    return res


# --- the four real-shaped true pairs
def test_catapult_like_different_domains_but_13_shared_contact_names(config):
    shared = names("x", 13)
    res = two(config, "Catapult Communications Pty Ltd", "Catapult", ea=400, eb=671, wa="catapult.com",
              wb="catapultsports.com", ca=shared, cb=shared + ["extra one"])
    assert len(res.accounts) == 2 and not res.dropped                # flagged, never merged or dropped
    assert dup_names(res) == {"Catapult Communications Pty Ltd", "Catapult"}
    note = " ".join(" ".join(a.notes) for a in res.accounts)
    assert "possible_duplicate group dup-S1" in note and "name+shared contacts" in note
    assert "person" not in note                                      # contact names are never noted


def test_carsales_like_wrong_website_same_headcount(config):
    res = two(config, "Carsales", "carsales.com", ea=900, eb=900, wa="carsales.com.au", wb="example-other.com")
    assert len(dup_names(res)) == 2 and "name+same headcount" in " ".join(res.accounts[0].notes)


def test_finder_like_four_shared_contact_names(config):
    shared = names("f", 4)
    res = two(config, "Finder", "Finder.com", ea=300, eb=250, ca=shared, cb=shared)
    assert len(dup_names(res)) == 2


def test_freelancer_like_same_headcount(config):
    res = two(config, "Freelancer", "Freelancer Technology", ea=318, eb=318)
    assert len(dup_names(res)) == 2


# --- name containment alone never flags (8+ fake false-positive pairs)
@pytest.mark.parametrize("n1,n2", [
    ("Bank Australia", "Arab Bank"), ("Bank Australia", "Fake Bank Australia"),
    ("Meridian", "Meridian Informatics"), ("Perpetual", "Perpetual Guardian"),
    ("Acquire", "Acquire Online"), ("Retail", "Retail Employees Super Fund"),
    ("Australia Post", "Post Office"), ("Alpha Digital", "Beta Digital"),
    ("Commonwealth Bank", "Bank of Melbourne"), ("Health Services Pty Ltd", "Health Partners Pty Ltd"),
    ("Cat", "Catapult Communications"), ("Catapult", "Catapult BI")])
def test_name_containment_alone_does_not_flag(config, n1, n2):
    res = two(config, n1, n2)
    assert dup_names(res) == set() and duplicate_groups(res.accounts) == []


def test_two_shared_contact_names_is_not_enough(config):
    shared = names("m", 2)
    assert dup_names(two(config, "Meridian", "Meridian Informatics", ca=shared, cb=shared)) == set()


def test_three_shared_contact_names_is_enough(config):
    shared = names("m", 3)
    assert len(dup_names(two(config, "Meridian", "Meridian Informatics", ca=shared, cb=shared))) == 2


def test_shared_contact_names_without_name_containment_do_not_flag(config):
    shared = names("m", 10)
    assert dup_names(two(config, "Alpha Widgets", "Zeta Logistics", ca=shared, cb=shared)) == set()


def test_same_headcount_needs_shared_first_token(config):
    assert dup_names(two(config, "Meridian", "Informatics Meridian", ea=50, eb=50)) == set()


def test_same_headcount_none_does_not_count(config):
    res = build_account_list([sf_row("S1", "Meridian", None), sf_row("S2", "Meridian Informatics", None)], [], [],
                             config, {}, TODAY)
    assert dup_names(res) == set()


def test_short_distinctive_token_is_not_enough(config):
    assert dup_names(two(config, "Zeta", "Zeta Labs", ea=50, eb=50)) == set()      # 'zeta' is 4 chars


def test_contact_names_are_case_and_space_normalised(config):
    ca = ["Ann  Lee", "BOB ROY", "Cy Dee"]
    cb = ["ann lee", "bob roy", "cy dee"]
    assert len(dup_names(two(config, "Meridian", "Meridian Informatics", ca=ca, cb=cb))) == 2


def test_contact_ids_keyed_with_18_char_sf_id(config):
    shared = names("m", 3)
    res = build_account_list([sf_row(SF18, "Meridian", 50), sf_row("S2", "Meridian Informatics", 60)], [], [],
                             config, {}, TODAY, contact_names_by_account={SF15: shared, "S2": shared})
    assert len(dup_names(res)) == 2


# --- domain / ids alone flag
def test_same_domain_different_name_flags_alone(config):
    res = two(config, "Alpha Pty Ltd", "Zeta Trading", wa="www.shared-co.com.au", wb="https://SHARED-CO.com.au/about?x=1")
    assert len(dup_names(res)) == 2 and "same website domain" in " ".join(res.accounts[0].notes)


def test_different_domains_not_flagged(config):
    assert dup_names(two(config, "Alpha Pty Ltd", "Zeta Trading", wa="alpha.com.au", wb="zeta.com.au")) == set()


@pytest.mark.parametrize("site", ["https://www.linkedin.com/company/x", "foo.wixsite.com/bar", "facebook.com/x",
                                  "sites.google.com/view/x", "twitter.com/x"])
def test_shared_hosting_domains_ignored(config, site):
    assert dup_names(two(config, "Alpha Pty Ltd", "Zeta Trading", wa=site, wb=site)) == set()


@pytest.mark.parametrize("raw,want", [("www.Example.com", "example.com"), ("https://www.example.com.au/path", "example.com.au"),
                                      ("http://example.co.nz:8080/x?y", "example.co.nz"),
                                      ("shop.example.com.au", "example.com.au"), ("example.org.au", "example.org.au"),
                                      ("localhost", None), ("", None), (None, None),
                                      ("foo.wixsite.com", None), ("linkedin.com/company/x", None)])
def test_registrable_domain(raw, want):
    assert registrable_domain(raw) == want


def test_shared_zoominfo_id_still_flagged(config):
    res = build(sf=shared_zi_rows(), config=config)
    assert all("possible_duplicate" in a.flags for a in res.accounts)
    assert "shared id" in " ".join(res.accounts[0].notes)


# --- groups, cap, determinism
def test_three_way_group_by_domain(config):
    res = build(sf=[sf_row("S1", "Aaa", Website="g.com.au"), sf_row("S2", "Bbb", Website="www.g.com.au"),
                    sf_row("S3", "Ccc", Website="https://g.com.au")], config=config)
    groups = duplicate_groups(res.accounts)
    assert len(groups) == 1 and len(groups[0]) == 3
    assert "pairs: 3" in summarise(res)


def test_group_of_six_is_flagged(config):
    rows = [sf_row(f"S{i}", f"Name {i}", Website="six.com.au") for i in range(6)]
    res = build(sf=rows, config=config)
    assert len(dup_names(res)) == 6 and not any("large_domain_group" in a.flags for a in res.accounts)


def test_group_over_six_is_large_domain_group(config):
    rows = [sf_row(f"S{i}", f"Name {i}", Website="agency-site.com.au") for i in range(7)]
    res = build(sf=rows, config=config)
    assert dup_names(res) == set() and all("large_domain_group" in a.flags for a in res.accounts)
    assert duplicate_groups(res.accounts) == [] and "large_domain_group: 7 accounts" in summarise(res)


def test_name_group_over_six_with_headcount_evidence_is_large_name_group(config):
    rows = [sf_row("S0", "Zedcorp", 77)] + [sf_row(f"S{i}", f"Zedcorp {chr(64 + i)}x", 77) for i in range(1, 7)]
    res = build(sf=rows, config=config)
    assert len(res.accounts) == 7 and dup_names(res) == set()
    assert all("large_name_group" in a.flags for a in res.accounts)
    assert "large_name_group: 7 accounts" in summarise(res)


def test_name_group_of_six_with_evidence_is_flagged(config):
    rows = [sf_row("S0", "Zedcorp", 77)] + [sf_row(f"S{i}", f"Zedcorp {chr(64 + i)}x", 77) for i in range(1, 6)]
    assert len(dup_names(build(sf=rows, config=config))) == 6


def test_distinct_companies_not_flagged(config):
    res = build(sf=[sf_row("S1", "Alpha Pty Ltd"), sf_row("S2", "Beta Pty Ltd"), sf_row("S3", "Gamma Ltd")],
                config=config)
    assert dup_names(res) == set() and duplicate_groups(res.accounts) == []


def test_duplicates_across_sources(config):
    res = build(sf=[sf_row("S1", "Catapult Communications", 50)], zi=[{"id": "Z7", "name": "Catapult Group"}],
                config=config)
    assert len(res.accounts) == 2 and dup_names(res) == set()        # containment only: no flag


def test_zoominfo_row_sharing_nooks_domain_flagged(config):
    nk = {"id": "N1", "name": "Alpha Co", "domain": "alpha.com.au", "numEmployees": 50}
    res = build(sf=[sf_row("S1", "Totally Different", 60, Website="www.alpha.com.au")], nk=[nk], config=config)
    assert len(res.accounts) == 2 and len(dup_names(res)) == 2


def test_duplicate_flags_independent_of_input_order(config):
    rows = [sf_row("S1", "Aaa", Website="o.com.au"), sf_row("S2", "Bbb", Website="www.o.com.au"),
            sf_row("S3", "Meridian", 40), sf_row("S4", "Meridian Informatics", 40), sf_row("S5", "Plain")]
    a = build(sf=rows, config=config)
    b = build(sf=list(reversed(rows)), config=config)
    snap = lambda r: [(x.name, x.flags, x.notes) for x in r.accounts]   # noqa: E731
    assert snap(a) == snap(b) and len(dup_names(a)) == 4


def test_duplicate_groups_reads_build_flags_including_contact_evidence(config):
    shared = names("c", 3)
    res = two(config, "Meridian", "Meridian Informatics", ca=shared, cb=shared)
    assert len(duplicate_groups(res.accounts)) == 1                  # no contacts passed here


def test_duplicate_groups_fresh_detection_order_and_no_mutation():
    a = [Account("1", "Zulu", website="z.com"), Account("2", "Catapult", website="c.com"), Account("3", "Alpha"),
         Account("4", "Other", website="www.c.com"), Account("5", "Zulu Labs", website="z.com")]
    groups = duplicate_groups(a)
    assert [[x.sf_account_id for x in g] for g in groups] == [["1", "5"], ["2", "4"]]
    assert all(x.flags == [] and x.notes == [] for x in a)


def test_duplicate_groups_fresh_with_contacts_param():
    shared = names("c", 3)
    a = [Account("1", "Meridian"), Account("2", "Meridian Informatics")]
    assert duplicate_groups(a) == []
    assert len(duplicate_groups(a, {"1": shared, "2": shared})) == 1


def test_duplicate_groups_accepts_iterators():
    gen = (Account(str(i), n, website="a.com") for i, n in enumerate(["Aaa", "Bbb"]))
    assert len(duplicate_groups(gen)) == 1


def test_flag_probable_duplicates_returns_group_count_and_is_idempotent():
    a = [Account("1", "Aaa", website="a.com"), Account("2", "Bbb", website="a.com"), Account("3", "Other")]
    assert flag_probable_duplicates(a) == 1
    snap = [(x.flags[:], x.notes[:]) for x in a]
    flag_probable_duplicates(a)
    assert snap == [(x.flags, x.notes) for x in a] and a[2].flags == []


# --- summary
def test_summary_lists_pairs_with_evidence(config):
    res = two(config, "Freelancer", "Freelancer Technology", ea=318, eb=318)
    text = summarise(res)
    assert "possible duplicate pairs: 1 (evidence: name+same headcount: 1; top 10 listed with names)" in text
    assert "Freelancer ~ Freelancer Technology [name+same headcount]" in text


def test_summary_evidence_breakdown_mixed(config):
    res = build(sf=[sf_row("S1", "Aaa", Website="a.com"), sf_row("S2", "Bbb", Website="a.com"),
                    sf_row("S3", "Freelancer", 1999), sf_row("S4", "Freelancer Technology", 1999)],
                config=config)
    text = summarise(res)
    assert "possible duplicate pairs: 2" in text and "same website domain: 1" in text and "name+same headcount: 1" in text


def test_summary_top_ten_cap(config):
    rows = [sf_row(f"A{i}", f"Pair{i}a", Website=f"p{i}.com") for i in range(12)]
    rows += [sf_row(f"B{i}", f"Pair{i}b", Website=f"www.p{i}.com") for i in range(12)]
    text = summarise(build(sf=rows, config=config))
    assert "possible duplicate pairs: 12" in text
    assert sum(1 for l in text.splitlines() if l.startswith("    Pair")) == 10


def test_summary_no_duplicates_line(config):
    assert "possible duplicate pairs: 0 (evidence: none;" in summarise(build(sf=[sf_row()], config=config))


def test_summary_pair_with_dropped_twin_not_counted(config):
    res = build(sf=[sf_row("S1", "Aaa", Current_Customer__c=True, Website="a.com"), sf_row("S2", "Bbb", Website="a.com")],
                config=config)
    assert "possible duplicate pairs: 0" in summarise(res)
    assert "possible_duplicate" in res.accounts[0].flags            # still flagged, twin named in the note
    assert "'Aaa'" in " ".join(res.accounts[0].notes)
