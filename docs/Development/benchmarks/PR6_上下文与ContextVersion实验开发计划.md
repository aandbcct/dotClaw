# dotClaw Benchmark PR6：上下文与 ContextVersion 实验开发计划

> 状态：已确认的开发基线。本文定义 PR6 的唯一范围；只验证当前已实现的版本化上下文、Session 历史压缩与 Owner 隔离，不把摘要质量、运行中闭合片段压缩、TOCTOU 或强制重建对照写成生产能力。

## 1. PR 定位

### 1.1 唯一目标

量化 ContextVersion（版本化上下文）在固定输入下的一致性、冷恢复时的抗漂移与复用收益、Session 历史压缩的预算效果，以及 GLOBAL/AGENT/SESSION/RUN 四层 Owner 的内容隔离。

### 1.2 当前问题

- 当前 `ContextProvider` 在 `replay_active_context`（复用活动快照标记）为真时，直接从活动 ContextVersion 的持久化 Slot 重建输入，而不重新加载外部 Slot；`resume_run()`、审批恢复和委派恢复均装配该标记。
- 普通 Run 的上下文构建会将 SNAPSHOT Slot、内容哈希和工具 Schema 哈希持久化为 ContextVersion；同一 Run 的 ReAct 后续轮次在内容未变化时复用同一版本。
- 当前实现仅在上下文预算超限时压缩最旧完整 Session Conversation，候选摘要只在成功提交时投影到 Session；失败、取消、放弃不应污染后续请求。已有测试覆盖单点行为，但没有可复跑的 token、预算通过率、压缩成本和污染率结果。
- GLOBAL/AGENT/SESSION/RUN Owner 已有分层与缓存边界，但尚未以统一场景和 Provider 加载次数量化内容隔离或谨慎的复用效率。

### 1.3 完成后的链路

```text
固定 Context 场景 / 冷恢复场景 / 历史压缩语料
    → ContextProvider + RuntimeEngine 隔离执行
    → ContextVersion / RunMessage / RunEvent / Session 历史压缩事实
    → 内容哈希、Slot/消息序列、Provider 加载次数、Token 与预算结果
    → BenchmarkSample（单次采样记录）JSONL + Context 快照/报告
```

强制重新加载外部 Slot 只作为 Benchmark 反事实对照，不进入生产 Runtime 入口。

## 2. PR 边界

### 2.1 包含内容

1. 以完整有限场景表验证固定输入下的 ContextVersion 内容、工具 Schema、规范化 Slot、Slot 顺序和最终消息序列一致性。
2. 在外部 Slot 由 `v1` 变为 `v2` 后冷重建恢复，验证复用原 ContextVersion、外部 Provider 不重新加载且不新增版本。
3. 在外部输入不变时，以当前快照复用和 Benchmark 强制重建对照测量恢复阶段耗时、外部加载次数和 ContextVersion 新增数。
4. 对当前 Session 历史压缩量化 token 前后值、缩减率、预算通过率、最近 Conversation 保留、完整 Tool Call/Result 边界和压缩耗时。
5. 以完整有限 Owner 场景验证 GLOBAL、AGENT、SESSION、RUN 的内容归属与隔离；仅在记录缓存命中/Provider 加载数时报告复用效率。
6. 对成功、失败、取消和放弃 Run 的历史压缩候选投影/污染边界各执行受控重复实验。

### 2.2 明确不包含

- 不评估历史压缩摘要质量、事实正确性、回答质量或真实模型效果；
- 不测试或宣称尚未实现的运行中闭合执行片段压缩；
- 不修改 ContextProvider、ContextSlotManager、RuntimeEngine、ContextVersion schema、生产缓存策略或恢复协议；
- 不将强制重建外部 Slot 作为生产恢复模式、历史版本或正常 Runtime 基线；
- 不评估校验后文件系统 TOCTOU、真实检索服务、真实 LLM/API、网络、MCP 或跨进程缓存共享；
- 不将 PR3 并发隔离、PR4 操作节点恢复、PR5 工具安全或 PR7 委派业务场景并入本 PR。

## 3. 模块结构

### 3.1 新增文件

```text
benchmarks/
├── context_reliability.py           # Context 套件 CLI、场景编排与快照写出
├── context_workloads.py             # 固定 Slot/语料/预算场景与冷重建装配
├── context_assertions.py            # 哈希、Slot、消息、污染与 Owner 归属判定
├── context_controls.py              # Benchmark 强制重建对照与 Provider 加载观察
└── context_stats.py                 # token、预算、加载次数、恢复/压缩时延聚合

tests/benchmarks/
├── test_context_workloads.py
├── test_context_assertions.py
├── test_context_controls.py
├── test_context_stats.py
└── test_context_reliability.py
```

运行工件沿用统一布局：

