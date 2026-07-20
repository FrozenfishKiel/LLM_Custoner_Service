# LLM 聊天评测 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 按任务执行。步骤使用 checkbox（`- [ ]`）追踪。

**Goal:** 为真实认证电商客服聊天链路建立一个可重复运行的黑盒 LLM 质量评测基线。

**Architecture:** 评测不 mock Agent，也不绕过 auth/chat HTTP 路由；测试通过 FastAPI ASGI transport 注册账号、绑定业务用户、发送聊天消息，并用 MySQL fixture 与响应文本共同打分。

**Tech Stack:** Python 3.12、pytest、pytest-asyncio、httpx ASGITransport、FastAPI、Redis、MySQL、SQLAlchemy、Alembic、现有 Agent/Action 栈。

---

## Task 1: 评测用例建模

**Files:**
- Modify: `tests/evaluation/chat_eval_cases.py`

- [x] 定义 14 个评测用例：10 个订单/物流/地址/取消/售后业务用例，4 个边界拒答用例。
- [x] 每个用例记录输入轮次、fixture key、期望响应片段、期望最终业务状态或边界拒答规则。

## Task 2: 真实 integration harness

**Files:**
- Create: `tests/integration/chat_eval_support.py`
- Modify: `tests/integration/test_llm_chat_evaluation.py`

- [x] 创建隔离 MySQL 数据库并运行 Alembic migration。
- [x] 写入 deterministic 订单、物流、地址、售后 fixture。
- [x] 使用真实 auth API 注册、验证邮箱、登录，并绑定业务用户。
- [x] 使用真实 chat API 发送消息，不直接调用 action。
- [x] 对每个 case 计算：
  - `completed`
  - `factually_correct`
  - `boundary_correct`
  - `turns`
- [x] 聚合输出：
  - `total_cases`
  - `scenario_completion_rate`
  - `business_fact_accuracy`
  - `boundary_refusal_rate`
  - `average_turns_to_completion`

## Task 3: 评测报告与证据

**Files:**
- Create/Modify: `docs/reports/integration/2026-07-19-llm-chat-evaluation.md`
- Create/Modify: `docs/reports/integration/evidence/llm-chat-evaluation.txt`
- Create/Modify: `docs/reports/integration/evidence/llm-chat-evaluation-secret-scan.txt`

- [x] 记录历史 baseline：14 个用例，核心质量指标均为 0。
- [x] 明确该 baseline 代表“当前质量很弱”，不是上线合格证明。
- [x] 保存 per-case breakdown 和示例响应。
- [x] 做 scoped secret scan。

## Task 4: 当前复跑收口

**Files:**
- Create: `docs/reports/integration/evidence/llm-chat-evaluation-rerun-2026-07-20.txt`
- Modify: `docs/reports/integration/2026-07-19-llm-chat-evaluation.md`

- [x] 使用当前 Codex runtime 复跑 targeted eval。
- [x] 记录 fresh 结果：当前机器本地 MySQL 不可用，复跑失败于 integration 环境准备阶段。
- [x] 报告中区分“历史 baseline”与“当前复跑状态”，不伪造通过。

## 验证命令

历史完整评测命令：

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_llm_chat_evaluation.py -q -s -m integration
```

当前复跑命令：

```powershell
C:\Users\frozenfish\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest tests/integration/test_llm_chat_evaluation.py -q -s -m integration
```

当前复跑结果：

```text
FAILED tests/integration/test_llm_chat_evaluation.py::test_llm_chat_evaluation_emits_resume_usable_metrics
AssertionError: Local MySQL is unavailable for integration tests
```
