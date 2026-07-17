"""将 Runtime v2 的 DelegationPort 请求映射为目标 Agent 的子 Run。"""

from __future__ import annotations

import uuid
from typing import Protocol

from ..runtime.application.ports import DelegationPort
from ..runtime.domain.models import (
    ConversationMessage,
    ConversationSnapshot,
    DelegationRequest,
    DelegationResult,
    RunRequest,
    RunResult,
    RunStatus,
)
from ..session.session import SessionManager
from .registry import AgentRegistry


class DelegationSubmissionPort(Protocol):
    """适配器提交和取消目标 Run 所需的最小协调接口。"""

    async def submit(self, request: RunRequest) -> RunResult:
        """串行提交目标 Session 的普通 Run。"""

    async def cancel(self, run_id: str, reason: str) -> None:
        """取消已创建的目标 Run。"""


class RuntimeDelegationAdapter(DelegationPort):
    """在独立 target Session 内执行子 Run，并缓存标准化结果。"""

    def __init__(
        self,
        session_manager: SessionManager,
        agent_registry: AgentRegistry,
    ) -> None:
        """绑定目标 Run 的会话仓储与 Agent 身份目录。"""
        self._coordinator: DelegationSubmissionPort | None = None
        self._session_manager: SessionManager = session_manager
        self._agent_registry: AgentRegistry = agent_registry
        self._results: dict[str, DelegationResult] = {}

    def bind_coordinator(self, coordinator: DelegationSubmissionPort) -> None:
        """在组合根完成 Engine 装配后绑定唯一的目标 Run 协调器。"""
        if self._coordinator is not None:
            raise RuntimeError("DelegationPort 已绑定协调器，禁止重复装配")
        self._coordinator = coordinator

    async def submit(self, request: DelegationRequest) -> str:
        """创建目标 Session 并等待其子 Run 产生标准化终态结果。"""
        identity = self._agent_registry.get(request.target_agent_id)
        if identity is None:
            raise ValueError(f"未找到 delegation target Agent {request.target_agent_id}")
        session = await self._session_manager.create(
            title=f"委托-{identity.agent_name}",
            model=identity.model,
            agent_id=identity.agent_id,
        )
        child_request: RunRequest = RunRequest(
            session_id=session.id,
            lease_id=f"delegation-{uuid.uuid4().hex}",
            agent_id=identity.agent_id,
            user_message=ConversationMessage(
                message_id=f"delegation-input-{uuid.uuid4().hex}",
                role=request.input_message.role,
                content=request.input_message.content,
                created_at=request.input_message.created_at,
            ),
            conversation=ConversationSnapshot(session.id, (), 0),
            parent_run_id=request.parent_run_id,
            root_run_id=request.root_run_id,
        )
        coordinator: DelegationSubmissionPort = self._require_coordinator()
        result: RunResult = await coordinator.submit(child_request)
        self._results[result.run_id] = _to_delegation_result(result)
        return result.run_id

    async def result(self, child_run_id: str) -> DelegationResult | None:
        """返回已完成子 Run 的标准化结果；未知标识表示尚无结果。"""
        return self._results.get(child_run_id)

    async def cancel(self, child_run_id: str) -> None:
        """向协调器提交取消，并保留既有结果缓存用于审计查询。"""
        coordinator: DelegationSubmissionPort = self._require_coordinator()
        await coordinator.cancel(child_run_id, "父运行取消 delegation")

    def _require_coordinator(self) -> DelegationSubmissionPort:
        """保证适配器只在组合根完成双向装配后参与运行。"""
        if self._coordinator is None:
            raise RuntimeError("DelegationPort 尚未绑定 SessionRunCoordinator")
        return self._coordinator


def _to_delegation_result(result: RunResult) -> DelegationResult:
    """将目标 RunResult 转换为父运行只需了解的 delegation 摘要。"""
    output: str = result.final_message.content if result.final_message is not None else ""
    return DelegationResult(
        child_run_id=result.run_id,
        status=result.status,
        output=output,
        error=result.error,
    )
