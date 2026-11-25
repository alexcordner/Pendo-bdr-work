"""Tests for src/signals.py. Synthetic fixtures only; fixed 'today' so results never depend on the clock."""
import json
import math
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from tests.conftest import load_fixture
from src.loaders import scoop_from_zoominfo
from src.models import Account, Scoop, Signal, SignalKind
from src import signals as S

TODAY = datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc)


def _scoops(name):
    return [scoop_from_zoominfo(r) for r in load_fixture(name)["data"]]


@pytest.fixture(scope="module")
def sig_scoops():
    return {s.id: s for s in _scoops("signals_scoops.json")}


@pytest.fixture(scope="module")
def accounts():
    return [Account(sf_account_id=a["sf_account_id"], name=a["name"], zoominfo_id=a["zoominfo_id"])
            for a in load_fixture("signals_accounts.json")["accounts"]]


@pytest.fixture(scope="module")
def verdicts(sig_scoops, config):
    return {v.scoop.id: v for v in S.filter_scoops(list(sig_scoops.values()), config, TODAY)}


def make_scoop(desc="Example has secured funding.", types=("Funding",), topics=("Funding",), days=1,
               cid="FAKE-ZI-2001", name="Gamma Apps Pty Ltd", sid="X-1"):
    return Scoop(sid, cid, name, desc, TODAY - timedelta(days=days), tuple(types), tuple(topics))


def judge(config, **kw):
    return S.filter_scoops([make_scoop(**kw)], config, TODAY)[0]


# ------------------------------------------------------------ existing shared fixture (expected keep/drop)
def test_existing_fixture_matches_expected_judgements(config):
    scoops = _scoops("zoominfo_scoops.json")
    expected = {e["scoop"]: e["keep"] for e in load_fixture("zoominfo_scoop_sample_evidence.json")["expected"]}
    got = {v.scoop.id: v.keep for v in S.filter_scoops(scoops, config, TODAY)}
    assert got == expected


def test_existing_fixture_kinds(config):
    kinds = {v.scoop.id: v.kind for v in S.filter_scoops(_scoops("zoominfo_scoops.json"), config, TODAY) if v.keep}
    assert kinds["FAKE-SCOOP-1"] is SignalKind.FUNDING
    assert kinds["FAKE-SCOOP-2"] is SignalKind.EXEC_MOVE
    assert kinds["FAKE-SCOOP-4"] is SignalKind.PRODUCT_LAUNCH
    assert kinds["FAKE-SCOOP-5"] is SignalKind.MA
    assert kinds["FAKE-SCOOP-6"] is SignalKind.PARTNERSHIP
    assert kinds["FAKE-SCOOP-7"] is SignalKind.PROJECT_PAIN
    assert kinds["FAKE-SCOOP-12"] is SignalKind.HIRING_PLAN
    assert kinds["FAKE-SCOOP-14"] is SignalKind.EXEC_MOVE


# ------------------------------------------------------------ verdict contract
def test_one_verdict_per_scoop_in_input_order(sig_scoops, config):
    vs = S.filter_scoops(list(sig_scoops.values()), config, TODAY)
    assert [v.scoop.id for v in vs] == list(sig_scoops)


def test_every_verdict_has_a_reason(verdicts):
    assert all(v.reason.strip() for v in verdicts.values())


def test_dropped_verdicts_have_kind_none_kept_do_not(verdicts):
    for v in verdicts.values():
        assert (v.kind is not SignalKind.NONE) == v.keep


def test_empty_input(config):
    assert S.filter_scoops([], config, TODAY) == []


# ------------------------------------------------------------ drops and keeps by category
@pytest.mark.parametrize("sid", ["SIG-01", "SIG-02", "SIG-21"])
def test_generic_hiring_dropped(verdicts, sid):
    v = verdicts[sid]
    assert not v.keep and "generic hiring" in v.reason


def test_medical_health_generic_reason_names_keyword(verdicts):
    assert "medical & health" in verdicts["SIG-01"].reason


def test_investor_note_dropped(verdicts):
    v = verdicts["SIG-03"]
    assert not v.keep and "investor" in v.reason


def test_funded_company_with_investor_named_kept(verdicts):
    assert verdicts["SIG-04"].keep and verdicts["SIG-04"].kind is SignalKind.FUNDING


@pytest.mark.parametrize("desc", [
    "Gamma Apps has invested in a startup.",
    "Gamma Apps is investing in a new fund.",
])
def test_investor_phrasings_dropped(config, desc):
    assert not judge(config, desc=desc).keep


def test_funding_received_without_invest_verb_kept(config):
    assert judge(config, desc="Gamma Apps closed a Series B round.").keep


def test_investee_kept_when_company_is_object_of_invest(config):
    assert judge(config, desc="Acme Ventures invested $3M in Gamma Apps.").keep
    assert judge(config, desc="Acme Ventures made an investment in Gamma Apps.").keep


def test_ceo_only_exec_move_dropped(verdicts):
    v = verdicts["SIG-05"]
    assert not v.keep and "role" in v.reason


def test_ceo_change_at_company_with_digital_in_name_dropped(verdicts):
    assert not verdicts["SIG-06"].keep


def test_product_leader_exec_move_kept(verdicts):
    assert verdicts["SIG-07"].keep and verdicts["SIG-07"].kind is SignalKind.EXEC_MOVE


def test_ceo_naming_relevant_role_kept(config):
    assert judge(config, desc="New CEO appointed alongside a new Head of Product.",
                 types=("Executive Move",), topics=()).keep


def test_cx_new_hire_kept_as_new_hire(verdicts):
    assert verdicts["SIG-08"].keep and verdicts["SIG-08"].kind is SignalKind.NEW_HIRE


def test_cto_new_hire_kept(verdicts):
    assert verdicts["SIG-09"].keep


def test_it_director_kept(verdicts):
    assert verdicts["SIG-10"].keep


def test_pronoun_it_is_not_an_it_role(verdicts):
    assert not verdicts["SIG-11"].keep


def test_product_marketing_not_a_buyer(verdicts):
    assert not verdicts["SIG-12"].keep


@pytest.mark.parametrize("desc", [
    "<Person> moves to Chief Digital Officer.",
    "<Person> will lead Customer Success.",
    "<Person> named VP, Customer Success.",
    "<Person> promoted to Head of Engineering.",
    "<Person> joins as Product Director.",
    "<Person> appointed Head of Digital Workplace.",
    "<Person> becomes Chief Product Officer.",
    "<Person> joins as Head of Support.",
    "<Person> named CIO.",
    "<Person> joins as Digital Transformation Lead.",
    "<Person> joins as Product Owner.",
])
def test_relevant_roles_detected(desc):
    assert S.relevant_role(desc)


@pytest.mark.parametrize("desc", [
    "<Person> was appointed Chief Executive Officer.",
    "<Person> joins as CFO.",
    "<Person> will join as Head of Finance.",
    "<Person> appointed General Counsel.",
    "<Person> joins as Head of HR and Legal.",
    "It was announced that it will hire.",
    "",
])
def test_irrelevant_roles_not_detected(desc):
    assert S.relevant_role(desc) is None


def test_role_matching_is_word_boundary_aware():
    assert S.relevant_role("<Person> joins as Director of Productivity.") is None
    assert S.relevant_role("<Person> joins as Manager of Supportive Housing.") is None


def test_role_matching_case_insensitive():
    assert S.relevant_role("<person> joins as HEAD OF PRODUCT.")


def test_project_with_digital_workplace_kept(verdicts):
    assert verdicts["SIG-13"].keep and verdicts["SIG-13"].kind is SignalKind.PROJECT_PAIN


def test_pain_point_with_product_development_kept(verdicts):
    assert verdicts["SIG-14"].keep


@pytest.mark.parametrize("sid", ["SIG-15", "SIG-16"])
def test_project_or_pain_without_buyer_topic_dropped(verdicts, sid):
    assert not verdicts[sid].keep and "buyer topic" in verdicts[sid].reason


def test_internal_source_note_not_rescued_by_keywords(config):
    v = judge(config, desc="According to an internal source, the company is rebuilding its product app.",
              types=("Pain Point",), topics=("Pain Point",))
    assert not v.keep


def test_unknown_type_dropped(verdicts):
    assert not verdicts["SIG-20"].keep and "type" in verdicts["SIG-20"].reason


def test_scoop_with_no_types_dropped(config):
    v = judge(config, types=(), topics=())
    assert not v.keep and "none" in v.reason


def test_hiring_with_topic_kept(verdicts):
    assert verdicts["SIG-22"].keep and verdicts["SIG-22"].kind is SignalKind.HIRING_PLAN


def test_open_position_product_keyword_kept(verdicts):
    assert verdicts["SIG-28"].keep


def test_open_position_finance_dropped(verdicts):
    assert not verdicts["SIG-29"].keep


def test_drop_keyword_loses_to_buyer_keyword(config):
    v = judge(config, desc="Hiring a nurse informatics product manager.", types=("Hiring Plans",), topics=())
    assert v.keep


