"""Task delegation 的模型工具。

模块作用：把模型的 delegation 意图转换为 Dispatcher 的窄接口调用。工具层只从
``ToolExecutionContext`` 推导当前端点，绝不接受可伪造的 Identity 或 Session 参数。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from dotclaw.orchestration.task import Task, TaskEndpoint, TaskMessageType, TaskSpecification
from dotclaw.tools.handler import BuiltinToolHandler

if TYPE_CHECKING:
    from dotclaw.agent.agent import Agent
    from dotclaw.tools.base import ToolExecutionContext


def get_task_handlers() -> list[BuiltinToolHandler]:
    """创建 delegation MVP 的唯一工具集合。"""
    return [
        _delegate_handler(),
        _send_message_handler(),
        _wait_task_handler(),
        _status_handler(),
        _cancel_handler(),
    ]


def _delegate_handler() -> BuiltinToolHandler:
    """创建 source 端 delegate 工具。"""
    async def handle(target_agent_id: str, title: str, objective: str, materials: list[str] | None = None, constraints: list[str] | None = None, expected_deliverables: list[str] | None = None, _context: "ToolExecutionContext | None" = None) -> str:
        agent, session_id, run_id = _resolve_context(_context)
        specification: TaskSpecification = TaskSpecification(
            title=title,
            objective=objective,
            materials=list(materials or []),
            constraints=list(constraints or []),
            expected_deliverables=list(expected_deliverables or []),
        )
        task: Task = await agent.dispatcher.delegate(agent.runtime, agent.agent_id, session_id, run_id, target_agent_id, specification)
        return _task_json(task)
    return BuiltinToolHandler("delegate", "把一项任务委托给目标 Identity，并创建独立 target Session。", {"type": "object", "properties": {"target_agent_id": {"type": "string"}, "title": {"type": "string"}, "objective": {"type": "string"}, "materials": {"type": "array", "items": {"type": "string"}}, "constraints": {"type": "array", "items": {"type": "string"}}, "expected_deliverables": {"type": "array", "items": {"type": "string"}}}, "required": ["target_agent_id", "title", "objective"]}, handle, timeout=10.0)


def _send_message_handler() -> BuiltinToolHandler:
    """创建双端共用的 task_send_message 工具。"""
    async def handle(task_id: str, message_type: str, payload: str, _context: "ToolExecutionContext | None" = None) -> str:
        agent, session_id, run_id = _resolve_context(_context)
        task: Task = await agent.dispatcher.broker.get_task(task_id)
        endpoint: TaskEndpoint = _resolve_endpoint(task, agent.agent_id, session_id)
        kind: TaskMessageType = TaskMessageType(message_type)
        message = await agent.dispatcher.send_message(task_id, endpoint, agent.agent_id, session_id, run_id, kind, payload)
        return json.dumps({"task_id": task_id, "sequence": message.sequence, "status": (await agent.dispatcher.broker.get_task(task_id)).status.value}, ensure_ascii=False)
    return BuiltinToolHandler("task_send_message", "向当前 Task 的对端发送受状态机约束的消息。", {"type": "object", "properties": {"task_id": {"type": "string"}, "message_type": {"type": "string", "enum": [item.value for item in TaskMessageType]}, "payload": {"type": "string"}}, "required": ["task_id", "message_type", "payload"]}, handle, timeout=10.0)


def _wait_task_handler() -> BuiltinToolHandler:
    """创建等待当前端点入站消息的工具。"""
    async def handle(task_id: str, timeout: float = 60.0, _context: "ToolExecutionContext | None" = None) -> str:
        agent, session_id, _ = _resolve_context(_context)
        task: Task = await agent.dispatcher.broker.get_task(task_id)
        endpoint: TaskEndpoint = _resolve_endpoint(task, agent.agent_id, session_id)
        result = await agent.dispatcher.wait_task(task_id, endpoint, agent.agent_id, session_id, timeout)
        return json.dumps({"task_id": task_id, "status": result.task.status.value, "timed_out": result.timed_out, "messages": [_message_json(message) for message in result.messages]}, ensure_ascii=False)
    return BuiltinToolHandler("wait_task", "等待当前端点的新 Task 消息或终态；超时不会取消 Task。", {"type": "object", "properties": {"task_id": {"type": "string"}, "timeout": {"type": "number", "minimum": 0}}, "required": ["task_id"]}, handle, timeout=65.0)


def _status_handler() -> BuiltinToolHandler:
    """创建 Task 状态查询工具。"""
    async def handle(task_id: str, _context: "ToolExecutionContext | None" = None) -> str:
        agent, session_id, _ = _resolve_context(_context)
        task: Task = await agent.dispatcher.broker.get_task(task_id)
        endpoint: TaskEndpoint = _resolve_endpoint(task, agent.agent_id, session_id)
        checked: Task = await agent.dispatcher.task_status(task_id, endpoint, agent.agent_id, session_id)
        return _task_json(checked)
    return BuiltinToolHandler("task_status", "查询当前端点有权访问的 Task 状态。", {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}, handle, timeout=5.0)


def _cancel_handler() -> BuiltinToolHandler:
    """创建 source 端取消工具。"""
    async def handle(task_id: str, reason: str = "source 已取消任务", _context: "ToolExecutionContext | None" = None) -> str:
        agent, session_id, run_id = _resolve_context(_context)
        task: Task = await agent.dispatcher.cancel_task(task_id, agent.agent_id, session_id, run_id, reason)
        return _task_json(task)
    return BuiltinToolHandler("cancel_task", "取消当前 source Session 创建的活动 Task。", {"type": "object", "properties": {"task_id": {"type": "string"}, "reason": {"type": "string"}}, "required": ["task_id"]}, handle, timeout=10.0)


def _resolve_context(context: "ToolExecutionContext | None") -> tuple["Agent", str, str]:
    """从 Runtime 注入的上下文解析当前 Agent、Session 和 Run。"""
    if context is None:
        raise RuntimeError("Task 工具必须在 Runtime 执行上下文中调用")
    from dotclaw.agent.agent import Agent
    if not isinstance(context.agent, Agent) or not context.session_id:
        raise RuntimeError("Task 工具缺少当前 Agent 或 Session")
    if context.agent.dispatcher is None or context.agent.runtime is None:
        raise RuntimeError("当前 Agent 未装配 delegation Dispatcher")
    return context.agent, context.session_id, context.agentrun_id


def _resolve_endpoint(task: Task, agent_id: str, session_id: str) -> TaskEndpoint:
    """根据双重绑定识别调用者在该 Task 中的端点。"""
    if task.source.identity_id == agent_id and task.source.session_id == session_id:
        return TaskEndpoint.SOURCE
    if task.target.identity_id == agent_id and task.target.session_id == session_id:
        return TaskEndpoint.TARGET
    raise PermissionError("当前 Identity 或 Session 不属于该 Task")


def _task_json(task: Task) -> str:
    """序列化不泄露对端内部上下文的状态视图。"""
    return json.dumps({"task_id": task.task_id, "status": task.status.value, "target_session_id": task.target.session_id, "target_identity_id": task.target.identity_id, "error": task.error}, ensure_ascii=False)


def _message_json(message: "TaskMessage") -> dict[str, str | int]:
    """序列化单条可消费消息。"""
    return {"sequence": message.sequence, "type": message.message_type.value, "payload": message.payload, "sender": message.sender.value}


from dotclaw.orchestration.task import TaskMessage
