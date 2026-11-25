"""Tests for scripts/merge_pages.py (task 2a). Synthetic page files only (tests/fixtures/intake_pages/)."""
import json
from pathlib import Path

import pytest

from scripts import merge_pages as mp

PAGES = Path(__file__).resolve().parent / "fixtures" / "intake_pages"


def dl(pages, kind="auto", **kw):
    """derive_last_page with the now-mandatory expect_pages filled in and no Nooks page-size rule unless asked."""
    kw.setdefault("expect_pages", len(pages))
    kw.setdefault("page_size", None)
    return mp.derive_last_page(pages, kind, **kw)


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
    assert dl([page("nooks_page_more.json"), page("nooks_page_last.json")]) is True
    assert dl([page("nooks_page_more.json")]) is False
    assert dl([page("nooks_page_empty_end.json")]) is True        # cursor past the end


def test_derive_last_page_reordered_pages_are_an_error_not_a_guess():
    with pytest.raises(mp.MergeError, match="no next cursor"):
        dl([page("nooks_page_last.json"), page("nooks_page_more.json")])


def test_derive_last_page_has_more_and_mcp_wrapper():
    assert dl([page("has_more_true.json")]) is False
    assert dl([page("data_page2.json")]) is True
    wrapped = [{"type": "text", "text": json.dumps(page("nooks_page_last.json"))}]
    assert dl([wrapped]) is True


@pytest.mark.parametrize("fixture", ["bare_list.json", "records_page2.json", "threads_page.json"])
def test_derive_last_page_without_a_marker_is_an_error(fixture):
    with pytest.raises(mp.MergeError, match="cannot derive"):
        dl([page(fixture)])
    with pytest.raises(mp.MergeError, match="no pages"):
        dl([])


def test_cli_auto_derives_from_links_next(tmp_path):
    assert run_cli(tmp_path, [{"path": str(PAGES / "nooks_page_more.json")}, {"path": str(PAGES / "nooks_page_last.json")}],
                   "--last-page-reached", "auto", "--expect-pages", "2", "--page-size", "2") == 0
    assert json.loads((tmp_path / "raw" / "nooks_calls.json").read_text())["meta"]["reached_last_page"] is True
    assert run_cli(tmp_path, [{"path": str(PAGES / "nooks_page_more.json")}], "--last-page-reached", "auto",
                   "--expect-pages", "1", "--page-size", "2") == 0
    assert json.loads((tmp_path / "raw" / "nooks_calls.json").read_text())["meta"]["reached_last_page"] is False


def test_cli_auto_without_marker_fails_and_writes_nothing(tmp_path, capsys):
    assert run_cli(tmp_path, [{"path": str(PAGES / "bare_list.json")}], "--last-page-reached", "auto", "--expect-pages", "1",
                   name="x") == 2
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
    assert "hasMore" not in pg and dl([pg]) == (pg["links"]["next"] is None)


# ---------------------------------------------------------------- --derive-last-page rules per source (review F2)
def sf_page(n, done=True, start=0):
    return {"totalSize": n, "done": done, "records": [{"Id": f"001FAKE{i:08d}"} for i in range(start, start + n)]}


def zi_page(num, rows, total_results=None, seq="a"):
    """A real search_scoops page: meta.page.total is the ROW COUNT of the page, totalResults drifts."""
    meta = {"page": {"number": num, "total": rows}}
    if total_results is not None:
        meta["totalResults"] = total_results
    return {"data": [{"id": f"{seq}{num}-{i}"} for i in range(rows)], "meta": meta}


def test_salesforce_short_final_page_with_done_is_the_end():
    assert dl([sf_page(2000), sf_page(37, start=2000)], "salesforce") is True


def test_salesforce_full_final_page_is_not_the_end():
    assert dl([sf_page(2000), sf_page(2000, start=2000)], "salesforce") is False
    assert dl([sf_page(10)], "salesforce", sf_limit=10) is False


def test_salesforce_done_false_or_missing():
    assert dl([sf_page(5, done=False)], "salesforce") is False
    with pytest.raises(mp.MergeError, match="'done'"):
        dl([{"records": []}], "salesforce")


def test_zoominfo_ends_on_a_short_page():
    assert dl([zi_page(1, 3), zi_page(2, 3), zi_page(3, 2)], "zoominfo", page_size=3) is True


