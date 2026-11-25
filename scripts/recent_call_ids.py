#!/usr/bin/env python3
"""Which Nooks prospect ids must be resolved so recent calls can be tied to accounts (review N2).

Reads output/raw (written by the fetch step), finds the distinct Nooks prospect ids on calls from the last 14
days that no prospect carries yet, and writes them in batches of 25 for ``listProspects filter_id=<uuid,uuid,...>``:

    python scripts/recent_call_ids.py --raw-dir output/raw --out output/raw/_prospect_id_batches.json [--now ISO] [--all]

The session runs listProspects once per batch (comma-separated uuids), merges the pages into
output/raw/nooks_prospects.json (``merge_pages.py --name nooks_prospects``, no end flag), and re-runs the pipeline:
``crmId`` -> Salesforce Contact -> AccountId ties those calls to accounts. Prints counts only (ids are uuids, not
personal data, but they are never printed). Local files only; never calls a connector.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.intake import (RECENT_CALL_DAYS, build_prospects, id_batches, infer_prospect_ids_from_calls, load_raw,  # noqa: E402
                        recent_call_prospect_ids, untied_recent_calls)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--raw-dir", default="output/raw")
    ap.add_argument("--out", required=True)
    ap.add_argument("--now", default=None, help="ISO timestamp (default now, UTC)")
    ap.add_argument("--all", action="store_true", help="every recent prospect id, not only the unresolved ones")
    args = ap.parse_args(argv)
    now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    data = load_raw(args.raw_dir, now=now)
    if not data.report.ok:
        print("error: raw data has errors: " + "; ".join(data.report.errors), file=sys.stderr)
        return 2
    prospects = infer_prospect_ids_from_calls(data, build_prospects(data, {}, {}))
    ids = recent_call_prospect_ids(data, now) if args.all else untied_recent_calls(data, prospects, now)["ids_to_resolve"]
    batches = id_batches(ids)
    Path(args.out).write_text(json.dumps(batches), encoding="utf-8")
    print(f"{len(ids)} prospect ids from calls in the last {RECENT_CALL_DAYS} days to resolve, "
          f"{len(batches)} batches of up to 25 written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
