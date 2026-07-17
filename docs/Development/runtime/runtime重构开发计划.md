# Runtime 重构开发计划

> 状态：Phase 1–Phase 6 已完成并通过最终验收。
> 对应设计：[Runtime 重构设计](runtime重构设计.md)  
> 实施策略：**一个完整重构目标、一个工作分支、分阶段迁移、最终统一切换**。中间阶段不要求对外发布，但每阶段必须可运行、可测试、可回退。

## 1. 范围、目标与实施纪律

本计划实施以下已经确认的目标：

- 以共享、业务无状态的 `RuntimeEngine` 替代当前持有 run 级状态的 `Runtime`；
- 每次执行使用独立的 `RunExecution` 与纯 `AgentState`；
- 将运行数据拆为 `Conversation`、`AgentRun`、`RunEvent`、`RunMessage`、`Checkpoint` 五类容器；
- 将现有上下文槽适配为 `ContextPort`，缓存改为按 agent / session / run 隔离；
- 普通用户消息创建新 Run；审批、外部回调等控制协议恢复原 Run；
- Runtime 通过 Ports 依赖 LLM、工具、上下文、存储和可选 delegation；
- 清除 Runtime 对 SessionManager、Journal、Context Slot、MCP、Agent 实例等具体实现的直接依赖。

本计划明确不做：跨 Run `Task` 模型、Web UI、分布式队列、RunMessage 去重 / 对象存储、Trace 持久化、delegation 业务语义重做。

### 1.1 执行规则

1. 所有工作在同一重构分支完成，最终一次切换新入口；每个 Phase 使用独立、可审查的提交。
2. 新代码只能依赖新边界；旧代码只能被 adapter 调用，不允许新增业务调用方。
3. 每个 Phase 完成前必须通过该 Phase 的测试和既有的当前架构测试。
4. 旧实现仅在新实现已有替代、调用方为零、迁移测试通过后删除；禁止提前删除并靠“以后再补”恢复。
5. 任何数据格式变更都要保留一次性迁移或旧格式只读兼容，直至最终切换验证完成。

## 2. 阶段总览

| Phase | 目标 | 主要产物 | 删除条件 |
|---|---|---|---|
| 0 | 建立可验证基线和迁移护栏 | 测试分层、架构契约测试、旧 API 清单 | 当前架构测试稳定全绿 |
| 1 | 建立新领域模型与 Port 契约 | `RunExecution`、`RunRequest`、`RunEvent`、Repository Protocol | 新模型可独立单测 |
| 2 | 建立五类持久化容器 | 新文件仓储、格式迁移、Checkpoint run 键 | 新 run 不再写旧 AgentRun 字段 |
| 3 | 抽取 ContextPort 并迁移 Slot | `ContextBundle`、scoped cache、Slot adapter | Runtime 不再直接调用 Slot API |
| 4 | 切换 RuntimeEngine 主循环 | Engine、Coordinator、审批 / 取消协议、Ports 接入 | CLI 使用新入口，单 / 多 Session 测试通过 |
| 5 | 接入 delegation adapter 并删除旧路径 | DelegationPort、清理 Journal / Agent / Runtime 旧耦合 | 旧 Runtime 路径与兼容字段调用数为零 |
| 6 | 全量回归、数据迁移和文档收口 | 端到端测试、迁移脚本、删除清单完成 | 全量必跑测试全绿，旧实现物理删除 |

## 3. Phase 0：建立基线与迁移护栏

### 目标

在修改运行代码前，明确当前架构的必跑测试、历史测试和将被废弃的 API，避免重构期间无法判断回归。

### 新增

| 文件 | 内容 |
|---|---|
| `tests/runtime_v2/` | 新 Runtime 架构测试目录；后续所有新增测试放在此处 |
| `tests/runtime_v2/test_architecture_contract.py` | 依赖方向和禁止 import 的静态检查 |
| `docs/Development/runtime/runtime重构迁移清单.md` | 旧模块、调用方、替代模块、删除条件的逐项清单 |
| `pytest.ini` 或 `pyproject.toml` 的 pytest 配置 | 定义默认必跑集合与 `legacy` marker |

### 修改

