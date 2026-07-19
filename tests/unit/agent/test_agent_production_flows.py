# -*- coding: utf-8 -*-
"""Agent 生产 Flow 安全与 Tracker 配置测试。"""

from __future__ import annotations

from pathlib import Path

from atguigu_ai.agent.agent import Agent, AgentConfig
from atguigu_ai.core.stores import RedisTrackerStore


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


def _write_minimal_project(tmp_path: Path, *, endpoints_yml: str) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "domain.yml").write_text("version: '3.1'\n", encoding="utf-8")
    (project / "data" / "flows").mkdir(parents=True)
    (project / "data" / "flows" / "flow_order.yml").write_text(
        """
version: "3.1"
flows:
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
