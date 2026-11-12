"""工具调用账本的领域模型与严格 JSON 契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from math import isfinite
from typing import Mapping

from ...common.tool_replay import ToolExecutionEvidence, ToolReplayPolicy, ToolSideEffect
from .context import StagedHistoryCompression, StagedHistoryCompressionStatus, SuccessCommitIntent
from .control import AgentAction
from .events import RunEvent, RunEventType
from .facts import (
    AgentPolicySnapshot,
    AgentRun,
    ApprovalRecord,
    ApprovalStatus,
    JSONMap,
    MessageRole,
    RunCheckpoint,
    RunError,
    RunErrorCode,
    RunMessage,
    RunMessageKind,
    RunStatistics,
    ToolCall,
    require_json_map,
    utc_now_iso,
)
from .state import AgentRunState, RunOutcome


class ToolCallState(StrEnum):
    """单个工具调用的持久生命周期状态。"""

    PENDING = "pending"
    STARTED = "started"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_CHILD = "waiting_child"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"


class ToolCallKind(StrEnum):
    """调用进入普通工具执行链或委派执行链。"""

    ORDINARY = "ordinary"
    DELEGATION = "delegation"


class ToolOutcomeStatus(StrEnum):
    """已裁定工具结果的终态。"""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ToolEffectCertainty(StrEnum):
    """结果快照对副作用是否已确定的判断。"""

    KNOWN = "known"
    UNKNOWN = "unknown"


class ToolRecoveryAction(StrEnum):
    """恢复规划器对单个调用允许采取的动作。"""

    EXECUTE = "execute"
    RETRY = "retry"
    REUSE = "reuse"
    WAIT_APPROVAL = "wait_approval"
    RESUME_CHILD = "resume_child"
    BLOCK = "block"


@dataclass(frozen=True)
class ToolOutcomeSnapshot:
    """可重建的工具终态结果，不含审批等待或未知执行效果。"""

    status: ToolOutcomeStatus
    output: str
    error: RunError | None
    message_id: str | None
    observed_at: str
    effect_certainty: ToolEffectCertainty
    message_kind: RunMessageKind = RunMessageKind.TOOL_RESULT
    child_outcome: RunOutcome | None = None
    tool_error_code: str | None = None
    tool_error_type: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.output, str) or not self.observed_at:
            raise ValueError("工具结果正文和观测时间必须存在")
        _validate_utc(self.observed_at, "observed_at")
        if self.status in {ToolOutcomeStatus.COMPLETED, ToolOutcomeStatus.FAILED} and not self.message_id:
            raise ValueError("成功或失败工具结果必须引用稳定消息")
        if self.status is ToolOutcomeStatus.FAILED and self.error is None:
            raise ValueError("失败工具结果必须包含运行错误")
        if self.status is ToolOutcomeStatus.COMPLETED and self.error is not None:
            raise ValueError("成功工具结果不能包含失败错误")
        if self.message_kind is RunMessageKind.TOOL_RESULT and self.child_outcome is not None:
            raise ValueError("普通工具结果不能包含子运行结局")
        if self.message_kind not in {RunMessageKind.TOOL_RESULT, RunMessageKind.DELEGATION_RESULT}:
            raise ValueError("工具结果消息类别必须是 TOOL_RESULT 或 DELEGATION_RESULT")

    def to_dict(self) -> JSONMap:
        return {
            "status": self.status.value,
            "output": self.output,
            "error": None if self.error is None else self.error.to_dict(),
            "message_id": self.message_id,
            "observed_at": self.observed_at,
            "effect_certainty": self.effect_certainty.value,
            "message_kind": self.message_kind.value,
            "child_outcome": None if self.child_outcome is None else self.child_outcome.value,
            "tool_error_code": self.tool_error_code,
            "tool_error_type": self.tool_error_type,
        }

    @classmethod
    def from_dict(cls, data: JSONMap) -> ToolOutcomeSnapshot:
        _reject_unknown(data, {"status", "output", "error", "message_id", "observed_at", "effect_certainty", "message_kind", "child_outcome", "tool_error_code", "tool_error_type"}, "ToolOutcomeSnapshot")
        error = _optional_error(data.get("error"))
        return cls(
            status=ToolOutcomeStatus(_required_string(data, "status")),
            output=_required_string(data, "output", allow_empty=True),
            error=error,
            message_id=_optional_string(data.get("message_id"), "message_id"),
            observed_at=_required_string(data, "observed_at"),
            effect_certainty=ToolEffectCertainty(_required_string(data, "effect_certainty")),
            message_kind=RunMessageKind(_optional_string(data.get("message_kind"), "message_kind") or RunMessageKind.TOOL_RESULT.value),
            child_outcome=_optional_enum(data.get("child_outcome"), RunOutcome, "child_outcome"),
            tool_error_code=_optional_string(data.get("tool_error_code"), "tool_error_code"),
            tool_error_type=_optional_string(data.get("tool_error_type"), "tool_error_type"),
        )


@dataclass(frozen=True)
class ToolCallRecord:
    """一个冻结工具调用及其最后一次可靠执行事实。"""

    run_id: str
    call: ToolCall
    source_message_id: str
    ordinal: int
    kind: ToolCallKind
    state: ToolCallState
    attempt: int = 0
    replay_policy: ToolReplayPolicy = ToolReplayPolicy.NO_AUTO_RETRY
    side_effect: ToolSideEffect = ToolSideEffect.UNKNOWN
    replay_contract_version: str = ""
    idempotency_key: str | None = None
    key_valid_until: str | None = None
    retry_count: int = 0
    recovery_max_retries: int = 0
    recovery_initial_delay_s: float = 1.0
    recovery_backoff_multiplier: float = 2.0
    recovery_max_delay_s: float = 30.0
    recovery_retry_not_before: str | None = None
    last_execution_evidence: ToolExecutionEvidence | None = None
    last_error: RunError | None = None
    last_tool_error_code: str | None = None
    last_tool_error_type: str | None = None
    attempt_started_at: str | None = None
    approval_id: str | None = None
    approval_decision: bool | None = None
    child_run_id: str | None = None
    result: ToolOutcomeSnapshot | None = None
    uncertainty_reason: str | None = None
    updated_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        if not self.run_id or not self.call.call_id or not self.call.name or not self.source_message_id:
            raise ValueError("工具调用运行、调用、工具和来源消息标识必须非空")
        for name in ("ordinal", "attempt", "retry_count", "recovery_max_retries"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} 必须是非负整数，且不能是布尔值")
        _validate_recovery_budget(
            self.recovery_max_retries,
            self.recovery_initial_delay_s,
            self.recovery_backoff_multiplier,
            self.recovery_max_delay_s,
        )
        if self.replay_policy is ToolReplayPolicy.SAFE_RETRY and self.side_effect is not ToolSideEffect.NONE:
            raise ValueError("SAFE_RETRY 仅允许副作用声明为 NONE 的工具")
        if self.replay_policy in {ToolReplayPolicy.SAFE_RETRY, ToolReplayPolicy.KEYED_IDEMPOTENT} and not self.replay_contract_version:
            raise ValueError("安全重放策略必须冻结非空契约版本")
        if self.replay_policy is ToolReplayPolicy.KEYED_IDEMPOTENT and not self.idempotency_key:
            raise ValueError("KEYED_IDEMPOTENT 调用必须有稳定幂等键")
        if self.approval_decision is not None and not isinstance(self.approval_decision, bool):
            raise ValueError("approval_decision 必须为布尔值或 null")
        if self.approval_decision is not None and not self.approval_id:
            raise ValueError("审批决议必须关联 approval_id")
        if self.state is ToolCallState.STARTED and self.approval_decision is False:
            raise ValueError("已拒绝的工具调用不能进入 STARTED")
        if self.kind is not ToolCallKind.DELEGATION and self.child_run_id is not None:
            raise ValueError("普通工具调用不能关联子运行")
        if self.state is ToolCallState.WAITING_CHILD and (self.kind is not ToolCallKind.DELEGATION or not self.child_run_id):
            raise ValueError("WAITING_CHILD 仅允许关联稳定子运行的委派调用")
        if self.state is ToolCallState.WAITING_APPROVAL and not self.approval_id:
            raise ValueError("WAITING_APPROVAL 必须关联 approval_id")
        if self.state in {ToolCallState.PENDING, ToolCallState.STARTED, ToolCallState.WAITING_APPROVAL, ToolCallState.WAITING_CHILD, ToolCallState.UNCERTAIN} and self.result is not None:
            raise ValueError("非结果终态不能包含工具结果快照")
        if self.state is ToolCallState.SUCCEEDED and (self.result is None or self.result.status is not ToolOutcomeStatus.COMPLETED):
            raise ValueError("SUCCEEDED 必须包含 completed 结果快照")
        if self.state is ToolCallState.FAILED and (self.result is None or self.result.status is not ToolOutcomeStatus.FAILED):
            raise ValueError("FAILED 必须包含 failed 结果快照")
        if self.state is ToolCallState.CANCELLED and (self.result is None or self.result.status is not ToolOutcomeStatus.CANCELLED):
            raise ValueError("CANCELLED 必须包含 cancelled 结果快照")
        if self.result is not None:
            expected_message_kind = (
                RunMessageKind.DELEGATION_RESULT
                if self.kind is ToolCallKind.DELEGATION
                else RunMessageKind.TOOL_RESULT
            )
            if self.result.message_kind is not expected_message_kind:
                raise ValueError("工具调用类型与结果消息类别不一致")
        if self.state is ToolCallState.UNCERTAIN and not self.uncertainty_reason:
            raise ValueError("UNCERTAIN 必须保留明确原因")
        if not self.updated_at:
            raise ValueError("updated_at 必须非空")
        _validate_utc(self.updated_at, "updated_at")
        _validate_optional_utc(self.key_valid_until, "key_valid_until")
        _validate_optional_utc(self.recovery_retry_not_before, "recovery_retry_not_before")
        _validate_optional_utc(self.attempt_started_at, "attempt_started_at")

    def validate_transition(self, target: ToolCallState) -> None:
        """只校验状态图边；STARTED 重试的 attempt/retry_count 增量由提交入口校验。"""
        allowed = {
            ToolCallState.PENDING: {ToolCallState.STARTED, ToolCallState.WAITING_APPROVAL, ToolCallState.WAITING_CHILD, ToolCallState.CANCELLED},
            ToolCallState.STARTED: {ToolCallState.STARTED, ToolCallState.SUCCEEDED, ToolCallState.FAILED, ToolCallState.WAITING_APPROVAL, ToolCallState.WAITING_CHILD, ToolCallState.UNCERTAIN, ToolCallState.CANCELLED},
            ToolCallState.WAITING_APPROVAL: {ToolCallState.STARTED, ToolCallState.CANCELLED},
            ToolCallState.WAITING_CHILD: {ToolCallState.SUCCEEDED, ToolCallState.CANCELLED},
            ToolCallState.SUCCEEDED: {ToolCallState.SUCCEEDED},
            ToolCallState.FAILED: {ToolCallState.FAILED},
            ToolCallState.CANCELLED: {ToolCallState.CANCELLED},
            ToolCallState.UNCERTAIN: {ToolCallState.UNCERTAIN, ToolCallState.CANCELLED},
        }
        if target not in allowed[self.state]:
            raise ValueError(f"不允许工具调用状态迁移：{self.state.value} -> {target.value}")
        if target is ToolCallState.WAITING_CHILD and self.kind is not ToolCallKind.DELEGATION:
            raise ValueError("WAITING_CHILD 仅允许委派调用")

    def to_dict(self) -> JSONMap:
        return {
            "run_id": self.run_id,
            "call": self.call.to_dict(),
            "source_message_id": self.source_message_id,
            "ordinal": self.ordinal,
            "kind": self.kind.value,
            "state": self.state.value,
            "attempt": self.attempt,
            "replay_policy": self.replay_policy.value,
            "side_effect": self.side_effect.value,
            "replay_contract_version": self.replay_contract_version,
            "idempotency_key": self.idempotency_key,
            "key_valid_until": self.key_valid_until,
            "retry_count": self.retry_count,
            "recovery_max_retries": self.recovery_max_retries,
            "recovery_initial_delay_s": self.recovery_initial_delay_s,
            "recovery_backoff_multiplier": self.recovery_backoff_multiplier,
            "recovery_max_delay_s": self.recovery_max_delay_s,
            "recovery_retry_not_before": self.recovery_retry_not_before,
            "last_execution_evidence": None if self.last_execution_evidence is None else self.last_execution_evidence.value,
            "last_error": None if self.last_error is None else self.last_error.to_dict(),
            "last_tool_error_code": self.last_tool_error_code,
            "last_tool_error_type": self.last_tool_error_type,
            "attempt_started_at": self.attempt_started_at,
            "approval_id": self.approval_id,
            "approval_decision": self.approval_decision,
            "child_run_id": self.child_run_id,
            "result": None if self.result is None else self.result.to_dict(),
            "uncertainty_reason": self.uncertainty_reason,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: JSONMap) -> ToolCallRecord:
        fields = {"run_id", "call", "source_message_id", "ordinal", "kind", "state", "attempt", "replay_policy", "side_effect", "replay_contract_version", "idempotency_key", "key_valid_until", "retry_count", "recovery_max_retries", "recovery_initial_delay_s", "recovery_backoff_multiplier", "recovery_max_delay_s", "recovery_retry_not_before", "last_execution_evidence", "last_error", "last_tool_error_code", "last_tool_error_type", "attempt_started_at", "approval_id", "approval_decision", "child_run_id", "result", "uncertainty_reason", "updated_at"}
        _reject_unknown(data, fields, "ToolCallRecord")
        call_data = require_json_map(data.get("call"))
        call = ToolCall(
            _required_string(call_data, "call_id"),
            _required_string(call_data, "name"),
            _required_map(call_data.get("arguments"), "call.arguments"),
        )
        return cls(
            run_id=_required_string(data, "run_id"),
            call=call,
            source_message_id=_required_string(data, "source_message_id"),
            ordinal=_required_integer(data, "ordinal"),
            kind=ToolCallKind(_required_string(data, "kind")),
            state=ToolCallState(_required_string(data, "state")),
            attempt=_optional_integer(data.get("attempt"), "attempt", 0),
            replay_policy=ToolReplayPolicy(_optional_string(data.get("replay_policy"), "replay_policy") or ToolReplayPolicy.NO_AUTO_RETRY.value),
            side_effect=ToolSideEffect(_optional_string(data.get("side_effect"), "side_effect") or ToolSideEffect.UNKNOWN.value),
            replay_contract_version=_optional_string(data.get("replay_contract_version"), "replay_contract_version") or "",
            idempotency_key=_optional_string(data.get("idempotency_key"), "idempotency_key"),
            key_valid_until=_optional_string(data.get("key_valid_until"), "key_valid_until"),
            retry_count=_optional_integer(data.get("retry_count"), "retry_count", 0),
            recovery_max_retries=_optional_integer(data.get("recovery_max_retries"), "recovery_max_retries", 0),
            recovery_initial_delay_s=_optional_number(data.get("recovery_initial_delay_s"), "recovery_initial_delay_s", 1.0),
            recovery_backoff_multiplier=_optional_number(data.get("recovery_backoff_multiplier"), "recovery_backoff_multiplier", 2.0),
            recovery_max_delay_s=_optional_number(data.get("recovery_max_delay_s"), "recovery_max_delay_s", 30.0),
            recovery_retry_not_before=_optional_string(data.get("recovery_retry_not_before"), "recovery_retry_not_before"),
            last_execution_evidence=_optional_enum(data.get("last_execution_evidence"), ToolExecutionEvidence, "last_execution_evidence"),
            last_error=_optional_error(data.get("last_error")),
            last_tool_error_code=_optional_string(data.get("last_tool_error_code"), "last_tool_error_code"),
            last_tool_error_type=_optional_string(data.get("last_tool_error_type"), "last_tool_error_type"),
            attempt_started_at=_optional_string(data.get("attempt_started_at"), "attempt_started_at"),
            approval_id=_optional_string(data.get("approval_id"), "approval_id"),
            approval_decision=_optional_bool(data.get("approval_decision"), "approval_decision"),
            child_run_id=_optional_string(data.get("child_run_id"), "child_run_id"),
            result=None if data.get("result") is None else ToolOutcomeSnapshot.from_dict(require_json_map(data["result"])),
            uncertainty_reason=_optional_string(data.get("uncertainty_reason"), "uncertainty_reason"),
            updated_at=_required_string(data, "updated_at"),
        )


@dataclass(frozen=True)
class ToolProjectionIntent:
    """等待幂等补写的完整工具事实投影目标与基准快照。"""

    commit_id: str
    ledger_revision: int
    messages: tuple[RunMessage, ...]
    events: tuple[RunEvent, ...]
    checkpoint: RunCheckpoint | None
    run: AgentRun
    base_run: AgentRun
    base_checkpoint: RunCheckpoint | None
    approval: ApprovalRecord | None = None

    def __post_init__(self) -> None:
        if not self.commit_id or not isinstance(self.ledger_revision, int) or isinstance(self.ledger_revision, bool) or self.ledger_revision <= 0:
            raise ValueError("投影意图必须有提交标识和正账本版本")
        if (self.run.run_id, self.run.session_id) != (self.base_run.run_id, self.base_run.session_id):
            raise ValueError("投影前后 AgentRun 身份不一致")
        for checkpoint in (self.checkpoint, self.base_checkpoint):
            if checkpoint is not None and (checkpoint.run_id, checkpoint.session_id) != (self.run.run_id, self.run.session_id):
                raise ValueError("投影检查点与 AgentRun 身份不一致")
        if any(message.message_id == "" for message in self.messages):
            raise ValueError("投影消息 ID 不能为空")
        if any(event.run_id != self.run.run_id for event in self.events):
            raise ValueError("投影事件必须属于目标 AgentRun")
        if self.approval is not None and (self.approval.run_id, self.approval.session_id) != (self.run.run_id, self.run.session_id):
            raise ValueError("投影审批归属与 AgentRun 不一致")

    def to_dict(self) -> JSONMap:
        return {
            "commit_id": self.commit_id,
            "ledger_revision": self.ledger_revision,
            "messages": [message.to_dict() for message in self.messages],
            "events": [event.to_dict() for event in self.events],
            "checkpoint": None if self.checkpoint is None else self.checkpoint.to_dict(),
            "run": self.run.to_dict(),
            "base_run": self.base_run.to_dict(),
            "base_checkpoint": None if self.base_checkpoint is None else self.base_checkpoint.to_dict(),
            "approval": None if self.approval is None else self.approval.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: JSONMap) -> ToolProjectionIntent:
        _reject_unknown(data, {"commit_id", "ledger_revision", "messages", "events", "checkpoint", "run", "base_run", "base_checkpoint", "approval"}, "ToolProjectionIntent")
        raw_messages = _required_list(data, "messages")
        raw_events = _required_list(data, "events")
        return cls(
            commit_id=_required_string(data, "commit_id"),
            ledger_revision=_required_integer(data, "ledger_revision", minimum=1),
            messages=tuple(_run_message_from_dict(require_json_map(item)) for item in raw_messages),
            events=tuple(_run_event_from_dict(require_json_map(item)) for item in raw_events),
            checkpoint=_optional_checkpoint(data.get("checkpoint")),
            run=_agent_run_from_dict(require_json_map(data.get("run"))),
            base_run=_agent_run_from_dict(require_json_map(data.get("base_run"))),
            base_checkpoint=_optional_checkpoint(data.get("base_checkpoint")),
            approval=_optional_approval(data.get("approval")),
        )


@dataclass(frozen=True)
class ToolCallLedger:
    """单个运行所有工具调用的有序权威账本。"""

    session_id: str
    run_id: str
    revision: int
    calls: tuple[ToolCallRecord, ...]
    pending_projection: ToolProjectionIntent | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or isinstance(self.schema_version, bool):
            raise ValueError(f"不支持的工具账本版本：{self.schema_version!r}")
        if not self.session_id or not self.run_id:
            raise ValueError("工具账本 session_id 和 run_id 必须非空")
        if not isinstance(self.revision, int) or isinstance(self.revision, bool) or self.revision <= 0:
            raise ValueError("工具账本 revision 必须是正整数")
        call_ids: set[str] = set()
        ordinals: set[tuple[str, int]] = set()
        for call in self.calls:
            if call.run_id != self.run_id:
                raise ValueError("工具调用 run_id 与账本不一致")
            if call.call.call_id in call_ids:
                raise ValueError("同一运行内 call_id 必须唯一")
            call_ids.add(call.call.call_id)
            ordinal_key = (call.source_message_id, call.ordinal)
            if ordinal_key in ordinals:
                raise ValueError("同一助手消息内工具调用 ordinal 必须唯一")
            ordinals.add(ordinal_key)
        if self.pending_projection is not None:
            intent = self.pending_projection
            if intent.ledger_revision != self.revision:
                raise ValueError("投影意图版本必须等于工具账本 revision")
            if (intent.run.run_id, intent.run.session_id) != (self.run_id, self.session_id):
                raise ValueError("投影意图身份与账本不一致")

    def to_dict(self) -> JSONMap:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "revision": self.revision,
            "calls": [call.to_dict() for call in self.calls],
            "pending_projection": None if self.pending_projection is None else self.pending_projection.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: JSONMap) -> ToolCallLedger:
        _reject_unknown(data, {"schema_version", "session_id", "run_id", "revision", "calls", "pending_projection"}, "ToolCallLedger")
        version = _required_integer(data, "schema_version", minimum=1)
        if version != 1:
            raise ValueError(f"不支持的工具账本版本：{version}")
        raw_calls = _required_list(data, "calls")
        raw_intent = data.get("pending_projection")
        return cls(
            schema_version=version,
            session_id=_required_string(data, "session_id"),
            run_id=_required_string(data, "run_id"),
            revision=_required_integer(data, "revision", minimum=1),
            calls=tuple(ToolCallRecord.from_dict(require_json_map(item)) for item in raw_calls),
            pending_projection=None if raw_intent is None else ToolProjectionIntent.from_dict(require_json_map(raw_intent)),
        )


def _validate_recovery_budget(max_retries: int, initial_delay_s: float, multiplier: float, max_delay_s: float) -> None:
    if not isinstance(max_retries, int) or isinstance(max_retries, bool) or max_retries < 0:
        raise ValueError("recovery_max_retries 必须是非负整数")
    values = (initial_delay_s, multiplier, max_delay_s)
    if any(not isinstance(value, (int, float)) or isinstance(value, bool) or not isfinite(value) for value in values):
        raise ValueError("恢复退避参数必须是有限数字")
    if initial_delay_s < 0 or multiplier < 1 or max_delay_s < initial_delay_s:
        raise ValueError("恢复退避参数范围无效")


def _validate_utc(value: str, name: str) -> None:
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as error:
        raise ValueError(f"{name} 必须是 UTC ISO 时间") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{name} 必须是 UTC ISO 时间")


def _validate_optional_utc(value: str | None, name: str) -> None:
    if value is not None:
        _validate_utc(value, name)


def _reject_unknown(data: Mapping[str, object], allowed: set[str], model: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"{model} 含未知字段：{', '.join(sorted(unknown))}")


def _required_string(data: Mapping[str, object], name: str, *, allow_empty: bool = False) -> str:
    value = data.get(name)
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"{name} 必须是{'' if allow_empty else '非空'}字符串")
    return value


def _optional_string(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} 必须是字符串或 null")
    return value


def _required_integer(data: Mapping[str, object], name: str, minimum: int = 0) -> int:
    value = data.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{name} 必须是大于等于 {minimum} 的整数")
    return value


def _optional_integer(value: object, name: str, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} 必须是整数")
    return value


def _optional_number(value: object, name: str, default: float) -> float:
    if value is None:
        return default
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{name} 必须是数字")
    return float(value)


def _optional_bool(value: object, name: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"{name} 必须是布尔值或 null")
    return value


def _optional_enum(value: object, enum: type[StrEnum], name: str):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} 必须是字符串或 null")
    return enum(value)


def _required_map(value: object, name: str) -> JSONMap:
    if not isinstance(value, dict):
        raise ValueError(f"{name} 必须是对象")
    return value


def _required_list(data: Mapping[str, object], name: str) -> list[object]:
    value = data.get(name)
    if not isinstance(value, list):
        raise ValueError(f"{name} 必须是数组")
    return value


def _optional_error(value: object) -> RunError | None:
    if value is None:
        return None
    data = require_json_map(value)
    retryable = data.get("retryable", False)
    if not isinstance(retryable, bool):
        raise ValueError("RunError.retryable 必须是布尔值")
    return RunError(RunErrorCode(_required_string(data, "code")), _required_string(data, "message", allow_empty=True), retryable)


def _run_message_from_dict(data: JSONMap) -> RunMessage:
    raw_calls = data.get("tool_calls", [])
    if not isinstance(raw_calls, list):
        raise ValueError("RunMessage.tool_calls 必须是数组")
    calls = []
    for raw_call in raw_calls:
        call = require_json_map(raw_call)
        calls.append(ToolCall(_required_string(call, "call_id"), _required_string(call, "name"), _required_map(call.get("arguments"), "ToolCall.arguments")))
    return RunMessage(
        _required_string(data, "id"), _required_integer(data, "sequence", 1),
        RunMessageKind(_required_string(data, "kind")), MessageRole(_required_string(data, "role")),
        _required_string(data, "content", allow_empty=True), _optional_string(data.get("tool_call_id"), "tool_call_id"),
        _optional_string(data.get("name"), "name"), tuple(calls), _required_map(data.get("metadata", {}), "metadata"),
    )


def _run_event_from_dict(data: JSONMap) -> RunEvent:
    raw_sequence = data.get("sequence")
    if not isinstance(raw_sequence, int) or isinstance(raw_sequence, bool) or raw_sequence <= 0:
        raise ValueError("RunEvent.sequence 必须是正整数")
    raw_message_ids = data.get("message_ids", [])
    if not isinstance(raw_message_ids, list) or not all(isinstance(item, str) for item in raw_message_ids):
        raise ValueError("RunEvent.message_ids 必须是字符串数组")
    event_type = RunEventType(_required_string(data, "event_type"))
    raw_event_data = data.get("data")
    event_data = {} if raw_event_data is None else _required_map(raw_event_data, "data")
    if event_type is RunEventType.TOOL_RECOVERY_BLOCKED:
        _required_string(event_data, "call_id")
        _required_string(event_data, "reason")
        _required_integer(event_data, "attempt")
    return RunEvent(
        _required_string(data, "run_id"), raw_sequence, event_type,
        _required_string(data, "occurred_at"), tuple(raw_message_ids),
        _required_string(data, "summary", allow_empty=True), event_data,
    )


def _optional_checkpoint(value: object) -> RunCheckpoint | None:
    if value is None:
        return None
    data = require_json_map(value)
    action = AgentAction(_required_string(data, "action"))
    raw_staged_ids = data.get("staged_history_compression_ids", [])
    if not isinstance(raw_staged_ids, list) or not all(isinstance(item, str) for item in raw_staged_ids):
        raise ValueError("staged_history_compression_ids 必须是字符串数组")
    return RunCheckpoint(
        _required_string(data, "checkpoint_id"), _required_string(data, "run_id"), _required_string(data, "session_id"),
        _required_integer(data, "checkpoint_sequence", 1), _required_integer(data, "event_sequence"),
        _required_integer(data, "message_sequence"), _required_map(data.get("pending"), "pending"),
        _required_map(data.get("budget"), "budget"), action,
        None if data.get("active_context_version") is None else _required_integer(data, "active_context_version", 1),
        tuple(raw_staged_ids),
        None if data.get("tool_ledger_revision") is None else _required_integer(data, "tool_ledger_revision", 1),
        _optional_bool(data.get("replay_context"), "replay_context"),
    )


def _optional_approval(value: object) -> ApprovalRecord | None:
    if value is None:
        return None
    data = require_json_map(value)
    metadata = _required_map(data.get("metadata", {}), "metadata")
    return ApprovalRecord(
        _required_string(data, "approval_id"), _required_string(data, "run_id"), _required_string(data, "session_id"),
        ApprovalStatus(_required_string(data, "status")), _required_string(data, "created_at"), metadata,
        _optional_string(data.get("call_id"), "call_id"), _optional_bool(data.get("decision"), "decision"),
        _optional_string(data.get("resolved_at"), "resolved_at"),
    )


def _agent_run_from_dict(data: JSONMap) -> AgentRun:
    raw_policy = require_json_map(data.get("policy"))
    raw_statistics = require_json_map(data.get("statistics"))
    raw_error = data.get("error")
    raw_candidates = data.get("staged_history_compressions", [])
    if not isinstance(raw_candidates, list):
        raise ValueError("staged_history_compressions 必须是数组")
    candidates = []
    for raw_candidate in raw_candidates:
        candidate = require_json_map(raw_candidate)
        candidates.append(StagedHistoryCompression(
            _required_string(candidate, "candidate_id"), StagedHistoryCompressionStatus(_required_string(candidate, "status")),
            _required_integer(candidate, "session_baseline_version"), _required_string(candidate, "covered_through_conversation_id"),
            _required_string(candidate, "source_hash"), _required_string(candidate, "summary_hash"),
            _required_integer(candidate, "context_version", 1),
        ))
    raw_success = data.get("success_commit_intent")
    success = None
    if raw_success is not None:
        success_data = require_json_map(raw_success)
        success = SuccessCommitIntent(
            _required_string(success_data, "conversation_id"), _optional_string(success_data.get("latest_candidate_id"), "latest_candidate_id"),
            RunOutcome(_required_string(success_data, "target_outcome")), _required_string(success_data, "run_id"),
            _required_string(success_data, "session_id"),
        )
    return AgentRun(
        _required_string(data, "run_id"), _required_string(data, "session_id"), _required_string(data, "agent_id"),
        AgentRunState.from_dict(require_json_map(data.get("state"))), _required_string(data, "started_at"),
        AgentPolicySnapshot(
            _required_string(raw_policy, "agent_id"), _required_string(raw_policy, "identity_version"),
            _required_string(raw_policy, "model_id"), _required_integer(raw_policy, "max_iterations"),
            _required_map(raw_policy.get("policy_data", {}), "policy_data"),
        ),
        _required_string(data, "input_message_id"), _optional_string(data.get("parent_run_id"), "parent_run_id"),
        _optional_string(data.get("root_run_id"), "root_run_id"), _optional_string(data.get("ended_at"), "ended_at"),
        _optional_integer(data.get("resume_count"), "resume_count", 0), _optional_string(data.get("final_message_id"), "final_message_id"),
        None if data.get("latest_checkpoint_id") is None else _optional_string(data.get("latest_checkpoint_id"), "latest_checkpoint_id"),
        None if data.get("active_context_version") is None else _required_integer(data, "active_context_version", 1),
        tuple(candidates), success,
        RunStatistics(*(_optional_integer(raw_statistics.get(name), name, 0) for name in ("duration_ms", "llm_call_count", "tool_call_count", "tokens_in", "tokens_out"))),
        _optional_error(raw_error), _optional_string(data.get("delegation_task_id"), "delegation_task_id"),
    )
