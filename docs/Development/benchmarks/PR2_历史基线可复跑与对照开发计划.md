# dotClaw Benchmark PR2：历史基线可复跑与对照开发计划

> 状态：已确认的开发基线。本文定义 PR2 的唯一范围；实现期间若没有候选提交通过审计，交付历史可复跑性审计报告，不产出优化百分比，也不扩大到 Runtime 修复。

## 1. PR 定位

### 1.1 唯一目标

在独立 Git worktree 中审计并驱动旧执行链路，以与 PR1 `tool_success` 相同的业务语义生成历史快照；仅对可比指标输出当前/历史对照。

### 1.2 当前问题

- PR1 只能从当前 `EvalResult` 和 `RunTrace` 产生当前版本快照，不能启动或读取历史 Git 提交。
- 历史版本没有 Eval Dataset 和统一 Trace。初步审计已确认，`e27f206` 与 `5cdb4ad` 的旧验收测试分别引用已删除的 `AgentLoop` 和已迁移的 `memory.store`；历史测试文件本身不能作为可复跑性的唯一证据。
- 以当前 `.venv` 导入历史 worktree 会误用当前源码或不匹配依赖，不能作为可信历史实验环境。

### 1.3 完成后的链路

```text
候选历史提交
    → 独立 worktree + 该提交声明的依赖环境
    → 历史单工具业务场景的外围启动适配
    → 历史最终结果 / Journal / 记录型替身日志
    → PR1 BenchmarkSample（单次采样记录）
    → 历史 BenchmarkSnapshot（汇总快照） + 当前/历史对照报告
```

PR2 不要求历史源码具有 Eval、Dataset、Trace 或 Benchmark；适配和统计均位于当前仓库的 `benchmarks/` 外围。

## 2. PR 边界

### 2.1 包含内容

1. 建立历史候选提交的可复跑性审计：逐项验证独立 worktree、历史解释器/依赖、旧入口导入、固定业务场景执行及结果映射。
2. 为首个通过审计的提交建立一个仅服务于旧执行链路的具体启动适配；以固定脚本 LLM 与记录型单工具替身执行 `tool_success` 的等价任务。
3. 将历史最终状态、循环/工具调用数、外围端到端耗时和可取得的 Journal 指标写入 PR1 的统一单次记录、历史快照与 JSONL 原始证据。
4. 在同机、同场景、同预热和重复次数条件下，生成当前/历史的成功率、错误数、端到端 P50/P95/P99、循环轮数与工具调用数对照报告。
5. 为候选审计、历史映射、不可比指标、拒绝生成百分比和报告证据路径提供测试。

### 2.2 明确不包含

- 不将 Eval、Trace、Dataset 或 Benchmark 迁移到历史源码，不修改历史提交；
- 不把历史字段兼容、Git 调用或 worktree 管理加入 Runtime、Eval 主流程或生产 CLI；
- 不比较审批恢复、并发隔离、操作节点恢复、Capability 安全、ContextVersion 或多 Agent 委派；这些在 PR3 至 PR7 以专用实验建立；
- 不自动扫描整个 Git 历史或以日期、`bdf0591`、旧测试通过数任一项直接选定基线；
- 不使用真实 API、网络或外部 Tool；不生成 `[EXT]` 结论；
- 不修复候选提交的历史缺陷、不过度兼容多个旧接口，也不删除旧微基准。

## 3. 模块结构

### 3.1 新增文件

```text
benchmarks/
├── historical_baseline.py           # 历史审计、执行与对照 CLI
├── historical_audit.py              # worktree/环境/入口/场景四道审计门
├── historical_legacy_agent_v1.py    # 单一旧 Agent v1 入口的具体外围适配
└── historical_compare.py            # 仅聚合可比指标的纯函数与 Markdown 报告

tests/benchmarks/
├── test_historical_audit.py
├── test_historical_legacy_agent_v1.py
└── test_historical_compare.py
```

运行工件不提交：

```text
benchmarks/reports/historical-audits/<audit-id>/
├── audit.json                        # 每个候选及每道审计门的结果
├── environment/                      # Python、依赖解析与源码路径证据
├── worktrees/<short-commit>/         # 临时独立历史 worktree
└── comparison.md                     # 仅在存在正式历史基线时生成
```

历史快照仍使用 PR1 已确认目录，不另建结果格式：

```text
benchmarks/baselines/runtime_core_v1/
├── <current-snapshot-id>.json
├── <historical-snapshot-id>.json
└── samples/
    ├── <current-snapshot-id>.jsonl
    └── <historical-snapshot-id>.jsonl
```

### 3.2 修改文件

```text
benchmarks/eval_baseline_models.py   # 允许记录执行来源及历史缺失的可选测量值
benchmarks/eval_baseline_stats.py    # 复用现有聚合，明确 null 不参与相应指标
benchmarks/README.md                 # 增加历史审计、对照命令和结论边界
tests/benchmarks/test_eval_baseline_models.py
tests/benchmarks/test_eval_baseline_stats.py
```

### 3.3 不新增或修改的内容

