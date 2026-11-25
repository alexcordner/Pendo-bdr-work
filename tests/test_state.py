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
    assert S.state_to_dict(once) == S.state_to_dict(twice)
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
    assert S.do_not_call_prospects(calls, config) == {"FAKE-P1"}


def test_dnc_ignores_other_dispositions_and_none_ids(config):
    calls = [call("C1", pid=None, disp="Do Not Call"), call("C2", pid="FAKE-P2", disp="No Answer"),
             call("C3", pid="FAKE-P3", disp=None)]
    assert S.do_not_call_prospects(calls, config) == set()


def test_dnc_matches_by_name_not_id(config):
    c = replace(call("C1", disp="Do Not Call"), disposition_id="disp-something-else")
    assert S.do_not_call_prospects([c], config) == {"FAKE-P1"}


def test_dnc_phones_cover_unlinked_calls(config):
    c = call("C1", pid=None, disp="Do Not Call", to="+61400000555")
    assert S.do_not_call_phones([c], config) == {"+61400000555"}
    st = rec([c], prospects=[], config=config)
    assert "+61400000555" in st.dnc_phones               # saved even though the call was unmatched


def test_dnc_is_sticky_in_state(config):
    st = rec([call("C1", disp="Do Not Call")], config=config)
    st = rec([call("C2", disp="Interested", dur=99, when="2026-11-20T00:00:00Z")], state=st, config=config)
    assert "FAKE-P1" in st.dnc_prospect_ids


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
    assert "FAKE-P1" in st.dnc_prospect_ids


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
    orig = [pros(), pros(nid="FAKE-P2", acct="FAKE-ACC-2", phone="+61400000012")]
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
    assert S.was_in_last_sheet(st, "A") and not S.was_in_last_sheet(st, "C")
    assert st.sheets[-1]["account_ids"] == ["A", "B"]


def test_sheet_only_newest_last_sheet_counts():
    st = S.record_sheet(S.State(), date(2026, 11, 23), ["A"])
    st = S.record_sheet(st, date(2026, 11, 24), ["B"])
    assert S.was_in_last_sheet(st, "B") and not S.was_in_last_sheet(st, "A")


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
    assert S.was_in_last_sheet(st, "NEW") and not S.was_in_last_sheet(st, "OLD")


def test_was_in_last_sheet_empty_state_and_none():
    assert not S.was_in_last_sheet(S.State(), "A")
    assert not S.was_in_last_sheet(S.record_sheet(S.State(), date(2026, 11, 24), ["A"]), None)


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
    st = S.record_sent_emails(S.State(), [sent_email(when=None)], [pros()])
    recent, reason = S.emailed_recently(st, "pat.one@example.com", date(2030, 1, 1), 7)
    assert recent and "unknown" in reason


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
    assert S.state_to_dict(s1) == S.state_to_dict(s2)
    assert s2.report["record_sent_emails"]["duplicate"] == 1


def test_record_sent_emails_guards_missing_ids_and_unknown_recipient():
    bad = SentEmail(message_id="", thread_id=None, recipient="", recipient_key="", sent_at=None)
    st = S.record_sent_emails(S.State(), [bad, sent_email("M9", addr="stranger@example.com")], [pros()])
    assert st.report["record_sent_emails"]["skipped_no_key"] == 1
    assert "FAKE-ACC-1" not in st.accounts and "stranger@example.com" in st.emails


def test_scenario_emails(scenario):
    prospects, _, sent = scenario
    st = S.record_sent_emails(S.State(), sent, prospects)
    assert S.emailed_recently(st, "pat.one@example.com", date(2026, 11, 22), 7)[0]
    recent, reason = S.emailed_recently(st, "sam.two@example.com", date(2026, 12, 30), 7)
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
    assert S.state_to_dict(back) == S.state_to_dict(st)
    assert back.accounts["FAKE-ACC-2"].last_connect_at == st.accounts["FAKE-ACC-2"].last_connect_at
    assert back.dnc_prospect_ids == {"FAKE-P6"} and back.dnc_phones == {"+61400000016"}


def test_save_creates_missing_directory(tmp_path):
    S.save_state(S.State(), tmp_path / "nested" / "state")
    assert (tmp_path / "nested" / "state" / S.STATE_FILENAME).exists()


def test_file_is_valid_json_with_version(tmp_path):
    S.save_state(S.State(), tmp_path)
    assert json.loads((tmp_path / S.STATE_FILENAME).read_text())["version"] == S.SCHEMA_VERSION


def test_missing_file_gives_empty_state(tmp_path, caplog):
    st = S.load_state(tmp_path / "nope")
    assert S.state_to_dict(st) == S.state_to_dict(S.State())


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
    raw = {"version": 1, "accounts": {"GOOD": {"last_outcome": "connect"}, "BAD": "oops"},
           "prospects": {"P": 5}, "emails": {"k": "x"}, "sheets": [{"run_date": "nonsense"}, "x",
           {"run_date": "2026-11-24", "account_ids": ["A", 3]}]}
    (tmp_path / S.STATE_FILENAME).write_text(json.dumps(raw), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="src.state"):
        st = S.load_state(tmp_path)
    assert list(st.accounts) == ["GOOD"] and st.prospects == {} and st.emails == {}
    assert st.sheets == [{"run_date": "2026-11-24", "account_ids": ["A"]}]
    assert "skipping malformed" in caplog.text


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
    raw = {"version": 1, "accounts": {"A": {"last_call_at": "2026-11-10T01:00:00"}}}
    (tmp_path / S.STATE_FILENAME).write_text(json.dumps(raw), encoding="utf-8")
    assert S.load_state(tmp_path).accounts["A"].last_call_at == datetime(2026, 11, 10, 1, tzinfo=timezone.utc)
