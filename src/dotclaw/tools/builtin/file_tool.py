"""文件读写工具（builtin 子包 — Phase 5 迁移）"""

from __future__ import annotations

from pathlib import Path

import aiofiles

from dotclaw.tools.handler import BuiltinToolHandler

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB


async def read_file(path: str) -> str:
    """读取文件全部内容"""
    try:
        file_path = Path(path).expanduser()
        if not file_path.exists():
            return f"错误：文件不存在 '{path}'"
        if not file_path.is_file():
            return f"错误：'{path}' 不是文件"
        if file_path.stat().st_size > MAX_FILE_SIZE:
            return f"错误：文件过大（{file_path.stat().st_size} bytes），超过限制 {MAX_FILE_SIZE} bytes"
        async with aiofiles.open(file_path, encoding="utf-8", errors="replace") as f:
            return await f.read()
    except Exception as e:
        return f"错误：{e}"


async def write_file(path: str, content: str) -> str:
    """写入文件"""
    try:
        file_path = Path(path).expanduser()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
            await f.write(content)
        return f"成功写入 {path} ({len(content)} 字符)"
    except Exception as e:
        return f"错误：{e}"


async def list_dir(path: str = ".") -> str:
    """列出目录"""
    try:
        dir_path = Path(path).expanduser()
        if not dir_path.exists():
            return f"错误：目录不存在 '{path}'"
        if not dir_path.is_dir():
            return f"错误：'{path}' 不是目录"

        entries = []
        for entry in sorted(dir_path.iterdir()):
            mark = "/" if entry.is_dir() else ""
            entries.append(f"  {entry.name}{mark}")
        return "\n".join(entries) if entries else "(空目录)"
    except Exception as e:
        return f"错误：{e}"


def get_read_file_handler() -> BuiltinToolHandler:
    return BuiltinToolHandler(
        name="read_file",
        description="读取文件内容",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径（绝对或相对路径）",
                }
            },
            "required": ["path"],
        },
        handler_fn=read_file,
        needs_approval=False,
        timeout=10.0,
    )


def get_write_file_handler() -> BuiltinToolHandler:
    return BuiltinToolHandler(
        name="write_file",
        description="写入内容到文件（覆盖）",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的内容",
                },
            },
            "required": ["path", "content"],
        },
        handler_fn=write_file,
        needs_approval=True,
        timeout=10.0,
    )


def get_list_dir_handler() -> BuiltinToolHandler:
    return BuiltinToolHandler(
        name="list_dir",
        description="列出目录内容",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "目录路径（默认当前目录）",
                }
            },
            "required": [],
        },
        handler_fn=list_dir,
        needs_approval=False,
        timeout=10.0,
    )
