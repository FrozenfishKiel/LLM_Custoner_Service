from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import socket
import threading
import time
from types import SimpleNamespace
from typing import Callable

import pytest

from atguigu_ai.api.dependencies import AuthRouteDependencies
from atguigu_ai.api.routes.chat import ChatRouteDependencies
from atguigu_ai.api.server import create_app
from atguigu_ai.auth import (
    AccountIdentity,
    AccountRole,
    AccountStatus,
    CreatedSession,
    InvalidCredentials,
    LoginAccepted,
    PasswordResetAccepted,
    RegistrationAccepted,
)
from atguigu_ai.auth.business_identity import BusinessUserIdentity


pytestmark = pytest.mark.e2e


@dataclass
class RunningServer:
    base_url: str
    stop: Callable[[], None]
    rate_limiter: FakeRateLimiter


class FakeSessionStore:
    def __init__(self) -> None:
        self.sessions: dict[str, AccountIdentity] = {}

    async def resolve(self, token: str) -> AccountIdentity | None:
        return self.sessions.get(token)

    async def revoke(self, token: str) -> None:
        self.sessions.pop(token, None)

    async def revoke_all(self, account_id: str) -> None:
        for token, identity in list(self.sessions.items()):
            if identity.account_id == account_id:
                self.sessions.pop(token, None)


class FakeAuthService:
    def __init__(self, sessions: FakeSessionStore) -> None:
        self.sessions = sessions
        self.identity = AccountIdentity("account-1", AccountRole.consumer, AccountStatus.active)

    async def register(self, email: str, password: str) -> RegistrationAccepted:
        return RegistrationAccepted("account-1", email)

    async def verify_email(self, token: str) -> AccountIdentity | None:
        return self.identity

    async def resend_verification(self, email: str) -> PasswordResetAccepted:
        return PasswordResetAccepted()

    async def login(self, email: str, password: str) -> LoginAccepted:
        if password != "correct horse":
            raise InvalidCredentials()
        self.sessions.sessions["session-token"] = self.identity
        return LoginAccepted(
            identity=self.identity,
            session=CreatedSession("session-token", datetime.now(timezone.utc) + timedelta(days=7)),
        )

    async def logout(self, session_token: str) -> None:
        await self.sessions.revoke(session_token)

    async def forgot_password(self, email: str) -> PasswordResetAccepted:
        return PasswordResetAccepted()

    async def reset_password(self, token: str, new_password: str) -> PasswordResetAccepted:
        return PasswordResetAccepted()

    async def change_password(self, account_id: str, current_password: str, new_password: str) -> None:
        await self.sessions.revoke_all(account_id)


class FakeBusinessIdentityResolver:
    async def resolve(self, identity: AccountIdentity) -> BusinessUserIdentity:
        return BusinessUserIdentity(
            account_id=identity.account_id,
            user_id="business-user-1",
            role=identity.role,
            account_status=identity.status,
        )


class FakeAgent:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.reset_count = 0

    async def handle_message(self, message: str, sender_id: str, metadata: dict[str, object]) -> SimpleNamespace:
        self.messages.append(message)
        return SimpleNamespace(messages=[{"text": f"已收到：{message}。预计 7 月 21 日送达。"}])

    async def reset_tracker(self, sender_id: str) -> None:
        self.reset_count += 1


class FakeRateLimiter:
    def __init__(self) -> None:
        self.block_chat = False

    async def check(self, rule, subject: str):
        allowed = not (self.block_chat and rule.name == "chat.messages.account")
        return SimpleNamespace(
            allowed=allowed,
            limit=1,
            remaining=0 if not allowed else 1,
            retry_after_seconds=60,
            reset_after_seconds=60,
            rule_name=rule.name,
        )


def build_customer_frontend_e2e_app(rate_limiter: FakeRateLimiter | None = None):
    sessions = FakeSessionStore()
    limiter = rate_limiter or FakeRateLimiter()
    auth_deps = AuthRouteDependencies(
        service=FakeAuthService(sessions),
        sessions=sessions,
        cookie_secure=False,
        rate_limiter=limiter,
        client_ip_resolver=lambda request: "127.0.0.1",
    )
    app = create_app(
        auth_deps=auth_deps,
        chat_deps=ChatRouteDependencies(FakeAgent(), FakeBusinessIdentityResolver()),
        enable_inspect=False,
    )
    return app


@pytest.fixture(scope="module")
def browser_server() -> RunningServer:
    uvicorn = pytest.importorskip("uvicorn")
    rate_limiter = FakeRateLimiter()
    app = build_customer_frontend_e2e_app(rate_limiter)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("E2E server did not start")

    def stop() -> None:
        server.should_exit = True
        thread.join(timeout=5)

    try:
        yield RunningServer(f"http://127.0.0.1:{port}", stop, rate_limiter)
    finally:
        stop()


