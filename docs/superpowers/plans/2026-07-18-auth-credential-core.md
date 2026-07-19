# Authentication Credential Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build deterministic email/password security primitives and an atomic standalone-Redis credential-token store for email verification and password reset.

**Architecture:** `atguigu_ai.auth.credentials` is a dependency-free domain boundary apart from `email-validator`, AnyIO, and `argon2-cffi`; it canonicalizes consumer email addresses and admits bounded Argon2id work without touching infrastructure. `atguigu_ai.auth.credential_tokens` stores only SHA-256 token digests and uses one Lua script per operation to linearize replacement, global cross-purpose collision detection, and one-time consumption against standalone Redis. MySQL account transactions and the later synchronous `AuthService` remain outside this slice.

**Tech Stack:** Python 3.12, email-validator 2.x, argon2-cffi 23.x, AnyIO 4.x, redis-py asyncio 5.x, Redis 7 with AOF, pytest, pytest-asyncio, Docker Desktop

---

## File Map

- Modify `requirements-atguigu.txt`: declare bounded `email-validator`, `argon2-cffi`, and AnyIO runtime dependencies.
- Create `atguigu_ai/auth/credentials.py`: email normalization, password policy, bounded async Argon2id hashing/verification, dummy work, and encoded-hash cost caps.
- Create `tests/unit/auth/test_credentials.py`: exact exports, email/password boundaries, Argon2 behavior, admission limiting, and event-loop tests.
- Create `atguigu_ai/auth/credential_tokens.py`: token value types, validation, digest-only Redis keys, Lua issue/consume operations, and sanitized errors.
- Modify `atguigu_ai/auth/__init__.py`: preserve all model and Session exports and append exactly eleven credential-core exports.
- Create `tests/unit/auth/test_credential_tokens.py`: isolated token grammar, validation, Lua argument, collision, byte-response, and failure-mapping tests.
- Create `tests/integration/test_redis_credential_tokens.py`: real Redis lifecycle, corruption, TTL, concurrency, collision, outage, AOF, and cleanup tests.
- Create `docs/reports/integration/2026-07-18-auth-credential-core.md`: acceptance evidence, quantitative measurements, boundary review, and residual risks.
- Create `docs/reports/integration/evidence/auth-credential-core-independent-qa.md`: independent QA commands, observed counts, timings, defects, and disposition.
- Create generated evidence text files under `docs/reports/integration/evidence/auth-credential-core-*.txt`: retained command output for unit, integration, security, full-suite, dependency, secret, and DB-cleanup gates.

## Locked Contracts

The new `atguigu_ai.auth` names are exactly `EmailAddress`, `InvalidEmail`, `InvalidPassword`, `PasswordHashingOverloaded`, `PasswordPolicy`, `PasswordHasher`, `normalize_email`, `CredentialTokenPurpose`, `IssuedCredentialToken`, `CredentialTokenStoreUnavailable`, and `RedisCredentialTokenStore`. The existing eleven model and Session names remain exported. Private regexes, parsers, limiters, dummy hashes, digest helpers, and Lua source stay module-private.

Email parsing strips only leading/trailing ASCII whitespace, rejects embedded controls and display-name syntax, disables SMTPUTF8 and DNS deliverability checks, uses the validated ASCII/IDNA address as `display`, and uses `display.casefold()` as the duplicate/login key. Passwords are never stripped or normalized. Argon2 work runs through AnyIO with four worker slots and twenty total admitted calls; the twenty-first call fails immediately. Encoded attacker-controlled hashes are parsed and capped before Argon2 sees them.

Credential tokens are exactly 43 ASCII base64url characters generated from 32 random bytes. Redis receives only their lowercase 64-hex SHA-256 digest. Issuance checks both purpose token keys for a global collision, replaces only an account-owned prior token, and gets four allocation attempts. Consumption is at-most-once and purpose-bound. Redis strings and bytes are accepted; malformed inputs avoid Redis; wrong Redis types and invalid TTL/index state fail closed and are cleaned only where ownership is established.

This plan does not add MySQL queries, schema changes, HTTP routes, SMTP, rate limiting, Sessions orchestration, metrics, demo-data activation, or an `AuthService`. The future orchestration contract is recorded only: login, reset, disable, delete, and activation must serialize on the same MySQL account row with `SELECT ... FOR UPDATE`; token consumption occurs before durable mutation and a consumed token is never restored after downstream failure.

