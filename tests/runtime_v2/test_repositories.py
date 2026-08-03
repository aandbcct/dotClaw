"""Runtime E6 的 v4 RunRepository 格式与契约测试。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Protocol

import pytest

from dotclaw.runtime.adapters import CheckpointRepositoryAdapter, InMemoryRunRepository, RunRepositoryAdapter
from dotclaw.runtime.adapters.run_repository import _run_event_from_dict
from dotclaw.runtime.application.ports import RunRepository
from dotclaw.runtime.domain.events import RunEvent, RunEventType
from dotclaw.runtime.domain.context import (
    ContextContributionKind,
    ContextOwner,
    ContextPersistenceMode,
    ContextSlotSnapshot,
    ContextSlotStatus,
    TextSlotContent,
    ContextVersion,
    StagedHistoryCompression,
    StagedHistoryCompressionStatus,
    SuccessCommitIntent,
    new_context_version,
)
from dotclaw.runtime.domain.control import AgentAction
from dotclaw.runtime.domain.facts import (
    AgentPolicySnapshot,
    AgentRun,
    JSONMap,
    MessageRole,
    RunCheckpoint,
    RunMessage,
    RunMessageKind,
    require_json_map,
)
from dotclaw.runtime.domain.state import (
    AgentRunState,
    RunOutcome,
    RunStage,
    Running,
)


class ContextVersionRepository(Protocol):
    """E1 Context Version 契约测试需要的最小仓储操作。"""

    async def create_run(self, run: AgentRun) -> None:
        """创建测试 Run。"""

    async def append_context_version(self, session_id: str, run_id: str, context_version: ContextVersion) -> None:
        """追加上下文版本。"""

    async def load_context_versions(self, session_id: str, run_id: str) -> tuple[ContextVersion, ...]:
        """读取上下文版本。"""


def _run() -> AgentRun:
    """构造 v4 仓储测试所需的最小 Run。"""
    return AgentRun(
        run_id="run-1",
        session_id="session-1",
        agent_id="agent-1",
        state=AgentRunState(mode=Running(RunStage.CALLING_LLM)),
        started_at="2026-07-20T00:00:00+00:00",
        policy=AgentPolicySnapshot("agent-1", "identity-v1", "model-v1", 8),
        input_message_id="user-1",
    )


def _context_version(version: int) -> ContextVersion:
    """构造只包含稳定快照型 Slot 的完整版本。"""
    return new_context_version(
        version=version,
        slots=(
            ContextSlotSnapshot(
                slot_id="identity",
                owner=ContextOwner.AGENT,
                contribution_kind=ContextContributionKind.SYSTEM_CONTENT,
                persistence_mode=ContextPersistenceMode.SNAPSHOT,
                status=ContextSlotStatus.INCLUDED,
                injection_order=0,
                content=TextSlotContent("你是测试助手。"),
                content_hash="identity-hash",
            ),
            ContextSlotSnapshot(
                slot_id="history_compressions",
                owner=ContextOwner.SESSION,
                contribution_kind=ContextContributionKind.HISTORY_COMPRESSIONS,
                persistence_mode=ContextPersistenceMode.SNAPSHOT,
                status=ContextSlotStatus.EMPTY,
                injection_order=1,
                content=TextSlotContent(""),
            ),
        ),
        content_hash=f"context-hash-{version}",
        tool_schema_hash="tool-schema-hash",
    )


async def _assert_context_version_contract(repository: ContextVersionRepository) -> None:
    """验证 Fake 与文件仓储共享的追加不可变契约。"""
    run: AgentRun = _run()
    version_one: ContextVersion = _context_version(1)
    version_two: ContextVersion = _context_version(2)
    await repository.create_run(run)
    await repository.append_context_version(run.session_id, run.run_id, version_one)
    await repository.append_context_version(run.session_id, run.run_id, version_two)
    assert await repository.load_context_versions(run.session_id, run.run_id) == (version_one, version_two)
    with pytest.raises(ValueError, match="连续递增"):
        await repository.append_context_version(run.session_id, run.run_id, version_two)


async def test_in_memory_run_repository_satisfies_context_version_contract() -> None:
    """内存 Fake 必须与真实 Adapter 共享 v4 版本语义。"""
    await _assert_context_version_contract(InMemoryRunRepository())


async def test_file_run_repository_satisfies_context_version_contract(tmp_path: Path) -> None:
    """文件 Adapter 必须与内存 Fake 共享 v4 版本语义。"""
    await _assert_context_version_contract(RunRepositoryAdapter(tmp_path))


async def _assert_run_control_contract(repository: RunRepository) -> None:
    """验证活动版本、候选和成功意图均由 run.json 控制面保存。"""
    run: AgentRun = _run()
    candidate: StagedHistoryCompression = StagedHistoryCompression(
        candidate_id="candidate-1",
        status=StagedHistoryCompressionStatus.STAGED,
        session_baseline_version=1,
        covered_through_conversation_id="conversation-1",
        source_hash="source-hash",
        summary_hash="summary-hash",
        context_version=1,
    )
    intent: SuccessCommitIntent = SuccessCommitIntent(
        conversation_id="conversation-2",
        latest_candidate_id=candidate.candidate_id,
        target_outcome=RunOutcome.COMPLETED,
    )
    await repository.create_run(run)
    await repository.append_context_version(run.session_id, run.run_id, _context_version(1))
    await repository.set_active_context_version(run.session_id, run.run_id, 1)
    await repository.save_staged_history_compressions(run.session_id, run.run_id, (candidate,))
    await repository.save_success_commit_intent(run.session_id, run.run_id, intent)
    persisted: AgentRun | None = await repository.load_run(run.session_id, run.run_id)
    assert persisted is not None
    assert persisted.active_context_version == 1
    assert persisted.staged_history_compressions == (candidate,)
    assert persisted.success_commit_intent == intent


async def test_in_memory_run_repository_satisfies_run_control_contract() -> None:
    """内存 Fake 必须保存与文件 Adapter 相同的控制面事实。"""
    await _assert_run_control_contract(InMemoryRunRepository())


async def test_file_run_repository_satisfies_run_control_contract(tmp_path: Path) -> None:
    """文件 Adapter 必须保存与内存 Fake 相同的控制面事实。"""
    await _assert_run_control_contract(RunRepositoryAdapter(tmp_path))


async def test_v4_messages_payload_keeps_context_versions_and_messages_separate(tmp_path: Path) -> None:
    """摘要候选正文不得进入 run.json，完整版本只写 messages.json。"""
    repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path)
    run: AgentRun = _run()
    candidate: StagedHistoryCompression = StagedHistoryCompression(
        candidate_id="candidate-1",
        status=StagedHistoryCompressionStatus.STAGED,
        session_baseline_version=4,
        covered_through_conversation_id="conversation-3",
        source_hash="source-hash",
        summary_hash="summary-hash",
        context_version=1,
    )
    run = replace(run, staged_history_compressions=(candidate,))
    message: RunMessage = RunMessage("user-1", 1, RunMessageKind.USER_INPUT, MessageRole.USER, "你好")
    await repository.create_run(run)
    await repository.save_messages(run.session_id, run.run_id, (message,))
    await repository.append_context_version(run.session_id, run.run_id, _context_version(1))

    run_payload: JSONMap = require_json_map(json.loads(
        (tmp_path / run.session_id / "agent_runs" / run.run_id / "run.json").read_text(encoding="utf-8"),
    ))
    messages_payload: JSONMap = require_json_map(json.loads(
        (tmp_path / run.session_id / "agent_runs" / run.run_id / "messages.json").read_text(encoding="utf-8"),
    ))
    assert run_payload["version"] == 4
    assert messages_payload["version"] == 4
    assert len(messages_payload["context_versions"]) == 1
    raw_candidates = run_payload["staged_history_compressions"]
    assert isinstance(raw_candidates, list)
    candidate_payload: JSONMap = require_json_map(raw_candidates[0])
    assert set(candidate_payload) == {
        "candidate_id",
        "status",
        "session_baseline_version",
        "covered_through_conversation_id",
        "source_hash",
        "summary_hash",
        "context_version",
    }
    assert set(messages_payload) == {"run_id", "version", "context_versions", "messages"}


async def test_v1_and_v2_messages_are_rejected_without_conversion(tmp_path: Path) -> None:
    """任何历史 messages.json 读取都必须明确失败，禁止隐式迁移。"""
    repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path)
    run: AgentRun = _run()
    await repository.create_run(run)
    path: Path = tmp_path / run.session_id / "agent_runs" / run.run_id / "messages.json"
    for version in (1, 2, 3):
        path.write_text(json.dumps({"run_id": run.run_id, "version": version, "messages": []}), encoding="utf-8")
        with pytest.raises(ValueError, match="仅支持 v4"):
            await repository.load_messages(run.session_id, run.run_id)


async def test_checkpoint_writes_and_reads_v4_control_fields(tmp_path: Path) -> None:
    """checkpoint.json 必须使用 v4，且只保存活动版本与候选引用。"""
    repository: CheckpointRepositoryAdapter = CheckpointRepositoryAdapter(tmp_path)
    checkpoint: RunCheckpoint = RunCheckpoint(
        checkpoint_id="checkpoint-1",
        run_id="run-1",
        session_id="session-1",
        checkpoint_sequence=1,
        event_sequence=2,
        message_sequence=3,
        pending={},
        budget={"max_iterations": 8},
        active_context_version=2,
        staged_history_compression_ids=("candidate-1",),
    )
    await repository.save(checkpoint)
    assert await repository.load(checkpoint.session_id, checkpoint.run_id) == checkpoint
    payload: JSONMap = require_json_map(json.loads(
        (tmp_path / checkpoint.session_id / "agent_runs" / checkpoint.run_id / "checkpoint.json").read_text(encoding="utf-8"),
    ))
    assert payload["version"] == 4
    assert payload["active_context_version"] == 2
    assert payload["staged_history_compression_ids"] == ["candidate-1"]


async def test_file_repository_uses_atomic_replacement_for_v4_payload(tmp_path: Path) -> None:
    """v4 多次写入后不得遗留临时文件，证明文件替换路径原子收口。"""
    repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path)
    run: AgentRun = _run()
    await repository.create_run(run)
    await repository.save_messages(
        run.session_id,
        run.run_id,
        (RunMessage("user-1", 1, RunMessageKind.USER_INPUT, MessageRole.USER, "第一条"),),
    )
    await repository.save_messages(
        run.session_id,
        run.run_id,
        (RunMessage("user-1", 1, RunMessageKind.USER_INPUT, MessageRole.USER, "第二条"),),
    )
    assert not tuple(tmp_path.rglob("*.tmp"))


# ---------------------------------------------------------------------------
# PR1：事件反序列化契约（7.1）
# ---------------------------------------------------------------------------

def _valid_event_dict() -> JSONMap:
    """构造一个字段齐全的合法事件字典。"""
    return {
        "run_id": "run-1",
        "sequence": 2,
        "event_type": RunEventType.LLM_COMPLETED.value,
        "occurred_at": "2026-07-20T00:00:00+00:00",
        "message_ids": ["m-1", "m-2"],
        "summary": "模型完成",
        "data": {"call_index": 1},
    }


def test_event_round_trips_through_to_dict() -> None:
    """RunEvent.to_dict() 后可由 _run_event_from_dict() 正确恢复。"""
    event: RunEvent = RunEvent(
        run_id="run-1",
        sequence=3,
        event_type=RunEventType.TOOL_STARTED,
        occurred_at="2026-07-20T00:00:00+00:00",
        message_ids=("m-1",),
        summary="工具开始",
        data={"call_id": "c-1"},
    )
    assert _run_event_from_dict(event.to_dict()) == event


def test_event_uses_defaults_when_optional_fields_absent() -> None:
    """可选字段缺失时使用默认值。"""
    data: JSONMap = {
        "run_id": "run-1",
        "sequence": 1,
        "event_type": RunEventType.RUN_STARTED.value,
        "occurred_at": "2026-07-20T00:00:00+00:00",
    }
    event: RunEvent = _run_event_from_dict(data)
    assert event.message_ids == ()
    assert event.summary == ""
    assert event.data == {}


def test_event_rejects_invalid_event_type() -> None:
    """非法 event_type 必须失败。"""
    data: JSONMap = _valid_event_dict()
    data["event_type"] = "not_a_type"
    with pytest.raises(ValueError):
        _run_event_from_dict(data)


def test_event_rejects_zero_sequence() -> None:
    """sequence=0 必须失败。"""
    data: JSONMap = _valid_event_dict()
    data["sequence"] = 0
    with pytest.raises(ValueError):
        _run_event_from_dict(data)


def test_event_rejects_bool_sequence() -> None:
    """布尔值不视为整数，sequence 为 True 必须失败。"""
    data: JSONMap = _valid_event_dict()
    data["sequence"] = True
    with pytest.raises(ValueError):
        _run_event_from_dict(data)


def test_event_rejects_empty_run_id() -> None:
    """空 run_id 必须失败。"""
    data: JSONMap = _valid_event_dict()
    data["run_id"] = ""
    with pytest.raises(ValueError):
        _run_event_from_dict(data)


def test_event_rejects_non_string_occurred_at() -> None:
    """非字符串 occurred_at 必须失败。"""
    data: JSONMap = _valid_event_dict()
    data["occurred_at"] = 123
    with pytest.raises(ValueError):
        _run_event_from_dict(data)


def test_event_rejects_empty_occurred_at() -> None:
    """空字符串 occurred_at 必须失败。"""
    data: JSONMap = _valid_event_dict()
    data["occurred_at"] = ""
    with pytest.raises(ValueError):
        _run_event_from_dict(data)


def test_event_rejects_non_string_message_ids_elements() -> None:
    """message_ids 含非字符串元素必须失败。"""
    data: JSONMap = _valid_event_dict()
    data["message_ids"] = ["m-1", 5]
    with pytest.raises(ValueError):
        _run_event_from_dict(data)


def test_event_rejects_non_object_data() -> None:
    """data 不是对象必须失败。"""
    data: JSONMap = _valid_event_dict()
    data["data"] = "not-an-object"
    with pytest.raises(ValueError):
        _run_event_from_dict(data)


def test_event_rejects_non_string_summary_when_present() -> None:
    """summary 已出现但非字符串必须失败。"""
    data: JSONMap = _valid_event_dict()
    data["summary"] = 123
    with pytest.raises(ValueError):
        _run_event_from_dict(data)


def test_historical_approval_event_without_data_is_readable() -> None:
    """历史审批事件即使缺少 data 字段仍可读取（向后兼容）。"""
    data: JSONMap = {
        "run_id": "run-1",
        "sequence": 1,
        "event_type": RunEventType.APPROVAL_RESOLVED.value,
        "occurred_at": "2026-07-20T00:00:00+00:00",
    }
    event: RunEvent = _run_event_from_dict(data)
    assert event.event_type is RunEventType.APPROVAL_RESOLVED
    assert event.data == {}


# ---------------------------------------------------------------------------
# PR1：文件仓储 load_events 契约（7.2）
# ---------------------------------------------------------------------------

def _write_events(path: Path, lines: list[str]) -> None:
    """原子写入 events.jsonl 测试内容（末尾统一换行）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _event_line(run_id: str, sequence: int, event_type: RunEventType = RunEventType.RUN_STARTED) -> str:
    """构造一条合法事件行。"""
    return json.dumps(
        {
            "run_id": run_id,
            "sequence": sequence,
            "event_type": event_type.value,
            "occurred_at": "2026-07-20T00:00:00+00:00",
            "message_ids": [],
            "summary": "",
            "data": {},
        },
        ensure_ascii=False,
    )


