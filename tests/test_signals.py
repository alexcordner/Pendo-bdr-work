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
    assert S.signal_strength(SignalKind.FUNDING, 45) == 0.1359         # 0.6 * 0.5 ** (45/21)
    assert S.signal_strength(SignalKind.PARTNERSHIP, 21) == 0.15
    assert S.signal_strength(SignalKind.INTENT, 0) == 0.3


def test_per_kind_half_lives():
    assert S.half_life_for(SignalKind.EXEC_MOVE) == 45.0 and S.half_life_for(SignalKind.NEW_HIRE) == 45.0
    for k in (SignalKind.FUNDING, SignalKind.MA, SignalKind.PRODUCT_LAUNCH, SignalKind.PARTNERSHIP,
              SignalKind.PROJECT_PAIN, SignalKind.HIRING_PLAN, SignalKind.INTENT):
        assert S.half_life_for(k) == 21.0


def test_old_exec_move_outranks_newer_partnership():
    # 40-day-old exec move (0.9 * 0.5**(40/45) = 0.486) beats a 10-day-old partnership (0.3 * 0.5**(10/21) = 0.216)
    assert S.signal_strength(SignalKind.EXEC_MOVE, 40) == 0.486
    assert S.signal_strength(SignalKind.PARTNERSHIP, 10) == 0.2157


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
    assert len(sig.evidence) == S.EVIDENCE_MAX_CHARS and "… (" in sig.evidence       # cut text, then the partnership note
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
    return Signal("A", kind, strength, src, datetime(2026, 11, day, tzinfo=timezone.utc), f"scoop text {src} {kind.value}", "Z")


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
    kinds = [SignalKind.FUNDING, SignalKind.MA, SignalKind.PRODUCT_LAUNCH, SignalKind.NEW_HIRE]
    f = lambda *xs: S.combined_strength([_sig(kinds[i], x, src=f"s{i}") for i, x in enumerate(xs)])
    assert S.COMBINE_DAMPING == 0.5 and S.SAME_KIND_EXTRA_EACH == 0.15 and S.SAME_KIND_EXTRA_TOTAL == 0.30
    assert S.MAX_COMBINED_SIGNALS == 4
    assert f(0.5, 0.4) == 0.6                # different kinds: 1 - 0.5 * (1 - 0.5*0.4)
    assert f(0.4, 0.5) == 0.6
    assert f(0.5, 0.4, 0.2) == 0.64          # 1 - 0.5 * 0.8 * 0.9
    assert f(0.5, 0.5) == 0.625


def test_combined_single_equals_strength():
    assert S.combined_strength([_sig(SignalKind.FUNDING, 0.6)]) == 0.6


def test_combined_at_least_best_and_capped():
    sigs = [_sig(SignalKind.FUNDING, 0.6), _sig(SignalKind.EXEC_MOVE, 0.9), _sig(SignalKind.MA, 0.5)]
    c = S.combined_strength(sigs)
    assert 0.9 <= c <= 1.0


def test_combined_adding_signals_never_lowers_and_stops_after_four():
    kinds = [SignalKind.FUNDING, SignalKind.MA, SignalKind.PRODUCT_LAUNCH, SignalKind.NEW_HIRE, SignalKind.HIRING_PLAN]
    sigs, vals = [], []
    for i, k in enumerate(kinds):
        sigs.append(_sig(k, 0.5 - i * 0.05, src=f"s{i}"))
        vals.append(S.combined_strength(sigs))
    assert vals[0] < vals[1] < vals[2] < vals[3] and vals[3] == vals[4] and vals[4] < 1.0


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
    return Signal("A", SignalKind.INTENT, strength, src, datetime(2026, 11, day, tzinfo=timezone.utc), f"interest text {src}", "Z")


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
    assert S.combined_strength([_isig(0.3), _isig(0.2, src="b")]) == 0.37      # same kind: extra = min(0.5 * 0.2, 0.15)


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
    ("Partnership", "Gamma partnered with a vendor on a digital service.", (), 30),
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
    assert S.signal_strength(SignalKind.PARTNERSHIP, 30) == 0.1114
    assert S.signal_strength(SignalKind.HIRING_PLAN, 30) == 0.2043


def test_all_default_kept_ages_stay_above_the_floor():
    for kind, limit in S.KIND_MAX_AGE_DAYS.items():
        assert S.signal_strength(kind, limit) >= S.MIN_STRENGTH_FLOOR, kind


def test_below_floor_dropped_with_reason(config, monkeypatch):
    monkeypatch.setattr(S, "MIN_STRENGTH_FLOOR", 0.45)
    assert judge(config, days=5).keep                       # 0.6 * 0.5 ** (5/21) = 0.5087
    v = judge(config, days=15)                              # 0.3657 < 0.45
    assert not v.keep and "faded below floor" in v.reason and "0.37" in v.reason


def test_floor_boundary_is_inclusive(config, monkeypatch):
    monkeypatch.setattr(S, "MIN_STRENGTH_FLOOR", 0.3657)
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


# ============================================================ phase 2 dry-run fixes (fix-triggers)
def one(config, typ, desc, name="Example Co", days=5, topics=(), cid="FAKE-ZI-2001"):
    sc = Scoop("P-1", cid, name, desc, TODAY - timedelta(days=days), (typ,), tuple(topics))
    return S.filter_scoops([sc], config, TODAY)[0]


def sig_for(config, v, accounts):
    by_zi = {a.zoominfo_id: a for a in accounts if a.zoominfo_id}
    return S.scoops_to_signals([v], by_zi, config, TODAY).signals[0]


# ------------------------------------------------------------ weights table
def test_weights_table_literal():
    assert S.KIND_BASE_WEIGHT == {
        SignalKind.EXEC_MOVE: 0.90, SignalKind.PRODUCT_LAUNCH: 0.80, SignalKind.PROJECT_PAIN: 0.80,
        SignalKind.FUNDING: 0.60, SignalKind.NEW_HIRE: 0.65, SignalKind.MA: 0.50, SignalKind.HIRING_PLAN: 0.55,
        SignalKind.PARTNERSHIP: 0.30, SignalKind.INTENT: 0.30, SignalKind.NONE: 0.0}
    assert (S.LAUNCH_NON_DIGITAL_WEIGHT, S.MA_MINORITY_OR_UNRELATED_WEIGHT, S.MA_UNCLEAR_WEIGHT,
            S.PARTNERSHIP_NO_PRODUCT_WEIGHT) == (0.40, 0.25, 0.25, 0.20)


# ------------------------------------------------------------ (1) product launch relevance
def test_lab_consumables_launch_is_half_weight_with_visible_note(config, accounts):
    v = one(config, "Product Launch", "Acme Labs launched a new range of lab consumables.", name="Acme Labs Pty Ltd")
    assert v.keep and v.factor == 0.5 and v.notes == (S.LAUNCH_NOTE,)
    assert v.reason == "Kept: Product Launch, physical or unclear product launch: check it is digital, 5 days ago"
    sig = sig_for(config, v, accounts)
    assert sig.strength == S._strength(SignalKind.PRODUCT_LAUNCH, 5, False, 0.5)
    assert sig.strength == 0.3392                            # round(0.8 * 0.5 ** (5/21), 4) * 0.5
    assert S.signal_notes(sig) == [S.LAUNCH_NOTE] and sig.evidence.endswith(" (" + S.LAUNCH_NOTE + ")")


@pytest.mark.parametrize("desc", [
    "Example Co launched a new mobile banking app.", "Example Co launched a customer portal.",
    "Example Co launched its SaaS platform.", "Example Co launched an e-commerce website.",
    "Example Co launched an AI assistant.", "Example Co launched a cloud API.",
    "Example Co launched a new online store.", "Example Co launched a customer experience programme.",
])
def test_digital_product_launch_keeps_full_weight(config, desc):
    v = one(config, "Product Launch", desc)
    assert v.keep and v.factor == 1.0 and v.notes == () and v.reason == "Kept: Product Launch, 5 days ago"


@pytest.mark.parametrize("desc", [
    "Example Co launched a new range of cleaning products.", "Example Co launched a frozen-meals line.",
    "Example Co launched a new insulin pen.",
])
def test_physical_or_unclear_launches_discounted(config, desc):
    assert one(config, "Product Launch", desc).factor == 0.5


def test_launch_words_are_whole_words(config):
    assert one(config, "Product Launch", "Example Co launched a new apple juice.").factor == 0.5    # 'app' != 'apple'


def test_physical_launch_ranks_below_digital_launch_same_age(config, accounts):
    a = sig_for(config, one(config, "Product Launch", "Example Co launched a new mobile app."), accounts)
    b = sig_for(config, one(config, "Product Launch", "Example Co launched lab consumables."), accounts)
    assert a.strength == 0.6783 and b.strength == 0.3392


# ------------------------------------------------------------ (2) M&A direction
@pytest.mark.parametrize("desc,name,role", [
    ("Pickles Auctions to be acquired by EQT.", "Pickles Auctions", "target"),
    ("Pickles Auctions has agreed to be acquired by EQT.", "Pickles Auctions", "target"),
    ("Pickles Auctions was acquired by EQT.", "Pickles Auctions", "target"),
    ("Pickles Auctions has been bought by EQT.", "Pickles Auctions", "target"),
    ("Pickles Auctions is to merge with Other Co.", "Pickles Auctions", "target"),
    ("Pickles Auctions agreed a merger with Other Co.", "Pickles Auctions", "target"),
    ("Pickles Auctions sold to Other Co.", "Pickles Auctions", "target"),
    ("Pickles Auctions faces a takeover bid from Other Co.", "Pickles Auctions", "target"),
    ("Pickles to be acquired by EQT.", "EQT Group", "acquirer"),
    ("Goodman Group to acquire a 9.9% stake in NEXTDC.", "Goodman Group", "acquirer"),
    ("Goodman Group to acquire a 9.9% stake in NEXTDC.", "NEXTDC Ltd", "target"),
    ("Goodman Group acquires Tiny Analytics.", "Goodman Group", "acquirer"),
    ("Goodman Group acquires Tiny Analytics.", "Tiny Analytics Pty", "target"),
    ("Goodman Group launches a takeover bid for Tiny Analytics.", "Tiny Analytics", "target"),
    ("Goodman Group launches a takeover bid for Tiny Analytics.", "Goodman Group", "acquirer"),
    ("Takeover of Tiny Analytics by Goodman Group.", "Tiny Analytics", "target"),
    ("Goodman Group took a stake in Tiny Analytics.", "Goodman Group", "acquirer"),
    ("A big deal was announced.", "Goodman Group", "unclear"),
    ("Someone is acquiring someone.", "Goodman Group", "unclear"),
])
def test_ma_role(desc, name, role):
    assert S.ma_role(desc, name) == role


def test_ma_target_weight_and_reason(config, accounts):
    v = one(config, "Mergers & Acquisitions (M&A)", "Pickles Auctions to be acquired by EQT.", name="Pickles Auctions")
    assert v.keep and v.factor == 1.0 and v.notes == (S.MA_TARGET_NOTE,)
    assert v.reason == "Kept: Mergers & Acquisitions (M&A), being acquired: leadership/platform change likely, 5 days ago"
    assert sig_for(config, v, accounts).strength == 0.4239          # 0.5 * 0.5 ** (5/21)


