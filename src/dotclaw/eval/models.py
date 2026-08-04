"""版本化 Eval Case 与 Fixture 数据模型。

本模块只定义可序列化事实，不执行、不评分。所有模型复用 Runtime 既有的
``AgentPolicySnapshot`` / ``ConversationMessage`` / ``RunMessage`` /
``ToolDefinition`` / ``ToolCall``，不建立平行的 Agent 或 Context 模型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping, Sequence

from ..runtime.application.dto import ConversationMessage, ToolDefinition, ToolResultStatus
from ..runtime.domain.facts import (
    AgentPolicySnapshot,
    JSONMap,
    JSONValue,
    MessageRole,
    RunMessage,
    RunMessageKind,
    ToolCall,
)
from ..runtime.domain.state import RunOutcome

SCHEMA_VERSION: str = "1.0"
"""当前支持的唯一 Case schema 版本；读取到其他版本必须明确失败。"""

EVAL_SCHEMA_VERSION: str = SCHEMA_VERSION
"""评测结果使用的 schema 版本，与 Case schema 同源。"""


class EvalCaseValidationError(ValueError):
    """Case 或 Fixture 的结构、版本或取值不合法。"""


class ExecutionMode(StrEnum):
    """Case 声明的执行方式。"""

    PLAYBACK = "playback"
    """完全冻结策略与上下文，重放既有事实。"""

    REEXECUTION = "reexecution"
    """允许接入当前 Agent / Prompt / LLM，外部能力仍然隔离。"""


class FixtureMatchMode(StrEnum):
    """Fixture 消费时的匹配严格程度。"""

    STRICT = "strict"
    """按记录顺序精确消费，参数必须完全一致；只供 Playback / Gate。"""

    NORMAL = "normal"
    """按名称与关键参数匹配，允许未声明参数变化；只供 Re-execution。"""


def _ensure_json_value(value: object, label: str) -> None:
    """递归校验取值可被 JSON 表达，否则明确失败。"""
    if value is None or isinstance(value, (str, bool, int, float)):
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _ensure_json_value(item, f"{label}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise EvalCaseValidationError(f"{label} 的键必须是字符串，实际为 {type(key).__name__}")
            _ensure_json_value(item, f"{label}.{key}")
        return
    raise EvalCaseValidationError(f"{label} 不是 JSON 兼容取值：{type(value).__name__}")


def _ensure_json_map(value: object, label: str) -> JSONMap:
    """校验取值为 JSON 兼容对象并返回其精确类型。"""
    if not isinstance(value, dict):
        raise EvalCaseValidationError(f"{label} 必须是对象，实际为 {type(value).__name__}")
    _ensure_json_value(value, label)
    return dict(value)


def _require_map(value: object, label: str) -> Mapping[str, JSONValue]:
    """读取阶段要求节点为对象。"""
    if not isinstance(value, Mapping):
        raise EvalCaseValidationError(f"{label} 必须是对象")
    return value


def _require_str(data: Mapping[str, JSONValue], key: str, label: str, *, allow_empty: bool = True) -> str:
    """读取必填字符串字段。"""
    value: JSONValue | None = data.get(key)
    if not isinstance(value, str):
        raise EvalCaseValidationError(f"{label}.{key} 必须是字符串")
    if not allow_empty and not value:
        raise EvalCaseValidationError(f"{label}.{key} 不能为空")
    return value


def _optional_str(data: Mapping[str, JSONValue], key: str, label: str) -> str | None:
    """读取可空字符串字段。"""
    value: JSONValue | None = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise EvalCaseValidationError(f"{label}.{key} 必须是字符串或 null")
    return value


def _require_int(data: Mapping[str, JSONValue], key: str, label: str, default: int | None = None) -> int:
    """读取必填整数字段；布尔不被视为整数。"""
    value: JSONValue | None = data.get(key)
    if value is None and default is not None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvalCaseValidationError(f"{label}.{key} 必须是整数")
    return value


def _require_bool(data: Mapping[str, JSONValue], key: str, label: str) -> bool:
    """读取必填布尔字段。"""
    value: JSONValue | None = data.get(key)
    if not isinstance(value, bool):
        raise EvalCaseValidationError(f"{label}.{key} 必须是布尔值")
    return value


def _require_list(data: Mapping[str, JSONValue], key: str, label: str) -> Sequence[JSONValue]:
    """读取可缺省的数组字段。"""
    value: JSONValue | None = data.get(key)
    if value is None:
        return ()
    if not isinstance(value, list):
        raise EvalCaseValidationError(f"{label}.{key} 必须是数组")
    return value


def _enum_of[EnumT: StrEnum](enum_type: type[EnumT], raw: str, label: str) -> EnumT:
    """按取值解析枚举，未知取值明确失败。"""
    try:
        return enum_type(raw)
    except ValueError as error:
        raise EvalCaseValidationError(f"{label} 取值 {raw!r} 不受支持") from error


def _tool_call_to_dict(call: ToolCall) -> JSONMap:
    """序列化工具调用。"""
    return call.to_dict()


def _tool_call_from_dict(data: Mapping[str, JSONValue], label: str) -> ToolCall:
    """反序列化工具调用。"""
    return ToolCall(
        call_id=_require_str(data, "call_id", label, allow_empty=False),
        name=_require_str(data, "name", label, allow_empty=False),
        arguments=_ensure_json_map(data.get("arguments") or {}, f"{label}.arguments"),
    )


def _conversation_message_from_dict(data: Mapping[str, JSONValue], label: str) -> ConversationMessage:
    """反序列化会话消息。"""
    return ConversationMessage(
        message_id=_require_str(data, "id", label, allow_empty=False),
        role=_enum_of(MessageRole, _require_str(data, "role", label), f"{label}.role"),
        content=_require_str(data, "content", label),
        created_at=_require_str(data, "created_at", label),
    )


def _run_message_from_dict(data: Mapping[str, JSONValue], label: str) -> RunMessage:
    """反序列化运行消息。"""
    return RunMessage(
        message_id=_require_str(data, "id", label, allow_empty=False),
        sequence=_require_int(data, "sequence", label),
        kind=_enum_of(RunMessageKind, _require_str(data, "kind", label), f"{label}.kind"),
        role=_enum_of(MessageRole, _require_str(data, "role", label), f"{label}.role"),
        content=_require_str(data, "content", label),
        tool_call_id=_optional_str(data, "tool_call_id", label),
        name=_optional_str(data, "name", label),
        tool_calls=tuple(
            _tool_call_from_dict(_require_map(item, f"{label}.tool_calls[{index}]"), f"{label}.tool_calls[{index}]")
            for index, item in enumerate(_require_list(data, "tool_calls", label))
        ),
        metadata=_ensure_json_map(data.get("metadata") or {}, f"{label}.metadata"),
    )


def _tool_definition_from_dict(data: Mapping[str, JSONValue], label: str) -> ToolDefinition:
    """反序列化工具定义。"""
    return ToolDefinition(
        name=_require_str(data, "name", label, allow_empty=False),
        description=_require_str(data, "description", label),
        parameters=_ensure_json_map(data.get("parameters") or {}, f"{label}.parameters"),
    )


def _policy_from_dict(data: Mapping[str, JSONValue], label: str) -> AgentPolicySnapshot:
    """反序列化 Agent 策略快照，复用 Runtime 既有模型。"""
    return AgentPolicySnapshot(
        agent_id=_require_str(data, "agent_id", label, allow_empty=False),
        identity_version=_require_str(data, "identity_version", label),
        model_id=_require_str(data, "model_id", label),
        max_iterations=_require_int(data, "max_iterations", label),
        policy_data=_ensure_json_map(data.get("policy_data") or {}, f"{label}.policy_data"),
    )


@dataclass(frozen=True)
class Expectation:
    """Case 的通用断言载体；PR3 只保证其 JSON 兼容性，不解释 kind。"""

    kind: str
    target: str
    expected: JSONValue = None
    options: JSONMap = field(default_factory=dict)

    def __post_init__(self) -> None:
        """校验标识非空且断言载荷可 JSON 表达。"""
        if not self.kind:
            raise EvalCaseValidationError("expectation.kind 不能为空")
        if not self.target:
            raise EvalCaseValidationError("expectation.target 不能为空")
        _ensure_json_value(self.expected, "expectation.expected")
        _ensure_json_map(self.options, "expectation.options")

    def to_dict(self) -> JSONMap:
        """序列化为 JSON 兼容字典。"""
        return {
            "kind": self.kind,
            "target": self.target,
            "expected": self.expected,
            "options": dict(self.options),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, JSONValue]) -> Expectation:
        """从 JSON 字典严格反序列化。"""
        label: str = "expectation"
        return cls(
            kind=_require_str(data, "kind", label, allow_empty=False),
            target=_require_str(data, "target", label, allow_empty=False),
            expected=data.get("expected"),
            options=_ensure_json_map(data.get("options") or {}, f"{label}.options"),
        )


@dataclass(frozen=True)
class ConversationFixture:
    """Case 冻结的会话事实，不引用任何生产 Session。"""

    session_id: str
    messages: tuple[ConversationMessage, ...] = ()
    version: int = 0

    def __post_init__(self) -> None:
        """校验会话标识非空。"""
        if not self.session_id:
            raise EvalCaseValidationError("conversation_fixture.session_id 不能为空")

    def to_dict(self) -> JSONMap:
        """序列化为 JSON 兼容字典。"""
        return {
            "session_id": self.session_id,
            "messages": [message.to_dict() for message in self.messages],
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, JSONValue]) -> ConversationFixture:
        """从 JSON 字典严格反序列化。"""
        label: str = "conversation_fixture"
        return cls(
            session_id=_require_str(data, "session_id", label, allow_empty=False),
            messages=tuple(
                _conversation_message_from_dict(
                    _require_map(item, f"{label}.messages[{index}]"), f"{label}.messages[{index}]"
                )
                for index, item in enumerate(_require_list(data, "messages", label))
            ),
            version=_require_int(data, "version", label, default=0),
        )


@dataclass(frozen=True)
class ContextFixture:
    """一次上下文构建的冻结结果，直接复用 Runtime Context DTO 的事实。"""

    fixture_id: str
    messages: tuple[RunMessage, ...] = ()
    tools: tuple[ToolDefinition, ...] = ()
    estimated_tokens: int = 1

    def __post_init__(self) -> None:
        """校验 Fixture 标识非空。"""
        if not self.fixture_id:
            raise EvalCaseValidationError("context_fixture.fixture_id 不能为空")

    def to_dict(self) -> JSONMap:
        """序列化为 JSON 兼容字典。"""
        return {
            "fixture_id": self.fixture_id,
            "messages": [message.to_dict() for message in self.messages],
            "tools": [tool.to_dict() for tool in self.tools],
            "estimated_tokens": self.estimated_tokens,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, JSONValue]) -> ContextFixture:
        """从 JSON 字典严格反序列化。"""
        label: str = "context_fixture"
        return cls(
            fixture_id=_require_str(data, "fixture_id", label, allow_empty=False),
            messages=tuple(
                _run_message_from_dict(
                    _require_map(item, f"{label}.messages[{index}]"), f"{label}.messages[{index}]"
                )
                for index, item in enumerate(_require_list(data, "messages", label))
            ),
            tools=tuple(
                _tool_definition_from_dict(
                    _require_map(item, f"{label}.tools[{index}]"), f"{label}.tools[{index}]"
                )
                for index, item in enumerate(_require_list(data, "tools", label))
            ),
            estimated_tokens=_require_int(data, "estimated_tokens", label, default=1),
        )


@dataclass(frozen=True)
class LLMResponseFixture:
    """脚本化 LLM 的一次完整响应。"""

    message_id: str
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()

    def __post_init__(self) -> None:
        """校验消息标识非空。"""
        if not self.message_id:
            raise EvalCaseValidationError("llm_response.message_id 不能为空")

    def to_dict(self) -> JSONMap:
        """序列化为 JSON 兼容字典。"""
        return {
            "message_id": self.message_id,
            "content": self.content,
            "tool_calls": [_tool_call_to_dict(call) for call in self.tool_calls],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, JSONValue]) -> LLMResponseFixture:
        """从 JSON 字典严格反序列化。"""
        label: str = "llm_response"
        return cls(
            message_id=_require_str(data, "message_id", label, allow_empty=False),
            content=_require_str(data, "content", label),
            tool_calls=tuple(
                _tool_call_from_dict(
                    _require_map(item, f"{label}.tool_calls[{index}]"), f"{label}.tool_calls[{index}]"
                )
                for index, item in enumerate(_require_list(data, "tool_calls", label))
            ),
        )


@dataclass(frozen=True)
class LLMFixture:
    """按顺序回放的模型响应脚本。"""

    fixture_id: str
    responses: tuple[LLMResponseFixture, ...] = ()

    def __post_init__(self) -> None:
        """校验 Fixture 标识与响应消息标识唯一。"""
        if not self.fixture_id:
            raise EvalCaseValidationError("llm_fixture.fixture_id 不能为空")
        message_ids: list[str] = [response.message_id for response in self.responses]
        duplicated: set[str] = {item for item in message_ids if message_ids.count(item) > 1}
        if duplicated:
            raise EvalCaseValidationError(f"llm_fixture 存在重复响应消息标识：{sorted(duplicated)}")

    def to_dict(self) -> JSONMap:
        """序列化为 JSON 兼容字典。"""
        return {
            "fixture_id": self.fixture_id,
            "responses": [response.to_dict() for response in self.responses],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, JSONValue]) -> LLMFixture:
        """从 JSON 字典严格反序列化。"""
        label: str = "llm_fixture"
        return cls(
            fixture_id=_require_str(data, "fixture_id", label, allow_empty=False),
            responses=tuple(
                LLMResponseFixture.from_dict(_require_map(item, f"{label}.responses[{index}]"))
                for index, item in enumerate(_require_list(data, "responses", label))
            ),
        )


@dataclass(frozen=True)
class ToolFixture:
    """一次工具调用的冻结结果与匹配条件。"""

    fixture_id: str
    tool_name: str
    key_arguments: JSONMap = field(default_factory=dict)
    status: ToolResultStatus = ToolResultStatus.COMPLETED
    output: str = ""
    approval_id: str | None = None
    error_message: str = ""

    def __post_init__(self) -> None:
        """校验标识、名称与关键参数取值。"""
        if not self.fixture_id:
            raise EvalCaseValidationError("tool_fixture.fixture_id 不能为空")
        if not self.tool_name:
            raise EvalCaseValidationError("tool_fixture.tool_name 不能为空")
        _ensure_json_map(self.key_arguments, "tool_fixture.key_arguments")
        if self.status is ToolResultStatus.APPROVAL_REQUIRED and not self.approval_id:
            raise EvalCaseValidationError("tool_fixture 声明需要审批时必须提供 approval_id")

    def to_dict(self) -> JSONMap:
        """序列化为 JSON 兼容字典。"""
        return {
            "fixture_id": self.fixture_id,
            "tool_name": self.tool_name,
            "key_arguments": dict(self.key_arguments),
            "status": self.status.value,
            "output": self.output,
            "approval_id": self.approval_id,
            "error_message": self.error_message,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, JSONValue]) -> ToolFixture:
        """从 JSON 字典严格反序列化。"""
        label: str = "tool_fixture"
        return cls(
            fixture_id=_require_str(data, "fixture_id", label, allow_empty=False),
            tool_name=_require_str(data, "tool_name", label, allow_empty=False),
            key_arguments=_ensure_json_map(data.get("key_arguments") or {}, f"{label}.key_arguments"),
            status=_enum_of(ToolResultStatus, _require_str(data, "status", label), f"{label}.status"),
            output=_require_str(data, "output", label),
            approval_id=_optional_str(data, "approval_id", label),
            error_message=_require_str(data, "error_message", label),
        )


@dataclass(frozen=True)
class ApprovalFixture:
    """一次审批决议的冻结结果，只驱动隔离 Run 的审批记录。"""

    fixture_id: str
    approved: bool
    approval_id: str | None = None

    def __post_init__(self) -> None:
        """校验 Fixture 标识非空。"""
        if not self.fixture_id:
            raise EvalCaseValidationError("approval_fixture.fixture_id 不能为空")

    def to_dict(self) -> JSONMap:
        """序列化为 JSON 兼容字典。"""
        return {
            "fixture_id": self.fixture_id,
            "approved": self.approved,
            "approval_id": self.approval_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, JSONValue]) -> ApprovalFixture:
        """从 JSON 字典严格反序列化。"""
        label: str = "approval_fixture"
        return cls(
            fixture_id=_require_str(data, "fixture_id", label, allow_empty=False),
            approved=_require_bool(data, "approved", label),
            approval_id=_optional_str(data, "approval_id", label),
        )


@dataclass(frozen=True)
class DelegationFixture:
    """一次子执行的冻结受理与结果，不创建真实子 Session / 子 Run。"""

    fixture_id: str
    target_agent_id: str
    child_run_id: str
    task_id: str = ""
    target_session_id: str = ""
    outcome: RunOutcome | None = None
    output: str = ""
    error_message: str = ""

    def __post_init__(self) -> None:
        """校验标识与目标 Agent 非空。"""
        if not self.fixture_id:
            raise EvalCaseValidationError("delegation_fixture.fixture_id 不能为空")
        if not self.target_agent_id:
            raise EvalCaseValidationError("delegation_fixture.target_agent_id 不能为空")
        if not self.child_run_id:
            raise EvalCaseValidationError("delegation_fixture.child_run_id 不能为空")

    def to_dict(self) -> JSONMap:
        """序列化为 JSON 兼容字典。"""
        return {
            "fixture_id": self.fixture_id,
            "target_agent_id": self.target_agent_id,
            "child_run_id": self.child_run_id,
            "task_id": self.task_id,
            "target_session_id": self.target_session_id,
            "outcome": None if self.outcome is None else self.outcome.value,
            "output": self.output,
            "error_message": self.error_message,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, JSONValue]) -> DelegationFixture:
        """从 JSON 字典严格反序列化。"""
        label: str = "delegation_fixture"
        raw_outcome: str | None = _optional_str(data, "outcome", label)
        return cls(
            fixture_id=_require_str(data, "fixture_id", label, allow_empty=False),
            target_agent_id=_require_str(data, "target_agent_id", label, allow_empty=False),
            child_run_id=_require_str(data, "child_run_id", label, allow_empty=False),
            task_id=_require_str(data, "task_id", label),
            target_session_id=_require_str(data, "target_session_id", label),
            outcome=None if raw_outcome is None else _enum_of(RunOutcome, raw_outcome, f"{label}.outcome"),
            output=_require_str(data, "output", label),
            error_message=_require_str(data, "error_message", label),
        )


@dataclass(frozen=True, kw_only=True)
class EvalCase:
    """版本化的评测用例；字段顺序与开发计划一致，全部关键字传参。"""

    case_id: str
    schema_version: str = SCHEMA_VERSION
    name: str = ""
    agent_id: str
    input: ConversationMessage
    conversation_fixture: ConversationFixture
    policy_fixture: AgentPolicySnapshot
    context_fixtures: tuple[ContextFixture, ...] = ()
    llm_fixture: LLMFixture
    tool_fixtures: tuple[ToolFixture, ...] = ()
    approval_fixtures: tuple[ApprovalFixture, ...] = ()
    delegation_fixtures: tuple[DelegationFixture, ...] = ()
    expectations: tuple[Expectation, ...] = ()
    tags: tuple[str, ...] = ()
    source_trace: str | None = None
    execution_mode: ExecutionMode = ExecutionMode.PLAYBACK
    allow_partial_trace: bool = False
    """显式声明允许对部分 Trace（运行未完整结束）评分；未声明时部分 Trace 视为重建失败。"""

    def __post_init__(self) -> None:
        """校验 schema 版本、必填标识与全局 Fixture 标识唯一。"""
        if self.schema_version != SCHEMA_VERSION:
            raise EvalCaseValidationError(
                f"不支持的 case schema 版本 {self.schema_version!r}，当前仅支持 {SCHEMA_VERSION!r}"
            )
        if not self.case_id:
            raise EvalCaseValidationError("case_id 不能为空")
        if not self.agent_id:
            raise EvalCaseValidationError("agent_id 不能为空")
        fixture_ids: list[str] = [self.llm_fixture.fixture_id]
        fixture_ids.extend(item.fixture_id for item in self.context_fixtures)
        fixture_ids.extend(item.fixture_id for item in self.tool_fixtures)
        fixture_ids.extend(item.fixture_id for item in self.approval_fixtures)
        fixture_ids.extend(item.fixture_id for item in self.delegation_fixtures)
        duplicated: set[str] = {item for item in fixture_ids if fixture_ids.count(item) > 1}
        if duplicated:
            raise EvalCaseValidationError(f"存在重复 fixture 标识：{sorted(duplicated)}")

    def to_dict(self) -> JSONMap:
        """序列化为 JSON 兼容字典。"""
        return {
            "case_id": self.case_id,
            "schema_version": self.schema_version,
            "name": self.name,
            "agent_id": self.agent_id,
            "input": self.input.to_dict(),
            "conversation_fixture": self.conversation_fixture.to_dict(),
            "policy_fixture": self.policy_fixture.to_dict(),
            "context_fixtures": [item.to_dict() for item in self.context_fixtures],
            "llm_fixture": self.llm_fixture.to_dict(),
            "tool_fixtures": [item.to_dict() for item in self.tool_fixtures],
            "approval_fixtures": [item.to_dict() for item in self.approval_fixtures],
            "delegation_fixtures": [item.to_dict() for item in self.delegation_fixtures],
            "expectations": [item.to_dict() for item in self.expectations],
            "tags": list(self.tags),
            "source_trace": self.source_trace,
            "execution_mode": self.execution_mode.value,
            "allow_partial_trace": self.allow_partial_trace,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, JSONValue]) -> EvalCase:
        """从 JSON 字典严格反序列化；未知版本或非法取值立即失败。"""
        label: str = "case"
        schema_version: str = _require_str(data, "schema_version", label, allow_empty=False)
        if schema_version != SCHEMA_VERSION:
            raise EvalCaseValidationError(
                f"不支持的 case schema 版本 {schema_version!r}，当前仅支持 {SCHEMA_VERSION!r}"
            )
        tags_raw: Sequence[JSONValue] = _require_list(data, "tags", label)
        for index, tag in enumerate(tags_raw):
            if not isinstance(tag, str):
                raise EvalCaseValidationError(f"{label}.tags[{index}] 必须是字符串")
        return cls(
            case_id=_require_str(data, "case_id", label, allow_empty=False),
            schema_version=schema_version,
            name=_require_str(data, "name", label),
            agent_id=_require_str(data, "agent_id", label, allow_empty=False),
            input=_conversation_message_from_dict(
                _require_map(data.get("input"), f"{label}.input"), f"{label}.input"
            ),
            conversation_fixture=ConversationFixture.from_dict(
                _require_map(data.get("conversation_fixture"), f"{label}.conversation_fixture")
            ),
            policy_fixture=_policy_from_dict(
                _require_map(data.get("policy_fixture"), f"{label}.policy_fixture"), f"{label}.policy_fixture"
            ),
            context_fixtures=tuple(
                ContextFixture.from_dict(_require_map(item, f"{label}.context_fixtures[{index}]"))
                for index, item in enumerate(_require_list(data, "context_fixtures", label))
            ),
            llm_fixture=LLMFixture.from_dict(
                _require_map(data.get("llm_fixture"), f"{label}.llm_fixture")
            ),
            tool_fixtures=tuple(
                ToolFixture.from_dict(_require_map(item, f"{label}.tool_fixtures[{index}]"))
                for index, item in enumerate(_require_list(data, "tool_fixtures", label))
            ),
            approval_fixtures=tuple(
                ApprovalFixture.from_dict(_require_map(item, f"{label}.approval_fixtures[{index}]"))
                for index, item in enumerate(_require_list(data, "approval_fixtures", label))
            ),
            delegation_fixtures=tuple(
                DelegationFixture.from_dict(_require_map(item, f"{label}.delegation_fixtures[{index}]"))
                for index, item in enumerate(_require_list(data, "delegation_fixtures", label))
            ),
            expectations=tuple(
                Expectation.from_dict(_require_map(item, f"{label}.expectations[{index}]"))
                for index, item in enumerate(_require_list(data, "expectations", label))
            ),
            tags=tuple(str(tag) for tag in tags_raw),
            source_trace=_optional_str(data, "source_trace", label),
            execution_mode=_enum_of(
                ExecutionMode, _require_str(data, "execution_mode", label), f"{label}.execution_mode"
            ),
            allow_partial_trace=bool(data.get("allow_partial_trace", False)),
        )
