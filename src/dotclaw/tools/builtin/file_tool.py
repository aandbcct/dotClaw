"""文件读写工具（builtin 子包 — Tool v1 阶段二 @tool 迁移）。

工具名：builtin.files.read_text / write_text / list_directory。
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
from dotclaw.common.tool_replay import ToolReplayPolicy, ToolSideEffect

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
WORKSPACE_PATH_DESCRIPTION = (
    "以当前 workspace 为根的路径；优先使用相对路径，例如 out/result.json。"
    "也接受工作区内绝对路径；不要重复拼接 workspace 根目录"
)


@tool(
    name="builtin.files.read_text",
    description="读取工作区内的 UTF-8 文本文件。",
    policy=ToolPolicy.WORKSPACE_READ,
    timeout=10.0,
    replay_policy=ToolReplayPolicy.SAFE_RETRY, replay_contract_version="1",
    side_effect=ToolSideEffect.NONE, recovery_max_retries=3,
)
async def read_text(path: Annotated[str, Field(description=WORKSPACE_PATH_DESCRIPTION)], *, context: ToolContext) -> str:
    def perform() -> str:
        """读取文件全部内容。"""
        try:
            file_path = Path(path).expanduser()
            if not file_path.exists():
                return f"错误：文件不存在 '{path}'"
            if not file_path.is_file():
                return f"错误：'{path}' 不是文件"
            if file_path.stat().st_size > MAX_FILE_SIZE:
                return f"错误：文件过大（{file_path.stat().st_size} bytes），超过限制 {MAX_FILE_SIZE} bytes"
            with open(file_path, encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception as e:
            return f"错误：{e}"

    return await _await_io_completion(asyncio.to_thread(perform))


@tool(
    name="builtin.files.write_text",
    description="写入内容到文件（覆盖）。",
    policy=ToolPolicy.WORKSPACE_WRITE,
    needs_approval=True,
    timeout=10.0,
    side_effect=ToolSideEffect.PRESENT,
)
async def write_text(
    path: Annotated[str, Field(description=WORKSPACE_PATH_DESCRIPTION)],
    content: Annotated[str, Field(description="要写入的内容")],
    *, context: ToolContext,
) -> str:
    def perform() -> str:
        """写入文件。"""
        try:
            file_path = Path(path).expanduser()
            file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"成功写入 {path} ({len(content)} 字符)"
        except Exception as e:
            return f"错误：{e}"

    return await _await_io_completion(asyncio.to_thread(perform))


@tool(
    name="builtin.files.list_directory",
    description="列出目录内容。",
    policy=ToolPolicy.WORKSPACE_READ,
    timeout=10.0,
    replay_policy=ToolReplayPolicy.SAFE_RETRY, replay_contract_version="1",
    side_effect=ToolSideEffect.NONE, recovery_max_retries=3,
)
async def list_directory(path: Annotated[str, Field(description=WORKSPACE_PATH_DESCRIPTION)] = ".", *, context: ToolContext) -> str:
    def perform() -> str:
        """列出目录。"""
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

    return await _await_io_completion(asyncio.to_thread(perform))
