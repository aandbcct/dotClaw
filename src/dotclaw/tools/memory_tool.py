"""记忆工具"""

from __future__ import annotations

from pathlib import Path

import aiofiles

from .base import register_tool


def _get_memory_path(long_term_file: str) -> Path:
    """获取 MEMORY.md 路径"""
    path = Path(long_term_file).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@register_tool(
    name="memory_read",
    description="读取长期记忆（MEMORY.md）",
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
    },
)
async def memory_read(long_term_file: str = "./data/memory/MEMORY.md") -> str:
    """读取 MEMORY.md 内容"""
    try:
        path = _get_memory_path(long_term_file)
        if not path.exists():
            return "(MEMORY.md 尚无内容)"
        async with aiofiles.open(path, encoding="utf-8") as f:
            content = await f.read()
        return content if content.strip() else "(MEMORY.md 为空)"
    except Exception as e:
        return f"错误：{e}"


@register_tool(
    name="memory_write",
    description="追加写入长期记忆（MEMORY.md）",
    parameters={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "要追加的内容",
            }
        },
        "required": ["content"],
    },
)
async def memory_write(
    content: str,
    long_term_file: str = "./data/memory/MEMORY.md",
) -> str:
    """追加内容到 MEMORY.md"""
    try:
        path = _get_memory_path(long_term_file)
        async with aiofiles.open(path, "a", encoding="utf-8") as f:
            # 确保有换行分隔
            existing = ""
            if path.exists():
                async with aiofiles.open(path, encoding="utf-8") as rf:
                    existing = await rf.read()
            if existing and not existing.endswith("\n"):
                await f.write("\n")
            await f.write(content)
            await f.write("\n")
        return "已追加到 MEMORY.md"
    except Exception as e:
        return f"错误：{e}"
