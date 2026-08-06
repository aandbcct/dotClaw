# dotClaw Benchmark PR7：多 Agent 委派与证据收口开发计划

> 状态：已确认的开发基线。本文定义 PR7 的唯一范围；只量化当前单进程委派的父子 Run 语义和证据交付，不扩展跨进程等待恢复、远程 Agent、嵌套/并行委派或真实 API 压测。

## 1. PR 定位

### 1.1 唯一目标

量化当前多 Agent 委派的父子 Run 隔离、结果回灌、失败/取消传播与多父 Run 并发下的链路一致性，并将 PR1 至 PR6 的正式快照收口为可追溯的测试报告、README 与简历候选表述。

### 1.2 当前问题

- 当前 RuntimeDelegationAdapter（运行时委派适配器）会为目标 Agent 建立独立 Session 与子 Run；父 Run 在提交成功后进入委派挂起，`resume_delegation(child_run_id)` 回取结果并继续父 Run。现有 Runtime 测试覆盖若干单点路径，但没有统一的重复执行、时延、串扰和重复回灌数据。
- 取消服务会向已登记的子 Run 传播取消，但父取消、子终态和后续同父/同 Session 行为尚未以完整实验表和原始记录量化。
- PR1 至 PR6 的正式样本、快照和场景结论需要统一的证据筛选、覆盖率报告和对外表述规则；否则不能区分调试结果与可用于 README、报告或简历的结果。

### 1.3 完成后的链路

```text
固定父/子 Agent 与委派 Fixture
    → RuntimeDelegationAdapter + Task/Run/Broker 隔离执行
    → 父/子 Run、Task、消息、事件、Conversation 与取消事实
    → 委派正确性/时延/串扰指标
    → BenchmarkSample（单次采样记录）JSONL + Delegation 快照/报告

PR1 至 PR6 正式快照 + pytest-cov 原始结果
    → 证据清单与分层覆盖率
    → README/测试报告/简历候选结论
```

## 2. PR 边界

### 2.1 包含内容

1. 以完整有限结果表验证子 Run 完成、失败、取消、放弃时的父 Run 挂起、单次结果回灌、Task/事件语义与最终状态。
2. 验证父 Run 主动取消向子 Run 的传播、子 Run 收口后的锁/执行权释放及同一父 Session 后续请求可继续执行。
3. 以多个父 Session 并发重复执行，量化父/子 Run、Task、Broker 消息、结果回灌和流式输出的链路归属、重复/遗漏/串扰数及挂起至回灌时延。
4. 为 PR1 至 PR7 正式快照建立证据清单、报告生成与 README/简历候选表述规则；加入 `pytest-cov` 并报告真实总体和分层覆盖率。

### 2.2 明确不包含

- 不实现跨进程或冷重建后的 Task 等待映射、父子关系持久化、结果回灌恢复或 exactly-once 外部委派；这些仍是当前能力边界；
- 不新增远程 Runner、端点认证协议、任务预算、租约、冲突仲裁、嵌套/并行委派或新的生产 Broker；
- 不修改 Runtime 的委派状态机、Task 合约、取消协议、Session 锁、生产日志或持久化 schema；
- 不以真实 LLM/API、真实网络或供应商时延证明 Runtime 委派效率；如确需此类问题，使用独立 `[EXT]` Dataset/快照/报告；
- 不把 pytest 覆盖率设为准入阈值，也不以覆盖率替代并发、恢复、安全和委派的正确性结论；
- 不将开发期 quick run、缺少原始 JSONL 的旧结果或未固定环境的结果写入 README 或简历。

## 3. 模块结构

### 3.1 新增文件

```text
benchmarks/
├── delegation_reliability.py        # 委派套件 CLI、场景编排与快照写出
├── delegation_workloads.py          # 父/子 Agent Fixture、终态、取消与并发负载
├── delegation_assertions.py         # 父子链路、回灌次数、隔离与取消判定
├── delegation_stats.py              # 结果表、时延、错误数与置信区间聚合
└── evidence_report.py               # 正式快照/覆盖率证据清单和 Markdown 报告

tests/benchmarks/
├── test_delegation_workloads.py
├── test_delegation_assertions.py
├── test_delegation_stats.py
├── test_delegation_reliability.py
└── test_evidence_report.py
```

运行工件沿用统一布局：

```text
benchmarks/baselines/reliability_delegation_v1/
├── <snapshot-id>.json
└── samples/
    └── <snapshot-id>.jsonl

benchmarks/reports/delegation/<run-id>/
├── outcome-matrix.md
├── cancellation.md
├── concurrent-isolation.md
├── evidence-manifest.json
├── coverage.md
└── delegation-config.json
```

### 3.2 修改文件

