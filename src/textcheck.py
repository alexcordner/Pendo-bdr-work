"""Deterministic lint for generated call openers and "just tried you" email drafts.

The LLM writes the text; this module only checks it. Pure functions over strings and dicts, no I/O. The only file
touching code is ``load_voice_config`` and the small CLI (``python -m src.textcheck``). Stdlib + pyyaml only.

Packet dict shape consumed (all keys optional, extras ignored). Written to accept src/callsheet.py's
``ResearchPacket.to_dict()`` as is; the exact mapping is in docs/voice_guide.md.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

ERROR = "error"
WARNING = "warning"

#: Every code this module can emit. docs/voice_guide.md must list each (a test checks).
LINT_CODES: dict[str, str] = {
    "PACKET_INVALID": "the packet is not a mapping",
    "TEXT_INVALID": "the text to check is not a string",
    "UNICODE_SUSPECT": "zero-width character, non-Latin lookalike letter or other text that could dodge the checks",
    "SHAPE_MISSING": "a required line (OPENER / FACT 1 / FACT 2 / GATEKEEPER / TRIGGER) is missing",
    "SHAPE_ORDER": "the required lines are out of order",
    "SHAPE_EXTRA": "extra, duplicate or malformed lines (markdown, commentary, blank lines, quotes)",
    "DRAFT_STYLE_LABEL_MISSING": "voice not calibrated but the DRAFT STYLE label is missing",
    "DRAFT_STYLE_LABEL_STALE": "voice calibrated but the DRAFT STYLE label is still present (warning)",
    "OPENER_TOO_LONG": "opener is over the word limit",
    "OPENER_NO_QUESTION": "opener has no question / ask (warning)",
    "OPENER_MULTIPLE_QUESTIONS": "opener asks more than one thing (one ask only)",
    "OPENER_PENDO_FEATURE": "opener names a Pendo feature (clear phrase)",
    "OPENER_PENDO_FEATURE_SOFT": "opener has a word that may be feature talk (warning, INFERRED list)",
    "ROLE_ASSUMED": "packet flags an ambiguous role but the text assumes the contact's role",
    "BANNED_PHRASE": "banned phrase or hype word",
    "EXCLAMATION": "exclamation mark",
    "EMOJI": "emoji",
    "UNSUPPORTED_CLAIM": "claim about Pendo results, customers, growth or round/verb details the packet cannot support",
    "MUST_NOT_CLAIM": "text contains a phrase on the packet's must-not-claim list",
    "HISTORY_CLAIM": "text implies earlier contact (we spoke, left a voicemail) that the packet cannot support",
    "EMAIL_ADDRESS": "an email address appears in the text",
    "PHONE_NUMBER": "a phone number appears in the text",
    "LINK": "a link or web address appears in the text",
    "FACT_NO_SOURCE": "a FACT line does not end with (source: ...)",
    "FACT_NONE_FORMAT": "'none verified' written in the wrong form",
    "FACT_UNSUPPORTED": "fact text does not match a packet fact closely enough: possibly invented",
    "FACT_SOURCE_UNKNOWN": "cited source is not one of the packet's sources",
    "FACT_SOURCE_MISMATCH": "cited source is not the source of the fact it matches",
    "FACT_DATE_MISSING": "packet fact has a date but the citation does not carry it",
    "FACT_DATE_INVENTED": "citation carries a date the packet fact does not have",
    "FACT_DUPLICATE": "FACT 1 and FACT 2 are the same packet fact: padding",
    "FACT_SKIPPED": "'none verified' written although the packet has usable facts (warning)",
    "NUMBER_UNSUPPORTED": "a number, number word or date in the text is not in the packet",
    "PROPER_NOUN_UNSUPPORTED": "a name (company, person, place, title) in the text is not in the packet",
    "INVENTED_TRIGGER": "text claims or reacts to an event but the packet has no real trigger",
    "TRIGGER_MISMATCH": "an event word in the text is not in the packet's trigger or facts",
    "TRIGGER_LINE_CLAIMED": "TRIGGER line claims a trigger but the packet has none",
    "TRIGGER_LINE_OMITTED": "packet has a trigger but the TRIGGER line says none (warning)",
    "TRIGGER_LINE_UNSUPPORTED": "TRIGGER line does not match the packet trigger evidence",
    "TRIGGER_SOURCE_MISMATCH": "TRIGGER line cites a source other than the packet trigger's",
    "TRIGGER_DATE_MISSING": "packet trigger has a date but the TRIGGER line does not carry it",
    "TRIGGER_DATE_INVENTED": "TRIGGER line carries a date the packet trigger does not have",
    "GATEKEEPER_TOO_LONG": "gatekeeper line is over the word limit",
    "GATEKEEPER_PITCH": "gatekeeper line pitches or mentions the trigger",
    "GATEKEEPER_PITCH_SOFT": "gatekeeper line has a word that may be pitch (warning, INFERRED list)",
    "GATEKEEPER_NO_ROLE": "gatekeeper line does not ask for a role or person",
    "RELATIVE_TIME_UNVERIFIED": "'last month' style phrase the packet has no age_days to check (warning)",
    "SIMILAR_TO_DISLIKED": "near-duplicate of an opener Alex dislikes (warning)",
    "EMAIL_SUBJECT_LENGTH": "subject is not one line of the allowed word count",
    "EMAIL_SUBJECT_CAPS": "subject is shouting in capitals (warning)",
    "SUBJECT_FAKE_THREAD": "subject starts with Re:/Fwd: to fake a thread",
    "EMAIL_TOO_LONG": "body is over the word limit",
    "EMAIL_EMPTY": "subject or body is empty",
    "EMAIL_LINE_COUNT": "body does not have 3 to 5 content lines",
    "EMAIL_BULLETS": "bullets in the body",
    "EMAIL_OPENING": "first line does not start with the required opening",
    "EMAIL_NO_ASK": "body has no ask",
    "EMAIL_MULTIPLE_ASKS": "body has more than one ask",
    "EMAIL_SEND_LANGUAGE": "send, attach or invite language in any generated text (drafts only)",
    "EMAIL_SIGNOFF": "body does not end with the sign-off",
}

LABELS = ("OPENER", "FACT 1", "FACT 2", "GATEKEEPER", "TRIGGER")
NO_TRIGGER_LINE = "None found: cold call on ICP fit only"
FACT_NONE_LINE = "none verified"


class VoiceConfigError(ValueError):
    """voice.yaml is missing, malformed, or has an unknown key. Checks never run on a bad config (fail closed)."""


@dataclass(frozen=True)
class Violation:
    code: str
    message: str
    severity: str = ERROR

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "severity": self.severity}


def has_errors(violations: list[Violation]) -> bool:
    return any(v.severity == ERROR for v in violations)


def format_violations(violations: list[Violation], include_warnings: bool = True) -> str:
    """Plain text, one line per violation, errors first. ``OK`` when there is nothing to report."""
    rows = [v for v in violations if include_warnings or v.severity == ERROR]
    if not rows:
        return "OK: no violations"
    rows = sorted(rows, key=lambda v: (v.severity != ERROR, v.code))
    return "\n".join(f"{v.severity.upper()} {v.code}: {v.message}" for v in rows)


# ----------------------------------------------------------------------------------------------------- config

_LIST = "list[str]"
_RX = "list[regex]"
#: Required schema. Every key must be present, nothing else is allowed (a typo'd key raises).
SCHEMA: dict[str, Any] = {
    "calibrated": "bool",
    "draft_style_label": "str",
    "alex_examples": {"liked_openers": _LIST, "disliked_openers": _LIST, "proud_emails": _LIST},
    "opener": {"max_words": "int", "max_questions": "int", "ask_cues": _RX},
    "gatekeeper": {"max_words": "int", "role_cues": _LIST, "pitch_patterns": _RX, "soft_pitch_words": _LIST},
    "email": {"subject_min_words": "int", "subject_max_words": "int", "body_max_words": "int",
              "content_lines_min": "int", "content_lines_max": "int", "opening": "str", "signoff": "str",
              "allow_links": "bool", "ask_cues": _RX},
    "banned_phrases": _LIST, "banned_phrases_email_only": _LIST,
    "pendo_feature_phrases": _LIST, "pendo_feature_soft_words": _LIST,
    "unsupported_claim_patterns": _RX, "trigger_reaction_patterns": _RX, "trigger_event_words": _LIST,
    "round_patterns": _RX, "event_synonym_groups": _LIST, "unsupported_unless_in_packet": _LIST, "role_assumption_patterns": _RX, "send_language_patterns": _RX,
    "history_claim_patterns": _RX, "allowed_proper_nouns": _LIST, "allowed_acronyms": _LIST,
    "title_acronyms": "map", "number_word_exempt_units": _LIST, "company_suffixes": _LIST,
    "fact_overlap_min": "float", "trigger_overlap_min": "float", "disliked_similarity_min": "float",
}


def _validate(cfg: Any, schema: Mapping[str, Any], path: str = "") -> None:
    if not isinstance(cfg, Mapping):
        raise VoiceConfigError(f"voice config{(' ' + path) if path else ''} must be a mapping, got {type(cfg).__name__}")
    unknown = sorted(set(cfg) - set(schema))
    if unknown:
        raise VoiceConfigError(f"unknown voice config key(s) {unknown} at '{path or 'top level'}'")
    for key, spec in schema.items():
        where = f"{path}.{key}" if path else key
        if key not in cfg:
            raise VoiceConfigError(f"missing voice config key '{where}'")
        val = cfg[key]
        if isinstance(spec, dict):
            _validate(val, spec, where)
        elif spec == "bool":
            if not isinstance(val, bool):
                raise VoiceConfigError(f"'{where}' must be a real boolean (true/false), got {val!r}")
        elif spec == "int":
            if isinstance(val, bool) or not isinstance(val, int) or val < 0:
                raise VoiceConfigError(f"'{where}' must be a non-negative integer, got {val!r}")
        elif spec == "float":
            if isinstance(val, bool) or not isinstance(val, (int, float)) or not 0 <= val <= 1:
                raise VoiceConfigError(f"'{where}' must be a number between 0 and 1, got {val!r}")
        elif spec == "str":
            if not isinstance(val, str) or not val.strip():
                raise VoiceConfigError(f"'{where}' must be a non-empty string, got {val!r}")
        elif spec == "map":
            if not isinstance(val, Mapping) or not all(isinstance(k, str) and isinstance(v, str) for k, v in val.items()):
                raise VoiceConfigError(f"'{where}' must be a mapping of string to string")
        else:  # list[str] / list[regex]
            if not isinstance(val, list) or not all(isinstance(x, str) and x.strip() for x in val):
                raise VoiceConfigError(f"'{where}' must be a list of non-empty strings, got {val!r}")
            if spec == _RX:
                for rx in val:
                    try:
                        re.compile(rx)
                    except re.error as exc:
                        raise VoiceConfigError(f"'{where}' has an invalid regex {rx!r}: {exc}") from exc


def validate_voice_config(cfg: Any) -> dict[str, Any]:
    """Raise VoiceConfigError unless ``cfg`` has exactly the required keys with the right types."""
    _validate(cfg, SCHEMA)
    return dict(cfg)


def load_voice_config(path: str | Path = "config/voice.yaml") -> dict[str, Any]:
    """Read and validate the YAML file (I/O helper, kept apart from the pure checks)."""
    import yaml
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return validate_voice_config(raw)


# ----------------------------------------------------------------------------------------------------- normalisation

_CONFUSABLE = {
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y",
    "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "ɡ": "g", "ο": "o", "ν": "v",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O",
    "Р": "P", "С": "C", "Т": "T", "Х": "X", "Α": "A", "Β": "B", "Ε": "E",
    "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P",
    "Τ": "T", "Χ": "X", "І": "I", "Ј": "J",
}
_QUOTES = {"’": "'", "‘": "'", "‚": "'", "‛": "'", "“": '"', "”": '"',
           "–": "-", "—": "-", "−": "-", "‐": "-", "‑": "-"}


def normalize_text(text: str) -> tuple[str, list[str]]:
    """Return (clean, issues). Clean text is NFKC, with zero-width/format characters removed, spaces unified,
    Cyrillic/Greek lookalikes folded to Latin and smart quotes made plain. ``issues`` lists what looked suspicious
    (zero-width characters, lookalike or other non-Latin letters). NBSP alone is silently fixed (accidental)."""
    issues: list[str] = []
    t = unicodedata.normalize("NFKC", text)
    if any(unicodedata.category(c) == "Cf" for c in t):
        issues.append("zero-width or invisible format character")
    t = "".join(c for c in t if unicodedata.category(c) != "Cf")
    t = "".join(" " if unicodedata.category(c) == "Zs" else c for c in t)
    if any(c in _CONFUSABLE for c in t):
        issues.append("non-Latin lookalike letter (Cyrillic or Greek)")
    t = "".join(_CONFUSABLE.get(c, c) for c in t)
    odd = {c for c in t if c.isalpha() and not unicodedata.name(c, "").startswith("LATIN")}
    if odd:
        issues.append("non-Latin letters: " + "".join(sorted(odd))[:10])
    t = "".join(_QUOTES.get(c, c) for c in t)
    return t, issues


# ----------------------------------------------------------------------------------------------------- packet

_WHOLE = re.compile(r"[A-Za-z0-9]+(?:['&.\-][A-Za-z0-9]+)*")
_PART = re.compile(r"[a-z]+|[0-9]+")
_NUM = re.compile(r"\d+(?:[.,]\d+)*")
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}")
_DATEISH = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4}")
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december"]


@dataclass(frozen=True)
class _Item:
    text: str
    sources: tuple[str, ...]
    date: str
    kind: str  # fact | trigger


@dataclass
class _Packet:
    account_name: str
    first_name: str
    title: str
    trigger_kind: str
    trigger: _Item | None
    facts: list[_Item]
    items: list[_Item]
    ambiguous: bool
    must_not_phrases: list[str]
    contact_values: list[str]
    contact_history: str
    ages: list[int]
    corpus: str

    @property
    def ambiguous_role(self) -> bool:
        return self.ambiguous

    @property
    def sources(self) -> list[str]:
        return [s for it in self.items for s in it.sources]

    @property
    def evidence_text(self) -> str:
        return " \n".join(it.text for it in self.items)


def _s(v: Any) -> str:
    if v is None:
        return ""
    v = getattr(v, "value", v)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return normalize_text(str(v).strip())[0]


def _first(d: Mapping[str, Any], *keys: str) -> Any:
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def _nested(d: Mapping[str, Any], parent: str, key: str) -> Any:
    p = d.get(parent)
    return p.get(key) if isinstance(p, Mapping) else None


_INSTRUCTION = re.compile(r"^\s*(do not|don't|never|must not|avoid)\b", re.I)
_QUOTED = re.compile(r"[\"']([^\"']{3,})[\"']")


def _must_not_phrases(raw: list[Any]) -> list[str]:
    """Literal phrases only. Instruction sentences ("Do not claim ...") are prompt text: their quoted phrases are
    kept, the rest is dead for matching. Short non-instruction entries are literal phrases."""
    out: list[str] = []
    for r in raw:
        t = _s(r)
        if not t:
            continue
        if _INSTRUCTION.match(t) or len(t.split()) > 6:
            out += [q.strip() for q in _QUOTED.findall(t) if q.strip() and not q.strip().lower().startswith("fact ")]
        else:
            out.append(t)
    return out


def normalize_packet(packet: Mapping[str, Any]) -> _Packet:
    """Tolerant reader. See docs/voice_guide.md for the mapping. A fact with no source is unusable and ignored."""
    name = _s(_first(packet, "account_name", "name") or _nested(packet, "account", "name"))
    first = _s(_first(packet, "prospect_first_name", "first_name") or _nested(packet, "prospect", "first_name"))
    title = _s(_first(packet, "prospect_title", "title") or _nested(packet, "prospect", "title"))

    sigs = [s for s in (packet.get("signals") or []) if isinstance(s, Mapping)]
    usable_sigs = [s for s in sigs if _s(s.get("kind")).lower() not in ("", "none", "intent")
                   and _s(s.get("status")).lower() in ("", "ok", "undated")]
    trig_obj: Any
    if "trigger" in packet:
        trig_obj = packet.get("trigger")
    elif packet.get("has_real_trigger") is False:
        trig_obj = None
    else:
        trig_obj = usable_sigs[0] if usable_sigs else None
    kind, trigger = "none", None
    if isinstance(trig_obj, Mapping):
        k = _s(trig_obj.get("kind")).lower() or "none"
        if k not in ("none", "intent"):
            kind = k
            srcs = tuple(x for x in (_s(trig_obj.get("source")), _s(trig_obj.get("source_label"))) if x)
            trigger = _Item(_s(trig_obj.get("evidence") or trig_obj.get("text")), srcs,
                            _s(_first(trig_obj, "date", "source_date")), "trigger")

    facts: list[_Item] = []
    hist_src = _s(packet.get("contact_history_source")).lower()
    for f in packet.get("available_facts") or []:
        if not isinstance(f, Mapping) or not _s(f.get("text")):
            continue
        # The call log is not a research fact: never citable, never used in the opener (2c adds it as a fact).
        if _s(f["text"]).lower().startswith("previous contact") or (hist_src and _s(f.get("source")).lower() == hist_src):
            continue
        srcs = tuple(x for x in (_s(f.get("source")), _s(f.get("source_label"))) if x)
        if srcs:
            facts.append(_Item(_s(f["text"]), srcs, _s(f.get("date")), "fact"))
    items = list(facts)
    if trigger is not None:
        items.append(trigger)
    for s in usable_sigs:   # other usable signals may also be restated, with their own source strings
        srcs = tuple(x for x in (_s(s.get("source")), _s(s.get("source_label"))) if x)
        txt = _s(s.get("evidence") or s.get("text"))
        if srcs and txt and not any(it.text == txt and it.sources == srcs for it in items):
            items.append(_Item(txt, srcs, _s(_first(s, "date", "source_date")), "trigger"))

    ambiguous = any("ambig" in _s(f).lower() and "role" in _s(f).lower()
                    for key in ("ambiguity_flags", "flags") for f in (packet.get(key) or []))
    ambiguous = ambiguous or any(bool(s.get("ambiguous")) for s in sigs) or bool(packet.get("ambiguous"))

    raw_mnc = list(packet.get("must_not_phrases") or []) + list(_first(packet, "must_not_claim", "must_not_invent") or [])
    contact = [_s(packet.get(k)) for k in ("prospect_email", "prospect_phone", "email", "phone") if _s(packet.get(k))]
    ages: list[int] = []
    trig_dicts = [packet.get("trigger")] if isinstance(packet.get("trigger"), Mapping) else []
    for src in [packet] + sigs + trig_dicts:
        a = src.get("age_days")
        if isinstance(a, (int, float)) and not isinstance(a, bool) and a >= 0:
            ages.append(int(a))

    parts = [name, _s(packet.get("website")), _s(packet.get("location")), _s(packet.get("city")),
             _s(packet.get("state")), _s(packet.get("industry")), _s(packet.get("employees")), first, title]
    for it in items:
        parts += [it.text, it.date, *it.sources]
    history = _s(packet.get("contact_history"))
    return _Packet(name, first, title, kind, trigger, facts, items, ambiguous, _must_not_phrases(raw_mnc), contact,
                   history, ages, " \n".join(p for p in parts if p))


# ----------------------------------------------------------------------------------------------------- text tools

_STOP = frozenset("""a an the and or but of to in on at for from by with as is are was were be been it its this that these
those there their they them he she his her we our you your i my me not no so if than then too very just about into over
under up out who whom which what when where while will would can could has have had do does did""".split())

_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL = re.compile(r"(?:https?://|www\.)\S+|\b[\w-]+(?:\.[\w-]+)*\.(?:com|net|org|io|co|au|nz|ai|app|dev|ly)\b|"
                  r"\b(?:bit\.ly|t\.co|goo\.gl)/\S+", re.I)
_PHONE = re.compile(r"(?<![\w$])\+?\(?\d[\d ()\-]{6,}\d(?!\w)")
_SRC_TAIL = re.compile(r"^(.*?\S)\s*\(source:\s*(.+)\)\s*$")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿←-⇿️‍⌀-⏿]|"
                    r"(?<![\w])[:;=]-?[)(DPp](?![\w])|<3")
_DIGIT_WORDS = re.compile(r"(?:\b(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)\b[\s,.\-]*){7,}", re.I)
_SPELLED_LINK = re.compile(r"\b(?:dot|full stop)\s+(?:com|net|org|io|co|au|nz|ai|app)\b|\bat\s+\w+\s+dot\s+\w+", re.I)


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > 4 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _ev_stem(w: str) -> str:
    w = w.lower()
    for suf in ("ing", "ed", "es", "e", "s", "d"):
        if len(w) > 4 and w.endswith(suf):
            w = w[: -len(suf)]
            break
    return w[:4]


def _tokens(s: str) -> list[str]:
    s = _ISO.sub(" ", s.lower())
    return [_stem(w) for w in _WORD.findall(s) if w not in _STOP]


def _containment(a: str, b: str) -> float:
    ta, tb = set(_tokens(a)), set(_tokens(b))
    return len(ta & tb) / len(ta) if ta else 0.0


def _jaccard(a: str, b: str) -> float:
    ta, tb = set(_tokens(a)), set(_tokens(b))
    return len(ta & tb) / len(ta | tb) if ta | tb else 0.0


def _words(s: str) -> int:
    return len(s.split())


def _split_src(content: str) -> tuple[str, str | None]:
    m = _SRC_TAIL.match(content.strip())
    return (m.group(1), m.group(2)) if m else (content.strip(), None)


def _phrase_re(p: str) -> re.Pattern[str]:
    """Phrase match: case-insensitive, space/hyphen insensitive, whole words. A trailing ``*`` on a word allows
    inflected endings (``leverag*`` matches leverage, leveraging)."""
    parts = []
    for w in p.lower().replace("’", "'").replace("-", " ").split():
        if w.endswith("*"):
            parts.append(re.escape(w[:-1]) + r"[a-z]*")
        else:
            parts.append(re.escape(w))
    body = r"[\s\-]+".join(parts)
    return re.compile(r"(?<![a-z0-9])" + body + r"(?![a-z0-9]|'t\b)", re.I)


def _find_phrases(text: str, phrases: list[str]) -> list[str]:
    t = text.lower()
    return [p for p in phrases if _phrase_re(p).search(t)]


def _find_patterns(text: str, patterns: list[str]) -> list[str]:
    out = []
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            out.append(m.group(0))
    return out


def _date_variants(date: str) -> set[str]:
    d = date.strip()
    out = {d.lower()}
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", d)
    if m:
        y, mo, da = int(m.group(1)), int(m.group(2)), int(m.group(3))
        out |= {f"{y:04d}-{mo:02d}-{da:02d}", f"{da} {_MONTHS[mo-1]} {y}", f"{da} {_MONTHS[mo-1][:3]} {y}",
                f"{da:02d}/{mo:02d}/{y}", f"{da}/{mo}/{y}", f"{da:02d}/{mo:02d}/{y % 100:02d}"}
    return out


def _numbers(s: str) -> set[str]:
    s = re.sub(r"\bQ[1-4]\b", " ", s)
    return {n.replace(",", "").lstrip("0") or "0" for n in _NUM.findall(s)}


_UNITS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
          "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
          "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
          "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_MAG = {"hundred": 100, "thousand": 1000, "million": 10**6, "billion": 10**9}
_NUMW = re.compile(r"\b(?:(?:%s)(?:[\s-]+(?:and[\s-]+)?)?)+\b" % "|".join(list(_UNITS) + list(_MAG) + ["dozen"]), re.I)


def _number_words(text: str, exempt_units: list[str]) -> list[tuple[str, set[int]]]:
    """Number-word phrases (two, six hundred, twenty million) with the integer values that would license them:
    the whole value, and the mantissa before the magnitude word. A lone 'one' is ordinary English and is skipped."""
    out: list[tuple[str, set[int]]] = []
    for m in _NUMW.finditer(text):
        phrase = m.group(0).strip()
        words = re.findall(r"[a-z]+", phrase.lower())
        if words == ["one"] or not words:
            continue
        tail = text[m.end():m.end() + 14].lower()
        if any(re.match(r"\s*" + re.escape(u) + r"\b", tail) for u in exempt_units):
            continue
        total, current, mantissa = 0, 0, None
        for w in words:
            if w in _UNITS:
                current += _UNITS[w]
            elif w == "dozen":
                current = max(current, 1) * 12
            elif w in _MAG:
                if mantissa is None:
                    mantissa = max(current, 1)
                current = max(current, 1) * _MAG[w]
                if _MAG[w] >= 1000:
                    total += current
                    current = 0
        total += current
        vals = {total}
        if mantissa is not None:
            vals.add(mantissa)
        out.append((phrase, vals))
    return out


_MAG_CLASS = {"hundred": "h", "thousand": "k", "million": "m", "billion": "b"}
_DIGIT_MAG = re.compile(r"(\d+(?:\.\d+)?)\s?(billion|bn|million|mn|m|thousand|k)\b", re.I)


def _digit_magnitudes(text: str) -> set[tuple[str, str]]:
    cls = {"billion": "b", "bn": "b", "million": "m", "mn": "m", "m": "m", "thousand": "k", "k": "k"}
    return {(n.rstrip("0").rstrip(".") if "." in n else n, cls[m.lower()]) for n, m in _DIGIT_MAG.findall(text)}


def _magnitudes(corpus_l: str) -> set[str]:
    out = {cls for w, cls in _MAG_CLASS.items() if re.search(r"\b%s\b" % w, corpus_l)}
    return out | {m for _, m in _digit_magnitudes(corpus_l)}


def _derived_numbers(pk: _Packet) -> set[str]:
    out: set[str] = set()
    for a in pk.ages:
        out |= {str(a), str(a // 7), str(round(a / 7)), str(math.ceil(a / 7))}
        if a >= 30:
            out |= {str(a // 30), str(round(a / 30))}
    return out


# ----------------------------------------------------------------------------------------------------- shared checks

def _check_surface(text: str, where: str, v: list[Violation]) -> None:
    if "!" in text:
        v.append(Violation("EXCLAMATION", f"{where}: exclamation mark", ERROR))
    if _EMOJI.search(text):
        v.append(Violation("EMOJI", f"{where}: emoji", ERROR))


def _check_contact(text: str, where: str, pk: _Packet, v: list[Violation], allow_links: bool = False) -> None:
    clean = _ISO.sub(" ", text)
    if _EMAIL.search(text):
        v.append(Violation("EMAIL_ADDRESS", f"{where}: contains an email address", ERROR))
    for m in _PHONE.finditer(clean):
        if len(re.sub(r"\D", "", m.group(0))) >= 8:
            v.append(Violation("PHONE_NUMBER", f"{where}: contains a phone number ({m.group(0).strip()})", ERROR))
            break
    if _DIGIT_WORDS.search(text):
        v.append(Violation("PHONE_NUMBER", f"{where}: contains a phone number spelled out in words", ERROR))
    for val in pk.contact_values:
        if val.lower() in text.lower():
            v.append(Violation("EMAIL_ADDRESS" if "@" in val else "PHONE_NUMBER",
                               f"{where}: contains the contact's own detail", ERROR))
    if not allow_links and (_URL.search(_EMAIL.sub(" ", text)) or _SPELLED_LINK.search(text)):
        v.append(Violation("LINK", f"{where}: contains a link or web address", ERROR))


def _check_words(text: str, where: str, cfg: dict[str, Any], v: list[Violation], email: bool = False) -> None:
    banned = list(cfg["banned_phrases"]) + (list(cfg["banned_phrases_email_only"]) if email else [])
    for p in _find_phrases(text, banned):
        v.append(Violation("BANNED_PHRASE", f"{where}: banned phrase '{p}'", ERROR))
    for m in _find_patterns(text, cfg["unsupported_claim_patterns"]):
        v.append(Violation("UNSUPPORTED_CLAIM", f"{where}: unsupported claim '{m}' (nothing in the packet backs it)",
                           ERROR))
    for m in _find_patterns(text, cfg["send_language_patterns"]):
        v.append(Violation("EMAIL_SEND_LANGUAGE", f"{where}: send/attach/invite language ('{m}'); drafts only", ERROR))


_CUR = re.compile(r"(A\$|AU\$|NZ\$|US\$|C\$|\$|\u00a3|\u20ac|AUD|NZD|USD|GBP|EUR)\s?(\d[\d,]*(?:\.\d+)?)", re.I)


def _currencies(text: str) -> set[tuple[str, str]]:
    out = set()
    for c, n in _CUR.findall(text):
        num = n.replace(",", "").rstrip("0").rstrip(".") if "." in n else n.replace(",", "")
        cur = {"AU$": "A$", "AUD": "A$", "NZD": "NZ$", "USD": "US$", "GBP": "\u00a3", "EUR": "\u20ac"}.get(c.upper(), c.upper())
        out.add((cur, num))
    return out


def _check_currency(text: str, where: str, pk: _Packet, v: list[Violation]) -> None:
    """A$18 million is not NZ$18 million: the currency marker must go with the same amount in the packet. A bare $
    is accepted for any currency with that amount."""
    have = _currencies(pk.corpus)
    amounts = {n for _, n in have}
    for cur, num in sorted(_currencies(text)):
        ok = (cur, num) in have or (cur == "$" and num in amounts)
        if not ok and num in amounts:
            v.append(Violation("NUMBER_UNSUPPORTED", f"{where}: amount {cur}{num} does not match the packet's "
                               "currency", ERROR))


#: Relative time phrases and the age (days since the packet's event) each one is consistent with.
_REL_TIME = [(r"\byesterday\b", 1, 2), (r"\bthis week\b", 0, 7), (r"\blast week\b", 4, 14),
             (r"\bthis month\b", 0, 31), (r"\blast month\b", 20, 62), (r"\bthis year\b", 0, 366),
             (r"\blast year\b", 300, 800)]


def _check_relative_time(text: str, where: str, pk: _Packet, v: list[Violation]) -> None:
    """'last month' must fit the packet's age_days. With no age in the packet it cannot be checked: warning."""
    for pat, lo, hi in _REL_TIME:
        m = re.search(pat, text, re.I)
        if not m:
            continue
        if not pk.ages:
            v.append(Violation("RELATIVE_TIME_UNVERIFIED", f"{where}: '{m.group(0)}' cannot be checked: the packet has "
                               "no age_days (use an absolute date)", WARNING))
        elif not any(lo <= a <= hi for a in pk.ages):
            v.append(Violation("UNSUPPORTED_CLAIM", f"{where}: '{m.group(0)}' does not fit the packet's age_days "
                               f"{sorted(set(pk.ages))}", ERROR))


