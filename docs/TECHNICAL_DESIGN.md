# 电商智能客服系统技术方案

## 1. 文档信息

| 项目 | 内容 |
| --- | --- |
| 文档版本 | v2.0 |
| 文档状态 | 已定型，可用于编写实施计划 |
| 需求基线 | `docs/PRD.md` v2.0、`docs/REQUIREMENTS_DECISIONS.md` |
| 部署目标 | 中国大陆单机云服务器、Docker Compose、HTTPS公网访问 |

本方案是当前版本的技术实现基线。后续代码、配置、测试和部署必须与本方案一致。若发现需求缺口，先修改需求和技术方案，不得在实现中临时扩展。

## 2. 技术目标与边界

### 2.1 必须完成

- 邮箱注册、邮箱验证、密码登录和账号生命周期
- 基于Redis的服务端登录Session
- 账号与业务用户绑定
- 注册后幂等初始化独立模拟业务数据
- 登录用户与订单、物流、地址和售后数据的归属校验
- 现有Agent、Flow、Action和GraphRAG主链路生产化
- 正式用户页面和最小管理员页面
- MySQL、Redis、Neo4j、Nginx和应用容器化
- 结构化日志、Prometheus指标、Grafana面板和告警
- 自动化测试、LLM评测、压测、安全检查、备份和回滚

### 2.2 明确不做

- 多商家、多租户和商家管理
- 商品、购物车、下单和真实交易
- 支付、计费、套餐、账单和发票
- 真实商家数据同步
- 微信、企业微信、抖音等外部渠道
- 人工客服工作台和工单系统
- 微服务拆分、消息队列和Kubernetes
- 多地域、多机房和自动水平扩容

## 3. 设计原则

- MySQL是账号和业务事实的唯一持久化来源。
- Redis只保存可重建或有明确过期时间的临时状态。
- Neo4j只负责知识检索，不保存订单事实。
- 用户身份只从服务端Session解析，不信任请求体中的 `user_id` 或 `sender_id`。
- FastAPI路由只做协议适配、鉴权、校验和结果转换，不承载订单业务规则。
- Flow负责流程推进，Action负责业务查询和写入，LLM不得直接修改数据库。
- 对现有Agent进行局部改造，不重新实现一套对话框架。
- 所有完成声明必须有测试、日志、压测或演练证据。

## 4. 总体架构

```text
浏览器
  |
  | HTTPS
  v
Nginx
  |
  +------------------------------+
  |                              |
  v                              v
FastAPI应用                    静态资源
  |
  +--> Auth模块 ----------------> MySQL账号表
  |       |                      Redis Session/验证码/限流
  |       +---------------------> SMTP邮件服务
  |
  +--> Chat模块
  |       |
  |       +--> Agent / LangGraph
  |               |
  |               +--> DeepSeek API
  |               +--> Flow / Policy / Action
  |               +--> MySQL业务表
  |               +--> Neo4j GraphRAG
  |               +--> Redis TrackerStore
  |
  +--> Admin模块
  |       +--> 账号状态、模拟数据重置、指标和审计
  |
  +--> Prometheus指标
          |
          v
       Grafana
```

## 5. 部署拓扑

Docker Compose固定包含以下容器：

| 容器 | 职责 | 是否公网开放 |
| --- | --- | --- |
| `nginx` | HTTPS、静态资源、反向代理和请求限制 | 仅80/443 |
| `app` | FastAPI、Agent和后台任务 | 否 |
| `mysql` | 账号和业务事实 | 否 |
| `redis` | Session、验证码、限流和Tracker | 否 |
| `neo4j` | GraphRAG知识检索 | 否 |
| `prometheus` | 指标采集和保留 | 否 |
| `grafana` | 管理员监控面板 | 仅管理入口 |

初始服务器建议为4核16GB内存，系统盘和数据盘分离。最终规格根据压测和实际资源监控调整，不把建议规格写成性能成果。

应用保持单实例和单Agent进程，避免重复加载本地嵌入模型造成内存浪费。FastAPI使用异步I/O处理20个并发客服会话。

