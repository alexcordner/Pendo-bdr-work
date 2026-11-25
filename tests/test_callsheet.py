"""Tests for src/callsheet.py. Fully synthetic (tests/fixtures/callsheet_accounts.json), offline."""
from __future__ import annotations

import copy
import json
import random
import re
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src import callsheet as cs
from src import signals as sigmod
from src.models import Account, Prospect, RankedAccount, SheetEntry, Signal, SignalKind
from src.ranker import RankResult, account_key, rank_accounts
from src.state import State

FIX = Path(__file__).parent / "fixtures" / "callsheet_accounts.json"
UTC = timezone.utc
NOW = datetime(2026, 11, 25, 3, 0, tzinfo=UTC)          # Wed 14:00 AEDT
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE_RE = re.compile(r"\+\d{8,}|\b04\d{8}\b")
NID = "NK-RESOLVED-0001"
OWNER = "OWNER_ABC"
OPENER = {"opener": "Saw the new product lead. Who owns the app roadmap?", "fact_1": "Fact one (source: x)",
          "fact_2": "none verified", "gatekeeper": "Who looks after your app team?", "trigger": "Exec move"}


def prep(line, fields=None, *, nid=NID, owner_id=OWNER, state=None, **kw):
    """prepare_task with a resolved prospect copied from the sheet contact plus a Nooks id."""
    resolved = replace(line.entry.prospect, nooks_id=nid)
    return cs.prepare_task(line, resolved, state or State(), fields or OPENER, owner_id, **kw)


def sf(n): return f"001FAKE0000{n:04d}"


def load_rows(): return json.loads(FIX.read_text())


def world(rows=None):
    rows = rows or load_rows()
    accts, prospects, signals = [], [], {}
    for r in rows:
        n = r["n"]
        accts.append(Account(sf_account_id=sf(n), name=r["name"], website=f"www.fake{n}.example.com",
                             employees=r["emp"], billing_state=r["state"], billing_city=r["city"], timezone=r["tz"],
                             is_sdr_target=r["sdr"], flags=list(r["flags"]), zoominfo_id=f"FAKE-ZI-{n:04d}",
                             country="New Zealand" if "Auckland" in r["tz"] else "Australia"))
        prospects.append(Prospect(nooks_id=f"FAKE-NK-P-{n:04d}", sf_contact_id=f"003FAKE{n:04d}", sf_account_id=sf(n),
                                  nooks_account_id=None, name=f"{r['first']} Person{n}", title=r["title"],
                                  email=f"person{n}@fake{n}.example.com", phone_e164=f"+6140000{n:04d}"))
        if r["scoop"]:
            kind = SignalKind(r["scoop"]["kind"])
            signals[sf(n)] = [Signal(sf(n), kind, 0.8, f"zoominfo_scoop:FAKE-S-{n}",
                                     NOW - timedelta(days=r["scoop"]["days"]),
                                     f"{r['name']} has a new digital product lead.", zoominfo_id=f"FAKE-ZI-{n:04d}")]
    return accts, signals, prospects


@pytest.fixture
def parts(config):
    accts, signals, prospects = world()
    rr = rank_accounts(accts, signals, prospects, {}, set(), config, NOW)
    return accts, signals, prospects, rr


@pytest.fixture
def sheet(config, parts):
    accts, signals, prospects, rr = parts
    return cs.build_sheet(rr, accts, signals, prospects, config, NOW)


def line_for(sheet, name):
    return next(x for x in sheet.lines if x.account.name == name)


def all_text(sheet):
    out = [cs.render_sheet_text(sheet), cs.render_sheet_markdown(sheet), cs.sheet_to_json(sheet)]
    for ln in sheet.lines:
        out += [json.dumps(ln.packet.to_dict()), cs.render_packet_text(ln.packet), "\n".join(ln.why_chosen)]
    return "\n".join(out)


# ---------------------------------------------------------------- basic assembly
def test_world_ranks_all_callable(parts):
    assert len(parts[3].ranked) == 8


def test_one_line_per_ranked_account(sheet):
    assert len(sheet.lines) == 8
    assert {x.account.name for x in sheet.lines} == {r["name"] for r in load_rows()}


def test_lines_are_sheet_entries(sheet):
    assert all(isinstance(x.entry, SheetEntry) for x in sheet.lines)
    assert sheet.entries == [x.entry for x in sheet.lines]


def test_entry_opener_fields_start_empty(sheet):
    e = sheet.lines[0].entry
    assert (e.opener, e.fact_1, e.fact_2, e.gatekeeper, e.trigger) == ("",) * 5


def test_chosen_prospect_comes_from_rank_result(sheet):
    ln = line_for(sheet, "Alpha Digital Pty Ltd")
    assert ln.prospect.nooks_id == "FAKE-NK-P-0001"


def test_naive_now_rejected(config, parts):
    accts, signals, prospects, rr = parts
    with pytest.raises(ValueError):
        cs.build_sheet(rr, accts, signals, prospects, config, datetime(2026, 11, 25, 3, 0))


def test_negative_limit_rejected(config, parts):
    accts, signals, prospects, rr = parts
    with pytest.raises(ValueError):
        cs.build_sheet(rr, accts, signals, prospects, config, NOW, limit=-1)


def test_run_date_is_sydney_date(config, parts):
    accts, signals, prospects, rr = parts
    late = datetime(2026, 11, 25, 14, 0, tzinfo=UTC)          # 01:00 Thu AEDT
    s = cs.build_sheet(rr, accts, signals, prospects, config, late)
    assert s.run_date == date(2026, 11, 26)


def test_ranks_are_positions_by_score(sheet):
    ranks = sorted((x.rank, x.score) for x in sheet.lines)
    assert [r for r, _ in ranks] == list(range(1, 9))
    assert [s for _, s in ranks] == sorted((s for _, s in ranks), reverse=True)


# ---------------------------------------------------------------- ordering
def test_call_order_is_grouped_by_tz_window_and_sorted_in_group(sheet):
    seen = []
    for label, lines in sheet.groups:
        assert label not in seen
        seen.append(label)
        times = [x.local_call_time.astimezone(UTC) for x in lines]
        assert times == sorted(times)
        assert len({(x.account.timezone, x.ranked.window, x.local_call_time.date()) for x in lines}) == 1


def test_lines_are_flattened_groups(sheet):
    flat = [ln for _, lines in sheet.groups for ln in lines]
    assert flat == sheet.lines
    assert [x.call_no for x in sheet.lines] == list(range(1, len(sheet.lines) + 1))


def test_groups_ordered_by_earliest_instant(sheet):
    firsts = [lines[0].local_call_time.astimezone(UTC) for _, lines in sheet.groups]
    assert firsts == sorted(firsts)


def test_each_line_inside_its_window(sheet, config):
    for ln in sheet.lines:
        t = ln.local_call_time
        assert t.tzinfo is not None
        w = {w.name: w for w in config.call_windows}[ln.ranked.window]
        assert w.start <= t.timetz().replace(tzinfo=None) <= w.end


def test_group_label_names_tz_and_window(sheet):
    for label, lines in sheet.groups:
        assert lines[0].account.timezone in label and lines[0].ranked.window in label


def test_same_zone_group_time_order_matches(sheet):
    syd = [x for x in sheet.lines if x.account.timezone == "Australia/Sydney"]
    times = [x.local_call_time for x in syd]
    for label, lines in sheet.groups:
        t = [x.local_call_time for x in lines]
        assert t == sorted(t)
    assert len(syd) == 4


# ---------------------------------------------------------------- limit
def test_limit_takes_top_n_by_score(config, parts, sheet):
    accts, signals, prospects, rr = parts
    s = cs.build_sheet(rr, accts, signals, prospects, config, NOW, limit=3)
    assert len(s.lines) == 3
    top3 = {x.account.name for x in sorted(sheet.lines, key=lambda x: x.rank)[:3]}
    assert {x.account.name for x in s.lines} == top3


def test_limit_then_ordered_by_call_time_not_score(config, parts):
    accts, signals, prospects, rr = parts
    s = cs.build_sheet(rr, accts, signals, prospects, config, NOW, limit=5)
    assert [x.rank for x in s.lines] != sorted(x.rank for x in s.lines) or len(s.groups) == 1
    assert max(x.rank for x in s.lines) == 5


