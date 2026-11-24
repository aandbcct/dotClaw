"""助理请求、回执和展示快照的严格数据模型。"""

from datetime import date, datetime
import calendar
import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AtReminderRule(StrictModel):
    kind: Literal["at"]
    at: str

    @field_validator("at")
    @classmethod
    def rfc3339_with_timezone(cls, value: str) -> str:
        pattern = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
        if re.fullmatch(pattern, value) is None or datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("at 必须是带时区的 RFC3339 字符串")
        return value


class BeforeDeadlineReminderRule(StrictModel):
    kind: Literal["before_deadline"]
    offset_minutes: int = Field(ge=0)


class BeforeEventReminderRule(StrictModel):
    kind: Literal["before_event"]
    offset_minutes: int = Field(ge=0)


class PlanningDeadline(StrictModel):
    kind: Literal["date", "datetime"]
    value: date | datetime = Field(description="日期用 YYYY-MM-DD；时刻用带业务时区显式偏移的 RFC3339，例如 2026-11-26T20:00:00+08:00。")

    @field_validator("value", mode="before")
    @classmethod
    def parse_value(cls, value):
        if value is None or type(value) in (date, datetime):
            return value
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value)
        pattern = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
        if isinstance(value, str) and re.fullmatch(pattern, value):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed
        raise ValueError("planning 时间必须是日期或带时区的 RFC3339 时刻")

    @model_validator(mode="after")
    def kind_matches_value(self):
        if (self.kind == "date") != (type(self.value) is date):
            raise ValueError("planning 时间 kind 与 value 类型不匹配")
        return self


class TaskPlanningInput(StrictModel):
    kind: Literal["task", "event"] = "task"
    event_time: PlanningDeadline | None = Field(default=None, description="事件发生时间；精确时刻使用业务本地 RFC3339 偏移，不转换为 Z；只有日期则传 kind=date。")
    scheduled_time: PlanningDeadline | None = Field(default=None, description="任务安排时间；精确时刻使用业务本地 RFC3339 偏移，不转换为 Z。")
    time_text: str = ""
    related_task_id: str | None = None

    @model_validator(mode="after")
    def kind_specific_time(self):
        if self.kind == "task" and self.event_time is not None:
            raise ValueError("任务不能设置 event_time")
        if self.kind == "event" and self.scheduled_time is not None:
            raise ValueError("事件不能设置 scheduled_time")
        return self


class LocalTimeReminderRule(StrictModel):
    local_time: str

    @field_validator("local_time")
    @classmethod
    def hhmm_local_time(cls, value: str) -> str:
        if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) is None:
            raise ValueError("当地时刻必须为 HH:MM")
        return value


class BeforeDateReminderRule(LocalTimeReminderRule):
    kind: Literal["before_date"]
    days_before: int = Field(ge=0)


class DailyReminderRule(LocalTimeReminderRule):
    kind: Literal["daily"]


class WeeklyReminderRule(LocalTimeReminderRule):
    kind: Literal["weekly"]
    weekdays: list[int]

    @field_validator("weekdays")
    @classmethod
    def ordered_weekdays(cls, value: list[int]) -> list[int]:
        if not value or value != sorted(set(value)) or any(day not in range(7) for day in value):
            raise ValueError("weekly weekdays 必须是去重升序的 0..6")
        return value


class MonthlyReminderRule(LocalTimeReminderRule):
    kind: Literal["monthly"]
    day: int = Field(ge=1, le=31)


class YearlyReminderRule(LocalTimeReminderRule):
    kind: Literal["yearly"]
    month: int = Field(ge=1, le=12)
    day: int

    @model_validator(mode="after")
    def valid_calendar_day(self):
        if self.day < 1 or self.day > calendar.monthrange(2000, self.month)[1]:
            raise ValueError("yearly 月日无效")
        return self


ReminderRule = Annotated[
    AtReminderRule | BeforeDeadlineReminderRule | BeforeEventReminderRule | BeforeDateReminderRule | DailyReminderRule |
    WeeklyReminderRule | MonthlyReminderRule | YearlyReminderRule,
    Field(discriminator="kind"),
]


class TaskPatch(StrictModel):
    title: str | None = None
    description: str | None = None
    deadline: str | None = None
    planning: TaskPlanningInput | None = None


class TaskUpdateAction(StrictModel):
    action: Literal["update_task"]
    task_id: str
    expected_version: int = Field(ge=1)
    patch: TaskPatch


class TaskStatusAction(StrictModel):
    action: Literal["change_status"]
    task_id: str
    expected_version: int = Field(ge=1)
    status: Literal["completed", "cancelled"]


class ReminderUpdateAction(StrictModel):
    action: Literal["update_reminder"]
    task_id: str
    reminder_id: str
    expected_task_version: int = Field(ge=1)
    expected_version: int = Field(ge=1)
    rule: ReminderRule


class ReminderStatusAction(StrictModel):
    action: Literal["change_reminder_status"]
    task_id: str
    reminder_id: str
    expected_task_version: int = Field(ge=1)
    expected_version: int = Field(ge=1)
    status: Literal["pause", "resume", "cancel"]


