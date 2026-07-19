from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote

from atguigu_ai.email import EmailDeliveryUnavailable

from .account_repository import AccountRecord, DuplicateAccountEmail
from .credential_tokens import (
    CredentialTokenPurpose,
    CredentialTokenStoreUnavailable,
    RedisCredentialTokenStore,
)
from .credentials import (
    InvalidEmail,
    InvalidPassword,
    PasswordHasher,
    PasswordHashingOverloaded,
    normalize_email,
)
from .models import AccountRole, AccountStatus, AuditResult
from .session import (
    AccountIdentity,
    CreatedSession,
    RedisSessionStore,
    SessionStoreUnavailable,
)


class InvalidCredentials(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Invalid email or password")


class DuplicateRegistration(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Account email already exists")


class AuthServiceUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Authentication service is unavailable")


@dataclass(frozen=True)
class RegistrationAccepted:
    account_id: str
    email: str


@dataclass(frozen=True)
class LoginAccepted:
    identity: AccountIdentity
    session: CreatedSession


@dataclass(frozen=True)
class PasswordResetAccepted:
    accepted: bool = True


class AuthService:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], Any],
        password_hasher: PasswordHasher,
        credential_tokens: RedisCredentialTokenStore,
        sessions: RedisSessionStore,
        email_delivery: Any,
        public_base_url: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._uow_factory = uow_factory
        self._password_hasher = password_hasher
        self._credential_tokens = credential_tokens
        self._sessions = sessions
        self._email_delivery = email_delivery
        self._public_base_url = _normalize_public_base_url(public_base_url)
        self._clock = clock

    async def register(self, email: str, password: str) -> RegistrationAccepted:
        try:
            normalized_email = normalize_email(email)
            password_hash = await self._password_hasher.hash(password)
            with self._uow_factory() as uow:
                try:
                    account = uow.repository.create_pending_consumer(
                        normalized_email.display,
                        normalized_email.normalized,
                        password_hash,
                    )
                    issued = await self._credential_tokens.issue(
                        account.account_id,
                        CredentialTokenPurpose.verify_email,
                    )
                    await self._email_delivery.send_verification_email(
                        account.email,
                        self._url("verify-email", issued.token),
                    )
                    uow.commit()
                except DuplicateAccountEmail:
                    _rollback(uow)
                    raise DuplicateRegistration() from None
                except Exception:
                    _rollback(uow)
                    raise
            return RegistrationAccepted(account_id=account.account_id, email=account.email)
        except DuplicateRegistration:
            raise
        except (InvalidEmail, InvalidPassword):
            raise
        except Exception:
            raise AuthServiceUnavailable() from None

    async def verify_email(self, token: str) -> AccountIdentity | None:
        try:
            account_id = await self._credential_tokens.consume(
                token,
                CredentialTokenPurpose.verify_email,
            )
            if account_id is None:
                return None
            with self._uow_factory() as uow:
                try:
                    account = uow.repository.lock_by_account_id(account_id)
                    if account is None or account.status is not AccountStatus.pending:
                        return None
                    updated = uow.repository.mark_email_verified(account.account_id, self._now())
                    if updated is None:
                        return None
                    uow.repository.record_audit(
                        request_id="auth-service",
                        actor_account_id=updated.account_id,
                        actor_role=updated.role,
                        event_type="account.email_verified",
                        target_type="account",
                        target_id=updated.account_id,
                        result=AuditResult.success,
                        metadata=None,
                    )
                    uow.commit()
                except Exception:
                    _rollback(uow)
                    raise
            return _identity(updated)
        except Exception:
            raise AuthServiceUnavailable() from None

    async def login(self, email: str, password: str) -> LoginAccepted:
        try:
            try:
                normalized_email = normalize_email(email)
            except InvalidEmail:
                await self._password_hasher.verify(None, password)
                raise InvalidCredentials() from None

            with self._uow_factory() as uow:
                account = uow.repository.get_by_normalized_email(normalized_email.normalized)

            eligible = _eligible_for_login(account)
            password_hash = account.password_hash if eligible and account is not None else None
            verified = await self._password_hasher.verify(password_hash, password)
            if not eligible or account is None or not verified:
                raise InvalidCredentials() from None

            identity = _identity(account)
            created_session = await self._sessions.create(identity)
            return LoginAccepted(identity=identity, session=created_session)
        except InvalidCredentials:
            raise
        except Exception:
            raise AuthServiceUnavailable() from None

    async def logout(self, session_token: str) -> None:
        if not isinstance(session_token, str) or not session_token.strip():
            return None
        try:
            await self._sessions.revoke(session_token)
            return None
        except Exception:
            raise AuthServiceUnavailable() from None

    async def forgot_password(self, email: str) -> PasswordResetAccepted:
        try:
            try:
                normalized_email = normalize_email(email)
            except InvalidEmail:
                return PasswordResetAccepted()

            with self._uow_factory() as uow:
                account = uow.repository.get_by_normalized_email(normalized_email.normalized)
            if account is None or account.status is not AccountStatus.active:
                return PasswordResetAccepted()

            issued = await self._credential_tokens.issue(
                account.account_id,
                CredentialTokenPurpose.reset_password,
            )
            await self._email_delivery.send_password_reset_email(
                account.email,
                self._url("reset-password", issued.token),
            )
            return PasswordResetAccepted()
        except Exception:
            raise AuthServiceUnavailable() from None

    async def reset_password(self, token: str, new_password: str) -> PasswordResetAccepted | None:
        try:
            account_id = await self._credential_tokens.consume(
                token,
                CredentialTokenPurpose.reset_password,
            )
            if account_id is None:
                return None
            with self._uow_factory() as uow:
                try:
                    account = uow.repository.lock_by_account_id(account_id)
                    if account is None or account.status is not AccountStatus.active:
                        return None
                    new_password_hash = await self._password_hasher.hash(new_password)
                    await self._sessions.revoke_all(account.account_id)
                    uow.repository.replace_password_hash(account.account_id, new_password_hash)
                    uow.repository.record_audit(
                        request_id="auth-service",
                        actor_account_id=account.account_id,
                        actor_role=account.role,
                        event_type="account.password_reset",
                        target_type="account",
                        target_id=account.account_id,
                        result=AuditResult.success,
                        metadata=None,
                    )
                    uow.commit()
                except Exception:
                    _rollback(uow)
                    raise
            return PasswordResetAccepted()
        except InvalidPassword:
            raise
        except PasswordHashingOverloaded:
            raise AuthServiceUnavailable() from None
        except Exception:
            raise AuthServiceUnavailable() from None

    def _url(self, path: str, token: str) -> str:
        return f"{self._public_base_url}/{path}?token={quote(token, safe='')}"

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime):
            raise AuthServiceUnavailable() from None
        if now.tzinfo is None or now.utcoffset() is None:
            raise AuthServiceUnavailable() from None
        return now.astimezone(timezone.utc)


def _normalize_public_base_url(public_base_url: str) -> str:
    if not isinstance(public_base_url, str) or not public_base_url.strip():
        raise ValueError("public_base_url must be a non-blank string")
    normalized = public_base_url.strip().rstrip("/")
    if not (normalized.startswith("http://") or normalized.startswith("https://")):
        raise ValueError("public_base_url must start with http:// or https://")
    return normalized


def _eligible_for_login(account: AccountRecord | None) -> bool:
    return (
        account is not None
        and account.status is AccountStatus.active
        and account.role in {AccountRole.consumer, AccountRole.admin}
    )


def _identity(account: AccountRecord) -> AccountIdentity:
    return AccountIdentity(
        account_id=account.account_id,
        role=account.role,
        status=account.status,
    )


def _rollback(uow: Any) -> None:
    try:
        uow.rollback()
    except Exception:
        pass


__all__ = [
    "InvalidCredentials",
    "DuplicateRegistration",
    "AuthServiceUnavailable",
    "RegistrationAccepted",
    "LoginAccepted",
    "PasswordResetAccepted",
    "AuthService",
]
