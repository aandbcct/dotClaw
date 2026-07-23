# 移除运行时 Agent 门面修复开发计划

## 1. 目标与已确认边界

本修复在**一个可验证阶段**内完成：移除 `agent/agent.py` 中仅作转发的运行时 `Agent` 门面，收敛用户请求入口为 `SessionInteractionService → SessionRunCoordinator → RuntimeEngine`。

已确认的设计结论：

- `Session` 是用户可见的会话边界，`session.agent_id` 是该会话绑定 Identity 的唯一权威。
- `AgentIdentity` 是身份、模型、系统提示、可用工具、Context Slot 与策略收窄的声明边界；它不是可执行对象，必须保留。
- `SessionInteractionService` 是 Session 用例入口，负责 Session/Identity 路由、创建、删除和对外控制请求；它不持有 LLM、Tool、MCP 或 Runtime 执行状态。
- `SessionRunCoordinator` 是 Runtime 的并发与恢复协调器，负责同 Session 串行、审批恢复/重试串行化和取消死锁规避；它不读取 `SessionManager` 或 `AgentRegistry`。
- `RuntimeEngine` 继续只消费已冻结 `RunRequest` 和 Port；不直接依赖 Session、Identity Registry 或 Channel。
- 不重命名 `agent/` 包，不删除 `AgentIdentity`、Artifact、消息模型、`AgentRegistry` 或多 Agent 委派能力；本次只删除运行时 `Agent` 门面及其相关测试契约。
- 不引入通用 `ChatService`、`RuntimeOperations` 或额外中间层。

目标链路：

```text
Channel / CLI / Web
  → SessionInteractionService
     → 读取 Session 并验证 session.agent_id
     → 解析 AgentIdentity，创建冻结 RunRequest
  → SessionRunCoordinator
  → RuntimeEngine
```

## 2. 现状与问题

当前 `SessionInteractionService.get_agent()` 在每次提交时临时构造 `Agent(identity, coordinator, config)`；`Agent.process()` 再创建 `RunRequest` 并转发给 `SessionRunCoordinator`。这层对象没有独立生命周期、基础设施所有权或策略执行权。

其重复性可由当前事实确认：

- Identity 已由 `SessionInteractionService` 从 `session.agent_id` 解析并校验；
- 真实的 Identity 策略冻结由 `AgentPolicyResolver` 依据 `RunRequest.agent_id` 完成；
- 审批、取消、重试和放弃已经由 `SessionInteractionService` 直接调用 Coordinator，不经 `Agent`；
- `Agent` 每次临时创建，`last_run_result` 不是稳定的应用状态；CLI 却依赖它继续审批，导致普通提交与审批恢复存在两种入口形态。

因此应移除该门面，避免把“Agent”误解为一个实际拥有执行和生命周期的实体。

## 3. 单阶段实施计划：收敛入口并删除门面

### 3.1 前置契约

先为以下行为建立或调整测试，再修改生产代码：

- 给定绑定有效 Identity 的 Session，`SessionInteractionService.submit()` 创建的 Run 使用该 `agent_id`；
- 空或未知的 `session.agent_id` 明确失败，不能回退到默认 Identity；
- CLI 的普通消息、审批恢复、取消、重试与放弃只经 `SessionInteractionService`；
- 同一 Session 的普通提交、审批恢复与重试仍由 `SessionRunCoordinator` 串行；取消仍不等待该锁；
- `AgentIdentity` 的模型展示、策略冻结、Context Slot 覆盖和工具收窄行为不变。

已有 `tests/runtime_v2/test_phase1_identity_routing.py`、`test_entry_migration_contract.py`、`test_cli_submission_contract.py` 需从“Agent 门面存在”改为上述外部行为契约；不得保留仅为测试旧门面而存在的兼容层。

### 3.2 修改 `SessionInteractionService`

- 删除 `get_agent()`，以及对 `Agent` 和 `_display_result` 的导入。
- 保留 `_require_identity(session) -> AgentIdentity` 作为 Session-Identity 路由权威。
- 新增私有请求工厂：在已经验证 Identity 后，以 `create_run_request(session, identity.agent_id, user_message)` 创建 `RunRequest`；将其传给 `coordinator.submit_prepared(session.id, request_factory, output_port)`。
  - `RunRequest` 必须在 Coordinator 取得该 Session 租约后创建，保持历史压缩、Conversation 快照与 Run 创建的原有并发语义。
- `submit()`、`resolve_approval()`、`retry_interrupted()`、`abandon_interrupted()` 统一返回结构化 `RunResult`，不在 Service 内部映射为 CLI 文本。这样审批循环可从 `RunResult.approval_id` 继续恢复，也不会把 Channel 展示语义固化为应用服务 API。
- 新增或保留 `get_identity(session) -> AgentIdentity` 的只读校验入口，供 CLI Banner 与 `/model` 使用；它不创建运行时 Agent 对象。
- Service 仍只把结构化控制请求交给 Coordinator，不自行保存 `last_run_result` 或 Channel 状态。
- Session 删除协调逻辑不变：拒绝活动 Run、清理审批索引、删除完整目录、释放 SESSION/RUN 缓存；不释放共享 AGENT 缓存。

### 3.3 修改 CLI 与公开入口

