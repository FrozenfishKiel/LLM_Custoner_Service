# 生产启动链路与上线前最小闭环报告

日期：2026-07-20

## 本轮范围

本轮跳过“双击启动脚本”，优先完成三件上线前更关键的事：

1. 真实生产 app wiring：`create_production_app()` 可以装配 auth、rate limiter、chat deps、业务用户绑定解析器和真实 Agent 加载路径。
2. 运维最小接口：补齐 `/health/live`、`/health/ready`、`/internal/metrics`。
3. LLM 评测收口：把现有评测文件、历史 baseline、当前复跑状态和风险说明整理成中文可追踪材料。

## 已完成的代码闭环

- `atguigu_ai/api/production.py`
  - 新增 `ProductionBindingRepository`，通过已有 `AccountRepository` 查询账号和业务用户绑定。
  - 新增 `build_production_chat_deps()`，加载 `PRODUCTION_AGENT_PATH` 指向的 Agent，并构造 `BusinessIdentityResolver`。
  - `create_production_app()` 支持在 `PRODUCTION_CHAT_ENABLED=true` 或测试注入 `agent_factory` 时挂载真实 chat routes。
  - 缺失/无效 `PRODUCTION_AGENT_PATH` 会在生产 app 构建阶段失败，错误不包含 secret 值。

- `atguigu_ai/api/server.py`
  - `app.state.chat_deps` 会和 auth deps 一样注册，便于测试和运行期检查。
  - `/health/live` 返回进程存活状态。
  - `/health/ready` 返回 auth/chat/agent/rate limiter 是否完成装配。
  - `/internal/metrics` 返回最小文本指标，不输出 MySQL、Redis、SMTP、token、session 等配置值。

- `.env.example`
  - 补齐 `AUTH_PUBLIC_BASE_URL`、`PRODUCTION_CHAT_ENABLED`、`PRODUCTION_AGENT_PATH`、`PRODUCTION_ENABLE_INSPECT`。
  - 本地 Redis 示例改为 DB 15，降低误伤用户 Redis 现有数据的概率。
  - 明确 `/internal/metrics` 生产环境必须由反向代理或网络策略限制访问。

## TDD 证据

生产链路相关 unit/API 测试已经做过 RED/GREEN：

```powershell
C:\Users\frozenfish\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest tests/unit/api/test_production_dependencies.py -q
```

当前结果：

```text
8 passed, 1 warning
```

其中覆盖：

- 生产 auth deps 使用同一个 Redis 客户端装配 session、credential token 和 rate limiter；
- 缺少 `AUTH_PUBLIC_BASE_URL` 会安全失败；
- 生产 app 可注册 auth routes；
- 注入 Agent factory 时生产 app 可注册 chat deps；
- 无效 `PRODUCTION_AGENT_PATH` 会失败；
- `/health/live`、`/health/ready`、`/internal/metrics` 可访问且 metrics 不泄露测试 secret；
- `import atguigu_ai.api` 不要求数据库环境。

## LLM 评测状态

历史 evidence `docs/reports/integration/evidence/llm-chat-evaluation.txt` 显示 2026-07-19 曾跑通一次完整评测，但质量指标全为 0：

- 总用例数：14；
- 场景完成率：0.0000；
- 业务事实准确率：0.0000；
- 边界拒答率：0.0000；
- 平均完成轮数：0.00。

2026-07-20 当前环境复跑失败，原因是本机 MySQL 不可用：

```text
AssertionError: Local MySQL is unavailable for integration tests
```

所以当前结论是：评测框架已经收口为可追踪 baseline，但 fresh integration 复验被本地 MySQL 环境阻塞；不能把 LLM 客服质量宣称为上线合格。

## 上线前剩余风险

1. 真实 MySQL/Redis/SMTP 配置仍需要用户提供并验证。
2. `/internal/metrics` 当前没有应用内鉴权，本轮只做最小指标输出；生产必须用反向代理、内网 ACL 或平台网络策略限制访问。
3. LLM 业务质量 baseline 很弱，历史评测指标全为 0。上线演示可以展示“链路已接通”，不能展示成“客服质量稳定可用”。
4. 本轮没有做双击启动脚本，也没有做 Nginx/HTTPS/Kubernetes/Grafana 这类部署平台工作。

## 下一步建议

如果继续按当前路线推进，我建议顺序是：

1. 恢复本地 MySQL/Redis integration 环境，重新跑 LLM eval，拿到 fresh baseline。
2. 针对 Agent grounding、意图路由、业务 action 调用修 LLM 质量，让指标从 0 往上走。
3. 给 `/internal/metrics` 加部署层访问限制说明或应用内内部 token 保护。
4. 再回头做你提过的双击启动脚本，把启动体验补成更像应用程序。
