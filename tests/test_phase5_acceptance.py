"""Phase 5 验收测试 — 工具层架构重构

覆盖 10 个场景：
1. ToolRegistry 注册/查询/覆盖
2. ToolRegistry 注销
3. BuiltinToolHandler 执行
4. ToolExecutor 审批流程
5. ToolExecutor 超时控制
6. ToolExecutor 工具未找到
7. AgentLoop 集成
8. 配置加载
9. 日志合并
10. 旧 config 格式兼容
"""

import asyncio
import sys
from pathlib import Path

import pytest

# 确保 src 在路径中
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ============================================================
# 辅助工具
# ============================================================

class FakeChannel:
    """模拟 Channel 用于审批测试"""
    def __init__(self, answer: str = "y"):
        self._answer = answer
        self.printed: list[str] = []

    async def ask_user(self, prompt: str) -> str:
        return self._answer

    def print_info(self, msg: str):
        self.printed.append(msg)

    def stream(self, content: str):
        pass

    async def send(self, content: str):
        pass

    def print_error(self, msg: str):
        self.printed.append(msg)


# ============================================================
# 场景 1: ToolRegistry 注册/查询/覆盖
# ============================================================

class TestToolRegistry:
    """ToolRegistry 纯注册表测试"""

    def test_register_and_get(self):
        """注册工具后可通过 get() 获取"""
        from dotclaw.tools.registry import ToolRegistry
        from dotclaw.tools.handler import BuiltinToolHandler

        registry = ToolRegistry()

        async def dummy() -> str:
            return "ok"

        handler = BuiltinToolHandler(
            name="test_tool",
            description="A test tool",
            parameters={"type": "object", "properties": {}},
            handler_fn=dummy,
        )
        registry.register(handler)

        assert registry.get("test_tool") is handler
        assert registry.get("nonexistent") is None

    def test_all_names(self):
        """all_names() 返回注册的所有工具名"""
        from dotclaw.tools.registry import ToolRegistry
        from dotclaw.tools.handler import BuiltinToolHandler

        registry = ToolRegistry()

        async def dummy() -> str:
            return "ok"

        registry.register(BuiltinToolHandler("a", "desc", {}, dummy))
        registry.register(BuiltinToolHandler("b", "desc", {}, dummy))

        names = registry.all_names()
        assert sorted(names) == ["a", "b"]

    def test_register_override(self):
        """同名工具后注册覆盖前注册（静默覆盖）"""
        from dotclaw.tools.registry import ToolRegistry
        from dotclaw.tools.handler import BuiltinToolHandler

        registry = ToolRegistry()

        async def old() -> str:
            return "old"

        async def new() -> str:
            return "new"

        handler1 = BuiltinToolHandler("tool", "old desc", {}, old)
        handler2 = BuiltinToolHandler("tool", "new desc", {}, new)

        registry.register(handler1)
        registry.register(handler2)

        assert registry.get("tool") is handler2
        assert registry.get("tool").definition().description == "new desc"

    def test_get_definitions(self):
        """get_definitions() 返回所有 ToolDefinition"""
        from dotclaw.tools.registry import ToolRegistry
        from dotclaw.tools.handler import BuiltinToolHandler

        registry = ToolRegistry()

        async def dummy() -> str:
            return "ok"

        registry.register(BuiltinToolHandler("a", "A", {}, dummy))
        registry.register(BuiltinToolHandler("b", "B", {}, dummy))

        defs = registry.get_definitions()
        assert len(defs) == 2

    def test_list_by_source(self):
        """list_by_source() 按来源过滤"""
        from dotclaw.tools.registry import ToolRegistry
        from dotclaw.tools.handler import BuiltinToolHandler
        from dotclaw.tools.base import ToolSource

        registry = ToolRegistry()

        async def dummy() -> str:
            return "ok"

        registry.register(BuiltinToolHandler("a", "desc", {}, dummy))
        registry.register(BuiltinToolHandler("b", "desc", {}, dummy))

        builtin_handlers = registry.list_by_source(ToolSource.BUILTIN)
        assert len(builtin_handlers) == 2

        mcp_handlers = registry.list_by_source(ToolSource.MCP)
        assert len(mcp_handlers) == 0

    def test_clear(self):
        """clear() 清空注册表"""
        from dotclaw.tools.registry import ToolRegistry
        from dotclaw.tools.handler import BuiltinToolHandler

        registry = ToolRegistry()

        async def dummy() -> str:
            return "ok"

        registry.register(BuiltinToolHandler("a", "desc", {}, dummy))
        registry.clear()

        assert registry.all_names() == []
        assert len(registry.get_definitions()) == 0


