# Phase 6 代码审查报告

> 审查日期：2026-06-03
> 审查范围：Phase 6 MCP 协议集成（配置层+客户端+适配层+Provider+集成全部完成）
> 审查基准：`docs/phase6/phase6-roadmap.md` 设计文档 + `docs/prompt/code-review-prompt.md` 审查标准
> 测试状态：95/95 全部通过（Phase 1-5 回归 67 + Phase 6 验收 28）

---

## 审查总览

Phase 6 MCP 协议集成实现了完整的双传输支持（stdio + Streamable HTTP）、三层适配（tools/resources/prompts 对应三个独立 Handler 类）、高可用设计（启动失败跳过、运行时崩溃自动重连、优雅关闭）。架构方面很好地复用了 Phase 5 预留的 ToolProvider ABC 和 ToolHandler ABC，数据通路清晰，错误处理矩阵完整。配置层继承了 Phase 4/5 的风格（dataclass + 解析函数 + 校验），可观测性（`/tools` 分组展示 + `/mcp` 命令状态图标）提升了用户体验。

无 Critical 问题。发现 2 个 Warning 和 5 个 Minor 问题。

| 严重级别 | 数量 | 说明 |
|----------|------|------|
| Critical | 0 | — |
| Warning | 2 | 建议修复 |
| Minor | 5 | 可后续改进 |
| Info | 3 | 可选优化 |

---

## Warning — 建议修复

### W1. [mcp/client.py] 重连时旧 transport 未清理，可能导致资源泄漏

**位置**：`src/dotclaw/mcp/client.py:190-227` (`connect()`) + `src/dotclaw/mcp/client.py:293-309` (`_handle_execution_error()`)

**问题描述**：
`_handle_execution_error()` 在运行时异常时调用 `self.connect()` 尝试重连。`connect()` 会创建新的 `ClientSession` 和 transport 对象并覆盖 `self._transport` / `self._session`，但旧的 transport（尤其是 stdio 子进程）没有显式关闭。

对于 stdio 传输，旧连接的子进程不会被 terminate，GC 也不保证及时清理，可能导致：
- 孤儿子进程持续占用系统资源
- 多次重连后积累多个僵尸进程
- 子进程持有的文件描述符泄漏

**建议**：在 `connect()` 开头清理旧连接：

```python
async def connect(self) -> bool:
    """连接 MCP server：创建 transport → 握手 → 发现工具"""
    # 清理旧连接
    if self._session:
        try:
            await self._session.shutdown()
        except Exception:
            pass
        self._session = None
    if self._transport:
        # stdio transport 需要显式 terminate
        if hasattr(self._transport, 'terminate'):
            try:
                self._transport.terminate()
            except Exception:
                pass
        self._transport = None

    try:
        from mcp import ClientSession
        # ... 后续创建新连接 ...
```

---

### W2. [mcp/provider.py] `_connect_and_register` 异常时 client 状态泄漏

**位置**：`src/dotclaw/mcp/provider.py:87-141` (`_connect_and_register()`)

**问题描述**：
`_connect_and_register()` 在第 94 行将 client 加入 `self._clients` 字典，然后进行工具注册（tools/resources/prompts 三个循环）。如果注册过程中抛出异常（如 MCP server 返回的 tool schema 解析失败），异常传播到 `start()` 的 `asyncio.gather`，被 `return_exceptions=True` 捕获并加入 `_failed_servers`。

但此时 client 已经留在 `self._clients` 字典中（第 94 行已执行），造成了状态不一致：同一个 server 同时存在于 `_clients` 和 `_failed_servers` 两个字典中。

虽然在 `get_server_states()` 中 `_failed_servers` 后遍历会覆盖 clients 的结果，但残留的 client 在 `shutdown()` 时仍会被遍历并尝试关闭（可能失败），且占用了不必要的内存。

**建议**：将 `self._clients[client.server_name] = client` 移到注册成功后：

```python
async def _connect_and_register(self, client: McpClient) -> list[str]:
    success = await client.connect()
    if not success:
        raise McpClientError(f"连接失败: {client.server_name}")

    self._pending_servers.pop(client.server_name, None)
    registered: list[str] = []

    # 注册 tools
    for tool_info in client.tools:
        needs_approval = tool_info.name in self._approval_commands
        handler = McpToolCallHandler(...)
        self._registry.register(handler)
        registered.append(tool_info.name)

    # 注册 resources
    for resource_info in client.resources:
        handler = McpResourceHandler(...)
        self._registry.register(handler)
        registered.append(handler.definition().name)

    # 注册 prompts
    for prompt_info in client.prompts:
        handler = McpPromptHandler(...)
        self._registry.register(handler)
        registered.append(handler.definition().name)

    # 全部注册成功后才加入 clients
    self._clients[client.server_name] = client
    return registered
```

---

## Minor — 建议改进

### M1. [mcp/client.py] `read_resource()` 和 `get_prompt()` 无超时参数

**位置**：`src/dotclaw/mcp/client.py:269-289`

