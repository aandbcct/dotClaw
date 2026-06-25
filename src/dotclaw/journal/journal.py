"""Journal —— 统一观测日志。

一次 AgentLoop.run() 创建一个实例。
所有事件通过具名方法发射，参数只传业务事实。
loop_idx / model / timestamp / duration 全部内化。

配置由 dotclaw.config.settings.JournalConfig 提供，
通过 session_start(config) 传入。
"""

from __future__ import annotations

import time
from typing import Any, TYPE_CHECKING
from pathlib import Path
from dotclaw.journal.events import AgentEvent, EventType
from dotclaw.config.settings import JournalConfig
import json as _json
import os as _os
from datetime import date as _date, datetime as _dt, timezone as _tz

if TYPE_CHECKING:
    pass

_warned: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    """只在首次失败时记录一条 warning。"""
    if key not in _warned:
        import logging
        logging.getLogger("dotclaw.journal").warning(
            f"{key} failed (suppressed): {message}"
        )
        _warned.add(key)


# ═══════════════════════════════════════════════════════════════════
# Journal
# ═══════════════════════════════════════════════════════════════════


class Journal:
    """统一观测日志。

    一次 AgentLoop.run() 创建一个实例。内部维护会话状态和事件列表。
    所有事件通过具名方法发射，loop_idx / model / timestamp / duration 全部内化。
    """

    def __init__(self) -> None:
        self._session_id: str | None = None
        self._request_id: str | None = None
        self._model: str = ""
        self._loop_idx: int = 0
        self._events: list[AgentEvent] = []
        self._timers: dict[str, float] = {}
        self._config: JournalConfig = None  # JournalConfig from settings
        self._session_start_ts: float = 0.0
        self._ttft_ms: float = 0.0  # 最新一轮的 TTFT
        self._session_start_day: str = ""  # 日期固定，跨午夜不变
        # history / state sinks（lazy init）
        self._history_sink: Any = None
        self._state_sink: Any = None
        # state 累加器
        self._token_accum: dict[str, int] = {"input": 0, "output": 0}
        self._tool_count: int = 0
        self._errors_list: list[dict] = []
        self._message_count: int = 0
        self._max_loop_steps: int = 10
        self._creat_at: str = ""

    # ═══ 内部辅助 ═══

    def _emit(self, event_type: str, data: dict | None = None) -> None:
        """发射事件：追加到事件列表，触发各 sink。"""
        ts = time.time()
        created_at = time.strftime("%H:%M:%S", time.localtime(ts))
        ms = int((ts - int(ts)) * 1000)

        event = AgentEvent(
            timestamp=ts,
            created_at=f"{created_at}.{ms:03d}",
            event_type=event_type,
            data=data or {},
        )
        self._events.append(event)

        # 实时输出：trace_sink 逐行追加，console_sink 仅 ERROR/WARNING
        if self._config and self._request_id:
            if self._config.trace:
                try:
                    out_dir = self._ensure_output_dir()
                    if out_dir is not None:
                        filepath = out_dir / "trace.jsonl"
                        line = _json.dumps({
                            "ts": event.timestamp,
                            "t": event.created_at,
                            "type": event.event_type,
                            "data": event.data,
                        }, ensure_ascii=False)
                        with open(filepath, "a", encoding="utf-8") as f:
                            f.write(line + "\n")
                except Exception as e:
                    _warn_once("trace_sink", str(e))
            if self._config.console and event_type == EventType.ERROR:
                try:
                    from dotclaw.journal.sinks.console import console_sink
                    console_sink(event)
                except Exception as e:
                    _warn_once("console_sink", str(e))

    def _require_session(self) -> None:
        """确保 session_start() 已被调用。"""
        if self._session_id is None:
            raise RuntimeError("Must call session_start() before emitting events")

    def _timer_start(self, key: str) -> None:
        self._timers[key] = time.time()

    def _timer_end_ms(self, key: str) -> float:
        """计算从 _timer_start(key) 到现在的毫秒数。"""
        start = self._timers.get(key)
        if start is None:
            return 0.0
        return (time.time() - start) * 1000

    def _output_dir(self) -> Path | None:
        """返回本次对话的产出目录。

        格式: {trace_dir}/{session_id}/{date}/{request_id}
        None 表示 session_start() 尚未调用。
        """
        if not self._config or not self._session_id or not self._request_id:
            return None
        date_str = self._session_start_day or _date.today().isoformat()
        ts_str = time.strftime("%H%M%S", time.localtime(self._session_start_ts))

        return (
            Path(self._config.trace_dir)
            / self._session_id
            / date_str
            / f"{ts_str}-{self._request_id}"
        )

    def _ensure_output_dir(self) -> Path | None:
        """确保产出目录存在，返回路径。"""
        out_dir = self._output_dir()
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir

    # ═══ 会话 ═══

    def session_start(self, session_id: str,
                       request_id: str,
                       model: str,
                       config: "Any") -> None:
        """开始会话。

        Args:
            session_id: 会话 ID
            request_id: 请求 ID
            model: 模型名
            config: JournalConfig 实例
        """
        from datetime import date as _date

        self._session_id = session_id
        self._request_id = request_id
        self._model = model
        self._loop_idx = -1
        self._config = config
        self._events = []
        self._timers = {}
        self._session_start_ts = time.time()
        self._session_start_day = _date.today().isoformat()
        self._creat_at = time.strftime("%H:%M:%S", time.localtime(time.time()))
        # 重置累加器
        self._token_accum = {"input": 0, "output": 0}
        self._tool_count = 0
        self._errors_list = []
        self._message_count = 0
        if hasattr(config, 'max_loop_steps'):
            self._max_loop_steps = config.max_loop_steps

        self._emit(EventType.SESSION_START, {
            "session_id": self._session_id,
            "request_id": self._request_id,
            "model": self._model,
        })

    def session_end(self, exit_reason: str, success: bool = True,
                    total_duration_ms: float = 0.0) -> None:
        """结束会话。exit_reason: "success" | "error" | "interrupted" """
        self._require_session()
        self._emit(EventType.SESSION_END, {
            "exit_reason": exit_reason,
            "success": success,
            "total_duration_ms": round(total_duration_ms, 1),
        })
        status = "completed" if success else "error"
        self._update_state(status)

    # ═══ ReAct 循环 ═══

    def loop_start(self) -> None:
        """开始新一轮循环。loop_idx 内部自增。"""
        self._require_session()
        self._loop_idx += 1
        self._emit(EventType.LOOP_START, {"loop_idx": self._loop_idx})

    def loop_end(self, action: str) -> None:
        """结束当前循环。action: "tool_call" | "response" | "empty" """
        self._require_session()
        self._emit(EventType.LOOP_END, {
            "loop_idx": self._loop_idx, "action": action,
        })
        self._update_state("running")

    def empty_action(self) -> None:
        """记录一次空转。"""
        self._require_session()
        self._emit(EventType.EMPTY_ACTION, {"loop_idx": self._loop_idx})

    # ═══ LLM 调用 ═══

    def prompt_built(
        self,
        message_count: int,
        context_length: int,
        system_prompt: str = "",
        skills_injected: list[str] | None = None,
        tool_count: int = 0,
    ) -> None:
        """提示词构建完成。记录完整 system_prompt 用于调试。"""
        self._require_session()
        self._message_count = message_count
        self._emit(EventType.PROMPT_BUILT, {
            "loop_idx": self._loop_idx,
            "message_count": message_count,
            "context_length": context_length,
            "system_prompt": system_prompt,
            "skills_injected": skills_injected or [],
            "tool_count": tool_count,
        })

    def llm_call_start(self, attempt: int = 1) -> None:
        """发起 LLM 调用。model 已在 session_start 中设定。"""
        self._require_session()
        self._timer_start("llm_call")
        self._emit(EventType.LLM_CALL_START, {
            "loop_idx": self._loop_idx,
            "model": self._model,
            "attempt": attempt,
        })

    def llm_call_end(self) -> None:
        """LLM 调用结束 / 响应开始。
        内部计算 duration_ms（即 TTFT），自动补射 LLM_RESPONSE_START 事件。
        """
        self._require_session()
        self._ttft_ms = self._timer_end_ms("llm_call")

        self._emit(EventType.LLM_CALL_END, {
            "loop_idx": self._loop_idx,
            "model": self._model,
            "duration_ms": round(self._ttft_ms, 1),
        })

        # 自动补射 LLM_RESPONSE_START（同时发生）
        self._emit(EventType.LLM_RESPONSE_START, {"loop_idx": self._loop_idx})
        self._timer_start("llm_response")

    def llm_response_end(
        self,
        input_tokens: int,
        output_tokens: int,
        tps: float,
        status: str,
        stop_reason: str,
    ) -> None:
        """LLM 响应结束。内部计算 duration_ms、ttft_ms、Tps。"""
        self._require_session()
        response_ms = self._timer_end_ms("llm_response")

        # 累加 token 消耗
        self._token_accum["input"] += input_tokens
        self._token_accum["output"] += output_tokens

        # TPS：基于实际响应耗时计算，覆盖调用方传入的值
        actual_tps = (output_tokens / (response_ms / 1000)) if response_ms > 0 else 0.0

        self._emit(EventType.LLM_RESPONSE_END, {
            "loop_idx": self._loop_idx,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "duration_ms": round(response_ms, 1),
            "ttft_ms": round(self._ttft_ms, 1),
            "tps": round(actual_tps, 1),
            "status": status,
            "stop_reason": stop_reason,
        })

    # ═══ 工具调用 ═══

    def tool_start(self, tool_name: str, args: dict | None = None,
                   attempt: int = 1) -> None:
        """开始执行工具。记录工具名和调用参数。"""
        self._require_session()
        self._timer_start(f"tool_{tool_name}")
        self._emit(EventType.TOOL_START, {
            "loop_idx": self._loop_idx,
            "tool_name": tool_name,
            "args": args or {},
            "attempt": attempt,
        })

    def tool_end(
        self,
        tool_name: str,
        result_len: int,
        status: str,
        error_type: str = "",
    ) -> None:
        """工具执行结束。内部计算 duration_ms。"""
        self._require_session()
        duration_ms = self._timer_end_ms(f"tool_{tool_name}")

        self._tool_count += 1

        self._emit(EventType.TOOL_END, {
            "loop_idx": self._loop_idx,
            "tool_name": tool_name,
            "duration_ms": round(duration_ms, 1),
            "result_len": result_len,
            "status": status,
            "error_type": error_type,
        })

    # ═══ Skill ═══

    def skill_body_loaded(self, skill_name: str, status: str = "success",
                          cached: bool = False) -> None:
        """记录 Skill body 已加载。"""
        self._require_session()
        self._emit(EventType.SKILL_BODY_LOADED, {
            "loop_idx": self._loop_idx,
            "skill_name": skill_name,
            "status": status,
            "cached": cached,
        })

    def skill_reference_load(self, skill_name: str, reference_name: str,
                             status: str = "success") -> None:
        """记录 Skill 的 reference 文件被读取。"""
        self._require_session()
        self._emit(EventType.SKILL_REFERENCE, {
            "loop_idx": self._loop_idx,
            "skill_name": skill_name,
            "reference_name": reference_name,
            "status": status,
        })

    def skill_script_exec(self, skill_name: str, script_name: str,
                          status: str) -> None:
        """记录 Skill 脚本执行（实际执行由 tool_start/end 记录）。"""
        self._require_session()
        self._emit(EventType.SKILL_SCRIPT_EXEC, {
            "loop_idx": self._loop_idx,
            "skill_name": skill_name,
            "script_name": script_name,
            "status": status,
        })

    # ═══ 记忆 ═══

    def memory_retrieval(self, query: str, hit_count: int) -> None:
        """记录一次记忆检索。调用方应在检索完成后调用此方法。
        耗时由调用方通过 memory_retrieval_start/memory_retrieval 配对计算。
        """
        self._require_session()
        # 从上次 _timer_start("memory_retrieval") 到现在的耗时
        duration_ms = self._timer_end_ms("memory_retrieval")
        self._emit(EventType.MEMORY_RETRIEVAL, {
            "loop_idx": self._loop_idx,
            "query": query,
            "duration_ms": round(duration_ms, 1),
            "hit_count": hit_count,
        })

    def memory_retrieval_start(self) -> None:
        """标记记忆检索开始（调用方在开始检索前调用）。"""
        self._require_session()
        self._timer_start("memory_retrieval")

    def memory_write(self, write_type: str, status: str) -> None:
        self._require_session()
        self._emit(EventType.MEMORY_WRITE, {
            "loop_idx": self._loop_idx,
            "write_type": write_type,
            "status": status,
        })

    # ═══ 错误 ═══

    def error(self, level: str, source: str, message: str) -> None:
        """记录错误/警告。触发 console_sink 和 trace_sink。"""
        err = {
            "source": source,
            "message": message,
            "level": level,
        }
        self._errors_list.append(err)
        self._emit(EventType.ERROR, {
            "loop_idx": self._loop_idx,
            "level": level,
            "source": source,
            "message": message,
        })

    # ═══ 对话内容记录 ═══

    def record_message(self, message: Any) -> None:
        """记录一个 Message 实例到 history.jsonl。

        loop 取自 _loop_idx；ts 内部计算；step/role 从 Message 提取。
        调用方只需传入 Message 实体，不需要传 loop/ts/role。
        """
        if not self._config or not self._config.history:
            return
        if not self._request_id:
            return

        entry = {
            "loop": self._loop_idx,
            "ts": _dt.now(_tz.utc).isoformat(),
            "role": message.role,
            "content": message.content,
        }

        if message.role == "user":
            entry["step"] = "user_input"
        elif message.role == "assistant":
            entry["step"] = "llm_response"
            if message.tool_calls:
                entry["tool_calls"] = [
                    {"id": tc.id, "name": tc.name, "args": tc.arguments}
                    for tc in message.tool_calls
                ]
            else:
                entry["tool_calls"] = None
        elif message.role == "tool":
            entry["step"] = "tool_result"
            entry["tool_call_id"] = message.tool_call_id
            entry["name"] = message.name

        if self._history_sink is None:
            out_dir = self._ensure_output_dir()
            if out_dir is None:
                return
            from dotclaw.journal.sinks.history_sink import HistorySink
            self._history_sink = HistorySink(out_dir / "history.jsonl")
        self._history_sink.write(entry)

    def _update_state(self, status: str) -> None:
        """覆盖写入 state.json（内部调用）。

        Args:
            status: "running" | "completed" | "error"
        """
        if not self._config or not self._config.state:
            return
        if not self._request_id:
            return
        if self._state_sink is None:
            out_dir = self._ensure_output_dir()
            if out_dir is None:
                return
            from dotclaw.journal.sinks.state_sink import StateSink
            self._state_sink = StateSink(out_dir / "state.json")

        from datetime import datetime as _dt2, timezone as _tz2
        elapsed = int((time.time() - self._session_start_ts) * 1000) if self._session_start_ts else 0

        state = {
            "session_id": self._session_id or "",
            "request_id": self._request_id or "",
            "loop_index": self._loop_idx,
            "status": status,
            "message_count": self._message_count,
            "total_input_tokens": self._token_accum["input"],
            "total_output_tokens": self._token_accum["output"],
            "total_tool_calls": self._tool_count,
            "elapsed_ms": elapsed,
            "errors": list(self._errors_list),
            "model": self._model,
            "max_loop_steps": self._max_loop_steps,
            "updated_at": _dt2.now(_tz2.utc).isoformat(),
        }
        self._state_sink.write(state)

    # ═══ 生命周期 ═══

    def finalize(self) -> None:
        """会话结束处理：构建 report.json + snapshot.json，清空事件列表。

        注意：trace.jsonl 由 _emit() 中的 trace_sink 实时逐行写入，
        finalize() 不再重复写入。
        """

        if not self._config or not self._request_id:
            self._events = []
            self._close_sinks()
            return

        need_dir = self._config.trace or self._config.snapshot
        out_dir = self._ensure_output_dir() if need_dir else None

        # 1. 构建并写入 report.json
        if self._config.trace:
            try:
                report = _build_report(
                    events=self._events,
                    session_id=self._session_id or "",
                    request_id=self._request_id or "",
                    model=self._model,
                )
                report_path = out_dir / "report.json"
                report_path.write_text(
                    _json.dumps(report, ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
            except Exception as e:
                self.error("ERROR", "journal.report", f"构建 report.json 失败: {e}")

        # 2. 构建并写入 snapshot.json
        if self._config.snapshot:
            try:
                from dotclaw.journal.snapshot import SnapshotBuilder
                from dotclaw.journal.storage import save_snapshot, build_run_meta

                ts = _dt.now(_tz.utc)
                run_id = f"run_{ts.strftime('%Y%m%d_%H%M%S')}"

                meta = build_run_meta(
                    run_id=run_id,
                    test_dataset="interactive",
                    test_dataset_size=1,
                )
                builder = SnapshotBuilder(run_meta=meta, task_count=1)
                for event in self._events:
                    builder.process(event)
                snapshot = builder.build()
                save_snapshot(snapshot, str(out_dir), filename="snapshot")
            except Exception as e:
                self.error("ERROR", "journal.snapshot", f"构建 snapshot.json 失败: {e}")

        # 3. 关闭 sinks，清空事件列表
        self._close_sinks()
        self._events = []

    def _close_sinks(self) -> None:
        """关闭 history_sink 和 state_sink 句柄。"""
        if self._history_sink is not None:
            try:
                self._history_sink.close()
            except Exception:
                pass
            self._history_sink = None
        if self._state_sink is not None:
            self._state_sink = None


def _build_report(
    events: list,
    session_id: str,
    request_id: str,
    model: str,
) -> dict:
    """从事件流构建 report.json 汇总。"""
    loops = []
    current_loop = None
    errors_list = []
    tool_calls = []
    memory_events = []

    for event in events:
        etype = event.event_type
        data = event.data

        if etype == EventType.LOOP_START:
            current_loop = {
                "idx": data.get("loop_idx", 0),
                "llm_calls": [],
                "tools": [],
            }
        elif etype == EventType.LOOP_END and current_loop:
            current_loop["action"] = data.get("action", "")
            loops.append(current_loop)
            current_loop = None
        elif etype == EventType.LLM_CALL_START and current_loop:
            current_loop["llm_calls"].append({
                "model": data.get("model", ""),
                "attempt": data.get("attempt", 1),
            })
        elif etype == EventType.LLM_RESPONSE_END and current_loop and current_loop["llm_calls"]:
            last_llm = current_loop["llm_calls"][-1]
            last_llm.update({
                "input_tokens": data.get("input_tokens", 0),
                "output_tokens": data.get("output_tokens", 0),
                "duration_ms": data.get("duration_ms", 0),
                "status": data.get("status", "unknown"),
                "stop_reason": data.get("stop_reason", ""),
            })
        elif etype == EventType.TOOL_START and current_loop:
            current_loop["tools"].append({
                "name": data.get("tool_name", ""),
            })
        elif etype == EventType.TOOL_END and current_loop and current_loop["tools"]:
            last_tool = current_loop["tools"][-1]
            last_tool.update({
                "duration_ms": data.get("duration_ms", 0),
                "result_len": data.get("result_len", 0),
                "status": data.get("status", "unknown"),
            })
        elif etype == EventType.ERROR:
            errors_list.append({
                "level": data.get("level", ""),
                "source": data.get("source", ""),
                "message": data.get("message", ""),
            })
        elif etype == EventType.MEMORY_RETRIEVAL:
            memory_events.append({
                "type": "retrieval",
                "query": data.get("query", ""),
                "hit_count": data.get("hit_count", 0),
            })
        elif etype == EventType.MEMORY_WRITE:
            memory_events.append({
                "type": "write",
                "write_type": data.get("write_type", ""),
                "status": data.get("status", ""),
            })

    # 处理未闭合的 loop
    if current_loop:
        current_loop["action"] = "incomplete"
        loops.append(current_loop)

    session_events = [e for e in events if e.event_type in (
        EventType.SESSION_START, EventType.SESSION_END)]
    total_duration = 0.0
    if len(session_events) >= 2:
        total_duration = (session_events[-1].timestamp - session_events[0].timestamp) * 1000

    all_tools = [t for loop in loops for t in loop.get("tools", [])]
    tool_count = len(all_tools)
    tool_success = sum(1 for t in all_tools if t.get("status") == "success")
    tool_success_rate = tool_success / tool_count if tool_count > 0 else 0

    return {
        "session_id": session_id,
        "request_id": request_id,
        "model": model,
        "total_duration_ms": round(total_duration, 1),
        "loop_count": len(loops),
        "loops": loops,
        "tool_calls_total": tool_count,
        "tool_success_rate": round(tool_success_rate, 3),
        "errors": errors_list,
        "memory": memory_events,
    }