def test_limit_ranks_stay_global(config, parts):
    accts, signals, prospects, rr = parts
    s = cs.build_sheet(rr, accts, signals, prospects, config, NOW, limit=4)
    assert sorted(x.rank for x in s.lines) == [1, 2, 3, 4]


def test_limit_zero_gives_empty(config, parts):
    accts, signals, prospects, rr = parts
    s = cs.build_sheet(rr, accts, signals, prospects, config, NOW, limit=0)
    assert s.lines == [] and "no callable" in cs.render_sheet_text(s)


def test_limit_larger_than_available(config, parts):
    accts, signals, prospects, rr = parts
    assert len(cs.build_sheet(rr, accts, signals, prospects, config, NOW, limit=99).lines) == 8


def test_limit_recorded(config, parts):
    accts, signals, prospects, rr = parts
    s = cs.build_sheet(rr, accts, signals, prospects, config, NOW, limit=2)
    assert s.limit == 2 and s.ranked_total == 8 and "top 2" in cs.render_sheet_text(s)


# ---------------------------------------------------------------- why chosen / honesty
def test_sdr_target_line(sheet):
    assert "SDR target" in line_for(sheet, "Alpha Digital Pty Ltd").why_chosen
    assert "SDR target" not in line_for(sheet, "Bravo Software Pty Ltd").why_chosen


def test_trigger_line_has_kind_evidence_source_age(sheet):
    why = line_for(sheet, "Alpha Digital Pty Ltd").why_chosen
    t = next(w for w in why if w.startswith("Executive Move:"))
    assert "new digital product lead" in t and "ZoomInfo scoop" in t and "3 days ago" in t


def test_no_signal_says_so(sheet):
    ln = line_for(sheet, "Charlie Retail Ltd")
    assert "No trigger: ranked on fit only" in ln.why_chosen
    assert ln.packet.has_real_trigger is False and "no trigger" in ln.flags


def test_no_signal_packet_has_no_signal_or_trigger_fact(sheet):
    p = line_for(sheet, "Charlie Retail Ltd").packet
    assert p.signals == []
    assert all("scoop" not in f.source.lower() for f in p.available_facts)
    assert "no real trigger" in cs.render_packet_text(p)


def test_trigger_never_invented_for_every_untriggered_line(sheet):
    for ln in sheet.lines:
        has = bool(ln.ranked.signals)
        trig_lines = [w for w in ln.why_chosen if ":" in w and w.split(":")[0] in cs.KIND_LABELS.values()]
        assert bool(trig_lines) == has


def test_none_kind_signal_is_not_a_trigger(config, parts):
    accts, signals, prospects, rr = parts
    ra = copy.copy(next(r for r in rr.ranked if r.account.name == "Charlie Retail Ltd"))
    ra.signals = [Signal(None, SignalKind.NONE, 0.0, "none:x", None, "nothing")]
    r2 = RankResult(ranked=[ra], prospects=rr.prospects)
    s = cs.build_sheet(r2, accts, {}, prospects, config, NOW)
    assert "No trigger: ranked on fit only" in s.lines[0].why_chosen


def test_size_line_with_tier(sheet):
    assert "Size 400 (tier1)" in line_for(sheet, "Alpha Digital Pty Ltd").why_chosen
    assert "Size 75 (tier2)" in line_for(sheet, "Delta Health Pty Ltd").why_chosen


def test_size_unknown_line_and_flag(sheet):
    ln = line_for(sheet, "Echo Logistics Pty Ltd")
    assert "Size unknown (neutral fit)" in ln.why_chosen and "size unknown" in ln.flags


def test_flag_lines_from_account_flags(sheet):
    ln = line_for(sheet, "Golf Media Pty Ltd")
    assert "Flag: possible_duplicate" in ln.why_chosen and "possible_duplicate" in ln.flags


def test_outside_tier_size_line(sheet):
    assert "Size 30 (tier3)" in line_for(sheet, "Golf Media Pty Ltd").why_chosen


def test_prospect_line_uses_first_name_only(sheet):
    ln = line_for(sheet, "Alpha Digital Pty Ltd")
    assert ln.why_chosen[0] == "Prospect: Pat1, Head of Product"
    assert "Person1" not in all_text(sheet)


def test_extra_flags_non_anz_tz_guess_low_confidence(config, parts):
    accts, signals, prospects, rr = parts
    ra = copy.copy(rr.ranked[0])
    a = copy.copy(ra.account)
    a.non_anz_billing_anz_hq, a.timezone_guessed, a.confidence = True, True, "low"
    ra.account = a
    s = cs.build_sheet(RankResult(ranked=[ra], prospects=rr.prospects), accts, signals, prospects, config, NOW)
    for f in ("non-ANZ billing", "tz guessed", "low confidence"):
        assert f in s.lines[0].flags
    assert "Flag: non-ANZ billing, ANZ HQ" in s.lines[0].why_chosen


def test_ambiguous_signal_flagged(config, parts):
    accts, signals, prospects, rr = parts
    ra = copy.copy(next(r for r in rr.ranked if r.account.name == "Alpha Digital Pty Ltd"))
    sg = replace(ra.signals[0], evidence="New Head of Product" + sigmod.AMBIGUOUS_SUFFIX)
    ra.signals = [sg]
    s = cs.build_sheet(RankResult(ranked=[ra], prospects=rr.prospects), accts, signals, prospects, config, NOW)
    ln = s.lines[0]
    assert "role ambiguous" in ln.flags and ln.packet.ambiguity_flags == [sigmod.AMBIGUOUS_NOTE]
    assert ln.packet.signals[0].ambiguous and sigmod.AMBIGUOUS_SUFFIX not in ln.packet.signals[0].evidence
    assert sigmod.AMBIGUOUS_NOTE in ln.packet.signals[0].verdict_reason
    assert any(w == f"Flag: {sigmod.AMBIGUOUS_NOTE}" for w in ln.why_chosen)


def test_intent_signal_labelled_not_dated_event(config, parts):
    accts, signals, prospects, rr = parts
    ra = copy.copy(next(r for r in rr.ranked if r.account.name == "Charlie Retail Ltd"))
    ra.signals = [Signal(None, SignalKind.INTENT, 0.3, "zoominfo_intent:x", None, "Interest in digital adoption")]
    s = cs.build_sheet(RankResult(ranked=[ra], prospects=rr.prospects), accts, {}, prospects, config, NOW)
    ln = s.lines[0]
    assert "not a dated event" in ln.why_chosen[1] or "not a dated event" in ln.why_chosen[0]
    assert "not a dated event" in ln.packet.available_facts[0].text
    assert "date unknown" in ln.packet.available_facts[0].source


def test_signals_fallback_to_mapping_when_ranked_has_none(config, parts):
    accts, signals, prospects, rr = parts
    ra = copy.copy(next(r for r in rr.ranked if r.account.name == "Alpha Digital Pty Ltd"))
    ra.signals = []
    s = cs.build_sheet(RankResult(ranked=[ra], prospects=rr.prospects), accts, signals, prospects, config, NOW)
    assert s.lines[0].packet.has_real_trigger


def test_multiple_signals_count_line(config, parts):
    accts, signals, prospects, rr = parts
    ra = copy.copy(next(r for r in rr.ranked if r.account.name == "Alpha Digital Pty Ltd"))
    extra = Signal(sf(1), SignalKind.FUNDING, 0.5, "zoominfo_scoop:FAKE-S-X", NOW - timedelta(days=9), "Raised money")
    ra.signals = [ra.signals[0], extra]
    s = cs.build_sheet(RankResult(ranked=[ra], prospects=rr.prospects), accts, signals, prospects, config, NOW)
    assert "1 more trigger" in s.lines[0].why_chosen
    assert len(s.lines[0].packet.signals) == 2