## 6. 模块设计

### 6.1 Auth模块

新增 `atguigu_ai/auth/`，作为账号和登录的独立深模块。

对外接口只暴露以下能力：

```python
register(email, password) -> RegistrationResult
verify_email(token) -> AccountIdentity
login(email, password, client_info) -> Session
logout(session_id) -> None
change_password(account_id, old_password, new_password) -> None
request_password_reset(email) -> None
reset_password(token, new_password) -> None
delete_account(account_id, password) -> None
resolve_session(session_id) -> AccountIdentity | None
```

模块内部负责：

- 邮箱规范化和唯一性检查
- 密码强度校验和哈希
- 邮箱验证凭证
- 密码重置凭证
- Session创建、续期和撤销
- 登录失败计数和限流
- 账号状态检查
- 安全日志

路由和业务Action不得自行实现密码、Token或Session逻辑。

Session由深模块 `RedisSessionStore` 管理，对外interface仅包含 `create(identity)`、`resolve(token)`、`revoke(token)` 和 `revoke_all(account_id)`。模块生成至少256位随机token，只向浏览器返回原token；Redis key和日志只使用token的SHA-256摘要。Session模块不查询MySQL，调用者只能传入已经由服务端账号模块解析的 `AccountIdentity`。

### 6.2 Email模块

新增 `atguigu_ai/email/`，通过单一接口发送验证和重置邮件：

```python
send_verification_email(recipient, verification_url) -> None
send_password_reset_email(recipient, reset_url) -> None
```

实现使用标准SMTP，配置由环境变量注入。测试使用内存Fake Adapter，不发送真实邮件。

邮件发送失败时：

- 注册账号保持“待验证”状态。
- 接口返回通用提示，不泄露内部SMTP错误。
- 用户可以在限流范围内重新发送验证邮件。
- 详细错误写入结构化日志和指标。

### 6.3 Demo Data模块

新增 `ecs_demo/demo_data/`，负责为注册用户初始化和重置模拟业务数据。

接口：

```python
initialize_for_user(user_id, seed_version) -> InitializationResult
reset_for_user(user_id, requested_by) -> ResetResult
delete_for_user(user_id) -> None
```

约束：

- 使用固定模板，不随机生成无法复现的数据。
- 初始化覆盖待发货、已发货、已签收和售后等代表场景。
- 初始化在MySQL事务内完成。
- 使用 `seed_version` 和唯一约束保证幂等。
- 重置仅由管理员执行，必须先删除该用户旧模拟数据，再按当前模板重建。
- 删除账号时删除该用户全部模拟业务数据。

### 6.4 Identity Seam

新增统一身份接口，负责把登录账号映射为Agent和Action使用的业务用户：

```python
resolve_business_user(account_id) -> BusinessUserIdentity
```

`BusinessUserIdentity`至少包含：

```text
account_id
user_id
role
account_status
```

FastAPI在调用Agent前解析身份，并将可信 `user_id` 写入受控metadata。Agent在创建或恢复Tracker时强制设置该 `user_id`，忽略模型或用户消息尝试修改身份的命令。

原有 `switch_user_id` Flow不得在生产用户域加载，只允许测试环境使用。

### 6.5 Chat模块

Chat模块负责将经过认证的用户消息交给现有Agent：

```python
handle_authenticated_message(identity, message, metadata) -> ChatResponse
reset_conversation(identity) -> None
```

处理顺序：

1. 校验登录和账号状态。
2. 检查消息长度和限流。
3. 从身份模块取得可信 `user_id`。
4. 使用 `account_id` 生成不可碰撞的Tracker键。
5. 调用 `Agent.handle_message`。
6. Action再次校验订单归属。
7. 记录Flow、Action、耗时、Token和结果。
8. 返回结构化消息。

### 6.6 Admin模块

新增最小管理员模块，不建设商家后台。

对外能力：

```text
查询账号列表和状态
禁用或启用账号
撤销指定账号全部Session
重置指定账号模拟数据
查看审计记录
查看Grafana监控链接或摘要指标
```

