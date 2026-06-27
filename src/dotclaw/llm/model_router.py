"""模型路由器

重写版：按 purpose 生成候选列表，内持 RateLimiter + CircuitBreaker，
在 select() 阶段就过滤掉不可用的 provider。

接口：
- select(purpose, forced_model) → list[str]      # 排序+过滤后的候选模型名
- get_client(model_name) → LLMClient              # 懒加载客户端实例
- report_success(model_name) → None               # 上报成功 → CircuitBreaker.on_success()
- report_failure(model_name) → None               # 上报失败 → CircuitBreaker.on_failure()
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import LLMClient

if TYPE_CHECKING:
    from ..config.settings import RouterConfig

logger = logging.getLogger("dotclaw.llm.router")


class ModelRouter:
    """
    模型路由器。

    负责：
    - select(): 按 purpose 生成过滤后的候选列表（priority 排序）
    - get_client(): 懒加载客户端实例
    - report_success/failure(): 驱动熔断器状态机
    """

    def __init__(
        self,
        config: "RouterConfig",
        rate_limiter: "RateLimiter",            # noqa: F821
        circuit_breaker: "CircuitBreaker",      # noqa: F821
    ):
        self._config = config
        self._client_cache: dict[str, LLMClient] = {}
        self._rate_limiter = rate_limiter
        self._circuit_breaker = circuit_breaker

    # ============================================================
    # 公共 API
    # ============================================================

    def select(
        self,
        purpose: str = "chat",
        forced_model: str | None = None,
    ) -> list[str]:
        """
        返回按优先级排序、过滤不可用的候选模型列表。

        过滤顺序：
        1. 静态: 按 purpose.priority 排序，过滤 status != "active"
        2. 限流: rate_limiter.check(provider) == False → 跳过
        3. 熔断: circuit_breaker.is_open(provider) → 降到最后（兜底）
        4. HALF_OPEN provider → 保留（允许探测）
        5. 全部不可用 → 保留最优先的 OPEN provider 作为最后尝试

        如果 forced_model 匹配到某个模型/供应商，将其提升到候选列表第一位。
        """
        candidates = self._build_candidates(purpose, forced_model)

        if not candidates:
            logger.warning("select() 无候选模型，回退到 defaults.model")
            return [self._config.defaults.model]

        return candidates

    def get_client(self, model_name: str) -> LLMClient:
        """获取或懒加载创建客户端实例。"""
        if model_name in self._client_cache:
            return self._client_cache[model_name]

        model_cfg = self._config.models.get(model_name)
        if not model_cfg:
            raise ValueError(f"模型 '{model_name}' 未在配置中找到")

        provider_name = model_cfg.provider
        provider_cfg = self._config.providers.get(provider_name)
        if not provider_cfg:
            raise ValueError(f"provider '{provider_name}' 未在配置中找到")

        client = self._instantiate_client(provider_cfg, model_cfg)
        self._client_cache[model_name] = client
        return client

    async def try_acquire(self, provider: str, timeout: float) -> None:
        """
        尝试从限流器获取令牌。

        Agent 通过 Router 的门面调用，不直接触及 RateLimiter。
        超时抛 RateLimitTimeout → Proxy 视为降级信号。
        """
        from .rate_limiter import RateLimitTimeout
        try:
            await self._rate_limiter.acquire(provider, timeout=timeout)
        except RateLimitTimeout:
            raise  # 透传给 Proxy

    def get_provider_name(self, model_name: str) -> str:
        """获取 model 对应的 provider 名称。"""
        model_cfg = self._config.models.get(model_name)
        return model_cfg.provider if model_cfg else "unknown"

    def report_success(self, model_name: str) -> None:
        """上报一次成功调用 → 推进熔断器状态。"""
        model_cfg = self._config.models.get(model_name)
        if model_cfg:
            self._circuit_breaker.on_success(model_cfg.provider)

    def report_failure(self, model_name: str) -> None:
        """上报一次失败调用 → 推进熔断器状态。"""
        model_cfg = self._config.models.get(model_name)
        if model_cfg:
            self._circuit_breaker.on_failure(model_cfg.provider)

    def _get_retry_config(self, model_name: str) -> int:
        """获取 model 对应的重试次数（从 provider retry 配置读取）。"""
        model_cfg = self._config.models.get(model_name)
        if model_cfg:
            provider_cfg = self._config.providers.get(model_cfg.provider)
            if provider_cfg:
                return provider_cfg.retry.max_attempts
        return 3

    def _get_backoff_config(self, model_name: str) -> float:
        """获取 model 对应的退避因子（从 provider retry 配置读取）。"""
        model_cfg = self._config.models.get(model_name)
        if model_cfg:
            provider_cfg = self._config.providers.get(model_cfg.provider)
            if provider_cfg:
                return provider_cfg.retry.backoff_factor
        return 2.0

    # ============================================================
    # 内部: 候选列表构建
    # ============================================================

    def _build_candidates(
        self,
        purpose: str,
        forced_model: str | None,
    ) -> list[str]:
        """
        构建过滤后的候选列表。

        三层分组：
        - normal: CLOSED 且限流通过
        - half_open: HALF_OPEN（允许探测）
        - fallback: OPEN（全部不可用时的兜底）
        """
        purpose_cfg = self._config.purposes.get(purpose)
        if not purpose_cfg or not purpose_cfg.priority:
            return [self._config.defaults.model]

        # 按 priority 升序排列
        sorted_priorities = sorted(purpose_cfg.priority, key=lambda p: p.priority)

        normal = []
        half_open = []
        fallback = []

        for p in sorted_priorities:
            model_cfg = self._config.models.get(p.model)
            if not model_cfg or model_cfg.status != "active":
                continue

            provider = model_cfg.provider
            model_name = p.model

            # 限流检查
            if not self._rate_limiter.check(provider):
                logger.debug("select: %s 限流跳过", model_name)
                continue

            # 熔断状态
            cb_state = self._circuit_breaker.get_state(provider)

            if cb_state.value == "closed":
                normal.append(model_name)
            elif cb_state.value == "half_open":
                half_open.append(model_name)
            else:  # open
                fallback.append(model_name)

        # 排序: 如果 forced_model 匹配，提到最前
        candidates = normal + half_open

        if forced_model:
            candidates = self._prioritize_forced(candidates, fallback, forced_model)

        # 全部不可用 → 保留最优先的 OPEN provider 作为兜底
        if not candidates and fallback:
            logger.warning("select: 全部 provider 不可用，保留 %s 作为兜底", fallback[0])
            return fallback[:1]

        return candidates

    def _prioritize_forced(
        self,
        candidates: list[str],
        fallback: list[str],
        forced_model: str,
    ) -> list[str]:
        """如果 forced_model 匹配，将其提升到候选列表第一位。"""
        # 1. 精确匹配 model name
        if forced_model in candidates:
            candidates.remove(forced_model)
            return [forced_model] + candidates

        if forced_model in fallback:
            # forced model 在熔断中，仍然放在第一位（允许尝试）
            fallback.remove(forced_model)
            return [forced_model] + candidates + fallback

        # 2. 匹配 provider name → 将该 provider 的所有模型提到前面
        if forced_model in self._config.providers:
            # 从 candidates 中找属于该 provider 的模型
            provider_models = []
            remaining = candidates.copy()
            for m in candidates:
                cfg = self._config.models.get(m)
                if cfg and cfg.provider == forced_model:
                    provider_models.append(m)
                    remaining.remove(m)
            # 如果 candidates 中没有, 从全局 models 中找（降级作用域扩大）
            if not provider_models:
                for name, cfg in self._config.models.items():
                    if cfg.provider == forced_model and cfg.status == "active":
                        provider_models.append(name)
            if provider_models:
                return provider_models + remaining + fallback
            logger.warning("provider '%s' 没有 active 模型", forced_model)

        # 3. 不匹配 → 保持原顺序
        logger.warning("forced_model '%s' 不匹配任何模型/供应商，降级使用 purpose 排序", forced_model)
        return candidates + fallback

    # ============================================================
    # 内部: 客户端实例化
    # ============================================================

    def _instantiate_client(self, provider_cfg, model_cfg) -> LLMClient:
        """根据 provider 创建客户端实例（使用注册表 + 回调函数）。"""
        from .providers import get_provider

        api_key = provider_cfg.api_key
        base_url = provider_cfg.base_url
        model_id = model_cfg.model_id
        provider_name = model_cfg.provider

        client_cls = get_provider(provider_name)
        if client_cls is None:
            # 回退：未知 provider → 使用 QwenClient 作为兼容默认
            from .providers.qwen import QwenClient
            logger.warning("未知 provider '%s'，回退到 QwenClient", provider_name)
            client_cls = QwenClient

        return client_cls(
            api_key=api_key,
            base_url=base_url,
            model=model_id,
        )
