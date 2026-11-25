"""Reviewer scratch tests (2a review). Offline, synthetic. Each test documents one probe of the --live gate
or intake tolerance. A test that FAILS here is a finding."""
import json
import random
import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from src import state as st_mod
from src.config import load_config
from src.intake import load_raw, scrub
from src.pipeline import LiveRefused, PipelineStopped, run_pipeline
from tests.intake_helpers import read, rewrite, write_raw, iso
from tests.test_integration_pipeline import NOW, named_world

ROOT = Path(__file__).resolve().parent.parent
CFG = load_config(ROOT / "config" / "config.yaml")


def mk(tmp_path, **kw):
    w = named_world(); w.zoominfo_rows = []
    return write_raw(w, tmp_path / "raw", NOW, owner=CFG.owner.nooks_user_id, **kw), w


def live(raw, tmp_path):
    return run_pipeline(raw, CFG, tmp_path / "state", NOW, live=True, record_sheet=False, log_dir=tmp_path / "logs")


def live_ok(raw, tmp_path):
    try:
        live(raw, tmp_path); return True
    except PipelineStopped:
        return False


def test_baseline_live_passes(tmp_path):
    raw, _ = mk(tmp_path)
    assert live_ok(raw, tmp_path)


def test_partial_calls_blocks(tmp_path):
    raw, _ = mk(tmp_path, calls_last_page=False)
    assert not live_ok(raw, tmp_path)


def test_calls_flag_lies_in_file_but_run_meta_says_false(tmp_path):
    raw, _ = mk(tmp_path)
    rewrite(raw, "run_meta", lambda d: d["reached_last_page"].__setitem__("nooks_calls", False))
    assert not live_ok(raw, tmp_path)


def test_missing_gmail_blocks(tmp_path):
    raw, _ = mk(tmp_path, include_gmail=False)
    assert not live_ok(raw, tmp_path)


def test_credits_unknown_blocks(tmp_path):
    raw, _ = mk(tmp_path, credits=None)
    assert not live_ok(raw, tmp_path)


def test_credits_nonzero_within_budget0(tmp_path):
    raw, _ = mk(tmp_path, credits=3)
    assert not live_ok(raw, tmp_path)


def test_credits_bool_true(tmp_path):
    raw, _ = mk(tmp_path)
    rewrite(raw, "run_meta", lambda d: d.__setitem__("zoominfo_credits_used", True))
    assert not live_ok(raw, tmp_path)


def test_credits_string(tmp_path):
    raw, _ = mk(tmp_path)
    rewrite(raw, "run_meta", lambda d: d.__setitem__("zoominfo_credits_used", "0"))
    assert not live_ok(raw, tmp_path)


def test_owner_mismatch(tmp_path):
    raw, _ = mk(tmp_path)
    rewrite(raw, "run_meta", lambda d: d.__setitem__("nooks_user_id", "SOMEONE-ELSE"))
    assert not live_ok(raw, tmp_path)


@pytest.mark.xfail(strict=True, reason='F5 other-owner calls only warn')
def test_calls_by_other_owner_only_warning(tmp_path):
    """All calls belong to other reps: should this block live? (probe: filter is not a permission boundary)"""
    raw, _ = mk(tmp_path)
    def f(d):
        for r in d["rows"]:
            r["owner"] = {"id": "OTHER"}
    rewrite(raw, "nooks_calls", f)
    assert not live_ok(raw, tmp_path), "calls from another owner are only a WARNING; live passes"


def test_stale_blocks(tmp_path):
    raw, w = mk(tmp_path)
    r = run_pipeline  # noqa
    with pytest.raises(LiveRefused):
        run_pipeline(raw, CFG, tmp_path / "state", NOW + timedelta(hours=25), live=True, record_sheet=False)


def test_future_fetch_blocks(tmp_path):
    raw, _ = mk(tmp_path)
    with pytest.raises(LiveRefused):
        run_pipeline(raw, CFG, tmp_path / "state", NOW - timedelta(hours=2), live=True, record_sheet=False)


def test_empty_history_blocks(tmp_path):
    raw, _ = mk(tmp_path, nooks_calls=[])
    assert not live_ok(raw, tmp_path)