# ---------------------------------------------------------------- research packet
def test_packet_core_fields(sheet):
    p = line_for(sheet, "Alpha Digital Pty Ltd").packet
    assert (p.account_name, p.website, p.employees, p.tier) == ("Alpha Digital Pty Ltd", "www.fake1.example.com", 400, "tier1")
    assert (p.prospect_first_name, p.prospect_title, p.sdr_target) == ("Pat1", "Head of Product", True)
    assert p.city == "Sydney" and p.state == "New South Wales" and p.timezone == "Australia/Sydney"
    assert p.window in ("morning", "afternoon") and p.local_time.endswith("+11:00")


def test_packet_signal_fields(sheet):
    s = line_for(sheet, "Alpha Digital Pty Ltd").packet.signals[0]
    assert s.kind == "exec_move" and s.label == "Executive Move" and s.age_days == 3
    assert s.source == "zoominfo_scoop:FAKE-S-1" and s.source_label == "ZoomInfo scoop 2026-11-22"
    assert s.date == "2026-11-22" and s.verdict_reason.startswith("kept: Executive Move within")


def test_every_fact_has_source(sheet):
    for ln in sheet.lines:
        for f in ln.packet.available_facts:
            assert f.source.strip() and f.text.strip()


def test_facts_signals_first_then_profile(sheet):
    f = line_for(sheet, "Alpha Digital Pty Ltd").packet.available_facts
    assert f[0].source == "ZoomInfo scoop 2026-11-22"
    assert f[1].text == "About 400 employees" and f[1].source.startswith("Salesforce account record")


def test_two_facts_no_note(sheet):
    p = line_for(sheet, "Alpha Digital Pty Ltd").packet
    assert p.fact_count >= 2 and p.facts_note == ""


def test_fewer_than_two_facts_says_so(config, parts):
    accts, signals, prospects, rr = parts
    a = Account(sf_account_id=sf(50), name="Bare Co", employees=None, timezone="Australia/Sydney")
    pr = Prospect("FAKE-NK-P-50", None, sf(50), None, "Sam Bare", "Head of Product", phone_e164="+61400000050")
    ra = RankedAccount(a, 20.0, {}, "morning", datetime(2026, 11, 26, 9, 0, tzinfo=ZoneInfo("Australia/Sydney")))
    s = cs.build_sheet(RankResult(ranked=[ra], prospects={account_key(a): [pr]}), [a], {}, [pr], config, NOW)
    p = s.lines[0].packet
    assert p.fact_count == 0 and "Only 0 verified fact(s)" in p.facts_note and "Do not pad" in p.facts_note
    assert "NOTE: Only 0" in cs.render_packet_text(p)


def test_exactly_one_fact_says_so(config):
    a = Account(sf_account_id=sf(51), name="One Co", employees=300, timezone="Australia/Sydney")
    pr = Prospect("FAKE-NK-P-51", None, sf(51), None, "Sam One", "Head of Product", phone_e164="+61400000051")
    ra = RankedAccount(a, 20.0, {}, "morning", datetime(2026, 11, 26, 9, 0, tzinfo=ZoneInfo("Australia/Sydney")))
    p = cs.build_sheet(RankResult(ranked=[ra], prospects={account_key(a): [pr]}), [a], {}, [pr], config, NOW).lines[0].packet
    assert p.fact_count == 1 and "Only 1 verified" in p.facts_note


def test_packet_not_padded(sheet):
    p = line_for(sheet, "Echo Logistics Pty Ltd").packet     # unknown size, no scoop, no city
    assert p.fact_count == 0 or all("employees" not in f.text for f in p.available_facts)


def test_must_not_claim_list_present(sheet):
    for ln in sheet.lines:
        assert ln.packet.must_not_claim and any("invent" in m for m in ln.packet.must_not_claim)
        assert "MUST NOT INVENT" in cs.render_packet_text(ln.packet)


def test_contact_history_in_packet(config, parts):
    accts, signals, prospects, rr = parts
    key = account_key(next(a for a in accts if a.name == "Delta Health Pty Ltd"))
    h = {key: cs.ContactHistory(date(2026, 11, 20), "Left Voicemail", 2)}
    s = cs.build_sheet(rr, accts, signals, prospects, config, NOW, history=h)
    p = line_for(s, "Delta Health Pty Ltd").packet
    assert p.contact_history == "last call 2026-11-20, outcome Left Voicemail, 2 call(s) logged"
    assert p.contact_history_source == "Nooks call history"
    assert any(f.source == "Nooks call history" for f in p.available_facts)
    assert "HISTORY:" in cs.render_packet_text(p)


def test_empty_contact_history_adds_nothing():
    assert cs.ContactHistory().summary() is None


def test_no_history_means_none(sheet):
    assert line_for(sheet, "Alpha Digital Pty Ltd").packet.contact_history is None


def test_packet_to_dict_is_json_safe(sheet):
    for ln in sheet.lines:
        d = ln.packet.to_dict()
        assert json.loads(json.dumps(d)) == d


def test_packet_text_compact_and_labelled(sheet):
    t = cs.render_packet_text(line_for(sheet, "Alpha Digital Pty Ltd").packet)
    assert t.startswith("ACCOUNT: Alpha Digital Pty Ltd") and "CALLING: Pat1, Head of Product" in t
    assert "SDR TARGET" in t and "AVAILABLE FACTS" in t and len(t) < 2500


def test_packet_has_no_email_or_phone_keys(sheet):
    for ln in sheet.lines:
        assert not {"email", "phone", "phone_e164"} & set(ln.packet.to_dict())


def test_no_email_or_phone_anywhere(sheet):
    t = all_text(sheet)
    assert not EMAIL_RE.findall(t)
    assert "person1@" not in t and "@fake" not in t
    assert not PHONE_RE.search(t)
    assert "+6140000" not in t


def test_no_email_even_if_evidence_is_clean(config, parts):
    accts, signals, prospects, rr = parts
    assert "FAKE-NK-P-0001" not in cs.render_sheet_text(cs.build_sheet(rr, accts, signals, prospects, config, NOW))


def test_name_without_surname_leaks(sheet):
    t = all_text(sheet)
    assert all(f"Person{n}" not in t for n in range(1, 9))


# ---------------------------------------------------------------- buckets
@pytest.fixture
def bucket_world(config):
    rows = load_rows()
    accts, signals, prospects = world(rows)
    # needs_prospect: account 9 with no prospect; skipped: account 10 without timezone
    accts.append(Account(sf_account_id=sf(9), name="Needy Pty Ltd", employees=300, timezone="Australia/Sydney"))
    accts.append(Account(sf_account_id=sf(10), name="Nozone Pty Ltd", employees=300, timezone=None))
    prospects.append(Prospect("FAKE-NK-P-9", "c9", sf(9), None, "Nobody Phone", "Chef", phone_e164="+61400000009"))
    rr = rank_accounts(accts, signals, prospects, {}, set(), config, NOW)
    return accts, signals, prospects, rr


def test_needs_prospect_listed_not_in_lines(config, bucket_world):
    accts, signals, prospects, rr = bucket_world
    s = cs.build_sheet(rr, accts, signals, prospects, config, NOW)
    assert [b.name for b in s.needs_prospect] == ["Needy Pty Ltd"]
    assert "Needy Pty Ltd" not in {x.account.name for x in s.lines}
    assert "no_persona_match" in s.needs_prospect[0].reason


def test_skipped_listed_with_reason_and_counts(config, bucket_world):
    accts, signals, prospects, rr = bucket_world
    s = cs.build_sheet(rr, accts, signals, prospects, config, NOW)
    assert ("Nozone Pty Ltd", "no timezone") in [(b.name, b.reason) for b in s.skipped]
    assert s.skipped_reason_counts == {"no timezone": 1}


def test_bucket_section_rendered_with_names(config, bucket_world):
    accts, signals, prospects, rr = bucket_world
    s = cs.build_sheet(rr, accts, signals, prospects, config, NOW)
    t = cs.render_sheet_text(s)
    assert "NEEDS PROSPECT" in t and "Needy Pty Ltd" in t and "Nozone Pty Ltd: no timezone" in t
    assert "Top skip reasons: no timezone (1)" in t
    m = cs.render_sheet_markdown(s)
    assert "Needy Pty Ltd" in m and "Not on the sheet" in m


