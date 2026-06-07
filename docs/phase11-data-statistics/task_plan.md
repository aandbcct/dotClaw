# 数据统计模块 — 开发执行计划

> 项目：dotClaw Phase 11 - Agent 数据统计模块
> 创建日期：2026-06-07
> 状态：planning
> 参考文档：
> - 需求文档：docs/phase11-data-statistics/data-statistics-requirements.md
> - 设计文档：docs/phase11-data-statistics/data-statistics-design.md

---

## 目标

为 dotClaw Agent 框架构建零侵入的数据统计模块，支持运行时事件采集、指标快照计算、优化前后 A/B 对比。长期可维护，随框架演进逐步扩展。

## 设计原则

| 原则 | 说明 |
|------|------|
| **零业务侵入** | 业务代码只调用 `emit_event()`，不写埋点逻辑 |
| **关注点分离** | 事件采集 / 指标计算 / 对比分析 / 序列化 四层独立 |
| **可测试优先** | 每个组件都是纯函数或可 mock 的类，核心逻辑 100% 可单测 |
| **渐进式接入** | 没有采集器时 Agent 正常运行，有采集器时自动生效 |
| **接口稳定** | 核心接口（AgentEvent、RunMeta、MetricsCollector）设计好之后不轻易改 |

## 模块结构规划

```
src/
├── metrics/                          # 新增：数据统计模块
│   ├── __init__.py                   # 模块入口，导出公共接口
│   ├── events.py                     # AgentEvent 定义 + 事件类型常量
│   ├── collector.py                  # MetricsCollector（事件采集器）
│   ├── snapshot.py                   # 5 个 Metrics dataclass + AgentRunSnapshot + RunMeta
│   ├── builder.py                    # SnapshotBuilder（事件→指标计算）
│   ├── comparator.py                 # ComparisonReport + 对比逻辑
│   └── storage.py                    # JSON 序列化/反序列化/文件管理
tests/
├── metrics/                          # 新增：测试目录
│   ├── __init__.py
│   ├── test_events.py
│   ├── test_collector.py
│   ├── test_snapshot.py
│   ├── test_builder.py
│   ├── test_comparator.py
│   ├── test_storage.py
│   └── test_integration.py           # 端到端集成测试
```

---

## Phase 0：模块骨架与核心数据类型

**目标**：创建模块目录，定义所有 dataclass，确保类型体系完整、冻结（`frozen=True`）、无外部依赖。

**前置依赖**：无

### Step 0.1 — 创建模块目录

- [ ] 创建 `src/metrics/` 目录和 `__init__.py`
- [ ] 创建 `tests/metrics/` 目录和 `__init__.py`
- [ ] 在 `src/metrics/__init__.py` 中导出公共接口

### Step 0.2 — 定义事件类型（events.py）

- [ ] 定义 `AgentEvent` dataclass（`frozen=True`）
- [ ] 定义事件类型字符串常量（14 种）
  ```python
  EventType = {
      "REACT_LOOP_START": "react.loop_start",
      "REACT_LOOP_END": "react.loop_end",
      "REACT_EMPTY_ACTION": "react.empty_action",
      "TOOL_CALL_START": "tool.call_start",
      "TOOL_CALL_END": "tool.call_end",
      "SKILL_TRIGGER": "skill.trigger",
      "SKILL_BODY_LOADED": "skill.body_loaded",
      "SKILL_SCRIPT_EXEC": "skill.script_exec",
      "MEMORY_RETRIEVAL": "memory.retrieval",
      "MEMORY_WRITE": "memory.write",
      "LLM_REQUEST_START": "llm.request_start",
      "LLM_REQUEST_END": "llm.request_end",
      "SESSION_START": "session.start",
      "SESSION_END": "session.end",
  }
  ```

### Step 0.3 — 定义快照类型（snapshot.py）

- [ ] 定义 `ReactLoopMetrics`（11 字段）
- [ ] 定义 `ToolCallMetrics`（9 字段）
- [ ] 定义 `SkillMetrics`（9 字段）
- [ ] 定义 `MemoryMetrics`（12 字段）
- [ ] 定义 `AgentGeneralMetrics`（11 字段）
- [ ] 定义 `AgentRunSnapshot`（6 元信息 + 5 指标对象）
- [ ] 定义 `RunMeta`（运行元信息，供 SnapshotBuilder 使用）
- [ ] 定义 `ComparisonReport`（baseline + candidate + diffs + regressions + improvements）
- **所有 dataclass 必须 `frozen=True`**

