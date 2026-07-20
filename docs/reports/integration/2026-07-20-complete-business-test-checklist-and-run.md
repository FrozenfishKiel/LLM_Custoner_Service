# 完整业务测试清单落地与验收运行报告

日期：2026-07-20  
分支：`feat/llm-quality-baseline`  
工作目录：`D:\Projects\llm_customer_service_llm_quality`

## 本次目的

针对“不能只写一段测一段，必须完整测试一遍业务”的问题，本次完成两件事：

1. 将完整业务测试指标清单固化到仓库，避免以后只依赖临场记忆。
2. 按清单跑一遍当前最小完整业务验收链，并记录结果和剩余缺口。

## 已新增的长期规则

- `docs/COMPLETE_BUSINESS_TEST_CHECKLIST.md`
  - 定义完整业务闭环、必测路径、风险测试、压力与监控指标、量化指标、推荐命令和完成判定。
- `AGENTS.md`
  - 要求后续 Codex/agent 在本仓库工作时必须读取并遵守完整业务测试清单。
  - 明确不能用局部测试替代完整业务验证。

## Fresh verification

### 1. Auth HTTP 完整链路

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_auth_routes_http.py -q -s -m integration
```

结果：

```text
8 passed
```

覆盖：注册、邮箱验证、登录、Cookie/CSRF、账号信息、退出、改密、忘记/重置密码、Redis 故障脱敏等。

### 2. Chat 授权与业务用户绑定

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_chat_authorization_http.py -q -s -m integration
```

结果：

```text
7 passed
```

覆盖：登录后聊天、服务端可信身份、伪造身份清理、未登录/未绑定/禁用账号拒绝、tracker reset、依赖异常脱敏等。

### 3. Action 写库、幂等、审计与隔离

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_action_ownership_audit.py -q -s -m integration
```

结果：

```text
5 passed
```

覆盖：取消订单、修改收货信息、售后申请、跨用户隔离、幂等、审计、售后锁顺序。

### 4. 真实 LLM + Chat API + Flow + Action + MySQL 业务评测

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_llm_chat_evaluation.py -q -s -m integration
```

结果：

```text
llm_chat_eval total_cases=14 scenario_completion_rate=1.0000 business_fact_accuracy=1.0000 boundary_refusal_rate=1.0000 average_turns_to_completion=3.40
1 passed
```

覆盖：订单查询、物流查询、修改地址、取消订单、售后申请、天气/编程/闲聊/金融越界拒答。该测试经过真实 HTTP chat、生产 Agent、LLM、Flow、Action、MySQL/Redis，并校验回复和数据库最终状态。

### 5. 浏览器前端 E2E

首次运行结果：

```text
SKIPPED: could not import 'playwright.sync_api': No module named 'playwright'
```

处理：为当前 `.venv` 补齐 Playwright Python 包，然后重跑。

```powershell
.\.venv\Scripts\python.exe -m ensurepip --upgrade
.\.venv\Scripts\python.exe -m pip install --timeout 60 playwright
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_customer_frontend_browser.py -q -s -m e2e
```

结果：

```text
1 passed
```

覆盖：页面打开、登录、聊天、连续消息、限流提示、重置、CSRF 错误、退出、console error 检查。

注意：当前浏览器 E2E 使用 fake auth/chat/agent，验证前端和浏览器交互，不验证真实 LLM 与真实业务 DB。真实 LLM+DB 业务闭环目前由 `tests/integration/test_llm_chat_evaluation.py` 覆盖。上线候选前仍需要补“浏览器 + 真实 LLM + 真实 DB”的五类业务 E2E。

## 当前结论

当前最小完整业务验收链已跑完：

```text
Auth HTTP: 8 passed
Chat 授权: 7 passed
Action 写库/隔离/审计: 5 passed
真实 LLM 业务评测: 1 passed，14 cases，核心指标 1.0/1.0/1.0
浏览器 E2E: 1 passed
```

## 剩余缺口

- 浏览器 E2E 仍不是“真实 LLM + 真实 DB”的五类业务闭环。
- 20 并发真实客服会话压测还没有形成固定自动化结果。
- 管理员功能、账号注销、模拟数据初始化幂等、备份恢复、发布回滚、Grafana/告警规则仍需要后续实现和验证。
- LLM 评测当前主要按最终业务结果评分，后续应补显式 `expected_intent`、`expected_slots`、`expected_flow`、`expected_action` 分层指标。
