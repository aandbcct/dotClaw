"""TraceService 仓储读取与只读行为测试。"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from helpers import make_context_version, make_event, make_message, make_run

from dotclaw.runtime.adapters import InMemoryRunRepository
from dotclaw.runtime.domain.events import RunEventType
from dotclaw.runtime.domain.facts import MessageRole, RunMessageKind
from dotclaw.trace import SpanKind, TraceService
from dotclaw.trace.models import RunTrace

_READ_METHODS = {"find_run", "load_events", "load_messages", "load_context_versions"}


class RecordingRepository(InMemoryRunRepository):
    """记录被调用方法并委托给内存仓储；用于断言服务只做读取。"""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def find_run(self, run_id: str):
        self.calls.append("find_run")
        return await super().find_run(run_id)

    async def load_events(self, session_id: str, run_id: str):
        self.calls.append("load_events")
        return await super().load_events(session_id, run_id)

    async def load_messages(self, session_id: str, run_id: str):
        self.calls.append("load_messages")
        return await super().load_messages(session_id, run_id)

    async def load_context_versions(self, session_id: str, run_id: str):
        self.calls.append("load_context_versions")
        return await super().load_context_versions(session_id, run_id)


async def _populate(repo: InMemoryRunRepository) -> None:
    run = make_run()
    events = (
        make_event("run-1", 1, RunEventType.RUN_STARTED, message_ids=("msg-input",)),
        make_event("run-1", 2, RunEventType.LLM_STARTED, data={"call_index": 1, "model_id": "model-1", "context_version": 1}),
        make_event("run-1", 3, RunEventType.LLM_COMPLETED, message_ids=("msg-llm",)),
        make_event("run-1", 4, RunEventType.RUN_COMPLETED, message_ids=("msg-llm",)),
    )
    messages = (
        make_message("msg-input", 1, RunMessageKind.USER_INPUT, role=MessageRole.USER, content="question"),
        make_message("msg-llm", 2, RunMessageKind.LLM_RESPONSE, content="answer"),
    )
    await repo.create_run(run)
    await repo.save_messages("sess-1", "run-1", messages)
    await repo.append_context_version("sess-1", "run-1", make_context_version(1))
    for event in events:
        await repo.append_event("sess-1", event)


async def test_get_trace_reconstructs_spans():
    repo = InMemoryRunRepository()
    await _populate(repo)
    service = TraceService(repo)
    trace = await service.get_trace("run-1")
    assert isinstance(trace, RunTrace)
    assert {s.kind for s in trace.spans} == {SpanKind.RUN, SpanKind.LLM}
    assert trace.source.is_partial is False


async def test_get_trace_missing_run_raises_lookup():
    service = TraceService(InMemoryRunRepository())
    try:
        await service.get_trace("missing")
        assert False, "expected LookupError"
    except LookupError:
        pass


async def test_service_only_reads_repository():
    repo = RecordingRepository()
    await _populate(repo)
    service = TraceService(repo)
    trace = await service.get_trace("run-1")
    assert trace is not None
    assert "find_run" in repo.calls
    assert set(repo.calls).issubset(_READ_METHODS)
    assert not (set(repo.calls) - _READ_METHODS)
