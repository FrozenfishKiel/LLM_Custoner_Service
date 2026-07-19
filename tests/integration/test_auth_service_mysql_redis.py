from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

import pytest
import pytest_asyncio
from alembic import command
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import Session, sessionmaker

from ecs_demo.actions.db import build_database_url
from atguigu_ai.auth import (
    Account,
    AccountRepository,
    AccountRole,
    AccountStatus,
    AuthService,
    AuthServiceUnavailable,
    CredentialTokenPurpose,
    DuplicateRegistration,
    InvalidCredentials,
    PasswordHasher,
    RedisCredentialTokenStore,
    RedisSessionStore,
)
from atguigu_ai.email import EmailDeliveryUnavailable, FakeEmailDelivery
from tests.integration.test_account_migration import (
    _admin_engine,
    _alembic_config,
    _isolated_mysql_database,
    _target_name,
)
from tests.integration.test_redis_session import (
    CONTAINER,
    assert_owned_container,
    client,
    docker,
    recreate_owned_container,
    wait_for_redis,
)


pytestmark = pytest.mark.integration

NOW = datetime(2026, 7, 19, 9, 0, tzinfo=timezone.utc)
OLD_PASSWORD = "old correct horse"
NEW_PASSWORD = "new correct horse"
VERIFY_TOKEN = "V" * 43
RESET_TOKEN = "R" * 43
SESSION_TOKEN_1 = "session-token-1"
SESSION_TOKEN_2 = "session-token-2"
SESSION_TOKEN_3 = "session-token-3"


class RepositoryUnitOfWork:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None
        self.repository: AccountRepository | None = None

    def __enter__(self) -> RepositoryUnitOfWork:
        self._session = self._session_factory()
        self.repository = AccountRepository(self._session)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._session is not None:
            if exc_type is not None:
                self.rollback()
            self._session.close()
        return False

    def commit(self) -> None:
        assert self._session is not None
        self._session.commit()

    def rollback(self) -> None:
        assert self._session is not None
        self._session.rollback()


class FailingEmailDelivery:
    async def send_verification_email(self, recipient: str, url: str) -> None:
        raise EmailDeliveryUnavailable("smtp-password-leaked")

    async def send_password_reset_email(self, recipient: str, url: str) -> None:
        raise EmailDeliveryUnavailable("smtp-password-leaked")


@dataclass
class IntegrationFixture:
    database_url: URL
    engine: Engine
    redis: object
    email: FakeEmailDelivery
    service: AuthService
    sessions: RedisSessionStore


def _token_factory(tokens: list[str]):
    iterator = iter(tokens)
    return lambda: next(iterator)


def _session_factory(tokens: list[str]):
    iterator = iter(tokens)
    return lambda: next(iterator)


@contextmanager
def _migrated_mysql_database() -> Iterator[tuple[URL, Engine]]:
    with _isolated_mysql_database() as database_url:
        config = _alembic_config(database_url)
        config.attributes["connection_url"] = database_url
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("MIGRATION_EXPECTED_TARGET", _target_name(database_url))
            command.upgrade(config, "head")

        engine = create_engine(database_url, pool_pre_ping=True)
        try:
            yield database_url, engine
        finally:
            engine.dispose()


@pytest_asyncio.fixture
async def integration_fixture():
    assert_owned_container()
    redis = client()
    try:
        assert await redis.ping() is True
        await redis.flushdb()
        with _migrated_mysql_database() as (database_url, engine):
            session_factory = sessionmaker(bind=engine, expire_on_commit=False)
            email = FakeEmailDelivery()
            sessions = RedisSessionStore(
                redis,
                token_factory=_session_factory(
                    [SESSION_TOKEN_1, SESSION_TOKEN_2, SESSION_TOKEN_3]
                ),
                clock=lambda: NOW,
            )
            service = AuthService(
                uow_factory=lambda: RepositoryUnitOfWork(session_factory),
                password_hasher=PasswordHasher(),
                credential_tokens=RedisCredentialTokenStore(
                    redis,
                    ttl_seconds={
                        CredentialTokenPurpose.verify_email: 300,
                        CredentialTokenPurpose.reset_password: 300,
                    },
                    token_factory=_token_factory([VERIFY_TOKEN, RESET_TOKEN]),
                    clock=lambda: NOW,
                ),
                sessions=sessions,
                email_delivery=email,
                public_base_url="https://customer.example.test/auth",
                clock=lambda: NOW,
            )
            yield IntegrationFixture(database_url, engine, redis, email, service, sessions)
    finally:
        try:
            await wait_for_redis()
            await redis.flushdb()
        finally:
            await redis.aclose()


def _account_count(engine: Engine) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(Account))


