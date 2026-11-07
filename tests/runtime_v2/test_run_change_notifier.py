"""运行变更提示的无丢唤醒和等待者清理契约。"""

import asyncio

import pytest

from dotclaw.runtime.application.run_change_notifier import RunChangeNotifier


async def test_notification_before_wait_and_merged_versions() -> None:
    notifier = RunChangeNotifier()
    version = notifier.version("run")
    for _ in range(100):
        notifier.notify("run")
    assert await notifier.wait("run", version, 0) == 100
    assert notifier.version("other") == 0
    assert not notifier._waiters


async def test_multiple_waiters_wake_and_other_run_is_isolated() -> None:
    notifier = RunChangeNotifier()
    tasks = [asyncio.create_task(notifier.wait("run", 0, 5)) for _ in range(2)]
    await asyncio.sleep(0)
    notifier.notify("other")
    assert all(not task.done() for task in tasks)
    notifier.notify("run")
    notifier.notify("run")
    assert await asyncio.gather(*tasks) == [2, 2]
    assert not notifier._waiters


async def test_timeout_and_cancel_remove_waiters() -> None:
    notifier = RunChangeNotifier()
    assert await notifier.wait("run", 0, 0) == 0
    task = asyncio.create_task(notifier.wait("run", 0, 5))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not notifier._waiters
    notifier.notify("run")
    assert notifier.version("run") == 1
