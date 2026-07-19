# -*- coding: utf-8 -*-
"""TrackerStore 工厂与 Redis 配置合约测试。"""

from atguigu_ai.core.stores import RedisTrackerStore, create_tracker_store
from atguigu_ai.shared.config import TrackerStoreConfig


def test_factory_creates_redis_tracker_store() -> None:
    store = create_tracker_store(
        "redis",
        redis_client=object(),
        key_prefix="tracker:",
        ttl_seconds=60,
    )
    assert isinstance(store, RedisTrackerStore)


def test_tracker_store_config_parses_redis_options() -> None:
    config = TrackerStoreConfig.from_dict(
        {
            "type": "redis",
            "url": "${REDIS_URL:redis://127.0.0.1:6379/15}",
            "key_prefix": "tracker:",
            "ttl_seconds": 3600,
        }
    )
    assert config.type == "redis"
    assert config.url == "redis://127.0.0.1:6379/15"
    assert config.key_prefix == "tracker:"
    assert config.ttl_seconds == 3600