### Task 1: Add Dependencies and Credentials Contract Tests (RED)

**Files:**
- Modify: `requirements-atguigu.txt`
- Create: `tests/unit/auth/test_credentials.py`

- [x] **Step 1: Declare the three bounded runtime dependencies**

Insert immediately after the existing redis-py dependency in `requirements-atguigu.txt`:

```text
# Authentication credential primitives
email-validator>=2.2.0,<3.0.0
argon2-cffi>=23.1.0,<26.0.0
anyio>=4.4.0,<5.0.0
```

- [x] **Step 2: Install only the new dependency ranges and print non-secret versions**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pip install "email-validator>=2.2.0,<3.0.0" "argon2-cffi>=23.1.0,<26.0.0" "anyio>=4.4.0,<5.0.0"
D:\Anaconda3\envs\ai-content-ops\python.exe -c "import importlib.metadata as m; print('email-validator='+m.version('email-validator')); print('argon2-cffi='+m.version('argon2-cffi')); print('anyio='+m.version('anyio'))"
```

Expected: both commands exit `0`; the second prints versions inside all three declared ranges and no package is imported from outside `D:\Anaconda3\envs\ai-content-ops`.

- [x] **Step 3: Create the complete credentials unit contract**

Create `tests/unit/auth/test_credentials.py` with this content:

```python
import asyncio
import inspect
import threading
import time

import pytest

import atguigu_ai.auth.credentials as credentials_module
from atguigu_ai.auth import (
    EmailAddress,
    InvalidEmail,
    InvalidPassword,
    PasswordHasher,
    PasswordHashingOverloaded,
    PasswordPolicy,
    normalize_email,
)


@pytest.mark.parametrize(
    ("raw", "display", "normalized"),
    [
        (" User.Name+tag@Example.COM ", "User.Name+tag@example.com", "user.name+tag@example.com"),
        ("user@bücher.example", "user@xn--bcher-kva.example", "user@xn--bcher-kva.example"),
        ("CaseSensitive@EXAMPLE.COM", "CaseSensitive@example.com", "casesensitive@example.com"),
    ],
)
def test_normalize_email_returns_ascii_display_and_complete_casefold(raw, display, normalized):
    assert normalize_email(raw) == EmailAddress(display=display, normalized=normalized)


def test_email_length_boundary_accepts_254_ascii_characters():
    local = "a" * 64
    domain = ".".join(["b" * 63, "c" * 63, "d" * 57, "com"])
    address = f"{local}@{domain}"
    assert len(address) == 254
    assert normalize_email(address).display == address


@pytest.mark.parametrize(
    "value",
    [
        None,
        42,
        "",
        "   ",
        "Name <user@example.com>",
        "user\n@example.com",
        "user@example.com\x7f",
        "üser@example.com",
        "user@",
        "a" * 65 + "@example.com",
        "a" * 245 + "@example.com",
    ],
)
def test_invalid_email_has_one_sanitized_public_error(value):
    with pytest.raises(InvalidEmail) as captured:
        normalize_email(value)
    assert str(captured.value) == "Invalid email address"
    assert captured.value.__cause__ is None
    assert "example" not in str(captured.value)


@pytest.mark.parametrize("password", ["12345678", "x" * 128, "界" * 128, "密碼安全123"])
def test_password_policy_accepts_length_and_unicode_boundaries(password):
    assert PasswordPolicy().validate(password) is None


@pytest.mark.parametrize(
    "password",
    [
        None,
        42,
        "1234567",
        "x" * 129,
        "valid123\x00",
        "valid123\x1f",
        "valid123\x7f",
        "valid123\ud800",
    ],
)
def test_password_policy_rejects_type_scalar_control_and_surrogate_limits(password):
    with pytest.raises(InvalidPassword) as captured:
        PasswordPolicy().validate(password)
    assert str(captured.value) == "Password does not meet requirements"
    assert captured.value.__cause__ is None


def test_password_policy_does_not_trim_or_normalize():
    policy = PasswordPolicy()
    policy.validate(" pass word ")
    policy.validate("e\u0301password")
    policy.validate("épassword")


