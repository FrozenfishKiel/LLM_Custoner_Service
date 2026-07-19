# 正式电商客服前端与浏览器 E2E Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 基于已选 Product Design 方向 2，交付一个可演示、可测试的正式电商风格客服前端，并完成前端到后端的浏览器 E2E 全链路验证。

**Architecture:** 不引入 React/Vue/Vite，沿用当前 FastAPI 应用，新增生产前端路由、Jinja/HTML 模板、静态 CSS/JS。前端通过现有 `/api/auth/*`、`/api/account/me`、`/api/chat/*` 接口工作，CSRF 从 `auth_csrf` cookie 读取并写入 `X-CSRF-Token`。

**Tech Stack:** FastAPI, Jinja/HTML, CSS, 原生 JavaScript, pytest/TestClient, Playwright 浏览器验证。

---

## 文件结构

- Create `atguigu_ai/api/routes/frontend.py`：生产前端页面路由，返回同一个电商工作台 app shell。
- Create `atguigu_ai/api/templates/customer_frontend.html`：正式电商客服工作台 HTML。
- Create `atguigu_ai/api/static/customer_frontend.css`：方向 2 的视觉系统、响应式布局、状态样式。
- Create `atguigu_ai/api/static/customer_frontend.js`：登录/注册/找回/重置/账户/聊天交互，封装 API、CSRF、状态机。
- Modify `atguigu_ai/api/server.py`：挂载 `/static` 和前端路由。
- Modify `setup.py`：确保模板与静态资源安装后仍可用。
- Create `tests/unit/api/test_frontend_routes.py`：RED/GREEN 路由和静态资源契约测试。
- Create `tests/e2e/test_customer_frontend_browser.py`：浏览器级渲染/交互测试入口。
- Create `docs/reports/integration/2026-07-19-production-ecommerce-frontend-e2e.md`：中文验证报告。

## Task 1: 页面路由与资源契约

**Files:**
- Create: `tests/unit/api/test_frontend_routes.py`
- Create: `atguigu_ai/api/routes/frontend.py`
- Create: `atguigu_ai/api/templates/customer_frontend.html`
- Create: `atguigu_ai/api/static/customer_frontend.css`
- Create: `atguigu_ai/api/static/customer_frontend.js`
- Modify: `atguigu_ai/api/server.py`
- Modify: `setup.py`

- [ ] **Step 1: Write the failing test**

测试应断言：

```python
def test_customer_frontend_routes_render_ecommerce_shell():
    app = create_app(enable_inspect=False)
    client = TestClient(app)
    for path in ["/login", "/register", "/forgot-password", "/reset-password", "/account", "/chat"]:
        response = client.get(path)
        assert response.status_code == 200
        assert "客服工作台" in response.text
        assert "SHOPWISE" in response.text
        assert 'data-page="' in response.text

def test_customer_frontend_static_assets_are_served():
    app = create_app(enable_inspect=False)
    client = TestClient(app)
    css = client.get("/static/customer_frontend.css")
    js = client.get("/static/customer_frontend.js")
    assert css.status_code == 200
    assert "customer-shell" in css.text
    assert js.status_code == 200
    assert "window.CustomerFrontend" in js.text
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
pytest tests/unit/api/test_frontend_routes.py -q
```

Expected: FAIL，因为路由和静态资源还不存在。

- [ ] **Step 3: Implement minimal route/static/template wiring**

新增 `frontend.py` 返回 `customer_frontend.html`，`server.py` 挂载 `StaticFiles` 并 include 前端 router，`setup.py` 包含 `api/templates/*.html` 与 `api/static/*`。

- [ ] **Step 4: Run test to verify it passes**

Run:

```powershell
pytest tests/unit/api/test_frontend_routes.py -q
```

Expected: PASS。

## Task 2: 正式电商工作台视觉与前端交互

**Files:**
- Modify: `atguigu_ai/api/templates/customer_frontend.html`
- Modify: `atguigu_ai/api/static/customer_frontend.css`
- Modify: `atguigu_ai/api/static/customer_frontend.js`
- Modify: `tests/unit/api/test_frontend_routes.py`

