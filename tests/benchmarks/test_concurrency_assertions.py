"""concurrency_assertions 模块测试。"""

from __future__ import annotations

from benchmarks.concurrency_assertions import (
    CancelResult,
    IsolationResult,
    assert_cancel,
    assert_comparable_configs,
    assert_fifo_order,
    assert_isolation,
    check_isolation,
)
from benchmarks.concurrency_workloads import IdentifierCodec, RunFacts
from dotclaw.runtime.domain.events import RunEvent, RunEventType
from dotclaw.runtime.domain.facts import MessageRole, RunMessage, RunMessageKind, ToolCall


def _make_facts(
    run_id: str = "run-1",
    session_id: str = "s1",
    state_outcome: str = "completed",
    identifier: str = "s0_r0_req",
    final_content: str = "回答来自: s0_r0_req",
    events: tuple | None = None,
    messages: tuple | None = None,
) -> RunFacts:
    """构造测试用 RunFacts。"""
    return RunFacts(
        run_id=run_id,
        session_id=session_id,
        state_outcome=state_outcome,
        messages=messages or (
            RunMessage("llm-tool", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "调用工具", tool_calls=(ToolCall("call-1", "benchmark_echo", {"identifier": identifier}),)),
            RunMessage("tool-result", 2, RunMessageKind.TOOL_RESULT, MessageRole.TOOL, f"工具执行结果: {identifier}", tool_call_id="call-1"),
            RunMessage("msg-1", 3, RunMessageKind.FINAL_RESPONSE, MessageRole.ASSISTANT, final_content),
        ),
        events=events or (RunEvent(run_id, 1, RunEventType.TOOL_COMPLETED, "2026-01-01T00:00:00Z", message_ids=("tool-result",)),),
        final_message_content=final_content,
        identifier=identifier,
        context_versions=({"messages": [identifier]},),
        tool_records=(RunMessage("tool-result", 2, RunMessageKind.TOOL_RESULT, MessageRole.TOOL, f"工具执行结果: {identifier}", tool_call_id="call-1"),),
        stream_contents=(identifier,),
    )


class TestAssertFifoOrder:
    """FIFO 顺序断言测试。"""

    def test_all_completed_in_order(self):
        """全部按序完成。"""
        facts = [
            _make_facts("r1", state_outcome="completed", identifier="s0_r0_req"),
            _make_facts("r2", state_outcome="completed", identifier="s0_r1_req"),
            _make_facts("r3", state_outcome="completed", identifier="s0_r2_req"),
        ]
        results = assert_fifo_order(facts, [1, 2, 3])
        assert all(r.passed for r in results)

    def test_count_mismatch(self):
        """数量不匹配。"""
        results = assert_fifo_order([_make_facts()], [1, 2])
        assert any(not r.passed and "数" in r.detail for r in results)

    def test_empty_facts(self):
        """空 facts 列表。"""
        results = assert_fifo_order([], [])
        assert any(not r.passed for r in results)

    def test_incomplete_run(self):
        """Run 未完成。"""
        facts = [_make_facts("r1", state_outcome="running")]
        results = assert_fifo_order(facts, [1])
        assert any(not r.passed and "未完成" in r.detail for r in results)

    def test_order_with_events(self):
        """带事件的 FIFO 顺序。"""
        events1 = (RunEvent("r1", 1, RunEventType.RUN_STARTED, "2026-01-01T00:00:01Z", summary="start"),)
        events2 = (RunEvent("r2", 1, RunEventType.RUN_STARTED, "2026-01-01T00:00:02Z", summary="start"),)
        facts = [
            _make_facts("r1", events=events1, final_content="回答来自: s0_r0_req"),
            _make_facts("r2", events=events2, final_content="回答来自: s0_r1_req"),
        ]
        results = assert_fifo_order(facts, [1, 2])
        assert all(r.passed for r in results)


