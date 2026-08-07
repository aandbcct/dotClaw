"""PR6 真实 Runtime 装配夹具：所有结论读取临时持久化事实。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from pathlib import Path

import tiktoken

from dotclaw.context import ContextDependencies, build_context_provider
from dotclaw.context.ports import UserProfile
from dotclaw.runtime.adapters import ApprovalRepositoryAdapter, CheckpointRepositoryAdapter, RunRepositoryAdapter, SessionConversationProjector
from dotclaw.runtime.application.approval_service import ApprovalService
from dotclaw.runtime.application.cancellation_service import CancellationService
from dotclaw.runtime.application.context_budget import TokenCountRequest, TokenCountResult
from dotclaw.runtime.application.dto import ContextBundle, RunRequest, ToolInvocation, ToolResult, ToolResultStatus
from dotclaw.runtime.application.engine import RuntimeEngine
from dotclaw.runtime.application.execution import RunExecutionView
from dotclaw.runtime.application.history_compaction import HistoryCompactionRequest, HistoryCompactionResult
from dotclaw.runtime.application.ports import ContextPort, HistoryCompactorPort, LLMPort, LLMOutputPort, LLMUnavailableError, RunPolicyPort, ToolPort
from dotclaw.runtime.application.request_factory import create_run_request
from dotclaw.runtime.domain.context import ContextOwner
from dotclaw.runtime.domain.facts import AgentPolicySnapshot, MessageRole, RunMessage, RunMessageKind
from dotclaw.session.session import Session, SessionManager


@dataclass
class ObservedKnowledgeBase:
    """唯一可变外部 Slot 来源，记录生产 Provider 实际读取次数。"""

    value: str
    delay_ms: int = 0
    load_count: int = 0

    async def search(self, query: str) -> str:
        """返回可审计检索文本，并在每次真实读取时递增计数。"""
        self.load_count += 1
        if self.delay_ms:
            await asyncio.sleep(self.delay_ms / 1000)
        return self.value


@dataclass(frozen=True)
class BenchmarkAgentDescriptor:
    """为 GLOBAL Slot 提供确定性的真实 Agent 目录记录。"""

    agent_id: str = "global"
    agent_name: str = "GLOBAL"
    description: str = "GLOBAL"
    capabilities: list[str] = field(default_factory=list)


class BenchmarkAgentDirectory:
    """仅作为 GLOBAL 外部来源，返回固定可审计目录。"""

    def list_all(self) -> tuple[BenchmarkAgentDescriptor, ...]:
        """返回一条带 GLOBAL 标识的目录记录。"""
        return (BenchmarkAgentDescriptor(),)


class BenchmarkPolicy(RunPolicyPort):
    """冻结真实 Engine 的上下文窗口与 Agent 身份 Slot。"""

    def __init__(self, context_window: int = 200) -> None:
        self._context_window: int = context_window

    async def resolve(self, request: RunRequest) -> AgentPolicySnapshot:
        """为每个真实 Run 写入可区分的 Agent 提示词。"""
        return AgentPolicySnapshot(
            request.agent_id,
            "pr6",
            "pr6",
            4,
            policy_data={
                "context_window": self._context_window,
                "tokenizer_encoding": "cl100k_base",
                "system_prompt": f"AGENT:{request.agent_id}",
            },
        )


class FixedTokenizerCounter:
    """使用指定 tiktoken 编码统计实际 TokenCountRequest。"""

    def __init__(self, encoding_name: str = "cl100k_base") -> None:
        self._encoding_name: str = encoding_name
        self.requests: list[TokenCountRequest] = []

    async def count(self, request: TokenCountRequest) -> TokenCountResult:
        """按实际系统文本、历史、当前输入、运行消息及工具 Schema 重新计数。"""
        self.requests.append(request)
        if request.tokenizer_encoding != self._encoding_name:
            raise ValueError("PR6 tokenizer 与固定配置不一致")
        encoding = tiktoken.get_encoding(self._encoding_name)
        parts: list[str] = [*request.system_contents, request.history_summary, request.current_user_message.content]
        parts.extend(message.content for message in request.history_messages)
        parts.extend(message.content for message in request.run_messages)
        parts.extend(json.dumps(tool.parameters, ensure_ascii=False, sort_keys=True) + tool.name + tool.description for tool in request.tools)
        return TokenCountResult(sum(len(encoding.encode(part)) for part in parts) + request.protocol_overhead_tokens)


class BenchmarkCompactor(HistoryCompactorPort):
    """只替换摘要生成，生产压缩选择、暂存和投影流程保持不变。"""

    def __init__(self) -> None:
        self.requests: list[HistoryCompactionRequest] = []

    async def compact_history(self, request: HistoryCompactionRequest) -> HistoryCompactionResult:
        """返回确定性摘要，便于只验证预算和污染边界。"""
        self.requests.append(request)
        return HistoryCompactionResult("PR6 固定摘要")


class CapturingLLM(LLMPort):
    """记录生产 ContextBundle 的最终消息序列，然后按指定结果收口。"""

    def __init__(self, unavailable_first: bool = False, fail_first: bool = False) -> None:
        self._unavailable_first: bool = unavailable_first
        self._fail_first: bool = fail_first
        self.calls: int = 0
        self.contexts: list[ContextBundle] = []

    async def complete(self, context: ContextBundle, execution: RunExecutionView, output_port: LLMOutputPort | None = None) -> RunMessage:
        """记录实际输入；首次不可用时留下可公开恢复的 checkpoint。"""
        self.calls += 1
        self.contexts.append(context)
        if self._unavailable_first and self.calls == 1:
            raise LLMUnavailableError("PR6 冷恢复")
        if self._fail_first and self.calls == 1:
            raise RuntimeError("PR6 失败")
        return RunMessage("pr6-final", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "完成")

    async def cancel(self, run_id: str) -> None:
        """PR6 LLM 不保留额外取消资源。"""


class BlockingLLM(CapturingLLM):
    """等待取消信号的 LLM，用于驱动真实运行中的取消路径。"""

    def __init__(self) -> None:
        super().__init__()
        self.started: asyncio.Event = asyncio.Event()
        self.cancelled: asyncio.Event = asyncio.Event()
        self.run_id: str = ""

    async def complete(self, context: ContextBundle, execution: RunExecutionView, output_port: LLMOutputPort | None = None) -> RunMessage:
        """记录生产输入后等待 RuntimeEngine 的公开 cancel 调用。"""
        self.calls += 1
        self.contexts.append(context)
        self.run_id = execution.run_id
        self.started.set()
        await self.cancelled.wait()
        return RunMessage("pr6-cancelled", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "取消")

    async def cancel(self, run_id: str) -> None:
        """收到 RuntimeEngine 的取消传播后解除等待。"""
        self.cancelled.set()


class NoTool(ToolPort):
    """PR6 场景不进入工具执行。"""

    async def execute(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        """拒绝未预期的工具调用。"""
        return ToolResult(invocation.call.call_id, ToolResultStatus.FAILED)

    async def cancel(self, run_id: str) -> None:
        """PR6 工具端口没有运行资源。"""


class ForceRebuildContextPort(ContextPort):
    """Benchmark 反事实适配器：在回放期调用同一生产 Provider 重建 Slot。"""

    def __init__(self, inner: ContextPort) -> None:
        self._inner: ContextPort = inner

    async def build(self, request: RunRequest, execution: RunExecutionView) -> ContextBundle:
        """仅替换只读视图的回放标记，不创建字符串分支。"""
        return await self._inner.build(request, replace(execution, replay_active_context=False, active_context_version=None))

    async def release_scope(self, owner: ContextOwner, owner_key: str) -> None:
        """透传生命周期释放。"""
        await self._inner.release_scope(owner, owner_key)

    async def release_all(self) -> None:
        """透传全局释放。"""
        await self._inner.release_all()

    def request_refresh(self, slot_id: str, owner: ContextOwner, owner_key: str) -> None:
        """透传精确刷新。"""
        self._inner.request_refresh(slot_id, owner, owner_key)


def build_engine(root: Path, manager: SessionManager, counter: FixedTokenizerCounter, compactor: BenchmarkCompactor, llm: LLMPort | None = None, knowledge: ObservedKnowledgeBase | None = None, force_rebuild: bool = False, context_window: int = 200, profile: str = "SESSION:default") -> tuple[RuntimeEngine, RunRepositoryAdapter]:
    """构造生产 ContextProvider、文件仓储和 RuntimeEngine。"""
    provider: ContextPort = build_context_provider(ContextDependencies(knowledge_base=knowledge, user_profile=UserProfile(name=profile), agent_registry=BenchmarkAgentDirectory()))
    if force_rebuild:
        provider = ForceRebuildContextPort(provider)
    repository = RunRepositoryAdapter(root, SessionConversationProjector(manager))
    engine = RuntimeEngine(repository, CheckpointRepositoryAdapter(root), provider, llm or CapturingLLM(), NoTool(), BenchmarkPolicy(context_window), ApprovalService(ApprovalRepositoryAdapter(root)), CancellationService(), token_counter=counter, history_compactor=compactor)
    return engine, repository


async def session_with_history(root: Path, count: int = 3, agent_id: str = "agent-pr6", session_marker: str = "SESSION:default") -> tuple[SessionManager, Session, RunRequest]:
    """在临时根创建真实 Session、Conversation 与本次 Run 请求。"""
    manager = SessionManager(root)
    session = await manager.create(agent_id=agent_id)
    for index in range(count):
        conversation = session.add_conversation(f"{session_marker} 旧问题-{index}", f"{session_marker} 旧回答-{index}", [f"old-{index}"])
        # 固定语料的时间和标识是实验输入，避免把 UUID/时钟误判为上下文漂移。
        conversation.conversation_id = f"pr6-conversation-{index}"
        conversation.created_at = f"2026-01-01T00:00:0{index}"
    await manager.save(session)
    return manager, session, create_run_request(session, agent_id, "RUN:current")
