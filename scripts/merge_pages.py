#!/usr/bin/env python3
"""Merge connector page files into one raw dump file: output/raw/<name>.json.

WHY: a Claude session fetches paged connector data (MCP tools). Large tool results are spilled
to files by the harness. The session must NOT read them into its context; it lists the spilled
file paths in a manifest and runs this script, which writes ``{"meta": {...}, "rows": [...]}``.

Manifest (JSON): a list of ``{"path": "<spilled file>", "page": <int, optional>}``. Without
``page`` the list order is the page order. With ``page`` the entries are sorted by it.

Usage:
    python scripts/merge_pages.py --manifest m.json --name nooks_calls --raw-dir output/raw \
        --source-tool listCalls --query "owner=<id>, page_size=100" --last-page-reached true

``--last-page-reached`` is what the SESSION saw: ``true`` / ``false``, or ``auto`` to derive it from the
FINAL page (``derive_last_page``): Nooks has NO hasMore field, the end is ``links.next == null``
(a non-null ``links.next`` means NOT reached); a page with ``hasMore`` uses that. Salesforce keyset pages
carry no usable end marker (``done`` is true on every page): the session decides (a short page, fewer
rows than LIMIT, means the end). ``auto`` on a page with no end marker is an ERROR, never a guess.
``--rows-key`` forces a key when a page is a dict holding the rows under an unusual key.

Page envelopes understood (see ``extract_rows``), first match wins:
  1. bare list:                      [ {...}, {...} ]
  2. {"data": [...]}                 Nooks list tools, ZoomInfo search_scoops
  3. {"records": [...]}              Salesforce soqlQuery
  4. {"threads": [...]}              Gmail search_threads
  5. {"results": [...]}              generic search tools
  6. {"items": [...]}                generic
  7. {"rows": [...]}                 an already merged dump (meta ignored)
  8. a dict holding exactly ONE list-valued key  -> that list (any name)
  9. MCP text wrapper: [{"type":"text","text":"<json>"}] or {"content":[{"type":"text","text":"<json>"}]}
     is unwrapped and the inner JSON is read with rules 1-8.
A dict with several list-valued keys and none of the known names, an empty dict, a scalar, or an
unreadable file is an ERROR (exit 2). Nothing is guessed. An empty list is a valid empty page.

Rows are de-duplicated by id (``--id-field``, default ``id``; ``Id`` is accepted as a fallback,
so Salesforce rows work with the default). The FIRST occurrence wins, order is kept. A row with
no id is an error unless ``--allow-no-id`` is given (then it is kept and counted).
This script reads local files only. It never calls a connector.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.safeout import UnsafeOutput, safe_out_path  # noqa: E402

# Keys tried in this order when a page is a dict (rule 2-7 above).
KNOWN_ROW_KEYS = ("data", "records", "threads", "results", "items", "rows")


class MergeError(ValueError):
    """Malformed input. The CLI prints it and exits 2; nothing is written."""


def decode_text(raw: bytes) -> str:
    """UTF-8 (with or without BOM) or UTF-16 text; anything else is a MergeError (never UnicodeDecodeError)."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16")
        except UnicodeError as e:
            raise MergeError("file has a UTF-16 BOM but is not valid UTF-16") from e
    if b"\x00" in raw[:8]:                  # BOM-less UTF-16 (NUL bytes are valid UTF-8, so test this first)
        for enc in ("utf-16-le", "utf-16-be"):
            try:
                text = raw.decode(enc)
            except UnicodeError:
                continue
            if text.lstrip()[:1] in ("{", "["):
                return text
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    raise MergeError("file is not valid UTF-8 or UTF-16 text")


