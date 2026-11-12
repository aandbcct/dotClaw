"""工具调用账本事实与运行投影的提交协调。"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Mapping

from ..domain.control import AgentAction
from ..domain.events import (
    ApprovalGranted,
    DelegationCompleted,
    DelegationSubmitted,
    RunEvent,
    RunEventType,
    ToolApprovalRequired,
    ToolBatchCompleted,
    ToolBatchFailed,
)
from ..domain.facts import (
    AgentRun,
    ApprovalRecord,
    ApprovalStatus,
    MessageRole,
    RunCheckpoint,
    RunError,
    RunErrorCode,
    RunMessage,
    RunMessageKind,
    utc_now_iso,
)
from ...common.tool_replay import ToolExecutionEvidence, ToolReplayPolicy, ToolSideEffect
from ..domain.state import Ended, RunOutcome, Running, RunStage, Suspended, SuspendReason, transition
from ..domain.tool_recovery import (
    ToolCallKind,
    ToolCallLedger,
    ToolCallRecord,
    ToolCallState,
    ToolEffectCertainty,
    ToolOutcomeSnapshot,
    ToolOutcomeStatus,
    ToolProjectionIntent,
)
from .approval_service import ApprovalService
from .dto import ToolRecoveryContract, ToolRecoveryDecision, ToolRecoveryPlan
from ..domain.tool_recovery import ToolRecoveryAction
from .ports import CheckpointRepository, RunPersistenceError, RunRepository


class ToolProjectionPersistenceError(RunPersistenceError):
    """账本提交意图的条件投影未能完成，待处理意图必须保留。"""


class ToolRecoveryService:
    """在调用方持有运行短锁时，提交账本与其消息、事件、检查点和运行投影。"""

    def __init__(
        self,
        runs: RunRepository,
        checkpoints: CheckpointRepository,
        approvals: ApprovalService,
    ) -> None:
        self._runs = runs
        self._checkpoints = checkpoints
        self._approvals = approvals

    async def recover_projection(self, session_id: str, run_id: str) -> None:
        ledger = await self._runs.load_tool_ledger(session_id, run_id)
        if ledger is not None and ledger.pending_projection is not None:
            await self._apply_projection(ledger)

    async def import_legacy(
        self,
        run: AgentRun,
        checkpoint: RunCheckpoint,
        messages: tuple[RunMessage, ...],
        events: tuple[RunEvent, ...],
    ) -> ToolCallLedger:
        """只把无新协议引用的旧工具事实一次性迁入账本与新检查点。"""
        self._assert_run(run, checkpoint)
        if run.state.is_ended():
            raise ValueError("已终态旧运行保持只读")
        if await self._ledger(run.session_id, run.run_id) is not None:
            raise ValueError("已有工具账本的运行不能走旧格式导入")
        if checkpoint.tool_ledger_revision is not None or any(
            key in checkpoint.pending for key in ("tool_batch_id", "pending_call_ids")
        ) or any(
            event.event_type is RunEventType.TOOL_RECOVERY_BLOCKED
            or event.data.get("tool_protocol_version") is not None
            or event.data.get("commit_id") is not None
            for event in events
        ):
            raise ValueError("新格式工具事实缺失账本，禁止降级为旧格式导入")

        sources = tuple(
            message for message in messages
            if message.role is MessageRole.ASSISTANT and message.tool_calls
        )
        if not sources:
            raise ValueError("旧工具恢复缺少原始助手批次")
        source_by_id = {message.message_id: message for message in sources}
        if len(source_by_id) != len(sources):
            raise ValueError("旧助手工具批次身份不唯一")
        calls_by_id = {
            call.call_id: (source, ordinal, call)
            for source in sources
            for ordinal, call in enumerate(source.tool_calls)
        }
        if sum(len(source.tool_calls) for source in sources) != len(calls_by_id):
            raise ValueError("旧工具调用 ID 跨批次重复，不能安全导入")

        legacy_pending = checkpoint.pending.get("tool_calls")
        pending_ids: tuple[str, ...] = ()
        pending_entries: list[dict] | None = None
        if isinstance(legacy_pending, list):
            pending_entries = legacy_pending
            pending_ids = tuple(
                item["call_id"] for item in legacy_pending
                if isinstance(item, dict) and isinstance(item.get("call_id"), str)
            )
            if len(pending_ids) != len(legacy_pending):
                raise ValueError("旧检查点待执行调用格式不完整")
        elif isinstance(checkpoint.pending.get("call_id"), str):
            pending_ids = (checkpoint.pending["call_id"],)
        if not pending_ids and isinstance(checkpoint.pending.get("source_tool_call_id"), str):
            pending_ids = (checkpoint.pending["source_tool_call_id"],)
        if pending_ids:
            if any(call_id not in calls_by_id for call_id in pending_ids):
                raise ValueError("旧检查点待执行调用不属于原始助手批次")
            pending_sources = {calls_by_id[call_id][0].message_id for call_id in pending_ids}
            if len(pending_sources) != 1:
                raise ValueError("旧检查点待执行调用跨越多个助手批次")
            pending_source = calls_by_id[pending_ids[0]][0]
            start = calls_by_id[pending_ids[0]][1]
            expected_suffix = tuple(call.call_id for call in pending_source.tool_calls[start:])
            if pending_entries and tuple(pending_ids) != expected_suffix:
                raise ValueError("旧检查点待执行调用不是原始助手批次的完整后缀")
            if pending_entries and any(
                item != pending_source.tool_calls[start + offset].to_dict()
                for offset, item in enumerate(pending_entries)
            ):
                raise ValueError("旧检查点调用参数与原始助手批次不一致")
            if not pending_entries:
                pending_ids = expected_suffix

        approval_events: dict[str, RunEvent] = {}
        terminal_events: dict[str, RunEvent] = {}
        started_events: dict[str, list[RunEvent]] = {}
        submitted_events: dict[str, RunEvent] = {}
        for event in events:
            call_id = event.data.get("call_id")
            if event.event_type in {RunEventType.TOOL_STARTED, RunEventType.TOOL_COMPLETED, RunEventType.WAITING_APPROVAL, RunEventType.DELEGATION_SUBMITTED}:
                if not isinstance(call_id, str) or call_id not in calls_by_id:
                    continue
                source, _, _ = calls_by_id[call_id]
                source_id = event.data.get("source_response_message_id") or event.data.get("source_message_id")
                if source_id is not None and source_id != source.message_id:
                    raise ValueError("旧工具事件与助手批次来源不一致")
            if event.event_type is RunEventType.TOOL_STARTED and isinstance(call_id, str) and call_id in calls_by_id:
                started_events.setdefault(call_id, []).append(event)
            elif event.event_type is RunEventType.WAITING_APPROVAL and isinstance(call_id, str) and call_id in calls_by_id:
                approval_id = event.data.get("approval_id")
                if isinstance(approval_id, str):
                    approval_events[call_id] = event
            elif event.event_type is RunEventType.TOOL_COMPLETED and isinstance(call_id, str) and call_id in calls_by_id:
                if event.data.get("status") in {"completed", "failed", "cancelled"}:
                    terminal_events[call_id] = event
                elif event.data.get("status") == "approval_required":
                    approval_id = event.data.get("approval_id")
                    if isinstance(approval_id, str):
                        approval_events[call_id] = event
            elif event.event_type is RunEventType.DELEGATION_SUBMITTED and isinstance(call_id, str) and call_id in calls_by_id:
                submitted_events[call_id] = event

        approval_decisions: dict[str, tuple[bool, RunEvent]] = {}
        for event in events:
            if event.event_type is not RunEventType.APPROVAL_RESOLVED:
                continue
            approval_id = event.data.get("approval_id")
            call_id = event.data.get("call_id")
            approved = event.data.get("approved")
            if isinstance(approval_id, str) and type(approved) is bool:
                if not isinstance(call_id, str):
                    call_id = next((candidate for candidate, wait_event in approval_events.items() if wait_event.data.get("approval_id") == approval_id), None)
                wait = approval_events.get(call_id) if isinstance(call_id, str) else None
                if wait is not None and wait.data.get("approval_id") == approval_id:
                    approval_decisions[call_id] = (approved, event)

        submitted_child_ids: dict[str, str] = {
            call_id: str(event.data["child_run_id"])
            for call_id, event in submitted_events.items()
            if isinstance(event.data.get("child_run_id"), str)
        }
        requested_calls: list[str] = []
        for event in events:
            if event.event_type is RunEventType.DELEGATION_REQUESTED:
                call_id = event.data.get("tool_call_id")
                if isinstance(call_id, str) and call_id in calls_by_id:
                    requested_calls.append(call_id)
            elif event.event_type is RunEventType.DELEGATION_SUBMITTED:
                child_id = event.data.get("child_run_id")
                call_id = event.data.get("call_id")
                if not isinstance(call_id, str):
                    call_id = next((candidate for candidate in reversed(requested_calls) if candidate not in submitted_child_ids), None)
                if isinstance(call_id, str) and call_id in calls_by_id and isinstance(child_id, str):
                    submitted_child_ids[call_id] = child_id

        message_index = {message.message_id: message for message in messages}
        records: list[ToolCallRecord] = []
        approval_projection: ApprovalRecord | None = None
        now = utc_now_iso()
        for source in sources:
            for ordinal, call in enumerate(source.tool_calls):
                kind = ToolCallKind.DELEGATION if call.name == "delegate" else ToolCallKind.ORDINARY
                terminal = terminal_events.get(call.call_id)
                wait = approval_events.get(call.call_id)
                decision = approval_decisions.get(call.call_id)
                result = None
                state = ToolCallState.PENDING if call.call_id in pending_ids else ToolCallState.UNCERTAIN
                attempt = len(started_events.get(call.call_id, ()))
                uncertainty_reason = None if state is ToolCallState.PENDING else "legacy_execution_unproven"
                approval_id = wait.data.get("approval_id") if wait is not None else None
                approval_decision = decision[0] if decision is not None else None
                child_run_id = None

                if terminal is not None:
                    result_id = terminal.data.get("result_message_id")
                    result_message = message_index.get(result_id) if isinstance(result_id, str) else None
                    message_kind = RunMessageKind.TOOL_RESULT
                    child_outcome = None
                    valid_result = (
                        isinstance(result_id, str)
                        and result_message is not None
                        and result_message.role is MessageRole.TOOL
                        and result_message.tool_call_id == call.call_id
                    )
                    if kind is ToolCallKind.DELEGATION and valid_result:
                        message_kind = RunMessageKind.DELEGATION_RESULT
                        completed_delegation = next(
                            (event for event in events if event.event_type is RunEventType.DELEGATION_COMPLETED and result_message.message_id in event.message_ids),
                            None,
                        )
                        raw_child_id = completed_delegation.data.get("child_run_id") if completed_delegation is not None else None
                        child = await self._runs.find_run(raw_child_id) if isinstance(raw_child_id, str) else None
                        raw_outcome = completed_delegation.data.get("outcome") if completed_delegation is not None else None
                        if child is None or child.parent_run_id != run.run_id or child.state.outcome() is None or raw_outcome != child.state.outcome().value:
                            valid_result = False
                        else:
                            child_run_id = child.run_id
                            child_outcome = child.state.outcome()
                    if (
                        not valid_result
                        or result_message.kind is not message_kind
                    ):
                        state = ToolCallState.UNCERTAIN
                        uncertainty_reason = "legacy_terminal_result_unmatched"
                    else:
                        status = terminal.data["status"]
                        if status == "cancelled":
                            state = ToolCallState.UNCERTAIN
                            uncertainty_reason = "legacy_cancelled_effect_unproven"
                        else:
                            result = ToolOutcomeSnapshot(
                                ToolOutcomeStatus(status), result_message.content,
                                RunError(RunErrorCode.TOOL_FAILURE, str(terminal.data.get("error_summary") or result_message.content)) if status == "failed" else None,
                                result_message.message_id, terminal.occurred_at, ToolEffectCertainty.KNOWN,
                                message_kind=message_kind, child_outcome=child_outcome,
                            )
                            state = {"completed": ToolCallState.SUCCEEDED, "failed": ToolCallState.FAILED}[status]
                            uncertainty_reason = None
                elif kind is ToolCallKind.DELEGATION:
                    raw_child_id = submitted_child_ids.get(call.call_id)
                    if raw_child_id is None and checkpoint.pending.get("source_tool_call_id") == call.call_id:
                        raw_child_id = checkpoint.pending.get("child_run_id")
                    child = await self._runs.find_run(raw_child_id) if isinstance(raw_child_id, str) else None
                    child_messages = await self._runs.load_messages(child.session_id, child.run_id) if child is not None else ()
                    child_input = next((message for message in child_messages if child is not None and message.message_id == child.input_message_id), None)
                    args = call.arguments
                    expected_input = f"任务：{args.get('title')}\n\n目标：{args.get('objective')}"
                    if (
                        child is not None
                        and child.parent_run_id == run.run_id
                        and child.root_run_id == (run.root_run_id or run.run_id)
                        and child.agent_id == args.get("target_agent_id")
                        and child_input is not None
                        and child_input.role is MessageRole.USER
                        and child_input.content == expected_input
                        and bool(child.delegation_task_id)
                    ):
                        child_run_id = child.run_id
                        state = ToolCallState.WAITING_CHILD
                        uncertainty_reason = None
                    elif attempt:
                        state = ToolCallState.UNCERTAIN
                        uncertainty_reason = "legacy_delegation_identity_unproven"
                    elif call.call_id in pending_ids:
                        state = ToolCallState.PENDING
                        uncertainty_reason = None
                elif wait is not None:
                    state = ToolCallState.WAITING_APPROVAL
                    uncertainty_reason = None
                elif attempt:
                    state = ToolCallState.STARTED
                    uncertainty_reason = None

                if wait is not None and isinstance(approval_id, str):
                    approval_record = await self._approvals.find(approval_id)
                    if approval_record is not None:
                        if (approval_record.run_id, approval_record.session_id) != (run.run_id, run.session_id) or approval_record.call_id not in {None, call.call_id}:
                            raise ValueError("旧审批记录与等待事件关联不一致")
                        resolved_decision = decision[0] if decision is not None else approval_record.decision
                        if approval_record.decision is not None and decision is not None and approval_record.decision is not decision[0]:
                            raise ValueError("旧审批仓储与 APPROVAL_RESOLVED 事件决议冲突")
                        if approval_record.status in {ApprovalStatus.PENDING, ApprovalStatus.CONSUMED} and (
                            approval_record.call_id is None or (resolved_decision is not None and approval_record.decision is None)
                        ):
                            approval_projection = replace(
                                approval_record,
                                status=ApprovalStatus.CONSUMED if resolved_decision is not None else approval_record.status,
                                call_id=call.call_id,
                                decision=resolved_decision,
                                resolved_at=decision[1].occurred_at if decision is not None else approval_record.resolved_at,
                            )
                records.append(ToolCallRecord(
                    run.run_id, call, source.message_id, ordinal, kind, state,
                    attempt=attempt, replay_policy=ToolReplayPolicy.NO_AUTO_RETRY,
                    side_effect=ToolSideEffect.UNKNOWN, approval_id=approval_id,
                    approval_decision=approval_decision, child_run_id=child_run_id,
                    result=result, uncertainty_reason=uncertainty_reason, updated_at=now,
                ))

        active_records = [record for record in records if record.state not in _TERMINAL]
        current_source_id = None
        for call_id in pending_ids:
            source = calls_by_id.get(call_id, (None,))[0]
            if source is not None:
                current_source_id = source.message_id
                break
        if current_source_id is None and isinstance(checkpoint.pending.get("source_tool_call_id"), str):
            source = calls_by_id.get(checkpoint.pending["source_tool_call_id"], (None,))[0]
            current_source_id = source.message_id if source is not None else None
        if current_source_id is None:
            active_source_ids = {item.source_message_id for item in active_records}
            current_source_id = next((source.message_id for source in reversed(sources) if source.message_id in active_source_ids), None)
        if current_source_id is None:
            raise ValueError("舊檢查點無法關聯到原始助手批次")
        current_batch = tuple(record for record in records if record.source_message_id == current_source_id)
        target_pending = {
            "tool_batch_id": current_source_id,
            "pending_call_ids": [record.call.call_id for record in current_batch if record.state not in _TERMINAL],
        }
        base_run, base_checkpoint = await self._persistent_base(run)
        target_checkpoint = self._next_checkpoint(base_checkpoint, checkpoint, target_pending, AgentAction.EXECUTE_TOOLS, True)
        ledger = ToolCallLedger(run.session_id, run.run_id, 1, tuple(records))
        return await self._commit_fact(ledger, base_run, base_run, base_checkpoint, target_checkpoint, approval=approval_projection)

    async def reconcile(self, run: AgentRun, checkpoint: RunCheckpoint, contracts: Mapping[str, ToolRecoveryContract]) -> ToolRecoveryPlan:
        """从账本和已投影事实重建当前批次决策；未知执行效果一律阻断。"""
        await self.recover_projection(run.session_id, run.run_id)
        ledger = await self._ledger(run.session_id, run.run_id)
        if ledger is None:
            if checkpoint.tool_ledger_revision is not None or "tool_batch_id" in checkpoint.pending:
                raise ValueError("新格式检查点引用的工具账本不存在")
            raise ValueError("旧格式工具检查点需由兼容读取路径处理")
        if checkpoint.tool_ledger_revision is not None and checkpoint.tool_ledger_revision > ledger.revision:
            raise ValueError("检查点工具账本修订号超前")
        batch_id = checkpoint.pending.get("tool_batch_id")
        if not isinstance(batch_id, str) or not batch_id:
            batch_id = next((item.source_message_id for item in reversed(ledger.calls) if item.state not in _TERMINAL), "")
        batch = tuple(item for item in ledger.calls if item.source_message_id == batch_id)
        if not batch:
            raise ValueError("检查点引用的工具批次不在账本中")
        source = next((item for item in await self._runs.load_messages(run.session_id, run.run_id) if item.message_id == batch_id), None)
        if source is None or tuple(record.call for record in batch) != source.tool_calls:
            raise ValueError("工具账本与原始助手消息不一致")
        decisions: list[ToolRecoveryDecision] = []
        for record in batch:
            if record.kind is ToolCallKind.DELEGATION:
                if record.state in {ToolCallState.PENDING, ToolCallState.STARTED}:
                    action, reason = ToolRecoveryAction.EXECUTE, "stable_delegation_prepare"
                elif record.state is ToolCallState.WAITING_CHILD:
                    action, reason = ToolRecoveryAction.RESUME_CHILD, "delegation_pending"
                else:
                    action, reason = ToolRecoveryAction.REUSE, "stored_delegation_outcome"
                decisions.append(ToolRecoveryDecision(record.call.call_id, action, reason))
                continue
            if record.state in {ToolCallState.SUCCEEDED, ToolCallState.FAILED, ToolCallState.CANCELLED}:
                decisions.append(ToolRecoveryDecision(record.call.call_id, ToolRecoveryAction.REUSE, "stored_outcome"))
                continue
            if record.state is ToolCallState.UNCERTAIN:
                decisions.append(ToolRecoveryDecision(record.call.call_id, ToolRecoveryAction.BLOCK, record.uncertainty_reason or "uncertain_outcome"))
                continue
            contract = contracts[record.call.call_id]
            frozen_contract = (
                record.replay_policy, record.side_effect, record.replay_contract_version,
                record.key_valid_until,
                record.recovery_initial_delay_s, record.recovery_backoff_multiplier,
                record.recovery_max_delay_s, record.execution_scope,
            )
            current_contract = (
                contract.policy, contract.side_effect, contract.version, contract.key_valid_until,
                contract.recovery_initial_delay_s,
                contract.recovery_backoff_multiplier, contract.recovery_max_delay_s,
                contract.execution_scope,
            )
            legacy_unfrozen = (
                record.replay_contract_version == ""
                and record.replay_policy is ToolReplayPolicy.NO_AUTO_RETRY
                and record.side_effect is ToolSideEffect.UNKNOWN
            )
            if frozen_contract != current_contract and not legacy_unfrozen:
                action, reason = ToolRecoveryAction.BLOCK, "recovery_contract_changed"
            elif record.state is ToolCallState.PENDING:
                action, reason = ToolRecoveryAction.EXECUTE, "pending_not_started"
            elif record.state is ToolCallState.STARTED:
                if record.replay_policy.value in {"safe_retry", "keyed_idempotent"} and (
                    not record.execution_scope or not contract.execution_scope
                ):
                    action, reason = ToolRecoveryAction.BLOCK, "recovery_scope_missing"
                    decisions.append(ToolRecoveryDecision(record.call.call_id, action, reason))
                    continue
                key_valid = (
                    record.replay_policy.value == "keyed_idempotent"
                    and record.idempotency_key is not None
                    and (record.key_valid_until is None or datetime.fromisoformat(record.key_valid_until) > datetime.now(UTC))
                    and record.side_effect.value != "unknown"
                )
                retry_safe = record.last_execution_evidence in {None, ToolExecutionEvidence.UNKNOWN} and (
                    (record.replay_policy.value == "safe_retry" and record.side_effect.value == "none")
                    or key_valid
                )
                if not retry_safe:
                    action, reason = ToolRecoveryAction.BLOCK, "started_outcome_unknown"
                elif record.retry_count < min(record.recovery_max_retries, contract.recovery_max_retries):
                    action, reason = ToolRecoveryAction.RETRY, "safe_retry_due"
                else:
                    action, reason = ToolRecoveryAction.BLOCK, "retry_budget_exhausted"
            elif record.state is ToolCallState.WAITING_APPROVAL:
                action, reason = (ToolRecoveryAction.EXECUTE, "approval_granted") if record.approval_decision is True else (ToolRecoveryAction.WAIT_APPROVAL, "approval_pending")
            elif record.state is ToolCallState.WAITING_CHILD:
                action, reason = ToolRecoveryAction.RESUME_CHILD, "delegation_pending"
            else:
                action, reason = ToolRecoveryAction.BLOCK, record.uncertainty_reason or "uncertain_outcome"
            decisions.append(ToolRecoveryDecision(record.call.call_id, action, reason))
        messages = await self._runs.load_messages(run.session_id, run.run_id)
        events = await self._runs.load_events(run.session_id, run.run_id)
        return ToolRecoveryPlan(run, messages, checkpoint, ledger, len(events) + 1, len(messages) + 1, tuple(decisions))

    async def plan_batch(
        self,
        run: AgentRun,
        source_message: RunMessage,
        contracts: Mapping[str, ToolRecoveryContract],
        checkpoint: RunCheckpoint,
    ) -> ToolCallLedger:
        self._assert_run(run, checkpoint)
        messages = await self._runs.load_messages(run.session_id, run.run_id)
        if source_message.kind is not RunMessageKind.LLM_RESPONSE or source_message.role is not MessageRole.ASSISTANT or source_message not in messages:
            raise ValueError("工具批次来源必须是已持久化的原始助手响应")
        if not source_message.tool_calls or len({call.call_id for call in source_message.tool_calls}) != len(source_message.tool_calls):
            raise ValueError("助手响应必须包含唯一工具调用")
        existing = await self._ledger(run.session_id, run.run_id)
        if existing is not None:
            if existing.pending_projection is not None:
                await self._apply_projection(existing)
                existing = await self._ledger(run.session_id, run.run_id)
            records = tuple(item for item in existing.calls if item.source_message_id == source_message.message_id)
            if records:
                if tuple(record.call for record in records) != source_message.tool_calls:
                    raise ValueError("同批工具调用与冻结账本不一致")
                return existing
            if any(item.state not in _TERMINAL for item in existing.calls):
                raise ValueError("上一工具批次尚未完成，不能规划新批次")
        records: list[ToolCallRecord] = []
        for ordinal, call in enumerate(source_message.tool_calls):
            contract = contracts[call.call_id]
            key = None
            kind = ToolCallKind.DELEGATION if call.name == "delegate" else ToolCallKind.ORDINARY
            if contract.policy.value == "keyed_idempotent" and kind is ToolCallKind.ORDINARY:
                identity = json.dumps(["dotclaw-tool-v1", run.run_id, call.call_id], ensure_ascii=False, separators=(",", ":"))
                key = str(uuid.uuid5(uuid.NAMESPACE_URL, identity))
            records.append(ToolCallRecord(
                run_id=run.run_id, call=call, source_message_id=source_message.message_id,
                ordinal=ordinal, kind=kind, state=ToolCallState.PENDING,
                replay_policy=contract.policy, side_effect=contract.side_effect,
                replay_contract_version=contract.version, idempotency_key=key,
                key_valid_until=contract.key_valid_until, recovery_max_retries=contract.recovery_max_retries,
                recovery_initial_delay_s=contract.recovery_initial_delay_s,
                recovery_backoff_multiplier=contract.recovery_backoff_multiplier,
                recovery_max_delay_s=contract.recovery_max_delay_s,
                execution_scope=contract.execution_scope,
            ))
        base_ledger = existing or ToolCallLedger(run.session_id, run.run_id, 1, tuple(records))
        if existing is not None:
            base_ledger = replace(existing, calls=existing.calls + tuple(records))
        base_run, base_checkpoint = await self._persistent_base(run)
        pending = {"tool_batch_id": source_message.message_id, "pending_call_ids": [r.call.call_id for r in records]}
        target_checkpoint = self._next_checkpoint(base_checkpoint, checkpoint, pending, AgentAction.EXECUTE_TOOLS, None)
        target_run = replace(base_run, state=run.state)
        return await self._commit_fact(base_ledger, base_run, target_run, base_checkpoint, target_checkpoint)

    async def begin_attempt(self, run: AgentRun, call_id: str, checkpoint: RunCheckpoint, contract: ToolRecoveryContract | None = None) -> ToolCallRecord:
        ledger = await self._required_ledger(run, checkpoint)
        record = self._call(ledger, call_id)
        if record.state not in {ToolCallState.PENDING, ToolCallState.STARTED, ToolCallState.WAITING_APPROVAL}:
            raise ValueError("工具调用当前状态不允许开始执行")
        retry = record.state is ToolCallState.STARTED
        legacy_first_attempt = (
            not retry
            and record.replay_contract_version == ""
            and record.replay_policy is ToolReplayPolicy.NO_AUTO_RETRY
            and record.side_effect is ToolSideEffect.UNKNOWN
        )
        if record.kind is ToolCallKind.ORDINARY and (retry or contract is not None) and not legacy_first_attempt:
            if contract is None or (
                record.replay_policy, record.side_effect, record.replay_contract_version,
                record.key_valid_until,
                record.recovery_initial_delay_s, record.recovery_backoff_multiplier,
                record.recovery_max_delay_s, record.execution_scope,
            ) != (
                contract.policy, contract.side_effect, contract.version, contract.key_valid_until,
                contract.recovery_initial_delay_s,
                contract.recovery_backoff_multiplier, contract.recovery_max_delay_s,
                contract.execution_scope,
            ):
                raise ValueError("工具恢复契约与冻结记录不一致")
        if retry and (
            not record.execution_scope
            or contract is None
            or not contract.execution_scope
            or
            not (
                (record.replay_policy.value == "safe_retry" and record.side_effect.value == "none")
                or (
                    record.replay_policy.value == "keyed_idempotent"
                    and record.idempotency_key is not None
                    and record.side_effect.value != "unknown"
                    and (record.key_valid_until is None or datetime.fromisoformat(record.key_valid_until) > datetime.now(UTC))
                )
            )
            or record.last_execution_evidence not in {None, ToolExecutionEvidence.UNKNOWN}
            or record.retry_count >= min(record.recovery_max_retries, contract.recovery_max_retries)
            or record.recovery_retry_not_before is None
            or datetime.fromisoformat(record.recovery_retry_not_before) > datetime.now(UTC)
        ):
            raise ValueError("工具调用不满足安全重试条件")
        if record.state is ToolCallState.WAITING_APPROVAL and record.approval_decision is not True:
            raise ValueError("未批准的调用不能开始执行")
        updated = replace(
            record, state=ToolCallState.STARTED, attempt=record.attempt + 1,
            retry_count=record.retry_count + (1 if retry else 0),
            recovery_retry_not_before=None,
            attempt_started_at=utc_now_iso(), updated_at=utc_now_iso(),
        )
        record.validate_transition(updated.state)
        calls = tuple(updated if item.call.call_id == call_id else item for item in ledger.calls)
        base_run, base_checkpoint = await self._persistent_base(run)
        if not isinstance(base_run.state.mode, Running) or base_run.state.mode.stage is not RunStage.EXECUTING_TOOLS:
            raise ValueError("只有处于执行工具阶段的运行可以开始工具调用")
        event = await self._event(
            base_run,
            RunEventType.TOOL_STARTED,
            {
                "call_id": call_id,
                "tool_name": record.call.name,
                "source_response_message_id": record.source_message_id,
                "attempt": updated.attempt,
                "status": "started",
            },
            "工具调用开始",
            (record.source_message_id,),
        )
        pending = dict(base_checkpoint.pending if base_checkpoint else {})
        pending.setdefault("pending_call_ids", [item.call.call_id for item in calls if item.state not in _TERMINAL])
        target_checkpoint = self._next_checkpoint(base_checkpoint, checkpoint, pending, AgentAction.EXECUTE_TOOLS, None)
        target_run = base_run
        await self._commit_fact(replace(ledger, calls=calls), base_run, target_run, base_checkpoint, target_checkpoint, events=(event,))
        return updated

    async def commit_outcome(
        self,
        run: AgentRun,
        call_id: str,
        outcome: ToolOutcomeSnapshot,
        checkpoint: RunCheckpoint,
    ) -> ToolCallLedger:
        ledger = await self._required_ledger(run, checkpoint)
        record = self._call(ledger, call_id)
        if record.result is not None:
            if record.result != outcome:
                raise ValueError("工具调用已有不同终态结果")
            return ledger
        if record.state not in {ToolCallState.STARTED, ToolCallState.WAITING_CHILD}:
            raise ValueError("工具调用尚未开始或已处于其他终态")
        target_state = {
            ToolOutcomeStatus.COMPLETED: ToolCallState.SUCCEEDED,
            ToolOutcomeStatus.FAILED: ToolCallState.FAILED,
            ToolOutcomeStatus.CANCELLED: ToolCallState.CANCELLED,
        }[outcome.status]
        record.validate_transition(target_state)
        updated = replace(record, state=target_state, result=outcome, updated_at=utc_now_iso())
        calls = tuple(updated if item.call.call_id == call_id else item for item in ledger.calls)
        base_run, base_checkpoint = await self._persistent_base(run)
        all_pending = [item.call.call_id for item in calls if item.state not in _TERMINAL]
        pending = dict(base_checkpoint.pending if base_checkpoint else {})
        pending["pending_call_ids"] = all_pending
        if not all_pending:
            pending.pop("approval_id", None)
            pending.pop("child_run_id", None)
        target_checkpoint = self._next_checkpoint(base_checkpoint, checkpoint, pending, AgentAction.EXECUTE_TOOLS, None)
        target_run = base_run
        if outcome.status in {ToolOutcomeStatus.COMPLETED, ToolOutcomeStatus.FAILED}:
            target_run = replace(
                base_run,
                statistics=replace(base_run.statistics, tool_call_count=base_run.statistics.tool_call_count + 1),
            )
        messages: tuple[RunMessage, ...] = ()
        message_ids: tuple[str, ...] = ()
        if outcome.status is not ToolOutcomeStatus.CANCELLED:
            existing_messages = await self._runs.load_messages(run.session_id, run.run_id)
            message_id = outcome.message_id or f"tool-result-{uuid.uuid5(uuid.NAMESPACE_URL, run.run_id + ':' + call_id).hex}"
            message = RunMessage(message_id, len(existing_messages) + 1, outcome.message_kind, MessageRole.TOOL, outcome.output, call_id, record.call.name)
            messages = (message,)
            message_ids = (message.message_id,)
        event = await self._event(
            base_run,
            RunEventType.TOOL_COMPLETED,
            {
                "call_id": call_id,
                "tool_name": record.call.name,
                "source_response_message_id": record.source_message_id,
                "attempt": record.attempt,
                "status": outcome.status.value,
                "result_message_id": message_ids[0] if message_ids else "",
                "error_summary": outcome.output if outcome.status is ToolOutcomeStatus.FAILED else "",
                "tool_error_code": outcome.tool_error_code,
                "tool_error_type": outcome.tool_error_type,
                "child_outcome": None if outcome.child_outcome is None else outcome.child_outcome.value,
            },
            "工具调用已完成",
            message_ids,
        )
        events = (event,)
        if record.kind is ToolCallKind.DELEGATION:
            if outcome.status is not ToolOutcomeStatus.COMPLETED or outcome.message_kind is not RunMessageKind.DELEGATION_RESULT or record.child_run_id is None or outcome.message_id is None:
                raise ValueError("委派调用必须以 DELEGATION_RESULT 和稳定子运行结局提交")
            if isinstance(base_run.state.mode, Suspended) and base_run.state.mode.reason is SuspendReason.DELEGATION:
                target_run = replace(target_run, state=transition(base_run.state, DelegationCompleted(record.child_run_id)).state)
            elif not (isinstance(base_run.state.mode, Running) and base_run.state.mode.stage is RunStage.EXECUTING_TOOLS):
                raise ValueError("委派结果只能从委派挂起或工具恢复阶段提交")
            delegation_event = RunEvent(
                run.run_id, event.sequence + 1, RunEventType.DELEGATION_COMPLETED, utc_now_iso(),
                (outcome.message_id,), "委派子运行已完成",
                {"child_run_id": record.child_run_id, "outcome": outcome.child_outcome.value if outcome.child_outcome else ""},
            )
            events = (event, delegation_event)
        return await self._commit_fact(
            replace(ledger, calls=calls), base_run, target_run, base_checkpoint, target_checkpoint,
            messages=messages, events=events,
        )

    async def cancel_unfinished(self, run: AgentRun, checkpoint: RunCheckpoint) -> ToolCallLedger:
        """将取消收尾时账本中的非终态调用标为取消，并保留未知效果证据。"""
        ledger = await self._required_ledger(run, checkpoint)
        unfinished = tuple(item for item in ledger.calls if item.state not in _TERMINAL)
        if not unfinished:
            return ledger
        now = utc_now_iso()
        calls = list(ledger.calls)
        events = await self._runs.load_events(run.session_id, run.run_id)
        new_events: list[RunEvent] = []
        for record in unfinished:
            record.validate_transition(ToolCallState.CANCELLED)
            certainty = ToolEffectCertainty.UNKNOWN if record.state in {
                ToolCallState.STARTED, ToolCallState.WAITING_CHILD, ToolCallState.UNCERTAIN,
            } or record.last_execution_evidence is ToolExecutionEvidence.UNKNOWN else ToolEffectCertainty.KNOWN
            outcome = ToolOutcomeSnapshot(
                ToolOutcomeStatus.CANCELLED, "", None, None, now, certainty,
                message_kind=RunMessageKind.DELEGATION_RESULT if record.kind is ToolCallKind.DELEGATION else RunMessageKind.TOOL_RESULT,
            )
            updated = replace(record, state=ToolCallState.CANCELLED, result=outcome, updated_at=now)
            calls[calls.index(record)] = updated
            new_events.append(RunEvent(
                run.run_id, len(events) + len(new_events) + 1, RunEventType.TOOL_COMPLETED, now,
                message_ids=(record.source_message_id,), summary="工具调用因运行取消而收口",
                data={"call_id": record.call.call_id, "tool_name": record.call.name,
                    "status": "cancelled", "effect_certainty": certainty.value,
                    "source_response_message_id": record.source_message_id, "attempt": record.attempt},
            ))
        base_run, base_checkpoint = await self._persistent_base(run)
        pending = dict(base_checkpoint.pending if base_checkpoint else {})
        pending["pending_call_ids"] = [item.call.call_id for item in calls if item.state not in _TERMINAL]
        target_checkpoint = self._next_checkpoint(base_checkpoint, checkpoint, pending, checkpoint.action, None)
        return await self._commit_fact(
            replace(ledger, calls=tuple(calls)), base_run, base_run, base_checkpoint, target_checkpoint,
            events=tuple(new_events),
        )

    async def complete_batch(self, run: AgentRun, batch_id: str, checkpoint: RunCheckpoint) -> ToolCallLedger:
        ledger = await self._required_ledger(run, checkpoint)
        calls = tuple(item for item in ledger.calls if item.source_message_id == batch_id)
        if not calls:
            raise ValueError("工具批次不存在")
        if any(item.state is not ToolCallState.SUCCEEDED for item in calls):
            raise ValueError("只有所有调用成功取得结果后才能完成批次")
        base_run, base_checkpoint = await self._persistent_base(run)
        if isinstance(base_run.state.mode, Running) and base_run.state.mode.stage is RunStage.CALLING_LLM:
            return ledger
        result_ids = tuple(item.result.message_id for item in calls if item.result and item.result.message_id)
        moved = transition(base_run.state, ToolBatchCompleted(result_ids))
        target_run = replace(base_run, state=moved.state)
        target_checkpoint = self._next_checkpoint(base_checkpoint, checkpoint, {}, AgentAction.INVOKE_LLM, False)
        return await self._commit_fact(ledger, base_run, target_run, base_checkpoint, target_checkpoint)

    async def wait_approval(self, run: AgentRun, call_id: str, approval_id: str, checkpoint: RunCheckpoint) -> ToolCallLedger:
        ledger = await self._required_ledger(run, checkpoint)
        record = self._call(ledger, call_id)
        if record.state is ToolCallState.WAITING_APPROVAL and record.approval_id == approval_id:
            return ledger
        if record.state not in {ToolCallState.PENDING, ToolCallState.STARTED}:
            raise ValueError("当前调用不能进入审批等待")
        record.validate_transition(ToolCallState.WAITING_APPROVAL)
        updated = replace(record, state=ToolCallState.WAITING_APPROVAL, approval_id=approval_id, approval_decision=None, updated_at=utc_now_iso())
        approval = ApprovalRecord(approval_id, run.run_id, run.session_id, ApprovalStatus.PENDING, utc_now_iso(), call_id=call_id)
        calls = tuple(updated if item.call.call_id == call_id else item for item in ledger.calls)
        base_run, base_checkpoint = await self._persistent_base(run)
        moved = transition(base_run.state, ToolApprovalRequired(approval_id))
        target_run = replace(base_run, state=moved.state)
        pending = dict(base_checkpoint.pending if base_checkpoint else {})
        pending.update({"approval_id": approval_id, "pending_call_ids": [item.call.call_id for item in calls if item.state not in _TERMINAL]})
        target_checkpoint = self._next_checkpoint(base_checkpoint, checkpoint, pending, AgentAction.SUSPEND, None)
        event = await self._event(
            base_run,
            RunEventType.WAITING_APPROVAL,
            {
                "approval_id": approval_id,
                "call_id": call_id,
                "tool_name": record.call.name,
                "source_response_message_id": record.source_message_id,
                "attempt": record.attempt,
                "status": "approval_required",
            },
            "工具调用等待审批",
            (record.source_message_id,),
        )
        return await self._commit_fact(replace(ledger, calls=calls), base_run, target_run, base_checkpoint, target_checkpoint, events=(event,), approval=approval)

    async def decide_approval(self, run: AgentRun, approval_id: str, approved: bool, checkpoint: RunCheckpoint | None) -> ToolCallLedger:
        await self.recover_projection(run.session_id, run.run_id)
        ledger = await self._ledger(run.session_id, run.run_id)
        if ledger is None:
            raise ValueError("审批决议关联的工具账本不存在")
        records = [item for item in ledger.calls if item.approval_id == approval_id]
        if len(records) != 1:
            raise ValueError("审批 ID 未唯一关联账本调用")
        record = records[0]
        approval = await self._approvals.find(approval_id)
        if approval is None or (approval.run_id, approval.session_id, approval.call_id) != (run.run_id, run.session_id, record.call.call_id):
            raise ValueError("审批记录与账本调用身份不匹配")
        if approval.decision is not None and approval.decision is not approved:
            raise ValueError("审批仓储已保存相反决议")
        if approval.status is ApprovalStatus.CONSUMED and approval.decision is None:
            raise ValueError("旧消费记录缺少决议证据，禁止从账本补猜审批结论")
        if record.approval_decision is not None:
            if record.approval_decision is not approved:
                raise ValueError("审批决议不可翻转")
            if approval.decision is not record.approval_decision:
                raise ValueError("审批账本与仓储决议不一致")
            return ledger
        if run.state.is_ended():
            raise ValueError("运行已结束，不能新增审批决议")
        if checkpoint is None:
            raise ValueError("未决审批缺少活动检查点")
        ledger = await self._required_ledger(run, checkpoint)
        record = self._call(ledger, record.call.call_id)
        if record.state is not ToolCallState.WAITING_APPROVAL:
            raise ValueError("调用不在审批等待状态")
        resolved = replace(approval, status=ApprovalStatus.CONSUMED, decision=approved, resolved_at=approval.resolved_at or utc_now_iso())
        updated = replace(record, approval_decision=approved, updated_at=utc_now_iso())
        calls = tuple(updated if item.call.call_id == record.call.call_id else item for item in ledger.calls)
        base_run, base_checkpoint = await self._persistent_base(run)
        moved = transition(base_run.state, ApprovalGranted(approval_id)) if approved else None
        target_run = base_run if moved is None else replace(base_run, state=moved.state)
        events = (await self._event(base_run, RunEventType.APPROVAL_RESOLVED, {"approval_id": approval_id, "call_id": record.call.call_id, "tool_name": record.call.name, "source_response_message_id": record.source_message_id, "attempt": record.attempt, "approved": approved}, "审批决议已持久化", (record.source_message_id,)),)
        target_checkpoint = self._next_checkpoint(base_checkpoint, checkpoint, dict(base_checkpoint.pending if base_checkpoint else {}), AgentAction.EXECUTE_TOOLS if approved else AgentAction.SUSPEND, None)
        return await self._commit_fact(replace(ledger, calls=calls), base_run, target_run, base_checkpoint, target_checkpoint, events=events, approval=resolved)

    async def attach_child(self, run: AgentRun, call_id: str, child_run_id: str, checkpoint: RunCheckpoint) -> ToolCallLedger:
        ledger = await self._required_ledger(run, checkpoint)
        record = self._call(ledger, call_id)
        if record.kind is not ToolCallKind.DELEGATION:
            raise ValueError("只有委派调用可以关联子运行")
        child = await self._runs.find_run(child_run_id)
        if child is None or child.parent_run_id != run.run_id or child.root_run_id != (run.root_run_id or run.run_id):
            raise ValueError("子运行与父调用归属不匹配")
        target_agent_id = record.call.arguments.get("target_agent_id")
        title = record.call.arguments.get("title")
        objective = record.call.arguments.get("objective")
        child_messages = await self._runs.load_messages(child.session_id, child.run_id)
        initial = next((message for message in child_messages if message.message_id == child.input_message_id), None)
        expected_input = f"任务：{title}\n\n目标：{objective}"
        if child.agent_id != target_agent_id or initial is None or initial.role is not MessageRole.USER or initial.content != expected_input or not child.delegation_task_id:
            raise ValueError("子运行目标、输入或业务任务与父调用不匹配")
        if record.child_run_id is not None and record.child_run_id != child_run_id:
            raise ValueError("委派调用已关联不同子运行")
        if record.state is ToolCallState.WAITING_CHILD:
            return ledger
        record.validate_transition(ToolCallState.WAITING_CHILD)
        updated = replace(record, state=ToolCallState.WAITING_CHILD, child_run_id=child_run_id, updated_at=utc_now_iso())
        calls = tuple(updated if item.call.call_id == call_id else item for item in ledger.calls)
        base_run, base_checkpoint = await self._persistent_base(run)
        target_run = replace(base_run, state=transition(base_run.state, DelegationSubmitted(child_run_id)).state)
        pending = dict(base_checkpoint.pending if base_checkpoint else {})
        pending.update({"child_run_id": child_run_id, "pending_call_ids": [item.call.call_id for item in calls if item.state not in _TERMINAL]})
        target_checkpoint = self._next_checkpoint(base_checkpoint, checkpoint, pending, AgentAction.EXECUTE_TOOLS, None)
        event = await self._event(base_run, RunEventType.DELEGATION_SUBMITTED, {"call_id": call_id, "tool_name": record.call.name, "source_response_message_id": record.source_message_id, "attempt": record.attempt, "child_run_id": child_run_id}, "委派子运行已关联", (record.source_message_id,))
        return await self._commit_fact(replace(ledger, calls=calls), base_run, target_run, base_checkpoint, target_checkpoint, events=(event,))

    async def commit_run_failure(self, run: AgentRun, error: RunError, checkpoint: RunCheckpoint | None) -> ToolCallLedger:
        ledger = await self._ledger(run.session_id, run.run_id) or ToolCallLedger(run.session_id, run.run_id, 1, ())
        if ledger.pending_projection is not None:
            await self._apply_projection(ledger)
            ledger = await self._ledger(run.session_id, run.run_id) or ledger
        base_run, base_checkpoint = await self._persistent_base(run)
        if isinstance(base_run.state.mode, Ended):
            if base_run.state.mode.outcome is RunOutcome.FAILED and base_run.error == error:
                return ledger
            raise ValueError("运行已以不同事实结束，不能覆盖为工具失败")
        target_run = self._failed_run_target(base_run, error)
        event = await self._event(base_run, RunEventType.RUN_FAILED, {"error_code": error.code.value}, error.message)
        return await self._commit_fact(ledger, base_run, target_run, base_checkpoint, None, events=(event,))

    async def record_interruption(self, run: AgentRun, call_id: str, evidence: ToolExecutionEvidence | None, error: RunError, checkpoint: RunCheckpoint) -> ToolCallLedger:
        ledger = await self._required_ledger(run, checkpoint)
        record = self._call(ledger, call_id)
        if record.state is not ToolCallState.STARTED:
            raise ValueError("只有 STARTED 调用可以记录中断诊断")
        retry_not_before = record.recovery_retry_not_before
        if retry_not_before is None:
            delay = min(
                record.recovery_initial_delay_s * record.recovery_backoff_multiplier ** record.retry_count,
                record.recovery_max_delay_s,
            )
            retry_not_before = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
        updated = replace(record, last_execution_evidence=evidence or ToolExecutionEvidence.UNKNOWN, last_error=error, recovery_retry_not_before=retry_not_before, updated_at=utc_now_iso())
        calls = tuple(updated if item.call.call_id == call_id else item for item in ledger.calls)
        base_run, base_checkpoint = await self._persistent_base(run)
        target_checkpoint = self._next_checkpoint(base_checkpoint, checkpoint, dict(base_checkpoint.pending if base_checkpoint else {}), AgentAction.EXECUTE_TOOLS, None)
        return await self._commit_fact(replace(ledger, calls=calls), base_run, base_run, base_checkpoint, target_checkpoint)

    async def block_uncertain(self, run: AgentRun, call_id: str, reason: str, checkpoint: RunCheckpoint) -> ToolCallLedger:
        ledger = await self._required_ledger(run, checkpoint)
        record = self._call(ledger, call_id)
        if record.state is ToolCallState.UNCERTAIN:
            if record.uncertainty_reason == reason:
                return ledger
            raise ValueError("工具调用已按不同原因阻止恢复")
        record.validate_transition(ToolCallState.UNCERTAIN)
        updated = replace(record, state=ToolCallState.UNCERTAIN, uncertainty_reason=reason, last_execution_evidence=record.last_execution_evidence, updated_at=utc_now_iso())
        calls = tuple(updated if item.call.call_id == call_id else item for item in ledger.calls)
        base_run, base_checkpoint = await self._persistent_base(run)
        error = RunError(RunErrorCode.TOOL_OUTCOME_UNCERTAIN, "工具结果待确认：外部操作可能已生效，请先核对。")
        target_run = self._failed_run_target(base_run, error)
        existing_events = await self._runs.load_events(run.session_id, run.run_id)
        blocked_event = RunEvent(run.run_id, len(existing_events) + 1, RunEventType.TOOL_RECOVERY_BLOCKED, utc_now_iso(), (record.source_message_id,), "工具结果不确定，已阻止自动执行", {"call_id": call_id, "tool_name": record.call.name, "source_response_message_id": record.source_message_id, "reason": reason, "attempt": record.attempt})
        failed_event = RunEvent(run.run_id, len(existing_events) + 2, RunEventType.RUN_FAILED, utc_now_iso(), (), reason, {"error_code": error.code.value})
        return await self._commit_fact(replace(ledger, calls=calls), base_run, target_run, base_checkpoint, None, events=(blocked_event, failed_event))

    def _failed_run_target(self, base_run: AgentRun, error: RunError) -> AgentRun:
        return replace(base_run, state=transition(base_run.state, ToolBatchFailed(error)).state, ended_at=utc_now_iso(), error=error)

    async def _commit_fact(
        self, ledger: ToolCallLedger, base_run: AgentRun, target_run: AgentRun,
        base_checkpoint: RunCheckpoint | None, target_checkpoint: RunCheckpoint | None,
        *, messages: tuple[RunMessage, ...] = (), events: tuple[RunEvent, ...] = (),
        approval: ApprovalRecord | None = None,
    ) -> ToolCallLedger:
        current = await self._runs.load_tool_ledger(ledger.session_id, ledger.run_id)
        expected = 0 if current is None else current.revision
        if current is not None and current.pending_projection is not None:
            await self._apply_projection(current)
            current = await self._runs.load_tool_ledger(ledger.session_id, ledger.run_id)
            expected = current.revision
        if current is not None and current.revision != ledger.revision:
            raise ValueError("工具账本在投影构建期间已变化")
        persisted_messages = await self._runs.load_messages(ledger.session_id, ledger.run_id)
        persisted_events = await self._runs.load_events(ledger.session_id, ledger.run_id)
        if any(message.sequence != len(persisted_messages) + index for index, message in enumerate(messages, start=1)):
            raise ValueError("工具投影消息未从持久序号末尾连续追加")
        if any(event.sequence != len(persisted_events) + index for index, event in enumerate(events, start=1)):
            raise ValueError("工具投影事件未从持久序号末尾连续追加")
        if target_checkpoint is not None:
            target_checkpoint = replace(
                target_checkpoint,
                message_sequence=len(persisted_messages) + len(messages),
                event_sequence=len(persisted_events) + len(events),
            )
        intent = self._build_projection(base_run, target_run, base_checkpoint, target_checkpoint, messages, events, approval, expected + 1)
        return await self._commit_projection(replace(ledger, revision=max(expected, 1)), intent, expected)

    def _build_projection(self, base_run: AgentRun, target_run: AgentRun, base_checkpoint: RunCheckpoint | None, target_checkpoint: RunCheckpoint | None, messages: tuple[RunMessage, ...], events: tuple[RunEvent, ...], approval: ApprovalRecord | None, revision: int) -> ToolProjectionIntent:
        if (base_run.session_id, base_run.run_id) != (target_run.session_id, target_run.run_id):
            raise ValueError("工具投影运行身份不一致")
        if target_checkpoint is not None:
            if (target_checkpoint.session_id, target_checkpoint.run_id) != (base_run.session_id, base_run.run_id):
                raise ValueError("工具投影检查点归属不一致")
            if target_checkpoint.tool_ledger_revision != revision:
                target_checkpoint = replace(target_checkpoint, tool_ledger_revision=revision)
        if len({message.message_id for message in messages}) != len(messages) or len({message.sequence for message in messages}) != len(messages):
            raise ValueError("工具投影消息 ID 与序号必须唯一")
        if any(right.sequence != left.sequence + 1 for left, right in zip(messages, messages[1:])):
            raise ValueError("工具投影消息序号必须连续")
        if any(right.sequence != left.sequence + 1 for left, right in zip(events, events[1:])):
            raise ValueError("工具投影事件序号必须连续")
        commit_identity = json.dumps([base_run.run_id, revision], ensure_ascii=False, separators=(",", ":"))
        commit_id = uuid.uuid5(uuid.NAMESPACE_URL, commit_identity).hex
        projected_events = tuple(
            replace(
                event,
                data={
                    **event.data,
                    "tool_event_semantics": 2,
                    "tool_protocol_version": 1,
                    "attempt": event.data.get("attempt", 0),
                    "commit_id": commit_id,
                },
            )
            for event in events
        )
        return ToolProjectionIntent(commit_id, revision, messages, projected_events, target_checkpoint, target_run, base_run, base_checkpoint, approval)

    async def _commit_projection(self, ledger: ToolCallLedger, intent: ToolProjectionIntent, expected_revision: int) -> ToolCallLedger:
        pending = replace(ledger, revision=expected_revision + 1, pending_projection=intent)
        try:
            await self._runs.save_tool_ledger(pending, expected_revision=expected_revision)
        except (RunPersistenceError, ValueError) as error:
            raise ToolProjectionPersistenceError(str(error)) from error
        return await self._apply_projection(pending)

    async def _apply_projection(self, ledger: ToolCallLedger) -> ToolCallLedger:
        intent = ledger.pending_projection
        if intent is None:
            return ledger
        try:
            if intent.approval is not None:
                await self._approvals.ensure_projection(intent.approval)
            await self._runs.ensure_tool_messages(ledger.session_id, ledger.run_id, intent.messages)
            await self._runs.ensure_tool_events(ledger.session_id, intent.events)
            await self._checkpoints.ensure_tool_checkpoint(intent.base_checkpoint, intent.checkpoint)
            await self._runs.ensure_tool_run(intent.base_run, intent.run)
            completed = replace(ledger, revision=ledger.revision + 1, pending_projection=None)
            await self._runs.save_tool_ledger(completed, expected_revision=ledger.revision)
        except (RunPersistenceError, ValueError) as error:
            raise ToolProjectionPersistenceError(str(error)) from error
        return completed

    async def _required_ledger(self, run: AgentRun, checkpoint: RunCheckpoint) -> ToolCallLedger:
        self._assert_run(run, checkpoint)
        await self.recover_projection(run.session_id, run.run_id)
        ledger = await self._ledger(run.session_id, run.run_id)
        if ledger is None:
            raise ValueError("工具调用账本不存在")
        return ledger

    async def _ledger(self, session_id: str, run_id: str) -> ToolCallLedger | None:
        ledger = await self._runs.load_tool_ledger(session_id, run_id)
        if ledger is not None and (ledger.session_id, ledger.run_id) != (session_id, run_id):
            raise ValueError("工具账本归属不匹配")
        return ledger

    async def _persistent_base(self, run: AgentRun) -> tuple[AgentRun, RunCheckpoint | None]:
        base_run = await self._runs.load_run(run.session_id, run.run_id)
        if base_run is None:
            raise ValueError("持久运行不存在")
        base_checkpoint = await self._checkpoints.load(run.session_id, run.run_id)
        return base_run, base_checkpoint

    def _next_checkpoint(self, base: RunCheckpoint | None, template: RunCheckpoint, pending: dict, action: AgentAction, replay_context: bool | None) -> RunCheckpoint:
        sequence = 1 if base is None else base.checkpoint_sequence + 1
        return replace(template, checkpoint_id=f"{template.run_id}-checkpoint-{sequence}", checkpoint_sequence=sequence, event_sequence=0, message_sequence=0, pending=pending, action=action, tool_ledger_revision=None, replay_context=replay_context)

    async def _event(self, run: AgentRun, event_type: RunEventType, data: dict, summary: str, message_ids: tuple[str, ...] = ()) -> RunEvent:
        events = await self._runs.load_events(run.session_id, run.run_id)
        return RunEvent(run.run_id, len(events) + 1, event_type, utc_now_iso(), message_ids, summary, data)

    def _assert_run(self, run: AgentRun, checkpoint: RunCheckpoint) -> None:
        if (run.session_id, run.run_id) != (checkpoint.session_id, checkpoint.run_id):
            raise ValueError("工具调用运行与检查点身份不一致")

    def _call(self, ledger: ToolCallLedger, call_id: str) -> ToolCallRecord:
        matches = [item for item in ledger.calls if item.call.call_id == call_id]
        if len(matches) != 1:
            raise ValueError("工具调用 ID 未唯一关联账本")
        return matches[0]


_TERMINAL = {ToolCallState.SUCCEEDED, ToolCallState.FAILED, ToolCallState.CANCELLED}