@pytest.mark.asyncio
async def test_hash_uses_exact_argon2id_parameters_and_verifies():
    hasher = PasswordHasher()
    encoded = await hasher.hash("correct horse battery staple")
    assert encoded.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    salt, digest = encoded.rsplit("$", 2)[-2:]
    assert len(credentials_module._decode_argon2_field(salt)) == 16
    assert len(credentials_module._decode_argon2_field(digest)) == 32
    assert await hasher.verify(encoded, "correct horse battery staple") is True
    assert await hasher.verify(encoded, "wrong horse battery staple") is False
    assert hasher.needs_rehash(encoded) is False


@pytest.mark.asyncio
async def test_hash_validates_before_argon2(monkeypatch):
    called = False

    def forbidden_hash(password):
        nonlocal called
        called = True
        raise AssertionError(password)

    hasher = PasswordHasher()
    monkeypatch.setattr(hasher._argon2, "hash", forbidden_hash)
    with pytest.raises(InvalidPassword, match="^Password does not meet requirements$"):
        await hasher.hash("short")
    assert called is False


@pytest.mark.asyncio
async def test_verify_policy_invalid_password_returns_false_without_argon2(monkeypatch):
    called = False

    def forbidden_verify(encoded, password):
        nonlocal called
        called = True
        raise AssertionError((encoded, password))

    hasher = PasswordHasher()
    monkeypatch.setattr(hasher._argon2, "verify", forbidden_verify)
    assert await hasher.verify(None, "x" * 129) is False
    assert called is False


@pytest.mark.parametrize(
    "encoded",
    [
        None,
        "not-an-argon2-hash",
        "$argon2i$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$ZGlnaWVzdGRpZ2VzdGRpZ2VzdA",
        "$argon2id$v=16$m=65536,t=3,p=4$c2FsdHNhbHQ$ZGlnaWVzdGRpZ2VzdGRpZ2VzdA",
        "$argon2id$v=19$m=65537,t=3,p=4$c2FsdHNhbHQ$ZGlnaWVzdGRpZ2VzdGRpZ2VzdA",
        "$argon2id$v=19$m=65536,t=4,p=4$c2FsdHNhbHQ$ZGlnaWVzdGRpZ2VzdGRpZ2VzdA",
        "$argon2id$v=19$m=65536,t=3,p=5$c2FsdHNhbHQ$ZGlnaWVzdGRpZ2VzdGRpZ2VzdA",
        "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$ZGlnaWVzdGRpZ2VzdGRpZ2VzdA",
        "$argon2id$v=19$m=65536,t=3,p=4$" + "A" * 100000,
    ],
)
@pytest.mark.asyncio
async def test_none_malformed_unsupported_and_over_cap_hashes_take_one_dummy_verify(monkeypatch, encoded):
    calls = []

    def recording_verify(selected_hash, password):
        calls.append((selected_hash, password))
        return False

    hasher = PasswordHasher()
    monkeypatch.setattr(hasher._argon2, "verify", recording_verify)
    assert await hasher.verify(encoded, "valid-password") is False
    assert calls == [(credentials_module._DUMMY_HASH, "valid-password")]
    assert encoded not in [item[0] for item in calls]


@pytest.mark.asyncio
async def test_committed_dummy_hash_is_real_bounded_argon2id():
    assert credentials_module._eligible_hash(credentials_module._DUMMY_HASH) is True
    with pytest.raises(credentials_module.VerifyMismatchError):
        credentials_module.Argon2PasswordHasher().verify(
            credentials_module._DUMMY_HASH,
            "definitely-not-the-dummy-password",
        )
    assert await PasswordHasher().verify(None, "valid-password") is False


def test_needs_rehash_is_false_for_malformed_or_over_cap_hashes():
    hasher = PasswordHasher()
    assert hasher.needs_rehash("malformed") is False
    assert hasher.needs_rehash(
        "$argon2id$v=19$m=999999,t=3,p=4$c2FsdHNhbHQ$ZGlnaWVzdGRpZ2VzdGRpZ2VzdA"
    ) is False


@pytest.mark.asyncio
async def test_argon2_runs_off_the_event_loop(monkeypatch):
    main_thread = threading.get_ident()
    worker_threads = []

    def slow_hash(password):
        worker_threads.append(threading.get_ident())
        time.sleep(0.05)
        return "encoded"

    hasher = PasswordHasher()
    monkeypatch.setattr(hasher._argon2, "hash", slow_hash)
    ticks = 0

    async def ticker():
        nonlocal ticks
        for _ in range(5):
            await asyncio.sleep(0.01)
            ticks += 1

    encoded, _ = await asyncio.gather(hasher.hash("valid-password"), ticker())
    assert encoded == "encoded"
    assert ticks == 5
    assert worker_threads and all(item != main_thread for item in worker_threads)


