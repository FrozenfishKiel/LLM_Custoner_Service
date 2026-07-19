# 生产限流集成验证报告

日期：2026-07-19

## 本轮范围

本轮完成生产限流 slice：

- 新增 Redis fixed-window `RedisRateLimiter`。
- auth route 接入注册、登录、忘记密码、重发验证、验证邮箱、重置密码、改密码限流。
- chat route 接入消息发送和 reset 限流。
- 关键写 Action 接入 account_id + action_name 限流。
- 新增生产 auth deps/app wiring：生产入口会创建共享 Redis client，并把 `RedisRateLimiter` 注入 `AuthRouteDependencies`。
- 增加真实 Redis 并发、TTL、key 安全、错误类型风险测试。

本轮没有修改数据库 schema，没有引入第三方限流库。

## 正常用户路径

已覆盖：

- 注册、登录、邮箱验证、重发验证邮件、忘记密码、重置密码、改密码。
- chat messages 和 chat reset。
- 修改收货地址、取消订单、提交售后这三个关键写 Action。

未注入 limiter 时，auth/chat 保持课程兼容行为；注入 limiter 后，超限会在 service、Agent、Action 写入前阻断。

## 刁钻测试

已覆盖：

- 登录 `IP + email` subject 不把原始 email 交给 limiter，而是使用规范化 email 后的 SHA-256 摘要。
- 验证邮箱和重置密码只按 IP 限流，不把 token 原文作为 subject。
- chat 限流 subject 使用服务端解析出的 `account_id`，不使用客户端伪造的 metadata/account_id。
- Redis key 不包含原始 email、IP、token。
- CSRF/session 顺序保持：改密码在 CSRF/session 通过后才按 account_id 限流；chat 在 session、CSRF、business identity 后限流。

## 压力和并发测试

真实 Redis 集成测试：

```text
rate_limit_concurrency_samples=80 allowed=10 blocked=70
```

同一个 key 并发 80 次，规则 limit=10，最终只允许 10 次，阻断 70 次。

TTL 固定窗口验证：

```text
rate_limit_ttl_first=60 second=59
```

第二次请求没有刷新 TTL，证明当前实现不是滑动惩罚窗口。

## 风险测试

已覆盖：

- Redis wrong type key：映射为 `RateLimitStoreUnavailable`，错误信息脱敏。
- Redis 不可用：
  - auth/chat route 返回 `503 {"detail": "Rate limit service is unavailable"}`。
  - Action 返回 `系统繁忙，请稍后重试`。
- 超限：
  - HTTP 返回 429。
  - auth 不调用下游 service，不设置登录 cookie。
  - chat 不调用 Agent，不 reset tracker。
  - Action blocked/outage 路径在 DB import/session 前返回，不执行 DB 写。
- 清理：
  - Redis DB15 最终为 0。
  - 临时 MySQL 测试库最终为 0。

## 量化验证数据

本轮关键命令结果：

```text
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/rate_limit tests/unit/api/test_auth_routes.py tests/unit/api/test_chat_routes.py tests/unit/actions -q
85 passed, 39 warnings

D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/api/test_production_dependencies.py tests/unit/api/test_auth_routes.py tests/unit/api/test_chat_routes.py tests/unit/rate_limit -q
70 passed, 39 warnings

D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests -q -m "not integration"
325 passed, 82 deselected, 39 warnings

D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_account_migration.py -q -s -m integration
6 passed

D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_action_ownership_audit.py -q -s -m integration
5 passed

D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_auth_routes_http.py -q -s -m integration
8 passed

D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_auth_service_mysql_redis.py -q -s -m integration
11 passed

D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_chat_authorization_http.py -q -s -m integration
7 passed

D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_redis_session.py -q -s -m integration
21 passed

D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_redis_credential_tokens.py -q -s -m integration
15 passed

D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_redis_rate_limiter.py -q -s -m integration
5 passed

D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_redis_tracker_store.py -q -s -m integration
4 passed

docker exec llm-cs-redis redis-cli -n 15 DBSIZE
0

临时 MySQL 测试库数量
0

git diff --check
无输出
```

说明：一次直接运行 `pytest tests -q` 曾因之前超时后残留 pytest 进程叠加而超过 10 分钟。清理残留 pytest 进程后，非 integration 全量和 integration 单文件验证均通过。因为这些 integration 会停止/重启 Redis 或创建/删除 MySQL 测试库，最终采用串行单文件验证作为本轮上线前证据。

## 独立 Review

- Task 1 reviewer：未发现 Critical，指出 TTL/subject 覆盖不足；已补充测试并提交修正。
- Task 2 reviewer：未发现 Critical，指出 raw email subject 和 reset header 合同问题；已补充 RED 测试并提交修正。
- Task 3 reviewer：APPROVED。
- Task 5 reviewer：APPROVED。

## 剩余风险

- 生产 auth deps/app wiring 已接入 `RedisRateLimiter`；部署时仍必须提供正确的 `REDIS_URL`、MySQL、SMTP 和 `AUTH_PUBLIC_BASE_URL` 环境变量。
- `X-RateLimit-Reset` 现在由应用进程 `time.time()` 计算 Unix timestamp；多实例部署时如果系统时间漂移，header 可能轻微偏差，但阻断行为由 Redis TTL 决定。
- 本轮没有实现 Prometheus exporter，只固定了可接入指标名称。
