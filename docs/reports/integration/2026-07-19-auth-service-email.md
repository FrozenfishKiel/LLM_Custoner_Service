# Authentication Service and Email Delivery Integration Report

## Scope

- Slice: backend authentication service and email delivery adapter.
- Implemented modules: `atguigu_ai.email.delivery`, `atguigu_ai.auth.account_repository`, `atguigu_ai.auth.service`.
- Integration target: real MySQL account tables in isolated `llm_cs_test_<uuid>` databases plus owned Redis DB 15.
- Extra environment repair: the local `llm-cs-mysql` container was deleted and recreated after user approval because Docker `start` and `logs` hung on the old exited container. The replacement uses `mysql:8.0`, database `ecs`, the existing initialization-script bind, and a fresh data bind at `docker-data/mysql-data-auth-service-20260719`.

## Acceptance Evidence

| Check | Result | Evidence |
| --- | --- | --- |
| Unit service/repository/email contracts | 54 passed | `evidence/auth-service-email-unit.txt` |
| Real MySQL + Redis AuthService integration | 11 passed | `evidence/auth-service-email-integration.txt` |
| Auth/email/security regression | 215 passed | `evidence/auth-service-email-regression.txt` |
| Full repository suite | 268 passed, 0 failed, 49.80 seconds | `evidence/auth-service-email-full-suite.txt` |
| Compile changed Python areas | exit 0 | `evidence/auth-service-email-compileall.txt` |
| Whitespace check | `git diff --check` exit 0 | `evidence/auth-service-email-whitespace.txt` |
| Redis/MySQL cleanup | Redis DB15 `0`; no `llm_cs_test_%` databases | `evidence/auth-service-email-dbsize.txt` |
| Course data after MySQL rebuild | 10 users, 200 orders, 30 addresses, 13 after-sales rows | `evidence/auth-service-email-dbsize.txt` |
| Scoped secret scan | only deterministic test fixture field-name probes; no real secrets | `evidence/auth-service-email-secret-scan.txt` |

## Behavior Verified

- Registration creates one pending consumer account, hashes the password, issues a verification token, sends one verification email, and rolls back on email delivery outage.
- Duplicate normalized email is rejected without creating a second account or sending a second email.
- Email verification consumes the token once, locks the account row, activates only pending accounts, records audit, and rejects token reuse.
- Login is enumeration-safe for missing, pending, disabled, malformed, and wrong-password cases; active correct-password login creates a resolvable Redis Session.
- Forgot-password returns accepted for missing/inactive accounts and sends reset email only for active accounts.
- Reset-password consumes the reset token first, locks the account row, hashes the new password, revokes existing sessions, writes the new hash, records audit, and does not restore consumed tokens after downstream failure.
- SMTP delivery uses an explicit verified TLS context when TLS is enabled and maps dependency failures to `EmailDeliveryUnavailable("Email delivery is unavailable")`.
- Repository audit metadata rejects sensitive key substrings such as password, token, session, and secret.

## Quantitative Notes

- Focused MySQL+Redis integration completed in 17.86 seconds.
- Full repository suite completed in 49.80 seconds.
- The slice does not claim production capacity; route-level latency, browser E2E, and 20-concurrent-chat pressure remain later verification work.

## Secret Scan Disposition

The scoped scan matched only deterministic unit-test and integration-test probes:

- field names such as `raw_token`, `password_hash`, and `session_id`;
- fake values such as `hash-value` and `secret-hash` used to prove sanitization.

No credential-bearing Redis URL, SMTP password value, private key, production API token, raw credential token evidence, or real password was found in planned artifacts.

## Residual Risk

- HTTP routes, cookies, CSRF, browser pages, public SMTP environment wiring, rate limiting, demo-data initialization during verification, account deletion, and chat authorization are not part of this slice.
- MySQL was recreated locally during QA. Course data was regenerated and verified, but this is not a backup/restore exercise and does not replace later production recovery work.
- Shared Conda dependency cleanliness is still a later packaging concern; this slice validated behavior in the documented `ai-content-ops` environment.
