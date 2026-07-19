import hashlib
import inspect
import re
from datetime import datetime, timedelta, timezone
from enum import Enum

import pytest
from redis.exceptions import ConnectionError, RedisError, ResponseError, TimeoutError

import atguigu_ai.auth as auth_module
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
    CredentialTokenPurpose,
    CredentialTokenStoreUnavailable,
    EmailAddress,
    InvalidEmail,
    InvalidPassword,
    IssuedCredentialToken,
    PasswordHasher,
    PasswordHashingOverloaded,
    PasswordPolicy,
    RedisCredentialTokenStore,
    RedisSessionStore,
    SessionStoreUnavailable,
    normalize_email,
)


FIXED_NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
TOKEN_A = "A" * 43
TOKEN_B = "B" * 43
ACCOUNT_ID = "00000000-0000-0000-0000-000000000001"
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


class RecordingRedis:
    def __init__(self, replies=None, error: RedisError | None = None):
        self.replies = list(replies or [])
        self.error = error
        self.calls = []

    async def eval(self, script, numkeys, *values):
        self.calls.append((script, numkeys, values))
        if self.error is not None:
            raise self.error
        return self.replies.pop(0) if self.replies else 1


class ForeignPurpose(Enum):
    verify_email = "verify_email"


def store(redis, replies=None, token_factory=lambda: TOKEN_A, clock=lambda: FIXED_NOW):
    if replies is not None:
        redis.replies = list(replies)
    return RedisCredentialTokenStore(
        redis,
        ttl_seconds={
            CredentialTokenPurpose.verify_email: 1800,
            CredentialTokenPurpose.reset_password: 900,
        },
        token_factory=token_factory,
        clock=clock,
    )


def _flatten_calls(redis):
    return repr(redis.calls)


