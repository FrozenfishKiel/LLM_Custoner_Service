# Authentication Credential Core Integration Report - 2026-07-18

## Scope

This slice adds deterministic email/password credential primitives and Redis-backed email-verification/password-reset credential tokens. It does not add HTTP routes, SMTP delivery, MySQL AuthService orchestration, rate limiting, cookies, CSRF, or browser E2E.

## Acceptance Evidence

| Check | Observed result | Evidence |
| --- | --- | --- |
| Credential unit contracts | 105 passed, 0 failed, 1.68 seconds | `evidence/auth-credential-core-unit.txt` |
| Real Redis credential-token integration | 15 passed, 0 failed, 12.21 seconds | `evidence/auth-credential-core-integration.txt` |
| Load and quantitative checks | 2 passed, 13 deselected, 4.32 seconds | `evidence/auth-credential-core-load.txt` |
| Auth unit and security regression | 161 passed, 0 failed, 5.25 seconds | `evidence/auth-credential-core-regression.txt` |
| Python compileall | Exit 0 | fresh local command |
| Redis DB 15 cleanup | `0` | `evidence/auth-credential-core-dbsize.txt` |
| Secret scan | No unallowlisted private keys, credential-bearing Redis URLs, or long `sk-*` token patterns in scoped credential-core planned artifacts/evidence | `evidence/auth-credential-core-secret-scan.txt` |
| Dependency consistency | `pip check` exit 1 due shared-environment conflicts | `evidence/auth-credential-core-pip-check.txt` |
| Full repository suite | 203 passed, 0 failed, 38.18 seconds | `evidence/auth-credential-core-full-suite.txt` |

## Quantitative Results

| Scenario | Acceptance bound | Observed result |
| --- | ---: | ---: |
| 20 concurrent Argon2 verifies | P95 <= 1.0 second | P95 637.084 ms; wall 637.926 ms |
| Sequential Argon2 hash | recorded, no hard bound | P50 60.920 ms; P95 66.114 ms |
| Sequential Argon2 verify | recorded, no hard bound | P50 60.452 ms; P95 68.370 ms |
| Redis issue+consume | 300 samples recorded | P50 1.544 ms; P95 2.203 ms; 607.5 ops/s |
| Replacement after 1000 stale tokens | final issue <= 250 ms | 0.975 ms |
| AOF recreation | token survives; recovery <= 30 seconds | 1.703 seconds |

## User, Edge, Stress, And Risk Coverage

- Normal usage: issue and consume verification/reset tokens, replacement of an older same-purpose token, independent other-purpose token, expiry, and byte-response clients.
- Adversarial inputs: malformed consume tokens do not call Redis; raw tokens are absent from Redis keys; forced cross-purpose digest collisions exhaust exactly four attempts without overwrite.
- Corruption: wrong-type token keys, missing/wrong-type/no-TTL current indexes, corrupt cross-account indexes, and early expiry fail closed with scoped cleanup.
- Concurrency: 50 concurrent consumes produce exactly one winner; 50 concurrent issues leave exactly one final current token; issue-vs-consume has a valid linearized final state.
- Dependency risk: Redis outage raises only `CredentialTokenStoreUnavailable("Credential token store is unavailable")`; the owned Redis container is recreated and subsequent issue/consume succeeds.
- Persistence: an unconsumed token survives owned-container AOF recreation and remains consumable afterward.

## Monitoring Boundary

The primitive modules expose stable sanitized exceptions: `InvalidEmail`, `InvalidPassword`, `PasswordHashingOverloaded`, and `CredentialTokenStoreUnavailable`. Later HTTP routes should map dependency failures to service-unavailable responses, keep forgot-password/resend responses enumeration-safe, and emit internal counters for Redis failures, allocation exhaustion, Argon2 overload, token consumption outcomes, and downstream reset/activation failures. This slice records the monitoring boundary but does not add Prometheus counters or alerts.

## Shared Environment Notes

`pip check` currently reports conflicts in the shared `ai-content-ops` Conda environment, including OpenAI 2.28.0 vs this repository's OpenAI `<2.0.0` requirement plus unrelated `pywin32`, `pydantic`, and `unstructured-client` constraints. The credential-core tests do not import OpenAI and the full repository suite passed in this environment, but production packaging must use an isolated environment and pass `pip check`.

The full repository suite initially failed while the local MySQL container was stopped. After explicit approval to start the existing `llm-cs-mysql` container, the full suite passed with 203 tests and no matching `llm_cs_test_%` temporary databases remained afterward. Redis DB 15 was empty afterward.

## Residual Risk

This is still a primitive slice. Public registration, verification, login, reset, change-password, disable/delete orchestration, SMTP, cookies, CSRF, rate limits, browser E2E, and production observability remain later slices. The approved orchestration contract is: reset/change/login/disable/delete must serialize on the MySQL account row with `SELECT ... FOR UPDATE`; reset consumes the Redis token first, revokes Sessions while holding the row lock, writes the new password hash, commits, and never restores a consumed token after downstream failure.
