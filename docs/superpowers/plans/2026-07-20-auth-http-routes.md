# Authentication HTTP Routes, Cookies, and CSRF Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the backend authentication service through FastAPI routes with session cookies, CSRF protection, and authenticated account lookup so registered users can actually log in and use the API through the web entrypoint.

**Architecture:** `atguigu_ai.auth.service.AuthService` remains the business boundary for registration, verification, login, logout, password reset, resend-verification, and password change. `atguigu_ai.api.dependencies` owns app-level wiring, cookie policy, and CSRF helpers, while `atguigu_ai.api.routes.auth` owns HTTP request/response translation only. `atguigu_ai.api.server` becomes the composition root that mounts the auth router without reimplementing auth rules in the server or handlers.

**Tech Stack:** Python 3.12, FastAPI, Starlette TestClient, Pytest, Pytest-asyncio, existing SQLAlchemy/MySQL auth repository, Redis session/token stores, and the existing `ai-content-ops` environment.

---

## File Map

- Create `atguigu_ai/api/dependencies.py`: auth route dependency bundle, cookie policy helpers, CSRF helpers, and authenticated session resolution.
- Create `atguigu_ai/api/routes/__init__.py`: route package exports.
- Create `atguigu_ai/api/routes/auth.py`: FastAPI auth router, request/response models, cookie setting, CSRF enforcement, and `GET /api/account/me`.
- Modify `atguigu_ai/api/server.py`: mount the auth router through an optional dependency bundle while preserving the existing demo server behavior.
- Modify `atguigu_ai/api/__init__.py`: export the new auth dependency bundle if needed by tests/runtime composition.
- Modify `atguigu_ai/auth/service.py`: add `resend_verification(email)` and `change_password(account_id, current_password, new_password)` service methods with unit-of-work semantics.
- Modify `tests/unit/auth/test_auth_service.py`: extend unit coverage for resend-verification and change-password.
- Create `tests/unit/api/test_auth_routes.py`: route contract tests for cookies, CSRF, response shapes, and status codes with fakes.
- Create `tests/integration/test_auth_routes_http.py`: real FastAPI + MySQL + Redis authentication route integration using the owned harness.
- Modify `docs/TECHNICAL_DESIGN.md`: record the HTTP auth route layer, cookie names/policy, CSRF behavior, and the fact that account deletion/browser pages remain later slices.
- Create `docs/reports/integration/2026-07-20-auth-http-routes.md` and evidence files under `docs/reports/integration/evidence/auth-http-routes-*.txt`.

## Locked Contracts

This slice does not add browser pages, account deletion, demo-data initialization, admin pages, or chat authorization. It does add the HTTP routes that make the already-implemented AuthService reachable through the application entrypoint.

Cookie and CSRF policy:

- Session cookie name: `auth_session`
- CSRF cookie name: `auth_csrf`
- CSRF header name: `X-CSRF-Token`
- Session cookie: `HttpOnly`, `Secure` in HTTPS mode, `SameSite=Lax`
- CSRF cookie: readable by browser JavaScript, `Secure` in HTTPS mode, `SameSite=Lax`
- State-changing routes reject requests when the CSRF header and CSRF cookie do not match exactly
- Invalid or missing session cookie resolves to 401 for authenticated reads and 204/no-op for logout

HTTP route behavior:

- `POST /api/auth/register` registers a pending consumer and returns a generic accepted response without cookies.
- `POST /api/auth/verify-email` consumes the verification token and returns a generic verification result.
- `POST /api/auth/resend-verification` resends a verification email for pending accounts and stays enumeration-safe for missing/active/disabled accounts.
- `POST /api/auth/login` sets the session cookie and CSRF cookie on success and returns the signed-in identity.
- `POST /api/auth/logout` revokes the current session, clears both cookies, and is idempotent.
- `POST /api/auth/forgot-password` stays enumeration-safe and only sends email for active accounts.
- `POST /api/auth/reset-password` consumes the reset token and returns a generic accepted result or validation error as appropriate.
- `POST /api/auth/change-password` requires the current session plus CSRF, verifies the current password, updates the password, revokes sessions, and clears cookies after success because the current session is invalidated.
- `GET /api/account/me` returns the authenticated account identity from the session cookie.

### Task 1: HTTP Route Contracts and Service Extension RED

