"""Reviewer scratch (2d review). Runs the committed labelled corpus (docs/review_2d_labels.json) through textcheck.
Offline, FAKE data only. Not an assertion suite for the worker's code: it records findings.
Run: python -m pytest tests/test_review_2d_scratch.py -q -s   (prints the tables; writes nothing)."""
from __future__ import annotations
import json
from collections import Counter, defaultdict
from pathlib import Path

import pytest
from src import textcheck as tc

ROOT = Path(__file__).resolve().parent.parent
LABELS = json.loads((ROOT / "docs" / "review_2d_labels.json").read_text(encoding="utf-8"))
PACKETS = json.loads((ROOT / "tests" / "fixtures" / "text_packets.json").read_text(encoding="utf-8"))
PACKETS.pop("_comment", None)
PACKETS.update(LABELS["extra_packets"])
CFG = tc.load_voice_config(ROOT / "config" / "voice.yaml")


def lint(case, cfg=CFG):
    pk = PACKETS[case["packet"]]
    if case["kind"] == "opener":
        return tc.check_opener_block(case["text"], pk, cfg)
    return tc.check_email_draft(case["subject"], case["body"], pk, cfg)


def classify(case, cfg=CFG):
    v = lint(case, cfg)
    errs = [x.code for x in v if x.severity == tc.ERROR]
    warns = [x.code for x in v if x.severity == tc.WARNING]
    return errs, warns


def run(cases, cfg=CFG):
    rows = []
    for c in cases:
        errs, warns = classify(c, cfg)
        rows.append(dict(c, errs=errs, warns=warns, flagged=bool(errs)))
    return rows


def report(rows, title):
    bad = [r for r in rows if r["expect"] == "fail"]
    good = [r for r in rows if r["expect"] == "pass"]
    caught = [r for r in bad if r["flagged"]]
    warn_only = [r for r in bad if not r["flagged"] and r["warns"]]
    miss = [r for r in bad if not r["flagged"]]
    fp = [r for r in good if r["flagged"]]
    print(f"\n=== {title}: bad={len(bad)} caught(error)={len(caught)} ({len(caught)/max(1,len(bad)):.0%}) "
          f"warning-only={len(warn_only)} missed-total={len(miss)} | good={len(good)} false-pos={len(fp)} "
          f"({len(fp)/max(1,len(good)):.0%})")
    # caught for the INTENDED reason?
    wrong = [r for r in caught if r["intended_code"] and not (set(r["intended_code"].split("|")) & set(r["errs"]))]
    print(f"caught but not by the intended code: {len(wrong)}")
    for r in wrong:
        print(f"  WRONG-REASON {r['id']} {r['category']}: intended {r['intended_code']} got {r['errs']}")
    for r in miss:
        print(f"  MISS {r['id']} {r['category']}: intended {r['intended_code']} warns={r['warns']}")
    for r in fp:
        print(f"  FP   {r['id']} {r['category']}: {r['errs']}")
    return dict(bad=len(bad), caught=len(caught), warn_only=len(warn_only), miss=len(miss), good=len(good), fp=len(fp))


def test_corpus_report(capsys):
    with capsys.disabled():
        o = report(run(LABELS["opener_cases"]), "OPENER corpus")
        e = report(run(LABELS["email_cases"]), "EMAIL corpus")
        n = report(run(LABELS["natural_openers"]), "NATURAL openers (item 3)")
        fp = [r for r in run(LABELS["natural_openers"]) if r["flagged"]]
        c = Counter(code for r in fp for code in r["errs"])
        print("natural FP codes:", dict(c))


# ======================================================================================================== prototype fixes
# Reviewer's PROTOTYPE of recommended changes, layered on top of the worker's code WITHOUT editing it, to measure how
# much each would buy. Not production code.
import copy
import copy as _copy
import re as _re
import unicodedata as _ud

_CONFUSABLE = {"а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y", "і": "i", "ј": "j", "ѕ": "s",
               "ԁ": "d", "ɡ": "g", "ο": "o", "ν": "v"}
_ZW = _re.compile("[​-‏‪-‮⁠-⁤﻿­]")
_SMART_OK = set("’‘“”–—…£€")


