"""All SQL lives here."""
from uuid import UUID

from psycopg import Connection
from psycopg.types.json import Jsonb


def find_revisions(conn: Connection, source_system: str, external_id: str) -> list[dict]:
    """All revisions of one sender application, newest first (served by the unique index)."""
    return conn.execute(
        """SELECT id, revision, request_hash, status, response FROM applications
           WHERE source_system = %s AND external_id = %s ORDER BY revision DESC""",
        (source_system, external_id),
    ).fetchall()


def upsert_company(conn: Connection, country_code: str, tax_id: str, name: str | None) -> UUID:
    # DO UPDATE (no-op) instead of DO NOTHING so RETURNING also yields existing rows.
    row = conn.execute(
        """INSERT INTO companies (country_code, tax_id, name) VALUES (%s, %s, %s)
           ON CONFLICT (country_code, tax_id) DO UPDATE SET country_code = EXCLUDED.country_code
           RETURNING id""",
        (country_code, tax_id, name),
    ).fetchone()
    return row["id"]


def find_duplicate(conn: Connection, company_id: UUID, features_hash: str, model_version: str) -> dict | None:
    """An application of the same company, scored on the same features by the same model version.
    The model is deterministic, so its decision is reused instead of scoring again."""
    return conn.execute(
        """SELECT a.id, a.response -> 'decision' AS decision
           FROM applications a JOIN evaluations e ON e.application_id = a.id AND e.role = 'primary'
           WHERE a.company_id = %s AND a.status = 'scored' AND a.features_hash = %s AND e.model_version = %s
           ORDER BY a.received_at LIMIT 1""",
        (company_id, features_hash, model_version),
    ).fetchone()


def insert_application(conn: Connection, *, id: UUID, source_system: str, external_id: str, revision: int,
                       supersedes: UUID | None, request_hash: str, company_id: UUID | None, received_at,
                       payload: dict, features: dict | None, features_hash: str | None, gate_version: str,
                       status: str, issues: list[dict], duplicate_of: UUID | None, response: dict) -> None:
    conn.execute(
        """INSERT INTO applications (id, source_system, external_id, revision, supersedes, request_hash,
               company_id, received_at, payload, features, features_hash, gate_version, status, issues,
               duplicate_of, response)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (id, source_system, external_id, revision, supersedes, request_hash, company_id, received_at,
         Jsonb(payload), Jsonb(features) if features is not None else None, features_hash, gate_version,
         status, Jsonb(issues), duplicate_of, Jsonb(response)),
    )


def insert_evaluation(conn: Connection, *, application_id: UUID, role: str, model_version: str,
                      artifact_sha256: str | None, risk_band: str | None, reasons: list[str],
                      error: str | None, latency_ms: float | None) -> None:
    conn.execute(
        """INSERT INTO evaluations (application_id, role, model_version, artifact_sha256, risk_band, reasons,
               error, latency_ms)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (application_id, role, model_version, artifact_sha256, risk_band, reasons, error, latency_ms),
    )


def get_response(conn: Connection, application_id: UUID) -> dict | None:
    row = conn.execute("SELECT response FROM applications WHERE id = %s", (application_id,)).fetchone()
    return row["response"] if row else None


def list_company_applications(conn: Connection, country_code: str, tax_id: str) -> list[dict]:
    return conn.execute(
        """SELECT a.id AS application_id, a.external_id, a.source_system, a.revision, a.received_at, a.status,
                  a.response -> 'decision' ->> 'risk_band' AS risk_band
           FROM companies c JOIN applications a ON a.company_id = c.id
           WHERE c.country_code = %s AND c.tax_id = %s
           ORDER BY a.received_at DESC""",
        (country_code, tax_id),
    ).fetchall()