管理员账号通过部署初始化命令创建，不开放管理员自助注册。所有管理员操作必须记录审计事件。

## 7. 数据设计

### 7.1 MySQL新增表

#### `account`

| 字段 | 说明 |
| --- | --- |
| `account_id` | UUID主键 |
| `email` | 原始邮箱 |
| `email_normalized` | 唯一索引，登录和查重使用 |
| `password_hash` | 安全密码哈希 |
| `role` | `consumer`或`admin` |
| `status` | `pending`、`active`、`disabled` |
| `email_verified_at` | 邮箱验证时间 |
| `created_at` | 创建时间 |
| `updated_at` | 更新时间 |

账号注销执行物理删除，不保留可恢复的软删除账号记录。

#### `account_user_binding`

| 字段 | 说明 |
| --- | --- |
| `account_id` | 账号ID，唯一外键 |
| `user_id` | 现有 `user_info.user_id`，唯一外键 |
| `seed_version` | 模拟数据模板版本 |
| `initialized_at` | 初始化完成时间 |

该表确保一个账号只绑定一个业务用户，一个业务用户只属于一个账号。

`AuthBase` 只管理账号上下文的三张新表，不接管课程业务模型的SQLAlchemy metadata。运行时ORM将 `user_id` 作为受约束的标量字段；指向现有 `user_info.user_id` 的物理外键由Alembic迁移显式创建，并通过真实MySQL集成测试验证。这样避免账号模块在导入时反射或复制现有业务表，也避免未解析的跨metadata外键阻断DDL排序。

#### `audit_event`

| 字段 | 说明 |
| --- | --- |
| `event_id` | UUID主键 |
| `request_id` | 请求关联标识 |
| `actor_account_id` | 操作者账号，可空 |
| `actor_role` | 操作者角色 |
| `event_type` | 登录、禁用、重置、取消订单、售后等 |
| `target_type` | 操作对象类型 |
| `target_id` | 操作对象标识 |
| `result` | 成功或失败 |
| `metadata_json` | 脱敏后的附加信息 |
| `created_at` | 发生时间 |

账号注销后，审计记录中的账号标识替换为不可逆匿名标识，不保留邮箱和个人资料。

审计事件属于安全与业务操作证据，不等同于运行日志。注销后可以保留逐条匿名审计事件，但必须为该次注销生成不保存映射关系的随机匿名引用 `anon:<32位随机十六进制>`，并用它替换相关事件的 `actor_account_id`。凡 `target_id` 指向该账号、业务用户、订单、地址、物流或售后对象，必须置为 `NULL`；`target_type` 可以保留对象类别。`metadata_json` 只允许保留结果、错误类型、Flow和Action等非识别字段，必须删除邮箱、地址、订单号、物流单号和自由文本。管理员操作中以被注销账号或其业务对象为目标的事件同样执行 `target_id` 清空。HTTP、模型和运行日志仍只保留不可反查个人身份的聚合数据。

### 7.2 现有业务表改造

- 保留 `user_info`、`order_info`、`receive_info`、物流和售后等现有表。
- 所有订单、地址、物流和售后查询必须通过当前 `user_id` 过滤。
- 修改地址、取消订单和创建售后时必须同时使用业务对象ID和当前 `user_id` 查询。
- 增加实现幂等所需的唯一约束或幂等键。
- 数据库迁移使用版本化迁移工具，不通过运行时代码临时建表。

首个账号基线迁移只新增 `account`、`account_user_binding` 和 `audit_event`，不修改现有课程业务表的外键删除规则，也不重写已有数据。账号注销由应用服务在单个事务中按依赖顺序显式删除该业务用户的数据、删除绑定和账号，并匿名化需要保留的审计事件；后续只有实际验证需要时才单独评审外键级联改造。

版本化迁移使用 Alembic。迁移连接复用环境变量构造的 SQLAlchemy URL，配置文件、迁移脚本和命令输出不得包含数据库密码。每次执行DDL前必须显式设置 `MIGRATION_EXPECTED_TARGET=host:port/database`，迁移环境将其与实际非敏感目标严格比较，不匹配或缺失时拒绝执行。MySQL DDL可能发生部分提交，因此迁移前检查目标表不存在；失败后先检查Alembic版本和三个目标表，只允许在确认目标表为空时按依赖逆序清理本次新表并重新执行，禁止自动触碰现有业务表。

