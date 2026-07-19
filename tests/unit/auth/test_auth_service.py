from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import datetime, timedelta, timezone

import pytest

from atguigu_ai.email import EmailDeliveryUnavailable
from atguigu_ai.auth import (
    AccountIdentity,
    AccountRecord,
    AccountRole,
    AccountStatus,
    AuditResult,
    AuthService,
    AuthServiceUnavailable,
    CreatedSession,
    CredentialTokenPurpose,
    DuplicateAccountEmail,
    DuplicateRegistration,
    InvalidPassword,
    InvalidCredentials,
    IssuedCredentialToken,
    LoginAccepted,
    PasswordResetAccepted,
    RegistrationAccepted,
)


NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
VERIFY_TOKEN = "v" * 43
RESET_TOKEN = "r" * 43


def account(
    *,
    account_id: str = "00000000-0000-0000-0000-000000000001",
    email: str = "User@example.com",
    normalized: str = "user@example.com",
    password_hash: str = "hash-old-password",
    role: AccountRole = AccountRole.consumer,
    status: AccountStatus = AccountStatus.pending,
    verified_at: datetime | None = None,
) -> AccountRecord:
    return AccountRecord(
        account_id=account_id,
        email=email,
        email_normalized=normalized,
        password_hash=password_hash,
        role=role,
        status=status,
        email_verified_at=verified_at,
    )


class FakeRepository:
    def __init__(self) -> None:
        self.by_email: dict[str, AccountRecord] = {}
        self.by_id: dict[str, AccountRecord] = {}
        self.events: list[tuple] = []
        self.duplicate_on_create = False

    def create_pending_consumer(
        self,
        email: str,
        normalized_email: str,
        password_hash: str,
    ) -> AccountRecord:
        self.events.append(("create_pending_consumer", email, normalized_email, password_hash))
        if self.duplicate_on_create or normalized_email in self.by_email:
            raise DuplicateAccountEmail()
        created = account(
            email=email,
            normalized=normalized_email,
            password_hash=password_hash,
            status=AccountStatus.pending,
        )
        self.by_email[normalized_email] = created
        self.by_id[created.account_id] = created
        return created

    def get_by_normalized_email(self, normalized_email: str) -> AccountRecord | None:
        self.events.append(("get_by_normalized_email", normalized_email))
        return self.by_email.get(normalized_email)

    def lock_by_account_id(self, account_id: str) -> AccountRecord | None:
        self.events.append(("lock_by_account_id", account_id))
        return self.by_id.get(account_id)

    def mark_email_verified(self, account_id: str, verified_at: datetime) -> AccountRecord | None:
        self.events.append(("mark_email_verified", account_id, verified_at))
        current = self.by_id.get(account_id)
        if current is None or current.status is not AccountStatus.pending:
            return None
        updated = replace(
            current,
            status=AccountStatus.active,
            email_verified_at=verified_at,
        )
        self.by_id[account_id] = updated
        self.by_email[updated.email_normalized] = updated
        return updated

    def replace_password_hash(self, account_id: str, password_hash: str) -> None:
        self.events.append(("replace_password_hash", account_id, password_hash))
        current = self.by_id.get(account_id)
        if current is not None:
            updated = replace(current, password_hash=password_hash)
            self.by_id[account_id] = updated
            self.by_email[updated.email_normalized] = updated

    def record_audit(
        self,
        *,
        request_id: str,
        actor_account_id: str | None,
        actor_role: AccountRole,
        event_type: str,
        target_type: str | None,
        target_id: str | None,
        result: AuditResult,
        metadata: dict[str, object] | None = None,
    ) -> None:
        assert metadata is None or all(
            "token" not in key.lower()
            and "password" not in key.lower()
            and "session" not in key.lower()
            and "secret" not in key.lower()
            for key in metadata
        )
        self.events.append(
            (
                "record_audit",
                request_id,
                actor_account_id,
                actor_role,
                event_type,
                target_type,
                target_id,
                result,
                metadata,
            )
        )


