#!/usr/bin/env python3
"""Dry-run call sheet: run the pipeline over output/raw, build the top-N sheet, print it, save it.

Offline and read-only: it calls src.pipeline.run_pipeline (dry run, never live) and src.callsheet.build_sheet,
then prints render_sheet_text + the markdown summary + the bucket counts, and writes
output/dry_run_sheet.txt and output/dry_run_sheet.md (gitignored: real names stay out of git).
Nothing is sent or written to Nooks, Gmail or Salesforce.

    python scripts/dry_run_sheet.py --raw-dir output/raw --state-dir state --log-dir logs --limit 10
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import callsheet  # noqa: E402
from src.config import load_config  # noqa: E402
from src.pipeline import PipelineStopped, _parse_now, run_pipeline  # noqa: E402


def bucket_counts(sheet) -> dict[str, int]:
    return {"ranked_total": sheet.ranked_total, "on_sheet": len(sheet.lines),
            "needs_prospect": len(sheet.needs_prospect), "spilled": len(sheet.spilled),
            "skipped": len(sheet.skipped), "dropped_before_sheet": len(sheet.dropped)}


def build(raw_dir, state_dir, log_dir, config_path, now, limit: int = 10):
    """Run the pipeline in dry-run mode and assemble the sheet. Returns (PipelineResult, Sheet)."""
    cfg = load_config(config_path)
    res = run_pipeline(raw_dir, cfg, state_dir, now, live=False, log_dir=log_dir)
    sheet = callsheet.build_sheet(res.rank_result, res.accounts_result.accounts, res.signals, res.prospects,
                                  cfg, now, limit=limit)
    return res, sheet


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--raw-dir", default="output/raw")
    ap.add_argument("--state-dir", default="state")
    ap.add_argument("--log-dir", default="logs")
    ap.add_argument("--config", default=str(ROOT / "config" / "config.yaml"))
    ap.add_argument("--out-dir", default="output")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--now", default=None, help="ISO timestamp (default: now, UTC)")
    args = ap.parse_args(argv)
    try:
        res, sheet = build(args.raw_dir, args.state_dir, args.log_dir, args.config, _parse_now(args.now),
                           args.limit)
    except PipelineStopped as e:
        print("STOPPED. Nothing was written to Nooks, Gmail or Salesforce. Reasons:", file=sys.stderr)
        for r in e.reasons:
            print(f"  - {r}", file=sys.stderr)
        return 2
    text = callsheet.render_sheet_text(sheet)
    md = callsheet.render_sheet_markdown(sheet)
    counts = bucket_counts(sheet)
    counts_text = "Bucket counts: " + ", ".join(f"{k}={v}" for k, v in counts.items())
    print(text)
    print()
    print(md)
    print()
    print(counts_text)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "dry_run_sheet.txt").write_text(text + "\n\n" + counts_text + "\n", encoding="utf-8")
    (out / "dry_run_sheet.md").write_text(md + "\n\n" + counts_text + "\n", encoding="utf-8")
    print(f"wrote {out / 'dry_run_sheet.txt'} and {out / 'dry_run_sheet.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
