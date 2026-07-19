import pytest
from redis.exceptions import ConnectionError, ResponseError, TimeoutError

from atguigu_ai.rate_limit import (
    RateLimitRule,
    RateLimitStoreUnavailable,
    RedisRateLimiter,
    subject_digest,
)


class RecordingRedis:
    def __init__(self, replies=None, error=None):
        self.replies = list(replies or [])
        self.error = error
        self.calls = []

    async def eval(self, script, numkeys, *values):
        self.calls.append((script, numkeys, values))
        if self.error is not None:
            raise self.error
        return self.replies.pop(0)


@pytest.mark.asyncio
async def test_first_request_is_allowed_and_key_contains_only_digest():
    redis = RecordingRedis([[1, 1, 60]])
    limiter = RedisRateLimiter(redis, key_prefix="rate:")
    rule = RateLimitRule(name="auth.login.ip_email", scope="auth", limit=2, window_seconds=60)

    decision = await limiter.check(rule, "ip=127.0.0.1 email=User@Example.COM")

    assert decision.allowed is True
    assert decision.limit == 2
    assert decision.remaining == 1
    assert decision.retry_after_seconds == 0
    assert decision.reset_after_seconds == 60
    assert decision.rule_name == "auth.login.ip_email"
    assert len(redis.calls) == 1
    script, numkeys, values = redis.calls[0]
    assert "INCR" in script
    assert numkeys == 1
    key = values[0]
    assert key.startswith("rate:auth:auth.login.ip_email:")
    assert "User@Example.COM" not in key
    assert "127.0.0.1" not in key


@pytest.mark.asyncio
async def test_limit_exceeded_returns_retry_after_from_ttl():
    redis = RecordingRedis([[1, 1, 60], [0, 2, 58]])
    limiter = RedisRateLimiter(redis)
    rule = RateLimitRule(name="chat.messages.account", scope="chat", limit=1, window_seconds=60)

    allowed = await limiter.check(rule, "account-1")
    blocked = await limiter.check(rule, "account-1")

    assert allowed.allowed is True
    assert blocked.allowed is False
    assert blocked.remaining == 0
    assert blocked.retry_after_seconds == 58
    assert blocked.reset_after_seconds == 58


@pytest.mark.parametrize("reply", [[1, 1, -1], ["bad"], None])
@pytest.mark.asyncio
async def test_invalid_redis_replies_are_sanitized(reply):
    limiter = RedisRateLimiter(RecordingRedis([reply]))
    rule = RateLimitRule(name="auth.register.ip", scope="auth", limit=1, window_seconds=60)

    with pytest.raises(RateLimitStoreUnavailable) as captured:
        await limiter.check(rule, "127.0.0.1")

    assert str(captured.value) == "Rate limit store is unavailable"
    assert captured.value.__cause__ is None


@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("redis://user:secret@host"),
        TimeoutError("raw-token"),
        ResponseError("script source"),
    ],
)
@pytest.mark.asyncio
async def test_redis_errors_are_sanitized(error):
    limiter = RedisRateLimiter(RecordingRedis(error=error))
    rule = RateLimitRule(name="auth.register.ip", scope="auth", limit=1, window_seconds=60)

    with pytest.raises(RateLimitStoreUnavailable) as captured:
        await limiter.check(rule, "127.0.0.1")

    assert str(captured.value) == "Rate limit store is unavailable"
    assert captured.value.__cause__ is None
    assert "redis" not in str(captured.value).lower()
    assert "secret" not in str(captured.value)
    assert "raw-token" not in str(captured.value)
    assert "script source" not in str(captured.value)


@pytest.mark.parametrize(
    ("name", "scope", "limit", "window_seconds"),
    [
        ("Auth.Login", "auth", 1, 60),
        ("auth login", "auth", 1, 60),
        ("auth.login", "Auth", 1, 60),
        ("auth.login", "auth", 0, 60),
        ("auth.login", "auth", 1, 0),
        ("auth.login", "auth", True, 60),
        ("auth.login", "auth", 1, False),
    ],
)
def test_rule_rejects_invalid_configuration(name, scope, limit, window_seconds):
    with pytest.raises(ValueError):
        RateLimitRule(name=name, scope=scope, limit=limit, window_seconds=window_seconds)


@pytest.mark.parametrize("subject", ["", "   ", None, 42])
def test_subject_digest_rejects_invalid_subjects(subject):
    with pytest.raises(ValueError):
        subject_digest(subject)


def test_subject_digest_never_returns_raw_subject():
    digest = subject_digest("user@example.com")

    assert digest != "user@example.com"
    assert len(digest) == 64
    assert all(char in "0123456789abcdef" for char in digest)
