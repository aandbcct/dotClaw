"""Runtime v4 依赖的外部能力协议。"""

from __future__ import annotations

from typing import Protocol

from ..domain.cancellation import CancellationRecord, CancellationCommitIntent, CancellationReceipt
from ..domain.events import RunEvent
from dotclaw.runtime.application.execution import RunExecutionView
from ..domain.context import ContextOwner, ContextVersion, StagedHistoryCompression, SuccessCommitFaultPoint, SuccessCommitIntent
from ..domain.facts import (
    AgentRun,
    ApprovalRecord,
    HistoryCompressionSnapshot,
    RunCheckpoint,
    RunMessage,
    AgentPolicySnapshot,
    ToolCall,
)
from ..domain.tool_recovery import ToolCallLedger
from .context_budget import TokenCountRequest, TokenCountResult
from .context_compaction import ContextCompactionRequest, ContextCompactionResult
from .history_compaction import HistoryCompactionRequest, HistoryCompactionResult
from .dto import ContextBundle, ContextRefreshSignal, DelegationRequest, DelegationResult, DelegationSubmission, LLMOutputEvent, RunAcceptedEvent, RunRequest, ToolInvocation, ToolRecoveryContract, ToolResult

RUN_SESSION_MISMATCH_REASON = "run_session_mismatch"
"""仅用于完成读取后的归属拒绝，不与操作系统读盘权限错误混用。"""


class RunPersistenceError(RuntimeError):
    """运行持久化读取、写入、解析或条件投影失败。"""


class LLMUnavailableError(RuntimeError):
    """业务模型代理重试耗尽后的可恢复外部错误。"""


class InvalidLLMResponseError(RuntimeError):
    """模型正常结束却既无有效正文也无工具调用，不支持原检查点恢复。"""


class LLMOutputPort(Protocol):
    """承载运行接收确认与模型有序增量的应用协议。

    事件按语义分为 reasoning 与 response（见 ``LLMOutputEvent``）；入口层
    据此决定如何分区展示，但本端口不关心展示细节。
    """

    async def on_run_accepted(self, event: RunAcceptedEvent) -> None:
        """通知新运行已经持久化；不是终态，也不代表供应商已接收请求。"""

    async def emit(self, event: LLMOutputEvent) -> None:
        """输出一条语义化增量事件（reasoning 或 response）。"""


class ContextCompactionPort(Protocol):
    """将有序上下文片段压缩为可版本化摘要的协议。"""

    async def compact(self, request: ContextCompactionRequest) -> ContextCompactionResult:
        """基于已有摘要与待覆盖片段生成新的摘要。"""


class TokenCounterPort(Protocol):
    """统计结构化 LLM 输入 Token 的精确应用端口。"""
    async def count(self, request: TokenCountRequest) -> TokenCountResult:
        """返回精确数量或明确 Tokenizer 错误。"""


class HistoryCompactorPort(Protocol):
    """按完整 Conversation 批次生成滚动历史摘要。"""
    async def compact_history(self, request: HistoryCompactionRequest) -> HistoryCompactionResult:
        """压缩一批完整 Conversation，失败时不产生候选。"""


class ConversationProjectionPort(Protocol):
    """将成功运行投影到既有 Session Conversation 的协议。"""

    async def project_success(
        self,
        run: AgentRun,
        user_message: RunMessage,
        final_message: RunMessage,
        history_compression: HistoryCompressionSnapshot | None,
        source_conversation_hash: str,
    ) -> None:
        """原子投影成功 Conversation 与可选最新历史压缩。"""


class SuccessCommitFaultPort(Protocol):
    """仅供恢复测试在持久化边界模拟进程中断的端口。"""

    async def inject(self, point: SuccessCommitFaultPoint) -> None:
        """在指定成功提交边界抛出测试定义的中断异常。"""


