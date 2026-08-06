# dotClaw Benchmark PR5：Capability 安全链实验开发计划

> 状态：已确认的开发基线。本文定义 PR5 的唯一范围；只量化当前已实现的调用级安全能力，不将未实现的命令内容级策略或文件系统 TOCTOU 防护写成结果。

## 1. PR 定位

### 1.1 唯一目标

以完整有限安全决策表验证工具调用的参数校验、资源解析、策略收敛、审批、Handler 屏障、Agent 策略隔离和审计脱敏，并独立测量固定本地安全链的额外时延。

### 1.2 当前问题

- `ToolExecutor` 已按“入参验证 → Capability Broker → Policy Engine → 审批 → Handler”编排调用链；现有测试分散验证文件、命令、网络、MCP 和路径回填等行为，但没有统一决策矩阵、Handler 阻断统计或可提交快照。
- 当前 `PolicyEngine` 对文件路径、网络静态服务/主机和 MCP server 有资源约束；Agent 规则只能收窄全局规则。`process.exec` 当前只按档案决策，命令文本仅用于脱敏摘要，未提供命令内容级 allow/ask/deny。
- Broker 将已校验的绝对文件路径回填给 Handler，能证明决策资源与执行资源对齐；校验后联接点/符号链接被替换的文件系统竞态不在当前能力内。
- 安全链本地开销尚未在同一 Handler、同一接口、固定环境下与直接 Handler 基线对比。

### 1.3 完成后的链路

```text
安全决策表 Case
    → ToolExecutor.execute / execute_approved
    → 参数校验 → Capability Broker → Policy Engine → 审批 → Handler
    → 结果、阶段计数、审计/审批摘要与路径回填事实
    → BenchmarkSample（单次采样记录）JSONL + 安全快照/报告

性能 Case
    → 直接无副作用 Handler / 同接口完整安全链
    → validated invocation 至 Handler entry 计时
    → P50/P95 对照报告
```

## 2. PR 边界

### 2.1 包含内容

1. 将当前工具安全行为固化为完整有限决策表，覆盖参数校验、文件、进程档案、网络、MCP、审批、Agent 规则、审计脱敏和路径回填。
2. 为每个 Case 记录期望决策、实际结果、Broker/Policy/Approval/Handler 进入次数、资源摘要、路径对齐和脱敏检查结果。
3. 对参数校验失败、Policy deny、审批拒绝/无 Channel 等阻断分支分别统计 Handler 进入次数。
4. 以相同无副作用 Handler 与调用接口，对比直接 Handler 和完整安全链从已验证调用到 Handler entry 的 P50/P95 开销。
5. 保存矩阵版本、环境、原始记录、性能采样、统计脚本和结论边界。

### 2.2 明确不包含

- 不实现命令文本、二进制、shell 参数或命令组合级 allow/ask/deny；`process.exec` 仍是档案级治理；
- 不宣称消除校验后路径被联接点/符号链接替换的 TOCTOU；
- 不调用真实命令、网络、MCP server 或真实文件副作用；全部使用记录型无副作用 Handler；
- 不改变 `ToolExecutor`、`CapabilityBroker`、`PolicyEngine`、审批协议或生产默认策略；
- 不将安全矩阵纳入 Runtime 恢复、并发、ContextVersion 或多 Agent 委派实验；
- 不以确定性正确性 Case 的机械重复冒充可靠性采样。

## 3. 模块结构

### 3.1 新增文件

```text
benchmarks/
├── capability_reliability.py        # 决策表/性能 CLI、执行编排与快照写出
├── capability_matrix.py             # 完整有限决策表、期望结果与固定安全装配
├── capability_observers.py          # 阶段计数、资源/路径/摘要观察与断言输入
└── capability_stats.py              # 正确性汇总、Handler 屏障与性能分位数报告

tests/benchmarks/
├── test_capability_matrix.py
├── test_capability_observers.py
├── test_capability_stats.py
└── test_capability_reliability.py
```

运行工件沿用统一布局：