### Step 0.4 — 编写类型级测试

- [ ] 验证每个 Metrics 类可以正常构造
- [ ] 验证 `frozen=True`（禁止修改）
- [ ] 验证 `AgentRunSnapshot` 可以组��所有子指标

**验收标准**：
- `from src.metrics import AgentEvent, ReactLoopMetrics, ...` 全部导入成功
- 所有 dataclass 实例化不报错，字段数与设计文档一致
- 类型检查通过（`frozen=True` 生效）

**预估文件**：
- 新增：`src/metrics/__init__.py`、`src/metrics/events.py`、`src/metrics/snapshot.py`
- 新增：`tests/metrics/__init__.py`、`tests/metrics/test_snapshot.py`

---

## Phase 1：事件采集器

**目标**：实现 MetricsCollector，业务代码可以通过 `collector.on_event(event)` 发布事件。

**前置依赖**：Phase 0

### Step 1.1 — 实现 MetricsCollector（collector.py）

- [ ] 内部维护 `list[AgentEvent]` 事件流
- [ ] `on_event(event: AgentEvent)` — 追加事件，异常不抛出
- [ ] `on_event()` 应极轻量（<1ms）
- [ ] `clear()` — 清空事件流
- [ ] `event_count` — 属性，返回事件数
- [ ] `events` — 属性，返回当前事件列表（只读副本或直接引用，注明调用方不得修改）
- [ ] 可选：添加 `is_active` 开关，关闭时不记录事件

### Step 1.2 — 容错机制

- [ ] `on_event()` 内部用 try/except 包裹，异常时静默丢弃事件
- [ ] 事件数据格式不合法时（如缺失关键字段），记录 warning 但不中断
- [ ] 采集器自身异常不传播到调用方

### Step 1.3 — 编写采集器测试

- [ ] 测试正常追加事件
- [ ] 测试大量事件（10000+）的内存和性能
- [ ] 测试 `clear()` 后事件列表为空
- [ ] 测试异常数据不导致崩溃
- [ ] 测试 `is_active=False` 时不记录事件

**验收标准**：
- 采��器可正常工作，不影响主流程
- `on_event()` 在 10000 事件压力下总耗时 < 100ms
- 采集器自身异常不会导致 Agent 崩溃（FR1.4）

**预估文件**：
- 新增：`src/metrics/collector.py`
- 新增：`tests/metrics/test_collector.py`

---

## Phase 2：快照构建器

**目标**：实现 SnapshotBuilder，从事件流计算出 AgentRunSnapshot。纯数据处理，无副作用。

**前置依赖**：Phase 0, Phase 1

### Step 2.1 — 设计 SnapshotBuilder（builder.py）

核心接口：

```python
class SnapshotBuilder:
    def __init__(self, run_meta: RunMeta, task_count: int):
        """run_meta: 运行元信息；task_count: 测试任务总数"""

    def process(self, event: AgentEvent):
        """处理单个事件，内部累积中间状态"""

    def build(self) -> AgentRunSnapshot:
        """从累积的中间状态计算最终快照"""
```

### Step 2.2 — 中间状态设计

使用内部嵌套 dataclass 或字典维护中间统计：

```
_task_loops: list[int]           # 每个任务的循环轮数
_task_success: list[bool]        # 每个任务是否成功
_loop_durations: list[float]     # 每轮耗时(ms)
_llm_durations: list[float]      # 每轮 LLM 耗时
_tool_durations: list[float]     # 每轮工具耗时
_tool_calls: list[ToolCallRecord]  # 工具调用记录
_empty_actions: int              # 空转次数
_redundant_actions: int          # 冗余行动次数
_reasoning_tokens: list[int]     # 推理 token 数/轮
...
```

### Step 2.3 — 五大类指标计算逻辑