def test_zoominfo_ends_on_an_empty_page_after_full_pages():
    assert dl([zi_page(1, 3), zi_page(2, 3), zi_page(3, 0)], "zoominfo", page_size=3) is True
    assert dl([zi_page(1, 0)], "zoominfo", page_size=3) is True


def test_zoominfo_full_final_page_is_not_the_end():
    assert dl([zi_page(1, 3), zi_page(2, 3)], "zoominfo", page_size=3) is False


def test_zoominfo_drifting_totals_do_not_matter():
    """REAL: Executive Move read 458/435/437/383 while 400 rows came back; Partnership said 53 and returned 62."""
    pages = [zi_page(1, 3, 458), zi_page(2, 3, 435), zi_page(3, 3, 437), zi_page(4, 1, 383)]
    assert dl(pages, "zoominfo", page_size=3) is True


def test_zoominfo_meta_page_total_is_the_row_count_not_a_page_count():
    pages = [zi_page(1, 100), zi_page(2, 100), zi_page(3, 37)]
    assert pages[0]["meta"]["page"]["total"] == 100
    assert dl(pages, "zoominfo", page_size=100) is True


def test_zoominfo_gap_or_repeated_page_number_is_not_complete():
    assert dl([zi_page(1, 3), zi_page(3, 1)], "zoominfo", page_size=3) is False
    assert dl([zi_page(1, 3), zi_page(1, 3, seq="b"), zi_page(2, 1, seq="b")], "zoominfo", page_size=3) is False
    assert dl([zi_page(2, 1)], "zoominfo", page_size=3) is False                      # does not start at page 1


def test_zoominfo_a_page_given_twice_is_an_error():
    with pytest.raises(mp.MergeError, match="id repeated inside one type's pages"):
        dl([zi_page(1, 3), zi_page(2, 3), zi_page(2, 3)], "zoominfo", page_size=3)


def test_zoominfo_several_type_sequences_each_end_short_or_empty():
    good = [zi_page(1, 3, seq="a"), zi_page(2, 1, seq="a"), zi_page(1, 2, seq="b"), zi_page(1, 3, seq="c"), zi_page(2, 0, seq="c")]
    assert dl(good, "zoominfo", page_size=3) is True
    truncated_first = [zi_page(1, 3, seq="a"), zi_page(1, 2, seq="b")]                  # type a stopped after a FULL page 1
    assert dl(truncated_first, "zoominfo", page_size=3) is False


def test_zoominfo_the_same_scoop_may_appear_under_two_types():
    a = {"data": [{"id": "S1"}, {"id": "S2"}], "meta": {"page": {"number": 1, "total": 2}}}
    b = {"data": [{"id": "S2"}], "meta": {"page": {"number": 1, "total": 1}}}
    assert dl([a, b], "zoominfo", page_size=3) is True


def test_zoominfo_expect_pages_is_optional_but_checked_when_given():
    pages = [zi_page(1, 3), zi_page(2, 1)]
    assert mp.derive_last_page(pages, "zoominfo", page_size=3) is True                # no --expect-pages: fine
    assert mp.derive_last_page(pages, "zoominfo", expect_pages=2, page_size=3) is True
    with pytest.raises(mp.MergeError, match="--expect-pages says 3"):
        mp.derive_last_page(pages, "zoominfo", expect_pages=3, page_size=3)


def test_zoominfo_page_without_meta_is_an_error():
    with pytest.raises(mp.MergeError, match="meta.page.number"):
        dl([{"data": []}], "zoominfo")


def test_unknown_paging_kind():
    with pytest.raises(mp.MergeError, match="unknown paging kind"):
        dl([sf_page(1)], "ldap")


def _write(tmp_path, name, obj):
    p = tmp_path / name
    p.write_text(json.dumps(obj))
    return {"path": str(p)}


def test_cli_derive_last_page_salesforce_and_zoominfo(tmp_path):
    assert run_cli(tmp_path, [_write(tmp_path, "a.json", sf_page(2000)), _write(tmp_path, "b.json", sf_page(3, start=2000))],
                   "--derive-last-page", "--paging", "salesforce", "--expect-pages", "2", name="sf_accounts", ) == 0
    assert json.loads((tmp_path / "raw" / "sf_accounts.json").read_text())["meta"]["reached_last_page"] is True
    assert run_cli(tmp_path, [_write(tmp_path, "c.json", zi_page(1, 2)), _write(tmp_path, "d.json", zi_page(2, 1))],
                   "--derive-last-page", "--paging", "zoominfo", "--page-size", "2", name="zoominfo_scoops") == 0
    assert json.loads((tmp_path / "raw" / "zoominfo_scoops.json").read_text())["meta"]["reached_last_page"] is True


