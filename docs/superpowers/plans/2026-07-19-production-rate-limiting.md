# 生产限流 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 auth、chat 和关键写 Action 增加生产可用的 Redis 固定窗口限流，阻断暴力登录、批量注册、邮件轰炸、LLM/Agent 滥用和高频业务写操作。

**Architecture:** 新增 `atguigu_ai/rate_limit/` 作为唯一限流核心，HTTP route 和 Action 只负责选择 rule 与 subject。Redis 计数、TTL、敏感信息清洗、不可用异常都集中在 limiter 内；未注入 limiter 时保持课程兼容，生产 wiring 后续必须注入。

**Tech Stack:** Python 3、FastAPI、pytest、redis.asyncio、Redis Lua、SQLAlchemy Action 测试夹具。

---

## 文件结构

- 创建 `atguigu_ai/rate_limit/__init__.py`：导出限流公开 API。
- 创建 `atguigu_ai/rate_limit/limiter.py`：实现 `RateLimitRule`、`RateLimitDecision`、`RedisRateLimiter`、异常类型、subject hash 工具、默认规则。
- 修改 `atguigu_ai/api/dependencies.py`：`AuthRouteDependencies` 增加 `rate_limiter` 与 `client_ip_resolver`，新增 429/503 转换 helper。
- 修改 `atguigu_ai/api/routes/auth.py`：在注册、登录、忘记密码、重发验证、验证邮箱、重置密码、改密码入口调用限流。
- 修改 `atguigu_ai/api/routes/chat.py`：在业务身份解析后、payload/Agent/reset 前调用 chat 限流。
- 修改 `ecs_demo/actions/security.py`：提供 Action 限流 helper，统一处理超限和 Redis 不可用文案。
- 修改 `ecs_demo/actions/action_order.py`：在修改地址确认分支、取消订单确认分支接入 Action 限流。
- 修改 `ecs_demo/actions/action_postsale.py`：在提交售后确认分支接入 Action 限流。
- 创建 `tests/unit/rate_limit/test_redis_rate_limiter.py`：核心 limiter 单元测试。
- 修改 `tests/unit/api/test_auth_routes.py`：auth route 限流单元测试。
- 修改 `tests/unit/api/test_chat_routes.py`：chat route 限流单元测试。
- 创建 `tests/unit/actions/test_action_rate_limit.py`：关键写 Action 限流单元测试。
- 创建 `tests/integration/test_redis_rate_limiter.py`：真实 Redis 并发、TTL、key 安全集成测试。
- 创建 `docs/reports/integration/2026-07-19-production-rate-limiting.md`：最终中文测试与风险报告。

## Task 1: Redis RateLimiter 核心

**Files:**
- Create: `atguigu_ai/rate_limit/__init__.py`
- Create: `atguigu_ai/rate_limit/limiter.py`
- Test: `tests/unit/rate_limit/test_redis_rate_limiter.py`

- [ ] **Step 1: 写 RED 测试**

测试文件先覆盖固定窗口、超限、TTL 不刷新、subject 清洗、Redis 异常脱敏：

