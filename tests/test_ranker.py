"""Tests for src/ranker.py. Synthetic data only; fully offline."""
import json
import random
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.models import Account, Prospect, Signal, SignalKind
from src.ranker import (RankResult, account_key, pick_prospect, pick_prospects, rank_accounts,
                        score_account, summarise)
from tests.conftest import load_fixture

# Wed 2026-11-25 20:00 UTC = Thu 07:00 Sydney (AEDT), before the morning window.
NOW = datetime(2026, 11, 25, 20, 0, tzinfo=timezone.utc)
SYD = ZoneInfo("Australia/Sydney")


def acct(i="A1", employees=500, tz="Australia/Sydney", **kw):
    return Account(sf_account_id=f"FAKE-{i}", name=kw.pop("name", f"Acct {i}"), employees=employees,
                   timezone=tz, **kw)


# Signal ages are measured against the account-LOCAL call date. For the default NOW the first usable day is
# Thu 2026-11-26 (Sydney), so sig(days=d) is published d days before that date (UTC noon, no day-boundary games).
CALL_DATE = date(2026, 11, 26)
SIGDAY = datetime(2026, 11, 26, 3, 0, tzinfo=timezone.utc)


def sig(i="A1", kind=SignalKind.FUNDING, strength=1.0, days=0, source=None):
    return Signal(f"FAKE-{i}", kind, strength, source or f"zoominfo_scoop:{i}-{kind.value}-{days}",
                  SIGDAY - timedelta(days=days), "evidence")


def pros(i="A1", title="Head of Product", phone="+61400000001", name="Pat", **kw):
    return Prospect(nooks_id=f"P-{i}-{name}", sf_contact_id=None, sf_account_id=f"FAKE-{i}",
                    nooks_account_id=None, name=name, title=title, phone_e164=phone, **kw)


def run(accounts, sigs=None, prospects=(), cool=None, last=None, cfg=None, now=NOW):
    return rank_accounts(accounts, sigs or {}, list(prospects), cool or {}, last or set(), cfg, now)


def score_of(cfg, a, sigs=(), prospect=True, call_date=CALL_DATE):
    return score_account(a, list(sigs), prospect, cfg, call_date)


# ------------------------------------------------------------ score components in isolation
def test_trigger_strength_scales_weight(config):
    _, b = score_of(config, acct(employees=None), [sig(strength=0.5)], prospect=False)
    assert b["trigger_strength"] == pytest.approx(20.0)


def test_trigger_none_kind_is_zero(config):
    s = Signal("FAKE-A1", SignalKind.NONE, 0.0, "x", None, "")
    r = run([acct()], {"FAKE-A1": [s]}, [pros()], cfg=config)
    assert r.ranked[0].score_breakdown["trigger_strength"] == 0
    assert r.ranked[0].reasons[0] == "No trigger: ranked on fit only"


def test_combined_signals_use_signals_module_formula(config):
    from src import signals as sm
    sigs = [sig(strength=0.5, days=1), sig(kind=SignalKind.NEW_HIRE, strength=0.5, days=2)]
    _, b = score_of(config, acct(), sigs)
    assert sm.combined_strength(sigs) == 0.625
    assert b["trigger_strength"] == pytest.approx(40 * 0.625)
    many = [sig(kind=k, strength=0.95) for k in (SignalKind.FUNDING, SignalKind.MA, SignalKind.NEW_HIRE)]
    _, b = score_of(config, acct(), many)
    assert b["trigger_strength"] == pytest.approx(40 * sm.combined_strength(many), abs=0.01) and b["trigger_strength"] <= 40


def test_intent_never_best_while_dated_signal_exists(config):
    fund = sig(strength=0.1585, days=45)
    intent = sig(kind=SignalKind.INTENT, strength=0.3, days=0, source="zoominfo_intent:Z")
    r = run([acct()], {"FAKE-A1": [fund, intent]}, [pros()], cfg=config).ranked[0]
    assert r.reasons[0].startswith("Funding") and r.signals[0].kind is SignalKind.FUNDING
    assert r.signals[1].kind is SignalKind.INTENT


def test_intent_only_account_uses_intent_as_best(config):
    intent = sig(kind=SignalKind.INTENT, strength=0.3, days=2, source="zoominfo_intent:Z")
    r = run([acct()], {"FAKE-A1": [intent]}, [pros()], cfg=config).ranked[0]
    assert r.reasons[0] == "Intent signal 2 days ago"


@pytest.mark.parametrize("emp,factor", [(500, 1.0), (100, 0.6), (30, 0.3), (None, 0.3), (5000, 0.0), (5, 0.0)])
def test_icp_fit_tiers(config, emp, factor):
    _, b = score_of(config, acct(employees=emp), [], prospect=False)
    assert b["icp_fit"] == pytest.approx(25 * factor)


def test_recency_fresh_and_old(config):
    _, b = score_of(config, acct(), [sig(days=0)])
    assert b["recency"] == 15
    _, b = score_of(config, acct(), [sig(days=45)])
    assert b["recency"] == 0
    _, b = score_of(config, acct(), [sig(days=90)])
    assert b["recency"] == 0          # floored, never negative


def test_recency_linear(config):
    _, b = score_of(config, acct(), [sig(days=9)])
    assert b["recency"] == pytest.approx(15 * (1 - 9 / 45))


def test_recency_future_date_treated_as_today(config):
    s = replace(sig(), source_date=SIGDAY + timedelta(days=3))
    _, b = score_of(config, acct(), [s])
    assert b["recency"] == 15


def test_recency_no_date_is_zero(config):
    s = replace(sig(), source_date=None)
    _, b = score_of(config, acct(), [s])
    assert b["recency"] == 0


def test_named_prospect_points(config):
    _, yes = score_of(config, acct(), [], prospect=True)
    _, no = score_of(config, acct(), [], prospect=False)
    assert yes["named_prospect_with_phone"] == 15 and no["named_prospect_with_phone"] == 0


def test_score_clamped_at_zero(config):
    s, b = score_of(config, acct(employees=5, non_anz_billing_anz_hq=True), [], prospect=False)
    assert s == 0 and b["clamp_adjustment"] == 3 and sum(b.values()) == 0


def test_no_cooldown_component_in_breakdown(config):
    _, b = score_of(config, acct(), [sig()])
    assert "cooldown_penalty" not in b


def test_sdr_boost_points(config):
    _, b = score_of(config, acct(is_sdr_target=True), [])
    assert b["sdr_target_boost"] == 10
    _, b = score_of(config, acct(), [])
    assert b["sdr_target_boost"] == 0


def test_flagged_group_penalty(config):
    _, b = score_of(config, acct(non_anz_billing_anz_hq=True), [])
    assert b["flagged_group_penalty"] == -3
    s1, _ = score_of(config, acct(non_anz_billing_anz_hq=True), [])
    s2, _ = score_of(config, acct(), [])
    assert s1 == s2 - 3


@pytest.mark.parametrize("kw", [dict(), dict(is_sdr_target=True), dict(non_anz_billing_anz_hq=True)])
def test_breakdown_sums_to_score(config, kw):
    s, b = score_of(config, acct(**kw), [sig(strength=0.7, days=4)])
    assert sum(b.values()) == pytest.approx(s)


def test_clamp_to_100_recorded(config):
    s, b = score_of(config, acct(is_sdr_target=True), [sig()])   # 40+25+15+15+10 = 105
    assert s == 100
    assert b["clamp_adjustment"] == -5
    assert sum(b.values()) == pytest.approx(100)


def test_no_clamp_key_when_in_range(config):
    _, b = score_of(config, acct(), [sig(strength=0.5)])
    assert "clamp_adjustment" not in b


# ------------------------------------------------------------ cooldown / last sheet via rank
def skipped_reason_of(r, i="FAKE-A1"):
    return next(x.skipped_reason for x in r.skipped if x.account.sf_account_id == i)


def test_cooldown_active_is_skipped_with_reason(config):
    r = run([acct()], {}, [pros()], cool={"FAKE-A1": date(2026, 11, 30)}, cfg=config)
    assert not r.ranked and not r.needs_prospect and not r.spilled
    assert skipped_reason_of(r) == "In cooldown until 2026-11-30 (account-local)"


def test_cooldown_skip_also_applies_to_needs_prospect_accounts(config):
    r = run([acct()], {}, [], cool={"FAKE-A1": date(2026, 11, 30)}, cfg=config)
    assert not r.needs_prospect and skipped_reason_of(r).startswith("In cooldown until")


def test_cooldown_skip_not_ranked_even_with_strong_signal(config):
    r = run([acct()], {"FAKE-A1": [sig()]}, [pros()], cool={"FAKE-A1": date(2026, 11, 30)}, cfg=config)
    assert not r.ranked and r.prospects == {}


def test_cooldown_expired_no_penalty(config):
    p = [pros()]
    plain = run([acct()], {}, p, cfg=config).ranked[0].score
    r = run([acct()], {}, p, cool={"FAKE-A1": date(2026, 11, 26)}, cfg=config).ranked[0]
    assert r.score == plain      # until == call date (Thu 26th Sydney) means callable again


def test_last_sheet_is_hard_skip(config):
    r = run([acct("A1"), acct("A2")], {"FAKE-A1": [sig("A1")]}, [pros("A1"), pros("A2")],
            last={"FAKE-A1"}, cfg=config)
    assert [x.account.sf_account_id for x in r.ranked] == ["FAKE-A2"]
    assert r.skipped[0].skipped_reason == "on last sheet"


