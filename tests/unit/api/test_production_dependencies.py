from __future__ import annotations

import os
import subprocess
import sys

from atguigu_ai.api.production import build_production_auth_deps, create_production_app
from atguigu_ai.auth import AuthService, RedisCredentialTokenStore, RedisSessionStore
from atguigu_ai.email import SMTPEmailDelivery
from atguigu_ai.rate_limit import RedisRateLimiter
from fastapi.testclient import TestClient


class FakeRedis:
    async def eval(self, *_args):
        return [1, 1, 60]


def _settings() -> dict[str, str]:
    return {
        "MYSQL_HOST": "db.internal.test",
        "MYSQL_PORT": "3306",
        "MYSQL_DATABASE": "ecs",
        "MYSQL_USER": "service_user",
        "MYSQL_PASSWORD": "mysql-secret",
        "REDIS_URL": "redis://127.0.0.1:6379/15",
        "AUTH_PUBLIC_BASE_URL": "https://customer.example.test/auth",
        "SMTP_HOST": "smtp.example.test",
        "SMTP_PORT": "587",
        "SMTP_USERNAME": "mailer",
        "SMTP_PASSWORD": "smtp-secret",
        "SMTP_FROM_ADDRESS": "noreply@example.test",
        "SMTP_USE_TLS": "true",
    }


def test_build_production_auth_deps_injects_rate_limiter_with_shared_redis_client() -> None:
    created_urls: list[str] = []

    def redis_factory(url: str, **kwargs):
        created_urls.append(url)
        assert kwargs["decode_responses"] is True
        return FakeRedis()

    deps = build_production_auth_deps(environ=_settings(), redis_factory=redis_factory)

    assert isinstance(deps.service, AuthService)
    assert isinstance(deps.sessions, RedisSessionStore)
    assert isinstance(deps.service._credential_tokens, RedisCredentialTokenStore)
    assert isinstance(deps.service._email_delivery, SMTPEmailDelivery)
    assert isinstance(deps.rate_limiter, RedisRateLimiter)
    assert deps.rate_limiter._redis is deps.sessions._redis
    assert deps.service._credential_tokens._redis is deps.sessions._redis
    assert created_urls == ["redis://127.0.0.1:6379/15"]


def test_build_production_auth_deps_requires_public_base_url() -> None:
    settings = _settings()
    settings.pop("AUTH_PUBLIC_BASE_URL")

    try:
        build_production_auth_deps(environ=settings, redis_factory=lambda *_args, **_kwargs: FakeRedis())
    except RuntimeError as exc:
        assert str(exc) == "AUTH_PUBLIC_BASE_URL is required"
    else:
        raise AssertionError("missing AUTH_PUBLIC_BASE_URL should fail")


def test_create_production_app_registers_auth_dependencies_with_rate_limiter() -> None:
    app = create_production_app(
        environ=_settings(),
        redis_factory=lambda *_args, **_kwargs: FakeRedis(),
        enable_inspect=False,
    )

    assert isinstance(app.state.auth_deps.rate_limiter, RedisRateLimiter)
    response = TestClient(app).post(
        "/api/auth/login",
        json={"email": "user@example.com", "password": "wrong"},
    )
    assert response.status_code in {401, 503}


def test_production_helpers_are_exported_from_api_package() -> None:
    import atguigu_ai.api as api

    assert api.build_production_auth_deps is build_production_auth_deps
    assert api.create_production_app is create_production_app


def test_importing_api_package_does_not_require_database_environment() -> None:
    environment = os.environ.copy()
    for name in ("MYSQL_HOST", "MYSQL_PORT", "MYSQL_DATABASE", "MYSQL_USER", "MYSQL_PASSWORD"):
        environment.pop(name, None)

    child_code = """
import sys

class RejectActionDbImport:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "ecs_demo.actions.db":
            raise RuntimeError("ecs_demo.actions.db imported")
        return None

sys.meta_path.insert(0, RejectActionDbImport())
import atguigu_ai.api
print("API_IMPORT_OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", child_code],
        cwd=os.getcwd(),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "API_IMPORT_OK" in result.stdout
