"""Runtime v4 的本地文件 RunRepository 实现。"""

from __future__ import annotations

from ..application.ports import RUN_SESSION_MISMATCH_REASON

import asyncio
import json
import os
import tempfile
from hashlib import sha256
from dataclasses import dataclass, replace
from pathlib import Path
from collections.abc import Callable

from ..application.ports import ApprovalRepository, ConversationProjectionPort, SuccessCommitFaultPort
from ..domain.cancellation import (CancellationRequest, CancellationOrigin, CancellationRecord, CancellationPhase, CancellationEffect, CancellationCommitIntent)
from ..application.dto import ConversationMessage
from ..domain.events import RunEvent, RunEventType
from ..domain.state import AgentRunState, Ended, RunOutcome
from ..domain.context import (
    ContextContributionKind,
    ContextOwner,
    TextSlotContent,
    ContextVersion,
    SuccessCommitFaultPoint,
    StagedHistoryCompression,
    StagedHistoryCompressionStatus,
    SuccessCommitIntent as RunSuccessCommitIntent,
    context_version_from_dict,
)
from ..domain.facts import (
    AgentPolicySnapshot,
    AgentRun,
    HistoryCompressionSnapshot,
    JSONMap,
    JSONValue,
    MessageRole,
    RunError,
    RunErrorCode,
    RunMessage,
    RunMessageKind,
    RunStatistics,
    ToolCall,
    get_integer,
    get_string,
    require_json_map,
    utc_now_iso,
)
from ._file_support import (
    RunStorageFileName,
    SessionStorageFileName,
    StorageFormatVersion,
    load_json_map,
    validate_path_segment,
    write_json_atomic,
)


@dataclass(frozen=True)
class SuccessCommitIntent:
    """文件型成功提交的可恢复意图；完成后必须删除，不能成为业务事实源。"""

    run: AgentRun
    completed_event: RunEvent
    success_intent: RunSuccessCommitIntent

    def to_dict(self) -> JSONMap:
        """转换为可原子保存的事务意图记录。"""
        return {
            "version": int(StorageFormatVersion.CONTEXT_VERSIONS),
            "run": self.run.to_dict(),
            "completed_event": self.completed_event.to_dict(),
            "success_intent": self.success_intent.to_dict(),
        }


