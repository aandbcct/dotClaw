# Runtime ApplicationHost 收口开发计划

## 1. 已确认边界

- 本次在同一开发目标内完成，但按可验证阶段迁移，禁止大爆炸替换。
- 本地测试 Session 数据由开发者自行清理；代码不实现旧数据兼容、迁移脚本或自动删除。
- 项目功能中的 Session 删除按“拒绝活动 Run、完整清理关联事实”的新契约实现。
- 不纳入多进程/分布式锁、底层强制取消和 `RuntimeEngine` 大拆分。

## 2. 迁移总览

| 旧概念/路径 | 替代者 | 删除条件 |
| --- | --- | --- |
| `agent/factory.py:build_agent()` | `bootstrap.ApplicationHost.build()` | main 与测试入口迁移；全仓搜索无生产调用。 |
| `build_runtime_services()` 作为公开主入口 | Host 内部 Runtime 装配函数 | 仅保留 Runtime 装配测试的受控内部调用。 |
| Agent 持有工具/MCP/Skills/Dream/Context 与 `shutdown()` | ApplicationHost 生命周期资源 | Agent 构造参数、属性和关闭逻辑归零。 |
| 构造期 `LLMProxyAdapter(text_stream_port)` | 提交期/Run 级输出端口 | 双 Channel 并发输出契约测试通过。 |
| Tool Adapter `_waiting_calls`/`_executed_calls` 恢复权威 | checkpoint/控制状态传入的授权事实 | 重启后审批恢复测试通过；不再依赖集合判断。 |
| 单 Identity Context Plan 覆盖 | 基于完整 AgentRegistry 的覆盖 | 多 Identity Context 测试通过。 |

## 3. 阶段 0：基线与契约

### 目标

冻结现有 Runtime v4 的有效行为，为后续迁移建立最小契约测试。

### 修改项

- 记录当前 `tests/runtime_v2` 的通过基线，隔离与本次无关的历史失败。
- 新增或先补齐以下失败测试：
  - 新 Session 必须写入 `agent_id`；未知 Identity 不得提交；
  - 两个并发提交使用不同输出收集器时不得串流；
  - 进程内状态清空后，已批准 Run 恢复不得再次请求审批；
  - 两个 Identity 的不同 `context_slot_ids` 都应生效；
  - 删除活动 Session 被拒绝，终态 Session 删除后不存在运行目录和审批记录。

### 完成门槛

- 新契约在旧实现上可明确失败或标注待迁移；既有 Runtime 核心测试维持通过。

## 4. 阶段 1：Session-Identity 与交互入口

### 目标

将用户入口收敛为 Session，建立显式的 Identity 路由。

### 新增/修改

- 新增 `session_interaction.py`（名称以实现时项目风格为准）中的 `SessionInteractionService`。
- `SessionManager.create()` 的 `agent_id` 改为必填；构造与反序列化校验空值/未知值策略。
- Host 负责显式优先、默认兜底的 Identity 选择并在创建时落盘。
- Agent 收缩为 Identity + Coordinator 的轻量门面；移除直接暴露基础设施的属性和 `shutdown()`。
- 将 `Agent.process()` 的 Session Identity 一致性校验迁入 SessionInteractionService，避免调用方绕过 Session 权威。
- `main.py` 的 Session 创建、切换和正常消息路径改为调用 SessionInteractionService。

### 验证

- 不同 Session 可分别绑定不同 Identity，并生成对应 Run 策略。
- 用不匹配 Identity 的内部 Agent 门面不能绕过 Session 路由。
- 现有审批、取消、重试、放弃的外部行为保持等价。

## 5. 阶段 2：ApplicationHost 与资源生命周期

### 目标

建立唯一组合根，完成旧 Agent factory 的职责迁移。

### 新增/修改

- 新增 `bootstrap/application_host.py`：集中配置读取、Identity Registry 加载、LLM、工具、Skills、Memory、MCP、SessionManager、Runtime 的创建。
- 将 `agent/factory.py` 中的构建辅助函数迁入 bootstrap 私有模块或 ApplicationHost；依据关键/可降级规则处理初始化失败。
- 将 `runtime_factory.py` 收缩为 Host 私有的 Runtime 组装函数；`RuntimeServices` 移除工具、MCP、Skills 等展示字段。
- Host 统一执行 `recover_pending_success_commits()`，持有 MCP 初始化任务/Provider，并提供 `shutdown()`。
- `main.py` 仅构建 Host，并从 Host 取得 SessionInteractionService、SessionManager 和现有诊断资源。

### 验证

- 主入口不再 import `build_agent` 或 RuntimeServices。
- `agent/` 不再 import `bootstrap/` 或创建外部基础设施。
- MCP 启动失败可降级；关键依赖失败会明确终止启动。
- Host 关闭可等待/取消后台 MCP 初始化，并释放 Context 缓存。

## 6. 阶段 3：请求级输出端口

### 目标