- 不新增通用 `VersionAdapter`（版本读取适配器）或插件注册机制；PR2 只有一个经过审计的旧入口，具体适配足够；
- 不新增 Runtime、Eval、Trace 的生产模型、字段或持久化文件；
- 不修改历史 worktree 的源码、测试、配置或基准文件；环境和实验工件只写入当前运行报告目录；
- 不改变 PR1 当前 Eval 样本“可信结果必须携带 Trace”的校验。

## 4. 接口设计

### 4.1 历史审计入口

`benchmarks/historical_baseline.py` 提供 CLI；候选提交必须显式传入，避免隐式改变基线选择范围。

```text
python -m benchmarks.historical_baseline audit \
  --candidate <git-commit> \
  --dataset runtime_core_v1 \
  --case tool_success \
  --output benchmarks/reports/historical-audits/<audit-id>
```

审计顺序固定为：

1. 解析不可变完整提交号；
2. 创建 detached worktree；
3. 按该提交声明的依赖文件创建独立解释器环境，并记录 Python 与依赖证据；
4. 子进程显式从历史 `src` 导入，拒绝导入当前 checkout；
5. 启动历史单工具场景并校验最终回答、工具名、参数、调用次数和终态；
6. 映射统一记录并连续执行开发期采样；通过全部门槛的候选才可冻结为历史基线。

任一门失败时记录候选、失败门、异常摘要和证据路径，退出为审计失败；不产生历史快照或对照百分比。已有同名 worktree、不可解析提交、环境创建失败或源码路径不属于该 worktree 都是明确失败，不静默回退到当前环境。

### 4.2 历史执行与采样入口

```text
python -m benchmarks.historical_baseline run \
  --candidate <audited-full-commit> \
  --dataset runtime_core_v1 \
  --case tool_success \
  --warmup 5 --repeat 30 \
  --audit-output benchmarks/reports/historical-audits/<audit-id> \
  --save-baseline benchmarks/baselines/runtime_core_v1
```

- `run` 只接受通过同一审计输出确认的完整提交；不能以短哈希或未审计候选直接运行正式基线。
- 场景复用 PR1 Dataset 的 `tool_success` 业务语义：固定请求、脚本 LLM 的单次工具请求、固定工具输出与最终回答断言。历史适配只处理旧入口的调用形状，不复制或扩展 Runtime 业务规则。
- 计时包围历史子进程内完整主流程调用；每个样本均启动独立的临时状态目录，避免 Session、Journal 或文件状态跨样本泄漏。
- warmup 与正式样本均追加到 `samples/<snapshot-id>.jsonl`；仅正式样本生成历史快照。

### 4.3 对照入口

```text
python -m benchmarks.historical_baseline compare \
  --current benchmarks/baselines/runtime_core_v1/<current-snapshot-id>.json \
  --historical benchmarks/baselines/runtime_core_v1/<historical-snapshot-id>.json \
  --output benchmarks/reports/historical-audits/<audit-id>/comparison.md
```

- 两份快照必须具有相同 Dataset、Case、场景语义、正式 repeat、warmup、机器标识、Python 主/次版本和固定替身配置；任一条件不一致时报告不可比原因，不计算变化率。
- 对比仅在两侧均具备非空测量值时计算 `(current - historical) / historical`；历史值为 0、缺失或语义不一致时仅列原值和不可比原因。
- 成功率报告成功数/总数、Wilson 95% 区间和绝对错误数；时延报告样本数、P50/P95/P99、最大值和变化率；调用数报告均值及分布。

## 5. 数据模型设计

PR2 不新增平行结果模型，复用 PR1 的 `BenchmarkSample`（单次采样记录）和 `BenchmarkSnapshot`（汇总快照）。二者仍是 Benchmark 派生读模型，不是 Runtime 权威事实或恢复控制记录。

对既有记录补充最小元数据：

| 字段 | 含义 | 规则 |
|---|---|---|
| `execution_source` | `current_eval` 或 `historical_adapter` | 明确数据由当前 Eval 或历史外围适配产生 |
| `source_commit` | 实际执行的完整 Git 提交 | 当前和历史均必填，不使用展示短哈希代替 |
| `scenario_id` | 统一业务场景标识 | PR2 固定为 `tool_success` |
| `evidence_kind` | `run_trace`、`journal`、`final_result` 或 `recorded_fixture_log` | 说明语义/统计事实来自何处 |

历史链路没有的 Trace、token、内部阶段时延必须序列化为 `null`，并在快照中列入缺失指标；不得补 0、推断或与当前 Trace 合并。PR1 的当前运行器仍要求可信 Eval 结果存在 Trace，历史允许缺 Trace 是由 `execution_source=historical_adapter` 限定的读取规则。

历史适配输出的最低语义事实为：终态、通过/失败、失败类别、外层耗时、循环轮数、工具调用数、工具名和参数校验结果、最终回答校验结果及证据引用。任一必填语义事实无法取得时，该候选审计失败。

## 6. 行为与一致性边界

