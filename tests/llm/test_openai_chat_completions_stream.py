"""阶段三：Provider 流标准化与请求级状态隔离测试。

覆盖开发计划 §4 门槛：
- Client 解析：native reasoning_content、普通 content、同包双字段、usage-only、
  finish-only、多个工具调用一次性写出、跨 chunk 工具参数拼接、模式不自动猜测。
- 两个交错 chat()：工具参数 / finish reason / 标签缓冲互不串线，
  一个调用异常不污染另一个。
- Proxy 边界：无可见输出失败可降级、已展示 reasoning/response 后失败不可降级。
"""

from __future__ import annotations

import asyncio

import pytest

from dotclaw.llm.base import ChatChunk, ChatTextDelta, Message, TextDeltaKind, ToolCall
from dotclaw.llm.drivers.openai_chat_completions import (
    OpenAIChatCompletionsClient,
)
from dotclaw.llm.proxy import LLMProxy, CallSetupError, NonRetryableStreamError
from dotclaw.llm.reasoning import ReasoningMode, ReasoningPolicy


pytestmark = pytest.mark.asyncio


# ============================================================
# Mock 辅助
# ============================================================

class _MockAPIResponse:
    """把同步 chunk 列表包装为异步迭代器。"""

    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        self._iter = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class _FailAfterFirstResponse:
    """第一次迭代返回给定 chunk，第二次迭代抛异常（模拟流中断）。"""

    def __init__(self, chunk):
        self._chunk = chunk
        self._used = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._used:
            self._used = True
            return self._chunk
        raise RuntimeError("stream broke")


class _FakeClient(OpenAIChatCompletionsClient):
    """可注入 mock chunk 列表与推理策略的测试客户端（单次 chat 用一份 chunk）。"""

    def __init__(self, mock_chunks, policy: ReasoningPolicy | None = None):
        super().__init__(policy)
        self._mock_chunks = mock_chunks

    def _get_api_key(self) -> str:
        return "test"

    def _get_base_url(self) -> str:
        return "https://test/v1"

    def _get_model_id(self) -> str:
        return "test-model"

    def _get_client(self):
        class F:  # noqa: async 静态工厂
            class chat:
                class completions:
                    @staticmethod
                    async def create(**kw):
                        return _MockAPIResponse(self._mock_chunks)

        return F()


class _NonStreamMessage:
    """非流式 ChatCompletion 的 choice.message。"""

    def __init__(self, content: str, reasoning_content: str = ""):
        self.content = content
        self.reasoning_content = reasoning_content


class _NonStreamChoice:
    def __init__(self, message: _NonStreamMessage):
        self.message = message


class _NonStreamResponse:
    def __init__(self, content: str, reasoning_content: str = "", in_tok: int = 0, out_tok: int = 0):
        self.choices = [_NonStreamChoice(_NonStreamMessage(content, reasoning_content))]
        self.usage = _usage(in_tok, out_tok)


class _FakeNonStreamClient(OpenAIChatCompletionsClient):
    """注入非流式 ChatCompletion 响应的测试客户端（stream=False 调用）。"""

    def __init__(self, content: str, reasoning_content: str = "", in_tok: int = 0, out_tok: int = 0,
                 policy: ReasoningPolicy | None = None):
        super().__init__(policy)
        self._response = _NonStreamResponse(content, reasoning_content, in_tok, out_tok)

    def _get_api_key(self) -> str:
        return "test"

    def _get_base_url(self) -> str:
        return "https://test/v1"

    def _get_model_id(self) -> str:
        return "test-model"

    def _get_client(self):
        class F:  # noqa
            class chat:
                class completions:
                    @staticmethod
                    async def create(**kw):
                        return self._response

        return F()


