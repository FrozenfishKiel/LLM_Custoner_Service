# Authentication Service and Email Delivery Independent QA

Date: 2026-07-19

Workspace: `D:\Projects\llm_customer_service`

Disposition: APPROVED

## Summary

Independent QA passed for the Authentication Service and Email Delivery slice. Targeted unit tests, real MySQL + Redis integration tests, security regression tests, full suite, compileall, Redis/MySQL cleanup checks, UTF-8 artifact decoding, scoped secret scan, and commit/file scope review all completed without blocking findings.

Incremental re-review after post-QA changes also passed. The new `ecs_demo/gen_data.py` database URL change compiles, uses `build_database_url()`, and no longer carries hardcoded MySQL URL/password construction. Refreshed full-suite and DB evidence are consistent with this report.

## Commands and results

### Targeted unit tests

Command:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth/test_auth_service.py tests/unit/auth/test_account_repository.py tests/unit/email/test_delivery.py -q
```

Result: PASS, exit code 0.

Output:

```text
54 passed in 1.19s
```

### Real MySQL + Redis integration tests

Command:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_auth_service_mysql_redis.py -q -s -m integration
```

Result: PASS, exit code 0.

Output:

```text
11 passed in 16.92s
```

The run exercised Alembic migration setup against temporary MySQL databases and Redis DB 15 cleanup.

### Regression tests

Command:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth tests/unit/email tests/security -q
```

Result: PASS, exit code 0.

Output:

```text
215 passed in 5.25s
```

### Full test suite

Command:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests -q
```

Result: PASS, exit code 0.

Output:

```text
268 passed in 49.80s
```

### Compile check

Command:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m compileall -q atguigu_ai/auth atguigu_ai/email tests/unit/auth tests/unit/email tests/integration
```

Result: PASS, exit code 0. No output was emitted by `compileall -q`.

Post-QA incremental compile command:

```powershell
D:\Anaconda3\envs\ai-content-ops\python.exe -m py_compile ecs_demo/gen_data.py
```

Result: PASS, exit code 0. No output was emitted.

### Redis cleanup

Command:

```powershell
docker exec llm-cs-redis redis-cli -n 15 DBSIZE
```

Result: PASS, exit code 0.

Output:

```text
0
```

### MySQL cleanup and course data counts

Command:

```powershell
docker exec llm-cs-mysql mysql -uroot -p[redacted-local-test-password] -N -e "SELECT COUNT(*) AS temp_db_count FROM information_schema.schemata WHERE schema_name LIKE 'llm_cs_test\\_%' ESCAPE '\\';"
```

Result: PASS, exit code 0.

Output:

```text
0
```

Command:

```powershell
docker exec llm-cs-mysql mysql -uroot -p[redacted-local-test-password] -N -e "SHOW DATABASES LIKE 'llm_cs_test_%'; SELECT 'user_info', COUNT(*) FROM ecs.user_info UNION ALL SELECT 'order_info', COUNT(*) FROM ecs.order_info UNION ALL SELECT 'receive_info', COUNT(*) FROM ecs.receive_info UNION ALL SELECT 'postsale', COUNT(*) FROM ecs.postsale;"
```

Result: PASS, exit code 0. No temporary database names were returned, and course data counts were present:

```text
user_info    10
order_info   200
receive_info 30
postsale     13
```

The MySQL CLI emitted its standard warning about command-line password usage; the report redacts the local test password.

Post-QA refreshed DB evidence and live DB check:

```text
Redis DB15 DBSIZE: 0
MySQL temp database count matching llm_cs_test_%: 0
user_info    10
order_info   200
receive_info 30
postsale     13
```

### Strict UTF-8 decode for auth-service-email evidence

Command:

```powershell
@'
from pathlib import Path
for p in sorted(Path('docs/reports/integration/evidence').glob('auth-service-email-*.txt')):
    p.read_text(encoding='utf-8', errors='strict')
    print(f'OK {p}')
