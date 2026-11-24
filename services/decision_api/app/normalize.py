"""Deterministic clean-up of identifiers and labels found in the EDA."""
import re
from datetime import date

_NON_ALNUM = re.compile(r"[^A-Z0-9]")
# Spanish company tax ID (CIF/NIF): 9 alphanumerics once separators are removed.
_TAX_ID_PATTERNS = {"ES": re.compile(r"^[A-Z0-9]{9}$")}
_GENERIC_TAX_ID = re.compile(r"^[A-Z0-9]{5,20}$")

# The export uses ~20 spellings for 5 sectors ("FOOD MANUFACTURING", "food manuf.", ...).
_SECTOR_RULES = [
    ("food_manufacturing", ("food",)),
    ("retail", ("retail",)),
    ("it_services", ("it services", "it / software", "information technology", "software")),
    ("construction", ("construction",)),
    ("consulting", ("consult",)),
]
_REGIONS = {
    "andalucia": "andalucia", "cataluna": "cataluna", "catalunya": "cataluna",
    "galicia": "galicia", "madrid": "madrid", "pais vasco": "pais_vasco",
    "valencia": "valencia",
}


def tax_id(raw: str | None) -> str | None:
    """'e-33969978' -> 'E33969978'. Syntax only: it does not prove the ID is real."""
    if not raw:
        return None
    return _NON_ALNUM.sub("", raw.upper()) or None


def tax_id_is_valid(country_code: str, normalized: str) -> bool:
    return bool(_TAX_ID_PATTERNS.get(country_code, _GENERIC_TAX_ID).match(normalized))


def sector(raw: str | None) -> str | None:
    if not raw:
        return None
    text = raw.strip().lower()
    for canonical, needles in _SECTOR_RULES:
        if any(n in text for n in needles):
            return canonical
    return None


def region(raw: str | None) -> str | None:
    if not raw:
        return None
    return _REGIONS.get(raw.strip().lower())


def incorporation_date(raw: str | None) -> tuple[date | None, str | None]:
    """Returns (date, problem). Accepts ISO dates and unambiguous dd/mm/yyyy.

    '07/06/2007' could be 7 June or 6 July: we do not guess; the source owner
    has to confirm the locale. 'problem' is None when the value is fine or absent.
    """
    if not raw:
        return None, None
    try:
        return date.fromisoformat(raw), None
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
    if not m:
        return None, "unparseable_date"
    a, b, year = int(m[1]), int(m[2]), int(m[3])
    if a <= 12 and b <= 12 and a != b:
        return None, "ambiguous_date_format"
    day, month = (a, b) if a > 12 or a == b else (b, a)
    try:
        return date(year, month, day), None
    except ValueError:
        return None, "unparseable_date"