```text
benchmarks/baselines/reliability_context_v1/
├── <snapshot-id>.json
└── samples/
    └── <snapshot-id>.jsonl

benchmarks/reports/context/<run-id>/
├── consistency.md
├── recovery-replay.md
├── compression.md
├── owner-isolation.md
└── context-config.json
```

### 3.2 修改文件

```text
benchmarks/eval_baseline_models.py   # 扩展统一记录的 Context/Token/Provider 加载与污染字段
benchmarks/eval_baseline_stats.py    # 复用聚合，支持 Context 场景和反事实对照统计
benchmarks/README.md                 # 增加 PR6 命令、指标和能力边界
tests/benchmarks/test_eval_baseline_models.py
tests/benchmarks/test_eval_baseline_stats.py
```

### 3.3 不新增或修改的内容

- 不新增生产 ContextPort、ContextVersion、缓存层、摘要存储或 Context 迁移机制；
- 不在 `src/dotclaw/context/` 添加 Benchmark 标识、计时或强制刷新开关；
- 不把 Context 场景伪装为 PR1 的单 Run Eval Dataset，也不新建平行记录/快照协议；
- 不修改 Session 用户数据或提交真实压缩摘要；所有实验在临时存储根中执行。

## 4. 场景与接口设计

### 4.1 统一入口

```text
python -m benchmarks.context_reliability \
  --suite reliability_context_v1 \
  --compression-tokenizer cl100k_base \
  --recovery-warmup 5 --recovery-repeat 30 \
  --performance-warmup 5 --performance-repeat 30 \
  --output benchmarks/reports/context/<run-id> \
  --save-baseline benchmarks/baselines/reliability_context_v1
```

- 场景、语料、固定 Slot 内容、预算窗口、tokenizer、外部 Provider 延迟和对照模式均写入 `context-config.json` 与单次记录。
- 所有场景使用独立临时 Session/Run 存储根；无故障、复用和强制重建对照不能共享持久化状态。
- 完整有限一致性/Owner 表逐行执行；冷恢复、成功/失败/取消/放弃污染边界各重复 30 次；性能对照预热 5、正式 30 次。

### 4.2 固定输入一致性

在固定 Agent、Session、Run 输入、工具快照、检索替身和 Slot 配置下构建 ContextVersion。每个固定场景比较：

- `content_hash`、`tool_schema_hash`；
- 每个 Slot 的规范化内容哈希、Owner、状态、持久化模式与注入顺序；
- 最终发送给 LLM 的消息角色/正文/工具 Schema 序列；
- 版本数量和活动版本引用。

不比较 `created_at`，也不要求不同 Run 的 ContextVersion 标识相同。该场景是构建确定性的基础不变量，用于排除 Slot 顺序、缓存和消息拼装造成的非恢复性漂移；不单独作为简历中的性能或业务主结论。

### 4.3 冷恢复抗漂移

首次运行以外部 Slot 内容 `v1` 生成并持久化 ContextVersion 后，在可恢复节点中断。中断后将同一外部来源改为 `v2`，丢弃旧服务对象，从相同存储根冷重建并执行公开恢复入口。

一轮通过必须满足：

1. 同一 Run 和原活动 ContextVersion 被恢复；
2. 恢复输入仍包含 `v1`，不含 `v2`；
3. ContextVersion 总数不增加，内容/工具 Schema 哈希与 Slot 摘要不变；
4. 外部 Slot Provider 的重新加载次数为 0；
5. Run 最终按预期收口，且恢复期不产生重复 Conversation 或运行事实。

该场景正式重复 30 次，分别统计上下文漂移、Provider 重载、重复 ContextVersion 和内部事实错误数。

### 4.4 快照复用效率对照

此对照仅在外部 Slot 内容保持不变时执行，保证两侧输入语义相同：

- **当前复用模式**：正常 `replay_active_context`，直接从持久化活动 Slot 重建；
- **强制重建模式**：强制重建 ContextPort（测试对照适配器）忽略回放标记，重新加载所有外部 Slot 并再构造输入。

两侧使用同一固定外部 Slot 加载延迟、同一 Run 输入、同一 ContextVersion 初始事实和同一恢复节点；计时范围为恢复入口开始至完成恢复阶段的首个 LLM 调用前。记录恢复阶段 P50/P95、外部来源加载次数和新增 ContextVersion 数。

强制重建模式是“若不复用快照”的 Benchmark 反事实，不能表述为生产路径或历史版本。`v1 → v2` 抗漂移场景可额外观察强制重建导致的内容/版本变化，但不与效率样本混合。

### 4.5 Session 历史压缩

