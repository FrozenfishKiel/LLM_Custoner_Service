import asyncio
import base64
import inspect
import threading
import time

import pytest

import atguigu_ai.auth as auth_module
import atguigu_ai.auth.credentials as credentials_module
from atguigu_ai.auth import (
    Account,
    AccountIdentity,
    AccountRole,
    AccountStatus,
    AccountUserBinding,
    AuditEvent,
    AuditResult,
    AuthBase,
    CreatedSession,
    EmailAddress,
    InvalidEmail,
    InvalidPassword,
    PasswordHasher,
    PasswordHashingOverloaded,
    PasswordPolicy,
    RedisSessionStore,
    SessionStoreUnavailable,
    normalize_email,
)


def _argon2_field(value):
    return base64.b64encode(value).decode("ascii").rstrip("=")


def _argon2_hash(*, memory=65536, time_cost=3, parallelism=4, salt_length=16, digest_length=32):
    salt = _argon2_field(b"s" * salt_length)
    digest = _argon2_field(b"d" * digest_length)
    return f"$argon2id$v=19$m={memory},t={time_cost},p={parallelism}${salt}${digest}"


def _with_nonzero_pad_bits(value):
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    replacement_index = alphabet.index(value[-1]) ^ 1
    return f"{value[:-1]}{alphabet[replacement_index]}"


def _assert_exact_parameters(callable_object, expected):
    parameters = inspect.signature(callable_object).parameters
    assert tuple(parameters) == expected
    assert all(
        parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        and parameter.default is inspect.Parameter.empty
        for parameter in parameters.values()
    )


def test_existing_and_credential_exports_are_exact():
    assert auth_module.__all__ == [
        "Account",
        "AccountRole",
        "AccountStatus",
        "AccountUserBinding",
        "AuditEvent",
        "AuditResult",
        "AuthBase",
        "AccountIdentity",
        "CreatedSession",
        "RedisSessionStore",
        "SessionStoreUnavailable",
        "EmailAddress",
        "InvalidEmail",
        "InvalidPassword",
        "PasswordHashingOverloaded",
        "PasswordPolicy",
        "PasswordHasher",
        "normalize_email",
    ]
    assert all(getattr(auth_module, name) is not None for name in auth_module.__all__)


@pytest.mark.parametrize(
    ("raw", "display", "normalized"),
    [
        (" User.Name+tag@Example.COM ", "User.Name+tag@example.com", "user.name+tag@example.com"),
        ("user@bücher.example", "user@xn--bcher-kva.example", "user@xn--bcher-kva.example"),
        ("CaseSensitive@EXAMPLE.COM", "CaseSensitive@example.com", "casesensitive@example.com"),
    ],
)
def test_normalize_email_returns_ascii_display_and_complete_casefold(raw, display, normalized):
    assert normalize_email(raw) == EmailAddress(display=display, normalized=normalized)


def test_email_length_boundary_accepts_254_ascii_characters():
    local = "a" * 64
    domain = ".".join(["b" * 63, "c" * 63, "d" * 57, "com"])
    address = f"{local}@{domain}"
    assert len(address) == 254
    assert normalize_email(address).display == address


@pytest.mark.parametrize(
    "value",
    [
        None,
        42,
        "",
        "   ",
        "Name <user@example.com>",
        "user\n@example.com",
        "user@example.com\x7f",
        "\u00a0user@example.com\u00a0",
        "眉ser@example.com",
        "user@",
        "a" * 65 + "@example.com",
        "a" * 245 + "@example.com",
    ],
)
def test_invalid_email_has_one_sanitized_public_error(value):
    with pytest.raises(InvalidEmail) as captured:
        normalize_email(value)
    assert str(captured.value) == "Invalid email address"
    assert captured.value.__cause__ is None
    assert "example" not in str(captured.value)


@pytest.mark.parametrize("password", ["12345678", "x" * 128, "界" * 128, "密码安全1234"])
def test_password_policy_accepts_length_and_unicode_boundaries(password):
    assert PasswordPolicy().validate(password) is None


