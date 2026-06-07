# Agent 数据统计模块设计文档

> 版本：v1.1 | 更新日期：2026-06-07
>
> 本文档定义 Agent 框架中数据统计模块的完整指标体系与采集机制，用于优化前后效果对比。
>
> v1.1 变更：精简冗余字段，从 ~60 个压缩到 ~40 个，移除标注依赖指标和可推导字段。

---

## 一、设计背景

### 从优化场景倒推需求

| 优化场景 | 需要回答的问题 | 对应的指标 |
|---------|--------------|-----------|
| "工具调用总是出错" | 哪些工具错误率高？ | 工具成功率、错误分布 |
| "对话轮数太多" | 哪个环节在绕弯？平均几轮解决？ | ReAct 循环深度、空转率 |
| "记忆系统拖慢了速度" | 记忆检索耗时多少？命中率多少？ | 记忆检索延迟、命中率 |
| "Skill 触发频率异常" | 哪个 Skill 被高频触发？ | Skill 触发统计、触发率 |
| "整体太贵了" | token 花在哪了？ | token 用量分解、成本归因 |
| "优化后效果变了吗" | 优化前后对比 | 上述所有指标的 diff |

---

## 二、指标体系总览

```
AgentRunSnapshot（一次运行快照）
├── ReactLoopMetrics       ReAct 循环指标
├── ToolCallMetrics        工具系统指标
├── SkillMetrics           Skill 系统指标
├── MemoryMetrics          记忆系统指标
└── AgentGeneralMetrics    通用 Agent 评测指标
```

---

## 三、ReAct 循环指标

```python
@dataclass(frozen=True)
class ReactLoopMetrics:
    """ReAct 循环指标"""

    # ── 循环深度 ──
    total_loops: int                           # 总循环次数
    avg_loops_per_task: float                  # 平均每个任务循环次数
    max_loops_single_task: int                 # 单任务最大循环次数

    # ── 循环效率 ──
    task_completion_rate: float                # 任务完成率（成功/总任务）—— 唯一成功率入口
    empty_action_rate: float                   # 空转率：LLM 输出了思考但没有行动的循环占比
    redundant_action_rate: float               # 冗余行动率：重复调用同一工具+同参数的占比
    avg_reasoning_tokens_per_loop: int         # 每轮平均推理 token 数

    # ── 耗时 ──
    avg_loop_duration_ms: float                # 平均每轮耗时（含 LLM 推理 + 工具执行）
    avg_llm_duration_ms: float                 # 平均 LLM 推理耗时
    avg_tool_duration_ms: float                # 平均工具执行耗时
    p95_loop_duration_ms: float                # P95 循环耗时
```

### 与 v1.0 相比删除的字段

| 删除字段 | 原因 |
|---------|------|
| `loop_depth_distribution` | 可从原始事件流按需计算，不需要固化到快照 |
| `avg_loops_to_failure` | 失败样本太少，均值无统计意义 |
| `self_correction_count` | "自我纠正"缺乏可靠检测规则，定义模糊 |
| `backtracking_count` | "策略回退"与正常 ReAct 探索边界不清 |
| `p50_loop_duration_ms` | 通常接近均值，信息增量小 |
| `p99_loop_duration_ms` | 小样本下抖动剧烈，P95 已足够 |

### 指标诊断指南

| 指标异常 | 可能原因 | 优化方向 |
|---------|---------|---------|
| `empty_action_rate` 高 | LLM 在空想，prompt 太模糊 | 优化 Thought 引导语 |
| `redundant_action_rate` 高 | LLM 重复尝试，工具返回值不清晰 | 优化工具返回格式 |
| `avg_loops_per_task` 高 | 任务拆分不足或规划能力弱 | 优化规划 prompt 或子任务拆分 |
| `p95_loop_duration_ms` 显著高于 avg | 长尾请求存在异常慢的工具或 LLM 调用 | 排查 P95 对应的具体事件 |

---

## 四、工具系统指标

