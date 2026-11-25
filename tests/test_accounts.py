"""Tests for src/accounts.py (task 1d). Synthetic fixtures only; fully offline."""
import copy
from datetime import date, timedelta

import pytest

from src.accounts import (AccountListResult, Dropped, build_account_list, confidence, list_flagged,
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


def test_salesforce_book_account_not_local_business_dropped(config):
    # curated SF book: a CBD-style name with unknown size is kept (size_unknown), not noise
    res = build(sf=[sf_row(name="Fakeville CBD Group Services", emp=None)], config=config)
    assert len(res.accounts) == 1 and res.accounts[0].size_unknown


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
    assert any(n.startswith("flagged_non_anz_billing_anz_hq") for n in a.notes)
    assert confidence(a) == "low"


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


def test_dedupe_by_nooks_account_id(config):
    nk = {"id": "N1", "name": "Acme", "numEmployees": 50}
    res = build(nk=[nk, dict(nk, name="Acme (dup)")], config=config)
    assert len(res.accounts) == 1


def test_dedupe_by_sf_id_within_salesforce(config):
    res = build(sf=[sf_row("S1", "Acme Pty Ltd"), sf_row("S1", "Acme Pty Ltd")], config=config)
    assert len(res.accounts) == 1


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
    assert any(n.startswith("zoominfo_only") for n in a.notes) and confidence(a) == "low"
    assert a.size_unknown and a.country is None


def test_nooks_only_kept_and_flagged(config):
    res = build(nk=[{"id": "N1", "name": "Nov Networks", "numEmployees": 60}], config=config)
    a = res.accounts[0]
    assert any(n.startswith("nooks_only") for n in a.notes) and confidence(a) == "low"


def test_zoominfo_only_not_flagged_when_in_salesforce(config):
    res = build(sf=[sf_row("S1", "Acme Pty Ltd", **{"DOZISF__ZoomInfo_Id__c": "Z1"})],
                zi=[{"id": "Z1", "name": "Acme Pty Ltd"}], config=config)
    assert not any("zoominfo_only" in n for n in res.accounts[0].notes)


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
