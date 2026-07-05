"""ResumeManager —— 中断恢复管理器。

从 Journal trace 目录读取 history.jsonl + state.json，
重建 Agent._history，检测并返回未完成的工具调用。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..llm.base import Message, ToolCall

logger = logging.getLogger("dotclaw.agent.resume")


class ResumeManager:
    """中断恢复管理器。

    负责：
    1. 扫描 trace 目录，找到 status="running" 的中断 request
    2. 加载 history.jsonl，重建 Message 列表
    3. 检测未完成的工具调用（assistant(tool_calls) 后缺少 tool_result）
    """

    def __init__(self, trace_root: str = "./data/traces") -> None:
        self._trace_root = Path(trace_root)

    # ═══ 公开 API ═══

    def get_resume_context(self, session_id: str) -> dict | None:
        """获取 session 的恢复上下文。

        Returns:
            None 如果无需恢复。
            dict:
                messages: list[Message]       重建的对话消息
                incomplete_tools: list[ToolCall] 未完成的工具调用
                request_id: str               被中断的 request_id
                state: dict                   旧 state.json 内容（供 journal.restore_state）
        """
        path = self.find_interrupted(session_id)
        if path is None:
            return None

        entries = self.load_history(path / "history.jsonl")
        if not entries:
            return None

        messages, incomplete = self.reconstruct(entries)
        request_id = path.name.split("-", 1)[1] if "-" in path.name else path.name

        state = {}
        state_path = path / "state.json"
        if state_path.is_file():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        return {
            "messages": messages,
            "incomplete_tools": incomplete,
            "request_id": request_id,
            "state": state,
        }

    # ═══ 内部方法 ═══

    def find_interrupted(self, session_id: str) -> Path | None:
        """找到 session 最近一次被中断的 request 目录。

        按 state.json 中 status == "running" 判定。
        如果有多个，取目录名（HHMMSS 前缀）最新的。
        """
        session_dir = self._trace_root / session_id / "traces"
        if not session_dir.is_dir():
            return None

        # 收集所有 running 的 request 目录
        running: list[Path] = []
        for date_dir in sorted(session_dir.iterdir(), reverse=True):
            if not date_dir.is_dir():
                continue
            for req_dir in sorted(date_dir.iterdir(), reverse=True):
                if not req_dir.is_dir():
                    continue
                state_path = req_dir / "state.json"
                if not state_path.is_file():
                    continue
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                if state.get("status") == "running":
                    running.append(req_dir)

        if not running:
            return None

        # 取最新的
        running.sort(reverse=True)
        return running[0]

    def load_history(self, filepath: Path) -> list[dict]:
        """加载 history.jsonl，返回条目列表。"""
        if not filepath.is_file():
            return []
        entries: list[dict] = []
        try:
            for line in filepath.read_text(encoding="utf-8").strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            pass
        return entries

    def reconstruct(self, entries: list[dict]) -> tuple[list[Message], list[ToolCall]]:
        """从 history 条目重建 Message 列表，返回 (messages, incomplete_tools)。

        用步骤处理器模式，根据 step 字段分发到对应 handler。
        """
        ctx: dict[str, Any] = {
            "messages": [],
            "completed_ids": set(),
            "last_tool_calls": [],
        }

        for entry in entries:
            step = entry.get("step", "")
            handler = self._STEP_HANDLERS.get(step)
            if handler:
                handler(entry, ctx)

        # 计算未完成的工具 = 最后 assistant 的 tool_calls - 已有的 tool_result
        incomplete: list[ToolCall] = []
        for tc in ctx["last_tool_calls"]:
            if tc.id not in ctx["completed_ids"]:
                incomplete.append(tc)

        return ctx["messages"], incomplete

    # ═══ 步骤处理器 ═══

    @staticmethod
    def _handle_user_input(entry: dict, ctx: dict) -> None:
        ctx["messages"].append(Message(
            role="user",
            content=entry.get("content", ""),
        ))

    @staticmethod
    def _handle_llm_response(entry: dict, ctx: dict) -> None:
        raw_tool_calls = entry.get("tool_calls")
        tool_calls: list[ToolCall] | None = None
        if raw_tool_calls:
            tool_calls = [
                ToolCall(
                    id=tc["id"],
                    name=tc["name"],
                    arguments=tc.get("args", "{}"),
                )
                for tc in raw_tool_calls
            ]
            ctx["last_tool_calls"] = tool_calls

        ctx["messages"].append(Message(
            role="assistant",
            content=entry.get("content", ""),
            tool_calls=tool_calls,
        ))

    @staticmethod
    def _handle_tool_result(entry: dict, ctx: dict) -> None:
        call_id = entry.get("tool_call_id", "")
        ctx["completed_ids"].add(call_id)
        ctx["messages"].append(Message(
            role="tool",
            content=entry.get("content", ""),
            tool_call_id=call_id,
            name=entry.get("name"),
        ))

    _STEP_HANDLERS = {
        "user_input": _handle_user_input,
        "llm_response": _handle_llm_response,
        "tool_result": _handle_tool_result,
    }