@pytest.mark.parametrize(
    "password",
    [
        None,
        42,
        "1234567",
        "密码安全123",
        "x" * 129,
        "valid123\x00",
        "valid123\x1f",
        "valid123\x7f",
        "valid123\ud800",
    ],
)
def test_password_policy_rejects_type_scalar_control_and_surrogate_limits(password):
    with pytest.raises(InvalidPassword) as captured:
        PasswordPolicy().validate(password)
    assert str(captured.value) == "Password does not meet requirements"
    assert captured.value.__cause__ is None


def test_password_policy_does_not_trim_or_normalize():
    policy = PasswordPolicy()
    policy.validate(" pass word ")
    policy.validate("e\u0301password")
    policy.validate("épassword")


@pytest.mark.asyncio
async def test_hash_uses_exact_argon2id_parameters_and_verifies():
    hasher = PasswordHasher()
    encoded = await hasher.hash("correct horse battery staple")
    assert encoded.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    salt, digest = encoded.rsplit("$", 2)[-2:]
    assert len(credentials_module._decode_argon2_field(salt)) == 16
    assert len(credentials_module._decode_argon2_field(digest)) == 32
    assert await hasher.verify(encoded, "correct horse battery staple") is True
    assert await hasher.verify(encoded, "wrong horse battery staple") is False
    assert hasher.needs_rehash(encoded) is False


@pytest.mark.asyncio
async def test_hash_validates_before_argon2(monkeypatch):
    called = False

    def forbidden_hash(password):
        nonlocal called
        called = True
        raise AssertionError(password)

    hasher = PasswordHasher()
    monkeypatch.setattr(hasher._argon2, "hash", forbidden_hash)
    with pytest.raises(InvalidPassword, match="^Password does not meet requirements$"):
        await hasher.hash("short")
    assert called is False


@pytest.mark.asyncio
async def test_verify_policy_invalid_password_returns_false_without_argon2(monkeypatch):
    called = False

    def forbidden_verify(encoded, password):
        nonlocal called
        called = True
        raise AssertionError((encoded, password))

    hasher = PasswordHasher()
    monkeypatch.setattr(hasher._argon2, "verify", forbidden_verify)
    assert await hasher.verify(None, "x" * 129) is False
    assert await hasher.verify(_argon2_hash(), "x" * 129) is False
    assert called is False


@pytest.mark.parametrize(
    "encoded",
    [
        pytest.param(None, id="none"),
        pytest.param("not-an-argon2-hash", id="not-argon2"),
        pytest.param(
            "$argon2i$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$ZGlnaWVzdGRpZ2VzdGRpZ2VzdA",
            id="unsupported-type",
        ),
        pytest.param(
            "$argon2id$v=16$m=65536,t=3,p=4$c2FsdHNhbHQ$ZGlnaWVzdGRpZ2VzdGRpZ2VzdA",
            id="unsupported-version",
        ),
        pytest.param(
            "$argon2id$v=19$m=65537,t=3,p=4$c2FsdHNhbHQ$ZGlnaWVzdGRpZ2VzdGRpZ2VzdA",
            id="memory-over-cap",
        ),
        pytest.param(
            "$argon2id$v=19$m=65536,t=4,p=4$c2FsdHNhbHQ$ZGlnaWVzdGRpZ2VzdGRpZ2VzdA",
            id="time-over-cap",
        ),
        pytest.param(
            "$argon2id$v=19$m=65536,t=3,p=5$c2FsdHNhbHQ$ZGlnaWVzdGRpZ2VzdGRpZ2VzdA",
            id="parallelism-over-cap",
        ),
        pytest.param(
            "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$ZGlnaWVzdGRpZ2VzdGRpZ2VzdA",
            id="salt-under-min",
        ),
        pytest.param("$argon2id$v=19$m=65536,t=3,p=4$" + "A" * 100000, id="total-length-over-cap"),
    ],
)
@pytest.mark.asyncio
async def test_none_malformed_unsupported_and_over_cap_hashes_take_one_dummy_verify(monkeypatch, encoded):
    calls = []

    def recording_verify(selected_hash, password):
        calls.append((selected_hash, password))
        return False

    hasher = PasswordHasher()
    monkeypatch.setattr(hasher._argon2, "verify", recording_verify)
    assert await hasher.verify(encoded, "valid-password") is False
    assert calls == [(credentials_module._DUMMY_HASH, "valid-password")]
    assert encoded not in [item[0] for item in calls]