- **ReactLoopMetrics**：
  - `total_loops` = len(所有 react.loop_end)
  - `avg_loops_per_task` = total_loops / task_count
  - `task_completion_rate` = success_count / task_count
  - `empty_action_rate` = empty_count / total_loops
  - `redundant_action_rate` = redundant_count / total_loops
  - `p95_loop_duration_ms` = 排序后取 95% 位置
  - 边界：task_count=0 时所有 rate 返回 0.0

- **ToolCallMetrics**：
  - `total_calls` = len(所有 tool.call_end)
  - `overall_success_rate` = 成功数 / total_calls
  - `retry_rate` = 连续调用同一工具次数/总调用次数
  - 按工具聚合计算 success_rate、errors、avg_duration、p95_duration

- **SkillMetrics**：
  - `trigger_rate` = 触发 Skill 的请求数 / 总请求数
  - `token_overhead_per_skill` = 所有 skill.body_loaded 的 token 总和 / 触发次数

- **MemoryMetrics**：
  - `hit_rate` = 命中次数 / total_retrievals（"命中" = 检索返回非空结果）
  - `index_size` / `index_size_mb` — **暂不采集，返回 0 / 0.0**（记忆系统重构时再接入）

- **AgentGeneralMetrics**：
  - `total_input_tokens` = sum(所有 llm.request_start 的 input_tokens)
  - `cost_usd` — **暂不计算**，返回 0.0（当前配置体系无价格信息，字段保留为未来预留）
  - `cost_by_model` — **暂不计算**，返回空 dict
  - `avg_ttft_ms` — 由客户端侧计算（request_start 时间戳 → 首个 response chunk 时间戳）
  - `avg_tps` — 由客户端侧计算（output_tokens / (末 chunk - 首 chunk 时间)）
  - `avg_context_length` = 平均每次请求的 input_tokens

### Step 2.4 — 事件→指标映射表

| 事件类型 | 贡献的指标 |
|---------|----------|
| `session.start` | 会话计数 |
| `session.end` | 任务成败、总轮数 |
| `react.loop_start` | 循环计数 |
| `react.loop_end` | 循环耗时、动作类型 |
| `react.empty_action` | 空转数 |
| `tool.call_start` | 工具调用序列（用于检测冗余） |
| `tool.call_end` | 工具成功率、耗时、错误 |
| `skill.trigger` | Skill 触发次数 |
| `skill.body_loaded` | Skill token 开销、加载耗时 |
| `skill.script_exec` | 脚本执行成功率 |
| `memory.retrieval` | 检索次数、命中率、延迟 |
| `memory.write` | 写入次数、失败数 |
| `llm.request_start` | 输入 token 数 |
| `llm.request_end` | 输出 token 数、延迟、TTFT、TPS |

### Step 2.5 — 编写构建器测试

- [ ] 给定固定事件流，验证快照各字段值（Golden Test）
- [ ] 测试空事件流 → 快照全为 0/空
- [ ] 测试幂等性：同一事件流两次 build() 结果相同
- [ ] 测试边界：task_count=0, 除以零场景
- [ ] 测试 P95 计算正确性
- [ ] 测试重试率计算（模拟连续调用同一工具）

**验收标准**：
- 快照字段总数与设计文档一致
- Golden Test 全通过
- 所有边界情况不崩溃，返回合理的默认值

**预估文件**：
- 新增：`src/metrics/builder.py`
- 新增：`tests/metrics/test_builder.py`

---

## Phase 3：业务代码接入（事件发射点）

**目标**：在 Agent 核心循环、工具执行、Skill 加载、记忆系统、LLM 调用等位置插入 `emit_event()`。

**前置依赖**：Phase 0, Phase 1 （Phase 2 的 builder 可以并行开发）

**核心原则**：每个 `emit_event` 调用尽量只占用 1-2 行代码，不改变原有控制流。

### Step 3.1 — LLM 层接入

- [ ] 在 `src/llm/proxy.py` 的 LLM 请求前后发射 `llm.request_start` / `llm.request_end`
- [ ] 事件数据：model 名称、input_tokens、output_tokens、duration_ms
- [ ] **TTFT 客户端计算**：在 `llm.request_start` 记录 `perf_counter()`，收到首个响应 chunk 时计算差值写入 `llm.request_end` 事件的 `ttft_ms` 字段
- [ ] **TPS 客户端计算**：记录首 chunk 和末 chunk 时间戳，`tps = output_tokens / (末时间 - 首时间)`，流式响应无 chunk 时 tps=0
- [ ] 通过 AgentContext 获取 MetricsCollector（非全局单例）

