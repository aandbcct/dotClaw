"""定时提醒模块"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..channel.base import Channel


class ReminderManager:
    """最简一次性提醒"""

    def __init__(self, channel: "Channel | None" = None):
        self._tasks: dict[str, asyncio.Task] = {}
        self._channel = channel

    def set_channel(self, channel: "Channel"):
        self._channel = channel

    async def set_reminder(self, delay_seconds: float, message: str) -> str:
        """
        设置一个一次性提醒。
        返回 reminder_id，可用于取消。
        """
        reminder_id = str(uuid.uuid4())[:8]

        async def _remind():
            try:
                await asyncio.sleep(delay_seconds)
                if self._channel:
                    await self._channel.send(f"⏰ 提醒: {message}")
            except asyncio.CancelledError:
                pass  # 被取消，正常退出

        self._tasks[reminder_id] = asyncio.create_task(_remind())
        return reminder_id

    async def cancel_reminder(self, reminder_id: str) -> bool:
        """取消一个提醒"""
        task = self._tasks.pop(reminder_id, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return True
        return False

    async def list_reminders(self) -> dict[str, str]:
        """返回当前所有提醒（id → 描述占位）"""
        return {k: "(运行中)" for k in self._tasks}