def test_missing_disposition_name_stops(tmp_path):
    raw, _ = mk(tmp_path)
    rewrite(raw, "nooks_dispositions", lambda d: d["rows"].__setitem__(slice(None), [r for r in d["rows"] if r["name"] != "Do Not Call"]))
    rewrite(raw, "run_meta", lambda d: d["row_counts"].__setitem__("nooks_dispositions", len(read(raw, "nooks_dispositions")["rows"])))
    with pytest.raises(PipelineStopped):
        live(raw, tmp_path)


def test_unknown_disposition_blocks(tmp_path):
    raw, _ = mk(tmp_path)
    def f(d):
        d["rows"][0]["callDisposition"] = {"id": "NOPE"}
    rewrite(raw, "nooks_calls", f)
    assert not live_ok(raw, tmp_path)


def test_call_without_disposition_field(tmp_path):
    """callDisposition null (call never dispositioned): live?"""
    raw, _ = mk(tmp_path)
    def f(d):
        for r in d["rows"][:3]:
            r["callDisposition"] = None
    rewrite(raw, "nooks_calls", f)
    print("live ok with 3 null dispositions:", live_ok(raw, tmp_path))


def test_dry_run_state_marker_then_partial_live(tmp_path):
    """dry run on FULL data sets marker; next run partial must not go live."""
    raw, w = mk(tmp_path)
    run_pipeline(raw, CFG, tmp_path / "state", NOW, log_dir=tmp_path / "logs")
    raw2 = write_raw(w, tmp_path / "raw2", NOW, owner=CFG.owner.nooks_user_id, calls_last_page=False)
    with pytest.raises(LiveRefused):
        run_pipeline(raw2, CFG, tmp_path / "state", NOW, live=True, record_sheet=False)


def test_dry_run_partial_history_then_live_with_good_flags_but_truncated(tmp_path):
    """Truncated call list claims last page: only first 3 calls. State memory (calls_seen >= seen) should catch it
    ONLY if a prior run saw more."""
    raw, w = mk(tmp_path)
    run_pipeline(raw, CFG, tmp_path / "state", NOW, log_dir=tmp_path / "logs")
    rows = w.calls[:3]
    raw2 = write_raw(w, tmp_path / "raw2", NOW, owner=CFG.owner.nooks_user_id, nooks_calls=rows)
    assert not live_ok(raw2, tmp_path), "truncated history claiming last page after a fuller prior run"



def test_corrupt_state_live(tmp_path):
    raw, _ = mk(tmp_path)
    sd = tmp_path / "state"; sd.mkdir()
    (sd / "state.json").write_text("{not json")
    print("corrupt state -> live ok:", live_ok(raw, tmp_path))


def test_old_state_marker_live(tmp_path):
    raw, _ = mk(tmp_path)
    run_pipeline(raw, CFG, tmp_path / "state", NOW, log_dir=tmp_path / "logs")
    p = tmp_path / "state" / "state.json"
    d = json.loads(p.read_text()); print([k for k in d if "dnc" in k])
    for k in list(d):
        if k.startswith("dnc_rebuilt"):
            d.pop(k)
    p.write_text(json.dumps(d))
    print("marker stripped -> live ok:", live_ok(raw, tmp_path))


def test_dry_run_never_records_sheet(tmp_path):
    raw, _ = mk(tmp_path)
    r = run_pipeline(raw, CFG, tmp_path / "state", NOW, log_dir=tmp_path / "logs")
    assert not r.sheet_recorded
    d = json.loads((tmp_path / "state" / "state.json").read_text())
    assert not d.get("sheets") and not d.get("sheet_history"), [k for k in d]
    with pytest.raises(ValueError):
        run_pipeline(raw, CFG, tmp_path / "state", NOW, record_sheet=True)


def test_dry_run_with_partial_calls_saves_state(tmp_path):
    raw, _ = mk(tmp_path, calls_last_page=False)
    r = run_pipeline(raw, CFG, tmp_path / "state", NOW, log_dir=tmp_path / "logs")
    d = json.loads((tmp_path / "state" / "state.json").read_text())
    print("dry partial saved marker:", {k: v for k, v in d.items() if "dnc" in k})
    assert not st_mod.dnc_is_rebuilt(r.state)


