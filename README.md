# LLM Customer Service

电商智能客服系统示例项目。当前仓库包含客服 Agent、认证/会话、邮件、Redis Tracker、MySQL 业务数据、Action 归属审计、测试与中文工程文档。

## 仓库内容

- `atguigu_ai/`：核心框架、API、认证、会话、邮件、Agent、对话理解等代码。
- `ecs_demo/`：电商客服 Demo、Action、数据库模型、迁移和启动入口。
- `tests/`：单元测试、集成测试和安全回归测试。
- `docs/`：中文技术设计、计划、报告和验证证据。
- `.env.example`：可提交的环境变量模板。
- `requirements-atguigu.txt`：运行和测试依赖。

## 不提交到 Git 的内容

这些内容由 `.gitignore` 排除，不应提交：

- `.env`、`.env.*`：本地配置和密钥；只提交 `.env.example`。
- `docker-data/`：本地 MySQL/Redis/Neo4j 数据卷或挂载数据。
- `ecs_demo/models/`、`course_assets/`：课程资产和大模型文件，体积大且可重新准备。
- `trackers/`、`*.log`：运行态对话状态和日志。
- `__pycache__/`、`.pytest_cache/`、`*.egg-info/`、`build/`、`dist/`：Python 缓存和构建产物。

## 本地准备

建议使用 Python 3.11 环境。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-atguigu.txt
python -m pip install -e .
```

复制环境模板：

```powershell
Copy-Item .env.example .env
```

然后按本机情况填写 `.env`。生产环境不要使用模板里的占位密码。

## 外部服务

本项目的完整集成测试和 Demo 依赖：

- MySQL：默认本地 `127.0.0.1:3306`，数据库 `ecs`。
- Redis：默认 `127.0.0.1:6379`。
- Neo4j：默认 `bolt://127.0.0.1:7687`。
- SMTP：开发环境可使用本地 fake SMTP 或测试替身。

本地已有脚本默认使用容器名：

- `llm-cs-mysql`
- `llm-cs-redis`
- `llm-cs-neo4j`

如果你的 Python 或 CLI 不在 PATH 中，可以在启动前指定：

```powershell
$env:PYTHON_EXE="C:\Path\To\python.exe"
$env:ATGUIGU_CLI_EXE="C:\Path\To\atguigu.exe"
```

## 启动 Demo

```powershell
.\start_customer_service.ps1
```

只准备环境、不启动服务：

```powershell
.\start_customer_service.ps1 -PrepareOnly
```

启动后常用地址：

- Inspect UI: `http://127.0.0.1:8012/inspect`
- API docs: `http://127.0.0.1:8012/docs`

停止本地服务：

```powershell
.\stop_customer_service.bat
```

## 测试

常用命令：

```powershell
python -m pytest tests/unit/actions -q
python -m pytest tests/integration/test_action_ownership_audit.py -q -s -m integration
python -m pytest tests -q
```

集成测试会创建并清理 `llm_cs_test_<32 hex>` 临时 MySQL 库，并使用 Redis DB 15。不要并行运行会重建/清空同一 Redis 容器状态的集成测试。

## 当前上线前剩余工程项

详见 `docs/reports/integration/2026-07-19-action-ownership-audit.md`，当前仍包括生产限流、正式前端与浏览器 E2E、生产配置检查、监控告警与量化指标、备份恢复/压测/发布回滚演练等。