使用 Git 跟踪的固定 Conversation 语料、固定 tokenizer 和固定预算窗口构成压缩矩阵；每行记录未压缩输入 token、压缩后真实输入 token、缩减率、预算是否通过、覆盖至哪个 Conversation、保留的最近 Conversation 数及压缩耗时。

压缩场景必须验证：

- 只选择最旧完整 Conversation，不截断 Tool Call/Tool Result 配对；
- 压缩后的真实输入重新计数后才判定预算通过；
- 成功 Run 只提交最新 staged 候选摘要，下一次请求注入摘要和未覆盖的最近原文；
- 失败、取消、放弃 Run 不得向 Session 投影候选摘要或改变下一次请求内容；
- ContextVersion 保存候选摘要正文，Run 控制事实只保存摘要/来源哈希和版本引用。

“预算通过率”由固定语料矩阵中压缩关闭的基线与当前压缩路径分别计算；token 缩减和压缩耗时只代表固定语料/本地 tokenizer/确定性压缩替身下的输入预算与编排成本，不评估摘要质量或真实回答质量。

### 4.6 Owner 分层隔离

完整有限 Owner 场景至少验证：

| Owner | 场景与判据 |
|---|---|
| GLOBAL | 固定全局目录信息可在不同 Run 保持一致，且不含 Session 私有标识；仅记录缓存命中或 Provider 加载次数后才可报告复用效率 |
| AGENT | 不同 Agent 的身份、工具、技能 Slot 不串用；同 Agent 的稳定 Slot 可按事实复用 |
| SESSION | 同 Agent 的不同 Session 历史、摘要和用户资料互不污染 |
| RUN | 检索结果、运行消息、临时 ContextVersion 只属于当前 Run，不泄漏给其他 Run |

每个 Case 以唯一标识嵌入对应 Owner 内容，读取 ContextVersion/最终消息后检查允许标识与禁止的外部标识。Provider/缓存观察只用于解释已实现的复用，不将未记录的缓存行为推断为效率提升。

## 5. 数据模型与统计口径

PR6 继续使用 `BenchmarkSample`（单次采样记录）和 `BenchmarkSnapshot`（汇总快照），它们是 Benchmark 派生读模型，不是 ContextVersion、Session 或恢复控制事实。

新增 Context 字段：

| 字段组 | 字段 | 说明 |
|---|---|---|
| 一致性 | content_hash、tool_schema_hash、normalized_slot_hashes、slot_order_match、message_sequence_match | 不含 `created_at` 比较 |
| 恢复 | replay_mode、same_context_version、context_version_count_delta、context_drift_count、provider_reload_count、recovery_stage_duration_ms | 冷恢复与反事实对照 |
| 压缩 | tokens_before、tokens_after、token_reduction_ratio、budget_passed、retained_conversation_count、covered_through_id、compression_duration_ms | 固定语料/Tokenizer 结果 |
| 边界 | tool_pair_break_count、session_projection_count、session_pollution_count、run_outcome | 压缩/终态隔离事实 |
| Owner | owner_case_id、global_leak_count、agent_leak_count、session_leak_count、run_leak_count、provider_load_count、cache_hit_count | 内容归属及可观测复用 |
| 证据 | ContextVersion/Run/Session/事件/Provider 计数摘要 | 不保存完整敏感检索正文 |

报告分别呈现：完整有限一致性/Owner 表的通过数；30 次冷恢复的漂移/重载/重复版本绝对错误数与 Wilson 区间；压缩关闭/开启的 token、预算通过率和耗时；复用/强制重建对照的 P50/P95。只有两侧固定环境、输入、延迟与计时范围一致时才计算百分比变化。

## 6. 行为与一致性边界

- ContextVersion 一致性比较的是实际内容与结构，不比较生成时间或跨 Run 的版本号；
- 抗漂移只证明持久化快照恢复时不重新读取外部 Slot，不承诺普通新 Run 忽略最新外部数据；
- 强制重建是 Benchmark 反事实，不是 Runtime 支持的恢复选项，也不用于生产结论；
- 历史压缩仅覆盖 Session Conversation 的最旧完整批次；不评估摘要语义质量、不覆盖运行中闭合片段；
- 成功/失败/取消/放弃污染结论只针对临时固定语料和当前 Session 投影语义；
- Owner 复用效率必须以 `provider_load_count` 或 `cache_hit_count` 为证据；仅有内容一致性时只能报告隔离正确性；
- Token/预算指标依赖固定 tokenizer、语料与窗口，不能外推为真实模型上下文成本节省。

## 7. 必要的现有代码修改

仅扩展 Benchmark 派生记录、统计与报告，以保存 Context 结构摘要、Provider 加载、压缩预算和污染观察。

不修改 Context/Runtime 生产代码。强制重建模式在 Benchmark 测试装配中实现，不能向生产 `ContextProvider` 加开关；若现有持久化事实不足以判断投影/污染，先增加失败的 Benchmark 读取测试，再单独确认最小读取接口调整。

