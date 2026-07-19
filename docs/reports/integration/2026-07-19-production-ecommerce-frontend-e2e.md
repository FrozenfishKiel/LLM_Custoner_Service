# 正式电商客服前端与浏览器 E2E 集成报告

日期：2026-07-19

## 本次交付

本 slice 交付了一个标准电商后台风格的正式客服前端，视觉方向基于 Product Design 方向 2：

- 左侧深蓝电商导航：工作台、订单、售后、会员、账户安全。
- 主工作区：登录/注册/找回/重置入口、订单概览、售后状态、风险提示。
- AI 售后顾问：登录后通过 `/api/chat/messages` 发送消息，通过 `/api/chat/reset` 重置会话。
- 账户流程：注册、登录、找回密码、重置密码、修改密码、退出登录。
- 前端安全接入：从 `auth_csrf` cookie 读取 CSRF，并在状态变更请求中发送 `X-CSRF-Token`。
- 错误提示中文化：401、403、409、429、503 均映射为用户可理解的中文提示。

## 代码改动

- `atguigu_ai/api/routes/frontend.py`：新增前端页面路由。
- `atguigu_ai/api/templates/customer_frontend.html`：新增电商客服工作台模板。
- `atguigu_ai/api/static/customer_frontend.css`：新增正式视觉样式。
- `atguigu_ai/api/static/customer_frontend.js`：新增前端交互与 API 封装。
- `atguigu_ai/api/server.py`：挂载 `/static` 并注册前端页面路由。
- `setup.py`：打包模板与静态资源，保证安装后可用。
- `tests/unit/api/test_frontend_routes.py`：新增前端路由/资源/控件契约测试。
- `tests/e2e/test_customer_frontend_browser.py`：新增真实浏览器 E2E 测试。

## 验证结果

| 类型 | 证据 | 结果 |
| --- | --- | --- |
| 前端契约 + auth/chat 回归 | `docs/reports/integration/evidence/customer-frontend-unit-api.txt` | `47 passed, 39 warnings` |
| pytest 浏览器 E2E | `docs/reports/integration/evidence/customer-frontend-browser-pytest.txt` | `1 passed` |
| 内置浏览器手工自动化 E2E | `docs/reports/integration/evidence/customer-frontend-browser-e2e.json` | `ok: true` |
| 设计 QA | `design-qa.md` | `final result: passed` |
| 截图证据 | `docs/reports/integration/evidence/customer-frontend-viewport.png` | 无横向溢出 |

## 浏览器 E2E 覆盖

pytest 浏览器测试覆盖：

1. 打开 `/login`，确认正式电商客服工作台可见。
2. 未登录直接发送客服消息，前端显示中文 401 友好错误。
3. 登录成功，后端写入 `auth_session` 和 `auth_csrf`。
4. 登录后发送客服消息，真实命中 `/api/chat/messages`。
5. 连续发送 5 条消息，验证小压力下稳定性。
6. 重置聊天，真实命中 `/api/chat/reset`。
7. 通过浏览器网络拦截篡改 `X-CSRF-Token`，确认后端 403 且前端显示中文安全提示。
8. 退出登录，真实命中 `/api/auth/logout`。
9. 检查无非预期 console error。

## 量化数据

来自 `docs/reports/integration/evidence/customer-frontend-pytest-browser.json`：

- 首屏渲染：`365.05 ms`
- 单次客服消息往返：`46.81 ms`
- 连续消息压力样本：`5`
- 连续消息最大往返：`47.24 ms`
- 非预期 console error：`0`

来自内置浏览器实测：

- 首屏观测：`39 ms`
- 聊天往返：`342 ms`
- console error：`0`

## 风险测试

- 未登录访问聊天：浏览器 E2E 覆盖，显示“请先登录后再继续”。
- CSRF 不匹配：浏览器 E2E 覆盖，后端返回 403，前端显示“安全校验失败，请刷新页面后重试”。
- 429 限流：`tests/unit/api/test_auth_routes.py` 与 `tests/unit/api/test_chat_routes.py` 覆盖 auth/chat 限流返回与服务调用阻断。
- 503 服务不可用：`tests/unit/api/test_auth_routes.py` 与 `tests/unit/api/test_chat_routes.py` 覆盖 auth/chat/rate-limit outage 的脱敏 503。
- 横向溢出/裁切：Design QA 初次发现并修复，复查 `scrollWidth == clientWidth`。

## 已知限制

- 当前 E2E 使用测试专用假 Auth/Chat 依赖，不依赖真实 SMTP/MySQL/Redis；这是为了验证前后端 HTTP、cookie、CSRF、浏览器交互链路。真实数据库/邮件链路仍由已有 auth/chat 集成测试和后续部署测试承担。
- 商品图按用户要求仅做背景氛围，不作为参考图或关键信息来源。
- 真实品牌资产、图标系统、移动端 390px 精细 QA 仍是 P3 后续优化。

## 结论

正式电商客服前端与前后端浏览器 E2E 本 slice 已达到可演示状态。当前证据显示：页面可打开、核心账户与客服路径可操作、CSRF/未登录风险路径可见、无横向溢出、无非预期 console error。