- “首个通过审计的提交”是审计给定候选序列中的首个通过者，不等同于历史上最早、最新或性能最差的提交；审计报告必须列出候选顺序与选择理由。
- 历史 worktree、解释器环境、临时状态和每次样本的状态目录彼此隔离；Benchmark 绝不从当前 Runtime 进程调用历史代码。
- 历史旧测试的收集或通过只能作为审计证据，不能替代固定业务场景的运行门槛。
- 旧链路不支持的能力标注为“不支持”，不写为成功率 0%、耗时 0 或优化百分比。
- PR2 的性能结论仅适用于固定假 LLM/工具下的执行编排成本；它不代表真实模型、网络或供应商 API 性能。
- 正式当前/历史结果使用 `warmup=5, repeat=30`；开发期可用 `warmup=1, repeat=10`，且不得写入 README 或简历。

## 7. 必要的现有代码修改

仅调整 PR1 Benchmark 派生记录的通用序列化与聚合规则，以表达执行来源和历史缺失指标。原因是 PR2 必须与 PR1 使用同一快照格式，后续 PR3 至 PR7 也不能建立平行结果协议。

不改变 `EvalResult` 的 Trace 约束：当前 Eval 路径的无 Trace 仍是实验错误；历史路径由 PR2 外围适配器在记录层表达其证据来源与指标缺失。

## 8. 测试计划

### 8.1 正常路径

- 使用伪造 Git/worktree/子进程边界验证四道审计门按顺序执行，并记录完整候选证据；
- 固定历史输出可映射为与 PR1 相同 schema 的单次记录和历史快照；
- 当前与历史快照满足条件时，正确生成成功率、错误数、分位数、调用数和变化率；
- 连续样本使用不同临时状态目录，单次样本不会读取前一次状态。

### 8.2 边界路径

- 不存在、短格式、无法解析或未审计的提交被拒绝；
- 候选有工作树、环境或入口但场景断言失败时，审计记录失败且不生成基线；
- `warmup=0` 可用；正式 `repeat=0`、快照 ID 冲突和环境条件不一致明确失败；
- 历史缺失可选 Trace/Token 指标时，保留 `null`，但仍可对比共享的外层时延与调用数。

### 8.3 数据损坏

- 审计 JSON、历史子进程结果或记录型替身日志缺失必填字段、类型错误、未知 schema 或证据路径越界时明确失败；
- 历史输出声称成功但工具/最终回答校验不匹配时，不得映射为通过；
- 对照快照的内容摘要、Dataset、场景或采样信息被篡改时，拒绝计算百分比。

### 8.4 历史兼容

- 以已审计失败的 `e27f206`、`5cdb4ad` 结果样本作为审计失败夹具，验证迁移断裂不会被误判为可复跑；
- 以一个通过外围启动的固定历史输出夹具验证旧字段缺少 Trace 时的 `null` 映射；
- 不要求或修改历史源码以补齐当前字段。

### 8.5 回归测试

- PR1 Benchmark 模型、统计、报告与 CLI 测试继续通过；
- `tests/eval`、`tests/benchmarks`、完整 pytest、`compileall` 和 `git diff --check` 通过；
- 正式历史运行前后检查当前 worktree 无 Benchmark 以外的改动，历史 worktree 源码保持 detached 提交状态。

## 9. 实施顺序

1. 为 PR1 记录/快照补充执行来源、完整提交和缺失测量值的严格序列化规则，并先用测试保持当前 Trace 约束不变。
2. 实现历史审计的 worktree、独立环境、源码路径和候选报告；用伪造边界测试失败分类，不调用真实历史提交。
3. 为审计通过的单一旧 Agent v1 入口实现具体外围启动适配，并将 `tool_success` 结果映射到统一记录；不抽象第二种历史入口。
4. 实现快照可比性检查与对照报告，覆盖缺失值、0 基线、环境不一致和成功率区间。
5. 运行候选审计；若存在通过者，以开发采样验证后执行正式采样，保存 JSONL、历史快照和对照报告；若均失败，保存审计报告并停止在此边界。
6. 更新 Benchmark README，仅写入实际审计与正式运行产生的命令、证据和结论。

## 10. PR 验收标准

1. 候选提交只能在独立 worktree、独立解释器环境和显式历史源码路径中审计；
2. 审计报告逐项记录候选、环境、入口、场景及排除原因；
3. 只有通过固定 `tool_success` 等价场景的候选能生成历史快照；
4. 当前与历史快照复用同一记录格式；历史缺失值为 `null`，绝不伪造成 0；
5. 对照报告仅对环境和业务语义一致的共享指标计算百分比；
6. 历史不可复跑时，交付可追溯审计报告而非虚假的性能结论；
7. Runtime、Eval 主流程和历史源码均未被 Benchmark 侵入或修改。

## 11. 最终交付结果

PR2 完成后，开发者可先运行：

```text
python -m benchmarks.historical_baseline audit --candidate <commit> --dataset runtime_core_v1 --case tool_success
```

审计通过后，再生成历史快照并和 PR1 当前快照对照。PR2 只证明旧/新单工具执行主链的可比业务结果与编排成本；并发、恢复、安全、上下文和委派的量化结论仍由后续专门 PR 提供。