```python
import pytest
from redis.exceptions import RedisError

from atguigu_ai.rate_limit import (
    RateLimitRule,
    RateLimitStoreUnavailable,
    RedisRateLimiter,
    subject_digest,
)


class RecordingRedis:
    def __init__(self, *, error: Exception | None = None):
        self.error = error
        self.calls = []
        self.count = 0
        self.ttl = 60

    async def eval(self, script, numkeys, key, limit, window):
        self.calls.append((script, numkeys, key, limit, window))
        if self.error:
            raise self.error
        self.count += 1
        return [1 if self.count <= int(limit) else 0, self.count, self.ttl]


@pytest.mark.asyncio
async def test_first_request_is_allowed_and_key_contains_only_digest():
    redis = RecordingRedis()
    limiter = RedisRateLimiter(redis, key_prefix="rate:")
    rule = RateLimitRule(name="auth.login.ip_email", scope="auth", limit=2, window_seconds=60)

    decision = await limiter.check(rule, "ip=127.0.0.1 email=User@Example.COM")

    assert decision.allowed is True
    assert decision.remaining == 1
    assert decision.retry_after_seconds == 0
    key = redis.calls[0][2]
    assert key.startswith("rate:auth:auth.login.ip_email:")
    assert "User@Example.COM" not in key
    assert "127.0.0.1" not in key


@pytest.mark.asyncio
async def test_limit_exceeded_returns_retry_after_from_ttl():
    redis = RecordingRedis()
    limiter = RedisRateLimiter(redis)
    rule = RateLimitRule(name="chat.messages.account", scope="chat", limit=1, window_seconds=60)

    assert (await limiter.check(rule, "account-1")).allowed is True
    blocked = await limiter.check(rule, "account-1")

    assert blocked.allowed is False
    assert blocked.remaining == 0
    assert blocked.retry_after_seconds == 60


@pytest.mark.asyncio
async def test_redis_errors_are_sanitized():
    limiter = RedisRateLimiter(RecordingRedis(error=RedisError("redis://secret internal detail")))
    rule = RateLimitRule(name="auth.register.ip", scope="auth", limit=1, window_seconds=60)

    with pytest.raises(RateLimitStoreUnavailable) as exc:
        await limiter.check(rule, "127.0.0.1")

    assert str(exc.value) == "Rate limit store is unavailable"
    assert "redis" not in str(exc.value).lower()


def test_subject_digest_rejects_blank_and_never_returns_raw_subject():
    with pytest.raises(ValueError):
        subject_digest(" ")

    digest = subject_digest("user@example.com")

    assert digest != "user@example.com"
    assert len(digest) == 64
```

- [ ] **Step 2: 运行 RED**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/rate_limit/test_redis_rate_limiter.py -q
```

Expected: FAIL，原因是 `atguigu_ai.rate_limit` 模块不存在。

- [ ] **Step 3: 写最小实现**

`limiter.py` 实现固定窗口 Lua 调用：

```python
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from redis.exceptions import RedisError

_SAFE_NAME = re.compile(r"^[a-z0-9_.-]+$")

_CHECK_SCRIPT = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local key_type = redis.call('TYPE', key)['ok']
if key_type ~= 'none' and key_type ~= 'string' then
  return redis.error_reply('invalid rate limit key type')
end
local count = redis.call('INCR', key)
if count == 1 then
  redis.call('EXPIRE', key, window)
end
local ttl = redis.call('TTL', key)
if ttl < 0 then
  return redis.error_reply('invalid rate limit ttl')
end
if count > limit then
  return {0, count, ttl}
