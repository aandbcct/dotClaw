"""PR4 单一子进程强退边界测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from benchmarks.recovery_subprocess import run_subprocess_recovery


def test_tool_before_effect_survives_forced_subprocess_exit(tmp_path: Path) -> None:
    """子进程在工具前退出后，父进程新服务可恢复同一 Run。"""
    result = asyncio.run(run_subprocess_recovery(tmp_path))
    assert result.exit_code == 97
    assert result.control_pass is True
    assert result.internal_pass is True
    assert result.tool_effect_count == 1
    assert result.evidence_summary["subprocess_exit_code"] == 97
    assert result.evidence_summary["subprocess_command"]
    assert result.evidence_summary["persisted_files"]
    assert result.evidence_summary["source_commit"]
