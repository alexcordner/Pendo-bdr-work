"""Tests for src/pipeline.py (task 2a). Offline, synthetic. The raw dump is written from the 1g integration world
(tests/test_integration_pipeline.py) so the pipeline can be compared with that harness."""
import hashlib
import json
import random
import re
import shutil
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import accounts as acc_mod
from src import pipeline as pl
from src import ranker, state as st_mod
from src.config import load_config
from src.intake import RUN_META
from src.pipeline import LiveRefused, PipelineStopped, run_pipeline
from tests.intake_helpers import read, rewrite, write_raw
from tests.test_integration_pipeline import NOW, Builder, named_world, run_pipeline as harness

ROOT = Path(__file__).resolve().parent.parent
LATE = datetime(2026, 11, 25, 5, 12, tzinfo=timezone.utc)            # Wed 16:12 AEDT: Sydney spills


@pytest.fixture(scope="module")
def cfg():
    return load_config(ROOT / "config" / "config.yaml")


@pytest.fixture
def world():
    w = named_world()
    w.zoominfo_rows = []         # the pipeline passes no ZoomInfo company rows (free search is not fetched)
    return w


@pytest.fixture
def raw(world, cfg, tmp_path):
    return write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id)


def run(raw, cfg, tmp_path, now=NOW, **kw):
    kw.setdefault("log_dir", tmp_path / "logs")
    return run_pipeline(raw, cfg, tmp_path / "state", now, **kw)


def keys(result):
    return [ranker.account_key(r.account) for r in result.rank_result.ranked]


def names(items):
    return [r.account.name for r in items]


def snapshot(rr):
    """Everything observable about a RankResult, comparable between the harness and the pipeline."""
    def one(x):
        return (ranker.account_key(x.account), x.account.name, x.score, x.window, str(x.local_call_time),
                tuple(x.reasons), x.skipped_reason)
    return {"ranked": [one(x) for x in rr.ranked], "needs": [one(x) for x in rr.needs_prospect],
            "spilled": [one(x) for x in rr.spilled], "skipped": [one(x) for x in rr.skipped],
            "codes": dict(sorted(rr.needs_prospect_codes.items())),
            "prospects": {k: [p.nooks_id for p in v] for k, v in sorted(rr.prospects.items())}}


# ---------------------------------------------------------------- parity with the Phase 1 harness
@pytest.mark.parametrize("now", [NOW, LATE])
def test_same_ranked_ids_as_the_integration_harness(world, raw, cfg, tmp_path, now):
    h = harness(world, tmp_path / "hstate", now=now, live=False, mark_sheet=False)
    r = run(raw, cfg, tmp_path, now=now)
    assert keys(r) == [ranker.account_key(x.account) for x in h.result.ranked]
    assert len(keys(r)) > 5


@pytest.mark.parametrize("now", [NOW, LATE])
def test_full_rank_result_matches_the_harness(world, raw, cfg, tmp_path, now):
    h = harness(world, tmp_path / "hstate", now=now, live=False, mark_sheet=False)
    r = run(raw, cfg, tmp_path, now=now)
    assert snapshot(r.rank_result) == snapshot(h.result)
    assert [(d.name, d.reason) for d in r.accounts_result.dropped] == [(d.name, d.reason) for d in h.accounts.dropped]


def test_signals_and_verdicts_match_the_harness(world, raw, cfg, tmp_path):
    h = harness(world, tmp_path / "hstate", live=False, mark_sheet=False)
    r = run(raw, cfg, tmp_path)
    assert {k: [(s.kind, s.strength, s.source) for s in v] for k, v in r.signals.items()} == \
           {k: [(s.kind, s.strength, s.source) for s in v] for k, v in h.signals.items()}
    assert [(v.scoop.id, v.keep, v.reason) for v in r.verdicts] == [(v.scoop.id, v.keep, v.reason) for v in h.verdicts]


def test_state_dnc_matches_the_harness(world, raw, cfg, tmp_path):
    h = harness(world, tmp_path / "hstate", live=False, mark_sheet=False)
    r = run(raw, cfg, tmp_path)
    assert r.state.dnc_prospect_ids == h.state_after.dnc_prospect_ids
    assert r.state.dnc_phones == h.state_after.dnc_phones and r.state.dnc_emails == h.state_after.dnc_emails
    assert r.stats["prospects_blocked_from_state"] == h.n_prospects_blocked


def test_derived_nooks_links_match_the_world_links_when_prospects_exist(world, cfg, tmp_path):
    stripped = write_raw(world, tmp_path / "raw2", NOW, owner=cfg.owner.nooks_user_id, strip_links=True)
    r = run(stripped, cfg, tmp_path)
    assert r.stats["nooks_accounts_linked"] >= 1
    assert r.intake_report.stats["nooks_accounts_linked_to_salesforce"] == r.stats["nooks_accounts_linked"]


# ---------------------------------------------------------------- result shape
def test_result_shape(raw, cfg, tmp_path):
    r = run(raw, cfg, tmp_path)
    for f in ("accounts_result", "verdicts", "signals", "rank_result", "prospects", "state", "intake_report",
              "log_lines", "stats", "zoominfo_credits"):
        assert hasattr(r, f)
    assert isinstance(r.rank_result, ranker.RankResult) and isinstance(r.accounts_result, acc_mod.AccountListResult)
    assert r.intake_report.ok and r.zoominfo_credits == 0 and r.live is False
    assert r.run_date.isoformat() == "2026-11-25" and r.call_date.isoformat() == "2026-11-25"
    assert r.stats["ranked"] == len(r.rank_result.ranked) and r.stats["accounts_kept"] == len(r.accounts_result.accounts)


# ---------------------------------------------------------------- dry run: never a sheet
def test_dry_run_never_records_a_sheet(raw, cfg, tmp_path):
    r = run(raw, cfg, tmp_path)
    assert r.live is False and r.sheet_recorded is False and r.state.sheets == []
    assert st_mod.load_state(tmp_path / "state").sheets == []
    assert any("sheet history recorded: NO" in l for l in r.log_lines)


def test_dry_run_is_repeatable(raw, cfg, tmp_path):
    a = run(raw, cfg, tmp_path)
    b = run(raw, cfg, tmp_path)
    assert keys(a) == keys(b) and len(keys(b)) > 0


def test_dry_run_saves_calls_and_dnc_but_not_sheets(raw, cfg, tmp_path):
    run(raw, cfg, tmp_path)
    st = st_mod.load_state(tmp_path / "state")
    assert st_mod.dnc_is_rebuilt(st) and st.dnc_prospect_ids and st.sheets == []


def test_save_state_false_leaves_state_untouched(raw, cfg, tmp_path):
    run(raw, cfg, tmp_path, save_state=False)
    assert not (tmp_path / "state" / "state.json").exists()


def test_dry_run_with_record_sheet_is_a_programming_error(raw, cfg, tmp_path):
    with pytest.raises(ValueError, match="dry run never records"):
        run(raw, cfg, tmp_path, record_sheet=True)


def test_raw_files_are_never_modified(raw, cfg, tmp_path):
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in raw.iterdir()}
    run(raw, cfg, tmp_path)
    run(raw, cfg, tmp_path, live=True)
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in raw.iterdir()} == before


