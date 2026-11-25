"""Tests for scripts/merge_pages.py (task 2a). Synthetic page files only (tests/fixtures/intake_pages/)."""
import json
from pathlib import Path

import pytest

from scripts import merge_pages as mp

PAGES = Path(__file__).resolve().parent / "fixtures" / "intake_pages"


def page(name):
    return json.loads((PAGES / name).read_text())


def manifest(tmp_path, entries, name="m.json"):
    p = tmp_path / name
    p.write_text(json.dumps(entries))
    return p


def ids(rows, key="id"):
    return [r[key] for r in rows]


# ---------------------------------------------------------------- envelopes
@pytest.mark.parametrize("fixture,expected", [
    ("bare_list.json", ["FAKE-CALL-001", "FAKE-CALL-002"]),
    ("data_page1.json", ["FAKE-CALL-003", "FAKE-CALL-004"]),
    ("results_page.json", ["FAKE-CALL-006"]),
    ("items_page.json", ["FAKE-CALL-007"]),
    ("rows_page.json", ["FAKE-CALL-008"]),
    ("single_list_key.json", ["FAKE-CALL-009"]),
    ("mcp_text_list.json", ["FAKE-CALL-010"]),
    ("mcp_content.json", ["FAKE-CALL-011"]),
])
def test_envelopes_give_rows(fixture, expected):
    assert ids(mp.extract_rows(page(fixture))) == expected


def test_records_envelope_salesforce():
    rows = mp.extract_rows(page("records_page1.json"))
    assert ids(rows, "Id") == ["001FAKE00000001", "001FAKE00000002"]


def test_threads_envelope_gmail():
    assert ids(mp.extract_rows(page("threads_page.json"))) == ["FAKE-THR-1"]


def test_empty_page_is_valid_and_counted():
    rows, stats = mp.merge_pages([page("empty_page.json"), page("bare_list.json")])
    assert len(rows) == 2 and stats["empty_pages"] == 1 and stats["pages"] == 2


@pytest.mark.parametrize("fixture,needle", [
    ("two_lists.json", "several lists"),
    ("no_list.json", "no list"),
    ("scalar.json", "list or an object"),
    ("row_not_object.json", "not an object"),
    ("mcp_bad_text.json", "does not hold JSON"),
])
def test_bad_envelopes_fail_loudly(fixture, needle):
    with pytest.raises(mp.MergeError, match=needle):
        mp.extract_rows(page(fixture))


def test_rows_key_overrides_ambiguity():
    assert ids(mp.extract_rows(page("two_lists.json"), rows_key="b")) == ["FAKE-CALL-002"]


def test_rows_key_missing_is_an_error():
    with pytest.raises(mp.MergeError, match="requested key"):
        mp.extract_rows(page("two_lists.json"), rows_key="zzz")


def test_error_names_the_page_number():
    with pytest.raises(mp.MergeError, match="page 2"):
        mp.merge_pages([page("bare_list.json"), page("scalar.json")])


# ---------------------------------------------------------------- merge
def test_merge_keeps_order_and_drops_duplicates_first_wins():
    rows, stats = mp.merge_pages([page("data_page1.json"), page("data_page2.json")])
    assert ids(rows) == ["FAKE-CALL-003", "FAKE-CALL-004", "FAKE-CALL-005"]
    assert stats["duplicates_dropped"] == 1 and stats["rows_in"] == 4 and stats["rows_out"] == 3
    # first occurrence wins
    a = {"id": "x", "v": 1}
    b = {"id": "x", "v": 2}
    rows, _ = mp.merge_pages([[a], [b]])
    assert rows == [a]


def test_merge_uses_Id_for_salesforce_rows():
    rows, stats = mp.merge_pages([page("records_page1.json"), page("records_page2.json")])
    assert ids(rows, "Id") == [f"001FAKE0000000{i}" for i in (1, 2, 3, 4)]
    assert stats["duplicates_dropped"] == 0


def test_row_without_id_is_an_error_unless_allowed():
    with pytest.raises(mp.MergeError, match="no 'id'"):
        mp.merge_pages([page("row_no_id.json")])
    rows, stats = mp.merge_pages([page("row_no_id.json")], allow_no_id=True)
    assert len(rows) == 1 and stats["rows_without_id"] == 1


