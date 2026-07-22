"""参数 Schema 生成与本地参数校验（Tool v1 阶段一新增）。

职责边界：只做 Pydantic/手写模型到 JSON Schema 的转换、原始参数校验与
规范化、严格未知字段拒绝、校验错误的安全格式化。不负责策略判断或执行。
所有新增注释使用中文。
"""

from __future__ import annotations

import copy
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .base import ToolErrorCode, ToolErrorType, ToolResult

ModelT = TypeVar("ModelT", bound=BaseModel)


class ToolValidationError(Exception):
    """参数校验失败，统一映射为 INVALID_ARGUMENTS。

    不携带任何用户输入值，避免敏感信息进入错误结果或审计。
    """

    def __init__(self, message: str, *, loc: list[str] | None = None) -> None:
        self.error_code = ToolErrorCode.INVALID_ARGUMENTS
        self.error_type = ToolErrorType.VALIDATION
        self.loc: list[str] = loc or []
        super().__init__(message)


def to_json_schema(model: type[BaseModel], *, strip_title: bool = True) -> dict:
    """由 Pydantic 模型生成面向 LLM 的 JSON Schema。

    Pydantic v2 默认把嵌套模型与枚举拆到 $defs 并用 $ref 引用，多数 LLM
    不跟随 $ref，因此这里先内联解析为完整 Schema，再按需去除冗余 title。

    Args:
        model: 工具的 args_model 类。
        strip_title: 是否移除 Pydantic 默认生成的 title 字段（减少 LLM Schema 冗余）。
    """
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})
    raw = _resolve_refs(raw, defs)
    if strip_title:
        raw = _strip_titles(raw)
    return raw


def _resolve_refs(schema: Any, defs: dict, _seen: set[str] | None = None) -> Any:
    """递归把 $ref 解析为内联定义，避免 LLM 无法跟随引用。

    使用 _seen 防止循环引用导致的无限递归；无法解析时保留原 $ref。
    """
    if _seen is None:
        _seen = set()
    if isinstance(schema, dict):
        if "$ref" in schema:
            key = schema["$ref"].split("/")[-1]
            if key in defs and key not in _seen:
                resolved = _resolve_refs(
                    copy.deepcopy(defs[key]), defs, _seen | {key}
                )
                extra = {k: v for k, v in schema.items() if k != "$ref"}
                merged = dict(resolved)
                merged.update(extra)
                return merged
            return schema
        return {k: _resolve_refs(v, defs, _seen) for k, v in schema.items()}
    if isinstance(schema, list):
        return [_resolve_refs(item, defs, _seen) for item in schema]
    return schema


def _strip_titles(schema: dict) -> dict:
    """递归移除 JSON Schema 中的 title 字段，保留 description 等有用信息。"""
    if not isinstance(schema, dict):
        return schema
    result = {key: value for key, value in schema.items() if key != "title"}
    for key, value in result.items():
        if isinstance(value, dict):
            result[key] = _strip_titles(value)
        elif isinstance(value, list):
            result[key] = [
                _strip_titles(item) if isinstance(item, dict) else item
                for item in value
            ]
    return result


def validate_args(model: type[ModelT], raw_args: dict[str, Any]) -> ModelT:
    """校验原始参数字典，返回已验证的 Pydantic 实例。

    严格拒绝未声明字段（即便模型自身允许 extra）；校验失败时抛出
    ToolValidationError，错误码为 INVALID_ARGUMENTS，且不暴露任何输入值。

    Raises:
        ToolValidationError: 参数非法。调用方据此构造 INVALID_ARGUMENTS 结果，
            且不得继续执行工具函数。

    Returns:
        已验证的 Pydantic 模型实例，供 FunctionToolHandler 直接传入工具函数。
    """
    if not isinstance(raw_args, dict):
        raise ToolValidationError("参数必须是对象")
    _reject_unknown_fields(model, raw_args)
    try:
        return model.model_validate(raw_args)
    except ValidationError as exc:
        raise _safe_validation_error(exc) from exc