def test_partnership_and_ma_kept(verdicts):
    assert verdicts["SIG-26"].kind is SignalKind.PARTNERSHIP and verdicts["SIG-27"].kind is SignalKind.MA


def test_precedence_exec_move_over_new_hire(config):
    v = judge(config, desc="<P> joins as Head of Product.", types=("New Hire", "Executive Move"), topics=())
    assert v.kind is SignalKind.EXEC_MOVE


def test_multi_type_one_fails_other_passes(config):
    v = judge(config, desc="Gamma Apps raised $5 million.", types=("Funding", "Project"), topics=("Funding",))
    assert v.keep and v.kind is SignalKind.FUNDING


def test_type_matching_is_case_insensitive(config):
    assert judge(config, types=("FUNDING",)).keep


# ------------------------------------------------------------ age boundary
def test_scoop_at_max_age_is_kept(verdicts, config):
    assert config.signals.max_age_days == 45
    assert verdicts["SIG-17"].keep          # exactly 45 days old


def test_scoop_one_day_past_max_age_dropped(verdicts):
    v = verdicts["SIG-18"]
    assert not v.keep and "expired" in v.reason and "46" in v.reason


def test_config_max_age_is_only_the_default_for_kinds_missing_from_the_table(config, monkeypatch):
    cfg = replace(config, signals=replace(config.signals, max_age_days=10))
    # every kind is listed, so the config value caps nothing today
    assert S.filter_scoops([make_scoop(days=40)], cfg, TODAY)[0].keep
    monkeypatch.delitem(S.KIND_MAX_AGE_DAYS, SignalKind.FUNDING)
    assert S.max_age_for(SignalKind.FUNDING, cfg) == 10
    assert S.filter_scoops([make_scoop(days=10)], cfg, TODAY)[0].keep
    assert not S.filter_scoops([make_scoop(days=11)], cfg, TODAY)[0].keep


def test_future_dated_scoop_kept_as_age_zero(verdicts):
    v = verdicts["SIG-23"]
    assert v.keep and "future-dated" in v.reason
    assert S.age_days(v.scoop.published_date, TODAY) == 0


def test_today_can_be_a_date_or_datetime(sig_scoops, config):
    a = S.filter_scoops(list(sig_scoops.values()), config, date(2026, 11, 25))
    b = S.filter_scoops(list(sig_scoops.values()), config, TODAY)
    assert [(v.keep, v.reason) for v in a] == [(v.keep, v.reason) for v in b]


def test_age_uses_utc_date():
    pub = datetime(2026, 11, 24, 23, 0, tzinfo=timezone.utc)
    assert S.age_days(pub, datetime(2026, 11, 25, 0, 30, tzinfo=timezone.utc)) == 1


# ------------------------------------------------------------ strength: decay and bounds
def test_decay_monotonic_non_increasing():
    vals = [S.recency_factor(d) for d in range(0, 200)]
    assert all(a >= b for a, b in zip(vals, vals[1:])) and vals[0] == 1.0


def test_decay_half_life_literal_numbers():
    assert S.RECENCY_HALF_LIFE_DAYS == 21.0
    assert S.recency_factor(21) == 0.5
    assert S.recency_factor(42) == 0.25
    assert round(S.recency_factor(10), 4) == 0.7189        # 0.5 ** (10/21)
    assert S.recency_factor(45, 45) == 0.5


def test_decay_negative_days_clamped():
    assert S.recency_factor(-5) == 1.0


@pytest.mark.parametrize("kind", [k for k in SignalKind])
def test_strength_bounds_every_kind_and_age(kind):
    for d in (0, 1, 10, 45, 400):
        assert 0.0 <= S.signal_strength(kind, d) <= 1.0


def test_strength_none_kind_is_zero():
    assert S.signal_strength(SignalKind.NONE, 0) == 0.0


def test_strength_decreases_with_age_per_kind():
    for kind in S.KIND_BASE_WEIGHT:
        if kind is SignalKind.NONE:
            continue
        s = [S.signal_strength(kind, d) for d in (0, 7, 14, 30, 45)]
        assert s == sorted(s, reverse=True) and s[0] > s[-1]


def test_base_weights_in_range_and_cover_every_kind():
    assert set(S.KIND_BASE_WEIGHT) == set(SignalKind)
    assert all(0.0 <= w <= 1.0 for w in S.KIND_BASE_WEIGHT.values())


def test_kind_ordering_same_age():
    w = S.KIND_BASE_WEIGHT
    assert w[SignalKind.EXEC_MOVE] > w[SignalKind.FUNDING] > w[SignalKind.PARTNERSHIP]


def test_strength_table_literal_numbers():
    assert S.signal_strength(SignalKind.EXEC_MOVE, 0) == 0.9
    assert S.signal_strength(SignalKind.EXEC_MOVE, 21) == 0.6513       # 0.9 * 0.5 ** (21/45)
    assert S.signal_strength(SignalKind.EXEC_MOVE, 45) == 0.45         # one 45-day half-life
    assert S.signal_strength(SignalKind.EXEC_MOVE, 90) == 0.225
    assert S.signal_strength(SignalKind.NEW_HIRE, 45) == 0.325
    assert S.signal_strength(SignalKind.PRODUCT_LAUNCH, 10) == 0.5751  # 0.8 * 0.5 ** (10/21)
    assert S.signal_strength(SignalKind.FUNDING, 45) == 0.1585         # 0.7 * 0.5 ** (45/21)
    assert S.signal_strength(SignalKind.PARTNERSHIP, 21) == 0.225
    assert S.signal_strength(SignalKind.INTENT, 0) == 0.3


def test_per_kind_half_lives():
    assert S.half_life_for(SignalKind.EXEC_MOVE) == 45.0 and S.half_life_for(SignalKind.NEW_HIRE) == 45.0
    for k in (SignalKind.FUNDING, SignalKind.MA, SignalKind.PRODUCT_LAUNCH, SignalKind.PARTNERSHIP,
              SignalKind.PROJECT_PAIN, SignalKind.HIRING_PLAN, SignalKind.INTENT):
        assert S.half_life_for(k) == 21.0


def test_old_exec_move_outranks_newer_partnership():
    # 40-day-old exec move (0.9 * 0.5**(40/45) = 0.486) beats a 10-day-old partnership (0.45 * 0.5**(10/21) = 0.323)
    assert S.signal_strength(SignalKind.EXEC_MOVE, 40) == 0.486
    assert S.signal_strength(SignalKind.PARTNERSHIP, 10) == 0.3235


# ------------------------------------------------------------ scoops_to_signals
@pytest.fixture(scope="module")
def batch(verdicts, accounts, config):
    by_zi = {a.zoominfo_id: a for a in accounts if a.zoominfo_id}
    return S.scoops_to_signals(list(verdicts.values()), by_zi, config, TODAY)


def test_signals_only_for_kept_scoops(batch, verdicts):
    kept = {v.scoop.id for v in verdicts.values() if v.keep}
    ids = {s.source.split(":")[1] for s in batch.signals} | {s.id for s in batch.unmatched}
    assert ids == kept


def test_unmatched_company_returned_not_discarded(batch):
    assert [s.id for s in batch.unmatched] == ["SIG-19"]


def test_batch_unpacks_as_tuple(verdicts, accounts, config):
    signals, unmatched = S.scoops_to_signals(list(verdicts.values()), {}, config, TODAY)
    assert signals == [] and len(unmatched) == len([v for v in verdicts.values() if v.keep])


def test_signal_fields(batch):
    sig = next(s for s in batch.signals if s.source == "zoominfo_scoop:SIG-07")
    assert sig.kind is SignalKind.EXEC_MOVE and sig.sf_account_id == "FAKE-SIG-A1" and sig.zoominfo_id == "FAKE-ZI-2001"
    assert sig.evidence == "<Person> was appointed VP of Product."
    assert sig.source_date == datetime(2026, 11, 20, tzinfo=timezone.utc)
    assert sig.strength == 0.8333                       # 0.9 * 0.5 ** (5/45)


def test_source_label_constant():
    assert S.SOURCE_LABEL == "ZoomInfo scoop"


def test_evidence_trimmed(batch):
    sig = next(s for s in batch.signals if s.source == "zoominfo_scoop:SIG-30")
    assert len(sig.evidence) == S.EVIDENCE_MAX_CHARS and sig.evidence.endswith("…")
    assert "  " not in sig.evidence


def test_evidence_collapses_whitespace():
    assert S._trim_evidence("  a \n  b\t c ") == "a b c"


def test_signals_sorted_strongest_first(batch):
    strengths = [s.strength for s in batch.signals]
    assert strengths == sorted(strengths, reverse=True)


def test_duplicate_scoop_ids_used_once(verdicts, accounts, config):
    by_zi = {a.zoominfo_id: a for a in accounts if a.zoominfo_id}
    v = verdicts["SIG-07"]
    out = S.scoops_to_signals([v, v], by_zi, config, TODAY)
    assert len(out.signals) == 1


