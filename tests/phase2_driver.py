"""Drivers for the phase 2 determinism and scale tests (task 2f). Offline, synthetic.

    python -m tests.phase2_driver shuffle --seeds 20            # prints one digest per shuffle, as JSON
    python -m tests.phase2_driver scale --n 12000               # prints runtime and peak RSS, as JSON

Run as a module from the repo root (the tests run it in a subprocess with PYTHONHASHSEED set)."""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import resource
import sys
import tempfile
import time
from pathlib import Path

from src import callsheet as cs
from src.config import load_config
from src.pipeline import run_pipeline
from tests.intake_helpers import write_raw
from tests.phase2_world import LONG_EA_TITLE, phase2_world
from tests.test_integration_pipeline import NOW, Builder

ROOT = Path(__file__).resolve().parent.parent
SHUFFLED = ("sf_rows", "contacts", "nooks_prospects", "calls", "scoops", "nooks_accounts", "sent_threads")


def run_world(world, tmp: Path, cfg, limit: int | None = 12):
    world.zoominfo_rows = []
    raw = write_raw(world, tmp / "raw", NOW, owner=cfg.owner.nooks_user_id)
    res = run_pipeline(raw, cfg, tmp / "state", NOW, log_dir=tmp / "logs")
    sheet = cs.build_sheet(res.rank_result, res.accounts_result.accounts, res.signals, res.prospects, cfg, NOW,
                           limit=limit)
    return res, sheet


def digest(res, sheet) -> str:
    h = hashlib.sha256()
    for part in (cs.render_sheet_text(sheet), cs.render_sheet_markdown(sheet), cs.sheet_to_json(sheet),
                 json.dumps([ln.packet.to_dict() for ln in sheet.lines], sort_keys=True),
                 "\n".join(res.log_lines)):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def shuffled_digest(seed: int | None, cfg) -> str:
    w = phase2_world()
    if seed is not None:
        rng = random.Random(seed)
        for name in SHUFFLED:
            rng.shuffle(getattr(w, name))
    with tempfile.TemporaryDirectory() as t:
        res, sheet = run_world(w, Path(t), cfg, limit=None)
        return digest(res, sheet)


def cmd_shuffle(seeds: int) -> dict:
    cfg = load_config(ROOT / "config" / "config.yaml")
    return {"unshuffled": shuffled_digest(None, cfg), "shuffled": [shuffled_digest(s, cfg) for s in range(1, seeds + 1)]}


# ---------------------------------------------------------------- scale
TITLES = ["Head of Product", "VP Product", "Product Manager", "Director of Engineering", "CIO", "Receptionist",
          "Head of Digital", "Software Engineer", "Executive Assistant", "Assistant Director of Product",
          LONG_EA_TITLE, "Head of Customer Experience", "Product Owner", "Engineering Manager"]
STATES = ["New South Wales", "Victoria", "Queensland", "Western Australia", "South Australia", "Tasmania"]
_W1 = ["Alder", "Birch", "Cedar", "Dune", "Ember", "Fjord", "Glade", "Harbor", "Iris", "Jasper", "Kestrel", "Lumen",
       "Maple", "Nimbus", "Onyx", "Pebble", "Quarry", "Ridge", "Sable", "Thistle", "Umber", "Vale", "Willow", "Yarrow"]
_W2 = ["Works", "Labs", "Mobile", "Cloud", "Data", "Apps", "Pay", "Link", "Grid", "Forge", "Loop", "Nest", "Peak",
       "Wave", "Spark", "Stack", "Trail", "Vista"]


def company_name(i: int) -> str:
    """Realistic spread: two invented words (buckets of ~30 accounts share one) plus a numeric token."""
    return f"{_W1[i % len(_W1)]} {_W2[(i // len(_W1)) % len(_W2)]} {i:05d} Pty Ltd"


