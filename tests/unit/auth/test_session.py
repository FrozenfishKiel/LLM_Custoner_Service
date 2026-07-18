import hashlib
import inspect
from datetime import datetime, timezone

import pytest
from redis.exceptions import ConnectionError, ResponseError, TimeoutError

from atguigu_ai.auth import (
    Account,
    AccountIdentity,
    AccountRole,
    AccountStatus,
    AccountUserBinding,
    AuditEvent,
    AuditResult,
    AuthBase,
    CreatedSession,
    RedisSessionStore,
    SessionStoreUnavailable,
)


FIXED_NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)


class RecordingRedis:
    def __init__(self, replies=None, error=None):
        self.replies = list(replies or [])
        self.error = error
        self.calls = []
        self.eval_calls = 0

    async def eval(self, script, numkeys, *values):
        self.eval_calls += 1
        self.calls.append((script, numkeys, values))
        if self.error is not None:
            raise self.error
        return self.replies.pop(0) if self.replies else 1


def identity():
    return AccountIdentity(
        account_id="00000000-0000-0000-0000-000000000001",
        role=AccountRole.consumer,
        status=AccountStatus.active,
    )


def test_existing_and_session_exports_are_preserved():
    assert all(
        value is not None
        for value in (
            Account,
            AccountRole,
            AccountStatus,
            AccountUserBinding,
            AuditEvent,
            AuditResult,
            AuthBase,
            AccountIdentity,
            CreatedSession,
            RedisSessionStore,
            SessionStoreUnavailable,
        )
    )


def test_public_session_interface_is_async_and_exact():
    assert inspect.iscoroutinefunction(RedisSessionStore.create)
    assert inspect.iscoroutinefunction(RedisSessionStore.resolve)
    assert inspect.iscoroutinefunction(RedisSessionStore.revoke)
    assert inspect.iscoroutinefunction(RedisSessionStore.revoke_all)
    public = {name for name in vars(RedisSessionStore) if not name.startswith("_")}
    assert public == {"create", "resolve", "revoke", "revoke_all"}


@pytest.mark.parametrize(
    ("account_id", "role", "status"),
    [
        ("", AccountRole.consumer, AccountStatus.active),
        ("   ", AccountRole.consumer, AccountStatus.active),
        ("x" * 37, AccountRole.consumer, AccountStatus.active),
        (None, AccountRole.consumer, AccountStatus.active),
        ("account", "consumer", AccountStatus.active),
        ("account", AccountRole.consumer, "active"),
    ],
)
def test_account_identity_rejects_invalid_values(account_id, role, status):
    with pytest.raises(ValueError):
        AccountIdentity(account_id=account_id, role=role, status=status)


@pytest.mark.parametrize(
    ("ttl", "threshold"),
    [(0, 0), (-1, 0), (10, -1), (10, 10), (10, 11), (True, 0), (10, False)],
)
def test_constructor_rejects_invalid_ttl_configuration(ttl, threshold):
    with pytest.raises(ValueError):
        RedisSessionStore(RecordingRedis(), ttl_seconds=ttl, refresh_threshold_seconds=threshold)


@pytest.mark.asyncio
async def test_create_returns_raw_token_and_sends_only_digest_to_redis():
    redis = RecordingRedis([1])
    store = RedisSessionStore(
        redis,
        token_factory=lambda: "unit-test-token",
        clock=lambda: FIXED_NOW,
    )
    created = await store.create(identity())
    digest = hashlib.sha256(b"unit-test-token").hexdigest()

    assert created == CreatedSession(token="unit-test-token", expires_at=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc))
    assert len(redis.calls) == 1
    flattened = repr(redis.calls[0])
    assert digest in flattened
    assert "unit-test-token" not in flattened


@pytest.mark.asyncio
async def test_resolve_normalizes_byte_responses():
    redis = RecordingRedis([[b"account-1", b"admin", b"disabled"]])
    store = RedisSessionStore(redis)
    assert await store.resolve("raw-token") == AccountIdentity(
        account_id="account-1",
        role=AccountRole.admin,
        status=AccountStatus.disabled,
    )


@pytest.mark.parametrize("token", [None, "", "x" * 513, 42])
@pytest.mark.asyncio
async def test_malformed_token_resolves_none_without_redis(token):
    redis = RecordingRedis()
    store = RedisSessionStore(redis)
    assert await store.resolve(token) is None
    assert redis.calls == []


@pytest.mark.parametrize("token", [None, "", "x" * 513, 42])
@pytest.mark.asyncio
async def test_malformed_token_revoke_is_noop_without_redis(token):
    redis = RecordingRedis()
    store = RedisSessionStore(redis)
    assert await store.revoke(token) is None
    assert redis.calls == []


@pytest.mark.parametrize("operation", ["resolve", "revoke"])
@pytest.mark.asyncio
async def test_unencodable_token_is_malformed_without_redis_access(operation):
    redis = RecordingRedis()
    store = RedisSessionStore(redis)

    result = await getattr(store, operation)("\ud800")

    assert result is None
    assert redis.eval_calls == 0


@pytest.mark.asyncio
async def test_create_uses_four_attempts_for_invalid_tokens_and_collisions():
    generated = iter([None, "", "collision-one", "collision-two"])
    redis = RecordingRedis([0, 0])
    store = RedisSessionStore(redis, token_factory=lambda: next(generated))

    with pytest.raises(SessionStoreUnavailable, match="^Unable to allocate session$"):
        await store.create(identity())

    assert len(redis.calls) == 2
    assert all(call[1] == 3 for call in redis.calls)


@pytest.mark.asyncio
async def test_four_digest_collisions_do_not_overwrite_existing_sessions():
    tokens = iter(["token-1", "token-2", "token-3", "token-4"])
    redis = RecordingRedis([0, 0, 0, 0])
    store = RedisSessionStore(redis, token_factory=lambda: next(tokens))

    with pytest.raises(SessionStoreUnavailable, match="^Unable to allocate session$"):
        await store.create(identity())

    assert len(redis.calls) == 4
    assert all(call[1] == 3 for call in redis.calls)


@pytest.mark.parametrize("operation", ["create", "resolve"])
@pytest.mark.asyncio
async def test_invalid_clock_raises_value_error_without_redis_access(operation):
    redis = RecordingRedis()
    store = RedisSessionStore(redis, token_factory=lambda: "raw-token", clock=lambda: datetime(2026, 7, 18))
    with pytest.raises(ValueError, match="timezone-aware"):
        if operation == "create":
            await store.create(identity())
        else:
            await store.resolve("raw-token")
    assert redis.calls == []


@pytest.mark.parametrize("error", [ConnectionError("redis://user:secret@host"), TimeoutError("raw-token"), ResponseError("script source")])
@pytest.mark.parametrize("operation", ["create", "resolve", "revoke", "revoke_all"])
@pytest.mark.asyncio
async def test_redis_errors_have_one_stable_sanitized_boundary(error, operation):
    redis = RecordingRedis(error=error)
    store = RedisSessionStore(redis, token_factory=lambda: "raw-token")

    with pytest.raises(SessionStoreUnavailable) as captured:
        if operation == "create":
            await store.create(identity())
        elif operation == "resolve":
            await store.resolve("raw-token")
        elif operation == "revoke":
            await store.revoke("raw-token")
        else:
            await store.revoke_all(identity().account_id)

    assert str(captured.value) == "Session store is unavailable"
    assert captured.value.__cause__ is None
    assert "secret" not in str(captured.value)
    assert "raw-token" not in str(captured.value)
    assert "script source" not in str(captured.value)
