"""内置受限计算工具（builtin 子包 — Tool v1 阶段四）。

工具名：builtin.math.calculate。在沙箱内对受限算术表达式求值，独立于进程执行 Tool 的
常规能力；不执行 Python 代码、不访问文件/网络/环境（开发计划 §2.5 / 阶段四）。

安全边界（纵深防御）：
1. Pydantic 校验 expression 长度（1–256 字符）。
2. 求值前对 AST 做白名单遍历：仅允许数值字面量、+ - * / // % **、正负号、括号与
   固定数学函数；拒绝属性访问、下标、赋值、导入、集合、非白名单名称等。
3. 求值前限制 AST 深度与节点数，避免构造爆炸。
4. 自定义幂运算限制指数与结果量级；结果非有限或复数一律拒绝。
5. 任何异常映射为脱敏的计算错误，绝不回显内部栈。

所有新增注释使用中文。
"""

from __future__ import annotations

import ast
import json
import logging
import math
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator
from simpleeval import SimpleEval

from dotclaw.tools.base import ToolContext, ToolErrorCode, ToolResult
from dotclaw.tools.decorator import tool

logger = logging.getLogger("dotclaw.tools.builtin.math")

# ── 受限表达式边界常量（开发计划阶段四） ──
_MAX_EXPR_LEN = 256          # 与 Pydantic 校验一致
_MAX_AST_DEPTH = 20          # AST 最大嵌套深度
_MAX_AST_NODES = 100         # AST 最大节点数
_MAX_RESULT_MAGNITUDE = 1e100  # 结果绝对值上限
_MAX_EXPONENT = 1000         # 幂运算指数绝对值上限

# 允许的二元/一元运算符（固定集合）。
_ALLOWED_BIN_OPS = {
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
}
_ALLOWED_UNARY_OPS = {ast.USub, ast.UAdd}

# 数学函数白名单：固定、确定性、不触及文件/网络/环境（不含 simpleeval 默认的 rand/randint）。
_MATH_FUNCS: dict[str, Any] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "pow": math.pow,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "exp": math.exp,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "floor": math.floor,
    "ceil": math.ceil,
    "gcd": math.gcd,
    "fabs": math.fabs,
    "degrees": math.degrees,
    "radians": math.radians,
}

# 常量白名单：仅固定数值，不做任何名称查找到函数或环境变量。
_MATH_NAMES: dict[str, float] = {"pi": math.pi, "e": math.e, "tau": math.tau}


class CalculateArgs(BaseModel):
    """计算参数（显式 Pydantic 模型，严格校验；extra=forbid）。"""

    model_config = ConfigDict(extra="forbid")

    expression: str

    @field_validator("expression")
    @classmethod
    def _validate_expression(cls, v: str) -> str:
        v = v.strip()
        if not (1 <= len(v) <= _MAX_EXPR_LEN):
            raise ValueError("expression 需为去除首尾空白后 1–256 字符")
        return v


def _safe_pow(base: float, exp: float) -> float:
    """受控幂运算：限制指数与结果量级，避免构造爆炸。"""
    if abs(exp) > _MAX_EXPONENT:
        raise ValueError("指数过大")
    result = base ** exp
    if isinstance(result, complex) or abs(result) > _MAX_RESULT_MAGNITUDE:
        raise ValueError("结果过大")
    return result


def _validate_ast(node: ast.AST, depth: int, counter: list[int]) -> None:
    """递归白名单遍历：仅允许受限语法，超限即抛安全异常。"""
    counter[0] += 1
    if counter[0] > _MAX_AST_NODES:
        raise ValueError("表达式过于复杂")
    if depth > _MAX_AST_DEPTH:
        raise ValueError("表达式嵌套过深")

    if isinstance(node, ast.Expression):
        _validate_ast(node.body, depth + 1, counter)
    elif isinstance(node, ast.BinOp):
        if type(node.op) not in _ALLOWED_BIN_OPS:
            raise ValueError("不支持的运算符")
        _validate_ast(node.left, depth + 1, counter)
        _validate_ast(node.right, depth + 1, counter)
    elif isinstance(node, ast.UnaryOp):
        if type(node.op) not in _ALLOWED_UNARY_OPS:
            raise ValueError("不支持的一元运算符")
        _validate_ast(node.operand, depth + 1, counter)
    elif isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float)) or isinstance(node.value, bool):
            raise ValueError("仅支持数值字面量")
    elif isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _MATH_FUNCS:
            raise ValueError("不支持的函数调用")
        if node.keywords:
            raise ValueError("不支持关键字参数")
        for arg in node.args:
            _validate_ast(arg, depth + 1, counter)
    elif isinstance(node, ast.Name):
        if node.id not in _MATH_NAMES:
            raise ValueError("不支持的名称")
    else:
        # 拒绝属性访问/下标/赋值/导入/集合/推导式等一切未显式允许的形态。
        raise ValueError("不支持的语法")


def _safe_eval(expression: str) -> float | int:
    """在受限沙箱内求值，返回有限数值；任何非法形态抛带安全信息的 ValueError。"""
    # ① 解析为 eval 模式 AST，先做白名单遍历。
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError("表达式语法错误") from exc
    _validate_ast(tree, 0, [0])

    # ② 用 simpleeval 求值，但完全覆盖函数/名称/幂运算，杜绝默认危险项。
    evaluator = SimpleEval()
    evaluator.functions = _MATH_FUNCS
    evaluator.names = _MATH_NAMES
    evaluator.operators = dict(evaluator.operators)
    evaluator.operators[ast.Pow] = _safe_pow

    result = evaluator.eval(expression)

    # ③ 结果必须是有限实数；复数/无穷/NaN 一律拒绝。
    if isinstance(result, complex):
        raise ValueError("结果不是实数")
    if isinstance(result, float) and not math.isfinite(result):
        raise ValueError("结果非有限")
    if isinstance(result, (int, float)) and abs(result) > _MAX_RESULT_MAGNITUDE:
        raise ValueError("结果过大")
    return result


@tool(
    name="builtin.math.calculate",
    description=(
        "对受限数学表达式求值（数据来自本地计算，不访问文件或网络）。支持 + - * / // % **、"
        "括号、固定数学函数（sqrt/log/exp/sin/cos 等）与常量 pi/e/tau。仅做数值计算，"
        "不执行任意代码。"
    ),
    args_model=CalculateArgs,
)
async def math_calculate(args: CalculateArgs, context: ToolContext) -> ToolResult:
    """执行受限表达式计算，返回脱敏的有限数值结果或安全的计算错误。"""
    try:
        value = _safe_eval(args.expression)
    except ZeroDivisionError:
        return ToolResult.from_error(
            code=ToolErrorCode.EXECUTION_ERROR, message="计算错误：除以零"
        )
    except (ValueError, OverflowError, TypeError) as exc:
        # 仅透出我们生成的类别化安全信息，绝不回显内部栈。
        return ToolResult.from_error(
            code=ToolErrorCode.EXECUTION_ERROR, message=f"计算错误：{exc}"
        )
    except Exception:  # 兜底：任何未预期异常都归为计算错误，不泄漏细节。
        logger.exception("math.calculate 求值异常")
        return ToolResult.from_error(
            code=ToolErrorCode.EXECUTION_ERROR, message="计算错误：表达式无法求值"
        )

    return ToolResult(
        output=json.dumps(
            {"expression": args.expression, "result": value}, ensure_ascii=False
        )
    )
