"""运行事实观察：通知只负责唤醒，快照始终重读持久记录。"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass

from .cancellation_service import CancellationService
from .ports import RunRepository
from .run_change_notifier import RunChangeNotifier
from ..domain.cancellation import CancellationRecord
from ..domain.events import RunEvent, RunEventType
from ..domain.facts import AgentRun, RunMessage


_TERMINAL_EVENTS = frozenset({RunEventType.RUN_COMPLETED, RunEventType.RUN_CANCELLED,
                            RunEventType.RUN_FAILED, RunEventType.RUN_ABANDONED})


@dataclass(frozen=True)
class RunWatchCursor:
    """持久事件与取消控制记录的双游标。"""

    event_sequence: int = 0
    control_revision: int = 0

    def __post_init__(self) -> None:
        for value in (self.event_sequence, self.control_revision):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("观察游标必须为非负整数")

    @classmethod
    def parse(cls, value: str) -> RunWatchCursor:
        """严格解析传输游标，不接受负数、浮点或多余字段。"""
        match = re.fullmatch(r"e(0|[1-9][0-9]*)-c(0|[1-9][0-9]*)", value)
        if match is None or len(value) > 100:
            raise ValueError("运行观察游标格式无效")
        return cls(int(match[1]), int(match[2]))

    def __str__(self) -> str:
        return f"e{self.event_sequence}-c{self.control_revision}"


@dataclass(frozen=True)
class RunObservation:
    """保留原领域事实，提交状态独立于运行摘要，禁止推测终态。"""

    run: AgentRun
    events: tuple[RunEvent, ...]
    messages: tuple[RunMessage, ...]
    cancellation: CancellationRecord | None
    cursor: RunWatchCursor
    executing: bool
    generation: str
    committing: bool = False

    @property
    def terminal(self) -> bool:
        """只有提交完整的运行才允许关闭观察连接。"""
        return self.run.state.is_ended() and not self.committing


class RunWatchService:
    """复用唯一通知器，以短gate读取一致控制事实，不占用Session租约。"""

    def __init__(self, run_repository: RunRepository, cancellation_service: CancellationService,
                 run_change_notifier: RunChangeNotifier, generation: str) -> None:
        """绑定本次宿主装配的共享控制依赖与固定generation。"""
        self._repository: RunRepository = run_repository
        self._cancellation: CancellationService = cancellation_service
        self._notifier: RunChangeNotifier = run_change_notifier
        self._generation: str = generation

    async def snapshot(self, session_id: str, run_id: str) -> RunObservation:
        """纯读快照：不使用带隐式成功补偿的load_run。"""
        async with self._cancellation.gate(run_id):
            run = await self._repository.load_run_snapshot(session_id, run_id)
            if run is None:
                raise FileNotFoundError("运行不存在")
            committing = (await self._repository.has_pending_success_commit(session_id, run_id)
                          or await self._repository.has_pending_cancellation_commit(session_id, run_id))
            events = await self._repository.load_events(session_id, run_id)
            messages = await self._repository.load_messages(session_id, run_id)
            cancellation = await self._repository.load_cancellation(session_id, run_id)
            sequence = max((event.sequence for event in events
                            if not committing or event.event_type not in _TERMINAL_EVENTS), default=0)
            return RunObservation(run, events, messages, cancellation,
                RunWatchCursor(sequence, cancellation.revision if cancellation else 0),
                self._cancellation.has_driver(run_id), self._generation, committing)

    async def watch(self, session_id: str, run_id: str, cursor: RunWatchCursor | None = None) -> AsyncIterator[RunObservation]:
        """先取得通知版本再读快照，每次重连与15秒保底都发送完整快照。"""
        first = True
        while True:
            version = self._notifier.version(run_id)
            observation = await self.snapshot(session_id, run_id)
            if first and cursor is not None and (cursor.event_sequence > observation.cursor.event_sequence
                    or cursor.control_revision > observation.cursor.control_revision):
                raise ValueError("运行观察游标超出当前事实，请重新读取完整快照")
            first = False
            yield observation
            if observation.terminal:
                return
            await self._notifier.wait(run_id, version, 15)