def _events_path(tmp_path: Path, session_id: str = "session-1", run_id: str = "run-1") -> Path:
    """返回 events.jsonl 的测试路径。"""
    return tmp_path / session_id / "agent_runs" / run_id / "events.jsonl"


async def test_file_load_events_missing_file_returns_empty(tmp_path: Path) -> None:
    """events.jsonl 不存在时返回空元组。"""
    repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path)
    assert await repository.load_events("session-1", "run-1") == ()


async def test_file_load_events_empty_file_returns_empty(tmp_path: Path) -> None:
    """空文件返回空元组。"""
    repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path)
    _write_events(_events_path(tmp_path), [])
    assert await repository.load_events("session-1", "run-1") == ()


async def test_file_load_events_single_event(tmp_path: Path) -> None:
    """单个事件正常读取。"""
    repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path)
    _write_events(_events_path(tmp_path), [_event_line("run-1", 1)])
    events = await repository.load_events("session-1", "run-1")
    assert len(events) == 1
    assert events[0].sequence == 1
    assert events[0].run_id == "run-1"


async def test_file_load_events_multiple_events_in_order(tmp_path: Path) -> None:
    """多个事件按序读取。"""
    repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path)
    _write_events(_events_path(tmp_path), [
        _event_line("run-1", 1),
        _event_line("run-1", 2),
        _event_line("run-1", 3),
    ])
    events = await repository.load_events("session-1", "run-1")
    assert [event.sequence for event in events] == [1, 2, 3]


