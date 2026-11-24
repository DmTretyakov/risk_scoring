# Walkthrough

What to send to the running stack and what you should see. The database starts empty: applications get there only
when you send them, by hand (steps 1–2) or from the CSV (step 3). The numbers below assume a fresh start:

```bash
docker compose down -v && docker compose up -d --build     # -v deletes the database
```

## 1. Score one application

```bash
API=localhost:8010/v1/applications
APP='{"application_id":"DEMO-1","source_system":"manual_entry","company_name":"Demo S.L.","tax_id":"B99887766",
      "sector":"Retail","region":"Madrid","employees":12,"annual_revenue":"1500000","requested_amount":"200000"}'
curl -si -X POST $API -H 'Content-Type: application/json' -d "$APP"
```

`201`, `"status":"scored"`, `"risk_band":"low_risk"`, `"model_version":"v1"`. Sending it again returns the stored
answer (`200`, header `Idempotent-Replayed: true`); nothing new is stored.

v1 answered. The candidate v2 scored the same application after the response; its result is stored next to v1's and
never returned. Here they disagree: the request is 13% of revenue, and v2's ceiling is 12%.

```bash
docker compose exec postgres psql -U novicap -d decisions -c \
  "SELECT a.external_id, s.primary_version, s.primary_band, s.shadow_version, s.shadow_band
   FROM shadow_comparison s JOIN applications a ON a.id = s.application_id;"
```

## 2. Missing data, then a correction

```bash
curl -s -X POST $API -H 'Content-Type: application/json' \
  -d '{"application_id":"DEMO-2","source_system":"manual_entry","tax_id":"B11223344","annual_revenue":"900000","requested_amount":"100000"}'
# -> "status":"needs_clarification", issue "missing_required_value" on employees: stored, not scored

curl -s -X POST $API -H 'Content-Type: application/json' \
  -d '{"application_id":"DEMO-2","source_system":"manual_entry","tax_id":"B11223344","employees":8,"annual_revenue":"900000","requested_amount":"100000"}'
# -> same application_id with the headcount added: "status":"scored", "revision":2
```

## 3. Replay the supplied CSV

```bash
python3 scripts/replay_csv.py data/raw_applications.csv
```

Expect `New apps {'scored': 146, 'needs_review': 13, 'needs_clarification': 25, 'duplicate': 8}` and
`Risk bands {'low_risk': 154}`: the 13 requests above annual revenue go to review, so v1 calls nothing in the export
high risk. A few `429` backoffs are normal: the script sends faster than the per-source rate limit and retries.

## 4. A feed breaks

```bash
docker compose exec monitor python -m app.monitoring freeze-reference     # the replayed CSV becomes the reference
python3 scripts/simulate_traffic.py --scenario normal --n 200 --seed 1    # similar companies
python3 scripts/simulate_traffic.py --scenario degraded --n 400 --seed 2  # new_partner_feed: revenue /3, 40% no headcount
```

Grafana (http://localhost:3010, "Last 15 minutes"), within a minute after `degraded`:

- **Data quality by source:** the clarification share of `new_partner_feed` jumps;
- **Model behaviour:** the v1 high-risk share jumps from 0 far above the 20% line;
- **Drift:** revenue-based features in `alert`, while `requested_amount` stays `ok`: revenue changed, not the requests;
- http://localhost:9090/alerts: `SourceDataQualityDropped`, `HighRiskShareShift`, `FeatureDrift` firing.

## 5. Roll out v2, then roll back

```bash
CANARY_PERCENT=25 docker compose up -d decision-api    # v2 answers 25% of companies; v1 scores them in shadow
python3 scripts/simulate_traffic.py --n 200
```

Grafana, **Model behaviour**: about a quarter of the answers come from v2, v2 flags far more companies as high risk,
and `HighRiskShareShift` fires for v2. This is where someone decides whether to continue.

```bash
CANARY_PERCENT=100 docker compose up -d decision-api   # v2 answers everyone
docker compose stop scorer-v2                          # break v2 (after `up`, which starts it)
python3 scripts/simulate_traffic.py --n 30             # all answered by v1, no errors
docker compose up -d                                   # default settings again: v1 answers, v2 in shadow
```
