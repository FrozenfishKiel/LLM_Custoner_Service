# Authentication Credential Core Design

## Scope

This slice provides the security primitives required by PRD A-01, A-02, A-03, A-06, A-07, and A-08:

- canonical consumer email parsing and duplicate-key normalization;
- bounded password validation, Argon2id hashing, verification, and rehash detection;
- Redis-backed email-verification and password-reset tokens with expiry, replacement, and atomic one-time consumption.

It does not claim that registration or login is user-accessible. MySQL account transactions, SMTP delivery, rate limiting, Session creation/revocation orchestration, demo-data activation, HTTP routes, cookies, CSRF, metrics, and browser flows remain separate slices. In particular, an account must not be marked active until the later activation transaction creates and binds its business user and initializes demo data.

## Approaches Considered

### Layered credential core and later synchronous orchestration

Keep email/password policy and Redis credential-token storage as deep modules. A later `AuthService` synchronously coordinates their narrow interfaces with the account repository, Session store, email adapter, and activation port. This is the selected approach because failures remain attributable and each dependency can be tested independently without introducing distributed workflow infrastructure.

### FastAPI-route orchestration

Routes could directly validate passwords, query MySQL, write Redis, and send SMTP. This uses fewer initial files but spreads enumeration resistance, transaction boundaries, dependency failures, and security logging across handlers. It is rejected.

### Event-driven account activation

Registration and verification could publish events for email delivery and demo-data initialization. This would require durable messaging, idempotent consumers, reconciliation, and operational monitoring that the confirmed single-machine target does not otherwise need. It is rejected for this version.

## Domain Language

- **Account:** the durable login identity stored in MySQL.
- **Business user:** the existing ecommerce `user_info` identity that owns orders, logistics, addresses, and after-sales data.
- **Pending account:** an account whose email is not yet verified and which has no usable authenticated customer identity.
- **Active account:** a verified account whose business user binding and demo-data initialization have completed atomically.
- **Credential token:** an opaque, short-lived proof used only for email verification or password reset. It is not a Session.
- **Token purpose:** exactly `verify_email` or `reset_password`; a token cannot be consumed for the other purpose.

## Module Boundaries

### `atguigu_ai.auth.credentials`

This module exports:

```python
class EmailAddress:
    display: str
    normalized: str

class InvalidEmail(ValueError): ...

class InvalidPassword(ValueError): ...

class PasswordHashingOverloaded(RuntimeError): ...

class PasswordPolicy:
    def validate(self, password: str) -> None: ...

class PasswordHasher:
    async def hash(self, password: str) -> str: ...
    async def verify(self, password_hash: str | None, password: str) -> bool: ...
    def needs_rehash(self, password_hash: str) -> bool: ...

def normalize_email(value: str) -> EmailAddress: ...
```

The module has no database, Redis, HTTP, SMTP, logging, or environment access. Callers provide only validated scalar values to later adapters. `InvalidEmail` and `InvalidPassword` are public stable validation categories; parser and Argon2 exception types remain private. `PasswordHashingOverloaded` is the stable admission-control failure used by later HTTP code to return a generic overload response and increment an internal metric.

### `atguigu_ai.auth.credential_tokens`

This module exports:

```python
class CredentialTokenPurpose(str, Enum):
    verify_email = "verify_email"
    reset_password = "reset_password"

class IssuedCredentialToken:
    token: str
    expires_at: datetime

class CredentialTokenStoreUnavailable(RuntimeError): ...

class RedisCredentialTokenStore:
    async def issue(self, account_id: str, purpose: CredentialTokenPurpose) -> IssuedCredentialToken: ...
    async def consume(self, token: str, purpose: CredentialTokenPurpose) -> str | None: ...
```

The exact new `atguigu_ai.auth` export set is `EmailAddress`, `InvalidEmail`, `InvalidPassword`, `PasswordHashingOverloaded`, `PasswordPolicy`, `PasswordHasher`, `normalize_email`, `CredentialTokenPurpose`, `IssuedCredentialToken`, `CredentialTokenStoreUnavailable`, and `RedisCredentialTokenStore`. All existing model and Session exports remain unchanged. The token store does not query MySQL, send email, activate accounts, reset passwords, or create Sessions.

