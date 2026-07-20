"""非预算场景 RuntimeEngine 测试共用的强制预算 Port Fake。"""

from __future__ import annotations

from dotclaw.runtime.application.context_budget import TokenCountRequest, TokenCountResult
from dotclaw.runtime.application.history_compaction import HistoryCompactionRequest, HistoryCompactionResult
from dotclaw.runtime.application.ports import HistoryCompactorPort


class AlwaysWithinBudgetCounter:
    """为不覆盖压缩行为的测试提供真实调用所需的精确计数契约。"""

    async def count(self, request: TokenCountRequest) -> TokenCountResult:
        """返回低于测试策略窗口的稳定 Token 数。"""
        return TokenCountResult(1)


class UnexpectedHistoryCompactor(HistoryCompactorPort):
    """确保预算内测试不会静默进入历史压缩分支。"""

    async def compact_history(self, request: HistoryCompactionRequest) -> HistoryCompactionResult:
        """任何压缩调用都表示测试前提被破坏。"""
        raise AssertionError("预算内测试不应调用历史压缩器")
