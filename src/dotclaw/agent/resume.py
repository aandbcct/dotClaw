"""ResumeManager —— 中断恢复管理器。

从 StateStore + trace.jsonl 恢复执行上下文。
1. 从 StateStore 读取上次状态快照
2. 从 trace.jsonl 读取最近的对话消息
3. 返回恢复上下文供 TurnLoop 使用
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..runtime.state_store import StateStore
from ..llm.base import Message, ToolCall

logger = logging.getLogger("dotclaw.agent.resume")


class ResumeManager:
    """中断恢复管理器。

    负责：
    1. 从 StateStore 读取上次 state snapshot（判断是否需要恢复）
    2. 从 trace.jsonl 读取最近消息，重建 Message 列表
    3. 检测未完成的工具调用
    """

    def __init__(self, state_store: StateStore, trace_dir: str | Path) -> None:
        self._state_store: StateStore = state_store
        self._trace_dir: Path = Path(trace_dir)

    # ═══ 公开 API ═══

    async def get_resume_context(self, session_id: str) -> dict | None:
        """获取 session 的恢复上下文。

        恢复流程：
        1. 读 StateStore → 检查上次 AgentRun 是否是 TOOL_WAIT
        2. 读 trace.jsonl → 重建消息列表
        3. 返回恢复上下文

        Args:
            session_id: Session ID

        Returns:
            None 如果无需恢复。
            dict:
                messages: list[Message]       重建的对话消息
                incomplete_tools: list[ToolCall] 未完成的工具调用
                state: dict                   旧 state snapshot 数据（供 TurnLoop 恢复）
        """
        # 1. 读 StateStore
        snapshot = await self._state_store.load(session_id)
        if snapshot is None:
            return None

        # 不需要恢复的情况：phase 不是 active 状态
        if snapshot.end_status not in ("tool_wait", "running"):
            return None

        # 2. 读 trace.jsonl 重建消息
        trace_path: Path = self._trace_dir / "session" / session_id / "trace.jsonl"
        entries: list[dict] = self._load_trace(trace_path)
        if not entries:
            return None

        messages, incomplete = self._reconstruct_messages(entries)

        return {
            "messages": messages,
            "incomplete_tools": incomplete,
            "state": snapshot.to_dict(),
        }

    # ═══ 内部方法 ═══

    def _load_trace(self, filepath: Path) -> list[dict]:
        """加载 trace.jsonl，返回条目列表。"""
        if not filepath.is_file():
            return []
        entries: list[dict] = []
        try:
            for line in filepath.read_text(encoding="utf-8").strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    item: dict = json.loads(line)
                    entries.append(item)
                except json.JSONDecodeError:
                    continue
        except OSError:
            pass
        return entries

    def _reconstruct_messages(
        self, entries: list[dict],
    ) -> tuple[list[Message], list[ToolCall]]:
        """从 trace 条目重建 Message 列表。

        只提取 type="trace.message" 的事件。
        返回 (messages, incomplete_tools)。

        Args:
            entries: trace.jsonl 条目列表

        Returns:
            (messages, incomplete_tools)
        """
        ctx: dict[str, Any] = {
            "messages": [],
            "completed_ids": set(),
            "last_tool_calls": [],
        }

        # 只处理 TRACE_MESSAGE 事件
        for entry in entries:
            entry_type: str = entry.get("type", "")
            if entry_type != "trace.message":
                continue
            data: dict = entry.get("data", {})
            role: str = data.get("role", "")

            if role == "user":
                ctx["messages"].append(Message(
                    role="user",
                    content=data.get("content", ""),
                ))
            elif role == "assistant":
                raw_tool_calls = data.get("tool_calls")
                tool_calls: list[ToolCall] | None = None
                if raw_tool_calls:
                    tool_calls = [
                        ToolCall(
                            id=tc["id"],
                            name=tc["name"],
                            arguments=tc.get("arguments", "{}"),
                        )
                        for tc in raw_tool_calls
                    ]
                    ctx["last_tool_calls"] = tool_calls
                ctx["messages"].append(Message(
                    role="assistant",
                    content=data.get("content", ""),
                    tool_calls=tool_calls,
                ))
            elif role == "tool":
                call_id: str = data.get("tool_call_id", "")
                ctx["completed_ids"].add(call_id)
                ctx["messages"].append(Message(
                    role="tool",
                    content=data.get("content", ""),
                    tool_call_id=call_id,
                    name=data.get("name"),
                ))

        # 未完成的工具 = 最后 assistant 的 tool_calls - 已有的 tool_result
        incomplete: list[ToolCall] = []
        for tc in ctx["last_tool_calls"]:
            if tc.id not in ctx["completed_ids"]:
                incomplete.append(tc)

        return ctx["messages"], incomplete