class FakeUnitOfWork:
    def __init__(self, repository: FakeRepository, events: list[tuple]) -> None:
        self.repository = repository
        self.events = events
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self) -> FakeUnitOfWork:
        self.events.append(("uow_enter",))
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.events.append(("uow_exit", exc_type is not None))
        return False

    def commit(self) -> None:
        self.events.append(("commit",))
        self.commits += 1

    def rollback(self) -> None:
        self.events.append(("rollback",))
        self.rollbacks += 1


class FakeUnitOfWorkFactory:
    def __init__(self, repository: FakeRepository) -> None:
        self.repository = repository
        self.events: list[tuple] = []
        self.created: list[FakeUnitOfWork] = []

    def __call__(self) -> FakeUnitOfWork:
        uow = FakeUnitOfWork(self.repository, self.events)
        self.created.append(uow)
        return uow


class FakePasswordHasher:
    def __init__(self) -> None:
        self.hashes: list[str] = []
        self.verifications: list[tuple[str | None, str]] = []
        self.valid_hash_passwords: dict[str, str] = {}
        self.hash_error: Exception | None = None

    async def hash(self, password: str) -> str:
        self.hashes.append(password)
        if self.hash_error is not None:
            raise self.hash_error
        return f"hash-{password}"

    async def verify(self, password_hash: str | None, password: str) -> bool:
        self.verifications.append((password_hash, password))
        return self.valid_hash_passwords.get(password_hash or "") == password


class FakeCredentialTokenStore:
    def __init__(self) -> None:
        self.issued: list[tuple[str, CredentialTokenPurpose]] = []
        self.consumed: list[tuple[str, CredentialTokenPurpose]] = []
        self.restore_attempts: list[tuple[str, CredentialTokenPurpose]] = []
        self.consume_results: dict[tuple[str, CredentialTokenPurpose], str | None] = {}

    async def issue(
        self,
        account_id: str,
        purpose: CredentialTokenPurpose,
    ) -> IssuedCredentialToken:
        self.issued.append((account_id, purpose))
        token = VERIFY_TOKEN if purpose is CredentialTokenPurpose.verify_email else RESET_TOKEN
        return IssuedCredentialToken(token=token, expires_at=NOW + timedelta(minutes=30))

    async def consume(self, token: str, purpose: CredentialTokenPurpose) -> str | None:
        self.consumed.append((token, purpose))
        return self.consume_results.get((token, purpose))

    async def restore(self, token: str, purpose: CredentialTokenPurpose) -> None:
        self.restore_attempts.append((token, purpose))


class FakeSessionStore:
    def __init__(self) -> None:
        self.created: list[AccountIdentity] = []
        self.revoked: list[str] = []
        self.revoked_all: list[str] = []

    async def create(self, identity: AccountIdentity) -> CreatedSession:
        self.created.append(identity)
        return CreatedSession(
            token="session-token",
            expires_at=NOW + timedelta(days=7),
        )

    async def revoke(self, token: str) -> None:
        if not isinstance(token, str) or not token.strip():
            return None
        self.revoked.append(token)

    async def revoke_all(self, account_id: str) -> None:
        self.revoked_all.append(account_id)


class FakeEmailDelivery:
    def __init__(self) -> None:
        self.verifications: list[tuple[str, str]] = []
        self.resets: list[tuple[str, str]] = []
        self.error: Exception | None = None

    async def send_verification_email(self, recipient: str, url: str) -> None:
        if self.error is not None:
            raise self.error
        self.verifications.append((recipient, url))

    async def send_password_reset_email(self, recipient: str, url: str) -> None:
        if self.error is not None:
            raise self.error
        self.resets.append((recipient, url))


@dataclass
class ServiceFixture:
    repository: FakeRepository
    uow_factory: FakeUnitOfWorkFactory
    hasher: FakePasswordHasher
    tokens: FakeCredentialTokenStore
    sessions: FakeSessionStore
    email: FakeEmailDelivery
    service: AuthService


