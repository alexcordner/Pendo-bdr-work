"""Reviewer scratch tests, round 2 (re-review of e8a1b4a). Offline, synthetic."""
import json
from datetime import timedelta
from pathlib import Path

import pytest

from src import state as st_mod
from src.config import load_config
from src.intake import load_raw, parse_sheet_tag, scrub
from src.pipeline import LiveRefused, PipelineStopped, run_pipeline
from tests.intake_helpers import read, rewrite, write_raw
from tests.test_integration_pipeline import NOW, named_world

ROOT = Path(__file__).resolve().parent.parent
CFG = load_config(ROOT / "config" / "config.yaml")


def mk(tmp_path, **kw):
    w = named_world(); w.zoominfo_rows = []
    return write_raw(w, tmp_path / "raw", NOW, owner=CFG.owner.nooks_user_id, **kw), w


def live(raw, tmp_path, now=NOW):
    return run_pipeline(raw, CFG, tmp_path / "state", now, live=True, record_sheet=False, log_dir=tmp_path / "logs")


def ok(raw, tmp_path, now=NOW):
    try:
        live(raw, tmp_path, now); return True
    except PipelineStopped:
        return False


def test_baseline_live(tmp_path):
    raw, _ = mk(tmp_path); assert ok(raw, tmp_path)

@pytest.mark.parametrize("kw", [dict(calls_last_page=False), dict(include_gmail=False), dict(credits=None), dict(credits=2),
                                dict(nooks_calls=[]), dict(include_tasks=False)])
def test_gate_blocks(tmp_path, kw):
    raw, _ = mk(tmp_path, **kw)
    assert not ok(raw, tmp_path), kw

def test_gate_other_owner_blocks(tmp_path):
    raw, w = mk(tmp_path, keep_call_owners=True)
    rewrite(raw, "nooks_calls", lambda d: [r.__setitem__("owner", {"id": "OTHER"}) for r in d["rows"]])
    assert not ok(raw, tmp_path)

def test_gate_stale_future(tmp_path):
    raw, _ = mk(tmp_path)
    assert not ok(raw, tmp_path, NOW + timedelta(hours=25))
    assert not ok(raw, tmp_path, NOW - timedelta(hours=2))

def test_gate_owner_mismatch(tmp_path):
    raw, _ = mk(tmp_path)
    rewrite(raw, "run_meta", lambda d: d.__setitem__("nooks_user_id", "X")); assert not ok(raw, tmp_path)

def test_gate_unknown_disposition(tmp_path):
    raw, _ = mk(tmp_path)
    rewrite(raw, "nooks_calls", lambda d: d["rows"][0].__setitem__("callDisposition", {"id": "NOPE"}))
    assert not ok(raw, tmp_path)

def test_gate_floor_first_run(tmp_path):
    raw, w = mk(tmp_path, nooks_calls=w_calls(w=None) if False else None) if False else (None, None)
    w = named_world(); w.zoominfo_rows = []
    raw = write_raw(w, tmp_path / "raw", NOW, owner=CFG.owner.nooks_user_id, nooks_calls=w.calls[:3])
    assert not ok(raw, tmp_path), "3 calls on a first run must block live"

def test_gate_floor_bypass_via_state_file_exists(tmp_path):
    """Floor only applies when st.is_new: an attacker/accident creating ANY state.json removes it."""
    w = named_world(); w.zoominfo_rows = []
    raw = write_raw(w, tmp_path / "raw", NOW, owner=CFG.owner.nooks_user_id, nooks_calls=w.calls[:3])
    sd = tmp_path / "state"; sd.mkdir()
    full = write_raw(w, tmp_path / "full", NOW, owner=CFG.owner.nooks_user_id)
    run_pipeline(full, CFG, sd, NOW)                  # dry run on full data creates state
    # wipe seen calls but keep file valid
    p = sd / "state.json"; d = json.loads(p.read_text()); d["seen_calls"] = []; p.write_text(json.dumps(d))
    print("floor bypass with emptied-seen state ->", "live OK" if ok(raw, tmp_path) else "blocked")

def test_reset_state_blocks(tmp_path):
    raw, _ = mk(tmp_path)
    sd = tmp_path / "state"; sd.mkdir(); (sd / "state.json").write_text("{bad")
    assert not ok(raw, tmp_path)

def test_prospects_optional(tmp_path):
    raw, _ = mk(tmp_path, include_prospects=False); assert ok(raw, tmp_path)
    raw2, _ = mk(tmp_path / "b", nooks_prospects=[]); assert ok(raw2, tmp_path / "b")

