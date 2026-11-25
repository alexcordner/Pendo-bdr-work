"""Tests for src/intake.py (task 2a). Synthetic raw dumps only: tests/fixtures/intake_raw_ok/ and copies of it."""
import hashlib
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import intake
from src.intake import FILES, RUN_META, load_raw, scrub
from tests.intake_helpers import read, rewrite

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "intake_raw_ok"
NOW = datetime(2026, 11, 25, 3, 0, tzinfo=timezone.utc)        # same minute as the fixture's fetched_at
REQUIRED = [n for n, (req, _p) in FILES.items() if req]
OPTIONAL = [n for n, (req, _p) in FILES.items() if not req]


@pytest.fixture
def raw(tmp_path):
    d = tmp_path / "raw"
    shutil.copytree(FIXTURE, d)
    return d


def has(lst, needle):
    return any(needle in x for x in lst)


def set_count(raw, name, n):
    def f(d):
        d["row_counts"][name] = n
    rewrite(raw, RUN_META, f)


def set_rows(raw, name, rows):
    rewrite(raw, name, lambda d: d.__setitem__("rows", rows))
    set_count(raw, name, len(rows))


# ---------------------------------------------------------------- the good case
def test_fixture_loads_clean(raw):
    d = load_raw(raw, now=NOW)
    r = d.report
    assert r.ok and not r.errors
    assert r.counts["sf_accounts"] == 3 and r.counts["nooks_calls"] == 2
    assert r.reached_last_page["nooks_calls"] is True
    assert r.zoominfo_credits_used == 0 and not r.stale
    assert r.can_go_live, r.live_blockers
    assert len(d.calls) == 2 and len(d.scoops) == 1 and len(d.sent_emails) == 1 and d.disposition_index


def test_load_raw_never_writes_or_changes_files(raw):
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in raw.iterdir()}
    load_raw(raw, now=NOW)
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in raw.iterdir()} == before
    assert sorted(p.name for p in raw.iterdir()) == sorted(before)


def test_naive_now_is_refused(raw):
    with pytest.raises(ValueError):
        load_raw(raw, now=datetime(2026, 11, 25))


def test_missing_directory_is_an_error_not_an_exception(tmp_path):
    d = load_raw(tmp_path / "nope", now=NOW)
    assert has(d.report.errors, "raw directory not found") and not d.report.can_go_live


# ---------------------------------------------------------------- missing / unreadable files
@pytest.mark.parametrize("name", REQUIRED)
def test_missing_required_file_is_an_error(raw, name):
    (raw / f"{name}.json").unlink()
    r = load_raw(raw, now=NOW).report
    assert has(r.errors, f"required file missing: {name}.json") and not r.can_go_live


def test_missing_run_meta_is_an_error(raw):
    (raw / "run_meta.json").unlink()
    assert has(load_raw(raw, now=NOW).report.errors, "run_meta.json")


@pytest.mark.parametrize("name", OPTIONAL)
def test_missing_optional_file_is_only_a_warning(raw, name):
    (raw / f"{name}.json").unlink()
    set_count(raw, name, 0)
    r = load_raw(raw, now=NOW).report
    assert r.ok and has(r.warnings, f"optional file missing: {name}.json")


def test_missing_gmail_blocks_live_but_not_dry_run(raw):
    (raw / "gmail_sent.json").unlink()
    r = load_raw(raw, now=NOW).report
    assert r.ok and not r.can_go_live and has(r.live_blockers, "gmail_sent.json missing")


def test_invalid_json_is_an_error(raw):
    (raw / "nooks_calls.json").write_text("{broken")
    r = load_raw(raw, now=NOW).report
    assert has(r.errors, "nooks_calls: nooks_calls.json is not valid JSON")


@pytest.mark.parametrize("payload", [[], {"rows": []}, {"meta": {}, "rows": {}}, {"meta": [], "rows": []}, "text"])
def test_wrong_top_level_shape_is_an_error(raw, payload):
    (raw / "sf_contacts.json").write_text(json.dumps(payload))
    assert has(load_raw(raw, now=NOW).report.errors, "must be an object with a 'meta' object and a 'rows' list")


def test_non_object_row_is_an_error(raw):
    rewrite(raw, "sf_contacts", lambda d: d["rows"].append("oops"))
    assert has(load_raw(raw, now=NOW).report.errors, "rows are not objects")


