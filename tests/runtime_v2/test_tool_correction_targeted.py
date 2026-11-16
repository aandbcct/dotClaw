"""工具纠错控制流的定向动态验证；模型由脚本替身驱动。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from dotclaw.common.tool_replay import ToolExecutionEvidence
from dotclaw.runtime.adapters import ApprovalRepositoryAdapter, CheckpointRepositoryAdapter, RunRepositoryAdapter
from dotclaw.runtime.application.approval_service import ApprovalService
from dotclaw.runtime.application.cancellation_service import CancellationService
from dotclaw.runtime.application.dto import (
    ContextBundle, RunRequest, ToolInvocation, ToolResult, ToolResultStatus,
)
from dotclaw.runtime.application.engine import RuntimeEngine
from dotclaw.runtime.application.execution import RunExecutionView
from dotclaw.runtime.application.ports import LLMOutputPort, LLMPort, RunPolicyPort, ToolPort
from dotclaw.runtime.application.tool_recovery import ToolRecoveryService
from dotclaw.runtime.domain.facts import (
    AgentPolicySnapshot, MessageRole, RunError, RunErrorCode, RunMessage,
    RunMessageKind, ToolCall,
)
from dotclaw.runtime.domain.state import RunOutcome
from dotclaw.runtime.domain.tool_recovery import ToolEffectCertainty, ToolOutcomeSnapshot, ToolOutcomeStatus
from dotclaw.context import ContextDependencies, build_context_provider
from dotclaw.runtime.adapters import ToolExecutorAdapter
from tests.runtime_v2.test_tool_executor_adapter_policy import _build_executor, _execution, _invocation
from tests.runtime_v2.test_runtime_engine import AlwaysWithinBudgetCounter, UnexpectedHistoryCompactor, _request
from tests.runtime_v2.test_tool_projection_recovery import _seed
from tests.runtime_v2.tool_recovery_testkit import ToolPortTestBase, file_tool_recovery


class ScriptedLLM(LLMPort):
    """按轮次返回预设调用，记录每轮真实输入上下文。"""

    def __init__(self, replies: list[tuple[ToolCall, ...] | None], *, prefix: str = "script") -> None:
        self.replies = replies
        self.prefix = prefix
        self.contexts: list[ContextBundle] = []
        self.index = 0

    async def complete(self, context: ContextBundle, execution: RunExecutionView, output_port: LLMOutputPort | None = None) -> RunMessage:
        self.contexts.append(context)
        reply = self.replies[self.index] if self.index < len(self.replies) else None
        self.index += 1
        return RunMessage(
            f"{self.prefix}-response-{self.index}", 1, RunMessageKind.LLM_RESPONSE,
            MessageRole.ASSISTANT, "脚本回答", tool_calls=reply or (),
        )


class ScriptedTool(ToolPortTestBase):
    """按工具调用顺序返回预设结果，并记录实际进入的调用。"""

    def __init__(self, outcomes: list[ToolResultStatus] | None = None, uncertain_call_ids: set[str] | None = None) -> None:
        self.outcomes = outcomes or []
        self.uncertain_call_ids = uncertain_call_ids or set()
        self.calls: list[ToolInvocation] = []

    async def execute(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        self.calls.append(invocation)
        if invocation.call.call_id in self.uncertain_call_ids:
            return ToolResult(
                invocation.call.call_id, ToolResultStatus.COMPLETED, output="外部结果未确认",
                execution_evidence=ToolExecutionEvidence.UNKNOWN,
            )
        status = self.outcomes[len(self.calls) - 1] if len(self.calls) <= len(self.outcomes) else ToolResultStatus.COMPLETED
        if status is ToolResultStatus.FAILED:
            return ToolResult(
                invocation.call.call_id, status, output="参数无效",
                error=RunError(RunErrorCode.TOOL_FAILURE, "参数无效"),
                execution_evidence=ToolExecutionEvidence.RETURNED,
            )
        return ToolResult(
            invocation.call.call_id, ToolResultStatus.COMPLETED, output=f"结果-{invocation.call.call_id}",
            execution_evidence=ToolExecutionEvidence.RETURNED,
        )


class _PreflightCorrectionTool(ScriptedTool):
    """首个调用在 handler 启动前失败，改参调用可继续执行。"""

    async def preflight(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        if invocation.call.call_id == "invalid":
            return ToolResult(
                invocation.call.call_id, ToolResultStatus.FAILED, output="参数验证失败",
                error=RunError(RunErrorCode.TOOL_FAILURE, "参数验证失败"),
                execution_evidence=ToolExecutionEvidence.NOT_STARTED,
                tool_error_code="INVALID_ARGUMENTS",
            )
        return ToolResult(
            invocation.call.call_id, ToolResultStatus.COMPLETED,
            execution_evidence=ToolExecutionEvidence.NOT_STARTED,
        )


class _BudgetPolicy(RunPolicyPort):
    """为预算用例冻结指定的最大模型轮数。"""

    def __init__(self, limit: int, failure_limit: int) -> None:
        self.limit = limit
        self.failure_limit = failure_limit

    async def resolve(self, request: RunRequest) -> AgentPolicySnapshot:
        return AgentPolicySnapshot(
            request.agent_id, "identity-v1", "model-v1", self.limit,
            policy_data={
                "context_window": 128, "tokenizer_encoding": "cl100k_base",
                "tool_correction": {"version": 1, "repeat_failure_limit": self.failure_limit},
            },
        )


def _engine(root: Path, llm: LLMPort, tool: ToolPort, *, limit: int = 10, failure_limit: int = 3) -> RuntimeEngine:
    """通过真实文件仓储组装 Engine，仅替换模型和工具 Port。"""
    approval_repository = ApprovalRepositoryAdapter(root)
    return RuntimeEngine(
        RunRepositoryAdapter(root, approval_repository=approval_repository),
        CheckpointRepositoryAdapter(root), build_context_provider(ContextDependencies()), llm, tool, _BudgetPolicy(limit, failure_limit),
        ApprovalService(approval_repository), CancellationService(),
        token_counter=AlwaysWithinBudgetCounter(), tool_recovery=file_tool_recovery(root),
        history_compactor=UnexpectedHistoryCompactor(),
    )


async def test_failed_tool_result_is_in_next_round_and_corrected(tmp_path: Path) -> None:
    """失败结果必须持久化并进入下一轮上下文，脚本随后改参成功。"""
    first = ToolCall("bad-call", "lookup", {"query": "bad"})
    fixed = ToolCall("fixed-call", "lookup", {"query": "fixed"})
    llm = ScriptedLLM([(first,), (fixed,), None])
    tool = ScriptedTool([ToolResultStatus.FAILED, ToolResultStatus.COMPLETED])
    result = await _engine(tmp_path, llm, tool).execute(_request("feedback-success"))
    next_context = llm.contexts[1]
    assert result.state.outcome() is RunOutcome.COMPLETED, result.error
    assert [invocation.call.arguments for invocation in tool.calls] == [{"query": "bad"}, {"query": "fixed"}]
    assert any(message.tool_call_id == "bad-call" and "参数无效" in message.content for message in next_context.messages)


async def test_preflight_not_started_failure_is_recovered_with_corrected_arguments(tmp_path: Path) -> None:
    """参数预检失败不启动 handler，回灌后改参可成功完成。"""
    llm = ScriptedLLM([
        (ToolCall("invalid", "lookup", {"query": "bad"}),),
        (ToolCall("corrected", "lookup", {"query": "good"}),), None,
    ])
    tool = _PreflightCorrectionTool()
    engine = _engine(tmp_path, llm, tool)
    result = await engine.execute(_request("preflight-correction"))
    ledger = await engine._run_repository.load_tool_ledger("preflight-correction", result.run_id)
    events = await engine._run_repository.load_events("preflight-correction", result.run_id)
    assert result.state.outcome() is RunOutcome.COMPLETED
    assert [invocation.call.call_id for invocation in tool.calls] == ["corrected"]
    assert ledger is not None and ledger.calls[0].result is not None
    assert ledger.calls[0].attempt == 0
    assert ledger.calls[0].result.execution_evidence is ToolExecutionEvidence.NOT_STARTED
    assert not any(event.event_type.value == "tool_started" and event.data.get("call_id") == "invalid" for event in events)
    assert any(message.tool_call_id == "invalid" and "参数验证失败" in message.content for message in llm.contexts[1].messages)


async def test_multiple_calls_continue_after_failure(tmp_path: Path) -> None:
    """同批首个调用失败后仍执行其他调用，再把完整结果交给下一轮。"""
    llm = ScriptedLLM([(ToolCall("a", "lookup", {}), ToolCall("b", "lookup", {})), None])
    tool = ScriptedTool([ToolResultStatus.FAILED, ToolResultStatus.COMPLETED])
    result = await _engine(tmp_path, llm, tool).execute(_request("multi-call"))
    assert result.state.outcome() is RunOutcome.COMPLETED, result.error
    assert [item.call.call_id for item in tool.calls] == ["a", "b"]
    assert [item.tool_call_id for item in llm.contexts[1].messages if item.tool_call_id] == ["a", "b"]


@pytest.mark.parametrize("limit", [1, 3])
async def test_repeat_failure_blocks_at_configured_threshold(limit: int, tmp_path: Path) -> None:
    """达到失败阈值后同一规范调用不再进入工具 Port。"""
    calls = tuple(ToolCall(f"call-{i}", "lookup", {"q": 1}) for i in range(limit + 2))
    llm = ScriptedLLM([(call,) for call in calls] + [None])
    tool = ScriptedTool([ToolResultStatus.FAILED] * limit)
    engine = _engine(tmp_path, llm, tool, failure_limit=limit)
    result = await engine.execute(_request(f"repeat-{limit}"))
    ledger = await engine._run_repository.load_tool_ledger(f"repeat-{limit}", result.run_id)
    assert len(tool.calls) == limit
    assert ledger is not None and ledger.calls[limit].result is not None
    assert ledger.calls[limit].result.tool_error_code == "REPEAT_FAILURE_LIMIT"


async def test_success_clears_consecutive_failure_counter(tmp_path: Path) -> None:
    """相同组合明确成功后清零，之后允许重新累计三次失败。"""
    llm = ScriptedLLM([(ToolCall(f"call-{i}", "lookup", {"q": 1}),) for i in range(7)] + [None])
    tool = ScriptedTool([
        ToolResultStatus.FAILED, ToolResultStatus.FAILED, ToolResultStatus.COMPLETED,
        ToolResultStatus.FAILED, ToolResultStatus.FAILED, ToolResultStatus.FAILED,
    ])
    result = await _engine(tmp_path, llm, tool).execute(_request("repeat-reset"))
    assert result.state.outcome() is RunOutcome.COMPLETED
    assert len(tool.calls) == 6


@pytest.mark.parametrize("max_rounds", [1, 0])
async def test_iteration_budget_one_and_disabled_budget(tmp_path: Path, max_rounds: int) -> None:
    """预算为一时终止额外轮次，为零时允许超过默认十轮。"""
    llm = ScriptedLLM([(ToolCall(f"round-{i}", "lookup", {"round": i}),) for i in range(12)])
    tool = ScriptedTool()
    engine = _engine(tmp_path, llm, tool, limit=max_rounds)
    result = await engine.execute(_request(f"budget-{max_rounds}"))
    if max_rounds == 1:
        assert result.state.outcome() is RunOutcome.FAILED
        assert llm.index == 1
        assert len(tool.calls) == 1
        assert result.error is not None and result.error.code is RunErrorCode.ITERATION_LIMIT
    else:
        assert llm.index > 10
        assert result.state.outcome() is RunOutcome.COMPLETED


async def test_file_engine_result_is_projected_once(tmp_path: Path) -> None:
    """文件仓储正常提交后结果消息、事件及调用统计各只出现一次。"""
    engine = _engine(tmp_path, ScriptedLLM([(ToolCall("once", "lookup", {}),), None]), ScriptedTool())
    result = await engine.execute(_request("projection-once"))
    messages = await engine._run_repository.load_messages("projection-once", result.run_id)
    events = await engine._run_repository.load_events("projection-once", result.run_id)
    ledger = await engine._run_repository.load_tool_ledger("projection-once", result.run_id)
    run = await engine._run_repository.load_run("projection-once", result.run_id)
    assert len([message for message in messages if message.kind.value == "tool_result"]) == 1
    assert len([event for event in events if event.event_type.value == "tool_completed"]) == 1
    assert ledger is not None and ledger.calls[0].retry_count == 0
    assert run is not None and run.statistics.tool_call_count == 1


class _ApprovalThenContinueTool(ScriptedTool):
    """危险工具要求审批并给出稳定资源键，普通工具可直接执行。"""

    async def preflight(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        if invocation.call.name == "danger":
            return ToolResult(
                invocation.call.call_id, ToolResultStatus.APPROVAL_REQUIRED,
                approval_id="approval-targeted", approval_operation_keys=("resolved-resource",),
                execution_evidence=ToolExecutionEvidence.NOT_STARTED,
            )
        return ToolResult(
            invocation.call.call_id, ToolResultStatus.COMPLETED,
            execution_evidence=ToolExecutionEvidence.NOT_STARTED,
        )


class _UnknownThenApprovalTool(ScriptedTool):
    """先产生 UNKNOWN 结果，再让任务在审批点暂停供新 Engine 恢复。"""

    async def preflight(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        if invocation.call.name == "pause" and not invocation.approved:
            return ToolResult(
                invocation.call.call_id, ToolResultStatus.APPROVAL_REQUIRED,
                approval_id="resume-approval", execution_evidence=ToolExecutionEvidence.NOT_STARTED,
            )
        return ToolResult(
            invocation.call.call_id, ToolResultStatus.COMPLETED,
            execution_evidence=ToolExecutionEvidence.NOT_STARTED,
        )


async def test_approval_denial_blocks_reordered_and_same_resource_calls_but_allows_other_tool(tmp_path: Path) -> None:
    """审批拒绝按调用键和资源键阻断新 call_id，其他工具仍能继续。"""
    llm = ScriptedLLM([
        (ToolCall("approval-call", "danger", {"a": 1, "b": 2}),),
        (
            ToolCall("reordered", "danger", {"b": 2, "a": 1}),
            ToolCall("same-resource", "danger", {"path": "alternate"}),
            ToolCall("safe", "lookup", {"q": "ok"}),
        ), None,
    ])
    tool = _ApprovalThenContinueTool()
    engine = _engine(tmp_path, llm, tool)
    waiting = await engine.execute(_request("approval-denied-targeted"))
    assert waiting.approval_id == "approval-targeted"
    result = await engine.resolve_approval("approval-targeted", approved=False)
    assert result.state.outcome() is RunOutcome.COMPLETED
    assert [item.call.call_id for item in tool.calls] == ["safe"]
    ledger = await engine._run_repository.load_tool_ledger("approval-denied-targeted", result.run_id)
    assert ledger is not None
    original_denial = [item for item in ledger.calls if item.approval_decision is False]
    followup_blocks = [
        item for item in ledger.calls
        if item.call.name == "danger" and item.approval_decision is None and item.result is not None
    ]
    assert len(original_denial) == 1
    assert original_denial[0].result is not None and original_denial[0].result.tool_error_code == "APPROVAL_DENIED"
    assert len(followup_blocks) == 2
    assert all(item.result.tool_error_code == "APPROVAL_DENIED" for item in followup_blocks)
    assert [item.call.call_id for item in tool.calls] == ["safe"]


async def test_real_executor_preflight_keys_use_normalized_resolved_file_path() -> None:
    """真实 Adapter 的同路径别名预检生成相同且含绝对解析路径的审批键。"""
    port = ToolExecutorAdapter(_build_executor())
    execution = _execution("narrow-agent")
    first = await port.preflight(_invocation(), execution)
    alias_call = ToolCall("call-alias", "reader", {"path": "./a.txt"})
    alias = await port.preflight(ToolInvocation("run-1", alias_call), execution)
    expected_key = json.dumps(
        ["file.read", os.path.normcase(str(Path("a.txt").resolve()))],
        ensure_ascii=False, separators=(",", ":"),
    )
    assert first.status is ToolResultStatus.APPROVAL_REQUIRED
    assert alias.status is ToolResultStatus.APPROVAL_REQUIRED
    assert first.approval_operation_keys == alias.approval_operation_keys == (expected_key,)


async def test_unknown_side_effect_blocks_tool_and_continues_other_tools(tmp_path: Path) -> None:
    """UNKNOWN 执行证据封禁该工具名并持久化结果，另一工具仍执行。"""
    llm = ScriptedLLM([
        (ToolCall("maybe", "write", {"value": "x"}),),
        (ToolCall("retry", "write", {"value": "different"}), ToolCall("safe", "lookup", {})),
        None,
    ])
    tool = ScriptedTool(uncertain_call_ids={"maybe"})
    engine = _engine(tmp_path, llm, tool)
    result = await engine.execute(_request("unknown-block"))
    assert result.state.outcome() is RunOutcome.COMPLETED
    assert [item.call.call_id for item in tool.calls] == ["maybe", "safe"]
    ledger = await engine._run_repository.load_tool_ledger("unknown-block", result.run_id)
    assert ledger is not None and ledger.calls[0].result is not None
    assert ledger.calls[0].result.status is ToolOutcomeStatus.UNCERTAIN
    assert ledger.calls[1].result is not None and ledger.calls[1].result.tool_error_code == "TOOL_BLOCKED_UNCERTAIN"


async def test_unknown_block_survives_engine_rebuild(tmp_path: Path) -> None:
    """未知结果落盘后在审批点重建 Engine，恢复时仍封禁同名工具。"""
    first_llm = ScriptedLLM([
        (ToolCall("uncertain", "write", {"value": 1}), ToolCall("pause-call", "pause", {})),
    ])
    first_tool = _UnknownThenApprovalTool(uncertain_call_ids={"uncertain"})
    engine = _engine(tmp_path, first_llm, first_tool)
    waiting = await engine.execute(_request("unknown-engine-rebuild"))
    assert waiting.approval_id == "resume-approval"
    rebuilt_llm = ScriptedLLM([
        (ToolCall("write-after-restart", "write", {"value": 2}), ToolCall("safe-after-restart", "lookup", {})),
        None,
    ], prefix="rebuilt")
    rebuilt_tool = _UnknownThenApprovalTool()
    rebuilt = _engine(tmp_path, rebuilt_llm, rebuilt_tool)
    result = await rebuilt.resolve_approval("resume-approval", approved=True)
    events = await rebuilt._run_repository.load_events("unknown-engine-rebuild", result.run_id)
    ledger = await rebuilt._run_repository.load_tool_ledger("unknown-engine-rebuild", result.run_id)
    assert result.state.outcome() is RunOutcome.COMPLETED, (
        [(event.event_type.value, event.data.get("approval_id"), event.data.get("approved")) for event in events],
        [(call.call.call_id, call.state.value, call.approval_decision) for call in ledger.calls] if ledger else None,
    )
    assert [item.call.call_id for item in first_tool.calls] == ["uncertain"]
    assert [item.call.call_id for item in rebuilt_tool.calls] == ["pause-call", "safe-after-restart"]
    ledger = await rebuilt._run_repository.load_tool_ledger("unknown-engine-rebuild", result.run_id)
    assert ledger is not None
    assert ledger.calls[0].result is not None and ledger.calls[0].result.status is ToolOutcomeStatus.UNCERTAIN
    blocked = next(item for item in ledger.calls if item.call.call_id == "write-after-restart")
    assert blocked.result is not None and blocked.result.tool_error_code == "TOOL_BLOCKED_UNCERTAIN"


@pytest.mark.parametrize("failure_stage", ["messages", "events", "checkpoint"])
async def test_projection_failure_is_compensated_after_recovery_service_rebuild(failure_stage: str) -> None:
    """三种投影故障退出后重建服务并重复恢复，消息、事件、计数仍仅一次。"""
    runs, checkpoints, approvals, service, run, checkpoint = await _seed()
    await service.begin_attempt(run, "call-1", checkpoint)
    checkpoint = await checkpoints.load("session-1", "run-1")
    assert checkpoint is not None
    outcome = ToolOutcomeSnapshot(
        ToolOutcomeStatus.COMPLETED, "ok", None, "result-once", "2026-11-16T00:00:01+00:00",
        ToolEffectCertainty.KNOWN,
    )
    if failure_stage == "checkpoint":
        checkpoints.failure_stage = "checkpoint"
    else:
        runs.failure_stage = failure_stage
    with pytest.raises(OSError, match=failure_stage):
        await service.commit_outcome(run, "call-1", outcome, checkpoint)
    recovered = ToolRecoveryService(runs, checkpoints, approvals)
    await recovered.recover_projection("session-1", "run-1")
    await recovered.recover_projection("session-1", "run-1")
    messages = await runs.load_messages("session-1", "run-1")
    events = await runs.load_events("session-1", "run-1")
    persisted_run = await runs.load_run("session-1", "run-1")
    ledger = await runs.load_tool_ledger("session-1", "run-1")
    assert sum(message.kind.value == "tool_result" for message in messages) == 1
    assert sum(event.event_type.value == "tool_completed" for event in events) == 1
    assert persisted_run is not None and persisted_run.statistics.tool_call_count == 1
    assert ledger is not None and ledger.pending_projection is None and ledger.calls[0].retry_count == 0

