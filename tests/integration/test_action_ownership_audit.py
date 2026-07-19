from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).resolve().parents[2]
ECS_DEMO = ROOT / "ecs_demo"
if str(ECS_DEMO) not in sys.path:
    sys.path.insert(0, str(ECS_DEMO))

from actions.action_order import ActionCancelOrder
from actions.action_postsale import ActionApplyPostsale
from actions.db_table_class import (
    Base,
    OrderDetail,
    OrderInfo,
    OrderStatus,
    Postsale,
    PostsaleStatus,
    ProductCategory,
    ReceiveInfo,
    Region,
    SkuInfo,
    UserInfo,
)
from atguigu_ai.auth import Account, AccountUserBinding, AuditEvent
from tests.integration.test_account_migration import (
    _alembic_config,
    _isolated_mysql_database,
    _target_name,
)


pytestmark = pytest.mark.integration


@dataclass
class ActionDbFixture:
    session_factory: sessionmaker


class Tracker:
    def __init__(self, slots):
        self.slots = slots

    def get_slot(self, name):
        return self.slots.get(name)

    def set_slot(self, name, value):
        self.slots[name] = value


@pytest.fixture
def action_db_fixture(monkeypatch):
    import actions.db as action_db

    with _isolated_mysql_database() as database_url:
        config = _alembic_config(database_url)
        config.attributes["connection_url"] = database_url
        monkeypatch.setenv("MIGRATION_EXPECTED_TARGET", _target_name(database_url))
        command.upgrade(config, "head")

        engine = action_db.create_engine(database_url, pool_pre_ping=True)
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        monkeypatch.setattr(action_db, "SessionLocal", session_factory)
        _seed_two_users(session_factory)
        try:
            yield ActionDbFixture(session_factory=session_factory)
        finally:
            engine.dispose()