def test_last_sheet_matches_zoominfo_id(config):
    a = acct(zoominfo_id="ZI-1")
    r = run([a], {}, [pros()], last={"ZI-1"}, cfg=config)
    assert not r.ranked and r.skipped[0].skipped_reason == "on last sheet"


def test_last_sheet_skips_needs_prospect_accounts_too(config):
    r = run([acct()], {}, [], last={"FAKE-A1"}, cfg=config)
    assert not r.needs_prospect and r.skipped[0].skipped_reason == "on last sheet"


# ---- cooldown uses the account-LOCAL call date (H2)
def test_cooldown_first_callable_day_is_callable_even_when_utc_date_is_earlier(config):
    # Run Tue 20:00 AEDT = Tue 09:00 UTC; call date is Wed 2026-12-02 local. cooldown_until == Wed => callable.
    now = datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc)
    plain = run([acct()], {}, [pros()], cfg=config, now=now).ranked[0].score
    r = run([acct()], {}, [pros()], cool={"FAKE-A1": date(2026, 12, 2)}, cfg=config, now=now).ranked[0]
    assert r.score == plain and r.local_call_time.date() == date(2026, 12, 2)


def test_cooldown_day_after_call_date_still_active(config):
    now = datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc)
    plain = run([acct()], {}, [pros()], cfg=config, now=now).ranked[0].score
    r = run([acct()], {}, [pros()], cool={"FAKE-A1": date(2026, 12, 3)}, cfg=config, now=now)
    assert not r.ranked and skipped_reason_of(r) == "In cooldown until 2026-12-03 (account-local)"


def test_cooldown_utc_date_earlier_than_local(config):
    # now = Wed 23:00 UTC = Thu 10:00 Sydney (inside morning). Call date is Thu 26th. cooldown Thu => callable.
    now = datetime(2026, 11, 25, 23, 0, tzinfo=timezone.utc)
    plain = run([acct()], {}, [pros()], cfg=config, now=now).ranked[0].score
    r = run([acct()], {}, [pros()], cool={"FAKE-A1": date(2026, 11, 26)}, cfg=config, now=now).ranked[0]
    assert r.score == plain


def test_cooldown_datetime_value_converted_not_typeerror(config):
    now = datetime(2026, 11, 25, 23, 0, tzinfo=timezone.utc)
    plain = run([acct()], {}, [pros()], cfg=config, now=now).ranked[0].score
    r = run([acct()], {}, [pros()], cool={"FAKE-A1": datetime(2026, 11, 30, 5, 0)}, cfg=config, now=now)
    assert skipped_reason_of(r) == "In cooldown until 2026-11-30 (account-local)"


def test_cooldown_bad_type_valueerror(config):
    with pytest.raises(ValueError, match="cooldown_until"):
        run([acct()], {}, [pros()], cool={"FAKE-A1": "2026-12-01"}, cfg=config)


def test_cooldown_keyed_by_nooks_id(config):
    a = acct(nooks_account_id="NK-5")
    plain = run([a], {}, [pros()], cfg=config).ranked[0].score
    r = run([a], {}, [pros()], cool={"NK-5": date(2026, 12, 20)}, cfg=config)
    assert not r.ranked and skipped_reason_of(r).startswith("In cooldown")


# ------------------------------------------------------------ SDR boost is not a filter
def test_sdr_boost_not_a_filter(config):
    r = run([acct("A1", is_sdr_target=True), acct("A2")], {}, [pros("A1"), pros("A2")], cfg=config)
    assert len(r.ranked) == 2
    assert r.ranked[0].account.sf_account_id == "FAKE-A1"


def test_non_sdr_account_can_outrank_sdr_target(config):
    r = run([acct("A1", is_sdr_target=True, employees=30), acct("A2")], {"FAKE-A2": [sig("A2")]},
            [pros("A1"), pros("A2")], cfg=config)
    assert r.ranked[0].account.sf_account_id == "FAKE-A2"


# ------------------------------------------------------------ prospects
def test_persona_primary_before_secondary(config):
    ps = [pros(title="Product Manager", name="Sec"), pros(title="Head of Product", name="Pri")]
    assert pick_prospect(ps, config).name == "Pri"


def test_persona_seniority_order_within_primary(config):
    ps = [pros(title="Director of Product", name="Dir"), pros(title="VP Product", name="Vp")]
    assert pick_prospect(ps, config).name == "Vp"      # VP Product listed before Director of Product


def test_persona_case_insensitive_substring(config):
    assert pick_prospect([pros(title="SENIOR PRODUCT MANAGER, payments")], config) is not None


def test_persona_word_boundary_cto_vs_director(config):
    assert pick_prospect([pros(title="Director of Finance")], config) is None
    assert pick_prospect([pros(title="CTO")], config) is not None


def test_persona_no_match(config):
    assert pick_prospect([pros(title="Accountant")], config) is None
    assert pick_prospect([pros(title=None)], config) is None


def test_prospect_without_phone_never_chosen(config):
    assert pick_prospect([pros(phone=None)], config) is None


def test_phone_beats_better_title_without_phone(config):
    ps = [pros(title="Head of Product", phone=None, name="NoPhone"), pros(title="CTO", name="Phone")]
    assert pick_prospect(ps, config).name == "Phone"


def test_opted_out_prospect_never_chosen(config):
    assert pick_prospect([pros(opted_out_email=True)], config) is None


def test_do_not_call_prospect_never_chosen(config):
    ps = [pros(do_not_call=True, name="Blocked"), pros(title="CTO", name="Ok")]
    assert pick_prospect(ps, config).name == "Ok"


def test_tie_break_by_name_then_input_order_irrelevant(config):
    ps = [pros(name="Zed"), pros(name="amy"), pros(name="Bea")]
    for _ in range(5):
        random.shuffle(ps)
        assert pick_prospect(ps, config).name == "amy"


def test_max_per_account_per_run(config, raw_config):
    from src.config import parse_config
    raw_config["limits"]["max_per_account_per_run"] = 2
    cfg2 = parse_config(raw_config)
    ps = [pros(name="A"), pros(name="B"), pros(name="C")]
    assert [p.name for p in pick_prospects(ps, cfg2)] == ["A", "B"]
    assert len(pick_prospects(ps, config)) == 1


def test_only_prospect_blocked_skips_account(config):
    r = run([acct()], {}, [pros(do_not_call=True)], cfg=config)
    assert not r.ranked and "blocked" in r.skipped[0].skipped_reason


def test_only_prospect_opted_out_skips_account(config):
    r = run([acct()], {}, [pros(opted_out_email=True)], cfg=config)
    assert "blocked" in r.skipped[0].skipped_reason


def test_no_prospects_goes_to_needs_prospect(config):
    r = run([acct()], {}, [], cfg=config)
    assert not r.ranked and not r.skipped and len(r.needs_prospect) == 1
    ra = r.needs_prospect[0]
    assert r.needs_prospect_codes["FAKE-A1"] == "no_prospects_on_account"
    assert ra.score_breakdown["named_prospect_with_phone"] == 0
    assert ra.window is None and ra.local_call_time is None and ra.skipped_reason is None
    assert ra.reasons[0] == "Needs a prospect: no_prospects_on_account"


def test_persona_match_no_phone_code(config):
    r = run([acct()], {}, [pros(phone=None)], cfg=config)
    assert r.needs_prospect_codes["FAKE-A1"] == "persona_match_no_phone"
    assert r.needs_prospect[0].local_call_time is None


def test_no_persona_match_code(config):
    r = run([acct()], {}, [pros(title="Accountant")], cfg=config)
    assert r.needs_prospect_codes["FAKE-A1"] == "no_persona_match"


def test_blocked_plus_unmatched_is_no_persona_match(config):
    r = run([acct()], {}, [pros(do_not_call=True, name="Bl"), pros(title="Accountant", name="Ac")], cfg=config)
    assert r.needs_prospect_codes["FAKE-A1"] == "no_persona_match"


def test_blocked_matching_plus_phoneless_matching(config):
    r = run([acct()], {}, [pros(do_not_call=True, name="Bl"), pros(phone=None, name="Nop")], cfg=config)
    assert r.needs_prospect_codes["FAKE-A1"] == "persona_match_no_phone"


def test_needs_prospect_never_in_ranked_or_prospects(config):
    r = run([acct("A1"), acct("A2")], {"FAKE-A1": [sig("A1")]}, [pros("A2")], cfg=config)
    assert [x.account.sf_account_id for x in r.ranked] == ["FAKE-A2"]
    assert "FAKE-A1" not in r.prospects
    assert r.needs_prospect[0].account.sf_account_id == "FAKE-A1"


def test_H1_45_no_prospect_do_not_displace_10_callable(config):
    no_p = [acct(f"N{i:02d}", name=f"N{i:02d}") for i in range(45)]
    ok = [acct(f"C{i:02d}", name=f"C{i:02d}", employees=30) for i in range(10)]
    sigs = {a.sf_account_id: [sig(a.sf_account_id[5:])] for a in no_p}
    r = run(no_p + ok, sigs, [pros(a.sf_account_id[5:]) for a in ok], cfg=config)
    assert len(r.ranked) == 10 and all(x.account.sf_account_id.startswith("FAKE-C") for x in r.ranked)
    assert len(r.needs_prospect) == 45          # uncapped, even though cap is 40
    assert r.needs_prospect[0].score >= r.needs_prospect[-1].score
    assert all(x.local_call_time is None for x in r.needs_prospect)


