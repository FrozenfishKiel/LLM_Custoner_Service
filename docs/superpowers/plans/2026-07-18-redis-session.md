# Redis Session Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Redis-backed opaque Sessions for PRD A-04 and A-05, including the global revocation primitive required later by A-06, A-08, account disable, and account deletion.

**Architecture:** `RedisSessionStore` exposes four async operations and stores only SHA-256 token digests. Four Lua scripts linearize create, resolve, single revoke, and account-wide revoke; a per-account random 128-bit generation invalidates stale Sessions, while `revoke_all` remains O(1) and leaves old keys to TTL cleanup. This adapter is standalone-Redis-only because its scripts access keys derived at runtime; Redis Cluster is not supported.

**Verified harness deviation:** Docker Desktop 4.78 can leave a restarted container running internally while dropping its Windows host port publication. The integration harness therefore verifies the project ownership labels and the complete declared Redis contract, removes only that owned container, and recreates it with the same named AOF volume before polling the host endpoint. This supersedes the provisional `docker restart` / `docker start` snippets below; persistence is still proven across a real container boundary without replacing the volume.

**Post-review edge hardening:** The final contract also rejects strings that cannot be UTF-8 encoded without calling Redis, and resolve deletes a Session key of the wrong Redis type before returning unauthenticated. These two review-driven cases increase the final unit count to 43 and the integration count to 21; earlier code snippets remain implementation history rather than permission to remove the hardening.

**Execution note:** The planned intermediate RED, adapter, harness, and evidence commits were consolidated into one final reviewed feature commit so the completed Session slice remains atomic. Their checkboxes record that the corresponding artifacts and verification gates are present in that commit, not that four separate historical commits were created.

**Tech Stack:** Python 3.12, redis-py asyncio 5.x, Redis 7 with AOF, pytest, pytest-asyncio, Docker Desktop

---

## File Map

- Create `atguigu_ai/auth/session.py`: immutable Session values, validation, token allocation, four private Lua scripts, and the Redis adapter.
- Modify `atguigu_ai/auth/__init__.py`: preserve all seven model exports and append the four public Session exports.
- Modify `requirements-atguigu.txt`: add the redis-py runtime dependency.
- Modify `.env.example`: document Session TTL settings.
- Create `tests/unit/auth/test_session.py`: isolated public-contract, validation, hashing, normalization, collision, and failure-mapping tests.
- Create `tests/integration/test_redis_session.py`: real Redis atomicity, expiry, concurrency, restart, and recovery tests.
- Modify `docs/TECHNICAL_DESIGN.md`: record the concrete adapter and failure contract.
- Create `docs/reports/integration/2026-07-18-redis-session.md`: record commands, acceptance thresholds, and observed evidence.

## Public Contract

Only these Session names are exported from `atguigu_ai.auth`: `AccountIdentity`, `CreatedSession`, `RedisSessionStore`, and `SessionStoreUnavailable`. Private helpers and Lua constants remain internal to `session.py`. `AccountIdentity` raises `ValueError` when `account_id` is not a non-blank string of at most 36 characters or when `role`/`status` is not an `AccountRole`/`AccountStatus` instance. A token factory that produces an invalid token or an existing digest consumes one of four allocation attempts; four failed attempts raise `SessionStoreUnavailable("Unable to allocate session")`. The account generation is a lowercase 32-hex-character value from `secrets.token_hex(16)`; a missing generation makes every existing Session fail closed.

### Task 1: Add Unit Contract Tests (RED)

**Files:**
- Create: `tests/unit/auth/test_session.py`

- [x] **Step 1: Create the complete unit test module**

Create `tests/unit/auth/test_session.py` with this content:

```python
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

    async def eval(self, script, numkeys, *values):
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
```

