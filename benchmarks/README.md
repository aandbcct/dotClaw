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

## 历史基线可复跑与对照（PR2）

在独立 Git worktree 中审计并驱动旧执行链路，以与 PR1 `tool_success` 相同的业务
语义生成历史快照，仅对可比指标输出当前/历史对照。

```text
候选历史提交 → 独立 worktree + 该提交声明的依赖环境 → 历史单工具场景外围启动
    → 历史 AgentRun 终态/统计 + 记录型替身工具日志 → PR1 BenchmarkSample
    → 历史 BenchmarkSnapshot + 当前/历史对照报告
```

### 审计命令

候选提交必须显式传入，不隐式扫描历史：

```bash
python -m benchmarks.historical_baseline audit \
  --candidate 4e4cdd3 \
  --dataset runtime_core_v1 --case tool_success \
  --warmup 1 --repeat 10 \
  --output benchmarks/reports/historical-audits
```

审计顺序固定为六道门：解析完整提交号 → 创建 detached worktree → 独立解释器
环境（记录 Python 与依赖证据）→ 子进程显式从历史 `src` 导入 → 固定场景执行与
校验（终态、工具名、参数、调用次数、最终回答）→ 映射统一记录并连续开发期采样。
任一门失败时记录候选、失败门、异常摘要和证据路径，不产出历史快照或对照百分比。

### 生成历史基线并对照

```bash
# 1. 审计通过后，对同一完整提交执行正式采样（warmup=5, repeat=30）
python -m benchmarks.historical_baseline run \
  --candidate <audit.json 中的完整提交> \
  --audit-output benchmarks/reports/historical-audits/<audit-id> \
  --save-baseline benchmarks/baselines/runtime_core_v1

# 2. 当前与历史两份快照对照
python -m benchmarks.historical_baseline compare \
  --current benchmarks/baselines/runtime_core_v1/<current-snapshot-id>.json \
  --historical benchmarks/baselines/runtime_core_v1/<historical-snapshot-id>.json \
  --output benchmarks/reports/historical-audits/<audit-id>/comparison.md
```

`run` 只接受通过同一审计输出确认的完整提交号（不以短哈希代替）。

### 对照口径与结论边界

- 仅在相同 Dataset、共享场景、正式 repeat、warmup、机器标识、Python 主/次版本和
  固定替身配置时计算变化率；任一条件不一致或场景标识与 Case 列表不符（疑似篡改）
  时拒绝百分比；
- 两侧必须记录**一致且非空**的固定夹具指纹（`fixture_fingerprint`，由 Git 跟踪的
  Case 固定夹具派生），比较器只比较该指纹而非各版本自己的完整配置哈希；指纹
  缺失或不同即拒绝输出百分比——这是当前/历史对照的严格可比性门槛；
- 历史链路没有的 Trace / token / 内部阶段时延序列化为 `null`，不参与聚合与变化率；
- 历史值为 0 或缺失时仅列原值与不可比原因，不猜测为 0；
- 成功率报告成功数/总数、Wilson 95% 区间和绝对错误数；变化率为
  `(current - historical) / historical`；
- PR2 只证明旧/新单工具执行主链的可比业务结果与编排成本；审批恢复、并发隔离、
  操作节点恢复、Capability 安全、ContextVersion 与多 Agent 委派由后续 PR 以专用
  实验建立。

### 当前历史对照结果（commit `4e4cdd3`，AgentLoop 时代）

- 候选审计：`4e4cdd3` 六道审计门全部通过（20260806T065307Z 审计报告）；
- 固定夹具指纹：`tool_success` 两侧一致 `e3e3e26ced9cd716`；
- 历史正式采样：warmup=5, repeat=30，**30/30 通过（100%）**，Wall P50 **41.4 ms**；
- 对照结论（共享场景 `tool_success`）：成功率与 LLM（2 轮）/Tool（1 次）调用均值
  两侧完全一致（语义等价），当前端到端 Wall P50 2.4 ms，历史 41.4 ms，
  **耗时下降 -94.21%**（P95 -93.39%、P99 -93.30%）；
- 证据：`benchmarks/reports/historical-audits/4e4cdd3-20260806T065307Z/` 审计报告，
  `benchmarks/baselines/runtime_core_v1/` 下当前与历史快照 JSON + JSONL。

## 并发隔离与调度收益（PR3）

以固定的并发工作负载量化当前 Session 级串行、跨 Session 并行、状态隔离与取消
不阻塞行为，并在相同 Runtime 下对照 Benchmark 内部全局串行调度的成本。

