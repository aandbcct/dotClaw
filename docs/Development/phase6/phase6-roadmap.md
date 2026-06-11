# Phase 6 详细开发文档：MCP 协议集成

> 创建时间：2026-06-03
> 状态：已完成 ✅（2026-06-03）
> 依赖：Phase 1-5 已完成
> 变更日志：[docs/phase6/phase6-record.md](phase6-record.md)
> 协议版本：MCP 2025-03（最新规范，Streamable HTTP 传输）

---

## 一、开发目的

Phase 6 实现 MCP（Model Context Protocol）客户端，让 dotClaw 能够通过 MCP 协议动态调用外部工具，扩展 Agent 能力边界。

**核心目标**：
1. 双传输支持——stdio（本地子进程）+ Streamable HTTP（远程服务）
2. 统一工具抽象——MCP tools/resources/prompts 统一包装为 ToolHandler，复用 Phase 5 工具链路
3. 高可用设计——启动失败跳过、运行时崩溃自动重连、失败计数器、优雅关闭
4. 后台加载——MCP 连接不阻塞 Agent 启动，工具注册完成后自动可用
5. 可观测性——`/tools` 展示来源标记，`/mcp` 命令查看连接状态（含加载中状态）

**设计原则**：
- 复用 Phase 5 预留接口——ToolProvider ABC / ToolHandler ABC / config.tools.mcp_enabled
- 同名覆盖——MCP 工具与内置工具同一命名空间，后注册覆盖前注册
- 审批一致——MCP 工具与内置工具审批逻辑一致，默认不审批，用户可通过 approval_commands 按需配置
- 官方 SDK——使用 `mcp` Python SDK 处理协议细节，避免重复造轮
- 配置集中——MCP 配置 dataclass 集中在 `config/settings.py`，与 P4 MemoryConfig 一致

---

## 二、架构总览

```
+------------------------------------------------------------------+
|                           main.py                                 |
|  启动时：                                                         |
|    1. ToolRegistry() -> register builtin handlers                |
|    2. MCPToolProvider(global_config, registry) -> 后台 task      |
|    3. ToolExecutor(registry, approval_mgr)                       |
|                                                                   |
|  后台 task:                                                       |
|    MCPToolProvider.discover_and_register()                       |
|      |                                                            |
|      +-- 遍历 mcp_servers 配置                                    |
|      +-- 创建 McpClient(transport)                               |
|      +-- initialize 握手 + capabilities 协商                      |
|      +-- tools/list + resources/list + prompts/list              |
|      +-- 包装为 Handler 子类注册到 registry                      |
|      +-- 持续管理 client 生命周期（重连/关闭）                     |
+------------------------------------------------------------------+
                              |
                              v
+------------------------------------------------------------------+
|                      MCPToolProvider                             |
|  (ToolProvider ABC 实现)                                         |
|                                                                  |
|  职责：                                                          |
|    - 编排：遍历 servers -> 创建 clients -> 注册 tools            |
|    - 生命周期：start / shutdown / health_check                   |
|    - 状态管理：clients + failed_servers + pending_servers        |
|    - 后台加载：asyncio.create_task                               |
+------------------------------------------------------------------+
                              |
                              v
+------------------------------------------------------------------+
|                         McpClient                                |
|  (基于 mcp SDK 封装)                                             |
|                                                                  |
|  职责：                                                          |
|    - Transport：stdio / Streamable HTTP                         |
|    - Session：initialize 握手 + capabilities 协商               |
|    - Discovery：tools/list, resources/list, prompts/list        |
|    - Execution：call_tool / read_resource / get_prompt          |
|    - Reliability：重连 + 失败计数器 + cancel 通知                |
|    - 公开接口：get_tool_timeout() / get_startup_timeout()       |
+------------------------------------------------------------------+
                              |
                              v
+------------------------------------------------------------------+
|          McpToolCallHandler / McpResourceHandler                 |
|              / McpPromptHandler                                  |
|  (ToolHandler ABC 实现 — 三个独立类)                              |
|                                                                  |
|  职责：                                                          |
|    - 各自持有 client + 确定类型的 info                           |
|    - execute() -> client 对应方法                                |
|    - 超时控制 + 错误处理                                         |
+------------------------------------------------------------------+
                              |
                              v
+------------------------------------------------------------------+
|                      ToolExecutor                                |
|  (Phase 5 已实现)                                                 |
|                                                                  |
|  复用现有逻辑：                                                   |
|    - registry.get(name) -> McpToolCallHandler 等                |
|    - approval.check(definition) -> 默认不审批                    |
|    - asyncio.wait_for(handler.execute(), timeout)               |
|    - 返回 ToolResult(output, is_error, error_code)              |
+------------------------------------------------------------------+
```

**数据通路（启动 + 运行时）**：

```
启动阶段：
  main.py
    → ToolRegistry()
    → register_all(tool_registry)           # 注册内置工具
    → MCPToolProvider(config, registry)     # 创建 Provider
    → asyncio.create_task(provider.start()) # 后台启动
    → AgentLoop(tool_executor, ...)
    
  后台 task:
    provider.start()
      → 遍历 mcp_servers
      → 记录 _pending_servers[name] = "connecting"
      → McpClient(server_config).connect()
        → stdio: create_subprocess_exec + SDK ClientSession
        → streamable_http: SDK ClientSession + HTTP transport
      → initialize 握手 (timeout=4s)
      → tools/list -> 包装为 McpToolCallHandler
      → resources/list -> 包装为 McpResourceHandler
      → prompts/list -> 包装为 McpPromptHandler
      → registry.register(handler)
      → 清除 _pending_servers[name]
      → 更新 provider._clients[server_name] = client

运行时：
  AgentLoop.run()
    → LLM returns tool_calls
    → ToolExecutor.execute(name, args)
      → registry.get(name) -> McpToolCallHandler
      → handler.execute(args)
        → client.call_tool(name, args, timeout=60s)
        → 超时? -> client.cancel() -> raise TimeoutError
      → return ToolResult(output)

关闭阶段：
  main.py 退出
    → provider.shutdown()
      → 遍历 _clients
      → client.shutdown() -> 发送 shutdown 通知
      → 等待子进程退出 / 关闭 HTTP 连接
```

---

## 三、模块设计

### 3.1 配置层

**文件位置**：`config/settings.py`（配置 dataclass 定义 + 解析逻辑）

