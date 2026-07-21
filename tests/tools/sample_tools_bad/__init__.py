"""测试专用 @tool 包：含不支持推导的签名，Discovery 必须抛出 ToolDeclarationError。

所有新增注释使用中文。
"""

from __future__ import annotations

from typing import Optional

from dotclaw.tools.decorator import tool


@tool(name="sample.bad_optional", description="不支持：Optional 参数")
async def bad_optional(value: Optional[int]) -> str:
    """Optional 不在推导支持范围，Discovery 必须拒绝而非降级为无校验。"""
    return "x"
