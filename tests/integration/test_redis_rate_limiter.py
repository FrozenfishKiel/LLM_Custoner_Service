from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from atguigu_ai.rate_limit import RateLimitRule, RateLimitStoreUnavailable, RedisRateLimiter
from tests.integration.test_redis_session import CONTAINER, client, docker, wait_for_redis


@pytest_asyncio.fixture
async def redis_client():
    await wait_for_redis()
    redis = client()
    try:
        assert await redis.ping() is True
        await redis.flushdb()
        yield redis
    finally:
        try:
            await wait_for_redis()
            await redis.flushdb()
        finally:
            await redis.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_requests_never_exceed_limit(redis_client):
    limiter = RedisRateLimiter(redis_client)
    rule = RateLimitRule(name="chat.messages.account", scope="chat", limit=10, window_seconds=60)

    decisions = await asyncio.gather(*(limiter.check(rule, "account-1") for _ in range(80)))

    allowed = sum(1 for decision in decisions if decision.allowed)
    blocked = sum(1 for decision in decisions if not decision.allowed)
    print(f"rate_limit_concurrency_samples=80 allowed={allowed} blocked={blocked}")
    assert allowed == 10
    assert blocked == 70


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ttl_is_not_refreshed_inside_fixed_window(redis_client):
    limiter = RedisRateLimiter(redis_client)
    rule = RateLimitRule(name="auth.register.ip", scope="auth", limit=5, window_seconds=60)

    await limiter.check(rule, "127.0.0.1")
    keys = [key async for key in redis_client.scan_iter(match="rate:auth:auth.register.ip:*")]
    assert len(keys) == 1
    first_ttl = await redis_client.ttl(keys[0])
    await asyncio.sleep(1.1)
    await limiter.check(rule, "127.0.0.1")
    second_ttl = await redis_client.ttl(keys[0])

    print(f"rate_limit_ttl_first={first_ttl} second={second_ttl}")
    assert 1 <= second_ttl < first_ttl <= 60


@pytest.mark.integration
@pytest.mark.asyncio
async def test_subject_raw_value_never_appears_in_redis_keys(redis_client):
    limiter = RedisRateLimiter(redis_client)
    rule = RateLimitRule(name="auth.login.ip_email", scope="auth", limit=5, window_seconds=60)
    subject = "ip=127.0.0.1 email=secret-user@example.com token=raw-token"

    await limiter.check(rule, subject)

    keys = [key async for key in redis_client.scan_iter(match="rate:*")]
    assert len(keys) == 1
    key_dump = "\n".join(keys)
    assert "secret-user@example.com" not in key_dump
    assert "127.0.0.1" not in key_dump
    assert "raw-token" not in key_dump


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wrong_type_rate_limit_key_fails_closed_and_is_sanitized(redis_client):
    limiter = RedisRateLimiter(redis_client)
    rule = RateLimitRule(name="auth.register.ip", scope="auth", limit=5, window_seconds=60)
    await limiter.check(rule, "127.0.0.1")
    keys = [key async for key in redis_client.scan_iter(match="rate:auth:auth.register.ip:*")]
    assert len(keys) == 1
    await redis_client.delete(keys[0])
    await redis_client.lpush(keys[0], "wrong-type")

    with pytest.raises(RateLimitStoreUnavailable) as captured:
        await limiter.check(rule, "127.0.0.1")

    assert str(captured.value) == "Rate limit store is unavailable"
    assert captured.value.__cause__ is None


@pytest.mark.integration
def test_redis_rate_limiter_cleanup_leaves_db15_empty():
    result = docker("exec", CONTAINER, "redis-cli", "-n", "15", "DBSIZE")
    assert result.stdout.strip() == "0"
