"""Request workflow:

  retry cache (Redis) -> idempotency / correction (Postgres) -> gate -> route -> duplicate check
  -> answering scorer (HTTP) -> persist (one transaction) -> respond -> shadow scorer

The response is returned only after the commit: a decision we cannot explain
later is never sent. No transaction is held open during the scorer call.
"""
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from psycopg import errors
from psycopg_pool import ConnectionPool

from . import gate, repository, router
from . import metrics as m
from .cache import RedisLayer
from .config import Settings
from .schemas import ApplicationIn, ApplicationOut, Decision
from .scorers import ScorerClient, ScorerError

log = logging.getLogger("decision-api")

# Statuses the sender can fix by re-sending the same application_id with new data.
CORRECTABLE = {"needs_clarification", "needs_review"}


class IdempotencyConflict(Exception):
    """application_id already decided (scored/duplicate) and re-sent with different content."""


class ScoringUnavailable(Exception):
    """No model could answer. Nothing is stored, so the sender can safely retry."""


@dataclass
class Deps:
    pool: ConnectionPool
    settings: Settings
    redis: RedisLayer
    scorers: ScorerClient


@dataclass
class ShadowJob:
    application_id: uuid.UUID
    model_version: str
    features: dict


@dataclass
class Outcome:
    response: dict
    replayed: bool
    shadow: ShadowJob | None = None