| 文件 / 区域 | 修改内容 |
|---|---|
| `tests/` | 将引用已删除 `agent.loop`、`agent.context`、`agent.result`、`dotclaw.metrics` 的历史测试标记为 `legacy` 或移动至 `tests/legacy/`；不为它们复活旧模块 |
| CI / 本地测试说明 | 默认命令只运行当前架构测试集；legacy 测试单独执行并记录迁移状态 |
| `docs/Development/architecture.md` | 标记 `AgentLoop` 图为历史设计，链接到 `runtime重构设计.md` |

### 废弃但暂不删除

| 内容 | 原因 |
|---|---|
| `tests/test_phase1_acceptance.py`、`tests/test_phase3_acceptance.py`、`tests/test_phase4_acceptance.py` 中失效 import 的测试 | 先隔离并记录替代关系，等新架构验收测试覆盖后删除 |
| 旧架构文档中的 AgentLoop 描述 | 先标注历史，不让其继续作为当前实现依据 |

### 验收

- 默认测试命令可完成收集并全绿；
- `tests/runtime_v2/test_architecture_contract.py` 至少能阻止 Runtime import `journal`、`session`、`agent.slotContext` 等具体实现；
- 当前所有旧 API 有替代方向和删除条件，没有“无主”的历史测试。

## 4. Phase 1：建立新领域模型与 Port 契约

### 目标

在不切换主执行路径的前提下，先建立 Runtime 内核的稳定类型边界。此阶段不修改 CLI 行为。

### 新增

```text
src/dotclaw/runtime/
├── domain/
│   ├── __init__.py
│   ├── models.py
│   ├── events.py
│   ├── execution.py
│   └── state.py
└── application/
    ├── __init__.py
    └── ports.py
```

| 文件 | 新增内容 |
|---|---|
| `runtime/domain/models.py` | `RunRequest`、`RunResult`、`RunStatus`、`RunError`、`ConversationSnapshot`、`ContextBundle`、`AgentPolicySnapshot` |
| `runtime/domain/events.py` | `RunEvent`、`RunEventType`、`LLMCompleted`、`ToolCompleted`、`ApprovalResolved` 等领域事件 |
| `runtime/domain/execution.py` | `RunExecution`、预算、取消令牌、pending control 数据、消息游标 |
| `runtime/domain/state.py` | 新版纯 `AgentState`、Phase、Action 与转移表；不含 Message / Tool / Task 具体类型 |
| `runtime/application/ports.py` | `RunRepository`、`CheckpointRepository`、`ContextPort`、`LLMPort`、`ToolPort`、`ApprovalRepository`、`DelegationPort` Protocol |
| `tests/runtime_v2/test_domain_state.py` | 新状态机单测：只传领域事件、断言状态和 Action |
| `tests/runtime_v2/test_ports_contract.py` | Fake ports 与输入输出契约测试 |

### 修改

| 现有文件 | 修改内容 |
|---|---|
| `src/dotclaw/runtime/agent_state.py` | 先保留原文件作为 adapter；逐步提取转移表到 `domain/state.py`，保留旧 import 的过渡 re-export |
| `src/dotclaw/runtime/__init__.py` | 只导出新领域模型与兼容别名；禁止暴露具体 adapter |

### 废弃标记

| 现有内容 | 处理 |
|---|---|
| `AgentState.tasks`、`runtime/task.py` 在状态机中的引用 | 标记 deprecated；新状态机不再接受 Task。delegation 将来只通过 `DelegationCompleted` 事件返回 |
| `AgentState` 对 `agent.LLMResponse`、`llm.Message` 的 import | 新状态机禁止使用；旧文件只保留兼容 adapter |

### 验收

- `AgentState` 新实现可以不导入任何 LLM、Tool、Session、Task 或 Repository 模块；
- RunRequest、RunExecution、RunEvent 都可 JSON 序列化；
- 使用 fake ports 可以测试完整状态机转移，不需要启动 LLM / MCP / 文件系统。

## 5. Phase 2：建立新持久化容器与迁移旧数据

### 目标

建立五类持久化容器，让新运行记录不再把消息、快照、trace ID 混入 AgentRun。

### 新增

```text
src/dotclaw/runtime/adapters/
├── __init__.py
├── file_run_repository.py
├── file_checkpoint_repository.py
└── file_approval_repository.py

scripts/
└── migrate_agent_run_v1_to_v2.py
```

