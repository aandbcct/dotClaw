"""模型路由器

根据 purpose 选择最优模型，优先级制（priority 越小越优先）。
降级链从 priority 列表自动生成，无需单独配置。
客户端实例以 model_name 为 key 懒加载缓存。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import LLMClient

if TYPE_CHECKING:
    from ..config.settings import RouterConfig, ProviderConfig, ModelConfig

logger = logging.getLogger("dotclaw.llm.router")


class ModelRouter:
    """
    模型路由器。

    负责：
    - resolve(purpose, forced_model) → (LLMClient, model_name)
    - get_fallback_chain(purpose, from_model) → list[str]
    - get_available_models() → list[str]
    - 客户端实例懒加载缓存
    """

    def __init__(self, config: "RouterConfig"):
        self._config = config
        self._client_cache: dict[str, LLMClient] = {}

    def resolve(
        self,
        purpose: str = "chat",
        forced_model: str | None = None,
    ) -> tuple[LLMClient, str]:
        """
        解析 purpose 到具体的 (client, model_name)。

        forced_model 非空时的三层匹配优先级：
        1. 精确匹配 models 字典的 key
        2. 前缀匹配 providers 字典的 key
        3. 降级到 defaults.provider + defaults.model
        """
        if forced_model:
            model_name = self._resolve_forced_model(forced_model)
        else:
            model_name = self._resolve_by_purpose(purpose)

        client = self._get_or_create_client(model_name)
        return client, model_name

    def get_fallback_chain(self, purpose: str = "chat") -> list[str]:
        """
        返回指定 purpose 的降级模型列表（按 priority 升序）。

        降级链 = priority 列表中所有 active 模型，按优先级排序。
        不再需要独立配置 fallback_chain 字段。
        """
        purpose_cfg = self._config.purposes.get(purpose)
        if not purpose_cfg or not purpose_cfg.priority:
            return []

        # 按 priority 升序排列（越小越优先）
        sorted_priorities = sorted(purpose_cfg.priority, key=lambda p: p.priority)

        chain = []
        for p in sorted_priorities:
            cfg = self._config.models.get(p.model)
            if cfg and cfg.status == "active":
                chain.append(p.model)

        return chain

    def get_available_models(self) -> list[str]:
        """返回所有 status=active 的模型名称"""
        return [
            name
            for name, cfg in self._config.models.items()
            if cfg.status == "active"
        ]

    # ---- 内部方法 ----

    def _resolve_forced_model(self, forced_model: str) -> str:
        """三层匹配优先级解析 forced_model"""
        # 1. 精确匹配 models
        if forced_model in self._config.models:
            return forced_model

        # 2. 前缀匹配 providers
        if forced_model in self._config.providers:
            for name, cfg in self._config.models.items():
                if cfg.provider == forced_model and cfg.status == "active":
                    return name
            logger.warning(
                f"provider '{forced_model}' 没有 active 的 model，使用默认"
            )

        # 3. 降级
        logger.warning(
            f"forced_model '{forced_model}' 未匹配，降级到 "
            f"{self._config.defaults.provider}/{self._config.defaults.model}"
        )
        return self._config.defaults.model

    def _resolve_by_purpose(self, purpose: str) -> str:
        """
        按 purpose 的 priority 列表确定性选择 model。

        规则：priority 数值最小的 active model 被选中。
        同 priority 时按配置顺序（先出现优先）。
        """
        purpose_cfg = self._config.purposes.get(purpose)
        if not purpose_cfg or not purpose_cfg.priority:
            return self._config.defaults.model

        # 按 priority 升序排列，过滤 inactive
        sorted_models = sorted(purpose_cfg.priority, key=lambda p: p.priority)
        for p in sorted_models:
            cfg = self._config.models.get(p.model)
            if cfg and cfg.status == "active":
                return p.model

        return self._config.defaults.model

    def _get_or_create_client(self, model_name: str) -> LLMClient:
        """获取或懒加载创建 client 实例"""
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

    def _instantiate_client(
        self,
        provider_cfg: "ProviderConfig",
        model_cfg: "ModelConfig",
    ) -> LLMClient:
        """根据 provider 名称创建对应的客户端实例"""
        api_key = provider_cfg.api_key
        base_url = provider_cfg.base_url
        model_id = model_cfg.model_id
        provider_name = model_cfg.provider

        from .qwen import QwenClient
        from .deepseek import DeepSeekClient
        from .openai import OpenAIClient

        client_cls = {
            "qwen": QwenClient,
            "deepseek": DeepSeekClient,
            "openai": OpenAIClient,
        }.get(provider_name, QwenClient)

        return client_cls(
            api_key=api_key,
            base_url=base_url,
            model=model_id,
        )
