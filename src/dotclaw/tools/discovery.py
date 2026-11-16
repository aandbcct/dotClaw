"""工具发现（Tool v1 阶段二新增）。

职责：导入可信工具包（dotclaw.tools.builtin 及其子模块），收集被 @tool 标记的
函数，按元数据构造 FunctionToolHandler。对于未显式提供 args_model 的函数，尝试
从签名推导等价 Pydantic 模型；不满足支持范围的签名必须抛 ToolDeclarationError，
绝不降级为无校验调用（规则见总体设计 §4.1）。

依赖方向：discovery 只依赖 decorator / schema / function_handler / registry，
不执行工具、不扫描任意工作区目录。所有新增注释使用中文。
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from dataclasses import dataclass, field, replace
from enum import Enum
from math import isfinite
from types import UnionType
from typing import Annotated, Any, Callable, Literal, Union, get_args, get_origin, get_type_hints

from pydantic import AfterValidator, BeforeValidator, BaseModel, ConfigDict, create_model
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from .base import ToolExecutionContext, ToolSource
from .decorator import ToolMeta, get_tool_meta
from .function_handler import FunctionToolHandler
from .handler import ToolHandler
from .registry import DuplicateToolError

logger = logging.getLogger("dotclaw.tools.discovery")

# 签名推导支持的 JSON 标量类型（总体设计 §4.1）。
_BASIC_TYPES = (str, int, float, bool)


class ToolDeclarationError(Exception):
    """工具声明非法（如签名不可推导），Discovery 必须直接抛出，禁止降级。"""

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self.reason = reason
        super().__init__(f"工具声明非法 '{name}': {reason}")


@dataclass
class DiscoveryReport:
    """发现过程的观测记录，用于显式暴露导入失败、发现结果与冲突。"""

    imported_modules: list[str] = field(default_factory=list)
    failed_modules: list[str] = field(default_factory=list)
    discovered: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


@dataclass
class DiscoveryResult:
    """一次发现的产出：handler 列表与观测报告。"""

    handlers: list[ToolHandler]
    report: DiscoveryReport


class ToolDiscovery:
    """可信包工具发现器。

    唯一公开入口为 discover_builtin()；scan() 额外返回观测报告，便于测试与排障。
    """

    @staticmethod
    def discover_builtin(package: str = "dotclaw.tools.builtin") -> list[ToolHandler]:
        """扫描可信包并返回已构造的 ToolHandler 列表。

        发现或推导冲突会导致初始化失败（DuplicateToolError / ToolDeclarationError）。
        """
        return ToolDiscovery.scan(package).handlers

    @staticmethod
    def scan(package: str) -> DiscoveryResult:
        """扫描包并收集 @tool 函数，返回 handler 列表与观测报告。"""
        report = DiscoveryReport()
        handlers: dict[str, ToolHandler] = {}

        for func in _iter_tool_functions(package, report):
            meta = get_tool_meta(func)
            if meta is None:
                continue

            # 未显式提供 args_model 时尝试从签名推导；推导失败直接抛错。
            if meta.args_model is None:
                inferred = _infer_args_model(func, meta.name)
                meta = _with_inferred(meta, inferred)
            else:
                # 显式模型一律按单一 args 形参传入。
                meta = replace(meta, args_style="model")

            handler = FunctionToolHandler(func, meta)
            if handler.name in handlers:
                # 先记入观测报告的冲突列表（计划 §4 要求显式记录冲突），
                # 再按 §4.2「启动失败」抛错，绝不覆盖已有定义。
                report.conflicts.append(handler.name)
                existing = handlers[handler.name]
                raise DuplicateToolError(
                    handler.name,
                    existing.definition().source.value,
                    handler.definition().source.value,
                )
            handlers[handler.name] = handler
            report.discovered.append(handler.name)

        return DiscoveryResult(handlers=list(handlers.values()), report=report)


def _iter_tool_functions(package: str, report: DiscoveryReport) -> list[Callable[..., Any]]:
    """导入包及其子模块，收集所有被 @tool 标记的可调用对象。

    导入失败的子模块记入 report.failed_modules，不中断整体发现。
    """
    try:
        pkg = importlib.import_module(package)
    except Exception as exc:  # 包本身导入失败
        logger.warning("工具包 %s 导入失败: %s", package, exc)
        report.failed_modules.append(package)
        return []

    report.imported_modules.append(package)
    modules = [pkg]
    if hasattr(pkg, "__path__"):
        for mod_info in pkgutil.iter_modules(pkg.__path__, package + "."):
            try:
                modules.append(importlib.import_module(mod_info.name))
                report.imported_modules.append(mod_info.name)
            except Exception as exc:
                logger.warning("工具子模块 %s 导入失败: %s", mod_info.name, exc)
                report.failed_modules.append(mod_info.name)

    funcs: list[Callable[..., Any]] = []
    for mod in modules:
        for name in dir(mod):
            obj = getattr(mod, name)
            if callable(obj) and get_tool_meta(obj) is not None:
                funcs.append(obj)
    return funcs


def _safe_signature(func: Callable[..., Any]) -> inspect.Signature:
    """解析参数结构；完整注解由 _resolve_annotations 严格解析，失败不降级。"""
    try:
        return inspect.signature(func, eval_str=True)
    except Exception:
        return inspect.signature(func, eval_str=False)


def _resolve_annotations(func: Callable[..., Any], tool_name: str) -> dict[str, Any]:
    """解析完整注解并保留 Annotated 元数据，失败即在发现阶段拒绝工具。"""
    try:
        return get_type_hints(func, include_extras=True)
    except Exception as exc:
        raise ToolDeclarationError(tool_name, f"函数注解无法解析: {exc}") from exc


def _infer_args_model(func: Callable[..., Any], tool_name: str) -> type[BaseModel]:
    """从函数签名推导等价 Pydantic 模型。

    为直接签名创建唯一的内部参数模型；上下文不进入模型字段。
    """
    sig = _safe_signature(func)
    annotations = _resolve_annotations(func, tool_name)
    business: list[inspect.Parameter] = []
    contexts = 0
    for param in sig.parameters.values():
        annotation = annotations.get(param.name, inspect.Parameter.empty)
        if annotation is ToolExecutionContext:
            contexts += 1
            if param.kind not in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY,
            ):
                raise ToolDeclarationError(tool_name, "上下文参数必须是命名参数")
            if param.default is not inspect.Parameter.empty:
                raise ToolDeclarationError(tool_name, "上下文参数不能有默认值")
            continue
        if param.name in {"context", "ctx"}:
            raise ToolDeclarationError(tool_name, f"业务参数名 '{param.name}' 为保留名称")
        if param.kind not in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            raise ToolDeclarationError(tool_name, f"参数 '{param.name}' 不是命名参数或 keyword-only")
        if annotation is inspect.Parameter.empty:
            raise ToolDeclarationError(tool_name, f"参数 '{param.name}' 缺少类型注解")
        if isinstance(param.default, FieldInfo):
            raise ToolDeclarationError(tool_name, f"参数 '{param.name}' 的默认值必须写在函数签名中")
        _validate_annotation(annotation, tool_name, param.name, set())
        business.append(param)

    if contexts > 1:
        raise ToolDeclarationError(tool_name, "最多允许一个上下文参数")

    fields: dict[str, Any] = {}
    for param in business:
        ann = annotations[param.name]
        if param.default is inspect.Parameter.empty:
            fields[param.name] = (ann, ...)
        else:
            fields[param.name] = (ann, param.default)

    model_name = f"{getattr(func, '__name__', 'tool')}Args"
    try:
        model = create_model(model_name, __config__=ConfigDict(extra="forbid", validate_default=True), **fields)
        from .schema import to_json_schema

        to_json_schema(model)
        return model
    except ToolDeclarationError:
        raise
    except Exception as exc:
        raise ToolDeclarationError(tool_name, f"参数 schema 无法生成: {exc}") from exc


def _validate_annotation(
    annotation: Any,
    tool_name: str,
    parameter: str,
    model_stack: set[type[BaseModel]],
    *,
    check_field_metadata: bool = True,
) -> None:
    """验证声明只使用支持的业务类型，并拒绝循环 Pydantic schema。"""
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Annotated:
        base, *metadata = args
        for item in metadata:
            if isinstance(item, FieldInfo):
                if check_field_metadata and (item.default is not PydanticUndefined or item.default_factory is not None
                        or item.alias is not None or item.validation_alias is not None
                        or item.serialization_alias is not None):
                    raise ToolDeclarationError(tool_name, f"参数 '{parameter}' 的 Field 不支持默认值或 alias")
            elif not isinstance(item, (BeforeValidator, AfterValidator)):
                raise ToolDeclarationError(tool_name, f"参数 '{parameter}' 的 Annotated 元数据不受支持")
        _validate_annotation(base, tool_name, parameter, model_stack, check_field_metadata=check_field_metadata)
        return
    if annotation in _BASIC_TYPES:
        return
    if annotation is Any or annotation is inspect.Parameter.empty or isinstance(annotation, str):
        raise ToolDeclarationError(tool_name, f"参数 '{parameter}' 的类型不受支持或未解析")
    if origin is Literal:
        if not args or any(not _is_json_scalar(item) for item in args):
            raise ToolDeclarationError(tool_name, f"参数 '{parameter}' 的 Literal 必须是 JSON 标量")
        return
    if origin in (Union, UnionType):
        if len(args) != 2 or type(None) not in args:
            raise ToolDeclarationError(tool_name, f"参数 '{parameter}' 只支持单类型可空联合")
        branch = args[0] if args[1] is type(None) else args[1]
        _validate_annotation(branch, tool_name, parameter, model_stack, check_field_metadata=check_field_metadata)
        return
    if origin is list:
        if len(args) != 1:
            raise ToolDeclarationError(tool_name, f"参数 '{parameter}' 的 list 必须声明元素类型")
        _validate_annotation(args[0], tool_name, parameter, model_stack, check_field_metadata=check_field_metadata)
        return
    if origin is dict:
        if len(args) != 2 or args[0] is not str:
            raise ToolDeclarationError(tool_name, f"参数 '{parameter}' 只支持 dict[str, T]")
        _validate_annotation(args[1], tool_name, parameter, model_stack, check_field_metadata=check_field_metadata)
        return
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        if any(not _is_json_scalar(member.value) for member in annotation):
            raise ToolDeclarationError(tool_name, f"参数 '{parameter}' 的 Enum 值必须是 JSON 标量")
        return
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if annotation in model_stack:
            raise ToolDeclarationError(tool_name, f"参数 '{parameter}' 的嵌套模型包含循环 schema")
        next_stack = model_stack | {annotation}
        for field_info in annotation.model_fields.values():
            _validate_annotation(
                field_info.annotation, tool_name, parameter, next_stack,
                check_field_metadata=False,
            )
        return
    raise ToolDeclarationError(tool_name, f"参数 '{parameter}' 的类型 {_type_label(annotation)} 不受支持")


def _is_json_scalar(value: Any) -> bool:
    return value is None or type(value) in (str, int, float, bool) and (not isinstance(value, float) or isfinite(value))


def _with_inferred(meta: ToolMeta, inferred: type[BaseModel]) -> ToolMeta:
    """基于推导出的模型生成新元数据（args_style='fields'）。"""
    return replace(meta, args_model=inferred, args_style="fields")


def _type_label(ann: Any) -> str:
    """为不支持的类型生成可读标签，便于错误诊断（不含输入值）。"""
    name = getattr(ann, "__name__", str(ann))
    if hasattr(ann, "__origin__") or str(ann).startswith("typing."):
        return f"复合类型({name})"
    return name
