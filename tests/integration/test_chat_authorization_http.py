from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

import httpx
import pytest
import pytest_asyncio
from alembic import command
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ecs_demo.actions.db import build_database_url
from atguigu_ai.api.dependencies import AuthRouteDependencies
from atguigu_ai.api.routes.chat import ChatRouteDependencies
from atguigu_ai.api.server import create_app
from atguigu_ai.auth import (
    Account,
    AccountRepository,
    AccountStatus,
    AccountUserBinding,
    AuthService,
    BusinessIdentityResolver,
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

NOW = datetime(2026, 7, 19, 14, 0, tzinfo=timezone.utc)
PASSWORD = "old correct horse"
VERIFY_TOKEN = "V" * 43
SESSION_TOKEN_1 = "session-token-1"
SESSION_TOKEN_2 = "session-token-2"
BUSINESS_USER_ID = "business-user-1"


class RecordingAgent:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, dict[str, Any]]] = []
        self.reset_calls: list[str] = []

    async def handle_message(
        self,
        message: str,
        sender_id: str,
        metadata: dict[str, Any],
    ):
        self.messages.append((message, sender_id, metadata))
        return type(
            "AgentResponse",
            (),
            {"messages": [{"text": "收到"}]},
        )()

    async def reset_tracker(self, sender_id: str) -> None:
        self.reset_calls.append(sender_id)


class SessionScopedBindingRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get_business_user_binding(self, account_id: str):
        with self._session_factory() as session:
            return AccountRepository(session).get_business_user_binding(account_id)


@dataclass
class ChatFixture:
    engine: Engine
    redis: object
    email: FakeEmailDelivery
    service: AuthService
    sessions: RedisSessionStore
    session_factory: sessionmaker[Session]
    agent: RecordingAgent


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
            with engine.begin() as connection:
                connection.execute(
                    text("INSERT INTO user_info (user_id) VALUES (:user_id)"),
                    {"user_id": BUSINESS_USER_ID},
                )
            yield engine
        finally:
            engine.dispose()


@pytest.fixture(scope="module", autouse=True)
def cleanup_guard():
    yield
    _assert_no_temporary_mysql_databases()
    _assert_redis_db15_empty()


@pytest_asyncio.fixture
async def chat_fixture():
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
                token_factory=_token_factory([SESSION_TOKEN_1, SESSION_TOKEN_2]),
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
                    token_factory=_token_factory([VERIFY_TOKEN]),
                    clock=lambda: NOW,
                ),
                sessions=sessions,
                email_delivery=email,
                public_base_url="https://customer.example.test/auth",
                clock=lambda: NOW,
            )
            yield ChatFixture(engine, redis, email, service, sessions, session_factory, RecordingAgent())
    finally:
        try:
            await wait_for_redis()
            await redis.flushdb()
        finally:
            await redis.aclose()


@pytest_asyncio.fixture
async def client(chat_fixture: ChatFixture):
    app = create_app(
        auth_deps=AuthRouteDependencies(
            service=chat_fixture.service,
            sessions=chat_fixture.sessions,
        ),
        chat_deps=ChatRouteDependencies(
            agent=chat_fixture.agent,
            business_identity_resolver=BusinessIdentityResolver(
                SessionScopedBindingRepository(chat_fixture.session_factory)
            ),
        ),
        enable_inspect=False,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
    ) as test_client:
        yield test_client


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


async def _register_verify_login_and_bind(
    client: httpx.AsyncClient,
    fixture: ChatFixture,
) -> str:
    register = await client.post(
        "/api/auth/register",
        json={"email": "user@example.com", "password": PASSWORD},
    )
    assert register.status_code == 202
    verify = await client.post("/api/auth/verify-email", json={"token": VERIFY_TOKEN})
    assert verify.status_code == 200
    account = _account_for_email(fixture.engine, "user@example.com")
    with Session(fixture.engine) as session:
        session.add(
            AccountUserBinding(
                account_id=account.account_id,
                user_id=BUSINESS_USER_ID,
                seed_version="seed-v1",
            )
        )
        session.commit()
    login = await client.post(
        "/api/auth/login",
        json={"email": "user@example.com", "password": PASSWORD},
    )
    assert login.status_code == 200
    return account.account_id


def _account_for_email(engine: Engine, normalized_email: str) -> Account:
    with Session(engine) as session:
        account = session.execute(
            select(Account).where(Account.email_normalized == normalized_email)
        ).scalar_one()
        session.expunge(account)
        return account


async def test_bound_account_can_chat_with_trusted_tracker_and_metadata(
    client: httpx.AsyncClient,
    chat_fixture: ChatFixture,
) -> None:
    account_id = await _register_verify_login_and_bind(client, chat_fixture)

    response = await client.post(
        "/api/chat/messages",
        headers={"X-CSRF-Token": client.cookies.get("auth_csrf")},
        json={
            "message": "我的订单到哪里了？",
            "sender": "attacker",
            "metadata": {
                "Account_ID": "attacker-account",
                "nested": {"user_id": "attacker-user"},
                "safe": "kept",
            },
        },
    )

    assert response.status_code == 200
    assert response.json()[0]["recipient_id"] == f"account:{account_id}"
    assert chat_fixture.agent.messages == [
        (
            "我的订单到哪里了？",
            f"account:{account_id}",
            {
                "nested": {},
                "safe": "kept",
                "account_id": account_id,
                "user_id": BUSINESS_USER_ID,
                "account_role": "consumer",
                "account_status": "active",
            },
        )
    ]