# ---------------- intake tolerance
@pytest.mark.xfail(strict=True, reason='F4 UnicodeDecodeError escapes load_raw')
def test_utf16_file_does_not_raise(tmp_path):
    raw, _ = mk(tmp_path)
    p = raw / "sf_contacts.json"
    p.write_bytes(p.read_text().encode("utf-16"))
    d = load_raw(raw, now=NOW)       # contract: never raises for bad data
    assert not d.report.ok


def test_bom_file(tmp_path):
    raw, _ = mk(tmp_path)
    p = raw / "sf_contacts.json"
    p.write_bytes(b"\xef\xbb\xbf" + p.read_bytes())
    d = load_raw(raw, now=NOW)
    print("BOM ->", d.report.errors[:2])
    assert not d.report.ok            # loud, not silent


@pytest.mark.xfail(strict=True, reason='F4 UnicodeDecodeError escapes load_raw')
def test_binary_garbage(tmp_path):
    raw, _ = mk(tmp_path)
    (raw / "nooks_calls.json").write_bytes(b"\xff\xfe\x00garbage\x80")
    d = load_raw(raw, now=NOW)
    assert not d.report.ok


def test_numbers_as_strings_and_null_fields(tmp_path):
    raw, _ = mk(tmp_path)
    def f(d):
        for r in d["rows"]:
            r["duration"] = str(r.get("duration"))
            r["to"] = None
    rewrite(raw, "nooks_calls", f)
    d = load_raw(raw, now=NOW)
    print("calls duration str/null to -> malformed:", {k: (v.count, v.samples[:2]) for k, v in d.report.malformed.items()}, d.report.ok)


def test_huge_row_and_extra_keys(tmp_path):
    raw, _ = mk(tmp_path)
    def f(d):
        d["rows"][0]["junk"] = "x" * 5_000_000
        d["rows"][0]["extra"] = {"a": [1, 2, 3]}
    rewrite(raw, "sf_accounts", f)
    d = load_raw(raw, now=NOW)
    assert d.report.ok


def test_mixed_15_18_ids_dedupe(tmp_path):
    from src.loaders import normalise_sf_id
    assert normalise_sf_id("001000000000001AAA") == normalise_sf_id("001000000000001")


def test_pii_not_in_log(tmp_path):
    raw, w = mk(tmp_path)
    r = run_pipeline(raw, CFG, tmp_path / "state", NOW, log_dir=tmp_path / "logs")
    import re
    txt = "\n".join(r.log_lines) + Path(r.log_path).read_text()
    assert not re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", txt)
    assert not re.findall(r"\+\d{9,}", txt)


def test_pii_in_malformed_samples(tmp_path):
    raw, _ = mk(tmp_path)
    def f(d):
        d["rows"][0]["id"] = None
        d["rows"][0]["to"] = "jane.doe@example.com"
        d["rows"][0]["callDisposition"] = "jane.doe@example.com"
    rewrite(raw, "nooks_calls", f)
    d = load_raw(raw, now=NOW)
    txt = "\n".join(d.report.lines())
    assert "jane.doe" not in txt


def test_shuffle_files_same_result(tmp_path):
    raw, w = mk(tmp_path)
    base = run_pipeline(raw, CFG, tmp_path / "s1", NOW)
    raw2 = tmp_path / "raw_shuf"; shutil.copytree(raw, raw2)
    rnd = random.Random(5)
    for n in ("sf_accounts", "sf_contacts", "nooks_calls", "nooks_prospects", "zoominfo_scoops"):
        rewrite(raw2, n, lambda d: rnd.shuffle(d["rows"]))
    r2 = run_pipeline(raw2, CFG, tmp_path / "s2", NOW)
    a = [(x.account.name, x.score) for x in base.rank_result.ranked]
    b = [(x.account.name, x.score) for x in r2.rank_result.ranked]
    assert a == b


# ---------------- narrower allowlist proposed for the 2a edit of test_integration_pipeline.py
import ast, re as _re