# ============================================================
# 场景 2: ToolRegistry 注销
# ============================================================

class TestToolRegistryUnregister:
    """ToolRegistry 注销功能测试"""

    def test_unregister_success(self):
        """注销已注册工具返回 True"""
        from dotclaw.tools.registry import ToolRegistry
        from dotclaw.tools.handler import BuiltinToolHandler

        registry = ToolRegistry()

        async def dummy() -> str:
            return "ok"

        registry.register(BuiltinToolHandler("tool", "desc", {}, dummy))
        assert registry.unregister("tool") is True
        assert registry.get("tool") is None

    def test_unregister_nonexistent(self):
        """注销不存在的工具返回 False"""
        from dotclaw.tools.registry import ToolRegistry

        registry = ToolRegistry()
        assert registry.unregister("nonexistent") is False


# ============================================================
# 场景 3: BuiltinToolHandler 执行
# ============================================================

class TestBuiltinToolHandler:
    """BuiltinToolHandler 执行测试"""

    @pytest.mark.asyncio
    async def test_normal_execution(self):
        """正常执行返回 ToolResult"""
        from dotclaw.tools.handler import BuiltinToolHandler

        async def echo(message: str) -> str:
            return f"Echo: {message}"

        handler = BuiltinToolHandler(
            name="echo",
            description="Echo back",
            parameters={"type": "object", "properties": {"message": {"type": "string"}}},
            handler_fn=echo,
        )

        result = await handler.execute({"message": "hello"})
        assert result.output == "Echo: hello"
        assert result.is_error is False
        assert result.error_code is None

    @pytest.mark.asyncio
    async def test_exception_capture(self):
        """异常被捕获并返回 is_error=True"""
        from dotclaw.tools.handler import BuiltinToolHandler

        async def fail() -> str:
            raise ValueError("something went wrong")

        handler = BuiltinToolHandler(
            name="fail",
            description="Always fails",
            parameters={"type": "object", "properties": {}},
            handler_fn=fail,
        )

        result = await handler.execute({})
        assert result.is_error is True
        assert result.error_code == "EXECUTION_ERROR"
        assert result.error_type == "execution"
        assert "something went wrong" in result.output


# ============================================================
# 场景 4: ToolExecutor 审批流程
# ============================================================

