from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import rules

MODELS = Path(__file__).resolve().parents[1] / "models"


def features(rpe: str, ratio: str, version: str = "features-v1") -> dict:
    return {"feature_version": version, "revenue_per_employee": rpe, "requested_to_revenue": ratio}


@pytest.fixture(scope="module")
def v1():
    return rules.load_artifact(MODELS, "v1")[0]


@pytest.fixture(scope="module")
def v2():
    return rules.load_artifact(MODELS, "v2")[0]


@pytest.mark.parametrize("rpe, ratio, band", [
    ("100000", "0.20", "low_risk"),         # exactly 20%: prototype uses '>', so not risky
    ("100000", "0.2000001", "high_risk"),
    ("40000", "0.1", "low_risk"),           # exactly 40,000 per employee: '<' is not met
    ("39999.99", "0.1", "high_risk"),
])
def test_v1_boundaries(v1, rpe, ratio, band):
    assert rules.score(features(rpe, ratio), v1)[0] == band


def test_v2_only_moves_the_ratio_ceiling(v1, v2):
    case = features("100000", "0.15")
    assert rules.score(case, v1)[0] == "low_risk"
    assert rules.score(case, v2) == ("high_risk", ["high_requested_to_revenue"])


def test_every_fired_rule_is_a_reason(v1):
    assert rules.score(features("1000", "0.5"), v1)[1] == ["low_revenue_per_employee", "high_requested_to_revenue"]


def test_wrong_feature_version_is_refused(v1):
    with pytest.raises(rules.FeatureMismatch):
        rules.score(features("100000", "0.1", version="features-v0"), v1)


def test_http_contract(monkeypatch):
    monkeypatch.setenv("MODEL_VERSION", "v2")
    monkeypatch.setenv("MODELS_DIR", str(MODELS))
    from app.main import app
    with TestClient(app) as client:
        health = client.get("/health").json()
        assert health["model_version"] == "v2" and len(health["artifact_sha256"]) == 64
        body = client.post("/score", json={"features": features("100000", "0.15")}).json()
        assert body["risk_band"] == "high_risk" and body["artifact_sha256"] == health["artifact_sha256"]
        assert client.post("/score", json={"features": {"feature_version": "features-v1"}}).status_code == 422