## 8. 测试计划

### 8.1 正常路径

- 固定输入场景的内容/工具 Schema/Slot/消息序列完全一致；
- `v1 → v2` 冷恢复仍使用 v1、Provider 重载 0、版本增量 0；
- 当前复用与强制重建在相同输入下产生可比恢复耗时与加载计数；
- 压缩成功后仅投影最新摘要，下一请求正确注入摘要与最近原文；
- 四层 Owner 内容按允许/禁止标识完全隔离。

### 8.2 边界路径

- ContextVersion 不存在、活动版本不匹配、Slot 顺序/哈希/消息序列变化、Provider 在回放期被调用均明确失败；
- 语料为空、没有可压缩完整 Conversation、压缩后仍超限、tokenizer 不可用或预算窗口非法时明确归因；
- 压缩关闭/开启、复用/强制重建的输入、延迟、tokenizer、窗口或计时范围不一致时拒绝比较；
- 失败、取消、放弃后出现 Session 摘要、版本外泄或下一请求内容变化时计为污染。

### 8.3 数据损坏

- ContextVersion、Slot、Conversation、候选摘要、Provider 观察或 token 记录缺失字段/类型错误/未知 schema 时拒绝聚合；
- 工具调用/结果被拆分跨压缩边界、候选哈希/版本引用不一致、Session 投影重复或路径越界时明确失败；
- JSONL 混入 warmup、反事实与生产模式混合、或证据摘要与快照不匹配时拒绝正式报告。

### 8.4 历史兼容

PR6 不读取历史 Git Context。它验证 PR1 至 PR5 样本缺少 Context 字段时按 schema 版本明确处理，不把缺失值解释为内容一致、零加载或零污染。

### 8.5 回归测试

- `tests/runtime_v2/test_context_*`、`test_history_compression_session_loop.py`、恢复与成功提交相关测试继续通过；
- Eval Context retention scorer、PR1 至 PR5 Benchmark 模型/统计/报告测试继续通过；
- `tests/benchmarks`、完整 pytest、`compileall` 和 `git diff --check` 通过。

## 9. 实施顺序

1. 扩展统一记录/快照的 Context、Provider、token、污染字段和严格 schema 测试。
2. 实现固定 Slot/消息/工具快照工作负载与一致性、Owner 归属断言；固化有限场景表。
3. 实现 `v1 → v2` 冷恢复、服务冷重建和 Provider 加载观察；先完成 30 次抗漂移场景。
4. 实现仅限 Benchmark 的强制重建对照，固定加载延迟与计时范围，补齐可比性校验。
5. 实现固定语料/tokenizer/窗口的压缩关闭/开启矩阵及成功/失败/取消/放弃污染边界。
6. 输出 JSONL、快照和四类报告，执行正式采样；更新 README，仅写入实际结果与能力边界。

## 10. PR 验收标准

1. 固定输入完整表中内容哈希、工具 Schema、Slot 规范化哈希/顺序和消息序列均符合预期，且不比较 `created_at`；
2. 30 次 `v1 → v2` 冷恢复中，上下文漂移、Provider 重载和重复 ContextVersion 均可统计并有原始证据；
3. 快照复用与强制重建对照在相同输入/环境/计时范围下报告恢复 P50/P95、Provider 加载和版本新增数，并明确其反事实性质；
4. 固定语料压缩报告 token 缩减、预算通过率、最近 Conversation 保留、完整 Tool Call/Result 边界和压缩 P50/P95；
5. 成功 Run 才投影摘要；失败、取消、放弃各 30 次对后续 Session 的污染为可审计的绝对错误数；
6. GLOBAL/AGENT/SESSION/RUN 的上下文泄漏分别可统计，复用效率仅在 Provider/缓存观察存在时报告；
7. 未宣称摘要质量、运行中片段压缩、TOCTOU、真实 API 或强制重建生产能力；
8. Context 与 Runtime 生产语义未因 Benchmark 改写。

## 11. 最终交付结果

PR6 完成后，可基于正式快照写出以下类型结论：

```text
N 次冷恢复中，上下文漂移、外部来源重新加载和重复创建 ContextVersion 均为 0。
历史压缩使固定语料输入 Token 降低 X%，预算通过率由 A% 提升至 B%。
相对 Benchmark 强制重建对照，ContextVersion 复用使恢复阶段 P95 耗时降低 X%。
失败、取消或放弃 Run 对后续 Session 的污染为 0，跨 Agent、Session 和 Run 的上下文泄漏为 0。
```

所有 X/N/A/B 必须由实际快照替换，并附固定语料、tokenizer、环境、场景与对照边界；PR6 不证明真实摘要质量或未实现的运行中压缩。
