# dotClaw Benchmark 评测系统

## 是什么

一套自动化框架性能评测脚本，测量 dotClaw 各核心模块的耗时、吞吐量、稳定性。

与 `tests/`（功能正确性）互补——tests 回答"对不对"，benchmarks 回答"快不快"。

## Eval 基线快照（PR1）

基于 Git 跟踪的 Eval Dataset，重复执行当前隔离 Runtime，输出逐次 JSONL 原始记录、
JSON 基线快照与 Markdown 汇总报告。它回答"当前提交对固定 Eval 任务是否稳定完成、
耗时构成如何"，并作为后续版本对照的当前基线。

```text
benchmarks/datasets/runtime_core_v1/cases/*.json
    → ReexecutionRunner
    → EvalResult + RunTrace
    → BenchmarkSample（单次采样记录）JSONL
    → BenchmarkSnapshot（当前基线快照）JSON + Markdown 报告
```

### 入口命令

```bash
# 开发期快速验证（warmup=1, repeat=10 即可）
python -m benchmarks.eval_baseline --dataset runtime_core_v1 --warmup 1 --repeat 10

# 正式基线（warmup=5, repeat=30），可选 --save-baseline 提交基线目录
python -m benchmarks.eval_baseline \
  --dataset-root benchmarks/datasets \
  --dataset runtime_core_v1 \
  --warmup 5 --repeat 30 \
  --output benchmarks/reports/<run-id> \
  --save-baseline benchmarks/baselines/runtime_core_v1
```

参数：`--dataset-root`（默认 `benchmarks/datasets`）、`--dataset`（默认 `runtime_core_v1`）、
`--warmup`（默认 5，预热不进入正式统计）、`--repeat`（默认 30，必须 > 0）、
`--output`（非提交运行工件目录）、`--save-baseline`（可选提交基线目录）。

### 产物与布局

- 非提交运行输出写入 `--output`（默认 `benchmarks/reports/<snapshot-id>/`）：
  JSONL 原始记录、JSON 快照、Markdown 报告；
- 提交基线写入 `benchmarks/baselines/<dataset>/`：
  `<snapshot-id>.json` 与 `samples/<snapshot-id>.jsonl`；
- `<snapshot-id>` 固定为 `YYYYMMDDTHHMMSSZ_<short-git-commit>`（UTC），目标文件已
  存在时拒绝覆盖，新运行总是创建新快照。

### 口径与边界

- 通过率是隔离 Fixture 下的 Eval 语义通过率（`runtime_core_v1` 四个 Case），
  不等同于真实模型线上成功率；断言失败但 Trace 完整仍是有效样本；
- `wall_duration_ms` 是跨提交性能比较的端到端口径，Trace 关键路径用于解释内部
  耗时构成，两者分开报告且不可互相替代；
- P50/P95/P99 只在同机、同 Python、同 Dataset、同配置、同 repeat 下可比；
- Fixture 未产生的 token / 时延以 `null` 记录，不得猜测为 0；
- 快照不是 `EvalResult` / `RegressionReport` / Runtime 事实的替代品，不进入 CI Gate；
- 历史 worktree 对比、并发 / 故障注入由后续 PR 提供，PR1 不做。

### 当前基线（20260806T033707Z_7b1b093，commit `7b1b093`）

- 环境：Python 3.13.5 / Windows-11 / config 哈希 `b9bea591d3252a9a`；
- 采样：warmup=5, repeat=30，共 120 个正式样本，**120/120 通过（100%）**；
- 全局耗时：Wall P50 **0.83 ms**、P95 **1.64 ms**、P99 **2.08 ms**、Max **2.19 ms**；
- 调用统计：LLM 210 次、Tool 150 次，Trace 完整 120/120；
- 各 Case（30 样本）：`approval_rejected` P50 0.68 ms、`approval_resume` P50 1.06 ms、
  `context_retention` P50 0.80 ms、`tool_success` P50 0.82 ms，成功率均为 100%；
- 原始证据：`benchmarks/baselines/runtime_core_v1/` 下快照 JSON 与 140 行 JSONL
  （含 warmup 诊断记录）。

## 目录结构

```
benchmarks/
├── runner.py          # 旧 Agent/Journal 微基准评测入口（CLI）
├── stats.py           # 旧微基准公共工具（p50/p95/snapshot 转换）
├── eval_baseline.py   # PR1 Eval 基线 CLI 与编排（ReexecutionRunner + 计时采样）
├── eval_baseline_models.py   # PR1 BenchmarkSample / BenchmarkSnapshot 数据模型
├── eval_baseline_stats.py    # PR1 统计纯函数（分位数、成功率、聚合）
├── datasets/runtime_core_v1/cases/   # PR1 Git 跟踪的四个 Eval Case JSON
├── cases/             # 6 个旧微基准评测用例
│   ├── init_perf.py       # 初始化性能
│   ├── tool_dispatch.py   # 工具调度延迟
│   ├── llm_stream.py      # LLM 流式延迟
│   ├── memory_perf.py     # 记忆检索性能
│   ├── skill_load.py      # Skill 加载性能
│   └── stress.py          # 压力测试
├── dataset/           # 旧微基准测试数据集（自动生成）
│   ├── sample_skills/         # 100 个测试 Skill
│   ├── memory_corpus/         # 100 / 1000 / 10000 行语料
│   └── stress_prompts.json    # 压力测试用 prompts
├── reports/           # 报告输出（gitignore）
│   ├── benchmark_report_*.md
│   ├── snapshots/
│   └── <snapshot-id>/         # PR1 非提交运行工件（JSONL / JSON / MD）
└── baselines/         # 基线快照（git tracked，用于回归对比）
    ├── v1.0/                  # 旧微基准基线
    └── runtime_core_v1/       # PR1 Eval 基线（<snapshot-id>.json + samples/）
```

