# Redis TrackerStore 与生产 Flow 安全实施计划

> **给 agentic workers：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 按任务执行。所有步骤使用 checkbox（`- [ ]`）跟踪。

**目标：** 新增 Redis TrackerStore，并让生产 Agent 默认不加载 `switch_user_id` 这类 demo 身份切换 Flow。

**架构：** `RedisTrackerStore` 放在 `atguigu_ai/core/stores/redis_store.py`，继续实现现有 `TrackerStore` 抽象；工厂和配置层只负责把 endpoint 配置传入。Flow 安全放在 `Agent.load()` 组装边界：课程资源保留，生产默认过滤，测试/demo 可显式允许。

**技术栈：** Python 3.12、redis.asyncio、pytest、pytest-asyncio、真实 Redis Docker 集成测试、现有 Agent/FlowLoader/TrackerStore 抽象。

---

## 锁定合约

- `create_tracker_store("redis", ...)` 返回 `RedisTrackerStore`。
- Redis key 默认格式为 `tracker:{sender_id}`；chat 授权层传入的 sender_id 已经是 `account:{account_id}`，因此实际生产 key 为 `tracker:account:{account_id}`。
- `RedisTrackerStore.save()` 用 JSON 保存 `DialogueStateTracker.to_dict()`。
- `RedisTrackerStore.retrieve()` 缺失返回 `None`，存在则恢复 `DialogueStateTracker` 并应用 domain slots。
- `RedisTrackerStore.delete()` 删除单个 tracker。
- `RedisTrackerStore.keys()` 只返回当前 `key_prefix` 下的 sender_id，不泄露其它 Redis key。
- Redis 连接、超时、协议错误对外只暴露脱敏 `TrackerStoreConnectionError("Tracker store is unavailable")` 或 `TrackerStoreException("Tracker store is unavailable")`。
- `ttl_seconds` 为空表示不设置 TTL；提供时必须是正整数。
- `TrackerStoreConfig` 支持 `type/url/host/port/db/path/key_prefix/ttl_seconds`。
- `Agent.load()` 传递 tracker store 的完整非空配置，不再只传 `path`。
- `AgentConfig()` 默认 `runtime_mode="production"` 且 `allow_demo_identity_flows=False`。
- `Agent.load(..., config=AgentConfig())` 默认移除 `switch_user_id`。
- `Agent.load(..., config=AgentConfig(allow_demo_identity_flows=True))` 保留 `switch_user_id`。
- 不修改或删除 `ecs_demo/data/flows/flow_order.yml` 中的 demo flow 定义。
- 新增/更新文档必须用中文正文。

## 文件地图

- 创建 `atguigu_ai/core/stores/redis_store.py`：Redis TrackerStore 实现。
- 修改 `atguigu_ai/core/stores/__init__.py`：导出 `RedisTrackerStore`，工厂支持 `redis`。
- 修改 `atguigu_ai/shared/constants.py`：新增 `TRACKER_STORE_REDIS`。
- 修改 `atguigu_ai/shared/config.py`：`TrackerStoreConfig` 新增 `key_prefix`、`ttl_seconds`，并解析完整配置。
- 修改 `atguigu_ai/agent/agent.py`：`AgentConfig` 新增生产 Flow 安全字段；`Agent.load()` 过滤 demo 身份 Flow；完整传递 tracker store 配置。
- 创建 `tests/unit/core/test_redis_tracker_store.py`。
- 创建 `tests/unit/core/test_tracker_store_factory.py`。
- 创建 `tests/unit/agent/test_agent_production_flows.py`。
- 创建 `tests/integration/test_redis_tracker_store.py`。
- 修改 `docs/TECHNICAL_DESIGN.md`：记录 Redis TrackerStore 和生产 Flow 过滤已经实现。
- 创建最终报告 `docs/reports/integration/2026-07-19-redis-tracker-production-flow.md` 和 evidence 文件。

## Task 1：RED - Redis TrackerStore 与工厂合约

**文件：**

- 创建 `tests/unit/core/test_redis_tracker_store.py`
- 创建 `tests/unit/core/test_tracker_store_factory.py`

- [ ] **Step 1：写 RedisTrackerStore 单元失败测试**

在 `tests/unit/core/test_redis_tracker_store.py` 中定义一个 async fake Redis：