def test_cap_counts_callable_only(config):
    no_p = [acct(f"N{i:02d}", name=f"N{i:02d}") for i in range(30)]
    ok = [acct(f"C{i:02d}", name=f"C{i:02d}") for i in range(41)]
    r = run(no_p + ok, {}, [pros(a.sf_account_id[5:]) for a in ok], cfg=config)
    assert len(r.ranked) == 40 and len(r.needs_prospect) == 30
    assert sum(1 for s in r.skipped if s.skipped_reason.startswith("below daily cap")) == 1


def test_needs_prospect_still_scored_with_zero_prospect_points(config):
    r = run([acct()], {"FAKE-A1": [sig()]}, [], cfg=config)
    assert r.needs_prospect[0].score == 40 + 25 + 15


def test_prospect_linked_by_nooks_account_id(config):
    a = acct(nooks_account_id="NK-1")
    p = Prospect("n1", None, None, "NK-1", "Nora", "CTO", phone_e164="+61400000009")
    r = run([a], {}, [p], cfg=config)
    assert r.ranked[0].score_breakdown["named_prospect_with_phone"] == 15
    assert r.prospects[account_key(a)] == [p]


# ------------------------------------------------------------ timezone / windows
def test_timezone_none_skipped_before_helper(config, monkeypatch):
    import src.ranker as ranker
    def boom(*a, **k):
        raise AssertionError("window helper called for a None timezone")
    monkeypatch.setattr(ranker, "next_working_windows", boom)
    r = run([acct(tz=None)], {}, [pros()], cfg=config)
    assert r.skipped[0].skipped_reason == "no timezone" and not r.ranked


def test_empty_windows_skipped_with_reason(config, monkeypatch):
    import src.ranker as ranker
    monkeypatch.setattr(ranker, "next_working_windows", lambda *a, **k: [])
    r = run([acct()], {}, [pros()], cfg=config)
    assert "no call window" in r.skipped[0].skipped_reason and not r.ranked


def test_before_window_uses_window_start(config):
    ra = run([acct()], {}, [pros()], cfg=config).ranked[0]
    assert ra.window == "morning"
    assert ra.local_call_time == datetime(2026, 11, 26, 8, 45, tzinfo=SYD)


def test_now_inside_window_clamps_to_now(config):
    now = datetime(2026, 11, 25, 22, 30, tzinfo=timezone.utc)      # Thu 09:30 Sydney, inside morning
    ra = run([acct()], {}, [pros()], cfg=config, now=now).ranked[0]
    assert ra.window == "morning"
    assert ra.local_call_time >= now and ra.local_call_time < datetime(2026, 11, 26, 11, 30, tzinfo=SYD)


def test_now_after_morning_gets_afternoon(config):
    now = datetime(2026, 11, 26, 2, 0, tzinfo=timezone.utc)        # Thu 13:00 Sydney
    ra = run([acct()], {}, [pros()], cfg=config, now=now).ranked[0]
    assert ra.window == "afternoon"
    assert ra.local_call_time.hour in (13, 14, 15, 16)


def test_friday_after_hours_goes_to_monday(config):
    now = datetime(2026, 11, 27, 9, 0, tzinfo=timezone.utc)        # Fri 20:00 Sydney
    ra = run([acct()], {}, [pros()], cfg=config, now=now).ranked[0]
    assert ra.local_call_time.date() == date(2026, 11, 30)
    assert ra.local_call_time.weekday() == 0 and ra.window == "morning"


def test_naive_now_rejected(config):
    with pytest.raises(ValueError):
        run([acct()], cfg=config, now=datetime(2026, 11, 25, 20, 0))


def test_different_timezones_same_instant(config):
    accts = [acct("S", tz="Australia/Sydney"), acct("P", tz="Australia/Perth"),
             acct("N", tz="Pacific/Auckland")]
    r = run(accts, {}, [pros("S"), pros("P"), pros("N")], cfg=config)
    times = {x.account.sf_account_id: x.local_call_time for x in r.ranked}
    # Same instant = Wed 20:00 UTC: Sydney Thu 07:00, Auckland Thu 09:00 (inside morning), Perth Thu 04:00.
    assert times["FAKE-S"] == datetime(2026, 11, 26, 8, 45, tzinfo=SYD)
    assert times["FAKE-P"] == datetime(2026, 11, 26, 8, 45, tzinfo=ZoneInfo("Australia/Perth"))
    akl = times["FAKE-N"]
    assert akl.tzinfo.key == "Pacific/Auckland" and akl.date() == date(2026, 11, 26)
    assert akl >= NOW      # clamped to now (09:00 NZDT), not the 08:45 start
    assert akl == datetime(2026, 11, 26, 9, 1, tzinfo=ZoneInfo("Pacific/Auckland"))   # now 09:00:00 + 1 min lead


def test_perth_afternoon_while_sydney_next_morning(config):
    now = datetime(2026, 11, 26, 5, 0, tzinfo=timezone.utc)   # Thu 16:00 Sydney (late), 13:00 Perth
    r = run([acct("S"), acct("P", tz="Australia/Perth")], {}, [pros("S"), pros("P")], cfg=config, now=now)
    w = {x.account.sf_account_id: x.window for x in r.ranked}
    assert w["FAKE-S"] == "afternoon" and w["FAKE-P"] == "afternoon"
    now = datetime(2026, 11, 26, 6, 0, tzinfo=timezone.utc)   # Sydney 17:00 (past), Perth 14:00 (open)
    r = run([acct("S"), acct("P", tz="Australia/Perth")], {}, [pros("S"), pros("P")], cfg=config, now=now)
    t = {x.account.sf_account_id: x for x in r.ranked}
    assert t["FAKE-S"].local_call_time.date() == date(2026, 11, 27)
    assert t["FAKE-P"].local_call_time.date() == date(2026, 11, 26)


def test_window_grouping_sorted_by_score(config):
    accts = [acct(f"A{i}", name=f"N{i}", employees=[30, 500, 100][i % 3]) for i in range(9)]
    r = run(accts, {}, [pros(a.sf_account_id[5:]) for a in accts], cfg=config)
    for name in ("morning", "afternoon"):
        scores = [x.score for x in r.by_window[name]]
        assert scores == sorted(scores, reverse=True)
    assert len(r.by_window["morning"]) + len(r.by_window["afternoon"]) == 9
    assert all(x.window == "morning" for x in r.by_window["morning"])


def test_uses_both_windows_of_first_day(config):
    accts = [acct(f"A{i:02d}", name=f"N{i:02d}") for i in range(40)]
    r = run(accts, {}, [pros(a.sf_account_id[5:]) for a in accts], cfg=config)
    assert r.by_window["morning"] and r.by_window["afternoon"]
    assert all(x.local_call_time.date() == date(2026, 11, 26) for x in r.ranked)


def test_spread_distinct_times_min_slot_and_inside_windows(config):
    accts = [acct(f"A{i:02d}", name=f"N{i:02d}") for i in range(40)]
    r = run(accts, {}, [pros(a.sf_account_id[5:]) for a in accts], cfg=config)
    times = [x.local_call_time for x in r.ranked]
    assert times == sorted(times)            # best score gets the earliest slot
    assert all(b - a >= timedelta(minutes=3) for a, b in zip(times, times[1:]))
    day = date(2026, 11, 26)
    m = (datetime(day.year, day.month, day.day, 8, 45, tzinfo=SYD), datetime(2026, 11, 26, 11, 30, tzinfo=SYD))
    a = (datetime(2026, 11, 26, 13, 30, tzinfo=SYD), datetime(2026, 11, 26, 16, 30, tzinfo=SYD))
    for x in r.ranked:
        lo, hi = m if x.window == "morning" else a
        assert lo <= x.local_call_time < hi
    assert times[0] == m[0]


def test_late_in_window_spills_to_next_day_not_squeezed(config):
    # Thu 16:00 Sydney: first slot 16:01, 29 min left => 9 slots tonight; the other 31 are HELD for Friday.
    now = datetime(2026, 11, 26, 5, 0, tzinfo=timezone.utc)
    accts = [acct(f"A{i:02d}", name=f"N{i:02d}") for i in range(40)]
    r = run(accts, {}, [pros(a.sf_account_id[5:]) for a in accts], cfg=config, now=now)
    assert len(r.ranked) == 9 and len(r.spilled) == 31 and not r.skipped
    assert all(x.local_call_time.date() == date(2026, 11, 26) and x.local_call_time >= datetime(2026, 11, 26, 16, 1, tzinfo=SYD)
               for x in r.ranked)
    ts = sorted(x.local_call_time for x in r.ranked)
    assert all(b - a >= timedelta(minutes=3) for a, b in zip(ts, ts[1:]))
    assert all(x.local_call_time.date() == date(2026, 11, 27) for x in r.spilled)
    assert any("Spilled to Fri" in rr for rr in r.spilled[0].reasons)
    # held accounts are not tasks: not in ranked, not in prospects, no cap slot used
    ids = {x.account.sf_account_id for x in r.ranked}
    assert not ids & {x.account.sf_account_id for x in r.spilled}
    assert set(r.prospects) == ids
    # best accounts take tonight's slots
    assert max(x.account.name for x in r.ranked) < min(x.account.name for x in r.spilled)