def _account_for_email(engine: Engine, normalized_email: str) -> Account:
    with Session(engine) as session:
        account = session.execute(
            select(Account).where(Account.email_normalized == normalized_email)
        ).scalar_one()
        session.expunge(account)
        return account


async def _register(fixture: IntegrationFixture, email: str = " User@Example.com "):
    return await fixture.service.register(email, OLD_PASSWORD)


async def _register_and_verify(fixture: IntegrationFixture):
    registered = await _register(fixture)
    assert await fixture.service.verify_email(VERIFY_TOKEN) is not None
    return registered


@pytest.mark.asyncio
async def test_register_writes_one_pending_account_and_one_verification_email(
    integration_fixture: IntegrationFixture,
) -> None:
    result = await _register(integration_fixture)

    assert _account_count(integration_fixture.engine) == 1
    account = _account_for_email(integration_fixture.engine, "user@example.com")
    assert result.account_id == account.account_id
    assert account.email == "User@example.com"
    assert account.role == AccountRole.consumer.value
    assert account.status == AccountStatus.pending.value
    assert account.email_verified_at is None
    assert len(integration_fixture.email.outbox) == 1
    assert integration_fixture.email.outbox[0].purpose == "verify_email"
    assert integration_fixture.email.outbox[0].recipient == "User@example.com"
    assert integration_fixture.email.outbox[0].url.endswith(f"token={VERIFY_TOKEN}")


@pytest.mark.asyncio
async def test_duplicate_normalized_email_is_rejected_without_second_account_or_email(
    integration_fixture: IntegrationFixture,
) -> None:
    await _register(integration_fixture, "User@Example.com")

    with pytest.raises(DuplicateRegistration):
        await integration_fixture.service.register(" user@example.COM ", OLD_PASSWORD)

    assert _account_count(integration_fixture.engine) == 1
    assert len(integration_fixture.email.outbox) == 1


@pytest.mark.asyncio
async def test_verify_activates_account_and_reusing_token_fails(
    integration_fixture: IntegrationFixture,
) -> None:
    registered = await _register(integration_fixture)

    identity = await integration_fixture.service.verify_email(VERIFY_TOKEN)

    assert identity is not None
    assert identity.account_id == registered.account_id
    assert identity.role is AccountRole.consumer
    assert identity.status is AccountStatus.active
    account = _account_for_email(integration_fixture.engine, "user@example.com")
    assert account.status == AccountStatus.active.value
    assert account.email_verified_at is not None
    assert await integration_fixture.service.verify_email(VERIFY_TOKEN) is None


@pytest.mark.asyncio
async def test_login_before_verification_fails_generic_invalid_credentials(
    integration_fixture: IntegrationFixture,
) -> None:
    await _register(integration_fixture)

    with pytest.raises(InvalidCredentials) as captured:
        await integration_fixture.service.login("user@example.com", OLD_PASSWORD)

    assert str(captured.value) == "Invalid email or password"


@pytest.mark.asyncio
async def test_login_after_verification_creates_resolvable_redis_session(
    integration_fixture: IntegrationFixture,
) -> None:
    registered = await _register_and_verify(integration_fixture)

    login = await integration_fixture.service.login("User@Example.com", OLD_PASSWORD)

    resolved = await integration_fixture.sessions.resolve(login.session.token)
    assert login.identity.account_id == registered.account_id
    assert resolved == login.identity
    assert resolved.account_id == registered.account_id
    assert resolved.role is AccountRole.consumer
    assert resolved.status is AccountStatus.active


@pytest.mark.asyncio
async def test_forgot_password_missing_email_is_accepted_without_email(
    integration_fixture: IntegrationFixture,
) -> None:
    result = await integration_fixture.service.forgot_password("missing@example.com")

    assert result.accepted is True
    assert integration_fixture.email.outbox == []


@pytest.mark.asyncio
async def test_forgot_password_active_account_sends_one_reset_email(
    integration_fixture: IntegrationFixture,
) -> None:
    await _register_and_verify(integration_fixture)
    integration_fixture.email.outbox.clear()

    result = await integration_fixture.service.forgot_password("USER@example.com")

    assert result.accepted is True
    assert len(integration_fixture.email.outbox) == 1
    assert integration_fixture.email.outbox[0].purpose == "reset_password"
    assert integration_fixture.email.outbox[0].url.endswith(f"token={RESET_TOKEN}")