def test_spilled_never_in_sheet_or_payload(config, parts):
    accts, signals, prospects, rr = parts
    spilled = copy.copy(rr.ranked[0])
    spilled.window, spilled.local_call_time = "morning", datetime(2026, 11, 30, 9, 0, tzinfo=ZoneInfo("Australia/Sydney"))
    rr2 = RankResult(ranked=rr.ranked[1:], spilled=[spilled], prospects=rr.prospects)
    s = cs.build_sheet(rr2, accts, signals, prospects, config, NOW)
    assert spilled.account.name not in {x.account.name for x in s.lines}
    assert [b.name for b in s.spilled] == [spilled.account.name] and "held to Mon 30 Nov 09:00" in s.spilled[0].reason
    assert "SPILLED" in cs.render_sheet_text(s)


def test_bucket_list_capped_at_25_with_total(config, parts):
    accts, signals, prospects, rr = parts
    many = [RankedAccount(Account(sf_account_id=sf(100 + i), name=f"Skip {i:02d}"), skipped_reason="no timezone")
            for i in range(40)]
    s = cs.build_sheet(RankResult(skipped=many), accts, signals, prospects, config, NOW)
    t = cs.render_sheet_text(s)
    assert "SKIPPED (40)" in t and "Skip 24" in t and "Skip 25" not in t and "and 15 more" in t


def test_rank_skip_reason_variants_grouped(config, parts):
    accts, signals, prospects, rr = parts
    sk = [RankedAccount(Account(sf_account_id=sf(200), name="A"), skipped_reason="below daily cap (rank 41, cap 40)"),
          RankedAccount(Account(sf_account_id=sf(201), name="B"), skipped_reason="below daily cap (rank 42, cap 40)"),
          RankedAccount(Account(sf_account_id=sf(202), name="C"), skipped_reason="In cooldown until 2026-12-01 (account-local)")]
    s = cs.build_sheet(RankResult(skipped=sk), accts, signals, prospects, config, NOW)
    assert s.skipped_reason_counts == {"below daily cap": 2, "In cooldown": 1}


def test_ranked_with_skipped_reason_is_dropped_honestly(config, parts):
    accts, signals, prospects, rr = parts
    bad = copy.copy(rr.ranked[0])
    bad.skipped_reason = "on last sheet"
    s = cs.build_sheet(RankResult(ranked=[bad], prospects=rr.prospects), accts, signals, prospects, config, NOW)
    assert s.lines == [] and "on last sheet" in s.dropped[0].reason


def test_ranked_without_time_or_prospect_is_dropped(config, parts):
    accts, signals, prospects, rr = parts
    a = copy.copy(rr.ranked[0]); a.local_call_time = None
    b = copy.copy(rr.ranked[1])
    s = cs.build_sheet(RankResult(ranked=[a, b], prospects={}), accts, signals, [], config, NOW)
    assert s.lines == [] and len(s.dropped) == 2


def test_prospect_fallback_from_prospects_argument(config, parts):
    accts, signals, prospects, rr = parts
    s = cs.build_sheet(RankResult(ranked=rr.ranked, prospects={}), accts, signals, prospects, config, NOW)
    assert len(s.lines) == 8


# ---------------------------------------------------------------- rendering
def test_text_columns_present(sheet):
    t = cs.render_sheet_text(sheet)
    first = t.splitlines()[0]
    assert first.startswith("CALL SHEET 2026-11-25: 8 of 8")
    row = next(x for x in t.splitlines() if "Alpha Digital" in x)
    assert "Pat1, Head of Product" in row and "score" in row and "Executive Move" in row and "Thu" in row or "Wed" in row


def test_text_one_line_per_entry(sheet):
    rows = [x for x in cs.render_sheet_text(sheet).splitlines() if re.match(r"\s*\d+\. ", x)]
    assert len(rows) == 8


def test_text_flags_column(sheet):
    row = next(x for x in cs.render_sheet_text(sheet).splitlines() if "Echo Logistics" in x)
    assert "flags:" in row and "size unknown" in row


def test_markdown_explains_each_account(sheet):
    m = cs.render_sheet_markdown(sheet)
    assert m.startswith("# Call sheet 2026-11-25")
    for ln in sheet.lines:
        assert f"### {ln.call_no}. {ln.account.name}" in m
    assert "Why chosen:" in m and "Nothing was sent" in m


def test_json_roundtrip(sheet):
    d = json.loads(cs.sheet_to_json(sheet))
    assert len(d["lines"]) == 8 and d["run_date"] == "2026-11-25"
    assert d["lines"][0]["nooks_prospect_id"].startswith("FAKE-NK-P") and d["lines"][0]["sf_contact_id"].startswith("003FAKE")
    assert "email" not in cs.sheet_to_json(sheet).lower().replace("email address", "")


@pytest.fixture
def big(config):
    rows = []
    for n in range(1, 41):
        rows.append(dict(n=n, name=f"Company {n:02d} Pty Ltd", tz=["Australia/Sydney", "Australia/Perth", "Pacific/Auckland", "Australia/Brisbane"][n % 4],
                         state=None, city="X", emp=60 + n * 20, title="Head of Product", first=f"Pat{n}", sdr=(n % 7 == 0),
                         scoop={"days": n % 30, "kind": "funding"} if n % 3 else None, flags=[]))
    accts, signals, prospects = world(rows)
    rr = rank_accounts(accts, signals, prospects, {}, set(), config, NOW)
    return cs.build_sheet(rr, accts, signals, prospects, config, NOW), rr


def test_render_40_entries_readable(big):
    sheet, rr = big
    assert len(sheet.lines) >= 30
    t = cs.render_sheet_text(sheet)
    rows = [x for x in t.splitlines() if re.match(r"\s*\d+\. ", x)]
    assert len(rows) == len(sheet.lines)
    assert max(len(x) for x in rows) < 260
    assert t.count("\n== ") == len(sheet.groups)
    assert len(cs.render_sheet_markdown(sheet).splitlines()) > 100
    json.loads(cs.sheet_to_json(sheet))


def test_40_entries_no_contact_data(big):
    assert not EMAIL_RE.search(all_text(big[0])) and not PHONE_RE.search(all_text(big[0]))


def test_40_entries_unique_call_numbers_and_groups(big):
    sheet, _ = big
    assert [x.call_no for x in sheet.lines] == list(range(1, len(sheet.lines) + 1))
    assert len({g for g, _ in sheet.groups}) == len(sheet.groups)


# ---------------------------------------------------------------- determinism
def test_determinism_with_shuffled_inputs(config):
    base = None
    for seed in range(6):
        rows = load_rows()
        random.Random(seed).shuffle(rows)
        accts, signals, prospects = world(rows)
        random.Random(seed).shuffle(prospects)
        rr = rank_accounts(accts, signals, prospects, {}, set(), config, NOW)
        random.Random(seed + 1).shuffle(rr.ranked)
        out = cs.sheet_to_json(cs.build_sheet(rr, accts, signals, prospects, config, NOW, limit=6))
        base = base or out
        assert out == base


def test_repeat_calls_identical(config, parts):
    accts, signals, prospects, rr = parts
    a = cs.sheet_to_json(cs.build_sheet(rr, accts, signals, prospects, config, NOW))
    assert a == cs.sheet_to_json(cs.build_sheet(rr, accts, signals, prospects, config, NOW))


def test_build_does_not_mutate_inputs(config, parts):
    accts, signals, prospects, rr = parts
    before = [(r.account.name, r.score, list(r.reasons), r.local_call_time) for r in rr.ranked]
    cs.build_sheet(rr, accts, signals, prospects, config, NOW)
    assert before == [(r.account.name, r.score, list(r.reasons), r.local_call_time) for r in rr.ranked]


# ---------------------------------------------------------------- nooks payload
def test_payload_exact_shape(sheet):
    p = prep(sheet.lines[0], OPENER)
    assert set(p) == {"action", "dueAt", "ownerId", "prospectId", "priority", "note"}
    assert p["action"] == "call" and p["ownerId"] == OWNER
    assert p["prospectId"] == NID


def test_owner_placeholder_matches_nooks_pattern():
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,128}", cs.OWNER_ID_PLACEHOLDER)


