from __future__ import annotations

import subprocess

import pytest
import pytest_asyncio

from atguigu_ai.core.stores import RedisTrackerStore
from atguigu_ai.core.tracker import DialogueStateTracker
from atguigu_ai.shared.exceptions import TrackerStoreException
from tests.integration.test_redis_session import (
    CONTAINER,
    assert_owned_container,
    client,
    docker,
    wait_for_redis,
)


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


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


@pytest.fixture(scope="module", autouse=True)
def cleanup_guard():
    yield
    assert_owned_container()
    result = docker("exec", CONTAINER, "redis-cli", "-n", "15", "DBSIZE")
    assert result.stdout.strip() == "0"


def docker_start_without_captured_pipes() -> None:
    subprocess.run(
        ["docker", "start", CONTAINER],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=60,
    )


async def test_redis_tracker_store_round_trip_against_real_redis(redis_client) -> None:
    store = RedisTrackerStore(
        redis_client=redis_client,
        key_prefix="tracker:",
        ttl_seconds=300,
    )
    tracker = DialogueStateTracker("account:abc")
    tracker.set_slot("order_id", "order-1")
    tracker.start_flow("query_order_detail")

    await store.save(tracker)
    restored = await store.retrieve("account:abc")

    assert restored is not None
    assert restored.sender_id == "account:abc"
    assert restored.get_slot("order_id") == "order-1"
    assert restored.active_flow == "query_order_detail"
    assert await redis_client.ttl("tracker:account:abc") > 0


async def test_redis_tracker_store_keys_are_prefix_scoped(redis_client) -> None:
    await redis_client.set("other:account:abc", "{}")
    store = RedisTrackerStore(redis_client=redis_client, key_prefix="tracker:")

    await store.save(DialogueStateTracker("account:abc"))
    await store.save(DialogueStateTracker("account:def"))

    assert sorted(await store.keys()) == ["account:abc", "account:def"]


async def test_redis_tracker_store_delete_removes_only_one_tracker(redis_client) -> None:
    store = RedisTrackerStore(redis_client=redis_client, key_prefix="tracker:")
    await store.save(DialogueStateTracker("account:abc"))
    await store.save(DialogueStateTracker("account:def"))

    await store.delete("account:abc")

    assert await store.retrieve("account:abc") is None
    assert await store.retrieve("account:def") is not None


async def test_redis_tracker_store_outage_is_sanitized() -> None:
    assert_owned_container()
    redis = client()
    try:
        assert await redis.ping() is True
        await redis.flushdb()
        store = RedisTrackerStore(redis_client=redis, key_prefix="tracker:")
        docker("stop", CONTAINER)
        with pytest.raises(TrackerStoreException) as captured:
            await store.retrieve("account:abc")
    finally:
        await redis.aclose()
        docker_start_without_captured_pipes()
        await wait_for_redis()
        recovered = client()
        try:
            await recovered.flushdb()
        finally:
            await recovered.aclose()

    assert str(captured.value) == "Tracker store is unavailable"
    assert captured.value.__cause__ is None
