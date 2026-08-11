"""PR7 委派断言测试。"""

from benchmarks.delegation_assertions import assert_delegation_chain, passed
from .helpers import make_sample


def test_nonzero_isolation_fails() -> None:
    """任一可观测串扰非零必须失败。"""
    sample = make_sample(parent_run_id="p", child_run_id="c", task_id="t", parent_session_id="ps", child_session_id="cs", target_agent_id="a", delegation_submit_count=1, result_backfill_count=1, delegation_submitted_event_count=1, delegation_completed_event_count=1, cross_chain_message_count=1, cross_chain_context_count=0, cross_chain_tool_count=0, cross_chain_stream_count=0, misdelivery_count=0)
    assert not passed(assert_delegation_chain(sample))
