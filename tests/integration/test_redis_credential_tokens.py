import asyncio
import hashlib
import statistics
import threading
import time
from datetime import datetime, timezone

import psutil
import pytest
import pytest_asyncio
from redis.asyncio import Redis

from atguigu_ai.auth import (
    CredentialTokenPurpose,
    CredentialTokenStoreUnavailable,
    PasswordHasher,
    PasswordHashingOverloaded,
    RedisCredentialTokenStore,
)
from tests.integration.test_redis_session import (
    CONTAINER,
    REDIS_URL,
    assert_owned_container,
    client,
    docker,
    recreate_owned_container,
    wait_for_redis,
)


pytestmark = pytest.mark.integration

ACCOUNT_A = "00000000-0000-0000-0000-000000000001"
ACCOUNT_B = "00000000-0000-0000-0000-000000000002"
TOKEN_A = "A" * 43
TOKEN_B = "B" * 43
TOKEN_C = "C" * 43


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def token_key(purpose: CredentialTokenPurpose, token: str) -> str:
    return f"auth:{purpose.value}:{digest(token)}"


def current_key(purpose: CredentialTokenPurpose, account_id: str) -> str:
    return f"auth:credential_token_current:{purpose.value}:{account_id}"


def fixed_clock() -> datetime:
    return datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


def store(redis, tokens=None, ttl=30):
    iterator = iter(tokens or [TOKEN_A])
    return RedisCredentialTokenStore(
        redis,
        ttl_seconds={
            CredentialTokenPurpose.verify_email: ttl,
            CredentialTokenPurpose.reset_password: ttl,
        },
        token_factory=lambda: next(iterator),
        clock=fixed_clock,
    )


def generated_store(redis, ttl=30):
    return RedisCredentialTokenStore(
        redis,
        ttl_seconds={
            CredentialTokenPurpose.verify_email: ttl,
            CredentialTokenPurpose.reset_password: ttl,
        },
        clock=fixed_clock,
    )


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


@pytest.mark.asyncio
async def test_issue_consume_once_and_raw_token_never_appears_in_redis(redis_client):
    token_store = store(redis_client, [TOKEN_A])
    issued = await token_store.issue(ACCOUNT_A, CredentialTokenPurpose.verify_email)
    keys_before_consume = [key async for key in redis_client.scan_iter(match="auth:*")]

    assert issued.token == TOKEN_A
    assert TOKEN_A not in repr(keys_before_consume)
    assert any(digest(TOKEN_A) in key for key in keys_before_consume)
    assert await token_store.consume(TOKEN_A, CredentialTokenPurpose.verify_email) == ACCOUNT_A
    assert await token_store.consume(TOKEN_A, CredentialTokenPurpose.verify_email) is None
    assert await redis_client.dbsize() == 0


@pytest.mark.asyncio
async def test_replacement_invalidates_old_token_and_preserves_other_purpose(redis_client):
    token_store = store(redis_client, [TOKEN_A, TOKEN_B, TOKEN_C])
    old_verify = await token_store.issue(ACCOUNT_A, CredentialTokenPurpose.verify_email)
    reset = await token_store.issue(ACCOUNT_A, CredentialTokenPurpose.reset_password)
    new_verify = await token_store.issue(ACCOUNT_A, CredentialTokenPurpose.verify_email)

    assert old_verify.token == TOKEN_A
    assert reset.token == TOKEN_B
    assert new_verify.token == TOKEN_C
    assert await token_store.consume(TOKEN_A, CredentialTokenPurpose.verify_email) is None
    assert await token_store.consume(TOKEN_B, CredentialTokenPurpose.reset_password) == ACCOUNT_A
    assert await token_store.consume(TOKEN_C, CredentialTokenPurpose.verify_email) == ACCOUNT_A


@pytest.mark.asyncio
async def test_expired_token_and_index_fail_closed(redis_client):
    token_store = store(redis_client, [TOKEN_A], ttl=1)
    await token_store.issue(ACCOUNT_A, CredentialTokenPurpose.verify_email)
    await asyncio.sleep(1.2)

    assert await token_store.consume(TOKEN_A, CredentialTokenPurpose.verify_email) is None
    assert await redis_client.exists(token_key(CredentialTokenPurpose.verify_email, TOKEN_A)) == 0
    assert await redis_client.exists(current_key(CredentialTokenPurpose.verify_email, ACCOUNT_A)) == 0


