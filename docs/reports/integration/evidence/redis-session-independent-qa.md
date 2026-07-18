# Redis Session Independent QA Execution Record

## Record scope

This document is an independent QA execution record produced against
`D:/Projects/llm_customer_service`. It is a curated record of commands, observed
results, timings, and review findings. It is not raw pytest stdout.

- Date: 2026-07-18
- Host OS: Windows, PowerShell
- Test Python: CPython 3.11.15 via `uv run --no-project`
- Authoritative pytest environment: pytest 9.1.1, pytest-asyncio 1.4.0,
  redis-py constrained to `>=5,<6`, SQLAlchemy constrained to `>=2,<3`
- Redis endpoint: loopback port 6379, database 15
- Docker container exclusively exercised: `llm-cs-redis`
- MySQL and Neo4j were not accessed or changed.
- Production code was not modified by this QA execution.

## Commands executed

The repository requires Python 3.10 or newer. Commands used an ephemeral `uv`
environment because the system-default interpreter was Python 3.7 with a pytest
version that did not support the repository's `--strict-markers` setting.

Container identity and initial/final state:

```powershell
docker inspect llm-cs-redis --format '{{json .Config.Labels}}'
docker inspect llm-cs-redis --format 'status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} image={{.Config.Image}} restart={{.HostConfig.RestartPolicy.Name}}'
docker inspect llm-cs-redis --format '{{json .Mounts}}'
docker inspect llm-cs-redis --format '{{json .NetworkSettings.Ports}}'
docker inspect llm-cs-redis --format '{{json .Config.Cmd}}'
docker exec llm-cs-redis redis-cli -n 15 DBSIZE
docker exec llm-cs-redis redis-cli INFO persistence | Select-String '^(rdb_|aof_|loading)'
```

Integration collection and authoritative run:

```powershell
uv run --no-project --python 3.11 --with "pytest>=7" --with "pytest-asyncio>=0.21" --with "redis>=5,<6" --with "sqlalchemy>=2,<3" python -m pytest tests/integration/test_redis_session.py --collect-only -q

$sw=[System.Diagnostics.Stopwatch]::StartNew(); & uv run --no-project --python 3.11 --with "pytest>=7" --with "pytest-asyncio>=0.21" --with "redis>=5,<6" --with "sqlalchemy>=2,<3" python -m pytest tests/integration/test_redis_session.py -vv --durations=20; $code=$LASTEXITCODE; $sw.Stop(); Write-Output ("QA_WALL_SECONDS={0:N3}" -f $sw.Elapsed.TotalSeconds); exit $code
```

Unit and security collection, initial minimal-environment run, and authoritative
dependency-complete import-chain rerun:

```powershell
uv run --no-project --python 3.11 --with "pytest>=7" --with "pytest-asyncio>=0.21" --with "redis>=5,<6" --with "sqlalchemy>=2,<3" python -m pytest tests/unit/auth/test_session.py tests/security --collect-only -q

$sw=[System.Diagnostics.Stopwatch]::StartNew(); & uv run --no-project --python 3.11 --with "pytest>=7" --with "pytest-asyncio>=0.21" --with "redis>=5,<6" --with "sqlalchemy>=2,<3" python -m pytest tests/unit/auth/test_session.py tests/security -vv --durations=15; $code=$LASTEXITCODE; $sw.Stop(); Write-Output ("QA_WALL_SECONDS={0:N3}" -f $sw.Elapsed.TotalSeconds); exit $code

$sw=[System.Diagnostics.Stopwatch]::StartNew(); & uv run --no-project --python 3.11 --with "pytest>=7" --with "pytest-asyncio>=0.21" --with "redis>=5,<6" --with "sqlalchemy>=2,<3" --with "jinja2>=3,<4" --with "pymysql>=1" --with "pyyaml>=6,<7" --with "numpy>=1.24,<3" --with "langgraph>=0.2" --with "python-dotenv>=1,<2" python -m pytest tests/unit/auth/test_session.py tests/security -q --durations=15; $code=$LASTEXITCODE; $sw.Stop(); Write-Output ("QA_WALL_SECONDS={0:N3}" -f $sw.Elapsed.TotalSeconds); exit $code
```