- [x] **Step 2: Run the unit module and confirm RED**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth/test_session.py -q
```

Expected: exit code `1`; collection reports that `AccountIdentity` cannot be imported from `atguigu_ai.auth`.

- [x] **Step 3: Commit the RED test**

Run:

```powershell
git add tests/unit/auth/test_session.py
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "test: define Redis session contract"
```

Expected: exit code `0`; one new file is committed.

### Task 2: Implement the Redis Session Module (GREEN)

**Files:**
- Create: `atguigu_ai/auth/session.py`
- Modify: `atguigu_ai/auth/__init__.py`
- Modify: `requirements-atguigu.txt`
- Modify: `.env.example`

- [x] **Step 1: Add redis-py to runtime dependencies**

Append this dependency under the core dependency section in `requirements-atguigu.txt`:

```text
# Redis async Session storage
redis>=5.0.0,<6.0.0
```

- [x] **Step 2: Install the declared dependency and verify its version**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pip install "redis>=5.0.0,<6.0.0"
D:\Anaconda3\envs\ai-content-ops\python.exe -c "import redis; major=int(redis.__version__.split('.')[0]); assert major == 5; print(redis.__version__)"
```

Expected: both commands exit `0`; the second prints a `5.x` version.

- [x] **Step 3: Add Session environment examples**

Append these lines to `.env.example`:

```dotenv
SESSION_TTL_SECONDS=604800
SESSION_REFRESH_THRESHOLD_SECONDS=86400
```

- [x] **Step 4: Create the complete Session adapter**

Create `atguigu_ai/auth/session.py` with this content:

