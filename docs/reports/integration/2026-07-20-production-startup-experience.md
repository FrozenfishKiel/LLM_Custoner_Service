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

正式启动链路目前卡在 embedding 模型资产/配置：

- 默认配置要求本地存在 `./models/bge-base-zh-v1.5`，但当前 worktree 没有该目录。
- 原 `.env.example` 的 `EMBEDDING_MODEL=text-embedding-v3` 与当前代码不兼容，已确认会加载失败。

这不是 Docker、MySQL、Redis 或 Neo4j 的阻塞。前一轮已经用 Docker 容器环境中的真实凭据验证过 MySQL、Redis、Neo4j 可连接；本轮新增的模型加载检查把后续真实阻塞提前暴露出来。

## 后续建议

上线前必须二选一：

1. 准备并随部署流程提供本地模型目录 `./models/bge-base-zh-v1.5`。
2. 明确改造 embedding 方案，使用当前代码可加载的 HuggingFace sentence-transformers 模型 ID，并接受首次下载/缓存策略。

在没有有效 embedding 模型前，不应声明正式 chat/GraphRAG 链路可上线。