```python
@dataclass(frozen=True)
class ToolCallMetrics:
    """工具调用指标"""

    # ── 调用统计 ──
    total_calls: int                           # 总调用次数
    calls_by_tool: dict[str, int]              # 按工具分：{"read": 15, "exec": 8, ...}

    # ── 成功率 ──
    overall_success_rate: float                # 总成功率
    success_rate_by_tool: dict[str, float]     # 按工具分

    # ── 错误分析 ──
    errors_by_tool: dict[str, int]             # 错误次数：{"exec": 3, "read": 1}
    errors_by_type: dict[str, int]             # 错误类型：{"timeout": 5, "permission": 2}
    retry_rate: float                          # 重试率（同一工具连续调用次数>1的占比）

    # ── 耗时 ──
    avg_duration_by_tool: dict[str, float]     # 按工具平均耗时(ms)
    p95_duration_by_tool: dict[str, float]     # P95
```

### 与 v1.0 相比删除的字段

| 删除字段 | 原因 |
|---------|------|
| `calls_distribution` | = `calls_by_tool[t] / total_calls`，纯衍生值 |
| `total_tool_time_ms` | 可从 `avg_duration_by_tool × count` 推导 |
| `tool_selection_accuracy` | 需要标注"用户实际意图 vs 正确工具"，CLI 框架无此数据 |
| `tool_selection_errors` | 同上，附带 `ToolSelectionError` dataclass 一并删除 |

---

## 五、Skill 系统指标

```python
@dataclass(frozen=True)
class SkillMetrics:
    """Skill 系统指标"""

    # ── 触发统计 ──
    total_triggers: int                        # 总触发次数
    triggers_by_skill: dict[str, int]          # 按 Skill 分
    trigger_rate: float                        # 请求中触发了 Skill 的占比

    # ── 加载性能 ──
    avg_body_load_ms: float                    # Skill 内容加载耗时（body + reference 合并）
    body_cache_hit_rate: float                 # Skill 内容缓存命中率（body + reference 合并）

    # ── Skill 内执行 ──
    avg_scripts_per_trigger: float             # 每次触发平均执行脚本数
    script_success_rate: float                 # 脚本执行成功率
    avg_skill_duration_ms: float               # Skill 相关总耗时（含脚本执行）
    token_overhead_per_skill: float            # 每个 Skill 注入的平均 token 开销
```

### 与 v1.0 相比删除的字段

| 删除字段 | 原因 |
|---------|------|
| `trigger_precision` / `trigger_recall` | 需要标注"正确触发/漏触发"，CLI 框架无此数据 |
| `false_triggers` / `missed_triggers` | 同上，附带 `FalseTriggerRecord`/`MissedTriggerRecord` 一并删除 |
| `avg_reference_load_ms` | body 和 reference 是同一加载流程，拆分意义不大 |
| `reference_cache_hit_rate` | 同上，合并到 `body_cache_hit_rate` 统一统计 |

---

## 六、记忆系统指标

```python
@dataclass(frozen=True)
class MemoryMetrics:
    """记忆系统指标"""

    # ── 检索统计 ──
    total_retrievals: int                      # 总检索次数
    retrieval_rate: float                      # 请求中使用记忆的占比

    # ── 检索质量 ──
    hit_rate: float                            # 命中率（返回了非空结果的占比）

    # ── 检索性能 ──
    avg_retrieval_ms: float                    # 平均检索延迟
    p95_retrieval_ms: float                    # P95
    index_size: int                            # 索引文档数
    index_size_mb: float                       # 索引体积

    # ── 写入统计 ──
    total_writes: int                          # 总写入次数
    writes_by_type: dict[str, int]             # 按类型：{"daily_note": 5, "long_term": 2}
    write_failures: int                        # 写入失败次数

    # ── 上下文影响 ──
    avg_memory_tokens_per_request: float       # 每次请求注入的记忆平均 token 数
    memory_token_ratio: float                  # 记忆 token 占总 prompt token 的比例
```