| 文件 | 新增内容 |
|---|---|
| `file_run_repository.py` | 文件版 `RunRepository`：创建 / 更新 `run.json`，追加 `events.jsonl`，原子更新 `messages.json`，成功时提交 Conversation 投影 |
| `file_checkpoint_repository.py` | 文件版 `CheckpointRepository`：按 `session_id/run_id` 保存、加载、删除 `checkpoint.json` |
| `file_approval_repository.py` | `approval_id → run_id` 关联、状态与消费标记 |
| `migrate_agent_run_v1_to_v2.py` | 将旧 `AgentRun.messages` 转为 `messages.json`，将 `state_snapshot` 转为 checkpoint，保留旧 `trace_ids` 仅供迁移报告 |
| `tests/runtime_v2/test_file_repositories.py` | 原子写入、事件序号、消息 / 事件引用完整性、迁移脚本测试 |

### 修改

| 现有文件 | 修改内容 |
|---|---|
| `src/dotclaw/session/agent_run.py` | `AgentRun` 收缩为摘要：保留 id、归属、状态、时间、统计、错误摘要、引用；移除新写入路径中的 `messages`、`state_snapshot`、`trace_ids` |
| `src/dotclaw/runtime/state_store.py` | 改为旧格式读取 adapter，内部委托新版 `CheckpointRepository`；新 checkpoint 使用 `run_id` 而不是仅 `session_id` |
| `src/dotclaw/session/session.py` | 新增“成功投影追加”仓储接口或由 FileRunRepository 协调；失败 / 取消不得写 assistant message |

### 数据格式与写入顺序

新文件布局：

```text
data/sessions/{session_id}/agent_runs/{run_id}/
├── run.json
├── events.jsonl
├── messages.json
├── checkpoint.json
└── success_commit.json # 仅在成功提交中断时保留，补偿完成后删除
```

每个外部边界的写入顺序：

1. 原子更新 `messages.json`；
2. 追加引用消息 ID 的 `RunEvent`；
3. 若为安全边界，保存 `checkpoint.json`；
4. 若为终态，提交 `run.json`，成功时同时提交 Conversation 最终回复。
   文件型实现须先原子写入成功提交意图；RunEvent、Conversation 与 `run.json=COMPLETED`
   未同时完成时，启动和仓储读取必须幂等补偿该意图，完成后删除意图文件。

### 废弃与删除条件

| 内容 | 本阶段状态 | 最终删除条件 |
|---|---|---|
| `AgentRun.messages` | 不再写入，迁移脚本仍可读取 | 新旧 run 数据均可加载、所有调用改查 RunMessage |
| `AgentRun.state_snapshot` | 不再写入 | 恢复只使用 CheckpointRepository |
| `AgentRun.trace_ids` | 不再写入 | Journal 与 CLI 无调用方依赖 trace IDs |
| `StateStore._state_path(session_id)/state.json` | 只读兼容 | 所有活跃 session 已迁移或可安全放弃旧等待状态 |

### 验收

- 一个成功 run 生成五类数据中需要的四类：Conversation、AgentRun、RunEvent、RunMessage；checkpoint 可在完成后删除；
- 失败 / 取消 run 不写 Conversation assistant message；
- 每个 RunEvent 的 `message_ids` 都可在同 run 的 messages.json 找到；
- Checkpoint 不包含完整 prompt 或完整 tool result；
- 旧样例 `data/sessions/1a8d087e/agent_runs/5a6a8ae0.json` 可被迁移脚本处理。

## 6. Phase 3：抽取 ContextPort 并迁移上下文槽

### 目标

保留当前 Slot 的业务价值，消除 Runtime 对 `ContextAssembler`、Slot 缓存和手工 history 拼装的了解。

### 新增

```text
src/dotclaw/context/
├── __init__.py
├── ports.py
├── slot_context.py
├── slots.py
├── scoped_cache.py
└── slot_context_provider.py
```

