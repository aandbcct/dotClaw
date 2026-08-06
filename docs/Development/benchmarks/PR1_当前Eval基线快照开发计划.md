# dotClaw Benchmark PR1：当前 Eval 基线快照开发计划

> 状态：已确认的开发基线。本文定义 PR1 的唯一范围；实现期间发现 Runtime 事实不足时，先补失败测试并单独确认，不扩大本 PR。

## 1. PR 定位

### 1.1 唯一目标

基于 Git 跟踪的 Eval Dataset，重复执行当前隔离 Runtime，并输出可追溯的逐次记录、汇总报告和当前提交基线快照。

### 1.2 当前问题

- `EvalCase`、Dataset、Re-execution 和 `RunTrace` 已可证明单次任务是否按预期完成，但没有重复采样、性能统计或可提交的基线快照。
- 现有 `benchmarks/` 面向旧 Agent/Journal 指标；不能直接读取当前 `EvalResult` 与 `RunTrace`，也不能支撑后续版本的统一对照。
- 仓库没有 Git 跟踪的实际 Eval Dataset；默认 `data/datasets` 属于本地运行数据，不能作为提交间稳定实验输入。

### 1.3 完成后的链路

```text
benchmarks/datasets/runtime_core_v1/cases/*.json
    → ReexecutionRunner
    → EvalResult + RunTrace
    → BenchmarkSample（单次采样记录）JSONL
    → BenchmarkSnapshot（当前基线快照）JSON + Markdown 报告
```

PR1 只生成当前版本快照。PR2 才在独立 Git worktree 启动历史版本，并转换其历史记录为同一快照口径。

## 2. PR 边界

### 2.1 包含内容

1. 建立 `runtime_core_v1` 的 Git 跟踪 Dataset，首批四个 Case 复用现有 Eval 测试中已验证的 Fixture 语义，经人工审核后固化为工具成功、审批通过恢复、审批拒绝、上下文保持四个确定性 JSON Case。
2. 从现有 `ReexecutionRunner` 的结果中提取单次语义、Trace 指标、调用统计和环境元数据；预热结果不写入正式统计。
3. 对固定 Dataset 执行 warmup 与重复采样，写出 JSONL 原始记录、JSON 基线快照和 Markdown 汇总报告。
4. 提供只面向 PR1 的 CLI，显式指定 Dataset、warmup、repeat、输出目录和基线保存目录。
5. 为 Dataset、记录序列化、统计、报告和 CLI 参数建立测试。

### 2.2 明确不包含

- 历史 worktree、历史 Journal 适配、当前/历史百分比比较；
- 并发 Session、子进程崩溃、故障注入、恢复率实验；
- 委派提交、父 Run 挂起、结果回灌与取消传播的端到端实验；该完整控制流由 PR7 负责；
- 真实 LLM、网络、外部 Tool 或 `[EXT]` 性能结论；
- Runtime、状态机、Trace、Eval schema 或生产持久化改造；
- 旧 Agent/Journal 微基准的删除或迁移；
- pytest-cov、测试类型 marker 和覆盖率门槛。

## 3. 模块结构

### 3.1 新增文件

```text
benchmarks/
├── eval_baseline.py                 # Eval Dataset 的当前基线 CLI 和编排
├── eval_baseline_models.py          # BenchmarkSample（单次采样记录）与 BenchmarkSnapshot（汇总快照）
├── eval_baseline_stats.py           # 分位数、成功率和按 Case 聚合
├── datasets/runtime_core_v1/cases/  # 四个 Git 跟踪的 EvalCase JSON
└── reports/                         # 保持 gitignore，仅写运行工件

tests/benchmarks/
├── test_eval_baseline_models.py
├── test_eval_baseline_stats.py
└── test_eval_baseline_runner.py
```

### 3.2 修改文件

