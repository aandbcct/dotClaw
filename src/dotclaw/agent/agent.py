"""Runtime v2 的 Agent 门面。

模块只保存 Agent 身份与展示所需依赖，将普通执行、审批恢复和取消委托给
SessionRunCoordinator，禁止持有旧 Runtime、Session 级状态或 delegation runner。
（阶段 1）Agent 已收缩为 Identity + Coordinator 的轻量门面，不再暴露或关闭任何
基础设施；工具/MCP/Skill/Dream 等诊断展示依赖由 ApplicationHost 统一持有（总体设计 §5.3）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..runtime.domain.facts import RunErrorCode, RunStatus
from .identity import AgentIdentity

if TYPE_CHECKING:
    from ..config import Config
    from ..runtime.application.dto import RunRequest, RunResult
    from ..runtime.application.ports import TextStreamPort
    from ..runtime.application.session_run_coordinator import SessionRunCoordinator
    from ..session.session import Session


class Agent:
    """以声明式身份驱动 Runtime v2 协调器的轻量门面。"""

    def __init__(
        self,
        identity: AgentIdentity,
        coordinator: SessionRunCoordinator,
        config: Config,
    ) -> None:
        """仅绑定身份与共享协调器。

        Agent 不持有或关闭任何基础设施（总体设计 §5.3）：后台 MCP 任务与
        Context 缓存的生命周期由 ApplicationHost 统一拥有；工具/MCP/
        Skill/Dream 等诊断展示依赖改由 ApplicationHost 持有。
        """
        self._identity: AgentIdentity = identity
        self._coordinator: SessionRunCoordinator = coordinator
        self._config: Config = config
        self._last_run_result: RunResult | None = None

    @property
    def identity(self) -> AgentIdentity:
        """返回不可变的 Agent 身份约束。"""
        return self._identity

    @property
    def agent_id(self) -> str:
        """返回 Agent 唯一标识。"""
        return self._identity.agent_id

    @property
    def agent_name(self) -> str:
        """返回 Agent 显示名称。"""
        return self._identity.agent_name

    @property
    def config(self) -> Config:
        """返回当前 Agent 使用的全局配置。"""
        return self._config

    @property
    def model_id(self) -> str:
        """返回当前 Agent 解析后的模型标识，供 CLI 只读展示。"""
        return self._identity.resolve_model(self._config.llm.default_model)

    @property
    def last_run_result(self) -> RunResult | None:
        """返回最近一次运行结果，供 Channel 展示审批或错误信息。"""
        return self._last_run_result

    @property
    def has_streamed_final_answer(self) -> bool:
        """判断最近成功回复是否已在执行期间流式呈现。"""
        result: RunResult | None = self._last_run_result
        return result is not None and result.final_message is not None and result.has_streamed_text

    async def process(
        self,
        session: Session,
        user_message: str,
        text_stream_port: "TextStreamPort | None" = None,
    ) -> str:
        """提交普通用户消息，并将标准 RunResult 转换为 Channel 文本。

        ``text_stream_port`` 为本次提交的运行级输出端口，由 CLI 每次消息构造。
        """
        from ..runtime.application.request_factory import create_run_request

        async def create_request() -> RunRequest:
            return create_run_request(session, self.agent_id, user_message)

        result: RunResult = await self._coordinator.submit_prepared(session.id, create_request, text_stream_port)
        self._last_run_result = result
        return _display_result(result)

    async def resolve_approval(
        self,
        approval_id: str,
        approved: bool,
        text_stream_port: "TextStreamPort | None" = None,
    ) -> str:
        """提交审批决定并返回同一运行恢复后的展示文本；透传运行级输出端口。"""
        result: RunResult = await self._coordinator.resolve_approval(approval_id, approved, text_stream_port)
        self._last_run_result = result
        return _display_result(result)

    async def cancel_run(self, run_id: str, reason: str) -> None:
        """将取消请求交由运行协调器处理。"""
        await self._coordinator.cancel(run_id, reason)

    async def retry_interrupted(self, run_id: str, text_stream_port: "TextStreamPort | None" = None) -> str:
        """重试可恢复中断 Run，并返回 Channel 可展示的结果；透传运行级输出端口。"""
        result: RunResult = await self._coordinator.retry_interrupted(run_id, text_stream_port)
        self._last_run_result = result
        return _display_result(result)

    async def abandon_interrupted(self, run_id: str) -> str:
        """放弃可恢复中断 Run，并返回 Channel 可展示的结果。"""
        result: RunResult = await self._coordinator.abandon_interrupted(run_id)
        self._last_run_result = result
        return _display_result(result)


def _display_result(result: RunResult) -> str:
    """将 Runtime 领域结果收敛为 Channel 可直接展示的文本。"""
    if result.final_message is not None:
        return result.final_message.content
    if result.status is RunStatus.WAITING_APPROVAL:
        return f"运行等待审批：{result.run_id}"
    if result.status is RunStatus.INTERRUPTED:
        return f"运行已中断，可重试：{result.run_id}"
    if result.status is RunStatus.ABANDONED:
        return f"运行已放弃：{result.run_id}"
    if result.error is not None:
        if result.error.code is RunErrorCode.SESSION_BUSY:
            return "当前会话仍有未完成运行，请先完成审批、重试或取消后再发送消息。"
        return f"执行失败：{result.error.message}"
    return f"执行未完成：{result.status.value}"
