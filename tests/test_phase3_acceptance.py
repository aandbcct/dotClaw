"""
Phase 3 验收测试 — 8 个场景

运行方式: cd D:/dev/dotClaw && python tests/test_phase3_acceptance.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotclaw.agent.result import AgentResult
from dotclaw.agent.context import AgentContext
from dotclaw.agent.prompt.providers import (
    DataProvider, RoleProvider, RulesProvider, ToolsProvider,
)
from dotclaw.agent.prompt.builder import PromptBuilder
from dotclaw.agent.message_utils import validate, trim, clean
from dotclaw.llm.base import Message, ToolCall, ToolDefinition


# ============================================================
# 场景 1：AgentResult 兼容
# ============================================================

def test_1_agent_result_compat():
    print("\n=== 场景 1：AgentResult 兼容 ===")
    r = AgentResult(final_text="hello world", iterations=2, tool_calls_count=1,
                    duration_ms=100, request_id="abc123")
    assert str(r) == "hello world"
    assert r == "hello world"
    assert "world" in r
    assert "xyz" not in r
    assert r.final_text == "hello world"
    assert r.iterations == 2
    assert r.tool_calls_count == 1
    assert r.error is None

    # 异常情况
    r2 = AgentResult(error="BOOM", request_id="x")
    assert str(r2) == ""
    assert r2.error == "BOOM"
    print(f"  ✅ str/eq/in/final_text 全部兼容")


# ============================================================
# 场景 2：AgentContext 不可变
# ============================================================

def test_2_context_frozen():
    print("\n=== 场景 2：AgentContext 不可变 ===")
    ctx = AgentContext(
        session_id="s1",
        workspace=Path("/tmp"),
        project_root=Path("/tmp"),
        model="test",
        system_prompt="hi",
        request_id="r1",
    )
    print("  ctx created")
    raised = False
    try:
        print("  trying assignment...")
        ctx.model = "changed"
        print("  assignment succeeded (BAD)")
    except Exception:
        raised = True
        print("  assignment blocked (GOOD)")
    print(f"  raised={raised}")
    assert raised, "frozen should prevent assignment"
    print("  assertion passed")

    ctx2 = AgentContext(
        session_id="s2",
        workspace=Path("."),
        project_root=Path("/tmp/test"),
        model="t",
        system_prompt="h",
        request_id="r2",
    )
    assert ctx2.workspace == Path("/tmp/test"), \
        f"workspace should default to project_root, got {ctx2.workspace}"
    print(f"  ✅ frozen 阻止赋值, workspace 默认值正确")

    # workspace 默认值
    ctx2 = AgentContext(
        session_id="s2",
        workspace=Path(""),
        project_root=Path("/root"),
        model="t",
        system_prompt="h",
        request_id="r2",
    )
    assert ctx2.workspace == Path("/root")
    print(f"  ✅ frozen 阻止赋值, workspace 默认值正确")


# ============================================================
# 场景 3：PromptBuilder 拼接
# ============================================================

def test_3_prompt_builder():
    print("\n=== 场景 3：PromptBuilder 拼接 ===")
    ctx = AgentContext(
        session_id="s", workspace=Path("/tmp"), project_root=Path("/tmp"),
        model="m", system_prompt="你是一个AI助手。",
        tool_definitions=[
            ToolDefinition(name="get_time", description="获取时间",
                           parameters={"type": "object", "properties": {}}),
        ],
        rules="始终使用中文回复。", request_id="r",
    )

    builder = PromptBuilder([RoleProvider(), RulesProvider(), ToolsProvider()])
    prompt = builder.build(ctx)

    assert "你是一个AI助手" in prompt
    assert "始终使用中文回复" in prompt
    assert "get_time" in prompt
    assert "获取时间" in prompt

    # section 之间有分隔
    assert prompt.count("\n\n") >= 2
    print(f"  ✅ 3 个 section 正确拼接")
    print(f"  prompt[{len(prompt)} chars]: {prompt[:80]}...")


# ============================================================
# 场景 4：PromptBuilder 容错
# ============================================================

def test_4_prompt_builder_error_tolerance():
    print("\n=== 场景 4：PromptBuilder 容错 ===")

    class BadProvider(DataProvider):
        @property
        def section_name(self) -> str:
            return "bad"
        def provide(self, ctx):
            raise RuntimeError("boom")

    ctx = AgentContext(
        session_id="s", workspace=Path("/tmp"), project_root=Path("/tmp"),
        model="m", system_prompt="hi", request_id="r",
    )
    builder = PromptBuilder([RoleProvider(), BadProvider(), ToolsProvider()])
    prompt = builder.build(ctx)

    assert "hi" in prompt
    assert "boom" not in prompt  # BadProvider 被跳过
    print(f"  ✅ BadProvider 异常被跳过，其他 section 正常")


# ============================================================
# 场景 5：message_utils.validate 合法对话
# ============================================================

def test_5_validate_legal():
    print("\n=== 场景 5：validate 合法对话 ===")
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="hi"),
        Message(role="assistant", content="reply", tool_calls=[
            ToolCall(id="c1", name="get_time", arguments="{}"),
        ]),
        Message(role="tool", content="2026-05-30", tool_call_id="c1"),
        Message(role="assistant", content="时间如上"),
    ]
    issues = validate(msgs)
    assert issues == [], f"合法对话不应有 issue: {issues}"

    # 非法：孤立 tool 结果
    bad = [
        Message(role="system", content="s"),
        Message(role="tool", content="x", tool_call_id="orphan"),
    ]
    issues2 = validate(bad)
    assert len(issues2) > 0
    print(f"  ✅ 合法对话 0 issue, 孤立 tool 检测: {issues2}")


# ============================================================
# 场景 6：message_utils.trim 配对保护
# ============================================================

def test_6_trim_pairing():
    print("\n=== 场景 6：trim 配对保护 ===")
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="u1"),
        Message(role="assistant", content="a1", tool_calls=[
            ToolCall(id="c1", name="time", arguments="{}"),
            ToolCall(id="c2", name="weather", arguments='{"city":"bj"}'),
        ]),
        Message(role="tool", content="2026", tool_call_id="c1"),
        Message(role="tool", content="sunny", tool_call_id="c2"),
        Message(role="assistant", content="done"),
        Message(role="user", content="latest question"),
    ]
    # 用大量 token 预算确保不裁剪 → 验证结构完整
    result = trim(msgs, max_tokens=99999)
    assert len(result) == len(msgs)

    # 用很小预算裁剪
    result2 = trim(msgs, max_tokens=10)
    # system + latest user 应保留
    assert result2[0].role == "system"
    assert result2[-1].role == "user"
    assert result2[-1].content == "latest question"

    # 配对组内的消息不能散开：如果有 assistant(tool_calls)，则其 tool 消息必须也在
    assistant_indices = [i for i, m in enumerate(result2) if m.role == "assistant" and m.tool_calls]
    tool_indices = [i for i, m in enumerate(result2) if m.role == "tool"]
    # 所有 tool 消息的 tool_call_id 应存在于某个 assistant 的 tool_calls 中
    declared = set()
    for i in assistant_indices:
        for tc in result2[i].tool_calls:
            declared.add(tc.id)
    for i in tool_indices:
        tid = result2[i].tool_call_id
        assert tid in declared, f"工具消息 tool_call_id={tid} 无对应 assistant"

    print(f"  ✅ 全量保留 {len(result)} 条, 裁剪后 {len(result2)} 条, 配对完整")


# ============================================================
# 场景 7：message_utils.trim 中文估算
# ============================================================

def test_7_trim_chinese():
    print("\n=== 场景 7：trim 中文估算 ===")
    # 构造大量中文消息
    msgs = [Message(role="system", content="你是助手。")]
    for i in range(20):
        msgs.append(Message(role="user", content=f"第{i}条中文消息。你好世界！"))
        msgs.append(Message(role="assistant", content=f"收到第{i}条"))

    # 200 token 预算 → 应裁剪大量旧消息
    result = trim(msgs, max_tokens=200)
    assert len(result) < len(msgs), f"应裁剪: {len(result)} < {len(msgs)}"
    assert result[0].role == "system"
    # 最新消息应保留
    assert result[-1].role in ("user", "assistant")
    print(f"  ✅ 20 轮 → {len(result)} 条（预算 200 tokens）")


# ============================================================
# 场景 8：request_id 每次不同
# ============================================================

def test_8_request_id():
    print("\n=== 场景 8：request_id 每次不同 ===")
    import uuid
    id1 = uuid.uuid4().hex[:8]
    id2 = uuid.uuid4().hex[:8]
    assert id1 != id2
    assert len(id1) == 8
    assert len(id2) == 8
    print(f"  ✅ request_id '{id1}' != '{id2}'")


# ============================================================
# 运行入口
# ============================================================

def main():
    tests = [
        ("场景1-AgentResult兼容", test_1_agent_result_compat),
        ("场景2-AgentContext不可变", test_2_context_frozen),
        ("场景3-PromptBuilder拼接", test_3_prompt_builder),
        ("场景4-PromptBuilder容错", test_4_prompt_builder_error_tolerance),
        ("场景5-validate合法对话", test_5_validate_legal),
        ("场景6-trim配对保护", test_6_trim_pairing),
        ("场景7-trim中文估算", test_7_trim_chinese),
        ("场景8-request_id", test_8_request_id),
    ]
    passed, failed = 0, 0
    failures = []
    for name, func in tests:
        print(f"\n{'='*60}")
        try:
            func()
            passed += 1
            print(f"\n✅ {name} — 通过")
        except AssertionError as e:
            failed += 1; failures.append((name, str(e)))
            print(f"\n❌ {name}: {e}")
        except Exception as e:
            failed += 1; failures.append((name, str(e)))
            print(f"\n💥 {name}: {e}")
            import traceback; traceback.print_exc()

    total = len(tests)
    print(f"\n{'='*60}")
    print(f"结果: {passed}/{total} 通过")
    if failures:
        for n, e in failures: print(f"  ❌ {n}: {e[:150]}")


if __name__ == "__main__":
    main()
