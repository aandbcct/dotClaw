"""记忆工具（builtin 子包 — Tool v1 阶段二 @tool 迁移）。

工具名：builtin.memory.read / write。
业务参数由函数签名声明并由发现器生成内部模型；业务行为与迁移前保持一致。所有新增注释使用中文。
"""

from __future__ import annotations

from typing import Annotated
from pathlib import Path

import asyncio
from ..function_handler import _await_io_completion
from pydantic import Field

from dotclaw.tools.base import ToolContext
from dotclaw.tools.decorator import ToolPolicy, tool
from dotclaw.common.tool_replay import ToolSideEffect

DEFAULT_MEMORY_FILE = "./data/memory/MEMORY.md"


def _get_memory_path(long_term_file: str) -> Path:
    """获取 MEMORY.md 路径。"""
    path = Path(long_term_file).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@tool(
    name="builtin.memory.read",
    description="读取长期记忆（MEMORY.md）。",
    policy=ToolPolicy.WORKSPACE_READ,
    timeout=10.0,
    path_param="long_term_file",
    side_effect=ToolSideEffect.PRESENT,
)
async def read(long_term_file: Annotated[str, Field(description="MEMORY.md 路径")] = DEFAULT_MEMORY_FILE, *, context: ToolContext) -> str:
    def perform() -> str:
        """读取 MEMORY.md 内容。"""
        try:
            path = _get_memory_path(long_term_file)
            if not path.exists():
                return "(MEMORY.md 尚无内容)"
            with open(path, encoding="utf-8") as f:
                content = f.read()
            return content if content.strip() else "(MEMORY.md 为空)"
        except Exception as e:
            return f"错误：{e}"

    return await _await_io_completion(asyncio.to_thread(perform))


@tool(
    name="builtin.memory.write",
    description="追加写入长期记忆（MEMORY.md）。",
    policy=ToolPolicy.WORKSPACE_WRITE,
    needs_approval=True,
    timeout=10.0,
    path_param="long_term_file",
    side_effect=ToolSideEffect.PRESENT,
)
async def write(
    content: Annotated[str, Field(description="要追加的内容")],
    long_term_file: Annotated[str, Field(description="MEMORY.md 路径")] = DEFAULT_MEMORY_FILE,
    *, context: ToolContext,
) -> str:
    def perform() -> str:
        """追加内容到 MEMORY.md。"""
        try:
            path = _get_memory_path(long_term_file)
            with open(path, "a", encoding="utf-8") as f:
                # 确保有换行分隔
                existing = ""
                if path.exists():
                    with open(path, encoding="utf-8") as rf:
                        existing = rf.read()
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write(content)
                f.write("\n")
            return "已追加到 MEMORY.md"
        except Exception as e:
            return f"错误：{e}"

    return await _await_io_completion(asyncio.to_thread(perform))
