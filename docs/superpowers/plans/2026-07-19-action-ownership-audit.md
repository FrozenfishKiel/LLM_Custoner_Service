# Action 归属、事务、幂等和审计 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让订单、物流、地址和售后 Action 只能处理当前登录业务用户的数据，并让关键写操作具备事务、幂等和审计。

**Architecture:** 在 `ecs_demo/actions/security.py` 增加共享 Action 安全上下文和守卫函数，Action 从 `kwargs` 读取 chat route 注入的可信 `account_id/user_id`，不再信任 tracker 槽里的身份。读操作统一加归属条件；写操作在单事务内完成归属校验、业务状态校验、修改和审计。

**Tech Stack:** Python 3.12、SQLAlchemy ORM、pytest、pytest-asyncio、真实 MySQL integration fixture、现有 `audit_event` 表和 `AccountRepository.record_audit()` 字段契约。

---

## 文件地图

- 创建 `ecs_demo/actions/security.py`：Action 身份解析、归属查询、审计 helper。
- 修改 `ecs_demo/actions/action_order.py`：订单详情、地址选择、修改地址、取消订单归属与写操作硬化。
- 修改 `ecs_demo/actions/action_logistics.py`：物流查询归属硬化。
- 修改 `ecs_demo/actions/action_postsale.py`：售后资格、原因、创建售后归属/幂等/审计硬化。
- 创建 `tests/unit/actions/test_action_security.py`：共享 helper 单元测试。
- 创建 `tests/unit/actions/test_action_ownership.py`：Action 级别越权和幂等单元测试，优先使用 monkeypatch fake session。
- 创建 `tests/integration/test_action_ownership_audit.py`：真实 MySQL 双用户隔离、事务、审计和 cleanup 测试。
- 修改 `docs/TECHNICAL_DESIGN.md`：记录本 slice 的 Action 归属、幂等和审计落点。
- 创建最终报告 `docs/reports/integration/2026-07-19-action-ownership-audit.md` 与 evidence 文件。

## Task 1：RED - Action 安全上下文和归属 helper 合约

**Files:**
- Create: `tests/unit/actions/test_action_security.py`

- [ ] **Step 1：写身份解析失败测试**

在 `tests/unit/actions/test_action_security.py` 写入：

```python
from __future__ import annotations

import pytest

from ecs_demo.actions.security import ActionSecurityError, current_action_user


class Tracker:
    def __init__(self, slots=None):
        self.slots = slots or {}

    def get_slot(self, name):
        return self.slots.get(name)


def test_current_action_user_uses_trusted_kwargs() -> None:
    context = current_action_user(
        Tracker({"user_id": "attacker"}),
        account_id="account-1",
        user_id="business-user-1",
        account_role="consumer",
        request_id="request-1",
    )

    assert context.account_id == "account-1"
    assert context.user_id == "business-user-1"
    assert context.role == "consumer"
    assert context.request_id == "request-1"


def test_current_action_user_rejects_missing_trusted_identity_by_default() -> None:
    with pytest.raises(ActionSecurityError):
        current_action_user(Tracker({"user_id": "1001"}))


def test_current_action_user_demo_fallback_must_be_explicit() -> None:
    context = current_action_user(
        Tracker({"user_id": "1001"}),
        allow_demo_identity_fallback=True,
        account_id="demo-account",
    )

    assert context.account_id == "demo-account"
    assert context.user_id == "1001"
```

- [ ] **Step 2：写归属查询和审计 helper 失败测试**

追加：

