"""Tests for src/state.py. Offline; inputs come from tests/fixtures/state_scenario.json (synthetic)
plus the existing disposition fixture. Disk tests use tmp_path only."""
import json
import logging
import os
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from src import state as S
from src.loaders import build_disposition_index, call_from_nooks, parse_utc
from src.models import CallRecord, Prospect, SentEmail
from tests.conftest import load_fixture


# ---------------------------------------------------------------- helpers
def _d(st):
    """state_to_dict without last_run (run counts legitimately differ between a run and its replay)."""
    d = S.state_to_dict(st)
    d.pop("last_run")
    return d


def _dt(v):
    return parse_utc(v) if v else None


@pytest.fixture(scope="module")
def scenario():
    raw = load_fixture("state_scenario.json")
    prospects = [Prospect(**{"sf_contact_id": None, "title": None, "email": None, "phone_e164": None, **p}) for p in raw["prospects"]]
    calls = [CallRecord(**{**c, "started_at": _dt(c["started_at"])}) for c in raw["calls"]]
    sent = [SentEmail(message_id=s["message_id"], thread_id=s["thread_id"], recipient=s["recipient"],
                      recipient_key=s["recipient"], sent_at=_dt(s["sent_at"])) for s in raw["sent"]]
    return prospects, calls, sent


def call(cid="C", pid="FAKE-P1", disp="No Answer", dur=5, when="2026-11-10T01:00:00Z", to="+61400000011"):
    return CallRecord(nooks_id=cid, prospect_id=pid, disposition_name=disp, duration_s=dur,
                      started_at=_dt(when), to_e164=to)


def pros(nid="FAKE-P1", acct="FAKE-ACC-1", phone="+61400000011", email="pat.one@example.com", **kw):
    return Prospect(
        nooks_id=nid, sf_contact_id=None, sf_account_id=acct, nooks_account_id=None, name="X",
        email=email, phone_e164=phone, **kw)


def rec(calls, prospects=None, state=None, **kw):
    return S.record_calls(state or S.State(), calls, [pros()] if prospects is None else prospects,
                          config=kw.pop("config"), **kw)


def sent_email(mid="M", addr="patone@example.com", when="2026-11-20T01:00:00Z"):
    return SentEmail(message_id=mid, thread_id="T", recipient=addr,
                     recipient_key=addr, sent_at=_dt(when))


# ---------------------------------------------------------------- outcome rules
def test_no_answer_19s_is_no_contact(config):
    assert S.call_outcome(call(dur=19), config) == S.NO_CONTACT


def test_no_answer_21s_is_still_no_contact_by_name(config):
    # a 'No Answer' is never a connect, whatever the duration
    assert S.call_outcome(call(dur=21), config) == S.NO_CONTACT


def test_unknown_disposition_boundary_19_vs_21(config):
    assert S.call_outcome(call(disp="Meeting Booked", dur=19), config) == S.NO_CONTACT
    assert S.call_outcome(call(disp="Meeting Booked", dur=21), config) == S.CONNECT


def test_exactly_threshold_is_connect(config):
    assert S.call_outcome(call(disp="Interested", dur=config.limits.no_contact_max_duration_s), config) == S.CONNECT


def test_voicemail_never_a_connect_even_when_long(config):
    assert S.call_outcome(call(disp="Left Voicemail", dur=600), config) == S.NO_CONTACT


def test_gatekeeper_and_bad_number_never_connect(config):
    assert S.call_outcome(call(disp="Gatekeeper", dur=300), config) == S.NO_CONTACT
    assert S.call_outcome(call(disp="Bad Number / Wrong Number", dur=300), config) == S.NO_CONTACT


def test_disposition_name_match_ignores_case_and_space(config):
    assert S.call_outcome(call(disp="  left voicemail ", dur=300), config) == S.NO_CONTACT


def test_none_disposition_follows_duration(config):
    assert S.call_outcome(call(disp=None, dur=5), config) == S.NO_CONTACT
    assert S.call_outcome(call(disp=None, dur=60), config) == S.CONNECT


def test_none_duration_does_not_crash(config):
    assert S.call_outcome(call(disp="Interested", dur=None), config) == S.NO_CONTACT


def test_real_fixture_boundary_calls(config):
    idx = build_disposition_index(load_fixture("nooks_call_dispositions.json")["data"])
    rows = load_fixture("nooks_calls_sample.json")["data"]
    calls = [call_from_nooks(r, idx) for r in rows]
    na = [c for c in calls if c.disposition_name == "No Answer" and c.duration_s in (19, 21)]
    assert {c.duration_s for c in na} == {19, 21}
    assert all(S.call_outcome(c, config) == S.NO_CONTACT for c in na)
    vm = [c for c in calls if c.disposition_name == "Left Voicemail"]
    assert vm and all(S.call_outcome(c, config) == S.NO_CONTACT for c in vm)


# ---------------------------------------------------------------- record_calls
def test_record_calls_updates_account_and_prospect(config):
    st = rec([call("C1", dur=5)], config=config)
    a = st.accounts["FAKE-ACC-1"]
    assert a.last_outcome == S.NO_CONTACT and a.last_connect_at is None
    assert st.prospects["FAKE-P1"].call_count == 1


def test_connect_sets_last_connect_at(config):
    st = rec([call("C1", disp="Interested", dur=90)], config=config)
    assert st.accounts["FAKE-ACC-1"].last_outcome == S.CONNECT
    assert st.accounts["FAKE-ACC-1"].last_connect_at == _dt("2026-11-10T01:00:00Z")
    assert st.prospects["FAKE-P1"].connect_count == 1


def test_latest_call_wins_regardless_of_input_order(config):
    older = call("C1", disp="Interested", dur=90, when="2026-11-10T01:00:00Z")
    newer = call("C2", disp="No Answer", dur=3, when="2026-11-12T01:00:00Z")
    a = rec([newer, older], config=config).accounts["FAKE-ACC-1"]
    assert a.last_call_at == newer.started_at and a.last_outcome == S.NO_CONTACT
    assert a.last_connect_at == older.started_at


def test_replay_same_calls_is_idempotent(config, scenario):
    prospects, calls, _ = scenario
    once = S.record_calls(S.State(), calls, prospects, config=config)
    twice = S.record_calls(once, calls, prospects, config=config)
    assert _d(once) == _d(twice)
    assert twice.report["record_calls"]["new"] == 0
    assert twice.report["record_calls"]["duplicate"] == once.report["record_calls"]["new"]
    assert twice.prospects["FAKE-P1"].call_count == once.prospects["FAKE-P1"].call_count == 2


def test_duplicates_within_one_batch_counted_once(config):
    st = rec([call("C1"), call("C1")], config=config)
    assert st.prospects["FAKE-P1"].call_count == 1
    assert st.report["record_calls"]["duplicate"] == 1


def test_input_state_not_mutated(config):
    base = S.State()
    rec([call("C1")], state=base, config=config)
    assert base.accounts == {} and base.seen_calls == {}


def test_calls_with_none_nooks_id_dedupe_on_fingerprint(config):
    c = replace(call("x"), nooks_id=None)
    st = rec([c], config=config)
    st = rec([c], state=st, config=config)
    assert st.prospects["FAKE-P1"].call_count == 1


def test_unlinked_call_matched_on_phone(config):
    c = call("C1", pid=None, to="+61400000011")
    st = rec([c], config=config)
    assert st.accounts["FAKE-ACC-1"].last_call_at == c.started_at
    assert st.prospects["FAKE-P1"].call_count == 1


def test_unmatched_phone_counted_not_guessed(config):
    st = rec([call("C1", pid=None, to="+61400000555")], config=config)
    rep = st.report["record_calls"]
    assert rep["unmatched"] == 1 and rep["unmatched_call_ids"] == ["C1"]
    assert rep["unmatched_reasons"] == {"no_match": 1}
    assert st.accounts == {} and st.prospects == {} and st.seen_calls == {}


def test_unmatched_logs_warning(config, caplog):
    with caplog.at_level(logging.WARNING, logger="src.state"):
        rec([call("C1", pid=None, to="+61400000555")], config=config)
    assert "could not be matched" in caplog.text


def test_call_without_prospect_or_phone_is_unmatched(config):
    st = rec([call("C1", pid=None, to=None)], config=config)
    assert st.report["record_calls"]["unmatched_reasons"] == {"no_phone": 1}


def test_ambiguous_shared_phone_not_guessed(config, scenario):
    prospects, _, _ = scenario
    st = S.record_calls(S.State(), [call("C1", pid=None, to="+61400000099")], prospects, config=config)
    assert st.report["record_calls"]["unmatched_reasons"] == {"ambiguous_phone": 1}
    assert st.accounts == {}


def test_unmatched_calls_are_retried_next_run(config):
    c = call("C1", pid=None, to="+61400000011")
    st = rec([c], prospects=[], config=config)          # no prospects known yet
    assert st.report["record_calls"]["unmatched"] == 1
    st = rec([c], prospects=[pros()], state=st, config=config)
    assert st.prospects["FAKE-P1"].call_count == 1


def test_prospect_id_not_in_prospect_list_updates_prospect_only(config):
    st = rec([call("C1", pid="FAKE-GHOST")], prospects=[pros()], config=config)
    assert st.prospects["FAKE-GHOST"].call_count == 1
    assert st.accounts == {} and st.report["record_calls"]["no_account"] == 1


def test_account_found_via_accounts_by_id_when_prospect_has_no_sf_id(config):
    class Acct:  # only nooks_account_id is read
        nooks_account_id = "FAKE-NA-3"
    p = Prospect(nooks_id="FAKE-P3", sf_contact_id=None, sf_account_id=None,
                 nooks_account_id="FAKE-NA-3", name="N", phone_e164="+61400000013")
    st = rec([call("C1", pid="FAKE-P3", to="+61400000013")], prospects=[p],
             accounts_by_id={"FAKE-ACC-3": Acct()}, config=config)
    assert "FAKE-ACC-3" in st.accounts


def test_prospect_with_none_nooks_id_is_never_matched_by_none(config):
    p = replace(pros(), nooks_id=None)
    st = rec([call("C1", pid=None, to="+61400000011")], prospects=[p], config=config)
    assert st.report["record_calls"]["unmatched"] == 1      # no id to key history on; not guessed