def test_dropped_verdicts_ignored(verdicts, accounts, config):
    by_zi = {a.zoominfo_id: a for a in accounts if a.zoominfo_id}
    out = S.scoops_to_signals([verdicts["SIG-01"]], by_zi, config, TODAY)
    assert out.signals == [] and out.unmatched == []


def test_empty_company_id_is_unmatched(config):
    s = make_scoop(cid="")
    out = S.scoops_to_signals(S.filter_scoops([s], config, TODAY), {"": Account("X", "n", zoominfo_id="")}, config, TODAY)
    assert out.signals == [] and out.unmatched == [s]


def test_determinism_same_input_same_output(sig_scoops, accounts, config):
    runs = [S.trigger_radar(list(sig_scoops.values()), accounts, config, TODAY, top_n=None) for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]


def test_determinism_independent_of_input_order(sig_scoops, accounts, config):
    a = S.trigger_radar(list(sig_scoops.values()), accounts, config, TODAY, top_n=None)
    b = S.trigger_radar(list(reversed(sig_scoops.values())), list(reversed(accounts)), config, TODAY, top_n=None)
    assert [(e.account.sf_account_id, e.signal.source, e.combined_strength) for e in a] == \
           [(e.account.sf_account_id, e.signal.source, e.combined_strength) for e in b]


# ------------------------------------------------------------ best_signal / combined_strength
def _sig(kind, strength, day=20, src="s"):
    return Signal("A", kind, strength, src, datetime(2026, 11, day, tzinfo=timezone.utc), "e", "Z")


def test_best_signal_picks_strongest():
    a, b = _sig(SignalKind.FUNDING, 0.4), _sig(SignalKind.EXEC_MOVE, 0.8)
    assert S.best_signal([a, b]) is b


def test_best_signal_tie_prefers_newer():
    old, new = _sig(SignalKind.FUNDING, 0.5, day=10, src="a"), _sig(SignalKind.FUNDING, 0.5, day=15, src="b")
    assert S.best_signal([old, new]) is new
    assert S.best_signal([new, old]) is new


def test_best_signal_full_tie_deterministic_by_kind_then_source():
    a, b = _sig(SignalKind.FUNDING, 0.5, src="b"), _sig(SignalKind.FUNDING, 0.5, src="a")
    assert S.best_signal([a, b]).source == "a" == S.best_signal([b, a]).source


def test_best_signal_empty_and_none_only():
    assert S.best_signal([]) is None
    assert S.best_signal([S.no_trigger_signal(Account("A", "n"))]) is None


def test_combined_empty_is_zero():
    assert S.combined_strength([]) == 0.0


def test_combined_literal_numbers():
    f = lambda *xs: S.combined_strength([_sig(SignalKind.FUNDING, x, src=f"s{i}") for i, x in enumerate(xs)])
    assert S.COMBINE_DAMPING == 0.5
    assert f(0.5, 0.4) == 0.6                # 1 - 0.5 * (1 - 0.5*0.4)
    assert f(0.4, 0.5) == 0.6
    assert f(0.5, 0.4, 0.2) == 0.64          # 1 - 0.5 * 0.8 * 0.9
    assert f(0.5, 0.5) == 0.625


def test_combined_single_equals_strength():
    assert S.combined_strength([_sig(SignalKind.FUNDING, 0.6)]) == 0.6


def test_combined_at_least_best_and_capped():
    sigs = [_sig(SignalKind.FUNDING, 0.6), _sig(SignalKind.EXEC_MOVE, 0.9), _sig(SignalKind.MA, 0.5)]
    c = S.combined_strength(sigs)
    assert 0.9 <= c <= 1.0


def test_combined_diminishing_returns():
    base = [_sig(SignalKind.FUNDING, 0.5)]
    gains, cur = [], S.combined_strength(base)
    for i in range(5):
        base = base + [_sig(SignalKind.FUNDING, 0.5, src=f"s{i}")]
        nxt = S.combined_strength(base)
        gains.append(nxt - cur)
        cur = nxt
    assert all(g > 0 for g in gains) and gains == sorted(gains, reverse=True)
    assert cur < 1.0


def test_combined_max_strength_stays_one():
    assert S.combined_strength([_sig(SignalKind.FUNDING, 1.0)] * 4) == 1.0


def test_combined_order_independent():
    sigs = [_sig(SignalKind.FUNDING, 0.3), _sig(SignalKind.MA, 0.7), _sig(SignalKind.NEW_HIRE, 0.5)]
    assert S.combined_strength(sigs) == S.combined_strength(list(reversed(sigs)))


# ------------------------------------------------------------ no trigger
def test_no_trigger_signal():
    a = Account("SF1", "Name", zoominfo_id="ZI1")
    s = S.no_trigger_signal(a)
    assert (s.kind, s.strength, s.evidence, s.sf_account_id, s.zoominfo_id) == (SignalKind.NONE, 0.0, "No trigger found", "SF1", "ZI1")
    assert s.source_date is None


def test_no_trigger_signal_without_ids():
    s = S.no_trigger_signal(Account(None, "Name"))
    assert s.sf_account_id is None and s.zoominfo_id is None


# ------------------------------------------------------------ radar
@pytest.fixture(scope="module")
def radar(sig_scoops, accounts, config):
    return S.trigger_radar(list(sig_scoops.values()), accounts, config, TODAY, top_n=None)


def test_radar_sorted_by_strength_desc(radar):
    cs = [e.combined_strength for e in radar]
    assert cs == sorted(cs, reverse=True)


def test_radar_only_accounts_with_signals(radar):
    names = [e.account.name for e in radar]
    assert "No ZoomInfo Id Ltd" not in names and "Fictional Capital Partners" not in names
    assert len(names) == len(set(names))


def test_radar_unmatched_scoop_not_in_radar(radar):
    assert all(s.zoominfo_id != "FAKE-ZI-9999" for e in radar for s in e.signals)


def test_radar_best_signal_is_strongest_of_account(radar):
    for e in radar:
        assert e.signal.strength == max(s.strength for s in e.signals)
        assert e.combined_strength >= e.signal.strength


def test_radar_why_mentions_kind_and_evidence(radar):
    e = radar[0]
    assert e.signal.kind.value in e.why and e.signal.evidence in e.why


def test_radar_why_counts_extra_signals(radar):
    multi = [e for e in radar if len(e.signals) > 1][0]
    assert f"+{len(multi.signals) - 1} more" in multi.why


def test_radar_top_n(sig_scoops, accounts, config, radar):
    top2 = S.trigger_radar(list(sig_scoops.values()), accounts, config, TODAY, top_n=2)
    assert top2 == radar[:2]


def test_radar_top_n_zero_and_large(sig_scoops, accounts, config, radar):
    assert S.trigger_radar(list(sig_scoops.values()), accounts, config, TODAY, top_n=0) == []
    assert len(S.trigger_radar(list(sig_scoops.values()), accounts, config, TODAY, top_n=500)) == len(radar)


def test_radar_negative_top_n_rejected(accounts, config):
    with pytest.raises(ValueError):
        S.trigger_radar([], accounts, config, TODAY, top_n=-1)


def test_radar_default_top_n_is_20(config):
    accts = [Account(f"SF{i}", f"Co {i:02d}", zoominfo_id=f"Z{i}") for i in range(30)]
    scoops = [make_scoop(cid=f"Z{i}", sid=f"S{i}", desc="Co raised funds.") for i in range(30)]
    assert len(S.trigger_radar(scoops, accts, config, TODAY)) == 20


def test_radar_empty(accounts, config):
    assert S.trigger_radar([], accounts, config, TODAY) == []
    assert S.trigger_radar([make_scoop()], [], config, TODAY) == []


def test_radar_ties_break_by_recency_then_name(config):
    accts = [Account("SF-B", "Bravo Pty", zoominfo_id="ZB"), Account("SF-A", "alpha Pty", zoominfo_id="ZA"),
             Account("SF-C", "Charlie Pty", zoominfo_id="ZC")]
    desc = "Co raised $1 million."
    scoops = [make_scoop(cid="ZB", sid="1", desc=desc, days=3), make_scoop(cid="ZA", sid="2", desc=desc, days=3),
              make_scoop(cid="ZC", sid="3", desc=desc, days=3)]
    out = S.trigger_radar(scoops, accts, config, TODAY)
    assert [e.account.name for e in out] == ["alpha Pty", "Bravo Pty", "Charlie Pty"]      # name, case-insensitive
    assert out == S.trigger_radar(list(reversed(scoops)), list(reversed(accts)), config, TODAY)


