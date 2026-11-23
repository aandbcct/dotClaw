"""助理请求、业务操作及幂等结果的应用服务。"""

import asyncio
from datetime import UTC, date, datetime
from enum import Enum
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Awaitable, Callable, Literal, TypeVar
from uuid import NAMESPACE_URL, uuid4, uuid5
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from ..personal_tasks.models import (
    PersonalTaskFilter,
    PersonalTaskQuery,
    PersonalTaskStatus,
    PersonalTask,
    TaskDeadline,
    PersonalTaskView,
)
from ..personal_tasks.reminder_models import Reminder, ReminderRule, ReminderStatus
from ..personal_tasks.reminder_service import ReminderService
from ..personal_tasks.repository import (
    InvalidReminderInput,
    InvalidReminderTransition,
    PersonalTaskNotFound,
    PersonalTaskVersionConflict,
    ReminderNotFound,
    ReminderVersionConflict,
)
from ..personal_tasks.service import (
    InvalidPersonalTaskInput,
    InvalidPersonalTaskTransition,
    PersonalTaskService,
)
from ..runtime.application.ports import RunRepository
from ..runtime.domain.facts import AgentRun, MessageRole, RunMessageKind
from ..runtime.domain.tool_recovery import ToolCallLedger, ToolCallRecord, ToolCallState
from ..tools.base import ToolExecutionContext
from ..tools.function_handler import _await_io_completion
from .models import (
    AssistantBatchCommand,
    AssistantBatchItemResult,
    AssistantChange,
    AssistantError,
    AssistantInputRef,
    AssistantOperation,
    AssistantOperationData,
    AssistantOperationResult,
    AssistantProposal,
    AssistantProposalItem,
    AssistantProposalPreview,
    AssistantProposalValues,
    AssistantReminderRef,
    AssistantRequest,
    AssistantRequestIntent,
    AssistantRequestPayload,
    AssistantResultItem,
    AssistantResultSet,
    AssistantRunResults,
    AssistantSourceRef,
    AssistantTaskRef,
    ReminderStatusAction,
    ReminderUpdateAction,
    RequestBatchIntent,
    RequestDetailData,
    RequestPageData,
    ResultSetData,
    TaskDetailData,
    TaskPageData,
    TaskPatch as StoredTaskPatch,
    TaskStatusAction,
    TaskUpdateAction,
)
from .schemas import (
    BatchItem,
    BatchPrepareInput,
    BatchApplyInput,
    BatchReminderStatusItem,
    BatchReminderUpdateItem,
    BatchStatusItem,
    BatchUpdateItem,
    ReminderCreateInput,
    ReminderStatusInput,
    ReminderUpdateInput,
    RequestAbandonInput,
    RequestGetInput,
    RequestListInput,
    RequestSaveInput,
    RequestSourceInput,
    ResultSetGetInput,
    TaskCreateInput,
    TaskGetInput,
    TaskQueryInput,
    TaskStatusInput,
    TaskUpdateInput,
)
from .sqlite_repository import (
    AssistantRequestConflict,
    AssistantStorageError,
    SqliteAssistantRepository,
    SqliteAssistantTransaction,
)
from ..personal_tasks.sqlite_repository import SqlitePersonalTaskRepository


