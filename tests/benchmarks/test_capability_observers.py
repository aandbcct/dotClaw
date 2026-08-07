"""PR5 阶段观察与 Handler 屏障测试。"""

from __future__ import annotations

import pytest

from benchmarks.capability_matrix import CapabilityMatrixCase
from benchmarks.capability_reliability import run_matrix_case


@pytest.mark.asyncio
async def test_invalid_arguments_do_not_enter_any_downstream_stage() -> None:
    """参数校验失败不得触达 Broker、Policy、审批或 Handler。"""
    sample = await run_matrix_case(CapabilityMatrixCase("invalid", "cap.file.read", {"path": 1}, "INVALID_ARGUMENTS"))
    assert (sample.broker_entered, sample.policy_entered, sample.approval_entered, sample.handler_entered) == (0, 0, 0, 0)


@pytest.mark.asyncio
async def test_approved_file_path_is_broker_resolved_path() -> None:
    """获准文件调用的 Handler 路径必须与 Broker 校验资源一致。"""
    sample = await run_matrix_case(CapabilityMatrixCase("path", "cap.file.write", {"path": "draft.txt"}, "ALLOW", approval="approve"))
    assert sample.handler_entered == 1
    assert sample.resolved_path_match is True


@pytest.mark.asyncio
async def test_process_summary_redacts_sensitive_marker() -> None:
    """审批与审计摘要不保留测试密钥标记。"""
    sample = await run_matrix_case(CapabilityMatrixCase("secret", "cap.process.exec", {"command": "TOKEN=PR5_SECRET echo ok"}, "ALLOW", approval="approve"))
    assert sample.sensitive_leak_count == 0
    assert sample.approval_summary_redacted is True
