"""只发送内存变更提示，观察者醒来后必须重新读取事实。"""

from __future__ import annotations

import asyncio


class RunChangeNotifier:
    """按运行维护版本和等待者，不保存事件队列。"""

    def __init__(self) -> None:
        self._versions: dict[str, int] = {}
        self._waiters: dict[str, set[asyncio.Future[None]]] = {}

    def version(self, run_id: str) -> int:
        """在读取持久化事实前取得通知版本。"""
        return self._versions.get(run_id, 0)

    def notify(self, run_id: str) -> None:
        """递增提示版本，不等待消费者。"""
        self._versions[run_id] = self.version(run_id) + 1
        for waiter in self._waiters.get(run_id, ()):
            if not waiter.done():
                waiter.set_result(None)

    async def wait(self, run_id: str, after_version: int, timeout: float) -> int:
        """等待变化或超时，并返回当前最新版本。"""
        if after_version < 0 or timeout < 0:
            raise ValueError("通知版本和超时必须非负")
        if self.version(run_id) > after_version:
            return self.version(run_id)
        waiter = asyncio.get_running_loop().create_future()
        waiters = self._waiters.setdefault(run_id, set())
        waiters.add(waiter)
        try:
            try:
                await asyncio.wait_for(waiter, timeout)
            except TimeoutError:
                pass
            return self.version(run_id)
        finally:
            waiters.remove(waiter)
            if not waiters:
                del self._waiters[run_id]