## 快速开始

### 1. 生成测试数据（只需一次）

```bash
python scripts/generate_benchmark_dataset.py
```

### 2. 跑一次评测

```bash
# 跑全部 case（默认 warmup=3, repeat=10）
python -m benchmarks.runner

# 只跑指定的 case
python -m benchmarks.runner --filter init_perf,tool_dispatch

# 调节参数
python -m benchmarks.runner --warmup 5 --repeat 30
```

### 3. 看结果

- **控制台**：直接输出各 case 的 P50/P95/Min/Max
- **Markdown 报告**：`benchmarks/reports/benchmark_report_*.md`
- **快照文件**：`benchmarks/reports/snapshots/*.json`

### 4. 独立运行单个 case（不需要 runner）

除了通过 runner 批量跑，也可以直接调用单个评测脚本。每个 case 的 `run()` 函数签名统一：

```python
async def run(
    warmup=3,
    repeat=10,
    project_root=None,   # 项目根目录，默认自动检测
    output_dir=None,     # 传入路径则自动写 AgentRunSnapshot
) -> tuple[Metrics, RunMeta]:
```

示例：

```python
import asyncio
from pathlib import Path
from benchmarks.cases.init_perf import run

# 只返回内存对象（不写文件）
metrics, meta = asyncio.run(run(warmup=2, repeat=5))
print(f"Agent Init P95: {metrics.agent_full_p95_ms:.1f} ms")

# 同时也输出 snapshot JSON
metrics, meta = asyncio.run(run(
    warmup=2, repeat=5,
    output_dir="benchmarks/reports/my_test"
))
```

各 case 返回的 `metrics` 类型：

| Case | 返回类型 |
|------|---------|
| `init_perf` | `InitPerfMetrics` |
| `tool_dispatch` | `ToolCallMetrics` |
| `llm_stream` | `AgentGeneralMetrics` |
| `memory_perf` | `dict[str, MemoryMetrics]`  (key: small/medium/large) |
| `skill_load` | `dict[int, SkillMetrics]`  (key: 10/50/100) |
| `stress` | `AgentRunSnapshot` |

## 建立基线 & 回归对比

### 建立基线

跑一轮"干净"的评测（确保没有其他程序抢资源），然后手动保存为基线：

```bash
# 跑一轮
python -m benchmarks.runner --warmup 5 --repeat 30

# 把刚跑的 snapshot 复制到 baselines/
# 报告里 Summary 表的数据就是你的基线值
```

然后把 `benchmarks/baselines/` 目录 commit 到 git，作为团队的基准参考。

### 回归对比

改完代码后，跟基线对比看有没有退化：

```bash
python -m benchmarks.runner --baseline benchmarks/baselines/<baseline_file>.json
```

输出会标注哪些指标改善了、哪些退化了。

## 6 个 Case 说明

| Case | 测什么 | 需要 LLM? | 核心指标 |
|------|--------|-----------|---------|
| `init_perf` | Config/LLM/Skill/Tool/Memory 等模块初始化耗时 | 不需要 | Agent Init P95 |
| `tool_dispatch` | ToolExecutor 从收到调用到执行 handler 的调度开销 | 不需要 | Dispatch P95 |
| `llm_stream` | LLM 流式 API 的 TTFT / TPS / E2E | 真实 API（默认 qwen3.7-max） | TTFT [EXT] |
| `memory_perf` | SQLite FTS5 在不同数据量下的检索延迟 | 不需要 | P95 Retrieval |
| `skill_load` | SkillScanner 扫描不同数量 Skill 的耗时 | 不需要 | Scan P50 |
| `stress` | 并发工具调用 / 大上下文 / 超长 ReAct 循环 | 不需要 | E2E P50 |

## 参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--filter` | 全部 | 指定要跑的 case，逗号分隔。如 `--filter init_perf,stress` |
| `--warmup` | 3 | 前 N 次迭代丢弃（消除冷启动偏差） |
| `--repeat` | 10 | 实际测量迭代次数。CI 建议 >= 30 |
| `--baseline` | 无 | 基线 snapshot 文件路径，用于回归对比 |
| `--output` | `benchmarks/reports` | 报告输出目录 |

> `llm_stream` 默认调用 `config.yaml` 中 `llm.default_model` 配置的模型（当前为 qwen3.7-max）。
> 如需切换模型，修改 `config.yaml` 中的 `default_model` 即可。

## 注意事项

### 跑 benchmark 时的环境
- **关掉其他重负载程序**（浏览器、IDE 索引、杀毒扫描），否则数据波动大
- **Windows 电源模式设为"高性能"**，避免 CPU 降频
- **同一台机器上对比才有意义**，不同机器的绝对值不可比

### 数据解读
- **P50（中位数）**：典型性能。比平均值更能代表"大多数情况"
- **P95**：最差 5% 的情况。反映稳定性
- **warmup 必须 >= 3**：Python import 缓存、文件系统缓存会影响第 1 次测量

### 限制
- `llm_stream` 默认调用真实 API（qwen3.7-max），**有费用**。不想花钱就 `--filter` 排除
- 当前默认模型 qwen3.7-max 是推理模型，TTFT 偏高（~6s）。建议用 fast 模型（如 deepseek-v4-flash ~0.8s）来测框架流式链路
- Windows 上 `time.time()` 精度 ~15ms，sub-ms 操作可能显示 0（不影响趋势）
- 报告中的 `[EXT]` 标记表示包含外部依赖延迟（网络/API），与框架内部延迟含义不同
