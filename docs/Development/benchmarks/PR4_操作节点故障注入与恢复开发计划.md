# dotClaw Benchmark PR4：操作节点故障注入与恢复开发计划

> 状态：已确认的开发基线。本文定义 PR4 的唯一范围；无法由当前持久化事实证明的恢复能力必须报告为能力边界或预期失败，不得写入恢复成功率。

## 1. PR 定位

### 1.1 唯一目标

在已持久化的 LLM、工具、审批和成功提交边界注入可复现中断，量化正确操作节点恢复、框架内部事实幂等性，以及外部副作用可能重复的范围。

### 1.2 当前问题

- `RuntimeEngine.resume_run()` 读取同一 Run 的 checkpoint，并只从 `INVOKE_LLM` 或 `EXECUTE_TOOLS` 操作节点恢复；它复用 checkpoint 指向的 ContextVersion（上下文版本）。
- 工具前 checkpoint 已持久化待执行调用，成功提交已有 6 个持久化故障边界；审批有专用恢复入口。已有测试证明若干单点行为，但没有重复实验、分层错误率、恢复时延或原始证据快照。
- LLM 请求已发送但响应未知、以及工具副作用已经发生但进程崩溃时，系统无法可靠承诺外部 exactly-once；这不能与内部状态机和持久化事实正确性混为同一结论。
- 父子 Task 等待映射和结果回灌状态尚未作为跨进程恢复的完整持久化事实；委派等待冷重建不能作为当前正式恢复能力。

### 1.3 完成后的链路

```text
固定业务场景 + 指定故障边界
    → 当前 Runtime 隔离执行与持久化事实
    → 受控异常 / 代表性子进程强制退出
    → 丢弃旧服务对象，以同一存储根冷重建服务
    → resume_run / resolve_approval / success commit recovery
    → 控制状态、内部事实、外部副作用三层判定
    → BenchmarkSample（单次采样记录）JSONL + 恢复快照/报告
```

PR4 不自动恢复委派等待父 Run；该路径只产生能力边界审计结果。

## 2. PR 边界

### 2.1 包含内容

1. 对 LLM 调用前失败、LLM 结果未知、工具副作用前/后中断、审批等待冷重建及成功提交六个边界建立固定故障实验。
2. 每次故障后销毁旧应用服务对象，并从同一隔离存储根创建新服务，以公开恢复入口完成恢复；不以同一个 Engine 内部变量继续。
3. 分别记录控制状态恢复、框架内部事实一致性、外部副作用次数和恢复耗时；每层独立通过/失败。
4. 用一个代表性子进程强制退出场景验证“工具 checkpoint 已保存、外部副作用前”的真实进程边界。
5. 将委派等待冷重建作为预期失败/能力边界审计，保存原因与事实，不计入正式恢复成功率。

### 2.2 明确不包含

- 不承诺 LLM、外部 Tool、网络或任意外部副作用的跨崩溃 exactly-once；
- 不持久化父子 Task 关系、等待映射或结果回灌状态以补齐委派恢复；该能力留给 PR7 之后单独确认；
- 不修改 Runtime 状态机、checkpoint 语义、生产恢复流程或存储格式；
- 不调用真实 API、网络或真实高风险工具；所有副作用使用记录型替身；
- 不将取消、并发隔离、Capability 安全、上下文压缩或一般多 Agent 行为并入本 PR；
- 不把子进程强制退出扩大为所有边界的进程级混沌测试。

## 3. 模块结构

### 3.1 新增文件

```text
benchmarks/
├── recovery_reliability.py          # 恢复套件 CLI、轮次编排与快照写出
├── recovery_faults.py               # 固定故障边界、记录型 LLM/工具与冷重建装配
├── recovery_assertions.py           # 三层恢复判据和事实读取
├── recovery_subprocess.py           # 单一代表性子进程强制退出验证
└── recovery_stats.py                # 成功率、重复次数、恢复时延与报告聚合

tests/benchmarks/
├── test_recovery_faults.py
├── test_recovery_assertions.py
├── test_recovery_subprocess.py
├── test_recovery_stats.py
└── test_recovery_reliability.py
```

运行工件沿用统一快照布局：

```text
benchmarks/baselines/reliability_recovery_v1/
├── <snapshot-id>.json
└── samples/
    └── <snapshot-id>.jsonl

benchmarks/reports/recovery/<run-id>/
├── recovery-report.md
├── capability-boundary.md
├── fault-config.json
└── evidence/                         # 故障前后事实摘要、子进程退出码与记录型副作用日志
```

