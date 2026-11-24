import json
import logging
import math
import time
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import metrics as m
from . import normalize, repository, service
from .cache import RedisLayer
from .config import load_settings
from .schemas import ApplicationIn, ApplicationOut, HistoryItem
from .scorers import ScorerClient

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("decision-api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    m.init_alerting_series(list(settings.scorer_urls))
    pool = ConnectionPool(settings.database_url, min_size=1, max_size=10, open=False,
                          # autocommit: reads are single statements; writes use explicit conn.transaction()
                          kwargs={"row_factory": dict_row, "autocommit": True})
    pool.open(wait=True, timeout=30)
    deps = service.Deps(
        pool=pool, settings=settings,
        redis=RedisLayer(settings.redis_url, settings.rate_limit_capacity, settings.rate_limit_refill_per_s,
                         settings.retry_cache_ttl_s),
        scorers=ScorerClient(settings.scorer_urls, settings.scorer_timeout_s),
    )
    app.state.deps = deps
    yield
    deps.scorers.close()
    deps.redis.close()
    pool.close()


app = FastAPI(title="Novicap decision API", version="0.3.0", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def count_invalid_requests(request: Request, error: RequestValidationError):
    """Malformed requests never reach the endpoint; count them so a broken sender is visible."""
    if request.url.path == "/v1/applications":
        m.REQUESTS.labels("unparsed", "422").inc()
    return await request_validation_exception_handler(request, error)


@app.post("/v1/applications", response_model=ApplicationOut, status_code=201, responses={
    200: {"description": "Retry of an already processed request: the original response"},
    409: {"description": "application_id already decided and re-sent with different content"},
    429: {"description": "Rate limit for this source exceeded; see Retry-After"},
    503: {"description": "Scoring unavailable; nothing stored, safe to retry"},
})
def submit_application(body: ApplicationIn, request: Request, response: Response, background: BackgroundTasks):
    deps: service.Deps = request.app.state.deps
    started = time.perf_counter()

    src = m.source(body.source_system)
    allowed, wait_s = deps.redis.allow(body.source_system)
    if not allowed:
        m.RATE_LIMITED.labels(src).inc()
        m.REQUESTS.labels(src, "429").inc()
        log.info(json.dumps({"event": "rate_limited", "source_system": body.source_system}))
        return JSONResponse({"detail": "rate limit exceeded"}, status_code=429,
                            headers={"Retry-After": str(max(1, math.ceil(wait_s)))})
    try:
        outcome = service.submit(deps, body)
    except service.IdempotencyConflict as error:
        m.REQUESTS.labels(src, "409").inc()
        raise HTTPException(409, f"application_id '{body.application_id}' from '{body.source_system}' "
                                 f"is already {error}; send a new application_id for new data")
    except service.ScoringUnavailable as error:
        m.REQUESTS.labels(src, "503").inc()
        log.error(json.dumps({"event": "scoring_unavailable", "application_id": body.application_id,
                              "error": str(error)}))
        raise HTTPException(503, "scoring temporarily unavailable; retry later", headers={"Retry-After": "5"})

    result = outcome.response
    response.status_code = 200 if outcome.replayed else 201   # set explicitly: the default is not visible here
    if outcome.replayed:
        response.headers["Idempotent-Replayed"] = "true"
    if outcome.shadow:
        background.add_task(service.run_shadow, deps, outcome.shadow, result["decision"]["model_version"],
                            result["decision"]["risk_band"])
    elapsed = time.perf_counter() - started
    m.REQUESTS.labels(src, str(response.status_code)).inc()
    m.LATENCY.observe(elapsed)
    # One structured line per decision: enough to reconstruct an incident from logs alone.
    log.info(json.dumps({
        "event": "application_decided", "application_id": result["application_id"],
        "external_id": body.application_id, "revision": result["revision"],
        "source_system": body.source_system, "status": result["status"], "replayed": outcome.replayed,
        "model_version": (result["decision"] or {}).get("model_version"),
        "risk_band": (result["decision"] or {}).get("risk_band"),
        "issue_codes": [i["code"] for i in result["issues"]],
        "gate_version": result["gate_version"],
        "latency_ms": round(elapsed * 1000, 2),
    }))
    return result


@app.get("/v1/applications/{application_id}", response_model=ApplicationOut)
def get_application(application_id: UUID, request: Request):
    with request.app.state.deps.pool.connection() as conn:
        stored = repository.get_response(conn, application_id)
    if stored is None:
        raise HTTPException(404, "application not found")
    return stored


@app.get("/v1/companies/{country_code}/{tax_id}/applications", response_model=list[HistoryItem])
def company_applications(country_code: str, tax_id: str, request: Request):
    normalized = normalize.tax_id(tax_id)
    if not normalized:
        raise HTTPException(404, "company not found")
    with request.app.state.deps.pool.connection() as conn:
        return repository.list_company_applications(conn, country_code.upper(), normalized)


@app.get("/health")
def health(request: Request):
    """Liveness: the process and its database work."""
    with request.app.state.deps.pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/ready")
def ready(request: Request, response: Response):
    """Readiness for a load balancer: can we make a decision right now?
    Redis and the candidate scorer are optional by design and only reported
    (a failing canary falls back to the stable version)."""
    deps: service.Deps = request.app.state.deps
    checks = {}
    try:
        with deps.pool.connection() as conn:
            conn.execute("SELECT 1")
        checks["postgres"] = "ok"
    except Exception as error:  # noqa: BLE001 - report, do not crash the probe
        checks["postgres"] = f"error: {type(error).__name__}"
    for role, version in (("primary", deps.settings.primary_model), ("candidate", deps.settings.candidate_model)):
        if version:
            checks[f"scorer_{role}_{version}"] = "ok" if deps.scorers.healthy(version) else "error"
    checks["redis"] = "ok" if deps.redis.healthy() else "error (optional)"
    is_ready = checks["postgres"] == "ok" and checks[f"scorer_primary_{deps.settings.primary_model}"] == "ok"
    response.status_code = 200 if is_ready else 503
    return {"ready": is_ready, "checks": checks}


@app.get("/metrics", include_in_schema=False)
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
