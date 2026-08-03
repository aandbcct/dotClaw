"""默认拒绝真实依赖的 Fixture Port 实现。

所有 Port 都只消费 Case 声明的 Fixture：未匹配的调用一律抛出
``FixtureConfigurationError``，绝不回退到真实 LLM、真实工具或生产仓储。
"""

from __future__ import annotations

from dataclasses import replace

from ..runtime.application.dto import (
    ContextBundle,
    ContextMetadata,
    ContextRefreshSignal,
    DelegationRequest,
    DelegationResult,
    DelegationSubmission,
    RunRequest,
    ToolInvocation,
    ToolResult,
    ToolResultStatus,
)
from ..runtime.application.execution import RunExecutionView
from ..runtime.application.ports import LLMOutputPort
from ..runtime.domain.context import ContextOwner
from ..runtime.domain.facts import (
    AgentPolicySnapshot,
    ApprovalRecord,
    ApprovalStatus,
    JSONMap,
    JSONValue,
    MessageRole,
    RunError,
    RunErrorCode,
    RunMessage,
    RunMessageKind,
)
from .models import (
    ApprovalFixture,
    ContextFixture,
    DelegationFixture,
    FixtureMatchMode,
    LLMFixture,
    LLMResponseFixture,
    ToolFixture,
)


class FixtureConfigurationError(RuntimeError):
    """Fixture Configuration Failure：调用与 Case 声明的 Fixture 不一致。"""


def _describe_arguments(arguments: JSONMap) -> str:
    """生成稳定的参数描述，便于定位不一致。"""
    return "{" + ", ".join(f"{key}={arguments[key]!r}" for key in sorted(arguments)) + "}"


def _matches_key_arguments(key_arguments: JSONMap, actual: JSONMap) -> bool:
    """NORMAL 匹配：Case 声明的关键参数必须逐项一致，其余允许变化。"""
    for key, expected in key_arguments.items():
        if key not in actual:
            return False
        actual_value: JSONValue = actual[key]
        if actual_value != expected:
            return False
    return True


class ScriptedLLMPort:
    """按记录顺序回放模型响应的隔离 LLM Port。"""

    def __init__(self, fixture: LLMFixture, mode: FixtureMatchMode) -> None:
        """绑定响应脚本与匹配模式，并初始化独立消费游标。"""
        self._fixture: LLMFixture = fixture
        self._mode: FixtureMatchMode = mode
        self._cursor: int = 0
        self.cancelled_runs: list[str] = []

    @property
    def consumed(self) -> int:
        """已消费的响应数量。"""
        return self._cursor

    @property
    def remaining(self) -> tuple[LLMResponseFixture, ...]:
        """尚未消费的响应。"""
        return self._fixture.responses[self._cursor :]

    async def complete(
        self,
        context: ContextBundle,
        execution: RunExecutionView,
        output_port: LLMOutputPort | None = None,
    ) -> RunMessage:
        """返回脚本中的下一条响应；超出脚本即判定为额外调用。"""
        if self._cursor >= len(self._fixture.responses):
            raise FixtureConfigurationError(
                f"第 {self._cursor + 1} 次 LLM 调用没有对应 fixture（脚本共 {len(self._fixture.responses)} 条）"
            )
        response: LLMResponseFixture = self._fixture.responses[self._cursor]
        self._cursor += 1
        return RunMessage(
            message_id=response.message_id,
            sequence=self._cursor,
            kind=RunMessageKind.LLM_RESPONSE,
            role=MessageRole.ASSISTANT,
            content=response.content,
            tool_calls=response.tool_calls,
        )

    async def cancel(self, run_id: str) -> None:
        """隔离环境无远程调用，仅记录取消请求。"""
        self.cancelled_runs.append(run_id)

    def verify_fully_consumed(self) -> None:
        """校验脚本被完整消费；剩余响应视为缺失调用。"""
        if self.remaining:
            missing: list[str] = [item.message_id for item in self.remaining]
            raise FixtureConfigurationError(f"存在未被调用的 LLM fixture：{missing}")