def _reject_unknown_fields(model: type[BaseModel], raw_args: dict[str, Any]) -> None:
    """显式拒绝模型未声明的字段，确保 extra='forbid' 的语义不被绕过。"""
    allowed: set[str] = set(model.model_fields.keys())
    for field_info in model.model_fields.values():
        if field_info.alias:
            allowed.add(field_info.alias)
    extras = [key for key in raw_args if key not in allowed]
    if extras:
        extras.sort()
        raise ToolValidationError(
            f"未知参数: {', '.join(extras)}",
            loc=extras,
        )


def _safe_validation_error(exc: ValidationError) -> ToolValidationError:
    """将 Pydantic 校验错误转换为不泄露输入值的安全描述。"""
    parts: list[str] = []
    for err in exc.errors(include_url=False, include_context=False):
        loc = ".".join(str(item) for item in err.get("loc", ()))
        message = _safe_message(err.get("type", ""), err.get("ctx"))
        parts.append(f"{loc}: {message}" if loc else message)
    return ToolValidationError("; ".join(parts))


# 已知 Pydantic v2 错误类型到安全中文描述的映射（不含任何输入值）。
_SAFE_ERROR_MESSAGES: dict[str, str] = {
    "missing": "必填参数缺失",
    "extra_forbidden": "存在未声明参数",
    "string_type": "应为字符串",
    "string_too_short": "字符串长度不足",
    "string_too_long": "字符串长度超出限制",
    "int_type": "应为整数",
    "int_parsing": "整数格式不正确",
    "float_type": "应为数字",
    "float_parsing": "数字格式不正确",
    "bool_type": "应为布尔值",
    "bool_parsing": "布尔值格式不正确",
    "list_type": "应为数组",
    "dict_type": "应为对象",
    "enum": "参数取值不在允许范围内",
    "literal_error": "参数取值不在允许范围内",
    "type_error": "参数类型错误",
    "value_error": "参数取值不合法",
}


def _safe_message(error_type: str, ctx: dict | None) -> str:
    """根据错误类型返回安全描述；ctx 中只使用约束阈值，绝不使用输入值。"""
    message = _SAFE_ERROR_MESSAGES.get(error_type, "参数格式不正确")
    if ctx:
        # 仅补充阈值数字，不引用输入内容。
        if "min_length" in ctx:
            message = f"{message}（至少 {ctx['min_length']} 个字符）"
        elif "max_length" in ctx:
            message = f"{message}（最多 {ctx['max_length']} 个字符）"
        elif "gt" in ctx:
            message = f"{message}（须大于 {ctx['gt']}）"
        elif "ge" in ctx:
            message = f"{message}（须不小于 {ctx['ge']}）"
        elif "lt" in ctx:
            message = f"{message}（须小于 {ctx['lt']}）"
        elif "le" in ctx:
            message = f"{message}（须不大于 {ctx['le']}）"
        elif "allowed_values" in ctx:
            values = ", ".join(str(v) for v in ctx["allowed_values"])
            message = f"{message}（允许: {values}）"
    return message


# ---------------------------------------------------------------------------
# MCP / 任意 JSON Schema 校验适配层（总体设计 §4.5）
# ---------------------------------------------------------------------------

# 支持的基础 JSON Schema 类型。其余类型（object 嵌套、array 等）做有限检查，
# 不支持的组合子（$ref / allOf / anyOf / oneOf 等）降级为跳过对应子检查。
_PRIMITIVE_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
}

# 视为“无法校验、保守降级”的 JSON Schema 关键字。
_UNSUPPORTED_SCHEMA_KEYWORDS = ("allOf", "anyOf", "oneOf", "$ref", "$defs")


