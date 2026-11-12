"""ToolDiscovery 测试（Tool v1 阶段二）。

覆盖：可信包自动发现、签名推导成功（零参/基础类型）、每类不支持签名在 Discovery
阶段抛出 ToolDeclarationError（绝不降级为无校验）、导入失败被记录不中断。
所有新增注释使用中文。
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Optional

import pytest
from pydantic import BaseModel, Field

from dotclaw.tools.base import ToolExecutionContext
from dotclaw.tools.decorator import ToolMeta, tool
from dotclaw.tools.discovery import (
    ToolDeclarationError,
    ToolDiscovery,
    _infer_args_model,
)


class DiscoveryInner(BaseModel):
    value: int


class DiscoveryColor(str, Enum):
    RED = "red"
    BLUE = "blue"


class RecursiveModel(BaseModel):
    child: RecursiveModel | None = None


def _handler_by_name(handlers, name):
    for h in handlers:
        if h.name == name:
            return h
    raise AssertionError(f"未发现的工具: {name}")


# ── 自动发现 ──
def test_discover_sample_tools_returns_all_handlers() -> None:
    handlers = ToolDiscovery.discover_builtin("tests.tools.sample_tools")
    names = {h.name for h in handlers}
    assert names == {"sample.zero_arg", "sample.basic_fields", "sample.explicit"}


def test_discovery_report_records_import_failure_without_crashing() -> None:
    result = ToolDiscovery.scan("tests.tools.sample_tools_broken")
    # 即便同包子模块导入失败，正常工具仍被发现。
    assert any(h.name == "sample.broken_pkg.ok" for h in result.handlers)
    assert any("crash" in m for m in result.report.failed_modules)


# ── 签名推导成功 ──
async def test_inferred_tool_executes_and_validates() -> None:
    handlers = ToolDiscovery.discover_builtin("tests.tools.sample_tools")
    handler = _handler_by_name(handlers, "sample.basic_fields")
    # 正常参数：推导模型拆包到函数各形参。
    res = await handler.execute({"path": "a", "count": 2}, ToolExecutionContext())
    assert res.output == "a:2"
    # 未知字段：严格拒绝为 INVALID_ARGUMENTS，且不执行工具逻辑。
    res_bad = await handler.execute(
        {"path": "a", "extra": 1}, ToolExecutionContext()
    )
    assert res_bad.is_error
    assert res_bad.error_code == "INVALID_ARGUMENTS"


async def test_zero_arg_tool_executes() -> None:
    handlers = ToolDiscovery.discover_builtin("tests.tools.sample_tools")
    handler = _handler_by_name(handlers, "sample.zero_arg")
    res = await handler.execute({}, ToolExecutionContext())
    assert res.output == "ok"


async def test_explicit_model_tool_executes() -> None:
    handlers = ToolDiscovery.discover_builtin("tests.tools.sample_tools")
    handler = _handler_by_name(handlers, "sample.explicit")
    res = await handler.execute({"name": "z"}, ToolExecutionContext())
    assert res.output == "z"


# ── 不支持签名：Discovery 直接抛错，绝不降级 ──
def test_discover_bad_package_raises_tool_declaration_error() -> None:
    with pytest.raises(ToolDeclarationError):
        ToolDiscovery.discover_builtin("tests.tools.sample_tools_bad")


# ── _infer_args_model 单元：每类不支持签名 ──
def _meta(name: str) -> ToolMeta:
    return ToolMeta(name=name, description="x")


async def _noop() -> str:
    return "x"


def test_infer_zero_arg_returns_empty_model() -> None:
    async def f() -> str:
        return "x"

    model = _infer_args_model(f, "f")
    assert model.model_fields == {}
    assert model.model_config["extra"] == "forbid"
    assert model.model_config["validate_default"] is True
    assert model.model_json_schema()["additionalProperties"] is False


def test_infer_basic_fields_succeeds() -> None:
    async def f(path: str, count: int = 3) -> str:
        return "x"

    model = _infer_args_model(f, "f")
    assert model is not None
    assert set(model.model_fields.keys()) == {"path", "count"}


def _expect_declaration(func, name: str) -> None:
    with pytest.raises(ToolDeclarationError):
        _infer_args_model(func, name)


def test_infer_supports_required_nullable_type() -> None:
    async def f(x: Optional[int]) -> str:
        return "x"

    model = _infer_args_model(f, "f")
    assert model.model_fields["x"].is_required()


def test_infer_rejects_union() -> None:
    async def f(x: "int | str") -> str:  # type: ignore[misc]
        return "x"

    _expect_declaration(f, "f")


def test_infer_supports_typed_list() -> None:
    async def f(items: list[str]) -> str:
        return "x"

    model = _infer_args_model(f, "f")
    assert model.model_fields["items"].annotation == list[str]


def test_infer_supports_json_enum() -> None:
    async def f(c: DiscoveryColor) -> str:
        return "x"

    model = _infer_args_model(f, "f")
    assert model.model_fields["c"].annotation is DiscoveryColor


def test_infer_supports_nested_model() -> None:
    async def f(x: DiscoveryInner) -> str:
        return "x"

    model = _infer_args_model(f, "f")
    assert model.model_fields["x"].annotation is DiscoveryInner


def test_infer_preserves_annotated_field_constraints() -> None:
    async def f(x: Annotated[int, Field(ge=1)]) -> str:
        return "x"

    model = _infer_args_model(f, "f")
    assert model.model_json_schema()["properties"]["x"]["minimum"] == 1


def test_infer_rejects_any_and_reserved_context_name() -> None:
    async def any_value(value: Any) -> str:
        return "x"

    async def reserved(context: str) -> str:
        return "x"

    _expect_declaration(any_value, "any_value")
    _expect_declaration(reserved, "reserved")


def test_infer_context_is_keyword_only_and_not_in_schema() -> None:
    async def f(value: int, *, runtime: ToolExecutionContext) -> str:
        return str(value)

    model = _infer_args_model(f, "f")
    assert set(model.model_fields) == {"value"}


def test_infer_rejects_non_keyword_context_parameters() -> None:
    async def positional_only(context: ToolExecutionContext, /) -> str:
        return "x"

    async def variadic_context(*context: ToolExecutionContext) -> str:
        return "x"

    _expect_declaration(positional_only, "positional_only")
    _expect_declaration(variadic_context, "variadic_context")


def test_infer_rejects_field_defaults_aliases_and_default_factory() -> None:
    async def field_default(value: Annotated[int, Field(default=2)]) -> str:
        return "x"

    async def alias(value: Annotated[int, Field(alias="renamed")]) -> str:
        return "x"

    async def factory(value: list[int] = Field(default_factory=list)) -> str:
        return "x"

    _expect_declaration(field_default, "field_default")
    _expect_declaration(alias, "alias")
    _expect_declaration(factory, "factory")


def test_infer_rejects_recursive_nested_schema() -> None:
    async def f(value: RecursiveModel) -> str:
        return "x"

    _expect_declaration(f, "f")


def test_infer_rejects_positional_only() -> None:
    async def f(x: int, /) -> str:
        return "x"

    _expect_declaration(f, "f")


def test_infer_rejects_var_args() -> None:
    async def f(*args: int) -> str:
        return "x"

    _expect_declaration(f, "f")


def test_infer_rejects_var_kwargs() -> None:
    async def f(**kwargs: int) -> str:
        return "x"

    _expect_declaration(f, "f")


def test_infer_rejects_custom_type() -> None:
    class MyType:
        pass

    async def f(x: MyType) -> str:  # type: ignore[valid-type]
        return "x"

    _expect_declaration(f, "f")


def test_infer_rejects_missing_annotation() -> None:
    async def f(x) -> str:  # type: ignore[no-untyped-def]
        return "x"

    _expect_declaration(f, "f")
