"""个人事务 HTTP 路由。"""

from datetime import date

from fastapi import APIRouter, Query, Request, Response, status

from ...personal_tasks.models import PersonalTaskFilter, PersonalTaskQuery, PlanFilter
from ...personal_tasks.service import PersonalTaskService
from .personal_task_schemas import (ChangePersonalTaskStatusRequest, CreatePersonalTaskRequest,
    PersonalTaskDetailResponse, PersonalTaskListItemResponse, PersonalTaskListResponse, PersonalTaskResponse,
    ReminderSummaryResponse, UpdatePersonalTaskRequest, deadline_to_domain, planning_to_domain, to_reminder_response, to_task_response)

router = APIRouter(prefix="/api/v1/personal-tasks", tags=["personal-tasks"])


def _service(request: Request) -> PersonalTaskService:
    return request.app.state.host.personal_task_service


@router.get("", response_model=PersonalTaskListResponse)
async def list_personal_tasks(request: Request, filter: PersonalTaskFilter = PersonalTaskFilter.ACTIVE,
    keyword: str = "", deadline_from: date | None = None, deadline_until: date | None = None,
    overdue_only: bool = False, offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100),
    plan_filter: PlanFilter = PlanFilter.ALL):
    result = await _service(request).query(PersonalTaskQuery(filter, keyword, deadline_from, deadline_until,
        overdue_only, offset, limit, plan_filter))
    items = []
    for index, item in enumerate(result.items):
        response = to_task_response(item)
        items.append(PersonalTaskListItemResponse(id=response.id, title=response.title, description=response.description,
            status=response.status, deadline=response.deadline, planning=response.planning, created_at=response.created_at,
            updated_at=response.updated_at, completed_at=response.completed_at, version=response.version,
            is_overdue=response.is_overdue, reminder_changes=response.reminder_changes, reminder_summary=ReminderSummaryResponse(
                active_count=result.reminder_summaries[index].active_count,
                next_fire_at=result.reminder_summaries[index].next_fire_at)))
    return PersonalTaskListResponse(
        items=items, timezone=result.timezone, as_of=result.as_of,
        total=result.total, offset=result.offset, limit=result.limit, has_more=result.offset + len(result.items) < result.total,
    )


@router.post("", response_model=PersonalTaskResponse, status_code=status.HTTP_201_CREATED)
async def create_personal_task(request: Request, body: CreatePersonalTaskRequest):
    reminders = tuple(rule.model_dump() for rule in body.reminders)
    return to_task_response(await _service(request).create(title=body.title, description=body.description,
        deadline=deadline_to_domain(body.deadline), reminders=reminders, planning=planning_to_domain(body.planning)))


@router.get("/{task_id}", response_model=PersonalTaskDetailResponse)
async def get_personal_task(request: Request, task_id: str):
    view, reminders = await _service(request).get_detail(task_id)
    response = to_task_response(view)
    return PersonalTaskDetailResponse(id=response.id, title=response.title, description=response.description,
        status=response.status, deadline=response.deadline, planning=response.planning, created_at=response.created_at,
        updated_at=response.updated_at, completed_at=response.completed_at, version=response.version,
        is_overdue=response.is_overdue, reminder_changes=response.reminder_changes,
        reminders=[to_reminder_response(item) for item in reminders])


@router.put("/{task_id}", response_model=PersonalTaskResponse)
async def update_personal_task(request: Request, task_id: str, body: UpdatePersonalTaskRequest):
    return to_task_response(await _service(request).update(task_id, title=body.title, description=body.description, deadline=deadline_to_domain(body.deadline), expected_version=body.expected_version, planning=planning_to_domain(body.planning) if body.planning is not None else None))


@router.post("/{task_id}/status", response_model=PersonalTaskResponse)
async def change_personal_task_status(request: Request, task_id: str, body: ChangePersonalTaskStatusRequest):
    return to_task_response(await _service(request).change_status(task_id, status=body.status, expected_version=body.expected_version))


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_personal_task(request: Request, task_id: str, expected_version: int = Query(ge=1)) -> Response:
    await _service(request).delete(task_id, expected_version=expected_version)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