```python
from dataclasses import dataclass

from ecs_demo.actions.security import (
    ActionUserContext,
    audit_metadata,
    owned_order_query,
)


@dataclass
class Order:
    order_id: str
    user_id: str


class Query:
    def __init__(self, rows):
        self.rows = rows
        self.conditions = []

    def filter(self, *conditions):
        self.conditions.extend(str(condition) for condition in conditions)
        return self

    def first(self):
        for row in self.rows:
            if row.order_id == "order-1" and row.user_id == "business-user-1":
                return row
        return None


class Session:
    def __init__(self, rows):
        self.rows = rows

    def query(self, model):
        return Query(self.rows)


def test_audit_metadata_is_small_and_non_sensitive() -> None:
    metadata = audit_metadata(
        action_name="action_cancel_order",
        order_id="order-1",
        previous_status="待发货",
        raw_token="secret",
        csrf="secret",
    )

    assert metadata == {
        "action_name": "action_cancel_order",
        "order_id": "order-1",
        "previous_status": "待发货",
    }
```

`owned_order_query` 需要在 GREEN 阶段用真实 SQLAlchemy model 验证；这里先固定 helper API，不在 fake 条件表达式里模拟 SQLAlchemy。

- [ ] **Step 3：运行 RED**

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/actions/test_action_security.py -q
```

预期：collection/import 失败，因为 `ecs_demo.actions.security` 尚不存在。

- [ ] **Step 4：提交 RED**

```powershell
git add tests/unit/actions/test_action_security.py
git diff --cached --check
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "test: define action security contract"
```

## Task 2：GREEN - 实现 Action 安全 helper

**Files:**
- Create: `ecs_demo/actions/security.py`
- Modify: `tests/unit/actions/test_action_security.py`

- [ ] **Step 1：实现 `ActionUserContext` 和身份解析**

创建 `ecs_demo/actions/security.py`：

```python
from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4


class ActionSecurityError(RuntimeError):
    def __init__(self, message: str = "Action identity is unavailable") -> None:
        super().__init__(message)


@dataclass(frozen=True)
class ActionUserContext:
    account_id: str
    user_id: str
    role: str
    request_id: str


