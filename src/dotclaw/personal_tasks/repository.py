"""个人事务仓储接口与业务异常。"""

from typing import Protocol

from .models import PersonalTask, PersonalTaskStatus


class PersonalTaskNotFound(Exception):
    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__(f"事务不存在：{task_id}")


class PersonalTaskVersionConflict(Exception):
    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__(f"事务版本冲突：{task_id}")


class PersonalTaskStorageError(Exception):
    pass


class PersonalTaskRepository(Protocol):
    async def create(self, task: PersonalTask) -> PersonalTask: ...
    async def get(self, task_id: str) -> PersonalTask | None: ...
    async def list(self, statuses: tuple[PersonalTaskStatus, ...] | None) -> tuple[PersonalTask, ...]: ...
    async def replace(self, task: PersonalTask, *, expected_version: int) -> PersonalTask: ...
    async def delete(self, task_id: str, *, expected_version: int) -> None: ...
