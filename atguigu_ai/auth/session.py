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
local session_type = redis.call('TYPE', session_key)['ok']
if session_type ~= 'none' and session_type ~= 'hash' then
  redis.call('DEL', session_key)
  return {}
end
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
    if not isinstance(token, str) or not 0 < len(token) <= 512:
        return False
    try:
        token.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


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
