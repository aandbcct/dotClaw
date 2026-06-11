# 数据统计模块 — 开发执行计划

> 项目：dotClaw Phase 11 - Agent 数据统计模块
> 创建日期：2026-06-07
> 状态：planning
> 参考文档：
> - 需求文档：data-statistics-requirements.md
> - 设计文档：data-statistics-design.md

---

## 目标

为 dotClaw Agent 框架构建零侵入的数据统计模块，运行时自动采集事件 → 生成指标快照 → 保存 JSON → 手动对比两份快照评估优化效果。

**不做的事：批量 benchmark CLI、测试数据集管理**——当前阶段只跑单次任务看指标，对比靠 JSON diff。

## 设计原则

| 原则 | 说明 |
|------|------|
| **零业务侵入** | 业务代码只调用 `emit_event()`，不写埋点逻辑 |
| **关注点分离** | 事件采集 / 指标计算 / 存储与对比 三层独立 |
| **可测试优先** | 每个组件纯函数或可 mock，核心逻辑可单测 |
| **渐进式接入** | 没有采集器时 Agent 正常运行，有时自动生效 |
| **接口稳定** | AgentEvent、RunMeta、MetricsCollector 设计好之后不轻易改 |

## 模块结构

```
src/metrics/                      # 新增
├── __init__.py                   # 模块入口
├── events.py                     # AgentEvent + 事件类型常量
├── collector.py                  # MetricsCollector
├── snapshot.py                   # 5 个 Metrics dataclass + AgentRunSnapshot + RunMeta
├── builder.py                    # SnapshotBuilder（事件 → 快照）
├── storage.py                    # JSON 序列化 + 文件读写 + 简单 diff 对比
tests/metrics/                    # 新增
├── test_snapshot.py
├── test_collector.py
├── test_builder.py
├── test_storage.py
└── test_integration.py
```

---

## Phase 0：模块骨架与核心数据类型

**目标**：创建模块目录，定义所有 dataclass。全部 `frozen=True`，无外部依赖。

**前置依赖**：无

### Step 0.1 — 创建模块目录

- [ ] 创建 `src/metrics/` 和 `tests/metrics/`
- [ ] `src/metrics/__init__.py` 导出公共接口

### Step 0.2 — 定义事件类型（events.py）

- [ ] `AgentEvent` dataclass：`timestamp`（float, ms）、`event_type`（str）、`data`（dict）
- [ ] 14 种事件类型字符串常量

### Step 0.3 — 定义快照类型（snapshot.py）

- [ ] `ReactLoopMetrics`（11 字段）
- [ ] `ToolCallMetrics`（9 字段）
- [ ] `SkillMetrics`（9 字段）
- [ ] `MemoryMetrics`（12 字段）
- [ ] `AgentGeneralMetrics`（11 字段）
- [ ] `AgentRunSnapshot`（6 元信息 + 5 指标对象）
- [ ] `RunMeta`（运行元信息：run_id、timestamp、git_commit、config_hash、test_dataset、test_dataset_size）
- 全部 `frozen=True`

### Step 0.4 — 编写类型级测试

- [ ] 每个 class 正常构造、frozen 约束生效、字段数与设计文档一致

**验收标准**：

- 导入链完整：`from src.metrics import AgentEvent, AgentRunSnapshot, ...`
- 所有 dataclass 实例化不报错
- `pytest tests/metrics/test_snapshot.py` 全通过

**预估文件**：

- 新增：`src/metrics/__init__.py`、`src/metrics/events.py`、`src/metrics/snapshot.py`
- 新增：`tests/metrics/test_snapshot.py`

---

## Phase 1：事件采集器

**目标**：实现 MetricsCollector，业务代码通过 `collector.on_event(event)` 发布事件。

**前置依赖**：Phase 0

### Step 1.1 — 实现 MetricsCollector（collector.py）

- [ ] 内部 `list[AgentEvent]` 事件流
- [ ] `on_event(event)` — 追加事件（<1ms），异常静默丢弃
- [ ] `clear()` / `event_count` / 可选 `is_active` 开关

### Step 1.2 — 编写测试

- [ ] 正常追加、10000 事件性能、异常数据不崩溃、`is_active=False` 跳过

**验收标准**：

- 10000 事件采集总耗时 < 100ms
- 采集器异常不传播到调用方

**预估文件**：

- 新增：`src/metrics/collector.py`、`tests/metrics/test_collector.py`

---

## Phase 2：快照构建器

**目标**：SnapshotBuilder 从事件流计算 AgentRunSnapshot。纯数据处理，无副作用。

**前置依赖**：Phase 0, Phase 1

### Step 2.1 — 核心接口（builder.py）