def _non_blank(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def current_action_user(
    tracker,
    *,
    account_id: object | None = None,
    user_id: object | None = None,
    account_role: object | None = None,
    request_id: object | None = None,
    allow_demo_identity_fallback: bool = False,
    **_: object,
) -> ActionUserContext:
    trusted_account_id = _non_blank(account_id)
    trusted_user_id = _non_blank(user_id)
    if trusted_user_id is None and allow_demo_identity_fallback:
        trusted_user_id = _non_blank(tracker.get_slot("user_id"))
    if trusted_account_id is None and allow_demo_identity_fallback:
        trusted_account_id = "demo-account"
    if trusted_account_id is None or trusted_user_id is None:
        raise ActionSecurityError()
    return ActionUserContext(
        account_id=trusted_account_id,
        user_id=trusted_user_id,
        role=_non_blank(account_role) or "consumer",
        request_id=_non_blank(request_id) or f"action-{uuid4()}",
    )
```

- [ ] **Step 2：实现归属和审计 helper**

在同文件追加：

```python
SENSITIVE_METADATA_KEYS = {
    "password",
    "token",
    "session",
    "secret",
    "csrf",
    "raw_token",
    "session_id",
    "client_secret",
}


def owned_order_query(session, order_model, *, user_id: str, order_id: str):
    return session.query(order_model).filter(
        order_model.order_id == order_id,
        order_model.user_id == user_id,
    )


def audit_metadata(**values: object) -> dict[str, object]:
    clean: dict[str, object] = {}
    for key, value in values.items():
        lowered = key.lower()
        if any(sensitive in lowered for sensitive in SENSITIVE_METADATA_KEYS):
            continue
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            clean[key] = value
    return clean


def record_action_audit(
    session,
    *,
    context: ActionUserContext,
    event_type: str,
    target_type: str,
    target_id: str,
    result: str,
    metadata: dict[str, object] | None = None,
) -> None:
    from atguigu_ai.auth import AccountRepository, AccountRole, AuditResult

    repository = AccountRepository(session)
    repository.record_audit(
        request_id=context.request_id,
        actor_account_id=context.account_id,
        actor_role=AccountRole(context.role),
        event_type=event_type,
        target_type=target_type,
        target_id=target_id,
        result=AuditResult(result),
        metadata=metadata,
    )
```

- [ ] **Step 3：运行 GREEN**

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/actions/test_action_security.py -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m compileall -q ecs_demo/actions tests/unit/actions
git diff --check
```

预期：新增 action security 单测通过，compileall exit 0。

- [ ] **Step 4：提交 GREEN**

```powershell
git add ecs_demo/actions/security.py tests/unit/actions/test_action_security.py
git diff --cached --check
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "feat: add action security helpers"
```

## Task 3：RED/GREEN - 查询类 Action 归属硬化

**Files:**
- Modify: `ecs_demo/actions/action_order.py`
- Modify: `ecs_demo/actions/action_logistics.py`
- Modify: `ecs_demo/actions/action_postsale.py`
- Create: `tests/unit/actions/test_action_ownership.py`

- [ ] **Step 1：写越权查询失败测试**

创建 `tests/unit/actions/test_action_ownership.py`，用 monkeypatch 替换 `actions.db.SessionLocal` 和 `actions.db_table_class`，至少覆盖：

```python
import pytest

from ecs_demo.actions.action_order import ActionGetOrderDetail
from ecs_demo.actions.action_logistics import ActionGetLogisticsInfo
from ecs_demo.actions.action_postsale import ActionCheckPostsaleEligible


class Tracker:
    def __init__(self, slots):
        self.slots = slots
        self.set_values = {}

    def get_slot(self, name):
        return self.slots.get(name)

    def set_slot(self, name, value):
        self.set_values[name] = value


@pytest.mark.asyncio
async def test_order_detail_requires_trusted_user_identity() -> None:
    result = await ActionGetOrderDetail().run(Tracker({"order_id": "order-1"}))

    assert result.responses[0]["text"] == "当前登录身份不可用，请重新登录后再试。"
```

后续测试在 GREEN 阶段补真实 fake session，验证传入 `user_id="user-a"` 时不会返回 `user-b` 订单。

- [ ] **Step 2：查询 Action 使用 `current_action_user`**

在三个 Action 文件中导入：

```python
from actions.security import ActionSecurityError, current_action_user, owned_order_query
```

将读操作统一调整：

```python
try:
    context = current_action_user(tracker, **kwargs)
except ActionSecurityError:
    result.add_response("当前登录身份不可用，请重新登录后再试。")
    return result
```

订单详情、物流、售后资格、售后原因都必须用 `context.user_id` 加归属条件。不得再用：

```python
tracker.get_slot("user_id") or "1001"
session.query(OrderInfo).filter_by(order_id=order_id).first()
```

- [ ] **Step 3：运行查询 Action 测试**

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/actions/test_action_ownership.py -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/api/test_chat_routes.py tests/unit/agent/test_agent_production_flows.py -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m compileall -q ecs_demo/actions tests/unit/actions
git diff --check
```

- [ ] **Step 4：提交查询硬化**

```powershell
git add ecs_demo/actions/action_order.py ecs_demo/actions/action_logistics.py ecs_demo/actions/action_postsale.py tests/unit/actions/test_action_ownership.py
git diff --cached --check
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "fix: enforce action read ownership"
```

## Task 4：RED/GREEN - 写操作事务、幂等和审计

**Files:**
- Modify: `ecs_demo/actions/action_order.py`
- Modify: `ecs_demo/actions/action_postsale.py`
- Modify: `tests/unit/actions/test_action_ownership.py`

- [ ] **Step 1：写取消订单幂等和越权失败测试**

在 `tests/unit/actions/test_action_ownership.py` 追加测试：

```python
@pytest.mark.asyncio
async def test_cancel_order_requires_owned_order(monkeypatch) -> None:
    result = await ActionCancelOrder().run(
        Tracker({"order_id": "order-b"}),
        account_id="account-a",
        user_id="user-a",
        account_role="consumer",
        request_id="request-a",
    )

    assert result.responses[0]["text"] in {
        "未找到该订单，请检查订单号是否正确。",
        "当前登录身份不可用，请重新登录后再试。",
    }
```

实现 fake session 后把断言收紧为越权返回“未找到该订单，请检查订单号是否正确。”，并验证不会 commit。

- [ ] **Step 2：写售后重复提交失败测试**

追加：

```python
@pytest.mark.asyncio
async def test_apply_postsale_is_idempotent_for_same_order_type_and_reason(monkeypatch) -> None:
    result = await ActionApplyPostsale().run(
        Tracker({
            "order_id": "order-a",
            "postsale_type": "退款",
            "postsale_reason": "不想要了",
        }),
        account_id="account-a",
        user_id="user-a",
        account_role="consumer",
        request_id="request-a",
    )

    assert result.responses
```

GREEN 阶段补 fake session 或 integration 覆盖“重复提交后售后记录数量不增加”。

- [ ] **Step 3：实现写操作事务和审计**

修改地址、取消订单、提交售后必须使用：

```python
with SessionLocal() as session:
    try:
        order_info = owned_order_query(
            session,
            OrderInfo,
            user_id=context.user_id,
            order_id=order_id,
        ).with_for_update().first()
        if order_info is None:
            record_action_audit(
                session,
                context=context,
                event_type="business.order.cancel",
                target_type="order",
                target_id=order_id,
                result="failure",
                metadata=audit_metadata(action_name=self.name, reason="not_found_or_not_owned"),
            )
            session.commit()
            result.add_response("未找到该订单，请检查订单号是否正确。")
            return result
        old_order_status = order_info.order_status
        if old_order_status == "已取消":
            session.commit()
            result.add_response("订单已取消")
            return result
        order_info.order_status = "已取消"
        order_info.complete_time = datetime.now()
        record_action_audit(
            session,
            context=context,
            event_type="business.order.cancel",
            target_type="order",
            target_id=order_id,
            result="success",
            metadata=audit_metadata(action_name=self.name, previous_status=old_order_status),
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.error("取消订单失败: %s", exc)
        result.add_response("取消失败，请稍后重试。")
```

失败审计只在已取得 `context` 且 target_id 可用时记录；记录失败审计后也要 commit 审计或随业务事务回滚，计划采用“业务失败但数据库可用时记录 failure 审计并 commit；数据库异常时只写脱敏日志”。

- [ ] **Step 4：运行写操作测试**

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/actions/test_action_ownership.py -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth/test_account_repository.py tests/unit/auth/test_models.py -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m compileall -q ecs_demo/actions tests/unit/actions
git diff --check
```

- [ ] **Step 5：提交写操作硬化**

```powershell
git add ecs_demo/actions/action_order.py ecs_demo/actions/action_postsale.py tests/unit/actions/test_action_ownership.py
git diff --cached --check
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "fix: harden action mutations with audit"
```

## Task 5：真实 MySQL 集成测试与技术设计更新

**Files:**
- Create: `tests/integration/test_action_ownership_audit.py`
- Modify: `docs/TECHNICAL_DESIGN.md`

- [ ] **Step 1：写真实 MySQL 双用户隔离测试**

`tests/integration/test_action_ownership_audit.py` 复用 `tests/integration/test_account_migration.py` 的 `_isolated_mysql_database()` 和 `_alembic_config()`，并在临时库中插入：

- 两个 `user_info`
- 两个 `account`
- 两条 `account_user_binding`
- 各自订单、地址、物流和订单明细

测试覆盖：

```python
async def test_order_detail_does_not_cross_business_user(action_db_fixture)
async def test_logistics_does_not_cross_business_user(action_db_fixture)
async def test_cancel_order_is_owned_idempotent_and_audited(action_db_fixture)
async def test_apply_postsale_is_owned_idempotent_and_audited(action_db_fixture)
async def test_mutation_rolls_back_on_downstream_failure(action_db_fixture, monkeypatch)
```

- [ ] **Step 2：运行集成 RED/GREEN**

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_action_ownership_audit.py -q -s -m integration
```

预期 GREEN 后所有测试通过，且 module cleanup 验证无 `llm_cs_test_*` 临时库残留。

- [ ] **Step 3：更新中文技术设计**

在 `docs/TECHNICAL_DESIGN.md` 的 8.4 业务越权防护和 PRD 追踪矩阵附近补充：

- Action 从可信 metadata 获取业务用户。
- 订单、物流、地址、售后统一归属校验。
- 修改地址、取消订单、提交售后写入 `audit_event`。
- 重复取消和重复售后申请的幂等语义。

- [ ] **Step 4：提交集成与设计**

```powershell
git add tests/integration/test_action_ownership_audit.py docs/TECHNICAL_DESIGN.md
git diff --cached --check
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "test: cover action ownership audit integration"
```

## Task 6：证据、独立 QA 和最终报告

**Files:**
- Create: `docs/reports/integration/2026-07-19-action-ownership-audit.md`
- Create: `docs/reports/integration/evidence/action-ownership-audit-*.txt`
- Create: `docs/reports/integration/evidence/action-ownership-audit-independent-qa.md`
- Modify: `docs/superpowers/plans/2026-07-19-action-ownership-audit.md`

- [ ] **Step 1：保存验证证据**

运行并保存为 UTF-8 无 BOM：

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/actions tests/unit/api/test_chat_routes.py tests/unit/auth/test_account_repository.py tests/unit/auth/test_models.py -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_action_ownership_audit.py -q -s -m integration
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/agent tests/unit/core tests/unit/auth tests/unit/api tests/unit/actions tests/security -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m compileall -q ecs_demo/actions atguigu_ai tests/unit/actions tests/integration
git diff --check
docker exec llm-cs-redis redis-cli -n 15 DBSIZE
```

- [ ] **Step 2：做 scoped secret scan**

扫描本 slice 代码、测试、中文文档和 evidence，模式至少覆盖 private key、`sk-*`、credential-bearing Redis/MySQL URL、SMTP password、Authorization Bearer、Cookie、raw session/csrf token。

- [ ] **Step 3：独立 QA**

复用或新建 QA agent，要求重跑目标单测、真实 MySQL 集成、相关回归、全量测试、compileall、diff check、Redis cleanup、MySQL 临时库 cleanup、UTF-8 evidence 和 secret scan。结论必须为 APPROVED，且无 Critical/Important finding。

- [ ] **Step 4：写中文报告并提交**

报告包含：

- 本 slice 做了什么。
- B-01 至 B-10 覆盖情况。
- 越权、幂等、事务、审计测试结果。
- Redis/MySQL cleanup 结果。
- QA 结论。
- 距离上线仍剩的工程项。

提交：

```powershell
git add docs/superpowers/plans/2026-07-19-action-ownership-audit.md docs/reports/integration/2026-07-19-action-ownership-audit.md docs/reports/integration/evidence/action-ownership-audit-*
git diff --cached --check
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "docs: record action ownership audit verification"
git status --short
```

## 完成门禁

本 slice 只有满足以下条件才算完成：

- Action 不再默认从 tracker 槽或 `"1001"` 获取生产身份。
- 订单、物流、地址、售后查询都按当前业务用户归属过滤。
- 修改地址、取消订单、提交售后具备事务、幂等和审计。
- 越权对象与不存在对象对用户表现一致。
- 关键写操作成功和业务失败都有审计记录；数据库异常不泄露敏感细节。
- 真实 MySQL 双用户集成测试通过。
- 全量 `pytest tests -q` 通过。
- Redis DB15 和 MySQL 临时库 cleanup 均为 0。
- scoped secret scan 无发现。
- 独立 QA APPROVED。
- 工作区干净。