def _check_unless_in_packet(text: str, where: str, pk: _Packet, cfg: dict[str, Any], v: list[Violation]) -> None:
    corpus = pk.corpus.lower()
    for p in cfg["unsupported_unless_in_packet"]:
        m = _phrase_re(p).search(text.lower())
        if m and not _phrase_re(p).search(corpus):
            v.append(Violation("UNSUPPORTED_CLAIM", f"{where}: '{m.group(0)}' is a claim about them that the packet "
                               "does not support", ERROR))


def _check_rounds(text: str, where: str, pk: _Packet, cfg: dict[str, Any], v: list[Violation]) -> None:
    """Funding-round letters and similar details (Series C, seed, IPO) must be in the packet verbatim."""
    corpus = pk.corpus.lower()
    for pat in cfg["round_patterns"]:
        for m in re.finditer(pat, text, re.I):
            got = re.sub(r"\s+", " ", m.group(0).lower().strip())
            if got not in re.sub(r"\s+", " ", corpus):
                v.append(Violation("UNSUPPORTED_CLAIM", f"{where}: '{m.group(0)}' is not in the packet", ERROR))


def _name_tokens(s: str) -> list[re.Match[str]]:
    return list(_WHOLE.finditer(s))


def _corpus_names(corpus: str) -> tuple[set[str], set[str]]:
    c = unicodedata.normalize("NFKC", corpus).lower()
    whole = {re.sub(r"'s$", "", m.group(0)) for m in _WHOLE.finditer(c)}
    parts: set[str] = set()
    for w in _WHOLE.finditer(c):
        parts |= set(_PART.findall(w.group(0)))
    return whole | {re.sub(r"[.]+$", "", w) for w in whole}, parts