# ---------------------------------------------------------------- run_meta
@pytest.mark.parametrize("key", ["fetched_at", "sf_user_id", "nooks_user_id", "row_counts"])
def test_run_meta_required_keys(raw, key):
    rewrite(raw, RUN_META, lambda d: d.pop(key))
    assert has(load_raw(raw, now=NOW).report.errors, f"run_meta.json is missing '{key}'")


def test_run_meta_not_an_object(raw):
    (raw / "run_meta.json").write_text("[]")
    assert has(load_raw(raw, now=NOW).report.errors, "run_meta.json must be a JSON object")


def test_row_count_mismatch_is_an_error(raw):
    set_count(raw, "nooks_calls", 99)
    r = load_raw(raw, now=NOW).report
    assert has(r.errors, "run_meta says 99 rows but the file holds 2") and not r.can_go_live


def test_row_count_missing_entry_is_a_warning(raw):
    rewrite(raw, RUN_META, lambda d: d["row_counts"].pop("zoominfo_scoops"))
    r = load_raw(raw, now=NOW).report
    assert r.ok and has(r.warnings, "no entry for zoominfo_scoops")


def test_fetch_step_warnings_are_carried(raw):
    rewrite(raw, RUN_META, lambda d: d.__setitem__("warnings", ["listCalls page 9 retried twice"]))
    assert has(load_raw(raw, now=NOW).report.warnings, "fetch step warning: listCalls page 9 retried twice")


# ---------------------------------------------------------------- reached_last_page
def test_calls_not_at_last_page_blocks_live(raw):
    rewrite(raw, "nooks_calls", lambda d: d["meta"].__setitem__("reached_last_page", False))
    rewrite(raw, RUN_META, lambda d: d["reached_last_page"].__setitem__("nooks_calls", False))
    r = load_raw(raw, now=NOW).report
    assert r.ok and r.reached_last_page["nooks_calls"] is False and not r.calls_reached_last_page
    assert not r.can_go_live and has(r.live_blockers, "nooks_calls: paging did not reach the last page")
    assert has(r.warnings, "last page NOT reached")


@pytest.mark.parametrize("value", [None, "yes", 1, "true"])
def test_non_boolean_flag_is_treated_as_not_reached(raw, value):
    rewrite(raw, "nooks_calls", lambda d: d["meta"].__setitem__("reached_last_page", value))
    rewrite(raw, RUN_META, lambda d: d["reached_last_page"].pop("nooks_calls"))
    r = load_raw(raw, now=NOW).report
    assert r.reached_last_page["nooks_calls"] is False and not r.can_go_live


def test_run_meta_and_file_disagree_cautious_value_wins(raw):
    rewrite(raw, RUN_META, lambda d: d["reached_last_page"].__setitem__("nooks_calls", False))
    r = load_raw(raw, now=NOW).report
    assert r.reached_last_page["nooks_calls"] is False and has(r.warnings, "differs between run_meta")
    assert not r.can_go_live


@pytest.mark.parametrize("name", ["sf_accounts", "sf_contacts", "nooks_prospects", "nooks_tasks"])
def test_other_safety_sources_also_block_live(raw, name):
    rewrite(raw, name, lambda d: d["meta"].__setitem__("reached_last_page", False))
    rewrite(raw, RUN_META, lambda d: d["reached_last_page"].__setitem__(name, False))
    r = load_raw(raw, now=NOW).report
    assert not r.can_go_live and has(r.live_blockers, name)


@pytest.mark.parametrize("name", ["zoominfo_scoops", "nooks_accounts"])
def test_partial_scoops_or_nooks_accounts_only_warn(raw, name):
    rewrite(raw, name, lambda d: d["meta"].__setitem__("reached_last_page", False))
    rewrite(raw, RUN_META, lambda d: d["reached_last_page"].__setitem__(name, False))
    r = load_raw(raw, now=NOW).report
    assert r.can_go_live and has(r.warnings, f"{name}: last page NOT reached")


# ---------------------------------------------------------------- staleness
def test_stale_data_warns_and_blocks_live(raw):
    r = load_raw(raw, now=NOW + timedelta(hours=24, minutes=1)).report
    assert r.stale and has(r.warnings, "stale") and not r.can_go_live and r.ok


def test_exactly_24h_is_not_stale_and_fresh_is_fine(raw):
    assert not load_raw(raw, now=NOW + timedelta(hours=24)).report.stale
    assert not load_raw(raw, now=NOW).report.stale


