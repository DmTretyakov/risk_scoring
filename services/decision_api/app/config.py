import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    # version -> base URL, e.g. "v1=http://scorer-v1:8000,v2=http://scorer-v2:8000"
    scorer_urls: dict[str, str]
    primary_model: str               # stable version (see router.py)
    candidate_model: str | None      # new version: shadow, or answers the canary share
    canary_percent: int              # share of companies the candidate answers, 0-100
    scorer_timeout_s: float
    rate_limit_capacity: int         # burst size per source
    rate_limit_refill_per_s: float   # sustained requests per second per source
    retry_cache_ttl_s: int


def _urls(raw: str) -> dict[str, str]:
    return dict(item.strip().split("=", 1) for item in raw.split(",") if item.strip())


def load_settings() -> Settings:
    env = os.environ.get
    settings = Settings(
        database_url=os.environ["DATABASE_URL"],
        redis_url=env("REDIS_URL", "redis://redis:6379/0"),
        scorer_urls=_urls(env("SCORER_URLS", "v1=http://scorer-v1:8000")),
        primary_model=env("PRIMARY_MODEL", "v1"),
        candidate_model=env("CANDIDATE_MODEL") or None,
        canary_percent=int(env("CANARY_PERCENT", "0")),
        scorer_timeout_s=float(env("SCORER_TIMEOUT_S", "2")),
        rate_limit_capacity=int(env("RATE_LIMIT_CAPACITY", "20")),
        rate_limit_refill_per_s=float(env("RATE_LIMIT_REFILL_PER_S", "10")),
        retry_cache_ttl_s=int(env("RETRY_CACHE_TTL_S", "86400")),
    )
    # Fail at startup, not on the first request.
    for version in filter(None, (settings.primary_model, settings.candidate_model)):
        if version not in settings.scorer_urls:
            raise ValueError(f"no scorer URL for model {version}")
    if settings.candidate_model == settings.primary_model or not 0 <= settings.canary_percent <= 100:
        raise ValueError("CANDIDATE_MODEL must differ from PRIMARY_MODEL; CANARY_PERCENT must be 0-100")
    return settings
