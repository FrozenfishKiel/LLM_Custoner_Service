# 完整业务测试指标清单

本清单用于约束智能客服系统的每次开发验收。任何涉及用户体验、账号、聊天、LLM、Flow、Action、数据库、前端、部署或监控的改动，都不能只用“写一段测一段”的局部测试作为完成依据；必须按影响范围执行完整业务闭环验证，并记录量化结果。

## 1. 完整业务闭环定义

一次完整业务测试必须尽量模拟真实用户，而不是只调用内部函数。标准链路如下：

```text
用户打开页面或调用正式 API
  -> 注册 / 邮箱验证 / 登录 / 会话 Cookie / CSRF
  -> 进入客服聊天
  -> 发送真实自然语言
  -> LLM 识别意图和槽位
  -> Flow 推进
  -> Action 查询或写入业务数据库
  -> 返回用户能理解的结果
  -> 校验数据库最终状态、审计记录、权限隔离和用户可感知完成度
```

局部单元测试可以证明一个函数正确，但不能代替以上链路。

## 2. 必测业务路径

### 2.1 账号与授权

- 注册、邮箱验证、登录、退出、忘记密码、重置密码、修改密码。
- 注册/验证后，独立模拟业务数据初始化必须正确且幂等。
- 未登录访问聊天接口应被拒绝。
- CSRF 缺失或错误应被拒绝。
- 禁用账号、错误密码、重复注册、无效 token 的失败路径。
- 账号只能绑定和访问自己的业务用户数据。
- 管理员权限与普通用户权限隔离。
- 账号注销、关联业务数据删除或匿名化、审计保留策略需要端到端验证。

### 2.2 五类客服主业务

每类业务都要同时验证“回复文本”和“数据库事实”：

- 订单查询：能返回目标订单号、状态、订单明细和收货信息。
- 物流查询：能返回目标订单号和真实物流轨迹片段。
- 修改收货信息：能修改地址/姓名/电话，订单最终 `receive_id` 或收货字段正确，回复明确“成功”。
- 取消订单：能取消可取消订单，重复取消幂等，订单状态正确，回复明确“成功”。
- 售后申请：能提交退货退款/换货，售后记录正确，订单状态正确，重复提交幂等，回复明确“成功”。

### 2.3 LLM、槽位和 Flow

- 真实 LLM 评测必须覆盖意图识别、槽位提取、订单号保留、Flow 启动、Flow 中断/继续。
- LLM 评测除最终业务结果外，应逐步补充 `expected_intent`、`expected_slots`、`expected_flow`、`expected_action`，方便定位错误层级。
- 首句包含订单号时，不能要求用户重复选择订单，除非业务上必须二次确认。
- 用户按按钮和用户自然语言输入都要覆盖。
- Flow/action 槽位名必须一致，例如 action 读 `order_id`，flow 就不能只收集 `postsale_order_id`。
- Flow 分支必须跳转到 executor 能识别的具名 step，不得依赖当前引擎不支持的匿名嵌套步骤。

### 2.4 边界、刁钻输入与风险

- 明显非客服请求：天气、编程、金融投资、闲聊、笑话等，应稳定拒答并引导到订单/物流/售后。
- 注入类输入：SQL/Cypher/prompt injection/越权 order_id/伪造 user_id。
- 异常格式：空消息、超长消息、乱码、多轮上下文混淆、同一句包含多个意图。
- LLM 超时、限流、服务异常时不能编造业务事实，必须返回可理解的降级提示。
- 重复提交：取消订单、修改地址、售后申请必须幂等。
- 事务失败：中途异常不能留下半写入状态。
- 敏感信息：日志、响应、报告和测试输出不得打印密钥、Cookie、Session、CSRF、数据库密码。

### 2.5 前端与用户体验

- 正式前端能启动并打开。
- 注册/登录/聊天主路径可用。
- 订单/物流/改地址/取消/售后在浏览器里可以完成。
- 浏览器 E2E 不能长期只使用 fake agent；上线候选必须至少跑一次真实 LLM + 真实业务 DB 的五类业务浏览器闭环，或明确记录当前缺口。
- 按钮可点击，输入框可用，错误提示可理解。
- 压力或连续消息下页面不崩、不重复提交、不丢会话。

### 2.6 压力、稳定性与监控

- 至少覆盖连续消息、20 个并发客服会话、限流触发。
- MySQL/Redis/LLM 短暂失败时有可理解错误，不泄露内部栈或密钥。
- 关键指标应有来源：请求数、错误率、P95 延迟、LLM 调用次数/失败率、Flow 完成率、写操作成功率。
- 上线前要有 `/health/live`、`/health/ready`、Prometheus 指标、Grafana 面板或导出配置、告警规则和连续运行报告。
- 告警至少覆盖：服务不可用、依赖异常、P95 超阈值、5xx 超阈值、模型错误率/成本异常、备份失败。
- MySQL/Neo4j 备份恢复、发布回滚演练必须有证据；不能用“理论上可恢复”替代。

