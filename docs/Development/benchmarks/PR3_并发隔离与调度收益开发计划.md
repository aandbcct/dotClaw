# dotClaw Benchmark PR3：并发隔离与调度收益开发计划

> 状态：已确认的开发基线。本文定义 PR3 的唯一范围；实现发现当前 Runtime 无法提供所需权威事实时，先以失败场景证明，再单独确认最小改动，不把 Benchmark 观察字段写入生产领域模型。

## 1. PR 定位

### 1.1 唯一目标

以固定的并发工作负载量化当前 Session 级串行、跨 Session 并行、状态隔离与取消不阻塞行为，并在相同 Runtime 下对照 Benchmark 内部全局串行调度的成本。

### 1.2 当前问题

- `SessionRunCoordinator` 已以每 Session 异步锁串行化 `submit_prepared()`，不同 Session 使用不同锁；`cancel()` 明确不等待该锁。
- 现有测试已验证同 Session FIFO、不同 Session 独立持久化目录和运行级输出端口不串流，但均为单次断言，不能报告执行规模、错误率、排队时延、吞吐或相对全局串行的收益。
- `RunTrace` 与 PR1 的 Eval Case 面向单 Run 语义；并发顺序、排队等待、跨事实归属和取消时延需要 Benchmark 在外围编排、采样和读取，不应侵入 Runtime 主流程。

### 1.3 完成后的链路

```text
固定并发场景 + 固定延迟 Fake LLM / Fake Tool
    → SessionInteractionService
    → SessionRunCoordinator → RuntimeEngine
    → Run / RunMessage / RunEvent / ContextVersion / 工具记录 / 输出收集器
    → BenchmarkSample（单次采样记录）JSONL
    → BenchmarkSnapshot（汇总快照）+ 正确性与调度对照报告
```

全局串行仅是 Benchmark 进程内为同一 Runtime 工作负载施加的一把全局锁，不是生产路径、旧版本或推荐架构。

## 2. PR 边界

### 2.1 包含内容

1. 通过 `SessionInteractionService` 发起同 Session FIFO、多 Session 隔离、Session 数扩展、长短混合负载和取消不阻塞的受控实验。
2. 在 Benchmark 入口为每个 Session 分配单调 `accepted_seq`，并验证开始执行、完成及 Conversation 持久化顺序与它一致。
3. 为每一次并发请求注入唯一 Session、Run、请求、工具和流式输出标识，读取运行事实并统计跨 Session 串扰、串流、乱序、重复和遗漏。
4. 在 Session 级锁与 Benchmark 全局锁两种调度模式下运行相同负载，报告总耗时、吞吐、排队等待和端到端 P50/P95。
5. 对长 Run 持锁期间的取消，分别记录取消接口送达耗时、Run 进入取消终态的生效耗时、锁释放及同 Session 后续请求可用性。

### 2.2 明确不包含

- 不修改 `SessionRunCoordinator`、`RuntimeEngine`、`SessionInteractionService` 的锁、排队或取消生产语义；
- 不将 `accepted_seq`、排队时间、Benchmark 标识或全局锁写入 `RunRequest`、Run、Event、Conversation 或生产持久化；
- 不比较历史 Git 版本；历史单工具主链对照属于 PR2；
- 不注入进程崩溃、外部副作用失败或恢复控制点；这些属于 PR4；
- 不验证 Capability 策略正确性、ContextVersion 重放或多 Agent 委派；这些分别属于 PR5、PR6、PR7；
- 不调用真实模型、网络、外部 Tool 或生成 `[EXT]` 结论；
- 不把全局锁模式用于正常 Runtime、CLI 或 CI Gate。

## 3. 模块结构

### 3.1 新增文件

```text
benchmarks/
├── concurrency_reliability.py       # 并发场景 CLI、编排与快照写出
├── concurrency_workloads.py         # 固定工作负载、标识生成与受控延迟替身
├── concurrency_assertions.py        # 顺序、归属、隔离、取消和可比性判定
└── concurrency_stats.py             # 吞吐、排队/端到端时延与对照聚合

tests/benchmarks/
├── test_concurrency_workloads.py
├── test_concurrency_assertions.py
├── test_concurrency_stats.py
└── test_concurrency_reliability.py
```

运行工件使用 PR1 的快照规则，单次记录直接以快照 ID 命名，不再按记录 ID 创建目录：

```text
benchmarks/baselines/reliability_concurrency_v1/
├── <snapshot-id>.json
└── samples/
    └── <snapshot-id>.jsonl

benchmarks/reports/concurrency/<run-id>/
├── correctness.md
├── scheduling-comparison.md
└── workload-config.json
```