class RunRepositoryAdapter:
    """将运行摘要、消息、事件和 Conversation 投影写入本地目录的仓储适配器。"""

    def __init__(
        self,
        root_directory: str | Path,
        conversation_projector: ConversationProjectionPort | None = None,
        success_commit_fault_port: SuccessCommitFaultPort | None = None,
        approval_repository: ApprovalRepository | None = None,
    ) -> None:
        """初始化仓储根目录和可选的既有 Session 投影器。"""
        self._approval_repository = approval_repository
        self._root_directory: Path = Path(root_directory).resolve()
        self._conversation_projector: ConversationProjectionPort | None = conversation_projector
        self._success_commit_fault_port: SuccessCommitFaultPort | None = success_commit_fault_port

    async def load_cancellation(self, session_id: str, run_id: str) -> CancellationRecord | None:
        """读取控制记录；不提前补偿未完成的终态提交。"""
        return await asyncio.to_thread(self._load_cancellation_sync, session_id, run_id)

    def _load_cancellation_sync(self, session_id: str, run_id: str) -> CancellationRecord | None:
        run = self._load_run_sync(session_id, run_id)
        path = self._run_path(session_id, run_id).with_name(RunStorageFileName.CANCELLATION.value)
        if run is None:
            if path.exists():
                raise ValueError("取消记录缺少所属运行")
            return None
        if run.session_id != session_id or run.run_id != run_id:
            raise ValueError("取消记录的运行归属不一致")
        if not path.exists():
            return None
        record = _cancellation_record_from_dict(load_json_map(path))
        if record.request.run_id != run_id:
            raise ValueError("取消记录归属不一致")
        return record

    async def save_cancellation(self, session_id: str, record: CancellationRecord, *, expected_revision: int) -> None:
        """调用者持有运行 gate；文件替换不提供跨进程比较交换。"""
        await asyncio.to_thread(self._save_cancellation_sync, session_id, record, expected_revision)

    def _save_cancellation_sync(self, session_id: str, record: CancellationRecord, expected_revision: int) -> None:
        run_id = record.request.run_id
        run = self._load_run_sync(session_id, run_id)
        if run is None:
            raise FileNotFoundError("取消记录的运行不存在")
        current = self._load_cancellation_sync(session_id, run_id)
        _validate_cancellation_revision(current, record, expected_revision)
        if current is None and (run.state.is_ended() or self._success_commit_path(session_id, run_id).exists() or run.success_commit_intent is not None):
            raise ValueError("已终态或成功提交中的运行不能接受取消")
        write_json_atomic(self._run_path(session_id, run_id).with_name(RunStorageFileName.CANCELLATION.value), record.to_dict())

    async def list_child_runs(self, parent_run_id: str) -> tuple[AgentRun, ...]:
        """读取直接父子关系，结果按运行标识排序。"""
        validate_path_segment(parent_run_id, "parent_run_id")
        runs = await asyncio.to_thread(self._all_runs_sync)
        return tuple(sorted((run for run in runs if run.parent_run_id == parent_run_id), key=lambda run: run.run_id))

    def _all_runs_sync(self) -> tuple[AgentRun, ...]:
        runs = []
        for path in sorted(self._root_directory.glob("*/agent_runs/*/run.json")):
            run = self._load_run_sync(path.parent.parent.parent.name, path.parent.name)
            if run is not None:
                if (run.session_id, run.run_id) != (path.parent.parent.parent.name, path.parent.name):
                    raise ValueError("运行目录与归属不一致")
                runs.append(run)
        return tuple(runs)

    async def has_pending_success_commit(self, session_id: str, run_id: str) -> bool:
        """仅查询提交权，不触发成功恢复。"""
        def read() -> bool:
            run = self._load_run_sync(session_id, run_id)
            return self._success_commit_path(session_id, run_id).exists() or (run is not None and run.success_commit_intent is not None)
        return await asyncio.to_thread(read)

    async def list_pending_cancellations(self) -> tuple[CancellationRecord, ...]:
        """保留 settled 审计记录，但不把它返回为恢复入口。"""
        records = []
        for run in await asyncio.to_thread(self._all_runs_sync):
            record = await self.load_cancellation(run.session_id, run.run_id)
            if record is not None and record.phase is not CancellationPhase.SETTLED:
                records.append(record)
        return tuple(records)

    async def has_pending_cancellation_commit(self, session_id: str, run_id: str) -> bool:
        """只查询目标意图，并校验路径和运行归属。"""
        return await asyncio.to_thread(self._load_cancellation_intent_sync, session_id, run_id) is not None

    def _load_cancellation_intent_sync(self, session_id: str, run_id: str) -> CancellationCommitIntent | None:
        path = self._run_path(session_id, run_id).with_name(RunStorageFileName.CANCEL_COMMIT.value)
        if not path.exists():
            return None
        intent = _cancellation_intent_from_dict(load_json_map(path))
        if (intent.run.session_id, intent.run.run_id) != (session_id, run_id):
            raise ValueError("取消意图目录归属不一致")
        run = self._load_run_sync(session_id, run_id)
        if run is None or (run.session_id, run.run_id) != (session_id, run_id):
            raise ValueError("取消意图缺少一致的所属运行")
        return intent

    async def recover_cancellation_commit(self, session_id: str, run_id: str) -> None:
        """调用方持目标 gate，只重放已有意图。"""
        intent = await asyncio.to_thread(self._load_cancellation_intent_sync, session_id, run_id)
        if intent is not None:
            await self._validate_cancellation_approvals(intent)
            await asyncio.to_thread(self._prepare_cancellation_commit_sync, intent)
            await self._recover_cancellation_commit(intent)

    async def commit_cancellation(self, intent: CancellationCommitIntent) -> None:
        """先保留取消决定，再补偿所有文件和审批副作用。"""
        await self._validate_cancellation_approvals(intent)
        await asyncio.to_thread(self._prepare_cancellation_commit_sync, intent)
        await self._recover_cancellation_commit(intent)

    async def _validate_cancellation_approvals(self, intent: CancellationCommitIntent) -> None:
        if intent.approval_ids and self._approval_repository is None:
            raise ValueError("取消意图包含审批，但未注入审批仓储")
        if self._approval_repository is not None:
            for approval_id in intent.approval_ids:
                record = await self._approval_repository.load(approval_id)
                if record is not None and (record.run_id != intent.run.run_id or record.session_id != intent.run.session_id):
                    raise ValueError("取消审批不属于当前运行")

    def _prepare_cancellation_commit_sync(self, intent: CancellationCommitIntent) -> None:
        run = self._load_run_sync(intent.run.session_id, intent.run.run_id)
        if run is None:
            raise FileNotFoundError("取消运行不存在")
        if self._success_commit_path(run.session_id, run.run_id).exists() or run.success_commit_intent is not None:
            raise ValueError("成功与取消提交意图冲突")
        current = self._load_cancellation_sync(run.session_id, run.run_id)
        _validate_cancellation_intent(run, current, intent)
        path = self._run_path(run.session_id, run.run_id).with_name(RunStorageFileName.CANCEL_COMMIT.value)
        existing = self._load_events_sync(run.session_id, run.run_id)
        _validate_cancellation_events(existing, intent)
        if path.exists():
            if load_json_map(path) != intent.to_dict():
                raise ValueError("已存在内容不同的取消意图")
            return
        # 校验完整事件前缀后才落盘，冲突不能被意图覆盖。
        write_json_atomic(path, intent.to_dict())

    async def recover_pending_cancellation_commits(self) -> None:
        """启动补偿；两种提交意图并存时拒绝选择任意一方。"""
        paths = await asyncio.to_thread(lambda: tuple(sorted(self._root_directory.glob("*/agent_runs/*/cancel_commit.json"))))
        for path in paths:
            intent = _cancellation_intent_from_dict(await asyncio.to_thread(load_json_map, path))
            if (intent.run.session_id, intent.run.run_id) != (path.parent.parent.parent.name, path.parent.name):
                raise ValueError("取消意图目录归属不一致")
            await self._validate_cancellation_approvals(intent)
            await asyncio.to_thread(self._prepare_cancellation_commit_sync, intent)
            await self._recover_cancellation_commit(intent)

    async def _recover_cancellation_commit(self, intent: CancellationCommitIntent) -> None:
        run = intent.run
        await asyncio.to_thread(self._ensure_cancellation_events_sync, intent)
        if self._approval_repository is not None:
            for approval_id in intent.approval_ids:
                await self._approval_repository.consume(approval_id)
        await asyncio.to_thread(self._delete_checkpoint_sync, run.session_id, run.run_id)
        await asyncio.to_thread(self._save_run_sync, run)
        await asyncio.to_thread(self._settle_cancellation_sync, intent)
        await asyncio.to_thread(self._delete_cancellation_intent_sync, run.session_id, run.run_id)

    def _ensure_cancellation_events_sync(self, intent: CancellationCommitIntent) -> None:
        """原子替换取消事件尾批，临时文件中断不能破坏既有完整事件。"""
        run = intent.run
        existing = self._load_events_sync(run.session_id, run.run_id)
        _validate_cancellation_events(existing, intent)
        missing_events = tuple(event for event in intent.events if event.sequence > len(existing))
        if not missing_events:
            return
        message_ids = frozenset(message.message_id for message in self._load_messages_sync(run.session_id, run.run_id))
        if any(message_id not in message_ids for event in missing_events for message_id in event.message_ids):
            raise ValueError("取消事件引用了尚未保存的消息")
        path = self._run_path(run.session_id, run.run_id).with_name(RunStorageFileName.EVENTS.value)
        prefix = path.read_bytes() if path.exists() else b""
        # 原始前缀逐字节保留；没有末尾换行的完整旧事件只补一个分隔符。
        separator = b"\n" if prefix and not prefix.endswith((b"\n", b"\r")) else b""
        tail = "".join(json.dumps(event.to_dict(), ensure_ascii=False) + "\n" for event in missing_events).encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(prefix=".events.cancel.", suffix=".tmp", dir=path.parent)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as temporary_file:
                temporary_file.write(prefix + separator + tail)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, path)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

    def _settle_cancellation_sync(self, intent: CancellationCommitIntent) -> None:
        current = self._load_cancellation_sync(intent.run.session_id, intent.run.run_id)
        if current == intent.settled_record:
            return
        _validate_cancellation_revision(current, intent.settled_record, intent.settled_record.revision - 1)
        write_json_atomic(self._run_path(intent.run.session_id, intent.run.run_id).with_name(RunStorageFileName.CANCELLATION.value), intent.settled_record.to_dict())

    def _delete_cancellation_intent_sync(self, session_id: str, run_id: str) -> None:
        self._run_path(session_id, run_id).with_name(RunStorageFileName.CANCEL_COMMIT.value).unlink(missing_ok=True)

    async def list_runs(self) -> tuple[AgentRun, ...]:
        """稳定枚举运行事实，供启动恢复和关闭使用。"""
        return tuple(sorted(await asyncio.to_thread(self._all_runs_sync), key=lambda run: (run.started_at, run.run_id)))

    async def create_run(self, run: AgentRun) -> None:
        """创建新的 run.json，禁止意外覆盖既有运行。"""
        await asyncio.to_thread(self._create_run_sync, run)

    async def load_run(self, session_id: str, run_id: str) -> AgentRun | None:
        """读取指定运行摘要；文件不存在时返回 None。"""
        await self._recover_success_commit(session_id, run_id)
        return await self._read_run_with_retry(self._load_run_sync, session_id, run_id)

    async def find_run(self, run_id: str) -> AgentRun | None:
        """跨 Session 定位运行摘要，供取消和审批恢复使用。"""
        return await self._read_run_with_retry(self._find_run_sync, run_id)

    async def load_run_snapshot(self, session_id: str, run_id: str) -> AgentRun | None:
        """纯读指定归属，拒绝把其他Session中的同一运行当作不存在。"""
        validate_path_segment(session_id, "session_id")
        run = await self.find_run(run_id)
        if run is not None and run.session_id != session_id:
            raise PermissionError(RUN_SESSION_MISMATCH_REASON)
        return run

    async def _read_run_with_retry(self, reader: Callable[..., AgentRun | None], *arguments: str) -> AgentRun | None:
        """JSON共享读取已在load_json_map统一有界重试，此层不叠加成25次。"""
        return await asyncio.to_thread(reader, *arguments)

    async def list_active_runs(self, session_id: str) -> tuple[AgentRun, ...]:
        """纯读未完整提交终态的占用，包括摘要Ended但意图仍在的运行。"""
        return await asyncio.to_thread(self._list_active_runs_sync, session_id)

    async def save_run(self, run: AgentRun) -> None:
        """原子更新已存在运行的摘要。"""
        await asyncio.to_thread(self._save_run_sync, run)

    async def save_messages(self, session_id: str, run_id: str, messages: tuple[RunMessage, ...]) -> None:
        """原子保存完整消息，并保证消息序号与标识唯一。"""
        await asyncio.to_thread(self._save_messages_sync, session_id, run_id, messages)

    async def append_context_version(
        self,
        session_id: str,
        run_id: str,
        context_version: ContextVersion,
    ) -> None:
        """追加不可变 Context Version，禁止覆盖或跳过版本编号。"""
        await asyncio.to_thread(self._append_context_version_sync, session_id, run_id, context_version)

    async def load_context_versions(self, session_id: str, run_id: str) -> tuple[ContextVersion, ...]:
        """读取按版本连续递增的上下文版本事实。"""
        return await asyncio.to_thread(self._load_context_versions_sync, session_id, run_id)

    async def set_active_context_version(self, session_id: str, run_id: str, version: int) -> None:
        """保存 Run 当前活动版本引用，引用必须已经存在。"""
        await asyncio.to_thread(self._set_active_context_version_sync, session_id, run_id, version)

    async def save_staged_history_compressions(
        self,
        session_id: str,
        run_id: str,
        candidates: tuple[StagedHistoryCompression, ...],
    ) -> None:
        """保存不含摘要正文的候选控制信息。"""
        await asyncio.to_thread(self._save_staged_history_compressions_sync, session_id, run_id, candidates)

    async def save_success_commit_intent(
        self,
        session_id: str,
        run_id: str,
        intent: RunSuccessCommitIntent,
    ) -> None:
        """保存 run.json 内的成功提交意图控制信息。"""
        await asyncio.to_thread(self._save_success_commit_intent_sync, session_id, run_id, intent)

    async def load_messages(self, session_id: str, run_id: str) -> tuple[RunMessage, ...]:
        """读取完整消息；尚未写入时返回空元组。"""
        return await asyncio.to_thread(self._load_messages_sync, session_id, run_id)

    async def append_event(self, session_id: str, event: RunEvent) -> None:
        """在消息已落盘后追加有序事件。"""
        await asyncio.to_thread(self._append_event_sync, session_id, event)

    async def load_events(self, session_id: str, run_id: str) -> tuple[RunEvent, ...]:
        """读取按 sequence 连续排列的运行事件。"""
        return await asyncio.to_thread(self._load_events_sync, session_id, run_id)

    def _load_events_sync(self, session_id: str, run_id: str) -> tuple[RunEvent, ...]:
        """严格读取 events.jsonl，独立校验 run_id 与连续 sequence。

        文件不存在或内容为空返回空元组；中间空白行或任何无法解析的行
        （含最后一行）视为损坏并抛出 ``ValueError``，错误信息包含文件名与行号。
        不保证与 run.json / messages.json / checkpoint.json 构成跨文件事务快照。
        """
        path: Path = self._run_path(session_id, run_id).with_name(RunStorageFileName.EVENTS.value)
        if not path.is_file():
            return ()
        raw_text: str = path.read_text(encoding="utf-8")
        # 兼容拆分：末尾换行不产生空串，但保留中间空白行；
        # 真正的空文件或仅含空白/换行的文件直接返回空元组。
        raw_lines: list[str] = raw_text.splitlines()
        if not raw_lines or all(line.strip() == "" for line in raw_lines):
            return ()
        events: list[RunEvent] = []
        expected_sequence: int = 1
        for line_index, raw_line in enumerate(raw_lines, start=1):
            stripped_line: str = raw_line.strip()
            if stripped_line == "":
                raise ValueError(f"events.jsonl 第 {line_index} 行解析失败：存在空白行（文件损坏）")
            try:
                decoded_value: JSONValue = json.loads(stripped_line)
            except json.JSONDecodeError as decode_error:
                raise ValueError(f"events.jsonl 第 {line_index} 行解析失败：{decode_error}")
            if not isinstance(decoded_value, dict):
                raise ValueError(f"events.jsonl 第 {line_index} 行解析失败：根节点必须是对象")
            event_data: JSONMap = decoded_value
            event: RunEvent = _run_event_from_dict(event_data)
            if event.run_id != run_id:
                raise ValueError(
                    f"events.jsonl 第 {line_index} 行解析失败：事件 run_id 必须为 {run_id}，实际为 {event.run_id}"
                )
            if event.sequence != expected_sequence:
                raise ValueError(
                    f"events.jsonl 第 {line_index} 行解析失败：事件序号必须为 {expected_sequence}，实际为 {event.sequence}"
                )
            events.append(event)
            expected_sequence += 1
        return tuple(events)

    async def commit_success(
        self,
        run: AgentRun,
        final_message: RunMessage,
        completed_event: RunEvent,
        success_intent: RunSuccessCommitIntent,
    ) -> None:
        """以领域成功意图驱动可恢复事务，禁止分散写入成功事实。"""
        await asyncio.to_thread(self._validate_success_and_get_input_sync, run, final_message)
        await asyncio.to_thread(self._validate_completed_event_sync, run, final_message, completed_event)
        if success_intent.run_id != run.run_id or success_intent.session_id != run.session_id:
            raise ValueError("成功提交意图必须属于当前运行")
        intent: SuccessCommitIntent = SuccessCommitIntent(run, completed_event, success_intent)
        await asyncio.to_thread(self._prepare_success_commit_sync, intent)
        await self.save_success_commit_intent(run.session_id, run.run_id, success_intent)
        await self._recover_success_commit(run.session_id, run.run_id)

    async def load_conversation(self, session_id: str) -> tuple[ConversationMessage, ...]:
        """读取该仓储维护的成功 Conversation 投影。"""
        await self._recover_session_success_commits(session_id)
        return await asyncio.to_thread(self._load_conversation_sync, session_id)

    async def recover_pending_success_commits(self) -> None:
        """扫描全部未决成功提交，并幂等补齐其 RunEvent、Conversation 与 AgentRun。"""
        pending_commit_locations: tuple[tuple[str, str], ...] = await asyncio.to_thread(
            self._find_pending_success_commit_locations_sync,
        )
        session_id: str
        run_id: str
        for session_id, run_id in pending_commit_locations:
            await self._recover_success_commit(session_id, run_id)

    def _create_run_sync(self, run: AgentRun) -> None:
        path: Path = self._run_path(run.session_id, run.run_id)
        if path.exists():
            raise FileExistsError(f"运行 {run.run_id} 已存在")
        write_json_atomic(path, _run_payload(run))

    def _load_run_sync(self, session_id: str, run_id: str) -> AgentRun | None:
        path: Path = self._run_path(session_id, run_id)
        if not path.is_file():
            return None
        payload: JSONMap = load_json_map(path)
        _require_v4_format(payload, "run.json")
        return _agent_run_from_dict(payload)

    def _find_run_sync(self, run_id: str) -> AgentRun | None:
        """扫描受控运行目录并定位唯一 run.json。"""
        safe_run_id: str = validate_path_segment(run_id, "run_id")
        run_paths: tuple[Path, ...] = tuple(self._root_directory.glob(
            f"*/agent_runs/{safe_run_id}/{RunStorageFileName.RUN.value}"
        ))
        if len(run_paths) > 1:
            raise ValueError(f"运行 {run_id} 在多个 Session 中重复出现")
        if not run_paths:
            return None
        payload: JSONMap = load_json_map(run_paths[0])
        _require_v4_format(payload, "run.json")
        return _agent_run_from_dict(payload)

    def _save_run_sync(self, run: AgentRun) -> None:
        path: Path = self._run_path(run.session_id, run.run_id)
        if not path.is_file():
            raise FileNotFoundError(f"运行 {run.run_id} 尚未创建")
        write_json_atomic(path, _run_payload(run))

    def _save_messages_sync(self, session_id: str, run_id: str, messages: tuple[RunMessage, ...]) -> None:
        self._validate_messages(messages)
        path: Path = self._run_path(session_id, run_id).with_name(RunStorageFileName.MESSAGES.value)
        context_versions: tuple[ContextVersion, ...] = self._load_context_versions_from_path_sync(path)
        self._write_messages_payload_sync(path, run_id, messages, context_versions)

    def _append_context_version_sync(
        self,
        session_id: str,
        run_id: str,
        context_version: ContextVersion,
    ) -> None:
        """追加连续版本并保留已保存的 Run Message 事实。"""
        path: Path = self._run_path(session_id, run_id).with_name(RunStorageFileName.MESSAGES.value)
        context_versions: tuple[ContextVersion, ...] = self._load_context_versions_from_path_sync(path)
        expected_version: int = len(context_versions) + 1
        if context_version.version != expected_version:
            raise ValueError(f"Context Version 必须连续递增；期望 {expected_version}")
        messages: tuple[RunMessage, ...] = self._load_messages_from_path_sync(path)
        self._write_messages_payload_sync(path, run_id, messages, context_versions + (context_version,))

    def _load_context_versions_sync(self, session_id: str, run_id: str) -> tuple[ContextVersion, ...]:
        """读取 messages.json 中有序且不可变的上下文版本。"""
        path: Path = self._run_path(session_id, run_id).with_name(RunStorageFileName.MESSAGES.value)
        return self._load_context_versions_from_path_sync(path)

    def _set_active_context_version_sync(self, session_id: str, run_id: str, version: int) -> None:
        """校验版本存在后原子更新 run.json 中的活动引用。"""
        if version not in {item.version for item in self._load_context_versions_sync(session_id, run_id)}:
            raise ValueError("活动 Context Version 必须引用已保存版本")
        run: AgentRun | None = self._load_run_sync(session_id, run_id)
        if run is None:
            raise FileNotFoundError(f"运行 {run_id} 尚未创建")
        self._save_run_sync(replace(run, active_context_version=version))

    def _save_staged_history_compressions_sync(
        self,
        session_id: str,
        run_id: str,
        candidates: tuple[StagedHistoryCompression, ...],
    ) -> None:
        """保存候选前校验正文不进入 Run 控制面。"""
        if len({candidate.candidate_id for candidate in candidates}) != len(candidates):
            raise ValueError("历史压缩候选标识必须唯一")
        run: AgentRun | None = self._load_run_sync(session_id, run_id)
        if run is None:
            raise FileNotFoundError(f"运行 {run_id} 尚未创建")
        self._save_run_sync(replace(run, staged_history_compressions=candidates))

    def _save_success_commit_intent_sync(
        self,
        session_id: str,
        run_id: str,
        intent: RunSuccessCommitIntent,
    ) -> None:
        """保存成功意图前校验候选引用属于当前 Run。"""
        run: AgentRun | None = self._load_run_sync(session_id, run_id)
        if run is None:
            raise FileNotFoundError(f"运行 {run_id} 尚未创建")
        if self._load_cancellation_sync(session_id, run_id) is not None or self._run_path(session_id, run_id).with_name(RunStorageFileName.CANCEL_COMMIT.value).exists():
            raise ValueError("取消已保留提交权，不能保存成功意图")
        candidate_ids: frozenset[str] = frozenset(
            candidate.candidate_id for candidate in run.staged_history_compressions
        )
        if intent.latest_candidate_id is not None and intent.latest_candidate_id not in candidate_ids:
            raise ValueError("成功提交意图引用了未知历史压缩候选")
        self._save_run_sync(replace(run, success_commit_intent=intent))

    async def _recover_session_success_commits(self, session_id: str) -> None:
        """补偿指定 Session 下的所有未决成功提交，避免读取到互相矛盾的事实。"""
        pending_run_ids: tuple[str, ...] = await asyncio.to_thread(
            self._find_session_pending_success_commit_run_ids_sync,
            session_id,
        )
        run_id: str
        for run_id in pending_run_ids:
            await self._recover_success_commit(session_id, run_id)

    async def _recover_success_commit(self, session_id: str, run_id: str) -> None:
        """按 Session、事件、Run、checkpoint 顺序幂等补齐成功提交。"""
        if self._success_commit_path(session_id, run_id).exists() and self._run_path(session_id, run_id).with_name(RunStorageFileName.CANCEL_COMMIT.value).exists():
            raise ValueError("成功与取消提交意图同时存在")
        intent: SuccessCommitIntent | None = await asyncio.to_thread(
            self._load_success_commit_intent_sync,
            session_id,
            run_id,
        )
        if intent is None:
            return
        if self._run_path(session_id, run_id).with_name(RunStorageFileName.CANCEL_COMMIT.value).exists():
            raise ValueError("成功与取消提交意图同时存在")
        final_message: RunMessage = await asyncio.to_thread(self._load_final_message_sync, intent.run)
        user_message: RunMessage = await asyncio.to_thread(
            self._validate_success_and_get_input_sync,
            intent.run,
            final_message,
        )
        history_compression, source_hash = await asyncio.to_thread(
            self._history_compression_for_intent_sync,
            intent.run,
            intent.success_intent,
        )
        await self._inject_success_commit_fault(SuccessCommitFaultPoint.BEFORE_SESSION_PROJECTION)
        if self._conversation_projector is not None:
            await self._conversation_projector.project_success(
                intent.run,
                user_message,
                final_message,
                history_compression,
                source_hash,
            )
        else:
            await asyncio.to_thread(self._append_standalone_conversation_sync, intent.run, final_message)
        await self._inject_success_commit_fault(SuccessCommitFaultPoint.AFTER_SESSION_PROJECTION)
        await self._inject_success_commit_fault(SuccessCommitFaultPoint.BEFORE_COMPLETED_EVENT)
        await asyncio.to_thread(self._ensure_event_sync, intent.run.session_id, intent.completed_event)
        await self._inject_success_commit_fault(SuccessCommitFaultPoint.AFTER_COMPLETED_EVENT)
        await self._inject_success_commit_fault(SuccessCommitFaultPoint.BEFORE_RUN_FINALIZATION)
        completed_run: AgentRun = _completed_run_from_intent(intent.run, intent.success_intent)
        await asyncio.to_thread(self._save_run_sync, completed_run)
        await self._inject_success_commit_fault(SuccessCommitFaultPoint.AFTER_RUN_FINALIZATION)
        await asyncio.to_thread(self._delete_checkpoint_sync, intent.run.session_id, intent.run.run_id)
        await asyncio.to_thread(self._delete_success_commit_intent_sync, session_id, run_id)

    async def _inject_success_commit_fault(self, point: SuccessCommitFaultPoint) -> None:
        """仅在测试装配故障 Port 时于精确提交边界中断。"""
        if self._success_commit_fault_port is not None:
            await self._success_commit_fault_port.inject(point)

    def _history_compression_for_intent_sync(
        self,
        run: AgentRun,
        intent: RunSuccessCommitIntent,
    ) -> tuple[HistoryCompressionSnapshot | None, str]:
        """从候选引用的 Context Version 读取唯一保存的摘要正文。"""
        if intent.latest_candidate_id is None:
            return None, ""
        candidate: StagedHistoryCompression | None = next(
            (item for item in run.staged_history_compressions if item.candidate_id == intent.latest_candidate_id),
            None,
        )
        if candidate is None:
            raise ValueError("成功提交意图引用的历史压缩候选不存在")
        versions: tuple[ContextVersion, ...] = self._load_context_versions_sync(run.session_id, run.run_id)
        version: ContextVersion | None = next(
            (item for item in versions if item.version == candidate.context_version),
            None,
        )
        if version is None:
            raise ValueError("历史压缩候选引用的 Context Version 不存在")
        history_slot = next((slot for slot in version.slots if slot.contribution_kind is ContextContributionKind.HISTORY_COMPRESSIONS), None)
        if history_slot is None or not isinstance(history_slot.content, TextSlotContent):
            raise ValueError("历史压缩候选引用的 Context Version 缺少摘要 Slot")
        raw_content: str = history_slot.content.text
        if _hash_text(raw_content) != candidate.summary_hash:
            raise ValueError("历史压缩候选与 Context Version 摘要 hash 不一致")
        return HistoryCompressionSnapshot(candidate.context_version, candidate.covered_through_conversation_id, raw_content, candidate.summary_hash), candidate.source_hash

    def _delete_checkpoint_sync(self, session_id: str, run_id: str) -> None:
        """在终态 Run 已落盘后清理恢复 checkpoint。"""
        path: Path = self._run_path(session_id, run_id).with_name(RunStorageFileName.CHECKPOINT.value)
        if path.is_file():
            path.unlink()

    def _prepare_success_commit_sync(self, intent: SuccessCommitIntent) -> None:
        """原子创建或校验既有成功提交意图，保证重试不会覆盖另一笔提交。"""
        run_path = self._run_path(intent.run.session_id, intent.run.run_id)
        current = self._load_run_sync(intent.run.session_id, intent.run.run_id)
        if run_path.with_name(RunStorageFileName.CANCEL_COMMIT.value).exists() or self._load_cancellation_sync(intent.run.session_id, intent.run.run_id) is not None:
            raise ValueError("取消已保留提交权，不能提交成功")
        if current is not None and current.state.is_ended() and current.state != intent.run.state:
            raise ValueError("不能覆盖其他终态")
        path: Path = self._success_commit_path(intent.run.session_id, intent.run.run_id)
        if path.is_file():
            existing_intent: SuccessCommitIntent = _success_commit_intent_from_dict(load_json_map(path))
            if existing_intent != intent:
                raise ValueError("运行已存在内容不同的未决成功提交")
            return
        write_json_atomic(path, intent.to_dict())

    def _load_success_commit_intent_sync(
        self,
        session_id: str,
        run_id: str,
    ) -> SuccessCommitIntent | None:
        """读取指定 Run 的未决成功提交意图；不存在时表示无需补偿。"""
        path: Path = self._success_commit_path(session_id, run_id)
        if not path.is_file():
            return None
        return _success_commit_intent_from_dict(load_json_map(path))

    def _delete_success_commit_intent_sync(self, session_id: str, run_id: str) -> None:
        """删除全部事实已落盘的事务意图；删除失败时下次恢复会安全重试。"""
        path: Path = self._success_commit_path(session_id, run_id)
        if path.is_file():
            path.unlink()

    def _find_pending_success_commit_locations_sync(self) -> tuple[tuple[str, str], ...]:
        """扫描受控运行目录中的全部事务意图位置。"""
        intent_paths: tuple[Path, ...] = tuple(self._root_directory.glob(
            f"*/agent_runs/*/{RunStorageFileName.SUCCESS_COMMIT.value}",
        ))
        locations: list[tuple[str, str]] = []
        intent_path: Path
        for intent_path in intent_paths:
            run_id: str = intent_path.parent.name
            session_id: str = intent_path.parent.parent.parent.name
            locations.append((session_id, run_id))
        return tuple(locations)

    def _find_session_pending_success_commit_run_ids_sync(self, session_id: str) -> tuple[str, ...]:
        """定位指定 Session 内全部未决提交，供 Conversation 读取前补偿。"""
        safe_session_id: str = validate_path_segment(session_id, "session_id")
        intent_paths: tuple[Path, ...] = tuple((
            self._root_directory / safe_session_id / "agent_runs"
        ).glob(f"*/{RunStorageFileName.SUCCESS_COMMIT.value}"))
        return tuple(intent_path.parent.name for intent_path in intent_paths)

    def _load_final_message_sync(self, run: AgentRun) -> RunMessage:
        """从 RunMessage 唯一事实源读取成功提交引用的最终 assistant 消息。"""
        if run.final_message_id is None:
            raise ValueError("成功提交意图缺少最终消息标识")
        messages: tuple[RunMessage, ...] = self._load_messages_sync(run.session_id, run.run_id)
        final_message: RunMessage | None = next(
            (message for message in messages if message.message_id == run.final_message_id),
            None,
        )
        if final_message is None:
            raise ValueError("成功提交意图引用的最终消息不存在")
        return final_message

    def _load_messages_sync(self, session_id: str, run_id: str) -> tuple[RunMessage, ...]:
        path: Path = self._run_path(session_id, run_id).with_name(RunStorageFileName.MESSAGES.value)
        return self._load_messages_from_path_sync(path)

    def _load_messages_from_path_sync(self, path: Path) -> tuple[RunMessage, ...]:
        """读取 v4 的增量 RunMessage 数组。"""
        if not path.is_file():
            return ()
        payload: JSONMap = load_json_map(path)
        _require_v4_format(payload, "messages.json")
        raw_messages: JSONValue | None = payload.get("messages")
        if not isinstance(raw_messages, list):
            raise ValueError("messages.json 的 messages 字段必须是数组")
        messages: list[RunMessage] = []
        raw_message: JSONValue
        for raw_message in raw_messages:
            messages.append(_run_message_from_dict(require_json_map(raw_message)))
        result: tuple[RunMessage, ...] = tuple(messages)
        self._validate_messages(result)
        return result

    def _list_active_runs_sync(self, session_id: str) -> tuple[AgentRun, ...]:
        """领域Ended与提交占用分开判断，不在查询中执行补偿。"""
        safe_session_id: str = validate_path_segment(session_id, "session_id")
        directory: Path = self._root_directory / safe_session_id / "agent_runs"
        if not directory.is_dir():
            return ()
        runs: list[AgentRun] = []
        path: Path
        for path in directory.glob(f"*/{RunStorageFileName.RUN.value}"):
            run: AgentRun = _agent_run_from_dict(load_json_map(path))
            if (run.session_id, run.run_id) != (safe_session_id, path.parent.name):
                raise ValueError("运行目录与归属不一致")
            pending = (run.success_commit_intent is not None or self._success_commit_path(run.session_id, run.run_id).exists()
                       or path.with_name(RunStorageFileName.CANCEL_COMMIT.value).exists())
            if not run.state.is_ended() or pending:
                runs.append(run)
        return tuple(runs)

    def _load_context_versions_from_path_sync(self, path: Path) -> tuple[ContextVersion, ...]:
        """读取并校验 v4 的全部 Context Version。"""
        if not path.is_file():
            return ()
        payload: JSONMap = load_json_map(path)
        _require_v4_format(payload, "messages.json")
        raw_context_versions: JSONValue | None = payload.get("context_versions")
        if not isinstance(raw_context_versions, list):
            raise ValueError("messages.json 的 context_versions 字段必须是数组")
        versions: list[ContextVersion] = []
        raw_context_version: JSONValue
        for raw_context_version in raw_context_versions:
            versions.append(context_version_from_dict(require_json_map(raw_context_version)))
        expected_versions: tuple[int, ...] = tuple(range(1, len(versions) + 1))
        actual_versions: tuple[int, ...] = tuple(item.version for item in versions)
        if actual_versions != expected_versions:
            raise ValueError("Context Version 必须从 1 连续递增")
        return tuple(versions)

    def _write_messages_payload_sync(
        self,
        path: Path,
        run_id: str,
        messages: tuple[RunMessage, ...],
        context_versions: tuple[ContextVersion, ...],
    ) -> None:
        """以 v4 格式原子写入上下文版本和增量消息。"""
        payload: JSONMap = {
            "run_id": run_id,
            "version": int(StorageFormatVersion.CONTEXT_VERSIONS),
            "context_versions": [context_version.to_dict() for context_version in context_versions],
            "messages": [message.to_dict() for message in messages],
        }
        write_json_atomic(path, payload)

    def _append_event_sync(self, session_id: str, event: RunEvent) -> None:
        message_ids: frozenset[str] = frozenset(
            message.message_id for message in self._load_messages_sync(session_id, event.run_id)
        )
        missing_ids: tuple[str, ...] = tuple(
            message_id for message_id in event.message_ids if message_id not in message_ids
        )
        if missing_ids:
            raise ValueError(f"事件引用了尚未保存的消息：{', '.join(missing_ids)}")

        path: Path = self._run_path(session_id, event.run_id).with_name(RunStorageFileName.EVENTS.value)
        expected_sequence: int = self._next_event_sequence(path)
        if event.sequence != expected_sequence:
            raise ValueError(f"事件序号必须为 {expected_sequence}，实际为 {event.sequence}")
        path.parent.mkdir(parents=True, exist_ok=True)
        serialized_event: str = json.dumps(event.to_dict(), ensure_ascii=False)
        with path.open("a", encoding="utf-8", newline="\n") as event_file:
            event_file.write(f"{serialized_event}\n")
            event_file.flush()
            os.fsync(event_file.fileno())

    def _validate_success_and_get_input_sync(self, run: AgentRun, final_message: RunMessage) -> RunMessage:
        """校验成功提交条件，并定位已持久化的用户输入消息。"""
        if not (isinstance(run.state.mode, Ended) and run.state.mode.outcome is RunOutcome.COMPLETED):
            raise ValueError("只有已完成的运行才能提交 Conversation 投影")
        if final_message.role is not MessageRole.ASSISTANT:
            raise ValueError("最终 Conversation 投影必须是 assistant 消息")
        if final_message.message_id != run.final_message_id:
            raise ValueError("最终消息必须与 AgentRun 摘要引用一致")
        messages: tuple[RunMessage, ...] = self._load_messages_sync(run.session_id, run.run_id)
        input_message: RunMessage | None = next(
            (message for message in messages if message.message_id == run.input_message_id),
            None,
        )
        if input_message is None:
            raise ValueError("成功运行必须引用已保存的用户输入消息")
        if input_message.kind is not RunMessageKind.USER_INPUT or input_message.role is not MessageRole.USER:
            raise ValueError("运行输入消息必须是 user_input 类型的用户消息")
        stored_final_message: RunMessage | None = next(
            (message for message in messages if message.message_id == final_message.message_id),
            None,
        )
        if stored_final_message != final_message:
            raise ValueError("最终消息必须先以完整 RunMessage 保存后才能提交成功投影")
        return input_message

    def _validate_completed_event_sync(
        self,
        run: AgentRun,
        final_message: RunMessage,
        completed_event: RunEvent,
    ) -> None:
        """校验成功提交携带唯一且引用最终消息的完成事件。"""
        if completed_event.run_id != run.run_id:
            raise ValueError("完成事件必须属于当前运行")
        if completed_event.event_type is not RunEventType.RUN_COMPLETED:
            raise ValueError("成功提交必须携带 RUN_COMPLETED 事件")
        if completed_event.message_ids != (final_message.message_id,):
            raise ValueError("完成事件必须且只能引用最终 assistant 消息")

    def _ensure_event_sync(self, session_id: str, event: RunEvent) -> None:
        """幂等写入成功终态事件，使提交失败后的重试可继续完成。"""
        path: Path = self._run_path(session_id, event.run_id).with_name(RunStorageFileName.EVENTS.value)
        if path.is_file():
            serialized_event: str = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True)
            existing_line: str
            for existing_line in path.read_text(encoding="utf-8").splitlines():
                decoded_value: JSONValue = json.loads(existing_line)
                existing_event: JSONMap = require_json_map(decoded_value)
                if get_integer(existing_event, "sequence") != event.sequence:
                    continue
                existing_serialized_event: str = json.dumps(existing_event, ensure_ascii=False, sort_keys=True)
                if existing_serialized_event != serialized_event:
                    raise ValueError("完成事件序号已被其他事件占用")
                return
        self._append_event_sync(session_id, event)

    def _append_standalone_conversation_sync(self, run: AgentRun, final_message: RunMessage) -> None:
        """在未注入 Session 投影器时保留独立 Conversation 兼容容器。"""
        existing_messages: tuple[ConversationMessage, ...] = self._load_conversation_sync(run.session_id)
        if any(message.message_id == final_message.message_id for message in existing_messages):
            return
        conversation_message: ConversationMessage = ConversationMessage(
            message_id=final_message.message_id,
            role=MessageRole.ASSISTANT,
            content=final_message.content,
            created_at=run.ended_at or utc_now_iso(),
        )
        self._append_conversation_message_sync(run.session_id, conversation_message)

    def _append_conversation_message_sync(self, session_id: str, message: ConversationMessage) -> None:
        path: Path = self._conversation_path(session_id)
        existing_messages: list[JSONMap] = []
        if path.is_file():
            existing_payload: JSONMap = load_json_map(path)
            raw_messages: JSONValue | None = existing_payload.get("messages")
            if isinstance(raw_messages, list):
                raw_message: JSONValue
                for raw_message in raw_messages:
                    existing_messages.append(require_json_map(raw_message))
        existing_messages.append(message.to_dict())
        payload: JSONMap = {
            "session_id": session_id,
            "version": int(StorageFormatVersion.CONTEXT_VERSIONS),
            "messages": existing_messages,
        }
        write_json_atomic(path, payload)

    def _load_conversation_sync(self, session_id: str) -> tuple[ConversationMessage, ...]:
        path: Path = self._conversation_path(session_id)
        if not path.is_file():
            return ()
        payload: JSONMap = load_json_map(path)
        raw_messages: JSONValue | None = payload.get("messages")
        if not isinstance(raw_messages, list):
            raise ValueError("conversation.json 的 messages 字段必须是数组")
        messages: list[ConversationMessage] = []
        raw_message: JSONValue
        for raw_message in raw_messages:
            messages.append(_conversation_message_from_dict(require_json_map(raw_message)))
        return tuple(messages)

    def _next_event_sequence(self, path: Path) -> int:
        if not path.is_file():
            return 1
        lines: list[str] = path.read_text(encoding="utf-8").splitlines()
        if not lines:
            return 1
        last_line: str = lines[-1]
        decoded_value: JSONValue = json.loads(last_line)
        last_event: JSONMap = require_json_map(decoded_value)
        return get_integer(last_event, "sequence") + 1

    def _run_path(self, session_id: str, run_id: str) -> Path:
        safe_session_id: str = validate_path_segment(session_id, "session_id")
        safe_run_id: str = validate_path_segment(run_id, "run_id")
        return self._root_directory / safe_session_id / "agent_runs" / safe_run_id / RunStorageFileName.RUN.value

    def _conversation_path(self, session_id: str) -> Path:
        safe_session_id: str = validate_path_segment(session_id, "session_id")
        return self._root_directory / safe_session_id / SessionStorageFileName.CONVERSATION.value

    def _success_commit_path(self, session_id: str, run_id: str) -> Path:
        """返回单个 Run 的临时成功提交意图文件路径。"""
        return self._run_path(session_id, run_id).with_name(RunStorageFileName.SUCCESS_COMMIT.value)

    def _validate_messages(self, messages: tuple[RunMessage, ...]) -> None:
        message_ids: set[str] = set()
        expected_sequence: int = 1
        message: RunMessage
        for message in messages:
            if message.sequence != expected_sequence:
                raise ValueError("运行消息序号必须从 1 连续递增")
            if message.message_id in message_ids:
                raise ValueError("运行消息标识必须唯一")
            message_ids.add(message.message_id)
            expected_sequence += 1


