# 生产限流设计说明

日期：2026-07-19

## 背景

当前系统已经完成账号认证、Redis Session、邮件令牌、chat 授权、Redis Tracker、Action 归属和审计硬化。技术设计里已经预留了 Redis 限流 key 草案，例如：

- `rate:login:{ip}:{email_hash}`
- `rate:register:{ip}`
- `rate:email:{account_id}:{purpose}`
- `rate:chat:{account_id}`

但生产 HTTP route 还没有真正执行限流。上线前需要补齐对暴力登录、批量注册、邮件轰炸、chat 滥用和关键写操作高频调用的保护。

本设计只定义限流能力和实现边界；具体实现继续按 TDD 和集成验证推进。

## 目标

1. 给生产 auth/chat HTTP route 增加可测试、可配置、可观测的限流边界。
2. 限流判断必须在昂贵或有副作用的操作前执行，例如密码哈希、Redis token issue、SMTP 发送、Agent/LLM 调用、关键业务写 Action。
3. 限流状态使用 Redis 保存，并通过 Lua 或原子命令保证并发下计数正确。
4. 公共认证接口继续保持枚举安全：限流响应不能暴露邮箱是否存在、账号是否 pending/active、token 是否有效。
5. Redis 异常必须脱敏，不暴露 Redis URL、密码、key 或内部栈信息。

## 非目标

- 不在本 slice 做 CAPTCHA、WAF、设备指纹、风控评分或后台封禁系统。
- 不给 legacy `/api/messages`、WebSocket inspect/debug 接口做生产限流；它们属于课程/调试兼容入口，生产应优先使用 `/api/chat/*`。
- 不把限流计数写入 MySQL；短周期计数只放 Redis。
- 不做数据库 schema migration。
- 不直接实现 Prometheus exporter；本 slice 只固定监控事件和指标名称，后续监控 slice 接入。

## 推荐方案

采用自研小型 Redis RateLimiter。

新增独立模块，例如 `atguigu_ai/rate_limit/`，提供：

- `RedisRateLimiter`
- `RateLimitRule`
- `RateLimitDecision`
- `RateLimitExceeded`
- `RateLimitStoreUnavailable`

HTTP route 只负责选择规则和 subject，实际 Redis 计数、TTL、脱敏错误统一在 limiter 内。

不采用 route 内手写计数，因为重复逻辑多，容易遗漏 TTL 或异常处理。不优先引入第三方 FastAPI 限流库，因为现有项目已经有 Redis Lua 风格的 session/token 代码，自研小模块更容易保证 key 语义、异常语义和测试覆盖。

## 限流模型

首版使用固定窗口 fixed window。每条规则包含：

- `name`：规则名，例如 `auth.login.ip_email`。
- `limit`：窗口内允许次数。
- `window_seconds`：窗口长度。
- `subject`：限流主体，例如 IP、账号、邮箱 hash、account_id。
- `scope`：业务域，例如 `auth`、`email`、`chat`、`action`。

每次请求调用 limiter 后返回：

- `allowed`
- `limit`
- `remaining`
- `retry_after_seconds`
- `reset_after_seconds`
- `rule_name`

允许时继续执行 route；拒绝时返回 HTTP 429。

## Redis key 设计

统一前缀：

```text
rate:{scope}:{rule_name}:{subject_hash_or_id}
```

要求：

- key 中不直接存原始 email、token、message、密码、session token 或 CSRF token。
- email 使用 `normalize_email()` 后再做 SHA-256 hex digest。
- IP 默认使用 `request.client.host`，进入 key 前做 hash。
- account_id 是服务端内部 ID，首版可直接进入 key；如后续需要更强隐私，可以统一 hash。

示例：

```text
rate:auth:register.ip:{ip_hash}
rate:auth:login.ip_email:{ip_hash}:{email_hash}
rate:auth:login.ip:{ip_hash}
rate:email:resend_verification.account:{account_id}
rate:email:forgot_password.ip_email:{ip_hash}:{email_hash}
rate:chat:messages.account:{account_id}
rate:action:critical_mutation.account:{account_id}:{action_name}
```

## 默认规则

