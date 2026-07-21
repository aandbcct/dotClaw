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
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, create_model

from .base import ToolExecutionContext, ToolSource
from .decorator import ToolMeta, get_tool_meta
from .function_handler import FunctionToolHandler
from .handler import ToolHandler
from .registry import DuplicateToolError

logger = logging.getLogger("dotclaw.tools.discovery")

# 签名推导支持的基础标量类型（总体设计 §4.1）。
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
                meta = ToolMeta(
                    name=meta.name,
                    description=meta.description,
                    args_model=meta.args_model,
                    policy=meta.policy,
                    source=meta.source,
                    needs_approval=meta.needs_approval,
                    timeout=meta.timeout,
                    metadata=dict(meta.metadata),
                    args_style="model",
                )

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
    """解析函数签名；优先 eval_str=True（解析字符串注解），失败则降级为非 eval。

    生产 builtin 的类型均在模块级导入，可正常解析；仅测试或异常场景会降级。
    降级后注解保留为字符串，调用方按严格规则判为不支持（拒绝而非降级）。
    """
    try:
        return inspect.signature(func, eval_str=True)
    except Exception:
        return inspect.signature(func, eval_str=False)


def _is_context_param(param: inspect.Parameter) -> bool:
    """判断形参是否为运行上下文（按名称或注解）。"""
    return param.name in ("context", "ctx") or param.annotation in (
        ToolExecutionContext,
        ToolExecutionContext,  # 别名兼容
    )


def _infer_args_model(func: Callable[..., Any], tool_name: str) -> type[BaseModel] | None:
    """从函数签名推导等价 Pydantic 模型。

    支持范围（总体设计 §4.1）：
    - 零参函数 → 返回 None（无参对象）。
    - 全部业务参数为 str/int/float/bool 的普通位置/关键字参数（允许字面量默认值）。

    以下一律不支持，直接抛 ToolDeclarationError：
    Optional/Union、容器、枚举、嵌套模型、Annotated 约束、仅位置参数、
    *args、**kwargs、自定义类型、缺少类型注解。
    """
    sig = _safe_signature(func)
    business: list[inspect.Parameter] = []
    for param in sig.parameters.values():
        if _is_context_param(param):
            continue
        business.append(param)

    if not business:
        return None

    fields: dict[str, Any] = {}
    for param in business:
        ann = param.annotation
        if ann is inspect.Parameter.empty or ann is None:
            raise ToolDeclarationError(
                tool_name, f"参数 '{param.name}' 缺少类型注解，无法推导"
            )
        if param.kind not in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            raise ToolDeclarationError(
                tool_name,
                f"参数 '{param.name}' 为仅位置/*args/**kwargs，不在推导支持范围",
            )
        if isinstance(ann, str):
            # 注解无法解析（如局部定义的类型）；严格按不支持拒绝，绝不降级。
            raise ToolDeclarationError(
                tool_name,
                f"参数 '{param.name}' 的注解无法解析，不在推导支持范围",
            )
        if ann not in _BASIC_TYPES:
            # 覆盖 Optional/Union、容器、枚举、嵌套模型、Annotated、自定义类型。
            raise ToolDeclarationError(
                tool_name,
                f"参数 '{param.name}' 的类型 {_type_label(ann)} 不在推导支持范围"
                f"（仅支持 str/int/float/bool）",
            )
        if param.default is inspect.Parameter.empty:
            fields[param.name] = (ann, ...)
        else:
            fields[param.name] = (ann, param.default)

    model_name = f"{getattr(func, '__name__', 'tool')}Args"
    return create_model(model_name, **fields)


def _with_inferred(meta: ToolMeta, inferred: type[BaseModel] | None) -> ToolMeta:
    """基于推导出的模型生成新元数据（args_style='fields'）。"""
    return ToolMeta(
        name=meta.name,
        description=meta.description,
        args_model=inferred,
        policy=meta.policy,
        source=meta.source,
        needs_approval=meta.needs_approval,
        timeout=meta.timeout,
        metadata=dict(meta.metadata),
        args_style="fields" if inferred is not None else "model",
    )


def _type_label(ann: Any) -> str:
    """为不支持的类型生成可读标签，便于错误诊断（不含输入值）。"""
    name = getattr(ann, "__name__", str(ann))
    if hasattr(ann, "__origin__") or str(ann).startswith("typing."):
        return f"复合类型({name})"
    return name
