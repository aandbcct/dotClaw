"""个人助理批量审批在真实 Runtime 与 SQLite 上的恢复集成。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from dotclaw.assistant.context import AssistantContextReader
from dotclaw.assistant.service import AssistantService
from dotclaw.assistant.sqlite_repository import SqliteAssistantRepository
from dotclaw.common.tool_replay import ToolExecutionEvidence
from dotclaw.context import ContextDependencies, build_context_provider
from dotclaw.personal_tasks.service import PersonalTaskService
from dotclaw.personal_tasks.sqlite_repository import SqlitePersonalTaskRepository
from dotclaw.runtime.adapters import ApprovalRepositoryAdapter, CheckpointRepositoryAdapter, RunRepositoryAdapter
from dotclaw.runtime.application.approval_service import ApprovalService
from dotclaw.runtime.application.cancellation_service import CancellationService
from dotclaw.runtime.application.dto import (
    ContextBundle,
    ConversationMessage,
    ConversationSnapshot,
    RunRequest,
    RunResult,
    ToolInvocation,
    ToolRecoveryContract,
    ToolResult,
    ToolResultStatus as RuntimeToolResultStatus,
)
from dotclaw.runtime.application.engine import RuntimeEngine
from dotclaw.runtime.application.execution import RunExecutionView
from dotclaw.runtime.application.ports import LLMPort, RunPolicyPort, ToolPort
from dotclaw.runtime.application.tool_recovery import ToolRecoveryService
from dotclaw.runtime.domain.facts import AgentPolicySnapshot, MessageRole, RunMessage, RunMessageKind, ToolCall
from dotclaw.runtime.domain.state import RunOutcome
from dotclaw.tools.assistant_tools import register_assistant_tools
from dotclaw.tools.base import ToolDefinition as ChannelToolDefinition, ToolExecutionContext as ChannelToolContext
from dotclaw.tools.registry import ToolRegistry
from tests.runtime_v2.context_budget_fakes import AlwaysWithinBudgetCounter, UnexpectedHistoryCompactor


_INPUT_CREATED_AT = "2032-04-05T12:00:00+00:00"


class RecordingAssistantContextReader:
    """记录实际恢复时上下文读取收到的原始输入引用。"""

    def __init__(self, reader: AssistantContextReader) -> None:
        self._reader = reader
        self.references: list[tuple[str, str, datetime]] = []
        self.fail = False

    async def read(self, session_id: str, input_ref) -> str:
        self.references.append((input_ref.run_id, input_ref.message_id, input_ref.created_at))
        if self.fail:
            raise ValueError("assistant context unavailable")
        return await self._reader.read(session_id, input_ref)


class AssistantBatchPolicy(RunPolicyPort):
    """将注册表中的真实助理工具 Schema 冻结到本轮身份策略。"""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    async def resolve(self, request: RunRequest) -> AgentPolicySnapshot:
        tools = [
            {"name": item.name, "description": item.description, "parameters": item.parameters}
            for item in self._registry.get_definitions()
        ]
        return AgentPolicySnapshot(
            request.agent_id,
            "personal-assistant-v1",
            "scripted-model",
            8,
            policy_data={"system_prompt": "个人助理", "tools": tools, "context_window": 32000, "tokenizer_encoding": "cl100k_base"},
        )


class BatchApprovalModel(LLMPort):
    """依据prepare真实结果构造apply调用，第三次返回最终答复。"""

    def __init__(self, first_task_id: str, second_task_id: str) -> None:
        self._task_ids = (first_task_id, second_task_id)
        self.call_inputs: list[tuple[str, str]] = []

    async def complete(self, context: ContextBundle, execution: RunExecutionView, output_port=None) -> RunMessage:
        self.call_inputs.append((execution.input_message_id, execution.input_created_at))
        if len(self.call_inputs) == 1:
            arguments = {
                "summary": "批量改名",
                "items": [
                    {"action": "update", "task_id": task_id, "expected_version": 1, "patch": {"title": f"新标题{index}"}}
                    for index, task_id in enumerate(self._task_ids, start=1)
                ],
            }
            calls = (ToolCall("prepare-call", "builtin.assistant.batch.prepare", arguments),)
            return RunMessage("prepare-response", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "", tool_calls=calls)
        if len(self.call_inputs) == 2:
            prepared = next(
                json.loads(message.content)
                for message in reversed(context.messages)
                if message.role is MessageRole.TOOL and message.content.startswith("{")
                and json.loads(message.content).get("operation") == "batch.prepare"
            )
            arguments = {
                "request_id": prepared["request_id"],
                "expected_version": prepared["data"]["request"]["version"],
                "proposal_hash": prepared["data"]["request"]["proposal_hash"],
            }
            calls = (ToolCall("apply-call", "builtin.assistant.batch.apply", arguments),)
            return RunMessage("apply-response", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "", tool_calls=calls)
        return RunMessage("final-response", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "批量请求已处理")


class AssistantBatchToolPort(ToolPort):
    """通过真实助理 Handler 执行业务，并将其审批声明交给 Runtime。"""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self.contexts: list[ChannelToolContext] = []

    async def describe_recovery(self, call: ToolCall, execution: RunExecutionView) -> ToolRecoveryContract:
        definition: ChannelToolDefinition = self._registry.get(call.name).definition()
        return ToolRecoveryContract(
            definition.replay_policy,
            definition.replay_contract_version,
            side_effect=definition.side_effect,
            recovery_max_retries=definition.recovery_max_retries,
            recovery_initial_delay_s=definition.recovery_initial_delay_s,
            recovery_backoff_multiplier=definition.recovery_backoff_multiplier,
            recovery_max_delay_s=definition.recovery_max_delay_s,
            execution_scope=definition.metadata["assistant_database_path"],
        )

    async def preflight(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        definition: ChannelToolDefinition = self._registry.get(invocation.call.name).definition()
        if definition.needs_approval and not invocation.approved:
            return ToolResult(
                invocation.call.call_id,
                RuntimeToolResultStatus.APPROVAL_REQUIRED,
                approval_id="approval-batch",
                execution_evidence=ToolExecutionEvidence.NOT_STARTED,
            )
        return ToolResult(
            invocation.call.call_id,
            RuntimeToolResultStatus.COMPLETED,
            execution_evidence=ToolExecutionEvidence.NOT_STARTED,
        )

    async def execute(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        context = ChannelToolContext(
            agentrun_id=invocation.run_id,
            session_id=execution.session_id,
            agent_id=execution.policy.agent_id,
            tool_call_id=invocation.call.call_id,
            attempt=invocation.attempt,
            idempotency_key=invocation.idempotency_key,
            approved=invocation.approved,
            input_message_id=execution.input_message_id,
            input_created_at=execution.input_created_at,
        )
        self.contexts.append(context)
        result = await self._registry.get(invocation.call.name).execute(invocation.call.arguments, context)
        return ToolResult(
            invocation.call.call_id,
            RuntimeToolResultStatus.FAILED if result.is_error else RuntimeToolResultStatus.COMPLETED,
            result.output,
            error=None,
            execution_evidence=result.execution_evidence,
            tool_error_code=result.error_code,
            tool_error_type=result.error_type,
        )


def _runtime_engine(root: Path, registry: ToolRegistry, model: BatchApprovalModel, reader: RecordingAssistantContextReader) -> RuntimeEngine:
    approvals = ApprovalRepositoryAdapter(root / "runs")
    runs = RunRepositoryAdapter(root / "runs", approval_repository=approvals)
    checkpoints = CheckpointRepositoryAdapter(root / "runs")
    approval_service = ApprovalService(approvals)
    return RuntimeEngine(
        runs,
        checkpoints,
        build_context_provider(ContextDependencies(assistant_context_reader=reader)),
        model,
        AssistantBatchToolPort(registry),
        AssistantBatchPolicy(registry),
        approval_service,
        CancellationService(),
        token_counter=AlwaysWithinBudgetCounter(),
        history_compactor=UnexpectedHistoryCompactor(),
        tool_recovery=ToolRecoveryService(runs, checkpoints, approval_service),
    )


@pytest.mark.asyncio
async def test_batch_runs_without_runtime_approval_and_keeps_user_input(tmp_path: Path) -> None:
    """批量 apply 不触发审批并保留初始 USER_INPUT。"""
    database = tmp_path / "personal-assistant.sqlite3"
    timezone = ZoneInfo("UTC")
    task_repository = SqlitePersonalTaskRepository(database, timezone)
    await task_repository.initialize()
    task_service = PersonalTaskService(task_repository, timezone=timezone)
    first = await task_service.create(title="事务一")
    second = await task_service.create(title="事务二")
    assistant_repository = SqliteAssistantRepository(database)
    assistant_service = AssistantService(
        database,
        timezone=timezone,
        repository=assistant_repository,
        clock=lambda: datetime(2032, 4, 5, 12, tzinfo=UTC),
    )
    registry = ToolRegistry()
    register_assistant_tools(registry, assistant_service)
    model = BatchApprovalModel(first.task.id, second.task.id)
    reader = RecordingAssistantContextReader(AssistantContextReader(assistant_repository, timezone, False))
    engine = _runtime_engine(tmp_path, registry, model, reader)
    assistant_service.bind_run_repository(engine._run_repository)
    message = ConversationMessage("assistant-user-input", MessageRole.USER, "把两项事务改名", _INPUT_CREATED_AT)
    request = RunRequest(
        "personal-assistant",
        "assistant-lease",
        "personal-assistant",
        message,
        ConversationSnapshot("personal-assistant", (), 0),
    )

    result: RunResult = await engine.execute(request)
    assert result.approval_id is None
    assert result.state.outcome() is RunOutcome.COMPLETED
    assert model.call_inputs == [(message.message_id, _INPUT_CREATED_AT)] * 3
    assert [(run_id, message_id, created_at.isoformat()) for run_id, message_id, created_at in reader.references] == [
        (result.run_id, message.message_id, _INPUT_CREATED_AT),
    ] * 3
    stored_input = await engine._run_repository.load_messages("personal-assistant", result.run_id)
    assert stored_input[0].metadata["created_at"] == _INPUT_CREATED_AT
    saved_first, _ = await task_repository.get_detail(first.task.id)
    saved_second, _ = await task_repository.get_detail(second.task.id)
    assert (saved_first.title, saved_second.title) == ("新标题1", "新标题2")