> **设计决策**：MCP 配置 dataclass 集中定义在 `config/settings.py`，与 P4 MemoryConfig 一致。`mcp/` 包从 `config.settings` import dataclass，不反向依赖，避免循环导入风险。

**数据结构**：

```python
# config/settings.py — 新增

@dataclass
class McpGlobalConfig:
    """MCP 全局配置（默认值）"""
    startup_timeout: float = 4.0        # 握手超时（秒）
    tool_timeout: float = 60.0          # 工具调用超时（秒）
    restart_on_crash: bool = True       # 崩溃后是否自动重连
    max_restart_attempts: int = 3       # 最大重连次数

@dataclass
class McpServerConfig:
    """单个 MCP server 配置"""
    name: str                           # server 名称（唯一标识）
    transport: str                      # "stdio" | "streamable_http"
    
    # stdio 传输字段
    command: str = ""                   # 可执行命令
    args: list[str] = field(default_factory=list)
    
    # streamable_http 传输字段
    url: str = ""                       # HTTP endpoint
    headers: dict = field(default_factory=dict)  # 认证 headers
    
    # 覆盖全局配置（None 时使用全局默认）
    startup_timeout: float | None = None
    tool_timeout: float | None = None
    restart_on_crash: bool | None = None
    max_restart_attempts: int | None = None
    
    def get_startup_timeout(self, global_cfg: McpGlobalConfig) -> float:
        return self.startup_timeout if self.startup_timeout is not None else global_cfg.startup_timeout
    
    def get_tool_timeout(self, global_cfg: McpGlobalConfig) -> float:
        return self.tool_timeout if self.tool_timeout is not None else global_cfg.tool_timeout
    
    def get_restart_on_crash(self, global_cfg: McpGlobalConfig) -> bool:
        return self.restart_on_crash if self.restart_on_crash is not None else global_cfg.restart_on_crash
    
    def get_max_restart_attempts(self, global_cfg: McpGlobalConfig) -> int:
        return self.max_restart_attempts if self.max_restart_attempts is not None else global_cfg.max_restart_attempts

# ToolsConfig 扩展
@dataclass
class ToolsConfig:
    # ... 现有字段 ...
    
    # Phase 6 新增
    mcp_global: McpGlobalConfig = field(default_factory=McpGlobalConfig)
    mcp_servers: list[McpServerConfig] = field(default_factory=list)
```

**配置文件格式**（`config.yaml`）：

```yaml
tools:
  mcp_enabled: true
  mcp_global:                    # 全局默认配置
    startup_timeout: 4.0
    tool_timeout: 60.0
    restart_on_crash: true
    max_restart_attempts: 3
    
  mcp_servers:
    - name: filesystem
      transport: stdio
      command: npx
      args: ["-y", "@anthropic/mcp-server-filesystem", "/tmp"]
      # 不覆盖全局配置，使用默认值
      
    - name: remote-api
      transport: streamable_http
      url: http://localhost:8080/mcp
      headers:
        Authorization: "Bearer ${MCP_API_KEY}"
      tool_timeout: 120.0        # 单 server 覆盖
```

**解析逻辑**（在 `_raw_to_config()` 中）：

- 从 `tools.mcp_global` 解析 `McpGlobalConfig`
- 从 `tools.mcp_servers` 列表解析 `list[McpServerConfig]`
- 校验：transport 必须是 `stdio` 或 `streamable_http`
- 校验：stdio 必须有 command，streamable_http 必须有 url
- 校验：name 不能重复
- 环境变量展开：`${VAR}` 在 url / headers 中展开（由 `_expand_env()` 统一处理）

---

### 3.2 客户端层 `mcp/client.py`

**职责**：封装 mcp SDK 的 ClientSession，管理单个 MCP server 的连接、发现、执行、重连。

**核心类**：