def _sha256(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def submit(deps: Deps, app: ApplicationIn) -> Outcome:
    payload = app.model_dump(mode="json")
    request_hash = _sha256(payload)
    source, external_id = app.source_system, app.application_id

    # 1. Exact retry of the latest revision: answer from Redis without touching Postgres.
    cached = deps.redis.get_retry(source, external_id)
    if cached and cached["request_hash"] == request_hash:
        m.RETRY_CACHE.labels("redis_hit").inc()
        return Outcome(cached["response"], replayed=True)

    with deps.pool.connection() as conn:
        # 2. Postgres is authoritative: any earlier revision with identical content is a retry.
        revisions = repository.find_revisions(conn, source, external_id)
        same = next((r for r in revisions if r["request_hash"] == request_hash), None)
        if same:
            m.RETRY_CACHE.labels("db_hit").inc()
            if same is revisions[0]:
                deps.redis.put_retry(source, external_id, request_hash, same["response"])
            return Outcome(same["response"], replayed=True)

        # 3. Different content under a known application_id: a correction, allowed only
        #    while we have not decided. A decided application needs a new application_id.
        m.RETRY_CACHE.labels("miss").inc()
        previous = revisions[0] if revisions else None
        if previous and previous["status"] not in CORRECTABLE:
            raise IdempotencyConflict(previous["status"])

        try:
            outcome = _evaluate(conn, deps, app, payload, request_hash, previous)
        except errors.UniqueViolation:
            # A concurrent request with the same application_id committed first.
            revisions = repository.find_revisions(conn, source, external_id)
            same = next((r for r in revisions if r["request_hash"] == request_hash), None)
            if same:
                return Outcome(same["response"], replayed=True)
            raise IdempotencyConflict("concurrent change")

    deps.redis.put_retry(source, external_id, request_hash, outcome.response)
    _count(outcome.response)
    return outcome


def _count(response: dict) -> None:
    """Metrics for a newly created application (retries are not counted twice)."""
    src = m.source(response["source_system"])
    m.DECISIONS.labels(src, response["status"]).inc()
    for issue in response["issues"]:
        m.GATE_ISSUES.labels(src, issue["field"], issue["code"], issue["severity"]).inc()
    if response["status"] == "scored":
        decision = response["decision"]
        m.RISK_BANDS.labels(decision["model_version"], "primary", decision["risk_band"]).inc()


def _evaluate(conn, deps: Deps, app: ApplicationIn, payload: dict, request_hash: str,
              previous: dict | None) -> Outcome:
    settings = deps.settings
    application_id = uuid.uuid4()
    received_at = datetime.now(timezone.utc)
    result = gate.check(app, today=received_at.date())   # always re-run: corrections get a fresh check

    company_id = None
    if result.tax_id:
        company_id = repository.upsert_company(conn, app.country_code, result.tax_id, app.company_name)

    features_hash = _sha256(result.features) if result.features else None
    decision, duplicate_of, primary, shadow, canary_error = None, None, None, None, None

    if result.blocked:
        status = "needs_clarification"
    elif result.needs_review:
        status = "needs_review"
    else:
        route = router.route(company_id, settings.primary_model, settings.candidate_model, settings.canary_percent)
        earlier = repository.find_duplicate(conn, company_id, features_hash, route.answer)
        if earlier:
            status, duplicate_of = "duplicate", earlier["id"]
            decision = Decision(**earlier["decision"])
        else:
            primary, canary_error = _score(deps.scorers, route, result.features)
            status = "scored"
            decision = Decision(risk_band=primary["risk_band"], model_version=primary["model_version"],
                                reasons=primary["reasons"])
            if route.shadow and not canary_error:
                shadow = ShadowJob(application_id, route.shadow, result.features)

    revision = previous["revision"] + 1 if previous else 1
    supersedes = previous["id"] if previous else None
    response = ApplicationOut(
        application_id=application_id, external_id=app.application_id, source_system=app.source_system,
        revision=revision, supersedes=supersedes, status=status, decision=decision, issues=result.issues,
        duplicate_of=duplicate_of, gate_version=gate.GATE_VERSION, received_at=received_at,
    ).model_dump(mode="json")

    with conn.transaction():
        repository.insert_application(
            conn, id=application_id, source_system=app.source_system, external_id=app.application_id,
            revision=revision, supersedes=supersedes, request_hash=request_hash, company_id=company_id,
            received_at=received_at, payload=payload, features=result.features, features_hash=features_hash,
            gate_version=gate.GATE_VERSION, status=status, issues=response["issues"],
            duplicate_of=duplicate_of, response=response,
        )
        if primary:
            repository.insert_evaluation(
                conn, application_id=application_id, role="primary", model_version=primary["model_version"],
                artifact_sha256=primary["artifact_sha256"], risk_band=primary["risk_band"],
                reasons=primary["reasons"], error=None, latency_ms=primary["latency_ms"],
            )
        if canary_error:   # the canary's slot is its shadow result: failed, stable version answered
            repository.insert_evaluation(
                conn, application_id=application_id, role="shadow", model_version=route.answer,
                artifact_sha256=None, risk_band=None, reasons=[], error=canary_error, latency_ms=None,
            )
    return Outcome(response, replayed=False, shadow=shadow)


def _score(scorers: ScorerClient, route: router.Route, features: dict) -> tuple[dict, str | None]:
    """The answering model's result, plus the canary's error if the stable version had to answer.
    Never the other way round: an unproven candidate does not stand in for a failing stable model."""
    try:
        return scorers.score(route.answer, features, role="canary" if route.fallback else "primary"), None
    except ScorerError as error:
        if not route.fallback:
            raise ScoringUnavailable(str(error)) from error
        canary_error = str(error)[:500]
        log.warning(json.dumps({"event": "canary_fallback", "candidate": route.answer, "error": canary_error}))
    try:
        return scorers.score(route.fallback, features), canary_error
    except ScorerError as error:
        raise ScoringUnavailable(str(error)) from error


def run_shadow(deps: Deps, job: ShadowJob, primary_version: str, primary_band: str) -> None:
    """Runs after the response is sent; its success or failure never changes what the sender got.
    In production this would be a queue consumer (SQS) instead of an in-process background task."""
    try:
        result = deps.scorers.score(job.model_version, job.features, role="shadow")
        row = dict(artifact_sha256=result["artifact_sha256"], risk_band=result["risk_band"],
                   reasons=result["reasons"], error=None, latency_ms=result["latency_ms"])
    except ScorerError as error:
        row = dict(artifact_sha256=None, risk_band=None, reasons=[], error=str(error)[:500], latency_ms=None)
    with deps.pool.connection() as conn:
        repository.insert_evaluation(conn, application_id=job.application_id, role="shadow",
                                     model_version=job.model_version, **row)
    if row["risk_band"]:
        m.RISK_BANDS.labels(job.model_version, "shadow", row["risk_band"]).inc()
        m.SHADOW.labels(primary_version, job.model_version, str(row["risk_band"] == primary_band).lower()).inc()
    log.info(json.dumps({"event": "shadow_evaluated", "application_id": str(job.application_id),
                         "shadow_version": job.model_version, "shadow_band": row["risk_band"],
                         "primary_band": primary_band, "agree": row["risk_band"] == primary_band,
                         "error": row["error"]}))
