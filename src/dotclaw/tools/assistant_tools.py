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
    PlanSaveInput,
    PlanApplyInput,
)
from ..assistant.service import (
    ASSISTANT_AGENT_ID,
    ASSISTANT_SESSION_ID,
    ASSISTANT_TOOL_OPERATIONS,
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
        replay_policy: ToolReplayPolicy = ToolReplayPolicy.KEYED_IDEMPOTENT,
        side_effect: ToolSideEffect = ToolSideEffect.PRESENT,
    ) -> None:
        self._name = name
        self._description = description
        self._args_model = args_model
        self._policy = policy
        self._call = call
        self._replay_policy = replay_policy
        self._side_effect = side_effect
        self._database_path = str(Path(service._db_path).expanduser().resolve())

    @property
    def args_model(self) -> type[InputT]:
        return self._args_model

    def definition(self) -> ToolDefinition:
        operation = ASSISTANT_TOOL_OPERATIONS[self._name]
        return ToolDefinition(
            name=self._name,
            description=self._description,
            parameters=to_json_schema(self._args_model),
            source=ToolSource.BUILTIN,
            needs_approval=False,
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
            operation=ASSISTANT_TOOL_OPERATIONS[self._name],
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
    """把固定助理工具注册到现有 Registry。"""
    read = ToolPolicy.ASSISTANT_READ
    write = ToolPolicy.ASSISTANT_WRITE

    def register[RegisteredInput: BaseModel](
        name: str,
        description: str,
        args_model: type[RegisteredInput],
        policy: ToolPolicy,
        call: AssistantCall[RegisteredInput],
        *,
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
            replay_policy=replay_policy,
            side_effect=side_effect,
        )
        registry.register(handler)

    safe_retry = ToolReplayPolicy.SAFE_RETRY
    no_effect = ToolSideEffect.NONE
    register("builtin.assistant.tasks.query", "按条件查询事务并保留有序结果快照。记录已明确的个人事件前，先用此工具按关键词查重；可使用 filter=all 与 plan_filter=all。plan_filter 另可筛 unscheduled、scheduled、ended；查询 ended 时必须设置 filter=all 才包含已结束事务。", TaskQueryInput, read, service.query_tasks)
    register("builtin.assistant.tasks.get", "读取指定事务、安排和全部提醒详情。planning 内 datetime 按业务时区返回带显式偏移的本地 RFC3339；date 仍是日期，不补午夜。更新前先读取真实事务 ID、version 和完整 planning。", TaskGetInput, read, service.get_task)
    register("builtin.assistant.tasks.create", "创建个人事务及其初始提醒。明确的本人面试、考试等事件先查询去重，再用 planning={\"kind\":\"event\",\"event_time\":{\"kind\":\"date\",\"value\":\"YYYY-MM-DD\"},\"time_text\":\"晚上\"} 记录；已能确定的日期从服务端输入时间和业务时区推导，模糊时段保留原文，不猜钟点，也不要因缺钟点推迟登记。创建成功后用返回的真实事务 ID 填入 plans.save.event_ids。只有普通非规划请求才可传 request_id/expected_request_version；规划事件必须独立创建，不传规划 request_id。", TaskCreateInput, write, service.create_task)
    register("builtin.assistant.tasks.update", "按指定字段局部修改个人事务。修改已有事件先 tasks.get 并使用真实 ID/version；planning 是完整安排对象，须保留未变字段。精确时刻直接传业务当地 RFC3339 显式偏移（Asia/Shanghai 用 +08:00），不要转换为 Z；event_time=null 可清除事件时间。成功后按回执核对新 version 和本地时间，再报告变更。规划事件必须独立更新，不要传规划 request_id；计划变更使用 plans.save。普通非规划请求才可通过 request_id/expected_request_version 关联。", TaskUpdateInput, write, service.update_task)
    register("builtin.assistant.tasks.status", "仅完成或取消事务状态，不会移除安排时间。用户同时要求取消事件并移除时间时，先 tasks.get，再 tasks.update 提交完整 planning（event_time=null，保留其余安排字段），再用 update 回执的新 version 调用本工具取消，并核对两个成功回执。", TaskStatusInput, write, service.change_task_status)
    register("builtin.assistant.reminders.create", "为个人事务创建提醒。", ReminderCreateInput, write, service.create_reminder)
    register("builtin.assistant.reminders.update", "修改指定提醒规则。", ReminderUpdateInput, write, service.update_reminder)
    register("builtin.assistant.reminders.status", "暂停、恢复或取消指定提醒。", ReminderStatusInput, write, service.change_reminder_status)
    register("builtin.assistant.requests.list", "分页读取当前助理会话的未结请求。", RequestListInput, read, service.list_requests, replay_policy=safe_retry, side_effect=no_effect)
    register("builtin.assistant.requests.get", "读取指定助理请求及来源。", RequestGetInput, read, service.get_request, replay_policy=safe_retry, side_effect=no_effect)
    register("builtin.assistant.requests.save", "保存或更新等待澄清的请求草案。payload必须包含intent_args、missing_fields、candidate_task_ids、selected_task_ids和temporal_values；没有已解析时间时在payload.temporal_values传[]。source_refs可传[]，由服务自动关联当前用户输入，不要自行填写运行或消息ID。", RequestSaveInput, write, service.save_request)
    register("builtin.assistant.requests.abandon", "放弃等待处理的请求草案。", RequestAbandonInput, write, service.abandon_request)
    register("builtin.assistant.results.get", "读取已保存的有序查询结果快照。", ResultSetGetInput, read, service.get_result_set, replay_policy=safe_retry, side_effect=no_effect)
    register("builtin.assistant.batch.prepare", "准备并保存批量提案。若关联已有请求，request_id 与 expected_request_version 必须同时提供；expected_request_version 取自 requests.get 返回的 data.request.version。随后用返回的顶层 request_id、data.request.version、data.request.proposal_hash 调用 batch.apply，分别填入 request_id、expected_version、proposal_hash；apply 会自动执行当前有效提案，不请求结构化审批。", BatchPrepareInput, write, service.prepare_batch)
    register("builtin.assistant.batch.apply", "自动执行已保存的当前批量提案；使用 batch.prepare 返回的顶层 request_id 与 data.request.version、data.request.proposal_hash，分别填入 request_id、expected_version、proposal_hash。", BatchApplyInput, write, service.apply_batch)
    register("builtin.assistant.plans.save", "保存或更新个人计划草案，不创建正式事务或提醒。先用 tasks.create 登记明确的本人事件，再把成功回执中的真实事件 ID 放入 event_ids；items 只放准备任务，不能用任务 description 代替事件登记。每个 item 必须提供 planning 对象；已安排日期/时段写 planning.scheduled_time（开始时刻用业务本地带偏移RFC3339），确未安排才显式设 scheduled_time=null；不能只在 description 描述排期。event_ids 非空且 stage=proposed 时，每项 planning.related_task_id 都必须指向 event_ids 中的事件。新草案结果顶层含 request_id，data.request.id 与 data.request.version；更新前调用 requests.get 取最新 data.request.id 和 data.request.version，分别填入 request_id、expected_version；事务 version 不可代替计划 version。", PlanSaveInput, write, service.save_plan)
    register("builtin.assistant.plans.apply", "仅执行当前版本且 stage=proposed、没有 missing_fields 的已保存计划。只有用户在当前对话明确接受该版本后才调用；将 plans.save 或 requests.get 返回的 data.request.id 与 data.request.version 填入 request_id、expected_version。", PlanApplyInput, write, service.apply_plan)