@pytest.fixture()
def fixture() -> ServiceFixture:
    repository = FakeRepository()
    uow_factory = FakeUnitOfWorkFactory(repository)
    hasher = FakePasswordHasher()
    tokens = FakeCredentialTokenStore()
    sessions = FakeSessionStore()
    email = FakeEmailDelivery()
    service = AuthService(
        uow_factory=uow_factory,
        password_hasher=hasher,
        credential_tokens=tokens,
        sessions=sessions,
        email_delivery=email,
        public_base_url="https://public.example/app/",
        clock=lambda: NOW,
    )
    return ServiceFixture(repository, uow_factory, hasher, tokens, sessions, email, service)


def test_public_auth_service_exports_are_exact_and_async():
    import atguigu_ai.auth as auth

    assert auth.__all__ == [
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
        "DuplicateAccountEmail",
        "AccountRepositoryUnavailable",
        "AccountRecord",
        "AccountRepository",
        "InvalidCredentials",
        "DuplicateRegistration",
        "AuthServiceUnavailable",
        "RegistrationAccepted",
        "LoginAccepted",
        "PasswordResetAccepted",
        "AuthService",
    ]
    assert inspect.iscoroutinefunction(AuthService.register)
    assert inspect.iscoroutinefunction(AuthService.verify_email)
    assert inspect.iscoroutinefunction(AuthService.login)
    assert inspect.iscoroutinefunction(AuthService.logout)
    assert inspect.iscoroutinefunction(AuthService.forgot_password)
    assert inspect.iscoroutinefunction(AuthService.reset_password)


def test_result_values_are_frozen() -> None:
    registration = RegistrationAccepted("account-id", "user@example.com")
    login = LoginAccepted(
        AccountIdentity("account-id", AccountRole.consumer, AccountStatus.active),
        CreatedSession("session-token", NOW),
    )
    reset = PasswordResetAccepted()

    with pytest.raises(FrozenInstanceError):
        registration.email = "other@example.com"
    with pytest.raises(FrozenInstanceError):
        login.session = CreatedSession("other", NOW)
    with pytest.raises(FrozenInstanceError):
        reset.accepted = False


@pytest.mark.asyncio
async def test_register_creates_pending_account_issues_verify_token_and_sends_public_url(
    fixture: ServiceFixture,
) -> None:
    result = await fixture.service.register(" User@Example.com ", "correct horse")

    assert result == RegistrationAccepted(
        account_id="00000000-0000-0000-0000-000000000001",
        email="User@example.com",
    )
    assert fixture.hasher.hashes == ["correct horse"]
    assert fixture.repository.events[0] == (
        "create_pending_consumer",
        "User@example.com",
        "user@example.com",
        "hash-correct horse",
    )
    assert fixture.tokens.issued == [
        ("00000000-0000-0000-0000-000000000001", CredentialTokenPurpose.verify_email)
    ]
    assert fixture.email.verifications == [
        (
            "User@example.com",
            f"https://public.example/app/verify-email?token={VERIFY_TOKEN}",
        )
    ]
    assert fixture.uow_factory.created[-1].commits == 1
    assert VERIFY_TOKEN not in repr(fixture.repository.events)


@pytest.mark.asyncio
async def test_duplicate_register_raises_stable_result_without_sending_email(
    fixture: ServiceFixture,
) -> None:
    fixture.repository.duplicate_on_create = True

    with pytest.raises(DuplicateRegistration) as captured:
        await fixture.service.register("User@example.com", "correct horse")

    assert str(captured.value) == "Account email already exists"
    assert fixture.tokens.issued == []
    assert fixture.email.verifications == []
    assert fixture.uow_factory.created[-1].rollbacks == 1


@pytest.mark.asyncio
async def test_register_email_delivery_failure_rolls_back_and_exposes_sanitized_unavailable(
    fixture: ServiceFixture,
) -> None:
    fixture.email.error = EmailDeliveryUnavailable("smtp password leaked")

    with pytest.raises(AuthServiceUnavailable) as captured:
        await fixture.service.register("User@example.com", "correct horse")

    assert str(captured.value) == "Authentication service is unavailable"
    assert captured.value.__cause__ is None
    assert "smtp password leaked" not in repr(captured.value)
    assert fixture.uow_factory.created[-1].rollbacks == 1
    assert fixture.uow_factory.created[-1].commits == 0