end
return {1, count, ttl}
"""


class RateLimitStoreUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Rate limit store is unavailable")


@dataclass(frozen=True)
class RateLimitRule:
    name: str
    scope: str
    limit: int
    window_seconds: int

    def __post_init__(self) -> None:
        if not _SAFE_NAME.match(self.name):
            raise ValueError("rate limit rule name is invalid")
        if not _SAFE_NAME.match(self.scope):
            raise ValueError("rate limit scope is invalid")
        if self.limit < 1:
            raise ValueError("rate limit must be positive")
        if self.window_seconds < 1:
            raise ValueError("rate limit window must be positive")


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int
    reset_after_seconds: int
    rule_name: str


def subject_digest(subject: str) -> str:
    clean = subject.strip()
    if not clean:
        raise ValueError("rate limit subject is required")
    return hashlib.sha256(clean.encode("utf-8")).hexdigest()


class RedisRateLimiter:
    def __init__(self, redis: Any, *, key_prefix: str = "rate:") -> None:
        self._redis = redis
        self._key_prefix = key_prefix

    async def check(self, rule: RateLimitRule, subject: str) -> RateLimitDecision:
        key = f"{self._key_prefix}{rule.scope}:{rule.name}:{subject_digest(subject)}"
        try:
            raw = await self._redis.eval(_CHECK_SCRIPT, 1, key, rule.limit, rule.window_seconds)
        except (RedisError, ValueError, TypeError):
            raise RateLimitStoreUnavailable() from None
        try:
            allowed_raw, count_raw, ttl_raw = raw
            allowed = int(allowed_raw) == 1
            count = int(count_raw)
            ttl = int(ttl_raw)
        except (TypeError, ValueError):
            raise RateLimitStoreUnavailable() from None
        if ttl < 0:
            raise RateLimitStoreUnavailable()
        remaining = max(rule.limit - count, 0)
        return RateLimitDecision(
            allowed=allowed,
            limit=rule.limit,
            remaining=remaining,
            retry_after_seconds=0 if allowed else ttl,
            reset_after_seconds=ttl,
            rule_name=rule.name,
        )
```

`__init__.py` 导出上述类型。

- [ ] **Step 4: 运行 GREEN**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/rate_limit/test_redis_rate_limiter.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add atguigu_ai/rate_limit tests/unit/rate_limit/test_redis_rate_limiter.py
git commit -m "feat: add redis rate limiter core"
```

## Task 2: Auth route 限流接入

**Files:**
- Modify: `atguigu_ai/api/dependencies.py`
- Modify: `atguigu_ai/api/routes/auth.py`
- Test: `tests/unit/api/test_auth_routes.py`

- [ ] **Step 1: 写 RED 测试**

在 auth route 测试中加入 fake limiter：

```python
class FakeRateLimiter:
    def __init__(self):
        self.blocked_rules = set()
        self.unavailable_rules = set()
        self.calls = []

    async def check(self, rule, subject):
        self.calls.append((rule.name, subject))
        if rule.name in self.unavailable_rules:
            from atguigu_ai.rate_limit import RateLimitStoreUnavailable
            raise RateLimitStoreUnavailable()
        allowed = rule.name not in self.blocked_rules
        return SimpleNamespace(
            allowed=allowed,
            limit=rule.limit,
            remaining=0 if not allowed else rule.limit - 1,
            retry_after_seconds=60 if not allowed else 0,
            reset_after_seconds=60,
            rule_name=rule.name,
        )
```

新增断言：

```python
def test_register_rate_limit_blocks_before_service_call():
    client, service, limiter = build_client_with_rate_limiter()
    limiter.blocked_rules.add("auth.register.ip")

    response = client.post("/api/auth/register", json={"email": "user@example.com", "password": "Valid123!"})

    assert response.status_code == 429
    assert response.json() == {"detail": "Too many requests"}
    assert service.register_calls == []


def test_login_checks_ip_email_and_ip_rules_before_service_call():
    client, service, limiter = build_client_with_rate_limiter()
    limiter.blocked_rules.add("auth.login.ip_email")

    response = client.post("/api/auth/login", json={"email": "user@example.com", "password": "Valid123!"})

    assert response.status_code == 429
    assert ("auth.login.ip_email",) == (limiter.calls[0][0],)
    assert service.login_calls == []
    assert "set-cookie" not in response.headers


def test_change_password_rate_limit_uses_authenticated_account_after_csrf():
    client, service, limiter = build_client_with_rate_limiter()
    limiter.blocked_rules.add("auth.change_password.account")

    response = authenticated_request(
        client,
        "/api/auth/change-password",
        {"current_password": "Old123!", "new_password": "New123!"},
    )

    assert response.status_code == 429
    assert any(call == ("auth.change_password.account", "account-1") for call in limiter.calls)
    assert service.change_password_calls == []


def test_rate_limiter_outage_returns_sanitized_503():
    client, service, limiter = build_client_with_rate_limiter()
    limiter.unavailable_rules.add("auth.register.ip")

    response = client.post("/api/auth/register", json={"email": "user@example.com", "password": "Valid123!"})

    assert response.status_code == 503
    assert response.json() == {"detail": "Rate limit service is unavailable"}
    assert "redis" not in response.text.lower()
    assert service.register_calls == []
```

- [ ] **Step 2: 运行 RED**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/api/test_auth_routes.py -q
```

Expected: FAIL，原因是依赖对象没有 `rate_limiter`，route 未调用限流。

- [ ] **Step 3: 写最小实现**

`AuthRouteDependencies` 增加：

```python
from collections.abc import Callable
from typing import Any

rate_limiter: Any | None = None
client_ip_resolver: Callable[[Request], str] | None = None
```

新增 helper：

```python
def client_ip(request: Request, deps: AuthRouteDependencies) -> str:
    if deps.client_ip_resolver is not None:
        return deps.client_ip_resolver(request)
    return request.client.host if request.client else "unknown"
```

auth route 中定义规则常量，按 spec 顺序调用 `_check_rate_limit`；超限抛 429，不可用抛 503。

- [ ] **Step 4: 运行 GREEN**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/api/test_auth_routes.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add atguigu_ai/api/dependencies.py atguigu_ai/api/routes/auth.py tests/unit/api/test_auth_routes.py
git commit -m "feat: enforce auth route rate limits"
```

## Task 3: Chat route 限流接入

**Files:**
- Modify: `atguigu_ai/api/routes/chat.py`
- Test: `tests/unit/api/test_chat_routes.py`

- [ ] **Step 1: 写 RED 测试**

新增：

```python
def test_chat_message_rate_limit_blocks_before_payload_and_agent():
    client, agent, sessions, resolver, limiter = build_client_with_rate_limiter()
    limiter.blocked_rules.add("chat.messages.account")

    response = authenticated_request(client, "/api/chat/messages", {"message": "hello"})

    assert response.status_code == 429
    assert agent.messages == []
    assert any(call == ("chat.messages.account", "account-1") for call in limiter.calls)


def test_chat_reset_rate_limit_blocks_before_reset_tracker():
    client, agent, sessions, resolver, limiter = build_client_with_rate_limiter()
    limiter.blocked_rules.add("chat.reset.account")

    response = authenticated_request(client, "/api/chat/reset")

    assert response.status_code == 429
    assert agent.reset_calls == []


def test_chat_rate_limit_uses_server_account_not_forged_metadata():
    client, agent, sessions, resolver, limiter = build_client_with_rate_limiter()

    response = authenticated_request(
        client,
        "/api/chat/messages",
        {"message": "hello", "metadata": {"account_id": "attacker-account"}},
    )

    assert response.status_code == 200
    assert ("chat.messages.account", "account-1") in limiter.calls
```

- [ ] **Step 2: 运行 RED**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/api/test_chat_routes.py -q
```

Expected: FAIL，原因是 chat route 未调用 limiter。

- [ ] **Step 3: 写最小实现**

在 `_require_business_identity` 后、`_message_payload` 或 `reset_tracker` 前检查：

```python
await check_rate_limit(auth_deps.rate_limiter, CHAT_MESSAGES_RULE, identity.account_id)
```

不可用返回 `503 Rate limit service is unavailable`，超限返回 429。

- [ ] **Step 4: 运行 GREEN**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/api/test_chat_routes.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add atguigu_ai/api/routes/chat.py tests/unit/api/test_chat_routes.py
git commit -m "feat: enforce chat route rate limits"
```

## Task 4: 真实 Redis 集成与压力边界

**Files:**
- Create: `tests/integration/test_redis_rate_limiter.py`

- [ ] **Step 1: 写 RED 集成测试**

```python
import asyncio
import pytest

from atguigu_ai.rate_limit import RateLimitRule, RedisRateLimiter
from tests.integration.test_redis_session import client, wait_for_redis


@pytest.fixture
async def redis_client():
    await wait_for_redis()
    redis = client()
    await redis.flushdb()
    try:
        yield redis
    finally:
        await wait_for_redis()
        await redis.flushdb()
        await redis.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_requests_never_exceed_limit(redis_client):
    limiter = RedisRateLimiter(redis_client)
    rule = RateLimitRule(name="chat.messages.account", scope="chat", limit=10, window_seconds=60)

    decisions = await asyncio.gather(*(limiter.check(rule, "account-1") for _ in range(80)))

    assert sum(1 for decision in decisions if decision.allowed) == 10
    assert sum(1 for decision in decisions if not decision.allowed) == 70


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ttl_is_not_refreshed_inside_fixed_window(redis_client):
    limiter = RedisRateLimiter(redis_client)
    rule = RateLimitRule(name="auth.register.ip", scope="auth", limit=5, window_seconds=60)

    await limiter.check(rule, "127.0.0.1")
    key = next([key async for key in redis_client.scan_iter(match="rate:auth:auth.register.ip:*")])
    first_ttl = await redis_client.ttl(key)
    await asyncio.sleep(1.1)
    await limiter.check(rule, "127.0.0.1")
    second_ttl = await redis_client.ttl(key)

    assert second_ttl < first_ttl
```

- [ ] **Step 2: 运行 RED**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_redis_rate_limiter.py -q -s -m integration
```

Expected: 如果 Task 1 只用 fake 通过，真实 Redis 并发或 async fixture 细节可能失败；根据失败信息修测试夹具或 Lua 返回解析。

- [ ] **Step 3: 修到 GREEN**

只允许修改 limiter 或测试夹具，不改变 auth/chat 行为。确认 Redis DB15 测试前后清空。

- [ ] **Step 4: 运行 GREEN 与量化输出**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_redis_rate_limiter.py -q -s -m integration
docker exec llm-cs-redis redis-cli -n 15 DBSIZE
```

Expected: pytest PASS，`DBSIZE` 输出 `0`。

- [ ] **Step 5: 提交**

```powershell
git add tests/integration/test_redis_rate_limiter.py atguigu_ai/rate_limit
git commit -m "test: verify redis rate limiter integration"
```

## Task 5: 关键写 Action 限流

**Files:**
- Modify: `ecs_demo/actions/security.py`
- Modify: `ecs_demo/actions/action_order.py`
- Modify: `ecs_demo/actions/action_postsale.py`
- Test: `tests/unit/actions/test_action_rate_limit.py`

- [ ] **Step 1: 写 RED 测试**

覆盖修改地址、取消订单、提交售后三个确认分支：

```python
class BlockingActionLimiter:
    async def check(self, rule, subject):
        return SimpleNamespace(
            allowed=False,
            limit=rule.limit,
            remaining=0,
            retry_after_seconds=60,
            reset_after_seconds=60,
            rule_name=rule.name,
        )


def test_cancel_order_rate_limit_blocks_database_write(action_db_session, dispatcher, tracker):
    action = ActionCancelOrder()

    events = action.run(
        dispatcher,
        tracker_with_slots({"order_id": "order-1"}),
        {},
        account_id="account-1",
        user_id="user-1",
        rate_limiter=BlockingActionLimiter(),
    )

    assert any("系统繁忙" in event.get("text", "") or "稍后" in event.get("text", "") for event in dispatcher.messages)
    assert action_db_session.get(OrderInfo, "order-1").order_status != "已取消"
```

另外给 `ActionAskSetReceiveInfo` 和 `ActionApplyPostsale` 写同类断言，确保没有新增地址、没有售后记录。

- [ ] **Step 2: 运行 RED**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/actions/test_action_rate_limit.py -q
```

Expected: FAIL，原因是 Action 未识别 `rate_limiter`。

- [ ] **Step 3: 写最小实现**

在 `security.py` 增加：

```python
ACTION_MUTATION_RULE = RateLimitRule(
    name="action.critical_mutation.account",
    scope="action",
    limit=10,
    window_seconds=60,
)

async def require_action_rate_limit(rate_limiter, *, account_id: str, action_name: str) -> None:
    if rate_limiter is None:
        return
    try:
        decision = await rate_limiter.check(ACTION_MUTATION_RULE, f"{account_id}:{action_name}")
    except RateLimitStoreUnavailable:
        raise ActionSecurityError("系统繁忙，请稍后重试") from None
    if not decision.allowed:
        raise ActionSecurityError("系统繁忙，请稍后重试")
```

由于 Rasa Action `run` 是同步函数，如果 limiter 是异步接口，先提供同步桥接 helper：在没有运行中 event loop 时使用 `asyncio.run`；如果存在运行中 loop，则要求注入同步包装器并在测试中覆盖。实现时以当前 action 调用方式为准，不能引入“协程未 await”的隐患。

- [ ] **Step 4: 运行 GREEN**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/actions/test_action_rate_limit.py tests/unit/actions/test_action_security.py tests/unit/actions/test_action_ownership.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add ecs_demo/actions/security.py ecs_demo/actions/action_order.py ecs_demo/actions/action_postsale.py tests/unit/actions/test_action_rate_limit.py
git commit -m "feat: enforce action mutation rate limits"
```

## Task 6: 端到端、风险、压力、量化报告

**Files:**
- Modify or Create: `tests/integration/test_auth_routes_http.py`
- Modify or Create: `tests/integration/test_chat_authorization_http.py`
- Create: `docs/reports/integration/2026-07-19-production-rate-limiting.md`

- [ ] **Step 1: 写 HTTP RED 测试**

扩展真实 HTTP fixture，注入 `RedisRateLimiter(redis)` 后验证：

```python
async def test_register_rate_limit_blocks_second_request_before_service(client):
    for _ in range(5):
        response = await client.post("/api/auth/register", json={"email": "rate@example.com", "password": "Valid123!"})
        assert response.status_code == 202

    blocked = await client.post("/api/auth/register", json={"email": "rate@example.com", "password": "Valid123!"})

    assert blocked.status_code == 429
    assert blocked.json() == {"detail": "Too many requests"}
```

chat 集成测试同样验证第 31 次消息返回 429，Agent 调用次数保持 30。

- [ ] **Step 2: 运行 RED**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_auth_routes_http.py tests/integration/test_chat_authorization_http.py -q -s -m integration
```

Expected: FAIL，原因是 fixture 尚未注入生产 limiter 或规则窗口需要测试参数化。

- [ ] **Step 3: 修到 GREEN**

在测试 fixture 中显式注入短窗口或默认规则，不能用 sleep 绕过失败。保持 DB15 清理。

- [ ] **Step 4: 全量验证**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/rate_limit tests/unit/api/test_auth_routes.py tests/unit/api/test_chat_routes.py tests/unit/actions -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_redis_rate_limiter.py -q -s -m integration
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests -q
git diff --check
docker exec llm-cs-redis redis-cli -n 15 DBSIZE
```

Expected: 单元、集成、全量测试全部 PASS；`git diff --check` 无输出；Redis DB15 输出 `0`。

- [ ] **Step 5: 写中文报告**

报告必须包含：

- 正常用户路径：注册、登录、chat messages、chat reset、关键写 Action。
- 刁钻测试：伪造 metadata/account_id、重复邮件、token 原文不入 key、CSRF 顺序。
- 压力测试：同一 key 并发 80 次，允许次数不超过 limit。
- 风险测试：Redis 停止、错误 key 类型、敏感信息脱敏、TTL 不刷新。
- 量化数据：测试数量、并发样本数、blocked/allowed 计数、DB15 清理结果。

- [ ] **Step 6: 独立 Review/QA**

派一个独立 reviewer 检查：

- spec 验收标准是否都有测试或报告证据。
- 是否有超限后仍调用 service/Agent/Action。
- 是否有敏感原文进入 Redis key、HTTP 响应、日志或测试输出。
- 是否有未 await 的 coroutine、同步 Action 中错误使用 async limiter。

- [ ] **Step 7: 最终提交和推送**

```powershell
git add .
git commit -m "docs: report production rate limiting verification"
git push origin master
git ls-remote origin refs/heads/master
```

## 自检清单

- spec 的 6 条验收标准分别落在 Task 1、2、3、4、5、6。
- 没有数据库 schema migration。
- 未引入第三方限流库。
- 未配置可信代理时不读取 forwarded headers。
- 公共 auth 响应不泄漏账号存在性。
- 所有新增文档和报告使用中文。
- 每个生产代码变更前都有 RED 测试，且记录失败原因。
