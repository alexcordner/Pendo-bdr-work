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

``--last-page-reached`` is what the SESSION saw (the last page said hasMore == false, or the
SOQL result said done == true). It is never guessed here. ``--rows-key`` forces a key when a
page is a dict holding the rows under an unusual key.

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
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Keys tried in this order when a page is a dict (rule 2-7 above).
KNOWN_ROW_KEYS = ("data", "records", "threads", "results", "items", "rows")


class MergeError(ValueError):
    """Malformed input. The CLI prints it and exits 2; nothing is written."""


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
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
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
            pages.append(json.loads(p.read_text(encoding="utf-8")))
        except OSError as err:
            raise MergeError(f"cannot read page file {p}: {err}") from err
        except json.JSONDecodeError as err:
            raise MergeError(f"page file {p} is not valid JSON: {err}") from err
    return pages


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


def _bool_arg(text: str) -> bool:
    t = text.strip().lower()
    if t in ("true", "yes", "1"):
        return True
    if t in ("false", "no", "0"):
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--name", required=True, help="output file stem, e.g. nooks_calls")
    ap.add_argument("--raw-dir", type=Path, default=Path("output/raw"))
    ap.add_argument("--source-tool", required=True)
    ap.add_argument("--query", required=True, help="the query / filters used (no secrets)")
    ap.add_argument("--last-page-reached", type=_bool_arg, default=None,
                    help="true/false as SEEN by the session; omit for a non-paged source")
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
        rows, stats = merge_pages(load_pages(entries), id_field=args.id_field, allow_no_id=args.allow_no_id,
                                  rows_key=args.rows_key)
    except MergeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    dump = build_dump(rows, stats, source_tool=args.source_tool, query=args.query,
                      reached_last_page=args.last_page_reached, fetched_at=args.fetched_at,
                      paged=args.last_page_reached is not None)
    args.raw_dir.mkdir(parents=True, exist_ok=True)
    out = args.raw_dir / f"{args.name}.json"
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
