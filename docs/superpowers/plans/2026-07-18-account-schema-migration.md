# Account Schema Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a versioned, reversible MySQL schema baseline for consumer accounts, one-to-one business-user bindings, and anonymizable audit events without changing existing course business data.

**Architecture:** SQLAlchemy models in `atguigu_ai.auth` define the runtime contract. Alembic owns schema changes and reuses the environment-based database URL. The first revision creates only three new tables; account deletion and existing-business-data cleanup remain application-service transactions implemented in a later phase.

**Tech Stack:** Python 3.12, SQLAlchemy 2, Alembic, PyMySQL, MySQL 8, pytest

---

## File Map

- Create `atguigu_ai/auth/__init__.py`: public account model exports.
- Create `atguigu_ai/auth/models.py`: account, binding, and audit ORM schema.
- Create `alembic.ini`: non-secret Alembic entry configuration.
- Create `ecs_demo/migrations/env.py`: online/offline migration environment using environment configuration.
- Create `ecs_demo/migrations/script.py.mako`: Alembic revision template.
- Create `ecs_demo/migrations/versions/20260718_0001_account_baseline.py`: first schema revision.
- Create `tests/unit/auth/test_models.py`: model contract tests.
- Create `tests/integration/test_account_migration.py`: real MySQL upgrade/downgrade tests in an isolated temporary database.
- Create `docs/reports/migrations/2026-07-18-account-schema.md`: measured migration and QA evidence.
- Modify `.env.example`: document the explicit non-secret migration target gate.
- Modify `requirements-atguigu.txt`: add the bounded Alembic dependency.
- Modify `docs/TECHNICAL_DESIGN.md`: record the confirmed migration and deletion decisions.

### Task 1: Domain and Migration Decision Baseline

**Files:**
- Create: `CONTEXT.md`
- Modify: `docs/TECHNICAL_DESIGN.md`

- [ ] **Step 1: Record canonical domain terms**

Define Consumer Account, Administrator Account, Business User, Demo Business Data, Customer Service Conversation, Data Ownership, and Demo Data Reset without implementation details.

- [ ] **Step 2: Record the confirmed migration boundary**

State that the first revision creates only `account`, `account_user_binding`, and `audit_event`; existing business foreign keys and rows are unchanged. Clarify that audit events may remain only after irreversible actor anonymization and PII removal, while runtime logs retain anonymous aggregates.

- [ ] **Step 3: Verify documentation consistency**

Run:

```powershell
rg -n "首个账号基线迁移|不可逆匿名|Consumer Account|业务用户" CONTEXT.md docs/TECHNICAL_DESIGN.md
git diff --check
```

Expected: all decisions are present and the diff has no whitespace errors.

### Task 2: Model Contract RED

**Files:**
- Create: `tests/unit/auth/__init__.py`
- Create: `tests/unit/auth/test_models.py`

- [ ] **Step 1: Write model contract tests**

The tests import `Account`, `AccountUserBinding`, `AuditEvent`, and `AuthBase`, then assert:

```python
assert Account.__tablename__ == "account"
assert AccountUserBinding.__tablename__ == "account_user_binding"
assert AuditEvent.__tablename__ == "audit_event"
assert set(AuthBase.metadata.tables) == {
    "account",
    "account_user_binding",
    "audit_event",
}
```

Inspect SQLAlchemy metadata to verify column nullability and lengths, the unique normalized email, the one-to-one binding constraints, the `account_id` ORM foreign key with `CASCADE`, all planned indexes/check constraints, and the absence of ORM foreign keys on `user_id` and `audit_event.actor_account_id`. The real MySQL migration test verifies the physical `user_id -> user_info.user_id ON DELETE CASCADE` foreign key.

- [ ] **Step 2: Run RED**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth/test_models.py -q
```

Expected: FAIL because `atguigu_ai.auth` does not exist.

### Task 3: Account Model GREEN

**Files:**
- Create: `atguigu_ai/auth/__init__.py`
- Create: `atguigu_ai/auth/models.py`
- Test: `tests/unit/auth/test_models.py`

- [ ] **Step 1: Implement enums and declarative base**

Define string enums `AccountRole(consumer, admin)`, `AccountStatus(pending, active, disabled)`, and `AuditResult(success, failure)`. Define an isolated `AuthBase(DeclarativeBase)`; do not call `create_all`.

- [ ] **Step 2: Implement the three model contracts**

Use these exact storage contracts:

```text
account
  account_id VARCHAR(36) PK
  email VARCHAR(254) NOT NULL
  email_normalized VARCHAR(254) NOT NULL UNIQUE
  password_hash VARCHAR(255) NOT NULL
  role VARCHAR(16) NOT NULL CHECK consumer/admin
  status VARCHAR(16) NOT NULL CHECK pending/active/disabled
  email_verified_at DATETIME(6) NULL
  created_at DATETIME(6) NOT NULL
  updated_at DATETIME(6) NOT NULL
  INDEX(status, created_at)
  INDEX(role, status)

