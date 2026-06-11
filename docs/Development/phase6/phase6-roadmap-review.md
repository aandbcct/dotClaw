# Phase 6 开发计划审计报告

> 审计人：开发架构师
> 审计日期：2026-06-03
> 审计对象：`docs/phase6/phase6-roadmap.md` — "MCP 协议集成"
> 代码基线：P5 完成（ToolRegistry/Executor/Handler 三层分离、ToolSource.MCP 枚举已定义）
> 协议版本：MCP 2025-03（Streamable HTTP）
> **状态：已查阅 ✅**

---

## 总体评价

Phase 6 计划与 Phase 5 预留的扩展点高度对齐——`ToolSource.MCP` 枚举、`ToolProvider` ABC、`ToolsConfig.mcp_enabled` 全部在 P5 中就位，MCP 集成只需实现接口即可插入现有工具链路。架构设计层次分明：`McpClient`（连接层）→ `McpToolHandler`（适配层）→ `MCPToolProvider`（编排层）→ ToolExecutor（调度层），每一层职责清晰。

存在 **3 个结构性缺陷**和 **4 个设计缝隙**，其中 #1（配置双位）和 #2（缺失方法）是阻塞级问题。

---

## 一、结构性缺陷（🔴 阻塞级）

### 缺陷 1：MCP 配置 dataclass 双位置定义

**位置**：§3.1 vs §4.1

**问题描述**：

路线图在两个地方定义了相同的 dataclass：

| 位置 | 定义内容 |
|------|---------|
| §3.1 `mcp/config.py` | `McpGlobalConfig`、`McpServerConfig`、`load_mcp_config()` |
| §4.1 `config/settings.py` | `McpGlobalConfig`、`McpServerConfig`、`ToolsConfig.mcp_global`、`ToolsConfig.mcp_servers` |

这与 P4 审计中发现的 `MemoryConfig` 双位置问题**完全相同**。项目约定是配置 dataclass 集中在 `config/settings.py`。如果 `mcp/config.py` 定义它们、`config/settings.py` 再 import，会形成 `mcp → config → mcp` 的循环依赖风险——`settings.py` 的 `_expand_env()` 需要处理 `${MCP_API_KEY}` 等变量，而 `mcp/config.py` 的环境变量展开又依赖 settings.py。

**影响**：循环导入风险 + 配置约定分歧。

**改进建议**：

**方案 A（推荐，与 P4 一致）**：删除 `mcp/config.py`，所有 MCP 配置 dataclass 完全定义在 `config/settings.py`：

```python
# config/settings.py

@dataclass
class McpGlobalConfig:
    startup_timeout: float = 4.0
    tool_timeout: float = 60.0
    restart_on_crash: bool = True
    max_restart_attempts: int = 3

@dataclass
class McpServerConfig:
    name: str
    transport: str
    command: str = ""
    args: list = field(default_factory=list)
    url: str = ""
    headers: dict = field(default_factory=dict)
    startup_timeout: float | None = None
    tool_timeout: float | None = None
    restart_on_crash: bool | None = None
    max_restart_attempts: int | None = None

    def get_startup_timeout(self, g: McpGlobalConfig) -> float:
        return self.startup_timeout if self.startup_timeout is not None else g.startup_timeout

    def get_tool_timeout(self, g: McpGlobalConfig) -> float:
        return self.tool_timeout if self.tool_timeout is not None else g.tool_timeout
    # ... 其他 get_* 方法

@dataclass
class ToolsConfig:
    # ... 现有字段 ...
    mcp_global: McpGlobalConfig = field(default_factory=McpGlobalConfig)
    mcp_servers: list[McpServerConfig] = field(default_factory=list)
```

配置加载逻辑（`load_mcp_config` 的功能）直接放在 `_raw_to_config()` 中，与其他配置段一起解析。`mcp/` 包从 `config.settings` import dataclass，不反向依赖。

---

### 缺陷 2：`McpClient` 缺少 `read_resource()` 和 `get_prompt()` 方法

