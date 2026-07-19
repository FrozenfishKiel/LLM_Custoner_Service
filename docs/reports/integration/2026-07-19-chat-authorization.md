# Chat Authorization Verification Report

Date: 2026-07-19

## Scope

This slice adds production-authenticated chat endpoints:

- `POST /api/chat/messages`
- `POST /api/chat/reset`

The routes derive account and business-user identity from server-side `auth_session` plus `account_user_binding`; request body and metadata identity fields are not trusted. The legacy `/api/messages` route remains demo-compatible and is not treated as the production-authenticated route.

## Commits

- `1bd6450 docs: plan chat authorization slice`
- `063ccb2 test: define chat authorization contract`
- `dd2772d feat: add authenticated chat routes`
- `bf584ae test: cover chat authorization`

Task 4 also removes credential-shaped fake outage strings from chat unit tests so secret scans do not need fixture exceptions.

## Verification Evidence

| Gate | Evidence | Result |
| --- | --- | --- |
| Targeted unit | `evidence/chat-authorization-unit-targeted.txt` | `21 passed, 24 warnings` |
| Real HTTP integration | `evidence/chat-authorization-integration.txt` | `7 passed` |
| Auth/API/security regression | `evidence/chat-authorization-regression.txt` | `256 passed, 33 warnings` |
| Full suite | `evidence/chat-authorization-full-suite.txt` | `324 passed, 33 warnings` |
| Compileall | `evidence/chat-authorization-compileall.txt` | exit 0 |
| Whitespace check | `evidence/chat-authorization-diff-check.txt` | exit 0 |
| Redis cleanup | `evidence/chat-authorization-redis-db15.txt` | DB 15 size `0` |
| MySQL cleanup | `evidence/chat-authorization-mysql-temp-dbs.txt` | `[]` |
| Scoped secret scan | `evidence/chat-authorization-secret-scan.txt` | No scoped secret findings |

Warnings are current FastAPI/Starlette TestClient/httpx deprecations from unit tests using `TestClient`; they are not chat authorization behavior failures. The integration test uses `httpx.ASGITransport` directly and runs without pytest warnings.

## Security Behaviors Covered

- Missing or invalid session returns 401 before body validation.
- CSRF is required for both message and reset routes and is checked before business binding mutation.
- Pending or disabled account returns 403.
- Active account without `account_user_binding` returns 409.
- Agent tracker key is `account:{account_id}`.
- Trusted metadata contains server-derived `account_id`, `user_id`, `account_role`, and `account_status`.
- Client-supplied identity fields are recursively sanitized case-insensitively from metadata.
- Redis session outage maps to sanitized 503.
- Real SQLAlchemy/MySQL binding resolver outage maps to sanitized 503.
- Reset only targets the authenticated account tracker.

## Reviews

- Task 3 spec review initially found that the binding outage test used a fake repository instead of a real SQLAlchemy/MySQL failure path.
- The integration test was corrected to use a real SQLAlchemy engine pointed at a deliberately missing MySQL schema while auth setup still uses the migrated temp DB.
- Task 3 spec re-review: APPROVED.
- Task 3 quality re-review: APPROVED.
- Final independent QA: see `evidence/chat-authorization-independent-qa.md`.

## Remaining Engineering Work Before Launch

Excluding user-owned external prerequisites such as cloud purchase, domain/ICP, production SMTP account, production DeepSeek key, and final legal/privacy approval, the remaining engineering work is:

1. Demo-data initialization and account deletion lifecycle.
2. Action-level ownership hardening, write transactions, idempotency, and audit events.
3. Redis TrackerStore migration and production disabling of `switch_user_id` flows.
4. Rate limiting for auth, email, and chat paths.
5. Production customer UI and minimal admin UI.
6. Structured logs, Prometheus metrics, Grafana dashboards, and alert rules.
7. Docker Compose/Nginx/HTTPS production packaging, safe config templates, and release/rollback scripts.
8. Browser E2E for registration, login, chat, ownership isolation, reset, and admin flows.
9. LLM evaluation set for ecommerce intent, boundary refusal, and flow correctness.
10. Load/stress tests with quantitative P50/P95/error-rate outputs.
11. Backup/restore drills for MySQL and Neo4j.
12. Security/dependency scan and warning cleanup, including current TestClient/httpx deprecation warnings.
13. Final release evidence bundle and smoke test after deployment.

Rough distance: this chat-authorization slice removes one major security blocker, but the project is still several engineering slices away from launch. Backend auth is now substantially stronger; production readiness still depends mostly on business-action isolation, UI, observability, deployment, and end-to-end/LLM/load validation.