```text
benchmarks/eval_baseline_models.py   # 扩展统一记录的委派链路、回灌、取消和证据字段
benchmarks/eval_baseline_stats.py    # 复用统一聚合与正式样本资格校验
benchmarks/README.md                 # 增加 PR7 命令、指标、边界和结果写入规则
pyproject.toml                       # 增加 pytest-cov 开发依赖和覆盖率报告配置
README.md                            # 只在正式快照产生后写入实际量化结果与复现入口
tests/benchmarks/test_eval_baseline_models.py
tests/benchmarks/test_eval_baseline_stats.py
```

### 3.3 不新增或修改的内容

- 不新建平行样本、快照、报告格式；PR7 使用 PR1 的 `BenchmarkSample`（单次采样记录）与 `BenchmarkSnapshot`（汇总基线快照）；
- 不在 `src/dotclaw/runtime/`、`src/dotclaw/orchestration/` 或 `src/dotclaw/tools/` 注入 Benchmark 标识、计时、覆盖率或测试开关；
- 不将报告生成器变成生产分析服务，也不自动改写 README 的数字。

## 4. 场景与接口设计

### 4.1 统一入口

```text
python -m benchmarks.delegation_reliability \
  --suite reliability_delegation_v1 \
  --outcome-warmup 1 --outcome-repeat 1 \
  --cancellation-warmup 5 --cancellation-repeat 100 \
  --concurrent-parents 8 --concurrent-repeat 100 \
  --output benchmarks/reports/delegation/<run-id> \
  --save-baseline benchmarks/baselines/reliability_delegation_v1

pytest --cov=src/dotclaw --cov-report=json --cov-report=term-missing
python -m benchmarks.evidence_report \
  --snapshots benchmarks/baselines \
  --coverage coverage.json \
  --output benchmarks/reports/evidence/<run-id>
```

- 所有场景使用独立临时存储根与固定 Fixture；父/子 Agent、Session、Run、Task、工具、取消时机和 Fake LLM/Tool 延迟写入 `delegation-config.json` 与单次记录。
- 结果终态表为确定性有限 Case，每行执行一次；父取消和多父并发隔离各预热 5 次、正式重复 100 次。正式 README/简历使用的性能型样本仍遵循统一 `warmup=5, repeat=30`，不得与正确率样本混合。
- 计时范围、环境、Git 提交、Dataset/Fixture 版本、配置哈希和原始 JSONL 必须同时写入快照，缺失任一项的结果只能标记为诊断。

### 4.2 子终态与结果回灌有限表

固定同一父/子输入，分别构造四种子 Run 终态：`completed`、`failed`、`cancelled`、`abandoned`。每一行必须验证：

| 观察项 | 判据 |
|---|---|
| 提交链 | 父 Run 恰有一次委派提交，子 Run 与目标 Session/Agent、父 Run、Task 关联正确 |
| 挂起状态 | 父 Run 仅在子 Run 已取得稳定标识后进入委派挂起，控制节点与 Task 状态匹配 |
| 结果回灌 | 每个子终态对应预期父侧结果/失败语义；`DELEGATION_RESULT`、完成相关事件和 Conversation 只出现一次 |
| 幂等边界 | 重复使用相同 `child_run_id` 恢复、未知/不匹配子 Run 标识或重复回灌请求，不得产生第二条结果、第二次继续执行或新的子 Run |
| 终态归属 | 父/子 Run、Task、Broker 消息、RunEvent、RunMessage、Conversation 和流输出均只含本链路标识 |

该表报告通过行数/总行数、错误类型与绝对错误数；不把子 Run 的失败、取消或放弃表述为“委派成功”。

### 4.3 父取消传播

父 Run 在子 Run 执行期间主动取消，受控重复 100 次。单次记录至少包括：取消请求发出时间、父取消送达时间、子取消送达时间、父/子进入取消终态时间、子 Run 收口后同父 Session 后续请求开始/完成时间。

一轮通过必须满足：

1. 取消传播到关联子 Run，且不影响无关父/子链路；
2. 父和子都按当前语义进入预期取消终态，不被误记为普通失败；
3. 父侧不产生重复结果回灌、重复完成事件或额外 Conversation；
4. 子 Run 收口后执行权释放，同一父 Session 的后续请求可开始并完成；
5. 取消接口送达耗时与父/子生效耗时可独立统计 P50/P95，超时、遗漏、重复和串扰分别计数。

本场景只证明当前进程内取消传播；进程崩溃后的取消/等待恢复不计入成功率。

### 4.4 多父 Run 并发隔离与回灌时延

使用 8 个并发父 Session、每个固定一条委派请求，重复 100 轮。每条链路写入唯一父 Session/Run/Task/请求标识以及唯一子目标标识，使用固定延迟的 Fixture，避免供应商波动掩盖编排行为。

每轮验证：