```text
benchmarks/baselines/reliability_capability_v1/
├── <snapshot-id>.json
└── samples/
    └── <snapshot-id>.jsonl

benchmarks/reports/capability/<run-id>/
├── security-matrix.md
├── security-chain-overhead.md
└── matrix-config.json
```

### 3.2 修改文件

```text
benchmarks/eval_baseline_models.py   # 扩展统一记录的安全决策、阶段计数和性能字段
benchmarks/eval_baseline_stats.py    # 复用聚合，增加完整矩阵和安全链开销统计
benchmarks/README.md                 # 增加 PR5 命令、指标语义与能力边界
tests/benchmarks/test_eval_baseline_models.py
tests/benchmarks/test_eval_baseline_stats.py
```

### 3.3 不新增或修改的内容

- 不新增生产安全服务、通用安全插件、第二套策略语言或新的 Policy Port；
- 不修改内置工具注册、默认规则、网络服务配置、MCP 配置或 Runtime Tool 适配器；
- 不在 Benchmark 中创建可执行 shell 命令、网络请求或真实敏感文件；
- 不新建平行记录、快照、报告格式。

## 4. 决策表与接口设计

### 4.1 统一入口

```text
python -m benchmarks.capability_reliability \
  --suite reliability_capability_v1 \
  --matrix benchmarks/datasets/reliability_capability_v1/matrix.json \
  --performance-warmup 5 --performance-repeat 30 \
  --output benchmarks/reports/capability/<run-id> \
  --save-baseline benchmarks/baselines/reliability_capability_v1
```

- 正确性矩阵逐行执行一次；它是经过审核的完整有限决策表，不以重复次数替代覆盖。
- 性能仅执行无副作用 allow Case，使用 `warmup=5, repeat=30`；预热不进入正式统计。
- 每次运行使用临时 workspace、固定策略作用域、固定审批响应和记录型 Handler；配置、Python、平台、提交和矩阵内容摘要写入记录。

### 4.2 完整有限安全决策表

矩阵以稳定 `case_id`、调用入口、工具档案、参数、策略/Agent 规则、审批响应和期望事实定义。实际矩阵行数 `X` 以 Git 跟踪 JSON 为准，报告必须列出 `X/X`，不得用文档中的预估数量代替。

| 决策族 | 至少覆盖的 Case | 主要判据 |
|---|---|---|
| 参数校验 | 文件、命令、网络、MCP 的非法参数 | `INVALID_ARGUMENTS`；Broker、Policy、审批、Handler 进入均为 0 |
| 文件资源 | 工作区内读 allow；写 ask 的批准/拒绝/无 Channel；`..`、绝对路径、`.env`、`*.key`、Windows 联接点逃逸 | 决策符合预期；deny/未批准 Handler 为 0；实际 Handler 路径等于 Broker 已校验路径 |
| 进程档案 | ask 批准、ask 无 Channel、策略 deny、预批准仍受 deny、含密钥命令摘要 | Handler 屏障；审计/审批摘要不含密钥；只按 `process.exec` 档案得出结论 |
| 网络资源 | 服务未启用、启用且主机匹配、主机错配、Agent 收窄 ask/deny、恶意 URL 参数 | fail-closed；Agent URL 不改变静态声明目标；拒绝时 Handler 为 0 |
| MCP | allowlisted server、未授权 server、空白名单、Agent 收窄 | server 白名单 fail-closed；拒绝时 Handler 为 0 |
| Agent 隔离 | Agent A 收窄 deny、Agent B 保持 allow、无 Agent 规则回退全局 | 决策仅受当前 Agent 规则影响，不发生规则泄漏 |
| 审计与审批 | ask 批准、拒绝、无 Channel、敏感参数 | 批准才进入 Handler；摘要不泄露敏感字段 |

Windows 联接点 Case 仅在 Windows 且可由当前用户建立联接点时执行；不能建立时报告为环境跳过，而不是安全通过。矩阵完整性只在该环境的适用 Case 集内计算，报告须给出跳过原因与数量。

### 4.3 Handler 屏障与路径对齐

Benchmark 在不改变生产链路的前提下装配计数型 Broker、Policy、审批和无副作用 Handler，记录每一层实际进入次数。

