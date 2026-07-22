"""tools.function_handler 的契约测试（阶段一）。"""

from __future__ import annotations

import inspect

import pytest
from pydantic import BaseModel

from dotclaw.tools.base import (
    ToolExecutionContext,
    ToolResult,
    ToolErrorCode,
)
from dotclaw.tools.decorator import get_tool_meta, tool
from dotclaw.tools.function_handler import FunctionToolHandler
from dotclaw.tools.schema import validate_args


class AddArgs(BaseModel):
    a: int
    b: int


@tool(name="demo.add", args_model=AddArgs, description="加法")
async def add(args: AddArgs) -> str:
    return str(args.a + args.b)


@tool(name="demo.struct", args_model=AddArgs, description="结构化返回")
async def structured(args: AddArgs) -> ToolResult:
    return ToolResult(output="ok", metadata={"sum": args.a + args.b})


@tool(name="demo.err", args_model=AddArgs, description="异常")
async def boom(args: AddArgs) -> str:
    raise ValueError("boom")


@tool(name="demo.context", args_model=AddArgs, description="需要上下文")
async def with_context(args: AddArgs, context: ToolExecutionContext) -> str:
    return context.agentrun_id


async def test_function_handler_success_returns_output() -> None:
    """成功调用应返回 str 结果作为 output。"""
    handler = FunctionToolHandler(add, get_tool_meta(add))
    result = await handler.execute(
        validate_args(AddArgs, {"a": 2, "b": 3}), ToolExecutionContext()
    )
    assert result.output == "5"
    assert not result.is_error


async def test_function_handler_structured_tool_result_passthrough() -> None:
    """返回 ToolResult 时直接透传，不二次包装。"""
    handler = FunctionToolHandler(structured, get_tool_meta(structured))
    result = await handler.execute(
        validate_args(AddArgs, {"a": 1, "b": 2}), ToolExecutionContext()
    )
    assert result.output == "ok"
    assert result.metadata["sum"] == 3


async def test_function_handler_business_exception_maps_to_execution_error() -> None:
    """业务异常应统一映射为 EXECUTION_ERROR。"""
    handler = FunctionToolHandler(boom, get_tool_meta(boom))
    result = await handler.execute(
        validate_args(AddArgs, {"a": 1, "b": 2}), ToolExecutionContext()
    )
    assert result.is_error
    assert result.error_code == ToolErrorCode.EXECUTION_ERROR.value
    assert result.error_type == "execution"


async def test_function_handler_injects_context_when_param_present() -> None:
    """函数存在 context 形参时，应注入 Runtime 上下文。"""
    handler = FunctionToolHandler(with_context, get_tool_meta(with_context))
    ctx = ToolExecutionContext(agentrun_id="run-42")
    result = await handler.execute(
        validate_args(AddArgs, {"a": 1, "b": 1}), ctx
    )
    assert result.output == "run-42"


async def test_function_handler_definition_uses_args_model_schema() -> None:
    """definition 的 parameters 应来自 args_model 的 JSON Schema。"""
    handler = FunctionToolHandler(add, get_tool_meta(add))
    definition = handler.definition()
    assert definition.name == "demo.add"
    assert definition.parameters["type"] == "object"
    assert "a" in definition.parameters["properties"]


async def test_signature_analyzed_once_not_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """函数签名只在构造时分析一次，每次调用不再 inspect.signature 猜测 _context。"""
    calls = {"n": 0}
    original = inspect.signature

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(inspect, "signature", counting)
    handler = FunctionToolHandler(add, get_tool_meta(add))
    assert calls["n"] == 1  # 构造时分析一次
    await handler.execute(validate_args(AddArgs, {"a": 1, "b": 1}), ToolExecutionContext())
    await handler.execute(validate_args(AddArgs, {"a": 1, "b": 1}), ToolExecutionContext())
    assert calls["n"] == 1  # 调用期间不再分析
