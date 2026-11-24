"""End-to-end through HTTP against real Postgres, Redis and both scorer containers."""
import collections
from concurrent.futures import ThreadPoolExecutor

import pytest

VALID = dict(application_id="T-1", source_system="test", company_name="Acme S.L.", tax_id="B12345678",
             employees=10, annual_revenue="1000000", requested_amount="100000", sector="Retail")


def count(db, table: str, where: str = "true") -> int:
    return db.execute(f"SELECT count(*) AS n FROM {table} WHERE {where}").fetchone()["n"]


def post(client, **overrides):
    return client.post("/v1/applications", json={**VALID, **overrides})


# --- happy path, storage, shadow -------------------------------------------------

def test_health_and_readiness(client, make_client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json()["ready"] is True
    degraded = make_client(SCORER_URLS="v1=http://scorer-v1:9,v2=http://scorer-v2:8000")
    response = degraded.get("/ready")
    assert response.status_code == 503 and response.json()["checks"]["scorer_primary_v1"] == "error"


def test_valid_application_is_scored_by_primary_and_shadowed(client, db):
    response = post(client)
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "scored" and body["revision"] == 1 and body["supersedes"] is None
    assert body["decision"] == {"risk_band": "low_risk", "model_version": "v1", "reasons": ["within_all_thresholds"]}
    assert client.get(f"/v1/applications/{body['application_id']}").json() == body
    evaluations = {e["role"]: e for e in db.execute("SELECT * FROM evaluations").fetchall()}
    assert evaluations["primary"]["model_version"] == "v1" and len(evaluations["primary"]["artifact_sha256"]) == 64
    assert evaluations["shadow"]["model_version"] == "v2"  # scored after the response, same features


def test_shadow_disagreement_is_recorded_but_never_returned(client, db):
    body = post(client, requested_amount="150000").json()   # 15%: v1 low, v2-demo (12%) high
    assert body["decision"]["risk_band"] == "low_risk"
    row = db.execute("SELECT * FROM shadow_comparison").fetchone()
    assert (row["primary_band"], row["shadow_band"], row["agree"]) == ("low_risk", "high_risk", False)


# --- idempotency and corrections ------------------------------------------------

def test_retry_returns_the_stored_response_from_redis(client, db, redis_client):
    first = post(client)
    assert len(redis_client.keys("retry:*")) == 1
    retry = post(client)
    assert retry.status_code == 200 and retry.headers["Idempotent-Replayed"] == "true"
    assert retry.json() == first.json()
    assert count(db, "applications") == 1 and count(db, "evaluations", "role = 'primary'") == 1


def test_retry_still_works_when_redis_lost_its_data(client, redis_client):
    first = post(client).json()
    redis_client.flushdb()
    retry = post(client)
    assert retry.status_code == 200 and retry.json() == first


def test_correction_after_clarification_is_evaluated_on_the_new_data(client, db):
    asked = post(client, employees=None).json()
    assert asked["status"] == "needs_clarification"

    fixed = post(client, employees=10)
    body = fixed.json()
    assert fixed.status_code == 201 and body["status"] == "scored"
    assert body["revision"] == 2 and body["supersedes"] == asked["application_id"]

    # Retrying the correction replays it; re-sending the old payload replays the old answer.
    assert post(client, employees=10).json() == body
    assert post(client, employees=None).json() == asked
    # History of both revisions is kept; the first one is unchanged.
    assert client.get(f"/v1/applications/{asked['application_id']}").json() == asked
    assert count(db, "applications") == 2


def test_correction_after_review_is_allowed(client):
    flagged = post(client, annual_ebitda="5000000").json()     # EBITDA > revenue
    assert flagged["status"] == "needs_review"
    fixed = post(client, annual_ebitda="150000").json()
    assert fixed["status"] == "scored" and fixed["revision"] == 2


def test_decided_application_cannot_be_changed(client, db):
    post(client)
    response = post(client, requested_amount="900000")
    assert response.status_code == 409
    assert count(db, "applications") == 1


def test_concurrent_retries_create_one_application(client, db):
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(lambda _: post(client).status_code, range(8)))
    assert sorted(codes) == [200] * 7 + [201]
    assert count(db, "applications") == 1 and count(db, "evaluations", "role = 'primary'") == 1