### 与 v1.0 相比删除的字段

| 删除字段 | 原因 |
|---------|------|
| `avg_relevance_score` | 取决于检索系统是否提供分数，当前系统未必支持 |
| `top1_useful_rate` | 需要判断 LLM 是否"采纳"检索结果，本身需要 LLM-as-Judge，不可靠 |

---

## 七、通用 Agent 评测指标

```python
@dataclass(frozen=True)
class AgentGeneralMetrics:
    """通用 Agent 评测指标"""

    # ── Token & 成本 ──
    total_input_tokens: int                    # 总输入 token
    total_output_tokens: int                   # 总输出 token
    avg_tokens_per_task: float                 # 每任务平均 token
    cost_usd: float                            # 总成本（美元）
    cost_by_model: dict[str, float]            # 按模型分

    # ── 延迟 ──
    avg_ttft_ms: float                         # Time To First Token（首 token 延迟）
    avg_tps: float                             # Tokens Per Second（生成速度）
    avg_e2e_latency_ms: float                  # 端到端延迟（用户发送→最终回复）
    p95_e2e_latency_ms: float

    # ── 上下文效率 ──
    avg_context_length: int                    # 平均上下文长度（token）
    context_overflow_count: int                # 上下文溢出次数
```

### 与 v1.0 相比删除的字段

| 删除字段 | 原因 |
|---------|------|
| `total_tokens` | = `total_input_tokens + total_output_tokens`，一行加法 |
| `task_success_rate` | 与 `ReactLoopMetrics.task_completion_rate` 重复，保留 React 侧唯一入口 |
| `avg_quality_score` | 需要 LLM-as-Judge 自动评分，引入不可靠环节 |
| `user_satisfaction_rate` | CLI 框架无用户反馈机制 |
| `context_utilization` | "有效信息/总长度"中的"有效信息"缺乏可靠定义 |
| `total_sessions` / `avg_turns_per_session` / `avg_tasks_per_session` / `session_duration_distribution` | 跨运行全局统计，不适合单次 A/B 快照 |

---

## 八、聚合：运行快照与对比

### AgentRunSnapshot

```python
@dataclass(frozen=True)
class AgentRunSnapshot:
    """
    一次评测运行的所有指标快照。
    用于 A/B 对比：优化前 vs 优化后。
    """
    run_id: str                        # 运行标识
    timestamp: str                     # ISO-8601
    git_commit: str                    # 代码版本
    config_hash: str                   # 配置指纹
    test_dataset: str                  # 测试数据集标识
    test_dataset_size: int             # 测试样本数

    # 各子系统指标
    react: ReactLoopMetrics
    tools: ToolCallMetrics
    skills: SkillMetrics
    memory: MemoryMetrics
    general: AgentGeneralMetrics
```

### ComparisonReport

```python
@dataclass(frozen=True)
class ComparisonReport:
    """两次运行的对比报告"""
    baseline: AgentRunSnapshot         # 基线（优化前）
    candidate: AgentRunSnapshot        # 候选（优化后）
    diffs: dict[str, float]            # 指标名 → 变化百分比
    regressions: tuple[str, ...]       # 退化的指标列表
    improvements: tuple[str, ...]      # 改善的指标列表
```

### 对比逻辑

```python
def _is_improvement(field: str, pct: float) -> bool:
    """
    判断指标变化是改善还是退化。
    成功率类指标上升是改善，耗时/成本类指标下降是改善。
    """
    higher_is_better = {"rate", "accuracy", "precision", "recall", "hit_rate", "success"}
    lower_is_better = {"duration", "latency", "ms", "tokens", "cost", "loops", "errors"}
    if any(k in field for k in higher_is_better):
        return pct > 2.0   # 超过 2% 算显著改善
    if any(k in field for k in lower_is_better):
        return pct < -2.0  # 下降超过 2% 算显著改善
    return False
```

---

## 九、采集机制：零侵入事件订阅

