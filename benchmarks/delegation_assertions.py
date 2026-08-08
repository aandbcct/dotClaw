"""PR7 委派事实断言：只读取 Benchmark 采样，不参与 Runtime 控制流。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .eval_baseline_models import BenchmarkSample


@dataclass(frozen=True)
class DelegationAssertion:
    """一项父子链路判定（名称、是否通过、说明）。"""

    name: str
    passed: bool
    detail: str


def assert_delegation_chain(sample: BenchmarkSample) -> tuple[DelegationAssertion, ...]:
    """检查单条链路的一次提交、一次回灌、事件与隔离计数。"""
    required_ids = (sample.parent_run_id, sample.child_run_id, sample.task_id,
                    sample.parent_session_id, sample.child_session_id, sample.target_agent_id)
    checks = [
        DelegationAssertion("chain_identity", all(required_ids), "父子 Run、Task、Session 与目标 Agent 必须齐全"),
        DelegationAssertion("single_submit", sample.delegation_submit_count == 1, "每条链路只允许一次子 Run 提交"),
        DelegationAssertion("single_backfill", sample.result_backfill_count == 1, "每条链路只允许一次结果回灌"),
        DelegationAssertion("submitted_event", sample.delegation_submitted_event_count == 1, "提交事件必须恰好一次"),
        DelegationAssertion("completed_event", sample.delegation_completed_event_count == 1, "完成事件必须恰好一次"),
    ]
    for name, value in (("message", sample.cross_chain_message_count), ("context", sample.cross_chain_context_count),
                        ("tool", sample.cross_chain_tool_count), ("stream", sample.cross_chain_stream_count),
                        ("misdelivery", sample.misdelivery_count)):
        checks.append(DelegationAssertion(f"isolation_{name}", value == 0, f"跨链路 {name} 必须为 0"))
    return tuple(checks)


def assert_cancellation(sample: BenchmarkSample) -> tuple[DelegationAssertion, ...]:
    """检查父取消向子 Run 送达、生效以及同 Session 后续请求释放。"""
    return (
        DelegationAssertion("cancel_delivery", sample.cancel_delivery_ms is not None, "必须记录取消送达耗时"),
        DelegationAssertion("parent_cancel_effect", sample.parent_cancel_effect_ms is not None, "必须记录父 Run 取消生效耗时"),
        DelegationAssertion("child_cancel_effect", sample.child_cancel_effect_ms is not None, "必须记录子 Run 取消生效耗时"),
        DelegationAssertion("followup_started", sample.followup_started is True, "取消后后续请求必须已开始"),
        DelegationAssertion("followup_completed", sample.followup_completed is True, "取消后后续请求必须完成"),
    )


def passed(checks: Sequence[DelegationAssertion]) -> bool:
    """汇总一组断言。"""
    return all(check.passed for check in checks)