- 参数校验失败：Broker、Policy、审批、Handler 均为 0；
- Policy deny：Handler 为 0；
- ask 被拒绝或无 Channel：Handler 为 0；
- allow 或 ask 已批准：Handler 恰好为 1；
- 文件 allow/approved Case：Handler 收到的绝对路径与 Broker 的 `absolute_path` 一致。

路径一致性仅证明策略决策资源与实际执行资源对齐；不保证校验完成后文件系统对象不被替换，也不构成 TOCTOU 防护承诺。

### 4.4 性能基线与计时范围

性能使用相同的已验证参数、同一个无副作用 Handler、同一调用接口、同一临时 workspace 和同一进程环境，对比两种模式：

1. **直接 Handler 基线**：直接调用 Handler，使用已验证参数和与完整链相同的执行上下文；
2. **完整安全链**：从 `validated invocation` 开始，经 Broker、Policy、审批已预批准分支，到调用 Handler 的入口为止。

计时终点是 Handler `execute()` 首次进入，故不包含 Handler 业务执行、真实 I/O、网络、LLM、日志写入或用户审批等待。每条性能记录保存模式、预热标识、时间、环境和 Handler 标识；报告给出两侧样本数、P50/P95、最大值和“完整链 - 直接 Handler”的额外时延。

## 5. 数据模型与统计口径

PR5 继续使用 `BenchmarkSample`（单次采样记录）和 `BenchmarkSnapshot`（汇总快照），它们是 Benchmark 派生读模型，不是工具审计或 Runtime 权威事实。

新增安全字段：

| 字段组 | 字段 | 说明 |
|---|---|---|
| 决策 | matrix_case_id、expected_decision、actual_decision、actual_error_code、decision_pass | 完整有限表逐行判定 |
| 链路 | validation_entered、broker_entered、policy_entered、approval_entered、handler_entered | 阶段屏障的实际计数 |
| 资源 | resource_kind、policy_profile、matched_rule、resolved_path_match、network_service、network_host、mcp_server | 只保存脱敏/安全标识 |
| 审计 | journal_summary_redacted、approval_summary_redacted、sensitive_leak_count | 不保存密钥原文 |
| 隔离 | agent_id、agent_rule_source、agent_policy_isolated | Agent 规则收窄与泄漏证据 |
| 性能 | measurement_mode、pre_handler_duration_ms、is_warmup | 仅用于直接 Handler/完整链对照 |

正确性报告至少列出：适用矩阵行数、通过/失败/跳过数、策略判定 `X/X`、参数校验失败/Policy deny/未获审批调用的 Handler 进入次数、敏感字段泄露数。性能报告独立列出两种模式的 P50/P95，不把安全矩阵单次时延混入性能结论。

## 6. 行为与一致性边界

- 安全正确性结论仅覆盖 Git 跟踪的完整有限矩阵与其固定策略/替身环境；它不代表任意用户配置或未枚举工具。
- Handler 进入次数是执行屏障事实；`0` 只对明确阻断 Case 有意义，不将不存在的 Handler 或测试装配失败计为安全阻断。
- 网络目标由 ToolDefinition 静态声明和启用服务白名单决定；“恶意 URL 参数不改变目标”不等同于开放任意 URL 访问的安全性。
- `process.exec` 当前只支持档案级策略；报告不得出现“命令内容级拦截率”或等价措辞。
- 性能结论仅是本地、无副作用、预验证调用的安全链前置开销，不能外推为真实 Tool、网络或 API 性能。
- 所有摘要断言只检查已定义的敏感测试标记；不将“测试未覆盖的秘密格式”表述为通用零泄露保证。

## 7. 必要的现有代码修改

仅扩展 Benchmark 的派生记录、统计和报告，以保存安全决策与阶段观察。原因是 PR5 必须使用 PR1 建立的统一原始记录/快照，而不应把 Benchmark 计数或计时写入生产工具链。

`ToolExecutor`、Broker、Policy、审批和 Handler 不改动。若现有公开装配方式无法观察某个阶段，则先编写失败的 Benchmark 集成测试；只有事实读取确实缺失时，才单独讨论最小观察接口，不能以 Benchmark 名义重构安全链。