### 设计原则

**不在业务代码里写埋点，用事件订阅模式。**

```
业务代码 ──emit_event──▶ MetricsCollector.on_event(event)
                              │
                              ▶ 写入事件流（内存）
                              │
                         compute_snapshot()
                              │
                              ▶ AgentRunSnapshot
```

### 事件定义

```python
@dataclass(frozen=True)
class AgentEvent:
    """Agent 运行时事件"""
    timestamp: float              # Unix timestamp (ms)
    event_type: str               # 事件类型
    data: dict[str, Any]          # 事件数据
```

### 事件类型枚举

| 事件类型 | 触发时机 | data 关键字段 |
|---------|---------|-------------|
| `react.loop_start` | ReAct 循环开始 | `loop_index`, `thought` |
| `react.loop_end` | ReAct 循环结束 | `loop_index`, `action`, `duration_ms` |
| `react.empty_action` | LLM 输出思考但无行动 | `loop_index` |
| `tool.call_start` | 工具调用开始 | `tool_name`, `tool_input` |
| `tool.call_end` | 工具调用结束 | `tool_name`, `success`, `duration_ms`, `error` |
| `skill.trigger` | Skill 被触发 | `skill_name`, `trigger_source` |
| `skill.body_loaded` | Skill 内容加载完成 | `skill_name`, `token_count`, `duration_ms` |
| `skill.script_exec` | Skill 脚本执行 | `skill_name`, `script_path`, `success` |
| `memory.retrieval` | 记忆检索 | `query`, `hit`, `top_k`, `duration_ms` |
| `memory.write` | 记忆写入 | `memory_type`, `success` |
| `llm.request_start` | LLM 请求开始 | `model`, `input_tokens` |
| `llm.request_end` | LLM 请求结束 | `model`, `output_tokens`, `duration_ms` |
| `session.start` | 会话开始 | `session_id` |
| `session.end` | 会话结束 | `session_id`, `total_loops`, `success` |

### 采集器实现

```python
class MetricsCollector:
    """
    指标采集器：通过事件订阅收集，业务代码零侵入。
    业务代码只需调用 emit_event()，采集器自动聚合。
    """

    def __init__(self):
        self._events: list[AgentEvent] = []

    def on_event(self, event: AgentEvent):
        """订阅 Agent 运行时事件（供 Hook 调用）"""
        self._events.append(event)

    def compute_snapshot(self, run_meta: RunMeta) -> AgentRunSnapshot:
        """从事件流计算指标快照"""
        builder = SnapshotBuilder(run_meta)

        for event in self._events:
            builder.process(event)

        return builder.build()
```

### 业务代码接入示例

```python
# ReAct 循环中（无侵入，只需 emit）
def run_react_loop(user_message: str, collector: MetricsCollector):
    loop_index = 0
    while loop_index < max_loops:
        loop_start = time.time()

        collector.on_event(AgentEvent(
            timestamp=loop_start * 1000,
            event_type="react.loop_start",
            data={"loop_index": loop_index},
        ))

        # ... LLM 推理 ...

        collector.on_event(AgentEvent(
            timestamp=time.time() * 1000,
            event_type="react.loop_end",
            data={
                "loop_index": loop_index,
                "action": action_name,
                "duration_ms": (time.time() - loop_start) * 1000,
            },
        ))
        loop_index += 1
```

---

## 十、指标优先级

| 优先级 | 指标 | 理由 |
|--------|------|------|
| 🔴 **P0** | 任务完成率、平均循环次数、端到端延迟、token 用量、成本 | 不测这些等于盲飞 |
| 🟡 **P1** | 工具调用成功率、Skill 触发率、记忆命中率、空转率、冗余行动率 | 定位瓶颈，P0 异常时排查用 |
| ⚪ **P2** | P95 耗时、上下文溢出次数、token 分解 | 精细优化阶段用 |

---

## 十一、存储格式

### 单次运行快照（JSON）