### 7.3 Redis键设计

```text
auth:session:{token_hash}                 -> account identity、generation和时间，TTL 7天
auth:account_sessions:{account_id}        -> 当前token_hash集合，TTL随Session延长
auth:session_generation:{account_id}      -> 128位随机撤销generation，不设置TTL
auth:verify_email:{token_hash}            -> account_id，TTL 30分钟
auth:reset_password:{token_hash}          -> account_id，TTL 30分钟
rate:login:{ip}:{email_hash}              -> 登录计数
rate:register:{ip}                        -> 注册计数
rate:email:{account_id}:{purpose}         -> 邮件发送计数
rate:chat:{account_id}                    -> 客服调用计数
tracker:{account_id}                      -> 序列化DialogueStateTracker
```

Redis启用AOF持久化。Session、验证码和限流数据允许按TTL自然过期；订单事实不得只存在Redis。

Session的创建、解析、单个撤销和账号全量撤销使用短Lua脚本原子执行。账号generation使用不保存映射的128位随机值：首次创建Session时若generation不存在则原子初始化；`revoke_all`以新随机值覆盖generation并删除账号Session索引，执行时间不随Session数量增长。旧Session键等待TTL回收，但因generation不匹配立即失效；generation缺失时 `resolve` 必须fail closed并删除当前Session，绝不能回退为固定初始值。`resolve`在同一脚本中比较generation，只在剩余TTL低于配置阈值时滑动续期。该设计保证并发 `create/revoke_all` 和 `resolve/revoke_all` 存在明确线性化顺序，不使用分布式锁。

#### Redis Session adapter contract

`atguigu_ai.auth.RedisSessionStore` 负责不透明Session的创建、解析、单个撤销和账号全量撤销。客户端只接收随机原始token，Redis key只包含其SHA-256摘要；四个操作分别在单个Lua脚本内原子执行。账号全量撤销写入新的128位随机generation并只删除账号索引，因此保持O(1)；旧Session hash因generation不匹配立即失效并等待TTL清理，generation缺失同样视为无效。

格式错误的客户端token不得访问Redis。损坏的Session hash必须删除并按未认证处理。Redis连接、超时和脚本错误统一抛出 `SessionStoreUnavailable("Session store is unavailable")`；将其映射为HTTP 503属于后续认证路由阶段，当前模块不得回退为内存Session或伪装成401。

当前版本只支持单机standalone Redis，不宣称Redis Cluster兼容，因为解析脚本会在读取Session hash后派生账号generation和索引key。生产Redis必须启用AOF和专用持久化卷，使用 `maxmemory-policy=noeviction`；即使generation因故丢失，解析逻辑仍然fail closed，不允许旧Session恢复有效。

## 8. 认证与安全设计

### 8.1 密码

- 使用Argon2id或当前Python安全库推荐的等价算法。
- 密码最少8个字符，限制异常超长输入。
- 登录失败返回统一错误，不区分邮箱不存在和密码错误。
- 修改或重置密码后撤销该账号全部既有Session。

### 8.2 Session

- 服务端生成至少256位随机Session ID。
- 浏览器只保存Session Cookie，不保存身份和权限声明。
- Cookie设置 `HttpOnly`、`Secure` 和 `SameSite=Lax`。
- Session默认7天过期，用户活动时按配置续期。
- 禁用、修改密码、重置密码和注销账号时撤销全部Session。
- Redis连接、超时或脚本错误统一视为Session存储不可用，受保护操作fail closed；后续HTTP路由映射为503，不回退到内存Session，也不将依赖故障伪装成未登录401。

本版本不使用前端持久化JWT，避免Token撤销和权限变更复杂度。

### 8.3 CSRF、CORS和输入校验