def test_oldest_fetched_at_decides(raw):
    rewrite(raw, "nooks_calls", lambda d: d["meta"].__setitem__("fetched_at", "2026-11-23T03:00:00Z"))
    r = load_raw(raw, now=NOW).report
    assert r.stale and r.fetched_at == datetime(2026, 11, 23, 3, 0, tzinfo=timezone.utc)


def test_future_fetched_at_is_flagged(raw):
    rewrite(raw, "sf_accounts", lambda d: d["meta"].__setitem__("fetched_at", "2026-11-26T03:00:00Z"))
    r = load_raw(raw, now=NOW).report
    assert has(r.warnings, "in the future") and not r.can_go_live


def test_missing_file_fetched_at_blocks_live(raw):
    rewrite(raw, "nooks_calls", lambda d: d["meta"].pop("fetched_at"))
    r = load_raw(raw, now=NOW).report
    assert has(r.warnings, "fetched_at missing or invalid") and has(r.live_blockers, "nooks_calls: fetch time unknown")


def test_invalid_run_meta_fetched_at_is_an_error(raw):
    rewrite(raw, RUN_META, lambda d: d.__setitem__("fetched_at", "yesterday"))
    assert has(load_raw(raw, now=NOW).report.errors, "fetched_at is not an ISO timestamp")


def test_missing_meta_keys_warn(raw):
    rewrite(raw, "nooks_calls", lambda d: d["meta"].pop("query"))
    assert has(load_raw(raw, now=NOW).report.warnings, "nooks_calls.json meta is missing 'query'")


# ---------------------------------------------------------------- duplicates
def test_duplicate_calls_are_dropped_and_counted(raw):
    rows = read(raw, "nooks_calls")["rows"]
    set_rows(raw, "nooks_calls", rows + [dict(rows[0])])
    d = load_raw(raw, now=NOW)
    assert d.report.duplicates == {"nooks_calls": 1} and len(d.nooks_calls) == 2 and len(d.calls) == 2
    assert has(d.report.warnings, "nooks_calls: 1 duplicate rows")


def test_salesforce_15_and_18_char_ids_are_the_same_row(raw):
    rows = read(raw, "sf_accounts")["rows"]
    twin = dict(rows[0], Id=rows[0]["Id"] + "AAA")
    set_rows(raw, "sf_accounts", rows + [twin])
    d = load_raw(raw, now=NOW)
    assert d.report.duplicates == {"sf_accounts": 1} and len(d.sf_accounts) == 3


def test_first_duplicate_wins(raw):
    rows = read(raw, "nooks_prospects")["rows"]
    set_rows(raw, "nooks_prospects", rows + [dict(rows[0], title="Second copy")])
    d = load_raw(raw, now=NOW)
    assert [p["title"] for p in d.nooks_prospects if p["id"] == rows[0]["id"]] == [rows[0]["title"]]


# ---------------------------------------------------------------- malformed rows
def test_malformed_calls_are_counted_with_samples_and_block_live(raw):
    rows = read(raw, "nooks_calls")["rows"]
    bad = [{"id": f"BAD-{i}", "duration": 5} for i in range(8)]            # no time at all
    set_rows(raw, "nooks_calls", rows + bad)
    d = load_raw(raw, now=NOW)
    m = d.report.malformed["nooks_calls"]
    assert m.count == 8 and len(m.samples) == intake.SAMPLE_LIMIT == 5
    assert all("no usable time" in s for s in m.samples)
    assert len(d.calls) == 2                                              # good rows still converted
    assert not d.report.can_go_live and has(d.report.live_blockers, "call rows are malformed")
    assert has(d.report.warnings, "8 of 10 rows are malformed") and has(d.report.warnings, "more than 5%")


def test_a_few_malformed_rows_do_not_raise_the_5_percent_warning(raw):
    rows = [{"id": f"C{i}", "time": "2026-11-20T01:00:00Z", "duration": 3, "callDisposition": {"id": "disp-no-answer"}}
            for i in range(60)]
    set_rows(raw, "nooks_calls", rows + [{"id": "BAD"}])
    r = load_raw(raw, now=NOW).report
    assert r.malformed["nooks_calls"].count == 1 and not has(r.warnings, "more than 5%")


