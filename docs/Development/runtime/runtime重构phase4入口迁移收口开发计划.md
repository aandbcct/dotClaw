# Runtime 重构 Phase 4：入口迁移收口前置开发计划

> 状态：待开发。
>
> 本文是 [Runtime 重构开发计划](runtime重构开发计划.md) 中 Phase 4 的入口迁移收口子计划，供后续开发人员执行。
> 目标是以最小桥接方式跑通 Runtime v2 主路径，不进行无关的大规模模块重写；delegation、旧 Journal 完整退出和旧 Runtime 物理删除属于 Phase 5、Phase 6。

## 1. 当前完成情况

已完成的 Runtime v2 核心能力：

- `RuntimeEngine`、`SessionRunCoordinator`、`ApprovalService`、`CancellationService` 已存在；
- v2 文件仓储、Checkpoint、审批记录、ContextPort 和 scoped cache 已存在；
- 已有 fake ports 验证普通完成、不同 Session 隔离、同 Session FIFO、审批同 run_id 恢复、取消不写 Conversation；
- 当前全量回归基线为 `218 passed`。

未完成的入口迁移：

- `agent/factory.py` 仍装配旧 `Runtime`、`LLMProxy`、`ToolExecutor`；
- `Agent.process()` 仍调用旧 `Runtime.run()`，并直接写 `Session`；
- `main.py` 的普通消息仍经旧 Agent / Runtime 路径；
- `ToolExecutor` 的审批仍会直接依赖 Channel，不能直接接入新版审批恢复协议；
- 旧 `Runtime` 尚未收缩为兼容 facade。

因此，当前 Phase 4 为“核心执行路径完成、入口调用方未迁移”的状态，不能视为完整完成。

## 2. 本计划范围

### 2.1 目标

完成以下主路径：

```text
CLI 普通消息
  → Agent.process()
  → SessionRunCoordinator.submit()
  → RuntimeEngine.execute()
  → ContextPort / LLMPort / ToolPort
  → RunRepository 成功投影 Conversation
```

并跑通：

- 普通回复与澄清回复；
- 工具调用；
- 审批等待、同 run_id 恢复、审批拒绝；
- 取消；
- 同 Session FIFO、不同 Session 并行。

### 2.2 非目标

以下内容不在本计划中重写或删除：

- delegation、`Runtime.derive()`、Dispatcher、Task 语义；这些属于 Phase 5；
- Journal 渲染、Trace 历史展示；本轮只要求新主路径不依赖 Journal；
- 旧 Runtime 文件、旧 StateStore、旧 AgentRun 格式的物理删除；这些应在 Phase 5/6 调用方清零后进行；
- 工具业务实现、MCP、Memory、Skill 的内部重构；本轮只增加 Port bridge。

## 3. 迁移原则

- 先新增 adapter，再切调用方；旧路径未迁移完成前不得删除旧实现。
- RuntimeEngine 只能依赖 Port，不能 import Journal、Session、Channel、旧 Slot 或旧 Agent。
- 成功 Conversation 只能由 `RunRepository.commit_success()` 提交；`Agent.process()` 和 CLI 不得直接写 Session。
- ToolPort 遇到审批只能返回 `APPROVAL_REQUIRED`；Channel 只展示审批选项并提交决定。
- 每个阶段必须先通过专项测试，再切下一层调用方。
- 每阶段完成后更新 [runtime重构迁移清单.md](runtime重构迁移清单.md)。

## 4. 阶段计划

### 阶段 A：建立入口迁移基线

目标：锁定旧入口调用方和迁移完成标准。

新增：

- `tests/runtime_v2/test_entry_migration_contract.py`
  - 断言普通消息经过 Coordinator；
  - 断言 `Agent.process()` 不直接 `add_conversation()`；
  - 断言 `main.py` 不调用旧 `Runtime.run()`；
  - 断言失败、取消、审批等待不产生 Conversation assistant 消息。

修改：

- `runtime重构迁移清单.md`
  - 标记 `Runtime.run()`、`Agent.process()`、`ToolExecutor` 审批的当前调用方和删除条件。

废弃：

- 禁止新增旧 `Runtime.run()`、`Agent.process()` 直接 Session 写入、`ApprovalManager.check()` 作为新主路径调用。

删除：无。

验收：

```powershell
rg "runtime\.run|add_conversation|ApprovalManager|channel=" src tests
```

### 阶段 B：实现旧 LLM、工具和策略的 v2 Port bridge

目标：不改写 LLM/工具业务实现，只将其包装为 RuntimeEngine 可消费的 Port。

新增：

- `src/dotclaw/runtime/adapters/llm_proxy_port.py`
  - `LLMProxyPort` 实现 `LLMPort`；
  - `ContextBundle` 转旧 `Message` / 工具定义；
  - `ChatChunk` 聚合为标准 `RunMessage`；
  - 映射工具调用、普通回复、异常、token 统计和 best-effort cancel。