class _SeqClient(OpenAIChatCompletionsClient):
    """每次 chat() 调用按顺序消费一份异步响应（用于交错/异常隔离测试）。"""

    def __init__(self, responses, policy: ReasoningPolicy | None = None):
        super().__init__(policy)
        self._responses = list(responses)
        self._cursor = 0

    def _get_api_key(self) -> str:
        return "test"

    def _get_base_url(self) -> str:
        return "https://test/v1"

    def _get_model_id(self) -> str:
        return "test-model"

    def _get_client(self):
        class F:  # noqa
            class chat:
                class completions:
                    @staticmethod
                    async def create(**kw):
                        resp = self._responses[self._cursor]
                        self._cursor += 1
                        return resp

        return F()


def _delta(content: str = "", reasoning: str = "", tc=None):
    """构造 OpenAI delta 对象（content / reasoning_content / tool_calls）。

    tc 形如 [{"i":0,"id":...,"n":...,"a":...}]，转换为与 OpenAI SDK 一致的
    Dtc/Df 对象（delta.tool_calls[i].function.name/arguments）。
    """

    class Df:
        def __init__(self, n=None, a=None):
            self.name = n
            self.arguments = a

    class Dtc:
        def __init__(self, i=0, id=None, f=None):
            self.index = i
            self.id = id
            self.function = f

    class D:
        def __init__(self, c=None, r=None, t=None):
            self.content = c
            self.reasoning_content = r
            self.tool_calls = t

    tool_calls = None
    if tc:
        tool_calls = [
            Dtc(i=t.get("i", 0), id=t.get("id"), f=Df(n=t.get("n"), a=t.get("a")))
            for t in tc
        ]
    return D(c=content, r=reasoning, t=tool_calls)


def _usage(in_tok: int = 0, out_tok: int = 0):
    class U:
        def __init__(self, p, c):
            self.prompt_tokens = p
            self.completion_tokens = c

    return U(in_tok, out_tok)


def _chunk(delta, finish=None, usage=None):
    """用 delta 构造带 .choices 的 chunk。"""

    class Choice:
        def __init__(self, d, fr=None):
            self.delta = d
            self.finish_reason = fr

    class Chunk:
        def __init__(self, choice, u=None):
            self.choices = [choice]
            self.usage = u

    return Chunk(Choice(delta, fr=finish), u=usage)


async def _collect(client, messages=None) -> list[ChatChunk]:
    """运行一次 chat() 并收集所有 ChatChunk。"""
    return [
        c
        async for c in client.chat(
            messages or [Message(role="user", content="x")], stream=True
        )
    ]


async def _collect_non_stream(client, messages=None) -> list[ChatChunk]:
    """运行一次 chat(stream=False) 并收集所有 ChatChunk。"""
    return [
        c
        async for c in client.chat(
            messages or [Message(role="user", content="x")], stream=False
        )
    ]


def _text_deltas(results: list[ChatChunk]) -> list[tuple[str, str]]:
    """提取所有文本增量为 (kind, content) 列表。"""
    out = []
    for c in results:
        for d in c.text_deltas:
            out.append((d.kind.value, d.content))
    return out


# ============================================================
# 1. Client 解析：按推理模式分离 reasoning / response
# ============================================================

async def test_none_mode_plain_content():
    """none 模式：content 原样归为 response。"""
    client = _FakeClient([_chunk(_delta(content="你好"))])
    results = await _collect(client)
    assert _text_deltas(results) == [("response", "你好")]


async def test_native_mode_reasoning_content():
    """native 模式：reasoning_content 归 reasoning，content 归 response。"""
    client = _FakeClient(
        [_chunk(_delta(reasoning="我在思考", content="答案"))],
        policy=ReasoningPolicy(mode=ReasoningMode.NATIVE),
    )
    results = await _collect(client)
    assert _text_deltas(results) == [("reasoning", "我在思考"), ("response", "答案")]


async def test_native_mode_same_chunk_dual_fields():
    """native 模式：同包 reasoning + content 固定为 reasoning 在前、response 在后。"""
    client = _FakeClient(
        [_chunk(_delta(reasoning="r1", content="p1")), _chunk(_delta(content="p2"))],
        policy=ReasoningPolicy(mode=ReasoningMode.NATIVE),
    )
    results = await _collect(client)
    assert _text_deltas(results) == [
        ("reasoning", "r1"),
        ("response", "p1"),
        ("response", "p2"),
    ]


