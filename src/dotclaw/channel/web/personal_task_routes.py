"""个人事务 HTTP 路由。"""

from fastapi import APIRouter, Query, Request, Response, status

from ...personal_tasks.models import PersonalTaskFilter, PersonalTaskStatus
from ...personal_tasks.service import PersonalTaskService
from .personal_task_schemas import (ChangePersonalTaskStatusRequest, CreatePersonalTaskRequest,
    PersonalTaskListResponse, PersonalTaskResponse, UpdatePersonalTaskRequest, deadline_to_domain,
    to_task_list_response, to_task_response)

router = APIRouter(prefix="/api/v1/personal-tasks", tags=["personal-tasks"])


def _service(request: Request) -> PersonalTaskService:
    return request.app.state.host.personal_task_service


@router.get("", response_model=PersonalTaskListResponse)
async def list_personal_tasks(request: Request, filter: PersonalTaskFilter = PersonalTaskFilter.ACTIVE):
    return to_task_list_response(await _service(request).list(filter))


@router.post("", response_model=PersonalTaskResponse, status_code=status.HTTP_201_CREATED)
async def create_personal_task(request: Request, body: CreatePersonalTaskRequest):
    return to_task_response(await _service(request).create(title=body.title, description=body.description, deadline=deadline_to_domain(body.deadline)))


@router.get("/{task_id}", response_model=PersonalTaskResponse)
async def get_personal_task(request: Request, task_id: str):
    return to_task_response(await _service(request).get(task_id))


@router.put("/{task_id}", response_model=PersonalTaskResponse)
async def update_personal_task(request: Request, task_id: str, body: UpdatePersonalTaskRequest):
    return to_task_response(await _service(request).update(task_id, title=body.title, description=body.description, deadline=deadline_to_domain(body.deadline), expected_version=body.expected_version))


@router.post("/{task_id}/status", response_model=PersonalTaskResponse)
async def change_personal_task_status(request: Request, task_id: str, body: ChangePersonalTaskStatusRequest):
    return to_task_response(await _service(request).change_status(task_id, status=body.status, expected_version=body.expected_version))


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_personal_task(request: Request, task_id: str, expected_version: int = Query(ge=1)) -> Response:
    await _service(request).delete(task_id, expected_version=expected_version)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