### Step 3.2 — ReAct 循环接入

- [ ] 在 `src/agent/loop.py` 的循环开始发射 `react.loop_start`
- [ ] 在循环结束发射 `react.loop_end`（含 action 名称和 duration_ms）
- [ ] 检测 empty_action（无工具调用时发射）

### Step 3.3 — 工具执行层接入

- [ ] 在 `src/agent/tools/executor.py` 的工具调用前后发射 `tool.call_start` / `tool.call_end`
- [ ] 事件数据：tool_name、tool_input（可截断）、success、duration_ms、error 信息

### Step 3.4 — Skill 系统接入

- [ ] 在 Skill 触发点发射 `skill.trigger`
- [ ] 在 `src/skills/loader.py` 加载完成时发射 `skill.body_loaded`
- [ ] 在脚本执行后发射 `skill.script_exec`

### Step 3.5 — 记忆系统接入

- [ ] 在 `src/memory/` 检索前后发射 `memory.retrieval`
- [ ] 在写入后发射 `memory.write`

### Step 3.6 — 会话层接入

- [ ] 在会话开始时发射 `session.start`
- [ ] 在会话结束时发射 `session.end`（含 total_loops 和 success）

### Step 3.7 — 依赖注入设计

**关键架构决策**：MetricsCollector 如何传递给各模块？

方案：**Context 注入** — 在 `AgentContext` 中增加可选的 `metrics_collector` 字段。
- ❌ 不用全局单例（不可测试、多实例冲突）
- ✅ 通过 `AgentContext.metrics_collector: MetricsCollector | None` 传递
- 各模块从 Context 获取，若为 None 则跳过埋点
- 测试时可以创建独立 collector 实例

### Step 3.8 — 验证零侵入

- [ ] 将 `metrics_collector` 设为 None，Agent 正常运行
- [ ] 不修改任何业务方法的签名（通过 Context 而非函数参数传递）
- [ ] 不增加业务方法的 `if collector:` 之外的控制流分支

**验收标准**：
- Agent 正常运行时，所有 14 种事件类型都被采集到
- `metrics_collector=None` 时 Agent 行为不变
- 任意模块异常不影响事件发射链

**预估文件**：
- 修改：`src/agent/context.py`（增加 metrics_collector 字段）
- 修改：`src/llm/proxy.py`（LLM 事件埋点）
- 修改：`src/agent/loop.py`（ReAct 事件埋点）
- 修改：`src/agent/tools/executor.py`（工具事件埋点）
- 修改：`src/skills/loader.py`（Skill 事件埋点）
- 修改：`src/memory/`（记忆事件埋点）

---

## Phase 4：对比引擎

**目标**：实现 ComparisonReport 的构建，支持加载两份快照并自动判定改善/退化。

**前置依赖**：Phase 0

### Step 4.1 — 实现 flat_mapping（comparator.py）

将嵌套的 AgentRunSnapshot 扁平化为 `dict[str, Any]`，键名为点号分隔的路径（如 `react.total_loops`、`tools.success_rate_by_tool.read`）。

```python
def _flatten_snapshot(snapshot: AgentRunSnapshot) -> dict[str, Any]:
    """将嵌套快照扁平化为 {'react.total_loops': 127, ...}"""
```

### Step 4.2 — 实现 diff & 判定

```python
def compare_snapshots(
    baseline: AgentRunSnapshot,
    candidate: AgentRunSnapshot,
    min_change_pct: float = 2.0
) -> ComparisonReport:
    """对比两份快照，生成报告"""
```

- [ ] 对每个扁平化字段计算变化百分比 `(candidate - baseline) / baseline * 100`
- [ ] 应用 `_is_improvement()` 逻辑判定改善/退化
- [ ] 变化幅度 < `min_change_pct` 的指标不出现在改善/退化列表
- [ ] 处理 baseline=0 的除零情况（如 baseline 中某工具调用次数为 0）