默认值用于开发和首版生产。规则偏保守，但不应影响正常人工使用。

| 场景 | 维度 | 默认窗口 | 默认次数 | 触发时机 |
| --- | --- | ---: | ---: | --- |
| 注册 | IP | 1 小时 | 5 次 | 调用 `AuthService.register` 前 |
| 登录 | IP + email hash | 15 分钟 | 5 次 | 调用 `AuthService.login` 前 |
| 登录 | IP | 15 分钟 | 30 次 | 调用 `AuthService.login` 前 |
| 忘记密码 | IP + email hash | 1 小时 | 5 次 | 调用 `AuthService.forgot_password` 前 |
| 忘记密码 | IP | 1 小时 | 20 次 | 调用 `AuthService.forgot_password` 前 |
| 重发验证邮件 | IP + email hash | 1 小时 | 5 次 | 调用 `AuthService.resend_verification` 前 |
| 邮件发送 | account_id + purpose | 1 小时 | 5 次 | 确认将要发邮件前 |
| 邮箱验证 | IP | 15 分钟 | 60 次 | 调用 `verify_email` 前 |
| 重置密码 | IP | 15 分钟 | 20 次 | 调用 `reset_password` 前 |
| 改密码 | account_id | 15 分钟 | 5 次 | CSRF 和 session 通过后、调用 service 前 |
| chat 消息 | account_id | 1 分钟 | 30 次 | 解析账号和业务绑定后、调用 Agent 前 |
| chat reset | account_id | 1 分钟 | 10 次 | 解析账号和业务绑定后、reset tracker 前 |
| 关键业务写 Action | account_id + action_name | 1 分钟 | 10 次 | 写 Action 确认执行前 |

登录需要同时检查 `IP+email` 和 `IP` 两条规则；任意一条超限即拒绝。

邮件相关接口要继续枚举安全。忘记密码和重发验证可以先执行 IP/email 维度限流；account_id 维度只在系统确认将要发邮件时执行，不改变公共响应语义。

关键业务写 Action 包括修改地址、取消订单、提交售后。首版可以在 Action 入口根据可信 `account_id` 和 `action_name` 限流；后续也可以在 Agent action dispatcher 统一接入。

## HTTP 响应

超限统一返回：

```json
{
  "detail": "Too many requests"
}
```

状态码：`429 Too Many Requests`。

响应头：

```text
Retry-After: <seconds>
X-RateLimit-Limit: <limit>
X-RateLimit-Remaining: <remaining>
X-RateLimit-Reset: <unix timestamp>
```

要求：

- 不返回 rule_name、subject、email hash、account_id、Redis key。
- 登录、忘记密码、重发验证、验证邮箱、重置密码的超限响应保持统一，不暗示账号存在与否。
- 限流失败和认证失败的优先级按“先限流、再执行业务”处理；超限时不再做密码哈希、邮件发送或 Agent 调用。

## IP 解析

首版默认使用 `request.client.host`。

生产部署在反向代理之后时，只有显式配置可信代理 CIDR 后，才允许读取 `X-Forwarded-For` 或 `X-Real-IP`。未配置可信代理时忽略这些 header，防止客户端伪造 IP 绕过限流。

建议配置：

```text
TRUSTED_PROXY_CIDRS=
RATE_LIMIT_USE_FORWARDED_HEADERS=false
```

## Redis 原子实现

`RedisRateLimiter.check(rule, subject)` 使用 Lua 脚本完成：

1. 检查 key 类型：不存在或 string 可继续；异常类型按测试约定处理。
2. `INCR` 计数。
3. 如果计数为 1，则设置 `EXPIRE window_seconds`。
4. 获取 TTL。
5. 返回 `[allowed, count, ttl]`。

并发要求：

- 同一 key 的并发请求不能突破 `limit`。
- TTL 必须只在窗口开始时设置，不能每次请求刷新，否则会变成滑动惩罚窗口。
- Redis 返回异常、协议异常、TTL 异常统一映射为 `RateLimitStoreUnavailable("Rate limit store is unavailable")`。

Redis 不可用策略：

