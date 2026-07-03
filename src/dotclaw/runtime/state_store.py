"""StateStore —— State 持久化的抽象接口 + SQLite 实现。

StateStore 负责 State 的 CRUD，是 Runtime 的状态持久化层。
设计为抽象接口，当前默认实现为 SQLiteStateStore，
后续可替换为 PostgreSQL、Redis 等后端。

所有持久化方法都是异步的，兼容 Runtime 的 async 执行模型。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .state import State


# ============================================================================
# StateStore — 抽象接口
# ============================================================================

class StateStore(ABC):
    """State 持久化的抽象接口。

    所有 State 存储后端必须实现此接口。
    接口设计为最小集：get / save / delete，覆盖基本 CRUD。
    """

    @abstractmethod
    async def get(self, thread_id: str) -> State | None:
        """根据 thread_id 获取 State。

        Args:
            thread_id: Session ID

        Returns:
            State 实例，不存在时返回 None
        """
        ...

    @abstractmethod
    async def save(self, state: State) -> None:
        """保存或更新 State。

        State.thread_id 作为主键，已存在则更新，不存在则插入。
        自动更新 State.updated_at。

        Args:
            state: 要保存的 State 实例
        """
        ...

    @abstractmethod
    async def delete(self, thread_id: str) -> None:
        """删除指定 thread_id 的 State。

        Args:
            thread_id: Session ID
        """
        ...


# ============================================================================
# SQLiteStateStore — SQLite 实现
# ============================================================================

class SQLiteStateStore(StateStore):
    """基于 SQLite 的 StateStore 实现。

    使用 aiosqlite 异步驱动。
    表结构：
        states (
            thread_id  TEXT PRIMARY KEY,
            state_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )

    线程安全：SQLite 的 WAL 模式 + aiosqlite 的串行化访问保证
    同一进程内的并发安全。

    Args:
        db_path: SQLite 数据库文件路径
    """

    # ── SQL 模板 ──

    _TABLE_DDL: str = """
        CREATE TABLE IF NOT EXISTS states (
            thread_id  TEXT PRIMARY KEY,
            state_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """

    _UPSERT_SQL: str = """
        INSERT INTO states (thread_id, state_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(thread_id) DO UPDATE SET
            state_json = excluded.state_json,
            updated_at = excluded.updated_at
    """

    _SELECT_SQL: str = """
        SELECT state_json FROM states WHERE thread_id = ?
    """

    _DELETE_SQL: str = """
        DELETE FROM states WHERE thread_id = ?
    """

    _INDEX_DDL: str = """
        CREATE INDEX IF NOT EXISTS idx_states_updated_at
        ON states (updated_at)
    """

    # ── 实例方法 ──

    def __init__(self, db_path: str | Path) -> None:
        """初始化 SQLiteStateStore。

        不会立即建立连接——首次访问时通过 _ensure_init() 懒初始化。

        Args:
            db_path: SQLite 数据库文件路径
        """
        self._db_path: str = str(db_path)
        self._initialized: bool = False

    async def _ensure_init(self) -> None:
        """懒初始化：确保数据库表和索引存在。"""
        if self._initialized:
            return
        import aiosqlite

        db_dir: Path = Path(self._db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)

        self._conn: aiosqlite.Connection = await aiosqlite.connect(
            self._db_path,
            isolation_level=None,  # 自动提交模式
        )
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute(self._TABLE_DDL)
        await self._conn.execute(self._INDEX_DDL)
        self._initialized = True

    # ── StateStore 接口实现 ──

    async def get(self, thread_id: str) -> State | None:
        """根据 thread_id 获取 State。"""
        await self._ensure_init()
        import aiosqlite

        cursor: aiosqlite.Cursor = await self._conn.execute(
            self._SELECT_SQL, (thread_id,)
        )
        row: tuple | None = await cursor.fetchone()
        await cursor.close()

        if row is None:
            return None

        state_json_str: str = row[0]
        data: dict[str, Any] = json.loads(state_json_str)
        return _state_from_dict(data)

    async def save(self, state: State) -> None:
        """保存或更新 State。"""
        await self._ensure_init()

        state.touch()
        data: dict[str, Any] = _state_to_dict(state)
        state_json_str: str = json.dumps(data, ensure_ascii=False)

        await self._conn.execute(
            self._UPSERT_SQL,
            (state.thread_id, state_json_str, state.updated_at),
        )

    async def delete(self, thread_id: str) -> None:
        """删除指定 thread_id 的 State。"""
        await self._ensure_init()
        await self._conn.execute(self._DELETE_SQL, (thread_id,))

    async def close(self) -> None:
        """关闭数据库连接。"""
        if self._initialized:
            await self._conn.close()
            self._initialized = False


# ============================================================================
# 序列化辅助函数
# ============================================================================

def _state_to_dict(state: State) -> dict[str, Any]:
    """将 State 序列化为可 JSON 存储的 dict。

    Message 列表中的 tool_calls 需要特殊处理（ToolCall 是 dataclass）。
    """
    messages_data: list[dict[str, Any]] = []
    for m in state.messages:
        msg_dict: dict[str, Any] = {
            "role": m.role,
            "content": m.content,
            "name": m.name,
            "tool_call_id": m.tool_call_id,
        }
        if m.tool_calls:
            msg_dict["tool_calls"] = [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in m.tool_calls
            ]
        messages_data.append(msg_dict)

    return {
        "thread_id": state.thread_id,
        "messages": messages_data,
        "agent_outputs": state.agent_outputs,
        "metadata": state.metadata,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
    }


def _state_from_dict(data: dict[str, Any]) -> State:
    """从 dict 反序列化为 State。"""
    from ..llm.base import Message, ToolCall

    messages: list[Message] = []
    for m_data in data.get("messages", []):
        tool_calls: list[ToolCall] | None = None
        tc_data: list[dict[str, Any]] | None = m_data.get("tool_calls")
        if tc_data:
            tool_calls = [
                ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"])
                for tc in tc_data
            ]
        messages.append(Message(
            role=m_data["role"],
            content=m_data.get("content", ""),
            name=m_data.get("name"),
            tool_call_id=m_data.get("tool_call_id"),
            tool_calls=tool_calls,
        ))

    return State(
        thread_id=data["thread_id"],
        messages=messages,
        agent_outputs=data.get("agent_outputs", {}),
        metadata=data.get("metadata", {}),
        created_at=data.get("created_at", ""),
        updated_at=data.get("updated_at", ""),
    )