def test_scenario_end_to_end(config, scenario):
    prospects, calls, _ = scenario
    st = S.record_calls(S.State(), calls, prospects, config=config)
    rep = st.report["record_calls"]
    assert rep["new"] == 6 and rep["unmatched"] == 1                  # C6 has an unknown number
    assert st.accounts["FAKE-ACC-1"].last_outcome == S.NO_CONTACT     # voicemail 90s is not a connect
    assert st.accounts["FAKE-ACC-2"].last_outcome == S.CONNECT
    assert "FAKE-P3" in st.prospects and "FAKE-ACC-3" not in st.accounts   # P3 has no account known


# ---------------------------------------------------------------- do not contact
def test_dnc_by_name_from_any_past_call(config):
    calls = [call("C1", pid="FAKE-P1", disp="Do Not Call", when="2025-01-01T00:00:00Z"),
             call("C2", pid="FAKE-P1", disp="Interested", dur=100, when="2026-11-01T00:00:00Z")]
    assert S.do_not_call_prospects(calls, config) == {"fake-p1"}


def test_dnc_ignores_other_dispositions_and_none_ids(config):
    calls = [call("C1", pid=None, disp="Do Not Call"), call("C2", pid="FAKE-P2", disp="No Answer"),
             call("C3", pid="FAKE-P3", disp=None)]
    assert S.do_not_call_prospects(calls, config) == set()


def test_dnc_matches_by_name_not_id(config):
    c = replace(call("C1", disp="Do Not Call"), disposition_id="disp-something-else")
    assert S.do_not_call_prospects([c], config) == {"fake-p1"}


def test_dnc_phones_cover_unlinked_calls(config):
    c = call("C1", pid=None, disp="Do Not Call", to="+61400000555")
    assert S.do_not_call_phones([c], config) == {"+61400000555"}
    st = rec([c], prospects=[], config=config)
    assert "+61400000555" in st.dnc_phones               # saved even though the call was unmatched


def test_dnc_is_sticky_in_state(config):
    st = rec([call("C1", disp="Do Not Call")], config=config)
    st = rec([call("C2", disp="Interested", dur=99, when="2026-11-20T00:00:00Z")], state=st, config=config)
    assert "fake-p1" in st.dnc_prospect_ids


def test_is_blocked_dnc_id(config):
    st = rec([call("C1", disp="Do Not Call")], config=config)
    blocked, reason = S.is_blocked(pros(), st)
    assert blocked and "Do Not Call" in reason


def test_is_blocked_dnc_phone_for_other_prospect(config):
    st = rec([call("C1", pid=None, disp="Do Not Call", to="+61400000555")], prospects=[], config=config)
    blocked, reason = S.is_blocked(pros(nid="FAKE-X", phone="+61400000555"), st)
    assert blocked and "+61400000555" in reason


def test_is_blocked_dnc_on_unlinked_call_matched_by_phone(config):
    st = rec([call("C1", pid=None, disp="Do Not Call", to="+61400000011")], config=config)
    assert "fake-p1" in st.dnc_prospect_ids


def test_is_blocked_prospect_flag(config):
    blocked, reason = S.is_blocked(pros(do_not_call=True), S.State())
    assert blocked and reason


def test_is_blocked_email_opt_out_blocks_email_not_call():
    p = pros(opted_out_email=True)
    assert S.is_blocked(p, S.State(), "email")[0] is True
    assert S.is_blocked(p, S.State(), "any")[0] is True
    assert S.is_blocked(p, S.State(), "call") == (False, "")


def test_is_blocked_clean_prospect_and_none_ids():
    p = replace(pros(), nooks_id=None, phone_e164=None)
    assert S.is_blocked(p, S.State()) == (False, "")


def test_is_blocked_every_block_has_reason(config):
    st = rec([call("C1", disp="Do Not Call")], config=config)
    for p in (pros(), pros(do_not_call=True), pros(nid="Z", phone="+61400000011"), pros(nid="Z", phone=None, opted_out_email=True)):
        blocked, reason = S.is_blocked(p, st)
        assert blocked and reason.strip()


def test_is_blocked_bad_channel_raises():
    with pytest.raises(ValueError):
        S.is_blocked(pros(), S.State(), "sms")


def test_email_opt_out_keys_dotted_and_none(scenario):
    prospects, _, _ = scenario
    assert S.email_opt_out_keys(prospects) == {"optout@example.com"}
    assert S.email_opt_out_keys([pros(email=None, opted_out_email=True)]) == set()


def test_mark_do_not_call_returns_copies(config):
    st = rec([call("C1", disp="Do Not Call")], config=config)
    orig = [pros(), pros(nid="FAKE-P2", acct="FAKE-ACC-2", phone="+61400000012", email="sam.two@example.com")]
    out = S.mark_do_not_call(orig, st)
    assert [p.do_not_call for p in out] == [True, False]
    assert orig[0].do_not_call is False


# ---------------------------------------------------------------- cooldowns
def test_last_touched_latest_of_call_or_email(config):
    st = rec([call("C1", when="2026-11-10T01:00:00Z")], config=config)
    st = S.record_sent_emails(st, [sent_email(when="2026-11-15T01:00:00Z")], [pros()])
    assert S.last_touched(st) == {"FAKE-ACC-1": date(2026, 11, 15)}


def test_last_touched_call_newer_than_email(config):
    st = S.record_sent_emails(S.State(), [sent_email(when="2026-11-01T01:00:00Z")], [pros()])
    st = rec([call("C1", when="2026-11-10T01:00:00Z")], state=st, config=config)
    assert S.last_touched(st)["FAKE-ACC-1"] == date(2026, 11, 10)


def test_cooldown_no_answer_three_days(config):
    st = rec([call("C1", when="2026-11-10T01:00:00Z")], config=config)
    assert S.cooldown_until(st, config)["FAKE-ACC-1"] == date(2026, 11, 13)
    assert S.is_in_cooldown(st, "FAKE-ACC-1", date(2026, 11, 12), config)
    assert not S.is_in_cooldown(st, "FAKE-ACC-1", date(2026, 11, 13), config)


def test_cooldown_connect_fourteen_days(config):
    st = rec([call("C1", disp="Interested", dur=60, when="2026-11-10T01:00:00Z")], config=config)
    assert S.cooldown_until(st, config)["FAKE-ACC-1"] == date(2026, 11, 24)


def test_cooldown_month_boundary_no_answer(config):
    st = rec([call("C1", when="2026-11-29T01:00:00Z")], config=config)
    assert S.cooldown_until(st, config)["FAKE-ACC-1"] == date(2026, 12, 2)


def test_cooldown_year_and_leap_boundary(config):
    st = rec([call("C1", disp="Interested", dur=60, when="2026-12-25T01:00:00Z")], config=config)
    assert S.cooldown_until(st, config)["FAKE-ACC-1"] == date(2027, 1, 8)
    st = rec([call("C2", disp="Interested", dur=60, when="2028-02-20T01:00:00Z")], config=config)
    assert S.cooldown_until(st, config)["FAKE-ACC-1"] == date(2028, 3, 5)   # 2028 is a leap year


def test_cooldown_connect_then_no_answer_keeps_longer_window(config):
    calls = [call("C1", disp="Interested", dur=60, when="2026-11-01T01:00:00Z"),
             call("C2", disp="No Answer", dur=3, when="2026-11-05T01:00:00Z")]
    # connect + 14 = Nov 15 beats no-answer Nov 5 + 3 = Nov 8
    assert S.cooldown_until(rec(calls, config=config), config)["FAKE-ACC-1"] == date(2026, 11, 15)


def test_cooldown_no_answer_after_old_connect_uses_later_date(config):
    calls = [call("C1", disp="Interested", dur=60, when="2026-10-01T01:00:00Z"),
             call("C2", disp="No Answer", dur=3, when="2026-11-05T01:00:00Z")]
    assert S.cooldown_until(rec(calls, config=config), config)["FAKE-ACC-1"] == date(2026, 11, 8)


def test_cooldown_unknown_or_none_account_not_in_cooldown(config):
    assert not S.is_in_cooldown(S.State(), "NOPE", date(2026, 11, 1), config)
    assert not S.is_in_cooldown(S.State(), None, date(2026, 11, 1), config)


def test_email_alone_starts_no_call_cooldown(config):
    st = S.record_sent_emails(S.State(), [sent_email()], [pros()])
    assert "FAKE-ACC-1" not in S.cooldown_until(st, config)


# ---------------------------------------------------------------- sheet history
def test_sheet_roundtrip_and_last_sheet():
    st = S.record_sheet(S.State(), date(2026, 11, 24), ["A", "B", None])
    assert S.was_in_last_sheet(st, "A", date(2026, 11, 25)) and not S.was_in_last_sheet(st, "C", date(2026, 11, 25))
    assert st.sheets[-1]["account_ids"] == ["A", "B"]
    assert st.report["record_sheet"] == {"recorded": 2, "dropped_no_id": 1}


def test_sheet_only_newest_last_sheet_counts():
    st = S.record_sheet(S.State(), date(2026, 11, 23), ["A"])
    st = S.record_sheet(st, date(2026, 11, 24), ["B"])
    assert S.was_in_last_sheet(st, "B", date(2026, 11, 25)) and not S.was_in_last_sheet(st, "A", date(2026, 11, 25))


def test_sheet_keeps_last_three():
    st = S.State()
    for d in range(1, 6):
        st = S.record_sheet(st, date(2026, 11, d), [f"A{d}"])
    assert [s["run_date"] for s in st.sheets] == ["2026-11-03", "2026-11-04", "2026-11-05"]


def test_sheet_same_date_replaces_and_dedupes():
    st = S.record_sheet(S.State(), date(2026, 11, 24), ["A", "A"])
    st = S.record_sheet(st, date(2026, 11, 24), ["B"])
    assert len(st.sheets) == 1 and st.sheets[0]["account_ids"] == ["B"]


def test_sheet_backfill_does_not_displace_newest():
    st = S.record_sheet(S.State(), date(2026, 11, 24), ["NEW"])
    st = S.record_sheet(st, date(2026, 11, 20), ["OLD"])
    assert S.was_in_last_sheet(st, "NEW", date(2026, 11, 25)) and not S.was_in_last_sheet(st, "OLD", date(2026, 11, 25))


def test_was_in_last_sheet_empty_state_and_none():
    assert not S.was_in_last_sheet(S.State(), "A", date(2026, 11, 25))
    assert not S.was_in_last_sheet(S.record_sheet(S.State(), date(2026, 11, 24), ["A"]), None, date(2026, 11, 25))


