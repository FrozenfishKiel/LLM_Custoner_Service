import asyncio
import hashlib
import json
import statistics
import subprocess
import time
from datetime import datetime

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from redis.exceptions import RedisError

from atguigu_ai.auth import AccountIdentity, AccountRole, AccountStatus, RedisSessionStore, SessionStoreUnavailable


REDIS_URL = "redis://127.0.0.1:6379/15"
CONTAINER = "llm-cs-redis"


def docker(*args, check=True, timeout=45):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True, timeout=timeout)


def assert_owned_container():
    inspected = json.loads(docker("inspect", CONTAINER).stdout)[0]
    labels = inspected["Config"].get("Labels") or {}
    bindings = inspected["HostConfig"]["PortBindings"]["6379/tcp"]
    mounts = inspected["Mounts"]
    assert labels.get("com.atguigu.project") == "llm_customer_service"
    assert labels.get("com.atguigu.purpose") == "redis-session-integration"
    assert inspected["Config"]["Image"] == "redis:7"
    assert bindings == [{"HostIp": "127.0.0.1", "HostPort": "6379"}]
    assert inspected["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped"
    assert any(item["Type"] == "volume" and item["Name"] == "llm-cs-redis-data" and item["Destination"] == "/data" for item in mounts)
    assert inspected["Config"]["Cmd"] == ["redis-server", "--appendonly", "yes", "--maxmemory-policy", "noeviction"]


async def wait_for_redis(timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        redis = client()
        try:
            if await redis.ping() is True:
                return time.monotonic()
        except RedisError:
            pass
        finally:
            await redis.aclose()
        await asyncio.sleep(0.25)
    raise AssertionError("Redis was not reachable through 127.0.0.1:6379 within 30 seconds")


async def recreate_owned_container():
    assert_owned_container()
    docker("rm", "-f", CONTAINER)
    created = docker(
        "create",
        "--name",
        CONTAINER,
        "--label",
        "com.atguigu.project=llm_customer_service",
        "--label",
        "com.atguigu.purpose=redis-session-integration",
        "--restart",
        "unless-stopped",
        "-p",
        "127.0.0.1:6379:6379",
        "-v",
        "llm-cs-redis-data:/data",
        "redis:7",
        "redis-server",
        "--appendonly",
        "yes",
        "--maxmemory-policy",
        "noeviction",
        timeout=45,
    )
    assert created.stdout.strip()
    assert_owned_container()
    started = docker("start", CONTAINER, timeout=45)
    assert started.stdout.strip() == CONTAINER
    return await wait_for_redis()


def client():
    return Redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=1,
    )


def identity(account_id="account-1"):
    return AccountIdentity(account_id, AccountRole.consumer, AccountStatus.active)


@pytest_asyncio.fixture
async def redis_client():
    assert_owned_container()
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
async def test_create_resolve_and_raw_token_never_appears_in_keys(redis_client):
    store = RedisSessionStore(redis_client, token_factory=lambda: "integration-raw-token")
    created = await store.create(identity())
    assert await store.resolve(created.token) == identity()
    all_keys = [key async for key in redis_client.scan_iter(match="*")]
    assert all("integration-raw-token" not in key for key in all_keys)
    assert any(hashlib.sha256(b"integration-raw-token").hexdigest() in key for key in all_keys)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_revoke_is_idempotent(redis_client):
    store = RedisSessionStore(redis_client)
    created = await store.create(identity())
    await store.revoke(created.token)
    await store.revoke(created.token)
    assert await store.resolve(created.token) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_revoke_all_invalidates_three_sessions_and_allows_a_new_one(redis_client):
    store = RedisSessionStore(redis_client)
    sessions = [await store.create(identity()) for _ in range(3)]
    await store.revoke_all("account-1")
    assert [await store.resolve(item.token) for item in sessions] == [None, None, None]
    replacement = await store.create(identity())
    assert await store.resolve(replacement.token) == identity()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["account_id", "role", "status", "generation", "issued_at", "last_seen_at"])
async def test_corrupt_or_missing_hash_fields_fail_closed(redis_client, field):
    store = RedisSessionStore(redis_client, token_factory=lambda: f"corrupt-{field}")
    created = await store.create(identity())
    digest = hashlib.sha256(created.token.encode()).hexdigest()
    key = f"auth:session:{digest}"
    await redis_client.hdel(key, field)
    assert await store.resolve(created.token) is None
    assert await redis_client.exists(key) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wrong_type_session_key_is_deleted_and_resolves_none(redis_client):
    token = "wrong-type-session"
    digest = hashlib.sha256(token.encode()).hexdigest()
    key = f"auth:session:{digest}"
    await redis_client.set(key, "corrupt")

    assert await RedisSessionStore(redis_client).resolve(token) is None
    assert await redis_client.exists(key) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_generation_mismatch_fails_closed(redis_client):
    store = RedisSessionStore(redis_client)
    created = await store.create(identity())
    current = await redis_client.get("auth:session_generation:account-1")
    replacement = "0" * 32 if current != "0" * 32 else "1" * 32
    await redis_client.set("auth:session_generation:account-1", replacement)
    assert await store.resolve(created.token) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_missing_generation_deletes_session_and_index_entry(redis_client):
    store = RedisSessionStore(redis_client)
    created = await store.create(identity())
    digest = hashlib.sha256(created.token.encode()).hexdigest()
    await redis_client.delete("auth:session_generation:account-1")
    assert await store.resolve(created.token) is None
    assert await redis_client.exists(f"auth:session:{digest}") == 0
    assert await redis_client.exists("auth:account_sessions:account-1") == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_refreshes_short_remaining_ttl(redis_client):
    store = RedisSessionStore(redis_client, ttl_seconds=4, refresh_threshold_seconds=2)
    created = await store.create(identity())
    digest = hashlib.sha256(created.token.encode()).hexdigest()
    await asyncio.sleep(2.2)
    assert await store.resolve(created.token) == identity()
    ttl = await redis_client.ttl(f"auth:session:{digest}")
    assert 3 <= ttl <= 4


