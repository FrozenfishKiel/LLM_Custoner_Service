# Chat Authorization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add authenticated chat routes that derive account and business-user identity from server-side auth state, not from client request bodies.

**Architecture:** Add a small auth-owned business binding resolver, a dedicated authenticated chat router, and optional app composition wiring. Keep legacy demo routes intact. Tests drive each security behavior before implementation.

**Tech Stack:** Python 3.12, FastAPI, httpx ASGI transport, Pytest, SQLAlchemy/MySQL, Redis session store, existing Agent interface.

---

## Locked Contract

- `POST /api/chat/messages` and `POST /api/chat/reset` require `auth_session`.
- Both chat routes require matching `auth_csrf` cookie and `X-CSRF-Token` header.
- Route code must never trust client `sender`, `sender_id`, `session_id`, `account_id`, `user_id`, `role`, or `account_status`.
- Tracker key is `account:{account_id}`.
- Trusted metadata injected into Agent contains:
  - `account_id`
  - `user_id`
  - `account_role`
  - `account_status`
- Missing/invalid session returns 401.
- Pending/disabled account returns 403.
- Active account without business binding returns 409.
- Auth/session/binding dependency outage returns sanitized 503.
- Legacy `/api/messages` remains available for demo compatibility and is not considered production-authenticated.

## File Map

- Modify `atguigu_ai/auth/account_repository.py`: add business binding read method and result dataclass if repository-local.
- Create `atguigu_ai/auth/business_identity.py`: public resolver, dataclasses, and sanitized exceptions.
- Modify `atguigu_ai/auth/__init__.py`: export resolver types.
- Create `atguigu_ai/api/routes/chat.py`: authenticated chat routes and request models.
- Modify `atguigu_ai/api/server.py`: optional chat route mounting.
- Modify `atguigu_ai/api/__init__.py`: export chat dependency bundle if needed.
- Create `tests/unit/auth/test_business_identity.py`: resolver unit tests.
- Create `tests/unit/api/test_chat_routes.py`: fake session/binding/agent route tests.
- Create `tests/integration/test_chat_authorization_http.py`: real FastAPI + MySQL + Redis route integration.
- Modify `docs/TECHNICAL_DESIGN.md`: document chat authorization is now implemented.
- Create final report/evidence under `docs/reports/integration/`.

## Task 1: RED Contracts for Business Identity and Chat Routes

**Files:**
- Create `tests/unit/auth/test_business_identity.py`
- Create `tests/unit/api/test_chat_routes.py`

- [ ] **Step 1: Write resolver tests**

Cover:

```python
async def test_resolver_returns_bound_active_business_user(...)
async def test_resolver_rejects_missing_binding(...)
async def test_resolver_rejects_pending_or_disabled_account(...)
async def test_resolver_maps_repository_outage_to_unavailable(...)
```

- [ ] **Step 2: Write chat route tests**

Cover:

```python
def test_chat_message_requires_session()
def test_chat_message_requires_csrf()
def test_chat_message_uses_server_tracker_and_trusted_metadata()
def test_chat_message_ignores_or_rejects_client_identity_fields()
def test_chat_message_returns_409_when_account_has_no_business_binding()
def test_chat_reset_requires_csrf_and_resets_only_authenticated_tracker()
def test_dependency_outage_returns_sanitized_503()
```

- [ ] **Step 3: Run RED**

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth/test_business_identity.py tests/unit/api/test_chat_routes.py -q
```

Expected: collection or test failures because resolver and router do not exist.

- [ ] **Step 4: Commit RED**

```powershell
git add tests/unit/auth/test_business_identity.py tests/unit/api/test_chat_routes.py
git diff --cached --check
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "test: define chat authorization contract"
```

## Task 2: GREEN Business Identity Resolver and Chat Router

**Files:**
- Modify `atguigu_ai/auth/account_repository.py`
- Create `atguigu_ai/auth/business_identity.py`
- Modify `atguigu_ai/auth/__init__.py`
- Create `atguigu_ai/api/routes/chat.py`
- Modify `atguigu_ai/api/server.py`
- Modify `atguigu_ai/api/__init__.py`
- Modify unit tests as needed only to align names with implementation.

- [ ] **Step 1: Implement binding resolver**

Add repository read for `account_user_binding` joined/validated with `account`.

Implement `BusinessIdentityResolver.resolve(account_identity)`:

- active account only;
- binding required;
- returns `BusinessUserIdentity`;
- maps SQLAlchemy/repository failures to `BusinessUserBindingUnavailable`;
- does not expose raw DB errors.

- [ ] **Step 2: Implement chat router**

Create route dependency dataclass:

```python
@dataclass(frozen=True)
class ChatRouteDependencies:
    agent: Agent
    business_identity_resolver: BusinessIdentityResolver
