-- Decision service schema (PostgreSQL 16).
-- Applied automatically by the postgres container on an empty volume.
-- Postgres is the source of truth; Redis only holds disposable copies.

-- One row per legal entity. The normalized tax ID is the business key:
-- company names change, tax IDs do not.
CREATE TABLE companies (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    country_code char(2)     NOT NULL,
    tax_id       text        NOT NULL,          -- upper-case, separators removed
    name         text,                          -- first name seen; informational only
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (country_code, tax_id)
);

-- Every submission is kept, including invalid ones: they are evidence for
-- source-quality monitoring and for answering "why did we say that?".
-- A sender may correct an application we could not score (needs_clarification /
-- needs_review) by re-sending the same application_id: that creates revision n+1,
-- linked to the previous one. Decided applications cannot be changed.
CREATE TABLE applications (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_system text        NOT NULL,
    external_id   text        NOT NULL,         -- the sender's application ID
    revision      smallint    NOT NULL DEFAULT 1 CHECK (revision >= 1),
    supersedes    uuid        REFERENCES applications(id),  -- previous revision
    request_hash  char(64)    NOT NULL,         -- sha256 of the canonical request
    company_id    uuid        REFERENCES companies(id),  -- NULL if tax ID unusable
    received_at   timestamptz NOT NULL,
    payload       jsonb       NOT NULL,         -- request exactly as received
    features      jsonb,                        -- normalized inputs; NULL if blocked by the gate
    features_hash char(64),
    gate_version  text        NOT NULL,
    status        text        NOT NULL
        CHECK (status IN ('scored', 'duplicate', 'needs_clarification', 'needs_review')),
    issues        jsonb       NOT NULL DEFAULT '[]',
    duplicate_of  uuid        REFERENCES applications(id),
    response      jsonb       NOT NULL,         -- exact response, replayed on retries
    UNIQUE (source_system, external_id, revision),   -- idempotency key + correction history
    CHECK ((revision = 1) = (supersedes IS NULL)),
    CHECK ((status = 'duplicate') = (duplicate_of IS NOT NULL))
);
-- Per-company lookups: the duplicate check on every request and the company history endpoint.
CREATE INDEX applications_company_history ON applications (company_id, received_at DESC);
-- Monitoring windows: status mix per source over time.
CREATE INDEX applications_received ON applications (received_at, source_system, status);

-- Model outputs on the frozen features of one application.
-- 'primary' is the result we answered with; 'shadow' is the other configured version,
-- scored on the same features after the response and never shown to the sender.
-- A canary that failed (the stable version answered instead) is a shadow error.
-- If no model can answer, the request fails (503) and nothing is stored.
CREATE TABLE evaluations (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id  uuid        NOT NULL REFERENCES applications(id),
    role            text        NOT NULL CHECK (role IN ('primary', 'shadow')),
    model_version   text        NOT NULL,
    artifact_sha256 char(64),                   -- exact artifact that produced the result
    risk_band       text        CHECK (risk_band IN ('low_risk', 'high_risk')),
    reasons         text[]      NOT NULL DEFAULT '{}',
    error           text,                       -- shadow scorer failure
    latency_ms      real,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (application_id, role),
    CHECK ((risk_band IS NULL) = (error IS NOT NULL)),
    CHECK (role = 'shadow' OR error IS NULL)
);

-- Primary vs shadow on the same applications: the main signal for a new model
-- version before any repayment outcome exists.
CREATE VIEW shadow_comparison AS
SELECT a.id AS application_id, a.received_at, a.source_system,
       p.model_version AS primary_version, p.risk_band AS primary_band,
       s.model_version AS shadow_version,  s.risk_band AS shadow_band, s.error AS shadow_error,
       p.risk_band = s.risk_band AS agree
FROM applications a
JOIN evaluations p ON p.application_id = a.id AND p.role = 'primary'
JOIN evaluations s ON s.application_id = a.id AND s.role = 'shadow';

-- Funded loans and their eventual outcome: the future training/evaluation label.
-- Filled by an internal job from the loan-servicing system, never by partners.
-- No row = not funded (NOT "did not repay"); 'unknown' = not matured yet.
CREATE TABLE loans (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id   uuid          NOT NULL UNIQUE REFERENCES applications(id),
    evaluation_id    uuid          NOT NULL REFERENCES evaluations(id),  -- the result we acted on
    principal_amount numeric(18,2) NOT NULL CHECK (principal_amount > 0),
    currency         char(3)       NOT NULL,
    funded_at        timestamptz   NOT NULL,
    due_at           timestamptz   NOT NULL CHECK (due_at > funded_at),
    outcome          text          NOT NULL DEFAULT 'unknown'
        CHECK (outcome IN ('unknown', 'repaid', 'late', 'defaulted')),
    outcome_at       timestamptz,
    CHECK ((outcome = 'unknown') = (outcome_at IS NULL))
);
-- Loans whose outcome should be collected next.
CREATE INDEX loans_awaiting_outcome ON loans (due_at) WHERE outcome = 'unknown';
