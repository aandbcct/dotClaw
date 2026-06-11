"""会话跟踪模块（P13）：逐步骤记录状态转换，trace.jsonl + report.json。

用法:
    tracer = AgentTracer(config.debug, data_root="data")
    tracer.start_session(req_id, user_message)
    tracer.start_loop(0)
    tracer.prompt_built(messages, msg_count, est_tokens)
    sid = tracer.llm_call_start(model="deepseek")
    tracer.llm_call_done(sid, success=True, duration_ms=800)
    tracer.end_loop(0)
    tracer.end_session(success=True, final_response="...")
    tracer.build_report()

输出路径: data/traces/{YYYY-MM-DD}/{request_id}/trace.jsonl + report.json
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config.settings import DebugConfig


class AgentTracer:
    """会话跟踪器。

    实时写入 trace.jsonl（每条事件一行 JSON append），
    会话结束后构建 report.json（将 start/done 配对合并为终态）。
    """

    def __init__(self, debug_config: "DebugConfig", data_root: str):
        self._enabled = debug_config.enable_tracer
        if not self._enabled:
            return

        self._data_root = Path(data_root)
        self._output_dir = self._data_root / "data"/"traces"

        # 步骤计数器：每个请求从 0 开始自增
        self._step_counter: int = 0
        self._req_id: str = ""
        self._round_num: int = -1
        self._trace_file: str = ""
        self._session_start_ts: float = 0.0

    # ==================================================================
    # 公共 API — 步骤方法
    # ==================================================================

    # ---- session ----

    def start_session(self, req_id: str, user_message: str) -> None:
        """开始一次 query 会话。"""
        if not self._enabled:
            return
        self._req_id = req_id
        self._step_counter = 0
        self._round_num = -1
        self._session_start_ts = time.time()

        # 确保输出目录存在
        today = datetime.now().strftime("%Y-%m-%d")
        dir_path = self._output_dir / today / req_id
        dir_path.mkdir(parents=True, exist_ok=True)
        self._trace_file = str(dir_path / "trace.jsonl")

        self._append(
            step="session",
            state="start",
            round_num=-1,
            user_message=user_message,
        )

    def end_session(
        self,
        success: bool,
        final_response: str = "",
        error: str | None = None,
    ) -> None:
        """结束一次 query 会话。"""
        if not self._enabled:
            return
        if self._session_start_ts == 0.0:
            return  # start_session 未被调用，静默跳过
        total_dur = int((time.time() - self._session_start_ts) * 1000)
        entry = {"final_response": final_response, "total_duration_ms": total_dur}
        if not success and error:
            entry["error"] = error

        self._append(
            step="session",
            state="success" if success else "failure",
            round_num=-1,
            **entry,
        )

    # ---- loop ----

    def start_loop(self, round_num: int) -> None:
        """开始一轮 ReAct 循环。"""
        if not self._enabled:
            return
        self._round_num = round_num
        self._append(step="loop", state="start", round_num=round_num)

    def end_loop(self) -> None:
        """结束一轮 ReAct 循环（永远是 success）。"""
        if not self._enabled:
            return
        self._append(
            step="loop",
            state="success",
            round_num=self._round_num,
        )

    # ---- prompt_built ----

    def prompt_built(
        self,
        messages: list,
        msg_count: int,
        est_tokens: int,
    ) -> None:
        """prompt 构造完毕（不会失败，一步到位写 success）。"""
        if not self._enabled:
            return
        msgs_serialized = []
        for m in messages:
            entry = {"role": m.role, "content": m.content}
            if m.name:
                entry["name"] = m.name
            if m.tool_call_id:
                entry["tool_call_id"] = m.tool_call_id
            if m.tool_calls:
                entry["tool_calls"] = [
                    {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                    for tc in m.tool_calls
                ]
            msgs_serialized.append(entry)

        self._append(
            step="prompt_built",
            state="success",
            round_num=self._round_num,
            messages=msgs_serialized,
            msg_count=msg_count,
            est_tokens=est_tokens,
        )

    # ---- llm_call ----

    def llm_call_start(self, model: str) -> str:
        """发起 LLM API 请求前调用。返回 step_id 供 done 配对。"""
        if not self._enabled:
            return ""
        sid = self._next_step_id()
        self._append(
            step="llm_call",
            state="start",
            round_num=self._round_num,
            step_id=sid,
            model=model,
        )
        return sid

    def llm_call_done(
        self,
        step_id: str,
        success: bool,
        duration_ms: float = 0,
        error: str | None = None,
    ) -> None:
        """LLM API 请求完成（第一个 chunk 到达时调用）。"""
        if not self._enabled:
            return
        state = "success" if success else "failure"
        entry = {"duration_ms": round(duration_ms, 1)}
        if not success and error:
            entry["error"] = error
        self._append(
            step="llm_call",
            state=state,
            round_num=self._round_num,
            step_id=step_id,
            **entry,
        )

    # ---- llm_response ----

    def llm_response_start(self) -> str:
        """开始接收流式回复。返回 step_id 供 done 配对。"""
        if not self._enabled:
            return ""
        sid = self._next_step_id()
        self._append(
            step="llm_response",
            state="start",
            round_num=self._round_num,
            step_id=sid,
        )
        return sid

    def llm_response_done(
        self,
        step_id: str,
        success: bool,
        finish_reason: str = "",
        usage: dict | None = None,
        duration_ms: float = 0,
        error: str | None = None,
    ) -> None:
        """流式回复接收完毕。"""
        if not self._enabled:
            return
        state = "success" if success else "failure"
        entry = {
            "finish_reason": finish_reason,
            "usage": usage,
            "duration_ms": round(duration_ms, 1),
        }
        if not success and error:
            entry["error"] = error
        self._append(
            step="llm_response",
            state=state,
            round_num=self._round_num,
            step_id=step_id,
            **entry,
        )

    # ---- tool_exec ----

    def tool_exec_start(self, tool_name: str, args: dict) -> str:
        """开始执行工具。返回 step_id 供 done 配对。"""
        if not self._enabled:
            return ""
        sid = self._next_step_id()
        self._append(
            step="tool_exec",
            state="start",
            round_num=self._round_num,
            step_id=sid,
            tool_name=tool_name,
            args=args,
        )
        return sid

    def tool_exec_done(
        self,
        step_id: str,
        success: bool,
        tool_name: str,
        result: str = "",
        duration_ms: float = 0,
        error: str | None = None,
    ) -> None:
        """工具执行完成。"""
        if not self._enabled:
            return
        state = "success" if success else "failure"
        entry = {
            "tool_name": tool_name,
            "result": result,
            "duration_ms": round(duration_ms, 1),
        }
        if not success and error:
            entry["error"] = error
        self._append(
            step="tool_exec",
            state=state,
            round_num=self._round_num,
            step_id=step_id,
            **entry,
        )

    # ==================================================================
    # report.json 构建
    # ==================================================================

    def build_report(self) -> str:
        """从 trace.jsonl 构建 report.json。返回 report 文件路径。"""
        if not self._enabled or not self._trace_file:
            return ""

        try:
            with open(self._trace_file, "r", encoding="utf-8") as f:
                lines = [json.loads(line) for line in f if line.strip()]
        except (FileNotFoundError, json.JSONDecodeError):
            return ""

        report = self._build_report_from_events(lines)

        report_path = str(
            Path(self._trace_file).parent / "report.json"
        )
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        return report_path

    def _build_report_from_events(self, events: list[dict]) -> dict:
        """从事件列表构建 report 结构。"""
        # 提取 session 级信息
        session_start = None
        session_end = None
        rounds_map: dict[int, dict] = {}
        pending_steps: dict[str, dict] = {}  # step_id → accumulated entry

        for ev in events:
            step = ev.get("step")
            rnum = ev.get("round", -1)

            # session 级事件
            if step == "session":
                if ev["state"] == "start":
                    session_start = ev
                else:
                    session_end = ev
                continue

            # 初始化轮次
            if rnum not in rounds_map:
                rounds_map[rnum] = {
                    "round": rnum,
                    "state": "success",
                    "prompt_built": None,
                    "llm_call": None,
                    "llm_response": None,
                    "tool_execs": [],
                }

            rd = rounds_map[rnum]
            sid = ev.get("step_id")

            # loop 事件 — 不进入 report 步骤列表
            if step == "loop":
                if ev["state"] == "failure":
                    rd["state"] = "failure"
                continue

            # prompt_built 永远 success，直接落
            if step == "prompt_built":
                rd["prompt_built"] = self._merge_step(
                    {"state": "success", "started_at": ev["ts"]},
                    ev,
                )
                continue

            # 有 step_id 的步骤 — start/done 配对
            if sid:
                if ev["state"] == "start":
                    pending_steps[sid] = {"started_at": ev["ts"], "step": step}
                else:
                    pending = pending_steps.pop(sid, {"started_at": ev["ts"]})
                    merged = self._merge_step(pending, ev)

                    if step == "llm_call":
                        rd["llm_call"] = merged
                    elif step == "llm_response":
                        rd["llm_response"] = merged
                    elif step == "tool_exec":
                        rd["tool_execs"].append(merged)
                        if ev["state"] == "failure":
                            rd["state"] = "failure"

        # 未配对完的 start → 标记 incomplete
        for sid, pending in pending_steps.items():
            pending["state"] = "incomplete"
            step = pending.pop("step", "")
            # 已知限制：未配对事件统一放入 round 0。因为在崩溃场景下
            # pending 事件不携带 round 信息（round 由 _round_num 在到期时管理，
            # start 事件写入 trace 时使用了当前的 _round_num，但 pending_steps
            # 字典不保存 round）。这种场景极少发生（仅崩溃时），暂做简化处理。
            rd = rounds_map.get(0)
            if not rd:
                continue
            if step == "llm_call":
                rd["llm_call"] = pending
            elif step == "llm_response":
                rd["llm_response"] = pending
            elif step == "tool_exec":
                rd["tool_execs"].append(pending)

        # 组装 report
        report = {
            "req_id": self._req_id,
            "user_message": session_start["user_message"] if session_start else "",
            "state": session_end["state"] if session_end else "incomplete",
            "started_at": session_start["ts"] if session_start else "",
            "total_duration_ms": (
                session_end.get("total_duration_ms", 0)
                if session_end
                else 0
            ),
            "final_response": (
                session_end.get("final_response", "")
                if session_end
                else ""
            ),
            "rounds": [
                rounds_map[k]
                for k in sorted(rounds_map.keys())
            ],
        }

        if session_end and session_end.get("error"):
            report["error"] = session_end["error"]

        return report

    def _merge_step(self, starter: dict, ev: dict) -> dict:
        """合并 start 和 done 事件为一个步骤条目。"""
        entry = dict(starter)
        for key, val in ev.items():
            if key in ("step", "state", "step_id", "round", "req_id", "started_at", "ts"):
                continue
            entry[key] = val
        entry["state"] = ev["state"]
        return entry

    # ==================================================================
    # 内部方法
    # ==================================================================

    def _next_step_id(self) -> str:
        sid = f"s_{self._step_counter:03d}"
        self._step_counter += 1
        return sid

    def _append(self, step: str, state: str, round_num: int, **extra) -> None:
        """向 trace.jsonl 追加一行事件。"""
        ts = datetime.now(timezone.utc).isoformat()
        entry = {
            "ts": ts,
            "req_id": self._req_id,
            "round": round_num,
            "step": step,
            "state": state,
            **extra,
        }
        with open(self._trace_file, "a", encoding="utf-8") as f:
            json.dump(entry, f, ensure_ascii=False)
            f.write("\n")