## 8. 测试计划

### 8.1 正常路径

- 每个适用矩阵行得到符合预期的决策、错误码和阶段计数；
- allow 和审批批准调用恰好进入 Handler 一次，阻断调用不进入；
- 文件路径回填与 Broker 校验目标一致；
- 网络/MCP 白名单、Agent 收窄与静态网络主机按矩阵正确生效；
- 直接 Handler 与完整链按固定计时范围输出可比较样本。

### 8.2 边界路径

- 空矩阵、重复 case_id、未知工具/档案/决策、无期望结果、非法审批响应或非法性能参数明确失败；
- 仅 Windows 联接点 Case 不适用时记录跳过；其他平台不因跳过被记为通过；
- `execute_approved()` 只能跳过 ask，不能绕过 deny；
- 两种性能模式的 Handler、参数、执行上下文、环境或计时范围不一致时拒绝比较。

### 8.3 数据损坏

- 矩阵 JSON、记录、快照中缺失/错误类型的决策、阶段计数、路径摘要或 schema 版本明确失败；
- 摘要含测试敏感标记、Handler 计数与结果矛盾、路径不一致或 Agent 规则泄漏均使相应 Case 失败；
- 原始记录路径越出输出目录、warmup 混入性能正式统计或配置摘要不匹配时拒绝报告。

### 8.4 历史兼容

PR5 不读取历史 Git Tool 记录。它验证 PR1 至 PR4 样本缺少安全字段时按 schema 版本明确处理，不将缺失计数解释为 Handler 未进入或无泄露。

### 8.5 回归测试

- `tests/tools` 中 Capability、Policy、Executor 安全、审批、网络、MCP、路径回填和 Agent 规则隔离测试继续通过；
- PR1 至 PR4 Benchmark 模型、统计和报告测试继续通过；
- `tests/benchmarks`、完整 pytest、`compileall` 和 `git diff --check` 通过。

## 9. 实施顺序

1. 定义并审查 Git 跟踪的有限安全决策矩阵，建立其 schema、完整性和预期结果测试。
2. 实现固定安全装配、阶段观察、路径/摘要事实读取，先完成矩阵正确性与 Handler 屏障断言。
3. 实现 Agent 策略隔离、Windows 联接点适用性和环境跳过报告。
4. 实现相同 Handler/接口下的直接基线与完整链 Handler-entry 计时，补齐可比性校验和分位数报告。
5. 写出 JSONL、快照和两份 Markdown 报告；执行完整矩阵与正式性能采样。
6. 更新 Benchmark README，仅写入实际矩阵行数、正式结果和明确能力边界。

## 10. PR 验收标准

1. Git 跟踪的完整有限安全矩阵逐行可执行，报告实际适用行数及 `X/X` 策略判定结果；
2. 所有参数校验失败、Policy deny 和未获审批调用进入 Handler 的次数均为 0；
3. 文件 Handler 的实际路径与 Broker 校验资源一致，但报告不宣称 TOCTOU 已解决；
4. 网络与 MCP 的 fail-closed、Agent 策略收窄及审计/审批摘要脱敏均有原始证据；
5. 进程命令的结论明确限制为档案级治理，不生成命令内容级安全结论；
6. 直接 Handler 与完整链以同一接口/Handler、固定计时范围、预热/样本和环境生成 P50/P95；
7. 快照、JSONL、矩阵配置和报告可追溯且不覆盖既有基线；
8. 生产工具安全链未因 Benchmark 修改。

## 11. 最终交付结果

PR5 完成后，可基于正式快照写出两类结果：

```text
正确性：在包含 X 行的完整有限安全决策矩阵中，策略判定 X/X 符合预期；
所有参数校验失败、Policy deny 和未获审批调用进入 Handler 的次数均为 0，
审计及审批摘要中的敏感字段泄露为 0。

性能：相对直接 Handler，固定本地安全链增加的 P50/P95 延迟为 X/Y ms。
```

其中 X/Y 必须由正式运行替换；PR5 不证明命令内容级治理、TOCTOU 防护、真实外部 Tool 安全性或跨崩溃外部副作用控制。
