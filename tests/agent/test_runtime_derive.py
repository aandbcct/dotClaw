"""测试 Runtime.derive() —— 派生隔离。"""

import pytest

from dotclaw.runtime import Runtime
from dotclaw.channel.null import NullChannel


class FakeLLM:
    """测试用假 LLM。"""
    pass


class FakeToolExecutor:
    """测试用假 ToolExecutor。"""
    pass


class FakeAssembler:
    """测试用假 Assembler。"""

    def clone(self) -> "FakeAssembler":
        """模拟隔离的 Assembler 克隆。"""
        return FakeAssembler()


class FakeSessionManager:
    """测试用假 SessionManager。"""
    pass


class FakeRunManager:
    """测试用假 RunManager。"""
    pass


class FakeMemoryManager:
    """测试用假 MemoryManager。"""
    pass


class FakeSkillRegistry:
    """测试用假 SkillRegistry。"""
    pass


class FakeAgentRegistry:
    """测试用假 AgentRegistry。"""
    pass


class FakeChannel:
    """测试用假 Channel。"""
    pass


class TestRuntimeDerive:
    """Runtime.derive() 派生隔离。"""

    @pytest.fixture
    def runtime(self) -> Runtime:
        """构造最小 Runtime 供 derive 使用。"""
        return Runtime(
            llm=FakeLLM(),
            tool_executor=FakeToolExecutor(),
            assembler=FakeAssembler(),
            agent_registry=FakeAgentRegistry(),
            session_mgr=FakeSessionManager(),
            run_mgr=FakeRunManager(),
            channel=FakeChannel(),
            memory_mgr=FakeMemoryManager(),
            skill_registry=FakeSkillRegistry(),
        )

    def test_derive_shares_llm(self, runtime: Runtime) -> None:
        """derive 后 llm 引用相同。"""
        derived = runtime.derive()
        assert derived.llm is runtime.llm

    def test_derive_shares_session_mgr(self, runtime: Runtime) -> None:
        """derive 后 session_mgr 引用相同。"""
        derived = runtime.derive()
        assert derived.session_mgr is runtime.session_mgr

    def test_derive_clones_assembler(self, runtime: Runtime) -> None:
        """derive 后 Assembler 必须隔离，避免 Slot 缓存泄漏。"""
        derived = runtime.derive()
        assert derived.assembler is not runtime.assembler

    def test_derive_shares_memory_mgr(self, runtime: Runtime) -> None:
        """derive 后 memory_mgr 引用相同。"""
        derived = runtime.derive()
        assert derived.memory_mgr is runtime.memory_mgr

    def test_derive_shares_skill_registry(self, runtime: Runtime) -> None:
        """derive 后 skill_registry 引用相同。"""
        derived = runtime.derive()
        assert derived.skill_registry is runtime.skill_registry

    def test_derive_shares_agent_registry(self, runtime: Runtime) -> None:
        """derive 后 agent_registry 引用相同。"""
        derived = runtime.derive()
        assert derived.agent_registry is runtime.agent_registry

    def test_derive_default_channel_is_null(self, runtime: Runtime) -> None:
        """derive 不传 channel 时默认为 NullChannel。"""
        derived = runtime.derive()
        assert isinstance(derived.channel, NullChannel)

    def test_derive_override_channel(self, runtime: Runtime) -> None:
        """derive 可显式 override channel。"""
        custom = FakeChannel()
        derived = runtime.derive(channel=custom)
        assert derived.channel is custom

    def test_derive_is_different_instance(self, runtime: Runtime) -> None:
        """derive 返回新的 Runtime 实例，非同一对象。"""
        derived = runtime.derive()
        assert derived is not runtime

    def test_derive_keeps_tool_executor(self, runtime: Runtime) -> None:
        """derive 默认保留 tool_executor 引用。"""
        derived = runtime.derive()
        assert derived.tool_executor is runtime.tool_executor

    def test_derive_keeps_run_mgr(self, runtime: Runtime) -> None:
        """derive 默认保留 run_mgr 引用。"""
        derived = runtime.derive()
        assert derived.run_mgr is runtime.run_mgr

    def test_derive_keeps_config(self, runtime: Runtime) -> None:
        """derive 默认保留 config 引用。"""
        derived = runtime.derive()
        assert derived.config is runtime.config

    def test_derive_keeps_hidden_task_harness_metadata(self, runtime: Runtime) -> None:
        """target 的 Task ID 只经 Harness 上下文传递，不进入提示词。"""
        derived = runtime.derive(delegation_endpoint="target", delegation_task_id="task-1")
        assert derived.delegation_endpoint == "target"
        assert derived.delegation_task_id == "task-1"
