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

from ..config.settings import LLMDriver, ModelConfig, ProviderConfig, RouterConfig
from .base import LLMClient
from .circuit_breaker import BreakerState, CircuitBreaker
from .drivers import create_driver_client, get_driver_capabilities
from .rate_limiter import RateLimiter, RateLimitTimeout
from .reasoning import ReasoningPolicy

logger = logging.getLogger("dotclaw.llm.router")


class CircuitUnavailableError(RuntimeError):
    """模型所属供应商正在熔断，当前调用不得进入客户端。"""


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
        config: RouterConfig,
        rate_limiter: RateLimiter,
        circuit_breaker: CircuitBreaker,
    ):
        self._validate_config(config)
        self._config = config
        self._client_cache: dict[str, LLMClient] = {}
        self._rate_limiter = rate_limiter
        self._circuit_breaker = circuit_breaker

    # ============================================================
    # 公共 API
    # ============================================================

    def preferred_model(self, purpose: str = "chat") -> str:
        """返回指定用途静态优先级最高的 active 模型。"""
        models = self.models_for_purpose(purpose)
        if models:
            return models[0]
        raise ValueError(f"用途 {purpose!r} 没有 active 模型")

    def models_for_purpose(self, purpose: str = "chat") -> tuple[str, ...]:
        """返回指定用途按优先级排序的全部静态 active 模型。"""
        purpose_cfg = self._config.purposes.get(purpose)
        if purpose_cfg is None:
            raise ValueError(f"未配置用途: {purpose}")
        models: list[str] = []
        for item in sorted(purpose_cfg.priority, key=lambda value: value.priority):
            model_cfg = self._config.models.get(item.model)
            if (
                model_cfg is not None
                and model_cfg.status == "active"
                and item.model not in models
            ):
                models.append(item.model)
        return tuple(models)

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
        3. 熔断: circuit_breaker.is_open(provider) → 跳过
        4. HALF_OPEN provider → 保留（允许探测）
        5. 全部不可用 → 返回空候选，由调用层快速失败

        如果 forced_model 匹配到某个模型/供应商，将其提升到候选列表第一位。
        """
        candidates = self._build_candidates(purpose, forced_model)

        return candidates

    def begin_call(self, model_name: str) -> BreakerState:
        """在逻辑模型调用前检查熔断状态，并独占 HALF_OPEN 探测名额。"""
        provider = self.get_provider_name(model_name)
        state = self._circuit_breaker.get_state(provider)
        if state is BreakerState.OPEN:
            raise CircuitUnavailableError(f"provider {provider!r} 正在熔断冷却")
        if state is BreakerState.HALF_OPEN and not self._circuit_breaker.try_half_open(provider):
            raise CircuitUnavailableError(f"provider {provider!r} 已有 HALF_OPEN 探测请求")
        return state

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

    def cancel_call(self, model_name: str, state: BreakerState) -> None:
        """释放未真正请求 Provider 的 HALF_OPEN 探测名额。"""
        if state is not BreakerState.HALF_OPEN:
            return
        model_cfg = self._config.models.get(model_name)
        if model_cfg:
            self._circuit_breaker.cancel_half_open(model_cfg.provider)

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

        两层分组：
        - normal: CLOSED 且限流通过
        - half_open: HALF_OPEN（允许探测）
        OPEN 状态不会进入候选列表。
        """
        purpose_cfg = self._config.purposes.get(purpose)
        if not purpose_cfg or not purpose_cfg.priority:
            return []

        # 按 priority 升序排列
        sorted_priorities = sorted(purpose_cfg.priority, key=lambda p: p.priority)

        normal = []
        half_open = []

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
                logger.debug("select: %s 熔断跳过", model_name)

        # 排序: 如果 forced_model 匹配，提到最前
        candidates = normal + half_open

        if forced_model:
            candidates = self._prioritize_forced(candidates, forced_model)

        return candidates

    def _prioritize_forced(
        self,
        candidates: list[str],
        forced_model: str,
    ) -> list[str]:
        """如果 forced_model 匹配，将其提升到候选列表第一位。"""
        # 1. 精确匹配 model name
        if forced_model in candidates:
            candidates.remove(forced_model)
            return [forced_model] + candidates

        # 精确模型允许位于 purpose 链之外；Session 绑定模型是明确选择，
        # 但仍必须通过限流和熔断检查。
        forced_config = self._config.models.get(forced_model)
        if (
            forced_config is not None
            and forced_config.status == "active"
            and self._rate_limiter.check(forced_config.provider)
            and self._circuit_breaker.get_state(forced_config.provider) is not BreakerState.OPEN
        ):
            return [forced_model] + candidates

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
                    if (
                        cfg.provider == forced_model
                        and cfg.status == "active"
                        and self._rate_limiter.check(cfg.provider)
                        and self._circuit_breaker.get_state(cfg.provider) is not BreakerState.OPEN
                    ):
                        provider_models.append(name)
            if provider_models:
                return provider_models + remaining
            logger.warning("provider '%s' 没有 active 模型", forced_model)

        # 3. 不匹配 → 保持原顺序
        logger.warning("forced_model '%s' 不匹配任何模型/供应商，降级使用 purpose 排序", forced_model)
        return candidates

    # ============================================================
    # 内部: 客户端实例化
    # ============================================================

    def _instantiate_client(
        self,
        provider_cfg: ProviderConfig,
        model_cfg: ModelConfig,
    ) -> LLMClient:
        """根据模型的有效 driver 创建协议客户端。"""
        driver = self._effective_driver(provider_cfg, model_cfg)
        return create_driver_client(
            driver,
            api_key=provider_cfg.api_key,
            base_url=provider_cfg.base_url,
            model=model_cfg.model_id,
            policy=ReasoningPolicy.from_config(model_cfg.reasoning),
            parameters=dict(self._config.defaults.parameters),
        )

    @staticmethod
    def _effective_driver(
        provider_cfg: ProviderConfig,
        model_cfg: ModelConfig,
    ) -> LLMDriver:
        """模型级配置优先，否则使用 provider 默认协议。"""
        return model_cfg.driver or provider_cfg.driver

    @classmethod
    def _validate_config(cls, config: RouterConfig) -> None:
        """在 Router 可被调用前验证全部静态引用和 driver 能力。"""
        if not config.models:
            raise ValueError("models 至少需要配置一个模型")

        for purpose_name, purpose_cfg in config.purposes.items():
            for index, priority in enumerate(purpose_cfg.priority):
                if priority.model not in config.models:
                    raise ValueError(
                        f"purposes.{purpose_name}.priority[{index}] 引用未知模型: "
                        f"{priority.model!r}"
                    )

        chat_cfg = config.purposes.get("chat")
        if chat_cfg is None or not any(
            config.models.get(item.model) is not None
            and config.models[item.model].status == "active"
            for item in chat_cfg.priority
        ):
            raise ValueError("purposes.chat 至少需要一个 active 模型")

        for model_name, model_cfg in config.models.items():
            if model_cfg.status != "active":
                continue
            provider_cfg = config.providers.get(model_cfg.provider)
            if provider_cfg is None:
                raise ValueError(
                    f"models.{model_name}.provider 引用未知 provider: "
                    f"{model_cfg.provider!r}"
                )
            driver = cls._effective_driver(provider_cfg, model_cfg)
            supported = get_driver_capabilities(driver)
            unsupported = sorted(set(model_cfg.capabilities) - supported)
            if unsupported:
                raise ValueError(
                    f"models.{model_name} 的 driver {driver.value!r} 不支持能力: "
                    f"{', '.join(unsupported)}"
                )