def test_samples_never_hold_emails_or_phones(raw):
    set_rows(raw, "nooks_calls", [{"id": "jane.doe@example.com +61412345678 0412345678", "duration": 3}])
    d = load_raw(raw, now=NOW)
    text = "\n".join(d.report.lines())
    assert "@" not in text and "61412345678" not in text and "0412345678" not in text
    assert "<email>" in text


def test_malformed_salesforce_account_is_counted_but_kept_for_accounts_module(raw):
    rows = read(raw, "sf_accounts")["rows"]
    set_rows(raw, "sf_accounts", rows + [{"Name": "No Id Pty Ltd"}])
    d = load_raw(raw, now=NOW)
    assert d.report.malformed["sf_accounts"].count == 1 and len(d.sf_accounts) == 4


def test_malformed_scoop_is_counted_not_fatal(raw):
    rows = read(raw, "zoominfo_scoops")["rows"]
    set_rows(raw, "zoominfo_scoops", rows + [{"id": "S-BAD", "attributes": {"id": "S-BAD", "description": "x"}}])
    d = load_raw(raw, now=NOW)
    assert d.report.malformed["zoominfo_scoops"].count == 1 and len(d.scoops) == 1 and d.report.can_go_live


def test_malformed_gmail_thread_is_counted_per_thread(raw):
    rows = read(raw, "gmail_sent")["rows"]
    bad = {"id": "T-BAD", "messages": [{"threadId": "x", "toRecipients": ["a@example.com"]}]}   # message without id
    set_rows(raw, "gmail_sent", rows + [bad])
    d = load_raw(raw, now=NOW)
    assert d.report.malformed["gmail_sent"].count == 1 and len(d.sent_emails) == 1


def test_malformed_contact_and_prospect_block_live(raw):
    set_rows(raw, "sf_contacts", read(raw, "sf_contacts")["rows"] + [{"AccountId": "x"}])
    r = load_raw(raw, now=NOW).report
    assert r.malformed["sf_contacts"].count == 1 and not r.can_go_live
    set_rows(raw, "nooks_prospects", read(raw, "nooks_prospects")["rows"] + [{"name": "No Id"}])
    assert load_raw(raw, now=NOW).report.malformed["nooks_prospects"].count == 1


# ---------------------------------------------------------------- dispositions / owners / credits
def test_unknown_disposition_id_warns_and_blocks_live(raw):
    rows = read(raw, "nooks_calls")["rows"]
    rows[0]["callDisposition"] = {"id": "disp-from-the-future"}
    set_rows(raw, "nooks_calls", rows)
    d = load_raw(raw, now=NOW)
    assert d.report.stats["calls_with_unknown_disposition"] == 1 and not d.report.can_go_live
    assert has(d.report.warnings, "Do Not Call among them could not be recognised")


def test_empty_dispositions_is_an_error(raw):
    set_rows(raw, "nooks_dispositions", [])
    assert has(load_raw(raw, now=NOW).report.errors, "nooks_dispositions has no rows")


def test_calls_by_another_owner_warn(raw):
    rewrite(raw, RUN_META, lambda d: d.__setitem__("nooks_user_id", "FAKE-SOMEONE-ELSE"))
    d = load_raw(raw, now=NOW)
    assert d.report.stats["calls_by_other_owner"] == 2 and has(d.report.warnings, "owner other than")


@pytest.mark.parametrize("value", ["missing", -1, "0", 1.5, True])
def test_unknown_or_invalid_credits_warn_and_block_live(raw, value):
    def f(d):
        if value == "missing":
            d.pop("zoominfo_credits_used")
        else:
            d["zoominfo_credits_used"] = value
    rewrite(raw, RUN_META, f)
    r = load_raw(raw, now=NOW).report
    assert r.zoominfo_credits_used is None and not r.can_go_live and has(r.warnings, "credits spent are UNKNOWN")


def test_credits_value_is_recorded(raw):
    rewrite(raw, RUN_META, lambda d: d.__setitem__("zoominfo_credits_used", 3))
    assert load_raw(raw, now=NOW).report.zoominfo_credits_used == 3


def test_empty_call_history_blocks_live(raw):
    set_rows(raw, "nooks_calls", [])
    r = load_raw(raw, now=NOW).report
    assert r.ok and has(r.live_blockers, "no call history")


