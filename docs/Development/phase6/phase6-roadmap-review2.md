# Phase 6 开发计划审计报告 v2

> 审计人：开发架构师（审计员2号）
> 审计日期：2026-06-03
> 审计对象：`docs/phase6/phase6-roadmap.md` 审计修正版 — "MCP 协议集成"
> 前置审计：`docs/phase6/phase6-roadmap-review.md`（审计员1号报告，3 个阻塞级 + 4 个设计缝隙 + 3 个缺失项）
> 代码基线：P5 已实现（ToolExecutor 已含 `get_definitions()` / `get_handler()` 转发方法）
> **状态：已查阅 ✅**

---

## 总体评价

审计员1号的 10 项修正（3 个阻塞级缺陷 + 4 个设计缝隙 + 3 个缺失项）在修正版 roadmap 中**全部正确落地**。配置 dataclass 集中到 `config/settings.py`、缺失方法补全、Handler 拆分为三个独立类、非文本 content 降级处理、`/mcp` 加载中状态展示等均符合预期。P6 与 P5 预留接口的对齐程度很高——`ToolSource.MCP` 枚举、`ToolProvider` ABC、`ToolExecutor.get_definitions()` / `get_handler()` 转发方法全部就位，MCP 集成只需实现接口即插即入。

二次审计发现 **1 个新的阻塞级缺陷** 和 **5 个设计缝隙/前瞻性问题**。整体架构质量高，修正成本低。

---

## 一、结构性问题（🔴 阻塞级）

### 缺陷 1：`McpResourceInfo` 和 `McpPromptInfo` 缺少 `from_mcp()` 类方法

**位置**：§3.2 `_discover()` vs. §3.3 数据类定义

**问题描述**：

`McpClient._discover()` 明确调用了两个未定义的方法：

```python
# §3.2 — McpClient._discover()
self._resources = [McpResourceInfo.from_mcp(r) for r in resources_result.resources]
self._prompts = [McpPromptInfo.from_mcp(p) for p in prompts_result.prompts]
```

但在 §3.3 的数据类定义中：

- `McpToolInfo` 有 `from_mcp()` ✅
- `McpResourceInfo` **无** `from_mcp()` ❌
- `McpPromptInfo` **无** `from_mcp()` ❌

运行时 `_discover()` 会在第 361 行（resources）或第 368 行（prompts）抛出 `AttributeError`，导致 resources/prompts 发现失败。虽然 `_discover()` 对这些调用做了 try/except 容错（第 363-364 行），但失败原因会变成 `AttributeError: type object 'McpResourceInfo' has no attribute 'from_mcp'`，掩盖了真正的意图——本应是"server 不支持 resources/prompts"，而非代码 bug。

**影响**：所有 MCP server 的 resources 和 prompts 发现会静默失败（被 try/except 捕获），tool 发现正常。用户只能使用 tools/call，无法使用 resources/read 和 prompts/get 功能。

**改进建议**：

在 §3.3 中为 `McpResourceInfo` 和 `McpPromptInfo` 补充 `from_mcp()` 类方法：

```python
@dataclass
class McpResourceInfo:
    uri: str
    name: str
    description: str = ""
    mime_type: str = ""

    @classmethod
    def from_mcp(cls, mcp_resource) -> "McpResourceInfo":
        """从 MCP SDK 的 Resource 对象构建"""
        return cls(
            uri=mcp_resource.uri,
            name=mcp_resource.name,
            description=getattr(mcp_resource, 'description', '') or '',
            mime_type=getattr(mcp_resource, 'mimeType', '') or '',
        )


@dataclass
class McpPromptInfo:
    name: str
    description: str = ""
    arguments: list[dict] = field(default_factory=list)

    @classmethod
    def from_mcp(cls, mcp_prompt) -> "McpPromptInfo":
        """从 MCP SDK 的 Prompt 对象构建"""
        return cls(
            name=mcp_prompt.name,
            description=getattr(mcp_prompt, 'description', '') or '',
            arguments=[
                {"name": a.name, "description": getattr(a, 'description', ''),
                 "required": getattr(a, 'required', False)}
                for a in getattr(mcp_prompt, 'arguments', []) or []
            ],
        )
```

