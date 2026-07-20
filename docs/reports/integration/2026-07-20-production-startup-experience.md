# 生产启动体验与运维预检报告

日期：2026-07-20

## 本轮目标

本轮继续在隔离 worktree `D:\Projects\llm_customer_service_ops`、分支 `feat/ops-startup-experience` 中推进“上线运维/启动体验”任务，重点不是改业务功能，而是让正式启动入口在用户双击或运行前能尽早暴露真实缺项。

## 已完成改动

- 扩展 `start_customer_service_production.ps1` 的 Python 运行时依赖预检，覆盖真实 Agent 链路会用到的 Redis、SQLAlchemy、PyMySQL、Jinja2、LangGraph、LangChain Core、SentenceTransformers 等模块。
- 外部服务预检从 TCP 探测升级为真实凭据检查：
  - MySQL 执行 `SELECT 1`
  - Redis 执行 `PING`
  - Neo4j 执行 `verify_connectivity()` 和 `RETURN 1`
- 增加 Neo4j 端口预检，避免只检查 MySQL/Redis。
- 增加 embedding 模型预检：
  - `EMBEDDING_MODEL` 为空时失败。
  - 本地路径形式的 `EMBEDDING_MODEL` 必须真实存在。
  - 存在路径或远程模型 ID 都会继续用当前 Python 运行时实际初始化 `SentenceTransformer(EMBEDDING_MODEL)`。
  - 加载失败时启动脚本直接失败，不再让 app factory 吞掉 GraphRAG 初始化错误后返回“假成功”。
- 增加测试夹具，单元测试使用 fake `sentence_transformers`，避免测试依赖 HuggingFace 网络下载；真实手工验证不使用该夹具。

## 已运行验证

### 1. RED/GREEN：缺失本地 embedding 模型

新增测试先失败，失败原因为启动脚本没有提前识别缺失模型路径，而是继续构造 app 并返回 0。

修复后单项测试通过：

```text
1 passed
```

### 2. RED/GREEN：启动器必须实际加载 embedding 模型

新增测试先失败，失败原因为脚本中没有 `SentenceTransformer` 加载检查。

修复后单项测试通过：

```text
1 passed
```

### 3. 启动脚本单元测试

命令：

```powershell
C:\Users\frozenfish\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest tests/unit/ops/test_production_startup_scripts.py -q
```

结果：

```text
8 passed in 21.01s
```

覆盖内容：

- `-CheckOnly -SkipExternalServiceChecks -NoBrowser` 可执行。
- 缺少必要生产配置时会失败。
- 输出不包含测试密码、SMTP 密码、Neo4j 密码或 DeepSeek key 占位值。
- `.bat` 双击入口委托到 PowerShell 脚本。
- Python 运行时依赖覆盖真实 Agent 依赖。
- MySQL、Redis、Neo4j 会做真实连接/凭据检查。
- 本地 embedding 模型路径缺失会快速失败。
- 启动器会实际加载 `SentenceTransformer(EMBEDDING_MODEL)`。

### 4. 真实脚本验证：默认本地模型缺失

当前 worktree 没有 `.env`，项目内也没有 `./models/bge-base-zh-v1.5`。运行：

```powershell
.\start_customer_service_production.ps1 -CheckOnly -NoBrowser -Port 8099
```

结果按预期失败：

```text
Embedding model check failed: local EMBEDDING_MODEL path does not exist: D:\Projects\llm_customer_service_ops\.\models\bge-base-zh-v1.5
```

这说明启动脚本已经能在进入 app factory 前暴露模型资产缺失问题。

### 5. 真实脚本验证：`.env.example` 原有模型名不可用

将 `EMBEDDING_MODEL` 设置为原 `.env.example` 中的 `text-embedding-v3` 后运行：

```powershell
.\start_customer_service_production.ps1 -CheckOnly -NoBrowser -Port 8099
```

结果按预期失败：

```text
Embedding model load failed: RepositoryNotFoundError
Repository Not Found for url: https://huggingface.co/sentence-transformers/text-embedding-v3/resolve/main/modules.json
```

结论：`text-embedding-v3` 不是当前 `sentence_transformers.SentenceTransformer(...)` 能直接加载的有效模型名，不能作为生产启动示例配置。

## 当前真实阻塞

上一轮正式启动链路卡在 embedding 模型资产/配置：

- 默认配置要求本地存在 `./models/bge-base-zh-v1.5`，但当前 worktree 没有该目录。
- 原 `.env.example` 的 `EMBEDDING_MODEL=text-embedding-v3` 与当前代码不兼容，已确认会加载失败。