def test_window_with_under_min_slot_left_unused(config):
    now = datetime(2026, 11, 26, 5, 28, tzinfo=timezone.utc)      # Thu 16:28 Sydney, 2 minutes left
    r = run([acct()], {}, [pros()], cfg=config, now=now)
    assert r.ranked[0].local_call_time == datetime(2026, 11, 27, 8, 45, tzinfo=SYD)


def test_now_inside_window_everyone_after_now(config):
    now = datetime(2026, 11, 25, 22, 30, tzinfo=timezone.utc)      # Thu 09:30 Sydney
    accts = [acct(f"A{i:02d}", name=f"N{i:02d}") for i in range(5)]
    r = run(accts, {}, [pros(a.sf_account_id[5:]) for a in accts], cfg=config, now=now)
    assert all(x.local_call_time >= now for x in r.ranked)
    assert r.ranked[0].local_call_time == datetime(2026, 11, 26, 9, 31, tzinfo=SYD)   # now 09:30:00 + 1 min


def test_guessed_timezone_treated_like_confirmed(config):
    # The later-half rule was dropped: guessed and confirmed share one timeline (no shared minutes).
    r = run([acct("A1"), acct("A2", timezone_guessed=True)], {}, [pros("A1"), pros("A2")], cfg=config)
    t = sorted(x.local_call_time for x in r.ranked)
    assert t[1] - t[0] >= timedelta(minutes=3)
    g = [x for x in r.ranked if x.account.timezone_guessed][0]
    assert "Timezone guessed (country default)" in g.reasons


def test_timezones_scheduled_independently(config):
    accts = [acct("S1"), acct("S2"), acct("P1", tz="Australia/Perth")]
    r = run(accts, {}, [pros("S1"), pros("S2"), pros("P1")], cfg=config)
    perth = [x for x in r.ranked if x.account.timezone == "Australia/Perth"][0]
    assert perth.local_call_time == datetime(2026, 11, 26, 8, 45, tzinfo=ZoneInfo("Australia/Perth"))


def test_local_call_time_is_aware_in_account_tz(config):
    ra = run([acct(tz="Australia/Perth")], {}, [pros()], cfg=config).ranked[0]
    assert ra.local_call_time.tzinfo.key == "Australia/Perth"


# ------------------------------------------------------------ cap
def test_cap_truncates_after_scoring(config):
    accts = [acct(f"A{i:02d}", name=f"N{i:02d}") for i in range(45)]
    r = run(accts, {}, [pros(a.sf_account_id[5:]) for a in accts], cfg=config)
    assert len(r.ranked) == 40 and len(r.skipped) == 5
    assert all(s.skipped_reason.startswith("below daily cap") for s in r.skipped)


def test_cap_applied_after_skips(config):
    good = [acct(f"G{i:02d}", name=f"G{i:02d}") for i in range(40)]
    bad = [acct(f"B{i:02d}", tz=None, name=f"A{i:02d}") for i in range(10)]   # would sort first by name
    r = run(bad + good, {}, [pros(a.sf_account_id[5:]) for a in good], cfg=config)
    assert len(r.ranked) == 40
    assert sum(1 for s in r.skipped if s.skipped_reason == "no timezone") == 10


def test_cap_keeps_best_scores(config):
    accts = [acct(f"A{i:02d}", name=f"N{i:02d}") for i in range(41)]
    sigs = {"FAKE-A40": [sig("A40")]}
    r = run(accts, sigs, [pros(a.sf_account_id[5:]) for a in accts], cfg=config)
    assert r.ranked[0].account.sf_account_id == "FAKE-A40"
    assert "FAKE-A40" not in {s.account.sf_account_id for s in r.skipped}


# ------------------------------------------------------------ reasons
def test_reasons_funding_days_ago(config):
    r = run([acct()], {"FAKE-A1": [sig(days=6)]}, [pros()], cfg=config).ranked[0]
    assert r.reasons[0] == "Funding scoop 6 days ago"


def test_reason_one_day_singular_and_non_scoop_noun(config):
    s = sig(days=1, source="manual:1")
    r = run([acct()], {"FAKE-A1": [s]}, [pros()], cfg=config).ranked[0]
    assert r.reasons[0] == "Funding signal 1 day ago"


def test_reasons_sdr_and_no_trigger(config):
    r = run([acct(is_sdr_target=True)], {}, [pros()], cfg=config).ranked[0]
    assert "SDR target" in r.reasons and r.reasons[0] == "No trigger: ranked on fit only"


def test_reasons_size_unknown_and_flagged(config):
    r = run([acct(employees=None, non_anz_billing_anz_hq=True)], {}, [pros()], cfg=config).ranked[0]
    assert any("Headcount unknown" in x for x in r.reasons)
    assert any("Non-ANZ billing" in x for x in r.reasons)


def test_signals_not_invented_for_other_account(config):
    r = run([acct("A1"), acct("A2")], {"FAKE-A1": [sig("A1")]}, [pros("A1"), pros("A2")], cfg=config)
    other = [x for x in r.ranked if x.account.sf_account_id == "FAKE-A2"][0]
    assert other.signals == [] and other.reasons[0].startswith("No trigger")


def test_signals_joined_by_zoominfo_id(config):
    a = acct(zoominfo_id="ZI-9")
    s = Signal(None, SignalKind.MA, 0.6, "zoominfo_scoop:9", NOW - timedelta(days=3), "e", zoominfo_id="ZI-9")
    r = run([a], {"ZI-9": [s]}, [pros()], cfg=config).ranked[0]
    assert r.score_breakdown["trigger_strength"] == pytest.approx(24.0)


def test_signal_found_under_both_keys_not_double_counted(config):
    a = acct(zoominfo_id="ZI-9")
    s = Signal("FAKE-A1", SignalKind.MA, 0.6, "zoominfo_scoop:9", NOW, "e", zoominfo_id="ZI-9")
    r = run([a], {"FAKE-A1": [s], "ZI-9": [s]}, [pros()], cfg=config).ranked[0]
    assert r.score_breakdown["trigger_strength"] == pytest.approx(24.0)


# ------------------------------------------------------------ determinism
def test_shuffled_input_same_output(config):
    accts = [acct(f"A{i:02d}", name=f"N{i % 7}", employees=[30, 100, 500][i % 3], is_sdr_target=i % 4 == 0)
             for i in range(30)]
    sigs = {f"FAKE-A{i:02d}": [sig(f"A{i:02d}", strength=0.3 + (i % 5) / 10, days=i % 9)] for i in range(0, 30, 2)}
    ps = [pros(a.sf_account_id[5:]) for a in accts]

    def snapshot(res):
        return [(r.account.sf_account_id, r.score, r.window, r.local_call_time) for r in res.ranked], \
               [(s.account.sf_account_id, s.skipped_reason) for s in res.skipped]

    base = snapshot(run(accts, sigs, ps, cfg=config))
    rnd = random.Random(1)
    for _ in range(5):
        a2, p2 = accts[:], ps[:]
        rnd.shuffle(a2); rnd.shuffle(p2)
        assert snapshot(run(a2, sigs, p2, cfg=config)) == base


def test_tie_break_name_then_id(config):
    accts = [acct("A3", name="Same"), acct("A1", name="Same"), acct("A2", name="Same")]
    r = run(accts, {}, [pros(x) for x in ("A1", "A2", "A3")], cfg=config)
    assert [x.account.sf_account_id for x in r.ranked] == ["FAKE-A1", "FAKE-A2", "FAKE-A3"]


def test_newer_signal_ranks_first(config):
    a1, a2 = acct("A1"), acct("A2")
    r = run([a1, a2], {"FAKE-A1": [sig("A1", days=10)], "FAKE-A2": [sig("A2", days=2)]},
            [pros("A1"), pros("A2")], cfg=config)
    assert r.ranked[0].account.sf_account_id == "FAKE-A2"


# ------------------------------------------------------------ result shape / summary / fixture
def test_by_window_keys_follow_config(config):
    r = run([], cfg=config)
    assert list(r.by_window) == ["morning", "afternoon"] and isinstance(r, RankResult)


def test_does_not_mutate_inputs(config):
    a = acct()
    run([a], {"FAKE-A1": [sig()]}, [pros()], cfg=config)
    assert a.notes == [] and a.timezone == "Australia/Sydney"


def test_summarise_contents(config):
    accts = [acct("A1"), acct("A2", tz=None)]
    r = run(accts, {}, [pros("A1")], cfg=config)
    text = summarise(r)
    assert "Ranked 1 accounts; skipped 1; needs prospect 0; spilled 0." in text
    assert "skipped - no timezone: 1" in text
    assert "no real trigger (ranked on fit only): 1" in text
    assert "Acct A1" in text and "Thu 08:45" in text


def test_summarise_empty(config):
    assert summarise(run([], cfg=config)).startswith("Ranked 0 accounts; skipped 0; needs prospect 0; spilled 0.")


