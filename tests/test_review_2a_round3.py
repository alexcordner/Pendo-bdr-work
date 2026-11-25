"""Reviewer scratch tests, round 3 (re-review of 2e5d58a). Offline, synthetic."""
import pytest as _pt
try:
    from src.intake import untied_recent_calls as _x  # noqa: F401  (only on phase2/2a-fixes and later)
except ImportError:
    _pt.skip("needs phase2/2a-fixes (2e5d58a)", allow_module_level=True)
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from src.config import load_config
from src.intake import (MAX_UNTIED_RECENT_SHARE, address_hash, load_raw, parse_sheet_tag, untied_recent_calls, DEFAULT_ADDRESS_SALT)
from src.loaders import call_from_nooks
from src.models import Prospect
from src.pipeline import LiveRefused, PipelineStopped, run_pipeline
from tests.intake_helpers import rewrite, write_raw
from tests.test_integration_pipeline import NOW, named_world

ROOT = Path(__file__).resolve().parent.parent
CFG = load_config(ROOT / "config" / "config.yaml")
OWN = CFG.owner.nooks_user_id


def world_raw(tmp_path):
    w = named_world(); w.zoominfo_rows = []
    raw = write_raw(w, tmp_path / "raw", NOW, owner=OWN)
    return raw, w


def mkcall(d, i, pid, to, when):
    return call_from_nooks({"id": f"c{i}", "duration": 5, "to": to, "prospect": {"id": pid} if pid else None,
                            "callDisposition": {"id": next(iter(d.disposition_index))}, "owner": {"id": OWN},
                            "time": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "createdAt": when.strftime("%Y-%m-%dT%H:%M:%SZ")},
                           d.disposition_index)


def P(nid, acct="001A", phone=None):
    return Prospect(nooks_id=nid, sf_contact_id="003" + (nid or "x"), sf_account_id=acct, nooks_account_id=None, name="n", title="t",
                    email=None, phone_e164=phone, opted_out_email=False)


@pytest.fixture
def d(tmp_path):
    raw, w = world_raw(tmp_path); return load_raw(raw, now=NOW)


def test_boundaries(d):
    d.calls = []
    # exactly 14 days old: counted ("recent")
    d.calls = [mkcall(d, 1, "X", "+61411111111", NOW - timedelta(days=14))]
    assert untied_recent_calls(d, [], NOW)["total"] == 1
    # 14 days + 1 s: not recent
    d.calls = [mkcall(d, 1, "X", "+61411111111", NOW - timedelta(days=14, seconds=1))]
    assert untied_recent_calls(d, [], NOW)["total"] == 0
    # future call
    d.calls = [mkcall(d, 1, "X", "+61411111111", NOW + timedelta(days=30))]
    r = untied_recent_calls(d, [], NOW); print("future-dated call counted as recent:", r["total"], r["untied"])
    # zero calls
    d.calls = []
    assert untied_recent_calls(d, [], NOW)["share"] == 0.0


def test_exact_5_percent(d):
    ps = [P("T")]
    t = NOW - timedelta(days=1)
    d.calls = [mkcall(d, i, "T", "+61411111111", t) for i in range(95)] + [mkcall(d, 100 + i, "U", "+61422222222", t) for i in range(5)]
    r = untied_recent_calls(d, ps, NOW); print("5/100 share", r["share"], "blocked?", r["share"] > MAX_UNTIED_RECENT_SHARE)
    d.calls.append(mkcall(d, 200, "U", "+61422222222", t))
    r = untied_recent_calls(d, ps, NOW); print("6/101 share", round(r["share"], 4), "blocked?", r["share"] > MAX_UNTIED_RECENT_SHARE)


def test_all_external_does_not_block(tmp_path):
    raw, w = world_raw(tmp_path)
    def f(dd):
        for r in dd["rows"]:
            r["prospect"] = None; r["to"] = "external"
    rewrite(raw, "nooks_calls", f)
    try:
        run_pipeline(raw, CFG, tmp_path / "s", NOW, live=True, record_sheet=False)
        print("ALL calls external/no prospect -> live PASSES (unlinkable not held against threshold)")
    except PipelineStopped as e:
        print("blocked:", e.reasons[:2])


