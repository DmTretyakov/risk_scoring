"""Drift monitor and Prometheus metrics."""
import random

from app import monitoring


def test_psi_is_zero_for_identical_and_large_for_shifted_distributions():
    same = {"a": 0.5, "b": 0.5}
    assert monitoring.psi(same, same, 200, 200) == 0
    assert monitoring.psi(same, {"a": 0.1, "b": 0.9}, 200, 200) > monitoring.ALERT


def test_a_rare_category_missing_from_the_window_is_not_an_alert():
    reference = {"decided": 0.85, "needs_clarification": 0.13, "needs_review": 0.02}
    assert monitoring.psi(reference, {"decided": 0.87, "needs_clarification": 0.13}, 180, 120) < monitoring.WATCH


def test_missing_values_are_a_bin_of_their_own():
    row = {"payload": {"annual_revenue": "1000000", "employees": None, "requested_amount": "-5", "sector": "x"},
           "source_system": "s", "status": "needs_clarification", "risk_band": None}
    seen = monitoring.observe(row)
    assert seen["employees"] is None and seen["requested_amount"] is None   # missing and invalid
    assert seen["sector"] == "unmapped" and seen["risk_band"] == "not_scored"
    assert monitoring._bin("employees", None, [1.0]) == monitoring.MISSING


def test_drift_is_detected_after_a_feed_degrades(client, db, csv_rows, replay, simulate):
    for row in csv_rows:
        client.post("/v1/applications", json=replay.row_to_payload(row))
    monitoring.freeze_reference(db)

    # Too few companies in the window: no number, no false alarm.
    rng = random.Random(7)
    for i in range(10):
        client.post("/v1/applications", json=simulate.synthetic(rng.choice(csv_rows), "normal", rng, "t", i))
    small = {r["feature"]: r for r in monitoring.run_once(db, window_minutes=60, min_companies=50)}
    assert {r["status"] for r in small.values()} == {"insufficient_data"}

    for i in range(10, 120):
        client.post("/v1/applications", json=simulate.synthetic(rng.choice(csv_rows), "degraded", rng, "t", i))
    result = {r["feature"]: r for r in monitoring.run_once(db, window_minutes=60, min_companies=50)}
    for feature in ("annual_revenue", "employees", "source_system", "decision_status"):
        assert result[feature]["status"] == "alert", (feature, result[feature])
    stored = db.execute("SELECT count(DISTINCT run_at) AS runs, count(*) AS rows FROM drift_runs").fetchone()
    assert stored["runs"] == 2 and stored["rows"] == 2 * len(result)


def test_normal_traffic_does_not_alert(client, db, csv_rows, replay, simulate):
    for row in csv_rows:
        client.post("/v1/applications", json=replay.row_to_payload(row))
    monitoring.freeze_reference(db)
    rng = random.Random(11)
    for i in range(180):
        client.post("/v1/applications", json=simulate.synthetic(rng.choice(csv_rows), "normal", rng, "n", i))
    result = monitoring.run_once(db, window_minutes=60, min_companies=50)
    assert not [r for r in result if r["status"] == "alert"], result


def test_metrics_endpoint_counts_decisions(client):
    client.post("/v1/applications", json={"application_id": "M-1", "source_system": "legacy_crm",
                                          "tax_id": "B12345678", "employees": 10, "annual_revenue": "1000000",
                                          "requested_amount": "100000"})
    text = client.get("/metrics").text
    assert 'decision_requests_total{http_status="201",source_system="legacy_crm"}' in text
    assert 'decisions_total{source_system="legacy_crm",status="scored"}' in text
    assert 'risk_bands_total{model_version="v2",risk_band="low_risk",role="shadow"}' in text
    assert "decision_request_seconds_bucket" in text
    # Series used by alerts exist before the first failure, so rate() sees the very first errors.
    assert 'scorer_errors_total{model_version="v2",role="canary"}' in text
    assert 'decision_requests_total{http_status="503",source_system="legacy_crm"}' in text
    assert 'risk_bands_total{model_version="v2",risk_band="high_risk",role="primary"}' in text
    assert 'decisions_total{source_system="new_partner_feed",status="needs_clarification"}' in text
