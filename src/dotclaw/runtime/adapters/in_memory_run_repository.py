"""用于 Runtime v3 契约测试的内存 RunRepository 适配器。"""

from __future__ import annotations

from dataclasses import replace

from ..application.dto import ConversationMessage
from ..domain.context import ContextVersion, StagedHistoryCompression, StagedHistoryCompressionStatus, SuccessCommitIntent
from ..domain.events import RunEvent
from ..domain.facts import AgentRun, MessageRole, RunMessage, RunStatus


class InMemoryRunRepository:
    """以精确领域事实模拟 v3 Run 仓储，不参与生产持久化。"""

    def __init__(self) -> None:
        """初始化隔离的内存事实表。"""
        self._runs: dict[tuple[str, str], AgentRun] = {}
        self._messages: dict[tuple[str, str], tuple[RunMessage, ...]] = {}
        self._context_versions: dict[tuple[str, str], tuple[ContextVersion, ...]] = {}
        self._events: dict[tuple[str, str], tuple[RunEvent, ...]] = {}
        self._conversations: dict[str, tuple[ConversationMessage, ...]] = {}

    async def create_run(self, run: AgentRun) -> None:
        """创建唯一 Run 摘要。"""
        key: tuple[str, str] = (run.session_id, run.run_id)
        if key in self._runs:
            raise FileExistsError(f"运行 {run.run_id} 已存在")
        self._runs[key] = run

    async def load_run(self, session_id: str, run_id: str) -> AgentRun | None:
        """按 Session 与 Run 标识读取摘要。"""
        return self._runs.get((session_id, run_id))

    async def find_run(self, run_id: str) -> AgentRun | None:
        """按全局唯一 Run 标识定位摘要。"""
        matches: tuple[AgentRun, ...] = tuple(run for run in self._runs.values() if run.run_id == run_id)
        if len(matches) > 1:
            raise ValueError(f"运行 {run_id} 在多个 Session 中重复出现")
        return matches[0] if matches else None

    async def list_active_runs(self, session_id: str) -> tuple[AgentRun, ...]:
        """返回仍占用 Session 的非终态 Run。"""
        terminal: frozenset[RunStatus] = frozenset({
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.ABANDONED,
        })
        return tuple(run for run in self._runs.values() if run.session_id == session_id and run.status not in terminal)

    async def save_run(self, run: AgentRun) -> None:
        """覆盖已创建的 Run 摘要。"""
        key: tuple[str, str] = (run.session_id, run.run_id)
        if key not in self._runs:
            raise FileNotFoundError(f"运行 {run.run_id} 尚未创建")
        self._runs[key] = run

    async def save_messages(self, session_id: str, run_id: str, messages: tuple[RunMessage, ...]) -> None:
        """替换连续且唯一的 Run Message 序列。"""
        _validate_messages(messages)
        self._messages[(session_id, run_id)] = messages

    async def append_context_version(
        self,
        session_id: str,
        run_id: str,
        context_version: ContextVersion,
    ) -> None:
        """仅允许按自然数连续追加不可变版本。"""
        key: tuple[str, str] = (session_id, run_id)
        existing: tuple[ContextVersion, ...] = self._context_versions.get(key, ())
        if context_version.version != len(existing) + 1:
            raise ValueError("Context Version 必须从 1 连续递增")
        self._context_versions[key] = existing + (context_version,)

    async def load_context_versions(self, session_id: str, run_id: str) -> tuple[ContextVersion, ...]:
        """读取 Run 的全部不可变版本。"""
        return self._context_versions.get((session_id, run_id), ())

    async def set_active_context_version(self, session_id: str, run_id: str, version: int) -> None:
        """保存已存在版本的活动引用。"""
        versions: tuple[ContextVersion, ...] = await self.load_context_versions(session_id, run_id)
        if version not in {item.version for item in versions}:
            raise ValueError("活动 Context Version 必须引用已保存版本")
        run: AgentRun | None = await self.load_run(session_id, run_id)
        if run is None:
            raise FileNotFoundError(f"运行 {run_id} 尚未创建")
        self._runs[(session_id, run_id)] = replace(run, active_context_version=version)

    async def save_staged_history_compressions(
        self,
        session_id: str,
        run_id: str,
        candidates: tuple[StagedHistoryCompression, ...],
    ) -> None:
        """保存候选控制信息，禁止重复标识。"""
        if len({candidate.candidate_id for candidate in candidates}) != len(candidates):
            raise ValueError("历史压缩候选标识必须唯一")
        run: AgentRun | None = await self.load_run(session_id, run_id)
        if run is None:
            raise FileNotFoundError(f"运行 {run_id} 尚未创建")
        self._runs[(session_id, run_id)] = replace(run, staged_history_compressions=candidates)

    async def save_success_commit_intent(
        self,
        session_id: str,
        run_id: str,
        intent: SuccessCommitIntent,
    ) -> None:
        """保存成功提交意图，并校验候选引用有效。"""
        run: AgentRun | None = await self.load_run(session_id, run_id)
        if run is None:
            raise FileNotFoundError(f"运行 {run_id} 尚未创建")
        candidate_ids: frozenset[str] = frozenset(
            candidate.candidate_id for candidate in run.staged_history_compressions
        )
        if intent.latest_candidate_id is not None and intent.latest_candidate_id not in candidate_ids:
            raise ValueError("成功提交意图引用了未知历史压缩候选")
        self._runs[(session_id, run_id)] = replace(run, success_commit_intent=intent)

    async def load_messages(self, session_id: str, run_id: str) -> tuple[RunMessage, ...]:
        """读取 Run Message 事实。"""
        return self._messages.get((session_id, run_id), ())

    async def append_event(self, session_id: str, event: RunEvent) -> None:
        """追加连续事件，并要求其消息引用已存在。"""
        key: tuple[str, str] = (session_id, event.run_id)
        message_ids: frozenset[str] = frozenset(message.message_id for message in self._messages.get(key, ()))
        if any(message_id not in message_ids for message_id in event.message_ids):
            raise ValueError("事件引用了尚未保存的消息")
        events: tuple[RunEvent, ...] = self._events.get(key, ())
        if event.sequence != len(events) + 1:
            raise ValueError("事件序号必须连续")
        self._events[key] = events + (event,)

    async def commit_success(
        self,
        run: AgentRun,
        final_message: RunMessage,
        completed_event: RunEvent,
        success_intent: SuccessCommitIntent,
    ) -> None:
        """以领域意图模拟幂等成功投影、终态事件与候选提交。"""
        if run.status is not RunStatus.COMPLETED or final_message.role is not MessageRole.ASSISTANT:
            raise ValueError("成功提交必须包含完成 Run 与 assistant 最终消息")
        if success_intent.run_id != run.run_id or success_intent.session_id != run.session_id:
            raise ValueError("成功提交意图必须属于当前运行")
        await self.append_event(run.session_id, completed_event)
        current: tuple[ConversationMessage, ...] = self._conversations.get(run.session_id, ())
        if not any(message.message_id == final_message.message_id for message in current):
            current = current + (ConversationMessage(
                final_message.message_id,
                MessageRole.ASSISTANT,
                final_message.content,
                run.ended_at or "",
            ),)
        self._conversations[run.session_id] = current
        candidates: tuple[StagedHistoryCompression, ...] = tuple(
            replace(
                candidate,
                status=(
                    StagedHistoryCompressionStatus.COMMITTED
                    if candidate.candidate_id == success_intent.latest_candidate_id
                    else candidate.status
                ),
            )
            for candidate in run.staged_history_compressions
        )
        await self.save_run(replace(run, staged_history_compressions=candidates, success_commit_intent=None))


def _validate_messages(messages: tuple[RunMessage, ...]) -> None:
    """校验消息序号连续且标识唯一。"""
    message_ids: set[str] = set()
    expected_sequence: int = 1
    message: RunMessage
    for message in messages:
        if message.sequence != expected_sequence or message.message_id in message_ids:
            raise ValueError("运行消息序号必须连续且标识唯一")
        message_ids.add(message.message_id)
        expected_sequence += 1
