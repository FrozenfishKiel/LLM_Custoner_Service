from __future__ import annotations

import pytest
from sqlalchemy import Column, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ecs_demo.actions.security import (
    ActionSecurityError,
    audit_metadata,
    current_action_user,
    owned_order_query,
)


class Base(DeclarativeBase):
    pass


class Order(Base):
    __tablename__ = "order_info"

    order_id = Column(String(50), primary_key=True)
    user_id = Column(String(50), nullable=False)


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


def test_owned_order_query_requires_order_and_user_match() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        session.add_all(
            [
                Order(order_id="shared-order", user_id="user-a"),
                Order(order_id="other-order", user_id="user-b"),
            ]
        )
        session.commit()

    with Session(engine) as session:
        owned = owned_order_query(
            session,
            Order,
            user_id="user-a",
            order_id="shared-order",
        ).first()
        crossed = owned_order_query(
            session,
            Order,
            user_id="user-b",
            order_id="shared-order",
        ).first()

    assert owned is not None
    assert owned.user_id == "user-a"
    assert crossed is None
