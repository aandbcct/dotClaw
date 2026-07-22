"""阶段四：MCP 参数 JSON Schema 校验适配层测试（validate_json_schema）。

覆盖：未知字段拒绝、必填缺失、类型错误、enum、数组、additionalProperties 放行、
不支持的组合子（$ref）保守降级。
"""

from __future__ import annotations

import pytest

from dotclaw.tools.schema import validate_json_schema, ToolValidationError


def test_valid_object_passes():
    schema = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
    assert validate_json_schema({"q": "hi"}, schema) == {"q": "hi"}


def test_unknown_field_rejected():
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    with pytest.raises(ToolValidationError):
        validate_json_schema({"q": "hi", "extra": 1}, schema)


def test_additional_properties_true_allows_unknown():
    schema = {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "additionalProperties": True,
    }
    assert validate_json_schema({"q": "hi", "extra": 1}, schema) == {"q": "hi", "extra": 1}


def test_missing_required_rejected():
    schema = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
    with pytest.raises(ToolValidationError):
        validate_json_schema({}, schema)


def test_wrong_type_rejected():
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    with pytest.raises(ToolValidationError):
        validate_json_schema({"n": "not-int"}, schema)


def test_enum_rejected_when_out_of_range():
    schema = {"type": "object", "properties": {"m": {"enum": ["a", "b"]}}}
    with pytest.raises(ToolValidationError):
        validate_json_schema({"m": "c"}, schema)


def test_array_items_checked():
    schema = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"type": "string"}}},
    }
    with pytest.raises(ToolValidationError):
        validate_json_schema({"items": [1, 2]}, schema)
    assert validate_json_schema({"items": ["a", "b"]}, schema) == {"items": ["a", "b"]}


def test_non_object_rejected():
    schema = {"type": "object", "properties": {}}
    with pytest.raises(ToolValidationError):
        validate_json_schema("not-a-dict", schema)


def test_unsupported_ref_degrades_gracefully():
    # 含 $ref 的组合子无法校验：保守降级为不阻断（仅确认是对象）。
    schema = {"type": "object", "properties": {"q": {"$ref": "#/defs/X"}}}
    assert validate_json_schema({"q": "anything"}, schema) == {"q": "anything"}


def test_empty_schema_accepts_any_object():
    # 无 properties：无法校验，保守降级接受任意对象。
    assert validate_json_schema({"anything": 1, "goes": True}, {}) == {"anything": 1, "goes": True}