# --- gate and company history ---------------------------------------------------

def test_bad_data_is_kept_but_never_scored(client, db):
    body = post(client, employees=-3).json()
    assert body["status"] == "needs_clarification" and body["decision"] is None
    assert body["issues"][0]["code"] == "invalid_negative_value"
    row = db.execute("SELECT payload, features FROM applications").fetchone()
    assert row["payload"]["employees"] == "-3" and row["features"] is None
    assert count(db, "evaluations") == 0


def test_type_errors_are_rejected_before_storage(client, db):
    assert post(client, employees="many").status_code == 422
    assert post(client, unexpected=1).status_code == 422
    assert count(db, "applications") == 0


def test_same_company_same_figures_reuses_the_decision(client, db):
    first = post(client).json()
    again = post(client, application_id="T-1-DUP", tax_id="b-12345678", sector="RETAIL",
                 notes="Duplicate submission?").json()
    assert again["status"] == "duplicate" and again["duplicate_of"] == first["application_id"]
    assert again["decision"] == first["decision"]
    third = post(client, application_id="T-1-DUP2").json()
    assert third["duplicate_of"] == first["application_id"]      # always the scored one, not a copy
    assert count(db, "companies") == 1 and count(db, "evaluations", "role = 'primary'") == 1


def test_same_company_new_figures_is_scored(client):
    post(client)
    assert post(client, application_id="T-2", requested_amount="150000").json()["status"] == "scored"


def test_company_history_is_found_by_any_tax_id_spelling(client):
    post(client)
    post(client, application_id="T-2", employees=None)
    history = client.get("/v1/companies/es/b-1234-5678/applications").json()
    assert [h["external_id"] for h in history] == ["T-2", "T-1"]
    assert client.get("/v1/companies/ES/X00000000/applications").json() == []


def test_unknown_application_is_404(client):
    assert client.get("/v1/applications/00000000-0000-4000-8000-000000000000").status_code == 404


# --- rollout ----------------------------------------------------------------------

def test_canary_gives_each_company_one_model(make_client, db):
    client = make_client(CANARY_PERCENT="50")
    first, second = {}, {}
    for i in range(40):
        tax_id = f"B{i:08d}"
        first[tax_id] = post(client, application_id=f"C-{i}", tax_id=tax_id).json()["decision"]["model_version"]
        second[tax_id] = post(client, application_id=f"C-{i}-B", tax_id=tax_id,
                              requested_amount="110000").json()["decision"]["model_version"]
    assert first == second                         # a company keeps its model across applications
    assert set(first.values()) == {"v1", "v2"}     # both versions answer someone
    # Whichever version answered, the other one was scored as its shadow.
    assert count(db, "shadow_comparison", "primary_version <> shadow_version") == 80


def test_full_rollout_and_rollback(make_client, db):
    full = make_client(CANARY_PERCENT="100")
    body = post(full, requested_amount="150000").json()       # 15%: v1 low, v2-demo (12%) high
    assert body["decision"] == {"risk_band": "high_risk", "model_version": "v2",
                                "reasons": ["high_requested_to_revenue"]}
    shadow = db.execute("SELECT model_version, risk_band FROM evaluations WHERE role = 'shadow'").fetchone()
    assert shadow == {"model_version": "v1", "risk_band": "low_risk"}   # stable version kept warm

    rolled_back = make_client(CANARY_PERCENT="0")
    # Same company, same figures, but v1 answers again: the v2 decision is not reused.
    body = post(rolled_back, application_id="T-2", requested_amount="150000").json()
    assert body["status"] == "scored" and body["decision"]["model_version"] == "v1"


def test_failing_canary_falls_back_to_the_stable_version(make_client, db):
    client = make_client(CANARY_PERCENT="100", SCORER_URLS="v1=http://scorer-v1:8000,v2=http://scorer-v2:9")
    body = post(client).json()
    assert body["status"] == "scored" and body["decision"]["model_version"] == "v1"
    rows = {e["role"]: e for e in db.execute("SELECT role, model_version, error FROM evaluations").fetchall()}
    assert rows["primary"]["model_version"] == "v1"
    assert rows["shadow"]["model_version"] == "v2" and rows["shadow"]["error"]
    assert client.get("/ready").json()["ready"] is True


