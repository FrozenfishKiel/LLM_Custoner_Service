# 下一个 AI 接手交接文档

日期：2026-07-20  
当前工作区：`D:\Projects\llm_customer_service_ops`  
当前分支：`feat/ops-startup-experience`  
远程仓库：`https://github.com/FrozenfishKiel/LLM_Custoner_Service.git`

这份文档用于让下一个 AI 快速接上当前工作。重点是先理解我们已经把“生产启动体验”和“项目内 embedding 模型”这条链路推进到了什么程度，再继续做上线前剩余验证。不要从零重做，也不要重新下载模型。

## 当前已经完成到哪里

当前分支最近几个关键提交如下：

```text
b254bf3 test: add production startup server e2e
dffe97d fix: bundle embedding model and action loader
8347cd0 test: harden production startup preflight
ae79bf4 feat: add production startup launcher
64e3756 feat: wire production readiness chain
```

生产启动入口已经存在：

- `start_customer_service_production.ps1`
- `start_customer_service_production.bat`

这个入口和旧课程 demo 入口是分开的。旧入口仍是：

- `start_customer_service.ps1`
- `start_customer_service.bat`

生产启动脚本现在会做几类预检：必填生产配置、Python 运行时依赖、embedding 模型加载、MySQL/Redis/Neo4j 真实连接、FastAPI production app factory。这里最重要的是，它现在不会再出现“GraphRAG 或模型实际失败，但启动器仍然显示成功”的假成功。

## embedding 模型已经封装进项目

用户明确要求：embedding 模型下载以后直接封装进项目，后续不允许重复下载，也不要再把模型或项目依赖装到 C 盘。

当前模型位置：

```text
models/bge-base-zh-v1.5
```

模型文件已经通过 Git LFS 跟踪并推送。关键大文件是：

```text
models/bge-base-zh-v1.5/pytorch_model.bin
```

`start_customer_service_production.ps1` 默认使用：

```text
EMBEDDING_MODEL=./models/bge-base-zh-v1.5
```

脚本还会默认把 HuggingFace / transformers / sentence-transformers 缓存指向项目内：

```text
.model-cache/
```

这个目录已被 `.gitignore` 忽略，不应提交。后续不要再主动下载模型；如果确实要更新模型，必须先和用户确认，因为这会影响仓库体积、LFS、部署资产和启动耗时。

当前仍可见的本地缓存目录：

```text
.model-cache/
models/bge-base-zh-v1.5/.cache/
```

它们是 ignored，本地可存在，不影响仓库。不要把这些缓存目录提交。

## 物流 Action 导入错误已经修复

之前生产启动时出现：

```text
Failed to load actions from ... action_logistics.py: cannot import name 'ActionGetLogisticsCompanys'
```

根因不是 `ActionGetLogisticsCompanys` 类不存在。直接 import `ecs_demo.actions.action_logistics`、`actions.action_logistics` 和 `ecs_demo.actions` 都能看到这个类。

真正原因在动态 Action loader：`atguigu_ai/agent/agent.py` 使用 `spec_from_file_location("actions.action_logistics", file)` 手工加载子模块；而 `action_logistics.py` 顶部有 `from .security import ...`，这会触发 `actions/__init__.py`。`actions/__init__.py` 又反过来从正在半初始化的 `actions.action_logistics` 导入 `ActionGetLogisticsCompanys`，形成循环导入。

修复方式是在动态加载子模块之前，先在 `sys.modules` 里注册一个轻量的 `actions` 包对象并设置 `__path__`，避免执行 `actions/__init__.py`，从而切断循环导入。

相关代码：

- `atguigu_ai/agent/agent.py`
- `ecs_demo/actions/action_logistics.py`
- `ecs_demo/actions/__init__.py`

回归测试：

- `tests/unit/agent/test_custom_action_loader.py`

## 当前已有的验证证据

已跑过的关键验证：

```text
29 passed in 25.40s
```

覆盖：

- Action loader 能注册物流 Action。
- 生产启动脚本单元测试通过。
- Action 相关单元测试通过。

真实生产启动预检已通过：

```text
EMBEDDING_MODEL_LOCAL_CONFIG_OK D:\Projects\llm_customer_service_ops\.\models\bge-base-zh-v1.5
EMBEDDING_MODEL_LOAD_OK
MYSQL_CONNECTION_OK
REDIS_CONNECTION_OK
NEO4J_CONNECTION_OK
PRODUCTION_APP_FACTORY_OK route_count=20
PRODUCTION_STARTUP_CHECK_OK http://127.0.0.1:8099
```