@pytest.mark.parametrize("desc,weight,note", [
    ("Gamma Group to acquire a 9.9% stake in NEXTDC.", 0.25, S.MA_MINORITY_NOTE),
    ("Gamma Group to take a stake in Other Co.", 0.25, S.MA_MINORITY_NOTE),
    ("Gamma Group to acquire a minority interest in Other Co.", 0.25, S.MA_MINORITY_NOTE),
    ("Gamma Group to acquire a 30% stake in a software company.", 0.25, S.MA_MINORITY_NOTE),
    ("Gamma Group to acquire a majority stake in an energy company.", 0.25, S.MA_MINORITY_NOTE),
    ("Gamma Group to acquire an energy company.", 0.25, S.MA_MINORITY_NOTE),
    ("Gamma Group to acquire a gold mine.", 0.25, S.MA_MINORITY_NOTE),
    ("Gamma Group to acquire a 60% stake in a software company.", 0.5, S.MA_ACQUIRER_FULL_NOTE),
    ("Gamma Group to acquire Tiny Analytics, a data platform business.", 0.5, S.MA_ACQUIRER_FULL_NOTE),
    ("Gamma Group to acquire a customer experience software firm.", 0.5, S.MA_ACQUIRER_FULL_NOTE),
    ("Gamma Group to acquire Tiny Co outright.", 0.5, S.MA_ACQUIRER_FULL_NOTE),
    ("Gamma Group acquires Tiny Co.", 0.5, S.MA_ACQUIRER_FULL_NOTE),
])
def test_ma_acquirer_variants(desc, weight, note):
    role, w, n = S.classify_ma(desc, "Gamma Group")
    assert (role, w, n) == ("acquirer", weight, note)


def test_ma_minority_acquirer_evidence_note_and_strength(config, accounts):
    v = one(config, "Mergers & Acquisitions (M&A)", "Goodman Group to acquire a 9.9% stake in NEXTDC.", name="Goodman Group")
    assert v.factor == 0.5 and v.notes == (S.MA_MINORITY_NOTE,) and S.MA_MINORITY_NOTE in v.reason
    sig = sig_for(config, v, accounts)
    assert sig.strength == 0.2119                                   # round(0.5 * 0.5 ** (5/21), 4) * 0.5
    assert S.signal_notes(sig) == [S.MA_MINORITY_NOTE]


def test_ma_unclear_direction_is_quarter_weight(config):
    v = one(config, "Mergers & Acquisitions (M&A)", "A large deal was announced.", name="Goodman Group")
    assert v.keep and v.factor == 0.5 and v.notes == (S.MA_UNCLEAR_NOTE,)


def test_funding_typed_acquisition_is_reclassified_as_ma_target(config, accounts):
    v = one(config, "Funding", "Pickles Auctions to be acquired by EQT.", name="Pickles Auctions")
    assert v.keep and v.kind is SignalKind.MA and v.factor == 1.0
    assert v.reason.startswith("Kept: Funding (reclassified as M&A: company is being acquired), being acquired")
    assert sig_for(config, v, accounts).kind is SignalKind.MA


@pytest.mark.parametrize("desc", [
    "Pickles Auctions has been acquired by EQT.", "Pickles Auctions acquired by EQT in a $1.3bn deal.",
    "Pickles Auctions agreed to be taken over by EQT.",
])
def test_funding_typed_acquired_variants_become_ma(config, desc):
    assert one(config, "Funding", desc, name="Pickles Auctions").kind is SignalKind.MA


def test_funding_that_received_money_is_not_reclassified(config):
    v = one(config, "Funding", "Pickles Auctions raised $50 million, backed by an investor, and was acquired by nobody.",
            name="Pickles Auctions")
    assert v.kind is SignalKind.FUNDING


def test_funding_acquirer_wording_stays_funding_path(config):
    # 'took a stake in' on a Funding scoop is an investor note (dropped), not an M&A reclassification
    v = one(config, "Funding", "Example Super has taken a stake in Other Co's infrastructure arm.", name="Example Super")
    assert not v.keep and "investor note" in v.reason


def test_funding_acquirer_of_company_on_ma_type_unaffected(config):
    assert one(config, "Mergers & Acquisitions (M&A)", "Gamma Group acquires Tiny Co.", name="Gamma Group").kind is SignalKind.MA


# ------------------------------------------------------------ partnership
def test_partnership_weights(config, accounts):
    plain = one(config, "Partnership", "Beta Corp signs a sponsorship deal with Fin Co.", name="Beta Corp")
    assert plain.factor == pytest.approx(2 / 3) and plain.notes == (S.PARTNERSHIP_NOTE,)
    assert sig_for(config, plain, accounts).strength == 0.1696      # round(0.3 * 0.5 ** (5/21), 4) * 2/3
    rich = one(config, "Partnership", "Beta Corp partners with Fin Co to build a customer mobile app.", name="Beta Corp")
    assert rich.factor == 1.0 and rich.notes == ()
    assert sig_for(config, rich, accounts).strength == 0.2544       # 0.3 * 0.5 ** (5/21)


@pytest.mark.parametrize("desc", ["Beta Corp partnered with a vendor on a digital service.",
                                  "Beta Corp partnered with Fin Co on a product bundle.",
                                  "Beta Corp partnered with Fin Co to serve customers."])
def test_partnership_words(config, desc):
    assert one(config, "Partnership", desc).factor == 1.0


def test_ai_datacentre_deal_alone_does_not_count_as_product_words():
    assert not S._PARTNERSHIP_WORDS.search("AI data centre capacity cloud infrastructure deal")


# ------------------------------------------------------------ (4) same-deal pairs
def deal_pair(config, a_desc, b_desc, a_name="Goodman Group", b_name="NEXTDC Ltd"):
    sa = Scoop("D-1", "ZA", a_name, a_desc, TODAY - timedelta(days=5), ("Mergers & Acquisitions (M&A)",), ())
    sb = Scoop("D-2", "ZB", b_name, b_desc, TODAY - timedelta(days=5), ("Mergers & Acquisitions (M&A)",), ())
    return S.filter_scoops([sa, sb], config, TODAY)


def test_same_deal_both_kept_with_reason(config):
    d = "Goodman Group to acquire a 9.9% stake in NEXTDC."
    va, vb = deal_pair(config, d, d)
    assert va.keep and vb.keep
    assert va.same_deal_as == ("NEXTDC Ltd",) and vb.same_deal_as == ("Goodman Group",)
    assert va.reason.endswith("; same deal as NEXTDC Ltd") and vb.reason.endswith("; same deal as Goodman Group")
    assert (va.factor, vb.factor) == (0.5, 0.7)                     # buyer minority 0.25, target minority 0.35
    assert "same deal as NEXTDC Ltd" in va.notes


def test_same_deal_detected_when_texts_differ_but_name_each_other(config):
    va, vb = deal_pair(config, "Goodman Group to acquire 9.9% of NEXTDC.", "NEXTDC says Goodman Group will buy 9.9%.")
    assert va.same_deal_as and vb.same_deal_as


def test_unrelated_ma_scoops_are_not_the_same_deal(config):
    va, vb = deal_pair(config, "Goodman Group to acquire Tiny Analytics.", "NEXTDC Ltd to acquire Other Software Co.")
    assert va.same_deal_as == () and vb.same_deal_as == ()
    assert "same deal" not in va.reason


def test_same_company_twice_is_not_a_pair(config):
    sa = Scoop("D-1", "ZA", "Goodman Group", "Goodman Group to acquire a 9.9% stake in NEXTDC.", TODAY, ("Mergers & Acquisitions (M&A)",), ())
    sb = replace(sa, id="D-2")
    assert all(v.same_deal_as == () for v in S.filter_scoops([sa, sb], config, TODAY))


def test_same_deal_note_reaches_evidence_and_helper(config, accounts):
    d = "Goodman Group to acquire a 9.9% stake in NEXTDC."
    va, vb = deal_pair(config, d, d)
    by_zi = {"ZA": Account("SF-A", "Goodman"), "ZB": Account("SF-B", "NEXTDC")}
    sigs = S.scoops_to_signals([va, vb], by_zi, config, TODAY).signals
    notes = {s.sf_account_id: S.signal_notes(s) for s in sigs}
    assert notes["SF-A"] == [S.MA_MINORITY_NOTE, "same deal as NEXTDC Ltd"]
    assert notes["SF-B"] == [S.MA_TARGET_MINORITY_NOTE, "same deal as Goodman Group"]


def test_combined_strength_counts_one_deal_once_within_an_account():
    def ma(strength, src, text):
        return Signal("A", SignalKind.MA, strength, src, datetime(2026, 11, 20, tzinfo=timezone.utc), text, "Z")
    one_deal = [ma(0.4, "a", "Goodman Group to acquire a 9.9% stake in NEXTDC."),
                ma(0.3, "b", "Goodman Group takes 9.9% stake in NEXTDC, a data centre firm.")]
    assert S.combined_strength(one_deal) == 0.4
    assert S.combined_strength(one_deal[:1]) == 0.4
    two_deals = [ma(0.4, "a", "Goodman Group to acquire a 9.9% stake in NEXTDC."),
                 ma(0.3, "b", "Goodman Group to acquire Tiny Analytics for $20m.")]
    assert S.combined_strength(two_deals) == 0.49                   # 1 - 0.6 * (1 - 0.5 * 0.3)


def test_dedupe_deals_keeps_the_strongest_and_other_kinds():
    t = "Goodman Group to acquire a 9.9% stake in NEXTDC."
    ma = lambda st, src: Signal("A", SignalKind.MA, st, src, datetime(2026, 11, 20, tzinfo=timezone.utc), t, "Z")
    fund = _sig(SignalKind.FUNDING, 0.3)
    out = S.dedupe_deals([ma(0.2, "x"), ma(0.4, "y"), fund])
    assert sorted((s.kind.value, s.source) for s in out) == [("funding", "s"), ("ma", "y")]


def test_dedupe_ignores_note_suffixes():
    t = "Goodman Group to acquire a 9.9% stake in NEXTDC."
    a = Signal("A", SignalKind.MA, 0.4, "a", None, t + " (" + S.MA_MINORITY_NOTE + ")", "Z")
    b = Signal("A", SignalKind.MA, 0.3, "b", None, t + " (same deal as NEXTDC)", "Z")
    assert len(S.dedupe_deals([a, b])) == 1


# ------------------------------------------------------------ (5) exec moves: vacated roles and start dates
@pytest.mark.parametrize("desc", [
    "The CTO of Example Co has left to join another organisation.",
    "Example Co's Interim Chief Digital Officer is leaving.",
    "Example Co's Head of Product has resigned.",
    "Example Co's Chief Digital Officer steps down.",
])
def test_vacated_role_is_a_trigger_with_reason(config, desc):
    v = one(config, "Executive Move", desc)
    assert v.keep and "vacated, new leader likely" in v.reason


def test_vacated_reason_literal(config):
    v = one(config, "Executive Move", "Example Co's Interim Chief Digital Officer is leaving.")
    assert v.reason == "Kept: Executive Move, role 'Chief Digital Officer' vacated, new leader likely, 5 days ago"


def test_left_to_join_another_org_is_vacated(config):
    assert S.role_vacated("The CTO has left to join another organisation.")
    assert not S.role_vacated("Example Co appointed a new CTO. The old CTO has left.")   # an incoming leader is named
    assert S.role_vacated("Example Co's Head of Product has resigned.")


@pytest.mark.parametrize("desc", [
    "Example Co appointed a new Chief Digital Officer.",
    "Example Co hired a Head of Product to replace the one who left.",
    "Jordan joins Example Co as CTO after the previous CTO departed.",
])
def test_incoming_leader_not_marked_vacated(config, desc):
    assert not S.role_vacated(desc)
    assert "vacated" not in one(config, "Executive Move", desc).reason


