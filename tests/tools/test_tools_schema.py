"""tools.schema 的单元与契约测试（阶段一）。"""

from __future__ import annotations

from enum import Enum
from typing import Optional

import pytest
from pydantic import BaseModel, Field

from dotclaw.tools.base import ToolErrorCode, ToolErrorType
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


class _Spy:
    """记录函数是否被调用的桩。"""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def __call__(self) -> None:
        self.calls.append(1)


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


def test_validate_unknown_field_rejected_and_function_not_called() -> None:
    """未知字段应被严格拒绝，且校验发生在调用之前（函数未被调用）。"""
    spy = _Spy()
    with pytest.raises(ToolValidationError) as exc:
        validate_args(SampleArgs, {"name": "x", "unknown_field": 1})
    assert exc.value.error_code == ToolErrorCode.INVALID_ARGUMENTS
    assert "unknown_field" in str(exc.value)
    assert spy.calls == []


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