@pytest.mark.asyncio
async def test_twenty_jobs_are_admitted_four_at_a_time_and_excess_fails_immediately(monkeypatch):
    entered = 0
    maximum_running = 0
    lock = threading.Lock()
    release = threading.Event()

    def blocked_hash(password):
        nonlocal entered, maximum_running
        with lock:
            entered += 1
            maximum_running = max(maximum_running, entered)
        release.wait(timeout=5)
        with lock:
            entered -= 1
        return "encoded"

    hashers = [PasswordHasher(), PasswordHasher()]
    for hasher in hashers:
        monkeypatch.setattr(hasher._argon2, "hash", blocked_hash)
    tasks = [
        asyncio.create_task(hashers[index % 2].hash(f"valid-password-{index}"))
        for index in range(20)
    ]
    await asyncio.sleep(0.1)
    started = time.perf_counter()
    with pytest.raises(PasswordHashingOverloaded, match="^Password hashing capacity is unavailable$"):
        await PasswordHasher().hash("overflow-password")
    assert time.perf_counter() - started < 0.1
    assert maximum_running == 4
    release.set()
    assert await asyncio.gather(*tasks) == ["encoded"] * 20


def test_public_password_interface_is_exact():
    assert inspect.iscoroutinefunction(PasswordHasher.hash)
    assert inspect.iscoroutinefunction(PasswordHasher.verify)
    assert not inspect.iscoroutinefunction(PasswordHasher.needs_rehash)
    assert {name for name in vars(PasswordPolicy) if not name.startswith("_")} == {"validate"}
    assert {name for name in vars(PasswordHasher) if not name.startswith("_")} == {
        "hash",
        "verify",
        "needs_rehash",
    }
```

- [x] **Step 4: Run the focused RED test**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth/test_credentials.py -q
```

Expected: exit code `2`; collection fails because `atguigu_ai.auth.credentials` and the new public exports do not exist. Dependency import failure is not the intended RED.

- [x] **Step 5: Commit the dependency and credentials RED contract**

Run:

```powershell
git add requirements-atguigu.txt tests/unit/auth/test_credentials.py
git diff --cached --check
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "test: define credential primitive contract"
```

Expected: both commands exit `0`; the commit contains only the dependency declarations and credentials test module.

### Task 2: Implement Credentials Primitives (GREEN)

**Files:**
- Create: `atguigu_ai/auth/credentials.py`
- Modify: `atguigu_ai/auth/__init__.py`
- Test: `tests/unit/auth/test_credentials.py`

- [x] **Step 1: Create the complete email/password implementation**

Create `atguigu_ai/auth/credentials.py` with this content:

```python
from __future__ import annotations

import base64
import binascii
import re
import threading
from dataclasses import dataclass
from typing import Callable, TypeVar

import anyio
from argon2 import PasswordHasher as Argon2PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type
from email_validator import EmailNotValidError, validate_email


_EMAIL_ERROR = "Invalid email address"
_PASSWORD_ERROR = "Password does not meet requirements"
_OVERLOAD_ERROR = "Password hashing capacity is unavailable"
_ASCII_WHITESPACE = " \t\n\r\v\f"
_ARGON2_RE = re.compile(
    r"^\$argon2id\$v=(?P<version>\d+)\$"
    r"m=(?P<memory>\d+),t=(?P<time>\d+),p=(?P<parallelism>\d+)\$"
    r"(?P<salt>[A-Za-z0-9+/]{11,43})\$(?P<digest>[A-Za-z0-9+/]{22,86})$"
)
_MAX_ENCODED_HASH_LENGTH = 512
_DUMMY_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$"
    "k2MD9zRiy8pDjx2t4nZL8w$MWZbaOikwezbFdMP6gsLCQbfOJ66LNdeioM6dR4WST8"
)
_T = TypeVar("_T")
_WORKERS = threading.BoundedSemaphore(4)
_ADMISSION = threading.BoundedSemaphore(20)


@dataclass(frozen=True)
class EmailAddress:
    display: str
    normalized: str


class InvalidEmail(ValueError):
    pass


class InvalidPassword(ValueError):
    pass


class PasswordHashingOverloaded(RuntimeError):
    pass


def normalize_email(value: str) -> EmailAddress:
    try:
        if not isinstance(value, str):
            raise ValueError
        candidate = value.strip(_ASCII_WHITESPACE)
        if not candidate or any(ord(character) < 32 or ord(character) == 127 for character in candidate):
            raise ValueError
        parsed = validate_email(
            candidate,
            allow_smtputf8=False,
            allow_display_name=False,
            check_deliverability=False,
        )
        display = parsed.ascii_email
        if display is None or len(display) > 254 or not display.isascii():
            raise ValueError
        return EmailAddress(display=display, normalized=display.casefold())
    except (EmailNotValidError, TypeError, ValueError, UnicodeError):
        raise InvalidEmail(_EMAIL_ERROR) from None


class PasswordPolicy:
    def validate(self, password: str) -> None:
        try:
            if not isinstance(password, str) or not 8 <= len(password) <= 128:
                raise ValueError
            for character in password:
                codepoint = ord(character)
                if codepoint < 32 or codepoint == 127 or 0xD800 <= codepoint <= 0xDFFF:
                    raise ValueError
            if len(password.encode("utf-8")) > 512:
                raise ValueError
        except (TypeError, ValueError, UnicodeError):
            raise InvalidPassword(_PASSWORD_ERROR) from None


def _decode_argon2_field(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, validate=True)


def _eligible_hash(encoded: object) -> bool:
    if not isinstance(encoded, str) or len(encoded) > _MAX_ENCODED_HASH_LENGTH:
        return False
    match = _ARGON2_RE.fullmatch(encoded)
    if match is None:
        return False
    try:
        salt = _decode_argon2_field(match.group("salt"))
        digest = _decode_argon2_field(match.group("digest"))
    except (ValueError, binascii.Error):
        return False
    return (
        int(match.group("version")) == 19
        and 8 <= int(match.group("memory")) <= 65536
        and 1 <= int(match.group("time")) <= 3
        and 1 <= int(match.group("parallelism")) <= 4
        and 8 <= len(salt) <= 32
        and 16 <= len(digest) <= 64
    )


class PasswordHasher:
    def __init__(self, policy: PasswordPolicy | None = None) -> None:
        self._policy = policy or PasswordPolicy()
        self._argon2 = Argon2PasswordHasher(
            time_cost=3,
            memory_cost=65536,
            parallelism=4,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
    async def _run(self, operation: Callable[[], _T]) -> _T:
        if not _ADMISSION.acquire(blocking=False):
            raise PasswordHashingOverloaded(_OVERLOAD_ERROR) from None

        def guarded_operation() -> _T:
            with _WORKERS:
                return operation()

        try:
            return await anyio.to_thread.run_sync(guarded_operation)
        finally:
            _ADMISSION.release()

    async def hash(self, password: str) -> str:
        self._policy.validate(password)
        return await self._run(lambda: self._argon2.hash(password))

    async def verify(self, password_hash: str | None, password: str) -> bool:
        try:
            self._policy.validate(password)
        except InvalidPassword:
            return False
        selected_hash = password_hash if _eligible_hash(password_hash) else _DUMMY_HASH

        def verify_sync() -> bool:
            try:
                verified = bool(self._argon2.verify(selected_hash, password))
                return verified if selected_hash == password_hash else False
            except (InvalidHashError, VerificationError, VerifyMismatchError):
                return False

        return await self._run(verify_sync)

    def needs_rehash(self, password_hash: str) -> bool:
        if not _eligible_hash(password_hash):
            return False
        try:
            return self._argon2.check_needs_rehash(password_hash)
        except (InvalidHashError, VerificationError):
            return False
```

- [x] **Step 2: Append the exact credential-core exports without changing existing exports**

Append these imports to `atguigu_ai/auth/__init__.py` after the Session import block:

```python
from .credentials import (
    EmailAddress,
    InvalidEmail,
    InvalidPassword,
    PasswordHasher,
    PasswordHashingOverloaded,
    PasswordPolicy,
    normalize_email,
)
```

Append these names to `__all__` after `SessionStoreUnavailable`:

```python
    "EmailAddress",
    "InvalidEmail",
    "InvalidPassword",
    "PasswordHashingOverloaded",
    "PasswordPolicy",
    "PasswordHasher",
    "normalize_email",
```