- 页面和接口采用同源部署。
- 所有状态修改接口验证CSRF Token或使用同等的双重防护。
- CORS仅允许正式域名和明确的本地开发地址。
- 限制请求体、消息、邮箱和密码长度。
- 按接口设置IP、账号和邮箱维度限流。

### 8.4 业务越权防护

Action禁止仅按 `order_id` 查询后修改，必须使用：

```text
order_id + current_user_id
```

物流、地址和售后对象同样执行归属校验。模型输出的 `user_id`、前端传入的 `user_id` 和对话中的“切换用户”请求均不得覆盖登录身份。

## 9. HTTP接口

### 9.1 公开认证接口

```text
POST /api/auth/register
POST /api/auth/verify-email
POST /api/auth/resend-verification
POST /api/auth/login
POST /api/auth/forgot-password
POST /api/auth/reset-password
```

### 9.2 登录用户接口

```text
POST   /api/auth/logout
POST   /api/auth/change-password
GET    /api/account/me
DELETE /api/account/me
POST   /api/chat/messages
POST   /api/chat/reset
```

`/api/chat/messages`请求中不包含 `sender` 和 `user_id`：

```json
{
  "message": "我的订单到哪里了？",
  "metadata": {}
}
```

### 9.3 管理员接口

```text
GET  /api/admin/accounts
POST /api/admin/accounts/{account_id}/disable
POST /api/admin/accounts/{account_id}/enable
POST /api/admin/accounts/{account_id}/reset-demo-data
GET  /api/admin/audit-events
GET  /api/admin/metrics/summary
```

### 9.4 健康与内部接口

```text
GET /health/live
GET /health/ready
GET /internal/metrics
```

`/internal/metrics`只对Prometheus开放。`/inspect`、`/docs`、`/redoc`、Tracker详情、Domain和Flow配置只允许本机或管理网络访问。

## 10. 前端方案

本版本使用FastAPI模板、原生HTML/CSS/JavaScript，不建立独立React或Vue工程。原因是产品界面规模有限，重点是智能客服、账号安全和生产运行，独立SPA会扩大构建和部署边界。

必须提供以下页面：

```text
/register            注册
/verify-email        邮箱验证结果
/login               登录
/forgot-password     忘记密码
/reset-password      重置密码
/chat                客服主界面
/account             账号设置
/admin               最小管理员页面
/privacy             隐私政策
/terms               用户协议
```

前端不保存模型密钥、数据库凭证或用户权限，不实现订单业务规则。所有页面必须处理加载、空状态、失败、登录过期和重复提交。

## 11. 对现有Agent的改造

### 11.1 保留

- `Agent`主类
- LangGraph处理图
- `LLMCommandGenerator`
- `CommandProcessor`
- `FlowPolicy`
- `EnterpriseSearchPolicy`
- 订单、物流和售后Flow
- 订单、物流和售后Action
- MySQL模拟业务表
- Neo4j/GraphRAG

### 11.2 必须修改

- TrackerStore新增Redis Adapter。
- Agent接收服务端可信业务身份。
- 生产配置不加载 `switch_user_id` Flow。
- Action查询和写入增加用户归属条件。
- Action写操作增加幂等和完整事务。
- EnterpriseSearchPolicy的非业务回复限定为电商客服身份。
- LLM、数据库和Neo4j异常转换为稳定错误类型。
- 日志不记录完整消息中的敏感数据。

### 11.3 禁止改造

- 不重写LangGraph或另建一套Agent框架。
- 不把订单规则移到Prompt。
- 不让LLM生成或执行任意SQL/Cypher修改业务数据。
- 不以“降级”为由把客服角色改成通用助手。
- 不引入新的业务Flow超出PRD范围。

## 12. 配置与密钥

仓库只提交 `.env.example`，实际密钥不进入Git和镜像。

主要配置：

