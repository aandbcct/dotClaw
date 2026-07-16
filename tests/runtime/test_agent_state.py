"""AgentState 状态机单元测试。

覆盖：
- 正常流程：IDLE → THINKING → ACTING → THINKING → RESPONDING → DONE
- 无工具调用：IDLE → THINKING → RESPONDING → DONE
- 安全阀：max_iterations / tool_loop_detection
- 错误处理：LLM error / tool error
- Handoff 流程
- TRUNCATED 自动过渡
- 非法状态转换抛出异常
"""

from __future__ import annotations

import pytest

from dotclaw.runtime.agent_state import (
    AgentPhase,
    AgentAction,
    AgentStatus,
    AgentState,
    AgentStartEvent,
    LLMResponseEvent,
    ToolsDoneEvent,
    ContinueEvent,
    AgentEvent,
)
from dotclaw.agent.agent import LLMResponse
from dotclaw.llm.base import Message, ToolCall


# ============================================================================
# 辅助函数
# ============================================================================

def _make_llm_response(
    content: str = "",
    tool_calls: list[ToolCall] | None = None,
    finish_reason: str = "stop",
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> LLMResponse:
    """创建测试用 LLMResponse。"""
    return LLMResponse(
        content=content,
        tool_calls=tool_calls or [],
        finish_reason=finish_reason,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _make_agent_state(
    task_id: str = "test-task",
    thread_id: str = "test-thread",
    agent_id: str = "test-agent",
    max_iterations: int = 5,
) -> AgentState:
    """创建测试用 AgentState。"""
    return AgentState(
        task_id=task_id,
        thread_id=thread_id,
        agent_id=agent_id,
        max_iterations=max_iterations,
    )


# ============================================================================
# 正常流程测试
# ============================================================================

class TestNormalFlow:
    """IDLE → THINKING → ACTING → THINKING → RESPONDING → DONE"""

    def test_start_transitions_to_thinking(self) -> None:
        """start 事件从 IDLE 转到 THINKING，返回 INVOKE_LLM。"""
        state: AgentState = _make_agent_state()
        assert state.phase == AgentPhase.IDLE

        action: AgentAction = state.handle_event(AgentStartEvent("你好"))

        assert state.phase == AgentPhase.THINKING
        assert action == AgentAction.INVOKE_LLM
        assert state.iteration == 1
        assert not state.is_terminal

    def test_llm_response_with_tools_transitions_to_acting(self) -> None:
        """有 tool_calls 的 LLM 响应 → ACTING → EXECUTE_TOOLS。"""
        state: AgentState = _make_agent_state()
        state.handle_event(AgentStartEvent("读文件"))

        tc: ToolCall = ToolCall(id="1", name="read_file", arguments='{"path":"a.txt"}')
        resp: LLMResponse = _make_llm_response(
            content="我来读取文件",
            tool_calls=[tc],
        )
        action: AgentAction = state.handle_event(LLMResponseEvent(resp))

        assert state.phase == AgentPhase.ACTING
        assert action == AgentAction.EXECUTE_TOOLS
        assert state.current_llm_response is resp

    def test_tools_done_continues_loop(self) -> None:
        """工具执行完成后回到 THINKING，iteration 递增。"""
        state: AgentState = _make_agent_state()
        state.handle_event(AgentStartEvent("读文件"))

        tc: ToolCall = ToolCall(id="1", name="read_file", arguments='{"path":"a.txt"}')
        state.handle_event(LLMResponseEvent(_make_llm_response(tool_calls=[tc])))

        tool_result: Message = Message(role="tool", content="文件内容", tool_call_id="1")
        action: AgentAction = state.handle_event(ToolsDoneEvent([tool_result]))

        assert state.phase == AgentPhase.THINKING
        assert action == AgentAction.INVOKE_LLM
        assert state.iteration == 2

    def test_final_response_completes(self) -> None:
        """无 tool_calls 的 LLM 响应 → RESPONDING → DONE。"""
        state: AgentState = _make_agent_state()
        state.handle_event(AgentStartEvent("你好"))

        resp: LLMResponse = _make_llm_response(
            content="你好！有什么可以帮助你的？",
            tool_calls=[],
        )
        action: AgentAction = state.handle_event(LLMResponseEvent(resp))

        assert state.is_terminal
        assert action == AgentAction.FINALIZE
        assert state.end_status == AgentStatus.COMPLETED

    def test_multi_tool_rounds_completes(self) -> None:
        """多轮工具调用后最终完成。"""
        state: AgentState = _make_agent_state(max_iterations=10)
        state.handle_event(AgentStartEvent("复杂任务"))

        # 第 1 轮
        tc1: ToolCall = ToolCall(id="1", name="search", arguments='{"q":"test"}')
        state.handle_event(LLMResponseEvent(_make_llm_response(tool_calls=[tc1])))
        state.handle_event(ToolsDoneEvent([
            Message(role="tool", content="搜索结果", tool_call_id="1")
        ]))

        # 第 2 轮
        tc2: ToolCall = ToolCall(id="2", name="read_file", arguments='{"path":"a.txt"}')
        state.handle_event(LLMResponseEvent(_make_llm_response(tool_calls=[tc2])))
        state.handle_event(ToolsDoneEvent([
            Message(role="tool", content="文件内容", tool_call_id="2")
        ]))

        # 第 3 轮：最终回复
        state.handle_event(LLMResponseEvent(_make_llm_response(
            content="任务完成，结果如下：...",
            tool_calls=[],
        )))

        assert state.is_terminal
        assert state.end_status == AgentStatus.COMPLETED
        assert state.iteration == 3
        assert state.tool_calls_total == 2


# ============================================================================
# 边界与异常测试
# ============================================================================

class TestEdgeCases:
    """安全阀、错误处理、边界场景。"""

    def test_max_iterations_stops_loop(self) -> None:
        """达到 max_iterations 后应强制结束。"""
        state: AgentState = _make_agent_state(max_iterations=2)
        state.handle_event(AgentStartEvent("任务"))

        # 第 1 轮
        tc: ToolCall = ToolCall(id="1", name="search", arguments='{"q":"x"}')
        state.handle_event(LLMResponseEvent(_make_llm_response(tool_calls=[tc])))
        state.handle_event(ToolsDoneEvent([Message(role="tool", content="r", tool_call_id="1")]))

        # 第 2 轮
        tc2: ToolCall = ToolCall(id="2", name="search", arguments='{"q":"y"}')
        state.handle_event(LLMResponseEvent(_make_llm_response(tool_calls=[tc2])))
        action: AgentAction = state.handle_event(ToolsDoneEvent([
            Message(role="tool", content="r2", tool_call_id="2")
        ]))

        # iteration 已经是 2，达到 max_iterations=2，应该停
        assert state.is_terminal
        assert action == AgentAction.FINALIZE
        assert state.error_message is not None
        assert "超出最大迭代次数" in state.error_message

    def test_tool_loop_detection_stops(self) -> None:
        """连续 3 次相同工具调用应被检测并终止。"""
        state: AgentState = _make_agent_state(max_iterations=10)
        state.handle_event(AgentStartEvent("任务"))

        for i in range(3):
            tc: ToolCall = ToolCall(
                id=str(i), name="same_tool",
                arguments='{"arg":"same"}'
            )
            state.handle_event(LLMResponseEvent(_make_llm_response(
                content=f"调用第{i+1}次",
                tool_calls=[tc],
            )))
            state.handle_event(ToolsDoneEvent([
                Message(role="tool", content=f"结果{i+1}", tool_call_id=str(i))
            ]))

        # 第 3 次后应检测到死循环并自动进入 DONE
        assert state.is_terminal
        assert state.error_message is not None
        assert "死循环" in state.error_message

    def test_tool_loop_different_args_no_detect(self) -> None:
        """不同参数的同名工具调用不应被误判为死循环。"""
        state: AgentState = _make_agent_state(max_iterations=10)
        state.handle_event(AgentStartEvent("任务"))

        for i in range(4):
            tc: ToolCall = ToolCall(
                id=str(i), name="search",
                arguments=f'{{"q":"query_{i}"}}'
            )
            state.handle_event(LLMResponseEvent(_make_llm_response(tool_calls=[tc])))
            state.handle_event(ToolsDoneEvent([
                Message(role="tool", content=f"r{i}", tool_call_id=str(i))
            ]))

        # 不同参数，不应触发死循环检测
        assert state.phase == AgentPhase.THINKING

    def test_llm_error_transitions_to_failed(self) -> None:
        """LLM 返回 error → FAILED。"""
        state: AgentState = _make_agent_state()
        state.handle_event(AgentStartEvent("任务"))

        resp: LLMResponse = LLMResponse(
            content="", tool_calls=[], finish_reason="error",
            input_tokens=0, output_tokens=0,
        )
        action: AgentAction = state.handle_event(LLMResponseEvent(resp))

        assert state.phase == AgentPhase.FAILED
        assert state.end_status == AgentStatus.FAILED
        assert action == AgentAction.FINALIZE
        assert state.is_terminal

    def test_tool_error_transitions_to_failed(self) -> None:
        """工具执行错误 → FAILED。"""
        state: AgentState = _make_agent_state()
        state.handle_event(AgentStartEvent("任务"))

        tc: ToolCall = ToolCall(id="1", name="bad_tool", arguments="{}")
        state.handle_event(LLMResponseEvent(_make_llm_response(tool_calls=[tc])))

        action: AgentAction = state.handle_event(ToolsDoneEvent(
            results=[],
            tool_error=True,
        ))

        assert state.phase == AgentPhase.FAILED
        assert state.end_status == AgentStatus.FAILED
        assert action == AgentAction.FINALIZE

    def test_truncated_auto_transitions_to_responding(self) -> None:
        """finish_reason="length" → TRUNCATED + WAIT。

        v2 行为：TRUNCATED 返回 WAIT 而非自动推进到 DONE，
        由 Runtime._step() 注入 "continue" 提示后推 ContinueEvent 续跑。
        直接调用 handle_event() 时返回 WAIT（等待 Runtime 介入）。
        """
        state: AgentState = _make_agent_state()
        state.handle_event(AgentStartEvent("长任务"))

        resp: LLMResponse = _make_llm_response(
            content="这是一段被截断的回复...",
            tool_calls=[],
            finish_reason="length",
        )
        action: AgentAction = state.handle_event(LLMResponseEvent(resp))

        # v2: TRUNCATED 不是终态，WAIT 等待 Runtime 推 ContinueEvent
        assert state.phase == AgentPhase.TRUNCATED
        assert action == AgentAction.WAIT
        assert not state.is_terminal

        # Runtime 检测到 TRUNCATED+WAIT → 设置续跑标记 → 推 ContinueEvent
        state._truncated_continue_allowed = True
        action = state.handle_event(ContinueEvent())

        # 续跑 → THINKING + INVOKE_LLM（由 Runtime 继续调用 LLM）
        assert state.phase == AgentPhase.THINKING
        assert action == AgentAction.INVOKE_LLM
        assert state.truncated_count == 1

    def test_truncated_max_count_falls_back(self) -> None:
        """truncated_count >= _MAX_TRUNCATED_CONTINUE 时回退到 RESPONDING。"""
        state: AgentState = _make_agent_state()
        state.handle_event(AgentStartEvent("长任务"))

        # 模拟 3 次截断后续跑
        resp: LLMResponse = _make_llm_response(
            content="截断...", tool_calls=[], finish_reason="length",
        )
        for i in range(3):
            action = state.handle_event(LLMResponseEvent(resp))
            assert action == AgentAction.WAIT
            state._truncated_continue_allowed = True
            action = state.handle_event(ContinueEvent())
            assert action == AgentAction.INVOKE_LLM
            assert state.truncated_count == i + 1

        # 第 4 次截断 → 超过上限，回退到 RESPONDING
        action = state.handle_event(LLMResponseEvent(resp))
        assert action == AgentAction.WAIT
        state._truncated_continue_allowed = True
        action = state.handle_event(ContinueEvent())
        # guard 跳过 priority 10，匹配 priority 20 → RESPONDING
        # _auto_chain: RESPONDING + ContinueEvent → DONE + FINALIZE
        assert action == AgentAction.FINALIZE
        assert state.is_terminal

    def test_stop_signal_from_tool(self) -> None:
        """工具返回 stop_signal → RESPONDING。"""
        state: AgentState = _make_agent_state()
        state.handle_event(AgentStartEvent("任务"))

        tc: ToolCall = ToolCall(id="1", name="task_complete", arguments="{}")
        state.handle_event(LLMResponseEvent(_make_llm_response(tool_calls=[tc])))

        action: AgentAction = state.handle_event(ToolsDoneEvent(
            results=[Message(role="tool", content="任务完成", tool_call_id="1")],
            stop_signal=True,
        ))

        assert state.is_terminal
        assert action == AgentAction.FINALIZE


# ============================================================================
# Handoff 测试
# ============================================================================

class TestHandoff:
    """Handoff 流程测试。"""

    def test_handoff_signal_from_tool(self) -> None:
        """工具返回 handoff_signal → HANDOFF。"""
        state: AgentState = _make_agent_state()
        state.handle_event(AgentStartEvent("分配任务"))

        tc: ToolCall = ToolCall(id="1", name="delegate", arguments='{"to":"agent_b"}')
        state.handle_event(LLMResponseEvent(_make_llm_response(tool_calls=[tc])))

        action: AgentAction = state.handle_event(ToolsDoneEvent(
            results=[Message(role="tool", content="转交给 agent_b", tool_call_id="1")],
            handoff_signal=True,
        ))

        assert state.is_terminal
        assert action == AgentAction.HANDOFF_TARGET
        assert state.end_status == AgentStatus.HANDOFF
        assert state.handoff_context == "转交给 agent_b"

    def test_handoff_priority_over_stop(self) -> None:
        """handoff 信号优先级高于 stop 信号。"""
        state: AgentState = _make_agent_state()
        state.handle_event(AgentStartEvent("任务"))

        tc: ToolCall = ToolCall(id="1", name="delegate", arguments="{}")
        state.handle_event(LLMResponseEvent(_make_llm_response(tool_calls=[tc])))

        action: AgentAction = state.handle_event(ToolsDoneEvent(
            results=[Message(role="tool", content="handoff", tool_call_id="1")],
            handoff_signal=True,
            stop_signal=True,  # 同时有 stop，但 handoff 优先
        ))

        assert state.is_terminal
        assert state.end_status == AgentStatus.HANDOFF


# ============================================================================
# 非法状态转换测试
# ============================================================================

class TestInvalidTransitions:
    """非法状态转换应抛出 ValueError。"""

    def test_start_from_non_idle_raises(self) -> None:
        """从 THINKING 发送 start 事件应报错。"""
        state: AgentState = _make_agent_state()
        state.handle_event(AgentStartEvent("任务"))

        with pytest.raises(ValueError, match="无法从 .* 响应 start"):
            state.handle_event(AgentStartEvent("又一次启动"))

    def test_llm_response_from_non_thinking_raises(self) -> None:
        """从 ACTING 发送 llm_response 事件应报错。"""
        state: AgentState = _make_agent_state()
        state.handle_event(AgentStartEvent("任务"))
        tc: ToolCall = ToolCall(id="1", name="tool", arguments="{}")
        state.handle_event(LLMResponseEvent(_make_llm_response(tool_calls=[tc])))

        with pytest.raises(ValueError, match="无法从 .* 响应 llm_response"):
            state.handle_event(LLMResponseEvent(_make_llm_response()))

    def test_tools_done_from_non_acting_raises(self) -> None:
        """从 THINKING 发送 tools_done 事件应报错。"""
        state: AgentState = _make_agent_state()
        state.handle_event(AgentStartEvent("任务"))

        with pytest.raises(ValueError, match="无法从 .* 响应 tools_done"):
            state.handle_event(ToolsDoneEvent([]))


# ============================================================================
# 属性和辅助方法测试
# ============================================================================

class TestProperties:
    """属性和辅助方法测试。"""

    def test_is_terminal_false_initially(self) -> None:
        """初始状态不是终止状态。"""
        state: AgentState = _make_agent_state()
        assert not state.is_terminal

    def test_is_terminal_true_after_done(self) -> None:
        """完成后 is_terminal 为 True。"""
        state: AgentState = _make_agent_state()
        state.handle_event(AgentStartEvent("任务"))
        state.handle_event(LLMResponseEvent(_make_llm_response(content="完成")))

        assert state.is_terminal

    def test_is_terminal_true_after_failed(self) -> None:
        """失败后 is_terminal 为 True。"""
        state: AgentState = _make_agent_state()
        state.handle_event(AgentStartEvent("任务"))
        state.handle_event(LLMResponseEvent(LLMResponse(
            content="", tool_calls=[], finish_reason="error",
            input_tokens=0, output_tokens=0,
        )))

        assert state.is_terminal

    def test_tool_calls_total_counts_correctly(self) -> None:
        """tool_calls_total 正确统计工具调用次数。"""
        state: AgentState = _make_agent_state()
        state.handle_event(AgentStartEvent("任务"))

        for i in range(3):
            tc: ToolCall = ToolCall(id=str(i), name=f"tool_{i}", arguments="{}")
            state.handle_event(LLMResponseEvent(_make_llm_response(tool_calls=[tc])))
            state.handle_event(ToolsDoneEvent([
                Message(role="tool", content=f"r{i}", tool_call_id=str(i))
            ]))

        assert state.tool_calls_total == 3

    def test_error_message_empty_on_success(self) -> None:
        """正常完成时 error_message 为 None。"""
        state: AgentState = _make_agent_state()
        state.handle_event(AgentStartEvent("任务"))
        state.handle_event(LLMResponseEvent(_make_llm_response(content="完成")))

        assert state.error_message is None

    def test_error_message_set_on_max_iterations(self) -> None:
        """超出迭代限制时 error_message 被设置。"""
        state: AgentState = _make_agent_state(max_iterations=1)
        state.handle_event(AgentStartEvent("任务"))

        tc: ToolCall = ToolCall(id="1", name="tool", arguments="{}")
        state.handle_event(LLMResponseEvent(_make_llm_response(tool_calls=[tc])))
        state.handle_event(ToolsDoneEvent([Message(role="tool", content="r", tool_call_id="1")]))

        assert state.error_message is not None
        assert "最大迭代次数" in state.error_message


# ============================================================================
# 多次状态转换完整性测试
# ============================================================================

class TestFullCycle:
    """完整生命周期测试。"""

    def test_full_recycle_with_multiple_rounds(self) -> None:
        """模拟完整 ReAct 循环：3 轮工具调用 + 最终回复。"""
        state: AgentState = _make_agent_state(task_id="full-1", max_iterations=10)

        # Start
        state.handle_event(AgentStartEvent("帮我分析文件"))

        # Round 1: list files
        tc1: ToolCall = ToolCall(id="1", name="list_files", arguments='{"dir":"."}')
        state.handle_event(LLMResponseEvent(_make_llm_response(tool_calls=[tc1])))
        state.handle_event(ToolsDoneEvent([
            Message(role="tool", content="a.txt, b.txt", tool_call_id="1")
        ]))
        assert state.phase == AgentPhase.THINKING
        assert state.iteration == 2

        # Round 2: read file
        tc2: ToolCall = ToolCall(id="2", name="read_file", arguments='{"path":"a.txt"}')
        state.handle_event(LLMResponseEvent(_make_llm_response(tool_calls=[tc2])))
        state.handle_event(ToolsDoneEvent([
            Message(role="tool", content="Hello World", tool_call_id="2")
        ]))
        assert state.phase == AgentPhase.THINKING
        assert state.iteration == 3

        # Round 3: search
        tc3: ToolCall = ToolCall(id="3", name="search", arguments='{"q":"Hello"}')
        state.handle_event(LLMResponseEvent(_make_llm_response(tool_calls=[tc3])))
        state.handle_event(ToolsDoneEvent([
            Message(role="tool", content="Found 5 results", tool_call_id="3")
        ]))
        assert state.phase == AgentPhase.THINKING
        assert state.iteration == 4

        # Final: response
        state.handle_event(LLMResponseEvent(_make_llm_response(
            content="分析完成，文件包含 'Hello World'，搜索结果 5 条。",
        )))

        assert state.is_terminal
        assert state.end_status == AgentStatus.COMPLETED
        assert state.tool_calls_total == 3
        assert state.iteration == 4
