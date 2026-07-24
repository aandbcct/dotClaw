"""LLM 代理：路由编排 + 降级 + 流式统一接口

重写版：Router 负责所有路由智能（选路/限流/熔断），
Proxy 只管重试编排 + 流式保护 + Journal 追踪。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator, TYPE_CHECKING

from .base import ChatChunk, LLMClient, Message, ToolDefinition

if TYPE_CHECKING:
    from .model_router import ModelRouter

logger = logging.getLogger("dotclaw.llm")


# ============================================================
# 异常类
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
    LLM 代理：薄层编排。

    职责：
    - 通过 ModelRouter.select() 获取候选列表
    - 迭代候选列表，逐个尝试（含单模型内指数退避重试）
    - CallSetupError / RateLimitTimeout → 降级到下一个候选
    - NonRetryableStreamError → 直接向上抛出
    - Journal 追踪（llm_call_start/end）
    """

    def __init__(self, model_router: "ModelRouter"):
        self._router = model_router

    @property
    def available_models(self) -> list[str]:
        """返回当前可用的模型列表（经过限流+熔断过滤）。"""
        return self._router.select("chat")

    # todo 目前chat方法应该是llm调用总入口，但方法内部写死了chat()，不能进行emb或其他功能，需要解耦
    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        model: str | None = None,
        purpose: str = "chat",
        stream: bool = True,
        journal: "Any | None" = None,
    ) -> AsyncIterator[ChatChunk]:
        """
        统一聊天接口：选路 → 迭代候选 → 调用 → 降级。
        """
        from .rate_limiter import RateLimitTimeout

        # 1. 路由选出候选列表
        candidates = self._router.select(purpose, model)

        # ── Journal：LLM 调用开始 ──
        if journal:
            journal.llm_call_start()

        first_chunk_ts: float | None = None
        output_token_count = 0
        input_tokens = 0
        output_tokens = 0
        ttft_ms = 0.0
        call_start = time.perf_counter()
        last_error: Exception | None = None

        try:
            for model_name in candidates:
                provider = self._router.get_provider_name(model_name)
                client = self._router.get_client(model_name)

                # 单模型内指数退避重试
                max_retries = self._get_retry_config(model_name)
                base_delay = self._get_backoff_config(model_name)

                try:
                    for attempt in range(max_retries):
                        try:
                            # 限流令牌获取（timeout=100ms）
                            await self._router.try_acquire(provider, timeout=0.1)

                            # 调用 client.chat()
                            chat_iter = client.chat(messages, tools, stream)

                            # 尝取第一个 chunk（区分 CallSetupError vs 流式异常）
                            first_chunk = await anext(chat_iter)

                            # 第一个 chunk 成功
                            if first_chunk_ts is None:
                                first_chunk_ts = time.perf_counter()
                                ttft_ms = (first_chunk_ts - call_start) * 1000
                                if journal:
                                    journal.llm_call_end()

                            yield first_chunk
                            try:
                                async for chunk in chat_iter:
                                    if chunk.text_deltas:
                                        output_token_count += sum(
                                            len(delta.content) for delta in chunk.text_deltas
                                        )
                                    if chunk.finish_reason is not None and chunk.usage is not None:
                                        input_tokens = chunk.usage.input_tokens
                                        output_tokens = chunk.usage.output_tokens
                                    yield chunk
                            except Exception as e:
                                raise NonRetryableStreamError(
                                    f"流式响应中断 ({model_name}): {e}"
                                ) from e

                            # 成功 → 上报
                            self._router.report_success(model_name)
                            return

                        except NonRetryableStreamError:
                            raise

                        except StopAsyncIteration:
                            self._router.report_success(model_name)
                            return  # 正常空响应

                        except RateLimitTimeout as e:
                            # 限流超时 → 降级到下一个候选
                            last_error = e
                            logger.warning(
                                "限流 %s 超时，降级到下一个候选", model_name
                            )
                            raise CallSetupError(str(e)) from e

                        except CallSetupError as e:
                            # 构造错误 → 降级
                            last_error = e
                            self._router.report_failure(model_name)
                            raise  # 抛给外层 except 处理

                        except Exception as e:
                            # 未知异常 → 指数退避重试
                            last_error = e
                            self._router.report_failure(model_name)
                            if attempt < max_retries - 1:
                                delay = base_delay * (2 ** attempt)
                                logger.warning(
                                    "模型 %s 调用失败 (attempt %d/%d): %s，%.1fs 后重试...",
                                    model_name, attempt + 1, max_retries, e, delay,
                                )
                                await asyncio.sleep(delay)
                            else:
                                logger.error(
                                    "模型 %s 全部 %d 次重试失败: %s",
                                    model_name, max_retries, e,
                                )
                                raise CallSetupError(
                                    f"模型 {model_name} 调用失败（{max_retries} 次重试后）: {e}"
                                ) from e

                except CallSetupError:
                    # 当前模型最终失败 → 尝试下一个候选
                    logger.warning("降级：%s 失败，尝试下一个候选...", model_name)
                    continue

            # 全部候选失败
            raise RuntimeError(
                f"所有候选模型 ({', '.join(candidates)}) 均调用失败。最后错误: {last_error}"
            )

        finally:
            pass

    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
        purpose: str = "embedding",
        dimensions: int = 1024,
    ) -> list[list[float]]:
        """
        文本向量化接口。

        通过 ModelRouter.select(purpose) 查找嵌入模型，
        取第一个候选调用 client.embed()。
        """
        candidates: list[str] = self._router.select(purpose, model)
        if not candidates:
            raise RuntimeError("无可用 embedding 模型")

        model_name: str = candidates[0]
        client: LLMClient = self._router.get_client(model_name)
        return await client.embed(texts, dimensions=dimensions)

    def _get_retry_config(self, model_name: str) -> int:
        """获取 model 的重试次数（从 Router 门面获取）。"""
        return self._router._get_retry_config(model_name)

    def _get_backoff_config(self, model_name: str) -> float:
        """获取 model 的退避因子（从 Router 门面获取）。"""
        return self._router._get_backoff_config(model_name)