def test_ceo_only_departure_still_dropped(config):
    assert not one(config, "Executive Move", "Example Co CEO steps down after eight years.").keep


@pytest.mark.parametrize("desc,expected", [
    ("Example Co appointed a new Chief Digital Officer, effective 1 December 2026.", "2026-12-01"),
    ("Example Co appointed a new Chief Digital Officer, effective 1 December.", "2026-12-01"),
    ("Jordan will join Example Co as CTO starting December 1, 2026.", "2026-12-01"),
    ("Jordan will start as Head of Product on 15 October.", "2026-10-15"),
    ("Jordan joins as Head of Product from 2026-12-01.", "2026-12-01"),
    ("Jordan commences as Head of Digital on the 3rd of March 2027.", None),
    ("Example Co announced on 3 November that it will hire.", None),
    ("Example Co appointed Jordan as CTO.", None),
])
def test_incoming_start_date(desc, expected):
    got = S.incoming_start_date(desc, datetime(2026, 11, 20, tzinfo=timezone.utc))
    assert (got.date().isoformat() if got else None) == expected


def test_start_date_next_year_inference():
    pub = datetime(2026, 12, 15, tzinfo=timezone.utc)
    assert S.incoming_start_date("Jordan joins as CTO, effective 1 February.", pub).date().isoformat() == "2027-02-01"
    assert S.incoming_start_date("Jordan joins as CTO, effective 1 December.", pub).date().isoformat() == "2026-12-01"


def test_invalid_start_date_ignored():
    assert S.incoming_start_date("Jordan joins as CTO, effective 31 February.", datetime(2026, 11, 20, tzinfo=timezone.utc)) is None


def test_past_start_date_sets_age_and_source_date(config, accounts):
    v = one(config, "Executive Move", "Example Co appointed a new Chief Digital Officer, effective 15 October.", days=60)
    assert v.keep and v.event_date == datetime(2026, 10, 15, tzinfo=timezone.utc)
    assert v.reason == "Kept: Executive Move, role 'Chief Digital Officer', incoming leader starts 2026-10-15, started 41 days ago"
    sig = sig_for(config, v, accounts)
    assert sig.source_date == datetime(2026, 10, 15, tzinfo=timezone.utc)
    assert sig.strength == 0.4786                                   # 0.9 * 0.5 ** (41/45)


def test_future_start_date_is_age_zero_and_source_date_stays_published(config, accounts):
    v = one(config, "Executive Move", "Example Co appointed a new Chief Digital Officer, effective 1 December.", days=10)
    assert v.keep and v.reason == "Kept: Executive Move, role 'Chief Digital Officer', incoming leader starts 2026-12-01"
    sig = sig_for(config, v, accounts)
    assert sig.strength == 0.9 and sig.source_date == TODAY - timedelta(days=10)


def test_start_date_rescues_an_old_scoop_from_the_age_limit(config):
    old = one(config, "Executive Move", "Example Co appointed a new Chief Digital Officer, effective 1 November.", days=100)
    assert old.keep and "started 24 days ago" in old.reason
    assert not one(config, "Executive Move", "Example Co appointed a new Chief Digital Officer.", days=100).keep


def test_start_date_does_not_apply_to_other_kinds(config):
    assert one(config, "Funding", "Example Co raised $5 million, effective 1 November.", days=5).event_date is None


# ------------------------------------------------------------ signal notes helper
def test_signal_notes_and_is_ambiguous_with_several_notes():
    ev = "Text (food plant) (" + S.AMBIGUOUS_NOTE + ") (same deal as Other Co)"
    sig = Signal("A", SignalKind.MA, 0.2, "s", None, ev, "Z")
    assert S.signal_notes(sig) == [S.AMBIGUOUS_NOTE, "same deal as Other Co"] and S.is_ambiguous(sig)


def test_signal_notes_leave_plain_parentheses_alone():
    sig = Signal("A", SignalKind.MA, 0.2, "s", None, "Director of Support Services (aged care)", "Z")
    assert S.signal_notes(sig) == [] and not S.is_ambiguous(sig)


def test_evidence_with_notes_fits_the_limit(config, accounts):
    long = "Goodman Group to acquire a 9.9% stake in NEXTDC. " + "More. " * 80
    v = one(config, "Mergers & Acquisitions (M&A)", long, name="Goodman Group")
    sig = sig_for(config, v, accounts)
    assert len(sig.evidence) <= S.EVIDENCE_MAX_CHARS and S.signal_notes(sig) == [S.MA_MINORITY_NOTE]


# ------------------------------------------------------------ the dry-run examples end to end through the radar
def test_dry_run_examples_through_the_radar(config):
    accts = [Account("SF-1", "Acme Labs", zoominfo_id="Z1"), Account("SF-2", "Pickles Auctions", zoominfo_id="Z2"),
             Account("SF-3", "Goodman Group", zoominfo_id="Z3"), Account("SF-4", "NEXTDC Ltd", zoominfo_id="Z4"),
             Account("SF-5", "Beta Corp", zoominfo_id="Z5")]
    d = "Goodman Group to acquire a 9.9% stake in NEXTDC."
    scoops = [
        Scoop("1", "Z1", "Acme Labs", "Acme Labs launched a new range of lab consumables.", TODAY - timedelta(days=3), ("Product Launch",), ()),
        Scoop("2", "Z2", "Pickles Auctions", "Pickles Auctions to be acquired by EQT.", TODAY - timedelta(days=3), ("Funding",), ()),
        Scoop("3", "Z3", "Goodman Group", d, TODAY - timedelta(days=3), ("Mergers & Acquisitions (M&A)",), ()),
        Scoop("4", "Z4", "NEXTDC Ltd", d, TODAY - timedelta(days=3), ("Mergers & Acquisitions (M&A)",), ()),
        Scoop("5", "Z5", "Beta Corp", "Beta Corp signs a sponsorship deal with Fin Co.", TODAY - timedelta(days=3), ("Partnership",), ()),
    ]
    out = {e.account.name: e for e in S.trigger_radar(scoops, accts, config, TODAY, top_n=None)}
    assert out["Pickles Auctions"].signal.kind is SignalKind.MA                      # was scored as Funding
    assert out["Pickles Auctions"].signal.strength == 0.4529                         # 0.5 * 0.5 ** (3/21)
    assert out["NEXTDC Ltd"].signal.strength == 0.317                                # the target of a 9.9% stake: 0.4529 * 0.7
    assert out["Goodman Group"].signal.strength == 0.2265                            # the minority buyer (0.4529 * 0.5)
    assert out["Acme Labs"].signal.strength == 0.3623                                # 0.8 * 0.5 ** (3/21) * 0.5
    assert out["Beta Corp"].signal.strength == 0.1811                                # round(0.3 * 0.5 ** (3/21), 4) * 2/3
    ranked = [e.account.name for e in S.trigger_radar(scoops, accts, config, TODAY, top_n=None)]
    assert ranked.index("Pickles Auctions") < ranked.index("Goodman Group") and ranked[-1] == "Beta Corp"


# ------------------------------------------------------------ extra pins for the M&A and same-deal rules
def test_ma_role_passive_wording_without_the_company_is_unclear():
    assert S.ma_role("Other Co to be acquired by EQT.", "Goodman Group") == "unclear"


def test_stake_wording_without_a_percentage():
    assert S.classify_ma("Gamma Group to take a stake in a software company.", "Gamma Group")[1] == 0.25
    assert S.classify_ma("Gamma Group to acquire a majority stake in Other Co.", "Gamma Group")[1] == 0.25
    assert S.classify_ma("Gamma Group to acquire a majority stake in a software company.", "Gamma Group")[1] == 0.5
    assert S.classify_ma("Gamma Group to acquire a controlling stake in a data business.", "Gamma Group")[1] == 0.5


def test_same_deal_when_each_names_the_other_even_if_texts_differ_a_lot(config):
    va, vb = deal_pair(
        config, "Goodman Group to acquire 9.9% of NEXTDC, Reuters reported on Monday in Sydney.",
        "NEXTDC said Goodman Group is buying in, Bloomberg said Tuesday in Melbourne.")
    assert not S._same_deal_text(va.scoop.description, vb.scoop.description)
    assert va.same_deal_as and vb.same_deal_as


def test_one_sided_mention_with_unrelated_texts_is_not_the_same_deal(config):
    va, vb = deal_pair(config, "Goodman Group to acquire Tiny Co.", "Tiny Co Ltd to acquire Other Software Firm.",
                       a_name="Goodman Group", b_name="Tiny Co Ltd")
    assert va.same_deal_as == () and vb.same_deal_as == ()


def test_same_deal_text_thresholds():
    assert not S._same_deal_text("Alpha Bravo Charlie Delta Echo went ahead.", "Alpha Bravo Xray Yankee Zulu went ahead.")
    assert not S._same_deal_text("Nextdc Reuters said.", "Nextdc said.")            # one shared token is not enough
    assert S._same_deal_text("Goodman Group buys 9.9% of NEXTDC.", "Goodman Group acquires a 9.9% stake in NEXTDC.")


# ============================================================ phase 2 addendum: debt, funds, promotions, rumours
# ------------------------------------------------------------ (a) debt funding
@pytest.mark.parametrize("desc", [
    "AirTrunk has secured $2.99 billion in Debt funding. Investors included Banco Santander.",
    "Example Co secured a $50 million credit facility.", "Example Co closed a $20 million term loan.",
    "Example Co issued $100 million of senior notes.", "Example Co raised $200 million in bonds.",
    "Example Co secured a revolving credit line.", "Example Co completed a debt refinancing.",
])
def test_debt_funding_is_quarter_weight(config, accounts, desc):
    v = one(config, "Funding", desc, name="AirTrunk")
    assert v.keep and v.kind is SignalKind.FUNDING and v.notes == (S.DEBT_NOTE,)
    assert v.reason == "Kept: Funding, debt facility, not an equity round, 5 days ago"
    assert sig_for(config, v, accounts).strength == 0.212                 # 0.25 * 0.5 ** (5/21)


@pytest.mark.parametrize("desc", [
    "Example Co secured $5 million in funding.", "Example Co raised $20 million in a Series B round.",
    "Example Co closed a seed round.", "Example Co raised $30 million of venture capital.",
    "Example Co secured a $50 million credit facility and Series A equity.",
    "Example Co raised $40 million in growth equity.",
])
def test_equity_rounds_keep_full_funding_weight(config, accounts, desc):
    v = one(config, "Funding", desc)
    assert v.keep and v.notes == () and v.factor == 1.0
    assert sig_for(config, v, accounts).strength == 0.5087                # 0.6 * 0.5 ** (5/21)


def test_debt_funding_fades_below_the_floor_after_27_days(config):
    assert judge(config, desc="Example Co secured a $50 million credit facility.", days=27).keep
    v = judge(config, desc="Example Co secured a $50 million credit facility.", days=28)
    assert not v.keep and "faded below floor" in v.reason


def test_debt_wording_is_whole_word(config):
    assert one(config, "Funding", "Example Co raised $10 million from a loanee network.").factor == 1.0
    assert one(config, "Funding", "Example Co secured funding for its notesbook app.").factor == 1.0


