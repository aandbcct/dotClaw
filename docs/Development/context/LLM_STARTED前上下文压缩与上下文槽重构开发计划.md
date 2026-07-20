# LLM_STARTED 前上下文压缩与上下文槽重构开发计划

> 状态：已完成（E1–E5 已验收）
> 实施方式：同一分支、同一开发窗口内完成完整目标；按以下 E1–E5 阶段提交和验收，不采用无法定位回归的大爆炸改动。
> 关联设计：[整体设计](LLM_STARTED前上下文压缩与上下文槽重构整体设计.md)

## 1. 开发范围、前提与完成定义

### 1.1 范围

本计划将 Runtime 切换为：

1. 每次业务 LLM 调用的 `LLM_STARTED` 前，按真实请求 token 判断是否需要压缩 Session Conversation；
2. 超限时只压缩最旧的 75% 完整 Conversation，候选先写 Run，成功后才提交 Session；
3. `messages.json` 使用仅支持的 v3 `context_versions` 格式，精确审计每次模型调用的上下文演化；
4. 将现有 Context Slot 改为多 Owner、可注册、可刷新、可释放的上下文注入机制；
5. 同 Session 严格串行，模型服务不可用时可中断、可重试、可放弃；
6. 将最终 Conversation 投影、最新压缩候选、终态事件和 `run.json` 收敛到可恢复成功提交。

### 1.2 已确认前提

| 项目 | 约束 |
|---|---|
| 数据迁移 | 所有现有 Session 数据均为测试数据，可删除；不实现 v1/v2 读取、写入、迁移或兼容。 |
| Session 调度 | 同一 Session 的 `RUNNING`、`WAITING_APPROVAL`、`INTERRUPTED` 都是活动 Run；普通 query 返回 `SESSION_BUSY`，不排队。 |
| 用户补充输入 | 本期不接受 Run 途中的普通 query 或补充输入；审批、拒绝、取消属于控制命令。 |
| 压缩范围 | 仅 `Session.conversations`；不得压缩或投影 Run 中间消息、工具结果、委派消息。 |
| 预算 | 统计实际请求，不预留未来回复或下一轮空间；压缩后仍超限则显式失败。 |
| 压缩模型 | 使用 `LLMUsage.CONTEXT_COMPACTION` 路由；首期仅配置 `qwen3.7-max` 和显式 `cl100k_base`。 |

### 1.3 当前基线与目标差异

| 当前代码事实 | 目标替代 |
|---|---|
| `SlotContextProvider` 接受启动时构造的固定 `tuple[ContextSlot, ...]`，Slot 以 `produce()` 产出字符串。 | `ContextSlotRegistry`、`ContextPlanResolver`、`ContextSlotManager` 按 Owner 与 Binding 组合，Slot 以结构化 `ContextContribution` 产出。 |
| `messages.json` v2 保存 `initial_context`；`RunRepository` 有 `save/load_initial_context()` 与 `requires_messages_migration()`。 | v3 仅保存连续、完整、不可变 `context_versions`；`run.json.active_context_version` 指向当前版本。 |
| `SessionHistoryPreparationService` 在 Run 创建前压缩并写 Session。 | `RuntimeEngine` 每次业务 `LLM_STARTED` 前生成 staged 候选，成功提交后才写 Session。 |
| 上下文 token 估算使用字符数近似，未完整计入工具 Schema。 | `TokenCounterPort` + tiktoken Adapter 统计实际请求及协议开销；无编码配置明确拒绝。 |
| `RUN_CONTEXT` 表达压缩范围，`SlotCacheScope` 混合所有权和缓存含义。 | 仅 `SESSION_HISTORY`；Owner、刷新策略、缓存范围拆成独立枚举。 |
| `SessionRunCoordinator` 的等待状态可能释放 Session 调度权。 | 由未终态 Run 持续占有 Session，重启后 `RUNNING` 转 `INTERRUPTED(PROCESS_RESTART)`。 |

### 1.4 完成门槛

只有同时满足以下条件才可宣布本计划完成：

- E1–E5 的新增、修改、废弃和删除项均完成；
- 全部新旧相关测试、契约测试和全量回归通过；
- `git diff --check` 通过；
- 删除前搜索验证没有生产源码遗留引用；
- 文档与最终代码目录、端口名称、持久化字段一致。

## 2. 跨模块最小契约（先于真实实现）