@pytest.mark.integration
@pytest.mark.asyncio
async def test_inactive_session_and_single_session_index_expire(redis_client):
    store = RedisSessionStore(redis_client, ttl_seconds=2, refresh_threshold_seconds=0)
    created = await store.create(identity())
    digest = hashlib.sha256(created.token.encode()).hexdigest()
    session_key = f"auth:session:{digest}"
    index_key = "auth:account_sessions:account-1"
    assert await redis_client.exists(session_key) == 1
    assert await redis_client.exists(index_key) == 1
    await asyncio.sleep(2.2)
    assert await store.resolve(created.token) is None
    assert await redis_client.exists(session_key) == 0
    assert await redis_client.exists(index_key) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_invalid_tokens_do_not_call_redis(redis_client):
    class CountingRedis:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.eval_calls = 0

        async def eval(self, *args):
            self.eval_calls += 1
            return await self.wrapped.eval(*args)

    counting = CountingRedis(redis_client)
    store = RedisSessionStore(counting)
    assert await store.resolve("") is None
    assert await store.resolve("x" * 513) is None
    assert await store.revoke("") is None
    assert await store.revoke("x" * 513) is None
    assert counting.eval_calls == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_invalid_clock_does_not_call_redis_or_delete_session(redis_client):
    healthy_store = RedisSessionStore(redis_client)
    created = await healthy_store.create(identity())
    digest = hashlib.sha256(created.token.encode()).hexdigest()

    class CountingRedis:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.eval_calls = 0

        async def eval(self, *args):
            self.eval_calls += 1
            return await self.wrapped.eval(*args)

    counting = CountingRedis(redis_client)
    bad_clock_store = RedisSessionStore(counting, clock=lambda: datetime(2026, 7, 18))
    with pytest.raises(ValueError, match="timezone-aware"):
        await bad_clock_store.resolve(created.token)
    assert counting.eval_calls == 0
    assert await redis_client.exists(f"auth:session:{digest}") == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_revoke_all_remains_constant_time_with_1000_sessions(redis_client):
    store = RedisSessionStore(redis_client, ttl_seconds=300, refresh_threshold_seconds=30)
    low_latencies = []
    for _ in range(20):
        await store.create(identity())
        started = time.perf_counter()
        await store.revoke_all("account-1")
        low_latencies.append(time.perf_counter() - started)

    sessions = [await store.create(identity()) for _ in range(1000)]
    started = time.perf_counter()
    await store.revoke_all("account-1")
    high_latency = time.perf_counter() - started
    print(f"revoke_all_1000_seconds={high_latency:.6f}; low_median_seconds={statistics.median(low_latencies):.6f}")

    assert high_latency <= 0.250
    assert high_latency <= max(0.100, statistics.median(low_latencies) * 10)
    assert await redis_client.exists("auth:account_sessions:account-1") == 0
    assert await redis_client.exists(f"auth:session:{hashlib.sha256(sessions[0].token.encode()).hexdigest()}") == 1
    assert await store.resolve(sessions[0].token) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_revoke_all_overlap_is_safe_for_50_iterations(redis_client):
    store = RedisSessionStore(redis_client)
    for _ in range(50):
        created, _ = await asyncio.gather(store.create(identity()), store.revoke_all("account-1"))
        await store.revoke_all("account-1")
        assert await store.resolve(created.token) is None
        replacement = await store.create(identity())
        assert await store.resolve(replacement.token) == identity()
        await store.revoke_all("account-1")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_revoke_all_overlap_is_safe_for_50_iterations(redis_client):
    store = RedisSessionStore(redis_client)
    for _ in range(50):
        created = await store.create(identity())
        await asyncio.gather(store.resolve(created.token), store.revoke_all("account-1"))
        assert await store.resolve(created.token) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_aof_session_survives_container_recreation(redis_client):
    store = RedisSessionStore(redis_client, ttl_seconds=60, refresh_threshold_seconds=10)
    created = await store.create(identity())
    await asyncio.sleep(1.2)
    started = time.monotonic()
    await recreate_owned_container()
    recreate_seconds = time.monotonic() - started
    print(f"aof_recreate_seconds={recreate_seconds:.6f}")
    restarted = client()
    try:
        assert await RedisSessionStore(restarted, ttl_seconds=60, refresh_threshold_seconds=10).resolve(created.token) == identity()
        assert recreate_seconds <= 30
    finally:
        await restarted.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_redis_failure_is_sanitized_and_recovers(redis_client):
    store = RedisSessionStore(redis_client)
    assert_owned_container()
    docker("stop", CONTAINER)
    try:
        with pytest.raises(SessionStoreUnavailable, match="^Session store is unavailable$"):
            await store.resolve("failure-token")
    finally:
        await recreate_owned_container()
    recovered = client()
    try:
        await recovered.flushdb()
        recovered_store = RedisSessionStore(recovered)
        created = await recovered_store.create(identity())
        assert await recovered_store.resolve(created.token) == identity()
    finally:
        await recovered.flushdb()
        await recovered.aclose()
