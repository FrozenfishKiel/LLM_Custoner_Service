# 生产启动链路、LLM 评测收口与上线运维最小闭环设计

日期：2026-07-20

## 背景

项目已经完成账号认证、Session/CSRF、生产限流、真实前端和浏览器 E2E。现在最大缺口不是再做一个演示页面，而是把“真实生产 app 能启动、能接真实客服 Agent、能被健康检查和评测门禁衡量”补齐。

当前状态：

- `create_production_app()` 只构建 auth deps，没有注入真实 `Agent`、`ChatRouteDependencies` 和业务用户绑定解析。
- `start_customer_service.ps1` 仍走课程 inspect demo，不是生产前端 + auth/chat 路径。
- LLM 评测 slice 已有未提交半成品，报告显示当前指标全为 0，但这本身是有价值的真实 baseline。
- 技术设计中写了 `/health/live`、`/health/ready`、`/internal/metrics`，代码尚未实现。

## 目标

本轮只做上线前最小闭环：

1. 真实生产 app wiring：`create_production_app()` 能加载真实 Agent、真实 auth、真实 chat 路由和业务用户绑定解析。
2. LLM 评测收口：接管现有脏文件，确保评测可跑、报告事实准确、证据 UTF-8 可读，并作为当前质量 baseline 提交。
3. 运维最小接口：实现 `/health/live`、`/health/ready`、`/internal/metrics` 的轻量版本，让启动链路具备基础可观测性。
4. 配置最小闭环：补齐 `.env.example` 和中文上线检查报告，明确生产必填项、不能提交真实密钥。

## 非目标

- 不做双击启动脚本；用户已明确本轮可以跳过。
- 不做 Kubernetes、Nginx、HTTPS 自动部署、Grafana Dashboard 或完整 Prometheus exporter。
- 不修 LLM 业务质量本身。本轮只保证“评测能真实衡量当前质量”，不把 0 分指标包装成上线合格。
- 不引入 React/Vue 或独立前端构建链。
- 不更改数据库 schema，除非现有迁移/绑定逻辑已经要求。

## 推荐方案

采用“三段式顺序实现”：

1. 先补生产 app wiring。
   - 原因：LLM 评测、浏览器真实验收和 readiness 都依赖生产 app 能把 auth/chat/Agent 组合起来。
2. 再收 LLM 评测。
   - 原因：当前已有半成品和 evidence，先让它变成干净、可提交、可解释的 baseline。
3. 最后补 health/metrics/config/report。
   - 原因：运维接口需要基于真实 app 状态汇总，而不是写静态占位。

## 生产 app 设计

新增或扩展 `atguigu_ai/api/production.py`：

- `ProductionSettings`：从环境变量读取并校验生产配置。
- `build_production_auth_deps()`：保留现有行为，继续共享 Redis 给 Session、Token 和 RateLimiter。
- `build_production_chat_deps()`：加载真实 Agent，并创建 `BusinessIdentityResolver`。
- `ProductionBindingRepository`：用 SQLAlchemy session factory 从 `account_user_binding` + `account` 查询业务用户绑定。
- `create_production_app()`：同时注入 `auth_deps` 和 `chat_deps`，前端页面保持可访问。

配置约定：

- `PRODUCTION_AGENT_PATH`：默认 `ecs_demo`。
- `PRODUCTION_ENABLE_INSPECT`：默认 false。
- `REDIS_URL`、`MYSQL_*`、`SMTP_*`、`AUTH_PUBLIC_BASE_URL` 为生产启动核心配置。

失败策略：

- 缺少必填配置：启动阶段抛 `RuntimeError`，错误只包含配置名，不包含 secret 值。
- Agent 路径不存在：启动阶段抛 `RuntimeError("PRODUCTION_AGENT_PATH is invalid")`。
- 业务绑定查询异常：chat route 已转换为脱敏 503。

## Health / Metrics 设计

新增轻量运维状态，不做完整监控平台。

接口：

- `GET /health/live`：只说明进程活着，返回 200。
- `GET /health/ready`：检查 app 是否具备生产关键依赖对象：
  - auth deps 是否存在；
  - chat deps 是否存在；
  - agent 是否存在；
  - rate limiter 是否存在；
  - readiness checker 如果配置了可选探针，则执行探针。
- `GET /internal/metrics`：返回文本格式的最小指标快照，至少包含：
  - `customer_service_info`;
  - `customer_service_auth_configured`;
  - `customer_service_chat_configured`;
  - `customer_service_agent_ready`;
  - `customer_service_rate_limiter_configured`;

安全边界：

- 本轮不做 IP allowlist 中间件，但报告中明确生产应通过反向代理或网络策略限制 `/internal/metrics`。
- metrics 不输出邮箱、账号、session、token、密码、数据库 URL 或 API Key。

## LLM 评测收口设计

接管当前未提交文件：

- `tests/integration/chat_eval_support.py`
- `tests/integration/test_llm_chat_evaluation.py`
- `docs/reports/integration/2026-07-19-llm-chat-evaluation.md`
- `docs/reports/integration/evidence/llm-chat-evaluation*.txt`
- `docs/superpowers/plans/2026-07-19-llm-chat-evaluation.md`

验收标准：

- 评测测试能顺序运行，不与 Redis DB15 并发测试互相污染。
- evidence 使用 UTF-8。
- 报告必须明确当前 baseline 很弱，不支持“客服质量已上线合格”的说法。
- 指标必须来自真实输出，不能手写美化。

## 测试策略

必须遵守 TDD：

1. 先写失败测试：生产 app 注册 chat deps，ready/metrics 路由存在，配置缺失安全失败。
2. 看见 RED 后实现。
3. 运行 GREEN：
   - `tests/unit/api/test_production_dependencies.py`
   - `tests/unit/api/test_frontend_routes.py`
   - `tests/unit/api/test_auth_routes.py`
   - `tests/unit/api/test_chat_routes.py`
   - LLM eval 相关 integration（如果本机 Docker/Redis/MySQL 可用）
4. 编码检查：
   - 中文报告和 evidence 均 UTF-8 可读。
   - `git diff --check` 通过。

## 风险

- 真实 Agent 加载会触发项目 actions/import 路径，必须避免 `import atguigu_ai.api` 时就连接数据库或读取生产 secret。
- LLM eval 当前指标为 0，产品层面不能据此声称“智能客服质量上线可用”。
- `/internal/metrics` 暂无访问控制，生产部署必须由网络层限制。
- 真实 SMTP 仍需要用户提供可用配置，本轮只做配置校验和 wiring。

## 完成定义

- `create_production_app()` 同时挂载 auth/chat/front-end。
- `/health/live`、`/health/ready`、`/internal/metrics` 有自动化测试。
- LLM eval 文件被收口为干净提交，报告中文/英文均事实准确且 UTF-8 可读。
- 中文上线前报告记录：验证命令、结果、剩余风险、上线前用户还需提供的配置。
- 提交并推送，不夹带无关文件。
