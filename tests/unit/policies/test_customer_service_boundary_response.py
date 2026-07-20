from __future__ import annotations

import pytest

from atguigu_ai.core.tracker import DialogueStateTracker
from atguigu_ai.dialogue_understanding.stack.stack_frame import CannotHandleStackFrame
from atguigu_ai.policies.enterprise_search_policy import EnterpriseSearchPolicy


@pytest.mark.asyncio
async def test_cannot_handle_response_points_back_to_customer_service_scope() -> None:
    tracker = DialogueStateTracker(sender_id="boundary-response-test")
    tracker.dialogue_stack.push(
        CannotHandleStackFrame(reason="out_of_customer_service_scope")
    )
    policy = EnterpriseSearchPolicy()

    prediction = await policy.predict(tracker)

    assert prediction.action == "action_send_text"
    text = prediction.metadata["text"]
    assert "订单" in text
    assert "物流" in text
    assert "售后" in text
