# Runtime 重构迁移清单

> 状态：Phase 1、Phase 2、Phase 3、Phase 4 的 Runtime v2 执行路径已完成并通过回归验收。旧 CLI/Agent 入口仍将在后续兼容迁移中切换。本文记录旧 Runtime 相关模块的归属、当前调用方、替代方向与删除条件；后续 Phase 完成时必须同步更新。
> 对应设计：[Runtime 重构设计](runtime重构设计.md)。

## 使用规则

- 新代码只能依赖 `runtime/domain`、`runtime/application` 定义的边界；旧模块只能由适配器或兼容门面调用。
- 删除前执行 `rg "旧符号或旧模块" src tests docs`，确认调用方为零或仅剩迁移脚本、历史文档。
- 历史测试不得通过复活已删除的生产 API 修复；其测试归属见 [测试迁移清单](../test-migration.md)。

## 生产 API 与模块

| 旧模块 / API | 当前调用方或职责 | 替代模块 | 计划 Phase | 删除条件 |
| --- | --- | --- | --- | --- |
| `runtime/runtime.py::Runtime` | `main.py`、`agent/agent.py`、`agent/factory.py`、`orchestration/dispatcher.py`、`orchestration/runners/local.py`；仍为旧入口兼容实现 | `runtime/application/engine.py::RuntimeEngine` + `SessionRunCoordinator`（Phase 4 已实现） | 4 | 入口、Agent 与编排均只调用新入口；Runtime 仅保留兼容门面期间的调用方为零后删除 |
| `runtime/runtime.py::Runtime.derive()` | 子 Agent 与 handoff 的运行隔离 | `RunExecution`、作用域化 ContextPort 与 `DelegationPort` | 4–5 | delegation adapter 覆盖父子运行隔离，生产代码不再调用 `derive()` |
| `runtime/agent_state.py::AgentState` | 旧 Runtime 驱动 ReAct；状态持有 LLM 响应、工具结果与 Task | `runtime/domain/state.py::AgentState`；旧模块显式导出 `V2AgentState` 等迁移别名 | 1 | 新状态机测试覆盖全部转移，旧 Runtime 兼容门面不再依赖旧状态机 |
| `runtime/task.py` 与 `AgentState.tasks` | 旧状态机内的跨 Run 任务语义 | 本轮不替代；新版 `AgentState` 已无 Task 依赖，delegation 后续仅经 `DelegationPort` 返回 | 1、5 | 保留调用方迁移至 delegation adapter 或明确移除 |
| `runtime/state_store.py::StateStore` | `Runtime`、`agent/resume.py`；以 Session ID 保存恢复快照 | `CheckpointRepository`（以 `run_id` 为键；Phase 2 已增加委托入口） | 2 | 新增和恢复运行只使用 CheckpointRepository，旧 `state.json` 仅保留 `save_legacy` / `load_legacy` 兼容路径或迁移脚本 |
| `session/agent_run.py::AgentRun` 的 `messages`、`state_snapshot`、`trace_ids` | 旧 Runtime 写入，`AgentRunManager` 读取 | `RunMessage`、`Checkpoint`、`RunEvent` 与摘要 AgentRun（Phase 2 文件仓储已新增） | 2 | Runtime v2 新运行不写这些字段；迁移脚本可处理旧样例，旧 Runtime 迁移至新入口后删除兼容读写 |
| `agent/slotContext.py::SlotContext`、`ContextAssembler` | 旧 Runtime 兼容回退与历史测试；新工厂不再构造或注入 | `context/slot_context.py`、`SlotContextProvider`、`ScopedCache` | 3 | RuntimeEngine 完成切换后移除旧 Runtime 的回退分支；旧 Slot 测试迁移至 `tests/runtime_v2/test_context_port.py` 后删除 |
| `agent/agent.py::Agent.process()` 的 Session 直接提交 | `main.py` 通过 Agent 处理用户消息并直接保存 Conversation | `SessionRunCoordinator.submit()` 与 RunRepository 成功投影 | 4 | Conversation 仅由成功终态的 RunRepository 提交，Agent 不直接写 Session |
| `journal/journal.py` 的私有状态与 `journal/sinks/state_sink.py` | 旧 Runtime 读取 `_events`、`_agentrun_id`、`_agentrun_sequence`，并经 Journal 间接保存状态 | `RunEvent`、RunRepository、CheckpointRepository；Journal 退化为可选渲染器 | 2、5 | RuntimeEngine 无 Journal import 或私有字段读取；恢复集成测试不依赖 StateSink |
| `tools/executor.py` / `tools/approval.py` 的直接交互 | 工具直接驱动 Journal、Channel 与审批流程 | `ToolPort` 返回标准化结果或 `ApprovalRequired` | 4 | Channel 只展示审批选项，RuntimeEngine 经 ApprovalRepository 处理恢复 |
| `agent/factory.py` 的 Runtime 具体装配 | 已构造 `SlotContextProvider` 并以 ContextPort 注入兼容 Runtime | `bootstrap/runtime_factory.py` 组合根 | 3–4 | 新工厂只装配 Ports 与 RuntimeEngine；旧构造路径无生产调用方 |

