"""助理记录仓储及共享 SQLite 事务上下文。"""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from .models import AssistantOperation, AssistantRequest, AssistantResultSet
from ..personal_tasks.repository import PersonalTaskStorageError


class AssistantStorageError(PersonalTaskStorageError):
    """助理持久化结构或数据库操作失败。"""


class AssistantRequestConflict(RuntimeError):
    """请求不存在、版本冲突或状态不允许修改。"""


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def migrate_assistant_schema(connection: sqlite3.Connection) -> None:
    """在调用方事务内创建助理三张表及索引。"""
    try:
        connection.execute("""CREATE TABLE IF NOT EXISTS assistant_requests (
            id TEXT PRIMARY KEY NOT NULL, session_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('create_task','update_task','change_status','reminder','batch')),
            summary TEXT NOT NULL CHECK(length(summary)<=200),
            status TEXT NOT NULL CHECK(status IN ('awaiting_input','awaiting_confirmation','resolved','abandoned')),
            payload_json TEXT NOT NULL, source_refs_json TEXT NOT NULL, proposal_json TEXT,
            proposal_hash TEXT, proposal_run_id TEXT, result_refs_json TEXT NOT NULL DEFAULT '[]',
            version INTEGER NOT NULL CHECK(version>=1), created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS assistant_operations (
            operation_key TEXT PRIMARY KEY NOT NULL, session_id TEXT NOT NULL, run_id TEXT NOT NULL,
            call_id TEXT NOT NULL, tool_name TEXT NOT NULL, arguments_hash TEXT NOT NULL,
            request_id TEXT, result_json TEXT NOT NULL, created_at TEXT NOT NULL)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS assistant_result_sets (
            id TEXT PRIMARY KEY NOT NULL, session_id TEXT NOT NULL, run_id TEXT NOT NULL,
            call_id TEXT NOT NULL, items_json TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(session_id,run_id,call_id))""")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_assistant_requests_session_status_updated ON assistant_requests(session_id,status,updated_at DESC,id ASC)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_assistant_operations_session_run_created ON assistant_operations(session_id,run_id,created_at,operation_key)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_assistant_result_sets_session_created ON assistant_result_sets(session_id,created_at,id)")
    except sqlite3.Error as error:
        raise AssistantStorageError("助理数据库迁移失败") from error


def validate_assistant_schema(connection: sqlite3.Connection) -> None:
    """核对v3助理表结构；不对已标记v3的数据库做修补。"""
    for query in (
        "SELECT id,session_id,kind,summary,status,payload_json,source_refs_json,proposal_json,proposal_hash,proposal_run_id,result_refs_json,version,created_at,updated_at FROM assistant_requests LIMIT 0",
        "SELECT operation_key,session_id,run_id,call_id,tool_name,arguments_hash,request_id,result_json,created_at FROM assistant_operations LIMIT 0",
        "SELECT id,session_id,run_id,call_id,items_json,created_at FROM assistant_result_sets LIMIT 0",
    ):
        try:
            connection.execute(query)
        except sqlite3.Error as error:
            raise AssistantStorageError("v3助理数据库结构缺失或不兼容") from error
    indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    if not {"idx_assistant_requests_session_status_updated", "idx_assistant_operations_session_run_created", "idx_assistant_result_sets_session_created"}.issubset(indexes):
        raise AssistantStorageError("v3助理数据库索引缺失")


class SqliteAssistantTransaction:
    """每次助理操作专用的串行写事务。"""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self.connection: sqlite3.Connection | None = None

    def _open(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._db_path, timeout=5.0, isolation_level=None, check_same_thread=False)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            return connection
        except BaseException:
            connection.close()
            raise

    async def __aenter__(self) -> sqlite3.Connection:
        self.connection = await asyncio.to_thread(self._open)
        return self.connection

    @staticmethod
    def _finish(connection: sqlite3.Connection, commit: bool) -> None:
        try:
            connection.execute("COMMIT" if commit else "ROLLBACK")
        except sqlite3.Error:
            if commit and connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        if self.connection is not None:
            await asyncio.to_thread(self._finish, self.connection, exc_type is None)
            self.connection = None
        return False


