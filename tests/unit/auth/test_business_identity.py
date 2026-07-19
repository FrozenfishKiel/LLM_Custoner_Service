from __future__ import annotations

from dataclasses import dataclass

import pytest

from atguigu_ai.auth import AccountIdentity, AccountRole, AccountStatus
from atguigu_ai.auth.business_identity import (
    BusinessIdentityResolver,
    BusinessUserNotBound,
    BusinessUserBindingUnavailable,
)


@dataclass(frozen=True)
class BoundBusinessUser:
    user_id: str
    role: AccountRole = AccountRole.consumer
    account_status: AccountStatus = AccountStatus.active


class FakeBindingRepository:
    def __init__(self, binding: BoundBusinessUser | None) -> None:
        self.binding = binding
        self.account_ids: list[str] = []
        self.error: Exception | None = None

    def get_business_user_binding(self, account_id: str) -> BoundBusinessUser | None:
        self.account_ids.append(account_id)
        if self.error is not None:
            raise self.error
        return self.binding


def active_identity(status: AccountStatus = AccountStatus.active) -> AccountIdentity:
    return AccountIdentity(
        account_id="account-1",
        role=AccountRole.consumer,
        status=status,
    )


@pytest.mark.asyncio
async def test_resolver_returns_bound_active_business_user() -> None:
    repository = FakeBindingRepository(BoundBusinessUser(user_id="business-user-1"))
    resolver = BusinessIdentityResolver(repository)

    resolved = await resolver.resolve(active_identity())

    assert resolved.account_id == "account-1"
    assert resolved.user_id == "business-user-1"
    assert resolved.role is AccountRole.consumer
    assert resolved.account_status is AccountStatus.active
    assert repository.account_ids == ["account-1"]


@pytest.mark.asyncio
async def test_resolver_rejects_missing_binding() -> None:
    resolver = BusinessIdentityResolver(FakeBindingRepository(None))

    with pytest.raises(BusinessUserNotBound):
        await resolver.resolve(active_identity())


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [AccountStatus.pending, AccountStatus.disabled])
async def test_resolver_rejects_pending_or_disabled_account(status: AccountStatus) -> None:
    repository = FakeBindingRepository(BoundBusinessUser(user_id="business-user-1"))
    resolver = BusinessIdentityResolver(repository)

    with pytest.raises(PermissionError):
        await resolver.resolve(active_identity(status))

    assert repository.account_ids == []


@pytest.mark.asyncio
async def test_resolver_rejects_stale_session_when_bound_account_is_not_active() -> None:
    repository = FakeBindingRepository(
        BoundBusinessUser(
            user_id="business-user-1",
            account_status=AccountStatus.disabled,
        )
    )
    resolver = BusinessIdentityResolver(repository)

    with pytest.raises(PermissionError):
        await resolver.resolve(active_identity())

    assert repository.account_ids == ["account-1"]


@pytest.mark.asyncio
async def test_resolver_maps_repository_outage_to_unavailable() -> None:
    repository = FakeBindingRepository(BoundBusinessUser(user_id="business-user-1"))
    repository.error = RuntimeError("mysql://customer:secret@db/internal")
    resolver = BusinessIdentityResolver(repository)

    with pytest.raises(BusinessUserBindingUnavailable) as captured:
        await resolver.resolve(active_identity())

    assert str(captured.value) == "Business user binding is unavailable"
    assert captured.value.__cause__ is None
    assert "secret" not in str(captured.value)
