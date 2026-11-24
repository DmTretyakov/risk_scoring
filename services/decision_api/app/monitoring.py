"""Batch drift monitor: compares recent applications with a frozen reference.

    python -m app.monitoring freeze-reference     # freeze the current population as the reference
    python -m app.monitoring run --every 30       # loop; also exposes Prometheus gauges on :9100

Batch, not in the request path: drift is a property of a population, and computing it
must never slow down or break decisions.

One row per company per window (its latest application), so a company re-sending many
times cannot fake a shift. Inputs come from the raw payload, so blocked applications count
too, and missing values are a bin of their own: a feed that starts dropping a field shows
up as drift. Outputs: decision status mix and risk band.

PSI over reference-decile bins with Laplace smoothing (+0.5 per bin), so a rare category
absent from a small window does not explode; 0.1 / 0.25 are the usual watch / act levels.
Sampling noise alone gives about (bins - 1) * (1/n_ref + 1/n_cur), ~0.1 for 100 vs 180
companies, hence MIN_COMPANIES. Target drift needs outcomes: see the loan_vintages view.
"""
import argparse
import bisect
import json
import logging
import math
import os
import statistics
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from prometheus_client import Gauge, start_http_server

from . import normalize

log = logging.getLogger("monitor")

MISSING = "missing"
NUMERIC = ("annual_revenue", "employees", "requested_amount", "revenue_per_employee", "requested_to_revenue")
CATEGORICAL = ("sector", "region", "source_system", "decision_status", "risk_band")
WATCH, ALERT = 0.10, 0.25
PSEUDO_COUNT = 0.5   # Laplace smoothing per bin

PSI = Gauge("drift_psi", "Population stability index vs the frozen reference (NaN = insufficient data)", ["feature"])
COMPANIES = Gauge("drift_window_companies", "Companies in the current monitoring window")
LAST_RUN = Gauge("monitor_last_run_timestamp_seconds", "Unix time of the last successful monitor run")
HAS_REFERENCE = Gauge("drift_reference_present", "1 if a reference population is frozen")

_POPULATION = """
SELECT DISTINCT ON (COALESCE(company_id, id))
       source_system, status, payload, response -> 'decision' ->> 'risk_band' AS risk_band
FROM applications
WHERE received_at > %s AND received_at <= %s
ORDER BY COALESCE(company_id, id), received_at DESC
"""


def _positive(value) -> float | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return float(number) if number.is_finite() and number > 0 else None


def observe(row: dict) -> dict:
    """Monitored values of one application, computed from what was sent."""
    p = row["payload"]
    revenue, employees, requested = (_positive(p.get(k)) for k in ("annual_revenue", "employees", "requested_amount"))
    log10 = lambda x: math.log10(x) if x else None  # noqa: E731 - money and headcount are log-normal-ish
    return {
        "annual_revenue": log10(revenue),
        "employees": log10(employees),
        "requested_amount": log10(requested),
        "revenue_per_employee": log10(revenue / employees) if revenue and employees else None,
        "requested_to_revenue": requested / revenue if requested and revenue else None,
        "sector": normalize.sector(p.get("sector")) or ("unmapped" if p.get("sector") else MISSING),
        "region": normalize.region(p.get("region")) or ("unmapped" if p.get("region") else MISSING),
        "source_system": row["source_system"],
        # 'duplicate' carries a decision like 'scored'; resubmission behaviour is not data drift.
        "decision_status": "decided" if row["status"] in ("scored", "duplicate") else row["status"],
        "risk_band": row["risk_band"] or "not_scored",
    }


def _bin(feature: str, value, edges: list[float] | None) -> str:
    if value is None:
        return MISSING
    return f"b{bisect.bisect_right(edges, value)}" if feature in NUMERIC else str(value)


def _shares(labels: list[str]) -> dict[str, float]:
    counts = Counter(labels)
    return {label: n / len(labels) for label, n in counts.items()}


def build_spec(observations: list[dict]) -> dict:
    spec = {}
    for feature in NUMERIC + CATEGORICAL:
        edges = None
        if feature in NUMERIC:
            values = sorted(o[feature] for o in observations if o[feature] is not None)
            edges = sorted(set(statistics.quantiles(values, n=10))) if len(values) >= 10 else []
        spec[feature] = {"edges": edges, "shares": _shares([_bin(feature, o[feature], edges) for o in observations])}
    return spec


