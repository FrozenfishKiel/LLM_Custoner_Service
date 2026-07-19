# Chat Authorization Design

## Goal

Build a complete authenticated chat boundary so customer-service conversations run only for the logged-in account's trusted business user, never for user identity supplied by the browser, LLM, or request body.

## Problems to Solve

1. Existing `POST /api/messages` accepts `sender` from the request body and uses it as the Agent tracker key.
2. Existing chat/session reset endpoints can read or reset arbitrary tracker IDs.
3. The system has `account_user_binding`, but no runtime resolver that maps a logged-in account to the trusted course-business `user_id`.
4. Chat metadata can currently carry identity-like fields from the client.
5. Docker/MySQL test harness recently proved fragile when containers are started without an init process or readiness checks.

## Chosen Approach

Add a dedicated authenticated chat route layer instead of mutating the legacy demo route in place.

- Keep legacy `/api/messages`, `/api/sessions/{session_id}`, and inspect/debug routes for course/demo compatibility.
- Add `/api/chat/messages` and `/api/chat/reset` as production-style authenticated endpoints.
- Resolve identity from `auth_session` using the existing `AuthRouteDependencies`.
- Resolve the business user through a new auth-owned binding resolver.
- Generate tracker IDs only on the server: `account:{account_id}`.
- Sanitize client metadata by rejecting/removing identity fields, then inject trusted `account_id`, `user_id`, role, and account status.
- Require CSRF on `POST /api/chat/reset`; `POST /api/chat/messages` is also state-changing because it mutates conversation state, so it requires CSRF too.
- Return 401 for missing/invalid session, 403 for non-active accounts, 409 for active accounts without a business-user binding, and 503 for dependency outages.

## Components

### Business Identity Resolver

Create an auth module component that reads `account_user_binding`:

```python
@dataclass(frozen=True)
class BusinessUserIdentity:
    account_id: str
    user_id: str
    role: AccountRole
    account_status: AccountStatus

class BusinessUserBindingUnavailable(RuntimeError): ...
class BusinessUserNotBound(RuntimeError): ...
```

The resolver should:

- lock nothing for read-only chat routing;
- return a trusted binding only for the authenticated account;
- fail closed if the account is missing, disabled, pending, or the binding is missing;
- expose sanitized, stable errors to HTTP routes.

### Authenticated Chat Router

Create `atguigu_ai/api/routes/chat.py` with:

- `POST /api/chat/messages`
- `POST /api/chat/reset`

Request body for messages:

```json
{
  "message": "我的订单到哪里了？",
  "metadata": {}
}
```

The route must ignore or reject client-supplied `sender`, `sender_id`, `session_id`, `account_id`, `user_id`, `role`, and `account_status`.

The message route:

1. Requires CSRF.
2. Resolves authenticated account from session cookie.
3. Resolves trusted business user binding.
4. Calls `agent.handle_message(message, sender_id="account:{account_id}", metadata=trusted_metadata)`.
5. Returns existing message response shape with `recipient_id="account:{account_id}"`.

The reset route:

1. Requires CSRF.
2. Resolves authenticated account and business binding.
3. Calls `agent.reset_tracker("account:{account_id}")`.
4. Returns `204`.

### App Composition

Extend `create_app(...)` with optional chat dependencies. Chat routes mount only when both:

- `auth_deps` is present;
- `chat_deps` or `agent` plus a binding resolver are present.

This keeps old demo routes untouched while allowing production-style tests to instantiate only the authenticated chat surface.

### Harness Stability

For this slice, add a small documented helper or test fixture update that:

- waits for Redis before using it;
- documents that MySQL containers should be recreated with `--init`;
- avoids parallel integration use of shared named Redis/MySQL containers.

Do not rely on Docker restarts as a normal workflow.

## Testing Requirements

Every security behavior needs a corresponding test:

- Missing session on chat message returns 401.
- Invalid session on chat message returns 401.
- Pending/disabled account identity returns 403.
- Active account without business binding returns 409.
- Client-supplied `sender`, `sender_id`, `session_id`, `account_id`, or `user_id` cannot affect tracker key or metadata.
- Successful message uses tracker key `account:{account_id}` and injects trusted `user_id`.
- Reset requires CSRF and resets only the authenticated account tracker.
- Cross-account requests cannot read/reset another account tracker.
- Redis session outage maps to sanitized 503.
- MySQL binding resolver outage maps to sanitized 503.
- Integration test uses real FastAPI, MySQL account/binding rows, Redis session, and fake Agent.
- Evidence must include unit, integration, regression, full suite, compileall, whitespace, secret scan, Redis/MySQL cleanup, and independent QA.

## Out of Scope

- Browser UI pages.
- Admin account management.
- Account deletion.
- Production rate limiting implementation, except documenting the later hook.
- Rewriting all Agent actions; this slice injects trusted identity and tests the HTTP/Agent boundary. Deeper per-action ownership hardening remains a follow-up unless tests prove an action currently bypasses `user_id`.