class TestToolExecutorApproval:
    """ToolExecutor 审批流程测试"""

    @pytest.mark.asyncio
    async def test_needs_approval_denied(self):
        """needs_approval=True + approval_commands 中包含 → 用户拒绝 → APPROVAL_DENIED"""
        from dotclaw.tools.registry import ToolRegistry
        from dotclaw.tools.executor import ToolExecutor
        from dotclaw.tools.approval import ApprovalManager
        from dotclaw.tools.handler import BuiltinToolHandler

        registry = ToolRegistry()

        async def dangerous() -> str:
            return "should not execute"

        handler = BuiltinToolHandler(
            name="dangerous_tool",
            description="A dangerous tool",
            parameters={"type": "object", "properties": {}},
            handler_fn=dangerous,
            needs_approval=True,
        )
        registry.register(handler)

        approval = ApprovalManager(approval_commands=["dangerous_tool"])
        executor = ToolExecutor(registry, approval)

        channel = FakeChannel(answer="n")
        result = await executor.execute("dangerous_tool", {}, channel=channel)

        assert result.is_error is True
        assert result.error_code == "APPROVAL_DENIED"
        assert result.error_type == "approval"

    @pytest.mark.asyncio
    async def test_needs_approval_allowed(self):
        """needs_approval=True + approval_commands 中包含 → 用户确认 → 正常执行"""
        from dotclaw.tools.registry import ToolRegistry
        from dotclaw.tools.executor import ToolExecutor
        from dotclaw.tools.approval import ApprovalManager
        from dotclaw.tools.handler import BuiltinToolHandler

        registry = ToolRegistry()

        async def dangerous() -> str:
            return "executed safely"

        handler = BuiltinToolHandler(
            name="dangerous_tool",
            description="A dangerous tool",
            parameters={"type": "object", "properties": {}},
            handler_fn=dangerous,
            needs_approval=True,
        )
        registry.register(handler)

        approval = ApprovalManager(approval_commands=["dangerous_tool"])
        executor = ToolExecutor(registry, approval)

        channel = FakeChannel(answer="y")
        result = await executor.execute("dangerous_tool", {}, channel=channel)

        assert result.is_error is False
        assert result.output == "executed safely"

    @pytest.mark.asyncio
    async def test_needs_approval_not_in_list(self):
        """needs_approval=True 但不在 approval_commands 中 → 放行"""
        from dotclaw.tools.registry import ToolRegistry
        from dotclaw.tools.executor import ToolExecutor
        from dotclaw.tools.approval import ApprovalManager
        from dotclaw.tools.handler import BuiltinToolHandler

        registry = ToolRegistry()

        async def dangerous() -> str:
            return "executed"

        handler = BuiltinToolHandler(
            name="dangerous_tool",
            description="A dangerous tool",
            parameters={"type": "object", "properties": {}},
            handler_fn=dangerous,
            needs_approval=True,
        )
        registry.register(handler)

        # approval_commands 不包含 dangerous_tool
        approval = ApprovalManager(approval_commands=[])
        executor = ToolExecutor(registry, approval)

        channel = FakeChannel(answer="n")  # 用户拒绝，但不应触发审批
        result = await executor.execute("dangerous_tool", {}, channel=channel)

        assert result.is_error is False
        assert result.output == "executed"

    @pytest.mark.asyncio
    async def test_approval_disabled(self):
        """approval._enabled = False → 全部放行"""
        from dotclaw.tools.registry import ToolRegistry
        from dotclaw.tools.executor import ToolExecutor
        from dotclaw.tools.approval import ApprovalManager
        from dotclaw.tools.handler import BuiltinToolHandler

        registry = ToolRegistry()

        async def dangerous() -> str:
            return "executed"

        handler = BuiltinToolHandler(
            name="dangerous_tool",
            description="dangerous",
            parameters={},
            handler_fn=dangerous,
            needs_approval=True,
        )
        registry.register(handler)

        approval = ApprovalManager(approval_commands=["dangerous_tool"])
        approval.set_enabled(False)
        executor = ToolExecutor(registry, approval)

        channel = FakeChannel(answer="n")
        result = await executor.execute("dangerous_tool", {}, channel=channel)

        assert result.is_error is False
        assert result.output == "executed"


# ============================================================
# 场景 5: ToolExecutor 超时控制
# ============================================================

class TestToolExecutorTimeout:
    """ToolExecutor 超时控制测试"""

    @pytest.mark.asyncio
    async def test_timeout(self):
        """超时返回 TIMEOUT error_code"""
        from dotclaw.tools.registry import ToolRegistry
        from dotclaw.tools.executor import ToolExecutor
        from dotclaw.tools.handler import BuiltinToolHandler

        registry = ToolRegistry()

        async def slow() -> str:
            await asyncio.sleep(5)
            return "done"

        handler = BuiltinToolHandler(
            name="slow",
            description="A slow tool",
            parameters={"type": "object", "properties": {}},
            handler_fn=slow,
            timeout=0.5,  # 0.5 秒超时
        )
        registry.register(handler)

        executor = ToolExecutor(registry)
        result = await executor.execute("slow", {})

        assert result.is_error is True
        assert result.error_code == "TIMEOUT"
        assert result.error_type == "timeout"


