"""测试专用 @tool 包：证明新增工具无需修改注册列表即可被 Discovery 发现。

包含：无参工具、可推导的基础类型参数工具、显式模型工具。所有新增注释使用中文。
"""

from __future__ import annotations

from pydantic import BaseModel

from dotclaw.tools.base import ToolContext
from dotclaw.tools.decorator import tool


@tool(name="sample.zero_arg", description="无参工具")
async def zero_arg() -> str:
    """无参工具，推导结果为无参对象。"""
    return "ok"


@tool(name="sample.basic_fields", description="基础类型参数（由签名推导）")
async def basic_fields(path: str, count: int = 3, context: ToolContext = None) -> str:
    """全部业务参数为 str/int，含字面量默认值；context 形参被推导忽略。"""
    return f"{path}:{count}"


class ExplicitArgs(BaseModel):
    """显式声明的参数模型。"""

    name: str = "default"


@tool(
    name="sample.explicit",
    description="显式模型工具",
    args_model=ExplicitArgs,
)
async def explicit(args: ExplicitArgs, context: ToolContext) -> str:
    """使用显式 args_model。"""
    return args.name
