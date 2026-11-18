"""个人事务 SQLite 持久化适配器。"""

import asyncio
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

from .models import PersonalTask, PersonalTaskStatus, TaskDeadline
from .repository import PersonalTaskNotFound, PersonalTaskStorageError, PersonalTaskVersionConflict


class SqlitePersonalTaskRepository:
    def __init__(self, db_path: Path):
        self._db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        connection = None
        try:
            connection = sqlite3.connect(self._db_path, timeout=5.0, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA synchronous=FULL")
            return connection
        except sqlite3.Error as error:
            if connection is not None:
                connection.close()
            raise PersonalTaskStorageError("打开事务数据库失败") from error

    @staticmethod
    def _encode(task: PersonalTask) -> tuple:
        deadline_kind = task.deadline.kind if task.deadline else None
        deadline_value = None
        if task.deadline:
            deadline_value = task.deadline.value.isoformat() if deadline_kind == "date" else task.deadline.value.isoformat(timespec="microseconds").replace("+00:00", "Z")
        return (task.id, task.title, task.description, task.status.value, deadline_kind, deadline_value,
                task.created_at.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                task.updated_at.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                task.completed_at.isoformat(timespec="microseconds").replace("+00:00", "Z") if task.completed_at else None,
                task.version)

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> PersonalTask:
        kind, value = row["deadline_kind"], row["deadline_value"]
        deadline = None if kind is None else TaskDeadline(kind, date.fromisoformat(value) if kind == "date" else datetime.fromisoformat(value.replace("Z", "+00:00")))
        return PersonalTask(row["id"], row["title"], row["description"], PersonalTaskStatus(row["status"]),
                            deadline, datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")).astimezone(UTC),
                            datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00")).astimezone(UTC),
                            datetime.fromisoformat(row["completed_at"].replace("Z", "+00:00")).astimezone(UTC) if row["completed_at"] else None,
                            row["version"])

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
            if version == 0 and tables:
                raise PersonalTaskStorageError("版本 0 数据库已有用户表")
            if version == 0:
                connection.execute("""CREATE TABLE personal_tasks (
                    id TEXT PRIMARY KEY NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','in_progress','completed','cancelled')),
                    deadline_kind TEXT CHECK(deadline_kind IN ('date','datetime') OR deadline_kind IS NULL),
                    deadline_value TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    completed_at TEXT, version INTEGER NOT NULL CHECK(version >= 1),
                    CHECK((deadline_kind IS NULL) = (deadline_value IS NULL)),
                    CHECK((status = 'completed') = (completed_at IS NOT NULL)))""")
                connection.execute("PRAGMA user_version=1")
            elif version == 1:
                connection.execute("SELECT id,title,description,status,deadline_kind,deadline_value,created_at,updated_at,completed_at,version FROM personal_tasks LIMIT 0")
            else:
                raise PersonalTaskStorageError(f"不支持的数据库版本：{version}")
            connection.execute("COMMIT")
        except Exception as error:
            connection.rollback()
            if isinstance(error, (PersonalTaskStorageError, sqlite3.Error)):
                if isinstance(error, sqlite3.Error):
                    raise PersonalTaskStorageError("初始化事务数据库失败") from error
                raise
            raise
        finally:
            connection.close()

    async def create(self, task: PersonalTask) -> PersonalTask:
        return await asyncio.to_thread(self._create_sync, task)

    def _create_sync(self, task: PersonalTask) -> PersonalTask:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO personal_tasks VALUES (?,?,?,?,?,?,?,?,?,?)", self._encode(task))
            connection.execute("COMMIT")
            return task
        except sqlite3.Error as error:
            connection.rollback()
            raise PersonalTaskStorageError("创建事务失败") from error
        finally:
            connection.close()

    async def get(self, task_id: str) -> PersonalTask | None:
        return await asyncio.to_thread(self._get_sync, task_id)

    def _get_sync(self, task_id: str) -> PersonalTask | None:
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM personal_tasks WHERE id=?", (task_id,)).fetchone()
            return self._row_to_task(row) if row else None
        except (sqlite3.Error, ValueError, TypeError) as error:
            raise PersonalTaskStorageError("读取事务失败") from error
        finally:
            connection.close()

    async def list(self, statuses: tuple[PersonalTaskStatus, ...] | None) -> tuple[PersonalTask, ...]:
        return await asyncio.to_thread(self._list_sync, statuses)

    def _list_sync(self, statuses: tuple[PersonalTaskStatus, ...] | None) -> tuple[PersonalTask, ...]:
        connection = self._connect()
        try:
            if statuses is None:
                rows = connection.execute("SELECT * FROM personal_tasks").fetchall()
            else:
                marks = ",".join("?" for _ in statuses)
                rows = connection.execute(f"SELECT * FROM personal_tasks WHERE status IN ({marks})", tuple(s.value for s in statuses)).fetchall()
            return tuple(self._row_to_task(row) for row in rows)
        except (sqlite3.Error, ValueError, TypeError) as error:
            raise PersonalTaskStorageError("列出事务失败") from error
        finally:
            connection.close()

    async def replace(self, task: PersonalTask, *, expected_version: int) -> PersonalTask:
        return await asyncio.to_thread(self._replace_sync, task, expected_version)

    def _replace_sync(self, task: PersonalTask, expected_version: int) -> PersonalTask:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            values = self._encode(task)
            cursor = connection.execute("UPDATE personal_tasks SET title=?,description=?,status=?,deadline_kind=?,deadline_value=?,updated_at=?,completed_at=?,version=? WHERE id=? AND version=?",
                (values[1], values[2], values[3], values[4], values[5], values[7], values[8], values[9], task.id, expected_version))
            if cursor.rowcount == 0:
                exists = connection.execute("SELECT 1 FROM personal_tasks WHERE id=?", (task.id,)).fetchone()
                raise PersonalTaskVersionConflict(task.id) if exists else PersonalTaskNotFound(task.id)
            connection.execute("COMMIT")
            return task
        except (PersonalTaskNotFound, PersonalTaskVersionConflict):
            connection.rollback()
            raise
        except sqlite3.Error as error:
            connection.rollback()
            raise PersonalTaskStorageError("更新事务失败") from error
        finally:
            connection.close()

    async def delete(self, task_id: str, *, expected_version: int) -> None:
        await asyncio.to_thread(self._delete_sync, task_id, expected_version)

    def _delete_sync(self, task_id: str, expected_version: int) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute("DELETE FROM personal_tasks WHERE id=? AND version=?", (task_id, expected_version))
            if cursor.rowcount == 0:
                exists = connection.execute("SELECT 1 FROM personal_tasks WHERE id=?", (task_id,)).fetchone()
                raise PersonalTaskVersionConflict(task_id) if exists else PersonalTaskNotFound(task_id)
            connection.execute("COMMIT")
        except (PersonalTaskNotFound, PersonalTaskVersionConflict):
            connection.rollback()
            raise
        except sqlite3.Error as error:
            connection.rollback()
            raise PersonalTaskStorageError("删除事务失败") from error
        finally:
            connection.close()
