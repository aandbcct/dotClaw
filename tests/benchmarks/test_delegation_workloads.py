"""PR7 真实 Runtime 委派 Fixture 回归测试。"""

from __future__ import annotations

import asyncio

import pytest

from benchmarks.delegation_workloads import ChildOutcome, DelegationWorkloadConfig, run_child_outcome, run_concurrent_completed, run_parent_cancellation


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", tuple(ChildOutcome))
async def test_child_outcome_backfills_once(tmp_path, outcome: ChildOutcome) -> None:
    """四种子 Run 终态均经真实父子链路回灌一次。"""
    facts = await run_child_outcome(tmp_path / outcome.value, DelegationWorkloadConfig(fake_delay_ms=1), outcome.value, outcome)
    assert facts["child_outcome"] == outcome.value
    assert facts["parent_outcome"] == "completed"
    assert facts["delegation_submit_count"] == 1
    assert facts["result_backfill_count"] == 1
    assert facts["delegation_submitted_event_count"] == 1
    assert facts["delegation_completed_event_count"] == 1
    assert facts["cross_chain_message_count"] == 0
    assert facts["cross_chain_context_count"] == 0
    assert facts["cross_chain_tool_count"] == 0
    assert facts["cross_chain_stream_count"] == 0
    assert facts["misdelivery_count"] == 0


@pytest.mark.asyncio
async def test_concurrent_parent_chains_are_isolated(tmp_path) -> None:
    """多个父 Session 并发时每条链路保留唯一父子 Run 与 Task 标识。"""
    facts = await run_concurrent_completed(tmp_path, DelegationWorkloadConfig(fake_delay_ms=1, concurrent_parents=3), 0)
    assert len(facts) == 3
    assert len({item["parent_run_id"] for item in facts}) == 3
    assert len({item["child_run_id"] for item in facts}) == 3
    assert len({item["task_id"] for item in facts}) == 3
    assert all(item["result_backfill_count"] == 1 for item in facts)
    assert all(item["cross_chain_message_count"] == 0 for item in facts)
    assert all(item["cross_chain_context_count"] == 0 for item in facts)
    assert all(item["cross_chain_tool_count"] == 0 for item in facts)
    assert all(item["cross_chain_stream_count"] == 0 for item in facts)
    assert all(item["misdelivery_count"] == 0 for item in facts)


@pytest.mark.asyncio
async def test_parent_cancellation_propagates_and_releases_session_lock(tmp_path) -> None:
    """父主动取消必须传至子 Run，随后同 Session follow-up 可正常完成。"""
    facts = await run_parent_cancellation(tmp_path, DelegationWorkloadConfig(fake_delay_ms=1), "parent-cancel")
    assert facts["submitted_waiting"] is True
    assert facts["parent_cancelled"] is True
    assert facts["child_cancelled"] is True
    assert facts["cancel_delivery_ms"] >= 0.0
    assert facts["parent_cancel_effect_ms"] >= 0.0
    assert facts["child_cancel_effect_ms"] >= 0.0
    assert facts["followup_started"] is True
    assert facts["followup_completed"] is True
