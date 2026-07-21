"""函数工具处理器（Tool v1 阶段一新增，阶段二接入签名推导）。

只负责调用一个本地函数，并把返回值规范为统一的 ToolResult。
不在每次调用时用 inspect.signature 猜测 _context：函数签名在构造时分析一次。

参数校验：阶段二暂在 handler 入口完成（对已验证模型幂等，executor 直接传模型时
不重复校验）。总体设计 §4.5 的固定链路（校验→Broker→Policy→审批→handler）将在
阶段三由 ToolExecutor 统一编排，届时 handler 仅接收已验证参数。
所有新增注释使用中文。
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable

from pydantic import BaseModel

from .base import (
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorType,
    ToolExecutionContext,
    ToolResult,
)
from .decorator import ToolMeta
from .handler import ToolHandler
from .schema import ToolValidationError, validate_args

logger = logging.getLogger("dotclaw.tools.function_handler")


class FunctionToolHandler(ToolHandler):
    """将 @tool 装饰的本地函数包装为统一的 ToolHandler。

    约定：函数第一个位置参数接收已验证的 args_model 实例（命名通常为 args，
    args_style="model"），或在签名推导场景下各业务形参直接对应模型字段
    （args_style="fields"）；可选的 context 参数接收 ToolExecutionContext。
    """

    def __init__(self, func: Callable[..., Any], meta: ToolMeta) -> None:
        self._func = func
        self._meta = meta
        # 构造时分析一次函数签名，缓存 args / context 形参名。
        self._call_plan = self._analyze_signature(func)

    @staticmethod
    def _analyze_signature(func: Callable[..., Any]) -> dict[str, str | None]:
        """在构造时分析一次函数签名，确定 args 与 context 形参名。

        这样 execute() 在每次调用时只需按缓存的形参名填入，不再即时猜测 _context。
        优先 eval_str=True 解析字符串注解；失败降级为非 eval（注解按名称判定上下文）。
        """
        try:
            params = inspect.signature(func, eval_str=True).parameters.values()
        except Exception:
            params = inspect.signature(func, eval_str=False).parameters.values()
        plan: dict[str, str | None] = {"args_param": None, "context_param": None}
        for param in params:
            if param.name in ("context", "ctx") or param.annotation in (
                ToolExecutionContext,
                ToolContext,
            ):
                plan["context_param"] = param.name
            elif plan["args_param"] is None:
                plan["args_param"] = param.name
        return plan

    def definition(self) -> ToolDefinition:
        """返回面向 LLM 的工具定义。"""
        return self._meta.build_definition()

    @property
    def args_model(self) -> type[BaseModel] | None:
        """暴露参数模型，供上层（阶段三 executor）做统一校验。"""
        return self._meta.args_model

    async def execute(
        self,
        arguments: Any,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        """执行本地函数并规范化结果。

        Args:
            arguments: 原始参数字典，或已由上层校验过的 args_model 实例。
            context: Runtime 注入的每调用上下文。

        注意：本方法不再猜测 _context；校验为幂等（已验证模型直接透传）。
        """
        model = self._meta.args_model
        # 阶段二：在 handler 入口完成校验（对已是模型实例的参数幂等）。
        # 阶段三由 executor 统一校验后传入模型，此分支变为纯透传。
        if model is not None and not isinstance(arguments, model):
            try:
                arguments = validate_args(model, arguments)
            except ToolValidationError as exc:
                return ToolResult.from_error(
                    code=ToolErrorCode.INVALID_ARGUMENTS,
                    message=str(exc),
                    error_type=ToolErrorType.VALIDATION,
                )
        validated = arguments

        # 按 args_style 把已验证参数打包为函数调用实参。
        kwargs: dict[str, Any] = {}
        if self._meta.args_style == "fields" and model is not None:
            # 签名推导：模型字段拆包到函数各业务形参。
            kwargs.update(validated.model_dump())
        elif model is not None and self._call_plan["args_param"] is not None:
            # 显式模型：整个模型作为单一 args 形参传入。
            kwargs[self._call_plan["args_param"]] = validated
        if self._call_plan["context_param"] is not None:
            kwargs[self._call_plan["context_param"]] = context or ToolExecutionContext()

        try:
            result = self._func(**kwargs)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:  # 业务异常统一转为 EXECUTION_ERROR
            logger.exception("工具 %s 执行出错", self._meta.name)
            return ToolResult.from_error(
                code=ToolErrorCode.EXECUTION_ERROR,
                message=f"工具执行出错: {exc}",
                error_type=ToolErrorType.EXECUTION,
            )

        # 结构化 ToolResult 直接透传；其余返回值转为 output 文本。
        if isinstance(result, ToolResult):
            return result
        return ToolResult(output=str(result))
