# 生产启动体验与运维预检报告

日期：2026-07-20

## 本轮改动

本轮在独立 worktree `D:\Projects\llm_customer_service_ops`、分支 `feat/ops-startup-experience` 中完成，避免和 LLM 质量修复窗口互相改同一批文件。

新增正式启动入口：

- `start_customer_service_production.ps1`
- `start_customer_service_production.bat`

保留原课程 demo 入口：

- `start_customer_service.ps1`
- `start_customer_service.bat`

README 启动段已补充中文说明，区分“正式前端 + 生产 auth/chat 链路”和“课程 inspect Demo”。

## 脚本能力

`start_customer_service_production.ps1` 支持：

- `-CheckOnly`：只做预检和 app factory 检查，不启动长驻服务。
- `-SkipExternalServiceChecks`：跳过 MySQL/Redis TCP 探测，用于本机服务未启动时验证脚本和 FastAPI app factory。
- `-NoBrowser`：不自动打开浏览器，便于自动化测试。
- `-EnableInspect`：显式打开 inspect 页面；默认关闭。
- `-BindAddress` / `-Port`：指定监听地址和端口。

预检覆盖：

- 必填生产配置存在性：MySQL、Neo4j、DeepSeek、auth public base URL、SMTP 基础配置。
- Python 运行时依赖：`dotenv`、`fastapi`、`uvicorn`、`jieba`、`neo4j`、`neo4j_graphrag`、`langchain_community`、`langchain_openai`。
- 外部服务：MySQL、Redis TCP 可达性。
- FastAPI app factory：构建 `create_production_app()`，并像真实用户一样请求 `/`、`/login`、`/health/live`、`/health/ready`、`/internal/metrics`、未登录 `/api/chat/messages`。

## 我实际跑过的测试

### 1. TDD 脚本单元测试

命令：

```powershell
C:\Users\frozenfish\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest tests/unit/ops/test_production_startup_scripts.py -q
```

结果：

```text
3 passed
```

覆盖：

- PowerShell 脚本 `-CheckOnly -SkipExternalServiceChecks` 可以真实执行并构建生产 app。
- 缺少 `AUTH_PUBLIC_BASE_URL` 时会快速失败。
- 输出不包含测试密码、Neo4j 密码、DeepSeek key 占位值。
- `.bat` wrapper 确实委托到 `start_customer_service_production.ps1`。

### 2. 手动预检使用测试

命令：

```powershell
.\start_customer_service_production.ps1 -CheckOnly -SkipExternalServiceChecks -NoBrowser -Port 8099
```

结果：

```text
PYTHON_RUNTIME_OK
PRODUCTION_APP_FACTORY_OK route_count=20
PRODUCTION_STARTUP_CHECK_OK http://127.0.0.1:8099
```

备注：因为本机 Neo4j 未启动，Agent 内部 GraphRAG 初始化仍会记录 “Could not connect to Neo4j database”。这是 `-SkipExternalServiceChecks` 明确允许的降级场景；不加该参数时会在外部服务检查阶段提前失败。

### 3. 真实启动 + HTTP 使用测试

我用脚本后台启动了生产服务到临时端口 `8099`，随后实际请求页面和接口，再关闭监听进程。

结果：

```text
FRONT_STATUS=200
LOGIN_STATUS=200
LIVE={"status":"alive"}
READY={"ready":true,"checks":{"auth_configured":true,"chat_configured":true,"agent_ready":true,"rate_limiter_configured":true}}
METRICS_HAS_AUTH=True
CHAT_UNAUTH_STATUS=401
```

这说明脚本不只是静态存在，实际可以把正式前端、健康检查、metrics 和未登录聊天保护跑起来。

### 4. 外部服务失败路径测试

命令：

```powershell
.\start_customer_service_production.ps1 -CheckOnly -NoBrowser -Port 8099
```

当前本机 Docker/MySQL 不可用，结果按预期失败：

```text
MySQL is not reachable at 127.0.0.1:3306
```

该失败不泄露 MySQL 密码、SMTP 密码、Neo4j 密码或 DeepSeek key。

## 剩余问题

- 当前本机 Docker daemon 未运行，所以完整外部服务预检不能通过。
- `ecs_demo/actions/action_logistics.py` 仍会打印一个 action 导入错误：`ActionGetLogisticsCompanys` 无法从同文件导入。它不阻止 FastAPI 启动，但会影响后续物流相关能力，建议放到 LLM/业务 action 修复线处理。
- `/internal/metrics` 仍需在真实部署时通过反向代理或网络策略限制访问。
