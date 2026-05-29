"""令牌桶限流器

支持按 provider 维度的并发控制，asyncio.Lock 保证并发安全。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


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

    async def acquire(self, provider: str) -> None:
        """
        获取一个令牌。若超出速率则 await 等待直到令牌恢复。

        requests_per_minute = 0 时立即返回（不限流）。
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
            self._buckets[provider] = (tokens, now)

            # 计算需要等待的时间（必须在 decrement 之后）
            wait_time = (1.0 - tokens) / refill_rate if tokens < 1.0 else 0

        if wait_time > 0:
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