> 注：属性名（如 `mimeType` vs `mime_type`）需根据实际 MCP SDK 的 API 调整。已在 §9.4-9.5 标注此类待确认项。

---

## 二、设计缝隙（🟡 重要）

### 缝隙 2：MCP 工具无法加入审批控制——`needs_approval=False` 是第一道不可逾越的门

**位置**：§3.3 McpToolCallHandler.definition() · §4.4 P5 ToolExecutor 审批流程

**问题描述**：

Phase 5 的审批流程是双重 AND 门：

```
1. ToolExecutor: if definition.needs_approval → 进入 step 2, else → 跳过
2. ApprovalManager: if tool_name in approval_commands → 触发确认, else → 放行
```

所有三个 MCP Handler（McpToolCallHandler / McpResourceHandler / McpPromptHandler）在 `definition()` 中硬编码了 `needs_approval=False`。这意味着：

- **即使用户将 MCP 工具名加入 `config.yaml` 的 `approval_commands` 列表，审批也不会触发。**
- 第一道门（`needs_approval`）直接短路，第二道门（`approval_commands`）永远不会被检查。

对于安全性敏感的场景——例如 MCP server 暴露了文件删除或网络访问能力——用户无法通过 config 控制审批。

虽然 §8 验收标准第 14 条声明"审批一致：MCP 工具与内置工具审批逻辑一致，默认不审批，用户按需配置"，但按需配置`无法生效`。

**改进建议**：

三选一：

- **方案 A（推荐，最小改动）**：将 `needs_approval` 从 Handler 硬编码改为从 config 读取。在 `_connect_and_register()` 创建 Handler 时检查 `tool_name in config.tools.approval_commands`：

```python
# §3.4 — _connect_and_register()
needs_approval = tool_info.name in self._registry_approval_commands  # 需要传递此信息
handler = McpToolCallHandler(
    client=client,
    tool_info=tool_info,
    timeout=client.get_tool_timeout(),
    needs_approval=needs_approval,  # 新增参数
)
```

这需要 MCPToolProvider 持有 `approval_commands` 引用，并在 `McpToolCallHandler` 构造函数中新增 `needs_approval` 参数。

- **方案 B**：修改 P5 的 ToolExecutor，使其在跳过 `needs_approval` 门之前也检查 `approval_commands`。但这会改变 P5 的审批语义，影响面较大。

- **方案 C**：在 Phase 6 文档中明确声明此限制："MCP 工具在 Phase 6 不支持审批控制，预计 Phase 7+ 支持"。如果选此方案，验收第 14 条需修正。

建议**方案 A**——使 MCP 工具的审批控制可配置。

---

### 缝隙 3：`McpToolCallHandler` 三重超时嵌套——冗余但非 bug

**位置**：§3.3 McpToolCallHandler.execute() · §3.2 McpClient.call_tool() · §4.4 P5 ToolExecutor

**问题描述**：

当前设计形成了三层超时嵌套：

| 层级 | 位置 | 超时来源 | 值 |
|------|------|---------|---|
| 1 | `ToolExecutor.execute()` | `definition.timeout` | 60s（从 Handler 的 ToolDefinition 读取） |
| 2 | `McpToolCallHandler.execute()` | `self._timeout` | 60s（从 `client.get_tool_timeout()` 初始化） |
| 3 | `McpClient.call_tool()` | `timeout` 参数 | 60s（由 Handler 传入） |

第 1 层（ToolExecutor）通过 `asyncio.wait_for(handler.execute(), timeout)` 控制；第 3 层（McpClient）通过 `asyncio.wait_for(session.call_tool(), timeout)` 控制。两层 `asyncio.wait_for` 嵌套，超时值相同。