def test_dry_never_sheet(tmp_path):
    raw, _ = mk(tmp_path)
    r = run_pipeline(raw, CFG, tmp_path / "s", NOW); assert not r.sheet_recorded
    with pytest.raises(ValueError): run_pipeline(raw, CFG, tmp_path / "s", NOW, record_sheet=True)

def test_surrogate_never_in_log_result_or_marked_prospects(tmp_path):
    raw, _ = mk(tmp_path)
    r = run_pipeline(raw, CFG, tmp_path / "s", NOW, log_dir=tmp_path / "l")
    assert "sfc:" not in Path(r.log_path).read_text()
    assert all(not (p.nooks_id or "").startswith("sfc:") for p in r.prospects)
    assert all(not (p.nooks_id or "").startswith("sfc:") for ps in r.rank_result.prospects.values() for p in ps)
    st = (tmp_path / "s" / "state.json").read_text()
    print("sfc: keys persisted in state.json:", st.count("sfc:"))

def test_forged_tags():
    from datetime import date
    assert parse_sheet_tag("[apac-callsheet 2026-02-30 #1]") is None
    assert parse_sheet_tag("x" * 5_000_000 + " [apac-callsheet 2026-11-24 #3 acct=001ABCDEFGHIJKL]")[2]
    multi = "[apac-callsheet 2026-11-20 #1] and later [apac-callsheet 2026-11-24 #2]"
    print("multi tag in one note ->", parse_sheet_tag(multi))
    assert parse_sheet_tag("[apac-callsheet 2026-11-24 #1 acct=bad id]") is None or True
    print("acct=short ->", parse_sheet_tag("[apac-callsheet 2026-11-24 #1 acct=001]"))
    print("no-tag-bracket ->", parse_sheet_tag("apac-callsheet 2026-11-24 #1"))
    print("acct 16 chars ->", parse_sheet_tag("[apac-callsheet 2026-11-24 #1 acct=001ABCDEFGHIJKLM]"))


def _dnc_id(w):
    return next(d["id"] for d in w.dispositions if d["name"] == "Do Not Call")


def test_dnc_call_external_to_unresolved_prospect_id_is_invisible(tmp_path):
    """A Do Not Call on a Nooks prospect id whose `to` is 'external', and the Contact has no Nooks id (the normal
    case now that prospects are not bulk fetched): the Contact must be blocked, or at least counted in the log."""
    w = named_world(); w.zoominfo_rows = []
    target = w.contacts[0]                                   # some contact; make it the DNC victim
    calls = [dict(c) for c in w.calls] + [{
        "id": "FAKE-CALL-DNC-X", "source": "nooks_sep", "duration": 5, "to": "external", "direction": "outgoing",
        "prospect": {"id": "NK-UNRESOLVED-1"}, "callDisposition": {"id": _dnc_id(w)},
        "owner": {"id": CFG.owner.nooks_user_id}, "time": "2026-11-20T03:00:00Z", "createdAt": "2026-11-20T03:00:00Z"}]
    raw = write_raw(w, tmp_path / "raw", NOW, owner=CFG.owner.nooks_user_id, nooks_calls=calls, include_prospects=False)
    r = run_pipeline(raw, CFG, tmp_path / "s", NOW, log_dir=tmp_path / "l")
    txt = Path(r.log_path).read_text()
    print([l for l in txt.splitlines() if "NK-UNRESOLVED-1" in l or "do-not-call" in l.lower() or "unresolved" in l.lower()][:8])
    st = json.loads((tmp_path / "s" / "state.json").read_text())
    assert "nk-unresolved-1" in [x.lower() for x in st.get("dnc_prospect_ids", [])]


def test_shared_phone_two_contacts_dnc_by_phone_blocks_both_not_wrong_one(tmp_path):
    w = named_world(); w.zoominfo_rows = []
    c0, c1 = dict(w.contacts[0]), dict(w.contacts[1])
    shared = "+61299990000"
    c0["Phone"] = shared; c0["MobilePhone"] = None; c1["Phone"] = shared; c1["MobilePhone"] = None
    contacts = [c0, c1] + [dict(c) for c in w.contacts[2:]]
    calls = [dict(c) for c in w.calls] + [{
        "id": "FAKE-CALL-DNC-Y", "source": "nooks_sep", "duration": 5, "to": shared, "direction": "outgoing",
        "prospect": None, "callDisposition": {"id": _dnc_id(w)}, "owner": {"id": CFG.owner.nooks_user_id},
        "time": "2026-11-20T03:00:00Z", "createdAt": "2026-11-20T03:00:00Z"}]
    raw = write_raw(w, tmp_path / "raw", NOW, owner=CFG.owner.nooks_user_id, nooks_calls=calls, sf_contacts=contacts,
                    include_prospects=False)
    r = run_pipeline(raw, CFG, tmp_path / "s", NOW)
    by_c = {p.sf_contact_id: p for p in r.prospects}
    from src.loaders import normalise_sf_id
    a, b = by_c[normalise_sf_id(c0["Id"])], by_c[normalise_sf_id(c1["Id"])]
    print("shared-phone DNC: contact0 blocked", a.do_not_call, "contact1 blocked", b.do_not_call)
    assert a.do_not_call and b.do_not_call            # over-block is the safe direction; attaching to only one is wrong