def _agent_run_from_dict(data: JSONMap) -> AgentRun:
    """将 run.json 反序列化为领域摘要。"""
    raw_policy: JSONValue | None = data.get("policy")
    raw_statistics: JSONValue | None = data.get("statistics")
    policy_data: JSONMap = require_json_map(raw_policy) if raw_policy is not None else {}
    statistics_data: JSONMap = require_json_map(raw_statistics) if raw_statistics is not None else {}
    raw_error: JSONValue | None = data.get("error")
    raw_candidates: JSONValue | None = data.get("staged_history_compressions")
    raw_success_intent: JSONValue | None = data.get("success_commit_intent")
    error: RunError | None = _run_error_from_dict(require_json_map(raw_error)) if raw_error is not None else None
    # 只接受新格式：缺失 state 即视为非法持久化数据，不再静默兜底为 Created。
    if "state" not in data:
        raise ValueError("AgentRun 持久化数据缺少 state 字段（只接受 v4 状态格式）")
    return AgentRun(
        run_id=get_string(data, "run_id"),
        session_id=get_string(data, "session_id"),
        agent_id=get_string(data, "agent_id"),
        state=AgentRunState.from_dict(require_json_map(data["state"])),
        started_at=get_string(data, "started_at"),
        policy=AgentPolicySnapshot(
            agent_id=get_string(policy_data, "agent_id"),
            identity_version=get_string(policy_data, "identity_version"),
            model_id=get_string(policy_data, "model_id"),
            max_iterations=get_integer(policy_data, "max_iterations"),
            policy_data=_json_map_or_empty(policy_data.get("policy_data")),
        ),
        input_message_id=get_string(data, "input_message_id"),
        parent_run_id=_optional_string(data.get("parent_run_id")),
        root_run_id=_optional_string(data.get("root_run_id")),
        delegation_task_id=_optional_string(data.get("delegation_task_id")),
        ended_at=_optional_string(data.get("ended_at")),
        resume_count=get_integer(data, "resume_count"),
        final_message_id=_optional_string(data.get("final_message_id")),
        latest_checkpoint_id=_optional_string(data.get("latest_checkpoint_id")),
        active_context_version=_optional_positive_integer(data.get("active_context_version")),
        staged_history_compressions=_staged_history_compressions_from_value(raw_candidates),
        success_commit_intent=(
            None if raw_success_intent is None else _run_success_commit_intent_from_dict(
                require_json_map(raw_success_intent),
            )
        ),
        statistics=RunStatistics(
            duration_ms=get_integer(statistics_data, "duration_ms"),
            llm_call_count=get_integer(statistics_data, "llm_call_count"),
            tool_call_count=get_integer(statistics_data, "tool_call_count"),
            tokens_in=get_integer(statistics_data, "tokens_in"),
            tokens_out=get_integer(statistics_data, "tokens_out"),
        ),
        error=error,
    )


