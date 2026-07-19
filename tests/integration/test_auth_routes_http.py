from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

import httpx
import pytest
import pytest_asyncio
from alembic import command
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import Engine, URL
from sqlalchemy.orm import Session, sessionmaker

from ecs_demo.actions.db import build_database_url
from atguigu_ai.api.dependencies import AuthRouteDependencies
from atguigu_ai.api.server import create_app
from atguigu_ai.auth import (
    Account,
    AccountRepository,
    AccountStatus,
    AuthService,
    CredentialTokenPurpose,
    PasswordHasher,
    RedisCredentialTokenStore,
    RedisSessionStore,
)
from atguigu_ai.email import FakeEmailDelivery
from tests.integration.test_account_migration import (
    _admin_engine,
    _alembic_config,
    _isolated_mysql_database,
    _target_name,
)
from tests.integration.test_auth_service_mysql_redis import RepositoryUnitOfWork
from tests.integration.test_redis_session import (
    CONTAINER,
    assert_owned_container,
    client as redis_client,
    docker,
    recreate_owned_container,
    wait_for_redis,
)


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

NOW = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
OLD_PASSWORD = "old correct horse"
NEW_PASSWORD = "new correct horse"
VERIFY_TOKEN_1 = "V" * 43
VERIFY_TOKEN_2 = "W" * 43
RESET_TOKEN = "R" * 43
EXTRA_TOKEN = "S" * 43
SESSION_TOKEN_1 = "session-token-1"
SESSION_TOKEN_2 = "session-token-2"
SESSION_TOKEN_3 = "session-token-3"


@dataclass
class HttpFixture:
    engine: Engine
    redis: object
    email: FakeEmailDelivery
    service: AuthService
    sessions: RedisSessionStore


def _token_factory(tokens: list[str]):
    iterator = iter(tokens)
    return lambda: next(iterator)


@contextmanager
def _migrated_mysql_database() -> Iterator[Engine]:
    with _isolated_mysql_database() as database_url:
        config = _alembic_config(database_url)
        config.attributes["connection_url"] = database_url
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("MIGRATION_EXPECTED_TARGET", _target_name(database_url))
            command.upgrade(config, "head")

        engine = create_engine(database_url, pool_pre_ping=True)
        try:
            yield engine
        finally:
            engine.dispose()


@pytest_asyncio.fixture
async def http_fixture():
    assert_owned_container()
    await wait_for_redis()
    redis = redis_client()
    try:
        assert await redis.ping() is True
        await redis.flushdb()
        with _migrated_mysql_database() as engine:
            session_factory = sessionmaker(bind=engine, expire_on_commit=False)
            email = FakeEmailDelivery()
            sessions = RedisSessionStore(
                redis,
                token_factory=_token_factory(
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
                    token_factory=_token_factory(
                        [VERIFY_TOKEN_1, VERIFY_TOKEN_2, RESET_TOKEN, EXTRA_TOKEN]
                    ),
                    clock=lambda: NOW,
                ),
                sessions=sessions,
                email_delivery=email,
                public_base_url="https://customer.example.test/auth",
                clock=lambda: NOW,
            )
            yield HttpFixture(engine, redis, email, service, sessions)
    finally:
        try:
            await wait_for_redis()
            await redis.flushdb()
        finally:
            await redis.aclose()


@pytest_asyncio.fixture
async def client(http_fixture: HttpFixture):
    app = create_app(
        auth_deps=AuthRouteDependencies(
            service=http_fixture.service,
            sessions=http_fixture.sessions,
        ),
        enable_inspect=False,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
    ) as test_client:
        yield test_client


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


def _assert_no_temporary_mysql_databases() -> None:
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


def _assert_redis_db15_empty() -> None:
    assert_owned_container()
    result = docker("exec", CONTAINER, "redis-cli", "-n", "15", "DBSIZE")
    assert result.stdout.strip() == "0"