def test_scenario_fixture_end_to_end(config):
    fx = load_fixture("ranker_scenario.json")
    now = datetime.fromisoformat(fx["now"])
    accounts = [Account(a["id"], a["name"], employees=a["employees"], timezone=a["timezone"],
                        is_sdr_target=a["sdr"]) for a in fx["accounts"]]
    sigs = {k: [Signal(k, SignalKind(s["kind"]), s["strength"], f"zoominfo_scoop:{k}",
                       now - timedelta(days=s["days_ago"]), "e") for s in v] for k, v in fx["signals"].items()}
    ps = [Prospect(p["id"], None, p["account"], None, p["name"], p["title"], phone_e164=p["phone"])
          for p in fx["prospects"]]
    r = rank_accounts(accounts, sigs, ps, {}, set(), config, now)
    assert [x.account.sf_account_id for x in r.ranked] == ["FAKE-A1", "FAKE-A2"]
    assert [x.account.sf_account_id for x in r.needs_prospect] == ["FAKE-A3"]
    assert r.needs_prospect_codes == {"FAKE-A3": "persona_match_no_phone"}
    assert r.ranked[0].reasons[0] == "Funding scoop 7 days ago"   # published Thu 19 Nov UTC, call date Thu 26 Nov Sydney
    assert [s.account.sf_account_id for s in r.skipped] == ["FAKE-A4"]
    assert r.needs_prospect[0].score_breakdown["named_prospect_with_phone"] == 0     # CTO has no phone


# ------------------------------------------------------------ M2 persona normalisation / aliases
@pytest.mark.parametrize("title", [
    "Chief Technology Officer", "Chief Technical Officer", "CTO & Co-founder", "cto",
    "VP, Product", "VP of Product", "Vice President, Product", "Vice President of Product", "vp product, ANZ",
    "Head of CX", "Head of Customer Experience", "Head of Products", "Head of Product & Design",
    "Customer Success Director", "Director of Customer Success", "Head of Customer Success",
    "Product Director", "Director of Product Management", "Chief Product Officer", "CPO",
    "Product Managers", "Senior Product Manager", "Product Manager (Growth)", "Group Product Manager",
    "Head of Customer Support", "Digital Director", "Digital Transformation Manager",
])
def test_persona_titles_that_should_match(config, title):
    from src.ranker import _persona_rank
    assert _persona_rank(title, config) is not None, title


@pytest.mark.parametrize("title", [
    "Director of Product-Led Growth", "Co-CTO", "Director", "Director of Finance", "Product-Led Growth Lead",
    "Head of Productivity", "CIO", "Chief Information Officer", "Accountant", "", "VP Sales",
    "Product Owner",
])
def test_persona_titles_that_should_not_match(config, title):
    from src.ranker import _persona_rank
    assert _persona_rank(title, config) is None, title


def test_persona_cto_alias_is_secondary(config):
    from src.ranker import _persona_rank
    assert _persona_rank("Chief Technology Officer", config)[0] == 1
    assert _persona_rank("VP of Product", config)[0] == 0


def test_persona_longest_match_beats_shadowing_primary(config):
    from src.ranker import _persona_rank
    assert _persona_rank("Group Head of Digital Workplace", config)[0] == 1   # secondary, not 'Head of Digital'
    assert _persona_rank("Head of Digital", config)[0] == 0


def test_persona_equal_length_prefers_primary(config):
    from src.ranker import _persona_rank
    # 'Head of Support' (primary) vs nothing longer: primary wins
    assert _persona_rank("Head of Support", config)[0] == 0


def test_aliases_table_is_documented_constant():
    from src.ranker import ALIASES
    flat = {p for g in ALIASES for p in g}
    assert {"cto", "chief technology officer", "cio", "chief information officer", "cpo",
            "vp product", "head cx", "customer success director"} <= flat


def test_norm_keeps_hyphen_inside_token():
    from src.ranker import _norm
    assert _norm("Director of Product-Led Growth") == ["director", "product-led", "growth"]
    assert _norm("VP, Product & Design") == ["vp", "product", "design"]


def test_vp_alias_wins_pick_with_phone(config):
    ps = [pros(title="Vice President, Product", name="Vee"), pros(title="Product Managers", name="Pm")]
    assert pick_prospect(ps, config).name == "Vee"


# ------------------------------------------------------------ M3 invalid timezone
@pytest.mark.parametrize("tz", ["", "Mars/Base", "Australia/Nowhere", " "])
def test_invalid_timezone_skipped_not_crash(config, tz):
    r = run([acct("A1", tz=tz), acct("A2")], {}, [pros("A1"), pros("A2")], cfg=config)
    assert [x.account.sf_account_id for x in r.ranked] == ["FAKE-A2"]
    assert r.skipped[0].skipped_reason == f"invalid timezone {tz!r}"


# ------------------------------------------------------------ misc review items
def test_naive_signal_date_treated_as_utc(config):
    s = replace(sig(days=3), source_date=(SIGDAY - timedelta(days=3)).replace(tzinfo=None))
    r = run([acct()], {"FAKE-A1": [s]}, [pros()], cfg=config).ranked[0]
    assert r.reasons[0] == "Funding scoop 3 days ago"


def test_intent_signal_has_label(config):
    s = sig(kind=SignalKind.INTENT, strength=0.4, days=2, source="zoominfo_intent:1")
    r = run([acct()], {"FAKE-A1": [s]}, [pros()], cfg=config).ranked[0]
    assert r.reasons[0] == "Intent signal 2 days ago"


def test_duplicate_prospect_objects_counted_once(config):
    p1 = pros(name="Dup")
    p2 = replace(p1)
    r = run([acct()], {}, [p1, p2], cfg=config)
    assert len(r.prospects["FAKE-A1"]) == 1


def test_flags_alone_do_not_change_score(config):
    a, b = acct(), acct(flags=["noise_location"])
    assert score_of(config, a, [sig()])[0] == score_of(config, b, [sig()])[0]


# ------------------------------------------------------------ M3 confidence ceiling / flags
def test_low_confidence_capped_at_ceiling(config):
    from src.ranker import LOW_CONFIDENCE_CEILING
    assert LOW_CONFIDENCE_CEILING == 60
    s, b = score_of(config, acct(confidence="low"), [sig()])        # raw 95
    assert s == 60 and b["confidence_ceiling_adjustment"] == -35
    assert sum(b.values()) == pytest.approx(60)


def test_normal_confidence_not_capped(config):
    s, b = score_of(config, acct(), [sig()])
    assert s == 95 and "confidence_ceiling_adjustment" not in b


def test_ceiling_key_absent_when_it_does_not_bite(config):
    s, b = score_of(config, acct(confidence="low", employees=30), [], prospect=False)
    assert s == 7.5 and "confidence_ceiling_adjustment" not in b


def test_ceiling_then_clamp_breakdown_sums(config):
    s, b = score_of(config, acct(confidence="low", employees=None, is_sdr_target=True, non_anz_billing_anz_hq=True), [], prospect=False)
    assert sum(b.values()) == pytest.approx(s) and 0 <= s <= 60


def test_low_confidence_cannot_beat_normal_in_rank(config):
    lo, hi = acct("A1", confidence="low"), acct("A2", employees=100)
    r = run([lo, hi], {"FAKE-A1": [sig("A1")], "FAKE-A2": [sig("A2", strength=0.5)]},
            [pros("A1"), pros("A2")], cfg=config)
    scores = {x.account.sf_account_id: x.score for x in r.ranked}
    assert scores["FAKE-A1"] == 60 and scores["FAKE-A2"] > 60 and r.ranked[0].account.sf_account_id == "FAKE-A2"


def test_flags_and_confidence_shown_in_reasons(config):
    a = acct(flags=["noise_location", "odd_thing"], confidence="low")
    r = run([a], {}, [pros()], cfg=config).ranked[0]
    assert "Flag: noise_location - check this is a real company" in r.reasons
    assert "Flag: odd_thing - check before calling" in r.reasons
    assert "Low-confidence record (score capped at 60)" in r.reasons


def test_no_flag_reasons_for_clean_account(config):
    r = run([acct()], {}, [pros()], cfg=config).ranked[0]
    assert not any(x.startswith("Flag:") or x.startswith("Low-confidence") for x in r.reasons)


# ------------------------------------------------------------ L1 one age function, local call date
def test_age_matches_signals_age_days_with_local_call_date(config):
    from src import signals as sm
    # Run Wed 20:30 UTC = Thu 07:30 Sydney. Scoop published Sun 22 Nov 13:00 UTC.
    now = datetime(2026, 11, 25, 20, 30, tzinfo=timezone.utc)
    s = replace(sig(), source_date=datetime(2026, 11, 22, 13, 0, tzinfo=timezone.utc))
    r = run([acct()], {"FAKE-A1": [s]}, [pros()], cfg=config, now=now).ranked[0]
    expected = sm.age_days(s.source_date, date(2026, 11, 26))
    assert expected == 4 and r.reasons[0] == "Funding scoop 4 days ago"


def test_age_uses_account_local_date_per_timezone(config):
    # 20:30 UTC Wed: Sydney/Perth/Auckland are all already Thu 26 Nov, so all read the same age.
    now = datetime(2026, 11, 25, 20, 30, tzinfo=timezone.utc)
    s = replace(sig(), source_date=datetime(2026, 11, 22, 13, 0, tzinfo=timezone.utc))
    accts = [acct("S"), acct("P", tz="Australia/Perth"), acct("N", tz="Pacific/Auckland")]
    sg = {f"FAKE-{i}": [replace(s, sf_account_id=f"FAKE-{i}")] for i in "SPN"}
    r = run(accts, sg, [pros("S"), pros("P"), pros("N")], cfg=config, now=now)
    assert {x.reasons[0] for x in r.ranked} == {"Funding scoop 4 days ago"}