# ---------------------------------------------------------------- live
def test_live_records_the_sheet_for_ranked_only(raw, cfg, tmp_path):
    r = run(raw, cfg, tmp_path, now=LATE, live=True)
    assert r.live and r.sheet_recorded and len(r.rank_result.spilled) > 0
    sheet = r.state.sheets[-1]
    ranked_ids = {st_mod.normalise_sf_id(x.account.sf_account_id) for x in r.rank_result.ranked}
    assert {st_mod.normalise_sf_id(i) for i in sheet["account_ids"]} == ranked_ids
    other = {st_mod.normalise_sf_id(x.account.sf_account_id) for x in
             r.rank_result.spilled + r.rank_result.needs_prospect + r.rank_result.skipped}
    assert not (other & ranked_ids) and not (other & {st_mod.normalise_sf_id(i) for i in sheet["account_ids"]})
    assert st_mod.load_state(tmp_path / "state").sheets[-1]["run_date"] == "2026-11-25"


def test_live_with_record_sheet_false_records_nothing(raw, cfg, tmp_path):
    r = run(raw, cfg, tmp_path, live=True, record_sheet=False)
    assert r.live and not r.sheet_recorded and r.state.sheets == []


def test_live_works_on_a_brand_new_state_when_the_history_is_complete(raw, cfg, tmp_path):
    assert st_mod.load_state(tmp_path / "state").is_new
    r = run(raw, cfg, tmp_path, live=True)
    assert st_mod.dnc_is_rebuilt(r.state)


def test_live_next_night_skips_last_sheet(raw, cfg, tmp_path):
    first = run(raw, cfg, tmp_path, live=True)
    nxt = datetime(2026, 11, 26, 3, 0, tzinfo=timezone.utc)
    rewrite(raw, "nooks_calls", lambda d: None)             # unchanged data, refreshed time below
    for n in ("sf_accounts", "sf_contacts", "nooks_calls", "nooks_dispositions", "nooks_prospects", "nooks_accounts",
              "zoominfo_scoops", "gmail_sent"):
        rewrite(raw, n, lambda d: d["meta"].__setitem__("fetched_at", "2026-11-26T02:30:00Z"))
    rewrite(raw, RUN_META, lambda d: d.__setitem__("fetched_at", "2026-11-26T02:30:00Z"))
    second = run(raw, cfg, tmp_path, now=nxt, live=True)
    first_ids = {ranker.account_key(x.account) for x in first.rank_result.ranked}
    skipped_last = {ranker.account_key(x.account) for x in second.rank_result.skipped if x.skipped_reason == "on last sheet"}
    assert first_ids & skipped_last


def test_live_refused_when_calls_did_not_reach_the_last_page(world, cfg, tmp_path):
    raw = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id, calls_last_page=False)
    with pytest.raises(LiveRefused) as e:
        run(raw, cfg, tmp_path, live=True)
    assert any("nooks_calls" in r and "last page" in r for r in e.value.reasons)
    assert not (tmp_path / "state" / "state.json").exists()            # refused before anything was recorded


def test_old_marker_cannot_rescue_a_partial_history(world, cfg, tmp_path):
    ok = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id)
    run(ok, cfg, tmp_path)                                              # a good run: the marker is set
    assert st_mod.dnc_is_rebuilt(st_mod.load_state(tmp_path / "state"))
    bad = write_raw(world, tmp_path / "raw2", NOW, owner=cfg.owner.nooks_user_id, calls_last_page=False)
    with pytest.raises(LiveRefused):
        run(bad, cfg, tmp_path, live=True)
    r = run(bad, cfg, tmp_path)                                         # a dry run still works, with a warning
    assert not r.intake_report.calls_reached_last_page
    assert any("did NOT reach the last page" in l for l in r.log_lines)


def test_dry_run_with_partial_history_does_not_set_the_marker(world, cfg, tmp_path):
    bad = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id, calls_last_page=False)
    r = run(bad, cfg, tmp_path)
    assert not st_mod.dnc_is_rebuilt(r.state)
    assert any("Do Not Call list is NOT trusted" in l for l in r.log_lines)
    assert r.state.sheets == []


def test_partial_history_after_a_full_one_is_refused_for_live(world, cfg, tmp_path):
    full = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id)
    run(full, cfg, tmp_path)
    fewer = write_raw(world, tmp_path / "raw2", NOW, owner=cfg.owner.nooks_user_id, nooks_calls=world.calls[:3])
    with pytest.raises(LiveRefused) as e:
        run(fewer, cfg, tmp_path, live=True)
    assert any("rebuild rejected" in r or "partial" in r for r in e.value.reasons)


def test_dry_run_with_partial_history_after_a_full_one_warns(world, cfg, tmp_path):
    full = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id)
    run(full, cfg, tmp_path)
    fewer = write_raw(world, tmp_path / "raw2", NOW, owner=cfg.owner.nooks_user_id, nooks_calls=world.calls[:3])
    r = run(fewer, cfg, tmp_path)
    assert any("rebuild rejected" in l for l in r.log_lines)
    assert st_mod.load_state(tmp_path / "state").dnc_prospect_ids          # earlier DNC memory not lost


@pytest.mark.parametrize("mutate,needle", [
    (lambda r: rewrite(r, "gmail_sent", lambda d: None) or (r / "gmail_sent.json").unlink(), "gmail_sent"),
    (lambda r: rewrite(r, RUN_META, lambda d: d.pop("zoominfo_credits_used")), "credits"),
    (lambda r: rewrite(r, RUN_META, lambda d: d.__setitem__("zoominfo_credits_used", 2)), "credit budget"),
])
def test_live_refusals_from_intake(raw, cfg, tmp_path, mutate, needle):
    if "gmail" in needle:
        (raw / "gmail_sent.json").unlink()
    else:
        mutate(raw)
    with pytest.raises(LiveRefused) as e:
        run(raw, cfg, tmp_path, live=True)
    assert any(needle in r for r in e.value.reasons)
    r = run(raw, cfg, tmp_path)                                         # dry run is fine
    assert r.live is False


def test_live_refused_for_stale_data(raw, cfg, tmp_path):
    with pytest.raises(LiveRefused) as e:
        run(raw, cfg, tmp_path, now=datetime(2026, 11, 26, 4, 0, tzinfo=timezone.utc), live=True)
    assert any("older than 24 hours" in r for r in e.value.reasons)


def test_live_refused_for_malformed_calls(raw, cfg, tmp_path):
    def f(d):
        d["rows"].append({"id": "BAD-CALL", "duration": 1})
    rewrite(raw, "nooks_calls", f)
    rewrite(raw, RUN_META, lambda d: d["row_counts"].__setitem__("nooks_calls", d["row_counts"]["nooks_calls"] + 1))
    with pytest.raises(LiveRefused) as e:
        run(raw, cfg, tmp_path, live=True)
    assert any("malformed" in r for r in e.value.reasons)


def test_live_refusal_is_logged_and_writes_a_log(raw, cfg, tmp_path):
    (raw / "gmail_sent.json").unlink()
    with pytest.raises(LiveRefused):
        run(raw, cfg, tmp_path, live=True)
    text = next((tmp_path / "logs").glob("*_pipeline.log")).read_text()
    assert "== STOPPED ==" in text and "live refused" in text


# ---------------------------------------------------------------- stops
def test_missing_disposition_stops_the_run(raw, cfg, tmp_path):
    rows = [r for r in read(raw, "nooks_dispositions")["rows"] if r["name"] != cfg.dispositions.do_not_call]
    rewrite(raw, "nooks_dispositions", lambda d: d.__setitem__("rows", rows))
    rewrite(raw, RUN_META, lambda d: d["row_counts"].__setitem__("nooks_dispositions", len(rows)))
    with pytest.raises(PipelineStopped) as e:
        run(raw, cfg, tmp_path)
    assert "Do Not Call" in e.value.reasons[0]
    assert not (tmp_path / "state" / "state.json").exists()            # nothing recorded