@pytest.mark.asyncio
async def test_verify_email_consumes_token_before_lock_activates_audits_and_commits(
    fixture: ServiceFixture,
) -> None:
    pending = account()
    fixture.repository.by_id[pending.account_id] = pending
    fixture.tokens.consume_results[(VERIFY_TOKEN, CredentialTokenPurpose.verify_email)] = pending.account_id

    result = await fixture.service.verify_email(VERIFY_TOKEN)

    assert result == AccountIdentity(
        pending.account_id,
        AccountRole.consumer,
        AccountStatus.active,
    )
    assert fixture.tokens.consumed == [(VERIFY_TOKEN, CredentialTokenPurpose.verify_email)]
    assert fixture.uow_factory.events[0] == ("uow_enter",)
    assert fixture.repository.events[:3] == [
        ("lock_by_account_id", pending.account_id),
        ("mark_email_verified", pending.account_id, NOW),
        (
            "record_audit",
            "auth-service",
            pending.account_id,
            AccountRole.consumer,
            "account.email_verified",
            "account",
            pending.account_id,
            AuditResult.success,
            None,
        ),
    ]
    assert fixture.uow_factory.created[-1].commits == 1


@pytest.mark.parametrize(
    "locked_account",
    [
        None,
        account(status=AccountStatus.active, verified_at=NOW),
        account(status=AccountStatus.disabled),
    ],
)
@pytest.mark.asyncio
async def test_verify_email_returns_none_for_missing_expired_ineligible_or_reused_token(
    fixture: ServiceFixture,
    locked_account: AccountRecord | None,
) -> None:
    if locked_account is not None:
        fixture.repository.by_id[locked_account.account_id] = locked_account
        account_id = locked_account.account_id
    else:
        account_id = "missing-account"
    fixture.tokens.consume_results[(VERIFY_TOKEN, CredentialTokenPurpose.verify_email)] = account_id

    assert await fixture.service.verify_email(VERIFY_TOKEN) is None
    assert fixture.uow_factory.created[-1].commits == 0

    assert await fixture.service.verify_email("missing-token") is None


@pytest.mark.parametrize(
    "stored_account",
    [
        None,
        account(status=AccountStatus.pending),
        account(status=AccountStatus.disabled),
        account(status=AccountStatus.active, password_hash="hash-other-password", verified_at=NOW),
    ],
)
@pytest.mark.asyncio
async def test_login_uses_generic_invalid_credentials_and_dummy_verify_for_unknown_or_ineligible(
    fixture: ServiceFixture,
    stored_account: AccountRecord | None,
) -> None:
    if stored_account is not None:
        fixture.repository.by_email[stored_account.email_normalized] = stored_account
    fixture.hasher.valid_hash_passwords = {"hash-old-password": "correct horse"}

    with pytest.raises(InvalidCredentials) as captured:
        await fixture.service.login("User@example.com", "correct horse")

    assert str(captured.value) == "Invalid email or password"
    assert fixture.sessions.created == []
    assert len(fixture.hasher.verifications) == 1
    if stored_account is None or stored_account.status is not AccountStatus.active:
        assert fixture.hasher.verifications[0][0] is None


@pytest.mark.asyncio
async def test_login_creates_session_only_for_active_account_and_correct_password(
    fixture: ServiceFixture,
) -> None:
    active = account(status=AccountStatus.active, verified_at=NOW)
    fixture.repository.by_email[active.email_normalized] = active
    fixture.hasher.valid_hash_passwords = {"hash-old-password": "correct horse"}

    result = await fixture.service.login("User@example.com", "correct horse")

    identity = AccountIdentity(active.account_id, AccountRole.consumer, AccountStatus.active)
    assert result == LoginAccepted(
        identity=identity,
        session=CreatedSession("session-token", NOW + timedelta(days=7)),
    )
    assert fixture.sessions.created == [identity]


@pytest.mark.asyncio
async def test_logout_revokes_supplied_token_and_is_idempotent_for_malformed_token(
    fixture: ServiceFixture,
) -> None:
    assert await fixture.service.logout("session-token") is None
    assert await fixture.service.logout("") is None
    assert await fixture.service.logout(None) is None

    assert fixture.sessions.revoked == ["session-token"]


