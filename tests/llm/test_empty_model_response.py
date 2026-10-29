"""空模型输出必须确定失败，不重发、不降级或成功投影。"""

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from dotclaw.llm.base import ChatChunk, ChatTextDelta, EmptyModelResponseError, Message, TextDeltaKind, TokenUsage, ToolCall
from dotclaw.llm.proxy import LLMProxy
from dotclaw.runtime.adapters.llm_proxy_adapter import LLMProxyAdapter
from dotclaw.runtime.adapters.run_repository import RunRepositoryAdapter
from dotclaw.runtime.application.ports import InvalidLLMResponseError
from dotclaw.runtime.domain.events import RunEventType
from dotclaw.runtime.domain.facts import MessageRole, RunErrorCode, RunMessage, RunMessageKind
from tests.llm.test_openai_chat_completions_stream import _FakeClient, _FakeNonStreamClient, _chunk, _delta, _usage
from tests.runtime_v2.test_llm_proxy_adapter import _make_context, _make_execution
from tests.runtime_v2.test_runtime_engine import _engine, _request


def _proxy(chunks: list[ChatChunk]) -> tuple[LLMProxy, Mock, Mock]:
    """提供多候选和多次重试预算，验证空输出仍只调用一次。"""
    async def chat(*args, **kwargs) -> AsyncIterator[ChatChunk]:
        for chunk in chunks:
            yield chunk
    client = Mock()
    client.chat = Mock(side_effect=chat)
    router = Mock()
    router.select.return_value = ["primary", "fallback"]
    router.get_provider_name.return_value = "fake"
    router.get_client.return_value = client
    router.try_acquire = AsyncMock()
    router._get_retry_config.return_value = 3
    router._get_backoff_config.return_value = 0
    return LLMProxy(router), router, client


@pytest.mark.asyncio
@pytest.mark.parametrize("chunks", [[], [ChatChunk(usage=TokenUsage(4, 0))], [ChatChunk(text_deltas=(ChatTextDelta(TextDeltaKind.REASONING, "思考"),))], [ChatChunk(text_deltas=(ChatTextDelta(TextDeltaKind.RESPONSE, " \n\t"),))]])
async def test_invalid_output_has_one_failure_without_retry_or_fallback(chunks) -> None:
    proxy, router, client = _proxy(chunks)
    with pytest.raises(EmptyModelResponseError):
        _ = [chunk async for chunk in proxy.chat([Message("user", "隔离问题")])]
    client.chat.assert_called_once()
    router.get_client.assert_called_once_with("primary")
    router.report_failure.assert_called_once_with("primary")
    router.report_success.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("chunks", [[ChatChunk(tool_calls=(ToolCall("c", "tool", "{}"),))], [ChatChunk(text_deltas=(ChatTextDelta(TextDeltaKind.RESPONSE, "\n  原始缩进\n"),))]])
async def test_first_chunk_valid_output_is_not_trimmed(chunks) -> None:
    proxy, router, _ = _proxy(chunks)
    assert [chunk async for chunk in proxy.chat([Message("user", "问题")])] == chunks
    router.report_success.assert_called_once_with("primary")
    router.report_failure.assert_not_called()


@pytest.mark.asyncio
async def test_driver_empty_and_usage_only_keep_real_metadata() -> None:
    assert [chunk async for chunk in _FakeClient([]).chat([])] == []
    chunks = [chunk async for chunk in _FakeClient([_chunk(_delta(), usage=_usage(4, 0))]).chat([])]
    assert chunks[-1].finish_reason is None
    assert chunks[-1].usage == TokenUsage(4, 0)
    chunks = [chunk async for chunk in _FakeClient([_chunk(_delta(content="ok"))]).chat([])]
    assert all(chunk.finish_reason is None and chunk.usage is None for chunk in chunks)


@pytest.mark.asyncio
async def test_nonstream_tool_only_preserves_calls_and_finish_reason() -> None:
    client = _FakeNonStreamClient("")
    client._response.choices[0].finish_reason = "tool_calls"
    client._response.choices[0].message.tool_calls = [SimpleNamespace(id="c1", function=SimpleNamespace(name="read", arguments="{}"))]
    chunks = [chunk async for chunk in client.chat([], stream=False)]
    assert chunks[0].tool_calls == (ToolCall("c1", "read", "{}"),)
    assert chunks[0].finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_adapter_preserves_invalid_response_cause() -> None:
    proxy, _, _ = _proxy([])
    with pytest.raises(InvalidLLMResponseError) as caught:
        await LLMProxyAdapter(proxy).complete(_make_context(), _make_execution("empty-run"))
    assert isinstance(caught.value.__cause__, EmptyModelResponseError)


@pytest.mark.asyncio
@pytest.mark.parametrize("through_adapter", [True, False])
async def test_empty_engine_result_is_failed_without_success_projection(tmp_path, through_adapter) -> None:
    engine = _engine(tmp_path)
    if through_adapter:
        proxy, _, _ = _proxy([])
        engine._llm_port = LLMProxyAdapter(proxy)
    else:
        engine._llm_port.complete = AsyncMock(return_value=RunMessage("empty", 0, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, " \n"))
    result = await engine.execute(_request("session-empty"))
    assert result.state.outcome().value == "failed"
    assert result.error.code is RunErrorCode.EMPTY_MODEL_RESPONSE
    assert result.error.retryable is False
    repository = RunRepositoryAdapter(tmp_path)
    assert await repository.load_conversation("session-empty") == ()
    assert (await repository.load_run("session-empty", result.run_id)).error == result.error
    events = await repository.load_events("session-empty", result.run_id)
    assert RunEventType.RUN_FAILED in [event.event_type for event in events]
    assert RunEventType.RUN_COMPLETED not in [event.event_type for event in events]


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_cancel_wins_over_empty_response(tmp_path, raises) -> None:
    engine = _engine(tmp_path)
    async def complete(context, execution, output_port):
        engine._cancellation_service.request(execution.run_id, "用户取消")
        if raises:
            raise InvalidLLMResponseError("空输出")
        return RunMessage("empty", 0, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "")
    engine._llm_port.complete = complete
    result = await engine.execute(_request("session-cancel"))
    assert result.state.outcome().value == "cancelled"
