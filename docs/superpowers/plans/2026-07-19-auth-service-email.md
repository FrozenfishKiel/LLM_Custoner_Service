# Authentication Service and Email Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the backend authentication application service for consumer registration, email verification, password login, logout, forgot-password, and reset-password using the already implemented account tables, credential primitives, Redis Session store, and Redis credential-token store.

**Architecture:** `atguigu_ai.auth.account_repository` owns the SQLAlchemy/MySQL account transaction boundary and row-lock operations. `atguigu_ai.email.delivery` owns SMTP-facing email abstractions with a deterministic fake for tests. `atguigu_ai.auth.service` orchestrates repository, password hashing, credential tokens, sessions, and email delivery while preserving enumeration-safe public outcomes and sanitized dependency failures.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x sync sessions, PyMySQL, Redis asyncio stores already implemented, SMTP via standard library, pytest, pytest-asyncio, Docker MySQL/Redis harness.

---

## File Map

- Create `atguigu_ai/auth/account_repository.py`: sync repository and unit-of-work helpers for account rows, audit rows, and row-level locking.
- Create `atguigu_ai/auth/service.py`: async `AuthService` orchestration, public result dataclasses, stable public errors, and enumeration-safe flows.
- Create `atguigu_ai/email/__init__.py` and `atguigu_ai/email/delivery.py`: email delivery protocol, fake outbox, SMTP adapter, and sanitized delivery error.
- Modify `atguigu_ai/auth/__init__.py`: export only stable service/repository result types needed by later API routes.
- Create `tests/unit/auth/test_account_repository.py`: repository contract with SQLite metadata checks and SQLAlchemy fake/session behavior where possible.
- Create `tests/unit/auth/test_auth_service.py`: fake repository, fake email, fake token/session stores, password hasher stubs, and service behavior matrix.
- Create `tests/unit/email/test_delivery.py`: fake and SMTP adapter contract without sending real email.
- Create `tests/integration/test_auth_service_mysql_redis.py`: real MySQL account tables + Redis Session/token stores + fake email end-to-end.
- Create `docs/reports/integration/2026-07-19-auth-service-email.md` and evidence files under `docs/reports/integration/evidence/auth-service-email-*.txt`.
- Modify `docs/TECHNICAL_DESIGN.md`: record concrete AuthService transaction order, email adapter error policy, and remaining HTTP/demo-data boundary.

## Locked Contracts

This slice does not add HTTP routes, browser pages, SMTP production configuration, demo-data initialization, account deletion, admin management, rate limiting, CSRF, cookies, or chat authorization. It builds the backend service that later routes will call.

Public service semantics:

- Register normalizes email, rejects duplicate normalized email, hashes password, creates a `pending` consumer account, issues a `verify_email` credential token, and asks the email adapter to send a verification URL.
- Register returns a generic success result for created pending accounts. It may return a stable duplicate-email domain result only to route code; HTTP mapping in the later slice must still avoid unsafe detail if needed.
- Verify email consumes a `verify_email` token first, then locks the account row with `SELECT ... FOR UPDATE`, marks a pending account active, sets `email_verified_at`, records audit, and returns `AccountIdentity`. Reuse, expiry, disabled accounts, and missing rows fail closed.
- Login normalizes email, performs one password verification for existing eligible accounts and one dummy verification for missing/ineligible accounts, returns one generic `InvalidCredentials` error for unknown email, pending, disabled, and wrong password, and creates a Redis Session only for active consumer/admin accounts with correct password.
- Logout revokes only the supplied Session token and is idempotent.
- Forgot password normalizes email and returns a generic accepted result for missing, pending, and disabled accounts. For active accounts it issues a `reset_password` token and sends a reset URL.
- Reset password consumes a `reset_password` token first, then locks the account row, validates and hashes the new password, revokes all sessions while holding the row lock, writes the new hash, records audit, and commits. A consumed token is never restored after downstream failure.
- Redis, SMTP, and MySQL dependency errors are exposed through stable service exceptions that contain no URL, password, token, account email, or raw SQL.

### Task 1: Email Delivery Contract and Fake Adapter (RED/GREEN)

**Files:**
- Create: `tests/unit/email/test_delivery.py`
- Create: `atguigu_ai/email/__init__.py`
- Create: `atguigu_ai/email/delivery.py`