class SqliteAssistantRepository:
    def __init__(self, db_path: Path):
        self._db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        connection = None
        try:
            connection = sqlite3.connect(self._db_path, timeout=5.0, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            return connection
        except sqlite3.Error as error:
            if connection is not None:
                connection.close()
            raise AssistantStorageError("打开助理数据库失败") from error

    @staticmethod
    def _request(row: sqlite3.Row) -> AssistantRequest:
        try:
            data = {
                "id": row["id"], "session_id": row["session_id"], "kind": row["kind"],
                "summary": row["summary"], "status": row["status"],
                "payload": json.loads(row["payload_json"]), "source_refs": json.loads(row["source_refs_json"]),
                "proposal": json.loads(row["proposal_json"]) if row["proposal_json"] is not None else None,
                "proposal_hash": row["proposal_hash"], "proposal_run_id": row["proposal_run_id"],
                "result_refs": json.loads(row["result_refs_json"]), "version": row["version"],
                "created_at": row["created_at"], "updated_at": row["updated_at"],
            }
            return AssistantRequest.model_validate_json(json.dumps(data, ensure_ascii=False))
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as error:
            raise AssistantStorageError("助理请求 JSON 数据损坏或结构未知") from error

    @staticmethod
    def _operation(row: sqlite3.Row) -> AssistantOperation:
        try:
            result = json.loads(row["result_json"])
            data = {
                "operation_key": row["operation_key"], "session_id": row["session_id"],
                "run_id": row["run_id"], "call_id": row["call_id"], "tool_name": row["tool_name"],
                "arguments_hash": row["arguments_hash"], "request_id": row["request_id"],
                "result": result, "created_at": row["created_at"],
            }
            return AssistantOperation.model_validate_json(json.dumps(data, ensure_ascii=False))
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as error:
            raise AssistantStorageError("助理回执 JSON 数据损坏或结构未知") from error

    @staticmethod
    def _result_set(row: sqlite3.Row) -> AssistantResultSet:
        try:
            data = {
                "id": row["id"], "session_id": row["session_id"], "run_id": row["run_id"],
                "call_id": row["call_id"], "items": json.loads(row["items_json"]),
                "created_at": row["created_at"],
            }
            return AssistantResultSet.model_validate_json(json.dumps(data, ensure_ascii=False))
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as error:
            raise AssistantStorageError("助理结果快照 JSON 数据损坏或结构未知") from error

    async def get_request(self, session_id: str, id: str, *, connection: sqlite3.Connection | None = None) -> AssistantRequest | None:
        return await asyncio.to_thread(self._get_request_sync, session_id, id, connection)

    def _get_request_sync(self, session_id: str, id: str, connection: sqlite3.Connection | None) -> AssistantRequest | None:
        owned = connection is None
        connection = connection or self._connect()
        try:
            row = connection.execute("SELECT * FROM assistant_requests WHERE session_id=? AND id=?", (session_id, id)).fetchone()
            return self._request(row) if row else None
        except sqlite3.Error as error:
            raise AssistantStorageError("读取助理请求失败") from error
        finally:
            if owned: connection.close()

    async def list_requests(self, session_id: str, *, keyword: str, offset: int, limit: int, statuses: tuple[str, ...] | None, connection: sqlite3.Connection | None = None) -> tuple[list[AssistantRequest], int]:
        return await asyncio.to_thread(self._list_requests_sync, session_id, keyword, offset, limit, statuses, connection)

    def _list_requests_sync(self, session_id: str, keyword: str, offset: int, limit: int, statuses: tuple[str, ...] | None, connection: sqlite3.Connection | None) -> tuple[list[AssistantRequest], int]:
        owned = connection is None
        connection = connection or self._connect()
        try:
            clauses, values = ["session_id=?"], [session_id]
            if keyword:
                clauses.append("summary LIKE ?"); values.append(f"%{keyword}%")
            if statuses is not None:
                clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")"); values.extend(statuses)
            where = " AND ".join(clauses)
            total = connection.execute(f"SELECT count(*) FROM assistant_requests WHERE {where}", values).fetchone()[0]
            rows = connection.execute(f"SELECT * FROM assistant_requests WHERE {where} ORDER BY updated_at DESC,id ASC LIMIT ? OFFSET ?", (*values, limit, offset)).fetchall()
            return [self._request(row) for row in rows], total
        except sqlite3.Error as error:
            raise AssistantStorageError("列出助理请求失败") from error
        finally:
            if owned: connection.close()

    async def save_request(self, request: AssistantRequest, *, expected_version: int | None, connection: sqlite3.Connection) -> AssistantRequest:
        return await asyncio.to_thread(self._save_request_sync, request, expected_version, connection)

    def _save_request_sync(self, request: AssistantRequest, expected_version: int | None, connection: sqlite3.Connection) -> AssistantRequest:
        payload_json = request.payload.model_dump_json(exclude_unset=True)
        source_json = json.dumps([item.model_dump(mode="json") for item in request.source_refs], ensure_ascii=False, separators=(",", ":"))
        result_refs_json = json.dumps(request.result_refs, ensure_ascii=False, separators=(",", ":"))
        proposal_json = request.proposal.canonical_json() if request.proposal is not None else None
        if len(payload_json.encode("utf-8")) > 16 * 1024:
            raise AssistantStorageError("助理请求草案超过 16 KiB")
        try:
            if expected_version is None:
                if request.version != 1 or request.status not in ("awaiting_input", "awaiting_confirmation"):
                    raise AssistantRequestConflict(request.id)
                connection.execute("INSERT INTO assistant_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    request.id, request.session_id, request.kind, request.summary, request.status, payload_json, source_json,
                    proposal_json, request.proposal_hash, request.proposal_run_id, result_refs_json, request.version,
                    _stamp(request.created_at), _stamp(request.updated_at)))
            else:
                allowed_from = {
                    "awaiting_input": ("awaiting_input", "awaiting_confirmation"),
                    "awaiting_confirmation": ("awaiting_input",),
                    "resolved": ("awaiting_input", "awaiting_confirmation"),
                    "abandoned": ("awaiting_input", "awaiting_confirmation"),
                }[request.status]
                if request.version != expected_version + 1:
                    raise AssistantRequestConflict(request.id)
                status_marks = ",".join("?" for _ in allowed_from)
                cursor = connection.execute(f"""UPDATE assistant_requests SET kind=?,summary=?,status=?,payload_json=?,source_refs_json=?,proposal_json=?,proposal_hash=?,proposal_run_id=?,result_refs_json=?,version=?,updated_at=? WHERE session_id=? AND id=? AND version=? AND status IN ({status_marks})""", (
                    request.kind, request.summary, request.status, payload_json, source_json, proposal_json, request.proposal_hash,
                    request.proposal_run_id, result_refs_json, request.version, _stamp(request.updated_at), request.session_id,
                    request.id, expected_version, *allowed_from))
                if cursor.rowcount != 1:
                    raise AssistantRequestConflict(request.id)
            return request
        except sqlite3.IntegrityError as error:
            raise AssistantRequestConflict(request.id) from error
        except sqlite3.Error as error:
            raise AssistantStorageError("保存助理请求失败") from error

    async def get_operation(self, key: str, *, connection: sqlite3.Connection | None = None) -> AssistantOperation | None:
        return await asyncio.to_thread(self._get_operation_sync, key, connection)

    def _get_operation_sync(self, key: str, connection: sqlite3.Connection | None) -> AssistantOperation | None:
        owned = connection is None; connection = connection or self._connect()
        try:
            row = connection.execute("SELECT * FROM assistant_operations WHERE operation_key=?", (key,)).fetchone()
            return self._operation(row) if row else None
        except sqlite3.Error as error:
            raise AssistantStorageError("读取助理回执失败") from error
        finally:
            if owned: connection.close()

    async def insert_operation(self, operation: AssistantOperation, *, connection: sqlite3.Connection) -> None:
        await asyncio.to_thread(self._insert_operation_sync, operation, connection)

    def _insert_operation_sync(self, operation: AssistantOperation, connection: sqlite3.Connection) -> None:
        try:
            connection.execute("INSERT INTO assistant_operations VALUES(?,?,?,?,?,?,?,?,?)", (
                operation.operation_key, operation.session_id, operation.run_id, operation.call_id, operation.tool_name,
                operation.arguments_hash, operation.request_id, operation.result.model_dump_json(), _stamp(operation.created_at)))
        except sqlite3.IntegrityError as error:
            raise AssistantRequestConflict(operation.operation_key) from error
        except sqlite3.Error as error:
            raise AssistantStorageError("写入助理回执失败") from error

    async def save_result_set(self, result_set: AssistantResultSet, *, connection: sqlite3.Connection) -> None:
        await asyncio.to_thread(self._save_result_set_sync, result_set, connection)

    def _save_result_set_sync(self, result_set: AssistantResultSet, connection: sqlite3.Connection) -> None:
        try:
            connection.execute("INSERT INTO assistant_result_sets VALUES(?,?,?,?,?,?)", (
                result_set.id, result_set.session_id, result_set.run_id, result_set.call_id,
                json.dumps([item.model_dump(mode="json") for item in result_set.items], ensure_ascii=False, separators=(",", ":")),
                _stamp(result_set.created_at)))
        except sqlite3.IntegrityError as error:
            raise AssistantRequestConflict(result_set.id) from error
        except sqlite3.Error as error:
            raise AssistantStorageError("保存助理结果快照失败") from error

    async def get_result_set(self, session_id: str, id: str, *, connection: sqlite3.Connection | None = None) -> AssistantResultSet | None:
        return await asyncio.to_thread(self._get_result_set_sync, session_id, id, connection)

    def _get_result_set_sync(self, session_id: str, id: str, connection: sqlite3.Connection | None) -> AssistantResultSet | None:
        owned = connection is None; connection = connection or self._connect()
        try:
            row = connection.execute("SELECT * FROM assistant_result_sets WHERE session_id=? AND id=?", (session_id, id)).fetchone()
            return self._result_set(row) if row else None
        except sqlite3.Error as error:
            raise AssistantStorageError("读取助理结果快照失败") from error
        finally:
            if owned: connection.close()

    async def list_run_results(self, session_id: str, run_id: str) -> tuple[list[AssistantResultSet], list[AssistantOperation]]:
        return await asyncio.to_thread(self._list_run_results_sync, session_id, run_id)

    def _list_run_results_sync(self, session_id: str, run_id: str) -> tuple[list[AssistantResultSet], list[AssistantOperation]]:
        connection = self._connect()
        try:
            sets = connection.execute("SELECT * FROM assistant_result_sets WHERE session_id=? AND run_id=? ORDER BY created_at,id", (session_id, run_id)).fetchall()
            operations = connection.execute("SELECT * FROM assistant_operations WHERE session_id=? AND run_id=? ORDER BY created_at,operation_key", (session_id, run_id)).fetchall()
            return [self._result_set(row) for row in sets], [self._operation(row) for row in operations]
        except sqlite3.Error as error:
            raise AssistantStorageError("读取助理运行结果失败") from error
        finally:
            connection.close()

    async def has_session_data(self, session_id: str) -> bool:
        return await asyncio.to_thread(self._has_session_data_sync, session_id)

    def _has_session_data_sync(self, session_id: str) -> bool:
        connection = self._connect()
        try:
            return any(connection.execute(f"SELECT 1 FROM {table} WHERE session_id=? LIMIT 1", (session_id,)).fetchone() for table in ("assistant_requests", "assistant_operations", "assistant_result_sets"))
        finally:
            connection.close()

    async def list_recent_result_sets(self, session_id: str, limit: int) -> list[AssistantResultSet]:
        return await asyncio.to_thread(self._list_recent_result_sets_sync, session_id, limit)

    def _list_recent_result_sets_sync(self, session_id: str, limit: int) -> list[AssistantResultSet]:
        connection = self._connect()
        try:
            rows = connection.execute("SELECT * FROM assistant_result_sets WHERE session_id=? ORDER BY created_at DESC,id ASC LIMIT ?", (session_id, limit)).fetchall()
            return [self._result_set(row) for row in rows]
        except sqlite3.Error as error:
            raise AssistantStorageError("列出助理结果快照失败") from error
        finally:
            connection.close()
