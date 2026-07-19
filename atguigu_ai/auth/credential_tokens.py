from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Mapping

from redis.exceptions import RedisError


class CredentialTokenPurpose(str, Enum):
    verify_email = "verify_email"
    reset_password = "reset_password"


@dataclass(frozen=True)
class IssuedCredentialToken:
    token: str
    expires_at: datetime


class CredentialTokenStoreUnavailable(RuntimeError):
    pass


_ISSUE_CREDENTIAL_TOKEN_LUA = r"""
local verify_email_token_key = KEYS[1]
local reset_password_token_key = KEYS[2]
local current_key = KEYS[3]
local previous_token_prefix = ARGV[1]
local account_id = ARGV[2]
local ttl = tonumber(ARGV[3])
local digest = ARGV[4]
local token_key = ARGV[5]

if redis.call('EXISTS', verify_email_token_key) == 1 or redis.call('EXISTS', reset_password_token_key) == 1 then
  return 0
end

local current_type = redis.call('TYPE', current_key)['ok']
if current_type ~= 'none' and current_type ~= 'string' then
  redis.call('DEL', current_key)
end

local previous_digest = redis.call('GET', current_key)
if previous_digest then
  if string.len(previous_digest) == 64 and string.match(previous_digest, '^[0-9a-f]+$') ~= nil then
    local previous_token_key = previous_token_prefix .. previous_digest
    local previous_type = redis.call('TYPE', previous_token_key)['ok']
    if previous_type == 'string' then
      local previous_account_id = redis.call('GET', previous_token_key)
      if previous_account_id == account_id then
        redis.call('DEL', previous_token_key)
      else
        redis.call('DEL', current_key)
      end
    else
      redis.call('DEL', current_key)
    end
  else
    redis.call('DEL', current_key)
  end
end

redis.call('SET', token_key, account_id, 'EX', ttl)
redis.call('SET', current_key, digest, 'EX', ttl)
return 1
"""

_CONSUME_CREDENTIAL_TOKEN_LUA = r"""
local token_key = KEYS[1]
local current_key_prefix = ARGV[1]
local digest = ARGV[2]

local token_type = redis.call('TYPE', token_key)['ok']
if token_type == 'none' then
  return ''
end
if token_type ~= 'string' then
  redis.call('DEL', token_key)
  return ''
end

local account_id = redis.call('GET', token_key)
if not account_id or string.len(account_id) == 0 or string.len(account_id) > 36 then
  redis.call('DEL', token_key)
  return ''
end

local token_ttl = redis.call('TTL', token_key)
if token_ttl <= 0 then
  redis.call('DEL', token_key)
  return ''
end

local current_key = current_key_prefix .. account_id
local current_type = redis.call('TYPE', current_key)['ok']
if current_type == 'none' then
  redis.call('DEL', token_key)
  return ''
end
if current_type ~= 'string' then
  redis.call('DEL', current_key)
  redis.call('DEL', token_key)
  return ''
end

local current_digest = redis.call('GET', current_key)
local current_ttl = redis.call('TTL', current_key)
if current_digest ~= digest then
  redis.call('DEL', token_key)
  return ''
end
if current_ttl <= 0 then
  redis.call('DEL', token_key)
  redis.call('DEL', current_key)
  return ''
end

redis.call('DEL', token_key)
redis.call('DEL', current_key)
return account_id
"""

_TOKEN_PREFIX = {
    CredentialTokenPurpose.verify_email: "auth:verify_email:",
    CredentialTokenPurpose.reset_password: "auth:reset_password:",
}
_CURRENT_PREFIX = "auth:credential_token_current:"
_DEFAULT_TTL_SECONDS = {
    CredentialTokenPurpose.verify_email: 1800,
    CredentialTokenPurpose.reset_password: 1800,
}
_STORE_ERROR = "Credential token store is unavailable"
_ALLOCATION_ERROR = "Unable to allocate credential token"
_MAX_ALLOCATION_ATTEMPTS = 4
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


