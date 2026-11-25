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
    "Acme Ventures invested in Gamma Apps.",
    "Gamma Apps has invested in a startup.",
    "Gamma Apps is investing in a new fund.",
])
def test_investor_phrasings_dropped(config, desc):
    assert not judge(config, desc=desc).keep


def test_funding_received_without_invest_verb_kept(config):
    assert judge(config, desc="Gamma Apps closed a Series B round.").keep


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


def test_decay_half_life():
    assert math.isclose(S.recency_factor(S.RECENCY_HALF_LIFE_DAYS), 0.5)


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


def test_fresh_exec_move_strength_equals_base_weight():
    assert S.signal_strength(SignalKind.EXEC_MOVE, 0) == S.KIND_BASE_WEIGHT[SignalKind.EXEC_MOVE]


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
    assert math.isclose(sig.strength, S.signal_strength(SignalKind.EXEC_MOVE, 5))


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
    topics = {s.source.split(":")[1] for s in out}
    assert topics <= S.INTENT_TOPICS
    assert any("Payroll" in r[0].get("topic", "") and "not a Pendo-relevant" in r[1] for r in skipped)


def test_intent_every_row_accounted_for(intent_rows, accounts, config):
    skipped = []
    out = S.intent_to_signals(intent_rows, accounts, config, TODAY, skipped)
    assert len(out) + len(skipped) == len(intent_rows)


def test_intent_skip_reasons(intent_rows, accounts, config):
    skipped = []
    S.intent_to_signals(intent_rows, accounts, config, TODAY, skipped)
    reasons = " | ".join(r for _, r in skipped)
    for needle in ("outside 60-100", "not in A-E", "no matching account", "missing topic or company_id", "superseded"):
        assert needle in reasons


def test_intent_highest_score_wins_per_company_topic(intent_rows, accounts, config):
    out = S.intent_to_signals(intent_rows, accounts, config, TODAY)
    dx = [s for s in out if s.zoominfo_id == "FAKE-ZI-2001"]
    assert len(dx) == 1 and "score 90" in dx[0].evidence


def test_intent_strength_formula_capped_and_bounded(intent_rows, accounts, config):
    out = S.intent_to_signals(intent_rows, accounts, config, TODAY)
    assert all(0.0 <= s.strength <= S.INTENT_MAX_STRENGTH for s in out)
    undated = next(s for s in out if s.zoominfo_id == "FAKE-ZI-2002")     # score 100, A, no date: capped
    assert undated.strength == S.INTENT_MAX_STRENGTH


def test_intent_recency_decay_applies_when_dated(accounts, config):
    row = {"company_id": "FAKE-ZI-2001", "topic": "Onboarding", "signal_score": 100, "audience_strength": "A"}
    fresh = S.intent_to_signals([{**row, "recorded_date": "2026-11-25"}], accounts, config, TODAY)[0]
    old = S.intent_to_signals([{**row, "recorded_date": "2026-10-25"}], accounts, config, TODAY)[0]
    assert old.strength < fresh.strength


def test_intent_audience_ordering(accounts, config):
    def strength(aud):
        r = {"company_id": "FAKE-ZI-2001", "topic": "Onboarding", "signal_score": 75, "audience_strength": aud}
        return S.intent_to_signals([r], accounts, config, TODAY)[0].strength
    vals = [strength(a) for a in "ABCDE"]
    assert vals == sorted(vals, reverse=True) and vals[0] > vals[-1]


def test_intent_kind_and_links(intent_rows, accounts, config):
    out = S.intent_to_signals(intent_rows, accounts, config, TODAY)
    assert all(s.kind is SignalKind.PROJECT_PAIN and s.sf_account_id for s in out)
    assert all("UNVERIFIED" in s.evidence for s in out)


def test_intent_works_without_skipped_list_and_non_dict_rows(accounts, config):
    assert S.intent_to_signals(["junk", None], accounts, config, TODAY) == []


def test_intent_rejects_bool_and_string_scores(accounts, config):
    rows = [{"company_id": "FAKE-ZI-2001", "topic": "Onboarding", "signal_score": v, "audience_strength": "A"}
            for v in (True, "90")]
    skipped = []
    assert S.intent_to_signals(rows, accounts, config, TODAY, skipped) == [] and len(skipped) == 2


def test_intent_deterministic(intent_rows, accounts, config):
    a = S.intent_to_signals(intent_rows, accounts, config, TODAY)
    b = S.intent_to_signals(list(reversed(intent_rows)), list(reversed(accounts)), config, TODAY)
    assert a == b