- 父 Run、子 Run、Task、目标 Agent/Session 与 Broker 消息一一归属本链路；
- 每条链路的子 Run 创建数、结果回灌数、`delegation_submitted` 与 `delegation_completed` 事件数均为 1；
- 父挂起至子结果回灌的时延、父最终完成时延和错误类别写入样本，汇总 P50/P95；
- 任何跨父 Session 消息、任务结果、ContextVersion、工具事实或流式输出串流均计为泄漏；重复子 Run、重复回灌、遗漏完成和错误投递分别计数。

报告使用“通过轮次/总轮次、绝对错误数、Wilson 区间、P50/P95”呈现；Fixture 固定时延下的时延只用于描述本地委派编排，不外推为真实模型端到端性能。

### 4.5 证据收口与覆盖率

`evidence_report.py` 只接收已完成正式采样的 PR1 至 PR7 快照与 `coverage.json`，生成不可混淆的证据清单：

- 每个结论对应 Git 提交、机器/环境、Dataset/Fixture、配置哈希、预热/正式样本数、原始 JSONL、快照与报告路径；
- 仅按真实源文件路径汇总总体覆盖率，以及 Runtime、Tool、Context、Orchestration、LLM 五个目录的行/分支覆盖率；未命中目录显式报告“不适用/未覆盖”，不填零或猜测；
- 只有具备完整证据链且结果来自正式样本的指标，才生成 README 表格行和简历候选句；没有实际数字时保留结论模板而不写百分比；
- 报告将“正确性”、“本地 Fixture 编排效率”、“固定语料/安全链成本”和“[EXT] 真实 API”分区，禁止跨区混合聚合或归因。

## 5. 数据模型与统计口径

PR7 继续使用 `BenchmarkSample` 和 `BenchmarkSnapshot`，二者是 Benchmark 派生读模型，不是 Task、Run、Broker 或取消控制事实。

新增委派字段：

| 字段组 | 字段 | 说明 |
|---|---|---|
| 链路 | parent_run_id、child_run_id、task_id、parent_session_id、child_session_id、target_agent_id、chain_request_id | 使用脱敏/哈希标识关联单条委派链 |
| 语义 | child_outcome、parent_outcome、delegation_submit_count、result_backfill_count、delegation_submitted_event_count、delegation_completed_event_count | 终态、幂等和事件事实 |
| 隔离 | cross_chain_message_count、cross_chain_context_count、cross_chain_tool_count、cross_chain_stream_count、misdelivery_count | 所有非零均为错误 |
| 取消 | cancel_delivery_ms、parent_cancel_effect_ms、child_cancel_effect_ms、followup_started、followup_completed | 取消与执行权释放 |
| 时延 | suspend_to_backfill_ms、parent_end_to_end_ms | 固定 Fixture 下的本地编排时延 |
| 证据 | git_commit、fixture_version、config_hash、environment、raw_sample_path、formal_sampling | 结果进入对外结论的资格 |

统计规则：确定性终态表使用通过行数/总行数；100 次场景使用成功率、绝对错误数和 Wilson 置信区间；时延使用正式非 warmup 样本的 P50/P95。只有对照双方工作负载、延迟、样本资格、环境和计时范围相同时才计算相对变化。

## 6. 行为与一致性边界

- 当前委派结论限于单进程、单层、Fixture 控制的源到目标执行，不宣称跨进程恢复、持久化等待、远程 Agent 或分布式 exactly-once；
- “单次回灌”仅指当前 Runtime 内部的结果消息、状态推进、事件与 Conversation 事实不重复，不等同于子 Agent 所有外部副作用 exactly-once；
- 取消生效时间受固定 Fixture、调度和本机负载影响；只报告测量条件下的分布，不承诺实时 SLA；
- 覆盖率衡量被测试的代码行/分支，不证明场景完整、没有竞态或业务效果；
- README 和简历只能引用实际正式快照中的数字，且必须携带适用场景与能力边界。

## 7. 必要的现有代码修改

仅修改 Benchmark 派生记录、统计、报告、依赖声明和文档。`pytest-cov` 作为开发依赖加入现有测试配置，覆盖率收集保持在测试命令外部；不改写 Runtime/Orchestration 的生产控制流。

若当前公开读取接口不足以核验某项父子关系、事件次数、Broker 消息或流归属，先增加失败的 Benchmark 读取测试，再单独确认最小只读查询接口；不得为了测试而向生产路径加入 Benchmark 状态。

## 8. 测试计划

### 8.1 正常路径

- 完整四行子终态表按预期挂起、回灌、收口，且每条链路只创建一个子 Run；
- 100 次父取消均能记录父/子送达及生效时延，子 Run 收口后后续请求可继续执行；
- 8 父 Session 并发 × 100 轮的所有链路都可定位到唯一父/子/Task/Broker 事实，生成 P50/P95；
- 已有正式快照和覆盖率输入可生成带完整追溯信息的证据清单。