def test_infer_ids_attacks(tmp_path):
    from src.intake import infer_prospect_ids_from_calls, load_raw
    from src.models import Prospect
    w = named_world(); w.zoominfo_rows = []
    raw = write_raw(w, tmp_path / "raw", NOW, owner=CFG.owner.nooks_user_id)
    d = load_raw(raw, now=NOW)
    P = lambda cid, ph: Prospect(nooks_id=None, sf_contact_id=cid, sf_account_id="001A", nooks_account_id=None, name="n", title="t",
                                email=None, phone_e164=ph, opted_out_email=False)
    from src.loaders import call_from_nooks
    def C(i, pid, to):
        row = {"id": i, "duration": 5, "to": to, "prospect": {"id": pid}, "callDisposition": {"id": w.dispositions[0]["id"]},
               "owner": {"id": CFG.owner.nooks_user_id}, "time": "2026-11-01T00:00:00Z", "createdAt": "2026-11-01T00:00:00Z"}
        return call_from_nooks(row, d.disposition_index)
    # 1. one prospect id, two phones -> unresolved
    d.calls = [C("a", "NK1", "+61411111111"), C("b", "NK1", "+61422222222")]
    out = infer_prospect_ids_from_calls(d, [P("003A", "+61411111111"), P("003B", "+61422222222")])
    assert all(p.nooks_id is None for p in out)
    # 2. phone shared by two contacts -> unresolved
    d.calls = [C("a", "NK1", "+61411111111")]
    out = infer_prospect_ids_from_calls(d, [P("003A", "+61411111111"), P("003B", "+61411111111")])
    assert all(p.nooks_id is None for p in out)
    # 3. two different Nooks ids both dialled the same single-contact phone (duplicate prospects): first wins, second left
    d.calls = [C("a", "NK1", "+61411111111"), C("b", "NK2", "+61411111111")]
    out = infer_prospect_ids_from_calls(d, [P("003A", "+61411111111")])
    print("two nooks ids, one phone ->", [p.nooks_id for p in out])
    # 4. the Nooks prospect was the person's COLLEAGUE who answered/transferred: same number (switchboard) but one contact has it
    d.calls = [C("a", "NK-COLLEAGUE", "+61411111111")]
    out = infer_prospect_ids_from_calls(d, [P("003A", "+61411111111")])
    print("colleague on a number that only ONE contact lists ->", [p.nooks_id for p in out], "(WRONG-PERSON ATTACH POSSIBLE)")


def test_cooldown_lost_when_call_prospect_ids_unresolved(tmp_path):
    """Recent calls carry Nooks prospect ids. With nooks_prospects absent (the new normal) and phone inference unable
    to resolve them (shared / missing phones), the account-level cooldown is not applied."""
    w = named_world(); w.zoominfo_rows = []
    raw_a = write_raw(w, tmp_path / "a", NOW, owner=CFG.owner.nooks_user_id)                      # prospects present
    raw_b = write_raw(w, tmp_path / "b", NOW, owner=CFG.owner.nooks_user_id, include_prospects=False)
    # blank every phone so inference cannot resolve ids
    for raw in (raw_b,):
        rewrite(raw, "sf_contacts", lambda d: [r.update({"Phone": None, "MobilePhone": None}) for r in d["rows"]])
    a = run_pipeline(raw_a, CFG, tmp_path / "sa", NOW)
    b = run_pipeline(raw_b, CFG, tmp_path / "sb", NOW)
    ca = {k: v for k, v in a.state.accounts.items()}
    cb = {k: v for k, v in b.state.accounts.items()}
    print("accounts with call state: with prospects", len(ca), "| without prospect file and no phones", len(cb))
    print("recorded without account:", b.state.report["record_calls"]["no_account"], "of", b.state.report["record_calls"]["calls_seen"])
    assert len(cb) > 0