- `src/dotclaw/runtime/adapters/tool_executor_port.py`
  - `ToolExecutorPort` 实现 `ToolPort`；
  - 普通工具结果映射为 v2 `ToolResult`；
  - 需要审批的工具返回 `ToolResultStatus.APPROVAL_REQUIRED`；
  - 审批通过后允许同一 `run_id + call_id` 执行一次。
- `src/dotclaw/runtime/adapters/agent_policy_port.py`
  - `AgentPolicyPort` 实现 `RunPolicyPort`；
  - 冻结 identity、模型、最大循环次数、system prompt、工具定义与 token 预算。

修改：

- `src/dotclaw/tools/executor.py`
  - 抽出无 Channel 副作用的“工具是否需要审批”查询入口；
  - 保留旧 `execute()` 兼容行为，不能在本阶段删除。
- `src/dotclaw/tools/approval.py`
  - 保留旧兼容用途；新路径不再调用其交互式 `check()`。
- `src/dotclaw/runtime/adapters/__init__.py`
  - 导出三类 bridge adapter。

废弃：

- 新主路径中 `ToolExecutor.execute(..., channel=...)`；
- 新主路径中 `ApprovalManager.check()`；
- Engine 直接依赖 `LLMProxy`、`ToolExecutor`、旧 LLM Message 类型。

删除：无。旧 LLM 和 ToolExecutor 仍是 adapter 的实现依赖。

验收：

- `tests/runtime_v2/test_llm_proxy_port.py`；
- `tests/runtime_v2/test_tool_executor_port.py`；
- 审批工具不访问 Channel；
- `rg "dotclaw\.llm|dotclaw\.tools" src/dotclaw/runtime/application` 无结果。

### 阶段 C：新增组合根，装配 Engine 与 Coordinator

目标：工厂以新版 Port 组合 Runtime，不再把旧 Runtime 作为普通消息执行器。

新增：

- `src/dotclaw/bootstrap/runtime_factory.py`
  - 装配 `RuntimeEngine`、`SessionRunCoordinator`；
  - 装配 `LLMProxyPort`、`ToolExecutorPort`、`AgentPolicyPort`、`SlotContextProvider`；
  - 装配 `FileRunRepository`、`FileCheckpointRepository`、`FileApprovalRepository`、`SessionConversationProjector`。
- `src/dotclaw/runtime/application/request_factory.py`
  - 从 Session 快照创建 `RunRequest`；
  - 生成 `ConversationMessage`、`ConversationSnapshot`、`lease_id`；
  - 不将可变 Session 对象传入 Engine。

修改：

- `src/dotclaw/agent/factory.py`
  - 调用 `bootstrap/runtime_factory.py`；
  - 返回 Coordinator / Engine 所需入口对象；
  - 不再为普通消息装配旧 Runtime、Journal、StateStore、旧 AgentRunManager。

废弃：

- 工厂中旧 Runtime 主循环装配；
- 工厂向普通消息路径注入 Journal、旧 StateStore、ContextAssembler。

删除：

- 删除 `agent/factory.py` 中已无调用的旧 Runtime 普通执行装配代码。
- 不删除仍可能被 delegation 兼容路径使用的对象；必须登记到迁移清单。

验收：

- 工厂集成测试创建 Coordinator 并成功提交普通 Run；
- 新 Run 不写旧 `state.json`、旧 `AgentRun.messages`、`trace_ids`；
- 成功 Run 经 `SessionConversationProjector` 写既有 Session。

### 阶段 D：迁移 Agent.process 的提交语义

目标：Agent 只负责身份、策略和调用门面；Conversation 由 RunRepository 唯一写入。

修改：

- `src/dotclaw/agent/agent.py`
  - `process()` 改为构造/提交 RunRequest 到 `SessionRunCoordinator`；
  - 返回 `RunResult.final_message.content` 或标准错误；
  - 删除直接 `session.add_conversation()` 与 `session_mgr.save()`；
  - 普通消息不再调用旧 `Runtime.run()`。

废弃：

- `Agent.process()` 中的直接 Session 写入；
- `Agent.process()` 中通过 `run_ids` 决定 Conversation 写入。

删除：

- 在 `rg "add_conversation|session_mgr\.save|runtime\.run" src/dotclaw/agent/agent.py` 无结果后，物理删除对应代码、类型导入和历史测试断言。

验收：

- 普通回复与澄清回复各写一条 Conversation；
- 失败、取消、审批等待不写 Conversation；
- 同 Session 两次 `process()` 严格 FIFO；
- 不同 Session 可并行。

### 阶段 E：迁移 CLI 与审批交互入口