**位置**：§3.2 vs §3.3

**问题描述**：

`McpToolHandler.execute()` 在处理 RESOURCE 和 PROMPT 类型时调用了 `McpClient` 上不存在的方法：

```python
# §3.3 — McpToolHandler.execute()
elif self._tool_type == McpToolType.RESOURCE:
    result = await self._client.read_resource(uri)       # ← 不存在

else:  # PROMPT
    result = await self._client.get_prompt(name, args)   # ← 不存在
```

但 `McpClient` 类（§3.2）只定义了 `connect()`、`call_tool()`、`_handle_execution_error()`、`shutdown()`。没有 `read_resource()` 也没有 `get_prompt()`。

**影响**：RESOURCE 和 PROMPT 类型的工具无法被调用，运行时 `AttributeError`。

**改进建议**：

在 `McpClient` 中补充两个方法：

```python
# mcp/client.py — McpClient 新增方法

async def read_resource(self, uri: str) -> McpToolResult:
    """读取 MCP resource"""
    try:
        result = await self._session.read_resource(uri)
        return McpToolResult.from_mcp(result)
    except Exception as e:
        return McpToolResult(content="", is_error=True, error_message=str(e))

async def get_prompt(self, name: str, arguments: dict) -> McpToolResult:
    """获取 MCP prompt"""
    try:
        result = await self._session.get_prompt(name, arguments)
        return McpToolResult.from_mcp(result)
    except Exception as e:
        return McpToolResult(content="", is_error=True, error_message=str(e))
```

> 注：上述方法中的 SDK 方法名（`read_resource`、`get_prompt`）需根据实际 `mcp` SDK API 调整。路线图 §9.4-9.5 已标注此事。

---

### 缺陷 3：`MCPToolProvider` 访问 `McpClient._config` 私有属性

**位置**：§3.4 line 717

**问题描述**：

```python
# §3.4 — MCPToolProvider._connect_and_register()
handler = McpToolHandler(
    client=client,
    tool_type=McpToolType.TOOL,
    tool_info=tool_info,
    timeout=client._config.get_tool_timeout(self._global),  # ← 访问私有属性
)
```

`_config` 是 `McpClient` 的私有属性（以下划线开头）。`MCPToolProvider` 从外部访问它打破了封装。如果 `McpClient` 的内部实现变更，Provider 也会受影响。

**改进建议**：

在 `McpClient` 上暴露公开方法：

```python
class McpClient:
    def get_timeout(self) -> float:
        """返回工具调用超时（秒）"""
        return self._config.get_tool_timeout(self._global)
```

然后将 `client._config.get_tool_timeout(...)` 改为 `client.get_timeout()`。

---

## 二、设计缝隙（🟡 重要）

### 缝隙 4：Resource 命名可能跨 server 冲突

**位置**：§3.3 命名策略

**问题描述**：

计划中 Resource 的命名规则是 `read_{name}`：

| MCP 类型 | 命名 | 冲突风险 |
|---------|------|---------|
| Tool | 原名 | 同名的后覆盖先（设计如此） |
| Resource | `read_{name}` | 两个 server 都有 `config` resource → 都叫 `read_config` → 后注册覆盖前 |
| Prompt | `prompt_{name}` | 同上 |

虽然计划 §9.8 接受"同名覆盖"设计，但 Resource 的冲突风险高于 Tool——MCP servers 的 tool 名通常有业务含义，但 resource 名（如 `config`、`data`、`schema`）非常容易跨 server 重复。

**改进建议**：

将命名策略从 `read_{name}` 改为 `read_{server}_{name}`：

```python
# McpToolHandler (RESOURCE) — 命名带 server 前缀避免跨 server 冲突
elif self._tool_type == McpToolType.RESOURCE:
    server_name = self._definition.metadata["server"]
    resource_name = info.name
    tool_name = f"read_{server_name}_{resource_name}"
```

同理，Prompt 的 `prompt_{name}` 也建议改为 `prompt_{server}_{name}`。如果保持原名策略（设计简洁性），至少在路线图中说明冲突风险。