| 文件 | 新增内容 |
|---|---|
| `context/ports.py` | `ContextPort` 的实现侧协议与 ContextMetadata |
| `context/scoped_cache.py` | `ScopeKey` 与按 STATIC / SESSION / CONDITIONAL 分区的缓存 |
| `context/slot_context_provider.py` | 将 Slot 输出、ConversationSnapshot、临时 RunMessage 组装为 `ContextBundle` |
| `tests/runtime_v2/test_context_port.py` | Bundle、token budget、scope cache 隔离测试 |

### 修改

| 现有文件 | 修改内容 |
|---|---|
| `agent/slotContext.py` | 迁移 / 拆分为 `context/slot_context.py`；`SlotContext` 删除 Journal 字段，加入 agent identity version、run id、ConversationSnapshot 所需输入 |
| `agent/slotContextImp.py` | 迁移 / 拆分为 `context/slots.py`；保留 Identity / Tools / Skills / Memory 等业务 Slot |
| `agent/factory.py` | 改为构造 `SlotContextProvider` 并作为 ContextPort 注入；不再将 assembler 直接塞给 Runtime |
| `runtime/runtime.py`（尚未切换时） | 用临时 adapter 调用 ContextPort，验证生成 prompt 等价 |

### 具体缓存迁移

| 现有策略 | 新缓存键 |
|---|---|
| `forever` / STATIC | `agent_id + identity_version` |
| `session` / SESSION | `agent_id + identity_version + session_id` |
| `request` / CONDITIONAL | `run_id` |
| DYNAMIC | 不缓存 |

### 废弃与删除条件

| 内容 | 本阶段状态 | 最终删除条件 |
|---|---|---|
| `ContextAssembler.on_new_request()` | 不再由 Runtime 调用 | 所有缓存由 ScopedCache 管理 |
| `ContextAssembler.clone()` | 不再用于 delegation 隔离 | delegation 通过 run scope / ContextPort 隔离 |
| `SlotContext.journal` | 删除 | 无 Slot 依赖 Journal |
| Runtime `_build_system_prompt()` / `_build_context_msgs()` | 暂保留 adapter | RuntimeEngine 只消费 ContextBundle |

### 验收

- 同一 RuntimeEngine 连续构造两个不同 Agent 或 Session 的 Context，不会出现 Identity / Tools / Memory 串用；
- ContextPort 输出完整 messages 与工具定义，Runtime 不参与 prompt / history 拼装；
- MemorySlot 失败只降级当前 Context 构造，不影响 run 状态机；
- Context token budget 有明确裁剪或失败策略，ProjectSlot 不可无限读入文件。

## 7. Phase 4：实现 RuntimeEngine、Session 协调、审批与取消

### 目标

将主循环从旧 `Runtime` 切换为基于 RunExecution、Ports 与新持久化容器的 RuntimeEngine。

### 新增

```text
src/dotclaw/runtime/application/
├── engine.py
├── session_run_coordinator.py
├── approval_service.py
└── cancellation_service.py

tests/runtime_v2/
├── test_runtime_engine.py
├── test_session_coordinator.py
├── test_approval_resume.py
└── test_cancellation.py
```

| 文件 | 新增内容 |
|---|---|
| `engine.py` | `RuntimeEngine.execute()`、`resolve_approval()`、`cancel()`；创建 / 恢复 RunExecution，驱动 AgentState，调用 Ports |
| `session_run_coordinator.py` | Session 单活跃租约、FIFO 队列、run 完成后的下一请求调度 |
| `approval_service.py` | 审批记录创建、结构化决定入口、审批与 run 的关联校验 |
| `cancellation_service.py` | run 级取消令牌管理、超时转换为取消原因 |

### 修改

| 现有文件 | 修改内容 |
|---|---|
| `src/dotclaw/runtime/runtime.py` | 首先缩为兼容 facade，转发到 RuntimeEngine；逐步移除 `_current_agent`、`_current_session_id`、`_current_agentrun_id`、Journal 直接调用、StateStore 直接调用 |
| `src/dotclaw/agent/agent.py` | `process()` 不再直接 `session.add_conversation()` / `session_mgr.save()`；改为提交 RunRequest 给 Coordinator / Engine |
| `src/dotclaw/main.py` | CLI 普通消息通过 SessionRunCoordinator 提交；审批 UI 调用 `resolve_approval()`；不再直接持有 runtime 内部对象 |
| `src/dotclaw/tools/executor.py`、`tools/approval.py` | ToolPort 返回 `ApprovalRequired`，不直接询问 Channel、不自行推进 Runtime 状态 |
| `src/dotclaw/channel/*` | 只负责展示普通回复和审批有限选项；不读取 / 写入 Runtime 状态 |

