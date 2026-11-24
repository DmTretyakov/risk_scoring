# Design

The export is unreviewed, the formula will change, and repayment outcomes are months away. So the service scores only
data it can trust, keeps enough to reproduce every decision, rolls models out gradually and reversibly, and watches
inputs and outputs instead of outcomes. **Next** marks what is not built.

**What the data says** ([eda/FINDINGS.md](eda/FINDINGS.md)): 196 rows are 180 companies, so identity is the
normalized tax ID and retries are idempotent. Headcounts are missing or negative and the prototype crashes on 24 rows,
so a data gate asks the source instead of imputing. All prototype high-risk results fall on records that need a data
check (mostly revenue that looks like thousands), so a request above annual revenue goes to review. On the other 146
companies v1 flags none while v2-demo changes 41, so a new version is shadowed before it answers anyone.

## Architecture

```text
POST /v1/applications -> decision-api
  1. rate limit per source ........................................ Redis
  2. known application_id? retry -> stored answer; correction -> new revision ... Postgres
  3. data gate: missing / implausible values -> needs_clarification / needs_review
  4. router: which model version answers this company
  5. same company and figures already scored by that version? -> duplicate, that decision reused
  6. answer: scorer-v1 (stable)     shadow, after the response: scorer-v2 (candidate)
  7. store the application and both results ........................ Postgres
monitor (every 30 s): drift of stored features against a frozen reference -> Postgres
Prometheus scrapes the API, scorers and monitor and fires alerts; Grafana shows metrics and drift.
```

- **Outcomes:** `scored`; `duplicate` (same company, figures and model version as an already scored application: the
  model is deterministic, so that decision is reused); `needs_clarification` (the source fixes the data and re-sends
  the same id as a new revision); `needs_review` (implausible figures).
- **Failures:** stable scorer down → 503, nothing stored, safe to retry; candidate down → the stable version answers;
  Redis down → rate limit fails open.

| Tradeoff | Why | Cost |
| --- | --- | --- |
| A scorer container per model version | Roll a model out and back without an API release | A network hop, more containers |
| Features computed once, in the API | One place for validation; stored features allow replay and drift checks | New inputs need an API release |
| Shadow as an in-process background task | Simple, adds no latency | Lost if the process dies (**Next:** a queue) |

## Versioning and rollout

A model is an artifact (`models/v1.json`) identified by version and SHA-256; every result is stored with both, next to
its features. Settings `PRIMARY_MODEL`, `CANDIDATE_MODEL`, `CANARY_PERCENT`:

1. **Replay** stored applications through the new artifact offline.
2. **Shadow** (`CANARY_PERCENT=0`): the candidate scores live traffic after each response and never answers.
3. **Canary** (5 → 25 → 100% of companies, sticky by company): the stable version answers if the candidate fails.
4. **Promote:** `PRIMARY_MODEL=v2 CANDIDATE_MODEL=v1 CANARY_PERCENT=0`.

Rollback is a config change, no build: `CANARY_PERCENT=0` before promotion, `PRIMARY_MODEL=v1 CANDIDATE_MODEL=v2
CANARY_PERCENT=0` after it. Readiness needs only Postgres and the stable scorer.

## Detecting bad behaviour without outcomes

| Signal | Catches |
| --- | --- |
| 5xx, latency, scorer errors | Service failures |
| Clarification share and issue codes per source | A feed starting to send broken data, usually the first sign |
| High-risk share per model; primary vs shadow disagreement | Prediction drift; a candidate unlike its replay |
| PSI per feature against a frozen reference | Input drift |

Thresholds are starting points from 196 rows, to be calibrated on real traffic.

## Debugging, new prototypes

Postgres keeps per application the payload as received, features, gate issues, model version, artifact SHA-256 and
reasons (primary and shadow), the exact response and earlier revisions: "why did X get this answer on date Y" is one
query, reproducible with the same artifact. Each request also logs one JSON line with these IDs.

A new prototype with new thresholds is a new artifact and scorer container, then replay, shadow, canary; no code
change. New inputs need a new `feature_version` in the API; a trained model needs a loader in the scorer. Data Science
hands over expected outputs on a reference set, which becomes the parity test, as for v1.

**On AWS** (not built): EKS with a deployment per scorer version, RDS PostgreSQL, ElastiCache, artifacts in S3 or
MLflow, rollout settings as Helm values in Git applied by Argo CD, managed Prometheus and Grafana.

## Scope and Next

A local demo: no authentication, Grafana without login, ports bound to `127.0.0.1`.

- **Prepared, not in use:** `loans` table and `loan_vintages` view (default rate by month and model). **Next:** tie
  each loan to its application's primary result and count defaults after a fixed horizon.
- **Next:** shadow completion and fallback metrics; expected high-risk share per version; immutable artifacts and a
  registry; a CI gate with replay and parity; an API key per source; a `needs_review` workflow; Alembic.