async def test_tags_mode_label_parsing():
    """tags 模式：按标签切分 reasoning 与 response。"""
    client = _FakeClient(
        [_chunk(_delta(content="<think>想一下</think>回答"))],
        policy=ReasoningPolicy(mode=ReasoningMode.TAGS),
    )
    results = await _collect(client)
    assert _text_deltas(results) == [("reasoning", "想一下"), ("response", "回答")]


async def test_none_mode_does_not_parse_tags():
    """模式不自动猜测：none 模式不识别标签，整段作为 response。"""
    client = _FakeClient([_chunk(_delta(content="<think>x</think>y"))])
    results = await _collect(client)
    assert _text_deltas(results) == [("response", "<think>x</think>y")]


async def test_usage_only_chunk_accumulates():
    """usage-only chunk 不产生文本，token 用量累积到末尾结束包。"""
    client = _FakeClient([_chunk(_delta(), usage=_usage(in_tok=10, out_tok=5))])
    results = await _collect(client)
    # 中间无文本增量
    assert _text_deltas(results) == []
    assert results[-1].usage is not None
    assert results[-1].usage.input_tokens == 10
    assert results[-1].usage.output_tokens == 5


async def test_finish_only_chunk_no_text():
    """finish-only chunk 不产生文本增量，结束包携带 finish_reason。"""
    client = _FakeClient([_chunk(_delta(), finish="stop")])
    results = await _collect(client)
    assert _text_deltas(results) == []
    assert results[-1].finish_reason == "stop"


async def test_multiple_tool_calls_written_once():
    """多个完成的工具调用在结束包一次性写入同一 ChatChunk.tool_calls。"""
    chunk = _chunk(
        _delta(
            tc=[
                {"i": 0, "id": "c1", "n": "get_a", "a": '{"x":1}'},
                {"i": 1, "id": "c2", "n": "get_b", "a": '{"y":2}'},
            ]
        ),
        finish="stop",
    )
    client = _FakeClient([chunk])
    results = await _collect(client)
    tool_chunks = [c for c in results if c.tool_calls]
    # 仅一个 chunk 承载工具调用，且包含两个
    assert len(tool_chunks) == 1
    assert len(tool_chunks[0].tool_calls) == 2
    names = {tc.name for tc in tool_chunks[0].tool_calls}
    assert names == {"get_a", "get_b"}


async def test_cross_chunk_tool_args_concatenated():
    """跨 chunk 的工具调用参数正确拼接，结束包写出完整 JSON。"""
    chunks = [
        _chunk(_delta(tc=[{"i": 0, "id": "c1", "n": "get_t", "a": '{"cit'}])),
        _chunk(_delta(tc=[{"i": 0, "a": 'y":"北京"}'}])),
        _chunk(_delta(), finish="stop"),
    ]
    client = _FakeClient(chunks)
    results = await _collect(client)
    tcs = [tc for c in results for tc in c.tool_calls if tc.name]
    assert len(tcs) == 1
    assert tcs[0].name == "get_t"
    # 参数跨 chunk 拼接后应为合法 JSON
    assert tcs[0].arguments == '{"city":"北京"}'


# ============================================================
# 2. 两个交错 chat()：请求级状态互不串线
# ============================================================

async def test_isolated_interleaved_calls():
    """同一 Client 实例的两次 chat() 各自持有独立状态，互不串线。"""
    client = _SeqClient(
        [
            _MockAPIResponse(
                [
                    _chunk(_delta(tc=[{"i": 0, "id": "a1", "n": "get_a", "a": "{}"}]), finish="stop"),
                ]
            ),
            _MockAPIResponse(
                [
                    _chunk(_delta(tc=[{"i": 0, "id": "b1", "n": "get_b", "a": "{}"}]), finish="length"),
                ]
            ),
        ]
    )
    # 调用 A
    res_a = await _collect(client)
    tcs_a = [tc for c in res_a for tc in c.tool_calls if tc.name]
    assert [tc.name for tc in tcs_a] == ["get_a"]
    assert res_a[-1].finish_reason == "stop"
    # 调用 B（应完全独立，不被 A 的状态污染）
    res_b = await _collect(client)
    tcs_b = [tc for c in res_b for tc in c.tool_calls if tc.name]
    assert [tc.name for tc in tcs_b] == ["get_b"]
    assert res_b[-1].finish_reason == "length"


