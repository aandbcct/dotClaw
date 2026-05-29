"""LLM 代理：多供应商路由 + 限流 + 降级 + 流式统一接口"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, TYPE_CHECKING

from .base import ChatChunk, LLMClient, Message, ToolDefinition

if TYPE_CHECKING:
    from .model_router import ModelRouter
    from ..common.rate_limiter import RateLimiter

logger = logging.getLogger("dotclaw.llm")


# ============================================================
# P2 新增异常类
# ============================================================

class CallSetupError(Exception):
    """
    调用前异常 — 触发降级。

    触发条件：连接超时、HTTP 非 2xx（流开始前）、认证失败、DNS 解析失败。
    应在 async for 循环开始之前捕获。
    """
    pass


class NonRetryableStreamError(Exception):
    """
    流式中途异常 — 不降级，直接向上抛出。

    触发条件：流式响应中途断连、chunk 解析失败。
    一旦 async for 开始产出至少一个 chunk，后续异常不降级。
    """
    pass


# ============================================================
# LLMProxy
# ============================================================

class LLMProxy:
    """
    LLM 代理：统一入口。

    P2 职责：
    - 通过 ModelRouter 解析 purpose → (client, model)
    - RateLimiter 限流保护
    - CallSetupError → 降级到 fallback_chain 的下一个模型
    - NonRetryableStreamError → 直接向上抛出
    - 单模型内重试（指数退避）
    """

    def __init__(
        self,
        model_router: "ModelRouter",
        rate_limiter: "RateLimiter",
    ):
        self._router = model_router
        self._rate_limiter = rate_limiter

    @property
    def available_models(self) -> list[str]:
        return self._router.get_available_models()

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        model: str | None = None,
        purpose: str = "chat",
        stream: bool = True,
    ) -> AsyncIterator[ChatChunk]:
        """
        统一聊天接口：限流 → 路由 → 调用 → 降级。
        """
        # 1. 路由解析
        client, resolved_model = self._router.resolve(purpose, model)

        # 2. 查找 provider 以进行限流
        provider = self._router._config.models.get(resolved_model)
        provider_name = provider.provider if provider else "unknown"

        async for chunk in self._call_with_fallback(
            client=client,
            resolved_model=resolved_model,
            provider_name=provider_name,
            purpose=purpose,
            messages=messages,
            tools=tools,
            stream=stream,
        ):
            yield chunk

    async def _call_with_fallback(
        self,
        client: LLMClient,
        resolved_model: str,
        provider_name: str,
        purpose: str,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        stream: bool,
    ) -> AsyncIterator[ChatChunk]:
        """带降级的调用：当前模型失败后依次尝试 fallback_chain"""
        fallback_chain = self._router.get_fallback_chain(purpose)

        # 构建完整的尝试列表：[当前模型] + [fallback 中不同于当前的模型]
        models_to_try = [resolved_model]
        for fb in fallback_chain:
            if fb != resolved_model:
                models_to_try.append(fb)

        last_error = None

        for attempt_model in models_to_try:
            try:
                # 限流
                model_cfg = self._router._config.models.get(attempt_model)
                if model_cfg is None:
                    logger.warning(
                        f"模型 '{attempt_model}' 未在配置中找到，跳过"
                    )
                    continue

                await self._rate_limiter.acquire(model_cfg.provider)

                # 获取 client
                if attempt_model == resolved_model:
                    current_client = client
                else:
                    current_client = self._router._get_or_create_client(attempt_model)

                # 单模型内重试
                provider_cfg = self._router._config.providers.get(model_cfg.provider)
                max_retries = provider_cfg.retry.max_attempts if provider_cfg else 3
                base_delay = provider_cfg.retry.backoff_factor if provider_cfg else 2.0

                for attempt in range(max_retries):
                    try:
                        # 调用 client.chat()
                        chat_iter = current_client.chat(messages, tools, stream)

                        # 尝试获取第一个 chunk（区分 CallSetupError vs NonRetryableStreamError）
                        first_chunk = await anext(chat_iter)

                        # 第一个 chunk 成功，进入流式阶段
                        yield first_chunk
                        try:
                            async for chunk in chat_iter:
                                yield chunk
                        except Exception as e:
                            # 流式中途异常 → 不降级
                            raise NonRetryableStreamError(
                                f"流式响应中断 ({attempt_model}): {e}"
                            ) from e

                        return  # 成功完成

                    except NonRetryableStreamError:
                        raise  # 直接向上抛出

                    except StopAsyncIteration:
                        return  # 正常的空响应

                    except CallSetupError:
                        raise  # 不重试，直接触发降级

                    except Exception as e:
                        # 在获取第一个 chunk 之前的异常 → 可能是 CallSetupError
                        if attempt < max_retries - 1:
                            delay = base_delay * (2 ** attempt)
                            logger.warning(
                                f"模型 {attempt_model} 调用失败 "
                                f"(attempt {attempt + 1}/{max_retries}): {e}，"
                                f" {delay:.1f}s 后重试..."
                            )
                            await asyncio.sleep(delay)
                        else:
                            logger.error(
                                f"模型 {attempt_model} 全部 {max_retries} 次重试失败: {e}"
                            )
                            raise CallSetupError(
                                f"模型 {attempt_model} 调用失败（{max_retries} 次重试后）: {e}"
                            ) from e

            except CallSetupError as e:
                last_error = e
                logger.warning(f"降级：{attempt_model} 失败，尝试下一个模型...")
                continue

            except NonRetryableStreamError:
                raise  # 不降级

        raise RuntimeError(
            f"所有模型 ({', '.join(models_to_try)}) 均调用失败。最后错误: {last_error}"
        )