def test_radar_equal_strength_newer_first(config):
    # An exec move 0 days old and a funding scoop with a strength tuned to be equal is brittle, so use equal kind
    # and different ages: older scoop is weaker, newer comes first regardless of name.
    accts = [Account("SF-A", "Aardvark", zoominfo_id="ZA"), Account("SF-Z", "Zebra", zoominfo_id="ZZ")]
    scoops = [make_scoop(cid="ZA", sid="1", days=10), make_scoop(cid="ZZ", sid="2", days=2)]
    assert [e.account.name for e in S.trigger_radar(scoops, accts, config, TODAY)] == ["Zebra", "Aardvark"]


def test_radar_accounts_sharing_a_zoominfo_id_each_get_an_entry(config):
    accts = [Account("SF-1", "First", zoominfo_id="Z"), Account("SF-2", "Second", zoominfo_id="Z")]
    out = S.trigger_radar([make_scoop(cid="Z")], accts, config, TODAY)
    assert [e.account.name for e in out] == ["First", "Second"]
    assert out[0].signal.source == out[1].signal.source and out[0].combined_strength == out[1].combined_strength


def test_radar_combines_multiple_scoops_per_account(config):
    acct = Account("SF-1", "Gamma", zoominfo_id="Z")
    one = S.trigger_radar([make_scoop(cid="Z", sid="1")], [acct], config, TODAY)[0]
    two = S.trigger_radar([make_scoop(cid="Z", sid="1"), make_scoop(cid="Z", sid="2", desc="Gamma launched an app.",
                                                                    types=("Product Launch",), topics=())],
                          [acct], config, TODAY)[0]
    assert two.combined_strength > one.combined_strength and len(two.signals) == 2


def test_radar_does_not_mutate_inputs(sig_scoops, accounts, config):
    before = json.dumps([a.__dict__ for a in accounts], default=str)
    S.trigger_radar(list(sig_scoops.values()), accounts, config, TODAY)
    assert json.dumps([a.__dict__ for a in accounts], default=str) == before


# ------------------------------------------------------------ intent (UNVERIFIED placeholder)
@pytest.fixture
def intent_rows():
    return load_fixture("signals_intent.json")["rows"]


def test_intent_only_relevant_topics_scored(intent_rows, accounts, config):
    skipped = []
    out = S.intent_to_signals(intent_rows, accounts, config, TODAY, skipped)
    topics = {s.source.split(":", 2)[2] for s in out}
    assert topics <= S.INTENT_TOPICS
    assert any(r[0].get("topic") == "Payroll Software" and "not a Pendo-relevant" in r[1] for r in skipped)


def test_intent_standalone_onboarding_not_a_topic(accounts, config):
    skipped = []
    row = {"company_id": "FAKE-ZI-2001", "topic": "Onboarding", "signal_score": 100, "audience_strength": "A"}
    assert S.intent_to_signals([row], accounts, config, TODAY, skipped) == []
    assert "not a Pendo-relevant" in skipped[0][1]
    assert "onboarding" not in S.INTENT_TOPICS and {"product onboarding", "user onboarding"} <= S.INTENT_TOPICS


def test_intent_every_row_accounted_for(intent_rows, accounts, config):
    skipped = []
    out = S.intent_to_signals(intent_rows, accounts, config, TODAY, skipped)
    assert len(out) + len(skipped) == len(intent_rows)


def test_intent_skip_reasons(intent_rows, accounts, config):
    skipped = []
    S.intent_to_signals(intent_rows, accounts, config, TODAY, skipped)
    reasons = " | ".join(r for _, r in skipped)
    for needle in ("outside 60-100", "not in A-E", "no matching account", "missing topic or company_id",
                   "superseded", "strength 0.0"):
        assert needle in reasons


def test_intent_score_60_skipped_not_zero_signal(accounts, config):
    row = {"company_id": "FAKE-ZI-2001", "topic": "Digital Experience", "signal_score": 60, "audience_strength": "A",
           "recorded_date": "2026-11-25"}
    skipped = []
    assert S.intent_to_signals([row], accounts, config, TODAY, skipped) == []
    assert skipped and "strength 0.0" in skipped[0][1]
    skipped.clear()
    assert S.intent_to_signals([{**row, "signal_score": 61}], accounts, config, TODAY, skipped) == []
    assert "faded below floor" in skipped[0][1]          # 0.025 < 0.10


def test_intent_highest_score_wins_per_company_topic(intent_rows, accounts, config):
    out = S.intent_to_signals(intent_rows, accounts, config, TODAY)
    dx = [s for s in out if s.zoominfo_id == "FAKE-ZI-2001"]
    assert len(dx) == 1 and "score 90" in dx[0].evidence


def test_intent_literal_strengths(accounts, config):
    def strength(score, aud, date=None, topic="Digital Experience"):
        r = {"company_id": "FAKE-ZI-2001", "topic": topic, "signal_score": score, "audience_strength": aud}
        if date:
            r["recorded_date"] = date
        out = S.intent_to_signals([r], accounts, config, TODAY)
        return out[0].strength if out else None
    assert S.INTENT_MAX_STRENGTH == 0.3 and S.INTENT_UNDATED_AGE_DAYS == 21
    assert strength(100, "A", "2026-11-25") == 0.3                # 1.0 capped at 0.3, today
    assert strength(80, "B", "2026-11-25") == 0.3                 # 0.425 capped
    assert strength(70, "C", "2026-11-25") == 0.175               # 0.25 * 0.7
    assert strength(70, "E", "2026-11-25") == 0.1                 # exactly the floor is kept
    assert strength(100, "A", "2026-11-20") == 0.2544             # 0.3 * 0.5 ** (5/21)
    assert strength(100, "A", "2026-10-30") == 0.1272             # 26 days
    assert strength(100, "A") == 0.15                             # undated = 21 days old: 0.3 * 0.5
    assert strength(75, "C") == 0.1312                            # undated 0.2625 * 0.5 = 0.13125 (float rounds down)
    assert strength(70, "E") is None                              # undated 0.05 < floor
    assert strength(100, "A", "2026-10-25") is None               # 31 days: older than the 30-day limit


def test_intent_undated_is_not_age_zero(accounts, config):
    r = {"company_id": "FAKE-ZI-2001", "topic": "Digital Experience", "signal_score": 100, "audience_strength": "A"}
    undated = S.intent_to_signals([r], accounts, config, TODAY)[0]
    today_dated = S.intent_to_signals([{**r, "recorded_date": "2026-11-25"}], accounts, config, TODAY)[0]
    assert undated.strength == 0.15 < today_dated.strength == 0.3 and undated.source_date is None


def test_intent_expired_reason(accounts, config):
    skipped = []
    r = {"company_id": "FAKE-ZI-2001", "topic": "Digital Experience", "signal_score": 100, "audience_strength": "A",
         "recorded_date": "2026-10-01"}
    assert S.intent_to_signals([r], accounts, config, TODAY, skipped) == []
    assert "expired" in skipped[0][1] and "limit 30" in skipped[0][1]


def test_intent_evidence_says_interest_not_dated_event(intent_rows, accounts, config):
    out = S.intent_to_signals(intent_rows, accounts, config, TODAY)
    assert out and all("interest signal, not a dated event" in s.evidence for s in out)


def test_intent_never_exceeds_cap_or_a_fresh_scoop(intent_rows, accounts, config):
    out = S.intent_to_signals(intent_rows, accounts, config, TODAY)
    assert out and all(s.strength <= 0.3 for s in out)
    assert S.signal_strength(SignalKind.EXEC_MOVE, 0) > max(s.strength for s in out)


def test_intent_recency_decay_applies_when_dated(accounts, config):
    row = {"company_id": "FAKE-ZI-2001", "topic": "Product Onboarding", "signal_score": 100, "audience_strength": "A"}
    fresh = S.intent_to_signals([{**row, "recorded_date": "2026-11-25"}], accounts, config, TODAY)[0]
    old = S.intent_to_signals([{**row, "recorded_date": "2026-11-05"}], accounts, config, TODAY)[0]
    assert old.strength < fresh.strength


def test_intent_audience_ordering(accounts, config):
    def strength(aud):
        r = {"company_id": "FAKE-ZI-2001", "topic": "User Onboarding", "signal_score": 70, "audience_strength": aud,
             "recorded_date": "2026-11-25"}
        return S.intent_to_signals([r], accounts, config, TODAY)[0].strength
    vals = [strength(a) for a in "ABCDE"]
    assert vals == sorted(vals, reverse=True) and vals[0] > vals[-1]


def test_intent_kind_source_and_links(intent_rows, accounts, config):
    out = S.intent_to_signals(intent_rows, accounts, config, TODAY)
    assert all(s.kind is SignalKind.INTENT and s.sf_account_id for s in out)
    assert all("UNVERIFIED" in s.evidence for s in out)
    assert {s.source for s in out} == {"zoominfo_intent:FAKE-ZI-2001:digital experience",
                                       "zoominfo_intent:FAKE-ZI-2002:product analytics"}


def test_intent_sources_unique_across_accounts(accounts, config):
    rows = [{"company_id": c, "topic": "Digital Experience", "signal_score": 90, "audience_strength": "A"}
            for c in ("FAKE-ZI-2001", "FAKE-ZI-2002")]
    out = S.intent_to_signals(rows, accounts, config, TODAY)
    assert len({s.source for s in out}) == 2


