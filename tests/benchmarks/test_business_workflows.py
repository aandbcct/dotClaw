"""PR8 真实 Session 工作流回归测试。"""

import pytest

from benchmarks.business_workflows import run_compressed_history_continuation, run_preference_aware_followup


@pytest.mark.asyncio
async def test_preference_workflow_commits_and_reuses_session(tmp_path) -> None:
    """偏好工作流经持久化 Session 和下一次请求完成。"""
    result = await run_preference_aware_followup(tmp_path)
    assert result.passed and result.preference_committed


@pytest.mark.asyncio
async def test_compression_workflow_uses_persisted_compression(tmp_path) -> None:
    """压缩工作流必须实际创建压缩并被下一请求读取。"""
    result = await run_compressed_history_continuation(tmp_path)
    assert result.passed and result.used_compression