使 Runtime 实例可被多个 Channel 安全共享。

### 修改项

- 定义只属于本次提交的输出选项/执行参数；不得把 Port 放入需要持久化或诊断序列化的 `RunRequest`。
- 依次迁移 `SessionInteractionService -> Agent -> SessionRunCoordinator -> RuntimeEngine -> LLMPort -> LLMProxyAdapter` 的方法签名。
- `LLMProxyAdapter` 构造函数只接收 LLMProxy；删除 `_text_stream_port` 成员。
- Scheduler 和无流式测试传入 `None`；CLI 每次消息构造本次的 `ChannelTextStreamAdapter`。

### 验证

- 两个 ChannelCollector 并发运行时，各自只收到本 Run 的分片。
- 不传输出端口时，RunResult 仍可正常返回最终文本。
- `has_streamed_text` 仍准确反映本次 Run 的流式输出情况。

## 7. 阶段 4：多 Identity Context 与审批恢复

### 目标

补齐本次主架构的两个持久化正确性缺口。

### 修改项

- 将 `_agent_context_plan_configuration(identity)` 替换为基于完整 AgentRegistry 的配置构造；默认 Slot 配置与各 Identity 显式覆盖同时保留。
- `AgentPolicyResolver` 与 Context Plan 使用同一份注册 Identity 来源。
- 改造审批恢复 DTO/ToolInvocation，使 Runtime 在 checkpoint 恢复时显式表达该 ToolCall 已获批准。
- 删除 ToolExecutorAdapter 作为恢复权威的 `_waiting_calls` 与 `_executed_calls`；若保留短生命周期执行缓存，必须在 Run 终态清理且不改变恢复语义。
- 将硬编码的 `context_compaction_model`、tokenizer 改为来自配置/RouterConfig 的确定性解析，并补缺省行为测试。

### 验证

- 非默认 Identity 的 `context_slot_ids` 对应实际 Context Bundle。
- 模拟重启（重建 Adapter/Engine）后，同一 `approval_id` 通过仅执行一次工具且不再次等待审批。
- 已执行工具的重复调用仍被拒绝，且终态 Run 不累积 Adapter 内存状态。
- Router 缺失模型项或使用不同默认模型时，预算/压缩配置遵循显式回退规则。

## 8. 阶段 5：Session 删除与物理收口

### 目标

让 Session 删除与 Runtime 文件事实保持一致，并物理删除旧路径。

### 修改项

- 在应用级增加 Session 删除协调流程：检查活跃 Run、清理审批记录、删除完整 Session 目录、释放 Context 缓存。
- 为 ApprovalRepository 增加按 Session 清理所需的最小 Port/Adapter 方法；不得让 SessionManager 直接了解审批文件布局。
- `SessionManager.delete()` 收缩为 Session 文件/目录原子操作或仅供协调器调用；不再以单文件删除代表完整 Session 删除。
- 删除 `agent/factory.py`、其导出、未使用 `_build_context_port()`、旧 main 导入和不再适用的测试契约。
- 更新 README 与 Runtime 文档中“唯一组合根”“Agent 门面”“Session Identity”的陈述。

### 删除前搜索验证

```powershell
rg -n "build_agent|agent\.factory|_build_context_port|RuntimeServices.*tool_executor|memory_dream" src tests
rg -n "LLMProxyAdapter\([^\n]*text_stream|_waiting_calls|_executed_calls" src tests
```

### 完成门槛

- 终态 Session 删除后，其目录、Run 文件和审批记录均不可再被查询。
- 活动 Session 删除被明确拒绝，不产生部分删除。
- 上述旧符号不存在生产调用；所有替代测试通过。

## 9. 推荐提交顺序

1. `test(runtime): add session identity, output, approval recovery contracts`
2. `refactor(session): route interaction by session identity`
3. `refactor(bootstrap): introduce application host and slim agent`
4. `refactor(runtime): make output ports run-scoped`
5. `fix(runtime): persist approval authority and multi-identity context plans`
6. `fix(session): coordinate complete session deletion`
7. `docs(runtime): document application host architecture`

## 10. 最终验收清单

- [ ] 应用启动只有 `ApplicationHost` 一个公开组合根。
- [ ] 每个新 Session 都持久化有效 `agent_id`，所有消息从 Session Identity 路由。
- [ ] Agent 不持有或关闭基础设施；RuntimeEngine 不依赖具体基础设施。
- [ ] 多 Channel 并发使用同一 Host 时流文本不串流。
- [ ] Identity Registry 中所有 Agent 的 Context Slot 覆盖都生效。
- [ ] 审批在重启后可恢复且同一工具调用至多执行一次。
- [ ] 关键/可降级初始化策略可测试、可观测。
- [ ] 删除 Session 不留 Run、checkpoint、事件或审批孤儿数据。
- [ ] Runtime v4 架构、入口迁移、审批、取消、恢复、Context 与多 Channel 新测试通过。