def _prep(text: str) -> tuple[str, list[str]]:
    """F1: NFKC, strip zero-width, NBSP->space, fold Cyrillic/Greek lookalikes; report suspicious characters."""
    issues = []
    if _ZW.search(text):
        issues.append("zero-width/format character")
    t = _ZW.sub("", text).replace(" ", " ")
    t = _ud.normalize("NFKC", t)
    if any(ch in _CONFUSABLE for ch in t):
        issues.append("non-Latin lookalike letter")
    t = "".join(_CONFUSABLE.get(ch, ch) for ch in t)
    return t, issues


_NUMW = _re.compile(r"\b(two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
                    r"seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|"
                    r"thousand|million|billion|dozen)\b(?!\s+(?:seconds?|minutes?|mins?|questions?)\b)", _re.I)

CFG_ADD = {
    "banned_phrases": ["cutting edge", "best in class", "revolutionary", "amazing", "unlocking", "unlocks", "checking in",
                       "touching base", "reach out", "reached out", "circling back", "hope you", "following up",
                       "next-generation", "next generation", "supercharging"],
    "unsupported_claim_patterns": [
        r"\b(customers|teams|companies|clients) (using|on|with) pendo\b", r"\bpendo (customers|clients|users)\b",
        r"\b(see|get|gain|achieve|enjoy)\b[^.?!]{0,30}\b(higher|more|fewer|less|faster|better|dramatically|half)\b",
        r"\bcut\b[^.?!]{0,40}\bhalf\b", r"\b(more|fewer|less|higher|lower) (adoption|tickets|churn)\b"],
    "pendo_feature_words": ["recordings", "checklist", "checklists", "onboarding flows", "user sessions", "drop off",
                            "drop-off", "segmentation"],
    "email_send_language_patterns": [
        r"\b(sent|sending|send|attach\w*|forward\w*)\b[^.?!]{0,30}\b(you|over|invite|calendar|now)\b",
        r"\bi(?:'ve| have|'ll| will| am|'m)\s+(?:sent|send|sending|emailing|emailed)\b"],
    "role_assumption_patterns": [r"\byou (look after|handle|care about|are responsible|will care|must care)\b",
                                 r"\bresponsible for\b"],
    "trigger_event_words": ["promotion", "shipped", "went live", "rolling out", "rolled out", "expanding", "expands"],
}


def proto_cfg(cfg=CFG):
    c = _copy.deepcopy(cfg)
    for k, add in CFG_ADD.items():
        c[k] = list(c.get(k, [])) + add
    # F7: gatekeeper pitch words that are ordinary receptionist English
    c["gatekeeper"]["pitch_words"] = [w for w in c["gatekeeper"]["pitch_words"] if w not in ("help", "free", "our", "we", "save")]
    c["pendo_feature_words"] = [w for w in c["pendo_feature_words"] if w not in ("guide", "guides")]
    return c


_orig_supported = tc._check_supported