class TestCheckIsolation:
    """跨 Session 隔离检查测试。"""

    def test_no_leaks(self):
        """零串扰。"""
        facts_by_session = {
            0: [_make_facts("r0", identifier="s0_r0_req", final_content="回答来自: s0_r0_req")],
            1: [_make_facts("r1", identifier="s1_r0_req", final_content="回答来自: s1_r0_req")],
        }
        result = check_isolation(facts_by_session, 2)
        assert not result.any_leak
        assert result.message_leak_count == 0

    def test_message_leak(self):
        """消息包含其他 Session 标识。"""
        facts_by_session = {
            0: [_make_facts("r0", identifier="s0_r0_req", final_content="回答来自: s0_r0_req and s1_r0_req")],
            1: [_make_facts("r1", identifier="s1_r0_req", final_content="回答来自: s1_r0_req")],
        }
        result = check_isolation(facts_by_session, 2)
        assert result.message_leak_count > 0

    def test_event_leak_with_foreign_run_id(self):
        """持久化事件属于其他 Run 时必须失败。"""
        facts_by_session = {
            0: [_make_facts("r0", events=(RunEvent("foreign-run", 1, RunEventType.TOOL_COMPLETED, "2026-01-01T00:00:00Z", message_ids=("tool-result",)),))],
            1: [_make_facts("r1", final_content="回答: s1_r0_req")],
            2: [_make_facts("r2", final_content="回答: s2_r0_req")],
        }
        result = check_isolation(facts_by_session, 3)
        # Session 0 的事件 run_id 与持久化读取目标不一致
        assert result.event_leak_count > 0

    def test_missing_real_evidence_is_not_reported_as_zero_leak(self):
        """缺少 ContextVersion、工具或流事实时，隔离结论必须失败而非伪报零串扰。"""
        facts = _make_facts("r0", identifier="s0_r0_req", final_content="回答来自: s0_r0_req")
        facts = RunFacts(
            run_id=facts.run_id,
            session_id=facts.session_id,
            state_outcome=facts.state_outcome,
            messages=facts.messages,
            events=facts.events,
            final_message_content=facts.final_message_content,
            identifier=facts.identifier,
        )
        result = check_isolation({0: [facts]}, 1)
        assert result.context_leak_count == 1
        assert result.tool_leak_count == 1
        assert result.stream_leak_count == 1


class TestAssertIsolation:
    """隔离断言结果转换测试。"""

    def test_all_clean(self):
        """全部通过。"""
        iso = IsolationResult(total_requests=32, message_leak_count=0,
                              event_leak_count=0, context_leak_count=0,
                              tool_leak_count=0, stream_leak_count=0)
        results = assert_isolation(iso)
        assert all(r.passed for r in results)

    def test_with_leaks(self):
        """存在串扰。"""
        iso = IsolationResult(total_requests=32, message_leak_count=3,
                              event_leak_count=1, context_leak_count=0,
                              tool_leak_count=0, stream_leak_count=0,
                              details=["串扰详情"])
        results = assert_isolation(iso)
        assert any(not r.passed for r in results)
        # 消息和事件各一条失败断言
        failed = [r for r in results if not r.passed]
        assert len(failed) == 2


class TestAssertCancel:
    """取消断言测试。"""

    def test_all_success(self):
        """全部通过。"""
        cr = CancelResult(
            delivered=True, effective=True, lock_released=True,
            followup_completed=True, delivery_ms=5.0, effect_ms=50.0,
        )
        results = assert_cancel(cr)
        assert all(r.passed for r in results)

    def test_not_effective(self):
        """取消未生效。"""
        cr = CancelResult(
            delivered=True, effective=False, lock_released=False,
            followup_completed=False,
        )
        results = assert_cancel(cr)
        assert any(not r.passed and "未生效" in r.detail for r in results)

    def test_not_delivered(self):
        """取消未送达。"""
        cr = CancelResult(
            delivered=False, effective=False, lock_released=False,
            followup_completed=False,
        )
        results = assert_cancel(cr)
        assert any(not r.passed and "未送达" in r.detail for r in results)

    def test_followup_failed(self):
        """取消生效但后续请求未完成。"""
        cr = CancelResult(
            delivered=True, effective=True, lock_released=True,
            followup_completed=False,
        )
        results = assert_cancel(cr)
        assert any(not r.passed and "后续" in r.detail for r in results)


class TestAssertComparableConfigs:
    """可比性断言测试。"""

    def test_matching_configs(self):
        """配置一致。"""
        session = {"session_count": 8, "requests_per_session": 4, "fake_delay_ms": 20, "warmup": 5, "repeat": 100}
        global_cfg = {"session_count": 8, "requests_per_session": 4, "fake_delay_ms": 20, "warmup": 5, "repeat": 100}
        results = assert_comparable_configs(session, global_cfg)
        assert all(r.passed for r in results)

    def test_session_count_mismatch(self):
        """Session 数不一致。"""
        session = {"session_count": 8, "requests_per_session": 4, "fake_delay_ms": 20, "warmup": 5, "repeat": 100}
        global_cfg = {"session_count": 4, "requests_per_session": 4, "fake_delay_ms": 20, "warmup": 5, "repeat": 100}
        results = assert_comparable_configs(session, global_cfg)
        assert any(not r.passed for r in results)

    def test_delay_mismatch(self):
        """延迟不一致。"""
        session = {"session_count": 8, "requests_per_session": 4, "fake_delay_ms": 20, "warmup": 5, "repeat": 100}
        global_cfg = {"session_count": 8, "requests_per_session": 4, "fake_delay_ms": 40, "warmup": 5, "repeat": 100}
        results = assert_comparable_configs(session, global_cfg)
        assert any(not r.passed for r in results)