# ------------------------------------------------------------ (b) fund / property launches
@pytest.mark.parametrize("desc", [
    "Charter Hall has announced the launch of the Charter Hall Convenience Retail Fund.",
    "Example Co launches a new REIT.", "Example Co launched a listed property trust.",
    "Example Co launched a new ETF.", "Example Co launched a new mortgage product.",
    "Example Co launched a home loan for first buyers.", "Example Co launched a new insurance policy.",
    "Example Co launches a managed portfolio.",
])
def test_financial_or_property_launch_is_discounted(config, accounts, desc):
    v = one(config, "Product Launch", desc)
    assert v.keep and v.notes == (S.FINANCIAL_LAUNCH_NOTE,) and v.factor == 0.25
    assert v.reason == "Kept: Product Launch, financial or property product launch, not a digital product, 5 days ago"
    assert sig_for(config, v, accounts).strength == 0.1696                # round(0.8 * 0.5 ** (5/21), 4) * 0.25


@pytest.mark.parametrize("desc", [
    "Example Bank launched a new banking app for its funds customers.",
    "Example Insurer launches a digital insurance platform.",
    "Example Super launched a member portal for its fund.",
    "Example Co launched an online mortgage tool.",
])
def test_bank_or_fund_launching_a_digital_product_stays_full_weight(config, desc):
    v = one(config, "Product Launch", desc)
    assert v.factor == 1.0 and v.notes == ()


def test_physical_launch_without_financial_words_stays_at_0_40(config):
    assert one(config, "Product Launch", "Example Co launched lab consumables.").factor == 0.5


# ------------------------------------------------------------ (c) digital launches named in the dry run
@pytest.mark.parametrize("desc", [
    "Ai-Media launch of Lexi Access, a new solution targeting higher education.",
    "Atturra SignalAI, an AI-powered product.", "Atturra launched SignalAI.",
    "Example Co launched a new workplace technology.",
])
def test_named_dry_run_digital_launches_keep_full_weight(config, desc):
    v = one(config, "Product Launch", desc)
    assert v.factor == 1.0 and v.notes == ()


def test_camel_case_ai_is_case_sensitive():
    assert S._CAMEL_AI.search("SignalAI") and not S._CAMEL_AI.search("Maintain the rail") and not S._CAMEL_AI.search("Saia")


# ------------------------------------------------------------ (d) C-suite promotions
@pytest.mark.parametrize("desc", [
    "One New Zealand Group has promoted from Chief Financial Officer to the role of CEO.",
    "Example Co's Jane promoted from Chief Operating Officer to CEO.",
    "Example Co's Jane moves from Chief Financial Officer to Managing Director.",
    "Example Co's Jane has moved from the role of CFO to the role of CEO.",
])
def test_csuite_promotion_without_a_buyer_role_dropped(config, desc):
    v = one(config, "Executive Move", desc, days=86)
    assert not v.keep and v.reason.startswith("Dropped: C-suite move from") and "buyer role" in v.reason


def test_csuite_promotion_reason_literal(config):
    v = one(config, "Executive Move", "One New Zealand Group promoted from Chief Financial Officer to the role of CEO.", days=86)
    assert v.reason == ("Dropped: C-suite move from 'Chief Financial Officer' to 'CEO': "
                        "neither the new nor the vacated role is a buyer role")


def test_promotion_into_a_buyer_role_counts(config):
    v = one(config, "Executive Move", "Example Co's Sam moved from Head of Marketing to Chief Digital Officer.")
    assert v.keep and v.reason == "Kept: Executive Move, new role 'Chief Digital Officer', 5 days ago"


def test_promotion_out_of_a_buyer_role_vacates_it(config):
    v = one(config, "Executive Move", "Example Co's Jane moves from Chief Technology Officer to CEO.")
    assert v.keep and v.reason == "Kept: Executive Move, vacated role 'Chief Technology Officer', new leader likely, 5 days ago"


def test_promotion_with_start_date(config):
    v = one(config, "Executive Move",
            "Example Co's Sam promoted from Head of Marketing to Chief Digital Officer, effective 1 November.", days=30)
    assert v.keep and v.event_date == datetime(2026, 11, 1, tzinfo=timezone.utc) and "started 24 days ago" in v.reason


def test_role_inside_a_promotion_sentence_is_not_taken_from_the_other_text(config):
    # the company name contains 'Digital' but neither role is a buyer role
    v = one(config, "Executive Move", "Example Digital Group promoted Jane from Chief Financial Officer to CEO.")
    assert not v.keep


# ------------------------------------------------------------ (e) rumoured acquisitions
def test_rumoured_acquirer_is_half_the_acquirer_weight(config, accounts):
    v = one(config, "Mergers & Acquisitions (M&A)", "SG Fleet is reportedly in talks to acquire FleetPartners.", name="SG Fleet")
    assert v.keep and v.factor == 0.5 and S.RUMOUR_NOTE in v.notes
    assert v.reason == ("Kept: Mergers & Acquisitions (M&A), acquirer of a digital business or a full acquisition; "
                        "reported talks, not a confirmed deal, 5 days ago")
    assert sig_for(config, v, accounts).strength == 0.2119                # 0.25 * 0.5 ** (5/21)


def test_confirmed_acquirer_is_not_halved(config):
    v = one(config, "Mergers & Acquisitions (M&A)", "SG Fleet agreed to acquire FleetPartners.", name="SG Fleet")
    assert v.factor == 1.0 and v.notes == ()


def test_rumoured_minority_acquirer_stacks(config):
    v = one(config, "Mergers & Acquisitions (M&A)", "Gamma Group is reportedly in talks to take a 9.9% stake in NEXTDC.", name="Gamma Group")
    assert v.factor == 0.25 and v.notes == (S.MA_MINORITY_NOTE, S.RUMOUR_NOTE)


@pytest.mark.parametrize("word", ["reportedly", "in talks", "rumoured", "understood to", "considering", "exploring"])
def test_rumour_words(config, word):
    d = f"SG Fleet is {word} to acquire FleetPartners." if word != "in talks" else "SG Fleet is in talks to acquire FleetPartners."
    assert one(config, "Mergers & Acquisitions (M&A)", d, name="SG Fleet").factor == 0.5


def test_rumoured_target_keeps_target_weight(config):
    v = one(config, "Mergers & Acquisitions (M&A)", "SG Fleet is reportedly in talks to acquire FleetPartners.", name="FleetPartners Group")
    assert v.factor == 1.0 and v.notes == (S.MA_TARGET_NOTE,)   # documented: the rumour discount applies to the acquirer only


# ------------------------------------------------------------ (f) FleetPartners / SG Fleet and Goodman / NEXTDC pairs
def test_fleet_pair_is_the_same_deal(config):
    d = "SG Fleet is reportedly in talks to acquire FleetPartners."
    sa = Scoop("F-1", "ZS", "SG Fleet Group", d, TODAY - timedelta(days=5), ("Mergers & Acquisitions (M&A)",), ())
    sb = Scoop("F-2", "ZF", "FleetPartners Group", d, TODAY - timedelta(days=5), ("Mergers & Acquisitions (M&A)",), ())
    va, vb = S.filter_scoops([sa, sb], config, TODAY)
    assert va.same_deal_as == ("FleetPartners Group",) and vb.same_deal_as == ("SG Fleet Group",)
    assert va.reason.endswith("; same deal as FleetPartners Group") and vb.reason.endswith("; same deal as SG Fleet Group")
    assert (va.factor, vb.factor) == (0.5, 1.0)


def test_fleet_pair_with_different_wording(config):
    sa = Scoop("F-1", "ZS", "SG Fleet Group", "SG Fleet is reportedly in talks to acquire FleetPartners.", TODAY - timedelta(days=5),
               ("Mergers & Acquisitions (M&A)",), ())
    sb = Scoop("F-2", "ZF", "FleetPartners Group", "FleetPartners has received an approach from SG Fleet.", TODAY - timedelta(days=5),
               ("Mergers & Acquisitions (M&A)",), ())
    va, vb = S.filter_scoops([sa, sb], config, TODAY)
    assert va.same_deal_as and vb.same_deal_as


def test_promotion_between_two_buyer_roles_reports_the_new_role(config):
    v = one(config, "Executive Move", "Example Co's Jo moves from Chief Technology Officer to Chief Digital Officer.")
    assert v.keep and v.reason == "Kept: Executive Move, new role 'Chief Digital Officer', 5 days ago"


def test_signal_notes_for_debt_fund_and_rumour(config, accounts):
    d = sig_for(config, one(config, "Funding", "Example Co secured a $50 million credit facility."), accounts)
    f = sig_for(config, one(config, "Product Launch", "Example Co launches a new property trust."), accounts)
    r = sig_for(config, one(config, "Mergers & Acquisitions (M&A)", "SG Fleet is reportedly in talks to acquire FleetPartners.", name="SG Fleet"), accounts)
    assert S.signal_notes(d) == [S.DEBT_NOTE] and S.signal_notes(f) == [S.FINANCIAL_LAUNCH_NOTE]
    assert S.signal_notes(r) == [S.RUMOUR_NOTE]


def test_solution_and_technology_count_as_digital(config):
    assert one(config, "Product Launch", "Example Co launched a new solution for universities.").factor == 1.0


# ============================================================ fix-triggers2: stacking, minority target, hardware launches
def L(strength, src, text, day=20, kind=SignalKind.PRODUCT_LAUNCH):
    return Signal("A", kind, strength, src, datetime(2026, 11, day, tzinfo=timezone.utc), text, "Z")


# ------------------------------------------------------------ (1) stacking
CATAPULT = [  # nine launch scoops for one fake company, strengths like the dry run (0.59, 0.59, 0.30, 0.27 ...)
    L(0.59, "c1", "Catapult launched Vision Cameras for elite teams."),
    L(0.59, "c2", "Catapult launched Vision Cameras for elite teams."),            # same scoop, repeated
    L(0.30, "c3", "Catapult unveils GPS Pods for field sport."),
    L(0.27, "c4", "Catapult introduces Heart Sensors for athletes."),
    L(0.25, "c5", "Catapult releases Edge Tracker for coaches."),
    L(0.22, "c6", "Catapult announced the launch of Clip Trainer."),
    L(0.20, "c7", "Catapult debuted Pro Sleeves for training."),
    L(0.18, "c8", "Catapult rolls out Lite Vest for juniors."),
    L(0.15, "c9", "Catapult launched Motion Band for rehab."),
]


def test_nine_launches_do_not_stack_towards_one():
    c = S.combined_strength(CATAPULT)
    # repeated scoop counts once; best 0.59; extras count min(0.5 * s, 0.15): 0.15, 0.135, then 0.015 (0.30 total)
    assert c == 0.7031
    assert 0.59 < c < 0.75


def test_nine_launches_old_formula_would_have_been_near_one():
    old = 1.0
    for x in sorted((s.strength for s in CATAPULT), reverse=True)[1:]:
        old *= 1 - 0.5 * x
    assert round(1 - 0.41 * old, 2) == 0.87                # what the previous formula gave for these nine
    assert S.combined_strength(CATAPULT) < 0.75


def test_same_kind_extras_capped_each_and_in_total():
    sigs = [L(0.6, f"s{i}", f"Company launched Product{i} Alpha{i}.") for i in range(3)]
    sigs = [L(0.6, "a", "Alpha Co launched Alpha Hub."), L(0.5, "b", "Beta Co launched Beta Hub."),
            L(0.1, "c", "Gamma Co launched Gamma Hub.")]
    # extras: min(0.5, 0.15) = 0.15, then min(0.1, 0.15, 0.30 - 0.15) = 0.1
    assert S.combined_strength(sigs) == round(1 - 0.4 * 0.85 * 0.95, 4)


