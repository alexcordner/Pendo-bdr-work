"""Phase 2 gates, determinism, scale and the hard-rule scan (task 2f). Offline, synthetic (tests/phase2_world.py)."""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from src import callsheet as cs
from src.config import load_config
from src.pipeline import LiveRefused, run_pipeline
from tests.intake_helpers import write_raw
from tests.phase2_world import PERSONAL_EMAIL, phase2_world
from tests.test_integration_pipeline import NOW
from tests.test_merge_pages import zi_page

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def cfg():
    return load_config(ROOT / "config" / "config.yaml")


@pytest.fixture
def world():
    w = phase2_world()
    w.zoominfo_rows = []
    return w


def run(raw, cfg, tmp, **kw):
    return run_pipeline(raw, cfg, tmp / "state", NOW, log_dir=tmp / "logs", **kw)


def recent(days):
    return (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def tied_calls(world, n, days=5):
    """n recent calls on a prospect the world knows (their account is tied by Nooks id)."""
    pr = world.nooks_prospects[0]
    return [{"id": f"FAKE-TIED-{i}", "time": recent(days), "createdAt": recent(days), "duration": 20, "to": "+61400000001",
             "prospect": {"id": pr["id"]}, "callDisposition": {"id": "disp-no-answer"}} for i in range(n)]


def stray_calls(n, prefix, days=5):
    return [{"id": f"FAKE-{prefix}-{i}", "time": recent(days), "duration": 20, "to": f"+6140099{i:04d}",
             "prospect": {"id": f"NK-{prefix}-{i}"}, "callDisposition": {"id": "disp-no-answer"}} for i in range(n)]


# ---------------------------------------------------------------- (v) the 2a gates on this world
def test_v_baseline_world_passes_the_live_gate(world, cfg, tmp_path):
    raw = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id)
    r = run(raw, cfg, tmp_path, live=True)
    assert r.live and r.gmail_uncovered == [] and r.stats["recent_calls_not_tied_to_account"] == 0


def test_v_six_percent_unresolved_recent_calls_block_live(world, cfg, tmp_path):
    calls = list(world.calls) + tied_calls(world, 94) + stray_calls(6, "UNRES")        # 6 of 100 recent calls
    raw = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id, nooks_calls=calls)
    dry = run(raw, cfg, tmp_path / "dry")
    assert dry.stats["recent_calls_unresolved"] == 6 and dry.stats["recent_calls_total"] >= 100
    assert dry.stats["recent_calls_not_tied_to_account"] / dry.stats["recent_calls_total"] > 0.05
    with pytest.raises(LiveRefused) as e:
        run(raw, cfg, tmp_path / "live", live=True)
    assert any("not tied to an account" in x for x in e.value.reasons)


def test_v_three_percent_unresolved_calls_older_than_three_days_do_not_block(world, cfg, tmp_path):
    calls = list(world.calls) + tied_calls(world, 97) + stray_calls(3, "UNRES")
    raw = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id, nooks_calls=calls)
    assert run(raw, cfg, tmp_path, live=True).live


def test_v_lead_prospect_calls_do_not_block_live(world, cfg, tmp_path):
    calls = list(world.calls) + tied_calls(world, 94) + stray_calls(6, "LEAD")
    leads = [{"id": f"NK-LEAD-{i}", "type": "salesforce", "name": f"Lead {i}", "crmId": f"00QFAKE00000{i:03d}",
              "account": None} for i in range(6)]
    raw = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id, nooks_calls=calls,
                    nooks_prospects=list(world.nooks_prospects) + leads)
    r = run(raw, cfg, tmp_path, live=True)
    assert r.live and r.stats["recent_calls_not_in_book"] == 6 and r.stats["recent_calls_not_tied_to_account"] == 0


