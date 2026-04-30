"""工具审批管理器"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..channel.base import Channel


# 需要审批的工具
NEEDS_APPROVAL = {"exec", "python"}


class ApprovalManager:
    """危险工具执行前需要用户确认"""

    def __init__(self):
        self._enabled = True

    def set_enabled(self, enabled: bool):
        self._enabled = enabled

    async def check(
        self,
        tool_name: str,
        arguments: dict,
        channel: "Channel | None" = None,
    ) -> bool:
        """
        检查工具是否需要审批。

        如果是危险工具且有 channel，则向用户请求确认。
        如果没有 channel，默认放行（子 Agent 场景）。
        """
        if tool_name not in NEEDS_APPROVAL:
            return True

        if not self._enabled:
            return True

        if channel is None:
            # 无 channel 时默认放行（子 Agent 不需要审批）
            return True

        # 通过 channel 向用户请求确认
        import json
        args_str = json.dumps(arguments, ensure_ascii=False, indent=2)
        confirm = await channel.ask_user(
            f"⚠️ 即将执行危险工具 `{tool_name}`\n"
            f"参数：{args_str}\n"
            f"确认执行？(y/n): "
        )
        return confirm.strip().lower() in ("y", "yes")