def test_archived_disposition_counts_as_missing(raw, cfg, tmp_path):
    def f(d):
        for r in d["rows"]:
            if r["name"] == cfg.dispositions.voicemail:
                r["archived"] = True
    rewrite(raw, "nooks_dispositions", f)
    with pytest.raises(PipelineStopped):
        run(raw, cfg, tmp_path)


def test_intake_errors_stop_the_run_and_are_logged(raw, cfg, tmp_path):
    (raw / "sf_contacts.json").unlink()
    with pytest.raises(PipelineStopped) as e:
        run(raw, cfg, tmp_path)
    assert any("sf_contacts" in r for r in e.value.reasons)
    log = next((tmp_path / "logs").glob("*.log")).read_text()
    assert "== STOPPED ==" in log and "required file missing: sf_contacts.json" in log
    assert not (tmp_path / "state" / "state.json").exists()


def test_count_mismatch_stops_the_run(raw, cfg, tmp_path):
    rewrite(raw, RUN_META, lambda d: d["row_counts"].__setitem__("sf_accounts", 1))
    with pytest.raises(PipelineStopped, match="run_meta says 1 rows"):
        run(raw, cfg, tmp_path)


def test_wrong_owner_stops_the_run(raw, cfg, tmp_path):
    rewrite(raw, RUN_META, lambda d: d.__setitem__("nooks_user_id", "FAKE-NOOKS-USER-SOMEONE-ELSE"))
    with pytest.raises(PipelineStopped, match="different Nooks user"):
        run(raw, cfg, tmp_path)


def test_stop_without_log_dir_still_raises_with_log_lines(raw, cfg, tmp_path):
    (raw / "sf_contacts.json").unlink()
    with pytest.raises(PipelineStopped) as e:
        run_pipeline(raw, cfg, tmp_path / "state", NOW)
    assert any("== STOPPED ==" in l for l in e.value.log_lines)


def test_naive_now_and_bad_limit_are_refused(raw, cfg, tmp_path):
    with pytest.raises(ValueError):
        run_pipeline(raw, cfg, tmp_path / "s", datetime(2026, 11, 25))
    for bad in (0, -1, True, 2.5):
        with pytest.raises(ValueError):
            run(raw, cfg, tmp_path, limit=bad)


# ---------------------------------------------------------------- limit
def test_limit_lowers_the_cap(raw, cfg, tmp_path):
    full = run(raw, cfg, tmp_path, save_state=False)
    small = run(raw, cfg, tmp_path, limit=3, save_state=False)
    assert len(full.rank_result.ranked) > 3 and len(small.rank_result.ranked) == 3
    assert keys(small) == keys(full)[:3]


def test_limit_cannot_raise_the_cap(raw, cfg, tmp_path):
    capped = replace(cfg, limits=replace(cfg.limits, daily_call_cap=4))
    r = run(raw, capped, tmp_path, limit=40, save_state=False)
    assert len(r.rank_result.ranked) == 4


# ---------------------------------------------------------------- safety on the ranked result
def test_no_blocked_prospect_is_chosen_and_state_marks_dnc(raw, cfg, tmp_path):
    r = run(raw, cfg, tmp_path)
    assert any(p.do_not_call for p in r.prospects)
    for ps in r.rank_result.prospects.values():
        assert not any(ranker.is_blocked(p) for p in ps)
    assert set(r.rank_result.prospects) <= set(keys(r))


def test_only_ranked_accounts_have_chosen_prospects(raw, cfg, tmp_path):
    r = run(raw, cfg, tmp_path, now=LATE)
    ranked = set(keys(r))
    assert set(r.rank_result.prospects) == ranked
    not_tasks = {ranker.account_key(x.account) for x in
                 r.rank_result.spilled + r.rank_result.needs_prospect + r.rank_result.skipped}
    assert not (not_tasks & set(r.rank_result.prospects))


# ---------------------------------------------------------------- determinism
def shuffle_all(raw, seed):
    rng = random.Random(seed)
    for p in raw.glob("*.json"):
        if p.name == "run_meta.json":
            continue
        d = json.loads(p.read_text())
        rng.shuffle(d["rows"])
        p.write_text(json.dumps(d))


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_shuffled_files_give_identical_results(world, cfg, tmp_path, seed):
    a_raw = write_raw(world, tmp_path / "a", NOW, owner=cfg.owner.nooks_user_id)
    b_raw = write_raw(world, tmp_path / "b", NOW, owner=cfg.owner.nooks_user_id)
    shuffle_all(b_raw, seed)
    a = run_pipeline(a_raw, cfg, tmp_path / "sa", NOW)
    b = run_pipeline(b_raw, cfg, tmp_path / "sb", NOW)
    assert snapshot(a.rank_result) == snapshot(b.rank_result)
    assert [l for l in a.log_lines if not l.startswith("RUN LOG")] == [l for l in b.log_lines if not l.startswith("RUN LOG")]
    assert a.stats == b.stats


def test_identical_across_two_runs_of_the_same_inputs(raw, cfg, tmp_path):
    a = run_pipeline(raw, cfg, tmp_path / "s1", NOW)
    b = run_pipeline(raw, cfg, tmp_path / "s2", NOW)
    assert a.log_lines == b.log_lines


# ---------------------------------------------------------------- run log
def test_log_is_written_with_the_documented_name(raw, cfg, tmp_path):
    r = run(raw, cfg, tmp_path)
    assert r.log_path.parent == tmp_path / "logs"
    assert re.fullmatch(r"2026-11-25_1400_pipeline\.log", r.log_path.name)       # 03:00 UTC = 14:00 AEDT
    assert r.log_path.read_text().startswith("RUN LOG pipeline")


def test_log_is_never_overwritten(raw, cfg, tmp_path):
    a = run(raw, cfg, tmp_path)
    b = run(raw, cfg, tmp_path)
    assert a.log_path != b.log_path and b.log_path.name.endswith("_pipeline-2.log")
    assert a.log_path.exists() and b.log_path.exists()


def test_no_log_dir_means_no_file(raw, cfg, tmp_path):
    r = run_pipeline(raw, cfg, tmp_path / "state", NOW)
    assert r.log_path is None and not (tmp_path / "logs").exists()


def test_log_has_no_emails_or_phone_numbers(raw, cfg, tmp_path):
    r = run(raw, cfg, tmp_path, now=LATE)
    text = r.log_path.read_text()
    assert "@" not in text and "example.com" not in text
    assert not re.search(r"\+\d{8,}", text) and not re.search(r"\b0[2-478]\d{8}\b", text)
    # the raw world does contain them: the test is meaningful
    assert "@" in (raw / "sf_contacts.json").read_text() and "+614" in (raw / "sf_contacts.json").read_text()


def test_log_names_every_skipped_dropped_needs_and_spilled_account(raw, cfg, tmp_path):
    r = run(raw, cfg, tmp_path, now=LATE)
    text = r.log_path.read_text()
    rr = r.rank_result
    assert rr.spilled and rr.skipped and rr.needs_prospect and r.accounts_result.dropped
    for x in rr.skipped + rr.needs_prospect + rr.spilled + rr.ranked:
        assert x.account.name in text, x.account.name
    for d in r.accounts_result.dropped:
        assert (d.name or "(no name)") in text and d.reason in text
    for sec in ("== INTAKE ==", "== STATE ==", "== SIGNALS ==", "== ACCOUNTS ==", "== RANKER ==", "== RESULT =="):
        assert sec in text


