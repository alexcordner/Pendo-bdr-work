"""Phase 2 end-to-end tests (task 2f). Offline, synthetic: the world in tests/phase2_world.py is built from the
patterns of the first live dry run with INVENTED names. The real src.pipeline.run_pipeline, src.accounts,
src.signals, src.ranker, src.callsheet and src.textcheck run together; nothing is mocked.

Scenarios: (i) assistants / junk titles, (ii) duplicate pairs, (iii) trigger weights and notes, (iv) personal
email, textcheck against REAL packets built from real signals with notes. Gates, determinism, scale and the
hard-rule scan are in tests/test_integration_phase2_gates.py."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import accounts as acc_mod
from src import callsheet as cs
from src import ranker
from src import signals as sg
from src import textcheck as tc
from src.config import load_config
from src.models import SignalKind
from src.pipeline import run_pipeline
from tests.intake_helpers import write_raw
from tests.phase2_world import HEAD_PROD, LONG_EA_TITLE, PERSONAL_EMAIL, SHARED_NAMES, phase2_world
from tests.test_integration_pipeline import NOW, sf15

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def cfg():
    return load_config(ROOT / "config" / "config.yaml")


@pytest.fixture(scope="module")
def built(tmp_path_factory, cfg):
    tmp = tmp_path_factory.mktemp("phase2")
    w = phase2_world()
    w.zoominfo_rows = []
    raw = write_raw(w, tmp / "raw", NOW, owner=cfg.owner.nooks_user_id)
    res = run_pipeline(raw, cfg, tmp / "state", NOW, log_dir=tmp / "logs")
    sheet = cs.build_sheet(res.rank_result, res.accounts_result.accounts, res.signals, res.prospects, cfg, NOW,
                           limit=60)
    return {"world": w, "res": res, "sheet": sheet, "tmp": tmp, "raw": raw}


def line(built, account_name):
    return next(x for x in built["sheet"].lines if x.account.name == account_name)


def names_on_sheet(sheet):
    return [x.account.name for x in sheet.lines]


def acct(built, name):
    return next(a for a in built["res"].accounts_result.accounts if a.name == name)


def best(built, n):
    return sg.best_signal(built["res"].signals.get(sf15(n), []))


# ---------------------------------------------------------------- (i) assistants and junk titles
def test_i_long_multi_role_title_is_never_chosen(built):
    assert len(LONG_EA_TITLE) >= 110
    for ps in built["res"].rank_result.prospects.values():
        for p in ps:
            assert "executive assistant" not in (p.title or "").lower()
    for ln in built["sheet"].lines:
        assert "assistant" not in (ln.entry.prospect.title or "").lower().replace("assistant director", "")


def test_i_only_contact_is_a_junk_title_means_needs_prospect(built):
    rr = built["res"].rank_result
    quill = acct(built, "Quill Only Assistant Pty Ltd")
    key = ranker.account_key(quill)
    assert rr.needs_prospect_codes[key] == "junk_title"
    assert quill.name not in names_on_sheet(built["sheet"])
    assert any(b.name == quill.name and b.reason.startswith("junk_title") for b in built["sheet"].needs_prospect)
    assert key not in rr.prospects                         # never a prospect, so never a task payload


def test_i_second_clean_contact_is_chosen_instead(built):
    ln = line(built, "Reed Assistant And Clean Pty Ltd")
    assert ln.entry.prospect.title == HEAD_PROD
    assert ln.packet.prospect_first_name == "Cleo"
    assert "Prospect: Cleo, Head of Product" == ln.why_chosen[0]


def test_i_assistant_director_of_product_is_chosen(built):
    ln = line(built, "Sable Assistant Director Pty Ltd")
    assert ln.entry.prospect.title == "Assistant Director of Product"


def test_i_junk_title_is_in_the_sheet_bucket_enumeration_and_log_counts(built):
    assert "junk_title" in ranker.NEEDS_PROSPECT_CODES
    text = cs.render_sheet_text(built["sheet"])
    assert "Quill Only Assistant Pty Ltd: junk_title" in text
    log = "\n".join(built["res"].log_lines)
    assert "junk_title (needs prospect" in log and built["res"].stats["needs_prospect_junk_title"] == 1


# ---------------------------------------------------------------- (ii) duplicates
def test_ii_real_build_account_list_flags_catapult_pair_only(built):
    flagged = sorted(a.name for a in built["res"].accounts_result.accounts if "possible_duplicate" in a.flags)
    assert flagged == ["Catapult", "Catapult Communications"]
    for a in built["res"].accounts_result.accounts:
        if a.name in ("Bank Australia Pty Ltd", "Arab Bank Pty Ltd"):
            assert "possible_duplicate" not in a.flags
    notes = [n for a in acct_pair(built) for n in a.notes if n.startswith("possible_duplicate group dup-")]
    assert len(notes) == 2 and all("name+shared contacts" in n for n in notes)
    groups = acc_mod.duplicate_groups(built["res"].accounts_result.accounts)
    assert [sorted(x.name for x in g) for g in groups] == [["Catapult", "Catapult Communications"]]
    assert built["res"].stats["possible_duplicate_groups"] == 1 and built["res"].stats["possible_duplicate_accounts"] == 2
    assert len(SHARED_NAMES) == 13


def acct_pair(built):
    return [a for a in built["res"].accounts_result.accounts if a.name.startswith("Catapult")]


def test_ii_higher_scoring_stays_other_collapses_and_is_mentioned(built):
    names = names_on_sheet(built["sheet"])
    assert "Catapult Communications" in names and "Catapult" not in names
    ranked = {r.account.name: r for r in built["res"].rank_result.ranked}
    assert len(ranked["Catapult Communications"].signals) == 9
    assert ranked["Catapult Communications"].score > ranked["Catapult"].score
    ln = line(built, "Catapult Communications")
    assert "also on your list as Catapult: probable same company" in ln.why_chosen
    assert any(d.name == "Catapult" and "probable duplicate of Catapult Communications" in d.reason
               for d in built["sheet"].dropped)


def test_ii_never_both_on_the_sheet_at_any_limit(built, cfg):
    res = built["res"]
    for limit in range(1, 24):
        sheet = cs.build_sheet(res.rank_result, res.accounts_result.accounts, res.signals, res.prospects, cfg, NOW,
                               limit=limit)
        names = names_on_sheet(sheet)
        assert not {"Catapult Communications", "Catapult"} <= set(names), limit
        assert len(names) == min(limit, len(res.rank_result.ranked) - 1)                # the slot goes to the next account, never left empty
    # the sheet builder also works when the accounts argument is empty (groups come from the ranked accounts)
    sheet = cs.build_sheet(res.rank_result, [], res.signals, res.prospects, cfg, NOW)
    assert "Catapult" not in names_on_sheet(sheet)


def test_ii_false_positive_pairs_are_not_collapsed(built):
    names = names_on_sheet(built["sheet"])
    assert "Bank Australia Pty Ltd" in names and "Arab Bank Pty Ltd" in names
    assert not any(d.name in ("Bank Australia Pty Ltd", "Arab Bank Pty Ltd") for d in built["sheet"].dropped)
    assert not cs.probable_duplicates(acct(built, "Bank Australia Pty Ltd"), acct(built, "Arab Bank Pty Ltd"))
    assert cs.probable_duplicates(*acct_pair(built))


# ---------------------------------------------------------------- (iii) triggers
def test_iii_lab_consumables_launch_is_half_weight_with_note(built):
    lab, digital = best(built, 120), best(built, 128)
    assert lab.kind is SignalKind.PRODUCT_LAUNCH and digital.kind is SignalKind.PRODUCT_LAUNCH
    assert 0.40 < lab.strength / digital.strength < 0.60            # x0.5 (one day of recency apart)
    assert any(n.startswith("physical or unclear") for n in sg.signal_notes(lab))
    assert sg.signal_notes(digital) == []


def test_iii_to_be_acquired_typed_funding_becomes_ma_target(built):
    s = best(built, 121)
    assert s.kind is SignalKind.MA
    v = next(v for v in built["res"].verdicts if v.scoop.company_name == "Eqtbound Bank Pty Ltd")
    assert "reclassified as M&A" in v.reason and "being acquired" in v.reason


def test_iii_two_accounts_on_the_same_stake_deal_show_same_deal_as(built):
    holder, foo = best(built, 122), best(built, 123)
    assert "same deal as Foo Digital Pty Ltd" in sg.signal_notes(holder)
    assert "same deal as Holder Capital Pty Ltd" in sg.signal_notes(foo)
    assert any(n.startswith("acquirer of a minority stake") for n in sg.signal_notes(holder))
    assert "Flag: same deal as Foo Digital Pty Ltd" in line(built, "Holder Capital Pty Ltd").why_chosen
    assert "Flag: same deal as Holder Capital Pty Ltd" in line(built, "Foo Digital Pty Ltd").why_chosen


def test_iii_debt_facility_is_down_weighted(built):
    debt, equity = best(built, 124), best(built, 129)
    assert debt.kind is SignalKind.FUNDING and equity.kind is SignalKind.FUNDING
    assert debt.strength < 0.5 * equity.strength
    assert sg.signal_notes(debt) == ["debt facility, not an equity round"]


def test_iii_property_fund_launch_is_down_weighted(built):
    fund, digital = best(built, 125), best(built, 128)
    assert fund.strength < 0.3 * digital.strength
    assert sg.signal_notes(fund) == ["financial or property product launch, not a digital product"]


def test_iii_cfo_to_ceo_at_86_days_is_dropped_neither_role_is_a_buyer_role(built):
    assert built["res"].signals.get(sf15(126)) is None
    v = next(v for v in built["res"].verdicts if v.scoop.company_name == "Cfopromoted Pty Ltd")
    assert not v.keep and "neither the new nor the vacated role is a buyer role" in v.reason
    ln = line(built, "Cfopromoted Pty Ltd")
    assert "No trigger: ranked on fit only" in ln.why_chosen and not ln.packet.has_real_trigger


def test_iii_rumour_is_half_of_the_acquirer_weight(built):
    rumour, full = best(built, 127), best(built, 132)
    assert rumour.kind is SignalKind.MA and full.kind is SignalKind.MA
    assert rumour.strength / full.strength == pytest.approx(0.5, abs=0.02)
    assert sg.signal_notes(rumour) == ["reported talks, not a confirmed deal"]
    assert sg.signal_notes(full) == []


def test_iii_ambiguous_role_is_kept_and_flagged(built):
    s = best(built, 133)
    assert sg.is_ambiguous(s)
    ln = line(built, "Ambigretail Pty Ltd")
    assert ln.packet.ambiguity_flags == [sg.AMBIGUOUS_NOTE] and ln.packet.signals[0].ambiguous
    assert "role ambiguous" in ln.flags


# ---- signal notes on the sheet and in the packets
@pytest.mark.parametrize("name,note", [
    ("Labware Supplies Pty Ltd", "physical or unclear product launch: check it is digital"),
    ("Airlike Infrastructure Pty Ltd", "debt facility, not an equity round"),
    ("Chartlike Property Group Pty Ltd", "financial or property product launch, not a digital product"),
    ("Acqrumour Pty Ltd", "reported talks, not a confirmed deal"),
    ("Holder Capital Pty Ltd", "acquirer of a minority stake or unrelated business"),
    ("Ambigretail Pty Ltd", "role ambiguous: check it is a digital product role"),
])
def test_c_notes_are_in_why_chosen_flags_and_packet_but_not_in_the_quotable_text(built, name, note):
    ln = line(built, name)
    assert f"Flag: {note}" in ln.why_chosen
    assert note in ln.packet.signals[0].notes and note in ln.packet.signals[0].verdict_reason
    pk = ln.packet.to_dict()
    assert "(" + note not in pk["signals"][0]["evidence"] and note not in pk["signals"][0]["evidence"]
    assert all(note not in f["text"] for f in pk["available_facts"])
    assert note.split(":")[0] not in " ".join(f["text"] for f in pk["available_facts"])
    if not note.startswith("role ambiguous"):
        assert note in pk["facts_note"] and any(note in m for m in pk["must_not_claim"])
        assert pk["must_not_phrases"]
    assert note not in "".join(ln.packet.signals[0].evidence)
    assert note in cs.render_packet_text(ln.packet)


def test_c_target_direction_is_carried_on_the_signal_and_reaches_the_sheet(built):
    """The 'being acquired' note travels on Signal.evidence (signals.signal_notes), so the packet notes, the
    'Flag:' line in why_chosen and the sheet flags all show it for the target of a full acquisition."""
    note = "being acquired: leadership/platform change likely"
    ln = line(built, "Eqtbound Bank Pty Ltd")
    assert ln.packet.signals[0].notes == (note,)
    assert f"Flag: {note}" in ln.why_chosen and note in ln.flags
    assert note not in ln.packet.to_dict()["signals"][0]["evidence"]          # kept out of the quotable text


# ---------------------------------------------------------------- (iv) personal email
def test_iv_personal_email_is_on_the_sheet_call_only(built):
    ln = line(built, "Personalmail Pty Ltd")
    assert "personal_email" in ln.flags
    assert ln.packet.email_ok is False and ln.packet.email_reason == "personal_email"
    assert cs.email_draft_allowed(ln) is False
    assert any("contact email is personal" in x for x in ln.why_chosen)
    assert any("Do not suggest or draft an email" in m for m in ln.packet.must_not_claim)
    assert built["res"].stats["chosen_personal_email"] == 1
    assert cs.email_draft_allowed(line(built, "Normalmail Pty Ltd")) is True


def _everything_text(built) -> list[tuple[str, str]]:
    sheet = built["sheet"]
    out = [("sheet text", cs.render_sheet_text(sheet)), ("sheet markdown", cs.render_sheet_markdown(sheet)),
           ("sheet json", cs.sheet_to_json(sheet)),
           ("run log", "\n".join(built["res"].log_lines)),
           ("log file", Path(built["res"].log_path).read_text())]
    for ln in sheet.lines:
        out.append((f"packet {ln.account.name}", json.dumps(ln.packet.to_dict())))
        out.append((f"packet text {ln.account.name}", cs.render_packet_text(ln.packet)))
        out.append((f"why {ln.account.name}", " | ".join(ln.why_chosen + ln.flags)))
    return out


def test_iv_the_personal_address_and_no_other_pii_appear_anywhere(built):
    import re
    w = built["world"]
    emails = {c["Email"].lower() for c in w.contacts if c.get("Email")} | {PERSONAL_EMAIL}
    local_parts = {e.split("@")[0] for e in emails}
    phones = {c["MobilePhone"] for c in w.contacts if c.get("MobilePhone")}
    surnames = {(c.get("LastName") or "").lower() for c in w.contacts if c.get("LastName")}
    surnames.discard("")
    assert len(surnames) > 30 and len(phones) > 30
    for where, text in _everything_text(built):
        low = text.lower()
        for e in emails | local_parts:
            assert e not in low, (where, "email", e)
        assert "gmail.com" not in low, where
        assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), where
        for ph in phones:
            assert ph not in text and ph.lstrip("+") not in text, (where, "phone")
        # surnames: "Sharedvp" etc. A surname that is also an account name word (e.g. 'Holder') is excluded below
        account_words = {x.lower() for a in w.sf_rows for x in a["Name"].replace(",", " ").split()}
        for sn in surnames - account_words:
            assert sn not in low, (where, "surname", sn)


# ---------------------------------------------------------------- textcheck against REAL packets (c, d)
@pytest.fixture(scope="module")
def vcfg():
    return tc.load_voice_config(ROOT / "config" / "voice.yaml")


LABEL = "DRAFT STYLE: not yet calibrated to Alex"
EMP = "About 220 employees (source: Salesforce account record (ZoomInfo employee count))"


def block(built, name, opener, gatekeeper="Who looks after your digital products?", fact1=None):
    pk = line(built, name).packet.to_dict()
    sig = pk["signals"][0]
    ev = fact1 or f"{sig['evidence']} (source: {sig['source_label']})"
    return pk, "\n".join([LABEL, f"OPENER: {opener}", f"FACT 1: {ev}", f"FACT 2: {EMP}", f"GATEKEEPER: {gatekeeper}",
                          f"TRIGGER: {sig['evidence']} (source: {sig['source_label']})"])


def codes(v, severity=None):
    return sorted({x.code for x in v if severity is None or x.severity == severity})


GOOD = [
    ("Labware Supplies Pty Ltd", "Hi Gus, Alex from Pendo. Saw Labware launched a new lab consumables range. Got a minute?"),
    ("Airlike Infrastructure Pty Ltd", "Hi Kim, Alex from Pendo. Saw the new debt facility. Got a minute?"),
    ("Chartlike Property Group Pty Ltd", "Hi Lee, Alex from Pendo. Saw the new property fund. Got a minute?"),
    ("Acqrumour Pty Ltd", "Hi Nia, Alex from Pendo. Saw the report on talks to acquire Target Rumoured. Got a minute?"),
    ("Foo Digital Pty Ltd", "Hi Jon, Alex from Pendo. Saw Holder is taking a stake in Foo Digital. Got a minute?"),
    ("Eqtbound Bank Pty Ltd", "Hi Hal, Alex from Pendo. Saw Eqtbound is being acquired by EQT. Got a minute?"),
    ("Plainfunding Pty Ltd", "Hi Pam, Alex from Pendo. Saw the Series B. Got a minute?"),
]
BAD = [
    ("Labware Supplies Pty Ltd", "Hi Gus, Alex from Pendo. Congrats on the new app launch. Got a minute?"),
    ("Airlike Infrastructure Pty Ltd", "Hi Kim, Alex from Pendo. Congrats on the funding round. Got a minute?"),
    ("Chartlike Property Group Pty Ltd", "Hi Lee, Alex from Pendo. Congrats on the new product launch. Got a minute?"),
    ("Acqrumour Pty Ltd", "Hi Nia, Alex from Pendo. Congrats on acquiring Target Rumoured. Got a minute?"),
]


@pytest.mark.parametrize("name,opener", GOOD, ids=[g[0] for g in GOOD])
def test_d_good_blocks_lint_clean_against_real_packets_with_notes(built, vcfg, name, opener):
    pk, text = block(built, name, opener)
    v = tc.check_opener_block(text, pk, vcfg)
    assert not tc.has_errors(v), [(x.code, x.message) for x in v]


@pytest.mark.parametrize("name,opener", BAD, ids=[g[0] for g in BAD])
def test_d_bad_blocks_overstating_a_noted_trigger_are_rejected(built, vcfg, name, opener):
    pk, text = block(built, name, opener)
    assert "MUST_NOT_CLAIM" in codes(tc.check_opener_block(text, pk, vcfg), "error")


def test_d_fact_line_quoting_the_note_text_is_not_a_packet_fact(built, vcfg):
    sig = line(built, "Labware Supplies Pty Ltd").packet.to_dict()["signals"][0]
    quoted = f"{sig['evidence']} (physical or unclear product launch: check it is digital) (source: {sig['source_label']})"
    pk, text = block(built, "Labware Supplies Pty Ltd", "Hi Gus, Alex from Pendo. Saw Labware launched a new lab "
                     "consumables range. Got a minute?", fact1=quoted)
    assert "FACT_UNSUPPORTED" in codes(tc.check_opener_block(text, pk, vcfg), "error")


def test_d_packet_fields_are_read_as_callsheet_emits_them(built):
    for ln in built["sheet"].lines:
        pk = tc.normalize_packet(ln.packet.to_dict())
        assert pk.account_name == ln.account.name
        assert pk.contact_values == []                                          # no email / phone key in a packet
        assert pk.trigger_kind == (ln.packet.trigger_kind if ln.packet.has_real_trigger else "none")
        assert pk.ambiguous_role == bool(ln.packet.ambiguity_flags)
        assert pk.must_not_phrases is not None
    d = line(built, "Ambigretail Pty Ltd").packet.to_dict()
    assert d["ambiguity_flags"] == [sg.AMBIGUOUS_NOTE] and d["signals"][0]["ambiguous"] is True
    assert d["signals"][0]["status"] in ("ok", "undated") and d["email_ok"] is True and d["email_reason"] == "ok"
    pd = line(built, "Personalmail Pty Ltd").packet.to_dict()
    assert pd["email_ok"] is False and pd["email_reason"] == "personal_email"


def test_d_role_assumed_is_flagged_for_the_ambiguous_packet(built, vcfg):
    pk, text = block(built, "Ambigretail Pty Ltd", "Hi Sia, as Head of Product you will own this. Got a minute?")
    assert "ROLE_ASSUMED" in codes(tc.check_opener_block(text, pk, vcfg), "error")
    pk, ok = block(built, "Ambigretail Pty Ltd", "Hi Sia, Alex from Pendo. Saw the new product hire. Got a minute?")
    assert "ROLE_ASSUMED" not in codes(tc.check_opener_block(ok, pk, vcfg))


def test_d_email_draft_for_a_personal_email_packet_still_lints_as_text_only(built, vcfg):
    """textcheck has no email_ok rule: refusing to draft is callsheet.email_draft_allowed's job (Phase 3 must call it)."""
    ln = line(built, "Personalmail Pty Ltd")
    assert cs.email_draft_allowed(ln) is False
    assert "email_ok" in ln.packet.to_dict()
