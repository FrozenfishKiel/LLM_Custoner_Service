# Redis TrackerStore 与生产 Flow 安全验证报告

日期：2026-07-19
范围：Redis TrackerStore、TrackerStore 配置/工厂、生产默认禁用 `switch_user_id` demo 身份切换 Flow。

## 本 slice 完成内容

- 新增 `RedisTrackerStore`，支持 `save/retrieve/delete/keys`，使用 JSON 持久化 `DialogueStateTracker.to_dict()`。
- `create_tracker_store("redis", ...)` 与 `TrackerStoreConfig` 已接入 Redis 配置，包括 `url/host/port/db/username/password/key_prefix/ttl_seconds`。
- `ttl_seconds` 拒绝 bool、空值以外的非正整数；`key_prefix` 拒绝空字符串和 Redis glob 字符，避免跨前缀误扫。
- Redis 连接/协议/超时类错误统一映射为脱敏 `TrackerStoreException("Tracker store is unavailable")`。
- `AgentConfig` 默认 `runtime_mode="production"`、`allow_demo_identity_flows=False`，`Agent.load()` 默认过滤 `switch_user_id`；显式允许时保留课程 demo flow。
- 中文技术设计已更新，生产 chat tracker key 统一记录为 `tracker:account:{account_id}`。

## 验证结果

| 项目 | 命令 | 结果 | 证据 |
| --- | --- | --- | --- |
| 目标单测 | `pytest tests/unit/core/test_redis_tracker_store.py tests/unit/core/test_tracker_store_factory.py tests/unit/agent/test_agent_production_flows.py -q` | 15 passed | `evidence/redis-tracker-production-flow-unit-targeted.txt` |
| Redis Tracker 真实集成 | `pytest tests/integration/test_redis_tracker_store.py -q -s -m integration` | 4 passed | `evidence/redis-tracker-production-flow-integration.txt` |
| 相关回归 | `pytest tests/unit/agent tests/unit/core tests/unit/auth tests/unit/api tests/security -q` | 259 passed，33 warnings | `evidence/redis-tracker-production-flow-regression.txt` |
| email 单测补充 | `pytest tests/unit/email -q` | 12 passed | `evidence/redis-tracker-production-flow-unit-email.txt` |
| 全量测试 | `pytest tests -q` | 343 passed，33 warnings | `evidence/redis-tracker-production-flow-full-suite.txt` |
| 编译检查 | `compileall -q atguigu_ai/agent atguigu_ai/core atguigu_ai/shared tests/unit/agent tests/unit/core tests/integration` | exit 0 | `evidence/redis-tracker-production-flow-compileall.txt` |
| whitespace | `git diff --check` | exit 0 | `evidence/redis-tracker-production-flow-whitespace.txt` |
| Redis 清理 | `docker exec llm-cs-redis redis-cli -n 15 DBSIZE` | 0 | `evidence/redis-tracker-production-flow-dbsize.txt` |
| scoped secret scan | changed source/tests/docs/evidence 范围 | 未发现匹配 | `evidence/redis-tracker-production-flow-secret-scan.txt` |
| 独立 QA | agent 复验 Task 4 文档一致性与 Redis 集成测试 | APPROVED | `evidence/redis-tracker-production-flow-independent-qa.md` |

## 全量门禁状态

初次全量测试时，本地 MySQL/Docker 测试环境曾卡死：`CREATE DATABASE llm_cs_test_*` 查询停在 `checking permissions`，`docker restart llm-cs-mysql` 返回 `tried to kill container, but did not receive an exit event`。重启 Docker/环境后已恢复，复查结果如下：

- `tests/integration/test_account_migration.py::test_account_schema_upgrade_downgrade_and_repeatability`：1 passed。
- `tests/integration/test_auth_routes_http.py::test_register_has_no_cookies_and_persists_pending_account`：1 passed。
- `pytest tests -q`：343 passed，33 warnings，耗时 75.18 秒。
- Redis DB15 cleanup：0。
- MySQL 临时测试库：0。
- MySQL 测试相关卡住查询：0。

结论：Redis TrackerStore 专项验证和当前全量自动化测试均已通过；剩余上线工作进入后续功能 slice，而不是本 slice 的测试阻塞。

## 已知 warnings

相关回归中存在既有 TestClient/httpx deprecation warnings：

- `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated`
- `DeprecationWarning: Setting per-request cookies=<...> is being deprecated`

本 slice 没有扩大或修复这些 warnings；它们需要作为后续技术债处理。

## 距离上线仍剩的工作

排除必须由你提供的外部信息/部署账号/真实生产配置后，工程侧还剩：

1. 做 Action ownership / rate limiting / 审计闭环，避免已登录用户触发越权业务动作。
2. 做生产配置模板与启动检查：Redis TrackerStore、Redis Session、MySQL、CORS、cookie secure/samesite、SMTP 等必须显式配置。
3. 做端到端用户流测试：注册、验证邮箱、登录、绑定/识别业务身份、chat、reset、logout、异常 Redis/MySQL/SMTP 场景。
4. 做上线监控与量化阈值：登录成功率、chat 4xx/5xx、Redis/MySQL 延迟、会话创建/解析失败率、邮件发送失败率、异常脱敏日志抽样。

当前 slice 不再继续扩大到这些任务；它们应进入后续开发任务。