- [ ] **Step 1: Write the email delivery contract tests**

Create `tests/unit/email/test_delivery.py` with tests for:

```python
import inspect
import smtplib

import pytest

from atguigu_ai.email import (
    EmailDeliveryUnavailable,
    EmailMessage,
    FakeEmailDelivery,
    SMTPEmailDelivery,
)


def test_public_email_exports_are_exact():
    import atguigu_ai.email as email_module

    assert email_module.__all__ == [
        "EmailDeliveryUnavailable",
        "EmailMessage",
        "FakeEmailDelivery",
        "SMTPEmailDelivery",
    ]
    assert inspect.iscoroutinefunction(FakeEmailDelivery.send_verification_email)
    assert inspect.iscoroutinefunction(FakeEmailDelivery.send_password_reset_email)
    assert inspect.iscoroutinefunction(SMTPEmailDelivery.send_verification_email)
    assert inspect.iscoroutinefunction(SMTPEmailDelivery.send_password_reset_email)


@pytest.mark.asyncio
async def test_fake_delivery_records_sanitized_messages():
    delivery = FakeEmailDelivery()
    await delivery.send_verification_email("User@example.com", "https://example.test/verify?token=secret-token")
    await delivery.send_password_reset_email("User@example.com", "https://example.test/reset?token=reset-token")

    assert [message.purpose for message in delivery.outbox] == ["verify_email", "reset_password"]
    assert delivery.outbox[0].recipient == "User@example.com"
    assert delivery.outbox[0].url == "https://example.test/verify?token=secret-token"
    assert "secret-token" not in repr(delivery.outbox[0])


@pytest.mark.parametrize("recipient", ["", "   ", None, 42])
@pytest.mark.asyncio
async def test_fake_delivery_rejects_invalid_recipient(recipient):
    with pytest.raises(ValueError):
        await FakeEmailDelivery().send_verification_email(recipient, "https://example.test/verify")


@pytest.mark.parametrize("url", ["", "ftp://example.test/x", "javascript:alert(1)", None])
@pytest.mark.asyncio
async def test_fake_delivery_rejects_invalid_public_url(url):
    with pytest.raises(ValueError):
        await FakeEmailDelivery().send_password_reset_email("user@example.com", url)


@pytest.mark.asyncio
async def test_smtp_delivery_maps_dependency_errors_without_secret_text(monkeypatch):
    captured = {}

    class FailingSMTP:
        def __init__(self, host, port, timeout):
            captured["host"] = host
            captured["port"] = port
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def starttls(self):
            raise smtplib.SMTPException("smtp-password-leaked")

    monkeypatch.setattr(smtplib, "SMTP", FailingSMTP)
    delivery = SMTPEmailDelivery(
        host="smtp.example.test",
        port=587,
        username="smtp-user",
        password="smtp-password-leaked",
        from_address="noreply@example.test",
        use_tls=True,
    )

    with pytest.raises(EmailDeliveryUnavailable) as captured_error:
        await delivery.send_verification_email("user@example.com", "https://example.test/verify?token=abc")

    assert str(captured_error.value) == "Email delivery is unavailable"
    assert captured_error.value.__cause__ is None
    assert "smtp-password-leaked" not in repr(captured_error.value)
    assert captured == {"host": "smtp.example.test", "port": 587, "timeout": 10}
```

- [ ] **Step 2: Run RED**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/email/test_delivery.py -q
```

Expected: collection fails because `atguigu_ai.email` does not exist.

- [ ] **Step 3: Implement email delivery**

Create `atguigu_ai/email/delivery.py` implementing:

- frozen `EmailMessage(purpose, recipient, url)` with `repr=False` for `url`;
- `EmailDeliveryUnavailable("Email delivery is unavailable")`;
- `FakeEmailDelivery.outbox` list with async `send_verification_email` and `send_password_reset_email`;
- `SMTPEmailDelivery` with constructor parameters `host`, `port`, `username`, `password`, `from_address`, `use_tls`, `timeout=10`; it builds plain-text messages and sends via `smtplib.SMTP` inside `anyio.to_thread.run_sync`;
- validation requiring non-blank string recipient and `http://` or `https://` URL;
- sanitized mapping for `OSError`, `smtplib.SMTPException`, and `TimeoutError`.

Create `atguigu_ai/email/__init__.py` exporting exactly:

```python
from .delivery import EmailDeliveryUnavailable, EmailMessage, FakeEmailDelivery, SMTPEmailDelivery

__all__ = [
    "EmailDeliveryUnavailable",
    "EmailMessage",
    "FakeEmailDelivery",
    "SMTPEmailDelivery",
]
```

- [ ] **Step 4: Run GREEN and commit**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/email/test_delivery.py -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m compileall -q atguigu_ai/email tests/unit/email
git add atguigu_ai/email tests/unit/email/test_delivery.py
git diff --cached --check
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "feat: add email delivery adapter"
```

Expected: tests and compile exit `0`; commit contains only the email module and its tests.

### Task 2: Account Repository Contract and MySQL Unit Boundary

**Files:**
- Create: `tests/unit/auth/test_account_repository.py`
- Create: `atguigu_ai/auth/account_repository.py`
- Modify: `atguigu_ai/auth/__init__.py`

- [ ] **Step 1: Write repository contract tests**

Create tests covering:

- `AccountRecord` and `LockedAccount` immutable values expose only account id, email, normalized email, password hash, role, status, and verified timestamp;
- `AccountRepository.create_pending_consumer(email, normalized_email, password_hash)` inserts `AccountRole.consumer` and `AccountStatus.pending`;
- duplicate normalized email maps to `DuplicateAccountEmail("Account email already exists")`;
- `get_by_normalized_email()` returns `None` for missing rows;
- `lock_by_account_id()` issues a `SELECT ... FOR UPDATE` query and returns `None` when missing;
- `mark_email_verified()` changes only pending accounts to active and sets `email_verified_at`;
- `replace_password_hash()` updates the hash without returning or logging it;
- `record_audit()` writes sanitized metadata and rejects metadata containing keys named `password`, `token`, `session`, or `secret`;
- public exports from `atguigu_ai.auth` preserve all existing names and append repository names.

- [ ] **Step 2: Run RED**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth/test_account_repository.py -q
```

Expected: collection fails because `atguigu_ai.auth.account_repository` does not exist.

- [ ] **Step 3: Implement repository**

Create `atguigu_ai/auth/account_repository.py` with sync SQLAlchemy code using an injected `sqlalchemy.orm.Session`. Use UUID4 string IDs, timezone-aware UTC timestamps, `Account`, `AuditEvent`, `AccountRole`, `AccountStatus`, and `AuditResult` from `models.py`. Implement:

```python
class DuplicateAccountEmail(RuntimeError): ...
class AccountRepositoryUnavailable(RuntimeError): ...

@dataclass(frozen=True)
class AccountRecord: ...

class AccountRepository:
    def create_pending_consumer(self, email: str, normalized_email: str, password_hash: str) -> AccountRecord: ...
    def get_by_normalized_email(self, normalized_email: str) -> AccountRecord | None: ...
    def lock_by_account_id(self, account_id: str) -> AccountRecord | None: ...
    def mark_email_verified(self, account_id: str, verified_at: datetime) -> AccountRecord | None: ...
    def replace_password_hash(self, account_id: str, password_hash: str) -> None: ...
    def record_audit(self, *, request_id: str, actor_account_id: str | None, actor_role: AccountRole, event_type: str, target_type: str | None, target_id: str | None, result: AuditResult, metadata: Mapping[str, object] | None = None) -> None: ...
```

Do not commit transactions inside repository methods; the service/unit-of-work controls commit and rollback.