def psi(reference: dict[str, float], current: dict[str, float], n_reference: int, n_current: int) -> float:
    labels = set(reference) | set(current)
    smooth = lambda share, n: (share * n + PSEUDO_COUNT) / (n + PSEUDO_COUNT * len(labels))  # noqa: E731
    total = 0.0
    for label in labels:
        r, c = smooth(reference.get(label, 0.0), n_reference), smooth(current.get(label, 0.0), n_current)
        total += (c - r) * math.log(c / r)
    return total


def status_of(value: float | None) -> str:
    if value is None:
        return "insufficient_data"
    return "alert" if value >= ALERT else "watch" if value >= WATCH else "ok"


def _population(conn, start: datetime, end: datetime) -> list[dict]:
    return [observe(r) for r in conn.execute(_POPULATION, (start, end)).fetchall()]


def freeze_reference(conn, start: datetime | None = None, end: datetime | None = None) -> int:
    end = end or datetime.now(timezone.utc)
    start = start or datetime(1970, 1, 1, tzinfo=timezone.utc)
    observations = _population(conn, start, end)
    if not observations:
        raise SystemExit("no applications to build a reference from")
    row = conn.execute(
        "INSERT INTO drift_reference (window_start, window_end, companies, spec) VALUES (%s, %s, %s, %s) RETURNING id",
        (start, end, len(observations), Jsonb(build_spec(observations))),
    ).fetchone()
    log.info(json.dumps({"event": "reference_frozen", "reference_id": row["id"], "companies": len(observations)}))
    return row["id"]


def run_once(conn, window_minutes: int, min_companies: int, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    reference = conn.execute("SELECT * FROM drift_reference ORDER BY id DESC LIMIT 1").fetchone()
    HAS_REFERENCE.set(1 if reference else 0)
    if reference is None:
        log.warning(json.dumps({"event": "no_reference", "hint": "run: python -m app.monitoring freeze-reference"}))
        LAST_RUN.set(now.timestamp())
        return []

    # The window never overlaps the reference period.
    start = max(reference["window_end"], now - timedelta(minutes=window_minutes))
    observations = _population(conn, start, now)
    enough = len(observations) >= min_companies
    results = []
    with conn.transaction():
        for feature, ref in reference["spec"].items():
            value = None
            if enough:
                current = _shares([_bin(feature, o[feature], ref["edges"]) for o in observations])
                value = psi(ref["shares"], current, reference["companies"], len(observations))
            results.append({"feature": feature, "psi": value, "status": status_of(value)})
            conn.execute(
                """INSERT INTO drift_runs (run_at, reference_id, window_start, window_end, companies, feature, psi, status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (now, reference["id"], start, now, len(observations), feature, value, status_of(value)),
            )
            PSI.labels(feature).set(value if value is not None else math.nan)
    COMPANIES.set(len(observations))
    LAST_RUN.set(now.timestamp())
    log.info(json.dumps({"event": "drift_run", "companies": len(observations),
                         "alerts": [r["feature"] for r in results if r["status"] == "alert"]}))
    return results


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Drift monitor")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("freeze-reference")
    run = sub.add_parser("run")
    run.add_argument("--every", type=int, default=0, help="seconds between runs; 0 = run once")
    args = parser.parse_args()

    window = int(os.environ.get("WINDOW_MINUTES", "60"))
    min_companies = int(os.environ.get("MIN_COMPANIES", "100"))
    connect = lambda: psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, row_factory=dict_row)  # noqa: E731

    if args.command == "freeze-reference":
        with connect() as conn:
            freeze_reference(conn)
        return
    if not args.every:
        with connect() as conn:
            run_once(conn, window, min_companies)
        return
    start_http_server(int(os.environ.get("METRICS_PORT", "9100")))
    while True:
        try:
            with connect() as conn:   # fresh connection per run: survives database restarts
                run_once(conn, window, min_companies)
        except Exception:   # keep looping; staleness is alerted via monitor_last_run_timestamp_seconds
            log.exception("drift run failed")
        time.sleep(args.every)


if __name__ == "__main__":
    main()
