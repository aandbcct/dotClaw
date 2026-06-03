"""
Phase 1 验收测试 — 五个场景的端到端测试 + 回归验证（Phase 5 更新）

通过 Mock LLM 和 Fake Channel 模拟完整交互流程，
无需真实 API Key 即可运行。

运行方式:
    cd D:/dev/dotClaw
    python tests/test_phase1_acceptance.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

# 确保 src 在路径中
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotclaw.agent.loop import AgentLoop
from dotclaw.llm.base import ChatChunk, Message, ToolCall, ToolDefinition
from dotclaw.tools.base import ToolResult
from dotclaw.tools.registry import ToolRegistry
from dotclaw.tools.executor import ToolExecutor
from dotclaw.tools.handler import BuiltinToolHandler
from dotclaw.tools.approval import ApprovalManager
from dotclaw.memory.store import Session, SessionManager, SessionMessage
from dotclaw.config.settings import Config, AgentConfig, ToolsConfig, DebugConfig, LLMConfig


# ============================================================
# 测试辅助类
# ============================================================

class MockLLM:
    """模拟 LLM：按顺序返回预设的 ChatChunk 序列。"""

    def __init__(self, response_sequences: list[list[ChatChunk]]):
        self._sequences = response_sequences
        self._idx = 0
        self.calls: list[dict] = []

    async def chat(self, messages, tools=None, model=None, purpose="chat", stream=True):
        call_info = {
            "seq_idx": self._idx,
            "msg_count": len(messages),
            "tools_count": len(tools) if tools else 0,
            "messages": [(m.role, m.content[:50]) for m in messages],
        }
        self.calls.append(call_info)

        if self._idx >= len(self._sequences):
            yield ChatChunk(content="(fallback)", is_final=True)
            return

        seq = self._sequences[self._idx]
        self._idx += 1
        for chunk in seq:
            yield chunk


class FakeChannel:
    """模拟 Channel：收集输出，预设 ask_user 的返回值。"""

    def __init__(self, approval_answer: str = "y"):
        self.streams: list[str] = []
        self.sends: list[str] = []
        self.infos: list[str] = []
        self.errors: list[str] = []
        self.ask_prompts: list[str] = []
        self._approval_answer = approval_answer

    async def receive(self) -> str:
        return ""

    async def send(self, message: str) -> None:
        self.sends.append(message)

    async def stream(self, chunk: str) -> None:
        self.streams.append(chunk)

    async def ask_user(self, prompt: str) -> str:
        self.ask_prompts.append(prompt)
        return self._approval_answer

    def print_error(self, message: str) -> None:
        self.errors.append(message)

    def print_info(self, message: str) -> None:
        self.infos.append(message)


def make_config(tmpdir: str) -> Config:
    return Config(
        llm=LLMConfig(default_model="qwen-plus", stream=True),
        agent=AgentConfig(system_prompt="你是一个测试助手。"),
        tools=ToolsConfig(approval_commands=["exec", "python"]),
        debug=DebugConfig(level="INFO", log_file=""),
    )


def text(content: str) -> list[ChatChunk]:
    return [ChatChunk(content=content, is_final=True)]


def streaming(chunks: list[str]) -> list[ChatChunk]:
    seq = [ChatChunk(content=c) for c in chunks[:-1]]
    seq.append(ChatChunk(content=chunks[-1], is_final=True))
    return seq


def tool_call(name: str, args: dict, call_id: str = "call_001") -> list[ChatChunk]:
    return [
        ChatChunk(tool_call=ToolCall(id=call_id, name=name, arguments=json.dumps(args))),
        ChatChunk(is_final=True),
    ]


def _make_executor() -> ToolExecutor:
    """创建测试用 ToolExecutor（注册基础工具）"""
    registry = ToolRegistry()
    return ToolExecutor(registry)


# ============================================================
# 场景 1：纯文本对话
# ============================================================

async def test_1_text():
    print("\n=== 场景 1：纯文本对话 ===")
    mock = MockLLM([streaming(["你好", "！", "我是", "dotClaw", "助手"])])
    ch = FakeChannel()

    with tempfile.TemporaryDirectory() as td:
        cfg = make_config(td)
        sm = SessionManager(f"{td}/sessions")
        s = await sm.create("测试")
        agent = AgentLoop(llm=mock, session=s, session_mgr=sm, channel=ch, config=cfg)

        resp = await agent.run("你好")
        assert resp == "你好！我是dotClaw助手"
        assert ch.streams == ["你好", "！", "我是", "dotClaw", "助手"]
        assert len(mock.calls) == 1

        # 验证 Fix 2: load() 返回的 messages 是 SessionMessage 对象
        saved = await sm.load(s.id)
        assert saved is not None
        assert len(saved.messages) == 2
        assert isinstance(saved.messages[0], SessionMessage), \
            f"load() 应返回 SessionMessage 对象，实际: {type(saved.messages[0])}"
        assert saved.messages[0].role == "user"
        assert saved.messages[1].role == "assistant"

        print(f"  ✅ 流式输出正确，会话消息类型正确")


# ============================================================
# 场景 2：带工具调用的对话
# ============================================================

async def test_2_tool():
    print("\n=== 场景 2：带工具调用的对话 ===")

    # Phase 5: 使用 ToolExecutor + ToolRegistry 代替旧的 register_tool 装饰器
    registry = ToolRegistry()

    async def get_time() -> str:
        return "2026-05-28 17:30:00"

    registry.register(BuiltinToolHandler(
        name="get_time",
        description="获取当前时间",
        parameters={"type": "object", "properties": {}},
        handler_fn=get_time,
    ))

    executor = ToolExecutor(registry)

    mock = MockLLM([
        tool_call("get_time", {}, call_id="t1"),
        streaming(["现在", "是", "2026年5月28日", " 17:30"]),
    ])
    ch = FakeChannel()

    with tempfile.TemporaryDirectory() as td:
        cfg = make_config(td)
        sm = SessionManager(f"{td}/sessions")
        s = await sm.create("工具测试")
        agent = AgentLoop(
            llm=mock, session=s, session_mgr=sm, channel=ch, config=cfg,
            tool_executor=executor,
        )

        resp = await agent.run("现在几点了？")

        assert len(mock.calls) == 2, f"LLM 应调用 2 次，实际: {len(mock.calls)}"
        assert "2026年5月28日" in resp

        # 验证 Fix 1: 第 2 次调用的 messages 中应包含 assistant 的 tool_calls 消息
        call2_roles = [m[0] for m in mock.calls[1]["messages"]]
        assert "assistant" in call2_roles, (
            f"第2次调用应包含 assistant 消息（含 tool_calls），角色序列: {call2_roles}"
        )
        assert "tool" in call2_roles, f"第2次调用应包含 tool 结果消息，角色序列: {call2_roles}"

        print(f"  ✅ assistant tool_calls 消息正确注入, roles: {call2_roles}")


# ============================================================
# 场景 3：危险工具审批
# ============================================================

async def test_3_approval():
    print("\n=== 场景 3：危险工具审批 ===")

    # 3.1 用户同意
    print("  --- 3.1 用户同意 ---")

    # Phase 5: 注册 exec 工具（needs_approval=True）
    registry = ToolRegistry()

    async def exec_cmd(command: str) -> str:
        return f"输出: hello"

    registry.register(BuiltinToolHandler(
        name="exec",
        description="执行 Shell 命令",
        parameters={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
        handler_fn=exec_cmd,
        needs_approval=True,
    ))

    approval = ApprovalManager(approval_commands=["exec", "python"])
    executor = ToolExecutor(registry, approval)

    mock = MockLLM([
        tool_call("exec", {"command": "echo hello"}, call_id="e1"),
        text("命令执行成功: hello"),
    ])
    ch = FakeChannel(approval_answer="y")

    with tempfile.TemporaryDirectory() as td:
        cfg = make_config(td)
        sm = SessionManager(f"{td}/sessions")
        s = await sm.create("审批测试")
        agent = AgentLoop(
            llm=mock, session=s, session_mgr=sm, channel=ch, config=cfg,
            tool_executor=executor,
        )

        resp = await agent.run("请执行 echo hello")
        assert len(ch.ask_prompts) == 1, f"审批应触发 1 次，实际: {len(ch.ask_prompts)}"
        assert "exec" in ch.ask_prompts[0]
        print(f"  ✅ 审批触发正确，用户同意后执行")

    # 3.2 用户拒绝
    print("  --- 3.2 用户拒绝 ---")

    registry2 = ToolRegistry()

    async def exec_dangerous(command: str) -> str:
        return "不应该执行"

    registry2.register(BuiltinToolHandler(
        name="exec",
        description="执行 Shell 命令",
        parameters={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
        handler_fn=exec_dangerous,
        needs_approval=True,
    ))

    approval2 = ApprovalManager(approval_commands=["exec", "python"])
    executor2 = ToolExecutor(registry2, approval2)

    mock2 = MockLLM([
        tool_call("exec", {"command": "rm -rf /"}, call_id="e2"),
        text("操作已取消"),
    ])
    ch2 = FakeChannel(approval_answer="n")

    with tempfile.TemporaryDirectory() as td:
        cfg = make_config(td)
        tr = ToolRegistry()
        sm = SessionManager(f"{td}/sessions")
        s = await sm.create("拒绝测试")
        agent = AgentLoop(
            llm=mock2, session=s, session_mgr=sm, channel=ch2, config=cfg,
            tool_executor=executor2,
        )

        resp = await agent.run("请执行 rm -rf /")
        assert len(ch2.ask_prompts) == 1

        # 验证第2次调用中 tool 结果包含拒绝信息
        tool_msgs = [m for m in mock2.calls[1]["messages"] if m[0] == "tool"]
        assert len(tool_msgs) >= 1
        assert "拒绝" in tool_msgs[0][1], f"tool 结果应包含 '拒绝': {tool_msgs[0]}"
        print(f"  ✅ 用户拒绝后正确返回拒绝信息")


# ============================================================
# 场景 4：调试追踪
# ============================================================

async def test_4_debug():
    print("\n=== 场景 4：调试追踪 ===")
    mock = MockLLM([text("回答内容")])
    ch = FakeChannel()

    with tempfile.TemporaryDirectory() as td:
        cfg = make_config(td)
        sm = SessionManager(f"{td}/sessions")
        s = await sm.create("调试测试")
        agent = AgentLoop(llm=mock, session=s, session_mgr=sm, channel=ch, config=cfg)

        await agent.run("测试问题")
        agent.debug_trace(ch)

        trace = ch.infos[-1]
        assert "推理过程" in trace
        assert "测试问题" in trace
        print(f"  ✅ debug_trace 输出正确")


# ============================================================
# 场景 5：多轮对话
# ============================================================

async def test_5_multiturn():
    print("\n=== 场景 5：多轮对话 ===")
    mock = MockLLM([
        text("好的，我记住了，你叫张三。"),
        text("你叫张三。"),
    ])
    ch = FakeChannel()

    with tempfile.TemporaryDirectory() as td:
        cfg = make_config(td)
        sm = SessionManager(f"{td}/sessions")
        s = await sm.create("多轮测试")
        agent = AgentLoop(llm=mock, session=s, session_mgr=sm, channel=ch, config=cfg)

        r1 = await agent.run("我的名字是张三")
        r2 = await agent.run("我叫什么名字？")

        assert "张三" in r1
        assert "张三" in r2, f"第2轮应记住名字: {r2}"
        assert len(mock.calls) == 2

        call2_roles = mock.calls[1]["messages"]
        user_msgs = [(r, c) for r, c in call2_roles if r == "user"]
        assert any("张三" in c for _, c in user_msgs), f"历史应包含'张三': {user_msgs}"
        assert user_msgs[-1][1] == "我叫什么名字？"

        print(f"  ✅ 多轮对话记忆正确")


# ============================================================
# 回归验证：修复 2 — SessionManager.load() 类型
# ============================================================

async def test_regression_load_sessionmessage():
    """验证 load() 返回的 messages 是 SessionMessage 对象，不是 dict"""
    print("\n=== 回归验证：SessionManager.load() 类型正确 ===")

    with tempfile.TemporaryDirectory() as td:
        sm = SessionManager(f"{td}/sessions")
        s = await sm.create("类型测试")
        s.messages.append(SessionMessage(role="user", content="hello"))
        s.messages.append(SessionMessage(role="assistant", content="hi"))
        await sm.save(s)

        loaded = await sm.load(s.id)
        assert loaded is not None
        for msg in loaded.messages:
            assert isinstance(msg, SessionMessage), \
                f"load() 返回的 message 应为 SessionMessage，实际: {type(msg)}"
            assert hasattr(msg, "role")
            assert hasattr(msg, "content")

        # 验证 list_all 也正确
        all_sessions = await sm.list_all()
        assert len(all_sessions) >= 1
        for msg in all_sessions[0].messages:
            assert isinstance(msg, SessionMessage)

        print(f"  ✅ load() 和 list_all() 均返回 SessionMessage 对象")


# ============================================================
# 回归验证：工具注册（Phase 5 更新）
# ============================================================

def test_regression_tools_registered():
    """验证 builtin 工具通过 register_all() 正确注册"""
    print("\n=== 回归验证：工具注册（Phase 5）===")

    from dotclaw.tools.builtin import register_all

    tr = ToolRegistry()
    register_all(tr)
    definitions = tr.get_definitions()
    tool_names = {d.name for d in definitions}

    required = {"exec", "read_file", "write_file", "list_dir",
                "memory_read", "memory_write", "system_info", "get_time"}
    missing = required - tool_names

    assert len(missing) == 0, (
        f"工具注册不完整。缺失: {sorted(missing)}\n"
        f"已注册: {sorted(tool_names)}"
    )

    print(f"  ✅ 已注册 {len(definitions)} 个工具: {sorted(tool_names)}")


# ============================================================
# 运行入口
# ============================================================

async def main_async():
    # 运行场景测试
    scenarios = [
        ("场景1-纯文本对话", test_1_text),
        ("场景2-带工具调用", test_2_tool),
        ("场景3-危险工具审批", test_3_approval),
        ("场景4-调试追踪", test_4_debug),
        ("场景5-多轮对话", test_5_multiturn),
    ]

    passed, failed = 0, 0
    failures = []

    for name, func in scenarios:
        print(f"\n{'='*60}")
        try:
            await func()
            passed += 1
            print(f"\n✅ {name} — 通过")
        except (AssertionError, Exception) as e:
            failed += 1
            failures.append((name, str(e)))
            print(f"\n❌ {name} — 失败: {e}")
            if not isinstance(e, AssertionError):
                import traceback
                traceback.print_exc()

    # 运行回归验证
    print(f"\n{'='*60}")
    regressions = [
        ("回归-SessionMessage 类型", test_regression_load_sessionmessage),
        ("回归-工具注册", test_regression_tools_registered),
    ]

    for name, func in regressions:
        try:
            if asyncio.iscoroutinefunction(func):
                await func()
            else:
                func()
            passed += 1
            print(f"\n✅ {name} — 通过")
        except (AssertionError, Exception) as e:
            failed += 1
            failures.append((name, str(e)))
            print(f"\n❌ {name} — 失败: {e}")
            if not isinstance(e, AssertionError):
                import traceback
                traceback.print_exc()

    # 总结
    total = len(scenarios) + len(regressions)
    print(f"\n{'='*60}")
    print(f"测试结果: {passed}/{total} 通过")

    if failures:
        print(f"\n失败详情 ({len(failures)} 项):")
        for n, e in failures:
            print(f"  ❌ {n}: {e[:200]}")

    if failed == 0:
        print("\n🎉 所有场景和回归测试全部通过！")
    else:
        print(f"\n⚠️ {failed} 项测试失败，需要修复。")


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