---

### 缝隙 5：`McpToolHandler` 类型联合 + cast 模式不够类型安全

**位置**：§3.3

**问题描述**：

```python
class McpToolHandler(ToolHandler):
    def __init__(
        self,
        client: McpClient,
        tool_type: McpToolType,
        tool_info: McpToolInfo | McpResourceInfo | McpPromptInfo,  # 联合类型
        ...
    ):
        self._tool_info = tool_info  # 具体类型取决于 tool_type

    def definition(self) -> ToolDefinition:
        if self._tool_type == McpToolType.TOOL:
            info = cast(McpToolInfo, self._tool_info)   # 强制转型
        elif self._tool_type == McpToolType.RESOURCE:
            info = cast(McpResourceInfo, self._tool_info)
        ...
```

`cast()` 是 Python 的类型提示作弊器——它告诉类型检查器"相信我"，但运行时不做任何验证。如果调用方传了错误的组合（比如 `tool_type=TOOL` 但 `tool_info=McpResourceInfo`），不会有错误提示，但字段访问会出错。

**改进建议**：

以下二选一：

- **方案 A（推荐）**：拆分 Handler。三个独立的 Handler 类（`McpToolHandler`、`McpResourceHandler`、`McpPromptHandler`），每个只接受确定的类型，不需要 cast：

```python
class McpToolHandler(ToolHandler):       # 只处理 TOOL
    def __init__(self, client, tool_info: McpToolInfo, ...): ...

class McpResourceHandler(ToolHandler):   # 只处理 RESOURCE
    def __init__(self, client, info: McpResourceInfo, ...): ...

class McpPromptHandler(ToolHandler):     # 只处理 PROMPT
    def __init__(self, client, info: McpPromptInfo, ...): ...
```

- **方案 B**：保持单一 Handler，但在 `__init__` 中添加运行时校验：

```python
def __init__(self, client, tool_type, tool_info, timeout=60.0):
    if tool_type == McpToolType.TOOL and not isinstance(tool_info, McpToolInfo):
        raise TypeError(f"TOOL 需要 McpToolInfo，实际: {type(tool_info)}")
    # ... 类似校验 ...
```

建议方案 A——三个独立 Handler 类更清晰，不需要 cast，`_connect_and_register()` 中根据 `tool_type` 创建对应的 Handler。

---

### 缝隙 6：MCP content 非文本类型未处理

**位置**：§3.3 — `McpToolResult.from_mcp()`

**问题描述**：

```python
@classmethod
def from_mcp(cls, mcp_result) -> "McpToolResult":
    text_parts = []
    for item in mcp_result.content:
        if hasattr(item, 'text'):
            text_parts.append(item.text)
    return cls(content="\n".join(text_parts), ...)
```

MCP 协议支持三种 content 类型：`text`、`image`、`resource`。当前只处理了 `text`，遇到 image 或 resource 类型的 content 时，`text_parts` 为空列表，`content` 变成空字符串——Agent 无法看到任何有用的输出。

**改进建议**：

```python
for item in mcp_result.content:
    if hasattr(item, 'text'):
        text_parts.append(item.text)
    elif hasattr(item, 'data') and hasattr(item, 'mimeType'):
        # image / resource 类型：输出描述
        mime = getattr(item, 'mimeType', 'unknown')
        text_parts.append(f"[{mime} content, {len(item.data)} bytes]")
    else:
        text_parts.append(f"[unsupported content type: {type(item).__name__}]")
```

---

### 缝隙 7：`/mcp` 命令在后台加载完成前显示的信息不完整

**位置**：§3.5

**问题描述**：

MCP 工具通过 `asyncio.create_task` 后台加载。用户在 CLI 启动后立即输入 `/mcp` 时，后台 task 可能还没完成连接和工具注册。`_cmd_mcp()` 此时只能显示"加载中"的状态，但计划中的 `get_server_states()` 返回的 `_clients` dict 在加载完成前是空的，`_failed_servers` 也可能还是空的。