def test_owner_id_override(sheet):
    assert prep(sheet.lines[0], OPENER, owner_id="abc123")["ownerId"] == "abc123"


def test_payload_note_layout(sheet):
    ln = sheet.lines[0]
    lines = prep(ln, OPENER)["note"].split("\n")
    assert lines[0] == f"[apac-callsheet 2026-11-25 #{ln.rank}]"
    assert [x.split(":")[0] for x in lines[1:]] == ["OPENER", "FACT 1", "FACT 2", "GATEKEEPER", "TRIGGER"]
    assert lines[1] == "OPENER: " + OPENER["opener"]


def test_tag_line_format_and_parsable():
    tag = cs.task_tag(date(2026, 12, 3), 7)
    assert tag == "[apac-callsheet 2026-12-03 #7]"
    assert re.fullmatch(r"\[apac-callsheet (\d{4}-\d{2}-\d{2}) #(\d+)\]", tag)


@pytest.mark.parametrize("missing", list(cs.OPENER_FIELDS))
def test_refuses_empty_opener_field(sheet, missing):
    bad = dict(OPENER); bad[missing] = "  "
    with pytest.raises(cs.SheetError, match=missing):
        prep(sheet.lines[0], bad)


def test_refuses_absent_opener_field(sheet):
    bad = dict(OPENER); del bad["fact_2"]
    with pytest.raises(cs.SheetError):
        prep(sheet.lines[0], bad)


def test_refuses_needs_prospect_entry(config, bucket_world):
    accts, signals, prospects, rr = bucket_world
    ra = rr.needs_prospect[0]
    with pytest.raises(cs.SheetError):
        cs.prepare_task(SheetEntry(ra), None, State(), OPENER, OWNER)           # type: ignore[arg-type]
    with pytest.raises(cs.SheetError):
        cs.prepare_task(ra, None, State(), OPENER, OWNER)                       # type: ignore[arg-type]


def test_refuses_spilled_and_skipped_ranked_accounts(config, parts):
    accts, signals, prospects, rr = parts
    ln = cs.build_sheet(rr, accts, signals, prospects, config, NOW).lines[0]
    ln.ranked.skipped_reason = "below daily cap (rank 41, cap 40)"
    with pytest.raises(cs.SheetError):
        prep(ln, OPENER)
    ln.ranked.skipped_reason = None
    ln.ranked.local_call_time = None
    with pytest.raises(cs.SheetError):
        prep(ln, OPENER)


def test_due_time_in_account_zone_sydney_dst(sheet):
    ln = line_for(sheet, "Alpha Digital Pty Ltd")
    due = prep(ln, OPENER)["dueAt"]
    assert due.endswith("+11:00")
    assert datetime.fromisoformat(due) == ln.local_call_time


def test_due_time_offsets_per_zone(sheet):
    expect = {"Bravo Software Pty Ltd": "+08:00", "Charlie Retail Ltd": "+13:00", "Delta Health Pty Ltd": "+10:00",
              "Golf Media Pty Ltd": "+11:00"}
    for name, off in expect.items():
        assert prep(line_for(sheet, name), OPENER)["dueAt"].endswith(off), name


def test_due_time_non_dst_period(config):
    june = datetime(2026, 6, 24, 3, 0, tzinfo=UTC)
    accts, signals, prospects = world()
    rr = rank_accounts(accts, signals, prospects, {}, set(), config, june)
    s = cs.build_sheet(rr, accts, signals, prospects, config, june)
    due = prep(line_for(s, "Alpha Digital Pty Ltd"), OPENER)["dueAt"]
    assert due.endswith("+10:00")
    assert prep(line_for(s, "Charlie Retail Ltd"), OPENER)["dueAt"].endswith("+12:00")


def test_due_time_across_dst_change(config):
    # Fri 2027-04-02 evening AEDT: first usable day is Monday 2027-04-05, after AEDT ends on 2027-04-04.
    now = datetime(2027, 4, 2, 9, 0, tzinfo=UTC)
    accts, signals, prospects = world()
    rr = rank_accounts(accts, signals, prospects, {}, set(), config, now)
    s = cs.build_sheet(rr, accts, signals, prospects, config, now)
    due = prep(line_for(s, "Alpha Digital Pty Ltd"), OPENER)["dueAt"]
    assert datetime.fromisoformat(due).date() == date(2027, 4, 5) and due.endswith("+10:00")


def test_due_is_aware_and_parses(sheet):
    for ln in sheet.lines:
        d = datetime.fromisoformat(prep(ln, OPENER)["dueAt"])
        assert d.utcoffset() is not None


@pytest.mark.parametrize("score,prio", [(100, "urgent"), (75, "urgent"), (74.9, "high"), (55, "high"),
                                        (54.9, "normal"), (35, "normal"), (34.9, "low"), (0, "low")])
def test_priority_bands(score, prio):
    assert cs.priority_for(score) == prio


def test_priority_values_are_nooks_enum(sheet):
    for ln in sheet.lines:
        assert prep(ln, OPENER)["priority"] in {"low", "normal", "high", "urgent"}


def test_payload_note_collapses_newlines_in_fields(sheet):
    bad = dict(OPENER); bad["opener"] = "line one\nTRIGGER: fake"
    note = prep(sheet.lines[0], bad)["note"]
    assert note.count("\n") == 5 and "OPENER: line one TRIGGER: fake" in note


def test_payload_has_no_contact_data(sheet):
    for ln in sheet.lines:
        p = json.dumps(prep(ln, OPENER))
        assert "@" not in p and not PHONE_RE.search(p)


def test_payload_builder_is_pure(sheet):
    ln = sheet.lines[0]
    assert prep(ln, OPENER) == prep(ln, OPENER)
    assert ln.entry.opener == ""                       # the entry is not filled in by the builder


def test_tag_rank_matches_line_rank(sheet):
    for ln in sheet.lines:
        assert f"#{ln.rank}]" in prep(ln, OPENER)["note"].split("\n")[0]


# ---------------------------------------------------------------- Phase 3 lazy resolution (nooks_id None in dry run)


def dry_world(config):
    """Dry-run world: prospects are SF contacts, nooks_id None."""
    accts, signals, prospects = world()
    prospects = [replace(p, nooks_id=None) for p in prospects]
    rr = rank_accounts(accts, signals, prospects, {}, set(), config, NOW)
    return accts, signals, prospects, rr, cs.build_sheet(rr, accts, signals, prospects, config, NOW)


def test_sheet_works_with_nooks_id_none(config):
    _, _, _, rr, s = dry_world(config)
    assert len(rr.ranked) == 8 and len(s.lines) == 8
    assert all(ln.prospect.nooks_id is None for ln in s.lines)


def test_packet_identifies_contact_by_sf_id(config):
    *_, s = dry_world(config)
    p = line_for(s, "Alpha Digital Pty Ltd").packet
    assert p.prospect_sf_contact_id == "003FAKE0001" and p.to_dict()["prospect_sf_contact_id"] == "003FAKE0001"
    assert "SF contact 003FAKE0001" in cs.render_packet_text(p)


def test_dry_run_renderings_have_no_contact_data_and_no_none_id(config):
    *_, s = dry_world(config)
    t = all_text(s)
    assert not EMAIL_RE.findall(t) and not PHONE_RE.search(t)
    d = json.loads(cs.sheet_to_json(s))
    assert d["lines"][0]["nooks_prospect_id"] is None and d["lines"][0]["sf_contact_id"]
    assert "Pat1, Head of Product" in cs.render_sheet_text(s)


def test_resolution_list_in_call_order_deduped(config):
    *_, s = dry_world(config)
    ids = cs.sheet_needs_prospect_resolution(s)
    assert ids == [ln.prospect.sf_contact_id for ln in s.lines] and len(set(ids)) == 8


def test_resolution_list_empty_when_ids_present(sheet):
    assert cs.sheet_needs_prospect_resolution(sheet) == []


def test_resolution_list_dedupes_shared_contact(config):
    *_, s = dry_world(config)
    s.lines[1].entry.prospect = s.lines[0].entry.prospect
    assert len(cs.sheet_needs_prospect_resolution(s)) == 7