async def test_file_load_events_rejects_corrupted_json(tmp_path: Path) -> None:
    """JSON 损坏时失败。"""
    repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path)
    _write_events(_events_path(tmp_path), [_event_line("run-1", 1), "{not json"])
    with pytest.raises(ValueError, match="events.jsonl"):
        await repository.load_events("session-1", "run-1")


async def test_file_load_events_rejects_non_object_root(tmp_path: Path) -> None:
    """根节点不是对象时失败。"""
    repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path)
    _write_events(_events_path(tmp_path), ["[1, 2, 3]"])
    with pytest.raises(ValueError, match="events.jsonl"):
        await repository.load_events("session-1", "run-1")


async def test_file_load_events_rejects_sequence_not_starting_at_one(tmp_path: Path) -> None:
    """sequence 不从 1 开始时失败。"""
    repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path)
    _write_events(_events_path(tmp_path), [_event_line("run-1", 2)])
    with pytest.raises(ValueError, match="events.jsonl"):
        await repository.load_events("session-1", "run-1")


async def test_file_load_events_rejects_sequence_gap(tmp_path: Path) -> None:
    """sequence 跳号时失败。"""
    repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path)
    _write_events(_events_path(tmp_path), [_event_line("run-1", 1), _event_line("run-1", 3)])
    with pytest.raises(ValueError, match="events.jsonl"):
        await repository.load_events("session-1", "run-1")


