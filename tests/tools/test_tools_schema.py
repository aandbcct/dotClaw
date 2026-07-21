"""tools.schema 的单元与契约测试（阶段一）。"""

from __future__ import annotations

from enum import Enum
from typing import Optional

import pytest
from pydantic import BaseModel, Field

from dotclaw.tools.base import (
    ToolErrorCode,
    ToolErrorType,
    ToolExecutionContext,
    ToolResult,
)
from dotclaw.tools.decorator import get_tool_meta, tool
from dotclaw.tools.function_handler import FunctionToolHandler
from dotclaw.tools.schema import (
    ToolValidationError,
    to_json_schema,
    validate_args,
)


class Color(str, Enum):
    RED = "red"
    GREEN = "green"


class SampleArgs(BaseModel):
    name: str = Field(description="名称")
    age: int = 18
    tag: Optional[str] = None


class NestedArgs(BaseModel):
    inner: SampleArgs
    items: list[int]


class EnumArgs(BaseModel):
    color: Color


# 调用计数，用于验证校验失败时工具函数不会被执行。
_CALL_COUNT = {"n": 0}


@tool(name="demo.spy", args_model=SampleArgs, description="调用探针")
async def _spy_tool(args: SampleArgs) -> str:
    _CALL_COUNT["n"] += 1
    return "ran"


def test_json_schema_basic_structure() -> None:
    """基础模型：类型、必填、描述、默认值应正确转换。"""
    schema = to_json_schema(SampleArgs)
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"name"}
    assert schema["properties"]["name"]["type"] == "string"
    assert schema["properties"]["name"]["description"] == "名称"
    assert schema["properties"]["age"]["default"] == 18


def test_json_schema_nested_and_array() -> None:
    """嵌套模型与数组应递归转换为 JSON Schema。"""
    schema = to_json_schema(NestedArgs)
    assert schema["properties"]["inner"]["type"] == "object"
    assert schema["properties"]["items"]["type"] == "array"
    assert schema["properties"]["items"]["items"]["type"] == "integer"


def test_json_schema_enum() -> None:
    """枚举字段应转换为 JSON Schema 的 enum 列表。"""
    schema = to_json_schema(EnumArgs)
    assert set(schema["properties"]["color"]["enum"]) == {"red", "green"}


def test_json_schema_strips_titles() -> None:
    """生成的 Schema 应去掉 Pydantic 默认 title，避免 LLM 冗余。"""
    schema = to_json_schema(SampleArgs)
    assert "title" not in schema
    for prop in schema["properties"].values():
        assert "title" not in prop


def test_validate_missing_required_raises_invalid_arguments() -> None:
    """缺失必填字段应返回 INVALID_ARGUMENTS。"""
    with pytest.raises(ToolValidationError) as exc:
        validate_args(SampleArgs, {})
    assert exc.value.error_code == ToolErrorCode.INVALID_ARGUMENTS
    assert exc.value.error_type == ToolErrorType.VALIDATION
    assert "name" in str(exc.value)


def test_validate_wrong_type_raises_invalid_arguments() -> None:
    """错误类型应返回 INVALID_ARGUMENTS。"""
    with pytest.raises(ToolValidationError) as exc:
        validate_args(SampleArgs, {"name": 123})
    assert exc.value.error_code == ToolErrorCode.INVALID_ARGUMENTS


def test_validate_unknown_field_rejected() -> None:
    """未知字段应被严格拒绝，错误信息包含字段名，错误码为 INVALID_ARGUMENTS。"""
    with pytest.raises(ToolValidationError) as exc:
        validate_args(SampleArgs, {"name": "x", "unknown_field": 1})
    assert exc.value.error_code == ToolErrorCode.INVALID_ARGUMENTS
    assert "unknown_field" in str(exc.value)


async def test_validation_failure_blocks_tool_execution() -> None:
    """校验失败时必须短路：工具函数（handler）绝不执行；只有校验通过才执行。

    模拟 Executor 的固定顺序：先校验，失败直接返回 INVALID_ARGUMENTS，
    绝不进入 handler；校验通过才调用被装饰函数。
    """
    handler = FunctionToolHandler(_spy_tool, get_tool_meta(_spy_tool))

    # 失败路径：错型 + 未知字段。
    _CALL_COUNT["n"] = 0
    raw_bad = {"name": 123, "unknown_field": 1}
    result = None
    try:
        validated = validate_args(SampleArgs, raw_bad)
    except ToolValidationError as exc:
        result = ToolResult.from_error(
            code=exc.error_code, message=str(exc), error_type=exc.error_type
        )
    else:
        result = await handler.execute(validated, ToolExecutionContext())

    assert result is not None and result.is_error
    assert result.error_code == ToolErrorCode.INVALID_ARGUMENTS.value
    assert _CALL_COUNT["n"] == 0  # 工具函数从未执行

    # 成功路径：校验通过，工具函数应当执行一次。
    _CALL_COUNT["n"] = 0
    validated_ok = validate_args(SampleArgs, {"name": "ok"})
    await handler.execute(validated_ok, ToolExecutionContext())
    assert _CALL_COUNT["n"] == 1


def test_validate_success_returns_instance_with_defaults() -> None:
    """校验成功应返回已验证的 Pydantic 实例并保留默认值。"""
    inst = validate_args(SampleArgs, {"name": "alice"})
    assert inst.name == "alice"
    assert inst.age == 18
    assert inst.tag is None


def test_validation_error_message_does_not_leak_values() -> None:
    """校验错误描述不得泄露用户输入值（如 123）。"""
    with pytest.raises(ToolValidationError) as exc:
        validate_args(SampleArgs, {"name": 123})
    assert "123" not in str(exc.value)
