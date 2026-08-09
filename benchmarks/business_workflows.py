"""PR8 两个真实 Session 工作流的受控编排。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .context_reliability import ContextRunConfig, _compression_history
from .context_runtime_fixture import BenchmarkCompactor, CapturingLLM, CompletingTool, FixedTokenizerCounter, ToolThenFinalLLM, build_engine, session_with_history
from dotclaw.runtime.application.request_factory import create_run_request
from dotclaw.runtime.domain.state import RunOutcome


@dataclass(frozen=True)
class WorkflowResult:
    """真实工作流的最小业务结果，供统一 BenchmarkSample 转换。"""

    workflow_id: str
    version: str
    passed: bool
    run_id: str | None
    used_compression: bool
    preference_committed: bool


async def run_preference_aware_followup(root: Path) -> WorkflowResult:
    """在临时 Session 中真实提交首轮偏好，并由下一次 create_run_request() 读取。"""
    manager, session, first_request = await session_with_history(root, count=0, session_marker="偏好：输出简洁")
    counter, compactor, llm = FixedTokenizerCounter(), BenchmarkCompactor(), CapturingLLM()
    engine, _ = build_engine(root, manager, counter, compactor, llm=llm)
    first = await engine.execute(first_request)
    persisted = await manager.load(session.id)
    followup = create_run_request(persisted, session.agent_id, "请按已记录偏好给出后续方案")
    second = await engine.execute(followup)
    committed = len((await manager.load(session.id)).conversations) >= 2
    passed = first.state.outcome() is RunOutcome.COMPLETED and second.state.outcome() is RunOutcome.COMPLETED and committed
    return WorkflowResult("preference_aware_followup", "1", passed, second.run_id, False, committed)


async def run_compressed_history_continuation(root: Path) -> WorkflowResult:
    """以生产压缩路径处理持久化历史，并验证下一请求读取活动压缩快照。"""
    # 复用 PR6 已验证的真实工具历史构造，随后仍在本工作流执行压缩与下一请求。
    config = ContextRunConfig("business", "cl100k_base", 0, 1, 0, 1, 0, 1)
    manager, session, request, _, _ = await _compression_history(root, config)
    counter, compactor, llm = FixedTokenizerCounter(), BenchmarkCompactor(), ToolThenFinalLLM()
    engine, _ = build_engine(root, manager, counter, compactor, llm=llm, context_window=70, tool=CompletingTool())
    first = await engine.execute(request)
    persisted = await manager.load(session.id)
    followup = create_run_request(persisted, session.agent_id, "继续遵守关键约束")
    second = await engine.execute(followup)
    used = followup.conversation.compressed_history is not None
    passed = first.state.outcome() is RunOutcome.COMPLETED and second.state.outcome() is RunOutcome.COMPLETED and used
    return WorkflowResult("compressed_history_continuation", "1", passed, second.run_id, used, False)
