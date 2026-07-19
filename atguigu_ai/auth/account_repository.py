from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from .models import Account, AccountRole, AccountStatus, AuditEvent, AuditResult


class DuplicateAccountEmail(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Account email already exists")


class AccountRepositoryUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Account repository is unavailable")


@dataclass(frozen=True)
class AccountRecord:
    account_id: str
    email: str
    email_normalized: str
    password_hash: str
    role: AccountRole
    status: AccountStatus
    email_verified_at: datetime | None


_FORBIDDEN_METADATA_KEY_PARTS = frozenset({"password", "token", "session", "secret"})


class AccountRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create_pending_consumer(
        self,
        email: str,
        normalized_email: str,
        password_hash: str,
    ) -> AccountRecord:
        _require_non_blank_string(email, "email")
        _require_non_blank_string(normalized_email, "normalized_email")
        _require_non_blank_string(password_hash, "password_hash")

        account = Account(
            account_id=str(uuid4()),
            email=email,
            email_normalized=normalized_email,
            password_hash=password_hash,
            role=AccountRole.consumer.value,
            status=AccountStatus.pending.value,
            email_verified_at=None,
        )
        try:
            self._session.add(account)
            self._session.flush()
        except IntegrityError:
            raise DuplicateAccountEmail() from None
        except SQLAlchemyError:
            raise AccountRepositoryUnavailable() from None
        return _account_record(account)

    def get_by_normalized_email(self, normalized_email: str) -> AccountRecord | None:
        _require_non_blank_string(normalized_email, "normalized_email")
        try:
            account = self._session.execute(
                select(Account).where(Account.email_normalized == normalized_email)
            ).scalar_one_or_none()
        except SQLAlchemyError:
            raise AccountRepositoryUnavailable() from None
        return None if account is None else _account_record(account)

    def lock_by_account_id(self, account_id: str) -> AccountRecord | None:
        _require_non_blank_string(account_id, "account_id")
        try:
            account = self._session.execute(
                select(Account)
                .where(Account.account_id == account_id)
                .with_for_update()
            ).scalar_one_or_none()
        except SQLAlchemyError:
            raise AccountRepositoryUnavailable() from None
        return None if account is None else _account_record(account)

    def mark_email_verified(
        self,
        account_id: str,
        verified_at: datetime,
    ) -> AccountRecord | None:
        _require_non_blank_string(account_id, "account_id")
        if not isinstance(verified_at, datetime):
            raise ValueError("verified_at must be a datetime")
        verified_at = _utc_aware(verified_at)
        try:
            account = self._session.execute(
                select(Account).where(
                    Account.account_id == account_id,
                    Account.status == AccountStatus.pending.value,
                )
            ).scalar_one_or_none()
            if account is None:
                return None
            account.status = AccountStatus.active.value
            account.email_verified_at = verified_at
            self._session.flush()
        except SQLAlchemyError:
            raise AccountRepositoryUnavailable() from None
        return _account_record(account)

    def replace_password_hash(self, account_id: str, password_hash: str) -> None:
        _require_non_blank_string(account_id, "account_id")
        _require_non_blank_string(password_hash, "password_hash")
        try:
            account = self._session.get(Account, account_id)
            if account is not None:
                account.password_hash = password_hash
                self._session.flush()
        except SQLAlchemyError:
            raise AccountRepositoryUnavailable() from None
        return None

    def record_audit(
        self,
        *,
        request_id: str,
        actor_account_id: str | None,
        actor_role: AccountRole,
        event_type: str,
        target_type: str | None,
        target_id: str | None,
        result: AuditResult,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        _require_non_blank_string(request_id, "request_id")
        _require_non_blank_string(event_type, "event_type")
        if not isinstance(actor_role, AccountRole):
            raise ValueError("actor_role must be an AccountRole")
        if not isinstance(result, AuditResult):
            raise ValueError("result must be an AuditResult")
        if actor_account_id is not None:
            _require_non_blank_string(actor_account_id, "actor_account_id")
        if target_type is not None:
            _require_non_blank_string(target_type, "target_type")
        if target_id is not None:
            _require_non_blank_string(target_id, "target_id")

        sanitized_metadata = _sanitize_metadata(metadata)
        event = AuditEvent(
            event_id=str(uuid4()),
            request_id=request_id,
            actor_account_id=actor_account_id,
            actor_role=actor_role.value,
            event_type=event_type,
            target_type=target_type,
            target_id=target_id,
            result=result.value,
            metadata_json=sanitized_metadata,
        )
        try:
            self._session.add(event)
            self._session.flush()
        except SQLAlchemyError:
            raise AccountRepositoryUnavailable() from None
        return None


def _account_record(account: Account) -> AccountRecord:
    return AccountRecord(
        account_id=account.account_id,
        email=account.email,
        email_normalized=account.email_normalized,
        password_hash=account.password_hash,
        role=AccountRole(account.role),
        status=AccountStatus(account.status),
        email_verified_at=(
            None
            if account.email_verified_at is None
            else _utc_aware(account.email_verified_at)
        ),
    )


def _utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _require_non_blank_string(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-blank string")


def _sanitize_metadata(
    metadata: Mapping[str, object] | None,
) -> dict[str, Any] | None:
    if metadata is None:
        return None
    if not isinstance(metadata, Mapping):
        raise ValueError("metadata must be a mapping")
    return {
        str(key): _sanitize_metadata_value(key, value)
        for key, value in metadata.items()
    }


def _sanitize_metadata_value(key: object, value: object) -> Any:
    key_text = str(key)
    key_lower = key_text.lower()
    if any(part in key_lower for part in _FORBIDDEN_METADATA_KEY_PARTS):
        raise ValueError("metadata contains a sensitive key")
    if isinstance(value, Mapping):
        return _sanitize_metadata(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_sanitize_metadata_value("<item>", item) for item in value]
    raise ValueError("metadata contains a non-JSON value")
