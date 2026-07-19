# Redis TrackerStore 与生产 Flow 安全设计

日期：2026-07-19

## 背景

上一阶段已经完成 `/api/chat/messages` 和 `/api/chat/reset` 的服务端可信身份接入：路由只从 `auth_session` 和 `account_user_binding` 推导账号与业务用户，并使用 `account:{account_id}` 作为 Agent 的 tracker key。

当前仍有两个上线阻塞点：

1. TrackerStore 工厂只支持 `json`、`mysql`、`memory`，生产配置还不能使用 Redis 保存对话状态。
2. 课程 demo 的 `switch_user_id` flow 仍会从 `ecs_demo/data/flows/flow_order.yml` 加载；如果生产环境加载该 flow，用户可以通过对话切换业务 `user_id`，重新打开越权入口。

本设计只处理 Tracker 持久化和生产 Flow 安全边界，不处理 Action 级订单归属校验、限流、监控、UI 或部署。

## 目标

- 新增 Redis 版 TrackerStore，满足现有 `TrackerStore` 抽象。
- 让 `create_tracker_store("redis", ...)` 可以创建 Redis TrackerStore。
- 让 `Agent.load()` 能把 `endpoints.yml` 中的 tracker store 配置完整传给工厂，而不是只传 `path`。
- 默认生产模式不加载 `switch_user_id` flow。
- 保留本地课程 demo 和测试显式开启 `switch_user_id` 的能力。
- 用真实 Redis 集成测试证明保存、恢复、删除、keys、异常脱敏和资源清理。

## 非目标

- 不删除 `ecs_demo/data/flows/flow_order.yml` 里的 `switch_user_id` 定义。
- 不改写业务 Action 的 SQL 查询逻辑；Action 归属校验是后续独立 slice。
- 不把 Redis TrackerStore 作为订单事实来源；订单事实仍以 MySQL/Neo4j 为准。
- 不新增复杂分布式锁、消息队列或多实例部署编排。

## 方案选择

### 方案 A：直接删除 `switch_user_id`

优点是简单；缺点是破坏课程 demo 的本地教学能力，也不利于测试“生产禁用、测试允许”的边界。

### 方案 B：在 FlowLoader 里硬编码排除

优点是实现集中；缺点是所有加载场景都会受影响，测试和课程 demo 需要绕路。

### 方案 C：在 Agent 加载边界按运行模式过滤（推荐）

Agent 加载项目后，根据配置决定是否允许 demo-only flow。默认生产安全：不允许 `switch_user_id`。测试或本地 demo 可以显式配置允许。这样不改课程资产，又能把生产边界放在真正组装 Agent 的地方。

## 详细设计

### RedisTrackerStore

新增 `atguigu_ai/core/stores/redis_store.py`，类名为 `RedisTrackerStore`。

构造参数：

- `redis_client`：可选，测试可注入真实或 fake Redis 客户端。
- `url`：可选 Redis URL，未传 `redis_client` 时使用。
- `host`、`port`、`db`：未传 `url` 时组成连接。
- `key_prefix`：默认 `tracker:`。
- `ttl_seconds`：可选，正整数；为空则不设置 TTL。
- `domain`：沿用 `TrackerStore` 现有参数，用于恢复 domain slots。

行为：

- `save(tracker)`：把 `tracker.to_dict()` 序列化为 JSON，写入 `key_prefix + tracker.sender_id`。设置了 TTL 时使用 `SET key value EX ttl`。
- `retrieve(sender_id)`：读取 JSON，缺失返回 `None`；JSON 损坏或结构无法恢复时抛 `TrackerSerializationError`，不得泄露原始 Redis URL、密码或内部错误细节。
- `delete(sender_id)`：删除单个 key。
- `keys()`：扫描 `key_prefix*`，返回去掉前缀后的 sender_id 列表；支持 Redis 返回 bytes 或 str。
- Redis 连接、超时、协议错误统一映射为 `TrackerStoreConnectionError("Tracker store is unavailable")` 或 `TrackerStoreException("Tracker store is unavailable")`，异常文本不包含连接串、密码、token。

### 工厂与配置

新增常量：

- `TRACKER_STORE_REDIS = "redis"`

更新 `create_tracker_store()`：

- 支持 `redis`。
- 继续保留 `json`、`mysql`、`memory`。
- 对未知类型给出明确错误。

更新 `TrackerStoreConfig`：

- 增加 `key_prefix`、`ttl_seconds`。
- `from_dict()` 解析这两个字段。

更新 `Agent.load()`：

- 从 `endpoints_config.tracker_store` 读取完整配置。
- 调用工厂时传入 `path/url/host/port/db/username/password/key_prefix/ttl_seconds` 中非空字段。
- 这样 MySQL 和 Redis 配置都不会被只传 `path` 的旧逻辑截断。

### 生产 Flow 安全

新增 `AgentConfig` 字段：

- `runtime_mode: str = "production"`
- `allow_demo_identity_flows: bool = False`

规则：

- 默认 `runtime_mode="production"` 且 `allow_demo_identity_flows=False`。
- 当不允许 demo identity flows 时，Agent 加载完成后从 `FlowsList` 中移除 `switch_user_id`。
- 当测试或本地 demo 显式设置 `allow_demo_identity_flows=True` 时，保留该 flow。

设计理由：

- 默认安全，避免新部署忘记关 demo flow。
- 不修改课程原始 flow 文件，保留可教学、可回归的 demo 能力。
- 后续如果有更多 demo-only flow，可以扩展成列表，而不是散落在各处。

### 错误处理与安全

- Redis 依赖错误不能把 URL、密码、key、原始 tracker JSON 写入对外异常字符串。
- 测试 fixture 不写 credential-bearing URL，例如不出现 `redis://user:password@...`。
- 证据和报告保存为 UTF-8，无 BOM。

## 测试策略

### 单元测试

新增或修改：

- `tests/unit/core/test_redis_tracker_store.py`
- `tests/unit/core/test_tracker_store_factory.py`
- `tests/unit/agent/test_agent_production_flows.py`

覆盖：

- `RedisTrackerStore` 保存后可恢复 `DialogueStateTracker`。
- 缺失 key 返回 `None`。
- 删除后无法恢复。
- `keys()` 返回去前缀 sender_id。
- TTL 参数必须是正整数。
- Redis 异常脱敏。
- `create_tracker_store("redis")` 返回 `RedisTrackerStore`。
- `Agent.load()` 默认移除 `switch_user_id`。
- 显式 `allow_demo_identity_flows=True` 时保留 `switch_user_id`。

### 集成测试

新增：

- `tests/integration/test_redis_tracker_store.py`

覆盖真实 Redis：

- 保存、恢复、覆盖、删除。
- `keys()` 只返回当前前缀下的 tracker。
- Redis 重启后如果 AOF 保留则可恢复；如果环境重建为空，也要有确定行为并记录。
- Redis outage 返回脱敏异常。
- 测试结束 Redis DB15 或指定测试 DB 清理为 0。

### 回归与证据

最终保留：

- targeted unit 输出。
- Redis integration 输出。
- auth/chat regression 输出。
- full suite 输出。
- compileall、diff check、cleanup、secret scan。
- 独立 QA 记录。

## 上线影响

完成后，chat 授权的可信 tracker key 会落到 Redis TrackerStore，生产 Agent 默认不会加载 `switch_user_id`。这会继续降低用户越权风险，并让对话状态更接近生产部署拓扑。

仍未完成的上线项包括：Action 归属校验、限流、UI、监控告警、备份恢复、压测、LLM 评测和发布回滚演练。
