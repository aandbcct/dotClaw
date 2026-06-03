"""Phase 6 验收测试 — MCP 协议集成

覆盖场景：配置解析 / McpClient 状态机 / Handler 定义 / Handler 执行 / Provider 初始化 / main.py 命令
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ============================================================
# 场景 1: 配置解析
# ============================================================

class TestMcpConfig:
    """MCP 配置 dataclass 测试"""

    def test_mcp_global_config_defaults(self):
        from dotclaw.config.settings import McpGlobalConfig
        cfg = McpGlobalConfig()
        assert cfg.startup_timeout == 4.0
        assert cfg.tool_timeout == 60.0
        assert cfg.restart_on_crash is True
        assert cfg.max_restart_attempts == 3

    def test_mcp_server_config_getters(self):
        from dotclaw.config.settings import McpServerConfig, McpGlobalConfig

        global_cfg = McpGlobalConfig(startup_timeout=4.0, tool_timeout=60.0)
        server = McpServerConfig(
            name="test", transport="stdio", command="echo", args=["hello"],
        )
        assert server.get_startup_timeout(global_cfg) == 4.0
        assert server.get_tool_timeout(global_cfg) == 60.0
        assert server.get_restart_on_crash(global_cfg) is True

    def test_mcp_server_config_override(self):
        from dotclaw.config.settings import McpServerConfig, McpGlobalConfig

        global_cfg = McpGlobalConfig(tool_timeout=60.0)
        server = McpServerConfig(
            name="test", transport="stdio", command="echo",
            tool_timeout=120.0,
        )
        assert server.get_tool_timeout(global_cfg) == 120.0

    def test_config_yaml_mcp_parsed(self):
        from dotclaw.config.settings import load_config, _find_project_root

        config_path = _find_project_root() / "config.yaml"
        if not config_path.exists():
            pytest.skip("config.yaml not found")

        config = load_config(str(config_path))
        assert hasattr(config.tools, "mcp_global")
        assert hasattr(config.tools, "mcp_servers")
        assert isinstance(config.tools.mcp_servers, list)
        assert config.tools.mcp_enabled is True

    def test_config_validation_duplicate_name(self):
        from dotclaw.config.settings import _parse_mcp_servers

        with pytest.raises(ValueError, match="重复"):
            _parse_mcp_servers([
                {"name": "dup", "transport": "stdio", "command": "echo"},
                {"name": "dup", "transport": "stdio", "command": "ls"},
            ])

    def test_config_validation_missing_command(self):
        from dotclaw.config.settings import _parse_mcp_servers

        with pytest.raises(ValueError, match="command"):
            _parse_mcp_servers([
                {"name": "no_cmd", "transport": "stdio"},
            ])

    def test_config_validation_missing_url(self):
        from dotclaw.config.settings import _parse_mcp_servers

        with pytest.raises(ValueError, match="url"):
            _parse_mcp_servers([
                {"name": "no_url", "transport": "streamable_http"},
            ])

    def test_config_validation_invalid_transport(self):
        from dotclaw.config.settings import _parse_mcp_servers

        with pytest.raises(ValueError, match="transport"):
            _parse_mcp_servers([
                {"name": "bad", "transport": "websocket"},
            ])


# ============================================================
# 场景 2: McpClient 状态机
# ============================================================

class TestMcpClientState:
    """McpClientState 枚举测试"""

    def test_state_values(self):
        from dotclaw.mcp import McpClientState
        assert McpClientState.STARTING.value == "starting"
        assert McpClientState.CONNECTED.value == "connected"
        assert McpClientState.CRASHED.value == "crashed"
        assert McpClientState.FAILED.value == "failed"
        assert McpClientState.SHUTDOWN.value == "shutdown"

    def test_mcp_client_initial_state(self):
        from dotclaw.mcp import McpClient
        from dotclaw.config.settings import McpServerConfig, McpGlobalConfig

        client = McpClient(
            McpServerConfig(name="test", transport="stdio", command="echo"),
            McpGlobalConfig(),
        )
        assert client.state.value == "starting"
        assert client.server_name == "test"
        assert client.tools == []
        assert client.resources == []
        assert client.prompts == []

    def test_mcp_client_timeout_getters(self):
        from dotclaw.mcp import McpClient
        from dotclaw.config.settings import McpServerConfig, McpGlobalConfig

        client = McpClient(
            McpServerConfig(name="test", transport="stdio", command="echo", tool_timeout=90.0),
            McpGlobalConfig(tool_timeout=60.0, startup_timeout=4.0),
        )
        assert client.get_tool_timeout() == 90.0
        assert client.get_startup_timeout() == 4.0


# ============================================================
# 场景 3: Info/Result 数据类
# ============================================================

class TestMcpInfoDataclasses:
    """McpToolInfo / McpResourceInfo / McpPromptInfo / McpToolResult 测试"""

    def test_tool_info_from_mcp(self):
        from dotclaw.mcp import McpToolInfo

        class MockTool:
            name = "test_tool"
            description = "A test tool"
            inputSchema = {"type": "object", "properties": {}}

        info = McpToolInfo.from_mcp(MockTool())
        assert info.name == "test_tool"
        assert info.description == "A test tool"
        assert info.input_schema == {"type": "object", "properties": {}}

    def test_resource_info_from_mcp(self):
        from dotclaw.mcp import McpResourceInfo

        class MockResource:
            uri = "file:///test.txt"
            name = "test_resource"
            description = "Test resource"
            mimeType = "text/plain"

        info = McpResourceInfo.from_mcp(MockResource())
        assert info.uri == "file:///test.txt"
        assert info.name == "test_resource"
        assert info.mime_type == "text/plain"

    def test_prompt_info_from_mcp(self):
        from dotclaw.mcp import McpPromptInfo

        class MockArg:
            name = "topic"
            description = "Topic name"
            required = True
            type = "string"

        class MockPrompt:
            name = "review_code"
            description = "Review code prompt"
            arguments = [MockArg()]

        info = McpPromptInfo.from_mcp(MockPrompt())
        assert info.name == "review_code"
        assert len(info.arguments) == 1
        assert info.arguments[0]["name"] == "topic"
        assert info.arguments[0]["required"] is True
        assert info.arguments[0]["type"] == "string"

    def test_tool_result_from_mcp_text(self):
        from dotclaw.mcp import McpToolResult

        class MockText:
            text = "result content"

        class MockResult:
            content = [MockText()]
            isError = False

        result = McpToolResult.from_mcp(MockResult())
        assert result.content == "result content"
        assert result.is_error is False

    def test_tool_result_from_resource(self):
        from dotclaw.mcp import McpToolResult

        class MockText:
            text = "resource content"

        class MockResult:
            contents = [MockText()]

        result = McpToolResult.from_resource_result(MockResult())
        assert "resource content" in result.content


# ============================================================
# 场景 4: Handler 定义（无真实 MCP 连接）
# ============================================================

class TestMcpHandlerDefinition:
    """McpToolCallHandler / McpResourceHandler / McpPromptHandler 定义测试"""

    def test_tool_handler_definition(self):
        from dotclaw.mcp import McpToolCallHandler
        from dotclaw.mcp.client import McpClient
        from dotclaw.config.settings import McpServerConfig, McpGlobalConfig

        client = McpClient(
            McpServerConfig(name="test-srv", transport="stdio", command="echo"),
            McpGlobalConfig(tool_timeout=30.0),
        )

        handler = McpToolCallHandler(
            client=client,
            name="my_tool",
            description="A tool",
            input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
            timeout=30.0,
            needs_approval=True,
        )

        definition = handler.definition()
        assert definition.name == "my_tool"
        assert definition.source.value == "mcp"
        assert definition.needs_approval is True
        assert definition.timeout == 30.0
        assert definition.metadata["server"] == "test-srv"
        assert definition.metadata["mcp_type"] == "tool"

    def test_resource_handler_naming(self):
        from dotclaw.mcp import McpResourceHandler
        from dotclaw.mcp.client import McpClient
        from dotclaw.config.settings import McpServerConfig, McpGlobalConfig

        client = McpClient(
            McpServerConfig(name="fs", transport="stdio", command="echo"),
            McpGlobalConfig(),
        )

        handler = McpResourceHandler(
            client=client,
            resource_name="config",
            uri="file:///config.json",
            description="Server config",
        )

        definition = handler.definition()
        assert definition.name == "read_fs_config"
        assert definition.source.value == "mcp"
        assert definition.metadata["mcp_type"] == "resource"

    def test_prompt_handler_parameters(self):
        from dotclaw.mcp import McpPromptHandler
        from dotclaw.mcp.client import McpClient
        from dotclaw.config.settings import McpServerConfig, McpGlobalConfig

        client = McpClient(
            McpServerConfig(name="srv", transport="stdio", command="echo"),
            McpGlobalConfig(),
        )

        handler = McpPromptHandler(
            client=client,
            prompt_name="review",
            description="Code review",
            arguments_info=[
                {"name": "file", "description": "File path", "required": True, "type": "string"},
                {"name": "strictness", "description": "Level", "required": False, "type": "number"},
            ],
        )

        definition = handler.definition()
        assert definition.name == "prompt_srv_review"
        params = definition.parameters
        assert params["required"] == ["file"]
        assert "file" in params["properties"]
        assert "strictness" in params["properties"]
        assert params["properties"]["strictness"]["type"] == "number"


# ============================================================
# 场景 5: Provider 初始化
# ============================================================

class TestProvider:
    """MCPToolProvider 初始化与状态测试"""

    def test_provider_init(self):
        from dotclaw.tools.registry import ToolRegistry
        from dotclaw.mcp import MCPToolProvider
        from dotclaw.config.settings import McpGlobalConfig

        registry = ToolRegistry()
        provider = MCPToolProvider(
            global_config=McpGlobalConfig(),
            server_configs=[],
            registry=registry,
        )

        assert provider.clients == {}
        assert provider.failed_servers == {}
        assert len(provider.get_server_states()) == 0

    def test_provider_implements_tool_provider_abc(self):
        from dotclaw.mcp import MCPToolProvider
        from dotclaw.tools.provider import ToolProvider

        assert issubclass(MCPToolProvider, ToolProvider)

    @pytest.mark.asyncio
    async def test_provider_start_empty(self):
        from dotclaw.tools.registry import ToolRegistry
        from dotclaw.mcp import MCPToolProvider
        from dotclaw.config.settings import McpGlobalConfig

        registry = ToolRegistry()
        provider = MCPToolProvider(
            global_config=McpGlobalConfig(),
            server_configs=[],
            registry=registry,
        )

        result = await provider.start()
        assert result == []
        assert len(provider.get_server_states()) == 0

    @pytest.mark.asyncio
    async def test_provider_repeat_start(self):
        from dotclaw.tools.registry import ToolRegistry
        from dotclaw.mcp import MCPToolProvider
        from dotclaw.config.settings import McpGlobalConfig

        registry = ToolRegistry()
        provider = MCPToolProvider(
            global_config=McpGlobalConfig(),
            server_configs=[],
            registry=registry,
        )

        await provider.start()
        result2 = await provider.start()
        assert result2 == []  # 重复调用返回空列表

    @pytest.mark.asyncio
    async def test_provider_shutdown(self):
        from dotclaw.tools.registry import ToolRegistry
        from dotclaw.mcp import MCPToolProvider
        from dotclaw.config.settings import McpGlobalConfig

        registry = ToolRegistry()
        provider = MCPToolProvider(
            global_config=McpGlobalConfig(),
            server_configs=[],
            registry=registry,
        )

        await provider.start()
        await provider.shutdown()
        assert provider.clients == {}


# ============================================================
# 场景 6: 错误类型
# ============================================================

class TestMcpErrors:
    """MCP 异常类型测试"""

    def test_mcp_error_hierarchy(self):
        from dotclaw.mcp import McpError, McpClientError, McpUnavailableError

        assert issubclass(McpClientError, McpError)
        assert issubclass(McpUnavailableError, McpError)

    def test_mcp_unavailable_error_message(self):
        from dotclaw.mcp import McpUnavailableError

        e = McpUnavailableError("test server crashed")
        assert "test server crashed" in str(e)


# ============================================================
# 场景 7: 回归测试
# ============================================================

class TestRegression:
    """Phase 6 不应影响 Phase 1-5 功能"""

    def test_imports_dont_break(self):
        """所有核心模块应正常导入"""
        from dotclaw.tools.base import ToolDefinition, ToolResult, ToolSource
        from dotclaw.tools.registry import ToolRegistry
        from dotclaw.tools.executor import ToolExecutor
        from dotclaw.tools.handler import BuiltinToolHandler
        from dotclaw.tools.provider import ToolProvider
        from dotclaw.tools.builtin import register_all
        from dotclaw.agent.logger import AgentLogger, TraceRecord

        assert ToolSource.MCP.value == "mcp"
        assert ToolSource.SKILL.value == "skill"

    def test_config_mcp_fields_present(self):
        from dotclaw.config.settings import McpGlobalConfig, McpServerConfig

        global_cfg = McpGlobalConfig()
        server_cfg = McpServerConfig(name="test-srv", transport="stdio", command="echo")

        assert hasattr(global_cfg, "startup_timeout")
        assert hasattr(global_cfg, "tool_timeout")
        assert hasattr(global_cfg, "restart_on_crash")
        assert hasattr(global_cfg, "max_restart_attempts")
        assert hasattr(server_cfg, "name")
        assert hasattr(server_cfg, "transport")
        assert hasattr(server_cfg, "command")
        assert hasattr(server_cfg, "url")
        assert hasattr(server_cfg, "headers")
        assert hasattr(server_cfg, "startup_timeout")
        assert hasattr(server_cfg, "tool_timeout")