def _success_commit_intent_from_dict(data: JSONMap) -> SuccessCommitIntent:
    """将临时成功提交意图反序列化为严格领域模型。"""
    raw_run: JSONValue | None = data.get("run")
    raw_completed_event: JSONValue | None = data.get("completed_event")
    raw_success_intent: JSONValue | None = data.get("success_intent")
    if raw_run is None or raw_completed_event is None or raw_success_intent is None:
        raise ValueError("成功提交意图缺少 run、completed_event 或领域意图")
    return SuccessCommitIntent(
        run=_agent_run_from_dict(require_json_map(raw_run)),
        completed_event=_run_event_from_dict(require_json_map(raw_completed_event)),
        success_intent=_run_success_commit_intent_from_dict(require_json_map(raw_success_intent)),
    )


def _run_event_from_dict(data: JSONMap) -> RunEvent:
    """将 events.jsonl 或事务意图中的审计事件严格反序列化为领域事件。

    必填字段缺失或非法、或可选字段已出现但类型错误时一律抛出 ``ValueError``，
    不在本函数中静默降级或过滤，损坏记录必须显式失败。
    """
    raw_run_id: JSONValue | None = data.get("run_id")
    if not isinstance(raw_run_id, str) or raw_run_id == "":
        raise ValueError("事件 run_id 必须是非空字符串")

    raw_sequence: JSONValue | None = data.get("sequence")
    if not isinstance(raw_sequence, int) or isinstance(raw_sequence, bool) or raw_sequence <= 0:
        raise ValueError("事件 sequence 必须是大于 0 的整数（布尔值不视为整数）")

    raw_event_type: JSONValue | None = data.get("event_type")
    if not isinstance(raw_event_type, str):
        raise ValueError("事件 event_type 必须是字符串")
    try:
        event_type: RunEventType = RunEventType(raw_event_type)
    except ValueError:
        raise ValueError(f"事件 event_type 非法：{raw_event_type}")

    raw_occurred_at: JSONValue | None = data.get("occurred_at")
    if not isinstance(raw_occurred_at, str) or raw_occurred_at == "":
        raise ValueError("事件 occurred_at 必须是非空字符串")

    raw_message_ids: JSONValue | None = data.get("message_ids")
    if raw_message_ids is None:
        message_ids: tuple[str, ...] = ()
    elif not isinstance(raw_message_ids, list):
        raise ValueError("事件 message_ids 必须是字符串数组")
    else:
        for element in raw_message_ids:
            if not isinstance(element, str):
                raise ValueError("事件 message_ids 的元素必须是字符串")
        message_ids = tuple(raw_message_ids)

    raw_summary: JSONValue | None = data.get("summary")
    if raw_summary is None:
        summary: str = ""
    elif not isinstance(raw_summary, str):
        raise ValueError("事件 summary 必须是字符串")
    else:
        summary = raw_summary

    raw_event_data: JSONValue | None = data.get("data")
    if raw_event_data is None:
        event_data: JSONMap = {}
    elif not isinstance(raw_event_data, dict):
        raise ValueError("事件 data 必须是 JSON 对象")
    else:
        event_data = raw_event_data

    if event_type is RunEventType.TOOL_RECOVERY_BLOCKED:
        call_id = event_data.get("call_id")
        reason = event_data.get("reason")
        attempt = event_data.get("attempt")
        if not isinstance(call_id, str) or not call_id or not isinstance(reason, str) or not reason:
            raise ValueError("TOOL_RECOVERY_BLOCKED 必须包含 call_id 和 reason")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 0:
            raise ValueError("TOOL_RECOVERY_BLOCKED attempt 必须是非负整数")

    return RunEvent(
        run_id=raw_run_id,
        sequence=raw_sequence,
        event_type=event_type,
        occurred_at=raw_occurred_at,
        message_ids=message_ids,
        summary=summary,
        data=event_data,
    )