async def test_interleaved_exception_does_not_pollute():
    """一个调用流中断异常，不影响同实例后续调用的解析状态。"""
    client = _SeqClient(
        [
            # 调用 A：先产出一段文本，再流中断
            _FailAfterFirstResponse(_chunk(_delta(content="中断前"))),
            # 调用 B：正常完整输出
            _MockAPIResponse(
                [_chunk(_delta(content="正常")), _chunk(_delta(), finish="stop")]
            ),
        ]
    )
    # 调用 A 抛异常
    with pytest.raises(RuntimeError):
        await _collect(client)
    # 调用 B 不受影响，仍正确产出
    res_b = await _collect(client)
    assert _text_deltas(res_b) == [("response", "正常")]


# ============================================================
# 3. Proxy 边界：可见输出决定是否降级
# ============================================================

class _ToolThenFailClient:
    """先产出一个工具调用 chunk（无可见文本），随后流中断。"""

    async def chat(self, messages, tools=None, stream=True):
        yield ChatChunk(tool_calls=(ToolCall(id="c1", name="get_t", arguments="{}"),))
        raise RuntimeError("broke")


class _TextThenFailClient:
    """先产出可见 response 文本，随后流中断。"""

    async def chat(self, messages, tools=None, stream=True):
        yield ChatChunk(text_deltas=(ChatTextDelta(TextDeltaKind.RESPONSE, "seen"),))
        raise RuntimeError("broke")


class _OkClient:
    """正常产出可见文本并结束。"""

    async def chat(self, messages, tools=None, stream=True):
        yield ChatChunk(
            text_deltas=(ChatTextDelta(TextDeltaKind.RESPONSE, "ok"),),
            finish_reason="stop",
        )


class _DowngradeRouter:
    """primary 流中断、fallback 正常。"""

    def __init__(self):
        self.failed: list[str] = []
        self.succeeded: list[str] = []

    def select(self, purpose="chat", forced_model=None):
        return ["primary", "fallback"]

    def get_client(self, model_name):
        return _ToolThenFailClient() if model_name == "primary" else _OkClient()

    def get_provider_name(self, model_name):
        return "qwen"

    async def try_acquire(self, provider, timeout):
        return None

    def report_success(self, model_name):
        self.succeeded.append(model_name)

    def report_failure(self, model_name):
        self.failed.append(model_name)

    def _get_retry_config(self, model_name):
        return 1

    def _get_backoff_config(self, model_name):
        return 0.01


class _SpyRouter:
    """只暴露一个候选，用于验证“已可见输出后不降级”。"""

    def __init__(self):
        self.attempted: list[str] = []

    def select(self, purpose="chat", forced_model=None):
        return ["only"]

    def get_client(self, model_name):
        self.attempted.append(model_name)
        return _TextThenFailClient()

    def get_provider_name(self, model_name):
        return "qwen"

    async def try_acquire(self, provider, timeout):
        return None

    def report_success(self, model_name):
        pass

    def report_failure(self, model_name):
        pass

    def _get_retry_config(self, model_name):
        return 1

    def _get_backoff_config(self, model_name):
        return 0.01


async def test_proxy_no_visible_output_falls_back():
    """无可见输出即失败：Proxy 允许降级到下一个候选。"""
    router = _DowngradeRouter()
    proxy = LLMProxy(model_router=router)
    text = "".join(
        [
            delta.content
            async for chunk in proxy.chat(
                [Message(role="user", content="hi")], purpose="chat", stream=False
            )
            for delta in chunk.text_deltas
        ]
    )
    assert "ok" in text
    assert router.failed == ["primary"]
    assert router.succeeded == ["fallback"]