### 3.2 修改文件

```text
benchmarks/eval_baseline_models.py   # 扩展统一记录的并发/取消观察字段与 schema 校验
benchmarks/eval_baseline_stats.py    # 复用分位数和成功率，支持按调度模式、场景聚合
benchmarks/README.md                 # 增加 PR3 命令、基线类型和结论边界
tests/benchmarks/test_eval_baseline_models.py
tests/benchmarks/test_eval_baseline_stats.py
```

### 3.3 不新增或修改的内容

- 不新建 Runtime Port、事件、Repository、锁服务或生产配置；
- 不为“全局锁”和“Session 锁”建立可扩展策略框架；PR3 只有当前生产路径和 Benchmark 内部对照两种明确模式；
- 不修改 PR1 Dataset 或把并发场景伪装为单 Run `EvalCase`；
- 不新建平行采样、统计、快照或报告格式。

## 4. 场景与接口设计

### 4.1 统一入口

```text
python -m benchmarks.concurrency_reliability \
  --suite reliability_concurrency_v1 \
  --core-warmup 5 --core-repeat 50 \
  --scaling-warmup 5 --scaling-repeat 30 \
  --fake-delay-ms 20 \
  --output benchmarks/reports/concurrency/<run-id> \
  --save-baseline benchmarks/baselines/reliability_concurrency_v1
```

- 所有普通请求经 `SessionInteractionService.submit()` 进入，以覆盖 Session 路由、冻结请求、协调器和运行级输出端口。
- 取消经同一入口的 `SessionInteractionService.cancel()` 发起；不直接调用 Engine 绕过应用链路。
- 每轮新建隔离的测试存储根和 Runtime 服务实例；全局锁对照与 Session 锁实验也使用彼此独立的存储根，防止 Conversation、Run 或缓存残留影响结果。
- `fake-delay-ms`、Session 数、每 Session 请求数、长/短延迟、warmup、repeat 和调度模式必须写入 `workload-config.json` 与每条样本。

### 4.2 同 Session FIFO 场景

每轮向一个已加载 Session 对象并发提交 20 个请求。Benchmark 在入口以该 Session 的同步计数器分配 `accepted_seq=1..20`；受控提交闸门按此序号放行，使其表示 API 接受顺序，而非不可控的协程调度先后。

每个请求带有唯一 `session_id`、`accepted_seq`、请求标识和期望答案标识。固定延迟替身记录 Runtime 真正开始执行的次序；Run 终态、Conversation 读取结果和输出记录提供完成与持久化次序。

一轮通过必须同时满足：

1. 所有 20 个请求产生唯一 Run，均完成且返回自身标识；
2. Runtime 开始执行、Run 完成、Conversation 中成功消息的顺序均严格等于 `accepted_seq`；
3. 每个序号恰好出现一次；乱序、重复、遗漏均为 0。

正式采样为 50 轮，即至少 1,000 个同 Session 请求；报告同时给出每轮和总计的绝对错误数、成功数/总数及 Wilson 95% 区间。

### 4.3 多 Session 隔离场景

每轮创建 8 个 Session，每个 Session 并发提交 4 个带唯一标识的请求。替身 LLM、工具和输出收集器均回显所属 Session、Run 与请求标识；Context 测试贡献中也包含该 Session 的唯一标识。

每个请求完成后，Benchmark 读取其返回结果、`RunMessage`、`RunEvent`、`ContextVersion`、工具记录及流式输出，并检查：

- 每个事实只含本 Session / Run / 请求的允许标识；
- 不包含其他 Session 的标识；
- Run、ContextVersion 和工具记录的所有者/引用与本请求一致；
- 输出收集器仅得到自身 Run 的流式片段。

正式采样为 50 轮，即至少 1,600 个多 Session 请求。跨 Session 消息串扰、上下文串扰、工具结果串扰和输出串流均分别计数，目标是各项 `0/N`，不只报告合并成功率。

### 4.4 跨 Session 并行收益场景

对每一个固定负载，分别运行：

- 当前 Session 级锁模式：正常通过 `SessionInteractionService` 提交；
- Benchmark 全局锁模式：在同一应用入口之外，以一把仅属于该测试批次的异步锁包围每次提交。

两种模式使用相同提交、Python、Fake LLM/Tool、固定延迟、Session/请求标识和预热/重复次数。每个 Session 内仍按 `accepted_seq` 有序；全局锁模式只改变不同 Session 之间是否能重叠执行。

负载包括：