@pytest.mark.parametrize(
    ("encoded", "expected_length"),
    [
        (_argon2_hash(memory=7), None),
        (_argon2_hash(time_cost=0), None),
        (_argon2_hash(parallelism=0), None),
        (_argon2_hash(salt_length=33), None),
        (_argon2_hash(digest_length=15), None),
        (_argon2_hash(digest_length=65), None),
        (_argon2_hash(memory="0" * 500 + "65536"), 513),
    ],
)
def test_eligible_hash_rejects_every_lower_upper_and_total_length_cap(encoded, expected_length):
    salt, digest = encoded.rsplit("$", 2)[-2:]
    assert _argon2_field(base64.b64decode(salt + "=" * (-len(salt) % 4))) == salt
    assert _argon2_field(base64.b64decode(digest + "=" * (-len(digest) % 4))) == digest
    if expected_length is not None:
        assert len(encoded) >= expected_length
    assert credentials_module._eligible_hash(encoded) is False


@pytest.mark.asyncio
async def test_noncanonical_argon2_base64_uses_dummy_verify(monkeypatch):
    encoded = _argon2_hash()
    salt, digest = encoded.rsplit("$", 2)[-2:]
    noncanonical_salt = _with_nonzero_pad_bits(salt)
    noncanonical_digest = _with_nonzero_pad_bits(digest)
    noncanonical = encoded.replace(salt, noncanonical_salt).replace(digest, noncanonical_digest)
    assert base64.b64decode(noncanonical_salt + "=" * (-len(noncanonical_salt) % 4), validate=True)
    assert base64.b64decode(noncanonical_digest + "=" * (-len(noncanonical_digest) % 4), validate=True)
    assert _argon2_field(base64.b64decode(noncanonical_salt + "=" * (-len(noncanonical_salt) % 4))) != noncanonical_salt
    assert _argon2_field(base64.b64decode(noncanonical_digest + "=" * (-len(noncanonical_digest) % 4))) != noncanonical_digest
    assert credentials_module._eligible_hash(noncanonical) is False

    calls = []

    def recording_verify(selected_hash, password):
        calls.append((selected_hash, password))
        return False

    hasher = PasswordHasher()
    monkeypatch.setattr(hasher._argon2, "verify", recording_verify)
    assert await hasher.verify(noncanonical, "valid-password") is False
    assert calls == [(credentials_module._DUMMY_HASH, "valid-password")]


@pytest.mark.asyncio
async def test_committed_dummy_hash_is_real_bounded_argon2id():
    assert credentials_module._eligible_hash(credentials_module._DUMMY_HASH) is True
    with pytest.raises(credentials_module.VerifyMismatchError):
        credentials_module.Argon2PasswordHasher().verify(
            credentials_module._DUMMY_HASH,
            "definitely-not-the-dummy-password",
        )
    assert await PasswordHasher().verify(None, "valid-password") is False


def test_needs_rehash_is_false_for_malformed_or_over_cap_hashes():
    hasher = PasswordHasher()
    assert hasher.needs_rehash("malformed") is False
    assert hasher.needs_rehash(
        "$argon2id$v=19$m=999999,t=3,p=4$c2FsdHNhbHQ$ZGlnaWVzdGRpZ2VzdGRpZ2VzdA"
    ) is False


@pytest.mark.asyncio
async def test_argon2_runs_off_the_event_loop(monkeypatch):
    main_thread = threading.get_ident()
    worker_threads = []

    def slow_hash(password):
        worker_threads.append(threading.get_ident())
        time.sleep(0.05)
        return "encoded"

    hasher = PasswordHasher()
    monkeypatch.setattr(hasher._argon2, "hash", slow_hash)
    ticks = 0

    async def ticker():
        nonlocal ticks
        for _ in range(5):
            await asyncio.sleep(0.01)
            ticks += 1

    encoded, _ = await asyncio.gather(hasher.hash("valid-password"), ticker())
    assert encoded == "encoded"
    assert ticks == 5
    assert worker_threads and all(item != main_thread for item in worker_threads)