@pytest.mark.asyncio
async def test_reset_password_changes_hash_revokes_sessions_and_requires_new_password(
    integration_fixture: IntegrationFixture,
) -> None:
    await _register_and_verify(integration_fixture)
    first_login = await integration_fixture.service.login("user@example.com", OLD_PASSWORD)
    old_hash = _account_for_email(integration_fixture.engine, "user@example.com").password_hash
    integration_fixture.email.outbox.clear()
    await integration_fixture.service.forgot_password("user@example.com")

    result = await integration_fixture.service.reset_password(RESET_TOKEN, NEW_PASSWORD)

    assert result is not None
    new_hash = _account_for_email(integration_fixture.engine, "user@example.com").password_hash
    assert new_hash != old_hash
    assert await integration_fixture.sessions.resolve(first_login.session.token) is None
    with pytest.raises(InvalidCredentials):
        await integration_fixture.service.login("user@example.com", OLD_PASSWORD)
    second_login = await integration_fixture.service.login("user@example.com", NEW_PASSWORD)
    assert second_login.session.token == SESSION_TOKEN_2


@pytest.mark.asyncio
async def test_redis_outage_during_token_or_session_operations_is_sanitized_and_mysql_consistent(
    integration_fixture: IntegrationFixture,
) -> None:
    assert_owned_container()
    docker("stop", CONTAINER)
    try:
        with pytest.raises(AuthServiceUnavailable) as registration_error:
            await integration_fixture.service.register("outage@example.com", OLD_PASSWORD)
        assert str(registration_error.value) == "Authentication service is unavailable"
        assert registration_error.value.__cause__ is None
        assert _account_count(integration_fixture.engine) == 0
    finally:
        await recreate_owned_container()

    recovered = client()
    try:
        await recovered.flushdb()
        session_factory = sessionmaker(bind=integration_fixture.engine, expire_on_commit=False)
        service = AuthService(
            uow_factory=lambda: RepositoryUnitOfWork(session_factory),
            password_hasher=PasswordHasher(),
            credential_tokens=RedisCredentialTokenStore(
                recovered,
                ttl_seconds={
                    CredentialTokenPurpose.verify_email: 300,
                    CredentialTokenPurpose.reset_password: 300,
                },
                token_factory=_token_factory([VERIFY_TOKEN]),
                clock=lambda: NOW,
            ),
            sessions=RedisSessionStore(
                recovered,
                token_factory=_session_factory([SESSION_TOKEN_1]),
                clock=lambda: NOW,
            ),
            email_delivery=integration_fixture.email,
            public_base_url="https://customer.example.test/auth",
            clock=lambda: NOW,
        )
        await service.register("user@example.com", OLD_PASSWORD)
        assert await service.verify_email(VERIFY_TOKEN) is not None

        docker("stop", CONTAINER)
        try:
            with pytest.raises(AuthServiceUnavailable) as login_error:
                await service.login("user@example.com", OLD_PASSWORD)
            assert str(login_error.value) == "Authentication service is unavailable"
            assert login_error.value.__cause__ is None
            assert _account_for_email(
                integration_fixture.engine,
                "user@example.com",
            ).status == AccountStatus.active.value
        finally:
            await recreate_owned_container()
    finally:
        recovered_after_restart = client()
        try:
            await recovered_after_restart.flushdb()
        finally:
            await recovered_after_restart.aclose()
        await recovered.aclose()


@pytest.mark.asyncio
async def test_email_delivery_outage_during_registration_rolls_back_pending_account(
    integration_fixture: IntegrationFixture,
) -> None:
    session_factory = sessionmaker(bind=integration_fixture.engine, expire_on_commit=False)
    service = AuthService(
        uow_factory=lambda: RepositoryUnitOfWork(session_factory),
        password_hasher=PasswordHasher(),
        credential_tokens=RedisCredentialTokenStore(
            integration_fixture.redis,
            ttl_seconds={
                CredentialTokenPurpose.verify_email: 300,
                CredentialTokenPurpose.reset_password: 300,
            },
            token_factory=_token_factory([VERIFY_TOKEN]),
            clock=lambda: NOW,
        ),
        sessions=integration_fixture.sessions,
        email_delivery=FailingEmailDelivery(),
        public_base_url="https://customer.example.test/auth",
        clock=lambda: NOW,
    )

    with pytest.raises(AuthServiceUnavailable) as captured:
        await service.register("user@example.com", OLD_PASSWORD)

    assert str(captured.value) == "Authentication service is unavailable"
    assert captured.value.__cause__ is None
    assert "smtp-password-leaked" not in repr(captured.value)
    assert _account_count(integration_fixture.engine) == 0


def test_cleanup_leaves_no_temporary_mysql_databases_and_redis_db15_empty() -> None:
    admin_engine = _admin_engine(build_database_url())
    try:
        with admin_engine.connect() as connection:
            databases = [
                row[0]
                for row in connection.execute(text("SHOW DATABASES LIKE 'llm_cs_test_%'"))
            ]
        assert databases == []
    finally:
        admin_engine.dispose()

    assert_owned_container()
    result = docker("exec", CONTAINER, "redis-cli", "-n", "15", "DBSIZE")
    assert result.stdout.strip() == "0"
