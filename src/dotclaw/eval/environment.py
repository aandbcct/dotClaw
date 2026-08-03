"""隔离 Runtime 执行环境组装。

``EvalEnvironment`` 把 ``EvalCase`` 的 Fixture 装配为一个默认拒绝真实依赖的
``RuntimeEngine``：所有外部能力首先由 Fixture 提供，未匹配调用在配置了真实
依赖端口时回退到该端口，否则直接判定为配置失败，绝不静默接触生产 LLM、
工具、Session、Memory 或网络。

本模块同时提供内存版的 Run / Checkpoint 仓储与固定的 TokenCounter /
HistoryCompactor，确保两次独立环境不共享任何运行事实或 Fixture 消费游标。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..runtime.application.approval_service import ApprovalService
from ..runtime.application.cancellation_service import CancellationService
from ..runtime.application.context_budget import TokenCountRequest, TokenCountResult
from ..runtime.application.dto import ConversationSnapshot, RunRequest, RunResult
from ..runtime.application.engine import RuntimeEngine
from ..runtime.application.history_compaction import HistoryCompactionRequest, HistoryCompactionResult
from ..runtime.adapters.in_memory_run_repository import InMemoryRunRepository
from ..runtime.application.ports import (
    ApprovalRepository,
    CheckpointRepository,
    ContextPort,
    DelegationPort,
    HistoryCompactorPort,
    LLMPort,
    RunPolicyPort,
    RunRepository,
    TokenCounterPort,
    ToolPort,
)
from ..runtime.domain.events import RunEvent
from ..runtime.domain.facts import AgentRun, RunCheckpoint, RunMessage
from ..runtime.domain.state import AgentRunState
from .fixtures import (
    FixtureApprovalRepository,
    FixtureConfigurationError,
    FixtureContextPort,
    FixtureDelegationPort,
    FixtureRunPolicyPort,
    FixtureToolPort,
    ScriptedLLMPort,
)
from .models import EvalCase, ExecutionMode, FixtureMatchMode


def _match_mode(mode: ExecutionMode) -> FixtureMatchMode:
    """Playback 冻结回放走 STRICT，Re-execution 走允许参数变化的 NORMAL。"""
    return FixtureMatchMode.STRICT if mode is ExecutionMode.PLAYBACK else FixtureMatchMode.NORMAL


# --------------------------------------------------------------------------- #
# 内存仓储与固定计数器：保证环境完全自包含、互不共享
# --------------------------------------------------------------------------- #


class InMemoryCheckpointRepository:
    """隔离的内存检查点仓储，不参与任何生产持久化。"""

    def __init__(self) -> None:
        """初始化隔离的检查点事实表。"""
        self._checkpoints: dict[tuple[str, str], RunCheckpoint] = {}

    async def save(self, checkpoint: RunCheckpoint) -> None:
        """原子保存最新检查点。"""
        self._checkpoints[(checkpoint.session_id, checkpoint.run_id)] = checkpoint

    async def load(self, session_id: str, run_id: str) -> RunCheckpoint | None:
        """加载指定运行的最新检查点。"""
        return self._checkpoints.get((session_id, run_id))

    async def delete(self, session_id: str, run_id: str) -> None:
        """删除已不再需要的检查点。"""
        self._checkpoints.pop((session_id, run_id), None)


class _FixedTokenCounter:
    """固定返回 1 token 的精确计数器，隔离环境不接真实 Tokenizer。"""

    async def count(self, request: TokenCountRequest) -> TokenCountResult:
        """所有结构化输入均记为 1 token，使上下文预算恒在窗口内。"""
        return TokenCountResult(input_tokens=1)


class _FixedHistoryCompactor:
    """固定返回空摘要的历史压缩器，隔离环境不产生真实压缩调用。"""

    async def compact_history(self, request: HistoryCompactionRequest) -> HistoryCompactionResult:
        """返回空摘要，表示无需任何历史压缩。"""
        return HistoryCompactionResult(summary="")


# --------------------------------------------------------------------------- #
# 可选真实依赖：Re-execution 模式下注入，缺省则完全由 Fixture 驱动
# --------------------------------------------------------------------------- #


@dataclass
class EvalDependencies:
    """Re-execution 模式下可注入的真实能力端口；缺省时由 Fixture 提供。

    这些端口仅在对应 Fixture 无法匹配调用时被回退使用。Playback 场景下若
    不提供，未匹配的调用会直接判定为配置失败，从而默认拒绝真实依赖。
    """

    llm_port: LLMPort | None = None
    tool_port: ToolPort | None = None
    context_port: ContextPort | None = None
    policy_port: RunPolicyPort | None = None
    delegation_port: DelegationPort | None = None
    approval_repository: ApprovalRepository | None = None


# --------------------------------------------------------------------------- #
# Fixture 优先的组合端口：先消费 Fixture，未匹配再回退真实端口
# --------------------------------------------------------------------------- #


class _LLMComposite:
    """LLM 组合端口：优先脚本化 Fixture，缺失时回退真实端口。"""

    def __init__(self, fixture: ScriptedLLMPort | None, real: LLMPort | None) -> None:
        """绑定 Fixture 与可选真实端口。"""
        self._fixture: ScriptedLLMPort | None = fixture
        self._real: LLMPort | None = real
        self.fixture_served: int = 0
        self.real_served: int = 0

    async def complete(
        self,
        context: Any,
        execution: Any,
        output_port: Any = None,
    ) -> RunMessage:
        """优先返回 Fixture 响应；Fixture 缺失时回退真实端口。"""
        if self._fixture is not None:
            try:
                result: RunMessage = await self._fixture.complete(context, execution, output_port)
                self.fixture_served += 1
                return result
            except FixtureConfigurationError:
                if self._real is None:
                    raise
        if self._real is None:
            raise FixtureConfigurationError("未配置 LLM fixture 或真实 LLM 端口")
        self.real_served += 1
        return await self._real.complete(context, execution, output_port)

    async def cancel(self, run_id: str) -> None:
        """取消请求仅转发到真实端口（若有）。"""
        if self._real is not None:
            await self._real.cancel(run_id)


class _ToolComposite:
    """工具组合端口：优先 Fixture 结果，缺失时回退真实端口。"""

    def __init__(self, fixture: FixtureToolPort | None, real: ToolPort | None) -> None:
        """绑定 Fixture 与可选真实端口。"""
        self._fixture: FixtureToolPort | None = fixture
        self._real: ToolPort | None = real
        self.fixture_served: int = 0
        self.real_served: int = 0

    async def execute(self, invocation: Any, execution: Any) -> Any:
        """优先返回 Fixture 结果；Fixture 缺失时回退真实端口。"""
        if self._fixture is not None:
            try:
                result: Any = await self._fixture.execute(invocation, execution)
                self.fixture_served += 1
                return result
            except FixtureConfigurationError:
                if self._real is None:
                    raise
        if self._real is None:
            raise FixtureConfigurationError("未配置工具 fixture 或真实工具端口")
        self.real_served += 1
        return await self._real.execute(invocation, execution)

    async def cancel(self, run_id: str) -> None:
        """取消请求仅转发到真实端口（若有）。"""
        if self._real is not None:
            await self._real.cancel(run_id)


class _ContextComposite:
    """上下文组合端口：优先冻结 Fixture，缺失时回退真实端口。"""

    def __init__(self, fixture: FixtureContextPort | None, real: ContextPort | None) -> None:
        """绑定 Fixture 与可选真实端口。"""
        self._fixture: FixtureContextPort | None = fixture
        self._real: ContextPort | None = real
        self.fixture_served: int = 0
        self.real_served: int = 0

    async def build(self, request: Any, execution: Any) -> Any:
        """优先返回冻结上下文；Fixture 缺失时回退真实端口。"""
        if self._fixture is not None:
            try:
                result: Any = await self._fixture.build(request, execution)
                self.fixture_served += 1
                return result
            except FixtureConfigurationError:
                if self._real is None:
                    raise
        if self._real is None:
            raise FixtureConfigurationError("未配置上下文 fixture 或真实上下文端口")
        self.real_served += 1
        return await self._real.build(request, execution)

    async def release_scope(self, owner: Any, owner_key: str) -> None:
        """释放请求仅转发到真实端口（若有）。"""
        if self._real is not None:
            await self._real.release_scope(owner, owner_key)

    async def release_all(self) -> None:
        """释放请求仅转发到真实端口（若有）。"""
        if self._real is not None:
            await self._real.release_all()

    def request_refresh(self, slot_id: str, owner: Any, owner_key: str) -> None:
        """刷新请求仅转发到真实端口（若有）。"""
        if self._real is not None:
            self._real.request_refresh(slot_id, owner, owner_key)

    def publish_signal(self, signal: Any) -> None:
        """刷新信号仅转发到真实端口（若有）。"""
        if self._real is not None:
            self._real.publish_signal(signal)


class _PolicyComposite:
    """策略组合端口：优先冻结 Fixture，缺失时回退真实端口。"""

    def __init__(self, fixture: FixtureRunPolicyPort | None, real: RunPolicyPort | None) -> None:
        """绑定 Fixture 与可选真实端口。"""
        self._fixture: FixtureRunPolicyPort | None = fixture
        self._real: RunPolicyPort | None = real
        self.fixture_served: int = 0
        self.real_served: int = 0

    async def resolve(self, request: Any) -> Any:
        """优先返回冻结策略；Fixture 缺失时回退真实端口。"""
        if self._fixture is not None:
            try:
                result: Any = await self._fixture.resolve(request)
                self.fixture_served += 1
                return result
            except FixtureConfigurationError:
                if self._real is None:
                    raise
        if self._real is None:
            raise FixtureConfigurationError("未配置策略 fixture 或真实策略端口")
        self.real_served += 1
        return await self._real.resolve(request)


class _DelegationComposite:
    """委派组合端口：优先 Fixture 受理，缺失时回退真实端口。"""

    def __init__(self, fixture: FixtureDelegationPort | None, real: DelegationPort | None) -> None:
        """绑定 Fixture 与可选真实端口。"""
        self._fixture: FixtureDelegationPort | None = fixture
        self._real: DelegationPort | None = real
        self.fixture_served: int = 0
        self.real_served: int = 0

    async def submit(self, request: Any) -> Any:
        """优先受理 Fixture 子执行；Fixture 缺失时回退真实端口。"""
        if self._fixture is not None:
            try:
                result: Any = await self._fixture.submit(request)
                self.fixture_served += 1
                return result
            except FixtureConfigurationError:
                if self._real is None:
                    raise
        if self._real is None:
            raise FixtureConfigurationError("未配置委派 fixture 或真实委派端口")
        self.real_served += 1
        return await self._real.submit(request)

    async def result(self, child_run_id: str) -> Any:
        """优先查询 Fixture 子执行结果；Fixture 缺失时回退真实端口。"""
        if self._fixture is not None:
            try:
                return await self._fixture.result(child_run_id)
            except FixtureConfigurationError:
                if self._real is None:
                    raise
        if self._real is None:
            raise FixtureConfigurationError("未配置委派 fixture 或真实委派端口")
        return await self._real.result(child_run_id)

    async def cancel(self, child_run_id: str) -> None:
        """取消请求仅转发到真实端口（若有）。"""
        if self._real is not None:
            await self._real.cancel(child_run_id)


class _ApprovalRepositoryComposite:
    """审批仓储组合端口：优先 Fixture 记录，缺失时回退真实仓储。"""

    def __init__(self, fixture: FixtureApprovalRepository | None, real: ApprovalRepository | None) -> None:
        """绑定 Fixture 与可选真实仓储。"""
        self._fixture: FixtureApprovalRepository | None = fixture
        self._real: ApprovalRepository | None = real
        self.fixture_served: int = 0
        self.real_served: int = 0

    async def create(self, record: Any) -> None:
        """优先写入 Fixture 记录；Fixture 缺失时回退真实仓储。"""
        if self._fixture is not None:
            try:
                await self._fixture.create(record)
                self.fixture_served += 1
                return
            except FixtureConfigurationError:
                if self._real is None:
                    raise
        if self._real is None:
            raise FixtureConfigurationError("未配置审批 fixture 或真实审批仓储")
        self.real_served += 1
        await self._real.create(record)

    async def load(self, approval_id: str) -> Any:
        """优先读取 Fixture 记录；未命中且配置了真实仓储时回退。"""
        if self._fixture is not None:
            record: Any = await self._fixture.load(approval_id)
            if record is not None:
                return record
        if self._real is None:
            return None
        return await self._real.load(approval_id)

    async def consume(self, approval_id: str) -> Any:
        """优先消费 Fixture 记录；Fixture 缺失时回退真实仓储。"""
        if self._fixture is not None:
            try:
                record: Any = await self._fixture.consume(approval_id)
                if record is not None:
                    self.fixture_served += 1
                return record
            except FixtureConfigurationError:
                if self._real is None:
                    raise
        if self._real is None:
            return None
        self.real_served += 1
        return await self._real.consume(approval_id)


# --------------------------------------------------------------------------- #
# 执行结果封装与隔离环境
# --------------------------------------------------------------------------- #


@dataclass
class EvalRunOutcome:
    """一次隔离执行的可见结果，附带仓储事实与所属环境引用。"""

    run_id: str
    result: RunResult
    run: AgentRun | None
    messages: tuple[RunMessage, ...]
    events: tuple[RunEvent, ...]
    environment: EvalEnvironment

    @property
    def state(self) -> AgentRunState:
        """运行终态（AgentRunState）。"""
        return self.result.state

    def assert_fully_consumed(self) -> None:
        """断言所有声明的 Fixture 均被完整消费，否则判定 Case 配置错误。"""
        self.environment.verify_fixtures_consumed()


class EvalEnvironment:
    """把 ``EvalCase`` 装配为默认拒绝真实依赖的隔离 Runtime 执行环境。"""

    def __init__(
        self,
        case: EvalCase,
        mode: FixtureMatchMode | None = None,
        dependencies: EvalDependencies | None = None,
    ) -> None:
        """组装内存仓储、固定计数器、Fixture 端口与 RuntimeEngine。"""
        self.case: EvalCase = case
        self.mode: FixtureMatchMode = mode or _match_mode(case.execution_mode)
        deps: EvalDependencies = dependencies or EvalDependencies()

        # 每个环境独立持有内存仓储，互不共享运行事实或检查点。
        self.run_repository: RunRepository = InMemoryRunRepository()
        self.checkpoint_repository: CheckpointRepository = InMemoryCheckpointRepository()
        self.token_counter: TokenCounterPort = _FixedTokenCounter()
        self.history_compactor: HistoryCompactorPort = _FixedHistoryCompactor()

        # 每个环境独立构造 Fixture 端口，消费游标不跨环境共享。
        self.scripted_llm: ScriptedLLMPort = ScriptedLLMPort(case.llm_fixture, self.mode)
        self.fixture_tool: FixtureToolPort = FixtureToolPort(case.tool_fixtures, self.mode)
        self.fixture_context: FixtureContextPort = FixtureContextPort(case.context_fixtures, self.mode)
        self.fixture_approval: FixtureApprovalRepository = FixtureApprovalRepository(case.approval_fixtures, self.mode)
        self.policy_fixture: FixtureRunPolicyPort = FixtureRunPolicyPort(case.policy_fixture)
        self.fixture_delegation: FixtureDelegationPort | None = (
            FixtureDelegationPort(case.delegation_fixtures, self.mode) if case.delegation_fixtures else None
        )

        # 组合端口：Fixture 优先，未匹配再回退真实依赖（缺省则拒绝）。
        self.llm_port: _LLMComposite = _LLMComposite(self.scripted_llm, deps.llm_port)
        self.tool_port: _ToolComposite = _ToolComposite(self.fixture_tool, deps.tool_port)
        self.context_port: _ContextComposite = _ContextComposite(self.fixture_context, deps.context_port)
        self.policy_port: _PolicyComposite = _PolicyComposite(self.policy_fixture, deps.policy_port)
        self.approval_repository: _ApprovalRepositoryComposite = _ApprovalRepositoryComposite(
            self.fixture_approval, deps.approval_repository
        )
        self.delegation_port: _DelegationComposite | None = (
            _DelegationComposite(self.fixture_delegation, deps.delegation_port)
            if (self.fixture_delegation is not None or deps.delegation_port is not None)
            else None
        )

        self.approval_service: ApprovalService = ApprovalService(self.approval_repository)
        self.cancellation_service: CancellationService = CancellationService()

        self.engine: RuntimeEngine = RuntimeEngine(
            run_repository=self.run_repository,
            checkpoint_repository=self.checkpoint_repository,
            context_port=self.context_port,
            llm_port=self.llm_port,
            tool_port=self.tool_port,
            policy_port=self.policy_port,
            approval_service=self.approval_service,
            cancellation_service=self.cancellation_service,
            delegation_port=self.delegation_port,
            token_counter=self.token_counter,
            history_compactor=self.history_compactor,
        )

    def verify_fixtures_consumed(self) -> None:
        """校验全部声明 Fixture 均被消费；剩余项视为缺失调用。"""
        self.scripted_llm.verify_fully_consumed()
        self.fixture_tool.verify_fully_consumed()
        self.fixture_context.verify_fully_consumed()
        self.fixture_approval.verify_fully_consumed()
        if self.fixture_delegation is not None:
            self.fixture_delegation.verify_fully_consumed()

    async def run(self, output_port: Any = None) -> EvalRunOutcome:
        """按 Case 输入构造请求并执行，返回隔离运行结果。"""
        request: RunRequest = self._build_request()
        result: RunResult = await self.engine.execute(request, output_port=output_port)
        run: AgentRun | None = await self.run_repository.load_run(request.session_id, result.run_id)
        messages: tuple[RunMessage, ...] = await self.run_repository.load_messages(
            request.session_id, result.run_id
        )
        events: tuple[RunEvent, ...] = await self.run_repository.load_events(
            request.session_id, result.run_id
        )
        return EvalRunOutcome(
            run_id=result.run_id,
            result=result,
            run=run,
            messages=messages,
            events=events,
            environment=self,
        )

    def _build_request(self) -> RunRequest:
        """由 Case 的会话与输入消息构造执行请求。"""
        conversation: ConversationSnapshot = ConversationSnapshot(
            session_id=self.case.conversation_fixture.session_id,
            messages=tuple(self.case.conversation_fixture.messages),
            version=self.case.conversation_fixture.version,
        )
        return RunRequest(
            session_id=self.case.conversation_fixture.session_id,
            lease_id=f"eval-{self.case.case_id}",
            agent_id=self.case.agent_id,
            user_message=self.case.input,
            conversation=conversation,
        )
