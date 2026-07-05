"""Journal —— 统一观测模块。

一个入口、多路输出：事件一次发射，按需路由到 trace.jsonl、report.json、snapshot.json。

v2 变更：
- trace 路径统一为 session/{session_id}/trace.jsonl
- history 合并入 trace，所有消息以 TRACE_MESSAGE 事件写入 trace.jsonl
- 不再有独立的 history.jsonl
"""

from __future__ import annotations

import time
import uuid as _uuid
from typing import Any, TYPE_CHECKING
from pathlib import Path
from dotclaw.journal.events import AgentEvent, EventType, TraceMessageRole
from dotclaw.config.settings import JournalConfig
import json as _json
from datetime import datetime as _dt, timezone as _tz

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

    TurnLoop 在 Session 生命周期内持有一个 Journal 实例。
    所有事件通过具名方法发射，agentrun_id / model / timestamp 全部内化。
    """

    def __init__(self) -> None:
        self._session_id: str | None = None
        self._conversation_id: str | None = None
        self._agentrun_id: str | None = None
        self._model: str = ""
        self._loop_idx: int = 0
        self._events: list[AgentEvent] = []
        self._timers: dict[str, float] = {}
        self._config: JournalConfig = None
        self._session_start_ts: float = 0.0
        self._ttft_ms: float = 0.0
        # state 累加器
        self._token_accum: dict[str, int] = {"input": 0, "output": 0}
        self._tool_count: int = 0
        self._errors_list: list[dict] = []
        self._message_count: int = 0
        self._max_loop_steps: int = 10
        self._agentrun_sequence: int = 0
        # trace sink：单一 trace.jsonl 文件句柄
        self._trace_file: Any = None
        # state sink
        self._state_sink: Any = None

    # ═══ 内部辅助 ═══

    def _emit(self, event_type: str, data: dict | None = None) -> None:
        """发射事件：追加到事件列表及 trace.jsonl。"""
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

        # 实时写入 trace.jsonl
        if self._config and self._config.trace and self._session_id:
            self._write_trace_line(ts, created_at, ms, event_type, data or {})

        # 控制台输出（仅 ERROR）
        if self._config and self._config.console and event_type == EventType.ERROR:
            try:
                from dotclaw.journal.sinks.console import console_sink
                console_sink(event)
            except Exception as e:
                _warn_once("console_sink", str(e))

    def _write_trace_line(
        self,
        ts: float,
        created_at: str,
        ms: int,
        event_type: str,
        data: dict,
    ) -> None:
        """写入一行到 trace.jsonl。

        原子操作：
        1. 确保 trace.jsonl 文件句柄已打开
        2. 序列化事件为 JSON 行
        3. 追加写入并 flush
        """
        if self._trace_file is None:
            out_dir = self._trace_output_dir()
            if out_dir is None:
                return
            out_dir.mkdir(parents=True, exist_ok=True)
            filepath = out_dir / "trace.jsonl"
            self._trace_file = open(filepath, "a", encoding="utf-8")

        line_data: dict = {
            "ts": ts,
            "t": f"{created_at}.{ms:03d}",
            "type": event_type,
            "agentrun_id": self._agentrun_id or "",
            "data": data,
        }
        try:
            line: str = _json.dumps(line_data, ensure_ascii=False, default=str)
            self._trace_file.write(line + "\n")
            self._trace_file.flush()
        except OSError as e:
            _warn_once("trace_sink", str(e))

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

    def _trace_output_dir(self) -> Path | None:
        """返回 trace.jsonl 所在目录。

        格式: {trace_dir}/{session_id}/
        None 表示 session_start() 尚未调用。
        """
        if not self._config or not self._session_id:
            return None
        return Path(self._config.trace_dir) / self._session_id

    def _conversation_dir(self) -> Path | None:
        """返回 snapshot/report 所在目录。

        格式: {trace_dir}/{session_id}/{conversation_id}/
        None 表示 session_start() 尚未调用。
        """
        base = self._trace_output_dir()
        if base is None or self._conversation_id is None:
            return None
        return base / self._conversation_id

    # ═══ 会话 ═══

    def session_start(
        self,
        session_id: str,
        model: str,
        config: "Any",
        conversation_id: str = "",
    ) -> None:
        """开始会话。

        Args:
            session_id: 会话 ID
            model: 模型名
            config: JournalConfig 实例
            conversation_id: 对话 ID（用于 snapshot/report 子目录）
        """
        from datetime import date as _date

        self._session_id = session_id
        self._conversation_id = conversation_id or _uuid.uuid4().hex[:8]
        self._model = model
        self._loop_idx = -1
        self._config = config
        self._events = []
        self._timers = {}
        self._session_start_ts = time.time()
        self._agentrun_sequence = -1
        # 重置累加器
        self._token_accum = {"input": 0, "output": 0}
        self._tool_count = 0
        self._errors_list = []
        self._message_count = 0
        if hasattr(config, 'max_loop_steps'):
            self._max_loop_steps = config.max_loop_steps

        self._emit(EventType.SESSION_START, {
            "session_id": self._session_id,
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

    # ═══ AgentRun 管理 ═══

    def agentrun_start(self, agentrun_id: str, trigger: str) -> None:
        """标记新 AgentRun 开始，内部递增 sequence。

        Args:
            agentrun_id: 新 AgentRun ID
            trigger: 触发源类型（TriggerType.value）
        """
        self._agentrun_id = agentrun_id
        self._agentrun_sequence += 1
        self._emit(EventType.LOOP_START, {
            "agentrun_id": agentrun_id,
            "trigger": trigger,
            "sequence": self._agentrun_sequence,
        })

    def agentrun_end(self, end_status: str) -> None:
        """标记当前 AgentRun 结束。

        Args:
            end_status: 结束状态（RunEndStatus.value）
        """
        self._require_session()
        self._emit(EventType.LOOP_END, {
            "agentrun_id": self._agentrun_id,
            "end_status": end_status,
        })

    # ═══ 对话内容记录（Trace Message）═══

    def record_message(self, message: Any) -> None:
        """以 TRACE_MESSAGE 事件记录一条消息到 trace.jsonl。

        role/content/tool_calls/tool_call_id/name 从 Message 提取。
        agentrun_id 自动附加。

        Args:
            message: Message 实例（role/content/tool_calls/...）
        """
        if not self._config or not self._config.trace:
            return

        entry: dict = {
            "role": message.role,
            "content": message.content,
        }

        if message.role == TraceMessageRole.ASSISTANT.value:
            if message.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "name": tc.name,
                        "arguments": tc.arguments,
                    }
                    for tc in message.tool_calls
                ]
        elif message.role == TraceMessageRole.TOOL.value:
            entry["tool_call_id"] = message.tool_call_id
            entry["name"] = message.name

        self._message_count += 1
        self._emit(EventType.TRACE_MESSAGE, entry)

    def record_state_change(self, phase: str, end_status: str, iteration: int) -> None:
        """记录 AgentState 变更事件到 trace.jsonl。

        Args:
            phase: 当前 AgentPhase.value
            end_status: AgentStatus.value
            iteration: 当前迭代次数
        """
        self._emit(EventType.STATE_CHANGE, {
            "phase": phase,
            "end_status": end_status,
            "iteration": iteration,
        })

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
            "agentrun_id": self._agentrun_id,
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
            "agentrun_id": self._agentrun_id,
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
            "agentrun_id": self._agentrun_id,
            "model": self._model,
            "duration_ms": round(self._ttft_ms, 1),
        })

        self._emit(EventType.LLM_RESPONSE_START, {
            "agentrun_id": self._agentrun_id,
        })
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

        self._token_accum["input"] += input_tokens
        self._token_accum["output"] += output_tokens

        actual_tps = (output_tokens / (response_ms / 1000)) if response_ms > 0 else 0.0

        self._emit(EventType.LLM_RESPONSE_END, {
            "agentrun_id": self._agentrun_id,
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
            "agentrun_id": self._agentrun_id,
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
            "agentrun_id": self._agentrun_id,
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
            "agentrun_id": self._agentrun_id,
            "skill_name": skill_name,
            "status": status,
            "cached": cached,
        })

    def skill_reference_load(self, skill_name: str, reference_name: str,
                             status: str = "success") -> None:
        """记录 Skill 的 reference 文件被读取。"""
        self._require_session()
        self._emit(EventType.SKILL_REFERENCE, {
            "agentrun_id": self._agentrun_id,
            "skill_name": skill_name,
            "reference_name": reference_name,
            "status": status,
        })

    def skill_script_exec(self, skill_name: str, script_name: str,
                          status: str) -> None:
        """记录 Skill 脚本执行。"""
        self._require_session()
        self._emit(EventType.SKILL_SCRIPT_EXEC, {
            "agentrun_id": self._agentrun_id,
            "skill_name": skill_name,
            "script_name": script_name,
            "status": status,
        })

    # ═══ 记忆 ═══

    def memory_retrieval(self, query: str, hit_count: int) -> None:
        """记录一次记忆检索。"""
        self._require_session()
        duration_ms = self._timer_end_ms("memory_retrieval")
        self._emit(EventType.MEMORY_RETRIEVAL, {
            "agentrun_id": self._agentrun_id,
            "query": query,
            "duration_ms": round(duration_ms, 1),
            "hit_count": hit_count,
        })

    def memory_retrieval_start(self) -> None:
        """标记记忆检索开始。"""
        self._require_session()
        self._timer_start("memory_retrieval")

    def memory_write(self, write_type: str, status: str) -> None:
        self._require_session()
        self._emit(EventType.MEMORY_WRITE, {
            "agentrun_id": self._agentrun_id,
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
            "agentrun_id": self._agentrun_id,
            "level": level,
            "source": source,
            "message": message,
        })

    # ═══ State 覆盖写入 ═══

    def _update_state(self, status: str) -> None:
        """覆盖写入 state.json（内部调用）。

        Args:
            status: "running" | "completed" | "error"
        """
        if not self._config or not self._config.state:
            return
        if not self._session_id:
            return
        if self._state_sink is None:
            out_dir = self._trace_output_dir()
            if out_dir is None:
                return
            from dotclaw.journal.sinks.state_sink import StateSink
            self._state_sink = StateSink(out_dir / "state.json")

        elapsed = int((time.time() - self._session_start_ts) * 1000) if self._session_start_ts else 0

        state: dict = {
            "session_id": self._session_id or "",
            "agentrun_id": self._agentrun_id or "",
            "agentrun_sequence": self._agentrun_sequence,
            "status": status,
            "message_count": self._message_count,
            "total_input_tokens": self._token_accum["input"],
            "total_output_tokens": self._token_accum["output"],
            "total_tool_calls": self._tool_count,
            "elapsed_ms": elapsed,
            "errors": list(self._errors_list),
            "model": self._model,
            "max_loop_steps": self._max_loop_steps,
            "updated_at": _dt.now(_tz.utc).isoformat(),
        }
        self._state_sink.write(state)

    # ═══ 生命周期 ═══

    def restore_state(self, state: dict) -> None:
        """从上次 state.json 恢复累加器。

        resume 时在 session_start() 之后调用。
        """
        self._agentrun_sequence = state.get("agentrun_sequence", -1)
        self._token_accum["input"] = state.get("total_input_tokens", 0)
        self._token_accum["output"] = state.get("total_output_tokens", 0)
        self._tool_count = state.get("total_tool_calls", 0)
        self._errors_list = list(state.get("errors", []))
        self._message_count = state.get("message_count", 0)

    def finalize(self) -> None:
        """会话结束处理：构建 report.json + snapshot.json。

        注意：trace.jsonl 已实时写入，finalize() 不再重复写入。
        """
        if not self._config or not self._session_id:
            self._events = []
            self._close_trace_file()
            return

        need_dir = self._config.trace or self._config.snapshot
        conv_dir = self._conversation_dir() if need_dir else None

        # 构建并写入 report.json（{sid}/{conversation_id}/）
        if self._config.trace and conv_dir is not None:
            try:
                report = _build_report(
                    events=self._events,
                    session_id=self._session_id or "",
                    request_id=self._agentrun_id or "",
                    model=self._model,
                )
                conv_dir.mkdir(parents=True, exist_ok=True)
                report_path = conv_dir / "report.json"
                report_path.write_text(
                    _json.dumps(report, ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
            except Exception as e:
                self.error("ERROR", "journal.report", f"构建 report.json 失败: {e}")

        # 构建并写入 snapshot.json（{sid}/{conversation_id}/）
        if self._config.snapshot and conv_dir is not None:
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
                conv_dir.mkdir(parents=True, exist_ok=True)
                save_snapshot(snapshot, str(conv_dir), filename="snapshot")
            except Exception as e:
                self.error("ERROR", "journal.snapshot", f"构建 snapshot.json 失败: {e}")

        self._close_trace_file()
        self._events = []

    def _close_trace_file(self) -> None:
        """关闭 trace.jsonl 文件句柄。"""
        if self._trace_file is not None:
            try:
                self._trace_file.close()
            except OSError:
                pass
            self._trace_file = None
        if self._state_sink is not None:
            self._state_sink = None


def _build_report(
    events: list,
    session_id: str,
    request_id: str,
    model: str,
) -> dict:
    """从事件流构建 report.json 汇总。"""
    loops: list[dict] = []
    current_loop: dict | None = None
    errors_list: list[dict] = []
    memory_events: list[dict] = []
    trace_messages: list[dict] = []

    for event in events:
        etype = event.event_type
        data = event.data

        if etype == EventType.LOOP_START:
            current_loop = {
                "agentrun_id": data.get("agentrun_id", ""),
                "trigger": data.get("trigger", ""),
                "sequence": data.get("sequence", 0),
                "llm_calls": [],
                "tools": [],
            }
        elif etype == EventType.LOOP_END and current_loop:
            current_loop["end_status"] = data.get("end_status", "")
            loops.append(current_loop)
            current_loop = None
        elif etype == EventType.LLM_CALL_START and current_loop:
            current_loop["llm_calls"].append({
                "model": data.get("model", ""),
                "attempt": data.get("attempt", 1),
            })
        elif etype == EventType.LLM_RESPONSE_END and current_loop and current_loop["llm_calls"]:
            last_llm: dict = current_loop["llm_calls"][-1]
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
            last_tool: dict = current_loop["tools"][-1]
            last_tool.update({
                "duration_ms": data.get("duration_ms", 0),
                "result_len": data.get("result_len", 0),
                "status": data.get("status", "unknown"),
            })
        elif etype == EventType.TRACE_MESSAGE:
            trace_messages.append({
                "agentrun_id": data.get("agentrun_id", ""),
                "role": data.get("role", ""),
                "content": (data.get("content", "") or "")[:200],
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

    if current_loop:
        current_loop["end_status"] = "incomplete"
        loops.append(current_loop)

    session_events = [e for e in events if e.event_type in (
        EventType.SESSION_START, EventType.SESSION_END)]
    total_duration: float = 0.0
    if len(session_events) >= 2:
        total_duration = (session_events[-1].timestamp - session_events[0].timestamp) * 1000

    all_tools = [t for loop_data in loops for t in loop_data.get("tools", [])]
    tool_count: int = len(all_tools)
    tool_success: int = sum(1 for t in all_tools if t.get("status") == "success")
    tool_success_rate: float = tool_success / tool_count if tool_count > 0 else 0.0

    return {
        "session_id": session_id,
        "request_id": request_id,
        "model": model,
        "total_duration_ms": round(total_duration, 1),
        "agentrun_count": len(loops),
        "loops": loops,
        "trace_messages": trace_messages,
        "tool_calls_total": tool_count,
        "tool_success_rate": round(tool_success_rate, 3),
        "errors": errors_list,
        "memory": memory_events,
    }
