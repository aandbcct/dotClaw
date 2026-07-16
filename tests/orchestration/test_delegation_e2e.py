"""同进程 delegation 的真实工具链端到端测试。

本模块作用：用脚本化 Fake LLM 驱动真实 Runtime、ToolExecutor、Dispatcher 和
Broker，验证 harness 能完成 source 与 target 的双向闭环，不依赖外部模型服务。
"""

from __future__ import annotations

import asyncio
import json


from dotclaw.agent.agent import Agent
from dotclaw.agent.identity import AgentIdentity
from dotclaw.agent.slotContext import ContextAssembler
from dotclaw.agent.slotContextImp import AvailableAgentsSlot, IdentitySlot, ToolsSlot
from dotclaw.journal import Journal
from dotclaw.llm.base import ChatChunk, Message, ToolCall
from dotclaw.orchestration.dispatcher import AgentDispatcher
from dotclaw.orchestration.message_broker import TaskMessageBroker
from dotclaw.orchestration.registry import AgentRegistry
from dotclaw.runtime.runtime import Runtime
from dotclaw.session.agent_run import AgentRun
from dotclaw.session.session import Session
from dotclaw.tools.approval import ApprovalManager
from dotclaw.tools.builtin.task_tool import get_task_handlers
from dotclaw.tools.executor import ToolExecutor
from dotclaw.tools.registry import ToolRegistry
from dotclaw.tools.base import ToolDefinition


class FakeSessionManager:
    """测试用 SessionManager，保留 target Session 以验证隔离。"""

    def __init__(self) -> None:
        self.sessions: list[Session] = []

    async def create(self, title: str, model: str, agent_id: str) -> Session:
        """创建内存 target Session。"""
        session: Session = Session(id=f"target-{len(self.sessions) + 1}", title=title, model=model, agent_id=agent_id)
        self.sessions.append(session)
        return session


class FakeRunManager:
    """测试用 RunManager，不引入持久化副作用。"""

    async def list(self, session_id: str, end_status: str) -> list[AgentRun]:
        """测试场景不包含历史等待 Run。"""
        return []

    async def save(self, run: AgentRun, session_id: str) -> None:
        """接收 Runtime 审计记录。"""
        return None


class FakeStateStore:
    """测试用状态仓储；本闭环不会触发审批等待。"""

    async def save(self, session_id: str, snapshot: "StateSnapshot") -> None:
        """满足 Runtime 依赖接口。"""
        return None


class ScriptedDelegationLLM:
    """按 source/target 身份脚本化返回真实工具调用的 Fake LLM。"""

    def __init__(self) -> None:
        self._source_step: int = 0
        self._target_step: int = 0

    async def chat(self, messages: list[Message], tools: list[ToolDefinition] | None, model: str, purpose: str, stream: bool):
        """根据 Identity system prompt 和当前步骤生成下一项模型行为。"""
        system_message: str = messages[0].content
        if system_message.splitlines()[0] == "TARGET":
            response: tuple[str, dict[str, str] | str] = self._next_target()
        else:
            response = self._next_source(messages)
        if response[0] == "final":
            yield ChatChunk(content=str(response[1]), is_final=True, finish_reason="stop")
            return
        arguments: str = json.dumps(response[1], ensure_ascii=False)
        yield ChatChunk(tool_call=ToolCall(id=f"call-{self._source_step}-{self._target_step}", name=response[0], arguments=arguments))
        yield ChatChunk(is_final=True, finish_reason="tool_calls")

    def _next_source(self, messages: list[Message]) -> tuple[str, dict[str, str] | str]:
        """生成 source 的委托、等待、回复、汇总步骤。"""
        step: int = self._source_step
        self._source_step += 1
        if step == 0:
            return "delegate", {"target_agent_id": "target", "title": "分析", "objective": "给出结论"}
        if step == 1:
            return "wait_task", {"timeout": 1}
        if step == 2:
            return "task_send_message", {"message_type": "reply", "payload": "补充数据"}
        if step == 3:
            return "wait_task", {"timeout": 1}
        return "final", "source 汇总：target 已完成"

    def _next_target(self) -> tuple[str, dict[str, str] | str]:
        """生成 target 的提问、等待与最终成果步骤。"""
        step: int = self._target_step
        self._target_step += 1
        if step == 0:
            return "task_send_message", {"message_type": "question", "payload": "请补充数据"}
        if step == 1:
            return "wait_task", {"timeout": 1}
        return "final", "target 成果"



def _build_runtime() -> tuple[Runtime, Agent, FakeSessionManager, TaskMessageBroker]:
    """装配真实 delegation 依赖与最小测试替身。"""
    registry: AgentRegistry = AgentRegistry()
    source_identity: AgentIdentity = AgentIdentity(
        agent_id="source",
        agent_name="Source",
        system_prompt_template="SOURCE",
        allowed_tools=["delegate", "task_send_message", "wait_task", "task_status", "cancel_task"],
        max_loop_steps=12,
    )
    target_identity: AgentIdentity = AgentIdentity(
        agent_id="target",
        agent_name="Target",
        system_prompt_template="TARGET",
        allowed_tools=["task_send_message", "wait_task", "task_status"],
        max_loop_steps=12,
    )
    registry.register(source_identity)
    registry.register(target_identity)
    tool_registry: ToolRegistry = ToolRegistry()
    for handler in get_task_handlers():
        tool_registry.register(handler)
    executor: ToolExecutor = ToolExecutor(tool_registry, ApprovalManager())
    sessions: FakeSessionManager = FakeSessionManager()
    broker: TaskMessageBroker = TaskMessageBroker()
    runtime: Runtime = Runtime(
        llm=ScriptedDelegationLLM(),
        tool_executor=executor,
        assembler=ContextAssembler([IdentitySlot(), ToolsSlot(), AvailableAgentsSlot()]),
        agent_registry=registry,
        session_mgr=sessions,
        run_mgr=FakeRunManager(),
        journal=Journal(),
        state_store=FakeStateStore(),
    )
    agent: Agent = Agent(source_identity, runtime=runtime, dispatcher=AgentDispatcher(broker))
    return runtime, agent, sessions, broker


def test_real_tool_chain_completes_bidirectional_delegation() -> None:
    """真实工具执行链完成 source 提问回复、target 成果和 source 汇总。"""
    async def scenario() -> None:
        runtime, source, sessions, broker = _build_runtime()
        source_session: Session = Session(id="source-session", title="源会话", agent_id="source")
        answer, _ = await runtime.run(source_session, source, "请委托并汇总")
        assert answer == "source 汇总：target 已完成"
        assert len(sessions.sessions) == 1
        task = await broker.active_task_for_source(source_session.id)
        assert task is None
        assert "delegate" not in (await _target_prompt(runtime, sessions.sessions[0])).split()
    asyncio.run(scenario())


async def _target_prompt(runtime: Runtime, session: Session) -> str:
    """构建 target system prompt，验证不暴露 AvailableAgentsSlot。"""
    target = runtime.agent_registry.get("target")
    assert target is not None
    target_runtime: Runtime = runtime.derive(delegation_endpoint="target", delegation_task_id="task")
    agent: Agent = Agent(target, runtime=target_runtime)
    return await target_runtime._build_system_prompt(session.id, agent, "任务")


from dotclaw.runtime.state_store import StateSnapshot