每个跨模块能力先定义 Protocol、精确 DTO、Fake/InMemory Adapter 与契约测试，再接入真实 Adapter。不得以 `Any` 或宽泛 `object` 绕过契约。

| 契约 | 调用方需要的最小操作 | 必须保证的语义与失败 | Fake / 契约测试 |
|---|---|---|---|
| `TokenCounterPort` | 对结构化 LLM 请求统计 input token | 返回精确计数或明确的 `TOKENIZER_UNAVAILABLE`；必须包含工具 Schema 和协议开销；不允许字符数回退 | 固定 token 数 Fake；验证所有请求组成项均传入 |
| `HistoryCompactorPort` | 以前一摘要和完整 Conversation 批次生成新摘要 | 单批失败不产生候选；服务不可用转可恢复错误；批次不可拆 Conversation | Scripted Fake；单批、滚动、失败与 hash 测试 |
| `ContextPort` | 用 Plan、Owner 快照和 Run Message 引用构建 `ContextBundle` | 只返回本次有效 Slot；不写持久化；失败 Slot 以结构化状态报告 | Fake Slot / InMemory Registry；顺序、工具定义、消息引用测试 |
| `RunRepositoryPort` | 追加 Context Version、候选、事件、checkpoint 与成功提交意图 | Context Version 只追加；重复恢复幂等；文件原子替换 | 临时目录 Adapter；崩溃点恢复测试 |
| `SessionProjectionPort` | 原子应用最终 Conversation 与最新候选 | 未成功 Run 永不写入；重复应用无重复 Conversation/摘要 | InMemory Session；幂等投影测试 |
| `SessionRunCoordinator` | 创建、控制、重试、放弃 Run | 一个 Session 仅一个未终态 Run；重启状态可恢复 | InMemory 活动 Run 表；并发请求和状态迁移测试 |

## 3. 分阶段实施

### E1：v3 领域模型、持久化契约与旧格式清理

**阶段目标**：先固定新的数据真相与序列化边界，使后续 Token、Slot、Engine 改造都不再依赖 `initial_context` 或旧消息格式。

**前置依赖**：现有持久化 A–D 已存在；测试 Session 可清空。

**新增**：

- `runtime/domain/context.py`（或等价聚焦模块）：`ContextVersion`、`ContextSlotSnapshot`、`StagedHistoryCompression`、`SuccessCommitIntent`、`ContextContributionKind`、`ContextSlotStatus`、`ContextOwner`、候选状态与中断/放弃原因枚举；
- v3 `messages.json`、`run.json`、`checkpoint.json` 的精确 DTO 与 JSON 序列化/反序列化；
- Run Repository v3 Fake、格式契约测试、原子文件替换测试。

**修改**：

- `runtime/application/ports.py` 与 `runtime/adapters/run_repository.py`：替换 `save/load_initial_context()` 为追加/读取 Context Version，保存活动版本、候选和成功提交意图；
- `runtime/domain/state.py`、`facts.py`、`events.py`：增加 `INTERRUPTED`、`ABANDONED`、`RUN_INTERRUPTED` 等明确状态与事件；
- `runtime/application/execution.py`、`dto.py`：内存执行态只保存 `active_context_version`、候选引用和 checkpoint 所需字段；
- 所有相关 fixture：直接构造 v3 数据，不保留旧格式夹具。

**废弃与物理删除**：

| 废弃项 | 删除条件 | 搜索验证 |
|---|---|---|
| `InitialContextSnapshot`、`initial_context` 字段和序列化函数 | v3 审批恢复与输入审计测试通过 | `rg -n "InitialContextSnapshot|initial_context" src tests` 无生产引用 |
| `save_initial_context()`、`load_initial_context()`、`requires_messages_migration()` | 所有 Port、Adapter、调用方替换 | `rg -n "requires_messages_migration|save_initial_context|load_initial_context" src tests` 归零 |
| v1/v2 解析、迁移分支和 `scripts/migrate_messages_v1_to_v2.py` | 所有测试仅写 v3 | `rg -n '"version": [12]|messages.*v[12]' src tests scripts` 仅允许历史文档引用 |
| `ContextCompactionScope.RUN_CONTEXT` | E1 的替代枚举测试通过 | `rg -n "RUN_CONTEXT" src tests` 归零 |

**验收与门槛**：