# ---------------------------------------------------------------- manifest
def test_manifest_page_numbers_sort_pages(tmp_path):
    m = manifest(tmp_path, [{"path": str(PAGES / "data_page2.json"), "page": 2},
                            {"path": str(PAGES / "data_page1.json"), "page": 1}])
    entries = mp.read_manifest(m)
    assert [Path(e["path"]).name for e in entries] == ["data_page1.json", "data_page2.json"]


@pytest.mark.parametrize("bad,needle", [
    ([], "non-empty"),
    ({"path": "x"}, "non-empty"),
    ([{"nopath": 1}], "string 'path'"),
    ([{"path": "a", "page": 1}, {"path": "b"}], "every entry or on none"),
    ([{"path": "a", "page": 1}, {"path": "b", "page": 1}], "duplicate page"),
    ([{"path": "a", "page": 0}], ">= 1"),
    ([{"path": "a", "page": True}], ">= 1"),
])
def test_bad_manifests(tmp_path, bad, needle):
    with pytest.raises(mp.MergeError, match=needle):
        mp.read_manifest(manifest(tmp_path, bad))


def test_unreadable_manifest_and_pages(tmp_path):
    with pytest.raises(mp.MergeError, match="cannot read manifest"):
        mp.read_manifest(tmp_path / "nope.json")
    with pytest.raises(mp.MergeError, match="cannot read page file"):
        mp.load_pages([{"path": str(tmp_path / "gone.json")}])
    with pytest.raises(mp.MergeError, match="not valid JSON"):
        mp.load_pages([{"path": str(PAGES / "invalid.json")}])


# ---------------------------------------------------------------- CLI
def run_cli(tmp_path, entries, *extra, name="nooks_calls"):
    m = manifest(tmp_path, entries)
    return mp.main(["--manifest", str(m), "--name", name, "--raw-dir", str(tmp_path / "raw"),
                    "--source-tool", "listCalls", "--query", "owner=<id>", *extra])


def test_cli_writes_meta_and_rows_and_prints_counts_only(tmp_path, capsys):
    rc = run_cli(tmp_path, [{"path": str(PAGES / "data_page1.json")}, {"path": str(PAGES / "data_page2.json")}],
                 "--last-page-reached", "true", "--fetched-at", "2026-11-25T02:00:00Z")
    out = capsys.readouterr().out
    assert rc == 0
    dump = json.loads((tmp_path / "raw" / "nooks_calls.json").read_text())
    assert set(dump) == {"meta", "rows"} and len(dump["rows"]) == 3
    m = dump["meta"]
    assert m["source_tool"] == "listCalls" and m["page_count"] == 2 and m["reached_last_page"] is True
    assert m["fetched_at"] == "2026-11-25T02:00:00Z" and m["merge"]["duplicates_dropped"] == 1
    assert "FAKE-CALL" not in out and "3 rows from 2 pages" in out       # counts only, no row content


def test_cli_last_page_false_and_non_paged(tmp_path):
    assert run_cli(tmp_path, [{"path": str(PAGES / "bare_list.json")}], "--last-page-reached", "false") == 0
    assert json.loads((tmp_path / "raw" / "nooks_calls.json").read_text())["meta"]["reached_last_page"] is False
    assert run_cli(tmp_path, [{"path": str(PAGES / "bare_list.json")}], name="gmail_sent") == 0
    assert json.loads((tmp_path / "raw" / "gmail_sent.json").read_text())["meta"]["reached_last_page"] is None


def test_cli_malformed_input_exits_2_and_writes_nothing(tmp_path, capsys):
    rc = run_cli(tmp_path, [{"path": str(PAGES / "bare_list.json")}, {"path": str(PAGES / "two_lists.json")}])
    assert rc == 2 and "page 2" in capsys.readouterr().err
    assert not (tmp_path / "raw").exists() or not list((tmp_path / "raw").iterdir())


def test_cli_does_not_clobber_the_good_file_on_failure(tmp_path):
    assert run_cli(tmp_path, [{"path": str(PAGES / "bare_list.json")}]) == 0
    before = (tmp_path / "raw" / "nooks_calls.json").read_text()
    assert run_cli(tmp_path, [{"path": str(PAGES / "invalid.json")}]) == 2
    assert (tmp_path / "raw" / "nooks_calls.json").read_text() == before