`RedisCredentialTokenStore` accepts an async redis-py client, optional per-purpose TTLs, a token factory, and a clock. Both default TTLs are 1800 seconds; configured TTLs must be positive integers. The clock must return a timezone-aware datetime. Account IDs follow the existing Session boundary: non-blank strings of at most 36 characters. Invalid constructor, account, purpose, or clock input fails before any Redis call.

## Email Contract

1. Input must be a Python string. Leading and trailing ASCII whitespace is removed; embedded control characters and display-name syntax are rejected.
2. The address is parsed with `email-validator` using `allow_smtputf8=False` and `check_deliverability=False`. Network DNS checks do not belong in deterministic registration validation.
3. The validated mailbox must be at most 254 ASCII characters.
4. `display` is the validated address with its canonical ASCII/IDNA domain and preserved local-part spelling. `normalized` is `display.casefold()` so registration and login treat the complete address as case-insensitive, matching the existing unique `email_normalized` column and migration tests.
5. Invalid input raises one stable `InvalidEmail("Invalid email address")`; parser details and the submitted address are not exposed through the public boundary or logs.

This product-level case-insensitive mailbox rule is deliberate even though SMTP permits case-sensitive local parts. It avoids duplicate consumer accounts and matches common provider behavior.

## Password Contract

1. Password input must be a Python string containing 8 through 128 Unicode scalar values. It is never trimmed or normalized.
2. NUL, ASCII control characters, DEL, and isolated surrogate code points are rejected. The UTF-8 representation must not exceed 512 bytes.
3. Validation failures raise `InvalidPassword("Password does not meet requirements")`; public login failures later use a different uniform invalid-credentials result.
4. Hashing uses Argon2id through `argon2-cffi` with explicit parameters: 64 MiB memory, time cost 3, parallelism 4, 32-byte hash, and 16-byte random salt.
5. `verify` accepts a stored hash or `None`. A valid hash is verified normally. For `None`, malformed hashes, or unsupported hash encodings, the hasher verifies the submitted password against a private valid dummy Argon2id hash and returns `False`; unknown email, corrupt storage, and wrong password therefore all execute one expensive Argon2 verification without exposing library details.
6. Policy-invalid submitted passwords return `False` from `verify` without entering Argon2, allowing the later public login boundary to reject attacker-controlled oversized work while still using the same generic invalid-credentials response. `hash` validates and raises `InvalidPassword`. `needs_rehash` reports whether a valid stored hash differs from current parameters and returns `False` for malformed input.
7. Hash and verify are async public operations. Argon2 runs through AnyIO's worker-thread bridge and a process-wide shared bounded worker semaphore of four concurrent jobs, so application event loops never execute Argon2 directly, synchronous imports require no async backend, and constructing extra `PasswordHasher` instances cannot multiply capacity. A second process-wide bounded semaphore allows at most 20 admitted jobs including running work; excess attempts fail immediately with `PasswordHashingOverloaded("Password hashing capacity is unavailable")`. Later per-IP/account rate limiting remains mandatory.
8. Before verifying an encoded hash, the module rejects values longer than 512 ASCII characters, applies bounded field-length syntax, and parses its parameters without performing Argon2 work. Only Argon2id version 19 hashes within memory 8-65536 KiB, time cost 1-3, parallelism 1-4, hash length 16-64, and salt length 8-32 are eligible. Higher-cost, oversized, or malformed attacker-controlled encodings take the bounded dummy path instead of allocating their declared resources.
9. A local design probe with the final 64 MiB/t=3/p=4 settings observed sequential verify P95 62.86 ms and 20 verifications through four workers completing in 750.33 ms. Implementation evidence must measure end-to-end async latency including queue time and require 20 admitted operations to complete with P95 at most 1 second, no error, and no OOM on the documented local target.

## Credential Token Contract

### Keys