@pytest.fixture(scope="module", autouse=True)
def cleanup_guard():
    yield
    _assert_no_temporary_mysql_databases()
    _assert_redis_db15_empty()


def _set_account_status(engine: Engine, normalized_email: str, status: AccountStatus) -> None:
    with Session(engine) as session:
        account = session.execute(
            select(Account).where(Account.email_normalized == normalized_email)
        ).scalar_one()
        account.status = status.value
        session.commit()


def _cookies(response) -> list[str]:
    return response.headers.get_list("set-cookie")


async def _register(client: httpx.AsyncClient, email: str = " User@Example.com "):
    return await client.post(
        "/api/auth/register",
        json={"email": email, "password": OLD_PASSWORD},
    )


async def _verify(client: httpx.AsyncClient, token: str = VERIFY_TOKEN_1):
    return await client.post("/api/auth/verify-email", json={"token": token})


async def _register_and_verify(client: httpx.AsyncClient) -> None:
    assert (await _register(client)).status_code == 202
    assert (await _verify(client)).status_code == 200


async def _login(client: httpx.AsyncClient, password: str = OLD_PASSWORD):
    return await client.post(
        "/api/auth/login",
        json={"email": "user@example.com", "password": password},
    )


async def test_register_has_no_cookies_and_persists_pending_account(
    client: httpx.AsyncClient, http_fixture: HttpFixture
) -> None:
    response = await _register(client)

    assert response.status_code == 202
    assert response.json() == {"accepted": True}
    assert _cookies(response) == []
    assert _account_count(http_fixture.engine) == 1
    account = _account_for_email(http_fixture.engine, "user@example.com")
    assert account.status == AccountStatus.pending.value
    assert len(http_fixture.email.outbox) == 1
    assert http_fixture.email.outbox[0].purpose == "verify_email"


async def test_resend_verification_sends_only_for_pending_account(
    client: httpx.AsyncClient, http_fixture: HttpFixture
) -> None:
    await _register_and_verify(client)
    http_fixture.email.outbox.clear()

    active = await client.post("/api/auth/resend-verification", json={"email": "user@example.com"})
    missing = await client.post("/api/auth/resend-verification", json={"email": "missing@example.com"})

    assert active.json() == missing.json() == {"accepted": True}
    assert http_fixture.email.outbox == []
    disabled = await _register(client, "disabled@example.com")
    assert disabled.status_code == 202
    _set_account_status(http_fixture.engine, "disabled@example.com", AccountStatus.disabled)
    http_fixture.email.outbox.clear()
    disabled_resend = await client.post(
        "/api/auth/resend-verification", json={"email": "disabled@example.com"}
    )
    assert disabled_resend.status_code == 200
    assert disabled_resend.json() == {"accepted": True}
    assert http_fixture.email.outbox == []

    pending = await _register(client, "pending@example.com")
    resent = await client.post(
        "/api/auth/resend-verification", json={"email": "pending@example.com"}
    )
    assert pending.status_code == 202
    assert resent.status_code == 200
    assert [message.purpose for message in http_fixture.email.outbox] == [
        "verify_email",
        "verify_email",
    ]


async def test_login_sets_secure_cookies_and_account_me_resolves_identity(client: httpx.AsyncClient) -> None:
    await _register_and_verify(client)

    response = await _login(client)
    cookies = _cookies(response)
    me = await client.get("/api/account/me")

    assert response.status_code == 200
    assert response.json()["role"] == "consumer"
    session_cookie = next(cookie for cookie in cookies if cookie.startswith("auth_session="))
    csrf_cookie = next(cookie for cookie in cookies if cookie.startswith("auth_csrf="))
    assert "HttpOnly" in session_cookie
    assert "Secure" in session_cookie
    assert "SameSite=Lax" in session_cookie
    assert "HttpOnly" not in csrf_cookie
    assert "Secure" in csrf_cookie
    assert "SameSite=Lax" in csrf_cookie
    assert me.status_code == 200
    assert me.json() == response.json()