```text
固定并发场景 + 固定延迟 Fake LLM / Fake Tool
    → SessionInteractionService
    → SessionRunCoordinator → RuntimeEngine
    → 持久化事实读取 → BenchmarkSample（JSONL）
    → ConcurrencySnapshot（JSON + Markdown 报告）
```

### 入口命令

```bash
# 开发期快速验证（核心与扩展各 1 轮）
python -m benchmarks.concurrency_reliability \
  --core-warmup 0 --core-repeat 1 \
  --scaling-warmup 0 --scaling-repeat 1 \
  --fake-delay-ms 20

# 正式实验（核心正确性 5+50；扩展调度 5+30）
python -m benchmarks.concurrency_reliability \
  --suite reliability_concurrency_v1 \
  --core-warmup 5 --core-repeat 50 \
  --scaling-warmup 5 --scaling-repeat 30 \
  --fake-delay-ms 20 \
  --output benchmarks/reports/concurrency/<run-id> \
  --save-baseline benchmarks/baselines/reliability_concurrency_v1
```

参数：`--suite`（实验族，默认 `reliability_concurrency_v1`）、
`--core-warmup` / `--core-repeat`（FIFO、隔离、取消，默认 5 / 50）、
`--scaling-warmup` / `--scaling-repeat`（扩展、固定并发、长短混合，默认 5 / 30）、
`--fake-delay-ms`（固定延迟毫秒，默认 20）、
`--output`（工件输出目录）、`--save-baseline`（可选基线目录）。

### 覆盖场景

| 场景 | 说明 | 核心指标 |
|------|------|---------|
| 同 Session FIFO | 1 Session × 20 请求，验证开始/完成/Conversation 顺序 | 乱序/重复/遗漏 = 0/N |
| 多 Session 隔离 | 8 Session × 4 请求，验证跨 Session 消息/事件/上下文/工具/输出零串扰 | 串扰 = 0/N |
| Session 数扩展 | 1/2/4/8 Session × 4 请求，绘制吞吐随 Session 数变化 | 吞吐(req/s) |
| 固定并发对照 | 8×4 请求，Session 锁 vs 全局锁主对照 | 吞吐变化率 |
| 取消不阻塞 | 1 长 Run + 后续请求，验证取消送达/生效/锁释放 | 送达/生效 P50/P95 |

### 正式基线（20260807T030719Z_24d6b1f，commit `24d6b1f`）

- 复现命令：

  ```bash
  python -m benchmarks.concurrency_reliability \
    --suite reliability_concurrency_v1 \
    --core-warmup 5 --core-repeat 50 \
    --scaling-warmup 5 --scaling-repeat 30 \
    --fake-delay-ms 20 \
    --output benchmarks/reports/concurrency/pr3-formal-20260807-rerun \
    --save-baseline benchmarks/baselines/reliability_concurrency_v1
  ```

- 核心正确性：同 Session FIFO **1,000/1,000** 请求开始、完成和 Conversation 顺序一致；
  多 Session 隔离 **1,600/1,600** 请求通过，消息、事件、ContextVersion、持久化工具记录和
  输出串流泄漏均为 **0**；取消 **50/50** 送达、生效、锁释放和后续请求可用。
- 取消时延：送达 P50/P95 **1.0 / 2.1 ms**，生效 P50/P95 **118.7 / 126.7 ms**。
- 8×4 固定并发对照：Session 锁吞吐 **54.2 req/s**，全局锁 **9.7 req/s**；相对 Benchmark
  全局串行吞吐 **+456.99%**，排队 P95 **-83.71%**，端到端 P95 **-81.45%**。
- 原始证据：基线快照与 JSONL 位于 `benchmarks/baselines/reliability_concurrency_v1/`；
  同次 `correctness.md`、`scheduling-comparison.md` 和 `workload-config.json` 位于
  `benchmarks/reports/concurrency/pr3-formal-20260807-rerun/`。

### 口径与边界

- FIFO 结论只针对同进程、单 `SessionRunCoordinator` 实例内的同 Session 提交；
- 隔离判据以持久化运行事实中的标识回显为准，Fake LLM/Tool 只消除外部不确定性；
- 全局锁对照仅证明调度结构容量影响，不等同于真实 API 端到端加速；
- 取消结论仅证明 Runtime 内取消信号、终态收口与租约释放；不涉及外部副作用停止；
- 不修改 `SessionRunCoordinator`、`RuntimeEngine`、`SessionInteractionService`
  的锁、排队或取消生产语义。

### 当前基线（20260806T065437Z_cd5a1cc，commit `cd5a1cc`）