```text
auth:verify_email:{token_hash}                         -> account_id, TTL 1800 seconds
auth:reset_password:{token_hash}                       -> account_id, TTL 1800 seconds
auth:credential_token_current:{purpose}:{account_id}   -> token_hash, TTL 1800 seconds
```

The raw token contains 256 random bits from `secrets.token_urlsafe(32)` and is returned once. Its accepted wire grammar is exactly 43 ASCII base64url characters matching `[A-Za-z0-9_-]{43}` with no padding. Redis keys and values never contain the raw token.

### Issuance

`issue` validates `account_id` and purpose before touching Redis. One Lua script validates the candidate digest, checks both purpose prefixes so the raw candidate is not already represented anywhere, replaces the previously indexed token for the same account and purpose, stores the new digest key and current-token index, and applies the purpose TTL to both. Before deleting a previously indexed token key, the script verifies that its stored account ID equals the account being issued; a corrupt index that points at another account's digest is deleted without touching the other account's token. A digest collision consumes one of four allocation attempts. Four failures raise `CredentialTokenStoreUnavailable("Unable to allocate credential token")`.

Only one token per account and purpose is current. Resending verification or requesting another reset immediately invalidates the previous token without affecting the other purpose.

### Consumption

Malformed raw tokens return `None` without Redis access. One Lua script reads the account ID from the digest key, validates its shape, derives the current-token index, and compares its digest. A match atomically deletes both keys and returns the account ID. Missing, expired, replaced, wrong-purpose, corrupt, or already consumed tokens return `None` and clean the directly addressed corrupt key where safe.

Both Lua scripts check Redis key types before `GET`. During issue, a wrong-type account/purpose current index is account-scoped and is deleted before issuing the replacement. During consume, a wrong-type digest key is deleted and returns `None`; a valid digest key paired with a missing, expired, wrong-type, non-expiring, or mismatched current index causes the digest key to be deleted and returns `None`. A wrong-type current index is also deleted because it is scoped to the validated account and purpose. Neither path converts stored corruption into an unauthenticated 401-worthy dependency error. Actual Redis command/script failures still raise the sanitized store-unavailable exception.

The adapter accepts redis-py clients configured with either decoded string responses or raw byte responses and normalizes both. `expires_at` is a UTC-aware datetime computed from the same validated integer TTL passed to Redis `EXPIRE`; invalid or naive clocks fail before Redis access.

The adapter supports standalone Redis only because consume derives an account-specific key after reading the token key. It does not claim Redis Cluster compatibility.

Consumption is deliberately **consume before durable mutation** and at-most-once. A later email-activation or password-reset service must atomically consume the proof before starting its MySQL mutation. If a downstream dependency fails before MySQL commit, the token is not restored: MySQL account/password state remains unchanged, Sessions may already have been conservatively revoked, the dependency failure is recorded internally, and the user must request a fresh verification or reset email. Public responses must not claim success. This avoids a crash window in which an already successful password change could be followed by automatic token restoration and replay. The project does not introduce distributed transactions, token reservation, or a new MySQL token ledger in this version.

### Required future orchestration order

Login, password change, password reset, account disable, and account deletion must serialize on the same MySQL account row with `SELECT ... FOR UPDATE`; an in-process lock is insufficient because deployment may run multiple workers.

- Login opens a transaction, locks the account row, verifies status/password, creates the Redis Session while retaining the row lock, then commits. Redis failure rolls back and returns no Session to the client.
- Password reset consumes the reset token, opens a transaction, locks the account row, calls `revoke_all` while retaining the lock, writes the new password hash, then commits. A competing old-password login either creates its Session before reset acquires the row lock and is then revoked, or acquires the row after commit and fails password verification. There is no generation window in which an old-password login survives reset.
- If `revoke_all` fails, the password transaction rolls back. If MySQL fails after successful revocation, the password remains unchanged, old Sessions are conservatively invalidated, the consumed token stays spent, and the user requests a new token. If the commit succeeds but the HTTP response is lost, the new password and revoked Sessions are the durable truth; retrying the spent token cannot change state again.
- Email activation consumes the verification token and then performs email timestamp, business-user creation/binding, demo initialization, and `active` transition in one MySQL row-locked transaction. Failure leaves the account `pending`; response loss after commit leaves it fully active. A pending user can request a fresh token, while an already active user receives the later endpoint's idempotent generic result.

