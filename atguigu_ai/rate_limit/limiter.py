from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from redis.exceptions import RedisError


_SAFE_NAME = re.compile(r"^[a-z0-9_.-]+$")

_CHECK_SCRIPT = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local key_type = redis.call('TYPE', key)['ok']
if key_type ~= 'none' and key_type ~= 'string' then
  return redis.error_reply('invalid rate limit key type')
end
local count = redis.call('INCR', key)
if count == 1 then
  redis.call('EXPIRE', key, window)
end
local ttl = redis.call('TTL', key)
if ttl < 0 then
  return redis.error_reply('invalid rate limit ttl')
end
if count > limit then
  return {0, count, ttl}
end
return {1, count, ttl}
"""


class RateLimitStoreUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Rate limit store is unavailable")


@dataclass(frozen=True)
class RateLimitRule:
    name: str
    scope: str
    limit: int
    window_seconds: int

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _SAFE_NAME.match(self.name):
            raise ValueError("rate limit rule name is invalid")
        if not isinstance(self.scope, str) or not _SAFE_NAME.match(self.scope):
            raise ValueError("rate limit scope is invalid")
        if not _is_positive_int(self.limit):
            raise ValueError("rate limit must be positive")
        if not _is_positive_int(self.window_seconds):
            raise ValueError("rate limit window must be positive")


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int
    reset_after_seconds: int
    rule_name: str


def subject_digest(subject: object) -> str:
    if not isinstance(subject, str):
        raise ValueError("rate limit subject is required")
    clean = subject.strip()
    if not clean:
        raise ValueError("rate limit subject is required")
    return hashlib.sha256(clean.encode("utf-8")).hexdigest()


class RedisRateLimiter:
    def __init__(self, redis: Any, *, key_prefix: str = "rate:") -> None:
        self._redis = redis
        self._key_prefix = key_prefix

    async def check(self, rule: RateLimitRule, subject: str) -> RateLimitDecision:
        key = f"{self._key_prefix}{rule.scope}:{rule.name}:{subject_digest(subject)}"
        try:
            raw = await self._redis.eval(_CHECK_SCRIPT, 1, key, rule.limit, rule.window_seconds)
            allowed_raw, count_raw, ttl_raw = raw
            allowed = int(allowed_raw) == 1
            count = int(count_raw)
            ttl = int(ttl_raw)
        except (RedisError, TypeError, ValueError):
            raise RateLimitStoreUnavailable() from None
        if ttl < 0:
            raise RateLimitStoreUnavailable() from None
        return RateLimitDecision(
            allowed=allowed,
            limit=rule.limit,
            remaining=max(rule.limit - count, 0),
            retry_after_seconds=0 if allowed else ttl,
            reset_after_seconds=ttl,
            rule_name=rule.name,
        )


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