def test_log_lists_every_scoop_verdict_and_unmatched_scoops(raw, cfg, tmp_path):
    r = run(raw, cfg, tmp_path)
    text = r.log_path.read_text()
    for v in r.verdicts:
        assert v.scoop.id in text
    assert "kept scoops with no matching account" in text


def test_log_uses_the_existing_summarise_functions(raw, cfg, tmp_path):
    r = run(raw, cfg, tmp_path)
    text = "\n".join(r.log_lines)
    assert acc_mod.summarise(r.accounts_result) in text
    assert "Ranked " in text and "accounts kept:" in text


def test_log_says_nothing_was_written(raw, cfg, tmp_path):
    text = "\n".join(run(raw, cfg, tmp_path).log_lines)
    assert "Nooks tasks / Gmail drafts written: NONE" in text and "Salesforce is read-only" in text


def test_bulk_non_anz_drops_are_counted_not_listed(world, cfg, tmp_path):
    b = Builder()
    for n in range(1, 60):
        b.account(n, f"Overseas {n:03d} Inc", country="United States", hq_country="United States", emp=200)
    b.account(100, "Local Pty Ltd", state="New South Wales", emp=200)
    b.prospect(100, 1, "Head of Product")
    raw = write_raw(b.w, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id)
    r = run(raw, cfg, tmp_path)
    text = r.log_path.read_text()
    assert f"dropped - {acc_mod.R_NOT_ANZ} (59)" in text
    assert "and 34 more (counted, names not listed)" in text
    assert "Overseas 001 Inc" in text and "Overseas 059 Inc" not in text


def test_scrub_log_removes_contact_details():
    s = pl.scrub_log("a jane.doe@example.com b +61 412 345 678 c 0412 345 678 d FAKE-SCOOP-00001 e 001FAKE00001234")
    assert "@" not in s and "412 345" not in s and "FAKE-SCOOP-00001" in s and "001FAKE00001234" in s


# ---------------------------------------------------------------- credits (rule 7)
def test_credits_are_reported_and_logged(raw, cfg, tmp_path):
    r = run(raw, cfg, tmp_path)
    assert r.zoominfo_credits == 0
    assert any("ZoomInfo credits used this run (rule 7): 0 (budget 0)" in l for l in r.log_lines)


def test_credits_over_budget_are_a_warning_and_a_live_blocker(raw, cfg, tmp_path):
    rewrite(raw, RUN_META, lambda d: d.__setitem__("zoominfo_credits_used", 5))
    r = run(raw, cfg, tmp_path)
    assert r.zoominfo_credits == 5 and not r.intake_report.can_go_live
    assert any("exceed the per-run budget" in l for l in r.log_lines)


def test_unknown_credits_are_none_and_logged_as_unknown(raw, cfg, tmp_path):
    rewrite(raw, RUN_META, lambda d: d.pop("zoominfo_credits_used"))
    r = run(raw, cfg, tmp_path)
    assert r.zoominfo_credits is None and any("UNKNOWN" in l for l in r.log_lines)


# ---------------------------------------------------------------- Do Not Call phone format (backlog)
def test_dnc_call_with_non_e164_to_is_logged_by_id_only(world, cfg, tmp_path):
    calls = [dict(c) for c in world.calls]
    dnc = [c for c in calls if c["callDisposition"]["id"] == "disp-do-not-call"][0]
    dnc["to"] = "0412 345 678"
    raw = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id, nooks_calls=calls)
    r = run(raw, cfg, tmp_path)
    assert r.stats["dnc_calls_phone_not_e164"] >= 1
    text = r.log_path.read_text()
    assert f"DNC call {dnc['id']}" in text and "0412 345 678" not in text and "412345678" not in text


def test_dnc_call_with_unusable_to_is_logged(world, cfg, tmp_path):
    calls = [dict(c) for c in world.calls]
    dnc = [c for c in calls if c["callDisposition"]["id"] == "disp-do-not-call"][0]
    dnc["to"] = "n/a"
    raw = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id, nooks_calls=calls)
    r = run(raw, cfg, tmp_path)
    assert any(f"DNC call {dnc['id']}: 'to' could not be normalised" in l for l in r.log_lines)


def test_clean_dnc_numbers_log_nothing(raw, cfg, tmp_path):
    assert run(raw, cfg, tmp_path).stats["dnc_calls_phone_not_e164"] == 0


# ---------------------------------------------------------------- tasks, joins
def test_our_open_tasks_are_counted(world, cfg, tmp_path):
    tasks = [{"id": "T1", "action": "call", "status": "ready", "completed": False},
             {"id": "T2", "action": "call", "status": "finished", "completed": True}]
    raw = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id, include_tasks=True, nooks_tasks=tasks)
    assert run(raw, cfg, tmp_path).stats["our_open_nooks_tasks"] == 1


def test_prospect_without_contact_row_cannot_be_called(world, cfg, tmp_path):
    keep = world.contacts[1:]
    raw = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id, sf_contacts=keep)
    r = run(raw, cfg, tmp_path)
    assert r.intake_report.stats["nooks_rows_without_contact_row"] == 1
    assert any("no Salesforce Contact row" in l for l in r.log_lines)


# ---------------------------------------------------------------- CLI
def cli(raw, tmp_path, *extra, now=NOW):
    return pl.main(["--raw-dir", str(raw), "--state-dir", str(tmp_path / "state"), "--log-dir", str(tmp_path / "logs"),
                    "--now", now.isoformat(), *extra])


def test_cli_dry_run_is_the_default(raw, tmp_path, capsys):
    assert cli(raw, tmp_path) == 0
    out = capsys.readouterr().out
    assert "Dry run: nothing written" in out and "Ranked " in out and "ZoomInfo credits used: 0" in out
    assert st_mod.load_state(tmp_path / "state").sheets == []
    assert list((tmp_path / "logs").glob("*_pipeline.log"))


def test_cli_explicit_dry_run_flag(raw, tmp_path, capsys):
    assert cli(raw, tmp_path, "--dry-run", "--limit", "2") == 0
    assert "Ranked 2 " in capsys.readouterr().out


def test_cli_live_and_dry_run_are_mutually_exclusive(raw, tmp_path):
    with pytest.raises(SystemExit) as e:
        cli(raw, tmp_path, "--live", "--dry-run")
    assert e.value.code == 2


def test_cli_live_refuses_when_a_precondition_fails(world, cfg, tmp_path, capsys):
    bad = write_raw(world, tmp_path / "bad", NOW, owner=cfg.owner.nooks_user_id, calls_last_page=False)
    assert cli(bad, tmp_path, "--live") == 2
    err = capsys.readouterr().err
    assert "LIVE REFUSED" in err and "nooks_calls" in err
    assert not (tmp_path / "state" / "state.json").exists()


def test_cli_live_with_everything_in_place_still_writes_nothing(raw, tmp_path, capsys):
    assert cli(raw, tmp_path, "--live") == 0
    out = capsys.readouterr().out
    assert "Phase 3 (nooks_writer) owns writing tasks" in out and "recorded no sheet history" in out
    assert st_mod.load_state(tmp_path / "state").sheets == []


def test_cli_reports_a_stop(raw, tmp_path, capsys):
    (raw / "sf_accounts.json").unlink()
    assert cli(raw, tmp_path) == 2
    assert "STOPPED" in capsys.readouterr().err


def test_cli_bad_now_is_rejected(raw, tmp_path):
    with pytest.raises(SystemExit):
        pl.main(["--raw-dir", str(raw), "--now", "next tuesday"])