```python
class SnapshotBuilder:
    def __init__(self, run_meta: RunMeta, task_count: int): ...
    def process(self, event: AgentEvent) -> None: ...
    def build(self) -> AgentRunSnapshot: ...
```

### Step 2.2 — 指标计算逻辑（摘要，详见设计文档）

| Metrics 类 | 关键计算 |
|-----------|---------|
| ReactLoopMetrics | total_loops、avg_loops_per_task、task_completion_rate、empty_action_rate、冗余率、P95 耗时 |
| ToolCallMetrics | 按工具聚合：成功率、错误数、avg/p95 耗时、retry_rate |
| SkillMetrics | 触发率、缓存命中率、脚本成功率、token 开销 |
| MemoryMetrics | 命中率（非空=命中）、index_size/index_size_mb 暂返回 0 |
| AgentGeneralMetrics | total_input/output_tokens、avg_tokens_per_task、cost_usd 暂返回 0、TTFT/TPS 客户端计时 |

### Step 2.3 — 关键事件→指标映射

| 事件 | 贡献 |
|------|------|
| `session.start/end` | 任务计数、成败判定 |
| `react.loop_start/end/empty_action` | 循环次数、耗时、空转 |
| `tool.call_start/end` | 成功率、耗时、错误、retry |
| `skill.trigger/body_loaded/script_exec` | 触发率、token 开销 |
| `memory.retrieval/write` | 命中率、写入次数 |
| `llm.request_start/end` | token 用量、TTFT、TPS |

### Step 2.4 — 编写测试

- [ ] Golden Test（固定事件流 → 固定快照值）
- [ ] 空事件流 → 全 0/空
- [ ] 幂等性、除零边界、P95 正确性

**验收标准**：

- 快照字段无遗漏无多余，与设计文档严格一致
- 边界情况不崩溃，返回合理默认值

**预估文件**：

- 新增：`src/metrics/builder.py`、`tests/metrics/test_builder.py`

---

## Phase 3：业务代码接入（事件发射点）

**目标**：在 Agent/LLM/Tools/Skills/Memory 各模块插入 `emit_event()`，一行调用，不改变控制流。

**前置依赖**：Phase 0, Phase 1（Phase 2 可并行）

### Step 3.1 — LLM 层（proxy.py）

- [ ] `llm.request_start` / `llm.request_end`
- [ ] TTFT：`perf_counter()` 记录请求开始时间，首个 chunk 到达时算差值
- [ ] TPS：首 chunk → 末 chunk 时间差 / output_tokens

### Step 3.2 — ReAct 循环（loop.py）

- [ ] `react.loop_start` / `react.loop_end`（含 action 名称 + duration_ms）
- [ ] 检测 empty_action

### Step 3.3 — 工具执行（executor.py）

- [ ] `tool.call_start` / `tool.call_end`（含 success、duration_ms、error）

### Step 3.4 — Skill 系统

- [ ] `skill.trigger` / `skill.body_loaded` / `skill.script_exec`

### Step 3.5 — 记忆系统

- [ ] `memory.retrieval` / `memory.write`

### Step 3.6 — 会话层

- [ ] `session.start` / `session.end`

### Step 3.7 — 依赖注入

- [ ] `AgentContext` 新增 `metrics_collector: MetricsCollector | None` 字段
- [ ] None 时跳过埋点，有实例时自动生效
- [ ] 不修改任何业务方法签名

### Step 3.8 — 零侵入验证

- [ ] `metrics_collector=None` → Agent 行为不变
- [ ] 不引入 `if collector:` 之外的控制流分支

**验收标准**：

- 完整一次运行后 14 种事件类型全部采集到
- collector=None 时无差异
- 异常不影响发射链

**预估文件**：

- 修改：`src/agent/context.py`、`src/llm/proxy.py`、`src/agent/loop.py`
- 修改：`src/agent/tools/executor.py`、`src/skills/loader.py`、`src/memory/`

---

## Phase 4：存储与对比

**目标**：快照 JSON 序列化 → 文件读写 → 简易对比（两份快照 diff，不搞重型报告引擎）。

**前置依赖**：Phase 0, Phase 2

### Step 4.1 — JSON 序列化（storage.py）

- [ ] `snapshot_to_json(snapshot) -> str` — 快照 → 格式化 JSON（indent=2）
- [ ] `snapshot_from_json(json_str) -> AgentRunSnapshot`
- [ ] `save_snapshot(snapshot, path)` / `load_snapshot(path)`
- [ ] 文件名 `{run_id}.json`，默认输出 `data/snapshots/`

### Step 4.2 — 简易对比（storage.py）

```python
def diff_snapshots(
    baseline: AgentRunSnapshot,
    candidate: AgentRunSnapshot,
) -> list[str]:
    """对比两份快照，返回人类可读的差异行列表。

    对每个数值字段计算变化百分比，标注改善/退化。
    变化幅度 < 2% 的字段跳过。
    """
```

