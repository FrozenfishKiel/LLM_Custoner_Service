from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from fastapi.testclient import TestClient

from atguigu_ai.api.dependencies import AuthRouteDependencies
from atguigu_ai.api.routes.chat import ChatRouteDependencies, create_chat_router
from atguigu_ai.api.server import create_app
from atguigu_ai.auth import AccountIdentity, AccountRole, AccountStatus, AuthService
from atguigu_ai.auth.business_identity import (
    BusinessUserBindingUnavailable,
    BusinessUserIdentity,
    BusinessUserNotBound,
)
from atguigu_ai.rate_limit import RateLimitStoreUnavailable


class FakeAuthService:
    pass


class FakeSessions:
    def __init__(self) -> None:
        self.identity: AccountIdentity | None = AccountIdentity(
            "account-1", AccountRole.consumer, AccountStatus.active
        )
        self.error: Exception | None = None

    async def resolve(self, token: str) -> AccountIdentity | None:
        if self.error is not None:
            raise self.error
        return self.identity if token == "session-token" else None


class FakeBusinessIdentityResolver:
    def __init__(self) -> None:
        self.identity = BusinessUserIdentity(
            account_id="account-1",
            user_id="business-user-1",
            role=AccountRole.consumer,
            account_status=AccountStatus.active,
        )
        self.error: Exception | None = None
        self.calls: list[AccountIdentity] = []

    async def resolve(self, identity: AccountIdentity) -> BusinessUserIdentity:
        self.calls.append(identity)
        if self.error is not None:
            raise self.error
        return self.identity


class FakeAgent:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, dict[str, object]]] = []
        self.reset_calls: list[str] = []

    async def handle_message(
        self, message: str, sender_id: str, metadata: dict[str, object]
    ) -> SimpleNamespace:
        self.messages.append((message, sender_id, metadata))
        return SimpleNamespace(messages=[{"text": "received"}])

    async def reset_tracker(self, sender_id: str) -> None:
        self.reset_calls.append(sender_id)


class FakeRateLimiter:
    def __init__(self) -> None:
        self.blocked_rules: set[str] = set()
        self.unavailable_rules: set[str] = set()
        self.calls: list[tuple[str, str]] = []

    async def check(self, rule, subject: str):
        self.calls.append((rule.name, subject))
        if rule.name in self.unavailable_rules:
            raise RateLimitStoreUnavailable()
        allowed = rule.name not in self.blocked_rules
        return SimpleNamespace(
            allowed=allowed,
            limit=rule.limit,
            remaining=rule.limit - 1 if allowed else 0,
            retry_after_seconds=0 if allowed else 60,
            reset_after_seconds=60,
            rule_name=rule.name,
        )


def build_client(
    *,
    rate_limiter: FakeRateLimiter | None = None,
) -> tuple[TestClient, FakeAgent, FakeSessions, FakeBusinessIdentityResolver]:
    sessions = FakeSessions()
    resolver = FakeBusinessIdentityResolver()
    agent = FakeAgent()
    auth_deps = AuthRouteDependencies(
        service=FakeAuthService(),
        sessions=sessions,
        rate_limiter=rate_limiter,
    )
    app = create_app(auth_deps=auth_deps, enable_inspect=False)
    app.include_router(create_chat_router(ChatRouteDependencies(agent, resolver)))
    return TestClient(app, base_url="https://testserver"), agent, sessions, resolver


def build_client_with_rate_limiter() -> tuple[TestClient, FakeAgent, FakeSessions, FakeBusinessIdentityResolver, FakeRateLimiter]:
    limiter = FakeRateLimiter()
    client, agent, sessions, resolver = build_client(rate_limiter=limiter)
    return client, agent, sessions, resolver, limiter


def authenticated_request(client: TestClient, path: str, payload: dict[str, object] | None = None):
    return client.post(
        path,
        cookies={"auth_session": "session-token", "auth_csrf": "csrf-token"},
        headers={"X-CSRF-Token": "csrf-token"},
        json=payload,
    )


def test_chat_message_requires_missing_or_invalid_session() -> None:
    client, agent, _, _ = build_client()

    no_cookies = client.post("/api/chat/messages", json={"message": "hello"})
    no_cookies_invalid_body = client.post("/api/chat/messages", json={})
    missing = client.post(
        "/api/chat/messages",
        cookies={"auth_csrf": "csrf-token"},
        headers={"X-CSRF-Token": "csrf-token"},
        json={"message": "hello"},
    )
    invalid = client.post(
        "/api/chat/messages",
        cookies={"auth_session": "invalid-session", "auth_csrf": "csrf-token"},
        headers={"X-CSRF-Token": "csrf-token"},
        json={"message": "hello"},
    )

    assert no_cookies.status_code == 401
    assert no_cookies_invalid_body.status_code == 401
    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert agent.messages == []