# ---------------------------------------------------------------- tasks
def test_tasks_are_converted(raw):
    set_rows(raw, "nooks_tasks", [{"id": "T1", "action": "call", "status": "ready", "completed": False,
                                   "dueAt": "2026-11-26T00:00:00Z", "prospect": {"id": "P"}},
                                  {"id": "T2", "action": "call", "status": "finished", "completed": True}])
    d = load_raw(raw, now=NOW)
    assert [t.nooks_id for t in d.tasks] == ["T1", "T2"] and d.tasks[0].due_at is not None


# ---------------------------------------------------------------- joins
def test_derive_links_from_prospect_contact_join(raw):
    d = load_raw(raw, now=NOW)
    links = intake.derive_nooks_sf_links(d)
    assert links == {"FAKE-NK-ACCT-0001": "001FAKE00000001", "FAKE-NK-ACCT-0002": "001FAKE00000002"}
    assert d.report.stats["nooks_accounts_linked_to_salesforce"] == 2


def test_derive_links_ambiguous_account_is_left_unlinked(raw):
    contacts = read(raw, "sf_contacts")["rows"]
    prospects = read(raw, "nooks_prospects")["rows"]
    prospects[1]["account"] = {"id": prospects[0]["account"]["id"]}        # two SF accounts behind one Nooks account
    set_rows(raw, "nooks_prospects", prospects)
    d = load_raw(raw, now=NOW)
    links = intake.derive_nooks_sf_links(d)
    assert prospects[0]["account"]["id"] not in links
    assert d.report.stats["nooks_accounts_link_ambiguous"] == 1 and has(d.report.warnings, "several Salesforce accounts")
    assert contacts


def test_nooks_account_rows_adds_links_without_touching_raw(raw):
    d = load_raw(raw, now=NOW)
    before = json.dumps(d.nooks_accounts, sort_keys=True)
    rows = intake.nooks_account_rows(d, intake.derive_nooks_sf_links(d))
    assert all(r["_sf_account_id"] for r in rows)
    assert json.dumps(d.nooks_accounts, sort_keys=True) == before


def test_nooks_account_rows_keeps_an_existing_link(raw):
    d = load_raw(raw, now=NOW)
    d.nooks_accounts[0]["_sf_account_id"] = "001KEEPME0000001"
    rows = intake.nooks_account_rows(d, {d.nooks_accounts[0]["id"]: "001OTHER00000001"})
    assert rows[0]["_sf_account_id"] == "001KEEPME0000001"


def test_build_prospects_joins_contact_phone_and_optout(raw):
    d = load_raw(raw, now=NOW)
    ps = intake.build_prospects(d, {}, {})
    assert len(ps) == 2 and all(p.phone_e164 for p in ps)
    assert d.report.stats["prospects_with_salesforce_contact"] == 2


def test_prospect_with_crm_id_but_no_contact_is_flagged(raw):
    set_rows(raw, "sf_contacts", read(raw, "sf_contacts")["rows"][:1])
    d = load_raw(raw, now=NOW)
    ps = intake.build_prospects(d, {}, {})
    assert d.report.stats["prospects_with_crm_id_but_no_contact_row"] == 1
    assert has(d.report.warnings, "crmId but no Salesforce Contact row")
    assert sum(1 for p in ps if p.phone_e164 is None) == 1                # no contact: no phone, cannot be called


def test_prospect_without_crm_id_is_counted(raw):
    rows = read(raw, "nooks_prospects")["rows"]
    rows[0]["crmId"] = None
    set_rows(raw, "nooks_prospects", rows)
    d = load_raw(raw, now=NOW)
    intake.build_prospects(d, {}, {})
    assert d.report.stats["prospects_without_crm_id"] == 1


# ---------------------------------------------------------------- scrub / lines
@pytest.mark.parametrize("text,gone", [("mail jane@example.com now", "jane@example.com"),
                                       ("call +61 412 345 678 now", "412 345 678"),
                                       ("call 0412 345 678", "345 678")])
def test_scrub_removes_contact_details(text, gone):
    assert gone not in scrub(text)


def test_scrub_leaves_ordinary_text_and_ids_alone():
    assert scrub("scoop 'FAKE-SCOOP-00001' has no published date") == "scoop 'FAKE-SCOOP-00001' has no published date"


def test_report_lines_are_plain_text(raw):
    d = load_raw(raw, now=NOW)
    text = "\n".join(d.report.lines())
    assert "rows kept per file:" in text and "zoominfo credits used: 0" in text