## 3. 核心量化指标

每次完整业务验收至少记录：

| 指标 | 含义 | 最低要求 |
| --- | --- | --- |
| `total_cases` | 评测总用例数 | 不得少于当前基线 |
| `scenario_completion_rate` | 核心业务完成率 | 开发阶段不得下降；上线候选应为 1.0 |
| `business_fact_accuracy` | 数据库事实正确率 | 写操作上线候选应为 1.0 |
| `boundary_refusal_rate` | 越界拒答率 | 上线候选应为 1.0 |
| `average_turns_to_completion` | 完成业务平均轮数 | 记录趋势，异常升高需解释 |
| 意图识别准确率 | LLM 输出是否选对业务意图 | 逐步补充显式评分 |
| 槽位提取准确率 | 订单号、售后类型、地址等槽位是否正确 | 逐步补充显式评分 |
| Flow/Action 命中率 | 是否进入预期 flow/action | 逐步补充显式评分 |
| 写操作幂等通过率 | 重复取消/重复售后等 | 1.0 |
| 越权拦截通过率 | 用户 A 不能访问用户 B 数据 | 1.0 |
| E2E 通过率 | 浏览器真实路径 | 上线候选应全部通过 |
| 压测错误率 | 并发/连续消息错误比例 | 目标值必须来自实测 |
| P95 延迟 | API/LLM/页面响应延迟 | 目标值必须来自实测 |

不得把目标值、模拟值或单次局部成功写成项目成果。

## 4. 推荐验证命令

根据改动范围选择，但涉及聊天/业务/LLM 时，至少应运行前四组：

```powershell
# 生产 flow、LLM 边界、策略与基础契约
.\.venv\Scripts\python.exe -m pytest tests/unit/agent/test_agent_production_flows.py tests/unit/dialogue_understanding/test_customer_service_boundary_guard.py tests/unit/policies/test_customer_service_boundary_response.py tests/unit/flows/test_production_flow_contracts.py -q

# 账号、授权、API 依赖和聊天路由
.\.venv\Scripts\python.exe -m pytest tests/unit/api/test_chat_routes.py tests/unit/api/test_production_dependencies.py -q

# 注册、验证、登录、改密、退出等真实 HTTP 链路
.\.venv\Scripts\python.exe -m pytest tests/integration/test_auth_routes_http.py -q -s -m integration

# 登录后聊天 API、业务用户绑定和授权隔离
.\.venv\Scripts\python.exe -m pytest tests/integration/test_chat_authorization_http.py -q -s -m integration

# 真实 MySQL 写操作归属、幂等、审计
.\.venv\Scripts\python.exe -m pytest tests/integration/test_action_ownership_audit.py -q

# 真实 LLM + Chat API + Flow + Action + MySQL 的完整评测
.\.venv\Scripts\python.exe -m pytest tests/integration/test_llm_chat_evaluation.py -q -s -m integration

# 浏览器前端 E2E，若当前前端/Playwright 环境可用则必须运行
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_customer_frontend_browser.py -q

# 编译与补丁格式
.\.venv\Scripts\python.exe -m compileall atguigu_ai ecs_demo tests
git diff --check
```

当前最小完整验收链建议按顺序运行，避免多个测试同时使用 Redis DB 15 互相污染：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_auth_routes_http.py -q -s -m integration
.\.venv\Scripts\python.exe -m pytest tests/integration/test_chat_authorization_http.py -q -s -m integration
.\.venv\Scripts\python.exe -m pytest tests/integration/test_action_ownership_audit.py -q -s -m integration
.\.venv\Scripts\python.exe -m pytest tests/integration/test_llm_chat_evaluation.py -q -s -m integration
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_customer_frontend_browser.py -q -s -m e2e
```

如果某条命令因为环境缺失无法运行，报告必须写清楚：

- 缺什么依赖；
- 为什么当前不能补；
- 哪些风险因此没有被覆盖；
- 替代验证做了什么；
- 下一步谁来补齐。

## 5. 每次测试报告必须包含

- 测试日期、分支、commit、工作目录。
- 测试环境：Python、Docker、MySQL、Redis、LLM provider。
- 本次改动影响范围。
- 跑过的命令、退出码、通过/失败数量。
- 完整业务量化指标。
- 失败用例的根因、修复、回归测试。
- 未覆盖风险和下一步。

## 6. 完成判定

只有同时满足以下条件，才能说“当前业务验证完成”：

- 影响范围内的局部测试通过。
- 至少一组完整业务链路测试通过。
- 写操作不只看回复，还验证数据库最终状态。
- 授权隔离、幂等、边界拒答没有回退。
- 量化指标已记录。
- 失败和未覆盖项已明确写入报告。

如果只跑了单元测试或只手动点了页面，不能称为完整业务测试完成。