def test_only_the_strongest_counted_entries_count():
    base = [L(0.6, "a", "Alpha Co launched Alpha Hub."), L(0.5, "b", "Beta Co launched Beta Hub."),
            L(0.45, "c", "Gamma Co launched Gamma Hub.")]
    more = base + [L(0.4, "d", "Delta Co launched Delta Hub."), L(0.35, "e", "Echo Co launched Echo Hub.")]
    assert S.combined_strength(more) == S.combined_strength(base) == round(1 - 0.4 * 0.85 * 0.85, 4)


def test_mixed_kinds_keep_the_damped_contribution():
    sigs = [_sig(SignalKind.EXEC_MOVE, 0.8, src="e"), _sig(SignalKind.PRODUCT_LAUNCH, 0.6, src="l"),
            _sig(SignalKind.FUNDING, 0.4, src="f")]
    assert S.combined_strength(sigs) == round(1 - 0.2 * (1 - 0.3) * (1 - 0.2), 4)          # 0.888


def test_mixed_kind_case_with_same_kind_repeats():
    sigs = [_sig(SignalKind.EXEC_MOVE, 0.8, src="e"), L(0.6, "l1", "Alpha Co launched Alpha Hub."),
            L(0.5, "l2", "Beta Co launched Beta Hub."), _sig(SignalKind.FUNDING, 0.4, src="f")]
    # leads: exec 0.8, launch 0.6 (x0.5), funding 0.4 (x0.5); extra: launch #2 at 0.15 -> all four count
    assert S.combined_strength(sigs) == round(1 - 0.2 * (1 - 0.3) * (1 - 0.2) * (1 - 0.15), 4)         # 0.9048


def test_one_signal_per_kind_is_unchanged_by_the_new_rule():
    sigs = [_sig(SignalKind.FUNDING, 0.5, src="a"), _sig(SignalKind.MA, 0.4, src="b")]
    assert S.combined_strength(sigs) == 0.6


def test_combined_always_at_least_best_and_at_most_one():
    for n in range(1, 8):
        sigs = [L(0.9 - i * 0.05, f"s{i}", f"Co{i} launched Thing{i} Pro.") for i in range(n)]
        c = S.combined_strength(sigs)
        assert 0.9 <= c <= 1.0


# near-identical scoops
def test_same_first_60_characters_count_once():
    a = L(0.5, "a", "Catapult launched the Vision camera for elite teams across Europe today.")
    b = L(0.4, "b", "Catapult launched the Vision camera for elite teams across Europe, said the firm in a statement.")
    assert S._normalised_start(a.evidence) == S._normalised_start(b.evidence)
    assert S.dedupe_signals([a, b]) == [a]
    assert S.combined_strength([a, b]) == 0.5


def test_normalisation_ignores_case_punctuation_and_spacing():
    a = L(0.5, "a", "Catapult launched the Vision camera for elite teams across Europe today.")
    b = L(0.4, "b", "CATAPULT   launched the Vision camera, for elite teams across Europe today!!")
    assert S.dedupe_signals([a, b]) == [a]


def test_same_product_name_within_14_days_counts_once():
    a = L(0.5, "a", "Catapult launched VisionPro for elite teams.", day=20)
    b = L(0.4, "b", "Catapult unveils VisionPro at a trade show.", day=6)            # 14 days earlier
    assert S._product_name(a.evidence) == S._product_name(b.evidence) == "visionpro"
    assert S.dedupe_signals([a, b]) == [a]


def test_same_product_name_more_than_14_days_apart_counts_twice():
    a = L(0.5, "a", "Catapult launched VisionPro for elite teams.", day=20)
    b = L(0.4, "b", "Catapult unveils VisionPro at a trade show.", day=5)             # 15 days earlier
    assert len(S.dedupe_signals([a, b])) == 2


def test_different_products_are_not_duplicates():
    a = L(0.5, "a", "Catapult launched VisionPro for elite teams.")
    b = L(0.4, "b", "Catapult launched ClipTrainer for coaches.")
    assert len(S.dedupe_signals([a, b])) == 2


def test_duplicates_must_be_the_same_kind():
    a = L(0.5, "a", "Catapult launched VisionPro for elite teams.")
    b = Signal("A", SignalKind.FUNDING, 0.4, "b", a.source_date, a.evidence, "Z")
    assert len(S.dedupe_signals([a, b])) == 2


def test_product_name_extraction():
    assert S._product_name("Catapult launches the new PerformanceHub 2 today.") == "performancehub"
    assert S._product_name("Catapult has secured funding.") is None
    assert S._product_name("Acme announced the launch of Lexi Access, a new solution.") == "lexi access"


def test_dedupe_strips_notes_before_comparing():
    t = "Catapult launched VisionPro cameras for elite teams across the world today."
    a = L(0.5, "a", t + " (" + S.LAUNCH_NOTE + ")")
    b = L(0.4, "b", t)
    assert len(S.dedupe_signals([a, b])) == 1


# ------------------------------------------------------------ (2) M&A direction: NEXTDC wording, 9.9% minority target
NEXTDC_TEXT = ("NEXTDC has entered into an agreement for Goodman and Digital Realty to acquire a 9.9% stake in the company.")


@pytest.mark.parametrize("name,role", [("NEXTDC Ltd", "target"), ("Goodman Group", "acquirer"),
                                       ("Digital Realty Trust", "acquirer")])
def test_agreement_for_x_to_acquire_stake_in_the_company(name, role):
    assert S.ma_role(NEXTDC_TEXT, name) == role


def test_nextdc_scoop_is_a_weak_target_not_an_acquirer(config, accounts):
    v = one(config, "Mergers & Acquisitions (M&A)", NEXTDC_TEXT, name="NEXTDC Ltd")
    assert v.keep and v.factor == pytest.approx(0.7) and v.notes == (S.MA_TARGET_MINORITY_NOTE,)
    assert v.reason == "Kept: Mergers & Acquisitions (M&A), minority stake purchase: weak trigger, 5 days ago"
    sig = sig_for(config, v, accounts)
    assert sig.strength == 0.2967 and S.signal_notes(sig) == [S.MA_TARGET_MINORITY_NOTE]       # round(0.4239 * 0.7, 4)
    assert "acquirer" not in v.reason


def test_goodman_side_of_the_same_wording_is_the_minority_acquirer(config):
    v = one(config, "Mergers & Acquisitions (M&A)", NEXTDC_TEXT, name="Goodman Group")
    assert v.factor == 0.5 and v.notes == (S.MA_MINORITY_NOTE,)


@pytest.mark.parametrize("name,role,weight", [
    ("Gamma Group", "acquirer", 0.25), ("Delta Systems", "target", 0.35)])
def test_x_has_agreed_to_acquire_a_stake_in_y(name, role, weight):
    d = "Gamma Group has agreed to acquire a 9.9% stake in Delta Systems."
    r, w, _ = S.classify_ma(d, name)
    assert (r, w) == (role, weight)


def test_majority_or_full_acquisition_target_keeps_half_weight():
    assert S.classify_ma("Gamma Group has agreed to acquire a 60% stake in Delta Systems.", "Delta Systems")[:2] == ("target", 0.5)
    assert S.classify_ma("Gamma Group has agreed to acquire a majority stake in Delta Systems.", "Delta Systems")[:2] == ("target", 0.5)
    assert S.classify_ma("Delta Systems to be acquired by Gamma Group.", "Delta Systems")[:2] == ("target", 0.5)
    assert S.classify_ma("Delta Systems agreed to be acquired by Gamma Group in a full takeover.", "Delta Systems")[:2] == ("target", 0.5)


@pytest.mark.parametrize("d", [
    "Gamma Group agreed to buy a minority interest in Delta Systems.",
    "Gamma Group took a 15% stake in Delta Systems.",
    "Gamma Group takes a stake in Delta Systems.",
])
def test_minority_stake_in_the_company_is_a_weak_target(d):
    assert S.classify_ma(d, "Delta Systems")[1] == 0.35


@pytest.mark.parametrize("d,name,role", [
    ("Delta Systems has signed a deal for Gamma Group to acquire Delta Systems.", "Gamma Group", "acquirer"),
    ("The board allows Gamma Group to acquire a stake in Delta Systems.", "Delta Systems", "target"),
    ("Delta Systems received an offer for Gamma Group to buy it.", "Delta Systems", "target"),
])
def test_other_third_party_constructions(d, name, role):
    assert S.ma_role(d, name) == role


def test_nextdc_goodman_pair_is_still_the_same_deal(config):
    va, vb = deal_pair(config, NEXTDC_TEXT, NEXTDC_TEXT, a_name="Goodman Group", b_name="NEXTDC Ltd")
    assert va.same_deal_as == ("NEXTDC Ltd",) and vb.same_deal_as == ("Goodman Group",)
    assert (va.factor, vb.factor) == (0.5, pytest.approx(0.7))


# ------------------------------------------------------------ (3) hardware launches beat platform / interface words
@pytest.mark.parametrize("desc", [
    "Example Co launched a scaled-up robotics platform with a new user interface.",
    "Example Co launched a new chromatography instrument with a cloud platform.",
    "Example Co launched high-resolution cameras with AI-powered analytics.",
    "Example Co launched emitters for digital sampling.",
    "Example Co launched a sensor platform for mobile fleets.",
    "Example Co launched a reagent kit for a laboratory assay platform.",
    "Example Co launched a new device with a digital display.",
    "Example Co launched powder handling equipment with a colour display.",
    "Example Co launched hardware for the cloud.",
])
def test_hardware_launch_is_non_digital_even_with_platform_or_interface_words(config, desc):
    v = one(config, "Product Launch", desc)
    assert v.factor == 0.5 and v.notes == (S.LAUNCH_NOTE,)


@pytest.mark.parametrize("desc", [
    "Example Co launched a robotics platform with companion software.",
    "Example Co launched a camera with a new mobile app.",
    "Example Co launched a sensor with an analytics portal.",
    "Example Co launched a device management API.",
])
def test_hardware_with_an_explicit_software_product_stays_digital(config, desc):
    assert one(config, "Product Launch", desc).factor == 1.0


@pytest.mark.parametrize("desc", [
    "Example Co launched an AI-powered platform for banks.", "Example Co launched a fintech cloud platform.",
    "Example Co launched a new user interface for its app.", "Example Co launched a software platform.",
])
def test_software_launches_without_hardware_words_stay_digital(config, desc):
    assert one(config, "Product Launch", desc).factor == 1.0


def test_hardware_words_are_whole_words(config):
    assert one(config, "Product Launch", "Example Co launched a mobile banking app for devotees.").factor == 1.0
    assert S._HARDWARE_PRODUCT.search("lab devices") and not S._HARDWARE_PRODUCT.search("Acme Labs and a cameraman")
    assert not S._HARDWARE_PRODUCT.search("equipmentless")


def test_financial_launch_still_checked_after_the_hardware_rule(config):
    assert one(config, "Product Launch", "Example Co launches a new property trust.").factor == 0.25


def test_same_product_name_with_a_missing_date_counts_once():
    a = Signal("A", SignalKind.PRODUCT_LAUNCH, 0.5, "a", None, "Catapult launched VisionPro for elite teams.", "Z")
    b = Signal("A", SignalKind.PRODUCT_LAUNCH, 0.4, "b", datetime(2026, 11, 20, tzinfo=timezone.utc),
               "Catapult unveils VisionPro at a trade show.", "Z")
    assert S.dedupe_signals([a, b]) == [a]