class RunRepository(Protocol):
    """保存运行摘要、消息、事件与成功会话投影的协议。"""

    async def load_cancellation(self, session_id: str, run_id: str) -> CancellationRecord | None:
        """读取并校验取消记录归属。"""

    async def save_cancellation(self, session_id: str, record: CancellationRecord, *, expected_revision: int) -> None:
        """校验修订号后保存取消进度。"""

    async def list_child_runs(self, parent_run_id: str) -> tuple[AgentRun, ...]:
        """从持久关系查询直接子运行。"""

    async def has_pending_success_commit(self, session_id: str, run_id: str) -> bool:
        """查询成功是否已保留提交权，不触发恢复。"""

    async def has_pending_cancellation_commit(self, session_id: str, run_id: str) -> bool:
        """查询目标运行是否存在未完成取消提交。"""

    async def recover_cancellation_commit(self, session_id: str, run_id: str) -> None:
        """只重放目标运行已冻结的取消提交意图。"""

    async def commit_cancellation(self, intent: CancellationCommitIntent) -> None:
        """按可恢复协议提交取消终态。"""

    async def recover_pending_cancellation_commits(self) -> None:
        """幂等恢复所有取消提交意图。"""

    async def list_pending_cancellations(self) -> tuple[CancellationRecord, ...]:
        """查询尚未完成收尾的取消请求。"""

    async def list_runs(self) -> tuple[AgentRun, ...]:
        """按 started_at、run_id 稳定枚举运行事实。"""

    async def create_run(self, run: AgentRun) -> None:
        """创建运行摘要。"""

    async def load_run(self, session_id: str, run_id: str) -> AgentRun | None:
        """加载指定运行摘要。"""

    async def load_run_snapshot(self, session_id: str, run_id: str) -> AgentRun | None:
        """严格校验归属的纯读取，不触发任何提交恢复。"""

    async def find_run(self, run_id: str) -> AgentRun | None:
        """按运行标识定位摘要，供取消和审批恢复使用。"""

    async def list_active_runs(self, session_id: str) -> tuple[AgentRun, ...]:
        """纯读未完整提交终态的Run：非Ended或仍有成功/取消意图，作为唯一占用规则。"""

    async def save_run(self, run: AgentRun) -> None:
        """原子更新运行摘要。"""

    async def ensure_tool_run(self, base_run: AgentRun, target_run: AgentRun) -> None:
        """目标已存在则幂等成功；仅当前等于 base_run 时写入目标。"""

    async def load_tool_ledger(self, session_id: str, run_id: str) -> ToolCallLedger | None:
        """加载严格校验归属的工具账本，不存在返回 None。"""

    async def save_tool_ledger(self, ledger: ToolCallLedger, *, expected_revision: int) -> None:
        """按预期修订号保存下一账本版本。"""

    async def save_messages(self, session_id: str, run_id: str, messages: tuple[RunMessage, ...]) -> None:
        """原子更新完整运行消息。"""

    async def ensure_tool_messages(self, session_id: str, run_id: str, messages: tuple[RunMessage, ...]) -> None:
        """按 ID 与完整内容幂等补写连续消息。"""

    async def append_context_version(
        self,
        session_id: str,
        run_id: str,
        context_version: ContextVersion,
    ) -> None:
        """追加 Run 的不可变 Context Version，禁止覆盖既有版本。"""

    async def load_context_versions(self, session_id: str, run_id: str) -> tuple[ContextVersion, ...]:
        """加载按版本连续递增的上下文版本事实。"""

    async def set_active_context_version(self, session_id: str, run_id: str, version: int) -> None:
        """保存 Run 当前活动的 Context Version 引用。"""

    async def save_staged_history_compressions(
        self,
        session_id: str,
        run_id: str,
        candidates: tuple[StagedHistoryCompression, ...],
    ) -> None:
        """保存不含摘要正文的历史压缩候选控制信息。"""

    async def save_success_commit_intent(
        self,
        session_id: str,
        run_id: str,
        intent: SuccessCommitIntent,
    ) -> None:
        """保存可恢复成功提交意图控制信息。"""

    async def load_messages(self, session_id: str, run_id: str) -> tuple[RunMessage, ...]:
        """加载完整运行消息。"""

    async def append_event(self, session_id: str, event: RunEvent) -> None:
        """追加已引用存在消息的运行事件。"""

    async def load_events(self, session_id: str, run_id: str) -> tuple[RunEvent, ...]:
        """读取按 sequence 连续排列的运行事件。"""

    async def ensure_tool_events(self, session_id: str, events: tuple[RunEvent, ...]) -> None:
        """校验连续事件前缀并原子替换完整 JSONL。"""

    async def commit_success(
        self,
        run: AgentRun,
        final_message: RunMessage,
        completed_event: RunEvent,
        success_intent: SuccessCommitIntent,
    ) -> None:
        """以成功提交意图统一驱动 Conversation、事件和终态 Run 的恢复顺序。"""


