"""助理工具的严格输入结构。"""

from typing import Annotated, Literal
from datetime import date, datetime
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from .models import AssistantRequestPayload, ReminderRule


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _parse_task_deadline(value: date | datetime | str | None) -> date | datetime | None:
    if value is None or type(value) in (date, datetime):
        return value
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return date.fromisoformat(value)
    timestamp_pattern = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
    if isinstance(value, str) and re.fullmatch(timestamp_pattern, value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed
    raise ValueError("截止时间必须是 YYYY-MM-DD 或带时区的 RFC3339 时刻")


def _parse_query_date(value: date | str | None) -> date | None:
    if value is None or type(value) is date:
        return value
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return date.fromisoformat(value)
    raise ValueError("查询日期必须为 YYYY-MM-DD")


class TaskQueryInput(StrictInput):
    filter: Literal["active", "all", "completed", "cancelled"] = "active"
    keyword: str = ""
    deadline_from: date | None = None
    deadline_until: date | None = None
    overdue_only: bool = False
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)

    @field_validator("deadline_from", "deadline_until", mode="before")
    @classmethod
    def parse_query_dates(cls, value):
        return _parse_query_date(value)


class TaskGetInput(StrictInput):
    task_id: str


class TaskCreateInput(StrictInput):
    title: str
    description: str = ""
    deadline: date | datetime | None = None
    reminders: list[ReminderRule] = Field(default_factory=list)
    request_id: str | None = Field(default=None, description="关联已有请求时必须与 expected_request_version 同时提供。")
    expected_request_version: int | None = Field(default=None, ge=1, description="关联已有请求时必须与 request_id 同时提供；取自 requests.get 返回的 data.request.version。")

    @field_validator("deadline", mode="before")
    @classmethod
    def parse_deadline(cls, value):
        return _parse_task_deadline(value)

    @model_validator(mode="after")
    def request_reference_is_paired(self):
        if (self.request_id is None) != (self.expected_request_version is None):
            raise PydanticCustomError("request_reference_pair", "request_id 与 expected_request_version 必须同时提供")
        return self


class TaskPatch(StrictInput):
    title: str | None = None
    description: str | None = None
    deadline: date | datetime | None = None

    @field_validator("deadline", mode="before")
    @classmethod
    def parse_deadline(cls, value):
        return _parse_task_deadline(value)

    @model_validator(mode="after")
    def patch_has_fields(self):
        if not self.model_fields_set:
            raise ValueError("patch 至少包含一个字段")
        return self


class TaskUpdateInput(StrictInput):
    task_id: str
    expected_version: int = Field(ge=1)
    patch: TaskPatch
    request_id: str | None = Field(default=None, description="关联已有请求时必须与 expected_request_version 同时提供。")
    expected_request_version: int | None = Field(default=None, ge=1, description="关联已有请求时必须与 request_id 同时提供；取自 requests.get 返回的 data.request.version。")

    @model_validator(mode="after")
    def request_reference_is_paired(self):
        if (self.request_id is None) != (self.expected_request_version is None):
            raise PydanticCustomError("request_reference_pair", "request_id 与 expected_request_version 必须同时提供")
        return self


class TaskStatusInput(StrictInput):
    task_id: str
    expected_version: int = Field(ge=1)
    status: Literal["completed", "cancelled"]
    request_id: str | None = Field(default=None, description="关联已有请求时必须与 expected_request_version 同时提供。")
    expected_request_version: int | None = Field(default=None, ge=1, description="关联已有请求时必须与 request_id 同时提供；取自 requests.get 返回的 data.request.version。")

    @model_validator(mode="after")
    def request_reference_is_paired(self):
        if (self.request_id is None) != (self.expected_request_version is None):
            raise PydanticCustomError("request_reference_pair", "request_id 与 expected_request_version 必须同时提供")
        return self


class ReminderCreateInput(StrictInput):
    task_id: str
    rule: ReminderRule
    expected_task_version: int = Field(ge=1)
    request_id: str | None = Field(default=None, description="关联已有请求时必须与 expected_request_version 同时提供。")
    expected_request_version: int | None = Field(default=None, ge=1, description="关联已有请求时必须与 request_id 同时提供；取自 requests.get 返回的 data.request.version。")

    @model_validator(mode="after")
    def request_reference_is_paired(self):
        if (self.request_id is None) != (self.expected_request_version is None):
            raise PydanticCustomError("request_reference_pair", "request_id 与 expected_request_version 必须同时提供")
        return self


