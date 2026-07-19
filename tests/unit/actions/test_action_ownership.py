from __future__ import annotations

import pytest

from ecs_demo.actions.action_logistics import ActionGetLogisticsInfo
from ecs_demo.actions.action_order import (
    ActionAskOrderId,
    ActionAskReceiveId,
    ActionAskSetReceiveInfo,
    ActionCancelOrder,
    ActionGetOrderDetail,
)
from ecs_demo.actions.action_postsale import (
    ActionApplyPostsale,
    ActionAskOrderIdAfterDelivered,
    ActionAskPostsaleReason,
    ActionCheckPostsaleEligible,
)


class Tracker:
    def __init__(self, slots):
        self.slots = slots
        self.set_values = {}

    def get_slot(self, name):
        return self.slots.get(name)

    def set_slot(self, name, value):
        self.set_values[name] = value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "slots"),
    [
        (ActionGetOrderDetail(), {"order_id": "order-1", "user_id": "attacker"}),
        (ActionGetLogisticsInfo(), {"order_id": "order-1", "user_id": "attacker"}),
        (ActionCheckPostsaleEligible(), {"order_id": "order-1", "user_id": "attacker"}),
        (ActionAskOrderId(), {"goto": "action_ask_order_id_before_shipped", "user_id": "attacker"}),
        (ActionAskReceiveId(), {"order_id": "order-1", "user_id": "attacker"}),
        (ActionAskOrderIdAfterDelivered(), {"user_id": "attacker"}),
        (ActionAskPostsaleReason(), {"order_id": "order-1", "user_id": "attacker"}),
    ],
)
async def test_query_actions_reject_missing_trusted_identity(action, slots) -> None:
    result = await action.run(Tracker(slots))

    assert result.responses[0]["text"] == "当前登录身份不可用，请重新登录后再试。"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "slots"),
    [
        (ActionCancelOrder(), {"order_id": "order-1", "user_id": "attacker"}),
        (
            ActionAskSetReceiveInfo(),
            {
                "order_id": "order-1",
                "receive_id": "receive-1",
                "set_receive_info": True,
                "user_id": "attacker",
            },
        ),
        (
            ActionApplyPostsale(),
            {
                "order_id": "order-1",
                "postsale_type": "退款",
                "postsale_reason": "不想要了",
                "user_id": "attacker",
            },
        ),
    ],
)
async def test_mutation_actions_reject_missing_trusted_identity(action, slots) -> None:
    result = await action.run(Tracker(slots))

    assert result.responses[0]["text"] == "当前登录身份不可用，请重新登录后再试。"
