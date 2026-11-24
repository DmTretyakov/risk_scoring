"""Which model version answers an application and which one is scored in shadow.

Configured by PRIMARY_MODEL (stable), CANDIDATE_MODEL (new) and CANARY_PERCENT:
  0     candidate in shadow only: scored after the response, never returned
  10    candidate answers 10% of companies; the stable version is their shadow
  100   candidate answers everyone; the stable version stays warm in shadow
Rollback at any stage: CANARY_PERCENT=0. Promotion: PRIMARY_MODEL=<candidate>.

The canary group is a hash of (candidate version, company): a company always gets the
same model during a rollout, raising the percentage only adds companies, and each new
candidate is tried on a different group.
"""
import hashlib
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class Route:
    answer: str              # version whose result is returned
    shadow: str | None       # the other version, scored after the response
    fallback: str | None     # stable version that answers if the canary scorer fails


def in_canary(company_id: UUID, candidate: str, percent: int) -> bool:
    bucket = int.from_bytes(hashlib.sha256(f"{candidate}:{company_id}".encode()).digest()[:8], "big") % 100
    return bucket < percent


def route(company_id: UUID, primary: str, candidate: str | None, canary_percent: int) -> Route:
    if candidate is None:
        return Route(answer=primary, shadow=None, fallback=None)
    if in_canary(company_id, candidate, canary_percent):
        return Route(answer=candidate, shadow=primary, fallback=primary)
    return Route(answer=primary, shadow=candidate, fallback=None)
