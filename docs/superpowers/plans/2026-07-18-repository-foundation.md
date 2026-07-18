# Repository Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish a reproducible Git, configuration-safety, and pytest baseline without changing database schema or customer-service behavior.

**Architecture:** Keep the existing application structure. Protect local/runtime assets with repository ignore rules, document every required environment variable with safe placeholders, and make the existing MySQL connection consume environment configuration through a small pure URL builder that can be tested without a live database.

**Tech Stack:** Git, Python 3.12, pytest, python-dotenv, SQLAlchemy, PowerShell

---

## File Map

- Create `.gitignore`: excludes secrets, local data, generated models, logs, caches, reports, and build output.
- Create `.env.example`: documents safe configuration names and non-secret placeholders.
- Create `pytest.ini`: defines test discovery and test categories.
- Create `tests/security/test_repository_safety.py`: verifies ignore rules and prevents fixed database credentials from returning.
- Modify `ecs_demo/actions/db.py`: loads local environment configuration and constructs the SQLAlchemy URL without embedded credentials.
- Modify `start_customer_service.ps1`: reuses `actions.db` for the seed-data count instead of embedding a second database connection.

### Task 1: Repository Safety Bootstrap

**Files:**
- Create: `.gitignore`
- Create: `.env.example`
- Create: `pytest.ini`
- Create: `tests/security/__init__.py`

- [ ] **Step 1: Add ignore rules before Git initialization**

Create `.gitignore` with explicit exclusions for `.env`, `docker-data/`, `ecs_demo/models/`, `course_assets/`, `trackers/`, logs, Python caches, package metadata, test caches, coverage output, certificates, local IDE files, and generated test artifacts. Keep `.env.example` eligible for tracking.

- [ ] **Step 2: Add safe configuration documentation**

Create `.env.example` with the variables defined by `TECHNICAL_DESIGN.md`: application, MySQL, Redis, Neo4j, DeepSeek, SMTP, model timeout/retry, and the legacy embedding variable. Values must be local addresses or explicit `change-me` placeholders, never copied from `ecs_demo/.env`.

- [ ] **Step 3: Add pytest discovery configuration**

Create `pytest.ini`:

```ini
[pytest]
testpaths = tests
python_files = test_*.py
addopts = -ra --strict-markers
markers =
    integration: requires local infrastructure
    e2e: requires a running browser application
    evaluation: runs the versioned LLM evaluation set
    load: runs performance or concurrency checks
```

- [ ] **Step 4: Initialize Git and verify ignored runtime assets**

Run:

```powershell
git init
git check-ignore ecs_demo/.env docker-data/mysql-data/ibdata1 ecs_demo/run.stdout.log ecs_demo/models/bge-base-zh-v1.5/pytorch_model.bin trackers/test_user.json
```

Expected: all five paths are printed as ignored.

### Task 2: Fixed-Credential Regression Tests

**Files:**
- Create: `tests/security/test_repository_safety.py`
- Test: `tests/security/test_repository_safety.py`

- [ ] **Step 1: Write the failing source-safety tests**

Add tests that read `ecs_demo/actions/db.py` and `start_customer_service.ps1`, assert that the Python module exposes a `build_database_url(environ)` function, assert that representative environment settings produce the expected host/port/database/user, and assert that neither source contains an inline password argument or a fixed `db_password` assignment.

- [ ] **Step 2: Run the tests and confirm the expected failure**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/security/test_repository_safety.py -q
```

Expected: FAIL because `build_database_url` does not exist and the two current sources still embed credentials.

### Task 3: Environment-Based MySQL Configuration

**Files:**
- Modify: `ecs_demo/actions/db.py`
- Modify: `start_customer_service.ps1`
- Test: `tests/security/test_repository_safety.py`

- [ ] **Step 1: Implement the pure database URL builder**

In `ecs_demo/actions/db.py`, load `ecs_demo/.env` with `load_dotenv`, then implement:

```python
def build_database_url(environ: Mapping[str, str] | None = None) -> URL:
    values = os.environ if environ is None else environ
    password = values.get("MYSQL_PASSWORD")
    if not password:
        raise RuntimeError("MYSQL_PASSWORD is required")
    return URL.create(
        "mysql+pymysql",
        username=values.get("MYSQL_USER", "root"),
        password=password,
        host=values.get("MYSQL_HOST", "127.0.0.1"),
        port=int(values.get("MYSQL_PORT", "3306")),
        database=values.get("MYSQL_DATABASE", "ecs"),
        query={"charset": "utf8mb4"},
    )
```

Use the returned `URL` to create the existing engine and preserve `SessionLocal` as the public compatibility seam.

- [ ] **Step 2: Remove the duplicate credential from the startup script**

Change `Get-OrderCount` so its Python snippet imports `SessionLocal` from `actions.db`, executes `select count(*) from order_info` through SQLAlchemy, and closes the session in `finally`. The script must not pass or print a password.

- [ ] **Step 3: Run the focused tests**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/security/test_repository_safety.py -q
```

Expected: all repository-safety tests pass.

- [ ] **Step 4: Verify imports without connecting to MySQL**

Run from `ecs_demo`:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -c "from actions.db import build_database_url; print(build_database_url().drivername)"
```

Expected: prints `mysql+pymysql` and does not print credentials.

### Task 4: Baseline Verification and Commit

**Files:**
- Verify: all tracked source and documentation files

- [ ] **Step 1: Run the current automated suite**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest -q
```

Expected: all collected tests pass; the exact count becomes the first measured baseline.

- [ ] **Step 2: Stage files and inspect the candidate baseline**

Run:

```powershell
git add .
git status --short
git diff --cached --check
```

Expected: no `.env`, database volume, model weight, runtime log, Tracker data, certificate, cache, or generated report is staged; whitespace check exits successfully.

- [ ] **Step 3: Scan staged file names and content for secret hazards**

Run a repository safety check that reports only file names and rule identifiers, never matching values. Any hit in a tracked source file blocks the commit until reviewed.

- [ ] **Step 4: Create the baseline commit**

Run:

```powershell
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "chore: establish secure project baseline"
```

Expected: one root commit containing the sanitized course baseline, requirements/design documents, Harness specification, implementation plan, and initial security tests.

- [ ] **Step 5: Record the clean baseline**

Run:

```powershell
git status --short --branch
git log -1 --oneline
```

Expected: clean working tree on the initial branch and one baseline commit.
