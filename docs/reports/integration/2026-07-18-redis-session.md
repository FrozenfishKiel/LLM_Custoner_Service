# Redis Session Integration Report - 2026-07-18

## Scope

This slice completes the Redis-backed opaque Session store for PRD A-04 and A-05. It also provides the account-wide revocation primitive required later by A-06, A-08, account disable, and account deletion. HTTP authentication routes, cookies, password workflows, and account orchestration remain outside this slice.

## Acceptance Evidence

| Check | Observed result | Evidence |
| --- | --- | --- |
| Unit contract | 43 passed, 0 failed, 1.12 seconds | `evidence/redis-session-unit.txt` |
| Real Redis lifecycle | 21 passed, 0 failed, 14.48 seconds | `evidence/redis-session-integration.txt` |
| Unit and security regression | 56 passed, 0 failed, 4.73 seconds | `evidence/redis-session-regression.txt` |
| Complete repository suite | 83 passed, 0 failed, 23.64 seconds | `evidence/redis-session-full-suite.txt` |
| Python compileall | Exit 0, no compile errors | fresh local command |
| Redis persistence policy | AOF enabled; `maxmemory-policy=noeviction` | container config inspection |
| Test isolation | Redis database 15 size was 0 after the suite | `evidence/redis-session-dbsize.txt` |
| Dependency consistency | Project versions restored; five shared-environment conflicts remain | `evidence/redis-session-pip-check.txt` |

## Quantitative Results

| Scenario | Acceptance bound | Observed result |
| --- | ---: | ---: |
| `revoke_all` with 1000 stale Session hashes | <=250 ms and <=`max(100 ms, 10x low-cardinality median)` | 0.769 ms; low-cardinality median 1.011 ms |
| Concurrent create/revoke-all | 50 iterations without stale valid Session | 50/50 safe |
| Concurrent resolve/revoke-all | 50 iterations without stale valid Session | 50/50 safe |
| Sliding expiry | refreshed Session TTL in 3-4 seconds | within bound |
| AOF persistence across owned-container recreation | Session resolves and recovery <=30 seconds | 1.890 seconds |
| Redis outage and recovery | sanitized failure, then successful create/resolve | passed |
| Allocation collision budget | exactly four attempts | passed |

Independent sequential microbenchmark results against the local Redis container:

| Operation | Samples | P50 | P95 | Throughput |
| --- | ---: | ---: | ---: | ---: |
| Create | 300 | 0.697 ms | 0.971 ms | 1356.2 ops/s |
| Resolve | 300 | 0.726 ms | 0.979 ms | 1319.1 ops/s |
| Revoke | 300 | 0.606 ms | 0.832 ms | 1545.7 ops/s |
| Revoke all | 10 vs. 1000 Sessions | 0.727 ms | 0.698 ms | constant-time behavior observed |

Source: `evidence/redis-session-independent-qa.md`.

The independent QA run separately observed 66/66 relevant cases: 20 Redis integration cases and 46 Session-unit plus repository-security cases. Integration pytest time was 16.58 seconds; AOF recreation took 2.99 seconds and outage recovery took 2.16 seconds. Its whole 1000-Session test case took 0.96 seconds including setup; the acceptance metric above measures only the `revoke_all` call. The independent execution record is retained in `evidence/redis-session-independent-qa.md`.

## User, Edge, Stress, and Risk Coverage

- Normal usage: create, resolve, single logout, repeated logout, account-wide logout, renewal, expiry, and new login after revocation.
- Adversarial inputs: empty and oversized tokens never call Redis; raw tokens never appear in Redis keys; malformed Redis records and missing generation fail closed and are deleted.
- Encoding and type corruption: non-UTF-8-encodable token strings never call Redis; a Session key with the wrong Redis type is deleted and resolves as unauthenticated.
- Concurrency: create/revoke-all and resolve/revoke-all races were repeated 50 times each with an explicit final invalidation check.
- Persistence and dependency failure: Session state survived recreation with the same named AOF volume; a stopped Redis produced only `SessionStoreUnavailable("Session store is unavailable")` and recovered without an in-memory fallback.
- Harness ownership: destructive container operations require the project and purpose labels, fixed loopback binding, named volume, Redis 7 image, command, and `unless-stopped` restart policy.

## Monitoring Boundary

The store exposes a single sanitized `SessionStoreUnavailable` boundary, allowing later HTTP middleware to count dependency failures and return 503 without leaking Redis details. This slice does not yet expose Prometheus counters, online-Session gauges, HTTP status metrics, or alerts; those belong to the later authentication-route and observability slices and must be exercised again in end-to-end testing.

## Environment Incident

Docker Desktop 4.78 was observed returning an internally healthy Redis after `docker restart` while dropping the Windows host port publication. A TCP connection to port 6379 could succeed through a stale Docker proxy without receiving a Redis protocol response. The harness now verifies container ownership, recreates only the owned container with the same AOF named volume, and polls an actual Redis `PING` through `127.0.0.1`.

The shared `ai-content-ops` Conda environment had drifted to OpenAI 2.28.0 and LangChain OpenAI 1.1.11, which violated this repository's OpenAI 1.x contract. It was restored to OpenAI 1.109.1 and LangChain OpenAI 1.1.9, and the repository upper bound was tightened to `<1.1.10`. Fresh `pip check` still reports five conflicts owned by other workloads in the shared environment (`mcp`, `openai-agents`, `pythonproject16`, `sqlmodel`, and `unstructured-client`). Production packaging must use an isolated environment and pass `pip check` there.

## Residual Risk

This evidence validates the standalone Redis Session adapter, not the complete login experience. Cookie flags, CSRF, rate limiting, account status refresh, HTTP 503 mapping, SMTP, password hashing, registration/login endpoints, browser behavior, metrics, and production Redis authentication/TLS remain unimplemented or unverified in this slice. Old revoked Session hashes intentionally wait for TTL cleanup and generation keys have no TTL, so production observability must include Redis memory and key-count capacity. Disk-full, AOF corruption, replication/failover, sustained high latency, long soak, and production healthcheck behavior remain for deployment fault exercises.