def test_misconfigured_rollout_does_not_start(make_client):
    with pytest.raises(ValueError):
        make_client(CANDIDATE_MODEL="v3")      # no scorer URL for v3


# --- failures and limits --------------------------------------------------------

def test_primary_scorer_down_stores_nothing_and_retry_recovers(make_client, db):
    broken = make_client(SCORER_URLS="v1=http://scorer-v1:9,v2=http://scorer-v2:8000")
    response = post(broken)
    assert response.status_code == 503 and response.headers["Retry-After"] == "5"
    assert count(db, "applications") == 0
    healthy = make_client(SCORER_URLS="v1=http://scorer-v1:8000,v2=http://scorer-v2:8000")
    assert post(healthy).status_code == 201


def test_shadow_scorer_down_does_not_affect_the_answer(make_client, db):
    client = make_client(SCORER_URLS="v1=http://scorer-v1:8000,v2=http://scorer-v2:9")
    assert post(client).json()["status"] == "scored"
    shadow = db.execute("SELECT * FROM evaluations WHERE role = 'shadow'").fetchone()
    assert shadow["risk_band"] is None and "v2" in shadow["error"]


def test_rate_limit_per_source(make_client, db):
    client = make_client(RATE_LIMIT_CAPACITY="3", RATE_LIMIT_REFILL_PER_S="0.01")
    codes = [post(client, application_id=f"T-{i}").status_code for i in range(5)]
    assert codes == [201, 201, 201, 429, 429]
    blocked = post(client, application_id="T-9")
    assert int(blocked.headers["Retry-After"]) >= 1
    assert post(client, application_id="T-9", source_system="other").status_code == 201  # separate bucket
    assert count(db, "applications") == 4


def test_redis_down_fails_open(make_client, db):
    client = make_client(REDIS_URL="redis://redis:6390/0")   # nothing listens there
    assert post(client).status_code == 201
    assert post(client).status_code == 200                    # retry served by Postgres
    assert count(db, "applications") == 1


# --- the supplied export ----------------------------------------------------------

def test_full_csv_replay(client, db, csv_rows, replay, prototype):
    """The supplied export, row by row, as the source would send it."""
    http, status = collections.Counter(), collections.Counter()
    for row in csv_rows:
        response = client.post("/v1/applications", json=replay.row_to_payload(row))
        http[response.status_code] += 1
        if response.status_code == 201:                # count each application once, not its retries
            status[response.json()["status"]] += 1

    assert http == {201: 192, 200: 4}                  # 4 exact re-sends of the same application_id
    assert count(db, "companies") == 180               # tax-ID spelling variants collapse
    assert status["needs_clarification"] == 25         # 22 missing + 2 zero + 1 negative headcount
    assert status["needs_review"] == 13                # requested > revenue (revenue probably in thousands),
                                                       # EBITDA > revenue, 50,000 staff on 90k revenue
    assert status["duplicate"] == 8                    # '-DUP' rows that pass the gate reuse the first decision

    # The v1 scorer reproduces the Data Science prototype on every row it scored.
    expected = {r["application_id"]: prototype.score_row(r) for r in csv_rows if r["employees"] not in ("", "0")}
    scored = db.execute("""SELECT a.external_id, e.risk_band FROM applications a
                           JOIN evaluations e ON e.application_id = a.id AND e.role = 'primary'""").fetchall()
    assert len(scored) == status["scored"] and all(expected[s["external_id"]] == s["risk_band"] for s in scored)

    # Every high-risk result of the prototype came from implausible data, now held for review:
    # on what is left, v1 flags nobody.
    assert count(db, "evaluations", "role = 'primary' AND risk_band = 'high_risk'") == 0

    # Shadow v2-demo (12% ceiling) moves 41 companies from low to high risk, as in the EDA.
    changed = db.execute("SELECT primary_band, shadow_band FROM shadow_comparison WHERE NOT agree").fetchall()
    assert len(changed) == 41
    assert all((c["primary_band"], c["shadow_band"]) == ("low_risk", "high_risk") for c in changed)
    assert count(db, "evaluations", "role = 'primary' AND risk_band IS NOT NULL") == status["scored"]