# ---------------------------------------------------------------- static safety
SOURCES = [ROOT / "src" / "intake.py", ROOT / "src" / "pipeline.py", ROOT / "scripts" / "merge_pages.py"]
FORBIDDEN_CALLS = ["send_message", "sendTaskEmail", "create_draft", "createTask", "createEmail", "createAccountNote",
                   "createProspectNote", "createSobjectRecord", "updateSobjectRecord", "deleteSobjectRecord",
                   "enrich_companies", "enrich_contacts", "account_research", "forward"]


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_connector_write_names_in_source(path):
    text = path.read_text()
    for name in FORBIDDEN_CALLS:
        assert name not in text, f"{name} appears in {path.name}"


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_network_or_process_imports(path):
    text = path.read_text()
    assert not re.search(r"^\s*(import|from)\s+(socket|urllib|requests|http|subprocess|smtplib|ftplib|ssl)\b", text, re.M)


def test_pipeline_does_not_write_outside_state_and_log_dirs():
    text = (ROOT / "src" / "pipeline.py").read_text()
    writes = re.findall(r"\.write_text\(|open\([^)]*['\"]w", text)
    assert len(writes) == 1                    # the run log, via write_log


def test_no_hardcoded_real_ids_in_new_files():
    for p in SOURCES + [ROOT / "docs" / "fetch_runbook.md", ROOT / "docs" / "intake_schema.md"]:
        text = p.read_text()
        assert "5koD9szETWeI1F0DcRTqkoHGPlY2" not in text and "@pendo.io" not in text


# ---------------------------------------------------------------- probe findings (task 2a, round 2)
def p_ok(ps):
    """Contact-built prospects: a Contact id always; a Nooks id only when inferred from a unique call phone."""
    return all(p.sf_contact_id and not (p.nooks_id or "").startswith(pl.SURROGATE) for p in ps)


def test_contacts_only_run_has_no_nooks_ids_and_no_internal_keys_leak(world, cfg, tmp_path):
    raw = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id, include_prospects=False)
    r = run(raw, cfg, tmp_path)
    assert r.intake_report.ok and r.stats["prospects_nooks_id_unresolved"] > 0
    assert all(not (p.nooks_id or "").startswith(pl.SURROGATE) for p in r.prospects)
    for ps in r.rank_result.prospects.values():
        assert p_ok(ps)
    assert r.stats["prospects_nooks_id_resolved"] >= r.stats.get("prospect_ids_inferred_from_call_phone", 0)
    assert len(keys(r)) > 0
    assert r.stats["contacts_with_usable_phone"] > 0


def test_contacts_only_do_not_call_still_blocks_by_phone_and_email(world, cfg, tmp_path):
    with_ids = run(write_raw(world, tmp_path / "a", NOW, owner=cfg.owner.nooks_user_id), cfg, tmp_path / "ra")
    no_ids = run(write_raw(world, tmp_path / "b", NOW, owner=cfg.owner.nooks_user_id, include_prospects=False), cfg,
                 tmp_path / "rb")
    blocked = lambda r: {p.sf_contact_id for p in r.prospects if p.do_not_call}
    assert blocked(with_ids) and blocked(no_ids) <= blocked(with_ids)
    # nobody blocked by phone / email in the full run may be chosen in the contacts-only run
    chosen = {p.sf_contact_id for ps in no_ids.rank_result.prospects.values() for p in ps}
    assert not (chosen & blocked(no_ids))


def test_phone_matched_calls_still_cool_accounts_down_without_nooks_ids(world, cfg, tmp_path):
    b = Builder()
    b.account(1, "Cool Down Pty Ltd", state="New South Wales", emp=300)
    p = b.prospect(1, 1, "Head of Product")
    b.scoop(1, "Funding", "Cool raised $5 million in funding.", 1, topics=("Funding",))
    b.call(p, "No Answer", 1, duration=10, unlinked=True)          # no prospect id: phone only
    raw = write_raw(b.w, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id, include_prospects=False)
    r = run(raw, cfg, tmp_path)
    assert r.stats["accounts_dropped"] >= 1 or any(x.skipped_reason and "cooldown" in x.skipped_reason.lower()
                                                   for x in r.rank_result.skipped)
    assert not names(r.rank_result.ranked)


def test_tagged_task_puts_the_account_on_the_last_sheet(world, cfg, tmp_path):
    base = run(write_raw(world, tmp_path / "a", NOW, owner=cfg.owner.nooks_user_id), cfg, tmp_path / "ra")
    top = base.rank_result.ranked[0].account
    tasks = [{"id": "T1", "action": "call", "status": "finished", "completed": True,
              "note": f"[apac-callsheet 2026-11-24 #1 acct={top.sf_account_id}]", "prospect": {"id": "NK"}},
             {"id": "T2", "action": "call", "status": "ready", "completed": False, "note": "untagged", "prospect": {"id": "NK"}}]
    raw = write_raw(world, tmp_path / "b", NOW, owner=cfg.owner.nooks_user_id, nooks_tasks=tasks)
    r = run(raw, cfg, tmp_path / "rb")
    assert top.name not in names(r.rank_result.ranked)
    assert any(x.account.name == top.name and x.skipped_reason == "on last sheet" for x in r.rank_result.skipped)
    assert r.stats["last_sheet_accounts_from_tasks"] == 1
    assert any("1 from tagged Nooks tasks" in l for l in r.log_lines)


def test_tagged_task_for_today_or_untagged_tasks_change_nothing(world, cfg, tmp_path):
    base = run(write_raw(world, tmp_path / "a", NOW, owner=cfg.owner.nooks_user_id), cfg, tmp_path / "ra")
    top = base.rank_result.ranked[0].account
    tasks = [{"id": "T1", "action": "call", "status": "ready", "completed": False,
              "note": f"[apac-callsheet 2026-11-25 #1 acct={top.sf_account_id}]", "prospect": {"id": "NK"}}]
    r = run(write_raw(world, tmp_path / "b", NOW, owner=cfg.owner.nooks_user_id, nooks_tasks=tasks), cfg, tmp_path / "rb")
    assert keys(r) == keys(base)


def test_state_and_task_memory_are_unioned(world, cfg, tmp_path):
    base = run(write_raw(world, tmp_path / "a", NOW, owner=cfg.owner.nooks_user_id), cfg, tmp_path / "ra")
    a, b = base.rank_result.ranked[0].account, base.rank_result.ranked[1].account
    state = tmp_path / "rc" / "state"
    st_mod.save_state(st_mod.record_sheet(st_mod.State(), datetime(2026, 11, 24).date(), [a.sf_account_id]), state)
    tasks = [{"id": "T1", "action": "call", "status": "finished", "completed": True,
              "note": f"[apac-callsheet 2026-11-24 #1 acct={b.sf_account_id}]", "prospect": {"id": "NK"}}]
    raw = write_raw(world, tmp_path / "c", NOW, owner=cfg.owner.nooks_user_id, nooks_tasks=tasks)
    r = run_pipeline(raw, cfg, state, NOW)
    skipped = {x.account.name for x in r.rank_result.skipped if x.skipped_reason == "on last sheet"}
    assert {a.name, b.name} <= skipped


def test_live_refused_when_tagged_tasks_cannot_be_tied_to_an_account(world, cfg, tmp_path):
    tasks = [{"id": "T1", "action": "call", "status": "finished", "completed": True,
              "note": "[apac-callsheet 2026-11-24 #1]", "prospect": {"id": "NK-NOBODY"}}]
    raw = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id, nooks_tasks=tasks)
    with pytest.raises(LiveRefused) as e:
        run(raw, cfg, tmp_path, live=True)
    assert any("tagged tasks" in r for r in e.value.reasons)
    assert run(raw, cfg, tmp_path).live is False