def test_chat_message_requires_csrf() -> None:
    client, agent, _, resolver = build_client()

    response = client.post(
        "/api/chat/messages",
        cookies={"auth_session": "session-token", "auth_csrf": "csrf-token"},
        json={"message": "hello"},
    )
    invalid_body = client.post(
        "/api/chat/messages",
        cookies={"auth_session": "session-token", "auth_csrf": "csrf-token"},
        json={},
    )
    mismatch = client.post(
        "/api/chat/messages",
        cookies={"auth_session": "session-token", "auth_csrf": "csrf-token"},
        headers={"X-CSRF-Token": "other-token"},
        json={"message": "hello"},
    )

    assert response.status_code == 403
    assert invalid_body.status_code == 403
    assert mismatch.status_code == 403
    assert agent.messages == []
    assert resolver.calls == []


def test_chat_message_uses_server_tracker_and_trusted_metadata() -> None:
    client, agent, _, _ = build_client()

    response = authenticated_request(client, "/api/chat/messages", {"message": "hello"})

    assert response.status_code == 200
    assert response.json() == [{"recipient_id": "account:account-1", "text": "received", "buttons": None, "image": None, "custom": None}]
    assert agent.messages == [
        (
            "hello",
            "account:account-1",
            {
                "account_id": "account-1",
                "user_id": "business-user-1",
                "account_role": "consumer",
                "account_status": "active",
            },
        )
    ]


def test_chat_message_rate_limit_blocks_before_payload_and_agent() -> None:
    client, agent, _, _, limiter = build_client_with_rate_limiter()
    limiter.blocked_rules.add("chat.messages.account")

    response = authenticated_request(client, "/api/chat/messages", {"message": "hello"})

    assert response.status_code == 429
    assert response.json() == {"detail": "Too many requests"}
    assert agent.messages == []
    assert limiter.calls == [("chat.messages.account", "account-1")]


def test_chat_message_rate_limit_uses_server_account_not_forged_metadata() -> None:
    client, agent, _, _, limiter = build_client_with_rate_limiter()

    response = authenticated_request(
        client,
        "/api/chat/messages",
        {"message": "hello", "metadata": {"account_id": "attacker-account"}},
    )

    assert response.status_code == 200
    assert ("chat.messages.account", "account-1") in limiter.calls
    assert all(subject != "attacker-account" for _, subject in limiter.calls)
    assert agent.messages[-1][1] == "account:account-1"


def test_chat_message_rate_limiter_outage_returns_sanitized_503() -> None:
    client, agent, _, _, limiter = build_client_with_rate_limiter()
    limiter.unavailable_rules.add("chat.messages.account")

    response = authenticated_request(client, "/api/chat/messages", {"message": "hello"})

    assert response.status_code == 503
    assert response.json() == {"detail": "Rate limit service is unavailable"}
    assert "redis" not in response.text.lower()
    assert agent.messages == []


def test_chat_message_ignores_or_rejects_client_identity_fields() -> None:
    client, agent, _, _ = build_client()
    forged = {
        "message": "hello",
        "sender": "attacker",
        "sender_id": "attacker",
        "session_id": "attacker-session",
        "account_id": "attacker-account",
        "user_id": "attacker-user",
        "role": "admin",
        "account_status": "disabled",
        "metadata": {
            "account_id": "attacker-account",
            "Account_ID": "attacker-account",
            "nested": {"user_id": "attacker-user"},
            "safe": "kept",
        },
    }

    response = authenticated_request(client, "/api/chat/messages", forged)

    assert response.status_code in {200, 422}
    if response.status_code == 200:
        _, sender_id, metadata = agent.messages[-1]
        assert sender_id == "account:account-1"
        assert metadata == {
            "nested": {},
            "safe": "kept",
            "account_id": "account-1",
            "user_id": "business-user-1",
            "account_role": "consumer",
            "account_status": "active",
        }
    else:
        assert agent.messages == []


def test_chat_message_returns_409_when_account_has_no_business_binding() -> None:
    client, agent, _, resolver = build_client()
    resolver.error = BusinessUserNotBound()

    response = authenticated_request(client, "/api/chat/messages", {"message": "hello"})

    assert response.status_code == 409
    assert agent.messages == []


def test_chat_message_checks_csrf_before_business_binding() -> None:
    client, agent, _, resolver = build_client()
    resolver.error = BusinessUserNotBound()

    response = client.post(
        "/api/chat/messages",
        cookies={"auth_session": "session-token", "auth_csrf": "csrf-token"},
        json={"message": "hello"},
    )

    assert response.status_code == 403
    assert resolver.calls == []
    assert agent.messages == []


def test_chat_reset_returns_409_when_account_has_no_business_binding() -> None:
    client, agent, _, resolver = build_client()
    resolver.error = BusinessUserNotBound()

    response = authenticated_request(client, "/api/chat/reset")

    assert response.status_code == 409
    assert agent.reset_calls == []


