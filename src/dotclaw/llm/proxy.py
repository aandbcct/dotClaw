"""LLM 代理：路由编排 + 降级 + 流式统一接口

重写版：Router 负责所有路由智能（选路/限流/熔断），
Proxy 只管重试编排 + 流式保护 + Journal 追踪。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, AsyncIterator, TYPE_CHECKING

from .base import ChatChunk, EmptyModelResponseError, LLMClient, Message, TextDeltaKind, ToolDefinition
from ..common.logging_setup import exception_diagnostic

if TYPE_CHECKING:
    from dotclaw.config.settings import RouterConfig
    from .model_router import ModelRouter

logger = logging.getLogger("dotclaw.llm")


def _format_exception(error: BaseException) -> str:
    """格式化异常；无文本异常必须保留类型，避免产生空白日志。"""
    return type(error).__name__


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

    def preferred_model(self, purpose: str = "chat") -> str:
        """返回用途优先级最高的静态 active 模型。"""
        return self._router.preferred_model(purpose)

    def models_for_purpose(self, purpose: str = "chat") -> tuple[str, ...]:
        """返回用途下按优先级排序的全部静态 active 模型。"""
        return self._router.models_for_purpose(purpose)

    @property
    def router_config(self) -> "RouterConfig":
        """返回只读展示所需的模型路由配置。"""
        return self._router.config

    # todo 目前chat方法应该是llm调用总入口，但方法内部写死了chat()，不能进行emb或其他功能，需要解耦
    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        model: str | None = None,
        purpose: str = "chat",
        stream: bool = True,
        journal: "Any | None" = None,
        timeout_seconds: float | None = None,
        retry_count: int | None = None,
    ) -> AsyncIterator[ChatChunk]:
        """
        统一聊天接口：选路 → 迭代候选 → 调用 → 降级。
        """
        from .circuit_breaker import BreakerState
        from .model_router import CircuitUnavailableError
        from .rate_limiter import RateLimitTimeout

        # 1. 路由选出候选列表
        candidates = self._router.select(purpose, model)
        if not candidates:
            raise RuntimeError(f"用途 {purpose!r} 没有可调用模型（候选均限流、熔断或未启用）")

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

                try:
                    begin_call = getattr(self._router, "begin_call", None)
                    breaker_state = (
                        begin_call(model_name)
                        if begin_call is not None
                        else BreakerState.CLOSED
                    )
                except CircuitUnavailableError as error:
                    last_error = error
                    logger.warning("跳过模型 %s：%s", model_name, error)
                    continue

                # 单模型内指数退避重试
                max_retries = (
                    retry_count + 1 if retry_count is not None else self._get_retry_config(model_name)
                )
                if breaker_state is BreakerState.HALF_OPEN:
                    max_retries = 1
                if max_retries <= 0:
                    raise ValueError("retry_count 必须大于等于 0")
                base_delay = self._get_backoff_config(model_name)

                try:
                    provider_attempted = False
                    for attempt in range(max_retries):
                        attempt_start: float | None = None
                        attempt_end: float | None = None
                        response_chars = 0
                        tool_call_count = 0
                        finish_reason: str | None = None
                        call_error: BaseException | None = None
                        try:
                            # 限流令牌获取（timeout=100ms）
                            await self._router.try_acquire(provider, timeout=0.1)

                            # 调用 client.chat()
                            provider_attempted = True
                            attempt_start = time.perf_counter()
                            logger.info("llm.call.started %s", json.dumps({"provider": provider, "model": model_name, "attempt": attempt + 1}))
                            client_options: dict[str, float | int] = {}
                            if timeout_seconds is not None:
                                client_options["timeout_seconds"] = timeout_seconds
                            if retry_count is not None:
                                client_options["retry_count"] = retry_count
                            chat_iter = client.chat(messages, tools, stream, **client_options)

                            # 可见输出边界：每次尝试重新计，避免跨重试误判
                            visible_output_started = False

                            # 尝取第一个 chunk（区分 CallSetupError vs 流式异常）
                            has_response_text = False
                            has_tool_calls = False
                            first_chunk = await anext(chat_iter, None)
                            if first_chunk is None:
                                raise EmptyModelResponseError()

                            # 第一个 chunk 成功
                            if first_chunk_ts is None:
                                first_chunk_ts = time.perf_counter()
                                ttft_ms = (first_chunk_ts - call_start) * 1000
                                if journal:
                                    journal.llm_call_end()

                            # 可见输出边界：只要交付过非空 reasoning/response delta，
                            # 即视为“已输出”，此后流异常不得重试或降级（设计 §7）。
                            if first_chunk.text_deltas:
                                visible_output_started = True
                            has_response_text = any(delta.kind is TextDeltaKind.RESPONSE and delta.content.strip() for delta in first_chunk.text_deltas)
                            has_tool_calls = bool(first_chunk.tool_calls)
                            response_chars += sum(len(delta.content) for delta in first_chunk.text_deltas if delta.kind is TextDeltaKind.RESPONSE)
                            tool_call_count += len(first_chunk.tool_calls)
                            finish_reason = first_chunk.finish_reason if first_chunk.finish_reason is not None else finish_reason
                            if first_chunk.usage is not None:
                                input_tokens = first_chunk.usage.input_tokens
                                output_tokens = first_chunk.usage.output_tokens
                            yield first_chunk
                            try:
                                async for chunk in chat_iter:
                                    has_response_text |= any(delta.kind is TextDeltaKind.RESPONSE and delta.content.strip() for delta in chunk.text_deltas)
                                    has_tool_calls |= bool(chunk.tool_calls)
                                    response_chars += sum(len(delta.content) for delta in chunk.text_deltas if delta.kind is TextDeltaKind.RESPONSE)
                                    tool_call_count += len(chunk.tool_calls)
                                    finish_reason = chunk.finish_reason if chunk.finish_reason is not None else finish_reason
                                    if chunk.text_deltas:
                                        visible_output_started = True
                                        output_token_count += sum(
                                            len(delta.content) for delta in chunk.text_deltas
                                        )
                                    if chunk.usage is not None:
                                        input_tokens = chunk.usage.input_tokens
                                        output_tokens = chunk.usage.output_tokens
                                    yield chunk
                            except asyncio.CancelledError:
                                # 运行取消必须直达客户端 finally，绝不可重试或降级。
                                raise
                            except Exception as e:
                                if visible_output_started:
                                    # 已展示 reasoning/response：不可降级，直接向上抛出
                                    raise NonRetryableStreamError(
                                        f"流式响应中断 ({model_name}): {e}"
                                    ) from e
                                # 尚未产生可见输出：允许既有重试与候选切换
                                raise CallSetupError(
                                    f"流开始但未产生可见输出即中断 ({model_name}): {e}"
                                ) from e

                            if not has_response_text and not has_tool_calls:
                                raise EmptyModelResponseError()
                            # 有效输出完成后才能记成功。
                            self._router.report_success(model_name)
                            return

                        except asyncio.CancelledError as error:
                            call_error = error
                            attempt_end = time.perf_counter()
                            # 取消不计作 Provider 失败，也不进入退避或候选降级。
                            cancel_call = getattr(self._router, "cancel_call", None)
                            if cancel_call is not None:
                                cancel_call(model_name, breaker_state)
                            raise
                        except EmptyModelResponseError as error:
                            call_error = error
                            attempt_end = time.perf_counter()
                            # 确定无效输出只计一次失败，不重试也不进入候选降级。
                            self._router.report_failure(model_name)
                            raise
                        except NonRetryableStreamError as error:
                            call_error = error
                            attempt_end = time.perf_counter()
                            # 已有可见输出时禁止重试或降级，但传输失败仍计一次逻辑失败。
                            self._router.report_failure(model_name)
                            raise

                        except RateLimitTimeout as e:
                            call_error = e
                            attempt_end = time.perf_counter()
                            # 限流超时 → 降级到下一个候选
                            cancel_call = getattr(self._router, "cancel_call", None)
                            if cancel_call is not None:
                                cancel_call(model_name, breaker_state)
                            last_error = e
                            logger.warning(
                                "限流 %s 超时，降级到下一个候选", model_name
                            )
                            raise CallSetupError(str(e)) from e

                        except CallSetupError as e:
                            call_error = e
                            attempt_end = time.perf_counter()
                            # 构造错误 → 降级
                            last_error = e
                            raise  # 抛给外层 except 处理

                        except Exception as e:
                            call_error = e
                            attempt_end = time.perf_counter()
                            # 未知异常 → 指数退避重试
                            last_error = e
                            if attempt < max_retries - 1:
                                delay = base_delay * (2 ** attempt)
                                logger.warning(
                                    "模型 %s 调用失败 (attempt %d/%d): %s，%.1fs 后重试...",
                                    model_name,
                                    attempt + 1,
                                    max_retries,
                                    _format_exception(e),
                                    delay,
                                )
                                await asyncio.sleep(delay)
                            else:
                                logger.error(
                                    "模型 %s 全部 %d 次重试失败: %s",
                                    model_name,
                                    max_retries,
                                    _format_exception(e),
                                )
                                raise CallSetupError(
                                    f"模型 {model_name} 调用失败（{max_retries} 次重试后）: "
                                    f"{_format_exception(e)}"
                                ) from e
                        finally:
                            if attempt_start is not None:
                                fields: dict[str, str | int | float | None] = {
                                    "provider": provider, "model": model_name, "attempt": attempt + 1,
                                    "elapsed_ms": round(((attempt_end if attempt_end is not None else time.perf_counter()) - attempt_start) * 1000, 2),
                                    "finish_reason": finish_reason, "response_chars": response_chars,
                                    "tool_call_count": tool_call_count,
                                    "error_type": type(call_error).__name__ if call_error is not None else None,
                                    "http_status": None, "request_id": None,
                                }
                                cause = call_error
                                for _ in range(4):
                                    if cause is None:
                                        break
                                    status_code = getattr(cause, "status_code", None)
                                    request_id = getattr(cause, "request_id", None)
                                    if isinstance(status_code, int) and 100 <= status_code <= 599:
                                        fields["http_status"] = status_code
                                    if isinstance(request_id, str) and re.fullmatch(r"[\w-]{1,128}", request_id):
                                        fields["request_id"] = request_id
                                    cause = cause.__cause__
                                if call_error is not None:
                                    fields["diagnostic"] = exception_diagnostic(call_error)
                                logger.log(logging.INFO if call_error is None else logging.WARNING, "%s %s", "llm.call.finished" if call_error is None else "llm.call.failed", json.dumps(fields, ensure_ascii=False))

                except CallSetupError:
                    # 当前模型最终失败 → 尝试下一个候选
                    if provider_attempted:
                        self._router.report_failure(model_name)
                    logger.warning("降级：%s 失败，尝试下一个候选...", model_name)
                    continue

            # 全部候选失败
            raise RuntimeError(
                f"所有候选模型 ({', '.join(candidates)}) 均调用失败。最后错误: "
                f"{_format_exception(last_error) if last_error else 'unknown'}"
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