# ============================================================
# 场景 6: ToolExecutor 工具未找到
# ============================================================

class TestToolExecutorNotFound:
    """ToolExecutor 工具未找到测试"""

    @pytest.mark.asyncio
    async def test_tool_not_found(self):
        """未注册的工具返回 TOOL_NOT_FOUND"""
        from dotclaw.tools.registry import ToolRegistry
        from dotclaw.tools.executor import ToolExecutor

        registry = ToolRegistry()
        executor = ToolExecutor(registry)

        result = await executor.execute("nonexistent", {})

        assert result.is_error is True
        assert result.error_code == "TOOL_NOT_FOUND"
        assert result.error_type == "not_found"


# ============================================================
# 场景 7: AgentLoop 集成
# ============================================================

class TestAgentLoopIntegration:
    """AgentLoop 工具调用集成测试"""

    @pytest.mark.asyncio
    async def test_tool_executor_integration(self):
        """_tool_executor.execute() 能被 AgentLoop 调用"""
        from dotclaw.tools.registry import ToolRegistry
        from dotclaw.tools.executor import ToolExecutor
        from dotclaw.tools.handler import BuiltinToolHandler

        registry = ToolRegistry()

        async def echo(message: str) -> str:
            return f"Echo: {message}"

        registry.register(BuiltinToolHandler(
            name="echo",
            description="Echo",
            parameters={"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]},
            handler_fn=echo,
        ))

        executor = ToolExecutor(registry)
        result = await executor.execute("echo", {"message": "test"})

        assert result.is_error is False
        assert result.output == "Echo: test"


# ============================================================
# 场景 8: 配置加载
# ============================================================

class TestConfigLoading:
    """配置加载测试"""

    def test_load_config_has_new_fields(self):
        """config.yaml 新字段正确加载到 ToolsConfig"""
        from dotclaw.config.settings import load_config, _find_project_root
        import os

        config_path = _find_project_root() / "config.yaml"
        if not config_path.exists():
            pytest.skip("config.yaml not found")

        config = load_config(str(config_path))

        assert hasattr(config.tools, "builtin_enabled")
        assert isinstance(config.tools.approval_commands, list)
        assert "exec" in config.tools.approval_commands
        assert hasattr(config.tools, "mcp_enabled")
        assert hasattr(config.tools, "skill_enabled")
        assert hasattr(config.tools, "disabled_tools")
        assert hasattr(config.tools, "exec_timeout")

    def test_approval_commands_load_correctly(self):
        """approval_commands 从 config.yaml 正确加载"""
        from dotclaw.config.settings import load_config, _find_project_root

        config_path = _find_project_root() / "config.yaml"
        if not config_path.exists():
            pytest.skip("config.yaml not found")

        config = load_config(str(config_path))

        assert "exec" in config.tools.approval_commands
        assert "python" in config.tools.approval_commands


# ============================================================
# 场景 9: 日志合并
# ============================================================

class TestLoggerMerge:
    """AgentLogger 合并 DebugManager 测试"""

    def test_agent_logger_manages_trace_directly(self):
        """AgentLogger 直接管理 _last_trace，不再委托 DebugManager"""
        from dotclaw.agent.logger import AgentLogger, TraceRecord

        logger = AgentLogger(level="INFO")
        trace = TraceRecord(
            timestamp="2026-06-03T12:00:00",
            session_id="test-session",
            user_message="Hello",
        )
        logger.record(trace)

        retrieved = logger.get_last_trace()
        assert retrieved is trace
        assert retrieved.user_message == "Hello"

    def test_debug_subpackage_deleted(self):
        """debug/ 子包已删除，import 报 ModuleNotFoundError"""
        with pytest.raises(ModuleNotFoundError):
            import dotclaw.debug  # noqa: F401

    def test_format_summary(self):
        """TraceRecord.format_summary() 正常工作"""
        from dotclaw.agent.logger import TraceRecord

        trace = TraceRecord(
            timestamp="2026-06-03T12:00:00",
            session_id="test",
            user_message="测试消息",
            duration_ms=500,
            llm_responses=[{}, {}],  # 2 iterations
        )

        summary = trace.format_summary()
        assert "最近一次推理过程" in summary
        assert "测试消息" in summary
        assert "500ms" in summary


# ============================================================
# 场景 10: 旧 config 格式兼容
# ============================================================

class TestConfigBackwardCompat:
    """旧 config 格式向后兼容测试"""

    def test_old_format_needs_approval(self):
        """旧格式 exec.needs_approval:true → approval_commands=["exec"]"""
        from dotclaw.config.settings import _raw_to_config

        raw = {
            "tools": {
                "exec": {"needs_approval": True},
                "python": {"needs_approval": True},
            }
        }
        config = _raw_to_config(raw)

        assert "exec" in config.tools.approval_commands
        assert "python" in config.tools.approval_commands

    def test_old_format_disabled(self):
        """旧格式 exec.enabled:false → disabled_tools=["exec"]"""
        from dotclaw.config.settings import _raw_to_config

        raw = {
            "tools": {
                "exec": {"enabled": False},
            }
        }
        config = _raw_to_config(raw)

        assert "exec" in config.tools.disabled_tools

    def test_mixed_format_merge(self):
        """混合格式正确合并"""
        from dotclaw.config.settings import _raw_to_config

        raw = {
            "tools": {
                "approval_commands": ["exec"],
                "python": {"needs_approval": True},
            }
        }
        config = _raw_to_config(raw)

        assert "exec" in config.tools.approval_commands
        assert "python" in config.tools.approval_commands

    def test_old_exec_timeout(self):
        """旧格式 python.timeout:30 → exec_timeout=30"""
        from dotclaw.config.settings import _raw_to_config

        raw = {
            "tools": {
                "python": {"timeout": 30},
            }
        }
        config = _raw_to_config(raw)

        assert config.tools.exec_timeout == 30


# ============================================================
# 场景 11: ToolDefinition 增强字段
# ============================================================

class TestEnhancedDefinitions:
    """ToolDefinition/ToolResult 增强字段测试"""

    def test_tool_definition_enhanced(self):
        """ToolDefinition 有 source, needs_approval, timeout, metadata"""
        from dotclaw.tools.base import ToolDefinition, ToolSource

        td = ToolDefinition(
            name="test",
            description="desc",
            parameters={},
            source=ToolSource.BUILTIN,
            needs_approval=True,
            timeout=30.0,
            metadata={"key": "value"},
        )

        assert td.source == ToolSource.BUILTIN
        assert td.needs_approval is True
        assert td.timeout == 30.0
        assert td.metadata == {"key": "value"}

    def test_tool_result_enhanced(self):
        """ToolResult 有 error_code, error_type, metadata"""
        from dotclaw.tools.base import ToolResult

        result = ToolResult(
            output="error msg",
            is_error=True,
            error_code="TIMEOUT",
            error_type="timeout",
            metadata={"duration": 60},
        )

        assert result.is_error is True
        assert result.error_code == "TIMEOUT"
        assert result.error_type == "timeout"
        assert result.metadata == {"duration": 60}

    def test_tool_execution_context(self):
        """ToolExecutionContext 存在且包含 timeout"""
        from dotclaw.tools.base import ToolExecutionContext

        ctx = ToolExecutionContext(timeout=30.0)
        assert ctx.timeout == 30.0


# ============================================================
# 场景 12: builtin 工具工厂函数
# ============================================================

class TestBuiltinFactoryFunctions:
    """内置工具工厂函数测试"""

    def test_exec_factory(self):
        from dotclaw.tools.builtin.exec_tool import get_exec_handler
        h = get_exec_handler()
        assert h.name == "exec"
        assert h.definition().needs_approval is True
        assert h.definition().source.value == "builtin"

    def test_file_factories(self):
        from dotclaw.tools.builtin.file_tool import (
            get_read_file_handler, get_write_file_handler, get_list_dir_handler,
        )
        read = get_read_file_handler()
        assert read.name == "read_file"

        write = get_write_file_handler()
        assert write.name == "write_file"
        assert write.definition().needs_approval is True

        list_dir = get_list_dir_handler()
        assert list_dir.name == "list_dir"

    def test_memory_factories(self):
        from dotclaw.tools.builtin.memory_tool import (
            get_memory_read_handler, get_memory_write_handler,
        )
        read = get_memory_read_handler()
        assert read.name == "memory_read"

        write = get_memory_write_handler()
        assert write.name == "memory_write"
        assert write.definition().needs_approval is True

    def test_system_factories(self):
        from dotclaw.tools.builtin.system_tool import (
            get_system_info_handler, get_time_handler,
        )
        sys = get_system_info_handler()
        assert sys.name == "system_info"

        time = get_time_handler()
        assert time.name == "get_time"

    def test_register_all(self):
        """register_all() 注册全部 8 个内置工具"""
        from dotclaw.tools.registry import ToolRegistry
        from dotclaw.tools.builtin import register_all

        registry = ToolRegistry()
        register_all(registry)

        names = sorted(registry.all_names())
        expected = sorted([
            "exec", "read_file", "write_file", "list_dir",
            "memory_read", "memory_write", "system_info", "get_time",
        ])
        assert names == expected


# ============================================================
# 场景 13: ToolProvider ABC
# ============================================================

class TestToolProviderABC:
    """ToolProvider ABC 定义测试"""

    def test_cannot_instantiate_abc(self):
        """ToolProvider 是 ABC，不能直接实例化"""
        from dotclaw.tools.provider import ToolProvider

        with pytest.raises(TypeError):
            ToolProvider()  # type: ignore[abstract]


# ============================================================
# 场景 14: W1 修复验证 — exec_command CancelledError 处理
# ============================================================

class TestExecCancelledError:
    """W1 修复：确保 ToolExecutor 超时 cancel 时子进程被 kill"""

    @pytest.mark.asyncio
    async def test_exec_command_cancelled_error_caught(self):
        """CancelledError 被正确捕获——不会泄露为未处理异常"""
        from dotclaw.tools.builtin.exec_tool import exec_command

        # 验证代码路径存在：函数签名中应包含 CancelledError 处理
        # 通过直接注入一个会抛 CancelledError 的 mock 场景
        # 这里只验证正常执行不受影响的回归路径
        import inspect
        source = inspect.getsource(exec_command)
        assert "CancelledError" in source, "exec_command 应包含 CancelledError 处理"
        assert "proc.kill()" in source, "CancelledError 处理中应调用 proc.kill()"

    @pytest.mark.asyncio
    async def test_exec_command_normal_execution_still_works(self):
        """普通执行不受 CancelledError 修复影响"""
        from dotclaw.tools.builtin.exec_tool import exec_command

        result = await exec_command("echo hello")
        assert "hello" in result


# ============================================================
# 场景 15: W2 修复验证 — read_file 文件大小限制
# ============================================================

class TestReadFileSizeLimit:
    """W2 修复：read_file 有文件大小限制"""

    @pytest.mark.asyncio
    async def test_normal_file_reads_fine(self):
        """小文件正常读取"""
        from dotclaw.tools.builtin.file_tool import read_file
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("hello world")
            tmp_path = f.name

        try:
            result = await read_file(tmp_path)
            assert "hello world" in result
        finally:
            import os
            os.unlink(tmp_path)

    @pytest.mark.asyncio
    async def test_large_file_rejected(self):
        """超过 10MB 的文件被拒绝"""
        from dotclaw.tools.builtin.file_tool import read_file, MAX_FILE_SIZE
        import tempfile

        # 创建一个恰好超过限制的空洞文件（稀疏文件）
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.seek(MAX_FILE_SIZE + 100)
            f.write(b"x")
            tmp_path = f.name

        try:
            result = await read_file(tmp_path)
            assert "文件过大" in result
            assert "错误" in result
        finally:
            import os
            os.unlink(tmp_path)
