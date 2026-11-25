"""Tests for src/textcheck.py. Everything here is FAKE: invented companies and people, no live data."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from src import textcheck as tc
from src.textcheck import (ERROR, LINT_CODES, WARNING, Violation, check_email_draft, check_opener_block,
                           format_violations, has_errors, load_voice_config, normalize_packet, parse_block,
                           parse_email_block)

ROOT = Path(__file__).resolve().parent.parent
LABEL = "DRAFT STYLE: not yet calibrated to Alex"
PACKETS = json.loads((ROOT / "tests/fixtures/text_packets.json").read_text())
PACKETS.pop("_comment", None)

@pytest.fixture(scope="module")
def real_cfg():
    return load_voice_config(ROOT / "config/voice.yaml")


@pytest.fixture()
def cfg(real_cfg):
    """Real rules, calibrated, so most tests need no label line."""
    c = copy.deepcopy(real_cfg)
    c["calibrated"] = True
    return c


def pk(name: str) -> dict:
    return copy.deepcopy(PACKETS[name])


def codes(vs: list[Violation], severity: str | None = None) -> set[str]:
    out = {v.code for v in vs if severity is None or v.severity == severity}
    return out


# ---- good examples come straight from the skill files, so the docs cannot rot ------------------------------

_GOOD = json.loads((ROOT / "tests/fixtures/text_good_blocks.json").read_text())


def _opener_examples() -> list[tuple[dict, str]]:
    return [(x["packet"], x["text"]) for x in _GOOD["openers"]]


def _email_examples() -> list[tuple[str, str, str]]:
    return [(x["packet_key"], x["subject"], x["body"]) for x in _GOOD["emails"]]


OPENERS = _opener_examples()
EMAILS = _email_examples()
OPENER_BY_ACCOUNT = {p["account_name"]: (p, o) for p, o in OPENERS}


def opener_case(account: str) -> tuple[dict, str]:
    p, o = OPENER_BY_ACCOUNT[account]
    return copy.deepcopy(p), o


def swap(text: str, old: str, new: str) -> str:
    assert old in text, old
    return text.replace(old, new, 1)


def test_fixture_has_six_opener_examples_and_four_email_examples():
    assert len(OPENERS) == 6
    assert len(EMAILS) == 4


@pytest.mark.parametrize("packet,text", OPENERS, ids=[p["account_name"] for p, _ in OPENERS])
def test_skill_opener_examples_pass(cfg, packet, text):
    assert not has_errors(check_opener_block(text, packet, cfg)), format_violations(check_opener_block(text, packet, cfg))


@pytest.mark.parametrize("packet,text", OPENERS, ids=[p["account_name"] for p, _ in OPENERS])
def test_skill_opener_examples_pass_with_label_when_uncalibrated(real_cfg, packet, text):
    assert not has_errors(check_opener_block(LABEL + "\n" + text, packet, real_cfg))


@pytest.mark.parametrize("packet,text", OPENERS, ids=[p["account_name"] for p, _ in OPENERS])
def test_skill_opener_examples_fail_without_label_when_uncalibrated(real_cfg, packet, text):
    assert "DRAFT_STYLE_LABEL_MISSING" in codes(check_opener_block(text, packet, real_cfg), ERROR)


@pytest.mark.parametrize("key,subject,body", EMAILS, ids=[e[0] for e in EMAILS])
def test_skill_email_examples_pass(cfg, key, subject, body):
    vs = check_email_draft(subject, body, PACKETS[key], cfg)
    assert not has_errors(vs), format_violations(vs)


@pytest.mark.parametrize("key,subject,body", EMAILS, ids=[e[0] for e in EMAILS])
def test_skill_email_examples_label_rules(real_cfg, key, subject, body):
    assert "DRAFT_STYLE_LABEL_MISSING" in codes(check_email_draft(subject, body, PACKETS[key], real_cfg), ERROR)
    assert not has_errors(check_email_draft(subject, LABEL + "\n" + body, PACKETS[key], real_cfg))


def test_real_config_loads_and_is_uncalibrated_with_empty_examples(real_cfg):
    assert real_cfg["calibrated"] is False
    assert real_cfg["alex_examples"] == {"liked_openers": [], "disliked_openers": [], "proud_emails": []}
    assert real_cfg["draft_style_label"] == LABEL


# ---- parse helpers ------------------------------------------------------------------------------------------

def test_parse_block_sections():
    p, o = opener_case("Larkspur Insurance")
    sec, order, extra = parse_block(o)
    assert order == list(tc.LABELS) and not extra and sec["OPENER"].startswith("Hi Priya")


def test_parse_email_block_splits_subject_and_body():
    s, b = parse_email_block("SUBJECT: tried you today\nBODY:\nHi A,\nJust tried you.\nAlex")
    assert s == "tried you today" and b.splitlines()[0] == "Hi A,"


def test_parse_email_block_keeps_label_in_body():
    _, b = parse_email_block(LABEL + "\nSUBJECT: x y\nBODY:\nHi A,\nAlex")
    assert b.splitlines()[0] == LABEL


# ---- shape --------------------------------------------------------------------------------------------------

def _larkspur():
    return opener_case("Larkspur Insurance")


def test_shape_missing_line(cfg):
    p, o = _larkspur()
    bad = "\n".join(o.splitlines()[:3] + o.splitlines()[4:])
    assert "SHAPE_MISSING" in codes(check_opener_block(bad, p, cfg), ERROR)


def test_shape_order(cfg):
    p, o = _larkspur()
    ls = o.splitlines()
    ls[1], ls[2] = ls[2], ls[1]
    assert "SHAPE_ORDER" in codes(check_opener_block("\n".join(ls), p, cfg), ERROR)


@pytest.mark.parametrize("extra", ["Here is your opener:", "```", "", "**OPENER:** hi", "NOTE: check this"])
def test_shape_extra_lines(cfg, extra):
    p, o = _larkspur()
    assert "SHAPE_EXTRA" in codes(check_opener_block(extra + "\n" + o, p, cfg), ERROR)


def test_shape_duplicate_label_is_extra(cfg):
    p, o = _larkspur()
    assert "SHAPE_EXTRA" in codes(check_opener_block(o + "\nOPENER: Hi again?", p, cfg), ERROR)


def test_shape_label_needs_colon_space(cfg):
    p, o = _larkspur()
    bad = o.replace("OPENER: ", "OPENER:", 1)
    c = codes(check_opener_block(bad, p, cfg), ERROR)
    assert "SHAPE_MISSING" in c and "SHAPE_EXTRA" in c


def test_shape_clean_passes(cfg):
    p, o = _larkspur()
    assert check_opener_block(o, p, cfg) == []


def test_empty_text_reports_all_missing(cfg):
    p, _ = _larkspur()
    vs = check_opener_block("", p, cfg)
    assert sum(v.code == "SHAPE_MISSING" for v in vs) == 5


def test_non_string_text(cfg):
    assert codes(check_opener_block(None, {}, cfg)) == {"TEXT_INVALID"}  # type: ignore[arg-type]
    assert codes(check_email_draft(None, "x", {}, cfg)) == {"TEXT_INVALID"}  # type: ignore[arg-type]


def test_non_mapping_packet(cfg):
    assert codes(check_opener_block("x", None, cfg)) == {"PACKET_INVALID"}  # type: ignore[arg-type]
    assert codes(check_email_draft("a b", "x", [], cfg)) == {"PACKET_INVALID"}  # type: ignore[arg-type]


# ---- calibration label --------------------------------------------------------------------------------------

def test_label_stale_warning_when_calibrated(cfg):
    p, o = _larkspur()
    vs = check_opener_block(LABEL + "\n" + o, p, cfg)
    assert "DRAFT_STYLE_LABEL_STALE" in codes(vs, WARNING) and not has_errors(vs)


def test_label_must_be_first_line(real_cfg):
    p, o = _larkspur()
    assert "DRAFT_STYLE_LABEL_MISSING" in codes(check_opener_block(o + "\n" + LABEL, p, real_cfg), ERROR)


def test_label_wrong_wording(real_cfg):
    p, o = _larkspur()
    assert "DRAFT_STYLE_LABEL_MISSING" in codes(check_opener_block("DRAFT STYLE: ok\n" + o, p, real_cfg), ERROR)


# ---- opener: length, question, features, banned words, punctuation ------------------------------------------

def test_opener_too_long(cfg):
    p, o = _larkspur()
    long = swap(o, "Is now a bad time for a short question?",
                "Is now a bad time for a short question about how the team is planning the next few months?")
    assert "OPENER_TOO_LONG" in codes(check_opener_block(long, p, cfg), ERROR)


def _with_opener(o: str, opener: str) -> str:
    ls = o.splitlines()
    ls[0] = "OPENER: " + opener
    return "\n".join(ls)


def test_opener_at_limit_passes(cfg):
    p, o = _larkspur()
    op = "Hi Priya, Alex from Pendo. Larkspur just appointed a new Chief Digital Officer. Is now really a bad time for a short question today again?"
    assert len(op.split()) == 25
    assert "OPENER_TOO_LONG" not in codes(check_opener_block(_with_opener(o, op), p, cfg))


def test_opener_one_over_limit(cfg):
    p, o = _larkspur()
    op = "Hi Priya, Alex from Pendo. Larkspur just appointed a new Chief Digital Officer. Is now really a bad time for a short question today again now?"
    assert len(op.split()) == 26
    assert "OPENER_TOO_LONG" in codes(check_opener_block(_with_opener(o, op), p, cfg), ERROR)


def test_opener_no_question_warning(cfg):
    p, o = _larkspur()
    assert "OPENER_NO_QUESTION" in codes(check_opener_block(o.replace("?", ".", 1), p, cfg), WARNING)


def test_opener_two_questions_is_an_error(cfg):
    p, o = _larkspur()
    two = swap(o, "Is now a bad time for a short question?", "Is now OK? Or later?")
    assert "OPENER_MULTIPLE_QUESTIONS" in codes(check_opener_block(two, p, cfg), ERROR)


@pytest.mark.parametrize("word", ["in-app guides", "session replay", "walkthroughs", "heatmaps", "product analytics",
                                  "onboarding checklists"])
def test_opener_pendo_feature(cfg, word):
    p, o = _larkspur()
    bad = swap(o, "short question", f"short question about {word}")
    assert "OPENER_PENDO_FEATURE" in codes(check_opener_block(bad, p, cfg), ERROR)


@pytest.mark.parametrize("word", ["analytics", "surveys", "funnels"])
def test_ambiguous_feature_words_are_only_warnings(cfg, word):
    p, o = _larkspur()
    vs = check_opener_block(swap(o, "short question", f"short question on {word}"), p, cfg)
    assert "OPENER_PENDO_FEATURE_SOFT" in codes(vs, WARNING) and "OPENER_PENDO_FEATURE" not in codes(vs)


def test_feature_word_in_the_packet_is_not_flagged(cfg):
    p, o = _larkspur()
    p["available_facts"][1]["text"] = "Larkspur Insurance runs a customer app with session replay"
    ok = swap(o, "short question", "short question on session replay")
    assert "OPENER_PENDO_FEATURE" not in codes(check_opener_block(ok, p, cfg))


def test_pendo_feature_only_checked_in_opener(cfg):
    p, o = _larkspur()
    p["available_facts"][1]["text"] = "Larkspur Insurance runs a customer app with analytics"
    bad = swap(o, "runs a customer app for policy claims", "runs a customer app with analytics")
    assert "OPENER_PENDO_FEATURE" not in codes(check_opener_block(bad, p, cfg))


@pytest.mark.parametrize("phrase", ["Hope you're well", "I'm reaching out", "It is a quick call", "to touch base",
                                    "let me circle back", "no synergy", "we leverage data", "a game-changing app",
                                    "to revolutionise it", "Hope you’re well"])
def test_banned_phrases_in_opener(cfg, phrase):
    p, o = _larkspur()
    bad = swap(o, "Is now a bad time for a short question?", phrase + "?")
    assert "BANNED_PHRASE" in codes(check_opener_block(bad, p, cfg), ERROR)


def test_banned_phrase_whole_word_only(cfg):
    p, o = _larkspur()
    ok = swap(o, "short question", "short question on lever arch files")
    assert "BANNED_PHRASE" not in codes(check_opener_block(ok, p, cfg))


def test_banned_phrase_in_gatekeeper(cfg):
    p, o = _larkspur()
    bad = swap(o, "Could you point me to", "Hope you're well. Point me to")
    assert "BANNED_PHRASE" in codes(check_opener_block(bad, p, cfg), ERROR)


def test_exclamation_mark(cfg):
    p, o = _larkspur()
    assert "EXCLAMATION" in codes(check_opener_block(swap(o, "Alex from Pendo.", "Alex from Pendo!"), p, cfg), ERROR)


@pytest.mark.parametrize("emoji", ["\U0001F680", "\U0001F44B", "✨", "\U0001F600"])
def test_emoji(cfg, emoji):
    p, o = _larkspur()
    assert "EMOJI" in codes(check_opener_block(swap(o, "Hi Priya,", f"Hi Priya {emoji},"), p, cfg), ERROR)


# ---- contact details ----------------------------------------------------------------------------------------

def test_email_address_in_opener(cfg):
    p, o = _larkspur()
    bad = swap(o, "Hi Priya,", "Hi priya@larkspur-insurance.example.com,")
    assert "EMAIL_ADDRESS" in codes(check_opener_block(bad, p, cfg), ERROR)


def test_email_address_in_gatekeeper(cfg):
    p, o = _larkspur()
    bad = swap(o, "whoever looks after your customer app?", "whoever looks after it? Try sam@fake.example.com")
    assert "EMAIL_ADDRESS" in codes(check_opener_block(bad, p, cfg), ERROR)


@pytest.mark.parametrize("phone", ["+61 400 000 001", "0400 000 001", "(02) 9000 0000", "+61400000001"])
def test_phone_number(cfg, phone):
    p, o = _larkspur()
    assert "PHONE_NUMBER" in codes(check_opener_block(swap(o, "Hi Priya,", f"Hi Priya, call {phone}."), p, cfg), ERROR)


def test_dates_are_not_phone_numbers(cfg):
    p, o = _larkspur()
    assert "PHONE_NUMBER" not in codes(check_opener_block(o, p, cfg))


def test_contact_value_from_packet_is_banned(cfg):
    p, o = _larkspur()
    p["prospect_phone"] = "+61 411 222 333"
    assert "PHONE_NUMBER" in codes(check_opener_block(swap(o, "Hi Priya,", "Hi Priya +61 411 222 333,"), p, cfg), ERROR)


def test_link_in_opener(cfg):
    p, o = _larkspur()
    assert "LINK" in codes(check_opener_block(swap(o, "Hi Priya,", "Hi Priya, see www.pendo.io"), p, cfg), ERROR)


# ---- facts --------------------------------------------------------------------------------------------------

def test_fact_missing_source(cfg):
    p, o = _larkspur()
    bad = swap(o, " (source: zoominfo_scoop:FAKE-101, 2026-11-12)\nFACT 2", "\nFACT 2")
    assert "FACT_NO_SOURCE" in codes(check_opener_block(bad, p, cfg), ERROR)


def test_fact_invented_text_is_possibly_invented(cfg):
    p, o = _larkspur()
    bad = swap(o, "Larkspur Insurance runs a customer app for policy claims",
               "Larkspur Insurance is building a data lake with a new vendor")
    vs = check_opener_block(bad, p, cfg)
    assert "FACT_UNSUPPORTED" in codes(vs, ERROR)
    assert any("possibly invented" in v.message for v in vs)


def test_fact_with_plausible_but_unsupported_number(cfg):
    p, o = opener_case("Tidewater Credit Union")
    bad = swap(o, "has about 310 employees", "has about 450 employees")
    assert "NUMBER_UNSUPPORTED" in codes(check_opener_block(bad, p, cfg), ERROR)


def test_fact_unsupported_extra_claim_appended(cfg):
    p, o = _larkspur()
    bad = swap(o, "runs a customer app for policy claims", "runs a customer app for policy claims used by 90000 people")
    assert "NUMBER_UNSUPPORTED" in codes(check_opener_block(bad, p, cfg), ERROR)


def test_fact_source_unknown(cfg):
    p, o = _larkspur()
    bad = swap(o, "zoominfo_scoop:FAKE-101, 2026-11-12)\nFACT 2", "linkedin:post, 2026-11-12)\nFACT 2")
    assert "FACT_SOURCE_UNKNOWN" in codes(check_opener_block(bad, p, cfg), ERROR)


def test_fact_source_mismatch(cfg):
    p, o = _larkspur()
    bad = swap(o, "(source: zoominfo_scoop:FAKE-101, 2026-11-12)\nFACT 2", "(source: website:larkspur-insurance.example.com/app, 2026-11-12)\nFACT 2")
    assert "FACT_SOURCE_MISMATCH" in codes(check_opener_block(bad, p, cfg), ERROR)


def test_fact_date_missing(cfg):
    p, o = _larkspur()
    assert "FACT_DATE_MISSING" in codes(check_opener_block(swap(o, "FAKE-101, 2026-11-12)\nFACT 2", "FAKE-101)\nFACT 2"), p, cfg), ERROR)


def test_fact_wrong_date(cfg):
    p, o = _larkspur()
    bad = swap(o, "FAKE-101, 2026-11-12)\nFACT 2", "FAKE-101, 2026-10-12)\nFACT 2")
    assert "FACT_DATE_INVENTED" in codes(check_opener_block(bad, p, cfg), ERROR)


@pytest.mark.parametrize("d", ["12 Nov 2026", "12 November 2026", "12/11/2026"])
def test_fact_date_other_formats_accepted(cfg, d):
    p, o = _larkspur()
    ok = swap(o, "FAKE-101, 2026-11-12)\nFACT 2", f"FAKE-101, {d})\nFACT 2")
    assert not has_errors(check_opener_block(ok, p, cfg))


def test_fact_duplicate_is_padding(cfg):
    p, o = _larkspur()
    ls = o.splitlines()
    ls[2] = "FACT 2: " + ls[1][len("FACT 1: "):]
    assert "FACT_DUPLICATE" in codes(check_opener_block("\n".join(ls), p, cfg), ERROR)


def test_fact_two_padded_when_only_one_fact_exists(cfg):
    p, o = opener_case("Cinder Payments")
    bad = swap(o, "FACT 2: none verified", "FACT 2: Cinder Payments is a payments firm in Sydney (source: salesforce:Account, 2026-11-01)")
    c = codes(check_opener_block(bad, p, cfg), ERROR)
    assert "FACT_UNSUPPORTED" in c


def test_fact_two_none_verified_ok_with_one_fact(cfg):
    p, o = opener_case("Cinder Payments")
    assert check_opener_block(o, p, cfg) == []


def test_fact_none_verified_wrong_form(cfg):
    p, o = opener_case("Cinder Payments")
    assert "FACT_NONE_FORMAT" in codes(check_opener_block(swap(o, "none verified", "None verified."), p, cfg), ERROR)


def test_fact_skipped_warning_when_packet_has_facts(cfg):
    p, o = _larkspur()
    bad = swap(o, "FACT 2: Larkspur Insurance runs a customer app for policy claims (source: website:larkspur-insurance.example.com/app, 2026-11-20)", "FACT 2: none verified")
    vs = check_opener_block(bad, p, cfg)
    assert "FACT_SKIPPED" in codes(vs, WARNING) and not has_errors(vs)


def test_fact_without_source_in_packet_is_unusable(cfg):
    p, o = _larkspur()
    del p["available_facts"][1]["source"]
    assert "FACT_UNSUPPORTED" in codes(check_opener_block(o, p, cfg), ERROR)


def test_fact_one_extra_word_is_enough_to_fail_the_tight_overlap(cfg):
    p, o = _larkspur()
    bad = swap(o, "runs a customer app for policy claims", "runs a customer app for policy claims today")
    assert "FACT_UNSUPPORTED" in codes(check_opener_block(bad, p, cfg), ERROR)


def test_fact_reordered_words_with_same_content_pass(cfg):
    p, o = _larkspur()
    ok = swap(o, "runs a customer app for policy claims", "runs a customer app for claims policy")
    assert "FACT_UNSUPPORTED" not in codes(check_opener_block(ok, p, cfg))


def test_fact_trigger_evidence_counts_as_a_source_of_facts(cfg):
    p, o = _larkspur()
    p["available_facts"] = []
    assert "FACT_UNSUPPORTED" not in codes(check_opener_block(o.replace(o.splitlines()[2], "FACT 2: none verified"), p, cfg))


# ---- numbers and proper nouns -------------------------------------------------------------------------------

def test_number_in_opener_unsupported(cfg):
    p, o = _larkspur()
    assert "NUMBER_UNSUPPORTED" in codes(check_opener_block(swap(o, "short question", "short 7 minute question"), p, cfg), ERROR)


def test_number_in_opener_supported_by_packet(cfg):
    p, o = opener_case("Tidewater Credit Union")
    ok = swap(o, "Curious how the first weeks are going?", "Curious how it is going across the 310 staff?")
    assert "NUMBER_UNSUPPORTED" not in codes(check_opener_block(ok, p, cfg))


def test_proper_noun_unsupported_company(cfg):
    p, o = _larkspur()
    bad = swap(o, "short question", "short question for Harbourline")
    assert "PROPER_NOUN_UNSUPPORTED" in codes(check_opener_block(bad, p, cfg), ERROR)


def test_sentence_initial_company_with_suffix_is_caught(cfg):
    p, o = _larkspur()
    bad = swap(o, "Larkspur just appointed", "Harbourline Group just appointed")
    assert "PROPER_NOUN_UNSUPPORTED" in codes(check_opener_block(bad, p, cfg), ERROR)


def test_known_gap_bare_sentence_initial_company_name_is_not_name_checked(cfg):
    """DOCUMENTED GAP: a one-word company at the start of a sentence cannot be told from 'Ringing software teams'
    without a dictionary. Other rules (event words, numbers, claims) have to carry it."""
    p, o = _larkspur()
    bad = swap(o, "Larkspur just appointed", "Harbourline just appointed")
    assert "PROPER_NOUN_UNSUPPORTED" not in codes(check_opener_block(bad, p, cfg))


@pytest.mark.parametrize("starter", ["Honestly", "Apologies", "Maybe", "Perhaps", "Appreciate", "Understand", "Morning",
                                     "Ringing", "Everything", "Cold-ish", "Totally"])
def test_ordinary_sentence_starters_are_not_names(cfg, starter):
    p, o = _larkspur()
    ok = swap(o, "Is now a bad time", f"{starter} software teams are busy. Is now a bad time")
    assert "PROPER_NOUN_UNSUPPORTED" not in codes(check_opener_block(ok, p, cfg))


def test_proper_noun_unsupported_place(cfg):
    p, o = _larkspur()
    bad = swap(o, "short question", "short question for Brisbane")
    assert "PROPER_NOUN_UNSUPPORTED" in codes(check_opener_block(bad, p, cfg), ERROR)


def test_proper_noun_unsupported_in_gatekeeper(cfg):
    p, o = _larkspur()
    assert "PROPER_NOUN_UNSUPPORTED" in codes(check_opener_block(swap(o, "your customer app?", "your Zephyr app?"), p, cfg), ERROR)


def test_proper_noun_allowed_names(cfg):
    p, o = _larkspur()
    assert "PROPER_NOUN_UNSUPPORTED" not in codes(check_opener_block(o, p, cfg))


def test_proper_noun_possessive_matches_packet(cfg):
    p, o = _larkspur()
    ok = swap(o, "Larkspur just appointed", "Larkspur's board just appointed")
    assert "PROPER_NOUN_UNSUPPORTED" not in codes(check_opener_block(ok, p, cfg))


# ---- trigger consistency ------------------------------------------------------------------------------------

def test_trigger_line_must_be_exact_none_when_packet_has_none(cfg):
    p, o = opener_case("Marlow Software Group")
    assert "TRIGGER_LINE_CLAIMED" in codes(check_opener_block(swap(o, "TRIGGER: None found: cold call on ICP fit only", "TRIGGER: Marlow Software Group raised funding (source: zoominfo_scoop:FAKE-9, 2026-11-01)"), p, cfg), ERROR)


@pytest.mark.parametrize("kind", ["none", "None", "", None, "intent"])
def test_trigger_kind_none_variants_count_as_no_trigger(cfg, kind):
    p, o = _larkspur()
    p["trigger"] = {"kind": kind, "evidence": "x", "source": "s", "date": "2026-11-12"}
    assert "TRIGGER_LINE_CLAIMED" in codes(check_opener_block(o, p, cfg), ERROR)


def test_event_word_that_restates_a_packet_fact_is_not_an_invented_trigger(cfg):
    """H1: with no trigger, restating a real FACT is fine; a reaction or an unsupported event is not."""
    p, o = opener_case("Marlow Software Group")
    p["available_facts"][0]["text"] = "Marlow Software Group launched a builders app"
    ok = swap(o, "Marlow builds software for builders", "Marlow launched a builders app")
    assert "INVENTED_TRIGGER" not in codes(check_opener_block(ok, p, cfg))
    bad = swap(o, "Marlow builds software for builders", "Marlow acquired a rival")
    assert "INVENTED_TRIGGER" in codes(check_opener_block(bad, p, cfg), ERROR)


def test_trigger_line_omitted_warning(cfg):
    p, o = _larkspur()
    bad = swap(o, "TRIGGER: Larkspur Insurance appointed Dana Whitfield as Chief Digital Officer (source: zoominfo_scoop:FAKE-101, 2026-11-12)", "TRIGGER: None found: cold call on ICP fit only")
    assert "TRIGGER_LINE_OMITTED" in codes(check_opener_block(bad, p, cfg), WARNING)


def test_trigger_line_copied_from_another_company(cfg):
    p, o = _larkspur()
    other = "Bellbird Fitness raised NZ$18 million in a Series B round (source: zoominfo_scoop:FAKE-202, 2026-11-03)"
    bad = o.replace(o.splitlines()[4], "TRIGGER: " + other)
    c = codes(check_opener_block(bad, p, cfg), ERROR)
    assert {"TRIGGER_LINE_UNSUPPORTED", "PROPER_NOUN_UNSUPPORTED", "NUMBER_UNSUPPORTED"} <= c


def test_trigger_copied_from_different_company_in_opener(cfg):
    p, o = _larkspur()
    bad = swap(o, "Larkspur just appointed a new Chief Digital Officer.", "Bellbird Fitness just raised NZ$18 million.")
    c = codes(check_opener_block(bad, p, cfg), ERROR)
    assert "PROPER_NOUN_UNSUPPORTED" in c and "TRIGGER_MISMATCH" in c and "NUMBER_UNSUPPORTED" in c


def test_trigger_line_source_mismatch(cfg):
    p, o = _larkspur()
    bad = swap(o, "TRIGGER: Larkspur Insurance appointed Dana Whitfield as Chief Digital Officer (source: zoominfo_scoop:FAKE-101", "TRIGGER: Larkspur Insurance appointed Dana Whitfield as Chief Digital Officer (source: zoominfo_scoop:FAKE-999")
    assert "TRIGGER_SOURCE_MISMATCH" in codes(check_opener_block(bad, p, cfg), ERROR)


def test_trigger_line_missing_source(cfg):
    p, o = _larkspur()
    ls = o.splitlines()
    ls[4] = "TRIGGER: Larkspur Insurance appointed Dana Whitfield as Chief Digital Officer"
    assert "TRIGGER_SOURCE_MISMATCH" in codes(check_opener_block("\n".join(ls), p, cfg), ERROR)


def test_trigger_line_date_missing(cfg):
    p, o = _larkspur()
    ls = o.splitlines()
    ls[4] = ls[4].replace(", 2026-11-12)", ")")
    assert "TRIGGER_DATE_MISSING" in codes(check_opener_block("\n".join(ls), p, cfg), ERROR)


@pytest.mark.parametrize("claim", ["Saw the news last week.", "Congratulations on the move.", "Noticed you are expanding.",
                                   "Heard that you launched an app.", "Big raise recently."])
def test_invented_trigger_in_opener_when_none(cfg, claim):
    p, o = opener_case("Marlow Software Group")
    bad = swap(o, "Cold call, I'll be upfront.", claim)
    assert "INVENTED_TRIGGER" in codes(check_opener_block(bad, p, cfg), ERROR)


def test_no_trigger_opener_is_honest_and_passes(cfg):
    p, o = opener_case("Marlow Software Group")
    assert check_opener_block(o, p, cfg) == []


def test_trigger_event_word_not_in_evidence(cfg):
    p, o = _larkspur()
    assert "TRIGGER_MISMATCH" in codes(check_opener_block(swap(o, "just appointed a new Chief Digital Officer", "just acquired a new vendor"), p, cfg), ERROR)


def test_gatekeeper_must_not_mention_trigger(cfg):
    p, o = _larkspur()
    bad = swap(o, "whoever looks after your customer app?", "whoever handles the new appointment?")
    assert "GATEKEEPER_PITCH" in codes(check_opener_block(bad, p, cfg), ERROR)


def test_signals_list_is_used_when_trigger_key_absent(cfg):
    p, o = _larkspur()
    t = p.pop("trigger")
    p["signals"] = [{"kind": "partnership", "strength": 0.2, "evidence": "weak", "source": "x", "date": "2026-01-01"},
                    {"kind": "exec_move", "strength": 0.9, **{k: t[k] for k in ("evidence", "source", "date")}}]
    assert check_opener_block(o, p, cfg) == []


def test_signals_with_only_none_kinds_means_no_trigger(cfg):
    p, _ = opener_case("Marlow Software Group")
    p.pop("trigger")
    p["signals"] = [{"kind": "none", "strength": 0.0, "evidence": "", "source": "", "date": ""}]
    assert normalize_packet(p).trigger is None


# ---- unsupported claims -------------------------------------------------------------------------------------

@pytest.mark.parametrize("claim", ["We help companies like yours increase adoption by 30%.",
                                   "Our customers love it.", "Companies like you cut churn.", "We've helped teams in ANZ.",
                                   "Proven to boost retention by a lot.", "It is a 10x win.", "See our case study."])
def test_unsupported_claims_in_opener(cfg, claim):
    p, o = _larkspur()
    bad = swap(o, "Is now a bad time for a short question?", claim)
    assert "UNSUPPORTED_CLAIM" in codes(check_opener_block(bad, p, cfg), ERROR)


def test_adoption_claim_also_trips_number_check(cfg):
    p, o = _larkspur()
    bad = swap(o, "Is now a bad time for a short question?", "We help companies like yours increase adoption by 30%.")
    assert "NUMBER_UNSUPPORTED" in codes(check_opener_block(bad, p, cfg), ERROR)


def test_unsupported_claim_in_fact_line(cfg):
    p, o = _larkspur()
    p["available_facts"][1]["text"] = "Larkspur Insurance runs a customer app and we help companies like yours"
    bad = swap(o, "runs a customer app for policy claims", "runs a customer app and we help companies like yours")
    assert "UNSUPPORTED_CLAIM" in codes(check_opener_block(bad, p, cfg), ERROR)


def test_must_not_claim_phrase(cfg):
    p, o = _larkspur()
    bad = swap(o, "short question", "short question, you are an existing customer")
    assert "MUST_NOT_CLAIM" in codes(check_opener_block(bad, p, cfg), ERROR)


def test_must_not_claim_in_trigger_alias_key(cfg):
    p, o = _larkspur()
    p["must_not_invent"] = p.pop("must_not_claim") + ["Chief Digital Officer"]
    assert "MUST_NOT_CLAIM" in codes(check_opener_block(o, p, cfg), ERROR)


# ---- ambiguous role -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("phrase", ["As the Product Manager, you decide", "in your role", "your team", "You lead it",
                                    "Product Manager"])
def test_ambiguous_role_must_not_be_assumed(cfg, phrase):
    p, o = opener_case("Greenvale Dairy")
    bad = swap(o, "Who handles that side of the business there?", phrase + "?")
    assert "ROLE_ASSUMED" in codes(check_opener_block(bad, p, cfg), ERROR)


def test_role_assumption_allowed_without_flag(cfg):
    p, o = opener_case("Cinder Payments")
    assert "ROLE_ASSUMED" not in codes(check_opener_block(o, p, cfg))
    p["flags"] = []
    assert "your engineering team" in o


def test_ambiguous_role_clean_opener_passes(cfg):
    p, o = opener_case("Greenvale Dairy")
    assert check_opener_block(o, p, cfg) == []


# ---- gatekeeper ---------------------------------------------------------------------------------------------

def test_gatekeeper_too_long(cfg):
    p, o = _larkspur()
    bad = swap(o, "Could you point me to whoever looks after your customer app?", "Could you please point me to whoever it is that looks after your customer app there?")
    assert "GATEKEEPER_TOO_LONG" in codes(check_opener_block(bad, p, cfg), ERROR)


def test_gatekeeper_at_limit_14_words_passes(cfg):
    p, o = _larkspur()
    g = "Could you point me to whoever really looks after your customer app, if possible?"
    assert len(g.split()) == 14
    assert "GATEKEEPER_TOO_LONG" not in codes(check_opener_block(swap(o, "Could you point me to whoever looks after your customer app?", g), p, cfg))


def test_gatekeeper_fifteen_words_fails(cfg):
    p, o = _larkspur()
    g = "Could you point me to whoever looks after your customer app, if that is possible?"
    assert len(g.split()) == 15
    assert "GATEKEEPER_TOO_LONG" in codes(check_opener_block(swap(o, "Could you point me to whoever looks after your customer app?", g), p, cfg), ERROR)


@pytest.mark.parametrize("pitch", ["We help teams with analytics.", "Our platform is great.", "Can I demo it?",
                                   "It is a free trial."])
def test_gatekeeper_pitch(cfg, pitch):
    p, o = _larkspur()
    bad = swap(o, "Could you point me to whoever looks after your customer app?", "Who owns the app? " + pitch)
    assert "GATEKEEPER_PITCH" in codes(check_opener_block(bad, p, cfg), ERROR)


@pytest.mark.parametrize("line", ["Could you help me find who looks after the app?", "Is the head of product free?",
                                  "Who would I speak to about your software?", "Who looks after our quote? Sorry, your app?"])
def test_receptionist_english_is_not_a_pitch(cfg, line):
    p, o = _larkspur()
    ok = swap(o, "Could you point me to whoever looks after your customer app?", line)
    assert "GATEKEEPER_PITCH" not in codes(check_opener_block(ok, p, cfg))


def test_gatekeeper_soft_pitch_word_is_a_warning(cfg):
    p, o = _larkspur()
    vs = check_opener_block(swap(o, "your customer app?", "your platform?"), p, cfg)
    assert "GATEKEEPER_PITCH_SOFT" in codes(vs, WARNING) and "GATEKEEPER_PITCH" not in codes(vs)


def test_gatekeeper_no_role(cfg):
    p, o = _larkspur()
    bad = swap(o, "Could you point me to whoever looks after your customer app?", "Is this Larkspur Insurance?")
    assert "GATEKEEPER_NO_ROLE" in codes(check_opener_block(bad, p, cfg), ERROR)


# ---- disliked openers ---------------------------------------------------------------------------------------

def test_similar_to_disliked_warning(cfg):
    p, o = _larkspur()
    cfg["alex_examples"]["disliked_openers"] = ["Hi Priya, Alex from Pendo. Larkspur just appointed a new Chief Digital Officer. Is now a bad time for a short question?"]
    assert "SIMILAR_TO_DISLIKED" in codes(check_opener_block(o, p, cfg), WARNING)


def test_not_similar_to_disliked_passes(cfg):
    p, o = _larkspur()
    cfg["alex_examples"]["disliked_openers"] = ["Dear sir, our revolutionary platform awaits."]
    assert "SIMILAR_TO_DISLIKED" not in codes(check_opener_block(o, p, cfg))


# ---- email --------------------------------------------------------------------------------------------------

def good_email(key: str = "exec_move") -> tuple[str, str]:
    for k, s, b in EMAILS:
        if k == key:
            return s, b
    raise KeyError(key)


def test_email_good(cfg):
    s, b = good_email()
    assert check_email_draft(s, b, PACKETS["exec_move"], cfg) == []


@pytest.mark.parametrize("subject", ["tried", "tried you re the big new Larkspur thing", "", "tried you\nre Larkspur"])
def test_email_subject_length(cfg, subject):
    _, b = good_email()
    assert codes(check_email_draft(subject, b, PACKETS["exec_move"], cfg), ERROR) & {"EMAIL_SUBJECT_LENGTH", "EMAIL_EMPTY"}


@pytest.mark.parametrize("subject", ["tried you", "tried you re Larkspur app", "tried you re Larkspur"])
def test_email_subject_length_boundaries_pass(cfg, subject):
    _, b = good_email()
    assert "EMAIL_SUBJECT_LENGTH" not in codes(check_email_draft(subject, b, PACKETS["exec_move"], cfg))


def test_email_subject_caps_warning(cfg):
    _, b = good_email()
    assert "EMAIL_SUBJECT_CAPS" in codes(check_email_draft("TRIED YOU", b, PACKETS["exec_move"], cfg), WARNING)


def test_email_subject_exclamation_and_emoji(cfg):
    _, b = good_email()
    assert "EXCLAMATION" in codes(check_email_draft("tried you!", b, PACKETS["exec_move"], cfg), ERROR)
    assert "EMOJI" in codes(check_email_draft("tried you \U0001F44B", b, PACKETS["exec_move"], cfg), ERROR)


def test_email_too_long(cfg):
    s, b = good_email()
    bad = b.replace("Who is the best person to talk to about it?", "Who is the best person to talk to about it? " + " ".join(["more"] * 60))
    assert "EMAIL_TOO_LONG" in codes(check_email_draft(s, bad, PACKETS["exec_move"], cfg), ERROR)


def test_email_word_limit_boundary(cfg):
    s, b = good_email()
    lines = b.splitlines()
    have = len(b.split())
    lines[3] = lines[3].rstrip("?") + " " + " ".join(["x"] * (79 - have)) + "?"
    ok = "\n".join(lines)
    assert len(ok.split()) == 79
    assert "EMAIL_TOO_LONG" not in codes(check_email_draft(s, ok, PACKETS["exec_move"], cfg))
    lines[3] = lines[3].rstrip("?") + " x?"
    assert "EMAIL_TOO_LONG" in codes(check_email_draft(s, "\n".join(lines), PACKETS["exec_move"], cfg), ERROR)


def test_email_label_not_counted_in_words(cfg, real_cfg):
    s, b = good_email()
    assert "EMAIL_TOO_LONG" not in codes(check_email_draft(s, LABEL + "\n" + b, PACKETS["exec_move"], real_cfg))


def test_email_line_count_too_few(cfg):
    s, _ = good_email()
    assert "EMAIL_LINE_COUNT" in codes(check_email_draft(s, "Hi Priya,\nJust tried you. Who is the best person to talk to?\nAlex", PACKETS["exec_move"], cfg), ERROR)


def test_email_line_count_too_many(cfg):
    s, b = good_email()
    bad = b.replace("Alex", "Short line one.\nShort line two.\nShort line three.\nAlex")
    assert "EMAIL_LINE_COUNT" in codes(check_email_draft(s, bad, PACKETS["exec_move"], cfg), ERROR)


def test_email_opening_required(cfg):
    s, b = good_email()
    assert "EMAIL_OPENING" in codes(check_email_draft(s, b.replace("Just tried you.", "I wanted to ask."), PACKETS["exec_move"], cfg), ERROR)


def test_email_no_ask(cfg):
    s, b = good_email()
    assert "EMAIL_NO_ASK" in codes(check_email_draft(s, b.replace("Who is the best person to talk to about it?", "That is all."), PACKETS["exec_move"], cfg), ERROR)


def test_email_two_question_marks_is_two_asks(cfg):
    s, b = good_email()
    assert "EMAIL_MULTIPLE_ASKS" in codes(check_email_draft(s, b.replace("about it?", "about it? Or is that wrong?"), PACKETS["exec_move"], cfg), ERROR)


def test_email_question_plus_cue_is_two_asks(cfg):
    s, b = good_email()
    assert "EMAIL_MULTIPLE_ASKS" in codes(check_email_draft(s, b.replace("about it?", "about it? Let me know."), PACKETS["exec_move"], cfg), ERROR)


def test_email_cue_only_ask_counts_once(cfg):
    s, b = good_email()
    ok = b.replace("Who is the best person to talk to about it?", "Please let me know who to talk to.")
    c = codes(check_email_draft(s, ok, PACKETS["exec_move"], cfg))
    assert "EMAIL_NO_ASK" not in c and "EMAIL_MULTIPLE_ASKS" not in c


def test_email_link_banned_by_default(cfg):
    s, b = good_email()
    assert "LINK" in codes(check_email_draft(s, b.replace("Alex", "See https://pendo.io\nAlex"), PACKETS["exec_move"], cfg), ERROR)


def test_email_link_allowed_when_configured(cfg):
    s, b = good_email()
    cfg["email"]["allow_links"] = True
    assert "LINK" not in codes(check_email_draft(s, b.replace("Alex", "See https://pendo.io\nAlex"), PACKETS["exec_move"], cfg))


def test_email_address_in_body(cfg):
    s, b = good_email()
    assert "EMAIL_ADDRESS" in codes(check_email_draft(s, b.replace("Alex", "Reply to alex@fake.example.com\nAlex"), PACKETS["exec_move"], cfg), ERROR)


def test_email_phone_in_body(cfg):
    s, b = good_email()
    assert "PHONE_NUMBER" in codes(check_email_draft(s, b.replace("Alex", "+61 400 000 001\nAlex"), PACKETS["exec_move"], cfg), ERROR)


def test_email_contact_address_from_packet(cfg):
    s, b = good_email()
    p = pk("exec_move")
    p["prospect_email"] = "priya@larkspur-insurance.example.com"
    assert "EMAIL_ADDRESS" in codes(check_email_draft(s, b.replace("Hi Priya,", "Hi priya@larkspur-insurance.example.com,"), p, cfg), ERROR)


@pytest.mark.parametrize("phrase", ["I'll send this now.", "Sending this now.", "Sent from my phone.", "[SEND]", "This will auto-send."])
def test_email_send_language(cfg, phrase):
    s, b = good_email()
    assert "EMAIL_SEND_LANGUAGE" in codes(check_email_draft(s, b.replace("Alex", phrase + "\nAlex"), PACKETS["exec_move"], cfg), ERROR)


@pytest.mark.parametrize("word", ["quick", "Quick", "attached", "touch base", "Hope you're well", "synergy"])
def test_email_banned_words(cfg, word):
    s, b = good_email()
    assert "BANNED_PHRASE" in codes(check_email_draft(s, b.replace("closer look", f"closer look, {word}"), PACKETS["exec_move"], cfg), ERROR)


def test_quick_is_banned_in_email_but_not_in_opener(cfg):
    p, o = _larkspur()
    assert "BANNED_PHRASE" not in codes(check_opener_block(swap(o, "short question", "quick question"), p, cfg))


def test_email_exclamation(cfg):
    s, b = good_email()
    assert "EXCLAMATION" in codes(check_email_draft(s, b.replace("closer look.", "closer look!"), PACKETS["exec_move"], cfg), ERROR)


def test_email_invented_trigger_when_packet_has_none(cfg):
    s, b = good_email("none_cold")
    bad = b.replace("I don't have a specific reason for today.", "Congratulations on the raise.")
    assert "INVENTED_TRIGGER" in codes(check_email_draft(s, bad, PACKETS["none_cold"], cfg), ERROR)


def test_email_trigger_copied_from_other_company(cfg):
    s, b = good_email("none_cold")
    bad = b.replace("I don't have a specific reason for today.", "Bellbird Fitness raised NZ$18 million in a Series B round.")
    c = codes(check_email_draft(s, bad, PACKETS["none_cold"], cfg), ERROR)
    assert {"INVENTED_TRIGGER", "PROPER_NOUN_UNSUPPORTED", "NUMBER_UNSUPPORTED"} <= c


def test_email_trigger_that_is_not_the_packet_trigger(cfg):
    s, b = good_email()
    bad = b.replace("Larkspur Insurance appointed a new Chief Digital Officer.", "Larkspur Insurance acquired a rival.")
    assert "TRIGGER_MISMATCH" in codes(check_email_draft(s, bad, PACKETS["exec_move"], cfg), ERROR)


def test_email_unsupported_number_and_name(cfg):
    s, b = good_email()
    c = codes(check_email_draft(s, b.replace("Who is the best", "Do you have 15 minutes with Zephyr Corp? Who is the best"), PACKETS["exec_move"], cfg), ERROR)
    assert {"NUMBER_UNSUPPORTED", "PROPER_NOUN_UNSUPPORTED"} <= c


def test_email_unsupported_claim(cfg):
    s, b = good_email()
    bad = b.replace("closer look.", "closer look. We help companies like yours increase adoption by 30%.")
    c = codes(check_email_draft(s, bad, PACKETS["exec_move"], cfg), ERROR)
    assert {"UNSUPPORTED_CLAIM", "NUMBER_UNSUPPORTED"} <= c


def test_email_ambiguous_role(cfg):
    p = pk("ambiguous_role")
    s, b = "tried you re Greenvale", "Hi Tane,\nJust tried you. Greenvale Dairy is hiring a Head of Digital.\nAs the Product Manager you may know who owns it.\nWho handles that there?\nAlex"
    assert "ROLE_ASSUMED" in codes(check_email_draft(s, b, p, cfg), ERROR)


def test_email_must_not_claim(cfg):
    s, b = good_email()
    assert "MUST_NOT_CLAIM" in codes(check_email_draft(s, b.replace("closer look.", "closer look as an existing customer."), PACKETS["exec_move"], cfg), ERROR)


def test_email_missing_signoff_is_an_error(cfg):
    s, b = good_email()
    assert "EMAIL_SIGNOFF" in codes(check_email_draft(s, b.rsplit("\n", 1)[0], PACKETS["exec_move"], cfg), ERROR)
    assert "EMAIL_SIGNOFF" in codes(check_email_draft(s, b.replace("\nAlex", "\nCheers, Alex"), PACKETS["exec_move"], cfg), ERROR)


def test_email_empty(cfg):
    assert "EMAIL_EMPTY" in codes(check_email_draft("", "", PACKETS["exec_move"], cfg), ERROR)


def test_email_label_missing_when_uncalibrated(real_cfg):
    s, b = good_email()
    assert "DRAFT_STYLE_LABEL_MISSING" in codes(check_email_draft(s, b, PACKETS["exec_move"], real_cfg), ERROR)


# ---- tolerance of the packet shape --------------------------------------------------------------------------

def test_extra_and_missing_keys_tolerated(cfg):
    p, o = _larkspur()
    p["something_new"] = {"a": 1}
    for k in ("website", "location", "employees", "flags", "must_not_claim"):
        p.pop(k)
    assert check_opener_block(o, p, cfg) == []


def test_alternate_key_names(cfg):
    p, o = _larkspur()
    p["name"] = p.pop("account_name")
    p["prospect"] = {"first_name": p.pop("prospect_first_name"), "title": p.pop("prospect_title")}
    n = normalize_packet(p)
    assert n.account_name == "Larkspur Insurance" and n.first_name == "Priya" and n.title == "Head of Product"
    assert check_opener_block(o, p, cfg) == []


def test_enum_kind_and_datetime_dates(cfg):
    from datetime import datetime, timezone
    from src.models import SignalKind
    p, o = _larkspur()
    p["trigger"]["kind"] = SignalKind.EXEC_MOVE
    p["trigger"]["date"] = datetime(2026, 11, 12, tzinfo=timezone.utc)
    assert check_opener_block(o, p, cfg) == []


def test_bare_string_facts_are_ignored():
    n = normalize_packet({"available_facts": ["no source here", {"text": "ok", "source": "s"}]})
    assert len(n.facts) == 1


def test_empty_packet_is_tolerated(cfg):
    assert check_opener_block("", {}, cfg)


@pytest.mark.parametrize("bad_cfg", [None, {}, [], "voice.yaml"])
def test_missing_or_empty_voice_cfg_raises_instead_of_disabling_rules(bad_cfg):
    with pytest.raises(tc.VoiceConfigError):
        check_opener_block("OPENER: Hi?", {}, bad_cfg)
    with pytest.raises(tc.VoiceConfigError):
        check_email_draft("a b", "x", {}, bad_cfg)


# ---- reporting ----------------------------------------------------------------------------------------------

def test_format_violations_ok():
    assert format_violations([]).startswith("OK")


def test_format_violations_orders_errors_first():
    out = format_violations([Violation("B", "warn", WARNING), Violation("A", "err", ERROR)]).splitlines()
    assert out[0].startswith("ERROR A") and out[1].startswith("WARNING B")


def test_format_violations_can_hide_warnings():
    assert format_violations([Violation("B", "w", WARNING)], include_warnings=False).startswith("OK")


def test_violation_to_dict_and_frozen():
    v = Violation("X", "m")
    assert v.to_dict() == {"code": "X", "message": "m", "severity": "error"}
    with pytest.raises(Exception):
        v.code = "Y"  # type: ignore[misc]


def test_checks_are_pure(cfg):
    p, o = _larkspur()
    p0, c0 = copy.deepcopy(p), copy.deepcopy(cfg)
    check_opener_block(o, p, cfg)
    assert p == p0 and cfg == c0


def test_deterministic(cfg):
    p, o = _larkspur()
    bad = swap(o, "Harbourline", "Harbourline") if "Harbourline" in o else o.replace("Larkspur just", "Harbourline just")
    assert check_opener_block(bad, p, cfg) == check_opener_block(bad, p, cfg)


# ---- docs stay in step with code ----------------------------------------------------------------------------

def test_every_code_is_documented():
    doc = (ROOT / "docs/voice_guide.md").read_text()
    for code in LINT_CODES:
        assert f"`{code}`" in doc, code
