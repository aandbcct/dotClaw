"""Runtime v2 的 run 级取消令牌管理。"""

from __future__ import annotations

from ..domain.execution import CancellationToken


class CancellationService:
    """集中管理活动 Run 的取消令牌，不保存业务执行状态。"""

    def __init__(self) -> None:
        self._tokens: dict[str, CancellationToken] = {}

    def register(self, run_id: str, token: CancellationToken) -> None:
        """登记正在执行的 run 取消令牌。"""
        self._tokens[run_id] = token

    def unregister(self, run_id: str) -> None:
        """清理已结束 run 的取消令牌。"""
        self._tokens.pop(run_id, None)

    def request(self, run_id: str, reason: str) -> bool:
        """向活动 run 发送取消请求；不存在时返回 False。"""
        token: CancellationToken | None = self._tokens.get(run_id)
        if token is None:
            return False
        token.request(reason)
        return True
