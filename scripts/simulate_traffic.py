"""Synthetic traffic for the monitoring demo. Standard library only.

    python3 scripts/simulate_traffic.py --scenario normal   --n 200
    python3 scripts/simulate_traffic.py --scenario degraded --n 400

Every row is resampled from the supplied CSV and clearly synthetic:
application IDs start with SIM-, tax IDs with Z (never real companies).
  normal:   same mix as the export, amounts jittered by about 10%
  degraded: one feed breaks: new_partner_feed only, 40% missing headcount,
            revenue reported ~3x lower (e.g. thousands vs units), construction-heavy
"""
import argparse
import collections
import csv
import random
import time
from pathlib import Path

from replay_csv import post, row_to_payload

CSV = Path(__file__).resolve().parents[1] / "data" / "raw_applications.csv"


def synthetic(row: dict, scenario: str, rng: random.Random, run_id: str, i: int) -> dict:
    p = row_to_payload(row)
    p["application_id"] = f"SIM-{run_id}-{i:05d}"
    p["tax_id"] = f"Z{rng.randrange(10**8):08d}"
    p["company_name"] = f"Synthetic {i}"
    p["notes"] = None
    for field in ("annual_revenue", "requested_amount", "annual_ebitda"):
        if p[field] is not None:
            p[field] = f"{float(p[field]) * rng.lognormvariate(0, 0.1):.2f}"
    if scenario == "degraded":
        p["source_system"] = "new_partner_feed"
        if p["annual_revenue"] is not None:
            p["annual_revenue"] = f"{float(p['annual_revenue']) / 3:.2f}"
        if rng.random() < 0.4:
            p["employees"] = None
        if rng.random() < 0.6:
            p["sector"] = "Construction"
    return p


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", choices=["normal", "degraded"], default="normal")
    parser.add_argument("--n", type=int, default=200)
    parser.add_argument("--url", default="http://127.0.0.1:8010")
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    with open(CSV, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    run_id = f"{args.scenario[:3]}{int(time.time())}"
    status, waits = collections.Counter(), collections.Counter()
    for i in range(args.n):
        code, body = post(args.url, synthetic(rng.choice(rows), args.scenario, rng, run_id, i), waits)
        status[body.get("status", code)] += 1
    print(f"{args.scenario}: {args.n} synthetic applications -> {dict(status)}  backoffs {dict(waits)}")


if __name__ == "__main__":
    main()
