"""文件读写工具（builtin 子包 — Tool v1 阶段二 @tool 迁移）。

工具名：builtin.files.read_text / write_text / list_directory。
参数由 Pydantic 模型单一事实来源；业务行为与迁移前保持一致。所有新增注释使用中文。
"""

from __future__ import annotations

from pathlib import Path

import aiofiles
from pydantic import BaseModel, Field

from dotclaw.tools.base import ToolContext
from dotclaw.tools.decorator import ToolPolicy, tool

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB


class ReadTextArgs(BaseModel):
    """读取文本文件的参数。"""

    path: str = Field(description="文件路径（绝对或相对路径）")


class WriteTextArgs(BaseModel):
    """写入文本文件的参数。"""

    path: str = Field(description="文件路径")
    content: str = Field(description="要写入的内容")


class ListDirectoryArgs(BaseModel):
    """列出目录内容的参数。"""

    path: str = Field(default=".", description="目录路径（默认当前目录）")


@tool(
    name="builtin.files.read_text",
    description="读取工作区内的 UTF-8 文本文件。",
    args_model=ReadTextArgs,
    policy=ToolPolicy.WORKSPACE_READ,
    timeout=10.0,
)
async def read_text(args: ReadTextArgs, context: ToolContext) -> str:
    """读取文件全部内容。"""
    try:
        file_path = Path(args.path).expanduser()
        if not file_path.exists():
            return f"错误：文件不存在 '{args.path}'"
        if not file_path.is_file():
            return f"错误：'{args.path}' 不是文件"
        if file_path.stat().st_size > MAX_FILE_SIZE:
            return f"错误：文件过大（{file_path.stat().st_size} bytes），超过限制 {MAX_FILE_SIZE} bytes"
        async with aiofiles.open(file_path, encoding="utf-8", errors="replace") as f:
            return await f.read()
    except Exception as e:
        return f"错误：{e}"


@tool(
    name="builtin.files.write_text",
    description="写入内容到文件（覆盖）。",
    args_model=WriteTextArgs,
    policy=ToolPolicy.WORKSPACE_WRITE,
    needs_approval=True,
    timeout=10.0,
)
async def write_text(args: WriteTextArgs, context: ToolContext) -> str:
    """写入文件。"""
    try:
        file_path = Path(args.path).expanduser()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
            await f.write(args.content)
        return f"成功写入 {args.path} ({len(args.content)} 字符)"
    except Exception as e:
        return f"错误：{e}"


@tool(
    name="builtin.files.list_directory",
    description="列出目录内容。",
    args_model=ListDirectoryArgs,
    policy=ToolPolicy.WORKSPACE_READ,
    timeout=10.0,
)
async def list_directory(args: ListDirectoryArgs, context: ToolContext) -> str:
    """列出目录。"""
    try:
        dir_path = Path(args.path).expanduser()
        if not dir_path.exists():
            return f"错误：目录不存在 '{args.path}'"
        if not dir_path.is_dir():
            return f"错误：'{args.path}' 不是目录"

        entries = []
        for entry in sorted(dir_path.iterdir()):
            mark = "/" if entry.is_dir() else ""
            entries.append(f"  {entry.name}{mark}")
        return "\n".join(entries) if entries else "(空目录)"
    except Exception as e:
        return f"错误：{e}"