## 历史测试与文档

| 历史项 | 当前状态 | 替代 / 维护位置 | 删除条件 |
| --- | --- | --- | --- |
| `tests/test_phase4_acceptance.py` | 已标记 `legacy`，依赖已删除的 `agent.context` 与旧 prompt provider | `MemoryManager`、MemorySlot 的当前契约测试；后续补 Runtime v2 context 测试 | 对应 Runtime v2 ContextPort 验收覆盖后删除 |
| `tests/test_phase7_acceptance.py` | 已标记 `legacy`，包含已删除的 `agent.context` / `agent.prompt` 引用 | SkillRegistry、SkillsSlot 的当前模块测试 | Skill 的 Runtime v2 上下文契约覆盖后删除 |
| `tests/metrics/` | 已标记 `legacy`，依赖已删除的 `dotclaw.metrics` 命名空间 | `tests/journal/` 的事件、历史与存储测试 | Journal 退出 Runtime 事实存储后，替代 RunEvent 测试覆盖指标需求 |
| 计划中提及的 `tests/test_phase1_acceptance.py`、`tests/test_phase3_acceptance.py` | 当前工作树不存在 | 历史迁移关系记录于 `test-migration.md` | 无需删除；若恢复文件，必须先标记 `legacy` 并登记替代测试 |
| AgentLoop 相关旧设计图 | 保留作历史背景，不作为当前实现依据 | [开发架构状态](../architecture.md) 与 Runtime 重构设计 | 所有引用迁移为 RuntimeEngine 后按文档治理策略归档或删除 |

## Phase 0 审计命令

```powershell
# 默认必跑测试
.\.venv\Scripts\python.exe -m pytest

# Runtime v2 的依赖方向护栏
.\.venv\Scripts\python.exe -m pytest tests/runtime_v2/test_architecture_contract.py

# 删除前的调用方审计模板
rg "Runtime|StateStore|ContextAssembler|slotContext|state_sink" src tests docs
```

## Phase 1、Phase 2 与 Phase 3 已交付内容

- `runtime/domain/`：可 JSON 序列化的请求、结果、消息、运行摘要、事件、检查点、`RunExecution` 与纯 `AgentState`；不导入 LLM、Tool、Session、Task 或仓储实现。
- `runtime/application/ports.py`：Run、Checkpoint、Context、LLM、Tool、Approval、Delegation 的 Protocol 边界。
- `runtime/adapters/`：文件版 Run、Checkpoint、Approval 仓储；RunEvent 只允许引用已原子写入的 RunMessage。
- `runtime/adapters/session_conversation_projector.py`：成功运行通过既有 `SessionManager` 幂等投影为 Conversation；失败、取消、等待审批不会生成 Conversation。
- `FileCheckpointRepository`：递归拒绝 `prompt`、`messages`、`tool_result` 等完整上下文载荷，检查点只保存恢复所需的最小控制数据。
- `runtime/agent_state.py`、`session/agent_run.py` 与 `StateStore`：明确旧格式兼容别名或 `*_legacy` 入口，防止 Runtime v2 新代码误用旧持久化模型。
- `scripts/migrate_agent_run_v1_to_v2.py`：旧 AgentRun 到 v2 容器的可重复迁移脚本；保留源文件，支持显式覆盖迁移目标。
- `tests/runtime_v2/`：状态机、fake ports、文件原子性、事件引用完整性、旧样例迁移、Session 成功投影、checkpoint 载荷边界及 Phase 1/2 兼容验收测试。
- `context/`：不含 Journal 的 `SlotContext`、业务 Slot、`SlotContextProvider` 与 `ScopedCache`；缓存按 Agent 身份、Session、Run 分区，避免共享 Runtime 时串用。
- `SlotContextProvider`：输出完整 `ContextBundle`（消息、工具定义、来源、失败槽位与 token 预算元数据）；Memory 等可选 Slot 失败只降级当前 Context 构造。
- `LegacyContextPortAdapter`：旧 Runtime 在未切换 RuntimeEngine 前经 ContextPort 生成 system prompt，旧 Assembler 仅作为未注入 ContextPort 时的兼容回退。
- `tests/runtime_v2/test_context_port.py`：覆盖完整 Bundle、三类缓存隔离、Memory 降级、token 预算及旧 Runtime 过渡适配器。
- `runtime/application/engine.py`：纯 Port 驱动的 `RuntimeEngine`，将 RunExecution、消息、事件、审批恢复和取消全部限制在 run 局部变量与持久化容器中；不导入 Journal、Session 或旧 Slot。
- `runtime/application/session_run_coordinator.py`：同 Session FIFO 租约、不同 Session 并行的请求协调器。
- `runtime/application/approval_service.py`、`cancellation_service.py`：审批记录的唯一消费入口与 run 级取消令牌管理。
- `tests/runtime_v2/test_runtime_engine.py`：覆盖普通澄清完成、跨 Session 隔离、同 Session FIFO、审批同 run_id 恢复及取消不写 Conversation。
