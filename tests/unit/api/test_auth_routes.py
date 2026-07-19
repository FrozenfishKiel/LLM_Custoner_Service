from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from atguigu_ai.api.dependencies import AuthRouteDependencies
from atguigu_ai.api.server import create_app
from atguigu_ai.auth import (
    AccountIdentity,
    AccountRole,
    AccountStatus,
    CreatedSession,
    DuplicateRegistration,
    InvalidCredentials,
    LoginAccepted,
    PasswordResetAccepted,
    RegistrationAccepted,
)


NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


@dataclass
class FakeSessionStore:
    identities: dict[str, AccountIdentity]

    def __init__(self) -> None:
        self.identities = {
            "session-token": AccountIdentity(
                "account-1",
                AccountRole.consumer,
                AccountStatus.active,
            )
        }
        self.resolved: list[str] = []
        self.revoked: list[str] = []
        self.revoked_all: list[str] = []

    async def resolve(self, token: str) -> AccountIdentity | None:
        self.resolved.append(token)
        return self.identities.get(token)

    async def revoke(self, token: str) -> None:
        self.revoked.append(token)

    async def revoke_all(self, account_id: str) -> None:
        self.revoked_all.append(account_id)


class FakeAuthService:
    def __init__(self) -> None:
        self.login_error: Exception | None = None
        self.register_error: Exception | None = None
        self.login_calls: list[tuple[str, str]] = []
        self.register_calls: list[tuple[str, str]] = []
        self.logout_calls: list[str] = []
        self.verify_email_calls: list[str] = []
        self.forgot_password_calls: list[str] = []
        self.reset_password_calls: list[tuple[str, str]] = []
        self.resend_verification_calls: list[str] = []
        self.change_password_calls: list[tuple[str, str, str]] = []
        self.login_identity = AccountIdentity(
            "account-1",
            AccountRole.consumer,
            AccountStatus.active,
        )
        self.session = CreatedSession("session-token", NOW + timedelta(days=7))
        self.verify_email_result: AccountIdentity | None = self.login_identity
        self.register_result = RegistrationAccepted("account-1", "User@example.com")
        self.accepted = PasswordResetAccepted()

    async def register(self, email: str, password: str) -> RegistrationAccepted:
        self.register_calls.append((email, password))
        if self.register_error is not None:
            raise self.register_error
        return self.register_result

    async def verify_email(self, token: str) -> AccountIdentity | None:
        self.verify_email_calls.append(token)
        return self.verify_email_result

    async def login(self, email: str, password: str) -> LoginAccepted:
        self.login_calls.append((email, password))
        if self.login_error is not None:
            raise self.login_error
        return LoginAccepted(identity=self.login_identity, session=self.session)

    async def logout(self, session_token: str) -> None:
        self.logout_calls.append(session_token)

    async def forgot_password(self, email: str) -> PasswordResetAccepted:
        self.forgot_password_calls.append(email)
        return self.accepted

    async def reset_password(self, token: str, new_password: str) -> PasswordResetAccepted | None:
        self.reset_password_calls.append((token, new_password))
        return self.accepted if token == "reset-token" else None

    async def resend_verification(self, email: str) -> PasswordResetAccepted:
        self.resend_verification_calls.append(email)
        return self.accepted

    async def change_password(self, account_id: str, current_password: str, new_password: str) -> None:
        self.change_password_calls.append((account_id, current_password, new_password))


def build_client(*, service: FakeAuthService | None = None, sessions: FakeSessionStore | None = None) -> tuple[TestClient, FakeAuthService, FakeSessionStore]:
    auth_service = service or FakeAuthService()
    auth_sessions = sessions or FakeSessionStore()
    deps = AuthRouteDependencies(service=auth_service, sessions=auth_sessions)
    app = create_app(auth_deps=deps, enable_inspect=False)
    return TestClient(app, base_url="https://testserver"), auth_service, auth_sessions