- 新建 Run 仅能写 v3；读取 v1/v2 返回明确不支持错误，不进行隐式转换；
- Context Version 必须从 1 连续递增，旧版本不可覆盖；
- 候选正文不重复写到 `run.json`；
- Fake 与真实文件 Repository 通过相同契约测试；
- 完成后才允许 E2/E3 依赖 v3 接口。

**风险**：领域模型过宽会再次变成类型垃圾桶。控制方式是将可长期成立的事实放 `domain`，将 `ContextBudgetDecision`、构建请求等过程 DTO 放 `application`。

### E2：精确 Token 预算与上下文压缩用途路由

**阶段目标**：用可替换、可验证的计数和压缩 Port 取代字符估算与隐式裁剪；此阶段只建立能力与测试，不提前把完整 Engine 时序迁入。

**前置依赖**：E1 的 v3 Context Version 与候选领域类型、Run Repository Port。

**新增**：

- `TokenCounterPort`、`TokenCountRequest`、`TokenCountResult` 和 `ContextBudgetDecision`；
- 基于 tiktoken 的 Token Counter Adapter；
- `LLMUsage.CONTEXT_COMPACTION` 与 `HistoryCompactorPort`；
- 压缩批次选择器：按完整 Conversation 选择当前未覆盖部分最旧的 75%，至少保留一条最新原文；
- 滚动摘要器：当摘要源超过压缩模型窗口时，按完整 Conversation 批次传递 `previous_summary`；
- Token Counter、Compactor 的 Fake 与契约测试。

**修改**：

- `config/settings.py`：`ModelConfig` 增加显式 `tokenizer_encoding`；
- 用途路由配置：加入 `context_compaction`，首期候选只含 `qwen3.7-max`，配置 `cl100k_base`；
- `agent_policy_resolver.py`：冻结 context window、model ID、tokenizer encoding 与压缩用途路由信息；
- `llm_context_compactor.py`：必须以 `purpose=CONTEXT_COMPACTION` 调用代理，并把服务不可用映射为可恢复的端口错误；
- `slot_context_provider.py`：先保留入口兼容，但移除其中字符数估算和静默裁剪职责，为 E3 的 ContextPort 让位。

**废弃与物理删除**：

| 废弃项 | 删除条件 | 搜索验证 |
|---|---|---|
| 字符长度除以 4 的 `_estimate_tokens` 类逻辑 | 所有预算路径经 `TokenCounterPort` | `rg -n "len\(.+\) / 4|// 4|_estimate_tokens" src tests` 无预算实现残留 |
| Provider 内部截断/静默裁剪 | E4 预算失败回归测试通过 | `rg -n "truncate|trim|裁剪" src/dotclaw/context src/dotclaw/runtime` 人工确认无静默路径 |
| 压缩调用默认 chat 用途 | 压缩路由配置与 Adapter 测试通过 | `rg -n "LLMUsage\.CONTEXT_COMPACTION|context_compaction" src tests config` 均有明确引用 |

**验收与门槛**：

- 计数请求包含系统 Slot、历史摘要、未压缩历史、当次输入、Run Message、工具 Schema、协议开销；
- 缺失或不可用 tokenizer 时只记录不含 prompt 正文的 `WARNING`，并返回确定性拒绝；
- 压缩输入按 Conversation 分批；75% 选择算法和“至少保留一条”边界有单元测试；
- Proxy 重试耗尽可被 Engine 区分为可中断错误；
- 完成后才允许 E4 调用真实压缩 Port。

**风险**：模型实际分词与兼容编码可能不同。首期不做静默容错；编码由配置冻结并可替换 Adapter。

### E3：Context Slot 多 Owner 重构

**阶段目标**：将 Slot 从“固定 system prompt 字符串生产者”改为“按计划加载的结构化上下文贡献”，并保留独立 Owner 的数据生命周期。

**前置依赖**：E1 的 Context Version 快照模型；E2 的 ContextBundle 预算输入结构。

**新增**：

- `context/contracts.py`：`ContextSlot`、`ContextContribution`、`ContextSlotDescriptor`、`ContextSlotBinding`、`ContextPlan`、Owner/缓存/刷新枚举；
- `ContextSlotRegistry`：注册 Descriptor 和构造器；
- `ContextPlanResolver`：从 Agent、Session、Run、Global 的启用配置解析排序后的 Binding；
- `ContextSlotManager`：实例缓存、`request_refresh()`、`drain_signals()`、`release_scope()`；
- `ContextSignalBus` 和类型化信号订阅接口；
- `context/provider.py`：作为 `ContextPort` 具体实现，输出 `ContextBundle`、Slot 快照与 Run Message ID 引用；
- 新 Slot 接入测试样例，证明只新增 Slot/Descriptor/注册/Agent 启用项即可进入 Plan。