| 负载 | 配置 | 目的 |
|---|---|---|
| Session 扩展 | 1/2/4/8 个 Session，每 Session 4 个短请求 | 绘制随独立会话数增加的吞吐、等待和 P95 曲线 |
| 固定并发 | 8 个 Session，每 Session 4 个短请求 | 形成当前与全局串行的主对照数据 |
| 长短混合 | 1 个 Session 的 1 个长请求，另有 7 个 Session 的短请求 | 证明长任务不会阻塞其他 Session 的短任务 |

短请求和长请求均由固定延迟替身控制；默认短延迟 20ms、长延迟 200ms，正式结果必须以保存的配置为准。记录批次总耗时、吞吐量、每请求入口至开始执行的排队等待、入口至终态的端到端时延及其 P50/P95。变化率只在同负载的两种模式间计算，并标明“相对 Benchmark 全局串行调度”。

Session 扩展、固定并发和长短混合的每个调度模式/负载组合各预热 5 次、正式执行 30 次；该次数只用于调度效率对照，不混入 FIFO、隔离或取消正确率。

### 4.5 取消不阻塞场景

每轮启动一个受控长 Run，在其进入 LLM/Tool 等待点且仍持有 Session 执行权后，经 `SessionInteractionService.cancel()` 发送取消。时间点定义为：

- **送达耗时**：从调用 `cancel()` 到取消调用返回；
- **生效耗时**：从调用 `cancel()` 到该 Run 持久化为取消终态；
- **后续可用性**：取消 Run 收口后，同 Session 新请求能完成且不返回 `SESSION_BUSY`。

一轮通过必须证明：取消调用在长 Run 正常延迟结束前返回；目标 Run 进入取消终态；Run 收口后租约释放；后续同 Session 请求完成。正式采样为 50 轮，并分别报告送达/生效时延 P50/P95、失败数与锁释放失败数。

## 5. 数据模型与统计口径

PR3 继续使用 `BenchmarkSample`（单次采样记录）和 `BenchmarkSnapshot`（汇总快照），它们都是 Benchmark 派生读模型，不是 Runtime 权威事实。

单次记录新增且必须严格校验的并发观察字段：

| 字段组 | 字段 | 说明 |
|---|---|---|
| 工作负载 | scenario_id、schedule_mode、session_count、requests_per_session、fake_delay_ms | 固定实验条件 |
| 顺序 | accepted_seq、execution_started_seq、completed_seq、conversation_commit_seq | 同 Session FIFO 的外部观察证据 |
| 时延 | queue_wait_ms、wall_duration_ms、cancel_delivery_ms、cancel_effect_ms | 缺失时为 `null`，不当作 0 |
| 隔离 | message_leak_count、event_leak_count、context_leak_count、tool_leak_count、stream_leak_count | 按事实类型拆分的绝对错误数 |
| 取消 | cancellation_delivered、cancellation_effective、lock_released、followup_completed | 取消路径的布尔结果 |
| 证据 | Run/ContextVersion/输出收集器/工具记录引用与内容摘要 | 不保存 Prompt、密钥或完整输出正文 |

快照除 PR1 的成功率与分位数外，按场景、Session 数和调度模式汇总：请求数、错误数、Wilson 95% 区间、乱序/重复/遗漏、各类串扰、吞吐、排队等待与端到端时延。全局锁对照仅计算两侧共享且条件一致的指标；0 基线、缺失值或配置不一致时不计算变化率。

## 6. 行为与一致性边界

- `accepted_seq` 是 Benchmark 入口观察值，仅用于定义本次实验的请求接受顺序；不声称 Runtime 已持久化该字段。
- FIFO 结论只针对受控、同进程、单 `SessionRunCoordinator` 实例内的同 Session 提交；跨进程公平性不在 PR3 承诺范围。
- 隔离判据以真实持久化运行事实和运行级输出端口为准；Fake LLM/Tool 只消除外部不确定性，不替代事实读取。
- 全局锁对照证明调度结构的容量影响，不能表述为真实 Provider/API 端到端加速或历史版本提升。
- 长短混合的成功标准是短请求不等待无关 Session 的长请求；不要求短请求绝对零等待，也不承诺操作系统级实时性。
- 取消送达不等同于外部副作用已停止；PR3 仅证明 Runtime 内取消信号、终态收口与租约释放。跨崩溃、副作用幂等与恢复正确性留给 PR4。

## 7. 必要的现有代码修改

仅扩展 PR1 的 Benchmark 派生记录、统计与报告能力，用于保存并发实验的观察字段和场景聚合。原因是 PR3 必须复用同一原始 JSONL 与快照协议，后续实验不能各自建立结果格式。

