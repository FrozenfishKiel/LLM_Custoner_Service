# Authentication Credential Core Independent QA

## Scope/commit

- Scope: independent QA of authentication credential primitives and Redis-backed one-time credential tokens.
- Working directory used: `D:\Projects\llm_customer_service`.
- E: workspace copy was not used.
- Initial `git status --short`:

```text
 M docs/TECHNICAL_DESIGN.md
?? docs/reports/integration/2026-07-18-auth-credential-core.md
?? docs/reports/integration/evidence/auth-credential-core-dbsize.txt
?? docs/reports/integration/evidence/auth-credential-core-full-suite.txt
?? docs/reports/integration/evidence/auth-credential-core-integration.txt
?? docs/reports/integration/evidence/auth-credential-core-load.txt
?? docs/reports/integration/evidence/auth-credential-core-pip-check.txt
?? docs/reports/integration/evidence/auth-credential-core-regression.txt
?? docs/reports/integration/evidence/auth-credential-core-secret-scan.txt
?? docs/reports/integration/evidence/auth-credential-core-unit.txt
?? tests/integration/test_redis_credential_tokens.py
```

- `git log -5 --oneline`:

```text
1e7bf3b feat: add one-time credential tokens
72f1893 test: define credential token contract
f2c1fd6 feat: add bounded credential primitives
2939875 test: define authentication credential contract
808d088 docs: plan auth credential core
```

## Command table

| # | Command | Exit | Observed result/counts |
|---|---|---:|---|
| 1 | `git status --short; git log -5 --oneline` | 0 | Status and latest 5 commits captured above. |
| 2 | `D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth/test_credentials.py tests/unit/auth/test_credential_tokens.py -q` | 0 | `105 passed in 1.70s`. |
| 3 | `D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_redis_credential_tokens.py -q -s -m "integration and not load" --durations=10` | 0 | `13 passed, 2 deselected in 8.25s`; printed `credential_token_aof_recreate_seconds=1.500000`. |
| 4 | `D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/integration/test_redis_credential_tokens.py -q -s -m load --durations=10` | 0 | `2 passed, 13 deselected in 4.27s`; quantitative output captured below. |
| 5 | `D:\Anaconda3\envs\ai-content-ops\python.exe -m pytest tests/unit/auth tests/security -q` | 0 | `161 passed in 5.00s`. |
| 6 | `D:\Anaconda3\envs\ai-content-ops\python.exe -m compileall -q atguigu_ai/auth tests/unit/auth tests/integration` | 0 | No output; compileall completed successfully. |
| 7 | `docker exec llm-cs-redis redis-cli -n 15 DBSIZE` | 0 | `0`. |
| 8 | `docker inspect llm-cs-redis --format ...` | 0 | Labels, image, loopback port, named volume, and AOF command verified below. |

## Normal flow

Covered by unit and integration tests:

- Credential export/interface compatibility checks.
- Email normalization including ASCII display and casefold behavior.
- Password policy accept/reject boundaries, Unicode handling, and no silent trim/normalization.
- Async password hash/verify/needs-rehash flows and module-level bounded semaphore behavior.
- Credential token public async interface.
- Redis issue/consume once flow.
- Raw token is not present in Redis.
- Replacement invalidates old token while preserving other purposes.
- Raw bytes Redis client replies are converted to string account IDs.

## Edge/corruption

Covered edge and corruption scenarios observed in the test names and successful command results:

- Invalid token TTL constructor settings are rejected before Redis use.
- Invalid `account_id`, invalid purpose enum/object, and invalid clock values are rejected.
- Expired token and index fail closed.
- Wrong-type token keys are deleted.
- Missing, wrong-type, and no-TTL current indexes fail closed.
- Corrupt cross-account index does not delete another account's token.
- Forced digest collision across purposes uses four attempts without overwrite.
- Concurrent consume has exactly one winner.
- Concurrent issue leaves exactly one current token.
- Issue-vs-consume linearizes to a valid final state.
- Redis failure is sanitized and recovery path is exercised.
- AOF unconsumed token survives owned container recreation.

## Stress/quantitative numbers

Copied from load and duration output:

```text
argon2_load hash_p50_ms=60.683 hash_p95_ms=62.731 verify_p50_ms=57.402 verify_p95_ms=61.790 concurrent_p50_ms=385.857 concurrent_p95_ms=646.058 concurrent_wall_ms=647.135 rss_mb=98.4
credential_token_redis_samples=300 p50_ms=1.763 p95_ms=2.485 ops_per_second=542.5
credential_token_replacement_1000_final_issue_ms=0.937
```

Slowest non-load integration durations:

```text
2.71s call tests/integration/test_redis_credential_tokens.py::test_aof_unconsumed_token_survives_owned_container_recreation
1.89s call tests/integration/test_redis_credential_tokens.py::test_redis_failure_is_sanitized_and_recovers
1.22s call tests/integration/test_redis_credential_tokens.py::test_expired_token_and_index_fail_closed
```

Slowest load durations:

```text
1.90s call tests/integration/test_redis_credential_tokens.py::test_argon2_load_bounds_and_overload
1.43s call tests/integration/test_redis_credential_tokens.py::test_redis_issue_consume_quantitative_bounds
```

## Risk/monitoring boundary

- This QA run verifies local unit/security behavior, local Redis integration behavior, AOF container recreation behavior, and bounded load/performance assertions in the test suite.
- This run does not prove production Redis topology, production network latency, production maxmemory/AOF persistence settings, alerting, backup/restore, or multi-host failure behavior.
- Monitoring boundary to carry forward: token issuance/consume latency, Redis error rate, AOF persistence health, Redis memory pressure/noeviction failures, and abnormal token collision/retry counts.

## Cleanup DB size

`docker exec llm-cs-redis redis-cli -n 15 DBSIZE` returned:

```text
0
```

## Docker environment verification

`docker inspect llm-cs-redis` showed:

```text
Labels={"com.atguigu.project":"llm_customer_service","com.atguigu.purpose":"redis-session-integration"}
Image=redis:7
Cmd=["redis-server","--appendonly","yes","--maxmemory-policy","noeviction"]
HostConfigPortBindings={"6379/tcp":[{"HostIp":"127.0.0.1","HostPort":"6379"}]}
Mounts=[{"Type":"volume","Name":"llm-cs-redis-data","Source":"/var/lib/docker/volumes/llm-cs-redis-data/_data","Destination":"/data","Driver":"local","Mode":"z","RW":true,"Propagation":""}]
```

Disposition: required labels `project=llm_customer_service` and `purpose=redis-session-integration` are represented as `com.atguigu.project` and `com.atguigu.purpose`; image is `redis:7`; host binding is loopback `127.0.0.1:6379`; named volume `llm-cs-redis-data` is mounted to `/data`; Redis command enables AOF via `--appendonly yes`.

## Defects/disposition

- No failing specified QA command was observed.
- No code/test defects were identified in this independent QA pass.
- Existing dirty worktree entries were present before this report was written and were not modified by this QA pass, except for adding this report file.

## Residual risks

- Test outcomes are local to the current Python environment and Docker Redis container.
- Performance figures are single-run observations, not a statistically significant benchmark campaign.
- Redis container ownership and recreation behavior were exercised by the tests, but broader host/Docker daemon failure modes remain outside this QA scope.

## Final disposition

APPROVED