def test_line_with_no_ids_flagged_and_not_listed(config):
    *_, s = dry_world(config)
    s.lines[0].entry.prospect = replace(s.lines[0].entry.prospect, sf_contact_id=None)
    assert s.lines[0].entry.prospect.sf_contact_id not in cs.sheet_needs_prospect_resolution(s)
    assert len(cs.sheet_needs_prospect_resolution(s)) == 7


def test_no_contact_id_flag_on_build(config):
    accts, signals, prospects = world()
    prospects = [replace(p, nooks_id=None, sf_contact_id=None) for p in prospects]
    rr = rank_accounts(accts, signals, prospects, {}, set(), config, NOW)
    s = cs.build_sheet(rr, accts, signals, prospects, config, NOW)
    assert all("no contact id" in ln.flags for ln in s.lines)


# ---------------------------------------------------------------- ContactHistory sources
def test_history_matched_by_phone_label(config, parts):
    accts, signals, prospects, rr = parts
    key = account_key(next(a for a in accts if a.name == "Delta Health Pty Ltd"))
    h = {key: cs.ContactHistory(date(2026, 11, 20), "No Answer", 1, matched_by="phone")}
    p = line_for(cs.build_sheet(rr, accts, signals, prospects, config, NOW, history=h), "Delta Health Pty Ltd").packet
    assert p.contact_history_source == "Nooks call history (matched by phone)"
    assert any(f.source == "Nooks call history (matched by phone)" for f in p.available_facts)


def test_history_matched_by_prospect_id_label():
    assert cs.ContactHistory(matched_by="nooks_prospect_id").source_label == "Nooks call history (matched by nooks_prospect_id)"
    assert cs.ContactHistory().source_label == "Nooks call history"


# ================================================================ review fixes (2c review)
from src.models import Prospect as _P  # noqa: E402,F401


# ---- H1: owner id
@pytest.mark.parametrize("bad", ["", "   ", None, 5, ["x"], "has space", "bad!id", "x" * 129])
def test_h1_owner_id_refused(sheet, bad):
    with pytest.raises(cs.SheetError):
        prep(sheet.lines[0], owner_id=bad)


def test_h1_placeholder_refused_live(sheet):
    with pytest.raises(cs.SheetError, match="placeholder"):
        prep(sheet.lines[0], owner_id=cs.OWNER_ID_PLACEHOLDER)


def test_h1_placeholder_allowed_only_in_dry_run(sheet):
    assert prep(sheet.lines[0], owner_id=cs.OWNER_ID_PLACEHOLDER, dry_run=True)["ownerId"] == cs.OWNER_ID_PLACEHOLDER


def test_h1_blank_owner_refused_even_in_dry_run(sheet):
    with pytest.raises(cs.SheetError):
        prep(sheet.lines[0], owner_id="", dry_run=True)


def test_h1_owner_is_required_positional(sheet):
    ln = sheet.lines[0]
    with pytest.raises(TypeError):
        cs.prepare_task(ln, replace(ln.prospect, nooks_id=NID), State(), OPENER)       # type: ignore[call-arg]


def test_h2_no_public_unchecked_payload_builder():
    assert not hasattr(cs, "nooks_task_payload") and not hasattr(cs, "recheck_blocked")


# ---- H2: prepare_task is the only path
def test_h2_padded_id_checked_as_stripped(sheet):
    st = State(); st.dnc_prospect_ids.add("NK-RES")
    with pytest.raises(cs.BlockedError, match="Do Not Call"):
        prep(sheet.lines[0], nid=" NK-RES ", state=st)


def test_h2_padded_id_used_stripped(sheet):
    assert prep(sheet.lines[0], nid="\tNK-OK \n")["prospectId"] == "NK-OK"


def test_h2_payload_uses_exactly_the_checked_id(sheet):
    assert prep(sheet.lines[0], nid="NK-77")["prospectId"] == "NK-77"


@pytest.mark.parametrize("bad", [None, "", "  ", "NK 1", "NK​1", "​", "x" * 129])
def test_h2_bad_nooks_id(sheet, bad):
    with pytest.raises(cs.SheetError):
        prep(sheet.lines[0], nid=bad)


def test_h2_resolved_sf_id_none_fails_closed(sheet):
    ln = sheet.lines[0]
    with pytest.raises(cs.SheetError, match="cannot verify"):
        cs.prepare_task(ln, replace(ln.prospect, nooks_id=NID, sf_contact_id=None), State(), OPENER, OWNER)


def test_h2_resolved_sf_id_blank_fails_closed(sheet):
    ln = sheet.lines[0]
    with pytest.raises(cs.SheetError):
        cs.prepare_task(ln, replace(ln.prospect, nooks_id=NID, sf_contact_id="  "), State(), OPENER, OWNER)


def test_h2_sheet_contact_without_sf_id_fails_closed(sheet):
    ln = sheet.lines[0]
    resolved = replace(ln.prospect, nooks_id=NID)
    ln.entry.prospect = replace(ln.entry.prospect, sf_contact_id=None)
    with pytest.raises(cs.SheetError, match="sheet contact has no SF"):
        cs.prepare_task(ln, resolved, State(), OPENER, OWNER)


def test_h2_mismatched_ids(sheet):
    ln = sheet.lines[0]
    with pytest.raises(cs.SheetError, match="different contact"):
        cs.prepare_task(ln, replace(ln.prospect, nooks_id=NID, sf_contact_id="003OTHER"), State(), OPENER, OWNER)


def test_h2_15_and_18_char_sf_ids_match(config):
    a = Account(sf_account_id=sf(60), name="Idco", employees=300, timezone="Australia/Sydney")
    pr = Prospect(None, "003AB0000012345", sf(60), None, "Sam Idco", "Head of Product", phone_e164="+61400000060")
    ra = RankedAccount(a, 50.0, {}, "morning", datetime(2026, 11, 26, 9, 0, tzinfo=ZoneInfo("Australia/Sydney")))
    s = cs.build_sheet(RankResult(ranked=[ra], prospects={account_key(a): [pr]}), [a], {}, [pr], config, NOW)
    r18 = replace(pr, nooks_id="NK-18", sf_contact_id="003AB0000012345AAA")
    assert cs.prepare_task(s.lines[0], r18, State(), OPENER, OWNER)["prospectId"] == "NK-18"


def test_h2_blocked_by_nooks_id(sheet):
    st = State(); st.dnc_prospect_ids.add(NID)
    with pytest.raises(cs.BlockedError):
        prep(sheet.lines[0], state=st)


def test_h2_blocked_sheet_contact_phone_when_resolved_has_none(sheet):
    from src.state import _phone_key
    ln = sheet.lines[0]
    st = State(); st.dnc_phones.add(_phone_key(ln.prospect.phone_e164))
    resolved = replace(ln.prospect, nooks_id=NID, phone_e164=None, email=None)
    with pytest.raises(cs.BlockedError, match="Do Not Call"):
        cs.prepare_task(ln, resolved, st, OPENER, OWNER)


def test_h2_blocked_sheet_contact_email(sheet):
    from src.state import _hash_email
    ln = sheet.lines[0]
    st = State(); st.dnc_emails.add(_hash_email(ln.prospect.email))
    resolved = replace(ln.prospect, nooks_id=NID, phone_e164=None, email=None)
    with pytest.raises(cs.BlockedError):
        cs.prepare_task(ln, resolved, st, OPENER, OWNER)


def test_h2_flags_and_opt_out_block(sheet):
    ln = sheet.lines[0]
    for kw in ({"do_not_call": True}, {"opted_out_email": True}):
        with pytest.raises(cs.BlockedError):
            cs.prepare_task(ln, replace(ln.prospect, nooks_id=NID, **kw), State(), OPENER, OWNER)


def test_h2_sheet_contact_flag_blocks(sheet):
    ln = sheet.lines[0]
    ln.entry.prospect = replace(ln.entry.prospect, do_not_call=True)
    with pytest.raises(cs.BlockedError, match="sheet contact"):
        cs.prepare_task(ln, replace(ln.prospect, nooks_id=NID, do_not_call=False), State(), OPENER, OWNER)


def test_h2_resolved_not_a_prospect(sheet):
    with pytest.raises(cs.SheetError):
        cs.prepare_task(sheet.lines[0], None, State(), OPENER, OWNER)      # type: ignore[arg-type]