def _write_funcs(path):
    tree = ast.parse(path.read_text())
    hits = {}
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        src = ast.get_source_segment(path.read_text(), fn) or ""
        if _re.search(r"open\([^)]*[\"'][wa]b?[\"']|\.write_text\(|\.write_bytes\(|mkstemp|os\.replace|os\.unlink|shutil", src):
            hits[fn.name] = True
    return sorted(hits)


def test_narrow_writers_pipeline_only_write_log():
    src = ROOT / "src"
    assert "write_log" in _write_funcs(src / "pipeline.py")
    assert [f for f in _write_funcs(src / "pipeline.py") if f != "write_log" and f != "run_pipeline"] == []
    others = [p.name for p in src.glob("*.py") if p.name not in ("pipeline.py", "state.py") and _write_funcs(p)]
    assert others == []


def test_narrow_argparse_sys_only_in_pipeline():
    for p in (ROOT / "src").glob("*.py"):
        mods = {a.name.split(".")[0] for n in ast.walk(ast.parse(p.read_text())) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module.split(".")[0] for n in ast.walk(ast.parse(p.read_text())) if isinstance(n, ast.ImportFrom) and n.module}
        if p.name != "pipeline.py":
            assert not mods & {"argparse", "sys"}, (p.name, mods & {"argparse", "sys"})


def test_banned_tool_names_not_in_src():
    banned = _re.compile(r"send_message|sendTaskEmail|createTask|createEmail|createCall|create_draft|enrich_|redeem|account_research|soqlQuery|createSobject|updateSobject", _re.I)
    for p in list((ROOT / "src").glob("*.py")) + [ROOT / "scripts" / "merge_pages.py"]:
        hits = [l for l in p.read_text().splitlines() if banned.search(l) and not l.strip().startswith(("#", '"', "*", "``"))]
        print(p.name, len(hits), [h.strip()[:90] for h in hits[:3]])


@pytest.mark.xfail(strict=True, reason='F1 nooks_prospects required, no Contact-as-prospect path')
def test_probe_c_without_nooks_prospects_pipeline_still_ranks(tmp_path):
    """Probe finding (c): nooks_prospects cannot be bulk fetched. Pipeline must still produce a sheet using
    Salesforce Contacts as prospects."""
    raw, _ = mk(tmp_path)
    (raw / "nooks_prospects.json").unlink()
    r = run_pipeline(raw, CFG, tmp_path / "state", NOW)
    assert len(r.rank_result.ranked) > 0


@pytest.mark.xfail(strict=True, reason='F1 empty prospects => nothing ranked')
def test_probe_c_empty_prospects_ranks_nothing(tmp_path):
    raw, _ = mk(tmp_path, nooks_prospects=[])
    r = run_pipeline(raw, CFG, tmp_path / "state", NOW)
    print("ranked with empty prospects:", len(r.rank_result.ranked), "needs_prospect", len(r.rank_result.needs_prospect))
    assert len(r.rank_result.ranked) > 0


@pytest.mark.xfail(strict=True, reason='F3 task note tag not read')
def test_probe_b_task_note_tag_feeds_last_sheet(tmp_path):
    raw, w = mk(tmp_path, include_tasks=True)
    acct = w.sf_rows[0]["Id"]
    tag_rows = [{"id": "T1", "action": "call", "completed": False, "dueAt": None, "status": "ready",
                 "note": "[apac-callsheet 2026-11-24 #1] x", "prospect": {"id": "FAKE-NK-P-0001-1"}, "sequence": None,
                 "sequenceState": None, "sequenceStep": None, "email": None, "owner": {"id": CFG.owner.nooks_user_id}}]
    rewrite(raw, "nooks_tasks", lambda d: d["rows"].extend(tag_rows))
    rewrite(raw, "run_meta", lambda d: d["row_counts"].__setitem__("nooks_tasks", 1))
    from src.intake import load_raw
    d = load_raw(raw, now=NOW)
    print("task rows:", len(d.nooks_tasks), "task models:", [t.__dict__.keys() for t in d.tasks][:1])
    assert any(getattr(t, "note", None) for t in d.tasks), "TaskRecord/task_from_nooks drops `note`"