def test_powder_launch_is_hardware(config):
    assert one(config, "Product Launch", "Example Co launched a metal powder with a digital ordering platform.").factor == 0.5


def test_same_kind_extras_stop_at_the_total_cap():
    sigs = [L(0.6, "a", "Alpha Co launched Alpha Hub."), L(0.5, "b", "Beta Co launched Beta Hub."),
            L(0.45, "c", "Gamma Co launched Gamma Hub."), L(0.4, "d", "Delta Co launched Delta Hub.")]
    # extras 0.15 + 0.15, then the third extra is capped to 0 by the 0.30 total
    assert S.combined_strength(sigs) == round(1 - 0.4 * 0.85 * 0.85, 4)


# ------------------------------------------------------------ notes carried onto Signal: being acquired, role vacated
def test_being_acquired_note_reaches_the_signal(config, accounts):
    v = one(config, "Mergers & Acquisitions (M&A)", "Pickles Auctions to be acquired by EQT.", name="Pickles Auctions")
    sig = sig_for(config, v, accounts)
    assert sig.evidence == "Pickles Auctions to be acquired by EQT. (being acquired: leadership/platform change likely)"
    assert S.signal_notes(sig) == [S.MA_TARGET_NOTE] and sig.strength == 0.4239


def test_reclassified_funding_target_carries_the_note(config, accounts):
    v = one(config, "Funding", "Pickles Auctions to be acquired by EQT.", name="Pickles Auctions")
    assert S.signal_notes(sig_for(config, v, accounts)) == [S.MA_TARGET_NOTE]


def test_full_acquirer_has_no_being_acquired_note(config, accounts):
    v = one(config, "Mergers & Acquisitions (M&A)", "Gamma Group acquires Tiny Co.", name="Gamma Group")
    assert S.signal_notes(sig_for(config, v, accounts)) == []


def test_target_note_stacks_with_same_deal_note(config):
    d = "Delta Systems to be acquired by Gamma Group."
    va, vb = deal_pair(config, d, d, a_name="Gamma Group", b_name="Delta Systems")
    by_zi = {"ZA": Account("SF-A", "Gamma"), "ZB": Account("SF-B", "Delta")}
    sigs = {s.sf_account_id: s for s in S.scoops_to_signals([va, vb], by_zi, config, TODAY).signals}
    assert S.signal_notes(sigs["SF-B"]) == [S.MA_TARGET_NOTE, "same deal as Gamma Group"]
    assert S.signal_notes(sigs["SF-A"]) == ["same deal as Delta Systems"]


@pytest.mark.parametrize("desc", [
    "The CTO of Example Co has left to join another organisation.",
    "Example Co's Interim Chief Digital Officer is leaving.",
])
def test_role_vacated_note_reaches_the_signal(config, accounts, desc):
    v = one(config, "Executive Move", desc)
    assert v.notes == (S.VACATED_NOTE,)
    sig = sig_for(config, v, accounts)
    assert sig.evidence == desc + " (role vacated: new leader likely)" and S.signal_notes(sig) == [S.VACATED_NOTE]


def test_vacated_via_promotion_carries_the_note(config, accounts):
    v = one(config, "Executive Move", "Example Co's Jane moves from Chief Technology Officer to CEO.")
    assert S.signal_notes(sig_for(config, v, accounts)) == [S.VACATED_NOTE]


def test_incoming_leader_has_no_vacated_note(config, accounts):
    for d in ("Example Co appointed a new Chief Digital Officer.",
              "Example Co's Sam moved from Head of Marketing to Chief Digital Officer."):
        assert S.signal_notes(sig_for(config, one(config, "Executive Move", d), accounts)) == []


def test_vacated_and_ambiguous_notes_stack_in_order(config, accounts):
    v = one(config, "Executive Move", "Example Co's Head of Product, Footwear has left to join a rival.")
    assert v.ambiguous and S.signal_notes(sig_for(config, v, accounts)) == [S.AMBIGUOUS_NOTE, S.VACATED_NOTE]


def test_long_text_with_two_notes_fits(config, accounts):
    d = "Example Co's Interim Chief Digital Officer is leaving. " + "More text. " * 60
    sig = sig_for(config, one(config, "Executive Move", d), accounts)
    assert len(sig.evidence) <= S.EVIDENCE_MAX_CHARS and S.signal_notes(sig) == [S.VACATED_NOTE]


# ============================================================ review fixes on 600687a
# ------------------------------------------------------------ (1) 'with' / 'from' are not third-party buyers
@pytest.mark.parametrize("d", [
    "Acme signs agreement with Bigco to acquire Zeta.",
    "Acme gets approval from the ACCC to acquire Zeta.",
    "Acme, supported by an agreement with Bigco to acquire Zeta, expands.",
    "Acme reached a deal with Bigco to acquire Zeta.",
    "Acme won an offer from Bigco to acquire Zeta.",
])
def test_with_or_from_counterparty_does_not_make_the_company_the_target(d):
    assert S.ma_role(d, "Acme Holdings") == "acquirer"


def test_acme_with_bigco_is_a_full_acquirer(config):
    v = one(config, "Mergers & Acquisitions (M&A)", "Acme signs agreement with Bigco to acquire Zeta.", name="Acme Holdings")
    assert v.factor == 1.0 and v.notes == ()


@pytest.mark.parametrize("d,name,role", [
    ("Acme has entered into an agreement for Bigco to acquire a 9.9% stake in the company.", "Acme Holdings", "target"),
    ("Acme has entered into an agreement for Bigco and Cargo to acquire a stake in the company.", "Bigco Ltd", "acquirer"),
    ("Acme has entered into an agreement for Bigco and Cargo to acquire a stake in the company.", "Cargo Pty", "acquirer"),
    ("Acme made a deal for Bigco to buy it.", "Acme Holdings", "target"),
    ("The board allows Bigco to acquire a stake in Acme.", "Acme Holdings", "target"),
    ("The regulator permits Bigco to acquire Zeta.", "Bigco Ltd", "acquirer"),
])
def test_for_and_allow_constructions_still_work(d, name, role):
    assert S.ma_role(d, name) == role


def test_nextdc_goodman_digital_realty_wording_unchanged():
    assert S.ma_role(NEXTDC_TEXT, "NEXTDC Ltd") == "target"
    assert S.ma_role(NEXTDC_TEXT, "Goodman Group") == "acquirer"
    assert S.ma_role(NEXTDC_TEXT, "Digital Realty Trust") == "acquirer"


# ------------------------------------------------------------ (2) kind diversity before the cap
def test_new_hire_is_not_dropped_by_three_stronger_funding_scoops():
    sigs = [_sig(SignalKind.FUNDING, 0.50, src="f1"), _sig(SignalKind.FUNDING, 0.49, src="f2"),
            _sig(SignalKind.FUNDING, 0.48, src="f3"), _sig(SignalKind.NEW_HIRE, 0.45, src="h")]
    with_hire = S.combined_strength(sigs)
    without = S.combined_strength(sigs[:3])
    assert with_hire == 0.72 and without == 0.6388 and with_hire > without      # 1 - 0.5 * 0.775 * 0.85 * 0.85


def test_best_of_each_kind_is_picked_before_the_cap():
    sigs = [_sig(SignalKind.FUNDING, 0.5, src="f"), _sig(SignalKind.NEW_HIRE, 0.4, src="h"),
            _sig(SignalKind.MA, 0.3, src="m"), _sig(SignalKind.PRODUCT_LAUNCH, 0.2, src="l"),
            _sig(SignalKind.HIRING_PLAN, 0.1, src="p")]
    # leads damped: 0.2, 0.15, 0.1, 0.05; the cap keeps the best plus the three strongest others
    assert S.combined_strength(sigs) == round(1 - 0.5 * 0.8 * 0.85 * 0.9, 4)


def test_one_signal_per_kind_scores_are_unchanged():
    sigs = [_sig(SignalKind.EXEC_MOVE, 0.8, src="e"), _sig(SignalKind.FUNDING, 0.6, src="f")]
    assert S.combined_strength(sigs) == round(1 - 0.2 * 0.7, 4)


def test_catapult_nine_launches_unchanged_by_the_new_order():
    assert S.combined_strength(CATAPULT) == 0.7031


def test_intent_is_never_best_even_with_the_new_order():
    real, intent = _sig(SignalKind.FUNDING, 0.16, src="f"), _isig(0.3)
    assert S.combined_strength([real, intent]) == 0.286                 # 1 - 0.84 * (1 - 0.15)


def test_new_kind_ranks_among_extras_by_contribution():
    sigs = [_sig(SignalKind.EXEC_MOVE, 0.9, src="e"), _sig(SignalKind.FUNDING, 0.1, src="f1"),
            _sig(SignalKind.FUNDING, 0.1, src="f2"), _sig(SignalKind.FUNDING, 0.1, src="f3")]
    # funding lead counts 0.05; extras 0.1 + 0.1; best + three others = all four entries
    assert S.combined_strength(sigs) == round(1 - 0.1 * 0.95 * 0.95 * 0.95, 4)


# ------------------------------------------------------------ (3) place words are not product names
@pytest.mark.parametrize("text", [
    "Gamma launches New Zealand expansion of lending app.", "Gamma launched New Zealand credit card.",
    "Gamma launched Australian range of cards.", "Gamma launches Sydney pilot.",
    "Gamma unveils the Asia Pacific expansion.",
])
def test_place_and_generic_words_are_not_product_names(text):
    assert S._product_name(text) is None


def test_new_zealand_launches_do_not_dedupe_by_name():
    a = L(0.5, "a", "Gamma launches New Zealand expansion of lending app.")
    b = L(0.4, "b", "Gamma launched New Zealand credit card.")
    assert len(S.dedupe_signals([a, b])) == 2


def test_place_words_are_stripped_from_a_real_name():
    assert S._product_name("Gamma launched Australian VisionPro today.") == "visionpro"
    assert S._product_name("Gamma launched VisionPro Australia.") == "visionpro"


def test_lowercase_product_names_are_a_known_limit():
    assert S._product_name("Catapult launches the pro tracker for coaches.") is None
    a = L(0.5, "a", "Catapult launches the pro tracker for coaches in Europe this week for everyone.", day=20)
    b = L(0.4, "b", "Catapult introduced the pro tracker, a coaching tool for clubs worldwide.", day=20)
    assert len(S.dedupe_signals([a, b])) == 2                           # different first 60 characters, no capital name


# ------------------------------------------------------------ (5) application / webapp / online store are software
@pytest.mark.parametrize("desc", [
    "Example Co launches an application for battery monitoring.",
    "Example Co launched a webapp for sensor fleets.",
    "Example Co launched Powder Coat Pro online store.",
    "Example Co launched an online shop for laboratory reagents.",
    "Example Co launched applications for device management.",
])
def test_application_webapp_and_online_store_keep_hardware_words_digital(config, desc):
    assert one(config, "Product Launch", desc).factor == 1.0


def test_hardware_without_a_software_noun_is_still_physical(config):
    assert one(config, "Product Launch", "Example Co launched a battery for sensor fleets.").factor == 0.5


def test_allow_base_form_and_enable_permit_variants():
    assert S.ma_role("Acme is willing to allow Bigco to acquire a stake in Acme.", "Acme Holdings") == "target"
    assert S.ma_role("Acme will enable Bigco to acquire Zeta.", "Acme Holdings") == "unclear"      # Zeta is the object
    assert S.ma_role("Acme will permit Bigco to acquire Zeta.", "Bigco Ltd") == "acquirer"