async def test_file_load_events_rejects_duplicate_sequence(tmp_path: Path) -> None:
    """sequence 重复时失败。"""
    repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path)
    _write_events(_events_path(tmp_path), [_event_line("run-1", 1), _event_line("run-1", 1)])
    with pytest.raises(ValueError, match="events.jsonl"):
        await repository.load_events("session-1", "run-1")


async def test_file_load_events_rejects_mismatched_run_id(tmp_path: Path) -> None:
    """event.run_id 不匹配时失败。"""
    repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path)
    _write_events(_events_path(tmp_path), [_event_line("other-run", 1)])
    with pytest.raises(ValueError, match="events.jsonl"):
        await repository.load_events("session-1", "run-1")


async def test_file_load_events_rejects_blank_line_in_middle(tmp_path: Path) -> None:
    """中间空白行视为文件损坏。"""
    repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path)
    _write_events(_events_path(tmp_path), [_event_line("run-1", 1), "", _event_line("run-1", 2)])
    with pytest.raises(ValueError, match="events.jsonl"):
        await repository.load_events("session-1", "run-1")


async def test_file_load_events_error_message_includes_line_number(tmp_path: Path) -> None:
    """错误信息包含事件行号。"""
    repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path)
    _write_events(_events_path(tmp_path), [_event_line("run-1", 1), _event_line("run-1", 5)])
    with pytest.raises(ValueError, match="第 2 行"):
        await repository.load_events("session-1", "run-1")