**Files:**
- Modify: `tests/unit/auth/test_auth_service.py`
- Create: `tests/unit/api/test_auth_routes.py`
- Modify: `atguigu_ai/auth/service.py` only after tests are written

- [ ] **Step 1: Write the failing service-extension and route-contract tests**

Add the following new behavior tests to `tests/unit/auth/test_auth_service.py`:

```python
@pytest.mark.asyncio
async def test_resend_verification_sends_email_only_for_pending_account(fixture):
    pending = account(status=AccountStatus.pending)
    fixture.repository.by_email[pending.email_normalized] = pending

    result = await fixture.service.resend_verification("User@example.com")

    assert result.accepted is True
    assert fixture.tokens.issued == [(pending.account_id, CredentialTokenPurpose.verify_email)]
    assert fixture.email.verifications == [
        (pending.email, "https://public.example/app/verify-email?token=vvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvv")
    ]


@pytest.mark.asyncio
async def test_change_password_verifies_current_password_revokes_sessions_and_updates_hash(fixture):
    active = account(status=AccountStatus.active, verified_at=NOW, password_hash="hash-old")
    fixture.repository.by_id[active.account_id] = active
    fixture.hasher.valid_hash_passwords["hash-old"] = "old correct horse"

    await fixture.service.change_password(active.account_id, "old correct horse", "new correct horse")

    assert fixture.hasher.verifications == [("hash-old", "old correct horse")]
    assert fixture.sessions.revoked_all == [active.account_id]
    assert fixture.repository.events[-2:] == [
        ("replace_password_hash", active.account_id, "hash-new correct horse"),
        ("record_audit", "auth-service", active.account_id, AccountRole.consumer, "account.password_changed", "account", active.account_id, AuditResult.success, None),
    ]
```

Create `tests/unit/api/test_auth_routes.py` with a fake auth service and fake session store. Cover:

```python
def test_login_sets_session_and_csrf_cookies_and_returns_identity():
    response = client.post("/api/auth/login", json={"email": "User@example.com", "password": "old correct horse"})
    assert response.status_code == 200
    assert response.json()["account_id"] == "account-1"
    assert "auth_session=" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=Lax" in response.headers["set-cookie"]
    assert "auth_csrf=" in response.headers["set-cookie"]


def test_logout_requires_csrf_and_clears_cookies():
    response = client.post("/api/auth/logout")
    assert response.status_code == 403


def test_account_me_reads_identity_from_session_cookie():
    response = client.get("/api/account/me", cookies={"auth_session": "session-token"})
    assert response.status_code == 200
    assert response.json()["account_id"] == "account-1"
```

The fake route test module should also cover:

- register response is generic and does not set cookies;
- verify-email and forgot/reset stay enumeration-safe;
- change-password requires a matching CSRF header;
- invalid/missing session returns 401 for `GET /api/account/me`;
- login failure does not set cookies;
- logout clears both cookies.

- [ ] **Step 2: Run RED**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth/test_auth_service.py tests/unit/api/test_auth_routes.py -q
```

Expected: collection fails because the new route module and service methods do not exist yet.

- [ ] **Step 3: Commit the RED contract**

```powershell
git add tests/unit/auth/test_auth_service.py tests/unit/api/test_auth_routes.py
git diff --cached --check
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "test: define auth http route contract"
```

Expected: only the new tests and the AuthService extension coverage are committed.

### Task 2: Auth Service Extension and HTTP Router GREEN

**Files:**
- Modify: `atguigu_ai/auth/service.py`
- Create: `atguigu_ai/api/dependencies.py`
- Create: `atguigu_ai/api/routes/__init__.py`
- Create: `atguigu_ai/api/routes/auth.py`
- Modify: `atguigu_ai/api/server.py`
- Modify: `atguigu_ai/api/__init__.py`
- Modify: `tests/unit/auth/test_auth_service.py`

- [ ] **Step 1: Implement the minimal service extension**

Add two methods to `AuthService`:

```python
async def resend_verification(self, email: str) -> PasswordResetAccepted: ...
async def change_password(self, account_id: str, current_password: str, new_password: str) -> None: ...
```

Required behavior:

- `resend_verification` normalizes email, returns accepted for missing/active/disabled accounts, and only issues a verification token + sends verification email for pending accounts.
- `change_password` locks the account row, verifies the current password, hashes the new password, revokes all sessions, updates the hash, records audit, and commits.
- `InvalidPassword` from the new password validation must propagate as a user input error, while dependency outages map to `AuthServiceUnavailable`.
- Bad current password raises `InvalidCredentials`.

Use the existing repository/unit-of-work and existing `PasswordHasher`, `RedisCredentialTokenStore`, `RedisSessionStore`, and `EmailDeliveryUnavailable` behavior.

- [ ] **Step 2: Implement route dependencies and router**

Create `atguigu_ai/api/dependencies.py` with:

```python
@dataclass(frozen=True)
class AuthRouteDependencies:
    service: AuthService
    sessions: RedisSessionStore
    session_cookie_name: str = "auth_session"
    csrf_cookie_name: str = "auth_csrf"
    csrf_header_name: str = "X-CSRF-Token"
    cookie_secure: bool = True