@pytest.mark.parametrize(
    "stored_account",
    [
        None,
        account(status=AccountStatus.pending),
        account(status=AccountStatus.disabled),
    ],
)
@pytest.mark.asyncio
async def test_forgot_password_accepts_missing_pending_and_disabled_without_email(
    fixture: ServiceFixture,
    stored_account: AccountRecord | None,
) -> None:
    if stored_account is not None:
        fixture.repository.by_email[stored_account.email_normalized] = stored_account

    assert await fixture.service.forgot_password("User@example.com") == PasswordResetAccepted()
    assert fixture.tokens.issued == []
    assert fixture.email.resets == []


@pytest.mark.asyncio
async def test_forgot_password_sends_reset_email_only_for_active_account(
    fixture: ServiceFixture,
) -> None:
    active = account(status=AccountStatus.active, verified_at=NOW)
    fixture.repository.by_email[active.email_normalized] = active

    assert await fixture.service.forgot_password("User@example.com") == PasswordResetAccepted()

    assert fixture.tokens.issued == [(active.account_id, CredentialTokenPurpose.reset_password)]
    assert fixture.email.resets == [
        (
            active.email,
            f"https://public.example/app/reset-password?token={RESET_TOKEN}",
        )
    ]


@pytest.mark.asyncio
async def test_reset_password_consumes_before_lock_hashes_revokes_updates_audits_and_commits(
    fixture: ServiceFixture,
) -> None:
    active = account(status=AccountStatus.active, verified_at=NOW)
    fixture.repository.by_id[active.account_id] = active
    fixture.tokens.consume_results[(RESET_TOKEN, CredentialTokenPurpose.reset_password)] = active.account_id

    result = await fixture.service.reset_password(RESET_TOKEN, "new correct horse")

    assert result == PasswordResetAccepted()
    assert fixture.tokens.consumed == [(RESET_TOKEN, CredentialTokenPurpose.reset_password)]
    assert fixture.repository.events == [
        ("lock_by_account_id", active.account_id),
        ("replace_password_hash", active.account_id, "hash-new correct horse"),
        (
            "record_audit",
            "auth-service",
            active.account_id,
            AccountRole.consumer,
            "account.password_reset",
            "account",
            active.account_id,
            AuditResult.success,
            None,
        ),
    ]
    assert fixture.sessions.revoked_all == [active.account_id]
    assert fixture.uow_factory.created[-1].commits == 1


@pytest.mark.asyncio
async def test_reset_password_never_restores_consumed_token_after_downstream_failure(
    fixture: ServiceFixture,
) -> None:
    active = account(status=AccountStatus.active, verified_at=NOW)
    fixture.repository.by_id[active.account_id] = active
    fixture.tokens.consume_results[(RESET_TOKEN, CredentialTokenPurpose.reset_password)] = active.account_id
    fixture.hasher.hash_error = RuntimeError("argon secret")

    with pytest.raises(AuthServiceUnavailable):
        await fixture.service.reset_password(RESET_TOKEN, "new correct horse")

    assert fixture.tokens.consumed == [(RESET_TOKEN, CredentialTokenPurpose.reset_password)]
    assert fixture.tokens.restore_attempts == []
    assert fixture.uow_factory.created[-1].rollbacks == 1


@pytest.mark.asyncio
async def test_reset_password_preserves_invalid_new_password_validation(
    fixture: ServiceFixture,
) -> None:
    active = account(status=AccountStatus.active, verified_at=NOW)
    fixture.repository.by_id[active.account_id] = active
    fixture.tokens.consume_results[(RESET_TOKEN, CredentialTokenPurpose.reset_password)] = active.account_id
    fixture.hasher.hash_error = InvalidPassword("Password does not meet requirements")

    with pytest.raises(InvalidPassword) as captured:
        await fixture.service.reset_password(RESET_TOKEN, "short")

    assert str(captured.value) == "Password does not meet requirements"
    assert fixture.tokens.consumed == [(RESET_TOKEN, CredentialTokenPurpose.reset_password)]
    assert fixture.tokens.restore_attempts == []
    assert fixture.sessions.revoked_all == []
    assert fixture.uow_factory.created[-1].rollbacks == 1
