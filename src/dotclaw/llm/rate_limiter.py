"""令牌桶限流器

按 provider 维度的并发控制，asyncio.Lock 保证并发安全。

增强：
- check(provider) → bool: 无锁近似读，Router.select() 过滤用
- acquire(provider, timeout): 超时则抛 RateLimitTimeout，触发 Proxy 降级
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


class RateLimitTimeout(Exception):
    """acquire() 等待超时时抛出，被 Proxy 视为 CallSetupError 触发降级。"""
    pass


@dataclass
class RateLimitConfig:
    """单个 provider 的限流配置"""
    requests_per_minute: int = 0  # 0 = 不限流


class RateLimiter:
    """
    令牌桶限流器。

    按 provider 维度独立计数（同一 provider 的所有 model 共享一个令牌桶）。
    使用 asyncio.Lock 保护 refill + consume 复合操作。
    """

    def __init__(self, configs: dict[str, RateLimitConfig]):
        """
        configs: provider_name → RateLimitConfig
        """
        self._configs = configs
        # 每个 provider 一个桶: {provider: (tokens, last_refill_time)}
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = asyncio.Lock()

    # ================================================================
    # check(): 无锁近似读 — Router.select() 过滤用
    # ================================================================

    def check(self, provider: str) -> bool:
        """
        无锁近似检查：provider 当前是否有可用令牌。

        用于 select() 中快速过滤限流过载的 provider。
        不精确（无锁读可能读到过期状态），但不影响正确性
        —— acquire() 是真正的守门员，check() 只是提前过滤。

        返回 True 表示当前可能有令牌（或未配置限流）。
        """
        config = self._configs.get(provider)
        if config is None or config.requests_per_minute <= 0:
            return True

        max_tokens = float(config.requests_per_minute)
        refill_rate = max_tokens / 60.0

        # 无锁读取（近似值）
        now = time.monotonic()
        tokens, last_refill = self._buckets.get(provider, (max_tokens, now))
        elapsed = now - last_refill
        estimated = min(max_tokens, tokens + elapsed * refill_rate)

        return estimated >= 1.0

    # ================================================================
    # acquire(): 真正的令牌获取 — Proxy 调用前执行
    # ================================================================

    async def acquire(self, provider: str, timeout: float | None = None) -> None:
        """
        获取一个令牌。

        - requests_per_minute = 0 时立即返回（不限流）。
        - 超时（timeout 秒后仍无令牌）→ 抛 RateLimitTimeout。
        - timeout=None → 保持旧行为，无限等待直到令牌恢复。
        """
        config = self._configs.get(provider)
        if config is None or config.requests_per_minute <= 0:
            return

        max_tokens = float(config.requests_per_minute)
        refill_rate = max_tokens / 60.0  # tokens per second

        async with self._lock:
            now = time.monotonic()
            tokens, last_refill = self._buckets.get(provider, (max_tokens, now))

            # 补充令牌
            elapsed = now - last_refill
            tokens = min(max_tokens, tokens + elapsed * refill_rate)

            if tokens >= 1.0:
                tokens -= 1.0
                # 本次已成功获取令牌，剩余令牌为零也不应等待下一轮补充。
                wait_time = 0.0
            else:
                wait_time = (1.0 - tokens) / refill_rate
            self._buckets[provider] = (tokens, now)

        if wait_time > 0:
            if timeout is not None and wait_time > timeout:
                raise RateLimitTimeout(
                    f"provider '{provider}' 限流: 需等待 {wait_time:.2f}s, "
                    f"超时 {timeout:.2f}s"
                )
            # 在锁外等待，避免阻塞其他请求
            await asyncio.sleep(wait_time)
            # 等待后重新获取
            await self._acquire_after_wait(provider, config, max_tokens, refill_rate)

    async def _acquire_after_wait(
        self,
        provider: str,
        config: RateLimitConfig,
        max_tokens: float,
        refill_rate: float,
    ) -> None:
        """等待后重新尝试获取令牌"""
        async with self._lock:
            now = time.monotonic()
            tokens, last_refill = self._buckets.get(provider, (max_tokens, now))
            elapsed = now - last_refill
            tokens = min(max_tokens, tokens + elapsed * refill_rate)
            tokens = max(0, tokens - 1.0)
            self._buckets[provider] = (tokens, now)
