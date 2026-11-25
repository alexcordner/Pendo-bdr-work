"""Deterministic lint for generated call openers and "just tried you" email drafts.

The LLM writes the text; this module only checks it. Pure functions over strings and dicts, no I/O
(``load_voice_config`` is the one small file helper and is kept separate). Stdlib + pyyaml only.

Packet dict shape consumed (all keys optional, extra keys ignored; mapping documented in docs/voice_guide.md):
  account_name | name, website, location, employees, prospect_first_name, prospect_title,
  trigger {kind, evidence, source, date} or None  (or ``signals`` list, strongest used),
  available_facts [{text, source, date}], flags [str], must_not_claim [str].
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

ERROR = "error"
WARNING = "warning"

#: Every code this module can emit, with a one-line meaning. docs/voice_guide.md must list each (a test checks).
LINT_CODES: dict[str, str] = {
    "PACKET_INVALID": "the packet is not a mapping",
    "SHAPE_MISSING": "a required line (OPENER / FACT 1 / FACT 2 / GATEKEEPER / TRIGGER) is missing",
    "SHAPE_ORDER": "the required lines are out of order",
    "SHAPE_EXTRA": "extra, duplicate or malformed lines (markdown, commentary, blank lines)",
    "DRAFT_STYLE_LABEL_MISSING": "voice not calibrated but the DRAFT STYLE label is missing",
    "DRAFT_STYLE_LABEL_STALE": "voice calibrated but the DRAFT STYLE label is still present (warning)",
    "OPENER_TOO_LONG": "opener is over the word limit",
    "OPENER_NO_QUESTION": "opener has no question / ask (warning)",
    "OPENER_MULTIPLE_QUESTIONS": "opener asks more than one thing (warning)",
    "OPENER_PENDO_FEATURE": "opener mentions a Pendo feature",
    "ROLE_ASSUMED": "packet flags an ambiguous role but the text assumes the contact's role",
    "BANNED_PHRASE": "banned phrase or hype word",
    "EXCLAMATION": "exclamation mark",
    "EMOJI": "emoji",
    "UNSUPPORTED_CLAIM": "claim about Pendo results or customers that the packet cannot support",
    "MUST_NOT_CLAIM": "text contains something on the packet's must-not-claim list",
    "EMAIL_ADDRESS": "an email address appears in the text",
    "PHONE_NUMBER": "a phone number appears in the text",
    "LINK": "a link or web address appears in the text",
    "FACT_NO_SOURCE": "a FACT line does not end with (source: ...)",
    "FACT_NONE_FORMAT": "'none verified' written in the wrong form",
    "FACT_UNSUPPORTED": "fact text does not overlap any packet fact: possibly invented",
    "FACT_SOURCE_UNKNOWN": "cited source is not one of the packet's sources",
    "FACT_SOURCE_MISMATCH": "cited source is not the source of the fact it matches",
    "FACT_DATE_MISSING": "cited source line does not carry the packet date",
    "FACT_DUPLICATE": "FACT 1 and FACT 2 are the same packet fact: padding",
    "FACT_SKIPPED": "'none verified' written although the packet has usable facts (warning)",
    "NUMBER_UNSUPPORTED": "a number or date in the text is not in the packet",
    "PROPER_NOUN_UNSUPPORTED": "a name (company, person, place, title) in the text is not in the packet",
    "INVENTED_TRIGGER": "text claims or reacts to an event but the packet has no real trigger",
    "TRIGGER_MISMATCH": "an event word in the text is not in the packet's trigger evidence",
    "TRIGGER_LINE_CLAIMED": "TRIGGER line claims a trigger but the packet has none",
    "TRIGGER_LINE_OMITTED": "packet has a trigger but the TRIGGER line says none (warning)",
    "TRIGGER_LINE_UNSUPPORTED": "TRIGGER line does not match the packet trigger evidence",
    "TRIGGER_SOURCE_MISMATCH": "TRIGGER line cites a source other than the packet trigger's",
    "TRIGGER_DATE_MISSING": "TRIGGER line does not carry the packet trigger date",
    "GATEKEEPER_TOO_LONG": "gatekeeper line is over the word limit",
    "GATEKEEPER_PITCH": "gatekeeper line contains pitch language",
    "GATEKEEPER_NO_ROLE": "gatekeeper line does not ask for a role or person",
    "SIMILAR_TO_DISLIKED": "near-duplicate of an opener Alex dislikes (warning)",
    "EMAIL_SUBJECT_LENGTH": "subject is not one line of the allowed word count",
    "EMAIL_SUBJECT_CAPS": "subject is shouting in capitals (warning)",
    "EMAIL_TOO_LONG": "body is over the word limit",
    "EMAIL_EMPTY": "subject or body is empty",
    "EMAIL_LINE_COUNT": "body does not have 3 to 5 content lines",
    "EMAIL_OPENING": "first line does not start with the required opening",
    "EMAIL_NO_ASK": "body has no ask",
    "EMAIL_MULTIPLE_ASKS": "body has more than one ask",
    "EMAIL_SEND_LANGUAGE": "body contains send language (drafts only)",
    "EMAIL_SIGNOFF": "body does not end with the sign-off (warning)",
}

DEFAULTS: dict[str, Any] = {
    "calibrated": False,
    "draft_style_label": "DRAFT STYLE: not yet calibrated to Alex",
    "alex_examples": {"liked_openers": [], "disliked_openers": [], "proud_emails": []},
    "opener": {"max_words": 25, "max_questions": 1},
    "gatekeeper": {"max_words": 14, "role_cues": [], "pitch_words": []},
    "email": {
        "subject_min_words": 2, "subject_max_words": 5, "body_max_words": 79,
        "content_lines_min": 3, "content_lines_max": 5, "opening": "Just tried you",
        "signoff": "Alex", "allow_links": False, "ask_cues": [],
    },
    "banned_phrases": [], "banned_phrases_email_only": [], "pendo_feature_words": [],
    "unsupported_claim_patterns": [], "trigger_reaction_patterns": [], "trigger_event_words": [],
    "role_assumption_patterns": [], "email_send_language_patterns": [], "allowed_proper_nouns": [],
    "sentence_start_words": [],
    "fact_overlap_min": 0.6, "trigger_overlap_min": 0.5, "disliked_similarity_min": 0.7,
}

LABELS = ("OPENER", "FACT 1", "FACT 2", "GATEKEEPER", "TRIGGER")
NO_TRIGGER_LINE = "None found: cold call on ICP fit only"
FACT_NONE_LINE = "none verified"


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

def load_voice_config(path: str | Path = "config/voice.yaml") -> dict[str, Any]:
    """The only I/O helper: read the YAML file. The check functions take the resulting dict."""
    import yaml
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _cfg(voice_cfg: Mapping[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    voice_cfg = voice_cfg or {}
    for k, default in DEFAULTS.items():
        v = voice_cfg.get(k, default)
        if isinstance(default, dict) and isinstance(v, Mapping):
            merged = dict(default)
            merged.update(v)
            v = merged
        out[k] = v
    return out


# ----------------------------------------------------------------------------------------------------- packet

@dataclass(frozen=True)
class _Item:
    text: str
    source: str
    date: str


@dataclass
class _Packet:
    account_name: str
    first_name: str
    title: str
    trigger_kind: str
    trigger: _Item | None
    facts: list[_Item]
    flags: list[str]
    must_not_claim: list[str]
    contact_values: list[str]
    corpus: str

    @property
    def ambiguous_role(self) -> bool:
        return any("ambig" in f.lower() and "role" in f.lower() for f in self.flags)

    @property
    def sources(self) -> list[str]:
        out = [i.source for i in self.facts if i.source]
        if self.trigger and self.trigger.source:
            out.append(self.trigger.source)
        return out

    @property
    def items(self) -> list[_Item]:
        """Everything a line may be traced to: usable facts plus the trigger evidence."""
        return self.facts + ([self.trigger] if self.trigger else [])


def _s(v: Any) -> str:
    if v is None:
        return ""
    v = getattr(v, "value", v)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v).strip()


def _first(d: Mapping[str, Any], *keys: str) -> Any:
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def _nested(d: Mapping[str, Any], parent: str, key: str) -> Any:
    p = d.get(parent)
    return p.get(key) if isinstance(p, Mapping) else None


def normalize_packet(packet: Mapping[str, Any]) -> _Packet:
    """Tolerant reader: see docs/voice_guide.md for the field mapping. Facts without a source are unusable."""
    name = _s(_first(packet, "account_name", "name") or _nested(packet, "account", "name"))
    first = _s(_first(packet, "prospect_first_name", "first_name") or _nested(packet, "prospect", "first_name"))
    title = _s(_first(packet, "prospect_title", "title") or _nested(packet, "prospect", "title"))

    trig_obj: Any = packet.get("trigger") if "trigger" in packet else None
    if "trigger" not in packet:
        sigs = [s for s in (packet.get("signals") or []) if isinstance(s, Mapping)
                and _s(s.get("kind")).lower() not in ("", "none")]
        if sigs:
            trig_obj = max(sigs, key=lambda s: float(s.get("strength") or 0.0))
    kind, trigger = "none", None
    if isinstance(trig_obj, Mapping):
        k = _s(trig_obj.get("kind")).lower() or "none"
        if k != "none":
            kind = k
            trigger = _Item(_s(trig_obj.get("evidence") or trig_obj.get("text")), _s(trig_obj.get("source")),
                            _s(_first(trig_obj, "date", "source_date")))

    facts: list[_Item] = []
    for f in packet.get("available_facts") or []:
        if isinstance(f, str):
            continue  # a bare string carries no source, so it cannot be cited
        if isinstance(f, Mapping) and _s(f.get("text")) and _s(f.get("source")):
            facts.append(_Item(_s(f["text"]), _s(f["source"]), _s(f.get("date"))))

    flags = [_s(f) for f in (packet.get("flags") or [])]
    mnc = [_s(x) for x in (_first(packet, "must_not_claim", "must_not_invent") or []) if _s(x)]
    contact = [_s(packet.get(k)) for k in ("prospect_email", "prospect_phone", "email", "phone") if _s(packet.get(k))]

    parts = [name, _s(packet.get("website")), _s(packet.get("location")), _s(packet.get("employees")), first, title]
    for it in facts + ([trigger] if trigger else []):
        parts += [it.text, it.source, it.date]
    return _Packet(name, first, title, kind, trigger, facts, flags, mnc, contact, " \n".join(p for p in parts if p))


# ----------------------------------------------------------------------------------------------------- text tools

_STOP = frozenset("""a an the and or but of to in on at for from by with as is are was were be been it its this that these
those there their they them he she his her we our you your i my me not no so if than then too very just about into over
under up out who whom which what when where while will would can could has have had do does did""".split())

_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_NUM = re.compile(r"\d+(?:[.,]\d+)*")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL = re.compile(r"(?:https?://|www\.)\S+|\b[\w-]+(?:\.[\w-]+)*\.(?:com|net|org|io|co|au|nz|ai|app|dev)\b", re.I)
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}")
_PHONE = re.compile(r"(?<![\w$])\+?\(?\d[\d ()\-]{6,}\d(?!\w)")
_SRC_TAIL = re.compile(r"^(.*?\S)\s*\(source:\s*(.+)\)\s*$")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿←-⇿️‍⌀-⏿]")


def _norm_apos(s: str) -> str:
    return s.replace("’", "'").replace("‘", "'")


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > 4 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _tokens(s: str) -> list[str]:
    s = _ISO.sub(" ", _norm_apos(s).lower())
    return [_stem(w) for w in _WORD.findall(s) if w not in _STOP and len(w) > 1]


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
    return re.compile(r"(?<![\w])" + re.escape(_norm_apos(p).lower()) + r"(?![\w])", re.I)


def _find_phrases(text: str, phrases: list[str]) -> list[str]:
    t = _norm_apos(text).lower()
    return [p for p in phrases if _phrase_re(p).search(t)]


def _find_patterns(text: str, patterns: list[str]) -> list[str]:
    t = _norm_apos(text)
    out = []
    for p in patterns:
        m = re.search(p, t, re.I)
        if m:
            out.append(m.group(0))
    return out


def _date_variants(date: str) -> set[str]:
    d = date.strip()
    out = {d.lower()}
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", d)
    if m:
        y, mo, da = int(m.group(1)), int(m.group(2)), int(m.group(3))
        months = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
                  "november", "december"]
        out |= {f"{y:04d}-{mo:02d}-{da:02d}", f"{da} {months[mo-1]} {y}", f"{da} {months[mo-1][:3]} {y}",
                f"{da:02d}/{mo:02d}/{y}", f"{da}/{mo}/{y}"}
    return out


def _numbers(s: str) -> set[str]:
    return {n.replace(",", "").lstrip("0") or "0" for n in _NUM.findall(s)}


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
    for val in pk.contact_values:
        if val.lower() in text.lower():
            v.append(Violation("EMAIL_ADDRESS" if "@" in val else "PHONE_NUMBER",
                               f"{where}: contains the contact's own detail", ERROR))
    if not allow_links and _URL.search(_EMAIL.sub(" ", text)):
        v.append(Violation("LINK", f"{where}: contains a link or web address", ERROR))


def _check_words(text: str, where: str, cfg: dict[str, Any], v: list[Violation], email: bool = False) -> None:
    banned = list(cfg["banned_phrases"]) + (list(cfg["banned_phrases_email_only"]) if email else [])
    for p in _find_phrases(text, banned):
        v.append(Violation("BANNED_PHRASE", f"{where}: banned phrase '{p}'", ERROR))
    for m in _find_patterns(text, cfg["unsupported_claim_patterns"]):
        v.append(Violation("UNSUPPORTED_CLAIM", f"{where}: unsupported claim '{m}' (nothing in the packet backs it)",
                           ERROR))


def _check_supported(text: str, where: str, pk: _Packet, cfg: dict[str, Any], v: list[Violation]) -> None:
    """Numbers and proper nouns must exist in the packet."""
    text = _norm_apos(text)
    have = _numbers(pk.corpus)
    for n in sorted(_numbers(text) - have):
        v.append(Violation("NUMBER_UNSUPPORTED", f"{where}: number/date '{n}' is not in the packet", ERROR))
    corpus_words = set(_WORD.findall(_norm_apos(pk.corpus).lower()))
    corpus_words |= {w.split("'")[0] for w in corpus_words}
    allowed = {w.lower() for w in cfg["allowed_proper_nouns"]} | {"i"}
    starters = {w.lower() for w in cfg["sentence_start_words"]}
    flagged: list[str] = []
    for sent in re.split(r"(?<=[.?!])\s+|\n+", text):
        toks = list(re.finditer(r"[A-Za-z][A-Za-z'&-]*", sent))
        for i, m in enumerate(toks):
            w = m.group(0).strip("'-&")
            if len(w) < 2 or not w[0].isupper():
                continue
            if i == 0 and (not starters or w.lower() in starters or w.lower().split("'")[0] in starters):
                continue  # sentence-initial words are tested only against the known-starters list
            base = re.sub(r"'s$", "", w.lower())
            if base in allowed or base.split("'")[0] in allowed or base in corpus_words:
                continue
            if base not in flagged:
                flagged.append(base)
    for w in flagged:
        v.append(Violation("PROPER_NOUN_UNSUPPORTED", f"{where}: name '{w}' is not in the packet", ERROR))


def _check_trigger_claims(text: str, where: str, pk: _Packet, cfg: dict[str, Any], v: list[Violation]) -> bool:
    """Event claims need a real trigger; event words must come from its evidence. Returns True if a claim was found."""
    reactions = _find_patterns(text, cfg["trigger_reaction_patterns"])
    words = [w for w in cfg["trigger_event_words"] if _phrase_re(w).search(_norm_apos(text).lower())]
    if not reactions and not words:
        return False
    if pk.trigger is None:
        claim = (reactions + words)[0]
        v.append(Violation("INVENTED_TRIGGER", f"{where}: '{claim}' claims an event but the packet has no real "
                           "trigger (cold call on ICP fit only)", ERROR))
        return True
    ev = {w[:5] for w in _WORD.findall(_norm_apos(pk.trigger.text).lower())}
    for w in words:
        if w.lower()[:5] not in ev:
            v.append(Violation("TRIGGER_MISMATCH", f"{where}: event word '{w}' is not in the trigger evidence "
                               f"'{pk.trigger.text}'", ERROR))
    return True


def _check_role(text: str, where: str, pk: _Packet, cfg: dict[str, Any], v: list[Violation]) -> None:
    if not pk.ambiguous_role:
        return
    hits = _find_patterns(text, cfg["role_assumption_patterns"])
    if pk.title and _phrase_re(pk.title).search(_norm_apos(text).lower()):
        hits.append(pk.title)
    if hits:
        v.append(Violation("ROLE_ASSUMED", f"{where}: packet flags an ambiguous role, but the text assumes it "
                           f"('{hits[0]}')", ERROR))


def _check_must_not(texts: list[tuple[str, str]], pk: _Packet, v: list[Violation]) -> None:
    for where, text in texts:
        for claim in pk.must_not_claim:
            if claim.lower() in _norm_apos(text).lower():
                v.append(Violation("MUST_NOT_CLAIM", f"{where}: contains '{claim}' (on the packet's must-not-claim "
                                   "list)", ERROR))


def _check_label(lines_first: str | None, cfg: dict[str, Any], v: list[Violation]) -> None:
    present = lines_first is not None and lines_first.strip() == cfg["draft_style_label"]
    if not cfg["calibrated"] and not present:
        v.append(Violation("DRAFT_STYLE_LABEL_MISSING", f"voice is not calibrated: first line must be "
                           f"'{cfg['draft_style_label']}'", ERROR))
    if cfg["calibrated"] and present:
        v.append(Violation("DRAFT_STYLE_LABEL_STALE", "voice is calibrated but the DRAFT STYLE label is present",
                           WARNING))


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


def _match_item(text: str, pk: _Packet) -> tuple[_Item | None, float]:
    best, score = None, 0.0
    for it in pk.items:
        s = _containment(text, it.text)
        if s > score:
            best, score = it, s
    return best, score


def _check_cited(where: str, cited: str, item: _Item | None, pk: _Packet, v: list[Violation]) -> None:
    low = cited.lower()
    if not any(s.lower() in low for s in pk.sources):
        v.append(Violation("FACT_SOURCE_UNKNOWN" if where.startswith("FACT") else "TRIGGER_SOURCE_MISMATCH",
                           f"{where}: cited source '{cited}' is not one of the packet's sources", ERROR))
        return
    if item is None:
        return
    if item.source.lower() not in low:
        v.append(Violation("FACT_SOURCE_MISMATCH" if where.startswith("FACT") else "TRIGGER_SOURCE_MISMATCH",
                           f"{where}: cites '{cited}' but the matching packet entry is from '{item.source}'", ERROR))
    elif item.date and not any(d in low for d in _date_variants(item.date)):
        v.append(Violation("FACT_DATE_MISSING" if where.startswith("FACT") else "TRIGGER_DATE_MISSING",
                           f"{where}: source citation must carry the date {item.date}", ERROR))


def check_opener_block(text: str, packet_dict: Mapping[str, Any], voice_cfg: Mapping[str, Any] | None
                       ) -> list[Violation]:
    """Lint one opener block (OPENER / FACT 1 / FACT 2 / GATEKEEPER / TRIGGER) against its research packet."""
    v: list[Violation] = []
    if not isinstance(packet_dict, Mapping):
        return [Violation("PACKET_INVALID", "packet must be a mapping", ERROR)]
    cfg, pk = _cfg(voice_cfg), normalize_packet(packet_dict)
    body = text.rstrip()
    lines = body.splitlines()
    first = lines[0] if lines else None
    label = cfg["draft_style_label"]
    has_label = first is not None and first.strip() == label
    _check_label(first, cfg, v)
    if has_label:
        body = "\n".join(lines[1:])

    sec, order, extra = parse_block(body)
    for lab in LABELS:
        if lab not in sec:
            v.append(Violation("SHAPE_MISSING", f"missing line '{lab}: ...'", ERROR))
    if order != [lab for lab in LABELS if lab in sec]:
        v.append(Violation("SHAPE_ORDER", f"lines must be in the order {', '.join(LABELS)}", ERROR))
    for ln in extra:
        v.append(Violation("SHAPE_EXTRA", f"unexpected line: '{ln[:60]}'", ERROR))

    spoken: list[tuple[str, str]] = []
    full = [(k, sec[k]) for k in LABELS if k in sec]
    for k, content in full:
        _check_surface(content, k, v)
        _check_contact(_split_src(content)[0], k, pk, v)
    _check_must_not([(k, c) for k, c in full], pk, v)

    # OPENER
    if "OPENER" in sec:
        o = sec["OPENER"]
        spoken.append(("OPENER", o))
        if _words(o) > cfg["opener"]["max_words"]:
            v.append(Violation("OPENER_TOO_LONG", f"OPENER is {_words(o)} words; limit {cfg['opener']['max_words']}",
                               ERROR))
        q = o.count("?")
        if q == 0:
            v.append(Violation("OPENER_NO_QUESTION", "OPENER has no question or ask", WARNING))
        elif q > cfg["opener"]["max_questions"]:
            v.append(Violation("OPENER_MULTIPLE_QUESTIONS", f"OPENER asks {q} questions; one ask only", WARNING))
        for w in _find_phrases(o, cfg["pendo_feature_words"]):
            v.append(Violation("OPENER_PENDO_FEATURE", f"OPENER mentions a Pendo feature ('{w}')", ERROR))
        _check_words(o, "OPENER", cfg, v)
        _check_supported(o, "OPENER", pk, cfg, v)
        _check_trigger_claims(o, "OPENER", pk, cfg, v)
        _check_role(o, "OPENER", pk, cfg, v)
        for d in cfg["alex_examples"].get("disliked_openers") or []:
            if _jaccard(o, d) >= cfg["disliked_similarity_min"]:
                v.append(Violation("SIMILAR_TO_DISLIKED", "OPENER is close to an opener Alex dislikes", WARNING))
                break

    # FACTS
    matched: list[_Item] = []
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
            v.append(Violation("FACT_NO_SOURCE", f"{k}: must end with '(source: <source>, <date>)'", ERROR))
        item, score = _match_item(ftext, pk)
        if score < cfg["fact_overlap_min"]:
            v.append(Violation("FACT_UNSUPPORTED", f"{k}: possibly invented; best overlap with any packet fact is "
                               f"{score:.0%}", ERROR))
            item = None
        elif item is not None:
            if item in matched:
                v.append(Violation("FACT_DUPLICATE", f"{k} repeats the same packet fact as the other FACT line "
                                   "(padding)", ERROR))
            matched.append(item)
        if cited is not None:
            _check_cited(k, cited, item, pk, v)
        _check_supported(ftext, k, pk, cfg, v)
        for m in _find_patterns(ftext, cfg["unsupported_claim_patterns"]):
            v.append(Violation("UNSUPPORTED_CLAIM", f"{k}: unsupported claim '{m}'", ERROR))

    # GATEKEEPER
    if "GATEKEEPER" in sec:
        g = sec["GATEKEEPER"]
        spoken.append(("GATEKEEPER", g))
        if _words(g) > cfg["gatekeeper"]["max_words"]:
            v.append(Violation("GATEKEEPER_TOO_LONG", f"GATEKEEPER is {_words(g)} words; limit "
                               f"{cfg['gatekeeper']['max_words']}", ERROR))
        pitch = _find_phrases(g, cfg["gatekeeper"]["pitch_words"])
        if pitch:
            v.append(Violation("GATEKEEPER_PITCH", f"GATEKEEPER sounds like a pitch ('{pitch[0]}')", ERROR))
        if cfg["gatekeeper"]["role_cues"] and not _find_phrases(g, cfg["gatekeeper"]["role_cues"]):
            v.append(Violation("GATEKEEPER_NO_ROLE", "GATEKEEPER must ask for a role or person", ERROR))
        _check_words(g, "GATEKEEPER", cfg, v)
        _check_supported(g, "GATEKEEPER", pk, cfg, v)
        if _find_patterns(g, cfg["trigger_reaction_patterns"]) or _find_phrases(g, cfg["trigger_event_words"]):
            v.append(Violation("GATEKEEPER_PITCH", "GATEKEEPER must not mention the trigger", ERROR))

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
            if cited is None:
                v.append(Violation("TRIGGER_SOURCE_MISMATCH", "TRIGGER must end with '(source: ..., <date>)'", ERROR))
            else:
                _check_cited("TRIGGER", cited, pk.trigger, pk, v)
            if _containment(ttext, pk.trigger.text) < cfg["trigger_overlap_min"]:
                v.append(Violation("TRIGGER_LINE_UNSUPPORTED", "TRIGGER text does not match the packet trigger "
                                   "evidence", ERROR))
            _check_supported(ttext, "TRIGGER", pk, cfg, v)
    return v


# ----------------------------------------------------------------------------------------------------- email draft

_GREETING = re.compile(r"^(hi|hello|hey|g'day)\b[^.?!]*,$", re.I)


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
    and is excluded from every other check."""
    v: list[Violation] = []
    if not isinstance(packet_dict, Mapping):
        return [Violation("PACKET_INVALID", "packet must be a mapping", ERROR)]
    cfg, pk, em = _cfg(voice_cfg), normalize_packet(packet_dict), _cfg(voice_cfg)["email"]
    subject, body = (subject or "").strip(), (body or "").strip("\n")
    lines = body.splitlines()
    first = lines[0] if lines else None
    _check_label(first, cfg, v)
    if first is not None and first.strip() == cfg["draft_style_label"]:
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
    if _words(text) > em["body_max_words"]:
        v.append(Violation("EMAIL_TOO_LONG", f"body is {_words(text)} words; limit {em['body_max_words']}", ERROR))

    content = list(lines)
    if content and _GREETING.match(content[0]):
        content = content[1:]
    if content and content[-1] == em["signoff"]:
        content = content[:-1]
    else:
        v.append(Violation("EMAIL_SIGNOFF", f"body should end with the sign-off '{em['signoff']}'", WARNING))
    if not em["content_lines_min"] <= len(content) <= em["content_lines_max"]:
        v.append(Violation("EMAIL_LINE_COUNT", f"body has {len(content)} content lines; want "
                           f"{em['content_lines_min']}-{em['content_lines_max']}", ERROR))
    if not content or not content[0].startswith(em["opening"]):
        v.append(Violation("EMAIL_OPENING", f"first line must start with '{em['opening']}'", ERROR))

    # exactly one ask: question marks, plus cue phrases in sentences with no question mark
    asks = text.count("?")
    for sent in re.split(r"(?<=[.?!])\s+|\n+", text):
        if "?" not in sent and _find_patterns(sent, em["ask_cues"]):
            asks += 1
    if asks == 0:
        v.append(Violation("EMAIL_NO_ASK", "body has no ask", ERROR))
    elif asks > 1:
        v.append(Violation("EMAIL_MULTIPLE_ASKS", f"body has {asks} asks; one only", ERROR))

    for where, t in (("subject", subject), ("body", text)):
        _check_surface(t, where, v)
        _check_contact(t, where, pk, v, allow_links=bool(em["allow_links"]))
        _check_words(t, where, cfg, v, email=True)
        _check_supported(t, where, pk, cfg, v)
        _check_trigger_claims(t, where, pk, cfg, v)
        _check_role(t, where, pk, cfg, v)
    for p in _find_patterns(text, cfg["email_send_language_patterns"]):
        v.append(Violation("EMAIL_SEND_LANGUAGE", f"body has send language ('{p}'); drafts only", ERROR))
    _check_must_not([("subject", subject), ("body", text)], pk, v)
    if pk.trigger is not None and _find_patterns(text, cfg["trigger_reaction_patterns"]) + [
            w for w in cfg["trigger_event_words"] if _phrase_re(w).search(text.lower())]:
        if _containment(pk.trigger.text, text) < 0.3:
            v.append(Violation("TRIGGER_MISMATCH", "body refers to an event but shares too little with the packet "
                               "trigger evidence", ERROR))
    return v