```python
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from redis.exceptions import RedisError

from .models import AccountRole, AccountStatus


_CREATE_SESSION_LUA = r"""
local session_key = KEYS[1]
local index_key = KEYS[2]
local generation_key = KEYS[3]
if redis.call('EXISTS', session_key) == 1 then
  return 0
end
local candidate = ARGV[7]
if string.len(candidate) ~= 32 or string.match(candidate, '^[0-9a-f]+$') == nil then
  return redis.error_reply('invalid session generation candidate')
end
redis.call('SETNX', generation_key, candidate)
local generation = redis.call('GET', generation_key)
if not generation or string.len(generation) ~= 32 or string.match(generation, '^[0-9a-f]+$') == nil then
  return redis.error_reply('invalid session generation')
end
redis.call('HSET', session_key,
  'account_id', ARGV[1],
  'role', ARGV[2],
  'status', ARGV[3],
  'generation', generation,
  'issued_at', ARGV[4],
  'last_seen_at', ARGV[4])
redis.call('EXPIRE', session_key, tonumber(ARGV[5]))
redis.call('SADD', index_key, ARGV[6])
redis.call('EXPIRE', index_key, tonumber(ARGV[5]))
return 1
"""

_RESOLVE_SESSION_LUA = r"""
local session_key = KEYS[1]
local values = redis.call('HMGET', session_key,
  'account_id', 'role', 'status', 'generation', 'issued_at', 'last_seen_at')
if not values[1] then
  redis.call('DEL', session_key)
  return {}
end
local account_id = values[1]
local role = values[2]
local status = values[3]
local stored_generation = values[4]
local function remove_session()
  redis.call('DEL', session_key)
  if account_id and string.len(account_id) > 0 then
    local index_key = ARGV[1] .. account_id
    redis.call('SREM', index_key, ARGV[3])
    if redis.call('SCARD', index_key) == 0 then
      redis.call('DEL', index_key)
    end
  end
end
if not account_id or string.len(account_id) == 0 or string.len(account_id) > 36
   or (role ~= 'consumer' and role ~= 'admin')
   or (status ~= 'pending' and status ~= 'active' and status ~= 'disabled')
   or not stored_generation or string.len(stored_generation) ~= 32
   or string.match(stored_generation, '^[0-9a-f]+$') == nil
   or not tonumber(values[5]) or not tonumber(values[6]) then
  remove_session()
  return {}
end
local current_generation = redis.call('GET', ARGV[2] .. account_id)
if not current_generation or string.len(current_generation) ~= 32
   or string.match(current_generation, '^[0-9a-f]+$') == nil
   or stored_generation ~= current_generation then
  remove_session()
  return {}
end
local remaining = redis.call('TTL', session_key)
if remaining < 0 then
  remove_session()
  return {}
end
redis.call('HSET', session_key, 'last_seen_at', ARGV[4])
if remaining <= tonumber(ARGV[6]) then
  local index_key = ARGV[1] .. account_id
  redis.call('EXPIRE', session_key, tonumber(ARGV[5]))
  redis.call('SADD', index_key, ARGV[3])
  redis.call('EXPIRE', index_key, tonumber(ARGV[5]))
end
return {account_id, role, status}
"""

_REVOKE_SESSION_LUA = r"""
local session_key = KEYS[1]
local account_id = redis.call('HGET', session_key, 'account_id')
local deleted = redis.call('DEL', session_key)
if account_id and string.len(account_id) > 0 and string.len(account_id) <= 36 then
  local index_key = ARGV[1] .. account_id
  redis.call('SREM', index_key, ARGV[2])
  if redis.call('SCARD', index_key) == 0 then
    redis.call('DEL', index_key)
  end
end
return deleted
"""

_REVOKE_ALL_SESSIONS_LUA = r"""
local index_key = KEYS[1]
local generation_key = KEYS[2]
local generation = ARGV[1]
if string.len(generation) ~= 32 or string.match(generation, '^[0-9a-f]+$') == nil then
  return redis.error_reply('invalid session generation')
end
redis.call('SET', generation_key, generation)
redis.call('DEL', index_key)
return 1
"""

_SESSION_PREFIX = "auth:session:"
_INDEX_PREFIX = "auth:account_sessions:"
_GENERATION_PREFIX = "auth:session_generation:"
_STORE_ERROR = "Session store is unavailable"
_ALLOCATION_ERROR = "Unable to allocate session"
_MAX_ALLOCATION_ATTEMPTS = 4


@dataclass(frozen=True)
class AccountIdentity:
    account_id: str
    role: AccountRole
    status: AccountStatus

    def __post_init__(self) -> None:
        if not isinstance(self.account_id, str) or not self.account_id.strip() or len(self.account_id) > 36:
            raise ValueError("account_id must be a non-blank string of at most 36 characters")
        if not isinstance(self.role, AccountRole):
            raise ValueError("role must be an AccountRole")
        if not isinstance(self.status, AccountStatus):
            raise ValueError("status must be an AccountStatus")


@dataclass(frozen=True)
class CreatedSession:
    token: str
    expires_at: datetime


class SessionStoreUnavailable(RuntimeError):
    pass


def _valid_token(token: object) -> bool:
    return isinstance(token, str) and 0 < len(token) <= 512


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, str):
        return value
    raise ValueError("Redis response is not text")


class RedisSessionStore:
    def __init__(
        self,
        redis_client: Any,
        ttl_seconds: int = 604800,
        refresh_threshold_seconds: int = 86400,
        token_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer")
        if (
            isinstance(refresh_threshold_seconds, bool)
            or not isinstance(refresh_threshold_seconds, int)
            or refresh_threshold_seconds < 0
            or refresh_threshold_seconds >= ttl_seconds
        ):
            raise ValueError("refresh_threshold_seconds must satisfy 0 <= threshold < ttl")
        self._redis = redis_client
        self._ttl_seconds = ttl_seconds
        self._refresh_threshold_seconds = refresh_threshold_seconds
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now.astimezone(timezone.utc)

    async def create(self, identity: AccountIdentity) -> CreatedSession:
        if not isinstance(identity, AccountIdentity):
            raise ValueError("identity must be an AccountIdentity")
        now = self._now()
        for _ in range(_MAX_ALLOCATION_ATTEMPTS):
            token = self._token_factory()
            if not _valid_token(token):
                continue
            token_digest = _digest(token)
            try:
                created = await self._redis.eval(
                    _CREATE_SESSION_LUA,
                    3,
                    _SESSION_PREFIX + token_digest,
                    _INDEX_PREFIX + identity.account_id,
                    _GENERATION_PREFIX + identity.account_id,
                    identity.account_id,
                    identity.role.value,
                    identity.status.value,
                    str(int(now.timestamp())),
                    str(self._ttl_seconds),
                    token_digest,
                    secrets.token_hex(16),
                )
            except RedisError:
                raise SessionStoreUnavailable(_STORE_ERROR) from None
            if created == 1:
                return CreatedSession(token=token, expires_at=now + timedelta(seconds=self._ttl_seconds))
            if created != 0:
                raise SessionStoreUnavailable(_STORE_ERROR) from None
        raise SessionStoreUnavailable(_ALLOCATION_ERROR) from None

    async def resolve(self, token: str) -> AccountIdentity | None:
        if not _valid_token(token):
            return None
        token_digest = _digest(token)
        now = self._now()
        try:
            result = await self._redis.eval(
                _RESOLVE_SESSION_LUA,
                1,
                _SESSION_PREFIX + token_digest,
                _INDEX_PREFIX,
                _GENERATION_PREFIX,
                token_digest,
                str(int(now.timestamp())),
                str(self._ttl_seconds),
                str(self._refresh_threshold_seconds),
            )
            if not result:
                return None
            if not isinstance(result, (list, tuple)) or len(result) != 3:
                await self._redis.eval(
                    _REVOKE_SESSION_LUA,
                    1,
                    _SESSION_PREFIX + token_digest,
                    _INDEX_PREFIX,
                    token_digest,
                )
                return None
            return AccountIdentity(
                account_id=_text(result[0]),
                role=AccountRole(_text(result[1])),
                status=AccountStatus(_text(result[2])),
            )
        except (ValueError, UnicodeError):
            try:
                await self._redis.eval(
                    _REVOKE_SESSION_LUA,
                    1,
                    _SESSION_PREFIX + token_digest,
                    _INDEX_PREFIX,
                    token_digest,
                )
            except RedisError:
                raise SessionStoreUnavailable(_STORE_ERROR) from None
            return None
        except RedisError:
            raise SessionStoreUnavailable(_STORE_ERROR) from None

    async def revoke(self, token: str) -> None:
        if not _valid_token(token):
            return None
        token_digest = _digest(token)
        try:
            await self._redis.eval(
                _REVOKE_SESSION_LUA,
                1,
                _SESSION_PREFIX + token_digest,
                _INDEX_PREFIX,
                token_digest,
            )
        except RedisError:
            raise SessionStoreUnavailable(_STORE_ERROR) from None
        return None

    async def revoke_all(self, account_id: str) -> None:
        if not isinstance(account_id, str) or not account_id.strip() or len(account_id) > 36:
            raise ValueError("account_id must be a non-blank string of at most 36 characters")
        new_generation = secrets.token_hex(16)
        try:
            await self._redis.eval(
                _REVOKE_ALL_SESSIONS_LUA,
                2,
                _INDEX_PREFIX + account_id,
                _GENERATION_PREFIX + account_id,
                new_generation,
            )
        except RedisError:
            raise SessionStoreUnavailable(_STORE_ERROR) from None
        return None
```