- [ ] **Step 4: Run GREEN and regression**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth/test_account_repository.py tests/unit/auth/test_models.py -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth tests/security -q
```

Expected: all tests pass; no existing public auth export is removed.

- [ ] **Step 5: Commit repository slice**

Run:

```powershell
git add atguigu_ai/auth/account_repository.py atguigu_ai/auth/__init__.py tests/unit/auth/test_account_repository.py
git diff --cached --check
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "feat: add account repository"
```

### Task 3: AuthService Unit Contract

**Files:**
- Create: `tests/unit/auth/test_auth_service.py`
- Create: `atguigu_ai/auth/service.py`
- Modify: `atguigu_ai/auth/__init__.py`

- [ ] **Step 1: Write AuthService unit tests with fakes**

Create fake repository/unit-of-work, fake hasher, fake credential-token store, fake session store, and fake email delivery. Cover:

- register creates pending account, hashes password, issues verify token, sends verification URL based on configured public base URL, and never stores raw token in repository metadata;
- duplicate register raises/returns the stable duplicate result without sending email;
- SMTP failure rolls back the account transaction and exposes `AuthServiceUnavailable("Authentication service is unavailable")`;
- verify email consumes token before opening durable mutation, locks the account, activates pending account, records audit, commits, and returns `AccountIdentity`;
- verify email returns `None` for missing/expired token, missing account, already active, or disabled account;
- login uses generic `InvalidCredentials("Invalid email or password")` for unknown email, pending, disabled, and wrong password;
- login does dummy password verification for unknown or ineligible accounts;
- login creates Redis Session only for active account + correct password;
- logout revokes the provided session token and is idempotent for malformed tokens;
- forgot password returns accepted for missing/pending/disabled accounts and only sends email for active accounts;
- reset password consumes reset token before lock, hashes new password, calls `revoke_all` while the row is locked, updates password, records audit, commits, and never restores consumed token after failure.

- [ ] **Step 2: Run RED**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth/test_auth_service.py -q
```

Expected: collection fails because `atguigu_ai.auth.service` does not exist.

- [ ] **Step 3: Implement AuthService**

Create `atguigu_ai/auth/service.py` with:

```python
class InvalidCredentials(RuntimeError): ...
class DuplicateRegistration(RuntimeError): ...
class AuthServiceUnavailable(RuntimeError): ...

@dataclass(frozen=True)
class RegistrationAccepted:
    account_id: str
    email: str

@dataclass(frozen=True)
class LoginAccepted:
    identity: AccountIdentity
    session: CreatedSession

@dataclass(frozen=True)
class PasswordResetAccepted:
    accepted: bool = True
```

`AuthService` constructor accepts repository unit-of-work factory, `PasswordHasher`, `RedisCredentialTokenStore`, `RedisSessionStore`, email delivery, `public_base_url`, and `clock`. All public methods are async. Sync repository work runs inside the injected unit-of-work; Argon2/Redis/email calls keep their existing async boundaries. Any dependency exception from repository, token store, session store, password hasher overload, or email delivery maps to `AuthServiceUnavailable("Authentication service is unavailable")` unless the contract says the public outcome is enumeration-safe accepted/invalid.

- [ ] **Step 4: Run GREEN and regression**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth/test_auth_service.py tests/unit/auth/test_account_repository.py tests/unit/email/test_delivery.py -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth tests/unit/email tests/security -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit service slice**

Run:

```powershell
git add atguigu_ai/auth/service.py atguigu_ai/auth/__init__.py tests/unit/auth/test_auth_service.py
git diff --cached --check
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "feat: add authentication service"
```

### Task 4: Real MySQL + Redis AuthService Integration

**Files:**
- Create: `tests/integration/test_auth_service_mysql_redis.py`
- Modify: `docs/TECHNICAL_DESIGN.md`

- [ ] **Step 1: Write integration tests**

Use the existing isolated MySQL database helper pattern from `tests/integration/test_account_migration.py` and the owned Redis DB15 harness from `tests/integration/test_redis_session.py`. Cover:

- register writes exactly one pending account and sends exactly one verification email;
- duplicate normalized email is rejected without a second account or email;
- verify activates the account and one-time token reuse fails;
- login before verification fails with generic invalid credentials;
- login after verification creates a Redis Session that resolves to the same account id/role/status;
- forgot-password for missing email returns accepted and sends no email;
- forgot-password for active account sends one reset email;
- reset-password changes the hash, revokes old sessions, and allows login with the new password only;
- Redis outage during token/session operations maps to sanitized service unavailable and leaves MySQL transaction in a consistent state;
- email delivery outage during registration rolls back pending account creation;
- DB cleanup leaves zero `llm_cs_test_%` databases and Redis DB15 size zero.