### Step 4.3 — 编写对比测试

- [ ] 全部相同 → 无改善无退化
- [ ] 成功率从 0.85 → 0.92 → 出现在 improvements
- [ ] 延迟从 5000ms → 8000ms → 出现在 regressions
- [ ] 变化 1% 的指标不出现在列表中
- [ ] baseline 为 0 的除零处理

**验收标准**：
- 对比报告正确区分改善和退化
- 小幅度变化被过滤
- 边界情况不崩溃

**预估文件**：
- 新增：`src/metrics/comparator.py`
- 新增：`tests/metrics/test_comparator.py`

---

## Phase 5：序列化与存储

**目标**：支持 AgentRunSnapshot 的 JSON 序列化/反序列化，文件读写管理。

**前置依赖**：Phase 0, Phase 2

### Step 5.1 — JSON 序列化（storage.py）

- [ ] `snapshot_to_json(snapshot: AgentRunSnapshot) -> str` — 快照 → JSON 字符串
- [ ] `snapshot_from_json(json_str: str) -> AgentRunSnapshot` — JSON 字符串 → 快照
- [ ] 处理 `dataclass` 嵌套序列化（尤其是 `dict[str, int]` 等类型）
- [ ] JSON 输出格式化（indent=2），人类可读

### Step 5.2 — 文件管理

- [ ] `save_snapshot(snapshot: AgentRunSnapshot, path: str)` — 保存到文件
- [ ] `load_snapshot(path: str) -> AgentRunSnapshot` — 从文件加载
- [ ] 文件名规范：`{run_id}.json`（如 `run_20260607_001.json`）
- [ ] 默认输出目录：`data/snapshots/`

### Step 5.3 — 配置指纹与 Git commit 采集

- [ ] `RunMeta` 中的 `git_commit` — 通过 `git rev-parse HEAD` 获取
- [ ] `config_hash` — 对 `config.yaml` + `model_router_config.yaml` 做 SHA256
- [ ] 这两个字段在构建 SnapshotBuilder 时自动填充（或由调用方传入）

### Step 5.4 — 编写存储测试

- [ ] 序列化→反序列化 往返一致性
- [ ] JSON 输出包含所有预期字段
- [ ] 文件保存和加载的正确性
- [ ] JSON 文件可直接用编辑器打开阅读

**验收标准**：
- 快照能完整序列化/反序列化，无字段丢失
- JSON 可读，缩进正确
- 文件命名规范，支持通过 run_id 查找

**预估文件**：
- 新增：`src/metrics/storage.py`
- 新增：`tests/metrics/test_storage.py`

---

## Phase 6：CLI 集成

**目标**：在 dotClaw 命令行中增加数据统计相关子命令。

**前置依赖**：Phase 0-5

### Step 6.1 — 命令设计

```bash
# 单次评测运行
dotclaw bench run --dataset <path> --output <snapshot_dir>

# 对比两次运行
dotclaw bench compare --baseline <snapshot_a.json> --candidate <snapshot_b.json>

# 查看快照摘要
dotclaw bench inspect <snapshot.json>
```

### Step 6.2 — 测试数据集格式

- [ ] 定义测试数据集 JSON 格式
  ```json
  {
    "name": "bench_v1",
    "description": "基础评测数据集",
    "tasks": [
      {"id": "task_001", "message": "帮我读取 config.yaml"},
      {"id": "task_002", "message": "列出所有 Python 文件"},
      ...
    ]
  }
  ```

### Step 6.3 — bench run 实现

- [ ] 加载测试数据集
- [ ] 创建 MetricsCollector 实例
- [ ] 对每个 task 调用 Agent，传入 collector
- [ ] 所有 task 完成后，调用 `collector.compute_snapshot()` 生成快照
- [ ] 保存快照到 `--output` 目录
- [ ] 在控制台打印关键指标摘要

### Step 6.4 — bench compare 实现

- [ ] 加载两份快照 JSON
- [ ] 调用 `compare_snapshots()` 生成 ComparisonReport
- [ ] 输出 Markdown 对比表格到控制台
- [ ] 可选：输出到文件（`--output report.md`）