ASSISTANT_AGENT_ID = "personal-assistant"
ASSISTANT_SESSION_ID = "personal-assistant"
_RESULT_OPERATION_NAMES = {
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
_T = TypeVar("_T")
_TransactionAction = Callable[
    [sqlite3.Connection, PersonalTaskService, ReminderService, datetime], Awaitable[_T]
]


class AssistantConfigurationError(RuntimeError):
    """助理运行缺少服务端必需的冻结上下文或幂等键。"""


class AssistantRequestSourceError(RuntimeError):
    """草案引用的运行输入不存在或不属于当前助理会话。"""


class AssistantApprovalRequiredError(RuntimeError):
    """批量命令没有来自运行时的可信批准。"""


_DOMAIN_ERRORS = (
    PersonalTaskNotFound,
    PersonalTaskVersionConflict,
    InvalidPersonalTaskInput,
    InvalidPersonalTaskTransition,
    InvalidReminderInput,
    InvalidReminderTransition,
    ReminderNotFound,
    ReminderVersionConflict,
    AssistantRequestConflict,
    AssistantRequestSourceError,
)


class AssistantService:
    def __init__(
        self,
        db_path: Path,
        *,
        timezone: ZoneInfo,
        repository: SqliteAssistantRepository,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db_path = db_path
        self._timezone = timezone
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(UTC))
        self._run_repository: RunRepository | None = None

    def bind_run_repository(self, repository: RunRepository) -> None:
        if self._run_repository is not None:
            raise AssistantConfigurationError("助理运行仓储只能绑定一次")
        self._run_repository = repository

    def _run_repo(self) -> RunRepository:
        if self._run_repository is None:
            raise AssistantConfigurationError("助理运行仓储尚未绑定")
        return self._run_repository

    @staticmethod
    def _operation_name(tool_name: str) -> str:
        return _RESULT_OPERATION_NAMES[tool_name]

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise AssistantConfigurationError("助理时间上下文必须包含时区")
        return value.astimezone(UTC)

    @staticmethod
    def _timestamp(value: datetime) -> str:
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")

    @staticmethod
    def _json_default(value: object) -> str:
        if isinstance(value, datetime):
            return AssistantService._timestamp(value)
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, Enum) and isinstance(value.value, str):
            return value.value
        raise TypeError(f"无法规范化助理参数：{type(value).__name__}")

    @classmethod
    def _arguments_hash(cls, tool_name: str, session_id: str, arguments: BaseModel) -> str:
        normalized = {
            "tool_name": tool_name,
            "session_id": session_id,
            "arguments": arguments.model_dump(mode="python", exclude_unset=True),
        }
        encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=cls._json_default)
        return sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _result(
        operation: str,
        *,
        outcome: Literal["succeeded", "failed", "partial"] = "succeeded",
        data: AssistantOperationData | None = None,
        task_refs: list[AssistantTaskRef] | None = None,
        reminder_refs: list[AssistantReminderRef] | None = None,
        changes: list[AssistantChange] | None = None,
        result_set_id: str | None = None,
        request_id: str | None = None,
        items: list[AssistantBatchItemResult] | None = None,
        error: AssistantError | None = None,
    ) -> AssistantOperationResult:
        return AssistantOperationResult(
            operation=operation,
            outcome=outcome,
            data=data,
            task_refs=task_refs or [],
            reminder_refs=reminder_refs or [],
            changes=changes or [],
            result_set_id=result_set_id,
            request_id=request_id,
            items=items or [],
            error=error,
        )

    @classmethod
    def _error_result(cls, tool_name: str, code: str, message: str) -> AssistantOperationResult:
        return cls._result(
            cls._operation_name(tool_name),
            outcome="failed",
            error=AssistantError(code=code, message=message),
        )

    @classmethod
    def _domain_error_result(cls, tool_name: str, error: Exception) -> AssistantOperationResult:
        if isinstance(error, (PersonalTaskNotFound, ReminderNotFound)):
            code = "not_found"
        elif isinstance(error, (PersonalTaskVersionConflict, ReminderVersionConflict)):
            code = "version_conflict"
        elif isinstance(error, AssistantRequestConflict):
            code = "request_conflict"
        elif isinstance(error, (InvalidPersonalTaskTransition, InvalidReminderTransition)):
            code = "invalid_transition"
        elif isinstance(error, InvalidReminderInput):
            code = "invalid_time"
        elif isinstance(error, InvalidPersonalTaskInput):
            code = "invalid_arguments"
        elif isinstance(error, AssistantRequestSourceError):
            code = "invalid_arguments"
        else:
            raise TypeError(f"未映射的助理领域错误：{type(error).__name__}")
        return cls._error_result(tool_name, code, str(error))

    def _assert_context(self, context: ToolExecutionContext) -> None:
        if context.agent_id != ASSISTANT_AGENT_ID or context.session_id != ASSISTANT_SESSION_ID:
            raise AssistantConfigurationError("助理工具只能在固定助理身份和会话中运行")

    def _operation_key(self, context: ToolExecutionContext) -> str:
        self._assert_context(context)
        if not context.idempotency_key:
            raise AssistantConfigurationError("助理工具缺少运行时稳定幂等键")
        return context.idempotency_key

    async def _in_transaction(self, action: _TransactionAction[_T]) -> _T:
        now = self._utc(self._clock())

        async def operation() -> _T:
            async with SqliteAssistantTransaction(self._db_path) as connection:
                task_repository = SqlitePersonalTaskRepository(self._db_path, self._timezone, connection=connection)
                task_service = PersonalTaskService(task_repository, timezone=self._timezone, clock=lambda: now)
                reminder_service = ReminderService(task_repository, timezone=self._timezone, clock=lambda: now)
                return await action(connection, task_service, reminder_service, now)

        return await _await_io_completion(operation())

    async def _execute_once(
        self,
        operation_key: str,
        tool_name: str,
        arguments: BaseModel,
        context: ToolExecutionContext,
        action: _TransactionAction[AssistantOperationResult],
    ) -> AssistantOperationResult:
        self._assert_context(context)
        if not operation_key:
            raise AssistantConfigurationError("助理工具缺少运行时稳定幂等键")
        arguments_hash = self._arguments_hash(tool_name, context.session_id, arguments)

        async def execute(connection, task_service, reminder_service, now):
            existing = await self._repository.get_operation(operation_key, connection=connection)
            if existing is not None:
                if existing.arguments_hash != arguments_hash:
                    return self._error_result(tool_name, "key_conflict", "同一幂等键已用于不同参数")
                return existing.result

            await asyncio.to_thread(connection.execute, "SAVEPOINT effect")
            try:
                result = await action(connection, task_service, reminder_service, now)
            except _DOMAIN_ERRORS as error:
                await asyncio.to_thread(connection.execute, "ROLLBACK TO effect")
                await asyncio.to_thread(connection.execute, "RELEASE effect")
                result = self._domain_error_result(tool_name, error)
            else:
                await asyncio.to_thread(connection.execute, "RELEASE effect")
            record = AssistantOperation(
                operation_key=operation_key,
                session_id=context.session_id,
                run_id=context.agentrun_id,
                call_id=context.tool_call_id,
                tool_name=tool_name,
                arguments_hash=arguments_hash,
                request_id=result.request_id,
                result=result,
                created_at=now,
            )
            await self._repository.insert_operation(record, connection=connection)
            return result

        return await self._in_transaction(execute)

    @staticmethod
    def _deadline(value: date | datetime | None) -> TaskDeadline | None:
        if value is None:
            return None
        try:
            return TaskDeadline("datetime", value) if isinstance(value, datetime) else TaskDeadline("date", value)
        except ValueError as error:
            raise InvalidPersonalTaskInput(str(error)) from error

    @staticmethod
    def _rule_dict(rule: ReminderRule) -> dict[str, object]:
        return rule.model_dump(mode="python", exclude_unset=True)

    @staticmethod
    def _task_ref(task_view: PersonalTaskView) -> AssistantTaskRef:
        task = task_view.task
        return AssistantTaskRef(id=task.id, title=task.title, status=task.status.value, version=task.version)

    @staticmethod
    def _reminder_ref(reminder: Reminder) -> AssistantReminderRef:
        return AssistantReminderRef(
            id=reminder.id,
            task_id=reminder.task_id,
            status=reminder.status.value,
            version=reminder.version,
            next_fire_at=reminder.next_fire_at,
        )

    @classmethod
    def _changes(cls, task_view: PersonalTaskView) -> list[AssistantChange]:
        return [
            AssistantChange(
                id=change.reminder_id,
                action=change.action,
                reason=change.reason,
                next_fire_at=change.next_fire_at,
                version=change.version,
            )
            for change in task_view.reminder_changes
        ]

    @staticmethod
    def _deadline_text(task: PersonalTask) -> str | None:
        if task.deadline is None:
            return None
        if task.deadline.kind == "date":
            return task.deadline.value.isoformat()
        return AssistantService._timestamp(task.deadline.value)

    @staticmethod
    def _reminder_summary(reminders: tuple[Reminder, ...]) -> str:
        active = [reminder for reminder in reminders if reminder.status is ReminderStatus.ACTIVE]
        if not active:
            return "无启用提醒"
        next_fire = min((reminder.next_fire_at for reminder in active if reminder.next_fire_at is not None), default=None)
        next_text = AssistantService._timestamp(next_fire) if next_fire else ""
        return f"{len(active)} 个启用提醒" + (f"；下一次 {next_text}" if next_text else "")

    @classmethod
    def _result_item(cls, task_view: PersonalTaskView, reminder_summary: str) -> AssistantResultItem:
        task = task_view.task
        return AssistantResultItem(
            id=task.id,
            title=task.title,
            status=task.status.value,
            version=task.version,
            deadline=cls._deadline_text(task),
            reminder_summary=reminder_summary,
        )

    @staticmethod
    def _result_set_id(run_id: str, call_id: str) -> str:
        return str(uuid5(NAMESPACE_URL, f"dotclaw:assistant-result:{run_id}:{call_id}"))

    async def _result_set(
        self,
        connection: sqlite3.Connection,
        context: ToolExecutionContext,
        now: datetime,
        task_views: tuple[PersonalTaskView, ...],
        summaries: tuple[str, ...],
    ) -> AssistantResultSet:
        result_set = AssistantResultSet(
            id=self._result_set_id(context.agentrun_id, context.tool_call_id),
            session_id=context.session_id,
            run_id=context.agentrun_id,
            call_id=context.tool_call_id,
            items=[self._result_item(view, summary) for view, summary in zip(task_views, summaries)],
            created_at=now,
        )
        await self._repository.save_result_set(result_set, connection=connection)
        return result_set

    async def _associated_request(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        request_id: str | None,
        expected_version: int | None,
    ) -> AssistantRequest | None:
        if request_id is None:
            return None
        if expected_version is None:
            raise AssistantRequestConflict(request_id)
        request = await self._repository.get_request(session_id, request_id, connection=connection)
        if request is None or request.version != expected_version or request.status != "awaiting_input":
            raise AssistantRequestConflict(request_id)
        return request

    async def _resolve_request(
        self,
        connection: sqlite3.Connection,
        request: AssistantRequest | None,
        result_refs: tuple[str, ...],
        now: datetime,
    ) -> str | None:
        if request is None:
            return None
        resolved = request.model_copy(update={
            "status": "resolved",
            "result_refs": list(dict.fromkeys((*request.result_refs, *result_refs))),
            "version": request.version + 1,
            "updated_at": now,
        })
        await self._repository.save_request(resolved, expected_version=request.version, connection=connection)
        return request.id

    async def query_tasks(self, input: TaskQueryInput, context: ToolExecutionContext) -> AssistantOperationResult:
        tool_name = "builtin.assistant.tasks.query"

        async def action(connection, task_service, reminder_service, now):
            result = await task_service.query(PersonalTaskQuery(
                filter=PersonalTaskFilter(input.filter),
                keyword=input.keyword,
                deadline_from=input.deadline_from,
                deadline_until=input.deadline_until,
                overdue_only=input.overdue_only,
                offset=input.offset,
                limit=input.limit,
            ))
            summaries = tuple(
                (f"{summary.active_count} 个启用提醒" + (f"；下一次 {self._timestamp(summary.next_fire_at)}" if summary.next_fire_at else ""))
                if summary.active_count else "无启用提醒"
                for summary in result.reminder_summaries
            )
            task_views = result.items
            result_set = await self._result_set(connection, context, now, task_views, summaries)
            return self._result(
                "tasks.query",
                data=TaskPageData(
                    kind="task_page",
                    items=[self._task_ref(view) for view in task_views],
                    total=result.total,
                    offset=result.offset,
                    limit=result.limit,
                    timezone=result.timezone,
                    as_of=result.as_of,
                ),
                result_set_id=result_set.id,
            )

        return await self._execute_once(self._operation_key(context), tool_name, input, context, action)

    async def get_task(self, input: TaskGetInput, context: ToolExecutionContext) -> AssistantOperationResult:
        tool_name = "builtin.assistant.tasks.get"

        async def action(connection, task_service, reminder_service, now):
            task_view, reminders = await task_service.get_detail(input.task_id)
            result_set = await self._result_set(
                connection,
                context,
                now,
                (task_view,),
                (self._reminder_summary(reminders),),
            )
            return self._result(
                "tasks.get",
                data=TaskDetailData(
                    kind="task_detail",
                    task=self._task_ref(task_view),
                    reminders=[self._reminder_ref(item) for item in reminders],
                ),
                task_refs=[self._task_ref(task_view)],
                reminder_refs=[self._reminder_ref(item) for item in reminders],
                result_set_id=result_set.id,
            )

        return await self._execute_once(self._operation_key(context), tool_name, input, context, action)

    async def create_task(self, input: TaskCreateInput, context: ToolExecutionContext) -> AssistantOperationResult:
        tool_name = "builtin.assistant.tasks.create"

        async def action(connection, task_service, reminder_service, now):
            request = await self._associated_request(connection, context.session_id, input.request_id, input.expected_request_version)
            view = await task_service.create(
                title=input.title,
                description=input.description,
                deadline=self._deadline(input.deadline),
                reminders=tuple(self._rule_dict(rule) for rule in input.reminders),
            )
            saved_view, reminders = await task_service.get_detail(view.task.id)
            request_id = await self._resolve_request(connection, request, (view.task.id,), now)
            return self._result(
                "tasks.create",
                task_refs=[self._task_ref(saved_view)],
                reminder_refs=[self._reminder_ref(item) for item in reminders],
                changes=self._changes(view),
                request_id=request_id,
            )

        return await self._execute_once(self._operation_key(context), tool_name, input, context, action)

    async def update_task(self, input: TaskUpdateInput, context: ToolExecutionContext) -> AssistantOperationResult:
        tool_name = "builtin.assistant.tasks.update"

        async def action(connection, task_service, reminder_service, now):
            request = await self._associated_request(connection, context.session_id, input.request_id, input.expected_request_version)
            current = await task_service.get(input.task_id)
            patch_fields = input.patch.model_fields_set
            title = current.task.title if "title" not in patch_fields else input.patch.title
            description = current.task.description if "description" not in patch_fields else input.patch.description
            if title is None or description is None:
                raise InvalidPersonalTaskInput("事务标题和描述不能设为 null")
            deadline = current.task.deadline if "deadline" not in patch_fields else self._deadline(input.patch.deadline)
            view = await task_service.update(
                input.task_id,
                title=title,
                description=description,
                deadline=deadline,
                expected_version=input.expected_version,
            )
            _, reminders = await task_service.get_detail(input.task_id)
            request_id = await self._resolve_request(connection, request, (input.task_id,), now)
            return self._result(
                "tasks.update",
                task_refs=[self._task_ref(view)],
                reminder_refs=[self._reminder_ref(item) for item in reminders],
                changes=self._changes(view),
                request_id=request_id,
            )

        return await self._execute_once(self._operation_key(context), tool_name, input, context, action)

    async def change_task_status(self, input: TaskStatusInput, context: ToolExecutionContext) -> AssistantOperationResult:
        tool_name = "builtin.assistant.tasks.status"

        async def action(connection, task_service, reminder_service, now):
            request = await self._associated_request(connection, context.session_id, input.request_id, input.expected_request_version)
            view = await task_service.change_status(
                input.task_id,
                status=PersonalTaskStatus(input.status),
                expected_version=input.expected_version,
            )
            _, reminders = await task_service.get_detail(input.task_id)
            request_id = await self._resolve_request(connection, request, (input.task_id,), now)
            return self._result(
                "tasks.status",
                task_refs=[self._task_ref(view)],
                reminder_refs=[self._reminder_ref(item) for item in reminders],
                changes=self._changes(view),
                request_id=request_id,
            )

        return await self._execute_once(self._operation_key(context), tool_name, input, context, action)

    async def create_reminder(self, input: ReminderCreateInput, context: ToolExecutionContext) -> AssistantOperationResult:
        tool_name = "builtin.assistant.reminders.create"

        async def action(connection, task_service, reminder_service, now):
            request = await self._associated_request(connection, context.session_id, input.request_id, input.expected_request_version)
            reminder = await reminder_service.create(
                input.task_id,
                rule=self._rule_dict(input.rule),
                expected_task_version=input.expected_task_version,
            )
            task_view = await task_service.get(input.task_id)
            request_id = await self._resolve_request(connection, request, (input.task_id, reminder.id), now)
            return self._result(
                "reminders.create",
                task_refs=[self._task_ref(task_view)],
                reminder_refs=[self._reminder_ref(reminder)],
                request_id=request_id,
            )

        return await self._execute_once(self._operation_key(context), tool_name, input, context, action)

    async def update_reminder(self, input: ReminderUpdateInput, context: ToolExecutionContext) -> AssistantOperationResult:
        tool_name = "builtin.assistant.reminders.update"

        async def action(connection, task_service, reminder_service, now):
            request = await self._associated_request(connection, context.session_id, input.request_id, input.expected_request_version)
            reminder = await reminder_service.update(
                input.task_id,
                input.reminder_id,
                rule=self._rule_dict(input.rule),
                expected_version=input.expected_version,
                expected_task_version=input.expected_task_version,
            )
            task_view = await task_service.get(input.task_id)
            request_id = await self._resolve_request(connection, request, (input.task_id, reminder.id), now)
            return self._result(
                "reminders.update",
                task_refs=[self._task_ref(task_view)],
                reminder_refs=[self._reminder_ref(reminder)],
                request_id=request_id,
            )

        return await self._execute_once(self._operation_key(context), tool_name, input, context, action)

    async def change_reminder_status(self, input: ReminderStatusInput, context: ToolExecutionContext) -> AssistantOperationResult:
        tool_name = "builtin.assistant.reminders.status"

        async def action(connection, task_service, reminder_service, now):
            request = await self._associated_request(connection, context.session_id, input.request_id, input.expected_request_version)
            reminder = await reminder_service.change_status(
                input.task_id,
                input.reminder_id,
                action=input.action,
                expected_version=input.expected_version,
                expected_task_version=input.expected_task_version,
            )
            task_view = await task_service.get(input.task_id)
            request_id = await self._resolve_request(connection, request, (input.task_id, reminder.id), now)
            return self._result(
                "reminders.status",
                task_refs=[self._task_ref(task_view)],
                reminder_refs=[self._reminder_ref(reminder)],
                request_id=request_id,
            )

        return await self._execute_once(self._operation_key(context), tool_name, input, context, action)

    async def list_requests(self, input: RequestListInput, context: ToolExecutionContext) -> AssistantOperationResult:
        self._assert_context(context)
        requests, total = await self._repository.list_requests(
            context.session_id,
            keyword=input.keyword,
            offset=input.offset,
            limit=input.limit,
            statuses=("awaiting_input", "awaiting_confirmation"),
        )
        return self._result(
            "requests.list",
            data=RequestPageData(kind="request_page", items=requests, total=total, offset=input.offset, limit=input.limit),
        )

    async def get_request(self, input: RequestGetInput, context: ToolExecutionContext) -> AssistantOperationResult:
        self._assert_context(context)
        request = await self._repository.get_request(context.session_id, input.id)
        if request is None:
            return self._error_result("builtin.assistant.requests.get", "not_found", "助理请求不存在")
        return self._result("requests.get", data=RequestDetailData(kind="request_detail", request=request))

    async def get_result_set(self, input: ResultSetGetInput, context: ToolExecutionContext) -> AssistantOperationResult:
        self._assert_context(context)
        result_set = await self._repository.get_result_set(context.session_id, input.result_set_id)
        if result_set is None:
            return self._error_result("builtin.assistant.results.get", "not_found", "结果快照不存在")
        return self._result("results.get", data=ResultSetData(kind="result_set", result_set=result_set), result_set_id=result_set.id)

    def _current_input_ref(self, context: ToolExecutionContext) -> AssistantInputRef:
        message_id = context.input_message_id
        created_at = context.input_created_at
        if not message_id or not created_at:
            raise AssistantConfigurationError("助理运行上下文缺少服务端输入消息来源")
        try:
            value = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise AssistantConfigurationError("助理输入消息时间格式无效") from error
        if value.tzinfo is None or value.utcoffset() is None:
            raise AssistantConfigurationError("助理输入消息时间必须包含时区")
        return AssistantInputRef(run_id=context.agentrun_id, message_id=message_id, created_at=value.astimezone(UTC))

    async def _resolve_source_refs(
        self,
        session_id: str,
        context: ToolExecutionContext,
        source_refs: tuple[AssistantSourceRef | RequestSourceInput, ...],
    ) -> list[AssistantInputRef]:
        repository = self._run_repo()
        current = self._current_input_ref(context)
        requested = list(source_refs)
        if not any(ref.run_id == current.run_id and ref.message_id == current.message_id for ref in requested):
            requested.append(AssistantSourceRef(run_id=current.run_id, message_id=current.message_id))
        unique: dict[tuple[str, str], AssistantInputRef] = {}
        for ref in requested:
            key = (ref.run_id, ref.message_id)
            if key in unique:
                continue
            run: AgentRun | None = await repository.load_run_snapshot(session_id, ref.run_id)
            if run is None or run.session_id != session_id or run.agent_id != ASSISTANT_AGENT_ID or run.input_message_id != ref.message_id:
                raise AssistantRequestSourceError("来源必须是当前助理会话中的用户输入消息")
            messages = await repository.load_messages(session_id, ref.run_id)
            message = next((item for item in messages if item.message_id == ref.message_id), None)
            if message is None or message.role is not MessageRole.USER or message.kind is not RunMessageKind.USER_INPUT:
                raise AssistantRequestSourceError("来源消息不是已保存的用户输入")
            if key == (current.run_id, current.message_id):
                created_at = current.created_at
            else:
                created_value = message.metadata.get("created_at")
                if not isinstance(created_value, str) or not created_value:
                    raise AssistantRequestSourceError("历史来源缺少用户输入消息时间")
                try:
                    created_at = datetime.fromisoformat(created_value.replace("Z", "+00:00"))
                except ValueError as error:
                    raise AssistantRequestSourceError("历史用户输入消息时间格式无效") from error
                if created_at.tzinfo is None or created_at.utcoffset() is None:
                    raise AssistantRequestSourceError("历史用户输入消息时间必须包含时区")
                created_at = created_at.astimezone(UTC)
            unique[key] = AssistantInputRef(run_id=ref.run_id, message_id=ref.message_id, created_at=created_at)
        return list(unique.values())

    @staticmethod
    def _validate_temporal_sources(payload: AssistantRequestPayload, refs: list[AssistantInputRef]) -> None:
        allowed = {(ref.run_id, ref.message_id) for ref in refs}
        for value in payload.temporal_values:
            if (value.source_ref.run_id, value.source_ref.message_id) not in allowed:
                raise AssistantRequestSourceError("时间解释必须关联已验证的用户输入来源")

    @staticmethod
    def _payload(
        intent_args: AssistantRequestIntent,
        *,
        selected_task_ids: list[str] | None = None,
    ) -> AssistantRequestPayload:
        return AssistantRequestPayload(
            intent_args=intent_args,
            missing_fields=[],
            candidate_task_ids=[],
            selected_task_ids=selected_task_ids or [],
            temporal_values=[],
            last_error=None,
        )

    async def save_request(self, input: RequestSaveInput, context: ToolExecutionContext) -> AssistantOperationResult:
        tool_name = "builtin.assistant.requests.save"

        async def action(connection, task_service, reminder_service, now):
            self._assert_context(context)
            existing = None
            if input.id is not None:
                existing = await self._repository.get_request(context.session_id, input.id, connection=connection)
                if existing is None or existing.version != input.expected_version or existing.status != "awaiting_input":
                    raise AssistantRequestConflict(input.id)
            source_inputs = tuple(input.source_refs)
            if existing is not None:
                source_inputs = tuple((*existing.source_refs, *source_inputs))
            refs = await self._resolve_source_refs(context.session_id, context, source_inputs)
            self._validate_temporal_sources(input.payload, refs)
            if existing is None:
                request = AssistantRequest(
                    id=str(uuid4()),
                    session_id=context.session_id,
                    kind=input.kind,
                    summary=input.summary,
                    status="awaiting_input",
                    payload=input.payload,
                    source_refs=refs,
                    proposal=None,
                    proposal_hash=None,
                    proposal_run_id=None,
                    result_refs=[],
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
                await self._repository.save_request(request, expected_version=None, connection=connection)
            else:
                request = AssistantRequest(
                    id=existing.id,
                    session_id=context.session_id,
                    kind=input.kind,
                    summary=input.summary,
                    status="awaiting_input",
                    payload=input.payload,
                    source_refs=refs,
                    proposal=None,
                    proposal_hash=None,
                    proposal_run_id=None,
                    result_refs=existing.result_refs,
                    version=existing.version + 1,
                    created_at=existing.created_at,
                    updated_at=now,
                )
                await self._repository.save_request(request, expected_version=existing.version, connection=connection)
            return self._result("requests.save", data=RequestDetailData(kind="request_detail", request=request), request_id=request.id)

        return await self._execute_once(self._operation_key(context), tool_name, input, context, action)

    async def abandon_request(self, input: RequestAbandonInput, context: ToolExecutionContext) -> AssistantOperationResult:
        tool_name = "builtin.assistant.requests.abandon"

        async def action(connection, task_service, reminder_service, now):
            request = await self._repository.get_request(context.session_id, input.id, connection=connection)
            if request is None or request.version != input.expected_version or request.status not in ("awaiting_input", "awaiting_confirmation"):
                raise AssistantRequestConflict(input.id)
            if request.status == "awaiting_confirmation" and await self._has_active_apply(request):
                raise AssistantRequestConflict("先处理当前批量审批，再放弃请求")
            abandoned = AssistantRequest(
                id=request.id,
                session_id=request.session_id,
                kind=request.kind,
                summary=request.summary,
                status="abandoned",
                payload=request.payload,
                source_refs=request.source_refs,
                proposal=None,
                proposal_hash=None,
                proposal_run_id=None,
                result_refs=request.result_refs,
                version=request.version + 1,
                created_at=request.created_at,
                updated_at=now,
            )
            await self._repository.save_request(abandoned, expected_version=request.version, connection=connection)
            return self._result("requests.abandon", data=RequestDetailData(kind="request_detail", request=abandoned), request_id=abandoned.id)

        return await self._execute_once(self._operation_key(context), tool_name, input, context, action)

    async def _has_active_apply(self, request: AssistantRequest) -> bool:
        repository = self._run_repo()
        if request.proposal_run_id is None:
            return False
        run = await repository.load_run_snapshot(request.session_id, request.proposal_run_id)
        if run is None or run.session_id != request.session_id or run.agent_id != ASSISTANT_AGENT_ID:
            raise AssistantRequestSourceError("批量提案来源运行不存在或归属无效")
        ledger: ToolCallLedger | None = await repository.load_tool_ledger(request.session_id, request.proposal_run_id)
        if ledger is None:
            return False
        apply_record = next((
            record for record in ledger.calls
            if record.call.name == "builtin.assistant.batch.apply"
            and record.call.arguments.get("request_id") == request.id
            and record.call.arguments.get("expected_version") == request.version
            and record.call.arguments.get("proposal_hash") == request.proposal_hash
        ), None)
        if apply_record is None:
            return False
        if run.state.is_ended() and apply_record.state is ToolCallState.PENDING:
            return False
        return not apply_record.is_settled

    def _batch_command(self, item: BatchItem) -> AssistantBatchCommand:
        if isinstance(item, BatchUpdateItem):
            return TaskUpdateAction(
                action="update_task",
                task_id=item.task_id,
                expected_version=item.expected_version,
                patch=StoredTaskPatch.model_validate(item.patch.model_dump(mode="json", exclude_unset=True)),
            )
        if isinstance(item, BatchStatusItem):
            return TaskStatusAction(
                action="change_status",
                task_id=item.task_id,
                expected_version=item.expected_version,
                status=item.status,
            )
        if isinstance(item, BatchReminderUpdateItem):
            return ReminderUpdateAction(
                action="update_reminder",
                task_id=item.task_id,
                reminder_id=item.reminder_id,
                expected_task_version=item.expected_task_version,
                expected_version=item.expected_version,
                rule=item.rule,
            )
        if isinstance(item, BatchReminderStatusItem):
            return ReminderStatusAction(
                action="change_reminder_status",
                task_id=item.task_id,
                reminder_id=item.reminder_id,
                expected_task_version=item.expected_task_version,
                expected_version=item.expected_version,
                status=item.status,
            )
        raise TypeError(f"未知助理批量输入：{type(item).__name__}")

    @staticmethod
    def _reminder_at(task_id: str, reminders: tuple[Reminder, ...], reminder_id: str) -> Reminder:
        reminder = next((item for item in reminders if item.id == reminder_id and item.task_id == task_id), None)
        if reminder is None:
            raise ReminderNotFound(reminder_id)
        return reminder

    async def _prepare_item(
        self,
        item: BatchItem,
        task_service: PersonalTaskService,
    ) -> tuple[AssistantProposalItem, AssistantBatchCommand]:
        command = self._batch_command(item)
        if isinstance(item, (BatchUpdateItem, BatchStatusItem, BatchReminderUpdateItem, BatchReminderStatusItem)):
            task_view, reminders = await task_service.get_detail(item.task_id)
            task = task_view.task
            expected_task_version = (
                item.expected_task_version
                if isinstance(item, (BatchReminderUpdateItem, BatchReminderStatusItem))
                else item.expected_version
            )
            if task.version != expected_task_version:
                raise PersonalTaskVersionConflict(item.task_id)
            if isinstance(item, BatchUpdateItem):
                before_values = AssistantProposalValues(
                    title=task.title,
                    description=task.description,
                    deadline=self._deadline_text(task),
                    status=task.status.value,
                    version=task.version,
                )
                after = before_values.model_dump(mode="python", exclude_unset=True)
                for field in item.patch.model_fields_set:
                    value = getattr(item.patch, field)
                    if field in ("title", "description") and value is None:
                        raise InvalidPersonalTaskInput(f"{field} 不能设为 null")
                    after[field] = self._timestamp(value) if isinstance(value, datetime) else value.isoformat() if isinstance(value, date) else value
                after["version"] = task.version + 1
                return AssistantProposalItem(
                    target=self._task_ref(task_view),
                    before=before_values,
                    after=AssistantProposalValues.model_validate(after),
                    command=command,
                ), command
            if isinstance(item, BatchStatusItem):
                before = AssistantProposalValues(status=task.status.value, version=task.version)
                after = AssistantProposalValues(status=item.status, version=task.version + 1)
                return AssistantProposalItem(target=self._task_ref(task_view), before=before, after=after, command=command), command
            reminder = self._reminder_at(item.task_id, reminders, item.reminder_id)
            if reminder.version != item.expected_version:
                raise ReminderVersionConflict(item.reminder_id)
            target = self._reminder_ref(reminder)
            if isinstance(item, BatchReminderUpdateItem):
                before = AssistantProposalValues(status=reminder.status.value, version=reminder.version, reminder_rule=reminder.rule)
                after = AssistantProposalValues(status="active", version=reminder.version + 1, reminder_rule=item.rule)
            else:
                target_status = {"pause": "paused", "resume": "active", "cancel": "cancelled"}[item.status]
                before = AssistantProposalValues(status=reminder.status.value, version=reminder.version, reminder_rule=reminder.rule)
                after = AssistantProposalValues(status=target_status, version=reminder.version + 1, reminder_rule=reminder.rule)
            return AssistantProposalItem(target=target, before=before, after=after, command=command), command
        raise TypeError(f"未知助理批量输入：{type(item).__name__}")

    async def prepare_batch(self, input: BatchPrepareInput, context: ToolExecutionContext) -> AssistantOperationResult:
        tool_name = "builtin.assistant.batch.prepare"

        async def action(connection, task_service, reminder_service, now):
            self._assert_context(context)
            task_ids = [item.task_id for item in input.items]
            if len(task_ids) != len(set(task_ids)):
                raise InvalidPersonalTaskInput("同一事务在一个批量提案中只能出现一次")
            request = await self._associated_request(connection, context.session_id, input.request_id, input.expected_request_version)
            prepared = [await self._prepare_item(item, task_service) for item in input.items]
            proposal = AssistantProposal(summary=input.summary, items=[item for item, _ in prepared])
            proposal_hash = sha256(proposal.canonical_json().encode("utf-8")).hexdigest()
            commands = [command for _, command in prepared]
            payload = self._payload(
                RequestBatchIntent(action="batch", summary=input.summary, items=commands),
                selected_task_ids=task_ids,
            )
            source_refs = await self._resolve_source_refs(context.session_id, context, ())
            if request is None:
                saved = AssistantRequest(
                    id=str(uuid4()),
                    session_id=context.session_id,
                    kind="batch",
                    summary=input.summary,
                    status="awaiting_confirmation",
                    payload=payload,
                    source_refs=source_refs,
                    proposal=proposal,
                    proposal_hash=proposal_hash,
                    proposal_run_id=context.agentrun_id,
                    result_refs=[],
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
                await self._repository.save_request(saved, expected_version=None, connection=connection)
            else:
                refs = [ref for ref in request.source_refs]
                refs.extend(ref for ref in source_refs if (ref.run_id, ref.message_id) not in {(old.run_id, old.message_id) for old in refs})
                saved = AssistantRequest(
                    id=request.id,
                    session_id=context.session_id,
                    kind="batch",
                    summary=input.summary,
                    status="awaiting_confirmation",
                    payload=payload,
                    source_refs=refs,
                    proposal=proposal,
                    proposal_hash=proposal_hash,
                    proposal_run_id=context.agentrun_id,
                    result_refs=request.result_refs,
                    version=request.version + 1,
                    created_at=request.created_at,
                    updated_at=now,
                )
                await self._repository.save_request(saved, expected_version=request.version, connection=connection)
            return self._result("batch.prepare", data=RequestDetailData(kind="request_detail", request=saved), request_id=saved.id)

        return await self._execute_once(self._operation_key(context), tool_name, input, context, action)

    async def _execute_batch_command(
        self,
        command: AssistantBatchCommand,
        task_service: PersonalTaskService,
        reminder_service: ReminderService,
    ) -> AssistantOperationResult:
        if isinstance(command, TaskUpdateAction):
            current = await task_service.get(command.task_id)
            fields = command.patch.model_fields_set
            title = current.task.title if "title" not in fields else command.patch.title
            description = current.task.description if "description" not in fields else command.patch.description
            if title is None or description is None:
                raise InvalidPersonalTaskInput("事务标题和描述不能设为 null")
            if "deadline" not in fields:
                deadline = current.task.deadline
            elif command.patch.deadline is None:
                deadline = None
            elif "T" in command.patch.deadline:
                try:
                    parsed_deadline = datetime.fromisoformat(command.patch.deadline.replace("Z", "+00:00"))
                except ValueError as error:
                    raise InvalidPersonalTaskInput("批量提案中的截止时刻格式无效") from error
                if parsed_deadline.tzinfo is None or parsed_deadline.utcoffset() is None:
                    raise InvalidPersonalTaskInput("批量提案中的截止时刻必须包含时区")
                deadline = self._deadline(parsed_deadline)
            else:
                try:
                    deadline = self._deadline(date.fromisoformat(command.patch.deadline))
                except ValueError as error:
                    raise InvalidPersonalTaskInput("批量提案中的截止日期格式无效") from error
            view = await task_service.update(command.task_id, title=title, description=description, deadline=deadline, expected_version=command.expected_version)
            _, reminders = await task_service.get_detail(command.task_id)
            return self._result("tasks.update", task_refs=[self._task_ref(view)], reminder_refs=[self._reminder_ref(item) for item in reminders], changes=self._changes(view))
        if isinstance(command, TaskStatusAction):
            view = await task_service.change_status(command.task_id, status=PersonalTaskStatus(command.status), expected_version=command.expected_version)
            _, reminders = await task_service.get_detail(command.task_id)
            return self._result("tasks.status", task_refs=[self._task_ref(view)], reminder_refs=[self._reminder_ref(item) for item in reminders], changes=self._changes(view))
        if isinstance(command, ReminderUpdateAction):
            reminder = await reminder_service.update(command.task_id, command.reminder_id, rule=command.rule.model_dump(mode="python", exclude_unset=True), expected_version=command.expected_version, expected_task_version=command.expected_task_version)
            task = await task_service.get(command.task_id)
            return self._result("reminders.update", task_refs=[self._task_ref(task)], reminder_refs=[self._reminder_ref(reminder)])
        if isinstance(command, ReminderStatusAction):
            reminder = await reminder_service.change_status(command.task_id, command.reminder_id, action=command.status, expected_version=command.expected_version, expected_task_version=command.expected_task_version)
            task = await task_service.get(command.task_id)
            return self._result("reminders.status", task_refs=[self._task_ref(task)], reminder_refs=[self._reminder_ref(reminder)])
        raise TypeError(f"未知助理批量命令：{type(command).__name__}")

    @staticmethod
    def _batch_item_result(index: int, result: AssistantOperationResult) -> AssistantBatchItemResult:
        return AssistantBatchItemResult(
            index=index,
            outcome="failed" if result.outcome == "failed" else "succeeded",
            task_refs=result.task_refs,
            reminder_refs=result.reminder_refs,
            error=result.error,
        )

    @staticmethod
    def _batch_child_key(request_id: str, proposal_hash: str, index: int) -> str:
        return f"batch:{request_id}:{proposal_hash}:{index}"

    @staticmethod
    def _batch_summary_key(request_id: str, proposal_hash: str) -> str:
        return f"batch-summary:{request_id}:{proposal_hash}"

    async def apply_batch(self, input: BatchApplyInput, context: ToolExecutionContext) -> AssistantOperationResult:
        tool_name = "builtin.assistant.batch.apply"
        operation_key = self._operation_key(context)
        arguments_hash = self._arguments_hash(tool_name, context.session_id, input)
        if not context.approved:
            raise AssistantApprovalRequiredError("批量执行必须经过运行时批准")

        async def check_replay(connection, task_service, reminder_service, now):
            existing = await self._repository.get_operation(operation_key, connection=connection)
            if existing is not None:
                if existing.arguments_hash != arguments_hash:
                    return self._error_result(tool_name, "key_conflict", "同一幂等键已用于不同参数")
                if (
                    existing.session_id != context.session_id
                    or existing.run_id != context.agentrun_id
                    or existing.call_id != context.tool_call_id
                    or existing.tool_name != tool_name
                    or existing.request_id != input.request_id
                ):
                    return self._error_result(tool_name, "request_conflict", "操作回执不属于当前运行调用")
                return existing.result
            summary = await self._repository.get_operation(self._batch_summary_key(input.request_id, input.proposal_hash), connection=connection)
            if summary is not None:
                if summary.arguments_hash != arguments_hash:
                    return self._error_result(tool_name, "key_conflict", "同一批量提案的汇总参数不匹配")
                if summary.run_id != context.agentrun_id or summary.request_id != input.request_id or summary.tool_name != tool_name:
                    return self._error_result(tool_name, "request_conflict", "批量汇总回执不属于当前运行提案")
                await self._repository.insert_operation(
                    AssistantOperation(
                        operation_key=operation_key,
                        session_id=context.session_id,
                        run_id=context.agentrun_id,
                        call_id=context.tool_call_id,
                        tool_name=tool_name,
                        arguments_hash=arguments_hash,
                        request_id=input.request_id,
                        result=summary.result,
                        created_at=now,
                    ),
                    connection=connection,
                )
                return summary.result
            return None

        replay = await self._in_transaction(check_replay)
        if replay is not None:
            return replay

        async def read_proposal(connection, task_service, reminder_service, now):
            existing = await self._repository.get_operation(operation_key, connection=connection)
            if existing is not None:
                if existing.arguments_hash != arguments_hash:
                    return self._error_result(tool_name, "key_conflict", "同一幂等键已用于不同参数")
                if (
                    existing.session_id != context.session_id
                    or existing.run_id != context.agentrun_id
                    or existing.call_id != context.tool_call_id
                    or existing.tool_name != tool_name
                    or existing.request_id != input.request_id
                ):
                    return self._error_result(tool_name, "request_conflict", "操作回执不属于当前运行调用")
                return existing.result
            summary = await self._repository.get_operation(
                self._batch_summary_key(input.request_id, input.proposal_hash),
                connection=connection,
            )
            if summary is not None:
                if summary.arguments_hash != arguments_hash:
                    return self._error_result(tool_name, "key_conflict", "同一批量提案的汇总参数不匹配")
                if summary.run_id != context.agentrun_id or summary.request_id != input.request_id or summary.tool_name != tool_name:
                    return self._error_result(tool_name, "request_conflict", "批量汇总回执不属于当前运行提案")
                await self._repository.insert_operation(
                    AssistantOperation(
                        operation_key=operation_key,
                        session_id=context.session_id,
                        run_id=context.agentrun_id,
                        call_id=context.tool_call_id,
                        tool_name=tool_name,
                        arguments_hash=arguments_hash,
                        request_id=input.request_id,
                        result=summary.result,
                        created_at=now,
                    ),
                    connection=connection,
                )
                return summary.result
            request = await self._repository.get_request(context.session_id, input.request_id, connection=connection)
            if request is None or request.status != "awaiting_confirmation" or request.version != input.expected_version or request.proposal_hash != input.proposal_hash or request.proposal is None:
                conflict = AssistantRequestConflict(input.request_id)
                failure = self._domain_error_result(tool_name, conflict)
                await self._repository.insert_operation(
                    AssistantOperation(
                        operation_key=operation_key,
                        session_id=context.session_id,
                        run_id=context.agentrun_id,
                        call_id=context.tool_call_id,
                        tool_name=tool_name,
                        arguments_hash=arguments_hash,
                        request_id=input.request_id,
                        result=failure,
                        created_at=now,
                    ),
                    connection=connection,
                )
                return failure
            if request.proposal_run_id != context.agentrun_id:
                conflict = AssistantRequestConflict("批量提案与批准运行不匹配")
                failure = self._domain_error_result(tool_name, conflict)
                await self._repository.insert_operation(
                    AssistantOperation(
                        operation_key=operation_key,
                        session_id=context.session_id,
                        run_id=context.agentrun_id,
                        call_id=context.tool_call_id,
                        tool_name=tool_name,
                        arguments_hash=arguments_hash,
                        request_id=input.request_id,
                        result=failure,
                        created_at=now,
                    ),
                    connection=connection,
                )
                return failure
            return request

        request_or_failure = await self._in_transaction(read_proposal)
        if isinstance(request_or_failure, AssistantOperationResult):
            return request_or_failure
        request = request_or_failure
        child_results: list[AssistantBatchItemResult] = []
        final_result: AssistantOperationResult | None = None

        for index, proposal_item in enumerate(request.proposal.items):
            command = proposal_item.command
            child_key = self._batch_child_key(request.id, input.proposal_hash, index)
            child_arguments = {"request_id": request.id, "proposal_hash": input.proposal_hash, "index": index, "command": command.model_dump(mode="python", exclude_unset=True)}
            child_hash_value = json.dumps(
                {"tool_name": tool_name, "session_id": context.session_id, "arguments": child_arguments},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                default=self._json_default,
            )
            child_hash = sha256(child_hash_value.encode("utf-8")).hexdigest()
            is_final = index == len(request.proposal.items) - 1

            async def apply_item(connection, task_service, reminder_service, now, *, item_index=index, item_command=command, key=child_key, hash_value=child_hash, finalize=is_final):
                previous = await self._repository.get_operation(key, connection=connection)
                if previous is not None:
                    if previous.arguments_hash != hash_value:
                        raise AssistantRequestConflict(key)
                    item_result = previous.result
                else:
                    current_request = await self._repository.get_request(context.session_id, request.id, connection=connection)
                    if current_request is None or current_request.status != "awaiting_confirmation" or current_request.version != input.expected_version or current_request.proposal_hash != input.proposal_hash or current_request.proposal_run_id != context.agentrun_id:
                        raise AssistantRequestConflict(request.id)
                    await asyncio.to_thread(connection.execute, "SAVEPOINT effect")
                    try:
                        item_result = await self._execute_batch_command(item_command, task_service, reminder_service)
                    except _DOMAIN_ERRORS as error:
                        await asyncio.to_thread(connection.execute, "ROLLBACK TO effect")
                        await asyncio.to_thread(connection.execute, "RELEASE effect")
                        item_result = self._domain_error_result(self._batch_item_tool_name(item_command), error)
                    else:
                        await asyncio.to_thread(connection.execute, "RELEASE effect")
                    child_record = AssistantOperation(
                        operation_key=key,
                        session_id=context.session_id,
                        run_id=context.agentrun_id,
                        call_id=f"{context.tool_call_id}:{item_index}",
                        tool_name=self._batch_item_tool_name(item_command),
                        arguments_hash=hash_value,
                        request_id=request.id,
                        result=item_result,
                        created_at=now,
                    )
                    await self._repository.insert_operation(child_record, connection=connection)
                if not finalize:
                    return None

                all_results: list[AssistantOperationResult] = []
                for result_index in range(len(request.proposal.items)):
                    record = await self._repository.get_operation(self._batch_child_key(request.id, input.proposal_hash, result_index), connection=connection)
                    if record is None:
                        raise AssistantStorageError("批量汇总缺少已提交子项回执")
                    all_results.append(record.result)
                results = [self._batch_item_result(result_index, result) for result_index, result in enumerate(all_results)]
                all_succeeded = all(item.outcome == "succeeded" for item in results)
                refs = list(dict.fromkeys(
                    [ref.id for result in all_results for ref in (*result.task_refs, *result.reminder_refs)]
                ))
                updated = AssistantRequest(
                    id=request.id,
                    session_id=request.session_id,
                    kind=request.kind,
                    summary=request.summary,
                    status="resolved" if all_succeeded else "awaiting_input",
                    payload=request.payload.model_copy(update={"last_error": None if all_succeeded else "批量操作有失败项，请检查结果并重新准备提案"}),
                    source_refs=request.source_refs,
                    proposal=request.proposal if all_succeeded else None,
                    proposal_hash=request.proposal_hash if all_succeeded else None,
                    proposal_run_id=request.proposal_run_id if all_succeeded else None,
                    result_refs=list(dict.fromkeys((*request.result_refs, *refs))),
                    version=request.version + 1,
                    created_at=request.created_at,
                    updated_at=now,
                )
                await self._repository.save_request(updated, expected_version=request.version, connection=connection)
                summary_result = self._result(
                    "batch.apply",
                    outcome="succeeded" if all_succeeded else "partial",
                    request_id=request.id,
                    items=results,
                )
                summary_record = AssistantOperation(
                    operation_key=self._batch_summary_key(request.id, input.proposal_hash),
                    session_id=context.session_id,
                    run_id=context.agentrun_id,
                    call_id=context.tool_call_id,
                    tool_name=tool_name,
                    arguments_hash=arguments_hash,
                    request_id=request.id,
                    result=summary_result,
                    created_at=now,
                )
                await self._repository.insert_operation(summary_record, connection=connection)
                await self._repository.insert_operation(
                    AssistantOperation(
                        operation_key=operation_key,
                        session_id=context.session_id,
                        run_id=context.agentrun_id,
                        call_id=context.tool_call_id,
                        tool_name=tool_name,
                        arguments_hash=arguments_hash,
                        request_id=request.id,
                        result=summary_result,
                        created_at=now,
                    ),
                    connection=connection,
                )
                return summary_result

            final_result = await self._in_transaction(apply_item)
            if final_result is not None:
                return final_result
        raise AssistantStorageError("批量执行结束时没有汇总回执")

    @staticmethod
    def _batch_item_tool_name(command: AssistantBatchCommand) -> str:
        if isinstance(command, TaskUpdateAction):
            return "builtin.assistant.tasks.update"
        if isinstance(command, TaskStatusAction):
            return "builtin.assistant.tasks.status"
        if isinstance(command, ReminderUpdateAction):
            return "builtin.assistant.reminders.update"
        if isinstance(command, ReminderStatusAction):
            return "builtin.assistant.reminders.status"
        raise TypeError(f"未知助理批量命令：{type(command).__name__}")

    async def reconcile_controls(self, session_id: str) -> None:
        if session_id != ASSISTANT_SESSION_ID:
            raise AssistantConfigurationError("请求状态同步只能针对固定助理会话")
        repository = self._run_repo()
        offset = 0
        awaiting_confirmation: list[AssistantRequest] = []
        while True:
            requests, total = await self._repository.list_requests(
                session_id,
                keyword="",
                offset=offset,
                limit=50,
                statuses=("awaiting_confirmation",),
            )
            awaiting_confirmation.extend(requests)
            offset += len(requests)
            if offset >= total or not requests:
                break
        for request in awaiting_confirmation:
            await self._reconcile_request(request, repository)

    async def _reconcile_request(self, request: AssistantRequest, repository: RunRepository) -> None:
        if request.status != "awaiting_confirmation" or request.proposal_run_id is None or request.proposal_hash is None:
            return
        run = await repository.load_run_snapshot(request.session_id, request.proposal_run_id)
        if run is None:
            raise AssistantStorageError("批量提案来源运行不存在")
        if run.session_id != request.session_id or run.agent_id != ASSISTANT_AGENT_ID:
            raise AssistantRequestSourceError("批量提案来源运行归属无效")

        async def reconcile(connection, task_service, reminder_service, now):
            summary_key = self._batch_summary_key(request.id, request.proposal_hash)
            summary = await self._repository.get_operation(summary_key, connection=connection)
            if summary is not None:
                target_status = "resolved" if summary.result.outcome == "succeeded" else "awaiting_input"
                if request.status == target_status:
                    return
                await self._save_reconciled_request(connection, request, target_status, summary.result, now)
                return

            if not run.state.is_ended():
                return
            ledger: ToolCallLedger | None = await repository.load_tool_ledger(request.session_id, request.proposal_run_id)
            apply_record: ToolCallRecord | None = None
            if ledger is not None:
                apply_record = next((
                    record for record in ledger.calls
                    if record.call.name == "builtin.assistant.batch.apply"
                    and record.call.arguments.get("request_id") == request.id
                    and record.call.arguments.get("expected_version") == request.version
                    and record.call.arguments.get("proposal_hash") == request.proposal_hash
                ), None)
            if apply_record is not None:
                if (
                    apply_record.state is not ToolCallState.PENDING
                    and apply_record.approval_decision is not False
                    and not apply_record.is_settled
                ):
                    return

            results: list[AssistantOperationResult] = []
            if request.proposal is not None:
                for index in range(len(request.proposal.items)):
                    child = await self._repository.get_operation(self._batch_child_key(request.id, request.proposal_hash, index), connection=connection)
                    if child is not None:
                        results.append(child.result)
            partial_refs = [
                ref.id
                for result in results
                for ref in (*result.task_refs, *result.reminder_refs)
            ]
            reason = "批量审批被拒绝" if apply_record is not None and apply_record.approval_decision is False else "批量运行已结束，未完成的项目可重新准备提案"
            projection = self._result("batch.apply", outcome="partial", request_id=request.id, items=[self._batch_item_result(i, result) for i, result in enumerate(results)])
            await self._save_reconciled_request(connection, request, "awaiting_input", projection, now, partial_refs, reason)

        await self._in_transaction(reconcile)

    async def _save_reconciled_request(
        self,
        connection,
        request: AssistantRequest,
        status: str,
        result: AssistantOperationResult,
        now: datetime,
        result_refs: list[str] | None = None,
        reason: str | None = None,
    ) -> None:
        resolved = status == "resolved"
        refs = result_refs if result_refs is not None else [ref.id for ref in (*result.task_refs, *result.reminder_refs)]
        payload = request.payload if resolved else request.payload.model_copy(update={"last_error": reason or "批量提案已结束，请重新确认操作"})
        updated = AssistantRequest(
            id=request.id,
            session_id=request.session_id,
            kind=request.kind,
            summary=request.summary,
            status=status,
            payload=payload,
            source_refs=request.source_refs,
            proposal=request.proposal if resolved else None,
            proposal_hash=request.proposal_hash if resolved else None,
            proposal_run_id=request.proposal_run_id if resolved else None,
            result_refs=list(dict.fromkeys((*request.result_refs, *refs))),
            version=request.version + 1,
            created_at=request.created_at,
            updated_at=now,
        )
        try:
            await self._repository.save_request(updated, expected_version=request.version, connection=connection)
        except AssistantRequestConflict:
            latest = await self._repository.get_request(request.session_id, request.id, connection=connection)
            if latest is None or latest.version != request.version:
                return
            raise

    async def get_run_results(self, session_id: str, run_id: str) -> AssistantRunResults:
        if session_id != ASSISTANT_SESSION_ID:
            raise AssistantRequestSourceError("运行结果只允许从固定助理会话读取")
        repository = self._run_repo()
        run = await repository.load_run_snapshot(session_id, run_id)
        if run is None or run.session_id != session_id or run.agent_id != ASSISTANT_AGENT_ID:
            raise AssistantRequestSourceError("助理运行不存在")
        result_sets, operations = await self._repository.list_run_results(session_id, run_id)
        return AssistantRunResults(run_id=run_id, result_sets=result_sets, operations=operations)

    async def get_proposal(
        self,
        session_id: str,
        request_id: str,
        version: int,
        proposal_hash: str,
    ) -> AssistantProposalPreview:
        if session_id != ASSISTANT_SESSION_ID:
            raise AssistantRequestSourceError("批量提案只允许从固定助理会话读取")
        request = await self._repository.get_request(session_id, request_id)
        if request is None or request.version != version or request.proposal_hash != proposal_hash or request.proposal is None or request.proposal_run_id is None:
            raise AssistantRequestConflict(request_id)
        run = await self._run_repo().load_run_snapshot(session_id, request.proposal_run_id)
        if run is None or run.session_id != session_id or run.agent_id != ASSISTANT_AGENT_ID:
            raise AssistantRequestSourceError("批量提案来源运行不存在")
        return AssistantProposalPreview(request_id=request.id, version=request.version, hash=proposal_hash, items=request.proposal.items)
