"""以持久父子运行关系准备、启动和投影同进程委派。"""

from __future__ import annotations

import json
import uuid
from typing import Protocol

from ..runtime.application.dto import ConversationMessage, ConversationSnapshot, DelegationRequest, DelegationResult, DelegationSubmission, RunRequest, RunResult
from ..runtime.application.ports import DelegationPort, RunRepository
from ..runtime.domain.cancellation import CancellationOrigin, CancellationReceipt
from ..runtime.domain.facts import AgentRun, MessageRole
from ..runtime.domain.state import Created, Running, Suspended
from ..session.session import SessionManager
from .dispatcher import AgentDispatcher
from .registry import AgentRegistry
from .task import TaskSpecification


class DelegationSubmissionPort(Protocol):
    """子运行协调协议，不向适配器泄漏能力任务句柄。"""

    async def _prepare_child_run_locked(self, request: RunRequest) -> str: ...
    def schedule_prepared(self, run_id: str) -> None: ...
    def schedule_resume(self, run_id: str) -> None: ...
    async def wait_terminal(self, run_id: str) -> RunResult: ...
    async def cancel(self, run_id: str, reason: str, *, origin: CancellationOrigin = CancellationOrigin.USER, parent_request_id: str | None = None) -> CancellationReceipt: ...