_GREET = {"hi", "hello", "hey", "g'day", "gday", "morning", "afternoon", "evening", "dear", "thanks", "thank", "cheers"}


def _check_supported(text: str, where: str, pk: _Packet, cfg: dict[str, Any], v: list[Violation]) -> None:
    """Numbers and names must exist in the packet. Names: a name copied verbatim from the packet always passes;
    mid-sentence capitalised tokens are always checked; a sentence-initial word is checked only when it looks like a
    company (followed by another capitalised word, or a possessive)."""
    exempt = cfg["number_word_exempt_units"]
    have = _numbers(pk.corpus) | _derived_numbers(pk)
    for n in sorted(_numbers(text) - have):
        v.append(Violation("NUMBER_UNSUPPORTED", f"{where}: number/date '{n}' is not in the packet", ERROR))
    corpus_l = pk.corpus.lower()
    corpus_mags = _magnitudes(corpus_l)
    for phrase, vals in _number_words(text, exempt):
        words = re.findall(r"[a-z]+", phrase.lower())
        in_corpus = all(w in corpus_l for w in words if w not in ("and",))
        mags = {_MAG_CLASS[w] for w in words if w in _MAG_CLASS}
        mantissa_ok = bool(mags) and str(min(vals)) in have and mags <= corpus_mags
        if not (in_corpus or str(max(vals)) in have or mantissa_ok):
            v.append(Violation("NUMBER_UNSUPPORTED", f"{where}: number word '{phrase}' is not in the packet", ERROR))
    for num, mag in _digit_magnitudes(text):
        if (num, mag) not in _digit_magnitudes(pk.corpus):
            v.append(Violation("NUMBER_UNSUPPORTED", f"{where}: '{num} {mag}' (amount and magnitude) is not in the "
                               "packet", ERROR))

    whole, parts = _corpus_names(pk.corpus)
    allowed = {w.lower() for w in cfg["allowed_proper_nouns"]} | {"i"}
    acr = {w.lower() for w in cfg["allowed_acronyms"]}
    titles = {k.lower(): v_.lower() for k, v_ in cfg["title_acronyms"].items()}
    suffixes = {w.lower() for w in cfg["company_suffixes"]}
    flagged: list[str] = []

    def known(w: str) -> bool:
        base = re.sub(r"'s$", "", w.lower()).rstrip(".")
        if base in whole:
            return True
        sub = _PART.findall(base)
        return bool(sub) and all(p in parts for p in sub)

    for sent in re.split(r"(?<=[.?!])\s+|\n+", text):
        toks = _name_tokens(sent)
        for i, m in enumerate(toks):
            w = m.group(0)
            if not any(ch.isupper() for ch in w) or len(w) < 2 or not (w[0].isupper() or w[0].isdigit()):
                continue
            base = re.sub(r"'s$", "", w.lower())
            if base in allowed or base.split("'")[0] in allowed:
                continue
            if base in titles:
                if known(base) or titles[base] in corpus_l:
                    continue
            elif base in acr:
                continue
            if known(w):
                continue
            if i == 0:
                nxt = toks[i + 1] if i + 1 < len(toks) else None
                poss = w.lower().endswith("'s")
                company = poss or (nxt is not None and nxt.group(0)[:1].isupper() and nxt.group(0).lower() in suffixes
                                   and sent[m.end():nxt.start()].strip() == "")
                if base in _GREET or base in _STOP or not company:
                    continue
            if base not in flagged:
                flagged.append(base)
    for w in flagged:
        v.append(Violation("PROPER_NOUN_UNSUPPORTED", f"{where}: name '{w}' is not in the packet", ERROR))