当前代码：

```python
states = mcp_provider.get_server_states()
if not states:
    channel.print_info("(未配置 MCP server)")
```

如果 3 个 servers 中有 2 个还在连接中、1 个已完成，`get_server_states()` 只返回 1 个（已完成的那一个）。用户看到的是一部分信息。

**改进建议**：

在 `MCPToolProvider` 中增加 `_pending_servers` dict，记录正在连接的 servers：

```python
async def _connect_and_register(self, client: McpClient) -> list[str]:
    self._pending_servers[client.server_name] = "connecting"
    try:
        success = await client.connect()
        # ...
        del self._pending_servers[client.server_name]
    except Exception:
        self._pending_servers[client.server_name] = "failed"
        raise
```

然后 `get_server_states()` 也返回 pending 状态：

```python
def get_server_states(self):
    result = {}
    for name in self._pending_servers:
        result[name] = (McpClientState.STARTING, self._pending_servers[name])
    # ... 已有逻辑
```

---

## 三、前瞻性审查

### 长期发展性评估

| 维度 | 评分 | 说明 |
|------|------|------|
| **P7 Skill 工具化** | ✅ 好 | `ToolSource.SKILL` 枚举已预留，`SkillToolProvider` 只需实现同一接口 |
| **多 MCP server 扩展** | ✅ 好 | `mcp_servers` 列表 + 并行连接 + 独立状态管理 |
| **热重载** | ✅ 好 | P6 实现的基础连接/断开为热重载提供了 API（§10 标记为 P7+ 扩展） |
| **工具条件过滤** | ⚠️ 中等 | §10 标记为后续扩展，当前所有 MCP 工具无条件注册 |
| **MCP logging 推送** | ✅ 好 | §10 标记为后续扩展，不影响当前功能 |
| **MCP sampling** | ✅ 好 | §10 明确标记为低优先级，"极少用"，不放 scope 正确 |

### 架构亮点

1. **双传输支持**：stdio + Streamable HTTP 覆盖了本地工具和远程服务的所有场景
2. **后台异步加载**：`asyncio.create_task` 不阻塞 Agent 启动——用户体验设计到位
3. **渐进降级**：单个 server 失败不影响其他 server 和 Agent 主流程
4. **状态机清晰**：STARTING → CONNECTED / FAILED / CRASHED / SHUTDOWN 五个状态，转移规则明确
5. **`/mcp` 可观测性**：新命令提供连接状态面板，运维友好
6. **`/tools` 分组增强**：按来源分组展示，用户能区分内置工具和 MCP 工具
7. **官方 SDK 依赖**：用 `mcp` Python SDK 而非自研协议——正确的工程取舍

---

## 四、缺失项清单

| # | 缺失项 | 影响 | 建议 |
|---|--------|------|------|
| 1 | `McpClient.read_resource()` 方法 | 🔴 Resource 类型工具无法调用 | 在 §3.2 补充方法定义 |
| 2 | `McpClient.get_prompt()` 方法 | 🔴 Prompt 类型工具无法调用 | 在 §3.2 补充方法定义 |
| 3 | RESOURCE/PROMPT 测试覆盖 | 🟡 两种工具类型未经测试 | 在 §7.1 补充测试项 |
| 4 | `McpToolResult.from_mcp()` 非文本 content 处理 | 🟡 遇到 image/resource 类型返回空字符串 | 见缝隙 6 |
| 5 | `_cmd_tools` 使用 `tool_executor.get_handler()` — 当前 ToolExecutor 有此方法吗？ | 🟡 需要确认 P5 executor 是否有此方法 | 如无，需从 registry 获取 |
| 6 | `__init__.py` 导出列表 | 🟢 公共 API 未定义 | mcp 包导出 MCPToolProvider、McpClient、load_mcp_config |

---

## 五、改进建议汇总

### 必须在编码前修正（阻塞 P6 开发启动）