```python
class RecordingRedis:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}
        self.error = error

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        if self.error:
            raise self.error
        self.values[key] = value
        if ex is not None:
            self.expirations[key] = ex
        return True

    async def get(self, key: str):
        if self.error:
            raise self.error
        return self.values.get(key)

    async def delete(self, key: str) -> int:
        if self.error:
            raise self.error
        existed = key in self.values
        self.values.pop(key, None)
        self.expirations.pop(key, None)
        return 1 if existed else 0

    async def scan_iter(self, match: str):
        if self.error:
            raise self.error
        prefix = match.removesuffix("*")
        for key in list(self.values):
            if key.startswith(prefix):
                yield key
```

覆盖测试：

```python
@pytest.mark.asyncio
async def test_save_and_retrieve_tracker_round_trip_with_ttl() -> None:
    redis = RecordingRedis()
    store = RedisTrackerStore(redis_client=redis, key_prefix="tracker:", ttl_seconds=60)
    tracker = DialogueStateTracker("account:abc")
    tracker.set_slot("order_id", "order-1")

    await store.save(tracker)
    restored = await store.retrieve("account:abc")

    assert restored is not None
    assert restored.sender_id == "account:abc"
    assert restored.get_slot("order_id") == "order-1"
    assert redis.expirations["tracker:account:abc"] == 60

@pytest.mark.asyncio
async def test_missing_delete_and_keys_are_scoped_to_prefix() -> None:
    redis = RecordingRedis()
    redis.values["other:account:abc"] = "{}"
    store = RedisTrackerStore(redis_client=redis, key_prefix="tracker:")

    assert await store.retrieve("missing") is None
    await store.save(DialogueStateTracker("account:abc"))
    await store.save(DialogueStateTracker("account:def"))

    assert sorted(await store.keys()) == ["account:abc", "account:def"]
    await store.delete("account:abc")
    assert await store.retrieve("account:abc") is None

@pytest.mark.asyncio
async def test_redis_errors_are_sanitized() -> None:
    redis = RecordingRedis(error=RuntimeError("internal redis outage detail"))
    store = RedisTrackerStore(redis_client=redis)

    with pytest.raises(TrackerStoreException) as captured:
        await store.save(DialogueStateTracker("account:abc"))

    assert str(captured.value) == "Tracker store is unavailable"
    assert captured.value.__cause__ is None
    assert "internal" not in str(captured.value)

def test_ttl_must_be_positive_integer() -> None:
    with pytest.raises(ValueError):
        RedisTrackerStore(redis_client=RecordingRedis(), ttl_seconds=0)
```

- [ ] **Step 2：写工厂失败测试**

在 `tests/unit/core/test_tracker_store_factory.py` 中覆盖：

```python
def test_factory_creates_redis_tracker_store() -> None:
    store = create_tracker_store(
        "redis",
        redis_client=object(),
        key_prefix="tracker:",
        ttl_seconds=60,
    )
    assert isinstance(store, RedisTrackerStore)

def test_tracker_store_config_parses_redis_options() -> None:
    config = TrackerStoreConfig.from_dict({
        "type": "redis",
        "url": "${REDIS_URL:redis://127.0.0.1:6379/15}",
        "key_prefix": "tracker:",
        "ttl_seconds": 3600,
    })
    assert config.type == "redis"
    assert config.url == "redis://127.0.0.1:6379/15"
    assert config.key_prefix == "tracker:"
    assert config.ttl_seconds == 3600
```

- [ ] **Step 3：运行 RED**

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/core/test_redis_tracker_store.py tests/unit/core/test_tracker_store_factory.py -q
```

预期：collection 或 import 失败，因为 `RedisTrackerStore` 和 `TRACKER_STORE_REDIS` 尚不存在。

- [ ] **Step 4：提交 RED**

```powershell
git add tests/unit/core/test_redis_tracker_store.py tests/unit/core/test_tracker_store_factory.py
git diff --cached --check
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "test: define redis tracker store contract"
```

## Task 2：GREEN - RedisTrackerStore、配置和工厂

**文件：**

- 创建 `atguigu_ai/core/stores/redis_store.py`
- 修改 `atguigu_ai/core/stores/__init__.py`
- 修改 `atguigu_ai/shared/constants.py`
- 修改 `atguigu_ai/shared/config.py`

- [ ] **Step 1：实现 RedisTrackerStore**

实现要点：

```python
class RedisTrackerStore(TrackerStore):
    def __init__(
        self,
        domain: Domain | None = None,
        redis_client: Any | None = None,
        url: str | None = None,
        host: str = "127.0.0.1",
        port: int = 6379,
        db: int = 15,
        key_prefix: str = "tracker:",
        ttl_seconds: int | None = None,
    ) -> None:
        ...