class ReminderUpdateInput(StrictInput):
    task_id: str
    reminder_id: str
    expected_task_version: int = Field(ge=1)
    expected_version: int = Field(ge=1)
    rule: ReminderRule
    request_id: str | None = Field(default=None, description="关联已有请求时必须与 expected_request_version 同时提供。")
    expected_request_version: int | None = Field(default=None, ge=1, description="关联已有请求时必须与 request_id 同时提供；取自 requests.get 返回的 data.request.version。")

    @model_validator(mode="after")
    def request_reference_is_paired(self):
        if (self.request_id is None) != (self.expected_request_version is None):
            raise PydanticCustomError("request_reference_pair", "request_id 与 expected_request_version 必须同时提供")
        return self


class ReminderStatusInput(StrictInput):
    task_id: str
    reminder_id: str
    expected_task_version: int = Field(ge=1)
    expected_version: int = Field(ge=1)
    action: Literal["pause", "resume", "cancel"]
    request_id: str | None = Field(default=None, description="关联已有请求时必须与 expected_request_version 同时提供。")
    expected_request_version: int | None = Field(default=None, ge=1, description="关联已有请求时必须与 request_id 同时提供；取自 requests.get 返回的 data.request.version。")

    @model_validator(mode="after")
    def request_reference_is_paired(self):
        if (self.request_id is None) != (self.expected_request_version is None):
            raise PydanticCustomError("request_reference_pair", "request_id 与 expected_request_version 必须同时提供")
        return self


class RequestListInput(StrictInput):
    keyword: str = ""
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)


class RequestGetInput(StrictInput):
    id: str


class ResultSetGetInput(StrictInput):
    result_set_id: str


class RequestSourceInput(StrictInput):
    run_id: str
    message_id: str


class RequestSaveInput(StrictInput):
    id: str | None = Field(default=None, description="更新已有请求时必须与 expected_version 同时提供。")
    expected_version: int | None = Field(default=None, ge=1, description="更新已有请求时必须与 id 同时提供；取自 requests.get 返回的 data.request.version。")
    kind: Literal["create_task", "update_task", "change_status", "reminder", "batch"]
    summary: str = Field(max_length=200)
    payload: AssistantRequestPayload
    source_refs: list[RequestSourceInput] = Field(max_length=20)

    @model_validator(mode="after")
    def request_version_is_paired(self):
        if (self.id is None) != (self.expected_version is None):
            raise PydanticCustomError("request_version_pair", "id 与 expected_version 必须同时提供")
        return self


class RequestAbandonInput(StrictInput):
    id: str
    expected_version: int = Field(ge=1)


class BatchUpdateItem(StrictInput):
    action: Literal["update"]
    task_id: str
    expected_version: int = Field(ge=1)
    patch: TaskPatch


class BatchStatusItem(StrictInput):
    action: Literal["status"]
    task_id: str
    expected_version: int = Field(ge=1)
    status: Literal["completed", "cancelled"]


class BatchReminderUpdateItem(StrictInput):
    action: Literal["reminder_update"]
    task_id: str
    reminder_id: str
    expected_task_version: int = Field(ge=1)
    expected_version: int = Field(ge=1)
    rule: ReminderRule


class BatchReminderStatusItem(StrictInput):
    action: Literal["change_reminder_status"]
    task_id: str
    reminder_id: str
    expected_task_version: int = Field(ge=1)
    expected_version: int = Field(ge=1)
    status: Literal["pause", "resume", "cancel"]


BatchItem = Annotated[BatchUpdateItem | BatchStatusItem | BatchReminderUpdateItem | BatchReminderStatusItem, Field(discriminator="action")]


class BatchPrepareInput(StrictInput):
    request_id: str | None = Field(default=None, description="关联已有请求时必须与 expected_request_version 同时提供。")
    expected_request_version: int | None = Field(default=None, ge=1, description="关联已有请求时必须与 request_id 同时提供；取自 requests.get 返回的 data.request.version。")
    summary: str = Field(max_length=200)
    items: list[BatchItem] = Field(min_length=2, max_length=20)

    @model_validator(mode="after")
    def request_reference_is_paired(self):
        if (self.request_id is None) != (self.expected_request_version is None):
            raise PydanticCustomError("request_reference_pair", "request_id 与 expected_request_version 必须同时提供")
        return self


class BatchApplyInput(StrictInput):
    request_id: str
    expected_version: int = Field(ge=1)
    proposal_hash: str