- [x] **Step 5: Preserve model exports and append Session exports**

Replace `atguigu_ai/auth/__init__.py` with:

```python
from .models import (
    Account,
    AccountRole,
    AccountStatus,
    AccountUserBinding,
    AuditEvent,
    AuditResult,
    AuthBase,
)
from .session import (
    AccountIdentity,
    CreatedSession,
    RedisSessionStore,
    SessionStoreUnavailable,
)

__all__ = [
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
]
```

- [x] **Step 6: Run the unit module and confirm GREEN**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth/test_session.py -q
```

Expected: exit code `0`; `43 passed`.

- [x] **Step 7: Run the existing unit and security suites**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit tests/security -q
```

Expected: exit code `0`; no failure or error.

- [x] **Step 8: Commit the adapter slice**

Run:

```powershell
git add .env.example requirements-atguigu.txt atguigu_ai/auth/session.py atguigu_ai/auth/__init__.py tests/unit/auth/test_session.py
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "feat: add Redis session adapter"
```

Expected: exit code `0`; Session implementation, exports, dependency, configuration, and unit tests are committed together.

### Task 3: Add a Reproducible Real-Redis Harness

**Files:**
- Create: `tests/integration/test_redis_session.py`

- [x] **Step 1: Verify or create the Redis 7 AOF container**

Run from PowerShell:

```powershell
$existing = docker ps -a --filter "name=^/llm-cs-redis$" --format "{{.Names}}"
if (-not $existing) {
  docker run -d --name llm-cs-redis --label com.atguigu.project=llm_customer_service --label com.atguigu.purpose=redis-session-integration --restart unless-stopped -p 127.0.0.1:6379:6379 -v llm-cs-redis-data:/data redis:7 redis-server --appendonly yes --maxmemory-policy noeviction
} else {
  $inspect = docker inspect llm-cs-redis | ConvertFrom-Json | Select-Object -First 1
  $binding = $inspect.HostConfig.PortBindings.'6379/tcp' | Select-Object -First 1
  $volume = $inspect.Mounts | Where-Object { $_.Type -eq 'volume' -and $_.Name -eq 'llm-cs-redis-data' -and $_.Destination -eq '/data' }
  $command = $inspect.Config.Cmd -join ' '
  $owned = $inspect.Config.Labels.'com.atguigu.project' -eq 'llm_customer_service' -and $inspect.Config.Labels.'com.atguigu.purpose' -eq 'redis-session-integration'
  if (-not $owned -or $inspect.Config.Image -ne 'redis:7' -or $inspect.HostConfig.RestartPolicy.Name -ne 'unless-stopped' -or $binding.HostIp -ne '127.0.0.1' -or $binding.HostPort -ne '6379' -or -not $volume -or $command -ne 'redis-server --appendonly yes --maxmemory-policy noeviction') { throw "llm-cs-redis is not owned by this project or does not match the exact Redis 7/AOF/noeviction/loopback/named-volume/restart-policy contract" }
  docker start llm-cs-redis | Out-Null
}
docker exec llm-cs-redis redis-cli ping
```

Expected: exit code `0`; the last line is `PONG`. Reuse is allowed only after exact ownership labels, `redis:7`, `unless-stopped`, loopback port, named volume, AOF, and `noeviction` validation. Any mismatch stops before `start`, `FLUSHDB`, `restart`, or `stop`.

- [x] **Step 2: Create the complete integration test module**

Create `tests/integration/test_redis_session.py` with this content:

```python
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

from atguigu_ai.auth import AccountIdentity, AccountRole, AccountStatus, RedisSessionStore, SessionStoreUnavailable


REDIS_URL = "redis://127.0.0.1:6379/15"
CONTAINER = "llm-cs-redis"


def docker(*args, check=True):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True, timeout=45)


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


def wait_for_redis(timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = docker("exec", CONTAINER, "redis-cli", "ping", check=False)
        if result.returncode == 0 and result.stdout.strip() == "PONG":
            return time.monotonic()
        time.sleep(0.25)
    raise AssertionError("Redis did not return PONG within 30 seconds")


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
async def test_aof_session_survives_container_restart(redis_client):
    store = RedisSessionStore(redis_client, ttl_seconds=60, refresh_threshold_seconds=10)
    created = await store.create(identity())
    await asyncio.sleep(1.2)
    started = time.monotonic()
    docker("restart", CONTAINER)
    wait_for_redis()
    restarted = client()
    try:
        assert await RedisSessionStore(restarted, ttl_seconds=60, refresh_threshold_seconds=10).resolve(created.token) == identity()
        assert time.monotonic() - started <= 30
    finally:
        await restarted.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_redis_failure_is_sanitized_and_recovers(redis_client):
    store = RedisSessionStore(redis_client)
    docker("stop", CONTAINER)
    try:
        with pytest.raises(SessionStoreUnavailable, match="^Session store is unavailable$"):
            await store.resolve("failure-token")
    finally:
        docker("start", CONTAINER)
        wait_for_redis()
    recovered = client()
    try:
        await recovered.flushdb()
        recovered_store = RedisSessionStore(recovered)
        created = await recovered_store.create(identity())
        assert await recovered_store.resolve(created.token) == identity()
    finally:
        await recovered.flushdb()
        await recovered.aclose()
```

- [x] **Step 3: Register the integration marker if pytest warns about it**

If `pytest.ini` exists, add this exact marker entry under `[pytest]`; otherwise create `pytest.ini` with this content:

```ini
[pytest]
markers =
    integration: requires local infrastructure and exercises real adapters
```

Expected: `pytest --collect-only` emits no `PytestUnknownMarkWarning` for `integration`.