_ROLE_PHRASES = [r"\bchief [a-z]+(?: [a-z]+)? officer\b", r"\bchief [a-z]+\b", r"\bhead of [a-z]+(?: [a-z]+)?\b",
                 r"\bdirector of [a-z]+(?: [a-z]+)?\b", r"\b(?:vp|vice president)(?: of)? [a-z]+\b",
                 r"\bgeneral manager\b", r"\bmanaging director\b"]


def _check_titles(text: str, where: str, pk: _Packet, cfg: dict[str, Any], v: list[Violation]) -> None:
    """A job title in the opener or email must be in the packet (appointed a 'chief executive' is not the packet's
    'Chief Digital Officer'). Not applied to gatekeeper lines, which only ask for a role."""
    corpus = re.sub(r"\s+", " ", pk.corpus.lower())
    seen: set[str] = set()
    for pat in _ROLE_PHRASES:
        for m in re.finditer(pat, text, re.I):
            t = re.sub(r"\s+", " ", m.group(0).lower())
            words = t.split()
            if t in seen or any(t in s_ for s_ in seen):
                continue
            seen.add(t)
            if t not in corpus and not (len(words) > 2 and " ".join(words[:2]) in corpus
                                        and re.search(r"\b%s\b" % re.escape(words[2]), corpus)):
                v.append(Violation("TRIGGER_MISMATCH", f"{where}: role '{m.group(0)}' is not in the packet", ERROR))