实际执行效果：由于 ToolExecutor 的 `wait_for` 包装了整个 `handler.execute()` 调用，它会先触发超时，返回 `ToolResult(error_code="TIMEOUT")`。McpClient 内部的 `wait_for` 在正常情况下不会触发（因为 ToolExecutor 已提前 cancel）。

这不是 bug，但有三重冗余——`context` 参数在 Handler 中完全未被使用。设计上可以简化：

```python
# 简化后：Handler 不再做自己的超时，Trust ToolExecutor
async def execute(self, arguments, context=None) -> ToolResult:
    try:
        result = await self._client.call_tool(
            self._tool_info.name,
            arguments,
            timeout=None,  # 不设置客户端超时，由 ToolExecutor 全局控制
        )
        ...
```

但这会失去 belt-and-suspenders 的保护。当前设计的安全性更高——如果 ToolExecutor 的超时因某种原因未生效（极端边缘情况），客户端超时仍然在工作。

**建议**：在 §3.3 的执行逻辑中增加注释，说明三重超时的意图：

> "ToolExecutor 在 Handler 外部用 asyncio.wait_for 控制超时。Handler 内部通过 client.call_tool(timeout=...) 设置客户端超时作为兜底保护。context 参数暂未在 MCP Handler 内使用——超时来源为 definition.timeout（由 provider 在注册时根据 server config 设置）。"

---

### 缝隙 4：`McpPromptHandler` 参数类型硬编码为 `string`

**位置**：§3.3 McpPromptHandler.definition()

**问题描述**：

```python
def definition(self) -> ToolDefinition:
    return ToolDefinition(
        ...
        parameters={
            "type": "object",
            "properties": {
                arg["name"]: {"type": "string", "description": arg.get("description", "")}
                for arg in self._prompt_info.arguments
            },
        },
        ...
    )
```

MCP prompt 的参数可能不是 `string` 类型——例如可能有 `number` 或 `boolean` 类型的参数。当前实现将所有参数硬编码为 `"type": "string"`，会导致：

- 如果 LLM 生成 `{"count": 5}` 但 schema 声明为 `type: string`，LLM 可能输出 `{"count": "5"}`（字符串），然后 prompts/get 收到错误类型的参数
- 如果 MCP server 的 prompt 参数 schema 声明了 `required` 属性，当前实现也未传递

**改进建议**：

`McpPromptInfo.from_mcp()` 应保存参数的原始类型信息，然后在 `definition()` 中正确映射：

```python
@dataclass
class McpPromptInfo:
    name: str
    description: str = ""
    arguments: list[dict] = field(default_factory=list)

    @classmethod
    def from_mcp(cls, mcp_prompt) -> "McpPromptInfo":
        return cls(
            name=mcp_prompt.name,
            description=getattr(mcp_prompt, 'description', '') or '',
            arguments=[
                {
                    "name": a.name,
                    "description": getattr(a, 'description', ''),
                    "required": getattr(a, 'required', False),
                    "type": getattr(a, 'type', 'string'),  # 保留原始类型
                }
                for a in getattr(mcp_prompt, 'arguments', []) or []
            ],
        )
```

然后在 `McpPromptHandler.definition()` 中：

```python
parameters={
    "type": "object",
    "properties": {
        arg["name"]: {
            "type": arg.get("type", "string"),
            "description": arg.get("description", ""),
        }
        for arg in self._prompt_info.arguments
    },
    "required": [
        arg["name"]
        for arg in self._prompt_info.arguments
        if arg.get("required", False)
    ] or None,  # None if empty (OpenAI API 兼容)
}
```

---

### 缝隙 5：P6 `_cmd_tools()` 增强与 P5 实际实现的差异

**位置**：§3.6 `_cmd_tools()` vs. P5 实际 main.py 代码

**问题描述**：

P6 roadmap §3.6 中的 `_cmd_tools()` 是按来源分组的增强版本：

```python
def _cmd_tools(channel, tool_executor):
    definitions = tool_executor.get_definitions()
    builtin = [d for d in definitions if d.source == ToolSource.BUILTIN]
    mcp_tools = [d for d in definitions if d.source == ToolSource.MCP]
    # 分组展示...
```