- 对认证安全敏感接口默认 fail closed：返回 503 `Rate limit service is unavailable`，不继续执行业务。
- 对已登录 chat 接口也 fail closed，避免 Redis 故障时 LLM/Agent 被无限调用。
- 响应不得暴露 Redis 原始错误。

## 配置

新增配置对象建议：

```python
@dataclass(frozen=True)
class RateLimitSettings:
    enabled: bool
    fail_closed: bool
    rules: Mapping[str, RateLimitRule]
```

环境变量建议：

```text
RATE_LIMIT_ENABLED=true
RATE_LIMIT_FAIL_CLOSED=true
RATE_LIMIT_REGISTER_IP_LIMIT=5
RATE_LIMIT_REGISTER_IP_WINDOW_SECONDS=3600
RATE_LIMIT_LOGIN_IP_EMAIL_LIMIT=5
RATE_LIMIT_LOGIN_IP_EMAIL_WINDOW_SECONDS=900
RATE_LIMIT_LOGIN_IP_LIMIT=30
RATE_LIMIT_LOGIN_IP_WINDOW_SECONDS=900
RATE_LIMIT_EMAIL_IP_EMAIL_LIMIT=5
RATE_LIMIT_EMAIL_IP_EMAIL_WINDOW_SECONDS=3600
RATE_LIMIT_EMAIL_ACCOUNT_LIMIT=5
RATE_LIMIT_EMAIL_ACCOUNT_WINDOW_SECONDS=3600
RATE_LIMIT_CHAT_ACCOUNT_LIMIT=30
RATE_LIMIT_CHAT_ACCOUNT_WINDOW_SECONDS=60
RATE_LIMIT_CHAT_RESET_ACCOUNT_LIMIT=10
RATE_LIMIT_CHAT_RESET_ACCOUNT_WINDOW_SECONDS=60
RATE_LIMIT_ACTION_MUTATION_ACCOUNT_LIMIT=10
RATE_LIMIT_ACTION_MUTATION_ACCOUNT_WINDOW_SECONDS=60
```

开发环境可以显式关闭：

```text
RATE_LIMIT_ENABLED=false
```

关闭时必须在启动日志中标记。后续生产配置检查 slice 应校验 `APP_ENV=production` 时 `RATE_LIMIT_ENABLED=true`。

## 接入点

### Auth routes

`AuthRouteDependencies` 增加可选 `rate_limiter` 和 `client_ip_resolver`。如果未提供 limiter，则保持当前测试和课程兼容行为；生产 app wiring 必须提供。

接入顺序：

- `register`：解析 IP → 检查 `register.ip` → 调用 service。
- `login`：解析 IP、规范化 email/hash → 检查 `login.ip_email` 和 `login.ip` → 调用 service。
- `forgot-password`、`resend-verification`：解析 IP、规范化 email/hash → 检查 IP/email 维度 → 调用 service。
- `verify-email`、`reset-password`：只用 IP 维度，不基于 token 原文建 key。
- `change-password`：先 CSRF/session，再按 account_id 限流，再调用 service。

### Chat routes

接入顺序：

1. resolve session。
2. require CSRF。
3. resolve business identity。
4. 按 account_id 检查 chat 限流。
5. 解析 message payload。
6. 调用 Agent。

### Action critical mutation

首版建议在 Action 入口处接入，因为当前关键写 Action 已经统一从 `kwargs` 中拿可信 `account_id/user_id`：

- `ActionAskSetReceiveInfo`
- `ActionCancelOrder`
- `ActionApplyPostsale`

Action 限流应只约束写操作确认分支，不影响只读展示分支。

如果 limiter 不可用，Action 返回脱敏的“系统繁忙，请稍后重试”，并记录内部日志或指标，不执行数据库写操作。

## 监控和量化指标

后续监控 slice 需要接入以下指标；本 slice 先固定名称：

```text
rate_limit.check.total{rule,result}
rate_limit.blocked.total{rule}
rate_limit.store_unavailable.total{operation}
auth.route.rate_limited.total{route}
chat.route.rate_limited.total{route}
action.rate_limited.total{action_name}
```

日志要求：