def _digest(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def test_existing_credential_and_token_exports_are_exact():
    assert auth_module.__all__ == [
        "Account",
        "AccountRole",
        "AccountStatus",
        "AccountUserBinding",
        "AuditEvent",
        "AuditResult",
        "AuthBase",
        "AccountIdentity",
        "CreatedSession",
        "RedisSessionStore",
        "SessionStoreUnavailable",
        "EmailAddress",
        "InvalidEmail",
        "InvalidPassword",
        "PasswordHashingOverloaded",
        "PasswordPolicy",
        "PasswordHasher",
        "normalize_email",
        "CredentialTokenPurpose",
        "IssuedCredentialToken",
        "CredentialTokenStoreUnavailable",
        "RedisCredentialTokenStore",
    ]
    assert all(
        item is not None
        for item in (
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
            EmailAddress,
            InvalidEmail,
            InvalidPassword,
            PasswordHashingOverloaded,
            PasswordPolicy,
            PasswordHasher,
            normalize_email,
            CredentialTokenPurpose,
            IssuedCredentialToken,
            CredentialTokenStoreUnavailable,
            RedisCredentialTokenStore,
        )
    )


def test_public_token_interface_is_async_and_exact():
    assert inspect.iscoroutinefunction(RedisCredentialTokenStore.issue)
    assert inspect.iscoroutinefunction(RedisCredentialTokenStore.consume)
    assert {name for name in vars(RedisCredentialTokenStore) if not name.startswith("_")} == {
        "issue",
        "consume",
    }


@pytest.mark.parametrize(
    "ttl_seconds",
    [
        {CredentialTokenPurpose.verify_email: 1800},
        {CredentialTokenPurpose.verify_email: 0, CredentialTokenPurpose.reset_password: 900},
        {CredentialTokenPurpose.verify_email: 1800, CredentialTokenPurpose.reset_password: -1},
        {CredentialTokenPurpose.verify_email: True, CredentialTokenPurpose.reset_password: 900},
        {CredentialTokenPurpose.verify_email: 1800, CredentialTokenPurpose.reset_password: "900"},
    ],
)
def test_constructor_rejects_invalid_ttl_configuration_without_redis(ttl_seconds):
    redis = RecordingRedis()
    with pytest.raises(ValueError):
        RedisCredentialTokenStore(redis, ttl_seconds=ttl_seconds)
    assert redis.calls == []


@pytest.mark.parametrize("account_id", [None, 42, "", "   ", "x" * 37])
@pytest.mark.asyncio
async def test_issue_rejects_invalid_account_id_without_redis(account_id):
    redis = RecordingRedis()
    with pytest.raises(ValueError):
        await store(redis).issue(account_id, CredentialTokenPurpose.verify_email)
    assert redis.calls == []


@pytest.mark.parametrize("purpose", ["verify_email", ForeignPurpose.verify_email])
@pytest.mark.asyncio
async def test_issue_and_consume_reject_invalid_purpose_without_redis(purpose):
    redis = RecordingRedis()
    token_store = store(redis)
    with pytest.raises(ValueError):
        await token_store.issue(ACCOUNT_ID, purpose)
    with pytest.raises(ValueError):
        await token_store.consume(TOKEN_A, purpose)
    assert redis.calls == []


@pytest.mark.parametrize("clock_value", [datetime(2026, 7, 18, 12, 0), "not-a-datetime"])
@pytest.mark.asyncio
async def test_issue_rejects_invalid_clock_without_redis(clock_value):
    redis = RecordingRedis()
    with pytest.raises(ValueError):
        await store(redis, clock=lambda: clock_value).issue(
            ACCOUNT_ID,
            CredentialTokenPurpose.verify_email,
        )
    assert redis.calls == []


@pytest.mark.parametrize(
    "token",
    [
        None,
        "",
        "A" * 42,
        "A" * 44,
        "A" * 42 + "+",
        "A" * 42 + "/",
        "A" * 42 + "=",
        "é" * 43,
        "\ud800",
        42,
    ],
)
@pytest.mark.asyncio
async def test_malformed_consume_token_returns_none_without_redis(token):
    redis = RecordingRedis()
    assert await store(redis).consume(token, CredentialTokenPurpose.verify_email) is None
    assert redis.calls == []


@pytest.mark.asyncio
async def test_issue_returns_raw_token_expiry_and_sends_only_digest_to_redis():
    redis = RecordingRedis([1])
    issued = await store(redis).issue(ACCOUNT_ID, CredentialTokenPurpose.verify_email)

    assert issued == IssuedCredentialToken(
        token=TOKEN_A,
        expires_at=FIXED_NOW + timedelta(seconds=1800),
    )
    assert TOKEN_RE.fullmatch(issued.token)
    flattened = _flatten_calls(redis)
    assert _digest(TOKEN_A) in flattened
    assert TOKEN_A not in flattened
    assert "auth:verify_email:" in flattened
    assert 1800 in redis.calls[0][2] or "1800" in redis.calls[0][2]


@pytest.mark.parametrize("reply", [ACCOUNT_ID, ACCOUNT_ID.encode("ascii")])
@pytest.mark.asyncio
async def test_consume_normalizes_string_and_byte_account_replies(reply):
    redis = RecordingRedis([reply])
    assert await store(redis).consume(TOKEN_A, CredentialTokenPurpose.reset_password) == ACCOUNT_ID
    flattened = _flatten_calls(redis)
    assert _digest(TOKEN_A) in flattened
    assert TOKEN_A not in flattened
    assert "reset_password" in flattened


@pytest.mark.parametrize("reply", [None, "", b""])
@pytest.mark.asyncio
async def test_missing_consume_reply_returns_none(reply):
    redis = RecordingRedis([reply])
    assert await store(redis).consume(TOKEN_A, CredentialTokenPurpose.verify_email) is None


@pytest.mark.asyncio
async def test_four_issue_collisions_raise_sanitized_allocation_error():
    tokens = iter([TOKEN_A, TOKEN_B, "C" * 43, "D" * 43])
    redis = RecordingRedis([0, 0, 0, 0])
    with pytest.raises(CredentialTokenStoreUnavailable) as captured:
        await store(redis, token_factory=lambda: next(tokens)).issue(
            ACCOUNT_ID,
            CredentialTokenPurpose.verify_email,
        )

    assert str(captured.value) == "Unable to allocate credential token"
    assert captured.value.__cause__ is None
    assert len(redis.calls) == 4
    flattened = _flatten_calls(redis)
    for token in [TOKEN_A, TOKEN_B, "C" * 43, "D" * 43]:
        assert _digest(token) in flattened
        assert token not in flattened


@pytest.mark.parametrize("error", [ConnectionError("redis://secret"), TimeoutError("raw-token"), ResponseError("lua")])
@pytest.mark.parametrize("operation", ["issue", "consume"])
@pytest.mark.asyncio
async def test_redis_errors_are_sanitized(error, operation):
    redis = RecordingRedis(error=error)
    token_store = store(redis)
    with pytest.raises(CredentialTokenStoreUnavailable) as captured:
        if operation == "issue":
            await token_store.issue(ACCOUNT_ID, CredentialTokenPurpose.verify_email)
        else:
            await token_store.consume(TOKEN_A, CredentialTokenPurpose.verify_email)

    assert str(captured.value) == "Credential token store is unavailable"
    assert captured.value.__cause__ is None
    assert "secret" not in str(captured.value)
    assert "raw-token" not in str(captured.value)
    assert "lua" not in str(captured.value)


@pytest.mark.parametrize("reply", [2, -1, "1", b"1", None])
@pytest.mark.asyncio
async def test_unexpected_issue_lua_reply_is_sanitized_unavailable(reply):
    redis = RecordingRedis([reply])
    with pytest.raises(CredentialTokenStoreUnavailable) as captured:
        await store(redis).issue(ACCOUNT_ID, CredentialTokenPurpose.verify_email)
    assert str(captured.value) == "Credential token store is unavailable"
    assert captured.value.__cause__ is None


@pytest.mark.parametrize("reply", [42, b"\xff", "x" * 37])
@pytest.mark.asyncio
async def test_malformed_consume_lua_reply_is_sanitized_unavailable(reply):
    redis = RecordingRedis([reply])
    with pytest.raises(CredentialTokenStoreUnavailable) as captured:
        await store(redis).consume(TOKEN_A, CredentialTokenPurpose.verify_email)
    assert str(captured.value) == "Credential token store is unavailable"
    assert captured.value.__cause__ is None