- [x] **Step 3: Run the focused GREEN tests**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth/test_credentials.py -q --durations=10
```

Expected: exit code `0`; all credentials tests pass. The real hash test prints no password or hash, and the overload test completes in under 5 seconds.

- [x] **Step 4: Run the existing auth and security regression set**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth tests/security -q
```

Expected: exit code `0`; all collected tests pass and the eleven pre-existing `atguigu_ai.auth` exports remain importable.

- [x] **Step 5: Commit credentials GREEN**

Run:

```powershell
git add atguigu_ai/auth/credentials.py atguigu_ai/auth/__init__.py
git diff --cached --check
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "feat: add bounded credential primitives"
```

Expected: both commands exit `0`; the commit contains only the credentials implementation and public export change.

### Task 3: Add Credential Token Contract Tests (RED)

**Files:**
- Create: `tests/unit/auth/test_credential_tokens.py`

- [x] **Step 1: Define the recording Redis and fixed token helpers**

Create a `RecordingRedis` async fake that records every `eval(script, numkeys, *values)` call and consumes configured replies or raises a configured `redis.exceptions.RedisError`. Define:

```python
FIXED_NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
TOKEN_A = "A" * 43
TOKEN_B = "B" * 43

def store(redis, replies=None, token_factory=lambda: TOKEN_A, clock=lambda: FIXED_NOW):
    return RedisCredentialTokenStore(
        redis,
        ttl_seconds={
            CredentialTokenPurpose.verify_email: 1800,
            CredentialTokenPurpose.reset_password: 900,
        },
        token_factory=token_factory,
        clock=clock,
    )
```

The fake must never interpret Lua. Unit assertions inspect arguments and stable result handling only; real atomic behavior belongs to Task 5.

- [x] **Step 2: Add the exact public and validation matrix**

Add parameterized tests for this matrix:

| Contract | Inputs | Expected |
| --- | --- | --- |
| exact public methods | class namespace | only `issue`, `consume` |
| constructor TTL | missing purpose, `0`, negative, bool, non-int | `ValueError`, zero Redis calls |
| account ID | non-string, blank, 37 chars | `ValueError`, zero Redis calls |
| purpose | raw string or foreign enum | `ValueError`, zero Redis calls |
| clock | naive datetime or non-datetime | `ValueError`, zero Redis calls |
| malformed consume token | empty, 42/44 chars, `+`, `/`, `=`, non-ASCII, surrogate, non-string | `None`, zero Redis calls |
| valid issue | reply `1` | raw token returned; UTC `expires_at`; Redis arguments contain SHA-256 digest, never raw token |
| valid consume | bytes or string account reply | normalized account ID |
| missing consume | empty reply | `None` |
| collision | four `0` replies | exactly four evals then `CredentialTokenStoreUnavailable("Unable to allocate credential token")` |
| Redis error | connection, timeout, response errors for both methods | exactly `CredentialTokenStoreUnavailable("Credential token store is unavailable")` with no cause text |
| unexpected Lua reply | non-`0/1` issue or malformed consume reply | sanitized unavailable error |

Assert every generated token matches `^[A-Za-z0-9_-]{43}$`, both token digests are passed so cross-purpose collisions can be checked, and the public export list preserves all existing model/Session names plus the eleven names fixed by the design.

- [x] **Step 3: Run the focused RED test**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth/test_credential_tokens.py -q
```

Expected: collection fails only because `atguigu_ai.auth.credential_tokens` and its exports do not exist.

- [x] **Step 4: Commit token RED**

```powershell
git add tests/unit/auth/test_credential_tokens.py
git diff --cached --check
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "test: define credential token contract"
```

### Task 4: Implement Redis Credential Tokens (GREEN)

**Files:**
- Create: `atguigu_ai/auth/credential_tokens.py`
- Modify: `atguigu_ai/auth/__init__.py`

- [x] **Step 1: Add immutable public values and strict scalar validation**

Implement the exact signatures from the approved design. Use a frozen `IssuedCredentialToken`, the two-value string enum, `_valid_account_id`, exact 43-character ASCII base64url `_valid_token`, SHA-256 `_digest`, bytes/string response normalization, four allocation attempts, and timezone-aware UTC clock normalization. Default both TTLs to 1800 seconds and require the TTL mapping to contain exactly both enum purposes.

Key construction is fixed:

```python
_TOKEN_PREFIX = {
    CredentialTokenPurpose.verify_email: "auth:verify_email:",
    CredentialTokenPurpose.reset_password: "auth:reset_password:",
}
_CURRENT_PREFIX = "auth:credential_token_current:"