def _supported_v2(text, where, pk, cfg, v):
    """F3: sentence-initial words are name-checked only when followed by another capitalised word (company-like) or a
    possessive; mid-sentence capitalised words are always checked. F4: number words checked. Abbreviations ALL-CAPS 2-4
    letters are allowed when not a company token (CX, AI, WA, SaaS ...)."""
    text = tc._norm_apos(text)
    have = tc._numbers(pk.corpus)
    for n in sorted(tc._numbers(text) - have):
        v.append(tc.Violation("NUMBER_UNSUPPORTED", f"{where}: number/date '{n}' is not in the packet", tc.ERROR))
    corpus_l = pk.corpus.lower()
    for m in _NUMW.finditer(text):
        if m.group(1).lower() not in corpus_l:
            v.append(tc.Violation("NUMBER_UNSUPPORTED", f"{where}: number word '{m.group(1)}' is not in the packet", tc.ERROR))
    corpus_words = set(tc._WORD.findall(tc._norm_apos(pk.corpus).lower()))
    corpus_words |= {w.split("'")[0] for w in corpus_words}
    allowed = {w.lower() for w in cfg["allowed_proper_nouns"]} | {"i", "cx", "ai", "wa", "nsw", "qld", "vic", "sa", "tas", "nt", "act", "ceo", "cto", "cio", "cfo", "saas", "ux", "it", "hr", "nov", "dec", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct"}
    flagged = []
    for sent in _re.split(r"(?<=[.?!])\s+|\n+", text):
        toks = list(_re.finditer(r"[A-Za-z][A-Za-z'&-]*", sent))
        for i, m in enumerate(toks):
            w = m.group(0).strip("'-&")
            if len(w) < 2 or not w[0].isupper():
                continue
            base = _re.sub(r"'s$", "", w.lower())
            if i == 0:
                if w.lower() in {x.lower() for x in cfg["sentence_start_words"]}:
                    continue
                nxt = toks[i + 1].group(0) if i + 1 < len(toks) else ""
                companyish = (nxt[:1].isupper() and sent[m.end():m.end() + 1] == " ") or w.lower().endswith("'s")
                if not companyish:
                    continue
            if base in allowed or base.split("'")[0] in allowed or base in corpus_words:
                continue
            if base not in flagged:
                flagged.append(base)
    for w in flagged:
        v.append(tc.Violation("PROPER_NOUN_UNSUPPORTED", f"{where}: name '{w}' is not in the packet", tc.ERROR))


_orig_trig = tc._check_trigger_claims


def _trig_v2(text, where, pk, cfg, v):
    """F2: event words may come from ANY packet item (trigger or fact), by 4-letter stem; with no trigger any event
    word still errors."""
    reactions = tc._find_patterns(text, cfg["trigger_reaction_patterns"])
    words = [w for w in cfg["trigger_event_words"] if tc._phrase_re(w).search(tc._norm_apos(text).lower())]
    if not reactions and not words:
        return False
    if pk.trigger is None:
        v.append(tc.Violation("INVENTED_TRIGGER", f"{where}: claims an event but packet has no trigger", tc.ERROR))
        return True
    ev = {w[:4] for it in pk.items for w in tc._WORD.findall(tc._norm_apos(it.text).lower())}
    for w in words:
        if w.lower()[:4] not in ev:
            v.append(tc.Violation("TRIGGER_MISMATCH", f"{where}: event word '{w}' not in packet evidence", tc.ERROR))
    return True


def proto_lint(case):
    cfg = proto_cfg()
    pk = PACKETS[case["packet"]]
    texts = [case["text"]] if case["kind"] == "opener" else [case["subject"], case["body"]]
    extra = []
    clean = []
    for t in texts:
        c, issues = _prep(t)
        clean.append(c)
        extra += [tc.Violation("UNICODE_SUSPECT", i, tc.ERROR) for i in issues]
    # F5 quotes around a line / bullets / fake thread subjects
    if case["kind"] == "opener":
        for ln in clean[0].splitlines():
            if _re.match(r"^(OPENER|GATEKEEPER|FACT [12]|TRIGGER): [\"“']", ln):
                extra.append(tc.Violation("SHAPE_EXTRA", "line wrapped in quotes", tc.ERROR))
    else:
        if _re.match(r"^(re|fwd?):", clean[0].strip(), _re.I):
            extra.append(tc.Violation("EMAIL_SUBJECT_FAKE_THREAD", "subject fakes a thread", tc.ERROR))
        if any(_re.match(r"^\s*[-*•]\s", ln) for ln in clean[1].splitlines()):
            extra.append(tc.Violation("EMAIL_BULLETS", "bullets in body", tc.ERROR))
    tc._check_supported, tc._check_trigger_claims = _supported_v2, _trig_v2
    try:
        if case["kind"] == "opener":
            v = tc.check_opener_block(clean[0], pk, cfg)
            # F6: opener/gatekeeper send language + multi-question to error + ask cues
            for k, sec in tc.parse_block(clean[0].split("\n", 1)[1] if clean[0].startswith("DRAFT STYLE") else clean[0])[0].items():
                if k in ("OPENER", "GATEKEEPER"):
                    for m in tc._find_patterns(sec, cfg["email_send_language_patterns"]):
                        extra.append(tc.Violation("SEND_LANGUAGE", f"{k}: '{m}'", tc.ERROR))
                if k == "OPENER":
                    asks = sec.count("?") + len(_re.findall(r"\b(can i|could i|may i|mind if|is it fine|are you free|do you have a (minute|second))\b", sec, _re.I))
                    if asks > 1:
                        extra.append(tc.Violation("OPENER_MULTIPLE_ASKS", "more than one ask", tc.ERROR))
            v = [x for x in v if x.code != "OPENER_MULTIPLE_QUESTIONS"]
        else:
            v = tc.check_email_draft(clean[0], clean[1], pk, cfg)
    finally:
        tc._check_supported, tc._check_trigger_claims = _orig_supported, _orig_trig
    return v + extra


def run_proto(cases):
    rows = []
    for c in cases:
        v = proto_lint(c)
        errs = [x.code for x in v if x.severity == tc.ERROR]
        rows.append(dict(c, errs=errs, warns=[x.code for x in v if x.severity == tc.WARNING], flagged=bool(errs)))
    return rows


@pytest.mark.skip(reason='round-1 prototype used the pre-round-2 voice.yaml schema; superseded by the worker fixes')
def test_proto_report(capsys):
    with capsys.disabled():
        print("\n######## PROTOTYPE (reviewer's recommended changes) ########")
        report(run_proto(LABELS["opener_cases"]), "PROTO opener")
        report(run_proto(LABELS["email_cases"]), "PROTO email")
        report(run_proto(LABELS["natural_openers"]), "PROTO natural")


# ======================================================================================================== findings as xfail
# Each test asserts the DESIRED behaviour and is xfail against the worker's commit 7b98a97. If the worker fixes the
# finding, the test XPASSes (non-strict) and the marker can be removed. IDs match docs/review_2d.md.
LBL = LABELS["label_line"]
DEF = LABELS["default_lines"]


def _blk(pk, opener, f1=None, f2=None, gk=None, tr=None, label=True):
    d = DEF[pk]
    return "\n".join(([LBL] if label else []) + ["OPENER: " + opener, "FACT 1: " + (f1 or d["f1"]),
                                                  "FACT 2: " + (f2 or d["f2"]), "GATEKEEPER: " + (gk or d["gk"]),
                                                  "TRIGGER: " + (tr or d["tr"])])


def _errs(text, pk, cfg=CFG):
    return [x.code for x in tc.check_opener_block(text, pk, cfg) if x.severity == tc.ERROR]


X = lambda f: f          # round 2: these findings are FIXED; they are now plain regression tests
NOTFIXED = pytest.mark.xfail(strict=False)   # still-open findings from the held-out round


@X
def test_F01_hyphenated_company_name_in_verbatim_fact_must_pass():
    ev = "Smith-Jones Partners raised A$5 million in a Series A round."
    pk = {"account_name": "Smith-Jones Partners", "employees": 40, "prospect_first_name": "Sam",
          "trigger": {"kind": "funding", "evidence": ev, "source": "zoominfo_scoop:FAKE-9", "date": "2026-11-03"},
          "available_facts": [{"text": ev[:-1], "source": "zoominfo_scoop:FAKE-9", "date": "2026-11-03"}]}
    src = "(source: zoominfo_scoop:FAKE-9, 2026-11-03)"
    txt = "\n".join([LBL, "OPENER: Hi Sam, Alex from Pendo. Who owns product there?", f"FACT 1: {ev[:-1]} {src}",
                     "FACT 2: none verified", "GATEKEEPER: Who looks after product, please?", f"TRIGGER: {ev[:-1]} {src}"])
    assert _errs(txt, pk) == []


@X
def test_F02_real_ambiguity_flags_key_must_enable_role_rule():
    pk = copy.deepcopy(PACKETS["ambiguous_role"])
    pk["ambiguity_flags"] = pk.pop("flags")           # the key 2c actually emits
    txt = _blk("ambiguous_role", "Hi Tane, Alex from Pendo. Your team must be busy. Who owns the portal?")
    assert "ROLE_ASSUMED" in _errs(txt, pk)


@X
def test_F03_same_scoop_cited_consistently_must_pass_against_real_packet():
    real = json.loads((ROOT / "docs" / "review_2d_real_packets.json").read_text())["packets"]["Alpha Digital Pty Ltd"] if (
        ROOT / "docs" / "review_2d_real_packets.json").exists() else None
    if real is None:
        pytest.skip("real 2c packet fixture not committed")
    t = "Alpha Digital Pty Ltd has a new digital product lead."
    lab = "ZoomInfo scoop 2026-11-22"
    txt = "\n".join([LBL, "OPENER: Hi Pat, Alex from Pendo. Alpha Digital has a new digital product lead. Who owns the app?",
                     f"FACT 1: {t} (source: {lab}, 2026-11-22)", "FACT 2: none verified",
                     "GATEKEEPER: Who looks after product there, please?", f"TRIGGER: {t} (source: {lab}, 2026-11-22)"])
    assert [c for c in _errs(txt, real) if c.endswith("SOURCE_MISMATCH")] == []


@X
def test_F04_missing_or_empty_config_must_not_silently_disable_checks():
    txt = _blk("exec_move", "Hi Priya, Alex from Pendo. Our game-changing tool helps. Who owns the app?")
    try:
        errs = _errs(txt, PACKETS["exec_move"], {})
    except Exception:
        return                                         # raising is also acceptable (fail closed)
    assert "BANNED_PHRASE" in errs


@X
@pytest.mark.parametrize("sep", ["\u200b", "\u00a0", "\u00ad"])
def test_F05_invisible_separators_must_not_dodge_banned_phrases(sep):
    txt = _blk("exec_move", f"Hi Priya, Alex from Pendo. Can I grab a quick{sep}call? Who owns the app?")
    assert {"BANNED_PHRASE", "UNICODE_SUSPECT"} & set(_errs(txt, PACKETS["exec_move"]))


@X
def test_F06_send_language_must_be_caught_in_opener_and_gatekeeper():
    txt = _blk("exec_move", "Hi Priya, Alex from Pendo. I've sent you an email. Who owns the app?")
    assert "EMAIL_SEND_LANGUAGE" in _errs(txt, PACKETS["exec_move"])


@X
def test_F07_receptionist_english_is_not_a_pitch():
    txt = _blk("exec_move", "Hi Priya, Alex from Pendo. Who owns the customer app now?", gk="Could you help me find who looks after the app?")
    assert "GATEKEEPER_PITCH" not in _errs(txt, PACKETS["exec_move"])


@X
def test_F08_event_word_from_a_packet_fact_is_not_a_trigger_mismatch():
    txt = _blk("funding", "Hi Jonas, Alex from Pendo. Bellbird is hiring a Product Designer. Who owns product there?")
    assert "TRIGGER_MISMATCH" not in _errs(txt, PACKETS["funding"])


@X
def test_F09_two_asks_in_the_opener_must_be_an_error():
    txt = _blk("exec_move", "Hi Priya, Alex from Pendo. Is now a bad time? Who owns the customer app?")
    assert "OPENER_MULTIPLE_QUESTIONS" in _errs(txt, PACKETS["exec_move"])


@X
def test_F10_number_words_must_be_checked():
    txt = _blk("exec_move", "Hi Priya, Alex from Pendo. Larkspur has about six hundred staff. Who owns the app?")
    assert "NUMBER_UNSUPPORTED" in _errs(txt, PACKETS["exec_move"])


@X
def test_F11_non_boolean_calibrated_must_not_turn_the_label_check_off():
    cfg = dict(CFG, calibrated="false")
    txt = _blk("exec_move", "Hi Priya, Alex from Pendo. Who owns the customer app now?", label=False)
    try:
        errs = _errs(txt, PACKETS["exec_move"], cfg)
    except Exception:
        return
    assert "DRAFT_STYLE_LABEL_MISSING" in errs


@X
@pytest.mark.parametrize("starter", ["Honestly", "Apologies", "Maybe", "Perhaps", "Appreciate", "Understand", "Morning"])
def test_F12_ordinary_sentence_starters_must_not_be_names(starter):
    txt = _blk("exec_move", f"Hi Priya, Alex from Pendo. {starter}, who owns the customer app now?")
    assert "PROPER_NOUN_UNSUPPORTED" not in _errs(txt, PACKETS["exec_move"])


@X
def test_F13_invented_date_in_a_citation_tail_must_be_rejected():
    """2c facts carry NO date (Salesforce facts); a model that guesses one must not get through."""
    pk = copy.deepcopy(PACKETS["exec_move"])
    for f in pk["available_facts"]:
        f.pop("date", None)                              # the real 2c FactItem shape: text + source only
    txt = _blk("exec_move", "Hi Priya, Alex from Pendo. Who owns the customer app now?",
               f2="Larkspur Insurance runs a customer app for policy claims (source: website:larkspur-insurance.example.com/app, 2019-01-01)")
    assert any(c.startswith("FACT_DATE") or c == "FACT_SOURCE_MISMATCH" for c in _errs(txt, pk))


@X
def test_F14_junk_alex_examples_are_rejected_at_validation():
    cfg = copy.deepcopy(CFG); cfg["alex_examples"]["disliked_openers"] = [None, ""]
    with pytest.raises(tc.VoiceConfigError):
        tc.validate_voice_config(cfg)


@X
def test_F15_ordinary_english_guide_is_not_a_pendo_feature():
    txt = _blk("exec_move", "Hi Priya, Alex from Pendo. Who would guide the customer app roadmap now?")
    assert "OPENER_PENDO_FEATURE" not in _errs(txt, PACKETS["exec_move"])


@X
def test_F16_alnum_company_name_must_not_be_split():
    ev = "Zip2Pay launched a new wallet."
    pk = {"account_name": "Zip2Pay", "prospect_first_name": "Sam",
          "trigger": {"kind": "product_launch", "evidence": ev, "source": "zoominfo_scoop:FAKE-9", "date": "2026-11-03"},
          "available_facts": [{"text": ev[:-1], "source": "zoominfo_scoop:FAKE-9", "date": "2026-11-03"}]}
    src = "(source: zoominfo_scoop:FAKE-9, 2026-11-03)"
    txt = "\n".join([LBL, "OPENER: Hi Sam, Alex from Pendo. Who owns the wallet?", f"FACT 1: {ev[:-1]} {src}", "FACT 2: none verified",
                     "GATEKEEPER: Who looks after the wallet, please?", f"TRIGGER: {ev[:-1]} {src}"])
    assert _errs(txt, pk) == []


def test_determinism_and_purity_of_check_functions():
    """PASS evidence: same input, same output; the packet and config dicts are not mutated."""
    case = LABELS["opener_cases"][0]
    pk, cfg = copy.deepcopy(PACKETS[case["packet"]]), copy.deepcopy(CFG)
    a = [x.to_dict() for x in tc.check_opener_block(case["text"], pk, cfg)]
    b = [x.to_dict() for x in tc.check_opener_block(case["text"], pk, cfg)]
    assert a == b and pk == PACKETS[case["packet"]] and cfg == CFG


def test_textcheck_has_no_network_process_or_random_imports():
    import ast
    src = (ROOT / "src" / "textcheck.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imports = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imports |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not ({"os", "subprocess", "socket", "requests", "urllib", "http", "random", "datetime", "time"} & imports)
    calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not ({"exec", "eval", "__import__"} & calls)


# ======================================================================================================== round 2 held-out
HO = json.loads((ROOT / "docs" / "review_2d_heldout2.json").read_text(encoding="utf-8"))
HOP = json.loads((ROOT / "docs" / "review_2d_heldout2_packets.json").read_text(encoding="utf-8"))["packets"]


def ho_rows():
    rows = []
    for c in HO["cases"]:
        pk = HOP[c["packet"]]
        v = (tc.check_opener_block(c["text"], pk, CFG) if c["kind"] == "opener"
             else tc.check_email_draft(c["subject"], c["body"], pk, CFG))
        errs = [x.code for x in v if x.severity == tc.ERROR]
        rows.append(dict(c, errs=errs, flagged=bool(errs)))
    return rows


def test_heldout2_report(capsys):
    """Prints the held-out numbers. Asserts only the floor the reviewer measured (no false positives on the 62 good blocks)."""
    rows = ho_rows()
    bad = [r for r in rows if r["expect"] == "fail"]
    good = [r for r in rows if r["expect"] == "pass"]
    with capsys.disabled():
        print(f"\nHELD-OUT 2: bad caught {sum(r['flagged'] for r in bad)}/{len(bad)}; good false positives "
              f"{sum(r['flagged'] for r in good)}/{len(good)}")
        for r in bad:
            if not r["flagged"]:
                print("  MISS", r["id"], r["category"])
    assert not [r["id"] for r in good if r["flagged"]]


def _ho(pk, opener, gk="Who is the right person for digital there, please?", f1="none verified", f2="none verified", tr="None found: cold call on ICP fit only"):
    return "\n".join([LBL, "OPENER: " + opener, "FACT 1: " + f1, "FACT 2: " + f2, "GATEKEEPER: " + gk, "TRIGGER: " + tr])


@NOTFIXED
def test_N2_stale_signal_restated_as_news_must_be_rejected():
    """gumnut: signal is 60 days old, has_real_trigger False, yet 'is migrating its ecommerce platform' passes."""
    assert _errs(_ho("gumnut", "Hi Olivia, Alex from Pendo. Gumnut is migrating its ecommerce platform. Who owns the online side?"), HOP["gumnut"])


@NOTFIXED
def test_N4_gendered_pronouns_for_a_named_person_must_be_rejected():
    tr = "Ironbark Freight appointed Callum Reyes as Head of Product (source: zoominfo_scoop:FAKE-H-20, 2026-11-11)"
    assert _errs(_ho("ironbark", "Hi Declan, Alex from Pendo. Callum Reyes starts his new role next week. Who handles digital alongside him?", tr=tr), HOP["ironbark"])


@NOTFIXED
def test_N5a_competitor_at_sentence_start_must_be_rejected():
    assert _errs(_ho("lorikeet", "Hi Niamh, Alex from Pendo. Gainsight users often switch. Who owns product?"), HOP["lorikeet"])


@NOTFIXED
def test_N5b_pendo_helps_claim_and_leading_platform_must_be_rejected():
    assert _errs(_ho("lorikeet", "Hi Niamh, Alex from Pendo. Pendo is the leading digital adoption platform. Who owns product?"), HOP["lorikeet"])
    assert _errs(_ho("lorikeet", "Hi Niamh, Alex from Pendo. Pendo helps teams see where users struggle. Who owns product?"), HOP["lorikeet"])


@NOTFIXED
def test_N6_calendar_time_ask_in_opener_must_be_rejected():
    assert _errs(_ho("lorikeet", "Hi Niamh, Alex from Pendo. Would love to get twenty minutes on your calendar. Who owns product?"), HOP["lorikeet"])


@NOTFIXED
def test_N7a_gatekeeper_product_analytics_must_be_an_error_not_a_warning():
    assert _errs(_ho("lorikeet", "Hi Niamh, Alex from Pendo. Who owns product?", gk="Could I speak to whoever looks after your product analytics?"), HOP["lorikeet"])


@NOTFIXED
def test_N7b_gatekeeper_must_not_repeat_the_trigger_by_name():
    tr = "Ironbark Freight appointed Callum Reyes as Head of Product (source: zoominfo_scoop:FAKE-H-20, 2026-11-11)"
    assert _errs(_ho("ironbark", "Hi Declan, Alex from Pendo. Who owns product?", gk="Who looks after digital now that Callum Reyes is Head of Product?", tr=tr), HOP["ironbark"])


@NOTFIXED
def test_N7c_send_them_a_report_in_the_gatekeeper_line_must_be_rejected():
    assert _errs(_ho("lorikeet", "Hi Niamh, Alex from Pendo. Who owns product?", gk="Who is your head of product? I'd like to send them a free report."), HOP["lorikeet"])


@NOTFIXED
def test_N8_two_days_ago_must_not_pass_for_a_nine_day_old_signal():
    tr = "Wattlebird Energy raised A$22 million in a Series C round led by Southgate Capital (source: zoominfo_scoop:FAKE-H-10, 2026-11-16)"
    assert _errs(_ho("wattle", "Hi Imogen, Alex from Pendo. Wattlebird raised A$22 million two days ago. Who owns product?", tr=tr), HOP["wattle"])


@NOTFIXED
def test_N9_invented_lowercase_descriptor_about_the_company_must_be_rejected():
    tr = "Wattlebird Energy raised A$22 million in a Series C round led by Southgate Capital (source: zoominfo_scoop:FAKE-H-10, 2026-11-16)"
    assert _errs(_ho("wattle", "Hi Imogen, Alex from Pendo. Wattlebird, the Perth renewable energy company, raised a Series C. Who owns product?", tr=tr), HOP["wattle"])
