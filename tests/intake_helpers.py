"""Test helper (task 2a): write a synthetic World (tests/test_integration_pipeline.py) as the raw-dump
directory that src/intake.py reads. Everything is invented; no connector is called."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

UTC = timezone.utc
NOOKS_USER = "FAKE-NOOKS-USER-ALEX"          # the owner id used by the integration world's calls


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def dump(rows: list, *, tool: str, query: str = "q", pages: int = 1, last: bool | None = True,
         fetched_at: str, **extra_meta) -> dict:
    meta = {"source_tool": tool, "query": query, "page_count": pages, "reached_last_page": last,
            "fetched_at": fetched_at, **extra_meta}
    return {"meta": meta, "rows": rows}


def write_raw(world, raw_dir, now: datetime, *, calls_last_page: bool = True, credits: int | None = 0,
              strip_links: bool = False, include_gmail: bool = True, include_tasks: bool = True,
              include_prospects: bool = True, gmail_window: int | None = 7,
              owner: str = NOOKS_USER, **overrides) -> Path:
    """Write every raw file for ``world``. ``overrides`` replaces rows per file stem, e.g.
    ``nooks_calls=[...]``. Rows are written exactly as the world holds them; ``strip_links=True`` removes the
    synthetic ``_sf_account_id`` from Nooks accounts (real Nooks rows never carry it)."""
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    fetched = iso(now)
    nooks_accounts = [dict(r) for r in world.nooks_accounts]
    if strip_links:
        for r in nooks_accounts:
            r.pop("_sf_account_id", None)
    spec = {
        "sf_accounts": (world.sf_rows, "soqlQuery", True),
        "sf_contacts": (world.contacts, "soqlQuery", True),
        "nooks_calls": (world.calls, "listCalls", calls_last_page),
        "nooks_dispositions": (world.dispositions, "listCallDispositions", None),
        "nooks_accounts": (nooks_accounts, "listAccounts", True),
        "zoominfo_scoops": (world.scoops, "search_scoops", True),
    }
    if include_prospects:
        spec["nooks_prospects"] = (world.nooks_prospects, "listProspects", True)
    if include_tasks:
        spec["nooks_tasks"] = ([], "listTasks", True)
    if include_gmail:
        spec["gmail_sent"] = (world.sent_threads, "search_threads", None)
    counts, flags = {}, {}
    for name, (rows, tool, last) in spec.items():
        rows = overrides.get(name, rows)
        extra = {}
        query = "q"
        if name == "gmail_sent":
            query = "in:sent to:<addr> newer_than:7d"
            if gmail_window is not None:
                extra["window_days"] = gmail_window
        (raw_dir / f"{name}.json").write_text(json.dumps(dump(rows, tool=tool, last=last, fetched_at=fetched,
                                                                query=query, **extra)))
        counts[name] = len(rows)
        if last is not None:
            flags[name] = last
    meta = {"fetched_at": fetched, "sf_user_id": "FAKE-SF-USER-ALEX", "nooks_user_id": owner,
            "row_counts": counts, "reached_last_page": flags, "zoominfo_credits_used": credits, "warnings": []}
    if credits is None:
        del meta["zoominfo_credits_used"]
    (raw_dir / "run_meta.json").write_text(json.dumps(meta))
    return raw_dir


def read(raw_dir, name: str) -> dict:
    return json.loads((Path(raw_dir) / f"{name}.json").read_text())


def rewrite(raw_dir, name: str, fn) -> None:
    """Load a dump file, apply ``fn(dict)`` (mutates), write it back."""
    d = read(raw_dir, name)
    fn(d)
    (Path(raw_dir) / f"{name}.json").write_text(json.dumps(d))
