"""测试专用 @tool 包：含一个导入即崩溃的子模块，验证 Discovery 记录失败不中断。

所有新增注释使用中文。
"""

from __future__ import annotations

from dotclaw.tools.decorator import tool


@tool(name="sample.broken_pkg.ok", description="可正常发现的工具")
async def ok() -> str:
    """即便同包内有子模块导入失败，此工具仍应被发现。"""
    return "ok"