def test_live_refused_without_a_task_list(raw, cfg, tmp_path):
    (raw / "nooks_tasks.json").unlink()
    with pytest.raises(LiveRefused) as e:
        run(raw, cfg, tmp_path, live=True)
    assert any("nooks_tasks.json missing" in r for r in e.value.reasons)


def test_gmail_hits_are_anchored_to_the_search_day_not_the_run_day(world, cfg, tmp_path):
    raw = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id)
    def f(d):
        d["meta"]["fetched_at"] = "2026-11-24T03:00:00Z"
        for t in d["rows"]:
            for m in t["messages"]:
                m.pop("internalDate", None)               # the live search results carry no date
    rewrite(raw, "gmail_sent", f)
    r = run(raw, cfg, tmp_path)
    firsts = {v["first_seen"] for per in r.state.emails.values() for v in per.values()}
    assert firsts == {"2026-11-24"}
    assert all(v["sent_on"] is None for per in r.state.emails.values() for v in per.values())


def test_gmail_window_shorter_than_the_cooldown_blocks_live(world, cfg, tmp_path):
    raw = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id, gmail_window=3)
    with pytest.raises(LiveRefused) as e:
        run(raw, cfg, tmp_path, live=True)
    assert any("shorter than the email cooldown" in r for r in e.value.reasons)


def test_log_header_states_the_dnc_limitation(raw, cfg, tmp_path):
    text = run(raw, cfg, tmp_path).log_path.read_text()
    head = text.split("== INTAKE ==")[0]
    assert ("DNC visibility: Nooks-dialer calls only (Outreach-sourced calls have no Do Not Call disposition); calls "
            "fetched with the owner filter; dry-run DNC match is by phone/email hash, Nooks prospect id after lazy "
            "resolution in Phase 3") in head and "RE-RUN" in head


def test_log_reports_scoops_matched_to_the_book(raw, cfg, tmp_path):
    r = run(raw, cfg, tmp_path)
    s = r.stats
    assert 0 < s["scoops_matched_to_book"] <= s["scoops"] and s["scoop_companies_matched_to_book"] <= s["scoop_companies"]
    assert any(f"scoops matched to your book: {s['scoops_matched_to_book']} of {s['scoops']}" in l for l in r.log_lines)
    assert any("scoop types in the file:" in l for l in r.log_lines)


def test_log_counts_external_and_odd_to_values(world, cfg, tmp_path):
    calls = [dict(c) for c in world.calls]
    calls[0]["to"], calls[1]["to"] = "external", "+61 (0)4 1234 5678"
    raw = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id, nooks_calls=calls)
    r = run(raw, cfg, tmp_path)
    assert r.stats["calls_to_external"] == 1 and r.stats["calls_to_not_e164_other"] == 1
    assert any("1 literal 'external', 1 other" in l for l in r.log_lines)


def test_per_type_scoop_counts_from_run_meta_are_logged(raw, cfg, tmp_path):
    rewrite(raw, RUN_META, lambda d: d.__setitem__("scoop_counts_by_type", {"Funding": 270, "Executive Move": 1000}))
    assert any("Executive Move=1000, Funding=270" in l for l in run(raw, cfg, tmp_path).log_lines)


def test_unmatched_scoops_are_capped_in_the_log(world, cfg, tmp_path):
    b = Builder()
    b.account(1, "Only Account Pty Ltd", state="New South Wales", emp=300)
    b.prospect(1, 1, "Head of Product")
    for n in range(100, 140):
        b.scoop(n, "Funding", f"Stranger {n} raised $5 million in funding.", 2, topics=("Funding",))
    raw = write_raw(b.w, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id)
    r = run(raw, cfg, tmp_path)
    text = r.log_path.read_text()
    assert r.stats["scoops_matched_to_book"] == 0 and "and 15 more (counted)" in text


def test_calls_from_other_sources_warn_but_do_not_block_a_live_run(world, cfg, tmp_path):
    calls = [dict(c) for c in world.calls]
    calls[0]["source"] = "outreach"
    raw = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id, nooks_calls=calls)
    r = run(raw, cfg, tmp_path, live=True)
    assert any("Outreach" in w for w in r.intake_report.warnings) and r.live


# ---------------------------------------------------------------- review fixes (F1, F5, F6, F7, F10)
def test_run_without_nooks_prospects_file_still_ranks_and_can_go_live(world, cfg, tmp_path):
    raw = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id, include_prospects=False)
    assert not (raw / "nooks_prospects.json").exists()
    r = run(raw, cfg, tmp_path, live=True)
    assert len(r.rank_result.ranked) > 0 and r.live


def test_empty_nooks_prospects_file_still_ranks(world, cfg, tmp_path):
    raw = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id, nooks_prospects=[])
    assert len(run(raw, cfg, tmp_path).rank_result.ranked) > 0


def test_foreign_owner_calls_block_live(world, cfg, tmp_path):
    raw = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id)
    rewrite(raw, "nooks_calls", lambda d: d["rows"][0].__setitem__("owner", {"id": "SOMEONE-ELSE"}))
    with pytest.raises(LiveRefused) as e:
        run(raw, cfg, tmp_path, live=True)
    assert any("owner other than" in r for r in e.value.reasons)
    assert run(raw, cfg, tmp_path).live is False                       # a dry run only warns


def test_tiny_first_ever_history_blocks_live_unless_confirmed(world, cfg, tmp_path):
    raw = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id, nooks_calls=world.calls[:3])
    with pytest.raises(LiveRefused) as e:
        run(raw, cfg, tmp_path, live=True)
    assert any("suspiciously small" in r for r in e.value.reasons)
    rewrite(raw, RUN_META, lambda d: d.__setitem__("empty_history_confirmed", True))
    assert run(raw, cfg, tmp_path / "again", live=True).live


def test_tiny_history_with_earlier_state_is_not_a_first_run_problem(world, cfg, tmp_path):
    full = write_raw(world, tmp_path / "a", NOW, owner=cfg.owner.nooks_user_id)
    run(full, cfg, tmp_path)                                           # state now exists
    small = write_raw(world, tmp_path / "b", NOW, owner=cfg.owner.nooks_user_id, nooks_calls=world.calls[:3])
    try:
        run(small, cfg, tmp_path, live=True)
    except LiveRefused as e:
        assert not any("suspiciously small" in r for r in e.reasons)   # refused for the partial history instead


def test_confirmed_empty_history_marks_dnc_rebuilt(world, cfg, tmp_path):
    raw = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id, nooks_calls=[])
    rewrite(raw, RUN_META, lambda d: d.__setitem__("empty_history_confirmed", True))
    r = run(raw, cfg, tmp_path, live=True)
    assert st_mod.dnc_is_rebuilt(r.state) and r.state.dnc_rebuilt_empty


def test_unconfirmed_empty_history_blocks_live(world, cfg, tmp_path):
    raw = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id, nooks_calls=[])
    with pytest.raises(LiveRefused):
        run(raw, cfg, tmp_path, live=True)


def test_reset_state_without_tagged_tasks_blocks_live(raw, cfg, tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "state.json").write_text("{not json")
    with pytest.raises(LiveRefused) as e:
        run(raw, cfg, tmp_path, live=True)
    assert any("state was reset" in r for r in e.value.reasons)
    assert run(raw, cfg, tmp_path).live is False                       # dry run continues