**验收标准**：
- `dotclaw bench run --dataset data/test_bench.json` 生成快照文件
- `dotclaw bench compare` 输出正确的对比报告
- 命令行参数校验完善（文件不存在→报错）

**预估文件**：
- 新增：`src/channel/bench_commands.py`（或集成到现有 CLI）
- 新增：`data/test_bench.json`（测试数据集样本）

---

## Phase 7：全面测试与验收

**目标**：完成所有测试，确保模块质量达标。

**前置依赖**：Phase 0-6

### Step 7.1 — 单元测试补全

- [ ] 每个 `src/metrics/*.py` 对应的测试文件覆盖率 > 90%
- [ ] 关键边缘测试：空事件流、单事件、大数据量、异常数据格式

### Step 7.2 — 集成测试

- [ ] `test_integration.py`：完整的"运行→采集→快照→对比"链路
- [ ] 使用固定的 mock Agent（不调用真实 LLM），产出确定性事件流
- [ ] 验证快照字段值与预期一致

### Step 7.3 — 验收测试（对照需求文档 AC1-AC5）

| AC | 测试内容 |
|----|---------|
| AC1 | 完整事件类型采集 + 零侵入验证 + 容错 |
| AC2 | 五大类指标无遗漏 + 幂等性 |
| AC3 | 对比报告正确性 + 过滤小变化 |
| AC4 | JSON 可读 + 元信息完整 |
| AC5 | 端到端：优化前快照 A + 优化后快照 B + 对比报告 |

### Step 7.4 — 性能测试

- [ ] 1000 事件采集总耗时 < 100ms
- [ ] 1000 事件 → 快照构建耗时 < 50ms
- [ ] 序列化/反序列化耗时 < 20ms（正常大小快照）
- [ ] 内存占用 < 5MB（50 任务场景）

**验收标准**：
- 所有单元测试通过
- 集成测试通过
- 性能指标达标
- pytest 覆盖率 > 90%

**预估文件**：
- 修改：`tests/metrics/*.py`（补全）
- 新增：`tests/metrics/test_integration.py`

---

## 附录 A：错误处理矩阵

| 场景 | 处理方式 | 影响范围 |
|------|---------|---------|
| 采集器未初始化（None） | 跳过埋点 | 无影响 |
| `on_event()` 内部异常 | 静默丢弃事件，记录 debug log | 事件丢失 |
| 事件数据字段缺失 | 使用默认值填充（如 duration_ms=0） | 指标可能不准 |
| SnapshotBuilder 除零 | 返回 0.0 或空 dict | 快照中部分字段为 0 |
| GPT commit 获取失败 | git_commit 设为 "unknown" | 元信息不全 |
| JSON 反序列化字段不匹配 | 跳过未知字段，缺失字段用默认值 | 快照数据不完整 |
| 对比时 baseline 字段值为 0 | 跳过该字段的百分比计算 | diff 中该字段为 None |

## 附录 B：长期可维护性措施

| 措施 | 说明 |
|------|------|
| **接口版本化** | AgentEvent 和 AgentRunSnapshot 的字段变更通过新增 `version` 字段标识 |
| **Schema 校验** | 事件类型新增时在 `EventType` 常量中添加，builder 中映射表同步更新 |
| **向后兼容** | 旧版本快照 JSON 反序列化时，新增字段用默认值，不报错 |
| **模块独立** | metrics 模块不依赖 Agent 的具体实现，只依赖事件接口 |
| **测试优先** | 新增指标必须同时新增 golden test |
| **文档同步** | 指标变更时同步更新设计文档和需求文档 |

## 附录 C：风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| LLM 返回格式变化导致事件字段不匹配 | 事件采集失败 | 事件数据字段为 `dict[str, Any]`，不强制类型 |
| Agent 核心循环重构导致埋点位置失效 | 指标不准确 | 埋点位置通过 AgentContext 注入，不硬编码到方法内部 |
| 多模型供应商流式响应协议不同 | AgentGeneralMetrics 中 TTFT/TPS 可能不准确 | 客户端侧统一使用 `perf_counter()` 计时，不依赖 LLM 返回的 timing 字段 |
| 快照 JSON 体积随数据集增大而膨胀 | 存储压力 | 不存储原始事件流（events），只存储聚合后的指标 |
