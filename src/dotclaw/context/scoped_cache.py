"""按 Agent、Session 和 Run 隔离的上下文槽缓存。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SlotCacheScope(StrEnum):
    """上下文槽缓存的隔离粒度。"""

    STATIC = "static"
    SESSION = "session"
    CONDITIONAL = "conditional"
    DYNAMIC = "dynamic"


@dataclass(frozen=True)
class ScopeKey:
    """缓存项的完整作用域标识。"""

    slot_name: str
    scope: SlotCacheScope
    agent_id: str
    identity_version: str
    session_id: str = ""
    run_id: str = ""


@dataclass(frozen=True)
class CacheLookup:
    """缓存查询结果，允许缓存内容本身为 None。"""

    found: bool
    content: str | None = None


class ScopedCache:
    """仅保存 Slot 产物，不保存 Run 级状态或 Journal 数据。"""

    def __init__(self) -> None:
        self._entries: dict[ScopeKey, str | None] = {}

    def get(self, key: ScopeKey) -> CacheLookup:
        """读取指定作用域的缓存结果。"""
        if key not in self._entries:
            return CacheLookup(found=False)
        return CacheLookup(found=True, content=self._entries[key])

    def set(self, key: ScopeKey, content: str | None) -> None:
        """保存指定作用域的槽产物。"""
        self._entries[key] = content

    def clear_run(self, run_id: str) -> None:
        """清理已经结束运行的条件缓存。"""
        keys: tuple[ScopeKey, ...] = tuple(
            key for key in self._entries if key.run_id == run_id
        )
        key: ScopeKey
        for key in keys:
            del self._entries[key]

    def build_key(
        self,
        slot_name: str,
        scope: SlotCacheScope,
        agent_id: str,
        identity_version: str,
        session_id: str,
        run_id: str,
    ) -> ScopeKey | None:
        """按槽声明的作用域生成最小且隔离的缓存键。"""
        if scope is SlotCacheScope.DYNAMIC:
            return None
        if scope is SlotCacheScope.STATIC:
            return ScopeKey(slot_name, scope, agent_id, identity_version)
        if scope is SlotCacheScope.SESSION:
            return ScopeKey(slot_name, scope, agent_id, identity_version, session_id)
        return ScopeKey(slot_name, scope, agent_id, identity_version, session_id, run_id)