不修改 Runtime 生产代码。若现有读取接口无法定位某类事实，将先新增失败的 Benchmark 集成测试；任何为读取权威事实而需要的最小生产接口调整必须另行确认。

## 8. 测试计划

### 8.1 正常路径

- 受控提交闸门为每个 Session 分配连续 `accepted_seq`，同 Session 20 请求的开始、完成和 Conversation 顺序均匹配；
- 8×4 场景中每类运行事实和输出收集器均只含所属标识；
- 全局锁模式与 Session 锁模式在相同构造样本下正确统计吞吐、排队和端到端分位数；
- 长短混合场景中短请求在长请求终态之前完成；
- 取消场景正确记录送达、生效、锁释放与后续请求完成。

### 8.2 边界路径

- Session 数 1 时两种调度模式不报告虚假的并行收益；
- 0/负数 Session 数、请求数、延迟、warmup 或 repeat 被明确拒绝；
- 同一 Session 中任意请求失败、重试、重复标识、漏标识或 Conversation 顺序不一致时，整轮正确性失败且保留原始证据；
- 取消尚未到达长 Run 等待点、取消对象不存在或后续请求仍 `SESSION_BUSY` 时，明确归类失败。

### 8.3 数据损坏

- Run/消息/Event/ContextVersion/工具/输出证据缺失、所有者不匹配、未知 schema、类型错误或跨输出目录引用时明确失败；
- JSONL/快照中 `accepted_seq`、时延、调度模式或负载配置不合法时拒绝聚合；
- 任一对照侧缺少同负载配置、原始证据摘要不一致或混入 warmup 时拒绝生成变化率。

### 8.4 历史兼容

PR3 不读取历史 Git Runtime 记录；仅验证 PR1 样本在新增并发字段缺失时按 schema 版本规则明确读取，而不是静默推断为 0。

### 8.5 回归测试

- PR1 Eval 基线模型、统计和 CLI 测试继续通过；
- `tests/runtime_v2` 中 FIFO、跨 Session、输出隔离及取消相关测试继续通过；
- `tests/benchmarks`、完整 pytest、`compileall` 和 `git diff --check` 通过。

## 9. 实施顺序

1. 扩展统一采样记录和统计的并发字段、schema 版本与严格反序列化测试，确保 PR1 数据缺字段时不会被当作 0。
2. 实现固定延迟替身、标识编码、受控提交闸门与单轮事实读取；先完成同 Session FIFO 与多 Session 隔离的失败/成功断言。
3. 实现 Session 锁/全局锁两种 Benchmark 调度模式、Session 数扩展和长短混合负载，补齐可比性校验及统计报告。
4. 实现取消不阻塞场景，区分送达和生效时延，并验证锁释放与后续请求。
5. 接入 JSONL、快照和 Markdown 报告；用开发期采样验证后，执行核心正确性/取消 `warmup=5, repeat=50` 与扩展负载 `warmup=5, repeat=30`，保存基线与原始数据。
6. 更新 Benchmark README，仅写入正式运行实际得到的正确性和调度结论。

## 10. PR 验收标准

1. 同 Session 20×50 请求以 `accepted_seq` 为基准，开始、完成与 Conversation 顺序均无乱序、重复或遗漏；
2. 8×4×50 请求中，返回结果、RunMessage、RunEvent、ContextVersion、工具记录和流式输出的串扰分别可统计，正式结论可表达为各项 `0/N`；
3. 1/2/4/8 Session、8×4 与长短混合负载均能在两种调度模式下产出可比 JSONL 与报告；
4. 报告区分总耗时、吞吐、排队等待和端到端 P50/P95，并仅以全局锁模式作为调度对照；
5. 取消 50 轮报告送达/生效时延、终态成功、锁释放和后续请求可用性；
6. 基线配置、环境、固定延迟、原始数据、快照和统计脚本可追溯且不覆盖既有快照；
7. Runtime 生产锁、状态机、持久化和接口未因 Benchmark 而改变。

## 11. 最终交付结果

PR3 完成后可执行固定配置的并发套件，并得到可复跑的两类结论：

```text
正确性：同 Session 0/N 乱序、重复、遗漏；跨 Session 0/N 消息、事件、上下文、工具和输出串扰。
效率：相对 Benchmark 全局串行调度的吞吐、P95 排队等待和长短混合负载结果。
```

它不证明跨进程调度公平性、真实 API 性能、崩溃恢复、工具安全或多 Agent 委派可靠性；这些由后续 PR 分别验证。