def test_customer_frontend_browser_complete_user_journey(browser_server: RunningServer) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    executable_path = _chromium_executable_path()
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=executable_path)
        page = browser.new_page(viewport={"width": 1440, "height": 1024})
        console_errors: list[str] = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

        started = time.perf_counter()
        page.goto(f"{browser_server.base_url}/login")
        page.get_by_text("客服工作台").first.wait_for()
        first_paint_ms = (time.perf_counter() - started) * 1000

        page.locator('[data-testid="chat-form"] input').fill("未登录时能发送吗？")
        page.locator('[data-testid="chat-form"]').evaluate("form => form.requestSubmit()")
        page.locator("#notice").get_by_text("请先登录后再继续。").wait_for()

        page.locator('[data-testid="login-form"] input[name="email"]').fill("user@example.com")
        page.locator('[data-testid="login-form"] input[name="password"]').fill("correct horse")
        page.locator('[data-testid="login-form"]').evaluate("form => form.requestSubmit()")
        page.get_by_text("登录成功，可以开始客服会话。").wait_for()

        started = time.perf_counter()
        page.locator('[data-testid="chat-form"] input').fill("我想查询轻量羽绒服什么时候到。")
        page.locator('[data-testid="chat-form"]').evaluate("form => form.requestSubmit()")
        page.get_by_text("预计 7 月 21 日送达").wait_for()
        round_trip_ms = (time.perf_counter() - started) * 1000

        pressure_durations_ms: list[float] = []
        for index in range(5):
            started = time.perf_counter()
            page.locator('[data-testid="chat-form"] input').fill(f"连续消息压力测试 {index}")
            page.locator('[data-testid="chat-form"]').evaluate("form => form.requestSubmit()")
            page.get_by_text(f"已收到：连续消息压力测试 {index}").wait_for()
            pressure_durations_ms.append((time.perf_counter() - started) * 1000)

        browser_server.rate_limiter.block_chat = True
        page.locator('[data-testid="chat-form"] input').fill("限流测试")
        page.locator('[data-testid="chat-form"]').evaluate("form => form.requestSubmit()")
        page.locator("#notice").get_by_text("操作太频繁了，请稍后再试。").wait_for()
        browser_server.rate_limiter.block_chat = False

        page.locator('[data-testid="reset-chat-button"]').click()
        page.get_by_text("聊天已重置。").wait_for()

        def corrupt_csrf(route):
            headers = dict(route.request.headers)
            headers["x-csrf-token"] = "wrong-token"
            route.continue_(headers=headers)

        page.route("**/api/chat/messages", corrupt_csrf)
        page.locator('[data-testid="chat-form"] input').fill("CSRF 测试")
        page.locator('[data-testid="chat-form"]').evaluate("form => form.requestSubmit()")
        page.locator("#notice").get_by_text("安全校验失败，请刷新页面后重试。").wait_for()
        page.unroute("**/api/chat/messages", corrupt_csrf)

        page.locator('[data-testid="logout-button"]').click()
        page.get_by_text("已退出登录。").wait_for()

        assert first_paint_ms < 3000
        assert round_trip_ms < 3000
        assert max(pressure_durations_ms) < 3000
        unexpected_console_errors = [
            message
            for message in console_errors
            if "401 (Unauthorized)" not in message
            and "403 (Forbidden)" not in message
            and "429 (Too Many Requests)" not in message
        ]
        assert unexpected_console_errors == []
        evidence_dir = Path("docs/reports/integration/evidence")
        evidence_dir.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(evidence_dir / "customer-frontend-pytest-browser.png"), full_page=True)
        (evidence_dir / "customer-frontend-pytest-browser.json").write_text(
            (
                "{\n"
                f'  "first_paint_ms": {first_paint_ms:.2f},\n'
                f'  "chat_round_trip_ms": {round_trip_ms:.2f},\n'
                f'  "pressure_messages": {len(pressure_durations_ms)},\n'
                f'  "pressure_max_ms": {max(pressure_durations_ms):.2f},\n'
                f'  "unexpected_console_errors": {len(unexpected_console_errors)}\n'
                "}\n"
            ),
            encoding="utf-8",
        )
        browser.close()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _chromium_executable_path() -> str | None:
    configured = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    if configured:
        return configured
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright/chromium-1228/chrome-win64/chrome.exe",
        Path.home()
        / "AppData/Roaming/iSlide/iSlide Tools/Browser/DotNetBrowser/2.26.2/chromium/WindowsX64/chromium.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None