```text
APP_ENV
APP_SECRET_KEY
PUBLIC_BASE_URL
ALLOWED_ORIGINS

MYSQL_HOST
MYSQL_PORT
MYSQL_DATABASE
MYSQL_USER
MYSQL_PASSWORD

REDIS_URL
NEO4J_URI
NEO4J_USER
NEO4J_PASSWORD

DEEPSEEK_API_KEY
DEEPSEEK_API_BASE
DEEPSEEK_MODEL
LLM_TIMEOUT_SECONDS
LLM_MAX_RETRIES

SMTP_HOST
SMTP_PORT
SMTP_USERNAME
SMTP_PASSWORD
SMTP_FROM_ADDRESS
SMTP_USE_TLS
```

之前暴露过的模型Key必须在生产部署前撤销并重新生成。

## 13. 日志、指标和告警

### 13.1 结构化日志

每次请求至少记录：

```text
request_id
account_id_hash
role
route
flow_name
action_name
status_code
latency_ms
error_type
model_name
token_usage
timestamp
```

禁止记录密码、Session ID、验证码、重置Token、模型Key和完整敏感地址。

### 13.2 Prometheus指标

- 注册、验证和登录成功/失败计数
- 在线Session和并发客服请求
- HTTP请求量、状态码和P50/P95延迟
- Flow触发、完成和失败计数
- Action成功、失败和回滚计数
- 兜底回答和越界回答计数
- DeepSeek调用次数、延迟、Token和错误
- MySQL、Redis和Neo4j连接状态
- 邮件发送成功和失败计数

### 13.3 Grafana面板

至少提供：

- 系统健康总览
- 客服业务完成情况
- 模型调用质量和成本
- 账号注册与登录漏斗
- 错误和依赖异常

### 13.4 告警

- 服务或依赖不可用
- 5xx错误率连续超阈值
- P95延迟连续超阈值
- 模型错误率或成本异常
- 磁盘空间不足
- 备份失败
- 邮件连续发送失败

## 14. 测试与验证

### 14.1 单元测试

- 邮箱规范化和密码规则
- 密码哈希和验证
- Session创建、续期和撤销
- 验证和重置Token的过期及单次使用
- 模拟数据初始化幂等
- Flow槽位和意图切换
- Action状态、归属和幂等判断
- Tracker序列化

### 14.2 集成测试

- MySQL账号与业务用户绑定
- Redis Session、Token、限流和Tracker
- SMTP Fake Adapter
- 注册到模拟数据初始化完整事务
- 五类Action真实数据库读写
- Neo4j检索成功与失败

### 14.3 端到端测试

使用浏览器自动化覆盖：

- 注册、验证、登录和退出
- 忘记密码和重置密码
- 未登录访问拦截
- 两个账号间数据隔离
- 订单查询、物流、地址、取消和售后流程
- 重复提交和确认
- 管理员禁用账号和重置模拟数据
- 注销账号和数据删除

### 14.4 LLM评测

评测集必须版本化，至少包含：

```text
case_id
user_input
conversation_history
expected_intent
expected_slots
expected_flow
expected_action
expected_final_state
```

Prompt、模型、Flow、策略或命令解析修改后必须重新执行评测。

### 14.5 压测与故障演练

- 验证20个并发客服会话。
- 验证注册、登录和非模型接口P95目标。
- 注入DeepSeek超时和限流。
- 注入MySQL、Redis和Neo4j短暂不可用。
- 验证Redis重启后订单事实不丢失。
- 验证应用镜像回滚、MySQL恢复和Neo4j恢复。

## 15. 数据备份与恢复

- MySQL每日逻辑备份，保留周期由部署配置确定，至少保留7天。
- Neo4j定期一致性备份或受控停机快照，至少保留最近可恢复版本。
- Redis启用AOF，Session和Tracker允许在严重故障时失效，但不得影响MySQL业务事实。
- 备份文件加密存储，不与应用数据卷放在同一故障域。
- 每次正式发布前备份MySQL并记录恢复点。
- 上线验收前必须实际完成一次恢复演练。

## 16. CI/CD与发布

```text
代码提交
  -> 静态检查
  -> 单元测试
  -> 集成测试
  -> LLM离线评测
  -> 构建不可变镜像
  -> 依赖和镜像安全扫描
  -> 部署预发布
  -> 端到端测试
  -> 备份生产数据
  -> 生产发布
  -> 健康检查和冒烟测试
  -> 异常则回滚上一镜像
```

