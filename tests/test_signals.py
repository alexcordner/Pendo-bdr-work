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


def test_age_boundary_with_custom_config(config):
    cfg = replace(config, signals=replace(config.signals, max_age_days=10))
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
    assert S.signal_strength(SignalKind.INTENT, 0) == 0.5


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


def test_radar_same_zoominfo_id_first_account_wins(config):
    accts = [Account("SF-1", "First", zoominfo_id="Z"), Account("SF-2", "Second", zoominfo_id="Z")]
    out = S.trigger_radar([make_scoop(cid="Z")], accts, config, TODAY)
    assert [e.account.name for e in out] == ["First"]


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
    row = {"company_id": "FAKE-ZI-2001", "topic": "Digital Experience", "signal_score": 60, "audience_strength": "A"}
    skipped = []
    assert S.intent_to_signals([row], accounts, config, TODAY, skipped) == []
    assert skipped and "strength 0.0" in skipped[0][1]
    assert S.intent_to_signals([{**row, "signal_score": 61}], accounts, config, TODAY)[0].strength == 0.025


def test_intent_highest_score_wins_per_company_topic(intent_rows, accounts, config):
    out = S.intent_to_signals(intent_rows, accounts, config, TODAY)
    dx = [s for s in out if s.zoominfo_id == "FAKE-ZI-2001"]
    assert len(dx) == 1 and "score 90" in dx[0].evidence


def test_intent_literal_strengths(accounts, config):
    def strength(score, aud, date=None, topic="Digital Experience"):
        r = {"company_id": "FAKE-ZI-2001", "topic": topic, "signal_score": score, "audience_strength": aud}
        if date:
            r["recorded_date"] = date
        return S.intent_to_signals([r], accounts, config, TODAY)[0].strength
    assert S.INTENT_MAX_STRENGTH == 0.5
    assert strength(100, "A") == 0.5                      # 1.0 capped at 0.5
    assert strength(90, "A") == 0.5                       # 0.75 capped
    assert strength(80, "B") == 0.425                     # 0.5 * 0.85
    assert strength(75, "C") == 0.2625                    # 0.375 * 0.7
    assert strength(70, "E") == 0.1                       # 0.25 * 0.4
    assert strength(100, "A", "2026-11-20") == 0.4239     # 0.5 * 0.5 ** (5/21)
    assert strength(100, "A", "2026-10-25") == 0.1797     # 31 days: 0.5 * 0.5 ** (31/21)


def test_intent_never_exceeds_a_fresh_scoop_of_half_strength(intent_rows, accounts, config):
    out = S.intent_to_signals(intent_rows, accounts, config, TODAY)
    assert out and all(s.strength <= 0.5 for s in out)
    assert S.signal_strength(SignalKind.EXEC_MOVE, 0) > max(s.strength for s in out)


def test_intent_recency_decay_applies_when_dated(accounts, config):
    row = {"company_id": "FAKE-ZI-2001", "topic": "Product Onboarding", "signal_score": 100, "audience_strength": "A"}
    fresh = S.intent_to_signals([{**row, "recorded_date": "2026-11-25"}], accounts, config, TODAY)[0]
    old = S.intent_to_signals([{**row, "recorded_date": "2026-10-25"}], accounts, config, TODAY)[0]
    assert old.strength < fresh.strength


def test_intent_audience_ordering(accounts, config):
    def strength(aud):
        r = {"company_id": "FAKE-ZI-2001", "topic": "User Onboarding", "signal_score": 75, "audience_strength": aud}
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