def _seed_two_users(session_factory: sessionmaker) -> None:
    now = datetime(2026, 7, 19, 10, 0, 0)
    with session_factory() as session:
        session.add_all(
            [
                UserInfo(user_id="user-a"),
                UserInfo(user_id="user-b"),
                Region(province="浙江省", city="杭州市", district="西湖区"),
                OrderStatus(order_status="待发货", status_code=310),
                OrderStatus(order_status="已取消", status_code=100),
                OrderStatus(order_status="售后中", status_code=400),
                PostsaleStatus(
                    postsale_status="退款待审核",
                    is_refund=1,
                    is_return=0,
                    is_exchange=0,
                    status_code=410,
                ),
                ProductCategory(product_category="手机"),
                SkuInfo(
                    sku_id="sku-a",
                    sku_name="测试手机",
                    sku_price=Decimal("1999.00"),
                    sku_category="手机",
                    sku_count=10,
                ),
                ReceiveInfo(
                    receive_id="receive-a",
                    user_id="user-a",
                    receiver_name="用户A",
                    receiver_phone="13000000001",
                    receive_province="浙江省",
                    receive_city="杭州市",
                    receive_district="西湖区",
                    receive_street_address="一号路",
                ),
                ReceiveInfo(
                    receive_id="receive-b",
                    user_id="user-b",
                    receiver_name="用户B",
                    receiver_phone="13000000002",
                    receive_province="浙江省",
                    receive_city="杭州市",
                    receive_district="西湖区",
                    receive_street_address="二号路",
                ),
                Account(
                    account_id="account-a",
                    email="a@example.test",
                    email_normalized="a@example.test",
                    password_hash="hash-a",
                    role="consumer",
                    status="active",
                    email_verified_at=now,
                ),
                AccountUserBinding(
                    account_id="account-a",
                    user_id="user-a",
                    seed_version="seed-v1",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                OrderInfo(
                    order_id="order-a",
                    create_time=now,
                    user_id="user-a",
                    receive_id="receive-a",
                    order_status="待发货",
                ),
                OrderInfo(
                    order_id="order-b",
                    create_time=now,
                    user_id="user-b",
                    receive_id="receive-b",
                    order_status="待发货",
                ),
                OrderDetail(
                    order_detail_id="detail-a",
                    order_id="order-a",
                    sku_id="sku-a",
                    sku_name="测试手机",
                    sku_count=1,
                    total_amount=Decimal("1999.00"),
                    final_amount=Decimal("1999.00"),
                    discount_amount=Decimal("0.00"),
                ),
            ]
        )
        session.commit()


@pytest.mark.asyncio
async def test_cancel_order_is_owned_idempotent_and_audited(action_db_fixture) -> None:
    action = ActionCancelOrder()

    first = await action.run(
        Tracker({"order_id": "order-a"}),
        account_id="account-a",
        user_id="user-a",
        account_role="consumer",
        request_id="request-cancel-1",
    )
    second = await action.run(
        Tracker({"order_id": "order-a"}),
        account_id="account-a",
        user_id="user-a",
        account_role="consumer",
        request_id="request-cancel-2",
    )
    crossed = await action.run(
        Tracker({"order_id": "order-b"}),
        account_id="account-a",
        user_id="user-a",
        account_role="consumer",
        request_id="request-cancel-3",
    )

    assert first.responses[0]["text"].startswith("订单已取消")
    assert second.responses[0]["text"] == "订单已取消"
    assert crossed.responses[0]["text"] == "未找到该订单，请检查订单号是否正确。"

    with Session(action_db_fixture.session_factory.kw["bind"]) as session:
        order_a_status = session.scalar(
            select(OrderInfo.order_status).where(OrderInfo.order_id == "order-a")
        )
        order_b_status = session.scalar(
            select(OrderInfo.order_status).where(OrderInfo.order_id == "order-b")
        )
        audit_events = session.execute(
            select(AuditEvent).order_by(AuditEvent.request_id)
        ).scalars().all()
        temp_db_count = session.execute(text("SELECT 1")).scalar()

    assert order_a_status == "已取消"
    assert order_b_status == "待发货"
    assert temp_db_count == 1
    assert [(event.event_type, event.result, event.target_id) for event in audit_events] == [
        ("business.order.cancel", "success", "order-a"),
        ("business.order.cancel", "success", "order-a"),
        ("business.order.cancel", "failure", "order-b"),
    ]


@pytest.mark.asyncio
async def test_apply_postsale_is_owned_idempotent_and_audited(action_db_fixture) -> None:
    action = ActionApplyPostsale()
    slots = {
        "order_id": "order-a",
        "postsale_type": "退款",
        "postsale_reason": "不想要了",
    }

    first = await action.run(
        Tracker(dict(slots)),
        account_id="account-a",
        user_id="user-a",
        account_role="consumer",
        request_id="request-postsale-1",
    )
    second = await action.run(
        Tracker(dict(slots)),
        account_id="account-a",
        user_id="user-a",
        account_role="consumer",
        request_id="request-postsale-2",
    )
    crossed = await action.run(
        Tracker({**slots, "order_id": "order-b"}),
        account_id="account-a",
        user_id="user-a",
        account_role="consumer",
        request_id="request-postsale-3",
    )

    assert "申请已提交" in first.responses[0]["text"]
    assert "申请已提交" in second.responses[0]["text"]
    assert crossed.responses[0]["text"] == "未找到该订单。"

    with Session(action_db_fixture.session_factory.kw["bind"]) as session:
        postsale_rows = session.execute(select(Postsale)).scalars().all()
        order_a_status = session.scalar(
            select(OrderInfo.order_status).where(OrderInfo.order_id == "order-a")
        )
        order_b_status = session.scalar(
            select(OrderInfo.order_status).where(OrderInfo.order_id == "order-b")
        )
        audit_events = session.execute(
            select(AuditEvent)
            .where(AuditEvent.event_type == "business.postsale.apply")
            .order_by(AuditEvent.request_id)
        ).scalars().all()

    assert len(postsale_rows) == 1
    assert postsale_rows[0].order_detail_id == "detail-a"
    assert order_a_status == "售后中"
    assert order_b_status == "待发货"
    assert [(event.result, event.target_id) for event in audit_events] == [
        ("success", "order-a"),
        ("success", "order-a"),
        ("failure", "order-b"),
    ]