### 8.2 边界路径

- 目标 Agent 不存在、子 Run 标识未知/不匹配、子终态已回灌、重复恢复、父已终态或取消时，明确拒绝或按既有幂等语义处理；
- 子失败、取消、放弃不污染无关链路，也不被错误统计为完成；
- 样本缺少 Git 提交、环境、固定 Fixture、原始 JSONL、正式采样标识或快照引用时，拒绝进入 README/简历候选输出；
- 覆盖率 JSON 缺失、目录重叠、源文件无法映射或报告版本不匹配时明确失败。

### 8.3 数据损坏

- 父/子/Task 关联不一致、事件计数冲突、Broker 消息顺序或目标不匹配、流记录缺失所属标识时计为链路错误，不静默忽略；
- JSONL 混入 warmup、不同 Fixture、不同 Git 提交、不同场景或重复样本时拒绝正式聚合；
- 覆盖率数据字段类型错误、文件路径逃逸、证据清单与快照统计不一致时拒绝生成对外结论。

### 8.4 历史兼容

PR7 不把历史 Git 委派实现纳入同口径性能比较。它验证 PR1 至 PR6 的已存在正式快照可在 schema 允许的缺失委派字段下读取，但缺失不得解释为零委派错误或零覆盖率。

### 8.5 回归测试

- 现有 `tests/runtime_v2/test_delegation_*`、Task/Broker、取消、Context/Run 关联测试继续通过；
- PR1 至 PR6 Benchmark 模型、统计、快照、报告与历史基线读取测试继续通过；
- `tests/benchmarks`、完整 pytest（含 `pytest-cov`）、`compileall` 和 `git diff --check` 通过。

## 9. 实施顺序

1. 扩展统一记录/快照的委派、取消、隔离和正式证据字段，并先写严格 schema/资格校验测试。
2. 装配固定父/子 Fixture 与四行子终态表，完成父子关联、事件、回灌与重复请求断言。
3. 实现父取消传播与后续请求释放实验，记录送达/生效时延并完成 100 次采样。
4. 实现 8 父 Session 并发工作负载与链路归属断言，完成 100 轮隔离与回灌时延采样。
5. 加入 `pytest-cov`，实现快照/覆盖率证据清单和报告，拒绝不完整或非正式证据。
6. 执行正式采样；仅将实际结果、复现命令和边界写入 `benchmarks/README.md`、README 和简历候选报告。

## 10. PR 验收标准

1. 四行子终态有限表中，父/子 Run、Task、事件、结果回灌和最终状态均符合当前 Runtime 语义，重复/未知/不匹配请求不产生额外子 Run 或回灌；
2. 100 次父取消中，父/子取消送达和生效时延、重复/遗漏/串扰数均有 JSONL 原始证据，子收口后同 Session 后续请求可继续执行；
3. 8 父 Session 并发 × 100 轮中，父/子/Task/Broker/Context/工具/流式输出跨链路泄漏、错误投递、重复创建、重复回灌和遗漏完成均可统计；
4. 委派时延以固定 Fixture 下的 P50/P95 报告，且不外推为真实模型或网络性能；
5. `pytest-cov` 报告总体及 Runtime、Tool、Context、Orchestration、LLM 的真实行/分支覆盖率，未设置虚假阈值；
6. 每一条 README/简历候选结论都能追溯到固定提交、环境、Dataset/Fixture、配置、正式样本、JSONL、快照和报告；
7. 未宣称跨进程恢复、远程/嵌套委派、外部副作用 exactly-once、真实 API 性能或覆盖率即可靠性；
8. Runtime、Task、Broker、取消、Context 与生产持久化语义未因 Benchmark 改写。

## 11. 最终交付结果

PR7 完成后，可在正式快照已产生的前提下写出以下类型结论：

```text
在 X 条确定性委派终态路径中，父子关联、单次结果回灌与状态语义均为 X/X；重复、遗漏和错误投递为 0。
在 8 个并发父 Session、100 轮固定 Fixture 委派中，跨链路消息、上下文、工具结果和流式输出串扰为 0；父挂起至结果回灌 P95 为 Y ms。
在 100 次父取消实验中，取消传播遗漏为 0，子 Run 收口后同 Session 后续请求可继续执行。
Reliability & Benchmark Suite 覆盖 X 个正式场景、Y 次正式采样；总体及 Runtime/Tool/Context/Orchestration/LLM 覆盖率见可追溯测试报告。
```

所有 X/Y 必须以实际正式快照替换。简历只保留与当前职位相关、能由证据清单复现的两到三条，且注明固定 Fixture/单进程等适用边界；无正式数据时不使用百分比或零错误表述。