def test_intent_works_without_skipped_list_and_non_dict_rows(accounts, config):
    assert S.intent_to_signals(["junk", None], accounts, config, TODAY) == []


def test_intent_rejects_bool_and_string_scores(accounts, config):
    rows = [{"company_id": "FAKE-ZI-2001", "topic": "Digital Adoption", "signal_score": v, "audience_strength": "A"}
            for v in (True, "90")]
    skipped = []
    assert S.intent_to_signals(rows, accounts, config, TODAY, skipped) == [] and len(skipped) == 2


def test_intent_deterministic(intent_rows, accounts, config):
    a = S.intent_to_signals(intent_rows, accounts, config, TODAY)
    b = S.intent_to_signals(list(reversed(intent_rows)), list(reversed(accounts)), config, TODAY)
    assert a == b


# ------------------------------------------------------------ H3: multi-type scoops pick the highest-weight passing kind
_ALL_KIND_TYPES = {
    SignalKind.FUNDING: "Funding", SignalKind.EXEC_MOVE: "Executive Move", SignalKind.NEW_HIRE: "New Hire",
    SignalKind.MA: "Mergers & Acquisitions (M&A)", SignalKind.PRODUCT_LAUNCH: "Product Launch",
    SignalKind.PARTNERSHIP: "Partnership", SignalKind.PROJECT_PAIN: "Project", SignalKind.HIRING_PLAN: "Hiring Plans",
}
_PASS_ALL = "<P> joins as Head of Product. Gamma raised $1m, is hiring a Product Manager and launched an app."


@pytest.mark.parametrize("a,b", [(a, b) for i, a in enumerate(_ALL_KIND_TYPES) for b in list(_ALL_KIND_TYPES)[i + 1:]],
                         ids=lambda k: k.value)
def test_every_type_pair_picks_highest_weight_kind(config, a, b):
    v = judge(config, desc=_PASS_ALL, types=(_ALL_KIND_TYPES[a], _ALL_KIND_TYPES[b]), topics=("Customer Experience",))
    assert v.keep
    wa, wb = S.KIND_BASE_WEIGHT[a], S.KIND_BASE_WEIGHT[b]
    if wa != wb:
        assert v.kind is (a if wa > wb else b)
    else:
        assert v.kind.value == min(a.value, b.value)      # equal weights: alphabetical by kind name


def test_exec_move_plus_ma_is_exec_move(config):
    v = judge(config, desc="<P> joins as Head of Product after Gamma acquired Tiny Co.",
              types=("Executive Move", "Mergers & Acquisitions (M&A)"), topics=())
    assert v.kind is SignalKind.EXEC_MOVE


def test_project_plus_partnership_is_project_pain(config):
    v = judge(config, desc="Gamma partnered with a vendor on its CX project.", types=("Project", "Partnership"),
              topics=("Customer Experience",))
    assert v.kind is SignalKind.PROJECT_PAIN


def test_multi_type_failing_higher_kind_falls_to_lower(config):
    # exec move fails (no relevant role) so the partnership kind is what is left
    v = judge(config, desc="Gamma partnered with a vendor; <P> is the new CEO.", types=("Executive Move", "Partnership"), topics=())
    assert v.keep and v.kind is SignalKind.PARTNERSHIP


def test_kind_order_derived_from_weights_not_a_separate_list():
    assert not hasattr(S, "KIND_PRECEDENCE")
    ordered = S._by_weight(list(_ALL_KIND_TYPES))
    weights = [S.KIND_BASE_WEIGHT[k] for k in ordered]
    assert weights == sorted(weights, reverse=True)


# ------------------------------------------------------------ H1: role table (85 reviewer rows) and regressions
def _role_table_results(config):
    rows = load_fixture("signals_role_table.json")["rows"]
    out = []
    for r in rows:
        sc = Scoop(f"R{r['n']}", "Z", "Example Co", r["description"], TODAY, (r["type"],), (r["type"],))
        out.append((r, S.filter_scoops([sc], config, TODAY)[0].keep))
    return out


def test_role_table_has_85_rows():
    assert len(load_fixture("signals_role_table.json")["rows"]) == 85


def test_role_table_precision_and_recall(config):
    res = _role_table_results(config)
    tp = sum(1 for r, k in res if k and r["expected"] == "keep")
    fp = [r["n"] for r, k in res if k and r["expected"] == "drop"]
    fn = [r["n"] for r, k in res if not k and r["expected"] == "keep"]
    assert tp / (tp + len(fp)) >= 0.90, f"precision misses: {fp}"
    assert tp / (tp + len(fn)) >= 0.95, f"recall misses: {fn}"


def test_role_table_exact_known_misses(config):
    """Pins the remaining misses so a regression or an improvement is noticed (docs/signals_notes.md lists them)."""
    res = _role_table_results(config)
    assert sorted(r["n"] for r, k in res if k != (r["expected"] == "keep")) == []


@pytest.mark.parametrize("desc", [
    "Example Co hires Head of Product Supply and Logistics.",
    "Example Co names a Head of Product Compliance.",
    "Example Co appoints a Head of Product Development, Manufacturing.",
    "Example Co appoints Technology Procurement Director.",
    "Example Co has appointed a Director of Support Services (aged care).",
    "Example Co hires Social Media Manager for product campaigns.",
    "Example Co welcomes a Customer Support Officer.",
    "Example Co hires a Product Sales Manager.",
    "Example Co appoints a Head of Digital Finance.",
])
def test_non_buyer_titles_with_buyer_words_dropped(desc):
    assert S.relevant_role(desc) is None


@pytest.mark.parametrize("desc", [
    "Example Co appoints Chief Customer Officer.",
    "Example Co hired a Head of Customer Service and Support.",
    "Example Co appoints a Head of Mobile.",
    "Example Co hires a Head of Product Development.",
    "Example Co hires a VP Customer Care.",
])
def test_recall_fixes(desc):
    assert S.relevant_role(desc)


def test_for_connector_no_longer_links_title_to_domain():
    assert S.relevant_role("Example Co hires a Sales Director for the product range.") is None
    assert S.relevant_role("Example Co hires a Director for product.") is None


def test_negative_words_only_inspect_the_role_phrase_not_the_whole_sentence():
    assert S.relevant_role("Example Co appoints a Head of Product. The legal team welcomed the news.")
    assert S.relevant_role("Example Co appoints Head of Product; formerly in finance at a bank.")


# ------------------------------------------------------------ H2: hiring keywords
def test_support_staff_hospital_ward_dropped(config):
    v = judge(config, desc="Example Co is hiring support staff for the hospital ward.", types=("Hiring Plans",), topics=())
    assert not v.keep and "weak keyword 'support'" in v.reason


def test_drivers_for_delivery_platform_dropped(config):
    v = judge(config, desc="Example Co is hiring drivers for its delivery platform.", types=("Open Position",), topics=())
    assert not v.keep and "driver" in v.reason


@pytest.mark.parametrize("desc", [
    "Hiring mobile launch staff.", "Hiring platform operators.", "Hiring an app tester.", "Hiring engineering interns.",
])
def test_weak_keywords_alone_do_not_keep_hiring(config, desc):
    assert not judge(config, desc=desc, types=("Hiring Plans",), topics=()).keep


@pytest.mark.parametrize("desc", [
    "Hiring a Senior Product Manager.", "Hiring two UX designers.", "Hiring mobile app developers.",
    "Hiring a Customer Success Manager.", "Hiring a Head of Digital.",
])
def test_buyer_roles_keep_hiring(config, desc):
    assert judge(config, desc=desc, types=("Hiring Plans",), topics=()).keep


def test_strong_keyword_keeps_hiring_without_drop_word(config):
    assert judge(config, desc="Example Co is hiring across its digital team.", types=("Hiring Plans",), topics=()).keep


def test_topic_keeps_hiring_but_does_not_rescue_generic_drop(config):
    assert judge(config, desc="Example Co is hiring.", types=("Open Position",), topics=("Product Management",)).keep
    v = judge(config, desc="Example Co has more open roles in Medical & Health.", types=("Open Position",),
              topics=("Product Management",))
    assert not v.keep           # documented in docs/signals_notes.md (review L2)


def test_nurse_with_digital_app_but_no_role_dropped(config):
    assert not judge(config, desc="Example Co is hiring a nurse to support its new digital health app.",
                     types=("Hiring Plans",), topics=()).keep


# ------------------------------------------------------------ join key normalisation (review M2)
def test_join_keys_normalised(config):
    acct = Account("SF1", "Gamma", zoominfo_id=" 123 ")
    sc = make_scoop(cid="123")
    out = S.scoops_to_signals(S.filter_scoops([sc], config, TODAY), {123: acct}, config, TODAY)
    assert len(out.signals) == 1 and out.signals[0].zoominfo_id == "123" and out.unmatched == []
    radar = S.trigger_radar([sc], [acct], config, TODAY)
    assert [e.account.sf_account_id for e in radar] == ["SF1"]