def _synonyms(word: str, cfg: dict[str, Any]) -> list[str]:
    w = word.lower()
    for g in cfg["event_synonym_groups"]:
        members = [m.strip().lower() for m in g.split(",") if m.strip()]
        if w in members:
            return members
    return [w]


def _event_supported(w: str, ev: set[str], ev_text: str, cfg: dict[str, Any]) -> bool:
    return any(_ev_stem(m.split()[0]) in ev or m in ev_text for m in _synonyms(w, cfg))


def _check_trigger_claims(text: str, where: str, pk: _Packet, cfg: dict[str, Any], v: list[Violation]) -> None:
    """Reactions (saw, congrats) need a real trigger. Event words must appear in some packet fact or signal; with no
    trigger they may only restate a packet fact."""
    reactions = _find_patterns(text, cfg["trigger_reaction_patterns"])
    words = [w for w in cfg["trigger_event_words"] if _phrase_re(w).search(text.lower())]
    if not reactions and not words:
        return
    ev_text = (pk.evidence_text + " " + pk.trigger_kind.replace("_", " ")).lower()
    ev = {_ev_stem(w) for w in re.findall(r"[a-z]+", ev_text)}
    if pk.trigger is None:
        if reactions:
            v.append(Violation("INVENTED_TRIGGER", f"{where}: '{reactions[0]}' reacts to news but the packet has no "
                               "real trigger (cold call on ICP fit only)", ERROR))
            return
        bad = [w for w in words if not _event_supported(w, ev, ev_text, cfg)]
        if bad:
            v.append(Violation("INVENTED_TRIGGER", f"{where}: '{bad[0]}' claims an event but the packet has no "
                               "real trigger", ERROR))
        return
    for w in words:
        if not _event_supported(w, ev, ev_text, cfg):
            v.append(Violation("TRIGGER_MISMATCH", f"{where}: event word '{w}' is not in the packet's trigger or "
                               "facts", ERROR))


