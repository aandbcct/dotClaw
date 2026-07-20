"""Runtime v3 依赖的外部能力协议。"""

from __future__ import annotations

from typing import Protocol

from ..domain.events import RunEvent
from dotclaw.runtime.application.execution import RunExecutionView
from ..domain.context import ContextVersion, StagedHistoryCompression, SuccessCommitIntent
from ..domain.facts import (
    AgentRun,
    ApprovalRecord,
    RunCheckpoint,
    RunMessage,
    AgentPolicySnapshot,
)
from .context_compaction import ContextCompactionRequest, ContextCompactionResult
from .dto import ContextBundle, DelegationRequest, DelegationResult, DelegationSubmission, RunRequest, ToolInvocation, ToolResult


class TextStreamPort(Protocol):
    """将模型文本增量交付给入口层的应用协议。"""

    async def emit(self, run_id: str, chunk: str) -> None:
        """输出指定运行的一段非空文本增量。"""


class ContextCompactionPort(Protocol):
    """将有序上下文片段压缩为可版本化摘要的协议。"""

    async def compact(self, request: ContextCompactionRequest) -> ContextCompactionResult:
        """基于已有摘要与待覆盖片段生成新的摘要。"""


class ConversationProjectionPort(Protocol):
    """将成功运行投影到既有 Session Conversation 的协议。"""

    async def project_success(
        self,
        run: AgentRun,
        user_message: RunMessage,
        final_message: RunMessage,
    ) -> None:
        """仅在运行成功后追加一条可见对话记录。"""


class RunRepository(Protocol):
    """保存运行摘要、消息、事件与成功会话投影的协议。"""

    async def create_run(self, run: AgentRun) -> None:
        """创建运行摘要。"""

    async def load_run(self, session_id: str, run_id: str) -> AgentRun | None:
        """加载指定运行摘要。"""

    async def find_run(self, run_id: str) -> AgentRun | None:
        """按运行标识定位摘要，供取消和审批恢复使用。"""

    async def save_run(self, run: AgentRun) -> None:
        """原子更新运行摘要。"""

    async def save_messages(self, session_id: str, run_id: str, messages: tuple[RunMessage, ...]) -> None:
        """原子更新完整运行消息。"""

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

    async def commit_success(
        self,
        run: AgentRun,
        final_message: RunMessage,
        completed_event: RunEvent,
    ) -> None:
        """以成功终态为提交标记，统一提交事件、Conversation 投影和 Run 摘要。"""


class CheckpointRepository(Protocol):
    """按 run_id 保存和恢复检查点的协议。"""

    async def save(self, checkpoint: RunCheckpoint) -> None:
        """原子保存最新检查点。"""

    async def load(self, session_id: str, run_id: str) -> RunCheckpoint | None:
        """加载指定运行的最新检查点。"""

    async def delete(self, session_id: str, run_id: str) -> None:
        """删除已经不再需要的检查点。"""


class ContextPort(Protocol):
    """根据冻结请求和执行视图构造完整模型上下文。"""

    async def build(self, request: RunRequest, execution: RunExecutionView) -> ContextBundle:
        """构造完整模型消息、工具定义和上下文元数据。"""


class RunPolicyPort(Protocol):
    """在运行开始前解析并冻结 Agent 执行策略。"""

    async def resolve(self, request: RunRequest) -> AgentPolicySnapshot:
        """返回本次运行不可变的身份、模型和上下文策略。"""


class LLMPort(Protocol):
    """执行标准化模型调用并支持尽力取消的协议。"""

    async def complete(self, context: ContextBundle, execution: RunExecutionView) -> RunMessage:
        """返回一次完整模型响应消息。"""

    async def cancel(self, run_id: str) -> None:
        """尽力取消正在进行的模型调用。"""


class ToolPort(Protocol):
    """检查、执行和取消工具调用的协议。"""

    async def execute(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        """执行工具或返回审批需求。"""

    async def cancel(self, run_id: str) -> None:
        """尽力取消当前运行中的工具调用。"""


class ApprovalRepository(Protocol):
    """保存、定位和消费审批记录的协议。"""

    async def create(self, record: ApprovalRecord) -> None:
        """创建审批记录。"""

    async def load(self, approval_id: str) -> ApprovalRecord | None:
        """按审批 ID 查找记录。"""

    async def consume(self, approval_id: str) -> ApprovalRecord | None:
        """原子消费仍处于待处理状态的审批记录。"""


class DelegationPort(Protocol):
    """提交、查询和取消外部子执行的可选协议。"""

    async def submit(self, request: DelegationRequest) -> DelegationSubmission:
        """提交子执行并返回子 Run、Task 与目标 Session 的稳定关联。"""

    async def result(self, child_run_id: str) -> DelegationResult | None:
        """查询子执行结果。"""

    async def cancel(self, child_run_id: str) -> None:
        """尽力取消子执行。"""
