"""LLM 协议 driver 的显式构造入口。"""

from __future__ import annotations

from collections.abc import Callable

from dotclaw.config.settings import LLMDriver

from ..base import LLMClient
from ..reasoning import ReasoningPolicy
from .openai_chat_completions import OpenAIChatCompletionsClient


DriverFactory = Callable[..., LLMClient]

_DRIVER_FACTORIES: dict[LLMDriver, DriverFactory] = {
    LLMDriver.OPENAI_CHAT_COMPLETIONS: OpenAIChatCompletionsClient,
}

_DRIVER_CAPABILITIES: dict[LLMDriver, frozenset[str]] = {
    LLMDriver.OPENAI_CHAT_COMPLETIONS: frozenset(
        {"chat", "function_calling", "embedding"}
    ),
}


def create_driver_client(
    driver: LLMDriver,
    *,
    api_key: str,
    base_url: str,
    model: str,
    policy: ReasoningPolicy,
) -> LLMClient:
    """按显式协议名称创建客户端，未知协议不得回退。"""
    factory = _DRIVER_FACTORIES.get(driver)
    if factory is None:
        raise ValueError(f"未注册 LLM driver: {driver!s}")
    return factory(
        api_key=api_key,
        base_url=base_url,
        model=model,
        policy=policy,
    )


def get_driver_capabilities(driver: LLMDriver) -> frozenset[str]:
    """返回 driver 支持的模型能力，未知协议直接失败。"""
    capabilities = _DRIVER_CAPABILITIES.get(driver)
    if capabilities is None:
        raise ValueError(f"未注册 LLM driver: {driver!s}")
    return capabilities


__all__ = [
    "OpenAIChatCompletionsClient",
    "create_driver_client",
    "get_driver_capabilities",
]