```

And helpers:

- `issue_auth_cookies(response, session, csrf_token, secure)`
- `clear_auth_cookies(response, secure)`
- `require_csrf(request, deps)`
- `resolve_authenticated_identity(request, deps) -> AccountIdentity | None`

Create `atguigu_ai/api/routes/auth.py` with an `APIRouter` exposing the routes above. Use Pydantic request/response models in that module. Keep route handlers thin; they should only validate request payloads, call `AuthService`, set/clear cookies, and translate errors to HTTP status codes.

Modify `atguigu_ai/api/server.py` so `create_app()` accepts an optional `auth_deps: AuthRouteDependencies | None`, mounts the auth router when provided, and keeps existing demo routes untouched.

- [ ] **Step 3: Run GREEN**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth/test_auth_service.py tests/unit/api/test_auth_routes.py -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth tests/security -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m compileall -q atguigu_ai/auth atguigu_ai/api tests/unit/auth tests/unit/api
git diff --check
```

Expected: all tests pass; cookie/CSRF helpers are exercised by unit tests; existing auth/security regression stays green.

- [ ] **Step 4: Commit the router slice**

```powershell
git add atguigu_ai/auth/service.py atguigu_ai/api/dependencies.py atguigu_ai/api/routes/__init__.py atguigu_ai/api/routes/auth.py atguigu_ai/api/server.py atguigu_ai/api/__init__.py tests/unit/auth/test_auth_service.py tests/unit/api/test_auth_routes.py
git diff --cached --check
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "feat: add auth http routes"
```

### Task 3: Real HTTP Integration Against MySQL and Redis

**Files:**
- Create: `tests/integration/test_auth_routes_http.py`

- [ ] **Step 1: Write the integration tests**

Use the same isolated MySQL database helper pattern and owned Redis DB 15 harness used by the auth service integration tests. Build a FastAPI app with `create_app(auth_deps=...)` and a `TestClient(base_url="https://testserver")`.

Cover:

- register sets no cookies, returns accepted, and persists a pending account;
- resend-verification only sends for pending accounts;
- login sets secure session + CSRF cookies and returns identity;
- `GET /api/account/me` succeeds with the session cookie;
- `POST /api/auth/logout` requires CSRF and clears cookies;
- `POST /api/auth/change-password` requires CSRF, invalidates old session cookies, and requires the new password afterward;
- `forgot-password` and `reset-password` stay enumeration-safe;
- verify-email consumes token once and does not auto-login;
- Redis outage maps to sanitized 503-style service failures;
- `auth_session` and `auth_csrf` cookies use `HttpOnly` / `Secure` / `SameSite=Lax` as appropriate;
- the owned Redis DB 15 and temporary MySQL databases are empty after cleanup.

Include at least one negative test that omits the CSRF header and proves the route is rejected before service mutation.

- [ ] **Step 2: Run the integration test and cleanup checks**