An attempt to construct the repository's complete runtime dependency set was
bounded at 300 seconds and timed out before pytest started:

```powershell
uv run --no-project --python 3.11 --with-requirements requirements-atguigu.txt --with "pytest>=7" --with "pytest-asyncio>=0.21" python -m pytest tests/unit/auth/test_session.py tests/security -vv --durations=15
```

The independent latency benchmark was executed as an inline Python program with
the following command. It used 300 sequential samples per basic operation,
compared `revoke_all` at 10 and 1000 sessions, and called `FLUSHDB` in a `finally`
block:

```powershell
@'
import asyncio, statistics, time
from redis.asyncio import Redis
from atguigu_ai.auth import AccountIdentity, AccountRole, AccountStatus, RedisSessionStore

N = 300
identity = AccountIdentity("qa-bench-account", AccountRole.consumer, AccountStatus.active)

def stats(name, values):
    ordered = sorted(values)
    p50 = statistics.median(ordered)
    p95 = ordered[max(0, int(len(ordered) * 0.95) - 1)]
    total = sum(values)
    print(f"{name}: n={len(values)} p50_ms={p50*1000:.3f} p95_ms={p95*1000:.3f} throughput_ops_s={len(values)/total:.1f}")

async def main():
    redis = Redis.from_url("redis://127.0.0.1:6379/15", decode_responses=False)
    try:
        await redis.flushdb()
        store = RedisSessionStore(redis, ttl_seconds=300, refresh_threshold_seconds=30)
        created, values = [], []
        for _ in range(N):
            started = time.perf_counter()
            session = await store.create(identity)
            values.append(time.perf_counter() - started)
            created.append(session)
        stats("create", values)
        values = []
        for session in created:
            started = time.perf_counter()
            assert await store.resolve(session.token) is not None
            values.append(time.perf_counter() - started)
        stats("resolve", values)
        values = []
        for session in created:
            started = time.perf_counter()
            await store.revoke(session.token)
            values.append(time.perf_counter() - started)
        stats("revoke", values)
        for count in (10, 1000):
            await redis.flushdb()
            for _ in range(count):
                await store.create(identity)
            started = time.perf_counter()
            await store.revoke_all(identity.account_id)
            print(f"revoke_all: sessions={count} latency_ms={(time.perf_counter()-started)*1000:.3f}")
        print(f"keys_before_final_cleanup={await redis.dbsize()}")
    finally:
        await redis.flushdb()
        print(f"keys_after_final_cleanup={await redis.dbsize()}")
        await redis.aclose()

asyncio.run(main())
'@ | uv run --no-project --python 3.11 --with "redis>=5,<6" --with "sqlalchemy>=2,<3" python -
```

## Authoritative test results

| Suite | Result | pytest time | QA wall time |
| --- | ---: | ---: | ---: |
| `tests/integration/test_redis_session.py` | 20/20 passed | 16.58 s | 19.351 s |
| `tests/unit/auth/test_session.py` | 41/41 passed | included below | included below |
| `tests/security` | 5/5 passed | included below | included below |
| Unit plus security | 46/46 passed | 13.76 s | 40.854 s |

The valid runs therefore covered 66 collected tests with 66 passes and no
product assertion failures.

Observed integration test call durations:

- AOF-backed session survival across container recreation: 2.99 s.
- Redis outage, sanitized exception, container recreation, and recovery: 2.16 s.
- `create` racing `revoke_all` for 50 iterations: 0.30 s.
- `resolve` racing `revoke_all` for 50 iterations: 0.17 s.
- The 1000-session constant-time test, including setup and assertions: 0.96 s.
- TTL refresh test: 2.21 s.
- Inactive session/index expiry test: 2.21 s.

The integration suite verified normal create/resolve/revoke/revoke-all flows,
idempotent revoke, malformed tokens without Redis access, all six corrupt or
missing hash fields, generation mismatch and loss, TTL refresh and expiry,
concurrent ordering, AOF persistence, outage recovery, and raw-token exclusion
from Redis keys.

## Independent 300-sample benchmark

These are the benchmark program's raw aggregate output values. Throughput is
derived from the sum of sequential per-operation latency samples; it is not a
multi-client saturation measurement.