```text
benchmarks/README.md                 # 保留旧微基准说明，新增 PR1 Eval 基线入口与结果边界
src/dotclaw/eval/reexecution.py      # 增加按单 Case 执行的最小入口，批量入口复用它
tests/eval/test_reexecution.py       # 覆盖单 Case 与批量入口的一致性
```

### 3.3 不新增或修改的内容

- 不在 `src/dotclaw/runtime/`、`src/dotclaw/trace/` 中加入 Benchmark 类型或字段；
- 不在 `src/dotclaw/eval/` 新建平行执行器；仅扩展既有 `ReexecutionRunner`，不改变其隔离语义；
- 不修改 `data/datasets`，也不把基准 Case 写入用户运行数据。

### 3.4 `runtime_core_v1` Case 清单

| case_id | 复用的已验证语义 | 最小断言 | PR1 提供的 Trace 证据 |
|---|---|---|---|
| `tool_success` | `tool_case()` 的 LLM → `search` → 最终回答 | completed、工具序列、关键参数、最终文本 | LLM 与 Tool Span、调用数、关键路径 |
| `approval_resume` | `approval_resolved_case(approved=True)` 的等待审批后重放工具 | completed、审批 approved、最终文本 | Approval waiting/completed、Tool waiting/completed |
| `approval_rejected` | `approval_resolved_case(approved=False)` 的拒绝收口 | cancelled、审批 rejected、工具不执行 | Approval cancelled、Tool waiting |
| `context_retention` | `tool_case()` 中冻结系统文本与 ContextVersion 评分 | completed、指定 ContextVersion 保留关键文本 | ContextVersion 引用、内容保留断言 |

委派 Fixture 只验证端口匹配；当前没有可直接复用的 Eval 端到端委派 Case。委派成功、失败、取消和回灌在 PR7 从既有 Runtime v2 委派链路建立专用实验，不伪装为 PR1 的 Dataset 覆盖。

## 4. 接口设计

### 4.1 编排入口

`EvalBaselineRunner`（当前 Eval 基线编排器）位于 `benchmarks/eval_baseline.py`，只由 CLI 和测试调用。

```python
async def run_dataset(
    dataset_root: Path,
    dataset_name: str,
    *,
    warmup: int,
    repeat: int,
    output_dir: Path,
    baseline_dir: Path | None,
) -> BenchmarkSnapshot:
    ...
```

- 输入为现有 Dataset 根目录与名称；PR1 固定使用 `runtime_core_v1`，但 CLI 可显式覆盖名称。
- 每轮按稳定 Case 顺序调用 `ReexecutionRunner.run_case()`；每条返回的 `EvalResult` 转为一个 `BenchmarkSample`。
- `warmup` 必须大于等于 0，`repeat` 必须大于 0；Dataset 为空、结果数量与 Case 数不一致、无 Trace 的可信结果均视为实验错误，不生成可用基线。
- 该入口只写 `output_dir` 与可选 `baseline_dir`，不写 Dataset、Session、生产目录或 Runtime 事实。

### 4.2 Re-execution 单 Case 入口

在既有 `ReexecutionRunner` 上增加：

```python
async def run_case(self, case: EvalCase) -> EvalResult:
    ...
```

- 方法覆写 Case 为既有的 `ExecutionMode.REEXECUTION`，然后调用现有 `EvalRunner`；不改变 Fixture、依赖注入、隔离 Repository 或真实依赖回退的既有语义。
- `run_dataset()` 改为加载 Case 后逐个调用 `run_case()`，保留当前稳定顺序和单条错误转换行为。
- `EvalBaselineRunner` 在每次 `run_case()` 调用外以 `perf_counter()` 记录 `wall_duration_ms`，不由 Eval 层承担 Benchmark 计时。

### 4.3 CLI

```text
python -m benchmarks.eval_baseline \
  --dataset-root benchmarks/datasets \
  --dataset runtime_core_v1 \
  --warmup 5 --repeat 30 \
  --output benchmarks/reports/<run-id> \
  --save-baseline benchmarks/baselines/runtime_core_v1
```