### 3.2 修改文件

```text
benchmarks/eval_baseline_models.py   # 扩展统一记录的恢复三层判定、故障点与重复副作用字段
benchmarks/eval_baseline_stats.py    # 复用成功率/分位数，按故障点和保证层聚合
benchmarks/README.md                 # 增加 PR4 命令、结论边界及 external exactly-once 限制
tests/benchmarks/test_eval_baseline_models.py
tests/benchmarks/test_eval_baseline_stats.py
```

### 3.3 不新增或修改的内容

- 不新建生产 Fault Port；成功提交仅复用现有测试专用故障注入 Port，其他边界由 Benchmark 隔离替身控制；
- 不建立泛化故障注入框架、任意 hook 注册器或运行时插件机制；
- 不修改 `src/dotclaw/runtime/`、`src/dotclaw/orchestration/`、Task 持久化或历史 worktree；
- 不新建平行恢复记录、快照或报告协议。

## 4. 场景与接口设计

### 4.1 统一入口

```text
python -m benchmarks.recovery_reliability \
  --suite reliability_recovery_v1 \
  --warmup 5 --repeat 30 \
  --process-warmup 5 --process-repeat 50 \
  --output benchmarks/reports/recovery/<run-id> \
  --save-baseline benchmarks/baselines/reliability_recovery_v1
```

- 每轮使用新的存储根、记录型外部替身和初始应用服务；故障后丢弃初始服务，重建 Repository、checkpoint、Context、LLM、工具、审批与协调器对象。
- 无故障参考运行与故障恢复运行使用完全相同的脚本任务、固定 Fake 延迟、配置和环境；参考运行只用于解释恢复额外耗时和调用差异，不替代恢复正确性判据。
- 每次样本写入故障点、重建次数、恢复入口、恢复前/后 Run 与 checkpoint 摘要、事件/消息/ContextVersion 摘要和记录型副作用日志摘要。

### 4.2 三层保证判据

每个正式样本必须分别得出三个结果：

| 保证层 | 判定内容 | 不包含的承诺 |
|---|---|---|
| 控制状态 | 同一 Run、正确 checkpoint action、正确恢复入口、ContextVersion 不漂移、合法终态 | 外部调用恰好一次 |
| 内部事实 | ToolResult、状态迁移、RunEvent、Conversation 投影、checkpoint/成功提交意图最终一致且无重复 | 外部系统没有执行两次 |
| 外部副作用 | 已确认发送/执行数、可能重复数、记录型副作用日志 | 跨崩溃的 exactly-once |

`control_recovery_pass`、`internal_facts_pass` 与 `external_effect_status` 独立序列化；后者只能取“未发生、一次、观察到重复、结果未知、不适用”之一。外部副作用重复不得使已通过的前两层被改写为失败，也不得被隐藏在总恢复成功率中。

### 4.3 LLM 恢复场景

| 场景 | 注入语义 | 控制状态与内部事实判据 | 外部副作用统计 |
|---|---|---|---|
| `llm_before_send_failure` | LLM 在请求发送前明确失败 | 原 Run/ContextVersion/`INVOKE_LLM` 节点恢复，恢复后完成 | 首次发送 0，恢复发送 1，可安全重试 |
| `llm_response_unknown` | 请求已记录为发送，响应返回前中断 | 原 Run/ContextVersion/节点恢复，最终状态和内部消息不重复 | 发送次数及可能重复请求数单独报告，不宣称 exactly-once |

两种场景均不得生成新的初始 ContextVersion。结果未知场景只能表述为“恢复控制状态正确；外部 LLM 请求可能重复”。

### 4.4 工具恢复场景

| 场景 | 注入语义 | 必须证明 | 外部副作用统计 |
|---|---|---|---|
| `tool_before_effect` | `EXECUTE_TOOLS` checkpoint 已落盘、工具副作用前中断 | 同一 Run 从工具节点恢复；不重新规划/重新生成工具调用 | 初始执行 0，恢复执行 1 |
| `tool_after_effect` | 工具已写记录型副作用、返回 ToolResult 前中断 | ToolResult、状态迁移、完成事件、最终 Conversation 各仅一份且最终一致 | 记录总执行数和观察到的重复数；允许报告重复 |

工具后中断的外部重复属于预期可观测风险；若内部 ToolResult、事件或 Conversation 重复，属于框架内部一致性失败。

### 4.5 审批等待冷重建

初始运行进入审批挂起并持久化审批记录、checkpoint 与活动 ContextVersion 后，销毁旧服务并重建；再经公开 `resolve_approval()` 入口批准。

