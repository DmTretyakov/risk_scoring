-- Monitoring: drift results, outcome views, read-only role for Grafana.

-- Frozen reference population: per-feature bins and shares (see app/monitoring.py).
-- A new reference is frozen deliberately (e.g. after a validated model release),
-- never silently moved, otherwise slow drift becomes invisible.
CREATE TABLE drift_reference (
    id           bigserial PRIMARY KEY,
    created_at   timestamptz NOT NULL DEFAULT now(),
    window_start timestamptz NOT NULL,
    window_end   timestamptz NOT NULL,
    companies    integer     NOT NULL,
    spec         jsonb       NOT NULL
);

-- One row per feature per monitor run. History survives Prometheus retention.
CREATE TABLE drift_runs (
    id           bigserial PRIMARY KEY,
    run_at       timestamptz NOT NULL,
    reference_id bigint      NOT NULL REFERENCES drift_reference(id),
    window_start timestamptz NOT NULL,
    window_end   timestamptz NOT NULL,
    companies    integer     NOT NULL,
    feature      text        NOT NULL,
    psi          double precision,                 -- NULL when the window is too small
    status       text        NOT NULL CHECK (status IN ('ok', 'watch', 'alert', 'insufficient_data'))
);
CREATE INDEX drift_runs_time ON drift_runs (run_at DESC);

-- Target monitoring, once outcomes arrive: default rate per funding month and model.
-- Only matured loans count in the rate; 'unknown' is not a default.
CREATE VIEW loan_vintages AS
SELECT date_trunc('month', l.funded_at)                          AS vintage,
       e.model_version,
       count(*)                                                  AS loans,
       count(*) FILTER (WHERE l.outcome <> 'unknown')            AS matured,
       count(*) FILTER (WHERE l.outcome = 'defaulted')           AS defaulted,
       round(count(*) FILTER (WHERE l.outcome = 'defaulted')::numeric
             / NULLIF(count(*) FILTER (WHERE l.outcome <> 'unknown'), 0), 4) AS default_rate
FROM loans l JOIN evaluations e ON e.id = l.evaluation_id
GROUP BY 1, 2;

-- Grafana reads with a role that cannot write.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_reader') THEN
        CREATE ROLE grafana_reader LOGIN PASSWORD 'grafana-dev-only';
    END IF;
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO grafana_reader', current_database());
END $$;
GRANT USAGE ON SCHEMA public TO grafana_reader;
-- Only what the dashboard shows: drift results and outcome aggregates, never payloads.
GRANT SELECT ON drift_runs, loan_vintages TO grafana_reader;