### 运行路径

#### 普通消息

```text
CLI/Web → SessionRunCoordinator 获取租约 → RuntimeEngine.execute()
→ 创建 AgentRun / RunExecution → ContextPort → LLMPort / ToolPort 循环
→ 成功：提交 Conversation + AgentRun → 释放租约
```

#### 澄清问题

```text
LLM 输出“需要补充信息”形式的正常回复
→ Run = COMPLETED
→ 写 Conversation
→ 下一条任意用户消息创建新 Run
```

#### 审批

```text
ToolPort 返回 ApprovalRequired
→ Runtime 保存 Checkpoint + WAITING_APPROVAL Event
→ 交互层提交 approval_id + decision
→ RuntimeEngine.resolve_approval() 恢复原 run_id
```

#### 取消

```text
cancel(run_id) → Runtime 标记取消 → 安全点停止 → RUN_CANCELLED
→ 不写 Conversation → 删除 checkpoint / pending approval → 释放租约
```

### 废弃与删除条件

| 内容 | 本阶段状态 | 最终删除条件 |
|---|---|---|
| `Runtime.derive()` 的 run 隔离职责 | 不再使用 | RunExecution + scoped context 覆盖父子隔离 |
| Runtime `_init_fresh()` / `_init_resume()` 旧实现 | 保留 facade 期间可调用 | Engine 覆盖新建与审批恢复 |
| Agent `process()` 中会话写入 | 删除 | 成功提交只经 RunRepository |
| Tool ApprovalManager 直接 channel 交互 | 替换为 ApprovalRequired | 所有工具都经 ToolPort 协议 |

### 验收

- 两个不同 Session 的 fake LLM run 可并发，消息 / event / checkpoint 文件完全隔离；
- 同一 Session 两条消息严格 FIFO，Conversation 顺序稳定；
- 普通澄清回复完成 run，不锁住 Session；
- 审批恢复使用相同 `run_id`，恢复前后 RunMessage / Event 序列连续；
- 失败、取消、审批等待不写 Conversation assistant message；
- RuntimeEngine 不包含 run 级实例字段，也不 import `journal`、`session`、`agent.slotContext`。

## 8. Phase 5：适配 delegation 并清除 Journal / 旧 Runtime 耦合

### 目标

保持多 Agent 可用，但让它从 Runtime 内核依赖变为可选 Port；同时让 Journal 退出恢复和事实存储职责。

### 新增

| 文件 | 新增内容 |
|---|---|
| `src/dotclaw/orchestration/runtime_delegation_adapter.py` | 现有 Dispatcher / InstanceManager 到 DelegationPort 的适配器 |
| `tests/runtime_v2/test_delegation_port.py` | fake delegation 与现有 adapter 的父子 run / 回调测试 |
| `tests/runtime_v2/test_no_journal_dependency.py` | RuntimeEngine 不读取 Journal 私有状态的静态与行为测试 |

### 修改

| 现有文件 | 修改内容 |
|---|---|
| `runtime/application/engine.py` | 仅调用 DelegationPort 的 submit / cancel / result 接口，收到结果后转换为 `DelegationCompleted` 领域事件 |
| `orchestration/dispatcher.py` | 保留业务实现，对外由 adapter 包装；不让 Runtime import 它 |
| `journal/journal.py` | 移除 StateSink 写恢复状态和 Runtime 反向读取需求；可保留为订阅 RunEvent 的终端渲染器 |
| `journal/sinks/state_sink.py` | 停止注册 / 使用；Checkpoint 仅经 CheckpointRepository 保存 |
| `journal/sinks/trace.py` | 可保留为可选 RunEvent 渲染器，但不作为持久化真相 |

### 废弃与删除条件

| 内容 | 本阶段状态 | 最终删除条件 |
|---|---|---|
| Runtime 对 `journal._events`、`_agentrun_id`、`_agentrun_sequence` 的访问 | 删除 | RunEventRepository 覆盖全部事实与统计 |
| `Journal.restore_state()` / StateSink 恢复职责 | 废弃 | 无恢复路径依赖 Journal 文件 |
| Runtime 直接 import orchestration registry / dispatcher | 删除 | DelegationPort adapter 覆盖 delegation |