def test_cli_derive_cannot_be_combined_with_a_manual_flag(tmp_path, capsys):
    rc = run_cli(tmp_path, [{"path": str(PAGES / "nooks_page_last.json")}], "--derive-last-page", "--last-page-reached", "true")
    assert rc == 2 and "cannot be combined" in capsys.readouterr().err


def test_cli_derive_never_defaults_to_true(tmp_path, capsys):
    assert run_cli(tmp_path, [{"path": str(PAGES / "bare_list.json")}], "--derive-last-page", "--expect-pages", "1") == 2
    assert not (tmp_path / "raw").exists()


# ---------------------------------------------------------------- encodings (review F7)
def test_utf8_bom_and_utf16_pages_are_read(tmp_path):
    (tmp_path / "bom.json").write_bytes(b"\xef\xbb\xbf" + (PAGES / "bare_list.json").read_bytes())
    (tmp_path / "u16.json").write_bytes((PAGES / "bare_list.json").read_text().encode("utf-16"))
    pages = mp.load_pages([{"path": str(tmp_path / "bom.json")}, {"path": str(tmp_path / "u16.json")}])
    assert len(pages) == 2 and pages[0] == pages[1]


@pytest.mark.parametrize("payload", [b"\xff\xfe\x00garbage\x80", b"\x80\x81\x82", b"\xff\xfe\xd8\x00"])
def test_binary_page_is_a_merge_error_not_a_traceback(tmp_path, payload):
    (tmp_path / "bad.json").write_bytes(payload)
    with pytest.raises(mp.MergeError):
        mp.load_pages([{"path": str(tmp_path / "bad.json")}])


def test_cli_binary_page_and_manifest_exit_2(tmp_path, capsys):
    (tmp_path / "bad.json").write_bytes(b"\x80\x81")
    assert run_cli(tmp_path, [{"path": str(tmp_path / "bad.json")}]) == 2
    (tmp_path / "m2.json").write_bytes(b"\x80\x81")
    with pytest.raises(mp.MergeError):
        mp.read_manifest(tmp_path / "m2.json")
    assert "Traceback" not in capsys.readouterr().err


# ---------------------------------------------------------------- manifest consistency checks (review N1)
def nk(ids, nxt):
    return {"data": [{"id": i} for i in ids], "links": {"next": nxt}}


def test_nooks_good_three_page_manifest():
    assert dl([nk(["a", "b"], "c1"), nk(["c", "d"], "c2"), nk(["e"], None)], expect_pages=3) is True


def test_nooks_next_null_in_the_middle_is_an_error():
    with pytest.raises(mp.MergeError, match="page 1 of 3 has no next cursor"):
        dl([nk(["a"], None), nk(["b"], "c"), nk(["c"], None)])


def test_nooks_duplicated_terminal_page_is_an_error():
    with pytest.raises(mp.MergeError):
        dl([nk(["a"], "c"), nk(["b"], None), nk(["b"], None)])


def test_nooks_overlapping_pages_are_an_error_without_relying_on_order():
    with pytest.raises(mp.MergeError, match="id repeated on page 2"):
        dl([nk(["a", "c"], "x"), nk(["c", "d"], None)])


def test_descending_ids_pass_by_default_and_fail_only_with_strict_order():
    pages = [nk(["m", "n"], "x"), nk(["a", "b"], None)]               # unverified server order: not rejected by default
    assert dl(pages) is True
    with pytest.raises(mp.MergeError, match="strictly ascending"):
        dl(pages, strict_order=True)


def test_mixed_case_ids_are_not_falsely_rejected_by_default():
    assert dl([nk(["0B", "0a"], "x"), nk(["0C"], None)]) is True
    assert dl([sf_page(2000, start=0), sf_page(1, start=2000)], "salesforce") is True


def test_missing_middle_page_is_caught_only_by_expect_pages():
    pages = [nk(["a"], "c1"), nk(["c"], None)]                         # page 2 of 3 never made it into the manifest
    assert dl(pages) is True                          # undetectable from the pages alone
    with pytest.raises(mp.MergeError, match="--expect-pages says 3"):
        dl(pages, expect_pages=3)