# ============================================================ exact note strings (final review item 6)
def test_parenthetical_that_looks_like_a_note_prefix_is_plain_text():
    for tail in ("(being acquired by Bigco)", "(role vacated at the old firm)", "(minority stake purchase by Fund)",
                 "(debt facility of $5m)", "(reported talks with unions)", "(physical or unclear)", "(role ambiguous)"):
        sig = Signal("A", SignalKind.EXEC_MOVE, 0.5, "s", None, f"Acme appoints new CDO {tail}", "Z")
        assert S.signal_notes(sig) == [] and not S.is_ambiguous(sig)


def test_real_note_after_a_lookalike_parenthetical_strips_only_the_real_note():
    ev = "Acme appoints new CDO (being acquired by Bigco) (" + S.VACATED_NOTE + ")"
    sig = Signal("A", SignalKind.EXEC_MOVE, 0.5, "s", None, ev, "Z")
    assert S.signal_notes(sig) == [S.VACATED_NOTE]


def test_every_known_note_is_read_back_exactly():
    for note in sorted(S.KNOWN_NOTES):
        sig = Signal("A", SignalKind.MA, 0.5, "s", None, f"Plain scoop text. ({note})", "Z")
        assert S.signal_notes(sig) == [note]


def test_truncated_or_extended_note_text_is_not_a_note():
    for bad in (S.MA_TARGET_NOTE[:-1], S.MA_TARGET_NOTE + " today", S.VACATED_NOTE.upper(), "role vacated"):
        sig = Signal("A", SignalKind.MA, 0.5, "s", None, f"Plain scoop text. ({bad})", "Z")
        assert S.signal_notes(sig) == []


def test_same_deal_note_still_matches_with_names_only():
    ok = Signal("A", SignalKind.MA, 0.5, "s", None, "Text. (same deal as Bigco, Cargo)", "Z")
    no = Signal("A", SignalKind.MA, 0.5, "s", None, "Text. (same deal as )", "Z")
    assert S.signal_notes(ok) == ["same deal as Bigco, Cargo"] and S.signal_notes(no) == []


def test_note_prefixes_constant_is_gone():
    assert not hasattr(S, "NOTE_PREFIXES")


# ============================================================ fix-triggers3: scheme wording, percentage without 'stake'
@pytest.mark.parametrize("d", [
    "Acme shareholders vote on a scheme of arrangement for Bigco to acquire Acme.",
    "Acme shareholders approve a scheme for Bigco to acquire Acme.",
    "Acme gets approval from FIRB for Bigco to acquire Acme.",
    "Acme's board recommends a scheme of arrangement for Bigco to acquire all shares in Acme.",
    "Acme secures court approval for Bigco and Cargo to acquire the company.",
])
def test_scheme_and_regulator_wording_makes_the_company_the_target(d):
    assert S.ma_role(d, "Acme Holdings") == "target"


@pytest.mark.parametrize("name", ["Bigco Ltd"])
def test_scheme_buyer_side_is_the_acquirer(name):
    assert S.ma_role("Acme shareholders vote on a scheme of arrangement for Bigco to acquire Acme.", name) == "acquirer"
    assert S.ma_role("Acme gets approval from FIRB for Bigco to acquire Acme.", name) == "acquirer"


def test_scheme_target_weight_and_reason(config):
    v = one(config, "Mergers & Acquisitions (M&A)", "Acme shareholders vote on a scheme of arrangement for Bigco to acquire Acme.",
            name="Acme Holdings")
    assert v.factor == 1.0 and v.notes == (S.MA_TARGET_NOTE,)


@pytest.mark.parametrize("d,name,role", [
    ("Acme signs agreement with Bigco to acquire Zeta.", "Acme Holdings", "acquirer"),
    ("Acme gets approval from the ACCC to acquire Zeta.", "Acme Holdings", "acquirer"),
    ("Acme, supported by an agreement with Bigco to acquire Zeta, expands.", "Acme Holdings", "acquirer"),
    ("Acme seeks approval for the deal to acquire Zeta.", "Acme Holdings", "acquirer"),
    ("Acme raised funds for growth and plans to acquire Zeta.", "Acme Holdings", "acquirer"),
    ("Acme won approval for its plan to acquire Zeta.", "Acme Holdings", "acquirer"),
])
def test_existing_acquirer_cases_still_hold_after_the_for_change(d, name, role):
    assert S.ma_role(d, name) == role


def test_for_span_needs_a_named_company():
    assert S.ma_role("Acme secures approval for it to acquire Zeta.", "Acme Holdings") == "acquirer"
    assert S.ma_role("Acme secures approval for Bigco to acquire Zeta.", "Acme Holdings") == "unclear"


@pytest.mark.parametrize("d", [
    "Bigco to acquire 30% of Acme.", "Bigco acquires 19.9% of Acme.", "Bigco has agreed to buy 49.9% of Acme.",
    "Bigco to acquire 5% of Acme.",
])
def test_percentage_without_the_word_stake_is_a_minority_target(d, config):
    assert S.classify_ma(d, "Acme Holdings") == ("target", 0.35, S.MA_TARGET_MINORITY_NOTE)
    v = one(config, "Mergers & Acquisitions (M&A)", d, name="Acme Holdings")
    assert v.factor == pytest.approx(0.7) and v.notes == (S.MA_TARGET_MINORITY_NOTE,)


@pytest.mark.parametrize("d", [
    "Bigco to acquire 50% of Acme.", "Bigco to acquire 51% of Acme.", "Bigco to acquire 100% of Acme.",
    "Bigco to acquire all of Acme.", "Bigco to acquire the remaining 49% of Acme.",
    "Bigco to acquire the remaining 30% of Acme in a full takeover.", "Bigco to acquire Acme.",
])
def test_half_or_more_all_full_remaining_stay_half_weight_targets(d):
    assert S.classify_ma(d, "Acme Holdings")[:2] == ("target", 0.5)


def test_percentage_of_acquirer_side_is_still_minority():
    assert S.classify_ma("Bigco to acquire 30% of Acme.", "Bigco Ltd")[:2] == ("acquirer", 0.25)


def test_growth_percentages_do_not_make_a_minority_target():
    assert S.classify_ma("Acme, up 30% in revenue, to be acquired by Bigco.", "Acme Holdings")[:2] == ("target", 0.5)
    assert S.classify_ma("Revenue grew 30% as Bigco agreed to acquire Acme.", "Acme Holdings")[:2] == ("target", 0.5)


def test_percentage_of_with_the_stake_word_unchanged():
    assert S.classify_ma("Goodman Group to acquire a 9.9% stake in NEXTDC.", "NEXTDC Ltd")[:2] == ("target", 0.35)


# ============================================================ Iren-shaped partnership stacking (third dry run): a score must not RISE
def old_combined(strengths):
    """The formula before the per-kind stacking rules: best in full, every other signal at 0.5."""
    best, rest = max(strengths), 1.0
    for x in sorted(strengths, reverse=True)[1:]:
        rest *= 1 - 0.5 * x
    return round(1 - (1 - best) * rest, 4)


def partnership_trio(strengths=(0.29, 0.19, 0.19)):
    return [Signal("A", SignalKind.PARTNERSHIP, st, f"p{i}", datetime(2026, 11, 20 - 8 * i, tzinfo=timezone.utc),
                   f"Iren signs a customer contract number {i}.", "Z") for i, st in enumerate(strengths)]


def test_three_same_kind_partnerships_do_not_score_above_the_old_formula():
    sigs = partnership_trio()
    assert old_combined([0.29, 0.19, 0.19]) == 0.4185
    assert S.combined_strength(sigs) == 0.4185                    # was 0.487 (a +2.7 point rise at weight 40)


def test_new_combined_never_exceeds_the_old_formula_property():
    import itertools
    kinds = [SignalKind.PARTNERSHIP, SignalKind.FUNDING, SignalKind.PRODUCT_LAUNCH, SignalKind.EXEC_MOVE]
    vals = [0.05, 0.1, 0.19, 0.29, 0.45, 0.6, 0.9]
    n = 0
    for combo in itertools.product(range(len(kinds)), vals, vals, vals):
        k, a, b, c = combo
        sigs = [Signal("A", kinds[k], a, "a", None, "Alpha Co did Alpha one.", "Z"),
                Signal("A", kinds[k], b, "b", None, "Beta Co did Beta two.", "Z"),
                Signal("A", kinds[(k + 1) % 4], c, "c", None, "Gamma Co did Gamma three.", "Z")]
        assert S.combined_strength(sigs) <= old_combined([a, b, c]) + 1e-9, combo
        n += 1
    assert n == 4 * 7 ** 3


def test_same_kind_extras_never_count_more_than_half_their_strength():
    sigs = partnership_trio((0.5, 0.19, 0.1))
    assert S.combined_strength(sigs) == round(1 - 0.5 * (1 - 0.095) * (1 - 0.05), 4)


def test_strong_extras_are_capped_at_0_15_each():
    sigs = partnership_trio((0.9, 0.8, 0.7))
    assert S.combined_strength(sigs) == pytest.approx(1 - 0.1 * 0.85 * 0.85, abs=1e-4)   # 0.5 * 0.8 and 0.5 * 0.7 both exceed 0.15


# ------------------------------------------------------------ infrastructure / compute / ARR contracts are not partnerships
@pytest.mark.parametrize("desc", [
    "Iren signs $4bn ARR contracts for AI compute.", "Beta Corp signs an AI data-centre capacity deal worth $2 billion ARR.",
    "Beta Corp secures a GPU supply agreement.", "Beta Corp signs a 200MW power purchase agreement.",
    "Beta Corp signs a hyperscaler hosting contract.", "Beta Corp wins a take-or-pay offtake deal.",
    "Beta Corp signs colocation contracts with a cloud provider.", "Beta Corp signs a multi-year compute deal.",
])
def test_infrastructure_contracts_are_dropped_as_partnerships(config, desc):
    v = one(config, "Partnership", desc, name="Beta Corp")
    assert not v.keep
    assert v.reason == ("Dropped: infrastructure or capacity contract (compute, data centre, ARR, power), "
                        "not a product partnership")


@pytest.mark.parametrize("desc", [
    "Beta Corp partners with Fin Co to build a customer mobile app on GPU compute.",
    "Beta Corp signs a data-centre deal to host its digital platform for customers.",
    "Beta Corp signs a software partnership with a cloud provider.",
])
def test_infrastructure_wording_with_product_words_is_kept(config, desc):
    assert one(config, "Partnership", desc, name="Beta Corp").keep


def test_plain_partnership_without_infra_words_still_kept_at_0_20(config):
    v = one(config, "Partnership", "Beta Corp signs a sponsorship deal with Fin Co.", name="Beta Corp")
    assert v.keep and v.factor == pytest.approx(2 / 3)


def test_infrastructure_rule_only_applies_to_partnerships(config):
    assert one(config, "Funding", "Beta Corp raised $50 million to expand data centre capacity.", name="Beta Corp").keep
    assert one(config, "Product Launch", "Beta Corp launched a new cloud API for compute capacity.", name="Beta Corp").keep


def test_infrastructure_words_are_whole_words():
    assert not S._INFRASTRUCTURE_DEAL.search("Beta Corp signs a partnership on narrative, warrants and ironwork")
    assert S._INFRASTRUCTURE_DEAL.search("a 50 MW deal") and S._INFRASTRUCTURE_DEAL.search("$4bn ARR contracts")