一轮通过须证明：同一 Run 恢复；不创建第二个审批记录、不再次请求审批；ContextVersion 不漂移；获批后工具实际执行一次；最终 Run 与 Conversation 收口一次。审批拒绝路径不作为本 PR 主量化场景，已由 PR1 业务 Eval 覆盖。

### 4.6 成功提交恢复

复用现有成功提交故障点枚举的全部 6 个边界：Session 投影前/后、完成事件前/后、Run 收口前/后。每个边界各重复 30 次。

每轮中断后调用持久化层的成功提交恢复，并重复调用一次以验证幂等性。必须最终得到：一份 Conversation 投影、一个完成事件、一个 `COMPLETED` Run；成功提交意图和 checkpoint 均被清理。该场景没有外部副作用，外部层记为“不适用”。

### 4.7 委派等待冷重建能力边界

构造父 Run 已等待子 Run 的事实后冷重建，再尝试结果回灌。该场景固定为能力边界审计：记录父子关系、等待映射和回灌状态在重建后是否可用，以及失败类型/证据。

它不得进入控制状态恢复率、内部事实一致率或简历中的“委派可恢复”结论。只有未来实际持久化父子 Run 关系、等待映射和回灌状态，并由专门计划验收后，才可升级为正式场景。

### 4.8 代表性子进程强制退出

仅对 `tool_before_effect` 执行子进程验证：子进程在 checkpoint 已落盘、工具记录型副作用前以强制退出结束；父进程使用同一存储根新建服务并恢复。

该验证与受控异常使用相同判据和记录格式，额外保存子进程命令、退出码、源码提交、环境摘要和持久化文件摘要。正式执行 50 次；它验证该一个关键边界跨真实进程仍可恢复，不泛化为所有节点均已通过 OS 级中断验证。

## 5. 数据模型与统计口径

PR4 继续使用 `BenchmarkSample`（单次采样记录）和 `BenchmarkSnapshot`（汇总快照），二者是 Benchmark 派生读模型，不是 Runtime 权威事实。

新增恢复字段：

| 字段组 | 字段 | 说明 |
|---|---|---|
| 故障 | fault_scenario、fault_point、fault_mechanism、restart_kind、rebuild_count | 固定中断条件与冷重建证据 |
| 控制状态 | checkpoint_action_before、checkpoint_action_resumed、same_run_id、same_context_version、control_recovery_pass | 正确节点恢复证据 |
| 内部事实 | tool_result_count、state_transition_count、completed_event_count、conversation_projection_count、checkpoint_cleaned、success_intent_cleaned、internal_facts_pass | 幂等与最终一致性 |
| 外部副作用 | llm_request_sent_count、tool_effect_count、external_duplicate_count、external_effect_status | 不将未知或重复缩写为 0 |
| 时延 | fault_to_restart_ms、restart_to_terminal_ms、recovery_wall_duration_ms | 恢复成本；缺失为 `null` |
| 边界 | capability_status、capability_reason | 委派冷重建等非正式能力的审计结论 |

每个正式故障点报告样本数、控制状态成功数/总数、内部事实成功数/总数、Wilson 95% 区间、绝对错误数、恢复 P50/P95、外部副作用次数分布和重复数。成功提交按 6 个边界分别报告，不只给合并均值。

## 6. 行为与一致性边界

- “恢复成功”在 README/简历中必须明确属于控制状态或内部事实层；不能暗示外部调用 exactly-once。
- LLM 结果未知和工具执行后中断的外部重复是需要统计的风险，而非自动测试失败；但内部事实重复必定失败。
- 冷重建仅证明同机、同存储格式、相同代码提交下的恢复；不承诺跨版本数据迁移或分布式故障恢复。
- 子进程强制退出的结论只覆盖工具副作用前的一个 checkpoint 边界；其余边界使用受控异常验证。
- 委派冷重建为已知能力边界；即使某次恰好不报错，也不升级为可靠性结论，除非满足后续持久化设计和验收条件。
- 正式普通恢复场景/边界执行 30 次，成功提交为 6×30；工具前 checkpoint 的跨进程强退执行 50 次，预热结果不参与正式统计。

## 7. 必要的现有代码修改

仅扩展 PR1 的 Benchmark 派生记录、统计与报告字段，使三层恢复结论、故障点和外部重复可被统一保存和聚合。

复用现有成功提交测试专用故障注入 Port；其他中断由 Benchmark 的记录型替身或隔离子进程实现。不修改 Runtime 生产 checkpoint、状态机、恢复或外部 Port 协议。若实验发现现有运行事实无法判断某个内部重复，先写失败的事实读取测试，再单独确认最小生产读取接口变更。