def test_salesforce_short_middle_page_is_an_error():
    with pytest.raises(mp.MergeError, match="page 1 of 2 holds 1000 rows, not 2000"):
        dl([sf_page(1000), sf_page(500, start=1000)], "salesforce")


def test_salesforce_duplicate_ids_are_an_error_and_order_only_with_strict_order():
    with pytest.raises(mp.MergeError, match="id repeated on page 2"):
        dl([sf_page(2000), sf_page(5, start=0)], "salesforce")
    pages = [sf_page(2000, start=2000), sf_page(5, start=0)]          # descending but distinct
    assert dl(pages, "salesforce") is True
    with pytest.raises(mp.MergeError, match="strictly ascending"):
        dl(pages, "salesforce", strict_order=True)


def test_salesforce_single_short_page_with_expect_pages():
    assert dl([sf_page(7)], "salesforce", expect_pages=1) is True
    with pytest.raises(mp.MergeError):
        dl([sf_page(7)], "salesforce", expect_pages=2)


def test_cli_expect_pages_and_meta_json(tmp_path):
    entries = [{"path": str(PAGES / "nooks_page_more.json")}, {"path": str(PAGES / "nooks_page_last.json")}]
    assert run_cli(tmp_path, entries, "--derive-last-page", "--expect-pages", "3", "--page-size", "2") == 2
    assert not (tmp_path / "raw").exists()
    (tmp_path / "meta.json").write_text(json.dumps({"window_days": 7, "searched_address_hashes": ["abc"],
                                                    "reached_last_page": "SPOOF", "query": "SPOOF"}))
    assert run_cli(tmp_path, entries, "--derive-last-page", "--expect-pages", "2", "--page-size", "2", "--meta-json",
                   str(tmp_path / "meta.json")) == 0
    meta = json.loads((tmp_path / "raw" / "nooks_calls.json").read_text())["meta"]
    assert meta["window_days"] == 7 and meta["searched_address_hashes"] == ["abc"]
    assert meta["reached_last_page"] is True and meta["query"] == "owner=<id>"      # computed keys are never overridden


def test_cli_expect_pages_without_derive_still_checks(tmp_path):
    assert run_cli(tmp_path, [{"path": str(PAGES / "bare_list.json")}], "--expect-pages", "2") == 2


def test_cli_bad_meta_json(tmp_path):
    (tmp_path / "meta.json").write_text("[1]")
    assert run_cli(tmp_path, [{"path": str(PAGES / "bare_list.json")}], "--meta-json", str(tmp_path / "meta.json")) == 2


# ---------------------------------------------------------------- expect-pages mandatory, cursor continuity, page size (review round 3)
import base64 as _b64                                                      # noqa: E402
from urllib.parse import quote as _quote                                   # noqa: E402


def cur(row_id):
    raw = _b64.b64encode(json.dumps({"id": row_id, "v": 1}).encode()).decode()
    return f"/v1/calls?page%5Bafter%5D={_quote(raw, safe='')}&page_size=100"


def test_expect_pages_is_mandatory_for_derive():
    with pytest.raises(mp.MergeError, match="MANDATORY"):
        mp.derive_last_page([nk(["a"], None)])
    with pytest.raises(mp.MergeError, match="MANDATORY"):
        mp.derive_last_page([sf_page(1)], "salesforce")
    assert mp.derive_last_page([zi_page(1, 1)], "zoominfo", page_size=3) is True          # ZoomInfo: optional


def test_cli_derive_without_expect_pages_exits_2(tmp_path, capsys):
    assert run_cli(tmp_path, [{"path": str(PAGES / "nooks_page_last.json")}], "--derive-last-page") == 2
    assert "MANDATORY" in capsys.readouterr().err and not (tmp_path / "raw").exists()


def test_cursor_id_decodes_encoded_and_plain_cursors():
    assert mp.cursor_id(cur("abc-1")) == "abc-1"
    raw = _b64.b64encode(json.dumps({"id": "x9", "v": 1}).encode()).decode()
    assert mp.cursor_id(raw) == "x9" and mp.cursor_id(_quote(raw, safe="")) == "x9"
    for bad in (None, "", "/v1/calls?x=1", "not base64 !!", 5):
        assert mp.cursor_id(bad) is None