# ------------------------------------------------------------ L8 cooldown safety net
def test_N2_cooldown_judged_on_own_zone_call_date(config):
    # Thu 16:28 Sydney: Sydney's first usable day is Fri 27, Perth's is still Thu 26 (13:28 AWST).
    now = datetime(2026, 11, 26, 5, 28, tzinfo=timezone.utc)
    accts = [acct("S", name="Syd"), acct("P", name="Perth", tz="Australia/Perth")]
    ps = [pros("S"), pros("P")]
    both = {"FAKE-S": date(2026, 11, 27), "FAKE-P": date(2026, 11, 27)}      # both cooldowns end Fri 27
    r = run(accts, {}, ps, cool=both, cfg=config, now=now)
    assert [x.account.name for x in r.ranked] == ["Syd"]                    # Sydney calls Fri: allowed
    assert r.ranked[0].local_call_time.date() == date(2026, 11, 27)
    assert skipped_reason_of(r, "FAKE-P") == "In cooldown until 2026-11-27 (account-local)"   # Perth would call Thu
    assert not any(x.account.name == "Perth" for x in r.ranked + r.spilled + r.needs_prospect)


def test_N2_no_task_inside_cooldown_for_any_zone(config):
    now = datetime(2026, 11, 26, 5, 28, tzinfo=timezone.utc)
    accts = [acct(f"{z}{i}", name=f"{z}{i}", tz=tz) for z, tz in (("S", "Australia/Sydney"), ("P", "Australia/Perth"),
                                                              ("B", "Australia/Brisbane"), ("D", "Australia/Darwin"))
             for i in range(3)]
    cool = {a.sf_account_id: date(2026, 11, 27) for a in accts}
    r = run(accts, {}, [pros(a.sf_account_id[5:]) for a in accts], cool=cool, cfg=config, now=now)
    for x in r.ranked + r.spilled:
        assert x.local_call_time.date() >= date(2026, 11, 27), x.account.name
    assert r.ranked                                                          # Sydney still calls Friday


def test_needs_prospect_sorted_best_first_and_deterministic(config):
    accts = [acct(f"A{i}", name=f"N{i}", employees=[30, 500, 100][i % 3]) for i in range(9)]
    base = run(accts, {}, [], cfg=config)
    scores = [x.score for x in base.needs_prospect]
    assert scores == sorted(scores, reverse=True)
    rnd = random.Random(3)
    for _ in range(4):
        a2 = accts[:]
        rnd.shuffle(a2)
        again = run(a2, {}, [], cfg=config)
        assert [x.account.sf_account_id for x in again.needs_prospect] == \
               [x.account.sf_account_id for x in base.needs_prospect]


def test_summarise_lists_needs_prospect_codes(config):
    r = run([acct("A1"), acct("A2", name="B")], {}, [pros("A1", phone=None)], cfg=config)
    text = summarise(r)
    assert "needs prospect 2" in text
    assert "needs prospect (NOT callable, no task) - persona_match_no_phone: 1" in text
    assert "no_prospects_on_account: 1" in text


def test_friday_late_holds_overflow_for_monday(config):
    now = datetime(2026, 11, 27, 5, 20, tzinfo=timezone.utc)     # Fri 16:20 Sydney: earliest 16:21, 9 min = 3 slots
    accts = [acct(f"A{i}", name=f"N{i}") for i in range(5)]
    r = run(accts, {}, [pros(a.sf_account_id[5:]) for a in accts], cfg=config, now=now)
    assert len(r.ranked) == 3 and len(r.spilled) == 2
    assert {x.local_call_time.date() for x in r.ranked} == {date(2026, 11, 27)}
    assert {x.local_call_time.date() for x in r.spilled} == {date(2026, 11, 30)}


# ------------------------------------------------------------ N1 first usable day
def test_N1_thu_1628_nobody_is_spilled(config):
    now = datetime(2026, 11, 26, 5, 28, tzinfo=timezone.utc)     # Thu 16:28 Sydney, 2 min left: unusable
    accts = [acct(f"A{i:02d}", name=f"N{i:02d}") for i in range(40)]
    r = run(accts, {}, [pros(a.sf_account_id[5:]) for a in accts], cfg=config, now=now)
    assert len(r.ranked) == 40 and r.spilled == []
    assert all(x.local_call_time.date() == date(2026, 11, 27) for x in r.ranked)
    assert not any("Spilled" in rr for x in r.ranked for rr in x.reasons)


def test_N1_wed_1629_cooldown_thu_not_skipped(config):
    now = datetime(2026, 11, 25, 5, 29, tzinfo=timezone.utc)     # Wed 16:29 Sydney, 1 min left: unusable
    r = run([acct()], {}, [pros()], cool={"FAKE-A1": date(2026, 11, 26)}, cfg=config, now=now)
    assert r.ranked[0].local_call_time.date() == date(2026, 11, 26) and not r.skipped
    r2 = run([acct()], {}, [pros()], cool={"FAKE-A1": date(2026, 11, 27)}, cfg=config, now=now)
    assert not r2.ranked and skipped_reason_of(r2) == "In cooldown until 2026-11-27 (account-local)"


def test_N1_different_zones_may_have_different_first_days(config):
    now = datetime(2026, 11, 26, 5, 28, tzinfo=timezone.utc)     # Thu 16:28 Sydney, 13:28 Perth (afternoon open)
    r = run([acct("S"), acct("P", tz="Australia/Perth")], {}, [pros("S"), pros("P")], cfg=config, now=now)
    d = {x.account.sf_account_id: x.local_call_time.date() for x in r.ranked}
    assert d == {"FAKE-S": date(2026, 11, 27), "FAKE-P": date(2026, 11, 26)} and r.spilled == []


# ------------------------------------------------------------ N2 spilled / cap
def _cfg_cap(raw_config, cap):
    from src.config import parse_config
    raw_config["limits"]["daily_call_cap"] = cap
    return parse_config(raw_config)


def test_N2_held_account_does_not_eat_cap_slot(raw_config):
    cfg = _cfg_cap(raw_config, 4)
    now = datetime(2026, 11, 27, 5, 20, tzinfo=timezone.utc)     # Fri 16:20 Sydney (3 slots), 13:20 Perth (plenty)
    syd = [acct(f"S{i}", name=f"A{i}") for i in range(4)]
    per = [acct(f"P{i}", name=f"B{i}", employees=30, tz="Australia/Perth") for i in range(2)]
    ps = [pros(a.sf_account_id[5:]) for a in syd + per]
    r = run(syd + per, {}, ps, cfg=cfg, now=now)
    assert [x.account.sf_account_id for x in r.ranked] == ["FAKE-S0", "FAKE-S1", "FAKE-S2", "FAKE-P0"]
    assert [x.account.sf_account_id for x in r.spilled] == ["FAKE-S3"]
    assert [x.skipped_reason for x in r.skipped] == ["below daily cap (rank 5, cap 4)"]


def test_N2_spilled_shape(config):
    now = datetime(2026, 11, 27, 5, 20, tzinfo=timezone.utc)
    accts = [acct(f"A{i}", name=f"N{i}") for i in range(5)]
    r = run(accts, {}, [pros(a.sf_account_id[5:]) for a in accts], cfg=config, now=now)
    for x in r.spilled:
        assert x.window in ("morning", "afternoon") and x.local_call_time is not None and x.skipped_reason is None
        assert x not in r.by_window["morning"] + r.by_window["afternoon"]
    assert "spilled 2" in summarise(r)


def test_N2_spill_deterministic_under_shuffle(config):
    now = datetime(2026, 11, 27, 5, 20, tzinfo=timezone.utc)
    accts = [acct(f"A{i}", name=f"N{i}") for i in range(8)]
    ps = [pros(a.sf_account_id[5:]) for a in accts]
    snap = lambda r: ([x.account.sf_account_id for x in r.ranked], [(x.account.sf_account_id, x.local_call_time) for x in r.spilled])
    base = snap(run(accts, {}, ps, cfg=config, now=now))
    rnd = random.Random(5)
    for _ in range(4):
        a2 = accts[:]
        rnd.shuffle(a2)
        assert snap(run(a2, {}, ps, cfg=config, now=now)) == base


# ------------------------------------------------------------ N3 lead time
def test_N3_lead_rounds_up_to_whole_minute_plus_one(config):
    now = datetime(2026, 11, 25, 22, 30, 20, tzinfo=timezone.utc)     # Thu 09:30:20 Sydney
    r = run([acct()], {}, [pros()], cfg=config, now=now).ranked[0]
    assert r.local_call_time == datetime(2026, 11, 26, 9, 32, tzinfo=SYD)


def test_N3_exact_minute_gets_one_minute_lead(config):
    now = datetime(2026, 11, 25, 22, 30, 0, tzinfo=timezone.utc)
    assert run([acct()], {}, [pros()], cfg=config, now=now).ranked[0].local_call_time == \
        datetime(2026, 11, 26, 9, 31, tzinfo=SYD)


def test_N3_before_window_no_lead_effect(config):
    assert run([acct()], {}, [pros()], cfg=config).ranked[0].local_call_time == datetime(2026, 11, 26, 8, 45, tzinfo=SYD)


