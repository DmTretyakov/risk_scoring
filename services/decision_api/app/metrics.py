"""Prometheus metrics of the online path (scraped from /metrics).

Labels stay low-cardinality: no tax IDs, application IDs or company names.
source_system is client-supplied here, so unknown values collapse to "other"
(in production it would come from the authenticated partner identity).
"""
import os

from prometheus_client import Counter, Histogram

KNOWN_SOURCES = set(filter(None, os.environ.get(
    "KNOWN_SOURCES", "legacy_crm,manual_entry,new_partner_feed").split(",")))


def source(name: str) -> str:
    return name if name in KNOWN_SOURCES else "other"


REQUESTS = Counter("decision_requests_total", "POST /v1/applications by HTTP status", ["source_system", "http_status"])
LATENCY = Histogram("decision_request_seconds", "End-to-end latency of POST /v1/applications",
                    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5))
DECISIONS = Counter("decisions_total", "New applications by outcome", ["source_system", "status"])
GATE_ISSUES = Counter("gate_issues_total", "Data problems found by the gate",
                      ["source_system", "field", "code", "severity"])
RISK_BANDS = Counter("risk_bands_total", "Model results", ["model_version", "role", "risk_band"])
SCORER_SECONDS = Histogram("scorer_call_seconds", "Scorer HTTP call latency", ["model_version", "role"],
                           buckets=(0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2))
SCORER_ERRORS = Counter("scorer_errors_total", "Failed scorer calls", ["model_version", "role"])
SHADOW = Counter("shadow_comparisons_total", "Primary vs shadow on the same features",
                 ["primary_version", "shadow_version", "agree"])
RETRY_CACHE = Counter("retry_cache_total", "Retry lookups", ["result"])   # redis_hit | db_hit | miss
RATE_LIMITED = Counter("rate_limited_total", "Requests rejected by the rate limiter", ["source_system"])
REDIS_ERRORS = Counter("redis_errors_total", "Redis failures (requests continue without Redis)", ["op"])


def init_alerting_series(model_versions: list[str]) -> None:
    """Create every series that alerts use at 0. A series that first appears with a value
    already above 0 has no increase for rate()/increase() to see, so the first burst of
    failures or high-risk results after a deploy would be missed."""
    for version in model_versions:
        for role in ("primary", "canary", "shadow"):
            SCORER_ERRORS.labels(version, role)
        for role in ("primary", "shadow"):
            for band in ("low_risk", "high_risk"):
                RISK_BANDS.labels(version, role, band)
        for other in model_versions:
            for agree in ("true", "false"):
                if other != version:
                    SHADOW.labels(version, other, agree)
    for name in KNOWN_SOURCES | {"other"}:
        REQUESTS.labels(name, "503")
        for status in ("scored", "duplicate", "needs_clarification", "needs_review"):
            DECISIONS.labels(name, status)