def test_leading_zero_ids_do_not_match(config):
    out = S.scoops_to_signals(S.filter_scoops([make_scoop(cid="01")], config, TODAY),
                              {"1": Account("SF1", "G", zoominfo_id="1")}, config, TODAY)
    assert out.signals == [] and len(out.unmatched) == 1


def test_scoop_company_id_whitespace_stripped(config):
    out = S.scoops_to_signals(S.filter_scoops([make_scoop(cid=" 123 ")], config, TODAY),
                              {"123": Account("SF1", "G", zoominfo_id="123")}, config, TODAY)
    assert len(out.signals) == 1 and out.unmatched == []


# ============================================================ round 3 (second re-review)
# ------------------------------------------------------------ (a) role fixes
@pytest.mark.parametrize("desc", [
    "Example Co has hired an Executive Assistant to the CTO.",
    "Example Co hired a PA to the Head of Product.",
    "Example Co hired an EA to the Chief Digital Officer.",
])
def test_assistants_are_not_the_buyer(desc):
    assert S.relevant_role(desc) is None


def test_the_role_holder_after_an_assistant_still_counts():
    assert S.relevant_role("Example Co hired an Executive Assistant. A new CTO also joined.") == "CTO"


@pytest.mark.parametrize("desc", [
    "Example Co named a new Chief Information and Digital Officer.",
    "Example Co has a Chief Information and Technology Officer.",
    "Example Co has a Chief Digital and Information Officer.",
])
def test_combined_chief_titles_detected(desc):
    assert S.relevant_role(desc)


@pytest.mark.parametrize("desc", [
    "Example Co recruited a Head of Platform Engineering.",
    "Example Co hired a Head of Digital Customer Experience.",
    "Example Co hired a Director of Mobile Engineering.",
])
def test_one_free_word_between_title_and_domain(desc):
    assert S.relevant_role(desc)


@pytest.mark.parametrize("desc", [
    "Example Co hires a Sales Director for product.",
    "Example Co hires a Director of Hospital Technology.",
    "Example Co hires a Head of Procurement Engineering.",
])
def test_free_word_does_not_open_the_door_to_non_buyers(desc):
    assert S.relevant_role(desc) is None


def test_negative_window_ends_at_to_who_will_as_after():
    assert S.relevant_role("Example Co appointed a Chief Customer Officer to unify sales, success and support.")
    assert S.relevant_role("Example Co hired a Head of Product who will oversee supply planning tools.")
    assert S.relevant_role("Example Co named a Head of Digital as the legal reforms land.")
    assert S.relevant_role("Example Co hired a Head of Product after a finance review.")
    assert S.relevant_role("Example Co hired a Head of Product Supply to run logistics.") is None


@pytest.mark.parametrize("desc", [
    "Example Co hired a Product Manager, Fresh Produce.",
    "Example Co has appointed a Product Manager, Dairy.",
    "Example Co hired a Head of Product, Wine Portfolio.",
    "Example Co hired a Product Manager, Frozen Foods.",
    "Example Co hired a Head of Product Quality Assurance for its food plant.",
    "Example Co appointed a Head of Product Assurance, Pharmaceuticals.",
    "Example Co appointed a Head of Product Engineering, Mining Equipment.",
])
def test_physical_goods_product_roles_dropped(desc):
    assert S.relevant_role(desc) is None


def test_physical_words_only_matter_for_product_roles():
    assert S.relevant_role("Example Co hired a Head of Digital for its food business.")
    assert not S.role_match("Example Co hired a Head of Digital for its food business.").ambiguous


@pytest.mark.parametrize("desc", [
    "Example Co named a Head of Product, Footwear.",
    "Example Co hired a Product Lead, Home Loans.",
    "Example Co hired a Head of Product for its retail stores.",
])
def test_ambiguous_product_roles_kept_but_flagged(desc):
    rm = S.role_match(desc)
    assert rm.role and rm.ambiguous


@pytest.mark.parametrize("desc", [
    "Example Co hired a Head of Product.",
    "Example Co hired a Head of Product, Digital.",
    "Example Co hired a Head of Product, ANZ.",
    "Example Co hired a Chief Digital Officer for its retail stores.",   # not a 'product' role
])
def test_clear_roles_not_flagged(desc):
    rm = S.role_match(desc)
    assert rm.role and not rm.ambiguous


def test_ambiguous_verdict_reason_and_strength(config, accounts):
    v = judge(config, desc="Example Co named a Head of Product, Footwear.", types=("Executive Move",), topics=(), days=5)
    assert v.keep and v.ambiguous
    assert v.reason == ("Kept: Executive Move, role 'Head of Product', 5 days ago; "
                        "role ambiguous: check it is a digital product role")
    by_zi = {a.zoominfo_id: a for a in accounts if a.zoominfo_id}
    sig = S.scoops_to_signals([v], by_zi, config, TODAY).signals[0]
    assert sig.strength == 0.5833                         # 0.9 * 0.5 ** (5/45) * 0.7
    clear = judge(config, desc="Example Co named a Head of Product.", types=("Executive Move",), topics=(), days=5)
    assert not clear.ambiguous and "ambiguous" not in clear.reason
    assert S.scoops_to_signals([clear], by_zi, config, TODAY).signals[0].strength == 0.8333


def test_marketing_roles_still_dropped_without_the_old_prefilter():
    for d in ("Example Co hired a Product Marketing Manager.", "Example Co hired a Digital Marketing Lead."):
        assert S.relevant_role(d) is None


# ------------------------------------------------------------ (b) intent vs real signals
def _isig(strength, src="zoominfo_intent:Z:digital experience", day=20):
    return Signal("A", SignalKind.INTENT, strength, src, datetime(2026, 11, day, tzinfo=timezone.utc), "e", "Z")


def test_intent_never_best_when_a_real_signal_exists():
    weak_real = _sig(SignalKind.FUNDING, 0.16)
    intent = _isig(0.3)
    assert S.best_signal([intent, weak_real]) is weak_real
    assert S.best_signal([weak_real, intent]) is weak_real


def test_intent_alone_can_be_best():
    intent = _isig(0.3)
    assert S.best_signal([intent]) is intent


def test_intent_counts_only_as_damped_other_in_combined():
    # real 0.16 fully counted, intent 0.3 damped: 1 - 0.84 * (1 - 0.5 * 0.3) = 0.286
    assert S.combined_strength([_sig(SignalKind.FUNDING, 0.16), _isig(0.3)]) == 0.286
    assert S.combined_strength([_isig(0.3)]) == 0.3
    # two intents, no real: best intent 0.3, the other damped
    assert S.combined_strength([_isig(0.3), _isig(0.2, src="b")]) == 0.37


def test_intent_cannot_lift_combined_above_real_plus_damped_intent():
    real = _sig(SignalKind.FUNDING, 0.16)
    with_intent = S.combined_strength([real, _isig(0.3)])
    assert with_intent < 0.3 + 0.16     # not a straight sum
    assert with_intent > S.combined_strength([real])


# ------------------------------------------------------------ (c) per-kind max age and floor
def test_max_age_table_literal():
    assert S.KIND_MAX_AGE_DAYS == {
        SignalKind.EXEC_MOVE: 90, SignalKind.NEW_HIRE: 90, SignalKind.FUNDING: 45, SignalKind.PRODUCT_LAUNCH: 45,
        SignalKind.MA: 45, SignalKind.PROJECT_PAIN: 45, SignalKind.PARTNERSHIP: 30, SignalKind.HIRING_PLAN: 30,
        SignalKind.INTENT: 30}
    assert S.MIN_STRENGTH_FLOOR == 0.10 and S.AMBIGUOUS_ROLE_FACTOR == 0.7


_BOUNDARY_CASES = [
    ("Executive Move", "<P> joins as Head of Product.", (), 90),
    ("New Hire", "<P> joins as Head of Digital.", (), 90),
    ("Funding", "Gamma raised $5 million.", (), 45),
    ("Product Launch", "Gamma launched a new app.", (), 45),
    ("Mergers & Acquisitions (M&A)", "Gamma acquired Tiny Co.", (), 45),
    ("Project", "Gamma is rebuilding its app.", ("Customer Experience",), 45),
    ("Partnership", "Gamma partnered with a vendor.", (), 30),
    ("Hiring Plans", "Gamma is hiring a Product Manager.", (), 30),
]


@pytest.mark.parametrize("typ,desc,topics,limit", _BOUNDARY_CASES, ids=[c[0] for c in _BOUNDARY_CASES])
def test_per_kind_age_boundary(config, typ, desc, topics, limit):
    assert judge(config, desc=desc, types=(typ,), topics=topics, days=limit).keep
    v = judge(config, desc=desc, types=(typ,), topics=topics, days=limit + 1)
    assert not v.keep and v.reason.startswith("Dropped: expired") and f"limit {limit}" in v.reason


