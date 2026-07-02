"""AgentMessaging —— A2A 通信层。

Agent 间通信的统一入口。对标 A2A: tasks/send + tasks/cancel。

职责：
- 路由：通过 AgentRegistry 查找目标 Agent 的 Identity
- 创建：构造 Task（含上下文）
- 执行：derive Runtime → 构造 Agent → execute(task)
- 生命周期：send（同步等待）/ cancel（取消）

所有 Agent 平等，send/execute 不改变 Agent 类型，运行时决定角色。
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from .agent import Agent
from .task import Task

if TYPE_CHECKING:
    from .registry import AgentRegistry
    from .runtime import AgentRuntime

# ============================================================================
# AgentMessaging
# ============================================================================


class AgentMessaging:
    """Agent 间通信层。

    每个 Agent 实例持有一个。父 Agent 通过 send() 派生子 Agent，
    子 Agent 通过 execute() 接收并执行 Task。
    """

    def __init__(self, registry: "AgentRegistry", base_runtime: "AgentRuntime") -> None:
        """初始化。

        Args:
            registry: 全局 Agent 目录
            base_runtime: 本 Agent 的 Runtime（子 Agent 从中 derive）
        """
        self._registry: AgentRegistry = registry
        self._base_runtime: AgentRuntime = base_runtime

    # ── send（同步等待结果）──

    async def send(
        self,
        requester: str,
        target_agent_id: str,
        description: str,
        context: str = "",
        constraints: str = "",
        parent_run_id: str = "",
    ) -> Task:
        """发送 Task 给目标 Agent，阻塞等待执行完成。

        对标 A2A tasks/send（同步模式）。

        Args:
            requester: 发送方 agent_id
            target_agent_id: 接收方 agent_id
            description: 任务描述
            context: 父 Agent 传入的上下文摘要
            constraints: 约束条件
            parent_run_id: 父 AgentRun.run_id

        Returns:
            携带执行结果的 Task
        """
        from .identity import AgentIdentity

        # 构造 Task（输入侧）
        task: Task = Task(
            task_id=uuid.uuid4().hex[:12],
            requester=target_agent_id,
            description=description,
            context=context,
            constraints=constraints,
            parent_run_id=parent_run_id,
        )

        # 查 Registry
        identity: AgentIdentity | None = self._registry.get(target_agent_id)
        if identity is None:
            task.mark_failed(error=f"Agent '{target_agent_id}' not found in registry")
            return task

        # 派生 Runtime + 构造 Agent + 执行
        child_runtime: AgentRuntime = self._base_runtime.derive()
        child_agent: Agent = Agent(identity=identity, runtime=child_runtime)

        return await child_agent.execute(task)