**修改**：

- `slot_context_provider.py`：迁移或拆分为新 Provider，调用方只依赖 `ContextPort`；
- `slots.py`：将 Identity、Skills、Tools、History、UserInfo、Memory、Knowledge、AvailableAgents 迁入明确 Owner 的 Slot 实现；
- `scoped_cache.py`：拆分原混合 `SlotCacheScope` 为 `ContextOwner`、刷新策略、缓存范围；删除未被生产调用的 `clear_run` 或将其吸收为 `release_scope(RUN)`；
- `bootstrap/runtime_factory.py`、`agent/factory.py`：从固定 Slot 元组改为注册表与 Plan Resolver 装配；
- `AgentPolicyResolver`：由 Agent 启用 Tool ID 解析实际 Schema，放入 `ContextBundle.tools`，不复制完整 Schema 到 system text。

**废弃与物理删除**：

| 废弃项 | 删除条件 | 搜索验证 |
|---|---|---|
| 启动时固定 `tuple[ContextSlot, ...]` | 所有组合根改用 Registry/Resolver | `rg -n "SlotContextProvider\(|tuple\[ContextSlot" src tests` 人工确认无固定装配 |
| `produce() -> str | None` | 现有 Slot 全部迁入 `load()` | `rg -n "\.produce\(|def produce" src tests` 归零 |
| 混合 `SlotCacheScope` | Owner、缓存、刷新行为均有独立测试 | `rg -n "SlotCacheScope|CONDITIONAL|DYNAMIC" src tests` 无旧语义残留 |
| 默认 `WorkspaceSlot`、`ProjectSlot` | 默认 Context Plan 与集成测试不含二者 | `rg -n "WorkspaceSlot|ProjectSlot" src tests` 无生产引用 |

**验收与门槛**：

- 已绑定 Slot 必须在快照中以 `INCLUDED`、`EMPTY` 或 `FAILED` 出现；注册但未启用 Slot 不出现；
- `RunMessagesSlot` 只保存 Message ID 引用，不复制 Message 正文；
- 定向 `request_refresh(slot_id, ...)` 与 SignalBus 刷新都在下一 `LLM_STARTED` 安全点生效；外部不得拿到并直接刷新 Slot 实例；
- `release_scope` 在 Run 终态、Session 删除、Agent 卸载时被调用；
- 新 Slot 样例不修改 Engine 或全局 Slot 元组。

**风险**：把 Owner 读取逻辑塞进 Manager 会重新耦合。控制方式是 Manager 只管理 Slot 生命周期，Owner 数据由 Binding 中的精确 Port/快照提供。

### E4：LLM_STARTED 前动态压缩、严格串行与可恢复中断

**阶段目标**：把 E1–E3 的契约接入实际 Runtime 主路径，形成每次业务模型调用前的唯一上下文安全点。

**前置依赖**：E1 v3 存储、E2 Token/Compactor、E3 ContextPort；各 Fake 和契约测试已通过。

**新增**：

- `ContextBudgetPlanner` 的 Engine 编排：构建当前 Context Version、计数、选择压缩、生成 staged 候选、重建并复计数；
- `retry_interrupted(run_id)`、`abandon_interrupted(run_id)` 或等价的协调器用例；
- 进程启动/首次 Session 访问时遗留 `RUNNING` 转 `INTERRUPTED(PROCESS_RESTART)` 的恢复器；
- 每次业务 LLM 调用前的 checkpoint 数据：版本、候选引用、消息/事件序号、预算决策、`next_action=INVOKE_LLM`。

**修改**：

- `runtime/application/engine.py`：删除以 `initial_context` 冻结的调用时序，接入 ContextPort、ContextBudgetPlanner、v3 版本保存、`LLM_STARTED` 数据与中断映射；
- `runtime/application/session_run_coordinator.py`：以持久化未终态 Run 实现严格串行；`WAITING_APPROVAL`、`INTERRUPTED` 不释放 Session；
- 审批恢复：从 `active_context_version` 与 Run Message 引用重建，而不是构造空 Conversation；
- `runtime/adapters/llm_proxy_adapter.py`：使用 ContextBundle 的工具 Schema和消息引用，保留流式输出接口；
- CLI/Channel 入口：将 `SESSION_BUSY`、`INTERRUPTED`、`ABANDONED` 映射为清晰的用户可见结果。