def test_exec_move_days_46_to_90_kept_with_literal_strengths(config, accounts):
    by_zi = {a.zoominfo_id: a for a in accounts if a.zoominfo_id}
    for days, expected in ((46, 0.4431), (90, 0.225)):
        v = judge(config, desc="<P> joins as Head of Product.", types=("Executive Move",), topics=(), days=days)
        assert v.keep
        assert S.scoops_to_signals([v], by_zi, config, TODAY).signals[0].strength == expected
    assert S.signal_strength(SignalKind.NEW_HIRE, 90) == 0.1625
    assert S.signal_strength(SignalKind.PARTNERSHIP, 30) == 0.1672
    assert S.signal_strength(SignalKind.HIRING_PLAN, 30) == 0.2043


def test_all_default_kept_ages_stay_above_the_floor():
    for kind, limit in S.KIND_MAX_AGE_DAYS.items():
        assert S.signal_strength(kind, limit) >= S.MIN_STRENGTH_FLOOR, kind


def test_below_floor_dropped_with_reason(config, monkeypatch):
    monkeypatch.setattr(S, "MIN_STRENGTH_FLOOR", 0.5)
    assert judge(config, days=5).keep                       # 0.7 * 0.5 ** (5/21) = 0.5937
    v = judge(config, days=15)                              # 0.4267 < 0.5
    assert not v.keep and "faded below floor" in v.reason and "0.43" in v.reason


def test_floor_boundary_is_inclusive(config, monkeypatch):
    monkeypatch.setattr(S, "MIN_STRENGTH_FLOOR", 0.4267)
    assert judge(config, days=15).keep


def test_floor_applies_to_the_ambiguous_factor(config, monkeypatch):
    monkeypatch.setattr(S, "MIN_STRENGTH_FLOOR", 0.6)       # 0.8333 clear, 0.5833 ambiguous
    kw = dict(types=("Executive Move",), topics=(), days=5)
    assert judge(config, desc="Example Co named a Head of Product.", **kw).keep
    assert not judge(config, desc="Example Co named a Head of Product, Footwear.", **kw).keep


def test_multi_type_expired_high_kind_falls_back_to_lower(config):
    # exec move is within 90 days; the funding half of the same scoop would be expired but exec wins anyway
    v = judge(config, desc="<P> joins as Head of Product after Gamma raised $5 million.",
              types=("Executive Move", "Funding"), topics=(), days=60)
    assert v.keep and v.kind is SignalKind.EXEC_MOVE
    # an M&A + Partnership scoop at 40 days: partnership is expired (30), M&A (45) survives
    v = judge(config, desc="Gamma acquired Tiny Co.", types=("Partnership", "Mergers & Acquisitions (M&A)"),
              topics=(), days=40)
    assert v.keep and v.kind is SignalKind.MA


# ------------------------------------------------------------ (d) verdict reasons
def test_every_reason_is_a_kept_or_dropped_sentence(verdicts):
    for v in verdicts.values():
        assert v.reason.startswith("Kept: " if v.keep else "Dropped: ")
        assert "\n" not in v.reason and len(v.reason) < 300


def test_reason_literals(verdicts):
    assert verdicts["SIG-07"].reason == "Kept: Executive Move, role 'VP of Product', 5 days ago"
    assert verdicts["SIG-01"].reason == ("Dropped: generic hiring note (matches drop keyword 'medical & health', "
                                         "no buyer-role context)")
    assert verdicts["SIG-17"].reason == "Kept: Product Launch, 45 days ago"
    assert verdicts["SIG-18"].reason == "Dropped: expired, 46 days old (limit 45 for product_launch)"
    assert verdicts["SIG-23"].reason == "Kept: Product Launch, today (future-dated, treated as today)"
    assert verdicts["SIG-13"].reason == "Kept: Project, buyer topic digital workplace, 5 days ago"
    assert verdicts["SIG-20"].reason == "Dropped: scoop type not a Pendo trigger (types: Earnings)"


def test_reason_age_phrases(config):
    assert judge(config, days=0).reason == "Kept: Funding, today"
    assert judge(config, days=1).reason == "Kept: Funding, 1 day ago"
    assert judge(config, days=2).reason == "Kept: Funding, 2 days ago"


def test_verdict_summary(verdicts):
    assert S.verdict_summary(verdicts["SIG-07"]) == verdicts["SIG-07"].reason
    v = S.ScoopVerdict(verdicts["SIG-07"].scoop, False, SignalKind.NONE, "Dropped:  two\n lines ")
    assert S.verdict_summary(v) == "Dropped: two lines"


def test_two_free_words_are_not_matched():
    # known limit (docs/signals_notes.md): only ONE free word between title and domain
    assert S.relevant_role("Example Co recruited a Head of Cloud Platform Engineering.") is None


# ------------------------------------------------------------ (e) held-out regression
def _heldout_metrics(config, name):
    tp = fp = fn = tn = 0
    misses = []
    for r in load_fixture(name)["rows"]:
        if r.get("judge"):
            continue
        sc = Scoop(r["id"], "Z", "Example Co", r["description"], TODAY - timedelta(days=r["age_days"]),
                   tuple(r["types"]), tuple(r["topics"]))
        keep = S.filter_scoops([sc], config, TODAY)[0].keep
        want = r["expect"] == "keep"
        tp += keep and want
        fp += keep and not want
        fn += (not keep) and want
        tn += (not keep) and not want
        if keep != want:
            misses.append(r["id"])
    return (tp / (tp + fp) if tp + fp else 1.0), (tp / (tp + fn) if tp + fn else 1.0), misses


def test_heldout_round1_not_below_reviewer_numbers(config):
    precision, recall, misses = _heldout_metrics(config, "signals_heldout1.json")
    assert precision >= 1.0 and recall >= 0.96, misses


def test_heldout_round2_improves_on_reviewer_numbers(config):
    precision, recall, misses = _heldout_metrics(config, "signals_heldout2.json")
    assert precision > 0.57 and recall >= 0.89, misses


def test_heldout_files_have_expected_shape():
    for name, n in (("signals_heldout1.json", 128), ("signals_heldout2.json", 28)):
        rows = load_fixture(name)["rows"]
        assert len(rows) == n and all({"id", "types", "topics", "description", "expect", "age_days"} <= set(r) for r in rows)


# ============================================================ round 4 (fixes branch)
# ------------------------------------------------------------ L2: accounts sharing a ZoomInfo id
def test_scoops_to_signals_accepts_a_list_of_accounts_per_id(config):
    a1, a2 = Account("SF-1", "One", zoominfo_id="Z"), Account("SF-2", "Two", zoominfo_id="Z")
    v = S.filter_scoops([make_scoop(cid="Z")], config, TODAY)
    out = S.scoops_to_signals(v, {"Z": [a1, a2]}, config, TODAY)
    assert [x.sf_account_id for x in out.signals] == ["SF-1", "SF-2"] and out.unmatched == []
    one, two = out.signals
    assert (one.kind, one.strength, one.source, one.source_date, one.evidence, one.zoominfo_id) == \
           (two.kind, two.strength, two.source, two.source_date, two.evidence, two.zoominfo_id)


def test_single_account_value_still_works_and_empty_list_is_unmatched(config):
    v = S.filter_scoops([make_scoop(cid="Z")], config, TODAY)
    assert len(S.scoops_to_signals(v, {"Z": Account("SF-1", "One")}, config, TODAY).signals) == 1
    out = S.scoops_to_signals(v, {"Z": []}, config, TODAY)
    assert out.signals == [] and len(out.unmatched) == 1


def test_list_value_order_does_not_change_output_order(config):
    a1, a2 = Account("SF-1", "One"), Account("SF-2", "Two")
    v = S.filter_scoops([make_scoop(cid="Z")], config, TODAY)
    x = S.scoops_to_signals(v, {"Z": [a1, a2]}, config, TODAY).signals
    y = S.scoops_to_signals(v, {"Z": [a2, a1]}, config, TODAY).signals
    assert x == y


def test_intent_signal_attached_to_every_account_sharing_the_id(config):
    accts = [Account("SF-1", "One", zoominfo_id="Z"), Account("SF-2", "Two", zoominfo_id="Z")]
    row = {"company_id": "Z", "topic": "Digital Experience", "signal_score": 100, "audience_strength": "A",
           "recorded_date": "2026-11-25"}
    out = S.intent_to_signals([row], accts, config, TODAY)
    assert sorted(x.sf_account_id for x in out) == ["SF-1", "SF-2"]


