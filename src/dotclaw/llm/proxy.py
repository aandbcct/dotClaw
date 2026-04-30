"""LLM 代理：重试 + 降级 + 流式统一接口"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from .base import ChatChunk, LLMClient, Message, ToolDefinition
from .qwen import QwenClient
from ..config import LLMConfig


logger = logging.getLogger("dotclaw.llm")


class LLMProxy:
    """
    LLM 代理：统一入口。

    职责：
    - 指数退避重试（主模型）
    - fallback 降级（主模型失败 → 备用模型）
    - 流式/非流式统一接口
    """

    def __init__(self, config: LLMConfig):
        self._config = config
        self._clients: dict[str, LLMClient] = {}

        # 初始化所有客户端
        for name, cfg in config.clients.items():
            self._clients[name] = QwenClient(
                api_key=cfg.api_key,
                base_url=cfg.base_url,
                model=cfg.model,
            )

        self._primary = config.default_model
        self._fallbacks = list(config.fallbacks)
        self._max_retries = config.retry_max_retries
        self._base_delay = config.retry_base_delay

    @property
    def available_models(self) -> list[str]:
        return list(self._clients.keys())

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        model: str | None = None,
        stream: bool = True,
    ) -> AsyncIterator[ChatChunk]:
        """
        统一聊天接口：先试主模型（含重试），失败则降级。
        """
        models_to_try = []
        if model:
            models_to_try = [model]
        else:
            models_to_try = [self._primary] + self._fallbacks

        for attempt_model in models_to_try:
            client = self._clients.get(attempt_model)
            if not client:
                logger.warning(f"模型 {attempt_model} 未配置，跳过")
                continue

            for attempt in range(self._max_retries):
                try:
                    async for chunk in client.chat(messages, tools, stream):
                        yield chunk
                    return  # 成功，退出
                except Exception as e:
                    delay = self._base_delay * (2 ** attempt)
                    logger.warning(
                        f"模型 {attempt_model} 调用失败 (attempt {attempt + 1}/{self._max_retries}): {e}，"
                        f" {delay:.1f}s 后重试..."
                    )
                    await asyncio.sleep(delay)

            logger.error(f"模型 {attempt_model} 全部重试失败，尝试降级...")

        raise RuntimeError(f"所有模型 ({models_to_try}) 均调用失败")