def test_h2_due_time_passed_refused_when_now_given(sheet):
    ln = sheet.lines[0]
    with pytest.raises(cs.SheetError, match="already passed"):
        prep(ln, now=ln.local_call_time + timedelta(minutes=1))
    assert prep(ln, now=ln.local_call_time - timedelta(minutes=1))["dueAt"]
    with pytest.raises(ValueError):
        prep(ln, now=datetime(2026, 11, 25, 3, 0))


def test_h2_build_sheet_drops_do_not_call_prospect(config, parts):
    accts, signals, prospects, rr = parts
    key = account_key(rr.ranked[0].account)
    bad = replace(rr.prospects[key][0], do_not_call=True)
    rr2 = RankResult(ranked=rr.ranked, prospects={**rr.prospects, key: [bad]})
    s = cs.build_sheet(rr2, accts, signals, prospects, config, NOW)
    assert len(s.lines) == 7 and "do-not-call" in s.dropped[0].reason


# ---- M1: opener field validation
@pytest.mark.parametrize("field", cs.OPENER_FIELDS)
@pytest.mark.parametrize("val", [None, 0, ["a"], {"a": 1}, b"x", "​", "﻿", " ", ".", "-", "—", "none",
                                 "None", "N/A", "n/a", "null", "  ", "..."])
def test_m1_junk_refused_everywhere(sheet, field, val):
    with pytest.raises(cs.SheetError):
        prep(sheet.lines[0], dict(OPENER, **{field: val}))


@pytest.mark.parametrize("field", ["opener", "gatekeeper"])
@pytest.mark.parametrize("val", ["none verified", "None verified.", "None found: cold call on ICP fit only"])
def test_m1_honest_forms_not_ok_for_opener_or_gatekeeper(sheet, field, val):
    with pytest.raises(cs.SheetError, match="real text"):
        prep(sheet.lines[0], dict(OPENER, **{field: val}))


@pytest.mark.parametrize("field,val", [("fact_1", "none verified"), ("fact_2", "none verified"),
                                       ("trigger", "None found: cold call on ICP fit only")])
def test_m1_honest_forms_ok_elsewhere(sheet, field, val):
    assert val in prep(sheet.lines[0], dict(OPENER, **{field: val}))["note"]


def test_m1_zero_width_stripped_from_note(sheet):
    note = prep(sheet.lines[0], dict(OPENER, opener="Hi​ there Pat"))["note"]
    assert "​" not in note and "OPENER: Hi there Pat" in note


def test_m1_missing_key(sheet):
    bad = dict(OPENER); del bad["trigger"]
    with pytest.raises(cs.SheetError):
        prep(sheet.lines[0], bad)


# ---- M2: note cap
def test_m2_cap_refuses_not_truncates(sheet):
    with pytest.raises(cs.SheetError, match="cap"):
        prep(sheet.lines[0], dict(OPENER, opener="word " * 500))


def test_m2_exactly_at_cap_ok_and_over_refused(sheet):
    ln = sheet.lines[0]
    base = len(prep(ln, dict(OPENER, opener="a"))["note"])
    ok = prep(ln, dict(OPENER, opener="a" * (1 + cs.NOTE_MAX_CHARS - base)))
    assert len(ok["note"]) == cs.NOTE_MAX_CHARS
    with pytest.raises(cs.SheetError):
        prep(ln, dict(OPENER, opener="a" * (2 + cs.NOTE_MAX_CHARS - base)))


# ---- M3: scrub free text
def test_m3_scrub_unit():
    s = cs._scrub
    assert s("Head of Product +61400111222") == "Head of Product [redacted]"
    assert s("mail boss@company.example.com now") == "mail [redacted] now"
    assert s("call 02 9999 1234 or 0400 111 222 or +61 2 9999 1234") == "call [redacted] or [redacted] or [redacted]"
    assert s("021 123 4567") == "[redacted]"
    assert s("raised in 2026-11-22, series B, 2025 2026") == "raised in 2026-11-22, series B, 2025 2026"
    assert s("$20,000,000 round, 450 employees") == "$20,000,000 round, 450 employees"


def test_m3_title_with_phone(config):
    rows = load_rows()
    accts, signals, prospects = world(rows)
    prospects[0] = replace(prospects[0], title="Head of Product +61400111222")
    rr = rank_accounts(accts, signals, prospects, {}, set(), config, NOW)
    s = cs.build_sheet(rr, accts, signals, prospects, config, NOW)
    t = all_text(s)
    assert "61400111222" not in t and "[redacted]" in t


def test_m3_account_name_with_email(config):
    rows = load_rows()
    rows[0]["name"] = "boss@company.example.com Pty Ltd"
    accts, signals, prospects = world(rows)
    rr = rank_accounts(accts, signals, prospects, {}, set(), config, NOW)
    s = cs.build_sheet(rr, accts, signals, prospects, config, NOW)
    assert "boss@" not in all_text(s) and "[redacted] Pty Ltd" in cs.render_sheet_text(s)


def test_m3_scoop_evidence_with_contact(config, parts):
    accts, signals, prospects, rr = parts
    ra = copy.copy(next(r for r in rr.ranked if r.account.name == "Alpha Digital Pty Ltd"))
    ra.signals = [replace(ra.signals[0], evidence="Contact jo@scoop.example.com or +61 2 9999 1234 about the new lead")]
    s = cs.build_sheet(RankResult(ranked=[ra], prospects=rr.prospects), accts, signals, prospects, config, NOW)
    t = all_text(s)
    assert "jo@scoop" not in t and "9999 1234" not in t
    assert "Contact [redacted] or [redacted] about the new lead" == s.lines[0].packet.available_facts[0].text


def test_m3_bucket_names_scrubbed(config, parts):
    accts, signals, prospects, rr = parts
    sk = [RankedAccount(Account(sf_account_id=sf(300), name="x@y.example.com Co"), skipped_reason="no timezone")]
    s = cs.build_sheet(RankResult(skipped=sk), accts, signals, prospects, config, NOW)
    assert "x@y" not in json.dumps([b.__dict__ for b in s.skipped]) and "x@y" not in cs.render_sheet_text(s)


@pytest.mark.parametrize("val", ["Ring me on 0400 111 222", "Email x@y.example.com first", "call +61 2 9999 1234"])
def test_m3_opener_with_contact_data_refused(sheet, val):
    with pytest.raises(cs.SheetError, match="email address or phone"):
        prep(sheet.lines[0], dict(OPENER, opener=val))


def test_m3_sf_id_not_redacted(sheet):
    assert "003FAKE0001" in cs.render_packet_text(line_for(sheet, "Alpha Digital Pty Ltd").packet)


# ---- M4: repr
def test_m4_repr_hides_prospect_details(config):
    rows = load_rows()
    accts, signals, prospects = world(rows)
    prospects[0] = replace(prospects[0], name="Pat Smithson", email="pat.smithson@secret.example.org",
                           phone_e164="+61400123456")
    rr = rank_accounts(accts, signals, prospects, {}, set(), config, NOW)
    s = cs.build_sheet(rr, accts, signals, prospects, config, NOW)
    for obj in (s, s.lines[0], s.groups, s.lines):
        for text in (repr(obj), str(obj)):
            assert "Smithson" not in text and "secret.example.org" not in text and "61400123456" not in text


def test_m4_assertion_diff_safe(sheet):
    assert "person1@" not in repr(sheet) and "+6140000" not in repr(sheet)


# ---- M5: verdict from the real signal age
def _with_sig(config, parts, **kw):
    accts, signals, prospects, rr = parts
    ra = copy.copy(next(r for r in rr.ranked if r.account.name == "Alpha Digital Pty Ltd"))
    ra.signals = [replace(ra.signals[0], **kw)]
    return cs.build_sheet(RankResult(ranked=[ra], prospects=rr.prospects), accts, signals, prospects, config, NOW).lines[0]


