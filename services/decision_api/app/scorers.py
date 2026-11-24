"""HTTP client for scorer containers (one container per model version)."""
import time

import httpx

from .metrics import SCORER_ERRORS, SCORER_SECONDS


class ScorerError(Exception):
    pass


class ScorerClient:
    def __init__(self, urls: dict[str, str], timeout_s: float):
        self.urls = urls
        self.http = httpx.Client(timeout=timeout_s)   # pooled keep-alive connections, thread-safe

    def score(self, version: str, features: dict, role: str = "primary") -> dict:
        """Returns the scorer response plus latency_ms; raises ScorerError on any failure."""
        url = self.urls.get(version)
        if url is None:
            SCORER_ERRORS.labels(version, role).inc()
            raise ScorerError(f"no scorer configured for {version}")
        started = time.perf_counter()
        try:
            response = self.http.post(f"{url}/score", json={"features": features})
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as error:
            SCORER_ERRORS.labels(version, role).inc()
            raise ScorerError(f"{version}: {type(error).__name__}: {error}") from error
        if body.get("model_version") != version:
            SCORER_ERRORS.labels(version, role).inc()
            raise ScorerError(f"{url} serves {body.get('model_version')}, expected {version}")
        elapsed = time.perf_counter() - started
        SCORER_SECONDS.labels(version, role).observe(elapsed)
        body["latency_ms"] = elapsed * 1000
        return body

    def healthy(self, version: str) -> bool:
        url = self.urls.get(version)
        try:
            return url is not None and self.http.get(f"{url}/health", timeout=0.5).status_code == 200
        except httpx.HTTPError:
            return False

    def close(self) -> None:
        self.http.close()