AssistantBatchCommand = Annotated[
    TaskUpdateAction | TaskStatusAction | ReminderUpdateAction | ReminderStatusAction,
    Field(discriminator="action"),
]


class PlanItem(StrictModel):
    title: str
    description: str = ""
    deadline: str | None = None
    planning: TaskPlanningInput | None = None
    reminders: list[ReminderRule] = Field(default_factory=list)


class RequestCreateIntent(StrictModel):
    action: Literal["create_task"]
    title: str | None = None
    description: str | None = None
    deadline: str | None = None
    reminders: list[ReminderRule] = Field(default_factory=list)
    planning: TaskPlanningInput | None = None
    plan_items: list[PlanItem] | None = Field(default=None, max_length=20)


class RequestUpdateIntent(StrictModel):
    action: Literal["update_task"]
    task_id: str | None = None
    expected_version: int | None = Field(default=None, ge=1)
    patch: TaskPatch | None = None


class RequestStatusIntent(StrictModel):
    action: Literal["change_status"]
    task_id: str | None = None
    expected_version: int | None = Field(default=None, ge=1)
    status: Literal["completed", "cancelled"] | None = None


class RequestReminderIntent(StrictModel):
    action: Literal["reminder"]
    task_id: str | None = None
    reminder_id: str | None = None
    rule: ReminderRule | None = None
    expected_task_version: int | None = Field(default=None, ge=1)
    expected_version: int | None = Field(default=None, ge=1)


class RequestBatchIntent(StrictModel):
    action: Literal["batch"]
    summary: str | None = None
    items: list[AssistantBatchCommand] | None = Field(default=None, min_length=2, max_length=20)


AssistantRequestIntent = Annotated[
    RequestCreateIntent | RequestUpdateIntent | RequestStatusIntent | RequestReminderIntent | RequestBatchIntent,
    Field(discriminator="action"),
]


class AssistantSourceRef(StrictModel):
    run_id: str
    message_id: str


class TemporalValue(StrictModel):
    field: str
    original_text: str
    source_ref: AssistantSourceRef
    resolved_value: str | None
    timezone: str


class AssistantRequestPayload(StrictModel):
    intent_args: AssistantRequestIntent
    missing_fields: list[str] = Field(max_length=20)
    candidate_task_ids: list[str] = Field(max_length=50)
    selected_task_ids: list[str] = Field(max_length=50)
    temporal_values: list[TemporalValue]
    last_error: str | None = None
    planning_stage: Literal["needs_input", "drafting", "proposed"] | None = None

    @field_validator("candidate_task_ids")
    @classmethod
    def unique_candidates(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("candidate_task_ids 必须按顺序去重")
        return value

    @model_validator(mode="after")
    def valid_planning_stage(self):
        if self.planning_stage == "proposed":
            if not isinstance(self.intent_args, RequestCreateIntent) or not self.intent_args.plan_items or self.missing_fields:
                raise ValueError("proposed 计划必须包含任务且没有缺失字段")
            event_ids = set(self.selected_task_ids)
            for item in self.intent_args.plan_items:
                related_id = item.planning.related_task_id if item.planning is not None else None
                if event_ids and related_id not in event_ids:
                    raise ValueError("proposed 计划任务必须关联已选择事件")
                if related_id is not None and related_id not in event_ids:
                    raise ValueError("计划任务关联必须来自已选择事件")
        return self


class AssistantInputRef(StrictModel):
    run_id: str
    message_id: str
    created_at: datetime


class AssistantTaskRef(StrictModel):
    id: str
    title: str
    status: Literal["pending", "in_progress", "completed", "cancelled"]
    version: int = Field(ge=1)
    description: str = ""
    deadline: PlanningDeadline | None = None
    planning: TaskPlanningInput = Field(default_factory=TaskPlanningInput)


class AssistantReminderRef(StrictModel):
    id: str
    task_id: str
    status: Literal["active", "paused", "cancelled", "fired", "missed"]
    version: int = Field(ge=1)
    next_fire_at: datetime | None


class AssistantChange(StrictModel):
    id: str
    action: Literal["cancelled", "rescheduled", "paused"]
    reason: str | None
    next_fire_at: datetime | None
    version: int = Field(ge=1)


class AssistantProposalValues(StrictModel):
    title: str | None = None
    description: str | None = None
    status: Literal["pending", "in_progress", "completed", "cancelled", "active", "paused", "fired", "missed"] | None = None
    version: int | None = Field(default=None, ge=1)
    deadline: str | None = None
    planning: TaskPlanningInput | None = None
    reminder_summary: str | None = None
    reminder_rule: ReminderRule | None = None


class AssistantProposalItem(StrictModel):
    target: AssistantTaskRef | AssistantReminderRef
    before: AssistantProposalValues
    after: AssistantProposalValues
    command: AssistantBatchCommand

    @model_validator(mode="after")
    def proposal_command_has_change(self):
        if isinstance(self.command, TaskUpdateAction) and not self.command.patch.model_fields_set:
            raise ValueError("批量更新提案至少要包含一个修改字段")
        return self


class AssistantProposal(StrictModel):
    summary: str = Field(max_length=200)
    items: list[AssistantProposalItem] = Field(min_length=2, max_length=20)

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json", exclude_unset=True), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class AssistantRequest(StrictModel):
    id: str
    session_id: str
    kind: Literal["create_task", "update_task", "change_status", "reminder", "batch"]
    summary: str = Field(max_length=200)
    status: Literal["awaiting_input", "awaiting_confirmation", "resolved", "abandoned"]
    payload: AssistantRequestPayload
    source_refs: list[AssistantInputRef] = Field(max_length=20)
    proposal: AssistantProposal | None = None
    proposal_hash: str | None = None
    proposal_run_id: str | None = None
    result_refs: list[str]
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def kind_matches_payload(self):
        action_kind = {"create_task": "create_task", "update_task": "update_task", "change_status": "change_status", "reminder": "reminder", "batch": "batch"}
        if action_kind[self.kind] != self.payload.intent_args.action:
            raise ValueError("请求kind与草案动作不匹配")
        if self.status == "awaiting_input" and any(value is not None for value in (self.proposal, self.proposal_hash, self.proposal_run_id)):
            raise ValueError("等待用户输入的请求不能保留可执行提案")
        if self.status == "awaiting_confirmation" and (self.kind != "batch" or self.proposal is None or self.proposal_hash is None or self.proposal_run_id is None):
            raise ValueError("等待批量确认的请求必须保存提案、摘要和来源运行")
        return self


class TaskPageData(StrictModel):
    kind: Literal["task_page"]
    items: list[AssistantTaskRef] = Field(max_length=50)
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=50)
    timezone: str
    as_of: datetime