```python
class McpClientState(str, Enum):
    STARTING = "starting"        # 启动中
    CONNECTED = "connected"      # 已连接
    CRASHED = "crashed"          # 崩溃（重连失败次数已达上限）
    FAILED = "failed"            # 启动失败（命令不存在/握手超时）
    SHUTDOWN = "shutdown"        # 已关闭

class McpClient:
    """单个 MCP server 客户端封装"""
    
    def __init__(
        self,
        config: McpServerConfig,
        global_config: McpGlobalConfig,
    ):
        self._config = config
        self._global = global_config
        self._session: ClientSession | None = None
        self._state = McpClientState.STARTING
        self._failure_count = 0
        self._tools: list[McpToolInfo] = []
        self._resources: list[McpResourceInfo] = []
        self._prompts: list[McpPromptInfo] = []
    
    @property
    def state(self) -> McpClientState:
        return self._state
    
    @property
    def server_name(self) -> str:
        return self._config.name
    
    @property
    def tools(self) -> list[McpToolInfo]:
        return self._tools
    
    @property
    def resources(self) -> list[McpResourceInfo]:
        return self._resources
    
    @property
    def prompts(self) -> list[McpPromptInfo]:
        return self._prompts
    
    # ---- 公开配置访问（不暴露 _config 私有属性）----
    
    def get_tool_timeout(self) -> float:
        """返回工具调用超时（秒）"""
        return self._config.get_tool_timeout(self._global)
    
    def get_startup_timeout(self) -> float:
        """返回握手超时（秒）"""
        return self._config.get_startup_timeout(self._global)
    
    # ---- 连接管理 ----
    
    async def connect(self) -> bool:
        """
        连接 MCP server：
        1. 创建 transport（stdio / streamable_http）
        2. 调用 SDK ClientSession.initialize() 握手
        3. 查询 tools/resources/prompts 列表
        
        返回：连接成功返回 True，失败返回 False
        """
        try:
            if self._config.transport == "stdio":
                transport = StdioClientTransport(
                    command=self._config.command,
                    args=self._config.args,
                )
            else:  # streamable_http
                transport = HttpClientTransport(
                    url=self._config.url,
                    headers=self._config.headers,
                )
            
            self._session = ClientSession(transport)
            await asyncio.wait_for(
                self._session.initialize(),
                timeout=self.get_startup_timeout(),
            )
            
            # 发现工具/资源/提示词
            await self._discover()
            
            self._state = McpClientState.CONNECTED
            self._failure_count = 0
            return True
            
        except asyncio.TimeoutError:
            self._state = McpClientState.FAILED
            logger.error(f"MCP server {self._config.name} 握手超时")
            return False
        except Exception as e:
            self._state = McpClientState.FAILED
            logger.error(f"MCP server {self._config.name} 启动失败: {e}")
            return False
    
    async def _discover(self):
        """发现 tools/resources/prompts"""
        tools_result = await self._session.list_tools()
        self._tools = [McpToolInfo.from_mcp(t) for t in tools_result.tools]
        
        # resources（server 可能不支持，容错处理）
        try:
            resources_result = await self._session.list_resources()
            self._resources = [McpResourceInfo.from_mcp(r) for r in resources_result.resources]
        except Exception:
            self._resources = []
        
        # prompts（server 可能不支持，容错处理）
        try:
            prompts_result = await self._session.list_prompts()
            self._prompts = [McpPromptInfo.from_mcp(p) for p in prompts_result.prompts]
        except Exception:
            self._prompts = []
    
    # ---- 工具执行 ----
    
    async def call_tool(
        self,
        tool_name: str,
        arguments: dict,
        timeout: float | None = None,
    ) -> McpToolResult:
        """
        调用工具（MCP tools/call）：
        1. 检查 state，crashed/failed 时抛出异常
        2. 调用 session.call_tool()
        3. 超时后发送 cancel 通知
        """
        if self._state in (McpClientState.CRASHED, McpClientState.FAILED, McpClientState.SHUTDOWN):
            raise McpUnavailableError(f"MCP server {self._config.name} 不可用（state={self._state}）")
        
        timeout_val = timeout or self.get_tool_timeout()
        
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(tool_name, arguments),
                timeout=timeout_val,
            )
            return McpToolResult.from_mcp(result)
        except asyncio.TimeoutError:
            # 发送 cancel 通知
            await self._send_cancel()
            raise
        except Exception as e:
            # 运行时异常，触发重连检查
            await self._handle_execution_error(e)
            raise
    
    async def read_resource(self, uri: str) -> McpToolResult:
        """
        读取 MCP resource（resources/read）。
        
        用于 McpResourceHandler 调用。
        """
        if self._state in (McpClientState.CRASHED, McpClientState.FAILED, McpClientState.SHUTDOWN):
            raise McpUnavailableError(f"MCP server {self._config.name} 不可用（state={self._state}）")
        
        try:
            result = await self._session.read_resource(uri)
            return McpToolResult.from_resource_result(result)
        except Exception as e:
            await self._handle_execution_error(e)
            raise
    
    async def get_prompt(self, name: str, arguments: dict) -> McpToolResult:
        """
        获取 MCP prompt（prompts/get）。
        
        用于 McpPromptHandler 调用。
        """
        if self._state in (McpClientState.CRASHED, McpClientState.FAILED, McpClientState.SHUTDOWN):
            raise McpUnavailableError(f"MCP server {self._config.name} 不可用（state={self._state}）")
        
        try:
            result = await self._session.get_prompt(name, arguments)
            return McpToolResult.from_prompt_result(result)
        except Exception as e:
            await self._handle_execution_error(e)
            raise
    
    # ---- 重连与关闭 ----
    
    async def _handle_execution_error(self, error: Exception):
        """
        处理执行错误：
        1. 判断是否需要重连
        2. 重连失败计数器递增
        3. 达到上限标记为 CRASHED
        """
        if not self._config.get_restart_on_crash(self._global):
            self._state = McpClientState.CRASHED
            return
        
        self._failure_count += 1
        max_attempts = self._config.get_max_restart_attempts(self._global)
        if self._failure_count >= max_attempts:
            self._state = McpClientState.CRASHED
            logger.error(f"MCP server {self._config.name} 重连失败次数已达上限")
            return
        
        logger.warning(f"MCP server {self._config.name} 尝试重连（{self._failure_count}/{max_attempts}）")
        success = await self.connect()
        if not success:
            self._state = McpClientState.CRASHED
    
    async def _send_cancel(self):
        """发送 cancel 通知（超时时调用）"""
        if self._session:
            try:
                await self._session.send_cancel()  # SDK 方法名待确认
            except Exception:
                logger.debug(f"MCP server {self._config.name} cancel 通知发送失败")
    
    async def shutdown(self):
        """优雅关闭：发送 shutdown 通知，等待退出"""
        if self._session:
            try:
                await self._session.shutdown()
            except Exception:
                pass
            self._session = None
        self._state = McpClientState.SHUTDOWN
```

**错误处理矩阵**：

| 场景 | 状态变化 | 后续行为 |
|------|---------|---------|
| 启动失败（命令不存在）| FAILED | 跳过该 server，不重试 |
| 握手超时 | FAILED | 跳过该 server，不重试 |
| 运行时 `call_tool` / `read_resource` / `get_prompt` 异常 | 触发重连 | 失败计数器 +1，达到上限 → CRASHED |
| 重连成功 | CONNECTED | 失败计数器清零 |
| 达到重连上限 | CRASHED | 后续调用返回 MCP_UNAVAILABLE |
| 主动 shutdown | SHUTDOWN | 不再可用 |

---

### 3.3 工具适配层 `mcp/tool_adapter.py`

**职责**：将 MCP 的 tools/resources/prompts 包装为 dotClaw 的 ToolHandler。拆分为三个独立 Handler 类，消除 cast 模式，提升类型安全。

**数据结构**：