class FixtureToolPort:
    """只返回 Case 声明结果的隔离工具 Port。"""

    def __init__(self, fixtures: tuple[ToolFixture, ...], mode: FixtureMatchMode) -> None:
        """绑定工具 Fixture 与匹配模式，并初始化独立消费状态。"""
        self._fixtures: tuple[ToolFixture, ...] = fixtures
        self._mode: FixtureMatchMode = mode
        self._cursor: int = 0
        self._consumed: set[str] = set()
        self.cancelled_runs: list[str] = []

    @property
    def remaining(self) -> tuple[ToolFixture, ...]:
        """尚未消费的工具 Fixture。"""
        if self._mode is FixtureMatchMode.STRICT:
            return self._fixtures[self._cursor :]
        return tuple(item for item in self._fixtures if item.fixture_id not in self._consumed)

    async def execute(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        """按模式匹配 Fixture 并返回冻结结果；无法匹配即失败。"""
        fixture: ToolFixture = (
            self._match_strict(invocation) if self._mode is FixtureMatchMode.STRICT else self._match_normal(invocation)
        )
        return self._to_result(invocation, fixture)

    def _match_strict(self, invocation: ToolInvocation) -> ToolFixture:
        """STRICT：按记录顺序精确消费，名称与全部参数都必须一致。"""
        if self._cursor >= len(self._fixtures):
            raise FixtureConfigurationError(
                f"工具调用 {invocation.call.name} 超出 fixture 记录（共 {len(self._fixtures)} 条）"
            )
        fixture: ToolFixture = self._fixtures[self._cursor]
        if fixture.tool_name != invocation.call.name:
            raise FixtureConfigurationError(
                f"工具调用顺序不符：期望 {fixture.tool_name}，实际 {invocation.call.name}"
            )
        if dict(fixture.key_arguments) != dict(invocation.call.arguments):
            raise FixtureConfigurationError(
                f"工具 {fixture.tool_name} 参数不一致："
                f"期望 {_describe_arguments(dict(fixture.key_arguments))}，"
                f"实际 {_describe_arguments(dict(invocation.call.arguments))}"
            )
        self._cursor += 1
        self._consumed.add(fixture.fixture_id)
        return fixture

    def _match_normal(self, invocation: ToolInvocation) -> ToolFixture:
        """NORMAL：按名称与关键参数匹配，允许未声明参数变化。"""
        named: tuple[ToolFixture, ...] = tuple(
            item
            for item in self._fixtures
            if item.tool_name == invocation.call.name and item.fixture_id not in self._consumed
        )
        if not named:
            raise FixtureConfigurationError(f"工具 {invocation.call.name} 没有可用 fixture")
        for fixture in named:
            if _matches_key_arguments(dict(fixture.key_arguments), dict(invocation.call.arguments)):
                self._consumed.add(fixture.fixture_id)
                return fixture
        raise FixtureConfigurationError(
            f"工具 {invocation.call.name} 的关键参数不匹配："
            f"实际 {_describe_arguments(dict(invocation.call.arguments))}"
        )

    def _to_result(self, invocation: ToolInvocation, fixture: ToolFixture) -> ToolResult:
        """把 Fixture 转换为 Runtime 标准工具结果。"""
        if fixture.status is ToolResultStatus.APPROVAL_REQUIRED:
            return ToolResult(invocation.call.call_id, ToolResultStatus.APPROVAL_REQUIRED, approval_id=fixture.approval_id)
        if fixture.status is ToolResultStatus.FAILED:
            return ToolResult(
                invocation.call.call_id,
                ToolResultStatus.FAILED,
                error=RunError(RunErrorCode.TOOL_FAILURE, fixture.error_message or "fixture 声明工具失败"),
            )
        return ToolResult(invocation.call.call_id, ToolResultStatus.COMPLETED, output=fixture.output)

    async def cancel(self, run_id: str) -> None:
        """隔离环境无真实工具进程，仅记录取消请求。"""
        self.cancelled_runs.append(run_id)

    def verify_fully_consumed(self) -> None:
        """校验所有工具 Fixture 均被调用；剩余项视为缺失调用。"""
        if self.remaining:
            missing: list[str] = [item.fixture_id for item in self.remaining]
            raise FixtureConfigurationError(f"存在未被调用的工具 fixture：{missing}")


class FixtureApprovalRepository:
    """隔离的审批记录容器，并按 Fixture 提供审批决议。"""

    def __init__(self, fixtures: tuple[ApprovalFixture, ...], mode: FixtureMatchMode) -> None:
        """绑定审批 Fixture 与匹配模式，并初始化独立记录表。"""
        self._fixtures: tuple[ApprovalFixture, ...] = fixtures
        self._mode: FixtureMatchMode = mode
        self._cursor: int = 0
        self._consumed: set[str] = set()
        self._records: dict[str, ApprovalRecord] = {}

    @property
    def remaining(self) -> tuple[ApprovalFixture, ...]:
        """尚未消费的审批 Fixture。"""
        if self._mode is FixtureMatchMode.STRICT:
            return self._fixtures[self._cursor :]
        return tuple(item for item in self._fixtures if item.fixture_id not in self._consumed)

    async def create(self, record: ApprovalRecord) -> None:
        """创建隔离审批记录，重复标识明确失败。"""
        if record.approval_id in self._records:
            raise FixtureConfigurationError(f"审批 {record.approval_id} 已存在")
        self._records[record.approval_id] = record

    async def load(self, approval_id: str) -> ApprovalRecord | None:
        """按标识读取审批记录。"""
        return self._records.get(approval_id)

    async def consume(self, approval_id: str) -> ApprovalRecord | None:
        """原子消费仍处于待处理状态的审批记录。"""
        record: ApprovalRecord | None = self._records.get(approval_id)
        if record is None or record.status is not ApprovalStatus.PENDING:
            return None
        consumed: ApprovalRecord = replace(record, status=ApprovalStatus.CONSUMED)
        self._records[approval_id] = consumed
        return consumed

    def next_decision(self, approval_id: str) -> bool:
        """取出下一条审批决议；顺序或标识不符即判定为配置失败。"""
        if self._mode is FixtureMatchMode.STRICT:
            if self._cursor >= len(self._fixtures):
                raise FixtureConfigurationError(f"审批 {approval_id} 没有对应 fixture")
            fixture: ApprovalFixture = self._fixtures[self._cursor]
            if fixture.approval_id is not None and fixture.approval_id != approval_id:
                raise FixtureConfigurationError(
                    f"审批顺序不符：期望 {fixture.approval_id}，实际 {approval_id}"
                )
            self._cursor += 1
            self._consumed.add(fixture.fixture_id)
            return fixture.approved
        for candidate in self._fixtures:
            if candidate.fixture_id in self._consumed:
                continue
            if candidate.approval_id is not None and candidate.approval_id != approval_id:
                continue
            self._consumed.add(candidate.fixture_id)
            return candidate.approved
        raise FixtureConfigurationError(f"审批 {approval_id} 没有可用 fixture")

    def verify_fully_consumed(self) -> None:
        """校验所有审批 Fixture 均被使用；剩余项视为缺失调用。"""
        if self.remaining:
            missing: list[str] = [item.fixture_id for item in self.remaining]
            raise FixtureConfigurationError(f"存在未被使用的审批 fixture：{missing}")


class FixtureDelegationPort:
    """只受理 Case 声明子执行的隔离委派 Port，不创建真实子 Session / 子 Run。"""

    def __init__(self, fixtures: tuple[DelegationFixture, ...], mode: FixtureMatchMode) -> None:
        """绑定委派 Fixture 与匹配模式，并初始化独立受理表。"""
        self._fixtures: tuple[DelegationFixture, ...] = fixtures
        self._mode: FixtureMatchMode = mode
        self._cursor: int = 0
        self._consumed: set[str] = set()
        self._submitted: dict[str, DelegationFixture] = {}
        self.cancelled_children: list[str] = []

    @property
    def remaining(self) -> tuple[DelegationFixture, ...]:
        """尚未受理的委派 Fixture。"""
        if self._mode is FixtureMatchMode.STRICT:
            return self._fixtures[self._cursor :]
        return tuple(item for item in self._fixtures if item.fixture_id not in self._consumed)

    async def submit(self, request: DelegationRequest) -> DelegationSubmission:
        """按模式匹配委派 Fixture 并返回冻结受理信息。"""
        fixture: DelegationFixture = (
            self._match_strict(request) if self._mode is FixtureMatchMode.STRICT else self._match_normal(request)
        )
        self._submitted[fixture.child_run_id] = fixture
        return DelegationSubmission(
            child_run_id=fixture.child_run_id,
            task_id=fixture.task_id or f"task-{fixture.child_run_id}",
            target_session_id=fixture.target_session_id or f"session-{fixture.target_agent_id}",
        )

    def _match_strict(self, request: DelegationRequest) -> DelegationFixture:
        """STRICT：按记录顺序精确消费，目标 Agent 必须一致。"""
        if self._cursor >= len(self._fixtures):
            raise FixtureConfigurationError(
                f"委派 {request.target_agent_id} 超出 fixture 记录（共 {len(self._fixtures)} 条）"
            )
        fixture: DelegationFixture = self._fixtures[self._cursor]
        if fixture.target_agent_id != request.target_agent_id:
            raise FixtureConfigurationError(
                f"委派顺序不符：期望 {fixture.target_agent_id}，实际 {request.target_agent_id}"
            )
        self._cursor += 1
        self._consumed.add(fixture.fixture_id)
        return fixture

    def _match_normal(self, request: DelegationRequest) -> DelegationFixture:
        """NORMAL：按目标 Agent 匹配尚未受理的 Fixture。"""
        for candidate in self._fixtures:
            if candidate.fixture_id in self._consumed:
                continue
            if candidate.target_agent_id != request.target_agent_id:
                continue
            self._consumed.add(candidate.fixture_id)
            return candidate
        raise FixtureConfigurationError(f"委派目标 {request.target_agent_id} 没有可用 fixture")

    async def result(self, child_run_id: str) -> DelegationResult | None:
        """返回冻结子执行结果；未受理的子运行判定为配置失败。"""
        fixture: DelegationFixture | None = self._submitted.get(child_run_id)
        if fixture is None:
            raise FixtureConfigurationError(f"子运行 {child_run_id} 未经过 fixture 受理")
        if fixture.outcome is None:
            return None
        error: RunError | None = (
            RunError(RunErrorCode.INVALID_STATE, fixture.error_message) if fixture.error_message else None
        )
        return DelegationResult(
            child_run_id=child_run_id,
            outcome=fixture.outcome,
            output=fixture.output,
            error=error,
        )

    async def cancel(self, child_run_id: str) -> None:
        """隔离环境无真实子执行，仅记录取消请求。"""
        self.cancelled_children.append(child_run_id)

    def verify_fully_consumed(self) -> None:
        """校验所有委派 Fixture 均被受理；剩余项视为缺失调用。"""
        if self.remaining:
            missing: list[str] = [item.fixture_id for item in self.remaining]
            raise FixtureConfigurationError(f"存在未被受理的委派 fixture：{missing}")


class FixtureRunPolicyPort:
    """返回 Case 冻结策略的隔离策略 Port。"""

    def __init__(self, policy: AgentPolicySnapshot) -> None:
        """绑定冻结的 Agent 策略快照。"""
        self._policy: AgentPolicySnapshot = policy
        self.resolved_count: int = 0

    async def resolve(self, request: RunRequest) -> AgentPolicySnapshot:
        """返回冻结策略；Agent 不一致说明 Case 配置错误。"""
        if request.agent_id != self._policy.agent_id:
            raise FixtureConfigurationError(
                f"策略 fixture 与请求 Agent 不一致：期望 {self._policy.agent_id}，实际 {request.agent_id}"
            )
        self.resolved_count += 1
        return self._policy


class FixtureContextPort:
    """按记录顺序回放上下文构建结果的隔离上下文 Port。"""

    def __init__(self, fixtures: tuple[ContextFixture, ...], mode: FixtureMatchMode) -> None:
        """绑定上下文 Fixture 与匹配模式，并初始化独立消费游标。"""
        self._fixtures: tuple[ContextFixture, ...] = fixtures
        self._mode: FixtureMatchMode = mode
        self._cursor: int = 0
        self.released_scopes: list[tuple[ContextOwner, str]] = []
        self.release_all_count: int = 0

    @property
    def remaining(self) -> tuple[ContextFixture, ...]:
        """尚未消费的上下文 Fixture。"""
        return self._fixtures[self._cursor :]

    async def build(self, request: RunRequest, execution: RunExecutionView) -> ContextBundle:
        """返回下一次冻结的上下文；超出记录即判定为额外调用。"""
        if self._cursor >= len(self._fixtures):
            raise FixtureConfigurationError(
                f"第 {self._cursor + 1} 次上下文构建没有对应 fixture（共 {len(self._fixtures)} 条）"
            )
        fixture: ContextFixture = self._fixtures[self._cursor]
        self._cursor += 1
        return ContextBundle(
            messages=fixture.messages,
            tools=fixture.tools,
            metadata=ContextMetadata(estimated_tokens=fixture.estimated_tokens),
        )

    async def release_scope(self, owner: ContextOwner, owner_key: str) -> None:
        """隔离环境不缓存 Slot 实例，仅记录释放请求。"""
        self.released_scopes.append((owner, owner_key))

    async def release_all(self) -> None:
        """隔离环境不缓存 Slot 实例，仅记录释放请求。"""
        self.release_all_count += 1

    def request_refresh(self, slot_id: str, owner: ContextOwner, owner_key: str) -> None:
        """冻结上下文不接受刷新请求。"""

    def publish_signal(self, signal: ContextRefreshSignal) -> None:
        """冻结上下文不消费外部刷新信号。"""

    def verify_fully_consumed(self) -> None:
        """校验所有上下文 Fixture 均被使用；剩余项视为缺失调用。"""
        if self.remaining:
            missing: list[str] = [item.fixture_id for item in self.remaining]
            raise FixtureConfigurationError(f"存在未被使用的上下文 fixture：{missing}")