def _check_role(text: str, where: str, pk: _Packet, cfg: dict[str, Any], v: list[Violation]) -> None:
    if not pk.ambiguous_role:
        return
    hits = _find_patterns(text, cfg["role_assumption_patterns"])
    if pk.title and _phrase_re(pk.title).search(text.lower()):
        hits.append(pk.title)
    if hits:
        v.append(Violation("ROLE_ASSUMED", f"{where}: packet flags an ambiguous role, but the text assumes it "
                           f"('{hits[0]}')", ERROR))


def _check_history(text: str, where: str, pk: _Packet, cfg: dict[str, Any], v: list[Violation], opener: bool) -> None:
    """Earlier-contact claims (we spoke, left a voicemail) are never opener material; in an email they need the
    packet's contact_history to say so."""
    for m in _find_patterns(text, cfg["history_claim_patterns"]):
        supported = (not opener) and pk.contact_history and any(
            w in pk.contact_history.lower() for w in re.findall(r"[a-z]{5,}", m.lower()))
        if not supported:
            v.append(Violation("HISTORY_CLAIM", f"{where}: '{m}' implies earlier contact the packet cannot support",
                               ERROR))


def _check_must_not(texts: list[tuple[str, str]], pk: _Packet, v: list[Violation]) -> None:
    for where, text in texts:
        for claim in pk.must_not_phrases:
            if claim.lower() in text.lower():
                v.append(Violation("MUST_NOT_CLAIM", f"{where}: contains '{claim}' (on the packet's must-not-claim "
                                   "list)", ERROR))


def _check_label(first_line: str | None, cfg: dict[str, Any], v: list[Violation]) -> bool:
    present = first_line is not None and first_line.strip() == cfg["draft_style_label"]
    if not cfg["calibrated"] and not present:
        v.append(Violation("DRAFT_STYLE_LABEL_MISSING", f"voice is not calibrated: first line must be "
                           f"'{cfg['draft_style_label']}'", ERROR))
    if cfg["calibrated"] and present:
        v.append(Violation("DRAFT_STYLE_LABEL_STALE", "voice is calibrated but the DRAFT STYLE label is present",
                           WARNING))
    return present


_TAG_Q = re.compile(r",\s*(right|yeah|yes|correct|eh|no)\?\s*$", re.I)


def _asks(text: str, cues: list[str]) -> int:
    """Count asks. Per sentence: each question mark is one ask (a short tag like ', right?' is not), plus any cue
    phrase that is not the start of the sentence ("Who owns it, and can I send an invite?" is two). A sentence
    with no question mark counts one per cue phrase it holds ("Let me know."). Heuristic, documented."""
    n = 0
    for sent in re.split(r"(?<=[.?!])\s+|\n+", text):
        sent = sent.strip()
        if not sent:
            continue
        q = sent.count("?")
        if q and _TAG_Q.search(sent):
            q -= 1
            sent = _TAG_Q.sub("", sent)
        cues_at = [m.start() for p in cues for m in re.finditer(p, sent, re.I)]
        if q:
            n += q + sum(1 for a in cues_at if a > 0)
        else:
            n += len(cues_at)
    return n


def _unicode_issue(issues: list[str], v: list[Violation]) -> None:
    for i in issues:
        v.append(Violation("UNICODE_SUSPECT", f"text contains a {i}", ERROR))


def _check_common(text: str, where: str, pk: _Packet, cfg: dict[str, Any], v: list[Violation],
                  email: bool = False) -> None:
    _check_surface(text, where, v)
    _check_words(text, where, cfg, v, email=email)
    _check_rounds(text, where, pk, cfg, v)
    _check_currency(text, where, pk, v)
    _check_relative_time(text, where, pk, v)
    _check_unless_in_packet(text, where, pk, cfg, v)
    _check_supported(text, where, pk, cfg, v)


# ----------------------------------------------------------------------------------------------------- opener block

def parse_block(text: str) -> tuple[dict[str, str], list[str], list[str]]:
    """Returns (sections by label, labels in order found, unexpected lines). The label line is not a section."""
    sections: dict[str, str] = {}
    order: list[str] = []
    extra: list[str] = []
    pat = re.compile(r"^(OPENER|FACT 1|FACT 2|GATEKEEPER|TRIGGER): (\S.*)$")
    for ln in text.rstrip().splitlines():
        m = pat.match(ln.rstrip())
        if m and m.group(1) not in sections:
            sections[m.group(1)] = m.group(2).strip()
            order.append(m.group(1))
        else:
            extra.append(ln)
    return sections, order, extra


def _strip_known(cited: str, item_sources: list[str]) -> str:
    out = cited
    for s in sorted(set(item_sources), key=len, reverse=True):
        out = re.sub(re.escape(s), " ", out, flags=re.I)
    return out


def _check_cite(where: str, text: str, cited: str, pool: list[_Item], pk: _Packet, min_overlap: float,
                v: list[Violation], fact: bool) -> _Item | None:
    """Match ``text`` to the best packet item in ``pool`` and check the citation names one of that item's source
    strings (id OR label) and carries exactly the item's date (none if the item has none)."""
    pre = "FACT" if fact else "TRIGGER"
    scored = [(_containment(text, it.text), it) for it in pool]
    best = max((s for s, _ in scored), default=0.0)
    if best < min_overlap:
        code = "FACT_UNSUPPORTED" if fact else "TRIGGER_LINE_UNSUPPORTED"
        v.append(Violation(code, f"{where}: possibly invented; best overlap with the packet is {best:.0%}" if fact
                           else f"{where}: does not match the packet trigger evidence ({best:.0%})", ERROR))
        return None
    cands = [it for s, it in scored if s >= min_overlap and s >= best - 0.05]
    low = cited.lower()
    hits = [it for it in cands if any(s.lower() in low for s in it.sources)]
    if not hits:
        known = any(s.lower() in low for s in pk.sources)
        code = f"{pre}_SOURCE_MISMATCH" if (known or not fact) else "FACT_SOURCE_UNKNOWN"
        if not fact and not known:
            code = "TRIGGER_SOURCE_MISMATCH"
        v.append(Violation(code, f"{where}: cited '{cited}' is not a source of the matching packet entry "
                           f"(accepted: {', '.join(cands[0].sources)})", ERROR))
        return cands[0]
    resid = _strip_known(cited, [s for it in hits for s in it.sources])
    resid_dates = [d.lower() for d in _DATEISH.findall(resid)]
    ok, saw_dated = False, False
    for it in hits:
        if it.date:
            saw_dated = True
            variants = _date_variants(it.date)
            has_date = any(d in low for d in variants)
            if has_date and all(d in variants for d in resid_dates):
                ok = True
        elif not resid_dates:
            ok = True
    if not ok:
        if saw_dated and not resid_dates:
            v.append(Violation(f"{pre}_DATE_MISSING", f"{where}: citation must carry the packet date "
                               f"{next(i.date for i in hits if i.date)}", ERROR))
        else:
            v.append(Violation(f"{pre}_DATE_INVENTED", f"{where}: citation carries a date the packet does not have "
                               f"({', '.join(resid_dates) or 'none'})", ERROR))
    return hits[0]