def test_chat_message_returns_403_for_pending_or_disabled_account() -> None:
    for status in (AccountStatus.pending, AccountStatus.disabled):
        client, agent, sessions, resolver = build_client()
        sessions.identity = AccountIdentity("account-1", AccountRole.consumer, status)

        response = authenticated_request(client, "/api/chat/messages", {"message": "hello"})

        assert response.status_code == 403
        assert agent.messages == []
        assert resolver.calls == []


def test_chat_reset_requires_csrf_and_resets_only_authenticated_tracker() -> None:
    client, agent, _, resolver = build_client()

    forbidden = client.post(
        "/api/chat/reset",
        cookies={"auth_session": "session-token", "auth_csrf": "csrf-token"},
    )
    mismatch = client.post(
        "/api/chat/reset",
        cookies={"auth_session": "session-token", "auth_csrf": "csrf-token"},
        headers={"X-CSRF-Token": "other-token"},
    )
    response = authenticated_request(
        client,
        "/api/chat/reset",
        {"session_id": "attacker-session", "account_id": "attacker-account"},
    )

    assert forbidden.status_code == 403
    assert mismatch.status_code == 403
    assert response.status_code == 204
    assert agent.reset_calls == ["account:account-1"]
    assert len(resolver.calls) == 1


def test_chat_reset_rate_limit_blocks_before_reset_tracker() -> None:
    client, agent, _, _, limiter = build_client_with_rate_limiter()
    limiter.blocked_rules.add("chat.reset.account")

    response = authenticated_request(client, "/api/chat/reset")

    assert response.status_code == 429
    assert response.json() == {"detail": "Too many requests"}
    assert agent.reset_calls == []
    assert limiter.calls == [("chat.reset.account", "account-1")]


def test_chat_reset_rate_limiter_outage_returns_sanitized_503() -> None:
    client, agent, _, _, limiter = build_client_with_rate_limiter()
    limiter.unavailable_rules.add("chat.reset.account")

    response = authenticated_request(client, "/api/chat/reset")

    assert response.status_code == 503
    assert response.json() == {"detail": "Rate limit service is unavailable"}
    assert "redis" not in response.text.lower()
    assert agent.reset_calls == []


def test_chat_reset_requires_missing_or_invalid_session() -> None:
    client, agent, _, _ = build_client()

    no_cookies = client.post("/api/chat/reset")
    missing = client.post(
        "/api/chat/reset",
        cookies={"auth_csrf": "csrf-token"},
        headers={"X-CSRF-Token": "csrf-token"},
    )
    invalid = client.post(
        "/api/chat/reset",
        cookies={"auth_session": "invalid-session", "auth_csrf": "csrf-token"},
        headers={"X-CSRF-Token": "csrf-token"},
    )

    assert no_cookies.status_code == 401
    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert agent.reset_calls == []


def test_chat_reset_checks_csrf_before_business_binding() -> None:
    client, agent, _, resolver = build_client()
    resolver.error = BusinessUserNotBound()

    response = client.post(
        "/api/chat/reset",
        cookies={"auth_session": "session-token", "auth_csrf": "csrf-token"},
    )

    assert response.status_code == 403
    assert resolver.calls == []
    assert agent.reset_calls == []


def test_chat_reset_returns_403_for_pending_or_disabled_account() -> None:
    for status in (AccountStatus.pending, AccountStatus.disabled):
        client, agent, sessions, resolver = build_client()
        sessions.identity = AccountIdentity("account-1", AccountRole.consumer, status)

        response = authenticated_request(client, "/api/chat/reset")

        assert response.status_code == 403
        assert agent.reset_calls == []
        assert resolver.calls == []


def test_session_dependency_outage_returns_sanitized_503() -> None:
    client, agent, sessions, _ = build_client()
    sessions.error = RuntimeError("internal Redis session outage detail")

    response = authenticated_request(client, "/api/chat/messages", {"message": "hello"})

    assert response.status_code == 503
    assert response.json() == {"detail": "Authentication service is unavailable"}
    assert "secret" not in response.text
    assert agent.messages == []


def test_binding_dependency_outage_returns_sanitized_503() -> None:
    client, agent, _, resolver = build_client()
    resolver.error = BusinessUserBindingUnavailable()

    response = authenticated_request(client, "/api/chat/messages", {"message": "hello"})

    assert response.status_code == 503
    assert response.json() == {"detail": "Chat authorization service is unavailable"}
    assert agent.messages == []


def test_reset_binding_dependency_outage_returns_sanitized_503() -> None:
    client, agent, _, resolver = build_client()
    resolver.error = BusinessUserBindingUnavailable()

    response = authenticated_request(client, "/api/chat/reset")

    assert response.status_code == 503
    assert response.json() == {"detail": "Chat authorization service is unavailable"}
    assert agent.reset_calls == []
