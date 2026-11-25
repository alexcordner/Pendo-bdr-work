#!/usr/bin/env python3
"""Gmail search queue and coverage record for the ranked shortlist (review N3). Local files only.

    python scripts/gmail_queue.py queue  --raw-dir output/raw --state-dir state --out output/gmail_queue.json
    python scripts/gmail_queue.py hashes --addresses output/gmail_queue.json --out output/gmail_meta.json [--also output/gmail_meta.json]

``queue`` runs the pipeline as a dry run (it writes no state and no log) and writes the REAL dotted addresses of the
ranked, chosen prospects that are not yet covered by gmail_sent.json to --out (gitignored output/): the session
reads them ONE AT A TIME into ``search_threads`` queries (addresses enter the session's context, never git, logs or
chat). It prints a count only. ``hashes`` turns the file of addresses that were actually searched into
``{"hash_salt", "searched_address_hashes"}`` (salted hashes, no raw addresses) for ``merge_pages.py --meta-json``;
``--also`` unions the hashes of an earlier round. Flow: dry run -> queue -> search -> hashes -> merge gmail_sent ->
re-run, until the pipeline reports 0 ranked prospects uncovered.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config  # noqa: E402
from src.intake import DEFAULT_ADDRESS_SALT, address_hash  # noqa: E402
from src.pipeline import PipelineStopped, run_pipeline  # noqa: E402


def _now(text):
    dt = datetime.fromisoformat(text.replace("Z", "+00:00")) if text else datetime.now(timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def cmd_queue(a) -> int:
    cfg = load_config(a.config)
    try:
        res = run_pipeline(a.raw_dir, cfg, a.state_dir, _now(a.now), save_state=False)
    except PipelineStopped as e:
        print("STOPPED: " + "; ".join(e.reasons), file=sys.stderr)
        return 2
    want = set(res.gmail_uncovered)
    salt = _salt(a.raw_dir)
    addrs = []
    for ps in res.rank_result.prospects.values():
        for p in ps:
            h = address_hash(p.email, salt)
            if h in want and p.email not in addrs:
                addrs.append(p.email)
    Path(a.out).write_text(json.dumps(sorted(addrs)), encoding="utf-8")
    print(f"{len(addrs)} addresses to search written to {a.out} ({len(res.gmail_uncovered)} uncovered hashes)")
    return 0


def _salt(raw_dir) -> str:
    try:
        meta = json.loads((Path(raw_dir) / "gmail_sent.json").read_text(encoding="utf-8"))["meta"]
        s = meta.get("hash_salt")
        return s if isinstance(s, str) and s else DEFAULT_ADDRESS_SALT
    except (OSError, ValueError, KeyError, TypeError):
        return DEFAULT_ADDRESS_SALT


def cmd_hashes(a) -> int:
    addrs = json.loads(Path(a.addresses).read_text(encoding="utf-8"))
    if not isinstance(addrs, list):
        print("error: --addresses must hold a JSON list", file=sys.stderr)
        return 2
    hashes = {h for x in addrs if (h := address_hash(x, a.salt))}
    for prev in a.also or []:
        try:
            old = json.loads(Path(prev).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        old = old.get("meta", old) if isinstance(old, dict) else {}
        if old.get("hash_salt", a.salt) == a.salt:
            hashes |= set(old.get("searched_address_hashes") or [])
    Path(a.out).write_text(json.dumps({"hash_salt": a.salt, "searched_address_hashes": sorted(hashes)}), encoding="utf-8")
    print(f"{len(hashes)} address hashes written to {a.out} (no raw addresses)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("queue")
    q.add_argument("--raw-dir", default="output/raw")
    q.add_argument("--state-dir", default="state")
    q.add_argument("--config", default=str(Path(__file__).resolve().parent.parent / "config" / "config.yaml"))
    q.add_argument("--now", default=None)
    q.add_argument("--out", required=True)
    q.set_defaults(fn=cmd_queue)
    h = sub.add_parser("hashes")
    h.add_argument("--addresses", required=True)
    h.add_argument("--out", required=True)
    h.add_argument("--salt", default=DEFAULT_ADDRESS_SALT)
    h.add_argument("--also", action="append", help="an earlier meta json / gmail_sent.json whose hashes are kept")
    h.set_defaults(fn=cmd_hashes)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