- 环境：Python 3.13.5 / Windows-11 / config 哈希 `b9bea591d3252a9a`；
- 采样：warmup=5, repeat=30，共 120 个正式样本，**120/120 通过（100%）**；
- 全局耗时：Wall P50 **2.3 ms**、P95 **4.4 ms**；
- 调用统计：LLM 210 次、Tool 150 次，Trace 完整 120/120；
- 各 Case（30 样本）成功率均为 100%：`approval_rejected` P50 0.68 ms、
  `approval_resume` P50 1.04 ms、`context_retention` P50 0.80 ms、
  `tool_success` P50 2.40 ms；
- 原始证据：`benchmarks/baselines/runtime_core_v1/` 下快照 JSON 与 140 行 JSONL
  （含 warmup 诊断记录）；样本带 `execution_source` / `source_commit` /
  `scenario_id` / `evidence_kind` / `fixture_fingerprint` 来源元数据。

## PR4：操作节点故障注入与恢复

PR4 使用隔离存储根、记录型 LLM（大语言模型替身）和工具替身，在故障后销毁旧服务对象并从同一根目录冷重建。它分别报告控制状态、内部持久化事实和外部副作用；后两者不得互相改写结论。

```powershell
python -m benchmarks.recovery_reliability `
  --warmup 5 --repeat 30 `
  --process-warmup 5 --process-repeat 50 `
  --output benchmarks/reports/recovery/<run-id> `
  --save-baseline benchmarks/baselines/reliability_recovery_v1
```

- `llm_response_unknown` 只说明控制恢复是否正确；外部 LLM 请求可能重复，绝不表示 exactly-once。
- `tool_after_effect` 会记录可观察重复副作用，但 ToolResult（工具结果）、完成事件和 Conversation（会话投影）重复属于内部事实失败。
- 成功提交的六个边界逐点报告；工具前 checkpoint（检查点）另有子进程强制退出验证。
- 委派等待冷重建是当前能力边界，不进入恢复成功率；PR4 不证明跨版本、分布式或真实 API 的 exactly-once。
- 工件按统一布局写出 `<snapshot-id>.json` 和 `samples/<snapshot-id>.jsonl`；保存基线时二者会一并复制。

### 正式基线结果（20260807T092016Z_eb67d30）

本基线在提交 `eb67d30`、Python 3.13.5、Windows 11 上执行。快照只统计
`capability_status=FORMAL` 的非 warmup 样本；委派冷重建能力边界仅保留在原始
JSONL 和能力边界报告，不进入恢复成功率。

| 正式范围 | 控制状态恢复 | 内部事实一致性 | 恢复耗时 P50 / P95 |
|---|---:|---:|---:|
| LLM 前失败 | 30 / 30 | 30 / 30 | 51.95 / 62.03 ms |
| LLM 响应未知 | 30 / 30 | 30 / 30 | 57.91 / 93.55 ms |
| 工具副作用前中断 | 30 / 30 | 30 / 30 | 70.62 / 80.94 ms |
| 工具副作用后中断 | 30 / 30 | 30 / 30 | 69.85 / 105.18 ms |
| 审批冷重建 | 30 / 30 | 30 / 30 | 76.22 / 109.41 ms |
| 成功提交 6 个边界 | 180 / 180 | 180 / 180 | 9.40–14.97 / 10.66–18.73 ms |
| 工具前子进程强退 | 50 / 50 | 50 / 50 | 66.15 / 79.14 ms |

- 正式快照共 **380 / 380** 通过；对应 JSON、JSONL 和报告引用见
  `benchmarks/baselines/reliability_recovery_v1/20260807T092016Z_eb67d30.md`。
- 工具副作用后中断在记录型替身中观测到 **30 / 30** 重复；LLM 响应未知为
  **30 / 30** 未知结果。因此上述结论不承诺外部调用 exactly-once。


## PR5：Capability 安全链实验

PR5 使用 Git 跟踪的完整有限安全决策矩阵，验证现有工具链的参数校验、资源解析、策略收敛、审批、Handler 屏障、Agent 策略隔离、路径回填与摘要脱敏；所有 Handler 均为无副作用记录型替身，不执行真实文件、进程、网络或 MCP 调用。

```powershell
python -m benchmarks.capability_reliability `
  --suite reliability_capability_v1 `
  --matrix benchmarks/datasets/reliability_capability_v1/matrix.json `
  --performance-warmup 5 --performance-repeat 50 `
  --output benchmarks/reports/capability/<run-id> `
  --save-baseline benchmarks/baselines/reliability_capability_v1