# ---------------------------------------------------------------- email history
def test_dotted_vs_dotless_email_match():
    st = S.record_sent_emails(S.State(), [sent_email(addr="patone@example.com")], [pros()])
    recent, _ = S.emailed_recently(st, "pat.one@example.com", date(2026, 11, 22), 7)
    assert recent                                                   # Gmail dotless == real dotted
    assert S.emailed_recently(st, "PatOne@Example.com", date(2026, 11, 22), 7)[0]


def test_emailed_recently_boundary():
    st = S.record_sent_emails(S.State(), [sent_email(when="2026-11-18T01:00:00Z")], [pros()])
    assert S.emailed_recently(st, "pat.one@example.com", date(2026, 11, 24), 7)[0]       # 6 days
    assert not S.emailed_recently(st, "pat.one@example.com", date(2026, 11, 25), 7)[0]   # 7 days


def test_emailed_recently_outside_window_reason():
    st = S.record_sent_emails(S.State(), [sent_email(when="2026-10-01T01:00:00Z")], [pros()])
    recent, reason = S.emailed_recently(st, "pat.one@example.com", date(2026, 11, 25), 7)
    assert not recent and "outside" in reason


def test_date_unknown_sent_email_treated_as_recent_and_says_so():
    st = S.record_sent_emails(S.State(), [sent_email(when=None)], [pros()], today=date(2026, 11, 25))
    recent, reason = S.emailed_recently(st, "pat.one@example.com", date(2026, 11, 26), 7)
    assert recent and "unknown" in reason and "2026-12-02" in reason


def test_date_unknown_does_not_set_account_last_email():
    st = S.record_sent_emails(S.State(), [sent_email(when=None)], [pros()])
    assert "FAKE-ACC-1" not in st.accounts
    assert st.report["record_sent_emails"]["date_unknown"] == 1


def test_date_later_known_upgrades_unknown_entry():
    st = S.record_sent_emails(S.State(), [sent_email("M1", when=None)], [pros()])
    st = S.record_sent_emails(st, [sent_email("M1", when="2026-10-01T01:00:00Z")], [pros()])
    recent, _ = S.emailed_recently(st, "pat.one@example.com", date(2026, 11, 25), 7)
    assert not recent


def test_never_emailed_and_bad_address():
    assert S.emailed_recently(S.State(), "x@example.com", date(2026, 11, 1), 7) == (False, "no sent email on record")
    assert S.emailed_recently(S.State(), None, date(2026, 11, 1), 7)[0] is False
    assert S.emailed_recently(S.State(), "not-an-email", date(2026, 11, 1), 7)[0] is False


def test_record_sent_emails_idempotent():
    s1 = S.record_sent_emails(S.State(), [sent_email("M1")], [pros()])
    s2 = S.record_sent_emails(s1, [sent_email("M1")], [pros()])
    assert _d(s1) == _d(s2)
    assert s2.report["record_sent_emails"]["duplicate"] == 1


def test_record_sent_emails_guards_missing_ids_and_unknown_recipient():
    bad = SentEmail(message_id="", thread_id=None, recipient="", recipient_key="", sent_at=None)
    st = S.record_sent_emails(S.State(), [bad, sent_email("M9", addr="stranger@example.com")], [pros()])
    assert st.report["record_sent_emails"]["skipped_no_key"] == 1
    assert "FAKE-ACC-1" not in st.accounts and len(st.emails) == 1
    assert "stranger@example.com" not in json.dumps(_d(st))   # no raw email at rest


def test_scenario_emails(scenario):
    prospects, _, sent = scenario
    st = S.record_sent_emails(S.State(), sent, prospects, today=date(2026, 11, 25))
    assert S.emailed_recently(st, "pat.one@example.com", date(2026, 11, 22), 7)[0]
    recent, reason = S.emailed_recently(st, "sam.two@example.com", date(2026, 11, 30), 7)
    assert recent and "unknown" in reason


# ---------------------------------------------------------------- verify_dispositions
def test_verify_dispositions_all_present(config):
    idx = build_disposition_index(load_fixture("nooks_call_dispositions.json")["data"])
    assert S.verify_dispositions(config, idx) == []


def test_verify_dispositions_reports_missing_names(config):
    rows = [r for r in load_fixture("nooks_call_dispositions.json")["data"]
            if r["name"] not in ("Left Voicemail", "Do Not Call")]
    assert S.verify_dispositions(config, build_disposition_index(rows)) == ["Left Voicemail", "Do Not Call"]


def test_verify_dispositions_empty_index(config):
    assert len(S.verify_dispositions(config, {})) == 5


# ---------------------------------------------------------------- persistence
def test_round_trip_save_load(config, scenario, tmp_path):
    prospects, calls, sent = scenario
    st = S.record_calls(S.State(), calls, prospects, config=config)
    st = S.record_sent_emails(st, sent, prospects)
    st = S.record_sheet(st, date(2026, 11, 24), ["FAKE-ACC-1"])
    S.save_state(st, tmp_path)
    back = S.load_state(tmp_path)
    assert _d(back) == _d(st)
    assert back.accounts["FAKE-ACC-2"].last_connect_at == st.accounts["FAKE-ACC-2"].last_connect_at
    assert back.dnc_prospect_ids == {"fake-p6"} and back.dnc_phones == {"+61400000016"}


def test_save_creates_missing_directory(tmp_path):
    S.save_state(S.State(), tmp_path / "nested" / "state")
    assert (tmp_path / "nested" / "state" / S.STATE_FILENAME).exists()


def test_file_is_valid_json_with_version(tmp_path):
    S.save_state(S.State(), tmp_path)
    assert json.loads((tmp_path / S.STATE_FILENAME).read_text())["version"] == S.SCHEMA_VERSION


def test_missing_file_gives_empty_state(tmp_path, caplog):
    st = S.load_state(tmp_path / "nope")
    assert _d(st) == _d(S.State())