# ------------------------------------------------------------ N5 stale titles / CPO alias
@pytest.mark.parametrize("title", ["Former Head of Product", "Ex-Head of Product", "Ex CTO", "Past VP Product",
                                   "Interim Former Head of Product", "Acting Former CTO", "former cto",
                                   "  Former Chief Product Officer"])
def test_N5_stale_titles_do_not_match(config, title):
    from src.ranker import _persona_rank
    assert _persona_rank(title, config) is None


@pytest.mark.parametrize("title", ["Interim Head of Product", "Acting CTO", "Executive Director of Product",
                                   "Chief Product & Technology Officer", "Chief Product and Technology Officer"])
def test_N5_current_titles_still_match(config, title):
    from src.ranker import _persona_rank
    assert _persona_rank(title, config) is not None


def test_N5_product_owner_and_engineering_manager_still_unmatched(config):
    from src.ranker import _persona_rank
    assert _persona_rank("Product Owner", config) is None and _persona_rank("Engineering Manager", config) is None


# ------------------------------------------------------------ normalised Salesforce id keys (both directions)
ID15 = "001Fake0000000A"
ID18 = ID15 + "AAB"
assert len(ID15) == 15 and len(ID18) == 18


def sfacct(sf_id, **kw):
    return Account(sf_account_id=sf_id, name="Idco", employees=500, timezone="Australia/Sydney", **kw)


def sfpros(sf_id, name="Pat"):
    return Prospect("P-" + name, None, sf_id, None, name, "Head of Product", phone_e164="+61400000001")


def sfsig(sf_id):
    return Signal(sf_id, SignalKind.FUNDING, 1.0, "zoominfo_scoop:id", NOW, "e")


@pytest.mark.parametrize("acct_id,key_id", [(ID18, ID15), (ID15, ID18), (ID18, ID18), (ID15, ID15)])
def test_signal_keys_normalised(config, acct_id, key_id):
    r = run([sfacct(acct_id)], {key_id: [sfsig(key_id)]}, [sfpros(acct_id)], cfg=config).ranked[0]
    assert r.score_breakdown["trigger_strength"] == 40


@pytest.mark.parametrize("acct_id,key_id", [(ID18, ID15), (ID15, ID18)])
def test_cooldown_keys_normalised(config, acct_id, key_id):
    r = run([sfacct(acct_id)], {}, [sfpros(acct_id)], cool={key_id: date(2026, 12, 20)}, cfg=config)
    assert not r.ranked and r.skipped[0].skipped_reason == "In cooldown until 2026-12-20 (account-local)"


@pytest.mark.parametrize("acct_id,key_id", [(ID18, ID15), (ID15, ID18)])
def test_last_sheet_ids_normalised(config, acct_id, key_id):
    r = run([sfacct(acct_id)], {}, [sfpros(acct_id)], last={key_id}, cfg=config)
    assert not r.ranked and r.skipped[0].skipped_reason == "on last sheet"


@pytest.mark.parametrize("acct_id,pros_id", [(ID18, ID15), (ID15, ID18)])
def test_prospect_join_normalised(config, acct_id, pros_id):
    r = run([sfacct(acct_id)], {}, [sfpros(pros_id)], cfg=config)
    assert len(r.ranked) == 1 and r.ranked[0].score_breakdown["named_prospect_with_phone"] == 15


def test_blocked_prospect_join_normalised(config):
    p = replace(sfpros(ID15), do_not_call=True)
    r = run([sfacct(ID18)], {}, [p], cfg=config)
    assert r.skipped[0].skipped_reason.startswith("all prospects blocked")


def test_account_key_same_for_both_spellings():
    assert account_key(sfacct(ID15)) == account_key(sfacct(ID18)) == ID15


def test_case_is_not_folded_in_ids(config):
    other = ID15.replace("Fake", "fake")
    r = run([sfacct(ID15)], {other: [sfsig(other)]}, [sfpros(ID15)], cfg=config).ranked[0]
    assert r.score_breakdown["trigger_strength"] == 0       # different case = different id


def test_two_spellings_in_signal_map_merge(config):
    s2 = replace(sfsig(ID18), source="zoominfo_scoop:other", strength=0.5)
    r = run([sfacct(ID18)], {ID15: [sfsig(ID15)], ID18: [s2]}, [sfpros(ID18)], cfg=config).ranked[0]
    assert r.score_breakdown["trigger_strength"] == 40        # 1.0 + 0.1 capped at 1


def test_fake_ids_pass_through(config):
    r = run([acct()], {"FAKE-A1": [sig()]}, [pros()], last={"FAKE-OTHER"}, cfg=config)
    assert len(r.ranked) == 1 and r.ranked[0].score_breakdown["trigger_strength"] == 40


def test_cooldown_two_spellings_later_date_wins(config):
    r = run([sfacct(ID18)], {}, [sfpros(ID18)], cool={ID15: date(2026, 12, 20), ID18: date(2026, 11, 1)},
            cfg=config)
    assert not r.ranked and r.skipped[0].skipped_reason == "In cooldown until 2026-12-20 (account-local)"


# ------------------------------------------------------------ L7 summarise names every bucket
def _mixed(config, n_skip=0, n_needs=0, n_ok=3, now=NOW):
    ok = [acct(f"O{i:02d}", name=f"Okco {i:02d}") for i in range(n_ok)]
    skip = [acct(f"S{i:02d}", name=f"Skipco {i:02d}", tz=None) for i in range(n_skip)]
    needs = [acct(f"N{i:02d}", name=f"Needco {i:02d}") for i in range(n_needs)]
    return run(ok + skip + needs, {}, [pros(a.sf_account_id[5:]) for a in ok], cfg=config, now=now)


def test_summarise_names_skipped_with_reason(config):
    text = summarise(_mixed(config, n_skip=2))
    assert "SKIPPED accounts (2):" in text
    assert "- Skipco 00: no timezone" in text and "- Skipco 01: no timezone" in text


def test_summarise_names_needs_prospect_with_code(config):
    text = summarise(_mixed(config, n_needs=2))
    assert "NEEDS PROSPECT (no task) (2):" in text
    assert "- Needco 00: no_prospects_on_account, score" in text


def test_summarise_names_spilled_with_intended_time(config):
    now = datetime(2026, 11, 27, 5, 20, tzinfo=timezone.utc)
    r = _mixed(config, n_ok=5, now=now)
    text = summarise(r)
    assert "SPILLED (held, no task tonight) (2):" in text and "intended Mon 30 Nov 08:45" in text


def test_summarise_list_limit_and_total(config):
    text = summarise(_mixed(config, n_skip=30), list_limit=25)
    assert "SKIPPED accounts (30):" in text and "... and 5 more" in text
    assert text.count("no timezone") >= 25 and "Skipco 29" not in text


def test_summarise_top_n_configurable_default_15(config):
    accts = [acct(f"A{i:02d}", name=f"N{i:02d}") for i in range(20)]
    r = run(accts, {}, [pros(a.sf_account_id[5:]) for a in accts], cfg=config)
    default = summarise(r)
    assert " 15. " in default and " 16. " not in default and "... and 5 more ranked" in default
    assert " 3. " in summarise(r, top_n=3) and " 4. " not in summarise(r, top_n=3)


def test_summarise_no_bucket_sections_when_empty(config):
    text = summarise(_mixed(config))
    assert "SKIPPED" not in text and "NEEDS PROSPECT" not in text and "SPILLED" not in text


# ------------------------------------------------------------ N5 per-kind recency horizon
def test_N5_exec_move_60_days_old_still_earns_recency(config):
    s = sig(kind=SignalKind.EXEC_MOVE, days=60)
    _, b = score_of(config, acct(), [s])
    assert b["recency"] == pytest.approx(15 * (1 - 60 / 90), abs=0.01)     # limit 90, not config 45


def test_N5_exec_move_beyond_its_own_limit_is_zero(config):
    _, b = score_of(config, acct(), [sig(kind=SignalKind.EXEC_MOVE, days=91)])
    assert b["recency"] == 0


def test_N5_funding_uses_its_45_day_limit(config):
    _, b = score_of(config, acct(), [sig(days=30)])
    assert b["recency"] == pytest.approx(15 * (1 - 30 / 45), abs=0.01)
    _, b = score_of(config, acct(), [sig(days=46)])
    assert b["recency"] == 0


def test_N5_partnership_uses_its_30_day_limit(config):
    _, b = score_of(config, acct(), [sig(kind=SignalKind.PARTNERSHIP, days=15)])
    assert b["recency"] == pytest.approx(7.5, abs=0.01)
    _, b = score_of(config, acct(), [sig(kind=SignalKind.PARTNERSHIP, days=31)])
    assert b["recency"] == 0


def test_N5_limit_comes_from_the_best_signal_kind(config):
    best = sig(kind=SignalKind.EXEC_MOVE, strength=0.9, days=60)
    other = sig(kind=SignalKind.PARTNERSHIP, strength=0.2, days=1)
    _, b = score_of(config, acct(), [best, other])
    assert b["recency"] == pytest.approx(15 * (1 - 60 / 90), abs=0.01)


def test_N5_config_max_age_days_is_only_the_fallback(config, monkeypatch):
    from src import signals as sm
    monkeypatch.delitem(sm.KIND_MAX_AGE_DAYS, SignalKind.FUNDING)
    _, b = score_of(config, acct(), [sig(days=9)])
    assert b["recency"] == pytest.approx(15 * (1 - 9 / config.signals.max_age_days), abs=0.01)


