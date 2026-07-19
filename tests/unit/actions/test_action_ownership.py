from __future__ import annotations

import pytest

from ecs_demo.actions.action_logistics import ActionGetLogisticsInfo
from ecs_demo.actions.action_order import ActionGetOrderDetail
from ecs_demo.actions.action_postsale import ActionCheckPostsaleEligible


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
    ],
)
async def test_query_actions_reject_missing_trusted_identity(action, slots) -> None:
    result = await action.run(Tracker(slots))

    assert result.responses[0]["text"] == "当前登录身份不可用，请重新登录后再试。"