def cookie_dump(response) -> str:
    return "\n".join(response.headers.get_list("set-cookie"))


def cookie_line(response, name: str) -> str:
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{name}="):
            return header
    raise AssertionError(f"missing Set-Cookie for {name}")


def test_login_sets_session_and_csrf_cookies_and_returns_identity() -> None:
    client, service, _ = build_client()

    response = client.post(
        "/api/auth/login",
        json={"email": "User@example.com", "password": "old correct horse"},
    )

    cookies = cookie_dump(response)
    assert response.status_code == 200
    assert response.json()["account_id"] == "account-1"
    assert response.json()["role"] == "consumer"
    assert response.json()["status"] == "active"
    session_cookie = cookie_line(response, "auth_session")
    csrf_cookie = cookie_line(response, "auth_csrf")
    assert "auth_session=" in session_cookie
    assert "HttpOnly" in session_cookie
    assert "Secure" in session_cookie
    assert "SameSite=Lax" in session_cookie
    assert "auth_csrf=" in csrf_cookie
    assert "HttpOnly" not in csrf_cookie
    assert "Secure" in csrf_cookie
    assert "SameSite=Lax" in csrf_cookie
    assert service.login_calls == [("User@example.com", "old correct horse")]


def test_login_failure_does_not_set_cookies() -> None:
    client, service, _ = build_client()
    service.login_error = InvalidCredentials()

    response = client.post(
        "/api/auth/login",
        json={"email": "User@example.com", "password": "wrong"},
    )

    assert response.status_code == 401
    assert cookie_dump(response) == ""


def test_register_returns_accepted_without_cookies() -> None:
    client, service, _ = build_client()

    response = client.post(
        "/api/auth/register",
        json={"email": "User@example.com", "password": "correct horse"},
    )

    assert response.status_code == 202
    assert response.json()["accepted"] is True
    assert cookie_dump(response) == ""
    assert service.register_calls == [("User@example.com", "correct horse")]


def test_register_duplicate_email_remains_enumeration_safe() -> None:
    client, service, _ = build_client()
    service.register_error = DuplicateRegistration()

    response = client.post(
        "/api/auth/register",
        json={"email": "User@example.com", "password": "correct horse"},
    )

    assert response.status_code == 202
    assert response.json() == {"accepted": True}
    assert cookie_dump(response) == ""


def test_auth_routes_reject_wildcard_credentialed_cors() -> None:
    service = FakeAuthService()
    sessions = FakeSessionStore()
    deps = AuthRouteDependencies(service=service, sessions=sessions)

    with pytest.raises(ValueError, match="trusted CORS origins"):
        create_app(auth_deps=deps, cors_origins=["*"], enable_inspect=False)


def test_verify_email_stays_enumeration_safe() -> None:
    client, service, _ = build_client()
    service.verify_email_result = None

    known = client.post("/api/auth/verify-email", json={"token": "known-token"})
    unknown = client.post("/api/auth/verify-email", json={"token": "unknown-token"})

    assert known.status_code == 200
    assert unknown.status_code == 200
    assert known.json() == unknown.json() == {"accepted": True}
    assert service.verify_email_calls == ["known-token", "unknown-token"]


def test_resend_verification_stays_enumeration_safe() -> None:
    client, service, _ = build_client()

    response = client.post("/api/auth/resend-verification", json={"email": "User@example.com"})

    assert response.status_code == 200
    assert response.json() == {"accepted": True}
    assert cookie_dump(response) == ""
    assert service.resend_verification_calls == ["User@example.com"]


def test_forgot_password_stays_enumeration_safe() -> None:
    client, service, _ = build_client()

    known = client.post("/api/auth/forgot-password", json={"email": "User@example.com"})
    unknown = client.post("/api/auth/forgot-password", json={"email": "missing@example.com"})

    assert known.status_code == 200
    assert unknown.status_code == 200
    assert known.json() == unknown.json() == {"accepted": True}
    assert service.forgot_password_calls == ["User@example.com", "missing@example.com"]