| # | 建议 | 影响位置 |
|---|------|---------|
| 1 | MCP 配置 dataclass 统一放在 `config/settings.py`，删除 `mcp/config.py` 中的重复定义 | §3.1, §4.1 |
| 2 | 在 `McpClient` 中补充 `read_resource()` 和 `get_prompt()` 方法 | §3.2 |
| 3 | `MCPToolProvider` 不访问 `client._config` 私有属性，改为公开方法 | §3.2, §3.4 |

### 建议在编码前确认（提升实施质量）

| # | 建议 | 影响位置 |
|---|------|---------|
| 4 | Resource/Prompt 命名加 server 前缀避免跨 server 冲突，或文档说明冲突风险 | §3.3 |
| 5 | 拆分 `McpToolHandler` 为三个独立类，消除 cast 模式 | §3.3 |
| 6 | `McpToolResult.from_mcp()` 处理非文本 content 类型 | §3.3 |
| 7 | `/mcp` 命令支持展示"连接中"状态 | §3.4, §3.5 |

---

> **给计划人员的行动项**：优先解决缺陷 1（配置双位——与 P4 相同的问题）和缺陷 2（缺失方法——会导致运行时崩溃）。Phase 6 计划与 P5 预留接口的对齐程度是六个 Phase 中最高的——所有扩展点均已就位，只需实现接口即插即入。

---

## 六、计划修正回执

> 查阅人：dotclaw开发工程师
> 查阅日期：2026-06-03
> 修正版文档：`phase6-roadmap.md`（审计修正版）

### 修正处置汇总

| 审计编号 | 审计要点 | 处置 | 说明 |
|---------|---------|------|------|
| 缺陷 1 | MCP 配置 dataclass 双位置定义 | ✅ 采纳 | 配置 dataclass 统一在 `config/settings.py`，`mcp/` 包只 import 不定义，避免循环依赖 |
| 缺陷 2 | McpClient 缺少 read_resource / get_prompt | ✅ 采纳 | §3.2 补充两个方法，RESOURCE/PROMPT 类型工具可正常调用 |
| 缺陷 3 | Provider 访问 client._config 私有属性 | ✅ 采纳 | McpClient 新增 `get_tool_timeout()` 公开方法 |
| 缝隙 4 | Resource/Prompt 命名跨 server 冲突 | ✅ 采纳 | 命名改为 `read_{server}_{name}` / `prompt_{server}_{name}` |
| 缝隙 5 | cast 模式类型不安全 | ✅ 采纳 | 拆分为三个独立 Handler：McpToolCallHandler / McpResourceHandler / McpPromptHandler |
| 缝隙 6 | 非文本 content 类型未处理 | ✅ 采纳 | from_mcp() 增加非文本降级；新增 from_resource_result() / from_prompt_result() |
| 缝隙 7 | /mcp 加载中状态不可见 | ✅ 采纳 | Provider 新增 _pending_servers，get_server_states() 返回 pending 状态 |
| 缺失项 3 | RESOURCE/PROMPT 测试覆盖 | ✅ 采纳 | §7 补充测试项 |
| 缺失项 5 | tool_executor.get_handler() 是否存在 | ❌ 不适用 | Phase 5 ToolExecutor 已有此方法，无问题 |
| 缺失项 6 | __init__.py 导出列表 | ✅ 采纳 | §3.5 新增导出定义 |

### 未采纳项说明

- **缺失项 5**：确认 Phase 5 的 `ToolExecutor` 已有 `get_handler()` 方法（直接转发 `registry.get()`），不存在问题。

### 新增内容

审计报告中未覆盖但修正版新增的设计点：
1. `McpToolResult` 新增 `from_resource_result()` 和 `from_prompt_result()` 两个类方法，分别处理 resources/read 和 prompts/get 的返回格式
2. 验收标准从 14 条扩充至 16 条（新增非文本 content 降级、/mcp 加载中状态可见）
3. 新增 §3.5 `mcp/__init__.py` 包入口导出定义
4. 新增 §四「文件清单」独立章节，明确新增/修改/测试文件列表