@pytest.mark.parametrize("desc", ["Beta Corp signs a capacity agreement.", "Beta Corp signs a hyperscaler agreement.",
                                  "Beta Corp signs a hosting agreement."])
def test_each_infrastructure_word_works_on_its_own(config, desc):
    assert not one(config, "Partnership", desc, name="Beta Corp").keep


# ============================================================ review of 54be0d4: '% of' next to the verb, 'for' needs the right object
@pytest.mark.parametrize("d,name", [
    ("Bigco to acquire Acme, which generates 30% of its revenue from Asia.", "Acme Holdings"),
    ("Bigco to acquire Acme; 40% of staff will transfer.", "Acme Holdings"),
    ("Bigco to be acquired by Acme, 30% of Bigco shareholders support.", "Bigco Ltd"),
    ("Bigco to acquire Acme after 30% of Acme shareholders backed the offer.", "Acme Holdings"),
    ("Bigco to acquire Acme, with 20% of Acme's board departing.", "Acme Holdings"),
])
def test_percentages_about_something_else_do_not_make_a_minority_target(d, name):
    assert S.classify_ma(d, name)[:2] == ("target", 0.5)


@pytest.mark.parametrize("d", [
    "Bigco to acquire 30% of Acme.", "Bigco acquires 19.9% of Acme.", "In talks for Bigco to acquire 49% of Acme.",
    "Bigco buys 25% of Acme.", "Bigco agreed to purchase 10% of Acme.",
    "Bigco lifts holding to 30% of Acme.", "Bigco to acquire a further 20% of Acme.", "Bigco takes 15% of Acme.",
    "Bigco has bought 12.5% of Acme.",
])
def test_percentage_next_to_the_verb_with_the_target_right_after_of_is_minority(d):
    assert S.classify_ma(d, "Acme Holdings") == ("target", 0.35, S.MA_TARGET_MINORITY_NOTE)


@pytest.mark.parametrize("d", [
    "Bigco to acquire 50% of Acme.", "Bigco to acquire 100% of Acme.", "Bigco to acquire the remaining 49% of Acme.",
    "Bigco to acquire all of Acme.", "Bigco lifts holding to 55% of Acme.", "Bigco to acquire 75% of Acme.",
])
def test_half_or_more_and_remaining_stay_half_weight(d):
    assert S.classify_ma(d, "Acme Holdings")[:2] == ("target", 0.5)


def test_percentage_of_a_different_company_is_not_this_companys_minority_stake():
    assert S.classify_ma("Bigco to acquire 30% of Zeta and Acme.", "Acme Holdings")[:2] == ("target", 0.5)
    assert S.classify_ma("Bigco to acquire 30% of Zeta and all of Acme.", "Acme Holdings")[:2] == ("target", 0.5)


def test_full_takeover_wording_blocks_the_percent_of_rule():
    assert S.classify_ma("Bigco to acquire 30% of Acme in a full takeover.", "Acme Holdings")[:2] == ("target", 0.5)
    assert S.classify_ma("Bigco to acquire 30% of Acme, with majority control.", "Acme Holdings")[:2] == ("target", 0.5)


def test_lifts_holding_reads_as_buyer_and_target():
    d = "Bigco lifts holding to 55% of Acme."
    assert S.ma_role(d, "Bigco Ltd") == "acquirer" and S.ma_role(d, "Acme Holdings") == "target"


@pytest.mark.parametrize("d,name,role", [
    ("Acme seeks approval for Bigco to acquire Zeta.", "Acme Holdings", "unclear"),
    ("Acme raises funding for Bigco to acquire Zeta.", "Acme Holdings", "unclear"),
    ("Acme sets up a fund for Zeta to acquire startups.", "Acme Holdings", "unclear"),
    ("Acme gets approval from FIRB for Bigco to acquire Acme.", "Acme Holdings", "target"),
    ("Acme shareholders vote on a scheme of arrangement for Bigco to acquire Acme.", "Acme Holdings", "target"),
    ("Acme made a deal for Bigco to buy it.", "Acme Holdings", "target"),
    ("Acme has entered into an agreement for Bigco to acquire the company.", "Acme Holdings", "target"),
    ("Acme has entered into an agreement for Bigco to acquire 30% of the business.", "Acme Holdings", "target"),
    ("Acme seeks approval for Bigco to acquire Zeta.", "Bigco Ltd", "acquirer"),
    ("Acme seeks approval for Bigco to acquire Zeta.", "Zeta Pty", "target"),
])
def test_for_phrase_target_only_when_the_buyer_acquires_that_company(d, name, role):
    assert S.ma_role(d, name) == role


def test_nextdc_goodman_digital_realty_still_hold_after_the_object_rule():
    assert S.ma_role(NEXTDC_TEXT, "NEXTDC Ltd") == "target"
    assert S.ma_role(NEXTDC_TEXT, "Goodman Group") == "acquirer"
    assert S.ma_role(NEXTDC_TEXT, "Digital Realty Trust") == "acquirer"


@pytest.mark.parametrize("d,name,role", [
    ("Acme is for sale; Bigco to acquire Acme.", "Acme Holdings", "target"),
    ("Acme is for sale. Bigco to acquire Acme.", "Acme Holdings", "target"),
    ("Acme is for sale; Bigco to acquire Acme.", "Bigco Ltd", "acquirer"),
    ("Acme to acquire Zeta; Acme is a listed firm.", "Acme Holdings", "acquirer"),
    ("Acme grew 30% as Bigco agreed to acquire Acme.", "Acme Holdings", "acquirer"),      # same clause: known limit
])
def test_earlier_sentence_mention_does_not_make_the_company_the_buyer(d, name, role):
    assert S.ma_role(d, name) == role


def test_catapult_nine_launches_value():
    assert S.combined_strength(CATAPULT) == 0.7031


# ============================================================ fix-triggers4: unrelated earlier mention, per cent spelling, possessives
@pytest.mark.parametrize("d", [
    "Acme opens a new office. Bigco to acquire Zeta.", "Acme is a Sydney software firm. Bigco to acquire Zeta",
    "Acme posted record sales; Bigco to acquire Zeta.", "Acme hires a CFO. Meanwhile Bigco agreed to buy Zeta.",
])
def test_company_named_only_in_an_unrelated_earlier_sentence_is_unclear(d):
    assert S.classify_ma(d, "Acme Holdings")[:2] == ("unclear", 0.25)


@pytest.mark.parametrize("d,name,role", [
    ("Acme shares jump. Acme to acquire Zeta.", "Acme Holdings", "acquirer"),
    ("Acme announced results. Bigco to acquire Acme.", "Acme Holdings", "target"),
    ("Acme is a listed firm. It plans to acquire Zeta.", "Acme Holdings", "acquirer"),
    ("Acme is a listed firm. The company plans to acquire Zeta.", "Acme Holdings", "acquirer"),
    ("Acme is a listed firm. Today it said it will acquire Zeta.", "Acme Holdings", "acquirer"),
    ("Acme opens a new office. Bigco to acquire Zeta.", "Bigco Ltd", "acquirer"),
    ("Acme opens a new office. Bigco to acquire Zeta.", "Zeta Pty", "target"),
])
def test_same_clause_and_pronoun_subjects_still_read_as_before(d, name, role):
    assert S.ma_role(d, name) == role


def test_unclear_earlier_mention_is_quarter_weight(config):
    v = one(config, "Mergers & Acquisitions (M&A)", "Acme opens a new office. Bigco to acquire Zeta.", name="Acme Holdings")
    assert v.factor == 0.5 and v.notes == (S.MA_UNCLEAR_NOTE,)


@pytest.mark.parametrize("d", [
    "Bigco to acquire 30 per cent of Acme.", "Bigco to acquire 30 percent of Acme.", "Bigco to acquire up to 30% of Acme.",
    "Bigco to acquire up to 30 per cent of Acme.", "Bigco acquires 19.9 per cent of Acme.", "Bigco lifts holding in Acme to 30%.",
    "Bigco lifts holding in Acme to 30 per cent.", "Bigco raises its stake in Acme to up to 25 percent.",
])
def test_per_cent_up_to_and_holding_to_forms_are_minority_targets(d):
    assert S.classify_ma(d, "Acme Holdings") == ("target", 0.35, S.MA_TARGET_MINORITY_NOTE)


@pytest.mark.parametrize("d", [
    "Bigco to acquire 50 per cent of Acme.", "Bigco to acquire 100 per cent of Acme.", "Bigco to acquire 100 percent of Acme.",
    "Bigco lifts holding in Acme to 55%.", "Bigco lifts holding in Acme to 55 per cent.",
    "Bigco to acquire the remaining 49 per cent of Acme.", "Bigco to acquire up to 60% of Acme.",
])
def test_majority_forms_with_per_cent_stay_half_weight(d):
    assert S.classify_ma(d, "Acme Holdings")[:2] == ("target", 0.5)


def test_per_cent_in_the_stake_rule_and_acquirer_side():
    assert S.classify_ma("Goodman Group to acquire a 9.9 per cent stake in NEXTDC.", "NEXTDC Ltd")[:2] == ("target", 0.35)
    assert S.classify_ma("Goodman Group to acquire a 9.9 per cent stake in NEXTDC.", "Goodman Group")[:2] == ("acquirer", 0.25)
    assert S.classify_ma("Bigco lifts holding in Acme to 30%.", "Bigco Ltd")[:2] == ("acquirer", 0.25)


@pytest.mark.parametrize("name", ["Acme Holdings"])
def test_possessive_is_not_the_company_name(name):
    d = "Bigco takes 15% of Acme's market."
    assert S.classify_ma(d, name)[:2] == ("unclear", 0.25)
    assert S._percent_of_target(d, "Acme") is None
    assert S._percent_of_target("Bigco takes 15% of Acme’s market.", "Acme") is None
    assert S.classify_ma(d, "Bigco Ltd")[:2] == ("acquirer", 0.25)


def test_possessive_helper():
    assert S._mentions("Acme's market", "Acme") and not S._mentions("Acme's market", "Acme", possessive=False)
    assert S._mentions("Acme market", "Acme", possessive=False) and S._mentions("Acmes", "Acme") is False
    assert S._mentions("the Acme.", "Acme", possessive=False)


def test_company_still_named_normally_after_a_possessive_elsewhere():
    d = "Bigco takes 15% of Acme's market. Bigco to acquire 30% of Acme."
    assert S._percent_of_target(d, "Acme") == 30.0


def test_possessive_after_a_for_span_is_not_the_company():
    assert S.ma_role("Acme secures approval for Bigco to acquire Acme's rival.", "Acme Holdings") == "unclear"
    assert S.ma_role("Acme secures approval for Bigco to acquire Acme.", "Acme Holdings") == "target"


def test_lifts_holding_in_a_possessive_is_not_the_company():
    assert S._percent_of_target("Bigco lifts holding in Acme's parent to 30%.", "Acme") is None
    assert S._percent_of_target("Bigco lifts holding in Acme to 30%.", "Acme") == 30.0


def test_filler_words_do_not_count_as_another_company():
    assert S.ma_role("Acme is a listed firm. Meanwhile it will acquire Zeta.", "Acme Holdings") == "acquirer"
    assert S.ma_role("Acme is a listed firm. However Bigco will acquire Zeta.", "Acme Holdings") == "unclear"
