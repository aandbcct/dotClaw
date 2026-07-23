"""受限计算工具 builtin.math.calculate 的契约测试（Tool v1 阶段四）。

覆盖开发计划阶段四验收：普通算术与白名单函数正确；代码注入、属性/下标访问、超长/
深层表达式、超大幂、除零和非有限结果均安全拒绝；该 Tool 不产生 Capability Request。

全程本地计算，不访问文件或网络。所有新增注释使用中文。
"""

from __future__ import annotations

import json

from dotclaw.tools.base import ToolErrorCode, ToolExecutionContext
from dotclaw.tools.builtin.math_tool import CalculateArgs, math_calculate
from dotclaw.tools.capability import CapabilityBroker
from dotclaw.tools.decorator import ToolPolicy
from dotclaw.tools.discovery import ToolDiscovery


def _ctx() -> ToolExecutionContext:
    return ToolExecutionContext(timeout=30.0)


async def test_math_basic_arithmetic() -> None:
    """普通算术按优先级正确求值。"""
    result = await math_calculate(CalculateArgs(expression="1 + 2 * 3"), _ctx())
    assert not result.is_error
    assert json.loads(result.output)["result"] == 7


async def test_math_float_and_parens() -> None:
    """浮点与括号正确。"""
    result = await math_calculate(CalculateArgs(expression="(3.14 + 0.86) * 2"), _ctx())
    assert not result.is_error
    assert json.loads(result.output)["result"] == 8.0


async def test_math_whitelisted_functions() -> None:
    """白名单数学函数可用。"""
    r1 = await math_calculate(CalculateArgs(expression="sqrt(16)"), _ctx())
    assert json.loads(r1.output)["result"] == 4.0
    r2 = await math_calculate(CalculateArgs(expression="log10(1000)"), _ctx())
    assert json.loads(r2.output)["result"] == 3.0


async def test_math_constants() -> None:
    """常量白名单可用（pi/e/tau）。"""
    result = await math_calculate(CalculateArgs(expression="pi"), _ctx())
    assert not result.is_error
    assert abs(json.loads(result.output)["result"] - 3.141592653589793) < 1e-9


async def test_math_power_and_unary() -> None:
    """幂运算与正负号正确。"""
    r1 = await math_calculate(CalculateArgs(expression="2 ** 10"), _ctx())
    assert json.loads(r1.output)["result"] == 1024
    r2 = await math_calculate(CalculateArgs(expression="-5 + 3"), _ctx())
    assert json.loads(r2.output)["result"] == -2
    r3 = await math_calculate(CalculateArgs(expression="7 // 2"), _ctx())
    assert json.loads(r3.output)["result"] == 3
    r4 = await math_calculate(CalculateArgs(expression="7 % 3"), _ctx())
    assert json.loads(r4.output)["result"] == 1


# ───────────────── 安全拒绝 ─────────────────


async def test_math_rejects_attribute_access() -> None:
    """属性访问（如 math.pi）被拒绝。"""
    result = await math_calculate(CalculateArgs(expression="math.pi"), _ctx())
    assert result.is_error
    assert result.error_code == ToolErrorCode.EXECUTION_ERROR.value


async def test_math_rejects_subscript() -> None:
    """下标访问被拒绝。"""
    result = await math_calculate(CalculateArgs(expression="pi[0]"), _ctx())
    assert result.is_error
    assert result.error_code == ToolErrorCode.EXECUTION_ERROR.value


async def test_math_rejects_assignment() -> None:
    """赋值语句（语法错误）被拒绝且不回显内部栈。"""
    result = await math_calculate(CalculateArgs(expression="a = 5"), _ctx())
    assert result.is_error
    assert result.error_code == ToolErrorCode.EXECUTION_ERROR.value
    assert "Traceback" not in result.output


async def test_math_rejects_import() -> None:
    """import 语句被拒绝。"""
    result = await math_calculate(CalculateArgs(expression="import os"), _ctx())
    assert result.is_error
    assert result.error_code == ToolErrorCode.EXECUTION_ERROR.value


async def test_math_rejects_name_lookup() -> None:
    """非白名单名称（如 os）查不到，被拒绝。"""
    result = await math_calculate(CalculateArgs(expression="os.getcwd()"), _ctx())
    assert result.is_error
    assert result.error_code == ToolErrorCode.EXECUTION_ERROR.value


async def test_math_rejects_code_injection() -> None:
    """代码注入（__import__）被拒绝。"""
    result = await math_calculate(CalculateArgs(expression="__import__('os').getcwd()"), _ctx())
    assert result.is_error
    assert result.error_code == ToolErrorCode.EXECUTION_ERROR.value


async def test_math_rejects_division_by_zero() -> None:
    """除零映射为安全的计算错误。"""
    result = await math_calculate(CalculateArgs(expression="1/0"), _ctx())
    assert result.is_error
    assert result.error_code == ToolErrorCode.EXECUTION_ERROR.value
    assert "除以零" in result.output


async def test_math_rejects_huge_exponent() -> None:
    """超大指数被拒绝（防止构造爆炸）。"""
    result = await math_calculate(CalculateArgs(expression="2 ** 100000"), _ctx())
    assert result.is_error
    assert result.error_code == ToolErrorCode.EXECUTION_ERROR.value


async def test_math_rejects_non_finite() -> None:
    """非有限结果（如 sqrt(-1)、(-1)**0.5）被拒绝。"""
    r1 = await math_calculate(CalculateArgs(expression="sqrt(-1)"), _ctx())
    assert r1.is_error
    r2 = await math_calculate(CalculateArgs(expression="(-1) ** 0.5"), _ctx())
    assert r2.is_error


def test_math_rejects_overlong_expression() -> None:
    """超长表达式在 Pydantic 校验期即被拒绝（>256 字符）。"""
    with __import__("pytest").raises(Exception):
        CalculateArgs(expression="1+" * 200)


def test_math_definition_has_no_capability_request() -> None:
    """math 工具无策略档案、无网络声明；Broker 不生成任何 Capability Request。"""
    handlers = {h.name: h for h in ToolDiscovery.discover_builtin()}
    assert "builtin.math.calculate" in handlers
    definition = handlers["builtin.math.calculate"].definition()
    # 计算类工具无受保护资源：policy 留空，且不声明网络。
    assert definition.policy_profile is None
    assert definition.network_service is None
    assert definition.network_hosts == []
    # Broker 对该工具不生成任何资源请求（阶段四验收：不产生 Capability Request）。
    requests = CapabilityBroker().resolve(definition, {}, workspace_root=".")
    assert requests == []