```python
@dataclass
class McpToolInfo:
    """MCP 工具元信息"""
    name: str              # 工具名（原样使用，不加前缀）
    description: str
    input_schema: dict     # JSON Schema
    
    @classmethod
    def from_mcp(cls, mcp_tool) -> "McpToolInfo":
        return cls(
            name=mcp_tool.name,
            description=mcp_tool.description or "",
            input_schema=mcp_tool.inputSchema or {},
        )

@dataclass
class McpResourceInfo:
    """MCP 资源元信息"""
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
    """MCP 提示词元信息"""
    name: str
    description: str = ""
    arguments: list[dict] = field(default_factory=list)  # 提示词参数
    # 每个参数 dict 包含: name, description, required, type

    @classmethod
    def from_mcp(cls, mcp_prompt) -> "McpPromptInfo":
        """从 MCP SDK 的 Prompt 对象构建"""
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

@dataclass
class McpToolResult:
    """MCP 调用结果（统一）"""
    content: str           # 文本内容
    is_error: bool = False
    error_message: str = ""
    
    @classmethod
    def from_mcp(cls, mcp_result) -> "McpToolResult":
        """从 tools/call 结果构建"""
        text_parts = []
        for item in mcp_result.content:
            if hasattr(item, 'text'):
                text_parts.append(item.text)
            elif hasattr(item, 'data') and hasattr(item, 'mimeType'):
                # image / resource 类型：降级为描述文本
                mime = getattr(item, 'mimeType', 'unknown')
                text_parts.append(f"[{mime} content, {len(item.data)} bytes]")
            else:
                text_parts.append(f"[unsupported content type: {type(item).__name__}]")
        
        return cls(
            content="\n".join(text_parts),
            is_error=mcp_result.isError if hasattr(mcp_result, 'isError') else False,
        )
    
    @classmethod
    def from_resource_result(cls, mcp_result) -> "McpToolResult":
        """从 resources/read 结果构建"""
        text_parts = []
        for item in mcp_result.contents:
            if hasattr(item, 'text'):
                text_parts.append(item.text)
            elif hasattr(item, 'blob'):
                mime = getattr(item, 'mimeType', 'unknown')
                text_parts.append(f"[{mime} content, {len(item.blob)} bytes]")
            else:
                text_parts.append(f"[unsupported resource type: {type(item).__name__}]")
        return cls(content="\n".join(text_parts))
    
    @classmethod
    def from_prompt_result(cls, mcp_result) -> "McpToolResult":
        """从 prompts/get 结果构建"""
        text_parts = []
        for item in mcp_result.messages:
            if hasattr(item, 'content') and hasattr(item.content, 'text'):
                text_parts.append(f"[{item.role}] {item.content.text}")
            else:
                text_parts.append(f"[{getattr(item, 'role', 'unknown')}] {item}")
        return cls(content="\n".join(text_parts))
```

**三个独立 Handler 类**：

```python
class McpToolCallHandler(ToolHandler):
    """MCP tool 调用执行器（tools/call）"""
    
    def __init__(
        self,
        client: McpClient,
        tool_info: McpToolInfo,
        timeout: float = 60.0,
        needs_approval: bool = False,  # 由 Provider 根据 approval_commands 设置
    ):
        self._client = client
        self._tool_info = tool_info
        self._timeout = timeout
        self._needs_approval = needs_approval
    
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self._tool_info.name,
            description=self._tool_info.description,
            parameters=self._tool_info.input_schema,
            source=ToolSource.MCP,
            needs_approval=self._needs_approval,
            timeout=self._timeout,
            metadata={
                "server": self._client.server_name,
                "mcp_type": "tool",
            },
        )
    
    async def execute(
        self,
        arguments: dict,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        # 超时设计说明：ToolExecutor 在 Handler 外部用 asyncio.wait_for 控制超时。
        # Handler 内部通过 client.call_tool(timeout=...) 设置客户端超时作为兜底保护，
        # 确保 ToolExecutor 超时因极端边缘情况未生效时仍有二次保护。
        # context 参数暂未在 MCP Handler 内使用——超时来源为 definition.timeout
        # （由 Provider 在注册时根据 server config 设置）。
        try:
            result = await self._client.call_tool(
                self._tool_info.name,
                arguments,
                timeout=self._timeout,
            )
            return ToolResult(
                output=result.content,
                is_error=result.is_error,
                error_code="EXECUTION_ERROR" if result.is_error else None,
            )
        except asyncio.TimeoutError:
            return ToolResult(
                output=f"MCP 工具执行超时（{self._timeout}s）",
                is_error=True,
                error_code="TIMEOUT",
                error_type="timeout",
            )
        except McpUnavailableError as e:
            return ToolResult(
                output=f"MCP server 不可用: {e}",
                is_error=True,
                error_code="MCP_UNAVAILABLE",
                error_type="mcp",
            )
        except Exception as e:
            return ToolResult(
                output=f"MCP 工具执行错误: {e}",
                is_error=True,
                error_code="EXECUTION_ERROR",
                error_type="execution",
            )


class McpResourceHandler(ToolHandler):
    """MCP resource 读取执行器（resources/read）"""
    
    def __init__(
        self,
        client: McpClient,
        resource_info: McpResourceInfo,
        timeout: float = 60.0,
        needs_approval: bool = False,  # 由 Provider 根据 approval_commands 设置
    ):
        self._client = client
        self._resource_info = resource_info
        self._timeout = timeout
        self._needs_approval = needs_approval
    
    def definition(self) -> ToolDefinition:
        # 命名：read_{server}_{name}，避免跨 server 冲突
        server = self._client.server_name
        tool_name = f"read_{server}_{self._resource_info.name}"
        return ToolDefinition(
            name=tool_name,
            description=f"读取资源: {self._resource_info.description or self._resource_info.name}",
            parameters={
                "type": "object",
                "properties": {},
            },
            source=ToolSource.MCP,
            needs_approval=self._needs_approval,
            timeout=self._timeout,
            metadata={
                "server": server,
                "mcp_type": "resource",
                "uri": self._resource_info.uri,
            },
        )
    
    async def execute(
        self,
        arguments: dict,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        try:
            uri = self._resource_info.uri
            result = await self._client.read_resource(uri)
            return ToolResult(output=result.content)
        except asyncio.TimeoutError:
            return ToolResult(
                output=f"MCP 资源读取超时（{self._timeout}s）",
                is_error=True,
                error_code="TIMEOUT",
                error_type="timeout",
            )
        except McpUnavailableError as e:
            return ToolResult(
                output=f"MCP server 不可用: {e}",
                is_error=True,
                error_code="MCP_UNAVAILABLE",
                error_type="mcp",
            )
        except Exception as e:
            return ToolResult(
                output=f"MCP 资源读取错误: {e}",
                is_error=True,
                error_code="EXECUTION_ERROR",
                error_type="execution",
            )


class McpPromptHandler(ToolHandler):
    """MCP prompt 获取执行器（prompts/get）"""
    
    def __init__(
        self,
        client: McpClient,
        prompt_info: McpPromptInfo,
        timeout: float = 60.0,
        needs_approval: bool = False,  # 由 Provider 根据 approval_commands 设置
    ):
        self._client = client
        self._prompt_info = prompt_info
        self._timeout = timeout
        self._needs_approval = needs_approval
    
    def definition(self) -> ToolDefinition:
        # 命名：prompt_{server}_{name}，避免跨 server 冲突
        server = self._client.server_name
        tool_name = f"prompt_{server}_{self._prompt_info.name}"
        
        # 从 McpPromptInfo.arguments 构建参数 schema
        # 保留原始类型（string/number/boolean 等）和 required 属性
        properties = {}
        required_fields = []
        for arg in self._prompt_info.arguments:
            properties[arg["name"]] = {
                "type": arg.get("type", "string"),
                "description": arg.get("description", ""),
            }
            if arg.get("required", False):
                required_fields.append(arg["name"])
        
        parameters = {"type": "object", "properties": properties}
        if required_fields:
            parameters["required"] = required_fields
        
        return ToolDefinition(
            name=tool_name,
            description=f"获取提示词模板: {self._prompt_info.description or self._prompt_info.name}",
            parameters=parameters,
            source=ToolSource.MCP,
            needs_approval=self._needs_approval,
            timeout=self._timeout,
            metadata={
                "server": server,
                "mcp_type": "prompt",
                "prompt_name": self._prompt_info.name,
            },
        )
    
    async def execute(
        self,
        arguments: dict,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        try:
            result = await self._client.get_prompt(self._prompt_info.name, arguments)
            return ToolResult(output=result.content)
        except asyncio.TimeoutError:
            return ToolResult(
                output=f"MCP 提示词获取超时（{self._timeout}s）",
                is_error=True,
                error_code="TIMEOUT",
                error_type="timeout",
            )
        except McpUnavailableError as e:
            return ToolResult(
                output=f"MCP server 不可用: {e}",
                is_error=True,
                error_code="MCP_UNAVAILABLE",
                error_type="mcp",
            )
        except Exception as e:
            return ToolResult(
                output=f"MCP 提示词获取错误: {e}",
                is_error=True,
                error_code="EXECUTION_ERROR",
                error_type="execution",
            )
```