def test_reset_state_with_tagged_tasks_is_recoverable(world, cfg, tmp_path):
    base = run(write_raw(world, tmp_path / "a", NOW, owner=cfg.owner.nooks_user_id), cfg, tmp_path / "ra")
    top = base.rank_result.ranked[0].account
    tasks = [{"id": "T1", "action": "call", "status": "finished", "completed": True,
              "note": f"[apac-callsheet 2026-11-24 #1 acct={top.sf_account_id}]", "prospect": {"id": "NK"}}]
    raw = write_raw(world, tmp_path / "b", NOW, owner=cfg.owner.nooks_user_id, nooks_tasks=tasks)
    (tmp_path / "s2").mkdir()
    (tmp_path / "s2" / "state.json").write_text("{not json")
    r = run_pipeline(raw, cfg, tmp_path / "s2", NOW, live=True, record_sheet=False)
    assert r.live and top.name not in names(r.rank_result.ranked)      # the state reset did not forget last night


def test_sales_opt_out_is_counted_logged_and_never_blocks(world, cfg, tmp_path):
    contacts = [dict(c) for c in world.contacts]
    for c in contacts[:3]:
        c["Sales_Opt_Out_Email__c"] = True
    base = run(write_raw(world, tmp_path / "a", NOW, owner=cfg.owner.nooks_user_id), cfg, tmp_path / "ra")
    raw = write_raw(world, tmp_path / "b", NOW, owner=cfg.owner.nooks_user_id, sf_contacts=contacts)
    r = run(raw, cfg, tmp_path / "rb")
    assert r.stats["contacts_sales_opt_out_email_unverified"] == 3
    assert any("3 contacts with Sales_Opt_Out_Email__c (meaning unverified, NOT used to block)" in l for l in r.log_lines)
    assert keys(r) == keys(base)


def test_scoop_match_line_has_the_agreed_wording(raw, cfg, tmp_path):
    r = run(raw, cfg, tmp_path)
    s = r.stats
    line = next(l for l in r.log_lines if "scoops matched to your book" in l)
    assert f"{s['scoops_matched_to_book']} of {s['scoops']} ({s['scoops'] - s['scoops_matched_to_book']} unmatched, " \
           "normal: only ~30% of scoop companies are in your book" in line


def test_tagged_task_count_is_logged(world, cfg, tmp_path):
    tasks = [{"id": f"T{i}", "action": "call", "status": "finished", "completed": True,
              "note": f"[apac-callsheet 2026-11-24 #{i} acct=001FAKE0000000{i}]", "prospect": {"id": "NK"}} for i in (1, 2)]
    raw = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id, nooks_tasks=tasks)
    assert any("(2 tagged tasks seen)" in l for l in run(raw, cfg, tmp_path).log_lines)


def test_narrow_allowlist_argparse_and_sys_only_in_pipeline():
    import ast
    for p in (ROOT / "src").glob("*.py"):
        tree = ast.parse(p.read_text())
        mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        if p.name != "pipeline.py":
            assert not mods & {"argparse", "sys"}, p.name


def test_the_only_file_write_in_pipeline_is_write_log():
    import ast
    tree = ast.parse((ROOT / "src" / "pipeline.py").read_text())
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        has_write = any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "write_text"
                        for c in ast.walk(fn))
        assert has_write == (fn.name == "write_log"), fn.name


# ---------------------------------------------------------------- review round 3 (N2, N3, N4)
def test_recent_calls_not_tied_line_and_block_above_threshold(world, cfg, tmp_path):
    base = run(write_raw(world, tmp_path / "a", NOW, owner=cfg.owner.nooks_user_id), cfg, tmp_path / "ra")
    assert any("recent calls not tied to an account: 0 of" in l for l in base.log_lines)
    calls = [dict(c) for c in world.calls]
    t = (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i in range(10):                               # ten recent calls on unknown Nooks ids, no phone to tie them
        calls.append({"id": f"FAKE-UNTIED-{i}", "time": t, "duration": 20, "to": f"+6140000{i:04d}",
                      "prospect": {"id": f"NK-UNKNOWN-{i}"}, "callDisposition": {"id": "disp-no-answer"}})
    raw = write_raw(world, tmp_path / "b", NOW, owner=cfg.owner.nooks_user_id, nooks_calls=calls)
    r = run(raw, cfg, tmp_path / "rb")
    assert r.stats["recent_calls_not_tied_to_account"] == 10 and r.stats["recent_call_prospect_ids_to_resolve"] == 10
    assert any("recent calls not tied to an account: 10 of" in l for l in r.log_lines)
    with pytest.raises(LiveRefused) as e:
        run(raw, cfg, tmp_path / "rc", live=True)
    assert any("not tied to an account" in x for x in e.value.reasons)


def test_recent_calls_just_under_the_threshold_do_not_block(world, cfg, tmp_path):
    calls = [dict(c) for c in world.calls]
    t = (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i in range(300):                               # many recent calls, all tied by phone to a known prospect
        calls.append({**calls[0], "id": f"FAKE-TIED-{i}", "time": t, "createdAt": t})
    calls.append({"id": "FAKE-ONE-UNTIED", "time": t, "duration": 20, "to": "+61400999999",
                  "prospect": {"id": "NK-UNKNOWN"}, "callDisposition": {"id": "disp-no-answer"}})
    raw = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id, nooks_calls=calls)
    r = run(raw, cfg, tmp_path, live=True)
    assert r.live and r.stats["recent_calls_not_tied_to_account"] == 1


def test_nooks_prospect_rows_for_recent_ids_restore_the_cooldown(world, cfg, tmp_path):
    b = Builder()
    b.account(1, "Cool Pty Ltd", state="New South Wales", emp=300)
    p = b.prospect(1, 1, "Head of Product")
    b.scoop(1, "Funding", "Cool raised $5 million in funding.", 1, topics=("Funding",))
    b.call(p, "No Answer", 1, duration=10)
    row = b.w.nooks_prospects[0]
    unresolved = write_raw(b.w, tmp_path / "a", NOW, owner=cfg.owner.nooks_user_id, include_prospects=False,
                           sf_contacts=[{**b.w.contacts[0], "MobilePhone": None, "Phone": None}])
    r1 = run(unresolved, cfg, tmp_path / "ra")
    assert r1.stats["recent_calls_not_tied_to_account"] == 1
    resolved = write_raw(b.w, tmp_path / "b", NOW, owner=cfg.owner.nooks_user_id, nooks_prospects=[row],
                         sf_contacts=[{**b.w.contacts[0], "MobilePhone": None, "Phone": None}])
    r2 = run(resolved, cfg, tmp_path / "rb")
    assert r2.stats["recent_calls_not_tied_to_account"] == 0
    assert "Cool Pty Ltd" not in names(r2.rank_result.ranked)          # cooled down by the recent no-answer


def test_gmail_coverage_blocks_live_until_every_ranked_prospect_is_searched(world, cfg, tmp_path):
    raw = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id, gmail_cover=[])
    dry = run(raw, cfg, tmp_path)
    assert dry.gmail_uncovered and dry.stats["gmail_ranked_prospects_uncovered"] == len(dry.gmail_uncovered)
    assert all("@" not in h and len(h) == 32 for h in dry.gmail_uncovered)          # hashes only
    assert any("still need a Gmail search" in l for l in dry.log_lines)
    with pytest.raises(LiveRefused) as e:
        run(raw, cfg, tmp_path / "live", live=True)
    assert any("have not been Gmail-searched" in x for x in e.value.reasons)


