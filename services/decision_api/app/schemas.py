"""HTTP contract. FastAPI publishes it as OpenAPI at /docs and /openapi.json."""
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

Status = Literal["scored", "duplicate", "needs_clarification", "needs_review"]
Severity = Literal["blocking", "review", "warning"]
RiskBand = Literal["low_risk", "high_risk"]


class ApplicationIn(BaseModel):
    """One application as sent by a source.

    Types are checked here (422 on garbage). Business meaning (negative
    headcount, zero revenue, ...) is checked by the gate, so the record is
    kept and the sender gets field-level reasons instead of a bare 422.
    Missing values are JSON null; source-specific tokens such as "n/a" are
    mapped to null by the source adapter, not by the API.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    application_id: str = Field(min_length=1, max_length=100)
    # Stand-in for the authenticated partner identity in this prototype.
    source_system: str = Field(min_length=1, max_length=100)
    company_name: str | None = Field(default=None, max_length=300)
    country_code: str = Field(default="ES", pattern=r"^[A-Z]{2}$")
    tax_id: str | None = Field(default=None, max_length=40)
    sector: str | None = Field(default=None, max_length=100)
    region: str | None = Field(default=None, max_length=100)
    incorporation_date: str | None = Field(default=None, max_length=20)
    employees: Decimal | None = None
    annual_revenue: Decimal | None = None
    annual_ebitda: Decimal | None = None
    requested_amount: Decimal | None = None
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    notes: str | None = Field(default=None, max_length=2000)


class Issue(BaseModel):
    field: str
    code: str
    severity: Severity
    message: str


class Decision(BaseModel):
    risk_band: RiskBand
    model_version: str
    reasons: list[str]


class ApplicationOut(BaseModel):
    application_id: UUID
    external_id: str
    source_system: str
    revision: int = Field(description="1 for the first submission; +1 for each correction of the same application_id")
    supersedes: UUID | None = Field(description="previous revision that this correction replaces")
    status: Status
    decision: Decision | None
    issues: list[Issue]
    duplicate_of: UUID | None
    gate_version: str
    received_at: datetime


class HistoryItem(BaseModel):
    application_id: UUID
    external_id: str
    source_system: str
    revision: int
    received_at: datetime
    status: Status
    risk_band: RiskBand | None