**废弃与物理删除**：

| 废弃项 | 删除条件 | 搜索验证 |
|---|---|---|
| `SessionHistoryPreparationService` 与 Run 前直接写 Session 的路径 | 首次和多次 `LLM_STARTED` 前压缩、取消/中断不提交测试通过 | `rg -n "SessionHistoryPreparationService|prepare_history" src tests` 归零 |
| 审批恢复的 `initial_context` / 空 Conversation 构造 | 审批恢复上下文重建回归通过 | `rg -n "_conversation_from_initial_context|initial_context" src tests` 归零 |
| 等待审批释放 Session 调度权 | 严格串行与重启恢复测试通过 | `rg -n "release.*approval|WAITING_APPROVAL" src/dotclaw/runtime` 人工确认无释放路径 |

**验收与门槛**：

- 未超限不产生候选；超限仅压缩最旧 75% Conversation；同一 Run 多次候选只允许最新提交；
- 压缩调用失败或业务模型不可用时，保存 checkpoint 并转 `INTERRUPTED`；不新增半成品 Context Version/候选；
- Tokenizer 错误、参数错误、压缩后仍超限转 `FAILED`，不误标为可重试；
- `WAITING_APPROVAL` Run 时普通 query 返回 `SESSION_BUSY`；批准恢复仍可看到历史、工具调用与工具结果；
- 重启遗留 `RUNNING` 不会永久占锁；用户新 query 会先将 `INTERRUPTED` 旧 Run 标记 `ABANDONED`；
- 真实 ContextPort + 工具结果进入第二次 LLM 调用的集成测试通过。

**风险**：Engine 改动跨越主路径。控制方式是先以 Scripted LLM、Fake TokenCounter、InMemory Session 写入端建立端到端用例，再替换真实 Adapter。

### E5：成功事务、入口收口与最终删除

**阶段目标**：保证成功 Run 在崩溃时可恢复为一致事实，收口所有入口、旧代码、测试和文档。

**前置依赖**：E4 主路径、状态机和 v3 审计已通过。

**新增**：

- `SuccessCommitIntent` 恢复流程：扫描存在意图但未完成的 Run，按“Session 原子投影 → 幂等终态事件 → Run 终态 → 删除 checkpoint”补偿；
- 失败注入测试点：Conversation 投影前/后、终态事件前/后、Run 文件前/后中断；
- CLI/Channel 端到端用例，含流式输出、`SESSION_BUSY`、中断重试与放弃提示。

**修改**：

- `RunRepository.commit_success()`、`SessionConversationProjector` 与恢复器：只接受 `SuccessCommitIntent` 的可恢复顺序；
- CLI 和 Channel 输出：恢复 Runtime 的流式输出回调，并仅输出用户可见事件，避免第三方库 DEBUG 噪声；
- `docs/Development/context`：让整体设计、开发计划和实际实现名称完全一致。

**废弃与物理删除**：

| 废弃项 | 删除条件 | 搜索验证 |
|---|---|---|
| 分散且不可恢复的 `commit_success()` 写入顺序 | 全部崩溃注入与恢复测试通过 | `rg -n "commit_success|SuccessCommitIntent" src tests` 人工确认仅保留新路径 |
| 未接入的 CLI 输出旁路 / 旧日志配置 | 流式输出与日志级别集成测试通过 | `rg -n "_resolve_model|DEBUG" src/dotclaw` 人工确认入口无失效调用 |
| 所有 E1–E4 标记废弃项 | 替代调用归零、测试覆盖、搜索验证通过 | 见第 4 节最终删除清单 |

**验收与门槛**：

- 任一成功提交中断点恢复后，不会出现“`RUN_COMPLETED` 已写、Session 未投影”或“Session 已投影、Run 永不终态”；
- 同一 Run 重复恢复不重复追加 Conversation、Compression、事件；
- CLI 启动、模型选择、流式输出和 Runtime 事件展示通过冒烟测试；
- 全量测试、静态检查、`git diff --check` 和删除前搜索验证全部通过。

**风险**：文件系统不能提供跨文件 ACID 事务。解决方式是可恢复意图日志与幂等投影；适用边界是单个工作目录内的原子替换，不承诺多主机分布式事务。

## 4. 最终删除清单与验证顺序

物理删除必须放在替代实现和测试都通过之后，禁止仅停止调用而留下兼容层。