account_user_binding
  account_id VARCHAR(36) PK ORM/DB FK account.account_id ON DELETE CASCADE
  user_id VARCHAR(50) NOT NULL UNIQUE; DB FK user_info.user_id ON DELETE CASCADE is migration-owned
  seed_version VARCHAR(32) NOT NULL
  initialized_at DATETIME(6) NOT NULL

audit_event
  event_id VARCHAR(36) PK
  request_id VARCHAR(64) NOT NULL
  actor_account_id VARCHAR(80) NULL, deliberately no FK
  actor_role VARCHAR(16) NOT NULL
  event_type VARCHAR(64) NOT NULL
  target_type VARCHAR(32) NULL
  target_id VARCHAR(64) NULL
  result VARCHAR(16) NOT NULL CHECK success/failure
  metadata_json JSON NULL
  created_at DATETIME(6) NOT NULL
```

Indexes on `audit_event`: `request_id`, `(actor_account_id, created_at)`, `(event_type, created_at)`, `(target_type, target_id)`, and `created_at`.

- [ ] **Step 3: Run GREEN**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth/test_models.py -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit tests/security -q
```

Expected: both commands exit successfully and all collected tests pass.

### Task 4: Alembic and Migration RED

**Files:**
- Modify: `requirements-atguigu.txt`
- Create: `tests/integration/test_account_migration.py`

- [ ] **Step 1: Add migration dependency**

Add:

```text
alembic>=1.13.0,<2.0.0
```

Install it into `D:\Anaconda3\envs\ai-content-ops`:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pip install -r requirements-atguigu.txt
```

Expected: exit code `0` and Alembic resolves within the declared range.

- [ ] **Step 2: Write an isolated MySQL migration test**

The test must:

1. Connect to the local MySQL server using environment settings without printing the URL.
2. Generate the database name internally as `llm_cs_test_<32 lowercase hex>`, validate it against `^llm_cs_test_[0-9a-f]{32}$`, and reject `ecs`, `mysql`, `information_schema`, `performance_schema`, and `sys` before every create/drop operation.
3. Create only the pre-existing dependency `user_info(user_id VARCHAR(50) PRIMARY KEY)`.
4. Configure Alembic in memory with the temporary database URL.
5. Run `upgrade head`, inspect the three new tables, foreign keys, unique/check constraints, and indexes.
6. Insert an account and business user, verify duplicate normalized email and duplicate binding fail.
7. Run `downgrade base`, verify the three new tables disappear, `user_info` remains, and the standard `alembic_version` table remains with zero rows.
8. Run `upgrade head` again to prove repeatability.
9. Close Alembic/SQLAlchemy connections and dispose engines before cleanup, quote the already validated identifier with MySQL backticks, and drop the temporary database in `finally`. Cleanup failure must fail the test and report only the temporary database name.

- [ ] **Step 3: Run RED against a reachable MySQL container**

Start only `llm-cs-mysql`, then run:

```powershell
docker start llm-cs-mysql
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_account_migration.py -q -m integration
```

Expected: FAIL because Alembic configuration and revision files do not exist. A stopped or unreachable database is a test-environment failure, not the required RED.

### Task 5: Alembic Migration GREEN

**Files:**
- Create: `alembic.ini`
- Create: `ecs_demo/migrations/env.py`
- Create: `ecs_demo/migrations/script.py.mako`
- Create: `ecs_demo/migrations/versions/20260718_0001_account_baseline.py`
- Modify: `.env.example`
- Test: `tests/integration/test_account_migration.py`

- [ ] **Step 1: Add secret-free Alembic configuration**

Set `script_location = ecs_demo/migrations`. `env.py` must use an in-memory test URL when supplied by pytest and otherwise call `build_database_url()`. Before online or offline migration it computes `host:port/database` from the URL and requires an exact match with `MIGRATION_EXPECTED_TARGET`; missing or mismatched values raise a credential-free `RuntimeError`. Document `MIGRATION_EXPECTED_TARGET=127.0.0.1:3306/ecs` in `.env.example`. The migration environment must never log or persist the rendered password.

- [ ] **Step 2: Implement upgrade**

Create the three tables and exact constraints/indexes from Task 3. Use MySQL-compatible types and `CURRENT_TIMESTAMP(6)` defaults. Before creation, inspect the target and fail if any of the three target tables already exists without the expected Alembic revision. Do not alter `user_info` or any other existing table.

- [ ] **Step 3: Implement downgrade**

Drop in dependency order: `audit_event`, `account_user_binding`, then `account`. Do not drop or alter `user_info` or Alembic's own `alembic_version` table; after `downgrade base`, Alembic clears the version row itself.

- [ ] **Step 4: Run integration GREEN**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_account_migration.py -q -m integration
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth/test_models.py tests/security -q
```

