"""内置工具子包（Phase 5 新增）— 统一注册入口"""

from __future__ import annotations

from .exec_tool import get_exec_handler
from .file_tool import get_read_file_handler, get_write_file_handler, get_list_dir_handler
from .memory_tool import get_memory_read_handler, get_memory_write_handler
from .system_tool import get_system_info_handler, get_time_handler


def register_all(registry):
    """
    注册所有内置工具到注册表。
    在 main.py 启动时调用。
    """
    handlers = [
        get_exec_handler(),
        get_read_file_handler(),
        get_write_file_handler(),
        get_list_dir_handler(),
        get_memory_read_handler(),
        get_memory_write_handler(),
        get_system_info_handler(),
        get_time_handler(),
    ]
    for handler in handlers:
        registry.register(handler)