CLI 默认只使用隔离 Fixture；若未来加入真实依赖，必须使用单独命令和 `[EXT]` 标识，不扩展 PR1 参数。

## 5. 数据模型设计

### 5.1 BenchmarkSample（单次采样记录）

派生测试记录，不是 Runtime 权威事实，也不新增持久化容器。

| 字段组 | 内容 |
|---|---|
| 身份 | schema_version、suite、dataset、case_id、attempt、is_warmup |
| 版本环境 | git_commit、Python、平台、配置哈希、Eval schema 版本 |
| 语义 | passed、failure_kind、断言通过/总数、trace_available |
| 时延 | `wall_duration_ms`（Benchmark 外层真实耗时）与 Trace 的 LLM/Tool/Approval/关键路径耗时 |
| Run 统计 | duration、LLM/Tool 调用数、输入/输出 token（权威事实缺失时明确为 null） |
| 证据 | run_id、Trace source 元数据；不内联正文或敏感内容 |

单次记录按 JSONL 追加；Warmup 与正式采样均写出，并以 `is_warmup` 区分。Warmup 只保留为冷启动诊断证据，不得进入 `BenchmarkSnapshot` 的正式统计。

### 5.2 BenchmarkSnapshot（汇总快照）

派生对照工件，由同一次 Dataset、提交、环境和采样配置下的非 warmup `BenchmarkSample` 聚合而成。

- 全局元数据：schema、生成时间、提交、Dataset、环境、warmup、repeat；
- 每 Case 汇总：样本数、通过/失败数、成功率、P50/P95/P99、调用数和 Trace 健康指标；
- 全局汇总：所有 Case 的同口径聚合与失败归因计数；
- 原始证据路径：JSONL 文件相对路径与内容摘要。

汇总时仅选择 `is_warmup=false` 的记录；若 JSONL 中缺少正式采样，快照生成必须失败。

快照不是 `EvalResult`、`RegressionReport` 或 Runtime 事实的替代品；它不进入 CI Gate。

### 5.3 基线目录布局（已确认）

```text
benchmarks/baselines/runtime_core_v1/
├── <snapshot-id>.json               # 可提交的 BenchmarkSnapshot
└── samples/
    └── <snapshot-id>.jsonl           # 该快照对应的 BenchmarkSample 原始记录
```

一个 `<snapshot-id>` 只对应一次完整基准运行。快照只保存相对于自身目录的原始记录路径和内容摘要；新的运行必须创建新的 `<snapshot-id>.json` 与 `samples/<snapshot-id>.jsonl`，既有快照及原始记录不得覆盖。

`<snapshot-id>` 固定为 `YYYYMMDDTHHMMSSZ_<short-git-commit>`，时间使用 UTC、提交号使用当前 HEAD 的短哈希，例如 `20260806T091530Z_b6426cc`。目标快照或采样文件任一已存在时命令失败，不自动改名或覆盖。

## 6. 行为与一致性边界

- PR1 的“通过率”是隔离 Fixture 下的 Eval 语义通过率，证明当前 Runtime 对固定任务的确定性行为；不等同于真实模型线上成功率。
- `wall_duration_ms` 是跨提交性能比较的端到端口径；Trace 关键路径用于解释 Runtime 内部耗时构成，两者均报告且不得互相替代。
- P50/P95/P99 只在同机、同 Python、同 Dataset、同配置、同 repeat 下可用于后续提交的趋势比较。
- Trace 指标来自当前 Eval 隔离 Run。若一条可信结果未携带 Trace，说明 Eval 契约被破坏，整次实验报告为错误；断言失败但 Trace 完整仍是有效样本。
- `RunStatistics` 中未由当前 Fixture 产生的 token 或时延不得猜测为 0，序列化为 `null` 并在报告说明。
- 非提交的运行输出只进入 `benchmarks/reports/`；提交的基线快照写入 `benchmarks/baselines/<dataset>/`，单次 JSONL 直接写入其 `samples/<snapshot-id>.jsonl`。

