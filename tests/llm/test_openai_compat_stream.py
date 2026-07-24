"""阶段三：Provider 流标准化与请求级状态隔离测试。

覆盖开发计划 §4 门槛：
- Client 解析：native reasoning_content、普通 content、同包双字段、usage-only、
  finish-only、多个工具调用一次性写出、跨 chunk 工具参数拼接、模式不自动猜测。
- 两个交错 chat()：工具参数 / finish reason / 标签缓冲互不串线，
  一个调用异常不污染另一个。
- Proxy 边界：无可见输出失败可降级、已展示 reasoning/response 后失败不可降级。
"""

from __future__ import annotations

import pytest

from dotclaw.llm.base import ChatChunk, ChatTextDelta, Message, TextDeltaKind, ToolCall
from dotclaw.llm.openai_compat import OpenAICompatibleClient
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


class _FakeClient(OpenAICompatibleClient):
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


class _SeqClient(OpenAICompatibleClient):
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
