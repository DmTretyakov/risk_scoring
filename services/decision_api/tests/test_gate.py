from datetime import date

import pytest

from app import gate, normalize
from app.schemas import ApplicationIn


def make(**overrides) -> ApplicationIn:
    base = dict(application_id="T-1", source_system="test", tax_id="B12345678", employees=10,
                annual_revenue="1000000", requested_amount="100000", sector="Retail",
                region="Madrid", incorporation_date="2010-05-01")
    base.update(overrides)
    return ApplicationIn(**base)


def codes(result) -> set[tuple[str, str, str]]:
    return {(i.field, i.code, i.severity) for i in result.issues}


def test_clean_application_passes_with_features():
    result = gate.check(make())
    assert result.issues == [] and not result.blocked and not result.needs_review
    assert result.features["revenue_per_employee"] == "100000"
    assert result.features["requested_to_revenue"] == "0.1"
    assert result.features["sector"] == "retail"


@pytest.mark.parametrize("overrides, expected", [
    (dict(employees=None), ("employees", "missing_required_value")),
    (dict(employees=-3), ("employees", "invalid_negative_value")),
    (dict(employees=0), ("employees", "unsupported_zero_value")),
    (dict(employees="2.5"), ("employees", "invalid_headcount")),
    (dict(annual_revenue=None), ("annual_revenue", "missing_required_value")),
    (dict(requested_amount="-5"), ("requested_amount", "invalid_negative_value")),
    (dict(tax_id=None), ("tax_id", "missing_required_value")),
    (dict(tax_id="12-3"), ("tax_id", "invalid_tax_id")),
    (dict(currency="USD"), ("currency", "unsupported_currency")),
])
def test_blocking_issues_stop_scoring(overrides, expected):
    result = gate.check(make(**overrides))
    assert result.blocked
    assert (*expected, "blocking") in codes(result)
    assert result.features is None


@pytest.mark.parametrize("overrides, expected", [
    (dict(employees=50_000, annual_revenue="90000"), ("employees", "implausible_headcount")),
    (dict(annual_ebitda="2000000"), ("annual_ebitda", "ebitda_exceeds_revenue")),
    (dict(annual_revenue="3225.09", requested_amount="280516.32"), ("requested_amount", "requested_exceeds_revenue")),
    (dict(incorporation_date="2999-01-01"), ("incorporation_date", "incorporation_in_future")),
])
def test_suspicious_values_go_to_review(overrides, expected):
    result = gate.check(make(**overrides), today=date(2026, 11, 20))
    assert not result.blocked and result.needs_review
    assert (*expected, "review") in codes(result)


@pytest.mark.parametrize("overrides", [
    dict(annual_ebitda=None),                 # optional for v1
    dict(annual_ebitda="-50000"),             # loss-making is legitimate
    dict(incorporation_date="07/06/2007"),    # ambiguous day/month: warning only
    dict(sector="Space mining"),              # unknown label: warning only
])
def test_soft_problems_do_not_block(overrides):
    result = gate.check(make(**overrides))
    assert not result.blocked and not result.needs_review
    assert all(i.severity == "warning" for i in result.issues)


def test_tax_id_normalization_links_formatting_variants():
    assert {normalize.tax_id(t) for t in ["E-36472290", "e36472290", " E.364-72290 "]} == {"E36472290"}


@pytest.mark.parametrize("raw, expected", [
    ("FOOD MANUFACTURING", "food_manufacturing"), ("food manuf.", "food_manufacturing"),
    ("Food & Beverage Mfg", "food_manufacturing"), ("Retail / Wholesale", "retail"),
    ("IT / Software", "it_services"), ("Information Technology", "it_services"),
    ("Building & Construction", "construction"), ("consultancy ", "consulting"),
    ("Management Consultancy", "consulting"),
])
def test_sector_spellings_collapse(raw, expected):
    assert normalize.sector(raw) == expected


def test_dates():
    assert normalize.incorporation_date("2007-10-02") == (date(2007, 10, 2), None)
    assert normalize.incorporation_date("14/02/2009") == (date(2009, 2, 14), None)
    assert normalize.incorporation_date("07/06/2007") == (None, "ambiguous_date_format")
