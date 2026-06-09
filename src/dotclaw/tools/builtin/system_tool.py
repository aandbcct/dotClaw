"""系统信息工具（builtin 子包 — Phase 5 迁移）"""

from __future__ import annotations

import datetime
import os
import platform

from dotclaw.tools.handler import BuiltinToolHandler


async def system_info() -> str:
    """返回系统时间、当前目录、环境变量概要"""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cwd = os.getcwd()
    os_info = platform.system()

    env_keys = sorted(os.environ.keys())
    env_summary = ", ".join(
        k for k in env_keys
        if not k.upper().startswith("SECRET")
        and not k.upper().startswith("KEY")
        and not k.upper().startswith("PASSWORD")
    )

    return (
        f"当前时间: {now}\n"
        f"操作系统: {os_info}\n"
        f"当前目录: {cwd}\n"
        f"Python 版本: {platform.python_version()}\n"
        f"环境变量（不含敏感）: {env_summary}"
    )


async def get_time() -> str:
    """返回当前时间字符串"""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_system_info_handler() -> BuiltinToolHandler:
    return BuiltinToolHandler(
        name="system_info",
        description="获取系统基本信息，当用户提到系统信息详细内容，你需要根据系统信息回复时调用",
        parameters={"type": "object", "properties": {}, "required": []},
        handler_fn=system_info,
        needs_approval=False,
        timeout=10.0,
    )


def get_time_handler() -> BuiltinToolHandler:
    return BuiltinToolHandler(
        name="get_time",
        description="获取当前日期和时间,当用户问到任何与当前时间相关的内容时，先调用该tool获取当前时间",
        parameters={"type": "object", "properties": {}, "required": []},
        handler_fn=get_time,
        needs_approval=False,
        timeout=5.0,
    )