def check_opener_block(text: str, packet_dict: Mapping[str, Any], voice_cfg: Mapping[str, Any] | None
                       ) -> list[Violation]:
    """Lint one opener block (OPENER / FACT 1 / FACT 2 / GATEKEEPER / TRIGGER) against its research packet.
    Raises VoiceConfigError when ``voice_cfg`` is missing or invalid (never silently weakens the checks)."""
    cfg = validate_voice_config(voice_cfg)
    v: list[Violation] = []
    if not isinstance(packet_dict, Mapping):
        return [Violation("PACKET_INVALID", "packet must be a mapping", ERROR)]
    if not isinstance(text, str):
        return [Violation("TEXT_INVALID", "text must be a string", ERROR)]
    pk = normalize_packet(packet_dict)
    clean, issues = normalize_text(text)
    _unicode_issue(issues, v)
    lines = clean.rstrip().splitlines()
    first = lines[0] if lines else None
    has_label = _check_label(first, cfg, v)
    body = "\n".join(lines[1:]) if has_label else "\n".join(lines)

    sec, order, extra = parse_block(body)
    for lab in LABELS:
        if lab not in sec:
            v.append(Violation("SHAPE_MISSING", f"missing line '{lab}: ...'", ERROR))
    if order != [lab for lab in LABELS if lab in sec]:
        v.append(Violation("SHAPE_ORDER", f"lines must be in the order {', '.join(LABELS)}", ERROR))
    for ln in extra:
        v.append(Violation("SHAPE_EXTRA", f"unexpected line: '{ln[:60]}'", ERROR))
    for k, c in sec.items():
        if c[:1] in ('"', "'") or c[-1:] == '"':
            v.append(Violation("SHAPE_EXTRA", f"{k}: line is wrapped in quotes", ERROR))

    full = [(k, sec[k]) for k in LABELS if k in sec]
    for k, content in full:
        if k in ("FACT 1", "FACT 2", "TRIGGER"):   # OPENER and GATEKEEPER get the full set via _check_common
            _check_surface(content, k, v)
        _check_contact(_split_src(content)[0], k, pk, v)
    _check_must_not(full, pk, v)

    # OPENER
    if "OPENER" in sec:
        o = sec["OPENER"]
        if _words(o) > cfg["opener"]["max_words"]:
            v.append(Violation("OPENER_TOO_LONG", f"OPENER is {_words(o)} words; limit {cfg['opener']['max_words']}",
                               ERROR))
        n = _asks(o, cfg["opener"]["ask_cues"])
        if n == 0:
            v.append(Violation("OPENER_NO_QUESTION", "OPENER has no question or ask", WARNING))
        elif n > cfg["opener"]["max_questions"]:
            v.append(Violation("OPENER_MULTIPLE_QUESTIONS", f"OPENER asks {n} things; one ask only", ERROR))
        corpus_l = pk.corpus.lower()
        for w in _find_phrases(o, cfg["pendo_feature_phrases"]):
            if w.lower().rstrip("*") not in corpus_l:
                v.append(Violation("OPENER_PENDO_FEATURE", f"OPENER names a Pendo feature ('{w}')", ERROR))
        for w in _find_phrases(o, cfg["pendo_feature_soft_words"]):
            if w.lower().rstrip("*") not in corpus_l:
                v.append(Violation("OPENER_PENDO_FEATURE_SOFT", f"OPENER may be feature talk ('{w}')", WARNING))
        _check_common(o, "OPENER", pk, cfg, v)
        _check_trigger_claims(o, "OPENER", pk, cfg, v)
        _check_titles(o, "OPENER", pk, cfg, v)
        _check_role(o, "OPENER", pk, cfg, v)
        _check_history(o, "OPENER", pk, cfg, v, opener=True)
        for d in cfg["alex_examples"].get("disliked_openers") or []:
            if _jaccard(o, d) >= cfg["disliked_similarity_min"]:
                v.append(Violation("SIMILAR_TO_DISLIKED", "OPENER is close to an opener Alex dislikes", WARNING))
                break

    # FACTS
    matched: list[tuple[_Item, str]] = []
    for k in ("FACT 1", "FACT 2"):
        if k not in sec:
            continue
        content = sec[k]
        if content.lower().startswith("none"):
            if content != FACT_NONE_LINE:
                v.append(Violation("FACT_NONE_FORMAT", f"{k}: write exactly 'none verified'", ERROR))
            elif len(pk.facts) >= (2 if k == "FACT 2" else 1):
                v.append(Violation("FACT_SKIPPED", f"{k}: 'none verified' but the packet has "
                                   f"{len(pk.facts)} usable fact(s)", WARNING))
            continue
        ftext, cited = _split_src(content)
        if cited is None:
            v.append(Violation("FACT_NO_SOURCE", f"{k}: must end with '(source: <source>)' using a packet "
                               "source", ERROR))
            item = None
            best = max((_containment(ftext, it.text) for it in pk.items), default=0.0)
            if best < cfg["fact_overlap_min"]:
                v.append(Violation("FACT_UNSUPPORTED", f"{k}: possibly invented; best overlap with the packet is "
                                   f"{best:.0%}", ERROR))
        else:
            item = _check_cite(k, ftext, cited, pk.items, pk, cfg["fact_overlap_min"], v, fact=True)
        if item is not None:
            if any(item.text == m.text or _containment(ftext, mt) >= 0.9 for m, mt in matched):
                v.append(Violation("FACT_DUPLICATE", f"{k} repeats the same packet fact as the other FACT line "
                                   "(padding)", ERROR))
            matched.append((item, ftext))
        _check_rounds(ftext, k, pk, cfg, v)
        _check_currency(ftext, k, pk, v)
        _check_supported(ftext, k, pk, cfg, v)
        for m in _find_patterns(ftext, cfg["unsupported_claim_patterns"]):
            v.append(Violation("UNSUPPORTED_CLAIM", f"{k}: unsupported claim '{m}'", ERROR))

    # GATEKEEPER
    if "GATEKEEPER" in sec:
        g = sec["GATEKEEPER"]
        if _words(g) > cfg["gatekeeper"]["max_words"]:
            v.append(Violation("GATEKEEPER_TOO_LONG", f"GATEKEEPER is {_words(g)} words; limit "
                               f"{cfg['gatekeeper']['max_words']}", ERROR))
        for m in _find_patterns(g, cfg["gatekeeper"]["pitch_patterns"]):
            v.append(Violation("GATEKEEPER_PITCH", f"GATEKEEPER sounds like a pitch ('{m}')", ERROR))
        for w in _find_phrases(g, cfg["gatekeeper"]["soft_pitch_words"]):
            v.append(Violation("GATEKEEPER_PITCH_SOFT", f"GATEKEEPER may sound like a pitch ('{w}')", WARNING))
        if cfg["gatekeeper"]["role_cues"] and not _find_phrases(g, cfg["gatekeeper"]["role_cues"]):
            v.append(Violation("GATEKEEPER_NO_ROLE", "GATEKEEPER must ask for a role or person", ERROR))
        _check_common(g, "GATEKEEPER", pk, cfg, v)
        if _find_patterns(g, cfg["trigger_reaction_patterns"]) or _find_phrases(g, cfg["trigger_event_words"]):
            v.append(Violation("GATEKEEPER_PITCH", "GATEKEEPER must not mention the trigger", ERROR))
        _check_history(g, "GATEKEEPER", pk, cfg, v, opener=True)

    # TRIGGER
    if "TRIGGER" in sec:
        t = sec["TRIGGER"]
        none_line = t.strip().lower() == NO_TRIGGER_LINE.lower()
        if pk.trigger is None:
            if not none_line:
                v.append(Violation("TRIGGER_LINE_CLAIMED", f"packet has no real trigger: TRIGGER must be exactly "
                                   f"'{NO_TRIGGER_LINE}'", ERROR))
        elif none_line:
            v.append(Violation("TRIGGER_LINE_OMITTED", "packet has a trigger but TRIGGER says none found", WARNING))
        else:
            ttext, cited = _split_src(t)
            pool = [it for it in pk.items if it.kind == "trigger"]
            if cited is None:
                v.append(Violation("TRIGGER_SOURCE_MISMATCH", "TRIGGER must end with '(source: ...)'", ERROR))
                if max((_containment(ttext, it.text) for it in pool), default=0.0) < cfg["trigger_overlap_min"]:
                    v.append(Violation("TRIGGER_LINE_UNSUPPORTED", "TRIGGER text does not match the packet trigger "
                                       "evidence", ERROR))
            else:
                _check_cite("TRIGGER", ttext, cited, pool, pk, cfg["trigger_overlap_min"], v, fact=False)
            _check_rounds(ttext, "TRIGGER", pk, cfg, v)
            _check_currency(ttext, "TRIGGER", pk, v)
            _check_supported(ttext, "TRIGGER", pk, cfg, v)
    return v


