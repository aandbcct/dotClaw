"""注册固定的助理业务工具并适配现有 ToolHandler 接口。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Awaitable, Callable, Generic, TypeVar

from pydantic import BaseModel

from ..common.tool_replay import ToolExecutionEvidence, ToolReplayPolicy, ToolSideEffect
from ..personal_tasks.repository import PersonalTaskStorageError
from ..assistant.models import AssistantError, AssistantOperationResult
from ..assistant.schemas import (
    BatchApplyInput,
    BatchPrepareInput,
    ReminderCreateInput,
    ReminderStatusInput,
    ReminderUpdateInput,
    RequestAbandonInput,
    RequestGetInput,
    RequestListInput,
    RequestSaveInput,
    ResultSetGetInput,
    TaskCreateInput,
    TaskGetInput,
    TaskQueryInput,
    TaskStatusInput,
    TaskUpdateInput,
)
from ..assistant.service import (
    ASSISTANT_AGENT_ID,
    ASSISTANT_SESSION_ID,
    AssistantApprovalRequiredError,
    AssistantConfigurationError,
    AssistantRequestSourceError,
    AssistantService,
)
from .base import (
    ToolDefinition,
    ToolErrorCode,
    ToolErrorType,
    ToolExecutionContext,
    ToolResult,
    ToolSource,
)
from .decorator import ToolPolicy
from .handler import ToolHandler
from .registry import ToolRegistry
from .schema import ToolValidationError, to_json_schema, validate_args
from ..assistant.sqlite_repository import AssistantRequestConflict


InputT = TypeVar("InputT", bound=BaseModel)
AssistantCall = Callable[[InputT, ToolExecutionContext], Awaitable[AssistantOperationResult]]

_TOOL_OPERATIONS = {
    "builtin.assistant.tasks.query": "tasks.query",
    "builtin.assistant.tasks.get": "tasks.get",
    "builtin.assistant.tasks.create": "tasks.create",
    "builtin.assistant.tasks.update": "tasks.update",
    "builtin.assistant.tasks.status": "tasks.status",
    "builtin.assistant.reminders.create": "reminders.create",
    "builtin.assistant.reminders.update": "reminders.update",
    "builtin.assistant.reminders.status": "reminders.status",
    "builtin.assistant.requests.list": "requests.list",
    "builtin.assistant.requests.get": "requests.get",
    "builtin.assistant.requests.save": "requests.save",
    "builtin.assistant.requests.abandon": "requests.abandon",
    "builtin.assistant.results.get": "results.get",
    "builtin.assistant.batch.prepare": "batch.prepare",
    "builtin.assistant.batch.apply": "batch.apply",
}


class AssistantToolHandler(ToolHandler, Generic[InputT]):
    """把一个严格入参模型和固定 AssistantService 方法接入工具执行器。"""

    def __init__(
        self,
        *,
        service: AssistantService,
        name: str,
        description: str,
        args_model: type[InputT],
        policy: ToolPolicy,
        call: AssistantCall[InputT],
        needs_approval: bool = False,
        replay_policy: ToolReplayPolicy = ToolReplayPolicy.KEYED_IDEMPOTENT,
        side_effect: ToolSideEffect = ToolSideEffect.PRESENT,
    ) -> None:
        self._name = name
        self._description = description
        self._args_model = args_model
        self._policy = policy
        self._call = call
        self._needs_approval = needs_approval
        self._replay_policy = replay_policy
        self._side_effect = side_effect
        self._database_path = str(Path(service._db_path).expanduser().resolve())

    @property
    def args_model(self) -> type[InputT]:
        return self._args_model

    def definition(self) -> ToolDefinition:
        operation = _TOOL_OPERATIONS[self._name]
        return ToolDefinition(
            name=self._name,
            description=self._description,
            parameters=to_json_schema(self._args_model),
            source=ToolSource.BUILTIN,
            needs_approval=self._needs_approval,
            metadata={
                "assistant_operation": operation,
                "assistant_database_path": self._database_path,
                "assistant_session_id": ASSISTANT_SESSION_ID,
            },
            policy_profile=self._policy.value,
            replay_policy=self._replay_policy,
            replay_contract_version="assistant-v1",
            side_effect=self._side_effect,
            recovery_max_retries=1 if self._replay_policy is ToolReplayPolicy.KEYED_IDEMPOTENT else 0,
        )

    async def execute(
        self,
        arguments: Any,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        if context is None:
            return self._failure(
                "configuration_error",
                "助理工具必须由固定身份的 Runtime 上下文调用",
                ToolErrorCode.CONFIGURATION_ERROR,
                ToolErrorType.CONFIGURATION,
            )
        if context.agent_id != ASSISTANT_AGENT_ID or context.session_id != ASSISTANT_SESSION_ID:
            return self._failure(
                "configuration_error",
                "助理工具只能在固定助理身份和会话中运行",
                ToolErrorCode.CONFIGURATION_ERROR,
                ToolErrorType.CONFIGURATION,
            )
        try:
            validated = arguments if isinstance(arguments, self._args_model) else validate_args(self._args_model, arguments)
        except ToolValidationError as error:
            return self._failure(
                "invalid_arguments",
                str(error),
                ToolErrorCode.INVALID_ARGUMENTS,
                ToolErrorType.VALIDATION,
            )

        try:
            result = await self._call(validated, context)
        except AssistantApprovalRequiredError as error:
            return self._failure(
                "approval_required",
                str(error),
                ToolErrorCode.APPROVAL_DENIED,
                ToolErrorType.APPROVAL,
            )
        except AssistantConfigurationError as error:
            return self._failure(
                "configuration_error",
                str(error),
                ToolErrorCode.CONFIGURATION_ERROR,
                ToolErrorType.CONFIGURATION,
            )
        except AssistantRequestSourceError as error:
            return self._failure(
                "invalid_arguments",
                str(error),
                ToolErrorCode.INVALID_ARGUMENTS,
                ToolErrorType.VALIDATION,
            )
        except AssistantRequestConflict as error:
            return self._failure(
                "request_conflict",
                str(error),
                ToolErrorCode.EXECUTION_ERROR,
                ToolErrorType.EXECUTION,
            )
        except PersonalTaskStorageError as error:
            return self._failure(
                "execution_error",
                str(error),
                ToolErrorCode.EXECUTION_ERROR,
                ToolErrorType.EXECUTION,
                evidence=ToolExecutionEvidence.UNKNOWN,
            )

        output = _result_json(result)
        if result.outcome == "failed":
            error = result.error
            code = error.code if error is not None else "execution_error"
            tool_code, error_type = _tool_error(code)
            return ToolResult(
                output=output,
                is_error=True,
                error_code=tool_code.value,
                error_type=error_type.value,
                execution_evidence=ToolExecutionEvidence.RETURNED,
            )
        return ToolResult(output=output, execution_evidence=ToolExecutionEvidence.RETURNED)

    def _failure(
        self,
        code: str,
        message: str,
        tool_code: ToolErrorCode,
        error_type: ToolErrorType,
        *,
        evidence: ToolExecutionEvidence = ToolExecutionEvidence.NOT_STARTED,
    ) -> ToolResult:
        result = AssistantOperationResult(
            operation=_TOOL_OPERATIONS[self._name],
            outcome="failed",
            error=AssistantError(code=code, message=message),
        )
        return ToolResult(
            output=_result_json(result),
            is_error=True,
            error_code=tool_code.value,
            error_type=error_type.value,
            execution_evidence=evidence,
        )


def _result_json(result: AssistantOperationResult) -> str:
    return json.dumps(result.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _tool_error(code: str) -> tuple[ToolErrorCode, ToolErrorType]:
    if code in {"invalid_arguments", "invalid_time"}:
        return ToolErrorCode.INVALID_ARGUMENTS, ToolErrorType.VALIDATION
    if code == "not_found":
        return ToolErrorCode.EXECUTION_ERROR, ToolErrorType.NOT_FOUND
    return ToolErrorCode.EXECUTION_ERROR, ToolErrorType.EXECUTION


def register_assistant_tools(registry: ToolRegistry, service: AssistantService) -> None:
    """把十五个固定助理工具注册到现有 Registry。"""
    read = ToolPolicy.ASSISTANT_READ
    write = ToolPolicy.ASSISTANT_WRITE

    def register[RegisteredInput: BaseModel](
        name: str,
        description: str,
        args_model: type[RegisteredInput],
        policy: ToolPolicy,
        call: AssistantCall[RegisteredInput],
        *,
        needs_approval: bool = False,
        replay_policy: ToolReplayPolicy = ToolReplayPolicy.KEYED_IDEMPOTENT,
        side_effect: ToolSideEffect = ToolSideEffect.PRESENT,
    ) -> None:
        handler = AssistantToolHandler(
            service=service,
            name=name,
            description=description,
            args_model=args_model,
            policy=policy,
            call=call,
            needs_approval=needs_approval,
            replay_policy=replay_policy,
            side_effect=side_effect,
        )
        registry.register(handler)

    safe_retry = ToolReplayPolicy.SAFE_RETRY
    no_effect = ToolSideEffect.NONE
    register("builtin.assistant.tasks.query", "按条件查询个人事务并保留有序结果快照。", TaskQueryInput, read, service.query_tasks)
    register("builtin.assistant.tasks.get", "读取指定事务与全部提醒详情。", TaskGetInput, read, service.get_task)
    register("builtin.assistant.tasks.create", "创建个人事务及其初始提醒。", TaskCreateInput, write, service.create_task)
    register("builtin.assistant.tasks.update", "按指定字段局部修改个人事务。", TaskUpdateInput, write, service.update_task)
    register("builtin.assistant.tasks.status", "完成或取消个人事务。", TaskStatusInput, write, service.change_task_status)
    register("builtin.assistant.reminders.create", "为个人事务创建提醒。", ReminderCreateInput, write, service.create_reminder)
    register("builtin.assistant.reminders.update", "修改指定提醒规则。", ReminderUpdateInput, write, service.update_reminder)
    register("builtin.assistant.reminders.status", "暂停、恢复或取消指定提醒。", ReminderStatusInput, write, service.change_reminder_status)
    register("builtin.assistant.requests.list", "分页读取当前助理会话的未结请求。", RequestListInput, read, service.list_requests, replay_policy=safe_retry, side_effect=no_effect)
    register("builtin.assistant.requests.get", "读取指定助理请求及来源。", RequestGetInput, read, service.get_request, replay_policy=safe_retry, side_effect=no_effect)
    register("builtin.assistant.requests.save", "保存或更新等待澄清的请求草案。", RequestSaveInput, write, service.save_request)
    register("builtin.assistant.requests.abandon", "放弃等待处理的请求草案。", RequestAbandonInput, write, service.abandon_request)
    register("builtin.assistant.results.get", "读取已保存的有序查询结果快照。", ResultSetGetInput, read, service.get_result_set, replay_policy=safe_retry, side_effect=no_effect)
    register("builtin.assistant.batch.prepare", "保存事务与提醒批量变更提案，等待确认。", BatchPrepareInput, write, service.prepare_batch)
    register("builtin.assistant.batch.apply", "执行已保存且获批准的批量变更提案。", BatchApplyInput, write, service.apply_batch, needs_approval=True)