```

- 矩阵每个适用 Case 只执行一次；Windows 联接点无法由当前用户建立时记录为环境跳过，不计为安全通过；
- `security-matrix.md` 报告适用/通过/失败/跳过、三类阻断分支的 Handler 进入次数和测试敏感标记泄露数；路径对齐不表示已解决 TOCTOU；
- `security-chain-overhead.md` 只比较相同 Handler、已验证参数和执行上下文下的直接 Handler 与完整链抵达 Handler entry 的 P50/P95；预热不进入统计；
- 进程结论仅为 `process.exec` 档案级策略，不表达命令文本内容级治理。

### 正式基线（20260807T113238Z_96940b5）

- 环境：Windows、Python 3.13.5；矩阵 SHA-256 配置摘要为 `7e10f439fc1204ce`，原始 JSONL 与快照位于 `benchmarks/baselines/reliability_capability_v1/`；
- 安全正确性：27 个适用 Case **27/27** 策略判定符合预期；参数校验失败、Policy deny 与未获审批调用进入 Handler 的次数均为 **0**；敏感测试标记泄露为 **0**；Windows Junction 逃逸用例实际执行；
- 前置开销：同一已验证输入与记录型 Handler 下，直接 Handler P50/P95 为 **0.0026 / 0.0048 ms**；完整安全链 P50/P95 为 **2.0568 / 2.5199 ms**；安全链额外 P50/P95 为 **2.0542 / 2.5151 ms**（warmup=5、每种模式 50 个正式样本）；
- 边界：结果仅代表本机、无副作用替身和固定有限矩阵；不证明命令内容级治理、TOCTOU 防护、真实外部 Tool 安全性或真实网络/API 性能。

## PR6：ContextVersion 与 Session 历史压缩

PR6 以固定 Slot、固定历史语料和记录型外部 Provider 验证已实现的版本化上下文回放、
Session 历史压缩预算与 GLOBAL/AGENT/SESSION/RUN 内容隔离。强制重建只在 Benchmark
对照控制中存在，不会修改生产 `ContextProvider`（上下文提供者）或 Runtime（运行时）。

```powershell
python -m benchmarks.context_reliability `
  --suite reliability_context_v1 `
  --compression-tokenizer cl100k_base `
  --recovery-warmup 5 --recovery-repeat 30 `
  --performance-warmup 5 --performance-repeat 30 `
  --output benchmarks/reports/context/<run-id> `
  --save-baseline benchmarks/baselines/reliability_context_v1
```

工件包括 JSONL、`context-config.json` 与 `consistency.md`、`recovery-replay.md`、
`compression.md`、`owner-isolation.md`。Token/预算仅针对固定语料与 tokenizer，
不评估摘要质量或真实模型效果；冷恢复不代表普通新 Run 忽略外部最新数据。

## 目录结构

```
benchmarks/
├── runner.py          # 旧 Agent/Journal 微基准评测入口（CLI）
├── stats.py           # 旧微基准公共工具（p50/p95/snapshot 转换）
├── eval_baseline.py   # PR1 Eval 基线 CLI 与编排（ReexecutionRunner + 计时采样）
├── eval_baseline_models.py   # PR1 BenchmarkSample / BenchmarkSnapshot 数据模型
├── eval_baseline_stats.py    # PR1 统计纯函数（分位数、成功率、聚合）
├── concurrency_reliability.py    # PR3 并发 CLI 与编排
├── concurrency_workloads.py      # PR3 固定工作负载与受控延迟替身
├── concurrency_assertions.py     # PR3 顺序/归属/隔离/取消断言
├── concurrency_stats.py          # PR3 吞吐/排队/端到端时延与对照聚合
├── context_reliability.py         # PR6 ContextVersion 实验 CLI 与工件写出
├── context_workloads.py           # PR6 固定 Slot / 语料 / Owner 场景
├── context_assertions.py          # PR6 结构、边界与可比性断言
├── context_controls.py            # PR6 仅 Benchmark 的强制重建对照
├── context_stats.py               # PR6 token、错误数与时延聚合
├── historical_baseline.py    # PR2 历史审计 / 运行 / 对照 CLI
├── historical_audit.py       # PR2 六道审计门与审计报告
├── historical_legacy_agent_v1.py   # PR2 旧 Agent v1（AgentLoop）单场景适配
├── historical_compare.py     # PR2 可比性检查与对照报告纯函数
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
│   ├── <snapshot-id>/         # PR1 非提交运行工件（JSONL / JSON / MD）
│   └── historical-audits/     # PR2 审计输出（audit.json / environment / worktrees）
└── baselines/         # 基线快照（git tracked，用于回归对比）
    ├── v1.0/                  # 旧微基准基线
    ├── runtime_core_v1/       # PR1/PR2 Eval 基线（当前 + 历史快照 + samples/）
    └── reliability_concurrency_v1/  # PR3 并发基线（JSON + samples/）
    └── reliability_capability_v1/   # PR5 安全矩阵（JSON + samples/）
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
