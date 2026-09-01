"""GUI v0.1 SSE 消息流契约测试。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import pytest

from dotclaw.runtime.application.dto import (
    ConversationMessage,
    LLMOutputEvent,
    LLMOutputKind,
    RunResult,
)
from dotclaw.runtime.application.ports import LLMOutputPort
from dotclaw.runtime.domain.facts import MessageRole, RunError, RunErrorCode
from dotclaw.runtime.domain.state import (
    AgentRunState,
    Ended,
    RunOutcome,
    RunStage,
    Suspended,
    SuspendReason,
)
from dotclaw.session.session import Session
from dotclaw.channel.web.app import create_app
from dotclaw.channel.web.schemas import SSEEvent
from dotclaw.channel.web.sse import SSEOutputAdapter, encode_sse_event


@dataclass(frozen=True)
class _ParsedEvent:
    """测试解析后的单个 SSE 事件。"""

    event: str
    data: dict[str, object]


class _FakeSessionManager:
    """SSE 路由前置校验所需的会话读取替身。"""

    def __init__(self, session_ids: tuple[str, ...]) -> None:
        self._sessions: dict[str, Session] = {
            session_id: Session(
                id=session_id,
                agent_id="default",
                model="chat-model",
                created_at="2026-09-01T09:00:00+08:00",
                updated_at="2026-09-01T09:00:00+08:00",
            )
            for session_id in session_ids
        }

    async def load(self, session_id: str) -> Session | None:
        """读取已知测试会话。"""
        return self._sessions.get(session_id)

    async def list_all(self) -> list[Session]:
        """满足应用装配所需的最小列表接口。"""
        return list(self._sessions.values())


class _FakeSessionInteraction:
    """按测试脚本产生增量和 RunResult 的交互替身。"""

    def __init__(self, result: RunResult) -> None:
        self.result: RunResult = result
        self.output_events: tuple[LLMOutputEvent, ...] = ()
        self.submit_error: Exception | None = None
        self.submit_started: asyncio.Event = asyncio.Event()
        self.allow_finish: asyncio.Event = asyncio.Event()
        self.block_before_finish: bool = False
        self.completed: asyncio.Event = asyncio.Event()
        self.active_submissions: int = 0
        self.max_active_submissions: int = 0

    async def submit(
        self,
        session: Session | str,
        user_message: str,
        output_port: LLMOutputPort | None = None,
    ) -> RunResult:
        """按顺序发送预设增量，并返回预设终态。"""
        del session, user_message
        self.active_submissions += 1
        self.max_active_submissions = max(
            self.max_active_submissions,
            self.active_submissions,
        )
        self.submit_started.set()
        try:
            if self.submit_error is not None:
                raise self.submit_error
            if output_port is not None:
                for event in self.output_events:
                    await output_port.emit(event)
            if self.block_before_finish:
                await self.allow_finish.wait()
            return self.result
        finally:
            self.active_submissions -= 1
            self.completed.set()


class _FakeHost:
    """向 Web 应用暴露会话查询与提交替身。"""

    def __init__(
        self,
        manager: _FakeSessionManager,
        interaction: _FakeSessionInteraction,
    ) -> None:
        self.session_manager: _FakeSessionManager = manager
        self.session_interaction: _FakeSessionInteraction = interaction

    async def shutdown(self) -> None:
        """测试宿主没有额外资源需要释放。"""


async def _ready_host(host: _FakeHost) -> _FakeHost:
    """把测试宿主包装为异步工厂。"""
    return host


@asynccontextmanager
async def _client(host: _FakeHost) -> AsyncIterator[httpx.AsyncClient]:
    """启动应用 lifespan 并返回内存 HTTP 客户端。"""
    app = create_app(host_factory=lambda: _ready_host(host))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            yield client


def _host(result: RunResult, *session_ids: str) -> tuple[_FakeHost, _FakeSessionInteraction]:
    """构造共享结果脚本的 Host 与交互替身。"""
    manager = _FakeSessionManager(session_ids or ("session-1",))
    interaction = _FakeSessionInteraction(result)
    return _FakeHost(manager, interaction), interaction


def _completed_result(
    run_id: str = "run-1",
    *,
    streamed: bool = True,
) -> RunResult:
    """构造成功终态结果。"""
    return RunResult(
        run_id=run_id,
        state=AgentRunState(mode=Ended(RunOutcome.COMPLETED)),
        final_message=ConversationMessage(
            "answer-1",
            MessageRole.ASSISTANT,
            "最终回答",
            "2026-09-01T09:01:00+08:00",
        ),
        has_streamed_response=streamed,
    )


def _parse_sse(text: str) -> list[_ParsedEvent]:
    """严格解析测试响应中的 event/data 块。"""
    normalized = text.replace("\r\n", "\n").strip()
    if not normalized:
        return []
    parsed: list[_ParsedEvent] = []
    for block in normalized.split("\n\n"):
        lines = block.splitlines()
        assert len(lines) == 2
        assert lines[0].startswith("event: ")
        assert lines[1].startswith("data: ")
        payload = json.loads(lines[1].removeprefix("data: "))
        assert isinstance(payload, dict)
        parsed.append(_ParsedEvent(lines[0].removeprefix("event: "), payload))
    return parsed


@pytest.mark.asyncio
async def test_sse_adapter_preserves_delta_order_and_ignores_empty_content() -> None:
    """输出适配器只转发非空增量，并保持 Runtime 交付顺序。"""
    adapter = SSEOutputAdapter(queue_capacity=4)
    await adapter.emit(
        LLMOutputEvent("session-1", "run-1", LLMOutputKind.REASONING_DELTA, "思考")
    )
    await adapter.emit(
        LLMOutputEvent("session-1", "run-1", LLMOutputKind.RESPONSE_DELTA, "")
    )
    await adapter.emit(
        LLMOutputEvent("session-1", "run-1", LLMOutputKind.RESPONSE_DELTA, "回答")
    )
    await adapter.finish(
        SSEEvent(
            event="run.completed",
            data={"session_id": "session-1", "run_id": "run-1", "status": "completed"},
        )
    )

    chunks = [chunk async for chunk in adapter.stream()]
    events = _parse_sse("".join(chunks))

    assert events == [
        _ParsedEvent(
            "message.delta",
            {
                "session_id": "session-1",
                "run_id": "run-1",
                "kind": "reasoning_delta",
                "content": "思考",
            },
        ),
        _ParsedEvent(
            "message.delta",
            {
                "session_id": "session-1",
                "run_id": "run-1",
                "kind": "response_delta",
                "content": "回答",
            },
        ),
        _ParsedEvent(
            "run.completed",
            {"session_id": "session-1", "run_id": "run-1", "status": "completed"},
        ),
    ]


def test_encode_sse_event_uses_one_compact_json_data_line() -> None:
    """SSE 编码必须保留中文，并避免多行 data 破坏浏览器解析。"""
    encoded = encode_sse_event(
        SSEEvent(
            event="stream.error",
            data={"message": "执行失败", "retryable": False},
        )
    )

    assert encoded == (
        'event: stream.error\n'
        'data: {"message":"执行失败","retryable":false}\n\n'
    )


@pytest.mark.asyncio
async def test_disconnect_drops_pending_and_future_deltas_without_blocking() -> None:
    """订阅断开后输出端口必须立即退化为丢弃，不能阻塞后台 Run。"""
    adapter = SSEOutputAdapter(queue_capacity=1)
    await adapter.emit(
        LLMOutputEvent("session-1", "run-1", LLMOutputKind.RESPONSE_DELTA, "已排队")
    )
    adapter.disconnect()

    await asyncio.wait_for(
        adapter.emit(
            LLMOutputEvent("session-1", "run-1", LLMOutputKind.RESPONSE_DELTA, "断连后")
        ),
        timeout=0.1,
    )
    chunks = [chunk async for chunk in adapter.stream()]

    assert chunks == []


@pytest.mark.asyncio
async def test_disconnected_adapter_does_not_cancel_background_submission() -> None:
    """关闭订阅只丢弃输出，后台提交仍能按原有语义完成。"""
    interaction = _FakeSessionInteraction(_completed_result())
    interaction.block_before_finish = True
    adapter = SSEOutputAdapter(queue_capacity=1)
    submission = asyncio.create_task(
        interaction.submit("session-1", "继续执行", adapter)
    )
    await interaction.submit_started.wait()

    adapter.disconnect()
    interaction.allow_finish.set()
    result = await asyncio.wait_for(submission, timeout=0.1)

    assert result.run_id == "run-1"
    assert interaction.completed.is_set()


@pytest.mark.asyncio
async def test_adapter_rejects_a_second_terminal_event() -> None:
    """单条消息流只能有一个终态事件。"""
    adapter = SSEOutputAdapter(queue_capacity=2)
    terminal = SSEEvent(
        event="run.completed",
        data={"session_id": "session-1", "run_id": "run-1", "status": "completed"},
    )
    await adapter.finish(terminal)

    with pytest.raises(RuntimeError, match="终态"):
        await adapter.finish(terminal)


@pytest.mark.asyncio
async def test_message_endpoint_streams_deltas_then_one_completed_terminal() -> None:
    """成功请求先输出增量，最后输出一个完成事件。"""
    host, interaction = _host(_completed_result(), "session-1")
    interaction.output_events = (
        LLMOutputEvent("session-1", "run-1", LLMOutputKind.REASONING_DELTA, "想"),
        LLMOutputEvent("session-1", "run-1", LLMOutputKind.RESPONSE_DELTA, "答"),
    )

    async with _client(host) as client:
        response = await client.post(
            "/api/v1/sessions/session-1/messages",
            json={"content": "你好"},
        )

    events = _parse_sse(response.text)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert [event.event for event in events] == [
        "message.delta",
        "message.delta",
        "run.completed",
    ]
    assert events[-1].data == {
        "session_id": "session-1",
        "run_id": "run-1",
        "status": "completed",
        "final_message": {
            "id": "answer-1",
            "role": "assistant",
            "content": "最终回答",
            "created_at": "2026-09-01T09:01:00+08:00",
        },
        "has_streamed_response": True,
    }


@pytest.mark.asyncio
async def test_http_200_with_session_busy_is_a_failed_run() -> None:
    """SSE 已开始后的业务失败必须由终态事件表达，不能把 200 当成功。"""
    result = RunResult(
        run_id="run-active",
        state=AgentRunState(mode=Ended(RunOutcome.FAILED)),
        error=RunError(
            RunErrorCode.SESSION_BUSY,
            "Session 存在未终态 Run，暂不接受普通请求",
        ),
    )
    host, _ = _host(result, "session-1")

    async with _client(host) as client:
        response = await client.post(
            "/api/v1/sessions/session-1/messages",
            json={"content": "第二条消息"},
        )

    events = _parse_sse(response.text)
    assert response.status_code == 200
    assert events == [
        _ParsedEvent(
            "run.failed",
            {
                "session_id": "session-1",
                "run_id": "run-active",
                "status": "failed",
                "error": {
                    "code": "session_busy",
                    "message": "Session 存在未终态 Run，暂不接受普通请求",
                    "retryable": False,
                },
            },
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected_event", "expected_status"),
    [
        (
            RunResult(
                run_id="run-cancelled",
                state=AgentRunState(mode=Ended(RunOutcome.CANCELLED)),
            ),
            "run.cancelled",
            "cancelled",
        ),
        (
            RunResult(
                run_id="run-approval",
                state=AgentRunState(
                    mode=Suspended(
                        SuspendReason.APPROVAL,
                        "approval-1",
                        RunStage.EXECUTING_TOOLS,
                    )
                ),
                approval_id="approval-1",
            ),
            "run.suspended",
            "waiting_approval",
        ),
        (
            RunResult(
                run_id="run-delegation",
                state=AgentRunState(
                    mode=Suspended(
                        SuspendReason.DELEGATION,
                        "child-1",
                        RunStage.CALLING_LLM,
                    )
                ),
                child_run_id="child-1",
            ),
            "run.suspended",
            "waiting_delegation",
        ),
    ],
)
async def test_non_success_results_have_normalized_terminal_events(
    result: RunResult,
    expected_event: str,
    expected_status: str,
) -> None:
    """取消与两类挂起状态必须映射为稳定的 Web 终态。"""
    host, _ = _host(result, "session-1")

    async with _client(host) as client:
        response = await client.post(
            "/api/v1/sessions/session-1/messages",
            json={"content": "执行"},
        )

    events = _parse_sse(response.text)
    assert len(events) == 1
    assert events[0].event == expected_event
    assert events[0].data["status"] == expected_status
    assert events[0].data["run_id"] == result.run_id


@pytest.mark.asyncio
async def test_unexpected_stream_error_is_sanitized() -> None:
    """适配层异常不得通过 SSE 泄露路径、密钥或堆栈。"""
    host, interaction = _host(_completed_result(), "session-1")
    interaction.submit_error = RuntimeError(
        "boom C:\\secret\\config.yaml sk-test-secret"
    )

    async with _client(host) as client:
        response = await client.post(
            "/api/v1/sessions/session-1/messages",
            json={"content": "触发异常"},
        )

    events = _parse_sse(response.text)
    assert response.status_code == 200
    assert events == [
        _ParsedEvent(
            "stream.error",
            {
                "session_id": "session-1",
                "run_id": "",
                "status": "failed",
                "error": {
                    "code": "internal_error",
                    "message": "Web 消息流执行失败",
                    "retryable": False,
                },
            },
        )
    ]
    assert "secret" not in response.text.lower()
    assert "traceback" not in response.text.lower()


@pytest.mark.asyncio
async def test_different_sessions_are_not_serialized_by_web_layer() -> None:
    """Web 层不得增加跨 Session 全局锁。"""
    result = _completed_result()
    host, interaction = _host(result, "session-1", "session-2")
    interaction.block_before_finish = True

    async with _client(host) as client:
        first = asyncio.create_task(
            client.post(
                "/api/v1/sessions/session-1/messages",
                json={"content": "第一条"},
            )
        )
        second = asyncio.create_task(
            client.post(
                "/api/v1/sessions/session-2/messages",
                json={"content": "第二条"},
            )
        )
        for _ in range(100):
            if interaction.active_submissions == 2:
                break
            await asyncio.sleep(0)
        assert interaction.active_submissions == 2
        interaction.allow_finish.set()
        await asyncio.gather(first, second)

    assert interaction.max_active_submissions == 2
