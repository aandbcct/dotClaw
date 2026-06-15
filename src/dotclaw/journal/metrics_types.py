"""指标数据类定义。

全部 frozen=True，不可变，保证快照的幂等性。
"""

from dataclasses import dataclass, field


# =============================================================================
# RunMeta
# =============================================================================


@dataclass(frozen=True)
class RunMeta:
    """运行元信息，标识一次评测运行的上下文。"""

    run_id: str
    timestamp: str
    git_commit: str
    config_hash: str
    test_dataset: str
    test_dataset_size: int


# =============================================================================
# ReactLoopMetrics（11 字段）
# =============================================================================


@dataclass(frozen=True)
class ReactLoopMetrics:
    """ReAct 循环指标。"""

    # ── 循环深度 ──
    total_loops: int = 0
    avg_loops_per_task: float = 0.0
    max_loops_single_task: int = 0

    # ── 循环效率 ──
    task_completion_rate: float = 0.0
    empty_action_rate: float = 0.0
    redundant_action_rate: float = 0.0
    avg_reasoning_tokens_per_loop: int = 0

    # ── 耗时 ──
    avg_loop_duration_ms: float = 0.0
    avg_llm_duration_ms: float = 0.0
    avg_tool_duration_ms: float = 0.0
    p95_loop_duration_ms: float = 0.0


# =============================================================================
# ToolCallMetrics（9 字段）
# =============================================================================


@dataclass(frozen=True)
class ToolCallMetrics:
    """工具调用指标。"""

    # ── 调用统计 ──
    total_calls: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)

    # ── 成功率 ──
    overall_success_rate: float = 0.0
    success_rate_by_tool: dict[str, float] = field(default_factory=dict)

    # ── 错误分析 ──
    errors_by_tool: dict[str, int] = field(default_factory=dict)
    errors_by_type: dict[str, int] = field(default_factory=dict)
    retry_rate: float = 0.0

    # ── 耗时 ──
    avg_duration_by_tool: dict[str, float] = field(default_factory=dict)
    p95_duration_by_tool: dict[str, float] = field(default_factory=dict)


# =============================================================================
# SkillMetrics（9 字段）
# =============================================================================


@dataclass(frozen=True)
class SkillMetrics:
    """Skill 系统指标。"""

    # ── 触发统计 ──
    total_triggers: int = 0
    triggers_by_skill: dict[str, int] = field(default_factory=dict)
    trigger_rate: float = 0.0

    # ── 加载性能 ──
    avg_body_load_ms: float = 0.0
    body_cache_hit_rate: float = 0.0

    # ── Skill 内执行 ──
    avg_scripts_per_trigger: float = 0.0
    script_success_rate: float = 0.0
    avg_skill_duration_ms: float = 0.0
    token_overhead_per_skill: float = 0.0


# =============================================================================
# MemoryMetrics（12 字段）
# =============================================================================


@dataclass(frozen=True)
class MemoryMetrics:
    """记忆系统指标。"""

    # ── 检索统计 ──
    total_retrievals: int = 0
    retrieval_rate: float = 0.0

    # ── 检索质量 ──
    hit_rate: float = 0.0

    # ── 检索性能 ──
    avg_retrieval_ms: float = 0.0
    p95_retrieval_ms: float = 0.0
    index_size: int = 0
    index_size_mb: float = 0.0

    # ── 写入统计 ──
    total_writes: int = 0
    writes_by_type: dict[str, int] = field(default_factory=dict)
    write_failures: int = 0

    # ── 上下文影响 ──
    avg_memory_tokens_per_request: float = 0.0
    memory_token_ratio: float = 0.0


# =============================================================================
# AgentGeneralMetrics（11 字段）
# =============================================================================


@dataclass(frozen=True)
class AgentGeneralMetrics:
    """通用 Agent 评测指标。"""

    # ── Token & 成本 ──
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    avg_tokens_per_task: float = 0.0
    cost_usd: float = 0.0
    cost_by_model: dict[str, float] = field(default_factory=dict)

    # ── 延迟 ──
    avg_ttft_ms: float = 0.0
    avg_tps: float = 0.0
    avg_e2e_latency_ms: float = 0.0
    p95_e2e_latency_ms: float = 0.0

    # ── 上下文效率 ──
    avg_context_length: int = 0
    context_overflow_count: int = 0


# =============================================================================
# Benchmark 专用指标
# =============================================================================


@dataclass(frozen=True)
class InitPerfMetrics:
    """初始化性能评测指标 — 各核心组件构造耗时（P50/P95, 毫秒）。"""

    config_load_ms: float = 0.0
    config_load_p95_ms: float = 0.0
    llm_build_ms: float = 0.0
    llm_build_p95_ms: float = 0.0
    skill_scan_ms: float = 0.0
    skill_scan_p95_ms: float = 0.0
    tool_build_ms: float = 0.0
    tool_build_p95_ms: float = 0.0
    session_mgr_ms: float = 0.0
    session_mgr_p95_ms: float = 0.0
    prompt_builder_ms: float = 0.0
    prompt_builder_p95_ms: float = 0.0
    memory_build_ms: float = 0.0
    memory_build_p95_ms: float = 0.0
    agent_full_ms: float = 0.0
    agent_full_p95_ms: float = 0.0


# =============================================================================
# AgentRunSnapshot
# =============================================================================


@dataclass(frozen=True)
class AgentRunSnapshot:
    """一次评测运行的所有指标快照。

    包含 6 个元信息字段 + 5 个子系统指标对象。
    """

    # ── 元信息 ──
    run_id: str = ""
    timestamp: str = ""
    git_commit: str = ""
    config_hash: str = ""
    test_dataset: str = ""
    test_dataset_size: int = 0

    # ── 子系统指标 ──
    react: ReactLoopMetrics = field(default_factory=ReactLoopMetrics)
    tools: ToolCallMetrics = field(default_factory=ToolCallMetrics)
    skills: SkillMetrics = field(default_factory=SkillMetrics)
    memory: MemoryMetrics = field(default_factory=MemoryMetrics)
    general: AgentGeneralMetrics = field(default_factory=AgentGeneralMetrics)
