from __future__ import annotations

import pytest

from atguigu_ai.core.tracker import DialogueStateTracker, UserMessage
from atguigu_ai.dialogue_understanding.commands.answer_commands import CannotHandleCommand
from atguigu_ai.dialogue_understanding.commands.flow_commands import StartFlowCommand
from atguigu_ai.dialogue_understanding.commands.slot_commands import SetSlotCommand
from atguigu_ai.dialogue_understanding.generator import LLMCommandGenerator
from atguigu_ai.shared.llm.base_client import LLMClient, LLMResponse


class ExplodingLLM(LLMClient):
    def __init__(self) -> None:
        super().__init__(model="exploding", api_key="test")

    async def complete(self, messages, **kwargs):
        raise AssertionError("out-of-scope customer service requests should not call the LLM")

    def complete_sync(self, messages, **kwargs):
        raise NotImplementedError


class FixedCommandLLM(LLMClient):
    def __init__(self, content: str) -> None:
        super().__init__(model="fixed-command", api_key="test")
        self.content = content

    async def complete(self, messages, **kwargs):
        return LLMResponse(content=self.content, model=self.model)

    def complete_sync(self, messages, **kwargs):
        raise NotImplementedError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "今天上海天气怎么样",
        "帮我写一个 Python 快排",
        "你是谁，给我讲个笑话",
        "帮我分析一下比特币走势",
    ],
)
async def test_obvious_out_of_scope_customer_service_requests_are_guarded_before_llm(
    message: str,
) -> None:
    tracker = DialogueStateTracker(sender_id="boundary-test")
    tracker.update_with_message(UserMessage(text=message, sender_id=tracker.sender_id))
    generator = LLMCommandGenerator(llm_client=ExplodingLLM())

    result = await generator.generate(tracker)

    assert len(result.commands) == 1
    assert isinstance(result.commands[0], CannotHandleCommand)
    assert result.commands[0].reason == "out_of_customer_service_scope"


@pytest.mark.asyncio
async def test_order_id_in_user_message_is_preserved_when_starting_order_flow() -> None:
    tracker = DialogueStateTracker(sender_id="order-slot-test")
    tracker.update_with_message(
        UserMessage(
            text="取消订单 eval-order-cancel",
            sender_id=tracker.sender_id,
        )
    )
    generator = LLMCommandGenerator(
        llm_client=FixedCommandLLM("start flow cancel_order")
    )

    result = await generator.generate(tracker)

    assert any(
        isinstance(command, StartFlowCommand) and command.flow == "cancel_order"
        for command in result.commands
    )
    assert any(
        isinstance(command, SetSlotCommand)
        and command.name == "order_id"
        and command.value == "eval-order-cancel"
        for command in result.commands
    )
