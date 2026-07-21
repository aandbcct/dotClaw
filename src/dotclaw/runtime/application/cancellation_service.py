"""Runtime v4 的 run 级取消令牌管理。"""

from __future__ import annotations

from dotclaw.runtime.application.execution import CancellationToken


class CancellationService:
    """集中管理活动 Run 的取消令牌，不保存业务执行状态。"""

    def __init__(self) -> None:
        self._tokens: dict[str, CancellationToken] = {}
        self._delegated_runs: dict[str, str] = {}

    def register(self, run_id: str, token: CancellationToken) -> None:
        """登记正在执行的 run 取消令牌。"""
        self._tokens[run_id] = token

    def unregister(self, run_id: str) -> None:
        """清理已结束 run 的取消令牌。"""
        self._tokens.pop(run_id, None)
        self._delegated_runs.pop(run_id, None)

    def request(self, run_id: str, reason: str) -> bool:
        """向活动 run 发送取消请求；不存在时返回 False。"""
        token: CancellationToken | None = self._tokens.get(run_id)
        if token is None:
            return False
        token.request(reason)
        return True

    def register_delegated_run(self, parent_run_id: str, child_run_id: str) -> None:
        """登记父运行当前等待的子运行，供取消协议向下传播。"""
        self._delegated_runs[parent_run_id] = child_run_id

    def delegated_run_id(self, parent_run_id: str) -> str | None:
        """返回父运行当前等待的子运行标识，不泄漏其他运行状态。"""
        return self._delegated_runs.get(parent_run_id)

    def clear_delegated_run(self, parent_run_id: str, child_run_id: str) -> None:
        """仅当映射仍指向指定子运行时清理，避免覆盖后续委派。"""
        if self._delegated_runs.get(parent_run_id) == child_run_id:
            self._delegated_runs.pop(parent_run_id, None)