def test_v_gmail_coverage_loop(world, cfg, tmp_path):
    from src.intake import DEFAULT_ADDRESS_SALT, address_hash
    raw = write_raw(world, tmp_path / "raw0", NOW, owner=cfg.owner.nooks_user_id, gmail_cover=[])
    dry = run(raw, cfg, tmp_path / "a")
    assert dry.gmail_uncovered and dry.stats["gmail_ranked_prospects_uncovered"] == len(dry.gmail_uncovered)
    with pytest.raises(LiveRefused) as e:
        run(raw, cfg, tmp_path / "b", live=True)
    assert any("have not been Gmail-searched" in x for x in e.value.reasons)
    # search the first half, the rest is still named; then search them all
    emails = {c["Id"]: c["Email"] for c in world.contacts}
    chosen = [p for ps in dry.rank_result.prospects.values() for p in ps]
    half = [p.email for p in chosen[: len(chosen) // 2]]
    raw2 = write_raw(world, tmp_path / "raw1", NOW, owner=cfg.owner.nooks_user_id, gmail_cover=half)
    part = run(raw2, cfg, tmp_path / "c")
    assert 0 < len(part.gmail_uncovered) < len(dry.gmail_uncovered)
    assert not set(part.gmail_uncovered) & {address_hash(a, DEFAULT_ADDRESS_SALT) for a in half}
    raw3 = write_raw(world, tmp_path / "raw2", NOW, owner=cfg.owner.nooks_user_id, gmail_cover=list(emails.values()))
    assert run(raw3, cfg, tmp_path / "d", live=True).gmail_uncovered == []
    for lg in (tmp_path / "logs").glob("*.log"):                    # addresses are hashed, never written
        assert PERSONAL_EMAIL not in lg.read_text()


def test_v_real_zoominfo_drift_example_derives_last_page_true():
    """REAL: Executive Move read totalResults 458 / 435 / 437 / 383 while 400 rows came back over 4 full pages, and
    page 5 was an empty {meta} object. totalResults is never used."""
    from scripts import merge_pages as mp
    pages = [zi_page(1, 100, 458), zi_page(2, 100, 435), zi_page(3, 100, 437), zi_page(4, 100, 383), zi_page(5, 0)]
    assert mp.derive_last_page(pages, "zoominfo", expect_pages=5, page_size=100) is True
    # four full pages with no short / empty page after them: NOT complete, whatever totalResults says
    assert mp.derive_last_page(pages[:4], "zoominfo", expect_pages=4, page_size=100) is False
    # 400 rows with a short last page also ends
    assert mp.derive_last_page([zi_page(1, 100, 458), zi_page(2, 100, 435), zi_page(3, 100, 437), zi_page(4, 37, 383)],
                               "zoominfo", expect_pages=4, page_size=100) is True


def test_v_derive_last_page_refuses_a_gap_and_a_repeated_page():
    from scripts import merge_pages as mp
    assert mp.derive_last_page([zi_page(1, 3), zi_page(3, 1)], "zoominfo", expect_pages=2, page_size=3) is False
    assert mp.derive_last_page([zi_page(1, 3), zi_page(1, 3, seq="b"), zi_page(2, 1, seq="b")], "zoominfo",
                               expect_pages=3, page_size=3) is False


def test_v_phase3_preconditions_are_in_every_run_log(world, cfg, tmp_path):
    raw = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id)
    log = "\n".join(run(raw, cfg, tmp_path).log_lines)
    assert "== PHASE 3 PRECONDITIONS ==" in log and "HARD PRECONDITION 6" in log


def test_v_is_dnc_call_is_wired_into_the_pipeline():
    src = (ROOT / "src" / "pipeline.py").read_text()
    assert src.count("st_mod.is_dnc_call(") >= 2


# ---------------------------------------------------------------- (vi) shuffle determinism: 20 shuffles x 3 hash seeds
@pytest.mark.parametrize("hashseed", ["0", "1", "424242"])
def test_vi_pipeline_and_sheet_are_independent_of_input_order_and_hash_seed(hashseed):
    env = {**os.environ, "PYTHONHASHSEED": hashseed}
    out = subprocess.run([sys.executable, "-m", "tests.phase2_driver", "shuffle", "--seeds", "20"], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr[-2000:]
    d = json.loads(out.stdout.strip().splitlines()[-1])
    assert len(d["shuffled"]) == 20 and len(set(d["shuffled"])) == 1, "sheet / packets / log changed with input order"
    assert d["shuffled"][0] == d["unshuffled"]
    ref = Path(__file__).with_name("_phase2_digest_check.json")
    # all three hash seeds must agree with each other too (compared through the pytest cache of this session)
    seen = getattr(test_vi_pipeline_and_sheet_are_independent_of_input_order_and_hash_seed, "_seen", set())
    seen.add(d["unshuffled"])
    test_vi_pipeline_and_sheet_are_independent_of_input_order_and_hash_seed._seen = seen
    assert len(seen) == 1, "digest differs between PYTHONHASHSEED values"
    assert not ref.exists()


# ---------------------------------------------------------------- (vii) scale (small here; 12k numbers are in the report)
def test_vii_scale_smoke_1500_accounts_runs_in_bounds():
    out = subprocess.run([sys.executable, "-m", "tests.phase2_driver", "scale", "--n", "1500"], cwd=ROOT,
                         capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr[-2000:]
    d = json.loads(out.stdout.strip().splitlines()[-1])
    assert d["accounts"] == 1500 and d["on_sheet"] == 10 and d["ranked"] >= 10
    assert d["seconds_pipeline"] < 30 and d["peak_rss_mb"] < 400
    assert d["junk_title_needs_prospect"] > 0


# ---------------------------------------------------------------- (viii) hard-rule scan
SRC = sorted((ROOT / "src").glob("*.py"))
SCRIPTS = sorted((ROOT / "scripts").glob("*.py"))
WRITE_ATTRS = {"write_text", "write_bytes", "touch", "mkdir", "unlink", "rename", "rmdir", "symlink_to",
               "hardlink_to", "chmod", "write", "writelines", "dump", "copy", "copy2", "copyfile", "move", "rmtree"}
OS_ONLY_ATTRS = {"replace", "rename", "remove", "unlink", "makedirs", "fchmod", "open"}     # str.replace / datetime.replace are not writes
SEND_NAMES = re.compile(r"\b(sendTaskEmail|send_message|create_draft|createEmail|createTask|updateTask|deleteTask|"
                        r"createAccountNote|createProspectNote|createCrmProspects|syncProspects|updateProspect|"
                        r"createSobjectRecord|updateSobjectRecord|deleteSobjectRecord|sendmail|smtplib)\b")
NET_IMPORTS = {"requests", "httpx", "urllib", "http", "socket", "aiohttp", "smtplib", "ssl", "ftplib", "subprocess",
               "anthropic", "openai", "mcp", "paramiko", "telnetlib", "xmlrpc", "webbrowser"}


def _write_calls(path: Path) -> dict[str, list[str]]:
    """function name -> write-style call names inside it (open() with a write mode counts)."""
    tree = ast.parse(path.read_text())
    out: dict[str, list[str]] = {}
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        hits = []
        for c in ast.walk(fn):
            if not isinstance(c, ast.Call):
                continue
            if isinstance(c.func, ast.Attribute) and c.func.attr in WRITE_ATTRS:
                hits.append(c.func.attr)
            if (isinstance(c.func, ast.Attribute) and c.func.attr == "replace" and isinstance(c.func.value, ast.Name)
                    and c.func.value.id in ("os", "shutil")):
                hits.append("os.replace")
            if (isinstance(c.func, ast.Attribute) and c.func.attr == "replace" and isinstance(c.func.value, ast.Name)
                    and c.func.value.id in ("tmp", "path", "p", "out")):
                hits.append("Path.replace")
            if isinstance(c.func, ast.Name) and c.func.id == "open":
                mode = c.args[1].value if len(c.args) > 1 and isinstance(c.args[1], ast.Constant) else None
                for kw in c.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                        mode = kw.value.value
                if mode and any(ch in str(mode) for ch in "wax+"):
                    hits.append("open(write)")
        if hits:
            out[fn.name] = hits
    return out


def test_viii_no_send_draft_or_write_tool_names_anywhere():
    for p in SRC + SCRIPTS:
        text = p.read_text()
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        found = [m.group(0) for m in SEND_NAMES.finditer(code) if "'" not in code[max(0, m.start() - 1):m.start()]]
        # names may appear in refusal messages and docstrings; they must never be CALLED
        tree = ast.parse(text)
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                called = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
                assert not SEND_NAMES.fullmatch(called), (p.name, called)
        assert isinstance(found, list)


def test_viii_the_only_src_writers_are_state_and_pipeline_write_log():
    writers = {p.name: _write_calls(p) for p in SRC}
    writers = {k: v for k, v in writers.items() if v}
    assert sorted(writers) == ["pipeline.py", "state.py"], writers
    assert set(writers["pipeline.py"]) == {"write_log"}, writers["pipeline.py"]
    assert set(writers["state.py"]) == {"save_state", "_set_aside"}, writers["state.py"]   # _set_aside renames a corrupt state file aside


def test_viii_scripts_that_write_go_through_safeout():
    """Every script that writes anywhere must resolve its path with scripts/safeout.py (writes only under output/),
    except merge_pages (writes the raw dump it was told to, under output/) which also uses safeout."""
    for p in SCRIPTS:
        calls = _write_calls(p)
        if not calls or p.name == "safeout.py":
            continue
        text = p.read_text()
        assert "safeout" in text, (p.name, "writes but does not use safeout", calls)


def test_viii_safeout_refuses_paths_outside_output(tmp_path, monkeypatch):
    from scripts import safeout
    monkeypatch.setenv("APAC_OUTPUT_ROOT", str(tmp_path / "output"))
    assert str(safeout.safe_out_path(tmp_path / "output" / "x.json")).startswith(str(tmp_path / "output"))
    for bad in (tmp_path / "docs" / "x.json", tmp_path / "output", tmp_path / "x.json"):
        with pytest.raises(safeout.UnsafeOutput):
            safeout.safe_out_path(bad)
    link = tmp_path / "output" / "link.json"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(tmp_path / "elsewhere.json")
    with pytest.raises(safeout.UnsafeOutput):
        safeout.write_private(link, "x")
    assert not (tmp_path / "elsewhere.json").exists()


def test_viii_merge_pages_and_dry_run_sheet_refuse_a_path_outside_output(tmp_path, monkeypatch, capsys):
    from scripts import dry_run_sheet as drs
    from scripts import merge_pages as mp
    monkeypatch.setenv("APAC_OUTPUT_ROOT", str(tmp_path / "output"))
    page = tmp_path / "p.json"
    page.write_text(json.dumps({"data": [{"id": "a"}]}))
    man = tmp_path / "m.json"
    man.write_text(json.dumps([{"path": str(page)}]))
    code = mp.main(["--manifest", str(man), "--name", "nooks_x", "--raw-dir", str(tmp_path / "docs"), "--source-tool",
                    "listX", "--query", "q", "--last-page-reached", "true"])
    assert code == 2 and not (tmp_path / "docs").exists() and "must be under" in capsys.readouterr().err
    w = phase2_world()
    w.zoominfo_rows = []
    cfg = load_config(ROOT / "config" / "config.yaml")
    raw = write_raw(w, tmp_path / "output" / "raw", NOW, owner=cfg.owner.nooks_user_id)
    args = ["--raw-dir", str(raw), "--state-dir", str(tmp_path / "output" / "s"), "--log-dir", str(tmp_path / "output" / "l"),
            "--now", NOW.isoformat()]
    assert drs.main(args + ["--out-dir", str(tmp_path / "docs")]) == 2 and not (tmp_path / "docs").exists()
    assert drs.main(args + ["--out-dir", str(tmp_path / "output" / "sheet")]) == 0
    assert (tmp_path / "output" / "sheet" / "dry_run_sheet.txt").stat().st_mode & 0o777 == 0o600


ALLOWED_PROCESS = {"safeout.py": {"subprocess"}}     # runs `git check-ignore` with a fixed argv; nothing else


@pytest.mark.parametrize("path", SRC + SCRIPTS, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_viii_no_network_or_process_imports(path):
    tree = ast.parse(path.read_text())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            mods.add(n.module.split(".")[0])
    assert not (mods & NET_IMPORTS) - ALLOWED_PROCESS.get(path.name, set()), (path.name, mods & NET_IMPORTS)


def test_viii_the_sheet_never_builds_a_payload_without_the_checked_entry_point():
    """prepare_task is the only function that turns a sheet line into a task payload; it needs a Nooks owner id."""
    names = [n.name for n in ast.walk(ast.parse((ROOT / "src" / "callsheet.py").read_text()))
             if isinstance(n, ast.FunctionDef) and n.name.startswith(("prepare", "write", "send", "create"))]
    assert names == ["prepare_task"], names
