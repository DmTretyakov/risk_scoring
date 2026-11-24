"""Data gate: decides whether an application may reach a model at all.

Runs before routing and scoring. Three outcomes:
  * blocking issue -> needs_clarification (ask the sender to fix the data)
  * review issue   -> needs_review (plausible but suspicious: a human looks first)
  * warnings only  -> pass; warnings are stored for source-quality monitoring

Thresholds marked "proposed" are conservative guards chosen from the EDA,
not validated credit or fraud rules. They are versioned with GATE_VERSION.
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from . import normalize
from .schemas import ApplicationIn, Issue

GATE_VERSION = "gate-v1"
FEATURE_VERSION = "features-v1"
SUPPORTED_CURRENCIES = {"EUR"}          # v1 thresholds are in EUR
MAX_PLAUSIBLE_EMPLOYEES = 10_000        # proposed; EDA has one row with 50,000 staff and 90k revenue

_REQUIRED = ("employees", "annual_revenue", "requested_amount")


@dataclass
class GateResult:
    issues: list[Issue] = field(default_factory=list)
    tax_id: str | None = None           # normalized, only if syntactically valid
    features: dict | None = None        # model inputs; None when blocked

    @property
    def blocked(self) -> bool:
        return any(i.severity == "blocking" for i in self.issues)

    @property
    def needs_review(self) -> bool:
        return any(i.severity == "review" for i in self.issues)


def _plain(value: Decimal) -> str:
    """Decimal as a plain string ('100000', never '1E+5'); exact, so thresholds compare exactly."""
    return format(value, "f")


def _issue(result: GateResult, field_: str, code: str, severity: str, message: str) -> None:
    result.issues.append(Issue(field=field_, code=code, severity=severity, message=message))


def check(app: ApplicationIn, today: date | None = None) -> GateResult:
    today = today or date.today()
    r = GateResult()

    # Identity: without a usable tax ID we cannot tell which company applies, so we do not decide.
    tax_id = normalize.tax_id(app.tax_id)
    if tax_id is None:
        _issue(r, "tax_id", "missing_required_value", "blocking", "tax_id is required")
    elif not normalize.tax_id_is_valid(app.country_code, tax_id):
        _issue(r, "tax_id", "invalid_tax_id", "blocking", f"'{app.tax_id}' is not a valid {app.country_code} tax ID")
    else:
        r.tax_id = tax_id

    # Inputs of the v1 rules: present, non-negative, usable as denominators.
    for name in _REQUIRED:
        value: Decimal | None = getattr(app, name)
        if value is None:
            _issue(r, name, "missing_required_value", "blocking", f"{name} is required for scoring")
        elif value < 0:
            _issue(r, name, "invalid_negative_value", "blocking", f"{name} cannot be negative ({value})")
        elif value == 0:
            _issue(r, name, "unsupported_zero_value", "blocking", f"{name} = 0 cannot be scored by the current rules")
    if app.employees is not None and app.employees > 0 and app.employees != app.employees.to_integral_value():
        _issue(r, "employees", "invalid_headcount", "blocking", "employees must be a whole number (headcount, not FTE)")
    if app.currency not in SUPPORTED_CURRENCIES:
        _issue(r, "currency", "unsupported_currency", "blocking", f"amounts in {app.currency} are not supported")

    # Plausibility: the numbers parse, but do not make sense together.
    if app.employees is not None and app.employees > MAX_PLAUSIBLE_EMPLOYEES:
        _issue(r, "employees", "implausible_headcount", "review",
               f"{app.employees} employees exceeds the plausibility limit {MAX_PLAUSIBLE_EMPLOYEES}")
    if (app.annual_ebitda is not None and app.annual_revenue is not None
            and app.annual_revenue > 0 and app.annual_ebitda > app.annual_revenue):
        _issue(r, "annual_ebitda", "ebitda_exceeds_revenue", "review", "EBITDA cannot exceed revenue")
    # EDA: 13 rows ask for more than a year of revenue; 12 of them report revenue of 1.4k-4.9k EUR
    # next to requests of 54k-595k (probably thousands). v1 would call all 13 high risk.
    if (app.requested_amount is not None and app.annual_revenue is not None
            and app.annual_revenue > 0 and app.requested_amount > app.annual_revenue):
        _issue(r, "requested_amount", "requested_exceeds_revenue", "review",
               "requested amount exceeds annual revenue: confirm the revenue unit and period")

    incorporated, date_problem = normalize.incorporation_date(app.incorporation_date)
    if date_problem:
        _issue(r, "incorporation_date", date_problem, "warning", "date kept as sent; not used by the current model")
    elif incorporated and incorporated > today:
        _issue(r, "incorporation_date", "incorporation_in_future", "review", f"{incorporated} is in the future")

    sector, region = normalize.sector(app.sector), normalize.region(app.region)
    if app.sector and sector is None:
        _issue(r, "sector", "unknown_category", "warning", f"unmapped sector '{app.sector}'")
    if app.region and region is None:
        _issue(r, "region", "unknown_category", "warning", f"unmapped region '{app.region}'")

    if not r.blocked:
        revenue, employees, requested = app.annual_revenue, app.employees, app.requested_amount
        r.features = {
            "feature_version": FEATURE_VERSION,
            "currency": app.currency,
            "annual_revenue": _plain(revenue),
            "employees": int(employees),
            "requested_amount": _plain(requested),
            "annual_ebitda": None if app.annual_ebitda is None else _plain(app.annual_ebitda),
            "revenue_per_employee": _plain(revenue / employees),
            "requested_to_revenue": _plain(requested / revenue),
            "sector": sector,
            "region": region,
            "incorporation_date": incorporated.isoformat() if incorporated else None,
        }
    return r