- `main.py` 删除 `from dotclaw.agent import Agent`。
- Banner 直接由当前 Session 的 `AgentIdentity` 取得 `agent_name` 与解析后的模型；CLI 使用 `service.get_identity(current_session)`，避免绕过 Session-Identity 校验。
- 普通消息直接调用 `service.submit(current_session, user_input, text_stream_port)`；不再预先构造 Agent。
- 审批循环依据结构化 `RunResult.approval_id` 调用 `service.resolve_approval(approval_id, approved, text_stream_port)`；不得依赖已删除的 `Agent.last_run_result`。
- CLI 新增本地 `RunResult → Markdown/错误/流式收尾` 渲染函数：根据 `final_message`、`error`、`has_streamed_text` 和 `approval_id` 展示结果。其他 Channel 可按自身协议渲染同一领域结果。
- `/new`、`/switch` 后 Banner/展示路径重新按当前 Session 解析 Identity；不新增“切换 Agent”的用户命令。
- `dotclaw.agent.__init__` 删除 `Agent` 导出；顶层 `dotclaw.__init__` 若仍导出该类，也同步删除。

### 3.4 删除与文档收口

满足调用方迁移与测试通过后，物理删除：

- `src/dotclaw/agent/agent.py`；
- 仅服务于该门面的 `_display_result`、`last_run_result`、`has_streamed_final_answer` 及相关导出；
- 仅断言 `get_agent()`、直接构造 `Agent` 或 Agent 门面不可绕过 Session 的旧测试。

同步更新：

- `docs/wiki/Runtime 模块总体说明.md`：入口图改为 `SessionInteractionService → SessionRunCoordinator`，不再显示运行时 Agent 门面；
- `docs/wiki/Runtime 持久化架构.md`：入口图保持 Session-Identity 路由，不将 Identity 声明误画为运行时对象；
- `docs/Development/runtime/ApplicationHost/runtime-application-host收口总体设计.md` 与对应开发计划：将“轻量 Agent 门面”修订为“AgentIdentity 声明边界”；
- README 或 CLI 文档仅在仍出现可实例化 Agent 的用户表述时更新。

## 4. 兼容、数据与恢复边界

- **数据兼容**：无持久化格式变更。既有 `session.agent_id`、Run 的 `agent_id`、`AgentPolicySnapshot`、Context Owner key 均保持不变；不需要迁移脚本。
- **API 兼容**：`Agent` 属于内部运行时门面，本次移除其 Python 导入路径与构造契约；`SessionInteractionService` 的提交/控制方法从展示字符串改为 `RunResult`。仓库内调用方和测试必须一次迁完；不保留 deprecated shim。
- **审批恢复**：审批记录仍以 `approval_id → run_id/session_id` 定位，恢复由 Coordinator 在 Session 租约内调用 Engine；不依赖任何内存 Agent 实例。
- **多 Channel**：输出端口继续是提交/恢复级参数；Service 不保存 Channel 或输出端口实例，多个 Channel 可共享同一 Host。
- **回滚**：该修复不改变磁盘数据。若发布后需回滚代码，可恢复上一版本二进制并继续读取现有 Session/Run 数据；不得以恢复旧 Agent 门面为常态兼容方案。

## 5. 验证与完成门槛

### 定向验证

```powershell
$env:PYTHONUTF8 = '1'
.\.venv\Scripts\python.exe -m pytest -q tests/runtime_v2/test_phase1_identity_routing.py tests/runtime_v2/test_entry_migration_contract.py tests/runtime_v2/test_cli_submission_contract.py tests/runtime_v2/test_phase2_application_host.py tests/runtime_v2/test_session_deletion.py
.\.venv\Scripts\python.exe -m pytest -q tests/runtime_v2
.\.venv\Scripts\python.exe -m compileall -q src
```

### 删除前搜索

```powershell
rg -n "from dotclaw\.agent import Agent|from dotclaw\.agent\.agent import Agent|\bAgent\(" src tests
rg -n "get_agent\(|last_run_result|has_streamed_final_answer|_display_result" src tests
rg --files src\dotclaw\agent | rg "agent\.py$"
```

允许搜索结果继续出现“Agent”这一自然语言或领域术语，以及 `AgentIdentity`、`AgentRegistry`、`AgentRun`、`AgentPolicySnapshot`、`AgentAction`；验收的是运行时 `Agent` 类及其导入、构造和门面状态归零。

### 完成门槛

- 生产请求路径中不存在运行时 `Agent` 对象；普通消息、审批恢复、取消、重试、放弃均由 `SessionInteractionService` 以 `RunResult` 进入或返回 Coordinator 结果。
- Session-Identity 仍是唯一的身份路由权威；Identity 策略只在 Run 开始时冻结，不因删除门面改变。
- Coordinator 的 Session 串行、取消死锁规避、恢复语义和 Run 级输出端口契约保持通过。
- `agent/agent.py`、其导出、旧测试契约与生产调用均被物理删除。
- 定向测试、`tests/runtime_v2`、全量测试与 `compileall` 通过；`git diff --check` 无空白错误。

## 6. 推荐提交

单阶段可拆为两个原子提交，仍属于同一开发目标：

1. `refactor(session): 直接以 Identity 提交 Run，移除 Agent 门面调用`：迁移 Service、CLI 和测试。
2. `chore(agent): 删除运行时 Agent 门面并同步 Runtime 文档`：删除文件/导出、更新文档、执行全量验证。

第二个提交只能在第一个提交的替代调用和测试已完整通过后创建。