def test_corrupt_file_gives_empty_state_warns_and_is_kept(tmp_path, caplog):
    (tmp_path / S.STATE_FILENAME).write_text("{ not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="src.state"):
        st = S.load_state(tmp_path)
    assert st.accounts == {} and "could not load" in caplog.text
    assert (tmp_path / "state.json.corrupt-1").read_text() == "{ not json"   # never destroyed


def test_corrupt_file_not_overwritten_by_later_save(tmp_path):
    (tmp_path / S.STATE_FILENAME).write_text("garbage", encoding="utf-8")
    S.save_state(S.load_state(tmp_path), tmp_path)
    assert (tmp_path / "state.json.corrupt-1").read_text() == "garbage"
    assert S.load_state(tmp_path).version == S.SCHEMA_VERSION


def test_binary_garbage_and_wrong_shapes_do_not_crash(tmp_path):
    p = tmp_path / S.STATE_FILENAME
    for payload in (b"\xff\xfe\x00bad", b"[]", b"null", b"42", b""):
        p.write_bytes(payload)
        assert S.load_state(tmp_path).accounts == {}
        for f in tmp_path.glob("state.json.corrupt-*"):
            f.unlink()


def test_future_or_missing_version_is_rejected(tmp_path):
    p = tmp_path / S.STATE_FILENAME
    p.write_text(json.dumps({"version": 99, "accounts": {"A": {}}}), encoding="utf-8")
    assert S.load_state(tmp_path).accounts == {}
    p.write_text(json.dumps({"accounts": {}}), encoding="utf-8")
    assert S.load_state(tmp_path).accounts == {}


def test_malformed_entries_skipped_rest_kept(tmp_path, caplog):
    raw = {"version": 2, "accounts": {"GOOD": {"last_outcome": "connect"}, "BAD": "oops"},
           "prospects": {"P": 5}, "emails": {"k": "x", "k2": {"m": {"sent_on": "x"}}}, "sheets": [{"run_date": "nonsense"}, "x",
           {"run_date": "2026-11-24", "account_ids": ["A", 3]}]}
    (tmp_path / S.STATE_FILENAME).write_text(json.dumps(raw), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="src.state"):
        st = S.load_state(tmp_path)
    assert list(st.accounts) == ["GOOD"] and st.prospects == {} and st.emails == {}
    assert st.sheets == [{"run_date": "2026-11-24", "account_ids": ["A"]}]
    assert "skipped malformed" in caplog.text and len(st.load_warnings) == 6


def test_atomic_write_leaves_no_temp_files(tmp_path):
    S.save_state(S.State(), tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == [S.STATE_FILENAME]


def test_failed_write_keeps_old_file_and_leaves_no_partial(tmp_path, monkeypatch):
    good = S.record_sheet(S.State(), date(2026, 11, 24), ["A"])
    S.save_state(good, tmp_path)
    before = (tmp_path / S.STATE_FILENAME).read_text()

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        S.save_state(S.State(), tmp_path)
    monkeypatch.undo()
    assert (tmp_path / S.STATE_FILENAME).read_text() == before
    assert [p.name for p in tmp_path.iterdir()] == [S.STATE_FILENAME]      # temp file cleaned up


def test_failed_serialisation_leaves_no_partial(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise TypeError("cannot serialise")
    monkeypatch.setattr(S.json, "dump", boom)
    with pytest.raises(TypeError):
        S.save_state(S.State(), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_state_dir_comes_from_config_paths(config):
    assert config.paths.state_dir == "state"


def test_naive_datetimes_in_file_are_read_as_utc(tmp_path):
    raw = {"version": 2, "accounts": {"A": {"last_call_at": "2026-11-10T01:00:00"}}}
    (tmp_path / S.STATE_FILENAME).write_text(json.dumps(raw), encoding="utf-8")
    assert S.load_state(tmp_path).accounts["A"].last_call_at == datetime(2026, 11, 10, 1, tzinfo=timezone.utc)


# ================================================================= review fixes (docs/review_1c.md)
from zoneinfo import ZoneInfo  # noqa: E402


class FakeAcct:
    def __init__(self, tz=None, nooks=None, zi=None):
        self.timezone, self.nooks_account_id, self.zoominfo_id = tz, nooks, zi


def local(y, mo, d, h, mi, tz):
    """Aware UTC iso string for a wall-clock time in ``tz``."""
    return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(tz)).astimezone(timezone.utc).isoformat()


# ---- F1 / F8: reset flag and require_healthy
def test_corrupt_file_sets_was_reset_and_reason(tmp_path):
    (tmp_path / S.STATE_FILENAME).write_text("{trunc", encoding="utf-8")
    st = S.load_state(tmp_path)
    assert st.was_reset and "unusable" in st.reset_reason and not st.is_new


def test_missing_file_is_new_and_gated_for_live(tmp_path):
    st = S.load_state(tmp_path)
    assert st.is_new and not st.was_reset
    with pytest.raises(S.StateResetError, match="first run"):
        S.require_healthy(st)                           # a missing file looks like a deleted one
    S.require_healthy(st, live=False)                   # dry run is allowed


def test_reset_state_saved_then_reloaded_stays_unhealthy(tmp_path):
    """R1: a dry run that saves a reset state must not launder the gate."""
    (tmp_path / S.STATE_FILENAME).write_text("{trunc", encoding="utf-8")
    st = S.load_state(tmp_path)
    S.require_healthy(st, live=False)                   # dry run continues
    S.save_state(st, tmp_path)                          # ...and saves the empty state
    again = S.load_state(tmp_path)
    assert not again.was_reset and not again.is_new     # looks like a normal file now
    with pytest.raises(S.StateResetError):
        S.require_healthy(again)                        # but the marker is missing: still unhealthy


def test_marker_persists_across_save_load(config, tmp_path):
    calls = [call(f"C{i}", when=f"2026-11-0{i}T01:00:00Z") for i in range(1, 4)]
    st = rec(calls, config=config)
    st = S.mark_dnc_rebuilt(st, date(2026, 11, 25), 3, reached_last_page=True)
    S.require_healthy(st)
    S.save_state(st, tmp_path)
    back = S.load_state(tmp_path)
    S.require_healthy(back)
    assert back.dnc_rebuilt_at == "2026-11-25" and back.dnc_rebuilt_calls == 3
    assert back.dnc_rebuilt_earliest == "2026-11-01" and not back.dnc_rebuilt_empty


def test_rebuild_after_reset_clears_gate(config, scenario, tmp_path):
    (tmp_path / S.STATE_FILENAME).write_text("bad", encoding="utf-8")
    st = S.load_state(tmp_path)
    prospects, calls, _ = scenario
    st = S.record_calls(st, calls, prospects, config=config)
    st = S.mark_dnc_rebuilt(st, date(2026, 11, 25), st.report["record_calls"]["calls_seen"], reached_last_page=True)
    S.require_healthy(st)
    assert "fake-p6" in st.dnc_prospect_ids and st.dnc_rebuilt_earliest == "2026-10-01"


@pytest.mark.parametrize("n", [-1, None, True, "5", 1.5])
def test_rebuild_bad_count_rejected(n):
    with pytest.raises(ValueError):
        S.mark_dnc_rebuilt(S.State(), date(2026, 11, 25), n, reached_last_page=True)


def test_rebuild_from_zero_calls_rejected_without_confirmation():
    with pytest.raises(ValueError, match="empty_history_confirmed"):
        S.mark_dnc_rebuilt(S.State(), date(2026, 11, 25), 0, reached_last_page=True)
    with pytest.raises(ValueError):
        S.mark_dnc_rebuilt(S.State(), date(2026, 11, 25), 0, reached_last_page=True, empty_history_confirmed=False)


def test_rebuild_from_zero_calls_allowed_only_when_confirmed_and_last_page(tmp_path):
    with pytest.raises(ValueError):                      # confirmed but did not reach the last page
        S.mark_dnc_rebuilt(S.State(), date(2026, 11, 25), 0, reached_last_page=False, empty_history_confirmed=True)
    st = S.mark_dnc_rebuilt(S.State(), date(2026, 11, 25), 0, reached_last_page=True, empty_history_confirmed=True)
    assert S.dnc_is_rebuilt(st) and st.dnc_rebuilt_empty and st.dnc_rebuilt_earliest is None
    S.require_healthy(st)
    S.save_state(st, tmp_path)
    back = S.load_state(tmp_path)
    assert S.dnc_is_rebuilt(back) and back.dnc_rebuilt_empty
    S.require_healthy(back)


def test_empty_confirmation_rejected_when_calls_already_recorded(config):
    st = rec([call("C1")], config=config)
    with pytest.raises(ValueError, match="already holds"):
        S.mark_dnc_rebuilt(st, date(2026, 11, 25), 0, reached_last_page=True, empty_history_confirmed=True)


def test_partial_history_reached_last_page_false_rejected(config):
    st = rec([call("C1")], config=config)
    with pytest.raises(ValueError, match="last page"):
        S.mark_dnc_rebuilt(st, date(2026, 11, 25), 1, reached_last_page=False)
    for bad in (None, 1, "yes"):
        with pytest.raises(ValueError):
            S.mark_dnc_rebuilt(st, date(2026, 11, 25), 1, reached_last_page=bad)


def test_reached_last_page_is_required_keyword():
    with pytest.raises(TypeError):
        S.mark_dnc_rebuilt(S.State(), date(2026, 11, 25), 1)


def test_calls_seen_must_cover_distinct_recorded_calls(config):
    st = rec([call("C1"), call("C2"), call("C3")], config=config)
    with pytest.raises(ValueError, match="partial"):
        S.mark_dnc_rebuilt(st, date(2026, 11, 25), 2, reached_last_page=True)
    assert S.dnc_is_rebuilt(S.mark_dnc_rebuilt(st, date(2026, 11, 25), 3, reached_last_page=True))


def test_nonzero_count_with_no_calls_recorded_rejected():
    with pytest.raises(ValueError, match="not been run"):
        S.mark_dnc_rebuilt(S.State(), date(2026, 11, 25), 5, reached_last_page=True)


def test_earliest_call_includes_unmatched_calls(config):
    st = rec([call("C1", when="2026-11-10T01:00:00Z"),
              call("C2", pid=None, to="+61400000555", when="2026-09-01T01:00:00Z")], config=config)
    assert st.earliest_call_at == _dt("2026-09-01T01:00:00Z")
    assert S.mark_dnc_rebuilt(st, date(2026, 11, 25), 2, reached_last_page=True).dnc_rebuilt_earliest == "2026-09-01"


def test_marker_not_set_by_record_calls_or_other_steps(config, scenario):
    prospects, calls, sent = scenario
    st = S.record_calls(S.State(), calls, prospects, config=config)
    st = S.record_sent_emails(S.record_sheet(st, date(2026, 11, 24), ["A"]), sent, prospects)
    assert not S.dnc_is_rebuilt(st) and st.dnc_rebuilt_at is None
    with pytest.raises(S.StateResetError):
        S.require_healthy(st)


def test_marker_survives_record_steps(config):
    st = S.mark_dnc_rebuilt(S.State(), date(2026, 11, 25), 0, reached_last_page=True, empty_history_confirmed=True)
    st = S.record_calls(st, [call("C1")], [pros()], config=config)
    st = S.record_sheet(st, date(2026, 11, 25), ["A"])
    assert S.dnc_is_rebuilt(st)


def test_dry_run_gate_logs_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="src.state"):
        S.require_healthy(S.State(), live=False)
    assert "not trusted" in caplog.text


def test_record_calls_report_has_calls_seen(config):
    st = rec([call("C1"), call("C2", pid=None, to="+61400000555")], config=config)
    assert st.report["record_calls"]["calls_seen"] == 2


def test_bad_marker_values_reset(tmp_path):
    for bad in ({"dnc_rebuilt_at": "junk"}, {"dnc_rebuilt_calls": "9"}, {"dnc_rebuilt_calls": True}):
        (tmp_path / S.STATE_FILENAME).write_text(json.dumps({"version": 2, **bad}), encoding="utf-8")
        st = S.load_state(tmp_path)
        assert st.was_reset and not S.dnc_is_rebuilt(st)
        for f in tmp_path.glob("state.json.corrupt-*"):
            f.unlink()


def test_marker_with_zero_calls_in_file_is_not_trusted(tmp_path):
    (tmp_path / S.STATE_FILENAME).write_text(
        json.dumps({"version": 2, "dnc_rebuilt_at": "2026-11-25", "dnc_rebuilt_calls": 0}), encoding="utf-8")
    with pytest.raises(S.StateResetError):
        S.require_healthy(S.load_state(tmp_path))


@pytest.mark.parametrize("raw", [
    {"version": 2, "prospects": "x"}, {"version": 2, "accounts": []}, {"version": 2, "emails": 5},
    {"version": 2, "sheets": {}}, {"version": 2, "seen_calls": []},
    {"version": 2, "dnc_prospect_ids": "abc"}, {"version": 2, "dnc_phones": [1, 2]},
    {"version": 2, "dnc_emails": {"a": 1}}, {"version": 1}, {"version": 3}, [], "str", None, 7])
def test_wrong_container_types_reset_and_never_crash(tmp_path, raw):
    (tmp_path / S.STATE_FILENAME).write_text(json.dumps(raw), encoding="utf-8")
    st = S.load_state(tmp_path)
    assert st.was_reset and st.reset_reason
    assert (tmp_path / "state.json.corrupt-1").exists()


def test_dnc_string_is_not_split_into_characters(tmp_path):
    (tmp_path / S.STATE_FILENAME).write_text(json.dumps({"version": 2, "dnc_prospect_ids": "abc"}), encoding="utf-8")
    assert S.load_state(tmp_path).dnc_prospect_ids == set()


def test_reset_flags_are_not_saved(tmp_path):
    st = S.State(was_reset=True, reset_reason="x")
    S.save_state(st, tmp_path)
    assert not S.load_state(tmp_path).was_reset


# ---- F12: account-local days
def test_sydney_morning_no_answer_plus_three_local_days(config):
    c = call("C1", when=local(2026, 11, 2, 8, 50, "Australia/Sydney"))      # AEDT: UTC date is Nov 1
    st = rec([c], accounts_by_id={"FAKE-ACC-1": FakeAcct("Australia/Sydney")}, config=config)
    assert st.accounts["FAKE-ACC-1"].last_call_date == "2026-11-02"
    assert S.cooldown_until(st, config)["FAKE-ACC-1"] == date(2026, 11, 5)
    assert S.is_in_cooldown(st, "FAKE-ACC-1", date(2026, 11, 4), config)
    assert not S.is_in_cooldown(st, "FAKE-ACC-1", date(2026, 11, 5), config)


def test_sydney_morning_connect_plus_fourteen_local_days(config):
    c = call("C1", disp="Interested", dur=60, when=local(2026, 11, 2, 8, 50, "Australia/Sydney"))
    st = rec([c], accounts_by_id={"FAKE-ACC-1": FakeAcct("Australia/Sydney")}, config=config)
    assert S.cooldown_until(st, config)["FAKE-ACC-1"] == date(2026, 11, 16)


def test_auckland_morning_no_answer_and_connect(config):
    tz = "Pacific/Auckland"                                                  # NZDT: UTC date is Nov 1
    c = call("C1", when=local(2026, 11, 2, 8, 50, tz))
    st = rec([c], accounts_by_id={"FAKE-ACC-1": FakeAcct(tz)}, config=config)
    assert S.cooldown_until(st, config)["FAKE-ACC-1"] == date(2026, 11, 5)
    c2 = call("C2", disp="Interested", dur=60, when=local(2026, 11, 2, 8, 50, tz))
    st2 = rec([c2], accounts_by_id={"FAKE-ACC-1": FakeAcct(tz)}, config=config)
    assert S.cooldown_until(st2, config)["FAKE-ACC-1"] == date(2026, 11, 16)


def test_aest_winter_morning_also_local(config):
    c = call("C1", when=local(2026, 7, 6, 9, 0, "Australia/Sydney"))        # AEST UTC+10
    st = rec([c], accounts_by_id={"FAKE-ACC-1": FakeAcct("Australia/Sydney")}, config=config)
    assert S.cooldown_until(st, config)["FAKE-ACC-1"] == date(2026, 7, 9)


def test_perth_and_afternoon_calls_unchanged(config):
    c = call("C1", when=local(2026, 11, 2, 15, 0, "Australia/Sydney"))
    st = rec([c], accounts_by_id={"FAKE-ACC-1": FakeAcct("Australia/Sydney")}, config=config)
    assert S.cooldown_until(st, config)["FAKE-ACC-1"] == date(2026, 11, 5)
    c = call("C2", when=local(2026, 11, 2, 23, 30, "Australia/Perth"))
    st = rec([c], accounts_by_id={"FAKE-ACC-1": FakeAcct("Australia/Perth")}, config=config)
    assert st.accounts["FAKE-ACC-1"].last_call_date == "2026-11-02"


def test_unknown_or_none_timezone_defaults_to_sydney_not_utc(config):
    c = call("C1", when="2026-11-01T21:50:00Z")                              # = Nov 2 08:50 Sydney
    for acct in ({}, {"FAKE-ACC-1": FakeAcct(None)}):
        st = rec([c], accounts_by_id=acct, config=config)
        assert st.accounts["FAKE-ACC-1"].last_call_date == "2026-11-02"


@pytest.mark.parametrize("bad", ["Mars/Olympus", "", "../../etc/passwd", 42, "UTC+11"])
def test_invalid_timezone_does_not_crash(config, bad):
    st = rec([call("C1", when="2026-11-01T21:50:00Z")],
             accounts_by_id={"FAKE-ACC-1": FakeAcct(bad)}, config=config)
    assert st.accounts["FAKE-ACC-1"].last_call_date == "2026-11-02"


def test_local_dates_survive_save_load(config, tmp_path):
    c = call("C1", when=local(2026, 11, 2, 8, 50, "Australia/Sydney"))
    st = rec([c], accounts_by_id={"FAKE-ACC-1": FakeAcct("Australia/Sydney")}, config=config)
    S.save_state(st, tmp_path)
    assert S.cooldown_until(S.load_state(tmp_path), config)["FAKE-ACC-1"] == date(2026, 11, 5)


def test_last_touched_uses_local_date(config):
    c = call("C1", when=local(2026, 11, 2, 8, 50, "Australia/Sydney"))
    st = rec([c], accounts_by_id={"FAKE-ACC-1": FakeAcct("Australia/Sydney")}, config=config)
    assert S.last_touched(st)["FAKE-ACC-1"] == date(2026, 11, 2)


def test_email_sent_date_is_account_local():
    se = sent_email(when=local(2026, 11, 2, 8, 50, "Australia/Sydney"))
    st = S.record_sent_emails(S.State(), [se], [pros()], {"FAKE-ACC-1": FakeAcct("Australia/Sydney")})
    assert st.accounts["FAKE-ACC-1"].last_email_date == "2026-11-02"
    assert S.emailed_recently(st, "pat.one@example.com", date(2026, 11, 8), 7)[0]
    assert not S.emailed_recently(st, "pat.one@example.com", date(2026, 11, 9), 7)[0]


def test_lookup_by_nooks_and_zoominfo_ids(config):
    acct = FakeAcct("Australia/Sydney", nooks="FAKE-NA-1", zi="FAKE-ZI-1")
    st = rec([call("C1")], accounts_by_id={"FAKE-ACC-1": acct}, config=config)
    base = S.cooldown_until(st, config)["FAKE-ACC-1"]
    assert S.cooldown_until_by(st, config, "nooks_account_id") == {"FAKE-NA-1": base}
    assert S.cooldown_until_by(st, config, "zoominfo_id") == {"FAKE-ZI-1": base}
    with pytest.raises(ValueError):
        S.cooldown_until_by(st, config, "name")


def test_lookup_by_alt_id_skips_accounts_without_it(config):
    st = rec([call("C1")], config=config)       # no accounts_by_id: nooks id falls back to the prospect's (None here)
    assert S.cooldown_until_by(st, config, "zoominfo_id") == {}


# ---- F2: duplicate prospect, same email
def test_duplicate_prospect_same_email_stays_blocked(config):
    st = rec([call("C1", disp="Do Not Call")], config=config)
    dup = pros(nid="FAKE-DUP", phone="+61400000777", email="PAT.ONE@example.com")
    blocked, reason = S.is_blocked(dup, st)
    assert blocked and "email" in reason
    assert S.is_blocked(dup, st, "call")[0] and S.is_blocked(dup, st, "email")[0]


def test_dnc_email_dotless_form_blocks_too(config):
    st = rec([call("C1", disp="Do Not Call")], config=config)
    assert S.is_blocked(pros(nid="D", phone=None, email="patone@example.com"), st)[0]


def test_dnc_email_learned_when_prospect_listed_later(config):
    st = rec([call("C1", disp="Do Not Call")], prospects=[], config=config)          # prospect unknown yet
    assert st.dnc_emails == set()
    st = rec([], prospects=[pros()], state=st, config=config)                         # now the list has the email
    assert S.is_blocked(pros(nid="D", phone=None, email=None), st)[0] is False
    assert S.is_blocked(pros(nid="D", phone=None, email="pat.one@example.com"), st)[0]


def test_no_raw_email_stored_in_state_file(config, tmp_path):
    st = rec([call("C1", disp="Do Not Call")], config=config)
    st = S.record_sent_emails(st, [sent_email()], [pros()])
    S.save_state(st, tmp_path)
    text = (tmp_path / S.STATE_FILENAME).read_text()
    assert "example.com" not in text and "patone" not in text


def test_different_email_not_blocked(config):
    st = rec([call("C1", disp="Do Not Call")], config=config)
    assert not S.is_blocked(pros(nid="O", phone=None, email="other@example.com"), st)[0]


# ---- F3: disposition spelling variants
@pytest.mark.parametrize("name", ["Do Not Call.", "DO  NOT CALL", "Do-Not-Call", "do not call",
                                   "  Do Not Call\n", "Do\u00a0Not\u00a0Call", "Do_Not_Call!!", "DO NOT CALL "])
def test_dnc_name_variants_fail_closed(config, name):
    c = call("C1", disp=name)
    assert S.do_not_call_prospects([c], config) == {"fake-p1"}
    assert "fake-p1" in rec([c], config=config).dnc_prospect_ids


@pytest.mark.parametrize("name", ["Do Not Call (DNC)", "Not Do Call", "Do Not Callback", "Call", "", None])
def test_dnc_name_non_matches(config, name):
    assert S.do_not_call_prospects([call("C1", disp=name)], config) == set()


def test_voicemail_variants_still_no_contact(config):
    assert S.call_outcome(call(disp="LEFT  VOICEMAIL.", dur=500), config) == S.NO_CONTACT
    assert S.call_outcome(call(disp="bad number - wrong number", dur=500), config) == S.NO_CONTACT


# ---- F4: phone normalisation
def test_spaced_dnc_phone_blocks_canonical_number(config):
    c = call("C1", pid=None, disp="Do Not Call", to="+61 400 000 010")
    st = rec([c], prospects=[], config=config)
    assert S.is_blocked(pros(nid="X", phone="+61400000010", email=None), st)[0]
    assert S.is_blocked(pros(nid="Y", phone="+61 400 000 010", email=None), st)[0]


def test_unnormalisable_phone_still_blocks_exact_stripped_value(config):
    st = rec([call("C1", pid=None, disp="Do Not Call", to="(12) 345")], prospects=[], config=config)
    assert "12345" in st.dnc_phones or any("345" in p for p in st.dnc_phones)


def test_spaced_unlinked_call_matches_one_prospect_by_phone(config):
    st = rec([call("C1", pid=None, to="+61 400 000 011")], config=config)
    assert st.prospects["FAKE-P1"].call_count == 1


def test_do_not_call_phones_canonical(config):
    c = call("C1", pid=None, disp="Do Not Call", to="+61 400 000 010")
    assert S.do_not_call_phones([c], config) == {"+61400000010"}


# ---- F7: DNC is never a connect
def test_dnc_never_a_connect_whatever_duration(config):
    assert S.call_outcome(call(disp="Do Not Call", dur=600), config) == S.NO_CONTACT
    st = rec([call("C1", disp="Do Not Call", dur=600)], config=config)
    assert st.prospects["FAKE-P1"].connect_count == 0
    assert st.accounts["FAKE-ACC-1"].last_connect_at is None


# ---- F5: deterministic ties
def test_equal_timestamps_order_independent(config):
    a = call("C-A", disp="Interested", dur=60, when="2026-11-10T01:00:00Z")
    b = call("C-B", disp="No Answer", dur=3, when="2026-11-10T01:00:00Z")
    one = rec([a, b], config=config)
    two = rec([b, a], config=config)
    assert _d(one) == _d(two)
    assert one.accounts["FAKE-ACC-1"].last_call_key == "C-B"


def test_ties_across_batches_order_independent(config):
    a = call("C-A", disp="Interested", dur=60, when="2026-11-10T01:00:00Z")
    b = call("C-B", disp="No Answer", dur=3, when="2026-11-10T01:00:00Z")
    x = rec([b], state=rec([a], config=config), config=config)
    y = rec([a], state=rec([b], config=config), config=config)
    assert _d(x) == _d(y)


def test_shuffled_inputs_give_identical_state(config, scenario):
    import random
    prospects, calls, _ = scenario
    ref = _d(S.record_calls(S.State(), calls, prospects, config=config))
    rnd = random.Random(7)
    for _ in range(20):
        shuffled = calls[:]
        rnd.shuffle(shuffled)
        assert _d(S.record_calls(S.State(), shuffled, prospects, config=config)) == ref


# ---- F11: undated emails expire
def test_undated_email_expires_after_first_seen_plus_days():
    st = S.record_sent_emails(S.State(), [sent_email(when=None)], [pros()], today=date(2026, 11, 1))
    assert S.emailed_recently(st, "pat.one@example.com", date(2026, 11, 7), 7)[0]
    recent, reason = S.emailed_recently(st, "pat.one@example.com", date(2026, 11, 8), 7)
    assert not recent and "expired 2026-11-08" in reason


def test_undated_reason_names_first_seen_and_expiry():
    st = S.record_sent_emails(S.State(), [sent_email(when=None)], [pros()], today=date(2026, 11, 1))
    _, reason = S.emailed_recently(st, "pat.one@example.com", date(2026, 11, 2), 7)
    assert "date is unknown" in reason and "first seen 2026-11-01" in reason and "until 2026-11-08" in reason


def test_undated_first_seen_not_reset_by_resighting():
    st = S.record_sent_emails(S.State(), [sent_email(when=None)], [pros()], today=date(2026, 11, 1))
    st = S.record_sent_emails(st, [sent_email(when=None)], [pros()], today=date(2026, 11, 6))
    assert not S.emailed_recently(st, "pat.one@example.com", date(2026, 11, 8), 7)[0]


def test_old_dated_plus_expired_undated_not_recent_forever():
    st = S.record_sent_emails(S.State(), [sent_email("M1", when="2026-09-01T00:00:00Z"),
                                          sent_email("M2", when=None)], [pros()], today=date(2026, 10, 1))
    assert not S.emailed_recently(st, "pat.one@example.com", date(2026, 11, 25), 7)[0]


def test_dated_email_still_recent_while_undated_expired():
    st = S.record_sent_emails(S.State(), [sent_email("M1", when="2026-11-24T00:00:00Z"),
                                          sent_email("M2", when=None)], [pros()], today=date(2026, 10, 1))
    assert S.emailed_recently(st, "pat.one@example.com", date(2026, 11, 25), 7)[0]


def test_undated_email_roundtrips_through_file(tmp_path):
    st = S.record_sent_emails(S.State(), [sent_email(when=None)], [pros()], today=date(2026, 11, 1))
    S.save_state(st, tmp_path)
    assert S.emailed_recently(S.load_state(tmp_path), "pat.one@example.com", date(2026, 11, 2), 7)[0]


def test_future_dated_email_reason_is_sane():
    st = S.record_sent_emails(S.State(), [sent_email(when="2026-12-30T00:00:00Z")], [pros()])
    recent, reason = S.emailed_recently(st, "pat.one@example.com", date(2026, 11, 25), 7)
    assert recent


# ================================================================= R2: Salesforce id normalisation
ID15, ID18 = "0015g00000AbCdE", "0015g00000AbCdEAAA"      # synthetic ids; same record, two forms


def _acct_state(config, stored_as, tz="Australia/Sydney", when="2026-11-10T01:00:00Z"):
    p = pros(acct=stored_as)
    return rec([call("C1", when=when)], prospects=[p],
               accounts_by_id={stored_as: FakeAcct(tz)}, config=config)


@pytest.mark.parametrize("stored,lookup", [(ID15, ID18), (ID18, ID15), (ID15, ID15), (ID18, ID18)])
def test_is_in_cooldown_both_id_forms(config, stored, lookup):
    st = _acct_state(config, stored)
    assert S.is_in_cooldown(st, lookup, date(2026, 11, 11), config)
    assert not S.is_in_cooldown(st, lookup, date(2026, 11, 13), config)


@pytest.mark.parametrize("stored,lookup", [(ID15, ID18), (ID18, ID15)])
def test_cooldown_until_and_last_touched_keyed_canonically(config, stored, lookup):
    st = _acct_state(config, stored)
    assert list(S.cooldown_until(st, config)) == [ID15]
    assert list(S.last_touched(st)) == [ID15]
    assert S.cooldown_until(st, config).get(S._sf(lookup)) == date(2026, 11, 13)


@pytest.mark.parametrize("stored,lookup", [(ID15, ID18), (ID18, ID15)])
def test_sheet_both_id_forms(stored, lookup):
    st = S.record_sheet(S.State(), date(2026, 11, 24), [stored, lookup, None])
    assert st.sheets[-1]["account_ids"] == [ID15]               # deduped across forms
    assert S.was_in_last_sheet(st, lookup, date(2026, 11, 25)) and S.was_in_last_sheet(st, stored, date(2026, 11, 25))
    assert S.last_sheet_ids(st, date(2026, 11, 25)) == {ID15}


def test_accounts_by_id_18_char_keys_supply_timezone(config):
    c = call("C1", when=local(2026, 11, 2, 8, 50, "Australia/Sydney"))
    st = rec([c], prospects=[pros(acct=ID15)], accounts_by_id={ID18: FakeAcct("Australia/Sydney")}, config=config)
    assert st.accounts[ID15].last_call_date == "2026-11-02"      # Sydney local, not the UTC date Nov 1


def test_accounts_by_id_15_char_keys_with_18_char_prospect(config):
    c = call("C1", when=local(2026, 11, 2, 8, 50, "Pacific/Auckland"))
    st = rec([c], prospects=[pros(acct=ID18)], accounts_by_id={ID15: FakeAcct("Pacific/Auckland", nooks="N1")},
             config=config)
    assert list(st.accounts) == [ID15] and st.accounts[ID15].last_call_date == "2026-11-02"
    assert st.accounts[ID15].nooks_account_id == "N1"


def test_prospect_and_state_use_same_key_across_forms(config):
    st = rec([call("C1")], prospects=[pros(acct=ID15)], config=config)
    st = rec([call("C2", when="2026-11-11T01:00:00Z")], prospects=[pros(acct=ID18)], state=st, config=config)
    assert list(st.accounts) == [ID15] and st.prospects["FAKE-P1"].call_count == 2


def test_record_sent_emails_both_id_forms():
    st = S.record_sent_emails(S.State(), [sent_email()], [pros(acct=ID18)],
                              {ID15: FakeAcct("Australia/Sydney")})
    assert list(st.accounts) == [ID15]
    assert S.last_touched(st) == {ID15: date(2026, 11, 20)}


def test_account_without_usable_id_in_prospect_is_no_account(config):
    st = rec([call("C1")], prospects=[pros(acct="   ")], config=config)
    assert st.accounts == {} and st.report["record_calls"]["no_account"] == 1


def test_none_and_blank_ids_everywhere(config):
    st = S.State()
    assert not S.is_in_cooldown(st, None, date(2026, 11, 1), config)
    assert not S.is_in_cooldown(st, "  ", date(2026, 11, 1), config)
    assert not S.was_in_last_sheet(S.record_sheet(st, date(2026, 11, 1), [None, "", " "]), None, date(2026, 11, 2))


def test_file_with_mixed_id_forms_migrates_on_load(config, tmp_path):
    raw = {"version": 2, "accounts": {
        ID18: {"last_call_at": "2026-11-10T01:00:00+00:00", "last_call_date": "2026-11-10",
               "last_call_key": "A", "last_outcome": "no_contact"},
        ID15: {"last_call_at": "2026-11-12T01:00:00+00:00", "last_call_date": "2026-11-12",
               "last_call_key": "B", "last_outcome": "connect",
               "last_connect_at": "2026-11-12T01:00:00+00:00", "last_connect_date": "2026-11-12",
               "zoominfo_id": "Z1"}},
        "sheets": [{"run_date": "2026-11-24", "account_ids": [ID18]}]}
    (tmp_path / S.STATE_FILENAME).write_text(json.dumps(raw), encoding="utf-8")
    st = S.load_state(tmp_path)
    assert list(st.accounts) == [ID15]
    a = st.accounts[ID15]
    assert a.last_call_date == "2026-11-12" and a.last_outcome == "connect" and a.zoominfo_id == "Z1"
    assert S.was_in_last_sheet(st, ID15, date(2026, 11, 25))
    assert S.cooldown_until(st, config)[ID15] == date(2026, 11, 26)


def test_synthetic_fake_ids_unchanged(config):
    st = rec([call("C1")], config=config)
    assert list(st.accounts) == ["FAKE-ACC-1"]


# ================================================================= R3: malformed stored dates
def _load(tmp_path, acct):
    (tmp_path / S.STATE_FILENAME).write_text(json.dumps({"version": 2, "accounts": {"A": acct}}), encoding="utf-8")
    return S.load_state(tmp_path)


def test_bad_last_call_date_falls_back_to_utc_instant_and_warns(config, tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="src.state"):
        st = _load(tmp_path, {"last_call_at": "2026-11-10T01:00:00+00:00", "last_call_date": "junk",
                              "last_outcome": "no_contact"})
    assert st.accounts["A"].last_call_date == "2026-11-10"
    assert S.cooldown_until(st, config)["A"] == date(2026, 11, 13)       # cooldown NOT dropped
    assert any("malformed last_call_date" in w for w in st.load_warnings)
    assert "malformed last_call_date" in caplog.text


def test_missing_date_with_instant_uses_utc_date_quietly(config, tmp_path):
    st = _load(tmp_path, {"last_call_at": "2026-11-10T01:00:00+00:00", "last_outcome": "no_contact"})
    assert st.accounts["A"].last_call_date == "2026-11-10" and st.load_warnings == []


def test_bad_connect_and_email_dates_fall_back(tmp_path):
    st = _load(tmp_path, {"last_connect_at": "2026-11-05T01:00:00+00:00", "last_connect_date": 5,
                          "last_email_at": "2026-11-06T01:00:00+00:00", "last_email_date": "x"})
    assert st.accounts["A"].last_connect_date == "2026-11-05"
    assert st.accounts["A"].last_email_date == "2026-11-06"
    assert len(st.load_warnings) == 2


def test_bad_date_and_no_instant_warns_and_stays_none(tmp_path):
    st = _load(tmp_path, {"last_call_date": "junk"})
    assert st.accounts["A"].last_call_date is None and st.load_warnings


def test_load_logs_summary_count(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="src.state"):
        _load(tmp_path, {"last_call_date": "junk"})
    assert "malformed entr" in caplog.text


# ================================================================= 1g integration review fixes
RUN = date(2026, 11, 25)


# ---- H1: same-night rerun
def test_same_night_sheet_is_not_last_nights_sheet():
    st = S.record_sheet(S.State(), RUN, ["A", "B"])
    assert S.last_sheet_ids(st, RUN) == set()
    assert not S.was_in_last_sheet(st, "A", RUN)


def test_last_sheet_ignores_today_and_future_uses_previous_night():
    st = S.record_sheet(S.State(), date(2026, 11, 24), ["YESTERDAY"])
    st = S.record_sheet(st, RUN, ["TODAY"])
    st = S.record_sheet(st, date(2026, 11, 26), ["TOMORROW"])
    assert S.last_sheet_ids(st, RUN) == {"YESTERDAY"}
    assert S.last_sheet_ids(st, date(2026, 11, 27)) == {"TOMORROW"}
    assert S.last_sheet_ids(st, date(2026, 11, 24)) == set()


def test_rerecording_same_date_replaces_not_appends():
    st = S.record_sheet(S.State(), RUN, ["A"])
    st = S.record_sheet(st, RUN, ["B", "C"])
    assert st.sheets == [{"run_date": "2026-11-25", "account_ids": ["B", "C"]}]


def test_run_date_is_required_for_last_sheet():
    with pytest.raises(TypeError):
        S.last_sheet_ids(S.State())
    with pytest.raises(TypeError):
        S.was_in_last_sheet(S.State(), "A")


def test_two_runs_same_night_see_same_last_sheet():
    base = S.record_sheet(S.State(), date(2026, 11, 24), ["A"])
    first = S.last_sheet_ids(base, RUN)
    after_first = S.record_sheet(base, RUN, ["B"])
    assert S.last_sheet_ids(after_first, RUN) == first == {"A"}


# ---- L1: shrink guard and .prev backup
def _populated(config, tmp_path):
    st = rec([call("C1", disp="Do Not Call")], config=config)
    st = S.mark_dnc_rebuilt(st, RUN, 1, reached_last_page=True)
    S.save_state(st, tmp_path)
    return st


def test_save_empty_state_over_populated_file_refused(config, tmp_path):
    _populated(config, tmp_path)
    before = (tmp_path / S.STATE_FILENAME).read_text()
    with pytest.raises(S.StateShrinkError) as e:
        S.save_state(S.State(), tmp_path)
    assert "Do Not Call prospect ids" in str(e.value) and "marker" in str(e.value)
    assert (tmp_path / S.STATE_FILENAME).read_text() == before
    assert [p.name for p in tmp_path.iterdir()] == [S.STATE_FILENAME]       # no temp or .prev left behind


def test_fewer_dnc_phones_or_emails_or_accounts_refused(config, tmp_path):
    st = rec([call("C1", disp="Do Not Call")], config=config)
    S.save_state(st, tmp_path)
    for field_name, empty in (("dnc_phones", set()), ("dnc_emails", set()), ("dnc_prospect_ids", set())):
        smaller = S.load_state(tmp_path)
        setattr(smaller, field_name, empty)
        with pytest.raises(S.StateShrinkError):
            S.save_state(smaller, tmp_path)
    fewer = S.load_state(tmp_path)
    fewer.accounts = {}
    with pytest.raises(S.StateShrinkError, match="accounts"):
        S.save_state(fewer, tmp_path)


def test_lost_marker_alone_refused(config, tmp_path):
    _populated(config, tmp_path)
    st = S.load_state(tmp_path)
    st.dnc_rebuilt_at = None
    with pytest.raises(S.StateShrinkError, match="marker"):
        S.save_state(st, tmp_path)


def test_allow_shrink_overrides_and_keeps_backup(config, tmp_path):
    _populated(config, tmp_path)
    before = (tmp_path / S.STATE_FILENAME).read_text()
    S.save_state(S.State(), tmp_path, allow_shrink=True)
    assert (tmp_path / "state.json.prev").read_text() == before
    assert json.loads((tmp_path / S.STATE_FILENAME).read_text())["dnc_prospect_ids"] == []


def test_normal_growing_save_succeeds_and_rotates_one_prev(config, tmp_path):
    st = rec([call("C1")], config=config)
    S.save_state(st, tmp_path)
    assert not (tmp_path / "state.json.prev").exists()                      # nothing to back up the first time
    first = (tmp_path / S.STATE_FILENAME).read_text()
    st2 = rec([call("C2", when="2026-11-11T01:00:00Z")], state=st, config=config)
    S.save_state(st2, tmp_path)
    second = (tmp_path / S.STATE_FILENAME).read_text()
    assert (tmp_path / "state.json.prev").read_text() == first
    S.save_state(rec([call("C3", when="2026-11-12T01:00:00Z")], state=st2, config=config), tmp_path)
    assert (tmp_path / "state.json.prev").read_text() == second            # one rotating copy, replaced
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json", "state.json.prev"]


def test_save_over_unreadable_file_is_not_blocked(tmp_path):
    (tmp_path / S.STATE_FILENAME).write_text("garbage", encoding="utf-8")
    S.save_state(S.State(), tmp_path)                                       # nothing trustworthy to compare with
    assert (tmp_path / "state.json.prev").read_text() == "garbage"


def test_failed_save_leaves_no_prev_tmp_and_old_file(config, tmp_path, monkeypatch):
    st = rec([call("C1")], config=config)
    S.save_state(st, tmp_path)
    before = (tmp_path / S.STATE_FILENAME).read_text()
    real = os.replace
    calls_ = {"n": 0}

    def flaky(src, dst):
        calls_["n"] += 1
        if str(dst).endswith(S.STATE_FILENAME):
            raise OSError("boom")
        return real(src, dst)
    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(OSError):
        S.save_state(rec([call("C2", when="2026-11-11T01:00:00Z")], state=st, config=config), tmp_path)
    monkeypatch.undo()
    assert (tmp_path / S.STATE_FILENAME).read_text() == before
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


# ---- L2: fallback date uses the account zone
def test_fallback_date_uses_stored_account_zone(config, tmp_path):
    c = call("C1", when="2026-11-24T23:00:00Z")                              # Wed 10:00 Sydney
    st = rec([c], accounts_by_id={"FAKE-ACC-1": FakeAcct("Australia/Sydney")}, config=config)
    assert st.accounts["FAKE-ACC-1"].tz == "Australia/Sydney"
    raw = S.state_to_dict(st)
    raw["accounts"]["FAKE-ACC-1"]["last_call_date"] = "garbage"
    (tmp_path / S.STATE_FILENAME).write_text(json.dumps(raw), encoding="utf-8")
    bad = S.load_state(tmp_path)
    assert bad.accounts["FAKE-ACC-1"].last_call_date == "2026-11-25"         # not the UTC date 11-24
    assert S.cooldown_until(bad, config)["FAKE-ACC-1"] == date(2026, 11, 28)
    assert any("Australia/Sydney" in w for w in bad.load_warnings)


def test_fallback_date_uses_auckland_when_stored(config, tmp_path):
    c = call("C1", when="2026-11-24T20:00:00Z")                              # Wed 09:00 Auckland (NZDT)
    st = rec([c], accounts_by_id={"FAKE-ACC-1": FakeAcct("Pacific/Auckland")}, config=config)
    raw = S.state_to_dict(st)
    raw["accounts"]["FAKE-ACC-1"]["last_call_date"] = 5
    (tmp_path / S.STATE_FILENAME).write_text(json.dumps(raw), encoding="utf-8")
    assert S.load_state(tmp_path).accounts["FAKE-ACC-1"].last_call_date == "2026-11-25"


def test_fallback_defaults_to_sydney_when_no_zone_stored(tmp_path):
    raw = {"version": 2, "accounts": {"A": {"last_call_at": "2026-11-24T23:00:00+00:00", "last_call_date": "junk"}}}
    (tmp_path / S.STATE_FILENAME).write_text(json.dumps(raw), encoding="utf-8")
    assert S.load_state(tmp_path).accounts["A"].last_call_date == "2026-11-25"


def test_invalid_stored_zone_falls_back_to_sydney(tmp_path):
    raw = {"version": 2, "accounts": {"A": {"last_call_at": "2026-11-24T23:00:00+00:00", "last_call_date": "junk",
                                            "tz": "Mars/Base"}}}
    (tmp_path / S.STATE_FILENAME).write_text(json.dumps(raw), encoding="utf-8")
    st = S.load_state(tmp_path)
    assert st.accounts["A"].last_call_date == "2026-11-25" and st.accounts["A"].tz is None


# ---- L4: counts persisted
def test_last_run_counts_saved_and_reloaded(config, scenario, tmp_path):
    prospects, calls, sent = scenario
    st = S.record_calls(S.State(), calls, prospects, config=config)
    st = S.record_sent_emails(st, sent, prospects, today=RUN)
    st = S.record_sheet(st, RUN, ["A", None])
    S.save_state(st, tmp_path)
    back = S.load_state(tmp_path)
    assert back.last_run["record_calls"]["new"] == 6 and back.last_run["record_calls"]["unmatched"] == 1
    assert back.last_run["record_calls"]["unmatched_reasons"] == {"no_match": 1}
    assert back.last_run["record_sent_emails"]["new"] == 2
    assert back.last_run["record_sheet"] == {"recorded": 1, "dropped_no_id": 1}


def test_last_run_has_counts_only_no_ids_or_pii(config, scenario, tmp_path):
    prospects, calls, sent = scenario
    st = S.record_sent_emails(S.record_calls(S.State(), calls, prospects, config=config), sent, prospects)
    S.save_state(st, tmp_path)
    text = json.dumps(json.loads((tmp_path / S.STATE_FILENAME).read_text())["last_run"])
    assert "FAKE-C6" not in text and "unmatched_call_ids" not in text and "example.com" not in text


def test_last_run_survives_a_save_without_new_report(config, tmp_path):
    st = rec([call("C1")], config=config)
    S.save_state(st, tmp_path)
    again = S.load_state(tmp_path)                       # empty report this run
    S.save_state(again, tmp_path)
    assert S.load_state(tmp_path).last_run["record_calls"]["new"] == 1


def test_malformed_last_run_ignored_with_warning(tmp_path):
    (tmp_path / S.STATE_FILENAME).write_text(json.dumps({"version": 2, "last_run": "x"}), encoding="utf-8")
    st = S.load_state(tmp_path)
    assert not st.was_reset and st.last_run == {} and "malformed last_run" in st.load_warnings


# ---- L5: performance
def test_record_calls_20k_calls_is_fast(config):
    import time
    prospects = [Prospect(nooks_id=f"P{i}", sf_contact_id=None, sf_account_id=f"A{i}", nooks_account_id=None,
                          name="x", phone_e164=f"+6140{i:07d}") for i in range(2000)]
    base = _dt("2026-10-01T00:00:00Z")
    calls = [CallRecord(nooks_id=f"C{i}", prospect_id=f"P{i % 2000}", disposition_name="No Answer", duration_s=5,
                        started_at=base.replace(minute=i % 60), to_e164=None) for i in range(20000)]
    t = time.perf_counter()
    st = S.State()
    for i in range(0, len(calls), 1000):                 # 20 pages of 1000
        st = S.record_calls(st, calls[i:i + 1000], prospects, config=config)
    elapsed = time.perf_counter() - t
    assert len(st.seen_calls) == 20000
    assert elapsed < 2.0, elapsed                        # target < 1 s; slack for a busy CI box


def test_clone_is_independent_of_original(config):
    st = rec([call("C1", disp="Do Not Call")], config=config)
    st = S.record_sent_emails(st, [sent_email()], [pros()])
    cp = S._clone(st)
    cp.accounts["FAKE-ACC-1"].last_outcome = "x"
    cp.dnc_phones.add("+1")
    cp.emails[next(iter(cp.emails))]["new"] = {}
    cp.sheets.append({})
    cp.report["record_calls"]["new"] = 99
    assert st.accounts["FAKE-ACC-1"].last_outcome != "x" and "+1" not in st.dnc_phones
    assert "new" not in st.emails[next(iter(st.emails))] and st.sheets == []
    assert st.report["record_calls"]["new"] == 1


# ---- L6: None ids counted and warned
def test_record_sheet_none_ids_counted_and_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="src.state"):
        st = S.record_sheet(S.State(), RUN, ["A", None, "", "  ", "B"])
    assert st.report["record_sheet"] == {"recorded": 2, "dropped_no_id": 3}
    assert "3 account(s) with no usable id" in caplog.text


def test_record_calls_no_account_warns(config, caplog):
    with caplog.at_level(logging.WARNING, logger="src.state"):
        rec([call("C1", pid="FAKE-GHOST")], config=config)
    assert "no Salesforce account" in caplog.text


def test_sent_email_without_account_counted(caplog):
    with caplog.at_level(logging.WARNING, logger="src.state"):
        st = S.record_sent_emails(S.State(), [sent_email("M1", addr="stranger@example.com")], [pros()])
    assert st.report["record_sent_emails"]["no_account"] == 1
    assert "no recipient with a Salesforce account" in caplog.text


# ================================================================= Do Not Call prospect ids are case-insensitive
@pytest.mark.parametrize("stored_as,asked_as", [
    ("FAKE-P1", "fake-p1"), ("fake-p1", "FAKE-P1"), ("Fake-P1", "fAKE-p1"),
    ("FAKE-P1", "  FAKE-P1  "), ("fake-p1", "\tFake-P1\n"), ("FAKE-P1", "FAKE-P1")])
def test_dnc_blocks_whatever_the_id_case_or_padding(config, stored_as, asked_as):
    st = rec([call("C1", pid=stored_as, disp="Do Not Call")], prospects=[], config=config)
    blocked, reason = S.is_blocked(pros(nid=asked_as, phone=None, email=None), st)
    assert blocked and reason
    assert S.is_blocked(pros(nid=asked_as, phone=None, email=None), st, "call")[0]
    assert S.is_blocked(pros(nid=asked_as, phone=None, email=None), st, "email")[0]


def test_dnc_ids_stored_stripped_and_lowercase(config):
    st = rec([call("C1", pid="  FAKE-P1 ", disp="Do Not Call"),
              call("C2", pid="fake-P1", disp="Do Not Call")], prospects=[], config=config)
    assert st.dnc_prospect_ids == {"fake-p1"}
    assert st.report["record_calls"]["dnc_added"] == 1                    # one person, counted once


def test_do_not_call_prospects_returns_canonical_ids(config):
    calls = [call("C1", pid="FAKE-P1", disp="Do Not Call"), call("C2", pid=" fake-p1", disp="Do Not Call"),
             call("C3", pid="   ", disp="Do Not Call"), call("C4", pid=None, disp="Do Not Call")]
    assert S.do_not_call_prospects(calls, config) == {"fake-p1"}


def test_mark_do_not_call_catches_case_variants(config):
    st = rec([call("C1", pid="FAKE-P1", disp="Do Not Call")], prospects=[], config=config)
    out = S.mark_do_not_call([pros(nid="fake-p1", phone=None, email=None), pros(nid="FAKE-P2", phone=None, email=None)], st)
    assert [p.do_not_call for p in out] == [True, False]


def test_prospect_id_case_difference_still_links_call_to_prospect(config):
    st = rec([call("C1", pid="fake-p1", disp="Do Not Call")], prospects=[pros(nid="FAKE-P1")], config=config)
    assert S.is_blocked(pros(nid="Fake-P1", phone=None, email="pat.one@example.com"), st)[0]
    assert st.dnc_emails                                                    # linked despite the case difference
    assert "FAKE-P1" in st.prospects and st.accounts["FAKE-ACC-1"].last_call_at   # history under the real id


def test_other_prospect_not_blocked_by_case_folding(config):
    st = rec([call("C1", pid="FAKE-P1", disp="Do Not Call")], prospects=[], config=config)
    assert not S.is_blocked(pros(nid="FAKE-P10", phone=None, email=None), st)[0]
    assert not S.is_blocked(pros(nid="FAKE-P", phone=None, email=None), st)[0]


def test_old_file_with_mixed_case_ids_merges_on_load(tmp_path):
    raw = {"version": 2, "dnc_prospect_ids": ["ABC", "abc", " Abc ", "XyZ", ""]}
    (tmp_path / S.STATE_FILENAME).write_text(json.dumps(raw), encoding="utf-8")
    st = S.load_state(tmp_path)
    assert st.dnc_prospect_ids == {"abc", "xyz"} and not st.was_reset
    assert S.is_blocked(pros(nid="aBc", phone=None, email=None), st)[0]
    assert S.is_blocked(pros(nid="XYZ", phone=None, email=None), st)[0]


def test_saving_a_merged_old_file_is_not_a_shrink(tmp_path):
    raw = {"version": 2, "dnc_prospect_ids": ["ABC", "abc"], "dnc_rebuilt_at": "2026-11-25", "dnc_rebuilt_calls": 3}
    (tmp_path / S.STATE_FILENAME).write_text(json.dumps(raw), encoding="utf-8")
    st = S.load_state(tmp_path)
    S.save_state(st, tmp_path)                                              # 2 on disk -> 1 canonical: allowed
    assert json.loads((tmp_path / S.STATE_FILENAME).read_text())["dnc_prospect_ids"] == ["abc"]
    with pytest.raises(S.StateShrinkError):                                 # a real loss is still refused
        S.save_state(S.State(dnc_rebuilt_at="2026-11-25", dnc_rebuilt_calls=3), tmp_path)


def test_old_file_non_string_id_still_resets(tmp_path):
    (tmp_path / S.STATE_FILENAME).write_text(json.dumps({"version": 2, "dnc_prospect_ids": ["a", 5]}), encoding="utf-8")
    assert S.load_state(tmp_path).was_reset


def test_case_folding_is_casefold_not_lower():
    assert S._pid("STRASSE") == S._pid("straße") == "strasse"               # lower() would not merge these
    assert "straße".lower() != "STRASSE".lower()


def test_pid_key_edge_cases():
    assert S._pid(None) is None and S._pid("") is None and S._pid("  ") is None and S._pid(5) is None
    assert S._pid(" Ab ") == "ab"


def test_unicode_dnc_id_blocks_across_case_forms(config):
    st = rec([call("C1", pid="PROSPECT-STRASSE", disp="Do Not Call")], prospects=[], config=config)
    assert S.is_blocked(pros(nid="prospect-straße", phone=None, email=None), st)[0]


def test_dnc_id_case_survives_save_load(config, tmp_path):
    st = rec([call("C1", pid="FAKE-P1", disp="Do Not Call")], prospects=[], config=config)
    S.save_state(st, tmp_path)
    assert S.is_blocked(pros(nid="Fake-p1", phone=None, email=None), S.load_state(tmp_path))[0]


def test_is_blocked_none_and_blank_ids_unaffected(config):
    st = rec([call("C1", pid="FAKE-P1", disp="Do Not Call")], prospects=[], config=config)
    assert not S.is_blocked(pros(nid=None, phone=None, email=None), st)[0]
    assert not S.is_blocked(pros(nid="   ", phone=None, email=None), st)[0]


def test_sfc_internal_keys_unaffected(config):
    # accounts and sheets use Salesforce ids, not the prospect-id folding; 'sfc:' style keys pass through
    st = S.record_sheet(S.State(), RUN, ["sfc:ABC-1", "SFC:abc-1"])
    assert st.sheets[-1]["account_ids"] == ["sfc:ABC-1", "SFC:abc-1"]


# ---- public is_dnc_call
def test_is_dnc_call_public_wrapper(config):
    assert S.is_dnc_call(call(disp="Do Not Call"), config)
    assert S.is_dnc_call(call(disp="do-not-call."), config)
    assert S.is_dnc_call(call(disp="DO  NOT\u00a0CALL"), config)
    assert not S.is_dnc_call(call(disp="No Answer"), config)
    assert not S.is_dnc_call(call(disp=None), config)
    assert not S.is_dnc_call(call(disp=""), config)
    assert not S.is_dnc_call(call(disp="Do Not Call (DNC)"), config)


def test_is_dnc_call_ignores_disposition_id(config):
    c = replace(call(disp="Do Not Call"), disposition_id="anything")
    assert S.is_dnc_call(c, config)
    assert not S.is_dnc_call(replace(call(disp="Interested"), disposition_id="disp-do-not-call"), config)
