# -*- coding: utf-8 -*-
"""
redis_store - Redis Tracker 存储

将 DialogueStateTracker 序列化为 JSON 后保存到 Redis。
"""

import json
import re
from typing import Any, Iterable, Optional, Text

from atguigu_ai.core.domain import Domain
from atguigu_ai.core.stores.tracker_store import TrackerStore
from atguigu_ai.core.tracker import DialogueStateTracker
from atguigu_ai.shared.exceptions import (
    TrackerSerializationError,
    TrackerStoreConnectionError,
    TrackerStoreException,
)

SANITIZED_ERROR_MESSAGE = "Tracker store is unavailable"
_REDIS_GLOB_CHARS = re.compile(r"[*?\[\]]")


class RedisTrackerStore(TrackerStore):
    """Redis 版 TrackerStore。"""

    def __init__(
        self,
        domain: Optional[Domain] = None,
        redis_client: Any | None = None,
        url: str | None = None,
        host: str = "127.0.0.1",
        port: int = 6379,
        db: int | str = 15,
        username: str | None = None,
        password: str | None = None,
        key_prefix: str = "tracker:",
        ttl_seconds: int | None = None,
    ) -> None:
        super().__init__(domain)

        if ttl_seconds is not None and (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or ttl_seconds <= 0
        ):
            raise ValueError("ttl_seconds must be a positive integer")
        if not isinstance(key_prefix, str) or not key_prefix:
            raise ValueError("key_prefix must be a non-empty string")
        if _REDIS_GLOB_CHARS.search(key_prefix):
            raise ValueError("key_prefix must not contain Redis glob characters")

        self._redis_client = redis_client
        self.url = url
        self.host = host
        self.port = int(port)
        self.db = int(db)
        self.username = username
        self.password = password
        self.key_prefix = key_prefix
        self.ttl_seconds = ttl_seconds

    def _key(self, sender_id: Text) -> str:
        return f"{self.key_prefix}{sender_id}"

    def _client(self) -> Any:
        if self._redis_client is not None:
            return self._redis_client

        try:
            from redis.asyncio import Redis

            credential_kwargs = {
                key: value
                for key, value in {
                    "username": self.username,
                    "password": self.password,
                }.items()
                if value is not None
            }
            if self.url:
                self._redis_client = Redis.from_url(self.url, **credential_kwargs)
            else:
                self._redis_client = Redis(
                    host=self.host,
                    port=self.port,
                    db=self.db,
                    **credential_kwargs,
                )
            return self._redis_client
        except Exception:
            raise TrackerStoreConnectionError(SANITIZED_ERROR_MESSAGE) from None

    def _domain_slots(self) -> Any:
        return self.domain.slots if self.domain else None

    def _decode(self, value: bytes | str) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return value

    async def save(self, tracker: DialogueStateTracker) -> None:
        try:
            value = json.dumps(tracker.to_dict(), ensure_ascii=False)
            await self._client().set(
                self._key(tracker.sender_id),
                value,
                ex=self.ttl_seconds,
            )
        except (TypeError, ValueError):
            raise TrackerSerializationError(SANITIZED_ERROR_MESSAGE) from None
        except TrackerStoreException:
            raise
        except Exception:
            raise TrackerStoreException(SANITIZED_ERROR_MESSAGE) from None

    async def retrieve(self, sender_id: Text) -> Optional[DialogueStateTracker]:
        try:
            value = await self._client().get(self._key(sender_id))
        except TrackerStoreException:
            raise
        except Exception:
            raise TrackerStoreException(SANITIZED_ERROR_MESSAGE) from None

        if value is None:
            return None

        try:
            tracker_data = json.loads(self._decode(value))
            if not isinstance(tracker_data, dict):
                raise TrackerSerializationError(SANITIZED_ERROR_MESSAGE)
            return DialogueStateTracker.from_dict(tracker_data, self._domain_slots())
        except TrackerSerializationError:
            raise TrackerSerializationError(SANITIZED_ERROR_MESSAGE) from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise TrackerSerializationError(SANITIZED_ERROR_MESSAGE) from None
        except (TypeError, ValueError, KeyError, AttributeError):
            raise TrackerSerializationError(SANITIZED_ERROR_MESSAGE) from None
        except Exception:
            raise TrackerSerializationError(SANITIZED_ERROR_MESSAGE) from None

    async def delete(self, sender_id: Text) -> None:
        try:
            await self._client().delete(self._key(sender_id))
        except TrackerStoreException:
            raise
        except Exception:
            raise TrackerStoreException(SANITIZED_ERROR_MESSAGE) from None

    async def keys(self) -> Iterable[Text]:
        try:
            sender_ids = []
            async for key in self._client().scan_iter(match=f"{self.key_prefix}*"):
                decoded_key = self._decode(key)
                if decoded_key.startswith(self.key_prefix):
                    sender_ids.append(decoded_key.removeprefix(self.key_prefix))
            return sender_ids
        except TrackerStoreException:
            raise
        except Exception:
            raise TrackerStoreException(SANITIZED_ERROR_MESSAGE) from None