但实际 P5 代码基线中的 `_cmd_tools()` 是简单列表版本：

```python
def _cmd_tools(channel, tool_executor):
    definitions = tool_executor.get_definitions()
    channel.print_info(f"可用工具 ({len(definitions)} 个):")
    for d in definitions:
        handler = tool_executor.get_handler(d.name)
        mark = " [需审批]" if handler and handler.definition().needs_approval else ""
        channel.print_info(f"  {d.name}{mark}: {d.description}")
```

P6 的增强版本去掉了 `get_handler()` 调用和 `[需审批]` 标记显示。`[需审批]` 标记的丢失是一个功能退化——用户无法从 `/tools` 列表中看出哪些工具需要审批。

**改进建议**：

合并 P5 的审批标记和 P6 的来源分组：

```python
def _cmd_tools(channel, tool_executor):
    definitions = tool_executor.get_definitions()
    if not definitions:
        channel.print_info("(没有注册任何工具)")
        return

    builtin = [d for d in definitions if d.source == ToolSource.BUILTIN]
    mcp_tools = [d for d in definitions if d.source == ToolSource.MCP]

    if builtin:
        channel.print_info(f"内置工具 ({len(builtin)} 个):")
        for d in builtin:
            mark = " [需审批]" if d.needs_approval else ""
            channel.print_info(f"  {d.name}{mark}: {d.description}")

    if mcp_tools:
        by_server: dict[str, list] = {}
        for d in mcp_tools:
            server = d.metadata.get("server", "unknown")
            by_server.setdefault(server, []).append(d)
        channel.print_info(f"\nMCP 工具 ({len(mcp_tools)} 个):")
        for server, tools in by_server.items():
            channel.print_info(f"  [{server}]")
            for d in tools:
                mark = " [需审批]" if d.needs_approval else ""
                channel.print_info(f"    {d.name}{mark}: {d.description}")
```

> 注：`d.needs_approval` 直接读取 `ToolDefinition` 字段，无需通过 `get_handler()` 查找。

---

### 缝隙 6：`ToolSource` 导入缺失

**位置**：§3.6 `_cmd_tools()`

**问题描述**：

P6 的 `_cmd_tools()` 使用了 `ToolSource.BUILTIN` 和 `ToolSource.MCP`，但未在该代码片段中显示 import 语句。main.py 需要新增导入：

```python
from dotclaw.tools.base import ToolSource
```

虽然这是显而易见的补充，但作为完整的设计文档应包含此导入声明。

---

## 三、前瞻性审查

### 长期发展性评估

| 维度 | 评分 | 说明 |
|------|------|------|
| **P7 Skill 工具化兼容** | ✅ 好 | `ToolSource.SKILL` 枚举 + `ToolProvider` ABC 已预留，`SkillToolProvider` 与 `MCPToolProvider` 设计模式一致 |
| **多 MCP server 扩展** | ✅ 好 | 并行连接 + 独立状态管理 + server 前缀命名，架构可水平扩展 |
| **同 server 多工具同名** | ⚠️ 中等 | 如果一个 MCP server 内存在两个 `search` 工具（不常见但可能），后注册会覆盖先注册。MCP 协议未禁止 server 内同名，但实际极少出现 |
| **跨 server 工具同名** | ⚠️ 中等 | Tool 类型保持原名策略（后注册覆盖），Resource/Prompt 带 server 前缀避免冲突。Tool 的覆盖行为对用户无感知——建议至少输出 info 日志 |
| **工具审批可扩展性** | ⚠️ 中等 | 缝隙 2 已指出 MCP 工具无法加入审批。长期来看，审批应为 per-tool 可配置属性，而非硬编码 |
| **MCP 协议版本演进** | ✅ 好 | 使用官方 `mcp` SDK 处理协议协商。SDK 升级即可兼容新版本，无需自研协议适配 |
| **MCP 传输扩展** | ✅ 好 | 当前 stdio + Streamable HTTP 双传输。若未来出现 WebSocket 传输，只需在 `McpClient.connect()` 中新增分支 |
| **热重载** | ✅ 好 | `McpClient.shutdown()` + `MCPToolProvider.start()` 提供了重载的原子操作。§10 标记为后续优化 |

