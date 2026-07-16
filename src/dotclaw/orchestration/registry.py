"""AgentRegistry —— 系统级 Agent 目录。

启动时扫描 agent 配置目录，将所有 AgentIdentity 加载到内存。
Spawn 时通过 agent_id 查找 Identity，无需在 Identity 内部维护 sub_agents 映射。
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..agent.identity import AgentIdentity, load_agent_config

logger = logging.getLogger("dotclaw.agent.registry")


class AgentRegistry:
    """系统级 Agent 目录。

    所有 Agent 平等注册，无角色预设。Agent 间关系由运行时协商决定。

    对标 A2A Service Discovery：通过 agent_id 查找 AgentCard（Identity）。
    """

    def __init__(self) -> None:
        self._identities: dict[str, AgentIdentity] = {}

    # ── 加载 ──

    def load_all(self, agent_config_dir: Path) -> None:
        """扫描目录下所有 .yaml 文件，构造 AgentIdentity 注册。

        Args:
            agent_config_dir: Agent 配置目录路径
        """
        if not agent_config_dir.exists() or not agent_config_dir.is_dir():
            return

        for path in agent_config_dir.glob("*.yaml"):
            try:
                identity: AgentIdentity = load_agent_config(path=path)
                self._identities[identity.agent_id] = identity
                logger.info("已注册 Agent [%s]: %s", identity.agent_id, identity.agent_name)
            except Exception:
                logger.warning("跳过无效 Agent 配置: %s", path)

    # ── 查询 ──

    def register(self, identity: AgentIdentity) -> None:
        """直接注入 Identity（用于测试或程序化注册）。

        Args:
            identity: AgentIdentity 实例
        """
        self._identities[identity.agent_id] = identity

    def get(self, agent_id: str) -> AgentIdentity | None:
        """按 agent_id 查询 Identity。

        Args:
            agent_id: Agent 唯一标识

        Returns:
            AgentIdentity，不存在则返回 None
        """
        return self._identities.get(agent_id)

    def list_all(self) -> list[AgentIdentity]:
        """返回所有已注册的 AgentIdentity。"""
        return list(self._identities.values())