**命名策略**：

| MCP 类型 | 工具命名 | 说明 |
|---------|---------|------|
| Tool | 原名 | 如 `read_file`，可能覆盖内置同名工具（设计如此：同名后注册覆盖） |
| Resource | `read_{server}_{name}` | 如 `read_filesystem_config`，避免跨 server 冲突 |
| Prompt | `prompt_{server}_{name}` | 如 `prompt_remote_code_review`，避免跨 server 冲突 |

---

### 3.4 Provider 层 `mcp/provider.py`

**职责**：编排 MCP servers 的连接、工具注册、生命周期管理。

**核心类**：

```python
class MCPToolProvider(ToolProvider):
    """
    MCP 工具提供者（ToolProvider ABC 实现）。
    
    职责：
    - 编排：遍历 mcp_servers 配置 → 创建 clients → 注册 tools
    - 生命周期：start / shutdown / health_check
    - 状态管理：clients + failed_servers + pending_servers
    - 后台加载：asyncio.create_task
    """
    
    def __init__(
        self,
        global_config: McpGlobalConfig,
        server_configs: list[McpServerConfig],
        registry: ToolRegistry,
        approval_commands: list[str] | None = None,  # 需审批的工具名列表
    ):
        self._global = global_config
        self._server_configs = server_configs
        self._registry = registry
        self._approval_commands = set(approval_commands) if approval_commands else set()
        
        # 运行时状态
        self._clients: dict[str, McpClient] = {}       # server_name -> client
        self._failed_servers: dict[str, str] = {}      # server_name -> 失败原因
        self._pending_servers: dict[str, str] = {}     # server_name -> "connecting" 等状态
        self._started = False
    
    @property
    def clients(self) -> dict[str, McpClient]:
        """所有已连接的 clients"""
        return self._clients
    
    @property
    def failed_servers(self) -> dict[str, str]:
        """启动失败的 servers"""
        return self._failed_servers
    
    async def discover_and_register(self, registry: ToolRegistry) -> list[str]:
        """
        ToolProvider ABC 接口实现。
        
        连接所有 MCP servers 并注册工具。
        返回本次注册的工具名称列表。
        """
        return await self.start()
    
    async def start(self) -> list[str]:
        """
        启动所有 MCP servers（后台并行）。
        
        流程：
        1. 遍历 server_configs
        2. 为每个 server记录 pending 状态
        3. 创建 McpClient 并行连接
        4. 成功的注册工具，失败的记录原因
        5. 返回所有注册的工具名
        """
        if self._started:
            logger.warning("MCPToolProvider 已启动，忽略重复调用")
            return []
        
        self._started = True
        registered_tools: list[str] = []
        
        # 记录所有 server 为 pending
        for cfg in self._server_configs:
            self._pending_servers[cfg.name] = "connecting"
        
        # 并行连接所有 servers
        tasks = []
        for cfg in self._server_configs:
            client = McpClient(cfg, self._global)
            tasks.append(self._connect_and_register(client))
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for cfg, result in zip(self._server_configs, results):
            if isinstance(result, Exception):
                self._failed_servers[cfg.name] = str(result)
                self._pending_servers.pop(cfg.name, None)
                logger.error(f"MCP server {cfg.name} 启动失败: {result}")
            else:
                tool_names = result  # list[str]
                registered_tools.extend(tool_names)
        
        logger.info(f"MCP 已加载 {len(registered_tools)} 个工具")
        return registered_tools
    
    async def _connect_and_register(self, client: McpClient) -> list[str]:
        """
        连接单个 MCP server 并注册工具。
        
        返回注册的工具名列表。
        """
        success = await client.connect()
        if not success:
            raise McpClientError(f"连接失败: {client.server_name}")
        
        # 连接成功：从 pending 移到 clients
        self._pending_servers.pop(client.server_name, None)
        self._clients[client.server_name] = client
        registered: list[str] = []
        
        # 注册 tools（使用 McpToolCallHandler）
        for tool_info in client.tools:
            needs_approval = tool_info.name in self._approval_commands
            handler = McpToolCallHandler(
                client=client,
                tool_info=tool_info,
                timeout=client.get_tool_timeout(),
                needs_approval=needs_approval,
            )
            self._registry.register(handler)
            registered.append(tool_info.name)
        
        # 注册 resources（使用 McpResourceHandler）
        for resource_info in client.resources:
            resource_tool_name = f"read_{client.server_name}_{resource_info.name}"
            needs_approval = resource_tool_name in self._approval_commands
            handler = McpResourceHandler(
                client=client,
                resource_info=resource_info,
                timeout=client.get_tool_timeout(),
                needs_approval=needs_approval,
            )
            self._registry.register(handler)
            registered.append(handler.definition().name)
        
        # 注册 prompts（使用 McpPromptHandler）
        for prompt_info in client.prompts:
            prompt_tool_name = f"prompt_{client.server_name}_{prompt_info.name}"
            needs_approval = prompt_tool_name in self._approval_commands
            handler = McpPromptHandler(
                client=client,
                prompt_info=prompt_info,
                timeout=client.get_tool_timeout(),
                needs_approval=needs_approval,
            )
            self._registry.register(handler)
            registered.append(handler.definition().name)
        
        return registered
    
    async def shutdown(self):
        """关闭所有 MCP servers"""
        for client in self._clients.values():
            await client.shutdown()
        self._clients.clear()
        self._pending_servers.clear()
        self._started = False
    
    def get_server_states(self) -> dict[str, tuple[McpClientState, str]]:
        """
        获取所有 servers 的状态（含 pending）。
        
        返回：{server_name: (state, message)}
        """
        result = {}
        
        # 正在连接的 servers
        for name, status in self._pending_servers.items():
            result[name] = (McpClientState.STARTING, status)
        
        # 已连接的 clients
        for name, client in self._clients.items():
            result[name] = (client.state, "")
        
        # 启动失败的 servers
        for name, reason in self._failed_servers.items():
            result[name] = (McpClientState.FAILED, reason)
        
        return result
```