def test_m5_stale_signal_never_says_within_limit(config, parts):
    ln = _with_sig(config, parts, kind=SignalKind.FUNDING, source_date=NOW - timedelta(days=400))
    ps = ln.packet.signals[0]
    assert ps.status == "stale" and "within" not in ps.verdict_reason and "older than its 45 day limit" in ps.verdict_reason
    assert "signal older than limit" in ln.flags and "no dated trigger" in ln.flags
    assert ln.packet.has_real_trigger is False and ln.packet.trigger_kind is None
    assert not any(f.source.startswith("ZoomInfo scoop") for f in ln.packet.available_facts)
    assert "left out of the facts" in ln.packet.facts_note
    assert "Flag: signal older than its limit" in ln.why_chosen


def test_m5_future_signal_flagged(config, parts):
    ln = _with_sig(config, parts, source_date=NOW + timedelta(days=10))
    ps = ln.packet.signals[0]
    assert ps.status == "future" and ps.verdict_reason == "future-dated: check the source"
    assert "signal in the future" in ln.flags and ln.packet.has_real_trigger is False
    assert any("future-dated: check the source" in w for w in ln.why_chosen)
    assert not any(f.source.startswith("ZoomInfo scoop") for f in ln.packet.available_facts)


def test_m5_exec_move_within_90_day_limit_ok(config, parts):
    ln = _with_sig(config, parts, source_date=NOW - timedelta(days=80))
    assert ln.packet.signals[0].status == "ok" and "within 90 day limit" in ln.packet.signals[0].verdict_reason


def test_m5_exec_move_over_90_days_stale(config, parts):
    assert _with_sig(config, parts, source_date=NOW - timedelta(days=95)).packet.signals[0].status == "stale"


def test_m5_ok_signal_unchanged(sheet):
    p = line_for(sheet, "Alpha Digital Pty Ltd").packet
    assert p.has_real_trigger and p.trigger_kind == "exec_move" and p.facts_note == ""


def test_m5_intent_only_is_not_a_real_trigger(config, parts):
    ln = _with_sig(config, parts, kind=SignalKind.INTENT, source="zoominfo_intent:x",
                   source_date=NOW - timedelta(days=5), evidence="Interest in digital adoption")
    p = ln.packet
    assert p.has_real_trigger is False and p.trigger_kind is None and p.signals[0].status == "intent"
    assert "interest signal, not a dated event" in p.signals[0].verdict_reason
    assert "no dated trigger" in ln.flags and "No dated trigger: interest signal only" in ln.why_chosen
    assert "TRIGGER: none usable" in cs.render_packet_text(p)


def test_m5_intent_plus_dated_uses_dated(config, parts):
    accts, signals, prospects, rr = parts
    ra = copy.copy(next(r for r in rr.ranked if r.account.name == "Alpha Digital Pty Ltd"))
    intent = Signal(None, SignalKind.INTENT, 0.3, "zoominfo_intent:x", NOW - timedelta(days=2), "Interest")
    ra.signals = [ra.signals[0], intent]
    ln = cs.build_sheet(RankResult(ranked=[ra], prospects=rr.prospects), accts, signals, prospects, config, NOW).lines[0]
    assert ln.packet.has_real_trigger and ln.packet.trigger_kind == "exec_move"


def test_m5_undated_non_intent_is_usable_but_says_so(config, parts):
    ln = _with_sig(config, parts, source_date=None)
    ps = ln.packet.signals[0]
    assert ps.status == "undated" and "date unknown" in ps.verdict_reason and ln.packet.has_real_trigger


# ---- M6: bucket limit
def many_skipped(n):
    return [RankedAccount(Account(sf_account_id=sf(400 + i), name=f"Skip {i:03d}"), skipped_reason="no timezone")
            for i in range(n)]


def test_m6_text_none_lists_every_account(config, parts):
    accts, signals, prospects, rr = parts
    s = cs.build_sheet(RankResult(skipped=many_skipped(80)), accts, signals, prospects, config, NOW)
    full = cs.render_sheet_text(s, bucket_limit=None)
    assert all(f"Skip {i:03d}" in full for i in range(80)) and "more" not in full.split("Top skip")[0]
    assert "and 55 more" in cs.render_sheet_text(s)


def test_m6_markdown_none_lists_every_account_and_overflow_clean(config, parts):
    accts, signals, prospects, rr = parts
    s = cs.build_sheet(RankResult(skipped=many_skipped(80)), accts, signals, prospects, config, NOW)
    assert all(f"Skip {i:03d}" in cs.render_sheet_markdown(s, bucket_limit=None) for i in range(80))
    md = cs.render_sheet_markdown(s)
    assert "Skip 024" in md and "Skip 025" not in md and "- ... and 55 more" in md and "**  " not in md


def test_m6_custom_limit(config, parts):
    accts, signals, prospects, rr = parts
    s = cs.build_sheet(RankResult(skipped=many_skipped(10)), accts, signals, prospects, config, NOW)
    t = cs.render_sheet_text(s, bucket_limit=3)
    assert "Skip 002" in t and "Skip 003" not in t and "and 7 more" in t


def test_m6_json_names_all(config, parts):
    accts, signals, prospects, rr = parts
    s = cs.build_sheet(RankResult(skipped=many_skipped(80)), accts, signals, prospects, config, NOW)
    assert len(json.loads(cs.sheet_to_json(s))["skipped"]) == 80


# ---- Low: first names
@pytest.mark.parametrize("name,expected", [
    ("Pat Smith", "Pat"), ("Dr Pat Smith", "Pat"), ("Dr. Pat Smith", "Pat"), ("Prof. Jane Doe", "Jane"),
    ("Mr Sam Jones", "Sam"), ("Ms Kim Lee", "Kim"), ("Smith, Pat", "Pat"), ("Smith, Dr Pat", "Pat"),
    ("Smith, Pat Q.", "Pat"), ("Mary-Jane O'Neil", "Mary-Jane"),
    ("Pat", None), ("Dr Smith", None), ("Dr", None), ("Smith,", None), ("Smith, Jr", None), ("J. Smith", None),
    ("", None), (None, None), ("   ", None), ("12345 6789", None)])
def test_low_first_name(name, expected):
    assert cs._first_name(name) == expected


def test_low_unsure_name_shows_the_contact(config):
    rows = load_rows()
    accts, signals, prospects = world(rows)
    prospects[0] = replace(prospects[0], name="Smith,")
    rr = rank_accounts(accts, signals, prospects, {}, set(), config, NOW)
    s = cs.build_sheet(rr, accts, signals, prospects, config, NOW)
    ln = line_for(s, "Alpha Digital Pty Ltd")
    assert ln.packet.prospect_first_name is None and ln.why_chosen[0] == "Prospect: the contact, Head of Product"
    assert "the contact" in cs.render_sheet_text(s) and "Smith," not in all_text(s)


# ---- Low: markdown escaping
def test_low_markdown_escaping(config):
    rows = load_rows()
    rows[0]["name"] = "Evil\n# Injected heading | `x` Pty"
    rows[0]["title"] = "Head of Product " + "z" * 500
    accts, signals, prospects = world(rows)
    rr = rank_accounts(accts, signals, prospects, {}, set(), config, NOW)
    s = cs.build_sheet(rr, accts, signals, prospects, config, NOW)
    md = cs.render_sheet_markdown(s)
    assert not any(x.startswith("# Injected") for x in md.splitlines())
    assert "Evil \\# Injected heading \\| \\`x\\` Pty" in md
    assert max(len(x) for x in md.splitlines()) < 400 and "z" * 200 not in md


def test_low_md_helper():
    assert cs._md("a|b`c\nd#e") == "a\\|b\\`c d\\#e"
    assert len(cs._md("x" * 500, 50)) == 50


def test_low_account_name_single_line_in_text(config):
    rows = load_rows()
    rows[0]["name"] = "Two\nLine Co"
    accts, signals, prospects = world(rows)
    rr = rank_accounts(accts, signals, prospects, {}, set(), config, NOW)
    t = cs.render_sheet_text(cs.build_sheet(rr, accts, signals, prospects, config, NOW))
    assert "Two Line Co" in t


def test_low_unused_import_removed():
    import ast
    tree = ast.parse(Path(cs.__file__).read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    imported = [a.asname or a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names]
    assert [i for i in imported if i not in names and i != "annotations"] == []
