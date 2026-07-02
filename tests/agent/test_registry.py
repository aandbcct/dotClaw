"""测试 AgentRegistry —— 系统级 Agent 目录。"""

import tempfile
from pathlib import Path

import pytest

from dotclaw.agent.registry import AgentRegistry


class TestAgentRegistry:
    """AgentRegistry 加载、查询、列表。"""

    def test_empty_registry(self) -> None:
        """空目录注册，list_all 返回空列表。"""
        with tempfile.TemporaryDirectory() as tmp:
            registry = AgentRegistry()
            registry.load_all(Path(tmp))
            assert registry.list_all() == []

    def test_get_nonexistent_returns_none(self) -> None:
        """查询不存在的 agent 返回 None。"""
        with tempfile.TemporaryDirectory() as tmp:
            registry = AgentRegistry()
            registry.load_all(Path(tmp))
            assert registry.get("nonexistent") is None

    def test_load_single_agent(self) -> None:
        """加载一个 YAML 配置，正确构造 Identity。"""
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            yaml_content = """agent_id: researcher
agent_name: "研究员"
capabilities:
  - web_search
  - data_analysis
input_modes:
  - text
output_modes:
  - text
  - json
"""
            (config_dir / "researcher.yaml").write_text(yaml_content, encoding="utf-8")

            registry = AgentRegistry()
            registry.load_all(config_dir)

            identity = registry.get("researcher")
            assert identity is not None
            assert identity.agent_id == "researcher"
            assert identity.agent_name == "研究员"
            assert identity.capabilities == ["web_search", "data_analysis"]
            assert identity.input_modes == ["text"]
            assert identity.output_modes == ["text", "json"]

    def test_load_multiple_agents(self) -> None:
        """加载多个 agent YAML，list_all 返回全部。"""
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            (config_dir / "a.yaml").write_text("agent_id: agent-a", encoding="utf-8")
            (config_dir / "b.yaml").write_text("agent_id: agent-b", encoding="utf-8")

            registry = AgentRegistry()
            registry.load_all(config_dir)

            all_ids = [i.agent_id for i in registry.list_all()]
            assert sorted(all_ids) == ["agent-a", "agent-b"]

    def test_skips_non_yaml_files(self) -> None:
        """非 .yaml 文件被忽略。"""
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            (config_dir / "readme.md").write_text("# docs", encoding="utf-8")

            registry = AgentRegistry()
            registry.load_all(config_dir)

            assert registry.list_all() == []

    def test_invalid_yaml_is_skipped(self) -> None:
        """YAML 解析失败时不崩溃，跳过该文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            (config_dir / "bad.yaml").write_text(":: invalid yaml ::][", encoding="utf-8")
            (config_dir / "good.yaml").write_text("agent_id: good", encoding="utf-8")

            registry = AgentRegistry()
            registry.load_all(config_dir)

            # bad.yaml 被跳过，good.yaml 成功加载
            assert registry.get("good") is not None
            assert registry.get("good").agent_id == "good"
