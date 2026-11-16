"""C06 账本恢复服务测试装配。"""

from pathlib import Path

from dotclaw.runtime.adapters import ApprovalRepositoryAdapter, CheckpointRepositoryAdapter, RunRepositoryAdapter
from dotclaw.runtime.application.approval_service import ApprovalService
from dotclaw.runtime.application.tool_recovery import ToolRecoveryService
from dotclaw.common.tool_replay import ToolExecutionEvidence
from dotclaw.runtime.application.dto import ToolInvocation, ToolRecoveryContract, ToolResult, ToolResultStatus
from dotclaw.runtime.application.execution import RunExecutionView
from dotclaw.runtime.application.ports import ToolPort
from dotclaw.runtime.domain.facts import ToolCall


class ToolPortTestBase(ToolPort):
    """为无审批测试工具提供明确的无副作用预检和禁止自动重试契约。"""

    async def execute(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        raise NotImplementedError

    async def preflight(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        return ToolResult(invocation.call.call_id, ToolResultStatus.COMPLETED,
            execution_evidence=ToolExecutionEvidence.NOT_STARTED)

    async def describe_recovery(self, call: ToolCall, execution: RunExecutionView) -> ToolRecoveryContract:
        return ToolRecoveryContract()


def file_tool_recovery(root: Path) -> ToolRecoveryService:
    """使用同一目录的文件仓储构造测试恢复服务。"""
    approvals = ApprovalRepositoryAdapter(root)
    return ToolRecoveryService(
        RunRepositoryAdapter(root, approval_repository=approvals),
        CheckpointRepositoryAdapter(root),
        ApprovalService(approvals),
    )
