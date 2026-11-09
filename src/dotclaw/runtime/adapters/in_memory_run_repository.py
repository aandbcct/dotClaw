"""用于 Runtime v4 契约测试的内存 RunRepository 适配器。"""

from __future__ import annotations

from dataclasses import replace
from ...common.utils import validate_path_segment

from ..application.dto import ConversationMessage
from ..application.ports import ApprovalRepository, CheckpointRepository, RUN_SESSION_MISMATCH_REASON
from ..domain.cancellation import CancellationCommitIntent, CancellationRecord, CancellationPhase
from .run_repository import _validate_cancellation_revision, _validate_cancellation_intent, _validate_cancellation_events
from ..domain.context import ContextVersion, StagedHistoryCompression, StagedHistoryCompressionStatus, SuccessCommitIntent
from ..domain.events import RunEvent
from ..domain.facts import AgentRun, MessageRole, RunMessage
from ..domain.state import Ended, RunOutcome


class InMemoryRunRepository:
    """以精确领域事实模拟 v4 Run 仓储，不参与生产持久化。"""

    def __init__(self, approval_repository: ApprovalRepository | None = None, checkpoint_repository: CheckpointRepository | None = None) -> None:
        """初始化隔离的内存事实表。"""
        self._approval_repository = approval_repository
        self._checkpoint_repository = checkpoint_repository
        self._cancellations: dict[tuple[str, str], CancellationRecord] = {}
        self._cancellation_intents: dict[tuple[str, str], CancellationCommitIntent] = {}
        self._runs: dict[tuple[str, str], AgentRun] = {}
        self._messages: dict[tuple[str, str], tuple[RunMessage, ...]] = {}
        self._context_versions: dict[tuple[str, str], tuple[ContextVersion, ...]] = {}
        self._events: dict[tuple[str, str], tuple[RunEvent, ...]] = {}
        self._conversations: dict[str, tuple[ConversationMessage, ...]] = {}

    async def load_cancellation(self, session_id: str, run_id: str) -> CancellationRecord | None:
        """仅返回指定运行的取消记录。"""
        return self._cancellations.get((session_id, run_id))

    async def save_cancellation(self, session_id: str, record: CancellationRecord, *, expected_revision: int) -> None:
        """比较修订号并保持首次请求不可变。"""
        key = (session_id, record.request.run_id)
        run = self._runs.get(key)
        if run is None:
            raise FileNotFoundError("取消记录的运行不存在")
        current = self._cancellations.get(key)
        _validate_cancellation_revision(current, record, expected_revision)
        if current is None and (run.state.is_ended() or await self.has_pending_success_commit(*key)):
            raise ValueError("已终态或成功提交中的运行不能接受取消")
        self._cancellations[key] = record

    async def list_child_runs(self, parent_run_id: str) -> tuple[AgentRun, ...]:
        """使用运行事实中的直接父关系查询。"""
        return tuple(sorted((run for run in self._runs.values() if run.parent_run_id == parent_run_id), key=lambda run: run.run_id))

    async def has_pending_success_commit(self, session_id: str, run_id: str) -> bool:
        """读取成功提交控制字段。"""
        run = self._runs.get((session_id, run_id))
        return run is not None and run.success_commit_intent is not None

    async def list_pending_cancellations(self) -> tuple[CancellationRecord, ...]:
        """settled 记录只保留审计用途。"""
        return tuple(record for record in self._cancellations.values() if record.phase is not CancellationPhase.SETTLED)

    async def has_pending_cancellation_commit(self, session_id: str, run_id: str) -> bool:
        """查询指定运行的冻结取消意图。"""
        intent = self._cancellation_intents.get((session_id, run_id))
        if intent is not None and ((intent.run.session_id, intent.run.run_id) != (session_id, run_id) or (session_id, run_id) not in self._runs):
            raise ValueError("取消意图归属不一致")
        return intent is not None

    async def recover_cancellation_commit(self, session_id: str, run_id: str) -> None:
        """只重放指定运行，不触碰其他运行的提交。"""
        await self.has_pending_cancellation_commit(session_id, run_id)
        intent = self._cancellation_intents.get((session_id, run_id))
        if intent is not None:
            await self.commit_cancellation(intent)

    async def commit_cancellation(self, intent: CancellationCommitIntent) -> None:
        """按文件实现同样的顺序提交，保留失败意图。"""
        key = (intent.run.session_id, intent.run.run_id)
        run = self._runs.get(key)
        if run is None:
            raise FileNotFoundError("取消运行不存在")
        if await self.has_pending_success_commit(*key):
            raise ValueError("成功与取消提交意图冲突")
        _validate_cancellation_intent(run, self._cancellations.get(key), intent)
        if intent.run.latest_checkpoint_id is not None and self._checkpoint_repository is None:
            raise ValueError("取消意图包含检查点，但未注入检查点仓储")
        if intent.approval_ids and self._approval_repository is None:
            raise ValueError("取消意图包含审批，但未注入审批仓储")
        if self._approval_repository is not None:
            for approval_id in intent.approval_ids:
                approval = await self._approval_repository.load(approval_id)
                if approval is not None and (approval.run_id, approval.session_id) != (intent.run.run_id, intent.run.session_id):
                    raise ValueError("取消审批不属于当前运行")
        if key in self._cancellation_intents and self._cancellation_intents[key] != intent:
            raise ValueError("已存在内容不同的取消意图")
        _validate_cancellation_events(self._events.get(key, ()), intent)
        self._cancellation_intents[key] = intent
        await self._recover_cancellation_commit(intent)

    async def _recover_cancellation_commit(self, intent: CancellationCommitIntent) -> None:
        key = (intent.run.session_id, intent.run.run_id)
        for event in intent.events:
            existing = self._events.get(key, ())
            if event.sequence <= len(existing):
                if existing[event.sequence - 1] != event:
                    raise ValueError("取消事件内容冲突")
            else:
                await self.append_event(intent.run.session_id, event)
        if self._approval_repository is not None:
            for approval_id in intent.approval_ids:
                await self._approval_repository.consume(approval_id)
        if self._checkpoint_repository is not None:
            await self._checkpoint_repository.delete(*key)
        await self.save_run(intent.run)
        self._cancellations[key] = intent.settled_record
        del self._cancellation_intents[key]

    async def recover_pending_cancellation_commits(self) -> None:
        """失败后重新验证并完成固定的取消决定。"""
        for intent in tuple(self._cancellation_intents.values()):
            await self.commit_cancellation(intent)

    async def list_runs(self) -> tuple[AgentRun, ...]:
        """与文件仓储保持相同的稳定事实顺序。"""
        return tuple(sorted(self._runs.values(), key=lambda run: (run.started_at, run.run_id)))

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

    async def load_run_snapshot(self, session_id: str, run_id: str) -> AgentRun | None:
        """与文件仓储保持相同的纯读归属语义。"""
        validate_path_segment(session_id, "session_id")
        validate_path_segment(run_id, "run_id")
        run = await self.find_run(run_id)
        if run is not None and run.session_id != session_id:
            raise PermissionError(RUN_SESSION_MISMATCH_REASON)
        return run

    async def list_active_runs(self, session_id: str) -> tuple[AgentRun, ...]:
        """纯读未完整提交终态的占用，与文件仓储保持相同规则。"""
        return tuple(
            run for run in self._runs.values()
            if run.session_id == session_id and (not run.state.is_ended() or run.success_commit_intent is not None
                                                or (run.session_id, run.run_id) in self._cancellation_intents)
        )

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
        key = (session_id, run_id)
        if key in self._cancellations or key in self._cancellation_intents:
            raise ValueError("取消已保留提交权，不能保存成功意图")
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

    async def load_events(self, session_id: str, run_id: str) -> tuple[RunEvent, ...]:
        """读取已追加的事件；隔离容器仅供测试，不构成生产第二事实源。"""
        return self._events.get((session_id, run_id), ())

    async def commit_success(
        self,
        run: AgentRun,
        final_message: RunMessage,
        completed_event: RunEvent,
        success_intent: SuccessCommitIntent,
    ) -> None:
        """以领域意图模拟幂等成功投影、终态事件与候选提交。"""
        if not (isinstance(run.state.mode, Ended) and run.state.mode.outcome is RunOutcome.COMPLETED) or final_message.role is not MessageRole.ASSISTANT:
            raise ValueError("成功提交必须包含完成 Run 与 assistant 最终消息")
        if success_intent.run_id != run.run_id or success_intent.session_id != run.session_id:
            raise ValueError("成功提交意图必须属于当前运行")
        key = (run.session_id, run.run_id)
        current = self._runs.get(key)
        if key in self._cancellations or key in self._cancellation_intents:
            raise ValueError("取消已保留提交权，不能提交成功")
        if current is not None and current.state.is_ended() and current.state != run.state:
            raise ValueError("不能覆盖其他终态")
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
