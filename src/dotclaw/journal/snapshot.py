"""SnapshotBuilder — 从事件流计算指标快照。

纯数据处理，无副作用。process(event) 逐事件更新中间状态，
build() 产出一份 AgentRunSnapshot。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dotclaw.journal.events import EventType
from dotclaw.journal.metrics_types import (
    AgentGeneralMetrics,
    AgentRunSnapshot,
    MemoryMetrics,
    ReactLoopMetrics,
    RunMeta,
    SkillMetrics,
    ToolCallMetrics,
)

if TYPE_CHECKING:
    from dotclaw.journal.events import AgentEvent


def _p95(values: list[float]) -> float:
    """计算 P95 值，空列表返回 0.0。"""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = int(len(sorted_vals) * 0.95)
    return sorted_vals[min(idx, len(sorted_vals) - 1)]


def _safe_div(numerator: float, denominator: int) -> float:
    """安全除法，分母为 0 返回 0.0。"""
    return numerator / denominator if denominator > 0 else 0.0


class SnapshotBuilder:
    """从事件流构建 AgentRunSnapshot。

    Args:
        run_meta: 运行元信息。
        task_count: 测试数据集任务数（用于计算 per-task 平均值）。
    """

    def __init__(self, run_meta: RunMeta, task_count: int) -> None:
        self._meta = run_meta
        self._task_count = task_count

        # ── React 中间状态 ──
        self._total_loops = 0
        self._empty_action_count = 0
        self._redundant_action_count = 0
        self._loop_durations: list[float] = []
        self._llm_durations: list[float] = []
        self._tool_durations: list[float] = []
        self._reasoning_tokens: list[int] = []
        self._task_loop_counts: list[int] = []
        self._task_success_count = 0
        self._current_task_loops = 0
        self._last_action: tuple[str, str] | None = None  # (tool_name, args_json-ish)

        # ── 工具中间状态 ──
        self._total_tool_calls = 0
        self._tool_call_counts: dict[str, int] = {}
        self._tool_success_by_name: dict[str, int] = {}
        self._tool_error_counts: dict[str, int] = {}
        self._tool_error_types: dict[str, int] = {}
        self._tool_durations_by_tool: dict[str, list[float]] = {}
        self._tool_success_total = 0  # total successful tool calls
        self._retry_count = 0
        self._current_loop_tools: set[str] = set()

        # ── Skill 中间状态 ──
        self._skill_triggers = 0
        self._skill_triggers_by_name: dict[str, int] = {}
        self._skill_body_load_ms: list[float] = []
        self._skill_body_cache_hits = 0
        self._skill_body_total = 0
        self._skill_script_count = 0
        self._skill_script_success = 0
        self._skill_token_overhead: list[int] = []

        # ── 记忆中间状态 ──
        self._retrieval_durations: list[float] = []
        self._retrieval_hits = 0
        self._total_writes = 0
        self._writes_by_type: dict[str, int] = {}
        self._write_failures = 0

        # ── 通用/LLM 中间状态 ──
        self._input_tokens: list[int] = []
        self._output_tokens: list[int] = []
        self._ttft_list: list[float] = []
        self._tps_list: list[float] = []
        self._model_input_tokens: dict[str, int] = {}
        self._model_output_tokens: dict[str, int] = {}
        self._e2e_durations: list[float] = []

    # =========================================================================
    # process()
    # =========================================================================

    def process(self, event: "AgentEvent") -> None:
        """处理单个事件，更新中间状态。"""
        etype = event.event_type
        data = event.data

        if etype == EventType.SESSION_START:
            self._current_task_loops = 0

        elif etype == EventType.SESSION_END:
            self._task_loop_counts.append(self._current_task_loops)
            success_val = data.get("success")
            if success_val is True or success_val == "success":
                self._task_success_count += 1
            e2e = data.get("total_duration_ms", 0.0)
            if e2e > 0:
                self._e2e_durations.append(e2e)

        elif etype == EventType.LOOP_START:
            self._total_loops += 1
            self._current_task_loops += 1
            # reset per-loop tracking
            self._current_loop_tools.clear()
            thought_tokens = data.get("thought_tokens", 0)
            if thought_tokens:
                self._reasoning_tokens.append(thought_tokens)

        elif etype == EventType.LOOP_END:
            dur = data.get("duration_ms", 0.0)
            if dur > 0:
                self._loop_durations.append(dur)
            # Track last action for redundant detection
            action_name = data.get("action", "")
            action_input = str(data.get("action_input", ""))
            if action_name:
                current = (action_name, action_input)
                if self._last_action and self._last_action == current:
                    self._redundant_action_count += 1
                self._last_action = current

        elif etype == EventType.EMPTY_ACTION:
            self._empty_action_count += 1

        elif etype == EventType.TOOL_START:
            pass  # tracking done on call_end

        elif etype == EventType.TOOL_END:
            self._total_tool_calls += 1
            tool_name = data.get("tool_name", "unknown")
            dur = data.get("duration_ms", 0.0)

            # count by tool
            self._tool_call_counts[tool_name] = self._tool_call_counts.get(tool_name, 0) + 1

            # success/failure — support both "status": "success" (string) and "success": True (bool)
            tool_success = data.get("status") == "success" or data.get("success", False)
            if tool_success:
                self._tool_success_total += 1
                self._tool_success_by_name[tool_name] = self._tool_success_by_name.get(tool_name, 0) + 1
            else:
                self._tool_error_counts[tool_name] = self._tool_error_counts.get(tool_name, 0) + 1
                error_type = data.get("error_type", "unknown")
                self._tool_error_types[error_type] = self._tool_error_types.get(error_type, 0) + 1

            # duration (>= 0 to capture sub-millisecond tool executions)
            if dur >= 0:
                self._tool_durations.append(dur)
                if tool_name not in self._tool_durations_by_tool:
                    self._tool_durations_by_tool[tool_name] = []
                self._tool_durations_by_tool[tool_name].append(dur)

            # retry detection: same tool called again in same loop
            if tool_name in self._current_loop_tools:
                self._retry_count += 1
            self._current_loop_tools.add(tool_name)

        elif etype == EventType.SKILL_BODY_LOADED:
            # skill body 加载 = skill 被激活（一事件二义，合并计数）
            self._skill_triggers += 1
            self._skill_body_total += 1
            skill_name = data.get("skill_name", "unknown")
            self._skill_triggers_by_name[skill_name] = self._skill_triggers_by_name.get(skill_name, 0) + 1
            if data.get("cached", False):
                self._skill_body_cache_hits += 1
            token_count = data.get("token_count", 0)
            if token_count > 0:
                self._skill_token_overhead.append(token_count)

        elif etype == EventType.SKILL_SCRIPT_EXEC:
            self._skill_script_count += 1
            if data.get("status") == "success" or data.get("success", False):
                self._skill_script_success += 1

        elif etype == EventType.MEMORY_RETRIEVAL:
            dur = data.get("duration_ms", 0.0)
            if dur > 0:
                self._retrieval_durations.append(dur)
            hit_count = data.get("hit_count", 0)
            if hit_count > 0 or data.get("hit", False):
                self._retrieval_hits += 1

        elif etype == EventType.MEMORY_WRITE:
            self._total_writes += 1
            mem_type = data.get("write_type") or data.get("memory_type", "unknown")
            self._writes_by_type[mem_type] = self._writes_by_type.get(mem_type, 0) + 1
            if not data.get("success", True):
                self._write_failures += 1

        elif etype == EventType.LLM_CALL_START:
            # model tracking only — actual token counts are in LLM_RESPONSE_END
            pass

        elif etype == EventType.LLM_RESPONSE_END:
            input_tokens_val = data.get("input_tokens", 0)
            if input_tokens_val:
                self._input_tokens.append(input_tokens_val)
            output_tokens_val = data.get("output_tokens", 0)
            if output_tokens_val:
                self._output_tokens.append(output_tokens_val)
            model = data.get("model", "unknown")
            self._model_input_tokens[model] = self._model_input_tokens.get(model, 0) + input_tokens_val
            self._model_output_tokens[model] = self._model_output_tokens.get(model, 0) + output_tokens_val

            dur = data.get("duration_ms", 0.0)
            if dur > 0:
                self._llm_durations.append(dur)

            ttft = data.get("ttft_ms", 0.0)
            if ttft > 0:
                self._ttft_list.append(ttft)

            tps = data.get("tps", 0.0)
            if tps > 0:
                self._tps_list.append(tps)

    # =========================================================================
    # build()
    # =========================================================================

    def build(self) -> AgentRunSnapshot:
        """从已处理的事件构建 AgentRunSnapshot。

        可以多次调用，结果幂等（中间状态不变）。
        """
        task_count = max(self._task_count, len(self._task_loop_counts) or 1)

        react = self._build_react(task_count)
        tools = self._build_tools()
        skills = self._build_skills(task_count)
        memory = self._build_memory(task_count)
        general = self._build_general(task_count)

        return AgentRunSnapshot(
            run_id=self._meta.run_id,
            timestamp=self._meta.timestamp,
            git_commit=self._meta.git_commit,
            config_hash=self._meta.config_hash,
            test_dataset=self._meta.test_dataset,
            test_dataset_size=self._meta.test_dataset_size,
            react=react,
            tools=tools,
            skills=skills,
            memory=memory,
            general=general,
        )

    # ── 各子系统构建 ──

    def _build_react(self, task_count: int) -> ReactLoopMetrics:
        avg_loops = _safe_div(self._total_loops, task_count)
        max_loops = max(self._task_loop_counts) if self._task_loop_counts else 0
        completion_rate = _safe_div(self._task_success_count, task_count)
        empty_rate = _safe_div(self._empty_action_count, self._total_loops)
        redundant_rate = _safe_div(self._redundant_action_count, self._total_loops)
        avg_reasoning = (
            int(sum(self._reasoning_tokens) / len(self._reasoning_tokens))
            if self._reasoning_tokens else 0
        )

        return ReactLoopMetrics(
            total_loops=self._total_loops,
            avg_loops_per_task=round(avg_loops, 2),
            max_loops_single_task=max_loops,
            task_completion_rate=round(completion_rate, 4),
            empty_action_rate=round(empty_rate, 4),
            redundant_action_rate=round(redundant_rate, 4),
            avg_reasoning_tokens_per_loop=avg_reasoning,
            avg_loop_duration_ms=round(_safe_div(sum(self._loop_durations), len(self._loop_durations)), 1),
            avg_llm_duration_ms=round(_safe_div(sum(self._llm_durations), len(self._llm_durations)), 1),
            avg_tool_duration_ms=round(_safe_div(sum(self._tool_durations), len(self._tool_durations)), 1),
            p95_loop_duration_ms=round(_p95(self._loop_durations), 1),
        )

    def _build_tools(self) -> ToolCallMetrics:
        success_rate = _safe_div(self._tool_success_total, self._total_tool_calls)
        retry_rate = _safe_div(self._retry_count, self._total_tool_calls)

        success_by_tool: dict[str, float] = {}
        for name, count in self._tool_call_counts.items():
            s = self._tool_success_by_name.get(name, 0)
            success_by_tool[name] = round(_safe_div(s, count), 4)

        avg_by_tool: dict[str, float] = {}
        p95_by_tool: dict[str, float] = {}
        for name, durs in self._tool_durations_by_tool.items():
            if durs:
                avg_by_tool[name] = round(sum(durs) / len(durs), 1)
                p95_by_tool[name] = round(_p95(durs), 1)

        return ToolCallMetrics(
            total_calls=self._total_tool_calls,
            calls_by_tool=dict(self._tool_call_counts),
            overall_success_rate=round(success_rate, 4),
            success_rate_by_tool=success_by_tool,
            errors_by_tool=dict(self._tool_error_counts),
            errors_by_type=dict(self._tool_error_types),
            retry_rate=round(retry_rate, 4),
            avg_duration_by_tool=avg_by_tool,
            p95_duration_by_tool=p95_by_tool,
        )

    def _build_skills(self, task_count: int) -> SkillMetrics:
        trigger_rate = _safe_div(self._skill_triggers, task_count)
        cache_rate = _safe_div(self._skill_body_cache_hits, self._skill_body_total)
        avg_scripts = _safe_div(self._skill_script_count, self._skill_triggers)
        script_rate = _safe_div(self._skill_script_success, self._skill_script_count)
        avg_token_overhead = _safe_div(sum(self._skill_token_overhead), self._skill_triggers)

        return SkillMetrics(
            total_triggers=self._skill_triggers,
            triggers_by_skill=dict(self._skill_triggers_by_name),
            trigger_rate=round(trigger_rate, 4),
            avg_body_load_ms=0.0,    # body 注入在 prompt 阶段，无实际耗时
            body_cache_hit_rate=round(cache_rate, 4),
            avg_scripts_per_trigger=round(avg_scripts, 2),
            script_success_rate=round(script_rate, 4),
            avg_skill_duration_ms=0.0,   # 脚本执行由 tool_start/end 记录
            token_overhead_per_skill=round(avg_token_overhead, 1),
        )

    def _build_memory(self, task_count: int) -> MemoryMetrics:
        total_retrievals = len(self._retrieval_durations)
        retrieval_rate = _safe_div(total_retrievals, task_count)
        hit_rate = _safe_div(self._retrieval_hits, total_retrievals)

        return MemoryMetrics(
            total_retrievals=total_retrievals,
            retrieval_rate=round(retrieval_rate, 4),
            hit_rate=round(hit_rate, 4),
            avg_retrieval_ms=round(_safe_div(sum(self._retrieval_durations), total_retrievals), 1),
            p95_retrieval_ms=round(_p95(self._retrieval_durations), 1),
            index_size=0,                        # reserved: will be populated after memory refactor
            index_size_mb=0.0,                   # reserved
            total_writes=self._total_writes,
            writes_by_type=dict(self._writes_by_type),
            write_failures=self._write_failures,
            avg_memory_tokens_per_request=0.0,   # reserved
            memory_token_ratio=0.0,              # reserved
        )

    def _build_general(self, task_count: int) -> AgentGeneralMetrics:
        total_in = sum(self._input_tokens)
        total_out = sum(self._output_tokens)
        avg_tokens = _safe_div(total_in + total_out, task_count)
        avg_ttft = _safe_div(sum(self._ttft_list), len(self._ttft_list))
        avg_tps = _safe_div(sum(self._tps_list), len(self._tps_list))
        avg_e2e = _safe_div(sum(self._e2e_durations), len(self._e2e_durations))
        avg_ctx = int(_safe_div(sum(self._input_tokens), len(self._input_tokens)))

        # cost_by_model: placeholder (no pricing data)
        cost_by_model: dict[str, float] = {}
        all_models = set(self._model_input_tokens.keys()) | set(self._model_output_tokens.keys())
        for model in all_models:
            cost_by_model[model] = 0.0

        return AgentGeneralMetrics(
            total_input_tokens=total_in,
            total_output_tokens=total_out,
            avg_tokens_per_task=round(avg_tokens, 1),
            cost_usd=0.0,                        # reserved: no pricing config yet
            cost_by_model=cost_by_model,
            avg_ttft_ms=round(avg_ttft, 1),
            avg_tps=round(avg_tps, 1),
            avg_e2e_latency_ms=round(avg_e2e, 1),
            p95_e2e_latency_ms=round(_p95(self._e2e_durations), 1),
            avg_context_length=avg_ctx,
            context_overflow_count=0,            # reserved
        )