### 架构亮点（v2 保留 + 新增）

1. **P5 接口对齐完美**：`ToolExecutor.get_definitions()` / `get_handler()` / `execute()` 三个转发方法在 P5 已实现，MCP 集成零侵入 AgentLoop
2. **三个独立 Handler**：消除 cast 模式，类型安全，各自持有确定的 info 类型
3. **`_expand_env()` 原生支持嵌套 dict**：MCP headers 中的 `${MCP_API_KEY}` 自动展开，无需额外代码
4. **非文本 content 三级降级**：`from_mcp()` / `from_resource_result()` / `from_prompt_result()` 三个类方法各司其职
5. **`/mcp` 状态三态可见**：pending/connected/failed，启动中的 server 对用户透明
6. **server 前缀命名**：Resource/Prompt 的 `read_{server}_{name}` / `prompt_{server}_{name}` 避免跨 server 命名冲突

---

## 四、修正验收清单（审计员1号问题复查）

| # | 审计员1号问题 | v2 修正状态 | 备注 |
|---|-------------|-----------|------|
| 缺陷 1 | MCP 配置双位置定义 | ✅ 已修正 | 集中在 `config/settings.py`，无 `mcp/config.py` |
| 缺陷 2 | McpClient 缺少 read_resource/get_prompt | ✅ 已修正 | §3.2 补充两个方法 |
| 缺陷 3 | Provider 访问 client._config | ✅ 已修正 | 新增 `get_tool_timeout()` 公开方法 |
| 缝隙 4 | Resource/Prompt 命名冲突 | ✅ 已修正 | 改为 `read_{server}_{name}` / `prompt_{server}_{name}` |
| 缝隙 5 | cast 模式类型不安全 | ✅ 已修正 | 拆分为三个独立 Handler |
| 缝隙 6 | 非文本 content 未处理 | ✅ 已修正 | `from_mcp()` 增加降级；新增两个类方法 |
| 缝隙 7 | /mcp 加载中状态不可见 | ✅ 已修正 | `_pending_servers` dict |
| 缺失项 3 | RESOURCE/PROMPT 测试覆盖 | ✅ 已修正 | §7 补充测试项 |
| 缺失项 5 | get_handler() 是否存在 | ✅ 确认 | P5 ToolExecutor 已有此方法 |
| 缺失项 6 | __init__.py 导出 | ✅ 已修正 | §3.5 新增 |

---

## 五、改进建议汇总

### 必须在编码前修正（阻塞 P6 开发启动）

| # | 建议 | 影响位置 |
|---|------|---------|
| 1 | 补充 `McpResourceInfo.from_mcp()` 和 `McpPromptInfo.from_mcp()` 类方法定义 | §3.3 |

### 建议在编码前确认（提升实施质量）

| # | 建议 | 影响位置 |
|---|------|---------|
| 2 | MCP 工具审批可控——从 config 读取 `approval_commands` 设置 Handler 的 `needs_approval` | §3.3, §3.4 |
| 3 | 三重超时嵌套增加设计意图注释 | §3.3 |
| 4 | `McpPromptInfo.from_mcp()` 保留参数原始类型，`definition()` 正确映射 type/required | §3.3 |
| 5 | `_cmd_tools()` 合并 P5 的 `[需审批]` 标记和 P6 的来源分组，保留审批标记显示 | §3.6 |
| 6 | `_cmd_tools()` 补充 `from dotclaw.tools.base import ToolSource` 导入声明 | §3.6 |

### 长期关注（不阻塞 P6，后续 Phase 追踪）