async def test_proxy_visible_output_no_fallback():
    """已展示 reasoning/response 后失败：Proxy 不可降级，直接抛出。"""
    router = _SpyRouter()
    proxy = LLMProxy(model_router=router)
    with pytest.raises(NonRetryableStreamError):
        async for _ in proxy.chat([Message(role="user", content="x")], purpose="chat"):
            pass
    # 仅尝试一次，未切换其它候选
    assert router.attempted == ["only"]


class _RetryClient:
    """记录调用条件并在首包阶段失败的替身。"""

    def __init__(self) -> None:
        self.options: list[tuple[float | None, int | None]] = []

    async def chat(self, messages, tools=None, stream=True, timeout_seconds=None, retry_count=None):
        self.options.append((timeout_seconds, retry_count))
        raise RuntimeError("setup failed")
        yield ChatChunk()


class _RetryRouter(_SpyRouter):
    """使用单一失败客户端，验证调用级重试覆盖默认配置。"""

    def __init__(self) -> None:
        super().__init__()
        self.client = _RetryClient()

    def get_client(self, model_name):
        self.attempted.append(model_name)
        return self.client


async def test_proxy_passes_explicit_conditions_and_uses_extra_retry_count():
    """调用条件必须传至客户端，retry_count 表示额外重试而非总尝试次数。"""
    router = _RetryRouter()
    proxy = LLMProxy(router)

    with pytest.raises(RuntimeError):
        async for _ in proxy.chat(
            [Message(role="user", content="retry")],
            timeout_seconds=0.25,
            retry_count=2,
        ):
            pass

    assert router.client.options == [(0.25, 2), (0.25, 2), (0.25, 2)]


# ============================================================
# 4. 非流式分支（stream=False）也必须按推理模式分离
# ============================================================

async def test_tags_mode_non_stream_label_parsing():
    """tags 模式 + stream=False：复用同一解析器，标签不泄漏、分类正确。"""
    client = _FakeNonStreamClient(
        content="<think>想一下</think>回答",
        policy=ReasoningPolicy(mode=ReasoningMode.TAGS),
    )
    results = await _collect_non_stream(client)
    assert _text_deltas(results) == [("reasoning", "想一下"), ("response", "回答")]
    # 协议标签本身不得出现在任何文本增量中
    assert all(
        "<think>" not in d.content and "</think>" not in d.content
        for c in results
        for d in c.text_deltas
    )


async def test_none_mode_non_stream_does_not_parse_tags():
    """none 模式 + stream=False：不识别标签，整段作为 response。"""
    client = _FakeNonStreamClient(content="<think>x</think>y")
    results = await _collect_non_stream(client)
    assert _text_deltas(results) == [("response", "<think>x</think>y")]


async def test_native_mode_non_stream_reasoning_content():
    """native 模式 + stream=False：reasoning_content 归 reasoning，content 归 response。"""
    client = _FakeNonStreamClient(
        content="答案",
        reasoning_content="我在思考",
        policy=ReasoningPolicy(mode=ReasoningMode.NATIVE),
    )
    results = await _collect_non_stream(client)
    assert _text_deltas(results) == [("reasoning", "我在思考"), ("response", "答案")]


# ============================================================
# 5. 请求超时与 SDK stream 释放
# ============================================================

class _ControlledResponse:
    """可控制下一项到达时间且记录关闭次数的 SDK stream 替身。"""

    def __init__(
        self,
        chunks,
        wait_after_chunks: bool = False,
        close_error: bool = False,
        iteration_error: Exception | None = None,
    ):
        self._chunks = iter(chunks)
        self._wait_after_chunks = wait_after_chunks
        self._close_error = close_error
        self._iteration_error = iteration_error
        self._release = asyncio.Event()
        self.close_count = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration:
            if self._wait_after_chunks:
                await self._release.wait()
            if self._iteration_error is not None:
                raise self._iteration_error
            raise StopAsyncIteration

    async def close(self) -> None:
        self.close_count += 1
        if self._close_error:
            raise RuntimeError("close failed")