```json
{
  "run_id": "run_20260607_001",
  "timestamp": "2026-06-07T15:30:00+08:00",
  "git_commit": "a1b2c3d",
  "config_hash": "x9y8z7",
  "test_dataset": "bench_v1",
  "test_dataset_size": 50,
  "react": {
    "total_loops": 127,
    "avg_loops_per_task": 2.54,
    "max_loops_single_task": 8,
    "task_completion_rate": 0.94,
    "empty_action_rate": 0.03,
    "redundant_action_rate": 0.05,
    "avg_reasoning_tokens_per_loop": 420,
    "avg_loop_duration_ms": 2340.5,
    "avg_llm_duration_ms": 1800.0,
    "avg_tool_duration_ms": 540.5,
    "p95_loop_duration_ms": 8700.0
  },
  "tools": {
    "total_calls": 203,
    "calls_by_tool": {"read": 85, "exec": 62, "write": 56},
    "overall_success_rate": 0.91,
    "success_rate_by_tool": {"read": 0.96, "exec": 0.85, "write": 0.92},
    "errors_by_tool": {"exec": 9, "write": 4},
    "errors_by_type": {"timeout": 7, "permission": 4, "parse_error": 2},
    "retry_rate": 0.12,
    "avg_duration_by_tool": {"read": 120.5, "exec": 450.0, "write": 200.0},
    "p95_duration_by_tool": {"read": 250.0, "exec": 1200.0, "write": 500.0}
  },
  "skills": {
    "total_triggers": 47,
    "triggers_by_skill": {"xbrowser": 12, "pdf": 8, "xlsx": 7},
    "trigger_rate": 0.94,
    "avg_body_load_ms": 35.2,
    "body_cache_hit_rate": 0.82,
    "avg_scripts_per_trigger": 1.3,
    "script_success_rate": 0.97,
    "avg_skill_duration_ms": 180.0,
    "token_overhead_per_skill": 350
  },
  "memory": {
    "total_retrievals": 50,
    "retrieval_rate": 1.0,
    "hit_rate": 0.76,
    "avg_retrieval_ms": 85.3,
    "p95_retrieval_ms": 210.0,
    "index_size": 42,
    "index_size_mb": 0.85,
    "total_writes": 12,
    "writes_by_type": {"daily_note": 10, "long_term": 2},
    "write_failures": 0,
    "avg_memory_tokens_per_request": 320,
    "memory_token_ratio": 0.05
  },
  "general": {
    "total_input_tokens": 98500,
    "total_output_tokens": 58280,
    "avg_tokens_per_task": 3135.6,
    "cost_usd": 0.47,
    "cost_by_model": {"deepseek-v4": 0.32, "gpt-4o-mini": 0.15},
    "avg_ttft_ms": 420.0,
    "avg_tps": 85.3,
    "avg_e2e_latency_ms": 8900.0,
    "p95_e2e_latency_ms": 15200.0,
    "avg_context_length": 12800,
    "context_overflow_count": 0
  }
}
```

### 对比报告（Markdown，用于人工审阅）

```markdown
## 对比报告：优化前 vs 优化后

| 指标 | 优化前 | 优化后 | 变化 | 判定 |
|------|--------|--------|------|------|
| 任务完成率 | 0.89 | 0.94 | +5.6% | ✅ 改善 |
| 平均循环次数 | 3.12 | 2.54 | -18.6% | ✅ 改善 |
| 端到端延迟(ms) | 11200 | 8900 | -20.5% | ✅ 改善 |
| token 用量/任务 | 3850 | 3135 | -18.6% | ✅ 改善 |
| 工具调用成功率 | 0.87 | 0.91 | +4.6% | ✅ 改善 |
| 记忆命中率 | 0.68 | 0.76 | +11.8% | ✅ 改善 |
| Skill 触发率 | 0.92 | 0.94 | +2.2% | ✅ 改善 |

**结论**：优化方向正确，所有关键指标均有改善，无明显退化。
```