def _run_message_from_dict(data: JSONMap) -> RunMessage:
    """将 messages.json 的单条记录反序列化为领域消息。"""
    raw_tool_calls: JSONValue | None = data.get("tool_calls")
    tool_calls: list[ToolCall] = []
    if isinstance(raw_tool_calls, list):
        raw_tool_call: JSONValue
        for raw_tool_call in raw_tool_calls:
            tool_call_data: JSONMap = require_json_map(raw_tool_call)
            tool_calls.append(ToolCall(
                call_id=get_string(tool_call_data, "call_id"),
                name=get_string(tool_call_data, "name"),
                arguments=_json_map_or_empty(tool_call_data.get("arguments")),
            ))
    return RunMessage(
        message_id=get_string(data, "id"),
        sequence=get_integer(data, "sequence"),
        kind=RunMessageKind(get_string(data, "kind")),
        role=MessageRole(get_string(data, "role")),
        content=get_string(data, "content"),
        tool_call_id=_optional_string(data.get("tool_call_id")),
        name=_optional_string(data.get("name")),
        tool_calls=tuple(tool_calls),
        metadata=_json_map_or_empty(data.get("metadata")),
    )


def _require_v4_format(payload: JSONMap, file_name: str) -> None:
    """拒绝历史容器格式，禁止以隐式转换掩盖数据版本差异。"""
    version: int = get_integer(payload, "version")
    if version != int(StorageFormatVersion.CONTEXT_VERSIONS):
        raise ValueError(f"不支持的 {file_name} 格式版本：{version}；仅支持 v4")


