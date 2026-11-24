"""One scorer process serves exactly one pinned model version (MODEL_VERSION).

Two versions = two containers of the same image. Rolling a version out or back
is a routing change in decision-api; no scorer is modified in place.
"""
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel

from . import rules


SCORES = Counter("scorer_results_total", "Results served", ["model_version", "risk_band"])
SECONDS = Histogram("scorer_seconds", "Time spent scoring", ["model_version"],
                    buckets=(0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05))
INFO = Gauge("scorer_info", "Artifact served by this process (value is always 1)",
             ["model_version", "artifact_sha256", "feature_version"])


class ScoreRequest(BaseModel):
    features: dict


class ScoreResponse(BaseModel):
    model_version: str
    artifact_sha256: str
    risk_band: str
    reasons: list[str]


@asynccontextmanager
async def lifespan(app: FastAPI):
    version = os.environ["MODEL_VERSION"]
    models_dir = Path(os.environ.get("MODELS_DIR", "/srv/models"))
    app.state.artifact, app.state.sha256 = rules.load_artifact(models_dir, version)  # fail fast on a bad artifact
    INFO.labels(version, app.state.sha256, app.state.artifact["feature_version"]).set(1)
    yield


app = FastAPI(title="scorer", lifespan=lifespan)


@app.post("/score", response_model=ScoreResponse)
def score(body: ScoreRequest, request: Request):
    artifact = request.app.state.artifact
    started = time.perf_counter()
    try:
        risk_band, reasons = rules.score(body.features, artifact)
    except rules.FeatureMismatch as error:
        raise HTTPException(422, str(error))
    SECONDS.labels(artifact["model_version"]).observe(time.perf_counter() - started)
    SCORES.labels(artifact["model_version"], risk_band).inc()
    return ScoreResponse(model_version=artifact["model_version"], artifact_sha256=request.app.state.sha256,
                         risk_band=risk_band, reasons=reasons)


@app.get("/health")
def health(request: Request):
    artifact = request.app.state.artifact
    return {"status": "ok", "model_version": artifact["model_version"], "artifact_sha256": request.app.state.sha256,
            "feature_version": artifact["feature_version"]}


@app.get("/metrics", include_in_schema=False)
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