SCOOPS = [("Executive Move", "{co} has appointed a new Head of Product to lead its digital product team.", ("Product Management",)),
          ("Funding", "{co} raised $12 million in Series B funding.", ("Funding",)),
          ("Funding", "{co} secured a $300 million debt facility.", ("Funding",)),
          ("Product Launch", "{co} launched a new mobile banking app for customers.", ("Product Launch",)),
          ("Product Launch", "{co} launched a new property fund.", ("Product Launch",)),
          ("Mergers & Acquisitions (M&A)", "{co} to be acquired by a private equity firm.", ("M&A",)),
          ("Hiring Plans", "{co} has more open roles in Medical & Health.", ("Customer Experience",))]


def scale_world(n: int, seed: int = 7):
    rng = random.Random(seed)
    b = Builder()
    for i in range(1, n + 1):
        b.account(i, company_name(i), state=rng.choice(STATES), emp=rng.choice([40, 90, 150, 300, 800, 1900]),
                  target=(i % 17 == 0))
        b.w.sf_rows[-1]["Website"] = f"www.scaleco{i}.com.au"
        for j in range(1, 3 if i % 3 else 2):
            b.prospect(i, j, rng.choice(TITLES))
        if i % 6 == 0:
            typ, text, topics = rng.choice(SCOOPS)
            b.scoop(i, typ, text.format(co=company_name(i)), rng.randint(1, 40), topics=topics)
    prospects = [p for p in b.w.nooks_prospects]
    for k in range(min(2500, len(prospects))):
        pr = rng.choice(prospects)
        phone = next((c["MobilePhone"] for c in b.w.contacts if c["Id"] == pr["crmId"]), None) if k < 0 else None
        b.call({"id": pr["id"], "phone": b.phone()}, rng.choice(["No Answer", "Left Voicemail", "Gatekeeper"]),
               rng.choice([1, 2, 3, 20, 60, 200]), duration=rng.choice([10, 30, 90]))
    return b.w


def cmd_scale(n: int) -> dict:
    cfg = load_config(ROOT / "config" / "config.yaml")
    t0 = time.perf_counter()
    w = scale_world(n)
    t_build = time.perf_counter() - t0
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        t1 = time.perf_counter()
        w.zoominfo_rows = []
        raw = write_raw(w, tmp / "raw", NOW, owner=cfg.owner.nooks_user_id)
        t_write = time.perf_counter() - t1
        t2 = time.perf_counter()
        res = run_pipeline(raw, cfg, tmp / "state", NOW, log_dir=tmp / "logs")
        t_pipe = time.perf_counter() - t2
        t3 = time.perf_counter()
        sheet = cs.build_sheet(res.rank_result, res.accounts_result.accounts, res.signals, res.prospects, cfg, NOW,
                               limit=10)
        text = cs.render_sheet_text(sheet)
        t_sheet = time.perf_counter() - t3
    return {"accounts": n, "sf_contacts": len(w.contacts), "calls": len(w.calls), "scoops": len(w.scoops),
            "seconds_world_build": round(t_build, 2), "seconds_write_raw": round(t_write, 2),
            "seconds_pipeline": round(t_pipe, 2), "seconds_sheet": round(t_sheet, 2),
            "seconds_total": round(time.perf_counter() - t0, 2),
            "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
            "ranked": len(res.rank_result.ranked), "needs_prospect": len(res.rank_result.needs_prospect),
            "on_sheet": len(sheet.lines), "sheet_chars": len(text),
            "duplicate_groups": res.stats["possible_duplicate_groups"],
            "junk_title_needs_prospect": res.stats["needs_prospect_junk_title"], "stats_ranked": res.stats["ranked"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("shuffle")
    s.add_argument("--seeds", type=int, default=20)
    c = sub.add_parser("scale")
    c.add_argument("--n", type=int, default=12000)
    a = ap.parse_args(argv)
    out = cmd_shuffle(a.seeds) if a.cmd == "shuffle" else cmd_scale(a.n)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