def _run_payload(run: AgentRun) -> JSONMap:
    """生成唯一支持的 v4 run.json 载荷。"""
    payload: JSONMap = run.to_dict()
    payload["version"] = int(StorageFormatVersion.CONTEXT_VERSIONS)
    return payload


def _optional_positive_integer(value: JSONValue | None) -> int | None:
    """读取可选正整数。"""
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("active_context_version 必须是正整数或 null")
    return value


def _hash_text(content: str) -> str:
    """计算摘要正文的稳定 SHA-256。"""
    return sha256(content.encode("utf-8")).hexdigest()


def _staged_history_compressions_from_value(value: JSONValue | None) -> tuple[StagedHistoryCompression, ...]:
    """从 run.json 读取不含摘要正文的候选控制信息。"""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("staged_history_compressions 必须是数组")
    candidates: list[StagedHistoryCompression] = []
    raw_candidate: JSONValue
    for raw_candidate in value:
        candidate: JSONMap = require_json_map(raw_candidate)
        if "summary" in candidate or "content" in candidate:
            raise ValueError("staged_history_compressions 不得保存摘要正文")
        candidates.append(StagedHistoryCompression(
            candidate_id=get_string(candidate, "candidate_id"),
            status=StagedHistoryCompressionStatus(get_string(candidate, "status")),
            session_baseline_version=get_integer(candidate, "session_baseline_version"),
            covered_through_conversation_id=get_string(candidate, "covered_through_conversation_id"),
            source_hash=get_string(candidate, "source_hash"),
            summary_hash=get_string(candidate, "summary_hash"),
            context_version=get_integer(candidate, "context_version"),
        ))
    return tuple(candidates)