@pytest.mark.asyncio
async def test_raw_bytes_client_consumes_to_string_account_id():
    assert_owned_container()
    redis = Redis.from_url(
        REDIS_URL,
        decode_responses=False,
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    try:
        await redis.flushdb()
        token_store = store(redis, [TOKEN_A])
        await token_store.issue(ACCOUNT_A, CredentialTokenPurpose.reset_password)
        assert await token_store.consume(TOKEN_A, CredentialTokenPurpose.reset_password) == ACCOUNT_A
    finally:
        await redis.flushdb()
        await redis.aclose()


@pytest.mark.asyncio
async def test_wrong_type_token_key_is_deleted(redis_client):
    key = token_key(CredentialTokenPurpose.verify_email, TOKEN_A)
    await redis_client.lpush(key, "wrong")

    assert await generated_store(redis_client).consume(TOKEN_A, CredentialTokenPurpose.verify_email) is None
    assert await redis_client.exists(key) == 0


@pytest.mark.asyncio
async def test_missing_wrong_type_and_no_ttl_current_index_fail_closed(redis_client):
    token_store = store(redis_client, [TOKEN_A, TOKEN_B, TOKEN_C])
    missing = await token_store.issue(ACCOUNT_A, CredentialTokenPurpose.verify_email)
    await redis_client.delete(current_key(CredentialTokenPurpose.verify_email, ACCOUNT_A))
    assert await token_store.consume(missing.token, CredentialTokenPurpose.verify_email) is None
    assert await redis_client.exists(token_key(CredentialTokenPurpose.verify_email, missing.token)) == 0

    wrong_type = await token_store.issue(ACCOUNT_A, CredentialTokenPurpose.verify_email)
    index_key = current_key(CredentialTokenPurpose.verify_email, ACCOUNT_A)
    await redis_client.delete(index_key)
    await redis_client.lpush(index_key, "wrong")
    assert await token_store.consume(wrong_type.token, CredentialTokenPurpose.verify_email) is None
    assert await redis_client.exists(index_key) == 0

    no_ttl = await token_store.issue(ACCOUNT_A, CredentialTokenPurpose.verify_email)
    await redis_client.set(index_key, digest(no_ttl.token))
    assert await token_store.consume(no_ttl.token, CredentialTokenPurpose.verify_email) is None
    assert await redis_client.exists(index_key) == 0


@pytest.mark.asyncio
async def test_corrupt_cross_account_index_does_not_delete_other_account_token(redis_client):
    token_store = store(redis_client, [TOKEN_A, TOKEN_B])
    other = await token_store.issue(ACCOUNT_B, CredentialTokenPurpose.verify_email)
    await redis_client.set(
        current_key(CredentialTokenPurpose.verify_email, ACCOUNT_A),
        digest(other.token),
        ex=30,
    )

    mine = await token_store.issue(ACCOUNT_A, CredentialTokenPurpose.verify_email)

    assert mine.token == TOKEN_B
    assert await redis_client.get(token_key(CredentialTokenPurpose.verify_email, other.token)) == ACCOUNT_B
    assert await token_store.consume(other.token, CredentialTokenPurpose.verify_email) == ACCOUNT_B


@pytest.mark.asyncio
async def test_forced_digest_collision_across_purposes_uses_four_attempts_without_overwrite(redis_client):
    first_store = store(redis_client, [TOKEN_A])
    await first_store.issue(ACCOUNT_A, CredentialTokenPurpose.verify_email)
    colliding_store = store(redis_client, [TOKEN_A, TOKEN_A, TOKEN_A, TOKEN_A])

    with pytest.raises(CredentialTokenStoreUnavailable, match="^Unable to allocate credential token$"):
        await colliding_store.issue(ACCOUNT_B, CredentialTokenPurpose.reset_password)

    assert await first_store.consume(TOKEN_A, CredentialTokenPurpose.verify_email) == ACCOUNT_A


@pytest.mark.asyncio
async def test_concurrent_consume_has_exactly_one_winner(redis_client):
    token_store = store(redis_client, [TOKEN_A])
    await token_store.issue(ACCOUNT_A, CredentialTokenPurpose.reset_password)

    results = await asyncio.gather(
        *[
            token_store.consume(TOKEN_A, CredentialTokenPurpose.reset_password)
            for _ in range(50)
        ]
    )

    assert results.count(ACCOUNT_A) == 1
    assert results.count(None) == 49


@pytest.mark.asyncio
async def test_concurrent_issue_leaves_exactly_one_current_token(redis_client):
    token_store = generated_store(redis_client)
    issued = await asyncio.gather(
        *[
            token_store.issue(ACCOUNT_A, CredentialTokenPurpose.verify_email)
            for _ in range(50)
        ]
    )
    results = [
        await token_store.consume(item.token, CredentialTokenPurpose.verify_email)
        for item in issued
    ]

    assert results.count(ACCOUNT_A) == 1
    assert results.count(None) == 49


@pytest.mark.asyncio
async def test_issue_vs_consume_linearizes_to_valid_final_state(redis_client):
    token_store = store(redis_client, [TOKEN_A, TOKEN_B])
    old = await token_store.issue(ACCOUNT_A, CredentialTokenPurpose.verify_email)
    consume_old, new = await asyncio.gather(
        token_store.consume(old.token, CredentialTokenPurpose.verify_email),
        token_store.issue(ACCOUNT_A, CredentialTokenPurpose.verify_email),
    )

    assert consume_old in {ACCOUNT_A, None}
    assert await token_store.consume(new.token, CredentialTokenPurpose.verify_email) == ACCOUNT_A
    assert await token_store.consume(old.token, CredentialTokenPurpose.verify_email) is None


@pytest.mark.asyncio
async def test_redis_failure_is_sanitized_and_recovers(redis_client):
    token_store = generated_store(redis_client)
    assert_owned_container()
    docker("stop", CONTAINER)
    try:
        with pytest.raises(CredentialTokenStoreUnavailable, match="^Credential token store is unavailable$"):
            await token_store.consume(TOKEN_A, CredentialTokenPurpose.verify_email)
    finally:
        await recreate_owned_container()
    recovered = client()
    try:
        await recovered.flushdb()
        recovered_store = store(recovered, [TOKEN_A])
        issued = await recovered_store.issue(ACCOUNT_A, CredentialTokenPurpose.verify_email)
        assert await recovered_store.consume(issued.token, CredentialTokenPurpose.verify_email) == ACCOUNT_A
    finally:
        await recovered.flushdb()
        await recovered.aclose()


@pytest.mark.asyncio
async def test_aof_unconsumed_token_survives_owned_container_recreation(redis_client):
    token_store = store(redis_client, [TOKEN_A], ttl=60)
    issued = await token_store.issue(ACCOUNT_A, CredentialTokenPurpose.verify_email)
    await asyncio.sleep(1.2)
    started = time.monotonic()
    await recreate_owned_container()
    recreate_seconds = time.monotonic() - started
    print(f"credential_token_aof_recreate_seconds={recreate_seconds:.6f}")
    restarted = client()
    try:
        assert await generated_store(restarted, ttl=60).consume(issued.token, CredentialTokenPurpose.verify_email) == ACCOUNT_A
        assert recreate_seconds <= 30
    finally:
        await restarted.flushdb()
        await restarted.aclose()


@pytest.mark.load
@pytest.mark.asyncio
async def test_argon2_load_bounds_and_overload(monkeypatch):
    hasher = PasswordHasher()
    encoded = await hasher.hash("load-password-0")

    hash_samples = []
    for index in range(10):
        started = time.perf_counter()
        await hasher.hash(f"load-password-{index}")
        hash_samples.append(time.perf_counter() - started)

    verify_samples = []
    for _ in range(10):
        started = time.perf_counter()
        assert await hasher.verify(encoded, "load-password-0") is True
        verify_samples.append(time.perf_counter() - started)

    ticks = 0
    done = False

    async def heartbeat():
        nonlocal ticks
        while not done:
            await asyncio.sleep(0.01)
            ticks += 1

    async def measured_verify():
        started = time.perf_counter()
        result = await hasher.verify(encoded, "load-password-0")
        return result, time.perf_counter() - started

    started = time.perf_counter()
    heartbeat_task = asyncio.create_task(heartbeat())
    concurrent = await asyncio.gather(*[measured_verify() for _ in range(20)])
    done = True
    await heartbeat_task
    concurrent_wall = time.perf_counter() - started
    concurrent_results = [item[0] for item in concurrent]
    concurrent_samples = [item[1] for item in concurrent]
    rss_mb = psutil.Process().memory_info().rss / (1024 * 1024)

    assert all(concurrent_results)
    assert ticks > 0
    assert statistics.quantiles(concurrent_samples, n=20)[18] <= 1.0
    print(
        "argon2_load "
        f"hash_p50_ms={statistics.median(hash_samples) * 1000:.3f} "
        f"hash_p95_ms={statistics.quantiles(hash_samples, n=20)[18] * 1000:.3f} "
        f"verify_p50_ms={statistics.median(verify_samples) * 1000:.3f} "
        f"verify_p95_ms={statistics.quantiles(verify_samples, n=20)[18] * 1000:.3f} "
        f"concurrent_p50_ms={statistics.median(concurrent_samples) * 1000:.3f} "
        f"concurrent_p95_ms={statistics.quantiles(concurrent_samples, n=20)[18] * 1000:.3f} "
        f"concurrent_wall_ms={concurrent_wall * 1000:.3f} "
        f"rss_mb={rss_mb:.1f}"
    )

    entered = 0
    maximum_running = 0
    lock = threading.Lock()
    release = threading.Event()

    def blocked_verify(encoded_hash, password):
        nonlocal entered, maximum_running
        with lock:
            entered += 1
            maximum_running = max(maximum_running, entered)
        release.wait(timeout=5)
        with lock:
            entered -= 1
        return True

    controlled_hashers = [PasswordHasher(), PasswordHasher()]
    for item in controlled_hashers:
        monkeypatch.setattr(item._argon2, "verify", blocked_verify)
    tasks = [
        asyncio.create_task(controlled_hashers[index % 2].verify(encoded, "load-password-0"))
        for index in range(20)
    ]
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            if maximum_running == 4:
                break
        assert maximum_running == 4
        with pytest.raises(PasswordHashingOverloaded):
            await asyncio.wait_for(
                PasswordHasher().verify(encoded, "load-password-0"),
                timeout=0.2,
            )
    finally:
        release.set()
        assert await asyncio.wait_for(asyncio.gather(*tasks), timeout=6) == [True] * 20


@pytest.mark.load
@pytest.mark.asyncio
async def test_redis_issue_consume_quantitative_bounds(redis_client):
    token_store = generated_store(redis_client, ttl=60)
    samples = []
    started_all = time.perf_counter()
    for index in range(300):
        started = time.perf_counter()
        issued = await token_store.issue(f"acct-{index}", CredentialTokenPurpose.verify_email)
        assert await token_store.consume(issued.token, CredentialTokenPurpose.verify_email) == f"acct-{index}"
        samples.append(time.perf_counter() - started)
    wall = time.perf_counter() - started_all
    p50 = statistics.median(samples)
    p95 = statistics.quantiles(samples, n=20)[18]
    print(
        f"credential_token_redis_samples=300 p50_ms={p50 * 1000:.3f} "
        f"p95_ms={p95 * 1000:.3f} ops_per_second={300 / wall:.1f}"
    )

    replacement_store = generated_store(redis_client, ttl=60)
    for _ in range(1000):
        await replacement_store.issue(ACCOUNT_A, CredentialTokenPurpose.reset_password)
    started = time.perf_counter()
    await replacement_store.issue(ACCOUNT_A, CredentialTokenPurpose.reset_password)
    final_issue = time.perf_counter() - started
    print(f"credential_token_replacement_1000_final_issue_ms={final_issue * 1000:.3f}")
    assert final_issue <= 0.250