### 验收

- 禁用 Journal 后 RuntimeEngine 仍能执行、恢复、保存运行数据；
- delegation 的父 / 子运行关系以 `parent_run_id`、`root_run_id` 与 RunEvent 表达；
- RuntimeEngine 可使用 fake DelegationPort 测试，不需要创建真实子 Agent。

### 实施结果（2026-07-17）

- 已新增 `orchestration/runtime_delegation_adapter.py`。它包装既有 `AgentDispatcher` 的 Task / Broker 业务状态机，为每个 target Identity 创建独立 Session 与子 Run，并通过 `SessionRunCoordinator` 执行；子 Run 的完成和取消会回调投影为既有 Task 终态，父子关系写入 `parent_run_id`、`root_run_id` 和 delegation 事件。
- `RuntimeEngine` 仅依赖 `DelegationPort`：结构化 `delegate` 调用提交子运行、查询结果并转换为 `DelegationSubmitted`、`DelegationCompleted` 领域事件；Engine 不 import Dispatcher、Journal 或旧 Runtime。
- `AgentPolicyPort` 可根据 `AgentRegistry` 冻结 target Identity 的子运行策略，保持多 Agent 策略边界。
- Journal 已停止注册 StateSink、写入 `state.json` 和恢复 StateSink 累加器；独立 StateSink 兼容实现已在 Phase 6 物理删除。
- 已新增 `test_delegation_port.py` 与 `test_no_journal_dependency.py`，覆盖 fake Port 父子事件、真实 Adapter → Dispatcher → Coordinator 子 Run 回调、父取消向子 Run 传播、target Session 请求映射和 Engine 无旧基础设施依赖。

## 9. Phase 6：统一切换、删除旧路径与最终验证

### 目标

完成新入口切换、删除已废弃代码与格式，清理历史测试 / 文档，得到唯一的 Runtime 架构。

### 修改

| 文件 / 区域 | 修改内容 |
|---|---|
| `src/dotclaw/main.py` | 只使用 RuntimeEngine + SessionRunCoordinator 新入口 |
| `src/dotclaw/agent/factory.py` | 退化为 bootstrap 的薄包装，或迁移至 `bootstrap/runtime_factory.py` |
| `src/dotclaw/runtime/__init__.py` | 删除旧 Runtime / StateStore / AgentState 兼容导出，只导出新公开 API |
| `README.md`、`docs/Development/architecture.md` | 更新主架构图、运行模型、调试与持久化说明；删除 AgentLoop 当前实现描述 |
| `tests/` | 将仍有价值的 legacy 场景改写为 runtime_v2 验收测试，删除无效历史 import 测试 |

### 物理删除清单

以下内容只能在前述删除条件全部满足后删除：

| 删除对象 | 删除前验证 |
|---|---|
| `src/dotclaw/runtime/runtime.py` 旧实现（或旧 facade） | main / tests / orchestration 均不再引用旧 Runtime API |
| `src/dotclaw/runtime/state_store.py` 旧 Session 级实现 | 所有 checkpoint 均使用 run 级 CheckpointRepository |
| `AgentRun.messages`、`state_snapshot`、`trace_ids` 字段及序列化函数 | 代码无读取方，旧数据已迁移或有迁移命令 |
| `journal/sinks/state_sink.py` | grep 无调用方，恢复集成测试通过 |
| `ContextAssembler.clone()`、`on_new_request()` 旧缓存生命周期 | ContextPort scoped cache 测试覆盖所有 Slot |
| `Agent.process()` 的 Session 直接写入代码 | Conversation 仅由 RunRepository 成功提交 |
| 状态机中的 `Task` 与旧 `runtime/task.py` 耦合 | DelegationPort 场景测试通过且当前阶段不提供 Task API |
| 已失效的 legacy acceptance tests | 对应 runtime_v2 用例已覆盖，默认测试集无 legacy import |

删除操作前必须逐项执行：

```text
rg "旧符号或旧模块" src tests docs
→ 调用方为 0 或仅迁移脚本 / 历史文档
→ 新路径测试通过
→ 删除代码与过期文档
→ 全量必跑测试通过
```

