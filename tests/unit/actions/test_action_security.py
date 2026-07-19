from __future__ import annotations

import pytest

from ecs_demo.actions.security import ActionSecurityError, audit_metadata, current_action_user


class Tracker:
    def __init__(self, slots=None):
        self.slots = slots or {}

    def get_slot(self, name):
        return self.slots.get(name)


def test_current_action_user_uses_trusted_kwargs() -> None:
    context = current_action_user(
        Tracker({"user_id": "attacker"}),
        account_id="account-1",
        user_id="business-user-1",
        account_role="consumer",
        request_id="request-1",
    )

    assert context.account_id == "account-1"
    assert context.user_id == "business-user-1"
    assert context.role == "consumer"
    assert context.request_id == "request-1"


def test_current_action_user_rejects_missing_trusted_identity_by_default() -> None:
    with pytest.raises(ActionSecurityError):
        current_action_user(Tracker({"user_id": "1001"}))


def test_current_action_user_demo_fallback_must_be_explicit() -> None:
    context = current_action_user(
        Tracker({"user_id": "1001"}),
        allow_demo_identity_fallback=True,
        account_id="demo-account",
    )

    assert context.account_id == "demo-account"
    assert context.user_id == "1001"


def test_audit_metadata_is_small_and_non_sensitive() -> None:
    metadata = audit_metadata(
        action_name="action_cancel_order",
        order_id="order-1",
        previous_status="待发货",
        raw_token="secret",
        csrf="secret",
        nested={"token": "secret"},
        empty=None,
    )

    assert metadata == {
        "action_name": "action_cancel_order",
        "order_id": "order-1",
        "previous_status": "待发货",
    }