def _current_key(purpose, account_id):
    return f"{_CURRENT_PREFIX}{purpose.value}:{account_id}"
```

- [x] **Step 2: Implement the issue Lua transaction**

The script receives three keys: candidate verify key, candidate reset key, and current account/purpose index. Arguments contain account ID, purpose-specific candidate key, digest, TTL, and the two token prefixes. It must execute this order atomically:

```text
validate digest is 64 lowercase hex and account ID is non-blank <=36
if either candidate-purpose key exists: return 0
if current index type is neither none nor string: delete only the index
if index is string:
  validate old digest
  derive old token key for this purpose
  delete old token only when its type is string and GET equals this account ID
  delete the corrupt/current index in every other case
SET candidate token key -> account ID EX ttl
SET current index -> digest EX ttl
return 1
```

Never delete a token owned by another account when a current index is corrupt. The script is standalone-Redis-only by design.

- [x] **Step 3: Implement the consume Lua transaction**

The script receives the directly addressed token key; arguments contain the current-index prefix, purpose, digest, and both validated prefixes. It must atomically implement:

```text
if token key type is none: return empty
if token key type is not string: delete token key; return empty
read and validate account ID; otherwise delete token key and return empty
derive account/purpose current index
if index type is none: delete token key; return empty
if index type is not string: delete index and token; return empty
require index value == digest
require TTL(token) > 0 and TTL(index) > 0
on any mismatch: delete directly addressed token; delete index only when it points to digest; return empty
on match: delete both keys; return account ID
```

Map every redis-py `RedisError` and every impossible response shape to the stable store-unavailable exception. Do not catch programmer validation errors as dependency failures.

- [x] **Step 4: Append the seven token exports and run GREEN**

Preserve all existing exports and append `CredentialTokenPurpose`, `IssuedCredentialToken`, `CredentialTokenStoreUnavailable`, and `RedisCredentialTokenStore` (the other seven new credential names were added in Task 2).

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth/test_credential_tokens.py tests/unit/auth/test_credentials.py -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth tests/security -q
```

Expected: both commands exit `0`; no existing export or security regression fails.

- [x] **Step 5: Commit token GREEN**

```powershell
git add atguigu_ai/auth/credential_tokens.py atguigu_ai/auth/__init__.py
git diff --cached --check
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "feat: add one-time credential tokens"
```

### Task 5: Real Redis, Performance, Risk, and Completion Evidence

**Files:**
- Create: `tests/integration/test_redis_credential_tokens.py`
- Modify: `docs/TECHNICAL_DESIGN.md`
- Create: `docs/reports/integration/2026-07-18-auth-credential-core.md`
- Create: `docs/reports/integration/evidence/credential-core-*.txt`

- [x] **Step 1: Build the real Redis matrix on the owned harness**

Reuse the ownership-checked `llm-cs-redis` helpers from `test_redis_session.py`; do not duplicate destructive commands without the project/purpose label gate. Use DB 15, flush before/after every case, and cover:

| Scenario | Required assertion |
| --- | --- |
| issue/consume | first consume returns account; second returns `None`; raw token absent from all keys |
| replacement | old token `None`, new token succeeds, other purpose remains valid |
| expiry | both token/index expire and consume returns `None` |
| raw bytes client | `decode_responses=False` returns normalized account ID |
| digest key wrong type | corrupt key deleted, `None` |
| index wrong type/missing/no TTL/early expiry | fail closed; scoped cleanup exactly matches design |
| corrupt cross-account index | issuing A never deletes B's token |
| forced digest collision across accounts/purposes | four retries; no overwrite |
| concurrent consume | 50 iterations, exactly one winner every iteration |
| concurrent issue | 50 iterations, exactly one final current token |
| issue vs consume | both allowed linearization outcomes observed or deterministically forced |
| outage/recovery | sanitized exception; recreate same AOF volume; subsequent issue/consume works |
| AOF recreation | unconsumed token survives owned-container recreation |

- [x] **Step 2: Add quantitative tests and bounds**