## 8. 测试计划

### 8.1 正常路径

- 两类 LLM 故障分别得到正确 ContextVersion/操作节点恢复与发送次数分类；
- 工具前/后中断分别验证工具节点重放和内部事实单次提交；
- 审批等待冷重建不产生第二次审批、批准后工具只执行一次；
- 六个成功提交边界均经两次恢复调用后收敛为唯一 Conversation、事件和终态；
- 子进程强退后工具前节点可由父进程恢复；
- 委派冷重建审计能稳定输出能力边界，而非混入通过率。

### 8.2 边界路径

- 缺 checkpoint、无活动 ContextVersion、非法 checkpoint action、丢失输入消息或非法终态时拒绝恢复并明确归因；
- 无故障参考与故障样本的配置、Fake 延迟或源码提交不一致时，拒绝计算恢复成本差异；
- 中断未触发、恢复入口重复调用、ContextVersion 漂移、Run ID 改变或同 Session 留有非终态占用均视为失败；
- 子进程非预期退出码、超时、存储根污染或恢复后仍有锁占用时明确失败。

### 8.3 数据损坏

- Run、checkpoint、消息、事件、ContextVersion、审批记录、成功提交意图或记录型副作用日志缺失必填字段/类型错误时，不能映射为恢复成功；
- 事件序列重复、Conversation 投影重复、ToolResult 重复或 checkpoint/意图未清理时，内部事实层失败；
- JSONL 中未知故障点、非法外部副作用状态或跨样本证据路径时拒绝聚合。

### 8.4 历史兼容

PR4 不读取历史 Git Runtime。它验证 PR1/PR3 样本缺少恢复字段时按 schema 版本明确处理，不将缺失解释为“未重复”或“恢复成功”。

### 8.5 回归测试

- `tests/runtime_v2/test_recovery_boundary.py`、`test_e4_runtime_safety.py`、`test_e5_success_commit_recovery.py`、审批/委派相关现有测试继续通过；
- PR1 至 PR3 的 Benchmark 模型、统计和报告测试继续通过；
- `tests/benchmarks`、完整 pytest、`compileall` 和 `git diff --check` 通过。

## 9. 实施顺序

1. 扩展统一记录/快照的三层恢复字段、schema 规则和聚合测试，明确外部未知/重复不等于内部失败。
2. 实现固定 LLM、工具故障与冷重建装配，先完成两类 LLM、工具前/后中断和无故障参考运行。
3. 接入审批等待冷重建与成功提交六边界，读取并判定内部 Run/事件/Conversation/checkpoint 事实。
4. 实现单一工具前边界的子进程强退验证，以及委派冷重建能力边界审计。
5. 输出 JSONL、快照、三层报告和证据摘要；以开发期采样验证后执行普通故障点每点 30 次、跨进程强退 50 次。
6. 更新 Benchmark README，仅写入实际正式运行得到的恢复率、内部一致性与外部副作用边界。

## 10. PR 验收标准

1. LLM 调用前失败和结果未知明确分开统计，后者不宣称 exactly-once；
2. 工具前中断从 `EXECUTE_TOOLS` 恢复且不重新规划，工具后中断即使外部重复也不产生重复内部事实；
3. 审批等待冷重建不重建审批、不漂移 ContextVersion，批准后原 Run 正确收口；
4. 成功提交 6 个边界各 30 次后均收敛为唯一 Conversation、完成事件、Run 终态且清理控制记录；
5. 工具前 checkpoint 的子进程强退 50 次均可由新进程/服务恢复；
6. 委派冷重建只作为能力边界报告，不进入正式恢复成功率；
7. 每次正式结果可追溯到固定环境、故障配置、原始 JSONL 与前后事实摘要；
8. Runtime 生产状态机、checkpoint、恢复协议与历史数据未被 Benchmark 改写。

## 11. 最终交付结果

PR4 完成后可以得到类似以下、但必须以实际快照为准的分层结果：

```text
控制状态：工具前中断的正确节点恢复率 X/Y。
内部事实：工具后中断后 ToolResult / 完成事件 / Conversation 唯一收口率 X/Y。
外部副作用：工具后中断观测到重复执行 Z/Y；该指标不代表内部恢复失败。
能力边界：委派等待冷重建当前不计入恢复能力。
```

PR4 不证明跨版本恢复、分布式容错、真实 API exactly-once 或委派跨进程恢复；这些需要后续独立能力与实验支撑。