---

### 3.5 包入口 `mcp/__init__.py`

```python
"""MCP 协议集成模块"""

from .client import McpClient, McpClientState
from .tool_adapter import (
    McpToolCallHandler,
    McpResourceHandler,
    McpPromptHandler,
    McpToolInfo,
    McpResourceInfo,
    McpPromptInfo,
    McpToolResult,
)
from .provider import MCPToolProvider

__all__ = [
    "McpClient",
    "McpClientState",
    "McpToolCallHandler",
    "McpResourceHandler",
    "McpPromptHandler",
    "McpToolInfo",
    "McpResourceInfo",
    "McpPromptInfo",
    "McpToolResult",
    "MCPToolProvider",
]
```

---

### 3.6 集成层 `main.py` 修改

**修改点**：

```python
# main.py

async def _run_cli():
    # ... 现有初始化代码 ...
    
    # ---- Phase 6 新增：MCP 初始化 ----
    mcp_provider = None
    if config.tools.mcp_enabled and config.tools.mcp_servers:
        from dotclaw.mcp import MCPToolProvider
        from dotclaw.tools.base import ToolSource
        
        mcp_provider = MCPToolProvider(
            global_config=config.tools.mcp_global,
            server_configs=config.tools.mcp_servers,
            registry=tool_registry,
            approval_commands=config.tools.approval_commands,  # 传递审批列表
        )
        
        channel.print_info("MCP 工具加载中...")
        
        # 后台加载
        async def _load_mcp():
            try:
                tool_names = await mcp_provider.start()
                channel.print_info(f"已加载 {len(tool_names)} 个 MCP 工具")
            except Exception as e:
                channel.print_error(f"MCP 加载失败: {e}")
        
        asyncio.create_task(_load_mcp())
    # ---- Phase 6 MCP 初始化结束 ----
    
    # ... 创建 AgentLoop ...
    
    # 新增 /mcp 命令
    while True:
        # ... 现有命令处理 ...
        
        elif cmd == "/mcp":
            _cmd_mcp(channel, mcp_provider)
        
        # ... 其他命令 ...

def _cmd_tools(channel, tool_executor):
    """列出所有可用工具（Phase 6 增强 — 展示来源）"""
    definitions = tool_executor.get_definitions()
    if not definitions:
        channel.print_info("(没有注册任何工具)")
        return
    
    # 按来源分组
    builtin = [d for d in definitions if d.source == ToolSource.BUILTIN]
    mcp_tools = [d for d in definitions if d.source == ToolSource.MCP]
    
    if builtin:
        channel.print_info(f"内置工具 ({len(builtin)} 个):")
        for d in builtin:
            mark = " [需审批]" if d.needs_approval else ""
            channel.print_info(f"  {d.name}{mark}: {d.description}")
    
    if mcp_tools:
        # 按 server 分组
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

def _cmd_mcp(channel, mcp_provider):
    """查看 MCP servers 状态"""
    if not mcp_provider:
        channel.print_info("MCP 未启用")
        return
    
    states = mcp_provider.get_server_states()
    if not states:
        channel.print_info("(未配置 MCP server)")
        return
    
    channel.print_info("MCP servers:")
    for name, (state, message) in states.items():
        msg = f" — {message}" if message else ""
        channel.print_info(f"  [{name}] {state.value}{msg}")
```

---

## 四、文件清单

### 4.1 新增文件

| 文件 | 说明 |
|------|------|
| `mcp/__init__.py` | 包入口，导出公共 API |
| `mcp/client.py` | McpClient + McpClientState |
| `mcp/tool_adapter.py` | 三个 Handler + Info/Result 数据类 |
| `mcp/provider.py` | MCPToolProvider |

### 4.2 修改文件

| 文件 | 修改内容 |
|------|---------|
| `config/settings.py` | 新增 McpGlobalConfig / McpServerConfig dataclass；ToolsConfig 新增 mcp_global / mcp_servers 字段；_raw_to_config() 新增 MCP 配置解析 |
| `config.yaml` | 新增 mcp_global + mcp_servers 配置段 |
| `main.py` | MCP 初始化链（后台加载）；/mcp 命令；/tools 增强 |
| `pyproject.toml` | 新增 `mcp>=1.0.0` 依赖 |

### 4.3 测试文件

| 文件 | 说明 |
|------|------|
| `tests/test_phase6_acceptance.py` | Phase 6 验收测试 |

---

## 五、依赖管理

### 5.1 `pyproject.toml` 新增

```toml
dependencies = [
    # ... 现有依赖 ...
    "mcp>=1.0.0",  # MCP 官方 Python SDK
]
```