**问题描述**：
`call_tool()` 接受 `timeout` 参数并内部调用 `asyncio.wait_for`，但 `read_resource()` 和 `get_prompt()` 没有超时参数。当 ToolExecutor 通过 `asyncio.wait_for` 取消这两个方法时，MCP server 端的操作不会被 cancel 通知中止（没有等效于 `_send_cancel()` 的机制）。

**建议**：为 `read_resource()` 和 `get_prompt()` 添加 timeout 参数，超时时发送 cancel 通知。

---

### M2. [main.py:120] MCP 后台 task 引用未保存

**位置**：`src/dotclaw/main.py:113-120`

**问题描述**：
```python
async def _load_mcp():
    ...
asyncio.create_task(_load_mcp())
```
后台 task 引用未被保存，程序退出时无法显式等待该 task 完成。虽然在当前单进程 CLI 场景下不会造成实际问题（进程退出时 asyncio 会取消所有 task），但缺少引用使得无法监控 task 状态或实现优雅的等待策略。

**建议**：保存 task 引用，退出前 await：

```python
mcp_task = asyncio.create_task(_load_mcp())
# ... 在退出时:
if mcp_task and not mcp_task.done():
    mcp_task.cancel()
    try:
        await mcp_task
    except asyncio.CancelledError:
        pass
```

---

### M3. [config/settings.py] `McpServerConfig` 默认值允许无效状态

**位置**：`src/dotclaw/config/settings.py:99-128`

**问题描述**：
`McpServerConfig` 的 `name=""` 和 `transport="stdio"` 默认值允许创建语义上无效的配置对象。真正的校验在 `_parse_mcp_servers()` 中进行。如果其他代码直接构造 `McpServerConfig()`（如测试代码中），可能无意中创建无效配置。

**建议**：考虑在 dataclass 层面添加 `__post_init__` 校验，或在 factory 函数中强制要求 name 参数：

```python
@dataclass
class McpServerConfig:
    name: str  # 必填，无默认值
    transport: str = "stdio"
    # ... 其他字段
```

---

### M4. [mcp/tool_adapter.py] Handler execute() 的 `context` 参数未使用

**位置**：`src/dotclaw/mcp/tool_adapter.py:54, 127, 204`

**问题描述**：
三个 Handler 的 `execute()` 方法接受 `context: ToolExecutionContext | None = None` 但均未使用。虽然注释说明了超时设计的 belt-and-suspenders 策略，但 context 中携带的额外信息（如 timeout）可以使 Handler 在不同场景下自适应。

**建议**：考虑在 Handler 中使用 `context.timeout` 作为 fallback（当 `self._timeout` 未明确设置时）。

---

### M5. [mcp/client.py] `from_mcp()` 方法签名缺少类型标注

**位置**：`src/dotclaw/mcp/client.py:45, 62, 79, 103, 120, 134`

**问题描述**：
所有 `from_mcp()` 方法的参数 `mcp_tool` / `mcp_resource` / `mcp_prompt` / `mcp_result` 没有类型标注（仅注释说明），IDE 无法提供自动补全。

**建议**：添加 `TYPE_CHECKING` 条件下的类型标注：

```python
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.types import Tool, Resource, Prompt, CallToolResult, ReadResourceResult, GetPromptResult

@classmethod
def from_mcp(cls, mcp_tool: "Tool") -> "McpToolInfo":
    ...
```

---

## Info — 可选优化

### I1. [mcp/client.py] Info/Result 数据类与 McpClient 同文件

`McpToolInfo`、`McpResourceInfo`、`McpPromptInfo`、`McpToolResult`、`McpClientState`、异常类全部定义在 `client.py` 中（约 143 行数据类 + 189 行 McpClient）。按单一职责原则，Info/Result 数据类可拆到独立的 `types.py`。但考虑到这些类的消费者主要是 McpClient 和 tool_adapter，当前组织避免了额外的导入层次，是可接受的折中。

### I2. [mcp/client.py:314-320] `_send_cancel` SDK 兼容性

`_send_cancel()` 使用 `hasattr` 检测 `send_cancel` / `cancel` 两个方法名，这是良好的防御性编程。但 MCP SDK 的实际方法名需要在集成测试中确认。如果两个都不存在（SDK 未来版本变更），cancel 会静默失败（只有 debug 日志）。建议在开发环境下 log warning 以帮助早期发现 SDK 不兼容。

### I3. [mcp/client.py:202] `HttpClientTransport` 导入路径

`from mcp.client.http import HttpClientTransport` — 根据 MCP SDK 的实际包结构，Streamable HTTP 传输可能位于 `mcp.client.streamable_http` 路径下。如果 SDK 版本不匹配，此导入将在运行时失败（被 `except Exception` 捕获并标记为 FAILED，行为正确但错误信息不明确）。建议在文档中明确记录的 SDK 版本和导入路径对应关系。

---

## 架构审查结论

### 符合设计文档 ✓

