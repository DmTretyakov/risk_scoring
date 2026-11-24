"""Redis: per-source rate limiting and a cache of exact retries.

Redis is an accelerator, never the source of truth. If it is unreachable,
requests are still processed from Postgres (rate limiting fails open and is
logged), so a cache outage cannot stop credit decisions.
"""
import hashlib
import json
import logging

import redis

from .metrics import REDIS_ERRORS

log = logging.getLogger("decision-api")

# Token bucket, atomic inside Redis so parallel requests on several API replicas
# cannot overspend. Uses Redis server time: no dependence on replica clocks.
# Returns {allowed (0/1), milliseconds until the next token}.
_TOKEN_BUCKET = """
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1]) or capacity
local ts = tonumber(state[2]) or now
tokens = math.min(capacity, tokens + (now - ts) * rate / 1000)
local allowed, wait_ms = 0, 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  wait_ms = math.ceil((1 - tokens) * 1000 / rate)
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', now)
redis.call('PEXPIRE', KEYS[1], math.ceil(capacity * 1000 / rate) + 1000)
return {allowed, wait_ms}
"""


def _key(prefix: str, *parts: str) -> str:
    return prefix + hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


class RedisLayer:
    def __init__(self, url: str, capacity: int, refill_per_s: float, retry_ttl_s: int):
        self.client = redis.Redis.from_url(url, socket_timeout=0.2, socket_connect_timeout=0.2)
        self.capacity, self.refill_per_s, self.retry_ttl_s = capacity, refill_per_s, retry_ttl_s
        self._bucket = self.client.register_script(_TOKEN_BUCKET)

    def allow(self, source_system: str) -> tuple[bool, float]:
        """(allowed, seconds to wait). Fails open when Redis is down."""
        try:
            allowed, wait_ms = self._bucket(keys=[_key("ratelimit:", source_system)],
                                            args=[self.capacity, self.refill_per_s])
            return bool(allowed), wait_ms / 1000
        except redis.RedisError as error:
            REDIS_ERRORS.labels("rate_limit").inc()
            log.warning(json.dumps({"event": "redis_unavailable", "op": "rate_limit", "error": str(error)}))
            return True, 0.0

    def get_retry(self, source_system: str, external_id: str) -> dict | None:
        """Latest {request_hash, response} for this application ID, if cached."""
        try:
            raw = self.client.get(_key("retry:", source_system, external_id))
            return json.loads(raw) if raw else None
        except redis.RedisError as error:
            REDIS_ERRORS.labels("get_retry").inc()
            log.warning(json.dumps({"event": "redis_unavailable", "op": "get_retry", "error": str(error)}))
            return None

    def put_retry(self, source_system: str, external_id: str, request_hash: str, response: dict) -> None:
        """Called only after the Postgres commit; overwritten when a correction creates a new revision."""
        try:
            self.client.set(_key("retry:", source_system, external_id),
                            json.dumps({"request_hash": request_hash, "response": response}), ex=self.retry_ttl_s)
        except redis.RedisError as error:
            REDIS_ERRORS.labels("put_retry").inc()
            log.warning(json.dumps({"event": "redis_unavailable", "op": "put_retry", "error": str(error)}))

    def healthy(self) -> bool:
        try:
            return bool(self.client.ping())
        except redis.RedisError:
            return False

    def close(self) -> None:
        self.client.close()