新增了一个真实长驻启动测试：

```text
tests/integration/test_production_startup_server.py
```

这个测试默认 skip，避免普通 clone 后因为没有 Docker、真实模型或本机服务而失败。需要真实运行时设置：

```powershell
$env:RUN_PRODUCTION_STARTUP_SERVER_TEST='1'
python -m pytest tests/integration/test_production_startup_server.py -q -s
```

它会真实启动 `start_customer_service_production.ps1`，然后访问：

- `/health/ready`
- `/`
- `/login`
- `/health/live`
- `/internal/metrics`
- `/api/chat/messages`

用户曾问为什么卡住。那次卡住不是模型下载，也不是 Docker。原因是测试通过 PowerShell 启动长驻服务后，真正监听端口的是脚本内部 Python/uvicorn 子进程，原清理逻辑只杀父进程，导致监听进程残留。现在测试已改为按端口清理真实监听进程，复测结果：

```text
1 passed in 84.23s
```

最后一次提交前验证：

```text
10 passed, 1 skipped
git diff --check 通过
UTF-8 检查通过
```

## 下一个 AI 需要先读的资料

建议按这个顺序读，不要一上来全仓库乱翻。

第一组是产品和上线目标：

- `docs/PRD.md`
- `docs/TECHNICAL_DESIGN.md`
- `docs/HANDOFF.md`

注意：仓库里部分旧中文文档在 PowerShell 输出里可能显示乱码，优先按 UTF-8 打开文件，不要直接相信终端乱码显示。

第二组是生产启动和运维入口：

- `README.md`
- `start_customer_service_production.ps1`
- `start_customer_service_production.bat`
- `.env.example`
- `.gitignore`
- `.gitattributes`
- `docs/reports/integration/2026-07-20-production-startup-experience.md`

第三组是生产 app 装配：

- `atguigu_ai/api/production.py`
- `atguigu_ai/api/server.py`
- `atguigu_ai/api/routes/chat.py`
- `atguigu_ai/api/dependencies.py`

第四组是 Agent 和业务 Action：

- `atguigu_ai/agent/agent.py`
- `ecs_demo/actions/action_order.py`
- `ecs_demo/actions/action_logistics.py`
- `ecs_demo/actions/action_postsale.py`
- `ecs_demo/actions/security.py`
- `ecs_demo/addons/information_retrieval.py`

第五组是现有测试：

- `tests/unit/ops/test_production_startup_scripts.py`
- `tests/integration/test_production_startup_server.py`
- `tests/e2e/test_customer_frontend_browser.py`
- `tests/unit/actions/`
- `tests/integration/test_action_ownership_audit.py`
- `tests/unit/api/test_chat_routes.py`
- `tests/integration/test_chat_authorization_http.py`

## 目前还没有完成的上线工作

现在不能把项目说成“上线完成”。当前只是生产启动入口、模型资产、Action loader 和 HTTP 级长驻启动验证这一段已经压住了。

下一阶段优先做真实浏览器 E2E。现有 `tests/e2e/test_customer_frontend_browser.py` 是浏览器级测试，但它主要使用 fake auth service、fake agent 和测试 app。它证明了前端交互、登录表单、聊天表单、CSRF、限流提示等 UI 行为，但不是基于 `start_customer_service_production.ps1` 启动出来的真实生产服务。下一步应该补一个真实生产服务浏览器 E2E，至少覆盖打开首页、登录、发送客服消息、查询订单/物流/售后、登出、未登录保护、CSRF 错误和截图证据。

第二个优先级是 Auth + Chat + 业务数据全链路。现在 HTTP 级测试只确认未登录 chat 返回 401，还没有证明登录用户真的能通过 `/api/chat/messages` 驱动 Agent，并且只能访问自己的订单、地址、物流和售后数据。需要验证账号绑定业务用户、Redis session、MySQL、Neo4j、Action 归属校验和 tracker key 都串起来。

第三个优先级是业务 Action 可用性回归。物流导入错误修掉了，但仍要跑真实业务场景：订单详情、物流查询、修改地址、取消订单、售后申请、缺参数、错订单、越权订单、重复提交。这部分应该给出测试矩阵和结果，不要只跑一个 happy path。

第四个优先级是压力和风险测试。PRD 里提过 20 并发客服会话、非模型接口 P95、客服请求 P95、错误率、模型成本等指标。当前还没有完整压测数据。至少要做一轮带量化数据的测试，包括登录/首页/health/metrics/聊天请求延迟、并发请求下的错误率、Redis/MySQL/Neo4j 连接稳定性、模型超时或失败时的系统行为。

