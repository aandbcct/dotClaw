# dotClaw 工具层架构文档（Tool v1）

> **版本**: v1.0 | **对应阶段**: Tool v1（阶段一~四已落地，阶段五收敛文档） | **更新日期**: 2026-07-22
> **维护说明**: 本文档随工具层演进持续更新，每次架构变更请同步更新版本号和变更说明。
> **权威来源**：设计结论见《[Tool v1 总体设计](../tool/v1/总体设计.md)》，分阶段交付见《[Tool v1 开发计划](../tool/v1/开发计划.md)》。

---

## 1. 架构总览

dotClaw 工具层（Tool v1）把「可被 LLM 调用的能力」统一抽象为 `ToolHandler`，按**声明式 `@tool` + 可信包自动发现**注册到中心化 `ToolRegistry`，并由 `ToolExecutor` 以**固定安全链路**编排执行（入参校验 → Capability Broker → Policy Engine → 审批 → Handler → Journal）。MCP 远端工具作为独立来源，以命名空间 `mcp.<server>.<tool>` 接入同一注册表，但**只注册 tools**，resources/prompts 停留在 MCP 原生 API，不进入 Tool Registry。

核心分层（依赖方向自上而下，上层只依赖下层抽象）：

```
┌──────────────────────────────────────────────────────────────────────┐
│  Agent / Runtime（消费 ToolPort，规划 tool_call）                       │
│   └─ runtime/adapters/tool_executor_adapter.py  (ToolExecutorAdapter)  │
│   └─ agent/factory.py  (组合根：_build_tools / _build_mcp / build_agent)│
├──────────────────────────────────────────────────────────────────────┤
│  tools/executor.py  (ToolExecutor：固定安全链路编排)                     │
│     │ 依赖：registry + capability + policy + approval + schema          │
│  tools/registry.py  (ToolRegistry：纯注册表，无冲突 + 不可变快照)         │
├──────────────────────────────────────────────────────────────────────┤
│  tools/capability.py  (CapabilityBroker：定义+参数 → 资源请求)          │
│  tools/policy.py      (PolicyEngine：资源请求 → allow/ask/deny)         │
│  tools/approval.py    (ApprovalManager：ask 决策 → Channel 交互 Port)   │
│  tools/schema.py      (validate_args / validate_json_schema)            │
├──────────────────────────────────────────────────────────────────────┤
│  tools/decorator.py   (@tool / ToolPolicy / ToolMeta)                  │
│  tools/function_handler.py (FunctionToolHandler：已验证函数 → ToolResult)│
│  tools/discovery.py   (ToolDiscovery：可信包扫描 + 签名推导)             │
│  tools/parser.py      (SkillParser：执行后 Skill 命中检测，旁路)         │
├──────────────────────────────────────────────────────────────────────┤
│  mcp/provider.py   (MCPToolProvider：连接/发现/状态/重连，只注册 tools)  │
│  mcp/tool_adapter.py (McpToolAdapter：协议参数/结果转换，mcp.<server>.<tool>)│
│  mcp/client.py     (McpClient：单 server 连接状态机 + 调用)             │
└──────────────────────────────────────────────────────────────────────┘
```

**关键不变量**（总体设计 §10.1，违反即属缺陷）：
1. 任何工具函数之前必须完成参数验证。
2. 任何外部副作用之前必须完成 Broker 与 Policy 决策。
3. `ask` 在无交互能力时等价于拒绝（不得默认放行）。
4. Registry 内工具名全局唯一，禁止静默覆盖（`DuplicateToolError`）。
5. MCP tools 必须带 server 命名空间；resources/prompts 不进入 Registry。
6. 一个 Run 中的 LLM 工具 Schema 与可调用工具集一致且不可变（Run 级快照）。

---

## 2. 数据流

### 2.1 发现与注册（启动期，同步）