def test_cursor_continuity_passes_when_the_cursor_follows_the_last_row():
    assert dl([nk(["a", "b"], cur("b")), nk(["c", "d"], cur("d")), nk(["e"], None)]) is True


def test_cursor_pointing_past_a_missing_page_is_an_error():
    with pytest.raises(mp.MergeError, match="next cursor points after id 'd'"):
        dl([nk(["a", "b"], cur("d")), nk(["e"], None)])


def test_undecodable_cursors_skip_the_continuity_check():
    assert dl([nk(["a"], "opaque-1"), nk(["b"], None)]) is True


def test_nooks_non_final_page_must_be_full():
    pages = [nk([f"a{i}" for i in range(99)], "c1"), nk(["z"], None)]
    with pytest.raises(mp.MergeError, match="holds 99 rows, not 100"):
        dl(pages, page_size=100)
    assert dl(pages, page_size=99) is True and dl(pages, page_size=None) is True


def test_cli_strict_order_flag(tmp_path):
    a = _write(tmp_path, "a.json", nk(["m", "n"], "c1"))
    b = _write(tmp_path, "b.json", nk(["a"], None))
    assert run_cli(tmp_path, [a, b], "--derive-last-page", "--expect-pages", "2", "--page-size", "2") == 0
    assert run_cli(tmp_path, [a, b], "--derive-last-page", "--expect-pages", "2", "--page-size", "2", "--strict-order",
                   name="other") == 2


# ---------------------------------------------------------------- inline results: --verify-rows / --verify-ids-hash
def _inline(tmp_path, rows):
    return [_write(tmp_path, "inline.json", {"data": rows})]


def test_ids_hash_is_order_independent_and_short():
    assert mp.ids_hash(["b", "a", "c"]) == mp.ids_hash(["c", "a", "b"]) and len(mp.ids_hash(["a"])) == 12
    assert mp.ids_hash(["a", "b"]) != mp.ids_hash(["a", "bb"])


def test_verify_rows_and_hash_pass_for_a_faithful_copy(tmp_path):
    rows = [{"id": f"R{i}", "x": 1} for i in range(5)]
    h = mp.ids_hash([r["id"] for r in rows])
    assert run_cli(tmp_path, _inline(tmp_path, rows), "--verify-rows", "5", "--verify-ids-hash", h, name="nooks_tasks") == 0


def test_verify_rows_catches_a_lost_row(tmp_path, capsys):
    rows = [{"id": f"R{i}"} for i in range(5)]
    assert run_cli(tmp_path, _inline(tmp_path, rows[:4]), "--verify-rows", "5") == 2
    assert "--verify-rows says 5 rows but the pages hold 4" in capsys.readouterr().err
    assert not (tmp_path / "raw").exists()


def test_verify_ids_hash_catches_a_mistyped_id(tmp_path, capsys):
    seen = [f"R{i}" for i in range(5)]
    saved = [{"id": "R0"}, {"id": "R1"}, {"id": "R2"}, {"id": "R3"}, {"id": "R4_typo"}]
    assert run_cli(tmp_path, _inline(tmp_path, saved), "--verify-rows", "5", "--verify-ids-hash", mp.ids_hash(seen)) == 2
    assert "does not match the saved row ids" in capsys.readouterr().err and not (tmp_path / "raw").exists()


def test_verify_counts_rows_before_deduplication(tmp_path):
    rows = [{"id": "A"}, {"id": "A"}, {"id": "B"}]
    assert run_cli(tmp_path, _inline(tmp_path, rows), "--verify-rows", "3", "--verify-ids-hash",
                   mp.ids_hash(["A", "A", "B"])) == 0


def test_verify_hash_is_case_and_space_tolerant(tmp_path):
    rows = [{"id": "A"}]
    assert run_cli(tmp_path, _inline(tmp_path, rows), "--verify-ids-hash", " " + mp.ids_hash(["A"]).upper() + " ") == 0


def test_internal_id_list_never_reaches_the_dump(tmp_path):
    run_cli(tmp_path, _inline(tmp_path, [{"id": "A"}]), "--verify-rows", "1")
    meta = json.loads((tmp_path / "raw" / "nooks_calls.json").read_text())["meta"]
    assert "_ids" not in json.dumps(meta)