```

私有方法：

- `_key(sender_id)`：拼接 key。
- `_client()`：如果注入 client，直接返回；否则用 `redis.asyncio.Redis.from_url()` 或 host/port/db 创建。
- `_map_error()`：统一抛脱敏 TrackerStore 异常。

- [ ] **Step 2：更新工厂和配置**

更新：

- `TRACKER_STORE_REDIS = "redis"`
- `__all__` 导出 `RedisTrackerStore`
- `create_tracker_store("redis", **kwargs)`
- `TrackerStoreConfig` 增加：

```python
key_prefix: str = "tracker:"
ttl_seconds: Optional[int] = None
```

- [ ] **Step 3：运行 GREEN**

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/core/test_redis_tracker_store.py tests/unit/core/test_tracker_store_factory.py -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/core tests/unit/auth tests/unit/api tests/security -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m compileall -q atguigu_ai/core atguigu_ai/shared tests/unit/core
git diff --check
```

- [ ] **Step 4：提交 GREEN**

```powershell
git add atguigu_ai/core/stores/redis_store.py atguigu_ai/core/stores/__init__.py atguigu_ai/shared/constants.py atguigu_ai/shared/config.py tests/unit/core/test_redis_tracker_store.py tests/unit/core/test_tracker_store_factory.py
git diff --cached --check
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "feat: add redis tracker store"
```

## Task 3：生产 Flow 安全与 Agent 配置接入

**文件：**

- 修改 `atguigu_ai/agent/agent.py`
- 创建 `tests/unit/agent/test_agent_production_flows.py`

- [ ] **Step 1：写生产 Flow 过滤失败测试**

测试用临时项目目录创建最小 `domain.yml`、`data/flows/flow_order.yml`、`endpoints.yml`。

覆盖：

```python
def test_agent_load_filters_switch_user_id_by_default(tmp_path) -> None:
    project = write_project_with_switch_user_flow(tmp_path)
    agent = Agent.load(project, config=AgentConfig())
    assert "switch_user_id" not in agent.flows.flow_ids

def test_agent_load_can_keep_demo_identity_flow_when_explicitly_allowed(tmp_path) -> None:
    project = write_project_with_switch_user_flow(tmp_path)
    agent = Agent.load(project, config=AgentConfig(allow_demo_identity_flows=True))
    assert "switch_user_id" in agent.flows.flow_ids

def test_agent_load_passes_full_tracker_store_config(tmp_path) -> None:
    project = write_project_with_redis_tracker_config(tmp_path)
    agent = Agent.load(project, config=AgentConfig())
    assert isinstance(agent.tracker_store, RedisTrackerStore)
    assert agent.tracker_store.key_prefix == "tracker:"
    assert agent.tracker_store.ttl_seconds == 120
```

- [ ] **Step 2：实现 AgentConfig 与过滤**

在 `AgentConfig` 增加：

```python
runtime_mode: str = "production"
allow_demo_identity_flows: bool = False
```

新增私有函数：

```python
DEMO_IDENTITY_FLOW_IDS = frozenset({"switch_user_id"})

def _filter_demo_identity_flows(flows: FlowsList, *, allow: bool) -> None:
    if allow:
        return
    for flow_id in DEMO_IDENTITY_FLOW_IDS:
        flows.remove_flow(flow_id)
```

在 `Agent.load()` 加载 flows 后调用该过滤函数。

- [ ] **Step 3：完整传递 tracker store 配置**

替换旧逻辑：

```python
tracker_store = create_tracker_store(
    tracker_store_config.type,
    path=tracker_store_config.path,
)
```

改为构造非空字典：

```python
tracker_kwargs = {
    "path": tracker_store_config.path,
    "url": tracker_store_config.url,
    "host": tracker_store_config.host,
    "port": tracker_store_config.port,
    "db": tracker_store_config.db,
    "username": tracker_store_config.username,
    "password": tracker_store_config.password,
    "key_prefix": tracker_store_config.key_prefix,
    "ttl_seconds": tracker_store_config.ttl_seconds,
}
tracker_kwargs = {key: value for key, value in tracker_kwargs.items() if value is not None}
tracker_store = create_tracker_store(tracker_store_config.type, **tracker_kwargs)
```