**注意**：`mcp` SDK 会带入 `pydantic`、`httpx-sse`、`anyio`、`starlette`、`uvicorn` 等依赖。

---

## 六、错误处理设计

### 6.1 错误类型

```python
class McpError(Exception):
    """MCP 基础异常"""
    pass

class McpClientError(McpError):
    """MCP client 异常（连接失败等）"""
    pass

class McpUnavailableError(McpError):
    """MCP server 不可用（crashed/failed/shutdown）"""
    pass
```

### 6.2 错误传播

```
McpClient 方法异常
    |
    +-- TimeoutError → Handler.execute() → ToolResult(error_code="TIMEOUT")
    |
    +-- McpUnavailableError → Handler.execute() → ToolResult(error_code="MCP_UNAVAILABLE")
    |
    +-- Exception → Handler.execute() → ToolResult(error_code="EXECUTION_ERROR")
```

---

## 七、测试设计

### 7.1 单元测试

| 测试项 | 说明 |
|-------|------|
| 配置解析 | 全局默认 + 单 server 覆盖 + 校验（transport/name/command/url） |
| McpClient 状态机 | STARTING → CONNECTED / FAILED / CRASHED / SHUTDOWN 状态转移 |
| McpToolCallHandler | tool 类型工具的定义生成 + execute 调用 call_tool |
| McpResourceHandler | resource 类型工具的定义生成（含 server 前缀命名）+ execute 调用 read_resource |
| McpPromptHandler | prompt 类型工具的定义生成（含 server 前缀命名）+ execute 调用 get_prompt |
| McpToolResult.from_mcp | 文本 content + 非文本 content（image/resource）降级处理 |
| 重连逻辑 | 失败计数器递增 + 达到上限标记 CRASHED + 重连成功清零 |
| Provider 状态管理 | pending_servers + clients + failed_servers 三层状态 |

### 7.2 集成测试

| 测试项 | 说明 |
|-------|------|
| stdio 连接 | 启动真实 MCP server（如 `@anthropic/mcp-server-filesystem`），验证工具注册 |
| Streamable HTTP 连接 | 连接远程 MCP server，验证工具注册 |
| 工具调用 | Agent 对话中触发 MCP 工具，验证 tools/call 调用链路 |
| Resource 调用 | Agent 对话中触发 MCP resource 工具，验证 resources/read 链路 |
| Prompt 调用 | Agent 对话中触发 MCP prompt 工具，验证 prompts/get 链路 |
| 启动失败 | 配置不存在的命令，验证跳过 + 状态标记 failed |
| 运行时崩溃 | 模拟 server 崩溃，验证重连逻辑 + 状态标记 crashed |
| 后台加载 | 验证 MCP 加载不阻塞 Agent 首次消息响应 |
| /mcp 命令 | 验证加载中 / 已连接 / 失败三种状态显示 |
| /tools 增强 | 验证按来源分组显示（builtin / mcp:server_name） |

---

## 八、验收标准

1. **配置加载**：`config.yaml` 中 `mcp_servers` 列表正确解析，全局默认 + 单 server 覆盖生效
2. **stdio 连接**：配置 stdio 传输的 MCP server 能正常启动子进程、握手、发现工具并注册
3. **Streamable HTTP 连接**：配置 streamable_http 传输的 MCP server 能正常连接、握手、发现工具并注册
4. **工具调用**：Agent 对话中触发 MCP 工具 → `tools/call` → 返回结果，结果正确回传 LLM
5. **Resources 工具**：MCP server 暴露的 resources 包装为 `McpResourceHandler`，命名 `read_{server}_{name}`，可被 LLM 调用
6. **Prompts 工具**：MCP server 暴露的 prompts 包装为 `McpPromptHandler`，命名 `prompt_{server}_{name}`，可被 LLM 调用
7. **后台加载**：MCP 连接在后台 asyncio task 中进行，Agent 立即可用，MCP 加载完后提示已加载工具数
8. **启动失败**：单个 MCP server 启动失败（命令不存在/握手超时）不影响其他 server 和 Agent 主流程，状态标记为 failed
9. **运行时崩溃**：MCP server 崩溃后自动重连，失败计数器递增，达到上限后工具标记为不可用，状态标记为 crashed
10. **超时 + cancel**：MCP 工具超时后向 server 发 cancel 通知，ToolExecutor 返回 TIMEOUT
11. **优雅关闭**：Agent 退出时向所有 MCP server 发送 shutdown 通知
12. **`/tools` 增强**：展示工具来源标记（`[builtin]` / `[mcp:server_name]`）
13. **`/mcp` 命令**：展示各 MCP server 连接状态（starting / connected / crashed / failed），加载中状态可见
14. **审批可控**：MCP 工具默认不审批，用户可通过 `approval_commands` 配置按需开启，审批流程与内置工具一致（needs_approval + approval_commands 双重 AND 门）
15. **非文本 content**：MCP 返回的 image/resource 类型 content 降级为描述文本，不返回空字符串
16. **回归**：Phase 1-5 全部测试通过

---

## 九、开发注意事项

1. **mcp SDK 版本**：使用最新稳定版（>=1.0.0），API 可能随版本变化，需查阅官方文档
2. **Streamable HTTP 传输**：MCP 2025-03 规范用 Streamable HTTP 替代 SSE，SDK 需支持
3. **cancel 通知**：MCP SDK 的 `send_cancel()` 方法名可能不同，需确认
4. **resources/read 接口**：MCP SDK 读取资源的方法名需确认（可能是 `read_resource` 或 `get_resource`）
5. **prompts/get 接口**：MCP SDK 获取提示词的方法名需确认
6. **子进程清理**：stdio 传输的 server 子进程在 shutdown 时需确保退出，避免孤儿进程
7. **后台 task 异常**：MCP 后台加载 task 的异常需记录日志，不影响主流程
8. **同名工具覆盖**：MCP tool 与内置工具同名时，后注册覆盖。注册时输出 info 日志，帮助排查跨 server 工具名冲突
9. **环境变量展开**：`${VAR}` 在 url / headers 中展开，由 `config/settings.py` 的 `_expand_env()` 统一处理
10. **headers 字段**：Streamable HTTP 传输的认证 headers 需在 SDK 创建 transport 时传入
11. **配置集中**：MCP 配置 dataclass 定义在 `config/settings.py`，`mcp/` 包只 import 不定义
12. **封装原则**：`MCPToolProvider` 不访问 `McpClient._config`，通过 `client.get_tool_timeout()` 公开方法访问
13. **Handler 拆分**：三种 MCP 能力分别对应三个独立 Handler 类，不使用 cast 模式
14. **审批可控**：三个 Handler 的 `needs_approval` 由 Provider 根据 `approval_commands` 计算，不硬编码 False
15. **SDK 属性确认**：`mcp_result.isError` 属性名需根据实际 MCP SDK 验证；`McpResourceInfo.from_mcp()` / `McpPromptInfo.from_mcp()` 中的属性名（如 `mimeType` vs `mime_type`、`inputSchema` vs `input_schema`）需根据 SDK 版本确认
16. **ToolSource 导入**：main.py 中 `_cmd_tools()` 使用 `ToolSource`，需新增 `from dotclaw.tools.base import ToolSource`

