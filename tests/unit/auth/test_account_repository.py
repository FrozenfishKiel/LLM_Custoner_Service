from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timezone
from uuid import UUID

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from atguigu_ai.auth import (
    Account,
    AccountRecord,
    AccountRepository,
    AccountRepositoryUnavailable,
    AccountRole,
    AccountStatus,
    AuditEvent,
    AuditResult,
    AuthBase,
    DuplicateAccountEmail,
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE account (
                account_id VARCHAR(36) NOT NULL PRIMARY KEY,
                email VARCHAR(254) NOT NULL,
                email_normalized VARCHAR(254) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL,
                role VARCHAR(16) NOT NULL,
                status VARCHAR(16) NOT NULL,
                email_verified_at DATETIME,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE audit_event (
                event_id VARCHAR(36) NOT NULL PRIMARY KEY,
                request_id VARCHAR(64) NOT NULL,
                actor_account_id VARCHAR(80),
                actor_role VARCHAR(16) NOT NULL,
                event_type VARCHAR(64) NOT NULL,
                target_type VARCHAR(32),
                target_id VARCHAR(64),
                result VARCHAR(16) NOT NULL,
                metadata_json JSON,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    with Session(engine) as session:
        yield session


def test_account_record_is_an_immutable_public_value():
    verified_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    record = AccountRecord(
        account_id="account-id",
        email="User@example.com",
        email_normalized="user@example.com",
        password_hash="hash-value",
        role=AccountRole.consumer,
        status=AccountStatus.pending,
        email_verified_at=verified_at,
    )

    assert [field.name for field in fields(record)] == [
        "account_id",
        "email",
        "email_normalized",
        "password_hash",
        "role",
        "status",
        "email_verified_at",
    ]
    assert record.account_id == "account-id"
    assert record.email == "User@example.com"
    assert record.email_normalized == "user@example.com"
    assert record.password_hash == "hash-value"
    assert record.role is AccountRole.consumer
    assert record.status is AccountStatus.pending
    assert record.email_verified_at == verified_at
    with pytest.raises(FrozenInstanceError):
        record.email = "other@example.com"


def test_create_pending_consumer_inserts_consumer_pending_account(session):
    repository = AccountRepository(session)

    record = repository.create_pending_consumer(
        "User@example.com",
        "user@example.com",
        "hashed-password",
    )

    UUID(record.account_id)
    assert record.email == "User@example.com"
    assert record.email_normalized == "user@example.com"
    assert record.password_hash == "hashed-password"
    assert record.role is AccountRole.consumer
    assert record.status is AccountStatus.pending
    assert record.email_verified_at is None

    row = session.get(Account, record.account_id)
    assert row is not None
    assert row.role == AccountRole.consumer.value
    assert row.status == AccountStatus.pending.value
    assert row.password_hash == "hashed-password"


def test_duplicate_normalized_email_maps_to_stable_domain_error(session):
    repository = AccountRepository(session)
    repository.create_pending_consumer("User@example.com", "user@example.com", "hash-1")

    with pytest.raises(DuplicateAccountEmail) as captured:
        repository.create_pending_consumer(
            "Other@example.com",
            "user@example.com",
            "hash-2",
        )

    assert str(captured.value) == "Account email already exists"
    assert captured.value.__cause__ is None


def test_get_by_normalized_email_returns_none_for_missing_rows(session):
    repository = AccountRepository(session)

    assert repository.get_by_normalized_email("missing@example.com") is None


def test_get_by_normalized_email_returns_account_record_for_existing_row(session):
    repository = AccountRepository(session)
    created = repository.create_pending_consumer("User@example.com", "user@example.com", "hash")

    found = repository.get_by_normalized_email("user@example.com")

    assert found == created


def test_lock_by_account_id_uses_select_for_update_and_returns_none_when_missing(session):
    repository = AccountRepository(session)
    statements = []

    @event.listens_for(session.bind, "before_cursor_execute")
    def capture_statement(conn, cursor, statement, parameters, context, executemany):
        statements.append(context.compiled.statement)

    try:
        assert repository.lock_by_account_id("missing-account") is None
    finally:
        event.remove(session.bind, "before_cursor_execute", capture_statement)

    account_selects = [
        statement
        for statement in statements
        if getattr(statement, "is_select", False)
        and statement.get_final_froms()[0].name == Account.__tablename__
    ]
    assert account_selects
    assert any(statement._for_update_arg is not None for statement in account_selects)


def test_mark_email_verified_changes_only_pending_accounts_to_active(session):
    repository = AccountRepository(session)
    pending = repository.create_pending_consumer("P@example.com", "p@example.com", "hash")
    active = repository.create_pending_consumer("A@example.com", "a@example.com", "hash")
    disabled = repository.create_pending_consumer("D@example.com", "d@example.com", "hash")
    session.get(Account, active.account_id).status = AccountStatus.active.value
    session.get(Account, disabled.account_id).status = AccountStatus.disabled.value
    verified_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    updated = repository.mark_email_verified(pending.account_id, verified_at)

    assert updated is not None
    assert updated.status is AccountStatus.active
    assert updated.email_verified_at == verified_at
    assert session.get(Account, pending.account_id).email_verified_at == verified_at.replace(
        tzinfo=None
    )
    assert repository.mark_email_verified(active.account_id, verified_at) is None
    assert repository.mark_email_verified(disabled.account_id, verified_at) is None


def test_replace_password_hash_updates_hash_without_returning_or_logging(session, caplog):
    repository = AccountRepository(session)
    record = repository.create_pending_consumer("User@example.com", "user@example.com", "old-hash")

    result = repository.replace_password_hash(record.account_id, "new-secret-hash")

    assert result is None
    assert session.get(Account, record.account_id).password_hash == "new-secret-hash"
    assert "new-secret-hash" not in caplog.text


def test_record_audit_writes_sanitized_metadata(session):
    repository = AccountRepository(session)

    repository.record_audit(
        request_id="request-1",
        actor_account_id=None,
        actor_role=AccountRole.consumer,
        event_type="account.registered",
        target_type="account",
        target_id="account-1",
        result=AuditResult.success,
        metadata={"ip": "127.0.0.1", "attempt": 1, "nested": {"safe": True}},
    )

    event_row = session.execute(select(AuditEvent)).scalar_one()
    UUID(event_row.event_id)
    assert event_row.request_id == "request-1"
    assert event_row.actor_account_id is None
    assert event_row.actor_role == AccountRole.consumer.value
    assert event_row.event_type == "account.registered"
    assert event_row.target_type == "account"
    assert event_row.target_id == "account-1"
    assert event_row.result == AuditResult.success.value
    assert event_row.metadata_json == {
        "ip": "127.0.0.1",
        "attempt": 1,
        "nested": {"safe": True},
    }


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "token",
        "session",
        "secret",
        "password_hash",
        "raw_token",
        "session_id",
        "client_secret",
        "resetToken",
    ],
)
def test_record_audit_rejects_sensitive_metadata_keys(session, key):
    repository = AccountRepository(session)

    with pytest.raises(ValueError):
        repository.record_audit(
            request_id="request-1",
            actor_account_id="account-1",
            actor_role=AccountRole.consumer,
            event_type="account.login",
            target_type="account",
            target_id="account-1",
            result=AuditResult.failure,
            metadata={key: "unsafe"},
        )

    assert session.execute(select(AuditEvent)).all() == []


def test_repository_maps_dependency_errors_without_leaking_sql_or_hash():
    class FailingSession:
        def add(self, instance):
            raise SQLAlchemyError("INSERT password_hash='secret-hash'")

    repository = AccountRepository(FailingSession())

    with pytest.raises(AccountRepositoryUnavailable) as captured:
        repository.create_pending_consumer(
            "User@example.com",
            "user@example.com",
            "secret-hash",
        )

    assert str(captured.value) == "Account repository is unavailable"
    assert captured.value.__cause__ is None
    assert "secret-hash" not in repr(captured.value)
    assert "INSERT" not in repr(captured.value)


def test_auth_exports_preserve_existing_names_and_append_repository_names():
    import atguigu_ai.auth as auth

    assert auth.__all__ == [
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
        "CredentialTokenPurpose",
        "IssuedCredentialToken",
        "CredentialTokenStoreUnavailable",
        "RedisCredentialTokenStore",
        "DuplicateAccountEmail",
        "AccountRepositoryUnavailable",
        "AccountRecord",
        "AccountRepository",
    ]