- INFO/DEBUG 可记录 rule_name、route、result、retry_after。
- 不记录原始 email、token、password、message、session token、CSRF token。
- account_id 可记录；如后续认为敏感，可统一 hash。

上线前量化验收：

- 超限请求 100% 返回 429，不进入下游 service/Agent/Action。
- Redis 故障路径 100% 返回脱敏 503 或 Action 脱敏提示。
- 并发压测同一 key 时允许次数不超过 limit。
- 限流 key TTL 在窗口内单调下降，不因重复请求刷新。

## 测试策略

### 单元测试

新增 `tests/unit/rate_limit/test_redis_rate_limiter.py`：

- 第一次请求 allowed，remaining 正确，TTL 设置。
- 达到 limit 后下一次 blocked，retry_after 来自 TTL。
- Redis key 类型异常或 RedisError 映射为 `RateLimitStoreUnavailable`，不暴露原始错误。
- subject 校验覆盖空值、超长值和特殊字符。
- 不把 email/token/password/message 放进 key。

新增或扩展 `tests/unit/api/test_auth_routes.py`：

- 注册超限返回 429，`service.register` 未调用。
- 登录任一规则超限返回 429，`service.login` 未调用，cookies 不设置。
- 忘记密码和重发验证超限返回统一 429，不泄漏账号状态。
- 改密码在 session/CSRF 成功后按 account_id 限流，超限时 `service.change_password` 未调用。
- limiter 不可用返回脱敏 503。

新增或扩展 `tests/unit/api/test_chat_routes.py`：

- chat messages 超限返回 429，Agent 未调用。
- chat reset 超限返回 429，`reset_tracker` 未调用。
- limiter 不可用返回脱敏 503。
- 客户端伪造 metadata/account_id 不影响限流 subject。

新增 `tests/unit/actions/test_action_rate_limit.py`：

- 修改地址确认、取消订单、提交售后超限时不写数据库、不写成功 audit、不改变业务状态。
- 只读展示分支不受 critical mutation 限流影响。

### 集成测试

新增 `tests/integration/test_redis_rate_limiter.py`，使用 `llm-cs-redis` DB 15：

- 真实 Redis 下并发同一 key，允许次数不超过 limit。
- TTL 不刷新。
- 测试前后 DB15 清空。

扩展 HTTP 集成测试：

- auth route 真实 Redis 限流命中。
- chat route 真实 Redis 限流命中。
- 限流 key 不包含原始 email/token/message。

### 回归测试

每轮实现后至少运行：

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/rate_limit tests/unit/api/test_auth_routes.py tests/unit/api/test_chat_routes.py -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_redis_rate_limiter.py -q -s -m integration
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests -q
```

## 风险和缓解

| 风险 | 缓解 |
| --- | --- |
| Redis 故障导致生产入口全部 503 | fail closed 是安全优先选择；监控 `rate_limit.store_unavailable.total` 并告警 |
| NAT 或公司网络下多个用户共享 IP 误伤 | 登录使用 IP+email 与 IP 双维度，IP 全局限制给较高额度；chat 使用 account_id |
| 客户端伪造 X-Forwarded-For 绕过限制 | 默认忽略 forwarded header，仅可信代理配置后启用 |
| 邮件接口泄漏账号存在性 | 公共响应统一 202/200 或 429，不因账号存在性改变文案 |
| key 中泄漏敏感数据 | email/token/message/password/session/csrf 禁止入 key，测试强制扫描 |
| 限流破坏课程 legacy demo | limiter 在生产 auth/chat deps 中注入；未注入时保持旧行为，后续生产配置检查要求生产必须注入 |

## 验收标准

1. 所有限流规则有明确默认值、Redis key 形式和触发时机。
2. 所有超限路径返回 429，并且下游 service/Agent/Action 未调用。
3. Redis 不可用路径脱敏，且不继续执行昂贵或有副作用操作。
4. 登录、忘记密码、重发验证、验证邮箱、重置密码不因限流暴露账号存在性。
5. 真实 Redis 并发集成测试证明同一 key 不会超过 limit。
6. 全量测试通过，Redis DB15 和临时 MySQL 库清理为 0。