生产发布不得直接在服务器修改源代码。镜像、配置版本、数据库迁移版本和发布时间必须可追踪。

## 17. 计划文件改动范围

### 17.1 新增

```text
atguigu_ai/auth/
atguigu_ai/email/
atguigu_ai/api/routes/auth.py
atguigu_ai/api/routes/chat.py
atguigu_ai/api/routes/account.py
atguigu_ai/api/routes/admin.py
atguigu_ai/api/dependencies.py
atguigu_ai/core/stores/redis_store.py
ecs_demo/demo_data/
ecs_demo/migrations/
atguigu_ai/api/templates/
atguigu_ai/api/static/
tests/unit/
tests/integration/
tests/e2e/
tests/evaluation/
Dockerfile
docker-compose.yml
.dockerignore
.env.example
nginx/
prometheus/
grafana/
scripts/backup/
scripts/restore/
scripts/deploy/
```

### 17.2 修改

```text
atguigu_ai/api/server.py
atguigu_ai/agent/agent.py
atguigu_ai/core/stores/__init__.py
atguigu_ai/shared/config.py
atguigu_ai/policies/enterprise_search_policy.py
ecs_demo/config.yml
ecs_demo/endpoints.yml
ecs_demo/domain/domain_order.yml
ecs_demo/domain/domain_patterns.yml
ecs_demo/data/flows/flow_order.yml
ecs_demo/actions/db.py
ecs_demo/actions/action_order.py
ecs_demo/actions/action_logistics.py
ecs_demo/actions/action_postsale.py
ecs_demo/gen_data.py
requirements-atguigu.txt
```

不在上述范围中的现有核心模块原则上不修改。若实施时必须增加文件，先说明它对应的PRD需求和现有范围为何不能承载，再更新本方案。

## 18. PRD追踪矩阵

| PRD需求 | 技术落点 | 验证证据 |
| --- | --- | --- |
| A-01至A-10 | Auth模块、Email模块、Redis Session | 认证单元/集成/E2E测试 |
| D-01至D-06 | Demo Data模块、账号绑定表、MySQL事务 | 初始化和数据隔离测试 |
| U-01至U-08 | 模板、静态资源、Chat和Account路由 | 浏览器E2E截图和测试 |
| C-01至C-07 | Agent、Generator、FlowPolicy、评测集 | LLM评测报告 |
| B-01至B-10 | Action、归属校验、事务、幂等、审计 | MySQL集成和故障测试 |
| M-01至M-06 | Admin模块、RBAC、Grafana和审计 | 管理员E2E和权限测试 |
| 安全要求 | Session、CSRF、CORS、限流、Nginx | 安全测试和配置检查 |
| 性能目标 | 异步应用、Redis、压测脚本 | 压测报告 |
| 可恢复性 | Docker卷、备份和回滚脚本 | 恢复演练报告 |
| 量化指标 | Prometheus、Grafana、结构化日志 | 指标面板和运行报告 |

## 19. 完成定义

技术实现只有同时满足以下条件才算完成：

- PRD全部需求在追踪矩阵中有明确实现和验证证据。
- 注册账号登录后可以直接使用独立模拟数据完成五类客服流程。
- 用户身份、数据归属、管理员权限和Session撤销经过自动化测试。
- 关键写操作具备确认、事务、幂等、归属校验和审计。
- MySQL、Redis、Neo4j、SMTP和DeepSeek异常均有确定行为。
- 生产环境不暴露内部接口、密钥和敏感日志。
- 20并发目标完成压测，真实结果被记录。
- LLM评测、端到端测试、安全检查、备份恢复和发布回滚完成。
- Prometheus、Grafana和告警能够提供量化与故障证据。
- 域名、HTTPS、备案和隐私文档满足实际公网部署要求。
- 明确排除的商城、支付、多商家、外部渠道和人工客服功能未进入代码。

完成定义不得通过删减测试、修改统计口径或将未完成内容标记为后续阶段来规避。