def test_reset_password_stays_enumeration_safe() -> None:
    client, service, _ = build_client()

    known = client.post(
        "/api/auth/reset-password",
        json={"token": "reset-token", "new_password": "new correct horse"},
    )
    unknown = client.post(
        "/api/auth/reset-password",
        json={"token": "unknown-token", "new_password": "new correct horse"},
    )

    assert known.status_code == 200
    assert unknown.status_code == 200
    assert known.json() == unknown.json() == {"accepted": True}
    assert service.reset_password_calls == [
        ("reset-token", "new correct horse"),
        ("unknown-token", "new correct horse"),
    ]


def test_change_password_requires_matching_csrf_header() -> None:
    client, service, _ = build_client()

    response = client.post(
        "/api/auth/change-password",
        cookies={"auth_session": "session-token", "auth_csrf": "csrf-token"},
        json={
            "current_password": "old correct horse",
            "new_password": "new correct horse",
        },
    )

    assert response.status_code == 403
    assert service.change_password_calls == []


def test_change_password_requires_session_delegates_and_clears_cookies() -> None:
    client, service, _ = build_client()

    missing_session = client.post(
        "/api/auth/change-password",
        cookies={"auth_csrf": "csrf-token"},
        headers={"X-CSRF-Token": "csrf-token"},
        json={
            "current_password": "old correct horse",
            "new_password": "new correct horse",
        },
    )
    assert missing_session.status_code == 401

    response = client.post(
        "/api/auth/change-password",
        cookies={"auth_session": "session-token", "auth_csrf": "csrf-token"},
        headers={"X-CSRF-Token": "csrf-token"},
        json={
            "current_password": "old correct horse",
            "new_password": "new correct horse",
        },
    )

    cookies = cookie_dump(response)
    assert response.status_code == 204
    assert "auth_session=;" in cookies
    assert "auth_csrf=;" in cookies
    assert service.change_password_calls == [
        ("account-1", "old correct horse", "new correct horse")
    ]


def test_logout_requires_csrf_and_clears_cookies() -> None:
    client, service, _ = build_client()

    forbidden = client.post("/api/auth/logout", cookies={"auth_session": "session-token"})
    assert forbidden.status_code == 403

    response = client.post(
        "/api/auth/logout",
        cookies={"auth_session": "session-token", "auth_csrf": "csrf-token"},
        headers={"X-CSRF-Token": "csrf-token"},
    )

    cookies = cookie_dump(response)
    assert response.status_code == 204
    assert "auth_session=;" in cookies
    assert "auth_csrf=;" in cookies
    assert "Max-Age=0" in cookies
    assert service.logout_calls == ["session-token"]


def test_logout_with_csrf_is_noop_without_session() -> None:
    client, service, _ = build_client()

    response = client.post(
        "/api/auth/logout",
        cookies={"auth_csrf": "csrf-token"},
        headers={"X-CSRF-Token": "csrf-token"},
    )

    assert response.status_code == 204
    assert "auth_session=;" in cookie_dump(response)
    assert service.logout_calls == []


@pytest.mark.parametrize("session_token", [None, "missing-token"])
def test_account_me_rejects_missing_or_invalid_session(session_token: str | None) -> None:
    client, _, _ = build_client()
    cookies = {"auth_session": session_token} if session_token is not None else {}

    response = client.get("/api/account/me", cookies=cookies)

    assert response.status_code == 401


def test_account_me_reads_identity_from_session_cookie() -> None:
    client, _, _ = build_client()

    response = client.get("/api/account/me", cookies={"auth_session": "session-token"})

    assert response.status_code == 200
    assert response.json()["account_id"] == "account-1"
    assert response.json()["role"] == "consumer"
    assert response.json()["status"] == "active"