- [ ] **Step 2: Run focused integration**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_auth_service_mysql_redis.py -q -s -m integration
docker exec llm-cs-redis redis-cli -n 15 DBSIZE
```

Expected: integration tests pass and DB15 is `0`.

- [ ] **Step 3: Update technical design**

Add a short subsection under Auth module describing the concrete `AuthService` transaction ordering implemented by this slice and explicitly stating HTTP routes, cookies, CSRF, demo-data initialization, and real SMTP configuration remain later slices.

- [ ] **Step 4: Commit integration slice**

Run:

```powershell
git add tests/integration/test_auth_service_mysql_redis.py docs/TECHNICAL_DESIGN.md
git diff --cached --check
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "test: cover authentication service integration"
```

### Task 5: Evidence, Independent QA, and Final Commit Gate

**Files:**
- Create: `docs/reports/integration/2026-07-19-auth-service-email.md`
- Create: `docs/reports/integration/evidence/auth-service-email-unit.txt`
- Create: `docs/reports/integration/evidence/auth-service-email-integration.txt`
- Create: `docs/reports/integration/evidence/auth-service-email-regression.txt`
- Create: `docs/reports/integration/evidence/auth-service-email-full-suite.txt`
- Create: `docs/reports/integration/evidence/auth-service-email-secret-scan.txt`
- Create: `docs/reports/integration/evidence/auth-service-email-dbsize.txt`

- [ ] **Step 1: Run and capture verification commands**

Run and retain UTF-8 evidence:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth/test_auth_service.py tests/unit/auth/test_account_repository.py tests/unit/email/test_delivery.py -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_auth_service_mysql_redis.py -q -s -m integration
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth tests/unit/email tests/security -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m compileall -q atguigu_ai/auth atguigu_ai/email tests/unit/auth tests/unit/email tests/integration
git diff --check
docker exec llm-cs-redis redis-cli -n 15 DBSIZE
```

Expected: all tests/compile/whitespace checks exit `0`; Redis DB15 is `0`. If MySQL container is stopped, start the existing `llm-cs-mysql` container for the full suite and stop it back to its prior state after the run.

- [ ] **Step 2: Run scoped secret scan**

Run a scoped scan over changed planned artifacts for private keys, credential-bearing Redis URLs, SMTP passwords, raw reset/verification tokens, and `sk-*` token shapes. Fixture-only URLs or tokens must be either absent or explicitly documented as non-secret deterministic test fixtures.

- [ ] **Step 3: Independent QA review**

Dispatch/reuse a QA agent to rerun focused unit, integration, full regression, secret scan, MySQL temp database cleanup, Redis DB15 cleanup, and evidence UTF-8 checks. Require no open Critical/Important findings.

- [ ] **Step 4: Write report and complete plan**

Write `docs/reports/integration/2026-07-19-auth-service-email.md` summarizing command results, quantitative timings, dependency behavior, and residual risks. Check every evidence `.txt` with strict UTF-8 decoding. Mark all checkboxes complete only after the review gate passes.

- [ ] **Step 5: Final commit**

Run:

```powershell
git add atguigu_ai/auth/account_repository.py atguigu_ai/auth/service.py atguigu_ai/auth/__init__.py atguigu_ai/email/__init__.py atguigu_ai/email/delivery.py tests/unit/auth/test_account_repository.py tests/unit/auth/test_auth_service.py tests/unit/email/test_delivery.py tests/integration/test_auth_service_mysql_redis.py docs/TECHNICAL_DESIGN.md docs/superpowers/plans/2026-07-19-auth-service-email.md docs/reports/integration/2026-07-19-auth-service-email.md docs/reports/integration/evidence/auth-service-email-unit.txt docs/reports/integration/evidence/auth-service-email-integration.txt docs/reports/integration/evidence/auth-service-email-regression.txt docs/reports/integration/evidence/auth-service-email-full-suite.txt docs/reports/integration/evidence/auth-service-email-secret-scan.txt docs/reports/integration/evidence/auth-service-email-dbsize.txt
git diff --cached --check
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "docs: record authentication service verification"
git show --stat --oneline HEAD
git status --short
```

## Completion Gate

This slice is complete only when the AuthService can register, verify, login, logout, forgot-password, and reset-password through unit tests and real MySQL+Redis integration; email delivery has a fake and sanitized SMTP adapter; dependency outages have stable behavior; no raw passwords, raw credential tokens, SMTP credentials, Redis credential URLs, or private keys are present in planned artifacts; Redis DB15 and MySQL temporary databases are clean; final review and independent QA approve; and the final commit leaves a clean worktree. HTTP routes, cookies, CSRF, browser pages, real SMTP configuration, demo-data initialization, account deletion, rate limiting, and chat authorization remain later slices.