- [x] **Step 4: Run the real-Redis integration module**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_redis_session.py -q -m integration
```

Expected: exit code `0`; `21 passed` (the corrupt-field parameterization contributes six cases and wrong-type corruption adds one case), missing generation and invalid clocks fail closed, 1000 indexed Sessions meet both O(1) latency bounds, natural expiry removes the Session and single-Session index, both 50-iteration races complete, refreshed TTL is in `3..4`, recreation is below 30 seconds, and failure recovery succeeds. Redis unavailability is a failure, never a skip.

- [x] **Step 5: Prove database 15 is clean**

Run:

```powershell
docker exec llm-cs-redis redis-cli -n 15 DBSIZE
```

Expected: the final line is `0`.

- [x] **Step 6: Commit the real-Redis harness**

Run:

```powershell
git add tests/integration/test_redis_session.py pytest.ini
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "test: cover Redis session lifecycle"
```

Expected: exit code `0`; the integration harness and marker registration are committed.

### Task 4: Document the Slice and Capture QA Evidence

**Files:**
- Modify: `docs/TECHNICAL_DESIGN.md`
- Create: `docs/reports/integration/2026-07-18-redis-session.md`

- [x] **Step 1: Replace the provisional Session key/revocation text and add the concrete adapter contract**

In `docs/TECHNICAL_DESIGN.md`, replace every provisional `session_epoch`/integer-epoch entry and revoke-all traversal statement. The Redis key block must contain exactly `auth:session:{token_hash}`, `auth:account_sessions:{account_id}`, and `auth:session_generation:{account_id}` with random-generation semantics; no old epoch key or increment design may remain. Then place this subsection immediately after that single canonical key/revocation description:

```markdown
#### Redis Session adapter contract

`atguigu_ai.auth.RedisSessionStore` owns opaque Session creation, lookup, single revocation, and account-wide revocation. The client receives only a random raw token; Redis keys contain its SHA-256 digest. Create, resolve, revoke, and revoke-all each execute in one Lua script. Account-wide revocation writes a new random 128-bit value to `auth:session_generation:{account_id}` and deletes only the index, so it is O(1); old Session hashes fail generation comparison and expire by TTL. A missing generation is invalid rather than generation zero.

The adapter supports standalone Redis only; Redis Cluster is unsupported because resolve derives the account generation and index keys after reading the Session hash. Production Redis should enable AOF and use `maxmemory-policy noeviction` so authentication state is not silently evicted.

Malformed client tokens never reach Redis. Corrupt Session hashes are deleted and resolve as unauthenticated. Redis connection, timeout, and script failures raise `SessionStoreUnavailable("Session store is unavailable")`; HTTP mapping to 503 belongs to the later authentication-route slice. This slice supplies the revocation mechanism for password changes, reset, disable, and deletion, but does not yet orchestrate those account operations.
```

- [x] **Step 2: Run QA commands while capturing their complete output**

Run:

```powershell
New-Item -ItemType Directory -Force docs/reports/integration/evidence | Out-Null
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth/test_session.py -q 2>&1 | Tee-Object docs/reports/integration/evidence/redis-session-unit.txt
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_redis_session.py -q -m integration 2>&1 | Tee-Object docs/reports/integration/evidence/redis-session-integration.txt
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit tests/security -q 2>&1 | Tee-Object docs/reports/integration/evidence/redis-session-regression.txt
D:\Anaconda3\envs\ai-content-ops\python.exe -m pip check 2>&1 | Tee-Object docs/reports/integration/evidence/redis-session-pip-check.txt
docker exec llm-cs-redis redis-cli -n 15 DBSIZE | Tee-Object docs/reports/integration/evidence/redis-session-dbsize.txt
```

Expected: the three pytest commands exit `0`; unit reports `43 passed`; integration reports `21 passed`; DB size reports `0`. `pip check` is recorded verbatim; unrelated conflicts in the shared Conda environment are not represented as repository Redis compatibility failures.

- [x] **Step 3: Create the integration report with fixed acceptance criteria and evidence links**

Create `docs/reports/integration/2026-07-18-redis-session.md` with this content after replacing each command-result sentence only when the captured output differs:

```markdown
# Redis Session Integration Report — 2026-07-18

## Scope

This slice completes A-04/A-05 Session storage. It provides account-wide revocation used later by A-06, A-08, account disable, and account deletion; account/password/admin orchestration is outside this slice.

## Acceptance Evidence