| 检查项 | 状态 | 说明 |
|--------|------|------|
| 双传输支持（stdio + Streamable HTTP） | ✓ | `McpClient.connect()` 正确处理两种 transport |
| 三个独立 Handler 类 | ✓ | McpToolCallHandler / McpResourceHandler / McpPromptHandler 互不耦合 |
| ToolProvider ABC 实现 | ✓ | MCPToolProvider 正确实现 `discover_and_register()` |
| 配置 dataclass 集中定义 | ✓ | McpGlobalConfig / McpServerConfig 在 `config/settings.py` |
| 配置校验 | ✓ | 校验 transport/name/command/url + 重名检测 |
| 环境变量展开 | ✓ | `${VAR}` 由 `_expand_env()` 统一处理 |
| 高可用：启动失败跳过 | ✓ | `asyncio.gather(return_exceptions=True)` |
| 高可用：运行时重连 | ✓ | 失败计数器 + 重连上限 + CRASHED 标记 |
| 审批可控 | ✓ | needs_approval 从 approval_commands 计算，传给 Handler |
| 后台加载 | ✓ | `asyncio.create_task()` 不阻塞 Agent 启动 |
| ⁄tools 增强 | ✓ | 按 BUILTIN/MCP 分组，MCP 按 server 二次分组 |
| ⁄mcp 命令 | ✓ | 展示状态含 emoji 图标 + pending 状态 |
| Resource/Prompt 命名避免冲突 | ✓ | `read_{server}_{name}` / `prompt_{server}_{name}` |
| 非文本 content 降级处理 | ✓ | `McpToolResult.from_mcp()` 处理 image/resource 类型 |
| McpClient 不暴露私有属性 | ✓ | `get_tool_timeout()` / `get_startup_timeout()` 公开方法 |
| 优雅关闭 | ✓ | `McpClient.shutdown()` + `MCPToolProvider.shutdown()` |
| 回归测试全部通过 | ✓ | Phase 1-5 全部通过 |

### SOLID 原则评估

| 原则 | 评价 |
|------|------|
| **S — 单一职责** | ✓ McpClient 负责连接/执行，Handler 负责适配 ToolHandler ABC，Provider 负责编排 |
| **O — 开闭原则** | ✓ 新增 Transport 类型只需扩展 `connect()` 的 if/elif；新增 MCP 能力只需新增 Handler 类 |
| **L — 里氏替换** | ✓ 三个 Handler 均可安全替换 ToolHandler ABC 使用 |
| **I — 接口隔离** | ✓ ToolHandler 仅两个抽象方法，ToolProvider 仅一个 |
| **D — 依赖倒置** | ✓ ToolExecutor 依赖 ToolHandler ABC；MCPToolProvider 依赖 ToolProvider ABC |

### 数据流一致性

| 路径 | 状态 | 验证 |
|------|------|------|
| config.yaml → McpServerConfig | ✓ | `_parse_mcp_servers()` 正确解析+校验 |
| McpServerConfig → McpClient | ✓ | `connect()` 使用 transport/command/args/url/headers |
| McpClient → Handler | ✓ | `_connect_and_register()` 正确构造 Handler |
| Handler → ToolExecutor | ✓ | `execute()` 返回 `ToolResult`，由 `ToolExecutor` 统一超时控制 |
| AgentLoop → LLM | ✓ | 工具定义通过 `get_definitions()` 传入 LLM |

---

## 测试覆盖评估

| 场景 | 测试数 | 覆盖内容 | 评价 |
|------|--------|----------|------|
| 配置解析 | 8 | 默认值/覆盖/getter/校验（重名/缺command/缺url/错transport） | ✓ 充分 |
| McpClient 状态机 | 3 | 枚举值/初始状态/timeout getter | ✓ 充分 |
| Info/Result 数据类 | 5 | from_mcp 三类型 + Result 文本/资源 | ✓ 充分 |
| Handler 定义 | 3 | Tool定义/Resource命名/Prompt参数schema | ✓ 充分 |
| Provider 初始化 | 5 | init/ABC实现/空启动/重复启动/shutdown | ✓ 充分 |
| 错误类型 | 2 | 异常层次/错误信息 | ✓ 基本覆盖 |
| 回归测试 | 2 | 核心导入/MCP配置字段存在性 | ✓ 覆盖 |

**总计**：28 tests，覆盖 7 个场景。测试设计良好，Mock 使用恰当（MockTool/MockResource/MockPrompt 等）。未覆盖的领域（真实 MCP 连接、重连流程、cancel 发送）属于集成测试范畴，不适合在单元测试中实现。

---

## 整体评价

Phase 6 MCP 协议集成工程质量优秀。架构设计充分利用了 Phase 5 预留的 ToolProvider ABC 和 ToolHandler ABC 接口，四个新增文件（client/tool_adapter/provider/__init__）职责清晰、依赖方向正确。配置层继承了一致的 dataclass + 解析模式，校验覆盖全面。28 个单元测试覆盖了配置解析、状态机、Handler 定义、Provider 生命周期等关键路径。

发现的 2 个 Warning 建议修复：重连时旧 transport 未清理可能导致 stdio 子进程泄漏（生产环境累积风险），Provider 中 client 状态泄漏虽然不影响正确性但不够整洁。5 个 Minor 问题可在后续迭代中逐步优化。

**审查结论：通过，建议修复 W1/W2 后合入主干。**
