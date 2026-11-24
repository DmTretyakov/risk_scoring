# Novicap: working-capital risk decision service

An HTTP service that checks incoming SME applications, scores them with the Data Science prototype (v1) and stores
every decision. A second model version (v2-demo) runs next to it to show how a new model is rolled out.

## Run

Needs Docker with Compose v2. The supplied task files are not in the repository; copy them in first:

```bash
mkdir -p data reference
cp /path/to/raw_applications.csv data/               # required: CSV replay, synthetic traffic, tests, EDA
cp /path/to/risk_scoring_prototype.py reference/     # required by the tests: v1 must match the prototype
```

Everything else is in `docker-compose.yml`; one command builds and starts it:

```bash
docker compose up -d --build
```

| Container | Role |
| --- | --- |
| `decision-api` | The HTTP API: checks the data, picks the model version, stores the result, answers |
| `scorer-v1`, `scorer-v2` | One container per model version; each loads its model file from `models/` at start-up |
| `postgres` | Applications, decisions, drift results |
| `redis` | Rate limit per source and a short cache of answers for retries |
| `monitor` | Drift job, every 30 s |
| `prometheus`, `grafana` | Metrics, alerts, dashboard |

The database starts empty: nothing is scored until you send it. When `curl localhost:8010/ready` answers
`"ready":true`:

| Open | What |
| --- | --- |
| http://localhost:8010/docs | The API; every endpoint can be called from this page |
| http://localhost:3010 | Grafana dashboard "Decision service", no login |
| http://localhost:9090/alerts | Prometheus alert rules |

[WALKTHROUGH.md](WALKTHROUGH.md) lists what to send and what you should see: one application, the supplied CSV, a
broken feed, a model rollout.

```bash
docker compose down        # stop; add -v to also delete the database
```

## Tests

Two test containers that `up` does not start. Each builds, runs once and exits; they use their own database.

```bash
docker compose --profile test run --build --rm tests          # API against real Postgres, Redis and both scorers
docker compose --profile test run --build --rm scorer-tests   # scorer rules
```

## Documents and layout

- [DESIGN.md](DESIGN.md): architecture, tradeoffs, rollout and monitoring.
- [eda/FINDINGS.md](eda/FINDINGS.md): what the data showed; details in `eda/applications_eda.ipynb`.

```text
services/decision_api/  API, data checks, routing, storage; the drift monitor
services/scorer/        Serves one model version
models/                 Model files: v1 = the prototype's rules, v2 = demo challenger
infra/db/               PostgreSQL schema (applied to an empty database)
infra/monitoring/       Prometheus config, alert rules, Grafana dashboard
scripts/                CSV replay and synthetic traffic (standard-library Python)
eda/                    Notebook and findings
data/, reference/       Supplied CSV and prototype (not in the repository, see Run)
```
