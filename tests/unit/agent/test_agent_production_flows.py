# -*- coding: utf-8 -*-
"""Agent 生产 Flow 安全与 Tracker 配置测试。"""

from __future__ import annotations

import sys
import types
from pathlib import Path

from atguigu_ai.agent.agent import Agent, AgentConfig
from atguigu_ai.agent.actions import get_action
from atguigu_ai.core.stores import RedisTrackerStore


def test_agent_load_filters_switch_user_id_by_default(tmp_path: Path) -> None:
    project = _write_minimal_project(
        tmp_path,
        endpoints_yml="tracker_store:\n  type: memory\n",
        include_switch_user_id=True,
    )

    agent = Agent.load(project, config=AgentConfig())

    assert "switch_user_id" not in agent.flows.flow_ids
    assert "query_order_detail" in agent.flows.flow_ids


def test_agent_load_can_keep_demo_identity_flow_when_explicitly_allowed(
    tmp_path: Path,
) -> None:
    project = _write_minimal_project(
        tmp_path,
        endpoints_yml="tracker_store:\n  type: memory\n",
        include_switch_user_id=True,
    )

    agent = Agent.load(
        project,
        config=AgentConfig(allow_demo_identity_flows=True),
    )

    assert "switch_user_id" in agent.flows.flow_ids


def test_agent_load_passes_full_tracker_store_config(tmp_path: Path) -> None:
    project = _write_minimal_project(
        tmp_path,
        endpoints_yml="""
tracker_store:
  type: redis
  url: redis://127.0.0.1:6379/15
  key_prefix: "tracker:"
  ttl_seconds: 120
""",
    )

    agent = Agent.load(project, config=AgentConfig())

    assert isinstance(agent.tracker_store, RedisTrackerStore)
    assert agent.tracker_store.key_prefix == "tracker:"
    assert agent.tracker_store.ttl_seconds == 120
    assert agent.tracker_store.url == "redis://127.0.0.1:6379/15"


def test_agent_load_passes_redis_standalone_credentials(tmp_path: Path) -> None:
    project = _write_minimal_project(
        tmp_path,
        endpoints_yml="""
tracker_store:
  type: redis
  host: 127.0.0.1
  port: 6379
  db: 15
  username: default
  password: test-password
  key_prefix: "tracker:"
""",
    )

    agent = Agent.load(project, config=AgentConfig())

    assert isinstance(agent.tracker_store, RedisTrackerStore)
    assert agent.tracker_store.host == "127.0.0.1"
    assert agent.tracker_store.port == 6379
    assert agent.tracker_store.db == 15
    assert agent.tracker_store.username == "default"
    assert agent.tracker_store.password == "test-password"


def test_agent_load_registers_actions_with_relative_imports_without_package_cycle(
    tmp_path: Path,
) -> None:
    project = _write_minimal_project(
        tmp_path,
        endpoints_yml="tracker_store:\n  type: memory\n",
    )
    actions_dir = project / "actions"
    actions_dir.mkdir()
    (actions_dir / "__init__.py").write_text(
        """
from .action_shipping import ActionShippingLookup

__all__ = ["ActionShippingLookup"]
""",
        encoding="utf-8",
    )
    (actions_dir / "security.py").write_text(
        "HELPER_VALUE = 'loaded-relative-helper'\n",
        encoding="utf-8",
    )
    (actions_dir / "action_shipping.py").write_text(
        """
from atguigu_ai.agent.actions import Action, ActionResult
from .security import HELPER_VALUE


class ActionShippingLookup(Action):
    @property
    def name(self) -> str:
        return "action_test_shipping_lookup"

    async def run(self, tracker, domain=None, **kwargs):
        result = ActionResult()
        result.add_response(HELPER_VALUE)
        return result
""",
        encoding="utf-8",
    )

    Agent.load(project, config=AgentConfig())

    assert get_action("action_test_shipping_lookup") is not None


def test_agent_load_does_not_reload_patched_action_support_modules(
    tmp_path: Path,
) -> None:
    project = _write_minimal_project(
        tmp_path,
        endpoints_yml="tracker_store:\n  type: memory\n",
    )
    actions_dir = project / "actions"
    actions_dir.mkdir()
    (actions_dir / "__init__.py").write_text("", encoding="utf-8")
    (actions_dir / "db.py").write_text(
        "VALUE = 'unpatched-db-module'\n",
        encoding="utf-8",
    )
    (actions_dir / "action_probe.py").write_text(
        """
from atguigu_ai.agent.actions import Action, ActionResult


class ActionProbe(Action):
    @property
    def name(self) -> str:
        return "action_test_probe"

    async def run(self, tracker, domain=None, **kwargs):
        return ActionResult()
""",
        encoding="utf-8",
    )
    previous_actions = sys.modules.get("actions")
    previous_db = sys.modules.get("actions.db")
    patched_db = types.ModuleType("actions.db")
    patched_db.VALUE = "patched-db-module"
    sys.modules["actions.db"] = patched_db
    try:
        Agent.load(project, config=AgentConfig())

        assert sys.modules["actions.db"].VALUE == "patched-db-module"
    finally:
        if previous_db is None:
            sys.modules.pop("actions.db", None)
        else:
            sys.modules["actions.db"] = previous_db
        if previous_actions is None:
            sys.modules.pop("actions", None)
        else:
            sys.modules["actions"] = previous_actions


def _write_minimal_project(
    tmp_path: Path,
    *,
    endpoints_yml: str,
    include_switch_user_id: bool = False,
) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "domain.yml").write_text("version: '3.1'\n", encoding="utf-8")
    (project / "data" / "flows").mkdir(parents=True)
    switch_user_flow = (
        """
  switch_user_id:
    name: 切换账号
    description: 切换账号、切换用户、更换用户ID
    persisted_slots:
      - user_id
    steps:
      - collect: user_id
        description: 用户id
        ask_before_filling: true
        next: END
"""
        if include_switch_user_id
        else ""
    )
    (project / "data" / "flows" / "flow_order.yml").write_text(
        f"""
version: "3.1"
flows:
{switch_user_flow}
  query_order_detail:
    name: 查询订单详情
    description: 查询订单详情
    steps:
      - action: utter_ask_order_id
        next: END
""",
        encoding="utf-8",
    )
    (project / "endpoints.yml").write_text(endpoints_yml, encoding="utf-8")
    return project
