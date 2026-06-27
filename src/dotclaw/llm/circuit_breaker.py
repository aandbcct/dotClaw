"""熔断器（Circuit Breaker）

按 provider 维度跟踪失败率，实现：
- CLOSED → OPEN: 连续 N 次失败后熔断
- OPEN → HALF_OPEN: 冷却时间后允许探测
- HALF_OPEN → CLOSED: 探测成功则恢复
- HALF_OPEN → OPEN: 探测失败则重新熔断

配置来自 model_router_config.yaml 的 providers.{name}.circuit_breaker 段。
默认: failure_threshold=5, cooldown_seconds=30, half_open_max=1
"""

from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

logger = logging.getLogger("dotclaw.llm.circuit_breaker")


class BreakerState(str, Enum):
    CLOSED = "closed"       # 正常：允许请求
    OPEN = "open"           # 熔断：拒绝请求
    HALF_OPEN = "half_open" # 半开：允许探测请求


@dataclass
class BreakerConfig:
    """单个 provider 的熔断器配置"""
    failure_threshold: int = 5      # 连续失败 N 次后熔断（0 = 关闭熔断器）
    cooldown_seconds: float = 30.0   # 熔断后冷却时间（秒）
    half_open_max: int = 1          # HALF_OPEN 时允许的探测请求数（默认 1）


class CircuitBreaker:
    """
    按 provider 维度的熔断器。

    每个 provider 独立状态机：
    - 成功调用 → on_success()
    - 失败调用 → on_failure()
    - 路由判断 → is_open(provider)
    - 候选排序 → get_state(provider)

    配置中 failure_threshold=0 可关闭该 provider 的熔断器。
    """

    def __init__(
        self,
        configs: dict[str, BreakerConfig] | None = None,
        on_state_change: Callable[[str, BreakerState, BreakerState, str], None] | None = None,
    ):
        """
        Args:
            configs: provider_name → BreakerConfig（从 RouterConfig 创建）
            on_state_change: 状态变更回调 (provider, old, new, reason)
        """
        self._configs = configs or {}

        # 每个 provider 的状态
        self._states: dict[str, BreakerState] = {}
        self._failures: dict[str, int] = {}   # 当前连续失败计数
        self._opened_at: dict[str, float] = {}  # 进入 OPEN 的时间
        self._half_open_attempts: dict[str, int] = {}  # HALF_OPEN 时的尝试次数

        self._on_state_change = on_state_change

    # ================================================================
    # 公共 API
    # ================================================================

    def is_open(self, provider: str) -> bool:
        """
        检查 provider 是否处于 OPEN 状态（完全拒绝请求）。

        用于 Router.select() 过滤不可用 provider。
        HALF_OPEN 返回 False（允许探测）。
        """
        state = self._get_effective_state(provider)
        return state == BreakerState.OPEN

    def get_state(self, provider: str) -> BreakerState:
        """
        获取 provider 当前的有效状态。

        用于 Router.select() 排序：正常 > HALF_OPEN > OPEN(兜底)。
        """
        return self._get_effective_state(provider)

    def on_success(self, provider: str) -> None:
        """上报一次成功调用。"""
        cfg = self._configs.get(provider)
        if cfg and cfg.failure_threshold == 0:
            return  # 熔断器关闭

        old_state = self._get_effective_state(provider)

        if old_state == BreakerState.HALF_OPEN:
            # HALF_OPEN 探测成功 → 恢复 CLOSED
            self._failures[provider] = 0
            self._half_open_attempts.pop(provider, None)
            self._states[provider] = BreakerState.CLOSED
            self._notify(provider, old_state, BreakerState.CLOSED,
                         f"HALF_OPEN 探测成功，恢复")
        else:
            # CLOSED 状态：重置连续失败计数
            self._failures[provider] = 0

    def on_failure(self, provider: str) -> None:
        """上报一次失败调用。"""
        cfg = self._configs.get(provider)
        if cfg and cfg.failure_threshold == 0:
            return  # 熔断器关闭

        old_state = self._get_effective_state(provider)

        # 累加失败计数
        current = self._failures.get(provider, 0) + 1
        self._failures[provider] = current

        threshold = cfg.failure_threshold if cfg else 5

        if old_state == BreakerState.HALF_OPEN:
            # HALF_OPEN 探测失败 → 立即回到 OPEN
            self._states[provider] = BreakerState.OPEN
            self._opened_at[provider] = time.monotonic()
            self._half_open_attempts.pop(provider, None)
            self._notify(provider, old_state, BreakerState.OPEN,
                         f"HALF_OPEN 探测失败 (连续 {current} 次)")
        elif old_state == BreakerState.CLOSED and current >= threshold:
            # 连续失败达阈值 → 触发熔断
            self._states[provider] = BreakerState.OPEN
            self._opened_at[provider] = time.monotonic()
            self._notify(provider, old_state, BreakerState.OPEN,
                         f"连续 {current} 次失败，触发熔断")

    def try_half_open(self, provider: str) -> bool:
        """
        尝试在 HALF_OPEN 状态下发起一个探测请求。

        返回 True 表示允许探测，False 表示已达探测上限需等待。

        注意：调用方应在探测请求发出**前**调用此方法，
        成功/失败后调用 on_success/on_failure 推进状态。
        """
        cfg = self._configs.get(provider)
        max_attempts = cfg.half_open_max if cfg else 1

        attempts = self._half_open_attempts.get(provider, 0)
        if attempts >= max_attempts:
            return False
        self._half_open_attempts[provider] = attempts + 1
        return True

    # ================================================================
    # 内部方法
    # ================================================================

    def _get_effective_state(self, provider: str) -> BreakerState:
        """
        获取有效状态，自动处理 OPEN → HALF_OPEN 转换。

        如果当前是 OPEN 且已过冷却时间，自动转为 HALF_OPEN。
        """
        explicit = self._states.get(provider, BreakerState.CLOSED)
        if explicit != BreakerState.OPEN:
            return explicit

        cfg = self._configs.get(provider)
        cooldown = cfg.cooldown_seconds if cfg else 30.0

        opened_at = self._opened_at.get(provider, 0)
        elapsed = time.monotonic() - opened_at

        if elapsed >= cooldown:
            old = BreakerState.OPEN
            self._states[provider] = BreakerState.HALF_OPEN
            self._half_open_attempts[provider] = 0
            self._notify(provider, old, BreakerState.HALF_OPEN,
                         f"冷却 {cooldown}s 完成，进入 HALF_OPEN")
            return BreakerState.HALF_OPEN

        return BreakerState.OPEN

    def _notify(
        self,
        provider: str,
        old: BreakerState,
        new: BreakerState,
        reason: str,
    ) -> None:
        """触发状态变更回调 + 日志。"""
        logger.info("[%s] %s → %s: %s", provider, old.value, new.value, reason)
        if self._on_state_change:
            try:
                self._on_state_change(provider, old, new, reason)
            except Exception:
                logger.exception("CircuitBreaker 状态回调异常")
