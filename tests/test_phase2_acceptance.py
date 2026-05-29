"""
Phase 2 验收测试 — 7 个场景（优先级制路由）

运行方式:
    cd D:/dev/dotClaw
    python tests/test_phase2_acceptance.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotclaw.llm.base import ChatChunk, Message, ToolCall
from dotclaw.llm.openai_compat import OpenAICompatibleClient
from dotclaw.common.rate_limiter import RateLimiter, RateLimitConfig
from dotclaw.llm.proxy import LLMProxy, CallSetupError, NonRetryableStreamError
from dotclaw.config.settings import (
    RouterConfig, DefaultsConfig, ProviderConfig, ProviderRetryConfig,
    ModelConfig, PurposeConfig, PurposePriority,
)

logging.getLogger("dotclaw.llm").setLevel(logging.WARNING)


# ============================================================
# 测试辅助
# ============================================================

def _make_minimal_router_config(
    models: dict | None = None,
    providers: dict | None = None,
    priorities: list | None = None,
    defaults: dict | None = None,
) -> RouterConfig:
    """构建最小测试用 RouterConfig（优先级制）"""
    return RouterConfig(
        defaults=DefaultsConfig(
            provider=defaults.get("provider", "qwen") if defaults else "qwen",
            model=defaults.get("model", "qwen3.7-max") if defaults else "qwen3.7-max",
            parameters={},
            fallback_enabled=True,
        ),
        providers=providers or {
            "qwen": ProviderConfig(
                api_key="test-key",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                retry=ProviderRetryConfig(max_attempts=1, backoff_factor=0.01),
            ),
        },
        models=models or {
            "qwen3.7-max": ModelConfig(provider="qwen", model_id="qwen3.7-max"),
            "qwen-turbo": ModelConfig(provider="qwen", model_id="qwen-turbo"),
        },
        purposes={
            "chat": PurposeConfig(
                description="test",
                priority=priorities or [
                    PurposePriority(model="qwen3.7-max", priority=1),
                    PurposePriority(model="qwen-turbo", priority=2),
                ],
            ),
        },
    )


# ============================================================
# 场景 1：OpenAICompatibleClient 等价性
# ============================================================

class _MockAPIResponse:
    def __init__(self, chunks): self._chunks = chunks
    def __aiter__(self): self._iter = iter(self._chunks); return self
    async def __anext__(self):
        try: return next(self._iter)
        except StopIteration: raise StopAsyncIteration


class _TestClient(OpenAICompatibleClient):
    def __init__(self, mock_chunks):
        super().__init__()
        self._mock_chunks = mock_chunks
    def _get_api_key(self) -> str: return "test"
    def _get_base_url(self) -> str: return "https://test/v1"
    def _get_model_id(self) -> str: return "test-model"
    def _get_client(self):
        class F:  # noqa
            class chat:
                class completions:
                    @staticmethod
                    async def create(**kw): return _MockAPIResponse(self._mock_chunks)
        return F()


def _chunk(content="", tc=None, finish=None):
    """构造 OpenAI SSE chunk（带 .choices 属性）"""
    class Df:
        def __init__(self, n=None, a=None): self.name = n; self.arguments = a
    class Dtc:
        def __init__(self, i=0, id=None, f=None): self.index = i; self.id = id; self.function = f
    class D:
        def __init__(self, c=None, t=None): self.content = c; self.tool_calls = t
    class Choice:
        def __init__(self, d, fr=None): self.delta = d; self.finish_reason = fr

    tcs = None
    if tc:
        tcs = [Dtc(i=t.get("i", 0), id=t.get("id"), f=Df(n=t.get("n"), a=t.get("a"))) for t in tc]

    class Chunk:
        def __init__(self, choice): self.choices = [choice]
    return Chunk(Choice(D(c=content, t=tcs), fr=finish))


async def test_1_equivalence():
    print("\n=== 场景 1：OpenAICompatibleClient 等价性 ===")
    chunks = [
        _chunk(content="你好"),
        _chunk(content="，"),
        _chunk(tc=[{"i": 0, "id": "c1", "n": "get_t", "a": '{"cit'}]),
        _chunk(tc=[{"i": 0, "a": 'y":"北京"}'}]),
        _chunk(finish="stop"),
    ]
    client = _TestClient(chunks)
    results = [c async for c in client.chat([Message(role="user", content="x")], stream=True)]
    contents = "".join(c.content for c in results if c.content)
    tcs = [c.tool_call for c in results if c.tool_call and c.tool_call.name]
    assert contents == "你好，"
    assert len(tcs) == 1 and tcs[0].name == "get_t"
    assert json.loads(tcs[0].arguments) == {"city": "北京"}
    assert results[-1].is_final
    print(f"  ✅ 文本 '{contents}', tool_call {tcs[0].name}({tcs[0].arguments})")


# ============================================================
# 场景 2：优先级制选择（确定性）
# ============================================================

async def test_2_priority():
    print("\n=== 场景 2：优先级制选择 ===")
    config = _make_minimal_router_config(
        priorities=[
            PurposePriority(model="qwen-turbo", priority=1),
            PurposePriority(model="qwen3.7-max", priority=2),
        ],
    )
    from dotclaw.llm.model_router import ModelRouter
    router = ModelRouter(config)

    # 确定性选择：总是返回 priority 最小的 active model
    for _ in range(100):
        _, m = router.resolve(purpose="chat")
        assert m == "qwen-turbo", f"应始终选 priority=1 的 qwen-turbo，实际: {m}"

    # 降级链按 priority 升序排列
    chain = router.get_fallback_chain("chat")
    assert chain == ["qwen-turbo", "qwen3.7-max"], f"降级链应为 priority 升序: {chain}"

    print(f"  ✅ 100 次 resolve 全部返回 qwen-turbo（priority=1）")
    print(f"  ✅ 降级链: {chain}")


# ============================================================
# 场景 3：Proxy 降级链（实际 API，按优先级依次降级）
# ============================================================

async def test_3_fallback_actual():
    print("\n=== 场景 3：Proxy 降级链（实际 API） ===")
    import os
    api_key = os.environ.get("QWEN_API_KEY", os.environ.get("DOTCLAW_API_KEY", ""))
    if not api_key:
        print("  ⚠️ API Key 未设置，跳过")
        return

    from dotclaw.llm.model_router import ModelRouter

    config = RouterConfig(
        defaults=DefaultsConfig(provider="qwen", model="test-fail",
                                parameters={}, fallback_enabled=True),
        providers={
            "qwen": ProviderConfig(
                api_key=api_key,
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                retry=ProviderRetryConfig(max_attempts=1, backoff_factor=0.01),
            ),
            "qwen-fail": ProviderConfig(
                api_key=api_key,
                base_url="https://invalid-host-99999.example.com/v1",
                rate_limit={"requests_per_minute": 0},
                retry=ProviderRetryConfig(max_attempts=1, backoff_factor=0.01),
            ),
        },
        models={
            "test-fail": ModelConfig(provider="qwen-fail", model_id="qwen3.7-max",
                                     context_window=32000, capabilities=["chat"], status="active"),
            "qwen-turbo": ModelConfig(provider="qwen", model_id="qwen-turbo",
                                      context_window=1000000, capabilities=["chat"], status="active"),
        },
        purposes={
            "chat": PurposeConfig(
                description="test",
                priority=[
                    PurposePriority(model="test-fail", priority=1),
                    PurposePriority(model="qwen-turbo", priority=2),
                ],
            ),
        },
    )

    router = ModelRouter(config)
    proxy = LLMProxy(model_router=router, rate_limiter=RateLimiter({}))
    chunks = [c.content async for c in proxy.chat(
        [Message(role="user", content="hi")], purpose="chat", stream=False
    )]
    response = "".join(chunks)
    assert len(response) > 0
    print(f"  ✅ 降级成功: {response[:80]}...")


# ============================================================
# 场景 4：forced_model 三层匹配
# ============================================================

async def test_4_forced_model():
    print("\n=== 场景 4：forced_model 三层匹配 ===")
    config = _make_minimal_router_config(
        defaults={"provider": "qwen", "model": "qwen3.7-max"},
        providers={
            "qwen": ProviderConfig(api_key="k", base_url="http://qwen"),
            "deepseek": ProviderConfig(api_key="k", base_url="http://ds"),
        },
        models={
            "qwen3.7-max": ModelConfig(provider="qwen", model_id="qwen3.7-max"),
            "qwen-turbo": ModelConfig(provider="qwen", model_id="qwen-turbo"),
            "deepseek-v3": ModelConfig(provider="deepseek", model_id="deepseek-chat"),
        },
    )
    from dotclaw.llm.model_router import ModelRouter
    router = ModelRouter(config)

    _, m1 = router.resolve(purpose="chat", forced_model="qwen3.7-max")
    assert m1 == "qwen3.7-max"
    _, m2 = router.resolve(purpose="chat", forced_model="deepseek")
    assert m2 == "deepseek-v3"
    _, m3 = router.resolve(purpose="chat", forced_model="nonexistent")
    assert m3 == "qwen3.7-max"  # fallback to default

    print(f"  ✅ 精确: qwen3.7-max → {m1}")
    print(f"  ✅ 前缀: deepseek → {m2}")
    print(f"  ✅ 降级: nonexistent → {m3}")


# ============================================================
# 场景 5：RateLimiter
# ============================================================

async def test_5_rate_limiter():
    print("\n=== 场景 5：RateLimiter ===")
    rl = RateLimiter({"qwen": RateLimitConfig(requests_per_minute=0)})
    t0 = time.monotonic()
    for _ in range(3): await rl.acquire("qwen")
    assert time.monotonic() - t0 < 0.5

    rl2 = RateLimiter({"qwen": RateLimitConfig(requests_per_minute=3)})
    orig = asyncio.sleep
    called = []
    async def fake(s): called.append(s)  # noqa
    asyncio.sleep = fake
    try:
        for _ in range(5): await rl2.acquire("qwen")
    finally:
        asyncio.sleep = orig
    assert len(called) >= 1
    print(f"  ✅ 不限流立即返回, 限流触发 {len(called)} 次 sleep")


# ============================================================
# 场景 6：流式中途不降级
# ============================================================

async def test_6_stream_no_fallback():
    print("\n=== 场景 6：Proxy 流式中途不降级 ===")
    class FailClient:
        async def chat(self, messages, tools=None, stream=True):
            yield ChatChunk(content="a")
            yield ChatChunk(content="b")
            raise RuntimeError("broke")

    class SpyRouter:
        def __init__(self):
            self._config = _make_minimal_router_config(
                models={"m": ModelConfig(provider="qwen", model_id="m")},
                priorities=[PurposePriority(model="m", priority=1)],
            )
        def resolve(self, p="chat", fm=None): return FailClient(), "m"
        def get_fallback_chain(self, p="chat"): return ["m"]
        def get_available_models(self): return ["m"]
        def _get_or_create_client(self, n): return FailClient()

    p = LLMProxy(model_router=SpyRouter(), rate_limiter=RateLimiter({}))
    try:
        async for _ in p.chat([Message(role="user", content="x")], purpose="chat"):
            pass
        assert False
    except (NonRetryableStreamError, RuntimeError):
        pass
    print(f"  ✅ 流式中途异常 → 不降级")


# ============================================================
# 场景 7：调用前失败降级
# ============================================================

async def test_7_setup_fallback():
    print("\n=== 场景 7：Proxy 调用前失败降级 ===")
    class FailClient:
        async def chat(self, messages, tools=None, stream=True):
            raise CallSetupError("refused")

    class OkClient:
        async def chat(self, messages, tools=None, stream=True):
            yield ChatChunk(content="ok", is_final=True)

    class SpyRouter:
        def __init__(self):
            self._config = _make_minimal_router_config(
                models={
                    "fail": ModelConfig(provider="qwen", model_id="f"),
                    "ok": ModelConfig(provider="qwen", model_id="o"),
                },
                priorities=[
                    PurposePriority(model="fail", priority=1),
                    PurposePriority(model="ok", priority=2),
                ],
            )
        def resolve(self, p="chat", fm=None): return FailClient(), "fail"
        def get_fallback_chain(self, p="chat"): return ["fail", "ok"]
        def get_available_models(self): return ["fail", "ok"]
        def _get_or_create_client(self, n):
            return OkClient() if n == "ok" else FailClient()

    p = LLMProxy(model_router=SpyRouter(), rate_limiter=RateLimiter({}))
    chunks = [c.content async for c in p.chat(
        [Message(role="user", content="x")], purpose="chat", model="fail"
    )]
    r = "".join(chunks)
    assert "ok" in r
    print(f"  ✅ 降级成功: {r}")


# ============================================================
# 运行入口
# ============================================================

async def main_async():
    tests = [
        ("场景1-OpenAICompatClient等价性", test_1_equivalence),
        ("场景2-优先级制选择", test_2_priority),
        ("场景3-Proxy降级链(实际API)", test_3_fallback_actual),
        ("场景4-forced_model三层匹配", test_4_forced_model),
        ("场景5-RateLimiter", test_5_rate_limiter),
        ("场景6-Proxy流式不降级", test_6_stream_no_fallback),
        ("场景7-Proxy调用前失败降级", test_7_setup_fallback),
    ]
    passed, failed = 0, 0
    failures = []
    for name, func in tests:
        print(f"\n{'='*60}")
        try:
            await func()
            passed += 1
            print(f"\n✅ {name} — 通过")
        except AssertionError as e:
            failed += 1; failures.append((name, str(e)))
            print(f"\n❌ {name}: {e}")
        except Exception as e:
            failed += 1; failures.append((name, str(e)))
            print(f"\n💥 {name}: {e}")
            import traceback; traceback.print_exc()

    print(f"\n{'='*60}")
    print(f"结果: {passed}/{len(tests)} 通过")
    if failures:
        for n, e in failures: print(f"  ❌ {n}: {e[:150]}")


def main():
    asyncio.run(main_async())

if __name__ == "__main__":
    main()
