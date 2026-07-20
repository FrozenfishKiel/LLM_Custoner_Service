from __future__ import annotations

from pathlib import Path

from atguigu_ai.agent.agent import _load_custom_actions


ROOT = Path(__file__).resolve().parents[3]


def test_custom_action_loader_registers_logistics_actions(monkeypatch) -> None:
    monkeypatch.setenv("MYSQL_PASSWORD", "placeholder-secret")

    registered = _load_custom_actions(ROOT / "ecs_demo" / "actions")

    assert "action_get_logistics_companys" in registered
    assert "action_get_logistics_info" in registered