目标：CLI 只负责输入、展示和有限审批选项，不读写 Runtime 内部状态。

修改：

- `src/dotclaw/main.py`
  - 普通消息调用迁移后的 `Agent.process()` / Coordinator；
  - 审批交互只提交 `approval_id + approved` 到 `RuntimeEngine.resolve_approval()`；
  - 取消操作只提交 `run_id + reason` 到 `RuntimeEngine.cancel()`；
  - 不再直接调用旧 `Runtime.run()`。
- `src/dotclaw/channel/*`
  - 只展示审批有限选项并返回决定；
  - 不读取 Journal、StateStore、旧 waiting run 或 ToolExecutor 内部状态。

新增：

- `tests/runtime_v2/test_cli_submission_contract.py`
  - 普通消息走 Coordinator；
  - 审批只调用 `resolve_approval()`；
  - CLI 不直接写 Session。

废弃：

- CLI 通过 `ApprovalManager` 或 ToolExecutor 直接处理审批；
- CLI 对 Journal / StateStore 的运行恢复依赖。

删除：

- `main.py` 中旧 Runtime 普通消息调用；
- Channel 中只为旧审批流程服务的调用代码。

验收：

```powershell
rg "runtime\.run|ApprovalManager|StateStore|journal\." src/dotclaw/main.py src/dotclaw/channel
```

新入口文件应无旧主路径引用。

### 阶段 F：旧 Runtime 收缩为兼容 facade

目标：删除已迁移的普通 ReAct 主循环，但保留 delegation 所需兼容边界。

修改：

- `src/dotclaw/runtime/runtime.py`
  - 普通消息入口转发到 Coordinator / Engine；
  - 标记为 `LegacyRuntimeFacade`；
  - 移除普通路径中 `_current_agent`、`_current_session_id`、`_current_agentrun_id`、`_current_allowed_tools`；
  - 移除普通路径中的 Journal、StateStore 和旧 prompt/history 手工拼装。
- `src/dotclaw/runtime/__init__.py`
  - 新 API 以 Engine、Coordinator、Port 为主；
  - 旧 Runtime 仅保留显式兼容名称。

废弃：

- `Runtime._init_fresh()`、`_init_resume()`、`_build_system_prompt()`、`_build_context_msgs()`、`_save_waiting_state()`、`_save_agent_run()` 的普通消息用途；
- Runtime 对 Journal 私有字段的读取。

删除：

- 当普通入口、Agent、CLI、工厂均不再调用这些方法时，物理删除旧普通执行循环。
- 不删除 `Runtime.derive()`、handoff、Dispatcher、Task 相关逻辑；它们必须等 Phase 5 `DelegationPort` adapter 完成。

验收：

```powershell
rg "Runtime\.run|_current_agent|_current_session_id|_current_agentrun_id|_build_system_prompt|_build_context_msgs" src tests docs
```

普通入口生产调用方必须为零；剩余引用只能是明确登记的 delegation 兼容代码、迁移脚本或历史文档。

## 5. 完整验收清单

- 默认测试集与 `tests/runtime_v2/` 全部通过；
- 普通回复、澄清回复、工具调用、审批等待/恢复、审批拒绝、取消均有集成测试；
- 两个不同 Session 可并发，单 Session 请求 FIFO；
- 审批恢复使用同一 `run_id`，消息与事件序列连续；
- 失败、取消、审批等待不写 Conversation assistant message；
- 新 Run 不产生旧 `state.json`、旧 `AgentRun.messages`、`trace_ids`；
- RuntimeEngine 不 import `journal`、`session`、`agent.slotContext`；
- 所有删除项均完成 `rg` 调用方审计、替代测试和全量回归。

## 6. 删除边界说明

本计划完成后，可以删除旧普通消息入口及其直接 Session 写入逻辑；但不能据此删除整个 `runtime/runtime.py`。

以下删除必须延后：

| 删除项 | 延后原因 | 对应阶段 |
| --- | --- | --- |
| `Runtime.derive()`、handoff、Dispatcher 直接耦合 | 需要先由 DelegationPort 覆盖 | Phase 5 |
| `runtime/task.py`、旧 AgentState 的 Task 耦合 | 需先完成 delegation 场景迁移 | Phase 5 |
| Journal StateSink 与旧恢复链路 | 需确认无 delegation / 历史恢复调用方 | Phase 5–6 |
| 整个旧 Runtime、旧 StateStore、旧 AgentRun 字段 | 需完成生产调用方清零和数据迁移 | Phase 6 |

## 7. 每阶段固定操作

每个阶段完成后必须执行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/runtime_v2 -q
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

并同步更新：

- [runtime重构迁移清单.md](runtime重构迁移清单.md)；
- 本文对应阶段状态、剩余调用方与删除条件；
- 新增 adapter、入口迁移和删除项的测试证据。
