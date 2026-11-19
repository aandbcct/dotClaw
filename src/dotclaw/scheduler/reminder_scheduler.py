"""持久化提醒扫描的 APScheduler 生命周期。"""

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..personal_tasks.reminder_service import ReminderService

logger = logging.getLogger("dotclaw.scheduler.reminders")


class ReminderScheduler:
    def __init__(self, service: ReminderService) -> None:
        self._service = service
        self._scheduler = AsyncIOScheduler()
        self._stopping = False
        self._tick_lock = asyncio.Lock()

    async def start(self) -> None:
        await self._service.scan(startup=True)
        self._scheduler.add_job(self._tick, "interval", seconds=1, id="personal-reminders", max_instances=1, coalesce=True, misfire_grace_time=None)
        self._scheduler.start()

    async def _tick(self) -> None:
        async with self._tick_lock:
            if self._stopping:
                return
            try:
                await self._service.scan()
            except Exception:
                logger.exception("提醒扫描失败；下次 tick 将继续重试")

    async def stop(self) -> None:
        self._stopping = True
        if self._scheduler.running:
            self._scheduler.pause()
        async with self._tick_lock:
            if self._scheduler.running:
                self._scheduler.shutdown(wait=False)