def test_id_with_account_none_untied(d):
    d.calls = [mkcall(d, 1, "T", "+61411111111", NOW - timedelta(days=1))]
    assert untied_recent_calls(d, [P("T", acct=None)], NOW)["untied"] == 1


def test_phone_two_prospects_untied(d):
    d.calls = [mkcall(d, 1, None, "+61411111111", NOW - timedelta(days=1))]
    r = untied_recent_calls(d, [P("A", phone="+61411111111"), P("B", acct="001B", phone="+61411111111")], NOW)
    assert r["untied"] == 1


def test_hash_and_normalisation():
    h = address_hash("Jane.Doe+news@Example.com")
    print("plus/dots/case ->", h == address_hash("janedoe@example.com"), h == address_hash("janedoe+news@example.com"))
    assert address_hash("not an address") is None
    # dictionary attack with the public salt: guessable address is recoverable
    guess = address_hash("jane.doe@example.com")
    assert guess == address_hash("janedoe@example.com", DEFAULT_ADDRESS_SALT)
    assert len(h) == 32


def test_raw_address_in_meta_rejected(tmp_path):
    raw, w = world_raw(tmp_path)
    rewrite(raw, "gmail_sent", lambda dd: dd["meta"].update({"searched_address_hashes": ["jane@example.com"]}))
    assert not load_raw(raw, now=NOW).report.ok


def test_log_has_no_addresses_with_coverage(tmp_path):
    import re
    raw, w = world_raw(tmp_path)
    r = run_pipeline(raw, CFG, tmp_path / "s", NOW, log_dir=tmp_path / "l")
    txt = Path(r.log_path).read_text()
    assert not re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", txt)
    print("uncovered:", len(r.gmail_uncovered), "| live with gmail uncovered ->", end=" ")
    try:
        run_pipeline(raw, CFG, tmp_path / "s2", NOW, live=True, record_sheet=False); print("PASSES")
    except PipelineStopped as e:
        print("refused:", e.reasons[0][:90])


def test_calls_to_resolved_lead_or_non_book_prospects_still_count_as_untied(tmp_path):
    """After the runbook's resolve step, a prospect whose crmId is a Lead (00Q..) or a Contact outside the book is
    resolved but can never get an account. Does it keep live blocked forever?"""
    raw, w = world_raw(tmp_path)
    rows = json.loads((raw / "nooks_calls.json").read_text())["rows"]
    n_recent = sum(1 for r in rows)  # all world calls are recent
    extra_calls, extra_prospects = [], []
    for i in range(3):   # 3 extra recent calls on Lead / non-book prospects
        pid = f"NK-LEAD-{i}"
        extra_prospects.append({"id": pid, "type": "salesforce", "name": "L", "firstName": "L", "lastName": str(i), "title": "t",
                                "primaryEmail": None, "crmId": f"00QFAKE0000000{i}", "account": None})
        extra_calls.append({"id": f"CL{i}", "source": "nooks_sep", "duration": 5, "to": f"+6142200000{i}", "direction": "outgoing",
                            "prospect": {"id": pid}, "callDisposition": {"id": w.dispositions[0]["id"]}, "owner": {"id": OWN},
                            "time": (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), "createdAt": "2026-11-24T00:00:00Z"})
    raw2 = write_raw(w, tmp_path / "raw2", NOW, owner=OWN, nooks_calls=[dict(c) for c in w.calls] + extra_calls,
                     nooks_prospects=list(w.nooks_prospects) + extra_prospects)
    r = run_pipeline(raw2, CFG, tmp_path / "s", NOW)
    st = r.intake_report.stats
    print("recent total", st["recent_calls_total"], "untied", st["recent_calls_not_tied_to_account"],
          "live blockers:", [b[:70] for b in r.intake_report.live_blockers])
