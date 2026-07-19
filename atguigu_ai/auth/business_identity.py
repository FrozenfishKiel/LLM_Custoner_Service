from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import AccountRole, AccountStatus
from .session import AccountIdentity


class BusinessUserNotBound(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Business user binding is missing")


class BusinessUserBindingUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Business user binding is unavailable")


@dataclass(frozen=True)
class BusinessUserIdentity:
    account_id: str
    user_id: str
    role: AccountRole
    account_status: AccountStatus


class BusinessIdentityResolver:
    def __init__(self, repository: Any) -> None:
        self._repository = repository

    async def resolve(self, identity: AccountIdentity) -> BusinessUserIdentity:
        if not isinstance(identity, AccountIdentity):
            raise ValueError("identity must be an AccountIdentity")
        if identity.status is not AccountStatus.active:
            raise PermissionError("Account is not active")
        try:
            binding = self._repository.get_business_user_binding(identity.account_id)
        except Exception:
            raise BusinessUserBindingUnavailable() from None
        if binding is None:
            raise BusinessUserNotBound() from None
        user_id = getattr(binding, "user_id", None)
        if not isinstance(user_id, str) or not user_id.strip():
            raise BusinessUserBindingUnavailable() from None
        role = getattr(binding, "role", identity.role)
        account_status = getattr(binding, "account_status", identity.status)
        if not isinstance(role, AccountRole) or not isinstance(account_status, AccountStatus):
            raise BusinessUserBindingUnavailable() from None
        if account_status is not AccountStatus.active:
            raise PermissionError("Account is not active")
        return BusinessUserIdentity(
            account_id=identity.account_id,
            user_id=user_id,
            role=role,
            account_status=account_status,
        )


__all__ = [
    "BusinessIdentityResolver",
    "BusinessUserBindingUnavailable",
    "BusinessUserIdentity",
    "BusinessUserNotBound",
]