async def test_logout_requires_csrf_before_service_mutation_and_clears_cookies(
    client: httpx.AsyncClient, http_fixture: HttpFixture
) -> None:
    await _register_and_verify(client)
    assert (await _login(client)).status_code == 200
    session_token = client.cookies.get("auth_session")

    forbidden = await client.post("/api/auth/logout")
    assert forbidden.status_code == 403
    assert http_fixture.service is not None
    assert http_fixture.service._sessions is http_fixture.sessions
    assert client.cookies.get("auth_session") == session_token
    assert (await client.get("/api/account/me")).status_code == 200

    response = await client.post(
        "/api/auth/logout",
        headers={"X-CSRF-Token": client.cookies.get("auth_csrf")},
    )
    cleared = _cookies(response)
    assert response.status_code == 204
    assert any(cookie.startswith("auth_session=;") and "HttpOnly" in cookie for cookie in cleared)
    assert any(cookie.startswith("auth_csrf=;") for cookie in cleared)
    assert client.cookies.get("auth_session") is None
    assert (await client.get("/api/account/me")).status_code == 401


async def test_change_password_requires_csrf_invalidates_session_and_requires_new_password(
    client: httpx.AsyncClient, http_fixture: HttpFixture
) -> None:
    await _register_and_verify(client)
    assert (await _login(client)).status_code == 200
    old_hash = _account_for_email(http_fixture.engine, "user@example.com").password_hash
    payload = {"current_password": OLD_PASSWORD, "new_password": NEW_PASSWORD}

    forbidden = await client.post("/api/auth/change-password", json=payload)
    assert forbidden.status_code == 403
    assert _account_for_email(http_fixture.engine, "user@example.com").password_hash == old_hash

    response = await client.post(
        "/api/auth/change-password",
        headers={"X-CSRF-Token": client.cookies.get("auth_csrf")},
        json=payload,
    )
    assert response.status_code == 204
    assert client.cookies.get("auth_session") is None
    assert (await _login(client, OLD_PASSWORD)).status_code == 401
    assert (await _login(client, NEW_PASSWORD)).status_code == 200


async def test_forgot_and_reset_password_are_enumeration_safe(client: httpx.AsyncClient) -> None:
    await _register_and_verify(client)
    known = await client.post("/api/auth/forgot-password", json={"email": "user@example.com"})
    missing = await client.post("/api/auth/forgot-password", json={"email": "missing@example.com"})
    reset = await client.post(
        "/api/auth/reset-password",
        json={"token": VERIFY_TOKEN_2, "new_password": NEW_PASSWORD},
    )
    invalid_reset = await client.post(
        "/api/auth/reset-password",
        json={"token": "x" * 43, "new_password": NEW_PASSWORD},
    )

    assert known.json() == missing.json() == reset.json() == invalid_reset.json() == {"accepted": True}
    assert (await _login(client, OLD_PASSWORD)).status_code == 401
    assert (await _login(client, NEW_PASSWORD)).status_code == 200


async def test_verify_email_consumes_token_once_without_creating_login_cookies(client: httpx.AsyncClient) -> None:
    assert (await _register(client)).status_code == 202

    first = await _verify(client)
    second = await _verify(client)

    assert first.json() == second.json() == {"accepted": True}
    assert _cookies(first) == _cookies(second) == []
    assert client.cookies.get("auth_session") is None
    assert (await client.get("/api/account/me")).status_code == 401


async def test_redis_outage_returns_sanitized_503(client: httpx.AsyncClient) -> None:
    assert_owned_container()
    docker("stop", CONTAINER)
    try:
        response = await _register(client, "outage@example.com")
        assert response.status_code == 503
        assert response.json() == {"detail": "Authentication service is unavailable"}
        assert "redis" not in response.text.lower()
    finally:
        await recreate_owned_container()