```
agent/factory.py:_build_tools()
  └─ ToolDiscovery.discover_builtin()
       ├─ 扫描可信包 dotclaw.tools.builtin（exec_tool/file_tool/memory_tool/system_tool）
       ├─ 对每个 @tool 函数：
       │    ├─ 复杂工具：取显式 args_model
       │    └─ 简单工具：从签名推导等价 Pydantic 模型（仅 str/int/float/bool + 字面量默认）
       │         不支持的签名（Optional/Union/容器/枚举/嵌套/Annotated/位置参数/*args/**kwargs）
       │         → 直接抛 ToolDeclarationError，绝不退化为无校验调用
       └─ FunctionToolHandler(meta, fn) → registry.register()
  └─ 按 config.tools.disabled_tools 逐个 unregister（迁移后的新规范名）
```

MCP 来源由 `agent/factory.py:_build_mcp()` 在 `build_agent` 中 await 首次发现完成后装配（阶段四修复：保证首个 Run 快照可见 MCP 工具）。

### 2.2 执行（运行期，固定安全链路）

```
ToolExecutor.execute(name, args, channel, journal) / execute_approved(...)
  │
  ├─ 1. registry.get(name) → None ⇒ TOOL_NOT_FOUND
  ├─ 2. 入参校验：本地工具走 validate_args(args_model)；MCP 走 validate_json_schema(input_schema)
  │       失败 ⇒ INVALID_ARGUMENTS（绝不进入 Broker/Policy/Handler）
  ├─ 3. CapabilityBroker.resolve(definition, validated, workspace_root)
  │       → list[CapabilityRequest]（文件/进程/网络/MCP 四类；policy=None 即 passthrough）
  ├─ 4. PolicyEngine.evaluate(requests, scope)
  │       → DENY ⇒ POLICY_DENIED（无审批、无执行）
  │       → ASK  ⇒ 经 ApprovalManager.request(summary, channel)
  │            ├─ 无 Channel 或拒绝 ⇒ APPROVAL_DENIED
  │            └─ 批准 ⇒ 继续
  │       → ALLOW ⇒ 继续
  ├─ 5. handler.execute(validated, ctx)  [asyncio.wait_for(timeout)]
  │       超时 ⇒ TIMEOUT；异常 ⇒ EXECUTION_ERROR；MCP server 不可用 ⇒ MCP_UNAVAILABLE
  └─ 6. Journal：tool_start / tool_policy_resolved / tool_approval_outcome / tool_end
              （仅脱敏摘要，不含密钥/认证头/原始敏感值）+ SkillParser 命中检测
```

---

## 3. 模块详解

### 3.1 基础类型 — `tools/base.py`
- `ToolSource(str, Enum)`：`BUILTIN` / `MCP` / `SKILL` / `CUSTOM`。
- `ToolDefinition`：`name` / `description` / `parameters`(JSON Schema) / `source` / `needs_approval` / `timeout` / `metadata` / `policy_profile`（ToolPolicy 档案值，Policy 阶段使用）。是工具对 LLM 与调度器的稳定契约。
- `ToolResult`：`output` / `is_error` / `error_code`(ToolErrorCode) / `error_type`(ToolErrorType) / `metadata`。所有执行结果的**唯一归一出口**。
- `ToolExecutionContext`：运行时注入（`timeout` / `agentrun_id` / `session_id` / `agent_id`），每次调用新建、不持久化。
- `ToolErrorCode`：`INVALID_ARGUMENTS` / `POLICY_DENIED` / `APPROVAL_DENIED` / `TOOL_NOT_FOUND` / `TIMEOUT` / `MCP_UNAVAILABLE` / `EXECUTION_ERROR` / `EXECUTOR_ERROR`。
- 禁止职责：不含执行逻辑、不含注册逻辑。

### 3.2 装饰器与策略档案 — `tools/decorator.py`
- `@tool(name, description, args_model?, policy?, source?, needs_approval?, timeout?, metadata?)`：仅把 `ToolMeta` 附着到函数 `__tool_meta__`，**不在导入时注册**。
- `ToolPolicy(str, Enum)`（工具作者只能从中选择，不能自由组合）：`WORKSPACE_READ`(`workspace.read`) / `WORKSPACE_WRITE`(`workspace.write`) / `PROCESS`(`process.exec`) / `NETWORK`(`network.http`) / `MCP`(`mcp.call`)。
- `ToolMeta.build_definition()`：由元数据构造 `ToolDefinition`，`parameters` 来自 `args_model` 的 `to_json_schema()`。