'@ | D:\Anaconda3\envs\ai-content-ops\python.exe -
```

Result: PASS, exit code 0.

Decoded files:

```text
auth-service-email-compileall.txt
auth-service-email-dbsize.txt
auth-service-email-full-suite.txt
auth-service-email-integration.txt
auth-service-email-regression.txt
auth-service-email-secret-scan.txt
auth-service-email-unit.txt
auth-service-email-whitespace.txt
```

### Scoped secret scan

Commands:

```powershell
rg -n --hidden -i "(password\s*=|password:|secret|api[_-]?key|private key|BEGIN RSA|BEGIN OPENSSH|mysql\+pymysql://|redis://|client_secret|raw_token|session_id|password_hash)" docs/reports/integration/evidence -g "auth-service-email-*"
```

```powershell
rg -n --hidden -i "(-----BEGIN [A-Z ]*PRIVATE KEY-----|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|mysql\+pymysql://[^\s""']+:[^@\s""']+@|redis://[^\s""']+:[^@\s""']+@|smtp.*password\s*=\s*['""][^'""]+|client_secret\s*[=:]\s*['""][^'""]+)" atguigu_ai/auth atguigu_ai/email tests/unit/auth tests/unit/email tests/integration docs/reports/integration/evidence -g "auth-service-email-*"
```

Result: PASS.

Findings:

- Broad scan hits in `auth-service-email-secret-scan.txt` are documented fixture/probe strings only: `password_hash`, `raw_token`, and fake sanitization text.
- Strict critical scan found no private keys, production API tokens, credential-bearing MySQL/Redis URLs, SMTP password values, or real `client_secret` values.
- Additional review hits in code/tests were deterministic test fixtures or generated/configured URL templates, not committed secrets.
- Post-QA scan included `ecs_demo/gen_data.py` and found the intended switch to `build_database_url()`. The only planned-artifact critical-pattern hit was an illustrative placeholder in `auth-service-email-secret-scan.txt` documenting that the previous credential-bearing URL construction was removed; it is not a real credential or live connection string. No real credential-bearing URL, private key, API token, SMTP password assignment, or real `client_secret` was found.

## Commit and file scope review

Reviewed slice scope from `07911d8^..HEAD`.

Changed files:

```text
atguigu_ai/auth/__init__.py
atguigu_ai/auth/account_repository.py
atguigu_ai/auth/service.py
atguigu_ai/email/__init__.py
atguigu_ai/email/delivery.py
docs/TECHNICAL_DESIGN.md
docs/superpowers/plans/2026-07-19-auth-service-email.md
ecs_demo/gen_data.py
tests/integration/test_auth_service_mysql_redis.py
tests/unit/auth/test_account_repository.py
tests/unit/auth/test_auth_service.py
tests/unit/auth/test_credential_tokens.py
tests/unit/email/test_delivery.py
```

Review notes:

- Auth/email implementation scope is consistent with the requested slice: email adapter, account repository, AuthService orchestration, and related exports.
- Tests cover registration, duplicate email handling, verification, login gating, password reset, session revocation, sanitized dependency outages, MySQL rollback behavior, Redis cleanup, email delivery behavior, and existing auth/security regressions.
- `ecs_demo/gen_data.py` change is justified and safe. The previous `max([postsale.complete_time for postsale in postsales])` could fail when generated post-sale completion times were `None`. The fix filters `None` values and falls back to a generated completion time from `delivered_time`, preserving demo-data generation without changing production auth/email behavior.
- Post-QA `ecs_demo/gen_data.py` now creates the engine from `build_database_url()` instead of assembling a hardcoded `mysql+pymysql://...` URL from local password variables. This reduces secret exposure risk and keeps database configuration centralized.
- No unrelated broad refactor, schema migration, production deployment change, secret/auth permission model change, or breaking external interface change was found in this slice.

## Findings

No blocking findings.

## Final disposition

APPROVED