def _unwrap_mcp_text(page):
    """Rule 9: ``[{"type":"text","text":"<json>"}]`` or ``{"content": [...]}``. Returns the inner
    JSON value, or the page unchanged if it is not that wrapper."""
    blocks = None
    if isinstance(page, dict) and isinstance(page.get("content"), list) and set(page) <= {"content", "isError"}:
        blocks = page["content"]
    elif isinstance(page, list) and page and all(isinstance(b, dict) and b.get("type") == "text" for b in page):
        blocks = page
    if blocks is None:
        return page
    texts = [b.get("text") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
    if len(texts) != 1 or not isinstance(texts[0], str):
        raise MergeError("MCP text wrapper must hold exactly one text block with a JSON string")
    try:
        return json.loads(texts[0])
    except json.JSONDecodeError as e:
        raise MergeError(f"MCP text wrapper does not hold JSON: {e}") from e


def extract_rows(page, rows_key: str | None = None) -> list:
    """Rows of one page, whatever the envelope. Raises MergeError for anything unrecognised."""
    page = _unwrap_mcp_text(page)
    if isinstance(page, list):
        rows = page
    elif isinstance(page, dict):
        if rows_key:
            if rows_key not in page or not isinstance(page[rows_key], list):
                raise MergeError(f"page has no list under the requested key {rows_key!r} (keys: {sorted(page)})")
            rows = page[rows_key]
        else:
            rows = None
            for key in KNOWN_ROW_KEYS:
                if isinstance(page.get(key), list):
                    rows = page[key]
                    break
            if rows is None:
                list_keys = [k for k, v in page.items() if isinstance(v, list)]
                if len(list_keys) == 1:
                    rows = page[list_keys[0]]
                elif not list_keys:
                    raise MergeError(f"page is a dict with no list inside (keys: {sorted(page)})")
                else:
                    raise MergeError(f"page has several lists and no known row key {list_keys}; use --rows-key")
    else:
        raise MergeError(f"page must be a list or an object, got {type(page).__name__}")
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise MergeError(f"row {i} is {type(row).__name__}, not an object")
    return rows


def _row_id(row: dict, id_field: str):
    for key in (id_field, "Id", "id"):
        v = row.get(key)
        if v not in (None, ""):
            return str(v)
    return None


PAGING_KINDS = ("auto", "nooks", "salesforce", "zoominfo")


def _page_dict(page) -> dict:
    page = _unwrap_mcp_text(page)
    if not isinstance(page, dict):
        raise MergeError("cannot derive reached_last_page: the page is not an object")
    return page


def _nooks_end(last: dict):
    links = last.get("links")
    if isinstance(links, dict) and "next" in links:
        return links["next"] in (None, "")
    if isinstance(last.get("hasMore"), bool):
        return not last["hasMore"]
    return None


def _zoominfo_complete(pages: list, page_size: int = 100) -> bool:
    """ZoomInfo ``search_scoops`` pages, one sequence per type fetched. REAL behaviour (first live dry run):
    ``meta.page.total`` is the ROW COUNT of that page (100 on a full page), NOT a page count, and
    ``meta.totalResults`` DRIFTS between pages, so neither is used. A sequence is complete when its page numbers
    (``meta.page.number``) run 1, 2, 3 ... without a gap, every page but the last is FULL (``page_size`` rows), no id
    repeats inside the sequence, and the last page is SHORT (fewer than ``page_size`` rows, an empty page included).
    The FINAL page of the manifest must end a sequence. The same scoop may appear in two sequences (several types)."""
    expected, ok, seen = None, True, set()
    for n, raw in enumerate(pages, 1):
        pg = _page_dict(raw)
        meta = pg.get("meta") if isinstance(pg.get("meta"), dict) else {}
        info = meta.get("page") if isinstance(meta.get("page"), dict) else {}
        num = info.get("number")
        if not isinstance(num, int) or isinstance(num, bool):
            raise MergeError(f"cannot derive reached_last_page: page {n} has no meta.page.number")
        rows = extract_rows(pg)
        ids = _row_ids(pg, ("id",))
        if num == 1:
            if expected is not None:                    # a new type began before the previous one ended
                ok = False
            expected, seen = 1, set()
        elif expected is None or num != expected:
            ok = False                                  # gap, repeat, or a sequence that does not start at page 1
        for v in ids:
            if v in seen:
                raise MergeError(f"zoominfo: id repeated inside one type's pages (page {n}): a page was given twice")
            seen.add(v)
        expected = num + 1
        if len(rows) < page_size:                       # a short or empty page ends the sequence
            expected = None
    return ok and expected is None


def _ascending(pages: list, key_names: tuple[str, ...], what: str) -> None:
    """Row ids must be strictly ascending across the WHOLE manifest (Nooks and Salesforce keyset paging order by
    id): a duplicated page, a reordered page or a repeated terminal page breaks that."""
    prev = None
    for n, raw in enumerate(pages, 1):
        for row in extract_rows(raw):
            v = next((str(row[k]) for k in key_names if row.get(k) not in (None, "")), None)
            if v is None:
                continue
            if prev is not None and v <= prev:
                raise MergeError(f"{what}: ids are not strictly ascending at page {n} (duplicated, reordered or "
                                 "overlapping pages)")
            prev = v


def _row_ids(raw, key_names) -> list[str]:
    out = []
    for row in extract_rows(raw):
        v = next((str(row[k]) for k in key_names if row.get(k) not in (None, "")), None)
        if v is not None:
            out.append(v)
    return out


def _no_duplicate_ids(pages: list, key_names: tuple[str, ...], what: str) -> None:
    """Order-agnostic: a set of ids across the whole manifest. A page given twice, overlapping pages or a repeated
    terminal page repeat ids whatever the server's ordering is."""
    seen: set[str] = set()
    for n, raw in enumerate(pages, 1):
        for v in _row_ids(raw, key_names):
            if v in seen:
                raise MergeError(f"{what}: id repeated on page {n} (a page was given twice or pages overlap)")
            seen.add(v)


def unquote(text: str) -> str:
    """Percent-decoding (urllib is not imported here: the static safety test bans network-capable modules)."""
    return re.sub(r"%([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), text)


def cursor_id(next_link) -> str | None:
    """The row id a Nooks cursor encodes. ``links.next`` carries ``page[after]=<cursor>`` (URL-encoded); the cursor is
    base64 of ``{"id": "<uuid>", "v": 1}``. None when the link has no decodable cursor (nothing is then checked)."""
    if not isinstance(next_link, str) or not next_link:
        return None
    m = re.search(r"page(?:\[|%5B)after(?:\]|%5D)=([^&]+)", next_link)
    raw = unquote(m.group(1)) if m else next_link
    for cand in (raw, unquote(raw)):
        try:
            pad = cand + "=" * (-len(cand) % 4)
            obj = json.loads(base64.urlsafe_b64decode(pad.encode("ascii")).decode("utf-8"))
        except (ValueError, UnicodeError):
            continue
        if isinstance(obj, dict) and obj.get("id") not in (None, ""):
            return str(obj["id"])
    return None


def derive_last_page(pages: list, kind: str = "auto", sf_limit: int = 2000, expect_pages: int | None = None,
                     strict_order: bool = False, page_size: int | None = 100) -> bool:
    """reached_last_page from the pages themselves (in page order), per source:
      nooks / auto  FINAL page ``links.next`` is null (Nooks has NO hasMore); a cursor means NOT reached.
                    ``hasMore`` is used when a page has that instead.
      salesforce    FINAL page ``done`` is true AND it holds fewer than ``sf_limit`` rows (keyset pages: ``done`` is
                    true on every page, so a full page may have more behind it).
      zoominfo      every page sequence is complete (see ``_zoominfo_complete``: gapless page numbers, full pages,
                    the end is a SHORT or EMPTY page; totals are NOT used, they drift). --expect-pages is optional.
    Without the evidence this is a MergeError: it never returns True on a guess."""
    if kind not in PAGING_KINDS:
        raise MergeError(f"unknown paging kind {kind!r}")
    if not pages:
        raise MergeError("no pages: cannot derive reached_last_page")
    if expect_pages is None and kind != "zoominfo":     # ZoomInfo's page count is unknown up front: optional there
        raise MergeError("--expect-pages N is MANDATORY with --derive-last-page: N is the number of page calls you "
                         "made (a missing middle page cannot be seen in the pages themselves)")
    if expect_pages is not None and len(pages) != expect_pages:
        raise MergeError(f"the manifest has {len(pages)} pages but --expect-pages says {expect_pages}: "
                         "a page is missing or extra")
    if kind == "zoominfo":
        if expect_pages is not None and len(pages) != expect_pages:
            raise MergeError(f"the manifest has {len(pages)} pages but --expect-pages says {expect_pages}")
        return _zoominfo_complete(pages, page_size or 100)
    last = _page_dict(pages[-1])
    if kind == "salesforce":
        if not isinstance(last.get("done"), bool):
            raise MergeError("cannot derive reached_last_page: the final page has no 'done' field")
        for n, raw in enumerate(pages[:-1], 1):                  # every non-final keyset page must be FULL
            rows = len(extract_rows(raw))
            if rows != sf_limit:
                raise MergeError(f"page {n} of {len(pages)} holds {rows} rows, not {sf_limit}: only the final "
                                 "Salesforce page may be short (a middle page is missing or truncated)")
        _no_duplicate_ids(pages, ("Id", "id"), "salesforce")
        if strict_order:
            _ascending(pages, ("Id", "id"), "salesforce")
        return last["done"] and len(extract_rows(last)) < sf_limit
    for n, raw in enumerate(pages[:-1], 1):                      # every non-final page must point at a next page
        pg = _page_dict(raw)
        if _nooks_end(pg) is not False:
            raise MergeError(f"page {n} of {len(pages)} has no next cursor (links.next is null / missing) but more "
                             "pages follow: the manifest is out of order or has a stray page")
    _no_duplicate_ids(pages, ("id", "Id"), "nooks")
    for n, raw in enumerate(pages[:-1], 1):
        pg = _page_dict(raw)
        rows = extract_rows(pg)
        if page_size and len(rows) != page_size:
            raise MergeError(f"page {n} of {len(pages)} holds {len(rows)} rows, not {page_size}: only the final page "
                             "may be short (a middle page is truncated; pass --page-size if you used another size)")
        want = cursor_id((pg.get("links") or {}).get("next"))
        ids = _row_ids(pg, ("id", "Id"))
        if want is not None and ids and want != ids[-1]:
            raise MergeError(f"page {n} of {len(pages)}: the next cursor points after id {want!r} but the page's last "
                             f"row is {ids[-1]!r} (a page between the two is missing or the pages are out of order)")
    if strict_order:
        _ascending(pages, ("id", "Id"), "nooks")
    end = _nooks_end(last)
    if end is None:
        raise MergeError("cannot derive reached_last_page: the final page has neither links.next nor hasMore; "
                         "pass --paging salesforce / zoominfo, or --last-page-reached true or false")
    return end


def ids_hash(ids) -> str:
    """Short checksum of a list of row ids (sorted, newline-joined, sha256, 12 hex characters). The session computes
    the same value from the ids it SAW so a transcription error in an inline result is detected."""
    return hashlib.sha256("\n".join(sorted(str(i) for i in ids)).encode("utf-8")).hexdigest()[:12]


def merge_pages(pages: list, *, id_field: str = "id", allow_no_id: bool = False,
                rows_key: str | None = None) -> tuple[list, dict]:
    """pages: list of already-parsed page values (page order). Returns (rows, stats)."""
    rows: list = []
    seen: set[str] = set()
    stats = {"pages": len(pages), "rows_in": 0, "duplicates_dropped": 0, "rows_without_id": 0,
             "empty_pages": 0}
    for n, page in enumerate(pages, 1):
        try:
            page_rows = extract_rows(page, rows_key)
        except MergeError as e:
            raise MergeError(f"page {n}: {e}") from e
        if not page_rows:
            stats["empty_pages"] += 1
        for row in page_rows:
            stats["rows_in"] += 1
            rid = _row_id(row, id_field)
            stats.setdefault("_ids", []).append(rid)
            if rid is None:
                if not allow_no_id:
                    raise MergeError(f"page {n}: a row has no {id_field!r} (or Id/id); use --allow-no-id to keep such rows")
                stats["rows_without_id"] += 1
                rows.append(row)
                continue
            if rid in seen:
                stats["duplicates_dropped"] += 1
                continue
            seen.add(rid)
            rows.append(row)
    stats["rows_out"] = len(rows)
    return rows, stats


def read_manifest(path: Path) -> list[dict]:
    try:
        data = json.loads(decode_text(Path(path).read_bytes()))
    except (OSError, ValueError) as e:
        raise MergeError(f"cannot read manifest {path}: {e}") from e
    if not isinstance(data, list) or not data:
        raise MergeError("manifest must be a non-empty JSON list of {path, page?}")
    entries = []
    for i, e in enumerate(data):
        if not isinstance(e, dict) or not isinstance(e.get("path"), str) or not e["path"]:
            raise MergeError(f"manifest entry {i} needs a string 'path'")
        if "page" in e and (not isinstance(e["page"], int) or isinstance(e["page"], bool) or e["page"] < 1):
            raise MergeError(f"manifest entry {i}: 'page' must be an integer >= 1")
        entries.append(e)
    with_page = [e for e in entries if "page" in e]
    if with_page and len(with_page) != len(entries):
        raise MergeError("manifest: give 'page' on every entry or on none")
    if with_page:
        nums = [e["page"] for e in entries]
        if len(set(nums)) != len(nums):
            raise MergeError(f"manifest has duplicate page numbers: {sorted(nums)}")
        entries = sorted(entries, key=lambda e: e["page"])
    return entries


def load_pages(entries: list[dict]) -> list:
    pages = []
    for e in entries:
        p = Path(e["path"])
        try:
            pages.append(json.loads(decode_text(p.read_bytes())))
        except OSError as err:
            raise MergeError(f"cannot read page file {p}: {err}") from err
        except MergeError as err:
            raise MergeError(f"page file {p}: {err}") from err
        except ValueError as err:
            raise MergeError(f"page file {p} is not valid JSON: {err}") from err
    return pages


def read_meta_json(path: Path) -> dict:
    try:
        d = json.loads(decode_text(Path(path).read_bytes()))
    except (OSError, ValueError) as e:
        raise MergeError(f"cannot read --meta-json {path}: {e}") from e
    if not isinstance(d, dict):
        raise MergeError("--meta-json must hold a JSON object")
    return d


def build_dump(rows: list, stats: dict, *, source_tool: str, query: str, reached_last_page: bool | None,
               fetched_at: str | None = None, paged: bool = True) -> dict:
    """The ``{"meta", "rows"}`` object that intake.load_raw reads. ``reached_last_page`` None means
    the source is not paged (single call)."""
    meta = {
        "source_tool": source_tool, "query": query, "page_count": stats["pages"],
        "reached_last_page": reached_last_page if paged else None,
        "fetched_at": fetched_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "merge": {k: stats[k] for k in ("rows_in", "duplicates_dropped", "rows_without_id", "empty_pages")},
    }
    return {"meta": meta, "rows": rows}


def _bool_arg(text: str):
    t = text.strip().lower()
    if t in ("true", "yes", "1"):
        return True
    if t in ("false", "no", "0"):
        return False
    if t == "auto":
        return "auto"
    raise argparse.ArgumentTypeError("expected true, false or auto")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--name", required=True, help="output file stem, e.g. nooks_calls")
    ap.add_argument("--raw-dir", type=Path, default=Path("output/raw"))
    ap.add_argument("--source-tool", required=True)
    ap.add_argument("--query", required=True, help="the query / filters used (no secrets)")
    ap.add_argument("--last-page-reached", type=_bool_arg, default=None,
                    help="true/false as SEEN by the session, or auto (from the final page's links.next / hasMore); "
                         "omit for a non-paged source")
    ap.add_argument("--derive-last-page", action="store_true",
                    help="set reached_last_page from the pages (see --paging); same as --last-page-reached auto")
    ap.add_argument("--paging", choices=PAGING_KINDS, default="auto",
                    help="rule for --derive-last-page: nooks (links.next null), salesforce (done and a short page), "
                         "zoominfo (gapless meta.page.number, ends on a short or empty page); auto = nooks")
    ap.add_argument("--sf-limit", type=int, default=2000, help="LIMIT used for Salesforce keyset pages")
    ap.add_argument("--expect-pages", type=int, default=None,
                    help="number of pages the session fetched (its own call count); a different manifest length "
                         "is an error. MANDATORY with --derive-last-page / --last-page-reached auto")
    ap.add_argument("--page-size", type=int, default=100,
                    help="rows per non-final Nooks page (the page_size used; non-final pages must be full)")
    ap.add_argument("--strict-order", action="store_true",
                    help="also require row ids strictly ascending across the manifest (off by default: the server's "
                         "ordering of mixed-case ids is unverified and a false rejection would stall every run)")
    ap.add_argument("--meta-json", type=Path, default=None,
                    help="JSON object whose keys are added to meta (e.g. window_days, searched_address_hashes, "
                         "hash_salt); it can never replace the keys this script computes")
    ap.add_argument("--verify-rows", type=int, default=None,
                    help="rows the session SAW (before de-duplication); a different count exits 2 and writes nothing. "
                         "For results that came back INLINE and were saved by hand")
    ap.add_argument("--verify-ids-hash", default=None,
                    help="12-hex checksum of the row ids the session saw (see docs/fetch_runbook.md); a mismatch exits 2")
    ap.add_argument("--id-field", default="id")
    ap.add_argument("--rows-key", default=None)
    ap.add_argument("--allow-no-id", action="store_true")
    ap.add_argument("--fetched-at", default=None, help="ISO UTC; default now")
    args = ap.parse_args(argv)
    if "/" in args.name or "\\" in args.name or not args.name:
        print("error: --name must be a plain file stem", file=sys.stderr)
        return 2
    try:
        entries = read_manifest(args.manifest)
        pages = load_pages(entries)
        rows, stats = merge_pages(pages, id_field=args.id_field, allow_no_id=args.allow_no_id,
                                  rows_key=args.rows_key)
        if args.derive_last_page and args.last_page_reached not in (None, "auto"):
            raise MergeError("--derive-last-page cannot be combined with --last-page-reached true/false")
        if args.derive_last_page or args.last_page_reached == "auto":
            args.last_page_reached = derive_last_page(pages, args.paging, args.sf_limit, args.expect_pages,
                                                      args.strict_order, args.page_size)
        elif args.expect_pages is not None and args.expect_pages != len(pages):
            raise MergeError(f"the manifest has {len(pages)} pages but --expect-pages says {args.expect_pages}")
        extra = read_meta_json(args.meta_json) if args.meta_json else {}
        if args.verify_rows is not None and args.verify_rows != stats["rows_in"]:
            raise MergeError(f"--verify-rows says {args.verify_rows} rows but the pages hold {stats['rows_in']}: "
                             "the inline result was not saved completely")
        if args.verify_ids_hash is not None:
            got = ids_hash([i for i in stats.get("_ids", []) if i is not None])
            if got != args.verify_ids_hash.strip().lower():
                raise MergeError(f"--verify-ids-hash {args.verify_ids_hash} does not match the saved row ids ({got}): "
                                 "a row id was changed, lost or mistyped when the inline result was written")
    except MergeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    dump = build_dump(rows, stats, source_tool=args.source_tool, query=args.query,
                      reached_last_page=args.last_page_reached, fetched_at=args.fetched_at,
                      paged=args.last_page_reached is not None)
    for k, v in extra.items():
        dump["meta"].setdefault(k, v)
    try:    # real data: only under output/ (gitignored), never through a symlink (scripts/safeout.py, review 2f)
        out = safe_out_path(args.raw_dir / f"{args.name}.json")
    except UnsafeOutput as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(dump, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out)                       # atomic: a half-written dump is never left behind
    # Counts only on stdout: no row content, so nothing sensitive enters the session's context.
    print(f"wrote {out}: {stats['rows_out']} rows from {stats['pages']} pages "
          f"({stats['duplicates_dropped']} duplicates dropped, {stats['empty_pages']} empty pages), "
          f"reached_last_page={dump['meta']['reached_last_page']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