def test_partial_gmail_coverage_names_exactly_the_missing_ones(world, cfg, tmp_path):
    from src.intake import address_hash
    full = run(write_raw(world, tmp_path / "a", NOW, owner=cfg.owner.nooks_user_id), cfg, tmp_path / "ra")
    assert full.gmail_uncovered == [] and full.stats["gmail_ranked_prospects"] > 2
    chosen = [p for ps in full.rank_result.prospects.values() for p in ps if p.email]
    half = [p.email for p in chosen[: len(chosen) // 2]]
    part = run(write_raw(world, tmp_path / "b", NOW, owner=cfg.owner.nooks_user_id, gmail_cover=half), cfg, tmp_path / "rb")
    expected = sorted({address_hash(p.email) for p in chosen[len(chosen) // 2:]} - {address_hash(a) for a in half})
    assert part.gmail_uncovered == expected


def test_live_passes_once_the_ranked_set_is_covered(world, cfg, tmp_path):
    r = run(write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id), cfg, tmp_path, live=True)
    assert r.live and r.gmail_uncovered == [] and any("0 still need a Gmail search" in l for l in r.log_lines)


def test_gmail_hashes_use_the_files_own_salt(world, cfg, tmp_path):
    from src.intake import address_hash
    raw = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id, gmail_cover=[])
    rewrite(raw, "gmail_sent", lambda d: d["meta"].__setitem__("hash_salt", "my-salt"))
    r = run(raw, cfg, tmp_path)
    p = next(p for ps in r.rank_result.prospects.values() for p in ps if p.email)
    assert address_hash(p.email, "my-salt") in r.gmail_uncovered and address_hash(p.email) not in r.gmail_uncovered


def test_dnc_calls_not_matched_to_any_contact_are_logged(world, cfg, tmp_path):
    calls = [dict(c) for c in world.calls]
    dnc = [c for c in calls if c["callDisposition"]["id"] == "disp-do-not-call"][0]
    dnc["to"], dnc["prospect"] = "external", {"id": "NK-NOBODY"}
    raw = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id, nooks_calls=calls)
    r = run(raw, cfg, tmp_path)
    assert r.stats["dnc_calls_not_matched_to_contact"] >= 1
    assert any("DNC calls not matched to any contact: " in l and "re-checked by Nooks prospect id in Phase 3" in l
               for l in r.log_lines)
    assert run(write_raw(world, tmp_path / "b", NOW, owner=cfg.owner.nooks_user_id), cfg,
               tmp_path / "rb").stats["dnc_calls_not_matched_to_contact"] == 0


def test_phase3_preconditions_are_in_the_header_the_result_section_and_the_live_output(raw, cfg, tmp_path, capsys):
    r = run(raw, cfg, tmp_path)
    text = r.log_path.read_text()
    assert "hard precondition" in text.split("== INTAKE ==")[0]
    section = text.split("== PHASE 3 PRECONDITIONS ==")[1]
    assert "HARD PRECONDITION" in section and "BY NOOKS PROSPECT ID" in section and "gmail_sent.json" in section
    assert pl.main(["--raw-dir", str(raw), "--state-dir", str(tmp_path / "s2"), "--log-dir", str(tmp_path / "l2"),
                    "--now", NOW.isoformat(), "--live"]) == 0
    assert "HARD PRECONDITION" in capsys.readouterr().out


def test_live_refusal_output_repeats_the_preconditions(world, cfg, tmp_path, capsys):
    bad = write_raw(world, tmp_path / "bad", NOW, owner=cfg.owner.nooks_user_id, calls_last_page=False)
    assert pl.main(["--raw-dir", str(bad), "--state-dir", str(tmp_path / "s"), "--log-dir", str(tmp_path / "l"),
                    "--now", NOW.isoformat(), "--live"]) == 2
    assert "HARD PRECONDITION" in capsys.readouterr().err


def test_pipeline_uses_the_public_state_api_for_dnc_calls():
    text = (ROOT / "src" / "pipeline.py").read_text()
    assert "st_mod._is_dnc" not in text and "st_mod.is_dnc_call" in text


# ---------------------------------------------------------------- helper scripts
def test_recent_call_ids_script(world, cfg, tmp_path, capsys):
    from scripts import recent_call_ids as rci
    calls = [dict(c) for c in world.calls]
    t = (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i in range(30):
        calls.append({"id": f"FAKE-X-{i}", "time": t, "duration": 5, "to": "+61400999999", "prospect": {"id": f"NK-X-{i:02d}"},
                      "callDisposition": {"id": "disp-no-answer"}})
    raw = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id, nooks_calls=calls)
    out = tmp_path / "batches.json"
    assert rci.main(["--raw-dir", str(raw), "--out", str(out), "--now", NOW.isoformat()]) == 0
    batches = json.loads(out.read_text())
    assert [len(b) for b in batches] == [25, 5] and batches[0][0] == "NK-X-00"
    assert "NK-X" not in capsys.readouterr().out                   # counts only
    (raw / "run_meta.json").unlink()
    assert rci.main(["--raw-dir", str(raw), "--out", str(out)]) == 2


def test_gmail_queue_and_hashes_scripts(world, cfg, tmp_path, capsys):
    from scripts import gmail_queue as gq
    from src.intake import address_hash
    raw = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id, gmail_cover=[])
    q = tmp_path / "queue.json"
    assert gq.main(["queue", "--raw-dir", str(raw), "--state-dir", str(tmp_path / "s"), "--now", NOW.isoformat(),
                    "--out", str(q)]) == 0
    addrs = json.loads(q.read_text())
    assert addrs and all("@" in a for a in addrs) and "@" not in capsys.readouterr().out      # count only on stdout
    meta = tmp_path / "meta.json"
    assert gq.main(["hashes", "--addresses", str(q), "--out", str(meta)]) == 0
    m = json.loads(meta.read_text())
    assert m["searched_address_hashes"] == sorted({address_hash(a) for a in addrs}) and "@" not in meta.read_text()
    # a second round keeps the first round's hashes
    q2 = tmp_path / "q2.json"
    q2.write_text(json.dumps(["extra.person@example.com"]))
    assert gq.main(["hashes", "--addresses", str(q2), "--out", str(meta), "--also", str(meta)]) == 0
    assert len(json.loads(meta.read_text())["searched_address_hashes"]) == len(addrs) + 1


def test_gmail_loop_reaches_a_covered_set(world, cfg, tmp_path):
    """dry run -> queue -> hashes -> merge into gmail_sent meta -> re-run: nothing uncovered, live passes."""
    from scripts import gmail_queue as gq
    from scripts import merge_pages as mp
    raw = write_raw(world, tmp_path / "raw", NOW, owner=cfg.owner.nooks_user_id, gmail_cover=[])
    q, meta, page = tmp_path / "q.json", tmp_path / "meta.json", tmp_path / "page.json"
    gq.main(["queue", "--raw-dir", str(raw), "--state-dir", str(tmp_path / "s"), "--now", NOW.isoformat(), "--out", str(q)])
    gq.main(["hashes", "--addresses", str(q), "--out", str(meta)])
    page.write_text(json.dumps({"threads": world.sent_threads}))
    man = tmp_path / "m.json"
    man.write_text(json.dumps([{"path": str(page)}]))
    assert mp.main(["--manifest", str(man), "--name", "gmail_sent", "--raw-dir", str(raw), "--source-tool", "search_threads",
                    "--query", "in:sent to:<addr> newer_than:7d", "--meta-json", str(meta),
                    "--fetched-at", "2026-11-25T03:00:00Z"]) == 0
    rewrite(raw, RUN_META, lambda d: d["row_counts"].__setitem__("gmail_sent", len(world.sent_threads)))
    r = run(raw, cfg, tmp_path / "x", live=True)
    assert r.live and r.gmail_uncovered == []