### 最终验收

1. 默认测试集、Runtime v2 单元测试、集成测试和关键端到端测试全部通过；
2. 新建 run、工具调用、澄清回复、审批等待 / 恢复、取消、不同 Session 并发、同 Session 排队、delegation adapter 均有覆盖；
3. 新增 Run 不再产生旧 `state.json`、旧 `AgentRun.messages`、`trace_ids`；
4. 所有当前架构文档只有一套 Runtime 叙事，不再将 AgentLoop 或 Journal StateSink 描述为现行路径；
5. `rg` 验证 RuntimeEngine 无具体基础设施 import，旧 Runtime API 无生产调用方；
6. 将旧运行目录样本迁移后可读取，迁移失败时给出可行动错误信息。

### 实施结果（2026-07-17）

- 已删除旧 Runtime、StateStore、旧状态机与 Task、旧 AgentRun、旧 SlotContext、旧 Resume、旧本地 runner、旧 Task 工具和 Journal StateSink；生产源码不存在这些模块的导入或符号引用。
- `Agent` 已收敛为 `SessionRunCoordinator` 门面；`AgentDispatcher` 仅保留由 `RuntimeDelegationAdapter` 调用的 Task/Broker 状态投影，不再依赖旧 Runtime 或本地 runner。
- 已删除或改写依赖旧 API 的历史测试；保留的运行、审批、取消、上下文、仓储、委派和迁移场景均由 `tests/runtime_v2/` 覆盖。
- README、开发架构状态和迁移清单已切换为 Runtime v2 的唯一叙事；旧样例迁移和缺失输入的可行动错误均有自动化测试。

## 10. 推荐提交顺序

即使最终一次合并，也建议按以下提交顺序保留历史：

```text
1. test(runtime): 建立 v2 测试基线与 legacy 隔离
2. feat(runtime): 添加领域模型与 ports
3. feat(storage): 添加 RunEvent / RunMessage / Checkpoint 仓储与迁移
4. feat(context): 添加 ContextPort 和 scoped slot cache
5. feat(runtime): 实现 RuntimeEngine 与 SessionRunCoordinator
6. feat(runtime): 接入审批、恢复、取消
7. feat(runtime): 通过 DelegationPort 接入现有编排
8. refactor(runtime): 切换 CLI / factory / agent 到新入口
9. refactor(runtime): 删除旧 Runtime、StateStore、Journal state 路径
10. docs(test): 更新文档、迁移测试并完成全量回归
```

## 11. 风险与控制点

| 风险 | 控制措施 |
|---|---|
| 新旧数据格式混用导致恢复失败 | 新 run 只写 v2；旧 run 只读兼容；迁移脚本可重复执行并带备份 |
| 一次改动过大无法定位回归 | 每 Phase 独立测试、独立提交；使用 fake ports 做高层行为测试 |
| Context 重构改变 prompt 行为 | 先使用旧 Slot adapter 双跑比对 ContextBundle；记录 token / 内容差异后再切换 |
| 有副作用工具在中断后重复执行 | 仅安全边界恢复；工具执行使用幂等键或人工确认；绝不盲重放 |
| 同 Session 并发写 Conversation | SessionRunCoordinator 租约 + 成功提交时 lease 校验 |
| Journal 删除过早丢失排障能力 | RunEvent + RunMessage 先覆盖事实；Journal 先退化为可选渲染器，最后再删状态职责 |
| delegation 扩大改造范围 | 本轮只做 DelegationPort adapter，不改变 Dispatcher 内部业务语义 |

## 12. 完成定义

本重构完成不以“新文件已经创建”为准，而以以下事实为准：

- RuntimeEngine 可以在不依赖旧 Runtime、Journal、StateStore、ContextAssembler 具体实现的情况下运行；
- 执行状态、完整消息、审计事件、恢复快照和 Conversation 各有唯一容器与唯一写入责任；
- 旧 AgentRun 混合字段、Session 级状态文件、Journal 状态存储和旧 Runtime 主循环已经物理删除；
- 所有保留功能都通过新 API 和新测试验证；
- 新增功能只能通过已定义 Ports 扩展，不会再次把具体基础设施塞回 Runtime 内核。
