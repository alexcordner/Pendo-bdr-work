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


def sig(i="A1", kind=SignalKind.FUNDING, strength=1.0, days=0, source=None):
    return Signal(f"FAKE-{i}", kind, strength, source or f"zoominfo_scoop:{i}-{kind.value}-{days}",
                  NOW - timedelta(days=days), "evidence")


def pros(i="A1", title="Head of Product", phone="+61400000001", name="Pat", **kw):
    return Prospect(nooks_id=f"P-{i}-{name}", sf_contact_id=None, sf_account_id=f"FAKE-{i}",
                    nooks_account_id=None, name=name, title=title, phone_e164=phone, **kw)


def run(accounts, sigs=None, prospects=(), cool=None, last=None, cfg=None, now=NOW):
    return rank_accounts(accounts, sigs or {}, list(prospects), cool or {}, last or set(), cfg, now)


def score_of(cfg, a, sigs=(), prospect=True, cool=False, last=False, now=NOW):
    return score_account(a, list(sigs), prospect, cool, last, cfg, now)


# ------------------------------------------------------------ score components in isolation
def test_trigger_strength_scales_weight(config):
    _, b = score_of(config, acct(employees=None), [sig(strength=0.5)], prospect=False)
    assert b["trigger_strength"] == pytest.approx(20.0)


def test_trigger_none_kind_is_zero(config):
    s = Signal("FAKE-A1", SignalKind.NONE, 0.0, "x", None, "")
    r = run([acct()], {"FAKE-A1": [s]}, [pros()], cfg=config)
    assert r.ranked[0].score_breakdown["trigger_strength"] == 0
    assert r.ranked[0].reasons[0] == "No trigger: ranked on fit only"


def test_combined_signals_bonus_capped(config):
    sigs = [sig(strength=0.5, days=1), sig(kind=SignalKind.NEW_HIRE, strength=0.5, days=2)]
    _, b = score_of(config, acct(), sigs)
    assert b["trigger_strength"] == pytest.approx(40 * 0.6)
    many = [sig(kind=k, strength=0.95) for k in (SignalKind.FUNDING, SignalKind.MA, SignalKind.NEW_HIRE)]
    _, b = score_of(config, acct(), many)
    assert b["trigger_strength"] == pytest.approx(40.0)


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
    s = replace(sig(), source_date=NOW + timedelta(days=3))
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


def test_cooldown_penalty_applied_and_clamped_at_zero(config):
    s, b = score_of(config, acct(employees=None), [], prospect=False, cool=True)
    assert b["cooldown_penalty"] == -30
    assert s == 0 and b["clamp_adjustment"] > 0


def test_cooldown_and_last_sheet_penalised_once(config):
    _, b = score_of(config, acct(), [sig()], cool=True, last=True)
    assert b["cooldown_penalty"] == -30


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
    s, b = score_of(config, acct(**kw), [sig(strength=0.7, days=4)], cool=False)
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
def test_cooldown_active_in_rank(config):
    p = [pros()]
    plain = run([acct()], {}, p, cfg=config).ranked[0].score
    cooled = run([acct()], {}, p, cool={"FAKE-A1": date(2026, 11, 30)}, cfg=config).ranked[0]
    assert cooled.score == plain - 30 and "In cooldown" in cooled.reasons


def test_cooldown_expired_no_penalty(config):
    p = [pros()]
    plain = run([acct()], {}, p, cfg=config).ranked[0].score
    r = run([acct()], {}, p, cool={"FAKE-A1": date(2026, 11, 25)}, cfg=config).ranked[0]
    assert r.score == plain      # until == today means callable again


def test_last_sheet_penalty(config):
    p = [pros()]
    plain = run([acct()], {}, p, cfg=config).ranked[0].score
    r = run([acct()], {}, p, last={"FAKE-A1"}, cfg=config).ranked[0]
    assert r.score == plain - 30 and "On the last call sheet" in r.reasons


def test_last_sheet_matches_zoominfo_id(config):
    a = acct(zoominfo_id="ZI-1")
    r = run([a], {}, [pros()], last={"ZI-1"}, cfg=config).ranked[0]
    assert r.score_breakdown["cooldown_penalty"] == -30


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


def test_no_prospects_not_skipped_but_no_points(config):
    r = run([acct()], {}, [], cfg=config)
    assert len(r.ranked) == 1
    assert r.ranked[0].score_breakdown["named_prospect_with_phone"] == 0
    assert "No callable persona with a phone" in r.ranked[0].reasons


def test_prospect_without_phone_account_kept_without_points(config):
    r = run([acct()], {}, [pros(phone=None)], cfg=config)
    assert r.ranked and r.ranked[0].score_breakdown["named_prospect_with_phone"] == 0


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
    assert len(r.by_window["morning"]) == 3


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
    accts = [acct("A1", employees=30), acct("A2", employees=500), acct("A3", employees=100)]
    r = run(accts, {}, [pros(x) for x in ("A1", "A2", "A3")], cfg=config)
    scores = [x.score for x in r.by_window["morning"]]
    assert scores == sorted(scores, reverse=True) and len(scores) == 3
    assert r.by_window["afternoon"] == []


def test_spread_within_window_distinct_times(config):
    accts = [acct(f"A{i:02d}", name=f"N{i:02d}") for i in range(40)]
    r = run(accts, {}, [pros(a.sf_account_id[5:]) for a in accts], cfg=config)
    times = [x.local_call_time for x in r.ranked]
    assert len(set(times)) == 40
    start, end = datetime(2026, 11, 26, 8, 45, tzinfo=SYD), datetime(2026, 11, 26, 11, 30, tzinfo=SYD)
    assert all(start <= t < end for t in times)
    assert times == sorted(times)            # best score gets the earliest slot


def test_guessed_timezone_called_later(config):
    r = run([acct("A1"), acct("A2", timezone_guessed=True)], {}, [pros("A1"), pros("A2")], cfg=config)
    t = {x.account.sf_account_id: x.local_call_time for x in r.ranked}
    assert t["FAKE-A2"] > t["FAKE-A1"]
    assert t["FAKE-A2"] >= datetime(2026, 11, 26, 10, 0, tzinfo=SYD)
    assert "Timezone guessed: called later in window" in [x for x in r.ranked
                                                           if x.account.timezone_guessed][0].reasons


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
    assert "Ranked 1 accounts; skipped 1." in text
    assert "skipped - no timezone: 1" in text
    assert "no real trigger (ranked on fit only): 1" in text
    assert "Acct A1" in text and "Thu 08:45" in text


def test_summarise_empty(config):
    assert summarise(run([], cfg=config)).startswith("Ranked 0 accounts; skipped 0.")


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
    assert [x.account.sf_account_id for x in r.ranked] == ["FAKE-A1", "FAKE-A2", "FAKE-A3"]
    assert r.ranked[0].reasons[0] == "Funding scoop 6 days ago"
    assert [s.account.sf_account_id for s in r.skipped] == ["FAKE-A4"]
    assert r.ranked[2].score_breakdown["named_prospect_with_phone"] == 0     # CTO has no phone