第五个优先级是监控和安全暴露面。`/internal/metrics` 当前依赖网络层限制，代码层没有鉴权。上线前要决定是由反向代理/内网 ACL 限制，还是加应用内 token。还需要检查日志脱敏，确保不记录密码、session、验证码、模型 key、完整地址和订单敏感信息。

第六个优先级是处理 LangChain / Neo4j 弃用 warning。真实启动时会看到 `Neo4jGraph` 和 `CypherQueryCorrector` 的弃用 warning。它当前不阻塞启动，但会影响后续维护，建议迁到 `langchain-neo4j` 或至少记录到上线风险清单。

## 继续工作的推荐顺序

下一个 AI 接手后，建议先确认工作区和分支：

```powershell
git status --short
git branch --show-current
git log --oneline -5
git lfs ls-files
```

然后先跑普通验证，不要直接跑真实长驻服务：

```powershell
python -m pytest tests/unit/ops/test_production_startup_scripts.py tests/unit/agent/test_custom_action_loader.py -q
python -m pytest tests/integration/test_production_startup_server.py -q
```

第二条默认应该是 skip，这是正常的。确认普通验证稳定后，再根据本机 Docker 和环境变量情况开启真实长驻测试。

如果要跑真实长驻测试，需要准备 MySQL、Redis、Neo4j、DeepSeek key、SMTP 基础配置和项目内模型。当前这台机器之前通过 Docker 容器 `llm-cs-mysql`、`llm-cs-redis`、`llm-cs-neo4j` 验证过，但不要假设别的机器也有这些容器。

如果继续做真实浏览器 E2E，建议不要和现有 fake E2E 混在一起。新建一个单独测试文件，例如：

```text
tests/e2e/test_production_server_browser.py
```

让它默认 skip，需要设置类似：

```text
RUN_PRODUCTION_BROWSER_E2E=1
```

这样普通测试不会被真实外部服务拖死，正式上线验证时又有可重复入口。

## 注意事项

不要再往 C 盘下载模型或项目依赖。前面确实曾把 `sentence-transformers` 及其依赖装进 Codex 自带 Python runtime，路径在 `C:\Users\frozenfish\.cache\codex-runtimes\...`。后续如果要优化，应建立 D 盘项目运行环境，并把 pip/HF/torch/transformers 缓存都指向 D 盘。

不要删除或移动 `models/bge-base-zh-v1.5`，除非用户明确确认。它现在是项目运行链路的一部分，并通过 Git LFS 管理。

不要把 `.env`、`.model-cache/`、`docker-data/`、`trackers/`、日志或模型下载器内部 `.cache/` 提交进仓库。

不要声称项目已经上线。更准确的状态是：生产启动入口和模型资产已经可用，HTTP 级启动验证通过；真实浏览器业务链路、业务 Action 全回归、压力/风险测试、监控访问控制仍未完成。

## 用户要求的开发方式

用户不是只要“写功能”，而是明确要求按一套上线项目的工作流推进。下一个 AI 接手时要把这个当成项目约束，而不是聊天偏好。

先说最核心的节奏：每个任务都要先确认最小合理范围，然后写测试或复现脚本，再实现，再自己运行，再自己像真实用户一样使用一遍，发现问题就自测自改。不要写完让用户去跑第一遍。用户已经多次强调，如果没有明显分岔问题，就继续推进；只有遇到会改变方向、需要用户授权、涉及外部服务真实阻塞或高风险选择时才停下来问。

### 小工作流

当前项目默认按这个小工作流走：

1. 明确当前任务范围和不做什么。
2. 先读相关代码、文档、配置和已有测试。
3. 如果是 bug，先建立能复现的反馈环；如果是功能或行为变更，先写会失败的测试。
4. 看到测试按预期失败后，再做最小实现。
5. 跑目标测试，再跑相关回归测试。
6. 自己启动服务或脚本，像真实用户一样实际使用。
7. 把结果、失败、剩余风险写进中文报告或交接文档。
8. 提交并推送，保持工作区干净。

这套流程的目的不是形式化，而是避免“看起来能跑”但用户一试就坏。尤其是启动脚本、浏览器 E2E、登录/聊天/Action 这种链路，必须自己跑过。

### harness engineering 的含义