def validate_json_schema(raw_args: Any, schema: dict) -> dict:
    """按 JSON Schema 校验 MCP 等外部工具的原始参数字典（与 Pydantic 严格语义对齐）。

    覆盖范围（总体设计 §4.5 / 开发计划阶段四）：
    - 参数必须是对象，否则 INVALID_ARGUMENTS。
    - properties 中每个已知字段按声明的原始类型（string/integer/number/boolean）
      与 enum 约束检查；未知类型关键字降级为跳过该项检查（不阻断调用）。
    - required 字段缺失 → INVALID_ARGUMENTS。
    - additionalProperties 缺省视为 false，拒绝未声明字段（与本地 extra='forbid' 对齐）。

    不能校验的 schema（缺 properties / 含 $ref 等组合子）采取明确、保守的降级行为：
    仅确认是对象且不强制未知字段（additionalProperties 未显式 true 时仍按 properties
    拒绝已知之外的字段），不阻断调用、也不做深度类型校验。失败抛出 ToolValidationError，
    错误码 INVALID_ARGUMENTS，且不暴露任何输入值。

    Returns:
        校验通过后的参数字典（可能已做最小类型规整）。
    """
    if not isinstance(raw_args, dict):
        raise ToolValidationError("参数必须是对象")
    if not isinstance(schema, dict):
        # schema 自身非法：保守降级，原样接受（仅确认是 dict）。
        return raw_args

    props: dict = schema.get("properties") or {}
    required: list = schema.get("required") or []
    additional = schema.get("additionalProperties", False)

    # 未知字段拒绝（总体设计 §4.1 extra='forbid' 语义）：
    # - additionalProperties 显式为 true → 放行未知字段；
    # - 否则（false 或省略）且 schema 声明了 properties → 拒绝未知字段；
    # - 否则（无 properties，无法校验）→ 保守降级，放行未知字段（不阻断调用）。
    reject_unknown = (additional is not True) and bool(props)
    if reject_unknown:
        allowed = set(props.keys())
        extras = sorted(k for k in raw_args if k not in allowed)
        if extras:
            raise ToolValidationError(f"未知参数: {', '.join(extras)}", loc=extras)

    # 必填字段检查。
    missing = sorted(r for r in required if r not in raw_args)
    if missing:
        raise ToolValidationError(f"必填参数缺失: {', '.join(missing)}", loc=missing)

    # 已知字段类型/枚举检查；不支持的组合子降级跳过。
    for key, val in raw_args.items():
        if key not in props:
            continue
        _check_json_field(key, val, props[key])

    return raw_args


def _check_json_field(key: str, val: Any, field_schema: Any) -> None:
    """检查单个字段值是否满足其 JSON Schema 声明（不支持的组合子降级跳过）。"""
    if not isinstance(field_schema, dict):
        return
    if any(k in field_schema for k in _UNSUPPORTED_SCHEMA_KEYWORDS):
        return  # 组合子/引用：保守降级，不校验

    enum_values = field_schema.get("enum")
    if enum_values is not None:
        if val not in enum_values:
            raise ToolValidationError(f"{key}: 参数取值不在允许范围内", loc=[key])
        return

    declared = field_schema.get("type")
    if declared is None:
        return
    if declared == "array":
        _check_json_array(key, val, field_schema)
        return
    if declared == "object":
        _check_json_object(key, val, field_schema)
        return
    if declared in _PRIMITIVE_TYPES:
        if not isinstance(val, _PRIMITIVE_TYPES[declared]):
            # bool 是 int 的子类，需排除布尔被误判为整数。
            if declared in ("integer", "number") and isinstance(val, bool):
                raise ToolValidationError(f"{key}: 应为数字", loc=[key])
            raise ToolValidationError(f"{key}: 应为{declared}", loc=[key])
        return
    # 其他声明类型（如 null / 自定义格式）：降级跳过。


def _check_json_array(key: str, val: Any, field_schema: dict) -> None:
    """检查 array 类型：确认是列表，并对 items 做有限类型检查（组合子降级跳过）。"""
    if not isinstance(val, list):
        raise ToolValidationError(f"{key}: 应为数组", loc=[key])
    items_schema = field_schema.get("items")
    if not isinstance(items_schema, dict):
        return
    if any(k in items_schema for k in _UNSUPPORTED_SCHEMA_KEYWORDS):
        return
    item_type = items_schema.get("type")
    if item_type in _PRIMITIVE_TYPES:
        for idx, item in enumerate(val):
            if not isinstance(item, _PRIMITIVE_TYPES[item_type]):
                raise ToolValidationError(f"{key}[{idx}]: 应为{item_type}", loc=[key])


def _check_json_object(key: str, val: Any, field_schema: dict) -> None:
    """检查 object 类型：确认是字典，并对已知子属性做一层有限类型检查。"""
    if not isinstance(val, dict):
        raise ToolValidationError(f"{key}: 应为对象", loc=[key])
    sub_props = field_schema.get("properties") or {}
    for sub_key, sub_val in val.items():
        if sub_key not in sub_props:
            continue
        _check_json_field(f"{key}.{sub_key}", sub_val, sub_props[sub_key])

