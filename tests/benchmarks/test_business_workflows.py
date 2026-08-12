"""PR8 真实 Session 工作流回归测试。"""

import pytest

from benchmarks.business_workflows import run_compressed_history_continuation, run_preference_aware_followup


@pytest.mark.asyncio
async def test_preference_workflow_commits_and_reuses_session(tmp_path) -> None:
    """偏好必须经持久化 Session 进入下一请求，并约束最终交付。"""
    result = await run_preference_aware_followup(tmp_path)
    assert result.passed and result.preference_committed and result.preference_applied
    assert "简洁" in result.final_output and "验证" in result.final_output


@pytest.mark.asyncio
async def test_compression_workflow_uses_persisted_compression(tmp_path) -> None:
    """压缩摘要必须被下一请求使用，且关键历史约束继续约束最终交付。"""
    result = await run_compressed_history_continuation(tmp_path)
    assert result.passed and result.used_compression and result.key_constraint_retained
    assert "关键约束：先验证" in result.final_output
