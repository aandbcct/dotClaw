"""PR4 受控异常与冷重建事实测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from benchmarks.recovery_reliability import run_recovery_sample


def test_tool_after_effect_blocks_unknown_replay_and_keeps_facts_single(tmp_path: Path) -> None:
    """工具效果未知时阻断自动重放，保留一个效果事实与不确定调用事实。"""
    sample = asyncio.run(run_recovery_sample(tmp_path, "tool_after_effect", 0, False))
    assert sample.control_recovery_pass is True
    assert sample.internal_facts_pass is True
    assert sample.external_effect_status is not None
    assert sample.external_effect_status.value == "once"
    assert sample.external_duplicate_count == 0
    assert sample.tool_effect_count == 1


def test_approval_cold_rebuild_reuses_suspended_run(tmp_path: Path) -> None:
    """审批冷重建经过公开入口收口，不重新请求审批。"""
    sample = asyncio.run(run_recovery_sample(tmp_path, "approval_cold_rebuild", 0, False))
    assert sample.control_recovery_pass is True
    assert sample.internal_facts_pass is True
    assert sample.tool_effect_count == 1
