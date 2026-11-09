"""本地真实 HTTP 流经 SDK、代理和适配器取消后的连接证据。"""

import asyncio
import json
from types import SimpleNamespace

import pytest
import httpx
from openai import AsyncOpenAI

from dotclaw.llm.drivers.openai_chat_completions import OpenAIChatCompletionsClient
from dotclaw.runtime.adapters.llm_proxy_adapter import LLMProxyAdapter
from tests.llm.test_empty_model_response import _proxy
from tests.runtime_v2.test_llm_proxy_adapter import _make_context, _make_execution


async def test_cancelling_while_emitting_closes_real_http_stream_without_closing_shared_client() -> None:
    emitted, disconnected = asyncio.Event(), asyncio.Event()
    connections = []
    handlers = []
    async def handle(reader, writer):
        handlers.append(asyncio.current_task())
        number = len(connections)
        connections.append(writer)
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n") if line.lower().startswith(b"content-length:"))
            await reader.readexactly(length)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n")
            chunk = {"id": "local", "object": "chat.completion.chunk", "created": 0, "model": "test",
                "choices": [{"index": 0, "delta": {"content": "first" if number == 0 else "done"}, "finish_reason": None}]}
            writer.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            if number:
                writer.write(b"data: [DONE]\n\n")
                await writer.drain()
            else:
                await writer.drain()
                await reader.read()
                disconnected.set()
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    sdk = AsyncOpenAI(api_key="local-test", base_url=f"http://127.0.0.1:{port}/v1", max_retries=0,
        http_client=httpx.AsyncClient(trust_env=False))
    driver = OpenAIChatCompletionsClient(model="test")
    driver._get_client = lambda: sdk
    proxy, router, _ = _proxy([])
    router.get_client.return_value = driver
    adapter = LLMProxyAdapter(proxy)
    async def emit(event):
        emitted.set()
        await asyncio.Event().wait()
    first = asyncio.create_task(adapter.complete(_make_context(), _make_execution("first"), SimpleNamespace(emit=emit)))
    try:
        await asyncio.wait_for(emitted.wait(), 3)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.wait_for(disconnected.wait(), 3)
        assert not sdk.is_closed()
        second = await adapter.complete(_make_context(), _make_execution("second"))
        assert second.content == "done"
        assert len(connections) == 2
        router.report_failure.assert_not_called()
        assert router.cancel_call.call_count == 1
    finally:
        if not first.done():
            first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await sdk.close()
        for writer in connections:
            writer.close()
        server.close()
        await server.wait_closed()
        await asyncio.gather(*handlers, return_exceptions=True)
