from __future__ import annotations

from pathlib import Path

from atguigu_ai.dialogue_understanding.flow import FlowLoader


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_postsale_flow_collects_order_id_used_by_postsale_actions() -> None:
    flows = FlowLoader().load(PROJECT_ROOT / "ecs_demo" / "data" / "flows")
    flow = flows.get_flow("apply_postsale")

    assert flow is not None
    assert "order_id" in flow.get_slots_to_collect()
    assert "postsale_order_id" not in flow.get_slots_to_collect()


def test_address_modify_branches_target_registered_steps() -> None:
    flows = FlowLoader().load(PROJECT_ROOT / "ecs_demo" / "data" / "flows")
    flow = flows.get_flow("modify_order_receive_info")

    assert flow is not None
    branch_step = flow.get_step("select_modify_content")
    assert branch_step is not None

    branch_targets = {
        branch["then"]
        for branch in branch_step.next
        if isinstance(branch, dict) and branch.get("if", "").startswith("slots.modify_content")
    }

    assert branch_targets == {
        "collect_receiver_name",
        "collect_receiver_phone",
        "collect_receive_province",
    }
    for target in branch_targets:
        assert flow.get_step(target) is not None