class CheckpointRepository(Protocol):
    """按 run_id 保存和恢复检查点的协议。"""

    async def save(self, checkpoint: RunCheckpoint) -> None:
        """原子保存最新检查点。"""

    async def load(self, session_id: str, run_id: str) -> RunCheckpoint | None:
        """加载指定运行的最新检查点。"""

    async def delete(self, session_id: str, run_id: str) -> None:
        """删除已经不再需要的检查点。"""

    async def ensure_tool_checkpoint(
        self,
        base: RunCheckpoint | None,
        target: RunCheckpoint | None,
    ) -> None:
        """按 base/target 条件幂等保存或删除工具投影检查点。"""


class ContextPort(Protocol):
    """根据冻结请求和执行视图构造完整模型上下文。"""

    async def build(self, request: RunRequest, execution: RunExecutionView) -> ContextBundle:
        """构造完整模型消息、工具定义和上下文元数据。"""

    async def release_scope(self, owner: ContextOwner, owner_key: str) -> None:
        """在指定 Owner 生命周期结束时释放其 Slot 实例缓存。"""

    async def release_all(self) -> None:
        """Host 关闭时释放全部缓存 Slot 实例（跨所有 Owner 生命周期）。"""

    def request_refresh(self, slot_id: str, owner: ContextOwner, owner_key: str) -> None:
        """请求指定 Owner 的 Slot 在下一次安全点刷新。"""

    def publish_signal(self, signal: ContextRefreshSignal) -> None:
        """发布携带载荷的定向刷新事件。"""


class RunPolicyPort(Protocol):
    """在运行开始前解析并冻结 Agent 执行策略。"""

    async def resolve(self, request: RunRequest) -> AgentPolicySnapshot:
        """返回本次运行不可变的身份、模型和上下文策略。"""


class LLMPort(Protocol):
    """执行标准化模型调用并支持尽力取消的协议。"""

    async def complete(
        self,
        context: ContextBundle,
        execution: RunExecutionView,
        output_port: LLMOutputPort | None = None,
    ) -> RunMessage:
        """返回一次完整模型响应消息；output_port 为本次提交的运行级输出端口。"""


class ToolPort(Protocol):
    """检查、执行和取消工具调用的协议。"""

    async def execute(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        """执行工具或返回审批需求。"""

    async def preflight(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        """使用真实参数经过安全链，返回审批需求但不执行处理器。"""

    async def describe_recovery(self, call: ToolCall, execution: RunExecutionView) -> ToolRecoveryContract:
        """只读返回已注册工具的恢复安全契约，不执行工具。"""


class ApprovalRepository(Protocol):
    """保存、定位和消费审批记录的协议。"""

    async def create(self, record: ApprovalRecord) -> None:
        """创建审批记录。"""

    async def ensure(self, record: ApprovalRecord) -> None:
        """幂等投影审批记录，拒绝身份变化或决议翻转。"""

    async def load(self, approval_id: str) -> ApprovalRecord | None:
        """按审批 ID 查找记录。"""

    async def consume(self, approval_id: str) -> ApprovalRecord | None:
        """原子消费仍处于待处理状态的审批记录。"""


class DelegationPort(Protocol):
    """准备、启动、读取和收尾独立子运行的协议。"""

    async def prepare(self, request: DelegationRequest) -> DelegationSubmission:
        """只创建本地稳定关联，不启动外部能力。"""

    async def start(self, child_run_id: str) -> None:
        """安排已准备子运行，不等待执行结束。"""

    async def result(self, child_run_id: str) -> DelegationResult | None:
        """读取已提交终态，活跃运行返回 None。"""

    async def wait_terminal(self, child_run_id: str) -> DelegationResult:
        """观察真实终态，观察者取消不取消子驱动。"""

    async def cancel(self, child_run_id: str, reason: str, parent_request_id: str | None) -> CancellationReceipt:
        """仅请求停止，返回持久化受理回执。"""