def test_cli_no_temp_file_left_behind(tmp_path):
    run_cli(tmp_path, [{"path": str(PAGES / "bare_list.json")}])
    assert [p.name for p in (tmp_path / "raw").iterdir()] == ["nooks_calls.json"]


@pytest.mark.parametrize("name", ["a/b", "..\\x", ""])
def test_cli_rejects_path_like_names(tmp_path, name):
    assert run_cli(tmp_path, [{"path": str(PAGES / "bare_list.json")}], name=name) == 2


def test_cli_bad_last_page_value_is_rejected(tmp_path):
    with pytest.raises(SystemExit):
        run_cli(tmp_path, [{"path": str(PAGES / "bare_list.json")}], "--last-page-reached", "maybe")


def test_merged_dump_is_readable_by_intake(tmp_path):
    from src.intake import _read_dump, IntakeReport
    run_cli(tmp_path, [{"path": str(PAGES / "bare_list.json")}], "--last-page-reached", "true")
    got = _read_dump(tmp_path / "raw", "nooks_calls", IntakeReport())
    assert got is not None and len(got[1]) == 2


# ---------------------------------------------------------------- reached_last_page from the final page
def test_derive_last_page_nooks_links_next():
    assert mp.derive_last_page([page("nooks_page_more.json"), page("nooks_page_last.json")]) is True
    assert mp.derive_last_page([page("nooks_page_more.json")]) is False
    assert mp.derive_last_page([page("nooks_page_empty_end.json")]) is True        # cursor past the end


def test_derive_last_page_uses_only_the_final_page():
    assert mp.derive_last_page([page("nooks_page_last.json"), page("nooks_page_more.json")]) is False


def test_derive_last_page_has_more_and_mcp_wrapper():
    assert mp.derive_last_page([page("has_more_true.json")]) is False
    assert mp.derive_last_page([page("data_page2.json")]) is True
    wrapped = [{"type": "text", "text": json.dumps(page("nooks_page_last.json"))}]
    assert mp.derive_last_page([wrapped]) is True


@pytest.mark.parametrize("fixture", ["bare_list.json", "records_page2.json", "threads_page.json"])
def test_derive_last_page_without_a_marker_is_an_error(fixture):
    with pytest.raises(mp.MergeError, match="cannot derive"):
        mp.derive_last_page([page(fixture)])
    with pytest.raises(mp.MergeError, match="no pages"):
        mp.derive_last_page([])


def test_cli_auto_derives_from_links_next(tmp_path):
    assert run_cli(tmp_path, [{"path": str(PAGES / "nooks_page_more.json")}, {"path": str(PAGES / "nooks_page_last.json")}],
                   "--last-page-reached", "auto") == 0
    assert json.loads((tmp_path / "raw" / "nooks_calls.json").read_text())["meta"]["reached_last_page"] is True
    assert run_cli(tmp_path, [{"path": str(PAGES / "nooks_page_more.json")}], "--last-page-reached", "auto") == 0
    assert json.loads((tmp_path / "raw" / "nooks_calls.json").read_text())["meta"]["reached_last_page"] is False


def test_cli_auto_without_marker_fails_and_writes_nothing(tmp_path, capsys):
    assert run_cli(tmp_path, [{"path": str(PAGES / "bare_list.json")}], "--last-page-reached", "auto", name="x") == 2
    assert "cannot derive" in capsys.readouterr().err and not (tmp_path / "raw").exists()


def test_probe_fixture_shapes_merge(tmp_path):
    probe = Path(__file__).resolve().parent / "fixtures"
    for name, key in (("probe_nooks_calls_page.json", "id"), ("probe_nooks_tasks_oneoff.json", "id"),
                      ("probe_scoops_page.json", "id"), ("probe_sf_paging_page.json", "Id")):
        f = probe / name
        rows, _ = mp.merge_pages([json.loads(f.read_text())])
        assert rows and all(key in r for r in rows)


def test_probe_nooks_page_has_links_next_and_no_has_more():
    pg = json.loads((Path(__file__).resolve().parent / "fixtures" / "probe_nooks_calls_page.json").read_text())
    assert "hasMore" not in pg and mp.derive_last_page([pg]) == (pg["links"]["next"] is None)