class TaskDetailData(StrictModel):
    kind: Literal["task_detail"]
    task: AssistantTaskRef
    reminders: list[AssistantReminderRef]


class RequestPageData(StrictModel):
    kind: Literal["request_page"]
    items: list["AssistantRequest"]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=50)


class RequestDetailData(StrictModel):
    kind: Literal["request_detail"]
    request: "AssistantRequest"


class ResultSetData(StrictModel):
    kind: Literal["result_set"]
    result_set: "AssistantResultSet"


AssistantOperationData = Annotated[
    TaskPageData | TaskDetailData | RequestPageData | RequestDetailData | ResultSetData,
    Field(discriminator="kind"),
]


class AssistantError(StrictModel):
    code: str
    message: str


class AssistantBatchItemResult(StrictModel):
    index: int = Field(ge=0)
    outcome: Literal["succeeded", "failed"]
    task_refs: list[AssistantTaskRef] = Field(default_factory=list)
    reminder_refs: list[AssistantReminderRef] = Field(default_factory=list)
    error: AssistantError | None = None


class AssistantOperationResult(StrictModel):
    schema_version: Literal[1] = 1
    operation: Literal[
        "tasks.query", "tasks.get", "tasks.create", "tasks.update", "tasks.status",
        "reminders.create", "reminders.update", "reminders.status", "requests.list",
        "requests.get", "requests.save", "requests.abandon", "results.get",
        "batch.prepare", "batch.apply", "plans.save", "plans.apply",
    ]
    outcome: Literal["succeeded", "failed", "partial"]
    data: AssistantOperationData | None = None
    task_refs: list[AssistantTaskRef] = Field(default_factory=list)
    reminder_refs: list[AssistantReminderRef] = Field(default_factory=list)
    changes: list[AssistantChange] = Field(default_factory=list)
    error: AssistantError | None = None
    result_set_id: str | None = None
    request_id: str | None = None
    items: list[AssistantBatchItemResult] = Field(default_factory=list, max_length=20)


class AssistantOperation(StrictModel):
    operation_key: str
    session_id: str
    run_id: str
    call_id: str
    tool_name: str
    arguments_hash: str
    request_id: str | None
    result: AssistantOperationResult
    created_at: datetime


class AssistantResultItem(StrictModel):
    id: str
    title: str
    status: Literal["pending", "in_progress", "completed", "cancelled"]
    version: int = Field(ge=1)
    deadline: str | None = None
    description: str = ""
    planning: TaskPlanningInput = Field(default_factory=TaskPlanningInput)
    reminder_summary: str | None = None


class AssistantResultSet(StrictModel):
    id: str
    session_id: str
    run_id: str
    call_id: str
    items: list[AssistantResultItem] = Field(max_length=50)
    created_at: datetime


class AssistantRunResults(StrictModel):
    schema_version: Literal[1] = 1
    run_id: str
    result_sets: list[AssistantResultSet]
    operations: list[AssistantOperation]


class AssistantProposalPreview(StrictModel):
    request_id: str
    version: int = Field(ge=1)
    hash: str
    items: list[AssistantProposalItem] = Field(min_length=2, max_length=20)


RequestPageData.model_rebuild()
RequestDetailData.model_rebuild()
ResultSetData.model_rebuild()
AssistantOperationResult.model_rebuild()
AssistantOperation.model_rebuild()