class RedisCredentialTokenStore:
    def __init__(
        self,
        redis: Any,
        ttl_seconds: Mapping[CredentialTokenPurpose, int] | None = None,
        token_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._ttl_seconds = _validate_ttl_seconds(ttl_seconds)
        self._redis = redis
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def issue(self, account_id: str, purpose: CredentialTokenPurpose) -> IssuedCredentialToken:
        normalized_account_id = _validate_account_id(account_id)
        _validate_purpose(purpose)
        now = self._now()
        ttl_seconds = self._ttl_seconds[purpose]

        for _ in range(_MAX_ALLOCATION_ATTEMPTS):
            token = self._token_factory()
            if not _valid_token(token):
                continue
            token_digest = _digest(token)
            try:
                created = await self._redis.eval(
                    _ISSUE_CREDENTIAL_TOKEN_LUA,
                    3,
                    _token_key(CredentialTokenPurpose.verify_email, token_digest),
                    _token_key(CredentialTokenPurpose.reset_password, token_digest),
                    _current_key(purpose, normalized_account_id),
                    _TOKEN_PREFIX[purpose],
                    normalized_account_id,
                    ttl_seconds,
                    token_digest,
                    _token_key(purpose, token_digest),
                )
            except RedisError:
                raise CredentialTokenStoreUnavailable(_STORE_ERROR) from None
            if created == 1:
                return IssuedCredentialToken(
                    token=token,
                    expires_at=now + timedelta(seconds=ttl_seconds),
                )
            if created != 0:
                raise CredentialTokenStoreUnavailable(_STORE_ERROR) from None

        raise CredentialTokenStoreUnavailable(_ALLOCATION_ERROR) from None

    async def consume(self, token: str, purpose: CredentialTokenPurpose) -> str | None:
        _validate_purpose(purpose)
        if not _valid_token(token):
            return None
        token_digest = _digest(token)
        try:
            result = await self._redis.eval(
                _CONSUME_CREDENTIAL_TOKEN_LUA,
                1,
                _token_key(purpose, token_digest),
                _current_key_prefix(purpose),
                token_digest,
            )
        except RedisError:
            raise CredentialTokenStoreUnavailable(_STORE_ERROR) from None

        if not result:
            return None
        try:
            account_id = _text(result)
        except (UnicodeError, ValueError):
            raise CredentialTokenStoreUnavailable(_STORE_ERROR) from None
        try:
            return _validate_account_id(account_id)
        except ValueError:
            raise CredentialTokenStoreUnavailable(_STORE_ERROR) from None

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now.astimezone(timezone.utc)


def _validate_ttl_seconds(
    ttl_seconds: Mapping[CredentialTokenPurpose, int] | None,
) -> dict[CredentialTokenPurpose, int]:
    if ttl_seconds is None:
        return dict(_DEFAULT_TTL_SECONDS)
    expected_keys = set(CredentialTokenPurpose)
    if set(ttl_seconds.keys()) != expected_keys:
        raise ValueError("ttl_seconds must contain exactly every CredentialTokenPurpose")
    validated: dict[CredentialTokenPurpose, int] = {}
    for purpose, ttl in ttl_seconds.items():
        if not isinstance(purpose, CredentialTokenPurpose):
            raise ValueError("ttl_seconds keys must be CredentialTokenPurpose values")
        if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
            raise ValueError("ttl_seconds values must be positive integers")
        validated[purpose] = ttl
    return validated


def _validate_account_id(account_id: object) -> str:
    if not isinstance(account_id, str):
        raise ValueError("account_id must be a non-blank string of at most 36 characters")
    normalized = account_id.strip()
    if not normalized or len(normalized) > 36:
        raise ValueError("account_id must be a non-blank string of at most 36 characters")
    return normalized


def _validate_purpose(purpose: object) -> None:
    if not isinstance(purpose, CredentialTokenPurpose):
        raise ValueError("purpose must be a CredentialTokenPurpose")


def _valid_token(token: object) -> bool:
    if not isinstance(token, str) or _TOKEN_PATTERN.fullmatch(token) is None:
        return False
    try:
        token.encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _token_key(purpose: CredentialTokenPurpose, digest: str) -> str:
    return _TOKEN_PREFIX[purpose] + digest


def _current_key_prefix(purpose: CredentialTokenPurpose) -> str:
    return f"{_CURRENT_PREFIX}{purpose.value}:"


def _current_key(purpose: CredentialTokenPurpose, account_id: str) -> str:
    return _current_key_prefix(purpose) + account_id


def _text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, str):
        return value
    raise ValueError("Redis response is not text")


__all__ = [
    "CredentialTokenPurpose",
    "IssuedCredentialToken",
    "CredentialTokenStoreUnavailable",
    "RedisCredentialTokenStore",
]