---

## 十、后续扩展建议

以下内容不在 Phase 6 范围，但可作为后续优化方向：

| 优先级 | 模块 | 说明 |
|--------|------|------|
| 🟡 中 | 条件性工具加载 | ToolDefinition 新增 requires 字段，MCP server 可声明依赖 |
| 🟡 中 | 热重载 | 监测 config.yaml 变化，增量重启变化的 MCP server |
| 🟢 低 | MCP logging | 接收 MCP server 的日志推送，记录到 dotClaw 日志系统 |
| 🟢 低 | MCP sampling | 支持 MCP server 反向请求 LLM 推理（极少用） |

---

## 十一、参考资源

- MCP 官方规范：https://modelcontextprotocol.io/specification
- MCP Python SDK：https://github.com/modelcontextprotocol/python-sdk
- Anthropic MCP Server 示例：https://github.com/modelcontextprotocol/servers

---

## 审计修正记录

> 基于 `phase6-roadmap-review.md` 审计报告，以下修正已融入本文档：

| 审计编号 | 审计要点 | 处置 | 修正位置 |
|---------|---------|------|---------|
| 缺陷 1 | MCP 配置 dataclass 双位置定义 | ✅ 采纳 | §3.1 配置 dataclass 统一在 `config/settings.py`，删除 `mcp/config.py` 中的重复定义 |
| 缺陷 2 | McpClient 缺少 read_resource / get_prompt 方法 | ✅ 采纳 | §3.2 McpClient 新增 `read_resource()` 和 `get_prompt()` 方法 |
| 缺陷 3 | MCPToolProvider 访问 client._config 私有属性 | ✅ 采纳 | §3.2 McpClient 新增 `get_tool_timeout()` 公开方法；§3.4 Provider 使用 `client.get_tool_timeout()` |
| 缝隙 4 | Resource/Prompt 命名跨 server 冲突 | ✅ 采纳 | §3.3 Resource 命名改为 `read_{server}_{name}`，Prompt 命名改为 `prompt_{server}_{name}` |
| 缝隙 5 | cast 模式类型不安全 | ✅ 采纳 | §3.3 拆分为三个独立 Handler 类：McpToolCallHandler / McpResourceHandler / McpPromptHandler |
| 缝隙 6 | 非文本 content 类型未处理 | ✅ 采纳 | §3.3 `McpToolResult.from_mcp()` 增加非文本 content 降级处理；新增 `from_resource_result()` 和 `from_prompt_result()` |
| 缝隙 7 | /mcp 在加载完成前信息不完整 | ✅ 采纳 | §3.4 Provider 新增 `_pending_servers` dict；`get_server_states()` 返回 pending 状态 |
| 缺失项 3 | RESOURCE/PROMPT 测试覆盖 | ✅ 采纳 | §7.1 补充 McpResourceHandler / McpPromptHandler / Resource 调用 / Prompt 调用测试项 |
| 缺失项 5 | _cmd_tools 使用 tool_executor.get_handler() | ❌ 不适用 | Phase 5 的 ToolExecutor 已有 `get_handler()` 方法，不存在问题 |
| 缺失项 6 | __init__.py 导出列表 | ✅ 采纳 | §3.5 新增 `mcp/__init__.py` 导出定义 |

---

## 审计修正记录 v2（审计员2号）

> 基于 `phase6-roadmap-review2.md` 审计报告，以下修正已融入本文档：

| 审计编号 | 审计要点 | 处置 | 修正位置 |
|---------|---------|------|---------|
| 缺陷 1 | McpResourceInfo / McpPromptInfo 缺少 `from_mcp()` 类方法 | ✅ 采纳 | §3.3 补充两个 `from_mcp()` 类方法，保留原始类型和 required 属性 |
| 缝隙 2 | MCP 工具审批不可控——`needs_approval=False` 硬编码 | ✅ 采纳（方案 A） | §3.3 三个 Handler 构造函数新增 `needs_approval` 参数；§3.4 Provider 从 `approval_commands` 计算并传递；§3.6 main.py 传入 `approval_commands` |
| 缝隙 3 | 三重超时嵌套冗余 | ✅ 加注释 | §3.3 McpToolCallHandler.execute() 增加超时设计意图注释，保留 belt-and-suspenders 设计 |
| 缝隙 4 | McpPromptHandler 参数类型硬编码为 string | ✅ 采纳 | §3.3 `McpPromptInfo.from_mcp()` 保留 `type` 字段；`McpPromptHandler.definition()` 正确映射 type/required |
| 缝隙 5 | `_cmd_tools()` 丢失 P5 的 `[需审批]` 标记 | ✅ 采纳 | §3.6 MCP 工具列表也显示 `[需审批]` 标记 |
| 缝隙 6 | `ToolSource` 导入缺失 | ✅ 采纳 | §3.6 main.py 补充 `from dotclaw.tools.base import ToolSource` |

### 额外修正（审计报告未明确要求但顺带修正）

| 修正项 | 说明 |
|-------|------|
| 设计原则 | 审批描述从“默认不审批”改为“默认不审批，用户可通过 approval_commands 按需配置” |
| 验收第 14 条 | 从“审批一致”改为“审批可控”，明确说明 needs_approval + approval_commands 双重 AND 门 |
| 注意事项第 8 条 | 同名工具覆盖时输出 info 日志（审计前瞻性建议） |
| 注意事项新增 14-16 | 审批可控原则、SDK 属性确认、ToolSource 导入声明 |
