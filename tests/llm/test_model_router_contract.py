"""
Phase 2 验收测试 — 7 个场景（优先级制路由 + 熔断器 + 限流）

运行方式:
    cd D:/dev/dotClaw
    python tests/test_phase2_acceptance.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import pytest


from dotclaw.llm.base import ChatChunk, ChatTextDelta, Message, TextDeltaKind, ToolCall
from dotclaw.llm.openai_compat import OpenAICompatibleClient
from dotclaw.llm.rate_limiter import RateLimiter, RateLimitConfig, RateLimitTimeout
from dotclaw.llm.circuit_breaker import CircuitBreaker, BreakerConfig
from dotclaw.llm.model_router import ModelRouter
from dotclaw.llm.proxy import LLMProxy, CallSetupError, NonRetryableStreamError
from dotclaw.config.settings import (
    RouterConfig, DefaultsConfig, ProviderConfig, ProviderRetryConfig,
    ModelConfig, PurposeConfig, PurposePriority,
)

logging.getLogger("dotclaw.llm").setLevel(logging.WARNING)
logging.getLogger("dotclaw.llm.router").setLevel(logging.WARNING)

pytestmark = pytest.mark.asyncio


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


def _make_router(config: RouterConfig) -> ModelRouter:
    """构建带默认 RateLimiter + CircuitBreaker 的 ModelRouter。"""
    rl = RateLimiter({})
    cb = CircuitBreaker({})
    return ModelRouter(config, rl, cb)


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
    contents = "".join(d.content for c in results for d in c.text_deltas)
    tcs = [tc for c in results for tc in c.tool_calls if tc.name]
    assert contents == "你好，"
    assert len(tcs) == 1 and tcs[0].name == "get_t"
    assert json.loads(tcs[0].arguments) == {"city": "北京"}
    assert results[-1].finish_reason is not None
    print(f"  ✅ 文本 '{contents}', tool_call {tcs[0].name}({tcs[0].arguments})")


# ============================================================
# 场景 2：优先级制选择（select() 接口）
# ============================================================

async def test_2_priority():
    print("\n=== 场景 2：优先级制选择（select()） ===")
    config = _make_minimal_router_config(
        priorities=[
            PurposePriority(model="qwen-turbo", priority=1),
            PurposePriority(model="qwen3.7-max", priority=2),
        ],
    )
    router = _make_router(config)

    # select() 返回按 priority 升序排列的候选列表
    for _ in range(10):
        candidates = router.select(purpose="chat")
        assert candidates[0] == "qwen-turbo", (
            f"应始终优先选 priority=1 的 qwen-turbo，实际: {candidates[0]}"
        )
        assert len(candidates) == 2

    print(f"  ✅ select() 始终返回 [qwen-turbo, qwen3.7-max]")
    print(f"  ✅ 候选列表: {router.select('chat')}")


# ============================================================
# 场景 3：Proxy 降级链（实际 API）
# ============================================================

async def test_3_fallback_after_setup_failure():
    """首个候选在输出前失败时，Proxy 应切换到下一个候选。"""
    class FailingClient:
        async def chat(self, messages, tools=None, stream=True):
            if False:
                yield ChatChunk()
            raise CallSetupError("连接被拒绝")

    class SuccessfulClient:
        async def chat(self, messages, tools=None, stream=True):
            yield ChatChunk(text_deltas=(ChatTextDelta(TextDeltaKind.RESPONSE, "fallback"),), finish_reason="stop")

    class Router:
        def __init__(self):
            self.failed: list[str] = []
            self.succeeded: list[str] = []

        def select(self, purpose="chat", forced_model=None):
            return ["primary", "fallback"]

        def get_client(self, model_name):
            return FailingClient() if model_name == "primary" else SuccessfulClient()

        def get_provider_name(self, model_name):
            return "test"

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

    router = Router()
    proxy = LLMProxy(model_router=router)
    response = "".join([
        delta.content
        async for chunk in proxy.chat(
            [Message(role="user", content="hi")],
            purpose="chat",
            stream=False,
        )
        for delta in chunk.text_deltas
    ])

    assert response == "fallback"
    assert router.failed == ["primary"]
    assert router.succeeded == ["fallback"]


# ============================================================
# 场景 4：forced_model 三层匹配（via select()）
# ============================================================

async def test_4_forced_model():
    print("\n=== 场景 4：forced_model 三层匹配（via select()） ===")
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
    router = _make_router(config)

    # 精确匹配：forced_model 排在候选列表第一位
    c1 = router.select(purpose="chat", forced_model="qwen3.7-max")
    assert c1[0] == "qwen3.7-max"

    # provider 匹配：该 provider 的所有模型排在最前面
    c2 = router.select(purpose="chat", forced_model="deepseek")
    assert c2[0] == "deepseek-v3"  # deepseek provider 下唯一的 active model

    # 不匹配：保持原顺序
    c3 = router.select(purpose="chat", forced_model="nonexistent")
    assert c3[0] == "qwen3.7-max"  # 默认 priority=1

    print(f"  ✅ 精确: qwen3.7-max → candidate 首位: {c1}")
    print(f"  ✅ 前缀: deepseek → 首位: {c2[0]}")
    print(f"  ✅ 降级: nonexistent → 保持 priority 序: {c3}")


# ============================================================
# 场景 5：RateLimiter（增强版：check + timeout）
# ============================================================

async def test_5_rate_limiter():
    print("\n=== 场景 5：RateLimiter（check + timeout） ===")
    rl = RateLimiter({"qwen": RateLimitConfig(requests_per_minute=0)})
    t0 = time.monotonic()
    for _ in range(3): await rl.acquire("qwen")
    assert time.monotonic() - t0 < 0.5

    # check(): 无锁近似读
    assert rl.check("qwen")  # 不限流 → True
    assert rl.check("unknown")  # 未配置 → True

    # 消耗令牌后，check() 应报告当前不可立即执行。
    rl2 = RateLimiter({"qwen": RateLimitConfig(requests_per_minute=3)})
    for _ in range(3):
        await rl2.acquire("qwen")
    assert not rl2.check("qwen")
    print("  ✅ 不限流立即返回，令牌耗尽后 check() 正确拒绝")


async def test_5b_rate_limit_timeout():
    print("\n=== 场景 5b：RateLimiter timeout ===")
    rl = RateLimiter({"qwen": RateLimitConfig(requests_per_minute=1)})
    # 消耗唯一令牌
    await rl.acquire("qwen")
    # 下次 acquire 需要等待，但 timeout=0.01 秒太短
    try:
        await rl.acquire("qwen", timeout=0.01)
        assert False, "应超时"
    except RateLimitTimeout:
        pass
    print(f"  ✅ timeout=0.01 秒触发 RateLimitTimeout")


# ============================================================
# 场景 6：流式中途不降级
# ============================================================

async def test_6_stream_no_fallback():
    print("\n=== 场景 6：Proxy 流式中途不降级 ===")
    class FailClient:
        async def chat(self, messages, tools=None, stream=True):
            yield ChatChunk(text_deltas=(ChatTextDelta(TextDeltaKind.RESPONSE, "a"),))
            yield ChatChunk(text_deltas=(ChatTextDelta(TextDeltaKind.RESPONSE, "b"),))
            raise RuntimeError("broke")

    class SpyRouter:
        def select(self, purpose="chat", forced_model=None):
            return ["m"]
        def get_client(self, model_name):
            return FailClient()
        def get_provider_name(self, model_name):
            return "qwen"
        async def try_acquire(self, provider, timeout):
            pass
        def report_success(self, model_name):
            pass
        def report_failure(self, model_name):
            pass
        def _get_retry_config(self, model_name):
            return 1
        def _get_backoff_config(self, model_name):
            return 0.01

    p = LLMProxy(model_router=SpyRouter())
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
            if False:
                yield ChatChunk()
            raise CallSetupError("refused")

    class OkClient:
        async def chat(self, messages, tools=None, stream=True):
            yield ChatChunk(text_deltas=(ChatTextDelta(TextDeltaKind.RESPONSE, "ok"),), finish_reason="stop")

    class SpyRouter:
        def select(self, purpose="chat", forced_model=None):
            return ["fail", "ok"]
        def get_client(self, model_name):
            return OkClient() if model_name == "ok" else FailClient()
        def get_provider_name(self, model_name):
            return "qwen"
        async def try_acquire(self, provider, timeout):
            pass
        def report_success(self, model_name):
            pass
        def report_failure(self, model_name):
            pass
        def _get_retry_config(self, model_name):
            return 1
        def _get_backoff_config(self, model_name):
            return 0.01

    p = LLMProxy(model_router=SpyRouter())
    chunks = [d.content async for c in p.chat(
        [Message(role="user", content="x")], purpose="chat"
    ) for d in c.text_deltas]
    r = "".join(chunks)
    assert "ok" in r
    print(f"  ✅ 降级成功: {r}")


# ============================================================
# 场景 8：CircuitBreaker 状态机（新增）
# ============================================================

async def test_8_circuit_breaker():
    print("\n=== 场景 8：CircuitBreaker 状态机 ===")
    cb = CircuitBreaker({"qwen": BreakerConfig(
        failure_threshold=3, cooldown_seconds=0.1, half_open_max=1,
    )})

    # 初始 CLOSED
    assert not cb.is_open("qwen")
    assert cb.get_state("qwen").value == "closed"

    # 连续失败 → OPEN
    cb.on_failure("qwen")
    cb.on_failure("qwen")
    assert not cb.is_open("qwen")  # 还没到阈值
    cb.on_failure("qwen")
    assert cb.is_open("qwen")      # 触发熔断

    # OPEN → HALF_OPEN（冷却后）
    await asyncio.sleep(0.15)
    assert not cb.is_open("qwen")  # 自动进入 HALF_OPEN
    assert cb.get_state("qwen").value == "half_open"

    # HALF_OPEN 探测成功 → CLOSED
    cb.on_success("qwen")
    assert not cb.is_open("qwen")
    assert cb.get_state("qwen").value == "closed"

    print(f"  ✅ CLOSED → OPEN → HALF_OPEN → CLOSED 状态流转正常")


async def test_8b_circuit_breaker_half_open_failure():
    print("\n=== 场景 8b：HALF_OPEN 探测失败 → OPEN ===")
    cb = CircuitBreaker({"qwen": BreakerConfig(
        failure_threshold=2, cooldown_seconds=0.05, half_open_max=1,
    )})

    # 触发熔断
    cb.on_failure("qwen")
    cb.on_failure("qwen")
    assert cb.is_open("qwen")

    # 冷却后进入 HALF_OPEN
    await asyncio.sleep(0.1)
    assert cb.get_state("qwen").value == "half_open"

    # 探测失败 → 重新 OPEN
    cb.on_failure("qwen")
    assert cb.is_open("qwen")
    print(f"  ✅ HALF_OPEN 探测失败 → 立即回到 OPEN")


# ============================================================
# 运行入口
# ============================================================

async def main_async():
    tests = [
        ("场景1-OpenAICompatClient等价性", test_1_equivalence),
        ("场景2-优先级制选择(select)", test_2_priority),
        ("场景3-Proxy降级链", test_3_fallback_after_setup_failure),
        ("场景4-forced_model三层匹配", test_4_forced_model),
        ("场景5-RateLimiter(check+timeout)", test_5_rate_limiter),
        ("场景5b-RateLimiter-timeout", test_5b_rate_limit_timeout),
        ("场景6-Proxy流式不降级", test_6_stream_no_fallback),
        ("场景7-Proxy调用前失败降级", test_7_setup_fallback),
        ("场景8-CircuitBreaker状态机", test_8_circuit_breaker),
        ("场景8b-CircuitBreaker-半开失败", test_8b_circuit_breaker_half_open_failure),
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
