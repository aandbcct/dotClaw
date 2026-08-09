"""PR8 两个真实 Session 工作流的受控编排。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .context_reliability import ContextRunConfig, _compression_history
from .context_runtime_fixture import BenchmarkCompactor, CapturingLLM, CompletingTool, FixedTokenizerCounter, ToolThenFinalLLM, build_engine, session_with_history
from dotclaw.runtime.application.dto import ContextBundle
from dotclaw.runtime.application.execution import RunExecutionView
from dotclaw.runtime.application.history_compaction import HistoryCompactionRequest, HistoryCompactionResult
from dotclaw.runtime.application.ports import LLMOutputPort, LLMPort
from dotclaw.runtime.application.request_factory import create_run_request
from dotclaw.runtime.domain.facts import MessageRole, RunMessage, RunMessageKind
from dotclaw.runtime.domain.state import RunOutcome


_PREFERENCE_MARKERS: tuple[str, ...] = ("简洁", "验证")
_COMPRESSION_CONSTRAINT: str = "关键约束：先验证"


class _PreferenceWorkflowLLM(CapturingLLM):
    """偏好工作流的确定性 LLM 替身，分别冻结首轮记录与后续交付。"""

    async def complete(
        self,
        context: ContextBundle,
        execution: RunExecutionView,
        output_port: LLMOutputPort | None = None,
    ) -> RunMessage:
        """记录真实 ContextBundle，并依据调用轮次返回可验证交付。"""
        self.calls += 1
        self.contexts.append(context)
        content = "已记录偏好：回答简洁，并保留验证步骤。" if self.calls == 1 else "简洁方案：先实施，再执行验证步骤。"
        return RunMessage(f"pr8-preference-{self.calls}", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, content)


class _CompressionFollowupLLM(CapturingLLM):
    """压缩后续任务的确定性 LLM 替身，交付必须显式遵守冻结约束。"""

    async def complete(
        self,
        context: ContextBundle,
        execution: RunExecutionView,
        output_port: LLMOutputPort | None = None,
    ) -> RunMessage:
        """记录下一请求实际输入，并返回带关键约束的后续交付。"""
        self.calls += 1
        self.contexts.append(context)
        return RunMessage("pr8-compression-followup", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "遵守关键约束：先验证，再继续执行。")


class _BusinessHistoryCompactor(BenchmarkCompactor):
    """PR8 压缩替身：只冻结摘要正文，仍复用生产压缩选择和提交路径。"""

    async def compact_history(self, request: HistoryCompactionRequest) -> HistoryCompactionResult:
        """记录真实压缩请求，并把关键历史约束写入压缩摘要。"""
        self.requests.append(request)
        return HistoryCompactionResult(f"PR8 压缩摘要；{_COMPRESSION_CONSTRAINT}。")


@dataclass(frozen=True)
class WorkflowResult:
    """真实工作流的最小业务结果，供统一 BenchmarkSample 转换。"""

    workflow_id: str
    version: str
    passed: bool
    run_id: str | None
    used_compression: bool
    preference_committed: bool
    preference_applied: bool
    key_constraint_retained: bool
    final_output: str


def _context_text(llm: CapturingLLM) -> str:
    """读取替身捕获的最后一次真实 ContextBundle 文本，缺失即为空。"""
    if not llm.contexts:
        return ""
    return "\n".join(message.content for message in llm.contexts[-1].messages)


async def run_preference_aware_followup(root: Path, llm: LLMPort | None = None) -> WorkflowResult:
    """在临时 Session 中真实提交首轮偏好，并由下一次 create_run_request() 读取。"""
    manager, session, _ = await session_with_history(root, count=0)
    counter, compactor = FixedTokenizerCounter(), BenchmarkCompactor()
    runtime_llm = llm or _PreferenceWorkflowLLM()
    engine, _ = build_engine(root, manager, counter, compactor, llm=runtime_llm)
    first_request = create_run_request(session, session.agent_id, "我的偏好：后续回答必须简洁，并保留验证步骤。")
    first = await engine.execute(first_request)
    persisted = await manager.load(session.id)
    if persisted is None:
        return WorkflowResult("preference_aware_followup", "1", False, None, False, False, False, False, "")
    followup = create_run_request(persisted, session.agent_id, "请按已记录偏好给出后续方案")
    second = await engine.execute(followup)
    output = "" if second.final_message is None else second.final_message.content
    committed = len(persisted.conversations) == 1 and all(marker in persisted.conversations[0].user_query + persisted.conversations[0].final_answer for marker in _PREFERENCE_MARKERS)
    # 后续请求由 Runtime 实际执行；检查其冻结历史，避免把可观测替身作为真实 LLM 的前提。
    context_reused = all(marker in "\n".join(message.content for message in followup.conversation.messages) for marker in _PREFERENCE_MARKERS)
    applied = context_reused and all(marker in output for marker in _PREFERENCE_MARKERS)
    passed = first.state.outcome() is RunOutcome.COMPLETED and second.state.outcome() is RunOutcome.COMPLETED and committed and applied
    return WorkflowResult("preference_aware_followup", "1", passed, second.run_id, False, committed, applied, False, output)


async def run_compressed_history_continuation(root: Path, llm: LLMPort | None = None) -> WorkflowResult:
    """以生产压缩路径处理持久化历史，并验证下一请求读取活动压缩快照。"""
    # 复用 PR6 已验证的真实工具历史构造，随后仍在本工作流执行压缩与下一请求。
    config = ContextRunConfig("business", "cl100k_base", 0, 1, 0, 1, 0, 1)
    manager, session, request, _, _ = await _compression_history(root, config)
    counter, compactor = FixedTokenizerCounter(), _BusinessHistoryCompactor()
    runtime_llm = llm or ToolThenFinalLLM()
    engine, _ = build_engine(root, manager, counter, compactor, llm=runtime_llm, context_window=70, tool=CompletingTool())
    first = await engine.execute(request)
    persisted = await manager.load(session.id)
    if persisted is None:
        return WorkflowResult("compressed_history_continuation", "1", False, None, False, False, False, False, "")
    followup = create_run_request(persisted, session.agent_id, "继续遵守关键约束")
    followup_llm = llm if llm is not None else _CompressionFollowupLLM()
    followup_engine, _ = build_engine(root, manager, FixedTokenizerCounter(), BenchmarkCompactor(), llm=followup_llm)
    second = await followup_engine.execute(followup)
    output = "" if second.final_message is None else second.final_message.content
    used = followup.conversation.compressed_history is not None and _COMPRESSION_CONSTRAINT in followup.conversation.compressed_history.content
    # 压缩摘要已冻结进实际后续请求，真实 LLM 与确定性替身均消费同一份输入。
    context_reused = followup.conversation.compressed_history is not None and _COMPRESSION_CONSTRAINT in followup.conversation.compressed_history.content
    retained = used and context_reused and _COMPRESSION_CONSTRAINT in output
    passed = first.state.outcome() is RunOutcome.COMPLETED and second.state.outcome() is RunOutcome.COMPLETED and retained and bool(compactor.requests)
    return WorkflowResult("compressed_history_continuation", "1", passed, second.run_id, used, False, False, retained, output)