| Check | Required result | Evidence |
| --- | --- | --- |
| Unit contract | 43 passed, exit 0 | `evidence/redis-session-unit.txt` |
| Real Redis lifecycle | 21 passed, exit 0 | `evidence/redis-session-integration.txt` |
| Existing unit/security regression | no failures, exit 0 | `evidence/redis-session-regression.txt` |
| Allocation collision budget | exactly 4 attempts | unit contract |
| Concurrent create/revoke-all | 50/50 iterations safe | integration module |
| Concurrent resolve/revoke-all | 50/50 iterations safe | integration module |
| O(1) revoke-all | 1000 Sessions; at most 250 ms and at most `max(100 ms, 10x low-cardinality median)` | integration module |
| Missing generation | Session and index entry removed, resolve returns none | integration module |
| Invalid clock | `ValueError`, zero Redis calls, existing Session retained | unit and integration modules |
| Sliding expiry | observed Session TTL between 3 and 4 seconds | integration module |
| AOF recreation | Session resolves after owned-container recreation with the same named volume within 30 seconds | integration module |
| Dependency outage | sanitized failure, then successful create/resolve | integration module |
| Test isolation | Redis database 15 size is 0 | `evidence/redis-session-dbsize.txt` |
| Dependency consistency | raw `pip check` output retained | `evidence/redis-session-pip-check.txt` |

## Risk Result

Raw Session tokens are absent from Redis keys, malformed inputs cause no Redis access, corrupt records fail closed, and Redis error messages disclose no URL, token, credentials, key, or Lua source. Redis remains a Session dependency rather than a source of order truth.
```

- [x] **Step 4: Run the complete repository tests**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests -q
```

Expected: exit code `0`; all collected tests pass and no integration test is skipped.

- [x] **Step 5: Compile the changed Python package**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m compileall -q atguigu_ai/auth tests/unit/auth tests/integration
```

Expected: exit code `0`; no output.

- [x] **Step 6: Check whitespace and repository state**

Run:

```powershell
git diff --check
git status --short
```

Expected: `git diff --check` exits `0` with no output; status lists only files named by this plan plus captured evidence and the plan itself.

- [x] **Step 7: Scan staged names and content for forbidden secrets/runtime artifacts**

Run:

```powershell
git add .env.example requirements-atguigu.txt atguigu_ai/auth docs/TECHNICAL_DESIGN.md docs/superpowers/plans/2026-07-18-redis-session.md docs/reports/integration tests pytest.ini
$names = git diff --cached --name-only
$forbiddenNames = $names | Select-String -Pattern '(^|/)(\.env$|dump\.rdb$|appendonly\.aof$|.*\.log$|__pycache__/|.*\.pyc$)'
if ($forbiddenNames) { $forbiddenNames; throw 'forbidden staged filename' }
$secretHits = git diff --cached -- . ':(exclude)docs/superpowers/plans/2026-07-18-redis-session.md' ':(exclude)tests/unit/auth/test_session.py' | Select-String -CaseSensitive -Pattern 'redis://[^\s:@]+:[^\s@]+@|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|SESSION(_ID|_TOKEN)?\s*=\s*[^\s<]+'
if ($secretHits) { $secretHits; throw 'possible staged secret or raw Session token' }
```

Expected: exit code `0`; neither scan prints a match. The plan and Session unit module are excluded from the value scan because they intentionally contain test-only token and credential-like error fixtures; repository security tests separately assert that those values are sanitized and never committed as runtime configuration.

- [x] **Step 8: Commit documentation and QA evidence**

Run:

```powershell
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "docs: record Redis session verification"
```

Expected: exit code `0`; technical design, report, evidence, and final plan are committed.

- [x] **Step 9: Verify the final commit and clean worktree**

Run:

```powershell
git show --stat --oneline HEAD
git status --short
```

Expected: the commit title is `docs: record Redis session verification`; `git status --short` prints nothing.

## Completion Gate

The slice is complete only when all checkboxes are checked, all pytest and compile commands exit `0`, both 50-iteration concurrency tests pass, the 1000-Session revoke-all test meets both latency bounds, missing generation and invalid-clock tests pass, owned-container recreation with the same named AOF volume and outage recovery pass against the project-owned standalone Redis 7 container with `noeviction`, database 15 is empty, the report contains captured command output references, and the staged-file scans find no secret or runtime artifact. A-06/A-08 and admin/account orchestration remain separate implementation slices that consume `revoke_all`.