async def test_file_load_events_rejects_corrupted_last_line(tmp_path: Path) -> None:
    """最后一行 JSON 损坏时失败，不静默返回此前事件。"""
    repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path)
    _write_events(_events_path(tmp_path), [_event_line("run-1", 1), "broken"])
    with pytest.raises(ValueError, match="events.jsonl"):
        await repository.load_events("session-1", "run-1")


async def test_file_load_events_running_read_returns_contiguous_prefix(tmp_path: Path) -> None:
    """运行中读取返回已持久化且从 1 开始连续的事件前缀（不要求跨文件快照）。"""
    repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path)
    _write_events(_events_path(tmp_path), [_event_line("run-1", 1), _event_line("run-1", 2)])
    events = await repository.load_events("session-1", "run-1")
    assert [event.sequence for event in events] == [1, 2]


# ---------------------------------------------------------------------------
# PR1：内存仓储 load_events 契约（7.3）
# ---------------------------------------------------------------------------

async def test_in_memory_load_events_empty_by_default() -> None:
    """未写入事件时返回空元组。"""
    repository: InMemoryRunRepository = InMemoryRunRepository()
    assert await repository.load_events("session-1", "run-1") == ()


async def test_in_memory_load_events_returns_appended_events() -> None:
    """追加后可以读取。"""
    repository: InMemoryRunRepository = InMemoryRunRepository()
    run: AgentRun = _run()
    message: RunMessage = RunMessage("m-1", 1, RunMessageKind.USER_INPUT, MessageRole.USER, "你好")
    await repository.create_run(run)
    await repository.save_messages(run.session_id, run.run_id, (message,))
    event_one: RunEvent = RunEvent(run.run_id, 1, RunEventType.RUN_STARTED, "2026-07-20T00:00:00+00:00", (message.message_id,))
    event_two: RunEvent = RunEvent(run.run_id, 2, RunEventType.LLM_STARTED, "2026-07-20T00:00:00+00:00", ())
    await repository.append_event(run.session_id, event_one)
    await repository.append_event(run.session_id, event_two)
    assert await repository.load_events(run.session_id, run.run_id) == (event_one, event_two)


