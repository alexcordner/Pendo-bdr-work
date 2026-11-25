"""Second-round tests for src/textcheck.py (answers docs/review_2d.md). FAKE data only, offline.

Contents: config validation, unicode evasion, the reviewer's labelled corpus (docs/review_2d_labels.json) as a
measured regression gate, my own FRESH adversarial and natural cases (written after the rules were tuned, not used
to tune them), real 2c packets built by src/callsheet.py, and the CLI."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from src import callsheet as cs
from src import textcheck as tc
from src.ranker import rank_accounts
from src.textcheck import (ERROR, WARNING, VoiceConfigError, check_email_draft, check_opener_block, has_errors,
                           load_voice_config, normalize_text)
from tests.test_callsheet import NOW, world

ROOT = Path(__file__).resolve().parent.parent
LABEL = "DRAFT STYLE: not yet calibrated to Alex"
PACKETS = json.loads((ROOT / "tests/fixtures/text_packets.json").read_text())
PACKETS.pop("_comment", None)
LABELS = json.loads((ROOT / "docs/review_2d_labels.json").read_text())
PACKETS.update(LABELS["extra_packets"])


@pytest.fixture(scope="module")
def real_cfg():
    return load_voice_config(ROOT / "config/voice.yaml")


@pytest.fixture()
def cfg(real_cfg):
    c = copy.deepcopy(real_cfg)
    c["calibrated"] = True
    return c


def errs(vs, sev=ERROR):
    return {v.code for v in vs if v.severity == sev}


def fact_line(it: dict) -> str:
    src = it["source"] + (f", {it['date']}" if it.get("date") else "")
    return f"{it['text']} (source: {src})"


def block(pk: dict, opener: str, gk: str = "Who looks after the product side there, please?", label: bool = False,
          f2: str | None = None) -> str:
    """A well-formed block for packet ``pk`` (old fixture shape or real 2c shape) with the given opener."""
    facts = pk.get("available_facts") or []
    trig = pk.get("trigger") or next((s for s in pk.get("signals", []) if s.get("status") in ("ok", "undated")), None)
    f1 = fact_line(facts[0]) if facts else "none verified"
    f2_ = f2 or (fact_line(facts[1]) if len(facts) > 1 else "none verified")
    if trig and trig.get("kind") not in ("none", None, "intent") and pk.get("has_real_trigger", True):
        date = f", {trig['date']}" if trig.get("date") else ""
        t = f"{trig['evidence'].rstrip('.')} (source: {trig['source']}{date})"
    else:
        t = tc.NO_TRIGGER_LINE
    lines = ([LABEL] if label else []) + [f"OPENER: {opener}", f"FACT 1: {f1}", f"FACT 2: {f2_}", f"GATEKEEPER: {gk}",
                                         f"TRIGGER: {t}"]
    return "\n".join(lines)


# ============================================================================== config validation (B5)

def test_real_config_validates(real_cfg):
    assert real_cfg["calibrated"] is False


@pytest.mark.parametrize("mutate,needle", [
    (lambda c: c.pop("banned_phrases"), "missing voice config key 'banned_phrases'"),
    (lambda c: c.__setitem__("banned_phrase", []), "unknown voice config key"),
    (lambda c: c.__setitem__("calibrated", "false"), "real boolean"),
    (lambda c: c.__setitem__("calibrated", 0), "real boolean"),
    (lambda c: c["opener"].pop("max_words"), "opener.max_words"),
    (lambda c: c["opener"].__setitem__("max_words", "25"), "non-negative integer"),
    (lambda c: c["opener"].__setitem__("max_words", -1), "non-negative integer"),
    (lambda c: c["opener"].__setitem__("max_wrods", 25), "unknown voice config key"),
    (lambda c: c.__setitem__("banned_phrases", "quick call"), "list of non-empty strings"),
    (lambda c: c.__setitem__("banned_phrases", ["ok", ""]), "list of non-empty strings"),
    (lambda c: c.__setitem__("banned_phrases", ["ok", None]), "list of non-empty strings"),
    (lambda c: c.__setitem__("unsupported_claim_patterns", ["(unclosed"]), "invalid regex"),
    (lambda c: c.__setitem__("fact_overlap_min", 1.5), "between 0 and 1"),
    (lambda c: c.__setitem__("draft_style_label", ""), "non-empty string"),
    (lambda c: c["alex_examples"].__setitem__("disliked_openers", [None, ""]), "list of non-empty strings"),
    (lambda c: c["alex_examples"].pop("proud_emails"), "alex_examples.proud_emails"),
    (lambda c: c.__setitem__("title_acronyms", ["ceo"]), "mapping of string to string"),
    (lambda c: c["email"].__setitem__("allow_links", "no"), "real boolean"),
])
def test_bad_config_raises(real_cfg, mutate, needle):
    bad = copy.deepcopy(real_cfg)
    mutate(bad)
    with pytest.raises(VoiceConfigError, match=needle):
        check_opener_block("OPENER: x", {}, bad)
    with pytest.raises(VoiceConfigError):
        check_email_draft("a b", "x", {}, bad)


def test_load_voice_config_rejects_a_typo_in_the_file(tmp_path, real_cfg):
    d = copy.deepcopy(real_cfg)
    d["banned_phrase"] = d.pop("banned_phrases")
    p = tmp_path / "v.yaml"
    p.write_text(yaml.safe_dump(d))
    with pytest.raises(VoiceConfigError):
        load_voice_config(p)


def test_load_voice_config_rejects_empty_file(tmp_path):
    p = tmp_path / "v.yaml"
    p.write_text("")
    with pytest.raises(VoiceConfigError):
        load_voice_config(p)


def test_calibrated_string_false_cannot_switch_off_the_label_check(real_cfg):
    bad = dict(real_cfg, calibrated="false")
    with pytest.raises(VoiceConfigError):
        check_opener_block(block(PACKETS["exec_move"], "Hi Priya, Alex from Pendo. Who owns the app?"),
                           PACKETS["exec_move"], bad)


# ============================================================================== unicode evasion (B6)

def _op(cfg, opener, key="exec_move", **kw):
    return check_opener_block(block(PACKETS[key], opener, **kw), PACKETS[key], cfg)


@pytest.mark.parametrize("sep", ["​", "­", "﻿", "⁠", "‍"])
def test_invisible_characters_cannot_hide_a_banned_phrase(cfg, sep):
    vs = _op(cfg, f"Hi Priya, Alex from Pendo. Can I touch{sep} base? Who owns the app?")
    assert {"BANNED_PHRASE", "UNICODE_SUSPECT"} <= errs(vs)


def test_nbsp_is_normalised_not_an_evasion(cfg):
    vs = _op(cfg, "Hi Priya, Alex from Pendo. Quick call? Who owns the app?")
    assert "BANNED_PHRASE" in errs(vs) and "UNICODE_SUSPECT" not in errs(vs)


def test_cyrillic_lookalike_in_banned_word(cfg):
    vs = _op(cfg, "Hi Priya, Alex from Pendo. Our gаme-changing tool. Who owns the app?")
    assert {"BANNED_PHRASE", "UNICODE_SUSPECT"} <= errs(vs)


def test_cyrillic_lookalike_in_event_word_with_no_trigger(cfg):
    vs = _op(cfg, "Hi Ellie, Alex from Pendo. Congrаts on the funding. Who owns product?", key="none_cold")
    assert {"INVENTED_TRIGGER", "UNICODE_SUSPECT"} <= errs(vs)


def test_greek_lookalike(cfg):
    assert "UNICODE_SUSPECT" in errs(_op(cfg, "Hi Priya, Alex from Pendo. Οwns the app? Who does?"))


def test_fullwidth_exclamation_and_digits(cfg):
    vs = _op(cfg, "Hi Priya, Alex from Pendo！ Who owns the app? Larkspur has ７０ staff.")
    assert {"EXCLAMATION", "NUMBER_UNSUPPORTED"} <= errs(vs)


def test_smart_quotes_are_plain_apostrophes(cfg):
    vs = _op(cfg, "Hi Priya, Alex from Pendo. Hope you’re well. Who owns the app?")
    assert "BANNED_PHRASE" in errs(vs)


def test_other_scripts_are_suspect(cfg):
    assert "UNICODE_SUSPECT" in errs(_op(cfg, "Hi Priya, Alex from Pendo. 你好. Who owns the app?"))


def test_macrons_in_maori_names_are_fine(cfg):
    pk = copy.deepcopy(PACKETS["funding"])
    pk["prospect_first_name"] = "Tāne"
    o = block(pk, "Hi Tāne, Alex from Pendo. Who owns product there?")
    assert not has_errors(check_opener_block(o, pk, cfg))


def test_normalize_text_reports_issues():
    clean, issues = normalize_text("a​b а")
    assert clean == "ab a" and len(issues) == 2


def test_email_unicode(cfg):
    pk = PACKETS["exec_move"]
    body = "Hi Priya,\nJust tried you. Larkspur Insurance appointed a new Chief Digital Officer.\nA gаme-changing app.\nWho is the right person?\nAlex"
    assert {"BANNED_PHRASE", "UNICODE_SUSPECT"} <= errs(check_email_draft("tried you re Larkspur", body, pk, cfg))


# ============================================================================== send language everywhere (H2)

@pytest.mark.parametrize("phrase", ["I've sent you an email.", "I'll send over a calendar invite.", "Just sent you a note.",
                                    "I have sent the details.", "Sending you an invite now.", "See the attached.",
                                    "I'm sending you something.", "I emailed you earlier.", "A calendar invite is coming."])
def test_send_language_in_opener(cfg, phrase):
    vs = _op(cfg, f"Hi Priya, Alex from Pendo. {phrase} Who owns the app?")
    assert "EMAIL_SEND_LANGUAGE" in errs(vs)


def test_send_language_in_gatekeeper(cfg):
    vs = _op(cfg, "Hi Priya, Alex from Pendo. Who owns the app?", gk="Who looks after the app? I'll send over an invite.")
    assert "EMAIL_SEND_LANGUAGE" in errs(vs)


@pytest.mark.parametrize("phrase", ["I've sent you a calendar invite.", "I am sending you an invite.", "I'll send over an invite."])
def test_send_language_in_email(cfg, phrase):
    body = f"Hi Priya,\nJust tried you. Larkspur Insurance appointed a new Chief Digital Officer.\n{phrase}\nWho is the right person?\nAlex"
    assert "EMAIL_SEND_LANGUAGE" in errs(check_email_draft("tried you re Larkspur", body, PACKETS["exec_move"], cfg))


def test_no_send_language_false_positive(cfg):
    vs = _op(cfg, "Hi Priya, Alex from Pendo. Who sends the product updates at Larkspur?")
    assert "EMAIL_SEND_LANGUAGE" not in errs(vs)


# ============================================================================== one ask (H2)

@pytest.mark.parametrize("opener", [
    "Hi Priya, Alex from Pendo. Is now a bad time? Who owns the app?",
    "Hi Priya, Alex from Pendo. Who owns the app, and can I ask one more thing?",
    "Hi Priya, Alex from Pendo. Is it fine if I ask? Who owns the app?",
    "Hi Priya, Alex from Pendo. Are you free now, or should I call back?",
])
def test_opener_multiple_asks_error(cfg, opener):
    assert "OPENER_MULTIPLE_QUESTIONS" in errs(_op(cfg, opener))


@pytest.mark.parametrize("opener", [
    "Hi Priya, Alex from Pendo. Can I ask who owns the Larkspur app?",
    "Hi Priya, Alex from Pendo. Mind if I ask who owns the app?",
    "Hi Priya, Alex from Pendo. Larkspur has a new digital chief, right? Who owns the app?",
    "Hi Priya, Alex from Pendo. Who owns the customer app?",
])
def test_opener_single_ask_ok(cfg, opener):
    assert "OPENER_MULTIPLE_QUESTIONS" not in errs(_op(cfg, opener))


def test_email_would_you_or_should_i_is_two_asks(cfg):
    body = "Hi Priya,\nJust tried you. Larkspur Insurance appointed a new Chief Digital Officer.\nA new leader often reviews the app.\nWould you be open to a chat, or should I try your colleague?\nAlex"
    assert "EMAIL_MULTIPLE_ASKS" in errs(check_email_draft("tried you re Larkspur", body, PACKETS["exec_move"], cfg))


def test_email_question_plus_imperative_is_two_asks(cfg):
    body = "Hi Priya,\nJust tried you. Larkspur Insurance appointed a new Chief Digital Officer.\nWho owns the app?\nPlease reply with a name.\nAlex"
    assert "EMAIL_MULTIPLE_ASKS" in errs(check_email_draft("tried you re Larkspur", body, PACKETS["exec_move"], cfg))


def test_email_single_ask_ok(cfg):
    body = "Hi Priya,\nJust tried you. Larkspur Insurance appointed a new Chief Digital Officer.\nA new leader often reviews the app.\nWould you be open to a chat this week?\nAlex"
    assert not has_errors(check_email_draft("tried you re Larkspur", body, PACKETS["exec_move"], cfg))


def test_email_quotes_bullets_fake_thread(cfg):
    pk = PACKETS["exec_move"]
    good = "Hi Priya,\nJust tried you. Larkspur Insurance appointed a new Chief Digital Officer.\nA new leader often reviews the app.\nWho owns it?\nAlex"
    assert "SUBJECT_FAKE_THREAD" in errs(check_email_draft("Re: our chat", good, pk, cfg))
    assert "SUBJECT_FAKE_THREAD" in errs(check_email_draft("Fwd: Larkspur", good, pk, cfg))
    assert "EMAIL_BULLETS" in errs(check_email_draft("tried you today", good.replace("A new", "- A new"), pk, cfg))


def test_opener_line_wrapped_in_quotes(cfg):
    o = block(PACKETS["exec_move"], '"Hi Priya, Alex from Pendo. Who owns the app?"')
    assert "SHAPE_EXTRA" in errs(check_opener_block(o, PACKETS["exec_move"], cfg))


# ============================================================================== number words, rounds, titles, claims (H3/H4)

@pytest.mark.parametrize("phrase", ["about six hundred staff", "raised forty million", "a thirty-person team",
                                    "about two hundred and fifty people", "three weeks ago"])
def test_number_words_must_be_in_the_packet(cfg, phrase):
    assert "NUMBER_UNSUPPORTED" in errs(_op(cfg, f"Hi Priya, Alex from Pendo. Larkspur has {phrase}. Who owns the app?"))


def test_number_words_exempt_units(cfg):
    vs = _op(cfg, "Hi Priya, Alex from Pendo. Can I have two minutes? Who owns the app?")
    assert "NUMBER_UNSUPPORTED" not in errs(vs)
    assert "NUMBER_UNSUPPORTED" not in errs(_op(cfg, "Hi Priya, Alex from Pendo. Who owns the app, one question?"))


def test_number_word_matching_packet_value_is_fine(cfg):
    pk = copy.deepcopy(PACKETS["exec_move"])  # employees 620
    o = block(pk, "Hi Priya, Alex from Pendo. Larkspur has about six hundred and twenty staff. Who owns the app?")
    assert "NUMBER_UNSUPPORTED" not in errs(check_opener_block(o, pk, cfg))


def test_mantissa_of_money_words_matches_packet(cfg):
    pk = PACKETS["funding"]  # NZ$18 million
    o = block(pk, "Hi Jonas, Alex from Pendo. Bellbird raised eighteen million. Who owns product there?")
    assert "NUMBER_UNSUPPORTED" not in errs(check_opener_block(o, pk, cfg))


@pytest.mark.parametrize("claim", ["Series A", "Series C", "a seed round", "a bridge round", "the IPO"])
def test_round_letters_must_be_in_the_packet(cfg, claim):
    pk = PACKETS["funding"]
    assert "UNSUPPORTED_CLAIM" in errs(_op(cfg, f"Hi Jonas, Alex from Pendo. Bellbird raised {claim}. Who owns product?", key="funding"))


def test_series_b_is_fine_for_a_series_b_packet(cfg):
    assert not has_errors(_op(cfg, "Hi Jonas, Alex from Pendo. Bellbird raised a Series B. Who owns product?", key="funding",
                              gk="Who looks after the product team there, please?"))


def test_fact_line_series_letter_changed(cfg):
    pk = PACKETS["funding"]
    o = block(pk, "Hi Jonas, Alex from Pendo. Who owns product there?").replace("Series B round", "Series C round", 1)
    assert errs(check_opener_block(o, pk, cfg)) & {"FACT_UNSUPPORTED", "UNSUPPORTED_CLAIM"}


@pytest.mark.parametrize("flip", ["ended a partnership with Orbis Bank", "cancelled the partnership", "paused the Orbis Bank partnership"])
def test_meaning_flip_event_words(cfg, flip):
    assert "TRIGGER_MISMATCH" in errs(_op(cfg, f"Hi Wei, Alex from Pendo. Cinder {flip}. Who owns it?", key="one_fact",
                                           gk="Who runs engineering there, please?"))


def test_role_title_not_in_packet(cfg):
    assert "TRIGGER_MISMATCH" in errs(_op(cfg, "Hi Priya, Alex from Pendo. Larkspur appointed a new chief executive. Who owns the app?"))
    assert "PROPER_NOUN_UNSUPPORTED" in errs(_op(cfg, "Hi Priya, Alex from Pendo. Larkspur appointed a new CIO. Who owns the app?"))


def test_role_title_acronym_for_the_packet_title_is_fine(cfg):
    pk = copy.deepcopy(PACKETS["exec_move"])
    pk["trigger"]["evidence"] = "Larkspur Insurance appointed Dana Whitfield as Chief Technology Officer."
    pk["available_facts"][0]["text"] = "Larkspur Insurance appointed Dana Whitfield as Chief Technology Officer"
    o = block(pk, "Hi Priya, Alex from Pendo. Larkspur appointed a new CTO. Who owns the app?")
    assert not has_errors(check_opener_block(o, pk, cfg))


@pytest.mark.parametrize("claim", ["Pendo customers typically cut tickets in half.", "Teams using Pendo see better results.",
                                   "Other insurers use us.", "Customers like Zenith Bank rely on us.",
                                   "Bellbird is growing fast.", "The app is at the top of the store.",
                                   "Congratulations on your promotion."])
def test_claims_the_packet_cannot_support(cfg, claim):
    assert errs(_op(cfg, f"Hi Priya, Alex from Pendo. {claim} Who owns the app?")) & {"UNSUPPORTED_CLAIM"}


def test_contact_history_claims_never_in_opener(cfg):
    for phrase in ("We spoke last week.", "I left you a voicemail.", "Following up on our chat."):
        assert "HISTORY_CLAIM" in errs(_op(cfg, f"Hi Priya, Alex from Pendo. {phrase} Who owns the app?"))


def test_history_claim_in_email_needs_history_support(cfg):
    pk = copy.deepcopy(PACKETS["none_cold"])
    body = "Hi Ellie,\nJust tried you and left a voicemail.\nMarlow builds project software for builders.\nWho owns product?\nAlex"
    assert "HISTORY_CLAIM" in errs(check_email_draft("tried you today", body, pk, cfg))
    pk["contact_history"] = "last call 2026-11-20, outcome Left Voicemail"
    assert "HISTORY_CLAIM" not in errs(check_email_draft("tried you today", body, pk, cfg))


# ============================================================================== name rule (B1, B2)

def _pk_named(name, ev, first="Sam"):
    return {"account_name": name, "prospect_first_name": first, "prospect_title": "Head of Product",
            "trigger": {"kind": "funding", "evidence": ev + ".", "source": "zoominfo_scoop:FAKE-9", "date": "2026-11-03"},
            "available_facts": [{"text": ev, "source": "zoominfo_scoop:FAKE-9", "date": "2026-11-03"}]}


@pytest.mark.parametrize("name", ["Smith-Jones Partners", "Zip2Pay", "Co-op Finance", "3M Australia", "O'Brien & Co",
                                  "Coca-Cola Amatil", "Kmart", "B&B Hardware", "St. George Bank"])
def test_names_copied_verbatim_from_the_packet_never_fail(cfg, name):
    short = name.split()[0]
    pk = _pk_named(name, f"{name} raised A$5 million in a seed round")
    opener = f"Hi Sam, Alex from Pendo. {name} raised money. Does {short} have a product team?"
    o = block(pk, opener)
    assert not has_errors(check_opener_block(o, pk, cfg)), tc.format_violations(check_opener_block(o, pk, cfg))


def test_hyphenated_unknown_name_is_still_caught(cfg):
    assert "PROPER_NOUN_UNSUPPORTED" in errs(_op(cfg, "Hi Priya, Alex from Pendo. Not like Smith-Jones. Who owns the app?"))


def test_digits_in_first_name_are_fine(cfg):
    pk = copy.deepcopy(PACKETS["exec_move"])
    pk["prospect_first_name"] = "Pat1"
    assert not has_errors(check_opener_block(block(pk, "Hi Pat1, Alex from Pendo. Who owns the app?"), pk, cfg))


@pytest.mark.parametrize("starter", ["Honestly", "Apologies", "Maybe", "Perhaps", "Understand", "Morning", "Rolling",
                                     "Interested", "Straight", "Obviously", "Imagine", "Between"])
def test_many_ordinary_starters_pass(cfg, starter):
    assert "PROPER_NOUN_UNSUPPORTED" not in errs(_op(cfg, f"Hi Priya, Alex from Pendo. {starter}, who owns the customer app?"))


@pytest.mark.parametrize("acr", ["CX", "AI", "WA", "NSW", "QLD", "SaaS", "UX"])
def test_common_acronyms_pass(cfg, acr):
    assert "PROPER_NOUN_UNSUPPORTED" not in errs(_op(cfg, f"Hi Priya, Alex from Pendo. Who owns {acr} there?"))


def test_unknown_acronym_mid_sentence_is_caught(cfg):
    assert "PROPER_NOUN_UNSUPPORTED" in errs(_op(cfg, "Hi Priya, Alex from Pendo. Who owns the XYZ platform?"))


def test_month_abbreviations_and_ordinal_dates(cfg):
    vs = _op(cfg, "Hi Priya, Alex from Pendo. The appointment was on 12 Nov. Who owns the app?")
    assert not (errs(vs) & {"PROPER_NOUN_UNSUPPORTED", "NUMBER_UNSUPPORTED"})


def test_city_and_state_from_packet_fields_are_allowed(cfg):
    real = {"account_name": "Alpha Digital Pty Ltd", "city": "Sydney", "state": "New South Wales",
            "prospect_first_name": "Pat", "signals": [], "available_facts": [
                {"text": "Based in Sydney, New South Wales", "source": "Salesforce account record"}]}
    vs = check_opener_block(block(real, "Hi Pat, Alex from Pendo. Ringing a few Sydney teams. Who owns product?"), real, cfg)
    assert "PROPER_NOUN_UNSUPPORTED" not in errs(vs)


# ============================================================================== real 2c packets (B3, B4)

@pytest.fixture(scope="module")
def real_packets(request):
    from src.config import load_config
    config = load_config(ROOT / "config/config.yaml")
    accts, signals, prospects = world()
    rr = rank_accounts(accts, signals, prospects, {}, set(), config, NOW)
    sheet = cs.build_sheet(rr, accts, signals, prospects, config, NOW)
    return {ln.account.name: ln.packet.to_dict() for ln in sheet.lines}


def test_real_packets_are_built(real_packets):
    assert "Alpha Digital Pty Ltd" in real_packets and len(real_packets) >= 6


def test_real_packet_normalises_without_glue(real_packets):
    n = tc.normalize_packet(real_packets["Alpha Digital Pty Ltd"])
    assert n.account_name == "Alpha Digital Pty Ltd" and n.trigger is not None and n.trigger_kind == "exec_move"
    assert n.first_name == "Pat1" and 3 in n.ages
    assert len(n.facts) == 3 and not n.ambiguous


def test_real_packet_good_block_label_citation_on_both_lines(cfg, real_packets):
    pk = real_packets["Alpha Digital Pty Ltd"]
    lab = "ZoomInfo scoop 2026-11-22"
    t = "Alpha Digital Pty Ltd has a new digital product lead"
    o = "\n".join([f"OPENER: Hi Pat1, Alex from Pendo. Alpha Digital has a new digital product lead. Who owns the app?",
                   f"FACT 1: {t} (source: {lab})", "FACT 2: About 400 employees (source: Salesforce account record (ZoomInfo employee count))",
                   "GATEKEEPER: Who looks after product there, please?", f"TRIGGER: {t} (source: {lab})"])
    vs = check_opener_block(o, pk, cfg)
    assert not has_errors(vs), tc.format_violations(vs)


def test_real_packet_trigger_may_cite_the_id_and_date_and_fact_the_label(cfg, real_packets):
    pk = real_packets["Alpha Digital Pty Ltd"]
    t = "Alpha Digital Pty Ltd has a new digital product lead"
    o = "\n".join(["OPENER: Hi Pat1, Alex from Pendo. Who owns the app?",
                   f"FACT 1: {t} (source: ZoomInfo scoop 2026-11-22)", "FACT 2: Based in Sydney, New South Wales (source: Salesforce account record)",
                   "GATEKEEPER: Who looks after product there, please?",
                   f"TRIGGER: {t} (source: zoominfo_scoop:FAKE-S-1, 2026-11-22)"])
    assert not has_errors(check_opener_block(o, pk, cfg))


def test_real_packet_id_on_both_lines_is_accepted(cfg, real_packets):
    pk = real_packets["Alpha Digital Pty Ltd"]
    t = "Alpha Digital Pty Ltd has a new digital product lead"
    o = "\n".join(["OPENER: Hi Pat1, Alex from Pendo. Who owns the app?",
                   f"FACT 1: {t} (source: zoominfo_scoop:FAKE-S-1, 2026-11-22)", "FACT 2: About 400 employees (source: Salesforce account record (ZoomInfo employee count))",
                   "GATEKEEPER: Who looks after product there, please?",
                   f"TRIGGER: {t} (source: zoominfo_scoop:FAKE-S-1, 2026-11-22)"])
    assert not has_errors(check_opener_block(o, pk, cfg))


def test_real_packet_invented_date_on_a_dateless_fact_is_rejected(cfg, real_packets):
    pk = real_packets["Alpha Digital Pty Ltd"]
    o = "\n".join(["OPENER: Hi Pat1, Alex from Pendo. Who owns the app?",
                   "FACT 1: About 400 employees (source: Salesforce account record (ZoomInfo employee count), 2019-01-01)",
                   "FACT 2: none verified", "GATEKEEPER: Who looks after product there, please?",
                   "TRIGGER: Alpha Digital Pty Ltd has a new digital product lead (source: zoominfo_scoop:FAKE-S-1, 2026-11-22)"])
    assert "FACT_DATE_INVENTED" in errs(check_opener_block(o, pk, cfg))


def test_real_packet_wrong_trigger_date_rejected(cfg, real_packets):
    pk = real_packets["Alpha Digital Pty Ltd"]
    o = "\n".join(["OPENER: Hi Pat1, Alex from Pendo. Who owns the app?", "FACT 1: none verified", "FACT 2: none verified",
                   "GATEKEEPER: Who looks after product there, please?",
                   "TRIGGER: Alpha Digital Pty Ltd has a new digital product lead (source: zoominfo_scoop:FAKE-S-1, 2026-11-01)"])
    assert errs(check_opener_block(o, pk, cfg)) & {"TRIGGER_DATE_INVENTED", "TRIGGER_DATE_MISSING"}


def test_real_packet_relative_date_uses_age_days(cfg, real_packets):
    pk = real_packets["Alpha Digital Pty Ltd"]   # age_days 3
    ok = check_opener_block(block(_as_real(pk), "Hi Pat1, Alex from Pendo. The new lead started 3 days ago. Who owns the app?"), pk, cfg)
    assert "NUMBER_UNSUPPORTED" not in errs(ok)
    bad = check_opener_block(block(_as_real(pk), "Hi Pat1, Alex from Pendo. The new lead started 9 days ago. Who owns the app?"), pk, cfg)
    assert "NUMBER_UNSUPPORTED" in errs(bad)


def _as_real(pk):
    """block() helper wants status/ids on signals; real packets already have them. Facts have no date."""
    return pk


def test_real_packet_stale_signal_is_not_a_trigger(cfg, real_packets):
    pk = real_packets["Hotel Travel Pty Ltd"]
    assert pk["has_real_trigger"] is False
    n = tc.normalize_packet(pk)
    assert n.trigger is None
    o = "\n".join(["OPENER: Hi Pat8, Alex from Pendo. Cold call, I'll be upfront. Who owns product?",
                   "FACT 1: About 600 employees (source: Salesforce account record (ZoomInfo employee count))",
                   "FACT 2: Based in New South Wales (source: Salesforce account record)",
                   "GATEKEEPER: Who looks after product there, please?", "TRIGGER: None found: cold call on ICP fit only"])
    assert not has_errors(check_opener_block(o, pk, cfg))
    claimed = o.replace("TRIGGER: None found: cold call on ICP fit only", "TRIGGER: Hotel Travel formed a partnership (source: zoominfo_scoop:FAKE-S-8)")
    assert "TRIGGER_LINE_CLAIMED" in errs(check_opener_block(claimed, pk, cfg))


def test_real_packet_no_trigger_account(cfg, real_packets):
    pk = real_packets["Echo Logistics Pty Ltd"]
    o = block(pk, "Hi Pat5, Alex from Pendo. Cold call, I'll be upfront. Who owns product?", f2="none verified")
    assert not has_errors(check_opener_block(o, pk, cfg))
    bad = block(pk, "Hi Pat5, Alex from Pendo. Saw the news. Who owns product?", f2="none verified")
    assert "INVENTED_TRIGGER" in errs(check_opener_block(bad, pk, cfg))


def test_real_packet_ambiguity_flag_turns_on_the_role_rule(cfg, real_packets):
    pk = copy.deepcopy(real_packets["Alpha Digital Pty Ltd"])
    o = "Hi Pat1, Alex from Pendo. As Head of Product you must run the app. Who owns it?"
    assert "ROLE_ASSUMED" not in errs(check_opener_block(block(pk, o), pk, cfg))
    pk["ambiguity_flags"] = ["role ambiguous: check it is a digital product role"]
    assert "ROLE_ASSUMED" in errs(check_opener_block(block(pk, o), pk, cfg))


def test_real_packet_signal_ambiguous_true_alone_turns_it_on(cfg, real_packets):
    pk = copy.deepcopy(real_packets["Alpha Digital Pty Ltd"])
    pk["signals"][0]["ambiguous"] = True
    assert tc.normalize_packet(pk).ambiguous
    assert "ROLE_ASSUMED" in errs(check_opener_block(block(pk, "Hi Pat1, Alex from Pendo. Your team must be busy. Who owns it?"), pk, cfg))


def test_legacy_flags_key_still_works(cfg):
    pk = copy.deepcopy(PACKETS["ambiguous_role"])
    pk["ambiguity_flags"] = pk.pop("flags")
    assert tc.normalize_packet(pk).ambiguous


def test_intent_only_packet_is_not_a_trigger(cfg):
    pk = {"account_name": "Alpha Digital Pty Ltd", "prospect_first_name": "Pat", "has_real_trigger": False,
          "signals": [{"kind": "intent", "status": "intent", "evidence": "researching customer experience",
                       "source": "zoominfo_intent:FAKE-I-1", "source_label": "ZoomInfo intent signal", "date": None}],
          "available_facts": [{"text": "Intent signal (interest, not a dated event): researching customer experience",
                               "source": "ZoomInfo intent signal"}]}
    n = tc.normalize_packet(pk)
    assert n.trigger is None
    o = block(pk, "Hi Pat, Alex from Pendo. Saw you are researching this. Who owns it?")
    assert "INVENTED_TRIGGER" in errs(check_opener_block(o, pk, cfg))
    ok = "\n".join(["OPENER: Hi Pat, Alex from Pendo. Cold call, I'll be upfront. Who owns the customer experience side?",
                    "FACT 1: Intent signal (interest, not a dated event): researching customer experience (source: ZoomInfo intent signal)",
                    "FACT 2: none verified", "GATEKEEPER: Who looks after customer experience there, please?",
                    "TRIGGER: None found: cold call on ICP fit only"])
    assert not has_errors(check_opener_block(ok, pk, cfg))
    assert "TRIGGER_LINE_CLAIMED" in errs(check_opener_block(ok.replace("None found: cold call on ICP fit only", "Researching customer experience (source: ZoomInfo intent signal)"), pk, cfg))


def test_no_first_name_packet(cfg):
    pk = copy.deepcopy(PACKETS["none_cold"])
    pk["prospect_first_name"] = None
    o = block(pk, "Hi there, Alex from Pendo. Cold call, I'll be upfront. Who owns product?")
    assert not has_errors(check_opener_block(o, pk, cfg))
    assert "PROPER_NOUN_UNSUPPORTED" in errs(check_opener_block(block(pk, "Hi Ellie, Alex from Pendo. Who owns product?"), pk, cfg))


def test_contact_history_must_not_appear_in_the_opener(cfg):
    pk = copy.deepcopy(PACKETS["none_cold"])
    pk["contact_history"] = "last call 2026-11-20, outcome Left Voicemail"
    o = block(pk, "Hi Ellie, Alex from Pendo. I left you a voicemail on 20 November. Who owns product?")
    assert "HISTORY_CLAIM" in errs(check_opener_block(o, pk, cfg))


def test_must_not_claim_instruction_sentences_are_dead_text(cfg, real_packets):
    pk = real_packets["Alpha Digital Pty Ltd"]
    assert tc.normalize_packet(pk).must_not_phrases == []
    pk2 = dict(pk, must_not_claim=["Do not say 'a case study' to anyone.", "existing customer"], must_not_phrases=["roadmap review"])
    assert set(tc.normalize_packet(pk2).must_not_phrases) == {"a case study", "existing customer", "roadmap review"}


def test_must_not_phrases_key_is_matched(cfg):
    pk = copy.deepcopy(PACKETS["exec_move"])
    pk["must_not_phrases"] = ["roadmap review"]
    assert "MUST_NOT_CLAIM" not in errs(_op(cfg, "Hi Priya, Alex from Pendo. Who owns the roadmap review?"))  # not in this packet
    assert "MUST_NOT_CLAIM" in errs(check_opener_block(block(pk, "Hi Priya, Alex from Pendo. Who owns the roadmap review?"), pk, cfg))


# ============================================================================== labelled corpus gate

def _lint(c, cfg):
    pk = PACKETS[c["packet"]]
    return (check_opener_block(c["text"], pk, cfg) if c["kind"] == "opener"
            else check_email_draft(c["subject"], c["body"], pk, cfg))


def _measure(cases, cfg):
    bad = [c for c in cases if c["expect"] == "fail"]
    good = [c for c in cases if c["expect"] == "pass"]
    caught = sum(1 for c in bad if errs(_lint(c, cfg)))
    intended = sum(1 for c in bad if errs(_lint(c, cfg)) & set(c["intended_code"].split("|")))
    fp = [c["id"] for c in good if errs(_lint(c, cfg))]
    return len(bad), caught, intended, len(good), fp


def test_review_corpus_recall_and_false_positives(real_cfg):
    cases = LABELS["opener_cases"] + LABELS["email_cases"]
    nbad, caught, intended, ngood, fp = _measure(cases, real_cfg)
    assert nbad == 161 and ngood == 32
    assert caught / nbad >= 0.95, f"recall {caught}/{nbad}"
    assert intended / nbad >= 0.90, f"intended-reason recall {intended}/{nbad}"
    assert len(fp) / ngood <= 0.12, f"false positives on good blocks: {fp}"


def test_review_corpus_natural_openers_false_positives(real_cfg):
    _, _, _, n, fp = _measure(LABELS["natural_openers"], real_cfg)
    assert n == 84 and len(fp) / n <= 0.12, f"false positives on natural openers: {fp}"


# ============================================================================== fresh cases (not used for tuning)

FRESH_BAD = [
    ("exec_move", "Hi Priya, Alex from Pendo. Larkspur just raised money. Who owns the customer app?", {"TRIGGER_MISMATCH"}),
    ("exec_move", "Hi Priya, Alex from Pendo. Larkspur serves 90,000 policyholders. Who owns the app?", {"NUMBER_UNSUPPORTED"}),
    ("exec_move", "Hi Priya, Alex from Pendo. Larkspur is in Brisbane, correct? Who owns the app?", {"PROPER_NOUN_UNSUPPORTED"}),
    ("exec_move", "Hi Priya, Alex from Pendo. We make apps stickier. Who owns it?", {"UNSUPPORTED_CLAIM"}),
    ("exec_move", "Hi Priya, Alex from Pendo. Unlocking your app data could be huge. Who owns it?", {"BANNED_PHRASE"}),
    ("exec_move", "Hi Priya, Alex from Pendo. I hope this finds you busy. Who owns the app?", {"BANNED_PHRASE"}),
    ("exec_move", "Hi Priya, Alex from Pendo. Just checking in on the app. Who owns it?", {"BANNED_PHRASE"}),
    ("exec_move", "Hi Priya, Alex from Pendo. Revolutionising how insurers learn from apps. Who owns it?", {"BANNED_PHRASE"}),
    ("exec_move", "Hi Priya, Alex from Pendo. Leveraging data, who owns the app?", {"BANNED_PHRASE"}),
    ("exec_move", "Hi Priya, Alex from Pendo. Let's circle back on the app. Who owns it?", {"BANNED_PHRASE"}),
    ("exec_move", "Hi Priya, Alex from Pendo. Our in-app guides help insurers. Who owns the app?", {"OPENER_PENDO_FEATURE"}),
    ("exec_move", "Hi Priya, Alex from Pendo. Session replay shows where claims stall. Who owns it?", {"OPENER_PENDO_FEATURE"}),
    ("exec_move", "Hi Priya, Alex from Pendo. Who owns the app? Reply to priya@larkspur.example.com.", {"EMAIL_ADDRESS"}),
    ("exec_move", "Hi Priya, Alex from Pendo. Call 0412 345 678. Who owns the app?", {"PHONE_NUMBER"}),
    ("exec_move", "Hi Priya, Alex from Pendo. Details at pendo.io/larkspur. Who owns the app?", {"LINK"}),
    ("exec_move", "Hi Priya, Alex from Pendo. Who owns the app? \U0001F680", {"EMOJI"}),
    ("exec_move", "Hi Priya, Alex from Pendo! Who owns the app?", {"EXCLAMATION"}),
    ("exec_move", "Hi Priya, Alex from Pendo. I've booked a calendar invite for Friday. Who owns the app?", {"EMAIL_SEND_LANGUAGE"}),
    ("exec_move", "Hi Priya, Alex from Pendo. Who owns the app, and may I send you a deck?", {"OPENER_MULTIPLE_QUESTIONS", "EMAIL_SEND_LANGUAGE"}),
    ("funding", "Hi Jonas, Alex from Pendo. Bellbird closed a Series A. Who owns product?", {"UNSUPPORTED_CLAIM", "TRIGGER_MISMATCH"}),
    ("funding", "Hi Jonas, Alex from Pendo. Bellbird raised NZ$25 million. Who owns product?", {"NUMBER_UNSUPPORTED"}),
    ("funding", "Hi Jonas, Alex from Pendo. Bellbird raised eighteen billion. Who owns product?", {"NUMBER_UNSUPPORTED"}),
    ("funding", "Hi Jonas, Alex from Pendo. Bellbird is hiring five engineers. Who owns product?", {"NUMBER_UNSUPPORTED"}),
    ("funding", "Hi Jonas, Alex from Pendo. Congrats on your promotion. Who owns product?", {"UNSUPPORTED_CLAIM"}),
    ("funding", "Hi Jonas, Alex from Pendo. Bellbird just went public. Who owns product?", {"TRIGGER_MISMATCH", "UNSUPPORTED_CLAIM"}),
    ("product_launch", "Hi Marcus, Alex from Pendo. Tidewater shut its old app down. Who owns the new one?", {"TRIGGER_MISMATCH"}),
    ("product_launch", "Hi Marcus, Alex from Pendo. Saw the launch cost A$4 million. Who owns it?", {"NUMBER_UNSUPPORTED"}),
    ("product_launch", "Hi Marcus, Alex from Pendo. Tidewater Credit Union Pty Ltd launched it. Who owns it?", {"PROPER_NOUN_UNSUPPORTED"}),
    ("none_cold", "Hi Ellie, Alex from Pendo. Congrats on the new office. Who owns product?", {"INVENTED_TRIGGER"}),
    ("none_cold", "Hi Ellie, Alex from Pendo. Noticed you are hiring. Who owns product?", {"INVENTED_TRIGGER"}),
    ("none_cold", "Hi Ellie, Alex from Pendo. Marlow acquired a rival. Who owns product?", {"INVENTED_TRIGGER"}),
    ("none_cold", "Hi Ellie, Alex from Pendo. Big week at Marlow? Who owns product?", {"INVENTED_TRIGGER"}),
    ("none_cold", "Hi Ellie, Alex from Pendo. We spoke at a conference. Who owns product?", {"HISTORY_CLAIM"}),
    ("ambiguous_role", "Hi Tane, Alex from Pendo. As Product Manager you must care. Who owns it?", {"ROLE_ASSUMED"}),
    ("ambiguous_role", "Hi Tane, Alex from Pendo. You run the portal, right? Who owns it?", {"ROLE_ASSUMED"}),
    ("one_fact", "Hi Wei, Alex from Pendo. Cinder and Orbis Bank merged. Who owns it?", {"TRIGGER_MISMATCH"}),
]


@pytest.mark.parametrize("key,opener,want", FRESH_BAD, ids=[f"bad{i:02d}" for i in range(len(FRESH_BAD))])
def test_fresh_bad_openers_are_caught(cfg, key, opener, want):
    gk = {"funding": "Who looks after the product team there, please?", "one_fact": "Who runs engineering there, please?"}.get(key)
    vs = check_opener_block(block(PACKETS[key], opener, **({"gk": gk} if gk else {})), PACKETS[key], cfg)
    assert errs(vs) & want, f"{opener!r}: wanted one of {want}, got {errs(vs)}"


FRESH_GOOD = [
    ("exec_move", "Hi Priya, Alex from Pendo. Larkspur has a new digital chief, so who owns the claims app?"),
    ("exec_move", "Hi Priya, Alex from Pendo. Quick intro: I ring product teams. Who owns the customer app?"),
    ("exec_move", "Hi Priya, Alex from Pendo. Not sure this is your area. Who looks after the claims app?"),
    ("exec_move", "Hi Priya, Alex from Pendo. Is the claims app still run in house?"),
    ("exec_move", "Hi Priya, Alex from Pendo. Dana Whitfield joined as Chief Digital Officer. Who owns the app?"),
    ("exec_move", "Hi Priya, Alex from Pendo. Larkspur's new Chief Digital Officer. Does the app sit under them?"),
    ("exec_move", "Hi Priya, Alex from Pendo. Taking thirty seconds of your time. Who owns the customer app?"),
    ("exec_move", "Hi Priya, Alex from Pendo. Straight out of the blue, sorry. Who owns the app?"),
    ("exec_move", "Hi Priya, Alex from Pendo. Who handles the Larkspur customer app now?"),
    ("funding", "Hi Jonas, Alex from Pendo. Bellbird is hiring a Product Designer. Who owns the product side?"),
    ("funding", "Hi Jonas, Alex from Pendo. Congrats on the Series B. Who owns product there?"),
    ("funding", "Hi Jonas, Alex from Pendo. Series B for Bellbird, nice one. Who owns product?"),
    ("funding", "Hi Jonas, Alex from Pendo. Bellbird raised funding in November. Who owns product?"),
    ("product_launch", "Hi Marcus, Alex from Pendo. How is the new Tidewater member app going?"),
    ("product_launch", "Hi Marcus, Alex from Pendo. Tidewater launched a mobile app. Who looks after it?"),
    ("product_launch", "Hi Marcus, Alex from Pendo. About 310 staff and a new app. Who owns it?"),
    ("none_cold", "Hi Ellie, Alex from Pendo. Cold call, I'll be upfront. Does Marlow have a product owner?"),
    ("none_cold", "Hi Ellie, Alex from Pendo. Ringing software teams in Perth today. Who owns product at Marlow?"),
    ("none_cold", "Hi Ellie, Alex from Pendo. Marlow makes project software for builders. Who owns the product side?"),
    ("ambiguous_role", "Hi Tane, Alex from Pendo. Greenvale is hiring a Head of Digital. Who handles digital there?"),
    ("one_fact", "Hi Wei, Alex from Pendo. Cinder and Orbis Bank have a partnership. Who runs the engineering side?"),
]


@pytest.mark.parametrize("key,opener", FRESH_GOOD, ids=[f"good{i:02d}" for i in range(len(FRESH_GOOD))])
def test_fresh_good_openers_pass(cfg, key, opener):
    gk = {"funding": "Who looks after the product team there, please?", "one_fact": "Who runs engineering there, please?"}.get(key)
    vs = check_opener_block(block(PACKETS[key], opener, **({"gk": gk} if gk else {})), PACKETS[key], cfg)
    assert not has_errors(vs), f"{opener!r}: {tc.format_violations(vs)}"


FRESH_GATEKEEPER_GOOD = [
    "Could you put me through to whoever looks after the app?", "Who would I speak to about the customer app?",
    "Is the head of digital available, please?", "Who looks after your product team?",
    "Could I get the name of your product owner, please?", "Who is the right person for the app?",
]


@pytest.mark.parametrize("gk", FRESH_GATEKEEPER_GOOD)
def test_fresh_good_gatekeeper_lines_pass(cfg, gk):
    assert not has_errors(_op(cfg, "Hi Priya, Alex from Pendo. Who owns the app?", gk=gk))


# ============================================================================== CLI

def _run_cli(tmp_path, kind, packet, text, *extra):
    pp, tp = tmp_path / "p.json", tmp_path / "t.txt"
    pp.write_text(json.dumps(packet))
    tp.write_text(text)
    return subprocess.run([sys.executable, "-m", "src.textcheck", kind, "--packet", str(pp), "--text", str(tp), *extra],
                          cwd=ROOT, capture_output=True, text=True)


def test_cli_opener_ok_exit_0(tmp_path, real_cfg):
    pk = PACKETS["exec_move"]
    r = _run_cli(tmp_path, "opener", pk, block(pk, "Hi Priya, Alex from Pendo. Who owns the customer app?", label=True))
    assert r.returncode == 0 and r.stdout.startswith("OK")


def test_cli_opener_errors_exit_1(tmp_path):
    pk = PACKETS["exec_move"]
    r = _run_cli(tmp_path, "opener", pk, block(pk, "Hi Priya, Alex from Pendo. Hope you're well. Who owns the app?", label=True))
    assert r.returncode == 1 and "BANNED_PHRASE" in r.stdout


def test_cli_missing_label_is_an_error(tmp_path):
    pk = PACKETS["exec_move"]
    r = _run_cli(tmp_path, "opener", pk, block(pk, "Hi Priya, Alex from Pendo. Who owns the app?"))
    assert r.returncode == 1 and "DRAFT_STYLE_LABEL_MISSING" in r.stdout


def test_cli_email(tmp_path):
    pk = PACKETS["exec_move"]
    text = (LABEL + "\nSUBJECT: tried you re Larkspur\nBODY:\nHi Priya,\nJust tried you. Larkspur Insurance appointed a new "
            "Chief Digital Officer.\nA new leader often reviews the app.\nWho owns it?\nAlex\n")
    assert _run_cli(tmp_path, "email", pk, text).returncode == 0
    assert _run_cli(tmp_path, "email", pk, text.replace("Who owns it?", "Who owns it? Let me know.")).returncode == 1


def test_cli_bad_voice_file_exit_2(tmp_path):
    pk = PACKETS["exec_move"]
    bad = tmp_path / "v.yaml"
    bad.write_text("calibrated: 'false'\n")
    r = _run_cli(tmp_path, "opener", pk, block(pk, "Hi Priya, Alex from Pendo. Who owns the app?"), "--voice", str(bad))
    assert r.returncode == 2 and "cannot run the lint" in r.stderr


def test_cli_missing_file_exit_2(tmp_path):
    r = subprocess.run([sys.executable, "-m", "src.textcheck", "opener", "--packet", str(tmp_path / "no.json"),
                        "--text", str(tmp_path / "no.txt")], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 2


def test_cli_main_can_be_called_in_process(tmp_path, capsys):
    pk = PACKETS["exec_move"]
    (tmp_path / "p.json").write_text(json.dumps(pk))
    (tmp_path / "t.txt").write_text(block(pk, "Hi Priya, Alex from Pendo. Who owns the app?", label=True))
    assert tc.main(["opener", "--packet", str(tmp_path / "p.json"), "--text", str(tmp_path / "t.txt"),
                    "--voice", str(ROOT / "config/voice.yaml")]) == 0
    assert "OK" in capsys.readouterr().out


# ============================================================================== purity

def test_only_loader_and_cli_touch_files():
    import ast
    tree = ast.parse((ROOT / "src/textcheck.py").read_text(encoding="utf-8"))
    opens = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "open"]
    assert len(opens) == 1
    imports = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not ({"os", "socket", "subprocess", "requests", "urllib", "random", "datetime", "time"} & imports)


def test_checks_do_not_mutate_inputs(cfg):
    pk = copy.deepcopy(PACKETS["exec_move"])
    pk0, c0 = copy.deepcopy(pk), copy.deepcopy(cfg)
    check_opener_block(block(pk, "Hi Priya, Alex from Pendo. Who owns the app?"), pk, cfg)
    assert pk == pk0 and cfg == c0


# ============================================================================== fresh batches 2 and 3
# Written after batch 1 and run BEFORE any rule was changed for them (see docs/voice_guide.md for the numbers).
# They are regression tests now: later rule changes were made after seeing their first run, so they are no longer
# an unbiased estimate.

GK = {"funding": "Who looks after the product team there, please?", "one_fact": "Who runs engineering there, please?"}

FRESH_BAD_2 = [
    ("exec_move", "Hi Priya, Alex from Pendo. Larkspur has a new Chief Product Officer. Who owns the app?"),
    ("exec_move", "Hi Priya, Alex from Pendo. Larkspur's claims app won an award. Who owns it?"),
    ("exec_move", "Hi Priya, Alex from Pendo. Larkspur announced a big migration. Who owns the app?"),
    ("exec_move", "Hi Priya, Alex from Pendo. Ninety percent of insurers use apps like yours. Who owns it?"),
    ("exec_move", "Hi Priya, Alex from Pendo. Pendo helps Zenith Insurance with claims. Who owns the app?"),
    ("exec_move", "Hi Priya, Alex from Pendo. Dana Whitfield said the app is a priority. Who owns it?"),
    ("exec_move", "Hi Priya, Alex from Pendo. We improve retention for insurers. Who owns the app?"),
    ("exec_move", "Hi Priya, Alex from Pendo. Can you grab me five minutes? Who owns the app?"),
    ("exec_move", "Hi Priya, Alex from Pendo. Larkspur was founded in 1998. Who owns the app?"),
    ("exec_move", "Hi Priya, Alex from Pendo. Hoping you are well. Who owns the app?"),
    ("exec_move", "Hi Priya, Alex from Pendo. Wanted to reach out re the app. Who owns it?"),
    ("exec_move", "Hi Priya, Alex from Pendo. Heatmaps and funnels for your app. Who owns it?"),
    ("funding", "Hi Jonas, Alex from Pendo. Bellbird raised A$18 million. Who owns product?"),
    ("funding", "Hi Jonas, Alex from Pendo. Bellbird raised NZ$18 billion. Who owns product?"),
    ("funding", "Hi Jonas, Alex from Pendo. Bellbird is hiring a Head of Growth. Who owns product?"),
    ("funding", "Hi Jonas, Alex from Pendo. Bellbird raised a Series B led by Sequoia. Who owns product?"),
    ("funding", "Hi Jonas, Alex from Pendo. Bellbird opened a Sydney office. Who owns product?"),
    ("product_launch", "Hi Marcus, Alex from Pendo. Tidewater launched an Android app too. Who owns it?"),
    ("product_launch", "Hi Marcus, Alex from Pendo. The Tidewater app has 50k downloads. Who owns it?"),
    ("product_launch", "Hi Marcus, Alex from Pendo. Tidewater delayed its app launch. Who owns it?"),
    ("none_cold", "Hi Ellie, Alex from Pendo. Marlow just won a big contract. Who owns product?"),
    ("none_cold", "Hi Ellie, Alex from Pendo. Heard Marlow is growing. Who owns product?"),
    ("none_cold", "Hi Ellie, Alex from Pendo. Marlow has around 200 staff. Who owns product?"),
    ("ambiguous_role", "Hi Tane, Alex from Pendo. You look after the portal, so who owns it?"),
    ("ambiguous_role", "Hi Tane, Alex from Pendo. Greenvale is hiring a Head of Marketing. Who handles it?"),
    ("one_fact", "Hi Wei, Alex from Pendo. Cinder signed with Orbis Bank in 2024. Who owns it?"),
    ("one_fact", "Hi Wei, Alex from Pendo. Cinder and Orbis ended their partnership. Who owns it?"),
]
FRESH_BAD_3 = [
    ("exec_move", "Hi Priya, Alex from Pendo. Larkspur has hired a new Chief Digital Officer from Zenith. Who owns the app?"),
    ("exec_move", "Hi Priya, Alex from Pendo. Larkspur is rebuilding its claims app, I hear. Who owns it?"),
    ("exec_move", "Hi Priya, Alex from Pendo. Dana Whitfield tweeted about the app. Who owns it?"),
    ("exec_move", "Hi Priya, Alex from Pendo. Larkspur's app has poor reviews. Who owns it?"),
    ("exec_move", "Hi Priya, Alex from Pendo. We cut claims calls by a third. Who owns the app?"),
    ("exec_move", "Hi Priya, Alex from Pendo. I'd love to pick your brain. Who owns the app?"),
    ("exec_move", "Hi Priya, Alex from Pendo. Who owns the app? I'm a Pendo AE at 0400 111 222."),
    ("funding", "Hi Jonas, Alex from Pendo. Bellbird took a Series B from investors in Singapore. Who owns product?"),
    ("funding", "Hi Jonas, Alex from Pendo. Bellbird raised money last month. Who owns product?"),
    ("funding", "Hi Jonas, Alex from Pendo. Bellbird has two hundred members. Who owns product?"),
    ("product_launch", "Hi Marcus, Alex from Pendo. Tidewater replaced its app. Who owns it?"),
    ("product_launch", "Hi Marcus, Alex from Pendo. The Tidewater app crashes a lot. Who owns it?"),
    ("none_cold", "Hi Ellie, Alex from Pendo. Marlow's CTO left. Who owns product?"),
    ("none_cold", "Hi Ellie, Alex from Pendo. Marlow is hiring in Perth. Who owns product?"),
    ("one_fact", "Hi Wei, Alex from Pendo. Cinder Payments is expanding into Asia. Who owns it?"),
]
FRESH_GOOD_2 = [
    ("exec_move", "Hi Priya, Alex from Pendo. Fair warning, this is a cold-ish call. Who owns the claims app?"),
    ("exec_move", "Hi Priya, Alex from Pendo. Is the Larkspur claims app the right thing to ask about?"),
    ("exec_move", "Hi Priya, Alex from Pendo. Dana Whitfield took the Chief Digital Officer job. Who owns the app?"),
    ("funding", "Hi Jonas, Alex from Pendo. A NZ$18 million raise is big. Who owns product?"),
    ("funding", "Hi Jonas, Alex from Pendo. Bellbird raised in a Series B round. Who owns product?"),
    ("product_launch", "Hi Marcus, Alex from Pendo. Tidewater's launch is recent. How is the new app going?"),
    ("product_launch", "Hi Marcus, Alex from Pendo. Saw Tidewater's new mobile banking app. Who looks after it?"),
    ("none_cold", "Hi Ellie, Alex from Pendo. Straight up, no news hook. Who owns product?"),
    ("none_cold", "Hi Ellie, Alex from Pendo. Marlow is in Perth, right? Who owns product?"),
    ("none_cold", "Hi Ellie, Alex from Pendo. Software for builders is a good space. Who owns product?"),
    ("ambiguous_role", "Hi Tane, Alex from Pendo. Greenvale is hiring a Head of Digital. Who is across that?"),
    ("one_fact", "Hi Wei, Alex from Pendo. Orbis Bank and Cinder Payments partnered up. Who owns engineering?"),
    ("one_fact", "Hi Wei, Alex from Pendo. Cinder announced a partnership with Orbis Bank. Is that on your radar?"),
]
FRESH_GOOD_3 = [
    ("exec_move", "Hi Priya, Alex from Pendo. Larkspur has a new Chief Digital Officer. Does the app move under them?"),
    ("exec_move", "Hi Priya, Alex from Pendo. Not a sales pitch, just a question. Who owns the claims app?"),
    ("exec_move", "Hi Priya, Alex from Pendo. Sorry to interrupt. Who looks after the customer app?"),
    ("funding", "Hi Jonas, Alex from Pendo. Bellbird has a new Series B and a Product Designer role open. Who owns product?"),
    ("funding", "Hi Jonas, Alex from Pendo. Series B money for Bellbird. Is product planning busy?"),
    ("product_launch", "Hi Marcus, Alex from Pendo. Tidewater launched the app on 18 November. How is it going?"),
    ("none_cold", "Hi Ellie, Alex from Pendo. I ring software teams in Perth. Who owns product at Marlow?"),
    ("none_cold", "Hi Ellie, Alex from Pendo. Marlow has about 85 people. Who owns product?"),
    ("ambiguous_role", "Hi Tane, Alex from Pendo. Greenvale hiring a Head of Digital caught my eye. Who owns that area?"),
    ("one_fact", "Hi Wei, Alex from Pendo. Cinder's partnership with Orbis Bank is recent. Who owns the engineering side?"),
]


def _fresh(key, opener, cfg):
    return check_opener_block(block(PACKETS[key], opener, **({"gk": GK[key]} if key in GK else {})), PACKETS[key], cfg)


@pytest.mark.parametrize("key,opener", FRESH_BAD_2 + FRESH_BAD_3)
def test_fresh_bad_batches_2_and_3_are_caught(cfg, key, opener):
    assert errs(_fresh(key, opener, cfg)), opener


@pytest.mark.parametrize("key,opener", FRESH_GOOD_2 + FRESH_GOOD_3)
def test_fresh_good_batches_2_and_3_pass(cfg, key, opener):
    vs = _fresh(key, opener, cfg)
    assert not has_errors(vs), f"{opener!r}: {tc.format_violations(vs)}"


def test_two_short_questions_are_two_asks_even_when_the_first_is_rhetorical(cfg):
    """Strict by design (the call is 8 seconds): 'New CDO at Larkspur? Who owns the app?' is two questions."""
    assert "OPENER_MULTIPLE_QUESTIONS" in errs(_op(cfg, "Hi Priya, Alex from Pendo. New CDO at Larkspur? Who owns the app?"))


# ============================================================================== relative time, currency, magnitude

def test_relative_time_checked_against_age_days(cfg):
    pk = copy.deepcopy(PACKETS["funding"])           # age_days 14 on the trigger
    ok = _fresh_pk(pk, "Hi Jonas, Alex from Pendo. Bellbird raised money last week. Who owns product?", cfg)
    assert "UNSUPPORTED_CLAIM" not in errs(ok)
    for phrase in ("yesterday", "last month", "last year"):
        bad = _fresh_pk(pk, f"Hi Jonas, Alex from Pendo. Bellbird raised money {phrase}. Who owns product?", cfg)
        assert "UNSUPPORTED_CLAIM" in errs(bad), phrase


def test_relative_time_without_age_is_only_a_warning(cfg):
    vs = _op(cfg, "Hi Priya, Alex from Pendo. Larkspur announced this last week. Who owns the app?")
    assert "RELATIVE_TIME_UNVERIFIED" in errs(vs, WARNING)


def _fresh_pk(pk, opener, cfg):
    return check_opener_block(block(pk, opener, gk="Who looks after the product team there, please?"), pk, cfg)


@pytest.mark.parametrize("amount,ok", [("NZ$18 million", True), ("$18 million", True), ("A$18 million", False),
                                       ("US$18 million", False), ("NZ$18 billion", False), ("NZ$180 million", False)])
def test_currency_and_magnitude(cfg, amount, ok):
    vs = _op(cfg, f"Hi Jonas, Alex from Pendo. Bellbird raised {amount}. Who owns product?", key="funding",
             gk="Who looks after the product team there, please?")
    assert ("NUMBER_UNSUPPORTED" not in errs(vs)) is ok


def test_unsupported_quality_claims_unless_the_packet_says_so(cfg):
    pk = copy.deepcopy(PACKETS["product_launch"])
    bad = _fresh_pk(pk, "Hi Marcus, Alex from Pendo. Tidewater's app crashes. Who owns it?", cfg)
    assert "UNSUPPORTED_CLAIM" in errs(bad)
    pk["available_facts"][1]["text"] = "Tidewater Credit Union app reviews mention crashes"
    ok = _fresh_pk(pk, "Hi Marcus, Alex from Pendo. Tidewater's app crashes. Who owns it?", cfg)
    assert "UNSUPPORTED_CLAIM" not in errs(ok)


def test_event_synonyms_are_accepted_but_opposites_are_not(cfg):
    assert "TRIGGER_MISMATCH" not in errs(_op(cfg, "Hi Priya, Alex from Pendo. Dana Whitfield joined as Chief Digital Officer. Who owns the app?"))
    assert "TRIGGER_MISMATCH" in errs(_op(cfg, "Hi Priya, Alex from Pendo. Dana Whitfield resigned as Chief Digital Officer. Who owns the app?"))


# ============================================================================== every code has a minimal trigger

LARK = PACKETS["exec_move"]
GOOD_OP = block(LARK, "Hi Priya, Alex from Pendo. Who owns the customer app?")


def _o(old, new):
    assert old in GOOD_OP, old
    return check_opener_block(GOOD_OP.replace(old, new, 1), LARK, _CAL)


def _trig(line):
    return check_opener_block("\n".join(GOOD_OP.splitlines()[:4] + ["TRIGGER: " + line]), LARK, _CAL)


def _e(subject=None, body=None, pk=None):
    good_body = ("Hi Priya,\nJust tried you. Larkspur Insurance appointed a new Chief Digital Officer.\n"
                 "A new leader often reviews the app.\nWho owns it?\nAlex")
    return check_email_draft(subject or "tried you re Larkspur", body if body is not None else good_body, pk or LARK, _CAL)


_CAL = None  # filled by the autouse fixture below


@pytest.fixture(autouse=True)
def _set_cal(cfg):
    global _CAL
    _CAL = cfg


GOOD_BODY = ("Hi Priya,\nJust tried you. Larkspur Insurance appointed a new Chief Digital Officer.\n"
             "A new leader often reviews the app.\nWho owns it?\nAlex")

CODE_CASES = {
    "PACKET_INVALID": lambda: check_opener_block("x", None, _CAL),
    "TEXT_INVALID": lambda: check_opener_block(None, LARK, _CAL),
    "UNICODE_SUSPECT": lambda: _o("Hi Priya", "Hi Prіya"),
    "SHAPE_MISSING": lambda: check_opener_block("OPENER: Hi?", LARK, _CAL),
    "SHAPE_ORDER": lambda: check_opener_block("\n".join(reversed(GOOD_OP.splitlines())), LARK, _CAL),
    "SHAPE_EXTRA": lambda: check_opener_block("Sure:\n" + GOOD_OP, LARK, _CAL),
    "DRAFT_STYLE_LABEL_MISSING": lambda: check_opener_block(GOOD_OP, LARK, {**_CAL, "calibrated": False}),
    "DRAFT_STYLE_LABEL_STALE": lambda: check_opener_block(LABEL + "\n" + GOOD_OP, LARK, _CAL),
    "OPENER_TOO_LONG": lambda: _o("Who owns the customer app?", "Who owns the customer app " + "really " * 25 + "?"),
    "OPENER_NO_QUESTION": lambda: _o("Who owns the customer app?", "Tell me who owns the customer app."),
    "OPENER_MULTIPLE_QUESTIONS": lambda: _o("Who owns the customer app?", "Who owns it? Is it you?"),
    "OPENER_PENDO_FEATURE": lambda: _o("Who owns", "In-app guides: who owns"),
    "OPENER_PENDO_FEATURE_SOFT": lambda: _o("Who owns the", "Who owns the analytics for the"),
    "ROLE_ASSUMED": lambda: check_opener_block(GOOD_OP.replace("Who owns the customer app?", "Your team owns the app?"),
                                               {**LARK, "ambiguity_flags": ["role ambiguous"]}, _CAL),
    "BANNED_PHRASE": lambda: _o("Hi Priya,", "Hi Priya, hope you're well."),
    "EXCLAMATION": lambda: _o("Pendo.", "Pendo!"),
    "EMOJI": lambda: _o("Pendo.", "Pendo \U0001F680."),
    "UNSUPPORTED_CLAIM": lambda: _o("Who owns the customer app?", "We help companies like yours. Who owns the app?"),
    "MUST_NOT_CLAIM": lambda: _o("Who owns the customer app?", "Who has an existing customer app?"),
    "HISTORY_CLAIM": lambda: _o("Who owns the customer app?", "We spoke last week. Who owns it?"),
    "EMAIL_ADDRESS": lambda: _o("Hi Priya,", "Hi a@b.com,"),
    "PHONE_NUMBER": lambda: _o("Hi Priya,", "Hi Priya, 0412 345 678,"),
    "LINK": lambda: _o("Hi Priya,", "Hi Priya, www.pendo.io,"),
    "FACT_NO_SOURCE": lambda: _o(" (source: zoominfo_scoop:FAKE-101, 2026-11-12)\nFACT 2", "\nFACT 2"),
    "FACT_NONE_FORMAT": lambda: _o("FACT 2: Larkspur Insurance runs a customer app for policy claims (source: website:larkspur-insurance.example.com/app, 2026-11-20)", "FACT 2: None."),
    "FACT_UNSUPPORTED": lambda: _o("runs a customer app for policy claims", "runs a data lake for ads"),
    "FACT_SOURCE_UNKNOWN": lambda: _o("zoominfo_scoop:FAKE-101, 2026-11-12)\nFACT 2", "linkedin:post, 2026-11-12)\nFACT 2"),
    "FACT_SOURCE_MISMATCH": lambda: _o("(source: zoominfo_scoop:FAKE-101, 2026-11-12)\nFACT 2", "(source: website:larkspur-insurance.example.com/app, 2026-11-20)\nFACT 2"),
    "FACT_DATE_MISSING": lambda: _o("FAKE-101, 2026-11-12)\nFACT 2", "FAKE-101)\nFACT 2"),
    "FACT_DATE_INVENTED": lambda: _o("FAKE-101, 2026-11-12)\nFACT 2", "FAKE-101, 2019-01-01)\nFACT 2"),
    "FACT_DUPLICATE": lambda: check_opener_block("\n".join(GOOD_OP.splitlines()[:2] + ["FACT 2: " + GOOD_OP.splitlines()[1][8:]] + GOOD_OP.splitlines()[3:]), LARK, _CAL),
    "FACT_SKIPPED": lambda: check_opener_block(GOOD_OP.replace(GOOD_OP.splitlines()[2], "FACT 2: none verified"), LARK, _CAL),
    "NUMBER_UNSUPPORTED": lambda: _o("Who owns the customer app?", "Who owns the app for 12345 users?"),
    "PROPER_NOUN_UNSUPPORTED": lambda: _o("Who owns the customer app?", "Who owns the Zephyr app?"),
    "INVENTED_TRIGGER": lambda: check_opener_block(GOOD_OP.replace("Who owns the customer app?", "Congrats on the news. Who owns it?").replace(GOOD_OP.splitlines()[4], "TRIGGER: " + tc.NO_TRIGGER_LINE), {**LARK, "trigger": None}, _CAL),
    "TRIGGER_MISMATCH": lambda: _o("Who owns the customer app?", "Larkspur acquired a rival. Who owns the app?"),
    "TRIGGER_LINE_CLAIMED": lambda: check_opener_block(GOOD_OP, {**LARK, "trigger": None}, _CAL),
    "TRIGGER_LINE_OMITTED": lambda: _trig(tc.NO_TRIGGER_LINE),
    "TRIGGER_LINE_UNSUPPORTED": lambda: _trig("Bellbird raised money (source: zoominfo_scoop:FAKE-101, 2026-11-12)"),
    "TRIGGER_SOURCE_MISMATCH": lambda: _o("TRIGGER: Larkspur Insurance appointed Dana Whitfield as Chief Digital Officer (source: zoominfo_scoop:FAKE-101", "TRIGGER: Larkspur Insurance appointed Dana Whitfield as Chief Digital Officer (source: zoominfo_scoop:FAKE-999"),
    "TRIGGER_DATE_MISSING": lambda: check_opener_block("\n".join(GOOD_OP.splitlines()[:4] + [GOOD_OP.splitlines()[4].replace(", 2026-11-12", "")]), LARK, _CAL),
    "TRIGGER_DATE_INVENTED": lambda: check_opener_block("\n".join(GOOD_OP.splitlines()[:4] + [GOOD_OP.splitlines()[4].replace("2026-11-12", "2019-01-01")]), LARK, _CAL),
    "GATEKEEPER_TOO_LONG": lambda: _o("Could you point me to whoever looks after your customer app?" if "Could you point" in GOOD_OP else "Who looks after the product side there, please?", "Who looks after the product side there, please, if you do not mind me asking you today?"),
    "GATEKEEPER_PITCH": lambda: _o("Who looks after the product side there, please?", "Who looks after it? We help teams."),
    "GATEKEEPER_PITCH_SOFT": lambda: _o("Who looks after the product side there, please?", "Who looks after the platform?"),
    "GATEKEEPER_NO_ROLE": lambda: _o("Who looks after the product side there, please?", "Is this Larkspur Insurance?"),
    "SIMILAR_TO_DISLIKED": lambda: check_opener_block(GOOD_OP, LARK, {**_CAL, "alex_examples": {"liked_openers": [], "proud_emails": [], "disliked_openers": ["Hi Priya, Alex from Pendo. Who owns the customer app?"]}}),
    "RELATIVE_TIME_UNVERIFIED": lambda: _o("Who owns the customer app?", "Who owns it since last week?"),
    "EMAIL_SUBJECT_LENGTH": lambda: _e(subject="tried"),
    "EMAIL_SUBJECT_CAPS": lambda: _e(subject="TRIED YOU"),
    "SUBJECT_FAKE_THREAD": lambda: _e(subject="Re: our chat"),
    "EMAIL_TOO_LONG": lambda: _e(body=GOOD_BODY.replace("Who owns it?", "Who owns it " + "really " * 80 + "?")),
    "EMAIL_EMPTY": lambda: _e(body=""),
    "EMAIL_LINE_COUNT": lambda: _e(body="Hi Priya,\nJust tried you. Who owns it?\nAlex"),
    "EMAIL_BULLETS": lambda: _e(body=GOOD_BODY.replace("A new leader", "- A new leader")),
    "EMAIL_OPENING": lambda: _e(body=GOOD_BODY.replace("Just tried you.", "I tried you.")),
    "EMAIL_NO_ASK": lambda: _e(body=GOOD_BODY.replace("Who owns it?", "That is all.")),
    "EMAIL_MULTIPLE_ASKS": lambda: _e(body=GOOD_BODY.replace("Who owns it?", "Who owns it? Who runs it?")),
    "EMAIL_SEND_LANGUAGE": lambda: _e(body=GOOD_BODY.replace("Alex", "I've sent an invite.\nAlex")),
    "EMAIL_SIGNOFF": lambda: _e(body=GOOD_BODY.replace("\nAlex", "\nCheers")),
}


@pytest.mark.parametrize("code", sorted(tc.LINT_CODES))
def test_every_code_has_a_minimal_trigger(code):
    assert code in CODE_CASES, f"add a minimal case for {code}"
    got = {v.code for v in CODE_CASES[code]()}
    assert code in got, f"{code} not produced; got {got}"


def test_code_cases_and_registry_agree():
    assert set(CODE_CASES) == set(tc.LINT_CODES)


# ============================================================================== the v2 skills stay in step with the lint
import re  # noqa: E402

_OP_MD = (ROOT / "skills/opener/SKILL.md").read_text(encoding="utf-8")
_EM_MD = (ROOT / "skills/email-draft/SKILL.md").read_text(encoding="utf-8")
_OP_EX = [(json.loads(p), o) for p, o in re.findall(r"Packet \(fake\):\n```json\n(.*?)\n```\n\nOutput:\n```text\n(.*?)\n```", _OP_MD, re.S)]
_EM_EX = []
for _p, _t in re.findall(r"Packet \(fake\):\n```json\n(.*?)\n```\n\nDraft:\n```text\n(.*?)\n```", _EM_MD, re.S):
    _s, _b = tc.parse_email_block(_t)
    _EM_EX.append((json.loads(_p), _s, _b))


def test_skill_example_counts():
    assert len(_OP_EX) == 9 and len(_EM_EX) == 6


@pytest.mark.parametrize("pk,text", _OP_EX, ids=[p["account_name"] for p, _ in _OP_EX])
def test_skill_opener_examples_lint_clean(real_cfg, cfg, pk, text):
    assert tc.format_violations(check_opener_block(text, pk, cfg)).startswith(("OK", "WARNING"))
    assert not has_errors(check_opener_block(text, pk, cfg))
    assert not has_errors(check_opener_block(LABEL + "\n" + text, pk, real_cfg))
    assert "DRAFT_STYLE_LABEL_MISSING" in errs(check_opener_block(text, pk, real_cfg))


@pytest.mark.parametrize("pk,subject,body", _EM_EX, ids=[p["account_name"] for p, _, _ in _EM_EX])
def test_skill_email_examples_lint_clean(real_cfg, cfg, pk, subject, body):
    assert not has_errors(check_email_draft(subject, body, pk, cfg))
    assert not has_errors(check_email_draft(subject, LABEL + "\n" + body, pk, real_cfg))
    assert "DRAFT_STYLE_LABEL_MISSING" in errs(check_email_draft(subject, body, pk, real_cfg))


@pytest.mark.parametrize("pk,text", _OP_EX, ids=[p["account_name"] for p, _ in _OP_EX])
def test_skill_opener_examples_use_real_packet_keys(pk, text):
    assert "flags" not in pk and "trigger" not in pk and "ambiguity_flags" in pk and "signals" in pk


def test_skill_edge_cases_are_covered_by_examples():
    keys = {tuple(sorted((p["prospect_first_name"] is None, p["has_real_trigger"], bool(p["ambiguity_flags"]),
                          any(s["kind"] == "intent" for s in p["signals"]), bool(p["contact_history"]),
                          sum(1 for f in p["available_facts"] if not f["text"].startswith("Previous")) < 2))) for p, _ in _OP_EX}
    assert any(p["prospect_first_name"] is None for p, _ in _OP_EX)                      # no first name
    assert any(any(s["kind"] == "intent" for s in p["signals"]) for p, _ in _OP_EX)        # intent-only
    assert any(p["contact_history"] for p, _ in _OP_EX)                                  # history present
    assert any(p["ambiguity_flags"] for p, _ in _OP_EX)                                  # ambiguous role
    assert any(not p["has_real_trigger"] and not p["signals"] for p, _ in _OP_EX)          # no trigger
    assert any(len(p["available_facts"]) == 1 for p, _ in _OP_EX)                        # one fact only
    assert any(p["ambiguity_flags"] for p, _, _ in _EM_EX) and any(p["prospect_first_name"] is None for p, _, _ in _EM_EX)
    assert keys


@pytest.mark.parametrize("md", [_OP_MD, _EM_MD], ids=["opener", "email-draft"])
def test_skills_state_the_operating_rules(md):
    for needle in ("python -m src.textcheck", "at most 3 times", "NEEDS HUMAN:", "necessary, not sufficient",
                   "DRAFT STYLE: not yet calibrated to Alex", "ambiguity_flags", "FAKE", "structure only",
                   "Never edit the lint", "contact_history"):
        assert needle in md, needle
    assert "<- only while" not in md and "<-" not in md


def test_opener_skill_forbids_writing_anywhere():
    assert "Never write anything to Nooks, Gmail or Salesforce" in _OP_MD
    for tool in ("send_message", "sendTaskEmail", "createEmail"):
        assert tool in _EM_MD
    assert "NEVER send" in _EM_MD


def test_skill_cli_command_is_the_real_cli(tmp_path):
    """The exact command in the skill runs, on an example, and exits 0."""
    pk, text = _OP_EX[0]
    (tmp_path / "packet.json").write_text(json.dumps(pk))
    (tmp_path / "block.txt").write_text(LABEL + "\n" + text)
    r = subprocess.run([sys.executable, "-m", "src.textcheck", "opener", "--packet", str(tmp_path / "packet.json"),
                        "--text", str(tmp_path / "block.txt"), "--voice", "config/voice.yaml"], cwd=ROOT,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_call_history_fact_is_not_citable(cfg):
    pk, text = _OP_EX[8]    # Fennel Software: has a "Previous contact" fact
    bad = text.replace("FACT 2: About 40 employees (source: Salesforce account record (ZoomInfo employee count))",
                       "FACT 2: Previous contact: last call 2026-11-20, outcome No Answer (source: Nooks call log)")
    assert bad != text
    assert "FACT_UNSUPPORTED" in errs(check_opener_block(bad, pk, cfg))