### 3.3 参数 Schema 与校验 — `tools/schema.py`
- `to_json_schema(model)`：Pydantic 模型 → JSON Schema（供 LLM）。
- `validate_args(model, raw)`：本地工具参数校验，默认 `extra="forbid"` 拒绝未知字段；失败抛 `ToolValidationError`。
- `validate_json_schema(raw, schema)`：MCP 等外部工具的 JSON Schema 校验适配层；对 `$ref`/`anyOf` 等组合子保守降级（不阻塞未知结构，但严格拒绝未知字段），失败抛 `ToolValidationError` 并映射为 `INVALID_ARGUMENTS`。

### 3.4 函数执行器 — `tools/function_handler.py`
- `FunctionToolHandler`：把已验证的本地异步函数包装为 `ToolHandler`；`execute` 仅调用函数并归一 `ToolResult`（异常 → `EXECUTION_ERROR`）。不接触校验/策略/审批。

### 3.5 发现 — `tools/discovery.py`
- `ToolDiscovery.discover_builtin()`：导入可信包 `dotclaw.tools.builtin` 并收集 `@tool` 函数；记录导入失败、发现结果与冲突。
- `ToolDeclarationError`：签名不支持推导时抛出，禁止退化为无校验调用。

### 3.6 注册表 — `tools/registry.py`
- `ToolRegistry`：内存 `dict[str, ToolHandler]`。
- `register(handler)`：**同名冲突抛 `DuplicateToolError`**（携带双方来源），绝不静默覆盖。
- `unregister` / `get` / `get_definitions` / `list_by_source` / `all_names` / `clear`。
- `snapshot()`：返回 `tuple[ToolDefinition, ...]`，**每个定义深拷贝**——后续注册表增删不影响已取快照，满足 Run 级隔离。
- 禁止职责：不执行、不校验、不连接外部系统。

### 3.7 能力 Broker — `tools/capability.py`
- `CapabilityBroker.resolve(definition, validated_args, workspace_root) → list[CapabilityRequest]`：
  - 按 `policy_profile` 翻译文件（`path`）、进程（`command`）、网络（`url`）请求；`policy=None` 或未知档案返回空列表（passthrough）。
  - MCP 工具：`source == MCP` 时直接由 `metadata["server"]` 形成 `mcp.call` 请求，不依赖运行参数。
- `ResourceKind`：`FILE_READ` / `FILE_WRITE` / `PROCESS_EXEC` / `NETWORK_HTTP` / `MCP_CONNECT` / `MCP_CALL`。
- `normalize_workspace_path()`：用 `realpath` 解析符号链接/Windows 联接点，检测 `..`/绝对路径逃逸 workspace 根目录（安全关键）。
- 脱敏：`_desensitize_command` 剥离 `KEY=VALUE` 环境导出；`_desensitize_url` 去除查询串。`CapabilityRequest.describe()` 只返回脱敏摘要。

### 3.8 策略引擎 — `tools/policy.py`
- `PolicyEngine.evaluate(requests, scope) → PolicyOutcome`：
  - 合并规则：**任一 DENY → 整体 DENY**；否则**任一 ASK → 整体 ASK**；否则 **ALLOW**；无请求（passthrough）视为 ALLOW。
- `PolicyScope`：`global_rules`（安全上限）+ `agent_rules`（只能收窄）+ `workspace_root` + `denied_paths` + `allowed_mcp_servers`。
- 默认规则（设计确认）：`workspace.read=allow` / `workspace.write=ask` / `process.exec=ask` / `network.http=deny` / `mcp.connect=ask` / `mcp.call=ask`。
- `mcp.connect` / `mcp.call`：**server 不在 `allowed_mcp_servers` 即 DENY**（空列表 = deny-all，fail-closed）。
- 路径约束：`escaped` 或命中 `denied_paths`(glob) → DENY。
- 不变量：默认拒绝（无规则命中取保守 `ask`）；Agent 策略只能收窄（取 severity 更严格者）；审计摘要不含敏感值。