- [ ] **Step 1: Write failing contract tests**

测试应断言：

```python
def test_frontend_contains_required_user_journey_controls():
    response = TestClient(create_app(enable_inspect=False)).get("/chat")
    assert 'data-testid="login-form"' in response.text
    assert 'data-testid="register-form"' in response.text
    assert 'data-testid="chat-form"' in response.text
    assert 'data-testid="logout-button"' in response.text
    assert 'data-testid="reset-chat-button"' in response.text
```

- [ ] **Step 2: Verify RED**

Run:

```powershell
pytest tests/unit/api/test_frontend_routes.py -q
```

Expected: FAIL，缺少完整控件。

- [ ] **Step 3: Implement visual shell and JS behavior**

实现方向 2：

- 深蓝左侧导航；
- 顶部“客服工作台”标题；
- 订单、售后、风险提示概览；
- 最近订单列表；
- 右侧 AI 售后顾问聊天面板；
- 登录/注册/邮箱验证/找回密码/重置密码/改密/退出；
- 对 401/403/409/429/503 给出用户能理解的中文提示；
- 对聊天发送、重置聊天、退出登录提供 loading/success/error 状态。

- [ ] **Step 4: Verify GREEN**

Run:

```powershell
pytest tests/unit/api/test_frontend_routes.py -q
```

Expected: PASS。

## Task 3: 浏览器 E2E 与风险场景

**Files:**
- Create: `tests/e2e/test_customer_frontend_browser.py`
- Create: `docs/reports/integration/2026-07-19-production-ecommerce-frontend-e2e.md`
- Create/Update: `design-qa.md`

- [ ] **Step 1: Write failing E2E test**

浏览器测试覆盖：

- 打开 `/login`；
- 填写登录表单并通过 mock API 或测试服务完成登录；
- 登录后进入 `/chat`；
- 发送客服消息；
- 重置聊天；
- 触发错误场景：未登录、CSRF 缺失、429 限流、503 服务不可用；
- 检查无 console error；
- 记录基础量化数据：首屏渲染耗时、消息往返耗时、连续发送稳定性。

- [ ] **Step 2: Verify RED**

Run:

```powershell
pytest tests/e2e/test_customer_frontend_browser.py -q
```

Expected: FAIL，浏览器测试和/或前端契约未实现。

- [ ] **Step 3: Implement E2E support**

使用 Playwright 或可用浏览器自动化打开本地 FastAPI 服务；若本机缺 Playwright 浏览器二进制，安装或记录阻塞。测试服务使用仓库内假 Auth/Chat 依赖，不依赖真实 SMTP。

- [ ] **Step 4: Verify GREEN + design QA**

Run:

```powershell
pytest tests/e2e/test_customer_frontend_browser.py -q
pytest tests/unit/api/test_frontend_routes.py tests/unit/api/test_auth_routes.py tests/unit/api/test_chat_routes.py -q
```

然后捕获 1440x1024 截图，对比选定方向 2，写 `design-qa.md`，要求 `final result: passed` 或明确 blocked。

## Task 4: 独立测试代理与上线前报告

**Files:**
- Create/Modify: `docs/reports/integration/2026-07-19-production-ecommerce-frontend-e2e.md`

- [ ] **Step 1: Dispatch test agent**

测试代理只做验证，不改代码。覆盖：

- 正常用户路径；
- 刁钻输入；
- 未登录/CSRF/限流/服务不可用风险；
- 连续发送压力；
- 浏览器 console；
- 响应时间量化。

- [ ] **Step 2: Fix issues from test agent**

所有 P0/P1/P2 问题必须修复并复测；P3 可记录为后续优化。

- [ ] **Step 3: Final verification and commit**

Run:

```powershell
pytest tests/unit/api/test_frontend_routes.py tests/unit/api/test_auth_routes.py tests/unit/api/test_chat_routes.py -q
pytest tests/e2e/test_customer_frontend_browser.py -q
git status --short
```

提交并推送本 slice，保留与 LLM 评测相关的未跟踪文件不提交。