async def test_in_memory_load_events_preserves_append_order() -> None:
    """多个事件保持追加顺序。"""
    repository: InMemoryRunRepository = InMemoryRunRepository()
    run: AgentRun = _run()
    message: RunMessage = RunMessage("m-1", 1, RunMessageKind.USER_INPUT, MessageRole.USER, "你好")
    await repository.create_run(run)
    await repository.save_messages(run.session_id, run.run_id, (message,))
    events: list[RunEvent] = []
    for index in range(1, 4):
        event: RunEvent = RunEvent(run.run_id, index, RunEventType.LLM_STARTED, "2026-07-20T00:00:00+00:00", ())
        events.append(event)
        await repository.append_event(run.session_id, event)
    assert await repository.load_events(run.session_id, run.run_id) == tuple(events)


async def test_in_memory_load_events_isolates_runs() -> None:
    """不同 Run 的事件隔离。"""
    repository: InMemoryRunRepository = InMemoryRunRepository()
    run_a: AgentRun = replace(_run(), run_id="run-a", session_id="session-a")
    run_b: AgentRun = replace(_run(), run_id="run-b", session_id="session-b")
    message_a: RunMessage = RunMessage("m-a", 1, RunMessageKind.USER_INPUT, MessageRole.USER, "a")
    message_b: RunMessage = RunMessage("m-b", 1, RunMessageKind.USER_INPUT, MessageRole.USER, "b")
    await repository.create_run(run_a)
    await repository.create_run(run_b)
    await repository.save_messages("session-a", "run-a", (message_a,))
    await repository.save_messages("session-b", "run-b", (message_b,))
    event_a: RunEvent = RunEvent("run-a", 1, RunEventType.RUN_STARTED, "2026-07-20T00:00:00+00:00", (message_a.message_id,))
    event_b: RunEvent = RunEvent("run-b", 1, RunEventType.RUN_STARTED, "2026-07-20T00:00:00+00:00", (message_b.message_id,))
    await repository.append_event("session-a", event_a)
    await repository.append_event("session-b", event_b)
    assert await repository.load_events("session-a", "run-a") == (event_a,)
    assert await repository.load_events("session-b", "run-b") == (event_b,)