| # | 建议 | 备注 |
|---|------|------|
| 7 | Tool 同名覆盖时增加 info 日志——帮助排查跨 server 工具名冲突 | 实施时顺手加 |
| 8 | `McpToolResult.from_mcp()` 的 `mcp_result.isError` 确认 SDK 属性名 | 实施时验证 |

---

## 六、结论

**裁决：有条件通过。**

Phase 6 v2 计划在审计员 1 号的 10 项修正基础上，架构设计完整、与 P5 预留接口对齐度高、三 Handler 独立设计消除类型安全隐患、非文本 content 降级处理到位。

审计员 2 号新发现的 **1 个阻塞级缺陷**（`from_mcp()` 缺失）修正成本极低——只需在 §3.3 中补充两个类方法定义，约 30 行代码。5 个缝隙均在建议级别，不影响核心功能，但有 2 个（MCP 工具审批不可控、prompt 参数类型硬编码）建议修正以提升完成的完整性。

**若阻塞级缺陷 #1 修正，Phase 6 可以启动实施。**

> **给计划人员的行动项**：
> 1. 在 §3.3 中为 `McpResourceInfo` 和 `McpPromptInfo` 补充 `from_mcp()` 类方法（缺陷 1）
> 2. 评估 MCP 工具审批可控方案（缝隙 2，推荐方案 A）
> 3. 在 `McpPromptInfo.from_mcp()` 中保留参数原始类型（缝隙 4）
> 4. `_cmd_tools()` 保留 `[需审批]` 标记（缝隙 5）
>
> 以上修正完成后，即可启动 Phase 6 开发。

---

*文档版本：v2.0*
*审计日期：2026-06-03*
*审计人：审计员2号*

---

## 七、计划修正回执

> 查阅人：dotclaw开发工程师
> 查阅日期：2026-06-03
> 修正版文档：`phase6-roadmap.md`（审计修正版 v2）

### 修正处置汇总

| 审计编号 | 审计要点 | 处置 | 说明 |
|---------|---------|------|------|
| 缺陷 1 | McpResourceInfo / McpPromptInfo 缺少 `from_mcp()` | ✅ 采纳 | §3.3 补充两个类方法，保留原始类型（type）和 required 属性 |
| 缝隙 2 | MCP 工具审批不可控——`needs_approval=False` 硬编码 | ✅ 采纳（方案 A） | 三个 Handler 构造函数新增 `needs_approval` 参数；Provider 从 `approval_commands` 计算并传递；main.py 传入 `approval_commands` |
| 缝隙 3 | 三重超时嵌套冗余 | ✅ 加注释 | 保留 belt-and-suspenders 设计，在 McpToolCallHandler.execute() 增加设计意图注释 |
| 缝隙 4 | McpPromptHandler 参数类型硬编码为 string | ✅ 采纳 | `from_mcp()` 保留 `type` 字段；`definition()` 正确映射 type/required |
| 缝隙 5 | `_cmd_tools()` 丢失 `[需审批]` 标记 | ✅ 采纳 | MCP 工具列表也显示 `[需审批]` 标记 |
| 缝隙 6 | `ToolSource` 导入缺失 | ✅ 采纳 | main.py 补充 `from dotclaw.tools.base import ToolSource` |

### 不采纳项

无。全部 6 项审计要点均已采纳或加注释。

### 额外修正

| 修正项 | 说明 |
|-------|------|
| 设计原则 | 审批描述从“默认不审批”改为“默认不审批，用户可通过 approval_commands 按需配置” |
| 验收第 14 条 | 从“审批一致”改为“审批可控”，明确说明 needs_approval + approval_commands 双重 AND 门 |
| 注意事项第 8 条 | 同名工具覆盖时输出 info 日志（采纳前瞻性建议） |
| 注意事项新增 14-16 | 审批可控原则、SDK 属性确认、ToolSource 导入声明 |

### 结论

审计员2号的 6 项建议全部合理，已全部融入开发计划。阻塞级缺陷（from_mcp 缺失）和两个关键缝隙（审批可控、prompt 参数类型）均已修正。Phase 6 可以启动实施。