- [ ] 遍历快照中的标量字段（rate、count、duration 等），不递归展开 dict 嵌套字段
- [ ] `_is_improvement(name, pct)` 判定改善/退化
- [ ] 返回格式：`["task_completion_rate: 0.85 → 0.92 (+8.2%) ✅", "avg_e2e_latency_ms: 5000 → 8000 (+60.0%) ❌"]`
- [ ] 不构建 ComparisonReport dataclass——保留在设计中但本 Phase 不实现为独立类型

### Step 4.3 — 配置指纹

- [ ] `git_commit`：`git rev-parse HEAD`（失败时 "unknown"）
- [ ] `config_hash`：SHA256(config.yaml + model_router_config.yaml)
- [ ] RunMeta 由 SnapshotBuilder 自动填充或调用方传入

### Step 4.4 — 编写测试

- [ ] 序列化往返一致性、文件读写正确性
- [ ] diff_snapshots：全部相同→无输出、改善项正确标注、退化项正确标注、小变化被过滤

**验收标准**：

- JSON 可读，indent 正确
- 往返序列化无字段丢失
- diff 输出正确标注改善/退化

**预估文件**：

- 新增：`src/metrics/storage.py`、`tests/metrics/test_storage.py`

---

## Phase 5：测试与验收

**目标**：单元测试覆盖率 > 90%，集成测试通过，对照需求文档验收。

**前置依赖**：Phase 0-4

### Step 5.1 — 单元测试补全

- [ ] `tests/metrics/` 下所有 test_*.py 覆盖率 > 90%
- [ ] 边缘：空事件流、单事件、大数据量、异常数据格式、除零

### Step 5.2 — 集成测试

- [ ] mock Agent（不调用真实 LLM），产出确定性事件流
- [ ] 验证完整链路：run → collect → build → save → load → diff

### Step 5.3 — 验收测试（对照需求文档）

| AC | 测试内容 |
|----|---------|
| AC1 | 14 种事件类型完整采集 + 零侵入（collector=None） + 容错 |
| AC2 | 五大类指标无遗漏 + 幂等性（两次 build 结果相同） |
| AC3 | diff 函数标注正确 + 小变化（<2%）被过滤 |
| AC4 | JSON 可读 + 元信息完整（run_id、git_commit、config_hash） |
| AC5 | 端到端：手动跑两次，保存两份快照，diff 函数能反映预期变化 |

### Step 5.4 — 性能基准

- [ ] 1000 事件采集 < 100ms
- [ ] 1000 事件 → 快照构建 < 50ms
- [ ] 快照序列化/反序列化 < 20ms

**验收标准**：

- 所有单元测试通过
- 集成测试通过
- 性能指标达标
- pytest 覆盖率 > 90%

---

## 附录 A：错误处理矩阵

| 场景 | 处理方式 | 影响范围 |
|------|---------|---------|
| 采集器未初始化（None） | 跳过埋点 | 无影响 |
| `on_event()` 内部异常 | 静默丢弃，记录 debug log | 单事件丢失 |
| 事件字段缺失 | 默认值填充（duration_ms=0） | 指标可能偏低 |
| SnapshotBuilder 除零 | 返回 0.0 或空 dict | 指标为 0 |
| git commit 获取失败 | "unknown" | 元信息不全 |
| config_hash 计算失败 | "unknown" | 元信息不全 |
| JSON 反序列化字段不匹配 | 跳过未知字段，缺失字段默认值 | 快照不完整 |
| diff 时 baseline 字段为 0 | 跳过百分比计算，标注 "N/A" | 该字段不比较 |

## 附录 B：长期可维护性措施

| 措施 | 说明 |
|------|------|
| **接口版本化** | AgentEvent、AgentRunSnapshot 字段变更通过 version 字段标识 |
| **Schema 校验** | 事件类型新增时同步更新 EventType 常量和 builder 映射表 |
| **向后兼容** | 旧快照 JSON 反序列化时新增字段用默认值 |
| **模块独立** | metrics 不依赖 Agent 具体实现，只依赖事件接口 |
| **测试优先** | 新增指标必须同时新增 golden test |
| **文档同步** | 指标变更时同步更新设计文档和需求文档 |

## 附录 C：风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| Agent 核心循环重构导致埋点位置失效 | 指标不准 | 埋点通过 AgentContext 注入，不硬编码 |
| 多模型供应商流式协议不同 | TTFT/TPS 不准 | 客户端 `perf_counter()` 统一计时 |
| 快照 JSON 体积膨胀 | 存储压力 | 只存聚合指标，不存原始事件 |
