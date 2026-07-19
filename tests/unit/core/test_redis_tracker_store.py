# -*- coding: utf-8 -*-
"""Redis TrackerStore 合约测试。"""

import pytest

from atguigu_ai.core.stores import RedisTrackerStore
from atguigu_ai.core.tracker import DialogueStateTracker
from atguigu_ai.shared.exceptions import TrackerStoreException


class RecordingRedis:
    """记录 Redis 调用的异步 fake 客户端。"""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}
        self.error = error

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        if self.error:
            raise self.error
        self.values[key] = value
        if ex is not None:
            self.expirations[key] = ex
        return True

    async def get(self, key: str):
        if self.error:
            raise self.error
        return self.values.get(key)

    async def delete(self, key: str) -> int:
        if self.error:
            raise self.error
        existed = key in self.values
        self.values.pop(key, None)
        self.expirations.pop(key, None)
        return 1 if existed else 0

    async def scan_iter(self, match: str):
        if self.error:
            raise self.error
        prefix = match.removesuffix("*")
        for key in list(self.values):
            if key.startswith(prefix):
                yield key


@pytest.mark.asyncio
async def test_save_and_retrieve_tracker_round_trip_with_ttl() -> None:
    redis = RecordingRedis()
    store = RedisTrackerStore(redis_client=redis, key_prefix="tracker:", ttl_seconds=60)
    tracker = DialogueStateTracker("account:abc")
    tracker.set_slot("order_id", "order-1")

    await store.save(tracker)
    restored = await store.retrieve("account:abc")

    assert restored is not None
    assert restored.sender_id == "account:abc"
    assert restored.get_slot("order_id") == "order-1"
    assert redis.expirations["tracker:account:abc"] == 60


@pytest.mark.asyncio
async def test_missing_delete_and_keys_are_scoped_to_prefix() -> None:
    redis = RecordingRedis()
    redis.values["other:account:abc"] = "{}"
    store = RedisTrackerStore(redis_client=redis, key_prefix="tracker:")

    assert await store.retrieve("missing") is None
    await store.save(DialogueStateTracker("account:abc"))
    await store.save(DialogueStateTracker("account:def"))

    assert sorted(await store.keys()) == ["account:abc", "account:def"]
    await store.delete("account:abc")
    assert await store.retrieve("account:abc") is None


@pytest.mark.asyncio
async def test_redis_errors_are_sanitized() -> None:
    redis = RecordingRedis(error=RuntimeError("internal redis outage detail"))
    store = RedisTrackerStore(redis_client=redis)

    with pytest.raises(TrackerStoreException) as captured:
        await store.save(DialogueStateTracker("account:abc"))

    assert str(captured.value) == "Tracker store is unavailable"
    assert captured.value.__cause__ is None
    assert "internal" not in str(captured.value)


def test_ttl_must_be_positive_integer() -> None:
    with pytest.raises(ValueError):
        RedisTrackerStore(redis_client=RecordingRedis(), ttl_seconds=0)