### 3.9 审批端口 — `tools/approval.py`
- `ApprovalManager`：阶段三起重构为 **Approval Port**——只消费 Policy 的 `ask` 决策，通过 `Channel` 向用户展示**脱敏资源摘要**并请求确认。
- **不再持有命令列表，也不自行决定放行**（旧实现按 `approval_commands` 名单放行的逻辑已删除）。
- `request(summary, channel) → bool`：无 Channel → `False`（拒绝，不默认放行）；有 Channel → `channel.ask_user(...)`。

### 3.10 执行调度器 — `tools/executor.py`
- `ToolExecutor`：组合 `registry` + `approval_manager` + `policy_engine` + `capability_broker` + `skill_parser`。
- 两个入口：`execute()`（完整链路含 channel 审批）/ `execute_approved()`（ask 视为已批准，供 Runtime v2 适配器两阶段审批后复用）。
- `snapshot_definitions()`：转发 `registry.snapshot()`，供 Run 创建时捕获不可变工具集。
- `requires_approval(name)`：粗粒度预判（声明式 `needs_approval` 或档案默认 `ask`）。
- `_run_chain()`：严格按 §2.2 顺序；校验失败/deny/审批拒绝均直接返回，绝不进入 Handler。
- Journal 可观测：`tool_start` / `tool_policy_resolved` / `tool_approval_outcome`(仅脱敏) / `tool_end`，并关联 `agentrun_id`。

### 3.11 MCP Provider 与 Adapter — `mcp/`
- `MCPToolProvider`（`provider.py`）：编排连接、发现、状态、重连；**只注册 tools**（`McpToolAdapter`），不注册 resources/prompts；单 server 失败降级（`_failed_servers`），不阻塞 Agent 启动；`get_server_states()` 暴露 `McpClientState`（STARTING/CONNECTED/CRASHED/FAILED/SHUTDOWN）；连接前经 `mcp.connect` 网关（`allowed_mcp_servers` fail-closed）。
- `McpToolAdapter`（`tool_adapter.py`）：注册名 `mcp.<server>.<tool>`（`mcp_tool_name()`），`metadata["server"]` 存原始名；`execute()` 调用远程 `tools/call` 并归一 `ToolResult`；`input_schema` 暴露供 `validate_json_schema`。成功/超时/协议错误/不可用统一映射为 `ToolResult`/Journal 语义。
- `McpClient`（`client.py`）：单 server 连接状态机、`startup_timeout`(握手) / `tool_timeout`(调用)、崩溃重连（`restart_on_crash` / `max_restart_attempts`）。

### 3.12 Runtime 适配器 — `runtime/adapters/tool_executor_adapter.py`
- `ToolExecutorAdapter`：实现 Runtime `ToolPort`，隔离审批状态（按 `(run_id, call_id)` 去重，避免重复执行副作用）；对「需审批且未批准」返回 `APPROVAL_REQUIRED`，批准后再 `execute_approved`。Run 创建时通过 `agent_policy_resolver` 取 `executor.snapshot_definitions()` 的不可变快照，Run 内不再读动态 Registry。

### 3.13 组合根 — `agent/factory.py`
- `_build_tools(config, skill_registry)`：创建 `ToolRegistry` → `ToolDiscovery.discover_builtin()` 注册 → 按 `disabled_tools` `unregister` → 构造 `PolicyEngine`/`CapabilityBroker`/`ApprovalManager`/`ToolExecutor`。
- `_build_mcp(config, tool_registry)`：构造 `MCPToolProvider`（注入 `policy_engine`/`capability_broker` 复用连接网关与请求翻译），返回 `(provider, task)`。
- `build_agent()`：**await MCP 首次发现任务**后再装配 Runtime（阶段四修复，确保首个 Run 快照含 MCP 工具）；`client.connect` 自带 `startup_timeout`，失败 server 已降级，await 不无限阻塞。

---

## 4. 安全链路时序图