1. 删除 v1/v2 `messages.json` 读写、迁移判断、迁移脚本及 fixture；执行旧版本和 `initial_context` 搜索验证。
2. 删除 `RUN_CONTEXT` 与 Run 前 `SessionHistoryPreparationService`；执行压缩范围和 Session 写入点搜索验证。
3. 删除固定 Slot 元组、旧 `produce()`、混合 `SlotCacheScope` 以及 Workspace/Project 默认 Slot；执行 Slot API 与组合根搜索验证。
4. 删除字符数估算、Provider 静默裁剪和旧压缩默认路由；执行 TokenCounter/用途路由搜索验证。
5. 删除不可恢复的成功提交路径与过时 CLI 输出旁路；执行提交意图和入口搜索验证。

建议在删除前运行：

```powershell
rg -n "InitialContextSnapshot|initial_context|requires_messages_migration|RUN_CONTEXT|SessionHistoryPreparationService" src tests scripts
rg -n "def produce|\.produce\(|SlotCacheScope|WorkspaceSlot|ProjectSlot" src tests
rg -n "_estimate_tokens|context_compaction|SuccessCommitIntent" src tests config
python -m pytest -q
git diff --check
```

搜索结果仅可包含本计划、整体设计等历史说明文件中的必要文字；生产源码、测试、配置和迁移脚本中不得保留废弃实现引用。

## 5. 推荐提交顺序

1. `context-v3-domain-and-storage`：E1 领域事实、Port、v3 文件序列化、fixture 与格式测试。
2. `context-token-budget-and-compactor-route`：E2 TokenCounter、配置、压缩 Port/Fake/契约测试。
3. `context-slot-owner-lifecycle`：E3 Slot 合约、Registry、Resolver、Manager、SignalBus 与组合根。
4. `runtime-dynamic-context-compaction`：E4 Engine 安全点、候选、严格串行、审批恢复、中断/重试。
5. `runtime-success-recovery-and-cleanup`：E5 成功恢复、CLI/Channel、删除旧实现、全量回归和文档收口。

每个提交必须独立通过其阶段测试；若实际开发需要拆分为更小提交，仍应保持先契约、后 Adapter、再入口的依赖顺序。

## 6. 最终验收清单

### 入口与架构边界

- [x] CLI/Channel/API 均通过 `SessionRunCoordinator` 进入 Runtime，普通 query 不绕过 Session 串行约束。
- [x] Engine 只依赖 Application Port；不直接依赖 Slot、文件 Repository、Session Manager 或 LLM SDK。
- [x] 新增 Slot 仅需实现 Slot、Descriptor、注册和 Owner 启用配置，无需改 Engine 或固定全局元组。

### 上下文与持久化

- [x] 每个 `LLM_STARTED` 都可由 `context_version + incremental_message_ids + tool_schema_hash` 重建实际输入。
- [x] Snapshot 只包含有效 Plan Slot；每个绑定 Slot 的 `INCLUDED/EMPTY/FAILED` 状态完整可审计。
- [x] Context Version 只追加；摘要正文只出现于 Context Version，候选控制信息只出现于 Run。
- [x] 取消、失败、审批等待、中断和放弃均不向 Session 提交候选。

### 压缩与模型调用

- [x] Token 统计包含所有实际输入内容和工具 Schema；未知 tokenizer 写 `WARNING` 后拒绝，不使用字符数估算。
- [x] 超限只压缩最旧 75% 完整 Conversation；摘要分批不拆 Conversation；压缩后仍超限明确失败。
- [x] 压缩使用 `CONTEXT_COMPACTION` 用途路由；代理重试耗尽时 Run 为 `INTERRUPTED`。
- [x] 工具结果会进入下一次 LLM 调用；审批恢复能看到原 Conversation 与 Run Message。

### 一致性、迁移与质量

- [x] 一个 Session 同时至多一个未终态 Run；重启后遗留 `RUNNING` 可恢复或放弃，不会永久阻塞。
- [x] SuccessCommitIntent 失败注入恢复后，Run 终态、Session Conversation、压缩候选和事件一致且幂等。
- [x] v1/v2、`initial_context`、`RUN_CONTEXT`、Run 前压缩、旧 Slot API、Runtime 预算中的旧 token 估算及迁移脚本完成物理删除。
- [x] `python -m pytest -q`、针对性集成测试、`git diff --check` 和第 4 节搜索验证全部通过。