# ----------------------------------------------------------------------------------------------------- email draft

_GREETING = re.compile(r"^(hi|hello|hey|g'day|morning|good (morning|afternoon)|dear)\b[^.?!]*,$", re.I)


def parse_email_block(text: str) -> tuple[str, str]:
    """Split 'SUBJECT: ...' / 'BODY:' output into (subject, body). A leading DRAFT STYLE label stays in the body."""
    lines = text.strip("\n").splitlines()
    label = ""
    if lines and lines[0].startswith("DRAFT STYLE:"):
        label = lines.pop(0)
    subject, body_lines, in_body = "", [], False
    for ln in lines:
        if not in_body and ln.startswith("SUBJECT: "):
            subject = ln[len("SUBJECT: "):].strip()
        elif not in_body and ln.strip() == "BODY:":
            in_body = True
        elif in_body:
            body_lines.append(ln)
    body = "\n".join(([label] if label else []) + body_lines).strip("\n")
    return subject, body


def check_email_draft(subject: str, body: str, packet_dict: Mapping[str, Any],
                      voice_cfg: Mapping[str, Any] | None) -> list[Violation]:
    """Lint a "just tried you" draft. A leading DRAFT STYLE label line in ``body`` is required while uncalibrated
    and is excluded from every other check. Raises VoiceConfigError on a missing or invalid config."""
    cfg = validate_voice_config(voice_cfg)
    em = cfg["email"]
    v: list[Violation] = []
    if not isinstance(packet_dict, Mapping):
        return [Violation("PACKET_INVALID", "packet must be a mapping", ERROR)]
    if not isinstance(subject, str) or not isinstance(body, str):
        return [Violation("TEXT_INVALID", "subject and body must be strings", ERROR)]
    pk = normalize_packet(packet_dict)
    subject, i1 = normalize_text(subject)
    body, i2 = normalize_text(body)
    _unicode_issue(i1 + [i for i in i2 if i not in i1], v)
    subject, body = subject.strip(), body.strip("\n")
    lines = body.splitlines()
    first = lines[0] if lines else None
    if _check_label(first, cfg, v):
        lines = lines[1:]
    lines = [ln.strip() for ln in lines if ln.strip()]
    text = "\n".join(lines)
    if not subject or not text:
        v.append(Violation("EMAIL_EMPTY", "subject and body must both be non-empty", ERROR))
        return v

    n = _words(subject)
    if "\n" in subject or not em["subject_min_words"] <= n <= em["subject_max_words"]:
        v.append(Violation("EMAIL_SUBJECT_LENGTH", f"subject must be one line of {em['subject_min_words']}-"
                           f"{em['subject_max_words']} words (got {n})", ERROR))
    if subject.isupper():
        v.append(Violation("EMAIL_SUBJECT_CAPS", "subject is all capitals", WARNING))
    if re.match(r"^\s*(re|fwd?|fw)\s*:", subject, re.I):
        v.append(Violation("SUBJECT_FAKE_THREAD", "subject starts with Re:/Fwd:, which fakes a thread", ERROR))
    if _words(text) > em["body_max_words"]:
        v.append(Violation("EMAIL_TOO_LONG", f"body is {_words(text)} words; limit {em['body_max_words']}", ERROR))
    if any(re.match(r"^\s*([-*•]|\d+[.)])\s", ln) for ln in lines):
        v.append(Violation("EMAIL_BULLETS", "body has bullets or a numbered list", ERROR))

    content = list(lines)
    if content and _GREETING.match(content[0]):
        content = content[1:]
    if content and content[-1] == em["signoff"]:
        content = content[:-1]
    else:
        v.append(Violation("EMAIL_SIGNOFF", f"body must end with the sign-off line '{em['signoff']}'", ERROR))
    if not em["content_lines_min"] <= len(content) <= em["content_lines_max"]:
        v.append(Violation("EMAIL_LINE_COUNT", f"body has {len(content)} content lines; want "
                           f"{em['content_lines_min']}-{em['content_lines_max']}", ERROR))
    if not content or not content[0].startswith(em["opening"]):
        v.append(Violation("EMAIL_OPENING", f"first line must start with '{em['opening']}'", ERROR))

    asks = _asks("\n".join(content), em["ask_cues"])
    if asks == 0:
        v.append(Violation("EMAIL_NO_ASK", "body has no ask", ERROR))
    elif asks > 1:
        v.append(Violation("EMAIL_MULTIPLE_ASKS", f"body has {asks} asks; one only", ERROR))

    for where, t in (("subject", subject), ("body", text)):
        _check_contact(t, where, pk, v, allow_links=bool(em["allow_links"]))
        _check_common(t, where, pk, cfg, v, email=True)
        _check_trigger_claims(t, where, pk, cfg, v)
        _check_titles(t, where, pk, cfg, v)
        _check_role(t, where, pk, cfg, v)
        _check_history(t, where, pk, cfg, v, opener=False)
    _check_must_not([("subject", subject), ("body", text)], pk, v)
    return v


# ----------------------------------------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    """``python -m src.textcheck opener|email --packet packet.json --text file [--voice config/voice.yaml]``.
    For ``email`` the text file holds ``SUBJECT: ...`` / ``BODY:`` (optionally preceded by the DRAFT STYLE line).
    Prints format_violations. Exit 0 = no errors, 1 = errors, 2 = bad usage or config."""
    ap = argparse.ArgumentParser(prog="python -m src.textcheck", description=main.__doc__)
    ap.add_argument("kind", choices=["opener", "email"])
    ap.add_argument("--packet", required=True, help="research packet JSON (ResearchPacket.to_dict())")
    ap.add_argument("--text", required=True, help="file with the generated block ('-' for stdin)")
    ap.add_argument("--voice", default="config/voice.yaml")
    args = ap.parse_args(argv)
    try:
        packet = json.loads(Path(args.packet).read_text(encoding="utf-8"))
        text = sys.stdin.read() if args.text == "-" else Path(args.text).read_text(encoding="utf-8")
        cfg = load_voice_config(args.voice)
    except (OSError, ValueError) as exc:   # includes JSON errors and VoiceConfigError
        print(f"ERROR cannot run the lint: {exc}", file=sys.stderr)
        return 2
    if args.kind == "opener":
        vs = check_opener_block(text, packet, cfg)
    else:
        subject, body = parse_email_block(text)
        vs = check_email_draft(subject, body, packet, cfg)
    print(format_violations(vs))
    return 1 if has_errors(vs) else 0


if __name__ == "__main__":
    sys.exit(main())