用户说的 harness engineering，不是只写几个单元测试。这里的 harness 应该理解为一套可重复的工程验证装置，让下一个人能用命令重现结果。

当前已经形成了几个 harness：

- `tests/unit/ops/test_production_startup_scripts.py`：验证启动脚本的配置、依赖、模型检查、secret 不泄露。
- `tests/integration/test_production_startup_server.py`：真实启动生产脚本后做 HTTP 使用验证，默认 skip，需要显式环境变量开启。
- `tests/e2e/test_customer_frontend_browser.py`：浏览器级 UI 交互测试，但目前使用 fake app/fake agent，不等于真实生产服务浏览器 E2E。
- `docs/reports/integration/2026-07-20-production-startup-experience.md`：记录启动体验和验证证据。

后续每新增一个上线关键能力，都应尽量留下类似 harness：能一条命令跑，能说明前置条件，能输出可保存的证据，默认不要让普通开发者因为缺外部服务而全部失败。

### 多 agent 使用规则

用户允许开多个 agent，但明确说多 agent 有上限，必须合理分配，不能互相踩文件。

如果后续环境允许使用子 agent，建议最多同时开 2 到 3 个，不要为了并行而并行。分工必须按文件和职责隔离：

- 一个 agent 做真实浏览器 E2E 和用户路径验证，只改 `tests/e2e/`、报告和必要测试工具。
- 一个 agent 做业务 Action 回归和风险测试，只改 `tests/unit/actions/`、`tests/integration/`、`ecs_demo/actions/` 中明确相关文件。
- 一个 agent 做运维/启动体验，只改启动脚本、README、ops 测试和运维报告。

不要让两个 agent 同时改同一个核心文件，例如 `atguigu_ai/api/server.py`、`atguigu_ai/agent/agent.py`、`start_customer_service_production.ps1`。如果确实需要多人动同一处，先停下来重新分工，或者由主 agent 串行处理。

每个子 agent 都应该被要求返回：

- 改了哪些文件。
- 跑了哪些命令。
- 通过/失败的原始结果。
- 剩余风险。
- 是否有未提交改动。

主 agent 不能直接相信子 agent 的“完成”说法，必须自己看 diff、跑关键验证，再决定是否提交。

### 测试分层要求

用户要求“从头到尾测试”，这里要按真实上线项目理解，不只是 happy path。

后续测试至少要分成这些层：

- 正常使用：真实用户会做什么，例如打开页面、注册/登录、发客服消息、查询订单、查询物流、申请售后、登出。
- 刁钻测试：缺参数、错订单号、重复点击、刷新页面、过期 session、错误 CSRF、恶意消息要求切换用户。
- 风险测试：越权访问别人的订单、prompt 注入、secret 泄露、metrics 暴露、日志敏感信息。
- 外部依赖失败：MySQL、Redis、Neo4j、DeepSeek/LLM、SMTP 任意一个不可用时，系统是否稳定失败，而不是编造业务结果。
- 压力测试：至少覆盖 20 并发客服会话目标、P95、错误率和资源消耗。
- 监控验证：health、ready、metrics、日志、告警或至少可观测输出。

测试报告必须有量化数据。不要只写“测试通过”，要写命令、耗时、通过数量、P95 或最大耗时、错误率、是否有 warning、哪些 warning 暂不阻塞。

### 文档和沟通要求

用户是中文用户，明确要求文档写中文。新增 spec、报告、交接文档、测试说明都应写中文。代码里的变量名和测试名可以保持英文，但面向用户和协作的文档不要写英文大段说明。

沟通时不要装作很轻松。这个项目离上线还有真实工作量，应该直接说明进度、阻塞和风险。可以继续推进时就继续；遇到以下情况再停下来问用户：

- 要改数据库 schema 或迁移。
- 要改认证/权限模型。
- 要删除或移动大量文件。
- 要改生产部署策略。
- 要下载/替换大模型资产。
- 要把依赖或缓存写到 C 盘。
- 要做会明显影响另一个窗口工作的文件改动。

### Git 和隔离要求

当前这条线在隔离 worktree：

```text
D:\Projects\llm_customer_service_ops
```

分支：

```text
feat/ops-startup-experience
```

继续工作时先确认：

```powershell
git status --short
git branch --show-current
```

提交只包含当前任务相关文件。不要把 `.env`、缓存、日志、Docker 数据卷、临时截图、未说明的大文件提交进去。模型目录 `models/bge-base-zh-v1.5` 是例外，它已经作为用户确认后的项目资产通过 Git LFS 管理。