class _ObservedClient(OpenAIChatCompletionsClient):
    """记录 OpenAI 请求参数并返回可控 SDK stream 的测试客户端。"""

    def __init__(self, response: _ControlledResponse):
        super().__init__()
        self.response = response
        self.request_params = {}

    def _get_api_key(self) -> str:
        return "test"

    def _get_base_url(self) -> str:
        return "https://test/v1"

    def _get_model_id(self) -> str:
        return "test-model"

    def _get_client(self):
        owner = self

        class F:
            class chat:
                class completions:
                    @staticmethod
                    async def create(**kwargs):
                        owner.request_params = kwargs
                        return owner.response

        return F()


async def test_explicit_timeout_reaches_sdk_and_normal_stream_is_closed():
    """显式时间预算应传给 SDK 请求，并在正常结束后关闭流。"""
    response = _ControlledResponse([_chunk(_delta(content="ok"))])
    client = _ObservedClient(response)

    results = await _collect(client)

    assert _text_deltas(results) == [("response", "ok")]
    assert response.close_count == 1
    assert client.request_params["timeout"].connect == 60.0
    assert client.request_params["timeout"].read == 60.0
    assert client.request_params["timeout"].write == 60.0
    assert client.request_params["timeout"].pool == 60.0

    response = _ControlledResponse([_chunk(_delta(content="ok"))])
    client = _ObservedClient(response)
    await _collect_explicit_timeout(client, 0.2)
    assert client.request_params["timeout"].connect == 0.2
    assert client.request_params["timeout"].read == 0.2
    assert client.request_params["timeout"].write == 0.2
    assert client.request_params["timeout"].pool == 0.2


async def _collect_explicit_timeout(
    client: OpenAIChatCompletionsClient,
    timeout_seconds: float,
):
    """消费指定调用预算下的完整测试流。"""
    return [
        chunk
        async for chunk in client.chat(
            [Message(role="user", content="hi")], timeout_seconds=timeout_seconds
        )
    ]


async def test_first_chunk_timeout_closes_stream():
    """首个有效包超时必须结束等待并关闭 SDK stream。"""
    response = _ControlledResponse([], wait_after_chunks=True)
    client = _ObservedClient(response)

    with pytest.raises(TimeoutError):
        await _collect_explicit_timeout(client, 0.02)

    assert response.close_count == 1


async def test_stream_idle_timeout_closes_stream():
    """首包后 SSE 空闲超时也必须关闭 SDK stream。"""
    response = _ControlledResponse([_chunk(_delta(content="first"))], wait_after_chunks=True)
    client = _ObservedClient(response)
    iterator = client.chat([Message(role="user", content="hi")], timeout_seconds=0.02)

    first = await anext(iterator)
    assert _text_deltas([first]) == [("response", "first")]
    with pytest.raises(TimeoutError):
        await anext(iterator)

    assert response.close_count == 1


async def test_cancelling_stream_closes_sdk_response():
    """协程取消应经过 finally 关闭 SDK stream，而不是留下挂起连接。"""
    response = _ControlledResponse([], wait_after_chunks=True)
    client = _ObservedClient(response)
    task = asyncio.create_task(_collect_explicit_timeout(client, 1.0))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert response.close_count == 1


async def test_stream_close_failure_does_not_mask_iteration_error():
    """关闭失败只能记录，流迭代原始异常必须仍由调用方接收。"""
    response = _ControlledResponse(
        [_chunk(_delta(content="first"))],
        close_error=True,
        iteration_error=ValueError("stream broke"),
    )
    client = _ObservedClient(response)

    with pytest.raises(ValueError, match="stream broke"):
        await _collect_explicit_timeout(client, 0.2)

    assert response.close_count == 1