Run:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_auth_routes_http.py -q -s -m integration
docker exec llm-cs-redis redis-cli -n 15 DBSIZE
```

Expected: integration passes, Redis DB15 is `0`.

- [ ] **Step 3: Update technical design**

Add a short subsection to `docs/TECHNICAL_DESIGN.md` under the Auth module or HTTP interface describing:

- the new auth router endpoints;
- the cookie names and CSRF double-submit rule;
- that session cookie is HttpOnly / Secure / SameSite=Lax;
- that change-password and logout clear or invalidate current session state;
- that account deletion and browser pages remain later slices.

- [ ] **Step 4: Commit the integration slice**

```powershell
git add tests/integration/test_auth_routes_http.py docs/TECHNICAL_DESIGN.md
git diff --cached --check
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "test: cover auth http routes"
```

### Task 4: Evidence, Independent QA, and Final Gate

**Files:**
- Create: `docs/reports/integration/2026-07-20-auth-http-routes.md`
- Create: `docs/reports/integration/evidence/auth-http-routes-unit.txt`
- Create: `docs/reports/integration/evidence/auth-http-routes-integration.txt`
- Create: `docs/reports/integration/evidence/auth-http-routes-regression.txt`
- Create: `docs/reports/integration/evidence/auth-http-routes-full-suite.txt`
- Create: `docs/reports/integration/evidence/auth-http-routes-compileall.txt`
- Create: `docs/reports/integration/evidence/auth-http-routes-whitespace.txt`
- Create: `docs/reports/integration/evidence/auth-http-routes-secret-scan.txt`
- Create: `docs/reports/integration/evidence/auth-http-routes-dbsize.txt`
- Create: `docs/reports/integration/evidence/auth-http-routes-independent-qa.md`

- [ ] **Step 1: Capture the final verification commands**

Run and retain UTF-8 evidence:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth/test_auth_service.py tests/unit/api/test_auth_routes.py -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_auth_routes_http.py -q -s -m integration
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth tests/unit/api tests/unit/email tests/security -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests -q
D:\Anaconda3\envs\ai-content-ops\python.exe -m compileall -q atguigu_ai/auth atguigu_ai/api tests/unit/auth tests/unit/api tests/unit/email tests/integration
git diff --check
docker exec llm-cs-redis redis-cli -n 15 DBSIZE
```

Expected: all tests and checks pass; Redis DB15 is `0`.

- [ ] **Step 2: Run a scoped secret scan**

Scan the planned HTTP/auth artifacts for private keys, credential-bearing Redis/MySQL URLs, SMTP passwords, raw tokens, session IDs, CSRF secrets, and `sk-*`/cloud key shapes. Deterministic test probe strings are allowed only when they are clearly fake and documented.

- [ ] **Step 3: Independent QA review**

Dispatch/reuse a QA agent to rerun the targeted unit tests, integration route tests, regression suite, full suite, compileall, Redis/MySQL cleanup, evidence UTF-8 checks, and secret scan. Require no open Critical/Important findings.

- [ ] **Step 4: Write the report and close the loop**

Write `docs/reports/integration/2026-07-20-auth-http-routes.md` summarizing command results, cookie/CSRF behavior, route coverage, and residual risks. Mark all checkboxes complete only after the review gate passes.

- [ ] **Step 5: Final commit**

```powershell
git add atguigu_ai/auth/service.py atguigu_ai/api/dependencies.py atguigu_ai/api/routes/__init__.py atguigu_ai/api/routes/auth.py atguigu_ai/api/server.py atguigu_ai/api/__init__.py tests/unit/auth/test_auth_service.py tests/unit/api/test_auth_routes.py tests/integration/test_auth_routes_http.py docs/TECHNICAL_DESIGN.md docs/superpowers/plans/2026-07-20-auth-http-routes.md docs/reports/integration/2026-07-20-auth-http-routes.md docs/reports/integration/evidence/auth-http-routes-unit.txt docs/reports/integration/evidence/auth-http-routes-integration.txt docs/reports/integration/evidence/auth-http-routes-regression.txt docs/reports/integration/evidence/auth-http-routes-full-suite.txt docs/reports/integration/evidence/auth-http-routes-compileall.txt docs/reports/integration/evidence/auth-http-routes-whitespace.txt docs/reports/integration/evidence/auth-http-routes-secret-scan.txt docs/reports/integration/evidence/auth-http-routes-dbsize.txt docs/reports/integration/evidence/auth-http-routes-independent-qa.md
git diff --cached --check
git -c user.name=Codex -c user.email=codex@local.invalid commit -m "docs: record auth http route verification"
git show --stat --oneline HEAD
git status --short
```

## Completion Gate

This slice is complete only when the FastAPI auth routes are wired to the real AuthService, session cookies and CSRF protection work end-to-end, the HTTP account lookup works from session state, the new unit/integration/regression/full-suite evidence is retained, Redis DB15 and MySQL temporary databases are clean, the technical design documents the cookie/CSRF contract, the independent QA report is APPROVED, and the final commit leaves a clean worktree. Account deletion, demo-data initialization, browser pages, and chat authorization remain later slices.
