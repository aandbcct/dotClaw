"""审批端口（Tool v1 阶段三重构）。

ApprovalManager 在阶段三重构为 Approval Port 的交互适配器：它只消费 Policy Engine
给出的 `ask` 决策，通过 Channel 向用户展示**已脱敏**的资源摘要并请求确认。它不再
维护工具名列表，也不再自行决定放行（总体设计 §4.3 / §6）。

关键不变量（总体设计 §4.3）：无可用交互通道（Channel）时，`ask` 必须拒绝，不能像
旧实现那样默认放行。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..channel.base import Channel


class ApprovalManager:
    """审批端口：把 Policy 的 ask 决策交给用户确认。

    仅负责交互；放行/拒绝的最终语义由调用方（ToolExecutor）根据返回值翻译为
    APPROVAL_DENIED 或继续执行。
    """

    def __init__(self) -> None:
        # 阶段三起不再持有命令列表；审批完全由 Policy 的 ask 决策驱动。
        self._enabled = True

    def set_enabled(self, enabled: bool) -> None:
        """启用/停用审批端口（停用视为所有 ask 直接拒绝）。"""
        self._enabled = enabled

    async def request(self, summary: str, channel: "Channel | None" = None) -> bool:
        """请求用户确认一次资源访问。

        Args:
            summary: 由 Broker 生成的脱敏资源摘要（不含密钥/认证头）。
            channel: 交互通道；为 None 时直接拒绝（无默认放行）。

        Returns:
            True 表示用户批准；False 表示拒绝或无通道。
        """
        if not self._enabled:
            return False
        if channel is None:
            # 无交互通道：默认拒绝（总体设计 §4.3 不变量 3）。
            return False
        confirm = await channel.ask_user(
            f"⚠️ 即将执行需要审批的操作\n资源：{summary}\n确认执行？(y/n): "
        )
        return confirm.strip().lower() in ("y", "yes")
