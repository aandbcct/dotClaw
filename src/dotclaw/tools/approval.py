"""工具审批管理器（Phase 5 重构）"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..channel.base import Channel


class ApprovalManager:
    """
    危险工具执行前需要用户确认。

    审批策略（双重）：
    1. ToolDefinition.needs_approval 声明式（工具自己声明）
    2. config.tools.approval_commands 列表（用户配置覆盖）

    Phase 5 关键变化：
    - 删除硬编码 NEEDS_APPROVAL = {"exec", "python"}
    - 新增 _approval_commands 集合，从 config.yaml 加载
    """

    def __init__(self, approval_commands: list[str] | None = None):
        self._enabled = True
        self._approval_commands = set(approval_commands or [])

    def set_enabled(self, enabled: bool):
        self._enabled = enabled

    def set_approval_commands(self, commands: list[str]):
        """从 config.yaml 加载需要审批的命令列表"""
        self._approval_commands = set(commands)

    async def check(
        self,
        tool_name: str,
        arguments: dict,
        channel: "Channel | None" = None,
    ) -> bool:
        """
        检查工具是否需要审批。

        逻辑：
        1. _enabled=False -> 全部放行
        2. tool_name 在 _approval_commands 中 -> 需要审批
        3. 否则放行
        """
        if not self._enabled:
            return True

        if tool_name not in self._approval_commands:
            return True

        if channel is None:
            # 无 channel 时默认放行（子 Agent 场景）
            return True

        # 通过 channel 向用户请求确认
        args_str = json.dumps(arguments, ensure_ascii=False, indent=2)
        confirm = await channel.ask_user(
            f"⚠️ 即将执行危险工具 `{tool_name}`\n"
            f"参数：{args_str}\n"
            f"确认执行？(y/n): "
        )
        return confirm.strip().lower() in ("y", "yes")
