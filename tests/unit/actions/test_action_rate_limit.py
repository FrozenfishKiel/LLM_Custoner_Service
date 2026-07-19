from __future__ import annotations

from types import SimpleNamespace

import pytest

from ecs_demo.actions.action_order import ActionAskSetReceiveInfo, ActionCancelOrder
from ecs_demo.actions.action_postsale import ActionApplyPostsale
from ecs_demo.actions.security import ACTION_MUTATION_RULE
from atguigu_ai.rate_limit import RateLimitStoreUnavailable


class Tracker:
    def __init__(self, slots):
        self.slots = slots

    def get_slot(self, name):
        return self.slots.get(name)


class BlockingRateLimiter:
    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self.calls: list[tuple[str, str]] = []

    async def check(self, rule, subject: str):
        self.calls.append((rule.name, subject))
        if self.unavailable:
            raise RateLimitStoreUnavailable()
        return SimpleNamespace(
            allowed=False,
            limit=rule.limit,
            remaining=0,
            retry_after_seconds=60,
            reset_after_seconds=60,
            rule_name=rule.name,
        )


@pytest.mark.asyncio
async def test_cancel_order_rate_limit_blocks_before_database_access() -> None:
    limiter = BlockingRateLimiter()

    result = await ActionCancelOrder().run(
        Tracker({"order_id": "order-1"}),
        account_id="account-1",
        user_id="user-1",
        rate_limiter=limiter,
    )

    assert result.responses == [{"text": "系统繁忙，请稍后重试"}]
    assert limiter.calls == [(ACTION_MUTATION_RULE.name, "account-1:action_cancel_order")]


@pytest.mark.asyncio
async def test_set_receive_info_rate_limit_blocks_only_confirmed_write_branch() -> None:
    limiter = BlockingRateLimiter()

    result = await ActionAskSetReceiveInfo().run(
        Tracker(
            {
                "order_id": "order-1",
                "receive_id": "modify",
                "set_receive_info": True,
                "receiver_name": "张三",
                "receiver_phone": "13800000000",
                "receive_province": "浙江省",
                "receive_city": "杭州市",
                "receive_district": "西湖区",
                "receive_street_address": "文三路 1 号",
            }
        ),
        account_id="account-1",
        user_id="user-1",
        rate_limiter=limiter,
    )

    assert result.responses == [{"text": "系统繁忙，请稍后重试"}]
    assert limiter.calls == [(ACTION_MUTATION_RULE.name, "account-1:action_ask_set_receive_info")]


@pytest.mark.asyncio
async def test_apply_postsale_rate_limit_blocks_before_database_access() -> None:
    limiter = BlockingRateLimiter()

    result = await ActionApplyPostsale().run(
        Tracker(
            {
                "order_id": "order-1",
                "postsale_type": "退款",
                "postsale_reason": "不想要了",
            }
        ),
        account_id="account-1",
        user_id="user-1",
        rate_limiter=limiter,
    )

    assert result.responses == [{"text": "系统繁忙，请稍后重试"}]
    assert limiter.calls == [(ACTION_MUTATION_RULE.name, "account-1:action_apply_postsale")]


@pytest.mark.asyncio
async def test_action_rate_limiter_outage_is_sanitized() -> None:
    limiter = BlockingRateLimiter(unavailable=True)

    result = await ActionCancelOrder().run(
        Tracker({"order_id": "order-1"}),
        account_id="account-1",
        user_id="user-1",
        rate_limiter=limiter,
    )

    assert result.responses == [{"text": "系统繁忙，请稍后重试"}]