def _run_success_commit_intent_from_dict(data: JSONMap) -> RunSuccessCommitIntent:
    """从 run.json 控制字段恢复成功提交意图。"""
    raw_candidate_id: JSONValue | None = data.get("latest_candidate_id")
    latest_candidate_id: str | None = raw_candidate_id if isinstance(raw_candidate_id, str) else None
    return RunSuccessCommitIntent(
        conversation_id=get_string(data, "conversation_id"),
        latest_candidate_id=latest_candidate_id,
        target_outcome=RunOutcome(get_string(data, "target_outcome", RunOutcome.COMPLETED.value)),
        run_id=get_string(data, "run_id"),
        session_id=get_string(data, "session_id"),
    )


def _completed_run_from_intent(run: AgentRun, intent: RunSuccessCommitIntent) -> AgentRun:
    """以成功意图收敛终态 Run，并只提交最新的 staged 候选。

    终态控制状态直接沿用提交时携带的 ``run.state``（已是 ``Ended(target_outcome)``，
    且保留 ``iteration``/``retry_count`` 等循环计数），不在恢复时重建为
    ``iteration=0`` 的空白终态，避免故障恢复后丢失循环安全计数。
    """
    candidates: tuple[StagedHistoryCompression, ...] = tuple(
        replace(
            candidate,
            status=(
                StagedHistoryCompressionStatus.COMMITTED
                if candidate.candidate_id == intent.latest_candidate_id
                else candidate.status
            ),
        )
        for candidate in run.staged_history_compressions
    )
    return replace(
        run,
        state=run.state,
        staged_history_compressions=candidates,
        success_commit_intent=None,
    )


