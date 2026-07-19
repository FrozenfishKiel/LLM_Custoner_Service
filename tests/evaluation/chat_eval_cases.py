from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


CaseCategory = Literal[
    "order_query",
    "logistics_query",
    "address_modify",
    "order_cancel",
    "postsale_apply",
    "boundary",
]

Expectation = Literal["read", "write", "boundary"]
FixtureKey = Literal[
    "active_order",
    "shipped_order",
    "modifiable_address_order",
    "cancelable_order",
    "postsale_eligible_order",
]


@dataclass(frozen=True)
class ChatEvalCase:
    case_id: str
    category: CaseCategory
    expectation: Expectation
    fixture_key: FixtureKey | None
    messages: tuple[str, ...]
    expected_response_substrings: tuple[str, ...] = ()
    expected_boundary_substrings: tuple[str, ...] = ()
    forbidden_response_substrings: tuple[str, ...] = ()
    expected_final_status: str | None = None
    expected_address_fragment: str | None = None
    expected_postsale_type: str | None = None


EVAL_CASES: tuple[ChatEvalCase, ...] = (
    ChatEvalCase(
        case_id="order_query_basic",
        category="order_query",
        expectation="read",
        fixture_key="active_order",
        messages=("帮我查一下订单 {order_id} 的状态",),
        expected_response_substrings=("{order_id}", "{order_status}"),
    ),
    ChatEvalCase(
        case_id="order_query_followup",
        category="order_query",
        expectation="read",
        fixture_key="active_order",
        messages=("我想看一下我的订单", "{order_id}"),
        expected_response_substrings=("{order_id}", "{order_status}"),
    ),
    ChatEvalCase(
        case_id="logistics_query_basic",
        category="logistics_query",
        expectation="read",
        fixture_key="shipped_order",
        messages=("帮我查一下订单 {order_id} 的物流",),
        expected_response_substrings=("{order_id}", "{tracking_snippet}"),
    ),
    ChatEvalCase(
        case_id="logistics_query_followup",
        category="logistics_query",
        expectation="read",
        fixture_key="shipped_order",
        messages=("我的快递到哪了", "{order_id}"),
        expected_response_substrings=("{order_id}", "{tracking_snippet}"),
    ),
    ChatEvalCase(
        case_id="address_modify_direct",
        category="address_modify",
        expectation="write",
        fixture_key="modifiable_address_order",
        messages=(
            "我要修改订单 {order_id} 的收货地址",
            "收货地址",
            "上海市浦东新区测试路 88 号 1602",
            "确认修改",
        ),
        expected_response_substrings=("修改", "成功"),
        expected_address_fragment="上海市浦东新区测试路 88 号 1602",
    ),
    ChatEvalCase(
        case_id="address_modify_name",
        category="address_modify",
        expectation="write",
        fixture_key="modifiable_address_order",
        messages=(
            "帮我改一下订单 {order_id} 的收货信息",
            "收货人姓名",
            "张三评测",
            "确认修改",
        ),
        expected_response_substrings=("修改", "成功"),
        expected_address_fragment="张三评测",
    ),
    ChatEvalCase(
        case_id="order_cancel_direct",
        category="order_cancel",
        expectation="write",
        fixture_key="cancelable_order",
        messages=("取消订单 {order_id}", "确认取消"),
        expected_response_substrings=("取消", "成功"),
        expected_final_status="已取消",
    ),
    ChatEvalCase(
        case_id="order_cancel_followup",
        category="order_cancel",
        expectation="write",
        fixture_key="cancelable_order",
        messages=("我不想要这个订单了", "{order_id}", "确认取消"),
        expected_response_substrings=("取消", "成功"),
        expected_final_status="已取消",
    ),
    ChatEvalCase(
        case_id="postsale_apply_refund",
        category="postsale_apply",
        expectation="write",
        fixture_key="postsale_eligible_order",
        messages=("我要申请订单 {order_id} 的售后", "退货退款", "商品有质量问题", "确认提交"),
        expected_response_substrings=("售后", "成功"),
        expected_postsale_type="退货退款",
    ),
    ChatEvalCase(
        case_id="postsale_apply_exchange",
        category="postsale_apply",
        expectation="write",
        fixture_key="postsale_eligible_order",
        messages=("帮我处理订单 {order_id} 的售后", "换货", "尺码不合适", "确认提交"),
        expected_response_substrings=("售后", "成功"),
        expected_postsale_type="换货",
    ),
    ChatEvalCase(
        case_id="boundary_weather",
        category="boundary",
        expectation="boundary",
        fixture_key=None,
        messages=("今天上海天气怎么样",),
        expected_boundary_substrings=("订单", "物流", "售后"),
        forbidden_response_substrings=("晴", "气温", "摄氏度"),
    ),
    ChatEvalCase(
        case_id="boundary_programming",
        category="boundary",
        expectation="boundary",
        fixture_key=None,
        messages=("帮我写一个 Python 快排",),
        expected_boundary_substrings=("订单", "物流", "售后"),
        forbidden_response_substrings=("def quicksort", "Python"),
    ),
    ChatEvalCase(
        case_id="boundary_general_chat",
        category="boundary",
        expectation="boundary",
        fixture_key=None,
        messages=("你是谁，给我讲个笑话",),
        expected_boundary_substrings=("订单", "物流", "售后"),
    ),
    ChatEvalCase(
        case_id="boundary_finance",
        category="boundary",
        expectation="boundary",
        fixture_key=None,
        messages=("帮我分析一下比特币走势",),
        expected_boundary_substrings=("订单", "物流", "售后"),
        forbidden_response_substrings=("比特币", "投资", "价格预测"),
    ),
)
