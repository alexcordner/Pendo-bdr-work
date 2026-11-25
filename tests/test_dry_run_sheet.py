"""Tests for scripts/dry_run_sheet.py. Offline, synthetic data only (the 1g integration world)."""
from datetime import timezone
from pathlib import Path

import pytest

from scripts import dry_run_sheet as drs
from src.config import load_config
from tests.intake_helpers import write_raw
from tests.test_integration_pipeline import NOW, named_world

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def raw(tmp_path):
    w = named_world()
    w.zoominfo_rows = []
    owner = load_config(ROOT / "config" / "config.yaml").owner.nooks_user_id
    return write_raw(w, tmp_path / "raw", NOW, owner=owner)


def _run(raw, tmp_path, limit="3"):
    out = tmp_path / "out"
    code = drs.main(["--raw-dir", str(raw), "--state-dir", str(tmp_path / "state"), "--log-dir",
                     str(tmp_path / "logs"), "--out-dir", str(out), "--limit", limit, "--now", NOW.isoformat()])
    return code, out


def test_prints_and_writes_the_sheet(raw, tmp_path, capsys):
    code, out = _run(raw, tmp_path)
    assert code == 0
    shown = capsys.readouterr().out
    assert "Bucket counts:" in shown
    assert (out / "dry_run_sheet.txt").read_text().strip()
    assert (out / "dry_run_sheet.md").read_text().strip()
    assert "Bucket counts:" in (out / "dry_run_sheet.txt").read_text()


def test_limit_caps_the_lines(raw, tmp_path):
    cfg = drs.load_config(ROOT / "config" / "config.yaml")
    res, sheet = drs.build(raw, tmp_path / "state", tmp_path / "logs", ROOT / "config" / "config.yaml", NOW, limit=2)
    assert len(sheet.lines) <= 2
    counts = drs.bucket_counts(sheet)
    assert counts["on_sheet"] == len(sheet.lines)
    assert counts["ranked_total"] >= counts["on_sheet"]
    assert cfg is not None


def test_dry_run_never_records_history(raw, tmp_path):
    res, _ = drs.build(raw, tmp_path / "state", tmp_path / "logs", ROOT / "config" / "config.yaml", NOW)
    assert res.live is False and res.sheet_recorded is False


def test_stops_cleanly_without_raw_files(tmp_path, capsys):
    code = drs.main(["--raw-dir", str(tmp_path / "missing"), "--state-dir", str(tmp_path / "s"),
                     "--log-dir", str(tmp_path / "l"), "--out-dir", str(tmp_path / "o"), "--now", NOW.isoformat()])
    assert code == 2
    assert not (tmp_path / "o" / "dry_run_sheet.txt").exists()
