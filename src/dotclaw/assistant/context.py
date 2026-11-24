"""为助理模型上下文生成有界的业务状态摘要。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .models import AssistantInputRef
from .service import ASSISTANT_SESSION_ID
from .sqlite_repository import SqliteAssistantRepository


class AssistantContextReader:
    """读取当前输入时间、未结请求和最近展示快照，不访问事务仓储。"""

    def __init__(
        self,
        repository: SqliteAssistantRepository,
        timezone: ZoneInfo,
        scheduler_enabled: bool,
    ) -> None:
        self._repository = repository
        self._timezone = timezone
        self._scheduler_enabled = scheduler_enabled

    async def read(self, session_id: str, input_ref: AssistantInputRef) -> str:
        """返回版本化 JSON；数量和页内顺序严格保持有界。"""
        if session_id != ASSISTANT_SESSION_ID:
            raise ValueError("助理上下文只允许读取固定助理会话")
        requests, request_total = await self._repository.list_requests(
            session_id,
            keyword="",
            offset=0,
            limit=10,
            statuses=("awaiting_input", "awaiting_confirmation"),
        )
        result_sets = await self._repository.list_recent_result_sets(session_id, limit=3)
        input_local = input_ref.created_at.astimezone(self._timezone)
        input_date = input_local.date()
        weekdays = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
        payload = {
            "schema_version": 1,
            "time": {
                "now_utc": _timestamp(datetime.now(UTC)),
                "business_timezone": self._timezone.key,
                "input_created_at": _timestamp(input_ref.created_at),
                "input_local": input_local.isoformat(timespec="seconds"),
                "date_weekday_reference": [
                    {
                        "date": (input_date + timedelta(days=offset)).isoformat(),
                        "weekday": weekdays[(input_date + timedelta(days=offset)).weekday()],
                        "days_from_input": offset,
                    }
                    for offset in range(14)
                ],
            },
            "requests": {
                "items": [
                    {
                        "id": request.id,
                        "kind": request.kind,
                        "summary": request.summary,
                        "status": request.status,
                        "version": request.version,
                        "updated_at": _timestamp(request.updated_at),
                        "planning_stage": request.payload.planning_stage,
                        "missing_fields": request.payload.missing_fields,
                        "event_ids": request.payload.selected_task_ids,
                    }
                    for request in requests
                ],
                "total": request_total,
                "has_more": request_total > len(requests),
            },
            "result_sets": [
                {
                    "id": result_set.id,
                    "created_at": _timestamp(result_set.created_at),
                    "total": len(result_set.items),
                    "items": [
                        {
                            "id": item.id,
                            "title": item.title,
                            "version": item.version,
                        }
                        for item in result_set.items[:20]
                    ],
                    "has_more": len(result_set.items) > 20,
                }
                for result_set in result_sets
            ],
            "scheduler": {"enabled": self._scheduler_enabled},
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