# ------------------------------------------------------------ ambiguity carried in the evidence
def test_ambiguous_evidence_suffix_and_helper(config, accounts):
    by_zi = {a.zoominfo_id: a for a in accounts if a.zoominfo_id}
    v = judge(config, desc="Example Co named a Head of Product, Footwear.", types=("Executive Move",), topics=(), days=5)
    sig = S.scoops_to_signals([v], by_zi, config, TODAY).signals[0]
    assert sig.evidence == "Example Co named a Head of Product, Footwear. (role ambiguous: check it is a digital product role)"
    assert S.is_ambiguous(sig)
    clear = judge(config, desc="Example Co named a Head of Product.", types=("Executive Move",), topics=(), days=5)
    csig = S.scoops_to_signals([clear], by_zi, config, TODAY).signals[0]
    assert csig.evidence == "Example Co named a Head of Product." and not S.is_ambiguous(csig)


def test_ambiguous_suffix_survives_long_descriptions(config, accounts):
    by_zi = {a.zoominfo_id: a for a in accounts if a.zoominfo_id}
    desc = "Example Co named a Head of Product, Footwear. " + "More text. " * 60
    v = judge(config, desc=desc, types=("Executive Move",), topics=(), days=5)
    sig = S.scoops_to_signals([v], by_zi, config, TODAY).signals[0]
    assert len(sig.evidence) == S.EVIDENCE_MAX_CHARS and S.is_ambiguous(sig)


def test_is_ambiguous_false_for_intent_and_none():
    assert not S.is_ambiguous(_isig(0.2)) and not S.is_ambiguous(S.no_trigger_signal(Account("A", "n")))


# ------------------------------------------------------------ (1) investor-note rule, more verbs
def fund(config, desc, name="Example Co"):
    return S.filter_scoops([make_scoop(desc=desc, name=name)], config, TODAY)[0]


@pytest.mark.parametrize("desc", [
    "Example Capital participated in a $15 million round for Other Co.",
    "Example Ventures led a seed investment in Other Co.",
    "Example Super has taken a stake in Other Co's infrastructure arm.",
    "Example Fund co-led a Series A in Other Co.",
    "Example Co took a stake in Other Co.",
    "Example Partners backed Other Co.",
    "Example Co is backed by Fund Z.",
])
def test_investor_verbs_dropped(config, desc):
    assert not fund(config, desc).keep


@pytest.mark.parametrize("desc", [
    "Fund Y led a $3M round in Example Co.",
    "Fund Y took a stake in Example Co.",
    "Example Co raised $5 million, backed by Fund Z.",
    "Example Co has raised $5 million and Fund Z participated in the round.",
    "Example Co closed a Series B round.",
    "Example Co has received an investment of $3M.",
])
def test_investee_and_normal_funding_kept(config, desc):
    assert fund(config, desc).keep


def test_company_raised_then_investor_invested_is_kept(config):
    assert fund(config, "Gamma Apps raised $8 million; Fund X invested in the round.", name="Gamma Apps Pty Ltd").keep


def test_investor_reason_text(config):
    assert fund(config, "Example Capital participated in a round for Other Co.").reason == \
        "Dropped: investor note: the company is the one investing, not a company that received funding"
    assert "no funds received" in fund(config, "Example Co is backed by Fund Z.").reason


# ------------------------------------------------------------ (2) former / replaced / board
@pytest.mark.parametrize("desc", [
    "Example Bank appoints a Chair of the Risk Committee, a former CIO of a rival lender.",
    "Example Co has welcomed a Head of Procurement. She replaces the former Head of Digital.",
    "Example Co appointed a CMO, replacing the Chief Digital Officer.",
    "Example Co hired a CFO who was previously Chief Product Officer at a retailer.",
    "Example Co hired a Head of Finance, an ex-CPO of a rival.",
    "Example Co appointed a COO who succeeds the outgoing Head of Digital.",
])
def test_former_or_predecessor_roles_rejected(desc):
    assert S.relevant_role(desc) is None


@pytest.mark.parametrize("desc", [
    "She succeeds Jane Smith as Chief Digital Officer.",
    "Pat replaces Jane Smith as Head of Product.",
    "An ex-CPO of a bank joins as Head of Digital.",
    "Example Co has welcomed a Head of Digital. She replaces the former Head of Procurement.",
    "Example Co hired a Head of Product, who previously worked in finance.",
])
def test_new_leader_in_a_replacement_sentence_still_counts(desc):
    assert S.relevant_role(desc)


@pytest.mark.parametrize("desc", [
    "Example Co has appointed Jordan Fake to its Board; Jordan was previously Chief Product Officer.",
    "Example Co has appointed a Chair. She is also a director of two technology companies.",
    "Example Co appointed a non-executive director with a Chief Digital Officer background.",
    "The Board welcomed a new Chief Digital Officer.",
])
def test_board_notes_reject_roles(desc):
    assert S.relevant_role(desc) is None


def test_dashboard_and_onboarding_are_not_board():
    assert S.relevant_role("Example Co hired a Head of Product to lead dashboard and onboarding work.")


# ------------------------------------------------------------ (3) CIO
@pytest.mark.parametrize("desc", [
    "Example Super has appointed a CIO, who will oversee its $40 billion investment portfolio.",
    "Example Co appointed a CIO for its superannuation fund assets.",
    "Example Co appointed a CIO to manage funds under management.",
])
def test_cio_in_investment_sentence_rejected(desc):
    assert S.relevant_role(desc) is None


def test_cio_elsewhere_still_counts_and_other_sentences_do_not_veto():
    assert S.relevant_role("Example Co appointed a CIO to modernise its apps.") == "CIO"
    assert S.relevant_role("Example Co appointed a CIO. The company manages an investment portfolio.") == "CIO"
    assert S.relevant_role("Example Co appointed a Chief Information Officer for its investment arm.")   # spelled out


# ------------------------------------------------------------ (4) hiring path
@pytest.mark.parametrize("desc", [
    "Example Co is hiring a Product Compliance Officer and a Product Safety Auditor.",
    "Example Co is hiring a Product Compliance Officer.",
    "Example Co is hiring product engineers for its app.",
    "Example Co is hiring a Product Manager, Legal Operations.",
    "Example Co is hiring a Product Owner for procurement systems.",
])
def test_hiring_negative_list_and_bare_product_weak(config, desc):
    assert not judge(config, desc=desc, types=("Hiring Plans",), topics=()).keep


@pytest.mark.parametrize("desc", [
    "Example Co is hiring a Product Design Lead.", "Example Co is hiring a Senior Product Manager.",
    "Example Co is hiring a Product Owner for its mobile app.",
])
def test_hiring_product_roles_still_kept(config, desc):
    assert judge(config, desc=desc, types=("Hiring Plans",), topics=()).keep


def test_bare_product_is_a_weak_keyword():
    assert "product" in S.WEAK_KEEP_KEYWORDS


# ------------------------------------------------------------ (5) chief X and Y officer, hyphenated free words
@pytest.mark.parametrize("desc", [
    "Example Co has appointed a Chief Customer and Digital Officer.",
    "Example Co has a Chief Experience and Technology Officer.",
    "Example Co has a Chief Information and Technology Officer.",
    "Example Co has a Chief Product and Growth Officer.",
    "Example Co named a Chief Digital and Strategy Officer.",
])
def test_chief_x_and_y_officer_general(desc):
    assert S.relevant_role(desc)


@pytest.mark.parametrize("desc", [
    "Example Co has appointed a Chief Risk and Compliance Officer.",
    "Example Co has appointed a Chief Legal and People Officer.",
    "Example Co has appointed a Chief Financial and Operating Officer.",
])
def test_chief_x_and_y_officer_non_buyer_rejected(desc):
    assert S.relevant_role(desc) is None


@pytest.mark.parametrize("desc", [
    "Example Retail has hired a Head of E-commerce Product.",
    "Example Co hired a Head of Mobile-first Product.",
    "Example Co hired a Director of Cross-platform Engineering.",
])
def test_hyphenated_free_word(desc):
    assert S.relevant_role(desc)


# ------------------------------------------------------------ heldout3 regression (judge rows excluded)
def test_heldout3_not_below_reviewer_round3_numbers(config):
    precision, recall, misses = _heldout_metrics(config, "signals_heldout3.json")
    # the reviewer measured precision 86.8% / recall 96.7% (after two label corrections) on the old code
    assert precision > 0.868 and recall >= 0.967, misses


def test_heldout3_shape_and_label_corrections():
    rows = {r["id"]: r for r in load_fixture("signals_heldout3.json")["rows"]}
    assert len(rows) == 170
    assert rows["T169"]["expect"] == "drop" and rows["T170"]["expect"] == "drop"


def test_round_alone_is_not_money_received(config):
    # company name differs from the investor's, so only the 'received' word list can save it; 'round' must not
    v = S.filter_scoops([make_scoop(desc="Fictional Capital participated in a $15 million round for Other Co.",
                                    name="Gamma Apps Pty Ltd")], config, TODAY)[0]
    assert not v.keep and "investor note" in v.reason


@pytest.mark.parametrize("desc", [
    "Example Co has appointed a Chief Strategy and Growth Officer.",
    "Example Co has appointed a Chief Operating and Commercial Officer.",
])
def test_chief_x_and_y_officer_needs_a_buyer_word(desc):
    assert S.relevant_role(desc) is None