```text
create: n=300 p50_ms=0.697 p95_ms=0.971 throughput_ops_s=1356.2
resolve: n=300 p50_ms=0.726 p95_ms=0.979 throughput_ops_s=1319.1
revoke: n=300 p50_ms=0.606 p95_ms=0.832 throughput_ops_s=1545.7
revoke_all: sessions=10 latency_ms=0.727
revoke_all: sessions=1000 latency_ms=0.698
keys_before_final_cleanup=1001
keys_after_final_cleanup=0
```

The 10-session and 1000-session `revoke_all` measurements support the O(1)
generation-based implementation under this local test workload. They are not a
production capacity guarantee.

## Environment false failure and timeout

The first unit-plus-security run reported 45/46 passes. The sole failing test was
`test_database_url_is_built_from_the_supplied_environment`. Its subprocess
returned exit code 1 before reaching the URL assertions because the deliberately
minimal ephemeral environment did not contain dependencies imported by the broad
`ecs_demo.actions` package import chain. Direct reproduction exposed missing
modules in sequence: `jinja2`, `yaml`, `numpy`, `langgraph`, and `dotenv`.

After adding the repository-declared packages needed by that import chain,
including PyMySQL, the same complete 46-test selection passed 46/46. The initial
45/46 result is therefore classified as a QA environment false failure, not a
Redis Session or database URL product defect.

The attempt to use all of `requirements-atguigu.txt` timed out at approximately
300 seconds while constructing the environment, before pytest produced results.
That requirements set includes unrelated heavyweight ML dependencies such as
Torch and sentence-transformers. The process was terminated by its command
timeout and was not allowed to wait indefinitely.

## Redis container and cleanup evidence

Final observed contract and state:

- Container: `llm-cs-redis`, running, restart count 0.
- Labels: `com.atguigu.project=llm_customer_service` and
  `com.atguigu.purpose=redis-session-integration`.
- Image: `redis:7`.
- Command: `redis-server --appendonly yes --maxmemory-policy noeviction`.
- Port binding: container 6379 exposed only on `127.0.0.1:6379`.
- Persistence: named volume `llm-cs-redis-data` mounted at `/data`.
- AOF: enabled; final `aof_last_write_status=ok`, no pending background fsync.
- Final Redis DB 15 size: **0 keys**.

The integration fixtures rebuilt the owned Redis container as part of persistence
and outage tests. No MySQL or Neo4j containers or data were touched.

## Monitoring review and residual risk

`SessionStoreUnavailable` is exported as a stable exception boundary. Connection,
timeout, response, and script failures are converted to the sanitized message
`Session store is unavailable`; tests confirm secrets and raw Redis error details
are not exposed. This gives an upper layer a reliable exception type that can be
mapped to HTTP 503 and counted.

Concerns remaining before production rollout:

1. No current application route or middleware consumer was found that maps
   `SessionStoreUnavailable` to HTTP 503 or emits a structured metric, log, or
   alert. The design document explicitly leaves that mapping to a later auth
   routing stage.
2. Exceptions are raised with `from None`. This protects sensitive Redis details
   but also removes the cause, so monitoring cannot distinguish connectivity,
   timeout, Redis response, and Lua/data errors without separate instrumentation.
3. The scripts are tested only against standalone Redis. The resolve script
   derives and accesses keys dynamically and does not claim Redis Cluster
   compatibility.
4. `revoke_all` intentionally leaves invalidated session hashes until TTL expiry,
   while per-account generation keys have no TTL. Capacity and account-churn
   monitoring are required, especially with `noeviction`.
5. The integration container has no Docker healthcheck. Production readiness
   requires an external probe and alerting path.
6. Disk-full behavior, AOF corruption, replication/failover, network latency and
   timeout tuning, long soak/load behavior, production authentication, and TLS
   were not covered by this phase.

## QA disposition

`DONE_WITH_CONCERNS`: Redis Session functional, corruption, outage, persistence,
concurrency, and constant-time behavior passed the defined suites and independent
benchmark. The main release concern is missing upper-layer 503/metrics/logging
integration, followed by production Redis topology, durability, and capacity
validation gaps.