## 7. 必要的现有代码修改

只修改 `ReexecutionRunner`：增加 `run_case()` 并让 `run_dataset()` 复用它。该改动是 Benchmark 对每个 Case 计量真实端到端耗时的必要条件，避免 Benchmark 复制 Eval 的 REEXECUTION 覆写和错误分类逻辑。

不修改 `EvalRunner`、Fixture 环境、Trace 或 Runtime；`wall_duration_ms` 由 Benchmark 外层计时，不写入 EvalResult。

## 8. 测试计划

### 8.1 正常路径

- 四个 Case 均能被 `runtime_core_v1` Dataset 稳定加载并重复执行；
- 单 Case 入口与批量入口在相同 Case 下产生等价的 EvalResult；
- 非 warmup 每次执行产生一条可反序列化的记录；
- 汇总快照正确聚合成功率、分位数、调用数和 Trace 指标；
- CLI 写出 JSONL、JSON 快照和 Markdown 报告。

### 8.2 边界路径

- `warmup=0` 可用；`repeat=0`、负数和未知 Dataset 被明确拒绝；
- 空 Dataset、结果数不匹配、无 Trace 的可信结果产生实验错误，不生成成功基线；
- 全部断言失败但 Trace 完整时，仍生成快照并正确统计失败归因；
- 同名基线已存在时拒绝覆盖。

### 8.3 数据损坏

- Dataset Case schema 不兼容、JSON 损坏或非法路径由现有 Eval loader 明确失败；
- JSONL/快照写出后可重新读取，未知 schema 或字段类型错误明确失败；
- 报告引用的原始证据路径不得越出输出目录。

### 8.4 历史兼容

PR1 不读取历史 Runtime 记录；仅验证历史旧微基准目录和已有报告不被修改。

### 8.5 回归测试

- `tests/eval` 全部通过，特别是 Dataset、Re-execution、Trace 评分相关测试；
- 当前 872 项默认 pytest 保持通过；
- `compileall` 与 `git diff --check` 通过。

## 9. 实施顺序

1. 从现有 Eval 测试中提取已验证的工具成功、审批通过、审批拒绝和上下文保持 Fixture 语义，人工审核并固化为 Dataset 的四个最小 Case，再用 Eval Dataset 加载测试冻结其格式。
2. 扩展 `ReexecutionRunner.run_case()`，以测试证明它与既有批量入口等价。
3. 新增 `BenchmarkSample`、`BenchmarkSnapshot` 和严格序列化/反序列化测试。
4. 实现统计与报告纯函数，使用构造样本覆盖分位数、失败、缺失指标和禁止覆盖。
5. 实现 `EvalBaselineRunner` 与 CLI，围绕 `run_case()` 计时并写出工件，再补端到端测试。
6. 更新 Benchmark README，运行 PR1 命令生成第一份当前提交基线，并执行完整验证。

## 10. PR 验收标准

1. `benchmarks/datasets/runtime_core_v1/` 有四个可加载、Git 跟踪的 EvalCase；
2. 同一 Dataset 可经 PR1 重复执行并产出逐次 JSONL、汇总 JSON 和 Markdown；
3. 快照记录提交、环境、Dataset、采样配置、每 Case 语义结果与 Trace 指标；
4. Warmup 不进入正式统计；失败与缺失数据不被静默当作成功或 0；
5. 输出不改变 Runtime、Eval Dataset 用户目录、生产 Session 或 CI Gate；
6. 新增 Benchmark 测试和现有 Eval/全量测试通过。

## 11. 最终交付结果

PR1 完成后可执行：

```text
python -m benchmarks.eval_baseline --dataset runtime_core_v1 --warmup 5 --repeat 30
```

并得到当前提交对固定 Eval Dataset 的基线快照。它尚不能运行历史 worktree、计算当前/历史提升百分比、测试并发隔离或注入崩溃。
