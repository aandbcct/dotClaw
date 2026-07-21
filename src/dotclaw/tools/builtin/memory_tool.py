"""记忆工具（builtin 子包 — Tool v1 阶段二 @tool 迁移）。

工具名：builtin.memory.read / write。
参数由 Pydantic 模型单一事实来源；业务行为与迁移前保持一致。所有新增注释使用中文。
"""

from __future__ import annotations

from pathlib import Path

import aiofiles
from pydantic import BaseModel, Field

from dotclaw.tools.base import ToolContext
from dotclaw.tools.decorator import ToolPolicy, tool

DEFAULT_MEMORY_FILE = "./data/memory/MEMORY.md"


class MemoryReadArgs(BaseModel):
    """读取长期记忆的参数。"""

    long_term_file: str = Field(
        default=DEFAULT_MEMORY_FILE, description="MEMORY.md 路径"
    )


class MemoryWriteArgs(BaseModel):
    """追加写入长期记忆的参数。"""

    content: str = Field(description="要追加的内容")
    long_term_file: str = Field(
        default=DEFAULT_MEMORY_FILE, description="MEMORY.md 路径"
    )


def _get_memory_path(long_term_file: str) -> Path:
    """获取 MEMORY.md 路径。"""
    path = Path(long_term_file).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@tool(
    name="builtin.memory.read",
    description="读取长期记忆（MEMORY.md）。",
    args_model=MemoryReadArgs,
    policy=ToolPolicy.WORKSPACE_READ,
    timeout=10.0,
)
async def read(args: MemoryReadArgs, context: ToolContext) -> str:
    """读取 MEMORY.md 内容。"""
    try:
        path = _get_memory_path(args.long_term_file)
        if not path.exists():
            return "(MEMORY.md 尚无内容)"
        async with aiofiles.open(path, encoding="utf-8") as f:
            content = await f.read()
        return content if content.strip() else "(MEMORY.md 为空)"
    except Exception as e:
        return f"错误：{e}"


@tool(
    name="builtin.memory.write",
    description="追加写入长期记忆（MEMORY.md）。",
    args_model=MemoryWriteArgs,
    policy=ToolPolicy.WORKSPACE_WRITE,
    needs_approval=True,
    timeout=10.0,
)
async def write(args: MemoryWriteArgs, context: ToolContext) -> str:
    """追加内容到 MEMORY.md。"""
    try:
        path = _get_memory_path(args.long_term_file)
        async with aiofiles.open(path, "a", encoding="utf-8") as f:
            # 确保有换行分隔
            existing = ""
            if path.exists():
                async with aiofiles.open(path, encoding="utf-8") as rf:
                    existing = await rf.read()
            if existing and not existing.endswith("\n"):
                await f.write("\n")
            await f.write(args.content)
            await f.write("\n")
        return "已追加到 MEMORY.md"
    except Exception as e:
        return f"错误：{e}"