这不是 Docker、MySQL、Redis 或 Neo4j 的阻塞。前一轮已经用 Docker 容器环境中的真实凭据验证过 MySQL、Redis、Neo4j 可连接；本轮新增的模型加载检查把后续真实阻塞提前暴露出来。

## 2026-07-20 追加处理

用户要求 embedding 模型下载后直接封装进项目，后续不允许重复下载；同时要求解释并修复物流 Action 导入错误。

### embedding 模型处理

- 模型已下载到项目根目录 `models/bge-base-zh-v1.5`。
- 下载缓存强制放在 worktree 内 `.model-cache/`，并通过 `.gitignore` 忽略，不提交。
- `start_customer_service_production.ps1` 默认将 `HF_HOME`、`HUGGINGFACE_HUB_CACHE`、`SENTENCE_TRANSFORMERS_HOME` 和 `TRANSFORMERS_CACHE` 指向项目 `.model-cache/`，避免默认落到 C 盘。
- 模型目录使用 Git LFS 跟踪，避免普通 Git 直接提交大模型二进制。
- 真实启动预检已经在不显式设置 `EMBEDDING_MODEL`、不显式设置 HF 缓存变量的情况下通过，说明脚本会默认加载项目内模型。

验证结果：

```text
EMBEDDING_MODEL_LOCAL_CONFIG_OK D:\Projects\llm_customer_service_ops\.\models\bge-base-zh-v1.5
EMBEDDING_MODEL_LOAD_OK
MYSQL_CONNECTION_OK
REDIS_CONNECTION_OK
NEO4J_CONNECTION_OK
PRODUCTION_APP_FACTORY_OK route_count=20
PRODUCTION_STARTUP_CHECK_OK http://127.0.0.1:8099
```

### 物流 Action 导入错误

根因不是 `ActionGetLogisticsCompanys` 类不存在。直接导入 `ecs_demo.actions.action_logistics`、`actions.action_logistics` 和 `ecs_demo.actions` 都能看到该类。

真正原因是动态 Action loader 用 `spec_from_file_location("actions.action_logistics", file)` 手工加载子模块；`action_logistics.py` 顶部又使用 `from .security import ...`，这会触发 Python 解析 `actions` 包并执行 `actions/__init__.py`。而 `actions/__init__.py` 又反向从正在半初始化的 `actions.action_logistics` 导入 `ActionGetLogisticsCompanys`，形成循环导入，所以报：

```text
cannot import name 'ActionGetLogisticsCompanys' from 'actions.action_logistics'
```

修复方式是在动态加载子模块前，先在 `sys.modules` 中注册一个轻量的 `actions` 包对象并设置 `__path__`，避免执行 `actions/__init__.py`，从而切断循环导入。

回归测试：

```text
29 passed in 25.40s
```

## 后续建议

embedding 模型资产阻塞已解除。后续上线前仍建议处理 LangChain/Neo4j 弃用 warning，并继续做真实浏览器 E2E 与压力/风险测试。

## 2026-07-20 追加：真实长驻启动测试

在 `-CheckOnly` 预检之外，新增 `tests/integration/test_production_startup_server.py`，用于验证正式启动脚本真的能以长驻服务方式启动，并完成用户视角 HTTP 使用检查。

该测试默认跳过，避免普通开发者 clone 后被本机 Docker、真实模型和外部服务依赖卡住。需要真实验证时设置：

```powershell
$env:RUN_PRODUCTION_STARTUP_SERVER_TEST='1'
python -m pytest tests/integration/test_production_startup_server.py -q -s
```

覆盖内容：

- 通过 `start_customer_service_production.ps1` 启动真实生产服务。
- 等待 `/health/ready` 返回 ready。
- 请求 `/` 和 `/login`，确认正式前端页面可访问。
- 请求 `/health/live`，确认进程存活检查可用。
- 请求 `/internal/metrics`，确认指标存在且不输出关键 secret 名称。
- 未登录请求 `/api/chat/messages`，确认返回 `401`，不会绕过登录保护。

本轮调试中遇到一次测试卡住：原因不是模型下载或 Docker 服务，而是测试通过 PowerShell 启动长驻脚本后，真正监听端口的是脚本内部的 Python/uvicorn 子进程。原测试只杀父进程，导致子进程残留并让 pytest 外层等到超时。修复后测试结束时按端口定位并终止真实监听进程。

验证结果：

```text
1 passed in 84.23s (0:01:24)
```

复测后确认没有残留的本地监听进程。