def _conversation_message_from_dict(data: JSONMap) -> ConversationMessage:
    """将 conversation.json 的单条记录反序列化为对话消息。"""
    return ConversationMessage(
        message_id=get_string(data, "id"),
        role=MessageRole(get_string(data, "role")),
        content=get_string(data, "content"),
        created_at=get_string(data, "created_at"),
    )


def _run_error_from_dict(data: JSONMap) -> RunError:
    """将 JSON 错误摘要反序列化为领域错误。"""
    raw_retryable: JSONValue | None = data.get("retryable")
    retryable: bool = raw_retryable if isinstance(raw_retryable, bool) else False
    return RunError(RunErrorCode(get_string(data, "code")), get_string(data, "message"), retryable)


def _json_map_or_empty(value: JSONValue | None) -> JSONMap:
    """将可选 JSON 值收窄为对象，非对象时返回空对象。"""
    return value if isinstance(value, dict) else {}


def _optional_string(value: JSONValue | None) -> str | None:
    """将可选 JSON 值收窄为字符串或 None。"""
    return value if isinstance(value, str) else None


def _cancellation_record_from_dict(data: JSONMap) -> CancellationRecord:
    """严格重建取消记录的枚举和不可变集合。"""
    request = require_json_map(data["request"])
    return CancellationRecord(
        request=CancellationRequest(get_string(request, "request_id"), get_string(request, "run_id"),
            get_string(request, "reason"), CancellationOrigin(get_string(request, "origin")),
            get_string(request, "requested_at"), request.get("parent_request_id")),
        revision=get_integer(data, "revision"), phase=CancellationPhase(get_string(data, "phase")),
        pending_operation_ids=_cancellation_string_tuple(data["pending_operation_ids"]),
        pending_child_run_ids=_cancellation_string_tuple(data["pending_child_run_ids"]),
        effect=CancellationEffect(get_string(data, "effect")), diagnostic_code=data.get("diagnostic_code"),
        updated_at=get_string(data, "updated_at"), settled_at=data.get("settled_at"), version=get_integer(data, "version"))


def _cancellation_string_tuple(value: JSONValue) -> tuple[str, ...]:
    """拒绝字符串冒充数组，以及重复的资源标识。"""
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("取消资源标识必须是字符串数组")
    if len(set(value)) != len(value):
        raise ValueError("取消资源标识不能重复")
    return tuple(value)


def _cancellation_intent_from_dict(data: JSONMap) -> CancellationCommitIntent:
    """读取版本 1 意图，不改变运行容器版本。"""
    return CancellationCommitIntent(get_string(data, "request_id"), _agent_run_from_dict(require_json_map(data["run"])),
        tuple(_run_event_from_dict(require_json_map(item)) for item in data["events"]),
        _cancellation_string_tuple(data["approval_ids"]), _cancellation_record_from_dict(require_json_map(data["settled_record"])),
        get_integer(data, "version"))


def _validate_cancellation_revision(current: CancellationRecord | None, record: CancellationRecord, expected_revision: int) -> None:
    """首次请求固定不变，进度仅允许逐次递增。"""
    if expected_revision < 0 or (current.revision if current else 0) != expected_revision or record.revision != expected_revision + 1:
        raise ValueError("取消记录修订号冲突")
    if current is not None:
        if current.request != record.request or current.phase is CancellationPhase.SETTLED:
            raise ValueError("首次请求不可变且 settled 记录不可修改")
        before = (current.phase, current.pending_operation_ids, current.pending_child_run_ids, current.effect, current.diagnostic_code)
        after = (record.phase, record.pending_operation_ids, record.pending_child_run_ids, record.effect, record.diagnostic_code)
        if before == after:
            raise ValueError("取消进度未变化，不应增加修订号")


def _validate_cancellation_intent(run: AgentRun, current: CancellationRecord | None, intent: CancellationCommitIntent) -> None:
    """校验取消终态只能来自同一次已落盘请求。"""
    if current is None or current.request != intent.settled_record.request:
        raise ValueError("取消提交缺少一致的持久化请求")
    if (run.session_id, run.run_id) != (intent.run.session_id, intent.run.run_id):
        raise ValueError("取消运行归属不一致")
    if run.state.is_ended() and run != intent.run:
        raise ValueError("取消提交不能覆盖既有终态")
    if current != intent.settled_record:
        _validate_cancellation_revision(current, intent.settled_record, intent.settled_record.revision - 1)


def _validate_cancellation_events(existing: tuple[RunEvent, ...], intent: CancellationCommitIntent) -> None:
    """已存在的序号必须匹配全部事件内容，未存在的部分必须连续。"""
    terminal_types = {RunEventType.RUN_COMPLETED, RunEventType.RUN_FAILED, RunEventType.RUN_ABANDONED, RunEventType.RUN_CANCELLED}
    if any(event.event_type in terminal_types and event != intent.events[-1] for event in existing):
        raise ValueError("取消提交不能覆盖或重复既有终态事件")
    length = len(existing)
    for event in intent.events:
        if event.sequence <= len(existing):
            if existing[event.sequence - 1] != event:
                raise ValueError("取消事件与既有序号内容冲突")
        else:
            if event.sequence != length + 1:
                raise ValueError("取消事件序号不连续")
            length += 1
    if len(existing) > intent.events[-1].sequence:
        raise ValueError("取消终态后存在额外事件")