def test_N5_breakdown_still_sums_and_is_deterministic(config):
    sigs = [sig(kind=SignalKind.EXEC_MOVE, days=60), sig(strength=0.5, days=3)]
    s1, b1 = score_of(config, acct(is_sdr_target=True), sigs)
    s2, b2 = score_of(config, acct(is_sdr_target=True), list(reversed(sigs)))
    assert (s1, b1) == (s2, b2) and sum(b1.values()) == pytest.approx(s1)


# ------------------------------------------------------------ persona hygiene (first real dry run)
from src.ranker import _persona_rank, _title_class, _is_junk_shape, _is_non_buyer, _role_phrase_count  # noqa: E402

EA_TITLE = ("Executive Assistant To Chief Financial Officer, Chief Technology Officer And "
            "Chief Risk Officer")


def test_ea_title_is_long_and_junk_shaped():
    assert len(EA_TITLE) > 80 and _is_junk_shape(EA_TITLE) and _is_non_buyer(EA_TITLE)


def test_ea_title_never_matches_a_persona(config):
    assert _persona_rank(EA_TITLE, config) is None
    assert _title_class(EA_TITLE, config)[0] == "junk"


def test_ea_title_only_candidate_goes_to_needs_prospect_junk_title(config):
    r = run([acct()], {"FAKE-A1": [sig()]}, [pros(title=EA_TITLE)], cfg=config)
    assert not r.ranked and r.needs_prospect_codes["FAKE-A1"] == "junk_title"
    assert r.needs_prospect[0].local_call_time is None and r.prospects == {}
    assert r.needs_prospect[0].reasons[0] == "Needs a prospect: junk_title"


def test_ea_title_never_chosen_over_clean_title(config):
    ps = [pros(title=EA_TITLE, name="Aaa"), pros(title="Product Manager", name="Zed")]
    assert pick_prospect(ps, config).name == "Zed"


def test_rank2_scenario_junk_does_not_displace_callable(config):
    junk = acct("A1", name="JunkCo")
    ok = acct("A2", name="OkCo", employees=30)
    r = run([junk, ok], {"FAKE-A1": [sig("A1")]}, [pros("A1", title=EA_TITLE), pros("A2")], cfg=config)
    assert [x.account.name for x in r.ranked] == ["OkCo"]
    assert [x.account.name for x in r.needs_prospect] == ["JunkCo"]


@pytest.mark.parametrize("title", [
    "Executive Assistant to the CEO", "Personal Assistant to the Chief Product Officer", "PA to CTO",
    "EA to the Chief Technology Officer", "Assistant Product Manager", "Secretary to the CTO",
    "Product Coordinator, Head of Product Office", "IT Administrator, CTO Office", "Product Intern, Head of Product",
    "Graduate Product Manager", "Trainee Product Manager", "Chief of Staff to the CTO",
    "Chief of Staff to the Chief Technology Officer", "Former Head of Product", "Ex-CTO", "Past VP Product",
    "Interim Former Head of Product", "Acting Former CTO"])
def test_non_buyer_titles_rejected_before_persona_matching(config, title):
    assert _persona_rank(title, config) is None, title
    assert pick_prospect([pros(title=title)], config) is None


@pytest.mark.parametrize("title", ["Internal Communications Manager", "Head of Product Internals",
                                   "Head of Product", "Head of Product, reporting to the CTO"])
def test_non_buyer_words_need_whole_word_match(config, title):
    if title.startswith("Internal"):
        assert _persona_rank(title, config) is None          # no persona anyway
    else:
        assert _persona_rank(title, config) is not None


def test_reporting_to_cto_is_not_a_to_the_title(config):
    assert _persona_rank("Head of Product, reporting to the CTO", config) == (0, 0, 0)
    assert not _is_non_buyer("Head of Product, reporting to the CTO")


def test_reporting_clause_does_not_create_a_match(config):
    assert _persona_rank("Program Director Governance, reporting to the Chief Technology Officer", config) is None


# ---- junk shape
def test_long_title_over_80_chars_is_junk(config):
    t = "Head of Product " + "Digital Experience " * 5
    assert len(t) > 80
    assert _title_class(t, config)[0] == "junk"
    assert pick_prospect([pros(title=t)], config) is None


def test_exactly_80_chars_not_junk_by_length():
    t = "Head of Product" + " x" * 32
    assert len(t) == 79 or len(t) == 80
    assert not _is_junk_shape("a" * 80)
    assert _is_junk_shape("a" * 81)


def test_three_roles_listed_is_junk(config):
    t = "Head of Product, Chief Technology Officer and Chief Risk Officer"
    assert _role_phrase_count(t) == 3 and _title_class(t, config)[0] == "junk"


def test_two_roles_is_fine(config):
    assert _role_phrase_count("Head of Product and Chief Digital Officer") == 2
    assert _title_class("Head of Product and Chief Digital Officer", config)[0] == "ok"


@pytest.mark.parametrize("title,expected", [("VP, Product & Design", 1), ("Head of Product, Digital", 1),
                                            ("Vice President and Head of Product Marketing", 2),
                                            ("Product Manager - Pathology", 1), ("CTO & Co-founder", 2)])
def test_role_phrase_counts(title, expected):
    assert _role_phrase_count(title) == expected


def test_junk_only_candidate_code_beats_no_persona_match(config):
    t = "Head of Product, Chief Technology Officer, Chief Risk Officer, Chief Financial Officer"
    r = run([acct()], {}, [pros(title=t), pros(title="Accountant", name="Acc")], cfg=config)
    assert r.needs_prospect_codes["FAKE-A1"] == "junk_title"


def test_clean_title_without_phone_beats_junk_in_code(config):
    t = "Head of Product, Chief Technology Officer, Chief Risk Officer, Chief Financial Officer"
    r = run([acct()], {}, [pros(title=t), pros(title="CTO", phone=None, name="Nop")], cfg=config)
    assert r.needs_prospect_codes["FAKE-A1"] == "persona_match_no_phone"


def test_junk_title_unrelated_to_personas_is_no_persona_match(config):
    t = "Senior Accountant " + "x" * 90
    r = run([acct()], {}, [pros(title=t)], cfg=config)
    assert r.needs_prospect_codes["FAKE-A1"] == "no_persona_match"


def test_junk_code_listed_and_summarised(config):
    r = run([acct()], {}, [pros(title=EA_TITLE)], cfg=config)
    text = summarise(r)
    assert "junk_title" in text and "NEEDS PROSPECT" in text


# ---- head-of-title preference
def test_head_match_beats_later_segment_match_same_person(config):
    assert _persona_rank("Strategy Consultant, Head of Product Programs", config) == (0, 0, 1)   # later
    assert _persona_rank("Head of Product, Digital", config) == (0, 0, 0)                          # head


def test_head_title_beats_buried_primary_across_prospects(config):
    # primary match buried after a comma loses to a secondary match that IS the role.
    ps = [pros(title="Strategy Consultant, Head of Product Programs", name="Aaa"),
          pros(title="Product Manager", name="Zed")]
    assert pick_prospect(ps, config).name == "Zed"


def test_head_of_product_digital_beats_program_director_reporting_to_cto(config):
    ps = [pros(title="Program Director, Digital Transformation Office reporting to the CTO", name="Aaa"),
          pros(title="Head of Product, Digital", name="Zed")]
    assert pick_prospect(ps, config).name == "Zed"


def test_head_tie_still_primary_then_config_order_then_name(config):
    ps = [pros(title="Director of Product", name="B"), pros(title="Head of Product", name="C"),
          pros(title="Head of Product", name="A")]
    assert pick_prospect(ps, config).name == "A"
    for _ in range(4):
        random.shuffle(ps)
        assert pick_prospect(ps, config).name == "A"


def test_vp_comma_product_counts_as_head(config):
    assert _persona_rank("VP, Product & Design", config) == (0, 1, 0)


# ---- the real-shaped titles from the dry run
@pytest.mark.parametrize("title,expected", [
    ("Head Of Product - Digital Experience", (0, 0, 0)),
    ("Vice President and Head of Product Marketing", (0, 0, 0)),
    ("Program Director Digital Transformation", (0, 4, 0)),      # alias 'director digital' = Head of Digital
    ("Head of IT, Data Centre Development", (1, 3, 0)),
    ("VP, Product & Design", (0, 1, 0)),
    ("Interim Chief Digital Officer", (0, 5, 0)),
    ("Chief of Staff to the CTO", None),
    ("Product Manager - Pathology", (1, 0, 0)),                  # function ambiguity handled elsewhere
    (EA_TITLE, None),
])
def test_real_shaped_titles(config, title, expected):
    assert _persona_rank(title, config) == expected, title


def test_hygiene_is_deterministic_under_shuffle(config):
    titles = [EA_TITLE, "Head Of Product - Digital Experience", "Chief of Staff to the CTO", "Product Manager",
              "Vice President and Head of Product Marketing", "Strategy Consultant, Head of Product Programs"]
    ps = [pros(title=t, name=f"P{i}") for i, t in enumerate(titles)]
    base = pick_prospects(ps, config)
    rnd = random.Random(8)
    for _ in range(5):
        p2 = ps[:]
        rnd.shuffle(p2)
        assert pick_prospects(p2, config) == base