@pytest.mark.asyncio
async def test_true_argon2_verify_runs_off_the_event_loop(monkeypatch):
    main_thread = threading.get_ident()
    worker_threads = []

    def slow_verify(encoded, password):
        worker_threads.append(threading.get_ident())
        time.sleep(0.05)
        return True

    hasher = PasswordHasher()
    monkeypatch.setattr(hasher._argon2, "verify", slow_verify)
    ticks = 0

    async def ticker():
        nonlocal ticks
        for _ in range(5):
            await asyncio.sleep(0.01)
            ticks += 1

    verified, _ = await asyncio.gather(
        hasher.verify(_argon2_hash(), "valid-password"),
        ticker(),
    )
    assert verified is True
    assert ticks == 5
    assert worker_threads and all(item != main_thread for item in worker_threads)


def test_process_limiters_are_module_level_shared_bounded_semaphores():
    limiters = [
        value
        for value in vars(credentials_module).values()
        if isinstance(value, threading.BoundedSemaphore)
    ]
    assert len(limiters) == 2
    assert sorted(limiter._initial_value for limiter in limiters) == [4, 20]

    hashers = [PasswordHasher(), PasswordHasher()]
    assert all(
        not any(isinstance(value, threading.BoundedSemaphore) for value in vars(hasher).values())
        for hasher in hashers
    )

    async def observe_limiter_ids_from_fresh_loop():
        PasswordHasher()
        return tuple(id(limiter) for limiter in limiters)

    assert asyncio.run(observe_limiter_ids_from_fresh_loop()) == asyncio.run(
        observe_limiter_ids_from_fresh_loop()
    )


@pytest.mark.asyncio
async def test_twenty_jobs_are_admitted_four_at_a_time_and_excess_fails_immediately(monkeypatch):
    entered = 0
    maximum_running = 0
    lock = threading.Lock()
    release = threading.Event()

    def blocked_hash(password):
        nonlocal entered, maximum_running
        with lock:
            entered += 1
            maximum_running = max(maximum_running, entered)
        release.wait(timeout=5)
        with lock:
            entered -= 1
        return "encoded"

    def blocked_verify(encoded, password):
        blocked_hash(password)
        return True

    hashers = [PasswordHasher(), PasswordHasher()]
    for hasher in hashers:
        monkeypatch.setattr(hasher._argon2, "hash", blocked_hash)
        monkeypatch.setattr(hasher._argon2, "verify", blocked_verify)
    tasks = []
    expected = []
    for index in range(20):
        hasher = hashers[index % 2]
        password = f"valid-password-{index}"
        if index % 4 < 2:
            tasks.append(asyncio.create_task(hasher.hash(password)))
            expected.append("encoded")
        else:
            tasks.append(asyncio.create_task(hasher.verify(_argon2_hash(), password)))
            expected.append(True)
    results = None
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            if maximum_running == 4:
                break
        assert maximum_running == 4
        started = time.perf_counter()
        overflow_hasher = PasswordHasher()

        def forbidden_verify(encoded, password):
            raise AssertionError((encoded, password))

        monkeypatch.setattr(overflow_hasher._argon2, "verify", forbidden_verify)
        with pytest.raises(PasswordHashingOverloaded, match="^Password hashing capacity is unavailable$"):
            await asyncio.wait_for(
                overflow_hasher.verify(_argon2_hash(), "overflow-password"),
                timeout=0.2,
            )
        assert time.perf_counter() - started < 0.1
    finally:
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=6)
    assert results == expected


def test_public_password_interface_is_exact():
    _assert_exact_parameters(normalize_email, ("value",))
    _assert_exact_parameters(PasswordPolicy.validate, ("self", "password"))
    _assert_exact_parameters(PasswordHasher.hash, ("self", "password"))
    _assert_exact_parameters(PasswordHasher.verify, ("self", "password_hash", "password"))
    _assert_exact_parameters(PasswordHasher.needs_rehash, ("self", "password_hash"))
    assert inspect.iscoroutinefunction(PasswordHasher.hash)
    assert inspect.iscoroutinefunction(PasswordHasher.verify)
    assert not inspect.iscoroutinefunction(PasswordHasher.needs_rehash)
    assert {name for name in vars(PasswordPolicy) if not name.startswith("_")} == {"validate"}
    assert {name for name in vars(PasswordHasher) if not name.startswith("_")} == {
        "hash",
        "verify",
        "needs_rehash",
    }