class RuntimeDelegationAdapter(DelegationPort):
    """只持有仓储和业务投影依赖，不保存子驱动或第二份结果缓存。"""

    def __init__(self, session_manager: SessionManager, agent_registry: AgentRegistry,
                 dispatcher: AgentDispatcher, preferred_model: str, *, run_repository: RunRepository) -> None:
        self._coordinator: DelegationSubmissionPort | None = None
        self._session_manager = session_manager
        self._agent_registry = agent_registry
        self._dispatcher = dispatcher
        self._preferred_model = preferred_model
        self._run_repository = run_repository

    def bind_coordinator(self, coordinator: DelegationSubmissionPort) -> None:
        if self._coordinator is not None:
            raise RuntimeError("委派协调器不可重复绑定")
        self._coordinator = coordinator

    async def prepare(self, request: DelegationRequest) -> DelegationSubmission:
        """先保存 Created 子运行；相同工具调用重试使用相同子标识。"""
        if request.input_message.role is not MessageRole.USER:
            raise ValueError("委派输入必须为用户消息契约")
        parent = await self._run_repository.find_run(request.parent_run_id)
        if parent is None or (parent.agent_id, parent.session_id) != (request.source_agent_id, request.source_session_id):
            raise ValueError("委派来源与持久父运行不一致")
        if request.root_run_id != (parent.root_run_id or parent.run_id):
            raise ValueError("委派根运行不一致")
        identity = self._agent_registry.get(request.target_agent_id)
        if identity is None:
            raise ValueError("委派目标 Agent 不存在")
        key = json.dumps(["dotclaw-delegation-v1", request.parent_run_id, request.source_tool_call_id or request.input_message.message_id], ensure_ascii=False)
        child_id = uuid.uuid5(uuid.NAMESPACE_URL, key).hex
        existing = await self._run_repository.find_run(child_id)
        if existing is not None:
            messages = await self._run_repository.load_messages(existing.session_id, child_id)
            initial = next((message for message in messages if message.message_id == existing.input_message_id), None)
            if (existing.parent_run_id, existing.root_run_id, existing.agent_id) != (parent.run_id, request.root_run_id, request.target_agent_id) or initial is None or (initial.role, initial.content) != (request.input_message.role, request.input_message.content) or not existing.delegation_task_id:
                raise ValueError("相同委派标识的目标、来源或输入契约冲突")
            await self._restore_projection(existing)
            return DelegationSubmission(child_id, existing.delegation_task_id, existing.session_id)
        session = await self._session_manager.create(title=f"委托-{identity.agent_name}", model=self._preferred_model, agent_id=identity.agent_id)
        task = None
        try:
            task = await self._dispatcher.start_v2_delegation(parent.agent_id, parent.session_id, parent.run_id,
                identity.agent_id, session.id, TaskSpecification("Runtime v2 delegation", request.input_message.content))
            child_request = RunRequest(session.id, f"delegation-{child_id}", identity.agent_id,
                ConversationMessage(f"delegation-input-{child_id}", request.input_message.role, request.input_message.content, request.input_message.created_at),
                ConversationSnapshot(session.id, (), 0), parent.run_id, request.root_run_id, child_id, self._preferred_model, task.task_id)
            await self._require_coordinator()._prepare_child_run_locked(child_request)
            return DelegationSubmission(child_id, task.task_id, session.id)
        except BaseException:
            child = await self._run_repository.find_run(child_id)
            if child is None:
                if task is not None:
                    await self._dispatcher.fail_v2_preparation(task.task_id, child_id, "准备失败：子运行未创建或启动")
                await self._session_manager.delete(session.id)
            else:
                messages = await self._run_repository.load_messages(child.session_id, child_id)
                initial = next((message for message in messages if message.message_id == child.input_message_id), None)
                if (child.parent_run_id, child.root_run_id, child.agent_id) != (parent.run_id, request.root_run_id, request.target_agent_id) or initial is None or (initial.role, initial.content) != (request.input_message.role, request.input_message.content) or not child.delegation_task_id:
                    raise ValueError("准备失败后发现的稳定子运行与本调用契约冲突")
                await self._restore_projection(child)
                return DelegationSubmission(child_id, child.delegation_task_id, child.session_id)
            raise

    async def start(self, child_run_id: str) -> None:
        """只有协调器创建和持有实际驱动任务。"""
        self._require_coordinator().schedule_prepared(child_run_id)

    async def resume(self, child_run_id: str) -> None:
        """按持久子状态分流调度；挂起等待由原审批/委派入口唤醒。"""
        child = await self._run_repository.find_run(child_run_id)
        if child is None:
            raise ValueError("待恢复委派子运行不存在")
        await self._restore_projection(child)
        coordinator = self._require_coordinator()
        if child.state.is_ended():
            return
        parent = await self._run_repository.find_run(child.parent_run_id) if child.parent_run_id else None
        if parent is not None and await self._run_repository.load_cancellation(parent.session_id, parent.run_id) is not None:
            await coordinator.cancel(child.run_id, "父运行已请求停止", origin=CancellationOrigin.PARENT)
            return
        if await self._run_repository.load_cancellation(child.session_id, child.run_id) is not None:
            await coordinator.cancel(child.run_id, "恢复发现子运行已有取消请求", origin=CancellationOrigin.PARENT)
            return
        if isinstance(child.state.mode, Created):
            coordinator.schedule_prepared(child_run_id)
        elif isinstance(child.state.mode, Running):
            coordinator.schedule_resume(child_run_id)
        elif isinstance(child.state.mode, Suspended):
            return

    async def result(self, child_run_id: str) -> DelegationResult | None:
        """依据持久事实投影，不 await 或取消子驱动。"""
        run = await self._run_repository.find_run(child_run_id)
        if run is None:
            return None
        await self._restore_projection(run)
        if await self._run_repository.has_pending_cancellation_commit(run.session_id, run.run_id) or await self._run_repository.has_pending_success_commit(run.session_id, run.run_id) or not run.state.is_ended():
            return None
        messages = await self._run_repository.load_messages(run.session_id, run.run_id)
        final = next((message for message in messages if message.message_id == run.final_message_id), None)
        output = final.content if final else ""
        if run.delegation_task_id:
            await self._dispatcher.settle_v2_delegation(run.delegation_task_id, run.run_id, run.state.mode.outcome,
                output or (run.error.message if run.error else "委派运行已结束"))
        return DelegationResult(run.run_id, run.state.mode.outcome, output, run.error)

    async def wait_terminal(self, child_run_id: str) -> DelegationResult:
        await self._require_coordinator().wait_terminal(child_run_id)
        result = await self.result(child_run_id)
        if result is None:
            raise RuntimeError("等待完成后未读取到已提交子终态")
        return result

    async def cancel(self, child_run_id: str, reason: str, parent_request_id: str | None) -> CancellationReceipt:
        """只请求子运行停止，业务 Task 在其真实终态后结算。"""
        return await self._require_coordinator().cancel(child_run_id, reason, origin=CancellationOrigin.PARENT, parent_request_id=parent_request_id)

    async def _restore_projection(self, run: AgentRun) -> None:
        if not run.delegation_task_id:
            return
        parent = await self._run_repository.find_run(run.parent_run_id) if run.parent_run_id else None
        if parent is None:
            raise ValueError("委派子运行缺少持久父运行")
        messages = await self._run_repository.load_messages(run.session_id, run.run_id)
        initial = next((message for message in messages if message.message_id == run.input_message_id and message.role is MessageRole.USER), None)
        if initial is None:
            raise ValueError("委派子运行缺少持久输入")
        await self._dispatcher.restore_v2_delegation(run.delegation_task_id, parent.agent_id, parent.session_id,
            run.agent_id, run.session_id, initial.content)

    def _require_coordinator(self) -> DelegationSubmissionPort:
        if self._coordinator is None:
            raise RuntimeError("委派端口未绑定协调器")
        return self._coordinator
