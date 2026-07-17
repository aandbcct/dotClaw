"""将 Runtime v2 的 DelegationPort 请求映射为目标 Agent 的子 Run。"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Protocol

from ..runtime.application.dto import (
    ConversationMessage,
    ConversationSnapshot,
    DelegationRequest,
    DelegationResult,
    RunRequest,
    RunResult,
)
from ..runtime.application.ports import DelegationPort
from ..runtime.domain.facts import RunError, RunErrorCode, RunStatus
from ..session.session import SessionManager
from .dispatcher import AgentDispatcher
from .registry import AgentRegistry
from .task import TaskSpecification


class DelegationSubmissionPort(Protocol):
    """适配器提交和取消目标 Run 所需的最小协调接口。"""

    async def submit(self, request: RunRequest) -> RunResult:
        """串行提交目标 Session 的普通 Run。"""

    async def cancel(self, run_id: str, reason: str) -> None:
        """取消已创建的目标 Run。"""


@dataclass(frozen=True)
class DelegationTaskBinding:
    """子 Run 与既有 Dispatcher Task 的最小关联，供完成和取消回调使用。"""

    task_id: str
    source_agent_id: str
    source_session_id: str


class RuntimeDelegationAdapter(DelegationPort):
    """将 orchestration 的目标 Session 调度适配为 Runtime v2 委派端口。

    适配器拥有目标 Session 的创建、子 Run 提交和取消转发；RuntimeEngine 只接收
    子运行标识与标准化结果，既不依赖 Dispatcher，也不接触旧 Runtime。
    """

    def __init__(
        self,
        session_manager: SessionManager,
        agent_registry: AgentRegistry,
        dispatcher: AgentDispatcher,
    ) -> None:
        """绑定目标 Run 的会话仓储与 Agent 身份目录。"""
        self._coordinator: DelegationSubmissionPort | None = None
        self._session_manager: SessionManager = session_manager
        self._agent_registry: AgentRegistry = agent_registry
        self._dispatcher: AgentDispatcher = dispatcher
        self._results: dict[str, DelegationResult] = {}
        self._running: dict[str, asyncio.Task[RunResult]] = {}
        self._task_bindings: dict[str, DelegationTaskBinding] = {}

    def bind_coordinator(self, coordinator: DelegationSubmissionPort) -> None:
        """在组合根完成 Engine 装配后绑定唯一的目标 Run 协调器。"""
        if self._coordinator is not None:
            raise RuntimeError("DelegationPort 已绑定协调器，禁止重复装配")
        self._coordinator = coordinator

    async def submit(self, request: DelegationRequest) -> str:
        """创建目标 Session 后异步提交子 Run，并立即返回稳定运行标识。"""
        if not request.source_agent_id or not request.source_session_id:
            raise ValueError("delegation 请求必须包含来源 Agent 与 Session")
        identity = self._agent_registry.get(request.target_agent_id)
        if identity is None:
            raise ValueError(f"未找到 delegation target Agent {request.target_agent_id}")
        session = await self._session_manager.create(
            title=f"委托-{identity.agent_name}",
            model=identity.model,
            agent_id=identity.agent_id,
        )
        task = await self._dispatcher.start_v2_delegation(
            request.source_agent_id,
            request.source_session_id,
            request.parent_run_id,
            identity.agent_id,
            session.id,
            _task_specification(request),
        )
        child_run_id: str = uuid.uuid4().hex
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
            run_id=child_run_id,
        )
        coordinator: DelegationSubmissionPort = self._require_coordinator()
        execution: asyncio.Task[RunResult] = asyncio.create_task(coordinator.submit(child_request))
        self._running[child_run_id] = execution
        self._task_bindings[child_run_id] = DelegationTaskBinding(
            task.task_id,
            request.source_agent_id,
            request.source_session_id,
        )
        # 让子协程先完成 Run 注册，再向父 Engine 返回可取消的子运行标识。
        await asyncio.sleep(0)
        return child_run_id

    async def result(self, child_run_id: str) -> DelegationResult | None:
        """等待并返回标准化子运行结果；未知标识表示尚无结果。"""
        cached: DelegationResult | None = self._results.get(child_run_id)
        if cached is not None:
            return cached
        execution: asyncio.Task[RunResult] | None = self._running.get(child_run_id)
        if execution is None:
            return None
        try:
            result: RunResult = await execution
            delegated_result: DelegationResult = _to_delegation_result(result)
        except asyncio.CancelledError:
            delegated_result = DelegationResult(
                child_run_id,
                RunStatus.CANCELLED,
                error=RunError(RunErrorCode.CANCELLED, "delegation 子运行已取消"),
            )
        except Exception as error:
            delegated_result = DelegationResult(
                child_run_id,
                RunStatus.FAILED,
                error=RunError(RunErrorCode.TOOL_FAILURE, f"delegation 子运行异常：{error}"),
            )
        self._running.pop(child_run_id, None)
        self._results[child_run_id] = delegated_result
        await self._finish_task(child_run_id, delegated_result)
        return delegated_result

    async def cancel(self, child_run_id: str) -> None:
        """向协调器提交取消，并保留既有结果缓存用于审计查询。"""
        coordinator: DelegationSubmissionPort = self._require_coordinator()
        binding: DelegationTaskBinding | None = self._task_bindings.get(child_run_id)
        if binding is not None:
            await self._dispatcher.cancel_task(
                binding.task_id,
                binding.source_agent_id,
                binding.source_session_id,
                child_run_id,
                "父运行取消 delegation",
            )
        await coordinator.cancel(child_run_id, "父运行取消 delegation")

    def _require_coordinator(self) -> DelegationSubmissionPort:
        """保证适配器只在组合根完成双向装配后参与运行。"""
        if self._coordinator is None:
            raise RuntimeError("DelegationPort 尚未绑定 SessionRunCoordinator")
        return self._coordinator

    async def _finish_task(self, child_run_id: str, result: DelegationResult) -> None:
        """将子运行终态投影到 Dispatcher，保持 Task 业务状态由 orchestration 管理。"""
        binding: DelegationTaskBinding | None = self._task_bindings.pop(child_run_id, None)
        if binding is None:
            return
        output: str = result.output or (result.error.message if result.error is not None else "delegation 未返回输出")
        await self._dispatcher.finish_v2_delegation(
            binding.task_id,
            child_run_id,
            output,
            result.status is RunStatus.COMPLETED,
        )


def _to_delegation_result(result: RunResult) -> DelegationResult:
    """将目标 RunResult 转换为父运行只需了解的 delegation 摘要。"""
    output: str = result.final_message.content if result.final_message is not None else ""
    return DelegationResult(
        child_run_id=result.run_id,
        status=result.status,
        output=output,
        error=result.error,
    )


def _task_specification(request: DelegationRequest) -> TaskSpecification:
    """将 Runtime 的最小输入消息包装为既有 Task 状态机需要的不可变契约。"""
    return TaskSpecification(
        title="Runtime v2 delegation",
        objective=request.input_message.content,
    )