Add a `@pytest.mark.load` Argon2 test that first records ten sequential hashes and ten sequential verifies, then measures end-to-end await time from submission for 20 concurrent verifies shared across two `PasswordHasher` instances. Assert all succeed, event-loop heartbeat advances, no overload occurs at exactly 20 process-wide admissions, concurrent P95 is <=1.0 second, and the 21st request held behind 20 controlled jobs raises `PasswordHashingOverloaded`. Print sequential hash/verify P50/P95, concurrent P50/P95/wall time, and peak process working set; never print passwords or hashes.

Add 300 sequential real-Redis issue/consume samples and report P50/P95/ops per second. Add replacement with 1000 stale tokens and assert the final issue operation remains <=250 ms. These are local acceptance numbers, not production capacity claims.

- [x] **Step 3: Run focused integration and independent QA**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_redis_credential_tokens.py -q -s -m integration
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth/test_credentials.py tests/unit/auth/test_credential_tokens.py tests/security -q
docker exec llm-cs-redis redis-cli -n 15 DBSIZE
```

Expected: all tests pass, quantitative lines satisfy bounds, and DB size is `0`. Dispatch the dedicated QA Agent to rerun these commands, awkward Unicode/corruption cases, concurrency, resource/overload, secret exposure, monitoring boundary, and cleanup; retain its exact commands and measured results.

- [x] **Step 4: Update design documentation and write evidence**

Update the Redis key block and credential sections in `docs/TECHNICAL_DESIGN.md` with the current-index key, one-current-token semantics, consume-first failure policy, bounded async Argon2, and the future account-row-lock orchestration contract. Write the report with links to UTF-8 evidence for unit, integration, load, regression, `pip check`, DB size, and independent QA. Record shared-environment conflicts separately from project failures.

- [x] **Step 5: Run the final repository gates**

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m compileall -q atguigu_ai/auth tests/unit/auth tests/integration
git diff --check
docker exec llm-cs-redis redis-cli -n 15 DBSIZE
```

Expected: full suite and compile exit `0`, whitespace check is clean, DB size is `0`.

- [x] **Step 6: Stage and scan only planned artifacts**

Reject `.env`, Redis persistence files, logs, caches, bytecode, credential-bearing URLs, private keys, raw passwords/tokens, and runtime secrets. Explicit test fixtures may be excluded only by exact path and must remain covered by repository security tests. Confirm every evidence `.txt` is UTF-8/ASCII text rather than PowerShell UTF-16.

- [x] **Step 7: Complete reviews and commit**

Require specification approval, code-quality approval, and independent QA disposition with no open Critical/Important finding. Then commit:

```powershell
git add requirements-atguigu.txt atguigu_ai/auth/__init__.py atguigu_ai/auth/credentials.py atguigu_ai/auth/credential_tokens.py tests/unit/auth/test_credentials.py tests/unit/auth/test_credential_tokens.py tests/integration/test_redis_credential_tokens.py docs/TECHNICAL_DESIGN.md docs/reports/integration/2026-07-18-auth-credential-core.md docs/reports/integration/evidence/auth-credential-core-independent-qa.md docs/reports/integration/evidence/auth-credential-core-unit.txt docs/reports/integration/evidence/auth-credential-core-integration.txt docs/reports/integration/evidence/auth-credential-core-load.txt docs/reports/integration/evidence/auth-credential-core-regression.txt docs/reports/integration/evidence/auth-credential-core-full-suite.txt docs/reports/integration/evidence/auth-credential-core-pip-check.txt docs/reports/integration/evidence/auth-credential-core-secret-scan.txt docs/reports/integration/evidence/auth-credential-core-dbsize.txt docs/superpowers/plans/2026-07-18-auth-credential-core.md
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "feat: add authentication credential core"
git show --stat --oneline HEAD
git status --short
```

Expected: the final commit contains only this slice and the worktree is clean.

## Completion Gate

This primitive slice is complete only when every checkbox is closed, the approved design is fully represented, credentials and token unit suites are GREEN, real Redis corruption/concurrency/AOF/outage cases pass, Argon2 P95 and admission bounds pass, DB 15 is empty, UTF-8 evidence and independent QA are retained, both reviews approve, and the final commit leaves a clean worktree. MySQL account repository, SMTP, rate limiting, activation/demo-data transaction, HTTP routes, cookies, and browser E2E remain later slices and must implement the row-lock orchestration contract from the approved design.
