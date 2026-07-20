from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker

from atguigu_ai.api.dependencies import AuthRouteDependencies
from atguigu_ai.api.routes.chat import ChatRouteDependencies
from atguigu_ai.api.server import create_app
from atguigu_ai.auth import (
    AccountRepository,
    AuthService,
    BusinessIdentityResolver,
    CredentialTokenPurpose,
    PasswordHasher,
    RedisCredentialTokenStore,
    RedisSessionStore,
)
from atguigu_ai.email import SMTPEmailDelivery
from atguigu_ai.rate_limit import RedisRateLimiter


class RepositoryUnitOfWork:
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory
        self._session = None
        self.repository: AccountRepository | None = None

    def __enter__(self):
        self._session = self._session_factory()
        self.repository = AccountRepository(self._session)
        return self

    def commit(self) -> None:
        self._session.commit()

    def rollback(self) -> None:
        self._session.rollback()

    def __exit__(self, exc_type, exc, tb) -> None:
        self._session.close()


class ProductionBindingRepository:
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    def get_business_user_binding(self, account_id: str):
        with self._session_factory() as session:
            return AccountRepository(session).get_business_user_binding(account_id)


def build_production_auth_deps(
    *,
    environ: Mapping[str, str] | None = None,
    redis_factory: Any = Redis.from_url,
) -> AuthRouteDependencies:
    settings = os.environ if environ is None else environ
    redis = _build_redis(settings, redis_factory)
    session_factory = _build_session_factory(settings)
    sessions = RedisSessionStore(redis)
    service = _build_auth_service(settings, session_factory, redis, sessions)
    return AuthRouteDependencies(
        service=service,
        sessions=sessions,
        rate_limiter=RedisRateLimiter(redis),
    )


def build_production_chat_deps(
    *,
    environ: Mapping[str, str] | None = None,
    agent_factory: Any | None = None,
) -> ChatRouteDependencies:
    settings = os.environ if environ is None else environ
    agent_path = _production_agent_path(settings)
    factory = agent_factory or _load_agent
    agent = factory(agent_path)
    session_factory = _build_session_factory(settings)
    return ChatRouteDependencies(
        agent=agent,
        business_identity_resolver=BusinessIdentityResolver(
            ProductionBindingRepository(session_factory)
        ),
    )


def _build_redis(settings: Mapping[str, str], redis_factory: Any):
    redis = redis_factory(
        settings.get("REDIS_URL", "redis://127.0.0.1:6379/15"),
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    return redis


def _build_session_factory(settings: Mapping[str, str]):
    engine = create_engine(_build_database_url(settings), pool_pre_ping=True)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _build_auth_service(settings: Mapping[str, str], session_factory, redis, sessions):
    return AuthService(
        uow_factory=lambda: RepositoryUnitOfWork(session_factory),
        password_hasher=PasswordHasher(),
        credential_tokens=RedisCredentialTokenStore(
            redis,
            ttl_seconds={
                CredentialTokenPurpose.verify_email: _int_setting(settings, "AUTH_VERIFY_EMAIL_TOKEN_TTL_SECONDS", 1800),
                CredentialTokenPurpose.reset_password: _int_setting(settings, "AUTH_RESET_PASSWORD_TOKEN_TTL_SECONDS", 1800),
            },
        ),
        sessions=sessions,
        email_delivery=SMTPEmailDelivery(
            host=_required(settings, "SMTP_HOST"),
            port=_int_setting(settings, "SMTP_PORT", 587),
            username=settings.get("SMTP_USERNAME", ""),
            password=settings.get("SMTP_PASSWORD", ""),
            from_address=_required(settings, "SMTP_FROM_ADDRESS"),
            use_tls=_bool_setting(settings, "SMTP_USE_TLS", True),
        ),
        public_base_url=_required(settings, "AUTH_PUBLIC_BASE_URL"),
        clock=lambda: datetime.now(timezone.utc),
    )


def create_production_app(
    *,
    environ: Mapping[str, str] | None = None,
    redis_factory: Any = Redis.from_url,
    agent_factory: Any | None = None,
    enable_inspect: bool = False,
):
    settings = os.environ if environ is None else environ
    auth_deps = build_production_auth_deps(
        environ=settings,
        redis_factory=redis_factory,
    )
    chat_deps = None
    if _bool_setting(settings, "PRODUCTION_CHAT_ENABLED", agent_factory is not None):
        chat_deps = build_production_chat_deps(
            environ=settings,
            agent_factory=agent_factory,
        )
    return create_app(
        auth_deps=auth_deps,
        chat_deps=chat_deps,
        enable_inspect=enable_inspect,
    )


def _required(settings: Mapping[str, str], name: str) -> str:
    value = settings.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _int_setting(settings: Mapping[str, str], name: str, default: int) -> int:
    return int(settings.get(name, str(default)))


def _bool_setting(settings: Mapping[str, str], name: str, default: bool) -> bool:
    raw = settings.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _build_database_url(settings: Mapping[str, str]) -> URL:
    password = _required(settings, "MYSQL_PASSWORD")
    return URL.create(
        drivername="mysql+pymysql",
        username=settings.get("MYSQL_USER", "root"),
        password=password,
        host=settings.get("MYSQL_HOST", "127.0.0.1"),
        port=_int_setting(settings, "MYSQL_PORT", 3306),
        database=settings.get("MYSQL_DATABASE", "ecs"),
        query={"charset": "utf8mb4"},
    )


def _production_agent_path(settings: Mapping[str, str]) -> Path:
    raw = settings.get("PRODUCTION_AGENT_PATH", "ecs_demo")
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        raise RuntimeError("PRODUCTION_AGENT_PATH is invalid")
    return path


def _load_agent(path: Path):
    from atguigu_ai.agent.agent import Agent, AgentConfig

    return Agent.load(path, config=AgentConfig())


__all__ = [
    "ProductionBindingRepository",
    "RepositoryUnitOfWork",
    "build_production_auth_deps",
    "build_production_chat_deps",
    "create_production_app",
]