- [ ] **Step 4：运行测试**

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/agent/test_agent_production_flows.py tests/unit/core/test_tracker_store_factory.py -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/agent tests/unit/core tests/unit/api tests/security -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m compileall -q atguigu_ai/agent atguigu_ai/core atguigu_ai/shared tests/unit/agent tests/unit/core
git diff --check
```

- [ ] **Step 5：提交**

```powershell
git add atguigu_ai/agent/agent.py tests/unit/agent/test_agent_production_flows.py
git diff --cached --check
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "fix: disable demo identity flow in production"
```

## Task 4：真实 Redis 集成测试与技术设计更新

**文件：**

- 创建 `tests/integration/test_redis_tracker_store.py`
- 修改 `docs/TECHNICAL_DESIGN.md`

- [ ] **Step 1：写真实 Redis 集成测试**

复用 `tests/integration/test_redis_session.py` 的 owned Redis 容器 helper。

覆盖：

```python
async def test_redis_tracker_store_round_trip_against_real_redis(...)
async def test_redis_tracker_store_keys_are_prefix_scoped(...)
async def test_redis_tracker_store_delete_removes_only_one_tracker(...)
async def test_redis_tracker_store_outage_is_sanitized(...)
```

测试必须在 finally 中恢复 Redis 容器并清空 DB15。

- [ ] **Step 2：运行集成与 cleanup**

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_redis_tracker_store.py -q -s -m integration
docker exec llm-cs-redis redis-cli -n 15 DBSIZE
```

- [ ] **Step 3：更新中文技术设计**

在 `docs/TECHNICAL_DESIGN.md` 记录：

- Redis TrackerStore 已实现。
- 生产 tracker key 为 `tracker:account:{account_id}`。
- `switch_user_id` 作为 demo-only flow，生产默认过滤。
- 课程 demo 可以显式开启。

- [ ] **Step 4：提交集成**

```powershell
git add tests/integration/test_redis_tracker_store.py docs/TECHNICAL_DESIGN.md
git diff --cached --check
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "test: cover redis tracker integration"
```

## Task 5：证据、独立 QA 和最终报告

**文件：**

- 创建 `docs/reports/integration/2026-07-19-redis-tracker-production-flow.md`
- 创建 `docs/reports/integration/evidence/redis-tracker-production-flow-*.txt`
- 创建 `docs/reports/integration/evidence/redis-tracker-production-flow-independent-qa.md`
- 更新本计划 checkbox 为完成状态。

- [ ] **Step 1：保存验证证据**

运行并保存为 UTF-8 无 BOM：

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/core/test_redis_tracker_store.py tests/unit/core/test_tracker_store_factory.py tests/unit/agent/test_agent_production_flows.py -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_redis_tracker_store.py -q -s -m integration
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/agent tests/unit/core tests/unit/auth tests/unit/api tests/security -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m compileall -q atguigu_ai/agent atguigu_ai/core atguigu_ai/shared tests/unit/agent tests/unit/core tests/integration
git diff --check
docker exec llm-cs-redis redis-cli -n 15 DBSIZE
```

- [ ] **Step 2：做 scoped secret scan**

扫描本 slice 源码、测试、中文文档和 evidence：

- private key
- `sk-*`
- AWS/Slack token 形状
- credential-bearing Redis/MySQL URL
- SMTP password
- raw session/csrf cookie
- Redis tracker JSON 中不应出现真实 secret

- [ ] **Step 3：独立 QA**

复用或新建 QA agent，要求重跑 targeted unit、integration、regression、full suite、compileall、diff check、Redis cleanup、UTF-8 evidence、secret scan。必须无 Critical/Important finding。

- [ ] **Step 4：写中文报告并提交**

报告必须包含：

- 本 slice 做了什么。
- 测试结果。
- Redis cleanup 结果。
- QA 结论。
- 仍未上线的后续工程项。

提交：

```powershell
git add docs/superpowers/specs/2026-07-19-redis-tracker-production-flow-design.md docs/superpowers/plans/2026-07-19-redis-tracker-production-flow.md docs/reports/integration/2026-07-19-redis-tracker-production-flow.md docs/reports/integration/evidence/redis-tracker-production-flow-*
git diff --cached --check
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "docs: record redis tracker production flow verification"
git status --short
```

## 完成门禁

本 slice 只有在以下条件全部满足时才算完成：

- RedisTrackerStore 支持 save/retrieve/delete/keys，并通过单元和真实 Redis 集成测试。
- 工厂和 `TrackerStoreConfig` 支持 redis 完整配置。
- `Agent.load()` 默认过滤 `switch_user_id`，显式允许时保留。
- chat 使用的 `account:{account_id}` tracker key 可以通过 Redis TrackerStore 持久化。
- Redis 依赖故障脱敏。
- Redis DB15 清理为 0。
- scoped secret scan 无发现。
- 证据为 UTF-8 无 BOM。
- 独立 QA APPROVED。
- 最终工作区干净。
