# 生产就绪最小闭环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让生产 app 能真实挂载 auth/chat/Agent/frontend，并补齐 LLM 评测收口、health/ready/metrics 和上线前报告。

**Architecture:** 继续沿用 FastAPI + 原生前端，不引入新服务。`atguigu_ai/api/production.py` 负责生产依赖装配；`atguigu_ai/api/server.py` 负责通用路由和运维接口；LLM eval 保持 integration 测试和 evidence/report 形式。

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, Redis, pytest, httpx ASGI transport, existing Agent/Action stack.

---

## Task 1: 生产 app 注入真实 chat deps

**Files:**
- Modify: `tests/unit/api/test_production_dependencies.py`
- Modify: `atguigu_ai/api/production.py`
- Modify: `atguigu_ai/api/__init__.py`

- [ ] **Step 1: Write failing tests**

新增测试覆盖：

- `create_production_app()` 在提供 `agent_factory` 时注册 `/api/chat/messages`。
- 生产 chat deps 使用 `BusinessIdentityResolver`。
- 缺失或无效 `PRODUCTION_AGENT_PATH` 只在生产 app 构建时失败，不影响 `import atguigu_ai.api`。

- [ ] **Step 2: Verify RED**

Run:

```powershell
C:\Users\frozenfish\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest tests/unit/api/test_production_dependencies.py -q
```

Expected: FAIL，因为 production app 目前没有 chat deps wiring。

- [ ] **Step 3: Implement production wiring**

实现：

- `ProductionBindingRepository`
- `build_production_chat_deps()`
- `build_production_dependencies()`
- `create_production_app()` 同时传入 `auth_deps` 和 `chat_deps`

- [ ] **Step 4: Verify GREEN**

同上命令应 PASS。

## Task 2: 运维接口最小闭环

**Files:**
- Modify: `tests/unit/api/test_production_dependencies.py`
- Modify: `atguigu_ai/api/server.py`

- [ ] **Step 1: Write failing tests**

新增测试覆盖：

- `/health/live` 返回 200 和 `status=alive`。
- `/health/ready` 在 auth/chat/agent/limiter 存在时 ready=true。
- `/internal/metrics` 不泄露 secret，包含最小文本指标。

- [ ] **Step 2: Verify RED**

Run:

```powershell
C:\Users\frozenfish\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest tests/unit/api/test_production_dependencies.py -q
```

Expected: FAIL，因为接口未实现。

- [ ] **Step 3: Implement routes**

在 `server.py` 增加通用 health/metrics 路由，使用 `app.state.auth_deps`、`self.chat_deps`、`self.agent` 判断。

- [ ] **Step 4: Verify GREEN**

同上命令应 PASS。

## Task 3: LLM eval 收口

**Files:**
- Modify: `tests/integration/test_llm_chat_evaluation.py`
- Create/Modify: `tests/integration/chat_eval_support.py`
- Create/Modify: `docs/reports/integration/2026-07-19-llm-chat-evaluation.md`
- Create/Modify: `docs/reports/integration/evidence/llm-chat-evaluation.txt`
- Create/Modify: `docs/reports/integration/evidence/llm-chat-evaluation-secret-scan.txt`

- [ ] **Step 1: Inspect existing dirty files**

确认文件来自 LLM eval slice，不混入生产 frontend slice。

- [ ] **Step 2: Run targeted eval if infrastructure is available**

Run:

```powershell
C:\Users\frozenfish\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest tests/integration/test_llm_chat_evaluation.py -q
```

若 Docker/MySQL/Redis 缺失，报告要写明 blocked 原因；不能伪造结果。

- [ ] **Step 3: UTF-8 and fact cleanup**

确保 report/evidence UTF-8 可读，报告明确 baseline 很弱，不能声称质量上线合格。

## Task 4: 配置和上线前报告

**Files:**
- Modify: `.env.example`
- Create: `docs/reports/integration/2026-07-20-production-readiness-chain.md`

- [ ] **Step 1: Update config sample**

补齐：

- `AUTH_PUBLIC_BASE_URL`
- `PRODUCTION_AGENT_PATH`
- `PRODUCTION_ENABLE_INSPECT`
- readiness/metrics 说明

- [ ] **Step 2: Write Chinese report**

记录：

- 生产 app wiring
- LLM eval baseline
- health/metrics
- 验证命令和结果
- 仍需用户提供的生产配置

## Task 5: Final verification and commit

Run:

```powershell
C:\Users\frozenfish\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest tests/unit/api/test_production_dependencies.py tests/unit/api/test_frontend_routes.py tests/unit/api/test_auth_routes.py tests/unit/api/test_chat_routes.py -q
C:\Users\frozenfish\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m compileall atguigu_ai/api/production.py atguigu_ai/api/server.py tests/integration/chat_eval_support.py
git diff --check
git status --short
```

Then commit and push only files in this slice.