Expected: both commands exit successfully and all tests pass with no database URL or credentials in output.

### Task 6: Course Database Upgrade and Independent QA

**Files:**
- Verify: local MySQL `ecs` schema
- Create: `docs/reports/migrations/2026-07-18-account-schema.md`

- [ ] **Step 1: Capture non-sensitive pre-migration facts**

Run this read-only query and record its four counts under `Pre-migration counts` in `docs/reports/migrations/2026-07-18-account-schema.md`:

```powershell
@'
from sqlalchemy import text
from ecs_demo.actions.db import SessionLocal

tables = ("user_info", "order_info", "receive_info", "postsale")
session = SessionLocal()
try:
    for table in tables:
        count = session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        print(f"{table}={count}")
finally:
    session.close()
'@ | D:\Anaconda3\envs\ai-content-ops\python.exe -
```

Do not query or record personal field values.

Verify the non-sensitive target and opt in for this process only:

```powershell
$env:MIGRATION_EXPECTED_TARGET = '127.0.0.1:3306/ecs'
@'
from ecs_demo.actions.db import build_database_url
url = build_database_url()
actual = f"{url.host}:{url.port}/{url.database}"
expected = "127.0.0.1:3306/ecs"
print(actual)
if actual != expected:
    raise SystemExit(1)
'@ | D:\Anaconda3\envs\ai-content-ops\python.exe -
```

Expected: prints only `127.0.0.1:3306/ecs`. Any other target blocks the DDL.

- [ ] **Step 2: Apply the revision**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m alembic upgrade head
```

Expected: revision `20260718_0001` applied exactly once.

If upgrade fails, do not rerun immediately. Run `alembic current` and the Step 3 table-inspection script. If the revision is absent but one of the three new tables exists, verify each partial table has zero rows, then drop only `audit_event`, `account_user_binding`, and `account` in that reverse dependency order. Record the failure and cleanup in the migration report before rerunning. Never drop or alter `user_info` or another existing table.

- [ ] **Step 3: Verify data preservation and schema**

Run the Step 1 count command again and record the results under `Post-migration counts`; values must exactly equal the pre-migration counts. Then run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m alembic current
```

Record revision output under `Applied revision`. Then run:

```powershell
@'
from sqlalchemy import inspect
from ecs_demo.actions.db import engine

expected = {"account", "account_user_binding", "audit_event", "alembic_version"}
present = expected.intersection(inspect(engine).get_table_names())
for table in sorted(present):
    print(table)
if present != expected:
    raise SystemExit(1)
'@ | D:\Anaconda3\envs\ai-content-ops\python.exe -
```

Record the four printed names under `Created tables`.

- [ ] **Step 4: Run all automated checks**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit tests/security -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_account_migration.py -q -m integration
D:\Anaconda3\envs\ai-content-ops\python.exe -m compileall -q atguigu_ai ecs_demo/actions ecs_demo/migrations
```

Record each command, exit code, pass/fail count, duration, and compile error count under `Automated verification` in the migration report.

- [ ] **Step 5: Independent QA review**

The continuous QA Agent reruns the exact integration-test and count commands from Steps 3 and 4. Append its pass/fail result, measured counts, defects, and residual risk under `Independent QA` in `docs/reports/migrations/2026-07-18-account-schema.md`.

- [ ] **Step 6: Commit the migration slice**

After spec review, quality review, and independent QA pass:

```powershell
git add .env.example CONTEXT.md docs/TECHNICAL_DESIGN.md docs/superpowers/plans/2026-07-18-account-schema-migration.md docs/reports/migrations requirements-atguigu.txt atguigu_ai/auth alembic.ini ecs_demo/migrations tests
git diff --cached --check
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "feat: add account schema migration"
```

Expected: clean working tree and a commit containing only the account schema slice.