async def test_chat_rejects_missing_csrf_before_agent_or_binding_mutation(
    client: httpx.AsyncClient,
    chat_fixture: ChatFixture,
) -> None:
    await _register_verify_login_and_bind(client, chat_fixture)

    response = await client.post("/api/chat/messages", json={})

    assert response.status_code == 403
    assert chat_fixture.agent.messages == []


async def test_chat_requires_session_and_business_binding(
    client: httpx.AsyncClient,
    chat_fixture: ChatFixture,
) -> None:
    missing_session = await client.post("/api/chat/messages", json={})
    assert missing_session.status_code == 401

    register = await client.post(
        "/api/auth/register",
        json={"email": "unbound@example.com", "password": PASSWORD},
    )
    assert register.status_code == 202
    assert (await client.post("/api/auth/verify-email", json={"token": VERIFY_TOKEN})).status_code == 200
    login = await client.post(
        "/api/auth/login",
        json={"email": "unbound@example.com", "password": PASSWORD},
    )
    assert login.status_code == 200
    unbound = await client.post(
        "/api/chat/messages",
        headers={"X-CSRF-Token": client.cookies.get("auth_csrf")},
        json={"message": "hello"},
    )

    assert unbound.status_code == 409
    assert chat_fixture.agent.messages == []


async def test_disabled_account_cannot_chat_or_reset(
    client: httpx.AsyncClient,
    chat_fixture: ChatFixture,
) -> None:
    account_id = await _register_verify_login_and_bind(client, chat_fixture)
    with Session(chat_fixture.engine) as session:
        session.get(Account, account_id).status = AccountStatus.disabled.value
        session.commit()

    message = await client.post(
        "/api/chat/messages",
        headers={"X-CSRF-Token": client.cookies.get("auth_csrf")},
        json={"message": "hello"},
    )
    reset = await client.post(
        "/api/chat/reset",
        headers={"X-CSRF-Token": client.cookies.get("auth_csrf")},
    )

    assert message.status_code == 403
    assert reset.status_code == 403
    assert chat_fixture.agent.messages == []
    assert chat_fixture.agent.reset_calls == []


async def test_chat_reset_resets_only_authenticated_tracker(
    client: httpx.AsyncClient,
    chat_fixture: ChatFixture,
) -> None:
    account_id = await _register_verify_login_and_bind(client, chat_fixture)

    forbidden = await client.post("/api/chat/reset")
    response = await client.post(
        "/api/chat/reset",
        headers={"X-CSRF-Token": client.cookies.get("auth_csrf")},
        json={"session_id": "attacker"},
    )

    assert forbidden.status_code == 403
    assert response.status_code == 204
    assert chat_fixture.agent.reset_calls == [f"account:{account_id}"]


async def test_redis_session_outage_maps_to_sanitized_503(
    client: httpx.AsyncClient,
    chat_fixture: ChatFixture,
) -> None:
    await _register_verify_login_and_bind(client, chat_fixture)
    assert_owned_container()
    docker("stop", CONTAINER)
    try:
        response = await client.post(
            "/api/chat/messages",
            headers={"X-CSRF-Token": client.cookies.get("auth_csrf")},
            json={"message": "hello"},
        )
        assert response.status_code == 503
        assert response.json() == {"detail": "Authentication service is unavailable"}
        assert "secret" not in response.text
    finally:
        await recreate_owned_container()
        await wait_for_redis()


async def test_binding_resolver_outage_maps_to_sanitized_503(
    chat_fixture: ChatFixture,
) -> None:
    broken_engine = create_engine(
        build_database_url().set(database="llm_cs_missing_chat_auth_outage"),
        pool_pre_ping=True,
    )
    broken_session_factory = sessionmaker(bind=broken_engine, expire_on_commit=False)
    app = create_app(
        auth_deps=AuthRouteDependencies(
            service=chat_fixture.service,
            sessions=chat_fixture.sessions,
        ),
        chat_deps=ChatRouteDependencies(
            agent=chat_fixture.agent,
            business_identity_resolver=BusinessIdentityResolver(
                SessionScopedBindingRepository(broken_session_factory)
            ),
        ),
        enable_inspect=False,
    )
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as outage_client:
            account_id = await _register_verify_login_and_bind(outage_client, chat_fixture)
            response = await outage_client.post(
                "/api/chat/messages",
                headers={"X-CSRF-Token": outage_client.cookies.get("auth_csrf")},
                json={"message": "hello"},
            )
    finally:
        broken_engine.dispose()

    assert account_id
    assert response.status_code == 503
    assert response.json() == {"detail": "Chat authorization service is unavailable"}
    assert "secret" not in response.text