```
用户/Runtime       ToolExecutor        Validator      Broker        Policy       Approval      Handler      Journal
   │                  │                    │             │             │            │            │            │
   │─ execute ──────►│                    │             │             │            │            │            │
   │                  ├─ get(name) ───────┤             │             │            │            │            │
   │                  ├─ validate ───────►│ (INVALID?→INVALID_ARGUMENTS)                        │            │
   │                  ├─ broker.resolve ──────────────►│             │            │            │            │
   │                  ├─ policy.evaluate ───────────────────────────►│            │            │            │
   │                  │  DENY ⇒ POLICY_DENIED                                          │            │            │
   │                  ├─ ask? request(summary) ──────────────────────────────────────►│            │            │
   │                  │       无 Channel / 拒绝 ⇒ APPROVAL_DENIED                                     │            │
   │                  ├─ handler.execute(wait_for) ────────────────────────────────────────────►│            │
   │                  │       超时⇒TIMEOUT 异常⇒EXECUTION_ERROR MCP不可用⇒MCP_UNAVAILABLE         │            │
   │                  └─ tool_end + 脱敏策略/审批摘要 ──────────────────────────────────────────────────────►│
   │◄─ ToolResult ────┤                                                                                        │
```

---

## 5. 配置参考（`config.yaml` 的 `tools` 段）

```yaml
tools:
  builtin_enabled: true        # 是否注册内置工具
  mcp_enabled: true             # 是否启用 MCP（由 _build_mcp 检查）
  skill_enabled: true           # 遗留字段；Skill 真实开关在 config.skills.enabled

  approval_commands:            # 需审批工具名列表（新规范名；覆盖式）
    - builtin.process.execute

  disabled_tools: []            # 单工具禁用列表（新格式；旧嵌套格式已不再读取）

  exec_timeout: 60              # 秒（浮点数）

  web_search:
    enabled: false

  # 工具安全策略（阶段三，总体设计 §7.1）
  policy:
    workspace_root: .
    rules:
      workspace.read: allow
      workspace.write: ask
      process.exec: ask
      network.http: deny
      mcp.connect: ask
      mcp.call: ask
    denied_paths: [".env", ".git/**", "**/*.key"]
    allowed_mcp_servers: ["github"]   # 空列表在加载时被忽略，沿用默认允许列表

  mcp_global:
    startup_timeout: 4.0
    tool_timeout: 60.0
    restart_on_crash: true
    max_restart_attempts: 3

  mcp_servers: []                # 例: - name: fs / transport: stdio / command: npx / args: [...]
```

**一次性迁移（保留）**：旧工具名（`read_file`/`write_file`/`list_dir`/`exec`/`memory_read`/`memory_write`/`system_info`/`get_time`）在加载时经 `_migrate_tool_names` 转换为新规范名并输出弃用警告；冲突以新名为准。**已删除的兼容**：旧嵌套格式 `tools.exec.needs_approval` / `tools.exec.enabled` / `tools.python.timeout` 的读取逻辑已在阶段五移除。

---

## 6. 初始化链路（`agent/factory.py`）

```
build_agent()
  ├─ _build_tools() → ToolRegistry（discover_builtin 同步注册 8 个 builtin）
  ├─ _build_mcp() → MCPToolProvider（注入 policy_engine/capability_broker）
  ├─ await mcp_task   # 阶段四修复：首个 Run 前完成首次发现
  └─ build_runtime_services() → ToolExecutorAdapter(ToolPort)
       └─ Run 创建：agent_policy_resolver.resolve() → executor.snapshot_definitions()
          （不可变快照，Run 内不读动态 Registry）
```

---

## 7. 扩展预留（对接点）

| 对接点 | 现状 | 演进方向 |
|--------|------|----------|
| `ToolHandler` ABC | `FunctionToolHandler` + `McpToolAdapter` | 未来 CUSTOM/第三方本地工具包 |
| `ToolRegistry` | 无冲突注册 + 不可变快照 | 来源动态增删不影响在途 Run |
| `ToolProvider` ABC | `MCPToolProvider` 唯一实现 | `SkillToolProvider` / `CustomToolProvider`（Skill 当前走旁路 `SkillParser`，不注册为 Handler） |
| `PolicyEngine` | 文件/进程/网络/MCP 四类 | OS 级沙箱、网络命名空间（非本次范围） |
| Run 级快照 | `snapshot_definitions()` 深拷贝 | 重连只影响下一 Run 快照 |

*本文档由 dotClaw 开发工程师维护。架构变更后请同步更新此文档。*