Integration tests for the later orchestration slice must cover both login/reset lock orders, Redis revoke failure, MySQL rollback after successful revoke, MySQL commit with simulated response loss, duplicate activation, and consumed-token retry. These are completion gates for public password and verification routes, not claims made by this primitive-only slice.

### Failures

Redis connection, timeout, response, and script errors raise only `CredentialTokenStoreUnavailable("Credential token store is unavailable")`. There is no in-memory fallback. Later public forgot-password and resend endpoints must keep enumeration-safe responses while emitting structured internal dependency metrics.

## Concurrency and Security Invariants

- Two concurrent issues for the same account and purpose linearize; only the winner indexed last can be consumed.
- Two concurrent consumes of one token yield exactly one account ID and one `None`.
- Issue racing consume has exactly two valid linearized outcomes: consume wins first and returns the account before issue installs the replacement, or issue wins first and the old token returns `None`; a replaced token never becomes current again.
- A candidate digest that already exists for any account or purpose is treated as a collision and never overwrites the existing digest key or either account's current index.
- A verification token cannot reset a password and a reset token cannot verify email.
- Expired, missing, malformed, corrupt, or wrong-type token keys fail closed.
- Raw passwords, raw tokens, full submitted email values, Redis URLs, and library exception text never appear in exception messages or committed evidence.
- A downstream durable-operation failure never restores a consumed token; orchestration tests must prove MySQL state is unchanged before commit, any pre-commit revocation is conservative, and a newly issued token is required for retry.

## Verification Strategy

### Unit tests

- ASCII email normalization, surrounding whitespace, IDNA domain handling, complete-address case folding, length boundaries, controls, display names, Unicode local parts, and parser failures.
- Password 8/128 boundaries, UTF-8 byte bound, controls, surrogates, no trimming/normalization, Argon2id encoded parameters, correct/wrong password, malformed hash, and rehash detection.
- Dummy verification for `None` and malformed hashes, plus bounded invalid-password rejection before Argon2 work.
- Encoded-hash parameter caps, event-loop responsiveness, 20-job admission, overload rejection, and end-to-end P50/P95 including queue time.
- Exact public exports, token hashing, four-attempt collision budget, invalid inputs without Redis calls, purpose separation, stable error mapping, and clock validation.

### Real Redis integration

- Issue/consume once, replacement, expiry, raw-token absence, purpose isolation, decoded-string and raw-byte clients, digest/current-index corruption and wrong types, missing or early-expired index TTL, Redis outage/recovery, AOF recreation, and final DB 15 cleanup.
- Fifty-iteration concurrent issue and consume races with explicit single-winner and both issue-vs-consume outcome assertions; forced cross-account/cross-purpose digest collisions prove no overwrite, and a corrupt cross-account current index cannot delete the other account's token.
- Quantitative local measurements for 300 sequential issue/consume operations and replacement with 1000 stale token hashes; results are evidence, not production capacity claims.
- Argon2 timing and resource evidence for sequential hashing/verification and 20 concurrent verifications. The local 20-operation run must complete without OOM or error and with end-to-end P95 at most 1 second; observed P50/P95 and peak process memory are recorded rather than extrapolated into an unmeasured production capacity claim.

### Independent QA

The continuous QA Agent reruns unit, security, and real-Redis suites, exercises awkward Unicode and corruption cases, records latency and cleanup metrics, reviews exception and monitoring boundaries, and reports residual risks. SMTP, HTTP, MySQL activation, browser E2E, rate limiting, and production Redis durability remain explicitly outside this slice.

## Acceptance Gate

The slice is complete only when all tests and compile checks pass, Argon2 parameters are asserted from real encoded hashes, every token concurrency test has a single-winner invariant, Redis DB 15 is empty, dependency and secret scans are retained, independent QA has reported quantitative results, specification and quality reviews approve the implementation, and the worktree is clean after commit.