```

Route behavior:

- call `require_csrf(request, auth_deps)`;
- call `resolve_authenticated_identity(request, auth_deps)`;
- resolve business identity;
- sanitize client metadata by removing/rejecting identity keys;
- call Agent with `sender_id=f"account:{account_id}"`;
- return existing message response objects.

- [ ] **Step 3: Run GREEN**

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth/test_business_identity.py tests/unit/api/test_chat_routes.py -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth tests/unit/api tests/security -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m compileall -q atguigu_ai/auth atguigu_ai/api tests/unit/auth tests/unit/api
git diff --check
```

- [ ] **Step 4: Commit GREEN**

```powershell
git add atguigu_ai/auth/account_repository.py atguigu_ai/auth/business_identity.py atguigu_ai/auth/__init__.py atguigu_ai/api/routes/chat.py atguigu_ai/api/server.py atguigu_ai/api/__init__.py tests/unit/auth/test_business_identity.py tests/unit/api/test_chat_routes.py
git diff --cached --check
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "feat: add authenticated chat routes"
```

## Task 3: Real HTTP Integration and Harness Hardening

**Files:**
- Create `tests/integration/test_chat_authorization_http.py`
- Modify `docs/TECHNICAL_DESIGN.md`
- Optionally modify existing test harness helpers only if needed for deterministic readiness.

- [ ] **Step 1: Write integration tests**

Use real MySQL account/binding rows and Redis sessions. Use `httpx.AsyncClient` with ASGI transport.

Cover:

- logged-in bound account can send chat;
- Agent receives `sender_id="account:{account_id}"`;
- Agent metadata contains trusted `user_id` and not client-forged identity;
- missing CSRF rejects before Agent call;
- missing session 401;
- missing binding 409;
- disabled account 403;
- `/api/chat/reset` resets only authenticated tracker;
- Redis outage maps to sanitized 503;
- MySQL resolver outage maps to sanitized 503;
- Redis DB15 and MySQL temp DB cleanup are zero in finalizer/post-check.

- [ ] **Step 2: Run integration and cleanup**

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_chat_authorization_http.py -q -s -m integration
docker exec llm-cs-redis redis-cli -n 15 DBSIZE
```

- [ ] **Step 3: Update technical design**

Document:

- chat authorization routes implemented;
- trusted identity chain;
- tracker key format;
- request identity fields ignored/rejected;
- legacy demo route remains non-production.

- [ ] **Step 4: Commit integration**

```powershell
git add tests/integration/test_chat_authorization_http.py docs/TECHNICAL_DESIGN.md
git diff --cached --check
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "test: cover chat authorization"
```

## Task 4: Evidence, Independent QA, and Final Gate

**Files:**
- Create `docs/reports/integration/2026-07-19-chat-authorization.md`
- Create evidence files under `docs/reports/integration/evidence/chat-authorization-*.txt`
- Create `docs/reports/integration/evidence/chat-authorization-independent-qa.md`

- [ ] **Step 1: Capture verification evidence**

Run and save:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth/test_business_identity.py tests/unit/api/test_chat_routes.py -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_chat_authorization_http.py -q -s -m integration
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth tests/unit/api tests/unit/email tests/security -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m compileall -q atguigu_ai/auth atguigu_ai/api tests/unit/auth tests/unit/api tests/unit/email tests/integration
git diff --check
docker exec llm-cs-redis redis-cli -n 15 DBSIZE
```

- [ ] **Step 2: Run scoped secret scan**

Scan chat auth source/tests/docs/evidence for private keys, real API tokens, credential-bearing DB URLs, SMTP passwords, raw session/CSRF/token leaks.

- [ ] **Step 3: Independent QA**

QA must rerun targeted unit, integration, regression, full suite, compileall, whitespace, cleanup, evidence UTF-8, and secret scan.

- [ ] **Step 4: Write report and final commit**

```powershell
git add docs/superpowers/specs/2026-07-19-chat-auth-design.md docs/superpowers/plans/2026-07-19-chat-authorization.md docs/reports/integration/2026-07-19-chat-authorization.md docs/reports/integration/evidence/chat-authorization-*.txt docs/reports/integration/evidence/chat-authorization-independent-qa.md
git diff --cached --check
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "docs: record chat authorization verification"
git status --short
```

## Completion Gate

This slice is complete only when authenticated chat routes use trusted session and business binding identity end-to-end, forged request identity cannot influence tracker or metadata, reset is scoped to the authenticated account, real MySQL/Redis integration passes, cleanup checks are zero, independent QA is APPROVED, and the final worktree is clean.
