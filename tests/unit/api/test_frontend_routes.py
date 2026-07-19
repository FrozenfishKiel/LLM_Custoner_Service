from __future__ import annotations

from fastapi.testclient import TestClient

from atguigu_ai.api.server import create_app


FRONTEND_ROUTES = [
    "/login",
    "/register",
    "/forgot-password",
    "/reset-password",
    "/account",
    "/chat",
]


def test_customer_frontend_routes_render_ecommerce_shell() -> None:
    client = TestClient(create_app(enable_inspect=False), base_url="https://testserver")

    for path in FRONTEND_ROUTES:
        response = client.get(path)

        assert response.status_code == 200
        assert "客服工作台" in response.text
        assert "SHOPWISE" in response.text
        assert 'data-page="' in response.text
        assert 'href="/static/customer_frontend.css' in response.text
        assert 'src="/static/customer_frontend.js' in response.text


def test_customer_frontend_static_assets_are_served() -> None:
    client = TestClient(create_app(enable_inspect=False), base_url="https://testserver")

    css = client.get("/static/customer_frontend.css")
    js = client.get("/static/customer_frontend.js")

    assert css.status_code == 200
    assert "customer-shell" in css.text
    assert "support-panel" in css.text
    assert js.status_code == 200
    assert "window.CustomerFrontend" in js.text
    assert "X-CSRF-Token" in js.text


def test_frontend_contains_required_user_journey_controls() -> None:
    client = TestClient(create_app(enable_inspect=False), base_url="https://testserver")

    response = client.get("/chat")

    assert response.status_code == 200
    assert 'data-testid="login-form"' in response.text
    assert 'data-testid="register-form"' in response.text
    assert 'data-testid="forgot-password-form"' in response.text
    assert 'data-testid="reset-password-form"' in response.text
    assert 'data-testid="change-password-form"' in response.text
    assert 'data-testid="chat-form"' in response.text
    assert 'data-testid="logout-button"' in response.text
    assert 'data-testid="reset-chat-button"' in response.text


def test_frontend_contains_friendly_risk_copy() -> None:
    client = TestClient(create_app(enable_inspect=False), base_url="https://testserver")

    response = client.get("/static/customer_frontend.js")

    assert response.status_code == 200
    assert "请先登录后再继续" in response.text
    assert "安全校验失败" in response.text
    assert "操作太频繁" in response.text
    assert "服务暂时不可用" in response.text
