"""测试 Runtime —— 编排引擎。"""

from dotclaw.runtime import Runtime


class TestRuntime:
    """Runtime 基本字段验证。"""

    def test_basic_fields(self) -> None:
        """基本字段正确赋值。"""
        runtime = Runtime(
            llm=None,  # type: ignore[arg-type]
            tool_executor=None,
            assembler=None,
            state_store=None,  # type: ignore[arg-type]
            agent_registry=None,  # type: ignore[arg-type]
            session_mgr=None,  # type: ignore[arg-type]
            run_mgr=None,  # type: ignore[arg-type]
            channel=None,
        )
        assert runtime.llm is None
        assert runtime.tool_executor is None
        assert runtime.assembler is None
        assert runtime.state_store is None
        assert runtime.agent_registry is None
        assert runtime.session_mgr is None
        assert runtime.run_mgr is None
        assert runtime.channel is None

    def test_optional_fields_default(self) -> None:
        """可选字段有合理的默认值。"""
        runtime = Runtime(
            llm=None,  # type: ignore[arg-type]
            tool_executor=None,
            assembler=None,
            state_store=None,  # type: ignore[arg-type]
            agent_registry=None,  # type: ignore[arg-type]
            session_mgr=None,  # type: ignore[arg-type]
            run_mgr=None,  # type: ignore[arg-type]
            channel=None,
        )
        assert runtime.memory_mgr is None
        assert runtime.skill_registry is None
        assert runtime.mcp_provider is None
        assert runtime.config is None
