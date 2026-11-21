"""个人事务提醒 HTTP 路由。"""

from datetime import UTC, datetime

from fastapi import APIRouter, Request, status

from .reminder_schemas import CreateReminderRequest, ReminderListResponse, ReminderResponse, ReminderStatusRequest, UpdateReminderRequest

router = APIRouter(prefix="/api/v1/personal-tasks/{task_id}/reminders", tags=["reminders"])


def _response(reminder) -> ReminderResponse:
    return ReminderResponse(id=reminder.id, task_id=reminder.task_id, rule=reminder.rule, timezone=reminder.timezone, status=reminder.status.value, reason=reminder.reason, next_fire_at=reminder.next_fire_at, version=reminder.version, created_at=reminder.created_at, updated_at=reminder.updated_at)


@router.get("", response_model=ReminderListResponse)
async def list_reminders(request: Request, task_id: str):
    host = request.app.state.host
    items = await host.reminder_service.list(task_id)
    last_scan = await host.reminder_service.last_scan_at()
    now = datetime.now(UTC)
    return ReminderListResponse(items=[_response(item) for item in items], timezone=host._config.personal_tasks.timezone, scheduler_enabled=host.scheduler_enabled, last_scan_at=last_scan, scan_lag_seconds=max(0.0, (now-last_scan).total_seconds()) if last_scan else None)


@router.post("", response_model=ReminderResponse, status_code=status.HTTP_201_CREATED)
async def create_reminder(request: Request, task_id: str, body: CreateReminderRequest):
    return _response(await request.app.state.host.reminder_service.create(task_id, rule=body.rule.model_dump(), expected_task_version=body.expected_task_version))


@router.put("/{reminder_id}", response_model=ReminderResponse)
async def update_reminder(request: Request, task_id: str, reminder_id: str, body: UpdateReminderRequest):
    return _response(await request.app.state.host.reminder_service.update(task_id, reminder_id, rule=body.rule.model_dump(), expected_version=body.expected_version, expected_task_version=body.expected_task_version))


@router.post("/{reminder_id}/status", response_model=ReminderResponse)
async def change_reminder_status(request: Request, task_id: str, reminder_id: str, body: ReminderStatusRequest):
    return _response(await request.app.state.host.reminder_service.change_status(task_id, reminder_id, action=body.action, expected_version=body.expected_version, expected_task_version=body.expected_task_version))
