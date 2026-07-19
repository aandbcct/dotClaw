"""Runtime messages.json v1 到 v2 的显式迁移测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import scripts.migrate_messages_v1_to_v2 as messages_migration

from scripts.migrate_messages_v1_to_v2 import (
    MessagesMigrationOutcome,
    MessagesMigrationReport,
    migrate_messages_v1_run,
)

from dotclaw.runtime.domain.events import RunEvent, RunEventType
from dotclaw.runtime.domain.facts import (
    JSONMap,
    JSONValue,
    MessageRole,
    RunMessage,
    RunMessageKind,
    ToolCall,
    require_json_map,
)


def _message(
    message_id: str,
    sequence: int,
    kind: RunMessageKind,
    role: MessageRole,
    content: str,
    tool_calls: tuple[ToolCall, ...] = (),
) -> RunMessage:
    """构造具有连续序号的旧消息样例。"""
    return RunMessage(message_id, sequence, kind, role, content, tool_calls=tool_calls)


def _write_v1_run(run_directory: Path) -> None:
    """写入包含两轮重复 ContextBundle 的 v1 Run 样例。"""
    run_directory.mkdir(parents=True)
    messages: tuple[RunMessage, ...] = (
        _message("input-1", 1, RunMessageKind.USER_INPUT, MessageRole.USER, "当前问题"),
        _message("context-1", 2, RunMessageKind.LLM_REQUEST, MessageRole.SYSTEM, "系统提示"),
        _message("context-2", 3, RunMessageKind.LLM_REQUEST, MessageRole.ASSISTANT, "历史回答"),
        _message("context-3", 4, RunMessageKind.LLM_REQUEST, MessageRole.USER, "当前问题"),
        _message(
            "response-1",
            5,
            RunMessageKind.LLM_RESPONSE,
            MessageRole.ASSISTANT,
            "查询工具",
            (ToolCall("call-1", "lookup", {}),),
        ),
        _message("tool-1", 6, RunMessageKind.TOOL_RESULT, MessageRole.TOOL, "工具结果"),
        _message("context-4", 7, RunMessageKind.LLM_REQUEST, MessageRole.SYSTEM, "系统提示"),
        _message("context-5", 8, RunMessageKind.LLM_REQUEST, MessageRole.ASSISTANT, "历史回答"),
        _message("context-6", 9, RunMessageKind.LLM_REQUEST, MessageRole.USER, "当前问题"),
        _message(
            "context-7",
            10,
            RunMessageKind.LLM_RESPONSE,
            MessageRole.ASSISTANT,
            "查询工具",
            (ToolCall("call-1", "lookup", {}),),
        ),
        _message("context-8", 11, RunMessageKind.TOOL_RESULT, MessageRole.TOOL, "工具结果"),
        _message("response-2", 12, RunMessageKind.FINAL_RESPONSE, MessageRole.ASSISTANT, "最终回答"),
    )
    messages_payload: JSONMap = {
        "run_id": run_directory.name,
        "version": 1,
        "messages": [message.to_dict() for message in messages],
    }
    (run_directory / "messages.json").write_text(
        json.dumps(messages_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    run_payload: JSONMap = {
        "run_id": run_directory.name,
        "policy": {"model_id": "legacy-model"},
    }
    (run_directory / "run.json").write_text(
        json.dumps(run_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    events: tuple[RunEvent, ...] = (
        RunEvent(run_directory.name, 1, RunEventType.RUN_STARTED, "2026-07-19T00:00:00+00:00", ("input-1",)),
        RunEvent(run_directory.name, 2, RunEventType.CONTEXT_BUILT, "2026-07-19T00:00:01+00:00", ("context-1", "context-2", "context-3")),
        RunEvent(run_directory.name, 3, RunEventType.LLM_COMPLETED, "2026-07-19T00:00:02+00:00", ("response-1",)),
        RunEvent(run_directory.name, 4, RunEventType.TOOL_COMPLETED, "2026-07-19T00:00:03+00:00", ("tool-1",)),
        RunEvent(run_directory.name, 5, RunEventType.CONTEXT_BUILT, "2026-07-19T00:00:04+00:00", ("context-4", "context-5", "context-6", "context-7", "context-8")),
        RunEvent(run_directory.name, 6, RunEventType.LLM_COMPLETED, "2026-07-19T00:00:05+00:00", ("response-2",)),
        RunEvent(run_directory.name, 7, RunEventType.RUN_COMPLETED, "2026-07-19T00:00:06+00:00", ("response-2",)),
    )
    (run_directory / "events.jsonl").write_text(
        "\n".join(json.dumps(event.to_dict(), ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )


def test_migration_separates_initial_context_and_incremental_facts(tmp_path: Path) -> None:
    """迁移应删除每轮 ContextBundle 副本、生成初始快照并重建模型调用事件。"""
    run_directory: Path = tmp_path / "session-1" / "agent_runs" / "run-1"
    _write_v1_run(run_directory)

    report: MessagesMigrationReport = migrate_messages_v1_run(run_directory)

    assert report.outcome is MessagesMigrationOutcome.MIGRATED
    assert report.original_message_count == 12
    assert report.incremental_message_count == 4
    assert report.removed_llm_request_count == 6
    assert report.messages_backup_path is not None
    assert report.events_backup_path is not None
    assert report.messages_backup_path.is_file()
    assert report.events_backup_path.is_file()

    messages_payload: JSONMap = require_json_map(json.loads(
        (run_directory / "messages.json").read_text(encoding="utf-8"),
    ))
    assert messages_payload["version"] == 2
    raw_initial_context: JSONValue | None = messages_payload.get("initial_context")
    assert isinstance(raw_initial_context, dict)
    initial_context: JSONMap = raw_initial_context
    raw_history: JSONValue | None = initial_context.get("history")
    assert isinstance(raw_history, dict)
    history: JSONMap = raw_history
    assert history["recent_messages"] == [{
        "conversation_id": "legacy-history-3",
        "role": "assistant",
        "content": "历史回答",
        "created_at": "",
    }]
    raw_stored_messages: JSONValue | None = messages_payload.get("messages")
    assert isinstance(raw_stored_messages, list)
    stored_messages: list[JSONValue] = raw_stored_messages
    assert [message["id"] for message in stored_messages if isinstance(message, dict)] == [
        "input-1",
        "response-1",
        "tool-1",
        "response-2",
    ]
    assert all(message["kind"] != "llm_request" for message in stored_messages if isinstance(message, dict))

    events: list[JSONMap] = [
        require_json_map(json.loads(line))
        for line in (run_directory / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["event_type"] for event in events] == [
        "run_started",
        "llm_started",
        "llm_completed",
        "tool_completed",
        "llm_started",
        "llm_completed",
        "run_completed",
    ]
    assert [event["sequence"] for event in events] == list(range(1, 8))
    raw_first_llm_data: JSONValue | None = events[1].get("data")
    assert isinstance(raw_first_llm_data, dict)
    first_llm_data: JSONMap = raw_first_llm_data
    assert first_llm_data["model_id"] == "legacy-model"
    assert first_llm_data["incremental_message_ids"] == ["input-1"]
    raw_second_llm_data: JSONValue | None = events[4].get("data")
    assert isinstance(raw_second_llm_data, dict)
    second_llm_data: JSONMap = raw_second_llm_data
    assert second_llm_data["incremental_message_ids"] == [
        "input-1",
        "response-1",
        "tool-1",
    ]


def test_migration_is_idempotent_for_current_format(tmp_path: Path) -> None:
    """同一目录完成迁移后再次执行应只报告已是当前格式。"""
    run_directory: Path = tmp_path / "session-2" / "agent_runs" / "run-2"
    _write_v1_run(run_directory)

    migrate_messages_v1_run(run_directory)
    report: MessagesMigrationReport = migrate_messages_v1_run(run_directory)

    assert report.outcome is MessagesMigrationOutcome.ALREADY_CURRENT
    assert report.messages_backup_path is None
    assert report.events_backup_path is None


def test_migration_restores_v1_backups_when_event_rewrite_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """双文件迁移中事件写入失败时，messages 与 events 均必须恢复为原始 v1 内容。"""
    run_directory: Path = tmp_path / "session-3" / "agent_runs" / "run-3"
    _write_v1_run(run_directory)
    original_messages: str = (run_directory / "messages.json").read_text(encoding="utf-8")
    original_events: str = (run_directory / "events.jsonl").read_text(encoding="utf-8")

    def fail_event_rewrite(events_path: Path, events: tuple[RunEvent, ...]) -> None:
        """模拟 events.jsonl 原子替换失败。"""
        raise OSError("模拟事件写入失败")

    monkeypatch.setattr(messages_migration, "_write_events", fail_event_rewrite)

    with pytest.raises(OSError, match="模拟事件写入失败"):
        migrate_messages_v1_run(run_directory)

    assert (run_directory / "messages.json").read_text(encoding="utf-8") == original_messages
    assert (run_directory / "events.jsonl").read_text(encoding="utf-8") == original_events
